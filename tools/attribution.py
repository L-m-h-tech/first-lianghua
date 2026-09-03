# -*- coding: utf-8 -*-
r"""G28（第35轮）因子收益归因 + BHB 板块归因（复盘"钱是谁赚的"，研究/复盘侧，不接常驻、不改综合分）。

第33轮全网对标把"复盘归因"列为五环节最后一块短板：本项目综合分由 9 个 part 相加（新闻/原油联动/
机构/日线动量/技术共振/分钟共振/盘中动量/量仓资金/基本面），信号结果也早已落库
（signal_outcomes：30分钟/2小时/次日的方向收益），但从没回答过"已实现的盈亏到底该记在哪个因子、
哪个板块头上"。本工具用两类教科书方法、纯标准库把它拆清：

一、多因子收益归因（加法闭合 + 带截距 OLS）
  - 样本=实盘监控积累的信号事件：signals.parts_json ⨝ signal_outcomes（hit/miss/flat 为有效样本）。
  - 方向化暴露 x_ik = part_k(i) × 信号方向 dir_i（meta-labeling 口径，与 tools/factor_eval 完全一致：
    做空时正向 part 反而是负暴露）；被解释变量 y_i = 方向收益（=dir_i×(评估价/入场价−1)，storage 口径）。
  - OLS：y = α + Σ β_k·x_k + ε（正规方程+高斯消元，复用 tsmom_eval._solve，零方差/共线列自动剔除、
    奇异安全降级）。β_k=该因子每单位方向化暴露的边际收益；**加法分解 mean(y)=α+Σ β_k·mean(x_k)
    严格闭合（闭合误差≈0）**，即把平均盈亏逐笔记到每个因子与"残差 α"头上；另给 β 的 t 值、
    因子 IC、因子"支持时胜率/均收"、IS70/OOS30 的 β 方向一致性。

二、BHB 板块归因（Brinson-Hood-Beebower 1986，CFA 标准三效应）
  - 组合 P=实际信号（板块事件占比 w_p、板块内方向化平均收益 R_p）；基准 B=按全市场 64 品种板块
    只数占比 w_b（在有事件的板块集合内归一）、板块内"无方向"平均绝对涨跌 R_b（=y/dir）。
  - 配置效应 AR=Σ(w_p−w_b)·R_b（把信号部署到板块结构更优处的贡献）；
    选择效应 SR=Σ w_b·(R_p−R_b)（板块内方向/择时选择，相对板块自身平均波动的超额）；
    交互效应 IR=Σ(w_p−w_b)(R_p−R_b)；恒等式 AR+SR+IR = R_p−R_b 严格闭合。

三、累计归因曲线：事件按时间排序逐笔累计各因子贡献与残差，落 reports/attribution_curve.csv；
    BHB 另按评估月出月度三效应序列（写 JSON/txt），供后续看板（本轮不接 charts/main，研究侧先行）。

纯标准库、零新增第三方依赖、只读 monitor.db（mode=ro，绝不写库）。
输出 reports/attribution.txt + attribution.json + attribution_curve.csv。
用法（项目根目录）：
  D:\Python\python.exe tools\attribution.py                 # 全量、三周期，主周期=次日
  D:\Python\python.exe tools\attribution.py --days 365      # 近365天
  D:\Python\python.exe tools\attribution.py --selftest      # 零网络/零DB合成断言
"""
import argparse
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import config  # noqa: E402
import factor_eval as fe  # noqa: E402  复用 pearson/spearman/_canon，不重造轮子
from tsmom_eval import _solve  # noqa: E402  复用高斯消元

HORIZON_LABEL = {30: "30分钟", 120: "2小时", 1440: "次日"}


# =========================== 纯统计：多因子加法归因（可合成断言） ===========================
def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def ols_fit(X, y):
    """带截距最小二乘。X=[[特征...],...]（不含常数项，内部自动加），返回 (beta[截距,b1..], None)；
    样本不足/矩阵奇异返回 (None, 被用列)。零方差或共线列由调用方先剔除。"""
    n = len(y)
    k = len(X[0]) if X else 0
    if n < k + 2 or k == 0:
        return None
    M = [[1.0] + [float(v) for v in row] for row in X]
    p = k + 1
    XtX = [[sum(M[i][a] * M[i][b] for i in range(n)) for b in range(p)] for a in range(p)]
    Xty = [sum(M[i][a] * y[i] for i in range(n)) for a in range(p)]
    beta = _solve(XtX, Xty)
    return beta


def _ols_detail(X, y, beta):
    """给定 OLS 系数，返回 R² 与每个系数的 t 值（(X'X)^-1 对角×残差方差）。"""
    n = len(y)
    k = len(X[0])
    M = [[1.0] + [float(v) for v in row] for row in X]
    p = k + 1
    pred = [sum(beta[a] * M[i][a] for a in range(p)) for i in range(n)]
    resid = [y[i] - pred[i] for i in range(n)]
    sse = sum(e * e for e in resid)
    mu = _mean(y)
    sst = sum((v - mu) ** 2 for v in y)
    r2 = 1.0 - sse / sst if sst > 1e-18 else 0.0
    XtX = [[sum(M[i][a] * M[i][b] for i in range(n)) for b in range(p)] for a in range(p)]
    tstats = [0.0] * p
    if n > p:
        sigma2 = sse / (n - p)
        for j in range(p):
            ej = [1.0 if a == j else 0.0 for a in range(p)]
            col = _solve(XtX, ej)          # (X'X)^-1 的第 j 列
            if col is not None and col[j] > 1e-14 and sigma2 > 0:
                tstats[j] = beta[j] / math.sqrt(sigma2 * col[j])
    return {"r2": r2, "tstats": tstats}


def factor_attribution(events, factor_keys, x_eps=0.05):
    """事件样本 -> 多因子加法归因结果。

    events: [{'y':方向收益, 'x':{因子:方向化暴露}, 'dir':±1, 'sector':.., 'band':.., 'ts':..}]
    factor_keys: 规范因子顺序（缺失因子暴露按 0，即其当时对综合分确无贡献）。
    返回 dict：n/used(实际入模因子)/dropped(零方差或共线)/alpha/mean_y/closure_resid/r2/
              rows[{factor,n,beta,tstat,mean_x,contrib,share,ic,win_support,avg_support,avg_against}]。
    """
    n = len(events)
    out = {"n": n, "used": [], "dropped": [], "rows": [], "alpha": None, "mean_y": 0.0,
           "closure_resid": None, "r2": 0.0, "beta": {}, "mean_x": {}}
    if n == 0:
        return out
    ys = [float(e["y"]) for e in events]
    mean_y = _mean(ys)
    out["mean_y"] = mean_y

    # 逐步剔除零方差/导致奇异的列，保证 OLS 可解且不抛异常
    used = list(factor_keys)
    beta = None
    while used:
        X = [[float(e["x"].get(f, 0.0)) for f in used] for e in events]
        col_var = []
        for j in range(len(used)):
            col = [r[j] for r in X]
            m = _mean(col)
            col_var.append(sum((v - m) ** 2 for v in col))
        keep = [j for j, v in enumerate(col_var) if v > 1e-14]
        dropped_now = [used[j] for j in range(len(used)) if j not in keep]
        if dropped_now:
            used = [f for j, f in enumerate(used) if j in keep]
            out["dropped"].extend(dropped_now)
            continue
        beta = ols_fit(X, ys)
        if beta is not None:
            break
        # 仍奇异（多重共线）：剔除平均绝对暴露最小的一列再试
        abs_mean = sorted(range(len(used)),
                          key=lambda j: abs(_mean([r[j] for r in X])))
        out["dropped"].append(used.pop(abs_mean[0]))
        beta = None

    if not used or beta is None:
        # 全部因子都不可用：只给截距=均值
        out["alpha"] = mean_y
        out["closure_resid"] = 0.0
        used = []
        X = [[0.0] for _ in events]
        detail = {"r2": 0.0, "tstats": [0.0]}
    else:
        X = [[float(e["x"].get(f, 0.0)) for f in used] for e in events]
        detail = _ols_detail(X, ys, beta)
    out["used"] = used
    out["r2"] = detail["r2"]
    alpha = beta[0] if beta is not None else mean_y
    out["alpha"] = alpha

    contrib_sum = 0.0
    for j, f in enumerate(used):
        col = [float(e["x"].get(f, 0.0)) for e in events]
        mx = _mean(col)
        bj = beta[j + 1]
        contrib = bj * mx
        contrib_sum += contrib
        out["beta"][f] = bj
        out["mean_x"][f] = mx
        sup = [(col[i], ys[i]) for i in range(n) if col[i] > x_eps]
        aga = [(col[i], ys[i]) for i in range(n) if col[i] < -x_eps]
        win = (sum(1 for _, yy in sup if yy > 0) / len(sup)) if sup else None
        ic = fe.pearson(col, ys)
        out["rows"].append({
            "factor": f, "n": sum(1 for v in col if abs(v) > 1e-12),
            "beta": bj, "tstat": detail["tstats"][j + 1],
            "mean_x": mx, "contrib": contrib, "ic": ic,
            "win_support": win,
            "avg_support": _mean([yy for _, yy in sup]) if sup else None,
            "avg_against": _mean([yy for _, yy in aga]) if aga else None,
        })
    closed = alpha + contrib_sum
    out["closure_resid"] = mean_y - closed
    # 占比（以 |平均收益| 为分母；平均收益≈0 时占比记 None）
    denom = abs(mean_y) if abs(mean_y) > 1e-12 else None
    for r in out["rows"]:
        r["share"] = (r["contrib"] / denom) if denom else None
    out["contrib_sum"] = contrib_sum
    return out


def is_oos_split(events, oos_ratio=0.3):
    """按 ts 排序切前 (1-r) IS / 后 r OOS（事件样本，时间有序防自欺）；返回 (is,oos)。"""
    ordered = sorted(events, key=lambda e: e.get("ts") or "")
    cut = max(1, int(round(len(ordered) * (1 - oos_ratio))))
    if len(ordered) - cut < 2:
        return ordered, []
    return ordered[:cut], ordered[cut:]


# =========================== 纯统计：BHB 板块归因（可手算断言） ===========================
def bhb(sector_stats, bench_weights):
    """Brinson-Hood-Beebower 三效应（纯函数，分量严格闭合）。

    sector_stats: {sector: {'wp':组合权重, 'rp':组合板块收益, 'rb':基准板块收益}}
    bench_weights: {sector: 基准权重}（只取 sector_stats 中出现的板块；调用方负责归一）。
    返回 {sectors:[...], alloc/select/inter/total, port_ret, bench_ret, excess, closure_resid}。
    """
    rows = []
    port_ret = bench_ret = 0.0
    for s, st in sorted(sector_stats.items()):
        wp = float(st["wp"])
        rp = float(st["rp"])
        wb = float(bench_weights.get(s, 0.0))
        rb = float(st["rb"])
        alloc = (wp - wb) * rb
        select = wb * (rp - rb)
        inter = (wp - wb) * (rp - rb)
        rows.append({"sector": s, "wp": wp, "wb": wb, "rp": rp, "rb": rb,
                     "alloc": alloc, "select": select, "inter": inter,
                     "effect": alloc + select + inter})
        port_ret += wp * rp
        bench_ret += wb * rb
    alloc_t = sum(r["alloc"] for r in rows)
    select_t = sum(r["select"] for r in rows)
    inter_t = sum(r["inter"] for r in rows)
    total = alloc_t + select_t + inter_t
    return {"sectors": rows, "alloc": alloc_t, "select": select_t, "inter": inter_t,
            "total": total, "port_ret": port_ret, "bench_ret": bench_ret,
            "excess": port_ret - bench_ret,
            "closure_resid": (port_ret - bench_ret) - total}


def events_to_sector_stats(events):
    """事件 -> {sector: {n, wp=事件占比, rp=方向化均收, rb=无方向绝对均收}}（wp 在有事件板块内归一）。"""
    by = defaultdict(list)
    for e in events:
        by[e.get("sector") or "未知"].append(e)
    n = len(events)
    stats = {}
    for s, es in by.items():
        ys = [float(e["y"]) for e in es]
        raw = [float(e["y"]) / (1 if e.get("dir", 1) == 0 else e.get("dir", 1)) for e in es]
        stats[s] = {"n": len(es), "wp": len(es) / n if n else 0.0,
                    "rp": _mean(ys), "rb": _mean(raw)}
    return stats


def universe_sector_weights(sectors_with_events=None):
    """全市场 64 品种按板块只数占比；可只在给定板块集合内归一（BHB 考虑集口径）。"""
    cnt = defaultdict(int)
    for _, meta in config.VARIETIES.items():
        cnt[meta.get("cat", "未知")] += 1
    keys = sectors_with_events if sectors_with_events else list(cnt.keys())
    tot = sum(cnt.get(s, 0) for s in keys)
    if tot <= 0:
        # 兜底：事件里出现了 config 未登记板块时退化为等权
        return {s: 1.0 / len(keys) for s in keys}
    return {s: cnt.get(s, 0) / tot for s in keys}


# =========================== 累计归因曲线 / 分组统计 ===========================
def factor_curve(events, attr, factor_keys):
    """事件按 ts 排序，逐笔累计：总收益、残差(y−Σβx)、各入模因子 β·x；末端严格闭合=Σy。"""
    used = attr.get("used", [])
    beta = attr.get("beta", {})
    ordered = sorted(events, key=lambda e: e.get("ts") or "")
    rows, cum_t, cum_res = [], 0.0, 0.0
    cum_f = {f: 0.0 for f in used}
    for i, e in enumerate(ordered, 1):
        y = float(e["y"])
        fac_now = sum(beta[f] * float(e["x"].get(f, 0.0)) for f in used)
        cum_t += y
        cum_res += y - fac_now        # OLS 截距口径残差逐笔累计，末端合计=n·α，与因子项闭合
        for f in used:
            cum_f[f] += beta[f] * float(e["x"].get(f, 0.0))
        row = {"idx": i, "ts": e.get("ts", ""), "sym": e.get("sym", ""),
               "sector": e.get("sector", ""), "y": y, "cum_total": cum_t,
               "cum_alpha": cum_res}
        for f in factor_keys:
            row["cum_" + f] = cum_f.get(f, 0.0)
        rows.append(row)
    return rows


def group_mean(events, key):
    g = defaultdict(list)
    for e in events:
        g[e.get(key) or "未知"].append(float(e["y"]))
    return {k: {"n": len(v), "mean_y": _mean(v),
                "win": sum(1 for x in v if x > 0) / len(v)} for k, v in sorted(g.items())}


# =========================== 数据装载（只读 DB，纯自有、零网络） ===========================
def parse_event_row(row, factor_keys):
    """storage join 风格的一行 -> 规范事件 dict；坏行返回 None。"""
    try:
        parts = row.get("parts_json")
        parts = json.loads(parts) if isinstance(parts, str) else (parts or {})
        d = int(row["direction_int"])
        y = float(row["ret"])
        if d not in (1, -1):
            return None
    except (TypeError, ValueError, KeyError):
        return None
    x = {}
    for k, v in parts.items():
        try:
            x[fe._canon(k)] = float(v) * d       # meta-labeling 方向化暴露
        except (TypeError, ValueError):
            continue
    return {"y": y, "x": x, "dir": d,
            "sector": row.get("cat") or "未知", "sym": row.get("sym") or "",
            "band": row.get("score_band") or "", "score": row.get("score"),
            "ts": row.get("entry_ts") or row.get("eval_ts") or "",
            "horizon": int(row.get("horizon_min", 0))}


def load_events(db_path, horizons=(30, 120, 1440), days=None):
    """只读连接 monitor.db，按 horizon 返回 {h: [event...]}。绝不写库（mode=ro）。"""
    uri = "file:%s?mode=ro" % str(db_path).replace("\\", "/")
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    ph = ",".join("?" * len(horizons))
    sql = ("SELECT o.direction_int,o.score,o.score_band,o.horizon_min,o.ret,o.status,"
           "o.entry_ts,o.eval_ts,o.variety,s.cat,s.sym,s.parts_json "
           "FROM signal_outcomes o JOIN signals s ON s.id=o.signal_id "
           "WHERE o.status IN ('hit','miss','flat') AND o.horizon_min IN (%s)" % ph)
    args = [int(h) for h in horizons]
    if days:
        sql += " AND o.eval_ts>=?"
        args.append((datetime.now()
                     .fromtimestamp(datetime.now().timestamp() - int(days) * 86400)
                     ).strftime("%Y-%m-%d %H:%M:%S"))
    sql += " ORDER BY o.eval_ts ASC"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    data = {h: [] for h in horizons}
    keys = list(config.ATTR_FACTOR_ORDER)
    for r in rows:
        e = parse_event_row(dict(r), keys)
        if e is not None and e["horizon"] in data:
            data[e["horizon"]].append(e)
    return data


# =========================== 报告（txt + json sidecar） ===========================
def _pct(x, d=2):
    return ("%+." + str(d) + "f%%") % (x * 100) if x is not None else "--"


def _num(x, d=4):
    return ("%." + str(d) + "f") % x if x is not None else "--"


def _win(x):
    return ("%.1f%%" % (x * 100)) if x is not None else "--"


def monthly_bhb(events):
    """按评估月聚合 BHB（月度三效应序列），返回 [{month,n,alloc,select,inter,excess},...]。"""
    bym = defaultdict(list)
    for e in events:
        bym[(e.get("ts") or "")[:7]].append(e)
    out = []
    for m in sorted(bym):
        es = bym[m]
        stats = events_to_sector_stats(es)
        wb = universe_sector_weights(list(stats.keys()))
        r = bhb(stats, wb)
        out.append({"month": m, "n": len(es), "alloc": r["alloc"], "select": r["select"],
                    "inter": r["inter"], "excess": r["excess"]})
    return out


def attribute_horizon(events, factor_keys, oos_ratio=0.3, x_eps=0.05):
    """单周期完整归因：因子归因 + IS/OOS beta + BHB + 月度BHB + 分组。"""
    attr = factor_attribution(events, factor_keys, x_eps=x_eps)
    is_ev, oos_ev = is_oos_split(events, oos_ratio)
    attr["is"] = factor_attribution(is_ev, attr["used"], x_eps=x_eps) if len(is_ev) >= 10 else None
    attr["oos"] = factor_attribution(oos_ev, attr["used"], x_eps=x_eps) if len(oos_ev) >= 10 else None
    stats = events_to_sector_stats(events)
    wb = universe_sector_weights(list(stats.keys()))
    attr["bhb"] = bhb(stats, wb)
    attr["monthly_bhb"] = monthly_bhb(events)
    attr["by_dir"] = group_mean(events, "dir")
    attr["by_band"] = group_mean(events, "band")
    return attr


def build_report(data, factor_keys, main_h, days=None):
    L = []
    L.append("因子收益归因 + BHB 板块归因报告（G28 复盘）  生成于 %s" % _now())
    L.append("=" * 104)
    L.append("数据：signals.parts_json ⨝ signal_outcomes（hit/miss/flat 有效样本，只读 monitor.db，纯标准库）；")
    L.append("因子暴露=part×信号方向（meta-labeling，同 factor_eval），y=方向收益；加法归因 mean(y)=α+Σβ·mean(x) 严格闭合。")
    if days:
        L.append("样本窗口：近 %d 天。" % days)
    L.append("")
    sidecar = {"main_h": main_h, "horizons": {}, "factor_order": list(factor_keys)}

    for h in sorted(data):
        events = data[h]
        L.append("%s %s%s   有效事件 n=%d" % (
            "=" * 10, HORIZON_LABEL.get(h, h),
            ("（主周期）" if h == main_h else "（对照周期）"), len(events)))
        if len(events) < config.ATTR_MIN_SAMPLE:
            L.append("  样本不足（n=%d<%d），只计数不下结论。" % (len(events), config.ATTR_MIN_SAMPLE))
            L.append("")
            sidecar["horizons"][h] = {"n": len(events), "enough": False}
            continue
        a = attribute_horizon(events, factor_keys,
                              oos_ratio=config.ATTR_OOS_RATIO, x_eps=config.ATTR_X_EPS)
        # ---- 二、因子归因表 ----
        L.append("一、多因子加法归因（OLS：β=每单位方向化暴露的边际方向收益；贡献=β×平均暴露）")
        L.append("  %-8s %5s %10s %7s %10s %9s %8s %10s %10s" %
                 ("因子", "n", "β", "t值", "平均暴露", "贡献", "占比", "IC", "支持胜率"))
        for r in a["rows"]:
            share = ("%.0f%%" % (r["share"] * 100)) if r.get("share") is not None else "--"
            L.append("  %-8s %5d %10s %7s %10s %9s %9s %8s %10s" %
                     (r["factor"], r["n"], _num(r["beta"], 5), _num(r["tstat"], 2),
                      _num(r["mean_x"], 3), _pct(r["contrib"], 3), share,
                      _num(r["ic"], 3), _win(r["win_support"])))
        L.append("  %-8s %5s %10s %7s %10s %9s" %
                 ("残差α", a["n"], _num(a["alpha"], 5), "--", "--", _pct(a["alpha"], 3)))
        L.append("  平均方向收益合计 %s = 残差α %s + Σ因子贡献 %s；闭合误差 %.2e；OLS R²=%.3f" %
                 (_pct(a["mean_y"], 3), _pct(a["alpha"], 3), _pct(a["contrib_sum"], 3),
                  a["closure_resid"], a["r2"]))
        if a["dropped"]:
            L.append("  注：零方差/共线未入模因子：%s（样本中几乎不出现，不参与回归）" % "、".join(a["dropped"]))
        # 支持/反对对照
        L.append("  因子支持(x>%.2f) vs 反对(x<-%.2f) 平均方向收益：" %
                 (config.ATTR_X_EPS, config.ATTR_X_EPS))
        for r in a["rows"]:
            L.append("    %-8s 支持 %s（%s）/ 反对 %s" %
                     (r["factor"], _pct(r["avg_support"], 3), _win(r["win_support"]),
                      _pct(r["avg_against"], 3)))
        # ---- IS/OOS 稳健 ----
        if a["is"] and a["oos"]:
            L.append("二、IS(前%.0f%%)/OOS(后%.0f%%) β方向一致性（防过拟合）" %
                     ((1 - config.ATTR_OOS_RATIO) * 100, config.ATTR_OOS_RATIO * 100))
            agree = 0
            tot = 0
            for f in a["used"]:
                bi = a["is"]["beta"].get(f)
                bo = a["oos"]["beta"].get(f)
                if bi is not None and bo is not None:
                    tot += 1
                    if bi * bo >= 0:
                        agree += 1
                    L.append("    %-8s IS β=%s  OOS β=%s  %s" %
                             (f, _num(bi, 5), _num(bo, 5),
                              "同向" if bi * bo >= 0 else "翻转✗"))
            L.append("  β方向一致 %d/%d（OOS 翻转越多说明该因子贡献越不稳）。" % (agree, tot))
        # ---- 三、多空/分档 ----
        L.append("三、分组平均方向收益（交叉验证）")
        dirm = {1: "做多", -1: "做空"}
        L.append("  按方向：" + "；".join(
            "%s n=%d 均收%s 胜率%s" %
            (dirm.get(int(k), k), v["n"], _pct(v["mean_y"], 3), _win(v["win"]))
            for k, v in a["by_dir"].items()))
        L.append("  按分档：" + "；".join(
            "%s n=%d 均收%s" % (k, v["n"], _pct(v["mean_y"], 3))
            for k, v in a["by_band"].items()))
        # ---- 四、BHB ----
        b = a["bhb"]
        L.append("四、BHB 板块归因（基准=全市场品种板块只数占比×板块无方向均涨；组合=实际信号）")
        L.append("  %-8s %7s %7s %10s %10s %10s %10s %10s" %
                 ("板块", "w_p", "w_b", "R_p", "R_b", "配置AR", "选择SR", "交互IR"))
        for r in b["sectors"]:
            L.append("  %-8s %7.3f %7.3f %10s %10s %10s %10s %10s" %
                     (r["sector"], r["wp"], r["wb"], _pct(r["rp"], 3), _pct(r["rb"], 3),
                      _pct(r["alloc"], 3), _pct(r["select"], 3), _pct(r["inter"], 3)))
        L.append("  合计：配置 %s + 选择 %s + 交互 %s = %s；组合 %s − 基准 %s = 超额 %s；闭合误差 %.2e" %
                 (_pct(b["alloc"], 3), _pct(b["select"], 3), _pct(b["inter"], 3),
                  _pct(b["total"], 3), _pct(b["port_ret"], 3), _pct(b["bench_ret"], 3),
                  _pct(b["excess"], 3), b["closure_resid"]))
        # ---- 五、月度 BHB ----
        mb = a["monthly_bhb"]
        if mb:
            L.append("五、月度 BHB 三效应序列（累计归因曲线另见 attribution_curve.csv）")
            L.append("  " + " ".join("%s:配置%s/选择%s/超额%s" %
                                     (m["month"][2:], _pct(m["alloc"], 2),
                                      _pct(m["select"], 2), _pct(m["excess"], 2))
                                     for m in mb[-10:]))
        L.append("")
        sidecar["horizons"][h] = {
            "n": a["n"], "enough": True, "mean_y": a["mean_y"], "alpha": a["alpha"],
            "r2": a["r2"], "closure_resid": a["closure_resid"],
            "used": a["used"], "dropped": a["dropped"],
            "factors": a["rows"], "bhb": {k: v for k, v in b.items() if k != "sectors"},
            "bhb_sectors": b["sectors"], "monthly_bhb": mb,
            "by_dir": {str(k): v for k, v in a["by_dir"].items()},
            "by_band": a["by_band"]}

    L.append("诚实边界：①样本为实盘监控自 2026-08 起积累的信号事件，时段/品种分布有偏、非连续组合；")
    L.append("②OLS 为线性加法归因，不刻画因子交互/非线性，β 是相关而非因果；③BHB 基准为事件条件下的")
    L.append("板块无方向均涨，不是逐日连续基准，板块结论用于定位结构而非可交易收益；本报告不改任何线上权重。")
    return "\n".join(L) + "\n", sidecar


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _json_safe(o):
    if isinstance(o, dict):
        return {str(k): _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    if isinstance(o, float):
        if math.isnan(o) or math.isinf(o):
            return None
        return o
    return o


def write_curve(path, rows, factor_keys):
    head = ["idx", "ts", "sym", "sector", "y", "cum_total", "cum_alpha"] + \
           ["cum_" + f for f in factor_keys]
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(",".join(head) + "\n")
        for r in rows:
            vals = [r.get("idx"), r.get("ts"), r.get("sym"), r.get("sector")]
            vals += ["%.6f" % r.get("y", 0.0), "%.6f" % r.get("cum_total", 0.0),
                     "%.6f" % r.get("cum_alpha", 0.0)]
            vals += ["%.6f" % r.get("cum_" + f, 0.0) for f in factor_keys]
            fh.write(",".join(str(v) for v in vals) + "\n")


def run(argv=None):
    ap = argparse.ArgumentParser(description="G28 因子收益归因 + BHB 板块归因")
    ap.add_argument("--db", default=config.MONITOR_DB)
    ap.add_argument("--days", type=int, default=0, help="只取近 N 天评估事件，0=全量")
    ap.add_argument("--horizons", default=",".join(map(str, config.ATTR_HORIZONS)))
    ap.add_argument("--main-horizon", type=int, default=config.ATTR_MAIN_HORIZON)
    ap.add_argument("--out", default=config.ATTR_FILE)
    ap.add_argument("--json", dest="json_out", default=config.ATTR_JSON)
    ap.add_argument("--curve", default=config.ATTR_CURVE)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()

    horizons = tuple(int(x) for x in str(args.horizons).split(",") if x.strip())
    data = load_events(args.db, horizons, args.days or None)
    factor_keys = list(config.ATTR_FACTOR_ORDER)
    text, sidecar = build_report(data, factor_keys, args.main_horizon, args.days or None)
    with open(args.out, "w", encoding="utf-8-sig") as fh:
        fh.write(text)
    with open(args.json_out, "w", encoding="utf-8") as fh:
        json.dump(_json_safe(sidecar), fh, ensure_ascii=False, indent=1, allow_nan=False)
    # 主周期累计归因曲线
    main_events = data.get(args.main_horizon, [])
    if len(main_events) >= config.ATTR_MIN_SAMPLE:
        a = attribute_horizon(main_events, factor_keys,
                              oos_ratio=config.ATTR_OOS_RATIO, x_eps=config.ATTR_X_EPS)
        write_curve(args.curve, factor_curve(main_events, a, factor_keys), factor_keys)
    for h in horizons:
        print("%s n=%d" % (HORIZON_LABEL.get(h, h), len(data.get(h, []))))
    print("归因报告已写：%s" % args.out)
    return 0


# =========================== 零网络/零DB 合成断言 ===========================
def _ev(y, x, sector="黑色", d=1, band="分批", ts="2026-01-01", sym="RB"):
    return {"y": y, "x": dict(x), "dir": d, "sector": sector, "band": band, "ts": ts, "sym": sym}


def selftest():
    keys = ["A", "B", "C"]

    # 1) 方向化暴露符号：做空时正 part → 负暴露（parse 层）
    row = {"direction_int": -1, "ret": -0.01, "parts_json": json.dumps({"A": 2.0}),
           "cat": "有色", "sym": "CU", "score_band": "轻仓", "entry_ts": "2026-01-01",
           "horizon_min": 1440}
    e = parse_event_row(row, keys)
    assert e["x"]["A"] == -2.0 and abs(e["y"] + 0.01) < 1e-12 and e["dir"] == -1
    # 动态原油键归一
    row2 = dict(row, parts_json=json.dumps({"原油联动(w=0.50)": 1.0}))
    e2 = parse_event_row(row2, keys)
    assert "原油联动" in e2["x"] and abs(e2["x"]["原油联动"] + 1.0) < 1e-12
    # 坏行安全
    assert parse_event_row({"direction_int": 0}, keys) is None

    # 2) OLS 精确恢复已知系数 + 加法闭合：y = 0.001 + 2*A - 1*B（无噪声）
    evs = []
    for i in range(40):
        a = (i % 5) - 2
        b = ((i * 3) % 7) - 3
        evs.append(_ev(0.001 + 2.0 * a - 1.0 * b, {"A": float(a), "B": float(b), "C": 0.0},
                       ts="2026-%02d-%02d" % (i // 28 + 1, i % 28 + 1)))
    att = factor_attribution(evs, keys, x_eps=0.05)
    assert "C" in att["dropped"]                       # 零方差列被剔除
    assert abs(att["alpha"] - 0.001) < 1e-9, att
    assert abs(att["beta"]["A"] - 2.0) < 1e-9 and abs(att["beta"]["B"] + 1.0) < 1e-9
    assert abs(att["closure_resid"]) < 1e-12, att      # 严格闭合
    assert att["r2"] > 0.999

    # 3) 空样本 / 全零方差安全降级，不抛异常
    z = factor_attribution([], keys)
    assert z["n"] == 0 and z["rows"] == []
    z2 = factor_attribution([_ev(0.01, {"A": 0.0}), _ev(0.02, {"A": 0.0})], keys)
    assert abs(z2["alpha"] - 0.015) < 1e-12 and z2["used"] == []

    # 4) BHB 手算两板块 + 恒等式闭合
    #   板块1: wp=0.6,wb=0.5,rp=0.10,rb=0.08 → AR=.1*.08=.008 SR=.5*.02=.01 IR=.1*.02=.002
    #   板块2: wp=0.4,wb=0.5,rp=0.02,rb=0.04 → AR=-.1*.04=-.004 SR=.5*(-.02)=-.01 IR=(-.1)*(-.02)=.002
    stats = {"S1": {"wp": 0.6, "rp": 0.10, "rb": 0.08},
             "S2": {"wp": 0.4, "rp": 0.02, "rb": 0.04}}
    wb = {"S1": 0.5, "S2": 0.5}
    r = bhb(stats, wb)
    assert abs(r["sectors"][0]["alloc"] - 0.008) < 1e-12
    assert abs(r["sectors"][0]["select"] - 0.010) < 1e-12
    assert abs(r["sectors"][0]["inter"] - 0.002) < 1e-12
    assert abs(r["sectors"][1]["alloc"] + 0.004) < 1e-12
    assert abs(r["port_ret"] - 0.068) < 1e-12        # .6*.1+.4*.02
    assert abs(r["bench_ret"] - 0.060) < 1e-12       # .5*.08+.5*.04
    assert abs(r["excess"] - 0.008) < 1e-12
    assert abs(r["closure_resid"]) < 1e-12           # AR+SR+IR=excess
    assert abs(r["total"] - (0.008 - 0.004 + 0.010 - 0.010 + 0.002 + 0.002)) < 1e-12

    # 5) events_to_sector_stats：wp 归一、rb=无方向均涨（rp=方向化）
    es = [_ev(0.02, {"A": 1}, "S1", d=1), _ev(0.04, {"A": 1}, "S1", d=1),
          _ev(-0.02, {"A": 1}, "S2", d=-1)]
    st = events_to_sector_stats(es)
    assert abs(st["S1"]["wp"] - 2 / 3) < 1e-12
    assert abs(st["S1"]["rp"] - 0.03) < 1e-12
    # S2 做空 y=-0.02 → 无方向绝对涨跌 rb=y/dir=+0.02
    assert abs(st["S2"]["rb"] - 0.02) < 1e-12 and abs(st["S2"]["rp"] + 0.02) < 1e-12

    # 6) 累计曲线末端：Σ各因子累计+残差累计 = 累计总收益（闭合）
    curve = factor_curve(evs, att, keys)
    last = curve[-1]
    fac_sum = sum(last["cum_" + f] for f in att["used"])
    assert abs((last["cum_alpha"] + fac_sum) - last["cum_total"]) < 1e-9

    # 7) IS/OOS 切分有序、不重叠
    is_ev, oos_ev = is_oos_split(evs, 0.3)
    assert len(is_ev) + len(oos_ev) == len(evs) and is_ev[-1]["ts"] <= oos_ev[0]["ts"]

    # 8) 端到端 build_report：结构/闭合/键齐全，不联网不读库
    data = {1440: evs * 2}
    # 让样本量达到默认门槛且时间跨度足够
    for k, e in enumerate(data[1440]):
        e["ts"] = "2026-%02d-%02d" % (k // 28 + 1, k % 28 + 1)
    text, sc = build_report(data, ["A", "B", "C"], 1440)
    assert "BHB" in text and "闭合误差" in text
    assert sc["horizons"][1440]["enough"] is True
    assert abs(sc["horizons"][1440]["bhb"]["closure_resid"]) < 1e-12
    json.dumps(_json_safe(sc), allow_nan=False)       # sidecar 必须 JSON 安全（无 NaN）

    print("attribution selftest ALL PASS（方向化暴露/OLS恢复与闭合/零方差安全/"
          "BHB手算与恒等式/板块统计/累计曲线闭合/IS-OOS/报告结构 共8组）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
