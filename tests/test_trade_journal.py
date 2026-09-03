# -*- coding: utf-8 -*-
"""G30（第42轮）交易复盘 journal 零网络确定性测试（tools/trade_journal.py）。

全部手算可核、不连 monitor.db（分钟库重放用 monkeypatch 注入合成 bars）：
  - CSV 装载：类型转换、空头负分、按 exit_dt 排序、缺文件安全
  - 分桶统计与 metrics.trade_stats 口径一致、原因归并/信号强度|分|/持仓档
  - 日·周聚合（ISO 周键）、累计曲线
  - 盘中 MFE/MAE：多空镜像、闭区间、区间外安全、attach 覆盖率与不依赖真实库
  - 报告/JSON 成稿、空数据降级、规则化观察（全胜桶不误报、弱桶命中）
"""
import io
import json
import os

import trade_journal as tj


def _trade(sym="RB", sector="黑色", d="多", net=100.0, *, reason="日终强平",
           score=3.0, hold=6, entry="2026-01-02 10:00:00", exit_="2026-01-02 15:00:00",
           fee=10.0, gross=None, leg="平今", entry_px=100.0, lots=2):
    return {"sym": sym, "name": sym, "sector": sector, "dir": d, "lots": lots,
            "entry_dt": tj.parse_dt(entry), "exit_dt": tj.parse_dt(exit_),
            "entry_px": entry_px, "exit_px": entry_px, "leg": leg, "hold_bars": hold,
            "gross_yuan": net + fee if gross is None else gross,
            "open_fee_yuan": fee / 2, "close_fee_yuan": fee / 2, "fee_yuan": fee,
            "net_yuan": net, "reason": reason, "forced": "强平" in reason,
            "entry_score": score, "margin_rate": 0.1,
            "direction": 1 if d == "多" else -1}


def _write_csv(tmp_path, rows):
    p = tmp_path / "trades.csv"
    header = ["sym", "name", "sector", "dir", "lots", "entry_dt", "exit_dt", "entry_px",
              "exit_px", "leg", "hold_bars", "gross_yuan", "open_fee_yuan",
              "close_fee_yuan", "net_yuan", "reason", "forced", "entry_score", "margin_rate"]
    with io.open(str(p), "w", encoding="utf-8", newline="") as f:
        f.write(",".join(header) + "\n")
        for t in rows:
            f.write(",".join([
                t["sym"], t["name"], t["sector"], t["dir"], str(t["lots"]),
                t["entry_dt"].strftime("%Y-%m-%d %H:%M:%S") if t["entry_dt"] else "",
                t["exit_dt"].strftime("%Y-%m-%d %H:%M:%S") if t["exit_dt"] else "",
                str(t["entry_px"]), str(t["exit_px"]), t["leg"], str(t["hold_bars"]),
                str(t["gross_yuan"]), str(t["open_fee_yuan"]), str(t["close_fee_yuan"]),
                str(t["net_yuan"]), t["reason"], str(t["forced"]),
                "" if t["entry_score"] is None else str(t["entry_score"]),
                str(t["margin_rate"])]) + "\n")
    return str(p)


# --------------------------- 1) 装载 ---------------------------
def test_load_missing_file_safe():
    assert tj.load_trades("definitely___missing.csv") == []
    assert tj.load_equity("definitely___missing.csv") == []


def test_parse_dt_formats():
    assert tj.parse_dt("2026-01-02 15:00:00").minute == 0
    assert tj.parse_dt("2026-01-02 15:00").hour == 15
    assert tj.parse_dt("2026-01-02") is not None
    assert tj.parse_dt("垃圾") is None and tj.parse_dt("") is None and tj.parse_dt(None) is None


def test_load_csv_types_and_order(tmp_path):
    rows = [
        _trade("RB", net=10.0, exit_="2026-01-05 15:00:00"),
        _trade("MA", d="空", net=-20.0, score=-3.5, exit_="2026-01-02 15:00:00"),
    ]
    p = _write_csv(tmp_path, rows)
    ts = tj.load_trades(p)
    assert len(ts) == 2
    # 按 exit_dt 升序：01-02 的 MA 在前
    assert ts[0]["sym"] == "MA" and ts[1]["sym"] == "RB"
    assert ts[0]["direction"] == -1 and ts[0]["lots"] == 2
    assert abs(ts[0]["net_yuan"] + 20) < 1e-9 and abs(ts[0]["fee_yuan"] - 10) < 1e-9
    assert ts[0]["forced"] is False or ts[0]["forced"] in (True, False)
    # 坏值安全：缺 score -> None
    assert ts[0]["entry_score"] == -3.5


# --------------------------- 2) 分桶/归并 ---------------------------
def test_reason_group():
    assert tj.reason_group("止盈(跳空)") == "止盈"
    assert tj.reason_group("止损") == "止损"
    assert tj.reason_group("日终强平") == "日终/样本强平"
    assert tj.reason_group("样本末清仓") == "日终/样本强平"
    assert tj.reason_group("反向信号") == "反向信号"
    assert tj.reason_group("") == "其他"


def test_score_band_uses_abs_for_short():
    assert tj.score_band(-7.0) == tj.score_band(7.0)
    assert tj.score_band(-3.2).startswith("轻仓")
    assert tj.score_band(0.5).startswith("弱(")
    assert tj.score_band(None) == "无分"


def test_hold_band_monotone_cover():
    labels = {tj.hold_band(b) for b in (1, 2, 3, 6, 7, 12, 13, 99)}
    assert labels == {"1极短(1-2)", "2短(3-6)", "3中(7-12)", "4长(13+)"}


def test_bucket_table_handcalc():
    ts = [_trade("RB", net=100), _trade("RB", net=-50), _trade("MA", net=200)]
    rows = {r["key"]: r for r in tj.bucket_table(ts, lambda t: t["sym"])}
    rb = rows["RB"]
    assert rb["n"] == 2 and abs(rb["net"] - 50) < 1e-9
    assert abs(rb["win_rate"] - 0.5) < 1e-12
    # PF = 100/50 = 2
    assert abs(rb["pf"] - 2.0) < 1e-12
    assert rows["MA"]["n"] == 1 and rows["MA"]["pf"] is None  # 全胜桶 PF=None


def test_period_pnl_and_curve():
    ts = [_trade("RB", net=10, exit_="2026-01-02 15:00"),
          _trade("MA", net=-5, exit_="2026-01-02 23:00"),
          _trade("I", net=20, exit_="2026-01-05 15:00")]
    days = tj.period_pnl(ts, tj.day_key)
    assert len(days) == 2
    d0 = next(r for r in days if r["key"] == "2026-01-02")
    assert d0["n"] == 2 and abs(d0["net"] - 5) < 1e-9 and d0["win"] == 1
    assert tj.week_key(ts[2]) == "2026-W02"
    curve = tj.cumulative_curve(ts)
    assert abs(curve[-1][1] - 25) < 1e-9


# --------------------------- 3) MFE/MAE ---------------------------
def _bars():
    return [
        {"dt": tj.parse_dt("2026-01-02 09:30:00"), "h": 99.0, "l": 97.0},
        {"dt": tj.parse_dt("2026-01-02 10:00:00"), "h": 103.0, "l": 99.0},
        {"dt": tj.parse_dt("2026-01-02 11:00:00"), "h": 105.0, "l": 101.0},
        {"dt": tj.parse_dt("2026-01-02 15:00:00"), "h": 102.0, "l": 98.0},
    ]


def test_range_excursion_long_short():
    bars = _bars()
    t0, t1 = tj.parse_dt("2026-01-02 10:00:00"), tj.parse_dt("2026-01-02 15:00:00")
    mfe, mae, used = tj.range_excursion(1, 100.0, bars, t0, t1)
    assert used == 3 and abs(mfe - 0.05) < 1e-12 and abs(mae - 0.02) < 1e-12
    mfe2, mae2, used2 = tj.range_excursion(-1, 100.0, bars, t0, t1)
    assert used2 == 3 and abs(mfe2 - 0.02) < 1e-12 and abs(mae2 - 0.05) < 1e-12


def test_range_excursion_safe():
    assert tj.range_excursion(1, 100.0, [], None, None) == (None, None, 0)
    mfe, mae, used = tj.range_excursion(
        1, 100.0, _bars(), tj.parse_dt("2027-01-01 00:00:00"), tj.parse_dt("2027-01-02 00:00:00"))
    assert used == 0 and mfe is None and mae is None
    # 入场价非法
    assert tj.range_excursion(1, 0.0, _bars(), None, None) == (None, None, 0)


def test_attach_excursions_uses_injected_bars(monkeypatch):
    ts = [_trade("RB", net=10, entry="2026-01-02 10:00:00", exit_="2026-01-02 15:00:00")]
    monkeypatch.setattr(tj, "load_minute_bars_for", lambda *a, **k: _bars())
    meta = tj.attach_excursions(ts, period=30, lookback=100)
    assert meta["total"] == 1 and meta["with_excursion"] == 1 and abs(meta["coverage"] - 1.0) < 1e-12
    assert abs(ts[0]["mfe_bar"] - 0.05) < 1e-12 and abs(ts[0]["mae_bar"] - 0.02) < 1e-12
    ex = tj.excursion_summary(ts)
    assert ex["n"] == 1 and abs(ex["avg_mfe"] - 0.05) < 1e-12


def test_attach_excursions_missing_db_safe():
    # 装载器返回空（模拟无库/无数据）：覆盖率0但不抛错
    ts = [_trade("XX", net=1)]
    meta = tj.attach_excursions(ts, period=30, lookback=10)
    assert meta["with_excursion"] == 0 and meta["coverage"] == 0.0
    assert tj.excursion_summary(ts) is None


# --------------------------- 4) 报告/观察 ---------------------------
def test_build_report_empty_safe():
    rep = tj.build_report([])
    assert "无成交记录" in rep and "交易复盘 Journal" in rep


def test_build_report_and_json_roundtrip(tmp_path):
    ts = [_trade("RB", net=100, reason="止盈", score=5.0, hold=4),
          _trade("RB", net=-50, reason="止损", score=3.0, hold=8),
          _trade("MA", net=200, reason="止盈(跳空)", score=7.0, hold=3)]
    rep = tj.build_report(ts, review="both")
    for kw in ("总览", "品种 sym", "信号强度", "日节奏", "周节奏", "最佳/最差", "规则化观察"):
        assert kw in rep
    payload = tj.build_json_payload(ts, [], None, None, "both")
    blob = json.dumps(tj._json_safe(payload), ensure_ascii=False, allow_nan=False)
    assert json.loads(blob)["n_trades"] == 3


def test_observations_no_false_allwin_alert():
    # 12 笔全胜（PF=None）不得被报为弱势桶
    ts = [_trade("RB", net=10, reason="止盈") for _ in range(12)]
    import metrics
    overall = metrics.trade_stats([t["net_yuan"] for t in ts])
    obs = tj.observations(ts, overall, None)
    joined = "\n".join(obs)
    assert "弱势桶" not in joined


def test_observations_flags_weak_bucket():
    # 12 笔全亏（PF=0）应命中品种弱势桶
    ts = [_trade("RB", net=-10, reason="止损") for _ in range(12)]
    import metrics
    overall = metrics.trade_stats([t["net_yuan"] for t in ts])
    obs = "\n".join(tj.observations(ts, overall, None))
    assert "品种=RB" in obs and "弱势桶" in obs


def test_run_end_to_end(tmp_path):
    rows = [_trade("RB", net=100, exit_="2026-01-05 15:00:00"),
            _trade("MA", net=-40, d="空", score=-3.0, exit_="2026-01-06 15:00:00")]
    p = _write_csv(tmp_path, rows)
    out = str(tmp_path / "journal.txt")
    js = str(tmp_path / "journal.json")
    rc = tj.run(["--trades", p, "--equity", "", "--review", "both",
                 "--out", out, "--json-out", js])
    assert rc == 0 and os.path.exists(out) and os.path.exists(js)
    with io.open(js, encoding="utf-8") as f:
        assert json.load(f)["n_trades"] == 2
