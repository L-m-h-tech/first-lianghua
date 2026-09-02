# -*- coding: utf-8 -*-
"""真实手续费叠加、固定费折算、绩效统计与分档回归（第9/10轮，纯函数）。"""
import backtest


def _fee(mult=10, **kw):
    row = {"multiplier": mult, "open_amt_rate": 0.0, "open_per_lot": 0.0,
           "close_amt_rate": 0.0, "close_per_lot": 0.0,
           "today_amt_rate": 0.0, "today_per_lot": 0.0}
    row.update(kw)
    return row


def test_side_fee_amount_plus_per_lot():
    # 按金额费率与按手数固定费同时存在时相加
    row = _fee(open_amt_rate=1e-4, open_per_lot=3.0)
    ratio, yuan = backtest.side_fee(row, 3500.0, "open")
    notional = 3500 * 10
    assert abs(yuan - (notional * 1e-4 + 3)) < 1e-9
    assert abs(ratio - yuan / notional) < 1e-12


def test_side_fee_legs_and_guards():
    row = _fee(close_per_lot=5.0, today_per_lot=15.0, close_amt_rate=0.0)
    assert abs(backtest.side_fee(row, 3500, "close")[1] - 5.0) < 1e-9
    assert abs(backtest.side_fee(row, 3500, "today")[1] - 15.0) < 1e-9
    assert backtest.side_fee(None, 3500, "open") == (0.0, 0.0)
    assert backtest.side_fee(row, 0, "open") == (0.0, 0.0)
    assert backtest.side_fee(_fee(mult=0), 3500, "open") == (0.0, 0.0)


def test_metrics_from_returns():
    m = backtest.metrics_from_returns([0.01, -0.01, 0.02], 1)
    assert m["n"] == 3 and abs(m["win_rate"] - 2 / 3) < 1e-12
    expect_cum = 1.01 * 0.99 * 1.02 - 1
    assert abs(m["cumulative"] - expect_cum) < 1e-12
    assert m["max_dd"] >= 0
    assert backtest.metrics_from_returns([], 1) is None


def test_metrics_sharpe_zero_when_constant():
    m = backtest.metrics_from_returns([0.01, 0.01], 1)
    assert m["sharpe"] == 0.0          # 无波动不除零


def test_score_band():
    assert backtest.score_band(1.0) == "观望"
    assert backtest.score_band(3.0) == "轻仓"
    assert backtest.score_band(5.0) == "分批"
    assert backtest.score_band(-7.0) == "强信号"


def test_technical_score_sign_and_resonance():
    up = backtest.technical_score({"ret5": 0.02, "ret20": 0.03, "ma10": 100,
                                   "close": 102, "tech": {"resonance_score": 0.5}})
    down = backtest.technical_score({"ret5": -0.02, "ret20": -0.03, "ma10": 100,
                                     "close": 98, "tech": {"resonance_score": -0.5}})
    assert up > 0 and down < 0
    # 共振分被加进去
    base = backtest.technical_score({"ret5": 0, "ret20": 0, "tech": {}})
    plus = backtest.technical_score({"ret5": 0, "ret20": 0, "tech": {"resonance_score": 0.5}})
    assert abs(plus - base - 0.5) < 1e-9
