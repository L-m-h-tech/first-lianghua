# -*- coding: utf-8 -*-
"""G22（第34轮）多合约期限结构 + 持仓量(OI)连续历史重建 —— 研究侧数据底座。

为什么需要它（第33轮对标结论）：
  主连比例后复权（backtest.ratio_adjusted_bars）适合价格动量，但**算不了展期收益 carry**——
  carry 必须用真实的"近月 / 次月 / 远月"合约价。contracts.term_structure 只能给"当下"一条
  期限结构、没有历史序列。本模块用新浪逐合约日K（已实测：任意具体合约如 RB2501 可取完整
  生命周期日K，字段 p=持仓量 open_interest、s=结算价 settlement），重建**历史上每个交易日**
  的近/次/远月曲线，从而为 G23 截面 carry 双样本、G24 持仓量因子提供干净的研究输入。

口径（与 contracts.term_structure / fundamental_factors.carry_factor 对齐，保证可对照）：
  - 新浪日K合约代码统一"大写品种+4位年月"（RB2501/TA2501/M2501 均可；郑商所三位年 TA501 取不到）；
  - 近月=距交割月1号仍大于换月缓冲、且当日有持仓/结算价的最临近合约，依次次月、远月；
  - 年化展期收益率 = (近月结算/远月结算 - 1) * 365 / 两合约交割月1号间隔日历日，
    近高远低=反向市场 Backwardation、carry>0（多头持有近月有正 roll-down），与现有口径同号；
  - level/slope/curvature 用 Nelson-Siegel 风格固定载荷（[1,1,1]/[1,0,-1]/[1,-2,1]，对数结算价），
    是期限结构 PCA 平移/斜率/曲率三主成分的经典等权近似，纯标准库、无需特征分解；
  - 连续持仓量：当日全部存续合约持仓之和 oi_sum（市场总 OI）、近月持仓 oi_near。

纪律：纯标准库零新增依赖；本模块本轮**只被研究工具 tools/carry_eval.py 调用、不接入 main 常驻
主链、不改综合分**；逐合约日K缓存到 cache/term_history.db（被 .gitignore，可随时重建、不入库）。
"""
import math
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import config
import futures_data
from utils import LOG

# 换月缓冲（自然日）：距交割月1号 <= 该值的合约不再选为近/次/远月（规避交割月薄流动性）
ROLL_BUFFER_DAYS = 3
# 近月存续的最小持仓量（手）：低于此视为已摘牌/无量
MIN_OPEN_INTEREST = 1
TERM_DB_PATH = os.path.join("cache", "term_history.db")


# =========================== 纯函数：月份 / 代码 / 交割（零网络、可手算） ===========================
def month_iter(start_yy, start_mm, end_yy, end_mm):
    """枚举闭区间 [(yy,mm),...]，yy 为两位年（24=2024），自动跨年、升序。"""
    out = []
    yy, mm = start_yy, start_mm
    while (yy, mm) <= (end_yy, end_mm):
        out.append((yy, mm))
        mm += 1
        if mm > 12:
            mm, yy = 1, yy + 1
    return out


def kline_symbol(sym, yy, mm):
    """新浪日K接口合约代码：大写品种字母 + 4位年月（四所统一，实测三位年取不到）。"""
    return "%s%02d%02d" % (str(sym).upper(), yy, mm)


def parse_yymm(code):
    """代码末4位 -> (yy,mm)；非数字/长度不足返回 None。"""
    tail = str(code)[-4:]
    if len(tail) == 4 and tail.isdigit():
        yy, mm = int(tail[:2]), int(tail[2:])
        if 1 <= mm <= 12:
            return yy, mm
    return None


def full_year(yy):
    """两位年 -> 四位年（窗口 2000-2099）。"""
    return 2000 + yy


def delivery_first(yy, mm):
    """该合约交割月1号的 date（精确交割日各所不同；近月选择只需月序与大致间隔，用月首足够）。"""
    return date(full_year(yy), mm, 1)


def month_gap_days(near_yy, near_mm, far_yy, far_mm):
    """两合约交割月1号之间的日历天数（近月在前，>0）。"""
    return (delivery_first(far_yy, far_mm) - delivery_first(near_yy, near_mm)).days


def select_curve(on_date, live, roll_buffer_days=ROLL_BUFFER_DAYS, min_oi=MIN_OPEN_INTEREST):
    """在 on_date 当天的候选合约中选出 (near, next_, far)。

    live: [{code,yy,mm,settle,oi,vol}, ...]（当日有日K的合约）。
    规则：剔除结算价非正、持仓 < min_oi、以及距交割月1号 <= roll_buffer_days（临近/进入交割月）的合约；
    其余按交割月升序，near=首个、next=第二个、far=第三个；不足的位置为 None。
    """
    kept = []
    for c in live:
        if not (c.get("settle", 0.0) and c["settle"] > 0):
            continue
        if c.get("oi", 0) < min_oi:
            continue
        d_first = delivery_first(c["yy"], c["mm"])
        if (d_first - on_date).days <= roll_buffer_days:
            continue
        kept.append(c)
    kept.sort(key=lambda c: (c["yy"], c["mm"]))
    near = kept[0] if len(kept) >= 1 else None
    nxt = kept[1] if len(kept) >= 2 else None
    far = kept[2] if len(kept) >= 3 else None
    return near, nxt, far


def annual_carry(near_settle, far_settle, gap_days, annual_days=365.0):
    """年化展期收益率 =(近/远-1)*365/间隔日历日；gap<=0 或价格非法返回 None。"""
    if not gap_days or gap_days <= 0:
        return None
    if not (near_settle and far_settle and near_settle > 0 and far_settle > 0):
        return None
    return (near_settle / far_settle - 1.0) * annual_days / gap_days


def curve_loadings(p_near, p_next, p_far):
    """Nelson-Siegel 风格三载荷（对数结算价），返回 (level,slope,curvature)，缺腿对应分量为 None。

    level=(ln近+ln次+ln远)/3 平移；slope=ln近-ln远 斜率（>0 近高远低=Back）；
    curvature=ln近-2ln次+ln远 曲率。价格须全为正。
    """
    def _ln(x):
        return math.log(x) if x and x > 0 else None
    l1, l2, l3 = _ln(p_near), _ln(p_next), _ln(p_far)
    level = (l1 + l2 + l3) / 3.0 if None not in (l1, l2, l3) else None
    slope = (l1 - l3) if (l1 is not None and l3 is not None) else None
    curv = (l1 - 2 * l2 + l3) if None not in (l1, l2, l3) else None
    return level, slope, curv


def moving_mean(seq, n):
    """对浮点序列做 n 日简单移动平均（结果与 seq 等长，暖机不足为 None；None 透传跳过）。"""
    out = [None] * len(seq)
    if n <= 1:
        return list(seq)
    buf = []
    for i, v in enumerate(seq):
        if v is None:
            buf = []
            continue
        buf.append(v)
        if len(buf) > n:
            buf.pop(0)
        if len(buf) == n:
            out[i] = sum(buf) / n
    return out


def basis_change(seq, n):
    """序列的近 n 日差分（basis momentum = 展期/基差的变化量），头部不足为 None。"""
    out = [None] * len(seq)
    for i in range(n, len(seq)):
        a, b = seq[i], seq[i - n]
        if a is not None and b is not None:
            out[i] = a - b
    return out


def near_roll_nav(term_series):
    """近月连续净值（学术 carry 的正确收益口径，含展期 roll）。

    持续持有"当时近月合约"：同一近月合约内吃结算价逐日变动（Back 结构下近月随到期向现货收敛，
    这段 roll-down 被保留）；近月合约切换（换月）当天不跨合约计收益（置 0，与主连 ratio 复权同款
    换月处理，避免把两合约价差当盈亏）。返回与 term_series 等长的净值（起点 1.0；无近月/首日为 None）。

    对照：主连比例复权（backtest.ratio_adjusted_bars）选的是成交最大的主力、且复权因子会把换月
    跳空抹平，等于把展期收益从价格路径里拿掉；检验 carry 必须同时给"近月连续含 roll"口径，否则
    会错误地把 carry 最主要的收益来源剔除。
    """
    nav, prev, out = 1.0, None, []
    for r in term_series:
        code, s = r.get("near"), r.get("near_s")
        if not code or not s or s <= 0:
            out.append(None)
            continue
        if prev is not None and prev[0] == code and prev[1] > 0:
            nav *= s / prev[1]
        out.append(nav)
        prev = (code, s)
    return out


def _settle_of(bar):
    """优先结算价 s，缺失退回收盘价 c（统一转 float；新浪字段为字符串）。"""
    v = futures_data._f(bar.get("s"))
    if not v or v <= 0:
        v = futures_data._f(bar.get("c"))
    return v


def build_term_series(contract_bars, roll_buffer_days=ROLL_BUFFER_DAYS, min_oi=MIN_OPEN_INTEREST):
    """核心纯函数：{合约代码: [日K bar...]} -> 逐交易日的期限结构序列（日期升序）。

    每个输出元素：
      {date, near,next,far(代码), near_s,next_s,far_s(结算价),
       carry_far(近-远年化), carry_nn(近-次年化), level,slope,curv,
       oi_sum(当日存续合约总持仓), oi_near(近月持仓), n_live(当日存续合约数)}
    无有效曲线的交易日仍记录 oi_sum/n_live，但 carry 字段为 None。
    """
    # code -> (yy,mm, {date: bar})
    parsed = {}
    all_dates = set()
    for code, bars in contract_bars.items():
        ym = parse_yymm(code)
        if ym is None or not bars:
            continue
        day_map = {}
        for b in bars:
            d = str(b.get("d", ""))
            if d:
                day_map[d] = b
                all_dates.add(d)
        parsed[code] = (ym[0], ym[1], day_map)
    out = []
    for d in sorted(all_dates):
        on = date(int(d[:4]), int(d[5:7]), int(d[8:10]))
        live, oi_sum, vol_sum = [], 0, 0
        for code, (yy, mm, day_map) in parsed.items():
            b = day_map.get(d)
            if b is None:
                continue
            settle = _settle_of(b)
            oi = int(futures_data._f(b.get("p")) or 0)
            vol = int(futures_data._f(b.get("v")) or 0)
            if oi > 0:
                oi_sum += oi
            vol_sum += vol
            live.append({"code": code, "yy": yy, "mm": mm,
                         "settle": settle, "oi": oi, "vol": vol})
        n_live = len(live)
        near, nxt, far = select_curve(on, live, roll_buffer_days, min_oi)
        row = {"date": d, "near": None, "next": None, "far": None,
               "near_s": None, "next_s": None, "far_s": None,
               "carry_far": None, "carry_nn": None,
               "level": None, "slope": None, "curv": None,
               "oi_sum": oi_sum, "oi_near": None, "vol_sum": vol_sum,
               "near_vol": None, "n_live": n_live}
        if near:
            row.update(near=near["code"], near_s=near["settle"], oi_near=near["oi"],
                       near_vol=near["vol"])
        if nxt:
            row.update(next=nxt["code"], next_s=nxt["settle"])
        if far:
            row.update(far=far["code"], far_s=far["settle"])
        if near and nxt:
            g = month_gap_days(near["yy"], near["mm"], nxt["yy"], nxt["mm"])
            row["carry_nn"] = annual_carry(near["settle"], nxt["settle"], g)
        if near and far:
            g = month_gap_days(near["yy"], near["mm"], far["yy"], far["mm"])
            row["carry_far"] = annual_carry(near["settle"], far["settle"], g)
        if near:
            lv, sl, cv = curve_loadings(near["settle"],
                                        nxt["settle"] if nxt else None,
                                        far["settle"] if far else None)
            row["level"], row["slope"], row["curv"] = lv, sl, cv
        out.append(row)
    return out


# =========================== 本地缓存（SQLite，研究侧、可重建、不入库） ===========================
class TermHistoryStore:
    """逐合约日K的本地缓存；写操作加锁（供多线程下载共用一个连接）。"""

    def __init__(self, path=TERM_DB_PATH):
        self.path = path
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self):
        with self.lock:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS ckline ("
                "sym TEXT NOT NULL, code TEXT NOT NULL, d TEXT NOT NULL, "
                "o REAL, h REAL, l REAL, c REAL, s REAL, v INTEGER, p INTEGER, "
                "PRIMARY KEY(code,d))")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_ckline_sym ON ckline(sym)")
            # 已确认无数据/摘牌的合约，避免重复请求空合约
            self.conn.execute("CREATE TABLE IF NOT EXISTS cempty(code TEXT PRIMARY KEY, sym TEXT)")
            self.conn.commit()

    def is_empty_marked(self, code):
        with self.lock:
            cur = self.conn.execute("SELECT 1 FROM cempty WHERE code=?", (code,))
            return cur.fetchone() is not None

    def count_bars(self, code):
        with self.lock:
            cur = self.conn.execute("SELECT COUNT(*) FROM ckline WHERE code=?", (code,))
            return cur.fetchone()[0]

    def max_bar_date(self, code):
        """该合约缓存中的末根日期（"YYYY-MM-DD" 或 None）——第77轮 top-up 用。"""
        with self.lock:
            cur = self.conn.execute("SELECT MAX(d) FROM ckline WHERE code=?", (code,))
            row = cur.fetchone()
            return row[0] if row and row[0] else None

    def save_contract(self, sym, code, bars):
        """落库一个合约的日K；bars 为空则登记到 cempty。返回写入根数。"""
        with self.lock:
            if not bars:
                self.conn.execute("INSERT OR IGNORE INTO cempty(code,sym) VALUES(?,?)", (code, sym))
                self.conn.commit()
                return 0
            rows = []
            for b in bars:
                rows.append((sym, code, str(b.get("d", "")), futures_data._f(b.get("o")),
                             futures_data._f(b.get("h")), futures_data._f(b.get("l")),
                             futures_data._f(b.get("c")), _settle_of(b),
                             int(futures_data._f(b.get("v")) or 0), int(futures_data._f(b.get("p")) or 0)))
            self.conn.executemany(
                "INSERT OR REPLACE INTO ckline(sym,code,d,o,h,l,c,s,v,p) VALUES(?,?,?,?,?,?,?,?,?,?)",
                rows)
            self.conn.execute("DELETE FROM cempty WHERE code=?", (code,))
            self.conn.commit()
            return len(rows)

    def load_contract_bars(self, sym):
        """读出某品种全部已缓存合约 {code: [bar...]}（bar 字段与新浪日K一致：d/o/h/l/c/s/v/p）。"""
        with self.lock:
            cur = self.conn.execute(
                "SELECT code,d,o,h,l,c,s,v,p FROM ckline WHERE sym=? ORDER BY d", (sym,))
            out = {}
            for code, d, o, h, l, c, s, v, p in cur.fetchall():
                out.setdefault(code, []).append(
                    {"d": d, "o": o, "h": h, "l": l, "c": c, "s": s, "v": v, "p": p})
            return out

    def cached_codes(self, sym):
        with self.lock:
            cur = self.conn.execute("SELECT DISTINCT code FROM ckline WHERE sym=?", (sym,))
            return {r[0] for r in cur.fetchall()}

    def close(self):
        with self.lock:
            self.conn.close()


# =========================== 联网下载（研究侧离线跑，失败软降级、不编造） ===========================
def fetch_one_contract(sym, yy, mm, store, retry=2, pause=0.25, force=False):
    """下载单个合约日K并落库；返回 (code, n_bars, status)。已缓存/已标空直接跳过。

    force=True（第77轮 top-up 用）忽略"已缓存即跳过"，重拉并按 INSERT OR REPLACE 合并
    （重复日期以新数据覆盖，新日期追加）——用于已缓存合约末根落后的增量补K线。"""
    code = kline_symbol(sym, yy, mm)
    if store.is_empty_marked(code) and not force:
        return code, 0, "empty-marked"
    if not force and store.count_bars(code) > 0:
        return code, store.count_bars(code), "cached"
    try:
        bars = futures_data.fetch_daily_kline(code, retry=retry)
    except Exception as e:  # 单合约失败不阻断，记状态、下次可重试
        return code, 0, "error:%s" % type(e).__name__
    n = store.save_contract(sym, code, bars)
    if pause:
        time.sleep(pause)
    return code, n, ("saved" if n else "empty")


# =========================== 第77轮：增量补K线（top-up，修"缓存不回补"缺口） ===========================
def _parse_date(s):
    y, m, d = str(s).split("-")
    return date(int(y), int(m), int(d))


def topup_decide(today, entries, stale_days=10):
    """决定哪些合约需要增量补K线（纯函数，零网络）。

    entries=[(sym, code, yy, mm, max_date_str_or_None)]。规则：
      无缓存 → "new"；合约月仍在挂牌（yy/mm ≥ 上个日历月）且末根早于 today-stale_days → "stale"；
      已退市合约（末根天然早于今天）不补——避免对历史合约做无谓重拉。
    返回 {code: "new"|"stale"}。"""
    cutoff = today - timedelta(days=stale_days)
    last_month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    out = {}
    for _sym, code, yy, mm, maxd in entries:
        if not maxd:
            out[code] = "new"
            continue
        if (full_year(yy), mm) >= last_month and _parse_date(maxd) < cutoff:
            out[code] = "stale"
    return out


def topup_varieties(items, store, months_back=6, stale_days=10, workers=6, pause=0.1,
                    today=None, verbose=True):
    """对品种列表做增量补K线（第77轮，G22续④常驻采集的离线等价物）。

    items=[(中文名, 主连code)]（与 carry_eval.resolve_codes 输出同形）。
    枚举每品种近 months_back 个月的合约：无缓存→下载（new）；仍挂牌且末根落后 stale_days→
    重拉合并（stale，INSERT OR REPLACE 幂等）；已退市不补。返回统计 dict。"""
    import config
    today = today or date.today()
    months = []
    y, m = today.year, today.month
    for _ in range(months_back):
        months.append((y % 100, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    entries, sym_of = [], {}
    for name, main_code in items:
        meta = config.VARIETIES.get(name, {})
        sym = meta.get("sym") or main_code.rstrip("0")
        sym_of[sym] = name
        for yy, mm in months:
            code = kline_symbol(sym, yy, mm)
            if store.is_empty_marked(code):
                continue
            entries.append((sym, code, yy, mm, store.max_bar_date(code)))
    plan = topup_decide(today, entries, stale_days=stale_days)
    stats = {"checked": len(entries), "new": 0, "stale": 0, "fresh": len(entries) - len(plan),
             "errors": []}
    todo = [(c, r) for c, r in plan.items()]
    job_sym = {}
    for sym, code, yy, mm, _maxd in entries:
        job_sym[code] = (sym, yy, mm)
    if verbose and todo:
        print("top-up：检查 %d 个近月合约，需处理 %d（new %d / stale %d）"
              % (len(entries), len(todo), stats["new"], stats["stale"]))

    def _job(item):
        code, reason = item
        sym, yy, mm = job_sym[code]
        return fetch_one_contract(sym, yy, mm, store, pause=pause,
                                  force=(reason == "stale"))

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for code, _n, status in pool.map(_job, todo):
                reason = plan[code]
                if reason == "new":
                    stats["new"] += 1
                else:
                    stats["stale"] += 1
                if status.startswith("error"):
                    stats["errors"].append("%s:%s" % (code, status))
    return stats


def build_symbol_range(sym, start_yy, start_mm, end_yy, end_mm, store,
                       workers=6, retry=2, pause=0.2, progress=False):
    """枚举一个品种 [start,end] 的全部月份合约并下载落库（多线程、增量跳过已缓存）。

    返回 {code: status}。无效/未挂牌合约会被新浪返回空、自动登记 cempty，不影响其它合约。
    """
    months = month_iter(start_yy, start_mm, end_yy, end_mm)
    result = {}

    def _job(yy_mm):
        yy, mm = yy_mm
        return fetch_one_contract(sym, yy, mm, store, retry=retry, pause=pause)

    if workers <= 1:
        for ym in months:
            code, n, st = _job(ym)
            result[code] = st
            if progress:
                LOG.info("%s %s -> %d (%s)", sym, code, n, st)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_job, ym) for ym in months]
            for fut in as_completed(futs):
                code, n, st = fut.result()
                result[code] = st
                if progress:
                    LOG.info("%s %s -> %d (%s)", sym, code, n, st)
    return result


def term_series_for(sym, store, **kw):
    """便捷：从缓存读出某品种合约日K并重建期限结构序列（不联网；需先 build_symbol_range）。"""
    bars = store.load_contract_bars(sym)
    return build_term_series(bars, **kw)


# =========================== 第80轮：近月比例复权 OHLC 长序列（G25续长样本链） ===========================
def adjusted_near_ohlc(sym, store, warmup=126, roll_buffer_days=ROLL_BUFFER_DAYS,
                       min_oi=MIN_OPEN_INTEREST):
    """从逐合约缓存重建"近月合约比例复权"的 OHLC 长序列（第80轮，不联网）。

    近月选择复用 build_term_series（换月缓冲/剔除临交割/无量剔除）；换月日调整系数
    ×= 旧近月收/新近月收，使序列连续（换月日收益≈0，与 G21 面板 ratio_adjusted 同口径）。
    另现算 ret126/hv60 两列（G25 引擎），供 regime 标签直接消费。
    返回 rows=[{date,c,h,l,o,ret126,hv60}]（按日期升序；无数据返回 []）。"""
    import factor_expr as _fx
    bars = store.load_contract_bars(sym)
    if not bars:
        return []
    day_bar = {}
    for code, blist in bars.items():
        for b in blist:
            d = str(b.get("d", ""))
            if d:
                day_bar[(code, d)] = b
    ts = build_term_series(bars, roll_buffer_days, min_oi)
    rows, factor, prev_code, prev_close = [], 1.0, None, None
    for r in ts:
        code = r.get("near")
        b = day_bar.get((code, r["date"])) if code else None
        if b is None:
            continue
        c = futures_data._f(b.get("c"))
        if not c or c <= 0:
            continue
        if prev_code is not None and code != prev_code and prev_close:
            factor *= prev_close / c          # 换月拼接：新近月缩放到旧序列水平
        rows.append({"date": r["date"], "c": c * factor,
                     "h": futures_data._f(b.get("h")) * factor,
                     "l": futures_data._f(b.get("l")) * factor,
                     "o": futures_data._f(b.get("o")) * factor})
        prev_code, prev_close = code, c
    if not rows:
        return []
    series = {"close": [r["c"] for r in rows]}
    ret126 = _fx.compute_ts("close/delay(close,%d)-1" % warmup, series)
    hv60 = _fx.compute_ts("ts_std(log(close/delay(close,1)),60)*15.874507866387544", series)
    for i, r in enumerate(rows):
        r["ret126"] = ret126[i] if _fx._isnum(ret126[i]) else None
        r["hv60"] = hv60[i] if _fx._isnum(hv60[i]) else None
    return rows


# =========================== 合成自测（零网络；被 tests 与 --selftest 复用） ===========================
def _selftest():
    # 1) 月份枚举跨年、代码与解析互逆
    assert month_iter(24, 11, 25, 2) == [(24, 11), (24, 12), (25, 1), (25, 2)]
    assert kline_symbol("ta", 25, 1) == "TA2501"
    assert parse_yymm("RB2501") == (25, 1) and parse_yymm("bad") is None
    assert full_year(25) == 2025
    assert month_gap_days(25, 1, 25, 3) == (date(2025, 3, 1) - date(2025, 1, 1)).days

    # 2) select_curve：剔除临交割月/无量，按交割月排序选近/次/远
    on = date(2024, 12, 20)
    live = [
        {"code": "X2501", "yy": 25, "mm": 1, "settle": 100.0, "oi": 10, "vol": 1},  # 距1/1=12天，保留=近
        {"code": "X2412", "yy": 24, "mm": 12, "settle": 99.0, "oi": 10, "vol": 1},   # 已进交割月，剔除
        {"code": "X2503", "yy": 25, "mm": 3, "settle": 102.0, "oi": 8, "vol": 1},    # 远
        {"code": "X2502", "yy": 25, "mm": 2, "settle": 101.0, "oi": 9, "vol": 1},    # 次
        {"code": "X2504", "yy": 25, "mm": 4, "settle": 0.0, "oi": 9, "vol": 1},      # 无价剔除
    ]
    near, nxt, far = select_curve(on, live, roll_buffer_days=3, min_oi=1)
    assert near["code"] == "X2501" and nxt["code"] == "X2502" and far["code"] == "X2503"
    # 临近交割缓冲：把 on 推到 12/30，则 X2501 距1/1仅2天<=3 被剔除，近月变 X2502
    near2, nxt2, _ = select_curve(date(2024, 12, 30), live, 3, 1)
    assert near2["code"] == "X2502" and nxt2["code"] == "X2503"

    # 3) annual_carry：近高远低 Back 为正、近低远高 Contango 为负；间隔非法为 None
    gap = month_gap_days(25, 1, 25, 4)
    c_back = annual_carry(102.0, 100.0, gap)
    c_cont = annual_carry(100.0, 102.0, gap)
    assert c_back > 0 and c_cont < 0 and annual_carry(1, 1, 0) is None

    # 4) curve_loadings：Back 时 slope>0；缺远月时 slope=None、level 三腿齐全才有
    lv, sl, cv = curve_loadings(102.0, 101.0, 100.0)
    assert sl > 0 and lv is not None and cv is not None
    lv2, sl2, cv2 = curve_loadings(102.0, 101.0, None)
    assert sl2 is None and lv2 is None

    # 5) moving_mean / basis_change 暖机与差分
    assert moving_mean([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    assert basis_change([1.0, 2.0, 4.0, 7.0], 2) == [None, None, 3.0, 5.0]

    # 6) build_term_series：构造两个相邻月份合约，验证换月时近月滚动、carry 符号与 OI 汇总
    #   X2501 在 1月上半月存续、X2502/X2503 全程；近月在 X2501 摘牌后滚到 X2502
    bars = {
        "X2501": [{"d": "2024-12-02", "c": 100.0, "s": 100.0, "v": 5, "p": 100},
                  {"d": "2024-12-03", "c": 100.0, "s": 100.0, "v": 5, "p": 90}],
        "X2502": [{"d": "2024-12-02", "c": 99.0, "s": 99.0, "v": 5, "p": 80},
                  {"d": "2024-12-03", "c": 99.0, "s": 99.0, "v": 5, "p": 70}],
        "X2503": [{"d": "2024-12-02", "c": 98.0, "s": 98.0, "v": 5, "p": 60},
                  {"d": "2024-12-03", "c": 98.0, "s": 98.0, "v": 5, "p": 50}],
    }
    ser = build_term_series(bars, roll_buffer_days=0, min_oi=1)
    assert [r["date"] for r in ser] == ["2024-12-02", "2024-12-03"]
    r0 = ser[0]
    assert r0["near"] == "X2501" and r0["next"] == "X2502" and r0["far"] == "X2503"
    assert r0["oi_sum"] == 240 and r0["oi_near"] == 100
    # G23续：合约级成交量汇总（vol_sum=当日存续合约总成交量，near_vol=近月合约成交量）
    assert r0["vol_sum"] == 15 and r0["near_vol"] == 5
    assert r0["carry_far"] is not None and r0["carry_far"] > 0  # 近100>远98 => Back 正carry
    assert r0["slope"] > 0

    # 7) 空输入安全
    assert build_term_series({}) == []

    # 8) near_roll_nav：同一近月内吃结算价上涨（roll 保留），换月当天不跨合约计盈亏
    ts2 = [
        {"near": "A", "near_s": 100.0}, {"near": "A", "near_s": 102.0},
        {"near": "A", "near_s": 104.04},
        {"near": "B", "near_s": 80.0},    # 换月：价格跳到80，但不跨合约计收益，nav 延续
        {"near": "B", "near_s": 80.8},
    ]
    nav = near_roll_nav(ts2)
    assert abs(nav[2] - 1.0404) < 1e-12     # A 段 100->102->104.04 复利
    assert abs(nav[3] - 1.0404) < 1e-12     # 换月日 nav 不跳
    assert abs(nav[4] - 1.0404 * 1.01) < 1e-12
    assert near_roll_nav([{"near": None, "near_s": None}]) == [None]
    # 9) 第77轮 top-up：max_bar_date 与补K线决策（纯函数：new/stale/退市不补）
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="th_t_")
    tstore = TermHistoryStore(os.path.join(tmpdir, "th.db"))
    bars9 = [{"d": "2026-08-28", "c": 100.0, "s": 100.0, "v": 5, "p": 50},
             {"d": "2026-09-01", "c": 101.0, "s": 101.0, "v": 6, "p": 55}]
    tstore.save_contract("RB", "RB2609", bars9)
    assert tstore.max_bar_date("RB2609") == "2026-09-01"
    assert tstore.max_bar_date("RB9999") is None
    today9 = date(2026, 9, 5)
    plan = topup_decide(today9, [
        ("RB", "RB2610", 26, 10, None),            # 无缓存 → new
        ("RB", "RB2609", 26, 9, "2026-09-01"),     # 挂牌中、末根新鲜 → 不补
        ("RB", "RB2609b", 26, 9, "2026-08-20"),    # 挂牌中、末根落后>10天 → stale
        ("RB", "RB2601", 26, 1, "2025-12-30"),     # 已退市 → 不补
    ], stale_days=10)
    assert plan == {"RB2610": "new", "RB2609b": "stale"}, plan
    tstore.close()
    # 10) 第80轮 近月比例复权 OHLC：换月拼接连续（换月日收益≈0）+ ret126/hv60 现算
    tstore2 = TermHistoryStore(os.path.join(tmpdir, "th2.db"))
    def _bars(code_price, d0, d1):
        out = []
        for d in range(d0, d1 + 1):
            dt = date(2026, 1, 1) + timedelta(days=d)
            c = code_price + d * 0.5
            out.append({"d": dt.isoformat(), "c": c, "s": c, "v": 5, "p": 50,
                        "h": c * 1.01, "l": c * 0.99, "o": c})
        return out
    tstore2.save_contract("XX", "XX2603", _bars(100.0, 0, 44))    # 前段近月（45天）
    tstore2.save_contract("XX", "XX2604", _bars(200.0, 30, 74))   # 中段（价格跳高，拼接应连续）
    tstore2.save_contract("XX", "XX2605", _bars(300.0, 60, 119))  # 后段（保证任一日都有非缓冲近月）
    rows10 = adjusted_near_ohlc("XX", tstore2, warmup=5)
    tstore2.close()
    assert len(rows10) >= 100 and rows10[0]["date"] < rows10[-1]["date"]
    closes10 = [r["c"] for r in rows10]
    rets = [closes10[i] / closes10[i - 1] - 1.0 for i in range(1, len(closes10))]
    assert max(abs(r) for r in rets) < 0.02, max(abs(r) for r in rets)   # 换月拼接后无跳空
    assert any(r["ret126"] is not None for r in rows10) and any(r["hv60"] is not None for r in rows10)
    print("term_history selftest ALL PASS（月份代码/曲线选择换月缓冲/年化carry/NS载荷/"
          "均值差分/期限序列重建与OI汇总/空输入/近月连续净值/top-up决策与max_bar_date/"
          "近月比例复权OHLC 共10组）")
    return 0


if __name__ == "__main__":
    import argparse as _ap
    _aparser = _ap.ArgumentParser(description="term_history 期限结构缓存（缺省=自检）")
    _aparser.add_argument("--topup", action="store_true",
                          help="第77轮：增量补K线（近月挂牌合约无缓存下载/末根落后重拉合并）")
    _aparser.add_argument("--codes", default="", help="逗号分隔中文名/主连，缺省=全品种")
    _aparser.add_argument("--months-back", type=int, default=6)
    _aparser.add_argument("--stale-days", type=int, default=10)
    _aparser.add_argument("--workers", type=int, default=6)
    _aargs = _aparser.parse_args()
    if _aargs.topup:
        import backtest
        _items = backtest.resolve_codes(_aargs.codes, None)
        _store = TermHistoryStore(TERM_DB_PATH)
        try:
            _stats = topup_varieties(_items, _store, months_back=_aargs.months_back,
                                     stale_days=_aargs.stale_days, workers=_aargs.workers)
        finally:
            _store.close()
        print("top-up 完成：%s" % _stats)
        raise SystemExit(0)
    raise SystemExit(_selftest())
