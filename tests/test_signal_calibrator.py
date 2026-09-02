# -*- coding: utf-8 -*-
"""历史胜率校准器回归（第19轮 A3：贝叶斯平滑/四级回退/乘子裁剪/影子模式）。"""
import config
import signal_calibrator as sc


# ---------------- 纯函数 ----------------
def test_canonical_factor():
    assert sc.canonical_factor("原油联动(w=0.30)") == "原油联动"
    assert sc.canonical_factor("日线动量（w=0.4）") == "日线动量"
    assert sc.canonical_factor("") == sc.FACTOR_FALLBACK
    assert sc.canonical_factor(None) == sc.FACTOR_FALLBACK


def test_dominant_factor():
    parts = {"日线动量": 1.5, "消息面": 0.5}
    assert sc.dominant_factor(parts, 1) == "日线动量"
    # 做空时负贡献最大者才是支持做空的主导因子
    assert sc.dominant_factor({"日线动量": -1.5, "消息面": 0.2}, -1) == "日线动量"
    # 没有一个因子沿该方向为正贡献 -> 综合
    assert sc.dominant_factor({"日线动量": 1.0}, -1) == sc.FACTOR_FALLBACK
    assert sc.dominant_factor(parts, 0) == sc.FACTOR_FALLBACK
    assert sc.dominant_factor("notdict", 1) == sc.FACTOR_FALLBACK


def test_bayes_winrate():
    # 先验强度2、先验胜率0.5：(20+1)/(25+2)
    assert abs(sc.bayes_winrate(20, 25) - 21 / 27) < 1e-12
    assert sc.bayes_winrate(0, 0) == 0.5
    assert sc.bayes_winrate(-1, 5) == 0.5          # 非法
    assert sc.bayes_winrate(10, 5) == 0.5          # hits>n 非法


def test_mult_from_winrate():
    assert sc.mult_from_winrate(0.5) == 1.0
    assert abs(sc.mult_from_winrate(1.0) - config.CALIBRATOR_MULT_HI) < 1e-12   # 触上限1.2
    assert abs(sc.mult_from_winrate(0.0) - config.CALIBRATOR_MULT_LO) < 1e-12   # 触下限0.5
    assert sc.mult_from_winrate("bad") == 1.0


def test_band_of_score():
    assert sc._band_of_score(1.0) == "观望"
    assert sc._band_of_score(3.0) == "轻仓"
    assert sc._band_of_score(5.0) == "分批"
    assert sc._band_of_score(-7.0) == "强信号"


def _rows(n, hits, band="分批", d=1, fac=None, start=0):
    out = []
    for i in range(n):
        out.append({"direction_int": d, "score_band": band,
                    "hit": 1 if i < hits else 0, "ret": 0.01,
                    "parts_json": fac if fac is not None else {}})
    return out


def test_lookup_calibrated_at_finest_level():
    cal = sc.SignalCalibrator(rows=_rows(25, 20), min_n=20)
    info = cal.lookup(5.0, direction_int=1)
    assert info["calibrated"] is True
    assert info["n"] == 25
    assert info["level"] == sc.LV_FACTOR
    assert abs(info["winrate"] - 21 / 27) < 1e-12
    assert abs(info["mult"] - 1.2) < 1e-12          # 高胜率触乘子上限
    assert "影子" in cal.format_note(info, 1)


def test_small_sample_not_calibrated():
    cal = sc.SignalCalibrator(rows=_rows(5, 4), min_n=20)
    info = cal.lookup(5.0, direction_int=1)
    assert info["calibrated"] is False and info["mult"] == 1.0
    assert cal.format_note(info, 1) == ""


def test_four_level_fallback_to_band():
    # 每个主导因子组都 <20，但方向×分档层合计 25 -> 回退到 LV_BAND
    rows = _rows(10, 8, fac={"日线动量": 1.0}) + _rows(15, 12, fac={"消息面": 1.0})
    cal = sc.SignalCalibrator(rows=rows, min_n=20)
    info = cal.lookup(5.0, direction_int=1, parts={"日线动量": 1.0})
    assert info["calibrated"] is True and info["level"] == sc.LV_BAND and info["n"] == 25


def test_fallback_to_direction_then_all():
    # 分档层不足、方向层充足
    rows = _rows(25, 15, band="分批") + _rows(10, 6, band="轻仓")
    cal = sc.SignalCalibrator(rows=rows, min_n=20)
    # 查"强信号"档：该档0条，分批/轻仓各自<20？方向层(1,)=35 充足 -> LV_DIR
    info = cal.lookup(8.0, direction_int=1)
    assert info["calibrated"] is True and info["level"] == sc.LV_DIR


def test_disabled_and_neutral_direction():
    cal = sc.SignalCalibrator(rows=_rows(30, 25), enabled=False)
    assert cal.lookup(5.0, direction_int=1)["calibrated"] is False
    cal2 = sc.SignalCalibrator(rows=_rows(30, 25))
    assert cal2.lookup(0.0)["calibrated"] is False       # 中性方向不校准


def test_annotate_row_safe():
    cal = sc.SignalCalibrator(rows=_rows(25, 20))
    row = {"score": 5.0, "parts": {}}
    cal.annotate_row(row)
    assert row["calib"]["calibrated"] is True
    # 异常输入不抛出
    bad = sc.SignalCalibrator(rows=[])
    bad.annotate_row({"score": "x"})
