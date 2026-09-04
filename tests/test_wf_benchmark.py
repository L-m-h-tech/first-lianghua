# -*- coding: utf-8 -*-
"""G4续（第62轮）滚动 walk-forward + 对照基准 backtest_rigor.py 的零网络确定性回归。
（第26轮 next_open/bootstrap/IS-OOS/留档 的既有 G4 用例在 test_backtest_rigor.py，不重复。）"""
import backtest_rigor as br


def make_prepared(n=300, warmup=60):
    closes = [100.0 + 0.1 * i for i in range(n)]
    bars = [{"d": "D%03d" % i, "c": closes[i]} for i in range(n)]
    # 确定性的伪技术分序列（只用于占位，选参由假模拟器决定）
    series = [{"i": i, "ind": {}, "score": (1 if i % 7 == 0 else (-1 if i % 11 == 0 else 0))}
              for i in range(warmup, n)]
    return {"name": "t", "code": "T0", "sym": "T", "bars": bars, "closes": closes,
            "opens": list(closes), "highs": [c * 1.01 for c in closes],
            "lows": [c * 0.99 for c in closes], "series": series, "roll_count": 0}


# ---------- 切窗 ----------
def test_slice_reindex_and_lengths():
    p = make_prepared(100)
    sub = br.slice_prepared(p, 70, 90)
    assert len(sub["closes"]) == len(sub["bars"]) == len(sub["opens"]) == 20
    # 全局 70 -> 局部 0；ind/score 原样保留（因果指标，不重算）
    assert sub["series"][0]["i"] == 0
    assert sub["series"][0]["score"] == p["series"][70 - 60]["score"]
    assert sub["series"][-1]["i"] == 19
    assert sub["closes"][0] == p["closes"][70]


def test_slice_does_not_mutate_source():
    p = make_prepared(100)
    br.slice_prepared(p, 70, 90)
    assert len(p["closes"]) == 100 and p["series"][0]["i"] == 60  # 原序列索引不动


# ---------- 买入持有基准 ----------
def test_buy_hold_window_basic():
    assert abs(br.buy_hold_window([100, 105, 110], 0, 3) - 0.10) < 1e-12


def test_buy_hold_skips_leading_zero():
    assert abs(br.buy_hold_window([0, 0, 100, 120], 0, 4) - 0.20) < 1e-12


def test_buy_hold_none_when_uncomputable():
    assert br.buy_hold_window([], 0, None) is None
    assert br.buy_hold_window([0, 0], 0, 2) is None


def test_benchmark_starts_at_warmup():
    p = make_prepared(160)
    expect = p["closes"][-1] / p["closes"][60] - 1.0
    assert abs(br.benchmark_for_prepared(p, warmup=60) - expect) < 1e-12


def test_pooled_excess_and_beat():
    assert abs(br.pooled_buy_hold([0.1, 0.3, None]) - 0.2) < 1e-12
    assert br.pooled_buy_hold([None]) is None
    assert abs(br.excess(0.3, 0.1) - 0.2) < 1e-12
    assert br.excess(None, 0.1) is None and br.excess(0.1, float("nan")) is None
    pairs = [("A", 0.2, 0.1), ("B", -0.1, 0.0), ("C", 0.05, 0.1), ("D", None, 0.2)]
    beat, n, rows = br.beat_benchmark_pairs(pairs)
    assert n == 3 and beat == 1 and len(rows) == 3     # A跑赢；D缺策略值被跳过


# ---------- walk-forward 折划分 ----------
def test_wf_folds_contiguous_nonoverlap():
    folds = br.wf_folds(300, warmup=60, train_bars=120, test_bars=40)
    # 首折 OOS 起点 = warmup+train = 180；之后每折推进40，覆盖到300
    assert [(f[2], f[3]) for f in folds] == [(180, 220), (220, 260), (260, 300)]
    for ia, ib, oa, ob in folds:
        assert ib == oa and ia == oa - 120          # IS 紧邻且在 OOS 之前
        assert ob - oa == 40


def test_wf_folds_drop_tiny_tail():
    # 尾折 OOS 只有1根 -> 丢弃
    folds = br.wf_folds(221, 60, 120, 40)
    assert folds[-1][3] == 220                      # 180-220 保留，220-221 不足2根丢弃


# ---------- 选参 ----------
def test_select_best_param_picks_max_and_respects_min_trades():
    grid = [(5, 1.5), (10, 2.0), (20, 2.5)]

    def sim(sub, hold, entry):
        # hold 越大均收越高，但 hold=20 只给1笔（低于 min_is_trades=3，应被排除）
        n = 1 if hold == 20 else 5
        avg = hold * 0.001
        return {"trade_metrics": {"n": n, "avg": avg}, "trades": []}

    chosen, is_n, is_avg, cands = br.select_best_param("sub", grid, sim, 3)
    assert chosen == (10, 2.0)                      # 20被样本门槛排除，10胜出
    assert is_n == 5 and abs(is_avg - 0.01) < 1e-12
    assert len(cands) == 3


def test_select_best_param_tie_keeps_first():
    grid = [(5, 1.5), (10, 2.0)]

    def sim(sub, hold, entry):
        return {"trade_metrics": {"n": 4, "avg": 0.01}, "trades": []}

    chosen, _, _, _ = br.select_best_param("sub", grid, sim, 3)
    assert chosen == (5, 1.5)                       # 严格大于，并列保留先出现者


# ---------- 端到端 walk-forward ----------
def _recording_simulator(calls):
    def sim(sub, hold, entry):
        n = len(sub["closes"])
        dates = (sub["bars"][0]["d"], sub["bars"][-1]["d"])
        calls.append((n, dates, hold, entry))
        # OOS段长度=test时产出2笔带方向的交易；IS段产出4笔供选参
        k = 4 if n == 120 else 2
        trades = [{"ret": 0.01 * hold, "direction": "多" if j % 2 else "空"} for j in range(k)]
        avg = sum(t["ret"] for t in trades) / k
        return {"trades": trades, "trade_metrics": {"n": k, "avg": avg}}
    return sim


def test_walk_forward_oos_disjoint_and_tagged():
    p = make_prepared(300)
    calls = []
    grid = [(5, 1.5), (10, 2.0)]
    out = br.walk_forward_symbol(p, _recording_simulator(calls), grid, (10, 2.0),
                                 train_bars=120, test_bars=40, min_is_trades=3, warmup=60)
    assert len(out["folds"]) == 3
    # 每折：先对网格每个参数各调一次IS(长度120)，再调一次OOS(长度40)；IS末日期 < OOS首日期
    n_grid = len(grid)
    for k in range(3):
        is_calls = calls[k * (n_grid + 1): k * (n_grid + 1) + n_grid]
        oos_call = calls[k * (n_grid + 1) + n_grid]
        assert all(c[0] == 120 for c in is_calls) and oos_call[0] == 40
        assert all(c[1][1] < oos_call[1][0] for c in is_calls)
    # OOS 交易：3折×2笔=6，全部带 wf_fold/hold/entry 标注
    assert len(out["oos_trades"]) == 6
    assert {t["wf_fold"] for t in out["oos_trades"]} == {0, 1, 2}
    assert all("wf_hold" in t and "wf_entry" in t for t in out["oos_trades"])


def test_walk_forward_fallback_to_default():
    p = make_prepared(300)

    def thin_sim(sub, hold, entry):
        # IS 永远样本不足；OOS 给2笔
        n = len(sub["closes"])
        if n == 120:
            return {"trades": [], "trade_metrics": {"n": 0, "avg": 0.0}}
        return {"trades": [{"ret": 0.01, "direction": "多"}],
                "trade_metrics": {"n": 1, "avg": 0.01}}

    out = br.walk_forward_symbol(p, thin_sim, [(5, 1.5), (10, 2.0)], (7, 1.8),
                                 train_bars=120, test_bars=40, min_is_trades=3, warmup=60)
    assert all(f["fallback"] for f in out["folds"])
    assert all((f["hold"], f["entry"]) == (7, 1.8) for f in out["folds"])


def test_param_usage_and_is_oos_avg():
    folds = [
        {"hold": 5, "entry": 1.5, "is_avg": 0.02, "oos_avg": 0.01, "fallback": False},
        {"hold": 5, "entry": 1.5, "is_avg": 0.03, "oos_avg": -0.01, "fallback": False},
        {"hold": 10, "entry": 2.0, "is_avg": None, "oos_avg": 0.0, "fallback": True},
    ]
    usage = br.param_usage(folds)
    assert usage == {"5d/1.5": 2, "10d/2.0": 1}
    is_avg, oos_avg = br.is_vs_oos_avg(folds)
    assert abs(is_avg - 0.025) < 1e-12 and abs(oos_avg - 0.0) < 1e-12
    assert br.is_vs_oos_avg([{"is_avg": None, "oos_avg": None}]) == (None, None)
