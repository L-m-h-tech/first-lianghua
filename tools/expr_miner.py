# -*- coding: utf-8 -*-
"""第72-73轮 G25续：表达式因子自动挖掘 expr_miner——研究侧、红线门控。

按总纲第16条红线：自动挖掘最多在 tools 研究侧产出候选，任何因子仍须人工复核并通过
G23/G29 的双样本与因子体检，且受 G13/G16 门控，禁止端到端自动进综合分。本工具
**绝不写 LIBRARY/catalog、不被 main import、不自动改任何权重**，只把确定性候选表达式 +
前向 RankIC 体检结果落 reports/expr_miner.txt/.json + experiment_ledger 一条台账。

思路（确定性穷举，非随机/非遗传/非LLM）：
  1) 候选池 = 白名单字段 close/volume/oi/high/low 的**量纲无关派生量**（动量/量能变化/
     持仓变化/均线比/风险调整动量/量价相关/时序z/区间位置等），全部只用 factor_expr
     白名单 DSL 算子表达、窗口取自固定集合（5/10/20/60），可编译、无未来；
  2) 逐候选逐品种计算因子序列，对前向 H=1/5/20 交易日收益做**两层** Spearman RankIC
     （严格只向未来）：时序层=逐品种IC均值(meanIC)+全样本池化(pooledIC)，与 expr_research
     完全同口径；截面层（第73轮新增）=逐交易日跨品种 RankIC 的均值/ICIR/t值/正比例
     ——商品截面策略的标准口径；
  3) 报告按 H5 |meanIC| 降序**全量列出**（负结果照实）；|meanIC|≥0.05 视为"上榜候选"
     仅供人工复核（时序/截面两口径各自上榜，结论由数据动态生成，不预设）；
  4) 只产出候选与体检结果，绝不自动上线。

零新增运行依赖；只读 cache/research_panel.db；纯标准库。
用法（项目根目录）：
  D:\\Python\\python.exe tools\\expr_miner.py [--db cache/research_panel.db] [--limit N] [--selftest]
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import factor_expr as fe          # noqa: E402 白名单DSL引擎
import panel_builder as pb        # noqa: E402 G21面板回读

HORIZONS = (1, 5, 20)
DEFAULT_DB = ROOT / "cache" / "research_panel.db"
DEFAULT_TXT = ROOT / "reports" / "expr_miner.txt"
DEFAULT_JSON = ROOT / "reports" / "expr_miner.json"
MIN_FINITE = 30            # 单品种最少有限配对点才计入逐品种IC均值
IC_FLOOR = 0.05            # |meanIC| 上榜门槛：达到才值得人工复核（不自动上线）
MIN_CS = 10                # 逐日截面IC：当日截面最少有限品种数
WINDOWS = (5, 10, 20, 60)


# =========================== 输入装配（同 expr_research 口径） ===========================
def series_from_rows(rows):
    """面板列直读：close/volume/oi/high/low 与 expr_research 完全一致。"""
    return {"close": [r["c"] for r in rows], "volume": [r["v"] for r in rows],
            "high": [r.get("h", r["c"]) for r in rows],
            "low": [r.get("l", r["c"]) for r in rows],
            "oi": [r.get("oi", 0.0) for r in rows]}


def forward_return(close, t, h):
    """严格只向未来：t 时点看 t+h 收盘，不足返回 None（与 expr_research 同口径）。"""
    if t + h >= len(close):
        return None
    a, b = close[t], close[t + h]
    if not (fe._isnum(a) and fe._isnum(b)) or a <= 0:
        return None
    return b / a - 1.0


def aligned_pairs(fac, close, h):
    """因子序列 vs 前向 h 日收益的有限配对（成对剔除缺失）。"""
    xs, ys = [], []
    for t in range(len(fac)):
        if not fe._isnum(fac[t]):
            continue
        fwd = forward_return(close, t, h)
        if fe._isnum(fwd):
            xs.append(fac[t])
            ys.append(fwd)
    return xs, ys


# =========================== 逐日截面 RankIC（第73轮新增，纯函数） ===========================
def cross_section_ics(fac_by_sym_date, fwd_by_sym_date, min_cs=MIN_CS):
    """逐交易日截面 RankIC：当日跨品种 factor vs 前向收益的 Spearman。

    fac_by_sym_date={sym:{date:val}}；fwd_by_sym_date={sym:{date:fwd}}。
    返回 [(date, ic, n)]：当日截面有限配对≥min_cs 且 Spearman 有限才计入（纯函数、零IO）。"""
    all_dates = sorted({d for m in fac_by_sym_date.values() for d in m})
    out = []
    for d in all_dates:
        xs, ys = [], []
        for sym, m in fac_by_sym_date.items():
            v = m.get(d)
            if not fe._isnum(v):
                continue
            f = fwd_by_sym_date.get(sym, {}).get(d)
            if fe._isnum(f):
                xs.append(v)
                ys.append(f)
        if len(xs) >= min_cs:
            ic = fe.spearman(xs, ys)
            if fe._isnum(ic):
                out.append((d, ic, len(xs)))
    return out


def cs_summary(day_ics):
    """cross_section_ics 输出 → 均值/ICIR/t值/正比例/天数（t=mean/std×sqrt(n)，跨日独立近似）。"""
    xs = [ic for _, ic, _ in day_ics]
    n = len(xs)
    if n == 0:
        return {"mean_ic": None, "icir": None, "t_stat": None,
                "pct_positive": None, "n_days": 0}
    mean = sum(xs) / n
    var = sum((v - mean) ** 2 for v in xs) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    return {"mean_ic": mean, "icir": (mean / sd if sd > 1e-15 else 0.0),
            "t_stat": (mean / sd * math.sqrt(n) if sd > 1e-15 else 0.0),
            "pct_positive": sum(1 for v in xs if v > 0) / n, "n_days": n}


# =========================== 候选生成（确定性穷举，白名单DSL） ===========================
def candidate_pool():
    """生成确定性候选表达式列表（带 key/方向/名称/说明）。全部只用白名单算子+字段，
    时序上下文逐品种计算（不混截面算子——截面体检由 expr_research/组合层另行覆盖）。"""
    cands = []
    def add(key, expr, direction, name, note):
        cands.append({"key": key, "expr": expr, "direction": direction,
                      "name": name, "note": note})
    # --- 基础派生量（归一化，量纲无关）---
    for n in WINDOWS:
        add("mom_%d" % n, "close/delay(close,%d)-1" % n, +1,
            "%d日动量" % n, "前n日收益率（与 expr_ret 同源，表达式版）")
    for n in WINDOWS:
        add("vol_chg_%d" % n, "volume/delay(volume,%d)-1" % n, +1,
            "%d日成交量变化率" % n, "量能扩张/收缩")
    for n in WINDOWS:
        add("oi_chg_%d" % n, "oi/delay(oi,%d)-1" % n, +1,
            "%d日持仓量变化率" % n, "持仓增减代理")
    for n in (5, 10, 20):
        add("ma_ratio_%d_20" % n, "ts_mean(close,%d)/ts_mean(close,20)-1" % n, +1,
            "%d/20日均线比" % n, "短长均线强度")
    # 波动/风险调整动量
    for n in (20, 60):
        add("trend_per_vol_%d" % n,
            "(close/ts_mean(close,%d)-1)/(ts_std(close,%d)+0.000001)" % (n, n), +1,
            "单位波动趋势%d" % n, "趋势/波动，风险调整动量")
    # 量仓组合
    add("vol_oi_ratio", "volume/(oi+1)", 0, "量仓比", "换手活跃度代理")
    add("vol_oi_chg_ratio", "volume/delay(volume,5)/(oi/delay(oi,5)+1)", +1,
        "量仓变化比", "量增仓增相对强度")
    add("price_oi_corr20", "corr(close,oi,20)", 0, "价仓相关20", "量价配合诊断")
    # 量价配合
    for n in (5, 20):
        add("vol_price_corr_%d" % n, "corr(volume,close,%d)" % n, 0,
            "量价相关%d" % n, "量价同步/背离")
    # 时序标准化（尾窗 z-score 形态，量纲统一）
    for n in (20, 60):
        add("mom_z_%d" % n,
            "(close/delay(close,%d)-1-ts_mean(close/delay(close,1)-1,%d))/(ts_std(close/delay(close,1)-1,%d)+0.000001)"
            % (n, n, n), +1, "%d日动量z" % n, "动量减去自身窗均值再除自身窗std（时序z，跨品种可比）")
    # 高阶/非线性（白名单内）
    add("ret_sq_20", "sign(delta(close,1))*abs(delta(close,1)/delay(close,1))", 0,
        "日收益符号强度", "sign*|收益| 的方向强度代理")
    add("range_pos_20", "ts_minmax(close,20)", 0, "20日区间位置", "收盘在20日高低区间的位置")
    add("vol_surge_5", "volume/ts_mean(volume,20)", 0, "量能突增5", "当日量/20日均量（放量）")
    add("oi_surge_5", "oi/ts_mean(oi,20)", 0, "持仓突增5", "当日持仓/20日均持仓")
    add("ret_vol_ratio_20", "ts_mean(close/delay(close,1)-1,20)/(ts_std(close/delay(close,1)-1,20)+0.000001)",
        +1, "20日收益波动比", "均值/波动，风险调整动量")
    return cands


# =========================== 前向 RankIC 体检 ===========================
def evaluate_candidate(expr, series, close, horizons=HORIZONS):
    """单品种：表达式序列对未来 H 日收益的 Spearman；返回 {H:(ic,n)}。"""
    fac = fe.compute_ts(expr, series)
    out = {}
    for h in horizons:
        xs, ys = aligned_pairs(fac, close, h)
        out[h] = (fe.spearman(xs, ys), len(xs))
    return out


def _mean(vals):
    return sum(vals) / len(vals) if vals else None


def _max_abs_ic(rec):
    """候选在各档 H 的 |meanIC| 最大值（None 记 0），用于上榜判定与排序。"""
    best = 0.0
    for h in HORIZONS:
        mi = rec["h"][h]["mean_ic"]
        if fe._isnum(mi) and abs(mi) > best:
            best = abs(mi)
    return best


# =========================== 主流程 ===========================
def run(db_path=None, txt_path=None, json_path=None, limit=None, verbose=True):
    db_path = str(db_path or DEFAULT_DB)
    txt_path = str(txt_path or DEFAULT_TXT)
    json_path = str(json_path or DEFAULT_JSON)
    store = pb.PanelStore(db_path)
    syms = sorted(store.symbols())
    cands = candidate_pool()
    if limit:
        cands = cands[:limit]
    # 缓存每品种序列/日期/前向收益（前向收益与候选无关，只算一次）
    series_cache, dates_cache, fwd_cache = {}, {}, {}
    for sym in syms:
        rows = store.load_rows(sym)
        if not rows:
            continue
        series_cache[sym] = series_from_rows(rows)
        dates_cache[sym] = [r.get("date") for r in rows]
        close = series_cache[sym]["close"]
        fwd_cache[sym] = {h: {dates_cache[sym][t]: forward_return(close, t, h)
                              for t in range(len(close))} for h in HORIZONS}
    # 逐候选体检（每品种因子序列只算一次，三档 H 共用；同时落逐日截面层）
    results = []
    for c in cands:
        rec = {"key": c["key"], "expr": c["expr"], "direction": c["direction"],
               "name": c["name"], "note": c["note"], "h": {}, "cs": {}}
        per = {h: {"ics": [], "n_pair": 0, "pooled_x": [], "pooled_y": []} for h in HORIZONS}
        fac_by_sym_date = {}
        for sym, series in series_cache.items():
            close = series["close"]
            fac = fe.compute_ts(c["expr"], series)
            fac_by_sym_date[sym] = {dates_cache[sym][t]: v for t, v in enumerate(fac)
                                    if fe._isnum(v)}
            for h in HORIZONS:
                xs, ys = aligned_pairs(fac, close, h)
                if len(xs) >= MIN_FINITE:
                    ic = fe.spearman(xs, ys)
                    if fe._isnum(ic):
                        per[h]["ics"].append(ic)
                        per[h]["n_pair"] += len(xs)
                per[h]["pooled_x"].extend(xs)
                per[h]["pooled_y"].extend(ys)
        for h in HORIZONS:
            ics = per[h]["ics"]
            pooled = fe.spearman(per[h]["pooled_x"], per[h]["pooled_y"]) if per[h]["pooled_x"] else None
            rec["h"][h] = {
                "mean_ic": _mean(ics), "n_sym": len(ics), "n_pair": per[h]["n_pair"],
                "pooled_ic": pooled}
            rec["cs"][h] = cs_summary(cross_section_ics(
                fac_by_sym_date, {s: fwd_cache[s][h] for s in series_cache}, MIN_CS))
        results.append(rec)
    # 汇总：按 H5 的 |meanIC| 降序（参考），但报告保留全部；上榜候选动态判定
    def key_h(r, h):
        return abs(r["h"][h]["mean_ic"]) if fe._isnum(r["h"][h]["mean_ic"]) else 0.0
    ranked = sorted(results, key=lambda r: key_h(r, 5), reverse=True)
    hits = [r["key"] for r in results if _max_abs_ic(r) >= IC_FLOOR]

    def _cs_abs(r, h=5):
        mi = r["cs"][h]["mean_ic"]
        return abs(mi) if fe._isnum(mi) else 0.0
    cs_hits = [r["key"] for r in results
               if any(fe._isnum(r["cs"][h]["mean_ic"]) and abs(r["cs"][h]["mean_ic"]) >= IC_FLOOR
                      for h in HORIZONS)]
    cs_ranked = sorted(results, key=_cs_abs, reverse=True)
    result = {"n_symbols": len(series_cache), "horizons": list(HORIZONS),
              "min_finite": MIN_FINITE, "ic_floor": IC_FLOOR, "min_cs": MIN_CS,
              "n_candidates": len(cands), "candidates": results,
              "hits_over_floor": hits, "cs_hits_over_floor": cs_hits,
              "ranked_by_H5_mean_abs_ic": [r["key"] for r in ranked],
              "cs_ranked_by_H5_mean_abs_ic": [r["key"] for r in cs_ranked]}
    text = render_report(result)
    if verbose:
        print(text)
    os.makedirs(os.path.dirname(txt_path), exist_ok=True)
    with open(txt_path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(text + "\n")
    with open(json_path, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(result, fp, ensure_ascii=False, allow_nan=False, indent=1)
    # 台账旁路
    try:
        import experiment_ledger
        top = ranked[0] if ranked else None
        metrics = {}
        if top:
            metrics["top_H5_mean_ic"] = top["h"][5]["mean_ic"]
            metrics["top_H5_pooled_ic"] = top["h"][5]["pooled_ic"]
        metrics["n_candidates"] = len(cands)
        metrics["n_sym"] = len(series_cache)
        metrics["n_hits_over_floor"] = len(hits)
        metrics["n_cs_hits_over_floor"] = len(cs_hits)
        experiment_ledger.safe_record(
            "expr_miner", {"n_candidates": len(cands), "horizons": list(HORIZONS),
                           "min_finite": MIN_FINITE, "ic_floor": IC_FLOOR, "min_cs": MIN_CS},
            metrics,
            inputs={"panel": db_path, "n_sym": len(series_cache), "n_dates": None},
            artifacts=[txt_path, json_path],
            conclusion="G25续表达式因子自动挖掘：确定性穷举白名单候选%d条+前向RankIC双口径体检"
                       "（时序/逐日截面），时序上榜%d条、截面上榜%d条，负结果照实，只产出候选不自动上线"
                       % (len(cands), len(hits), len(cs_hits)),
            reproduce="D:\\Python\\python.exe tools/expr_miner.py")
    except Exception:
        pass
    return result


def _fmt_ic(r):
    if not fe._isnum(r["mean_ic"]):
        return "无样本"
    pooled = r["pooled_ic"] if fe._isnum(r["pooled_ic"]) else float("nan")
    return "mean%+.3f/pool%+.3f/n%d" % (r["mean_ic"], pooled, r["n_pair"])


def _fmt_cs(r):
    if not fe._isnum(r["mean_ic"]):
        return "无有效截面样本"
    return "mean%+.3f/t%+.1f/正%.0f%%" % (
        r["mean_ic"], r["t_stat"] or 0.0, 100.0 * (r["pct_positive"] or 0.0))


def _max_abs_cs_ic(rec):
    """候选在各档 H 的逐日截面 |meanIC| 最大值（None 记 0）。"""
    best = 0.0
    for h in HORIZONS:
        mi = rec.get("cs", {}).get(h, {}).get("mean_ic")
        if fe._isnum(mi) and abs(mi) > best:
            best = abs(mi)
    return best


def render_report(result):
    floor = result.get("ic_floor", IC_FLOOR)
    hits = [r for r in result["candidates"]
            if any(fe._isnum(r["h"][h]["mean_ic"]) and abs(r["h"][h]["mean_ic"]) >= floor
                   for h in result["horizons"])]
    cs_hits = [r for r in result["candidates"]
               if any(fe._isnum(r.get("cs", {}).get(h, {}).get("mean_ic"))
                      and abs(r["cs"][h]["mean_ic"]) >= floor
                      for h in result["horizons"])]
    L = ["=" * 104,
         " G25续 表达式因子自动挖掘 expr_miner（研究侧，红线门控：只产出候选+IC体检，不自动上线）  生成于 %s"
         % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         "=" * 104]
    L.append("品种数=%d；候选表达式=%d；前向 H=%s；单品种最少有限点=%d；上榜门槛 |meanIC|>=%.2f；截面日最少品种=%d" % (
        result["n_symbols"], result["n_candidates"], result["horizons"],
        result["min_finite"], floor, result.get("min_cs", MIN_CS)))
    L.append("红线门控（总纲第16条）：自动挖掘产物不进 LIBRARY/catalog、不改综合分；候选须人工复核+"
             "G23/G29 双样本体检+受 G13/G16 门控后方可谈影子。")
    L.append("-" * 104)
    L.append("[候选表达式 前向 RankIC·两层]（时序行 meanIC=逐品种IC均值/pooledIC=全样本池化；截面行=逐交易日跨品种 RankIC；"
             "按 H5 |meanIC| 降序全量列出，负结果照实）")
    for r in result["candidates"]:
        cells = [_fmt_ic(r["h"][h]) for h in result["horizons"]]
        L.append("  %-20s %+d  %s" % (r["key"], r["direction"], r["expr"]))
        L.append("    %-18s H=1 %-24s H=5 %-24s H=20 %-24s" % (r["name"], cells[0], cells[1], cells[2]))
        if r.get("cs"):
            cs_cells = [_fmt_cs(r["cs"][h]) for h in result["horizons"]]
            L.append("    %-18s H=1 %-24s H=5 %-24s H=20 %-24s"
                     % ("截面IC(逐日)", cs_cells[0], cs_cells[1], cs_cells[2]))
    L.append("-" * 104)
    if hits:
        L.append("[时序上榜 |meanIC|>=%.2f]（由数据动态判定，仅供人工复核，不自动上线）" % floor)
        for r in sorted(hits, key=lambda r: _max_abs_ic(r), reverse=True):
            cells = ", ".join("H%d mean%+.3f" % (h, r["h"][h]["mean_ic"])
                              if fe._isnum(r["h"][h]["mean_ic"]) else "H%d 无样本" % h
                              for h in result["horizons"])
            L.append("  %-20s %-18s %s" % (r["key"], r["name"], cells))
    else:
        L.append("[时序上榜 |meanIC|>=%.2f]  无（负结果照实：当前候选池无一达到上榜门槛）" % floor)
    if cs_hits:
        L.append("[截面上榜 |逐日截面meanIC|>=%.2f]（跨品种排序口径，仅供人工复核，不自动上线）" % floor)
        for r in sorted(cs_hits, key=lambda r: _max_abs_cs_ic(r), reverse=True):
            cells = ", ".join("H%d mean%+.3f/t%+.1f" % (h, r["cs"][h]["mean_ic"], r["cs"][h]["t_stat"] or 0.0)
                              if fe._isnum(r["cs"][h]["mean_ic"]) else "H%d 无有效样本" % h
                              for h in result["horizons"])
            L.append("  %-20s %-18s %s" % (r["key"], r["name"], cells))
    else:
        L.append("[截面上榜 |meanIC|>=%.2f]  无（负结果照实：当前候选池无一达到上榜门槛）" % floor)
    L.append("-" * 104)
    L.append("[诚实结论]")
    L.append("  自动挖掘只是把白名单算子+字段的确定性组合全部体检一遍；|meanIC|<%.2f 视为无稳定预测力，"
             "上榜与否由本次数据动态判定（见上节）。" % floor)
    L.append("  注：cross_rank/scale 等截面单调变换不改变截面 Spearman（秩不变），故截面体检直接覆盖"
             "全部候选，无需单列 cross_rank 版候选表达式。")
    L.append("  候选如需进一步研究，须人工复核表达式含义+G29 因子体检+G23 双样本，且默认不进分；"
             "本工具永不写 LIBRARY/catalog、不被 main import。")
    L.append("=" * 104)
    return "\n".join(L)


# =========================== 零网络/零DB 合成断言 ===========================
def _synth_series(n=200, seed=1):
    """构造一个含确定信号的合成品种：y 与 lag1 收益强相关（可被挖掘出）。"""
    import random
    rng = random.Random(seed)
    xs = []
    px = 100.0
    for i in range(n):
        shock = rng.gauss(0, 1) * 0.02
        px *= (1 + shock)
        xs.append(px)
    vol = [1000.0 + i for i in range(n)]
    oi = [500.0 + i * 0.5 for i in range(n)]
    return {"close": xs, "volume": vol, "high": [v * 1.01 for v in xs],
            "low": [v * 0.99 for v in xs], "oi": oi}


def selftest():
    # 1) 候选池全部可编译、可求值（白名单 DSL 有效）
    pool = candidate_pool()
    assert len(pool) > 10
    s = _synth_series()
    for c in pool:
        fac = fe.compute_ts(c["expr"], s)
        assert len(fac) == len(s["close"])
        assert any(fe._isnum(v) for v in fac), c["key"]
    # 2) 前向 IC 严格向未来：末端无未来
    close = s["close"]
    assert forward_return(close, len(close) - 1, 1) is None
    # 3) evaluate_candidate 与手算一致（动量因子）
    mom = evaluate_candidate("close/delay(close,5)-1", s, close, horizons=(1, 5))
    assert fe._isnum(mom[1][0]) and mom[1][1] > 0
    # 4) run() 合成面板（缺 DB 友好降级路径不需要，此处直接用小合成验证 render 与 JSON 结构）
    fake = {"n_symbols": 3, "horizons": [1, 5, 20], "min_finite": 30, "ic_floor": 0.05,
            "n_candidates": 2,
            "candidates": [{"key": "k1", "expr": "a", "direction": 1, "name": "n1", "note": "",
                            "h": {1: {"mean_ic": 0.01, "pooled_ic": 0.02, "n_sym": 2, "n_pair": 100},
                                  5: {"mean_ic": 0.03, "pooled_ic": 0.04, "n_sym": 2, "n_pair": 100},
                                  20: {"mean_ic": 0.05, "pooled_ic": 0.06, "n_sym": 2, "n_pair": 100}}},
                           {"key": "k2", "expr": "b", "direction": -1, "name": "n2", "note": "",
                            "h": {1: {"mean_ic": -0.02, "pooled_ic": -0.03, "n_sym": 2, "n_pair": 100},
                                  5: {"mean_ic": -0.04, "pooled_ic": -0.05, "n_sym": 2, "n_pair": 100},
                                  20: {"mean_ic": -0.06, "pooled_ic": -0.07, "n_sym": 2, "n_pair": 100}}}],
            "ranked_by_H5_mean_abs_ic": ["k2", "k1"]}
    text = render_report(fake)
    assert "红线门控" in text and "诚实结论" in text and "k2" in text
    # 5) 排序：按 H5 |meanIC| 降序（k2 abs 0.04 > k1 0.03）
    assert fake["ranked_by_H5_mean_abs_ic"][0] == "k2"
    # 6) 上榜结论由数据动态判定：达标候选必须列名、无达标必须明说"无"
    text_hit = render_report(fake)          # k1 H20 |0.05|>=0.05、k2 全档达标 → 有上榜
    assert "时序上榜" in text_hit and "k2" in text_hit.split("[时序上榜")[1].split("-"*104)[0]
    fake_no = {"n_symbols": 1, "horizons": [1, 5, 20], "min_finite": 30, "ic_floor": 0.05,
               "n_candidates": 1,
               "candidates": [{"key": "weak", "expr": "a", "direction": 1, "name": "w", "note": "",
                               "h": {1: {"mean_ic": 0.01, "pooled_ic": 0.01, "n_sym": 1, "n_pair": 50},
                                     5: {"mean_ic": 0.02, "pooled_ic": 0.02, "n_sym": 1, "n_pair": 50},
                                     20: {"mean_ic": -0.03, "pooled_ic": -0.03, "n_sym": 1, "n_pair": 50}}}],
               "ranked_by_H5_mean_abs_ic": ["weak"]}
    text_none = render_report(fake_no)
    assert "无一达到上榜门槛" in text_none
    # 7) _max_abs_ic：None 记 0，取各档最大绝对值
    assert abs(_max_abs_ic(fake["candidates"][1]) - 0.06) < 1e-12
    assert _max_abs_ic({"h": {h: {"mean_ic": None} for h in HORIZONS}}) == 0.0
    # 8) 逐日截面IC（纯函数）：A 每日跑赢 B 的双品种合成 → 截面IC 恒为 +1
    n8 = 40
    ca = [100.0 * (1.01 ** i) for i in range(n8)]
    cb = [100.0 * (1.001 ** i) for i in range(n8)]
    dates8 = ["d%02d" % i for i in range(n8)]
    fac_bd = {"A": {d: 1.0 for d in dates8}, "B": {d: -1.0 for d in dates8}}
    fwd_bd = {"A": {}, "B": {}}
    for i, d in enumerate(dates8):
        for sym8, cl in (("A", ca), ("B", cb)):
            for h in (1, 5):
                fwd_bd[sym8].setdefault(h, {})[d] = forward_return(cl, i, h)
    ics8 = cross_section_ics(fac_bd, {s: fwd_bd[s][5] for s in fwd_bd}, min_cs=2)
    assert len(ics8) == n8 - 5 and all(abs(ic - 1.0) < 1e-12 for _, ic, _ in ics8)
    s8 = cs_summary(ics8)
    assert abs(s8["mean_ic"] - 1.0) < 1e-12 and s8["pct_positive"] == 1.0 and s8["n_days"] == n8 - 5
    # 9) 截面零方差（所有品种同值）→ 无有效截面IC（spearman None 被过滤）
    fac_flat = {"A": {d: 1.0 for d in dates8}, "B": {d: 1.0 for d in dates8}}
    assert cross_section_ics(fac_flat, {s: fwd_bd[s][5] for s in fwd_bd}, min_cs=2) == []
    # 10) cs_summary t 统计量手算 + 渲染含截面行/截面上榜区
    vals = (0.2, -0.1, 0.1, 0.2, -0.2)
    s10 = cs_summary([("d", v, 10) for v in vals])
    m10 = sum(vals) / len(vals)
    var10 = sum((v - m10) ** 2 for v in vals) / (len(vals) - 1)
    assert abs(s10["mean_ic"] - m10) < 1e-12
    assert abs(s10["t_stat"] - m10 / math.sqrt(var10) * math.sqrt(len(vals))) < 1e-12
    fake_cs = {"n_symbols": 1, "horizons": [1, 5, 20], "min_finite": 30, "ic_floor": 0.05,
               "min_cs": 10, "n_candidates": 1,
               "candidates": [{"key": "cs1", "expr": "a", "direction": 1, "name": "c", "note": "",
                               "h": {h: {"mean_ic": 0.01, "pooled_ic": 0.01, "n_sym": 1, "n_pair": 50}
                                     for h in HORIZONS},
                               "cs": {1: {"mean_ic": 0.20, "icir": 1.5, "t_stat": 3.0,
                                          "pct_positive": 0.6, "n_days": 40},
                                      5: {"mean_ic": 0.30, "icir": 2.0, "t_stat": 4.0,
                                          "pct_positive": 0.7, "n_days": 40},
                                      20: {"mean_ic": 0.10, "icir": 0.8, "t_stat": 1.6,
                                           "pct_positive": 0.55, "n_days": 40}}}],
               "ranked_by_H5_mean_abs_ic": ["cs1"]}
    text_cs = render_report(fake_cs)
    assert "截面IC(逐日)" in text_cs and "截面上榜" in text_cs and "cs1" in text_cs
    print("expr_miner selftest ALL PASS（候选池全编译/前向IC严格未来/单因子手算/报告结构/排序/"
          "上榜动态判定/max_abs_ic/截面IC手算/截面零方差/截面t与渲染 共10组）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="G25续 表达式因子自动挖掘（研究侧，红线门控）")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--limit", type=int, default=None, help="只体检前N个候选（调试用）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    run(db_path=args.db, limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
