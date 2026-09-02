# -*- coding: utf-8 -*-
"""G7（第30轮）多窗口时序动量 TSMOM(63/126/252) 零网络确定性测试。

覆盖四块：
  1) futures_data 的长窗收益/波动调整 z/合成/序列 纯函数手算与降级；
  2) compute_indicators 新增影子键，且旧指标（截断140根口径）逐字节不变（铁律②）；
  3) analyzer 影子字段绝不进 parts、不改综合分（铁律①，开关两态 score/parts 相等）；
  4) tools/tsmom_eval 统计：远期收益无泄漏、OLS 残差正交、面板暖机、IS/OOS 切分。
"""
import math

import config
import futures_data as fd
import tsmom_eval as te


# --------------------------- 1) 纯函数 ---------------------------
def _line_closes(n, p0=100.0, step=0.1):
    return [p0 + i * step for i in range(n)]


def test_lookback_return_handcalc():
    closes = _line_closes(300)
    # end=299, lookback=63 -> closes[299]/closes[236]-1
    assert abs(fd._lookback_return(closes, 299, 63) - (closes[299] / closes[236] - 1)) < 1e-12
    # 历史不足 / 非法窗口 -> None
    assert fd._lookback_return(closes, 10, 63) is None
    assert fd._lookback_return(closes, 10, 0) is None
    # 价格非正安全
    assert fd._lookback_return([0.0, 1.0], 1, 1) is None


def test_tsmom_features_values_and_z():
    closes = _line_closes(300)
    f = fd.tsmom_features(closes, lookbacks=(63, 126, 252))
    assert set(f) == {"ret63", "ret126", "ret252", "tsmom63", "tsmom126",
                      "tsmom252", "blend", "n_valid"}
    # z = ret / (窗口日收益样本std * sqrt(ann))，手算复核 z63（原始 z 不裁剪，clip 只在 blend 聚合时）
    end = len(closes) - 1
    sd = fd._window_std(closes, end, 63)
    expect_z = f["ret63"] / (sd * math.sqrt(config.TSMOM_ANN))
    assert abs(f["tsmom63"] - expect_z) < 1e-9
    # 三窗口齐全、线性上行 z 全为正
    assert f["n_valid"] == 3
    assert f["tsmom63"] > 0 and f["tsmom126"] > 0 and f["tsmom252"] > 0
    # blend 经 tanh 压缩落在 (-1,1)
    assert -1 < f["blend"] < 1


def test_tsmom_insufficient_and_flat():
    # 历史不足：全部 None、blend None、n_valid=0，绝不编造
    f = fd.tsmom_features([100.0, 101.0, 102.0], lookbacks=(63, 126, 252))
    assert f["ret252"] is None and f["tsmom252"] is None and f["blend"] is None
    assert f["n_valid"] == 0
    # 常数序列：累计收益0、零波动 -> z 不可得（None），但不炸
    fc = fd.tsmom_features([100.0] * 260, lookbacks=(63, 126, 252))
    assert fc["ret252"] == 0.0 and fc["tsmom252"] is None and fc["blend"] is None
    # 空序列安全
    assert fd.tsmom_features([])["n_valid"] == 0


def test_tsmom_series_alignment():
    closes = _line_closes(300)
    ser = fd.tsmom_series(closes, lookbacks=(63, 126, 252))
    # 与输入等长
    assert all(len(v) == 300 for v in ser.values())
    # 暖机：t<最短窗63时全 None；t=63 起 ret63 可得、blend 随之可用；n_valid 随更长窗口逐步增加
    assert ser["blend"][62] is None and ser["ret63"][62] is None
    assert ser["blend"][63] is not None and ser["n_valid"][63] == 1
    assert ser["n_valid"][126] == 2 and ser["n_valid"][252] == 3
    # 序列某时点与单点函数一致
    one = fd.tsmom_at(closes, 280, lookbacks=(63, 126, 252))
    assert abs(ser["tsmom126"][280] - one["tsmom126"]) < 1e-12


# --------------------------- 2) compute_indicators 增量且旧值不变 ---------------------------
def _bars(closes):
    return [{"d": "2026-%02d-%02d" % (i // 28 % 12 + 1, i % 28 + 1),
             "o": c, "h": c + 1, "l": c - 1, "c": c, "v": 1000} for i, c in enumerate(closes)]


def test_compute_indicators_adds_g7_keys():
    closes = _line_closes(300)
    ind = fd.compute_indicators(_bars(closes))
    for k in ("ret63", "ret126", "ret252", "tsmom63", "tsmom126",
              "tsmom252", "tsmom_blend", "tsmom_n_valid"):
        assert k in ind
    assert ind["tsmom_n_valid"] == 3 and ind["ret252"] is not None


def test_compute_indicators_old_tech_byte_identical():
    """新增 G7 不得改变 max_bars=140 截断口径下的任何旧指标（内联旧算法对比）。"""
    closes = _line_closes(300, step=0.37)
    bars = _bars(closes)
    ind = fd.compute_indicators(bars)
    # 内联"旧版"输入：过滤正收盘 -> 截最后140根 -> technical_profile
    old_bars = [b for b in bars if fd._f(b.get("c")) > 0][-140:]
    oc = [fd._f(b["c"]) for b in old_bars]
    oh = [fd._f(b["h"]) for b in old_bars]
    ol = [fd._f(b["l"]) for b in old_bars]
    old_tech = fd.technical_profile(oc, oh, ol)
    new_tech = ind["tech"]
    # 旧 tech 全字段逐字节相等（浮点全等，因为输入序列与算法未变）
    for k, v in old_tech.items():
        if isinstance(v, float):
            assert new_tech[k] == v, k
        else:
            assert new_tech[k] == v, k
    assert ind["ret5"] == old_tech["ret5"] and ind["ret20"] == old_tech["ret20"]


def test_compute_indicators_short_history_safe():
    # 只有 50 根：旧指标仍可算，长窗为 None，不抛
    closes = _line_closes(50)
    ind = fd.compute_indicators(_bars(closes))
    assert ind["ret252"] is None and ind["tsmom_blend"] is None
    assert ind["ret5"] != 0  # 短窗动量照常


# --------------------------- 3) analyzer 影子不改分（铁律） ---------------------------
def _analyze_once(flat_calendar):
    import analyzer
    closes = _line_closes(300, p0=3000.0, step=1.5)
    ind = fd.compute_indicators(_bars(closes))
    ind["intraday"] = {}
    meta = dict(config.VARIETIES["螺纹钢"])
    quote = {"latest": closes[-1], "chg_pct": 0.05, "volume": 1e6, "open_interest": 2e6}
    return analyzer.analyze_variety("螺纹钢", meta, quote, ind, True, 0.5, [], 0.0, 0.0)


def test_analyzer_shadow_does_not_change_score(flat_calendar):
    import analyzer
    r_on = _analyze_once(flat_calendar)
    assert r_on["tsmom_shadow"] is not None
    expect_keys = {"ret63", "ret126", "ret252", "tsmom63", "tsmom126",
                   "tsmom252", "blend", "n_valid"}
    assert set(r_on["tsmom_shadow"]) == expect_keys
    # 影子绝不进 parts（parts 之和=综合分口径）
    assert all("tsmom" not in k and "ret63" not in k for k in r_on["parts"])
    score_on, parts_on = r_on["score"], dict(r_on["parts"])
    old = config.TSMOM_SHADOW
    try:
        config.TSMOM_SHADOW = False
        r_off = _analyze_once(flat_calendar)
        assert r_off["tsmom_shadow"] is None
        # 开关两态综合分与分项逐值相等
        assert r_off["score"] == score_on
        assert r_off["parts"] == parts_on
    finally:
        config.TSMOM_SHADOW = old


# --------------------------- 4) tools 统计 ---------------------------
def test_forward_returns_no_leak():
    closes = [100.0, 110.0, 121.0]
    fwd = te.forward_returns(closes, (1, 2))
    assert abs(fwd[1][0] - 0.10) < 1e-12
    assert abs(fwd[2][0] - 0.21) < 1e-12
    assert fwd[1][-1] is None and fwd[2][-1] is None and fwd[2][1] is None


def test_ols_residual_orthogonal():
    x1 = [1.0, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    x2 = [2.0, 1, 4, 3, 6, 5, 8, 7, 10, 9]
    tgt = [3 * a - 2 * b + 1 for a, b in zip(x1, x2)]
    resid = te.ols_residual(tgt, [x1, x2])
    # 完全线性关系 -> 残差全 0
    assert all(abs(r) < 1e-9 for r in resid)
    # 奇异矩阵退化为减均值，不抛
    rr = te.ols_residual([1.0, 2, 3], [[1.0, 1, 1]])
    assert abs(sum(rr)) < 1e-9


def test_records_warmup_and_no_future():
    closes = _line_closes(400)
    raw = _bars(closes)
    recs = te.build_symbol_records("X", raw, (63, 126, 252), (5, 20, 60))
    # 暖机 t>=252 且尾部60根无未来收益被剔除：t∈[252, n-60-1]，共 n-252-60
    assert len(recs) == 400 - 252 - 60
    r0 = recs[0]
    assert r0["ret252"] is not None and r0["fwd60"] is not None and r0["z63"] is not None


def test_split_is_oos():
    recs = [{"date": "2026-%02d" % (i % 12 + 1), "sym": "X"} for i in range(100)]
    isr, oosr = te.split_is_oos(recs, 0.3)
    assert len(isr) == 70 and len(oosr) == 30 and len(isr) + len(oosr) == 100


def test_eval_factor_monotone_panel():
    # 因子与未来收益完全同号正相关 -> RankIC=1、单调性1；x=0 一点不计方向命中，故 hit≈0.99
    recs = [{"z63": float(i - 50), "fwd20": (i - 50) * 0.01} for i in range(100)]
    m = te.eval_factor_horizon(recs, "z63", 20, 5)
    assert abs(m["rank_ic"] - 1.0) < 1e-9 and m["mono"] == 1.0 and m["hit"] > 0.98
