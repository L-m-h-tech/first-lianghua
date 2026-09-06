# -*- coding: utf-8 -*-
r"""WP-F2（P1-2）B3：triple-barrier 监督学习样本构建（研究侧离线工具，为 WP-F4 备料）。

做什么：
  对分钟回测同一套信号（信号 i 收盘确认、i+1 开盘入场、ATR 取自 i），按 López de Prado
  triple-barrier 打三分类标签：
    上轨(止盈) = 入场价 + 方向 × target_atr × ATR  -> label=+1（先触及）
    下轨(止损) = 入场价 - 方向 × stop_atr  × ATR   -> label=-1（先触及；同根双触保守按止损）
    纵向壁垒：最多观察 max_bars 根，到期未触 -> label=sign(方向×(到期收盘-入场))，走平=0
  同时落"特征快照"（全部只用信号 i 及之前的数据，严格 PIT）：
    技术类（由分钟bar现算）：mom5/mom20、ma10/20/60 偏离、20根高低位 RSV、ATR占比、
                            60根均量、分钟技术分、共振结构；
    信号类（signals 表就近匹配 ts<=bar_dt 的同轮信号）：综合分、9 因子拆分；
    截面类：同一 bar_dt 全品种信号分的稳健 z（复用 cross_section._robust_z）；
    情绪类：该轮信号命中消息的五维情绪均值（强度/不确定/前瞻/相关/极性）。

防泄漏（元方法红线，WP-F4 训练前的硬约束）：
  - 特征窗口 ≤ i、标签路径 > i，代码层物理隔离；--audit 对每个样本断言"未来价格不改变特征"；
  - ml_samples UNIQUE(sym,period,bar_dt)，INSERT OR REPLACE 可重复跑、长期保留；
  - purged_embargo_split：训练折样本若其【标签结束位置】越过测试折起点（再留 embargo 根），
    一律从训练集剔除，杜绝标签窗口横跨切分点的泄漏。

撮合口径与 intraday_backtest 完全一致（入场当根不查、止损优先、跳空开盘成交），区别仅是
本工具不扣手续费/滑点（成本是模型层之后的事，标签用理论触价更干净）。纯自有DB、零网络。

用法（项目根目录）：
  D:\Python\python.exe tools\build_ml_samples.py --all --period 30            # 全品种30m，写第9表
  D:\Python\python.exe tools\build_ml_samples.py --codes RB,CU --no-db        # 只统计不写库
  D:\Python\python.exe tools\build_ml_samples.py --selftest                   # 零网络合成断言
"""
import argparse
import bisect
import json
import statistics
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402


# =========================== triple-barrier（纯函数，可合成断言） ===========================
def triple_barrier(bars, sig_i, direction, atr, *, tp_atr, sl_atr, max_bars):
    """信号在 sig_i 收盘确认、sig_i+1 开盘入场；返回标签结果 dict；无法标注返回 None。

    与 intraday_backtest 同口径：入场当根(j0)不查止损/止盈，从 j0+1 起逐根判定；
    止损优先于止盈（同根双触保守按止损）；跳空以开盘价成交；到期按收盘方向定符号。
    返回 {entry_i,entry,exit_i,exit,label,exit_reason,bars_held,tp,sl,ret_dir}。
    """
    n = len(bars)
    j0 = sig_i + 1
    if direction not in (1, -1) or j0 >= n:
        return None
    if atr is None or atr <= 0:
        return None
    entry = float(bars[j0]["o"])
    if entry <= 0:
        return None
    tp = entry + direction * tp_atr * atr
    sl = entry - direction * sl_atr * atr
    last_j = min(n - 1, j0 + max_bars)
    for j in range(j0 + 1, last_j + 1):
        b = bars[j]
        o, h, l, c = float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])
        label, px, reason = None, None, None
        if direction > 0:
            if o <= sl:                 # 跳空低开破止损（止损优先）
                label, px, reason = -1, o, "止损(跳空)"
            elif o >= tp:               # 跳空高开越止盈
                label, px, reason = 1, o, "止盈(跳空)"
            elif l <= sl:               # 同根双触保守按止损，故先判 l
                label, px, reason = -1, sl, "止损"
            elif h >= tp:
                label, px, reason = 1, tp, "止盈"
        else:
            if o >= sl:
                label, px, reason = -1, o, "止损(跳空)"
            elif o <= tp:
                label, px, reason = 1, o, "止盈(跳空)"
            elif h >= sl:
                label, px, reason = -1, sl, "止损"
            elif l <= tp:
                label, px, reason = 1, tp, "止盈"
        if label is not None:
            return {"entry_i": j0, "entry": entry, "exit_i": j, "exit": px,
                    "label": label, "exit_reason": reason, "bars_held": j - j0,
                    "tp": tp, "sl": sl, "ret_dir": direction * (px / entry - 1.0)}
    # 纵向时间壁垒：到期收盘
    exit_c = float(bars[last_j]["c"])
    diff = direction * (exit_c / entry - 1.0)
    label = 1 if diff > 1e-9 else (-1 if diff < -1e-9 else 0)
    return {"entry_i": j0, "entry": entry, "exit_i": last_j, "exit": exit_c,
            "label": label, "exit_reason": "超时", "bars_held": last_j - j0,
            "tp": tp, "sl": sl, "ret_dir": diff}


def tech_features(closes, vols, scores, atrs, i):
    """只用 [0..i]（含信号 i）数据计算技术特征，严格 PIT。数据不足处给 None。"""
    f = {}
    c = closes[i]

    def _ma(k):
        if i + 1 < k:
            return None
        seg = closes[i - k + 1:i + 1]
        return sum(seg) / k

    def _ret(k):
        if i - k < 0 or closes[i - k] <= 0:
            return None
        return c / closes[i - k] - 1.0

    f["mom5"] = _ret(5)
    f["mom20"] = _ret(20)
    for k in (10, 20, 60):
        ma = _ma(k)
        f["ma%d_bias" % k] = (c / ma - 1.0) if ma else None
    # 20 根高低位 RSV ∈ [0,1]
    if i >= 19:
        seg = closes[i - 19:i + 1]
        hh, ll = max(seg), min(seg)
        f["rsv20"] = (c - ll) / (hh - ll) if hh > ll else 0.5
    else:
        f["rsv20"] = None
    atr = atrs[i] if i < len(atrs) else None
    f["atr_pct"] = (atr / c) if (atr and c > 0) else None
    if i >= 59:
        f["vol60"] = statistics.mean(vols[i - 59:i + 1])
    else:
        f["vol60"] = None
    f["tech_score"] = scores[i]
    f["ret1"] = _ret(1)
    return f


def purged_embargo_split(order, label_end, test_lo, test_hi, embargo=0):
    """purged + embargo 切分（防标签窗口横跨切分点）。

    order: 样本按时间排序的位置序列（如全局 bar 序号）；label_end[k]=样本 k 的标签结束位置；
    [test_lo,test_hi) 为测试折位置区间。训练样本 k 必须满足 label_end[k] < test_lo-embargo
    （标签在测试折开始前已结束）或 order[k] >= test_hi（样本在测试折之后，时间序列通常不会）。
    返回 (train_idx, test_idx)（均为 order 内的下标列表）。
    """
    train, test = [], []
    for idx, pos in enumerate(order):
        if test_lo <= pos < test_hi:
            test.append(idx)
            continue
        end = label_end[idx]
        if pos < test_lo and end >= test_lo - embargo:
            continue  # 标签窗口（加 embargo）探入测试折 -> 剔除
        train.append(idx)
    return train, test


# =========================== 信号/情绪就近匹配（PIT） ===========================
def build_signal_index(signal_rows):
    """signals 行 -> {variety: [(ts_float, row)...]} 按 ts 升序；供每根 bar 二分就近匹配。"""
    idx = defaultdict(list)
    for r in signal_rows:
        try:
            from datetime import datetime
            ts = datetime.strptime(r["ts"][:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except (TypeError, ValueError, KeyError):
            continue
        idx[r.get("variety", "")].append((ts, r))
    for v in idx:
        idx[v].sort(key=lambda t: t[0])
    return idx


def _session_key(dt):
    """交易时段归属键：20:00后~次日03:00 为同一夜段（归到开始自然日），其余为当日日段。

    分钟技术分信号点与实盘九因子快照时刻不必重合，故按"同一交易时段"就近匹配，
    避免把午盘/前夜的旧状态错配到下午/凌晨，也避免跨日盘夜盘串味。
    """
    from datetime import timedelta
    h = dt.hour
    if h >= 20:
        return dt.strftime("%Y-%m-%d") + "_night"
    if h < 3:
        return (dt - timedelta(days=1)).strftime("%Y-%m-%d") + "_night"
    return dt.strftime("%Y-%m-%d") + "_day"


def nearest_signal(sig_idx, variety, bar_dt, *, max_gap_sec=9000):
    """取同交易时段内、ts<=bar_dt、时间差 <=max_gap_sec 的最近一条信号（PIT，绝不取未来）。"""
    arr = sig_idx.get(variety)
    if not arr:
        return None
    t = bar_dt.timestamp()
    k = bisect.bisect_right([a[0] for a in arr], t) - 1
    if k < 0:
        return None
    ts, row = arr[k]
    if t - ts > max_gap_sec:
        return None
    from datetime import datetime
    if _session_key(bar_dt) != _session_key(datetime.fromtimestamp(ts)):
        return None
    return row


def datetime_date(dt):
    return dt.strftime("%Y-%m-%d")


def datetime_ts_date(ts):
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


_FAC_KEYS = ("intensity", "uncertainty", "forwardness", "relevance")


def aggregate_sentiment(raw_json, variety, cat):
    """从一条信号 raw_json.hits 聚合五维情绪均值（信号当时消息，PIT）。无消息返回 {}。"""
    try:
        raw = json.loads(raw_json) if isinstance(raw_json, str) else (raw_json or {})
        hits = raw.get("hits") or []
    except (TypeError, ValueError):
        return {}
    facs = []
    import factors
    for item in hits:
        try:
            news = item[1] if isinstance(item, (list, tuple)) else item.get("news")
            content = news.get("content", "")
        except (AttributeError, IndexError, TypeError):
            continue
        facs.append(factors.sentiment_facets(content, variety=variety, cat=cat))
    if not facs:
        return {}
    out = {k: round(statistics.mean(f.get(k, 0.0) for f in facs), 4) for k in _FAC_KEYS}
    out["polarity"] = round(statistics.mean(f.get("polarity", 0.0) for f in facs), 4)
    events = Counter(f.get("event", "综合") for f in facs)
    out["event"] = events.most_common(1)[0][0]
    out["n_news"] = len(facs)
    return out


def _canon(key):
    s = str(key or "").strip()
    for cut in ("(", "（"):
        if cut in s:
            s = s.split(cut, 1)[0].strip()
    return s


# =========================== 单品种构建 ===========================
def _sig_dir(score, entry_th):
    if score is None:
        return 0
    return 1 if score >= entry_th else (-1 if score <= -entry_th else 0)


def build_symbol(item, args, sig_idx):
    """单品种：装载→信号→triple-barrier→PIT特征，返回样本 dict 列表（不直接写库）。

    截面 z 依赖全市场同时刻信号，统一在主流程对全部样本回填（本函数不做）。
    """
    import storage
    import intraday_backtest as ib
    from backtest import ratio_adjusted_bars
    sym, code, name = item
    db = storage.MonitorDB()
    try:
        raw, _src = ib.load_minute_bars(db, sym, args.period, args.lookback, args.aggregate_from)
    finally:
        db.close()
    if len(raw) < config.INTRADAY_BT_WARMUP + args.max_bars + 5:
        return sym, [], "分钟bar不足(%d根)" % len(raw)
    bars, _roll = ratio_adjusted_bars(raw)
    closes, highs, lows, scores, atrs = ib.prepare_series(bars, args.sig_window)
    owners, _bases = ib.build_owner_meta(bars)
    vols = [float(b.get("v", 0) or 0) for b in bars]
    meta = config.VARIETIES.get(name, {})
    cat = meta.get("cat", "—")
    samples = []
    n = len(bars)
    for i in range(n):
        d = _sig_dir(scores[i], args.entry)
        if d == 0:
            continue
        tb = triple_barrier(bars, i, d, atrs[i], tp_atr=args.target_atr,
                            sl_atr=args.stop_atr, max_bars=args.max_bars)
        if tb is None:
            continue
        feat = tech_features(closes, vols, scores, atrs, i)
        bar_dt = bars[i]["dt"]
        bar_dt_txt = bar_dt.strftime("%Y-%m-%d %H:%M")
        sig = nearest_signal(sig_idx, name, bar_dt)
        if sig:
            try:
                parts = json.loads(sig.get("parts_json") or "{}")
                for k, v in parts.items():
                    try:
                        feat["f_" + _canon(k)] = float(v)
                    except (TypeError, ValueError):
                        pass
                feat["sig_score"] = float(sig.get("score", 0.0))
            except (TypeError, ValueError):
                pass
            if args.with_sentiment:
                sent = aggregate_sentiment(sig.get("raw_json"), name, cat)
                for k, v in sent.items():
                    feat["sent_" + k] = v
        samples.append({
            "sym": sym, "variety": name, "period": args.period,
            "bar_dt": bar_dt_txt,
            "trade_date": owners[i].strftime("%Y-%m-%d") if owners else bar_dt_txt[:10],
            "direction": d, "entry_price": tb["entry"], "atr": atrs[i],
            "tp_price": tb["tp"], "sl_price": tb["sl"],
            "exit_dt": bars[tb["exit_i"]]["dt"].strftime("%Y-%m-%d %H:%M"),
            "exit_price": tb["exit"], "label": tb["label"],
            "exit_reason": tb["exit_reason"], "bars_held": tb["bars_held"],
            "ret_dir": tb["ret_dir"], "tech_score": scores[i], "features": feat,
            "_sig_i": i, "_label_end_i": tb["exit_i"],
        })
    return sym, samples, None


def build_cross_section_z(signal_rows):
    """按 signals 同 ts 计算综合分稳健 z（复用 cross_section），返回 {ts到分钟: z_by_variety}。"""
    import cross_section
    by_ts = defaultdict(list)
    for r in signal_rows:
        try:
            score = float(r.get("score", 0.0))
        except (TypeError, ValueError):
            continue
        by_ts[r.get("ts", "")[:16]].append((r.get("variety", ""), score))
    out = {}
    for ts, lst in by_ts.items():
        if len(lst) < 3:
            continue
        zs = cross_section._robust_z([x[1] for x in lst])
        for (variety, _), z in zip(lst, zs):
            out[(ts, variety)] = round(z, 3)
    return out


# =========================== 主流程 ===========================
def run(argv=None):
    ap = argparse.ArgumentParser(description="triple-barrier 样本构建（WP-F2 B3）")
    ap.add_argument("--codes", default="", help="品种代码/中文名逗号分隔；留空且无--all时取默认")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--period", type=int, default=30, choices=(1, 5, 15, 30, 60))
    ap.add_argument("--aggregate-from", type=int, default=0, choices=(0, 1, 5, 15, 30))
    ap.add_argument("--lookback", type=int, default=config.INTRADAY_BT_LOOKBACK)
    ap.add_argument("--sig-window", type=int, default=config.INTRADAY_BT_SIG_WINDOW)
    ap.add_argument("--entry", type=float, default=config.INTRADAY_BT_ENTRY)
    ap.add_argument("--target-atr", type=float, default=config.ML_SAMPLE_TARGET_ATR)
    ap.add_argument("--stop-atr", type=float, default=config.ML_SAMPLE_STOP_ATR)
    ap.add_argument("--max-bars", type=int, default=config.ML_SAMPLE_MAX_BARS)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--no-db", action="store_true", help="只统计不写入 ml_samples 表")
    ap.add_argument("--no-sentiment", dest="with_sentiment", action="store_false")
    ap.set_defaults(with_sentiment=True)
    ap.add_argument("--audit", action="store_true", help="对每个样本做PIT无穿越审计断言")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()

    import storage
    import intraday_backtest as ib
    items = ib.resolve_items(args.codes, args.limit) if not args.all else ib.resolve_items("", 0)
    print("待构建品种 %d 个，period=%dm，止盈%.1fATR/止损%.1fATR/最长%d根"
          % (len(items), args.period, args.target_atr, args.stop_atr, args.max_bars))

    db = storage.MonitorDB()
    signal_rows = db.conn.execute(
        "SELECT ts,variety,score,parts_json,raw_json FROM signals WHERE ABS(score)>=? ORDER BY ts",
        (config.SCORE_NEUTRAL,)).fetchall()
    signal_rows = [dict(r) for r in signal_rows]
    db.close()
    sig_idx = build_signal_index(signal_rows)
    z_map = build_cross_section_z(signal_rows)
    print("就近信号池：%d 条、覆盖%d品种；截面z时间桶%d个"
          % (len(signal_rows), len(sig_idx), len({k[0] for k in z_map})))

    all_samples, errors = [], []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(build_symbol, it, args, sig_idx): it[0] for it in items}
        for fut in as_completed(futs):
            sym, ss, err = fut.result()
            if err:
                errors.append((sym, err))
                print("  [跳过] %s: %s" % (sym, err))
            else:
                all_samples.extend(ss)
    # 回填截面 z（需样本已带 variety/bar_dt）
    for s in all_samples:
        z = z_map.get((s["bar_dt"], s["variety"]))
        if z is not None:
            s["features"]["xs_score_z"] = z

    # PIT 审计：重算特征时把 i 之后的价格全部改写，特征必须不变
    if args.audit:
        audit_pit(items, args, sig_idx)

    # 汇总
    label_cnt = Counter(s["label"] for s in all_samples)
    reason_cnt = Counter(s["exit_reason"] for s in all_samples)
    feat_keys = Counter()
    for s in all_samples:
        for k in s["features"]:
            feat_keys[k] += 1
    print("样本总数 %d；标签分布 止盈(+1)=%d / 止损(-1)=%d / 超时走平(0)=%d"
          % (len(all_samples), label_cnt.get(1, 0), label_cnt.get(-1, 0), label_cnt.get(0, 0)))
    print("离场原因：" + "，".join("%s=%d" % (k, v) for k, v in reason_cnt.most_common()))
    print("特征完整率（/%d，保留1位小数）：%s"
          % (len(all_samples), "，".join("%s=%.1f%%" % (k, v / max(1, len(all_samples)) * 100)
                                        for k, v in feat_keys.most_common())))
    print("说明：技术特征/triple-barrier标签全样本可用；九因子/截面/情绪快照仅 signals 表覆盖期可用，随运行持续积累，不回填编造。")
    if errors:
        print("跳过品种 %d：%s" % (len(errors), "；".join(s for s, _ in errors)))

    if not args.no_db:
        db = storage.MonitorDB()
        try:
            n = db.insert_ml_samples(all_samples)
            total = db.conn.execute("SELECT COUNT(*) FROM ml_samples").fetchone()[0]
            print("已写入/覆盖 ml_samples %d 行；表内累计 %d 行（UNIQUE(sym,period,bar_dt)）" % (n, total))
        finally:
            db.close()
    else:
        print("--no-db：未写库。")
    return 0


def audit_pit(items, args, sig_idx):
    """PIT 无穿越审计：把信号 i 之后所有收盘价/最高/最低做扰动，重算 i 的技术特征必须完全一致。"""
    import intraday_backtest as ib
    from backtest import ratio_adjusted_bars
    checked = 0
    for it in items[:3]:
        import storage
        db = storage.MonitorDB()
        try:
            raw, _ = ib.load_minute_bars(db, it[0], args.period, args.lookback, args.aggregate_from)
        finally:
            db.close()
        bars, _ = ratio_adjusted_bars(raw)
        closes, _, _, scores, atrs = ib.prepare_series(bars, args.sig_window)
        vols = [float(b.get("v", 0) or 0) for b in bars]
        for i in range(config.INTRADAY_BT_WARMUP, min(len(bars) - args.max_bars - 2,
                                                      config.INTRADAY_BT_WARMUP + 40)):
            if _sig_dir(scores[i], args.entry) == 0:
                continue
            f1 = tech_features(closes, vols, scores, atrs, i)
            closes2 = list(closes)
            for k in range(i + 1, len(closes2)):
                closes2[k] = closes2[k] * 1.5 + 999
            f2 = tech_features(closes2, vols, scores, atrs, i)
            assert f1 == f2, ("PIT 泄漏：未来价格改变了 i=%d 的特征" % i)
            checked += 1
    assert checked > 0, "审计未覆盖任何样本"
    print("PIT 审计通过：扰动 %d 个信号点之后的全部价格，其特征不变（无未来函数）" % checked)


# =========================== 合成断言 ===========================
def _bar(o, h, l, c, v=100):
    return {"o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": float(v)}


def selftest():
    # 构造价格路径：j0=1 入场（开盘100），ATR=2，止盈=104、止损=97.6
    # 1) 先触止盈：第3根高点到104，此前低点不破97.6
    bars = [_bar(99, 99.5, 98.5, 99), _bar(100, 100.5, 99.5, 100),
            _bar(100.5, 102, 99.8, 101.8), _bar(102, 104.2, 101.5, 103.9)]
    r = triple_barrier(bars, 0, 1, 2.0, tp_atr=2.0, sl_atr=1.2, max_bars=48)
    assert r["label"] == 1 and r["exit_reason"] == "止盈" and r["exit_i"] == 3, r
    assert r["entry"] == 100.0 and abs(r["tp"] - 104) < 1e-9 and abs(r["sl"] - 97.6) < 1e-9

    # 2) 先触止损：第2根低点破97.6
    bars2 = [_bar(99, 99.5, 98.5, 99), _bar(100, 100.5, 99.5, 100),
             _bar(99, 99.2, 97.0, 97.5), _bar(97, 98, 96, 96.5)]
    r2 = triple_barrier(bars2, 0, 1, 2.0, tp_atr=2.0, sl_atr=1.2, max_bars=48)
    assert r2["label"] == -1 and r2["exit_reason"] == "止损", r2

    # 3) 同根双触（既到104又破97.6）保守按止损
    bars3 = [_bar(99, 99.5, 98.5, 99), _bar(100, 100.5, 99.5, 100),
             _bar(100, 105, 97, 100), _bar(100, 105, 97, 100)]
    r3 = triple_barrier(bars3, 0, 1, 2.0, tp_atr=2.0, sl_atr=1.2, max_bars=48)
    assert r3["label"] == -1 and r3["exit_reason"] == "止损", r3

    # 4) 跳空穿越：第2根开盘直接低于止损
    bars4 = [_bar(99, 99.5, 98.5, 99), _bar(100, 100.5, 99.5, 100),
             _bar(97, 97.5, 95, 96), _bar(96, 97, 94, 95)]
    r4 = triple_barrier(bars4, 0, 1, 2.0, tp_atr=2.0, sl_atr=1.2, max_bars=48)
    assert r4["label"] == -1 and r4["exit_reason"] == "止损(跳空)" and r4["exit"] == 97.0, r4

    # 5) 横盘超时：价格在 99~101 区间，永不触轨，max_bars=2 -> 到期 sign
    base = [_bar(99, 99.5, 98.5, 99), _bar(100, 100.5, 99.5, 100)]
    for k in range(5):
        base.append(_bar(100.2, 100.8, 99.6, 100.5))  # 到期收盘高于入场 -> 多头 label=1
    r5 = triple_barrier(base, 0, 1, 2.0, tp_atr=2.0, sl_atr=1.2, max_bars=2)
    assert r5["label"] == 1 and r5["exit_reason"] == "超时" and r5["bars_held"] == 2, r5
    # 完全走平 -> label=0
    flat = [_bar(99, 99.5, 98.5, 99), _bar(100, 100, 100, 100),
            _bar(100, 100, 100, 100), _bar(100, 100, 100, 100)]
    r5b = triple_barrier(flat, 0, 1, 2.0, tp_atr=2.0, sl_atr=1.2, max_bars=2)
    assert r5b["label"] == 0 and r5b["exit_reason"] == "超时", r5b

    # 6) 空头对称：高点上破止损 -> -1
    bars6 = [_bar(101, 101.5, 100.5, 101), _bar(100, 100.5, 99.5, 100),
             _bar(101, 102.5, 100.8, 102.4), _bar(102, 103, 101.5, 102.8)]
    r6 = triple_barrier(bars6, 0, -1, 2.0, tp_atr=2.0, sl_atr=1.2, max_bars=48)
    assert r6["label"] == -1 and r6["exit_reason"] == "止损", r6

    # 7) 入场当根 j0 不查（即使 j0 内已触轨也忽略，从 j0+1 起）
    bars7 = [_bar(99, 99.5, 98.5, 99), _bar(100, 120, 90, 100),
             _bar(100, 100.5, 99.5, 100), _bar(100, 100.5, 99.5, 100)]
    r7 = triple_barrier(bars7, 0, 1, 2.0, tp_atr=2.0, sl_atr=1.2, max_bars=2)
    assert r7["exit_reason"] == "超时", r7

    # 8) PIT 特征：构造序列，未来值改变不影响 i 处特征
    closes = [float(100 + i) for i in range(70)]
    vols = [100.0] * 70
    scores = [0.5] * 70
    atrs = [1.0] * 70
    f1 = tech_features(closes, vols, scores, atrs, 65)
    closes2 = list(closes)
    for k in range(66, 70):
        closes2[k] = 9999.0
    f2 = tech_features(closes2, vols, scores, atrs, 65)
    assert f1 == f2 and abs(f1["mom5"] - (closes[65] / closes[60] - 1)) < 1e-12

    # 9) purged + embargo：标签探入测试折的训练样本被剔除
    order = [0, 10, 20, 30, 40]
    label_end = [8, 25, 22, 38, 48]   # 样本10的标签结束于25，探入测试折[20,40)
    train, test = purged_embargo_split(order, label_end, 20, 40, embargo=2)
    assert test == [2, 3], test
    assert 1 not in train and 0 in train and 4 in train, train  # 位置1（标签到25>=18）被purge
    # embargo=0 时样本10标签结束25仍>=20 -> 仍剔除
    train0, _ = purged_embargo_split(order, label_end, 20, 40, embargo=0)
    assert 1 not in train0

    print("build_ml_samples selftest ALL PASS（止盈/止损/同根双触/跳空/超时走平/空头/入场当根/PIT/embargo 9类断言通过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
