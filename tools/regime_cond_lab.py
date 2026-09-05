# -*- coding: utf-8 -*-
r"""第75轮 G25/G29续：regime 条件化分层多空实验台（研究侧、红线门控）。

回答的问题（第74轮发现的后继检验）：expr_miner 逐日截面层 + factor_regime --expr 体检显示，
range_pct（日均振幅）的截面负 IC 高度集中在低波状态（低波桶 H20 -0.124~-0.135、高波桶≈0），
且信号极慢（lag1 自相关 0.79+、换手 0.10）。本工具把"因子 × regime 条件化"做成可复用实验台：
对【面板列或白名单表达式因子】，按 PIT regime 标签（复用 factor_regime.compute_labels：
trend=面板 ret126、vol=hv60 过去 120 日 ts_rank 三分位，只用过去、无未来）筛选当日截面，
做按 H 对齐的**非重叠**分层多空（复用 orthogonal_blend_oos 的 quantile_ls_day/换手/成本/
绩效原语），并排出【全样本 / 仅低波 / 仅高波】三口径对照——检验"条件化是否增强"这一相对命题。

诚实边界（元方法，写死）：
  - 因子定义本身来自全样本挖掘（range_pct 在 expr_miner 全池体检上榜）——存在选择偏差；
    本实验回答"同一因子在低波/高波子截面上是否分化"，**不是**因子的样本外发现；
  - regime 阈值（1/3、2/3）与字段（ret126/hv60/120日窗）沿用 config.REGIME_* 常量，不再调参
    （避免二次过拟合）；负结果照实写；
  - 研究侧红线：绝不写 LIBRARY/catalog、不被 main import、不自动改任何权重，产物只落
    reports/regime_cond_lab.txt/.json + experiment_ledger 台账。

纯标准库、零网络、只读 cache/research_panel.db。
用法（项目根目录）：
  D:\\Python\\python.exe tools\\regime_cond_lab.py                     # 默认 range_pct5 × vol regime
  D:\\Python\\python.exe tools\\regime_cond_lab.py --expr "ts_mean((high-low)/close,20):range_pct20"
  D:\\Python\\python.exe tools\\regime_cond_lab.py --factor ret63      # 面板列
  D:\\Python\python.exe tools\\regime_cond_lab.py --selftest
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

import factor_expr as fx                    # noqa: E402  G25 引擎（表达式因子求值）
import factor_health as fh                  # noqa: E402  forward_map（严格未来收益）
import factor_regime as frg                 # noqa: E402  compute_labels（PIT regime 标签）
import orthogonal_blend_oos as ob           # noqa: E402  cs_uniform/quantile_ls_day/绩效原语
import panel_builder as pb                  # noqa: E402  G21 面板回读
import expr_miner as em                     # noqa: E402  series_from_rows（同口径装配）

DEFAULT_DB = ROOT / "cache" / "research_panel.db"
DEFAULT_TXT = ROOT / "reports" / "regime_cond_lab.txt"
DEFAULT_JSON = ROOT / "reports" / "regime_cond_lab.json"
DEFAULT_EXPR = "ts_mean((high-low)/close,5):range_pct5"
VIEWS = ("all", "low", "high")
VIEW_LABEL = {"all": "全样本(不条件化)", "low": "仅低波(条件化)", "high": "仅高波(条件化)"}


# =========================== 装配（纯函数） ===========================
def resolve_factor(bysym_rows, factor=None, expr=None):
    """因子取值 → (name, fac_map{sym:{date:val}})。

    expr 非空：'EXPR[:名称]' 经 G25 引擎逐品种求值（expr_miner.series_from_rows 同口径）；
    否则 factor 必须是面板列名。缺失/非有限值不进 map。"""
    if expr:
        specs = frg.parse_expr_specs([expr])
        name, expr_text = specs[0][0], specs[0][1]
        fac_map = {}
        for sym, rows in bysym_rows.items():
            rows_sorted = sorted(rows, key=lambda r: r["date"])
            fac = fx.compute_ts(expr_text, em.series_from_rows(rows_sorted))
            fac_map[sym] = {r["date"]: v for r, v in zip(rows_sorted, fac) if fx._isnum(v)}
        return name, fac_map
    if not factor:
        raise ValueError("--factor 与 --expr 至少给一个")
    fac_map = {}
    for sym, rows in bysym_rows.items():
        fac_map[sym] = {r["date"]: r[factor] for r in rows
                        if isinstance(r.get(factor), (int, float)) and math.isfinite(r[factor])}
    return factor, fac_map


def compose_factor(maps):
    """复合截面因子（第78轮，纯函数）：逐日对各成员做截面均匀秩标准化后等权平均。

    maps=[{sym:{date:val}}, ...]；逐日只保留"当日全部成员均有限"的品种（成对剔除）。
    返回 {sym:{date:composite}}。成员方向须一致（本实验用于低波异象族：hv20/range_pct 全为
    "值高→跑输"的负IC族，复合=族内共识强度）。"""
    all_dates = set()
    for m in maps:
        for inner in m.values():
            all_dates |= set(inner)
    out = {}
    for d in sorted(all_dates):
        zs = [ob.cs_uniform({s: inner[d] for s, inner in m.items() if d in inner})
              for m in maps]
        common = None
        for z in zs:
            ks = set(z)
            common = ks if common is None else (common & ks)
        if not common:
            continue
        for s in common:
            out.setdefault(s, {})[d] = sum(z[s] for z in zs) / float(len(zs))
    return out


def regime_map_of(bysym_rows):
    """复用 factor_regime.compute_labels → {sym:{date:vol_label}}（low/mid/high/None）。"""
    labels = frg.compute_labels(bysym_rows)
    out = {}
    for sym, (rows, _tl, vl) in labels.items():
        out[sym] = {r["date"]: vl[i] for i, r in enumerate(rows)}
    return out


def forward_maps(bysym_rows, horizon):
    """严格未来 H 日收益 → {sym:{date:fwd}}（fh.forward_map，末端 H 日无值为 None）。"""
    out = {}
    for sym, rows in bysym_rows.items():
        rows_sorted = sorted(rows, key=lambda r: r["date"])
        closes = [r["c"] for r in rows_sorted]
        fwd = fh.forward_map(closes, (horizon,))[horizon]
        out[sym] = {r["date"]: fwd[t] for t, r in enumerate(rows_sorted)
                    if fwd[t] is not None and math.isfinite(fwd[t])}
    return out


# =========================== 三口径账本（纯函数） ===========================
def build_books(dates, fac_map, fwd_map, reg_map, h, n_q, min_names):
    """按 H 对齐的调仓日账本：每个调仓日给 all/low/high 三个视图的截面分数与前向收益。

    返回 (books, cs_ics)。books 每项 {"all"/"low"/"high": {sym: 分数}, "y": {sym: 前向收益}}；
    cs_ics=[(date, view, ic, n)]——各视图当日截面 Spearman（供 IC 层对照）。
    条件化视图的样本不足 min_names 时该视图为空 dict（quantile_ls_day 自然返回 None）。"""
    books, cs_ics = [], []
    for i, d in enumerate(dates):
        if i % h:
            continue
        score = {v: {} for v in VIEWS}
        y = {}
        for sym in fac_map:
            f = fac_map.get(sym, {}).get(d)
            yy = fwd_map.get(sym, {}).get(d)
            if not (fx._isnum(f) and fx._isnum(yy)):
                continue
            y[sym] = yy
            score["all"][sym] = f
            lbl = reg_map.get(sym, {}).get(d)
            if lbl == "low":
                score["low"][sym] = f
            elif lbl == "high":
                score["high"][sym] = f
        if len(y) < min_names:
            continue
        z = {v: ob.cs_uniform(score[v]) for v in VIEWS}
        book = {"y": y}
        for v in VIEWS:
            book[v] = z[v]
            if len(z[v]) >= min_names:
                ic = fx.spearman(list(z[v].values()), [y[s] for s in z[v]])
                if fx._isnum(ic):
                    cs_ics.append((d, v, ic, len(z[v])))
        books.append(book)
    return books, cs_ics


def view_summary(books, view, n_q, cost, h=1):
    """单视图绩效：按 H 对齐非重叠分层多空（books 已只含调仓日，hold=1 即期不重叠）。"""
    ls = ob.evaluate_ls_books_aligned(books, view, n_q, cost, hold=1, period_days=h)
    return ls


def summarize_ics(cs_ics, view):
    """某视图的逐日截面 IC 汇总（复用 expr_miner.cs_summary 口径）。"""
    xs = [(d, ic, n) for d, v, ic, n in cs_ics if v == view]
    return em.cs_summary(xs)


# =========================== 第76轮：稳健链（H网格/子期分段/placebo） ===========================
def placebo_regime_map(reg_map, dates, seed=20260905):
    """Placebo 标签重排（确定性种子）：把"日期→标签向量"整体随机重排——保留标签的截面结构
    与自相关量级，只破坏与(因子,前向收益)的对齐。若条件化增益是真实 regime 效应，重排后
    低波视图应退化到≈全样本水平。纯函数。"""
    import random
    rng = random.Random(seed)
    vecs = []
    for d in dates:
        vecs.append({sym: m.get(d) for sym, m in reg_map.items()})
    shuffled = vecs[:]
    rng.shuffle(shuffled)
    out = {}
    for d, vec in zip(dates, shuffled):
        for sym, lbl in vec.items():
            if lbl is not None:
                out.setdefault(sym, {})[d] = lbl
    return out


def evaluate_views(bysym_rows, fac_map, reg_map, h, n_q, min_names, cost):
    """单配置三口径评估（纯函数）：返回 (dates, summaries, ics, n_books)。"""
    fwd_map = forward_maps(bysym_rows, h)
    dates = sorted({d for m in fac_map.values() for d in m})
    books, cs_ics = build_books(dates, fac_map, fwd_map, reg_map, h, n_q, min_names)
    summaries = {v: view_summary(books, v, n_q, cost, h=h) for v in VIEWS}
    ics = {v: summarize_ics(cs_ics, v) for v in VIEWS}
    return dates, summaries, ics, len(books)


def robust_chain(bysym_rows, fac_map, reg_map, h, n_q, min_names, cost,
                 grid=(5, 10, 20, 40), seed=20260905, placebo_seeds=1):
    """稳健链（纯函数）：
    1) H 网格：grid 内各持有期的三口径净年化/截面IC（结构是否跨 H 稳定）；
    2) 子期分段：全样本日期对半切，各段内重建账本（结构是否跨时段稳定）；
    3) placebo：regime 标签日期重排后低波视图应退化到≈全样本（证明增益来自 regime 对齐）。
       第79轮：placebo_seeds>1 时跑多种子并给出分布（min/median/max），单种子结论不稳
       （第78轮复合因子曾用4种子人工验证，此处常设化）。"""
    out = {"h_grid": [], "sub_periods": [], "placebo": None, "placebos": []}
    for hh in grid:
        _d, summ, ics, n_books = evaluate_views(bysym_rows, fac_map, reg_map, hh, n_q, min_names, cost)
        out["h_grid"].append({
            "h": hh, "n_periods": n_books,
            "all_annual": summ["all"]["net"].get("annual_ret"),
            "low_annual": summ["low"]["net"].get("annual_ret"),
            "high_annual": summ["high"]["net"].get("annual_ret"),
            "all_ic": ics["all"]["mean_ic"], "low_ic": ics["low"]["mean_ic"],
            "high_ic": ics["high"]["mean_ic"]})
    # 子期分段（用主 h）
    fwd_map = forward_maps(bysym_rows, h)
    dates = sorted({d for m in fac_map.values() for d in m})
    half = len(dates) // 2
    for label, sub in (("前半", dates[:half]), ("后半", dates[half:])):
        books, cs_ics = build_books(sub, fac_map, fwd_map, reg_map, h, n_q, min_names)
        s_low = view_summary(books, "low", n_q, cost, h=h)
        s_all = view_summary(books, "all", n_q, cost, h=h)
        i_low = summarize_ics(cs_ics, "low")
        i_all = summarize_ics(cs_ics, "all")
        out["sub_periods"].append({
            "label": label, "date_min": sub[0] if sub else None,
            "date_max": sub[-1] if sub else None,
            "low_annual": s_low["net"].get("annual_ret"), "all_annual": s_all["net"].get("annual_ret"),
            "low_ic": i_low["mean_ic"], "all_ic": i_all["mean_ic"],
            "low_days": i_low["n_days"]})
    # placebo（用主 h；多种子给分布）
    p_ics_all, p_ann_all = [], []
    for k in range(max(1, int(placebo_seeds))):
        pmap = placebo_regime_map(reg_map, dates, seed=seed + k)
        _d, p_summ, p_ics, _nb = evaluate_views(bysym_rows, fac_map, pmap, h, n_q, min_names, cost)
        rec = {"seed": seed + k,
               "low_annual": p_summ["low"]["net"].get("annual_ret"),
               "low_ic": p_ics["low"]["mean_ic"],
               "all_annual": p_summ["all"]["net"].get("annual_ret")}
        out["placebos"].append(rec)
        if rec["low_ic"] is not None:
            p_ics_all.append(rec["low_ic"])
        if rec["low_annual"] is not None:
            p_ann_all.append(rec["low_annual"])

    def _stat(xs):
        if not xs:
            return None, None, None
        xs = sorted(xs)
        n = len(xs)
        med = xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])
        return xs[0], med, xs[-1]

    first = out["placebos"][0]
    ic_min, ic_med, ic_max = _stat(p_ics_all)
    an_min, an_med, an_max = _stat(p_ann_all)
    out["placebo"] = {"low_annual": first["low_annual"], "low_ic": first["low_ic"],
                      "all_annual": first["all_annual"],
                      "seeds": len(out["placebos"]),
                      "low_ic_min": ic_min, "low_ic_median": ic_med, "low_ic_max": ic_max,
                      "low_annual_min": an_min, "low_annual_median": an_med,
                      "low_annual_max": an_max}
    return out


def render_robust(rb):
    L = ["-" * 104,
         "[稳健链 --robust]（第76轮：结构是否跨持有期/跨时段稳定；placebo 验证增益来自 regime 对齐）"]
    L.append("  H网格（净年化%% / 截面IC）：")
    L.append("    %-6s %10s %10s %10s %10s %10s %10s"
             % ("H", "全样本年化", "低波年化", "高波年化", "全样本IC", "低波IC", "高波IC"))
    for r in rb["h_grid"]:
        L.append("    %-6d %10s %10s %10s %10s %10s %10s"
                 % (r["h"],
                    ("%+.2f%%" % (100.0 * r["all_annual"])) if r["all_annual"] is not None else "--",
                    ("%+.2f%%" % (100.0 * r["low_annual"])) if r["low_annual"] is not None else "--",
                    ("%+.2f%%" % (100.0 * r["high_annual"])) if r["high_annual"] is not None else "--",
                    ("%+.3f" % r["all_ic"]) if r["all_ic"] is not None else "--",
                    ("%+.3f" % r["low_ic"]) if r["low_ic"] is not None else "--",
                    ("%+.3f" % r["high_ic"]) if r["high_ic"] is not None else "--"))
    L.append("  子期分段（前/后半各重建账本，低波视图）：")
    for r in rb["sub_periods"]:
        L.append("    %s（%s ~ %s）：低波净年化 %s / IC %s（全样本 %s / %s，低波有效天数 %d）"
                 % (r["label"], r["date_min"], r["date_max"],
                    ("%+.2f%%" % (100.0 * r["low_annual"])) if r["low_annual"] is not None else "--",
                    ("%+.3f" % r["low_ic"]) if r["low_ic"] is not None else "--",
                    ("%+.2f%%" % (100.0 * r["all_annual"])) if r["all_annual"] is not None else "--",
                    ("%+.3f" % r["all_ic"]) if r["all_ic"] is not None else "--",
                    r["low_days"]))
    p = rb["placebo"]
    if p:
        if p.get("seeds", 1) > 1:
            L.append("  placebo（标签日期重排×%d种子，低波视图）：真实低波应远离分布才非分桶假象" % p["seeds"])
            L.append("    IC：min %s / median %s / max %s；净年化：min %s / median %s / max %s（全样本 IC 见上表）"
                     % (("%+.3f" % p["low_ic_min"]) if p["low_ic_min"] is not None else "--",
                        ("%+.3f" % p["low_ic_median"]) if p["low_ic_median"] is not None else "--",
                        ("%+.3f" % p["low_ic_max"]) if p["low_ic_max"] is not None else "--",
                        ("%+.2f%%" % (100.0 * p["low_annual_min"])) if p["low_annual_min"] is not None else "--",
                        ("%+.2f%%" % (100.0 * p["low_annual_median"])) if p["low_annual_median"] is not None else "--",
                        ("%+.2f%%" % (100.0 * p["low_annual_max"])) if p["low_annual_max"] is not None else "--"))
        else:
            L.append("  placebo（标签日期重排，低波视图）：净年化 %s / IC %s（全样本 %s）"
                     "——若与真实低波口径接近则为分桶假象、远离则 regime 效应成立（建议 --placebo-seeds 4 取分布）"
                     % (("%+.2f%%" % (100.0 * p["low_annual"])) if p["low_annual"] is not None else "--",
                        ("%+.3f" % p["low_ic"]) if p["low_ic"] is not None else "--",
                        ("%+.2f%%" % (100.0 * p["all_annual"])) if p["all_annual"] is not None else "--"))
    return L


# =========================== 主流程 ===========================
def run(db_path=None, txt_path=None, json_path=None, factor=None, expr=None,
        h=20, n_q=5, min_names=10, cost=None, verbose=True, robust=False, compose=None,
        placebo_seeds=1):
    db_path = str(db_path or DEFAULT_DB)
    txt_path = str(txt_path or DEFAULT_TXT)
    json_path = str(json_path or DEFAULT_JSON)
    cost = ob.DEFAULT_COST_ONEWAY if cost is None else cost
    store = pb.PanelStore(db_path)
    syms = sorted(store.symbols())
    bysym = {}
    for s in syms:
        rows = store.load_rows(s)
        if rows:
            bysym[s] = rows
    store.close()
    if compose:
        specs = [x for x in (t.strip() for t in compose.split(";")) if x]
        members = [resolve_factor(bysym, expr=sp)[1] for sp in specs]
        name, fac_map = "composite(%d)" % len(members), compose_factor(members)
    else:
        name, fac_map = resolve_factor(bysym, factor=factor, expr=expr)
    reg_map = regime_map_of(bysym)
    fwd_map = forward_maps(bysym, h)
    dates = sorted({d for m in fac_map.values() for d in m})
    books, cs_ics = build_books(dates, fac_map, fwd_map, reg_map, h, n_q, min_names)
    summaries = {v: view_summary(books, v, n_q, cost, h=h) for v in VIEWS}
    ics = {v: summarize_ics(cs_ics, v) for v in VIEWS}
    rb = (robust_chain(bysym, fac_map, reg_map, h, n_q, min_names, cost,
                       placebo_seeds=placebo_seeds) if robust else None)
    result = {"factor": name, "expr": expr, "compose": compose,
              "h": h, "n_q": n_q, "min_names": min_names,
              "cost_oneway": cost, "n_symbols": len(bysym), "n_dates": len(dates),
              "db": db_path,
              "date_min": dates[0] if dates else None, "date_max": dates[-1] if dates else None,
              "views": {}}
    for v in VIEWS:
        ls = summaries[v]
        result["views"][v] = {
            "n_periods": ls["n_periods"],
            "net": {k: ls["net"].get(k) for k in ("annual_ret", "sharpe", "max_drawdown",
                                                  "cum_ret", "pct_positive", "n_days")},
            "gross_annual": ls["gross"].get("annual_ret"),
            "avg_turnover": ls["avg_turnover_one_sided"], "total_cost": ls["total_cost"],
            "avg_spread": ls["avg_spread"], "cs_ic": ics[v]}
    if rb:
        result["robust"] = rb
    text = render_report(result, robust=rb)
    if verbose:
        print(text)
    os.makedirs(os.path.dirname(txt_path), exist_ok=True)
    with open(txt_path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(text + "\n")
    with open(json_path, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(result, fp, ensure_ascii=False, allow_nan=False, indent=1)
    try:
        import experiment_ledger
        low = result["views"]["low"]
        experiment_ledger.safe_record(
            "regime_cond_lab", {"factor": name, "h": h, "n_q": n_q, "min_names": min_names},
            {"low_net_annual": low["net"].get("annual_ret"),
             "low_net_sharpe": low["net"].get("sharpe"),
             "low_cs_ic": low["cs_ic"].get("mean_ic"),
             "all_net_annual": result["views"]["all"]["net"].get("annual_ret")},
            inputs={"panel": db_path, "n_sym": len(bysym), "n_dates": len(dates)},
            artifacts=[txt_path, json_path],
            conclusion="G25/G29续 regime 条件化分层多空实验台：全样本/低波/高波三口径对照，"
                       "检验条件化是否增强（因子定义有全样本选择偏差，仅相对命题）",
            reproduce="D:\\Python\\python.exe tools/regime_cond_lab.py")
    except Exception:
        pass
    return result


def render_report(result, robust=None):
    L = ["=" * 104,
         " G25/G29续 regime 条件化分层多空实验台（研究侧：检验条件化是否增强，不自动上线）  生成于 %s"
         % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         "=" * 104]
    L.append("因子=%s（%s）；H=%d 对齐非重叠再平衡；%d层多顶空底；单边成本万%.1f；截面最少%d品种"
             % (result["factor"], "复合截面" if result.get("compose") else
                ("表达式" if result["expr"] else "面板列"),
                result["h"], result["n_q"], 10000.0 * result["cost_oneway"], result["min_names"]))
    L.append("面板 %s：品种=%d 交易日=%d（%s ~ %s）；regime=vol(hv60过去120日ts_rank三分位,PIT)"
             % (result.get("db"), result["n_symbols"], result["n_dates"],
                result.get("date_min"), result.get("date_max")))
    L.append("-" * 104)
    L.append("  %-16s %8s %10s %9s %10s %10s %9s %10s %11s"
             % ("口径", "调仓期", "净年化", "净夏普", "净回撤", "毛年化", "日均换手", "截面IC",
                "IC_t/天数"))
    for v in VIEWS:
        s = result["views"][v]
        net = s["net"]
        ic = s["cs_ic"]
        ic_txt = ("%+.3f" % ic["mean_ic"]) if ic.get("mean_ic") is not None else "--"
        t_txt = ("%+.1f/%d" % (ic.get("t_stat") or 0.0, ic.get("n_days") or 0)
                 if ic.get("mean_ic") is not None else "--")
        L.append("  %-16s %8d %10s %9s %10s %10s %9s %10s %11s"
                 % (VIEW_LABEL[v], s["n_periods"],
                    ("%+.2f%%" % (100.0 * net["annual_ret"])) if net["annual_ret"] is not None else "--",
                    ("%+.2f" % net["sharpe"]) if net["sharpe"] is not None else "--",
                    ("%+.2f%%" % (100.0 * net["max_drawdown"])) if net["max_drawdown"] is not None else "--",
                    ("%+.2f%%" % (100.0 * s["gross_annual"])) if s["gross_annual"] is not None else "--",
                    ("%+.3f" % s["avg_turnover"]) if s["avg_turnover"] is not None else "--",
                    ic_txt, t_txt))
    low, high, allv = result["views"]["low"], result["views"]["high"], result["views"]["all"]
    L.append("-" * 104)
    L.append("[诚实结论]（相对命题：条件化是否增强；因子定义有全样本选择偏差，非样本外发现）")
    if low["net"]["annual_ret"] is not None and allv["net"]["annual_ret"] is not None:
        L.append("  低波 vs 全样本：净年化差 %+.2fpp，截面IC差 %+.3f；低波净夏普 %.2f vs 全样本 %.2f"
                 % (100.0 * (low["net"]["annual_ret"] - allv["net"]["annual_ret"]),
                    ((low["cs_ic"]["mean_ic"] or 0.0) - (allv["cs_ic"]["mean_ic"] or 0.0)),
                    low["net"]["sharpe"] or 0.0, allv["net"]["sharpe"] or 0.0))
    if high["net"]["annual_ret"] is not None:
        L.append("  高波（对照）：净年化 %+.2f%%、截面IC %s——若与低波反向/近零则条件化结构成立"
                 % (100.0 * high["net"]["annual_ret"],
                    ("%+.3f" % high["cs_ic"]["mean_ic"]) if high["cs_ic"]["mean_ic"] is not None else "--"))
    if low["net"]["annual_ret"] is not None and high["net"]["annual_ret"] is not None:
        L.append("  方向注记：本账本方向=多因子值高（高振幅）/空低振幅（因子原始方向，IC为负）；"
                 "反向持有（多低振幅/空高振幅）的低波口径镜像≈净年化 %+.2f%%、高波口径≈%+.2f%%"
                 "（权重取负、成本对称恒等）。"
                 % (-100.0 * low["net"]["annual_ret"], -100.0 * high["net"]["annual_ret"]))
    L.append("  负结果照实：若低波口径未优于全样本，则 regime 条件化不增强、仅为分桶统计假象。")
    L.append("  研究侧红线：本工具不写 LIBRARY/catalog、不被 main import、不自动改权重。")
    if robust:
        L.extend(render_robust(robust))
    L.append("=" * 104)
    return "\n".join(L)


# =========================== 零网络/零DB 合成断言 ===========================
def _synth_bysym(n_sym=8, n_date=300):
    """合成面板：hv60 以 40 日周期正弦振荡（ts_rank 全程铺满三分位→low/mid/high 都会出现），
    因子（价格水平）恒为未来收益序——低波视图 IC 应≈+1（健全性），高波视图同样可得样本。"""
    import random
    rng = random.Random(11)
    rows = []
    for si in range(n_sym):
        price = 100.0
        for t in range(n_date):
            hv = 0.02 + 0.58 * (0.5 + 0.5 * math.sin(2.0 * math.pi * t / 40.0))
            shock = 0.002 * si + rng.gauss(0, 0.0005)
            price *= (1 + shock)
            rows.append({"sym": "S%02d" % si,
                         "date": "2025-%02d-%02d" % (1 + (t // 28), 1 + (t % 28)),
                         "c": price, "hv60": hv, "ret126": 0.0})
    bysym = {}
    for r in rows:
        bysym.setdefault(r["sym"], []).append(r)
    return bysym


def selftest():
    bysym = _synth_bysym()
    # 1) resolve_factor 表达式与面板列两条路径
    name, fac = resolve_factor(bysym, expr="close/delay(close,5)-1:mom5")
    assert name == "mom5" and all(len(m) > 0 for m in fac.values())
    name2, fac2 = resolve_factor(bysym, factor="c")
    assert name2 == "c" and len(fac2) == len(fac)
    # 2) regime 标签：合成面板前半段=低波、后半段=高波（hv60 ts_rank 120日窗需要暖机，末端才可靠）
    reg = regime_map_of(bysym)
    lows = sum(1 for m in reg.values() for v in m.values() if v == "low")
    highs = sum(1 for m in reg.values() for v in m.values() if v == "high")
    assert lows > 0 and highs > 0
    # 3) build_books：结构/无未来（末端 H 日无账本）/条件化视图子集
    fac3 = {s: {r["date"]: float(r["c"]) for r in rows} for s, rows in bysym.items()}
    fm = forward_maps(bysym, 5)
    dates = sorted({d for m in fac3.values() for d in m})
    books, cs_ics = build_books(dates, fac3, fm, reg, h=5, n_q=3, min_names=4)
    assert books and all(set(b) >= {"all", "low", "high", "y"} for b in books)
    assert all(len(b["low"]) <= len(b["all"]) for b in books)
    last_date = dates[-1]
    assert all(last_date not in b["y"] for b in books[-1:])   # 末端无前向收益
    # 4) view_summary 三视图可跑、期数一致；IC 汇总结构齐
    for v in VIEWS:
        s = view_summary(books, v, 3, 0.0)
        assert s["n_periods"] > 0
    for v in VIEWS:
        s = summarize_ics(cs_ics, v)
        assert "mean_ic" in s and "n_days" in s
    # 5) 单调性健全性：合成面板低波段因子=价格水平=未来收益序 → 低波视图 IC 应为正
    low_ic = summarize_ics(cs_ics, "low")
    assert low_ic["mean_ic"] is not None and low_ic["mean_ic"] > 0.5, low_ic
    # 6) 第76轮 稳健链：placebo 确定性+保量、evaluate_views/robust_chain 结构齐
    reg6 = regime_map_of(bysym)
    dates6 = sorted({r["date"] for rows in bysym.values() for r in rows})
    pm1 = placebo_regime_map(reg6, dates6, seed=7)
    pm2 = placebo_regime_map(reg6, dates6, seed=7)
    assert pm1 == pm2                                     # 同种子确定性
    n_lbl = sum(1 for m in reg6.values() for v in m.values() if v)
    n_plb = sum(1 for m in pm1.values() for v in m.values() if v)
    assert n_lbl == n_plb                                 # 标签总量不变
    dates_e, summ_e, ics_e, nb = evaluate_views(bysym, fac3, reg6, 5, 3, 4, 0.0)
    assert nb > 0 and summ_e["low"]["net"].get("annual_ret") is not None
    rb6 = robust_chain(bysym, fac3, reg6, 5, 3, 4, 0.0, grid=(5, 10))
    assert len(rb6["h_grid"]) == 2 and len(rb6["sub_periods"]) == 2
    assert rb6["placebo"]["low_ic"] is not None
    # 第79轮：多种子 placebo 常设化——placebos 列表与 min/median/max 统计齐
    rb7 = robust_chain(bysym, fac3, reg6, 5, 3, 4, 0.0, grid=(5,), placebo_seeds=3)
    assert len(rb7["placebos"]) == 3 and rb7["placebo"]["seeds"] == 3
    assert rb7["placebo"]["low_ic_min"] is not None and rb7["placebo"]["low_ic_median"] is not None \
        and rb7["placebo"]["low_ic_max"] is not None
    text6 = "\n".join(render_robust(rb6))
    assert "H网格" in text6 and "placebo" in text6
    # 7) 第78轮 复合截面因子：逐日等权秩平均手算（两成员反向排序→共识0；同向→极值；缺失成对剔除）
    m1 = {"A": {"d": 1.0}, "B": {"d": 2.0}, "C": {"d": 3.0}}
    m2 = {"A": {"d": 30.0}, "B": {"d": 20.0}, "C": {"d": 10.0}}
    comp = compose_factor([m1, m2])
    assert abs(comp["A"]["d"]) < 1e-12 and abs(comp["B"]["d"]) < 1e-12 and abs(comp["C"]["d"]) < 1e-12
    m3 = {"A": {"d": 1.0}, "B": {"d": 2.0}, "C": {"d": 3.0}}
    comp2 = compose_factor([m1, m3])
    assert abs(comp2["A"]["d"] + 1.0) < 1e-12 and abs(comp2["C"]["d"] - 1.0) < 1e-12
    m4 = {"A": {"d": 5.0}, "B": {"d": 6.0}}
    comp3 = compose_factor([m1, m4])
    assert set(comp3) == {"A", "B"} and "C" not in comp3
    print("regime_cond_lab selftest ALL PASS（因子装配两路径/regime标签/build_books结构无未来/"
          "三视图绩效与IC汇总/低波强信号健全性/稳健链与placebo/复合截面因子 共7组）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="G25/G29续 regime 条件化分层多空实验台（研究侧）")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--factor", default=None, help="面板列名（与 --expr 二选一）")
    ap.add_argument("--expr", default=DEFAULT_EXPR,
                    help="'EXPR[:名称]'（白名单DSL）；传空串则须给 --factor")
    ap.add_argument("--h", type=int, default=20, help="持有期/再平衡间隔（交易日）")
    ap.add_argument("--quantiles", type=int, default=5)
    ap.add_argument("--min-names", type=int, default=10)
    ap.add_argument("--cost-oneway", type=float, default=None, help="单边成本率，默认回测口径万1.5")
    ap.add_argument("--robust", action="store_true", help="第76轮：稳健链（H网格/子期分段/placebo标签重排）")
    ap.add_argument("--placebo-seeds", type=int, default=1,
                    help="第79轮：placebo 重排种子数（>1 时报告 min/median/max 分布，判定更稳）")
    ap.add_argument("--compose", default=None,
                    help="第78轮：复合截面因子——';'分隔多条 'EXPR[:名称]'，逐日对各成员截面均匀秩"
                         "标准化后等权平均（成员全有限的品种才保留）；配 --robust 同样适用")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    run(db_path=args.db, factor=args.factor, expr=(args.expr.strip() or None),
        h=args.h, n_q=args.quantiles, min_names=args.min_names, cost=args.cost_oneway,
        robust=args.robust, compose=args.compose, placebo_seeds=args.placebo_seeds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
