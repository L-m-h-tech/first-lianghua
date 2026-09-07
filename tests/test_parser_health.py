# -*- coding: utf-8 -*-
"""第94轮 A1 parser_health 探针/告警测试：零网络、确定性。"""
import parser_health as ph


class _FakeState:
    alerts_emitted = []

    class alerts:
        @staticmethod
        def emit(*a, **kw):
            _FakeState.alerts_emitted.append((a, kw))


def _reset():
    ph._REGISTRY = ph._Registry()


def test_record_and_classify_pass():
    _reset()
    ph.record("s1", True, 50)
    ph.record("s1", True, 50)
    assert ph.check_alert() == []


def test_fail_streak_trigger():
    _reset()
    for _ in range(5):
        ph.record("s2", False, 0)
    alerts = ph.check_alert()
    assert len(alerts) == 1 and alerts[0]["reason"] == "fail_streak" and "5" in alerts[0]["detail"]


def test_alert_throttle():
    _reset()
    for _ in range(5):
        ph.record("throttle", False, 0)
    first = ph.check_alert()
    assert first
    ph.mark_alerted("throttle")
    second = ph.check_alert()
    assert second == []            # 节流期内不再重复


def test_structure_change_trigger():
    _reset()
    for _ in range(10):
        ph.record("src_s", True, 100)
    ph.record("src_s", True, 10)
    alerts = ph.check_alert()
    assert any(a["reason"] == "structure_change" and "100" in a["detail"] for a in alerts)


def test_emit_writes_txt(tmp_path, monkeypatch):
    _reset()
    monkeypatch.setattr(ph, "REPORT_TXT", str(tmp_path / "parser_health.txt"))
    for _ in range(5):
        ph.record("emit", False, 0)
    ph.render_reports()
    import os
    assert os.path.exists(str(tmp_path / "parser_health.txt"))


def test_selftest():
    assert ph.selftest() == 0
