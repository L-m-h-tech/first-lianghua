# -*- coding: utf-8 -*-
"""日内/平今回测（第15轮 WP-D1/D2，零新增第三方依赖）。

数据：storage.minute_bars 自采库（新浪主连1/5/15/30/60分钟，常驻积累；单周期约1023根），
     可选 --aggregate-from 1 用 intraday_bars.aggregate_bars 从1m现场聚合，交叉验证库内粗周期。
信号：分钟级"短中长三周期共振 + 波动标准化动量"，指标底层函数与 futures_data/analyzer 同源，
     但不重复计算HV锥（分钟滚动下太贵）；信号序列只预算一次，参数稳定性网格复用。
撮合：vnpy 式 bar 内保守撮合——
  · 信号在 bar i 收盘确认，一律在 bar i+1 开盘价成交（杜绝未来函数），入场当根不检查止损；
  · 止损/止盈为预埋停止单：跳空越过止损/止盈以开盘价成交，bar内触及以触发价成交，
    同一根bar同时触及止损与止盈时，保守按止损成交（vnpy 同约定）；
  · 反向信号/持有到期在"下一根开盘"离场；日内模式每个交易日最后一根bar按收盘价强平（不隔夜）；
  · 精确锁板：以前一交易日收盘价×(1±品种涨跌停常态幅度)为板价，整根bar封死在板价才判
    "买不进/卖不出"并顺延，仅触及不封死不算锁板；v==0 的bar不成交。
费用：开仓走 open 费率；平仓按 entry_dt 与 exit_dt 的【交易所结算交易日】比较——同一交易日（前一晚
     21:00夜盘到当日15:00日盘为同一交易日）开平走 today 平今费率，跨交易日走 close 平昨费率；真实券商费率表
     data/futures_fees.csv（含 multiplier 合约乘数），另扣单边滑点；同时输出"若按平昨计费"的
     对照人民币金额，量化平今优惠/加收的真实影响。
输出：reports/intraday_backtest_report.txt、reports/intraday_backtest_trades.csv，并挂到实时看板页签。

诚实边界（不伪装成逐笔回测）：
  - 只回放分钟技术面，不复原历史新闻/机构观点/L2逐笔/盘口排队；bar内触及顺序不可知，统一保守处理；
  - 分钟库窗口有限（1m约2~3个交易日、5m约1个月、30m约6个月），窗口外历史不可复原，靠常驻自采积累；
  - 主连为比例复权连续序列，换月跳空按 backtest.ratio_adjusted_bars 置零并后复权；
  - 涨跌停按"前一交易日收盘价"近似（免费分钟库无昨结算字段），且用常态档幅度，未建模长假扩板/
    交割月梯度，仅用于保守锁板识别；滑点为统一比例近似；结果仅供学习研究，不构成投资建议。

运行示例：
  D:\\Python\\python.exe intraday_backtest.py --codes RB,MA --period 30
  D:\\Python\\python.exe intraday_backtest.py --all --period 5 --swing --max-bars 24
  D:\\Python\\python.exe intraday_backtest.py --codes RB --period 5 --aggregate-from 1
  D:\\Python\\python.exe intraday_backtest.py --all --period 30 --no-cost --no-limit-filter
"""
import argparse
import csv
import math
import os
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import config
import futures_data
from futures_data import (_sma_series, _ema_series, _rsi_series, _kdj_series,
                          clip, _sample_std)
from backtest import load_fee_schedule, side_fee, ratio_adjusted_bars
import trade_calendar
from utils import now_str
import storage

DISCLAIMER = ("本回测仅回放分钟技术面，使用常驻自采的有限窗口分钟K线，bar内成交按vnpy式保守假设，"
              "涨跌停为前收×常态幅度的近似；不构成投资建议，据此操作风险自负。")


# ------------------------- 交易日归属（与 utils.trade_owner_date / report._owner_of_ts 同口径） -------------------------
def owner_of_dt(dt):
    """分钟bar归属的【交易所结算交易日】（平今/平昨与日终强平都按此口径，与utils里服务日切的
    "夜盘开盘日"口径不同，不可混用）：
      - 日盘 09:00-15:00：归属当天自然日；
      - 夜盘 21:00-24:00：归属【下一交易日】（周一晚夜盘与周二日盘同属周二，周五晚归属下周一）；
      - 凌晨 00:00-02:30：前一晚夜盘延续，归属当天自然日（周六凌晨顺延到下周一）；
    下一交易日由项目离线交易日历 trade_calendar 推算，法定节假日自动跳到节后，无需手工维护。"""
    d = dt.date()
    if dt.hour >= 21:
        return trade_calendar.next_trade_day(d)
    if dt.hour < 9:
        return d if trade_calendar.is_trade_day(d) else trade_calendar.next_trade_day(d)
    return d


def build_owner_meta(bars):
    """返回 (owners, prev_base)：prev_base[i]=该bar所属交易日的"前一交易日收盘价"（涨跌停基准）。"""
    trade_calendar.ensure()
    owners = [owner_of_dt(b["dt"]) for b in bars]
    seg_last, order = {}, []
    for i, o in enumerate(owners):
        if o not in seg_last:
            order.append(o)
        seg_last[o] = bars[i]["c"]
    prev_seg_close = {o: (seg_last[order[k - 1]] if k > 0 else None)
                      for k, o in enumerate(order)}
    bases = [prev_seg_close[o] for o in owners]
    return owners, bases


# ------------------------- 数据加载 -------------------------
def _to_dt(x):
    """minute_bars 库存 dt 为 'YYYY-MM-DD HH:MM' 文本，统一解析为 datetime（聚合路径同样兜底）。"""
    if isinstance(x, datetime):
        return x
    return datetime.strptime(str(x)[:16], "%Y-%m-%d %H:%M")


def load_minute_bars(db, sym, period, lookback, aggregate_from):
    """从自采分钟库读主连bar（sym 为无0品种码，如 RB）；aggregate_from 指定更细周期时现场聚合。"""
    period, aggregate_from = int(period), int(aggregate_from or 0)
    if aggregate_from and aggregate_from != period and period % aggregate_from == 0:
        import intraday_bars
        raw = db.minute_bars_for_sym(sym, aggregate_from, limit=lookback * (period // aggregate_from) + 4)
        # 相位对齐：1m自采起点未必落在目标周期边界（如21:13起），裁到首个整边界之后，
        # 使聚合段末时间戳与交易所原生粗周期（整5/15/30分）一致，避免K线整体错位一根。
        if aggregate_from == 1 and period in (5, 15, 30, 60):
            for k, b in enumerate(raw):
                dtb = intraday_bars._parse_dt(b.get("dt"))
                if dtb is not None and dtb.minute % period == 0:
                    raw = raw[k + 1:]
                    break
        merged = intraday_bars.aggregate_bars(raw, aggregate_from, period // aggregate_from)
        src = f"{aggregate_from}m边界对齐后聚合到{period}m"
    else:
        merged = db.minute_bars_for_sym(sym, period, limit=lookback)
        src = f"{period}m库内直读"
    out = []
    for b in merged[-lookback:]:
        try:
            dtv = _to_dt(b["dt"])
            out.append({"dt": dtv, "d": dtv, "contract": b.get("contract", ""),
                        "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]),
                        "c": float(b["c"]), "v": float(b.get("v") or 0.0)})
        except (KeyError, TypeError, ValueError):
            continue
    return out, src


def resolve_items(codes_arg, limit=0):
    """品种解析：支持 'RB'/'RB0'/中文名'螺纹钢'/留空(全品种)；返回 (sym无0码, 主连码, 中文名)。"""
    by_sym = {m["sym"]: (name, m) for name, m in config.VARIETIES.items()}
    if not codes_arg:
        items = [(m["sym"], m["code"], name) for name, m in config.VARIETIES.items()]
    else:
        items = []
        for tok in codes_arg.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if tok in config.VARIETIES:                       # 中文名
                m = config.VARIETIES[tok]
                items.append((m["sym"], m["code"], tok))
                continue
            sym = tok.upper().rstrip("0")                    # RB / RB0
            if sym in by_sym:
                name, m = by_sym[sym]
                items.append((sym, m["code"], name))
    return items[:limit] if limit else items


# ------------------------- 分钟信号（与技术面同源，去掉HV锥） -------------------------
def _majority(bull, bear):
    b, s = sum(bool(x) for x in bull), sum(bool(x) for x in bear)
    return 1 if b > s else (-1 if s > b else 0)


def atr_at(highs, lows, closes, period):
    n = len(closes)
    if n < period + 1:
        return None
    trs = []
    for k in range(n - period, n):
        trs.append(max(highs[k] - lows[k],
                       abs(highs[k] - closes[k - 1]),
                       abs(lows[k] - closes[k - 1])))
    return sum(trs) / len(trs)


def prepare_series(bars, window):
    """每根bar只算一次分钟技术分与ATR；返回 closes/highs/lows/scores/atrs（预热不足为None）。"""
    n = len(bars)
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    scores, atrs = [None] * n, [None] * n
    warm = config.INTRADAY_BT_WARMUP
    for i in range(n):
        lo = max(0, i - window + 1)
        m = i - lo + 1
        if m < warm:
            continue
        c = closes[lo:i + 1]
        h = highs[lo:i + 1]
        l = lows[lo:i + 1]
        mm = len(c)
        ma5 = _sma_series(c, 5)[-1]
        ma10 = _sma_series(c, 10)[-1]
        ma20 = _sma_series(c, 20)[-1]
        ma60 = _sma_series(c, config.TECH_LONG_MA)[-1]
        ef = _ema_series(c, config.TECH_MACD_FAST)
        es = _ema_series(c, config.TECH_MACD_SLOW)
        difs = [ef[k] - es[k] for k in range(mm) if ef[k] is not None and es[k] is not None]
        dea_s = _ema_series(difs, config.TECH_MACD_SIGNAL) if difs else []
        dif = difs[-1] if difs else 0.0
        dea = dea_s[-1] if dea_s else 0.0
        kk, dd, _jj = _kdj_series(h, l, c, config.TECH_KDJ_PERIOD)
        r5 = c[-1] / c[-6] - 1.0 if mm >= 6 and c[-6] > 0 else 0.0
        r20 = c[-1] / c[-21] - 1.0 if mm >= 21 and c[-21] > 0 else 0.0
        k_last, d_last = kk[-1], dd[-1]
        sv = _majority([ma5 and c[-1] > ma5, r5 > 0, k_last is not None and d_last is not None and k_last > d_last],
                       [ma5 and c[-1] < ma5, r5 < 0, k_last is not None and d_last is not None and k_last < d_last])
        mv = _majority([ma20 and c[-1] > ma20, dif >= dea],
                       [ma20 and c[-1] < ma20, dif < dea])
        lv = _majority([ma60 and c[-1] > ma60, ma20 and ma60 and ma20 > ma60],
                       [ma60 and c[-1] < ma60, ma20 and ma60 and ma20 < ma60])
        resonance = clip((sv + mv + lv) / 3.0 * config.TECH_RESONANCE_MAX,
                         -config.TECH_RESONANCE_MAX, config.TECH_RESONANCE_MAX)
        rets = [c[k] / c[k - 1] - 1.0 for k in range(1, mm) if c[k - 1] > 0]
        sd = _sample_std(rets[-60:]) if len(rets) >= 20 else 0.0
        score = resonance * 1.6
        if sd and sd > 1e-12:
            score += math.tanh(r5 / (sd * math.sqrt(5) + 1e-12)) * 1.5
            score += math.tanh(r20 / (sd * math.sqrt(20) + 1e-12)) * 1.0
        else:
            score += math.tanh(r5 * 400.0) * 1.5 + math.tanh(r20 * 150.0) * 1.0
        if ma10:
            score += 0.3 if c[-1] > ma10 else -0.3
        scores[i] = clip(score, -6.0, 6.0)
        atrs[i] = atr_at(h, l, c, config.INTRADAY_BT_ATR_PERIOD)
    return closes, highs, lows, scores, atrs


def _sig_dir(score, entry_th):
    if score is None:
        return 0
    return 1 if score >= entry_th else (-1 if score <= -entry_th else 0)


# ------------------------- 锁板识别 -------------------------
def locked_at(bar, base, move, eps, buying):
    """buying=True：买入/平空被涨停封死（整根最低价都贴在涨停限上）；
    buying=False：卖出/平多被跌停封死（整根最高价都贴在跌停限上）。"""
    if not base or base <= 0 or not move or move >= 1:
        return False
    if buying:
        return bar["l"] >= base * (1.0 + move) * (1.0 - eps)
    return bar["h"] <= base * (1.0 - move) * (1.0 + eps)


# ------------------------- vnpy式逐bar撮合 -------------------------
def simulate(sym, bars, prepared, owners, bases, entry_th, stop_atr, target_atr,
             flat_eod, max_bars, slip, fee_row, use_real_fees, fee_rate,
             use_limit, limit_move, limit_eps):
    closes, highs, lows, scores, atrs = prepared
    n = len(bars)
    mult = float(fee_row["multiplier"]) if (use_real_fees and fee_row) else 0.0
    trades = []
    pos = None
    pending = None
    blocked_entry = blocked_exit = 0

    def close_trade(i, px, reason):
        nonlocal pos
        d = pos["dir"]
        leg = "today" if pos["entry_owner"] == owners[i] else "close"
        if use_real_fees and fee_row and mult > 0:
            ofr, ofee = side_fee(fee_row, pos["entry_px"], "open")
            cfr, cfee = side_fee(fee_row, px, leg)
            _, hypo_yuan = side_fee(fee_row, px, "close")
            fee_mode = "真实费率表"
        else:
            ofr, ofee, cfr, cfee, hypo_yuan, fee_mode = fee_rate, 0.0, fee_rate, 0.0, 0.0, "兜底比例"
        gross = d * (px / pos["entry_px"] - 1.0)
        net = gross - ofr - cfr
        pnl_yuan = d * (px - pos["entry_px"]) * mult - ofee - cfee if mult > 0 else None
        trades.append({
            "sym": sym, "contract": bars[i].get("contract", ""),
            "dir": "多" if d > 0 else "空",
            "entry_dt": pos["entry_dt"].strftime("%Y-%m-%d %H:%M"),
            "exit_dt": bars[i]["dt"].strftime("%Y-%m-%d %H:%M"),
            "entry_owner": pos["entry_owner"].strftime("%Y-%m-%d"),
            "exit_owner": owners[i].strftime("%Y-%m-%d"),
            "leg": "平今" if leg == "today" else "平昨",
            "hold_bars": i - pos["entry_i"],
            "hold_min": round((bars[i]["dt"] - pos["entry_dt"]).total_seconds() / 60.0, 1),
            "entry_px": pos["entry_px"], "exit_px": px,
            "gross": gross, "open_fee_rate": ofr, "close_fee_rate": cfr,
            "slip_rate": slip, "net": net,
            "fee_open_yuan": ofee, "fee_close_yuan": cfee,
            "hypo_close_yuan": hypo_yuan, "today_save_yuan": (hypo_yuan - cfee),
            "pnl_yuan": pnl_yuan, "multiplier": mult, "fee_mode": fee_mode,
            "entry_score": pos["score"], "blocked_exits": pos["block"],
            "reason": reason})
        pos = None

    for i in range(n):
        bar = bars[i]
        # 1) 上一根收盘挂出的委托，在本根开盘成交
        if pending is not None:
            kind = pending[0]
            if kind == "entry":
                d = pending[1]
                is_locked = use_limit and locked_at(bar, bases[i], limit_move, limit_eps, d > 0)
                if bar["v"] <= 0 or is_locked:
                    blocked_entry += 1
                else:
                    px = bar["o"] * (1.0 + d * slip)
                    atr = atrs[pending[2]] or px * 0.002
                    pos = {"dir": d, "entry_i": i, "entry_dt": bar["dt"], "entry_px": px,
                           "entry_owner": owners[i], "atr": atr, "hold": 0, "block": 0,
                           "stop": px - d * stop_atr * atr,
                           "target": px + d * target_atr * atr, "score": pending[3]}
                    pending = None
                    # 入场当根不做止损/止盈（vnpy约定）；但若该根已是本交易日最后一根，
                    # 日内模式必须立即按收盘价强平，不能把刚开的仓带到下一交易日。
                    last_owner = (i == n - 1) or (owners[i + 1] != owners[i])
                    if flat_eod and last_owner:
                        reason = "样本末强平" if i == n - 1 else "日终强平"
                        close_trade(i, bar["c"] * (1.0 - d * slip), reason)
                    continue
                pending = None
            else:  # exit：下一根开盘离场，若离场方向被锁板则走不了、继续持有
                exit_reason = pending[1]
                d = pos["dir"]
                is_locked = use_limit and locked_at(bar, bases[i], limit_move, limit_eps, d <= 0)
                pending = None
                if is_locked:
                    blocked_exit += 1
                    pos["block"] += 1
                else:
                    px = bar["o"] * (1.0 - d * slip)
                    close_trade(i, px, exit_reason)
                    continue

        # 2) 持仓管理
        if pos is not None:
            pos["hold"] += 1
            d = pos["dir"]
            xpx, reason = None, None
            # 2a) 预埋止损/止盈单：止损优先（同根双触保守按止损），跳空越过以开盘成交
            if d > 0:
                if bar["o"] <= pos["stop"]:
                    xpx, reason = bar["o"] * (1.0 - slip), "止损(跳空)"
                elif bar["l"] <= pos["stop"]:
                    xpx, reason = pos["stop"] * (1.0 - slip), "止损"
                elif bar["o"] >= pos["target"]:
                    xpx, reason = bar["o"] * (1.0 + slip), "止盈(跳空)"
                elif bar["h"] >= pos["target"]:
                    xpx, reason = pos["target"] * (1.0 + slip), "止盈"
            else:
                if bar["o"] >= pos["stop"]:
                    xpx, reason = bar["o"] * (1.0 + slip), "止损(跳空)"
                elif bar["h"] >= pos["stop"]:
                    xpx, reason = pos["stop"] * (1.0 + slip), "止损"
                elif bar["o"] <= pos["target"]:
                    xpx, reason = bar["o"] * (1.0 - slip), "止盈(跳空)"
                elif bar["l"] <= pos["target"]:
                    xpx, reason = pos["target"] * (1.0 - slip), "止盈"
            if xpx is not None:
                is_locked = use_limit and locked_at(bar, bases[i], limit_move, limit_eps, d <= 0)
                if not is_locked:
                    close_trade(i, xpx, reason)
                    continue
                blocked_exit += 1
                pos["block"] += 1
            # 2b) 交易日最后一根：日内模式收盘强平（不隔夜）；样本末无论如何强平
            last_in_owner = (i == n - 1) or (owners[i + 1] != owners[i])
            if (flat_eod and last_in_owner) or i == n - 1:
                xpx = bar["c"] * (1.0 - d * slip)
                reason = "样本末强平" if i == n - 1 else "日终强平"
                is_locked = use_limit and locked_at(bar, bases[i], limit_move, limit_eps, d <= 0)
                if (not is_locked) or i == n - 1:
                    close_trade(i, xpx, reason)
                    continue
                blocked_exit += 1
                pos["block"] += 1
            # 2c) 反向信号 / 摆动模式到期：下一根开盘离场
            sig = _sig_dir(scores[i], entry_th)
            if sig == -d:
                pending = ("exit", "反向信号")
                continue
            if (not flat_eod) and pos["hold"] >= max_bars:
                pending = ("exit", "到期")
                continue
            continue

        # 3) 空仓：本根收盘决策，下一根开盘入场
        if i >= n - 1:
            continue
        sig = _sig_dir(scores[i], entry_th)
        if sig != 0 and atrs[i] is not None and atrs[i] > 0:
            pending = ("entry", sig, i, scores[i])

    if pos is not None:                                          # 兜底平掉残留持仓
        close_trade(n - 1, bars[n - 1]["c"], "样本末强平")
    return trades, blocked_entry, blocked_exit


# ------------------------- 绩效统计 -------------------------
def stats_of(rets):
    rets = [r for r in rets if r is not None and math.isfinite(r)]
    if not rets:
        return None
    n = len(rets)
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    avg = sum(rets) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    equity = peak = 1.0
    max_dd = 0.0
    for r in rets:
        equity *= 1.0 + r
        peak = max(peak, equity)
        max_dd = max(max_dd, 1.0 - equity / peak)
    std = statistics.stdev(rets) if n >= 2 else 0.0
    return {"n": n, "win_rate": len(wins) / n, "avg": avg,
            "avg_win": avg_win, "avg_loss": avg_loss,
            "pl_ratio": (avg_win / abs(avg_loss)) if avg_loss < 0 else None,
            "cumulative": equity - 1.0, "max_dd": max_dd, "std": std}


def ann_sharpe(stat, bars_total, trade_days, avg_hold_bars):
    """按交易笔数口径年化夏普：年bar数/平均持仓bar ≈ 年交易笔数（非重叠）。"""
    if not stat or stat["std"] <= 1e-12 or trade_days <= 0 or avg_hold_bars <= 0:
        return 0.0
    bars_per_year = bars_total / trade_days * 243.0
    tpy = bars_per_year / avg_hold_bars
    return stat["avg"] / stat["std"] * math.sqrt(max(tpy, 1.0))


def _pct(x, d=3):
    return "--" if x is None else f"{x * 100:.{d}f}%"


# ------------------------- 单品种任务 -------------------------
def run_symbol(item, args, fee_table):
    sym, code, name = item
    db = storage.MonitorDB()
    try:
        raw, src = load_minute_bars(db, sym, args.period, args.lookback, args.aggregate_from)
    finally:
        db.close()
    if len(raw) < config.INTRADAY_BT_WARMUP + 5:
        return sym, None, f"分钟bar不足({len(raw)}根)"
    bars, roll_count = ratio_adjusted_bars(raw)
    prepared = prepare_series(bars, args.sig_window)
    owners, bases = build_owner_meta(bars)
    fee_row = fee_table.get(sym) if args.real_fees else None
    move = args.limit_move if args.limit_move is not None else config.FUTURES_LIMIT_MOVE.get(
        sym, config.INTRADAY_BT_LIMIT_MOVE)
    trades, be, bx = simulate(
        sym, bars, prepared, owners, bases, args.entry, args.stop_atr, args.target_atr,
        args.flat_eod, args.max_bars, args.slip_rate,
        fee_row, args.real_fees, args.fee_rate,
        args.use_limit, move, config.INTRADAY_BT_LIMIT_TICK_EPS)
    # 参数稳定性网格（信号序列复用，只重跑状态机）
    stability = []
    if not args.no_stable:
        for e in config.INTRADAY_BT_STABLE_ENTRIES:
            for s in config.INTRADAY_BT_STABLE_STOPS:
                for t in config.INTRADAY_BT_STABLE_TARGETS:
                    tt, _, _ = simulate(
                        sym, bars, prepared, owners, bases, e, s, t,
                        args.flat_eod, args.max_bars, args.slip_rate,
                        fee_row, args.real_fees, args.fee_rate,
                        args.use_limit, move, config.INTRADAY_BT_LIMIT_TICK_EPS)
                    st = stats_of([x["net"] for x in tt])
                    stability.append({"entry": e, "stop": s, "target": t,
                                      "n": st["n"] if st else 0,
                                      "wr": st["win_rate"] if st else None,
                                      "avg": st["avg"] if st else None,
                                      "cum": st["cumulative"] if st else None})
    net_stat = stats_of([t["net"] for t in trades])
    gross_stat = stats_of([t["gross"] for t in trades])
    days = len(set(owners))
    avg_hold = (statistics.mean([t["hold_bars"] for t in trades]) if trades else 0.0)
    result = {"sym": sym, "code": code, "name": name, "src": src, "bars": len(bars),
              "days": days, "roll_count": roll_count, "trades": trades,
              "net": net_stat, "gross": gross_stat,
              "sharpe": ann_sharpe(net_stat, len(bars), days, avg_hold) if avg_hold else 0.0,
              "blocked_entry": be, "blocked_exit": bx,
              "first": bars[0]["dt"].strftime("%Y-%m-%d %H:%M"),
              "last": bars[-1]["dt"].strftime("%Y-%m-%d %H:%M"),
              "limit_move": move, "stability": stability, "avg_hold_bars": avg_hold}
    return sym, result, None


# ------------------------- 报告 -------------------------
def _group_stats(trades, pred):
    sub = [t for t in trades if pred(t)]
    return sub, stats_of([t["net"] for t in sub])


def build_report(results, errors, args):
    L = []
    L.append("=" * 112)
    mode = "日内模式(当日强平/不隔夜/平仓走平今)" if args.flat_eod else "摆动模式(允许跨交易日/同日开平走平今)"
    span = ""
    if results:
        spans = sorted(r["first"] for r in results)
        spans2 = sorted(r["last"] for r in results)
        span = f"{spans[0]} ~ {spans2[-1]}"
    L.append(f" 日内/平今回测报告（第15轮 WP-D1/D2）  生成于 {now_str()}")
    L.append("=" * 112)
    L.append(f" 回放周期: {args.period}分钟  |  模式: {mode}  |  数据窗口: {span or '—'}")
    L.append(f" 信号: 分钟三周期共振+波动标准化动量，入场阈值±{args.entry}；"
             f"离场: {args.stop_atr}×ATR止损 / {args.target_atr}×ATR止盈 / 反向信号"
             + ("" if args.flat_eod else f" / 最长{args.max_bars}根bar"))
    cost_txt = "零成本(无费无滑点)" if args.no_cost else (
        f"真实券商投机费率表+单边滑点{args.slip_rate*1e4:.1f}‱(万{args.slip_rate*1e4:.1f})")
    L.append(f" 成本: {cost_txt}  |  锁板过滤: {'关闭' if not args.use_limit else '开启(前收×常态涨跌停,整根封死才拦截)'}  |  "
             f"信号窗口{args.sig_window}根/预热{config.INTRADAY_BT_WARMUP}根")
    L.append("")

    ok = [r for r in results if r and r["trades"]]
    no_trade = [r for r in results if r and not r["trades"]]
    all_trades = [t for r in ok for t in r["trades"]]
    # ---- 全市场聚合 ----
    agg_net = stats_of([t["net"] for t in all_trades])
    agg_gross = stats_of([t["gross"] for t in all_trades])
    L.append("【一、全品种汇总】（净=毛-开/平手续费率-双边滑点；累计=逐笔顺序复利）")
    L.append(" " + _t("品种", 8) + _t("名称", 10) + _t("bar数", 7) + _t("交易日", 7)
             + _t("交易数", 7) + _t("胜率", 8) + _t("均毛", 9) + _t("均费", 9)
             + _t("均净", 9) + _t("累计净", 10) + _t("笔夏普年", 9) + _t("今/昨", 8)
             + _t("锁板拦", 7))
    for r in sorted(results, key=lambda x: -(x["net"]["avg"] if x["net"] else -9)):
        if not r["net"]:
            L.append(" " + _t(r["sym"], 8) + _t(r["name"], 10) + _t(str(r["bars"]), 7)
                     + _t(str(r["days"]), 7) + _t("0", 7) + "无成交样本")
            continue
        today_n = sum(1 for t in r["trades"] if t["leg"] == "平今")
        fee_avg = r["gross"]["avg"] - r["net"]["avg"]
        L.append(" " + _t(r["sym"], 8) + _t(r["name"], 10) + _t(str(r["bars"]), 7)
                 + _t(str(r["days"]), 7) + _t(str(r["net"]["n"]), 7)
                 + _t(f"{r['net']['win_rate']*100:.1f}%", 8)
                 + _t(_pct(r["gross"]["avg"]), 9) + _t(_pct(fee_avg), 9)
                 + _t(_pct(r["net"]["avg"]), 9) + _t(_pct(r["net"]["cumulative"]), 10)
                 + _t(f"{r['sharpe']:.2f}", 9)
                 + _t(f"{today_n}/{r['net']['n']-today_n}", 8)
                 + _t(str(r["blocked_entry"] + r["blocked_exit"]), 7))
    L.append("")
    if agg_net:
        L.append(" 全市场等权拼接：毛均收 %s，净均收 %s，净胜率 %.1f%%，净累计 %s，净最大回撤 %s；"
                 "盈亏比 %s；无成交品种 %d 个；错误 %d 个"
                 % (_pct(agg_gross["avg"]), _pct(agg_net["avg"]), agg_net["win_rate"] * 100,
                    _pct(agg_net["cumulative"]), _pct(agg_net["max_dd"]),
                    ("--" if agg_net["pl_ratio"] is None else f"{agg_net['pl_ratio']:.2f}"),
                    len(no_trade), len(errors)))
    L.append("")

    # ---- 多空分组 ----
    L.append("【二、方向/平仓路径分组】")
    for label, pred in (("多头", lambda t: t["dir"] == "多"),
                        ("空头", lambda t: t["dir"] == "空"),
                        ("平今(同日开平)", lambda t: t["leg"] == "平今"),
                        ("平昨(跨交易日)", lambda t: t["leg"] == "平昨")):
        sub, st = _group_stats(all_trades, pred)
        if st:
            L.append(" " + _t(label, 16) + _t(f"样本{st['n']}", 9)
                     + _t(f"胜率{st['win_rate']*100:.1f}%", 10) + _t(f"均净{_pct(st['avg'])}", 11)
                     + _t(f"累计{_pct(st['cumulative'])}", 11))
        else:
            L.append(" " + _t(label, 16) + "无样本")
    # 平今费用对照（人民币）
    today_trades = [t for t in all_trades if t["leg"] == "平今"]
    if today_trades and any(t["multiplier"] > 0 for t in today_trades):
        real_close = sum(t["fee_close_yuan"] for t in today_trades)
        hypo_close = sum(t["hypo_close_yuan"] for t in today_trades)
        save = sum(t["today_save_yuan"] for t in today_trades)
        L.append(" 平今路径费用对照（每手人民币，全部平今单合计）：实际平仓费 %.1f 元；"
                 "若按平昨费率将为 %.1f 元；平今优惠(>0为节省/<0为加收)合计 %.1f 元"
                 % (real_close, hypo_close, save))
    yuan_pnl = [t["pnl_yuan"] for t in all_trades if t["pnl_yuan"] is not None]
    if yuan_pnl:
        L.append(" 每手人民币盈亏：合计 %.1f 元/手、单笔均值 %.1f 元/手（乘数取自真实费率表 multiplier）"
                 % (sum(yuan_pnl), statistics.mean(yuan_pnl)))
    L.append("")

    # ---- 退出原因 ----
    L.append("【三、离场原因分布】")
    reasons = {}
    for t in all_trades:
        reasons.setdefault(t["reason"], []).append(t)
    for reason, sub in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
        st = stats_of([t["net"] for t in sub])
        L.append(" " + _t(reason, 16) + _t(f"{len(sub)}笔", 8)
                 + (_t(f"胜率{st['win_rate']*100:.1f}%", 10) + _t(f"均净{_pct(st['avg'])}", 11)
                    if st else ""))
    be = sum(r["blocked_entry"] for r in results)
    bx = sum(r["blocked_exit"] for r in results)
    L.append(f" 锁板/零量拦截：入场放弃 {be} 次，离场顺延 {bx} 次（保守假设，实盘以盘口为准）")
    L.append("")

    # ---- 参数稳定性 ----
    if not args.no_stable:
        L.append("【四、参数稳定性】（入场阈值×止损ATR×止盈ATR；全市场交易等权拼接，检验主参数是否孤峰）")
        grid = {}
        for r in results:
            for cell in r["stability"]:
                k = (cell["entry"], cell["stop"], cell["target"])
                grid.setdefault(k, []).append(cell)
        L.append(" " + _t("入场", 6) + _t("止损×ATR", 9) + _t("止盈×ATR", 9)
                 + _t("总交易", 8) + _t("胜率", 8) + _t("均净", 10) + _t("累计净", 10))
        rows = []
        for k, cells in grid.items():
            rets = [c["avg"] for c in cells if c["avg"] is not None]
            n_total = sum(c["n"] for c in cells)
            wins = sum((c["wr"] or 0) * c["n"] for c in cells)
            wr = wins / n_total if n_total else 0.0
            avg = statistics.mean(rets) if rets else None
            cum = 1.0
            for c in cells:
                if c["cum"] is not None:
                    cum *= 1.0 + c["cum"]
            rows.append((k, n_total, wr, avg, cum - 1.0))
        for (e, s, t), n_total, wr, avg, cum in sorted(rows, key=lambda x: -(x[3] or -9)):
            mark = "  <=主参数" if (abs(e - args.entry) < 1e-9 and abs(s - args.stop_atr) < 1e-9
                                   and abs(t - args.target_atr) < 1e-9) else ""
            L.append(" " + _t(str(e), 6) + _t(str(s), 9) + _t(str(t), 9)
                     + _t(str(n_total), 8) + _t(f"{wr*100:.1f}%", 8)
                     + _t(_pct(avg), 10) + _t(_pct(cum), 10) + mark)
        L.append("")

    L.append("-" * 112)
    L.append(" 数据与口径：" + DISCLAIMER)
    if errors:
        L.append(" 读取/计算失败品种：" + "；".join(f"{s}({e})" for s, e in errors))
    L.append("=" * 112)
    return "\n".join(L)


def _t(x, w):
    s = str(x)
    return s + " " * max(1, w - sum(2 if ord(ch) > 127 else 1 for ch in s))


CSV_FIELDS = ["sym", "name", "contract", "dir", "entry_dt", "exit_dt", "entry_owner",
              "exit_owner", "leg", "hold_bars", "hold_min", "entry_px", "exit_px",
              "gross", "open_fee_rate", "close_fee_rate", "slip_rate", "net",
              "fee_open_yuan", "fee_close_yuan", "hypo_close_yuan", "today_save_yuan",
              "pnl_yuan", "multiplier", "fee_mode", "entry_score", "blocked_exits", "reason"]


def write_trades_csv(results, path):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, lineterminator="\n")
        w.writeheader()
        for r in sorted(results, key=lambda x: x["sym"]):
            for t in r["trades"]:
                row = dict(t)
                row["name"] = r["name"]
                w.writerow(row)


# ------------------------- CLI -------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="分钟K线日内/平今回测（自采minute_bars库驱动）")
    p.add_argument("--codes", default="", help="品种代码逗号分隔，如 RB,MA 或 RB0；留空且无--all时默认重点品种")
    p.add_argument("--all", action="store_true", help="全64品种")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--period", type=int, default=config.INTRADAY_BT_PERIOD, choices=(1, 5, 15, 30, 60))
    p.add_argument("--aggregate-from", type=int, default=0, choices=(0, 1, 5, 15, 30),
                   help="从更细分钟周期现场聚合到--period（如 --period 5 --aggregate-from 1）")
    p.add_argument("--lookback", type=int, default=config.INTRADAY_BT_LOOKBACK)
    p.add_argument("--sig-window", type=int, default=config.INTRADAY_BT_SIG_WINDOW)
    p.add_argument("--entry", type=float, default=config.INTRADAY_BT_ENTRY)
    p.add_argument("--stop-atr", type=float, default=config.INTRADAY_BT_STOP_ATR)
    p.add_argument("--target-atr", type=float, default=config.INTRADAY_BT_TARGET_ATR)
    p.add_argument("--max-bars", type=int, default=config.INTRADAY_BT_MAX_BARS)
    p.add_argument("--swing", action="store_true", help="摆动模式：允许跨交易日持仓（默认日内强平）")
    p.add_argument("--fee-rate", type=float, default=config.INTRADAY_BT_FEE_RATE)
    p.add_argument("--slip-rate", type=float, default=config.INTRADAY_BT_SLIP_RATE)
    p.add_argument("--fees-file", default=config.FUTURES_FEES_FILE)
    p.add_argument("--no-real-fees", action="store_true")
    p.add_argument("--no-cost", action="store_true", help="零手续费零滑点（看纯信号毛收益）")
    p.add_argument("--limit-move", type=float, default=None, help="全局覆盖涨跌停幅度，如0.07")
    p.add_argument("--no-limit-filter", action="store_true", help="关闭锁板过滤")
    p.add_argument("--no-stable", action="store_true", help="跳过参数稳定性网格")
    p.add_argument("--workers", type=int, default=6)
    args = p.parse_args(argv)
    args.flat_eod = not args.swing
    args.use_limit = not args.no_limit_filter
    args.real_fees = not args.no_real_fees
    return args


def main(argv=None):
    args = parse_args(argv)
    fee_table = load_fee_schedule(args.fees_file) if args.real_fees else {}
    if args.no_cost:
        args.fee_rate, args.slip_rate = 0.0, 0.0
        args.real_fees = False
    # 不给 --codes 时默认全品种；支持 RB/RB0/中文名
    items = resolve_items(args.codes, args.limit)
    print(f"日内/平今回测：{len(items)}个品种，{args.period}分钟，"
          f"{'日内' if args.flat_eod else '摆动'}模式，真实费率{len(fee_table)}个品种")

    results, errors = [], []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_symbol, item, args, fee_table): item[0] for item in items}
        for fut in as_completed(futs):
            sym, result, err = fut.result()
            if err:
                errors.append((sym, err))
                print(f"  [跳过] {sym}: {err}")
            else:
                results.append(result)
                ntr = len(result["trades"])
                print(f"  [完成] {sym} {result['name']}: {result['bars']}根bar/"
                      f"{result['days']}个交易日, {ntr}笔交易, 换月修正{result['roll_count']}处")
    results.sort(key=lambda r: r["sym"])

    report = build_report(results, errors, args)
    os.makedirs(os.path.dirname(config.INTRADAY_BT_REPORT_FILE), exist_ok=True)
    with open(config.INTRADAY_BT_REPORT_FILE, "w", encoding="utf-8-sig") as f:
        f.write(report)
    write_trades_csv(results, config.INTRADAY_BT_TRADES_FILE)
    print("\n" + report[:3000])
    print(f"\n报告已写入: {config.INTRADAY_BT_REPORT_FILE}")
    print(f"逐笔交易CSV: {config.INTRADAY_BT_TRADES_FILE}")
    return results, errors


if __name__ == "__main__":
    main()
