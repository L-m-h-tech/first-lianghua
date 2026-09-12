# -*- coding: utf-8 -*-
"""PCR 因子影子体检工具回归（第129轮，tmp 临时库零网络，不碰生产 monitor.db）。"""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import pcr_factor_research as PR


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE option_chains(id INTEGER PRIMARY KEY, ts TEXT, cycle INTEGER,
        sym TEXT, expiry TEXT, put_oi REAL, call_oi REAL, pcr_oi REAL)""")
    conn.execute("""CREATE TABLE minute_bars(id INTEGER PRIMARY KEY, sym TEXT, period INTEGER,
        bar_dt TEXT, c REAL)""")
    rows = [
        ("2026-09-01 15:00:03", 1, "RB", "2610", 100.0, 200.0, 0.5),
        ("2026-09-01 23:00:03", 1, "RB", "2610", 120.0, 200.0, 0.6),   # 末次快照 → 0.6
        ("2026-09-02 23:00:03", 1, "RB", "2610", 200.0, 200.0, 1.0),
        ("2026-09-02 23:00:03", 1, "RB", "2701", 100.0, 100.0, 1.0),   # 跨月合计 300/300
    ]
    for r in rows:
        conn.execute("INSERT INTO option_chains(ts,cycle,sym,expiry,put_oi,call_oi,pcr_oi)"
                     " VALUES(?,?,?,?,?,?,?)", r)
    for d, dt, c in [("2026-09-01", "2026-09-01 23:00:00", 100.0),
                     ("2026-09-02", "2026-09-02 23:00:00", 101.0),
                     ("2026-09-08", "2026-09-08 23:00:00", 103.0)]:
        conn.execute("INSERT INTO minute_bars(sym,period,bar_dt,c) VALUES('RB',60,?,?)", (dt, c))
    conn.commit()
    conn.close()


def test_load_pcr_daily_last_snapshot_and_cross_expiry(tmp_path):
    db = str(tmp_path / "m.db")
    _make_db(db)
    pcr = PR.load_pcr_daily(db)
    # 同日多快照取末次（0.6 而非 0.5）；跨到期月 OI 合计（300/300=1.0）
    assert pcr["RB"] == [("2026-09-01", 0.6), ("2026-09-02", 1.0)]


def test_load_closes_last_bar_of_day(tmp_path):
    db = str(tmp_path / "m.db")
    _make_db(db)
    closes = PR.load_closes_from_minutes(db, ["RB"])
    assert closes["RB"] == [("2026-09-01", 100.0), ("2026-09-02", 101.0), ("2026-09-08", 103.0)]


def test_forward_returns_pit_alignment(tmp_path):
    db = str(tmp_path / "m.db")
    _make_db(db)
    closes = PR.load_closes_from_minutes(db, ["RB"])
    fwd = PR.forward_returns(closes["RB"])
    # 因子(t) 对 ret1d(t→t+1)：09-01 的前向1日 = +1%（严格 PIT）
    assert abs(fwd["2026-09-01"][0] - 0.01) < 1e-9
    assert fwd["2026-09-01"][1] is None               # 合成序列仅3日，5日前向不存在


def test_build_factors_level_chg_and_pct30_gate(tmp_path):
    db = str(tmp_path / "m.db")
    _make_db(db)
    pcr = PR.load_pcr_daily(db)
    fac = PR.build_factors(pcr)
    assert fac["pcr_level"][("RB", "2026-09-02")] == 1.0
    assert abs(fac["pcr_chg"][("RB", "2026-09-02")] - (1.0 / 0.6 - 1)) < 1e-9
    assert ("RB", "2026-09-02") not in fac["pcr_pct30"]    # 历史<30天 → 不产出（诚实缺项）


def test_spearman_and_quintile():
    assert abs(PR.spearman([1, 2, 3, 4], [10, 20, 30, 40]) - 1.0) < 1e-9
    assert PR.spearman([1, 2], [1, 2]) is None             # n<3
    qs = PR.quintile_spread([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], [1] * 5 + [2] * 5)
    assert qs[2] == 1.0                                     # 高-低价差
