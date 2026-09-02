# -*- coding: utf-8 -*-
"""G11 数据源熔断降级链零网络回归（注入假时钟，确定性状态迁移）。"""
import pytest

import data_router as dr
from data_router import SourceHealth, DataRouter, AllSourcesFailed, HealthRegistry, CLOSED, OPEN, HALF_OPEN


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


# ---------- SourceHealth 状态机 ----------
def test_closed_until_threshold():
    c = Clock()
    h = SourceHealth("s", fails_threshold=3, cooldown_sec=100, clock=c)
    for _ in range(2):
        assert h.allow()
        h.record_failure()
        assert h.state == CLOSED
    h.record_failure()                 # 第3次
    assert h.state == OPEN and h.trips == 1


def test_open_denies_during_cooldown_then_half_open_one_trial():
    c = Clock()
    h = SourceHealth("s", fails_threshold=1, cooldown_sec=100, clock=c)
    h.record_failure()
    assert h.state == OPEN
    assert h.allow() is False         # 冷却中拒绝
    c.advance(100)
    assert h.allow() is True          # 到期放一次试探 -> HALF_OPEN
    assert h.state == HALF_OPEN
    assert h.allow() is False         # 试探结果未回前不再放第二个


def test_half_open_success_closes():
    c = Clock()
    h = SourceHealth("s", fails_threshold=1, cooldown_sec=10, clock=c)
    h.record_failure()
    c.advance(10)
    h.allow()
    h.record_success()
    assert h.state == CLOSED and h.consecutive_fails == 0
    assert h.allow() is True


def test_half_open_failure_reopens():
    c = Clock()
    h = SourceHealth("s", fails_threshold=1, cooldown_sec=10, clock=c)
    h.record_failure()
    c.advance(10)
    h.allow()
    h.record_failure()
    assert h.state == OPEN and h.trips == 2
    assert h.allow() is False


def test_success_resets_consecutive():
    c = Clock()
    h = SourceHealth("s", fails_threshold=3, cooldown_sec=10, clock=c)
    h.record_failure(); h.record_failure(); h.record_success()
    h.record_failure(); h.record_failure()
    assert h.state == CLOSED          # 成功清零，未达阈值
    assert (h.success, h.fail) == (1, 4)


def test_disabled_always_allows():
    c = Clock()
    h = SourceHealth("s", fails_threshold=1, cooldown_sec=10, clock=c, enabled=False)
    for _ in range(10):
        h.record_failure()
    assert h.allow() is True and h.state == CLOSED


def test_availability_and_snapshot():
    c = Clock()
    h = SourceHealth("s", fails_threshold=5, cooldown_sec=10, clock=c)
    assert h.availability() == 1.0
    h.record_success(); h.record_failure()
    assert h.availability() == 0.5
    snap = h.snapshot()
    assert set(["name", "state", "total", "success", "fail", "trips",
                "availability", "cooldown_remaining"]) <= set(snap)


# ---------- DataRouter 有序主备 ----------
def test_router_primary_success():
    c = Clock()
    r = DataRouter([("a", lambda: "A"), ("b", lambda: "B")],
                   fails_threshold=2, cooldown_sec=10, clock=c)
    res = r.request()
    assert res.value == "A" and res.source == "a" and res.tried == ["a"]


def test_router_failover_to_secondary():
    c = Clock()

    def bad():
        raise RuntimeError("boom")

    r = DataRouter([("a", bad), ("b", lambda: "B")],
                   fails_threshold=2, cooldown_sec=10, clock=c)
    res = r.request()
    assert res.value == "B" and res.source == "b" and res.tried == ["a", "b"]


def test_router_validator_rejects_dirty_then_failover():
    c = Clock()
    r = DataRouter([("a", lambda: []), ("b", lambda: [1, 2])],
                   fails_threshold=2, cooldown_sec=10, clock=c,
                   validator=lambda x: len(x) > 0)
    res = r.request()
    assert res.value == [1, 2] and res.source == "b"
    assert r.health["a"].fail == 1


def test_router_circuit_skips_dead_source():
    c = Clock()
    calls = {"a": 0, "b": 0}

    def a():
        calls["a"] += 1
        raise RuntimeError("x")

    def b():
        calls["b"] += 1
        return "B"

    r = DataRouter([("a", a), ("b", b)], fails_threshold=1, cooldown_sec=100, clock=c)
    assert r.request().value == "B"     # a 失败1次即熔断
    assert r.health["a"].state == OPEN
    r.request()                          # 第二次：a 熔断被跳过，直接 b
    assert calls["a"] == 1 and calls["b"] == 2
    snap = r.snapshots()
    assert snap["a"]["state"] == OPEN and snap["a"]["skipped"] == 1


def test_router_all_failed_raises():
    c = Clock()
    r = DataRouter([("a", lambda: (_ for _ in ()).throw(RuntimeError("x"))),
                    ("b", lambda: (_ for _ in ()).throw(ValueError("y")))],
                   fails_threshold=5, cooldown_sec=10, clock=c)
    with pytest.raises(AllSourcesFailed) as ei:
        r.request()
    assert set(ei.value.errors) == {"a", "b"}


# ---------- HealthRegistry ----------
def test_registry_record_and_open_sources():
    reg = HealthRegistry(fails_threshold=2, cooldown_sec=10)
    reg.record("x", True); reg.record("x", False)
    assert reg.snapshot("x")["state"] == CLOSED
    reg.record("x", False)
    assert reg.snapshot("x")["state"] == OPEN
    assert [s["name"] for s in reg.open_sources()] == ["x"]
    assert reg.names() == ["x"]
    reg.reset()
    assert reg.names() == []


def test_registry_distinct_sources():
    reg = HealthRegistry()
    reg.record("a", True)
    reg.record("b", False)
    snaps = reg.snapshots()
    assert set(snaps) == {"a", "b"} and snaps["a"]["success"] == 1
