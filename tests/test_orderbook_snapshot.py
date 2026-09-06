# -*- coding: utf-8 -*-
"""G14（第92轮）一档盘口快照自采 orderbook_snapshot 测试：零网络、确定性。

覆盖：桶推导 / 软降级过滤 / upsert 去重 / 节流 / 时段门控 / 总开关 / 试点为空 /
统计报告落盘 / storage 幂等 / 异常全吞 / selftest。全部用注入 fetcher，不发任何请求。
"""
import json

import pytest

import config
import orderbook_snapshot as obs
import storage


def _fetcher(quotes_map):
    """把 {code: quote} 包成 fetcher(codes) 替身，只回 pilots 里请求的 code。"""
    def fake_fetcher(codes):
        return {c: quotes_map[c] for c in codes if c in quotes_map}
    return fake_fetcher


RB = {"name": "螺纹钢连续", "latest": 3173.0, "bid": 3172.0, "ask": 3173.0,
      "bid_vol": 218.0, "ask_vol": 7.0, "open_interest": 1502783.0,
      "volume": 277062.0, "prev_settle": 3160.0,
      "quote_date": "2026-09-04", "quote_time": "230000"}
CU = {"name": "铜连续", "latest": 109290.0, "bid": 109270.0, "ask": 109290.0,
      "bid_vol": 4.0, "ask_vol": 2.0, "open_interest": 211001.0,
      "volume": 26154.0, "prev_settle": 109110.0,
      "quote_date": "2026-09-05", "quote_time": "010000"}


@pytest.fixture(autouse=True)
def _reset_throttle():
    """每测重置进程内节流状态，防跨测污染。"""
    obs._LAST_COLLECT[0] = None
    yield
    obs._LAST_COLLECT[0] = None


def _now():
    from datetime import datetime
    return datetime(2026, 9, 7, 10, 5, 0)   # 周一10:05，flat_calendar 下为交易时段


def test_bucket_derivation():
    from datetime import datetime
    now = datetime(2026, 9, 6, 21, 7, 0)
    assert obs._bucket("2026-09-04", "230000", now) == "2026-09-04 23:00"
    assert obs._bucket("2026-09-05", "010000", now) == "2026-09-05 01:00"
    # 缺/坏行情时间 → 回退本机5分钟桶
    assert obs._bucket("", "", now) == "2026-09-06 21:05"
    assert obs._bucket("2026-09-04", "abc", now) == "2026-09-06 21:05"


def test_collect_stores_valid_and_filters_invalid(tmp_db, monkeypatch, flat_calendar):
    quotes = {"RB0": RB, "CU0": CU}
    quotes["CU0"] = dict(CU, bid=0.0)                     # 买一=0 → 非法档位
    res = obs.collect_once(tmp_db, now=_now(), force=True,
                           fetcher=_fetcher({"RB0": RB, "CU0": quotes["CU0"]}))
    assert res["stored"] == 1 and res["skipped"] == ""
    rows = tmp_db.conn.execute("SELECT * FROM tick_snapshots").fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["sym"] == "RB"
    assert r["bucket"] == "2026-09-04 23:00"
    assert r["bid"] == 3172.0 and r["ask"] == 3173.0
    assert r["bid_vol"] == 218.0 and r["ask_vol"] == 7.0
    assert abs(r["spread"] - 1.0) < 1e-9
    assert abs(r["spread_bp"] - 1.0 / 3173.0 * 1e4) < 1e-3
    assert r["variety"] == "螺纹钢"


def test_ask_lt_bid_filtered(tmp_db, monkeypatch, flat_calendar):
    bad = dict(RB, ask=3170.0)                            # ask < bid → 丢弃
    res = obs.collect_once(tmp_db, now=_now(), force=True,
                           fetcher=_fetcher({"RB0": bad}))
    assert res["stored"] == 0


def test_upsert_dedup_same_bucket_and_new_bucket(tmp_db, monkeypatch, flat_calendar):
    # 同一行情桶重复采集 → 幂等仍1行（upsert 去重）
    obs.collect_once(tmp_db, now=_now(), force=True, fetcher=_fetcher({"RB0": RB}))
    obs.collect_once(tmp_db, now=_now(), force=True, fetcher=_fetcher({"RB0": RB}))
    assert tmp_db.conn.execute("SELECT COUNT(*) FROM tick_snapshots").fetchone()[0] == 1
    # 不同行情桶（行情时间推进） → 新增一行
    rb2 = dict(RB, quote_time="230500")
    obs.collect_once(tmp_db, now=_now(), force=True, fetcher=_fetcher({"RB0": rb2}))
    rows = tmp_db.conn.execute("SELECT bucket FROM tick_snapshots ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["2026-09-04 23:00", "2026-09-04 23:05"]


def test_throttle_skips_within_interval(tmp_db, monkeypatch, flat_calendar):
    monkeypatch.setattr(config, "SNAPSHOT_ONLY_TRADING", False)   # 只验证节流
    from datetime import timedelta
    obs.collect_once(tmp_db, now=_now(), force=True, fetcher=_fetcher({"RB0": RB}))
    res = obs.collect_once(tmp_db, now=_now() + timedelta(seconds=60),
                           fetcher=_fetcher({"RB0": RB}))
    assert res["skipped"] == "throttle" and res["stored"] == 0
    # 超过节流窗口 → 允许采集
    res2 = obs.collect_once(tmp_db, now=_now() + timedelta(seconds=301),
                            fetcher=_fetcher({"RB0": RB}))
    assert res2["skipped"] == "" and res2["stored"] == 1


def test_trading_gate_offhours_and_force(tmp_db, monkeypatch, flat_calendar):
    from datetime import datetime
    weekend = datetime(2026, 9, 12, 12, 0, 0)               # 周六（flat_calendar 非交易日）
    res = obs.collect_once(tmp_db, now=weekend, fetcher=_fetcher({"RB0": RB}))
    assert res["skipped"] == "off_hours"
    res2 = obs.collect_once(tmp_db, now=weekend, force=True, fetcher=_fetcher({"RB0": RB}))
    assert res2["stored"] == 1


def test_disabled_and_no_pilots(tmp_db, monkeypatch, flat_calendar):
    monkeypatch.setattr(config, "SNAPSHOT_ENABLED", False)
    assert obs.collect_once(tmp_db, now=_now(), force=True,
                            fetcher=_fetcher({"RB0": RB}))["skipped"] == "disabled"
    monkeypatch.setattr(config, "SNAPSHOT_ENABLED", True)
    monkeypatch.setattr(config, "SNAPSHOT_VARIETIES", [])
    assert obs.collect_once(tmp_db, now=_now(), force=True,
                            fetcher=_fetcher({"RB0": RB}))["skipped"] == "no_pilots"


def test_error_swallowed(tmp_db, monkeypatch, flat_calendar):
    def boom(codes):
        raise RuntimeError("network down")
    res = obs.collect_once(tmp_db, now=_now(), force=True, fetcher=boom)
    assert res["skipped"] == "error" and res["stored"] == 0


def test_render_stats_writes_txt_and_json(tmp_db, monkeypatch, flat_calendar, tmp_path):
    monkeypatch.setattr(config, "SNAPSHOT_ONLY_TRADING", False)
    obs.collect_once(tmp_db, now=_now(), force=True, fetcher=_fetcher({"RB0": RB}))
    txt = str(tmp_path / "orderbook_stats.txt")
    js = str(tmp_path / "orderbook_stats.json")
    summ = obs.render_stats(tmp_db, days=30, txt=txt, js=js)
    assert summ["n_rows"] == 1 and summ["n_syms"] == 1
    assert summ["per_sym"]["RB"]["n_samples"] == 1
    assert (summ["per_sym"]["RB"]["spread_bp_avg"] or 0) > 0
    assert "G14 一档盘口快照统计" in open(txt, encoding="utf-8").read()
    payload = json.load(open(js, encoding="utf-8"))
    assert payload["per_sym"]["RB"]["latest_bid"] == 3172.0


def test_storage_upsert_idempotent(tmp_db):
    from datetime import datetime
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def row(bucket):
        return {"sym": "RB", "variety": "螺纹钢连续", "bucket": bucket,
                "quote_date": "2026-09-04", "quote_time": "230000",
                "collected_at": now_s,
                "bid": 3172.0, "ask": 3173.0, "latest": 3173.0,
                "bid_vol": 218.0, "ask_vol": 7.0, "spread": 1.0,
                "spread_bp": 3.15, "prev_settle": 3160.0, "oi": 1502783.0,
                "volume": 277062.0, "created_real": 1.0}
    assert tmp_db.upsert_tick_snapshots([row("2026-09-04 23:00")]) == 1
    assert tmp_db.upsert_tick_snapshots([row("2026-09-04 23:00")]) == 1   # 同桶覆盖
    rows = tmp_db.conn.execute("SELECT COUNT(*) FROM tick_snapshots").fetchone()[0]
    assert rows == 1
    assert tmp_db.upsert_tick_snapshots([]) == 0
    # 读取接口
    recs = tmp_db.recent_tick_snapshots(days=3650)
    assert len(recs) == 1 and recs[0]["sym"] == "RB"


def test_table_counts_includes_snapshots(tmp_db):
    counts = tmp_db.table_counts()
    assert "tick_snapshots" in counts and counts["tick_snapshots"] == 0


def test_selftest_passes():
    assert obs.selftest() == 0
