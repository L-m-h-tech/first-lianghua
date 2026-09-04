# -*- coding: utf-8 -*-
r"""G25（第38轮）表达式因子研究台 tools/expr_research.py——纯离线、零网络、只读 G21 面板，证明三件事：

1) training-serving parity（实时/离线同一引擎逐值一致）：
   · 面板列直读（离线） vs panel_rows_to_bars 回读成 bar 再取 c/v（实时链路拿到的形状）喂**同一条表达式**，
     全64品种逐值 maxAbsDiff 必须为 0（面板→bar→引擎无损）；
   · 表达式版 5日动量 delta(close,5)/delay(close,5) 与面板里实时管线 compute_indicators 落库的 ret5 列逐值对齐
     （这才是真正把"表达式引擎"接到"线上指标口径"的 training-serving 证据）。
2) 表达式因子的前向预测力（G29 式体检、严格只向未来取收益）：对 factor_expr.LIBRARY 每个因子，算对未来
   H=1/5/20 交易日收益的 Spearman RankIC（逐品种均值 + 全样本池化），负结果照实写；research 因子不进综合分。
3) 截面算子在真实多品种上可用：取最近交易日 cross_rank 跨品种排序演示。
不写生产库、不被 main import、零新增依赖（只用 sqlite3/json/标准库 + factor_expr/panel_builder）。
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import factor_expr as fe          # noqa: E402  根模块：表达式引擎+治理
import panel_builder as pb        # noqa: E402  同 tools：G21 面板回读

HORIZONS = (1, 5, 20)
DEFAULT_DB = os.path.join(_ROOT, "cache", "research_panel.db")
DEFAULT_TXT = os.path.join(_ROOT, "reports", "expr_research.txt")
DEFAULT_JSON = os.path.join(_ROOT, "reports", "expr_research.json")


# ---------------- 输入装配：面板列（离线） vs bar 回读（实时形状） ----------------
def series_from_rows(rows):
    """离线：直接读面板存储列（KDJ 因子需 high/low，缺则回退用收盘）。"""
    return {"close": [r["c"] for r in rows], "volume": [r["v"] for r in rows],
            "high": [r.get("h", r["c"]) for r in rows],
            "low": [r.get("l", r["c"]) for r in rows]}


def series_from_bars(rows):
    """实时形状：面板行先回读成 bar-dict（研究工具读面板的统一桥梁），再取 c/v/h/l。"""
    bars = pb.panel_rows_to_bars(rows)
    return {"close": [b["c"] for b in bars], "volume": [b["v"] for b in bars],
            "high": [b.get("h", b["c"]) for b in bars],
            "low": [b.get("l", b["c"]) for b in bars]}


def forward_return(close, t, h):
    """严格只向未来：t 时点看 t+h 收盘，不足返回 None。"""
    if t + h >= len(close):
        return None
    a, b = close[t], close[t + h]
    if not (fe._isnum(a) and fe._isnum(b)) or a <= 0:
        return None
    return b / a - 1.0


# ---------------- parity 与指标交叉核对 ----------------
def parity_for_rows(rows):
    """同一表达式：面板列直读 vs bar 回读，逐值比对，返回 (maxAbsDiff, 有限点总数, 不一致数)。"""
    off = series_from_rows(rows)
    rt = series_from_bars(rows)
    max_diff = 0.0
    nfinite = 0
    mismatch = 0
    for fac in fe.LIBRARY:
        a = fe.compute_ts(fac["expr"], off)
        b = fe.compute_ts(fac["expr"], rt)
        if len(a) != len(b):
            mismatch += 1
            continue
        for x, y in zip(a, b):
            if fe._isnum(x) or fe._isnum(y):
                nfinite += 1
                if not (fe._isnum(x) and fe._isnum(y)):
                    mismatch += 1
                else:
                    d = abs(x - y)
                    max_diff = max(max_diff, d)
                    if d > 0.0:
                        mismatch += 1
    return max_diff, nfinite, mismatch


def indicator_crosscheck(rows):
    """表达式版5日动量 vs 面板已落库的实时 ret5：仅在表达式已脱暖机处比较，返回 (maxAbsDiff,n)。"""
    s = series_from_rows(rows)
    expr_ret5 = fe.compute_ts("delta(close,5)/delay(close,5)", s)
    stored = [r["ret5"] for r in rows]
    max_diff = 0.0
    n = 0
    for x, y in zip(expr_ret5, stored):
        if fe._isnum(x) and fe._isnum(y):
            n += 1
            max_diff = max(max_diff, abs(x - y))
    return max_diff, n


# ---------------- 前向 RankIC ----------------
def symbol_factor_ic(expr, series, close, horizons=HORIZONS):
    """单品种：因子序列对未来 H 日收益的 Spearman；返回 {H:(ic,n)}。"""
    fac = fe.compute_ts(expr, series)
    out = {}
    for h in horizons:
        xs, ys = [], []
        for t in range(len(fac)):
            if not fe._isnum(fac[t]):
                continue
            fwd = forward_return(close, t, h)
            if fe._isnum(fwd):
                xs.append(fac[t]); ys.append(fwd)
        out[h] = (fe.spearman(xs, ys), len(xs))
    return out


def run(db_path=DEFAULT_DB, txt_path=DEFAULT_TXT, json_path=DEFAULT_JSON, verbose=True):
    store = pb.PanelStore(db_path)
    syms = sorted(store.symbols())
    lib = fe.LIBRARY
    # 累加器
    glob_parity_diff = 0.0
    glob_parity_pts = 0
    glob_parity_mis = 0
    glob_xcheck_diff = 0.0
    glob_xcheck_n = 0
    # per factor per horizon: 逐品种 ic 列表 + 池化 (x,y)
    per = {f["key"]: {h: {"ics": [], "px": [], "py": []} for h in HORIZONS} for f in lib}
    last_cs = {f["key"]: {} for f in lib}     # 最近交易日截面值
    for sym in syms:
        rows = store.load_rows(sym)
        if not rows:
            continue
        d, np_, mis = parity_for_rows(rows)
        glob_parity_diff = max(glob_parity_diff, d)
        glob_parity_pts += np_
        glob_parity_mis += mis
        xd, xn = indicator_crosscheck(rows)
        glob_xcheck_diff = max(glob_xcheck_diff, xd)
        glob_xcheck_n += xn
        series = series_from_rows(rows)
        close = series["close"]
        for f in lib:
            fac = fe.compute_ts(f["expr"], series)
            for h in HORIZONS:
                xs, ys = [], []
                for t in range(len(fac)):
                    fwd = forward_return(close, t, h)
                    if fe._isnum(fac[t]) and fe._isnum(fwd):
                        xs.append(fac[t]); ys.append(fwd)
                ic = fe.spearman(xs, ys)
                if fe._isnum(ic):
                    per[f["key"]][h]["ics"].append(ic)
                per[f["key"]][h]["px"].extend(xs)
                per[f["key"]][h]["py"].extend(ys)
            # 最近一个有限值做截面
            for t in range(len(fac) - 1, -1, -1):
                if fe._isnum(fac[t]):
                    last_cs[f["key"]][sym] = fac[t]
                    break

    def mean(v):
        return sum(v) / len(v) if v else None

    summary = {}
    for f in lib:
        summary[f["key"]] = {"name": f["name"], "direction": f["direction"], "expr": f["expr"], "h": {}}
        for h in HORIZONS:
            bucket = per[f["key"]][h]
            pooled = fe.spearman(bucket["px"], bucket["py"])
            summary[f["key"]]["h"][h] = {
                "mean_ic": mean(bucket["ics"]), "pooled_ic": pooled,
                "n_sym": len(bucket["ics"]), "n_pair": len(bucket["px"])}
    # 截面 cross_rank 演示（短长均线比）最近日
    cs_rank_demo = fe.eval_cs("cross_rank(m)", {"m": last_cs["expr_ma_ratio"]})
    finite_cs = sorted(((s, v) for s, v in cs_rank_demo.items() if fe._isnum(v)), key=lambda kv: kv[1])
    result = {
        "n_symbols": len(syms), "horizons": list(HORIZONS),
        "parity": {"max_abs_diff": glob_parity_diff, "points": glob_parity_pts, "mismatch": glob_parity_mis},
        "indicator_crosscheck_ret5": {"max_abs_diff": glob_xcheck_diff, "points": glob_xcheck_n},
        "factors": summary,
        "cs_demo_ma_ratio_bottom": finite_cs[:3], "cs_demo_ma_ratio_top": finite_cs[-3:],
    }

    lines = []
    lines.append("=" * 92)
    lines.append("G25 表达式因子研究台 expr_research（纯离线读 G21 面板，research 因子不进综合分）")
    lines.append("品种数=%d；面板路径 cache/research_panel.db；引擎=factor_expr（白名单DSL，无eval）" % len(syms))
    lines.append("-" * 92)
    lines.append("[training-serving parity]")
    lines.append("  面板列直读 vs bar回读 同表达式逐值：maxAbsDiff=%.3e，有限点=%d，不一致=%d（须0）"
                 % (glob_parity_diff, glob_parity_pts, glob_parity_mis))
    lines.append("  表达式5日动量 vs 实时管线落库 ret5：maxAbsDiff=%.3e，比对点=%d（须≈0）"
                 % (glob_xcheck_diff, glob_xcheck_n))
    lines.append("-" * 92)
    lines.append("[表达式因子 前向 RankIC]（严格未来收益；meanIC=逐品种IC均值，pooledIC=全样本池化）")
    lines.append("  %-18s %-8s | %-22s | %-22s | %-22s" % ("key", "方向", "H=1", "H=5", "H=20"))
    for f in lib:
        cells = []
        for h in HORIZONS:
            r = summary[f["key"]]["h"][h]
            mi = r["mean_ic"]; pi = r["pooled_ic"]
            cells.append("mean%+.3f/pool%+.3f/n%d" % (
                mi if fe._isnum(mi) else float("nan"),
                pi if fe._isnum(pi) else float("nan"), r["n_pair"]))
        lines.append("  %-18s %+d      | %-22s | %-22s | %-22s" %
                     (f["key"], f["direction"], cells[0], cells[1], cells[2]))
    lines.append("-" * 92)
    if finite_cs:
        lines.append("[截面 cross_rank 演示·短长均线比 最近交易日] 最低3: %s；最高3: %s"
                     % (finite_cs[:3], finite_cs[-3:]))
    lines.append("注：|IC|<0.05 视为无稳定预测力；research 因子即便IC为正也须双样本+G29体检才谈影子，默认不进分。")
    text = "\n".join(lines)
    if verbose:
        print(text)
    os.makedirs(os.path.dirname(txt_path), exist_ok=True)
    with open(txt_path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(text + "\n")
    with open(json_path, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(result, fp, ensure_ascii=False, allow_nan=False, indent=1)
    return result


# ---------------- 零网络/零DB 合成断言 ----------------
def _mk_rows(closes, vols=None, sym="T"):
    """造最小面板行（含 panel_rows_to_bars 所需键 + ret5 同口径手算）。"""
    rows = []
    for i, c in enumerate(closes):
        ret5 = c / closes[i - 5] - 1.0 if i >= 5 else 0.0
        rows.append({"sym": sym, "date": "2026-01-%02d" % (i + 1), "sector": "测试",
                     "o": c, "h": c, "l": c, "c": c,
                     "v": (vols[i] if vols else 1000.0 + i), "oi": 500.0, "ret5": ret5})
    return rows


def selftest():
    # 1) 面板列 vs bar回读 parity=0（含嵌套/除法/volume 字段）
    closes = [100.0 + i * 0.5 + (i % 3) * 0.2 for i in range(60)]
    rows = _mk_rows(closes)
    d, npts, mis = parity_for_rows(rows)
    assert d == 0.0 and mis == 0 and npts > 0, (d, npts, mis)
    # 2) 表达式动量 == 手算 ret5（=实时管线口径）
    xd, xn = indicator_crosscheck(rows)
    assert xd < 1e-12 and xn == len(rows) - 5, (xd, xn)
    # 3) 前向收益严格向未来：加速序列 close=t² 的 delta 递增、未来1日收益(2/t+1/t²)递减 → 秩相关 -1
    acc = [float(t * t) for t in range(1, 41)]
    s = {"close": acc, "volume": [1000.0] * 40}
    fac_inc = fe.compute_ts("delta(close,1)", s)
    xs, ys = [], []
    for t in range(len(fac_inc)):
        fwd = forward_return(acc, t, 1)
        if fe._isnum(fac_inc[t]) and fe._isnum(fwd):
            xs.append(fac_inc[t]); ys.append(fwd)
    assert abs(fe.spearman(xs, ys) + 1.0) < 1e-12, fe.spearman(xs, ys)
    assert forward_return(acc, len(acc) - 1, 1) is None  # 末端无未来
    lin = [float(v) for v in range(1, 41)]
    ic1 = symbol_factor_ic("close", {"close": lin, "volume": [1.0] * 40}, lin, horizons=(1,))[1]
    assert fe._isnum(ic1[0])
    # 4) 截面 cross_rank 在合成多品种上单调
    multi = {f["key"]: {} for f in fe.LIBRARY}
    rows_by = {("S%d" % k2): _mk_rows([10.0 + k2 + 0.1 * i for i in range(30)], sym="S%d" % k2)
               for k2 in range(5)}
    for sym, rr in rows_by.items():
        ss = series_from_rows(rr)
        val = fe.compute_ts("ts_mean(close,5)/ts_mean(close,20)-1", ss)
        multi_last = next(v for v in reversed(val) if fe._isnum(v))
        multi["expr_ma_ratio"][sym] = multi_last
    cr = fe.eval_cs("cross_rank(m)", {"m": multi["expr_ma_ratio"]})
    vals = [v for v in cr.values() if fe._isnum(v)]
    assert min(vals) == 0.0 and max(vals) == 1.0
    # 5) 治理：正交+IC加权合成在研究台可直接用（残差正交于基）
    base1 = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    base2 = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0]
    target = [2 * a - b for a, b in zip(base1, base2)]
    resid, beta = fe.orthogonalize(target, [base1, base2])
    assert abs(beta[0] - 2.0) < 1e-9 and abs(beta[1] + 1.0) < 1e-9
    assert all(abs(r) < 1e-9 for r in resid)
    print("expr_research selftest ALL PASS（面板/bar同表达式parity=0、表达式动量==实时ret5、"
          "前向收益严格向未来且秩相关方向正确、多品种cross_rank、OLS正交恢复 共5组）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="G25 表达式因子研究台（纯离线读面板）")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    run(db_path=args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
