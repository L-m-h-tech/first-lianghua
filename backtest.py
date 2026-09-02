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
import math
import os
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import config
import futures_data


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


def prepare_symbol(raw_bars):
    """只计算一次指标序列，参数稳定性扫描复用，避免重复计算。"""
    bars, roll_count = ratio_adjusted_bars(raw_bars)
    if len(bars) < 65:
        return None
    closes = [futures_data._f(b["c"]) for b in bars]
    highs = [futures_data._f(b["h"]) for b in bars]
    lows = [futures_data._f(b["l"]) for b in bars]
    series = []
    for i in range(60, len(closes)):
        ind = futures_data.compute_indicators(bars[:i + 1])
        series.append({"i": i, "ind": ind, "score": technical_score(ind)})
    return {"name": "", "code": "", "bars": bars, "closes": closes,
            "highs": highs, "lows": lows, "series": series, "roll_count": roll_count}


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
                 fee_table, use_real_fees):
    closes = prepared["closes"]
    gross = position["direction"] * (closes[i] / position["entry_price"] - 1.0)
    sym = str(prepared.get("sym") or "").strip().upper()
    if use_real_fees and sym in fee_table:
        exit_fee_rate, exit_fee_yuan = side_fee(fee_table[sym], closes[i], "close")
        fee_mode = "真实费率表"
    else:
        exit_fee_rate, exit_fee_yuan = fee_rate, 0.0
        fee_mode = "兜底比例费率"
    fee_cost = position.get("entry_fee_rate", fee_rate) + exit_fee_rate
    slip_cost = 2.0 * slip_rate
    cost = fee_cost + slip_cost
    return {
        "symbol": name, "code": code,
        "entry_date": position["entry_date"], "exit_date": prepared["bars"][i].get("d", ""),
        "direction": "多" if position["direction"] > 0 else "空",
        "entry_score": position["score"], "hold": i - position["entry_i"],
        "gross_ret": gross, "fee_cost": fee_cost, "slip_cost": slip_cost,
        "cost": cost, "ret": gross - cost,
        "fee_open_yuan": position.get("entry_fee_yuan", 0.0),
        "fee_close_yuan": exit_fee_yuan,
        "fee_round_yuan": position.get("entry_fee_yuan", 0.0) + exit_fee_yuan,
        "multiplier": fee_table.get(sym, {}).get("multiplier", 0) if use_real_fees else 0,
        "fee_mode": fee_mode,
        "blocked_exits": position.get("blocked_exits", 0), "exit": exit_reason}


def simulate_prepared(name, code, prepared, hold_days, entry_score,
                      fee_rate=0.0, slip_rate=0.0, limit_move=None,
                      collect_signals=False, fee_table=None, use_real_fees=True):
    closes = prepared["closes"]
    signal_rows, trades = [], []
    position = None
    blocked_entry = blocked_exit = 0
    fee_table = fee_table or {}
    fallback_cost_round = 2.0 * (fee_rate + slip_rate)

    for item in prepared["series"]:
        i = item["i"]
        score = item["score"]
        direction = _direction_from_score(score, entry_score)

        if direction and collect_signals:
            row = {"symbol": name, "code": code, "date": prepared["bars"][i].get("d", ""),
                   "score": score, "band": score_band(score),
                   "direction": "多" if direction > 0 else "空"}
            for horizon in (1, 5, 20):
                if i + horizon < len(closes):
                    row[f"h{horizon}"] = direction * (closes[i + horizon] / closes[i] - 1.0)
                else:
                    row[f"h{horizon}"] = None
            signal_rows.append(row)

        if position is not None:
            opposite = direction == -position["direction"] and direction != 0
            held = i - position["entry_i"]
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
                                              fee_rate, slip_rate, fee_table, use_real_fees))
                    position = None

        if position is None and direction:
            if _locked_limit(prepared, i, direction, limit_move):
                blocked_entry += 1
                continue
            entry_sym = str(prepared.get("sym") or "").strip().upper()
            if use_real_fees and entry_sym in fee_table:
                entry_fee_rate, entry_fee_yuan = side_fee(fee_table[entry_sym], closes[i], "open")
            else:
                entry_fee_rate, entry_fee_yuan = fee_rate, 0.0
            position = {"entry_i": i, "direction": direction, "score": score,
                        "entry_price": closes[i], "entry_date": prepared["bars"][i].get("d", ""),
                        "entry_fee_rate": entry_fee_rate, "entry_fee_yuan": entry_fee_yuan,
                        "blocked_exits": 0}

    if position is not None:
        i = len(closes) - 1
        trades.append(_build_trade(name, code, prepared, position, i, "样本末",
                                  fee_rate, slip_rate, fee_table, use_real_fees))

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
            "blocked_exit": blocked_exit, "cost_round": sample_cost,
            "real_fee_trades": sum(1 for t in trades if t["fee_mode"] == "真实费率表"),
            "last_signal": (signal_rows[-1] if signal_rows else None)}


def simulate_symbol(name, code, raw_bars, hold_days, entry_score,
                    fee_rate=0.0, slip_rate=0.0, limit_move=None,
                    fee_table=None, use_real_fees=True):
    prepared = prepare_symbol(raw_bars)
    if prepared is None:
        return None
    prepared["name"], prepared["code"] = name, code
    prepared["sym"] = code.rstrip("0").upper()
    return simulate_prepared(name, code, prepared, hold_days, entry_score,
                             fee_rate, slip_rate, limit_move, collect_signals=True,
                             fee_table=fee_table, use_real_fees=use_real_fees)


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
                                   use_real_fees=not args.no_real_fees)
        if not args.no_stable:
            stability = []
            for hold in config.BACKTEST_STABLE_HOLDS:
                for entry in config.BACKTEST_STABLE_ENTRIES:
                    rr = simulate_prepared(name, code, prepared, hold, entry,
                                           args.fee_rate, args.slip_rate,
                                           None if args.no_limit_filter else args.limit_move,
                                           collect_signals=False, fee_table=fee_table,
                                           use_real_fees=not args.no_real_fees)
                    stability.append({"hold": hold, "entry": entry,
                                      "metrics": rr["trade_metrics"],
                                      "blocked_entry": rr["blocked_entry"],
                                      "blocked_exit": rr["blocked_exit"]})
            result["stability"] = stability
        else:
            result["stability"] = []
        return name, result, ""
    except Exception as e:
        return name, None, f"{type(e).__name__}: {e}"


def _fmt_metrics(m):
    if not m:
        return "样本不足"
    pl = "--" if m["pl_ratio"] is None else f"{m['pl_ratio']:.2f}"
    return (f"n={m['n']} 胜率{m['win_rate']*100:.1f}% 均收{m['avg']*100:+.2f}% "
            f"盈/亏{pl} 累计{m['cumulative']*100:+.1f}% 年化{m['annualized']*100:+.1f}% "
            f"最大回撤{m['max_dd']*100:.1f}% 夏普{m['sharpe']:.2f}")


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
    L = ["=" * 96,
         f" 最小日线技术回测（生成于 {now}）",
         "=" * 96,
         f"参数：样本{args.days}根日线；入场|技术分|≥{args.entry}；固定持有{args.hold}个交易日，反向信号提前退出。",
         f"交易成本：{cost_txt}；成交限制：{limit_txt}。",
         "口径：仅回放日线动量+RSI/MACD/KDJ多周期共振，不含历史新闻、机构、实时量仓和分钟K线；主连疑似换月跳空已置0并比例复权。",
         "注意：单品种交易为非重叠；总体多品种净值按交易序列复利近似，实盘组合同时持仓时需另做资金权重曲线。",
         ""]
    L.append("一、总体非重叠交易表现（扣费后）")
    L.append("  净: " + _fmt_metrics(net_metrics))
    if gross_metrics:
        L.append(f"  毛: {_fmt_metrics(gross_metrics)}｜成本拖累 均收{(net_metrics['avg']-gross_metrics['avg'])*100:+.2f}%/"
                 f"累计{(net_metrics['cumulative']-gross_metrics['cumulative'])*100:+.1f}%")
    L.append(f"  锁板过滤：入场跳过{blocked_entry}次，离场顺延{blocked_exit}次")
    for direction in ("多", "空"):
        vals = [t["ret"] for t in all_trades if t["direction"] == direction]
        L.append(f"  {direction}头：" + _fmt_metrics(metrics_from_returns(vals, args.hold)))
    for band in ("轻仓", "分批", "强信号"):
        vals = [t["ret"] for t in all_trades if score_band(t["entry_score"]) == band]
        L.append(f"  {band}：" + _fmt_metrics(metrics_from_returns(vals, args.hold)))
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
                      "cost", "ret", "fee_open_yuan", "fee_close_yuan",
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
    args = parser.parse_args(argv)
    if args.no_cost:
        args.fee_rate = 0.0
        args.slip_rate = 0.0
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
    report = write_outputs(results, errors, args)
    print(report)
    print(f"报告已写入: {config.BACKTEST_REPORT_FILE}")
    print(f"信号明细已写入: {config.BACKTEST_SIGNALS_FILE}")
    print(f"交易明细已写入: {config.BACKTEST_TRADES_FILE}")
    return 0 if results else 2


if __name__ == "__main__":
    raise SystemExit(main())
