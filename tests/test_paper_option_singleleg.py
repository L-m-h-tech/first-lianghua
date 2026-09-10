# -*- coding: utf-8 -*-
"""第110轮：期权单腿信号接通回归（修结构性断链）。

背景：此前 on_cycle_options 只消费 option_strategies.recommend（strat_rows），而 recommend 从不输出
单腿买入（目录优先级单腿最低），导致期权撮合永远 0 成交；analyze_option 有 9515 次 all_pass=1
却从未接进撮合。本轮新增 opt_rows 参数：option_only/option_first 档遍历 analyze_option 单腿信号
成交 1 手；futures_first 档不启用（保持纪律）。零网络、确定性、显式注入账户表。
"""
import pytest

import config
from paper_broker import PaperBroker

MARGIN = {"M": {"broker_margin": 0.07, "limit_basic": 0.06, "multiplier": 10},
          "C": {"broker_margin": 0.07, "limit_basic": 0.06, "multiplier": 10}}


def _chain_map(sym="M", strike=2900.0, ask=30.0, bid=29.0):
    return {(sym, 26, 11): {"calls": [{"strike": strike, "ask": ask, "bid": bid,
                                       "last": (ask + bid) / 2}],
                            "puts": []}}


def _ao_row(sym="M", all_pass=True, kind="call", K=2900.0, prem=30.0, days=60):
    return {"name": sym, "kind": kind, "K": K, "yy": 26, "mm": 11,
            "month_label": "2611", "opt_code": "%s2611-C-%s" % (sym.lower(), int(K)),
            "prem": prem, "score": 6.0, "days": days, "all_pass": all_pass}


def _fut_rows(sym="M", score=6.0):
    return [{"sym": sym, "name": "豆粕", "score": score, "price": 2850.0}]


def _broker(name="1000_激进", equity0=1_000, priority="option_only", futures_max=0,
            opt_premium_ratio=0.80, entry_score=4.0, fill_mode="close"):
    return PaperBroker(name=name, equity0=equity0, entry_score=entry_score,
                       opt_premium_ratio=opt_premium_ratio, stop_loss_ratio=0.40,
                       priority=priority, futures_max=futures_max, options_max=None,
                       fill_mode=fill_mode)


def test_option_only_tier_opens_single_leg_from_analyze_option():
    """option_only 档：opt_rows 中 all_pass=True 的单腿 -> 成交 1 手（src=analyze_option）。"""
    br = _broker()
    s = br.on_cycle_options("2026-09-10 09:30:00", [], _chain_map(), _fut_rows(),
                            opt_rows=[_ao_row()])
    assert s["n_buy"] == 1
    assert len(s["trades"]) == 1
    t = s["trades"][0]
    assert t["sym"] == "M" and t["cp"] == "call" and t["lots"] == 1
    assert t.get("src") == "analyze_option"
    assert len(br.opt_positions) == 1


def test_option_only_skips_non_pass_single_leg():
    """all_pass=False 的单腿信号不成交（入场条件复用 analyze_option 既有 check）。"""
    br = _broker()
    s = br.on_cycle_options("2026-09-10 09:30:00", [], _chain_map(), _fut_rows(),
                            opt_rows=[_ao_row(all_pass=False)])
    assert s["n_buy"] == 0 and s["n_skipped"] == 0
    assert len(br.opt_positions) == 0


def test_option_only_skips_missing_chain_leg():
    """链上无该行权价 -> skipped（单腿分析-链上无该行权价），不成交。"""
    br = _broker()
    cm = {(("M", 26, 11)): {"calls": [], "puts": []}}
    s = br.on_cycle_options("2026-09-10 09:30:00", [], cm, _fut_rows(),
                            opt_rows=[_ao_row()])
    assert s["n_buy"] == 0 and s["n_skipped"] == 1
    assert len(br.opt_positions) == 0


def test_option_first_tier_opens_single_leg():
    """option_first 档同样接入 analyze_option 单腿信号。"""
    br = _broker(name="3000_赌徒", equity0=3_000, priority="option_first", futures_max=1)
    s = br.on_cycle_options("2026-09-10 09:30:00", [], _chain_map(), _fut_rows(),
                            opt_rows=[_ao_row()])
    assert s["n_buy"] == 1
    assert len(br.opt_positions) == 1


def test_futures_first_tier_not_enabled():
    """futures_first 档不接 analyze_option 单腿信号（保持纪律，仅推荐路径）。"""
    br = _broker(name="10万_激进", equity0=100_000, priority="futures_first", futures_max=None)
    s = br.on_cycle_options("2026-09-10 09:30:00", [], _chain_map(), _fut_rows(),
                            opt_rows=[_ao_row()])
    assert s["n_buy"] == 0 and len(s["trades"]) == 0
    assert len(br.opt_positions) == 0


def test_option_only_respects_premium_budget():
    """单腿权利金超过统一权益上限 -> skipped（权利金超统一权益上限），不成交。"""
    br = _broker(opt_premium_ratio=0.05)   # 1000×0.05=50 上限 < 权利金 30×10=300
    s = br.on_cycle_options("2026-09-10 09:30:00", [], _chain_map(), _fut_rows(),
                            opt_rows=[_ao_row()])
    assert s["n_buy"] == 0 and s["n_skipped"] == 1
    assert len(br.opt_positions) == 0


def test_option_only_skips_already_held_same_sym():
    """已持有同品种期权 -> 不加仓（与 strat_rows 路径同一纪律）。"""
    br = _broker()
    s1 = br.on_cycle_options("2026-09-10 09:30:00", [], _chain_map(), _fut_rows(),
                             opt_rows=[_ao_row()])
    assert s1["n_buy"] == 1
    s2 = br.on_cycle_options("2026-09-10 09:31:00", [], _chain_map(), _fut_rows(),
                             opt_rows=[_ao_row()])
    assert s2["n_buy"] == 0
    assert len(br.opt_positions) == 1
