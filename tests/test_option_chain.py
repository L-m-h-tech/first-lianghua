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


def _leg(strike, oi, cp="C", vol=0.0):
    return {"strike": float(strike), "oi": float(oi), "bid": 1, "last": 2,
            "ask": 3, "bid_vol": 1, "ask_vol": 1, "chg_pct": 0, "cp": cp, "vol": float(vol),
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


# ---------------- 第110轮：成交量 PCR（P_OP_ 批量快照补逐腿成交量） ----------------

def test_build_summary_pcr_vol_with_vol_map():
    """提供 vol_map 时回填每腿 vol 并给出成交量 PCR；P 总成交 / C 总成交。"""
    calls = [_leg(3000, 100, vol=10), _leg(2900, 40, vol=30)]     # C 总成交量 40
    puts = [_leg(3000, 150, "P", vol=20), _leg(3100, 50, "P", vol=60)]  # P 总成交量 80
    # code 形如 xC3000（_leg 构造），构造与之一致的 vol_map
    vol_map = {"xC3000": 10, "xC2900": 30, "xP3000": 20, "xP3100": 60}
    ch = oc.build_summary("CU", "SHFE", 26, 10, calls, puts, vol_map=vol_map)
    assert ch["call_vol"] == 40 and ch["put_vol"] == 80
    assert abs(ch["pcr_vol"] - 80 / 40) < 1e-9
    # 每腿 vol 已回填（按行权价升序：calls[0]=2900→30, puts[0]=3000→20）
    assert ch["calls"][0]["vol"] == 30 and ch["puts"][0]["vol"] == 20


def test_build_summary_pcr_vol_none_without_map():
    """不提供 vol_map 时 pcr_vol=None（诚实降级，与第98轮前一致）；vol 键存在但为 0。"""
    calls = [_leg(3000, 100), _leg(2900, 40)]
    puts = [_leg(3000, 150, "P"), _leg(3100, 50, "P")]
    ch = oc.build_summary("CU", "SHFE", 26, 10, calls, puts)
    assert ch["pcr_vol"] is None and ch["call_vol"] == 0 and ch["put_vol"] == 0
    assert all(x.get("vol", 0) == 0 for x in ch["calls"] + ch["puts"])


def test_build_summary_pcr_vol_zero_call_vol():
    """C 总成交量为 0 时 pcr_vol=None（不除零）；P 有量也不给。"""
    calls = [_leg(3000, 100)]                                   # vol=0
    puts = [_leg(3000, 150, "P", vol=50)]
    ch = oc.build_summary("X", "DCE", 26, 11, calls, puts, vol_map={"xP3000": 50})
    assert ch["pcr_vol"] is None


def test_parse_pop_volume():
    """P_OP_ 快照字段解析：成交量双候选兜底（实测 m 在[11]、部分所/远月在[3]）。"""
    # 实测样例：m2611C2900（成交量 660 在字段[11]）
    line_m = 'var hq_str_P_OP_m2611C2900="4,464.500,463.500,660.000,1,803.000,,2900,482.000,487.500,684.000,280.000,0.000,...";'
    assert oc._parse_pop_volume(line_m) == 280.0
    # 字段[11]为 0，回退[3]的情况（构造紧凑样例，长度仍 ≥12）
    line_b = 'var hq_str_P_OP_x2611C100="1,2,3,777,5,6,,100,8,9,10,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0";'
    assert oc._parse_pop_volume(line_b) == 777.0


def test_fetch_leg_volumes_parses_batch(monkeypatch):
    """批量快照解析：多 code 多行 -> {code: vol}；坏行/缺省静默。"""
    resp_lines = [
        'var hq_str_P_OP_m2611C2900="4,464.5,463.5,660,1,803,,2900,482,487.5,684,280,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0";',
        'var hq_str_P_OP_m2611P2900="3112,0.5,0.5,1,2,5208,,2900,1,1,204,0.5,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0";',
        'not an option line',          # 坏行跳过（P_OP_ 前缀不匹配）
    ]
    class _Resp:
        text = "\n".join(resp_lines)
        encoding = "gbk"
    class _H:
        def get(self, *a, **k):
            return _Resp()
    monkeypatch.setattr(oc.http, "get", _H().get)
    out = oc.fetch_leg_volumes(["m2611C2900", "m2611P2900"])
    assert out.get("m2611C2900") == 280.0
    assert out.get("m2611P2900") == 0.5


def test_fetch_leg_volumes_empty_codes():
    assert oc.fetch_leg_volumes([]) == {}


# ---------------- 第110轮：Policy B 定向分钟级刷链（hot_ttl 双档） ----------------

def test_cache_hot_ttl_short_refresh(monkeypatch):
    """hot 品种用 hot_ttl（默认300s）过期、普通品种用 OPTION_CHAIN_TTL；双档互不影响。"""
    monkeypatch.setattr(config, "OPTION_CHAIN_HOT_TTL", 300)
    monkeypatch.setattr(config, "OPTION_CHAIN_TTL", 1800)
    monkeypatch.setattr(config, "OPTION_CHAIN_HOT_SYMS", ("M",))
    cache = oc.OptionChainCache()
    # 手动注入两份"伪链"（避免真实网络）
    import time as _t
    chain_m = {"sym": "M", "updated": "1"}
    chain_c = {"sym": "C", "updated": "2"}
    with cache.lock:
        cache.cache[("M", 26, 11)] = (_t.time(), chain_m)
        cache.cache[("C", 26, 11)] = (_t.time(), chain_c)
    assert cache.is_hot("M") and not cache.is_hot("C")
    # 151 秒后：M（hot,300s）未过期；再把 M 的缓存时间拨旧 400 秒 -> 应过期
    with cache.lock:
        cache.cache[("M", 26, 11)] = (_t.time() - 400, chain_m)
    assert cache.get("M", 26, 11) is None          # hot 分钟级已过期
    assert cache.get("C", 26, 11) is not None      # 普通档 30min 未过期


def test_cache_hot_ttl_none_acts_legacy(monkeypatch):
    """OPTION_CHAIN_HOT_SYMS 为空 -> 全部品种等同旧行为（一律 OPTION_CHAIN_TTL）。"""
    monkeypatch.setattr(config, "OPTION_CHAIN_HOT_SYMS", ())
    cache = oc.OptionChainCache()
    assert not cache.is_hot("M") and not cache.is_hot("C")
    assert cache._ttl_of("M") == config.OPTION_CHAIN_TTL


def test_cache_hot_ttl_warm_hot_syms_override(monkeypatch):
    """warm(hot_syms=...) 可覆盖重点名单（不必依赖 config 白名单）。"""
    import time as _t
    monkeypatch.setattr(config, "OPTION_CHAIN_HOT_TTL", 300)
    monkeypatch.setattr(config, "OPTION_CHAIN_TTL", 1800)
    monkeypatch.setattr(config, "OPTION_CHAIN_HOT_SYMS", ())     # config 空
    cache = oc.OptionChainCache()
    cache._hot_syms = {"I"}
    cache.cache[("I", 26, 11)] = (_t.time() - 400, {"sym": "I"})
    assert cache.get("I", 26, 11) is None          # 通过 warm 覆盖名单后 hot 生效
    cache._hot_syms = None
    assert cache._ttl_of("I") == config.OPTION_CHAIN_TTL
