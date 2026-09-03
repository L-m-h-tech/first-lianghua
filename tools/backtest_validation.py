# -*- coding: utf-8 -*-
"""
backtest_validation.py — 回测样本外验证与防过拟合工具箱（WP-F4 前置 / AFML ch7、11-12）

定位：研究侧、离线、纯标准库（math/statistics/itertools/csv），不进常驻监控链路、不改任何生产打分。
解决一个问题：**"在一堆候选（参数组合/因子/模型）里挑出回测最好的那个"，这个挑选动作本身有多大概率是在拟合噪声。**

五块方法（公式来自公开论文，代码自写，无第三方依赖）：
  1) 收益矩 + PSR/DSR —— Bailey & López de Prado (2014), The Deflated Sharpe Ratio
     同时校正：样本长度 T、收益偏度/峰度（非正态）、"试了 N 次才挑到最好"的多重试验偏差。
  2) CSCV-PBO —— Bailey, Borwein, López de Prado & Zhu (2014), The Probability of Backtest Overfitting
     组合对称交叉验证：估计"样本内最优策略在样本外落入下半区"的概率，PBO<0.5 才说明选优过程优于抛硬币。
  3) PurgedKFold + Embargo —— López de Prado, AFML ch7：时序样本禁止随机 K 折，
     标签窗口横跨折边界的训练样本要 purge，折后再加 embargo，防前视泄漏。
  4) Walk-forward —— 滚动"前窗选参、后窗验证"，量化 IS→OOS 衰减、OOS 跑赢中位数比例、选参稳定性。
  5) 参数高原 vs 孤峰 —— 最优点邻域是否同样好（高原=稳健）还是四周骤降（孤峰=过拟合），给粗糙度。

用法：
  python tools/backtest_validation.py --selftest                      # 零网络合成断言
  python tools/backtest_validation.py --dsr-equity reports/portfolio_equity.csv --trials 18
  python tools/backtest_validation.py --grid RB --period 30           # 单品种参数网格 CSCV/WF/高原
  python tools/backtest_validation.py --grid RB,HC --period 30        # 多品种逐评
  python tools/backtest_validation.py --all-grid --period 30          # 全品种（较慢）
产出：reports/backtest_validation.txt（utf-8-sig）
"""
import argparse
import json
import csv
import itertools
from datetime import datetime
import math
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EULER_MASCHERONI = 0.5772156649015329


# ============================================================
# 1. 基础数值：正态 CDF/PPF、收益矩、Sharpe
# ============================================================
def norm_cdf(z):
    """标准正态分布函数 Φ(z)，用 math.erf。"""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def norm_ppf(p):
    """标准正态分位函数 Φ⁻¹(p)，Peter Acklam 有理逼近，全域误差 < 1.15e-9。"""
    if not 0.0 < p < 1.0:
        if p == 0.0 or p == 1.0:
            raise ValueError("norm_ppf: p 必须在 (0,1) 开区间")
        raise ValueError("norm_ppf: p 越界 %r" % p)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1.0 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)


def moments(xs):
    """返回 mean/std/偏度g3/Pearson峰度g4（正态 g3=0、g4=3，注意是非超额峰度）/T。"""
    n = len(xs)
    if n < 2:
        m = xs[0] if n else 0.0
        return m, 0.0, 0.0, 3.0, n
    mean = statistics.fmean(xs)
    m2 = sum((x - mean) ** 2 for x in xs) / n
    m3 = sum((x - mean) ** 3 for x in xs) / n
    m4 = sum((x - mean) ** 4 for x in xs) / n
    std = math.sqrt(m2)
    g3 = m3 / (m2 ** 1.5) if m2 > 1e-18 else 0.0
    g4 = m4 / (m2 ** 2) if m2 > 1e-18 else 3.0
    return mean, std, g3, g4, n


def per_period_sharpe(xs):
    """非年化、逐期 Sharpe（DSR/CSCV 一律用同频率 per-period，避免年化口径混入统计量）。"""
    mean, std, _, _, n = moments(xs)
    if n < 2 or std <= 1e-18:
        return 0.0
    return mean / std


# ============================================================
# 2. PSR / DSR
# ============================================================
def prob_sharpe_ratio(sr_obs, sr_bench, t_obs, skew, kurt_p):
    """PSR：真实 Sharpe 高于基准 sr_bench 的概率（校正非正态与样本长度）。"""
    if t_obs < 2:
        return 0.5
    denom_sq = 1.0 - skew * sr_obs + (kurt_p - 1.0) / 4.0 * sr_obs * sr_obs
    denom = math.sqrt(max(1e-12, denom_sq))
    z = (sr_obs - sr_bench) * math.sqrt(t_obs - 1) / denom
    return norm_cdf(z)


def expected_max_sharpe(n_trials, sr_std):
    """
    零真实优势原假设下，独立试 n_trials 次能"蒙到"的最大 per-period Sharpe 期望。
    sr_std：单次试验 Sharpe 估计量在零假设下的标准差，独立同分布收益时 ≈ 1/√T。
    """
    if n_trials is None or n_trials <= 1:
        return 0.0
    z1 = norm_ppf(1.0 - 1.0 / n_trials)
    z2 = norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    return sr_std * ((1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2)


def deflated_sharpe(returns, n_trials, sr_std=None):
    """
    输入一期期收益率（逐笔/逐日均可，但要与 n_trials、T 同口径），返回完整 DSR 诊断 dict。
    n_trials：为得到本策略实际试过的候选个数（参数网格×因子×品种选择等），用于多重试验校正。
    """
    rs = [float(x) for x in returns]
    mean, std, g3, g4, t_obs = moments(rs)
    sr = per_period_sharpe(rs)
    if sr_std is None:
        sr_std = 1.0 / math.sqrt(t_obs) if t_obs >= 1 else 0.0
    sr0 = expected_max_sharpe(n_trials, sr_std)
    psr_zero = prob_sharpe_ratio(sr, 0.0, t_obs, g3, g4)
    dsr = prob_sharpe_ratio(sr, sr0, t_obs, g3, g4)
    return {"sr_per_period": sr, "sr0_multiple_trial": sr0, "psr_vs_zero": psr_zero,
            "dsr": dsr, "t": t_obs, "mean": mean, "std": std, "skew": g3, "kurt": g4,
            "n_trials": n_trials}


# ============================================================
# 3. CSCV - PBO
# ============================================================
def _col_series(matrix, col, rows):
    return [matrix[r][col] for r in rows]


def cscv_pbo(matrix, n_blocks=10, max_combos=4000):
    """
    组合对称交叉验证估计 PBO。
    matrix: T 行（按时间先后）× N 列（N 个候选策略），元素为该时间块该候选的逐期收益率。
    返回 dict：pbo、logits 列表、评估的折数 combos、候选数 n_strats、块数 n_blocks。
    PBO = 样本内最优候选在样本外绩效排名落入下半区（logit 相对秩 <=0）的折数占比。
    """
    t = len(matrix)
    if t == 0:
        raise ValueError("cscv_pbo: 空矩阵")
    n = len(matrix[0])
    if n < 2:
        raise ValueError("cscv_pbo: 至少需要 2 个候选策略列")
    if any(len(row) != n for row in matrix):
        raise ValueError("cscv_pbo: 矩阵列数不一致")
    s = max(2, min(n_blocks, t))
    if s % 2 == 1:
        s -= 1  # CSCV 需要对半分
    block_of = [min(s - 1, i * s // t) for i in range(t)]
    blocks = [[] for _ in range(s)]
    for i, b in enumerate(block_of):
        blocks[b].append(i)
    half = s // 2
    all_combos = list(itertools.combinations(range(s), half))
    if len(all_combos) > max_combos:
        step = len(all_combos) / max_combos
        picked = [all_combos[int(k * step)] for k in range(max_combos)]
        all_combos = picked
    logits, detail = [], []
    for combo in all_combos:
        is_blocks = set(combo)
        is_rows = [i for i in range(t) if block_of[i] in is_blocks]
        oos_rows = [i for i in range(t) if block_of[i] not in is_blocks]
        is_perf = [per_period_sharpe(_col_series(matrix, j, is_rows)) for j in range(n)]
        oos_perf = [per_period_sharpe(_col_series(matrix, j, oos_rows)) for j in range(n)]
        nstar = max(range(n), key=lambda j: is_perf[j])
        below = sum(1 for v in oos_perf if v < oos_perf[nstar])
        equal = sum(1 for v in oos_perf if v == oos_perf[nstar])
        omega = (below + 0.5 * equal + 0.5) / n  # 相对秩 ∈ (0,1)，并列取中位
        omega = min(1.0 - 1e-9, max(1e-9, omega))
        lam = math.log(omega / (1.0 - omega))
        logits.append(lam)
        detail.append({"is_best": nstar, "oos_rank_omega": omega, "logit": lam})
    pbo = sum(1 for lam in logits if lam <= 0.0) / len(logits)
    return {"pbo": pbo, "logits": logits, "detail": detail, "combos": len(logits),
            "n_strats": n, "n_blocks": s,
            "median_logit": statistics.median(logits) if logits else 0.0}


# ============================================================
# 4. Purged K-Fold + Embargo
# ============================================================
def purged_kfold_splits(n, t1=None, n_splits=5, embargo=0):
    """
    AFML ch7 时序防泄漏 K 折（生成器），逐折 yield (train_idx, test_idx)（均为升序 list）。
    n          : 样本数，样本按时间先后编号 0..n-1。
    t1         : 长度 n，t1[i]=样本 i 的标签"到期"下标（>=i，triple-barrier 的 exit 位置）；
                 None 表示标签不跨期（t1[i]=i）。
    embargo    : 测试块前后各剔除多少个训练样本（整数个样本）。
    被 purge：训练样本 i 的标签窗口 [i, t1[i]] 与测试区间 [a,b) 相交。
    """
    if n_splits < 2:
        raise ValueError("n_splits 至少为 2")
    if t1 is None:
        t1 = list(range(n))
    else:
        t1 = [i if v is None else int(v) for i, v in enumerate(t1)]
        if len(t1) != n:
            raise ValueError("t1 长度必须等于 n")
    for k in range(n_splits):
        a = k * n // n_splits
        b = (k + 1) * n // n_splits
        if a == b:
            continue
        train = []
        for i in range(n):
            end = max(t1[i], i)
            # purge：[i,end] 与 [a,b) 相交则剔除
            if not (end <= a or i >= b):
                continue
            # embargo：测试块两侧各留 embargo 缓冲
            if (a - embargo) <= i < a or b <= i < (b + embargo):
                continue
            train.append(i)
        test = list(range(a, b))
        yield train, test


# ============================================================
# 5. Walk-forward 滚动选参/验证
# ============================================================
def walk_forward(matrix, train_size, test_size, step=None, purge=0, embargo=0):
    """
    滚动：每个时点用前 train_size 行按 Sharpe 选最优候选，在后 test_size 行样本外评估它。
    返回每段明细与汇总（OOS 选中候选均值、事后最优均值、衰减、跑赢中位数比例、选参切换次数）。

    第52轮 G27续：AFML ch7 防前视隔离带（默认都为 0，逐段结果与旧版完全一致）：
    - purge   : 从 IS 窗尾部剔除多少行（这些样本的持有期标签会向前延伸进 OOS，形成泄漏）；
    - embargo : IS 与 OOS 之间额外留多少行禁运带（OOS 起点整体后移），吸收标签重叠与序列相关。
    加隔离带后 IS 有效长度=train_size-purge、OOS=[train+embargo, train+embargo+test)。
    """
    t = len(matrix)
    n = len(matrix[0]) if t else 0
    step = test_size if step is None else step
    purge = max(0, int(purge)); embargo = max(0, int(embargo))
    if train_size - purge < 2:
        return {"segments": [], "n_segments": 0, "purge": purge, "embargo": embargo}
    segs = []
    start = 0
    while start + train_size + embargo + test_size <= t:
        is_rows = list(range(start, start + train_size - purge))
        oos_rows = list(range(start + train_size + embargo,
                             start + train_size + embargo + test_size))
        is_perf = [per_period_sharpe(_col_series(matrix, j, is_rows)) for j in range(n)]
        oos_perf = [per_period_sharpe(_col_series(matrix, j, oos_rows)) for j in range(n)]
        chosen = max(range(n), key=lambda j: is_perf[j])
        oos_sorted = sorted(oos_perf)
        med = statistics.median(oos_sorted)
        segs.append({"start": start, "chosen": chosen,
                     "is_sharpe": is_perf[chosen], "oos_sharpe": oos_perf[chosen],
                     "oos_best": max(oos_perf), "oos_median": med,
                     "beat_median": oos_perf[chosen] > med})
        start += step
    if not segs:
        return {"segments": [], "n_segments": 0}
    chosen_vals = [g["oos_sharpe"] for g in segs]
    best_vals = [g["oos_best"] for g in segs]
    is_vals = [g["is_sharpe"] for g in segs]
    switches = sum(1 for k in range(1, len(segs)) if segs[k]["chosen"] != segs[k - 1]["chosen"])
    mean_is = statistics.fmean(is_vals)
    mean_oos = statistics.fmean(chosen_vals)
    # IS 本身接近 0 时衰减比例无意义（分母趋零会爆百分比），返回 None 由渲染层标注
    decay = ((mean_is - mean_oos) / abs(mean_is)) if abs(mean_is) >= 0.05 else None
    return {"segments": segs, "n_segments": len(segs),
            "mean_is_sharpe": mean_is, "mean_oos_sharpe": mean_oos,
            "mean_oos_best": statistics.fmean(best_vals),
            "is_oos_decay": decay, "purge": purge, "embargo": embargo,
            "oos_beat_median_rate": sum(g["beat_median"] for g in segs) / len(segs),
            "param_switch_rate": switches / max(1, len(segs) - 1)}


# ============================================================
# 6. 参数高原 vs 孤峰
# ============================================================
def parameter_plateau(perf_map):
    """
    perf_map: {离散层坐标 tuple(int,...): 绩效值}。坐标每维是该参数取值排序后的"层号"。
    返回最优点、邻域均值、plateau_ratio（邻域均值/最优点，越接近1越像高原）、
    邻域正收益占比、整体粗糙度（相邻格绩效跳变的平均幅度，按|最优点|归一）。
    """
    if not perf_map:
        raise ValueError("parameter_plateau: 空绩效表")
    keys = list(perf_map.keys())
    ndim = len(keys[0])
    if any(len(k) != ndim for k in keys):
        raise ValueError("参数坐标维度不一致")
    best = max(keys, key=lambda k: perf_map[k])
    best_v = perf_map[best]
    # 邻域：与最优点曼哈顿距离恰好 1
    neighbors = [k for k in keys
                 if sum(abs(a - b) for a, b in zip(k, best)) == 1]
    neigh_vals = [perf_map[k] for k in neighbors]
    neigh_mean = statistics.fmean(neigh_vals) if neigh_vals else None
    denom = abs(best_v) if abs(best_v) > 1e-12 else 1.0
    plateau_ratio = (neigh_mean / best_v) if neigh_mean is not None and best_v != 0 else None
    neigh_positive = (sum(1 for v in neigh_vals if v > 0) / len(neigh_vals)) if neigh_vals else None
    # 粗糙度：所有"仅一维相差1层"的相邻对，绩效差绝对值的均值
    key_set = set(keys)
    diffs = []
    for k in keys:
        for d in range(ndim):
            up = list(k)
            up[d] += 1
            tup = tuple(up)
            if tup in key_set:
                diffs.append(abs(perf_map[tup] - perf_map[k]))
    roughness = (statistics.fmean(diffs) / denom) if diffs else 0.0
    verdict = "数据不足"
    if best_v <= 0.0:
        # 最优点本身不盈利时，"高原/孤峰"比值无意义（负负得正会给出>1的假稳健）
        verdict = "全网格绩效为负，参数稳健性无从谈起，应先检查信号方向/成本而非挑参数"
    elif plateau_ratio is not None:
        if plateau_ratio >= 0.8 and (neigh_positive or 0) >= 0.6 and roughness <= 0.5:
            verdict = "高原（邻域同样有效，参数稳健）"
        elif plateau_ratio <= 0.3 or roughness >= 1.0:
            verdict = "孤峰（最优点四周骤降，过拟合风险高，勿重仓单点参数）"
        else:
            verdict = "过渡（邻域部分有效，建议取参数区间而非单点）"
    return {"best_key": best, "best_perf": best_v, "neighbor_mean": neigh_mean,
            "plateau_ratio": plateau_ratio, "neighbor_positive_rate": neigh_positive,
            "roughness": roughness, "n_neighbors": len(neighbors), "verdict": verdict}


# ============================================================
# 真实数据适配
# ============================================================
def daily_returns_from_equity_csv(path):
    """从 portfolio_equity.csv 的逐 bar equity 按交易日聚合成日收益率序列。"""
    day_equity = {}
    order = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            day = row["dt"][:10]
            eq = float(row["equity"])
            if day not in day_equity:
                order.append(day)
            day_equity[day] = eq  # 当日最后一根 bar 的权益
    days = sorted(day_equity)
    rets, prev = [], None
    for day in days:
        eq = day_equity[day]
        if prev is not None and prev > 0:
            rets.append(eq / prev - 1.0)
        prev = eq
    return days, rets


def build_param_grid_matrix(sym, period=30, lookback=0, aggregate_from=0):
    """
    复用 intraday_backtest 的同一套信号与撮合，对 config 的稳定性参数网格逐组合回放，
    按"平仓交易日"把每笔净收益复利聚合成日收益，对齐成 T×N 矩阵（无交易日补0，口径在报告注明）。
    返回 (day_list, combo_name_list, matrix, combo_layer_map, perf_by_combo)。
    """
    import config
    import storage
    import intraday_backtest as ib
    from backtest import load_fee_schedule, ratio_adjusted_bars

    db = storage.MonitorDB()
    try:
        items = ib.resolve_items(sym)
        if not items:
            raise ValueError("无法解析品种 %r" % sym)
        sym_code, code, name = items[0]
        raw, src = ib.load_minute_bars(db, sym_code, period, lookback, aggregate_from)
    finally:
        db.close()
    bars, roll = ratio_adjusted_bars(raw)
    prepared = ib.prepare_series(bars, config.INTRADAY_BT_SIG_WINDOW)
    owners, bases = ib.build_owner_meta(bars)
    fee_table = load_fee_schedule(config.FUTURES_FEES_FILE)
    fee_row = fee_table.get(sym_code)
    move = config.FUTURES_LIMIT_MOVE.get(sym_code, config.INTRADAY_BT_LIMIT_MOVE)

    entries = list(config.INTRADAY_BT_STABLE_ENTRIES)
    stops = list(config.INTRADAY_BT_STABLE_STOPS)
    targets = list(config.INTRADAY_BT_STABLE_TARGETS)
    combos = list(itertools.product(entries, stops, targets))
    # 层号映射（把浮点参数值映射为排序层号，供 parameter_plateau 用）
    e_layer = {v: i for i, v in enumerate(sorted(entries))}
    s_layer = {v: i for i, v in enumerate(sorted(stops))}
    t_layer = {v: i for i, v in enumerate(sorted(targets))}

    per_combo_days = []
    names = []
    layer_map = {}
    total_perf = {}
    all_days = set()
    for (e, s, tval) in combos:
        trades, _, _ = ib.simulate(
            sym_code, bars, prepared, owners, bases, e, s, tval,
            config.INTRADAY_BT_FLAT_EOD, config.INTRADAY_BT_MAX_BARS,
            config.INTRADAY_BT_SLIP_RATE, fee_row, True, config.INTRADAY_BT_FEE_RATE,
            True, move, config.INTRADAY_BT_LIMIT_TICK_EPS)
        day_comp = {}
        for tr in trades:
            day = tr["exit_dt"][:10]
            day_comp[day] = (1.0 + day_comp.get(day, 0.0)) * (1.0 + tr["net"]) - 1.0
            all_days.add(day)
        per_combo_days.append(day_comp)
        name = "e%g/s%g/t%g" % (e, s, tval)
        names.append(name)
        layer_map[name] = (e_layer[e], s_layer[s], t_layer[tval])
        nets = [tr["net"] for tr in trades]
        total_perf[name] = per_period_sharpe(nets) if len(nets) >= 2 else 0.0

    days = sorted(all_days)
    matrix = []
    for day in days:
        matrix.append([pc.get(day, 0.0) for pc in per_combo_days])
    return {"sym": sym_code, "name": name, "days": days, "names": names,
            "matrix": matrix, "layer_map": layer_map, "total_perf": total_perf,
            "bars": len(bars), "src": src}


# ============================================================
# 报告渲染
# ============================================================
def _pct(x, d=1):
    return "--" if x is None else ("%.*f%%" % (d, 100.0 * x))


def render_dsr(title, d):
    out = ["【%s】" % title]
    out.append("  收益期数 T=%d；逐期 Sharpe=%.3f（非年化）；偏度 %.3f、Pearson峰度 %.3f（正态=3）"
               % (d["t"], d["sr_per_period"], d["skew"], d["kurt"]))
    out.append("  多重试验次数 N=%d → 零优势下蒙到的期望最大 Sharpe 阈值 SR0=%.3f"
               % (d["n_trials"], d["sr0_multiple_trial"]))
    out.append("  PSR(真实Sharpe>0)=%.3f；DSR(跑赢SR0多重试验阈值)=%.3f"
               % (d["psr_vs_zero"], d["dsr"]))
    if d["dsr"] >= 0.95:
        verdict = "DSR≥0.95：经多重试验与非正态校正后仍显著，结果较可信"
    elif d["dsr"] >= 0.8:
        verdict = "0.8≤DSR<0.95：边际，建议增加样本或减少试验次数后复核"
    else:
        verdict = "DSR<0.8：当前优势无法排除'试得多蒙到的'，不应当作稳健结论"
    out.append("  判定：" + verdict)
    return out


def render_grid(g, n_blocks, wf_train, wf_test):
    out = []
    out.append("=" * 96)
    out.append("品种 %s（%s）参数网格样本外验证；分钟bar %d 根、平仓交易日 %d 天、候选参数组合 %d 个；网格日收益无交易日补0"
               % (g["sym"], g["name"], g["bars"], len(g["days"]), len(g["names"])))
    # CSCV
    c = cscv_pbo(g["matrix"], n_blocks=n_blocks)
    out.append("一、CSCV 过拟合概率 PBO（S=%d 对半分、评估 %d 种对称划分）" % (c["n_blocks"], c["combos"]))
    out.append("  PBO=%.3f（样本内最优参数在样本外落入下半区的比例）；logit相对秩中位数=%.3f"
               % (c["pbo"], c["median_logit"]))
    if c["pbo"] < 0.2:
        v = "PBO<0.2：选优过程泛化良好"
    elif c["pbo"] < 0.5:
        v = "0.2≤PBO<0.5：选优略优于抛硬币，存在一定过拟合"
    elif c["pbo"] < 0.8:
        v = "0.5≤PBO<0.8：选优经常在样本外失效，过拟合明显"
    else:
        v = "PBO≥0.8：样本内最优几乎必然样本外平庸，参数搜索基本在拟合噪声"
    out.append("  判定：" + v)
    # walk-forward
    wf = walk_forward(g["matrix"], wf_train, wf_test)
    out.append("二、Walk-forward 滚动选参（IS窗%d天→OOS窗%d天，共%d段）"
               % (wf_train, wf_test, wf.get("n_segments", 0)))
    if wf.get("n_segments"):
        decay_txt = "IS≈0衰减不适用" if wf["is_oos_decay"] is None else ("%.1f%%" % (100.0 * wf["is_oos_decay"]))
        out.append("  IS选中参数Sharpe均值=%.3f，其样本外Sharpe=%.3f；同期事后最优=%.3f；IS→OOS衰减=%s"
                   % (wf["mean_is_sharpe"], wf["mean_oos_sharpe"], wf["mean_oos_best"], decay_txt))
        out.append("  OOS跑赢候选中位数比例=%s；相邻段最优参数切换率=%.1f%%（越低越稳定）"
                   % (_pct(wf["oos_beat_median_rate"]), 100.0 * wf["param_switch_rate"]))
    else:
        out.append("  交易日不足以完成一段完整 IS+OOS，跳过（样本积累后再跑）")
    # 参数高原
    perf_map = {g["layer_map"][nm]: g["total_perf"][nm] for nm in g["names"]}
    pl = parameter_plateau(perf_map)
    bk = pl["best_key"]
    best_name = next(nm for nm in g["names"] if g["layer_map"][nm] == bk)
    out.append("三、参数高原/孤峰（全样本逐笔Sharpe为绩效）")
    out.append("  最优点=%s 绩效=%.3f；邻域均值=%s、plateau_ratio=%s、邻域正收益占比=%s、粗糙度=%.3f"
               % (best_name, pl["best_perf"],
                  "--" if pl["neighbor_mean"] is None else "%.3f" % pl["neighbor_mean"],
                  "--" if pl["plateau_ratio"] is None else "%.3f" % pl["plateau_ratio"],
                  _pct(pl["neighbor_positive_rate"]), pl["roughness"]))
    out.append("  判定：" + pl["verdict"])
    return out, c, wf, pl, pl["best_perf"]


def build_report(args):
    sidecar = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "dsr": None, "grid": None, "summaries": []}
    lines = []
    lines.append("回测样本外验证与防过拟合报告（tools/backtest_validation.py）")
    lines.append("方法：Deflated Sharpe（多重试验/非正态校正）、CSCV-PBO、PurgedKFold、Walk-forward、参数高原")
    lines.append("口径：纯标准库离线计算，只评估不改动任何生产参数；Sharpe 均为同频率非年化值")
    lines.append("")

    # DSR
    if args.dsr_equity and os.path.exists(args.dsr_equity):
        days, rets = daily_returns_from_equity_csv(args.dsr_equity)
        if len(rets) >= 5:
            d = deflated_sharpe(rets, args.trials)
            lines += render_dsr("组合账户日收益 DSR（来源 %s，%d 个交易日）"
                                % (os.path.basename(args.dsr_equity), len(days)), d)
            if d["dsr"] >= 0.95:
                dsr_verdict = "经多重试验校正后仍显著"
            elif d["dsr"] >= 0.8:
                dsr_verdict = "边际，需增样本/减试验复核"
            else:
                dsr_verdict = "无法排除多重试验偶然性"
            sidecar["dsr"] = {"n_days": len(days), "sr_obs": d["sr_per_period"],
                              "sr0": d["sr0_multiple_trial"], "psr_zero": d["psr_vs_zero"],
                              "dsr": d["dsr"], "n_trials": d["n_trials"],
                              "verdict": dsr_verdict}
            lines.append("")
        else:
            lines.append("权益序列交易日不足（%d），DSR 跳过" % len(rets))
            lines.append("")

    # 参数网格
    codes = []
    if args.all_grid:
        import config
        import storage
        import intraday_backtest as ib
        db = storage.MonitorDB()
        try:
            codes = [it[0] for it in ib.resolve_items("", limit=args.limit or 0)]
        finally:
            db.close()
    elif args.grid:
        codes = [x.strip() for x in args.grid.split(",") if x.strip()]
    summaries = []
    for sym in codes:
        try:
            g = build_param_grid_matrix(sym, period=args.period)
            if len(g["days"]) < max(args.wf_train + args.wf_test, args.n_blocks) + 1:
                lines.append("=" * 96)
                lines.append("品种 %s：平仓交易日仅 %d 天，不足以做 CSCV/WF，跳过（随分钟库积累再评）"
                             % (sym, len(g["days"])))
                continue
            gl, c, wf, pl, best_perf = render_grid(g, args.n_blocks, args.wf_train, args.wf_test)
            lines += gl
            summaries.append((sym, c["pbo"],
                              wf.get("mean_oos_sharpe"), wf.get("oos_beat_median_rate"),
                              pl["plateau_ratio"], pl["verdict"], best_perf))
            sidecar["summaries"].append({"sym": sym, "pbo": c["pbo"],
                                         "oos_sharpe": wf.get("mean_oos_sharpe"),
                                         "oos_beat_median": wf.get("oos_beat_median_rate"),
                                         "plateau_ratio": pl["plateau_ratio"],
                                         "best_perf": best_perf})
        except Exception as exc:  # 单品种失败不拖垮整份报告
            lines.append("=" * 96)
            lines.append("品种 %s 评估失败：%s" % (sym, exc))
    if summaries:
        lines.append("=" * 96)
        n = len(summaries)
        n_good_pbo = sum(1 for x in summaries if x[1] < 0.2)
        n_loss = sum(1 for x in summaries if x[6] <= 0.0)
        n_oos_pos = sum(1 for x in summaries if (x[2] or 0.0) > 0.0)
        lines.append("全市场结论（共%d个品种；分钟窗口约6个月、样本偏短，结论随积累更新）：" % n)
        lines.append("  · PBO<0.2（选优泛化良好）%d个；全网格全样本Sharpe为负 %d个；Walk-forward样本外Sharpe为正 %d个"
                     % (n_good_pbo, n_loss, n_oos_pos))
        lines.append("  · 多数品种全网格微亏/IS→OOS明显衰减，说明当前样本长度尚不足以支撑'挑最优参数/上ML'，应继续积累而非重仓单点参数。")
        lines.append("汇总（PBO 越低越好；OOS beat 中位数比例越高越好；plateau 越接近1越稳健，全网格亏损时不评估）")
        lines.append("  品种    PBO     OOS_Sharpe  OOS跑赢中位率  plateau   判定")
        for sym, pbo, oos, beat, pr, verdict, best_perf in summaries:
            short = "全网格亏损" if best_perf <= 0.0 else verdict.split("（")[0]
            pr_txt = "--" if (best_perf <= 0.0 or pr is None) else "%.3f" % pr
            lines.append("  %-6s  %.3f   %-10s  %-12s  %-8s  %s"
                         % (sym, pbo,
                            "--" if oos is None else "%.3f" % oos,
                            _pct(beat), pr_txt, short))
        sidecar["grid"] = {"n": n, "pbo_good": n_good_pbo, "all_loss": n_loss,
                           "oos_pos": n_oos_pos}
    lines.append("")
    lines.append("说明：PBO/DSR 是对'选优动作'的统计校正，不是收益预测；样本越长、候选越少结论越可靠。")
    lines.append("PurgedKFold 为 WP-F4 训练 ml_samples 时的强制切分器（见 --selftest 断言），禁止随机K折。")
    return "\n".join(lines) + "\n", sidecar


# ============================================================
# 零网络合成断言
# ============================================================
def _selftest():
    import random
    rng = random.Random(20260902)

    # 1) 正态函数互逆与已知分位点
    assert abs(norm_cdf(0.0) - 0.5) < 1e-12
    assert abs(norm_ppf(0.975) - 1.9599639845) < 1e-6
    for p in (0.01, 0.25, 0.5, 0.75, 0.99):
        assert abs(norm_cdf(norm_ppf(p)) - p) < 1e-9
    # 对称样本矩
    sym = [rng.gauss(0, 1) for _ in range(20000)]
    _, _, g3, g4, _ = moments(sym)
    assert abs(g3) < 0.06 and abs(g4 - 3.0) < 0.15, (g3, g4)

    # 2) PSR：零均值大样本≈0.5；强正均值→接近1
    zero_seq = [rng.gauss(0, 1) for _ in range(3000)]
    psr_z = prob_sharpe_ratio(per_period_sharpe(zero_seq), 0.0, 3000,
                              moments(zero_seq)[2], moments(zero_seq)[3])
    assert 0.3 < psr_z < 0.7, psr_z
    good = [rng.gauss(0.08, 1.0) for _ in range(3000)]
    psr_g = prob_sharpe_ratio(per_period_sharpe(good), 0.0, 3000,
                              moments(good)[2], moments(good)[3])
    assert psr_g > 0.99, psr_g

    # 3) DSR：试验次数越多，SR0 阈值越高、DSR 越低（多重试验惩罚单调）
    sr0_1 = expected_max_sharpe(1, 1.0 / math.sqrt(1000))
    sr0_100 = expected_max_sharpe(100, 1.0 / math.sqrt(1000))
    sr0_10000 = expected_max_sharpe(10000, 1.0 / math.sqrt(1000))
    assert sr0_1 == 0.0 and sr0_100 < sr0_10000, (sr0_1, sr0_100, sr0_10000)
    d1 = deflated_sharpe(good, 1)
    dmany = deflated_sharpe(good, 5000)
    assert d1["dsr"] >= dmany["dsr"] and d1["sr0_multiple_trial"] == 0.0

    # 4) CSCV：纯噪声→PBO 接近 0.5；一列稳定真alpha→PBO≈0
    T, N = 240, 20
    noise = [[rng.gauss(0, 1) for _ in range(N)] for _ in range(T)]
    pbo_noise = cscv_pbo(noise, n_blocks=10)["pbo"]
    assert 0.2 < pbo_noise < 0.8, pbo_noise
    alpha = [[rng.gauss(0, 1) + (0.35 if j == 0 else 0.0) for j in range(N)] for _ in range(T)]
    pbo_alpha = cscv_pbo(alpha, n_blocks=10)["pbo"]
    assert pbo_alpha < 0.1, pbo_alpha

    # 5) PurgedKFold：跨折标签被 purge、embargo 生效、test 不进 train、无遗漏
    n = 100
    t1 = [min(n - 1, i + 8) for i in range(n)]  # 每样本标签向前看8个下标
    folds = list(purged_kfold_splits(n, t1, n_splits=5, embargo=3))
    assert len(folds) == 5
    for train, test in folds:
        ts = set(test)
        trs = set(train)
        assert not (ts & trs)  # 测试不进训练
        a, b = test[0], test[-1] + 1
        for i in train:
            assert t1[i] <= a or i >= b  # purge：标签窗口不跨测试区
            assert not (a - 3 <= i < a) and not (b <= i < b + 3)  # embargo
        # 除 test/train 外的 missing 样本，必须确实命中 purge（标签窗口跨测试区）或 embargo 带
        covered = ts | trs
        for i in range(n):
            if i in covered:
                continue
            purged = not (t1[i] <= a or i >= b)
            embargoed = (a - 3 <= i < a) or (b <= i < b + 3)
            assert purged or embargoed, (i, a, b)

    # 6) Walk-forward：一列持续占优→每段都选它、OOS 几乎总跑赢中位数、切换率0
    wf_mat = [[rng.gauss(0, 1) + (0.6 if j == 2 else 0.0) for j in range(8)] for _ in range(200)]
    wf = walk_forward(wf_mat, 40, 20)
    assert wf["n_segments"] >= 7
    assert all(seg["chosen"] == 2 for seg in wf["segments"])
    assert wf["param_switch_rate"] == 0.0
    assert wf["oos_beat_median_rate"] > 0.9
    # 第52轮：purge/embargo 默认0与旧版一致；加隔离带后持续占优列仍被选中、段数不增、字段回传
    wf_iso = walk_forward(wf_mat, 40, 20, purge=5, embargo=3)
    assert wf_iso["purge"] == 5 and wf_iso["embargo"] == 3
    assert wf_iso["n_segments"] <= wf["n_segments"]
    assert all(seg["chosen"] == 2 for seg in wf_iso["segments"])
    assert walk_forward(wf_mat, 6, 20, purge=5)["n_segments"] == 0   # IS-purge<2 安全返空

    # 7) 参数高原 vs 孤峰：构造规整 3x3，中心孤峰
    grid_peak = {(x, y): (1.0 if (x, y) == (1, 1) else 0.05)
                 for x in range(3) for y in range(3)}
    pl_peak = parameter_plateau(grid_peak)
    assert pl_peak["best_key"] == (1, 1) and pl_peak["plateau_ratio"] < 0.2
    grid_flat = {(x, y): (1.0 if (x, y) == (1, 1) else 0.95)
                 for x in range(3) for y in range(3)}
    pl_flat = parameter_plateau(grid_flat)
    assert pl_flat["plateau_ratio"] > 0.9 and pl_flat["roughness"] < 0.2

    print("backtest_validation selftest ALL PASS"
          "（正态CDF/PPF、矩、PSR零/强、DSR多重试验单调、CSCV噪声≈0.5/真alpha≈0、"
          "PurgedKFold purge+embargo、Walk-forward持续占优、高原vs孤峰）")


def main(argv=None):
    p = argparse.ArgumentParser(description="回测样本外验证与防过拟合工具箱")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--dsr-equity", default=os.path.join("reports", "portfolio_equity.csv"))
    p.add_argument("--no-dsr", action="store_true")
    p.add_argument("--trials", type=int, default=18, help="多重试验次数（参数网格×选择次数），默认18")
    p.add_argument("--grid", default="", help="逗号分隔品种做参数网格 CSCV/WF/高原，如 RB,HC")
    p.add_argument("--all-grid", action="store_true")
    p.add_argument("--period", type=int, default=30, choices=(1, 5, 15, 30, 60))
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--n-blocks", type=int, default=10)
    p.add_argument("--wf-train", type=int, default=20, help="walk-forward IS 窗（交易日）")
    p.add_argument("--wf-test", type=int, default=10, help="walk-forward OOS 窗（交易日）")
    p.add_argument("--out", default=os.path.join("reports", "backtest_validation.txt"))
    args = p.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    if args.no_dsr:
        args.dsr_equity = ""
    if not args.grid and not args.all_grid and not args.dsr_equity:
        p.error("请至少指定 --dsr-equity / --grid / --all-grid 之一，或先 --selftest")
    text, sidecar = build_report(args)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8-sig") as f:
        f.write(text)
    json_out = os.path.splitext(args.out)[0] + ".json"
    with open(json_out, "w", encoding="utf-8") as jf:
        json.dump(sidecar, jf, ensure_ascii=False, indent=2, allow_nan=False)
    print(text)
    print("已写出", args.out)
    print("结构化 sidecar 已写出", json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
