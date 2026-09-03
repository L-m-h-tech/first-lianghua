# -*- coding: utf-8 -*-
"""G25续（第59轮）旧技术因子"过程式 → 表达式"的逐值 parity 台 —— 纯标准库、纯增量、**不切主链**。

G25（第38轮）先落地了白名单表达式引擎 factor_expr 与一批**新研究因子**，并立"回退铁律"：旧技术/
基本面因子保持过程式原实现、综合分逐字节不变。本轮把综合分 live part 真正依赖的几个**旧过程式原始量**
用同一引擎**声明式重写**，并逐条证明它与 futures_data 里的过程式实现是否、以及在什么精度上等价：

  · ret5 / ret20（5/20 日收益）：过程式 technical_profile 写的是 `close/close[-n]-1`。表达式若写成
    `delta(close,n)/delay(close,n)` 代数等价但**运算顺序不同、末位不逐位相等**；本轮刻意写成同运算序的
    `close/delay(close,n)-1`，在随机游走序列上 **float.hex 逐位相等（max_diff=0.0）**。
  · ma5/10/20/60（简单均线）：过程式 _sma_series 用"增量累加/滑窗扣减"，表达式 ts_mean 每个窗用
    `sum` 重算，二者只在最后一两位浮点舍入上不同（实测最大相对差 ~1e-15），**非逐位但 1ULP 级一致**；
    因此主链继续用过程式（回退铁律），表达式版只作离线/研究统一口径，容差与原因在此钉死。
  · 日线动量 part：第59轮给 factor_expr 增补 tanh 逐元素算子后，整条 part 可声明式写成
    `tanh(ret5*160)*2.5+tanh(ret20*70)*2.0+tanh((price/ma10-1)*220)*1.0`（运算顺序与 analyzer 内联
    完全一致），对 factor_parts.legacy_daily_momentum 在网格上 **float.hex 逐位相等**。

分层依赖：顶层只 import 标准库 + factor_expr（纯引擎）；对 futures_data / factor_parts 的真实比对在
函数内**惰性 import**，保证纯函数层在缺主链依赖时也能自测。main.py/analyzer.py/futures_data.py 不得
import 本模块（由 tests 读源码钉死），综合分双哈希基线（equity=c4da4cdf / trades=50dcc80）逐字节不变。
"""
import math
import random

try:
    import factor_expr as fe
except Exception:                  # 极端环境缺引擎时延迟到使用处报错
    fe = None

# 旧过程式原始量 → 声明式表达式（key 与 factors_catalog 登记一致；ret 同运算序以逐位镜像）
RET_EXPRS = {1: "close/delay(close,1)-1", 5: "close/delay(close,5)-1", 20: "close/delay(close,20)-1"}
SMA_PERIODS = (5, 10, 20, 60)
# 日线动量 part 的声明式复刻（ma 项运算顺序/字面量与 analyzer 第62~65行、factor_parts.legacy 完全一致）
DAILY_MOMENTUM_EXPR_FULL = "tanh(ret5*160)*2.5+tanh(ret20*70)*2.0+tanh((price/ma10-1)*220)*1.0"
DAILY_MOMENTUM_EXPR_NOMA = "tanh(ret5*160)*2.5+tanh(ret20*70)*2.0"

# SMA 容差：增量累加 vs 窗内 sum 重算的末位舍入上限（实测 ~1e-15，留足余量取 1e-12 相对容差）
SMA_REL_TOL = 1e-12


def _isnum(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


# =========================== 过程式参考实现（独立书写，不调表达式引擎） ===========================
def procedural_ret_series(closes, n):
    """逐字复刻 futures_data.technical_profile 的 ret 口径并铺成序列：t>=n 且 base>0 才有值。"""
    out = [None] * len(closes)
    for t in range(len(closes)):
        if t >= n and _isnum(closes[t - n]) and closes[t - n] > 0 and _isnum(closes[t]):
            out[t] = closes[t] / closes[t - n] - 1.0
    return out


# =========================== 逐值 parity ===========================
def parity_ret(closes, n):
    """ret{n}：表达式 close/delay-1 vs 过程式，要求**逐位相等**。返回统计 dict。"""
    if fe is None:
        raise RuntimeError("factor_expr 引擎不可用")
    got = fe.compute_ts(RET_EXPRS[n], {"close": list(closes)})
    want = procedural_ret_series(closes, n)
    n_pair = n_hex = 0
    max_diff = 0.0
    mismatches = []
    for a, b in zip(got, want):
        if a is None and b is None:
            continue
        n_pair += 1
        if _isnum(a) and _isnum(b) and float.hex(a) == float.hex(b):
            n_hex += 1
        else:
            mismatches.append((a, b))
        if _isnum(a) and _isnum(b):
            max_diff = max(max_diff, abs(a - b))
    return {"factor": "ret%d" % n, "n_pair": n_pair, "n_hex": n_hex,
            "max_diff": max_diff, "mismatches": mismatches[:5],
            "bit_exact": n_pair == n_hex and max_diff == 0.0}


def parity_sma(closes, p):
    """ma{p}：表达式 ts_mean vs futures_data._sma_series（惰性 import 真过程式），容差级一致（非逐位）。"""
    import futures_data
    if fe is None:
        raise RuntimeError("factor_expr 引擎不可用")
    got = fe.compute_ts("ts_mean(close,%d)" % p, {"close": list(closes)})
    want = futures_data._sma_series(list(closes), p)
    n_pair = n_hex = 0
    max_abs = max_rel = 0.0
    for a, b in zip(got, want):
        if a is None and b is None:
            continue
        if _isnum(a) and _isnum(b):
            n_pair += 1
            if float.hex(a) == float.hex(b):
                n_hex += 1
            max_abs = max(max_abs, abs(a - b))
            if b:
                max_rel = max(max_rel, abs(a - b) / abs(b))
    return {"factor": "ma%d" % p, "n_pair": n_pair, "n_hex": n_hex,
            "max_abs_diff": max_abs, "max_rel_diff": max_rel,
            "within_tol": max_rel <= SMA_REL_TOL}


def daily_momentum_expr_value(ret5, ret20, price, ma10=None):
    """用表达式引擎对单时点求日线动量 part（标量包成长度1序列）；ma10 假值时走两项式。"""
    if fe is None:
        raise RuntimeError("factor_expr 引擎不可用")
    data = {"ret5": [ret5], "ret20": [ret20], "price": [price]}
    expr = DAILY_MOMENTUM_EXPR_NOMA
    if ma10:
        data["ma10"] = [ma10]
        expr = DAILY_MOMENTUM_EXPR_FULL
    out = fe.compute_ts(expr, data)
    return out[-1]


def parity_daily_momentum(seed=20260904, n_random=512):
    """日线动量 part：表达式引擎 vs factor_parts.legacy_daily_momentum，网格+随机逐位相等。"""
    import factor_parts
    grid = (-0.08, -0.02, -0.001, 0.0, 0.001, 0.02, 0.08)
    cases = []
    for r5 in grid:
        for r20 in grid:
            for ma in (None, 0.0, 3000.0, 3500.0, 4000.0):
                cases.append((r5, r20, 3500.0, ma))
    rng = random.Random(seed)
    for _ in range(n_random):
        cases.append((rng.uniform(-0.15, 0.15), rng.uniform(-0.3, 0.3),
                      rng.uniform(50.0, 100000.0),
                      None if rng.random() < 0.15 else rng.uniform(50.0, 100000.0)))
    n_pair = n_hex = 0
    max_diff = 0.0
    mismatches = []
    for r5, r20, price, ma10 in cases:
        ind = {"ret5": r5, "ret20": r20}
        if ma10 is not None:
            ind["ma10"] = ma10
        want = factor_parts.legacy_daily_momentum(ind, price)
        got = daily_momentum_expr_value(r5, r20, price, ma10)
        n_pair += 1
        if _isnum(got) and float.hex(got) == float.hex(want):
            n_hex += 1
        else:
            mismatches.append((r5, r20, price, ma10, got, want))
        if _isnum(got) and _isnum(want):
            max_diff = max(max_diff, abs(got - want))
    return {"factor": "日线动量part", "n_pair": n_pair, "n_hex": n_hex,
            "max_diff": max_diff, "mismatches": mismatches[:5],
            "bit_exact": n_pair == n_hex and max_diff == 0.0}


def _synthetic_closes(seed=20260904, n=420):
    rng = random.Random(seed)
    closes = [100.0]
    for _ in range(n - 1):
        closes.append(max(1.0, closes[-1] * (1 + rng.uniform(-0.03, 0.03))))
    return closes


def parity_report(closes=None):
    """对一条收盘价序列跑全部旧因子表达式化 parity，返回汇总 dict。"""
    closes = closes if closes is not None else _synthetic_closes()
    rep = {"ret": {n: parity_ret(closes, n) for n in sorted(RET_EXPRS)},
           "sma": {p: parity_sma(closes, p) for p in SMA_PERIODS},
           "daily_momentum": parity_daily_momentum()}
    return rep


# =========================== 离线自测（零网络/零DB，纯合成序列） ===========================
def selftest():
    assert fe is not None
    closes = _synthetic_closes()
    # 1) ret1/5/20 表达式与过程式**逐位相等**（同运算序 close/delay-1）
    for n in sorted(RET_EXPRS):
        r = parity_ret(closes, n)
        assert r["n_pair"] > 300 and r["bit_exact"], (n, r["mismatches"], r["max_diff"])
    # 反例自证：delta/delay 写法代数等价（数值容差内），但运算顺序不同、不承诺逐位——故选 close/delay-1
    proc5 = procedural_ret_series(closes, 5)
    delta_form = fe.compute_ts("delta(close,5)/delay(close,5)", {"close": list(closes)})
    assert all((a is None and b is None) or (abs(a - b) <= 1e-12)
               for a, b in zip(delta_form, proc5) if a is not None and b is not None)
    # 2) 手算 ret：t=5 处 close[5]/close[0]-1
    hand = closes[5] / closes[0] - 1.0
    assert float.hex(fe.compute_ts(RET_EXPRS[5], {"close": closes})[5]) == float.hex(hand)
    # 3) ma5/10/20/60 表达式 vs 真过程式 _sma_series：容差内一致，并确认其"非逐位、仅末位差"的事实
    for p in SMA_PERIODS:
        r = parity_sma(closes, p)
        assert r["n_pair"] > 300 and r["within_tol"], (p, r["max_rel_diff"])
        assert 0 <= r["n_hex"] < r["n_pair"]   # 增量累加 vs 窗内重算，存在末位差异
    # 4) 日线动量 part 声明式 vs factor_parts 独立 legacy：网格+随机逐位相等
    dm = parity_daily_momentum()
    assert dm["n_pair"] > 500 and dm["bit_exact"], dm["mismatches"][:3]
    # 5) 无未来函数：改最后一根，之前所有 close 序列表达式输出不变
    for expr in (RET_EXPRS[1], RET_EXPRS[5], RET_EXPRS[20], "ts_mean(close,20)"):
        base = fe.compute_ts(expr, {"close": list(closes)})
        pert = list(closes)
        pert[-1] += 77.0
        after = fe.compute_ts(expr, {"close": pert})
        assert all(base[t] == after[t] for t in range(len(closes) - 1))
    # 6) ma10=0 是假值：日线动量退化为两项式（与 analyzer 的 if ind.get('ma10') 一致）
    v_full = daily_momentum_expr_value(0.01, -0.01, 100.0, 0.0)
    v_noma = daily_momentum_expr_value(0.01, -0.01, 100.0, None)
    assert float.hex(v_full) == float.hex(v_noma) == float.hex(
        math.tanh(0.01 * 160) * 2.5 + math.tanh(-0.01 * 70) * 2.0)
    # 7) 表达式因子均已在 factors_catalog 登记（唯一注册表），且引擎因子库可编译
    import factors_catalog as catalog
    for k in ("expr_ret5_exact", "expr_ret20_exact", "expr_ma10", "expr_part_momentum_decl"):
        assert catalog.by_key(k) is not None, k
    assert catalog.validate() == []
    for f in fe.LIBRARY:
        out = fe.compute_ts(f["expr"], {"close": closes, "volume": [1000 + i for i in range(len(closes))]})
        assert len(out) == len(closes)
    rep = parity_report(closes)
    print("factor_legacy_expr selftest ALL PASS（7组：ret1/5/20逐字节镜像/运算序反例/手算、"
          "SMA容差且钉死非逐位、日线动量声明式逐位 n=%d、无未来、ma假值退化、catalog登记；"
          "SMA最大相对差 ma20=%.2e）"
          % (dm["n_pair"], max(rep["sma"][p]["max_rel_diff"] for p in SMA_PERIODS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
