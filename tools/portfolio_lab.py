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


def apply_mask_to_returns(return_map, mask):
    """把可交易性掩码应用到收益映射：不可交易日（锁板/临近交割）的 ret1d 置为缺失。

    mask: tradable_mask.mask_for_panel 输出 {sym: {date: {"tradable":bool}}}。
    返回新的 return_map（浅拷贝品种字典，不可交易日 pop 掉），纯函数不改入参。
    """
    out = {}
    for sym, dm in return_map.items():
        m = mask.get(sym)
        d = dict(dm)
        if m:
            for dt in list(d.keys()):
                e = m.get(dt)
                if e is not None and not e.get("tradable", True):
                    d.pop(dt, None)
        out[sym] = d
    return out


def dense_matrix(return_map, analysis_days=ANALYSIS_DAYS, coverage_min=COVERAGE_MIN, fill_missing=False):
    """选固定宇宙（分析窗内覆盖率达标）并对齐成 dates×symbols 稠密矩阵，返回 (dates, syms, matrix[t][i])。
    fill_missing=False（默认）：覆盖率不足的品种剔除、任一品种缺值的整日剔除（稠密，旧行为逐字节一致）。
    fill_missing=True：保留全部品种（"全64"口径），不剔除缺值日，缺失 ret1d 按 0.0 补（=当日该品种无敞口、
    无收益贡献，会略低估其波动，仅用于覆盖率稳健性对照，主结论仍以稠密固定宇宙为准）。"""
    all_dates = sorted(set().union(*[set(d.keys()) for d in return_map.values()]))[-analysis_days:]
    dset = set(all_dates)
    if fill_missing:
        syms = sorted(return_map)
        mat = [[(return_map[s].get(dt) or 0.0) for s in syms] for dt in all_dates]
        return list(all_dates), syms, mat
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
    out = {m: {"daily": [], "idx": [], "turnover": [], "eff_n": [], "ann_vol_at_rebal": [],
               "seg_bounds": []} for m in methods}
    prev_w = {m: None for m in methods}
    t = lookback
    while t < T:
        train = _series_by_asset(mat, t - lookback, t)
        hold_end = min(t + rebal, T)
        ws, seg_tv = {}, {}
        for m in methods:
            r = pc.construct(train, m, shrink=shrink, cap=cap)
            w = r["weights"]
            ws[m] = w
            tv = pc.turnover(w, prev_w[m]) if prev_w[m] is not None else None
            if tv is not None:
                out[m]["turnover"].append(tv)
            seg_tv[m] = tv
            out[m]["eff_n"].append(r["eff_n"])
            out[m]["ann_vol_at_rebal"].append(r["ann_vol"])
            prev_w[m] = w
        for m in methods:
            out[m]["seg_bounds"].append({"start": t, "length": hold_end - t,
                                         "entry_turnover": seg_tv[m]})
        for tt in range(t, hold_end):
            for m in methods:
                pr = sum(ws[m][i] * mat[tt][i] for i in range(len(ws[m])))
                out[m]["daily"].append(pr)
                out[m]["idx"].append(tt)   # 全局 mat 行号，供与 dates 对齐画组合历史净值
        t = hold_end
    return out


# 第52轮 G26续二：总敞口 gross 网格 × 换手成本（线性多头杠杆；默认 gross=1/零成本与原日收益逐点一致）
DEFAULT_GROSS_GRID = (1.0, 1.2, 1.5)
DEFAULT_ONEWAY_COST = 1.5e-4     # 单边调仓成本率（费5bp+滑点10bp 的数量级，与 wf_cost_lab 基准同档，可参数化）


def gross_net_daily(daily, seg_bounds, gross=1.0, one_way_cost=0.0):
    """把一段组合日收益按总敞口 gross 线性放大，并在每个再平衡段首日扣换手成本。
    成本=段首日单边换手 × gross × one_way_cost（杠杆下成交名义同步放大）。
    返回 (net_daily, charge_daily)，与 daily 等长；gross=1 且 cost=0 时 net 与入参逐点相同。纯函数不改入参。"""
    gross = float(gross)
    # daily 按再平衡段顺序拼接：先整体按 gross 线性放大，再把每段首日换手成本落到该段在 daily 内的首点
    net = [gross * r for r in daily]
    charges = [0.0] * len(daily)
    pos = 0
    for seg in seg_bounds:
        tv = seg.get("entry_turnover")
        if tv is not None and pos < len(net):
            c = gross * float(tv) * float(one_way_cost)   # 杠杆下成交名义同步放大
            charges[pos] = c
            net[pos] -= c
        pos += seg["length"]
    return net, charges


def gross_cost_grid(proxy, gross_list=DEFAULT_GROSS_GRID, one_way_cost=DEFAULT_ONEWAY_COST,
                    methods=None, periods_per_year=None):
    """对每方法×总敞口 gross 计算毛/净绩效与换手成本拖累。返回 {method: [按 gross 的结果 dict]}。
    净=毛日收益按 gross 放大后、在再平衡段首日扣换手成本；同时给零成本毛口径做对照。"""
    methods = methods or config.PC_METHODS
    ppy = periods_per_year or config.PC_PERIODS_PER_YEAR
    grid = {}
    for m in methods:
        daily = proxy[m]["daily"]; bounds = proxy[m]["seg_bounds"]
        rows = []
        for g in gross_list:
            gross_net, charges = gross_net_daily(daily, bounds, gross=g, one_way_cost=one_way_cost)
            gross_only, _ = gross_net_daily(daily, bounds, gross=g, one_way_cost=0.0)
            st_net = perf_stats(gross_net, periods_per_year=ppy)
            st_gross = perf_stats(gross_only, periods_per_year=ppy)
            total_charge = sum(charges)
            n = len(gross_net)
            ann_charge = (total_charge / n * ppy) if n else 0.0
            nav = nav_curve(gross_net)
            rows.append({"gross": float(g), "one_way_cost": float(one_way_cost),
                         "ann_ret_net": st_net.get("ann_ret"), "ann_vol_net": st_net.get("ann_vol"),
                         "sharpe_net": st_net.get("sharpe"), "maxdd_net": st_net.get("maxdd"),
                         "ann_ret_gross": st_gross.get("ann_ret"), "sharpe_gross": st_gross.get("sharpe"),
                         "maxdd_gross": st_gross.get("maxdd"), "ann_cost_drag": ann_charge,
                         "end_nav_net": nav[-1] if nav else None})
        grid[m] = rows
    return grid


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


def run(db_path=DEFAULT_DB, txt_path=None, json_path=None, verbose=True, mask=False):
    txt_path = txt_path or config.PC_FILE
    json_path = json_path or config.PC_JSON
    return_map, sectors, all_syms = load_return_map(db_path)
    # G22续（第71轮）：可交易性掩码剔除（--mask；只读 research_panel.db，零网络）
    mask_notes = ""
    if mask:
        try:
            import tradable_mask as tmask
            from collections import defaultdict as _dd
            db_p = str(os.path.join(_ROOT, "cache", "research_panel.db"))
            if os.path.exists(db_p):
                import sqlite3 as _sq
                con = _sq.connect(db_p)
                rows_by_date = _dd(dict)
                for row in con.execute("SELECT sym,date,c,h,l FROM research_panel ORDER BY sym,date"):
                    rows_by_date[row[1]][row[0]] = {"c": row[2], "h": row[3], "l": row[4]}
                con.close()
                mask = tmask.mask_for_panel(rows_by_date)
                removed = sum(1 for sym, dm in return_map.items()
                              for dt in dm if not (mask.get(sym) or {}).get(dt, {}).get("tradable", True))
                return_map = apply_mask_to_returns(return_map, mask)
                mask_notes = "；G22续掩码：剔不可交易日点%d（锁板/距交割月1号≤15天）" % removed
            else:
                mask_notes = "；G22续掩码：research_panel.db 不存在，跳过"
        except Exception as e:
            mask_notes = "；G22续掩码失败（不影响主流程）: %s" % type(e).__name__
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
    # 第52轮 G26续二：总敞口 gross(1.0/1.2/1.5) × 换手成本网格（固定宇宙，复用上面同一权重轨迹 proxy）
    gross_list = list(DEFAULT_GROSS_GRID)
    gross_grid = gross_cost_grid(proxy, gross_list, DEFAULT_ONEWAY_COST)
    # 全64口径（缺失日补0）对照：与稠密主宇宙同口径跑满三档 gross×换手成本，看放宽覆盖率后结论是否稳健
    dates_all, syms_all, mat_all = dense_matrix(return_map, fill_missing=True)
    proxy_all = rolling_proxy(mat_all)
    gross_grid_all = gross_cost_grid(proxy_all, gross_list, DEFAULT_ONEWAY_COST)

    L = []
    if mask_notes:
        L.append(mask_notes.lstrip("；"))
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
    # 【三】第52轮 G26续二：总敞口 gross 网格 × 换手成本
    L.append("【三】总敞口 gross 网格 × 换手成本影子（固定宇宙%d品种；同一权重轨迹按总敞口线性放大，"
             "再平衡段首日扣单边换手成本，单边费率%.1fbp；多头线性、不预测收益）"
             % (len(syms), DEFAULT_ONEWAY_COST * 1e4))
    L.append("  %-8s %5s %9s %9s %8s %9s %9s %9s %10s" %
             ("方法", "gross", "年化净", "年化毛", "波动净", "夏普净", "夏普毛", "回撤净", "年成本拖累"))
    for m in config.PC_METHODS:
        for cell in gross_grid[m]:
            L.append("  %-8s %4.1fx %+8.2f%% %+8.2f%% %7.2f%% %8.2f %8.2f %8.2f%% %9.2f%%"
                     % (name[m], cell["gross"], (cell["ann_ret_net"] or 0) * 100,
                        (cell["ann_ret_gross"] or 0) * 100, (cell["ann_vol_net"] or 0) * 100,
                        cell["sharpe_net"] or 0, cell["sharpe_gross"] or 0,
                        (cell["maxdd_net"] or 0) * 100, cell["ann_cost_drag"] * 100))
    L.append("  ─ 全%d品种口径（缺失日收益按0=当日无敞口，覆盖率稳健性对照）列同上、单边%.1fbp，三档 gross 全跑："
             % (len(syms_all), DEFAULT_ONEWAY_COST * 1e4))
    for m in config.PC_METHODS:
        for cell in gross_grid_all[m]:
            L.append("  %-8s %4.1fx %+8.2f%% %+8.2f%% %7.2f%% %8.2f %8.2f %8.2f%% %9.2f%%"
                     % (name[m], cell["gross"], (cell["ann_ret_net"] or 0) * 100,
                        (cell["ann_ret_gross"] or 0) * 100, (cell["ann_vol_net"] or 0) * 100,
                        cell["sharpe_net"] or 0, cell["sharpe_gross"] or 0,
                        (cell["maxdd_net"] or 0) * 100, cell["ann_cost_drag"] * 100))
    L.append("  逐日组合净值已含四方法 gross=1 口径（reports/portfolio_nav.csv）；本网格明细落 reports/portfolio_gross_grid.csv。")
    L.append("  读法：gross 等比放大收益与波动，夏普(毛)理论上不变、净夏普随换手成本上升而下降；回撤随 gross 线性放大，杠杆只改风险预算不产生 alpha。")
    L.append("-" * 104)
    L.append("诚实边界：日收益来自已比例复权主连面板、固定宇宙有幸存者偏差、代理未计手续费/滑点/保证金与换月、"
             "协方差用历史窗不代表未来；本结果仅决定'风险型分配是否值得后续以默认equal接入paper/backtest对照'，不直接上线。"
             "gross 网格为线性多头杠杆近似（收益/波动/回撤等比放大），未计保证金占用/强平线/融资成本与杠杆下的换手冲击，仅作风险预算影子对照。")
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
    # 第52轮 G26续二：gross×换手成本网格 CSV（长表 method,gross,...）
    gross_path = os.path.join(os.path.dirname(txt_path), "portfolio_gross_grid.csv")
    with open(gross_path, "w", encoding="utf-8", newline="") as fp:
        wg = csv.writer(fp)
        wg.writerow(["universe", "method", "gross", "one_way_cost", "ann_ret_net", "ann_ret_gross",
                     "ann_vol_net", "sharpe_net", "sharpe_gross", "maxdd_net", "maxdd_gross",
                     "ann_cost_drag", "end_nav_net"])

        def _write_grid(tag, grid):
            for m in config.PC_METHODS:
                for c in grid[m]:
                    wg.writerow([tag, m, c["gross"], c["one_way_cost"], c["ann_ret_net"], c["ann_ret_gross"],
                                 c["ann_vol_net"], c["sharpe_net"], c["sharpe_gross"], c["maxdd_net"],
                                 c["maxdd_gross"], c["ann_cost_drag"], c["end_nav_net"]])

        _write_grid("fixed%d" % len(syms), gross_grid)        # 稠密固定宇宙（主口径）
        _write_grid("all%d" % len(syms_all), gross_grid_all)  # 全品种口径（缺失补0，覆盖率对照）
    payload = {"universe": syms, "n_universe": len(syms), "dates": [dates[0], dates[-1]],
               "n_days": len(mat), "rolling_stats": stats, "nav_summary": nav_summary,
               "nav_csv": os.path.basename(nav_path), "snapshot": snap,
               "gross_grid": gross_grid, "gross_one_way_cost": DEFAULT_ONEWAY_COST,
               "gross_grid_csv": os.path.basename(gross_path),
               "all_universe": {"n": len(syms_all), "dates": [dates_all[0], dates_all[-1]],
                                "gross_grid": gross_grid_all,
                                "gross1": {m: gross_grid_all[m][0] for m in config.PC_METHODS}}}
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
             "methods": list(config.PC_METHODS), "panel_db": os.path.basename(db_path),
             "gross_grid": list(gross_list), "one_way_cost": DEFAULT_ONEWAY_COST,
             "all_universe_n": len(syms_all)},
            lab_metrics,
            inputs=[db_path], artifacts=[txt_path, json_path, nav_path, gross_path],
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
    # 第52轮 G26续二：fill_missing=True 保留全部品种（含稀疏 S4）、缺失补0、日期不剔除
    d3, sy3, mat3 = dense_matrix(rm2, analysis_days=260, fill_missing=True)
    assert "S4" in sy3 and len(sy3) == 5 and len(mat3) == 260 and len(mat3[0]) == 5
    # gross_net_daily 手算：成本只落在每段首日；gross=1零成本逐点恒等
    daily = [0.01, -0.02, 0.0, 0.03]
    bounds = [{"start": 60, "length": 2, "entry_turnover": 0.5},
              {"start": 62, "length": 2, "entry_turnover": 0.2}]
    n0, c0 = gross_net_daily(daily, bounds, gross=1.0, one_way_cost=0.0)
    assert all(abs(a - b) < 1e-15 for a, b in zip(n0, daily)) and sum(c0) == 0
    n2, c2 = gross_net_daily(daily, bounds, gross=2.0, one_way_cost=1e-3)
    assert abs(c2[0] - 1e-3) < 1e-15 and c2[1] == 0.0 and abs(c2[2] - 4e-4) < 1e-15 and c2[3] == 0.0
    assert abs(n2[0] - (0.02 - 1e-3)) < 1e-15 and abs(n2[1] + 0.04) < 1e-15
    assert abs(n2[2] - (0.0 - 4e-4)) < 1e-15 and abs(n2[3] - 0.06) < 1e-15
    # 首段无 prev（entry_turnover=None）不收成本
    b_first = [{"start": 60, "length": 2, "entry_turnover": None}] + bounds
    nf, cf = gross_net_daily([0.01, 0.01], b_first[:1], 1.0, 1e-3)
    assert sum(cf) == 0
    # gross_cost_grid：每方法三档 gross、gross 越大波动越大、净夏普≤毛夏普（成本只减不增）、结构键齐
    grid = gross_cost_grid(proxy, (1.0, 1.2, 1.5), 1.5e-4)
    for m in proxy:
        rows = grid[m]
        assert [r["gross"] for r in rows] == [1.0, 1.2, 1.5]
        assert rows[2]["ann_vol_net"] >= rows[0]["ann_vol_net"]
        for r in rows:
            assert r["sharpe_net"] <= r["sharpe_gross"] + 1e-9 and r["ann_cost_drag"] >= 0
    # 续二：全品种口径（fill_missing，含稀疏 S4）同样跑满三档 gross×成本，结构/单调/净≤毛与稠密一致
    proxy_a = rolling_proxy(mat3, lookback=60, rebal=20, shrink=0.1, cap=0.5)
    grid_a = gross_cost_grid(proxy_a, (1.0, 1.2, 1.5), 1.5e-4)
    assert set(grid_a) == set(proxy_a) and len(sy3) == 5
    for m in proxy_a:
        rows_a = grid_a[m]
        assert [r["gross"] for r in rows_a] == [1.0, 1.2, 1.5]
        assert rows_a[2]["ann_vol_net"] >= rows_a[0]["ann_vol_net"]
        for r in rows_a:
            assert r["sharpe_net"] <= r["sharpe_gross"] + 1e-9 and r["ann_cost_drag"] >= 0
    # 第71轮 G22续：apply_mask_to_returns 剔除不可交易日（锁板/临近交割）后收益映射变小、不改入参
    rm3 = {k: dict(v) for k, v in rm.items()}
    mask = {"S0": {dt: {"tradable": dt != "2025-260"} for dt in rm["S0"]},
            "S1": {dt: {"tradable": True} for dt in rm["S1"]}}
    out = apply_mask_to_returns(rm3, mask)
    assert "S0" in out and "2025-260" not in out["S0"] and "2025-260" in rm3["S0"]  # 不改入参
    assert "2025-260" in out["S1"] and len(out["S1"]) == len(rm3["S1"])
    assert len(out["S0"]) == len(rm3["S0"]) - 1
    print("portfolio_lab selftest ALL PASS（稠密面板对齐/固定宇宙覆盖率筛选/滚动样本外无未来且GMV波动≤等权/"
          "快照四方法合法/短序列安全/净值曲线复利与idx日期对齐/回撤窗口手算/fill_missing全品种/"
          "gross放大与段首日换手成本手算/gross网格单调/全品种三档网格/掩码剔除 共13组）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="G26 组合构建实验台（纯离线读面板）")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--mask", action="store_true", help="G22续：读 research_panel.db 算可交易性掩码并剔除不可交易日后重做组合实验")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    run(db_path=args.db, mask=getattr(args, "mask", False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
