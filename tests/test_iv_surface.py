# -*- coding: utf-8 -*-
"""IV 曲面：报价质量分级、Black-76 二分反推、call/put 合并回归（第12轮 WP-B）。"""
import config
import iv_surface as ivs
from option_analyzer import black76


def test_leg_quote_quality_grades():
    px, q, spr = ivs.leg_quote({"bid": 99, "ask": 101, "last": 100})
    assert abs(px - 100) < 1e-9 and q == 0 and spr <= config.IV_MAX_SPREAD_RATIO
    # 宽价差回退最新价、标低质量
    px, q, _ = ivs.leg_quote({"bid": 1, "ask": 199, "last": 95})
    assert px == 95 and q == 1
    # 宽价差且无成交 -> 丢弃
    assert ivs.leg_quote({"bid": 1, "ask": 199, "last": 0}) is None
    # 只有最新价
    px, q, spr = ivs.leg_quote({"bid": 0, "ask": 0, "last": 50})
    assert px == 50 and q == 1 and spr is None
    assert ivs.leg_quote({"bid": 0, "ask": 0, "last": 0}) is None


def test_implied_vol_roundtrip():
    F, K, T, sig = 100.0, 100.0, 0.25, 0.30
    for kind in ("call", "put"):
        price = black76(F, K, T, sig, kind)
        out = ivs.implied_vol(price, F, K, T, kind)
        assert out is not None and abs(out - sig) < 1e-6


def test_implied_vol_rejects_bad():
    assert ivs.implied_vol(0, 100, 100, 0.25, "call") is None
    assert ivs.implied_vol(None, 100, 100, 0.25, "call") is None
    # 深实值脏价格（低于内在价值）反推不出 -> None，不编造
    assert ivs.implied_vol(1.0, 100, 90, 0.25, "call") is None


def test_put_call_parity_consistent_iv():
    F, K, T, sig = 100.0, 105.0, 0.4, 0.25
    c_iv = ivs.implied_vol(black76(F, K, T, sig, "call"), F, K, T, "call")
    p_iv = ivs.implied_vol(black76(F, K, T, sig, "put"), F, K, T, "put")
    assert abs(c_iv - p_iv) < 1e-6


def test_merge_strike_oi_weighted():
    c = {"iv": 0.30, "oi": 100, "quality": 0}
    p = {"iv": 0.31, "oi": 300, "quality": 0}
    iv, q, warn = ivs._merge_strike(c, p)
    assert abs(iv - 0.3075) < 1e-9 and q == 0 and warn is False


def test_merge_strike_parity_warn_picks_clean_side():
    c = {"iv": 0.20, "oi": 100, "quality": 1}   # 低质量
    p = {"iv": 0.50, "oi": 50, "quality": 0}    # 高质量（窄价差）
    iv, q, warn = ivs._merge_strike(c, p)
    assert warn is True and iv == 0.50 and q == 0     # 偏差>3vol，取可信侧不平均脏值


def test_merge_strike_single_side():
    c = {"iv": 0.28, "oi": 10, "quality": 0}
    iv, q, warn = ivs._merge_strike(c, None)
    assert iv == 0.28 and q == 0 and warn is False


def test_implied_vol_profile_uses_page_info_single_dict():
    """第107轮：page_info 返回的单品种 dict 结构（含 atm_iv 键）
    应被 implied_vol_profile 正确命中 OpenVlab 来源（修复旧映射假设的静默降级）。"""
    from option_analyzer import implied_vol_profile
    page = {"atm_iv": {"code": "rb", "price": 3200.0, "atm_iv": 18.5, "iv_chg": 1.2,
                       "iv_pct": 60.0, "skew": 2.1, "skew_pct": 55.0, "hv": 22.0,
                       "source": "ctamap"},
            "option_chain": {}}
    row = {"name": "螺纹钢", "cat": "黑色", "page": page,
           "hv20": 0.22, "hv60": 0.22, "score": 3.0, "price": 3200.0,
           "vol_cone": {}, "hv_percentile": 0.4}
    r = implied_vol_profile(row)
    assert r["iv_src"] == "OpenVlab真实", r["iv_src"]
    assert abs(r["iv"] - 0.185) < 1e-9, r["iv"]
    assert abs(r["skew"] - 2.1) < 1e-9
    assert abs(r["iv_pct"] - 60.0) < 1e-9
    # 缺键 / 空 page 时安静降级，不崩溃
    row2 = dict(row, page={})
    r2 = implied_vol_profile(row2)
    assert r2["iv_src"] in ("T链反推", "HV估计"), r2["iv_src"]
