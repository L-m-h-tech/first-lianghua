# -*- coding: utf-8 -*-
"""量仓资金快照识别回归（第7轮，增/减仓×涨/跌、跨日重置、最少两快照）。"""
from flow_tracker import FlowTracker


def _q(latest, volume, oi, date="2026-09-01"):
    return {"latest": latest, "volume": volume, "open_interest": oi, "date": date}


def test_needs_two_snapshots():
    ft = FlowTracker()
    assert ft.update({"RB": _q(3500, 100, 10000)}, now_ts=1000) == {}


def test_long_build_up():
    ft = FlowTracker()
    ft.update({"RB": _q(3500, 100, 10000)}, now_ts=1000)
    out = ft.update({"RB": _q(3535, 200, 10500)}, now_ts=1060)   # 价涨、增仓
    f = out["RB"]
    assert f["direction"] == 1 and f["oi_chg"] == 500 and f["score"] > 0
    assert f["pattern"] == "增仓上行"


def test_short_build_up():
    ft = FlowTracker()
    ft.update({"RB": _q(3500, 100, 10000)}, now_ts=1000)
    out = ft.update({"RB": _q(3465, 200, 10500)}, now_ts=1060)   # 价跌、增仓
    f = out["RB"]
    assert f["direction"] == -1 and f["score"] < 0 and f["pattern"] == "增仓下行"


def test_long_cover_rebound():
    ft = FlowTracker()
    ft.update({"RB": _q(3500, 100, 10000)}, now_ts=1000)
    out = ft.update({"RB": _q(3535, 200, 9500)}, now_ts=1060)    # 价涨、减仓=空头回补
    assert "减仓上行" in out["RB"]["pattern"]


def test_volume_reset_within_day():
    # 同日日累计量异常回退（小于上一轮）-> 判为重置、volume_ratio=1，不崩
    ft = FlowTracker()
    ft.update({"RB": _q(3500, 5000, 10000)}, now_ts=1000)
    out = ft.update({"RB": _q(3510, 100, 10100)}, now_ts=1060)
    f = out["RB"]
    assert f["volume_reset"] is True and f["volume_ratio"] == 1.0


def test_new_trade_day_rebuilds_baseline():
    # 跨交易日清空重建：换日后第一轮只有1个快照，不产出 flow（不把隔夜当放量）
    ft = FlowTracker()
    ft.update({"RB": _q(3500, 5000, 10000, "2026-09-01")}, now_ts=1000)
    assert ft.update({"RB": _q(3510, 100, 10000, "2026-09-02")}, now_ts=2000) == {}


def test_invalid_price_skipped():
    ft = FlowTracker()
    ft.update({"RB": _q(3500, 100, 10000)}, now_ts=1000)
    assert ft.update({"RB": _q(0, 200, 10500)}, now_ts=1060) == {}
