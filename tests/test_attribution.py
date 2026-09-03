# -*- coding: utf-8 -*-
"""G28（第35轮）因子收益归因 + BHB 板块归因 零网络确定性测试。

全部手算可核、不连 monitor.db：
  - 方向化暴露（做空翻转、动态原油键归一、坏行安全）
  - 带截距 OLS 精确恢复已知系数 / 奇异安全
  - 多因子加法归因严格闭合 mean(y)=α+Σβ·mean(x)、零方差列剔除、空样本降级
  - BHB 三效应手算 + AR+SR+IR=组合−基准 恒等式（含随机 fuzz）
  - 板块统计 wp 归一 / rb 无方向、基准权重归一、累计曲线末端闭合、IS-OOS 有序
  - 端到端 build_report 结构与 sidecar JSON 安全
"""
import json
import math
import random

import attribution as at

KEYS = ["新闻消息面", "原油联动", "机构动向", "日线动量", "技术共振",
        "分钟共振", "盘中动量", "量仓资金", "基本面"]


def _ev(y, x, sector="黑色", d=1, band="分批", ts="2026-01-01", sym="RB"):
    return {"y": y, "x": dict(x), "dir": d, "sector": sector,
            "band": band, "ts": ts, "sym": sym}


def _linear_events(n=60, seed=1):
    """y = 0.001 + 2*A - 1*B + 0*C 的无噪声样本，C 恒 0（零方差应被剔除）。"""
    rnd = random.Random(seed)
    evs = []
    for i in range(n):
        a = rnd.randint(-2, 2)
        b = rnd.randint(-3, 3)
        ts = "2026-%02d-%02d" % (i // 28 + 1, i % 28 + 1)
        evs.append(_ev(0.001 + 2.0 * a - 1.0 * b,
                       {"A": float(a), "B": float(b), "C": 0.0}, ts=ts))
    return evs


# --------------------------- 1) 事件解析：方向化暴露 ---------------------------
def test_parse_event_row_signed_exposure():
    row = {"direction_int": -1, "ret": -0.01,
           "parts_json": json.dumps({"新闻消息面": 2.0, "日线动量": -1.0}),
           "cat": "有色", "sym": "CU", "score_band": "轻仓",
           "entry_ts": "2026-01-01", "horizon_min": 1440}
    e = at.parse_event_row(row, KEYS)
    assert e["dir"] == -1 and abs(e["y"] + 0.01) < 1e-12
    assert e["x"]["新闻消息面"] == -2.0      # 做空：正 part → 负暴露
    assert e["x"]["日线动量"] == +1.0       # 做空：负 part → 正暴露
    assert e["sector"] == "有色"


def test_parse_event_row_canon_oil_and_bad():
    row = {"direction_int": 1, "ret": 0.0,
           "parts_json": json.dumps({"原油联动(w=0.50)": 1.25}),
           "cat": "能源化工", "sym": "FU", "score_band": "分批",
           "entry_ts": "t", "horizon_min": 30}
    e = at.parse_event_row(row, KEYS)
    assert "原油联动" in e["x"] and abs(e["x"]["原油联动"] - 1.25) < 1e-12
    assert at.parse_event_row({"direction_int": 0}, KEYS) is None       # 非法方向
    assert at.parse_event_row({"direction_int": 1, "ret": "x",
                               "parts_json": "{}"}, KEYS) is None       # 坏收益


# --------------------------- 2) OLS 求解 ---------------------------
def test_ols_fit_recovers_coefficients():
    X, y = [], []
    for i in range(30):
        x1 = i - 15
        x2 = (i % 5) - 2
        X.append([float(x1), float(x2)])
        y.append(0.5 + 0.2 * x1 - 0.3 * x2)
    beta = at.ols_fit(X, y)
    assert beta is not None
    assert abs(beta[0] - 0.5) < 1e-9
    assert abs(beta[1] - 0.2) < 1e-9 and abs(beta[2] + 0.3) < 1e-9


def test_ols_fit_singular_and_undersize():
    assert at.ols_fit([[1.0], [1.0]], [0.1, 0.2]) is None               # 样本不足
    assert at.ols_fit([], []) is None
    # 完全共线（两列相同）→ 正规方程奇异返回 None
    X = [[1.0, 1.0], [2.0, 2.0], [3.0, 3.0], [4.0, 4.0]]
    y = [1.0, 2.0, 3.0, 4.0]
    assert at.ols_fit(X, y) is None


# --------------------------- 3) 多因子加法归因闭合 ---------------------------
def test_factor_attribution_closes_and_recovers():
    evs = _linear_events()
    a = at.factor_attribution(evs, ["A", "B", "C"], x_eps=0.05)
    assert a["n"] == len(evs)
    assert "C" in a["dropped"]                       # 零方差列剔除
    assert set(a["used"]) == {"A", "B"}
    assert abs(a["alpha"] - 0.001) < 1e-10
    assert abs(a["beta"]["A"] - 2.0) < 1e-9
    assert abs(a["beta"]["B"] + 1.0) < 1e-9
    # 严格闭合：mean(y)=α+Σβ·mean(x)
    assert abs(a["closure_resid"]) < 1e-12
    assert a["r2"] > 0.999
    # 每行贡献=β×平均暴露，行字段齐全
    for r in a["rows"]:
        assert abs(r["contrib"] - r["beta"] * r["mean_x"]) < 1e-12
        assert {"factor", "n", "beta", "tstat", "mean_x", "contrib",
                "ic", "win_support"} <= set(r)


def test_factor_attribution_empty_and_allzero():
    z = at.factor_attribution([], ["A"])
    assert z["n"] == 0 and z["rows"] == [] and z["used"] == []
    z2 = at.factor_attribution([_ev(0.01, {"A": 0.0}),
                                _ev(0.03, {"A": 0.0})], ["A"])
    assert z2["used"] == [] and abs(z2["alpha"] - 0.02) < 1e-12
    assert abs(z2["closure_resid"]) < 1e-12


def test_factor_attribution_support_winrate():
    # A 强支持时 y 恒正、反对时 y 恒负 → 支持胜率 100%，IC>0
    evs = [_ev(0.01, {"A": 1.0}) for _ in range(10)] + \
          [_ev(-0.01, {"A": -1.0}) for _ in range(10)]
    a = at.factor_attribution(evs, ["A"], x_eps=0.05)
    row = a["rows"][0]
    assert row["win_support"] == 1.0 and row["ic"] > 0.99
    assert row["avg_support"] > 0 and row["avg_against"] < 0


# --------------------------- 4) BHB 手算 + 恒等式 ---------------------------
def test_bhb_handcalc_two_sectors():
    stats = {"S1": {"wp": 0.6, "rp": 0.10, "rb": 0.08},
             "S2": {"wp": 0.4, "rp": 0.02, "rb": 0.04}}
    wb = {"S1": 0.5, "S2": 0.5}
    r = at.bhb(stats, wb)
    s1, s2 = r["sectors"]
    assert abs(s1["alloc"] - 0.008) < 1e-12      # (0.6-0.5)*0.08
    assert abs(s1["select"] - 0.010) < 1e-12     # 0.5*(0.10-0.08)
    assert abs(s1["inter"] - 0.002) < 1e-12      # 0.1*0.02
    assert abs(s2["alloc"] + 0.004) < 1e-12      # -0.1*0.04
    assert abs(s2["select"] + 0.010) < 1e-12     # 0.5*(0.02-0.04)
    assert abs(s2["inter"] - 0.002) < 1e-12      # -0.1*-0.02
    assert abs(r["port_ret"] - 0.068) < 1e-12
    assert abs(r["bench_ret"] - 0.060) < 1e-12
    assert abs(r["excess"] - 0.008) < 1e-12
    # 恒等式 AR+SR+IR = 组合−基准
    assert abs(r["total"] - r["excess"]) < 1e-12
    assert abs(r["closure_resid"]) < 1e-12


def test_bhb_identity_random_fuzz():
    rnd = random.Random(7)
    for _ in range(200):
        sectors = ["S%d" % i for i in range(rnd.randint(2, 6))]
        wp = [rnd.random() for _ in sectors]
        tot = sum(wp)
        wp = [v / tot for v in wp]
        wb = [rnd.random() for _ in sectors]
        tot = sum(wb)
        wb = [v / tot for v in wb]
        stats = {s: {"wp": wp[i], "rp": rnd.uniform(-0.05, 0.05),
                     "rb": rnd.uniform(-0.05, 0.05)}
                 for i, s in enumerate(sectors)}
        r = at.bhb(stats, {s: wb[i] for i, s in enumerate(sectors)})
        assert abs(r["total"] - r["excess"]) < 1e-12
        assert abs(r["closure_resid"]) < 1e-12


# --------------------------- 5) 板块统计 / 基准权重 ---------------------------
def test_events_to_sector_stats():
    es = [_ev(0.02, {"A": 1}, "S1", d=1), _ev(0.04, {"A": 1}, "S1", d=1),
          _ev(-0.02, {"A": 1}, "S2", d=-1)]
    st = at.events_to_sector_stats(es)
    assert abs(st["S1"]["wp"] - 2 / 3) < 1e-12 and abs(st["S2"]["wp"] - 1 / 3) < 1e-12
    assert abs(st["S1"]["rp"] - 0.03) < 1e-12
    assert abs(st["S2"]["rp"] + 0.02) < 1e-12     # 方向化
    assert abs(st["S2"]["rb"] - 0.02) < 1e-12     # 无方向绝对涨跌=y/dir


def test_universe_sector_weights_normalized():
    w = at.universe_sector_weights(["黑色", "有色"])
    assert abs(sum(w.values()) - 1.0) < 1e-12
    # 黑色8只有色11只 → 8/19, 11/19
    assert abs(w["黑色"] - 8 / 19) < 1e-12 and abs(w["有色"] - 11 / 19) < 1e-12
    full = at.universe_sector_weights()
    assert abs(sum(full.values()) - 1.0) < 1e-12


# --------------------------- 6) 累计曲线 / IS-OOS / 分组 ---------------------------
def test_factor_curve_terminal_closure():
    evs = _linear_events()
    a = at.factor_attribution(evs, ["A", "B", "C"])
    curve = at.factor_curve(evs, a, ["A", "B", "C"])
    assert len(curve) == len(evs) and curve[0]["idx"] == 1
    last = curve[-1]
    fac = sum(last["cum_" + f] for f in a["used"])
    assert abs((last["cum_alpha"] + fac) - last["cum_total"]) < 1e-9
    # 累计总收益=Σy
    assert abs(last["cum_total"] - sum(e["y"] for e in evs)) < 1e-9


def test_is_oos_split_ordered():
    evs = _linear_events()
    is_ev, oos_ev = at.is_oos_split(evs, 0.3)
    assert len(is_ev) + len(oos_ev) == len(evs)
    assert is_ev[-1]["ts"] <= oos_ev[0]["ts"]
    assert abs(len(oos_ev) / len(evs) - 0.3) < 0.05


def test_group_mean():
    evs = [_ev(0.01, {}, d=1), _ev(-0.01, {}, d=1), _ev(0.02, {}, d=-1)]
    g = at.group_mean(evs, "dir")
    assert g[1]["n"] == 2 and abs(g[1]["mean_y"]) < 1e-12
    assert g[-1]["n"] == 1 and abs(g[-1]["win"] - 1.0) < 1e-12


# --------------------------- 7) 端到端 attribute_horizon / build_report ---------------------------
def test_attribute_horizon_structure():
    evs = _linear_events(80)
    for e in evs:                       # 分散到两板块，保证 BHB 可算
        pass
    a = at.attribute_horizon(evs, ["A", "B", "C"])
    assert abs(a["bhb"]["closure_resid"]) < 1e-12
    assert abs(a["closure_resid"]) < 1e-12
    assert "is" in a and "oos" in a
    assert a["by_dir"] and a["by_band"]
    assert isinstance(a["monthly_bhb"], list)


def test_build_report_end_to_end_json_safe():
    evs = _linear_events(90)
    data = {30: evs, 1440: evs}
    text, sc = at.build_report(data, ["A", "B", "C"], 1440)
    assert "多因子加法归因" in text and "BHB 板块归因" in text and "闭合误差" in text
    assert "主周期" in text and "对照周期" in text
    assert set(sc["horizons"]) == {30, 1440}
    # sidecar 必须无 NaN/Inf，可被标准 JSON 序列化
    js = json.dumps(at._json_safe(sc), allow_nan=False)
    assert "closure_resid" in js
    # 样本不足时只计数不下结论、不抛异常
    t2, s2 = at.build_report({30: evs[:3]}, ["A", "B", "C"], 30)
    assert s2["horizons"][30]["enough"] is False and "样本不足" in t2
