# -*- coding: utf-8 -*-
r"""WP-F2（P1-2）B2：因子 IC 评估（研究侧离线工具，不进常驻链路、不自动改任何权重）。

回答的问题：analyzer 九个因子（新闻消息面/原油联动/机构动向/日线动量/技术共振/分钟共振/
盘中动量/量仓资金/基本面）里，谁真的对信号发出后的远期收益有预测力、谁在噪声？

数据与口径（全部来自自有 SQLite，零网络、纯标准库）：
  - storage.factor_outcome_pairs 把 signal_outcomes（30m/2h/1d 三周期已评估结果）与发出时
    signals.parts_json 的因子拆分一一对齐；
  - 主口径=meta-labeling 口径：x = 因子值×信号方向（沿信号方向的因子强度），
    y = 方向收益（已乘信号方向）。IC>0 表示"因子越支持这条信号、信号后续越赚钱"，
    这正是校准/权重最关心的问题（多空样本可混在一起）；
  - 参考口径=原始方向口径：x = 因子值原值，y = 远期收益×信号方向（还原成绝对涨跌方向），
    用来交叉验证因子本身的多空方向预测力。

指标：
  - IC = Pearson(因子, 远期收益)；RankIC = Spearman（对异常值稳健，主看它）；
  - ICIR = 月度 IC 序列 mean/std（跨月稳定性，>0 且月数够才有意义）；
  - 分档单调性：因子沿方向强度排序分 5 档，看各档平均方向收益/胜率是否随档位单调抬升，
    多空价差=最高档-最低档平均收益；
  - walk-forward：逐月滚动，之前所有月为 IS、当月为 OOS，比较 IS/OOS 的 IC 方向一致性；
  - 半衰期衰减：30m→2h→1d 的 RankIC 变化，看因子预测力随时间衰减还是增强。

输出：reports/factor_eval.txt（只给"建议权重区间"，绝不自动修改 analyzer 权重）。
用法（项目根目录）：
  D:\Python\python.exe tools\factor_eval.py                 # 近365天、三周期，写报告
  D:\Python\python.exe tools\factor_eval.py --days 9999     # 全量历史
  D:\Python\python.exe tools\factor_eval.py --selftest      # 零网络合成断言
"""
import argparse
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

HORIZON_LABEL = {30: "30分钟", 120: "2小时", 1440: "次日"}
# 建议权重区间（相对当前权重的乘数，仅建议）：阈值看 |meta RankIC|
SUGGEST = [
    (0.08, 1.10, 1.40, "预测力较强且稳定，可考虑上调权重"),
    (0.03, 0.90, 1.15, "弱有效，建议维持现权重"),
    (-0.03, 0.50, 0.90, "预测力接近噪声，可考虑下调权重"),
    (-math.inf, 0.00, 0.50, "方向疑似相反或长期反向，建议重点复核、暂降权"),
]


# =========================== 纯统计函数（可合成断言） ===========================
def pearson(xs, ys):
    """Pearson 相关系数；样本<2 或方差为 0 返回 0.0。"""
    n = len(xs)
    if n != len(ys) or n < 2:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 1e-15 or syy <= 1e-15:
        return 0.0
    return sxy / math.sqrt(sxx * syy)


def average_ranks(values):
    """平均秩（1 起，并列取平均名次），供 Spearman。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    k = 0
    while k < len(order):
        j = k
        while j + 1 < len(order) and values[order[j + 1]] == values[order[k]]:
            j += 1
        avg = (k + j) / 2.0 + 1.0
        for t in range(k, j + 1):
            ranks[order[t]] = avg
        k = j + 1
    return ranks


def spearman(xs, ys):
    """Spearman 秩相关 = 秩的 Pearson；并列用平均秩。"""
    if len(xs) < 2:
        return 0.0
    return pearson(average_ranks(xs), average_ranks(ys))


def quantile_buckets(pairs, n_q):
    """pairs=[(x,y)...] 按 x 排序分 n_q 档，返回各档 [n, 平均y, y>0占比, 平均x]。"""
    pts = sorted(pairs, key=lambda t: t[0])
    n = len(pts)
    out = []
    for q in range(n_q):
        a = q * n // n_q
        b = (q + 1) * n // n_q
        seg = pts[a:b] if b > a else []
        if not seg:
            out.append([0, 0.0, 0.0, 0.0])
            continue
        ys = [t[1] for t in seg]
        xs = [t[0] for t in seg]
        out.append([len(seg), sum(ys) / len(ys),
                    sum(1 for v in ys if v > 0) / len(ys), sum(xs) / len(xs)])
    return out


def monotonic_score(buckets):
    """平均y随档位单调上升的相邻比例（1=完全单调）；同时返回最高档-最低档平均y。"""
    ys = [b[1] for b in buckets if b[0] > 0]
    if len(ys) < 2:
        return 0.0, 0.0
    inc = sum(1 for a, b in zip(ys, ys[1:]) if b >= a)
    return inc / (len(ys) - 1), ys[-1] - ys[0]


def monthly_ic(pairs_by_month):
    """{month: [(x,y)...]} -> 每月 Spearman 列表 [(month, ic, n)]，按月份升序。"""
    out = []
    for m in sorted(pairs_by_month):
        seg = pairs_by_month[m]
        if len(seg) >= 5:
            ic = spearman([t[0] for t in seg], [t[1] for t in seg])
            out.append((m, ic, len(seg)))
    return out


def icir(ic_series):
    """月度 IC 序列的 ICIR=mean/std（样本标准差）；不足2个月返回0。"""
    vals = [v for _, v, _ in ic_series]
    if len(vals) < 2:
        return 0.0
    mu = sum(vals) / len(vals)
    var = sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)
    sd = math.sqrt(var)
    return mu / sd if sd > 1e-12 else 0.0


def walk_forward(month_pairs, min_train_months=2):
    """逐月滚动：之前所有月合并为 IS、当月为 OOS，返回 (is_ic, oos_ic) 序列与同号率。"""
    months = sorted(month_pairs)
    rows, same = [], 0
    for k in range(len(months)):
        if k < min_train_months:
            continue
        is_pts = [p for m in months[:k] for p in month_pairs[m]]
        oos_pts = month_pairs[months[k]]
        if len(is_pts) < 10 or len(oos_pts) < 5:
            continue
        is_ic = spearman([t[0] for t in is_pts], [t[1] for t in is_pts])
        oos_ic = spearman([t[0] for t in oos_pts], [t[1] for t in oos_pts])
        if is_ic * oos_ic > 0:
            same += 1
        rows.append((months[k], is_ic, oos_ic, len(oos_pts)))
    rate = same / len(rows) if rows else 0.0
    return rows, rate


def suggest_band(rank_ic, mono, n, min_sample):
    """按 meta RankIC + 单调性 + 样本量给建议权重区间（相对当前权重的乘数）与说明。"""
    if n < min_sample:
        return None, None, "样本不足(n=%d<%d)，暂不给建议" % (n, min_sample)
    for th, lo, hi, desc in SUGGEST:
        if rank_ic >= th:
            note = desc
            if th > 0 and mono < 0.5:
                note += "；但分档单调性差(%.0f%%)，需谨慎" % (mono * 100)
            return lo, hi, note


# =========================== 数据装载与评估 ===========================
def _canon(key):
    s = str(key or "").strip()
    for cut in ("(", "（"):
        if cut in s:
            s = s.split(cut, 1)[0].strip()
    return s


def load_samples(db, horizons, days):
    """返回 {horizon: {factor: [{'x_meta','x_raw','y_meta','y_raw','month'}...]}} 与因子清单。"""
    rows = db.factor_outcome_pairs(horizons, days)
    data = {h: defaultdict(list) for h in horizons}
    factors = set()
    for r in rows:
        try:
            import json
            parts = r.get("parts_json")
            parts = json.loads(parts) if isinstance(parts, str) else (parts or {})
            d = int(r["direction_int"])
            ret = float(r["ret"])              # 方向收益（已乘方向）
            h = int(r["horizon_min"])
            month = (r.get("eval_ts") or "")[:7]
        except (TypeError, ValueError):
            continue
        if h not in data or d not in (1, -1):
            continue
        for k, v in parts.items():
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            fac = _canon(k)
            factors.add(fac)
            data[h][fac].append({"x_meta": fv * d, "x_raw": fv,
                                 "y_meta": ret, "y_raw": ret * d, "month": month})
    return data, sorted(factors)


def eval_factor(samples, n_q):
    """对单因子单周期样本算全套指标。samples: [dict(x_meta/y_meta/month...)]。"""
    meta = [(s["x_meta"], s["y_meta"]) for s in samples]
    raw = [(s["x_raw"], s["y_raw"]) for s in samples]
    n = len(meta)
    ic = pearson([t[0] for t in meta], [t[1] for t in meta])
    ric = spearman([t[0] for t in meta], [t[1] for t in meta])
    raw_ric = spearman([t[0] for t in raw], [t[1] for t in raw])
    buckets = quantile_buckets(meta, n_q)
    mono, spread = monotonic_score(buckets)
    by_month = defaultdict(list)
    for s in samples:
        by_month[s["month"]].append((s["x_meta"], s["y_meta"]))
    mic = monthly_ic(by_month)
    return {"n": n, "ic": ic, "rank_ic": ric, "raw_rank_ic": raw_ric,
            "buckets": buckets, "mono": mono, "spread": spread,
            "monthly_ic": mic, "icir": icir(mic), "n_months": len(mic),
            "by_month": by_month}


def evaluate_all(data, factors, horizons, n_q):
    """逐因子×周期算全套指标，返回 {(factor, horizon): metric}（txt 报告与 JSON sidecar 共用，不重复计算）。"""
    metrics = {}
    for fac in factors:
        for h in horizons:
            metrics[(fac, h)] = eval_factor(data[h].get(fac, []), n_q)
    return metrics


def factor_metrics_json(metrics, factors, horizons, main_h, days=None):
    """评估结果 -> JSON 安全结构（P1-3 图表看板消费）；buckets=[n,平均方向收益,胜率,平均因子值]。"""
    facs = []
    for fac in factors:
        by_h = {}
        for h in horizons:
            m = metrics.get((fac, h))
            if not m:
                continue
            by_h[str(h)] = {
                "n": m["n"], "ic": m["ic"], "rank_ic": m["rank_ic"],
                "raw_rank_ic": m["raw_rank_ic"], "icir": m["icir"],
                "mono": m["mono"], "spread": m["spread"],
                "buckets": [[b[0], b[1], b[2], b[3]] for b in m["buckets"]],
                "monthly_ic": [[a, b, c] for a, b, c in m["monthly_ic"]],
                "n_months": m["n_months"],
            }
        facs.append({"name": fac, "by_h": by_h})
    return {
        "generated_at": _now(), "days": days, "main_h": main_h,
        "horizons": list(horizons),
        "horizon_labels": {str(h): HORIZON_LABEL.get(h, str(h)) for h in horizons},
        "factors": facs,
    }


def build_report(data, factors, horizons, n_q, min_sample, days, metrics=None):
    L = []
    L.append("=" * 100)
    L.append(" 因子 IC 评估报告（WP-F2 B2）  近%d天样本  生成于 %s" % (days, _now()))
    L.append("=" * 100)
    L.append("口径：主看 meta RankIC=Spearman(因子值×信号方向, 方向收益)，>0=因子越支持信号后续越赚；")
    L.append("      参考 原始RankIC=Spearman(因子原值, 远期绝对方向收益)；ICIR=月度IC的mean/std；")
    L.append("      分档=沿方向强度分%d档看平均方向收益单调性；纯自有DB、零网络、纯标准库。" % n_q)
    L.append("")

    # 1) 三周期总表（衰减）
    L.append("一、各因子分周期预测力总表（看 30m→2h→次日 的衰减/增强）")
    head = " %-10s " % "因子"
    for h in horizons:
        head += "| %-22s " % HORIZON_LABEL.get(h, str(h))
    L.append(head)
    L.append(" " + "-" * 96)
    if metrics is None:
        metrics = evaluate_all(data, factors, horizons, n_q)
    for fac in factors:
        line = " %-10s " % fac
        for h in horizons:
            m = metrics[(fac, h)]
            cell = "n=%-4d RIC=%+.3f ICIR=%+.2f" % (m["n"], m["rank_ic"], m["icir"])
            line += "| %-22s " % cell
        L.append(line)
    L.append("")

    # 2) 主周期（120m）逐因子明细 + 分档
    main_h = 120 if 120 in horizons else horizons[0]
    L.append("二、主周期（%s）逐因子明细与分档单调性" % HORIZON_LABEL.get(main_h, main_h))
    L.append(" %-10s %5s %8s %8s %8s %6s %10s %7s  各档平均方向收益(%%)/胜率(%%)" %
             ("因子", "n", "IC", "RankIC", "原始RIC", "ICIR", "多空价差", "单调"))
    for fac in factors:
        m = metrics[(fac, main_h)]
        if m["n"] == 0:
            continue
        btxt = " | ".join("%.2f/%.0f" % (b[1] * 100, b[2] * 100) for b in m["buckets"])
        L.append(" %-10s %5d %+8.3f %+8.3f %+8.3f %+6.2f %+9.3f%% %5.0f%%  %s"
                 % (fac, m["n"], m["ic"], m["rank_ic"], m["raw_rank_ic"], m["icir"],
                    m["spread"] * 100, m["mono"] * 100, btxt))
    L.append("")

    # 3) walk-forward（主周期）
    L.append("三、walk-forward 滚动验证（主周期%s；之前所有月=IS，当月=OOS）"
             % HORIZON_LABEL.get(main_h, main_h))
    wf_any = False
    for fac in factors:
        m = metrics.get((fac, main_h))
        if not m or m["n"] < min_sample:
            continue
        rows, same_rate = walk_forward(m["by_month"])
        if len(rows) < 2:
            continue
        wf_any = True
        is_mean = sum(r[1] for r in rows) / len(rows)
        oos_mean = sum(r[2] for r in rows) / len(rows)
        L.append(" %-10s IS_RankIC均值%+.3f | OOS均值%+.3f | IS/OOS同号率%.0f%%（%d个OOS月）"
                 % (fac, is_mean, oos_mean, same_rate * 100, len(rows)))
    if not wf_any:
        L.append(" 可用于 walk-forward 的月份不足（样本积累中）。")
    L.append("")

    # 4) 建议权重区间（只建议、不自动改）
    L.append("四、建议权重区间（相对当前 analyzer 权重的乘数；仅建议，需人工确认，绝不自动修改）")
    for fac in factors:
        m = metrics.get((fac, main_h))
        if not m:
            continue
        lo, hi, note = suggest_band(m["rank_ic"], m["mono"], m["n"], min_sample)
        if lo is None:
            L.append(" %-10s %s" % (fac, note))
        else:
            L.append(" %-10s 建议 %.2f× ~ %.2f× 当前权重；%s（RankIC=%+.3f、单调%.0f%%、n=%d、%d个月）"
                     % (fac, lo, hi, note, m["rank_ic"], m["mono"] * 100, m["n"], m["n_months"]))
    L.append("")
    L.append("诚实边界：方向收益为信号方向×后续涨跌幅，样本来自实盘监控积累（存在品种/时段分布偏差）；")
    L.append("IC 对非线性/交互效应不敏感；样本量小的因子结论会随积累变化，本报告不构成投资建议、不改任何线上参数。")
    return "\n".join(L) + "\n"


def _now():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run(argv=None):
    ap = argparse.ArgumentParser(description="因子IC评估（WP-F2 B2，离线研究工具）")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--horizons", default="30,120,1440")
    ap.add_argument("--quantiles", type=int, default=config.FACTOR_EVAL_N_QUANTILE)
    ap.add_argument("--min-sample", type=int, default=config.FACTOR_EVAL_MIN_SAMPLE)
    ap.add_argument("--out", default=config.FACTOR_EVAL_FILE)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    import storage
    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
    db = storage.MonitorDB()
    try:
        data, factors = load_samples(db, horizons, args.days)
    finally:
        db.close()
    total = sum(len(v) for h in data for v in data[h].values())
    main_h = 120 if 120 in horizons else horizons[0]
    metrics = evaluate_all(data, factors, horizons, args.quantiles)
    text = build_report(data, factors, horizons, args.quantiles, args.min_sample,
                        args.days, metrics=metrics)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8-sig") as f:
        f.write(text)
    # P1-3：结构化 JSON sidecar（图表看板消费；纯数值/列表，defaultdict 已剥离）
    import json as _json
    sidecar = factor_metrics_json(metrics, factors, horizons, main_h, days=args.days)
    with open(config.FACTOR_EVAL_JSON, "w", encoding="utf-8") as fj:
        fj.write(_json.dumps(sidecar, ensure_ascii=False, indent=1))
    print(text)
    print("因子数 %d，配对样本点 %d；报告已写入 %s；图表 JSON 已写入 %s"
          % (len(factors), total, args.out, config.FACTOR_EVAL_JSON))
    return 0


def selftest():
    """零网络合成断言：完全单调 RankIC≈1、完全无关 RankIC≈0、pearson 已知值、分档/ICIR。"""
    # 1) 完全单调正相关 -> Spearman=1
    xs = list(range(50))
    ys = [v * 2.0 + 1.0 for v in xs]
    assert abs(spearman(xs, ys) - 1.0) < 1e-9, spearman(xs, ys)
    # 完全反相关 -> -1
    assert abs(spearman(xs, list(reversed(xs))) + 1.0) < 1e-9
    # 2) 确定性无关（交替）-> RankIC≈0
    import random
    random.seed(7)
    x2 = list(range(200))
    y2 = [((i * 37) % 7) - 3 for i in x2]  # 与序号无单调关系的周期序列
    r = spearman(x2, y2)
    assert abs(r) < 0.08, r
    # 3) Pearson 已知：x=y -> 1；常数序列 -> 0（不炸）
    assert abs(pearson([1, 2, 3, 4], [2, 4, 6, 8]) - 1.0) < 1e-9
    assert pearson([1, 1, 1], [1, 2, 3]) == 0.0
    # 4) 平均秩并列
    rk = average_ranks([10, 20, 20, 30])
    assert rk == [1.0, 2.5, 2.5, 4.0], rk
    # 5) 分档：构造严格递增关系，单调性=1、多空价差>0
    pairs = [(float(i), float(i) / 100.0) for i in range(100)]
    b = quantile_buckets(pairs, 5)
    mono, spread = monotonic_score(b)
    assert mono == 1.0 and spread > 0, (mono, spread)
    # 6) ICIR：月度IC恒正且有波动 -> 正
    mic = [("2026-0%d" % i, 0.1 + 0.01 * i, 20) for i in range(1, 8)]
    assert icir(mic) > 0
    # 7) walk-forward 同号率：构造持续正相关月度数据
    by_month = {}
    for mi, mname in enumerate(["2026-01", "2026-02", "2026-03", "2026-04"]):
        by_month[mname] = [(float(k), float(k) * (1 if mi % 2 == 0 else 1) + mi)
                           for k in range(20)]
    rows, rate = walk_forward(by_month)
    assert rows and rate == 1.0, (rows, rate)
    # 8) 建议区间：强正单调 -> 上调区间
    lo, hi, note = suggest_band(0.12, 1.0, 100, 30)
    assert lo >= 1.1
    lo2, _, _ = suggest_band(0.0, 1.0, 100, 30)
    assert lo2 < 0.9
    print("factor_eval selftest ALL PASS（单调RankIC=1、无关RankIC=%.3f、并列秩、分档/ICIR/WF 均通过）" % r)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
