# -*- coding: utf-8 -*-
"""【需求⑤】主力合约月份探测与缓存（购买建议标明具体合约时间段）：
枚举每个品种未来8个月的月份合约（四大交易所统一 nf_大写品种+4位年月），
按"成交量+持仓量"排序得到主力月份；期权月份优先结合 webdata.OpenVlab真实挂牌
月份与到期日（需求⑥）选择并计算精确剩余天数。
【需求⑥】refresh(exp_cal=...) 接入OpenVlab期权日历，只在真实挂牌月份中选期权月份。

原理（接口实测）：新浪行情接口可直接查询任意月份合约，且四大交易所统一使用
"nf_ + 品种字母(大写) + 4位年月" 格式（如 nf_RB2610、nf_TA2610、nf_SI2612），
返回字段中含成交量与持仓量。对每个品种枚举未来数月，按 成交量+持仓量 排序
即得到主力/次主力月份；期权月份优先选取估算剩余天数满足 OPT_MIN_DAYS 的
最活跃月份。
"""
import threading
import time
from datetime import date

import config
import futures_data
from utils import LOG


def month_candidates(count=None):
    """从当月起往后 count 个月的 (yy, mm) 列表"""
    count = count or config.CONTRACT_CANDIDATES
    today = date.today()
    y, m = today.year, today.month
    out = []
    for _ in range(count):
        out.append((y % 100, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def contract_code(sym, ex, yy, mm):
    """按交易所惯例生成期货合约代码：rb2610 / m2601 / MA601 / si2612"""
    if ex == "CZCE":
        return f"{sym.upper()}{yy % 10}{mm:02d}"
    return f"{sym.lower()}{yy:02d}{mm:02d}"


def option_code_hint(sym, ex, yy, mm, strike, kind):
    """期权合约代码示意（实际执行价以交易所挂牌为准）
    上期所/能源/广期所: cu2610C620   大商所: m2601-C-3000   郑商所: MA601C2870
    """
    ck = "C" if kind == "call" else "P"
    if ex == "CZCE":
        return f"{sym.upper()}{yy % 10}{mm:02d}{ck}{strike:g}"
    if ex == "DCE":
        return f"{sym.lower()}{yy:02d}{mm:02d}-{ck}-{strike:g}"
    return f"{sym.lower()}{yy:02d}{mm:02d}{ck}{strike:g}"


def estimate_option_days(yy, mm):
    """估算某月份期权剩余天数：按"交割月前一月中旬"近似各交易所到期规则"""
    today = date.today()
    year = 2000 + yy
    py, pm = (year, mm - 1) if mm > 1 else (year - 1, 12)
    try:
        expiry = date(py, pm, 10)
    except ValueError:
        return 0
    return (expiry - today).days


def days_to_delivery(yy, mm):
    """距离该合约交割月1号的天数（负数=已进入交割月）"""
    return (date(2000 + yy, mm, 1) - date.today()).days


def fetch_ranking(sym, count=None):
    """查询某品种未来数月合约，返回按活跃度(成交量+持仓量)降序的列表"""
    codes = [f"{sym.upper()}{yy:02d}{mm:02d}" for yy, mm in month_candidates(count)]
    quotes = futures_data.fetch_quotes(codes)
    ranked = []
    for code, q in quotes.items():
        yy, mm = int(code[-4:-2]), int(code[-2:])
        vol = q.get("volume", 0)
        oi = q.get("open_interest", 0)
        score = vol + oi
        if score <= 0:
            continue
        ranked.append({"code": code, "yy": yy, "mm": mm,
                       "vol": vol, "oi": oi, "score": score,
                       # 保留价格字段供期限结构组装（零额外请求）
                       "latest": float(q.get("latest") or 0.0),
                       "prev_settle": float(q.get("prev_settle") or 0.0),
                       "chg_pct": float(q.get("chg_pct") or 0.0)})
    ranked.sort(key=lambda x: -x["score"])
    return ranked


class ContractCache:
    """sym -> {"main": 主力月份, "opt_month": 期权建议月份, "list": 全部活跃月份}"""

    def __init__(self):
        self.cache = {}
        self.lock = threading.Lock()

    def refresh(self, syms, exp_cal=None):
        """逐品种重新探测（约每个品种1次请求），完成后整体替换缓存。
        exp_cal: OpenVlab真实期权日历 {sym: {yymm: {"exp_date": date}}}，
        提供时只在真实挂牌月份中选期权月份并使用真实到期天数。"""
        exp_cal = exp_cal or {}
        fresh = {}
        for sym in syms:
            try:
                ranked = fetch_ranking(sym)
            except Exception as e:
                LOG.warning("%s 合约月份探测失败: %s", sym, e)
                continue
            info = {"main": None, "opt_month": None, "list": ranked}
            if ranked:
                info["main"] = ranked[0]
                cal = exp_cal.get(sym) or {}
                # 日历可用时只在真实挂牌月份里选，否则在活跃月份里选
                cands = [r for r in ranked if (r["yy"] * 100 + r["mm"]) in cal] or ranked
                for r in cands:
                    key = r["yy"] * 100 + r["mm"]
                    if key in cal:
                        d = (cal[key]["exp_date"] - date.today()).days
                        info["opt_month"] = dict(r, opt_days=d,
                                                 exp_date=cal[key]["exp_date"],
                                                 opt_src="OpenVlab真实到期日")
                        break
                    d = estimate_option_days(r["yy"], r["mm"])
                    if d >= config.OPT_MIN_DAYS:
                        info["opt_month"] = dict(r, opt_days=d,
                                                 opt_src="估算(交割月前一月中旬)")
                        break
                if info["opt_month"] is None:
                    r = cands[0]
                    key = r["yy"] * 100 + r["mm"]
                    if key in cal:
                        d = (cal[key]["exp_date"] - date.today()).days
                        info["opt_month"] = dict(r, opt_days=max(d, 5),
                                                 exp_date=cal[key]["exp_date"],
                                                 opt_src="OpenVlab真实到期日")
                    else:
                        info["opt_month"] = dict(r, opt_days=max(
                            estimate_option_days(r["yy"], r["mm"]), 5),
                            opt_src="估算(交割月前一月中旬)")
            fresh[sym] = (time.time(), info)
        if fresh:
            with self.lock:
                self.cache.update(fresh)
            LOG.info("主力合约月份探测完成（%d/%d 个品种）", len(fresh), len(syms))

    def get(self, sym, ttl=None):
        ttl = ttl or config.CONTRACT_TTL
        now = time.time()
        with self.lock:
            hit = self.cache.get(sym)
            if hit and now - hit[0] < ttl:
                return hit[1]
        return None


def _month_index(yy, mm):
    """2位年+月 -> 可比较的月份序号"""
    return yy * 12 + mm


def term_structure(info):
    """由合约月份探测结果组装期限结构（零新增数据源，价格来自月份探测同一次请求）。

    返回:
      None（有效月份不足）或
      {"months":[(label,code,price,oi)], "near_label","far_label","spread","spread_pct",
       "annual_carry","shape","slope_note","note"}
    口径：按日历月升序排列全部有效月份合约；近月=最早月份、远月=最晚月份；
    年化展期收益率=(近月/远月-1)*365/间隔天数；近高远低=反向(Back)、近低远高=正向(Contango)。
    """
    if not info:
        return None
    rows = [r for r in (info.get("list") or []) if r.get("latest", 0) > 0]
    rows.sort(key=lambda r: _month_index(r["yy"], r["mm"]))
    if len(rows) < config.TERM_MIN_MONTHS:
        return None
    months = [{"label": f"{r['yy']:02d}{r['mm']:02d}", "code": r["code"],
               "price": r["latest"], "oi": r.get("oi", 0)} for r in rows]
    near, far = rows[0], rows[-1]
    gap_days = max(1.0, (_month_index(far["yy"], far["mm"])
                         - _month_index(near["yy"], near["mm"])) * 30.42)
    spread = near["latest"] - far["latest"]
    spread_pct = spread / far["latest"] if far["latest"] > 0 else 0.0
    annual_carry = (near["latest"] / far["latest"] - 1.0) * config.TERM_ANNUAL_DAYS / gap_days
    # 用前2与后2均价判定形态，降低单月噪声
    head = sum(r["latest"] for r in rows[:2]) / min(2, len(rows))
    tail = sum(r["latest"] for r in rows[-2:]) / min(2, len(rows))
    if head > tail * 1.001:
        shape = "反向市场(近高远低)"
        slope_note = "近月升水，现货偏紧/Back结构，远月贴水"
    elif head < tail * 0.999:
        shape = "正向市场(近低远高)"
        slope_note = "近月贴水、远月升水/Contango结构，持有成本主导"
    else:
        shape = "近远月平水"
        slope_note = "月间价差很小，期限结构平坦"
    seq = "、".join(f"{m['label']}:{m['price']:g}" for m in months[:6])
    if len(months) > 6:
        seq += "…"
    note = (f"期限结构 {shape}；{near['code']} {near['latest']:g} vs {far['code']} {far['latest']:g}"
            f"，近-远月差{spread:+.1f}({spread_pct*100:+.2f}%)，年化展期收益率{annual_carry*100:+.2f}%；"
            f"{slope_note}｜月序 {seq}")
    return {"months": months, "near_label": months[0]["label"], "far_label": months[-1]["label"],
            "near_price": near["latest"], "far_price": far["latest"],
            "spread": spread, "spread_pct": spread_pct, "annual_carry": annual_carry,
            "shape": shape, "slope_note": slope_note, "note": note}
