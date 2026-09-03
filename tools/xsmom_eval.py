# -*- coding: utf-8 -*-
r"""G7（第31轮）截面动量多空 XSMOM 离线评估（研究侧工具，不进常驻链路、不改任何线上权重/综合分）。

与第30轮 tools/tsmom_eval.py 的区别（两类不同策略，务必区分）：
  - 时序动量 TSMOM（第30轮，已被真实数据证伪）：对每个品种，用它"自己过去 L 日收益"预测
    "它自己未来 H 日收益"，是品种内的时间序列问题；国内商品近4年品种内偏反转，不成立。
  - 截面动量 XSMOM（本轮）：在每个调仓日 t，把当时所有可得品种按过去 L 日波动调整动量 z 排序，
    等权（或反波动率加权）做多最强一档 Q_top、做空最弱一档 Q_bot，得到一条**市场中性多空组合**
    收益序列——赚的是"谁比谁强"的相对排序钱，天然对冲掉全市场同涨同跌（beta）。第30轮 pooled
    RankIC 的弱正主要来自这一截面成分，本轮把它从时序成分里干净地剥离出来单独检验能否赚钱。

学术参照：Jegadeesh & Titman (1993) 动量、Asness Moskowitz Pedersen (2013) "Value and Momentum
Everywhere" 的截面多空与反波动率加权。纯标准库、零新增第三方依赖。

数据与口径（与 backtest / tsmom_eval 完全同一条链路，保证三类结果可直接对照）：
  - backtest.resolve_codes 取全品种主连 -> futures_data.fetch_daily_kline ->
    backtest.ratio_adjusted_bars（主连换月跳空收益置 0 的比例后复权，避免换月假趋势）；
  - 排序因子 = futures_data.tsmom_series 的波动调整 z{L}=过去L日累计收益÷(L日日收益样本std×√252)，
    跨品种量纲可比（同时保留原始 ret{L} 可切换）；实时侧与本工具同一函数，杜绝两套算法；
  - 目标 = 未来 H 日简单收益 close[t+H]/close[t]-1；调仓日 t 的因子只用 t 及之前数据，无未来函数；
  - 主口径=**非重叠调仓**（每隔 H 个交易日调一次、持有 H 日，期与期不重叠，可复利、可算夏普/回撤/t）；
    另给日频重叠口径做稳健性对照（重叠会低估标准误、使 t 偏乐观，仅参考）。

输出：reports/xsmom_eval.txt（人类可读）+ reports/xsmom_eval.json（结构化 sidecar）。
用法（项目根目录）：
  D:\Python\python.exe tools\xsmom_eval.py                 # 全品种、L∈{20,63,126,252}×H∈{5,20,60}
  D:\Python\python.exe tools\xsmom_eval.py --codes RB0,CU0,M0 --limit 8
  D:\Python\python.exe tools\xsmom_eval.py --selftest      # 零网络合成断言
"""
import argparse
import math
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import config  # noqa: E402
import futures_data  # noqa: E402
import factor_eval as fe  # noqa: E402  复用 pearson/spearman
import backtest  # noqa: E402  复用 resolve_codes/ratio_adjusted_bars
import panel_builder as pb  # noqa: E402  G21续：--panel 读标准研究面板（已复权，不再二次复权）

# 因子键前缀：z=波动调整动量（主），ret=原始累计收益（对照）
FACTOR_KINDS = [("z", "波动调整动量z"), ("ret", "原始累计收益ret")]


# =========================== 面板构造（可合成断言） ===========================
def forward_returns(closes, horizons):
    """{H: [与 closes 等长，t 买入持有 H 日的简单收益，尾部不足为 None]}（无未来函数）。"""
    n = len(closes)
    out = {H: [None] * n for H in horizons}
    for t in range(n):
        for H in horizons:
            j = t + H
            if j < n and closes[t] > 0 and closes[j] > 0:
                out[H][t] = closes[j] / closes[t] - 1.0
    return out


def points_from_adjusted(name, sector, bars, lookbacks, horizons):
    """已比例复权 bar -> 逐时点 {sym,sector,date,各L的z/ret/vol,各H的fwd}（纯函数、不联网、不再复权）。"""
    if len(bars) < max(lookbacks) + min(horizons) + 2:
        return []
    closes = [futures_data._f(b["c"]) for b in bars]
    dates = [str(b.get("d", "")) for b in bars]
    ts = futures_data.tsmom_series(closes, lookbacks=tuple(lookbacks))
    fwd = forward_returns(closes, tuple(horizons))
    pts = []
    warm = max(lookbacks)
    for t in range(warm, len(closes)):
        p = {"sym": name, "sector": sector, "date": dates[t]}
        ok = True
        for L in lookbacks:
            p["z%d" % L] = ts["tsmom%d" % L][t]
            p["ret%d" % L] = ts["ret%d" % L][t]
            p["vol%d" % L] = futures_data._window_std(closes, t, L)
            if p["ret%d" % L] is None:
                ok = False
        for H in horizons:
            p["fwd%d" % H] = fwd[H][t]
        if ok:
            pts.append(p)
    return pts


def build_symbol_points(name, sector, raw_bars, lookbacks, horizons, days):
    """单品种网络旧路径：raw[-days:] 再比例复权 -> points_from_adjusted（历史逐值一致）。"""
    bars, _roll = backtest.ratio_adjusted_bars(raw_bars[-days:])
    return points_from_adjusted(name, sector, bars, lookbacks, horizons)


def build_panel(points):
    """把散点组织成 (有序交易日列表, {date: {sym: point}})；交易日=全部品种日期并集升序。"""
    by_date = defaultdict(dict)
    dates = set()
    for p in points:
        by_date[p["date"]][p["sym"]] = p
        dates.add(p["date"])
    return sorted(dates), by_date


# =========================== 截面组合（纯函数，可手算断言） ===========================
def _quantile_members(members_sorted, n_q):
    """按因子升序排好的成员切成 n_q 档，返回 [[member...], ...]，不重不漏（与 factor_eval 同切法）。"""
    n = len(members_sorted)
    bands = []
    for q in range(n_q):
        a, b = q * n // n_q, (q + 1) * n // n_q
        if b > a:
            bands.append(members_sorted[a:b])
    return bands


def _weighted_fwd(members, fkey, weight_kind):
    """一档成员的（加权）平均未来收益与权重表；weight_kind: equal / ivol（反波动率）。"""
    if weight_kind == "ivol":
        ws = []
        for m in members:
            v = m.get("vol")
            w = (1.0 / v) if (v is not None and v > 1e-12) else 0.0
            ws.append(w)
        s = sum(ws)
        if s <= 1e-15:  # 波动率全部缺失/退化 -> 安全退回等权
            weight_kind = "equal"
        else:
            return sum(w * m[fkey] for m, w in zip(members, ws)) / s
    return sum(m[fkey] for m in members) / len(members)


def cross_section_periods(dates, by_date, factor_key, horizon, lookback, n_q,
                          min_names, weight_kind="equal", step=None, sector_scope=None):
    """非重叠截面多空：在全局交易日上每隔 H 日调一次仓，每期多最强一档/空最弱一档，持有 H 日。

    sector_scope：None=全市场；否则只保留 sector 在该集合内的品种（板块池条件化，第32轮）。
    返回逐期 dict：date/n/bands_mean(各档平均fwd)/long/short/ls(多空价差)/mkt(池内等权基准)/
                  long_excess(多头档相对池内基准的超额=只做多不做空的选股alpha)/
                  long_syms/short_syms/long_sec/short_sec（成员板块计数）。
    """
    scope = set(sector_scope) if sector_scope else None
    fkey = "fwd%d" % horizon
    vkey = "vol%d" % lookback
    step = step or horizon
    periods = []
    for di in range(0, len(dates), step):
        d = dates[di]
        row = by_date.get(d, {})
        members = []
        for _sym, p in row.items():
            if scope is not None and p.get("sector") not in scope:
                continue
            fv, yv = p.get(factor_key), p.get(fkey)
            if fv is None or yv is None:
                continue
            if not (math.isfinite(fv) and math.isfinite(yv)):
                continue
            members.append({"sym": p["sym"], "sector": p["sector"], "fv": fv,
                            fkey: yv, "vol": p.get(vkey)})
        if len(members) < min_names or len(members) < 2 * n_q:
            continue
        members.sort(key=lambda m: m["fv"])
        bands = _quantile_members(members, n_q)
        if len(bands) < n_q:  # 切不出完整档数（成员太少）也跳过
            continue
        bands_mean = [_weighted_fwd(b, fkey, weight_kind) for b in bands]
        top, bot = bands[-1], bands[0]
        long_ret = _weighted_fwd(top, fkey, weight_kind)
        short_side = _weighted_fwd(bot, fkey, weight_kind)   # 最弱档的"绝对涨幅"；做空收益=-short_side
        ls = long_ret - short_side
        mkt = sum(m[fkey] for m in members) / len(members)
        sec_long, sec_short = defaultdict(float), defaultdict(float)
        for m in top:
            sec_long[m["sector"]] += 1.0 / len(top)
        for m in bot:
            sec_short[m["sector"]] += 1.0 / len(bot)
        periods.append({
            "date": d, "n": len(members), "bands_mean": bands_mean,
            "long": long_ret, "short_abs": short_side, "short_pnl": -short_side,
            "ls": ls, "mkt": mkt, "long_excess": long_ret - mkt,
            "long_syms": [m["sym"] for m in top], "short_syms": [m["sym"] for m in bot],
            "sec_long": dict(sec_long), "sec_short": dict(sec_short)})
    return periods


def overlap_daily_ls(dates, by_date, factor_key, horizon, lookback, n_q, min_names):
    """日频调仓、持有 H 日的重叠多空收益序列（每个交易日都排序，期与期重叠；t 偏乐观仅作稳健对照）。"""
    out = []
    fkey = "fwd%d" % horizon
    vkey = "vol%d" % lookback
    for d in dates:
        row = by_date.get(d, {})
        members = []
        for _sym, p in row.items():
            fv, yv = p.get(factor_key), p.get(fkey)
            if fv is None or yv is None or not (math.isfinite(fv) and math.isfinite(yv)):
                continue
            members.append({"sym": p["sym"], "sector": p["sector"], "fv": fv, fkey: yv,
                            "vol": p.get(vkey)})
        if len(members) < min_names or len(members) < 2 * n_q:
            continue
        members.sort(key=lambda m: m["fv"])
        bands = _quantile_members(members, n_q)
        if len(bands) < n_q:
            continue
        out.append(_weighted_fwd(bands[-1], fkey, "equal")
                   - _weighted_fwd(bands[0], fkey, "equal"))
    return out


# =========================== 绩效统计（纯函数，可手算断言） ===========================
def _equity_dd(seq):
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in seq:
        eq *= 1.0 + r
        peak = max(peak, eq)
        mdd = max(mdd, 1.0 - eq / peak)
    return eq - 1.0, mdd


def perf_stats(periods, horizon, cost_round=0.0, key="ls"):
    """逐期多空收益绩效。cost_round=单方向一次往返成本率；满仓多空两腿扣 2*cost_round。

    返回 n/毛均/净均/净t/净胜率/净累计/年化/夏普/最大回撤（净口径，非重叠可复利）。
    """
    gross = [p[key] for p in periods] if periods and isinstance(periods[0], dict) else list(periods)
    n = len(gross)
    if n == 0:
        return None
    legs = 2 if key == "ls" else 1
    net = [g - legs * cost_round for g in gross]
    mg = sum(gross) / n
    mn = sum(net) / n
    if n >= 2:
        sd = statistics_sample_std(net)
        tstat = (mn / (sd / math.sqrt(n))) if sd > 1e-12 else 0.0
        sharpe = (mn / sd * math.sqrt(252.0 / horizon)) if sd > 1e-12 else 0.0
    else:
        tstat = sharpe = 0.0
    win = sum(1 for v in net if v > 0) / n
    cum, mdd = _equity_dd(net)
    ann = mn * 252.0 / horizon
    return {"n": n, "gross_mean": mg, "net_mean": mn, "net_t": tstat, "win": win,
            "net_cum": cum, "annual": ann, "sharpe": sharpe, "max_dd": mdd,
            "gross": gross, "net": net}


def statistics_sample_std(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    mu = sum(xs) / n
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / (n - 1))


def bands_profile(periods, n_q):
    """跨期平均各档收益 Q1..Qn、相邻单调比例、档位列序 Spearman、多空价差（毛）。"""
    acc = [0.0] * n_q
    cnt = [0] * n_q
    for p in periods:
        for q, v in enumerate(p["bands_mean"]):
            acc[q] += v
            cnt[q] += 1
    means = [(acc[q] / cnt[q]) if cnt[q] else 0.0 for q in range(n_q)]
    inc = sum(1 for a, b in zip(means, means[1:]) if b >= a)
    mono = inc / (n_q - 1)
    rank_q = list(range(1, n_q + 1))
    col_ric = fe.spearman(rank_q, means) if len(means) >= 2 else 0.0
    return {"means": means, "mono": mono, "col_rank_ic": col_ric,
            "spread": means[-1] - means[0]}


def split_is_oos(periods, oos_ratio):
    """按调仓日升序切前 (1-r)=IS、后 r=OOS。"""
    ordered = sorted(periods, key=lambda p: p["date"])
    cut = int(len(ordered) * (1.0 - oos_ratio))
    return ordered[:cut], ordered[cut:]


def sector_breakdown(periods):
    """多空腿成员的板块平均权重分布，以及留一板块(LO SO)后多空毛均收。"""
    n = len(periods)
    if n == 0:
        return {}, {}
    long_w, short_w = defaultdict(float), defaultdict(float)
    for p in periods:
        for s, w in p["sec_long"].items():
            long_w[s] += w / n
        for s, w in p["sec_short"].items():
            short_w[s] += w / n
    sectors = sorted(set(long_w) | set(short_w))
    exposure = {s: {"long": long_w.get(s, 0.0), "short": short_w.get(s, 0.0),
                    "net": long_w.get(s, 0.0) - short_w.get(s, 0.0)} for s in sectors}
    loso = {}
    for drop in sectors:
        # 留一板块对照：只用"两条腿都不含 drop 板块"的调仓期，看剔除该板块后多空是否仍为正
        clean = [p["ls"] for p in periods
                 if drop not in p["sec_long"] and drop not in p["sec_short"]]
        if clean:
            loso[drop] = sum(clean) / len(clean)
    return exposure, loso


def sector_internal(dates, by_date, factor_key, horizon, lookback, n_q, min_sector, step=None):
    """每个板块内部单独做截面多空（只在该板块品种间排序），看动量是否只在某板块成立。"""
    step = step or horizon
    fkey = "fwd%d" % horizon
    out = defaultdict(list)
    for di in range(0, len(dates), step):
        d = dates[di]
        row = by_date.get(d, {})
        by_sec = defaultdict(list)
        for _sym, p in row.items():
            fv, yv = p.get(factor_key), p.get(fkey)
            if fv is None or yv is None or not (math.isfinite(fv) and math.isfinite(yv)):
                continue
            by_sec[p["sector"]].append((fv, yv))
        for sec, arr in by_sec.items():
            if len(arr) < min_sector or len(arr) < 2 * n_q:
                continue
            arr.sort(key=lambda t: t[0])
            bands = _quantile_members([{"fv": a, fkey: b} for a, b in arr], n_q)
            if len(bands) < n_q:
                continue
            top = sum(m[fkey] for m in bands[-1]) / len(bands[-1])
            bot = sum(m[fkey] for m in bands[0]) / len(bands[0])
            out[sec].append(top - bot)
    return {s: {"n": len(v), "mean": sum(v) / len(v),
                "win": sum(1 for x in v if x > 0) / len(v)} for s, v in out.items()}


# =========================== 第32轮：条件化 + 双样本稳健（纯函数，可手算断言） ===========================
# 候选腿模式 -> 逐期收益键：ls=多空价差(两腿成本) / lex=多头档相对池内基准超额(单腿) / long=纯多头(单腿,含beta)
LEG_KEY = {"ls": "ls", "lex": "long_excess", "long": "long"}
LEG_LABEL = {"ls": "多空", "lex": "多头超额", "long": "纯多头"}


def truncate_dates(dates, recent_n):
    """取全局交易日历最近 recent_n 个（by_date 共享、无需重建）；recent_n<=0 或更长时原样返回。"""
    if not recent_n or recent_n <= 0 or recent_n >= len(dates):
        return list(dates)
    return list(dates[-recent_n:])


def _candidate_perf(panel, factor_key, L, H, n_q, cond_min, cost_round, scope, leg):
    """单个条件化候选在单个窗口上的组合+净绩效（leg 决定收益键与成本腿数）。"""
    dates, by_date = panel
    key = LEG_KEY.get(leg, "ls")
    pers = cross_section_periods(dates, by_date, factor_key, H, L, n_q,
                                 cond_min, "equal", H, sector_scope=scope)
    return pers, perf_stats(pers, H, cost_round, key)


def conditional_scan(windows, factor_key, L, H, n_q, cond_min, cost_round, candidates):
    """对一组候选(名称,板块池,腿模式) × 两个样本窗口批量评估。

    windows: 有序 [(窗口名, (dates,by_date)), ...]（如 [("近4.1年",短面板),("9.9年",长面板)]）。
    返回 {名称: {"scope","leg","windows":{窗口名: perf或None}, "periods_n":{...}}}，保持候选顺序。
    """
    out = {}
    for name, scope, leg in candidates:
        row = {"scope": scope, "leg": leg, "windows": {}, "n_periods": {}}
        for wname, panel in windows:
            pers, pf = _candidate_perf(panel, factor_key, L, H, n_q, cond_min,
                                       cost_round, scope, leg)
            row["windows"][wname] = pf
            row["n_periods"][wname] = len(pers)
        out[name] = row
    return out


def robust_verdict(row, tmin, decay_tol, long_n_ratio=1.5):
    """双样本稳健判定：两窗净均收>0、两窗净 t≥tmin、长窗 t 不比短窗低过 decay_tol（显著性要随样本量稳定），
    且长窗非重叠期数须≥短窗×long_n_ratio（板块品种上市晚、长窗拿不到更长历史时，两窗实为同源小样本，不算双样本）。

    取 windows 顺序的第一个为"短窗"、最后一个为"长窗"；返回 (bool, [说明])。样本不足直接不稳健。
    """
    wins = list(row["windows"].items())
    perf = [(n, p) for n, p in wins if p is not None]
    if len(perf) < 2:
        return False, ["可用样本窗口不足2个（%s），无法做双样本稳健检验" % len(perf)]
    (sn, sp), (ln, lp) = perf[0], perf[-1]
    why = []
    for n, p in perf:
        if not (p["net_mean"] > 0):
            why.append("%s净均收%+.2f%%为负" % (n, p["net_mean"] * 100))
        if not (p["net_t"] >= tmin):
            why.append("%s净t=%+.2f未达%.1f" % (n, p["net_t"], tmin))
    if lp["net_t"] < sp["net_t"] - decay_tol:
        why.append("长窗(%s)t=%+.2f比短窗(%s)t=%+.2f衰减超过%.1f（regime偶然嫌疑）"
                   % (ln, lp["net_t"], sn, sp["net_t"], decay_tol))
    n_s, n_l = sp.get("n", 0), lp.get("n", 0)
    if n_l < n_s * long_n_ratio:
        why.append("长窗n=%d未比短窗n=%d多%.0f%%（池内品种凑齐分档门槛的历史不足，两窗实为同源小样本，非独立长样本）"
                   % (n_l, n_s, (long_n_ratio - 1) * 100))
    return (len(why) == 0), why


# =========================== 裁决 ===========================
def gate_verdict(main_perf, oos_perf, bands, leg_long, leg_short_pnl, exposure,
                 tmin, mono_gate, max_sector_drive):
    """截面多空"确定不更差"判据；返回 (bool, [原因])。与时序判据不同，针对市场中性组合定制。"""
    reasons = []
    if main_perf is None:
        return False, ["主组合无样本"]
    if not (main_perf["net_t"] >= tmin):
        reasons.append("净多空t=%+.2f未达门槛%.1f（非重叠期n=%d）"
                       % (main_perf["net_t"], tmin, main_perf["n"]))
    if not (main_perf["net_mean"] > 0):
        reasons.append("净多空均收%+.3f%%为负" % (main_perf["net_mean"] * 100))
    if oos_perf is not None and not (oos_perf["net_mean"] > 0):
        reasons.append("样本外OOS净多空%+.3f%%转负" % (oos_perf["net_mean"] * 100))
    if not (bands["mono"] >= mono_gate and bands["spread"] > 0):
        reasons.append("分档单调性%.0f%%/多空价差%+.2f%%不达标(门槛%.0f%%且价差>0)"
                       % (bands["mono"] * 100, bands["spread"] * 100, mono_gate * 100))
    # 不能两条腿都方向错（多头档应偏强、做空最弱档应赚钱=最弱档确实跌）
    if not (leg_long > 0 or leg_short_pnl > 0):
        reasons.append("多头腿%+.2f%%与做空腿%+.2f%%均不赚钱，价差无真实方向支撑"
                       % (leg_long * 100, leg_short_pnl * 100))
    # 单一板块偏置：净敞口绝对值最大的板块占比
    if exposure:
        worst = max(exposure.items(), key=lambda kv: abs(kv[1]["net"]))
        drive = abs(worst[1]["net"])
        if drive > max_sector_drive:
            reasons.append("板块偏置：%s净敞口占比%.0f%%>%.0f%%（动量可能只由单一板块驱动）"
                           % (worst[0], drive * 100, max_sector_drive * 100))
    return (len(reasons) == 0), reasons


# =========================== 报告 ===========================
def _now():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fk(lookback, kind):
    return ("z%d" if kind == "z" else "ret%d") % lookback


def evaluate_grid(dates, by_date, lookbacks, horizons, n_q, min_names, cost_round, main_l, main_h):
    """L×H 参数网格（主因子 z，等权，非重叠）：每格净多空绩效 + 分档单调性。"""
    grid = {}
    for L in lookbacks:
        for H in horizons:
            pers = cross_section_periods(dates, by_date, "z%d" % L, H, L, n_q,
                                         min_names, "equal", H)
            perf = perf_stats(pers, H, cost_round, "ls")
            bp = bands_profile(pers, n_q) if pers else None
            grid[(L, H)] = {"perf": perf, "bands": bp}
    return grid


def build_report(points, errors, dates, by_date, lookbacks, horizons, main_l, main_h,
                 n_q, min_names, min_sector, oos_ratio, tmin, mono_gate, max_drive,
                 cost_round, days, weight_kind="equal", robust_panel=None,
                 candidates=None, cond_min=None, decay_tol=None, main_days=None,
                 main_scope=None, main_leg="ls", long_n_ratio=1.5):
    n_sym = len({p["sym"] for p in points})
    leg_key = LEG_KEY.get(main_leg, "ls")
    Lout = []
    Lout.append("=" * 108)
    Lout.append(" G7 截面动量多空 XSMOM 离线评估（时序动量的截面替代）  生成于 %s" % _now())
    Lout.append("=" * 108)
    Lout.append("样本：%d 个品种、%d 个(品种×交易日)点、%d 个共同调仓日；日K经主连换月跳空置0比例后复权；"
                % (n_sym, len(points), len(dates)))
    Lout.append("      每个调仓日跨品种按波动调整动量z排序分%d档，多最强一档/空最弱一档，市场中性多空组合。" % n_q)
    if errors:
        Lout.append("取数失败品种 %d 个（不阻断）：%s"
                    % (len(errors), "、".join("%s(%s)" % (n, e[:20]) for n, e in errors[:12])))
    Lout.append("口径：主口径=非重叠(每H日调仓持有H日,可复利)；净=毛-两腿往返成本(单程往返%.2f%%、两腿%.2f%%/期)；"
                % (cost_round * 100, 2 * cost_round * 100))
    Lout.append("      t=净多空期均÷(期std/√期数)；分档单调=相邻档平均收益递增比例；纯标准库零新增依赖。")
    Lout.append("")

    # 表1：L×H 参数网格
    Lout.append("一、回看窗 L × 持有期 H 参数网格（因子=波动调整z、等权、非重叠；格式 净均收%/期 | t | 胜率 | 净夏普）")
    head = " %-8s " % "L＼H"
    for H in horizons:
        head += "| 持有%2d日（n期/净均/t/胜率/夏普）             " % H
    Lout.append(head)
    Lout.append(" " + "-" * 104)
    grid = evaluate_grid(dates, by_date, lookbacks, horizons, n_q, min_names,
                         cost_round, main_l, main_h)
    for L in lookbacks:
        line = " %-8d " % L
        for H in horizons:
            g = grid[(L, H)]
            pf = g["perf"]
            if pf is None:
                line += "| %-34s " % "无样本"
            else:
                line += "| n=%-3d %+5.2f%% t=%+5.2f 胜%3.0f%% 夏%5.2f " % (
                    pf["n"], pf["net_mean"] * 100, pf["net_t"], pf["win"] * 100, pf["sharpe"])
        Lout.append(line)
    Lout.append("")

    # 主组合明细
    fk_main = _fk(main_l, "z")
    scope_txt = "全市场" if not main_scope else "+".join(main_scope)
    pers = cross_section_periods(dates, by_date, fk_main, main_h, main_l, n_q,
                                 min_names, weight_kind, main_h, sector_scope=main_scope)
    perf_g = perf_stats(pers, main_h, 0.0, leg_key)
    perf_n = perf_stats(pers, main_h, cost_round, leg_key)
    bp = bands_profile(pers, n_q)
    is_p, oos_p = split_is_oos(pers, oos_ratio)
    perf_is, perf_oos = perf_stats(is_p, main_h, cost_round, leg_key), perf_stats(oos_p, main_h, cost_round, leg_key)
    if pers:
        leg_long = sum(p["long"] for p in pers) / len(pers)
        leg_bot = sum(p["short_abs"] for p in pers) / len(pers)
        leg_short = sum(p["short_pnl"] for p in pers) / len(pers)
        leg_mkt = sum(p["mkt"] for p in pers) / len(pers)
    else:
        leg_long = leg_bot = leg_short = leg_mkt = 0.0
    overlap = overlap_daily_ls(dates, by_date, fk_main, main_h, main_l, n_q, min_names)
    ov_mean = sum(overlap) / len(overlap) if overlap else 0.0
    ov_t = _tstat(overlap)
    # 反波动率加权稳健性
    pers_iv = cross_section_periods(dates, by_date, fk_main, main_h, main_l, n_q,
                                    min_names, "ivol", main_h, sector_scope=main_scope)
    perf_iv = perf_stats(pers_iv, main_h, cost_round, leg_key)
    # 原始 ret 因子对照（不做波动调整）
    pers_ret = cross_section_periods(dates, by_date, "ret%d" % main_l, main_h, main_l,
                                     n_q, min_names, "equal", main_h, sector_scope=main_scope)
    perf_ret = perf_stats(pers_ret, main_h, cost_round, leg_key)

    Lout.append("二、主组合（L=%d 日动量排序、持有 %d 日、%s、%d 档、池=%s、腿=%s）逐档与两腿拆解"
                % (main_l, main_h, "反波动率加权" if weight_kind == "ivol" else "等权", n_q,
                   scope_txt, LEG_LABEL.get(main_leg, main_leg)))
    if not pers:
        Lout.append(" ⚠ 主组合无可用调仓期（当日可得品种数不足 --min-names=%d 或暖机不足），以下主组合明细为空。"
                    % min_names)
    Lout.append(" 各档(Q1最弱→Q%d最强)平均未来%d日收益：%s"
                % (n_q, main_h, " | ".join("%+.2f%%" % (v * 100) for v in bp["means"])))
    Lout.append(" 分档单调性 %.0f%%、档位列序Spearman=%+.3f、Q%d-Q1 毛多空价差 %+.2f%%/期"
                % (bp["mono"] * 100, bp["col_rank_ic"], n_q, bp["spread"] * 100))
    Lout.append(" 腿拆解（毛，/期）：多头最强档 %+.2f%%｜做空最弱档 %+.2f%%（最弱档绝对涨幅%+.2f%%）｜"
                "全市场等权基准 %+.2f%%" % (leg_long * 100, leg_short * 100, leg_bot * 100, leg_mkt * 100))
    Lout.append(" 多空与市场基准相关系数 %+.3f（接近0=确实市场中性、赚的是相对排序而非beta）"
                % fe.pearson([p["ls"] for p in pers], [p["mkt"] for p in pers]))
    _fmt = lambda p: ("n=%d 毛均%+.2f%% 净均%+.2f%% 净t=%+.2f 胜率%.0f%% 净累计%+.1f%% 年化%+.1f%% "
                      "净夏普%+.2f 最大回撤%.1f%%") % (
                      p["n"], p["gross_mean"] * 100, p["net_mean"] * 100, p["net_t"],
                      p["win"] * 100, p["net_cum"] * 100, p["annual"] * 100,
                      p["sharpe"], p["max_dd"] * 100) if p else "样本不足"
    Lout.append(" 毛口径：" + _fmt(perf_g))
    Lout.append(" 净口径：" + _fmt(perf_n))
    Lout.append(" IS(前%.0f%%)：%s" % ((1 - oos_ratio) * 100, _fmt(perf_is)))
    Lout.append(" OOS(后%.0f%%)：%s" % (oos_ratio * 100, _fmt(perf_oos)))
    Lout.append(" 稳健性①反波动率加权：" + _fmt(perf_iv))
    Lout.append(" 稳健性②原始ret因子(不做波动调整)：" + _fmt(perf_ret))
    Lout.append(" 稳健性③日频重叠调仓(期数%d、重叠使t偏乐观仅参考)：毛均%+.2f%%/期、t=%+.2f"
                % (len(overlap), ov_mean * 100, ov_t))
    Lout.append("")

    # 表3：板块
    Lout.append("三、板块条件化（判断截面动量是全市场现象还是单一板块驱动）")
    exposure, loso = sector_breakdown(pers)
    if exposure:
        Lout.append(" 主组合多/空腿成员板块平均权重（净=多头占比-空头占比）：")
        for s, e in sorted(exposure.items(), key=lambda kv: -abs(kv[1]["net"])):
            Lout.append("   %-6s 多头%4.0f%% 空头%4.0f%% 净敞口%+5.0f%%%s"
                        % (s, e["long"] * 100, e["short"] * 100, e["net"] * 100,
                           "  ←单一板块偏置" if abs(e["net"]) > max_drive else ""))
    internal = sector_internal(dates, by_date, fk_main, main_h, main_l, n_q, min_sector, main_h)
    Lout.append(" 板块内各自做截面多空（只在同板块品种间排序，要求≥%d个品种；毛均/期、胜率、期数）：" % min_sector)
    if internal:
        pos = 0
        for s, v in sorted(internal.items(), key=lambda kv: -kv[1]["mean"]):
            flag = "为正" if v["mean"] > 0 else "为负"
            pos += 1 if v["mean"] > 0 else 0
            Lout.append("   %-6s 毛均%+.2f%%/期 胜率%.0f%% n=%d（%s）"
                        % (s, v["mean"] * 100, v["win"] * 100, v["n"], flag))
        Lout.append(" 板块内多空为正：%d/%d 个板块。" % (pos, len(internal)))
    else:
        Lout.append("   各板块品种数均不足，无法做板块内截面。")
    Lout.append("")

    # 表4：裁决
    Lout.append("四、『确定不更差』并入判据（净t≥%.1f、净均>0、OOS不转负、分档单调≥%.0f%%且价差>0、"
                "至少一条腿方向对、单一板块净敞口≤%.0f%%）" % (tmin, mono_gate * 100, max_drive * 100))
    ok, reasons = gate_verdict(perf_n, perf_oos, bp, leg_long, leg_short, exposure,
                              tmin, mono_gate, max_drive)
    verdict = {"ok": ok, "reasons": reasons,
               "main": {"L": main_l, "H": main_h, "weight": weight_kind,
                        "gross_mean": perf_g["gross_mean"] if perf_g else None,
                        "net_mean": perf_n["net_mean"] if perf_n else None,
                        "net_t": perf_n["net_t"] if perf_n else None,
                        "win": perf_n["win"] if perf_n else None,
                        "sharpe": perf_n["sharpe"] if perf_n else None,
                        "max_dd": perf_n["max_dd"] if perf_n else None,
                        "oos_net_mean": perf_oos["net_mean"] if perf_oos else None,
                        "mono": bp["mono"], "spread": bp["spread"],
                        "leg_long": leg_long, "leg_short_pnl": leg_short, "mkt_beta": leg_mkt}}
    if ok:
        Lout.append(" ✅ 主组合通过全部判据：截面动量多空在本样本成立，可作为下一轮在 cross_section 挂"
                    "『长窗动量排序』影子字段的候选（仍默认不改综合分，先影子积累）。")
    else:
        Lout.append(" ❌ 暂不并入，原因：")
        for r in reasons:
            Lout.append("   - " + r)
    Lout.append("")

    # 表5：条件化（板块池/多头腿）× 双样本稳健对照（第32轮；robust_panel 缺省则整章不输出=旧行为）
    cond_sidecar = None
    if robust_panel is not None and candidates:
        short_name = "近%.1f年" % ((main_days or days) / 252.0)
        long_name = "长%.1f年" % (days / 252.0)
        windows = [(short_name, (dates, by_date)), (long_name, robust_panel)]
        scan = conditional_scan(windows, fk_main, main_l, main_h, n_q,
                                cond_min or min_names, cost_round, candidates)
        Lout.append("五、条件化增强 × 双样本稳健对照（L=%d/H=%d；回答'板块池或只做多能否救回动量、且长样本不衰减'）"
                    % (main_l, main_h))
        Lout.append(" 候选（板块池/腿）            | %s 净均/t/夏普/n        | %s 净均/t/夏普/n       | 双样本稳健"
                    % (short_name, long_name))
        Lout.append(" " + "-" * 100)
        robust_names, cond_sidecar = [], {}
        for cname, row in scan.items():
            cells = []
            for wn in (short_name, long_name):
                p = row["windows"][wn]
                if p is None:
                    cells.append("%-24s" % "样本不足")
                else:
                    cells.append("%+5.2f%% t=%+5.2f 夏%5.2f n=%-3d"
                                 % (p["net_mean"] * 100, p["net_t"], p["sharpe"], p["n"]))
            ok_r, why_r = robust_verdict(row, tmin, decay_tol or 0.5, long_n_ratio or 1.5)
            tag = "✅稳健" if ok_r else ("✗ " + (why_r[0][:30] if why_r else ""))
            scope_txt = "全市场" if row["scope"] is None else "+".join(row["scope"])
            Lout.append(" %-12s(%s/%s) | %s | %s | %s"
                        % (cname, scope_txt[:8], LEG_LABEL.get(row["leg"], row["leg"]),
                           cells[0], cells[1], tag))
            cond_sidecar[cname] = {"leg": row["leg"], "scope": row["scope"],
                                   "robust": ok_r, "robust_reasons": why_r,
                                   "windows": {wn: (None if p is None else
                                                   {k: p[k] for k in ("n", "net_mean", "net_t", "win", "sharpe", "max_dd")})
                                               for wn, p in row["windows"].items()}}
            if ok_r:
                robust_names.append(cname)
        Lout.append(" 双样本稳健判据：两窗净均>0 且净t≥%.1f、长窗t不短窗衰减超%.1f、且长窗非重叠期数≥短窗×%.1f"
                    "（显著性须随样本量稳定、长样本须真有增量历史，防板块品种上市晚致两窗同源，第31轮教训）。"
                    % (tmin, decay_tol or 0.5, long_n_ratio or 1.5))
        if robust_names:
            Lout.append(" ✅ 通过双样本稳健的条件化候选：%s——可作为下一轮挂影子的优先对象（仍默认不改综合分）。"
                        % "、".join(robust_names))
        else:
            Lout.append(" ❌ 没有任何条件化候选（板块池/多头腿）通过双样本稳健检验：截面动量经条件化仍不达标，继续维持纯研究。")
        Lout.append("")

    Lout.append("诚实边界：①主连为比例后复权近似、样本为近约 %d 根日K（约%.1f年）单一行情 regime，品种上市早晚不一；"
                % (days, days / 252.0))
    Lout.append("②非重叠期数有限（持有%d日约%d期），t 统计对正态/独立假设敏感；③历史规律不代表未来，本报告只给研究证据，"
                % (main_h, len(pers)))
    Lout.append("  绝不自动修改 analyzer/cross_section 任何权重；④即便通过，并入仍须『默认影子、缺省等价旧版、可一键回退』。")
    sidecar = _sidecar(points, grid, verdict, exposure, internal, lookbacks, horizons,
                       main_l, main_h, days, perf_n, perf_oos, bp)
    if cond_sidecar is not None:
        sidecar["conditional"] = cond_sidecar
    return "\n".join(Lout) + "\n", sidecar, verdict


def _tstat(seq):
    n = len(seq)
    if n < 2:
        return 0.0
    mu = sum(seq) / n
    sd = statistics_sample_std(seq)
    return (mu / (sd / math.sqrt(n))) if sd > 1e-12 else 0.0


def _sidecar(points, grid, verdict, exposure, internal, lookbacks, horizons,
             main_l, main_h, days, perf_n, perf_oos, bp):
    g = {}
    for (L, H), v in grid.items():
        pf = v["perf"]
        g["%d_%d" % (L, H)] = None if pf is None else {
            "n": pf["n"], "net_mean": pf["net_mean"], "net_t": pf["net_t"],
            "win": pf["win"], "sharpe": pf["sharpe"], "max_dd": pf["max_dd"],
            "mono": v["bands"]["mono"] if v["bands"] else None}
    return {"generated_at": _now(), "days": days, "lookbacks": list(lookbacks),
            "horizons": list(horizons), "main_l": main_l, "main_h": main_h,
            "n_points": len(points), "n_symbols": len({p["sym"] for p in points}),
            "grid": g, "verdict": verdict,
            "sector_exposure": exposure, "sector_internal": internal}


# =========================== 数据抓取与入口 ===========================
def _sector_of(name):
    meta = config.VARIETIES.get(name, {})
    return meta.get("cat", "其他")


def _fetch_one(item, lookbacks, horizons, days, prefer_panel=False):
    name, code = item
    try:
        if prefer_panel:
            bars, _src = pb.load_adjusted_bars(code, days, prefer_panel=True)
            pts = points_from_adjusted(name, _sector_of(name), bars, lookbacks, horizons)
        else:
            raw = futures_data.fetch_daily_kline(code)
            pts = build_symbol_points(name, _sector_of(name), raw, lookbacks, horizons, days)
        if not pts:
            return name, [], "K线/暖机不足"
        return name, pts, ""
    except Exception as e:  # 单品种失败不阻断全市场
        return name, [], "%s: %s" % (type(e).__name__, e)


def collect_points(items, lookbacks, horizons, days, workers, prefer_panel=False):
    points, errors = [], []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = [pool.submit(_fetch_one, it, lookbacks, horizons, days, prefer_panel) for it in items]
        for fut in as_completed(futs):
            name, pts, err = fut.result()
            if pts:
                points.extend(pts)
            elif err:
                errors.append((name, err))
    points.sort(key=lambda p: (p["date"], p["sym"]))
    return points, errors


def run(argv=None):
    ap = argparse.ArgumentParser(description="G7 截面动量多空 XSMOM 离线评估（研究侧）")
    ap.add_argument("--codes", default="", help="逗号分隔品种/主连，缺省=全品种")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--days", type=int, default=config.XSMOM_ROBUST_DAYS,
                    help="拉取/长样本日K根数（默认2500≈9.9年，供双样本稳健对照）")
    ap.add_argument("--main-days", type=int, default=config.XSMOM_EVAL_DAYS,
                    help="主样本窗口=全局日历最近N个交易日（默认1023≈4.1年，对齐第30/31轮口径）")
    ap.add_argument("--lookbacks", default=",".join(map(str, config.XSMOM_LOOKBACKS)))
    ap.add_argument("--horizons", default=",".join(map(str, config.XSMOM_HORIZONS)))
    ap.add_argument("--main-l", type=int, default=config.XSMOM_MAIN_L)
    ap.add_argument("--main-h", type=int, default=config.XSMOM_MAIN_H)
    ap.add_argument("--quantiles", type=int, default=config.XSMOM_N_Q)
    ap.add_argument("--min-names", type=int, default=config.XSMOM_MIN_NAMES)
    ap.add_argument("--cond-min-names", type=int, default=config.XSMOM_COND_MIN_NAMES)
    ap.add_argument("--min-sector", type=int, default=config.XSMOM_MIN_SECTOR_NAMES)
    ap.add_argument("--decay-tol", type=float, default=config.XSMOM_DECAY_TOL)
    ap.add_argument("--long-n-ratio", type=float, default=config.XSMOM_LONG_N_RATIO)
    ap.add_argument("--scope", default="", help="主组合板块池，逗号分隔（如 有色,农产品），缺省=全市场")
    ap.add_argument("--leg", choices=("ls", "lex", "long"), default="ls",
                    help="主组合腿：ls多空(默认)/lex多头超额(long-池内基准)/long纯多头")
    ap.add_argument("--no-conditional", action="store_true", help="关闭第五章条件化双样本对照")
    ap.add_argument("--oos-ratio", type=float, default=config.XSMOM_OOS_RATIO)
    ap.add_argument("--tmin", type=float, default=config.XSMOM_TMIN)
    ap.add_argument("--mono-gate", type=float, default=config.XSMOM_MONO_GATE)
    ap.add_argument("--max-sector-drive", type=float, default=config.XSMOM_MAX_SECTOR_DRIVE)
    ap.add_argument("--fee-rate", type=float, default=config.BACKTEST_FEE_RATE)
    ap.add_argument("--slip-rate", type=float, default=config.BACKTEST_SLIP_RATE)
    ap.add_argument("--weight", choices=("equal", "ivol"), default="equal",
                    help="档内加权：equal 等权（默认）；ivol 反波动率加权（AQR口径）")
    ap.add_argument("--workers", type=int, default=config.XSMOM_EVAL_WORKERS)
    ap.add_argument("--out", default=config.XSMOM_EVAL_FILE)
    ap.add_argument("--panel", action="store_true",
                    help="G21续：优先读 cache/research_panel.db 已复权面板（缺省仍联网现拉，结果一致）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    lookbacks = tuple(sorted(int(x) for x in args.lookbacks.split(",") if x.strip()))
    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
    main_l = args.main_l if args.main_l in lookbacks else lookbacks[-1]
    main_h = args.main_h if args.main_h in horizons else horizons[len(horizons) // 2]
    cost_round = 2.0 * (args.fee_rate + args.slip_rate)   # 单方向一次往返成本（开+平）
    main_scope = tuple(s.strip() for s in args.scope.split(",") if s.strip()) or None
    items = backtest.resolve_codes(args.codes, args.limit if args.limit > 0 else None)
    # 一次拉满长样本（--days），主样本=全局日历最近 main_days 个交易日，两窗口同源可比
    points, errors = collect_points(items, lookbacks, horizons, args.days, args.workers, getattr(args, 'panel', False))
    if not points:
        print("无可用样本（全部品种取数失败或暖机不足），错误示例：%s" % errors[:3])
        return 2
    long_dates, long_by = build_panel(points)
    main_dates = truncate_dates(long_dates, args.main_days)
    main_set = set(main_dates)
    main_points = [p for p in points if p["date"] in main_set]
    candidates = None if args.no_conditional else tuple(config.XSMOM_COND_CANDIDATES)
    text, sidecar, verdict = build_report(
        main_points, errors, main_dates, long_by, lookbacks, horizons, main_l, main_h,
        args.quantiles, args.min_names, args.min_sector, args.oos_ratio,
        args.tmin, args.mono_gate, args.max_sector_drive, cost_round, args.days, args.weight,
        robust_panel=(long_dates, long_by), candidates=candidates,
        cond_min=args.cond_min_names, decay_tol=args.decay_tol, main_days=len(main_dates),
        main_scope=main_scope, main_leg=args.leg, long_n_ratio=args.long_n_ratio)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8-sig") as f:
        f.write(text)
    import json
    with open(config.XSMOM_EVAL_JSON, "w", encoding="utf-8") as f:
        f.write(json.dumps(sidecar, ensure_ascii=False, indent=1))
    print(text)
    n_robust = sum(1 for c in (sidecar.get("conditional") or {}).values() if c.get("robust"))
    print("品种时点 %d、覆盖品种 %d；主组合裁决 ok=%s；双样本稳健候选 %d 个；报告 -> %s；JSON -> %s"
          % (len(main_points), sidecar["n_symbols"], verdict["ok"], n_robust,
             args.out, config.XSMOM_EVAL_JSON))
    return 0


# =========================== 合成断言（零网络） ===========================
def _synthetic_panel(kind="trend", n_sym=20, n_days=320, seed=1):
    """构造确定性面板点集。

    kind=trend：每个品种有固定漂移强度且持续（强者恒强）-> 截面动量多空为正、分档单调；
    kind=reverse：每期排名确定性翻转 -> 截面多空为负。
    返回 points 列表（直接喂 build_panel）。
    """
    import random
    rng = random.Random(seed)
    drifts = [(-0.004 + 0.008 * i / (n_sym - 1)) for i in range(n_sym)]  # 从弱到强固定漂移
    points = []
    for si in range(n_sym):
        price = 100.0
        closes = []
        dates = []
        for t in range(n_days):
            shock = rng.gauss(0, 0.004)
            drift = drifts[si]
            if kind == "reverse":
                drift = drifts[si] * (1 if (t // 40) % 2 == 0 else -1)
            price = max(1.0, price * (1 + drift + shock))
            closes.append(price)
            dates.append("2025-%02d-%02d" % (t // 28 % 12 + 1, t % 28 + 1))
        Ls = (20, 60)
        Hs = (5, 20)
        ts = futures_data.tsmom_series(closes, lookbacks=Ls)
        fwd = forward_returns(closes, Hs)
        for t in range(max(Ls), len(closes)):
            p = {"sym": "S%02d" % si, "sector": "板块%d" % (si % 4), "date": dates[t]}
            ok = True
            for L in Ls:
                p["z%d" % L] = ts["tsmom%d" % L][t]
                p["ret%d" % L] = ts["ret%d" % L][t]
                p["vol%d" % L] = futures_data._window_std(closes, t, L)
                if p["ret%d" % L] is None:
                    ok = False
            for H in Hs:
                p["fwd%d" % H] = fwd[H][t]
            if ok:
                points.append(p)
    return points


def selftest():
    # 1) forward_returns 手算、尾部 None（无未来函数）
    closes = [100.0, 102.0, 101.0, 103.0]
    fwd = forward_returns(closes, (1, 2))
    assert abs(fwd[1][0] - 0.02) < 1e-12 and abs(fwd[2][0] - 0.01) < 1e-12
    assert fwd[1][-1] is None and fwd[2][-1] is None and fwd[2][-2] is None

    # 2) 分档不重不漏
    ms = [{"fv": i, "f": 0.0, "vol": 0.01} for i in range(20)]
    bands = _quantile_members(ms, 5)
    assert len(bands) == 5 and sum(len(b) for b in bands) == 20
    assert [m["fv"] for m in bands[0]][0] == 0 and [m["fv"] for m in bands[-1]][-1] == 19

    # 3) 等权 vs 反波动率加权：低波动成员权重更大
    members = [{"f": 0.10, "vol": 0.01}, {"f": 0.20, "vol": 0.04}]
    eq = _weighted_fwd(members, "f", "equal")
    iv = _weighted_fwd(members, "f", "ivol")
    assert abs(eq - 0.15) < 1e-12 and iv < eq, (eq, iv)  # 低波动(0.10)权重高->iv更接近0.10
    # 波动率全缺失安全退回等权
    assert abs(_weighted_fwd([{"f": 1.0, "vol": None}, {"f": 3.0, "vol": None}], "f", "ivol") - 2.0) < 1e-12

    # 4) 强者恒强面板：截面多空毛收益为正、t>0、分档单调=1
    pts = _synthetic_panel("trend")
    dates, by_date = build_panel(pts)
    pers = cross_section_periods(dates, by_date, "z60", 20, 60, 5, 16, "equal", 20)
    assert len(pers) >= 5, len(pers)
    pf = perf_stats(pers, 20, 0.0, "ls")
    bp = bands_profile(pers, 5)
    assert pf["gross_mean"] > 0 and pf["net_t"] > 0, pf
    assert bp["mono"] == 1.0 and bp["spread"] > 0, bp
    # 非重叠：调仓日期间隔=持有期，期数≈天数/20
    assert all(pers[i + 1]["date"] >= pers[i]["date"] for i in range(len(pers) - 1))

    # 5) 成本单调拉低净收益：净=毛-两腿往返
    pf_c = perf_stats(pers, 20, 0.0003, "ls")
    assert abs((pf["gross_mean"] - pf_c["net_mean"]) - 2 * 0.0003) < 1e-12

    # 6) 样本不足安全：min_names 超过当日品种数 -> 空组合、perf=None
    none_pers = cross_section_periods(dates, by_date, "z60", 20, 60, 5, 999, "equal", 20)
    assert none_pers == [] and perf_stats([], 20) is None

    # 7) IS/OOS 有序不重叠、覆盖全部
    isp, osp = split_is_oos(pers, 0.3)
    assert len(isp) + len(osp) == len(pers) and isp[-1]["date"] <= osp[0]["date"]

    # 8) 绩效统计手算：两期已知收益
    toy = [{"ls": 0.10}, {"ls": -0.05}]
    p_toy = perf_stats(toy, 20, 0.0, "ls")
    assert abs(p_toy["gross_mean"] - 0.025) < 1e-12 and abs(p_toy["win"] - 0.5) < 1e-12
    cum, mdd = _equity_dd([0.10, -0.05])
    assert abs(cum - (1.1 * 0.95 - 1)) < 1e-12 and mdd >= 0

    # 9) gate：t 不足/分档不单调必须否决；全达标通过
    good_perf = {"n": 40, "gross_mean": 0.01, "net_mean": 0.009, "net_t": 2.3,
                 "win": 0.6, "net_cum": 0.3, "annual": 0.1, "sharpe": 1.1, "max_dd": 0.05}
    good_bands = {"mono": 1.0, "spread": 0.02, "means": [0, 0, 0, 0, 0.02], "col_rank_ic": 1.0}
    ok, why = gate_verdict(good_perf, good_perf, good_bands, 0.01, 0.01,
                           {"有色": {"net": 0.2}}, 1.5, 0.75, 0.6)
    assert ok and not why, (ok, why)
    bad_perf = dict(good_perf, net_t=0.6)
    ok2, why2 = gate_verdict(bad_perf, good_perf, good_bands, 0.01, 0.01, {}, 1.5, 0.75, 0.6)
    assert not ok2 and any("t=" in w for w in why2)
    # 单一板块偏置否决
    ok3, why3 = gate_verdict(good_perf, good_perf, good_bands, 0.01, 0.01,
                             {"能化": {"net": 0.85}}, 1.5, 0.75, 0.6)
    assert not ok3 and any("板块偏置" in w for w in why3)

    # 10) build_report 全量跑通且裁决键齐全（强者恒强应通过）
    text, sidecar, verdict = build_report(
        pts, [], dates, by_date, (20, 60), (5, 20), 60, 20, 5, 16, 6,
        0.3, 1.5, 0.75, 0.6, 0.0003, 320, "equal")
    assert "XSMOM" in text and set(verdict) >= {"ok", "reasons", "main"}
    assert sidecar["n_symbols"] == 20 and "20_20" in sidecar["grid"]
    assert "五、条件化" not in text and "conditional" not in sidecar  # 缺省不传 robust=旧行为、无第五章

    # 11) sector_scope 板块池过滤 + long_excess=long-池内mkt 手算 + lex 单腿成本
    d11 = ["g1"]
    by11 = {"g1": {}}
    for k in range(12):
        sec = "有色" if k < 6 else "能化"
        by11["g1"]["V%02d" % k] = {"sym": "V%02d" % k, "sector": sec, "z60": float(k),
                                   "ret60": float(k), "vol60": 0.01, "fwd20": 0.01 * k}
    p_you = cross_section_periods(d11, by11, "z60", 20, 60, 3, 6, "equal", 20,
                                  sector_scope=("有色",))
    assert len(p_you) == 1 and p_you[0]["n"] == 6
    assert all(s.startswith("V0") for s in p_you[0]["long_syms"] + p_you[0]["short_syms"])
    # 有色6个 fwd=0..0.05，3档每档2：top=.045、bot=.005、池内mkt=.025 -> long_excess=.02
    assert abs(p_you[0]["long"] - 0.045) < 1e-12
    assert abs(p_you[0]["mkt"] - 0.025) < 1e-12
    assert abs(p_you[0]["long_excess"] - 0.02) < 1e-12
    # lex 单腿成本（只做多最强档，扣 1 次往返；ls 才扣两次）
    pf_lex = perf_stats(p_you, 20, 0.0003, "long_excess")
    assert abs(pf_lex["net_mean"] - (0.02 - 0.0003)) < 1e-12
    pf_ls = perf_stats(p_you, 20, 0.0003, "ls")
    assert abs(pf_ls["net_mean"] - (0.04 - 0.0006)) < 1e-12

    # 12) truncate_dates：取尾部、0/超长安全
    seq = list(range(10))
    assert truncate_dates(seq, 3) == [7, 8, 9]
    assert truncate_dates(seq, 0) == seq and truncate_dates(seq, 99) == seq

    # 13) conditional_scan 结构齐全 + robust_verdict 双样本判据
    cands = [("全市场·多空(基线)", None, "ls"), ("有色池·多空", ("有色",), "ls"),
             ("全市场·多头超额", None, "lex")]
    short_d = truncate_dates(dates, 160)
    scan = conditional_scan([("短", (short_d, by_date)), ("长", (dates, by_date))],
                            "z60", 60, 20, 5, 16, 0.0003, cands)
    assert list(scan) == [c[0] for c in cands]
    for row in scan.values():
        assert set(row["windows"]) == {"短", "长"}
    def _perf(t, m, n=40):
        return {"n": n, "gross_mean": m, "net_mean": m, "net_t": t, "win": 0.6,
                "net_cum": 0.2, "annual": 0.1, "sharpe": 1.0, "max_dd": 0.05,
                "gross": [], "net": []}
    ok_r, _ = robust_verdict({"windows": {"短": _perf(2.0, 0.01, 40), "长": _perf(1.8, 0.01, 100)}}, 1.5, 0.5)
    assert ok_r
    ok_d, why_d = robust_verdict({"windows": {"短": _perf(2.2, 0.01, 40), "长": _perf(1.0, 0.01, 100)}}, 1.5, 0.5)
    assert not ok_d and any("衰减" in w for w in why_d)
    # 长窗期数没比短窗多（板块品种上市晚、两窗同源）-> 即便 t 都高也不算双样本稳健
    ok_s, why_s = robust_verdict({"windows": {"短": _perf(2.01, 0.027, 25), "长": _perf(2.01, 0.027, 25)}}, 1.5, 0.5)
    assert not ok_s and any("同源" in w for w in why_s)
    ok_n, why_n = robust_verdict({"windows": {"短": _perf(2.0, 0.01), "长": None}}, 1.5, 0.5)
    assert not ok_n and any("窗口不足" in w for w in why_n)

    # 14) build_report 带 robust_panel 出第五章、sidecar.conditional 齐全且能容纳样本不足候选
    text2, sc2, _ = build_report(
        pts, [], dates, by_date, (20, 60), (5, 20), 60, 20, 5, 16, 6,
        0.3, 1.5, 0.75, 0.6, 0.0003, 320, "equal",
        robust_panel=(dates, by_date), candidates=cands, cond_min=16,
        decay_tol=0.5, main_days=160)
    assert "五、条件化" in text2 and "conditional" in sc2
    assert set(sc2["conditional"]) == {c[0] for c in cands}
    for c in sc2["conditional"].values():
        wkeys = list(c["windows"])
        assert len(wkeys) == 2 and wkeys[0].startswith("近") and wkeys[1].startswith("长")
        assert "robust" in c
    print("xsmom_eval selftest ALL PASS（远期无泄漏/分档/加权/趋势多空/成本/IS-OOS/裁决门/报告/"
          "板块池·多头超额/窗口截断/双样本稳健/条件化第五章 共14组）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
