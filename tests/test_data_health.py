"""G6 数据质量监控零网络回归。"""

from data_health import HealthMonitor, evaluate_quotes, format_health_block


def _q(latest=100.0, chg=0.01, date="2026-09-02"):
    return {"latest": latest, "chg_pct": chg, "date": date}


# ---------- evaluate_quotes ----------
def test_eval_missing_present():
    quotes = {"RB0": _q(), "MA0": _q()}
    ev = evaluate_quotes(quotes, ["RB0", "MA0", "CU0"], today_str="2026-09-02")
    assert ev["n_expected"] == 3 and ev["n_present"] == 2
    assert ev["missing"] == ["CU0"] and ev["stale"] == [] and ev["jump"] == []


def test_eval_stale_invalid_price():
    ev = evaluate_quotes({"RB0": _q(latest=0)}, ["RB0"], today_str="2026-09-02")
    assert ev["stale"] == ["RB0"]


def test_eval_stale_date_only_in_session():
    # 交易时段日期不符 -> 陈旧
    ev = evaluate_quotes(
        {"RB0": _q(date="2026-09-01")}, ["RB0"], today_str="2026-09-02", session_active=True
    )
    assert ev["stale"] == ["RB0"]
    # 非交易时段不按日期判陈旧
    ev2 = evaluate_quotes(
        {"RB0": _q(date="2026-09-01")}, ["RB0"], today_str="2026-09-02", session_active=False
    )
    assert ev2["stale"] == []


def test_eval_jump_threshold():
    ev = evaluate_quotes(
        {"RB0": _q(chg=0.31), "MA0": _q(chg=-0.40), "CU0": _q(chg=0.1)},
        ["RB0", "MA0", "CU0"],
        today_str="2026-09-02",
        jump_pct=0.30,
    )
    assert sorted(ev["jump"]) == ["MA0", "RB0"]


def test_eval_empty_expected():
    ev = evaluate_quotes({}, [])
    assert ev["n_expected"] == 0 and ev["missing"] == []


# ---------- HealthMonitor 跨轮 ----------
def _snap(total, success, fail, state="closed"):
    return {
        "name": "s",
        "state": state,
        "total": total,
        "success": success,
        "fail": fail,
        "consecutive_fails": fail,
        "skipped": 0,
        "trips": 0,
        "availability": (success / total) if total else 1.0,
        "cooldown_remaining": 0.0,
    }


def test_miss_streak_alert_after_n_cycles():
    hm = HealthMonitor()
    exp = ["A", "B"]
    # 第1轮 B 缺失（未达阈值2，不告警）
    r1 = hm.observe_cycle("t1", {"A": _q()}, exp, {}, today_str="2026-09-02")
    assert r1["alert_codes"] == []
    # 第2轮 B 仍缺失 -> 告警
    r2 = hm.observe_cycle("t2", {"A": _q()}, exp, {}, today_str="2026-09-02")
    assert r2["alert_codes"] == ["B"]
    # 第3轮 B 恢复 -> streak 清零，不告警
    r3 = hm.observe_cycle("t3", {"A": _q(), "B": _q()}, exp, {}, today_str="2026-09-02")
    assert r3["alert_codes"] == [] and hm.miss_streak["B"] == 0


def test_source_fail_streak_and_deltas():
    hm = HealthMonitor()
    # 第1轮：源 s 请求3次全失败（累计 total3/ok0/fail3）
    r1 = hm.observe_cycle("t1", {"A": _q()}, ["A"], {"s": _snap(3, 0, 3)}, today_str="2026-09-02")
    row = [x for x in r1["rows"] if x["source"] == "s"][0]
    assert row["req"] == 3 and row["ok"] == 0 and row["fail"] == 3
    assert r1["alert_sources"] == []  # 仅1轮
    # 第2轮：增量又失败2次（累计5/0/5），连续2轮全失败 -> 告警
    r2 = hm.observe_cycle(
        "t2", {"A": _q()}, ["A"], {"s": _snap(5, 0, 5, state="open")}, today_str="2026-09-02"
    )
    row2 = [x for x in r2["rows"] if x["source"] == "s"][0]
    assert row2["req"] == 2 and r2["alert_sources"] == ["s"]
    assert r2["open_sources"] == ["s"]


def test_source_recovery_resets_streak():
    hm = HealthMonitor()
    hm.observe_cycle("t1", {"A": _q()}, ["A"], {"s": _snap(2, 0, 2)}, today_str="d")
    r = hm.observe_cycle("t2", {"A": _q()}, ["A"], {"s": _snap(4, 2, 2)}, today_str="d")
    # 本轮有成功 -> streak 清零，不告警
    assert r["alert_sources"] == []


def test_quotes_aggregate_row():
    hm = HealthMonitor()
    r = hm.observe_cycle("t1", {"A": _q()}, ["A", "B"], {}, today_str="2026-09-02")
    qrow = [x for x in r["rows"] if x["source"] == "__quotes__"][0]
    assert qrow["req"] == 2 and qrow["ok"] == 1 and qrow["fail"] == 1
    assert abs(r["coverage"] - 0.5) < 1e-9


def test_format_block_content():
    hm = HealthMonitor()
    r = hm.observe_cycle(
        "t1", {"A": _q()}, ["A", "B"], {"quote_sina": _snap(1, 1, 0)}, today_str="2026-09-02"
    )
    txt = format_health_block(r)
    assert "数据源健康" in txt and "覆盖 1/2" in txt and "quote_sina" in txt
    assert format_health_block(None) == ""


# ---------- 第145轮 #10：口径/单位漂移监控 ----------
def _q_full(latest=100.0, oi=1000.0, vol=500.0, chg=0.01, date="2026-09-02"):
    return {
        "latest": latest,
        "open_interest": oi,
        "volume": vol,
        "bid": latest - 0.1,
        "ask": latest + 0.1,
        "chg_pct": chg,
        "date": date,
    }


def test_drift_requires_history():
    hm = HealthMonitor()
    # 单轮观察：样本不足，不告警
    r = hm.observe_cycle("t1", {"RB0": _q_full()}, ["RB0"], {}, today_str="2026-09-02")
    assert r["drift_alerts"] == []
    assert "RB0" not in {d["code"] for d in r["drift_alerts"]}


def test_drift_detects_unit_change():
    hm = HealthMonitor()
    ts = "2026-09-02"
    # 累积 6 轮正常值
    for _ in range(6):
        hm.observe_cycle(ts, {"RB0": _q_full()}, ["RB0"], {}, today_str=ts)
    # 第 7 轮：volume 量级 ×1000（疑似接口单位从"手"变"张"或小数位变更）
    r = hm.observe_cycle(ts, {"RB0": _q_full(vol=500.0 * 1000)}, ["RB0"], {}, today_str=ts)
    vols = [d for d in r["drift_alerts"] if d["field"] == "volume"]
    assert vols and vols[0]["code"] == "RB0"
    assert vols[0]["factor"] >= 50


def test_drift_no_false_positive_small_change():
    hm = HealthMonitor()
    ts = "2026-09-02"
    for _ in range(6):
        hm.observe_cycle(ts, {"RB0": _q_full()}, ["RB0"], {}, today_str=ts)
    # 正常价格波动 ±10% 不告警
    r = hm.observe_cycle(ts, {"RB0": _q_full(latest=110.0)}, ["RB0"], {}, today_str=ts)
    assert r["drift_alerts"] == []


def test_drift_zero_and_missing_ignored():
    hm = HealthMonitor()
    ts = "2026-09-02"
    for _ in range(6):
        hm.observe_cycle(ts, {"RB0": _q_full()}, ["RB0"], {}, today_str=ts)
    # 持仓量=0（接口缺字段常见）不触发漂移告警（另有缺数/陈旧体检兜底）
    r = hm.observe_cycle(ts, {"RB0": _q_full(oi=0.0)}, ["RB0"], {}, today_str=ts)
    assert all(d["field"] != "open_interest" for d in r["drift_alerts"])


def test_drift_reset_clears_history():
    hm = HealthMonitor()
    ts = "2026-09-02"
    for _ in range(6):
        hm.observe_cycle(ts, {"RB0": _q_full()}, ["RB0"], {}, today_str=ts)
    assert ("RB0", "latest") in hm.field_history
    hm.reset()
    assert hm.field_history == {}
    assert hm.last_result is None


def test_format_block_shows_drift():
    hm = HealthMonitor()
    ts = "2026-09-02"
    for _ in range(6):
        hm.observe_cycle(ts, {"RB0": _q_full()}, ["RB0"], {}, today_str=ts)
    r = hm.observe_cycle(ts, {"RB0": _q_full(vol=500.0 * 10000)}, ["RB0"], {}, today_str=ts)
    txt = format_health_block(r)
    assert "口径漂移" in txt
