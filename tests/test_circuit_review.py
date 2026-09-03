# -*- coding: utf-8 -*-
"""第50轮 G5④ 熔断阈值历史校准台 tools/circuit_review.py 的零网络/零DB 单测（只测纯函数与渲染）。"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOLS = os.path.join(_ROOT, "tools")
for p in (_ROOT, _TOOLS):
    if p not in sys.path:
        sys.path.insert(0, p)

import circuit_breaker as cb          # noqa: E402
import circuit_review as cr           # noqa: E402


def test_loss_and_forward_compound():
    assert cr.loss_of(-0.03) == pytest.approx(0.03)
    assert cr.loss_of(0.02) < 0
    d = [0.0, 0.1, -0.1]
    assert cr.forward_compound(d, 0, 2) == pytest.approx(1.1 * 0.9 - 1)
    # 后续不足 / 非法下标 → None（不抛）
    assert cr.forward_compound(d, 1, 2) is None
    assert cr.forward_compound(d, -1, 1) is None
    assert cr.forward_compound([], 0, 1) is None


def test_threshold_events_inclusive_and_daily_independent():
    daily = [0.01, -0.031, 0.02, -0.01, -0.04, 0.0]
    # 损失≥3%（含等号由 classify 口径一致）：索引1(-3.1%)、4(-4%)
    assert cr.threshold_events(daily, 0.03) == [1, 4]
    assert cr.threshold_events([], 0.03) == []


def test_level_counts_three_bands():
    daily = [0.01, -0.031, 0.02, -0.01, -0.04, 0.0, -0.06]
    cnt, idx = cr.level_counts(daily, {"warn": 0.02, "halt": 0.03, "delever": 0.05})
    assert cnt[cb.HALT] == 2 and idx[cb.HALT] == [1, 4]
    assert cnt[cb.DELEVER] == 1 and idx[cb.DELEVER] == [6]
    assert cnt[cb.WARN] == 0  # -1%/-3.1%/-4%/-6% 都不落在 [2%,3%) 的纯 warn 带


def test_conditional_forward_continuation_vs_bounce():
    # 续跌序列：跌≥3% 后次日必跌 → 条件下跌占比100%、均值为负
    seq = [0.0, -0.04, -0.03, -0.02, 0.0, -0.04, -0.02, -0.01]
    ev = cr.threshold_events(seq, 0.03)
    cf = cr.conditional_forwards(seq, ev, horizons=(1, 3))
    assert cf["conditional"][1]["n"] == 3
    assert cf["conditional"][1]["down_rate"] == 1.0
    assert cf["conditional"][1]["mean"] < 0
    # 基准 = 所有后面还有 max_h(=3) 日的时点
    assert cf["baseline"][1]["n"] == len(seq) - 3
    # 反弹序列：跌后次日全涨 → 条件均值为正、下跌占比0（熔断误杀可被识别）
    bounce = [0.0, -0.04, 0.03, 0.0, -0.04, 0.02, 0.0]
    cfb = cr.conditional_forwards(bounce, cr.threshold_events(bounce, 0.03), horizons=(1,))
    assert cfb["conditional"][1]["mean"] > 0
    assert cfb["conditional"][1]["down_rate"] == 0.0


def test_dist_empty_and_stats():
    assert cr._dist([])["n"] == 0 and cr._dist([])["mean"] is None
    d = cr._dist([-0.02, 0.02, -0.04])
    assert d["n"] == 3 and d["mean"] == pytest.approx(-0.0133333, abs=1e-6)
    assert d["median"] == pytest.approx(-0.02)
    assert d["down_rate"] == pytest.approx(2 / 3)


def test_sweep_monotone_and_share_bounded():
    daily = [0.0, -0.01, -0.02, -0.03, -0.04, 0.01]
    dates = ["d%d" % i for i in range(len(daily))]
    sw = cr.sweep_halt(dates, daily, grid=(0.01, 0.02, 0.03), horizons=(1, 5))
    ntrig = [r["n_trigger"] for r in sw]
    assert ntrig == sorted(ntrig, reverse=True)          # 阈值越高触发越少
    assert all(0.0 <= r["share"] <= 1.0 for r in sw)
    assert sw[0]["dates"] and all(isinstance(x, str) for x in sw[0]["dates"])


def test_analyze_method_structure_and_empty_safe():
    daily = [0.01, -0.031, 0.02, -0.04, 0.0]
    dates = ["d%d" % i for i in range(len(daily))]
    am = cr.analyze_method(dates, daily, horizons=(1, 3))
    assert am["n_days"] == 5 and am["counts"][cb.HALT] == 2
    assert len(am["sweep"]) == len(cr.SWEEP_GRID)
    assert am["calib_n"] >= 0 and "calib_forward" in am
    assert am["worst_day_loss"] == pytest.approx(0.04)
    empty = cr.analyze_method([], [])
    assert empty["counts"][cb.HALT] == 0
    assert empty["worst_day_loss"] == 0.0
    assert all(r["n_trigger"] == 0 for r in empty["sweep"])


def test_render_contains_sections_and_numbers():
    daily = []
    base = [0.002] * 40
    # 造若干 1%~3% 的下跌日使三档/网格都有内容
    for k in (5, 12, 20, 30):
        base[k] = -0.015
    base[25] = -0.032
    dates = ["2025-%02d-%02d" % ((i // 28) + 1, (i % 28) + 1) for i in range(len(base))]
    per = {m: cr.analyze_method(dates, base, horizons=cr.HORIZONS) for m in cr.METHODS}
    meta = {"n_universe": 61, "n_all": 64, "date_first": dates[0], "date_last": dates[-1],
            "n_mat": 504, "n_proxy": len(base)}
    txt = cr.render(meta, per)
    for kw in ("【一】", "【二】", "【三】", "warn", "halt", "delever", "条件", "基准", "诚实边界"):
        assert kw in txt


def test_selftest_passes():
    assert cr.selftest() == 0
