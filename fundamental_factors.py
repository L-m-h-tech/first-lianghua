# -*- coding: utf-8 -*-
"""第13轮 WP-C：基本面因子计算（纯函数、零网络、零第三方依赖，便于合成断言）。

四个子因子，方向均遵循"现货/主力偏紧或偏多 -> 正分"：
  1) 库存/仓单 inventory_factor：库存历史分位低 + 周环比去化 -> 偏多；高库存+累库 -> 偏空。
  2) 龙虎榜 rank_factor：成交持仓前20席会员净多率=(多-空)/(多+空)为正、且较昨日回升 -> 偏多。
  3) 期限carry carry_factor：近月相对远月年化升水(Back/反向市场, annual_carry>0)=现货紧 -> 偏多。
  4) 基差 basis_factor：现货相对期货主力升水(基差率>0)=现货坚挺 -> 偏多。
任一子因子数据缺失即跳过，并按"可得子项权重重新归一化"，缺数据不编造、不让单因子失真放大。
"""
import math

import config


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, float(x)))


def _tanh(x, k):
    try:
        return math.tanh(float(x) * float(k))
    except (TypeError, ValueError):
        return 0.0


def _pct(values, current):
    """current 在升序 values 中的经验分位（0~1）；样本不足返回 None。"""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    below = sum(1 for v in vals if v <= current)
    return below / len(vals)


def inventory_factor(series):
    """库存/仓单时序因子。

    series: 按日期升序的 [{"date":str,"stock":float,"chg":float}, ...]（stock=注册仓单/库存绝对量）。
    返回 (score∈[-1,1], detail) 或 None（样本不足/无有效值）。
    """
    pts = [p for p in (series or []) if p.get("stock") is not None and p["stock"] > 0]
    if len(pts) < config.FUND_INV_MIN_SAMPLES:
        return None
    stocks = [p["stock"] for p in pts]
    cur = stocks[-1]
    pct = _pct(stocks, cur)
    # 周环比：当前 vs FUND_INV_WOW_DAYS 个交易日前
    if len(stocks) > config.FUND_INV_WOW_DAYS and stocks[-1 - config.FUND_INV_WOW_DAYS] > 0:
        wow = cur / stocks[-1 - config.FUND_INV_WOW_DAYS] - 1.0
    else:
        wow = None
    level = (0.5 - pct) * 2.0                      # 低分位偏多、高分位偏空
    flow = -_tanh(wow, 1.0 / config.FUND_INV_WOW_K) if wow is not None else 0.0
    flow_w = 0.4 if wow is not None else 0.0
    score = 0.6 * level + flow_w * flow
    score = _clamp(score / (0.6 + flow_w))         # 缺周环比时按可得项归一
    detail = {"current": cur, "pct": pct, "wow": wow, "n": len(pts),
              "first_date": pts[0].get("date", ""), "last_date": pts[-1].get("date", "")}
    return score, detail


def rank_factor(long_oi, short_oi, prev_long=None, prev_short=None):
    """成交持仓龙虎榜（前20席会员）因子。

    long_oi/short_oi: 今日前20席多头/空头合计持仓；prev_*: 昨日合计（用于边际变化）。
    返回 (score∈[-1,1], detail) 或 None（多空合计都为0/缺失）。
    """
    L, S = float(long_oi or 0), float(short_oi or 0)
    if L + S <= 0:
        return None
    net = (L - S) / (L + S)                        # 净多率，正=前20席净多
    pL, pS = float(prev_long or 0), float(prev_short or 0)
    delta = None
    if pL + pS > 0:
        prev_net = (pL - pS) / (pL + pS)
        delta = net - prev_net
    level = _tanh(net, config.FUND_RANK_NET_K)
    if delta is None:
        score = level
    else:
        # 60%看净多率水平、40%看较昨日的边际改善
        score = 0.6 * level + 0.4 * _tanh(delta, config.FUND_RANK_DELTA_K)
    detail = {"long": L, "short": S, "net": net, "delta": delta}
    return _clamp(score), detail


def carry_factor(term):
    """期限结构 carry 因子（复用第11轮 contracts.term_structure 的 annual_carry，零新增请求）。

    term: term_structure() 返回 dict（含 annual_carry），或直接给年化展期收益率数值。
    返回 (score, detail) 或 None。
    """
    if term is None:
        return None
    ac = term.get("annual_carry") if isinstance(term, dict) else term
    if ac is None:
        return None
    shape = term.get("shape", "") if isinstance(term, dict) else ""
    return _tanh(ac, 1.0 / config.FUND_CARRY_K), {"annual_carry": ac, "shape": shape}


def basis_factor(basis_rate):
    """基差因子。basis_rate=(现货-期货主力)/期货主力，正=现货升水（现货坚挺，偏多）。"""
    if basis_rate is None:
        return None
    return _tanh(basis_rate, 1.0 / config.FUND_BASIS_K), {"basis_rate": basis_rate}


def build_fundamental(inv=None, rank=None, carry=None, basis=None):
    """把各子因子按 config 权重加权（缺失子项的权重按可得项重新归一化），产出基本面因子包。

    各入参为 inventory_factor/rank_factor/carry_factor/basis_factor 的 (score, detail) 结果或 None。
    返回:
      None（四个子项全缺）或
      {"score":贡献分(带正负, 已乘FUND_MAX_SCORE), "parts":{子项:裸分}, "sub":明细, "note":紧凑文本}
    """
    items = [
        ("库存仓单", config.FUND_INV_WEIGHT, inv),
        ("龙虎榜", config.FUND_RANK_WEIGHT, rank),
        ("期限carry", config.FUND_CARRY_WEIGHT, carry),
        ("基差", config.FUND_BASIS_WEIGHT, basis),
    ]
    avail = [(name, w, r) for name, w, r in items if r is not None]
    if not avail:
        return None
    wsum = sum(w for _, w, _ in avail)
    raw = sum(w * _clamp(r[0]) for _, w, r in avail) / wsum   # [-1,1]
    parts = {name: round(_clamp(r[0]), 2) for name, _, r in avail}
    sub = {name: r[1] for name, _, r in avail}
    score = _clamp(raw) * config.FUND_MAX_SCORE

    seg = []
    inv_d = sub.get("库存仓单")
    if inv_d:
        if inv_d["wow"] is None:
            wow_txt = "无"
        else:
            wv = inv_d["wow"] * 100
            # 注册仓单注销后重新注册会造成极低基数，周环比可达数十倍；展示封顶避免误导（因子已tanh饱和）
            if wv > 200:
                wow_txt = "≥+200%(低基数)"
            elif wv < -200:
                wow_txt = "≤-200%(低基数)"
            else:
                wow_txt = f"{wv:+.1f}%"
        seg.append(f"库存{inv_d['current']:g}/分位{inv_d['pct']:.0%}/周环比{wow_txt}")
    rk = sub.get("龙虎榜")
    if rk:
        seg.append("前20席净多率{:.1%}{}".format(
            rk["net"], "" if rk["delta"] is None else f"(较昨日{rk['delta']*100:+.1f}pct)"))
    cy = sub.get("期限carry")
    if cy:
        seg.append(f"年化carry {cy['annual_carry']*100:+.1f}%")
    bs = sub.get("基差")
    if bs:
        seg.append(f"基差率{bs['basis_rate']*100:+.1f}%")
    tone = "偏多" if raw > 0.12 else ("偏空" if raw < -0.12 else "中性")
    note = f"基本面{tone}(综合{score:+.2f})：" + "；".join(seg)
    return {"score": score, "raw": raw, "parts": parts, "sub": sub, "note": note}
