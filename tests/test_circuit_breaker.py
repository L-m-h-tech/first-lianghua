# -*- coding: utf-8 -*-
"""G5④（第48轮）组合层单日浮亏熔断：纯函数/状态机零网络测试 + 与 PaperBroker 的默认旁路/停开集成。"""
import pytest

import config
import circuit_breaker as cb_mod
from circuit_breaker import (CircuitBreaker, NORMAL, WARN, HALT, DELEVER, OBSERVE, PAPER_HALT,
                             day_of, daily_loss_pct, classify_level, max_level, filter_orders)
import paper_broker as pb


# ---------- 纯函数 ----------
@pytest.mark.parametrize("ts,expect", [("2026-09-03 10:00:00", "2026-09-03"),
                                       ("2026-09-03", "2026-09-03"), ("", None),
                                       ("xx", None), (None, None)])
def test_day_of(ts, expect):
    assert day_of(ts) == expect


def test_daily_loss():
    assert abs(daily_loss_pct(100, 97) - 0.03) < 1e-12
    assert daily_loss_pct(100, 102) < 0                 # 盈利为负
    assert daily_loss_pct(0, 1) == 0 and daily_loss_pct("x", 1) == 0


def test_classify_boundaries():
    th = {"warn": 0.02, "halt": 0.03, "delever": 0.05}
    assert classify_level(0.019, th) == NORMAL
    assert classify_level(0.02, th) == WARN
    assert classify_level(0.03, th) == HALT
    assert classify_level(0.05, th) == DELEVER


def test_max_level():
    assert max_level(WARN, HALT) == HALT
    assert max_level(DELEVER, NORMAL) == DELEVER


@pytest.mark.parametrize("orders,allow,kept", [
    ([{"action": "open"}, {"action": "close"}], True, ["open", "close"]),
    ([{"action": "open"}, {"action": "reverse_close"}, {"action": "reverse_open"},
      {"action": "close"}], False, ["reverse_close", "close"]),
    (None, False, []),
])
def test_filter_orders(orders, allow, kept):
    out = filter_orders(orders, allow)
    assert [o.get("action") for o in out] == kept


def test_invalid_args():
    with pytest.raises(ValueError):
        CircuitBreaker(action_mode="bad")
    with pytest.raises(ValueError):
        CircuitBreaker(warn=0.04, halt=0.03, delever=0.05)


# ---------- 状态机 ----------
def test_observe_always_allows_open_even_at_delever():
    cb = CircuitBreaker(action_mode=OBSERVE)
    cb.update("2026-09-03 09:30:00", 1e6)
    d = cb.update("2026-09-03 10:00:00", 930_000)        # -7% delever
    assert d["level"] == DELEVER and cb.open_allowed() is True
    assert d["suggest_reduce_ratio"] == 0.5 and d["messages"]


def test_sticky_intraday_and_day_reset():
    cb = CircuitBreaker(action_mode=PAPER_HALT)
    cb.update("2026-09-03 09:30:00", 1e6)
    cb.update("2026-09-03 10:00:00", 965_000)            # halt
    assert cb.level == HALT and cb.open_allowed() is False
    d = cb.update("2026-09-03 11:00:00", 995_000)        # 反弹，粘性不解除
    assert d["level"] == HALT and cb.open_allowed() is False
    d2 = cb.update("2026-09-04 09:30:00", 995_000)       # 日切重置
    assert d2["level"] == NORMAL and cb.open_allowed() is True and cb.events == []


def test_warn_still_allows_open():
    cb = CircuitBreaker(action_mode=PAPER_HALT)
    cb.update("2026-09-03 09:30:00", 1e6)
    cb.update("2026-09-03 10:00:00", 979_000)            # -2.1% warn
    assert cb.level == WARN and cb.open_allowed() is True


def test_risk_degree_second_trigger():
    cb = CircuitBreaker(action_mode=PAPER_HALT, risk_halt=0.95)
    cb.update("2026-09-03 09:30:00", 1e6)
    d = cb.update("2026-09-03 10:00:00", 999_000, risk_degree=0.97)
    assert d["level"] == HALT and d["risk_trigger"] is True
    d2 = cb.update("2026-09-03 10:05:00", 999_000, risk_degree="bad")
    assert d2["risk_trigger"] is False


def test_from_config_stub():
    class Stub:
        CIRCUIT_ACTION = PAPER_HALT
        CIRCUIT_WARN_LOSS = 0.01
        CIRCUIT_HALT_LOSS = 0.02
        CIRCUIT_DELEVER_LOSS = 0.04
        CIRCUIT_RISK_HALT = 0.9
        CIRCUIT_DELEVER_RATIO = 0.4
    c = CircuitBreaker.from_config(Stub())
    assert c.action_mode == PAPER_HALT and c.thresholds["warn"] == 0.01


# ---------- 与 PaperBroker 集成 ----------
@pytest.fixture
def loose_config(monkeypatch):
    monkeypatch.setattr(config, "PAPER_PER_SYMBOL", 0.05)
    monkeypatch.setattr(config, "PAPER_MAX_SYMBOL_WEIGHT", 1.0)
    monkeypatch.setattr(config, "PAPER_MAX_SECTOR_WEIGHT", 1.0)
    monkeypatch.setattr(config, "PAPER_MAX_CONCURRENT", 64)


def _broker(circuit):
    return pb.PaperBroker(db=None, restore=False, fill_mode="close", equity0=10_000_000,
                          slip_rate=0.0, circuit=circuit)


def test_paper_halt_blocks_new_open_but_allows_close(loose_config):
    cb = CircuitBreaker(action_mode=PAPER_HALT, warn=0.02, halt=0.03, delever=0.05)
    broker = _broker(cb)
    # 第一轮：正常开 RB 多
    r1 = broker.on_cycle("2026-09-03 09:05:00", [pb._row("RB", "螺纹", "黑色", 5.0, 3000.0)])
    assert any(t.get("side") == "open" for t in r1["trades"])
    # 人为把断路器推到 halt（模拟当日 -4% 浮亏）
    cb.update("2026-09-03 10:00:00", 9_600_000)
    assert cb.level == HALT and cb.open_allowed() is False
    # 第二轮：新品种 HC 强多应被拦；RB 转强空=反手只留平仓腿（平掉多仓、不反向开空）
    rows = [pb._row("HC", "热卷", "黑色", 5.0, 3000.0),
            pb._row("RB", "螺纹", "黑色", -5.0, 3000.0)]
    r2 = broker.on_cycle("2026-09-03 10:05:00", rows)
    sides = [t.get("side") for t in r2["trades"]]
    assert "open" not in sides                              # 停开：无任何开仓成交
    assert "close" in sides                                 # 反手的平仓腿保留
    assert not any(t.get("sym") == "HC" for t in r2["trades"])     # 新仓被拦
    assert r2["circuit"]["level"] == HALT and r2["circuit"]["allow_open"] is False


def test_default_observe_broker_never_blocks(loose_config):
    # 默认 circuit=None（config.CIRCUIT_ACTION=observe）：即便浮亏也照常开新仓
    broker = _broker(None)
    assert broker.breaker is None
    broker.on_cycle("2026-09-03 09:05:00", [pb._row("RB", "螺纹", "黑色", 5.0, 3000.0)])
    r2 = broker.on_cycle("2026-09-03 10:05:00", [pb._row("HC", "热卷", "黑色", 5.0, 3000.0)])
    assert any(t.get("sym") == "HC" and t.get("side") == "open" for t in r2["trades"])
    assert r2["circuit"] is None
