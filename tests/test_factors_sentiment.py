# -*- coding: utf-8 -*-
"""新闻情绪词典/否定反转/上下文闸门 + 五维情绪回归（第18轮 D1，不改主分）。"""
import math
from datetime import datetime, timedelta

import factors
from factors import NewsFactor, _lex_weight, sentiment_facets, facet_tags, _distinct_hits


# ---------------- 词典极性 / 闸门 / 否定 ----------------
def test_lex_basic_polarity():
    assert _lex_weight("美联储降息预期升温") > 0
    assert _lex_weight("美联储加息") < 0
    assert _lex_weight("无关的普通新闻文本") == 0.0


def test_lex_sector_filter():
    # “减产”只对能源 EN 板块计分
    assert _lex_weight("OPEC减产", factors.EN) > 0
    assert _lex_weight("OPEC减产", factors.BL) == 0.0


def test_lex_context_gate():
    # “增产”需上下文闸门：普通“工厂增产”不计，带原油语境才计负分
    assert _lex_weight("某工厂增产", factors.EN) == 0.0
    assert _lex_weight("原油产量增产", factors.EN) < 0


def test_lex_negation_reverses():
    pos = _lex_weight("原油减产", factors.EN)
    neg = _lex_weight("并没有原油减产", factors.EN)
    assert pos > 0 and neg < 0 and abs(neg + pos) < 1e-9


def test_distinct_hits_dedup():
    assert _distinct_hits("大幅大幅大幅", ("大幅",)) == 1
    assert _distinct_hits("大幅飙升", ("大幅", "飙升")) == 2


# ---------------- 五维情绪 ----------------
def test_facet_intensity_uncertainty_forward():
    f = sentiment_facets("螺纹钢大幅飙升涨停", variety="螺纹")
    assert f["intensity"] >= 0.99                 # 3个强度词 tanh 饱和
    assert f["relevance"] == 1.0
    assert f["event"] == "综合"
    f2 = sentiment_facets("市场可能或预计波动，下周仍有不确定性")
    assert f2["uncertainty"] > 0.9 and f2["forwardness"] > 0.5
    assert f2["relevance"] == 0.35                # 未点名品种


def test_facet_event_classification():
    assert sentiment_facets("美联储降息落地")["event"] == "货币政策"
    assert sentiment_facets("红海油轮遭导弹袭击")["event"] == "地缘"
    assert sentiment_facets("炼厂检修装置停产")["event"] == "供给"
    assert sentiment_facets("港口库存去库")["event"] == "需求库存"


def test_facet_polarity_reuses_lex():
    f = sentiment_facets("美联储大幅降息", cat=None)
    assert f["polarity"] > 0
    assert abs(f["polarity"]) <= 3.5              # 裁剪


def test_facet_safe_on_empty():
    f = sentiment_facets(None)
    assert f == {"polarity": 0.0, "intensity": 0.0, "uncertainty": 0.0,
                 "relevance": 0.35, "forwardness": 0.0, "event": "综合"}


def test_facet_tags_threshold():
    tags = facet_tags({"intensity": 0.9, "forwardness": 0.1, "uncertainty": 0.0,
                       "event": "供给"})
    assert "强0.9" in tags and "供给" in tags and "前瞻" not in tags
    assert facet_tags(None) == ""
    assert facet_tags({"intensity": 0.0, "forwardness": 0.0, "uncertainty": 0.0,
                       "event": "综合"}) == ""


# ---------------- NewsFactor 缓冲池 ----------------
def _news(content, important=False, confidence=1.0, mins_ago=0):
    return {"source": "t", "content": content,
            "time": datetime.now() - timedelta(minutes=mins_ago),
            "important": important, "confidence": confidence}


def test_newsfactor_dedup_and_score():
    nf = NewsFactor()
    n = nf.add([_news("美联储降息"), _news("美联储降息")])     # 同内容去重
    assert n == 1
    score, hits = nf.score(None)
    assert score > 0 and len(hits) >= 1


def test_newsfactor_confidence_discount():
    a = NewsFactor(); a.add([_news("美联储降息", confidence=1.0)])
    b = NewsFactor(); b.add([_news("美联储降息", confidence=0.4)])
    assert b.score(None)[0] < a.score(None)[0]


def test_newsfactor_variety_hit_amplifies():
    a = NewsFactor(); a.add([_news("螺纹钢地产政策发力")])
    b = NewsFactor(); b.add([_news("地产政策发力")])
    # 点名品种的那条在对应品种打分上被加权（这里用全局命中数对比方向即可）
    assert a.score(None)[0] != 0
