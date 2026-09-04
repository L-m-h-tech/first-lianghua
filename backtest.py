# -*- coding: utf-8 -*-
"""最小可行日线回测（P1第三/四批，零新增第三方依赖）。

用途：
1. 用新浪主连日线回放“日线动量 + RSI/MACD/KDJ多周期共振”的纯技术信号；
2. 统计 1/5/20 日信号结果，以及固定持有/反向退出的非重叠交易表现；
3. 默认读取data/futures_fees.csv的真实券商手续费（按金额+按手数），另扣滑点，过滤疑似锁涨跌停入场与离场，并输出3×3参数稳定性扫描；
4. 输出 reports/backtest_report.txt、backtest_signals.csv、backtest_trades.csv。

边界（不伪装成全量回测引擎）：
- 历史新闻、机构观点、实时量仓流、浏览器页面 IV、30/60分钟K线无法逐日复原，本回测只验证日线技术面；
- 新浪主连在换月时可能跳空。这里按“疑似换月日收益置0 + 比例后复权”处理，避免把换月价差算成盈亏；
- 真实手续费来自用户2026-08-28券商费率表转换的data/futures_fees.csv；日线持仓多日，按“开仓+平仓”计费，不使用平今费率；
- 逐笔盘口、固定金额手续费随政策调整、平今优惠触发条件仍未完整建模；滑点仍为统一比例近似；
- 涨跌停过滤只根据“收盘贴近最高/最低 + 涨跌幅阈值”识别疑似锁板，实盘逐笔成交仍需交易所盘口确认。

运行示例：
  D:\\Python\\python.exe backtest.py --codes RB0,MA0 --days 250
  D:\\Python\\python.exe backtest.py --codes RB0,MA0 --no-cost --no-limit-filter
  D:\\Python\\python.exe backtest.py --all --days 250 --hold 10
"""
import argparse
import csv
import json
import math
import os
import random
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import config
import futures_data
import metrics
import backtest_rigor as br


def _pct(x):
    return "--" if x is None else f"{x * 100:.2f}%"


_FEE_CACHE = {}


def load_fee_schedule(path=None, force=False):
    """读取标准库CSV手续费表；返回 {sym: row}，文件缺失时返回空表。"""
    path = os.path.abspath(path or config.FUTURES_FEES_FILE)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    cached = _FEE_CACHE.get(path)
    if not force and cached and cached["mtime"] == mtime:
        return cached["rows"]
    rows = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            sym = (r.get("sym") or "").strip().upper()
            if not sym:
                continue
            for k in ("multiplier", "open_amt_rate", "open_per_lot", "close_amt_rate",
                      "close_per_lot", "today_amt_rate", "today_per_lot"):
                r[k] = float(r.get(k) or 0.0)
            rows[sym] = r
    _FEE_CACHE[path] = {"mtime": mtime, "rows": rows}
    return rows


def side_fee(fee_row, price, leg):
    """单笔单边手续费：返回(占合约名义价值比例, 每手人民币金额)。leg=open/close/today。"""
    if not fee_row or price <= 0:
        return 0.0, 0.0
    mult = fee_row["multiplier"]
    if mult <= 0:
        return 0.0, 0.0
    if leg == "open":
        amt_rate, per_lot = fee_row["open_amt_rate"], fee_row["open_per_lot"]
    elif leg == "today":
        amt_rate, per_lot = fee_row["today_amt_rate"], fee_row["today_per_lot"]
    else:
        amt_rate, per_lot = fee_row["close_amt_rate"], fee_row["close_per_lot"]
    # 券商表中“按金额”和“按手数”可能同时存在：金额费=名义价值×费率，再叠加固定元/手。
    notional = price * mult
    yuan = notional * amt_rate + per_lot
    return yuan / notional if notional > 0 else 0.0, yuan


def ratio_adjusted_bars(bars):
    """主连比例后复权：疑似换月跳空日收益置0，其余日收益原样链接。"""
    bars = [dict(b) for b in bars if futures_data._f(b.get("c")) > 0]
    if len(bars) < 20:
        return bars, 0
    closes = [futures_data._f(b["c"]) for b in bars]
    raw_rets = [0.0]
    for i in range(1, len(closes)):
        raw_rets.append(closes[i] / closes[i - 1] - 1.0 if closes[i - 1] > 0 else 0.0)
    abs_rets = sorted(abs(r) for r in raw_rets[1:] if math.isfinite(r))
    mad = statistics.median(abs_rets) if abs_rets else 0.0
    threshold = max(config.BACKTEST_ROLL_GAP_ABS, mad * config.BACKTEST_ROLL_GAP_MAD)
    factor = 1.0
    roll_count = 0
    out = []
    for i, b in enumerate(bars):
        if i > 0 and abs(raw_rets[i]) > threshold:
            used_ret = 0.0
            roll_count += 1
        else:
            used_ret = raw_rets[i]
        if i > 0 and math.isfinite(raw_rets[i]) and raw_rets[i] > -0.999999:
            factor *= (1.0 + used_ret) / (1.0 + raw_rets[i])
        nb = dict(b)
        for k in ("o", "h", "l", "c"):
            nb[k] = futures_data._f(b.get(k)) * factor
        out.append(nb)
    return out, roll_count


def technical_score(ind):
    """与 analyzer.py 中技术面一致：日线动量 + 多周期共振。"""
    close = ind.get("close") or 0.0
    ma10 = ind.get("ma10") or 0.0
    score = (math.tanh(ind.get("ret5", 0.0) * 160.0) * 2.5 +
             math.tanh(ind.get("ret20", 0.0) * 70.0) * 2.0)
    if ma10 > 0:
        score += math.tanh((close / ma10 - 1.0) * 220.0) * 1.0
    tech = ind.get("tech") or {}
    score += float(tech.get("resonance_score") or 0.0)
    return score


def score_band(score):
    a = abs(score)
    if a < config.SCORE_NEUTRAL:
        return "观望"
    if a < config.SCORE_LIGHT:
        return "轻仓"
    if a < config.SCORE_MID:
        return "分批"
    return "强信号"


def metrics_from_returns(returns, hold_days):
    rets = [r for r in returns if math.isfinite(r)]
    if not rets:
        return None
    n = len(rets)
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    avg = sum(rets) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    win_rate = len(wins) / n
    pl_ratio = avg_win / abs(avg_loss) if avg_loss < 0 else None
    equity, peak, max_dd = 1.0, 1.0, 0.0
    for r in rets:
        equity *= 1.0 + r
        peak = max(peak, equity)
        max_dd = max(max_dd, 1.0 - equity / peak)
    cumulative = equity - 1.0
    if n >= 2:
        std = statistics.stdev(rets)
        sharpe = (avg / std * math.sqrt(252.0 / hold_days)) if std > 1e-12 else 0.0
    else:
        sharpe = 0.0
    annualized = avg * 252.0 / hold_days
    return {"n": n, "win_rate": win_rate, "avg": avg, "avg_win": avg_win,
            "avg_loss": avg_loss, "pl_ratio": pl_ratio, "cumulative": cumulative,
            "max_dd": max_dd, "annualized": annualized, "sharpe": sharpe}


def quantile_inplace(sorted_vals, q):
    """线性插值分位数（同 numpy linear / R type7 口径），输入须已升序。"""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    q = min(1.0, max(0.0, q))
    pos = q * (len(sorted_vals) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] * (hi - pos) + sorted_vals[hi] * (pos - lo)


def _equity_cum_dd(seq):
    """逐期复利累计收益与路径最大回撤。"""
    equity, peak, max_dd = 1.0, 1.0, 0.0
    for r in seq:
        equity *= 1.0 + r
        peak = max(peak, equity)
        max_dd = max(max_dd, 1.0 - equity / peak)
    return equity - 1.0, max_dd


def bootstrap_trade_stats(returns, n_boot=1000, seed=20260902, ci=(0.05, 0.95),
                          min_trades=20):
    """交易级 iid bootstrap：对逐笔净收益有放回重采样 n_boot 次，给累计收益/
    最大回撤的分位区间（固定种子、逐值可复现）。样本不足或关闭(n_boot=0)返回 None。
    注意 iid 假设交易近似独立，收益序列自相关强时区间偏乐观，报告中已声明。"""
    rets = [r for r in returns if math.isfinite(r)]
    n = len(rets)
    if not n_boot or n < min_trades:
        return None
    rng = random.Random(seed)
    cums, dds = [], []
    for _ in range(int(n_boot)):
        sample = (rets[rng.randrange(n)] for _ in range(n))
        cum, mdd = _equity_cum_dd(sample)
        cums.append(cum)
        dds.append(mdd)
    cums.sort()
    dds.sort()
    lo, hi = ci
    return {"n": n, "n_boot": int(n_boot),
            "cum_p5": quantile_inplace(cums, lo), "cum_median": quantile_inplace(cums, 0.5),
            "cum_p95": quantile_inplace(cums, hi),
            "dd_p5": quantile_inplace(dds, lo), "dd_median": quantile_inplace(dds, 0.5),
            "dd_p95": quantile_inplace(dds, hi)}


def split_is_oos(trades, oos_ratio):
    """按平仓日期(缺失回退入场日)升序，切前 (1-r) 为样本内IS、后 r 为样本外OOS。
    r<=0 或 >=1 时不切分（IS=全部、OOS空）。"""
    if not oos_ratio or oos_ratio <= 0.0 or oos_ratio >= 1.0:
        return list(trades), []
    ordered = sorted(trades,
                     key=lambda t: (t.get("exit_date") or t.get("entry_date") or ""))
    cut = int(len(ordered) * (1.0 - float(oos_ratio)))
    return ordered[:cut], ordered[cut:]


def percentile_at_or_below(values, x):
    """历史 values 中 <= x 的占比（0~1，含本次）；无有效样本返回 None。"""
    vals = [v for v in values if v is not None and math.isfinite(v)]
    if not vals:
        return None
    return sum(1 for v in vals if v <= x + 1e-15) / len(vals)


def load_validation_sidecar(path=None):
    """读取 tools/backtest_validation.py 产出的 DSR/PBO sidecar(JSON)；
    文件缺失/损坏/非对象一律返回 None（软降级，绝不拖垮回测）。"""
    path = path or config.BACKTEST_VALIDATION_JSON
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def prepare_symbol(raw_bars):
    """只计算一次指标序列，参数稳定性扫描复用，避免重复计算。"""
    bars, roll_count = ratio_adjusted_bars(raw_bars)
    if len(bars) < 65:
        return None
    closes = [futures_data._f(b["c"]) for b in bars]
    opens = [futures_data._f(b.get("o")) for b in bars]
    highs = [futures_data._f(b["h"]) for b in bars]
    lows = [futures_data._f(b["l"]) for b in bars]
    series = []
    for i in range(60, len(closes)):
        ind = futures_data.compute_indicators(bars[:i + 1])
        series.append({"i": i, "ind": ind, "score": technical_score(ind)})
    return {"name": "", "code": "", "bars": bars, "closes": closes,
            "opens": opens, "highs": highs, "lows": lows, "series": series,
            "roll_count": roll_count}


def _direction_from_score(score, entry_score):
    return 1 if score >= entry_score else (-1 if score <= -entry_score else 0)


def _locked_limit(prepared, i, trade_direction, limit_move):
    """trade_direction=1 买入/平空被涨停锁住；-1 卖出/平多被跌停锁住。"""
    if i <= 0 or limit_move is None or limit_move >= 1:
        return False
    closes, highs, lows = prepared["closes"], prepared["highs"], prepared["lows"]
    prev, cur = closes[i - 1], closes[i]
    if prev <= 0:
        return False
    ret = cur / prev - 1.0
    eps = max(cur * 1e-5, 1e-9)
    if trade_direction > 0:
        return ret >= limit_move and abs(cur - highs[i]) <= eps
    if trade_direction < 0:
        return ret <= -limit_move and abs(cur - lows[i]) <= eps
    return False


def _build_trade(name, code, prepared, position, i, exit_reason, fee_rate, slip_rate,
                 fee_table, use_real_fees, impact_rate=0.0, exit_price=None,
                 entry_i=None, exit_date=None):
    closes = prepared["closes"]
    if exit_price is None or exit_price <= 0:
        exit_price = closes[i]
    if entry_i is None:
        entry_i = position["entry_i"]
    gross = position["direction"] * (exit_price / position["entry_price"] - 1.0)
    sym = str(prepared.get("sym") or "").strip().upper()
    if use_real_fees and sym in fee_table:
        exit_fee_rate, exit_fee_yuan = side_fee(fee_table[sym], exit_price, "close")
        fee_mode = "真实费率表"
    else:
        exit_fee_rate, exit_fee_yuan = fee_rate, 0.0
        fee_mode = "兜底比例费率"
    fee_cost = position.get("entry_fee_rate", fee_rate) + exit_fee_rate
    slip_cost = 2.0 * slip_rate
    impact_cost = 2.0 * impact_rate
    cost = fee_cost + slip_cost + impact_cost
    return {
        "symbol": name, "code": code,
        "entry_date": position["entry_date"],
        "exit_date": exit_date or prepared["bars"][i].get("d", ""),
        "direction": "多" if position["direction"] > 0 else "空",
        "entry_score": position["score"], "hold": i - entry_i,
        "gross_ret": gross, "fee_cost": fee_cost, "slip_cost": slip_cost,
        "impact_cost": impact_cost, "cost": cost, "ret": gross - cost,
        "fee_open_yuan": position.get("entry_fee_yuan", 0.0),
        "fee_close_yuan": exit_fee_yuan,
        "fee_round_yuan": position.get("entry_fee_yuan", 0.0) + exit_fee_yuan,
        "multiplier": fee_table.get(sym, {}).get("multiplier", 0) if use_real_fees else 0,
        "fee_mode": fee_mode,
        "blocked_exits": position.get("blocked_exits", 0), "exit": exit_reason}


def simulate_prepared(name, code, prepared, hold_days, entry_score,
                      fee_rate=0.0, slip_rate=0.0, limit_move=None,
                      collect_signals=False, fee_table=None, use_real_fees=True,
                      fill_mode="close", impact_rate=0.0):
    """成交时点 fill_mode：
    - close（默认，旧口径）：信号根 i 收盘确认并以 closes[i] 成交；
    - next_open（G4 保守对照）：信号根 i 收盘决策、次根 i+1 以 opens[i+1] 成交，
      次根跳空锁板则顺延，末根仍持仓按末根收盘平、末根才出的入场信号无次根可成交则丢弃。
    冲击成本 impact_rate 为单边比例，与手续费/滑点分开列示、往返计两次。
    """
    closes = prepared["closes"]
    opens = prepared.get("opens") or [0.0] * len(closes)
    bars = prepared["bars"]
    signal_rows, trades = [], []
    position = None
    pending_entry = None   # (direction, score)：上一根挂出、本根开盘待成交的开仓单
    pending_exit = None    # exit_reason：上一根挂出、本根开盘待成交的离场单
    blocked_entry = blocked_exit = 0
    unfilled_entry = 0     # next_open：样本末尾挂单无次根可成交的信号数（不虚构）
    fee_table = fee_table or {}
    fallback_cost_round = 2.0 * (fee_rate + slip_rate + impact_rate)

    def _open_position(direction, score, fill_i, fill_price):
        entry_sym = str(prepared.get("sym") or "").strip().upper()
        if use_real_fees and entry_sym in fee_table:
            efr, efy = side_fee(fee_table[entry_sym], fill_price, "open")
        else:
            efr, efy = fee_rate, 0.0
        return {"entry_i": fill_i, "direction": direction, "score": score,
                "entry_price": fill_price, "entry_date": bars[fill_i].get("d", ""),
                "entry_fee_rate": efr, "entry_fee_yuan": efy, "blocked_exits": 0}

    for item in prepared["series"]:
        i = item["i"]
        score = item["score"]
        direction = _direction_from_score(score, entry_score)

        if direction and collect_signals:
            row = {"symbol": name, "code": code, "date": bars[i].get("d", ""),
                   "score": score, "band": score_band(score),
                   "direction": "多" if direction > 0 else "空"}
            for horizon in (1, 5, 20):
                if i + horizon < len(closes):
                    row[f"h{horizon}"] = direction * (closes[i + horizon] / closes[i] - 1.0)
                else:
                    row[f"h{horizon}"] = None
            signal_rows.append(row)

        # ---- next_open 阶段A：本根开盘成交上一根挂单（先平后开，支持反手）----
        if fill_mode == "next_open":
            if position is not None and pending_exit is not None:
                exit_dir = -position["direction"]
                px = opens[i]
                if px <= 0 or _locked_limit(prepared, i, exit_dir, limit_move):
                    blocked_exit += 1
                    position["blocked_exits"] = position.get("blocked_exits", 0) + 1
                else:
                    trades.append(_build_trade(name, code, prepared, position, i, pending_exit,
                                              fee_rate, slip_rate, fee_table, use_real_fees,
                                              impact_rate, exit_price=px,
                                              entry_i=position["entry_i"],
                                              exit_date=bars[i].get("d", "")))
                    position = None
                pending_exit = None
            if position is None and pending_entry is not None:
                d_new, s_new = pending_entry
                px = opens[i]
                if px <= 0 or _locked_limit(prepared, i, d_new, limit_move):
                    blocked_entry += 1
                else:
                    position = _open_position(d_new, s_new, i, px)
                pending_entry = None

        # ---- 阶段C：本根收盘决策 ----
        if position is not None:
            opposite = direction == -position["direction"] and direction != 0
            held = i - position["entry_i"]
            if fill_mode == "close":
                if position.get("exit_requested_i") is not None:
                    should_exit = True
                elif held >= hold_days or opposite:
                    position["exit_requested_i"] = i
                    should_exit = True
                else:
                    should_exit = False
                if should_exit:
                    # 平多要卖出(-1)，平空要买回(+1)；锁板时顺延到下一交易日。
                    exit_dir = -position["direction"]
                    if _locked_limit(prepared, i, exit_dir, limit_move):
                        blocked_exit += 1
                    else:
                        exit_reason = "反向" if opposite else ("到期" if held >= hold_days else "解锁离场")
                        trades.append(_build_trade(name, code, prepared, position, i, exit_reason,
                                                  fee_rate, slip_rate, fee_table, use_real_fees,
                                                  impact_rate))
                        position = None
            else:  # next_open：只挂离场单，次根开盘成交；锁板则次根决策时重新挂=顺延
                if held >= hold_days or opposite:
                    pending_exit = "反向" if opposite else "到期"

        if fill_mode == "close":
            if position is None and direction:
                if _locked_limit(prepared, i, direction, limit_move):
                    blocked_entry += 1
                else:
                    position = _open_position(direction, score, i, closes[i])
        else:
            # next_open：无仓直接挂开仓单；持仓但本根已挂离场(反手)时，挂反向开仓单次根先平后开
            will_reverse = (position is not None and pending_exit is not None
                            and direction == -position["direction"])
            if pending_entry is None and direction and (position is None or will_reverse):
                pending_entry = (direction, score)

    if position is not None:
        # 末根仍持仓：按末根收盘价平（next_open 下末根才挂的离场单无次根，同样回落末根收盘）
        i = len(closes) - 1
        reason = pending_exit or "样本末"
        trades.append(_build_trade(name, code, prepared, position, i, reason,
                                  fee_rate, slip_rate, fee_table, use_real_fees, impact_rate))
    if fill_mode == "next_open" and pending_entry is not None:
        unfilled_entry += 1   # 诚实记录：最后一根的入场信号没有次根可成交

    horizon_rows = []
    if collect_signals:
        for h in (1, 5, 20):
            vals = [r[f"h{h}"] for r in signal_rows if r.get(f"h{h}") is not None]
            horizon_rows.append((h, metrics_from_returns(vals, h)))
    trade_metrics = metrics_from_returns([t["ret"] for t in trades], hold_days)
    gross_metrics = metrics_from_returns([t["gross_ret"] for t in trades], hold_days)
    sample_cost = trades[0]["cost"] if trades else fallback_cost_round
    return {"name": name, "code": code, "bars": len(prepared["bars"]),
            "roll_count": prepared["roll_count"], "signals": signal_rows,
            "horizons": horizon_rows, "trades": trades, "trade_metrics": trade_metrics,
            "gross_metrics": gross_metrics, "blocked_entry": blocked_entry,
            "blocked_exit": blocked_exit, "unfilled_entry": unfilled_entry,
            "fill_mode": fill_mode, "cost_round": sample_cost,
            "real_fee_trades": sum(1 for t in trades if t["fee_mode"] == "真实费率表"),
            "last_signal": (signal_rows[-1] if signal_rows else None)}


def simulate_symbol(name, code, raw_bars, hold_days, entry_score,
                    fee_rate=0.0, slip_rate=0.0, limit_move=None,
                    fee_table=None, use_real_fees=True, fill_mode="close",
                    impact_rate=0.0):
    prepared = prepare_symbol(raw_bars)
    if prepared is None:
        return None
    prepared["name"], prepared["code"] = name, code
    prepared["sym"] = code.rstrip("0").upper()
    return simulate_prepared(name, code, prepared, hold_days, entry_score,
                             fee_rate, slip_rate, limit_move, collect_signals=True,
                             fee_table=fee_table, use_real_fees=use_real_fees,
                             fill_mode=fill_mode, impact_rate=impact_rate)


def resolve_codes(codes_arg, limit=None):
    if codes_arg:
        items = []
        for x in codes_arg.split(","):
            x = x.strip().upper()
            if not x:
                continue
            if x in config.VARIETIES:
                meta = config.VARIETIES[x]
                items.append((x, meta["code"]))  # meta["code"]已是主连RB0，勿再补0（新浪日K对RB00返回null）
            else:
                tok = x if not x.isalpha() else x + "0"  # 纯品种字母→主连补0；具体合约(如RB2601)原样
                items.append((tok, tok))
        return items
    items = [(name, meta["code"]) for name, meta in config.VARIETIES.items()]
    return items[:limit] if limit else items


def fetch_and_run(item, args):
    name, code = item
    try:
        bars = futures_data.fetch_daily_kline(code, args.days)[-args.days:]
        prepared = prepare_symbol(bars)
        if prepared is None:
            return name, None, f"K线不足: {len(bars)}根"
        prepared["name"], prepared["code"] = name, code
        prepared["sym"] = code.rstrip("0").upper()
        fee_table = load_fee_schedule(args.fees_file)
        result = simulate_prepared(name, code, prepared, args.hold, args.entry,
                                   args.fee_rate, args.slip_rate,
                                   None if args.no_limit_filter else args.limit_move,
                                   collect_signals=True, fee_table=fee_table,
                                   use_real_fees=not args.no_real_fees,
                                   fill_mode=args.fill, impact_rate=args.impact_rate)
        if not args.no_stable:
            stability = []
            for hold in config.BACKTEST_STABLE_HOLDS:
                for entry in config.BACKTEST_STABLE_ENTRIES:
                    rr = simulate_prepared(name, code, prepared, hold, entry,
                                           args.fee_rate, args.slip_rate,
                                           None if args.no_limit_filter else args.limit_move,
                                           collect_signals=False, fee_table=fee_table,
                                           use_real_fees=not args.no_real_fees,
                                           fill_mode=args.fill,
                                           impact_rate=args.impact_rate)
                    stability.append({"hold": hold, "entry": entry,
                                      "metrics": rr["trade_metrics"],
                                      "blocked_entry": rr["blocked_entry"],
                                      "blocked_exit": rr["blocked_exit"]})
            result["stability"] = stability
        else:
            result["stability"] = []
        # G4续：对照基准——同区间一直买入持有主连（主连已比例复权），供报告算超额
        result["buy_hold"] = br.benchmark_for_prepared(prepared)
        # G4续：滚动 walk-forward（默认关，--walk-forward 开）。每折只在前段IS选参、用于后段OOS。
        if getattr(args, "walk_forward", False):
            grid = [(h, e) for h in config.BACKTEST_STABLE_HOLDS
                    for e in config.BACKTEST_STABLE_ENTRIES]
            limit = None if args.no_limit_filter else args.limit_move

            def _sim(sub, hold, entry):
                return simulate_prepared(name, code, sub, hold, entry,
                                         args.fee_rate, args.slip_rate, limit,
                                         collect_signals=False, fee_table=fee_table,
                                         use_real_fees=not args.no_real_fees,
                                         fill_mode=args.fill, impact_rate=args.impact_rate)

            result["wf"] = br.walk_forward_symbol(
                prepared, _sim, grid, (args.hold, args.entry),
                train_bars=args.wf_train, test_bars=args.wf_test,
                min_is_trades=config.BACKTEST_WF_MIN_IS_TRADES,
                warmup=config.BACKTEST_WARMUP_BARS)
        else:
            result["wf"] = None
        return name, result, ""
    except Exception as e:
        return name, None, f"{type(e).__name__}: {e}"


def _fmt_boot_ci(b):
    if not b:
        return "样本不足（净交易<%d笔或已关闭），不做区间估计" % config.BACKTEST_BOOTSTRAP_MIN_TRADES
    return (f"累计收益 {b['cum_p5']*100:+.1f}%~{b['cum_p95']*100:+.1f}%"
            f"（中位 {b['cum_median']*100:+.1f}%）；最大回撤 {b['dd_p5']*100:.1f}%~"
            f"{b['dd_p95']*100:.1f}%（中位 {b['dd_median']*100:.1f}%）；{b['n_boot']}次固定种子重采样")


def _fmt_validation_ref(data):
    """把 backtest_validation 的 DSR/PBO sidecar 压成一两句交叉引用；无内容返回空串。"""
    if not isinstance(data, dict):
        return ""
    parts = []
    d = data.get("dsr")
    if isinstance(d, dict) and d.get("dsr") is not None:
        parts.append(f"组合日收益 DSR={d['dsr']:.2f}（观察SR {d.get('sr_obs', 0):.2f}，"
                     f"多重试验阈值SR0 {d.get('sr0', 0):.2f}，{d.get('verdict', '')}）")
    g = data.get("grid")
    if isinstance(g, dict) and g.get("n"):
        parts.append(f"分钟参数网格 {g['n']} 品种：PBO<0.2 共{g.get('pbo_good', 0)}、"
                     f"全网格全样本亏损 {g.get('all_loss', 0)}、WF样本外Sharpe为正 {g.get('oos_pos', 0)}")
    return "；".join(p for p in parts if p)


def _fmt_archive_info(info):
    if not info:
        return ""
    pct = info.get("percentile")
    pct_txt = "--" if pct is None else f"{pct*100:.0f}%"
    return (f"回测留档：本次为 backtest_runs 表第 {info['seq']}/{info['total']} 条日线回测记录，"
            f"累计收益好于历史 {pct_txt} 的运行（纵向对比、防'挑一次最好的'）")


def _fmt_metrics(m):
    if not m:
        return "样本不足"
    pl = "--" if m["pl_ratio"] is None else f"{m['pl_ratio']:.2f}"
    return (f"n={m['n']} 胜率{m['win_rate']*100:.1f}% 均收{m['avg']*100:+.2f}% "
            f"盈/亏{pl} 累计{m['cumulative']*100:+.1f}% 年化{m['annualized']*100:+.1f}% "
            f"最大回撤{m['max_dd']*100:.1f}% 夏普{m['sharpe']:.2f}")


def _fmt_g3_extended(returns, hold_days):
    """G3：在旧指标之外补一行交易级扩展绩效（profit factor/连胜连亏/Omega/Ulcer/Calmar）。
    逐笔非重叠交易收益视为离散观测；年化按 252/hold 与既有夏普同口径，样本不足给'-'。"""
    ts = metrics.trade_stats(returns)
    if not ts:
        return "  G3扩展绩效: 样本不足"
    ppy = 252.0 / hold_days if hold_days else 252.0
    omega = metrics.omega_ratio(returns)
    ulcer = metrics.ulcer_index(returns)
    calmar = metrics.calmar_ratio(returns, ppy)
    def _s(v, fmt="{:.2f}"):
        return "-" if v is None else fmt.format(v)
    return ("  G3扩展绩效: 盈亏因子PF {pf}  盈亏比 {pr}  最大连胜 {ws}笔/连亏 {ls}笔  "
            "Omega {om}  Ulcer {ul}  Calmar {cal}").format(
        pf=_s(ts["profit_factor"]), pr=_s(ts["payoff_ratio"]),
        ws=ts["max_win_streak"], ls=ts["max_loss_streak"],
        om=_s(omega), ul=_s(ulcer, "{:.3f}"), cal=_s(calmar))


def _fmt_overlap_metrics(m):
    """重叠信号只做横截面统计，不做复利净值曲线。"""
    if not m:
        return "样本不足"
    pl = "--" if m["pl_ratio"] is None else f"{m['pl_ratio']:.2f}"
    return (f"n={m['n']} 胜率{m['win_rate']*100:.1f}% 均收{m['avg']*100:+.2f}% "
            f"平均盈{m['avg_win']*100:+.2f}%/平均亏{m['avg_loss']*100:+.2f}% 盈/亏{pl} "
            f"年化均值{m['annualized']*100:+.1f}% 夏普{m['sharpe']:.2f}（重叠样本，不计累计/回撤/成本）")


def _aggregate_stability(results):
    """参数网格汇总：指标按各品种交易数加权；回撤取跨品种最差值。"""
    grid = {}
    for r in results:
        for item in r.get("stability", []):
            key = (item["hold"], item["entry"])
            slot = grid.setdefault(key, {"rows": [], "blocked_entry": 0, "blocked_exit": 0})
            slot["blocked_entry"] += item["blocked_entry"]
            slot["blocked_exit"] += item["blocked_exit"]
            if item.get("metrics"):
                slot["rows"].append(item["metrics"])
    out = []
    for (hold, entry), v in sorted(grid.items()):
        rows = v.get("rows", [])
        if not rows:
            continue
        n = sum(x["n"] for x in rows)
        win = sum(x["win_rate"] * x["n"] for x in rows) / n if n else 0.0
        avg = sum(x["avg"] * x["n"] for x in rows) / n if n else 0.0
        cum = sum(x["cumulative"] for x in rows) / len(rows)
        dd = max(x["max_dd"] for x in rows)
        sharpe = sum(x["sharpe"] * x["n"] for x in rows) / n if n else 0.0
        positive = sum(1 for x in rows if x["avg"] > 0)
        out.append({"hold": hold, "entry": entry, "n": n, "win_rate": win,
                    "avg": avg, "cum_avg": cum, "max_dd": dd, "sharpe": sharpe,
                    "positive_symbols": positive, "symbols": len(rows),
                    "blocked_entry": v["blocked_entry"], "blocked_exit": v["blocked_exit"]})
    return out


def build_report(results, errors, args):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_trades, all_signals = [], []
    for r in results:
        all_trades.extend(r["trades"])
        all_signals.extend(r["signals"])
    net_metrics = metrics_from_returns([t["ret"] for t in all_trades], args.hold)
    gross_metrics = metrics_from_returns([t["gross_ret"] for t in all_trades], args.hold)
    blocked_entry = sum(r["blocked_entry"] for r in results)
    blocked_exit = sum(r["blocked_exit"] for r in results)
    real_fee_trades = sum(r.get("real_fee_trades", 0) for r in results)
    total_trades = len(all_trades)
    fee_rows = load_fee_schedule(args.fees_file) if not args.no_real_fees else {}
    fee_as_of = sorted({str(r.get("as_of", "")) for r in fee_rows.values() if r.get("as_of")})
    fee_date = f"，交易日{fee_as_of[0]}" if len(fee_as_of) == 1 else ""
    if args.no_cost:
        cost_txt = "不计成本"
    elif args.no_real_fees or not fee_rows:
        cost_txt = f"兜底统一手续费：单边{args.fee_rate*100:.3f}%+滑点{args.slip_rate*100:.3f}%，往返{2*(args.fee_rate+args.slip_rate)*100:.3f}%"
    else:
        cost_txt = (f"真实券商手续费表{fee_date}（{real_fee_trades}/{total_trades}笔命中；按金额+按手数，开仓+平仓）"
                    f"+滑点单边{args.slip_rate*100:.3f}%；日线多日持仓不使用平今费率，未命中则回退单边{args.fee_rate*100:.3f}%")
    limit_txt = "不过滤锁板" if args.no_limit_filter else f"疑似锁板阈值±{args.limit_move*100:.0f}%"
    fill_txt = ("信号根收盘价成交（旧口径，便于纵向比较）" if args.fill == "close"
                else "次根开盘价成交（next_open 保守对照：看到收盘信号后已无法以该价成交，更贴近实盘）")
    impact_txt = "未计冲击成本" if args.impact_rate <= 0 else f"另计单边冲击成本{args.impact_rate*100:.3f}%（往返两次）"
    L = ["=" * 96,
         f" 最小日线技术回测（生成于 {now}）",
         "=" * 96,
         f"参数：样本{args.days}根日线；入场|技术分|≥{args.entry}；固定持有{args.hold}个交易日，反向信号提前退出；成交时点：{fill_txt}。",
         f"交易成本：{cost_txt}；{impact_txt}；成交限制：{limit_txt}。",
         "口径：仅回放日线动量+RSI/MACD/KDJ多周期共振，不含历史新闻、机构、实时量仓和分钟K线；主连疑似换月跳空已置0并比例复权。",
         "注意：单品种交易为非重叠；总体多品种净值按交易序列复利近似，实盘组合同时持仓时需另做资金权重曲线。"]
    ref_txt = _fmt_validation_ref(getattr(args, "_validation", None))
    if ref_txt:
        L.append("防过拟合交叉引用（tools/backtest_validation.py 最近结论，非本次计算）：" + ref_txt)
    arch_txt = _fmt_archive_info(getattr(args, "_archive", None))
    if arch_txt:
        L.append(arch_txt)
    L.append("")
    L.append("一、总体非重叠交易表现（扣费后）")
    L.append("  净: " + _fmt_metrics(net_metrics))
    if gross_metrics:
        L.append(f"  毛: {_fmt_metrics(gross_metrics)}｜成本拖累 均收{(net_metrics['avg']-gross_metrics['avg'])*100:+.2f}%/"
                 f"累计{(net_metrics['cumulative']-gross_metrics['cumulative'])*100:+.1f}%")
    unfilled = sum(r.get("unfilled_entry", 0) for r in results)
    extra = f"；next_open末根信号无次根成交、丢弃{unfilled}个" if unfilled else ""
    L.append(f"  锁板过滤：入场跳过{blocked_entry}次，离场顺延{blocked_exit}次{extra}")
    for direction in ("多", "空"):
        vals = [t["ret"] for t in all_trades if t["direction"] == direction]
        L.append(f"  {direction}头：" + _fmt_metrics(metrics_from_returns(vals, args.hold)))
    for band in ("轻仓", "分批", "强信号"):
        vals = [t["ret"] for t in all_trades if score_band(t["entry_score"]) == band]
        L.append(f"  {band}：" + _fmt_metrics(metrics_from_returns(vals, args.hold)))
    L.append(_fmt_g3_extended([t["ret"] for t in all_trades], args.hold))
    if not getattr(args, "no_bootstrap", False) and args.bootstrap > 0 and net_metrics:
        ci = tuple(config.BACKTEST_BOOTSTRAP_CI)
        boot_all = bootstrap_trade_stats([t["ret"] for t in all_trades], args.bootstrap,
                                         args.seed, ci, config.BACKTEST_BOOTSTRAP_MIN_TRADES)
        L.append("  置信区间（交易级iid bootstrap，假设逐笔近似独立；强自相关时区间偏乐观）：")
        L.append("    全部：" + _fmt_boot_ci(boot_all))
        for direction in ("多", "空"):
            vals = [t["ret"] for t in all_trades if t["direction"] == direction]
            b = bootstrap_trade_stats(vals, args.bootstrap, args.seed, ci,
                                      config.BACKTEST_BOOTSTRAP_MIN_TRADES)
            L.append(f"    {direction}头：" + _fmt_boot_ci(b))
    if getattr(args, "oos_ratio", 0.0) and 0.0 < args.oos_ratio < 1.0:
        is_tr, oos_tr = split_is_oos(all_trades, args.oos_ratio)
        is_pct = (1.0 - args.oos_ratio) * 100
        L.append(f"  样本内外对照（按平仓时间排序，前{is_pct:.0f}%为IS样本内、后{args.oos_ratio*100:.0f}%为OOS样本外）：")
        L.append("    IS 全部：" + _fmt_metrics(metrics_from_returns([t["ret"] for t in is_tr], args.hold)))
        L.append("    OOS全部：" + _fmt_metrics(metrics_from_returns([t["ret"] for t in oos_tr], args.hold)))
        for direction in ("多", "空"):
            iv = [t["ret"] for t in is_tr if t["direction"] == direction]
            ov = [t["ret"] for t in oos_tr if t["direction"] == direction]
            L.append(f"    IS/OOS {direction}头："
                     + _fmt_metrics(metrics_from_returns(iv, args.hold)) + " ｜ "
                     + _fmt_metrics(metrics_from_returns(ov, args.hold)))
    # ---- G4续：对照基准（买入持有主连 / 等权篮子 / 超额） ----
    if not getattr(args, "no_benchmark", False):
        pairs = []
        for r in results:
            tm = r.get("trade_metrics")
            pairs.append((r["code"], tm["cumulative"] if tm else None, r.get("buy_hold")))
        beat, n_valid, brows = br.beat_benchmark_pairs(pairs)
        pool_bh = br.pooled_buy_hold([p[2] for p in pairs])
        if n_valid and net_metrics and pool_bh is not None:
            ex = br.excess(net_metrics["cumulative"], pool_bh)
            L.append("  对照基准（同区间一直买入持有主连，主连已比例复权；策略仅信号时持仓、基准全程持仓）：")
            L.append(f"    等权{n_valid}品种篮子买入持有累计 {pool_bh*100:+.1f}%；策略净累计 "
                     f"{net_metrics['cumulative']*100:+.1f}%；超额(算术差) {ex*100:+.1f}个百分点")
            L.append(f"    逐品种跑赢买入持有基准 {beat}/{n_valid}，跑输/持平 {n_valid-beat}/{n_valid}")
    # ---- G4续：滚动 walk-forward 纯样本外 ----
    wf_symbols = [r for r in results if r.get("wf")]
    if wf_symbols:
        all_oos = [t for r in wf_symbols for t in r["wf"]["oos_trades"]]
        all_folds = [f for r in wf_symbols for f in r["wf"]["folds"]]
        L.append("  滚动walk-forward纯样本外（每折只在前段IS的3×3网格选参、用于后段互不重叠OOS；选参不含本段未来）：")
        L.append("    OOS拼接全部：" + _fmt_metrics(metrics_from_returns(
            [t["ret"] for t in all_oos], args.hold)))
        for direction in ("多", "空"):
            dv = [t["ret"] for t in all_oos if t["direction"] == direction]
            L.append(f"    OOS拼接{direction}头：" + _fmt_metrics(metrics_from_returns(dv, args.hold)))
        is_avg, oos_avg = br.is_vs_oos_avg(all_folds)
        usage = br.param_usage(all_folds)
        fb = sum(1 for f in all_folds if f["fallback"])
        if is_avg is not None:
            L.append(f"    共{len(wf_symbols)}品种/{len(all_folds)}折（其中{fb}折IS样本不足回退默认参数）；"
                     f"折级IS均收{is_avg*100:+.2f}%→OOS均收{oos_avg*100:+.2f}%；选参分布 {usage}")
        else:
            L.append(f"    共{len(wf_symbols)}品种/{len(all_folds)}折；IS/OOS均有交易的折不足，无法比较衰减；选参分布 {usage}")
    L.append("")
    L.append("二、信号发出后固定持有 1/5/20 个交易日的方向收益（允许样本重叠，用于观察衰减）")
    for h in (1, 5, 20):
        vals = [s[f"h{h}"] for s in all_signals if s.get(f"h{h}") is not None]
        L.append(f"  {h:>2}日：" + _fmt_overlap_metrics(metrics_from_returns(vals, h)))
    L.append("")
    if not args.no_stable:
        L.append("三、参数稳定性扫描（扣费后；持有日×入场阈值，分品种按交易数加权）")
        L.append("  " + f"{'持有':<6}{'阈值':<8}{'样本':<8}{'胜率':<10}{'均收':<12}{'平均累计':<12}{'最大回撤':<12}{'夏普':<8}{'正均收品种'}")
        for x in _aggregate_stability(results):
            L.append("  " + f"{x['hold']:<6}{x['entry']:<8}{x['n']:<8}"
                     f"{x['win_rate']*100:.1f}%".ljust(16) +
                     f"{x['avg']*100:+.2f}%".ljust(12) +
                     f"{x['cum_avg']*100:+.1f}%".ljust(12) +
                     f"{x['max_dd']*100:.1f}%".ljust(12) +
                     f"{x['sharpe']:.2f}".ljust(8) +
                     f"{x['positive_symbols']}/{x['symbols']}")
        L.append("")
    L.append(("四" if not args.no_stable else "三") + "、分品种交易表现（扣费后，按交易次数排序，最多展示30个）")
    ranked = sorted([r for r in results if r.get("trade_metrics")],
                    key=lambda x: -x["trade_metrics"]["n"])[:30]
    for r in ranked:
        m = r["trade_metrics"]
        gm = r.get("gross_metrics")
        gross_txt = "" if not gm else f"｜毛均收{gm['avg']*100:+.2f}%"
        L.append(f"  {r['name']:<8} {r['code']:<8} K线{r['bars']}根/换月修正{r['roll_count']}处/"
                 f"真实费率{r.get('real_fee_trades',0)}/{m['n']}笔/锁板入场跳过{r['blocked_entry']}、"
                 f"离场顺延{r['blocked_exit']}{gross_txt}｜" + _fmt_metrics(m))
    if errors:
        L.append("")
        L.append(f"{'五' if not args.no_stable else '四'}、失败品种（{len(errors)}个，前20个）")
        for name, err in errors[:20]:
            L.append(f"  {name}: {err}")
    L.append("")
    L.append("结论使用规则：胜率和盈亏比需同时看；样本<20只作观察。扣费后仍稳定、且参数网格多数为正，才说明技术规则具备初步稳健性。")
    return "\n".join(L) + "\n"


def _cost_mode_text(args):
    if args.no_cost:
        return "不计成本"
    if args.no_real_fees:
        return f"兜底比例费率+滑点{args.slip_rate}+冲击{args.impact_rate}"
    return f"真实费率表+滑点{args.slip_rate}+冲击{args.impact_rate}"


def archive_run(args, results, errors, net_metrics, db_factory=None):
    """把本次回测落 storage.backtest_runs 一行，并返回纵向对比信息。
    离线研究工具：默认写生产 monitor.db；任何异常（库损坏/不可写）软降级返回 None，
    绝不让留档失败拖垮回测报告。db_factory 供测试注入临时库。"""
    try:
        if db_factory is not None:
            db = db_factory()
        else:
            import storage
            db = storage.MonitorDB()
    except Exception:
        return None
    try:
        run_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        params = {"days": args.days, "hold": args.hold, "entry": args.entry,
                  "fill": args.fill, "slip_rate": args.slip_rate,
                  "impact_rate": args.impact_rate, "fee_rate": args.fee_rate,
                  "no_real_fees": args.no_real_fees, "no_cost": args.no_cost,
                  "oos_ratio": args.oos_ratio, "no_limit_filter": args.no_limit_filter,
                  "stable": not args.no_stable, "bootstrap": args.bootstrap}
        metrics = dict(net_metrics or {})
        rid = db.insert_backtest_run({
            "run_ts": run_ts, "kind": "daily", "fill_mode": args.fill,
            "cost_mode": _cost_mode_text(args), "n_symbols": len(results),
            "n_trades": metrics.get("n", 0), "sample_days": args.days,
            "params": params, "metrics": metrics,
            "cumulative": metrics.get("cumulative"), "max_dd": metrics.get("max_dd"),
            "sharpe": metrics.get("sharpe"), "win_rate": metrics.get("win_rate")})
        hist = db.backtest_run_history("daily")
        cums = [h["cumulative"] for h in hist if h["cumulative"] is not None]
        pct = percentile_at_or_below(cums, metrics.get("cumulative"))
        return {"id": rid, "seq": len(hist), "total": len(hist), "percentile": pct}
    except Exception:
        return None
    finally:
        try:
            db.close()
        except Exception:
            pass


def write_outputs(results, errors, args):
    os.makedirs(os.path.dirname(config.BACKTEST_REPORT_FILE), exist_ok=True)
    report = build_report(results, errors, args)
    with open(config.BACKTEST_REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(report)
    with open(config.BACKTEST_SIGNALS_FILE, "w", encoding="utf-8-sig", newline="") as f:
        fieldnames = ["symbol", "code", "date", "direction", "score", "band", "h1", "h5", "h20"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in results:
            for s in r["signals"]:
                w.writerow({k: s.get(k) for k in fieldnames})
    with open(config.BACKTEST_TRADES_FILE, "w", encoding="utf-8-sig", newline="") as f:
        fieldnames = ["symbol", "code", "entry_date", "exit_date", "direction",
                      "entry_score", "hold", "gross_ret", "fee_cost", "slip_cost",
                      "impact_cost", "cost", "ret", "fee_open_yuan", "fee_close_yuan",
                      "fee_round_yuan", "multiplier", "fee_mode",
                      "blocked_exits", "exit"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in results:
            for t in r["trades"]:
                w.writerow({k: t.get(k) for k in fieldnames})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="期货技术信号最小日线回测")
    parser.add_argument("--codes", default="", help="逗号分隔品种名或主连代码，如 RB0,MA0")
    parser.add_argument("--all", action="store_true", help="回测全部64个品种（默认也是全部，可用--limit限量）")
    parser.add_argument("--limit", type=int, default=0, help="只回测前N个品种，便于快速抽样")
    parser.add_argument("--days", type=int, default=config.BACKTEST_LOOKBACK_DAYS)
    parser.add_argument("--hold", type=int, default=config.BACKTEST_HOLD_DAYS)
    parser.add_argument("--entry", type=float, default=config.BACKTEST_ENTRY_SCORE)
    parser.add_argument("--workers", type=int, default=config.BACKTEST_WORKERS)
    parser.add_argument("--fee-rate", type=float, default=config.BACKTEST_FEE_RATE,
                        help="真实费率表缺失时的兜底单边手续费率，默认万0.5")
    parser.add_argument("--slip-rate", type=float, default=config.BACKTEST_SLIP_RATE,
                        help="单边滑点率（按价格比例），默认万1")
    parser.add_argument("--fees-file", default=config.FUTURES_FEES_FILE,
                        help="真实券商手续费CSV，默认data/futures_fees.csv")
    parser.add_argument("--no-real-fees", action="store_true",
                        help="不读真实手续费表，统一使用--fee-rate比例费率")
    parser.add_argument("--limit-move", type=float, default=config.BACKTEST_LIMIT_LOCK,
                        help="疑似锁涨跌停阈值，默认7%且要求收在最高/最低")
    parser.add_argument("--no-cost", action="store_true", help="不扣手续费和滑点")
    parser.add_argument("--no-limit-filter", action="store_true", help="不过滤疑似锁涨跌停")
    parser.add_argument("--no-stable", action="store_true", help="跳过3x3参数稳定性扫描")
    parser.add_argument("--fill", choices=("close", "next_open"),
                        default=config.BACKTEST_FILL_MODE,
                        help="成交时点：close=信号根收盘(默认,旧口径)；next_open=次根开盘(保守对照)")
    parser.add_argument("--impact-rate", type=float, default=config.BACKTEST_IMPACT_RATE,
                        help="单边冲击成本率(价格比例)，默认0=不额外计，往返两次")
    parser.add_argument("--bootstrap", type=int, default=config.BACKTEST_BOOTSTRAP_N,
                        help="交易序列bootstrap重采样次数，0=关闭，默认1000")
    parser.add_argument("--no-bootstrap", action="store_true", help="关闭bootstrap置信区间")
    parser.add_argument("--seed", type=int, default=config.BACKTEST_BOOTSTRAP_SEED,
                        help="bootstrap固定随机种子（可复现）")
    parser.add_argument("--oos-ratio", type=float, default=config.BACKTEST_OOS_RATIO,
                        help="样本外占比(0=关闭)；如0.3=后30%%交易为OOS与前70%%IS并列对照")
    parser.add_argument("--no-archive", action="store_true",
                        help="不向 storage.backtest_runs 落本次留档")
    parser.add_argument("--no-validation-ref", action="store_true",
                        help="报告抬头不引用 backtest_validation 的 DSR/PBO sidecar")
    parser.add_argument("--no-benchmark", action="store_true",
                        help="关闭买入持有/等权篮子对照基准与超额（默认给基准）")
    parser.add_argument("--walk-forward", action="store_true",
                        help="开启滚动walk-forward：每折只在前段IS选参、用于后段OOS，拼接纯样本外轨迹")
    parser.add_argument("--wf-train", type=int, default=config.BACKTEST_WF_TRAIN_BARS,
                        help="walk-forward 每折IS训练窗交易日根数（默认%d）" % config.BACKTEST_WF_TRAIN_BARS)
    parser.add_argument("--wf-test", type=int, default=config.BACKTEST_WF_TEST_BARS,
                        help="walk-forward 每折OOS测试窗交易日根数（默认%d，折间不重叠）" % config.BACKTEST_WF_TEST_BARS)
    args = parser.parse_args(argv)
    if args.no_cost:
        args.fee_rate = 0.0
        args.slip_rate = 0.0
        args.impact_rate = 0.0
        args.no_real_fees = True

    items = resolve_codes(args.codes, args.limit if args.limit > 0 else None)
    results, errors = [], []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(fetch_and_run, item, args) for item in items]
        for fut in as_completed(futures):
            name, result, err = fut.result()
            if result:
                results.append(result)
            elif err:
                errors.append((name, err))
    results.sort(key=lambda r: r["code"])
    all_trades = [t for r in results for t in r["trades"]]
    net_metrics = metrics_from_returns([t["ret"] for t in all_trades], args.hold)
    if not args.no_archive and results:
        args._archive = archive_run(args, results, errors, net_metrics)
    else:
        args._archive = None
    args._validation = None if args.no_validation_ref else load_validation_sidecar()
    report = write_outputs(results, errors, args)
    print(report)
    print(f"报告已写入: {config.BACKTEST_REPORT_FILE}")
    print(f"信号明细已写入: {config.BACKTEST_SIGNALS_FILE}")
    print(f"交易明细已写入: {config.BACKTEST_TRADES_FILE}")
    return 0 if results else 2


if __name__ == "__main__":
    raise SystemExit(main())
