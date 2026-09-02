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
