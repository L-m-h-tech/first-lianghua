# -*- coding: utf-8 -*-
r"""G5（第47轮，研究侧先行）组合层风险度量 portfolio_risk.py：把"一篮子品种的历史日收益 + 一组权重"
变成可复核的**组合风险数字**——相关矩阵、组合 VaR（历史模拟法 + 参数/方差-协方差法）、预期亏损 ES、
以及"原油 ±shock 经各品种 beta 传导"的压力情景。纯标准库、零网络、**纯函数不接 main、不改综合分/持仓/sizing**；
协方差复用 portfolio_constructor（对角收缩由调用方决定，本模块对传入的 C 直接计算）。

口径与符号约定（务必一致）：
  - 收益用小数（0.01=1%）；VaR/ES 一律以"损失为正"输出：VaR95=x 表示在 95% 置信下单日损失不超过 x。
  - 历史模拟法不对收益分布做正态假设，直接取组合日收益序列的经验分位（含真实肥尾/偏度）；
  - 参数法假设组合收益近似正态，σ_p=sqrt(w'Σw)，VaR=z·σ_p；二者差即"肥尾溢价"，是本模块要诚实暴露的重点；
  - 多日 VaR 按 i.i.d. 的 √h 缩放（VaR_h=VaR_1·√h），已在文案标注该假设；
  - 压力情景是**线性一阶传导**：组合损益 = Σ_i w_i·β_i·shock（β_i 为该品种对原油日收益的 OLS 斜率），
    忽略非线性/二阶与极端时相关结构突变，属"数量级情景"而非精确预测。

本轮只做**只读研究度量**；G5 剩余的 risk_gate 熔断可配置动作、第四种风险平价 sizing、组合历史净值回测留后续轮次，
且任何接入都须遵循"默认等价旧版、不传不变"。
"""
import math

import portfolio_constructor as pc

# 标准正态单侧分位（参数法用）
Z_QUANTILE = {0.90: 1.281552, 0.95: 1.644854, 0.975: 1.959964, 0.99: 2.326348, 0.995: 2.575829}
DEFAULT_LEVELS = (0.95, 0.99)
_EPS = 1e-12


# =========================== 相关矩阵 ===========================
def correlation_matrix(C):
    """协方差阵 → Pearson 相关阵；零方差行/列相关记 0（不除零），对角为 1。"""
    n = len(C)
    sig = [math.sqrt(max(C[i][i], 0.0)) for i in range(n)]
    R = [[0.0] * n for _ in range(n)]
    for i in range(n):
        R[i][i] = 1.0
        for j in range(i + 1, n):
            denom = sig[i] * sig[j]
            v = C[i][j] / denom if denom > _EPS else 0.0
            v = max(-1.0, min(1.0, v))
            R[i][j] = R[j][i] = v
    return R


def pairwise_correlations(R):
    """上三角 (i, j, rho) 列表（i<j）。"""
    n = len(R)
    return [(i, j, R[i][j]) for i in range(n) for j in range(i + 1, n)]


def avg_abs_offdiag(R):
    """平均绝对相关系数：衡量篮子的系统性联动强度（0=完全分散，1=完全同向）。"""
    pairs = pairwise_correlations(R)
    if not pairs:
        return 0.0
    return sum(abs(p[2]) for p in pairs) / len(pairs)


def avg_signed_offdiag(R):
    """平均（带符号）相关：负值说明篮子里普遍存在对冲性反向联动。"""
    pairs = pairwise_correlations(R)
    if not pairs:
        return 0.0
    return sum(p[2] for p in pairs) / len(pairs)


def top_pairs(syms, R, k=10, strongest=True):
    """相关最强(strongest=True)/最弱(False)的品种对，返回 [(sym_a, sym_b, rho), ...]，已排序。"""
    pairs = pairwise_correlations(R)
    pairs.sort(key=lambda p: p[2], reverse=strongest)
    return [(syms[i], syms[j], rho) for i, j, rho in pairs[:k]]


def sector_corr_block(R, syms, sector_of, order=None):
    """板块×板块平均相关（板块内为该板块所有品种对平均相关，板块间为跨板块全部对平均）。

    返回 (sectors, matrix)；sector_of(sym)->板块名；缺 sector 归"未知"。
    """
    groups = {}
    for idx, s in enumerate(syms):
        groups.setdefault(sector_of(s) or "未知", []).append(idx)
    sectors = order or sorted(groups)
    m = len(sectors)
    M = [[0.0] * m for _ in range(m)]
    for a in range(m):
        for b in range(a, m):
            ia, ib = groups[sectors[a]], groups[sectors[b]]
            vals = [R[i][j] for i in ia for j in ib if i != j]
            v = sum(vals) / len(vals) if vals else (1.0 if a == b else 0.0)
            M[a][b] = M[b][a] = v
    return sectors, M


# =========================== 分位数（线性插值，纯 py） ===========================
def percentile(xs, q):
    """经验分位数（C=1 线性插值，等价 numpy 'linear'）：q∈[0,1]。空序列返0。"""
    if not xs:
        return 0.0
    data = sorted(xs)
    if len(data) == 1:
        return data[0]
    pos = q * (len(data) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return data[lo]
    frac = pos - lo
    return data[lo] * (1 - frac) + data[hi] * frac


# =========================== 组合收益序列 ===========================
def portfolio_return_series(mat, w):
    """mat 为行式 dates×assets（mat[t][i]=资产i在t日收益），w[i] 权重 → 每日组合收益。"""
    return [sum(w[i] * row[i] for i in range(len(w))) for row in mat]


def portfolio_variance(w, C):
    """w'Σw。"""
    return pc._quad(C, w)


# =========================== 历史模拟法 VaR / ES ===========================
def historical_var(port_rets, levels=DEFAULT_LEVELS):
    """历史模拟法：对每个置信水平返回 VaR(损失为正) 与 ES/CVaR(超过 VaR 的尾部平均损失)。

    VaR_q = -quantile(port_rets, 1-q)；ES_q = -mean(r | r <= -VaR_q)。另给最差单日损失。
    """
    n = len(port_rets)
    out = {"n": n, "levels": {}}
    if n == 0:
        return out
    for q in levels:
        thr = percentile(port_rets, 1.0 - q)
        var = -thr
        tail = [r for r in port_rets if r <= thr]
        es = -(sum(tail) / len(tail)) if tail else var
        out["levels"][round(q, 4)] = {"var": var, "es": es, "tail_n": len(tail)}
    out["worst"] = -min(port_rets)
    out["best"] = -max(port_rets)
    return out


# =========================== 参数法 VaR ===========================
def parametric_var(w, C, levels=DEFAULT_LEVELS, horizons=(1,)):
    """方差-协方差法：σ_p=sqrt(w'Σw)，VaR_q=z_q·σ_p；多日按 √h 缩放。

    返回 {level: {sigma_daily, 1:{var}, h:{var}...}}。
    """
    sigma = math.sqrt(max(portfolio_variance(w, C), 0.0))
    out = {"sigma_daily": sigma, "ann_vol": sigma * math.sqrt(243), "levels": {}}
    for q in levels:
        z = Z_QUANTILE.get(round(q, 4)) or Z_QUANTILE.get(q)
        if z is None:
            raise ValueError("参数法无内置分位 z(%s)，请在 Z_QUANTILE 补充" % q)
        per_h = {h: z * sigma * math.sqrt(h) for h in horizons}
        out["levels"][round(q, 4)] = {"z": z, "var_by_horizon": per_h}
    return out


# =========================== 原油 beta 与压力情景 ===========================
def oil_betas(C, oil_idx):
    """从协方差阵算每资产对原油的 OLS 斜率 beta_i=C[i,oil]/C[oil,oil]，及 R²=相关²。返回 (betas, r2)。"""
    var_oil = C[oil_idx][oil_idx]
    n = len(C)
    if var_oil <= _EPS:
        return [0.0] * n, [0.0] * n
    betas, r2 = [], []
    for i in range(n):
        b = C[i][oil_idx] / var_oil
        denom = math.sqrt(max(C[i][i], 0.0)) * math.sqrt(max(var_oil, 0.0))
        rho = C[i][oil_idx] / denom if denom > _EPS else 0.0
        betas.append(b)
        r2.append(max(-1.0, min(1.0, rho)) ** 2)
    return betas, r2


def stress_oil(w, betas, shock):
    """原油收益冲击 shock（小数，如 -0.05）经 beta 线性传导：每品种贡献 contrib_i=w_i·beta_i·shock。

    返回 (组合总损益, [(i, contrib_i)...] 按绝对值降序)。总损益为负=亏损。
    """
    contrib = [(i, w[i] * betas[i] * shock) for i in range(len(w))]
    total = sum(c[1] for c in contrib)
    contrib.sort(key=lambda c: abs(c[1]), reverse=True)
    return total, contrib


# =========================== 分散化收益 ===========================
def standalone_var(w, C, q=0.95):
    """加权单体 VaR（假设各品种完全相关、无任何分散）：Σ w_i·z·σ_i。"""
    z = Z_QUANTILE[round(q, 4)]
    return sum(abs(w[i]) * z * math.sqrt(max(C[i][i], 0.0)) for i in range(len(w)))


def diversification_benefit(w, C, q=0.95):
    """分散化收益 = 1 - 组合参数VaR / 加权单体VaR（越大越分散；完全相关时≈0）。"""
    pv = parametric_var(w, C, levels=(q,), horizons=(1,))
    port = pv["levels"][round(q, 4)]["var_by_horizon"][1]
    stand = standalone_var(w, C, q)
    if stand <= _EPS:
        return 0.0, port, stand
    return 1.0 - port / stand, port, stand


# =========================== 统一快照 ===========================
def risk_snapshot(returns_by_asset, w, *, levels=DEFAULT_LEVELS, horizons=(1, 10),
                  shrink=None, oil_idx=None, sector_of=None, syms=None):
    """给定"按资产"收益序列与权重，一次性产出相关/VaR/压力全部指标。

    returns_by_asset[i] = 资产 i 的等长日收益序列；w 与资产同序。shrink 非空时对协方差做对角收缩。
    返回 dict（C/R/平均相关/历史&参数VaR/原油压力/分散化）。
    """
    C = pc.covariance(returns_by_asset)
    if shrink:
        C = pc.shrink_diagonal(C, shrink)
    R = correlation_matrix(C)
    n = len(C)
    syms = syms or ["A%d" % i for i in range(n)]
    T = min((len(r) for r in returns_by_asset), default=0)
    mat = [[returns_by_asset[i][t] for i in range(n)] for t in range(T)]
    port_rets = portfolio_return_series(mat, w)
    hv = historical_var(port_rets, levels)
    pv = parametric_var(w, C, levels, horizons)
    benefit, port_v, stand_v = diversification_benefit(w, C, list(levels)[0])
    out = {
        "n_assets": n, "n_days": T, "w_sum": sum(w), "gross": sum(abs(x) for x in w),
        "avg_abs_corr": avg_abs_offdiag(R), "avg_signed_corr": avg_signed_offdiag(R),
        "corr": R, "cov": C,
        "hist": hv, "param": pv,
        "div_benefit": benefit, "port_param_var": port_v, "standalone_var": stand_v,
        "strongest_pairs": top_pairs(syms, R, 8, True),
        "weakest_pairs": top_pairs(syms, R, 8, False),
    }
    if sector_of is not None:
        secs, block = sector_corr_block(R, syms, sector_of)
        out["sector_order"] = secs
        out["sector_block"] = block
    if oil_idx is not None and 0 <= oil_idx < n:
        betas, r2 = oil_betas(C, oil_idx)
        out["oil_idx"] = oil_idx
        out["oil_betas"] = betas
        out["oil_r2"] = r2
        scenarios = {}
        for shock in (-0.05, -0.10, 0.05):
            tot, contrib = stress_oil(w, betas, shock)
            scenarios[shock] = {"total": tot,
                                "top": [{"sym": syms[i], "beta": betas[i], "pnl": c}
                                        for i, c in contrib[:6] if abs(c) > 1e-9]}
        out["oil_stress"] = scenarios
    return out


# =========================== 零网络/零DB 手算自测 ===========================
def selftest():
    # 1) 相关阵：完全同向=1、完全反向=-1、常数序列(零方差)安全记0、对角=1、对称
    C = [[1.0, 1.0, 0.0], [1.0, 1.0, -1.0], [0.0, -1.0, 1.0]]
    R = correlation_matrix(C)
    assert abs(R[0][1] - 1.0) < 1e-9 and abs(R[1][2] + 1.0) < 1e-9
    assert all(abs(R[i][i] - 1.0) < 1e-9 for i in range(3))
    Cz = [[0.0, 0.0], [0.0, 1.0]]
    Rz = correlation_matrix(Cz)
    assert Rz[0][1] == 0.0 and Rz[0][0] == 1.0

    # 2) 平均绝对/带符号相关
    assert abs(avg_abs_offdiag([[1, -0.5], [-0.5, 1]]) - 0.5) < 1e-12
    assert abs(avg_signed_offdiag([[1, -0.5], [-0.5, 1]]) + 0.5) < 1e-12
    assert avg_abs_offdiag([[1.0]]) == 0.0

    # 3) percentile 线性插值：已知 [10,20,30,40]，中位=25，0分位=10，1分位=40
    xs = [10.0, 20.0, 30.0, 40.0]
    assert abs(percentile(xs, 0.5) - 25.0) < 1e-9
    assert abs(percentile(xs, 0.0) - 10.0) < 1e-9 and abs(percentile(xs, 1.0) - 40.0) < 1e-9
    assert percentile([], 0.95) == 0.0 and percentile([7.0], 0.5) == 7.0

    # 4) 组合收益序列手算
    mat = [[0.01, -0.02], [-0.01, 0.03]]
    pr = portfolio_return_series(mat, [0.6, 0.4])
    assert abs(pr[0] - (0.006 - 0.008)) < 1e-12 and abs(pr[1] - (-0.006 + 0.012)) < 1e-12

    # 5) 历史法 VaR/ES：构造 100 个收益，最差 -10%；99% 分位近似最差、ES≥VaR、损失为正
    rets = [0.001 * ((i % 7) - 3) for i in range(99)] + [-0.10]
    hv = historical_var(rets, (0.95, 0.99))
    assert hv["n"] == 100 and abs(hv["worst"] - 0.10) < 1e-12
    for q, d in hv["levels"].items():
        assert d["var"] >= 0 and d["es"] >= d["var"] - 1e-12 and d["tail_n"] >= 1
    assert historical_var([])["n"] == 0

    # 6) 参数法：单资产日方差 0.0001（σ=0.01），95%VaR=1.6449*0.01；10日=单日*sqrt10
    C1 = [[0.0001]]
    pv = parametric_var([1.0], C1, (0.95,), (1, 10))
    v1 = pv["levels"][0.95]["var_by_horizon"][1]
    v10 = pv["levels"][0.95]["var_by_horizon"][10]
    assert abs(v1 - 1.644854 * 0.01) < 1e-9
    assert abs(v10 - v1 * math.sqrt(10)) < 1e-12

    # 7) 分散化：两等方差不相关组合的参数VaR必须低于"加权单体(完全相关)"
    Cind = [[0.0004, 0.0], [0.0, 0.0004]]      # σ=0.02
    ben, portv, stand = diversification_benefit([0.5, 0.5], Cind, 0.95)
    assert portv < stand and ben > 0
    # 完全相关时分散化收益≈0
    Ccorr = [[0.0004, 0.0004], [0.0004, 0.0004]]
    ben2, _, _ = diversification_benefit([0.5, 0.5], Ccorr, 0.95)
    assert abs(ben2) < 1e-9

    # 8) oil beta：无噪声 y=2x → beta=2、R²=1；零方差原油安全
    x = [0.01 * ((i % 5) - 2) for i in range(20)]
    y = [2 * v for v in x]
    rets_assets = [y, x]
    Cb = pc.covariance(rets_assets)           # 资产0=y, 资产1=x(oil idx=1)
    betas, r2 = oil_betas(Cb, 1)
    assert abs(betas[0] - 2.0) < 1e-9 and abs(r2[0] - 1.0) < 1e-9
    b0, r0 = oil_betas([[0.0]], 0)
    assert b0[0] == 0.0 and r0[0] == 0.0

    # 9) 原油压力：w=1 全仓资产0(beta=2)，原油 -5% → 组合 -10%；+5% → +10%
    tot_down, contrib = stress_oil([1.0, 0.0], betas, -0.05)
    assert abs(tot_down - (-0.10)) < 1e-12
    tot_up, _ = stress_oil([1.0, 0.0], betas, 0.05)
    assert abs(tot_up - 0.10) < 1e-12

    # 10) top_pairs 排序 & 板块块
    R3 = [[1, 0.9, 0.1], [0.9, 1, -0.3], [0.1, -0.3, 1]]
    tp = top_pairs(["a", "b", "c"], R3, 1, True)
    assert tp[0][0] == "a" and tp[0][1] == "b" and abs(tp[0][2] - 0.9) < 1e-12
    secs, block = sector_corr_block(R3, ["a", "b", "c"], lambda s: "X" if s in ("a", "b") else "Y")
    assert secs == ["X", "Y"] and abs(block[0][0] - 0.9) < 1e-12   # X 板块内只有 a-b

    # 11) risk_snapshot 端到端 + 退化（单资产/零方差不崩）
    import random
    random.seed(47)
    common = [random.gauss(0, 1) for _ in range(120)]
    ra = [[0.01 * common[t] + random.gauss(0, 0.004) for t in range(120)] for _ in range(4)]
    snap = risk_snapshot(ra, [0.25] * 4, oil_idx=0,
                         sector_of=lambda s: "S", syms=["k0", "k1", "k2", "k3"])
    assert snap["n_assets"] == 4 and snap["n_days"] == 120
    assert 0 <= snap["avg_abs_corr"] <= 1
    assert snap["hist"]["levels"][0.95]["var"] >= 0
    assert "oil_stress" in snap and -0.05 in snap["oil_stress"]
    snap1 = risk_snapshot([[0.0] * 10], [1.0])
    assert snap1["param"]["sigma_daily"] == 0.0 and snap1["hist"]["worst"] == 0.0

    print("portfolio_risk selftest ALL PASS（相关阵/平均联动/分位数/组合收益序列/历史VaR-ES/"
          "参数VaR与√h缩放/分散化收益/原油beta与线性压力/板块块/端到端与零方差退化 共11组）")
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
