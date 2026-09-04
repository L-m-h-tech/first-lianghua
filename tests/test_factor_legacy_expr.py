# -*- coding: utf-8 -*-
"""G25续（第59轮）旧技术因子"过程式→表达式"parity 台测试（零网络/零DB，纯合成序列）。

钉死：
  1. ret1/5/20 表达式 close/delay-1 与 futures_data 过程式**逐字节相等**；
  2. ma5/10/20/60 表达式 ts_mean 与增量式 _sma_series 仅末位容差（并钉死"非逐位"这一事实）；
  3. 日线动量 part 用 tanh 声明式复刻，对 factor_parts 独立 legacy **逐字节相等**；
  4. 无未来函数、ma10 假值退化；
  5. 回退铁律：main/analyzer/futures_data 源码不得 import factor_legacy_expr（不切主链）。
"""
import math
import os

import factor_expr as fe
import factor_legacy_expr as fle

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _closes(n=300, seed=123):
    import random
    rng = random.Random(seed)
    c = [100.0]
    for _ in range(n - 1):
        c.append(max(1.0, c[-1] * (1 + rng.uniform(-0.03, 0.03))))
    return c


def test_ret_series_bit_exact():
    c = _closes()
    for n in (1, 5, 20):
        rep = fle.parity_ret(c, n)
        assert rep["bit_exact"] and rep["max_diff"] == 0.0
        assert rep["n_pair"] == rep["n_hex"] > 200


def test_ret_hand_value():
    c = _closes()
    got = fe.compute_ts(fle.RET_EXPRS[5], {"close": c})[5]
    assert float.hex(got) == float.hex(c[5] / c[0] - 1.0)


def test_sma_within_tolerance_but_not_bit_exact():
    c = _closes()
    for p in (5, 10, 20, 60):
        rep = fle.parity_sma(c, p)
        assert rep["within_tol"] and rep["max_rel_diff"] <= fle.SMA_REL_TOL
        assert rep["n_pair"] > 200
        # 增量累加 vs 窗内 sum 重算，长序列上必然存在末位差异（故主链保留过程式）
        assert rep["n_hex"] < rep["n_pair"]


def test_daily_momentum_decl_bit_exact():
    rep = fle.parity_daily_momentum()
    assert rep["bit_exact"] and rep["max_diff"] == 0.0 and rep["n_pair"] == rep["n_hex"] > 500


def test_daily_momentum_ma_falsy():
    a = fle.daily_momentum_expr_value(0.01, -0.01, 100.0, 0.0)
    b = fle.daily_momentum_expr_value(0.01, -0.01, 100.0, None)
    expect = math.tanh(0.01 * 160) * 2.5 + math.tanh(-0.01 * 70) * 2.0
    assert float.hex(a) == float.hex(b) == float.hex(expect)


def test_no_future_leak():
    c = _closes()
    for expr in (fle.RET_EXPRS[5], fle.RET_EXPRS[20], "ts_mean(close,20)"):
        base = fe.compute_ts(expr, {"close": list(c)})
        pert = list(c)
        pert[-1] += 999.0
        after = fe.compute_ts(expr, {"close": pert})
        assert all(base[t] == after[t] for t in range(len(c) - 1))


def test_boll_std_bit_exact():
    c = _closes(n=420)
    rep = fle.parity_boll_std(c, 20)
    assert rep["bit_exact"] and rep["max_diff"] == 0.0 and rep["n_pair"] == rep["n_hex"] > 300


def test_hv20_bit_exact():
    c = _closes(n=420)
    rep = fle.parity_hv20(c, 20)
    assert rep["bit_exact"] and rep["max_diff"] == 0.0 and rep["n_pair"] == rep["n_hex"] > 300
    assert abs(fle._SQRT252 - math.sqrt(252.0)) == 0.0


def test_orthogonal_ic_blend():
    import random
    rng = random.Random(99)
    n = 200
    base = [rng.gauss(0, 1) for _ in range(n)]
    f1 = list(base)
    f2 = [0.7 * base[t] + 0.3 * rng.gauss(0, 1) for t in range(n)]
    f3 = [rng.gauss(0, 1) for _ in range(n)]
    ob = fle.orthogonal_ic_blend([f1, f2, f3], [0.3, 0.2, 0.1])
    assert abs(sum(ob["weights"]) - 1.0) < 1e-12
    # f2 残差对基底 f1 近似正交
    resid = ob["residuals"][1]
    cov = sum(base[t] * resid[t] for t in range(n)) / n
    assert abs(cov) < 0.05
    assert all(isinstance(x, float) for x in ob["blend"])
    # 单因子退化
    one = fle.orthogonal_ic_blend([f1], [0.5])
    assert one["weights"] == [1.0] and one["residuals"][0] == f1
    # 因子数/IC数不一致报错
    import pytest
    with pytest.raises(ValueError):
        fle.orthogonal_ic_blend([f1, f2], [0.1])


def test_macd_stateful_bit_exact():
    c = _closes(n=420)
    for k, r in fle.parity_macd(c).items():
        assert r["bit_exact"] and r["n_pair"] == r["n_hex"] > 300, (k, r["mismatches"][:2])


def test_rsi_nonflat_bit_exact_and_flat_branch():
    c = _closes(n=420)
    r = fle.parity_rsi(c)
    assert r["bit_exact_nonflat"] and r["n_pair"] == r["n_hex"] > 300
    # 单边上涨触发过程式 avg_loss≈0 强制 100 分支：非平盘处仍逐位、且数到平盘分支
    up = [100.0 + i for i in range(60)]
    ru = fle.parity_rsi(up)
    assert ru["bit_exact_nonflat"] and ru["n_flat"] >= 1


def test_parity_report_and_catalog():
    rep = fle.parity_report(_closes())
    assert set(rep["ret"]) == {1, 5, 20} and set(rep["sma"]) == {5, 10, 20, 60}
    assert all(r["bit_exact"] for r in rep["ret"].values())
    assert all(r["within_tol"] for r in rep["sma"].values())
    assert rep["boll_std"]["bit_exact"] and rep["hv20"]["bit_exact"]
    assert rep["daily_momentum"]["bit_exact"]
    assert all(r["bit_exact"] for r in rep["macd"].values())
    assert rep["rsi"]["bit_exact_nonflat"]
    import factors_catalog as catalog
    for k in ("expr_ret5_exact", "expr_ret20_exact", "expr_ma10", "expr_part_momentum_decl",
              "expr_ma5", "expr_ma20", "expr_ma60", "expr_boll_std20", "expr_hv20",
              "expr_macd_dif", "expr_macd_dea", "expr_macd_hist", "expr_rsi14"):
        assert catalog.by_key(k) is not None and catalog.by_key(k)["status"] == "research"
    assert catalog.validate() == []


def test_main_chain_does_not_import_legacy_expr():
    # 回退铁律：生产链不得 import 旧因子表达式化 parity 台
    for fn in ("main.py", "analyzer.py", "futures_data.py"):
        src = open(os.path.join(ROOT, fn), encoding="utf-8").read()
        assert "factor_legacy_expr" not in src, "%s 不得 import factor_legacy_expr（G25续仍不切主链）" % fn
