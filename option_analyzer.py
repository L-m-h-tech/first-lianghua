# -*- coding: utf-8 -*-
"""【需求③】期权严格分析（比期货分析门槛更高）——单腿买入建议：
Black-76期货期权定价 + Delta/Gamma/Vega/Theta希腊字母 + 六项严格检查
（标的信号强度≥5/隐波不贵/幅度覆盖时间价值/剩余到期≥14天/Delta区间/Theta衰减）。
【需求⑤】建议标注期权月份与执行价、合约代码示意（月份来自 contracts 探测）。
【需求⑥】剩余天数优先用 webdata 的 OpenVlab 真实到期日计算。
（组合策略推荐见 option_strategies.py）

定价模型：Black-76（期货期权标准模型，国内商品期权均为期货期权）
  C = e^(-rT)·[F·N(d1) - K·N(d2)]，  P = e^(-rT)·[K·N(-d2) - F·N(-d1)]
  d1 = [ln(F/K) + σ²T/2] / (σ√T)，d2 = d1 - σ√T

参考的期权定价影响因素（经检索确认）：
  - 标的价格 / 执行价格 -> 内在价值与 Delta、Gamma
  - 隐含波动率(IV)     -> Vega，IV偏高时买方权利金贵
  - 剩余到期时间       -> 时间价值与 Theta 衰减，临近到期风险陡增
  - 无风险利率         -> 影响很小，取 r=2%

买入期权的六项严格检查（全部通过才给出"买入"建议，否则观望）：
  1. 标的综合分 |score| >= 5（期货只需2分）
  2. 波动率不贵：估计IV / HV60 <= 1.35
  3. 预期行情幅度 >= 1.5 倍平值权利金（时间价值覆盖）
  4. 剩余到期天数 >= 14 天（按探测到的合约月份估算）
  5. 建议合约 |Delta| 在 [0.35, 0.60]
  6. 每日Theta损耗 <= 权利金的 3%

合约月份说明：主分析和 contracts.ContractCache 探测的主力月份联动；
若主力月份期权临近到期，自动顺延到下一个活跃月份并给出说明。
建议执行价按近似执行价间距取整，并给出合约代码示意（实际以交易所挂牌为准）。
"""
import math

import config
import contracts as contracts_mod
from utils import norm_cdf, norm_pdf, fmt_px

R_FREE = 0.02   # 无风险利率


def black76(F, K, T, sigma, kind="call"):
    """Black-76 期货期权价格"""
    if T <= 0 or sigma <= 0:
        return max(0.0, F - K) if kind == "call" else max(0.0, K - F)
    sq = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / sq
    d2 = d1 - sq
    df = math.exp(-R_FREE * T)
    if kind == "call":
        return df * (F * norm_cdf(d1) - K * norm_cdf(d2))
    return df * (K * norm_cdf(-d2) - F * norm_cdf(-d1))


def greeks76(F, K, T, sigma, kind="call"):
    """Black-76 希腊字母：delta/gamma/vega(每1%IV)/theta(每年,除365得每天)"""
    if T <= 0 or sigma <= 0:
        d = 1.0 if (kind == "call" and F > K) else (-1.0 if (kind == "put" and F < K) else 0.0)
        return {"delta": d, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    sq = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / sq
    d2 = d1 - sq
    df = math.exp(-R_FREE * T)
    gamma = df * norm_pdf(d1) / (F * sq)
    vega = F * df * norm_pdf(d1) * math.sqrt(T) / 100.0
    decay = -F * df * norm_pdf(d1) * sigma / (2 * math.sqrt(T))
    if kind == "call":
        delta = df * norm_cdf(d1)
        price = df * (F * norm_cdf(d1) - K * norm_cdf(d2))
        theta = decay + R_FREE * df * price
    else:
        delta = -df * norm_cdf(-d1)
        price = df * (K * norm_cdf(-d2) - F * norm_cdf(-d1))
        theta = decay + R_FREE * df * price
    return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta}


def implied_vol_profile(fut_row):
    """统一期权隐波剖面，优先级：
    1) OpenVlab页面真实平值IV/IV分位/偏度；
    2) 第12轮：T型链市场价反推出的主力月份ATM IV（iv_surface，标注"T链反推"）；
    3) 无页面直读且链反推不可用时用HV估计IV，并用滚动HV分位+波动率锥作为代理。"""
    hv20 = fut_row.get("hv20") or config.DEFAULT_HV.get(fut_row.get("cat"), 0.25)
    hv60 = fut_row.get("hv60") or hv20
    page = fut_row.get("page") or {}
    atm = (page.get("atm_iv") or {}).get(fut_row.get("name")) or {}
    surf = fut_row.get("iv_surface") or {}
    market_iv = surf.get("main_atm_iv")
    if atm.get("atm_iv"):
        iv = float(atm["atm_iv"]) / 100.0
        hv_ref = float(atm.get("hv") or hv60 * 100) / 100.0
        iv_pct = atm.get("iv_pct")
        iv_src = "OpenVlab真实"
        skew = atm.get("skew")
        skew_pct = atm.get("skew_pct")
    elif market_iv:
        # 第12轮 WP-B：T链买卖中间价/最新价经 Black-76 反推的平值 IV，比 HV×1.05 更贴近盘面
        iv = float(market_iv)
        hv_ref = hv60
        iv_pct = fut_row.get("hv_percentile")
        iv_src = "T链反推"
        skew = skew_pct = None
    else:
        iv = max(hv20, hv60) * 1.05
        hv_ref = hv60
        iv_pct = fut_row.get("hv_percentile")
        iv_src = "HV估计"
        skew = skew_pct = None
    chain = page.get("option_chain") or {}
    pcr = chain.get("pcr")
    cone = fut_row.get("vol_cone") or {}
    cone20 = cone.get("20") or {}
    cone_note = "波动率锥样本不足"
    if cone20:
        p10, p50, p90 = cone20.get("p10"), cone20.get("p50"), cone20.get("p90")
        if p90 and iv >= p90:
            cone_note = "IV高于20日波动锥90%分位"
        elif p10 and iv <= p10:
            cone_note = "IV低于20日波动锥10%分位"
        elif p50:
            cone_note = f"IV位于20日波动锥{p10:.0%}/{p50:.0%}/{p90:.0%}区间"
    return {"iv": iv, "hv_ref": hv_ref or hv60, "hv20": hv20, "hv60": hv60,
            "iv_pct": iv_pct, "iv_src": iv_src, "skew": skew, "skew_pct": skew_pct,
            "pcr": pcr, "cone": cone, "cone_note": cone_note,
            "market_iv": market_iv}


def iv_pct_text(profile):
    pct = profile.get("iv_pct")
    if pct is None:
        return "分位样本不足"
    if profile["iv_src"] == "T链反推":
        return f"HV滚动分位{pct*100:.0f}%(代理IV分位)"
    return f"{profile['iv_src']}分位{pct * 100:.0f}%"


def analyze_option(name, fut_row):
    """对单个有场内期权的品种做严格分析，返回结果字典"""
    F = fut_row["price"]
    volp = implied_vol_profile(fut_row)
    hv20, hv60 = volp["hv20"], volp["hv60"]
    iv = volp["iv"]
    score = fut_row["score"]

    # 期权月份与剩余天数（来自主力合约探测，临近到期自动顺延）
    om = fut_row.get("opt_month") or {}
    yy, mm = om.get("yy", 0), om.get("mm", 0)
    month_label = f"{yy:02d}{mm:02d}" if yy else "主力月"
    days = om.get("opt_days") or config.OPT_ASSUMED_DAYS
    T = max(days, 5) / 365.0
    kind = "call" if score > 0 else "put"
    direction = "看涨" if score > 0 else "看跌"

    checks = []

    # 第11轮：完整期权T链概况（持仓量PCR/腿数/最大持仓行权价/ATM/PCR分位）
    chain = fut_row.get("option_chain") or {}
    chain_note = ""
    if chain and chain.get("pcr_oi") is not None:
        bits = ["持仓PCR=%.2f（%s）" % (chain["pcr_oi"], chain.get("sentiment") or "中性"),
                "C/P各%d/%d腿" % (chain.get("n_call", 0), chain.get("n_put", 0)),
                "看涨持仓%.0f/看跌持仓%.0f" % (chain.get("call_oi", 0), chain.get("put_oi", 0))]
        if chain.get("max_call_oi_strike"):
            bits.append("最大看涨持仓%g(压力)" % chain["max_call_oi_strike"])
        if chain.get("max_put_oi_strike"):
            bits.append("最大看跌持仓%g(支撑)" % chain["max_put_oi_strike"])
        if chain.get("atm_strike"):
            bits.append("平值行权价%g" % chain["atm_strike"])
        if chain.get("pcr_pct") is not None:
            bits.append("PCR近%d日分位%.0f%%" % (config.PCR_LOOKBACK_DAYS, chain["pcr_pct"] * 100))
        chain_note = "；".join(bits)

    # 第12轮 WP-B：多到期日 IV 曲面（ATM IV期限结构 / 25Δ风险反转 / 曲面矩阵）
    surf = fut_row.get("iv_surface") or {}
    surface_note = surf.get("summary_line", "")
    surface_matrix = surf.get("matrix_line", "")

    # 1) 标的信号强度（比期货更严格）
    ok1 = abs(score) >= config.OPT_SCORE_MIN
    checks.append(("标的信号强度", ok1,
                   f"综合分{score:+.1f}，期权要求|分|≥{config.OPT_SCORE_MIN}"))

    # 2) 波动率贵贱（IV/HV + IV历史分位 + 波动率锥）
    iv_ratio = iv / volp["hv_ref"] if volp["hv_ref"] > 0 else 9.9
    iv_pct = volp.get("iv_pct")
    pct_ok = iv_pct is None or iv_pct <= config.OPT_IV_PCT_BUY_MAX
    ok2 = iv_ratio <= config.OPT_IV_HV_RATIO_MAX and pct_ok
    skew_txt = ""
    if volp.get("skew") is not None:
        skew_txt = f"，偏度{volp['skew']:+.2f}"
    pcr_txt = "" if volp.get("pcr") is None else f"，PCR={volp['pcr']:.2f}"
    xcheck_txt = ""
    if volp.get("market_iv") and volp["iv_src"] == "OpenVlab真实":
        d_iv = iv - volp["market_iv"]
        if abs(d_iv) > config.IV_CROSS_CHECK_DIFF:
            xcheck_txt = f"，链反推ATM{volp['market_iv']*100:.0f}%与页面差{d_iv*100:+.0f}vol(以盘面为准)"
    checks.append(("波动率不贵", ok2,
                   f"{volp['iv_src']}IV {iv*100:.0f}%/HV {volp['hv_ref']*100:.0f}%"
                   f"={iv_ratio:.2f}；{iv_pct_text(volp)}；{volp['cone_note']}"
                   f"{skew_txt}{pcr_txt}{xcheck_txt}（裸买要求比值≤{config.OPT_IV_HV_RATIO_MAX}"
                   f"且分位≤{config.OPT_IV_PCT_BUY_MAX:.0%}，否则改价差或观望）"))

    # 3) 预期行情幅度覆盖时间价值
    atm_prem = black76(F, F, T, iv, kind=kind)
    strength = 0.8 + min(abs(score), 10.0) / 10.0 * 0.7
    exp_move = F * hv20 * math.sqrt(T) * strength
    cover = exp_move / atm_prem if atm_prem > 0 else 0.0
    ok3 = cover >= config.OPT_EXPECT_COVER
    checks.append(("幅度覆盖时间价值", ok3,
                   f"预期波动≈{exp_move:.1f}点 vs 平值权利金≈{atm_prem:.1f}点"
                   f"（覆盖{cover:.1f}倍，要求≥{config.OPT_EXPECT_COVER}）"))

    # 4) 剩余到期时间
    ok4 = days >= config.OPT_MIN_DAYS
    exp_note = (f"，到期日{om['exp_date'].strftime('%Y-%m-%d')}(OpenVlab真实到期日)"
                if om.get("exp_date") else "（按交割月前一月中旬近似）")
    checks.append(("剩余到期时间", ok4,
                   f"{month_label}月份期权剩余≈{days}天{exp_note}，"
                   f"要求≥{config.OPT_MIN_DAYS}天"))

    # 5) 建议执行价：强信号做虚一档（杠杆高），中等信号做平值（胜率高）；按近似档位取整
    strong = abs(score) >= config.SCORE_MID
    step = config.strike_step(F)
    raw_k = F * (1.02 if kind == "call" else 0.98) if strong else F
    K = round(raw_k / step) * step
    kname = "虚一档" if strong else "平值"
    prem = black76(F, K, T, iv, kind=kind)
    g = greeks76(F, K, T, iv, kind=kind)
    theta_day = g["theta"] / 365.0
    ok5 = config.OPT_DELTA_BAND[0] <= abs(g["delta"]) <= config.OPT_DELTA_BAND[1]
    checks.append(("Delta区间", ok5,
                   f"建议合约Delta≈{g['delta']:.2f}，要求|Delta|∈{config.OPT_DELTA_BAND}"))

    # 6) 时间价值衰减速度
    ok6 = prem > 0 and abs(theta_day) / prem <= config.OPT_THETA_DAY_MAX
    checks.append(("衰减可承受", ok6,
                   f"Theta≈{theta_day:.2f}点/天，占权利金"
                   f"{abs(theta_day)/prem*100 if prem > 0 else 99:.1f}%/天"
                   f"（上限{config.OPT_THETA_DAY_MAX*100:.0f}%）"))

    # 期权合约代码示意
    opt_code = ""
    if yy:
        opt_code = contracts_mod.option_code_hint(
            fut_row.get("sym", ""), fut_row.get("ex", ""), yy, mm, K, kind)

    all_pass = all(c[1] for c in checks) and ok1
    if all_pass:
        breakeven = K + prem if kind == "call" else K - prem
        verdict = f"买入{direction}期权（{month_label}月份·{kname}·执行价≈{K:g}）"
        pos_note = (f"参考代码 {opt_code}（示意，以交易所挂牌为准）；到期前需标的价格"
                    f"{'涨' if kind == 'call' else '跌'}过盈亏平衡点≈{fmt_px(breakeven)}"
                    f"（含权利金{prem:.1f}点）；仓位≤对应期货建议的1/3，权利金亏50%即止损")
    else:
        fails = "、".join(c[0] for c in checks if not c[1])
        verdict = f"观望/不参与（未通过: {fails}）"
        pos_note = ""

    return {"name": name, "score": score, "kind": kind, "direction": direction,
            "underlying_price": F,
            "iv": iv, "iv_ratio": iv_ratio, "hv20": hv20, "hv60": hv60,
            "iv_pct": iv_pct, "iv_src": volp["iv_src"], "skew": volp.get("skew"),
            "skew_pct": volp.get("skew_pct"), "pcr": volp.get("pcr"),
            "vol_cone": volp.get("cone") or {}, "cone_note": volp.get("cone_note", ""),
            "yy": yy, "mm": mm, "month_label": month_label, "days": days,
            "K": K, "kname": kname, "prem": prem,
            "delta": g["delta"], "gamma": g["gamma"], "vega": g["vega"],
            "theta_day": theta_day, "cover": cover,
            "opt_code": opt_code, "month_note": fut_row.get("month_note", ""),
            "checks": checks, "verdict": verdict, "pos_note": pos_note,
            "chain": chain, "chain_note": chain_note,
            "surface_note": surface_note, "surface_matrix": surface_matrix,
            "surface_brief": ({"term_shape": surf.get("term_shape"),
                               "term_diff": surf.get("term_diff"),
                               "main_atm_iv": surf.get("main_atm_iv")} if surf else {}),
            "all_pass": all_pass}
