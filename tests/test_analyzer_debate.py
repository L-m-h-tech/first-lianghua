# -*- coding: utf-8 -*-
"""综合分评级边界 + 多空双面论证卡回归（analyzer，纯函数零网络）。"""
import analyzer


def test_rating_bands_long():
    assert analyzer.rating(0)[0] == "中性"
    assert analyzer.rating(1.9)[0] == "中性"
    lab, _, conf = analyzer.rating(2.0)
    assert lab == "偏多" and conf == 55
    assert analyzer.rating(3.9)[0] == "偏多"
    assert analyzer.rating(4.0)[0] == "看多"
    assert analyzer.rating(6.4)[0] == "看多"
    lab, _, conf = analyzer.rating(6.5)
    assert lab == "强看多" and conf == 82


def test_rating_bands_short():
    assert analyzer.rating(-2.0)[0] == "偏空"
    assert analyzer.rating(-4.0)[0] == "看空"
    assert analyzer.rating(-6.5)[0] == "强看空"


def test_direction_text():
    assert analyzer.direction_text(1) == "做多"
    assert analyzer.direction_text(-1) == "做空"


def test_debate_splits_bull_bear():
    row = {"score": 5.0, "chg": 0.01,
           "parts": {"消息面": 1.5, "日线动量": 2.0, "技术共振": -0.8}}
    d = analyzer.build_debate(row)
    bull_txt = " ".join(d["bull"])
    bear_txt = " ".join(d["bear"])
    assert "消息" in bull_txt and "日线趋势" in bull_txt
    assert "技术共振" in bear_txt
    assert "当日" in bull_txt
    assert d["verdict"] == "多方占优"


def test_debate_verdict_levels():
    assert analyzer.build_debate({"score": 0.0})["verdict"] == "多空均衡"
    assert analyzer.build_debate({"score": 5.0})["verdict"] == "多方占优"
    assert analyzer.build_debate({"score": -5.0})["verdict"] == "空方占优"


def test_debate_dedup_fundamental_and_flow():
    # parts 里的“基本面/量仓资金”与专门分支同源，去重不重复列
    row = {"score": 3.0, "parts": {"基本面": 1.0, "量仓资金": 0.5},
           "fundamental": {"score": 1.0},
           "flow": {"score": 0.5, "pattern": "增仓上行"}}
    d = analyzer.build_debate(row)
    joined = " ".join(d["bull"])
    assert joined.count("基本面") == 1
    assert joined.count("量仓") == 1


def test_debate_flow_inst_hv():
    row = {"score": -7.0, "chg": -0.01,
           "flow": {"score": -0.6, "pattern": "增仓下行"},
           "inst_ratio": -0.3, "hv_percentile": 0.9}
    d = analyzer.build_debate(row)
    bear = " ".join(d["bear"])
    assert "增仓下行" in bear and "机构净空" in bear and "波动分位" in bear
    assert d["verdict"] == "空方占优"
