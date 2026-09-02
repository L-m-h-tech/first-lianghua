# -*- coding: utf-8 -*-
"""G3 完整绩效指标包 / tear sheet（纯标准库、零网络、零第三方依赖）。

统一绩效口径，供 portfolio.py / paper_broker.py / backtest.py / charts.py 复用，
替代各处各写一套的风险/盈亏统计。对标 pyfolio/empyrical 的指标清单，全部用标准库重写。

两类输入（不要混用）：
1) 周期收益序列 returns：每个元素是一个【固定周期】（如一个交易日）的收益率小数，
   用于风险、调整后收益、回撤、分布、滚动类指标；
2) 逐笔盈亏序列 trade_pnls：每个元素是一笔已平仓交易的盈亏（元或比例，方向带符号），
   用于胜率/盈亏比/profit factor/连胜连亏等交易级统计。

统一纪律（验收口径，见融合总纲 G3）：
- 只做确定性计算，不联网、不读 DB、不引入第三方库；
- 样本不足一律返回 None（由调用方显示“样本不足/-”），绝不抛异常；
- 所有比例输入输出都是【小数】（0.02 = 2%）；
- 年化周期数 bars_per_year 由调用方按数据频率传入（日度=243、30m 分钟另算）。

运行自检：D:\\Python\\python.exe metrics.py --selftest（全部为手算可复核的合成断言）。
"""
import argparse
import math
import statistics

# 年度交易日数（与 portfolio.performance 默认一致；国内期货剔除长假后约 243）
DEFAULT_BARS_PER_YEAR = 243
# 历史法 VaR/CVaR 的默认左尾概率
DEFAULT_VAR_ALPHA = 0.05
# 滚动夏普默认窗口（个周期；日度即交易日）
DEFAULT_ROLLING_WINDOW = 60
MIN_ROLLING = 3          # 少于该样本数滚动指标不给值（样本 stdev 至少要 2，3 起步更稳）


# =========================== 基础工具 ===========================

def clean_returns(returns):
    """过滤非有限值，返回 float 列表（不改变顺序）。"""
    if returns is None:
        return []
    out = []
    for r in returns:
        try:
            v = float(r)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            out.append(v)
    return out


def quantile_linear(sorted_vals, q):
    """线性插值分位数（同 numpy linear / R type7），输入须已升序；空返回 None。"""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    q = min(1.0, max(0.0, float(q)))
    pos = q * (len(sorted_vals) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return float(sorted_vals[lo])
    return (float(sorted_vals[lo]) * (hi - pos)
            + float(sorted_vals[hi]) * (pos - lo))


def returns_from_equity(equity):
    """逐点权益 -> 逐期简单收益率（长度=len(equity)-1）；非正权益跳过该断点。"""
    out = []
    prev = None
    for x in equity:
        try:
            v = float(x)
        except (TypeError, ValueError):
            prev = None
            continue
        if not math.isfinite(v):
            prev = None
            continue
        if prev is not None and prev > 0:
            out.append(v / prev - 1.0)
        prev = v
    return out


def _date_key(value):
    """从 'YYYY-MM-DD HH:MM:SS' / date / datetime 取 'YYYY-MM-DD'；取不到返回 None。"""
    if value is None:
        return None
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%Y-%m-%d")
        except Exception:
            return None
    s = str(value).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return None


def daily_last_equity(dates, equity):
    """按自然日取每日最后一个权益点，返回 (day_labels, day_equity)（升序、去重）。

    用于把一天多轮的纸面快照 / 逐 bar 权益曲线收敛成日度序列再算年化类指标。"""
    day_last = {}
    order = []
    for d, e in zip(dates, equity):
        k = _date_key(d)
        if k is None:
            continue
        try:
            v = float(e)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):
            continue
        if k not in day_last:
            order.append(k)
        day_last[k] = v            # 后写覆盖=当日最后一点
    order.sort()
    return order, [day_last[k] for k in order]


# =========================== 周期收益类指标 ===========================

def cumulative_return(returns):
    """复利累计收益 ∏(1+r)-1。"""
    rs = clean_returns(returns)
    if not rs:
        return None
    eq = 1.0
    for r in rs:
        eq *= 1.0 + r
    return eq - 1.0


def mean_return(returns):
    rs = clean_returns(returns)
    return sum(rs) / len(rs) if rs else None


def annualized_return(returns, bars_per_year=DEFAULT_BARS_PER_YEAR):
    """算术年化（均值×年周期数），与既有 portfolio.performance 的 ann_ret 同口径。"""
    rs = clean_returns(returns)
    if not rs:
        return None
    return sum(rs) / len(rs) * bars_per_year


def cagr(returns, bars_per_year=DEFAULT_BARS_PER_YEAR):
    """几何年化（复合年增长率 CAGR）；累计为负且非整数年时无法开实幂返回 None。"""
    rs = clean_returns(returns)
    n = len(rs)
    if n <= 0:
        return None
    eq = 1.0
    for r in rs:
        eq *= 1.0 + r
    if eq <= 0:
        return None
    years = n / float(bars_per_year)
    if years <= 0:
        return None
    return eq ** (1.0 / years) - 1.0


def volatility(returns, bars_per_year=DEFAULT_BARS_PER_YEAR):
    """年化波动率（样本标准差 stdev，n>=2）。"""
    rs = clean_returns(returns)
    if len(rs) < 2:
        return None
    sd = statistics.stdev(rs)
    return sd * math.sqrt(bars_per_year)


def sharpe_ratio(returns, bars_per_year=DEFAULT_BARS_PER_YEAR, rf_period=0.0):
    """年化夏普 =（周期均值-周期无风险）/样本stdev × √年周期数；stdev≈0 或样本<2 返回 None。"""
    rs = clean_returns(returns)
    if len(rs) < 2:
        return None
    sd = statistics.stdev(rs)
    if sd <= 1e-15:
        return 0.0
    return (sum(rs) / len(rs) - rf_period) / sd * math.sqrt(bars_per_year)


def downside_deviation(returns, mar=0.0, bars_per_year=None):
    """下行偏差 = sqrt(mean(min(r-mar,0)^2))，分母是【全部】期数（empyrical 口径）。

    bars_per_year 给定时做年化（×√年周期数），不给则返回周期口径。"""
    rs = clean_returns(returns)
    if not rs:
        return None
    sq = sum(min(r - mar, 0.0) ** 2 for r in rs) / len(rs)
    dd = math.sqrt(sq)
    return dd * math.sqrt(bars_per_year) if bars_per_year else dd


def sortino_ratio(returns, bars_per_year=DEFAULT_BARS_PER_YEAR, mar=0.0):
    """年化索提诺 =（均值-MAR）/下行偏差 × √年周期数；下行偏差≈0 返回 None/0。"""
    rs = clean_returns(returns)
    if not rs:
        return None
    dd = downside_deviation(rs, mar)
    if dd <= 1e-15:
        return 0.0
    return (sum(rs) / len(rs) - mar) / dd * math.sqrt(bars_per_year)


def drawdown_series(returns):
    """逐期水下回撤序列（正小数，0=在峰值上）：由复利净值相对历史峰值计算，长度=len(returns)。"""
    rs = clean_returns(returns)
    eq, peak = 1.0, 1.0
    out = []
    for r in rs:
        eq *= 1.0 + r
        peak = max(peak, eq)
        out.append(max(0.0, 1.0 - eq / peak) if peak > 0 else 0.0)
    return out


def max_drawdown(returns):
    """最大回撤（正小数）。"""
    dd = drawdown_series(returns)
    return max(dd) if dd else None


def calmar_ratio(returns, bars_per_year=DEFAULT_BARS_PER_YEAR):
    """Calmar = 几何年化(CAGR) / 最大回撤；回撤≈0 或无法年化返回 None。"""
    rs = clean_returns(returns)
    if len(rs) < 2:
        return None
    mdd = max_drawdown(rs)
    if not mdd or mdd <= 1e-15:
        return None
    g = cagr(rs, bars_per_year)
    if g is None:
        return None
    return g / mdd


def omega_ratio(returns, threshold=0.0):
    """Omega = Σmax(r-thr,0) / |Σmin(r-thr,0)|；无亏损（分母0）返回 None（而非无穷大）。"""
    rs = clean_returns(returns)
    if not rs:
        return None
    gains = sum(max(r - threshold, 0.0) for r in rs)
    losses = -sum(min(r - threshold, 0.0) for r in rs)
    if losses <= 1e-18:
        return None
    return gains / losses


def ulcer_index(returns):
    """Ulcer 指数 = sqrt(mean(dd^2))：同时惩罚回撤深度与持续时间（比单点 maxDD 更全面）。"""
    dd = drawdown_series(returns)
    if not dd:
        return None
    return math.sqrt(sum(x * x for x in dd) / len(dd))


def value_at_risk(returns, alpha=DEFAULT_VAR_ALPHA):
    """历史法 VaR（左尾 alpha 分位数，线性插值；通常为负小数，如 -0.03=95% 置信下单日损失不超过3%）。"""
    rs = clean_returns(returns)
    if len(rs) < 2:
        return None
    return quantile_linear(sorted(rs), alpha)


def conditional_var(returns, alpha=DEFAULT_VAR_ALPHA):
    """CVaR/ES：劣于等于 VaR 分位的左尾均值（负小数）；无左尾样本返回 None。"""
    rs = clean_returns(returns)
    if len(rs) < 2:
        return None
    var = value_at_risk(rs, alpha)
    tail = [r for r in rs if r <= var + 1e-15]
    if not tail:
        return None
    return sum(tail) / len(tail)


def rolling_sharpe(returns, window=DEFAULT_ROLLING_WINDOW,
                   bars_per_year=DEFAULT_BARS_PER_YEAR):
    """滚动年化夏普：每个时点取末尾 window 个周期计算；前 window-1 个为 None。返回等长列表。"""
    rs = clean_returns(returns)
    w = int(window)
    out = [None] * len(rs)
    if w < MIN_ROLLING or len(rs) < w:
        return out
    for i in range(w - 1, len(rs)):
        seg = rs[i - w + 1:i + 1]
        sd = statistics.stdev(seg)
        if sd > 1e-15:
            out[i] = (sum(seg) / w) / sd * math.sqrt(bars_per_year)
        else:
            out[i] = 0.0
    return out


def monthly_returns(returns, dates):
    """把周期收益按自然月复利聚合为月度收益矩阵。

    返回 {"years":[升序年份int], "cells":[[year, month(1-12), ret小数], ...],
          "matrix": {year: {1..12: ret}}}。dates 与 returns 等长，元素可为字符串/date。
    """
    rs = clean_returns(returns)
    if not rs or dates is None or len(dates) != len(returns):
        return None
    mult = {}        # (year, month) -> 月度净值乘数
    for r0, d in zip(returns, dates):
        k = _date_key(d)
        if not k:
            continue
        try:
            year, month = int(k[0:4]), int(k[5:7])
        except ValueError:
            continue
        key = (year, month)
        mult[key] = mult.get(key, 1.0) * (1.0 + float(r0))
    if not mult:
        return None
    matrix, cells = {}, []
    for (year, month), v in sorted(mult.items()):
        matrix.setdefault(year, {})[month] = v - 1.0
        cells.append([year, month, v - 1.0])
    return {"years": sorted(matrix), "cells": cells, "matrix": matrix}


# =========================== 逐笔交易类指标 ===========================

def trade_stats(trade_pnls):
    """逐笔盈亏 -> 交易级统计字典。金额/比例均可（带符号），样本为空返回 None。

    n/win_rate/avg_win(正)/avg_loss(负)/avg_pnl/expectancy(=avg_pnl)/
    profit_factor=总盈利/|总亏损|/payoff_ratio=平均盈/|平均亏|/
    max_win_streak/max_loss_streak/best/worst/gross_profit/gross_loss。"""
    pnls = clean_returns(trade_pnls)
    if not pnls:
        return None
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)                 # 负数
    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    # 连胜/连亏（按交易顺序；0 盈亏不中断也不计入）
    max_ws = max_ls = cur_w = cur_l = 0
    for p in pnls:
        if p > 0:
            cur_w += 1
            cur_l = 0
        elif p < 0:
            cur_l += 1
            cur_w = 0
        max_ws = max(max_ws, cur_w)
        max_ls = max(max_ls, cur_l)
    return {
        "n": len(pnls), "n_win": len(wins), "n_loss": len(losses),
        "n_flat": len(pnls) - len(wins) - len(losses),
        "win_rate": len(wins) / len(pnls),
        "avg_pnl": sum(pnls) / len(pnls),
        "expectancy": sum(pnls) / len(pnls),
        "avg_win": avg_win, "avg_loss": avg_loss,
        "gross_profit": gross_profit, "gross_loss": gross_loss,
        "profit_factor": (gross_profit / abs(gross_loss)) if gross_loss < -1e-18 else None,
        "payoff_ratio": (avg_win / abs(avg_loss)) if avg_loss < -1e-18 else None,
        "max_win_streak": max_ws, "max_loss_streak": max_ls,
        "best": max(pnls), "worst": min(pnls),
    }


def excursion(direction, entry_price, path_prices):
    """单笔持仓过程的最大有利/不利偏移（MFE/MAE，正小数）。

    direction=+1 多 / -1 空；path_prices 为开仓之后（含出场）的一系列盯市价格。
    有利偏移 = direction*(p-entry)/entry；MFE=其最大值(≥0)，MAE=其最小值的绝对值(≥0)。
    入场价非正或路径空返回 (None, None)。"""
    try:
        entry = float(entry_price)
    except (TypeError, ValueError):
        return None, None
    if entry <= 0 or not path_prices:
        return None, None
    fav = []
    for p in path_prices:
        try:
            v = float(p)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v) and v > 0:
            fav.append(direction * (v - entry) / entry)
    if not fav:
        return None, None
    mfe = max(max(fav), 0.0)
    mae = max(-min(fav), 0.0)
    return mfe, mae


def mae_mfe_summary(records):
    """汇总一批持仓的 MFE/MAE。records: [{'mfe':>=0,'mae':>=0,'win':bool?}, ...]
    （mfe/mae 也可由 excursion() 得到后填入）。返回均值与盈亏分组；空返回 None。"""
    mfe_all, mae_all, mfe_win, mae_win, mfe_loss, mae_loss = [], [], [], [], [], []
    for r in records or []:
        try:
            mfe, mae = float(r.get("mfe")), float(r.get("mae"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(mfe) and math.isfinite(mae)):
            continue
        mfe_all.append(mfe)
        mae_all.append(mae)
        if r.get("win") is True:
            mfe_win.append(mfe)
            mae_win.append(mae)
        elif r.get("win") is False:
            mfe_loss.append(mfe)
            mae_loss.append(mae)
    if not mfe_all:
        return None

    def _mean(xs):
        return sum(xs) / len(xs) if xs else None

    return {
        "n": len(mfe_all), "avg_mfe": _mean(mfe_all), "avg_mae": _mean(mae_all),
        "avg_mfe_win": _mean(mfe_win), "avg_mae_win": _mean(mae_win),
        "avg_mfe_loss": _mean(mfe_loss), "avg_mae_loss": _mean(mae_loss),
        # 平均 MFE/MAE：>1 说明有利波动幅度大于不利，趋势持仓体验更好
        "mfe_mae_ratio": (_mean(mfe_all) / _mean(mae_all))
                          if _mean(mae_all) and _mean(mae_all) > 1e-15 else None,
    }


# =========================== 一站式 tear sheet ===========================

def tear_sheet(returns, dates=None, bars_per_year=DEFAULT_BARS_PER_YEAR,
               var_alpha=DEFAULT_VAR_ALPHA, rolling_window=DEFAULT_ROLLING_WINDOW):
    """周期收益序列 -> 完整绩效字典（任一子指标样本不足则该键为 None，不抛异常）。

    返回固定键集合，调用方可用 .get 安全渲染；rolling 为等长列表（暖机期 None）。"""
    rs = clean_returns(returns)
    n = len(rs)
    out = {
        "n": n if rs else 0,
        "cumulative": cumulative_return(rs),
        "mean": mean_return(rs),
        "annualized": annualized_return(rs, bars_per_year) if rs else None,
        "cagr": cagr(rs, bars_per_year) if rs else None,
        "volatility": volatility(rs, bars_per_year),
        "sharpe": sharpe_ratio(rs, bars_per_year),
        "sortino": sortino_ratio(rs, bars_per_year),
        "calmar": calmar_ratio(rs, bars_per_year),
        "omega": omega_ratio(rs),
        "ulcer": ulcer_index(rs),
        "max_drawdown": max_drawdown(rs),
        "var": value_at_risk(rs, var_alpha),
        "cvar": conditional_var(rs, var_alpha),
        "var_alpha": var_alpha,
        "drawdown": drawdown_series(rs),
        "rolling_sharpe": rolling_sharpe(rs, rolling_window, bars_per_year),
        "monthly": monthly_returns(rs, dates) if dates is not None else None,
    }
    return out


# =========================== 合成自检（零网络，手算可复核） ===========================

def _approx(a, b, tol=1e-9):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def selftest():
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))
        if not cond:
            raise AssertionError("FAIL: " + name)

    # —— 手算序列：rs = [+1%, -2%, +3%, -1%, +2%]（n=5）——
    rs = [0.01, -0.02, 0.03, -0.01, 0.02]
    ck("均值=0.006", _approx(mean_return(rs), 0.006))
    # 1.01*0.98*1.03*0.99*1.02 = 1.0294850412
    ck("累计收益", _approx(cumulative_return(rs), 0.0294850412, 1e-9))
    ck("算术年化=均值*243", _approx(annualized_return(rs, 243), 0.006 * 243))
    # 下行偏差（全样本分母）：亏损 -0.02/-0.01 -> (0.0004+0.0001)/5=0.0001 -> sqrt=0.01
    ck("下行偏差=0.01", _approx(downside_deviation(rs), 0.01))
    # Sortino = 0.006/0.01*sqrt(243)
    ck("索提诺", _approx(sortino_ratio(rs, 243), 0.6 * math.sqrt(243), 1e-9))
    # 样本 stdev：离差平方和 0.00172 /(n-1)=0.00043
    sd = math.sqrt(0.00043)
    ck("夏普", _approx(sharpe_ratio(rs, 243), 0.006 / sd * math.sqrt(243), 1e-9))
    # Omega：盈利和 0.06 / 亏损和 0.03 = 2
    ck("Omega=2", _approx(omega_ratio(rs), 2.0))
    # 回撤序列 [0, 0.02, 0, 0.01, 0]，maxDD=0.02，Ulcer=sqrt((.0004+.0001)/5)=0.01
    _dd = drawdown_series(rs)
    ck("回撤序列", len(_dd) == 5
       and all(_approx(a, b, 1e-12) for a, b in zip(_dd, [0.0, 0.02, 0.0, 0.01, 0.0])))
    ck("最大回撤=0.02", _approx(max_drawdown(rs), 0.02))
    ck("Ulcer=0.01", _approx(ulcer_index(rs), 0.01))
    # ppy=5 时 CAGR=累计=0.0294850412，Calmar=/0.02
    ck("Calmar", _approx(calmar_ratio(rs, 5), 0.0294850412 / 0.02, 1e-9))
    # VaR5%：排序 [-2,-1,1,2,3]%，pos=0.2 -> -0.02*0.8+-0.01*0.2=-0.018；CVaR 取 <=-0.018 即 -0.02
    ck("VaR5%=-0.018", _approx(value_at_risk(rs, 0.05), -0.018))
    ck("CVaR5%=-0.02", _approx(conditional_var(rs, 0.05), -0.02))

    # —— 滚动夏普：window=3，前 2 个为 None，长度恒等 ——
    roll = rolling_sharpe(rs, 3, 243)
    ck("滚动长度", len(roll) == 5 and roll[0] is None and roll[1] is None
       and roll[2] is not None)
    seg = rs[:3]
    expect = (sum(seg) / 3) / statistics.stdev(seg) * math.sqrt(243)
    ck("滚动首值", _approx(roll[2], expect, 1e-9))
    ck("窗口过长全None", rolling_sharpe(rs, 99) == [None] * 5)

    # —— 权益转收益 / 日度收敛 ——
    ck("权益转收益", _approx(returns_from_equity([1.0, 1.01, 1.03])[1],
                              1.03 / 1.01 - 1.0))
    dts = ["2026-01-05 09:00", "2026-01-05 15:00", "2026-01-06 09:00"]
    days, deq = daily_last_equity(dts, [100.0, 101.0, 103.0])
    ck("日度取最后点", days == ["2026-01-05", "2026-01-06"] and deq == [101.0, 103.0])

    # —— 月度矩阵：1月 1.01*0.99=0.9999；2月 1.02*0.98=0.9996 ——
    mr = monthly_returns([0.01, -0.01, 0.02, -0.02],
                         ["2026-01-05", "2026-01-12", "2026-02-03", "2026-02-10"])
    ck("月度矩阵", _approx(mr["matrix"][2026][1], -0.0001, 1e-12)
       and _approx(mr["matrix"][2026][2], -0.0004, 1e-12)
       and mr["years"] == [2026] and len(mr["cells"]) == 2)

    # —— 逐笔交易：[100,-50,200,-80,-30,120] ——
    trades = [100.0, -50.0, 200.0, -80.0, -30.0, 120.0]
    ts = trade_stats(trades)
    ck("笔数/胜率", ts["n"] == 6 and _approx(ts["win_rate"], 0.5))
    ck("总盈/总亏", _approx(ts["gross_profit"], 420.0) and _approx(ts["gross_loss"], -160.0))
    ck("profit_factor=2.625", _approx(ts["profit_factor"], 2.625))
    ck("平均盈=140/平均亏=-53.333", _approx(ts["avg_win"], 140.0)
       and _approx(ts["avg_loss"], -160.0 / 3))
    # 胜负序列 W L W L L W -> 最大连胜1、最大连亏2
    ck("连胜连亏", ts["max_win_streak"] == 1 and ts["max_loss_streak"] == 2)
    ck("best/worst", ts["best"] == 200.0 and ts["worst"] == -80.0)
    ck("无亏损PF=None", trade_stats([1.0, 2.0])["profit_factor"] is None)

    # —— MFE/MAE：多单 entry100，路径 102/99/103/101 -> MFE3% MAE1% ——
    mfe, mae = excursion(1, 100.0, [102, 99, 103, 101])
    ck("多单MFE/MAE", _approx(mfe, 0.03) and _approx(mae, 0.01))
    # 空单 entry100，路径 98/101/97 -> MFE3% MAE1%
    mfe2, mae2 = excursion(-1, 100.0, [98, 101, 97])
    ck("空单MFE/MAE", _approx(mfe2, 0.03) and _approx(mae2, 0.01))
    ms = mae_mfe_summary([{"mfe": 0.03, "mae": 0.01, "win": True},
                          {"mfe": 0.02, "mae": 0.04, "win": False}])
    ck("MFE/MAE汇总", ms["n"] == 2 and _approx(ms["avg_mfe"], 0.025)
       and _approx(ms["avg_mae_win"], 0.01) and _approx(ms["avg_mfe_loss"], 0.02))

    # —— 样本不足安全：全部返回 None / 空结构，绝不抛 ——
    ck("空序列安全", cumulative_return([]) is None and sharpe_ratio([]) is None
       and max_drawdown([]) is None and calmar_ratio([]) is None
       and omega_ratio([]) is None and ulcer_index([]) is None
       and value_at_risk([0.01]) is None and trade_stats([]) is None
       and mae_mfe_summary([]) is None and monthly_returns([], []) is None)
    ck("脏值过滤", _approx(mean_return([0.01, None, "x", float("nan"), 0.03]), 0.02))
    # 常量收益（零波动）：夏普/索提诺给 0 而非除零，Calmar 无回撤给 None
    ck("零波动", sharpe_ratio([0.01, 0.01, 0.01]) == 0.0
       and sortino_ratio([0.01, 0.01, 0.01]) == 0.0
       and calmar_ratio([0.01, 0.01, 0.01]) is None)

    # —— tear_sheet 一键聚合，键齐全、子项独立降级 ——
    sheet = tear_sheet(rs, dates=["2026-01-%02d" % (i + 1) for i in range(5)])
    for key in ("n", "cumulative", "sharpe", "sortino", "calmar", "omega", "ulcer",
                "max_drawdown", "var", "cvar", "drawdown", "rolling_sharpe", "monthly"):
        ck("tear含" + key, key in sheet)
    ck("tear drawdown等长", len(sheet["drawdown"]) == 5)
    empty_sheet = tear_sheet([])
    ck("空tear不抛", empty_sheet["n"] == 0 and empty_sheet["sharpe"] is None)

    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description="G3 绩效指标包自检")
    parser.add_argument("--selftest", action="store_true", help="运行手算合成断言")
    args = parser.parse_args(argv)
    if args.selftest or True:
        checks = selftest()
        print("metrics.selftest: %d 项断言全部通过" % len(checks))
        for name, ok in checks:
            print("  [PASS] %s" % name)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
