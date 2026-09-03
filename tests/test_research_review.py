# -*- coding: utf-8 -*-
"""G30③（第43轮）研究侧一键复盘编排器 零网络确定性测试（tools/research_review.py）。

不连任何 DB/网络、不读真实 reports（全部用临时目录与合成 sidecar）：
  - 装载安全：缺文件/坏 JSON、新鲜度三态、age 文案
  - equity CSV：BOM、末尾空记录、期初取首行/期末取末行、最大回撤全表扫
  - signal_tracking 正则：中文周期/胜率/方向收益、无关行不匹配
  - 各 sidecar 段提取：失效预警、贡献排序、弱势桶 n 门槛、由盈转亏比例、缺周期安全
  - 规则化待办：WARN 优先排序、阈值命中、可选产物缺失降级 INFO、全 OK 路径
  - collect/build_report/build_json：空目录安全降级、七段齐全、allow_nan
"""
import datetime as dt
import io
import json
import os

import research_review as rr

FIXED = dt.datetime(2026, 9, 3, 15, 0, 0)


# ---------------- 装载与新鲜度 ----------------
def test_load_sidecar_missing_and_broken(tmp_path):
    assert rr.load_sidecar(str(tmp_path / "nope.json"), now=FIXED) == (None, None)
    bad = tmp_path / "bad.json"
    bad.write_text("{不是合法json", encoding="utf-8")
    obj, mt = rr.load_sidecar(str(bad), now=FIXED)
    assert obj is None and mt is not None          # 文件在但损坏：mtime 仍返回


def test_freshness_states():
    assert rr.freshness_state(None) == "missing"
    assert rr.freshness_state(FIXED - dt.timedelta(hours=1), now=FIXED) == "ok"
    assert rr.freshness_state(FIXED - dt.timedelta(days=10), now=FIXED, stale_hours=168) == "stale"
    assert rr.age_label(None) == "—"
    assert "分钟前" in rr.age_label(FIXED - dt.timedelta(minutes=30), now=FIXED)
    assert "天前" in rr.age_label(FIXED - dt.timedelta(days=3), now=FIXED)


def test_equity_summary_bom_emptyrow_and_span(tmp_path):
    p = tmp_path / "equity.csv"
    with io.open(str(p), "w", encoding="utf-8-sig", newline="") as f:
        f.write("dt,static,float,equity,margin,available,risk,drawdown,npos\n")
        f.write("t1,1000000,0,1000000,0,1000000,0,0,0\n")
        f.write("t2,1000000,0,850000,700000,150000,0.8235,0.15,5\n")
        f.write("t3,1000000,0,900000,100000,800000,0.1111,0.10,2\n")
        f.write(",,,,,,,,\n")
    e = rr.load_equity_summary(str(p))
    assert e["n_bars"] == 3 and e["end_dt"] == "t3" and e["start_dt"] == "t1"
    assert e["start_equity"] == 1000000 and e["equity"] == 900000
    assert abs(e["max_drawdown"] - 0.15) < 1e-12       # 全表最大在 t2
    assert abs(e["ret"] + 0.10) < 1e-12 and e["npos"] == 2
    assert rr.load_equity_summary(str(tmp_path / "x.csv")) == {}


def test_signal_tracking_regex(tmp_path):
    p = tmp_path / "sig.txt"
    with io.open(str(p), "w", encoding="utf-8-sig") as f:
        f.write("无关标题行\n")
        f.write(" 30分钟        样本464(过期40) 胜率49.3%   平均方向收益-0.01% 多头156/324\n")
        f.write("    · 分批/做多 子行不匹配\n")
        f.write(" 次日(约24小时) 样本315 胜率55.2%  平均方向收益+0.25%\n")
    rows = rr.load_signal_tracking(str(p))
    assert len(rows) == 2
    assert rows[0]["period"] == "30分钟" and abs(rows[0]["win_rate"] - 0.493) < 1e-12
    assert abs(rows[0]["avg_dir_ret"] + 0.0001) < 1e-12
    assert abs(rows[1]["avg_dir_ret"] - 0.0025) < 1e-12
    assert rr.load_signal_tracking(str(tmp_path / "x.txt")) == []


# ---------------- 段提取 ----------------
def test_sec_factor_health_alerts_and_sort():
    obj = {"event": {"30": {
        "A": {"n": 10, "ic": -0.1, "verdict": "失效预警", "max_consec_fail": 4, "frac_fail": 0.5},
        "B": {"n": 10, "ic": 0.2, "verdict": "有效", "max_consec_fail": 0, "frac_fail": 0.0}}},
        "daily": {"x": {"5": {"ic": 0.3}, "halflife": {"half_life": 20.0}},
                  "y": {"5": {"ic": -0.2}, "halflife": None}}}
    out = rr.sec_factor_health(obj)
    assert [a["factor"] for a in out["alerts"]] == ["A"]
    assert out["daily_ic"][0]["factor"] == "y" and out["daily_ic"][-1]["factor"] == "x"
    assert len(out["halflife"]) == 1
    assert rr.sec_factor_health(None) == {}
    assert rr.sec_factor_health({"event": {}})["alerts"] == []


def test_sec_attribution_order_and_missing_horizon():
    obj = {"horizons": {"30": {"n": 9, "alpha": 0.0, "r2": 0.1,
        "factors": [{"factor": "p", "contrib": 0.001},
                    {"factor": "q", "contrib": -0.002}, {"factor": "r", "contrib": 0.003}],
        "bhb_sectors": [{"sector": "s1", "effect": -0.01}, {"sector": "s2", "effect": 0.02}]}}}
    out = rr.sec_attribution(obj, "30")
    assert out["factor_bottom"][0]["factor"] == "q"
    assert out["factor_top"][0]["factor"] == "r"
    assert out["sector_top"][0]["sector"] == "s2"
    assert rr.sec_attribution(obj, "999") == {}


def test_sec_journal_weak_threshold_and_green_ratio():
    obj = {"n_trades": 100, "overall": {"win_rate": 0.4, "profit_factor": 0.6,
            "payoff_ratio": 1.2, "expectancy": -50, "max_win_streak": 3, "max_loss_streak": 8},
           "by_hold_band": [{"key": "短", "n": 50, "pf": 0.5, "net": -1},
                            {"key": "小", "n": 2, "pf": 0.1, "net": -1},     # n 门槛挡掉
                            {"key": "长", "n": 50, "pf": 2.0, "net": 1}],
           "by_score_band": [], "excursion": {"loss_once_green": 3, "n_loss": 4}}
    j = rr.sec_journal(obj)
    assert [b["key"] for b in j["weak_hold"]] == ["短"]
    assert abs(j["green_ratio"] - 0.75) < 1e-12
    assert rr.sec_journal({}) == {}


def test_sec_lab_and_validation():
    lab = rr.sec_lab({"n_universe": 61, "n_days": 300,
        "rolling_stats": {"erc": {"sharpe": 0.5, "maxdd": 0.06}},
        "snapshot": {"erc": {"eff_n": 20.0}}})
    assert abs(lab["methods"]["erc"]["sharpe"] - 0.5) < 1e-12 and lab["snapshot_eff_n"]["erc"] == 20.0
    v = rr.sec_validation({"dsr": {"dsr": 0.2, "verdict": "x", "n_trials": 18},
                           "grid": {"n": 1, "pbo_good": 0, "oos_pos": 0, "all_loss": 1}})
    assert v["dsr"] == 0.2 and v["grid_n"] == 1


# ---------------- 规则引擎 ----------------
def _all_missing_freshness():
    return {name: {"state": "missing", "mtime": "—", "age": "—"} for name, _l, _c in rr.SOURCES}


def test_actions_order_and_hits():
    bundle = {
        "factor_health": {"alerts": [{"factor": "F", "verdict": "失效预警", "ic": -0.01,
                                      "max_consec_fail": 3, "frac_fail": 0.5}]},
        "journal": {"pf": 0.6, "expectancy": -10, "win_rate": 0.4,
                    "weak_hold": [{"key": "短", "n": 30, "pf": 0.5, "net": -9}],
                    "weak_score": [], "green_ratio": 0.6, "loss_once_green": 6, "n_loss": 10},
        "equity": {"max_drawdown": 0.2, "risk": 0.2, "npos": 3},
        "validation": {"dsr": 0.1, "verdict": "v", "n_trials": 18},
    }
    fr = _all_missing_freshness()
    fr["factor_health.json"] = {"state": "ok", "mtime": "x", "age": "1h"}
    acts = rr.build_actions(bundle, fr, now=FIXED)
    levels = [x[0] for x in acts]
    assert levels == sorted(levels, key={"WARN": 0, "INFO": 1, "OK": 2}.__getitem__)
    text = " ".join(t for _l, t in acts)
    assert "失效预警" in text and "PF=0.60" in text and "持仓弱势桶" in text
    assert "超 15%" in text and "DSR=0.1000" in text
    assert "缺少研究产物" in text and "可选产物" in text     # expr 缺失降级 INFO


def test_actions_all_ok():
    fr = {name: {"state": "ok", "mtime": "x", "age": "1h"} for name, _l, _c in rr.SOURCES}
    acts = rr.build_actions({}, fr, now=FIXED)
    assert len(acts) == 1 and acts[0][0] == "OK"


def test_actions_optional_and_signal_missing_are_info():
    fr = _all_missing_freshness()
    acts = rr.build_actions({}, fr, now=FIXED)
    by_text = {t: lv for lv, t in acts}
    assert all(lv == "WARN" for lv, t in acts if "缺少研究产物" in t)
    assert any("可选产物" in t and lv == "INFO" for lv, t in acts)
    assert any("主链信号追踪" in t and lv == "INFO" for lv, t in acts)


# ---------------- 端到端 ----------------
def test_collect_empty_dir_safe(tmp_path):
    bundle, freshness = rr.collect(str(tmp_path), now=FIXED)
    assert bundle == {} or set(bundle) == set()
    assert all(v["state"] == "missing" for v in freshness.values())


def test_collect_reads_synthetic_sidecars(tmp_path):
    (tmp_path / "trade_journal.json").write_text(json.dumps({
        "n_trades": 10, "overall": {"win_rate": 0.5, "profit_factor": 1.2, "payoff_ratio": 1.1,
        "expectancy": 5, "max_win_streak": 2, "max_loss_streak": 2},
        "by_hold_band": [], "by_score_band": [], "excursion": {}}), encoding="utf-8")
    (tmp_path / "portfolio_equity.csv").write_text(
        "dt,static,float,equity,margin,available,risk,drawdown,npos\n"
        "t1,1000000,0,1000000,0,1000000,0,0,0\nt2,1000000,0,1010000,0,1010000,0,0,0\n",
        encoding="utf-8-sig")
    bundle, freshness = rr.collect(str(tmp_path), now=FIXED, stale_hours=10 ** 9)
    assert bundle["journal"]["n_trades"] == 10
    assert abs(bundle["equity"]["ret"] - 0.01) < 1e-12
    assert freshness["trade_journal.json"]["state"] == "ok"


def test_build_report_sections_and_json(tmp_path):
    bundle, fr = rr.collect(str(tmp_path), now=FIXED)
    rep = rr.build_report(bundle, fr, now=FIXED, reports_dir=str(tmp_path))
    for h in ("〇、数据源", "一、信号命中", "二、因子表现", "三、交易归因", "四、交易复盘",
              "五、组合与风险", "六、防过拟合", "七、规则化待办"):
        assert h in rep
    payload = rr.build_json_payload(bundle, fr, now=FIXED)
    s = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    assert json.loads(s)["actions"]


def test_formatters():
    assert rr._num(None) == "—"
    assert rr._pct(0.1234, 1) == "12.3%"
    assert rr._pct(-0.05, 1, signed=True) == "-5.0%"
    assert rr._state_cn("stale") == "陈旧"
