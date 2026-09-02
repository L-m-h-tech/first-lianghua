# -*- coding: utf-8 -*-
"""第12轮 WP-B：多到期日 IV 曲面 + 波动率微笑/skew + ATM IV 期限结构（零新增运行时依赖）。

原料：第11轮 option_chain.py 拉到的新浪商品期权 T 型链（多个到期月份，每腿含
买价/卖价/最新价/行权价/持仓量），配合项目已有的 Black-76 定价（option_analyzer.black76），
用二分法把每腿"市场价"反推为隐含波动率，再组装三层结构：

  1. 波动率微笑/skew（同一到期日、不同行权价）：ATM IV、25Δ 风险反转
     RR25 = IV(25Δ Put) - IV(25Δ Call)（正值=左偏，看跌保护更贵）、25Δ 蝶式宽度；
  2. ATM IV 期限结构（不同到期日）：近月→远月 ATM IV，近-远月差，
     近低远高=隐波 Contango、近高远低=隐波 Backwardation（近期事件推高近月）；
  3. 曲面矩阵：行=到期日、列=moneyness(K/F) 档位，值=反推 IV。

口径与纪律（与《未完成项落地方案.md》第7节一致，不夸大）：
  - 腿价格优先"买卖中间价"（bid/ask 同时为正且 ask>=bid），其次最新价 last；
    买一卖一为 0、无成交、或反推出界（深实值中间价低于内在价值）的腿一律丢弃，
    **缺档不插值、不编造**；
  - 同一行权价 call/put 都可反推时按持仓量加权合并（put-call parity 附近应一致），
    两者差超过 config.IV_PARITY_DIFF 时在点上标记 parity_warn（只提示不否决）；
  - 反推 IV 依赖挂单/成交质量，远月/深虚值流动性差，结果只作期权严格检查与
    日历价差的辅助证据，实盘以交易所/盘面隐波为准。
"""
import config
from option_analyzer import black76, greeks76
from utils import LOG


def leg_quote(leg):
    """单腿可用报价 + 质量分级，返回 (price, quality, spread_ratio) 或 None。

    quality=0 高质量：买卖双边都有且 价差/中间价 ≤ IV_MAX_SPREAD_RATIO，用中间价；
    quality=1 低质量：价差过宽（中间价不可信）时回退最新价 last，或只有单边挂单时用 last；
    宽价差且无最新价、或全无报价 -> None（不反推、不插值）。
    """
    bid = float(leg.get("bid") or 0.0)
    ask = float(leg.get("ask") or 0.0)
    last = float(leg.get("last") or 0.0)
    if bid > 0 and ask > 0 and ask + 1e-9 >= bid:
        mid = (bid + ask) / 2.0
        spr = (ask - bid) / mid if mid > 0 else 1.0
        if spr <= config.IV_MAX_SPREAD_RATIO:
            return mid, 0, spr
        if last > 0:                                          # 宽价差：中间价不可信，回退成交价
            return last, 1, spr
        return None                                           # 宽价差且无成交，丢弃
    if last > 0:
        return last, 1, None
    return None


def leg_price(leg):
    """兼容包装：只取价格（质量分级由 leg_quote 提供）。"""
    q = leg_quote(leg)
    return q[0] if q else None


def implied_vol(price, F, K, T, kind):
    """二分法反推隐含波动率（年化小数）。

    price 对 sigma 单调递增；sigma ∈ [IV_BISECT_LO, IV_BISECT_HI]，迭代 IV_BISECT_ITERS 次。
    价格<=0、超出上下界定价能力（如中间价低于内在价值）时返回 None，由调用方丢弃该腿。
    """
    if price is None or price <= 0 or F <= 0 or K <= 0 or T <= 0:
        return None
    lo, hi = config.IV_BISECT_LO, config.IV_BISECT_HI
    if black76(F, K, T, lo, kind) >= price - 1e-10:
        return None   # IV 已到下界仍定价偏高（多为深实值脏价格），不反推
    if black76(F, K, T, hi, kind) <= price + 1e-10:
        return None   # IV 到上界仍不够，价格异常
    for _ in range(config.IV_BISECT_ITERS):
        mid = (lo + hi) / 2.0
        if black76(F, K, T, mid, kind) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2.0


def _strike_iv_map(legs, F, T, kind):
    """{strike: {"iv","oi","quality","spread"}}；只保留可反推且 IV 未超上限的腿。"""
    out = {}
    for leg in legs:
        q = leg_quote(leg)
        if q is None:
            continue
        px, quality, spr = q
        K = float(leg["strike"])
        iv = implied_vol(px, F, K, T, kind)
        if iv is None or iv > config.IV_SURFACE_IV_CAP:      # 错价/陈旧价反推上天 -> 丢弃
            continue
        out[K] = {"iv": iv, "oi": float(leg.get("oi") or 0.0),
                  "quality": quality, "spread": spr}
    return out


def _pick_delta_point(points, target, side):
    """在微笑点中找 |delta| 最接近 target 的虚值腿；优先高质量(窄价差)腿，再按 delta 接近度。"""
    cand = []
    for p in points:
        if p.get("outlier"):
            continue
        d = p["call_delta"] if side == "call" else abs(p["put_delta"])
        if d <= 0.45:                                   # 只在虚值段找（ATM≈0.5 之外）
            cand.append((p["quality"], abs(d - target), p))
    if not cand:
        return None
    cand.sort(key=lambda x: (x[0], x[1]))
    return cand[0][2]


def _merge_strike(c, p):
    """合并同一行权价的 call/put 反推结果 -> (iv, quality, parity_warn)。
    两侧一致(差≤IV_PARITY_DIFF)按持仓量加权；偏差过大说明至少一侧脏，选报价质量更好的一侧，
    同质量选持仓量大的一侧（不把脏值平均进去）。"""
    if c and p:
        if abs(c["iv"] - p["iv"]) <= config.IV_PARITY_DIFF:
            ws = [(c["iv"], c["oi"]), (p["iv"], p["oi"])]
            wsum = sum(w for _, w in ws) or 1.0
            iv = sum(v * (w if w > 0 else 1.0) for v, w in ws) / \
                sum((w if w > 0 else 1.0) for _, w in ws)
            return iv, min(c["quality"], p["quality"]), False
        best = sorted((c, p), key=lambda x: (x["quality"], -x["oi"]))[0]
        return best["iv"], best["quality"], True
    one = c or p
    return one["iv"], one["quality"], False


def expiry_smile(chain, F, days):
    """单个到期日的波动率微笑。有效行权价/高质量腿不足返回 None（宁缺毋滥）。

    返回 {"label","yy","mm","days","T","points","atm_strike","atm_iv",
          "c25_iv","p25_iv","rr25","fly25","n_points","n_clean"}
    """
    if not chain or F <= 0:
        return None
    T = max(int(days), 1) / 365.0
    calls = _strike_iv_map(chain.get("calls") or [], F, T, "call")
    puts = _strike_iv_map(chain.get("puts") or [], F, T, "put")
    strikes = sorted(set(calls) | set(puts))
    points = []
    for K in strikes:
        c, p = calls.get(K), puts.get(K)
        iv_avg, quality, parity_warn = _merge_strike(c, p)
        gc = greeks76(F, K, T, iv_avg, "call")
        gp = greeks76(F, K, T, iv_avg, "put")
        points.append({"strike": K, "moneyness": K / F, "iv": iv_avg, "quality": quality,
                       "call_iv": c["iv"] if c else None, "put_iv": p["iv"] if p else None,
                       "call_delta": gc["delta"], "put_delta": gp["delta"],
                       "oi": (c["oi"] if c else 0) + (p["oi"] if p else 0),
                       "parity_warn": parity_warn})
    if len(points) < config.IV_SURFACE_MIN_STRIKES:
        return None
    clean = [p for p in points if p["quality"] == 0]
    if len(clean) < 3:                                   # 连ATM+两翼的高质量腿都凑不齐，不可信
        return None
    atm_strike = chain.get("atm_strike")
    atm_pool = clean if config.IV_SURFACE_NEED_CLEAN_ATM else points
    atm_pt = min(atm_pool, key=lambda p: abs(p["strike"] - (atm_strike or F)))
    # 相对 ATM 的离群点（深虚值权利金极小、微小报价误差反推出离谱IV）：标记后不参与25Δ/矩阵
    lo_mul, hi_mul = config.IV_OUTLIER_RATIO
    for p in points:
        p["outlier"] = not (atm_pt["iv"] * lo_mul <= p["iv"] <= atm_pt["iv"] * hi_mul)
    c25 = _pick_delta_point(points, config.IV_RR25_TARGET, "call")
    p25 = _pick_delta_point(points, config.IV_RR25_TARGET, "put")
    # RR/蝶式取单侧腿自己的反推IV（call侧用call_iv、put侧用put_iv），单侧缺失才回退合并IV
    c25_iv = (c25["call_iv"] if c25 and c25["call_iv"] is not None
              else c25["iv"] if c25 else None)
    p25_iv = (p25["put_iv"] if p25 and p25["put_iv"] is not None
              else p25["iv"] if p25 else None)
    rr25 = (p25_iv - c25_iv) if (c25_iv is not None and p25_iv is not None) else None
    fly25 = ((p25_iv + c25_iv) / 2.0 - atm_pt["iv"]) if rr25 is not None else None
    return {"label": chain.get("label"), "yy": int(chain.get("yy") or 0),
            "mm": int(chain.get("mm") or 0), "days": int(days), "T": T,
            "points": points, "atm_strike": atm_pt["strike"], "atm_iv": atm_pt["iv"],
            "c25_iv": c25_iv, "p25_iv": p25_iv, "rr25": rr25, "fly25": fly25,
            "n_points": len(points), "n_clean": len(clean)}


def _matrix_row(exp, F):
    """按 moneyness 档位取最近点 IV（偏差>0.03 不硬凑）；同档优先高质量腿，缺档 None。"""
    row = []
    for m in config.IV_MONEYNESS_GRID:
        cand = []
        for p in exp["points"]:
            if p.get("outlier"):
                continue
            gap = abs(p["moneyness"] - m)
            if gap <= 0.03:
                cand.append((p["quality"], gap, p))
        row.append(min(cand)[2]["iv"] if cand else None)
    return row


def _fmt_iv(v):
    return "--" if v is None else f"{v*100:.0f}%"


def build_surface(sym, ex, F, chains, days_map, main_label=None):
    """组装多到期日 IV 曲面。

    chains: {label: chain_dict}（option_chain.build_summary 产出，可跨多个月份）
    days_map: {label: 剩余天数}（优先 OpenVlab 真实到期日）
    main_label: 期权单腿/组合分析使用的主力月份 label，用于标记主 ATM IV。
    任何月份反推失败都跳过；没有任何一个有效到期日时返回 None（调用方降级，不阻断）。
    """
    if not chains or F <= 0:
        return None
    exps = []
    for label, ch in chains.items():
        days = days_map.get(label)
        if days is None or days < config.IV_SURFACE_MIN_DAYS:
            continue
        try:
            sm = expiry_smile(ch, F, days)
        except Exception as e:
            LOG.debug("IV微笑反推失败 %s %s: %s", sym, label, e)
            sm = None
        if sm:
            exps.append(sm)
    if not exps:
        return None
    exps.sort(key=lambda e: (e["yy"], e["mm"]))
    atm_by_label = {e["label"]: e["atm_iv"] for e in exps}
    near, far = exps[0], exps[-1]
    term_diff = near["atm_iv"] - far["atm_iv"] if len(exps) >= 2 else 0.0
    if len(exps) < 2:
        term_shape = "单一到期日"
    elif term_diff > config.IV_CALENDAR_MIN_DIFF:
        term_shape = "隐波近高远低(Backwardation,近期事件推高近月)"
    elif term_diff < -config.IV_CALENDAR_MIN_DIFF:
        term_shape = "隐波近低远高(Contango,远月更贵)"
    else:
        term_shape = "隐波期限结构平坦"
    matrix_rows = [(e["label"], _matrix_row(e, F)) for e in exps]
    grid = [config.IV_MONEYNESS_GRID] + [r for _, r in matrix_rows]
    main_atm = atm_by_label.get(main_label) if main_label else near["atm_iv"]
    summary_line, matrix_line = surface_text(exps, term_diff, term_shape, matrix_rows)
    return {"sym": sym, "ex": ex, "expiries": exps, "atm_by_label": atm_by_label,
            "near_label": near["label"], "far_label": far["label"],
            "term_diff": term_diff, "term_shape": term_shape,
            "main_label": main_label, "main_atm_iv": main_atm,
            "grid": grid, "summary_line": summary_line, "matrix_line": matrix_line}


def surface_text(exps, term_diff, term_shape, matrix_rows):
    """生成两行紧凑报告文本：ATM期限+skew 摘要行、曲面矩阵行。
    matrix_rows: [(label, [iv按moneyness档位])]"""
    chain_txt = "→".join(f"{e['label']} {e['atm_iv']*100:.1f}%" for e in exps)
    bits = [f"ATM {chain_txt}"]
    if len(exps) >= 2:
        bits.append(f"{term_shape}，近-远{term_diff*100:+.1f}vol")
    e0 = exps[0]
    if e0["rr25"] is not None:
        skew_dir = "看跌保护偏贵(左偏)" if e0["rr25"] > 0 else "看涨更贵(右偏)"
        bits.append(f"{e0['label']} 25Δ风险反转{e0['rr25']*100:+.1f}vol({skew_dir})"
                    f"、蝶式{e0['fly25']*100:+.1f}vol")
    n_bad = sum(1 for e in exps for p in e["points"] if p["parity_warn"])
    if n_bad:
        bits.append(f"{n_bad}档call/put反推偏差>{config.IV_PARITY_DIFF*100:.0f}vol(已取窄价差可信侧)")
    summary = "IV曲面(T链反推,%d个月份): " % len(exps) + "；".join(bits)
    # 矩阵：列头=K/F 档位
    head = "曲面矩阵(列=K/F " + "/".join(f"{m:.2f}" for m in config.IV_MONEYNESS_GRID) + "): "
    lines = [head]
    for label, row in matrix_rows:
        lines.append(f"{label}: " + "/".join(_fmt_iv(v) for v in row))
    matrix = "｜".join(lines)
    return summary, matrix
