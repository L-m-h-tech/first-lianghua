# -*- coding: utf-8 -*-
r"""G7（第30轮）多窗口时序动量 TSMOM(63/126/252) 离线时序 IC 评估（研究侧工具，不进常驻链路、不改任何线上权重）。

回答的问题（融合总纲 G7 的"先影子评估、确定不更差才并入"）：
  analyzer 现网日线动量只有 ret5/ret20 两个短窗。补 1/3/6 个月（63/126/252 交易日）多窗口
  时序动量之前，先用全市场真实日 K 离线回答——
    1) 各窗口（含波动调整 z 与等权合成）对未来 5/20/60 日收益有没有稳定预测力（IC/RankIC/ICIR）？
    2) 预测力随持有期限怎么衰减？
    3) 63/126/252 三窗口彼此冗余度多高（互相关）？
    4) 相对现有 ret5/ret20 短窗，长窗有没有"边际增量"（剔除短窗后的残差 IC）？
    5) 等权合成 vs 用样本内 IC 加权合成，样本外（后 30%）是否仍为正（防自欺）？
  只有这些证据整体为正、样本外不塌，后续轮次才允许把 TSMOM 并入"日线动量"；本工具只给证据与
  建议，绝不自动改 analyzer 任何权重（与 factor_eval 同一纪律）。

数据与口径（零新增依赖、纯标准库；日 K 走与 backtest 完全相同的装载/比例复权链路）：
  - backtest.resolve_codes 取全品种主连 -> futures_data.fetch_daily_kline ->
    backtest.ratio_adjusted_bars（主连换月跳空收益置 0 的比例后复权，避免换月假趋势）；
  - 因子：futures_data.tsmom_series 逐时点产出 ret/z/blend（实时侧与本工具同一函数，杜绝两套口径）；
    ret{L}=过去 L 日累计收益；z{L}=ret{L}/(过去 L 日日收益样本std×√252)，跨品种可比的波动调整趋势；
    blend=mean(tanh(clip(z{L},±3)))；另造 ret5/ret20 作为"现有短窗基线"对照；
  - 目标 y=未来 H 日收益 close[t+H]/close[t]-1（H=5/20/60）；暖机 t>=252 且 t+H<n 才入样，无未来函数；
  - 主指标 RankIC=Spearman（对异常值稳健，主看它），IC=Pearson 参考；ICIR=月度 RankIC 的 mean/std；
    分 5 档看单调性与多空价差；方向命中率=sign(因子)==sign(未来收益) 的比例。

输出：reports/tsmom_eval.txt（人类可读）+ reports/tsmom_eval.json（结构化 sidecar，供后续看板）。
用法（项目根目录）：
  D:\Python\python.exe tools\tsmom_eval.py                # 全品种、未来5/20/60日，写报告
  D:\Python\python.exe tools\tsmom_eval.py --codes RB0,MA0 --limit 8
  D:\Python\python.exe tools\tsmom_eval.py --selftest     # 零网络合成断言
"""
import argparse
import math
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import config  # noqa: E402
import futures_data  # noqa: E402
import factor_eval as fe  # noqa: E402  复用 pearson/spearman/分档/月度IC/ICIR
import backtest  # noqa: E402  复用 resolve_codes/ratio_adjusted_bars
import panel_builder as pb  # noqa: E402  G21续：--panel 读已复权面板

# 因子键 -> (中文名, 是否本次新增的长窗核心因子)
FACTORS = [
    ("ret5", "现有短窗ret5", False),
    ("ret20", "现有短窗ret20", False),
    ("ret63", "长窗收益63", True),
    ("ret126", "长窗收益126", True),
    ("ret252", "长窗收益252", True),
    ("z63", "波动调整TSMOM63", True),
    ("z126", "波动调整TSMOM126", True),
    ("z252", "波动调整TSMOM252", True),
    ("blend", "等权合成blend", True),
]
FACTOR_KEYS = [k for k, _, _ in FACTORS]
CORE_Z = ["z63", "z126", "z252"]
CROSS_KEYS = ["ret5", "ret20", "z63", "z126", "z252", "blend"]   # 互相关矩阵
SHORT_BASE = ["ret5", "ret20"]                                   # 边际增量要剔除的现有短窗


# =========================== 纯统计/面板构造（可合成断言） ===========================
def ols_residual(target, explan):
    """target 对 explan（list[list]，每列一个解释变量，内部自动加常数项）OLS 回归后的残差。

    用正规方程 + 高斯消元求解（维度极低），矩阵奇异时退化为"减均值"，绝不抛异常。
    残差 = target 中无法被 explan 线性解释的部分，用于"边际增量/偏相关"评估。
    """
    n = len(target)
    k = len(explan)
    if n < k + 2 or k == 0:
        mu = sum(target) / n if n else 0.0
        return [v - mu for v in target]
    X = [[1.0] + [float(explan[j][i]) for j in range(k)] for i in range(n)]
    p = k + 1
    XtX = [[sum(X[i][a] * X[i][b] for i in range(n)) for b in range(p)] for a in range(p)]
    Xty = [sum(X[i][a] * target[i] for i in range(n)) for a in range(p)]
    beta = _solve(XtX, Xty)
    if beta is None:   # 奇异：退化为减均值
        mu = sum(target) / n
        return [v - mu for v in target]
    return [target[i] - sum(beta[a] * X[i][a] for a in range(p)) for i in range(n)]


def _solve(A, b):
    """高斯消元解 Ax=b（A 为方阵）；奇异返回 None。"""
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            return None
        M[col], M[piv] = M[piv], M[col]
        for r in range(n):
            if r == col:
                continue
            factor = M[r][col] / M[col][col]
            for c in range(col, n + 1):
                M[r][c] -= factor * M[col][c]
    return [M[i][n] / M[i][i] for i in range(n)]


def forward_returns(closes, horizons):
    """{H: [与closes等长，t 时点买入持有 H 日的简单收益，尾部不足为 None]}（无未来函数泄漏）。"""
    n = len(closes)
    out = {H: [None] * n for H in horizons}
    for t in range(n):
        for H in horizons:
            j = t + H
            if j < n and closes[t] > 0 and closes[j] > 0:
                out[H][t] = closes[j] / closes[t] - 1.0
    return out


def records_from_adjusted(name, bars, lookbacks, horizons):
    """已比例复权 bar -> 逐时点因子+未来收益记录（纯函数、不联网、不再复权）。"""
    if len(bars) < max(lookbacks) + max(horizons) + 5:
        return []
    closes = [futures_data._f(b["c"]) for b in bars]
    dates = [str(b.get("d", "")) for b in bars]
    ts = futures_data.tsmom_series(closes, lookbacks=tuple(lookbacks))
    fwd = forward_returns(closes, tuple(horizons))
    recs = []
    warm = max(lookbacks)
    for t in range(warm, len(closes)):
        rec = {"sym": name, "date": dates[t]}
        ok = True
        for L in lookbacks:
            rec["ret%d" % L] = ts["ret%d" % L][t]
            rec["z%d" % L] = ts["tsmom%d" % L][t]
        rec["blend"] = ts["blend"][t]
        # 现有短窗基线（与 futures_data 同口径）
        rec["ret5"] = futures_data._lookback_return(closes, t, 5)
        rec["ret20"] = futures_data._lookback_return(closes, t, 20)
        for H in horizons:
            rec["fwd%d" % H] = fwd[H][t]
            if rec["fwd%d" % H] is None:
                ok = False
        # 长窗原始收益至少最长窗可得（z 允许因零波动缺失，故不强制 z）
        if rec.get("ret%d" % max(lookbacks)) is None:
            ok = False
        if ok:
            recs.append(rec)
    return recs


def build_symbol_records(name, raw_bars, lookbacks, horizons):
    """网络旧路径：比例复权 -> records_from_adjusted（历史逐值一致）。"""
    bars, _roll = backtest.ratio_adjusted_bars(raw_bars)
    return records_from_adjusted(name, bars, lookbacks, horizons)


def xy(records, factor, horizon):
    """从记录集取某因子×某地平线的 (xs, ys, months)，自动滤 None/非有限值。"""
    xs, ys, ms = [], [], []
    fk, yk = factor, "fwd%d" % horizon
    for r in records:
        x, y = r.get(fk), r.get(yk)
        if x is None or y is None:
            continue
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        xs.append(float(x))
        ys.append(float(y))
        ms.append((r.get("date") or "")[:7])
    return xs, ys, ms


def eval_factor_horizon(records, factor, horizon, n_q):
    """单因子×单地平线全套指标。"""
    xs, ys, months = xy(records, factor, horizon)
    n = len(xs)
    if n < 2:
        return {"n": n, "ic": 0.0, "rank_ic": 0.0, "icir": 0.0, "n_months": 0,
                "mono": 0.0, "spread": 0.0, "hit": 0.0, "monthly_ic": [], "buckets": []}
    ic = fe.pearson(xs, ys)
    ric = fe.spearman(xs, ys)
    by_month = defaultdict(list)
    for x, y, m in zip(xs, ys, months):
        by_month[m].append((x, y))
    mic = fe.monthly_ic(by_month)
    buckets = fe.quantile_buckets(list(zip(xs, ys)), n_q)
    mono, spread = fe.monotonic_score(buckets)
    hit = sum(1 for x, y in zip(xs, ys) if (x > 0) == (y > 0) and x != 0) / n
    return {"n": n, "ic": ic, "rank_ic": ric, "icir": fe.icir(mic),
            "n_months": len(mic), "mono": mono, "spread": spread, "hit": hit,
            "monthly_ic": mic, "buckets": buckets}


def cross_correlation(records, keys):
    """因子两两 Spearman 互相关矩阵（pooled，忽略任一缺失的时点）。"""
    mat = {}
    for a in keys:
        for b in keys:
            xa, xb = [], []
            for r in records:
                va, vb = r.get(a), r.get(b)
                if va is None or vb is None:
                    continue
                if math.isfinite(va) and math.isfinite(vb):
                    xa.append(va)
                    xb.append(vb)
            mat[(a, b)] = fe.spearman(xa, xb) if len(xa) >= 2 else 0.0
    return mat


def incremental_residual_ic(records, target, controls, horizon):
    """target 剔除 controls 线性信息后的残差，对未来收益的 RankIC（边际增量）。"""
    xs_t, ys, _ = xy(records, target, horizon)
    if len(xs_t) < 5:
        return None, 0
    idx = []
    cols = {c: [] for c in controls}
    for i, r in enumerate(records):
        vals = [r.get(c) for c in controls]
        yv = r.get("fwd%d" % horizon)
        tv = r.get(target)
        if tv is None or yv is None or any(v is None for v in vals):
            continue
        if not (math.isfinite(tv) and math.isfinite(yv) and all(math.isfinite(v) for v in vals)):
            continue
        idx.append(i)
        for c, v in zip(controls, vals):
            cols[c].append(float(v))
    tvec = [float(records[i][target]) for i in idx]
    yvec = [float(records[i]["fwd%d" % horizon]) for i in idx]
    resid = ols_residual(tvec, [cols[c] for c in controls])
    return fe.spearman(resid, yvec), len(tvec)


def split_is_oos(records, oos_ratio):
    """按日期升序切前(1-r)=IS、后 r=OOS（跨品种按同一日历顺序切，防用未来估权重）。"""
    ordered = sorted(records, key=lambda r: (r.get("date") or ""))
    cut = int(len(ordered) * (1.0 - oos_ratio))
    return ordered[:cut], ordered[cut:]


def per_symbol_stats(records, factor, horizon, min_pts=20):
    """分品种各算 RankIC 后的横截面一致性：参与品种数、RankIC 为正比例、中位 RankIC。

    pooled 正相关可能只由少数品种拉动；若大多数品种同向为正，结论才不是板块偏置。
    """
    by = defaultdict(list)
    for r in records:
        x, yv = r.get(factor), r.get("fwd%d" % horizon)
        if x is None or yv is None or not (math.isfinite(x) and math.isfinite(yv)):
            continue
        by[r["sym"]].append((x, yv))
    ics = [fe.spearman([p[0] for p in pts], [p[1] for p in pts])
           for pts in by.values() if len(pts) >= min_pts]
    if not ics:
        return {"n": 0, "pos_ratio": 0.0, "median": 0.0}
    srt = sorted(ics)
    return {"n": len(srt),
            "pos_ratio": sum(1 for v in srt if v > 0) / len(srt),
            "median": srt[len(srt) // 2]}


def ic_weighted_blend(records, zkeys, horizon, weights):
    """给定 {zkey: 权重}，构造 IC 加权合成 Σw·tanh(z)，返回与未来收益对齐的 (x,y)。"""
    xs, ys = [], []
    for r in records:
        zs = [r.get(k) for k in zkeys]
        yv = r.get("fwd%d" % horizon)
        if yv is None or any(z is None for z in zs):
            continue
        x = sum(weights[k] * math.tanh(max(-3.0, min(3.0, z))) for k, z in zip(zkeys, zs))
        xs.append(x)
        ys.append(yv)
    return xs, ys


# =========================== 报告 ===========================
def _now():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _gate_ok(m, resid_ic, oos_ric, gate, psym=None):
    """并入判据（全部满足才算影子证据支持）；返回 (bool, [不满足原因])。

    TSMOM 是"品种内时序"策略，故除 pooled 指标外还要求分品种一致性（过半品种同向、
    中位 RankIC 非负），否则 pooled 的正相关可能只是跨品种价格水平混合出的截面伪相关。
    """
    reasons = []
    if not (m["rank_ic"] >= gate):
        reasons.append("主地平线RankIC=%+.3f未达门槛%.2f" % (m["rank_ic"], gate))
    if not (m["icir"] > 0):
        reasons.append("ICIR=%+.2f非正(月度方向不稳)" % m["icir"])
    if oos_ric is not None and oos_ric < 0:
        reasons.append("样本外RankIC=%+.3f转负" % oos_ric)
    if resid_ic is not None and resid_ic < 0:
        reasons.append("对短窗残差增量IC=%+.3f为负(无独立增量)" % resid_ic)
    if psym is not None and psym["n"] >= 10:
        if psym["pos_ratio"] < 0.5 or psym["median"] < 0:
            reasons.append("品种内不成立：仅%d/%d品种RankIC为正(%.0f%%)、中位%+.3f"
                           % (round(psym["pos_ratio"] * psym["n"]), psym["n"],
                              psym["pos_ratio"] * 100, psym["median"]))
    return (len(reasons) == 0), reasons


def build_report(records, errors, lookbacks, horizons, main_h, n_q,
                 min_sample, gate, oos_ratio, days, workers):
    L = []
    L.append("=" * 104)
    L.append(" G7 多窗口时序动量 TSMOM(%s) 离线时序IC评估  生成于 %s"
             % ("/".join(map(str, lookbacks)), _now()))
    L.append("=" * 104)
    L.append("样本：%d 个品种共 %d 个(品种×交易日) pooled 时点；日K经主连换月跳空置0的比例后复权；"
             "暖机≥%d根、持有到 %d 日，无未来函数。" %
             (len({r["sym"] for r in records}), len(records), max(lookbacks), max(horizons)))
    if errors:
        L.append("取数失败品种 %d 个（不阻断）：%s"
                 % (len(errors), "、".join("%s(%s)" % (n, e[:24]) for n, e in errors[:12])))
    L.append("口径：RankIC=Spearman(因子, 未来H日收益)，主看它；IC=Pearson；ICIR=月度RankIC的mean/std；")
    L.append("      z=窗口收益÷(窗口日收益std×√%d)的波动调整趋势、跨品种可比；blend=等权tanh(z)；纯标准库零新增依赖。"
             % config.TSMOM_ANN)
    L.append("")

    # 表1：因子×地平线 RankIC 衰减总表
    L.append("一、各因子对未来收益的预测力总表（看 5→20→60 日 的衰减/增强；格式 RankIC / ICIR / n）")
    head = " %-16s " % "因子"
    for H in horizons:
        head += "| 未来%2d日                     " % H
    L.append(head)
    L.append(" " + "-" * 100)
    metrics = {}
    for fk, cn, _is_long in FACTORS:
        line = " %-16s " % cn
        for H in horizons:
            m = eval_factor_horizon(records, fk, H, n_q)
            metrics[(fk, H)] = m
            line += "| RIC=%+6.3f ICIR=%+5.2f n=%-5d " % (m["rank_ic"], m["icir"], m["n"])
        L.append(line)
    L.append("")

    # 表2：主地平线逐因子明细
    L.append("二、主地平线（未来%d日）逐因子明细：分档单调性/多空价差/方向命中率" % main_h)
    L.append(" %-16s %6s %8s %8s %7s %8s %7s %7s  各档平均未来收益(%%)/胜率(%%)"
             % ("因子", "n", "IC", "RankIC", "ICIR", "多空价差", "单调", "方向命中"))
    for fk, cn, _ in FACTORS:
        m = metrics[(fk, main_h)]
        if m["n"] == 0:
            continue
        btxt = " | ".join("%.2f/%.0f" % (b[1] * 100, b[2] * 100) for b in m["buckets"])
        L.append(" %-16s %6d %+8.3f %+8.3f %+7.2f %+7.2f%% %6.0f%% %6.0f%%  %s"
                 % (cn, m["n"], m["ic"], m["rank_ic"], m["icir"], m["spread"] * 100,
                    m["mono"] * 100, m["hit"] * 100, btxt))
    L.append("")

    # 表3：互相关
    L.append("三、因子间 Spearman 互相关（看长窗与现有短窗、长窗彼此的冗余度）")
    cmat = cross_correlation(records, CROSS_KEYS)
    L.append(" " + "".join("%10s" % k for k in CROSS_KEYS))
    for a in CROSS_KEYS:
        L.append(" %-8s" % a + "".join("%10.2f" % cmat[(a, b)] for b in CROSS_KEYS))
    L.append("")

    # 表4：边际增量（对短窗残差IC）
    L.append("四、长窗相对现有短窗(ret5/ret20)的边际增量（剔除短窗线性信息后的残差 RankIC，主地平线%d日）" % main_h)
    resid = {}
    for fk, cn, is_long in FACTORS:
        if not is_long:
            continue
        ric, n = incremental_residual_ic(records, fk, SHORT_BASE, main_h)
        resid[fk] = ric
        if ric is None:
            L.append(" %-16s 样本不足" % cn)
        else:
            L.append(" %-16s 残差RankIC=%+.3f（n=%d）%s"
                     % (cn, ric, n, "  ★剔除短窗后仍有正增量" if ric > 0 else "  增量不明显/为负"))
    L.append("")

    # 表5：等权 vs IC加权，IS/OOS
    L.append("五、等权合成 vs 样本内IC加权合成（IS=前%.0f%%估权重，OOS=后%.0f%%验证，主地平线%d日，防自欺）"
             % ((1 - oos_ratio) * 100, oos_ratio * 100, main_h))
    is_rec, oos_rec = split_is_oos(records, oos_ratio)
    weights = {}
    for k in CORE_Z:
        wm = eval_factor_horizon(is_rec, k, main_h, n_q)
        weights[k] = max(0.0, wm["rank_ic"])     # 负 IC 不给权重
    wsum = sum(weights.values())
    if wsum > 1e-9:
        weights = {k: v / wsum for k, v in weights.items()}
    else:
        weights = {k: 1.0 / len(CORE_Z) for k in CORE_Z}
    L.append(" IS 估计的 IC 权重：" + " ".join("%s=%.2f" % (k, weights[k]) for k in CORE_Z))
    for tag, subset in (("IS", is_rec), ("OOS", oos_rec)):
        xe, ye = ic_weighted_blend(subset, CORE_Z, main_h, weights)
        icw = fe.spearman(xe, ye) if len(xe) >= 2 else 0.0
        xb, yb = xy(subset, "blend", main_h)[:2], None
        eqw = fe.spearman(xb[0], xb[1]) if len(xb[0]) >= 2 else 0.0
        L.append(" %-3s n=%-5d 等权blend RankIC=%+6.3f | IC加权合成 RankIC=%+6.3f"
                 % (tag, len(xe), eqw, icw))
    xe_o, _ = ic_weighted_blend(oos_rec, CORE_Z, main_h, weights)
    _, yo = ic_weighted_blend(oos_rec, CORE_Z, main_h, weights)
    oos_icw = fe.spearman(xe_o, yo) if len(xe_o) >= 2 else None
    xb_o = xy(oos_rec, "blend", main_h)
    oos_eq = fe.spearman(xb_o[0], xb_o[1]) if len(xb_o[0]) >= 2 else None
    L.append("")

    # 表6：裁决
    L.append("六、是否满足『确定不更差』的并入判据（主H RankIC≥%.2f、ICIR>0、OOS不转负、对短窗残差增量≥0、"
             "且分品种过半为正中位非负——TSMOM须在品种内成立，防 pooled 截面伪相关）" % gate)
    verdict = {}
    for fk, cn, is_long in FACTORS:
        if not is_long:
            continue
        m = metrics[(fk, main_h)]
        # 各长窗因子的 OOS RankIC
        mo = eval_factor_horizon(oos_rec, fk, main_h, n_q)
        oos_ric = mo["rank_ic"] if mo["n"] >= 2 else None
        psym = per_symbol_stats(records, fk, main_h)
        ok, reasons = _gate_ok(m, resid.get(fk), oos_ric, gate, psym)
        verdict[fk] = {"ok": ok, "main_rank_ic": m["rank_ic"], "icir": m["icir"],
                       "oos_rank_ic": oos_ric, "resid_rank_ic": resid.get(fk),
                       "per_symbol_n": psym["n"], "per_symbol_pos_ratio": psym["pos_ratio"],
                       "per_symbol_median": psym["median"], "reasons": reasons}
        sym_txt = "分品种%d/%d为正(中位RIC=%+.3f)" % (
            round(psym["pos_ratio"] * psym["n"]), psym["n"], psym["median"])
        # reasons 已含品种内明细时不再重复 sym_txt
        tail = "" if any("品种内" in r for r in reasons) else ("；" + sym_txt)
        if m["n"] < min_sample:
            L.append(" %-16s 样本不足(n=%d<%d)，暂不下结论，继续影子积累。%s"
                     % (cn, m["n"], min_sample, sym_txt))
        elif ok:
            L.append(" %-16s ✅ 判据通过：主H RankIC=%+.3f、ICIR=%+.2f、OOS=%s、残差增量=%s；%s，可作为后续并入候选。"
                     % (cn, m["rank_ic"], m["icir"],
                        ("%+.3f" % oos_ric) if oos_ric is not None else "NA",
                        ("%+.3f" % resid[fk]) if resid.get(fk) is not None else "NA", sym_txt))
        else:
            L.append(" %-16s ❌ 暂不并入：%s%s" % (cn, "；".join(reasons), tail))
    L.append("")
    L.append("诚实边界：①主连为比例后复权近似、样本来自近约 %d 根日K（约4年），存在品种/时段与单一行情 regime 偏差；"
             % days)
    L.append("②时序IC对非线性/拐点不敏感，历史规律不代表未来；③本报告只提供研究证据，绝不自动修改 analyzer 权重；")
    L.append("④即便判据通过，并入时仍须遵守『默认影子、缺省等价旧版、可一键回退』，且先只加信息展示再谈改分。")
    sidecar = _sidecar(records, metrics, cmat, resid, weights, verdict,
                       lookbacks, horizons, main_h, days)
    return "\n".join(L) + "\n", sidecar, metrics


def _sidecar(records, metrics, cmat, resid, weights, verdict, lookbacks, horizons, main_h, days):
    facs = []
    for fk, cn, is_long in FACTORS:
        by_h = {}
        for H in horizons:
            m = metrics[(fk, H)]
            by_h[str(H)] = {"n": m["n"], "ic": m["ic"], "rank_ic": m["rank_ic"],
                            "icir": m["icir"], "mono": m["mono"], "spread": m["spread"],
                            "hit": m["hit"]}
        facs.append({"key": fk, "name": cn, "is_long": is_long, "by_h": by_h,
                     "resid_rank_ic": resid.get(fk),
                     "verdict": verdict.get(fk)})
    return {"generated_at": _now(), "days": days, "lookbacks": list(lookbacks),
            "horizons": list(horizons), "main_h": main_h,
            "n_records": len(records),
            "n_symbols": len({r["sym"] for r in records}),
            "ic_weights": weights,
            "cross": {a: {b: cmat[(a, b)] for b in CROSS_KEYS} for a in CROSS_KEYS},
            "factors": facs}


# =========================== 数据抓取与入口 ===========================
def _fetch_one(item, lookbacks, horizons, days, prefer_panel=False, panel_db=None):
    name, code = item
    try:
        if prefer_panel:
            bars, _src = pb.load_adjusted_bars(code, days, prefer_panel=True, db_path=panel_db)
            recs = records_from_adjusted(name, bars, lookbacks, horizons)
        else:
            raw = futures_data.fetch_daily_kline(code)
            raw = raw[-days:]
            recs = build_symbol_records(name, raw, lookbacks, horizons)
        if not recs:
            return name, [], "K线/暖机不足"
        return name, recs, ""
    except Exception as e:  # 单品种失败不阻断全市场评估
        return name, [], "%s: %s" % (type(e).__name__, e)


def collect_records(items, lookbacks, horizons, days, workers, prefer_panel=False, panel_db=None):
    records, errors = [], []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = [pool.submit(_fetch_one, it, lookbacks, horizons, days, prefer_panel, panel_db)
                for it in items]
        for fut in as_completed(futs):
            name, recs, err = fut.result()
            if recs:
                records.extend(recs)
            elif err:
                errors.append((name, err))
    records.sort(key=lambda r: (r.get("date") or "", r.get("sym") or ""))
    return records, errors


def run(argv=None):
    ap = argparse.ArgumentParser(description="G7 多窗口时序动量 TSMOM 离线时序IC评估（研究侧）")
    ap.add_argument("--codes", default="", help="逗号分隔品种/主连，缺省=全品种")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--days", type=int, default=config.TSMOM_EVAL_DAYS)
    ap.add_argument("--lookbacks", default=",".join(map(str, config.TSMOM_LOOKBACKS)))
    ap.add_argument("--horizons", default=",".join(map(str, config.TSMOM_FORECAST_HORIZONS)))
    ap.add_argument("--main-h", type=int, default=20)
    ap.add_argument("--quantiles", type=int, default=config.FACTOR_EVAL_N_QUANTILE)
    ap.add_argument("--min-sample", type=int, default=config.TSMOM_EVAL_MIN_SAMPLE)
    ap.add_argument("--gate", type=float, default=config.TSMOM_EVAL_RIC_GATE)
    ap.add_argument("--oos-ratio", type=float, default=config.TSMOM_EVAL_OOS_RATIO)
    ap.add_argument("--workers", type=int, default=config.TSMOM_EVAL_WORKERS)
    ap.add_argument("--out", default=config.TSMOM_EVAL_FILE)
    ap.add_argument("--panel", action="store_true", help="G21续：优先读已复权研究面板（缺省联网现拉）")
    ap.add_argument("--panel-db", default=None,
                    help="G21续(第88轮)：--panel 时读哪个面板库（缺省 G21 主面板；可指 cache/research_panel_long.db 长面板）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    lookbacks = tuple(int(x) for x in args.lookbacks.split(",") if x.strip())
    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
    main_h = args.main_h if args.main_h in horizons else horizons[len(horizons) // 2]
    items = backtest.resolve_codes(args.codes, args.limit if args.limit > 0 else None)
    records, errors = collect_records(items, lookbacks, horizons, args.days, args.workers,
                                     getattr(args,'panel',False), panel_db=args.panel_db)
    if not records:
        print("无可用样本（全部品种取数失败或暖机不足），错误示例：%s" % errors[:3])
        return 2
    text, sidecar, _ = build_report(records, errors, lookbacks, horizons, main_h,
                                    args.quantiles, args.min_sample, args.gate,
                                    args.oos_ratio, args.days, args.workers)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8-sig") as f:
        f.write(text)
    import json
    with open(config.TSMOM_EVAL_JSON, "w", encoding="utf-8") as f:
        f.write(json.dumps(sidecar, ensure_ascii=False, indent=1))
    print(text)
    print("品种时点 %d、覆盖品种 %d；报告 -> %s；JSON -> %s"
          % (len(records), sidecar["n_symbols"], args.out, config.TSMOM_EVAL_JSON))
    return 0


def selftest():
    """零网络合成断言：趋势持续/反转、OLS残差正交、远期收益无泄漏、IS/OOS、合成与降级。"""
    # 1) forward_returns 手算且尾部为 None（无未来函数）
    closes = [100.0, 101.0, 102.0, 104.0]
    fwd = forward_returns(closes, (1, 2))
    assert abs(fwd[1][0] - 0.01) < 1e-12 and abs(fwd[2][0] - 0.02) < 1e-12
    assert fwd[1][-1] is None and fwd[2][-1] is None and fwd[2][-2] is None

    # 2) 高斯消元解已知线性方程
    beta = _solve([[2.0, 1.0], [1.0, 3.0]], [3.0, 5.0])
    assert abs(beta[0] - 0.8) < 1e-9 and abs(beta[1] - 1.4) < 1e-9, beta
    assert _solve([[1.0, 1.0], [1.0, 1.0]], [1.0, 2.0]) is None  # 奇异->None

    # 3) OLS 残差与解释列、常数列正交（正规方程一阶条件）
    xs1 = [1.0, 2, 3, 4, 5, 6, 7, 8]
    xs2 = [2.0, 1, 4, 3, 6, 5, 8, 7]
    tgt = [3.0, 1, 8, 2, 10, 4, 15, 6]
    resid = ols_residual(tgt, [xs1, xs2])
    assert abs(fe.pearson(resid, xs1)) < 1e-9 and abs(fe.pearson(resid, xs2)) < 1e-9
    assert abs(sum(resid)) < 1e-9  # 与常数正交=残差均值0

    # 4) 构造"动量持续"面板：分段单调趋势，过去长窗涨->未来继续涨 => z252 对 fwd20 RankIC>0
    raw = []
    price = 100.0
    for seg in range(12):           # 12 段交替的持续趋势段
        step = 0.8 if seg % 2 == 0 else -0.8
        for _ in range(40):
            price = max(1.0, price * (1 + step / 100.0))
            raw.append({"d": "202%d-%02d-%02d" % (seg // 4 + 2, seg % 12 + 1, len(raw) % 28 + 1),
                        "o": price, "h": price * 1.001, "l": price * 0.999,
                        "c": price, "v": 1000})
    recs_up = build_symbol_records("TREND", raw, (63, 126, 252), (5, 20, 60))
    assert len(recs_up) > 100, len(recs_up)
    m_up = eval_factor_horizon(recs_up, "z252", 20, 5)
    assert m_up["rank_ic"] > 0.1, m_up["rank_ic"]   # 持续趋势下长窗动量应有正预测力

    # 5) 均值回复面板：价格绕中枢正弦，过去涨多未来回落 => ret20 对短未来 RankIC<0（至少能算出、方向可判）
    raw2 = []
    for i in range(520):
        # 周期25根：ret20 覆盖0.8个周期、fwd5 覆盖0.2个周期，两窗口中心错开半周期(π)，
        # 故"过去20日涨"与"未来5日收益"确定负相关（均值回复），用于反向断言
        p = 100.0 + 10.0 * math.sin(i * 2 * math.pi / 25.0)
        raw2.append({"d": "2026-%02d-%02d" % (i // 28 % 12 + 1, i % 28 + 1),
                     "o": p, "h": p + 0.1, "l": p - 0.1, "c": p, "v": 1})
    recs_rv = build_symbol_records("REV", raw2, (63, 126, 252), (5, 20))
    m_rv = eval_factor_horizon(recs_rv, "ret20", 5, 5)
    assert m_rv["n"] > 50 and m_rv["rank_ic"] < 0, m_rv["rank_ic"]

    # 6) IS/OOS 切分有序且不重叠、覆盖全部
    is_r, oos_r = split_is_oos(recs_up, 0.3)
    assert len(is_r) + len(oos_r) == len(recs_up)
    assert is_r[-1]["date"] <= oos_r[0]["date"] or is_r[-1]["date"] == oos_r[0]["date"]
    assert abs(len(oos_r) / len(recs_up) - 0.3) < 0.02

    # 7) IC加权合成不炸、长度对齐；全正时权重可归一
    xe, ye = ic_weighted_blend(recs_up, ["z63", "z126", "z252"], 20,
                               {"z63": 0.5, "z126": 0.5, "z252": 0.0})
    assert len(xe) == len(ye) and len(xe) > 50

    # 8) 边际增量：target 本身就是 ret5 时，对 (ret5,ret20) 残差≈0，RankIC 应≈0
    ric, n = incremental_residual_ic(recs_up, "ret5", ["ret5", "ret20"], 20)
    assert n > 20 and abs(ric) < 0.05, ric

    # 9) 样本不足安全降级（空记录/极少点不抛异常）
    assert eval_factor_horizon([], "z252", 20, 5)["n"] == 0
    # 分品种一致性：单一持续趋势品种应 1/1 为正、中位 IC>0；空样本安全
    ps = per_symbol_stats(recs_up, "z252", 20)
    assert ps["n"] == 1 and ps["pos_ratio"] == 1.0 and ps["median"] > 0.1, ps
    assert per_symbol_stats([], "z252", 20)["n"] == 0
    assert build_report(recs_up[:8], [], (63, 126, 252), (5, 20), 20, 5,
                        120, 0.02, 0.3, 1023, 1)[0]  # 少量样本仍能出报告文本

    # 9.5) _gate_ok：pooled 达标但分品种多数为负必须否决（防 pooled 截面伪相关）；反之过半为正才通过
    fakem = {"rank_ic": 0.03, "icir": 0.2}
    ok_bad, why = _gate_ok(fakem, 0.01, 0.02, 0.02,
                           {"n": 64, "pos_ratio": 9 / 64, "median": -0.15})
    assert not ok_bad and any("品种内" in w for w in why), (ok_bad, why)
    ok_good, _ = _gate_ok(fakem, 0.01, 0.02, 0.02,
                          {"n": 64, "pos_ratio": 0.6, "median": 0.03})
    assert ok_good

    # 10) build_report 全量跑通且裁决键齐全
    text, sidecar, _ = build_report(recs_up, [("BAD", "x")], (63, 126, 252), (5, 20, 60),
                                    20, 5, 50, 0.02, 0.3, 1023, 1)
    assert "TSMOM" in text and len(sidecar["factors"]) == len(FACTORS)
    print("tsmom_eval selftest ALL PASS（远期收益/OLS正交/趋势正IC·回复负IC/IS-OOS/增量/降级 共10组）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
