# -*- coding: utf-8 -*-
r"""G29（第37轮）因子体检 factor health——给每个因子一张"健康卡"，纯标准库、研究/监控记录层，不接 main、不改交易、不改综合分。

回答三个问题：①因子现在还有没有预测力（IC 及其块自助置信区间，保留时序自相关）；②预测力稳不稳
（滚动 IC、连续弱/翻转窗=失效预警）；③预测力衰减多快（日频层读 G21 标准面板，IC 期限曲线+指数半衰期）。

两层、数据源不同（刻意分开，避免口径混淆）：
  · 事件层：信号 9 part ⨝ signal_outcomes（复用 attribution.load_events，方向化暴露 x=part×dir，同 factor_eval），
    三周期 30/120/1440；给整体 RankIC、滚动IC、block bootstrap CI、多空/轻仓·分批·强信号分层（可得的 regime 代理）。
  · 日频层：读 cache/research_panel.db（G21 面板，已 PIT/复权），对 ret*/tsmom* 等日频因子算各未来 H 日的
    池化 RankIC 与 Q5-Q1 价差，并拟合 IC(H)=A·exp(-H/τ) 的半衰期 τ·ln2（不衰减时如实返回 None）。

输出 reports/factor_health.txt + .json（sidecar 无 NaN）。用法（项目根）：
  D:\Python\python.exe tools\factor_health.py                 # 事件层(monitor.db)+日频层(面板)
  D:\Python\python.exe tools\factor_health.py --no-daily      # 只事件层（面板缺失时）
  D:\Python\python.exe tools\factor_health.py --selftest
"""
import argparse
import json
import math
import os
import random
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import config  # noqa: E402
import factor_eval as fe  # noqa: E402  spearman/quantile_buckets
import attribution as at  # noqa: E402  load_events（只读 monitor.db）
import panel_builder as pb  # noqa: E402  日频层读 G21 面板

LN2 = math.log(2.0)


# =========================== 纯函数：事件层（可合成断言） ===========================
def factor_pairs(events, factor):
    """事件集 → 某因子的 (方向化暴露 x, 方向收益 y) 时间有序对，滤 None/非有限。"""
    out = []
    for e in events:
        x = e.get("x", {}).get(factor)
        y = e.get("y")
        if x is None or y is None:
            continue
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) \
                and math.isfinite(x) and math.isfinite(y):
            out.append((float(x), float(y)))
    return out


def rolling_ic(pairs, window, step):
    """时间有序 (x,y) 上以 window 为窗、step 为步长滑窗，返回 [(末位下标, RankIC, n)]。"""
    out = []
    n = len(pairs)
    if n < window or window <= 0:
        return out
    for a in range(0, n - window + 1, step):
        seg = pairs[a:a + window]
        out.append((a + window - 1, fe.spearman([p[0] for p in seg], [p[1] for p in seg]), len(seg)))
    return out


def block_bootstrap_ic(pairs, n_boot, block, seed):
    """块自助（保留时序自相关）：有放回抽连续块拼回等长，重算 RankIC；返回 (p5,p50,p95,同号概率)。

    确定性种子、纯标准库；样本不足返回 None 元组。
    """
    n = len(pairs)
    if n < block + 2 or block <= 0:
        return None
    rng = random.Random(seed)
    n_blocks = math.ceil(n / block)
    point = fe.spearman([p[0] for p in pairs], [p[1] for p in pairs])
    ics = []
    xs_all = [p[0] for p in pairs]
    ys_all = [p[1] for p in pairs]
    for _ in range(n_boot):
        bx, by = [], []
        for _b in range(n_blocks):
            start = rng.randint(0, n - block)
            for j in range(start, start + block):
                bx.append(xs_all[j]); by.append(ys_all[j])
        bx, by = bx[:n], by[:n]
        ics.append(fe.spearman(bx, by))
    ics.sort()
    pct = lambda q: ics[min(len(ics) - 1, max(0, int(round(q * (len(ics) - 1)))))]
    same = sum(1 for v in ics if (v >= 0) == (point >= 0)) / len(ics)
    return {"p5": pct(0.05), "p50": pct(0.50), "p95": pct(0.95), "prob_same_sign": same,
            "point": point, "n_boot": len(ics)}


def max_consecutive(flags):
    """布尔序列最长连续 True 长度。"""
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


def factor_event_health(events, factor, window=None, step=None, eps=None,
                        n_boot=None, block=None, seed=None, min_sample=None):
    """单因子单周期体检卡（纯函数）。verdict ∈ 健康/走弱/失效预警/样本不足。"""
    window = config.HEALTH_ROLL_WINDOW if window is None else window
    step = config.HEALTH_ROLL_STEP if step is None else step
    eps = config.HEALTH_IC_EPS if eps is None else eps
    n_boot = config.HEALTH_BOOT_B if n_boot is None else n_boot
    block = config.HEALTH_BLOCK if block is None else block
    seed = config.HEALTH_SEED if seed is None else seed
    min_sample = config.ATTR_MIN_SAMPLE if min_sample is None else min_sample
    pairs = factor_pairs(events, factor)
    n = len(pairs)
    card = {"factor": factor, "n": n, "ic": 0.0, "roll": [], "n_weak": 0, "n_flip": 0,
            "frac_fail": 0.0, "max_consec_fail": 0, "ci": None, "verdict": "样本不足"}
    if n < min_sample:
        return card
    ic = fe.spearman([p[0] for p in pairs], [p[1] for p in pairs])
    card["ic"] = ic
    roll = rolling_ic(pairs, window, step)
    card["roll"] = [{"end": r[0], "ic": round(r[1], 4)} for r in roll]
    weak = [1 for _, v, _ in roll if abs(v) < eps]
    flip = [1 for _, v, _ in roll if v * ic < 0]
    fail_flags = [(abs(v) < eps) or (v * ic < 0) for _, v, _ in roll]
    card["n_weak"] = sum(weak)
    card["n_flip"] = sum(flip)
    card["frac_fail"] = (sum(1 for f in fail_flags if f) / len(fail_flags)) if fail_flags else 0.0
    card["max_consec_fail"] = max_consecutive(fail_flags)
    card["ci"] = block_bootstrap_ic(pairs, n_boot, block, seed)
    # 裁决：连续失效窗达阈值=失效预警；CI 保守边也越过 eps 且同号率≥0.95=健康；其余=走弱/不稳定
    ci = card["ci"]
    ci_ok = ci is not None and ci["p5"] * ci["p95"] > 0
    if ci_ok:
        edge = ci["p5"] if ic >= 0 else ci["p95"]  # 朝 0 方向的保守边
        ci_strong = ((ic >= 0 and edge >= eps) or (ic < 0 and edge <= -eps)) \
            and ci["prob_same_sign"] >= 0.95
    else:
        ci_strong = False
    if card["max_consec_fail"] >= config.HEALTH_FAIL_WINDOWS:
        card["verdict"] = "失效预警"
    elif ci_strong and abs(ic) >= eps:
        # 统计上稳定非零即"健康"；IC 显著为负说明它是反向（反转）信号而非确认信号，如实标注
        card["verdict"] = "健康(反向)" if ic < 0 else "健康"
    else:
        card["verdict"] = "走弱/不稳定"
    return card


def regime_proxy(events, factor):
    """可得的 regime 代理：按信号方向（多/空）与信号档位（轻仓/分批/强信号）分桶 RankIC。"""
    out = {}
    for label, pred in (("多头", lambda e: e["dir"] == 1), ("空头", lambda e: e["dir"] == -1)):
        sub = [e for e in events if pred(e)]
        p = factor_pairs(sub, factor)
        out[label] = {"n": len(p), "ic": fe.spearman([x for x, _ in p], [y for _, y in p])} if p else {"n": 0, "ic": None}
    for label in ("轻仓", "分批", "强信号"):
        sub = [e for e in events if label in (e.get("band") or "")]
        p = factor_pairs(sub, factor)
        out[label] = {"n": len(p), "ic": fe.spearman([x for x, _ in p], [y for _, y in p])} if p else {"n": 0, "ic": None}
    return out


# =========================== 纯函数：日频层（读 G21 面板，IC 衰减半衰期） ===========================
def forward_map(closes, horizons):
    """{H: [与 closes 等长的 t→t+H 简单收益，不足为 None]}（无未来函数：只向未来取）。"""
    n = len(closes)
    out = {H: [None] * n for H in horizons}
    for t in range(n):
        for H in horizons:
            j = t + H
            if j < n and closes[t] > 0 and closes[j] > 0:
                out[H][t] = closes[j] / closes[t] - 1.0
    return out


def daily_factor_curve(rows_by_sym, factor, horizons):
    """池化跨品种：对每个未来 H 返回 (n, RankIC, Q5-Q1 均收益差)。纯函数。"""
    per_h = {H: [] for H in horizons}
    for sym, rows in rows_by_sym.items():
        rows = sorted(rows, key=lambda r: r["date"])
        closes = [r["c"] for r in rows]
        fwd = forward_map(closes, horizons)
        for t, r in enumerate(rows):
            fv = r.get(factor)
            if fv is None or not isinstance(fv, (int, float)) or not math.isfinite(fv):
                continue
            for H in horizons:
                if fwd[H][t] is not None:
                    per_h[H].append((fv, fwd[H][t]))
    curve = {}
    for H in horizons:
        pairs = per_h[H]
        if len(pairs) < 40:
            curve[H] = {"n": len(pairs), "ic": None, "q5q1": None}
            continue
        ic = fe.spearman([p[0] for p in pairs], [p[1] for p in pairs])
        buckets = fe.quantile_buckets(pairs, config.HEALTH_N_Q)
        q5q1 = buckets[-1][1] - buckets[0][1] if buckets[0][0] and buckets[-1][0] else None
        curve[H] = {"n": len(pairs), "ic": ic, "q5q1": q5q1}
    return curve


def fit_exp_halflife(horizons, curve):
    """拟合 ln|IC(H)| = a - H/τ → 半衰期=τ·ln2；不衰减(b>=0)/有效点不足返回 None（不编造）。"""
    hs, ys = [], []
    for H in horizons:
        ic = (curve.get(H) or {}).get("ic")
        if ic is not None and abs(ic) > 1e-4:
            hs.append(float(H)); ys.append(math.log(abs(ic)))
    if len(hs) < 3:
        return None
    n = len(hs)
    mx, my = sum(hs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in hs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(hs, ys))
    if sxx <= 1e-12:
        return None
    b = sxy / sxx                       # 斜率（衰减应<0）
    if b >= -1e-6:
        return None                     # 不衰减甚至增强：无半衰期
    tau = -1.0 / b
    hl = tau * LN2
    a = my - b * mx
    return {"tau": tau, "half_life": hl, "slope": b, "intercept": a}


def rows_by_symbol(rows):
    out = {}
    for r in rows:
        out.setdefault(r["sym"], []).append(r)
    return out


# =========================== 报告 ===========================
def _f3(x):
    return ("%+.3f" % x) if isinstance(x, (int, float)) else "--"


def build_event_section(events_by_h, factors, main_h):
    lines, side = [], {}
    for h in config.HEALTH_HORIZONS:
        evs = events_by_h.get(h, [])
        lines.append("【事件层 · %s分钟】n=%d" % ("次日" if h == 1440 else ("2小时" if h == 120 else "30分"), len(evs)))
        side[h] = {}
        head = "  %-8s %5s %7s %9s %5s %5s %6s %4s  %-10s" % (
            "因子", "n", "RankIC", "CI[p5,p95]", "弱窗", "翻窗", "连续失效", "同号", "裁决")
        lines.append(head)
        for f in factors:
            c = factor_event_health(evs, f)
            side[h][f] = {k: c[k] for k in ("n", "ic", "n_weak", "n_flip", "frac_fail",
                                            "max_consec_fail", "ci", "verdict")}
            ci = c["ci"] or {}
            ci_txt = "[%s,%s]" % (_f3(ci.get("p5")), _f3(ci.get("p95"))) if ci else "[--,--]"
            same = ("%.2f" % ci["prob_same_sign"]) if ci else "--"
            lines.append("  %-8s %5d %7.3f %9s %5d %5d %6d %4s  %-10s" % (
                f, c["n"], c["ic"], ci_txt, c["n_weak"], c["n_flip"], c["max_consec_fail"], same, c["verdict"]))
        # 主周期 regime 代理（只给一次，避免冗长）
        if h == main_h and evs:
            lines.append("  主周期 regime 代理（多/空 × 轻仓/分批/强信号 的 RankIC）：")
            for f in factors:
                rg = regime_proxy(evs, f)
                side[h][f]["regime"] = rg
                cells = " ".join("%s:n=%d,ic=%s" % (k, v["n"], _f3(v["ic"])) for k, v in rg.items())
                lines.append("    %-8s %s" % (f, cells))
        lines.append("")
    return "\n".join(lines), side


def build_daily_section(store):
    lines = ["【日频层 · G21 面板 IC 期限衰减】因子值→未来 H 交易日收益的池化 RankIC 与 Q5-Q1 价差"]
    side = {}
    rows = store.load_rows()
    bysym = rows_by_symbol(rows)
    hs = list(config.HEALTH_DECAY_H)
    head = "  %-11s " % "因子" + " ".join("H=%-7d" % H for H in hs) + "  半衰期(交易日)"
    lines.append(head)
    for f in config.HEALTH_DAILY_FACTORS:
        curve = daily_factor_curve(bysym, f, hs)
        fit = fit_exp_halflife(hs, curve)
        side[f] = {H: curve[H] for H in hs}
        side[f]["halflife"] = fit
        cells = " ".join("%-8s" % _f3(curve[H]["ic"]) for H in hs)
        hl = ("%.1f" % fit["half_life"]) if fit else "不衰减/不足"
        lines.append("  %-11s %s  %s" % (f, cells, hl))
    lines.append("  （括号为各 H 的 RankIC；半衰期由 |IC| 指数拟合，仅在单调衰减且≥3有效点时给出）")
    return "\n".join(lines), side


def build_report(events_by_h, factors, store=None):
    L = ["因子体检卡 G29 factor health  生成于 %s（研究/监控记录层，不改交易、不改综合分）"
         % datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "=" * 100,
         "口径：事件层=信号9part×方向 对 方向收益 的 RankIC（块长%d、自助%d次、滚动窗%d/步%d、连续%d窗失效预警、|IC|<%.2f为弱）；"
         % (config.HEALTH_BLOCK, config.HEALTH_BOOT_B, config.HEALTH_ROLL_WINDOW,
            config.HEALTH_ROLL_STEP, config.HEALTH_FAIL_WINDOWS, config.HEALTH_IC_EPS),
         "      日频层=G21标准面板(已PIT/复权) 日频因子对未来H交易日收益的池化RankIC与指数半衰期。", ""]
    event_txt, event_side = build_event_section(events_by_h, factors, config.HEALTH_MAIN_HORIZON)
    L.append(event_txt)
    daily_side = None
    if store is not None:
        daily_txt, daily_side = build_daily_section(store)
        L.append(daily_txt)
    text = "\n".join(L) + "\n"
    sidecar = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "event": {str(h): event_side[h] for h in event_side},
               "daily": daily_side}
    return text, sidecar


def load_event_data():
    return at.load_events(config.MONITOR_DB, horizons=tuple(config.HEALTH_HORIZONS))


def run(argv=None):
    ap = argparse.ArgumentParser(description="G29 因子体检")
    ap.add_argument("--no-daily", action="store_true", help="跳过日频层（不读面板）")
    ap.add_argument("--db", default=config.PANEL_DB)
    ap.add_argument("--out", default=config.HEALTH_FILE)
    ap.add_argument("--json", default=config.HEALTH_JSON)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    events_by_h = load_event_data()
    store = None
    if not args.no_daily and os.path.exists(args.db):
        store = pb.PanelStore(args.db)
    factors = list(config.ATTR_FACTOR_ORDER)
    text, sidecar = build_report(events_by_h, factors, store)
    if store is not None:
        store.close()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8-sig") as fh:
        fh.write(text)
    with open(args.json, "w", encoding="utf-8") as fh:
        json.dump(sidecar, fh, ensure_ascii=False, indent=1, allow_nan=False)
    print(text)
    print("报告 -> %s；JSON -> %s" % (args.out, args.json))
    return 0


# =========================== 零网络/零DB 合成断言 ===========================
def _lin_events(n, factor, slope, seed=1, noise=0.01):
    """y = slope·x + 小噪声 的合成事件（x 为该因子方向化暴露），时间有序。"""
    rng = random.Random(seed)
    evs = []
    for i in range(n):
        x = -1.0 + 2.0 * i / (n - 1)
        y = slope * x + (rng.random() - 0.5) * noise
        evs.append({"y": y, "x": {factor: x}, "dir": 1 if i % 2 == 0 else -1,
                    "sector": "黑色", "sym": "RB", "band": "分批" if i % 3 else "强信号",
                    "ts": "2026-08-%02d" % (i % 28 + 1), "horizon": 1440})
    return evs


def selftest():
    # 1) factor_pairs 滤 None/非有限
    evs = [{"y": 0.1, "x": {"A": 1.0}}, {"y": None, "x": {"A": 2.0}},
           {"y": 0.3, "x": {"A": None}}, {"y": float("nan"), "x": {"A": 1.0}}]
    assert len(factor_pairs(evs, "A")) == 1

    # 2) 强正相关→IC≈1、裁决健康；强负相关→IC≈-1
    pos = factor_event_health(_lin_events(120, "A", 1.0), "A", window=40, step=10,
                              n_boot=200, block=10, min_sample=40)
    assert pos["ic"] > 0.95 and pos["verdict"] == "健康", (pos["ic"], pos["verdict"])
    neg = factor_event_health(_lin_events(120, "A", -1.0), "A", window=40, step=10,
                              n_boot=200, block=10, min_sample=40)
    assert neg["ic"] < -0.95 and neg["ci"]["p95"] < 0

    # 3) 纯噪声→IC≈0、出现弱窗、不会误判健康
    rng = random.Random(7)
    noise_evs = [{"y": rng.random() - 0.5, "x": {"A": rng.random() - 0.5}, "dir": 1,
                  "band": "分批"} for _ in range(160)]
    nz = factor_event_health(noise_evs, "A", window=40, step=10, n_boot=100, block=10, min_sample=40)
    assert abs(nz["ic"]) < 0.25 and nz["verdict"] != "健康", nz["verdict"]

    # 4) 失效预警：前半强相关、后半完全无关（滚动窗连续走弱/翻转）
    strong = _lin_events(80, "A", 1.0, seed=2)
    flat = [{"y": rng.random() - 0.5, "x": {"A": rng.random() - 0.5}, "dir": 1, "band": "分批"} for _ in range(80)]
    decay = strong + flat
    dc = factor_event_health(decay, "A", window=40, step=10, n_boot=50, block=10, min_sample=40)
    assert dc["max_consec_fail"] >= 1

    # 5) 样本不足降级
    assert factor_event_health(_lin_events(10, "A", 1.0), "A", min_sample=40)["verdict"] == "样本不足"

    # 6) 块自助确定性（同种子两次一致）+ 同号概率
    pairs = factor_pairs(_lin_events(100, "A", 1.0), "A")
    c1 = block_bootstrap_ic(pairs, 100, 10, 123)
    c2 = block_bootstrap_ic(pairs, 100, 10, 123)
    assert c1 == c2 and c1["p5"] > 0 and c1["prob_same_sign"] > 0.9
    assert block_bootstrap_ic([(1, 2)], 100, 10, 1) is None

    # 7) 日频前向收益无未来函数 + 期限曲线 + 半衰期
    closes = [100.0 * (1.001 ** i) for i in range(120)]
    fwd = forward_map(closes, (1, 5, 20))
    assert fwd[5][0] is not None and abs(fwd[5][0] - (closes[5] / closes[0] - 1)) < 1e-12
    assert fwd[20][-1] is None and fwd[20][-21] is not None
    # 构造"因子=过去20日收益、价格有惯性"→ 短期IC正、随H拉长衰减
    rows = []
    for i, c in enumerate(closes):
        rows.append({"sym": "RB", "date": "2026-%03d" % i, "c": c,
                     "ret20": (c / closes[i - 20] - 1) if i >= 20 else None})
    bysym = {"RB": rows}
    curve = daily_factor_curve(bysym, "ret20", (1, 2, 3, 5, 10, 20, 40, 60))
    assert curve[1]["n"] > 40 and curve[60]["n"] < curve[1]["n"]
    # 指数半衰期：构造 |IC| 随 H 指数衰减的曲线
    dec = {H: {"ic": 0.6 * math.exp(-H / 10.0)} for H in (1, 2, 3, 5, 10, 20, 40)}
    fit = fit_exp_halflife((1, 2, 3, 5, 10, 20, 40), dec)
    assert abs(fit["half_life"] - 10.0 * LN2) < 0.5
    # 不衰减返回 None
    nod = {H: {"ic": 0.3} for H in (1, 2, 3, 5)}
    assert fit_exp_halflife((1, 2, 3, 5), nod) is None
    assert fit_exp_halflife((1,), {1: {"ic": 0.5}}) is None

    # 8) 端到端报告（合成事件、无面板）结构与 JSON 安全
    evh = {h: _lin_events(90, "技术共振", 0.8) for h in config.HEALTH_HORIZONS}
    text, side = build_report(evh, ["技术共振", "原油联动"], store=None)
    assert "因子体检卡" in text and "事件层" in text
    json.dumps(side, allow_nan=False)  # 无 NaN
    print("factor_health selftest ALL PASS（配对清洗/正负IC裁决/噪声不误判/失效预警/样本不足/"
          "块自助确定性/前向无未来/期限曲线与半衰期/端到端报告 共8组）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
