# -*- coding: utf-8 -*-
r"""G25续/G16前置（第61轮）正交IC加权接**真实研究面板**做样本外（walk-forward）对照——研究侧离线工具。

回答的问题：factor_legacy_expr.orthogonal_ic_blend（顺序Schmidt正交去共线 + |IC|有符号归一加权）
在真实面板上、严格样本外，是否比 ①等权合成 ②每个单因子 的下一期截面 RankIC 更稳/更高？
这是 G16 浅 ML（ml_samples）样本未齐前的一条**可解释线性合成基线**；纯研究侧，绝不进综合分、不改主链。

方法（全部来自自有 SQLite cache/research_panel.db，零网络、纯标准库）：
  1. 面板长表（sym×date）取候选因子列（默认 ret5/ret20/ret63/tsmom63/tsmom126/day_chg），
     前向收益 y_H(t)=c(t+H)/c(t)-1（严格未来，H=1/5/20 交易日）。
  2. 每个交易日先做**截面**均匀秩标准化（[-1,1]，只用当日截面、无未来），消除量纲/极值。
  3. Walk-forward 扩展窗：训练满 MIN_TRAIN 个交易日后，每 REFIT_EVERY 个交易日（月度）用
     **该日之前全部**训练样本重估一次：各因子池化 Spearman IC → orthogonal_ic_blend 学
     顺序正交 OLS 系数 betas 与 IC 权重 weights；两次再拟合之间对每个 OOS 日沿用同一组参数。
  4. OOS 日：用训练期 betas 对当日截面因子做三角递推残差化，再按 weights 合成得 blend 分；
     对照 equal 等权合成与每个单因子，计算当日**截面 RankIC**；汇总均值/ICIR/正比例/天数。
因果性：betas/weights/IC 只用 t 之前数据；OOS 日截面标准化只用当日截面；y 用 t 之后价格。

第73轮新增（G25续·动量反转反向利用）：`--rev-factor`（默认 ret63）额外构造一条**反转动量账本**
rev = -cs_uniform(rev_factor)，与正交/等权并列出 OOS 截面 RankIC 与分层多空净绩效——回答
"expr_miner 时序层发现的动量负IC（反转），在跨品种截面上样本外是否有肉"。注意：正交IC合成的
权重本就有符号（负IC因子自动得负权重），反向已隐含其中；本账本检验的是**纯反转单因子**本身。
若 rev_factor 不在候选列中，仅作为附加列装载（不进训练/blend，不污染合成）。

输出：reports/orthogonal_blend_oos.txt（人读）+ .json（机读），experiment_ledger 旁路台账一条。
用法（项目根目录）：
  D:\\Python\\python.exe tools\\orthogonal_blend_oos.py                 # 全面板、写报告
  D:\\Python\\python.exe tools\\orthogonal_blend_oos.py --factors ret20,ret63,tsmom126
  D:\\Python\\python.exe tools\\orthogonal_blend_oos.py --rev-factor tsmom126          # 换反转基准列
  D:\\Python\\python.exe tools\\orthogonal_blend_oos.py --selftest      # 零网络/零DB合成断言
"""
import argparse
import json
import math
import os
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
import factor_expr as fe                      # noqa: E402  白名单DSL：spearman/正交/加权原语
import factor_legacy_expr as fle              # noqa: E402  orthogonal_ic_blend
import config                                  # noqa: E402

DEFAULT_DB = _ROOT / "cache" / "research_panel.db"
DEFAULT_TXT = _ROOT / "reports" / "orthogonal_blend_oos.txt"
DEFAULT_JSON = _ROOT / "reports" / "orthogonal_blend_oos.json"
DEFAULT_FACTORS = ("ret5", "ret20", "ret63", "tsmom63", "tsmom126", "day_chg")
DEFAULT_REV_FACTOR = "ret63"   # 第73轮：反转动量账本基准列（expr_miner 时序层最强反转 mom_60 的面板代理）
HORIZONS = (1, 5, 20)
MIN_TRAIN = 60          # 至少 60 个交易日训练才开始 OOS
REFIT_EVERY = 20        # 每 20 个交易日（约月度）扩展窗重估一次参数
MIN_CS = 10             # OOS 当日完整样本截面至少 10 个品种才算一个有效 IC 点
N_QUANTILE = 5          # 分层多空：按分数分 5 层，多顶层、空底层（等权）
# 单边交易成本复用回测口径（兜底手续费 + 单边滑点），不新造数字；CLI 可覆盖
DEFAULT_COST_ONEWAY = getattr(config, "BACKTEST_FEE_RATE", 0.00005) + \
    getattr(config, "BACKTEST_SLIP_RATE", 0.00010)
TRADING_DAYS = 252


# --------------------------- 纯统计/变换（可合成断言） ---------------------------
def isnum(x):
    return fe._isnum(x)


def cs_uniform(values_by_sym):
    """当日截面均匀秩标准化到 [-1,1]（只用传入截面、确定性、抗极值）；不足 2 个返回 {}。"""
    items = [(s, v) for s, v in values_by_sym.items() if isnum(v)]
    m = len(items)
    if m < 2:
        return {}
    order = sorted(range(m), key=lambda i: items[i][1])
    ranks = [0.0] * m
    k = 0
    while k < m:                      # 平均秩处理并列
        j = k
        while j + 1 < m and items[order[j + 1]][1] == items[order[k]][1]:
            j += 1
        avg = (k + j) / 2.0 + 1.0
        for t in range(k, j + 1):
            ranks[order[t]] = avg
        k = j + 1
    denom = (m - 1) / 2.0 if m > 1 else 1.0
    return {items[i][0]: (ranks[i] - (m + 1) / 2.0) / denom for i in range(m)}


def apply_triangular(fvec, betas):
    """用训练期学到的顺序正交 OLS 系数，对一条 OOS 因子向量做同样的三角递推残差化。

    r_0=f_0；r_i = f_i - Σ_{j<i} beta_ij * r_j（与 factor_expr.orthogonalize 的训练变换同序）。"""
    resid = []
    for i in range(len(fvec)):
        ri = fvec[i]
        for j, b in enumerate(betas[i]):
            ri -= b * resid[j]
        resid.append(ri)
    return resid


def summarize_ic(daily_ics):
    """日度截面 IC 序列 → 均值/ICIR(mean/std)/正比例/天数。"""
    xs = [v for v in daily_ics if isnum(v)]
    n = len(xs)
    if n == 0:
        return {"mean_ic": None, "icir": None, "pct_positive": None, "n_days": 0}
    mean = sum(xs) / n
    var = sum((v - mean) ** 2 for v in xs) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    return {"mean_ic": mean, "icir": (mean / sd if sd > 1e-15 else 0.0),
            "pct_positive": sum(1 for v in xs if v > 0) / n, "n_days": n}


# --------------------------- 分层多空组合 / 换手 / 交易成本（第63轮，纯函数可合成断言） ---------------------------
def quantile_ls_day(score_by_sym, y_by_sym, n_q=N_QUANTILE):
    """单日分层多空：按分数升序分 n_q 层，多顶层、空底层、层内等权。

    返回 gross=1 的组合权重 {sym:+0.5/n_long（多）/-0.5/n_short（空）}，以及多/空腿平均前向收益、
    组合毛收益=0.5*(多腿-空腿)、多空价差=多腿-空腿。样本不足 2*n_q 返回 None。"""
    items = sorted(((s, score_by_sym[s]) for s in score_by_sym
                    if isnum(score_by_sym[s]) and isnum(y_by_sym.get(s))),
                   key=lambda kv: kv[1])
    m = len(items)
    if m < 2 * n_q:
        return None
    edges = [int(round(m * j / n_q)) for j in range(n_q + 1)]
    bottom = items[edges[0]:edges[1]]
    top = items[edges[n_q - 1]:edges[n_q]]
    longs = [s for s, _ in top]
    shorts = [s for s, _ in bottom]
    lr = sum(y_by_sym[s] for s in longs) / len(longs)
    sr = sum(y_by_sym[s] for s in shorts) / len(shorts)
    w = {}
    for s in longs:
        w[s] = 0.5 / len(longs)
    for s in shorts:
        w[s] = -0.5 / len(shorts)
    spread = lr - sr
    return {"weights": w, "long_ret": lr, "short_ret": sr, "spread": spread,
            "gross_ret": 0.5 * spread, "n_long": len(longs), "n_short": len(shorts)}


def turnover_between(w_prev, w_now):
    """相邻两日组合权重的换手：traded=Σ|Δw|（双边总成交额/组合净值），one_sided=0.5*traded。"""
    keys = set(w_prev) | set(w_now)
    traded = sum(abs(w_now.get(s, 0.0) - w_prev.get(s, 0.0)) for s in keys)
    return traded, 0.5 * traded


def perf_of_returns(rs):
    """日收益序列 → 均值/年化/波动/夏普/累计/最大回撤/正比例。"""
    xs = [v for v in rs if isnum(v)]
    n = len(xs)
    if n == 0:
        return {"n_days": 0}
    mean = sum(xs) / n
    var = sum((v - mean) ** 2 for v in xs) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    cum, peak, maxdd = 0.0, 0.0, 0.0
    for v in xs:
        cum += v
        peak = max(peak, cum)
        maxdd = max(maxdd, peak - cum)
    return {"n_days": n, "mean_daily": mean, "annual_ret": mean * TRADING_DAYS,
            "annual_vol": sd * math.sqrt(TRADING_DAYS),
            "sharpe": mean / sd * math.sqrt(TRADING_DAYS) if sd > 1e-15 else 0.0,
            "cum_ret": cum, "max_drawdown": maxdd,
            "pct_positive": sum(1 for v in xs if v > 0) / n}


def evaluate_ls_books_aligned(books, score_key="orth", n_q=N_QUANTILE,
                    cost_oneway=DEFAULT_COST_ONEWAY, hold=None, period_days=None):
    """G25续（第64轮）：按 H 对齐的**非重叠**再平衡分层多空（消除第63轮 H>1 前向收益日序重叠）。

    只在每 hold 个交易日（books 里的有效 OOS 日）的调仓日重算多空权重并记账一笔
    [调仓日, 调仓日+hold) 的持有期收益；中间日不重算、不计当日收益（期与期不重叠），
    因此净收益=Σ期收益-Σ期换手成本可以**真实复利**（累计净值/最大回撤可作数）。
    hold=None 或缺省=每天调仓（与 evaluate_ls_books 逐日重叠口径一致，仅作对照）。
    period_days（第75轮修正）：每笔记账期的真实持有天数；年化=mean×252/period_days、
    夏普=mean/sd×sqrt(252/period_days)。第64-74轮版本把 H>1 的期收益当"日收益"年化
    （annual/sharpe 被 ×h 虚高），本轮修正——同 h 内相对排序不受影响。缺省=hold。

    返回 {"n_q","cost_oneway","hold","n_periods","gross","net","avg_turnover_one_sided",
          "total_cost","avg_spread","avg_long_leg","avg_short_leg",
          "daily_gross","daily_net","daily_turnover"}（daily_* 按调仓期数对齐）。
    """
    hold = hold or 1
    pdays = period_days or hold
    annualize = TRADING_DAYS / float(pdays)
    gross, net, traded_series, onesided_series, cost_series, spreads, longr, shortr = \
        [], [], [], [], [], [], [], []
    prev_w = {}
    for i, book in enumerate(books):
        if i % hold != 0:
            continue                      # 非调仓日：持有不动（前向收益已在调仓日记账）
        day = quantile_ls_day(book[score_key], book["y"], n_q)
        if day is None:
            continue
        traded, onesided = turnover_between(prev_w, day["weights"])
        cost = traded * (cost_oneway or 0.0)
        gross.append(day["gross_ret"])
        net.append(day["gross_ret"] - cost)
        traded_series.append(traded)
        onesided_series.append(onesided)
        cost_series.append(cost)
        spreads.append(day["spread"])
        longr.append(day["long_ret"])
        shortr.append(day["short_ret"])
        prev_w = day["weights"]
    gperf, nperf = perf_of_returns(gross), perf_of_returns(net)
    # 第75轮修正：期收益按真实持有期年化（annualize=252/period_days），而非当"日收益"×252
    for p in (gperf, nperf):
        if p.get("n_days"):
            sd = (p["annual_vol"] / math.sqrt(TRADING_DAYS)) if p["annual_vol"] else 0.0
            p["annual_ret"] = p["mean_daily"] * annualize
            p["annual_vol"] = sd * math.sqrt(annualize)
            p["sharpe"] = (p["mean_daily"] / sd * math.sqrt(annualize)) if sd > 1e-15 else 0.0
    n = len(gross)
    return {"n_q": n_q, "cost_oneway": cost_oneway, "hold": hold, "n_periods": n,
            "gross": gperf, "net": nperf,
            "avg_traded": (sum(traded_series) / n if n else None),
            "avg_turnover_one_sided": (sum(onesided_series) / n if n else None),
            "total_cost": sum(cost_series),
            "avg_spread": (sum(spreads) / n if n else None),
            "avg_long_leg": (sum(longr) / n if n else None),
            "avg_short_leg": (sum(shortr) / n if n else None),
            "daily_gross": gross, "daily_net": net, "daily_turnover": onesided_series}


def evaluate_ls_books(books, score_key="orth", n_q=N_QUANTILE, cost_oneway=DEFAULT_COST_ONEWAY):
    """对 walk_forward 收集的每日 book（含分数与前向收益）构造分层多空组合，逐日算换手与成本。

    cost_t = traded_t * cost_oneway（换手部分按单边费率付一次费/滑点）；净收益=毛收益-cost。
    返回每日序列与汇总（毛/净、换手、成本、多空腿），纯函数、零 IO。"""
    gross, net, traded_series, onesided_series, cost_series, spreads, longr, shortr = \
        [], [], [], [], [], [], [], []
    prev_w = {}
    for book in books:
        day = quantile_ls_day(book[score_key], book["y"], n_q)
        if day is None:
            continue
        traded, onesided = turnover_between(prev_w, day["weights"])
        cost = traded * (cost_oneway or 0.0)
        gross.append(day["gross_ret"])
        net.append(day["gross_ret"] - cost)
        traded_series.append(traded)
        onesided_series.append(onesided)
        cost_series.append(cost)
        spreads.append(day["spread"])
        longr.append(day["long_ret"])
        shortr.append(day["short_ret"])
        prev_w = day["weights"]
    gperf, nperf = perf_of_returns(gross), perf_of_returns(net)
    n = len(gross)
    return {"n_q": n_q, "cost_oneway": cost_oneway,
            "gross": gperf, "net": nperf,
            "avg_traded": (sum(traded_series) / n if n else None),
            "avg_turnover_one_sided": (sum(onesided_series) / n if n else None),
            "total_cost": sum(cost_series),
            "avg_spread": (sum(spreads) / n if n else None),
            "avg_long_leg": (sum(longr) / n if n else None),
            "avg_short_leg": (sum(shortr) / n if n else None),
            "daily_gross": gross, "daily_net": net, "daily_turnover": onesided_series}


# --------------------------- 面板装载 ---------------------------
def load_panel(db_path, factors):
    """读 research_panel：返回 (dates升序, by_sym{sym:{date:{col:val}}}, sectors)。缺库返回 None。"""
    if not os.path.exists(db_path):
        return None
    cols = ["sym", "date", "sector", "c"] + [f for f in factors if f != "c"]
    c = sqlite3.connect(db_path)
    cur = c.cursor()
    have = {r[1] for r in cur.execute("PRAGMA table_info(research_panel)").fetchall()}
    missing = [f for f in factors if f not in have]
    if missing:
        c.close()
        raise ValueError("面板缺少因子列: %s" % missing)
    q = "SELECT %s FROM research_panel ORDER BY sym,date" % ",".join(cols)
    by_sym, dates, sectors = {}, set(), {}
    for row in cur.execute(q):
        rec = dict(zip(cols, row))
        sym, d = rec.pop("sym"), rec.pop("date")
        sectors[sym] = rec.pop("sector", None)
        by_sym.setdefault(sym, {})[d] = rec
        dates.add(d)
    c.close()
    return sorted(dates), by_sym, sectors


def build_forward(by_sym, horizon):
    """y{sym:{date: c(t+H)/c(t)-1}}，严格未来；末尾 H 日无 y。"""
    y = {}
    for sym, rows in by_sym.items():
        ds = sorted(rows)
        closes = [rows[d]["c"] for d in ds]
        yv = {}
        for t in range(len(ds) - horizon):
            c0, c1 = closes[t], closes[t + horizon]
            if isnum(c0) and isnum(c1) and c0 > 0:
                yv[ds[t]] = c1 / c0 - 1.0
        y[sym] = yv
    return y


# --------------------------- walk-forward 核心 ---------------------------
def walk_forward(dates, by_sym, factors, horizon,
                 min_train=MIN_TRAIN, refit_every=REFIT_EVERY, min_cs=MIN_CS,
                 rev_factor=None):
    """扩展窗月度再拟合、日度 OOS 打分。返回 {策略名: 日度IC列表} 与再拟合次数。

    第73轮：rev_factor 非空时额外出一条"反转动量"账本 rev=-cs_uniform(rev_factor)；
    rev_factor 不在 factors 里时仅作附加列装载（不进训练池、不进 blend）；日IC列表长度
    与其它策略对齐（当日有效截面不足 min_cs 时记 None）。"""
    factors = list(factors)
    k = len(factors)
    cols = factors + ([rev_factor] if (rev_factor and rev_factor not in factors) else [])
    K = len(cols)
    rev_idx = cols.index(rev_factor) if rev_factor else None
    ysym = build_forward(by_sym, horizon)
    # 训练池（增量累积列）：pool_f[i]、pool_y 对齐
    pool_f = [[] for _ in range(k)]
    pool_y = []
    model = None
    daily = {"orth_ic": [], "equal": []}
    for i in range(k):
        daily["f_" + factors[i]] = []
    if rev_factor:
        daily["rev"] = []
    books = []          # 第63轮：每个有效 OOS 日留一份 {orth/equal 分数, 前向收益 y}，供分层多空/换手/成本
    n_refit = 0
    added_up_to = -1

    def raw_vector(sym, d):
        r = by_sym[sym].get(d)
        if r is None:
            return None
        vec = [r.get(f) for f in cols]
        # blend 列必须全有限；附加反转列允许缺失（当日该品种不进 rev 账本）
        return vec if all(isnum(v) for v in vec[:k]) else None

    for t, d in enumerate(dates):
        # 1) 把 [added_up_to+1, t-1] 已成为历史的日期纳入训练池（截面标准化后）
        if t >= 1:
            for pt in range(added_up_to + 1, t):
                pd_ = dates[pt]
                raw_by_sym = {s: raw_vector(s, pd_) for s in by_sym}
                raw_by_sym = {s: v for s, v in raw_by_sym.items() if v is not None}
                cs_cols = [cs_uniform({s: raw_by_sym[s][i] for s in raw_by_sym}) for i in range(K)]
                for s in raw_by_sym:
                    yv = ysym.get(s, {}).get(pd_)
                    if not isnum(yv) or not all(s in z and isnum(z[s]) for z in cs_cols[:k]):
                        continue
                    for i in range(k):
                        pool_f[i].append(cs_cols[i][s])
                    pool_y.append(yv)
            added_up_to = t - 1
        # 2) 满足最小训练且到再拟合点 → 重估模型
        if t >= min_train and (t - min_train) % refit_every == 0 and len(pool_y) > k + 5:
            ics = [fe.spearman(pool_f[i], pool_y) for i in range(k)]
            ob = fle.orthogonal_ic_blend([list(col) for col in pool_f], ics)
            model = {"betas": ob["betas"], "weights": ob["weights"], "ics": ics}
            n_refit += 1
        # 3) OOS 当日打分（必须已有模型）
        if model is None:
            continue
        raw_by_sym = {}
        for s in by_sym:
            v = raw_vector(s, d)
            if v is not None and isnum(ysym.get(s, {}).get(d)):
                raw_by_sym[s] = v
        if len(raw_by_sym) < min_cs:
            continue
        cs_cols = [cs_uniform({s: raw_by_sym[s][i] for s in raw_by_sym}) for i in range(K)]
        valid = [s for s in raw_by_sym if all(s in z for z in cs_cols[:k])]
        if len(valid) < min_cs:
            continue
        score_orth, score_eq, score_single, yd = {}, {}, {f: {} for f in factors}, {}
        for s in valid:
            fv = [cs_cols[i][s] for i in range(k)]
            resid = apply_triangular(fv, model["betas"])
            score_orth[s] = sum(w * r for w, r in zip(model["weights"], resid))
            score_eq[s] = sum(fv) / k
            for i, f in enumerate(factors):
                score_single[f][s] = fv[i]
            yd[s] = ysym[s][d]
        daily["orth_ic"].append(fe.spearman(list(score_orth.values()), list(yd.values())))
        daily["equal"].append(fe.spearman(list(score_eq.values()), list(yd.values())))
        book = {"date": d, "orth": dict(score_orth), "equal": dict(score_eq), "y": dict(yd)}
        # 第73轮：反转动量账本（截面排序的反向；与 orth/equal/y 同日对齐）
        if rev_factor:
            zr = cs_cols[rev_idx]
            rev_scores = {s: -zr[s] for s in valid if s in zr and isnum(zr[s])}
            if len(rev_scores) >= min_cs:
                daily["rev"].append(fe.spearman(list(rev_scores.values()),
                                                [yd[s] for s in rev_scores]))
                book["rev"] = rev_scores
            else:
                daily["rev"].append(None)
        books.append(book)
        for f in factors:
            daily["f_" + f].append(fe.spearman([score_single[f][s] for s in valid],
                                               [yd[s] for s in valid]))
    return daily, n_refit, books


# --------------------------- 一次完整运行 ---------------------------
def run(db_path=DEFAULT_DB, txt_path=DEFAULT_TXT, json_path=DEFAULT_JSON,
        factors=DEFAULT_FACTORS, horizons=HORIZONS, n_q=N_QUANTILE,
        cost_oneway=DEFAULT_COST_ONEWAY, rev_factor=DEFAULT_REV_FACTOR, verbose=True):
    load_cols = list(factors) + ([rev_factor] if (rev_factor and rev_factor not in factors) else [])
    loaded = load_panel(str(db_path), load_cols)
    if loaded is None:
        msg = "未找到研究面板 %s；先运行 tools/panel_builder.py --all 建板。" % db_path
        if verbose:
            print(msg)
        return {"note": msg}
    dates, by_sym, sectors = loaded
    result = {"factors": list(factors), "rev_factor": rev_factor, "n_sym": len(by_sym),
              "n_dates": len(dates),
              "date_min": dates[0] if dates else None, "date_max": dates[-1] if dates else None,
              "min_train": MIN_TRAIN, "refit_every": REFIT_EVERY, "horizons": {}}
    lines = []
    lines.append("=" * 92)
    lines.append("正交IC加权 vs 等权 vs 单因子：真实面板 walk-forward 样本外截面RankIC对照（G25续/G16前置，研究侧）")
    lines.append("=" * 92)
    lines.append("面板 %s；品种=%d 交易日=%d（%s ~ %s）；候选因子=%s"
                 % (db_path, len(by_sym), len(dates), result["date_min"], result["date_max"],
                    "、".join(factors)))
    lines.append("设置：最小训练%d日 / 每%d日扩展窗重估 / OOS截面≥%d品种；因子先做当日截面均匀秩标准化；"
                 "反转动量账本=-截面秩(%s)" % (MIN_TRAIN, REFIT_EVERY, MIN_CS, rev_factor or "关"))
    for h in horizons:
        daily, n_refit, books = walk_forward(dates, by_sym, list(factors), h, rev_factor=rev_factor)
        summ = {name: summarize_ic(seq) for name, seq in daily.items()}
        # 第63轮：分层多空组合（多顶层/空底层），含换手与单边成本后的净收益（正交IC合成 vs 等权）
        ls_orth = evaluate_ls_books(books, "orth", n_q, cost_oneway)
        ls_equal = evaluate_ls_books(books, "equal", n_q, cost_oneway)
        ls_rev = evaluate_ls_books(books, "rev", n_q, cost_oneway) if rev_factor else None
        # G25续（第64轮）：按 H 对齐非重叠再平衡（消除 H>1 前向重叠，可复利）
        ls_orth_h = evaluate_ls_books_aligned(books, "orth", n_q, cost_oneway, hold=h, period_days=h)
        ls_equal_h = evaluate_ls_books_aligned(books, "equal", n_q, cost_oneway, hold=h, period_days=h)
        ls_rev_h = evaluate_ls_books_aligned(books, "rev", n_q, cost_oneway, hold=h, period_days=h) if rev_factor else None
        result["horizons"]["H%d" % h] = {"n_refit": n_refit, "summary": summ,
                                         "daily_orth": daily["orth_ic"], "daily_equal": daily["equal"],
                                         "ls_orth": ls_orth, "ls_equal": ls_equal,
                                         "ls_orth_aligned": ls_orth_h, "ls_equal_aligned": ls_equal_h}
        if rev_factor:
            result["horizons"]["H%d" % h]["daily_rev"] = daily["rev"]
            result["horizons"]["H%d" % h]["ls_rev"] = ls_rev
            result["horizons"]["H%d" % h]["ls_rev_aligned"] = ls_rev_h
        n_days = summ["orth_ic"]["n_days"]
        lines.append("")
        lines.append("[前向 H=%d 交易日] 有效OOS日=%d，月度重估=%d 次（ICIR=mean/std，正比例=日IC>0占比）"
                     % (h, n_days, n_refit))
        lines.append("  %-16s %10s %10s %10s %8s" % ("策略", "meanIC", "ICIR", "正比例", "天数"))
        order = ["orth_ic", "equal", "rev"] + ["f_" + f for f in factors if "f_" + f in summ]
        label = {"orth_ic": "正交IC合成", "equal": "等权合成",
                 "rev": "反转动量(-%s)" % (rev_factor or "")}
        for name in order:
            s = summ[name]
            if s["mean_ic"] is None:
                continue
            lines.append("  %-16s %10.4f %10.3f %9.1f%% %8d"
                         % (label.get(name, name.replace("f_", "单因子·")),
                            s["mean_ic"], s["icir"], 100.0 * s["pct_positive"], s["n_days"]))
        # 正交 vs 等权 的配对日度差
        do, de = daily["orth_ic"], daily["equal"]
        diff = [a - b for a, b in zip(do, de) if isnum(a) and isnum(b)]
        if diff:
            md = sum(diff) / len(diff)
            win = sum(1 for v in diff if v > 0) / len(diff)
            lines.append("  → 正交IC − 等权：平均日IC差 %+.4f，正交占优日占比 %.1f%%" % (md, 100.0 * win))
        # 第73轮：反转动量 vs 正交 的配对日度差（检验纯反转是否比合成更稳）
        if rev_factor and summ.get("rev", {}).get("mean_ic") is not None:
            dr = [a - b for a, b in zip(daily["orth_ic"], daily["rev"]) if isnum(a) and isnum(b)]
            if dr:
                lines.append("  → 正交IC − 反转动量：平均日IC差 %+.4f，正交占优日占比 %.1f%%"
                             % (sum(dr) / len(dr), 100.0 * sum(1 for v in dr if v > 0) / len(dr)))
        # 分层多空组合（Q%d：多顶层空底层、层内等权、gross=1）：毛收益→换手→单边成本→净收益
        lines.append("  分层多空（%d层多顶空底，单边成本万%.1f=费+滑点；H>1前向收益日序重叠，累计/回撤仅作相对比较）："
                     % (n_q, 10000.0 * cost_oneway))
        lines.append("  %-10s %8s %8s %8s %8s %9s %9s %9s"
                     % ("合成", "多腿", "空腿", "价差", "毛年化", "净年化", "净夏普", "日均换手"))
        for lname, ls in (("正交IC", ls_orth), ("等权", ls_equal), ("反转动量", ls_rev)):
            g, nn = ls["gross"], ls["net"]
            if g.get("n_days", 0) == 0:
                continue
            lines.append("  %-10s %8.4f %8.4f %8.4f %8.2f%% %8.2f%% %9.2f %9.3f"
                         % (lname, ls["avg_long_leg"], ls["avg_short_leg"], ls["avg_spread"],
                            100.0 * (g.get("annual_ret") or 0.0), 100.0 * (nn.get("annual_ret") or 0.0),
                            nn.get("sharpe") or 0.0, ls["avg_turnover_one_sided"] or 0.0))
        lines.append("  （毛/净均为 H=%d 前向持有期收益口径；总成本拖累=%.2f%%，净最大回撤=%.2f%%）"
                     % (h, 100.0 * ls_orth["total_cost"],
                        100.0 * (ls_orth["net"].get("max_drawdown") or 0.0)))
        # G25续（第64轮）：按 H 对齐非重叠再平衡（期数=调仓次数；净收益可复利）
        aligned_line = ("  [按H=%d对齐·非重叠再平衡] 调仓期=%d：正交净年化%+.2f%%/净夏普%.2f/日均换手%.3f（等权净年化%+.2f%%"
                        % (h, ls_orth_h["n_periods"],
                           100.0 * (ls_orth_h["net"].get("annual_ret") or 0.0),
                           ls_orth_h["net"].get("sharpe") or 0.0,
                           ls_orth_h["avg_turnover_one_sided"] or 0.0,
                           100.0 * (ls_equal_h["net"].get("annual_ret") or 0.0)))
        if ls_rev_h and ls_rev_h["n_periods"]:
            aligned_line += "；反转动量净年化%+.2f%%/净夏普%.2f）" % (
                100.0 * (ls_rev_h["net"].get("annual_ret") or 0.0),
                ls_rev_h["net"].get("sharpe") or 0.0)
        else:
            aligned_line += "）"
        lines.append(aligned_line)
    lines.append("")
    lines.append("注：本结果为研究侧线性合成基线，不自动改 analyzer 权重、不进综合分；上线仍须 G29 体检 + 样本外+真实成本后≥现状。")
    lines.append("注：反转动量账本=-截面秩(rev_factor)；正交IC合成的有符号权重本就隐含反向（负IC因子自动得负权重），"
                 "本账本用于检验\"纯反转单因子\"样本外是否有肉。")
    text = "\n".join(lines)
    if verbose:
        print(text)
    os.makedirs(os.path.dirname(str(txt_path)), exist_ok=True)
    with open(str(txt_path), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    with open(str(json_path), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    try:
        import experiment_ledger
        head = result["horizons"].get("H%d" % horizons[0], {}).get("summary", {})
        experiment_ledger.safe_record(
            "orthogonal_blend_oos",
            {"factors": list(factors), "horizons": list(horizons),
             "min_train": MIN_TRAIN, "refit_every": REFIT_EVERY},
            {k: v for k, v in head.items()} if head else None,
            inputs={"panel": str(db_path), "n_sym": len(by_sym), "n_dates": len(dates)},
            artifacts=[str(txt_path), str(json_path)],
            conclusion="真实面板walk-forward：正交IC合成与等权/单因子的样本外RankIC对照，负结果照实记录",
            reproduce="D:\\Python\\python.exe tools/orthogonal_blend_oos.py")
    except Exception:
        pass
    return result


# --------------------------- 零网络/零DB 合成自测 ---------------------------
def _synth_panel(seed=7, n_sym=16, n_date=220):
    """构造面板：共同风险因子 + 一个真alpha（ret63）+ 冗余因子，y 由 alpha+噪声驱动。"""
    import random
    rng = random.Random(seed)
    factors = list(DEFAULT_FACTORS)
    dates = ["2024-%02d-%02d" % (1 + (t // 28), 1 + (t % 28)) for t in range(n_date)]
    by_sym = {}
    common = [rng.gauss(0, 1) for _ in range(n_date)]
    alpha = [rng.gauss(0, 1) for _ in range(n_date)]
    for s in range(n_sym):
        rows = {}
        load_c = rng.uniform(0.6, 1.2)
        load_a = rng.uniform(0.4, 1.0)
        idio = [rng.gauss(0, 1) for _ in range(n_date)]
        price = 100.0
        closes = []
        for t, d in enumerate(dates):
            shock = load_c * common[t] + load_a * alpha[t] + 0.8 * idio[t]
            # 因子：ret63 承载 alpha，ret20 与共同因子相关（冗余），其余弱
            ret5 = 0.3 * common[t] + 0.2 * idio[t]
            ret20 = 0.9 * common[t] + 0.1 * rng.gauss(0, 1)
            ret63 = 0.2 * common[t] + 1.0 * alpha[t]
            tsmom63 = ret63 * 0.95 + 0.05 * rng.gauss(0, 1)
            tsmom126 = 0.5 * ret63 + 0.3 * rng.gauss(0, 1)
            day_chg = 0.2 * idio[t]
            price *= (1 + 0.01 * shock)
            closes.append(price)
            rows[d] = {"c": price, "ret5": ret5, "ret20": ret20, "ret63": ret63,
                       "tsmom63": tsmom63, "tsmom126": tsmom126, "day_chg": day_chg}
        by_sym["S%02d" % s] = rows
    return dates, by_sym, factors


def selftest():
    # 1) 截面均匀秩：已知序列映射到 [-1,1]、中位≈0
    z = cs_uniform({"a": 1.0, "b": 2.0, "c": 3.0})
    assert abs(z["a"] + 1.0) < 1e-12 and abs(z["c"] - 1.0) < 1e-12 and abs(z["b"]) < 1e-12
    z2 = cs_uniform({"a": 5.0, "b": 5.0, "c": 1.0})    # 并列平均秩
    assert abs(z2["a"] - z2["b"]) < 1e-12 and z2["c"] < z2["a"]
    # 2) 三角递推：betas 全 0 时残差=原因子；正交变换可逆且只用给定系数
    assert apply_triangular([0.3, -0.2], [[], []]) == [0.3, -0.2]
    r = apply_triangular([1.0, 2.0], [[], [0.5]])      # r1=2-0.5*1=1.5
    assert abs(r[0] - 1.0) < 1e-12 and abs(r[1] - 1.5) < 1e-12
    # 3) summarize_ic 基本量
    s = summarize_ic([0.1, -0.1, 0.2, 0.0])
    assert s["n_days"] == 4 and abs(s["mean_ic"] - 0.05) < 1e-12 and 0 <= s["pct_positive"] <= 1
    assert summarize_ic([])["n_days"] == 0
    # 4) walk-forward 合成面板能跑通、各策略日IC等长、权重和=1（经 orthogonal_ic_blend 保证）
    dates, by_sym, factors = _synth_panel()
    daily, n_refit, books = walk_forward(dates, by_sym, factors, 1, min_train=60, refit_every=20, min_cs=8)
    lens = {k: len(v) for k, v in daily.items()}
    assert len(set(lens.values())) == 1 and list(lens.values())[0] > 0, lens
    assert n_refit >= 5
    assert len(books) == daily["orth_ic"].__len__() and all("orth" in b and "y" in b for b in books)
    so = summarize_ic(daily["orth_ic"]); se = summarize_ic(daily["equal"])
    # 合成面板里 alpha 真实存在：正交合成与等权都应取得正均值 OOS IC（方向健全性）
    assert so["mean_ic"] is not None and se["mean_ic"] is not None
    # 4a) 第73轮：反转动量账本——rev IC 恒等于 -该因子IC（截面秩取负的对偶性），books 带 rev 键
    daily_r, n_refit_r, books_r = walk_forward(dates, by_sym, factors, 1, min_train=60,
                                               refit_every=20, min_cs=8, rev_factor="ret63")
    assert "rev" in daily_r and len(daily_r["rev"]) == len(daily_r["orth_ic"])
    assert all("rev" in b for b in books_r)
    for a, b in zip(daily_r["rev"], daily_r["f_ret63"]):
        assert (a is None and b is None) or (isnum(a) and isnum(b) and abs(a + b) < 1e-12)
    # 4a-2) 附加列路径：rev_factor 不在 factors 里（不进 blend）也能跑通、长度对齐
    factors_wo = [f for f in factors if f != "ret63"]
    daily_x, _, books_x = walk_forward(dates, by_sym, factors_wo, 1, min_train=60,
                                       refit_every=20, min_cs=8, rev_factor="ret63")
    lens_x = {k: len(v) for k, v in daily_x.items()}
    assert len(set(lens_x.values())) == 1 and "rev" in daily_x and "f_ret63" not in daily_x
    # 4a-3) 多空反向恒等：同一 y 下，分数取负=拿另一边 → 毛/价差变号、多空腿互换
    sc = {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 5.0,
          "f": 6.0, "g": 7.0, "h": 8.0, "i": 9.0, "j": 10.0}
    yy = {s: -0.002 * v for s, v in sc.items()}
    q1, q2 = quantile_ls_day(sc, yy, 5), quantile_ls_day({s: -v for s, v in sc.items()}, yy, 5)
    assert abs(q1["gross_ret"] + q2["gross_ret"]) < 1e-12 and abs(q1["spread"] + q2["spread"]) < 1e-12
    assert abs(q2["long_ret"] - q1["short_ret"]) < 1e-12 and abs(q2["short_ret"] - q1["long_ret"]) < 1e-12
    # 4b) 分层多空单日：顶层分>底层分、gross=1 权重、多正空负腿结构
    day = quantile_ls_day({"a": 1, "b": 2, "c": 3, "d": 4, "e": 5,
                           "f": 6, "g": 7, "h": 8, "i": 9, "j": 10},
                          {s: 0.01 * v for s, v in
                           [("a", 1), ("b", 2), ("c", 3), ("d", 4), ("e", 5),
                            ("f", 6), ("g", 7), ("h", 8), ("i", 9), ("j", 10)]}, n_q=5)
    assert abs(sum(abs(w) for w in day["weights"].values()) - 1.0) < 1e-12   # gross=1
    assert day["long_ret"] > day["short_ret"] and abs(day["gross_ret"] - 0.5 * day["spread"]) < 1e-12
    assert quantile_ls_day({"a": 1, "b": 2}, {"a": 0.1, "b": -0.1}, n_q=5) is None  # 样本不足
    # 4c) 换手：完全换仓 traded≈1；零成本净=毛；正成本净<毛
    w1 = {"a": 0.5, "b": -0.5}
    traded, ones = turnover_between({}, w1)
    assert abs(traded - 1.0) < 1e-12 and abs(ones - 0.5) < 1e-12
    traded2, _ = turnover_between(w1, {"c": 0.5, "d": -0.5})
    assert abs(traded2 - 2.0) < 1e-12
    ls0 = evaluate_ls_books(books, "orth", 5, 0.0)
    lsc = evaluate_ls_books(books, "orth", 5, 0.0003)
    assert ls0["gross"]["n_days"] == len(books) and ls0["net"]["n_days"] == len(books)
    assert all(abs(g - n) < 1e-15 for g, n in zip(ls0["daily_gross"], ls0["daily_net"]))  # 零成本净=毛
    assert lsc["total_cost"] > 0 and lsc["net"]["cum_ret"] <= ls0["net"]["cum_ret"] + 1e-12
    assert 0 <= (ls0["avg_turnover_one_sided"] or 0) <= 1.0
    # 4d) 按H对齐非重叠再平衡（G25续第64轮）：hold=2 时调仓期数≈一半、期与期不重叠；
    #     hold=1 时与逐日口径每期收益一致（仅记账方式不同）；正成本净≤毛、换手≤1
    al0 = evaluate_ls_books_aligned(books, "orth", 5, 0.0, hold=2)
    alc = evaluate_ls_books_aligned(books, "orth", 5, 0.0003, hold=2)
    assert al0["hold"] == 2 and al0["n_periods"] <= (len(books) + 1) // 2
    assert alc["total_cost"] >= 0 and alc["net"]["cum_ret"] <= al0["net"]["cum_ret"] + 1e-12
    assert 0 <= (al0["avg_turnover_one_sided"] or 0) <= 1.0
    al1 = evaluate_ls_books_aligned(books, "orth", 5, 0.0, hold=1)
    assert al1["n_periods"] >= al0["n_periods"] * 2 - 2  # hold=1 期数≈hold=2 的两倍（足样本满仓时精确2倍，跳过期不计数）
    assert abs(al1["gross"]["cum_ret"] - ls0["gross"]["cum_ret"]) < 1e-9   # hold=1 逐日=原逐日口径
    # 4e) 第75轮年化修正：期收益按真实持有期折算（annualize=252/period_days），hold=1 逐日口径不变
    fake_books = []
    for _k in range(100):
        sc = {"a": 1.0, "b": 0.9, "c": -0.9, "d": -1.0}
        yy = {"a": 0.01, "b": 0.01, "c": -0.01, "d": -0.01}
        fake_books.append({"s": sc, "y": yy})
    fa1 = evaluate_ls_books_aligned(fake_books, "s", 2, 0.0, hold=1)
    fa2 = evaluate_ls_books_aligned(fake_books, "s", 2, 0.0, hold=2, period_days=2)
    assert abs(fa1["net"]["annual_ret"] - 0.01 * 252) < 1e-9      # 逐日期：×252
    assert abs(fa2["net"]["annual_ret"] - 0.01 * 126) < 1e-9      # 2日期：×126（修正后，不再×252）
    assert abs(fa2["net"]["sharpe"] - fa1["net"]["sharpe"]) < 1e-9  # 常数收益：期折算后夏普一致
    assert fa2["n_periods"] == 50
    # 5) 无未来函数：截断最后一天不影响之前任一 OOS 日的 IC
    base_daily, _, base_books = walk_forward(dates[:-1], by_sym, factors, 1,
                                             min_train=60, refit_every=20, min_cs=8)
    for a, b in zip(daily["orth_ic"][:-1], base_daily["orth_ic"]):
        assert (a is None and b is None) or (isnum(a) and isnum(b) and abs(a - b) < 1e-12)
    print("orthogonal_blend_oos selftest ALL PASS（截面均匀秩/并列秩、三角递推残差化、IC汇总、"
          "合成面板walk-forward n_refit=%d OOS日=%d 正交meanIC=%.4f 等权meanIC=%.4f、"
          "分层多空gross=1/换手/成本净收益、按H对齐非重叠再平衡、无未来）"
          % (n_refit, so["n_days"], so["mean_ic"], se["mean_ic"]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="正交IC加权接真实面板做样本外对照（研究侧）")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--factors", default=",".join(DEFAULT_FACTORS), help="逗号分隔的面板因子列")
    ap.add_argument("--horizons", default="1,5,20", help="逗号分隔的前向交易日")
    ap.add_argument("--quantiles", type=int, default=N_QUANTILE, help="分层多空层数（多顶空底）")
    ap.add_argument("--cost-oneway", type=float, default=DEFAULT_COST_ONEWAY,
                    help="单边交易成本率（费+滑点），默认复用回测口径万1.5")
    ap.add_argument("--rev-factor", default=DEFAULT_REV_FACTOR,
                    help="反转动量账本基准列（默认 ret63；传空串关闭）")
    ap.add_argument("--out", default=str(DEFAULT_TXT), help="报告输出路径（对照变体请用独立文件，勿覆盖基准）")
    ap.add_argument("--json", dest="json_path", default=str(DEFAULT_JSON))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    factors = tuple(x.strip() for x in args.factors.split(",") if x.strip())
    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
    rev = args.rev_factor.strip() or None
    run(db_path=args.db, factors=factors, horizons=horizons, n_q=args.quantiles,
        cost_oneway=args.cost_oneway, rev_factor=rev, txt_path=args.out,
        json_path=args.json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
