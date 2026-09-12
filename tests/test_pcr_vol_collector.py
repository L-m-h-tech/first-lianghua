# -*- coding: utf-8 -*-
"""成交量 PCR 采集器回归（第132轮，纯函数/临时库零网络，不碰生产 monitor.db）。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import pcr_vol_collector as PC


class _FakeApi:
    """合成 api：1 个 call + 1 个 put，两日成交量；含一根未来占位行（volume=0）。"""

    def query_options(self, under, option_class=None):
        return ["X.c100"] if option_class == "CALL" else ["X.p100"]

    def get_kline_serial(self, oid, duration_seconds, data_length):
        import pandas as _pd
        data = {"X.c100": [(1789056000 * 1e9, 8.0), (1789342400 * 1e9, 0.0)],   # 09-11 + 未来占位
                "X.p100": [(1789056000 * 1e9, 4.0), (1789342400 * 1e9, 6.0)]}
        return _pd.DataFrame([{"datetime": d, "volume": v} for d, v in data[oid]])


def test_ns_to_date_beijing():
    assert PC._ns_to_date(None) is None
    assert len(PC._ns_to_date(1789056000 * 1e9)) == 10


def test_tq_symbol_candidates():
    assert PC._tq_symbols("SHFE", "RB", "2611") == ["SHFE.rb2611"]
    assert PC._tq_symbols("CZCE", "SR", "2611")[:2] == ["CZCE.SR611", "CZCE.SR2611"]
    assert PC._tq_symbols("DCE", "m", "2611") == ["DCE.m2611"]


def test_collect_variety_filters_future_placeholder():
    # 周末 TqSdk 日线附带下一交易日空壳行（volume=0）→ 必须过滤（2026-09-12 周六实测）
    rows = PC.collect_variety(_FakeApi(), "RB", "SHFE", "2611", days=5, today="2026-09-12")
    assert all(r["trade_date"] <= "2026-09-12" for r in rows)
    assert len(rows) == 1
    assert rows[0]["call_vol"] == 8.0 and rows[0]["put_vol"] == 4.0
    assert abs(rows[0]["pcr_vol"] - 0.5) < 1e-9


def test_write_rows_idempotent(tmp_path):
    db = str(tmp_path / "m.db")
    rows = [{"sym": "RB", "trade_date": "2026-09-11", "call_vol": 8.0, "put_vol": 4.0,
             "pcr_vol": 0.5, "n_calls": 1, "n_puts": 1, "source": "tqsdk"}]
    assert PC.write_rows(db, rows) == 1
    assert PC.write_rows(db, rows) == 1                 # REPLACE 幂等
    import sqlite3
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM option_pcr_vol").fetchone()[0]
    conn.close()
    assert n == 1
