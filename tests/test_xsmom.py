# -*- coding: utf-8 -*-
"""G7（第31轮）截面动量多空 XSMOM 零网络确定性测试。

与时序动量（test_tsmom）区分：这里检验"每个调仓日跨品种排序、多最强空最弱"的
市场中性组合构造、非重叠调仓、分档/加权/绩效/成本/板块/裁决门，全部手算可核、无网络。
"""
import math

import xsmom_eval as xe


# --------------------------- 1) 远期收益无泄漏 ---------------------------
def test_forward_returns_handcalc():
    closes = [100.0, 105.0, 102.0, 108.0]
    fwd = xe.forward_returns(closes, (1, 3))
    assert abs(fwd[1][0] - 0.05) < 1e-12
    assert abs(fwd[3][0] - 0.08) < 1e-12
    # 尾部不足为 None，绝不拿未来填充
    assert fwd[1][-1] is None and fwd[3][1] is None and fwd[3][2] is None


# --------------------------- 2) 分档与加权 ---------------------------
def test_quantile_members_partition():
    ms = [{"fv": i, "f": 0.0, "vol": 0.01} for i in range(20)]
    bands = xe._quantile_members(ms, 5)
    assert len(bands) == 5 and sum(len(b) for b in bands) == 20  # 不重不漏
    assert [m["fv"] for m in bands[0]] == [0, 1, 2, 3]
    assert [m["fv"] for m in bands[-1]] == [16, 17, 18, 19]
    # 升序：档均因子单调
    means = [sum(m["fv"] for m in b) / len(b) for b in bands]
    assert all(b > a for a, b in zip(means, means[1:]))


def test_weighting_equal_ivol_and_degrade():
    members = [{"f": 0.10, "vol": 0.01}, {"f": 0.20, "vol": 0.04}]
    assert abs(xe._weighted_fwd(members, "f", "equal") - 0.15) < 1e-12
    # 反波动率：1/0.01=100,1/0.04=25 -> 权重 0.8/0.2 -> 0.12
    iv = xe._weighted_fwd(members, "f", "ivol")
    assert abs(iv - (0.8 * 0.10 + 0.2 * 0.20)) < 1e-12 and iv < 0.15
    # 波动率全缺失 -> 安全退回等权，不抛
    assert abs(xe._weighted_fwd([{"f": 1.0, "vol": None}, {"f": 3.0, "vol": None}],
                                "f", "ivol") - 2.0) < 1e-12


# --------------------------- 3) 截面组合：手算多空 ---------------------------
def _hand_panel(n_q=3):
    """两个调仓日（间隔20个全局日），每日6品种、因子与未来20日收益严格正相关。"""
    dates = ["d1"] + ["mx%02d" % i for i in range(19)] + ["d2"]
    by = {}
    for di, d in enumerate(("d1", "d2")):
        row = {}
        for k in range(6):
            row["S%d" % k] = {
                "sym": "S%d" % k, "sector": "黑色" if k < 3 else "有色",
                "z20": 0.1 * (k + 1), "ret20": 0.1 * (k + 1),
                "vol20": 0.01 + 0.001 * k,
                "fwd20": 0.01 * (k + 1) + 0.005 * di}   # d2 整体上移 0.005
        by[d] = row
    return dates, by


def test_cross_section_long_short_handcalc():
    dates, by = _hand_panel()
    pers = xe.cross_section_periods(dates, by, "z20", 20, 20, 3, 6, "equal", 20)
    assert len(pers) == 2                      # 非重叠：d1、d2 各一期
    p = pers[0]
    # 3档每档2个：Q1=(.01,.02)均.015；Q3=(.05,.06)均.055 -> ls=.04
    assert len(p["bands_mean"]) == 3
    assert abs(p["bands_mean"][0] - 0.015) < 1e-12
    assert abs(p["bands_mean"][-1] - 0.055) < 1e-12
    assert abs(p["ls"] - 0.04) < 1e-12
    assert abs(p["long"] - 0.055) < 1e-12 and abs(p["short_abs"] - 0.015) < 1e-12
    assert abs(p["short_pnl"] + 0.015) < 1e-12   # 做空收益=-最弱档绝对涨幅
    assert p["long_syms"] == ["S4", "S5"] and p["short_syms"] == ["S0", "S1"]
    # d2 整体上移 0.005：多空价差不变（市场 beta 被对冲），但 mkt 基准上移
    assert abs(pers[1]["ls"] - 0.04) < 1e-12
    assert abs(pers[1]["mkt"] - pers[0]["mkt"] - 0.005) < 1e-12


def test_cross_section_ivol_and_insufficient():
    dates, by = _hand_panel()
    piv = xe.cross_section_periods(dates, by, "z20", 20, 20, 3, 6, "ivol", 20)
    # 反波动率加权后仍是 top>bot、ls>0（低波动权重略变，方向不变）
    assert piv[0]["ls"] > 0
    # 当日品种不足 min_names 或不足 2*n_q -> 跳过
    assert xe.cross_section_periods(dates, by, "z20", 20, 20, 3, 99, "equal", 20) == []
    assert xe.cross_section_periods(dates, by, "z20", 20, 20, 5, 6, "equal", 20) == []
    # 因子/未来缺失成员被剔除：8 品种、3档门槛6，剔除1个后7个仍成组
    dates8 = ["g1"]
    by8 = {"g1": {}}
    for k in range(8):
        by8["g1"]["S%d" % k] = {"sym": "S%d" % k, "sector": "X", "z20": float(k),
                                "ret20": float(k), "vol20": 0.01, "fwd20": 0.01 * k}
    by8["g1"]["S7"]["z20"] = None
    p2 = xe.cross_section_periods(dates8, by8, "z20", 20, 20, 3, 6, "equal", 20)
    assert p2[0]["n"] == 7


def test_nonoverlap_step():
    # 30 个连续日、step=20：只在 di=0、di=20 调仓，期与期不重叠
    dates = ["d%03d" % i for i in range(30)]
    by = {d: {"S%d" % k: {"sym": "S%d" % k, "sector": "X", "z20": float(k),
                          "ret20": float(k), "vol20": 0.01, "fwd20": 0.01 * k}
             for k in range(6)} for d in dates}
    pers = xe.cross_section_periods(dates, by, "z20", 20, 20, 3, 6, "equal", 20)
    assert [p["date"] for p in pers] == ["d000", "d020"]


# --------------------------- 4) 绩效/成本/分档 ---------------------------
def test_perf_stats_handcalc():
    pers = [{"ls": 0.04}, {"ls": 0.02}]
    p = xe.perf_stats(pers, 20, 0.001, "ls")     # 两腿成本 2*0.001=0.002/期
    assert abs(p["gross_mean"] - 0.03) < 1e-12
    assert abs(p["net"][0] - 0.038) < 1e-12 and abs(p["net"][1] - 0.018) < 1e-12
    assert abs(p["net_mean"] - 0.028) < 1e-12 and p["win"] == 1.0
    # t = mean/(sample_std/√2)；net=[.038,.018]，sample_std=.0141421 -> t=2.8
    assert abs(p["net_t"] - 2.8) < 1e-9
    assert p["n"] == 2 and p["sharpe"] > 0
    assert xe.perf_stats([], 20) is None


def test_equity_drawdown():
    cum, mdd = xe._equity_dd([0.10, -0.20, 0.10])
    assert abs(cum - (1.1 * 0.8 * 1.1 - 1)) < 1e-12
    # 峰值1.1 -> 谷0.88，回撤 0.2
    assert abs(mdd - 0.2) < 1e-9


def test_bands_profile_monotonic():
    pers = [{"bands_mean": [0.01 * q for q in range(1, 6)]},
            {"bands_mean": [0.01 * q - 0.002 for q in range(1, 6)]}]
    bp = xe.bands_profile(pers, 5)
    assert bp["mono"] == 1.0 and bp["spread"] > 0 and bp["col_rank_ic"] > 0.99


def test_split_is_oos_ordered():
    pers = [{"date": "d%03d" % i, "ls": 0.01} for i in range(10)]
    isp, osp = xe.split_is_oos(pers, 0.3)
    assert len(isp) == 7 and len(osp) == 3
    assert isp[-1]["date"] <= osp[0]["date"]


# --------------------------- 5) 板块 ---------------------------
def test_sector_breakdown_and_internal():
    # 期1：多有色空黑色；期2：两腿都是有色（不含黑色）。
    pers = [{"sec_long": {"有色": 1.0}, "sec_short": {"黑色": 1.0}, "ls": 0.03},
            {"sec_long": {"有色": 1.0}, "sec_short": {"有色": 1.0}, "ls": 0.01}]
    exp, loso = xe.sector_breakdown(pers)
    # 两期多头都含有色(long=1.0)；空头黑色0.5/有色0.5 -> 有色net=+0.5、黑色net=-0.5
    assert abs(exp["有色"]["net"] - 0.5) < 1e-12 and abs(exp["黑色"]["net"] + 0.5) < 1e-12
    # drop=黑色：期1含黑色被剔、期2不含黑色为干净期 -> loso[黑色]=期2.ls
    assert abs(loso["黑色"] - 0.01) < 1e-12
    # drop=有色：两期都含有色 -> 无干净期、不入 loso
    assert "有色" not in loso
    # 板块内截面：每板块 6 品种、因子与未来严格正相关 -> 多空为正
    dates = ["d%03d" % i for i in range(20)]
    by = {}
    for d in dates:
        row = {}
        for s in range(12):
            sec = "A" if s < 6 else "B"
            row["V%02d" % s] = {"sym": "V%02d" % s, "sector": sec, "z20": float(s),
                                "ret20": float(s), "vol20": 0.01,
                                "fwd20": 0.01 * (s - 6)}   # 严格随因子递增
        by[d] = row
    internal = xe.sector_internal(dates, by, "z20", 20, 20, 3, 6, 20)
    assert set(internal) == {"A", "B"}
    assert all(v["mean"] > 0 for v in internal.values())


# --------------------------- 6) 裁决门 ---------------------------
def _good_perf():
    return {"n": 40, "gross_mean": 0.01, "net_mean": 0.009, "net_t": 2.3,
            "win": 0.6, "net_cum": 0.3, "annual": 0.1, "sharpe": 1.1, "max_dd": 0.05}


def _good_bands():
    return {"mono": 1.0, "spread": 0.02, "means": [-0.01, 0, 0, 0, 0.01], "col_rank_ic": 1.0}


def test_gate_pass_and_each_veto():
    ok, why = xe.gate_verdict(_good_perf(), _good_perf(), _good_bands(),
                              0.01, 0.01, {"有色": {"net": 0.2}}, 1.5, 0.75, 0.6)
    assert ok and not why
    # t 不足否决
    bad = dict(_good_perf(), net_t=0.6)
    ok1, why1 = xe.gate_verdict(bad, _good_perf(), _good_bands(), 0.01, 0.01, {}, 1.5, 0.75, 0.6)
    assert not ok1 and any("t=" in w for w in why1)
    # OOS 转负否决
    oos_bad = dict(_good_perf(), net_mean=-0.01)
    ok2, why2 = xe.gate_verdict(_good_perf(), oos_bad, _good_bands(), 0.01, 0.01, {}, 1.5, 0.75, 0.6)
    assert not ok2 and any("OOS" in w for w in why2)
    # 分档不单调否决
    bad_bands = dict(_good_bands(), mono=0.25, spread=-0.01)
    ok3, why3 = xe.gate_verdict(_good_perf(), _good_perf(), bad_bands, 0.01, 0.01, {}, 1.5, 0.75, 0.6)
    assert not ok3 and any("分档" in w for w in why3)
    # 两腿皆亏否决
    ok4, why4 = xe.gate_verdict(_good_perf(), _good_perf(), _good_bands(),
                                -0.01, -0.01, {}, 1.5, 0.75, 0.6)
    assert not ok4 and any("腿" in w for w in why4)
    # 单一板块偏置否决
    ok5, why5 = xe.gate_verdict(_good_perf(), _good_perf(), _good_bands(), 0.01, 0.01,
                                {"能化": {"net": 0.85}}, 1.5, 0.75, 0.6)
    assert not ok5 and any("板块偏置" in w for w in why5)
    # 无主组合否决
    ok6, why6 = xe.gate_verdict(None, None, _good_bands(), 0.01, 0.01, {}, 1.5, 0.75, 0.6)
    assert not ok6


# --------------------------- 7) 单品种面板暖机/无未来 ---------------------------
def _bars(closes):
    return [{"d": "2025-%02d-%02d" % (i // 28 % 12 + 1, i % 28 + 1),
             "o": c, "h": c + 1, "l": c - 1, "c": c, "v": 1000} for i, c in enumerate(closes)]


def test_build_symbol_points_warmup_no_future():
    closes = [100.0 + i * 0.2 for i in range(300)]
    pts = xe.build_symbol_points("测试", "有色", _bars(closes), (20, 60), (5, 20), 1023)
    # 暖机 t>=max(L)=60 起入面板（共 300-60=240 点）；尾部 fwd 未成熟的点保留但 fwd=None，
    # 由组合层 cross_section_periods 过滤，绝不拿未来填充（无未来函数）。
    assert len(pts) == 300 - 60
    p0 = pts[0]
    assert p0["z20"] is not None and p0["z60"] is not None and p0["fwd20"] is not None
    assert p0["sector"] == "有色"
    # 线性上行 z 为正
    assert p0["z60"] > 0
    # 最后 20 个点 fwd20 尚未兑现=None；但更早的点 fwd20 可得
    assert pts[-1]["fwd20"] is None and pts[-1]["fwd5"] is None
    assert pts[-21]["fwd20"] is not None


# --------------------------- 8) 合成趋势面板：端到端为正、报告跑通 ---------------------------
def test_synthetic_trend_panel_positive():
    pts = xe._synthetic_panel("trend")
    dates, by = xe.build_panel(pts)
    pers = xe.cross_section_periods(dates, by, "z60", 20, 60, 5, 16, "equal", 20)
    pf = xe.perf_stats(pers, 20, 0.0, "ls")
    bp = xe.bands_profile(pers, 5)
    assert pf["gross_mean"] > 0 and pf["net_t"] > 0
    assert bp["mono"] == 1.0 and bp["spread"] > 0
    text, sidecar, verdict = xe.build_report(
        pts, [], dates, by, (20, 60), (5, 20), 60, 20, 5, 16, 6,
        0.3, 1.5, 0.75, 0.6, 0.0003, 320, "equal")
    assert "XSMOM" in text and verdict["ok"] is True
    assert sidecar["n_symbols"] == 20 and sidecar["grid"]


def test_build_report_empty_safe():
    # 极端高 min_names -> 主组合为空也必须出报告、不抛
    pts = xe._synthetic_panel("trend", n_sym=20, n_days=120)
    dates, by = xe.build_panel(pts)
    text, _sc, verdict = xe.build_report(
        pts, [], dates, by, (20, 60), (5, 20), 60, 20, 5, 999, 6,
        0.3, 1.5, 0.75, 0.6, 0.0003, 120, "equal")
    assert "无可用调仓期" in text and verdict["ok"] is False


# ================= 第32轮：板块池条件化 / 多头腿 / 双样本稳健 =================
def _scope_panel():
    """12 品种：有色6(V00..V05,fwd=0..0.05)、能化6(V06..V11,fwd=0.06..0.11)，单调仓日。"""
    dates = ["g1"]
    by = {"g1": {}}
    for k in range(12):
        sec = "有色" if k < 6 else "能化"
        by["g1"]["V%02d" % k] = {"sym": "V%02d" % k, "sector": sec, "z60": float(k),
                                 "ret60": float(k), "vol60": 0.01, "fwd20": 0.01 * k}
    return dates, by


def test_sector_scope_and_long_excess_handcalc():
    dates, by = _scope_panel()
    # 板块池：只在有色6个内分3档（每档2），成员全部为有色
    p = xe.cross_section_periods(dates, by, "z60", 20, 60, 3, 6, "equal", 20,
                                 sector_scope=("有色",))
    assert len(p) == 1 and p[0]["n"] == 6
    assert all(s in ("V00", "V01", "V02", "V03", "V04", "V05")
               for s in p[0]["long_syms"] + p[0]["short_syms"])
    # top=V04,V05(.04,.05)均.045；bot=V00,V01(.00,.01)均.005；池内mkt=有色6均=.025
    assert abs(p[0]["long"] - 0.045) < 1e-12
    assert abs(p[0]["short_abs"] - 0.005) < 1e-12
    assert abs(p[0]["mkt"] - 0.025) < 1e-12
    assert abs(p[0]["long_excess"] - 0.02) < 1e-12
    assert abs(p[0]["ls"] - 0.04) < 1e-12
    # 全市场口径 mkt=12 均=0.055（验证池内外基准不同）
    pall = xe.cross_section_periods(dates, by, "z60", 20, 60, 3, 12, "equal", 20)
    assert abs(pall[0]["mkt"] - 0.055) < 1e-12


def test_leg_cost_one_vs_two():
    dates, by = _scope_panel()
    p = xe.cross_section_periods(dates, by, "z60", 20, 60, 3, 6, "equal", 20,
                                 sector_scope=("有色",))
    # lex=多头超额单腿扣1次往返；ls=多空两腿扣2次
    pf_lex = xe.perf_stats(p, 20, 0.0003, "long_excess")
    assert abs(pf_lex["net_mean"] - (0.02 - 0.0003)) < 1e-12
    pf_long = xe.perf_stats(p, 20, 0.0003, "long")
    assert abs(pf_long["net_mean"] - (0.045 - 0.0003)) < 1e-12
    pf_ls = xe.perf_stats(p, 20, 0.0003, "ls")
    assert abs(pf_ls["net_mean"] - (0.04 - 0.0006)) < 1e-12


def test_truncate_dates():
    seq = list(range(10))
    assert xe.truncate_dates(seq, 3) == [7, 8, 9]
    assert xe.truncate_dates(seq, 0) == seq          # 0=不截断
    assert xe.truncate_dates(seq, 99) == seq         # 超长=原样
    assert seq == list(range(10))                   # 不改原序列


def _toy_perf(t, m=0.01, n=40):
    return {"n": n, "gross_mean": m, "net_mean": m, "net_t": t, "win": 0.6,
            "net_cum": 0.2, "annual": 0.1, "sharpe": 1.0, "max_dd": 0.05,
            "gross": [], "net": []}


def test_robust_verdict_branches():
    # 两窗都 t 达标、无衰减、长窗样本量足够多 -> 稳健
    ok, why = xe.robust_verdict({"windows": {"近": _toy_perf(2.0, n=40), "长": _toy_perf(1.8, n=100)}},
                                1.5, 0.5)
    assert ok and not why
    # 长窗 t 比短窗衰减超容差 -> 不稳健
    ok1, why1 = xe.robust_verdict({"windows": {"近": _toy_perf(2.2, n=40), "长": _toy_perf(1.0, n=100)}},
                                  1.5, 0.5)
    assert not ok1 and any("衰减" in w for w in why1)
    # 一窗为负 -> 不稳健
    ok2, why2 = xe.robust_verdict({"windows": {"近": _toy_perf(2.0, n=40), "长": _toy_perf(2.0, -0.01, 100)}},
                                  1.5, 0.5)
    assert not ok2 and any("为负" in w for w in why2)
    # 长窗期数没比短窗多（板块上市晚、两窗同源小样本）-> 即便两窗 t 都高也不稳健
    oks, whys = xe.robust_verdict({"windows": {"近": _toy_perf(2.01, 0.027, 25), "长": _toy_perf(2.01, 0.027, 25)}},
                                  1.5, 0.5)
    assert not oks and any("同源" in w for w in whys)
    # 窗口不足2个 -> 不稳健
    ok3, why3 = xe.robust_verdict({"windows": {"近": _toy_perf(2.0), "长": None}}, 1.5, 0.5)
    assert not ok3 and any("窗口不足" in w for w in why3)


def test_conditional_scan_structure():
    pts = xe._synthetic_panel("trend")
    dates, by = xe.build_panel(pts)
    short = xe.truncate_dates(dates, 160)
    cands = [("基线", None, "ls"), ("板块0", ("板块0",), "ls"), ("多头超额", None, "lex")]
    scan = xe.conditional_scan([("近", (short, by)), ("长", (dates, by))],
                               "z60", 60, 20, 5, 16, 0.0003, cands)
    assert list(scan) == ["基线", "板块0", "多头超额"]
    # 板块0只有5个品种、分5档需≥10 -> 样本不足 None；其余两窗都有 perf
    assert scan["板块0"]["windows"]["近"] is None and scan["板块0"]["windows"]["长"] is None
    assert scan["基线"]["windows"]["近"] is not None and scan["基线"]["windows"]["长"] is not None
    assert scan["多头超额"]["leg"] == "lex"


def test_build_report_conditional_chapter():
    pts = xe._synthetic_panel("trend")
    dates, by = xe.build_panel(pts)
    cands = [("全市场·多空", None, "ls"), ("全市场·多头超额", None, "lex")]
    # 带 robust_panel -> 出第五章、sidecar.conditional 齐全
    text, sc, _ = xe.build_report(
        pts, [], dates, by, (20, 60), (5, 20), 60, 20, 5, 16, 6,
        0.3, 1.5, 0.75, 0.6, 0.0003, 320, "equal",
        robust_panel=(dates, by), candidates=cands, cond_min=16,
        decay_tol=0.5, main_days=160, main_scope=None, main_leg="ls")
    assert "五、条件化" in text and "双样本稳健" in text
    assert set(sc["conditional"]) == {"全市场·多空", "全市场·多头超额"}
    # 主组合 --leg lex：二章标题标注腿模式，且净口径走多头超额（单腿）
    text_lex, _, _ = xe.build_report(
        pts, [], dates, by, (20, 60), (5, 20), 60, 20, 5, 16, 6,
        0.3, 1.5, 0.75, 0.6, 0.0003, 320, "equal", main_leg="lex")
    assert "腿=多头超额" in text_lex
