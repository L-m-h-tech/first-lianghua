# -*- coding: utf-8 -*-
"""SQLite 存储层回归（分钟bar去重、ML样本覆盖写、完整性，全部用临时库，不碰生产db）。"""
import storage


def _bar(dt="2026-09-01 09:05", sym="RB", contract="RB2610"):
    return {"sym": sym, "contract": contract, "exchange": "SHFE", "period": 5,
            "dt": dt, "trade_date": "2026-09-01", "o": 3500, "h": 3510, "l": 3495,
            "c": 3505, "v": 100, "amount": 350000}


def test_minute_bars_dedup(tmp_db):
    assert tmp_db.insert_minute_bars([_bar()]) == 1
    assert tmp_db.insert_minute_bars([_bar()]) == 0        # 同(contract,period,dt)忽略
    rows = tmp_db.minute_bars_for_sym("RB", 5)
    assert len(rows) == 1 and rows[0]["c"] == 3505
    cov = tmp_db.minute_bars_coverage()
    assert cov[5]["bars"] == 1 and cov[5]["contracts"] == 1


def test_minute_bars_cross_contract_stitch(tmp_db):
    # 换月后新旧主力按时间自然衔接、升序
    tmp_db.insert_minute_bars([_bar("2026-09-01 09:05", contract="RB2610"),
                               _bar("2026-10-01 09:05", contract="RB2701")])
    rows = tmp_db.minute_bars_for_sym("RB", 5)
    assert [r["contract"] for r in rows] == ["RB2610", "RB2701"]


def test_insert_minute_bars_empty(tmp_db):
    assert tmp_db.insert_minute_bars([]) == 0


def test_ml_samples_upsert(tmp_db):
    sample = {"sym": "RB", "variety": "螺纹钢", "period": 5, "bar_dt": "2026-09-01 09:05",
              "trade_date": "2026-09-01", "direction": 1, "entry_price": 3500, "atr": 20,
              "tp_price": 3540, "sl_price": 3476, "exit_dt": "2026-09-01 10:00",
              "exit_price": 3540, "label": 1, "exit_reason": "止盈", "bars_held": 11,
              "ret_dir": 0.011, "tech_score": 5.0, "features": {"mom": 0.3, "vol": 0.1}}
    assert tmp_db.insert_ml_samples([sample]) == 1
    sample["label"] = -1                                       # 同主键覆盖写
    assert tmp_db.insert_ml_samples([sample]) == 1
    rows = tmp_db.ml_sample_rows(sym="RB", period=5)
    assert len(rows) == 1 and rows[0]["label"] == -1
    assert rows[0]["features"]["mom"] == 0.3                   # features_json 反序列化


def test_table_counts_and_integrity(tmp_db):
    counts = tmp_db.table_counts()
    assert isinstance(counts, dict)
    # 建表后执行 integrity_check 不报错
    with tmp_db.lock:
        ok = tmp_db.conn.execute("PRAGMA integrity_check").fetchone()[0]
    assert ok == "ok"


def test_calibration_pairs_empty(tmp_db):
    assert tmp_db.calibration_pairs() == []


def test_data_health_upsert_and_recent(tmp_db):
    rows = [{"source": "quote_sina", "req": 64, "ok": 62, "fail": 2, "stale": 0,
             "jump": 0, "state": "closed", "note": "avail=0.97"},
            {"source": "__quotes__", "req": 64, "ok": 62, "fail": 2, "stale": 1,
             "jump": 0, "state": "closed", "note": "missing=CU0,XX0"}]
    assert tmp_db.insert_data_health("2026-09-02 10:00:00", rows) == 2
    # 同 (ts,source) 覆盖写，不新增
    rows[0]["ok"] = 64
    assert tmp_db.insert_data_health("2026-09-02 10:00:00", [rows[0]]) == 1
    recent = tmp_db.data_health_recent()
    assert len(recent) == 2
    sina = [r for r in recent if r["source"] == "quote_sina"][0]
    assert sina["ok"] == 64 and sina["fail"] == 2
    assert tmp_db.table_counts()["data_health"] == 2


def test_data_health_empty(tmp_db):
    assert tmp_db.insert_data_health("t", []) == 0
    assert tmp_db.data_health_recent() == []


def test_score_band_name():
    assert storage.score_band_name(1.0) == "观望"
    assert storage.score_band_name(3.0) == "轻仓"
    assert storage.score_band_name(5.0) == "分批"
    assert storage.score_band_name(-7.0) == "强信号"
