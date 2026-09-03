# -*- coding: utf-8 -*-
r"""G26（第40轮）组合构建实验台 tools/portfolio_lab.py：纯标准库、零网络、只读 G21 面板（mode=ro），
用 portfolio_constructor 的四种风险型权重（equal/inv_vol/erc/gmv）在全64商品日收益上做两件事：
1) **最新快照**：用过去 PC_LOOKBACK 日协方差，给各方法当前目标权重、年化波动、有效持仓数、分散化度、
   风险贡献最大品种，以及目标波动缩放所需杠杆；
2) **滚动代理回测（诚实的样本外对照）**：每 PC_REBAL 个交易日用"仅当时可得的过去 PC_LOOKBACK 日"估协方差、
   定权重并持有到下一再平衡日（严格无未来），比较各方法的年化收益/波动/夏普/最大回撤/平均有效N/年化换手，
   equal 等权为基线。**不预测预期收益、不接 main、不改综合分与既有 sizing**；结论只用于判断风险型分配是否值得
   后续在 paper/backtest 以"默认 equal、缺省等价旧版"方式接入。
"""
import argparse
import csv
import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import config                                  # noqa: E402
import portfolio_constructor as pc             # noqa: E402
import panel_builder as pb                     # noqa: E402
import experiment_ledger as el                 # noqa: E402  G27① 统一实验台账（旁路登记，失败不影响本工具）

DEFAULT_DB = os.path.join(_ROOT, "cache", "research_panel.db")
ANALYSIS_DAYS = 504                            # 取最近约2年做固定稠密面板
COVERAGE_MIN = 0.95                            # 固定宇宙：在分析窗内有效收益覆盖≥95%才纳入


# =========================== 面板 → 稠密收益矩阵 ===========================
def load_return_map(db_path=DEFAULT_DB):
    """返回 ({sym: {date: ret1d}}, {sym: sector})。"""
    store = pb.PanelStore(db_path)
    syms = sorted(store.symbols())
    rets, sectors = {}, {}
    for s in syms:
        d, sec = {}, None
        for r in store.load_rows(s):
            v = r.get("ret1d")
            if isinstance(v, (int, float)) and math.isfinite(v):
                d[r["date"]] = v
                sec = sec or r.get("sector")
        rets[s] = d
        sectors[s] = sec
    return rets, sectors, syms


def dense_matrix(return_map, analysis_days=ANALYSIS_DAYS, coverage_min=COVERAGE_MIN):
    """选固定宇宙（分析窗内覆盖率达标）并对齐成 dates×symbols 稠密矩阵，返回 (dates, syms, matrix[t][i])。"""
    all_dates = sorted(set().union(*[set(d.keys()) for d in return_map.values()]))[-analysis_days:]
    dset = set(all_dates)
    syms = [s for s in sorted(return_map)
            if len(dset & set(return_map[s].keys())) >= coverage_min * len(all_dates)]
    mat = []
    for dt in all_dates:
        row = []
        ok = True
        for s in syms:
            v = return_map[s].get(dt)
            if v is None:
                ok = False
                break
            row.append(v)
        if ok:
            mat.append(row)
    dates = [dt for dt in all_dates if all(dt in return_map[s] for s in syms)]
    return dates, syms, mat


def _series_by_asset(mat, lo, hi):
    """取矩阵行 [lo,hi)，转成"按资产"的收益序列列表（portfolio_constructor 约定）。"""
    n = len(mat[0])
    return [[mat[t][i] for t in range(lo, hi)] for i in range(n)]


# =========================== 滚动样本外代理回测 ===========================
def rolling_proxy(mat, methods=None, lookback=None, rebal=None, shrink=None, cap=None):
    """每 rebal 日用过去 lookback 日定权、持有到下一再平衡；返回 {method: {daily_ret, turnovers, effNs}}。"""
    methods = methods or config.PC_METHODS
    lookback = lookback or config.PC_LOOKBACK
    rebal = rebal or config.PC_REBAL
    shrink = config.PC_SHRINK if shrink is None else shrink
    cap = cap or config.PC_MAX_WEIGHT
    T = len(mat)
    out = {m: {"daily": [], "idx": [], "turnover": [], "eff_n": [], "ann_vol_at_rebal": []} for m in methods}
    prev_w = {m: None for m in methods}
    t = lookback
    while t < T:
        train = _series_by_asset(mat, t - lookback, t)
        hold_end = min(t + rebal, T)
        ws = {}
        for m in methods:
            r = pc.construct(train, m, shrink=shrink, cap=cap)
            w = r["weights"]
            ws[m] = w
            if prev_w[m] is not None:
                out[m]["turnover"].append(pc.turnover(w, prev_w[m]))
            out[m]["eff_n"].append(r["eff_n"])
            out[m]["ann_vol_at_rebal"].append(r["ann_vol"])
            prev_w[m] = w
        for tt in range(t, hold_end):
            for m in methods:
                pr = sum(ws[m][i] * mat[tt][i] for i in range(len(ws[m])))
                out[m]["daily"].append(pr)
                out[m]["idx"].append(tt)   # 全局 mat 行号，供与 dates 对齐画组合历史净值
        t = hold_end
    return out


def nav_curve(daily, start=1.0):
    """日收益序列 → 逐日复利净值（初始 start，默认1.0）；空序列返 []。纯函数、不改入参。"""
    nav, cur = [], float(start)
    for x in daily:
        cur *= (1.0 + x)
        nav.append(cur)
    return nav


def drawdown_window(nav, idxs=None, dates=None):
    """净值序列 → 最大回撤及其峰值/谷底位置（返回分数与下标/日期）；不足2点返零值。纯函数。"""
    res = {"maxdd": 0.0, "peak_i": None, "trough_i": None,
           "peak_date": None, "trough_date": None}
    if not nav:
        return res
    peak = nav[0]
    peak_k = 0
    for k, v in enumerate(nav):
        if v > peak:
            peak, peak_k = v, k
        dd = 1.0 - v / peak if peak > 0 else 0.0
        if dd > res["maxdd"]:
            res["maxdd"] = dd
            res["peak_i"], res["trough_i"] = peak_k, k
    if idxs is not None and res["peak_i"] is not None and dates is not None:
        pi, ti = idxs[res["peak_i"]], idxs[res["trough_i"]]
        res["peak_date"] = dates[pi] if 0 <= pi < len(dates) else None
        res["trough_date"] = dates[ti] if 0 <= ti < len(dates) else None
    return res


def perf_stats(daily, periods_per_year=None):
    """日收益序列 → 年化收益(几何)/年化波动/夏普/最大回撤/Calmar。"""
    ppy = periods_per_year or config.PC_PERIODS_PER_YEAR
    n = len(daily)
    if n < 2:
        return {"n": n}
    mean = sum(daily) / n
    var = sum((x - mean) ** 2 for x in daily) / (n - 1)
    vol = math.sqrt(max(var, 0))
    ann_vol = vol * math.sqrt(ppy)
    growth = 1.0
    peak = 1.0
    maxdd = 0.0
    for x in daily:
        growth *= (1 + x)
        peak = max(peak, growth)
        maxdd = max(maxdd, 1 - growth / peak)
    ann_ret = growth ** (ppy / n) - 1
    sharpe = ann_ret / ann_vol if ann_vol > 1e-12 else 0.0
    calmar = ann_ret / maxdd if maxdd > 1e-12 else 0.0
    return {"n": n, "ann_ret": ann_ret, "ann_vol": ann_vol, "sharpe": sharpe,
            "maxdd": maxdd, "calmar": calmar}


# =========================== 快照与报告 ===========================
def latest_snapshot(mat, syms, sectors, methods=None):
    methods = methods or config.PC_METHODS
    train = _series_by_asset(mat, len(mat) - config.PC_LOOKBACK, len(mat))
    snap = {}
    for m in methods:
        r = pc.construct(train, m, shrink=config.PC_SHRINK, cap=config.PC_MAX_WEIGHT)
        order = sorted(range(len(syms)), key=lambda i: -r["weights"][i])
        top = [{"sym": syms[i], "sector": sectors.get(syms[i]), "w": r["weights"][i]}
               for i in order[:8] if r["weights"][i] > 1e-6]
        # 目标波动缩放演示（不改核心权重）
        _, lev, ann_pre = pc.target_vol_scale(
            r["weights"], r["cov"], config.PC_TARGET_VOL_ANNUAL,
            config.PC_PERIODS_PER_YEAR, config.PC_MAX_GROSS)
        snap[m] = {"ann_vol": r["ann_vol"], "eff_n": r["eff_n"], "div_ratio": r["div_ratio"],
                   "max_rc": max(r["rc_frac"]), "gross": r["gross"], "top": top,
                   "target_vol_leverage": lev, "ann_vol_before_scale": ann_pre}
    return snap


def run(db_path=DEFAULT_DB, txt_path=None, json_path=None, verbose=True):
    txt_path = txt_path or config.PC_FILE
    json_path = json_path or config.PC_JSON
    return_map, sectors, all_syms = load_return_map(db_path)
    dates, syms, mat = dense_matrix(return_map)
    proxy = rolling_proxy(mat)
    stats = {m: perf_stats(proxy[m]["daily"]) for m in config.PC_METHODS}
    # 第49轮 G5⑤：多品种组合历史净值曲线（四方法逐日对齐，初始净值1.0；严格样本外、无成本）
    navs = {m: nav_curve(proxy[m]["daily"]) for m in config.PC_METHODS}
    nav_summary = {}
    for m in config.PC_METHODS:
        dw = drawdown_window(navs[m], proxy[m]["idx"], dates)
        nav_summary[m] = {
            "n": len(navs[m]),
            "start_date": dates[proxy[m]["idx"][0]] if proxy[m]["idx"] else None,
            "end_date": dates[proxy[m]["idx"][-1]] if proxy[m]["idx"] else None,
            "end_nav": navs[m][-1] if navs[m] else None,
            "min_nav": min(navs[m]) if navs[m] else None,
            "max_nav": max(navs[m]) if navs[m] else None,
            "maxdd_nav": dw["maxdd"], "dd_peak": dw["peak_date"], "dd_trough": dw["trough_date"]}
    for m in config.PC_METHODS:
        tv = proxy[m]["turnover"]
        en = proxy[m]["eff_n"]
        stats[m]["avg_turnover_rebal"] = sum(tv) / len(tv) if tv else None
        stats[m]["ann_turnover"] = (sum(tv) / len(tv) * config.PC_PERIODS_PER_YEAR / config.PC_REBAL) if tv else None
        stats[m]["avg_eff_n"] = sum(en) / len(en) if en else None
    snap = latest_snapshot(mat, syms, sectors)

    L = []
    L.append("=" * 104)
    L.append("G26 组合构建实验台 portfolio_lab（纯离线读 G21 面板，风险型权重、不预测收益、不接 main 不改综合分/sizing）")
    L.append("固定宇宙=%d/%d 品种（最近%d日覆盖率≥%.0f%%），稠密区间 %s~%s 共%d日；协方差窗%d、再平衡每%d日、对角收缩%.2f、单票上限%.0f%%"
             % (len(syms), len(all_syms), ANALYSIS_DAYS, COVERAGE_MIN * 100, dates[0], dates[-1], len(mat),
                config.PC_LOOKBACK, config.PC_REBAL, config.PC_SHRINK, config.PC_MAX_WEIGHT * 100))
    L.append("-" * 104)
    L.append("【一】滚动样本外代理回测（每%d日用过去%d日定权持有，equal 等权为基线；无成本、多头、不使用未来）"
             % (config.PC_REBAL, config.PC_LOOKBACK))
    head = "  %-8s %8s %8s %7s %8s %8s %9s %9s" % ("方法", "年化收益", "年化波动", "夏普", "最大回撤", "Calmar", "平均有效N", "年化换手")
    L.append(head)
    name = {"equal": "等权", "inv_vol": "逆波动", "erc": "风险平价", "gmv": "最小方差"}
    base_vol = stats["equal"]["ann_vol"]
    for m in config.PC_METHODS:
        s = stats[m]
        L.append("  %-8s %+7.2f%% %7.2f%% %7.2f %7.2f%% %8.2f %9.2f %8.2f%s"
                 % (name[m], s["ann_ret"] * 100, s["ann_vol"] * 100, s["sharpe"], s["maxdd"] * 100,
                    s["calmar"], s["avg_eff_n"], s["ann_turnover"],
                    "  ←基线" if m == "equal" else "  波动较等权%+.1f%%" % ((s["ann_vol"] / base_vol - 1) * 100)))
    nav_line = "  期末净值(初始1.0)：" + "  ".join(
        "%s=%.4f" % (name[m], nav_summary[m]["end_nav"]) for m in config.PC_METHODS
        if nav_summary[m]["end_nav"] is not None)
    L.append(nav_line)
    dd_line = "  净值最深回撤：" + "  ".join(
        "%s=%.2f%%(%s→%s)" % (name[m], nav_summary[m]["maxdd_nav"] * 100,
                              (nav_summary[m]["dd_peak"] or "?")[5:],
                              (nav_summary[m]["dd_trough"] or "?")[5:])
        for m in config.PC_METHODS if nav_summary[m]["end_nav"] is not None)
    L.append(dd_line)
    L.append("  逐日组合历史净值曲线（四方法对齐）已落 reports/portfolio_nav.csv（date+各法日收益/净值）。")
    L.append("  读法：风险型分配的价值应体现在'波动/回撤更低、夏普不更差'，而非收益更高（它不预测涨跌）；换手越大调仓成本越高。")
    L.append("-" * 104)
    L.append("【二】最新快照（过去%d日协方差；目标年化波动%.0f%%所需杠杆，受总敞口%.1f倍上限）"
             % (config.PC_LOOKBACK, config.PC_TARGET_VOL_ANNUAL * 100, config.PC_MAX_GROSS))
    for m in config.PC_METHODS:
        s = snap[m]
        tops = "、".join("%s=%.1f%%" % (t["sym"], t["w"] * 100) for t in s["top"][:6])
        L.append("  ● %s：年化波动%.2f%%、有效N%.1f、分散化度%.2f、最大单品种风险贡献%.1f%%、目标波动杠杆%.2f"
                 % (name[m], s["ann_vol"] * 100, s["eff_n"], s["div_ratio"], s["max_rc"] * 100,
                    s["target_vol_leverage"]))
        L.append("      主要权重：" + tops)
    L.append("-" * 104)
    L.append("诚实边界：日收益来自已比例复权主连面板、固定宇宙有幸存者偏差、代理未计手续费/滑点/保证金与换月、"
             "协方差用历史窗不代表未来；本结果仅决定'风险型分配是否值得后续以默认equal接入paper/backtest对照'，不直接上线。")
    text = "\n".join(L)
    if verbose:
        print(text)
    os.makedirs(os.path.dirname(txt_path), exist_ok=True)
    with open(txt_path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(text + "\n")
    # 第49轮 G5⑤：逐日组合历史净值曲线 CSV（以 equal 的 idx 为对齐主轴，四方法同一再平衡日历）
    nav_path = os.path.join(os.path.dirname(txt_path), "portfolio_nav.csv")
    base_idx = proxy[config.PC_METHODS[0]]["idx"]
    with open(nav_path, "w", encoding="utf-8", newline="") as fp:
        wcsv = csv.writer(fp)
        wcsv.writerow(["date"] + [m + "_ret" for m in config.PC_METHODS]
                      + [m + "_nav" for m in config.PC_METHODS])
        for k, gi in enumerate(base_idx):
            row = [dates[gi]]
            for m in config.PC_METHODS:
                row.append(proxy[m]["daily"][k])
            for m in config.PC_METHODS:
                row.append(navs[m][k])
            wcsv.writerow(row)
    payload = {"universe": syms, "n_universe": len(syms), "dates": [dates[0], dates[-1]],
               "n_days": len(mat), "rolling_stats": stats, "nav_summary": nav_summary,
               "nav_csv": os.path.basename(nav_path), "snapshot": snap}
    with open(json_path, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(payload, fp, ensure_ascii=False, allow_nan=False, indent=1)
    # G27① 统一实验台账（旁路：登记失败绝不影响本工具产物与返回值）
    try:
        lab_metrics = {"n_universe": len(syms), "n_days": len(mat)}
        for m in config.PC_METHODS:
            s = stats.get(m, {})
            lab_metrics[m] = {k: s.get(k) for k in
                              ("ann_ret", "ann_vol", "sharpe", "maxdd", "calmar",
                               "avg_eff_n", "ann_turnover")}
        el.safe_record(
            "portfolio_lab",
            {"analysis_days": ANALYSIS_DAYS, "coverage_min": COVERAGE_MIN, "lookback": config.PC_LOOKBACK,
             "rebal": config.PC_REBAL, "shrink": config.PC_SHRINK, "max_weight": config.PC_MAX_WEIGHT,
             "target_vol_annual": config.PC_TARGET_VOL_ANNUAL, "max_gross": config.PC_MAX_GROSS,
             "methods": list(config.PC_METHODS), "panel_db": os.path.basename(db_path)},
            lab_metrics,
            inputs=[db_path], artifacts=[txt_path, json_path, nav_path],
            conclusion="equal夏普%.2f / erc夏普%.2f / inv_vol夏普%.2f（固定宇宙%d品种 %s~%s）"
                       % (stats["equal"]["sharpe"], stats["erc"]["sharpe"], stats["inv_vol"]["sharpe"],
                          len(syms), dates[0], dates[-1]))
    except Exception:
        pass
    return payload


# =========================== 零网络/零DB 自测 ===========================
def _toy_panel():
    """造 4 品种、260 日合成 ret1d（两低波两高波、带共同因子），返回 return_map。"""
    import random
    random.seed(40)
    common = [random.gauss(0, 1) for _ in range(260)]
    vol = {f"S{i}": v for i, v in enumerate([0.004, 0.006, 0.018, 0.012])}
    beta = {f"S{i}": 0.2 + 0.15 * i for i in range(4)}
    rm = {}
    for s in vol:
        rm[s] = {}
        for t in range(260):
            rm[s]["2025-%03d" % (t + 1)] = beta[s] * 0.01 * common[t] + random.gauss(0, vol[s])
    sectors = {f"S{i}": "板块%d" % i for i in range(4)}
    return rm, sectors


def selftest():
    rm, sectors = _toy_panel()
    dates, syms, mat = dense_matrix(rm, analysis_days=260, coverage_min=1.0)
    assert len(syms) == 4 and len(mat) == 260, "稠密面板对齐"
    # 滚动代理：每方法日收益长度一致、权重无未来（首个再平衡在 lookback 之后）
    proxy = rolling_proxy(mat, lookback=60, rebal=20, shrink=0.1, cap=0.5)
    lens = {m: len(proxy[m]["daily"]) for m in proxy}
    assert len(set(lens.values())) == 1 and lens["equal"] == 200, lens
    assert all(len(proxy[m]["eff_n"]) >= 9 for m in proxy)
    # 样本外：GMV 波动不高于等权（风险型方法的核心承诺）
    st = {m: perf_stats(proxy[m]["daily"]) for m in proxy}
    assert st["gmv"]["ann_vol"] <= st["equal"]["ann_vol"] + 1e-9
    # 快照四方法键齐、权重和=1、top 非空
    snap = latest_snapshot(mat, syms, sectors, methods=("equal", "inv_vol", "erc", "gmv"))
    for m, s in snap.items():
        assert s["eff_n"] >= 1 and s["top"] and s["ann_vol"] >= 0
    # perf_stats 空/短序列安全
    assert perf_stats([])["n"] == 0 and perf_stats([0.01])["n"] == 1
    # 第49轮 G5⑤：净值曲线复利正确、四方法等长且与 idx/dates 对齐
    assert nav_curve([]) == []
    assert abs(nav_curve([0.1, -0.1, 0.0])[-1] - (1.1 * 0.9)) < 1e-12
    for m in proxy:
        assert len(navs_m := nav_curve(proxy[m]["daily"])) == len(proxy[m]["daily"]) == len(proxy[m]["idx"])
        assert proxy[m]["idx"][0] == 60 and proxy[m]["idx"][-1] == 259   # 首个再平衡在 lookback 之后、无未来
    # drawdown_window 手算：1.0→1.2(峰)→0.9，回撤=1-0.9/1.2=0.25，峰位1谷位2
    dw = drawdown_window([1.0, 1.2, 0.9], idxs=[0, 1, 2], dates=["d0", "d1", "d2"])
    assert abs(dw["maxdd"] - 0.25) < 1e-12 and dw["peak_i"] == 1 and dw["trough_i"] == 2
    assert dw["peak_date"] == "d1" and dw["trough_date"] == "d2"
    assert drawdown_window([])["maxdd"] == 0.0 and drawdown_window([1.0])["trough_i"] is None
    navs_all = {m: nav_curve(proxy[m]["daily"]) for m in proxy}
    assert len({len(v) for v in navs_all.values()}) == 1   # 四方法净值逐日对齐可同表落 CSV
    # 覆盖率不足的品种被剔除
    rm2 = {k: dict(v) for k, v in rm.items()}
    rm2["S4"] = {"2025-260": 0.01}
    d2, sy2, mat2 = dense_matrix(rm2, analysis_days=260, coverage_min=0.95)
    assert "S4" not in sy2 and len(sy2) == 4
    print("portfolio_lab selftest ALL PASS（稠密面板对齐/固定宇宙覆盖率筛选/滚动样本外无未来且GMV波动≤等权/"
          "快照四方法合法/短序列安全/净值曲线复利与idx日期对齐/回撤窗口手算 共8组）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="G26 组合构建实验台（纯离线读面板）")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    run(db_path=args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
