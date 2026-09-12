# -*- coding: utf-8 -*-
"""纸面账户三方对账工具回归（第128轮，tmp 临时库零网络，不碰生产 paper_accounts/monitor.db）。"""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import paper_reconcile as PR


def _make_paper_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE paper_trades(id INTEGER PRIMARY KEY, ts TEXT, pos_ref TEXT,
        sym TEXT, side TEXT, direction INTEGER, lots INTEGER, price REAL, notional REAL,
        slip_yuan REAL, fee_yuan REAL, realized_yuan REAL, leg TEXT, reason TEXT,
        forced INTEGER, entry_ts TEXT, entry_price REAL)""")
    conn.execute("""CREATE TABLE paper_equity(id INTEGER PRIMARY KEY, ts TEXT,
        static_equity REAL, float_pnl REAL, equity REAL, realized REAL, fees_paid REAL,
        n_trades INTEGER)""")
    conn.execute("CREATE TABLE paper_orders(id INTEGER PRIMARY KEY, status TEXT)")
    rows = [
        ("2026-09-01 09:30:00", "RB-1", "RB", "open", 1, 1, 3000, 30000, 3, 6, 0, "开仓", 0,
         "2026-09-01 09:30:00", 3000),
        ("2026-09-01 14:30:00", "RB-1", "RB", "close", 1, 1, 3020, 30200, 3, 6, 200, "平仓", 0,
         "2026-09-01 09:30:00", 3000),
        ("2026-09-02 09:30:00", "MA-1", "MA", "open", -1, 1, 2500, 25000, 2.5, 5, 0, "开仓", 0,
         "2026-09-02 09:30:00", 2500),
        ("2026-09-02 14:30:00", "MA-1", "MA", "close", -1, 1, 2504, 25040, 2.5, 5, -80, "平仓", 0,
         "2026-09-02 09:30:00", 2500),
        ("2026-09-03 09:30:00", "CU-1", "CU", "open", 1, 1, 70000, 700000, 70, 25, 0, "开仓", 0,
         "2026-09-03 09:30:00", 70000),
        ("2026-09-04 09:30:00", "I-1", "I", "open", 1, 1, 800, 8000, 8, 6, 0, "开仓", 1,
         "2026-09-04 09:30:00", 800),
        ("2026-09-04 09:35:00", "I-1", "I", "close", 1, 1, 799, 7990, 8, 6, -10, "平仓", 1,
         "2026-09-04 09:30:00", 800),
    ]
    for r in rows:
        conn.execute("INSERT INTO paper_trades(ts,pos_ref,sym,side,direction,lots,price,"
                     "notional,slip_yuan,fee_yuan,realized_yuan,leg,forced,entry_ts,entry_price)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
    conn.execute("INSERT INTO paper_equity(ts,static_equity,float_pnl,equity,realized,fees_paid,n_trades)"
                 " VALUES('2026-09-01 09:35:00',100000,0,100000,0,6,1)")
    conn.execute("INSERT INTO paper_equity(ts,static_equity,float_pnl,equity,realized,fees_paid,n_trades)"
                 " VALUES('2026-09-04 09:40:00',100110,5,100115,110,22,3)")
    conn.execute("INSERT INTO paper_orders(id,status) VALUES(1,'filled'),(2,'pending')")
    conn.commit()
    conn.close()


def test_reconcile_account_identity_pairing_metrics(tmp_path):
    db = str(tmp_path / "paper_测试.db")
    _make_paper_db(db)
    acc = PR.reconcile_account(db, name="paper_测试")
    # 资金守恒：equity0 = static - realized 逐行恒定 = 100000
    assert acc["identity_ok"] is True and acc["equity0_implied"] == 100000.0
    assert acc["n_closed"] == 3 and acc["n_open"] == 1 and acc["forced_n"] == 2
    assert acc["net_total"] == 110.0 and acc["win_rate"] == 0.3333
    assert acc["pf"] == 2.222 and acc["orders_pending"] == 1
    assert acc["fees_total"] == 59.0 and acc["slip_total"] == 97.0


def test_reconcile_account_empty_is_not_violation(tmp_path):
    db = str(tmp_path / "paper_空.db")
    _make_paper_db(db)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM paper_trades")
    conn.execute("DELETE FROM paper_equity")
    conn.commit()
    conn.close()
    acc = PR.reconcile_account(db)
    assert acc["identity_ok"] is None              # 无权益行=无法评估，不算违规
    assert acc["n_closed"] == 0


def test_match_signals_same_sym_dir_window(tmp_path):
    db = str(tmp_path / "paper_测试.db")
    _make_paper_db(db)
    acc = PR.reconcile_account(db)
    outcomes = [
        {"variety": "螺纹钢", "code": "RB0", "direction_int": 1,
         "entry_ts": "2026-09-01 09:45:00", "ret": 0.005, "status": "evaluated"},
        {"variety": "螺纹钢", "code": "RB0", "direction_int": -1,
         "entry_ts": "2026-09-01 09:45:00", "ret": -0.005, "status": "evaluated"},   # 反向不配
        {"variety": "螺纹钢", "code": "RB0", "direction_int": 1,
         "entry_ts": "2026-09-01 23:45:00", "ret": 0.009, "status": "evaluated"},    # 超窗不配
        {"variety": "甲醇", "code": "MA0", "direction_int": 1,
         "entry_ts": "2026-09-02 09:40:00", "ret": -0.002, "status": "pending"},     # 未评估不配
    ]
    matched, agg = PR.match_signals(acc["closed_details"], outcomes, window_min=30)
    assert len(matched) == 1 and matched[0][0]["pos_ref"] == "RB-1"
    assert agg["dir_consistency"] == 1.0 and agg["signal_ret_mean"] == 0.005


def test_sym_of_variety_and_code_fallback():
    assert PR._sym_of("螺纹钢", "RB0") == "RB"       # VARIETIES 中文映射
    assert PR._sym_of("不存在的品种", "SH0") == "SH"   # code 主连去尾 0 兜底
    assert PR._sym_of("不存在的品种", "") == ""


def test_render_contains_verdict_and_signal_section():
    acc = {"name": "paper_测试", "n_trades": 7, "n_closed": 3, "n_open": 1, "forced_n": 2,
           "orders_pending": 1, "net_total": 110.0, "win_rate": 0.3333, "pf": 2.222,
           "avg_net": 36.67, "avg_hold_min": 250.0, "fees_total": 58.0, "slip_total": 97.0,
           "identity_ok": True, "equity0_implied": 100000.0, "equity0_spread": 0.0,
           "equity_last": 100115.0, "drawdown_last": 0.0,
           "signal_match": {"n": 1, "dir_consistency": 1.0, "signal_ret_mean": 0.005,
                            "paper_ret_mean": 0.00667, "signal_win": 1.0, "paper_win": 1.0}}
    res = {"generated": "t", "window_min": 30, "n_accounts": 1, "total_closed_trips": 3,
           "total_net": 110.0, "g1_verdict": "样本不足", "accounts": [acc],
           "backtest_reference": None}
    txt = PR.render(res)
    assert "G1 验收结论" in txt and "样本不足" in txt
    assert "信号对照" in txt and "方向一致率 100%" in txt
