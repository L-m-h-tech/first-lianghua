# -*- coding: utf-8 -*-
"""横截面相对强弱回归（第18轮 B1，稳健z/板块聚合/广度，只比较不改分）。"""
import statistics

import config
import cross_section as cs


def test_robust_z_median_mad():
    # [1..5]：中位数3、MAD=1 -> z(5)=0.6745*2
    z = cs._robust_z([1, 2, 3, 4, 5])
    assert abs(z[0] + 1.349) < 1e-6 and abs(z[-1] - 1.349) < 1e-6
    assert abs(z[2]) < 1e-12          # 中位数处 z=0
    assert cs._robust_z([]) == []


def test_robust_z_mad_zero_falls_back_to_pstdev():
    # MAD=0（多数相同）时退回总体标准差，不除零；全相同 -> 全0
    z = cs._robust_z([1, 1, 1, 5, 1])
    sd = statistics.pstdev([1, 1, 1, 5, 1])
    assert abs(z[3] - (5 - 1) / sd) < 1e-9
    assert all(abs(v) < 1e-12 for v in cs._robust_z([2, 2, 2]))


def test_robust_z_clip():
    # 极端值被截断到 XS_Z_CLIP，单个涨停不把整表拉爆
    z = cs._robust_z([0] * 9 + [100])
    assert max(z) <= config.XS_Z_CLIP + 1e-12


def _rows(n, scores, chgs=None, cats=None):
    chgs = chgs or [0.0] * n
    cats = cats or ["黑色"] * n
    return [{"name": "V%d" % i, "cat": cats[i], "score": scores[i],
             "chg": chgs[i], "price": 100.0, "label": "x"} for i in range(n)]


def test_rank_robust_ordering_and_breadth():
    scores = [-6, -4, -2, 0, 1, 2, 4, 6, 5, 3]
    out = cs.rank(_rows(10, scores))
    assert out["robust"] is True
    xs = [r["xs"] for r in out["rows"]]
    assert xs == sorted(xs, reverse=True)          # 按 xs 降序
    assert out["rows"][0]["rank"] == 1 and out["rows"][-1]["rank"] == 10
    assert out["top_long"][0]["name"] == "V7"      # score=6 最强
    b = out["breadth"]
    assert b["n"] == 10
    assert b["bull"] == sum(1 for s in scores if s >= config.SCORE_NEUTRAL)
    assert b["bear"] == sum(1 for s in scores if s <= -config.SCORE_NEUTRAL)
    assert b["bull"] + b["bear"] + b["neutral"] == 10


def test_rank_sector_aggregation():
    scores = [5, 4, -5, -4, 3, 2, -3, -2]
    cats = ["黑色", "黑色", "有色", "有色", "能化", "能化", "农产品", "农产品"]
    out = cs.rank(_rows(8, scores, cats=cats))
    assert out["sector_rank"][0] == "黑色"          # 黑色平均最强
    assert out["sectors"]["黑色"]["n"] == 2
    assert out["sectors"]["黑色"]["up"] == 0        # chg 全0 -> 无涨跌家数


def test_rank_small_sample_not_robust():
    out = cs.rank(_rows(3, [6, 0, -6]))
    assert out["robust"] is False                   # < XS_MIN_SAMPLE 不做 z
    # 退回绝对分归一：6/6.5≈0.923，仍可排序
    assert out["rows"][0]["name"] == "V0"


def test_rank_empty_safe():
    assert cs.rank(None)["rows"] == []
    assert cs.rank([])["rows"] == []
    assert cs.rank([{"score": 1}, None])["rows"] == []   # 无 name 的行被滤掉


def test_format_block_and_no_score_mutation():
    rows = _rows(10, list(range(-5, 5)))
    before = [dict(r) for r in rows]
    out = cs.rank(rows)
    lines = cs.format_block(out)
    assert any("横截面强弱" in x for x in lines)
    assert any("板块强弱" in x for x in lines)
    assert any("全市场广度" in x for x in lines)
    # 只做横向比较：原始 row 的 score 不被改动
    for a, b in zip(rows, before):
        assert a["score"] == b["score"]
    assert cs.format_block(None) == []
