# -*- coding: utf-8 -*-
r"""G24（第54轮）微结构 / 持仓 / 季节因子族实验台 tools/microstructure_lab.py。

总纲 G24 长期排队的研究侧第一块（ΔOI/Amihud/特异波动/偏度/日历），与 factor_health/factor_regime/
portfolio_lab 同一套纪律：**纯标准库、零网络、只读 G21 标准研究面板（cache/research_panel.db，
mode=ro 语义、不改数据）、不接 main、不改任何线上权重与综合分**，结论只作研究、负结果诚实呈现。

本工具实现并前向检验五个日频微结构/持仓因子（全部 PIT，只用 t 及之前数据预测 t+H）：
  1) doi1 / doi5    持仓量 OI 的 1/5 日变化率（资金进出/仓位构建方向，面板有 oi 列）；
  2) amihud20       Amihud(2002) 非流动性 = 过去20日 mean(|ret1d| / 成交额(c*v))，越大越缺乏流动性；
  3) idiovol60      特异波动 = 过去60日"个股 vs 全市场等权"市场模型残差的标准差（剔除系统性波动）；
  4) skew60         过去60日日收益偏度（三阶标准化矩，左/右尾不对称）；
  5) 日历/季节      分自然月（及星期）的池化平均收益/上涨占比（经典商品季节性，非截面因子、单独成表）。
每个连续因子对未来 H=1/5/20 交易日做**跨品种池化 RankIC + 五档 Q5-Q1 价差 + 分档单调性**（复用
factor_eval.spearman/quantile_buckets、factor_health.forward_map，不重造轮子）。

**诚实数据缺口**：总纲 G24 还列了 HP/SP（hedging/speculative pressure 套保/投机压力），那需要
"交易者分类持仓"（商业/非商业，类 CFTC COT），本面板只有总持仓量 oi、没有分类持仓，故本轮不做、不编造，
仅在报告里标注，待 G22 期限/OI 采集拿到分类持仓再补。
"""
import argparse
import datetime as _dt
import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import factor_eval as feval          # noqa: E402  spearman/quantile_buckets
import factor_health as fh           # noqa: E402  forward_map/rows_by_symbol
import panel_builder as pb           # noqa: E402  PanelStore
import experiment_ledger as el       # noqa: E402  旁路台账

DEFAULT_DB = os.path.join(_ROOT, "cache", "research_panel.db")
LAB_TXT = os.path.join(_ROOT, "reports", "microstructure_lab.txt")
LAB_JSON = os.path.join(_ROOT, "reports", "microstructure_lab.json")

HORIZONS = (1, 5, 20)          # 前向持有期（交易日）
N_Q = 5                        # 分档数
MIN_PAIRS = 40                 # 池化对数下限（低于不给 IC，与 factor_health 一致）
AMIHUD_WIN = 20
IDIO_WIN = 60
SKEW_WIN = 60
# Amihud 原始量纲极小（|r|/成交额≈1e-9），展示乘 1e9 变成"每十亿名义的价格冲击"，正线性缩放不影响秩/IC
AMIHUD_SCALE = 1e9

# (因子键, 中文名, 方向解读)
FACTORS = (
    ("doi1", "持仓量变化OI(1日)", "OI骤增=资金进场"),
    ("doi5", "持仓量变化OI(5日)", "5日仓位构建"),
    ("amihud20", "Amihud非流动性20日", "值越大越缺流动性"),
    ("idiovol60", "特异波动60日", "剔除市场后的残差波动"),
    ("skew60", "收益偏度60日", "正=右尾/负=左尾"),
)


def _isnum(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# =========================== 纯函数：滚动因子（PIT、尾窗、无未来） ===========================
def pct_change(series, win):
    """对齐序列的 win 期变化率：out[t]=s[t]/s[t-win]-1，不足或非正/缺失为 None。纯函数不改入参。"""
    out = [None] * len(series)
    for t in range(len(series)):
        j = t - win
        if j >= 0 and _isnum(series[t]) and _isnum(series[j]) and series[j] > 0:
            out[t] = series[t] / series[j] - 1.0
    return out


def rolling_amihud(rets, closes, vols, win=AMIHUD_WIN, min_n=None):
    """滚动 Amihud：尾窗 win 内 mean(|ret|/(close*vol))；成交额非正的点跳过，有效点<min_n 返 None。"""
    min_n = win // 2 if min_n is None else min_n
    out = [None] * len(rets)
    for t in range(len(rets)):
        lo = max(0, t - win + 1)
        acc, cnt = 0.0, 0
        for k in range(lo, t + 1):
            if _isnum(rets[k]) and _isnum(closes[k]) and _isnum(vols[k]) and closes[k] > 0 and vols[k] > 0:
                acc += abs(rets[k]) / (closes[k] * vols[k])
                cnt += 1
        if cnt >= min_n:
            out[t] = acc / cnt
    return out


def _skew(xs):
    """样本偏度 = 三阶中心矩/方差^1.5；不足3点或零方差返 0.0（调用方按有效点数决定是否采用）。"""
    n = len(xs)
    if n < 3:
        return 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / n
    if var <= 1e-18:
        return 0.0
    third = sum((x - m) ** 3 for x in xs) / n
    return third / (var ** 1.5)


def rolling_skew(rets, win=SKEW_WIN, min_n=None):
    """滚动偏度：尾窗 win 内日收益偏度，有效点<min_n 为 None。"""
    min_n = (win * 2) // 3 if min_n is None else min_n
    out = [None] * len(rets)
    for t in range(len(rets)):
        lo = max(0, t - win + 1)
        xs = [x for x in rets[lo:t + 1] if _isnum(x)]
        if len(xs) >= min_n:
            out[t] = _skew(xs)
    return out


def market_by_date(bysym):
    """全市场等权"市场收益"：每个日期对所有品种当日 ret1d 取有限值均值（截面基准，供特异波动回归）。"""
    acc, cnt = {}, {}
    for rows in bysym.values():
        for r in rows:
            v = r.get("ret1d")
            if _isnum(v):
                acc[r["date"]] = acc.get(r["date"], 0.0) + v
                cnt[r["date"]] = cnt.get(r["date"], 0) + 1
    return {d: acc[d] / cnt[d] for d in acc}


def rolling_idiovol(rets, mkt, win=IDIO_WIN, min_n=None):
    """滚动特异波动：尾窗 win 内把 ret_i 对市场收益做一元 OLS（ret=α+β·m+e），残差 e 的样本标准差。

    rets/mkt 等长对齐（mkt[t] 为同日全市场等权收益）；只用尾窗（PIT），有效点<min_n 为 None。
    纯市场驱动（残差≈0）的品种 idiovol≈0，个股私有波动越大 idiovol 越高。
    """
    min_n = (win * 2) // 3 if min_n is None else min_n
    out = [None] * len(rets)
    for t in range(len(rets)):
        lo = max(0, t - win + 1)
        xs, ys = [], []
        for k in range(lo, t + 1):
            if _isnum(rets[k]) and _isnum(mkt[k]):
                xs.append(mkt[k]); ys.append(rets[k])
        n = len(xs)
        if n < min_n:
            continue
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx <= 1e-18:        # 市场无变化时退化为个股自身波动
            beta = 0.0
        else:
            sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
            beta = sxy / sxx
        alpha = my - beta * mx
        resid = [y - (alpha + beta * x) for x, y in zip(xs, ys)]
        var = sum(e * e for e in resid) / n
        out[t] = math.sqrt(var) if var > 0 else 0.0
    return out


def build_factor_series(bysym):
    """对每品种一次性算出五个因子的对齐序列，返回 {sym: {factor: [对齐 rows 的值序列]}}。"""
    mkt = market_by_date(bysym)
    out = {}
    for sym, rows0 in bysym.items():
        rows = sorted(rows0, key=lambda r: r["date"])
        oi = [r.get("oi") for r in rows]
        close = [r.get("c") for r in rows]
        vol = [r.get("v") for r in rows]
        ret = [r.get("ret1d") for r in rows]
        mseries = [mkt.get(r["date"]) for r in rows]
        out[sym] = {
            "doi1": pct_change(oi, 1),
            "doi5": pct_change(oi, 5),
            "amihud20": rolling_amihud(ret, close, vol, AMIHUD_WIN),
            "idiovol60": rolling_idiovol(ret, mseries, IDIO_WIN),
            "skew60": rolling_skew(ret, SKEW_WIN),
            "_dates": [r["date"] for r in rows],
            "_close": close,
        }
    return out


# =========================== 纯函数：前向检验 / 季节性 ===========================
def factor_forward_curve(bysym, series_by_sym, fname, horizons=HORIZONS, n_q=N_Q, min_pairs=MIN_PAIRS):
    """跨品种池化：因子 fname 对每个未来 H 的 (n, RankIC, Q5-Q1, 单调比例)。纯函数。"""
    per_h = {H: [] for H in horizons}
    for sym, rows0 in bysym.items():
        rows = sorted(rows0, key=lambda r: r["date"])
        closes = [r["c"] for r in rows]
        fwd = fh.forward_map(closes, tuple(horizons))
        vals = series_by_sym[sym][fname]
        for t in range(len(rows)):
            fv = vals[t] if t < len(vals) else None
            if not _isnum(fv):
                continue
            for H in horizons:
                if fwd[H][t] is not None:
                    per_h[H].append((fv, fwd[H][t]))
    curve = {}
    for H in horizons:
        pairs = per_h[H]
        if len(pairs) < min_pairs:
            curve[H] = {"n": len(pairs), "ic": None, "q5q1": None, "mono": None}
            continue
        ic = feval.spearman([p[0] for p in pairs], [p[1] for p in pairs])
        buckets = feval.quantile_buckets(pairs, n_q)
        q5q1 = buckets[-1][1] - buckets[0][1] if buckets[0][0] and buckets[-1][0] else None
        mono, _ = feval.monotonic_score(buckets)
        curve[H] = {"n": len(pairs), "ic": ic, "q5q1": q5q1, "mono": mono,
                    "q_means": [b[1] for b in buckets], "q_uprate": [b[2] for b in buckets]}
    return curve


def calendar_seasonality(bysym, key="month"):
    """池化日历效应：key='month' 按自然月1..12、'weekday' 按周一..周五聚合 ret1d，给 n/均值/中位/上涨占比。"""
    bins = {}
    for rows0 in bysym.values():
        for r in rows0:
            v = r.get("ret1d")
            if not _isnum(v):
                continue
            try:
                d = _dt.date.fromisoformat(r["date"])
            except (ValueError, TypeError):
                continue
            k = d.month if key == "month" else d.weekday()
            bins.setdefault(k, []).append(v)
    out = {}
    for k in sorted(bins):
        xs = sorted(bins[k])
        n = len(xs)
        out[k] = {"n": n, "mean": sum(xs) / n, "median": xs[n // 2],
                  "uprate": sum(1 for x in xs if x > 0) / n}
    return out


# =========================== 报告 / 运行 ===========================
def _ic_str(x):
    return "%+.3f" % x if _isnum(x) else "  -- "


def render(meta, results, season_m, season_w):
    L = []
    L.append("=" * 104)
    L.append("G24 微结构/持仓/季节因子族实验台 microstructure_lab（纯离线读 G21 面板，只研究不改权重、不接 main）")
    L.append("品种=%d，样本 %s~%s；因子全部 PIT 尾窗；前向 H=%s 日，池化 RankIC + 五档 Q5-Q1（n≥%d 才给IC）"
             % (meta["n_sym"], meta["d0"], meta["d1"], "/".join(map(str, HORIZONS)), MIN_PAIRS))
    L.append("-" * 104)
    L.append("【一】连续因子前向检验（跨品种池化；|IC|≥0.05 才算有弱信号、≥0.10 才算较稳，与既有研究同门槛）")
    L.append("  %-18s %5s %8s %10s %8s | %5s %8s %10s %8s | %5s %8s %10s %8s"
             % ("因子", "H", "RankIC", "Q5-Q1", "单调度", "H", "RankIC", "Q5-Q1", "单调度",
                "H", "RankIC", "Q5-Q1", "单调度"))
    for fkey, fname, _hint in FACTORS:
        cur = results[fkey]
        cells = []
        for H in HORIZONS:
            c = cur[H]
            cells.append("%5d %8s %10s %8s"
                         % (H, _ic_str(c["ic"]),
                            ("%+.2f%%" % (c["q5q1"] * 100)) if _isnum(c["q5q1"]) else "  --  ",
                            ("%.2f" % c["mono"]) if _isnum(c["mono"]) else " -- "))
        L.append("  %-18s %s" % (fname, " | ".join(cells)))
    L.append("  读法：RankIC 跨品种方向秩相关（正=因子越大未来涨越多）；Q5-Q1=最高档-最低档平均前向收益；"
             "单调度=五档均值相邻递增比例（1=完全单调）。Amihud 已乘%.0e仅为可读、不影响秩。" % AMIHUD_SCALE)
    L.append("-" * 104)
    L.append("【二】日历季节性（池化日收益；均值/上涨占比，商品经典'旺季/淡季'，样本短勿外推）")
    wname = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    mname = ["", "1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月", "9月", "10月", "11月", "12月"]
    row = []
    for m in range(1, 13):
        s = season_m.get(m)
        row.append("%s %+.2f%%/%.0f%%" % (mname[m], s["mean"] * 100, s["uprate"] * 100) if s else "%s --" % mname[m])
    L.append("  按月：" + "  ".join(row))
    roww = []
    for w in range(5):
        s = season_w.get(w)
        roww.append("%s %+.2f%%/%.0f%%(n%d)" % (wname[w], s["mean"] * 100, s["uprate"] * 100, s["n"]) if s else "")
    L.append("  按周：" + "  ".join(x for x in roww if x))
    # 最强/最弱月
    months = [(m, s["mean"]) for m, s in season_m.items() if s["n"] >= 30]
    if months:
        hi = max(months, key=lambda t: t[1]); lo = min(months, key=lambda t: t[1])
        L.append("  最强月=%s(%+.2f%%)，最弱月=%s(%+.2f%%)（仅历史描述、非未来保证）"
                 % (mname[hi[0]], hi[1] * 100, mname[lo[0]], lo[1] * 100))
    L.append("-" * 104)
    L.append("【三】数据缺口（诚实边界）：HP/SP 套保/投机压力需'交易者分类持仓'(类CFTC COT)，本面板只有总 OI、"
             "无分类持仓，本轮不做；微结构因子为日频、无法替代逐笔/盘口（精确流动性与滑点待 G14 一档快照自采）。")
    L.append("诚实边界：面板为主连比例复权日频、固定面板有幸存者偏差；Amihud 用 c*v 估成交额（单位不统一、"
             "仅横截面/时序秩可比）；未计手续费/滑点/保证金/换月；research 结论不进综合分、不挂影子、不自动上线。")
    return "\n".join(L)


def run(db_path=DEFAULT_DB, txt_path=LAB_TXT, json_path=LAB_JSON, verbose=True):
    store = pb.PanelStore(db_path)
    syms = sorted(store.symbols())
    rows = store.load_all() if hasattr(store, "load_all") else _load_all(store, syms)
    if hasattr(store, "close"):
        store.close()
    bysym = fh.rows_by_symbol(rows)
    series = build_factor_series(bysym)
    results = {fkey: factor_forward_curve(bysym, series, fkey) for fkey, _, _ in FACTORS}
    season_m = calendar_seasonality(bysym, "month")
    season_w = calendar_seasonality(bysym, "weekday")
    all_dates = sorted(r["date"] for r in rows)
    meta = {"n_sym": len(syms), "d0": all_dates[0] if all_dates else None,
            "d1": all_dates[-1] if all_dates else None, "n_rows": len(rows),
            "horizons": list(HORIZONS), "n_q": N_Q, "amihud_scale": AMIHUD_SCALE,
            "windows": {"amihud": AMIHUD_WIN, "idiovol": IDIO_WIN, "skew": SKEW_WIN}}
    text = render(meta, results, season_m, season_w)
    if verbose:
        print(text)
    os.makedirs(os.path.dirname(txt_path), exist_ok=True)
    with open(txt_path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(text + "\n")
    payload = {"meta": meta,
               "factors": {fkey: results[fkey] for fkey, _, _ in FACTORS},
               "factor_names": {fkey: fname for fkey, fname, _ in FACTORS},
               "season_month": {str(k): v for k, v in season_m.items()},
               "season_weekday": {str(k): v for k, v in season_w.items()}}
    with open(json_path, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(payload, fp, ensure_ascii=False, allow_nan=False, indent=1)
    # 旁路台账（登记失败绝不影响产物）
    try:
        brief = {}
        for fkey, _, _ in FACTORS:
            brief[fkey] = {("ic_h%d" % H): results[fkey][H]["ic"] for H in HORIZONS}
        el.safe_record(
            "microstructure_lab",
            {"horizons": list(HORIZONS), "n_q": N_Q,
             "windows": meta["windows"], "panel_db": os.path.basename(db_path)},
            {"n_sym": len(syms), "n_rows": len(rows), **brief},
            inputs=[db_path], artifacts=[txt_path, json_path],
            conclusion="G24微结构/持仓/季节因子族前向检验（%d品种%s~%s）：doi/amihud/idiovol/skew 池化RankIC与季节表，research不进分"
                       % (len(syms), meta["d0"], meta["d1"]))
    except Exception:
        pass
    return payload


def _load_all(store, syms):
    out = []
    for s in syms:
        out.extend(store.load_rows(s))
    return out


# =========================== 零网络/零DB 合成断言 ===========================
def _toy_bysym(seed=7):
    """两品种、约 220 日：价格几何随机、OI 趋势、量价齐全；返回 bysym（rows 已带 date/c/v/oi/ret1d）。"""
    import random
    rnd = random.Random(seed)
    bysym = {}
    for sym, drift in (("AA", 0.0006), ("BB", -0.0003)):
        rows, c, oi = [], 100.0, 10000.0
        for t in range(220):
            r = drift + rnd.gauss(0, 0.01)
            c *= (1 + r)
            oi *= (1 + 0.001 * math.sin(t / 12.0) + rnd.gauss(0, 0.002))
            d = _dt.date(2025, 1, 1) + _dt.timedelta(days=t)
            rows.append({"sym": sym, "date": d.isoformat(), "c": c, "v": 1e5 + rnd.random() * 1e4,
                         "oi": oi, "ret1d": r})
        bysym[sym] = rows
    return bysym


def selftest():
    # 1) pct_change 手算 + 边界
    pc = pct_change([100.0, 110.0, 121.0], 1)
    assert pc[0] is None and abs(pc[1] - 0.10) < 1e-12 and abs(pc[2] - 0.10) < 1e-12
    pc5 = pct_change([1.0, 2.0], 5)
    assert pc5[0] is None and pc5[1] is None           # 窗口不足
    assert pct_change([0.0, 1.0], 1)[1] is None         # 基数非正安全
    # 2) rolling_amihud：恒定 |r|/notional 时等于该常数；零成交额点跳过
    am = rolling_amihud([0.01, 0.01, 0.01], [1000.0] * 3, [100.0] * 3, win=3, min_n=3)
    assert abs(am[2] - 0.01 / 1e5) < 1e-12
    am0 = rolling_amihud([0.01, 0.01], [1000.0, 1000.0], [0.0, 0.0], win=2, min_n=1)
    assert am0[1] is None                               # 成交额全0 → 无有效点
    # 3) 偏度：对称≈0、右尾为正、左尾为负
    sym_seq = [-1.0, -0.5, 0.0, 0.5, 1.0] * 6
    assert abs(_skew(sym_seq)) < 1e-9
    assert _skew([0.0, 0.0, 0.0, 0.0, 1.0, 10.0]) > 1.0
    assert _skew([0.0, 0.0, 0.0, 0.0, -1.0, -10.0]) < -1.0
    rs = rolling_skew([None, 1.0, 2.0, -1.0], win=4, min_n=3)
    assert rs[0] is None and rs[3] is not None
    # 4) 特异波动：纯市场驱动→残差≈0；市场+固定私有噪声→残差波动≈噪声尺度
    mkt = [((i % 7) - 3) * 0.01 for i in range(120)]
    pure = [2.0 * m for m in mkt]
    iv0 = rolling_idiovol(pure, mkt, win=120, min_n=60)
    assert iv0[-1] is not None and iv0[-1] < 1e-12
    noisy = [2.0 * m + 0.004 * (1 if i % 2 else -1) for i, m in enumerate(mkt)]
    iv1 = rolling_idiovol(noisy, mkt, win=120, min_n=60)
    assert iv1[-1] is not None and iv1[-1] > 0.003       # 私有噪声被保留为残差波动
    assert rolling_idiovol(pure, [None] * 120, win=120, min_n=1)[-1] is None  # 市场全缺
    # 5) 端到端：build 五因子键齐、长度对齐、PIT（前段为 None）
    bysym = _toy_bysym()
    series = build_factor_series(bysym)
    for sym in bysym:
        n = len(bysym[sym])
        for fkey in ("doi1", "doi5", "amihud20", "idiovol60", "skew60"):
            assert len(series[sym][fkey]) == n
        assert series[sym]["doi1"][0] is None and series[sym]["doi5"][:5] == [None] * 5
        assert all(v is None or v >= 0 for v in series[sym]["amihud20"])
    # 6) 前向曲线：构造"因子=未来5日收益"→ H=5 RankIC≈1、Q5-Q1>0、单调度高
    perfect = {}
    for sym, rows in bysym.items():
        rows = sorted(rows, key=lambda r: r["date"])
        closes = [r["c"] for r in rows]
        fwd5 = fh.forward_map(closes, (5,))[5]
        perfect[sym] = {"g": [(v if v is not None else 0.0) for v in fwd5]}
    curve = factor_forward_curve(bysym, perfect, "g", horizons=(1, 5), min_pairs=20)
    assert curve[5]["ic"] is not None and curve[5]["ic"] > 0.99
    assert curve[5]["q5q1"] > 0 and curve[5]["mono"] >= 0.9
    # 7) 季节性：把所有 1 月收益改成 +0.02 → 1月均值≈0.02、上涨占比100%
    bs2 = {s: [dict(r) for r in rows] for s, rows in bysym.items()}
    for rows in bs2.values():
        for r in rows:
            if r["date"][5:7] == "01":
                r["ret1d"] = 0.02
    sm = calendar_seasonality(bs2, "month")
    assert abs(sm[1]["mean"] - 0.02) < 1e-12 and sm[1]["uprate"] == 1.0
    sw = calendar_seasonality(bysym, "weekday")
    assert set(sw) <= set(range(7)) and all(0 <= v["uprate"] <= 1 for v in sw.values())
    # 8) render 端到端不崩、含三板块标题
    meta = {"n_sym": 2, "d0": "2025-01-01", "d1": "2025-08-08", "n_rows": 440,
            "horizons": list(HORIZONS), "n_q": N_Q, "amihud_scale": AMIHUD_SCALE,
            "windows": {"amihud": 20, "idiovol": 60, "skew": 60}}
    res = {fkey: factor_forward_curve(bysym, series, fkey, min_pairs=20) for fkey, _, _ in FACTORS}
    txt = render(meta, res, sm, sw)
    assert "【一】" in txt and "【二】" in txt and "【三】" in txt
    print("microstructure_lab selftest ALL PASS（OI变化/Amihud非流动/滚动偏度/市场模型特异波动PIT/"
          "前向RankIC-Q5Q1单调/日历季节/端到端 共8组）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="G24 微结构/持仓/季节因子族实验台（纯离线读面板）")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    run(db_path=args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
