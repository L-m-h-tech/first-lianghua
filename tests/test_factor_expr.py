# -*- coding: utf-8 -*-
"""G25（第38轮）表达式因子引擎 factor_expr 的细粒度单测（零网络/零DB）。

覆盖：①解析器安全边界（白名单放行、危险/未知/属性/dunder/语句一律拒绝、窗口必须正整数字面量）；
②时序算子逐值手算与无未来；③截面算子；④治理（相关/正交/加权）；⑤实时离线结构性 parity。
聚合自测在 test_tools_selftest.test_factor_expr_selftest，本文件补边界与手算颗粒度。
"""
import math

import pytest

import factor_expr as fe
from factor_expr import ExprError, compute_ts, eval_cs, parse


C = [100.0, 101.0, 103.0, 102.0, 105.0, 108.0, 107.0, 110.0, 112.0, 115.0]


# ---------------- ① 安全边界 ----------------
@pytest.mark.parametrize("bad", [
    "__import__('os')", "x.open", "eval(close)", "exec(close)", "lambda:1", "globals()",
    "locals()", "getattr(x,y)", "foo(close,3)", "import os", "a;b", "close.__class__",
    "ts_mean(close,-2)", "ts_mean(close,close)", "ts_mean(close,2.5)", "delay(close)",
    "cross_rank(close)", "unknown(close,3)", "close..3", "(close+1", "close+1)",
])
def test_parser_rejects_dangerous(bad):
    with pytest.raises(ExprError):
        compute_ts(bad, {"close": C, "x": C, "y": [1.0] * len(C)})


def test_parser_accepts_whitelist_and_decimal():
    for ok in ["close+1", "ts_mean(close,5)", "delta(close,5)/delay(close,5)",
               "corr(close,volume,10)", "(close/ts_mean(close,20)-1)/(ts_std(close,20)+0.000001)",
               "abs(close-100)", "max(close,delay(close,1))", "decay_linear(close,3)"]:
        assert parse(ok)[0] in ("bin", "call")


def test_missing_field_raises():
    with pytest.raises(ExprError):
        compute_ts("ts_mean(nope,3)", {"close": C})


def test_ts_op_forbidden_in_cross_section():
    with pytest.raises(ExprError):
        eval_cs("ts_mean(m,3)", {"m": {"A": 1.0, "B": 2.0}})


# ---------------- ② 时序算子手算 ----------------
def test_delay_delta():
    d = compute_ts("delay(close,2)", {"close": C})
    assert d[:2] == [None, None] and d[2] == C[0] and d[9] == C[7]
    dl = compute_ts("delta(close,1)", {"close": C})
    assert dl[1] == C[1] - C[0] and dl[0] is None


def test_window_stats():
    tm = compute_ts("ts_mean(close,3)", {"close": C})
    assert tm[0] is None and tm[1] is None
    assert tm[2] == pytest.approx((C[0] + C[1] + C[2]) / 3)
    assert compute_ts("ts_sum(close,3)", {"close": C})[3] == sum(C[1:4])
    assert compute_ts("ts_min(close,3)", {"close": C})[3] == min(C[1:4])
    assert compute_ts("ts_max(close,3)", {"close": C})[3] == max(C[1:4])
    sd = compute_ts("ts_std(close,3)", {"close": C})
    import statistics
    assert sd[3] == pytest.approx(statistics.stdev(C[1:4]))


def test_ts_rank_minmax_decay():
    assert compute_ts("ts_rank(close,3)", {"close": [1.0, 2.0, 3.0]})[2] == pytest.approx(1.0)
    mid = compute_ts("ts_rank(close,3)", {"close": [3.0, 2.0, 1.0]})[2]
    assert mid == pytest.approx((0 + 0.5 * 2) / 3)
    assert compute_ts("ts_minmax(close,3)", {"close": [2.0, 4.0, 6.0]})[2] == 1.0
    assert compute_ts("ts_minmax(close,3)", {"close": [5.0, 5.0, 5.0]})[2] == 0.5
    dl = compute_ts("decay_linear(close,3)", {"close": [1.0, 2.0, 3.0]})
    assert dl[2] == pytest.approx((1 * 1 + 2 * 2 + 3 * 3) / 6)


def test_ts_ema_ts_rma_recurrence():
    # ts_ema：前 n 个有限值 SMA 播种，其后 alpha=2/(n+1) 递推；前导 None 不影响播种
    es = compute_ts("ts_ema(close,3)", {"close": [1.0, 2.0, 3.0, 5.0]})
    seed = (1 + 2 + 3) / 3.0
    assert es[:2] == [None, None]
    assert float.hex(es[2]) == float.hex(seed)
    assert float.hex(es[3]) == float.hex(0.5 * 5.0 + 0.5 * seed)
    # ts_rma：Wilder 平滑 ((n-1)*prev+x)/n
    rm = compute_ts("ts_rma(close,3)", {"close": [1.0, 2.0, 3.0, 6.0]})
    assert float.hex(rm[2]) == float.hex(seed)
    assert float.hex(rm[3]) == float.hex((2 * seed + 6.0) / 3.0)
    # 前导 None：前2个有限值(4,6)在 idx3 播种
    lead = compute_ts("ts_ema(close,2)", {"close": [None, None, 4.0, 6.0]})
    assert lead[:3] == [None, None, None] and float.hex(lead[3]) == float.hex(5.0)
    # 嵌套状态算子（MACD DEA 形态）可求值、长度对齐且在慢线暖机前全 None
    longc = [100.0 + i * 0.3 + (i % 5) for i in range(120)]
    dea = compute_ts("ts_ema(ts_ema(close,12)-ts_ema(close,26),9)", {"close": longc})
    assert len(dea) == len(longc) and dea[:25] == [None] * 25 and dea[-1] is not None


def test_corr_and_nested():
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = compute_ts("corr(close,y,3)", {"close": x, "y": [2 * v for v in x]})
    assert out[4] == pytest.approx(1.0) and out[:2] == [None, None]
    nest = compute_ts("delta(delta(close,2),2)/delay(close,4)", {"close": C})
    assert len(nest) == len(C) and nest[0] is None


def test_no_future_leak():
    base = compute_ts("ts_mean(close,5)+ts_std(close,5)", {"close": C})
    pert = list(C)
    pert[-1] += 999.0
    after = compute_ts("ts_mean(close,5)+ts_std(close,5)", {"close": pert})
    # 改最后一根，之前所有位置必须不变
    assert all(base[t] == after[t] for t in range(len(C) - 1))
    assert base[-1] != after[-1]


def test_safe_div_and_log_sign():
    z = compute_ts("close/0", {"close": C})
    assert all(v is None for v in z)
    assert compute_ts("sign(close-104)", {"close": C})[0] == -1
    lg = compute_ts("log(close)", {"close": [1.0, math.e, 0.0, -1.0]})
    assert lg[1] == pytest.approx(1.0) and lg[2] is None and lg[3] is None


def test_tanh_elementwise():
    # G25续：tanh 逐元素算子（时序/截面/标量、非有限安全降级）
    out = compute_ts("tanh(close)", {"close": [0.0, 1.0, -1.0]})
    assert out[0] == 0.0
    assert out[1] == pytest.approx(math.tanh(1.0))
    assert out[2] == pytest.approx(math.tanh(-1.0))
    # 复合表达式：声明式复刻日线动量的一项
    one = compute_ts("tanh(close*160)*2.5", {"close": [0.01]})
    assert one[0] == pytest.approx(math.tanh(0.01 * 160) * 2.5)
    cs = eval_cs("tanh(m)", {"m": {"A": 0.5, "B": -0.5}})
    assert cs["A"] == pytest.approx(math.tanh(0.5)) and cs["B"] == pytest.approx(-math.tanh(0.5))
    assert "tanh" in fe.WHITELIST and fe._EL_OPS["tanh"] == 1


# ---------------- ③ 截面 ----------------
def test_cross_rank_scale_zscore():
    cs = {"m": {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0}}
    cr = eval_cs("cross_rank(m)", cs)
    assert cr["A"] == 0.0 and cr["D"] == pytest.approx(1.0) and cr["B"] == pytest.approx(1 / 3)
    sc = eval_cs("scale(m)", cs)
    assert sum(abs(v) for v in sc.values()) == pytest.approx(1.0)
    zs = eval_cs("zscore(m)", cs)
    assert sum(zs.values()) == pytest.approx(0.0, abs=1e-12)


def test_cross_section_broadcast():
    cs = {"m": {"A": 1.0, "B": 2.0}}
    out = eval_cs("m*2+1", cs)
    assert out["A"] == 3.0 and out["B"] == 5.0


# ---------------- ④ 治理 ----------------
def test_correlations():
    assert fe.pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert fe.spearman([3, 1, 2], [6, 2, 4]) == pytest.approx(1.0)
    assert fe.pearson([1], [1]) is None
    assert fe.pearson([1, 1, 1], [1, 2, 3]) is None  # 零方差


def test_orthogonalize_recovers_beta():
    x1 = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    x2 = [2.0, 1.0, 3.0, 2.0, 4.0, 3.0]
    y = [2 * a + 3 * b for a, b in zip(x1, x2)]
    resid, beta = fe.orthogonalize(y, [x1, x2])
    assert beta[0] == pytest.approx(2.0) and beta[1] == pytest.approx(3.0)
    assert all(abs(r) < 1e-9 for r in resid)


def test_weights_and_combine():
    w = fe.ic_weights([0.3, -0.1])
    assert sum(abs(v) for v in w) == pytest.approx(1.0) and w[0] == pytest.approx(0.75)
    assert fe.equal_weights(4) == [0.25] * 4
    comb = fe.combine([[1.0, 2.0], [3.0, 4.0]], [0.5, 0.5])
    assert comb == [2.0, 3.0]
    iw = fe.icir_weights([[1, 2, 1, 2], [-1, -2, -1, -2]])
    assert iw[0] == pytest.approx(0.5) and iw[1] < 0


# ---------------- ⑤ parity 与因子库 ----------------
def test_structural_parity():
    e = "ts_mean(close,5)/ts_std(close,5)"
    a = compute_ts(e, {"close": C})
    b = compute_ts(e, {"close": list(C)})
    assert a == b


def test_library_compiles_and_registered():
    catalog_keys = set()
    import factors_catalog as fc
    catalog_keys = {r["key"] for r in fc.CATALOG}
    for f in fe.LIBRARY:
        out = compute_ts(f["expr"], {"close": C, "high": [v * 1.005 for v in C],
                                     "low": [v * 0.995 for v in C],
                                     "volume": [1000 + i for i in range(len(C))],
                                     "oi": [5000 + i * 2 for i in range(len(C))]})
        assert len(out) == len(C)
        assert f["key"] in catalog_keys  # 表达式因子必须在唯一注册表登记
    assert fc.validate() == []
