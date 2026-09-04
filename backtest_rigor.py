# -*- coding: utf-8 -*-
"""
G4 续（第62轮）回测严谨性补强：滚动 walk-forward 样本外 + 对照基准（纯标准库、零网络）。

本模块只放"可独立单测的纯函数/纯过程"，不 import backtest（避免循环依赖）：
  - slice_prepared：把 prepare_symbol 的全局序列切出 [a,b) 窗，并把 series 的全局 i
        重映射为窗内局部下标；指标 ind/score 沿用全局【因果】计算结果（只用 a 之前的历史，
        不含任何未来信息），因此切窗不引入未来函数。
  - buy_hold_*：同区间"一直买入持有主连"的对照基准收益（主连已比例复权）。
  - wf_folds / select_best_param / walk_forward_symbol：滚动前推——每折只在【前一段 IS】
        的参数网格里选净均收最好的 (持有日,入场阈值)，再用到【紧接着、互不重叠的 OOS 段】，
        逐折向后滚动，拼接出一条"选参从未用到自身未来"的纯样本外交易轨迹。

设计铁律：默认（不开 --walk-forward、不开基准）时 backtest.py 既有口径与结果逐值不变。
"""
import math

try:
    import config
    _WARMUP = getattr(config, "BACKTEST_WARMUP_BARS", 60)
    _TRAIN = getattr(config, "BACKTEST_WF_TRAIN_BARS", 120)
    _TEST = getattr(config, "BACKTEST_WF_TEST_BARS", 40)
    _MIN_IS = getattr(config, "BACKTEST_WF_MIN_IS_TRADES", 3)
except Exception:  # pragma: no cover
    _WARMUP, _TRAIN, _TEST, _MIN_IS = 60, 120, 40, 3


# ----------------------------- 切窗（无未来函数） -----------------------------

def slice_prepared(prepared, a, b):
    """取 prepared 全局 bar 下标半开区间 [a,b) 的子窗，series 索引重映射为窗内局部。"""
    n = len(prepared["closes"])
    a = max(0, int(a))
    b = min(n, int(b))
    sub = dict(prepared)
    sub["bars"] = prepared["bars"][a:b]
    for key in ("closes", "opens", "highs", "lows"):
        sub[key] = prepared[key][a:b]
    sub["series"] = [{"i": it["i"] - a, "ind": it["ind"], "score": it["score"]}
                     for it in prepared["series"] if a <= it["i"] < b]
    return sub


# ----------------------------- 对照基准：买入持有 -----------------------------

def _first_valid(closes, start):
    for i in range(max(0, start), len(closes)):
        try:
            if closes[i] and float(closes[i]) > 0:
                return i
        except (TypeError, ValueError):
            continue
    return None


def buy_hold_window(closes, a=0, b=None):
    """区间 [a,b) 内一直买入持有的收益率 = 末根收盘/区间首个有效收盘 - 1；无法计算返回 None。"""
    closes = list(closes)
    if not closes:
        return None
    b = len(closes) if b is None else min(int(b), len(closes))
    i0 = _first_valid(closes, a)
    if i0 is None or b - 1 < i0:
        return None
    p0, p1 = float(closes[i0]), float(closes[b - 1])
    if p0 <= 0 or p1 <= 0:
        return None
    return p1 / p0 - 1.0


def benchmark_for_prepared(prepared, warmup=_WARMUP):
    """单品种基准：从指标预热结束（与策略最早可交易日一致）到样本末的买入持有收益。"""
    return buy_hold_window(prepared["closes"], warmup, len(prepared["closes"]))


def pooled_buy_hold(values):
    """等权一篮子基准：各品种买入持有收益的算术平均（每个品种等资金）；全空返回 None。"""
    vals = [v for v in values if v is not None and math.isfinite(v)]
    return sum(vals) / len(vals) if vals else None


def excess(strategy_cum, benchmark_cum):
    """超额（算术差）：策略累计 - 基准累计；任一缺失返回 None。"""
    if strategy_cum is None or benchmark_cum is None:
        return None
    if not (math.isfinite(strategy_cum) and math.isfinite(benchmark_cum)):
        return None
    return strategy_cum - benchmark_cum


def beat_benchmark_pairs(pairs):
    """输入 [(code, 策略累计, 基准累计), ...]，对齐有效项后返回
    (跑赢基准数, 有效对数, [(code, strat, bh, excess), ...])；缺任一值的品种跳过。"""
    rows, beat = [], 0
    for code, sc, bh in pairs:
        e = excess(sc, bh)
        if e is None:
            continue
        if e > 1e-12:
            beat += 1
        rows.append((code, sc, bh, e))
    return beat, len(rows), rows


# ----------------------------- 滚动 walk-forward -----------------------------

def wf_folds(n_bars, warmup, train_bars, test_bars):
    """生成 (is_a,is_b,oos_a,oos_b)：首折 OOS 起点=warmup+train_bars，
    之后每折整体向后推进 test_bars，相邻折 OOS 互不重叠；OOS 不足 2 根的尾折丢弃。"""
    folds = []
    oos_a = int(warmup) + int(train_bars)
    test_bars = int(test_bars)
    while oos_a < n_bars:
        oos_b = min(n_bars, oos_a + test_bars)
        is_a = oos_a - int(train_bars)
        is_b = oos_a
        if oos_b - oos_a >= 2:
            folds.append((is_a, is_b, oos_a, oos_b))
        oos_a += test_bars
    return folds


def select_best_param(sub_is, grid, simulator, min_is_trades):
    """只在 IS 子窗内按"净均收最高"选 (hold,entry)；交易数不足 min_is_trades 的参数不参选；
    并列时保留网格中先出现者（严格大于），保证确定性。返回 (chosen, is_n, is_avg, candidates)。"""
    best, best_avg, best_n, candidates = None, None, 0, []
    for hold, entry in grid:
        rr = simulator(sub_is, hold, entry)
        m = rr.get("trade_metrics")
        n = m["n"] if m else 0
        avg = m["avg"] if m else None
        candidates.append((hold, entry, n, avg))
        if not m or n < min_is_trades or not math.isfinite(avg):
            continue
        if best is None or avg > best_avg + 1e-15:
            best, best_avg, best_n = (hold, entry), avg, n
    return best, best_n, best_avg, candidates


def _bar_date(bars, idx, default=""):
    if 0 <= idx < len(bars):
        return bars[idx].get("d", default) or default
    return default


def walk_forward_symbol(prepared, simulator, grid, default_param,
                        train_bars=_TRAIN, test_bars=_TEST, min_is_trades=_MIN_IS,
                        warmup=_WARMUP):
    """单品种滚动 walk-forward。

    simulator(sub_prepared, hold, entry) -> result dict（含 trades/trade_metrics），由 backtest
    注入（闭包绑定品种名与成本参数），本模块不直接依赖 backtest，便于用假模拟器做确定性单测。
    default_param=(hold,entry)：当某折 IS 全部参数样本不足时的兜底参数（并标 fallback=True）。

    返回 {"oos_trades":[...带 wf_fold/wf_hold/wf_entry 标注...], "folds":[每折选参与表现]}。
    拼接出的每一笔 OOS 交易，其选参都只用到它之前那段 IS——严格样本外。
    """
    closes = prepared["closes"]
    bars = prepared["bars"]
    oos_trades, folds_out = [], []
    for k, (ia, ib, oa, ob) in enumerate(wf_folds(len(closes), warmup, train_bars, test_bars)):
        sub_is = slice_prepared(prepared, ia, ib)
        chosen, is_n, is_avg, _ = select_best_param(sub_is, grid, simulator, min_is_trades)
        fallback = chosen is None
        if fallback:
            chosen = tuple(default_param)
        hold, entry = chosen
        sub_oos = slice_prepared(prepared, oa, ob)
        rr = simulator(sub_oos, hold, entry)
        trades = rr.get("trades", [])
        oos_rets = [t["ret"] for t in trades]
        oos_avg = sum(oos_rets) / len(oos_rets) if oos_rets else None
        for t in trades:
            t["wf_fold"] = k
            t["wf_hold"] = hold
            t["wf_entry"] = entry
        oos_trades.extend(trades)
        folds_out.append({
            "fold": k,
            "is_start": _bar_date(bars, ia), "is_end": _bar_date(bars, ib - 1),
            "oos_start": _bar_date(bars, oa), "oos_end": _bar_date(bars, ob - 1),
            "hold": hold, "entry": entry, "fallback": fallback,
            "is_n": is_n, "is_avg": is_avg,
            "oos_n": len(trades), "oos_avg": oos_avg})
    return {"oos_trades": oos_trades, "folds": folds_out}


def param_usage(folds):
    """统计各折选中的 (hold,entry) 出现次数（含兜底），返回 {f'{hold}d/{entry}': n}。"""
    usage = {}
    for f in folds:
        key = "%gd/%.1f" % (f["hold"], f["entry"])
        usage[key] = usage.get(key, 0) + 1
    return dict(sorted(usage.items(), key=lambda kv: (-kv[1], kv[0])))


def is_vs_oos_avg(folds):
    """折级 IS 均收均值 vs OOS 均收均值（只统计两侧都有交易的折），用于看样本外衰减。"""
    is_v, oos_v = [], []
    for f in folds:
        if f["is_avg"] is not None and f["oos_avg"] is not None and math.isfinite(f["is_avg"]) \
                and math.isfinite(f["oos_avg"]):
            is_v.append(f["is_avg"])
            oos_v.append(f["oos_avg"])
    if not is_v:
        return None, None
    return sum(is_v) / len(is_v), sum(oos_v) / len(oos_v)
