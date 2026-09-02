# -*- coding: utf-8 -*-
"""独立风控闸门回归（第18轮 A2，pass/warn/veto；默认只标注不改分）。"""
import config
import risk_gate
from risk_gate import PASS, WARN, VETO


def base_row(**kw):
    row = {"score": 3.0, "price": 3500.0, "chg": 0.001, "volume": 100000,
           "conf": 80, "hv_percentile": 0.5, "flow": {}, "risks": [],
           "month_note": "", "label": "偏多", "advice": "x"}
    row.update(kw)
    return row


def test_disabled_and_bad_input_pass(monkeypatch):
    monkeypatch.setattr(config, "RISK_GATE_ENABLED", False)
    assert risk_gate.evaluate(base_row(price=-1))["level"] == PASS
    monkeypatch.setattr(config, "RISK_GATE_ENABLED", True)
    assert risk_gate.evaluate("not-a-dict")["level"] == PASS


def test_healthy_pass():
    assert risk_gate.evaluate(base_row())["level"] == PASS


def test_no_price_veto():
    g = risk_gate.evaluate(base_row(price=0))
    assert g["level"] == VETO and any("无有效最新价" in x for x in g["veto"])


def test_low_liquidity_veto():
    g = risk_gate.evaluate(base_row(volume=100))
    assert g["level"] == VETO and any("流动性不足" in x for x in g["veto"])


def test_strong_signal_divergence_veto():
    # 强做多(score>=6.5)却当日大跌超2% -> 防追高摸顶 veto
    g = risk_gate.evaluate(base_row(score=7.0, chg=-0.03))
    assert g["level"] == VETO and any("背离" in x for x in g["veto"])
    # 反向背离不触发（强做空却大涨）
    g2 = risk_gate.evaluate(base_row(score=-7.0, chg=0.03))
    assert g2["level"] == VETO
    # 仅小幅反向不触发 veto
    assert risk_gate.evaluate(base_row(score=7.0, chg=-0.005))["level"] != VETO


def test_hv_extreme_veto_and_high_warn():
    assert risk_gate.evaluate(base_row(hv_percentile=0.97))["level"] == VETO
    g = risk_gate.evaluate(base_row(hv_percentile=0.85))
    assert g["level"] == WARN and any("HV20" in x for x in g["warn"])


def test_flow_conflict_warn():
    g = risk_gate.evaluate(base_row(score=3.0, flow={"score": -0.6, "pattern": "增仓下行"}))
    assert g["level"] == WARN and any("量仓资金" in x for x in g["warn"])
    # 同向不告警
    assert risk_gate.evaluate(base_row(flow={"score": 0.6}))["level"] == PASS


def test_near_delivery_and_low_conf_warn():
    g = risk_gate.evaluate(base_row(risks=["主力临近交割"]))
    assert any("交割" in x for x in g["warn"])
    g2 = risk_gate.evaluate(base_row(score=7.0, conf=50))
    assert g2["level"] == WARN and any("置信度" in x for x in g2["warn"])


def test_veto_outranks_warn():
    g = risk_gate.evaluate(base_row(price=0, hv_percentile=0.85))
    assert g["level"] == VETO and len(g["warn"]) >= 1 and len(g["veto"]) >= 1


def test_apply_gate_default_no_downgrade(monkeypatch):
    monkeypatch.setattr(config, "RISK_GATE_AUTO_DOWNGRADE", False)
    row = risk_gate.apply_gate(base_row(price=0))
    assert row["risk"]["level"] == VETO
    assert row["label"] == "偏多"                       # 默认不改标签
    assert "label_before_gate" not in row


def test_apply_gate_auto_downgrade(monkeypatch):
    monkeypatch.setattr(config, "RISK_GATE_AUTO_DOWNGRADE", True)
    row = risk_gate.apply_gate(base_row(price=0))
    assert row["label"] == "暂缓"
    assert row["label_before_gate"] == "偏多"
    # pass 时不降级
    ok = risk_gate.apply_gate(base_row())
    assert ok["label"] == "偏多"


def test_level_rank():
    assert risk_gate.level_rank(VETO) > risk_gate.level_rank(WARN) > risk_gate.level_rank(PASS)
