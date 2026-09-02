# -*- coding: utf-8 -*-
"""期权组合策略推荐器（只推荐期权/期权+标的组合，不含纯期货投机）。

策略目录：
1. 单腿买入：强方向 + 隐波不贵；
2. 牛市看涨价差/熊市看跌价差：方向明确但隐波偏贵；
3. 买入跨式：消息面与技术面冲突，预期双向大波动；
4. 铁鹰式：中性盘整 + 隐波偏贵，限定风险卖方；
5. 买入蝶式：中性窄幅震荡，借方限定风险；
6. 比率价差1:2：温和方向 + 隐波偏高，含一条裸露尾部腿；
7. 备兑看涨：持有/买入期货并卖出虚值看涨，增强收益；
8. 保护性认沽：持有/买入期货并买入看跌做保险。

权利金用 Black-76 与估计隐波计算；保证金只输出保守“点值”估算，实盘以交易所和期货公司为准。
"""
import math

import config
import contracts as contracts_mod
from option_analyzer import black76, greeks76, implied_vol_profile, iv_pct_text
from utils import fmt_px


def _round_k(x, step):
    return round(x / step) * step


def _leg(F, T, iv, kind, K, buy, qty=1):
    prem = black76(F, K, T, iv, kind=kind)
    g = greeks76(F, K, T, iv, kind=kind)
    s = 1 if buy else -1
    return {"buy": buy, "kind": kind, "K": K, "prem": prem, "qty": qty,
            "delta": g["delta"] * s * qty, "gamma": g["gamma"] * s * qty,
            "vega": g["vega"] * s * qty, "theta": g["theta"] * s * qty}


def _future_leg(F, buy=True, qty=1):
    s = 1 if buy else -1
    return {"buy": buy, "kind": "future", "K": F, "prem": 0.0, "qty": qty,
            "delta": s * qty, "gamma": 0.0, "vega": 0.0, "theta": 0.0}


def _legs_summary(legs, sym, ex, yy, mm):
    parts = []
    for lg in legs:
        qty_txt = f"{lg.get('qty', 1)}张" if lg.get("qty", 1) != 1 else ""
        op = "买" if lg["buy"] else "卖"
        if lg["kind"] == "future":
            parts.append(f"{op}{qty_txt}标的期货K≈{lg['K']:g}")
            continue
        kind = "看涨" if lg["kind"] == "call" else "看跌"
        code = contracts_mod.option_code_hint(sym, ex, yy, mm, lg["K"], lg["kind"])
        parts.append(f"{op}{qty_txt}{kind}K={lg['K']:g}(约{lg['prem']:.1f}点,{code})")
    return " + ".join(parts)


def _calendar_legs_text(sym, ex, legs):
    """日历价差跨到期月份的腿文本（每条腿带自己的月份与代码示意）。"""
    parts = []
    for lg in legs:
        op = "买" if lg["buy"] else "卖"
        kind = "看涨" if lg["kind"] == "call" else "看跌"
        yy, mm = lg.get("yy", 0), lg.get("mm", 0)
        label = lg.get("label", "")
        code = (contracts_mod.option_code_hint(sym, ex, yy, mm, lg["K"], lg["kind"])
                if yy else "")
        parts.append(f"{op}{label}月{kind}K={lg['K']:g}(约{lg['prem']:.1f}点,{code})")
    return " + ".join(parts)


def _inst_check(inst, direction):
    """机构观点配合度检查（交易可查AI研报）"""
    if not inst or inst.get("total", 0) < 3:
        return True, "机构观点样本不足(<3家)，不作为否决项"
    ratio = (inst["bullish"] - inst["bearish"]) / inst["total"]
    if direction == 0:
        ok = abs(ratio) < 0.6
        return ok, f"机构看多{inst['bullish']}/看空{inst['bearish']}（中性策略要求分歧不大）"
    ok = ratio * direction >= -0.2
    tip = "与策略同向" if ratio * direction > 0.2 else ("观点中性" if abs(ratio) <= 0.2 else "轻度相反")
    return ok, f"机构看多{inst['bullish']}/看空{inst['bearish']}，{tip}" + (
        "" if ok else "（强烈相反时否决）")


def _naked_option_margin_points(F, leg):
    """单张裸期权卖方保证金的保守点值估算（不含合约乘数，不替代交易所公式）。"""
    qty = abs(int(leg.get("qty", 1)))
    K, prem = leg["K"], leg["prem"]
    base = F if leg["kind"] == "call" else K
    otm = max(K - F, 0.0) if leg["kind"] == "call" else max(F - K, 0.0)
    main = max(base * config.OPT_SELLER_MARGIN_RATE - otm,
               base * config.OPT_SELLER_MARGIN_MIN_RATE)
    return qty * (prem + main)


def _futures_margin_points(legs):
    return sum(abs(lg.get("qty", 1)) * lg["K"] * config.OPT_FUTURES_MARGIN_RATE
               for lg in legs if lg.get("kind") == "future")


def _mk_candidate(name, direction, legs, F, sigma_move, checks_extra, priority):
    """汇总腿的盈亏结构与Greeks（腿可带qty）。"""
    net = sum((-1 if lg["buy"] else 1) * lg["prem"] * lg.get("qty", 1) for lg in legs)
    debit = -net
    return {"name": name, "direction": direction, "legs": legs, "net": net,
            "debit": debit,
            "delta": sum(lg["delta"] for lg in legs),
            "gamma": sum(lg["gamma"] for lg in legs),
            "vega": sum(lg["vega"] for lg in legs),
            "theta_day": sum(lg["theta"] for lg in legs) / 365.0,
            "sigma_move": sigma_move, "checks_extra": checks_extra,
            "priority": priority, "margin_points": 0.0,
            "margin_note": "买方结构无期权保证金，最大亏损=净支出，建议支出≤账户5%"}


def recommend(name, fut_row):
    """对单个期权品种给出组合策略推荐。"""
    F = fut_row["price"]
    if F <= 0:
        return None
    volp = implied_vol_profile(fut_row)
    hv20 = volp["hv20"]
    iv = volp["iv"]
    iv_ratio = iv / volp["hv_ref"] if volp["hv_ref"] > 0 else 9.9
    iv_src = volp["iv_src"]
    iv_pct = volp.get("iv_pct")
    page = fut_row.get("page") or {}
    prem_info = (page.get("prem") or {}).get(name) or {}
    rank_list = ((page.get("rank") or {}).get(name) or {}).get("list")
    if prem_info.get("prem") is not None:
        premium_flag = 1 if prem_info["prem"] > 0 else -1
    elif rank_list == "隐波最大上升":
        premium_flag = 1
    elif rank_list == "隐波最大下降":
        premium_flag = -1
    else:
        premium_flag = 0
    if premium_flag > 0:
        buy_cap, spread_cap, seller_floor = 1.20, 1.45, 1.05
    elif premium_flag < 0:
        buy_cap, spread_cap, seller_floor = 1.50, 1.80, 1.25
    else:
        buy_cap, spread_cap, seller_floor = 1.35, 1.60, 1.15
    skew_txt = "" if volp.get("skew") is None else f"，偏度{volp['skew']:+.2f}"
    pcr_txt = "" if volp.get("pcr") is None else f"，PCR={volp['pcr']:.2f}"
    prem_note = f"；{iv_pct_text(volp)}；{volp['cone_note']}{skew_txt}{pcr_txt}"
    if prem_info.get("prem") is not None:
        prem_note += (f"；溢价榜: IV{prem_info['iv']:.1f}%/HV{prem_info['hv']:.1f}%"
                      f" 溢价{prem_info['prem']:+.1f}")
    elif rank_list:
        prem_note += f"；OpenVlab{rank_list}榜在列"
    score = fut_row["score"]
    om = fut_row.get("opt_month") or {}
    yy, mm = om.get("yy", 0), om.get("mm", 0)
    days = om.get("opt_days") or config.OPT_ASSUMED_DAYS
    T = max(days, 5) / 365.0
    step = config.strike_step(F)
    inst = fut_row.get("inst") or {}
    conflict = any("方向不一致" in r for r in fut_row.get("risks", []))
    month_label = f"{yy:02d}{mm:02d}" if yy else "主力月"
    sigma_move = F * hv20 * math.sqrt(T)
    strength = 0.8 + min(abs(score), 10.0) / 10.0 * 0.7
    expected = sigma_move * strength
    K0 = _round_k(F, step)
    cands = []

    # ---- 候选1：方向价差 ----
    if score >= 2 or score <= -2:
        kind = "call" if score > 0 else "put"
        sgn = 1 if score > 0 else -1
        k_long = K0 if abs(score) < 6.5 else _round_k(F + sgn * step, step)
        k_short = _round_k(F + sgn * max(0.6 * sigma_move, step), step)
        if (k_short - k_long) * sgn <= 0:
            k_short = k_long + sgn * step
        legs = [_leg(F, T, iv, kind, k_long, True),
                _leg(F, T, iv, kind, k_short, False)]
        debit = legs[0]["prem"] - legs[1]["prem"]
        if debit <= 0:
            debit = step * 0.01
        width = abs(k_short - k_long)
        be = (k_long + debit) if kind == "call" else (k_long - debit)
        need = abs(be - F)
        max_profit = max(0.0, width - debit)
        checks = [
            ("方向信号", True, f"综合分{score:+.1f}（价差要求|分|≥2）"),
            ("隐波状态", iv_ratio <= spread_cap and (iv_pct is None or iv_pct <= config.OPT_IV_PCT_SPREAD_MAX),
             f"{iv_src}IV/HV={iv_ratio:.2f}，价差上限{spread_cap:.2f}/分位上限{config.OPT_IV_PCT_SPREAD_MAX:.0%}{prem_note}"),
            ("幅度覆盖", need > 0 and expected >= 1.3 * need,
             f"预期波动≈{expected:.1f}点 vs 盈亏平衡需{need:.1f}点"
             f"（覆盖{expected/need if need > 0 else 0:.1f}倍，要求≥1.3）"),
            ("剩余到期", days >= 14, f"{month_label}月份估算剩余≈{days}天（≥14天）"),
        ]
        iok, inode = _inst_check(inst, sgn)
        checks.insert(1, ("机构观点配合", iok, inode))
        cand = _mk_candidate("牛市看涨价差" if kind == "call" else "熊市看跌价差",
                             sgn, legs, F, sigma_move, checks, 10)
        cand["max_profit"], cand["max_loss"], cand["be"] = max_profit, debit, be
        cands.append(cand)

    # ---- 候选2：单腿买入 ----
    if abs(score) >= config.OPT_SCORE_MIN:
        kind = "call" if score > 0 else "put"
        legs = [_leg(F, T, iv, kind, K0, True)]
        prem = legs[0]["prem"]
        be = (K0 + prem) if kind == "call" else (K0 - prem)
        need = abs(be - F)
        checks = [
            ("方向信号", True, f"综合分{score:+.1f}（单腿要求|分|≥{config.OPT_SCORE_MIN}）"),
            ("隐波状态", iv_ratio <= buy_cap and (iv_pct is None or iv_pct <= config.OPT_IV_PCT_BUY_MAX),
             f"{iv_src}IV/HV={iv_ratio:.2f}，裸买上限{buy_cap:.2f}/分位上限{config.OPT_IV_PCT_BUY_MAX:.0%}{prem_note}"),
            ("幅度覆盖", need > 0 and expected >= 1.5 * need,
             f"预期波动≈{expected:.1f}点 vs 盈亏平衡需{need:.1f}点"
             f"（覆盖{expected/need if need > 0 else 0:.1f}倍，要求≥1.5）"),
            ("剩余到期", days >= 14, f"{month_label}月份估算剩余≈{days}天（≥14天）"),
        ]
        iok, inode = _inst_check(inst, 1 if score > 0 else -1)
        checks.insert(1, ("机构观点配合", iok, inode))
        cand = _mk_candidate("单腿买入" + ("看涨" if kind == "call" else "看跌"),
                             1 if score > 0 else -1, legs, F, sigma_move, checks, 5)
        cand["max_profit"], cand["max_loss"], cand["be"] = None, prem, be
        cands.append(cand)

    # ---- 候选3：买入跨式 ----
    if conflict and abs(score) >= 3:
        legs = [_leg(F, T, iv, "call", K0, True), _leg(F, T, iv, "put", K0, True)]
        cost = legs[0]["prem"] + legs[1]["prem"]
        checks = [
            ("方向分歧", True, "消息面与技术面方向冲突，存在双向大幅波动可能"),
            ("隐波状态", iv_ratio <= min(1.2, buy_cap) and (iv_pct is None or iv_pct <= config.OPT_IV_PCT_BUY_MAX),
             f"{iv_src}IV/HV={iv_ratio:.2f}，跨式上限{min(1.2, buy_cap):.2f}/分位上限{config.OPT_IV_PCT_BUY_MAX:.0%}{prem_note}"),
            ("幅度覆盖", sigma_move >= 1.3 * cost,
             f"1倍标准差波动≈{sigma_move:.1f}点 vs 跨式成本≈{cost:.1f}点"
             f"（覆盖{sigma_move/cost if cost > 0 else 0:.1f}倍，要求≥1.3）"),
            ("剩余到期", days >= 14, f"{month_label}月份估算剩余≈{days}天（≥14天）"),
        ]
        iok, inode = _inst_check(inst, 0)
        checks.insert(1, ("机构观点配合", iok, inode))
        cand = _mk_candidate("买入跨式", 0, legs, F, sigma_move, checks, 8)
        cand["max_profit"], cand["max_loss"], cand["be"] = None, cost, (K0 - cost, K0 + cost)
        cands.append(cand)

    # ---- 候选4：铁鹰式（中性卖方，限定风险） ----
    if abs(score) < 2:
        half = max(_round_k(sigma_move, step), 2 * step)
        kcs, kps = _round_k(F + half, step), _round_k(F - half, step)
        kco, kpo = kcs + 3 * step, kps - 3 * step
        legs = [_leg(F, T, iv, "call", kcs, False), _leg(F, T, iv, "put", kps, False),
                _leg(F, T, iv, "call", kco, True), _leg(F, T, iv, "put", kpo, True)]
        credit = sum((-1 if lg["buy"] else 1) * lg["prem"] for lg in legs)
        wing = (kcs + 3 * step) - kcs
        max_loss = max(0.0, wing - credit)
        checks = [
            ("中性信号", True, f"综合分{score:+.1f}（铁鹰要求|分|<2，预期区间盘整）"),
            ("隐波状态", iv_ratio >= seller_floor and (iv_pct is None or iv_pct >= config.OPT_IV_PCT_SELL_FLOOR),
             f"{iv_src}IV/HV={iv_ratio:.2f}，卖方法下限{seller_floor:.2f}/分位下限{config.OPT_IV_PCT_SELL_FLOOR:.0%}{prem_note}"),
            ("区间覆盖", (kcs - F) >= 1.3 * sigma_move and (F - kps) >= 1.3 * sigma_move,
             f"1倍标准差≈{sigma_move:.1f}点 vs 卖出执行价缓冲{min(kcs - F, F - kps):.1f}点（要求≥1.3倍）"),
            ("剩余到期", days >= 21, f"{month_label}月份估算剩余≈{days}天（卖方要求≥21天）"),
        ]
        iok, inode = _inst_check(inst, 0)
        checks.insert(1, ("机构观点配合", iok, inode))
        cand = _mk_candidate("铁鹰式(四腿)", 0, legs, F, sigma_move, checks, 6)
        cand["max_profit"], cand["max_loss"], cand["be"] = credit, max_loss, (kps + credit, kcs - credit)
        cand["margin_points"] = max_loss
        cand["margin_note"] = f"限定风险卖方，保守保证金≈{max_loss:.1f}点（最大亏损口径，实际以期货公司为准）"
        cands.append(cand)

    # ---- 候选5：买入蝶式（中性窄幅震荡，借方限定风险） ----
    if abs(score) < 2:
        wing = max(_round_k(0.8 * sigma_move, step), 2 * step)
        k1, k2, k3 = K0 - wing, K0, K0 + wing
        legs = [_leg(F, T, iv, "call", k1, True), _leg(F, T, iv, "call", k2, False, 2),
                _leg(F, T, iv, "call", k3, True)]
        debit = legs[0]["prem"] - 2 * legs[1]["prem"] + legs[2]["prem"]
        if debit > 0:
            max_profit = max(0.0, wing - debit)
            checks = [
                ("中性信号", True, f"综合分{score:+.1f}（蝶式要求|分|<2，预期收窄到平值附近）"),
                ("隐波状态", iv_ratio <= spread_cap and (iv_pct is None or iv_pct <= config.OPT_IV_PCT_SPREAD_MAX),
                 f"{iv_src}IV/HV={iv_ratio:.2f}，借方价差上限{spread_cap:.2f}/分位上限{config.OPT_IV_PCT_SPREAD_MAX:.0%}{prem_note}"),
                ("波动幅度", expected <= 1.2 * wing,
                 f"预期波动≈{expected:.1f}点 vs 蝶式翼宽{wing:.1f}点（要求≤1.2倍，过宽会打穿盈利区）"),
                ("剩余到期", days >= 14, f"{month_label}月份估算剩余≈{days}天（≥14天）"),
            ]
            iok, inode = _inst_check(inst, 0)
            checks.insert(1, ("机构观点配合", iok, inode))
            cand = _mk_candidate("买入蝶式(1:2:1)", 0, legs, F, sigma_move, checks, 4)
            cand["max_profit"], cand["max_loss"], cand["be"] = max_profit, debit, (k1 + debit, k3 - debit)
            cands.append(cand)

    # ---- 候选6：比率价差1:2（温和方向 + 隐波偏高，含裸腿） ----
    if 2 <= score < config.SCORE_MID or -config.SCORE_MID < score <= -2:
        sgn = 1 if score > 0 else -1
        kind = "call" if sgn > 0 else "put"
        wing = max(_round_k(0.8 * sigma_move, step), 2 * step)
        k_long = K0
        k_short = _round_k(F + sgn * wing, step)
        if (k_short - k_long) * sgn <= 0:
            k_short = k_long + sgn * step
        legs = [_leg(F, T, iv, kind, k_long, True),
                _leg(F, T, iv, kind, k_short, False, 2)]
        width = abs(k_short - k_long)
        net = sum((-1 if lg["buy"] else 1) * lg["prem"] * lg["qty"] for lg in legs)
        max_profit = width + net
        far_be = k_short + sgn * max_profit
        naked_leg = dict(legs[1], qty=1)  # 两张短腿中一张由长腿保护，剩余一张按裸腿估保证金
        margin = _naked_option_margin_points(F, naked_leg)
        checks = [
            ("方向信号", True, f"综合分{score:+.1f}（比率价差要求温和方向2~6.5）"),
            ("隐波状态", iv_ratio >= seller_floor and (iv_pct is None or iv_pct >= config.OPT_IV_PCT_SELL_FLOOR),
             f"{iv_src}IV/HV={iv_ratio:.2f}，卖方腿偏好≥{seller_floor:.2f}/分位≥{config.OPT_IV_PCT_SELL_FLOOR:.0%}{prem_note}"),
            ("幅度覆盖", 0.6 * width <= expected <= 1.8 * width,
             f"预期波动≈{expected:.1f}点 vs 短腿距离{width:.1f}点（理想落在短腿附近，过远会触发裸腿亏损）"),
            ("剩余到期", days >= 21, f"{month_label}月份估算剩余≈{days}天（卖方腿要求≥21天）"),
        ]
        iok, inode = _inst_check(inst, sgn)
        checks.insert(1, ("机构观点配合", iok, inode))
        cand = _mk_candidate(("看涨" if sgn > 0 else "看跌") + "比率价差1:2",
                             sgn, legs, F, sigma_move, checks, 3)
        cand["max_profit"], cand["max_loss"], cand["be"] = max_profit, None, far_be
        cand["margin_points"] = margin
        cand["margin_note"] = (f"1:2结构含1张裸露短腿，保守保证金≈{margin:.1f}点；"
                               "突破远端盈亏平衡后理论风险无上限，仅可小仓位")
        cands.append(cand)

    # ---- 候选7：备兑看涨（期货多头 + 卖虚值Call） ----
    if 2 <= score < config.SCORE_MID:
        k_short = _round_k(F + max(_round_k(0.8 * sigma_move, step), step), step)
        legs = [_future_leg(F, True), _leg(F, T, iv, "call", k_short, False)]
        prem = legs[1]["prem"]
        max_profit = k_short - F + prem
        checks = [
            ("温和偏多", True, f"综合分{score:+.1f}（备兑要求2≤分<6.5，强趋势不建议封顶）"),
            ("隐波状态", iv_ratio >= seller_floor and (iv_pct is None or iv_pct >= config.OPT_IV_PCT_SELL_FLOOR),
             f"{iv_src}IV/HV={iv_ratio:.2f}，卖Call偏好≥{seller_floor:.2f}/分位≥{config.OPT_IV_PCT_SELL_FLOOR:.0%}{prem_note}"),
            ("上方空间", expected <= 1.6 * (k_short - F),
             f"预期波动≈{expected:.1f}点 vs 卖Call缓冲{k_short-F:.1f}点（大涨超过执行价会放弃超额收益）"),
            ("剩余到期", days >= 14, f"{month_label}月份估算剩余≈{days}天（≥14天）"),
        ]
        iok, inode = _inst_check(inst, 1)
        checks.insert(1, ("机构观点配合", iok, inode))
        cand = _mk_candidate("备兑看涨(期货+卖Call)", 1, legs, F, sigma_move, checks, 4)
        cand["max_profit"], cand["max_loss"], cand["be"] = max_profit, F - prem, k_short - max_profit
        cand["margin_points"] = _futures_margin_points(legs)
        cand["margin_note"] = f"需持有/买入1张期货，期货保证金保守≈{cand['margin_points']:.1f}点；Call由标的备兑"
        cands.append(cand)

    # ---- 候选8：保护性认沽（期货多头 + 买Put保险） ----
    if score >= config.OPT_SCORE_MIN:
        k_put = _round_k(F - max(_round_k(0.5 * sigma_move, step), step), step)
        legs = [_future_leg(F, True), _leg(F, T, iv, "put", k_put, True)]
        prem = legs[1]["prem"]
        max_loss = F - k_put + prem
        checks = [
            ("方向信号", True, f"综合分{score:+.1f}（保护性认沽要求|分|≥{config.OPT_SCORE_MIN}）"),
            ("隐波状态", iv_ratio <= 1.80 and (iv_pct is None or iv_pct <= config.OPT_IV_PCT_SPREAD_MAX),
             f"{iv_src}IV/HV={iv_ratio:.2f}，保险腿可接受上限1.80/分位上限{config.OPT_IV_PCT_SPREAD_MAX:.0%}{prem_note}"),
            ("保险成本", expected >= 1.5 * prem,
             f"预期波动≈{expected:.1f}点 vs 保险成本≈{prem:.1f}点（要求≥1.5倍，避免保险过贵）"),
            ("剩余到期", days >= 14, f"{month_label}月份估算剩余≈{days}天（≥14天）"),
        ]
        iok, inode = _inst_check(inst, 1)
        checks.insert(1, ("机构观点配合", iok, inode))
        cand = _mk_candidate("保护性认沽(期货+买Put)", 1, legs, F, sigma_move, checks, 6)
        cand["max_profit"], cand["max_loss"], cand["be"] = None, max_loss, F + prem
        cand["margin_points"] = _futures_margin_points(legs)
        cand["margin_note"] = f"需持有/买入1张期货，期货保证金保守≈{cand['margin_points']:.1f}点；买Put无期权保证金"
        cands.append(cand)

    # ---- 候选9：日历价差（同行权价跨到期月，第12轮 WP-B，依赖 iv_surface） ----
    # 近月IV显著高于远月 -> 卖近买远(long calendar,净支出,做多远月Vega+赚近月快Theta)；
    # 远月IV显著高于近月 -> 买近卖远(short calendar,净收入,近月到期后远月裸露)。
    surf = fut_row.get("iv_surface") or {}
    exps = surf.get("expiries") or []
    if len(exps) >= 2:
        ne, fa = exps[0], exps[1]
        gap = (fa["yy"] * 12 + fa["mm"]) - (ne["yy"] * 12 + ne["mm"])
        iv_diff = ne["atm_iv"] - fa["atm_iv"]
        if (1 <= gap <= config.IV_CALENDAR_MAX_MONTH_GAP
                and abs(iv_diff) >= config.IV_CALENDAR_MIN_DIFF
                and ne["days"] >= config.IV_CALENDAR_NEAR_MIN_DAYS):
            Kc = ne.get("atm_strike") or K0
            if iv_diff > 0:
                # 卖近月 + 买远月（long calendar，借方/净支出，风险=净支出）
                leg_near = _leg(F, ne["T"], ne["atm_iv"], "call", Kc, False)
                leg_far = _leg(F, fa["T"], fa["atm_iv"], "call", Kc, True)
                cname = "看涨日历价差(卖近买远)"
                long_cal = True
            else:
                # 买近月 + 卖远月（short calendar，贷方/净收入，近月到期后远月裸露）
                leg_near = _leg(F, ne["T"], ne["atm_iv"], "call", Kc, True)
                leg_far = _leg(F, fa["T"], fa["atm_iv"], "call", Kc, False)
                cname = "看涨反向日历(买近卖远)"
                long_cal = False
            leg_near.update(yy=ne["yy"], mm=ne["mm"], label=ne["label"])
            leg_far.update(yy=fa["yy"], mm=fa["mm"], label=fa["label"])
            legs_cal = [leg_near, leg_far]
            debit = sum((1 if lg["buy"] else -1) * lg["prem"] for lg in legs_cal)
            t_remain = fa["T"] - ne["T"]
            est_max_profit = None
            if t_remain > 0:
                # 近月到期、标的恰好收在 K 附近时，远月剩余价值 - 净支出（标准日历最大盈利估算）
                far_resid = black76(Kc, Kc, t_remain, fa["atm_iv"], "call")
                est_max_profit = far_resid - debit if long_cal else (-debit) - far_resid
            liq_ok = (ne["n_points"] >= config.IV_SURFACE_MIN_STRIKES
                      and fa["n_points"] >= config.IV_SURFACE_MIN_STRIKES)
            # 静态盈亏空间：近月到期、标的收在K时的最优情形估算必须为正，
            # 否则时间价值损耗后无利可图（反向日历尤其可能静态最优仍亏损，应观望而非硬做）
            space_ok = est_max_profit is not None and est_max_profit > 0
            checks = [
                ("IV期限结构", True,
                 f"近月{ne['label']} ATM {ne['atm_iv']*100:.1f}% vs 远月{fa['label']} "
                 f"{fa['atm_iv']*100:.1f}%，差{iv_diff*100:+.1f}vol"
                 f"（|差|≥{config.IV_CALENDAR_MIN_DIFF*100:.0f}vol才做日历）"),
                ("到期跨度", 1 <= gap <= config.IV_CALENDAR_MAX_MONTH_GAP,
                 f"两腿相隔{gap}个月（要求1~{config.IV_CALENDAR_MAX_MONTH_GAP}个月，同行权价K≈{Kc:g}）"),
                ("近月剩余", ne["days"] >= config.IV_CALENDAR_NEAR_MIN_DAYS,
                 f"近月剩余{ne['days']}天/远月{fa['days']}天（近月≥{config.IV_CALENDAR_NEAR_MIN_DAYS}天）"),
                ("静态盈亏空间", space_ok,
                 f"假设IV不变、近月到期标的收K时最优盈利≈"
                 f"{est_max_profit:.1f}点" if est_max_profit is not None else
                 "近月到期标的收K时最优盈利无法估算（要求为正，否则时间价值损耗后无利可图）"),
                ("曲面流动性", liq_ok,
                 f"近/远月可反推行权价{ne['n_points']}/{fa['n_points']}档"
                 f"（≥{config.IV_SURFACE_MIN_STRIKES}档，缺档不插值）"),
                ("剩余到期", ne["days"] >= config.OPT_MIN_DAYS,
                 f"近月{ne['label']}估算剩余≈{ne['days']}天（≥{config.OPT_MIN_DAYS}天）"),
            ]
            iok, inode = _inst_check(inst, 0)
            checks.insert(1, ("机构观点配合", iok, inode))
            cand = _mk_candidate(cname, 0, legs_cal, F, sigma_move, checks, 7)
            if long_cal:
                cand["max_profit"], cand["max_loss"], cand["be"] = est_max_profit, max(debit, 0.0), None
                if debit > 0:
                    cand["margin_note"] = ("借方日历，最大亏损=净支出%.1f点；近月到期后只剩远月多头，"
                                           "建议支出≤账户5%%" % debit)
                else:
                    cand["margin_note"] = ("近月权利金反超远月、净%.1f点开仓(信用日历)；静态下两端保本，"
                                           "真实风险是近月到期时远月IV回落与流动性，仅可小仓位" % (-debit))
            else:
                naked = dict(leg_far, qty=1)
                margin = _naked_option_margin_points(F, naked)
                cand["max_profit"], cand["max_loss"], cand["be"] = est_max_profit, None, None
                cand["margin_points"] = margin
                cand["margin_note"] = (f"贷方日历，近月到期后远月Call裸露，保守保证金≈{margin:.1f}点；"
                                       "标的大幅上行时裸露腿理论风险无上限，仅可小仓位")
            cand["month_label_override"] = f"{ne['label']}/{fa['label']}"
            cand["legs_text_override"] = _calendar_legs_text(
                fut_row.get("sym", ""), fut_row.get("ex", ""), legs_cal)
            if est_max_profit is not None:
                cand["margin_note"] += ("；近月到期标的收近平值时最大盈利≈%.1f点(估算,依赖IV路径)"
                                        % est_max_profit)
            cands.append(cand)

    if not cands:
        return {"name": "无适合策略", "legs": [], "all_pass": False,
                "checks": [("适用性", False,
                            f"综合分{score:+.1f}、IV/HV={iv_ratio:.2f}下无满足条件的期权策略，观望")],
                "verdict": "观望", "pos_note": "", "month_label": month_label,
                "legs_text": "", "net": 0, "max_profit": None, "max_loss": None,
                "be": None, "delta": 0, "gamma": 0.0, "vega": 0, "theta_day": 0,
                "margin_points": 0.0, "margin_note": ""}

    cands.sort(key=lambda c: -c["priority"])
    chosen = next((c for c in cands if all(c2[1] for c2 in _build_checks(c, name, fut_row, month_label))),
                  None)
    if chosen is None:
        chosen = cands[0]

    checks = _build_checks(chosen, name, fut_row, month_label)
    ok_all = all(c[1] for c in checks)
    eff_month_label = chosen.get("month_label_override") or month_label
    legs_text = chosen.get("legs_text_override") or _legs_summary(
        chosen["legs"], fut_row.get("sym", ""), fut_row.get("ex", ""), yy, mm)
    month_paren = eff_month_label if "/" in eff_month_label else f"{eff_month_label}月份"
    if ok_all:
        verdict = f"建议执行：{chosen['name']}（{month_paren}）"
        mp = "理论无上限" if chosen.get("max_profit") is None else f"{chosen['max_profit']:.1f}点"
        ml = "理论无上限" if chosen.get("max_loss") is None else f"{chosen['max_loss']:.1f}点"
        pos_note = (f"具体腿: {legs_text}；净{'收入' if chosen['net'] >= 0 else '支出'}{abs(chosen['net']):.1f}点；"
                    f"最大盈利{mp} / 最大亏损{ml}；"
                    f"组合Greeks Δ{chosen['delta']:+.2f}/Γ{chosen['gamma']:+.4f}/Vega{chosen['vega']:+.1f}/Θ{chosen['theta_day']:+.1f}点每日；"
                    + chosen.get("margin_note", "买方策略最大亏损=净支出，建议≤账户5%"))
    else:
        fails = "、".join(c[0] for c in checks if not c[1])
        verdict = f"观望（未通过: {fails}）"
        pos_note = ""
    return {"name": chosen["name"], "legs": chosen["legs"], "all_pass": ok_all,
            "checks": checks, "verdict": verdict, "pos_note": pos_note,
            "month_label": eff_month_label, "legs_text": legs_text, "net": chosen["net"],
            "max_profit": chosen.get("max_profit"), "max_loss": chosen.get("max_loss"),
            "be": chosen.get("be"), "delta": chosen["delta"], "gamma": chosen.get("gamma", 0.0),
            "vega": chosen["vega"], "theta_day": chosen["theta_day"],
            "margin_points": chosen.get("margin_points", 0.0),
            "margin_note": chosen.get("margin_note", "")}


def _build_checks(cand, name, fut_row, month_label):
    """把候选自带的检查扩展上通用项（风险边界）。"""
    checks = list(cand["checks_extra"])
    if cand["net"] < 0 and cand.get("margin_points", 0.0) <= 0:
        msg = "净支出型结构，最大亏损=净支出，建议支出≤账户5%，亏50%止损"
    else:
        msg = cand.get("margin_note", "卖方结构需缴纳保证金（按交易所/期货公司标准），大幅跳空可能接近最大亏损")
    checks.append(("风险边界", True, msg))
    return checks
