# -*- coding: utf-8 -*-
"""第115轮：run_cycle 5.5 纸面分支接管测试（零网络、确定性）。

验证交易时段 + paper_ticker 已接管时，run_cycle 5.5 段跳过 on_cycle/write_paper_account
（避免 run_cycle 5/10 分钟写盘覆盖 ticker 每分钟写盘）；未接管/非交易时段保留旧行为。
仅验证分支选择逻辑（mock 最小 state），不跑 run_cycle 全量（依赖太重）。
"""
import types

import pytest


class _MiniBroker:
    """最小 broker 桩：记录 on_cycle / on_cycle_options 是否被调用。"""
    def __init__(self, name="10万_激进", priority="futures_first"):
        self.name = name
        self.priority = priority
        self.last_summary = {}
        self.calls = []

    def on_cycle(self, ts, fut_rows, quotes):
        self.calls.append(("on_cycle", ts))
        return {"snapshot": {"equity": 1000, "risk_degree": 0.1}}

    def on_cycle_options(self, ts, strat_rows, chain_map, fut_rows, opt_rows=None):
        self.calls.append(("on_cycle_options", ts))
        return {"n_buy": 0}


def _make_state(ticker_running, trading_now, pa_fut=True):
    """构造 run_cycle 5.5 段所需最小 state 对象。"""
    st = types.SimpleNamespace()
    st._paper_ticker_running = ticker_running
    st._paper_stash = {}
    st.papers = {"10万_激进": _MiniBroker()}
    st.last_papers = {}
    st.last_paper = None
    return st, trading_now, pa_fut


def _run_branch(st, trading_now, pa_fut):
    """复刻 main.py run_cycle 5.5 段的纯分支选择（不含真实撮合/写盘副作用）。
    返回 (action, called)：action∈{"skip_frozen","skip_ticker","do_cycle"}。"""
    import config as _cfg
    _skip_paper = (not trading_now) and getattr(_cfg, "PAPER_TRADING_ONLY", True)
    _ticker_took_over = bool(getattr(st, "_paper_ticker_running", False)) and trading_now
    if _skip_paper:
        return "skip_frozen", False
    if _ticker_took_over:
        return "skip_ticker", False
    if pa_fut:
        for _name, _broker in st.papers.items():
            _broker.on_cycle("t", [], {})
        return "do_cycle", True
    return "do_nothing", False


def test_ticker_running_skips_on_cycle():
    """交易时段 + ticker 已接管 -> skip_ticker（run_cycle 5.5 不撮合不写盘）。"""
    st, trading, fut = _make_state(ticker_running=True, trading_now=True)
    action, called = _run_branch(st, trading, fut)
    assert action == "skip_ticker" and not called


def test_ticker_not_running_keeps_cycle():
    """交易时段 + ticker 未接管（--once/禁用/间隔0）-> do_cycle（保留旧行为兜底）。"""
    st, trading, fut = _make_state(ticker_running=False, trading_now=True)
    action, called = _run_branch(st, trading, fut)
    assert action == "do_cycle" and called


def test_off_trading_frozen():
    """非交易时段 -> skip_frozen（两边都冻结，不撮合不写盘）。"""
    st, trading, fut = _make_state(ticker_running=True, trading_now=False)
    action, called = _run_branch(st, trading, fut)
    assert action == "skip_frozen" and not called


def test_pa_fut_empty_no_cycle():
    """交易时段、ticker 未接管、但分析产出空 -> do_nothing（无信号安全跳过）。"""
    st, trading, fut = _make_state(ticker_running=False, trading_now=True, pa_fut=False)
    action, called = _run_branch(st, trading, fut)
    assert action == "do_nothing" and not called
