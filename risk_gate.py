# -*- coding: utf-8 -*-
"""WP-F1（P0）A2：独立风控闸门（与打分解耦的第二道防线）。

analyzer 负责"找机会"（综合分越高越值得做），risk_gate 负责"挑毛病"：
独立复核每一条信号，只产出 veto（建议暂缓）/ warn（提示风险）/ pass（通过），
**默认不改综合分、不改建议**——veto 仅在报告显著标注并走告警通道；
只有 config.RISK_GATE_AUTO_DOWNGRADE=True 时，才由 main 把 veto 信号降级为观望。
这样保证与旧行为可对照、可一键回退。纯标准库、零网络。
"""
import config

PASS, WARN, VETO = "pass", "warn", "veto"
# veto 级别高于 warn；汇总时取最高
_RANK = {PASS: 0, WARN: 1, VETO: 2}

# 第138轮 E5：动态黑名单（freqtrade pair-locking 精神）
# 品种连续 N 轮无有效价/流动性不足 → 自动禁入（veto"黑名单"），连续 K 轮恢复后自动解禁。
# 状态显式传入（dict：sym -> {fail_streak, ok_streak}），纯函数可测，不进全局可变单例。
_BLACKLIST_REASON = "品种进入动态黑名单（连续%d轮无有效行情/流动性不足），暂不给出开仓信号"


def _blacklist_default():
    return {}


def record_streak(state, sym, ok, fail_threshold=None, recover_threshold=None):
    """更新品种连续失效/恢复计数。返回 (blocked?, reason?)。

    - ok=True 表示本轮有有效行情（价格>0 且量>=下限）：fail_streak 清零、ok_streak+1，
      连续 recover_threshold 轮恢复则解禁（从 state 移除）；
    - ok=False 表示本轮无有效行情：fail_streak+1、ok_streak 清零，连续 fail_threshold 轮则封禁。
    任一计数达到阈值即生效；返回 (是否被封禁, 原因或 None)。"""
    fail_threshold = fail_threshold if fail_threshold is not None         else getattr(config, "RISK_GATE_BLACKLIST_FAIL_ROUNDS", 3)
    recover_threshold = recover_threshold if recover_threshold is not None         else getattr(config, "RISK_GATE_BLACKLIST_RECOVER_ROUNDS", 5)
    st = state.setdefault(sym, {"fail_streak": 0, "ok_streak": 0, "blocked": False})
    if ok:
        st["fail_streak"] = 0
        if st.get("blocked"):
            # 封禁中的品种：需连续 recover_threshold 轮有效行情才解禁（ok_streak 累积）
            st["ok_streak"] = st.get("ok_streak", 0) + 1
            if st["ok_streak"] >= recover_threshold:
                state.pop(sym, None)      # 连续恢复达阈值 → 解禁（整个状态移除）
                return False, None
            return True, _BLACKLIST_REASON % fail_threshold   # 仍在封禁中
        st["ok_streak"] = 0
        return False, None
    st["ok_streak"] = 0
    st["fail_streak"] = st.get("fail_streak", 0) + 1
    if st["fail_streak"] >= fail_threshold:
        st["blocked"] = True              # 触发封禁（标记独立于计数）
        return True, _BLACKLIST_REASON % fail_threshold
    return False, None


def is_blocked(state, sym):
    """品种当前是否在动态黑名单中（纯查询，不更新计数）。以显式 blocked 标记为准。"""
    st = state.get(sym)
    return bool(st and st.get("blocked"))


def evaluate(row):
    """对单条品种分析结果做独立风控复核。

    返回 {"level": pass/warn/veto, "veto": [...], "warn": [...], "reasons": [...]}。
    总开关关闭、或输入为空时返回 pass（不干预）。任何异常都退回 pass，绝不阻断主流程。
    """
    ok = {"level": PASS, "veto": [], "warn": [], "reasons": []}
    if not config.RISK_GATE_ENABLED or not isinstance(row, dict):
        return ok
    veto, warn = [], []
    try:
        score = float(row.get("score", 0.0) or 0.0)
        price = float(row.get("price", 0.0) or 0.0)
        chg = float(row.get("chg", 0.0) or 0.0)
        conf = row.get("conf")
        direction = 1 if score > 0 else (-1 if score < 0 else 0)

        # ---- 硬否决 veto（只在较极端情形触发，避免过度干预）----
        # 0) 动态黑名单（连续 N 轮无有效行情/流动性不足自动禁入）
        bl_state = row.get("_blacklist") or {}
        bl_sym = row.get("sym") or row.get("code") or ""
        if bl_sym and is_blocked(bl_state, bl_sym):
            veto.append(_BLACKLIST_REASON %
                        getattr(config, "RISK_GATE_BLACKLIST_FAIL_ROUNDS", 3))

        # 1) 无有效行情 / 流动性不足：没有可靠价格或成交量过低，信号无成交基础
        volume = float(row.get("volume", 0.0) or 0.0)
        if price <= 0:
            veto.append("无有效最新价，暂不给出开仓信号")
        elif 0 <= volume < config.RISK_GATE_MIN_VOLUME:
            veto.append("成交量仅%d手、流动性不足，滑点风险高" % int(volume))

        # 2) 强信号与当日涨跌严重背离：高分做多却大跌 / 高分做空却大涨，防追高摸顶
        if abs(score) >= config.SCORE_MID and direction != 0 and \
                direction * chg <= -config.RISK_GATE_DIVERGE_CHG:
            veto.append("强信号(%+.1f)与当日涨跌(%+.2f%%)严重背离，谨防追高/摸顶"
                        % (score, chg * 100))

        # 3) 波动率处历史极端区：ATR 止损极易被打穿
        hvp = row.get("hv_percentile")
        if hvp is not None and hvp >= config.RISK_GATE_HV_EXTREME:
            veto.append("HV20处历史%.0f%%分位极端波动区，止损易被打穿、建议暂缓" % (hvp * 100))

        # ---- 警示 warn（不否决，只提示降杠杆/多核对）----
        if hvp is not None and config.RISK_GATE_HV_HIGH <= hvp < config.RISK_GATE_HV_EXTREME:
            warn.append("HV20分位%.0f%%偏高，建议降低仓位、放宽并复核止损" % (hvp * 100))

        # 4) 信号方向与量仓资金方向相反（价涨资金撤 / 价跌资金撤）
        if config.RISK_GATE_FLOW_CONFLICT and direction != 0:
            flow = row.get("flow") or {}
            fs = flow.get("score")
            if fs is not None and abs(fs) > 0.05 and direction * fs < 0:
                warn.append("信号方向与量仓资金(%s,资金分%+.2f)相反，持续性存疑"
                            % (flow.get("pattern", "—"), fs))

        # 5) 临近交割 / 期权临近到期
        if config.RISK_GATE_NEAR_DELIVERY:
            near = [r for r in (row.get("risks") or []) if "交割" in r]
            if near:
                warn.append(near[0])
            mn = row.get("month_note") or ""
            if "临近到期" in mn:
                warn.append(mn)

        # 6) 强信号但置信度偏低
        if abs(score) >= config.SCORE_MID and conf is not None and conf <= 55:
            warn.append("强信号但置信度仅%s%%，证据不够充分，建议减半仓试探" % conf)

    except Exception:
        return ok  # 风控自身出错绝不阻断监控

    if veto:
        level = VETO
    elif warn:
        level = WARN
    else:
        level = PASS
    reasons = ["⛔" + x for x in veto] + ["⚠" + x for x in warn]
    return {"level": level, "veto": veto, "warn": warn, "reasons": reasons}


def apply_gate(row):
    """在 row 上写入风控判定；若开启自动降级且被 veto，则把信号降级为观望。

    返回同一个 row（原地补充 row["risk"]；自动降级时改写 label/advice 并保留原值于
    row["label_before_gate"]/row["advice_before_gate"]，便于对照与恢复）。
    """
    if not config.RISK_GATE_ENABLED:
        row["risk"] = {"level": PASS, "veto": [], "warn": [], "reasons": []}
        return row
    g = evaluate(row)
    row["risk"] = g
    if g["level"] == VETO and config.RISK_GATE_AUTO_DOWNGRADE:
        row.setdefault("label_before_gate", row.get("label"))
        row.setdefault("advice_before_gate", row.get("advice"))
        row["label"] = "暂缓"
        row["advice"] = "风控闸门否决：%s（原：%s）" % ("；".join(g["veto"]),
                                                   row.get("label_before_gate", ""))
    return row


def level_rank(level):
    return _RANK.get(level, 0)
