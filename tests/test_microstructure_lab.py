# -*- coding: utf-8 -*-
"""第54轮 G24 微结构/持仓/季节因子族实验台 tools/microstructure_lab.py 的零网络/零DB 单测（只测纯函数与渲染）。"""
import datetime as dt
import math
import os
import random
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOLS = os.path.join(_ROOT, "tools")
for p in (_ROOT, _TOOLS):
    if p not in sys.path:
        sys.path.insert(0, p)

import factor_health as fh            # noqa: E402
import microstructure_lab as ml       # noqa: E402


def _toy(seed=3, n=160):
    rnd = random.Random(seed)
    bysym = {}
    for sym, drift in (("AA", 0.0005), ("BB", -0.0002)):
        rows, c, oi = [], 100.0, 10000.0
        for t in range(n):
            r = drift + rnd.gauss(0, 0.01)
            c *= (1 + r)
            oi *= (1 + rnd.gauss(0, 0.003))
            d = dt.date(2025, 1, 1) + dt.timedelta(days=t)
            rows.append({"sym": sym, "date": d.isoformat(), "c": c,
                         "v": 1e5 + rnd.random() * 1e4, "oi": oi, "ret1d": r})
        bysym[sym] = rows
    return bysym


def test_pct_change():
    pc = ml.pct_change([100.0, 110.0, 121.0], 1)
    assert pc[0] is None
    assert pc[1] == pytest.approx(0.10)
    assert pc[2] == pytest.approx(0.10)
    # 窗口不足 / 基数非正 / 缺失 全部安全降级为 None
    assert ml.pct_change([1.0, 2.0], 5)[1] is None
    assert ml.pct_change([0.0, 1.0], 1)[1] is None
    assert ml.pct_change([None, 1.0], 1)[1] is None


def test_rolling_amihud():
    am = ml.rolling_amihud([0.01, 0.01, 0.01], [1000.0] * 3, [100.0] * 3, win=3, min_n=3)
    assert am[2] == pytest.approx(0.01 / 1e5)
    assert am[0] is None and am[1] is None          # 有效点不足
    # 成交额为 0 的点跳过、不计入
    am0 = ml.rolling_amihud([0.01, 0.02], [1000.0, 1000.0], [0.0, 100.0], win=2, min_n=2)
    assert am0[1] is None
    am1 = ml.rolling_amihud([0.01, 0.02], [1000.0, 1000.0], [0.0, 100.0], win=2, min_n=1)
    assert am1[1] == pytest.approx(0.02 / 1e5)


def test_skew_signs_and_rolling():
    symmetric = [-1.0, -0.5, 0.0, 0.5, 1.0] * 6
    assert abs(ml._skew(symmetric)) < 1e-9
    assert ml._skew([0.0, 0.0, 0.0, 0.0, 1.0, 10.0]) > 1.0
    assert ml._skew([0.0, 0.0, 0.0, 0.0, -1.0, -10.0]) < -1.0
    rs = ml.rolling_skew([None, 1.0, 2.0, -1.0, 0.5], win=5, min_n=3)
    assert rs[0] is None and rs[-1] is not None
    # 零方差不炸
    assert ml.rolling_skew([0.0, 0.0, 0.0], win=3, min_n=2)[-1] == 0.0


def test_idiovol_pure_market_vs_noise():
    mkt = [((i % 7) - 3) * 0.01 for i in range(120)]
    pure = [2.0 * m for m in mkt]
    iv0 = ml.rolling_idiovol(pure, mkt, win=120, min_n=60)
    assert iv0[-1] is not None and iv0[-1] < 1e-12
    noisy = [2.0 * m + 0.004 * (1 if i % 2 else -1) for i, m in enumerate(mkt)]
    iv1 = ml.rolling_idiovol(noisy, mkt, win=120, min_n=60)
    assert iv1[-1] > 0.003
    # 市场全缺 → None（不硬算）
    assert ml.rolling_idiovol(pure, [None] * 120, win=120, min_n=1)[-1] is None


def test_market_by_date_equal_weight():
    bysym = {"A": [{"date": "d1", "ret1d": 0.02}, {"date": "d2", "ret1d": 0.0}],
             "B": [{"date": "d1", "ret1d": 0.04}, {"date": "d2", "ret1d": None}]}
    m = ml.market_by_date(bysym)
    assert m["d1"] == pytest.approx(0.03)
    assert m["d2"] == pytest.approx(0.0)


def test_build_series_alignment_and_pit():
    bysym = _toy()
    series = ml.build_factor_series(bysym)
    for sym, rows in bysym.items():
        n = len(rows)
        for f in ("doi1", "doi5", "amihud20", "idiovol60", "skew60"):
            assert len(series[sym][f]) == n
        assert series[sym]["doi1"][0] is None
        assert series[sym]["doi5"][:5] == [None] * 5       # 5日变化前5点无值=无未来
        assert all(v is None or v >= 0 for v in series[sym]["amihud20"])
        assert all(v is None or v >= 0 for v in series[sym]["idiovol60"])


def test_forward_curve_perfect_and_guard():
    bysym = _toy()
    perfect = {}
    for sym, rows in bysym.items():
        rows = sorted(rows, key=lambda r: r["date"])
        fwd5 = fh.forward_map([r["c"] for r in rows], (5,))[5]
        perfect[sym] = {"g": [v if v is not None else 0.0 for v in fwd5]}
    curve = ml.factor_forward_curve(bysym, perfect, "g", horizons=(1, 5), min_pairs=20)
    assert curve[5]["ic"] > 0.99 and curve[5]["q5q1"] > 0 and curve[5]["mono"] >= 0.9
    assert len(curve[5]["q_means"]) == 5
    # 样本不足不给 IC（不编造）
    tiny = {"A": [{"date": "2025-01-0%d" % i, "c": 100.0 + i} for i in range(1, 6)]}
    ts = {"A": {"g": [0.1, 0.2, 0.3, 0.4, 0.5]}}
    c2 = ml.factor_forward_curve(tiny, ts, "g", horizons=(1,), min_pairs=40)
    assert c2[1]["ic"] is None and c2[1]["q5q1"] is None


def test_calendar_seasonality():
    bysym = _toy()
    # 全部 1 月改成固定 +0.02
    for rows in bysym.values():
        for r in rows:
            if r["date"][5:7] == "01":
                r["ret1d"] = 0.02
    sm = ml.calendar_seasonality(bysym, "month")
    assert sm[1]["mean"] == pytest.approx(0.02) and sm[1]["uprate"] == pytest.approx(1.0)
    sw = ml.calendar_seasonality(_toy(), "weekday")
    assert set(sw) <= set(range(7))
    # 坏日期不炸、被跳过
    bad = {"A": [{"date": "xxxx", "ret1d": 0.01}]}
    assert ml.calendar_seasonality(bad, "month") == {}


def test_render_sections():
    bysym = _toy()
    series = ml.build_factor_series(bysym)
    res = {f: ml.factor_forward_curve(bysym, series, f, min_pairs=20) for f, _, _ in ml.FACTORS}
    sm = ml.calendar_seasonality(bysym, "month")
    sw = ml.calendar_seasonality(bysym, "weekday")
    meta = {"n_sym": 2, "d0": "2025-01-01", "d1": "2025-06-09", "n_rows": 320,
            "horizons": list(ml.HORIZONS), "n_q": 5, "amihud_scale": ml.AMIHUD_SCALE,
            "windows": {"amihud": 20, "idiovol": 60, "skew": 60}}
    txt = ml.render(meta, res, sm, sw)
    for marker in ("【一】", "【二】", "【三】", "Amihud", "HP/SP"):
        assert marker in txt
