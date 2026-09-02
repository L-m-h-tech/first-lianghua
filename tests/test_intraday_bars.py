# -*- coding: utf-8 -*-
"""分钟K周期聚合 + 合约代码构造回归（第14轮 WP-D0，纯函数零网络）。"""
import intraday_bars as ib


def _bar(dt, o, h, l, c, v=10, amount=1000):
    return {"dt": dt, "o": o, "h": h, "l": l, "c": c, "v": v, "amount": amount,
            "sym": "RB", "contract": "RB0", "period": 1}


def test_aggregate_basic_ohlcv():
    bars = [_bar("2026-09-01 09:0%d" % i, 100 + i, 102 + i, 99 + i, 101 + i) for i in range(1, 6)]
    out = ib.aggregate_bars(bars, 1, 5)
    assert len(out) == 1
    m = out[0]
    assert m["o"] == bars[0]["o"] and m["c"] == bars[-1]["c"]
    assert m["h"] == max(b["h"] for b in bars)
    assert m["l"] == min(b["l"] for b in bars)
    assert m["v"] == 50 and m["amount"] == 5000
    assert m["dt"] == bars[-1]["dt"] and m["period"] == 5


def test_aggregate_does_not_merge_across_break():
    # 09:02 -> 09:05 跨午休/缺口，不连续段不硬拼；factor=2 只合并前两根
    bars = [_bar("2026-09-01 09:01", 1, 2, 0, 1),
            _bar("2026-09-01 09:02", 1, 2, 0, 1),
            _bar("2026-09-01 09:05", 1, 2, 0, 1)]
    out = ib.aggregate_bars(bars, 1, 2)
    assert len(out) == 1 and out[0]["dt"] == "2026-09-01 09:02"


def test_aggregate_trailing_partial_dropped():
    bars = [_bar("2026-09-01 09:0%d" % i, 1, 2, 0, 1) for i in range(1, 8)]  # 7根，factor=3
    out = ib.aggregate_bars(bars, 1, 3)
    assert len(out) == 2                              # 末根零散不合成半根周期


def test_aggregate_factor_one_copies():
    bars = [_bar("2026-09-01 09:01", 1, 2, 0, 1)]
    out = ib.aggregate_bars(bars, 1, 1)
    assert len(out) == 1 and out[0] is not bars[0]    # 浅拷贝、非同一对象


def test_aggregate_skips_bad_dt():
    bars = [_bar("bad", 1, 2, 0, 1), _bar("2026-09-01 09:01", 1, 2, 0, 1),
            _bar("2026-09-01 09:02", 1, 2, 0, 1)]
    assert len(ib.aggregate_bars(bars, 1, 2)) == 1


def test_contract_code_builders():
    assert ib.em_contract_code("MA", "CZCE", 26, 10) == "ma610"     # 郑商所3位
    assert ib.em_contract_code("rb", "SHFE", 27, 1) == "rb2701"
    assert ib.project_contract_code("ma", 26, 10) == "MA2610"
    assert ib.em_secid("RB", "SHFE", 27, 1) == "113.rb2701"
    assert ib.em_secid("MA", "CZCE", 26, 10) == "115.ma610"
    assert ib.em_secid("XX", "NOPE", 26, 10) == ""                  # 未知交易所不硬拼
