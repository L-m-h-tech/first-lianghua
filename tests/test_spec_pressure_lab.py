# -*- coding: utf-8 -*-
"""第57轮 G24续 投机/套保压力代理 tools/spec_pressure_lab.py 的零网络/零DB 单测（只测纯函数与渲染）。"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOLS = os.path.join(_ROOT, "tools")
for p in (_ROOT, _TOOLS):
    if p not in sys.path:
        sys.path.insert(0, p)

import spec_pressure_lab as spl       # noqa: E402


def test_turnover_series():
    to = spl.turnover_series([10.0, 0.0, 5.0, -1.0], [5.0, 0.0, None, 2.0])
    assert to[0] == 2.0 and to[1] is None and to[2] is None and to[3] is None


def test_quadrant():
    assert spl.quadrant(0.01, 0.02).startswith("增仓上行")
    assert spl.quadrant(-0.01, 0.02).startswith("增仓下行")
    assert spl.quadrant(0.01, -0.02).startswith("减仓上行")
    assert spl.quadrant(-0.01, -0.02).startswith("减仓下行")
    assert spl.quadrant(None, 0.1) is None
    assert spl.quadrant(0.1, None) is None
    # 零收益按"不上行"处理
    assert spl.quadrant(0.0, 0.1).startswith("增仓下行")


def test_pct_change():
    xs = [100.0, 105.0, 110.0]
    assert abs(spl._pct_change(xs, 2) - 0.10) < 1e-12
    assert spl._pct_change(xs, 5) is None
    assert spl._pct_change([0.0, 1.0], 1) is None       # 基期非正


def test_symbol_stat():
    n = 80
    dates = ["2026-%03d" % i for i in range(n)]
    close = [100.0 + i for i in range(n)]
    vol = [1000.0] * (n - 5) + [3000.0] * 5
    oi = [2000.0 + 10 * i for i in range(n)]
    st = spl.symbol_stat(dates, close, vol, oi)
    assert st and st["turn_z"] is not None and st["turn_z"] > 1.0
    assert st["quadrant"].startswith("增仓上行")
    assert 0.0 <= st["turn_pctile"] <= 1.0 and st["n_valid"] == n
    # 空 / 全缺持仓 -> None
    assert spl.symbol_stat([], [], [], []) is None
    assert spl.symbol_stat(["a"], [1.0], [1.0], [0.0]) is None


def test_concentration_stat_percentile_rule():
    # 常态主力90%：分位=1，不报警
    by = {"d%03d" % i: [90.0, 10.0] for i in range(60)}
    cs = spl.concentration_stat(by)
    assert abs(cs["main_share"] - 0.9) < 1e-12
    assert cs["n_active"] == 2 and abs(cs["hhi"] - 0.82) < 1e-12 and not cs["rolling"]
    # 末日骤分散 -> 自身极低分位 -> 报警
    by2 = dict(by); by2["d060"] = [40.0, 35.0, 25.0]
    cs2 = spl.concentration_stat(by2)
    assert abs(cs2["main_share"] - 0.40) < 1e-12 and cs2["n_active"] == 3 and cs2["rolling"]
    # 常态就多合约分散（主力一直40%）-> 分位不低，不误报
    by3 = {"d%03d" % i: [40.0, 35.0, 25.0] for i in range(60)}
    assert not spl.concentration_stat(by3)["rolling"]
    assert spl.concentration_stat({}) is None


def test_render_sections():
    meta = {"panel_d0": "a", "panel_d1": "b", "z_win": 120, "z_min": 40, "chg_win": 5,
            "n_sym": 1, "med_z": 0.2, "n_hot": 0, "n_cold": 0, "conc_n": 1}
    rows = [{"sym": "RB", "sector": "黑色", "turnover": 0.6, "turn_z": 0.5,
             "turn_pctile": 0.6, "turn_mean": 0.5, "quadrant": "增仓上行(多头主动)"}]
    conc = [{"sym": "RB", "main_share": 0.9, "n_active": 2, "hhi": 0.82,
             "share_pctile": 0.5, "rolling": False}]
    txt = spl.render(meta, rows, {"增仓上行(多头主动)": 1}, conc)
    for sec in ("【一】", "【二】", "【三】", "【四】"):
        assert sec in txt
    assert "RB" in txt and "增仓上行" in txt
