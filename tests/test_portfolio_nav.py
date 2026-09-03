# -*- coding: utf-8 -*-
"""第49轮 G5⑤ 多品种组合历史净值曲线：portfolio_lab.nav_curve/drawdown_window/rolling_proxy 的 idx 对齐，零网络零面板。"""
import pytest

import portfolio_lab as pl
import config


def test_nav_curve_compound_and_empty():
    assert pl.nav_curve([]) == []
    nav = pl.nav_curve([0.1, -0.1, 0.0])
    assert abs(nav[0] - 1.1) < 1e-12
    assert abs(nav[1] - 0.99) < 1e-12      # 1.1*0.9
    assert abs(nav[2] - 0.99) < 1e-12
    nav2 = pl.nav_curve([0.0, 0.0], start=100.0)
    assert nav2 == [100.0, 100.0]


def test_nav_curve_does_not_mutate_input():
    daily = [0.01, -0.02]
    pl.nav_curve(daily)
    assert daily == [0.01, -0.02]


def test_drawdown_window_handcalc():
    # 1.0→1.2(峰)→0.9：回撤 1-0.9/1.2=0.25，峰位1、谷位2
    dw = pl.drawdown_window([1.0, 1.2, 0.9], idxs=[0, 1, 2], dates=["d0", "d1", "d2"])
    assert abs(dw["maxdd"] - 0.25) < 1e-12
    assert dw["peak_i"] == 1 and dw["trough_i"] == 2
    assert dw["peak_date"] == "d1" and dw["trough_date"] == "d2"


def test_drawdown_window_safety():
    assert pl.drawdown_window([])["maxdd"] == 0.0
    r = pl.drawdown_window([1.0])
    assert r["maxdd"] == 0.0 and r["trough_i"] is None
    # 单调上涨无回撤
    r2 = pl.drawdown_window([1.0, 1.1, 1.2])
    assert r2["maxdd"] == 0.0 and r2["peak_i"] in (None, 0)


def test_rolling_proxy_idx_aligned_and_nav_equal_length():
    """四方法 daily/idx 等长、idx 从 lookback 起（无未来），净值逐日对齐可同表落 CSV。"""
    rm, _ = pl._toy_panel()
    dates, syms, mat = pl.dense_matrix(rm, analysis_days=260, coverage_min=1.0)
    proxy = pl.rolling_proxy(mat, methods=config.PC_METHODS, lookback=60, rebal=20,
                             shrink=0.1, cap=0.5)
    base = proxy["equal"]
    assert base["idx"][0] == 60 and base["idx"][-1] == len(mat) - 1
    for m in config.PC_METHODS:
        assert len(proxy[m]["daily"]) == len(proxy[m]["idx"])
        nav = pl.nav_curve(proxy[m]["daily"])
        assert len(nav) == len(base["daily"])               # 四方法逐日对齐
        assert proxy[m]["idx"] == base["idx"]               # 共用同一再平衡日历
        # idx 严格递增、不越界
        assert all(base["idx"][k] < len(dates) for k in range(len(base["idx"])))
        assert all(base["idx"][k] < base["idx"][k + 1]
                   for k in range(len(base["idx"]) - 1))


def test_gmv_lower_vol_but_aligned_on_toy():
    """玩具面板上 gmv 波动不高于等权（风险型核心承诺），且净值曲线有限非 NaN。"""
    import math
    rm, _ = pl._toy_panel()
    _, _, mat = pl.dense_matrix(rm, analysis_days=260, coverage_min=1.0)
    proxy = pl.rolling_proxy(mat, methods=("equal", "gmv"), lookback=60, rebal=20)
    se = pl.perf_stats(proxy["equal"]["daily"])
    sg = pl.perf_stats(proxy["gmv"]["daily"])
    assert sg["ann_vol"] <= se["ann_vol"] + 1e-9
    for v in pl.nav_curve(proxy["gmv"]["daily"]):
        assert math.isfinite(v) and v > 0
