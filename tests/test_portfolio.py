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
