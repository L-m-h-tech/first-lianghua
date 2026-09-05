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


# =========================== 主流程 ===========================
def run(db_path=None, txt_path=None, json_path=None, factor=None, expr=None,
        h=20, n_q=5, min_names=10, cost=None, verbose=True):
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
    name, fac_map = resolve_factor(bysym, factor=factor, expr=expr)
    reg_map = regime_map_of(bysym)
    fwd_map = forward_maps(bysym, h)
    dates = sorted({d for m in fac_map.values() for d in m})
    books, cs_ics = build_books(dates, fac_map, fwd_map, reg_map, h, n_q, min_names)
    summaries = {v: view_summary(books, v, n_q, cost, h=h) for v in VIEWS}
    ics = {v: summarize_ics(cs_ics, v) for v in VIEWS}
    result = {"factor": name, "expr": expr, "h": h, "n_q": n_q, "min_names": min_names,
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
    text = render_report(result)
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


def render_report(result):
    L = ["=" * 104,
         " G25/G29续 regime 条件化分层多空实验台（研究侧：检验条件化是否增强，不自动上线）  生成于 %s"
         % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         "=" * 104]
    L.append("因子=%s（%s）；H=%d 对齐非重叠再平衡；%d层多顶空底；单边成本万%.1f；截面最少%d品种"
             % (result["factor"], "表达式" if result["expr"] else "面板列",
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
    print("regime_cond_lab selftest ALL PASS（因子装配两路径/regime标签/build_books结构无未来/"
          "三视图绩效与IC汇总/低波强信号健全性 共5组）")
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
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    run(db_path=args.db, factor=args.factor, expr=(args.expr.strip() or None),
        h=args.h, n_q=args.quantiles, min_names=args.min_names, cost=args.cost_oneway)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
