# -*- coding: utf-8 -*-
r"""G5④/G5⑤（第50轮，研究侧）组合层熔断阈值历史校准台 tools/circuit_review.py：
纯标准库、零网络、**只读** G21 面板（cache/research_panel.db，经 portfolio_lab 复现四方法日收益），
把第48轮 circuit_breaker 的单日浮亏阈值（warn2%/halt3%/delever5%）放到**真实历史日频净值曲线**上回放，
回答两个校准问题——①这些阈值历史上会触发多少次、是否过松/过频；②halt 触发之后市场是"继续跌"（停开避险有价值）
还是"随即反弹"（停开=误杀）。**只读研究、不接 main、不改 circuit_breaker 默认值/综合分/持仓/纸面成交**。

口径诚实边界（务必在报告写明）：
  - circuit_breaker 在实盘是**日内**状态机（当日日初权益→当前权益、当日粘性、跨日重置、一日多快照）；
    本台只有**日频**收盘净值，"单日浮亏"=当日收盘相对前一交易日收盘的损失（=-日收益），每日一根=每天重置，
    **无法体现日内触发后的当日锁定**，属保守的逐日代理，结论用于阈值数量级校准、不替代日内行为。
  - 日收益来自已比例复权主连面板、固定宇宙有幸存者偏差、未计手续费/滑点/保证金/换月；不构成投资建议。
出 reports/circuit_review.txt|.json，末尾经统一实验台账旁路登记一条。
"""
import argparse
import json
import os
import statistics
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import circuit_breaker as cb                   # noqa: E402 阈值默认值/分档口径单一事实源
import portfolio_lab as pl                     # noqa: E402 复用稠密面板+滚动样本外代理
import experiment_ledger as el                 # noqa: E402

REVIEW_TXT = os.path.join(_ROOT, "reports", "circuit_review.txt")
REVIEW_JSON = os.path.join(_ROOT, "reports", "circuit_review.json")
METHODS = ("equal", "inv_vol", "erc", "gmv")
METHOD_CN = {"equal": "等权", "inv_vol": "逆波动", "erc": "风险平价", "gmv": "最小方差"}
HORIZONS = (1, 3, 5, 10)                        # 触发后前瞻交易日
CALIB_THRESHOLD = 0.01                          # 条件远期分析的"校准观察档"：默认halt3%在日频分散组合0触发，
#                                                用1%档才能获得可统计样本（等权约18次）评估"大跌后续跌/反弹"
SWEEP_GRID = (0.005, 0.008, 0.01, 0.015, 0.02, 0.03)  # halt 阈值校准网格（覆盖有样本区到默认档）


# =========================== 纯函数（零网络，合成可断言） ===========================
def loss_of(ret):
    """日收益→单日损失分数（损失为正）：-ret。"""
    return -float(ret)


def forward_compound(daily, i, h):
    """触发日 i 之后 1..h 日的累计复利收益（不含触发当日）；后续不足 h 日返 None。"""
    if i < 0 or i + h >= len(daily):
        return None
    g = 1.0
    for k in range(1, h + 1):
        g *= (1.0 + daily[i + k])
    return g - 1.0


def _dist(vals):
    """一组收益的 n/均值/中位/下跌占比（<0 比例）。"""
    vals = [v for v in vals if v is not None]
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": None, "median": None, "down_rate": None}
    return {"n": n, "mean": sum(vals) / n, "median": statistics.median(vals),
            "down_rate": sum(1 for v in vals if v < 0) / n}


def threshold_events(daily, threshold):
    """逐日损失≥threshold 的事件（日频每日独立、相当于每日日切重置）。返回触发下标列表。"""
    out = []
    for i, r in enumerate(daily):
        if loss_of(r) >= threshold:
            out.append(i)
    return out


def conditional_forwards(daily, trigger_idx, horizons=HORIZONS):
    """触发点集合在各前瞻窗的远期收益分布；同时给全样本无条件基准分布作对照。"""
    cond, base = {}, {}
    max_h = max(horizons)
    for h in horizons:
        cv = [forward_compound(daily, i, h) for i in trigger_idx]
        # 无条件基准：所有"后面还有 h 日"的时点
        bv = [forward_compound(daily, i, h) for i in range(0, len(daily) - max_h)]
        cond[h] = _dist(cv)
        base[h] = _dist(bv)
    return {"conditional": cond, "baseline": base}


def level_counts(daily, thresholds):
    """按 (warn,halt,delever) 阈值统计三档穿越次数与触发下标（用 circuit_breaker 同口径分档）。"""
    cnt = {cb.NORMAL: 0, cb.WARN: 0, cb.HALT: 0, cb.DELEVER: 0}
    idx = {cb.WARN: [], cb.HALT: [], cb.DELEVER: []}
    for i, r in enumerate(daily):
        lv = cb.classify_level(loss_of(r), thresholds)
        cnt[lv] += 1
        if lv in idx:
            idx[lv].append(i)
    return cnt, idx


def sweep_halt(dates, daily, grid=SWEEP_GRID, horizons=HORIZONS):
    """halt 阈值网格：每档触发数/占比 + 各前瞻窗条件均值与下跌占比，供校准取舍。"""
    rows = []
    n = len(daily)
    for th in grid:
        ev = threshold_events(daily, th)
        cf = conditional_forwards(daily, ev, horizons)
        rows.append({"threshold": th, "n_trigger": len(ev), "share": len(ev) / n if n else 0.0,
                     "dates": [dates[i] for i in ev[:12]], "forward": cf["conditional"]})
    return rows


def analyze_method(dates, daily, *, warn=cb.DEFAULT_WARN, halt=cb.DEFAULT_HALT,
                   delever=cb.DEFAULT_DELEVER, calib=CALIB_THRESHOLD, horizons=HORIZONS):
    """单方法：默认三档穿越计数 + halt档与校准观察档触发后的条件远期 vs 无条件基准 + 阈值网格。"""
    thresholds = {"warn": warn, "halt": halt, "delever": delever}
    cnt, idx = level_counts(daily, thresholds)
    halt_fwd = conditional_forwards(daily, idx[cb.HALT], horizons)
    calib_idx = threshold_events(daily, calib)
    calib_fwd = conditional_forwards(daily, calib_idx, horizons)
    sweep = sweep_halt(dates, daily, horizons=horizons)
    return {"n_days": len(daily), "counts": cnt,
            "dates": {lv: [dates[i] for i in idx[lv][:12]] for lv in idx},
            "halt_forward": halt_fwd, "calib_threshold": calib,
            "calib_n": len(calib_idx), "calib_forward": calib_fwd, "sweep": sweep,
            "worst_day_loss": max((loss_of(r) for r in daily), default=0.0)}


def load_method_series(db_path):
    """经 portfolio_lab 复现四方法滚动样本外日收益，返回 (series, d0, d1, n_mat, n_universe, n_all)。"""
    return_map, _sectors, all_syms = pl.load_return_map(db_path)
    dates, syms, mat = pl.dense_matrix(return_map)
    proxy = pl.rolling_proxy(mat)
    out = {}
    for m in METHODS:
        idxs = proxy[m]["idx"]
        daily = proxy[m]["daily"]
        d = [dates[i] for i in idxs]
        out[m] = (d, daily)
    return out, dates[0], dates[-1], len(mat), len(syms), len(all_syms)


# =========================== 文本渲染 ===========================
def _pct(x, nd=2):
    return "NA" if x is None else ("%+.{}f%%".format(nd) % (x * 100))


def render(meta, per):
    L = []
    L.append("=" * 108)
    L.append("G5④ 组合层熔断阈值历史校准台 circuit_review（纯离线读 G21 面板，日频逐日代理，只读不改 circuit_breaker 默认/主链/持仓）")
    L.append("固定宇宙=%d/%d 品种、稠密 %s~%s 共%d日；滚动样本外曲线起于 lookback 后，各方法 %d 日；前瞻窗 %s 交易日"
             % (meta["n_universe"], meta["n_all"], meta["date_first"], meta["date_last"], meta["n_mat"],
                meta["n_proxy"], "/".join(str(h) for h in HORIZONS)))
    L.append("默认阈值 warn=%.0f%%/halt=%.0f%%/delever=%.0f%%（与 config.CIRCUIT_*、circuit_breaker 默认一致）"
             % (cb.DEFAULT_WARN * 100, cb.DEFAULT_HALT * 100, cb.DEFAULT_DELEVER * 100))
    L.append("-" * 108)
    L.append("【一】默认阈值下历史穿越次数（日频：单日损失≥阈值即一次，每日日切重置）")
    L.append("  %-8s %6s %6s %6s %8s %10s" % ("方法", "warn", "halt", "delever", "交易日", "最差单日"))
    for m in METHODS:
        c = per[m]["counts"]
        L.append("  %-8s %6d %6d %6d %8d %9.2f%%"
                 % (METHOD_CN[m], c[cb.WARN], c[cb.HALT], c[cb.DELEVER], per[m]["n_days"],
                    per[m]["worst_day_loss"] * 100))
    L.append("  读法：warn 是日常提示可较频；halt 一年(约243日)触发个位数~十几次属正常，若几十次=阈值过紧会频繁停开，0次=过松无意义。")
    L.append("-" * 108)
    L.append("【二】单日跌≥%.0f%% 后的前瞻收益：条件组 vs 全样本无条件基准（验证'停开避险'是否成立）"
             % (CALIB_THRESHOLD * 100))
    L.append("  （默认 halt=%.0f%% 在日频分散组合 0 触发、无法统计，故取 %.0f%% 校准观察档获得样本；n 过小仅供参考）"
             % (cb.DEFAULT_HALT * 100, CALIB_THRESHOLD * 100))
    for m in METHODS:
        hf = per[m]["calib_forward"]
        L.append("  ● %s（跌≥%.0f%% 触发 %d 次）"
                 % (METHOD_CN[m], CALIB_THRESHOLD * 100, per[m]["calib_n"]))
        L.append("    %5s | %-24s | %-24s | %-12s" % ("窗", "条件均值/中位/下跌占比", "基准均值/中位/下跌占比", "条件-基准"))
        for h in HORIZONS:
            cc, bb = hf["conditional"][h], hf["baseline"][h]
            if cc["n"] == 0:
                L.append("    T+%-3d | 无触发样本" % h)
                continue
            diff = None if cc["mean"] is None or bb["mean"] is None else cc["mean"] - bb["mean"]
            L.append("    T+%-3d | %s/%s/%.0f%%(n=%d) | %s/%s/%.0f%% | %s"
                     % (h, _pct(cc["mean"]), _pct(cc["median"]), (cc["down_rate"] or 0) * 100, cc["n"],
                        _pct(bb["mean"]), _pct(bb["median"]), (bb["down_rate"] or 0) * 100,
                        _pct(diff)))
    L.append("  读法：条件均值比基准更负、条件下跌占比更高 → 大跌后倾向续跌，halt 停开有避险价值；若条件组反而更正=大跌后均值回归，停开易误杀。")
    L.append("-" * 108)
    L.append("【三】halt 阈值网格校准（等权 equal 为代表；触发占比=次数/交易日，T+5 条件均值/下跌占比/样本n）")
    sw = per["equal"]["sweep"]
    L.append("  %-8s %8s %10s %14s %12s %6s" % ("halt阈值", "触发次数", "占交易日", "T+5条件均值", "T+5下跌占比", "n"))
    for row in sw:
        c5 = row["forward"][5]
        L.append("  %6.1f%% %8d %9.2f%% %14s %11s %6d"
                 % (row["threshold"] * 100, row["n_trigger"], row["share"] * 100,
                    _pct(c5["mean"]) if c5["mean"] is not None else "NA",
                    ("%.0f%%" % (c5["down_rate"] * 100)) if c5["down_rate"] is not None else "NA",
                    c5["n"]))
    L.append("-" * 108)
    L.append("诚实边界：日频收盘对收盘=逐日代理，无法复现 circuit_breaker 日内'当日粘性/一天多快照'，真实日内触发次数只会更多、锁定只在当日；")
    L.append("固定宇宙有幸存者偏差、未计手续费/滑点/保证金/换月；本结果只用于阈值数量级校准，不直接改 config 默认、不构成投资建议。")
    return "\n".join(L)


# =========================== 编排/台账/CLI ===========================
def run(db_path=None, txt_path=REVIEW_TXT, json_path=REVIEW_JSON, verbose=True):
    db_path = db_path or os.path.join(_ROOT, "cache", "research_panel.db")
    series, d0, d1, n_mat, n_universe, n_all = load_method_series(db_path)
    per = {}
    for m in METHODS:
        d, daily = series[m]
        per[m] = analyze_method(d, daily)
    meta = {"n_universe": n_universe, "n_all": n_all, "date_first": d0, "date_last": d1,
            "n_mat": n_mat, "n_proxy": len(series[METHODS[0]][1]),
            "warn": cb.DEFAULT_WARN, "halt": cb.DEFAULT_HALT, "delever": cb.DEFAULT_DELEVER,
            "calib_threshold": CALIB_THRESHOLD,
            "horizons": list(HORIZONS), "sweep_grid": list(SWEEP_GRID)}
    text = render(meta, per)
    if verbose:
        print(text)
    os.makedirs(os.path.dirname(txt_path), exist_ok=True)
    with open(txt_path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(text + "\n")
    payload = {"meta": meta, "per_method": per}
    with open(json_path, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(payload, fp, ensure_ascii=False, allow_nan=False, indent=1)
    try:
        eq = per["equal"]
        el.safe_record(
            "circuit_review",
            {"warn": cb.DEFAULT_WARN, "halt": cb.DEFAULT_HALT, "delever": cb.DEFAULT_DELEVER,
             "calib_threshold": CALIB_THRESHOLD,
             "horizons": list(HORIZONS), "sweep_grid": list(SWEEP_GRID),
             "panel_db": os.path.basename(db_path)},
            {m: {"warn": per[m]["counts"][cb.WARN], "halt": per[m]["counts"][cb.HALT],
                 "delever": per[m]["counts"][cb.DELEVER], "calib_n": per[m]["calib_n"],
                 "calib_t5_mean": per[m]["calib_forward"]["conditional"][5]["mean"],
                 "base_t5_mean": per[m]["calib_forward"]["baseline"][5]["mean"]}
             for m in METHODS},
            inputs=[db_path], artifacts=[txt_path, json_path],
            conclusion="固定宇宙%d品种 %s~%s：默认3%%halt日频0触发(分散组合最差单日%.2f%%)；1%%观察档等权%d次、T+5条件%s vs 基准%s"
                       % (n_universe, d0, d1, eq["worst_day_loss"] * 100, eq["calib_n"],
                          _pct(eq["calib_forward"]["conditional"][5]["mean"]),
                          _pct(eq["calib_forward"]["baseline"][5]["mean"])))
    except Exception:
        pass
    return payload


# =========================== 零网络/零DB 自测 ===========================
def selftest():
    # 1) loss/forward 手算
    assert abs(loss_of(-0.03) - 0.03) < 1e-12 and loss_of(0.02) < 0
    d = [0.0, 0.1, -0.1]
    assert abs(forward_compound(d, 0, 2) - (1.1 * 0.9 - 1)) < 1e-12
    assert forward_compound(d, 1, 2) is None and forward_compound(d, -1, 1) is None

    # 2) threshold_events：构造确定序列，第2/5日跌超3%
    daily = [0.01, -0.031, 0.02, -0.01, -0.04, 0.0]
    ev = threshold_events(daily, 0.03)
    assert ev == [1, 4]

    # 3) level_counts 三档分档（用 circuit_breaker 同口径）
    cnt, idx = level_counts(daily, {"warn": 0.02, "halt": 0.03, "delever": 0.05})
    assert cnt[cb.HALT] == 2 and cnt[cb.WARN] == 0 and idx[cb.HALT] == [1, 4]
    d2 = daily + [-0.06]
    cnt2, idx2 = level_counts(d2, {"warn": 0.02, "halt": 0.03, "delever": 0.05})
    assert cnt2[cb.DELEVER] == 1 and idx2[cb.DELEVER] == [6]

    # 4) conditional_forwards：构造"大跌后必续跌"序列，条件均值应显著负、下跌占比100%
    seq = [0.0, -0.04, -0.03, -0.02, 0.0, -0.04, -0.02, -0.01]
    ev3 = threshold_events(seq, 0.03)   # r<=-0.03：索引1(-.04)/2(-.03)/5(-.04)
    assert ev3 == [1, 2, 5]
    cf = conditional_forwards(seq, ev3, horizons=(1, 3))
    assert cf["conditional"][1]["n"] == 3
    assert cf["conditional"][1]["down_rate"] == 1.0 and cf["conditional"][1]["mean"] < 0
    # 基准样本数 = 所有后面还有 max_h=3 日的时点
    assert cf["baseline"][1]["n"] == len(seq) - 3

    # 5) 反弹序列：大跌次日全涨，条件 T+1 为正（熔断误杀情景能被识别）
    bounce = [0.0, -0.04, 0.03, 0.0, -0.04, 0.02, 0.0]
    evb = threshold_events(bounce, 0.03)
    cfb = conditional_forwards(bounce, evb, horizons=(1,))
    assert cfb["conditional"][1]["mean"] > 0 and cfb["conditional"][1]["down_rate"] == 0.0

    # 6) sweep_hart 网格单调：阈值越高触发越少
    dates = ["d%d" % i for i in range(len(daily))]
    sw = sweep_halt(dates, daily, grid=(0.02, 0.03, 0.04), horizons=(1,))
    assert sw[0]["n_trigger"] >= sw[1]["n_trigger"] >= sw[2]["n_trigger"]
    assert all(0 <= r["share"] <= 1 for r in sw)

    # 7) analyze_method 端到端结构完整、空序列安全
    am = analyze_method(dates, daily, horizons=(1, 3))
    assert am["counts"][cb.HALT] == 2 and len(am["sweep"]) == len(SWEEP_GRID)
    empty = analyze_method([], [], horizons=(1,))
    assert empty["counts"][cb.HALT] == 0 and empty["worst_day_loss"] == 0.0
    assert all(r["n_trigger"] == 0 for r in empty["sweep"])

    # 8) render 不崩且含三档标题
    meta = {"n_universe": 4, "n_all": 4, "date_first": "d0", "date_last": "d5",
            "n_mat": 6, "n_proxy": 6}
    per = {m: analyze_method(dates, daily, horizons=HORIZONS) for m in METHODS}
    txt = render(meta, per)
    for kw in ("【一】", "【二】", "【三】", "halt", "条件"):
        assert kw in txt
    print("circuit_review selftest ALL PASS（损失口径/远期复利/事件穿越/三档分档/续跌vs反弹条件分布/"
          "阈值网格单调/空序列安全/渲染 共8组）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="G5④ 组合层熔断阈值历史校准台（纯离线读面板）")
    ap.add_argument("--db", default=None)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    run(db_path=args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
