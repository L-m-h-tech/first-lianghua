# -*- coding: utf-8 -*-
"""1-2bar 短单病理切片回归（第130轮 Phase1，纯函数零网络）。"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import short_trade_pathology as SP


def _trade(sym, direction, entry_px, exit_px, entry_dt, hold_bars, gross, fee, net,
           reason, score, leg="平今"):
    return {"sym": sym, "dir": "多" if direction > 0 else "空", "direction": direction,
            "entry_px": entry_px, "exit_px": exit_px,
            "entry_dt": entry_dt, "exit_dt": entry_dt + timedelta(minutes=30 * hold_bars),
            "leg": leg, "hold_bars": hold_bars, "gross_yuan": gross, "fee_yuan": fee,
            "net_yuan": net, "reason": reason, "forced": False, "entry_score": score}


def _synthetic():
    trades = [
        _trade("RB", 1, 100.0, 99.0, datetime(2026, 9, 1, 9, 31), 2, -100.0, 6.0, -106.0,
               "止损", 1.5),
        _trade("MA", -1, 2500.0, 2525.0, datetime(2026, 9, 2, 9, 31), 2, -250.0, 5.0, -255.0,
               "止损", 4.5),
        _trade("CU", 1, 70000.0, 70050.0, datetime(2026, 9, 3, 9, 31), 1, 50.0, 24.0, -174.0,
               "反向信号平仓", 3.2),
        _trade("RB", 1, 100.0, 105.0, datetime(2026, 9, 5, 9, 31), 5, 500.0, 6.0, 494.0,
               "止盈", 4.0, leg="平昨"),
    ]
    bars = {"RB": [], "MA": []}
    t0 = datetime(2026, 9, 1, 10, 2)          # RB 止损后回摆 +1.5%（≥止损距离 1%）→ whipsaw
    for i in range(4):
        t = t0 + timedelta(minutes=30 * i)
        bars["RB"].append({"dt": t, "h": 101.5, "l": 98.5})
    t0 = datetime(2026, 9, 2, 10, 2)          # MA 止损后继续不利 → 非 whipsaw
    for i in range(4):
        t = t0 + timedelta(minutes=30 * i)
        bars["MA"].append({"dt": t, "h": 2526.0, "l": 2510.0})
    return trades, bars


def test_pathology_buckets_and_reasons():
    trades, bars = _synthetic()
    res = SP.build_pathology(trades, bars_by_sym=bars, post_bars=4, period_min=30)
    assert res["short_overall"]["n"] == 3 and res["short_overall"]["net"] == -535.0
    assert res["reason_rows"]["止损"]["n"] == 2 and "止盈" not in res["reason_rows"]  # 止盈单非短桶
    # 3-6bar 对照不含短单
    assert res["mid_overall"]["n"] == 1 and res["mid_overall"]["net"] == 494.0


def test_whipsaw_classification():
    trades, bars = _synthetic()
    res = SP.build_pathology(trades, bars_by_sym=bars, post_bars=4, period_min=30)
    r2 = res["r2_stop"]
    assert r2["n_stops"] == 2 and r2["n_with_bars"] == 2
    assert r2["whipsaw_rate_4bar"] == 0.5     # RB 回摆≥止损距=whipsaw；MA 继续不利=正确止损


def test_r1_counterfactual_score_below_3():
    trades, bars = _synthetic()
    res = SP.build_pathology(trades, bars_by_sym=bars, post_bars=4, period_min=30)
    # |分|<3 仅 RB（1.5）；CU 3.2 / MA 4.5 不在"门槛2→3"口径内
    assert res["r1_weak_n"] == 1 and res["r1_weak_net"] == -106.0


def test_r4_leg_structure():
    trades, bars = _synthetic()
    res = SP.build_pathology(trades, bars_by_sym=bars, post_bars=4, period_min=30)
    assert res["r4_leg_rows"]["平今"]["n"] == 3 and "平昨" not in res["r4_leg_rows"]  # 平昨单非短桶
    assert res["fee_ratio_short"] is not None and res["fee_ratio_mid"] is not None
