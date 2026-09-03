# -*- coding: utf-8 -*-
r"""G5④（第48轮）组合层单日浮亏熔断 circuit_breaker.py：与 risk_gate 的**单品种信号级** veto 正交，
本模块是**组合账户级**的日内回撤断路器——盯住"当日日初权益 → 当前权益"的浮亏（以及保证金风险度），
按阈值分级产出 normal/warn/halt/delever 决策。纯标准库、零网络、纯决策**不直接下单/不改持仓**，
是否真的拦截开仓由宿主（paper_broker）按动作模式决定。

三档（单日浮亏，损失为正）+ 一个第二触发源：
  - warn     浮亏≥warn 阈值：只标注/告警（建议层文字）；
  - halt     浮亏≥halt 阈值：停止开新仓（只允许平仓/减仓），即"冷静一下、不再加仓摊薄"；
  - delever  浮亏≥delever 阈值：在停开基础上给出建议减仓比例（建议层文字，**不自动砍仓**，自动减仓留后续）；
  - 保证金风险度 risk_degree≥risk_halt 同样抬到 halt（占用过高先停开）。

两条关键纪律：
  1. **当日粘性、日切重置**：级别只按"当日最深浮亏 peak_loss"定档，触发后当日不回落解锁（防阈值附近反复抖动），
     跨交易日（ts 日期前缀变化）自动重置、新的一天重新计基准；
  2. **动作模式默认 observe（只标注）**：observe 下 allow_open 恒为 True、宿主成交逐字节不变（等价旧版）；
     只有显式 paper_halt 模式才在 halt/delever 时真正拦截纸面层开新仓。真实账户永不自动操作。
"""

NORMAL, WARN, HALT, DELEVER = "normal", "warn", "halt", "delever"
_LEVEL_RANK = {NORMAL: 0, WARN: 1, HALT: 2, DELEVER: 3}

OBSERVE = "observe"          # 默认：只计算/标注，allow_open 恒 True
PAPER_HALT = "paper_halt"    # 纸面层：halt/delever 时停开新仓（平仓照常），不自动减仓
ACTION_MODES = (OBSERVE, PAPER_HALT)

# 委托动作里属于"开新仓/增加敞口"的类型（与 paper_broker _make_order 的 action 对齐）
OPEN_ACTIONS = ("open", "reverse_open")
CLOSE_ACTIONS = ("close", "reverse_close")

DEFAULT_WARN = 0.02
DEFAULT_HALT = 0.03
DEFAULT_DELEVER = 0.05
DEFAULT_RISK_HALT = 0.95     # 保证金风险度（占用/权益）≥此值抬到 halt
DEFAULT_DELEVER_RATIO = 0.5  # delever 档建议减仓到一半（仅文字建议）


def day_of(ts):
    """从 'YYYY-MM-DD HH:MM:SS' 取日期前缀；非法/空返回 None。"""
    if not ts:
        return None
    ts = str(ts)
    return ts[:10] if len(ts) >= 10 and ts[4] == "-" and ts[7] == "-" else None


def daily_loss_pct(day_open_equity, equity):
    """单日浮亏分数（损失为正）：(日初权益−当前权益)/日初权益；盈利时为负。基准非正返0。"""
    try:
        base = float(day_open_equity)
        cur = float(equity)
    except (TypeError, ValueError):
        return 0.0
    if base <= 0:
        return 0.0
    return (base - cur) / base


def classify_level(loss, thresholds):
    """按浮亏分数与 (warn, halt, delever) 阈值定档（纯函数，无粘性）。"""
    warn, halt, delever = thresholds["warn"], thresholds["halt"], thresholds["delever"]
    if loss >= delever:
        return DELEVER
    if loss >= halt:
        return HALT
    if loss >= warn:
        return WARN
    return NORMAL


def max_level(a, b):
    return a if _LEVEL_RANK[a] >= _LEVEL_RANK[b] else b


def filter_orders(orders, allow_open):
    """按是否允许开新仓过滤委托：allow_open=True 原样返回；False 时剔除开新仓腿、保留平仓腿。

    reverse（先平后开）被拆成 reverse_close + reverse_open 两条腿，停开时只保留 reverse_close。
    纯函数、不改入参元素，返回新列表。
    """
    if allow_open:
        return list(orders or [])
    kept = []
    for o in orders or []:
        action = (o or {}).get("action")
        if action in OPEN_ACTIONS:
            continue
        kept.append(o)
    return kept


class CircuitBreaker:
    """组合层日内熔断状态机。阈值/动作模式构造时注入（不 import config，便于零环境自测）。"""

    def __init__(self, *, action_mode=OBSERVE, warn=DEFAULT_WARN, halt=DEFAULT_HALT,
                 delever=DEFAULT_DELEVER, risk_halt=DEFAULT_RISK_HALT,
                 delever_ratio=DEFAULT_DELEVER_RATIO):
        if action_mode not in ACTION_MODES:
            raise ValueError("未知熔断动作模式 %r（可选 %s）" % (action_mode, ACTION_MODES))
        if not (0 < warn <= halt <= delever):
            raise ValueError("熔断阈值须满足 0<warn<=halt<=delever")
        self.action_mode = action_mode
        self.thresholds = {"warn": warn, "halt": halt, "delever": delever}
        self.risk_halt = risk_halt
        self.delever_ratio = delever_ratio
        self.day = None
        self.day_open_equity = None
        self.peak_loss = 0.0
        self.level = NORMAL
        self.events = []          # 当日升档事件 [(ts, new_level, loss)]

    # ---------------- 状态更新 ----------------
    def _reset_day(self, day, equity):
        self.day = day
        self.day_open_equity = equity
        self.peak_loss = 0.0
        self.level = NORMAL
        self.events = []

    def update(self, ts, equity, risk_degree=None, n_positions=None):
        """喂入本轮权益快照，返回 decision dict。跨日自动重置；当日级别只升不降（粘性）。"""
        day = day_of(ts)
        if day is None:
            return self.decision(ts, equity, 0.0, risk_degree, n_positions, day_changed=False)
        if day != self.day:
            self._reset_day(day, equity)
            day_changed = True
        else:
            day_changed = False
        loss = daily_loss_pct(self.day_open_equity, equity)
        # 当日最深浮亏（粘性峰值）
        self.peak_loss = max(self.peak_loss, loss)
        loss_level = classify_level(self.peak_loss, self.thresholds)
        level = loss_level
        # 第二触发源：保证金风险度超限抬到 halt（不直接到 delever）
        risk_trigger = False
        if risk_degree is not None:
            try:
                if float(risk_degree) >= self.risk_halt:
                    risk_trigger = True
                    level = max_level(level, HALT)
            except (TypeError, ValueError):
                risk_trigger = False
        # 粘性：不低于此前当日级别
        level = max_level(level, self.level)
        if _LEVEL_RANK[level] > _LEVEL_RANK[self.level]:
            self.events.append((str(ts), level, self.peak_loss))
        self.level = level
        return self.decision(ts, equity, loss, risk_degree, n_positions,
                             day_changed=day_changed, risk_trigger=risk_trigger)

    def decision(self, ts, equity, loss, risk_degree, n_positions, *,
                 day_changed=False, risk_trigger=False):
        """由当前 level + 动作模式组装决策（不改变状态）。"""
        allow_open = True
        if self.action_mode == PAPER_HALT and _LEVEL_RANK[self.level] >= _LEVEL_RANK[HALT]:
            allow_open = False
        msgs = []
        warn, halt, delever = self.thresholds["warn"], self.thresholds["halt"], self.thresholds["delever"]
        if self.level == WARN:
            msgs.append("组合当日浮亏%.2f%%达预警线%.0f%%，提示降杠杆、多核对，不拦截开仓"
                        % (self.peak_loss * 100, warn * 100))
        elif self.level == HALT:
            src = "（含保证金风险度%.0f%%触发）" % (risk_degree * 100) if risk_trigger else ""
            if self.action_mode == PAPER_HALT:
                msgs.append("组合当日浮亏%.2f%%达停开线%.0f%%%s，已停止开新仓、只允许平仓（当日粘性，日切解除）"
                            % (self.peak_loss * 100, halt * 100, src))
            else:
                msgs.append("组合当日浮亏%.2f%%达停开线%.0f%%%s（observe只标注：若切 paper_halt 将停开新仓）"
                            % (self.peak_loss * 100, halt * 100, src))
        elif self.level == DELEVER:
            msgs.append("组合当日浮亏%.2f%%达减仓线%.0f%%，建议主动减仓约%.0f%%（仅建议、不自动砍仓），%s"
                        % (self.peak_loss * 100, delever * 100, self.delever_ratio * 100,
                           "已停开新仓" if not allow_open else "observe未拦截"))
        return {
            "ts": str(ts) if ts is not None else None, "day": self.day, "day_changed": day_changed,
            "level": self.level, "action_mode": self.action_mode, "allow_open": allow_open,
            "daily_loss": loss, "peak_loss": self.peak_loss,
            "day_open_equity": self.day_open_equity, "equity": equity,
            "risk_degree": risk_degree, "risk_trigger": risk_trigger,
            "n_positions": n_positions, "suggest_reduce_ratio":
                (self.delever_ratio if self.level == DELEVER else 0.0),
            "messages": msgs,
        }

    def open_allowed(self):
        """当前是否允许开新仓：observe 恒 True；paper_halt 仅在 level 达 halt/delever 时 False。"""
        if self.action_mode != PAPER_HALT:
            return True
        return _LEVEL_RANK[self.level] < _LEVEL_RANK[HALT]

    # ---------------- 工厂/渲染 ----------------
    @classmethod
    def from_config(cls, cfg=None):
        """从 config 的 CIRCUIT_* 常量构造；cfg=None 时延迟 import config；缺项回退默认。"""
        if cfg is None:
            import config as cfg
        def g(name, default):
            return getattr(cfg, name, default)
        return cls(
            action_mode=g("CIRCUIT_ACTION", OBSERVE),
            warn=g("CIRCUIT_WARN_LOSS", DEFAULT_WARN),
            halt=g("CIRCUIT_HALT_LOSS", DEFAULT_HALT),
            delever=g("CIRCUIT_DELEVER_LOSS", DEFAULT_DELEVER),
            risk_halt=g("CIRCUIT_RISK_HALT", DEFAULT_RISK_HALT),
            delever_ratio=g("CIRCUIT_DELEVER_RATIO", DEFAULT_DELEVER_RATIO))

    def render(self, d=None):
        """一行人类可读状态（报告/日志用）。"""
        if d is None:
            return "熔断状态=%s/模式=%s" % (self.level, self.action_mode)
        if d["level"] == NORMAL:
            return "组合熔断 normal（当日浮亏%+.2f%%，允许开仓）" % (d["daily_loss"] * 100)
        return "组合熔断 %s｜%s｜当日峰值浮亏%.2f%%｜%s" % (
            d["level"], d["action_mode"], d["peak_loss"] * 100,
            "；".join(d["messages"]) if d["messages"] else "")


# =========================== 零网络/零DB 手算自测 ===========================
def selftest():
    # 1) day_of 解析与非法安全
    assert day_of("2026-09-03 10:15:00") == "2026-09-03"
    assert day_of("") is None and day_of("bad") is None and day_of(None) is None

    # 2) daily_loss_pct：损失为正、盈利为负、基准非正安全
    assert abs(daily_loss_pct(100.0, 97.0) - 0.03) < 1e-12
    assert abs(daily_loss_pct(100.0, 102.0) + 0.02) < 1e-12
    assert daily_loss_pct(0, 90) == 0.0 and daily_loss_pct(None, 90) == 0.0

    # 3) classify 三档边界
    th = {"warn": 0.02, "halt": 0.03, "delever": 0.05}
    assert classify_level(0.01, th) == NORMAL
    assert classify_level(0.02, th) == WARN and classify_level(0.029, th) == WARN
    assert classify_level(0.03, th) == HALT and classify_level(0.049, th) == HALT
    assert classify_level(0.05, th) == DELEVER

    # 4) 阈值非法报错、动作模式非法报错
    for bad in [dict(warn=0, halt=0.03, delever=0.05), dict(warn=0.04, halt=0.03, delever=0.05)]:
        try:
            CircuitBreaker(**bad)
            raise AssertionError("非法阈值应报错")
        except ValueError:
            pass
    try:
        CircuitBreaker(action_mode="nope")
        raise AssertionError("非法模式应报错")
    except ValueError:
        pass

    # 5) observe 默认：即便 delever 也 allow_open=True（等价旧版的核心不变量）
    cb = CircuitBreaker()
    d = cb.update("2026-09-03 09:30:00", 1_000_000)
    assert d["level"] == NORMAL and d["day_changed"] and d["allow_open"]
    cb.update("2026-09-03 10:00:00", 940_000)   # -6% → delever
    d = cb.update("2026-09-03 10:01:00", 940_000)
    assert d["level"] == DELEVER and d["allow_open"] is True and d["suggest_reduce_ratio"] == 0.5
    assert cb.events[-1][1] == DELEVER

    # 6) 当日粘性：浮亏先到 halt 后反弹回 warn 区，级别仍停在 halt（不抖动解锁）
    cb2 = CircuitBreaker(action_mode=PAPER_HALT)
    cb2.update("2026-09-03 09:30:00", 1_000_000)
    cb2.update("2026-09-03 10:00:00", 965_000)    # -3.5% halt
    assert cb2.level == HALT
    d = cb2.update("2026-09-03 11:00:00", 990_000)  # 反弹到 -1%
    assert d["level"] == HALT and d["allow_open"] is False and d["daily_loss"] == 0.01
    assert cb2.peak_loss >= 0.035 - 1e-9

    # 7) 日切重置：次日重新 normal、allow_open 恢复
    d = cb2.update("2026-09-04 09:30:00", 990_000)
    assert d["day_changed"] and d["level"] == NORMAL and d["allow_open"] and cb2.events == []

    # 8) paper_halt 逐档 allow_open：warn 仍可开、halt/delever 停开
    cb3 = CircuitBreaker(action_mode=PAPER_HALT)
    cb3.update("2026-09-03 09:30:00", 1_000_000)
    assert cb3.update("2026-09-03 09:45:00", 985_000)["allow_open"] is True   # -1.5% normal
    assert cb3.update("2026-09-03 10:00:00", 978_000)["level"] == WARN
    assert cb3.update("2026-09-03 10:01:00", 978_000)["allow_open"] is True   # warn 不停开
    assert cb3.update("2026-09-03 10:30:00", 968_000)["allow_open"] is False  # halt 停开

    # 9) 第二触发源：风险度超限抬到 halt（即使浮亏不大）
    cb4 = CircuitBreaker(action_mode=PAPER_HALT)
    cb4.update("2026-09-03 09:30:00", 1_000_000)
    d = cb4.update("2026-09-03 10:00:00", 995_000, risk_degree=0.97)
    assert d["level"] == HALT and d["risk_trigger"] and d["allow_open"] is False
    # 风险度非法不崩
    d2 = cb4.update("2026-09-03 10:05:00", 995_000, risk_degree="x")
    assert d2["risk_trigger"] is False

    # 10) filter_orders：停开时剔 open/reverse_open、保留 close/reverse_close
    orders = [{"action": "open", "sym": "A"}, {"action": "reverse_close", "sym": "B"},
              {"action": "reverse_open", "sym": "B"}, {"action": "close", "sym": "C"}]
    kept = filter_orders(orders, False)
    assert [o["action"] for o in kept] == ["reverse_close", "close"]
    assert [o["action"] for o in filter_orders(orders, True)] == ["open", "reverse_close",
                                                                  "reverse_open", "close"]
    assert filter_orders(None, False) == [] and filter_orders([], True) == []
    # 不改原列表
    assert len(orders) == 4

    # 11) render 不崩且含级别；from_config 在无 config 环境下回退默认（传 stub）
    class _Stub:
        CIRCUIT_ACTION = PAPER_HALT
    cbc = CircuitBreaker.from_config(_Stub())
    assert cbc.action_mode == PAPER_HALT
    cbc.update("2026-09-03 09:30:00", 1_000_000)
    cbc.update("2026-09-03 10:00:00", 960_000)
    txt = cbc.render()
    assert "halt" in txt

    print("circuit_breaker selftest ALL PASS（日期解析/浮亏口径/三档边界/参数校验/observe恒可开/"
          "当日粘性/日切重置/paper_halt逐档/风险度第二触发/委托过滤/渲染与工厂 共11组）")
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
