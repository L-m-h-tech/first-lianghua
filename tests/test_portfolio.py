# -*- coding: utf-8 -*-
"""组合资金账户回归（第16轮 WP-E：逐bar盯市/三种手数/约束链/强平状态机/费用/无bar时刻）。

重建第16轮37条零网络合成断言的核心口径，全部确定性、字母序回放、零网络。
"""
import portfolio as pf_mod
from portfolio import Portfolio, SymbolFeed


def _margin(**items):
    # items: SYM=(broker_margin, multiplier)
    return {s: {"broker_margin": m, "multiplier": mu, "limit_basic": 0.07}
            for s, (m, mu) in items.items()}


def _pf(equity=1_000_000, margin=None, fees=None, **kw):
    margin = margin if margin is not None else _margin(RB=(0.10, 10), CU=(0.12, 5))
    kw.setdefault("use_real_fees", False)       # 默认零费用，便于精确对账
    kw.setdefault("fee_rate", 0.0)
    kw.setdefault("slip_rate", 0.0)
    return Portfolio(equity, margin, fees or {}, **kw)


# ---------------- 盯市：一赚一亏逐点对齐 ----------------
def test_mark_to_market_two_positions():
    pf = _pf(equity=100000, margin=_margin(RB=(0.10, 10), CU=(0.12, 5)),
             per_symbol=0.20, max_symbol_weight=1.0, max_sector_weight=1.0)
    # RB 价格1000、mult10，单手名义1万；20%权重 -> 2手
    pos = pf.open("RB", "螺纹", "黑色", 1, 1000, "t0")
    assert pos.lots == 2
    assert abs(pf.margin_used({"RB": 1000}) - 2000) < 1e-9
    # RB 涨到1050：多头浮盈 (1050-1000)*10*2=1000
    assert abs(pf.float_pnl({"RB": 1050}) - 1000) < 1e-9
    assert abs(pf.equity({"RB": 1050}) - 101000) < 1e-9
    # CU 价格2000、mult5，单手名义1万；开空2手
    pf.open("CU", "铜", "有色", -1, 2000, "t1")
    # CU 跌到1900：空头浮盈 (2000-1900)*5*2=1000
    prices = {"RB": 1050, "CU": 1900}
    assert abs(pf.float_pnl(prices) - 2000) < 1e-9
    assert abs(pf.equity(prices) - 102000) < 1e-9
    used = pf.margin_used(prices)
    assert abs(used - (1050 * 10 * 2 * 0.10 + 1900 * 5 * 2 * 0.12)) < 1e-9
    assert abs(pf.risk_degree(prices) - used / 102000) < 1e-12
    assert pf.available(prices) == 102000 - used


def test_short_position_loss_direction():
    pf = _pf(equity=100000, margin=_margin(RB=(0.10, 10)),
             per_symbol=0.2, max_symbol_weight=1.0, max_sector_weight=1.0)
    pf.open("RB", "螺纹", "黑色", -1, 1000, "t0")     # 做空2手
    # 价格上涨对空头是亏损
    assert pf.float_pnl({"RB": 1100}) < 0
    assert abs(pf.float_pnl({"RB": 1100}) - (-1 * 100 * 10 * 2)) < 1e-9


# ---------------- 三种手数分配 ----------------
def test_sizing_equal_notional():
    pf = _pf(per_symbol=0.15)
    lots, why = pf.decide_lots("RB", 1, 3500)        # 单手名义3.5万，15%权重
    assert why is None and lots == int((1e6 * 0.15) // 35000)


def test_sizing_equal_risk():
    pf = _pf(sizing="equal_risk", risk_per_trade=0.01, stop_atr=1.2)
    # 单手风险=1.2*20*10=240，风险预算1万 -> 41.67手；单品种名义上限30% -> 30手
    lots, _ = pf.decide_lots("RB", 1, 1000, atr=20)
    assert lots == 30


def test_sizing_by_score():
    pf = _pf(sizing="score", score_weights={"强信号": 0.15, "轻仓": 0.05},
             max_symbol_weight=1.0)
    strong, _ = pf.decide_lots("RB", 1, 1000, score=7.0)   # 强信号15%
    light, _ = pf.decide_lots("CU", 1, 2000, score=3.0)    # 轻仓5%（单手名义1万）
    assert strong == 15 and light == 5


# ---------------- 约束链拦截 ----------------
def test_max_concurrent_cap():
    pf = _pf(margin=_margin(AA=(0.1, 10), BB=(0.1, 10), CC=(0.1, 10)),
             max_concurrent=2, per_symbol=0.05, max_symbol_weight=1.0,
             max_sector_weight=2.0)
    assert pf.open("AA", "a", "s1", 1, 1000, "t") is not None
    assert pf.open("BB", "b", "s2", 1, 1000, "t") is not None
    pos = pf.open("CC", "c", "s3", 1, 1000, "t")
    assert pos is None and pf.skipped[-1]["reason"] == "同时持仓数达上限"


def test_cannot_afford_one_lot():
    # 高价品种单手名义远超权重预算 -> 不足1手，是真实约束非bug
    pf = _pf(equity=100000, margin=_margin(CU=(0.12, 5)), per_symbol=0.15)
    lots, why = pf.decide_lots("CU", 1, 60000)            # 单手名义30万
    assert lots == 0 and "不足1手" in why


def test_symbol_weight_cap_binds():
    pf = _pf(per_symbol=0.9, max_symbol_weight=0.20, max_sector_weight=2.0)
    lots, _ = pf.decide_lots("RB", 1, 1000)               # 目标90手，单品种上限20手
    assert lots == 20


def test_sector_weight_cap_binds():
    pf = _pf(margin=_margin(RB=(0.1, 10), HC=(0.1, 10)),
             per_symbol=0.4, max_symbol_weight=2.0, max_sector_weight=0.60,
             sector_of={"RB": "黑色", "HC": "黑色"})
    pf.open("RB", "螺纹", "黑色", 1, 1000, "t0")           # 占40%板块额度
    lots, _ = pf.decide_lots("HC", 1, 1000)               # 板块只剩20%
    assert lots == 20


def test_no_multiplier_rejected():
    pf = _pf(margin={}, per_symbol=0.5)
    assert pf.decide_lots("ZZ", 1, 1000)[0] == 0
    assert pf.open("ZZ", "z", "s", 1, 1000, "t") is None


# ---------------- 保证金兜底 ----------------
def test_margin_fallback_registered():
    pf = _pf(margin=_margin(RB=(0.10, 10)), default_margin=0.12,
             per_symbol=0.1, max_symbol_weight=1.0, max_sector_weight=2.0)
    assert pf.margin_rate_of("XX") == 0.12
    assert "XX" in pf.fallback_margins
    assert pf.margin_rate_of("RB") == 0.10 and "RB" not in pf.fallback_margins


# ---------------- 费用对账 ----------------
def test_fee_accounting():
    fees = {"RB": {"multiplier": 10, "open_amt_rate": 0.0, "open_per_lot": 3.0,
                   "close_amt_rate": 0.0, "close_per_lot": 3.0,
                   "today_amt_rate": 0.0, "today_per_lot": 3.0}}
    pf = _pf(equity=100000, margin=_margin(RB=(0.10, 10)), fees=fees,
             use_real_fees=True, per_symbol=0.1, max_symbol_weight=1.0,
             max_sector_weight=2.0)
    pf.open("RB", "螺纹", "黑色", 1, 1000, "t0")       # 1手，开仓费3
    assert abs(pf.realized - (-3)) < 1e-9
    rec = pf.close("RB", 1100, "t1", "止盈")
    assert abs(rec["gross_yuan"] - 1000) < 1e-9        # (1100-1000)*10
    assert abs(rec["open_fee_yuan"] - 3) < 1e-9 and abs(rec["close_fee_yuan"] - 3) < 1e-9
    assert abs(rec["net_yuan"] - 994) < 1e-9
    assert abs(pf.fees_paid - 6) < 1e-9
    assert abs(pf.realized - 994) < 1e-9               # 平仓后累计已实现=净盈亏(毛-开-平)


# ---------------- 强平状态机：触发线/安全线、浮亏最大优先、一路砍到安全线 ----------------
def _three_sym_pf():
    pf = _pf(equity=200000, margin=_margin(AA=(0.02, 10), BB=(0.02, 10), CC=(0.02, 10)),
             per_symbol=0.8, max_symbol_weight=2.0, max_sector_weight=2.0,
             risk_liquidate=0.09, risk_safe=0.05)
    for s in ("AA", "BB", "CC"):
        pf.open(s, s, "sec", 1, 1000, "t0")           # 各16手
    return pf


def test_liquidate_worst_first_and_down_to_safe():
    pf = _three_sym_pf()
    prices = {"AA": 700, "BB": 500, "CC": 1000}       # BB亏最多、AA次之、CC持平
    pf.record("t1", prices)
    events = pf.liquidate("t1", prices.get)
    syms = [e["sym"] for e in events]
    assert syms == ["BB", "AA"]                        # 浮亏最大优先、一路砍到安全线
    assert "CC" in pf.positions                        # 风险度降到安全线以下即停，保留CC
    assert pf.risk_degree() < pf.risk_safe
    assert all(e["forced"] for e in events)


def test_liquidate_bankruptcy_closes_all():
    pf = _three_sym_pf()
    # 价格崩到权益为负 -> 穿仓，全部平掉
    prices = {"AA": 10, "BB": 10, "CC": 10}
    pf.record("t1", prices)
    events = pf.liquidate("t1", prices.get)
    assert len(pf.positions) == 0 and len(events) == 3


def test_no_liquidate_when_healthy():
    pf = _three_sym_pf()
    pf.record("t1", {"AA": 1001, "BB": 1001, "CC": 1001})
    assert pf.liquidate("t1", lambda s: 1001) == []


# ---------------- SymbolFeed.owner_at：无bar时刻取最近已收盘bar ----------------
def _feed():
    bars = [{"dt": "t%d" % i} for i in range(5)]
    owners = ["d0", "d1", "d2", "d3", "d4"]
    return SymbolFeed("RB", "螺纹", "黑色", bars, None, None, owners, None, None, 0.0)


def test_owner_at_bisect():
    f = _feed()
    assert f.owner_at("t2") == "d2"
    assert f.owner_at("t25") == "d2"                   # 无bar时刻 -> ≤t 最近一根
    assert f.owner_at("zz") == "d4"
    assert f.owner_at("a") is None                     # 早于首根 -> None


# ---------------- 校准乘子只在显式传入时生效 ----------------
class _FakeCalib:
    def __init__(self, mult): self.mult = mult
    def lookup(self, score, direction_int=None, parts=None):
        return {"calibrated": True, "mult": self.mult, "level": "方向", "n": 30}


def test_calibrator_changes_lots_only_when_passed():
    base = _pf(sizing="score", score_weights={"强信号": 0.30}, max_symbol_weight=2.0,
               max_sector_weight=2.0)
    lots0, _ = base.decide_lots("RB", 1, 1000, score=7.0)
    cal = _pf(sizing="score", score_weights={"强信号": 0.30}, max_symbol_weight=2.0,
              max_sector_weight=2.0, calibrator=_FakeCalib(0.5))
    lots1, _ = cal.decide_lots("RB", 1, 1000, score=7.0)
    assert lots1 == int(lots0 * 0.5) and lots0 > lots1


def test_curve_record_and_performance():
    pf = _pf(equity=100000, margin=_margin(RB=(0.10, 10)),
             per_symbol=0.2, max_symbol_weight=1.0, max_sector_weight=1.0)
    pf.open("RB", "螺纹", "黑色", 1, 1000, "t0")
    pf.record("t0", {"RB": 1000})
    pf.record("t1", {"RB": 1100})
    pf.close("RB", 1100, "t2", "止盈")
    pf.record("t2", {"RB": 1100})
    assert len(pf.curve) == 3
    perf = pf.performance()
    assert perf["n_trades"] == 1 and perf["win_rate"] == 1.0

# ================= 第41轮 G26续：横截面风险型 sizing（默认等价旧版/权重定手数/PIT无未来/确定性回放） =================

def _synth_feed(sym, n, seed, base=3000.0):
    """构造 n 根同步时间戳的合成bar（确定性几何随机游走），供 trailing_risk_weights/引擎测试。"""
    import random
    from datetime import datetime, timedelta
    rng = random.Random(seed)
    t0 = datetime(2026, 1, 5, 9, 0)
    bars, px = [], base
    for i in range(n):
        px *= (1.0 + rng.uniform(-0.004, 0.004))
        bars.append({"dt": t0 + timedelta(minutes=30 * i), "o": px, "h": px * 1.002,
                     "l": px * 0.998, "c": px, "v": 10000, "m": sym})
    # 全0综合分（不触发入场，entry_th=4）、1.0 ATR；引擎确定性/权重注入测试不需要真实信号
    return SymbolFeed(sym, "测试", "黑色", bars, [0.0] * n, [1.0] * n, None, None, None, 0.0)


def test_risk_sizing_off_ignores_injected_weights():
    """默认 risk_sizing=None：即便注入权重，手数决策也逐字节等价旧等名义（铁律：缺省不变）。"""
    pf = _pf()
    lots0, why0 = pf.decide_lots("RB", 1, 3000.0)     # 单手名义3万，等名义15%=5手
    pf.set_risk_weights({"RB": 0.30}, {"n": 1})
    lots1, why1 = pf.decide_lots("RB", 1, 3000.0)
    assert pf.risk_sizing is None and pf._last_target_weight is None
    assert lots0 == lots1 == 5 and why0 == why1


def test_risk_weight_drives_target_lots_and_missing_falls_back():
    """开启 inv_vol：宇宙内品种按注入权重定目标名义；宇宙外品种安全回退等名义 per_symbol。"""
    pf = _pf(risk_sizing="inv_vol", risk_gross=1.0)
    pf.set_risk_weights({"RB": 0.30})
    # RB 权重0.30 -> 目标名义30万，单手3万 -> 10手（等名义15%只有5手）
    lots_w, _ = pf.decide_lots("RB", 1, 3000.0)
    assert lots_w == 10 and pf._last_target_weight == 0.30
    # CU 不在权重宇宙 -> 回退等名义15%：单手名义3000*5=1.5万 -> 10手
    lots_fb, _ = pf.decide_lots("CU", 1, 3000.0)
    assert lots_fb == 10 and pf._last_target_weight is None


def test_trailing_risk_weights_strict_pit_no_future():
    """权重只用 t 之前的bar：改动 t 当根及以后的收盘价不影响结果；样本不足返回空map安全回退。"""
    feeds = {s: _synth_feed(s, 80, seed=k) for k, s in enumerate(("RB", "CU", "MA", "SA"))}
    t = feeds["RB"].bars[60]["dt"]
    w1, meta1 = pf_mod.trailing_risk_weights(feeds, t, "inv_vol", window=126, min_hist=20)
    assert set(w1) == set(feeds) and meta1["n"] == 4 and meta1["T"] >= 20
    # 篡改 t 当根(索引60)及以后的全部收盘价 -> 权重必须不变（严格无未来）
    feeds2 = {s: _synth_feed(s, 80, seed=k) for k, s in enumerate(("RB", "CU", "MA", "SA"))}
    for f in feeds2.values():
        for b in f.bars[60:]:
            b["c"] *= 1.5
    w2, _ = pf_mod.trailing_risk_weights(feeds2, t, "inv_vol", window=126, min_hist=20)
    assert w1.keys() == w2.keys()
    for s in w1:
        assert abs(w1[s] - w2[s]) < 1e-12
    # 过早的 t（可估历史<min_hist+1）-> 空 map + reason，绝不抛错
    t_early = feeds["RB"].bars[5]["dt"]
    w3, meta3 = pf_mod.trailing_risk_weights(feeds, t_early, "inv_vol", min_hist=40)
    assert w3 == {} and "reason" in meta3


def test_trailing_risk_weights_properties_sum_gross_and_cap():
    """inv_vol/erc 权重非负、求和=gross、单票不超 cap；缺历史品种进 excluded。"""
    feeds = {s: _synth_feed(s, 90, seed=k * 7 + 1) for k, s in
             enumerate(("RB", "CU", "MA", "SA", "I", "TA"))}
    for method in ("inv_vol", "erc"):
        w, meta = pf_mod.trailing_risk_weights(feeds, feeds["RB"].bars[80]["dt"], method,
                                               window=126, min_hist=30, cap=0.25, gross=1.0)
        assert len(w) == 6 and all(x >= 0 for x in w.values())
        assert abs(sum(w.values()) - 1.0) < 1e-9
        assert max(w.values()) <= 0.25 + 1e-9
        assert meta["eff_n"] >= 1
    # gross 缩放：gross=1.5 时权重和=1.5
    w, _ = pf_mod.trailing_risk_weights(feeds, feeds["RB"].bars[80]["dt"], "erc",
                                        min_hist=30, gross=1.5)
    assert abs(sum(w.values()) - 1.5) < 1e-9
    # 一个品种历史被人为截短 -> 不纳入宇宙、进 excluded，其余仍出权重
    feeds["TA"].bars = feeds["TA"].bars[:10]
    feeds["TA"].dts = [b["dt"] for b in feeds["TA"].bars]
    w, meta = pf_mod.trailing_risk_weights(feeds, feeds["RB"].bars[80]["dt"], "inv_vol", min_hist=30)
    assert "TA" not in w and "TA" in meta["excluded"] and len(w) == 5


def test_reset_feeds_enables_deterministic_replay():
    """同一批 feeds 经 _reset_feeds 后重复回放，曲线/成交逐值一致（影子对照的前提）。"""
    feeds = {s: _synth_feed(s, 70, seed=k) for k, s in enumerate(("RB", "CU", "MA"))}
    kw = dict(entry_th=4.0, stop_atr=1.2, target_atr=2.0, flat_eod=True, max_bars=48,
              use_limit=False, limit_eps=0.0008, minute_mode=True, hold_days=10)
    pf1 = _pf()
    pf_mod.run_portfolio(feeds, pf1, **kw)
    curve1 = [(pf_mod._dt(p["dt"]), p["equity"], p["npos"]) for p in pf1.curve]
    closed1 = [(t["sym"], t["lots"], round(t["net_yuan"], 6)) for t in pf1.closed]
    pf_mod._reset_feeds(feeds)
    pf2 = _pf()
    pf_mod.run_portfolio(feeds, pf2, **kw)
    curve2 = [(pf_mod._dt(p["dt"]), p["equity"], p["npos"]) for p in pf2.curve]
    closed2 = [(t["sym"], t["lots"], round(t["net_yuan"], 6)) for t in pf2.closed]
    assert curve1 == curve2 and closed1 == closed2


def test_engine_risk_cfg_injects_weights_and_none_equals_absent():
    """risk_cfg=None 与不传逐值一致；risk_cfg 开启后引擎按 rebalance 注入权重且留痕。"""
    feeds = {s: _synth_feed(s, 70, seed=k + 3) for k, s in enumerate(("RB", "CU", "MA", "SA"))}
    base = dict(entry_th=4.0, stop_atr=1.2, target_atr=2.0, flat_eod=True, max_bars=48,
                use_limit=False, limit_eps=0.0008, minute_mode=True, hold_days=10)
    pf_a = _pf()
    pf_mod.run_portfolio(feeds, pf_a, risk_cfg=None, **base)
    pf_mod._reset_feeds(feeds)
    pf_b = _pf()
    pf_mod.run_portfolio(feeds, pf_b, **base)          # 完全不传 risk_cfg
    assert [p["equity"] for p in pf_a.curve] == [p["equity"] for p in pf_b.curve]
    # 开启风险型：重估留痕，且首个重估点后权重宇宙非空
    pf_mod._reset_feeds(feeds)
    pf_c = _pf(risk_sizing="erc")
    rcfg = {"method": "erc", "window": 60, "rebalance": 10, "min_hist": 20,
            "shrink": 0.1, "cap": 0.25, "gross": 1.0}
    pf_mod.run_portfolio(feeds, pf_c, risk_cfg=rcfg, **base)
    assert len(pf_c.risk_meta_log) >= 1
    assert any(m.get("n", 0) >= 2 for m in pf_c.risk_meta_log)
    assert abs(sum(pf_c.risk_weights.values()) - 1.0) < 1e-9
