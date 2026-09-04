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
  · 第60轮再扩：ma5/20/60 同 SMA 末位容差；布林20日样本标准差 ts_std(close,20) 与年化历史波动率
    ts_std(log(close/delay(close,1)),20)*sqrt252 因**求和顺序与过程式完全一致**而 float.hex 逐位相等；
    并提供 orthogonal_ic_blend（顺序正交去共线 + |IC| 有符号归一加权）作为 G16 浅ML 样本未齐前的
    可解释线性合成基线（纯研究侧、不进综合分）。

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


def parity_boll_std(closes, p=20):
    """布林标准差：表达式 ts_std(close,p) vs futures_data._sample_std(窗内)，同求和序要求**逐位相等**。"""
    import futures_data
    got = fe.compute_ts("ts_std(close,%d)" % p, {"close": list(closes)})
    n_pair = n_hex = 0
    max_diff = 0.0
    mismatches = []
    for t in range(len(closes)):
        seg = closes[max(0, t - p + 1):t + 1]
        want = futures_data._sample_std(seg) if len(seg) >= p else None
        a = got[t]
        if want is None and a is None:
            continue
        n_pair += 1
        if _isnum(a) and _isnum(want) and float.hex(a) == float.hex(want):
            n_hex += 1
        else:
            mismatches.append((t, a, want))
        if _isnum(a) and _isnum(want):
            max_diff = max(max_diff, abs(a - want))
    return {"factor": "boll_std%d" % p, "n_pair": n_pair, "n_hex": n_hex,
            "max_diff": max_diff, "mismatches": mismatches[:5],
            "bit_exact": n_pair == n_hex and max_diff == 0.0}


# sqrt(252) 的精确 float 字面量（=math.sqrt(252)），保证年化乘法与过程式逐位一致
_SQRT252 = 15.874507866387544
HV20_EXPR = "ts_std(log(close/delay(close,1)),20)*%r" % _SQRT252

# ===== 第61轮：状态量 MACD/RSI 的声明式复刻（依赖 DSL 新增 ts_ema/ts_rma 状态递推算子） =====
# 周期取自 config（TECH_MACD_FAST/SLOW/SIGNAL=12/26/9，TECH_RSI_PERIOD=14）；表达式内用字面量、逐字对齐运算序。
MACD_DIF_EXPR = "ts_ema(close,12)-ts_ema(close,26)"
MACD_DEA_EXPR = "ts_ema(ts_ema(close,12)-ts_ema(close,26),9)"
MACD_HIST_EXPR = ("(ts_ema(close,12)-ts_ema(close,26)"
                  "-ts_ema(ts_ema(close,12)-ts_ema(close,26),9))*2.0")
# Wilder RSI：g/l 用 ts_rma（=avg_gain/avg_loss 平滑），外层运算序与 _rsi_series 完全一致
RSI14_EXPR = ("100.0-100.0/(1.0+ts_rma(max(close-delay(close,1),0.0),14)"
              "/ts_rma(max(delay(close,1)-close,0.0),14))")

# ===== 第63轮：EMA 列（MACD 底层 12/26）与 KDJ（非 close-only，吃 high/low）声明式复刻 =====
EMA_EXPRS = {12: "ts_ema(close,12)", 26: "ts_ema(close,26)"}
KDJ_K_EXPR = "kdj_sm(kdj_rsv(high,low,close,9),9)"
KDJ_D_EXPR = "kdj_sm(kdj_sm(kdj_rsv(high,low,close,9),9),9)"
KDJ_J_EXPR = ("3.0*kdj_sm(kdj_rsv(high,low,close,9),9)"
              "-2.0*kdj_sm(kdj_sm(kdj_rsv(high,low,close,9),9),9)")


def _procedural_macd(closes):
    """逐字复刻 futures_data.technical_profile 的 MACD 块（dif/dea/hist 三条序列）。"""
    import futures_data
    import config
    ema_fast = futures_data._ema_series(closes, config.TECH_MACD_FAST)
    ema_slow = futures_data._ema_series(closes, config.TECH_MACD_SLOW)
    n = len(closes)
    dif_s, dea_s, hist_s = [None] * n, [None] * n, [None] * n
    dif_values = []
    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            dif_values.append((i, ema_fast[i] - ema_slow[i]))
    if dif_values:
        dea_only = futures_data._ema_series([v for _, v in dif_values], config.TECH_MACD_SIGNAL)
        for (i, d), e in zip(dif_values, dea_only):
            dif_s[i] = d
            dea_s[i] = e
            hist_s[i] = None if e is None else (d - e) * 2.0
    return {"dif": dif_s, "dea": dea_s, "hist": hist_s}


def _bit_cmp(name, got, want):
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
    return {"factor": name, "n_pair": n_pair, "n_hex": n_hex, "max_diff": max_diff,
            "mismatches": mismatches[:5], "bit_exact": n_pair == n_hex and max_diff == 0.0}


def parity_macd(closes):
    """MACD dif/dea/hist：声明式 vs 过程式，三条序列均要求**逐位相等**（嵌套 ts_ema 复刻对 dif 连续子序列再EMA）。"""
    dif = fe.compute_ts(MACD_DIF_EXPR, {"close": list(closes)})
    dea = fe.compute_ts(MACD_DEA_EXPR, {"close": list(closes)})
    hist = fe.compute_ts(MACD_HIST_EXPR, {"close": list(closes)})
    want = _procedural_macd(closes)
    return {k: _bit_cmp("macd_" + k, got, want[k]) for k, got in
            (("dif", dif), ("dea", dea), ("hist", hist))}


def parity_rsi(closes, period=14):
    """Wilder RSI：声明式 vs futures_data._rsi_series。

    非平盘分支（avg_loss>1e-12）要求**逐位相等**；过程式在 avg_loss<=1e-12 时强制返回 100（平盘/单边），
    表达式 g/l 安全除法在 l≈0 处给 None——这一分支单独计数 n_flat，是唯一允许的、已钉死的口径差异。
    """
    import futures_data
    got = fe.compute_ts(RSI14_EXPR, {"close": list(closes)})
    want = futures_data._rsi_series(list(closes), period)
    n_pair = n_hex = n_flat = 0
    max_diff = 0.0
    mismatches = []
    for a, b in zip(got, want):
        if a is None and b is None:
            continue
        if b == 100.0:                    # 过程式平盘/单边强制分支
            n_flat += 1
            continue
        n_pair += 1
        if _isnum(a) and _isnum(b) and float.hex(a) == float.hex(b):
            n_hex += 1
        else:
            mismatches.append((a, b))
        if _isnum(a) and _isnum(b):
            max_diff = max(max_diff, abs(a - b))
    return {"factor": "rsi%d" % period, "n_pair": n_pair, "n_hex": n_hex, "n_flat": n_flat,
            "max_diff": max_diff, "mismatches": mismatches[:5],
            "bit_exact_nonflat": n_pair == n_hex and max_diff == 0.0}


def parity_ema(closes, period):
    """EMA{period} 列：表达式 ts_ema vs futures_data._ema_series（SMA播种），要求**逐位相等**。"""
    import futures_data
    got = fe.compute_ts(EMA_EXPRS[period], {"close": list(closes)})
    want = futures_data._ema_series(list(closes), period)
    return _bit_cmp("ema%d" % period, got, want)


def _synthetic_ohlc(seed=20260904, n=420):
    """合成 high>=close>=low 的 OHLC 随机游走（KDJ 非 close-only，需三序列且高低夹住收盘）。"""
    rng = random.Random(seed + 1)
    closes = _synthetic_closes(seed, n)
    highs, lows = [], []
    for c in closes:
        w = c * rng.uniform(0.001, 0.012)
        highs.append(c + w)
        lows.append(max(0.01, c - w * rng.uniform(0.5, 1.0)))
    return highs, lows, closes


def parity_kdj(highs, lows, closes, period=9):
    """KDJ K/D/J：声明式 vs futures_data._kdj_series（固定初值50、α=1/3、当拍新K喂D），要求三序列**逐位相等**。"""
    import futures_data
    data = {"high": list(highs), "low": list(lows), "close": list(closes)}
    got_k = fe.compute_ts(KDJ_K_EXPR, data)
    got_d = fe.compute_ts(KDJ_D_EXPR, data)
    got_j = fe.compute_ts(KDJ_J_EXPR, data)
    wk, wd, wj = futures_data._kdj_series(list(highs), list(lows), list(closes), period)
    return {"k": _bit_cmp("kdj_k", got_k, wk), "d": _bit_cmp("kdj_d", got_d, wd),
            "j": _bit_cmp("kdj_j", got_j, wj)}


def parity_hv20(closes, window=20):
    """20日历史波动率年化：表达式 log收益样本std*sqrt252 vs futures_data._hv_at，同运算序要求**逐位相等**。"""
    import futures_data
    got = fe.compute_ts(HV20_EXPR, {"close": list(closes)})
    n_pair = n_hex = 0
    max_diff = 0.0
    mismatches = []
    for t in range(len(closes)):
        want = futures_data._hv_at(closes, t, window)
        a = got[t]
        if want is None and a is None:
            continue
        n_pair += 1
        if _isnum(a) and _isnum(want) and float.hex(a) == float.hex(want):
            n_hex += 1
        else:
            mismatches.append((t, a, want))
        if _isnum(a) and _isnum(want):
            max_diff = max(max_diff, abs(a - want))
    return {"factor": "hv%d" % window, "n_pair": n_pair, "n_hex": n_hex,
            "max_diff": max_diff, "mismatches": mismatches[:5],
            "bit_exact": n_pair == n_hex and max_diff == 0.0}


def orthogonal_ic_blend(factor_matrix, ics, sequential=True):
    """G16 前置（研究侧、不进综合分）：逐因子对"已正交残差"再正交去共线，再按 |IC| 有符号归一加权合成。

    factor_matrix: [因子时序...]（等长，缺失位置 None）；ics: 各因子前瞻 IC（有符号）。
    顺序 Schmidt 式正交（每个因子只对前面已残差化的因子回归取残差），再用 factor_expr.ic_weights/combine。
    返回 {"blend":合成序列,"weights":IC权重(和=1),"residuals":[残差序列...],"betas":[回归系数...]}。
    G16 浅 ML 样本未齐前，它提供一条可解释的"正交+IC"线性合成基线；样本齐后由 ml_inference 接管。
    """
    if fe is None:
        raise RuntimeError("factor_expr 引擎不可用")
    k = len(factor_matrix)
    if k != len(ics):
        raise ValueError("因子数与 IC 数不一致")
    residuals, betas = [], []
    for i in range(k):
        if sequential and residuals:
            resid, beta = fe.orthogonalize(factor_matrix[i], residuals)
        else:
            resid, beta = list(factor_matrix[i]), []
        residuals.append(resid)
        betas.append(beta)
    weights = fe.ic_weights(list(ics))
    blend = fe.combine(residuals, weights)
    return {"blend": blend, "weights": weights, "residuals": residuals, "betas": betas}


def parity_report(closes=None):
    """对一条收盘价序列跑全部旧因子表达式化 parity，返回汇总 dict。"""
    closes = closes if closes is not None else _synthetic_closes()
    rep = {"ret": {n: parity_ret(closes, n) for n in sorted(RET_EXPRS)},
           "sma": {p: parity_sma(closes, p) for p in SMA_PERIODS},
           "boll_std": parity_boll_std(closes),
           "hv20": parity_hv20(closes),
           "macd": parity_macd(closes),
           "rsi": parity_rsi(closes),
           "ema": {p: parity_ema(closes, p) for p in sorted(EMA_EXPRS)},
           "kdj": parity_kdj(*_synthetic_ohlc()),
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
    for k in ("expr_ret5_exact", "expr_ret20_exact", "expr_ma10", "expr_part_momentum_decl",
              "expr_ma5", "expr_ma20", "expr_ma60", "expr_boll_std20", "expr_hv20",
              "expr_macd_dif", "expr_macd_dea", "expr_macd_hist", "expr_rsi14",
              "expr_ema12", "expr_ema26", "expr_kdj_k", "expr_kdj_d", "expr_kdj_j"):
        assert catalog.by_key(k) is not None, k
    assert catalog.validate() == []
    _h = [v * 1.004 for v in closes]
    _l = [v * 0.996 for v in closes]
    for f in fe.LIBRARY:
        out = fe.compute_ts(f["expr"], {"close": closes, "high": _h, "low": _l,
                                        "volume": [1000 + i for i in range(len(closes))]})
        assert len(out) == len(closes)
    # 8) 第60轮新增：boll_std / hv20 与过程式**同求和序逐位相等**（非容差）
    rb, rh = parity_boll_std(closes), parity_hv20(closes)
    assert rb["n_pair"] > 300 and rb["bit_exact"], rb["mismatches"][:3]
    assert rh["n_pair"] > 300 and rh["bit_exact"], rh["mismatches"][:3]
    assert abs(_SQRT252 - math.sqrt(252.0)) == 0.0
    # 9) 正交IC加权（G16前置）：残差对基底近似不相关、权重和=1、合成有限且保留主导方向
    rng2 = random.Random(20260904)
    n = 240
    base = [rng2.gauss(0, 1) for _ in range(n)]
    f1 = [x for x in base]
    f2 = [0.8 * base[t] + 0.2 * rng2.gauss(0, 1) for t in range(n)]   # 与 f1 强共线
    f3 = [rng2.gauss(0, 1) for _ in range(n)]                          # 独立因子
    ob = orthogonal_ic_blend([f1, f2, f3], [0.30, 0.20, 0.10])
    assert abs(sum(ob["weights"]) - 1.0) < 1e-12 and len(ob["weights"]) == 3
    resid2 = ob["residuals"][1]
    cov = sum(base[t] * resid2[t] for t in range(n)) / n              # 正交后与基底协方差≈0
    var = sum(resid2[t] * resid2[t] for t in range(n)) / n
    assert var > 0 and abs(cov) < 0.05, (cov, var)
    assert all(_isnum(x) for x in ob["blend"])
    # 单因子退化：正交无基底时残差=原序列、权重=1
    ob1 = orthogonal_ic_blend([f1], [0.4])
    assert ob1["weights"] == [1.0] and ob1["residuals"][0] == f1
    # 10) 第61轮：MACD 三序列嵌套 ts_ema 逐位、RSI 非平盘逐位（平盘强制100分支单独计数）
    for k, mr in parity_macd(closes).items():
        assert mr["n_pair"] > 300 and mr["bit_exact"], (k, mr["mismatches"][:3])
    rr = parity_rsi(closes)
    assert rr["n_pair"] > 300 and rr["bit_exact_nonflat"], rr["mismatches"][:3]
    # 构造单边上涨序列触发 RSI 平盘/单边强制分支：非平盘处仍逐位、且确实数到 n_flat
    up = [100.0 + i for i in range(60)]
    rr_up = parity_rsi(up)
    assert rr_up["bit_exact_nonflat"] and rr_up["n_flat"] >= 1
    # ts_ema/ts_rma 无未来：改最后一根不影响之前
    for expr in ("ts_ema(close,12)", MACD_DEA_EXPR, RSI14_EXPR):
        b0 = fe.compute_ts(expr, {"close": list(closes)})
        pp = list(closes); pp[-1] += 500.0
        b1 = fe.compute_ts(expr, {"close": pp})
        assert all(b0[t] == b1[t] for t in range(len(closes) - 1))
    # 11) 第63轮：EMA12/26 列逐位、KDJ K/D/J 三序列逐位（非 close-only，喂 high/low）
    for p in sorted(EMA_EXPRS):
        re = parity_ema(closes, p)
        assert re["n_pair"] > 300 and re["bit_exact"], ("ema%d" % p, re["mismatches"][:3])
    hs, ls, cs = _synthetic_ohlc()
    kd = parity_kdj(hs, ls, cs)
    for key, kr in kd.items():
        assert kr["n_pair"] > 300 and kr["bit_exact"], (key, kr["n_pair"], kr["mismatches"][:3])
    # KDJ 无未来：改最后一根 high/low/close，之前 K 输出不变
    b0 = fe.compute_ts(KDJ_K_EXPR, {"high": hs, "low": ls, "close": cs})
    h2, l2, c2 = list(hs), list(ls), list(cs)
    h2[-1], l2[-1], c2[-1] = h2[-1] * 2, 0.01, c2[-1]
    b1 = fe.compute_ts(KDJ_K_EXPR, {"high": h2, "low": l2, "close": c2})
    assert all(b0[t] == b1[t] for t in range(len(cs) - 1))
    rep = parity_report(closes)
    print("factor_legacy_expr selftest ALL PASS（11组：ret1/5/20逐字节镜像/运算序反例/手算、"
          "SMA容差且钉死非逐位、日线动量声明式逐位 n=%d、无未来、ma假值退化、catalog登记、"
          "boll/hv同求和序逐位 n=%d/%d、正交IC去共线、MACD三序列嵌套ts_ema逐位/RSI非平盘逐位(平盘分支n=%d)；"
          "SMA最大相对差 ma20=%.2e）"
          % (dm["n_pair"], rb["n_pair"], rh["n_pair"], rr["n_flat"],
             max(rep["sma"][p]["max_rel_diff"] for p in SMA_PERIODS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
