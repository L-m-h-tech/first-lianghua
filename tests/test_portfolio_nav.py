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


# ==================== 第52轮 G26续二 gross 网格×换手成本 / 全品种口径 ====================
def test_gross_net_daily_charge_only_at_segment_start():
    daily = [0.01, -0.02, 0.0, 0.03]
    bounds = [{"start": 60, "length": 2, "entry_turnover": 0.5},
              {"start": 62, "length": 2, "entry_turnover": 0.2}]
    n0, c0 = pl.gross_net_daily(daily, bounds, 1.0, 0.0)
    assert all(abs(a - b) < 1e-15 for a, b in zip(n0, daily)) and sum(c0) == 0
    n2, c2 = pl.gross_net_daily(daily, bounds, 2.0, 1e-3)
    assert abs(c2[0] - 1e-3) < 1e-15 and c2[1] == 0.0 and abs(c2[2] - 4e-4) < 1e-15 and c2[3] == 0.0
    assert abs(n2[0] - 0.019) < 1e-15 and abs(n2[1] + 0.04) < 1e-15
    assert abs(n2[2] + 4e-4) < 1e-15 and abs(n2[3] - 0.06) < 1e-15
    # 首段无 prev（entry_turnover=None）不收成本
    nf, cf = pl.gross_net_daily([0.01, 0.01], [{"start": 0, "length": 2, "entry_turnover": None}], 1.0, 1e-3)
    assert sum(cf) == 0 and nf == [0.01, 0.01]


def test_gross_grid_monotone_and_cost_drag():
    rm, _ = pl._toy_panel()
    _, _, mat = pl.dense_matrix(rm, analysis_days=260, coverage_min=1.0)
    proxy = pl.rolling_proxy(mat, methods=("equal", "gmv"), lookback=60, rebal=20)
    grid = pl.gross_cost_grid(proxy, (1.0, 1.2, 1.5), 1.5e-4, methods=("equal", "gmv"))
    for m, rows in grid.items():
        assert [r["gross"] for r in rows] == [1.0, 1.2, 1.5]
        assert rows[2]["ann_vol_net"] >= rows[0]["ann_vol_net"]      # 杠杆放大波动
        for r in rows:
            assert r["sharpe_net"] <= r["sharpe_gross"] + 1e-9       # 成本只减不增
            assert r["ann_cost_drag"] >= 0


def test_dense_matrix_fill_missing_keeps_all():
    rm = {"A": {"d1": 0.01, "d2": 0.0, "d3": -0.01},
          "B": {"d1": 0.02, "d3": 0.01}}                            # B 缺 d2
    d_dense, sy_dense, m_dense = pl.dense_matrix(rm, analysis_days=3, coverage_min=0.95)
    assert sy_dense == ["A"] and len(m_dense) == 3                  # 默认稠密：稀疏品种剔除
    d_all, sy_all, m_all = pl.dense_matrix(rm, analysis_days=3, fill_missing=True)
    assert sy_all == ["A", "B"] and len(m_all) == 3                 # 全品种：缺失补0、日期不剔
    assert m_all[1][1] == 0.0 and m_all[0][1] == 0.02


def test_all_universe_gross_grid_runs_three_levels():
    # 续二①：全品种口径（fill_missing，含稀疏品种）也要跑满 1.0/1.2/1.5 三档 gross×换手成本
    rm, _ = pl._toy_panel()
    rm["S4"] = {"2025-260": 0.01}                                   # 极稀疏品种，稠密口径会被剔除
    _, sy_all, m_all = pl.dense_matrix(rm, analysis_days=260, fill_missing=True)
    assert "S4" in sy_all and len(sy_all) == 5
    proxy_all = pl.rolling_proxy(m_all, lookback=60, rebal=20, shrink=0.1, cap=0.5)
    grid_all = pl.gross_cost_grid(proxy_all, (1.0, 1.2, 1.5), 1.5e-4)
    assert set(grid_all) == set(proxy_all)                          # 四方法全出
    for m, rows in grid_all.items():
        assert [r["gross"] for r in rows] == [1.0, 1.2, 1.5]        # 三档齐全（不再只有 gross=1）
        assert rows[2]["ann_vol_net"] >= rows[0]["ann_vol_net"]     # 杠杆放大波动
        assert rows[0]["gross"] == 1.0
        for r in rows:
            assert r["sharpe_net"] <= r["sharpe_gross"] + 1e-9      # 成本只减不增
            assert r["ann_cost_drag"] >= 0.0
