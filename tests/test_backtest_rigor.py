# -*- coding: utf-8 -*-
"""第26轮 G4 回测严谨性回归（零网络、确定性）：
- next_open 成交严格晚信号一根、close 旧口径逐值等价、末根信号不虚构、锁板顺延、反手；
- 冲击成本分项；bootstrap 区间可复现/有序/退化/样本不足；分位、IS/OOS、历史百分位、sidecar；
- storage 第12张表 backtest_runs 与 archive_run 纵向留档。
"""
import json
import os
from types import SimpleNamespace

import backtest


# ---------- 合成 prepared 夹具 ----------

def _prepared(closes, opens, scores, highs=None, lows=None):
    n = len(closes)
    highs = highs or list(closes)
    lows = lows or list(closes)
    bars = [{"d": f"2026-01-{i+1:02d}", "o": opens[i], "h": highs[i],
             "l": lows[i], "c": closes[i]} for i in range(n)]
    series = [{"i": i, "ind": {}, "score": scores[i]} for i in range(n)]
    return {"name": "测试", "code": "RB0", "sym": "RB", "bars": bars,
            "closes": list(closes), "opens": list(opens), "highs": list(highs),
            "lows": list(lows), "series": series, "roll_count": 0}


def _run(prepared, fill_mode="close", impact=0.0, limit_move=None, hold=3, entry=2.0):
    return backtest.simulate_prepared(
        "测试", "RB0", prepared, hold, entry, fee_rate=0.0, slip_rate=0.0,
        limit_move=limit_move, collect_signals=False, fee_table={},
        use_real_fees=False, fill_mode=fill_mode, impact_rate=impact)


# ---------- 一、成交时点 ----------

def test_close_fill_enters_at_signal_close():
    closes = [100, 101, 102, 103, 104, 105, 106, 107]
    scores = [3.0, 0, 0, 0, 0, 0, 0, 0]
    r = _run(_prepared(closes, [c - 0.5 for c in closes], scores), fill_mode="close")
    assert len(r["trades"]) == 1
    t = r["trades"][0]
    assert t["entry_date"] == "2026-01-01" and t["exit_date"] == "2026-01-04"
    assert abs(t["gross_ret"] - (103 / 100 - 1)) < 1e-12   # i0收盘进、i3(持有3根)收盘出
    assert t["hold"] == 3 and t["exit"] == "到期"


def test_next_open_fill_one_bar_later():
    closes = [100, 101, 102, 103, 104, 105, 106, 107]
    opens = [99.0, 100.5, 101.5, 102.5, 103.5, 104.5, 105.5, 106.5]
    scores = [3.0, 0, 0, 0, 0, 0, 0, 0]
    r = _run(_prepared(closes, opens, scores), fill_mode="next_open")
    assert len(r["trades"]) == 1
    t = r["trades"][0]
    # i0 决策 → i1 开盘 100.5 入场；i1 起持有3根 → i4 挂到期 → i5 开盘 104.5 离场
    assert t["entry_date"] == "2026-01-02" and t["exit_date"] == "2026-01-06"
    assert abs(t["gross_ret"] - (104.5 / 100.5 - 1)) < 1e-12
    assert t["hold"] == 4


def test_next_open_last_bar_signal_not_filled():
    closes = [100.0] * 8
    opens = [100.0] * 8
    scores = [0, 0, 0, 0, 0, 0, 0, 3.0]   # 仅末根出信号
    r = _run(_prepared(closes, opens, scores), fill_mode="next_open")
    assert r["trades"] == [] and r["unfilled_entry"] == 1
    # close 口径下末根信号当根成交、随即样本末平仓（hold=0），不丢弃
    rc = _run(_prepared(closes, opens, scores), fill_mode="close")
    assert len(rc["trades"]) == 1


def test_next_open_locked_entry_postponed_skip():
    # i1 开盘即封死涨停（+7%且收在最高）：买单无法成交，计 blocked_entry
    closes = [100, 107, 107, 108, 109, 110, 111, 112]
    opens = [99.0, 107, 107, 108, 109, 110, 111, 112]
    scores = [3.0, 0, 0, 0, 0, 0, 0, 0]
    r = _run(_prepared(closes, opens, scores), fill_mode="next_open", limit_move=0.07)
    assert r["blocked_entry"] == 1 and r["trades"] == []


def test_next_open_reverse_closes_then_opens():
    # i0 多信号 → i1 开盘进多；i2 反号 → i3 开盘先平多再开空；末根收盘平空
    closes = [100, 101, 102, 101, 100, 99, 98, 97]
    opens = [99.5, 100.5, 101.5, 101.5, 100.5, 99.5, 98.5, 97.5]
    scores = [3.0, 0, -3.0, 0, 0, 0, 0, 0]
    r = _run(_prepared(closes, opens, scores), fill_mode="next_open", hold=99)
    dirs = [t["direction"] for t in r["trades"]]
    assert dirs == ["多", "空"]
    assert r["trades"][0]["exit"] == "反向"
    assert r["trades"][1]["entry_date"] == r["trades"][0]["exit_date"]  # 同根先平后开


def test_close_path_equivalent_when_impact_zero():
    closes = [100 + i for i in range(8)]
    opens = [c - 0.3 for c in closes]
    scores = [3.0, 0, -3.0, 0, 3.0, 0, 0, 0]
    r = _run(_prepared(closes, opens, scores), fill_mode="close")
    # close 路径成交价全部取 closes：手算第一笔 i0进/i2反向平
    t0 = r["trades"][0]
    assert abs(t0["gross_ret"] - (closes[2] / closes[0] - 1)) < 1e-12
    assert t0["impact_cost"] == 0.0 and t0["slip_cost"] == 0.0 and t0["fee_cost"] == 0.0


# ---------- 二、冲击成本 ----------

def test_impact_cost_split_and_round_trip():
    closes = [100, 101, 102, 103, 104, 105, 106, 107]
    scores = [3.0, 0, 0, 0, 0, 0, 0, 0]
    r0 = _run(_prepared(closes, closes, scores), impact=0.0)
    r1 = _run(_prepared(closes, closes, scores), impact=0.0002)
    t0, t1 = r0["trades"][0], r1["trades"][0]
    assert t0["impact_cost"] == 0.0
    assert abs(t1["impact_cost"] - 0.0004) < 1e-12           # 往返两次
    assert abs(t1["cost"] - (t1["fee_cost"] + t1["slip_cost"] + 0.0004)) < 1e-12
    assert abs(t1["ret"] - (t1["gross_ret"] - t1["cost"])) < 1e-12
    assert t0["ret"] == t0["gross_ret"]


# ---------- 三、bootstrap / 分位 ----------

def test_quantile_known_values():
    v = [1.0, 2.0, 3.0, 4.0]
    assert backtest.quantile_inplace(v, 0.0) == 1.0
    assert backtest.quantile_inplace(v, 1.0) == 4.0
    assert abs(backtest.quantile_inplace(v, 0.5) - 2.5) < 1e-12
    assert backtest.quantile_inplace([7.0], 0.5) == 7.0
    assert backtest.quantile_inplace([], 0.5) is None


def test_bootstrap_deterministic_and_ordered():
    rets = [0.01, -0.02, 0.03, -0.005, 0.02, -0.01, 0.015, 0.008, -0.012,
            0.02, 0.004, -0.009, 0.011, -0.006, 0.007, 0.003, -0.011,
            0.009, 0.005, -0.004, 0.013]
    b1 = backtest.bootstrap_trade_stats(rets, 400, seed=7, min_trades=20)
    b2 = backtest.bootstrap_trade_stats(rets, 400, seed=7, min_trades=20)
    assert b1 == b2                                        # 固定种子可复现
    assert b1["cum_p5"] <= b1["cum_median"] <= b1["cum_p95"]
    assert 0 <= b1["dd_p5"] <= b1["dd_median"] <= b1["dd_p95"]
    assert b1["n"] == 21 and b1["n_boot"] == 400


def test_bootstrap_constant_collapses():
    b = backtest.bootstrap_trade_stats([0.01] * 30, 200, seed=1, min_trades=20)
    expect = 1.01 ** 30 - 1
    assert abs(b["cum_p5"] - expect) < 1e-9 and abs(b["cum_p95"] - expect) < 1e-9
    assert b["dd_p5"] == 0.0


def test_bootstrap_guards():
    assert backtest.bootstrap_trade_stats([0.01] * 5, 100, min_trades=20) is None  # 样本不足
    assert backtest.bootstrap_trade_stats([0.01] * 30, 0, min_trades=20) is None   # 关闭


# ---------- 四、IS/OOS、百分位、sidecar ----------

def test_split_is_oos_order_and_ratio():
    trades = [{"exit_date": f"2026-01-{i:02d}", "ret": i * 0.001} for i in range(10, 0, -1)]
    is_tr, oos_tr = backtest.split_is_oos(trades, 0.3)
    assert len(is_tr) == 7 and len(oos_tr) == 3
    assert [t["exit_date"] for t in is_tr + oos_tr] == sorted(t["exit_date"] for t in trades)
    is_all, oos_empty = backtest.split_is_oos(trades, 0.0)
    assert len(is_all) == 10 and oos_empty == []


def test_percentile_at_or_below():
    vals = [0.01, 0.02, 0.03, 0.04]
    assert abs(backtest.percentile_at_or_below(vals, 0.02) - 0.5) < 1e-12
    assert backtest.percentile_at_or_below([], 1.0) is None


def test_load_validation_sidecar_bad_paths(tmp_path):
    assert backtest.load_validation_sidecar(str(tmp_path / "missing.json")) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert backtest.load_validation_sidecar(str(bad)) is None
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"dsr": {"dsr": 0.9}, "grid": {"n": 3}}), encoding="utf-8")
    data = backtest.load_validation_sidecar(str(good))
    assert data["grid"]["n"] == 3
    txt = backtest._fmt_validation_ref(data)
    assert "DSR=0.90" in txt and "3 品种" in txt
    assert backtest._fmt_validation_ref(None) == ""


# ---------- 五、storage 第12张表 + archive_run ----------

def test_backtest_runs_table_and_history(tmp_db):
    counts = tmp_db.table_counts()
    assert "backtest_runs" in counts and counts["backtest_runs"] == 0
    rid = tmp_db.insert_backtest_run({"run_ts": "2026-09-02 18:00:00", "kind": "daily",
                                      "fill_mode": "close", "n_trades": 12,
                                      "cumulative": 0.05, "max_dd": 0.02,
                                      "sharpe": 0.4, "win_rate": 0.5,
                                      "params": {"hold": 10}, "metrics": {"n": 12}})
    assert rid == 1
    hist = tmp_db.backtest_run_history("daily")
    assert len(hist) == 1 and hist[0]["fill_mode"] == "close"
    assert json.loads(hist[0]["params_json"])["hold"] == 10


def _fake_args(**kw):
    base = dict(days=250, hold=10, entry=2.0, fill="close", slip_rate=1e-4,
                impact_rate=0.0, fee_rate=5e-5, no_real_fees=True, no_cost=False,
                oos_ratio=0.0, no_limit_filter=True, no_stable=True, bootstrap=0)
    base.update(kw)
    return SimpleNamespace(**base)


class _KeepOpen:
    """archive_run 用完会 close 自建连接；测试复用同一 tmp_db，close 改为 no-op。"""
    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


def test_archive_run_sequence_and_percentile(tmp_db):
    args = _fake_args()
    results = [{"trades": [{"ret": 0.01}, {"ret": -0.005}]}]
    factory = lambda: _KeepOpen(tmp_db)
    m1 = backtest.metrics_from_returns([0.01, -0.005], 10)
    info1 = backtest.archive_run(args, results, [], m1, db_factory=factory)
    assert info1["seq"] == 1
    m2 = backtest.metrics_from_returns([0.02, 0.01], 10)
    info2 = backtest.archive_run(args, results, [], m2, db_factory=factory)
    assert info2["seq"] == 2 and info2["total"] == 2
    assert abs(info2["percentile"] - 1.0) < 1e-12     # 第二次累计最高，好于100%
    hist = tmp_db.backtest_run_history("daily")
    assert len(hist) == 2


def test_archive_run_db_failure_soft_degrade():
    def boom():
        raise RuntimeError("db unavailable")
    args = _fake_args()
    info = backtest.archive_run(args, [], [], None, db_factory=boom)
    assert info is None       # 留档失败绝不拖垮回测
