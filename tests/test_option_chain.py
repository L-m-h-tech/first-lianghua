# -*- coding: utf-8 -*-
"""期权 T 型链解析与持仓量 PCR 回归（第11轮 WP-A，纯函数零网络）。"""
import config
import option_chain as oc


def test_product_code():
    assert oc.product_code("cu", "SHFE") == "cu_o"
    assert oc.product_code("SC", "INE") == "sc_o"
    assert oc.product_code("m", "DCE") == "m_o"
    assert oc.product_code("MA", "CZCE") == "ma"     # 郑商所无后缀


def test_pinzhong():
    assert oc.pinzhong("m", 26, 10) == "m2610"
    assert oc.pinzhong("CU", 27, 3) == "cu2703"


def test_parse_leg_9_elements():
    row = ["1", "100", "101", "102", "2", "50", "0.1", "3000", "cu2610C3000"]
    leg = oc.parse_leg(row, "C")
    assert leg["strike"] == 3000.0 and leg["oi"] == 50 and leg["bid"] == 100
    assert leg["code"] == "cu2610C3000" and leg["cp"] == "C"


def test_parse_leg_8_elements_strike_from_code():
    row = ["1", "100", "101", "102", "2", "50", "0.1", "m2609P2500"]
    leg = oc.parse_leg(row, "P")
    assert leg["strike"] == 2500.0 and leg["cp"] == "P"


def test_parse_leg_bad():
    assert oc.parse_leg(None, "C") is None
    assert oc.parse_leg(["1", "2", "3", "4", "5", "6", "7", "badcode"], "C") is None


def _leg(strike, oi, cp="C"):
    return {"strike": float(strike), "oi": float(oi), "bid": 1, "last": 2,
            "ask": 3, "bid_vol": 1, "ask_vol": 1, "chg_pct": 0, "cp": cp,
            "code": "x%s%d" % (cp, strike)}


def test_pcr_sentiment_tiers():
    assert oc.pcr_sentiment(None) == ""
    assert "极值" in oc.pcr_sentiment(1.6)
    assert "谨慎" in oc.pcr_sentiment(1.3)
    assert "均衡" in oc.pcr_sentiment(1.0)
    assert "乐观" in oc.pcr_sentiment(0.6)
    assert "偏热" in oc.pcr_sentiment(0.4)


def test_build_summary():
    calls = [_leg(3000, 100), _leg(2900, 40)]
    puts = [_leg(3000, 150, "P"), _leg(3100, 50, "P")]
    ch = oc.build_summary("CU", "SHFE", 26, 10, calls, puts)
    assert ch["call_oi"] == 140 and ch["put_oi"] == 200
    assert abs(ch["pcr_oi"] - 200 / 140) < 1e-9 and ch["pcr"] == ch["pcr_oi"]
    assert ch["max_call_oi_strike"] == 3000
    assert ch["calls"][0]["strike"] == 2900          # 已按行权价升序
    assert ch["label"] == "2610" and ch["sentiment"]


def test_build_summary_zero_call_oi():
    ch = oc.build_summary("X", "DCE", 26, 11, [], [_leg(100, 10, "P")])
    assert ch["pcr_oi"] is None                      # 无认购持仓不除零


def test_locate_atm():
    calls = [_leg(2900, 1), _leg(3000, 1), _leg(3100, 1)]
    ch = oc.build_summary("CU", "SHFE", 26, 10, calls, [])
    oc.locate_atm(ch, 3060)
    assert ch["atm_strike"] == 3100
    assert abs(ch["atm_distance_pct"] - (3100 / 3060 - 1)) < 1e-9
