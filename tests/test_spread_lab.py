# -*- coding: utf-8 -*-
"""第55轮 G12 产业链/跨期价差监控 tools/spread_lab.py 的零网络/零DB 单测（只测纯函数与渲染）。"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOLS = os.path.join(_ROOT, "tools")
for p in (_ROOT, _TOOLS):
    if p not in sys.path:
        sys.path.insert(0, p)

import spread_lab as sl       # noqa: E402


def test_rolling_z():
    z = sl.rolling_z([1.0] * 50 + [3.0], win=60, min_n=20)
    assert z[-1] is not None and z[-1] > 4
    assert sl.rolling_z([1.0] * 60, win=60, min_n=20)[-1] is None     # 零方差
    assert sl.rolling_z([1.0, 2.0], win=60, min_n=20)[-1] is None     # 样本不足
    # 前面点不足全为 None（PIT 无前视）
    z2 = sl.rolling_z([float(i) for i in range(50)], win=120, min_n=40)
    assert z2[38] is None and z2[49] is not None


def test_rolling_percentile():
    xs = [float(i) for i in range(60)]
    pc = sl.rolling_percentile(xs, win=60, min_n=20)
    assert pc[-1] == pytest.approx(1.0)
    assert sl.rolling_percentile(xs, win=60, min_n=80)[-1] is None


def test_spread_pct_and_curve():
    ts = [{"near_s": 101.0, "next_s": 100.0, "carry_nn": 0.05},
          {"near_s": 99.0, "next_s": 100.0, "carry_nn": -0.05},
          {"near_s": None, "next_s": 100.0, "carry_nn": None}]
    sp = sl.spread_pct_series(ts)
    assert sp[0] == pytest.approx(0.01) and sp[1] == pytest.approx(-0.01)
    assert sp[2] is None


def test_term_symbol_stat():
    series = [{"date": "2025-%02d-%02d" % (t // 28 + 1, t % 28 + 1),
               "near": "X2510", "next": "X2511", "near_s": 100.0, "next_s": 101.0 + 0.01 * t,
               "carry_nn": -0.04, "slope": -0.01, "oi_sum": 1000, "n_live": 8} for t in range(80)]
    st = sl.term_symbol_stat(series)
    assert st["curve"] == "contango" and st["near"] == "X2510"
    assert st["spread_z"] is not None and 0.0 <= st["spread_pctile"] <= 1.0
    assert sl.term_symbol_stat([]) is None
    # 近高远低 -> back
    series2 = [dict(series[0], near_s=102.0, next_s=100.0)] * 60
    st2 = sl.term_symbol_stat(series2)
    assert st2["curve"] == "back"


def test_aligned_ratio():
    od, r = sl.aligned_ratio(["d1", "d2", "d3"], [2.0, 4.0, 6.0],
                             ["d2", "d3", "d4"], [2.0, 2.0, 3.0])
    assert od == ["d2", "d3"]
    assert r[0] == pytest.approx(2.0) and r[1] == pytest.approx(3.0)
    od2, r2 = sl.aligned_ratio(["d1"], [0.0], ["d1"], [1.0])
    assert od2 == [] and r2 == []


def test_chain_stat():
    ratio = [1.0 + 0.001 * i for i in range(80)] + [1.6]
    cs = sl.chain_stat(["d%d" % i for i in range(81)], ratio, chg_win=60)
    assert cs["z"] > 1.0 and cs["chg60"] is not None and cs["n"] == 81
    assert sl.chain_stat([], []) is None
    # 样本不足不给 z
    cs2 = sl.chain_stat(["d1", "d2"], [1.0, 1.1], win=120, min_n=40)
    assert cs2["z"] is None and cs2["ratio"] == pytest.approx(1.1)


def test_aligned_margin_formula():
    # 手算压榨利润 0.785*M + 0.185*Y - A - fee
    cm = {"M": {"d1": 3000.0, "d2": 3100.0},
          "Y": {"d1": 9000.0, "d2": 9100.0},
          "A": {"d1": 5000.0, "d2": 5050.0}}
    w = (("M", 0.785), ("Y", 0.185), ("A", -1.0))
    od, v = sl.aligned_margin(cm, w, 110.0)
    assert od == ["d1", "d2"]
    assert v[0] == pytest.approx(0.785 * 3000 + 0.185 * 9000 - 5000 - 110)
    # 缺一条腿 -> 空
    cm.pop("A")
    assert sl.aligned_margin(cm, w, 110.0) == ([], [])
    # 非正价跳过
    cm2 = {"M": {"d1": 0.0}, "Y": {"d1": 9000.0}, "A": {"d1": 5000.0}}
    assert sl.aligned_margin(cm2, w) == ([], [])


def test_margin_stat():
    vals = [100.0 + i for i in range(80)] + [300.0]
    ms = sl.margin_stat(["d%d" % i for i in range(81)], vals, chg_win=60)
    assert ms["z"] > 1.0 and ms["chg60"] == pytest.approx(180.0) and ms["n"] == 81
    assert sl.margin_stat([], []) is None
    ms2 = sl.margin_stat(["d1"], [5.0], win=120, min_n=40)
    assert ms2["z"] is None and ms2["value"] == pytest.approx(5.0)


def test_last_valid():
    assert sl._last_valid([1.0, None, 2.0, None]) == (2, 2.0)
    assert sl._last_valid([None]) == (None, None)


def test_render_sections_and_breadth():
    meta = {"term_symbols": 1, "term_stats_n": 1, "d0": "2025-01-01", "d1": "2025-08-01",
            "panel_d0": "2025-01-01", "panel_d1": "2025-08-01", "z_win": 120, "chg_win": 60,
            "chain_n": 1, "margin_n": 1, "backwardation": {"back": 0, "n": 1}}
    one = [{"sym": "X", "date": "2025-08-01", "near": "X2510", "next": "X2511",
            "near_s": 100.0, "next_s": 101.0, "spread_pct": -0.0099, "carry_ann": -0.04,
            "slope": -0.01, "curve": "contango", "spread_z": -1.8, "spread_pctile": 0.1,
            "carry_z": -1.2, "oi_sum": 1000, "n_live": 8, "n_days": 80}]
    cc = [{"sector": "贵金属", "name": "金银比价(金/银)", "a": "AU", "b": "AG",
           "stat": {"date": "2025-08-01", "ratio": 80.0, "z": 1.7, "pctile": 0.9,
                    "chg60": 0.05, "n": 80}}]
    mg = [{"sector": "黑色", "name": "卷螺差(元/吨)", "note": "HC-RB",
           "stat": {"date": "2025-08-01", "value": 12.0, "z": 0.3, "pctile": 0.6,
                    "chg60": 5.0, "n": 80}}]
    txt = sl.render(meta, one, cc, mg, {"back": 0, "n": 1})
    for marker in ("【一】", "【二】", "【三】", "【四】", "backwardation 广度", "金银比价", "卷螺差"):
        assert marker in txt
