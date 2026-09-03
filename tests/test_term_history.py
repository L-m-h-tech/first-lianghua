# -*- coding: utf-8 -*-
"""G22（第34轮）term_history 多合约期限结构重建的零网络合成断言。"""
from datetime import date

import term_history as th


def test_month_iter_and_symbol():
    assert th.month_iter(24, 11, 25, 2) == [(24, 11), (24, 12), (25, 1), (25, 2)]
    assert th.kline_symbol("ta", 25, 1) == "TA2501"
    assert th.parse_yymm("RB2501") == (25, 1)
    assert th.parse_yymm("xx") is None
    assert th.month_gap_days(25, 1, 25, 3) == (date(2025, 3, 1) - date(2025, 1, 1)).days


def test_select_curve_roll_buffer():
    on = date(2024, 12, 20)
    live = [
        {"code": "X2412", "yy": 24, "mm": 12, "settle": 99.0, "oi": 10, "vol": 1},   # 已进交割月剔除
        {"code": "X2501", "yy": 25, "mm": 1, "settle": 100.0, "oi": 10, "vol": 1},
        {"code": "X2502", "yy": 25, "mm": 2, "settle": 101.0, "oi": 9, "vol": 1},
        {"code": "X2503", "yy": 25, "mm": 3, "settle": 102.0, "oi": 8, "vol": 1},
        {"code": "X2504", "yy": 25, "mm": 4, "settle": 0.0, "oi": 8, "vol": 1},      # 无价剔除
    ]
    near, nxt, far = th.select_curve(on, live, roll_buffer_days=3, min_oi=1)
    assert (near["code"], nxt["code"], far["code"]) == ("X2501", "X2502", "X2503")
    # 推到临近交割，X2501 被缓冲剔除，近月滚动到 X2502
    near2, nxt2, _ = th.select_curve(date(2024, 12, 30), live, 3, 1)
    assert near2["code"] == "X2502" and nxt2["code"] == "X2503"
    # 持仓低于门槛剔除
    live[1]["oi"] = 0
    assert th.select_curve(on, live, 3, 1)[0]["code"] == "X2502"


def test_annual_carry_sign():
    gap = th.month_gap_days(25, 1, 25, 4)
    assert th.annual_carry(102.0, 100.0, gap) > 0   # 近高远低 Back 正carry
    assert th.annual_carry(100.0, 102.0, gap) < 0   # 近低远高 Contango 负carry
    assert th.annual_carry(1.0, 1.0, 0) is None
    assert th.annual_carry(0.0, 1.0, gap) is None


def test_curve_loadings():
    lv, sl, cv = th.curve_loadings(102.0, 101.0, 100.0)
    assert sl > 0 and lv is not None and cv is not None
    lv2, sl2, cv2 = th.curve_loadings(102.0, 101.0, None)
    assert sl2 is None and lv2 is None and cv2 is None


def test_moving_mean_basis_change():
    assert th.moving_mean([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    assert th.moving_mean([1, 2], 1) == [1, 2]
    assert th.basis_change([1.0, 2.0, 4.0, 7.0], 2) == [None, None, 3.0, 5.0]


def test_build_term_series_roll_and_oi():
    bars = {
        "X2501": [{"d": "2024-12-02", "c": 100.0, "s": 100.0, "v": 5, "p": 100},
                  {"d": "2024-12-03", "c": 100.0, "s": 100.0, "v": 5, "p": 90}],
        "X2502": [{"d": "2024-12-02", "c": 99.0, "s": 99.0, "v": 5, "p": 80},
                  {"d": "2024-12-03", "c": 99.0, "s": 99.0, "v": 5, "p": 70}],
        "X2503": [{"d": "2024-12-02", "c": 98.0, "s": 98.0, "v": 5, "p": 60},
                  {"d": "2024-12-03", "c": 98.0, "s": 98.0, "v": 5, "p": 50}],
    }
    ser = th.build_term_series(bars, roll_buffer_days=0, min_oi=1)
    assert [r["date"] for r in ser] == ["2024-12-02", "2024-12-03"]
    r0 = ser[0]
    assert (r0["near"], r0["next"], r0["far"]) == ("X2501", "X2502", "X2503")
    assert r0["oi_sum"] == 240 and r0["oi_near"] == 100 and r0["n_live"] == 3
    assert r0["carry_far"] > 0 and r0["slope"] > 0     # 近100>远98 Back
    # 结算价缺失退回收盘价
    bars2 = {"Y2501": [{"d": "2024-12-02", "c": 50.0, "v": 1, "p": 5}],
             "Y2502": [{"d": "2024-12-02", "c": 49.0, "v": 1, "p": 5}]}
    s2 = th.build_term_series(bars2, 0, 1)
    assert s2[0]["near_s"] == 50.0
    assert th.build_term_series({}) == []


def test_module_selftest():
    assert th._selftest() == 0


def test_near_roll_nav():
    ts = [
        {"near": "A", "near_s": 100.0}, {"near": "A", "near_s": 102.0},
        {"near": "A", "near_s": 104.04},
        {"near": "B", "near_s": 80.0}, {"near": "B", "near_s": 80.8},
    ]
    nav = th.near_roll_nav(ts)
    assert abs(nav[2] - 1.0404) < 1e-12     # 同合约复利（含 roll）
    assert abs(nav[3] - 1.0404) < 1e-12     # 换月不跨合约计盈亏
    assert abs(nav[4] - 1.0404 * 1.01) < 1e-12
    assert th.near_roll_nav([{"near": None, "near_s": None}]) == [None]
