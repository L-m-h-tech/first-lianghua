# -*- coding: utf-8 -*-
"""G5（第47轮）portfolio_risk 纯函数零网络/零面板确定性测试：不读 research_panel.db、不碰生产库。"""
import math

import pytest

import portfolio_constructor as pc
import portfolio_risk as pr


# ---------- 相关矩阵 ----------
def test_correlation_basic():
    C = [[1.0, 1.0, -0.5], [1.0, 1.0, -0.5], [-0.5, -0.5, 1.0]]
    R = pr.correlation_matrix(C)
    assert abs(R[0][1] - 1.0) < 1e-9 and abs(R[0][2] + 0.5) < 1e-9
    assert all(abs(R[i][i] - 1.0) < 1e-9 for i in range(3))
    assert R[0][2] == R[2][0]


def test_correlation_zero_variance_safe():
    R = pr.correlation_matrix([[0.0, 0.0], [0.0, 1.0]])
    assert R[0][1] == 0.0 and R[0][0] == 1.0


def test_avg_corr_and_pairs():
    R = [[1, 0.8, -0.2], [0.8, 1, 0.4], [-0.2, 0.4, 1]]
    assert abs(pr.avg_abs_offdiag(R) - (0.8 + 0.2 + 0.4) / 3) < 1e-12
    assert abs(pr.avg_signed_offdiag(R) - (0.8 - 0.2 + 0.4) / 3) < 1e-12
    tp = pr.top_pairs(["a", "b", "c"], R, 1)
    assert tp[0][:2] == ("a", "b")
    assert pr.avg_abs_offdiag([[1.0]]) == 0.0


def test_sector_block():
    R = [[1, 0.9, 0.1], [0.9, 1, 0.2], [0.1, 0.2, 1]]
    secs, M = pr.sector_corr_block(R, ["a", "b", "c"], lambda s: "X" if s != "c" else "Y")
    assert secs == ["X", "Y"]
    assert abs(M[0][0] - 0.9) < 1e-12          # X 内仅 a-b
    assert abs(M[0][1] - (0.1 + 0.2) / 2) < 1e-12


# ---------- 分位数 ----------
@pytest.mark.parametrize("q,expect", [(0.0, 10.0), (0.5, 25.0), (1.0, 40.0), (0.25, 17.5)])
def test_percentile(q, expect):
    assert abs(pr.percentile([10.0, 20.0, 30.0, 40.0], q) - expect) < 1e-9


def test_percentile_degenerate():
    assert pr.percentile([], 0.95) == 0.0
    assert pr.percentile([3.0], 0.5) == 3.0


# ---------- 组合收益 ----------
def test_portfolio_return_series():
    mat = [[0.01, -0.02], [-0.01, 0.04]]
    out = pr.portfolio_return_series(mat, [0.5, 0.5])
    assert abs(out[0] + 0.005) < 1e-12 and abs(out[1] - 0.015) < 1e-12
    assert abs(pr.portfolio_variance([1.0], [[0.04]]) - 0.04) < 1e-12


# ---------- 历史 VaR/ES ----------
def test_historical_var():
    rets = [0.001 * (i - 50) for i in range(100)]
    hv = pr.historical_var(rets, (0.95, 0.99))
    assert hv["n"] == 100
    for d in hv["levels"].values():
        assert d["var"] >= 0 and d["es"] >= d["var"] - 1e-12 and d["tail_n"] >= 1
    assert abs(hv["worst"] - 0.050) < 1e-9      # 最小收益 0.001*(0-50)=-0.050
    assert pr.historical_var([])["n"] == 0


def test_historical_var_loss_positive():
    # 全正收益 → VaR 为负（即该分位仍盈利，无损失），worst 为最小日的负值
    hv = pr.historical_var([0.01, 0.02, 0.03], (0.95,))
    assert hv["levels"][0.95]["var"] < 0


# ---------- 参数 VaR ----------
def test_parametric_var_exact_and_horizon():
    pv = pr.parametric_var([1.0], [[0.0001]], (0.95,), (1, 10))
    v1 = pv["levels"][0.95]["var_by_horizon"][1]
    assert abs(v1 - 1.644854 * 0.01) < 1e-9
    assert abs(pv["levels"][0.95]["var_by_horizon"][10] - v1 * math.sqrt(10)) < 1e-12
    assert abs(pv["ann_vol"] - 0.01 * math.sqrt(243)) < 1e-9


def test_parametric_var_unknown_level_raises():
    with pytest.raises(ValueError):
        pr.parametric_var([1.0], [[0.01]], (0.92,), (1,))


# ---------- beta / 压力 ----------
def test_oil_betas_exact_linear():
    x = [0.01 * ((i % 5) - 2) for i in range(20)]
    y = [2 * v + 0.001 for v in x]             # 斜率2、带截距不影响协方差斜率
    C = pc.covariance([y, x])
    betas, r2 = pr.oil_betas(C, 1)
    assert abs(betas[0] - 2.0) < 1e-9 and abs(r2[0] - 1.0) < 1e-9
    b0, _ = pr.oil_betas([[0.0]], 0)
    assert b0[0] == 0.0


def test_stress_oil():
    betas = [2.0, 0.5]
    tot, contrib = pr.stress_oil([1.0, 0.0], betas, -0.05)
    assert abs(tot + 0.10) < 1e-12
    assert contrib[0][0] == 0                   # 贡献最大者排第一
    tot_up, _ = pr.stress_oil([0.5, 0.5], betas, 0.05)
    assert abs(tot_up - (0.5 * 2 * 0.05 + 0.5 * 0.5 * 0.05)) < 1e-12


# ---------- 分散化 ----------
def test_diversification_benefit():
    ben, portv, stand = pr.diversification_benefit([0.5, 0.5], [[4e-4, 0], [0, 4e-4]], 0.95)
    assert portv < stand and 0 < ben <= 1
    ben2, _, _ = pr.diversification_benefit([0.5, 0.5], [[4e-4, 4e-4], [4e-4, 4e-4]], 0.95)
    assert abs(ben2) < 1e-9


# ---------- 端到端 ----------
def test_risk_snapshot_end_to_end():
    import random
    random.seed(7)
    common = [random.gauss(0, 1) for _ in range(100)]
    rab = [[0.01 * common[t] + random.gauss(0, 0.004) for t in range(100)] for _ in range(4)]
    snap = pr.risk_snapshot(rab, [0.25] * 4, oil_idx=0, sector_of=lambda s: "S",
                            syms=["a", "b", "c", "d"])
    assert snap["n_assets"] == 4 and snap["n_days"] == 100
    assert abs(snap["w_sum"] - 1) < 1e-9
    assert 0 <= snap["avg_abs_corr"] <= 1
    assert snap["hist"]["levels"][0.95]["es"] >= snap["hist"]["levels"][0.95]["var"] - 1e-12
    assert set(snap["oil_stress"]) == {-0.05, -0.10, 0.05}
    assert "sector_block" in snap and len(snap["strongest_pairs"]) == 6   # C(4,2)=6 对


def test_risk_snapshot_degenerate():
    snap = pr.risk_snapshot([[0.0] * 10], [1.0])
    assert snap["param"]["sigma_daily"] == 0.0 and snap["hist"]["worst"] == 0.0
    assert snap["div_benefit"] == 0.0
