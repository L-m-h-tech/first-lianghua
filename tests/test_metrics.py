# -*- coding: utf-8 -*-
"""第29轮 G3 完整绩效指标包回归：metrics.py 纯函数（零网络、零第三方依赖）。

核心手算断言集中在 metrics.selftest()（可独立 `python metrics.py --selftest` 运行），
这里再固化：selftest 全绿、tear_sheet 结构、与 portfolio 绩效字典的集成键。
"""
import math

import metrics


def test_selftest_all_hand_computed_assertions_pass():
    checks = metrics.selftest()
    assert checks and all(ok for _, ok in checks)
    assert len(checks) >= 40                 # 防止后续误删断言导致“假覆盖”


def test_tear_sheet_fixed_keys_and_independent_degrade():
    rs = [0.01, -0.02, 0.03, -0.01, 0.02]
    sheet = metrics.tear_sheet(rs)
    expected = {"n", "cumulative", "mean", "annualized", "cagr", "volatility",
                "sharpe", "sortino", "calmar", "omega", "ulcer", "max_drawdown",
                "var", "cvar", "var_alpha", "drawdown", "rolling_sharpe", "monthly"}
    assert expected <= set(sheet)
    assert sheet["n"] == 5
    assert len(sheet["drawdown"]) == 5 and len(sheet["rolling_sharpe"]) == 5
    # 未传 dates -> monthly 为 None，但不影响其它指标
    assert sheet["monthly"] is None
    # 空序列：所有风险指标安全降级 None，不抛
    empty = metrics.tear_sheet([])
    assert empty["n"] == 0 and empty["sharpe"] is None and empty["drawdown"] == []


def test_quantile_linear_matches_hand_values():
    s = [-0.02, -0.01, 0.01, 0.02, 0.03]
    assert math.isclose(metrics.quantile_linear(s, 0.05), -0.018, abs_tol=1e-12)
    assert math.isclose(metrics.quantile_linear(s, 0.5), 0.01)
    assert metrics.quantile_linear([], 0.5) is None
    assert metrics.quantile_linear([0.7], 0.1) == 0.7


def test_daily_last_equity_collapses_intraday():
    dts = ["2026-01-05 09:00", "2026-01-05 15:00", "2026-01-06 09:00"]
    days, eq = metrics.daily_last_equity(dts, [100.0, 101.0, 103.0])
    assert days == ["2026-01-05", "2026-01-06"] and eq == [101.0, 103.0]
    # 脏值/无法解析日期被跳过
    d2, e2 = metrics.daily_last_equity(["x", "2026-01-06"], [1.0, 2.0])
    assert d2 == ["2026-01-06"] and e2 == [2.0]


def test_excursion_long_short_symmetric():
    mfe, mae = metrics.excursion(1, 100.0, [102, 99, 103, 101])
    assert math.isclose(mfe, 0.03) and math.isclose(mae, 0.01)
    mfe2, mae2 = metrics.excursion(-1, 100.0, [98, 101, 97])
    assert math.isclose(mfe2, 0.03) and math.isclose(mae2, 0.01)
    # 非法入场价 / 空路径安全
    assert metrics.excursion(1, 0, [1, 2]) == (None, None)
    assert metrics.excursion(1, 100, []) == (None, None)


def test_portfolio_performance_exposes_g3_keys():
    """集成：portfolio.performance() 在旧键之外新增 G3 键，且旧键口径不变。"""
    import portfolio
    pf = portfolio.Portfolio(1_000_000.0, {})
    # 无持仓逐日 record 也能产出全现金权益曲线，G3 新键应存在（值允许 None/0）
    from datetime import datetime, timedelta
    for i in range(5):
        pf.record(datetime(2026, 4, 1) + timedelta(days=i), {"RB0": 3000.0 + i})
    perf = pf.performance()
    for k in ("calmar", "omega", "ulcer", "var95", "cvar95", "monthly",
              "profit_factor", "max_win_streak", "max_loss_streak", "mae_mfe"):
        assert k in perf
    # 旧键依旧存在且类型稳定
    for k in ("total_ret", "ann_ret", "sharpe", "sortino", "max_dd", "win_rate"):
        assert k in perf
