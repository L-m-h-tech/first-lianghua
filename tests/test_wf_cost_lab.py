# -*- coding: utf-8 -*-
"""G27②③（第45轮）wf_cost_lab 零网络/零DB 确定性测试。

只测纯函数层（统计/成本曲面/break-even/WF稳定度/换手容量/成稿），真实回放 run_symbol 读分钟库、
属真实冒烟（不进 pytest）。曲面通过注入假 runner 复现，不依赖 intraday_backtest/storage。"""
import math

import pytest

import wf_cost_lab as W


# ---------- 统计 ----------
def test_per_trade_sharpe_and_compound():
    assert W.per_trade_sharpe([1]) == 0.0
    assert W.per_trade_sharpe([1, 1, 1]) == 0.0  # 标准差0
    assert W.per_trade_sharpe([1, -1, 1, -1]) == pytest.approx(0.0, abs=1e-12)
    assert W.compound([0.1, -0.1]) == pytest.approx(1.1 * 0.9 - 1)
    assert W.compound([]) == 0.0


def test_summarize_trades_basic_and_empty():
    tr = [{"net": 0.1, "gross": 0.12}, {"net": -0.05, "gross": -0.03}, {"net": 0.02, "gross": 0.04}]
    sm = W.summarize_trades(tr)
    assert sm["n_trades"] == 3
    assert sm["win_rate"] == pytest.approx(2 / 3)
    assert sm["sum_gross"] == pytest.approx(0.13)
    assert sm["sum_cost"] == pytest.approx(0.13 - 0.07)
    assert sm["cost_per_trade"] == pytest.approx((0.13 - 0.07) / 3)
    empty = W.summarize_trades([])
    assert empty["n_trades"] == 0 and empty["win_rate"] is None and empty["total_compound"] == 0.0


# ---------- ③ 成本曲面 ----------
def _linear_runner(per_trade_gross=0.002):
    def runner(fee, slip):
        r = per_trade_gross - 2 * fee - 2 * slip
        return [{"net": r, "gross": per_trade_gross} for _ in range(50)]
    return runner


def test_cost_surface_monotone_and_base_locator():
    surf = W.build_cost_surface(_linear_runner(), (0.0, 5e-5, 1e-3), (0.0, 1e-4, 1e-3))
    mat = W.surface_matrix(surf)
    assert mat[0][0] > mat[-1][-1]            # 零成本最优、高成本最差
    # 沿 fee、沿 slip 都单调不增
    si = surf["base"]["si"]
    col = [mat[i][si] for i in range(3)]
    assert col[0] >= col[1] >= col[2]
    row = mat[surf["base"]["fi"]]
    assert row[0] >= row[1] >= row[2]
    assert surf["base"]["fi"] == 1 and surf["base"]["si"] == 1  # fee 5e-5→idx1；slip 1e-4→idx1


def test_breakeven_cases():
    # 有转负档
    surf = W.build_cost_surface(_linear_runner(0.002), (0.0, 5e-5, 1e-3), (0.0, 1e-4, 1e-3))
    be = W.breakeven_cost(surf)
    assert be["fee"]["first_negative"] is not None
    assert be["fee"]["safety_x"] and be["fee"]["safety_x"] > 0
    # 全程为正：无转负、安全垫 None
    win = W.build_cost_surface(_linear_runner(0.05), (0.0, 1e-4), (0.0, 1e-4))
    assert W.breakeven_cost(win)["slip"]["first_negative"] is None
    assert W.breakeven_cost(win)["slip"]["safety_x"] is None
    # 基准已亏：安全垫 0
    lose = W.build_cost_surface(_linear_runner(-0.01), (0.0, 5e-5), (0.0, 1e-4))
    assert W.breakeven_cost(lose)["fee"]["safety_x"] == 0.0


def test_nearest_idx():
    assert W._nearest_idx((0.0, 1e-4, 2e-4), 1.1e-4) == 1
    assert W._nearest_idx((0.0,), 9) == 0


# ---------- ② WF 稳定度 ----------
def _seg(chosen, oos=0.1, is_=0.2, beat=True):
    return {"chosen": chosen, "is_sharpe": is_, "oos_sharpe": oos, "oos_best": oos + 0.1,
            "oos_median": 0.0, "beat_median": beat}


def test_wf_stability_stable():
    segs = [_seg(0, oos=0.3, is_=0.5) for _ in range(5)]
    ws = W.wf_stability(segs, ["a", "b"])
    assert ws["grade"] == "稳定" and ws["top_share"] == 1.0 and ws["switches"] == 0
    assert ws["is_oos_decay"] == pytest.approx(-0.2)
    assert ws["selection_regret"] == pytest.approx(0.1)
    assert ws["oos_positive_rate"] == 1.0 and ws["beat_median_rate"] == 1.0


def test_wf_stability_drift_and_empty():
    segs = [_seg(i % 4, oos=-0.1, beat=False) for i in range(10)]
    wd = W.wf_stability(segs, ["p0", "p1", "p2", "p3"])
    assert wd["grade"] == "漂移" and wd["switches"] == 9
    assert wd["top_share"] == pytest.approx(0.3)
    assert wd["oos_positive_rate"] == 0.0
    assert W.wf_stability([], ["a"])["grade"] == "样本不足"


def test_wf_stability_out_of_range_chosen_defensive():
    ws = W.wf_stability([_seg(99)], ["a", "b"])
    assert ws["chosen_sequence"] == ["?"] and ws["top_param"] == "?"


def test_parse_combo_name():
    assert W._parse_combo_name("e1.5/s2/t3") == (1.5, 2.0, 3.0)
    assert W._parse_combo_name("e2/s1.2/t1.5") == (2.0, 1.2, 1.5)


# ---------- 换手容量 ----------
class _DT:
    def __init__(self, d):
        self.d = d

    def strftime(self, f):
        return self.d


def _bars(n_days=2, per_day=10, px=100.0, vol=100.0):
    out = []
    for i in range(n_days * per_day):
        out.append({"dt": _DT("2026-09-%02d" % (1 + i // per_day)), "c": px, "v": vol})
    return out


def test_capacity_numbers():
    bars = _bars()
    trades = [{"entry_px": 100.0}, {"entry_px": 100.0}]
    cap = W.estimate_turnover_capacity(bars, trades, 10.0, participation_cap=0.10, days_year=243)
    assert cap["n_days"] == 2 and cap["n_trades"] == 2
    assert cap["trades_per_day"] == pytest.approx(1.0)
    assert cap["mkt_daily_notional"] == pytest.approx(1_000_000.0)   # 10根×100手×100价×10乘
    assert cap["notional_per_lot"] == pytest.approx(1000.0)
    assert cap["max_lots_per_trade"] == pytest.approx(100.0)         # 10%参与率
    assert cap["annual_turnover_lots_1lot"] == pytest.approx(243.0)


def test_capacity_empty_and_zero_multiplier():
    cap = W.estimate_turnover_capacity([], [], 10)
    assert cap["n_days"] == 0 and cap["max_lots_per_trade"] is None
    # 乘数0 → 名义0 → 上限 None（不抛）
    cap0 = W.estimate_turnover_capacity(_bars(), [{"entry_px": 100}], 0)
    assert cap0["notional_per_lot"] == 0.0 and cap0["max_lots_per_trade"] is None


def test_capacity_string_dt_fallback():
    bars = [{"dt": "2026-09-01 10:00", "c": 50.0, "v": 10.0}]
    cap = W.estimate_turnover_capacity(bars, [{"entry_px": 50.0}], 5.0)
    assert cap["n_days"] == 1


# ---------- 成稿 ----------
def _fake_result():
    surf = W.build_cost_surface(_linear_runner(), (0.0, 5e-5, 1e-3), (0.0, 1e-4, 1e-3))
    st = W.wf_stability([_seg(0) for _ in range(5)], ["e1/s1/t1", "e2/s2/t2"])
    cap = W.estimate_turnover_capacity(_bars(), [{"entry_px": 100.0}] * 2, 10.0)
    be = W.breakeven_cost(surf)
    return {"sym": "RB", "name": "螺纹", "period": 30, "bars": 1000, "n_days": 80,
            "n_combos": 18, "wf_train": 20, "wf_test": 10, "stability": st,
            "best_param": "e1/s1/t1", "surface": surf, "breakeven": be,
            "base_cell": surf["rows"][1]["cells"][1], "capacity": cap}


def test_render_symbol_and_full_report():
    res = _fake_result()
    rep = W.render_symbol(res)
    for kw in ("walk-forward 参数稳定性", "成本敏感性曲面", "安全垫", "换手/容量", "选中参数轨迹"):
        assert kw in rep
    full = W.build_report([res], ["RB"], 30)
    assert "总览" in full and "螺纹" in full
    # 空结果不抛
    empty = W.build_report([], ["XX"], 30)
    assert "无有效品种" in empty


def test_json_payload_no_nan():
    import json
    pay = W.build_json_payload([_fake_result()])
    assert pay["n_symbols"] == 1
    json.dumps(pay, allow_nan=False)  # 不含 NaN/Infinity
