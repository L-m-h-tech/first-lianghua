# -*- coding: utf-8 -*-
"""【需求②】期货综合分析与购买建议（评级/仓位/ATR止损止盈）：
综合分 = 新闻因子【需求①】+ 原油联动【需求①】+ 机构动向【需求⑥】+ 日线动量 + 盘中动量，
范围[-10,+10]；信号分级 观望/轻仓/分批建仓/顺势持有。
【需求⑤】建议自动标注主力合约月份（如"做空 ss2610"）、交割月移仓提示。
【需求⑥】机构动向因子(±2)与 inst_note 由 webdata 提供数据。
【需求⑦】forecast_line() 非交易时段预测走向（五路规则投票：综合因子/机构观点/
  消息面趋势/日线动量/原油隔夜方向）。
【需求⑩】建议/报告上的时间与轮动节奏标注由 report.render + rotation_desc 提供。
"""
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import config
import contracts as contracts_mod
import trade_calendar
import fundamental_factors
from utils import clip, fmt_px
from factors import sentiment_facets, facet_tags

# 保护 _parts_via_plugins 模块级全局（factor_parts 注册表）——run_cycle 与 paper_ticker
# 可能并发调用 analyze_all_varieties，必须互斥，否则两线程互踩 factor_parts/factor_plugin。
_analyze_lock = threading.Lock()


def rating(score):
    """综合分 -> (信号标签, 操作建议, 置信度%)"""
    s = abs(score)
    d = "多" if score > 0 else "空"
    if s < config.SCORE_NEUTRAL:
        return "中性", "观望：信号不足，多看少动", 40
    if s < config.SCORE_LIGHT:
        return f"偏{d}", f"轻仓试{d}（≤20%仓位），严格止损", 55
    if s < config.SCORE_MID:
        return f"看{d}", f"分批建{d}（20%~40%仓位），ATR止损", 70
    return f"强看{d}", f"顺势持{d}（40%~60%仓位），移动止损跟踪", 82


def _parts_via_plugins(fallback, oil_w, news_score, oil_score, inst, ind, kline_ok, price,
                       tick_mom, flow, term, fund_raw):
    """G2 最后一切片（第60轮）：经 factor_parts 注册表装配 9 个 live part 重建 parts。

    惰性 import 插件层（顶层不依赖）；9 个 part 已在 factor_parts.selftest 逐位 parity，
    重建结果与内联路径逐字节一致。任何异常都回退到内联 fallback，绝不影响主链可用性。
    """
    try:
        import factor_parts
        import factor_plugin as _fp
        factor_parts.register_builtin_parts(replace=True)
        try:
            return factor_parts.assemble_live_parts(
                news_score=news_score, oil_w=oil_w, oil_score=oil_score, inst=inst, ind=ind,
                kline_ok=kline_ok, price=price, tick_mom=tick_mom, flow=flow, term=term,
                fund_raw=fund_raw)
        finally:
            _fp.clear()
    except Exception:
        return fallback


def analyze_variety(name, meta, quote, ind, kline_ok, news_score, news_hits,
                    oil_score, tick_mom, contract=None, inst=None, page=None, flow=None,
                    fund_raw=None):
    """对单个品种生成分析结果行（contract合约月份/inst机构观点/page浏览器页面数据，均可为None）。
    flow 为 flow_tracker 根据相邻轮次成交量/持仓量算出的量仓资金因子。
    fund_raw 为第13轮基本面原料 {"inv":库存时序, "rank":(L,S,pL,pS)或None, "basis":基差率或None}。"""
    price = (quote.get("latest") or 0) or (ind.get("close") or 0)
    chg = quote.get("chg_pct") or 0.0
    flow = flow or {}
    # 期限结构提前计算（基本面carry因子与下方主力月份都要用）
    term = contracts_mod.term_structure(contract) if contract else None

    parts = {"新闻消息面": news_score}
    if meta["oil_w"] > 0:
        parts["原油联动(w=%.2f)" % meta["oil_w"]] = oil_score * meta["oil_w"]

    # 机构动向（交易可查AI研报：看多/震荡/看空机构数）
    inst_note, inst_ratio = "", None
    if inst and inst.get("total", 0) >= 3:
        inst_ratio = (inst["bullish"] - inst["bearish"]) / inst["total"]
        parts["机构动向"] = math.tanh(inst_ratio * 2.0) * 2.0
        inst_note = (f"机构观点(交易可查AI研报): 看多{inst['bullish']}/震荡{inst['volatile']}"
                     f"/看空{inst['bearish']}（共{inst['total']}家）")

    tech_info = ind.get("tech") or {}
    intraday_info = ind.get("intraday") or {}
    if kline_ok and price > 0:
        momentum = (math.tanh(ind["ret5"] * 160) * 2.5 +
                    math.tanh(ind["ret20"] * 70) * 2.0)
        if ind.get("ma10"):
            momentum += math.tanh((price / ind["ma10"] - 1) * 220) * 1.0
        parts["日线动量"] = momentum
        resonance = float(tech_info.get("resonance_score") or 0.0)
        if abs(resonance) > 0.01:
            parts["技术共振"] = resonance
    intra_resonance = float(intraday_info.get("resonance_score") or 0.0)
    if intraday_info.get("ok") and abs(intra_resonance) > 0.01:
        parts["分钟共振"] = intra_resonance
    if abs(tick_mom) > 0.01:
        parts["盘中动量"] = tick_mom
    flow_score = float(flow.get("score") or 0.0)
    if abs(flow_score) > 0.01:
        parts["量仓资金"] = flow_score

    # 第13轮 WP-C：基本面因子（库存仓单/龙虎榜/期限carry/基差，缺项自动归一，不编造）
    fund_pack = None
    inv_in = (fund_raw or {}).get("inv")
    rank_in = (fund_raw or {}).get("rank")
    basis_in = (fund_raw or {}).get("basis")
    inv_f = fundamental_factors.inventory_factor(inv_in) if inv_in else None
    rank_f = (fundamental_factors.rank_factor(*rank_in) if rank_in else None)
    carry_f = fundamental_factors.carry_factor(term)
    basis_f = fundamental_factors.basis_factor(basis_in) if basis_in is not None else None
    fund_pack = fundamental_factors.build_fundamental(inv_f, rank_f, carry_f, basis_f)
    if fund_pack and abs(fund_pack["score"]) > 0.01:
        parts["基本面"] = fund_pack["score"]

    # G2 最后一切片（第61轮起默认开，config.PLUGIN_PARTS_ENABLED）：用 factor_parts 注册表重建 parts。
    # 9 个 part 已逐位 parity，重建结果与上面内联一致；任何异常都由 helper 回退内联 parts，主链不受影响。
    if getattr(config, "PLUGIN_PARTS_ENABLED", True):
        parts = _parts_via_plugins(
            parts, meta.get("oil_w", 0.0), news_score, oil_score, inst, ind, kline_ok, price,
            tick_mom, flow, term, fund_raw)

    score = clip(sum(parts.values()), -10.0, 10.0)
    label, advice, conf = rating(score)

    atr = ind.get("atr") or 0.0
    if atr <= 0 and price > 0:
        atr = price * 0.015
    d = 1 if score > 0 else -1
    stop = price - d * 1.2 * atr
    target = price + d * 2.0 * atr

    risks = []
    hv20 = ind.get("hv20") or 0.0
    if hv20 > 0.35:
        risks.append("历史波动率偏高(HV20=%.0f%%)，注意控制仓位" % (hv20 * 100))
    if tech_info.get("rsi_note"):
        risks.append(tech_info["rsi_note"] + "，追单需等回踩/反抽确认")
    vote_sum = int(tech_info.get("vote_sum") or 0)
    if abs(score) >= config.SCORE_LIGHT and vote_sum * score < 0:
        risks.append("综合分方向与短中长技术周期共振相反，需防假突破")
    elif abs(score) >= config.SCORE_LIGHT and abs(vote_sum) < 3:
        risks.append("短中长周期尚未完全共振，仓位不宜一次打满")
    if abs(score) >= config.SCORE_LIGHT and intra_resonance and score * intra_resonance < 0:
        risks.append("30/60分钟级别与综合方向背离，等分钟级修复或回踩确认")
    elif abs(score) >= config.SCORE_MID and abs(intra_resonance) < 0.2 and intraday_info.get("ok"):
        risks.append("日线有方向但30/60分钟未完全共振，避免追在分钟短线末端")
    hv_pct = ind.get("hv_percentile")
    if hv_pct is not None and hv_pct >= 0.9:
        risks.append(f"HV20处于近阶段{hv_pct*100:.0f}%分位，波动放大，止损放宽并降低仓位")
    if not trade_calendar.is_trade_day(datetime.now()):
        risks.append("今日非交易日（周末/法定节假日），信号基于最近交易日数据")
    if kline_ok and price > 0 and ind.get("ma20"):
        if (score > 0 and price < ind["ma20"]) or (score < 0 and price > ind["ma20"]):
            risks.append("消息面与技术面方向不一致，建议等回踩/反抽确认")

    flow_note = ""
    if flow.get("pattern") and flow.get("pattern") != "量仓平稳":
        flow_note = (f"{flow['pattern']}：持仓变化{flow.get('oi_pct', 0) * 100:+.2f}%"
                     f"（{flow.get('prev_open_interest', 0):.0f}→{flow.get('open_interest', 0):.0f}），"
                     f"成交量相对近几轮均值{flow.get('volume_ratio', 1):.2f}倍，因子{flow_score:+.2f}")

    tech_note = ""
    if tech_info:
        tech_note = (
            f"{tech_info.get('resonance_note', '')}；RSI14={tech_info.get('rsi14', 0):.1f}"
            f"，MACD柱={tech_info.get('macd_hist', 0):.2f}"
            f"，KDJ={tech_info.get('kdj_k', 0):.1f}/{tech_info.get('kdj_d', 0):.1f}"
            f"/{tech_info.get('kdj_j', 0):.1f}，BOLL区间"
            f"{tech_info.get('boll_low', 0):g}~{tech_info.get('boll_up', 0):g}")
        if intraday_info.get("ok"):
            tech_note += "；" + intraday_info.get("resonance_note", "")

    # G7（第30轮）多窗口时序动量：影子记录，绝不加入 parts、绝不改变 score；
    # TSMOM_SHADOW=False（或取数失败/历史不足）时为 None，分析行与本轮之前逐字节等价。
    tsmom_shadow = None
    if getattr(config, "TSMOM_SHADOW", False) and kline_ok and price > 0:
        _shadow = {k: ind.get(k) for k in
                   ("ret63", "ret126", "ret252", "tsmom63", "tsmom126", "tsmom252")}
        _shadow["blend"] = ind.get("tsmom_blend")
        _shadow["n_valid"] = int(ind.get("tsmom_n_valid") or 0)
        if _shadow["n_valid"] > 0 or any(v is not None for k, v in _shadow.items()
                                         if k.startswith("ret")):
            tsmom_shadow = _shadow

    # 主力合约月份与期权月份（term 已在函数开头算好）
    contract_code, main_month = "", ""
    opt_month, month_note = None, ""
    if contract:
        main_c = contract.get("main")
        if main_c:
            contract_code = contracts_mod.contract_code(
                meta["sym"], meta["ex"], main_c["yy"], main_c["mm"])
            main_month = f"{main_c['yy']:02d}{main_c['mm']:02d}"
            dd = contracts_mod.days_to_delivery(main_c["yy"], main_c["mm"])
            if dd <= 0:
                risks.append(f"主力合约{contract_code}已进入交割月，个人客户持仓需移仓")
            elif dd < 45:
                risks.append(f"主力合约{contract_code}距交割月约{dd}天，临近交割注意移仓换月")
        opt_month = contract.get("opt_month")
        if opt_month and main_c and (opt_month["yy"], opt_month["mm"]) != (main_c["yy"], main_c["mm"]):
            month_note = (f"主力{main_month}月份期权临近到期，建议顺延至"
                          f"{opt_month['yy']:02d}{opt_month['mm']:02d}月份")
        # 期限结构 term 已在函数开头由 contracts.term_structure 组装（第13轮其carry进入综合分）

    result = {"name": name, "code": meta["code"], "sym": meta["sym"], "ex": meta["ex"],
            "cat": meta["cat"], "oil_w": meta["oil_w"],
            "price": price, "chg": chg, "score": score,
            "parts": parts, "label": label, "advice": advice, "conf": conf,
            "stop": stop, "target": target, "atr": atr,
            "hits": news_hits, "risks": risks,
            "volume": float(quote.get("volume") or 0.0),
            "open_interest": float(quote.get("open_interest") or 0.0),
            "flow": flow, "flow_note": flow_note,
            "hv20": hv20, "hv60": ind.get("hv60") or hv20,
            "tech": tech_info, "intraday": intraday_info, "tech_note": tech_note,
            "hv_percentile": ind.get("hv_percentile"),
            "vol_cone": ind.get("vol_cone") or {},
            "last_date": ind.get("last_date", ""),
            "contract_code": contract_code, "main_month": main_month,
            "opt_month": opt_month, "month_note": month_note, "term": term,
            "fundamental": fund_pack,
            "inst": inst or {}, "inst_ratio": inst_ratio, "inst_note": inst_note,
            "page": page or {}, "forecast": "",
            "tsmom_shadow": tsmom_shadow}
    result["debate"] = build_debate(result)
    return result


def forecast_line(row, news_trend=0.0, oil_dir=0.0):
    """非交易时段的预测走向（规则投票，非保证）：综合因子/机构观点/消息面趋势/
    日线动量/原油隔夜方向 加权投票，给出方向倾向与参考概率"""
    votes = []
    s = row["score"]
    votes.append((1.5, (s / abs(s)) if abs(s) >= 0.2 else 0.0, "综合因子方向"))
    ir = row.get("inst_ratio")
    if ir is not None:
        votes.append((1.0, max(-1.0, min(1.0, ir * 2.0)), "机构观点"))
    if news_trend:
        votes.append((1.0, max(-1.0, min(1.0, news_trend / 2.0)), "消息面近4小时趋势"))
    tech = (row.get("parts") or {}).get("日线动量")
    if tech:
        votes.append((1.0, max(-1.0, min(1.0, tech / 3.0)), "日线动量"))
    if row.get("oil_w", 0) > 0 and oil_dir:
        votes.append((0.5, float(oil_dir), "原油隔夜方向"))
    net = sum(w * v for w, v, _ in votes)
    if abs(net) < 0.8:
        label = "震荡为主" + ("、略偏多" if net > 0.3 else "、略偏空" if net < -0.3 else "")
        prob = 55.0
    else:
        label = "偏多" if net > 0 else "偏空"
        prob = 50 + min(18.0, abs(net) / 4.0 * 18.0)
    basis = "、".join(t for w, v, t in votes if abs(v) >= 0.3)
    return (f"预测走向(非交易时段·规则预测仅供参考): {label}，参考概率约{prob:.0f}%"
            f" —— 依据: {basis}；开盘后请以实际行情校验并重新评估")


def direction_text(score):
    return "做多" if score > 0 else "做空"


# 因子键 -> 多空卡上的短标签
_DEBATE_TAG = {"消息面": "消息", "原油联动": "原油", "机构动向": "机构",
               "日线动量": "日线趋势", "盘中动量": "盘中动量"}


def build_debate(row):
    """WP-F1 A1 多空双面论证卡：把同一品种的多、空证据分别列清，强制暴露反方依据。

    verdict（多方/空方/均衡）与综合分方向一致——价值不在改分，而在"自我对抗"：
    即使给做多建议，也把利空证据一并摆出，避免只报喜。纯函数、零网络。
    """
    bull, bear = [], []
    _flow = row.get("flow") or {}
    _fs = _flow.get("score")
    _has_flow = _fs is not None and abs(_fs) > 0.05
    for k, v in (row.get("parts") or {}).items():
        # "基本面"与下方 fundamental 同源、"量仓资金"由下方量仓分支带 pattern 更全，跳过避免重复
        if k == "基本面" or (k == "量仓资金" and _has_flow):
            continue
        tag = _DEBATE_TAG.get(k, k)
        if v > 0.05:
            bull.append("%s%+.1f" % (tag, v))
        elif v < -0.05:
            bear.append("%s%+.1f" % (tag, v))
    chg = float(row.get("chg", 0.0) or 0.0)
    if chg > 0.002:
        bull.append("当日%+.2f%%" % (chg * 100))
    elif chg < -0.002:
        bear.append("当日%+.2f%%" % (chg * 100))
    flow = _flow
    fs = _fs
    if fs is not None and abs(fs) > 0.05:
        item = "量仓%s%+.2f" % (flow.get("pattern", ""), fs)
        (bull if fs > 0 else bear).append(item)
    ir = row.get("inst_ratio")
    if ir is not None and abs(ir) >= 0.10:
        (bull if ir > 0 else bear).append("机构净%s%.0f%%" % ("多" if ir > 0 else "空", abs(ir) * 100))
    fp = row.get("fundamental")
    if fp and abs(fp.get("score", 0.0)) >= 0.15:
        (bull if fp["score"] > 0 else bear).append("基本面%+.2f" % fp["score"])
    hvp = row.get("hv_percentile")
    if hvp is not None and hvp >= 0.80:
        bear.append("波动分位%.0f%%偏高" % (hvp * 100))
    sc = float(row.get("score", 0.0) or 0.0)
    if sc >= 0.3:
        verdict = "多方占优"
    elif sc <= -0.3:
        verdict = "空方占优"
    else:
        verdict = "多空均衡"
    return {"bull": bull, "bear": bear, "verdict": verdict}


def detail_lines(row):
    """重点品种的操作明细行"""
    d = 1 if row["score"] > 0 else -1
    side = direction_text(row["score"])
    code_part = f"（{row['contract_code']}）" if row.get("contract_code") else ""
    cpart = f" {row['contract_code']}" if row.get("contract_code") else ""
    lines = []
    lines.append(f"● {row['name']}{code_part} [{row['cat']}] 综合分 {row['score']:+.1f} "
                 f"{row['label']} (置信度{row['conf']}%)")
    parts = " | ".join(f"{k} {v:+.1f}" for k, v in row["parts"].items())
    lines.append(f"    因子: {parts}")
    deb = row.get("debate") or build_debate(row)
    lines.append("    多空: 多[%s] vs 空[%s] → %s"
                 % ("、".join(deb["bull"]) or "无明确利多",
                    "、".join(deb["bear"]) or "无明确利空", deb["verdict"]))
    gate = row.get("risk") or {}
    if gate.get("reasons"):
        lines.append("    风控: " + "；".join(gate["reasons"]))
    calib = row.get("calib") or {}
    if calib.get("note"):
        lines.append("    校准: " + calib["note"])
    if row.get("tech_note"):
        lines.append(f"    技术: {row['tech_note']}")
    if row.get("flow_note"):
        lines.append(f"    量仓: {row['flow_note']}")
    if row.get("inst_note"):
        lines.append(f"    {row['inst_note']}")
    lines.append(f"    操作: {row['advice']} | 方向:{side}{cpart} | 参考开仓 {fmt_px(row['price'])} "
                 f"| 止损 {fmt_px(row['stop'])}(1.2×ATR) | 目标 {fmt_px(row['target'])}(2×ATR)")
    for s, n in row["hits"]:
        t = n.get("time").strftime("%m-%d %H:%M")
        fac = sentiment_facets(n.get("content", ""), variety=row["name"], cat=row["cat"])
        ftag = facet_tags(fac)
        lines.append(f"    消息: [{n.get('source')} {t}] {n.get('content','')[:66]} "
                     f"({s:+.1f}" + (f" ·{ftag}" if ftag else "") + ")")
    if row.get("month_note"):
        lines.append(f"    月份: {row['month_note']}")
    if row.get("term"):
        lines.append(f"    {row['term']['note']}")
    if row.get("fundamental"):
        lines.append(f"    {row['fundamental']['note']}")
    p = row.get("page") or {}
    if p.get("atm_iv"):
        a = p["atm_iv"]
        # 第107轮修复：防御性取值——atm_iv 键可能不存在（页面结构多来源差异）或值为 None
        _iv = a.get("atm_iv") if isinstance(a, dict) else None
        if _iv is not None:
            days_txt = f", 剩余{a.get('days')}天" if a.get("days") is not None else ""
            prem_txt = f", 溢价{a.get('prem'):+.2f}" if a.get("prem") is not None else ""
            first = f"    页面数据: OpenVlab真实平值隐波 {_iv:.1f}%"
            if a.get("iv_chg") is not None and a.get("source") != "ctamap":
                first += f"(变化{a.get('iv_chg'):+.2f}{days_txt}{prem_txt})"
            else:
                exp_txt = f", 合约{a.get('code')}" if a.get("code") else ""
                first += f"{exp_txt}{days_txt}"
                if a.get("iv_pct") is not None:
                    first += f", 百分位{a.get('iv_pct'):.0f}%"
                if a.get("skew") is not None:
                    first += f", 偏度{a.get('skew'):+.2f}"
            lines.append(first)
    elif p.get("rank"):
        r = p["rank"]
        lines.append(f"    页面数据: OpenVlab[{r['list']}] 隐波变化{r['iv_chg']:+.2f}")
    elif p.get("prem"):
        pr = p["prem"]
        lines.append(f"    页面数据: OpenVlab[{pr['list']}] 隐波{pr['iv']:.1f}%/实波{pr['hv']:.1f}%"
                     f" 溢价{pr['prem']:+.2f}")
    for h in p.get("headlines", []):
        d = "看多" if h["dir"] > 0 else "看空"
        lines.append(f"    页面动向: 交易可查[{h['label']}] {d} ({h['text']})")
    # 新增：乾坤归一综合评级（按标准品种名精确匹配，避免"铝"误匹配"氧化铝/铝合金"）
    for txt, rt in (p.get("rating") or {}).items():
        if rt.get("variety") == row["name"]:
            chg_txt = f", 涨跌幅{rt['chg']:+.2f}%" if rt.get("chg") is not None else ""
            lines.append(f"    页面评级: 交易可查乾坤归一[{rt['grade']}]"
                         f" {rt.get('contract','')} 评级{rt['grade']}"
                         f" 价{rt.get('price','-')}{chg_txt}")
            break
    # 新增：外盘比价（仅展示与本品种精确相关的外盘品种，避免每个品种都带伦铜）
    _EXT_REL = {"黄金": "美黄金", "白银": "美白银", "铜": "伦铜", "铝": "伦铝",
                "锌": "伦锌", "镍": "伦镍", "铅": "伦铅", "锡": "伦锡",
                "大豆": "美豆", "豆粕": "美豆粕", "豆油": "美豆油", "棉花": "美棉花",
                "玉米": "美玉米", "原油": "布原油", "棕榈油": "马棕油",
                "铁矿石": "铁矿FE"}
    ext_map = p.get("external") or {}
    ext_name = _EXT_REL.get(row["name"])
    if ext_name and ext_name in ext_map:
        xd = ext_map[ext_name]
        dev = xd.get("dev")
        dev_txt = f" 偏离{dev:+.2f}%" if dev is not None else ""
        lines.append(f"    外盘: {ext_name} {xd.get('last','-')}{dev_txt}"
                     f" (基准{xd.get('base','-')})")
    # 新增：基本信息（仅展示与本品种同名的基本面项）
    for f in (p.get("fundamentals") or []):
        if f.get("item") == row["name"] or f.get("metric") == row["name"]:
            chg_txt = f"（变动{f['chg']}）" if f.get("chg") is not None else ""
            lines.append(f"    基本面: {f['metric']} {f.get('value','-')}{chg_txt}"
                         f" ({f.get('date','')})")
    for r in row["risks"]:
        lines.append(f"    风险: {r}")
    if row.get("forecast"):
        lines.append(f"    {row['forecast']}")
    return lines


# ================== 共享全品种分析入口（run_cycle 与 paper_ticker 共用） ==================

def _prefetch_rank(state, watchlist):
    """按各品种主力合约并发预取龙虎榜前20席多空合计（fetcher内日缓存，当天仅首轮产生请求）。"""
    tasks = []
    for key, meta in watchlist:
        cinfo = state.contracts.get(meta["sym"])
        mc = (cinfo or {}).get("main")
        if not mc:
            continue
        emc = state.fetcher.em_code(meta["sym"])
        if emc:
            tasks.append((key, emc, mc["yy"], mc["mm"]))
    out = {}

    def one(t):
        key, emc, yy, mm = t
        try:
            return key, state.fetcher.rank_totals(emc, yy, mm)
        except Exception:
            return key, None

    if tasks:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for key, rk in ex.map(one, tasks):
                if rk:
                    out[key] = (rk["long"], rk["short"], rk["prev_long"], rk["prev_short"])
    return out


def _tick_momentum(hist):
    """盘中动量：基于程序运行期间每轮记录的价格（10分钟+30分钟）。"""
    if not hist or len(hist) < 12:
        return 0.0
    now_ts, now_px = hist[-1]
    if now_ts - hist[0][0] < 600:   # 数据不足10分钟不计算
        return 0.0

    def ret_over(sec):
        target = now_ts - sec
        base = None
        for ts, px in hist:
            if ts >= target:
                base = px
                break
        if not base:
            return 0.0
        return now_px / base - 1.0

    r10, r30 = ret_over(600), ret_over(1800)
    m = math.tanh(r10 * 2000) * 1.0 + math.tanh(r30 * 1000) * 1.0
    return clip(m, -1.5, 1.5)


def analyze_all_varieties(state, watchlist, quotes, flow_map):
    """共享全品种分析：逐品种综合分 + 非交易时段预测走向（run_cycle 与 paper_ticker 共用）。

    - 与主报告同一份评分路径（analyze_variety + forecast_line），保证开仓信号口径一致；
    - 只读 state 缓存（oil/news/klines/contracts/webdata/breader/fund_* 均为后台/共享维护），
      ticker 每分钟调用不产生高频网络（命中 TTL 缓存，唯一 fetch 是调用方传入的 quotes）；
    - 不写 state.last_forecasts（由 run_cycle 调用方赋值，报告侧输出）；
    - 不调用期权链预热（期权沿用 run_cycle 快照，Policy A）；
    - 并发安全：内部持 _analyze_lock，与 run_cycle 互斥 factor_parts 注册表全局。
    """
    from utils import is_variety_trading  # 局部 import，避免 analyzer 顶层依赖 utils 循环
    from utils import LOG
    with _analyze_lock:
        inst_map = state.webdata.views_snapshot()
        intraday_map = state.klines.warm_intraday(
            [(meta["code"], meta["cat"]) for _, meta in watchlist])
        rank_map = _prefetch_rank(state, watchlist)
        fut_rows = []
        for key, meta in watchlist:
            q = quotes.get(meta["code"]) or {}
            ind, kline_ok = state.klines.get(meta["code"], meta["cat"])
            ind = dict(ind)
            ind["intraday"] = intraday_map.get(meta["code"], ({}, False))[0]
            n_score, n_hits = state.news.score(meta["cat"], variety=key)
            o_score = state.oil.combined_score() if meta["oil_w"] > 0 else 0.0
            t_mom = _tick_momentum(state.var_hist.get(key))
            cinfo = state.contracts.get(meta["sym"])
            fund_raw = {"inv": state.fund_inv.get(meta["sym"]),
                        "rank": rank_map.get(key),
                        "basis": (state.fund_basis or {}).get(meta["sym"])}
            try:
                fut_rows.append(analyze_variety(
                    key, meta, q, ind, kline_ok, n_score, n_hits, o_score, t_mom,
                    cinfo, inst_map.get(key), state.breader.page_info(key),
                    flow=flow_map.get(meta["code"]), fund_raw=fund_raw))
            except Exception as e:
                LOG.warning("品种分析失败 %s: %s", key, e)

        # 非交易时段趋势（3.5 段）：仅对不在自身交易时段的品种附加预测走向
        news_trend = state.news.trend()
        oil_dir = state.oil.direction()
        for row in fut_rows:
            vmeta = config.VARIETIES.get(row["name"])
            if vmeta and is_variety_trading(vmeta):
                continue
            try:
                row["forecast"] = forecast_line(row, news_trend, oil_dir)
            except Exception as e:
                pass
        return fut_rows
