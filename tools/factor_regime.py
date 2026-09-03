# -*- coding: utf-8 -*-
r"""G29续（第39轮）因子的 regime 分层 / 换手稳定性 / 衰减形态 tools/factor_regime.py。

factor_health 回答"因子现在还有没有力、稳不稳、指数半衰期多久"，本工具补它刻意留下的三块：
1) **regime 分层 IC**：因子是否只在某种市场状态下有效？牛/熊/震荡（面板已PIT落库的 ret126 判）×
   高/中/低波（hv60 在过去 REGIME_VOL_LOOKBACK 日的 ts_rank，只用过去、PIT）分桶，桶内算前向 RankIC；
2) **因子持续性/隐含换手**：因子滚动分位（ts_rank）在再平衡间隔 k=1/5/20 日上的秩自相关与平均|分位变动|
   （换手代理，越低越稳、调仓成本越小）；
3) **幂律 vs 指数衰减形态**：同一 IC(H) 期限曲线分别拟合 ln|IC|=a−H/τ（指数，半衰期 τ·ln2）与
   ln|IC|=a−β·lnH（幂律），比对数空间 R² 选更优形态（不预设一定是指数）。
纯标准库、零网络、只读 G21 面板（mode=ro），复用 factor_health 日频层/factor_eval.spearman/
factor_expr 时序算子（G25引擎）/panel_builder；不接 main、不改任何线上权重与综合分。
"""
import argparse
import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import config                       # noqa: E402
import factor_expr as fx            # noqa: E402  G25 引擎：ts_rank
import factor_eval as feval         # noqa: E402  spearman
import factor_health as fh          # noqa: E402  forward_map/daily_factor_curve/fit_exp_halflife/rows_by_symbol
import panel_builder as pb          # noqa: E402

DEFAULT_DB = os.path.join(_ROOT, "cache", "research_panel.db")
LN2 = math.log(2.0)
TREND_LABELS = ("up", "down", "flat")
VOL_LABELS = ("low", "mid", "high")


# =========================== 纯函数：regime 标签（PIT、只用过去） ===========================
def trend_labels(rows, field=None, flat=None):
    """逐行牛/熊/震荡：用面板已 PIT 落库的趋势字段（默认 ret126），|x|<flat 为 flat。"""
    field = field or config.REGIME_TREND_FIELD
    flat = config.REGIME_TREND_FLAT if flat is None else flat
    out = []
    for r in rows:
        v = r.get(field)
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            out.append(None)
        elif v > flat:
            out.append("up")
        elif v < -flat:
            out.append("down")
        else:
            out.append("flat")
    return out


def vol_labels(rows, field=None, lookback=None, low=None, high=None):
    """逐行高/中/低波：hv 序列在过去 lookback 日的 ts_rank（G25引擎，只用尾窗、无未来）。"""
    field = field or config.REGIME_VOL_FIELD
    lookback = lookback or config.REGIME_VOL_LOOKBACK
    low = config.REGIME_VOL_LOW if low is None else low
    high = config.REGIME_VOL_HIGH if high is None else high
    hv = [r.get(field) for r in rows]
    rk = fx.compute_ts("ts_rank(%s,%d)" % (field, lookback), {field: hv})
    out = []
    for z in rk:
        if not fx._isnum(z):
            out.append(None)
        elif z < low:
            out.append("low")
        elif z > high:
            out.append("high")
        else:
            out.append("mid")
    return out


def _isnum(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# =========================== 纯函数：regime 分层 IC ===========================
def compute_labels(rows_by_sym):
    """每个品种只算一次 (trend标签, vol标签)，避免按因子/期限重复计算滚动 ts_rank。"""
    out = {}
    for sym, rows in rows_by_sym.items():
        rows = sorted(rows, key=lambda r: r["date"])
        out[sym] = (rows, trend_labels(rows), vol_labels(rows))
    return out


def regime_stratified_ic(rows_by_sym, factor, horizon, min_n=None, labels=None):
    """跨品种池化，按 trend/vol/组合 分桶算前向 horizon 日 RankIC；返回 {bucket: {ic,n}}。

    labels=compute_labels(rows_by_sym) 可预计算复用；不给则现算（单因子调用时方便）。
    """
    min_n = config.REGIME_MIN_N if min_n is None else min_n
    buckets = {}

    def add(key, fv, fwd):
        buckets.setdefault(key, []).append((fv, fwd))

    labels = labels if labels is not None else compute_labels(rows_by_sym)
    for sym, (rows, tl, vl) in labels.items():
        closes = [r["c"] for r in rows]
        fwd = fh.forward_map(closes, (horizon,))[horizon]
        for t, r in enumerate(rows):
            fv = r.get(factor)
            if not _isnum(fv) or fwd[t] is None:
                continue
            add("ALL", fv, fwd[t])
            if tl[t]:
                add("trend_" + tl[t], fv, fwd[t])
            if vl[t]:
                add("vol_" + vl[t], fv, fwd[t])
            if tl[t] in ("up", "down") and vl[t] in ("low", "high"):
                add("%s_%s" % (tl[t], vl[t]), fv, fwd[t])
    out = {}
    for key, pairs in buckets.items():
        n = len(pairs)
        ic = feval.spearman([p[0] for p in pairs], [p[1] for p in pairs]) if n >= min_n else None
        out[key] = {"ic": ic, "n": n}
    return out


# =========================== 纯函数：因子持续性 / 隐含换手 ===========================
def factor_persistence(rows_by_sym, factor, lags=None, win=None):
    """因子滚动分位在各再平衡间隔的秩自相关与平均|分位变动|（换手代理，0~1 越低越稳）。"""
    lags = lags or config.REGIME_TURNOVER_LAGS
    win = win or config.REGIME_RANK_WIN
    agg = {k: {"xa": [], "ya": [], "absdiff": []} for k in lags}
    for sym, rows in rows_by_sym.items():
        rows = sorted(rows, key=lambda r: r["date"])
        raw = [r.get(factor) for r in rows]
        # 用 G25 引擎把原始因子转成"过去 win 日分位"，跨品种可比、PIT
        rk = fx.compute_ts("ts_rank(%s,%d)" % (factor, win), {factor: raw})
        for k in lags:
            for t in range(k, len(rk)):
                if _isnum(rk[t]) and _isnum(rk[t - k]):
                    agg[k]["xa"].append(rk[t - k])
                    agg[k]["ya"].append(rk[t])
                    agg[k]["absdiff"].append(abs(rk[t] - rk[t - k]))
    out = {}
    for k in lags:
        a = agg[k]
        n = len(a["xa"])
        out[k] = {
            "autocorr": feval.spearman(a["xa"], a["ya"]) if n >= config.REGIME_MIN_N else None,
            "turnover": (sum(a["absdiff"]) / n) if n else None, "n": n}
    return out


# =========================== 纯函数：衰减形态（指数 vs 幂律） ===========================
def _ols(xs, ys):
    """一元 OLS，返回 (slope, intercept, r2)；方差退化返回 None。"""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 1e-15:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-15 else 1.0
    return b, a, r2


def fit_decay_shapes(horizons, curve):
    """同一 |IC(H)| 曲线分别拟合指数/幂律，返回两形态参数与 r2、按 r2 选 prefer；数据不足返 None。"""
    hs, lnic = [], []
    for H in horizons:
        ic = (curve.get(H) or {}).get("ic")
        if _isnum(ic) and abs(ic) > 1e-4:
            hs.append(float(H)); lnic.append(math.log(abs(ic)))
    if len(hs) < 3:
        return None
    exp_fit = _ols(hs, lnic)
    pow_fit = _ols([math.log(h) for h in hs], lnic)
    res = {"n_points": len(hs), "exp": None, "power": None, "prefer": None}
    if exp_fit and exp_fit[0] < 0:
        b, a, r2 = exp_fit
        res["exp"] = {"slope": b, "intercept": a, "r2": r2, "half_life": -LN2 / b}
    if pow_fit and pow_fit[0] < 0:  # ln|IC|=a−β·lnH，OLS 斜率为负，β=−slope（>0=衰减）
        b, a, r2 = pow_fit
        res["power"] = {"beta": -b, "intercept": a, "r2": r2}
    cands = []
    if res["exp"]:
        cands.append(("exp", res["exp"]["r2"]))
    if res["power"]:
        cands.append(("power", res["power"]["r2"]))
    if cands:
        cands.sort(key=lambda kv: kv[1], reverse=True)
        res["prefer"] = cands[0][0]
    return res


# =========================== 汇总与报告 ===========================
def analyze_factor(rows_by_sym, factor, horizons=None, decay_h=None, labels=None):
    horizons = horizons or config.REGIME_HORIZONS
    decay_h = decay_h or config.REGIME_DECAY_H
    rec = {"factor": factor, "regime_ic": {}, "persistence": {}, "decay_shape": None,
           "overall_curve": None}
    for H in horizons:
        rec["regime_ic"][H] = regime_stratified_ic(rows_by_sym, factor, H, labels=labels)
    rec["persistence"] = {str(k): v for k, v in factor_persistence(rows_by_sym, factor).items()}
    curve = fh.daily_factor_curve(rows_by_sym, factor, decay_h)
    rec["overall_curve"] = {str(H): curve[H] for H in decay_h}
    rec["decay_shape"] = fit_decay_shapes(decay_h, curve)
    return rec


def run(db_path=DEFAULT_DB, txt_path=None, json_path=None, verbose=True):
    txt_path = txt_path or config.REGIME_FILE
    json_path = json_path or config.REGIME_JSON
    store = pb.PanelStore(db_path)
    syms = sorted(store.symbols())
    rows = store.load_all() if hasattr(store, "load_all") else _load_all(store, syms)
    bysym = fh.rows_by_symbol(rows)
    labels = compute_labels(bysym)     # 每品种 regime 标签只算一次（滚动 ts_rank 较贵）
    factors = config.HEALTH_DAILY_FACTORS
    results = {f: analyze_factor(bysym, f, labels=labels) for f in factors}

    L = []
    L.append("=" * 100)
    L.append("G29续 因子 regime 分层 / 换手稳定性 / 衰减形态 factor_regime（纯离线读 G21 面板，只研究不改权重）")
    L.append("品种=%d；趋势=面板ret126(±%.0f%%判震荡)，波动=hv60过去%d日ts_rank分低/中/高；分桶IC需n≥%d"
             % (len(syms), config.REGIME_TREND_FLAT * 100, config.REGIME_VOL_LOOKBACK, config.REGIME_MIN_N))
    for f in factors:
        rec = results[f]
        L.append("-" * 100)
        L.append("● %s" % f)
        for H in config.REGIME_HORIZONS:
            b = rec["regime_ic"][H]
            def g(k, key="ic"):
                x = b.get(k)
                if not x or x[key] is None:
                    return "--"
                return ("%+.3f" % x[key]) if key == "ic" else str(x[key])
            L.append("  H=%2d 全样本IC=%s(n%s) | 牛%s/熊%s/震%s | 低波%s/中波%s/高波%s | 牛低波%s/牛高波%s/熊低波%s/熊高波%s"
                     % (H, g("ALL"), g("ALL", "n"), g("trend_up"), g("trend_down"), g("trend_flat"),
                        g("vol_low"), g("vol_mid"), g("vol_high"),
                        g("up_low"), g("up_high"), g("down_low"), g("down_high")))
        ps = []
        for k in config.REGIME_TURNOVER_LAGS:
            p = rec["persistence"][str(k)]
            ac = "%+.3f" % p["autocorr"] if _isnum(p["autocorr"]) else "--"
            tv = "%.3f" % p["turnover"] if _isnum(p["turnover"]) else "--"
            ps.append("lag%d自相关%s/换手%s" % (k, ac, tv))
        L.append("  持续性: " + "，".join(ps))
        ds = rec["decay_shape"]
        if ds:
            e = ds["exp"]; p = ds["power"]
            et = ("指数R²=%.3f/半衰期%.1f日" % (e["r2"], e["half_life"])) if e else "指数不衰减"
            pt = ("幂律R²=%.3f/β=%.3f" % (p["r2"], p["beta"])) if p else "幂律不成立"
            L.append("  衰减形态: %s；%s → 更接近【%s】" % (et, pt, ds["prefer"] or "无"))
        else:
            L.append("  衰减形态: 有效点不足，不拟合")
    L.append("-" * 100)
    L.append("读法：regime 间 IC 差异大=因子只在特定市场状态有效；换手随 lag 降得慢/自相关高=信号稳、调仓成本低；"
             "幂律优于指数=长尾慢衰减（远月仍有残余），指数优于幂律=快速指数遗忘。research 结论不进综合分。")
    text = "\n".join(L)
    if verbose:
        print(text)
    os.makedirs(os.path.dirname(txt_path), exist_ok=True)
    with open(txt_path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(text + "\n")
    payload = {"n_symbols": len(syms), "factors": results}
    with open(json_path, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(payload, fp, ensure_ascii=False, allow_nan=False, indent=1)
    return payload


def _load_all(store, syms):
    out = []
    for s in syms:
        out.extend(store.load_rows(s))
    return out


# =========================== 零网络/零DB 合成断言 ===========================
def _mk_rows(n=300):
    """造两品种行：因子 g 只在 up 趋势(=ret126>2%)时对未来20日有 +1 单调预测，其余状态无预测力。"""
    rows = []
    for sym, phase in (("AA", 0), ("BB", 5)):
        closes = [100.0]
        for t in range(1, n):
            # 价格分段：前半段走牛（稳定上行），后半段走熊
            drift = 0.004 if t < n * 0.6 else -0.004
            closes.append(closes[-1] * (1 + drift))
        for t in range(n):
            ret126 = (closes[t] / closes[t - 126] - 1) if t >= 126 else None
            hv60 = 0.15 + 0.0004 * ((t + phase) % 60)
            # g 在 up 段与"未来20日收益"完全单调一致；down/flat 段为固定常数(无区分度)
            if ret126 is not None and ret126 > 0.02 and t + 20 < n:
                g = closes[t + 20] / closes[t] - 1
            else:
                g = 0.5
            rows.append({"sym": sym, "date": "2025-%02d-%02d" % ((t // 28) + 1, (t % 28) + 1),
                         "c": closes[t], "ret126": ret126, "hv60": hv60, "g": g})
    return rows


def selftest():
    # 1) trend 标签边界
    rows = [{"ret126": x} for x in (None, 0.0, 0.03, -0.03, 0.01)]
    assert trend_labels(rows, flat=0.02) == [None, "flat", "up", "down", "flat"]
    # 2) vol 标签：单调上升 hv 的 ts_rank 从 None→low→high，全部落在三档之一
    vrows = [{"hv60": float(i)} for i in range(300)]
    vl = vol_labels(vrows, lookback=252)
    assert vl[0] is None and vl[-1] == "high" and set(x for x in vl if x) <= {"low", "mid", "high"}
    # 3) regime 分层：构造因子只在 up 有效 → up 桶 IC 接近+1、down 桶样本区分度弱
    rows = _mk_rows()
    bysym = fh.rows_by_symbol(rows)
    strat = regime_stratified_ic(bysym, "g", 20, min_n=10)
    assert strat["ALL"]["ic"] is not None
    assert strat.get("trend_up") and strat["trend_up"]["ic"] and strat["trend_up"]["ic"] > 0.9
    # 4) 持续性：平滑慢变因子 lag1 秩自相关很高(>0.95)、换手随再平衡间隔增大
    mono = []
    for t in range(300):
        mono.append({"sym": "X", "date": "d%03d" % t, "c": 100.0 + t,
                     "ret126": 0.05, "hv60": 0.2, "m": math.sin(t / 15.0)})
    pers = factor_persistence(fh.rows_by_symbol(mono), "m", lags=(1, 5), win=60)
    assert pers[1]["autocorr"] is not None and pers[1]["autocorr"] > 0.95
    assert 0.0 <= pers[1]["turnover"] <= 1.0 and pers[5]["turnover"] >= pers[1]["turnover"]
    # 5) 衰减形态：构造指数衰减曲线 → exp R²≈1 且 prefer=exp；幂律曲线 → prefer=power
    hs = (1, 2, 3, 5, 10, 20, 40, 60)
    exp_curve = {H: {"ic": math.exp(-H / 20.0)} for H in hs}
    fit_e = fit_decay_shapes(hs, exp_curve)
    assert fit_e["prefer"] == "exp" and fit_e["exp"]["r2"] > 0.999
    assert abs(fit_e["exp"]["half_life"] - 20.0 * LN2) < 1e-6
    pow_curve = {H: {"ic": H ** -0.5} for H in hs}
    fit_p = fit_decay_shapes(hs, pow_curve)
    assert fit_p["prefer"] == "power" and abs(fit_p["power"]["beta"] - 0.5) < 1e-9
    # 不衰减（常数）→ 两形态都不成立、prefer=None
    flat_curve = {H: {"ic": 0.1} for H in hs}
    assert fit_decay_shapes(hs, flat_curve)["prefer"] is None
    # 有效点不足 → None
    assert fit_decay_shapes((1, 2), {1: {"ic": 0.1}, 2: {"ic": 0.05}}) is None
    # 6) analyze_factor 端到端不崩、键齐
    rec = analyze_factor(bysym, "g", horizons=(20,), decay_h=(1, 5, 20, 40, 60))
    assert 20 in rec["regime_ic"] and "persistence" in rec
    print("factor_regime selftest ALL PASS（trend/vol标签PIT边界、regime分层IC只在有效桶显著、"
          "因子秩自相关/换手、指数vs幂律衰减形态择优与不衰减/样本不足安全降级、端到端 共6组）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="G29续 因子regime分层/换手/衰减形态（纯离线读面板）")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    run(db_path=args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
