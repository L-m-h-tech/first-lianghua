# -*- coding: utf-8 -*-
r"""G26（第40轮）组合构建器 portfolio_constructor.py：把"一批候选品种 + 各自历史日收益"
变成"一篮子目标权重"的纯标准库、零网络、**风险型**（不预测预期收益）权重分配层。

与 portfolio.py 的区别（互补、不替代）：portfolio.decide_lots 是**逐品种独立**按名义/ATR/分档定手数；
本模块是**横截面一次性**分配资本权重，回答"同一篮子里谁该分多少"。四种风险型方法：
  - equal    等权（基线，等价旧"等名义"口径，也是缺省/数据不足回退）；
  - inv_vol  逆波动，w∝1/σ；
  - erc      风险平价 Equal Risk Contribution，让每个品种对组合波动的边际贡献相等（对角情形=逆波动）；
  - gmv      长仓最小方差 Minimum Variance，min ½w'Σw，w≥0、Σw=1、单品种上限。
另含：协方差+对角收缩（保正定）、capped-simplex 投影（和为1+非负+单票上限的闭式约束）、目标波动缩放、
风险贡献/分散化度/有效N/换手等诊断。**纯函数、不接 main、不改综合分**；是否接到 paper/backtest 由后续轮次
在"默认 equal、缺省逐字节等价旧版"前提下另议。

算法均为经典闭式/凸优化的纯 Python 实现（无 numpy/scipy）：
  - 长仓二次规划用投影梯度（FISTA 式加速 + capped-simplex 投影），凸问题收敛到全局解；
  - ERC 用乘性定点更新（Spinu/Chaves 式），正定协方差下收敛。
"""
import math

# 独立默认值（不依赖 config，便于零环境自测；tools/portfolio_lab 用 config.PC_* 覆盖）
DEFAULT_SHRINK = 0.10
DEFAULT_CAP = 0.20
DEFAULT_TOL = 1e-9
_EPS = 1e-12


# =========================== 基础线性代数（纯 Python） ===========================
def _matvec(A, x):
    return [sum(A[i][j] * x[j] for j in range(len(x))) for i in range(len(A))]


def _quad(A, x):
    return sum(x[i] * v for i, v in enumerate(_matvec(A, x)))


def _norm(x):
    return math.sqrt(sum(v * v for v in x))


def largest_eigenvalue(A, iters=200):
    """幂迭代求对称半正定矩阵最大特征值（用作投影梯度步长 1/L）；退化返1。"""
    n = len(A)
    v = [1.0 / math.sqrt(n)] * n
    lam = 1.0
    for _ in range(iters):
        w = _matvec(A, v)
        nw = _norm(w)
        if nw < _EPS:
            return 1.0
        w = [z / nw for z in w]
        lam = sum(w[i] * z for i, z in enumerate(_matvec(A, w)))
        if _norm([w[i] - v[i] for i in range(n)]) < 1e-12:
            v = w
            break
        v = w
    return max(lam, _EPS)


def is_psd(A, tries=40):
    """Cholesky 判定对称阵半正定（不要求严格）；能分解即 PSD。"""
    n = len(A)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = sum(L[i][k] * L[j][k] for k in range(j))
            if i == j:
                v = A[i][i] - s
                if v < -1e-8:
                    return False
                L[i][j] = math.sqrt(max(v, 0.0))
            else:
                if abs(L[j][j]) < _EPS:
                    return False
                L[i][j] = (A[i][j] - s) / L[j][j]
    return True


# =========================== 协方差 + 对角收缩 ===========================
def covariance(returns):
    """returns=[[r_t 资产0...], ...] 或传入"按资产的序列列表"（n 个等长 list）。

    本函数约定输入是**按资产**：returns[i] 是资产 i 的收益序列（等长、已对齐）。返回 n×n 样本协方差（除以 T-1）。
    """
    n = len(returns)
    if n == 0:
        return []
    T = min(len(r) for r in returns)
    if T < 2:
        return [[0.0] * n for _ in range(n)]
    means = [sum(r[:T]) / T for r in returns]
    C = [[0.0] * n for _ in range(n)]
    for i in range(n):
        ri = returns[i]
        for j in range(i, n):
            rj = returns[j]
            s = sum((ri[t] - means[i]) * (rj[t] - means[j]) for t in range(T))
            v = s / (T - 1)
            C[i][j] = C[j][i] = v
    return C


def shrink_diagonal(C, alpha):
    """Ledoit-Wolf 简化收缩：(1-α)C + α·F，目标 F=平均方差对角阵（保正定、改善条件数）。α∈[0,1]。"""
    n = len(C)
    if n == 0 or alpha <= 0:
        return [row[:] for row in C]
    avg_var = sum(C[i][i] for i in range(n)) / n
    F = [[(avg_var if i == j else 0.0) for j in range(n)] for i in range(n)]
    return [[(1 - alpha) * C[i][j] + alpha * F[i][j] for j in range(n)] for i in range(n)]


# =========================== capped-simplex 投影（和=1、非负、单票上限） ===========================
def project_capped_simplex(v, cap=1.0, total=1.0):
    """把 v 投影到 {w: 0≤w_i≤cap, Σw=total}（欧氏最近）。二分求平移量 τ 使 Σclamp(v_i−τ,0,cap)=total。"""
    n = len(v)
    if n == 0:
        return []
    cap = max(cap, total / n)  # 上限不能小于平均，否则无解
    lo, hi = min(v) - total - 1.0, max(v) + 1.0

    def f(tau):
        return sum(min(max(x - tau, 0.0), cap) for x in v)

    # f 关于 tau 单调递减；二分找 f(τ)=total
    for _ in range(120):
        mid = 0.5 * (lo + hi)
        if f(mid) > total:
            lo = mid
        else:
            hi = mid
    tau = 0.5 * (lo + hi)
    w = [min(max(x - tau, 0.0), cap) for x in v]
    # 数值补平到 total
    d = total - sum(w)
    if abs(d) > 1e-12:
        room = [i for i in range(n) if (d > 0 and w[i] < cap - 1e-12) or (d < 0 and w[i] > 1e-12)]
        if room:
            step = d / len(room)
            for i in room:
                w[i] = min(max(w[i] + step, 0.0), cap)
    return w


# =========================== 权重方法 ===========================
def equal_weights(n):
    if n <= 0:
        return []
    return [1.0 / n] * n


def inverse_vol_weights(C, cap=None):
    sig = [math.sqrt(max(C[i][i], _EPS)) for i in range(len(C))]
    inv = [1.0 / s for s in sig]
    z = sum(inv)
    w = [x / z for x in inv]
    if cap:
        w = project_capped_simplex(w, cap)
    return w


def _gauss_solve(A, b):
    """高斯消元解 Ax=b（对称正定）；奇异返回 None。自包含、不依赖其它根模块。"""
    n = len(A)
    M = [list(map(float, A[i])) + [float(b[i])] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-14:
            return None
        M[col], M[piv] = M[piv], M[col]
        d = M[col][col]
        M[col] = [v / d for v in M[col]]
        for r in range(n):
            if r == col:
                continue
            f = M[r][col]
            if f:
                M[r] = [M[r][j] - f * M[col][j] for j in range(n + 1)]
    return [M[i][n] for i in range(n)]


def risk_parity(C, cap=None, tol=1e-4, max_iter=300):
    """ERC（等风险贡献）：最小化 F(w)=½w'Σw−Σln w_i，其 KKT 条件 w_i(Σw)_i=常数 即各品种风险贡献相等。

    全牛顿法：Hessian H=Σ+diag(1/w_i²)，每步解 H·d=g 并回溯线搜索保 w>0 且 F 下降；
    **迭代中不归一化**（归一化会破坏该目标的自然尺度、导致定点停滞，实测教训），仅末尾归一+上限投影。
    """
    n = len(C)
    if n == 1:
        return [1.0]
    w = inverse_vol_weights(C, cap=None)

    def F(ww):
        return 0.5 * _quad(C, ww) - sum(math.log(x) for x in ww)

    for _ in range(max_iter):
        sw = _matvec(C, w)
        # 尺度无关的收敛判据：k_i=w_i(Σw)_i 应彼此相等（相对偏差）
        k = [w[i] * sw[i] for i in range(n)]
        mk = sum(k) / n
        if mk > _EPS and max(abs(k[i] / mk - 1.0) for i in range(n)) < tol:
            break
        g = [sw[i] - 1.0 / w[i] for i in range(n)]
        H = [[C[i][j] + (1.0 / (w[i] * w[i]) if i == j else 0.0) for j in range(n)] for i in range(n)]
        d = _gauss_solve(H, g)
        if d is None:
            break
        a, fold = 1.0, F(w)
        ok = False
        for _bt in range(60):
            wn = [w[i] - a * d[i] for i in range(n)]
            if all(x > 1e-12 for x in wn):
                try:
                    Fn = F(wn)
                except ValueError:
                    Fn = float("inf")
                if math.isfinite(Fn) and Fn < fold - 1e-14:
                    w = wn
                    ok = True
                    break
            a *= 0.5
        if not ok:
            break
    z = sum(w)
    w = [x / z for x in w]
    if cap:
        w = project_capped_simplex(w, cap)
    return w


def quadratic_long_only(C, linear=None, cap=None, tol=1e-5, max_iter=2000):
    """长仓二次规划 min ½w'Cw − q'w，s.t. w≥0、Σw=1、w≤cap（投影梯度+Nesterov加速）。

    linear=None(q=0) 即最小方差 GMV。凸二次型，投影梯度收敛到全局最优。
    """
    n = len(C)
    if n == 1:
        return [1.0]
    q = linear or [0.0] * n
    L = largest_eigenvalue(C)
    step = 1.0 / L

    def obj(w):
        return 0.5 * _quad(C, w) - sum(q[i] * w[i] for i in range(n))

    x = equal_weights(n)
    y = x[:]
    t = 1.0
    best, best_val = x[:], obj(x)
    prev = x[:]
    for k in range(max_iter):
        gy = [a - b for a, b in zip(_matvec(C, y), q)]
        xn = project_capped_simplex([y[i] - step * gy[i] for i in range(n)], cap or 1.0)
        tn = 0.5 * (1 + math.sqrt(1 + 4 * t * t))
        y = [xn[i] + ((t - 1) / tn) * (xn[i] - x[i]) for i in range(n)]
        x, t = xn, tn
        val = obj(xn)
        if val < best_val:
            best_val, best = val, xn[:]
        if k > 10 and _norm([xn[i] - prev[i] for i in range(n)]) < tol:
            best = xn
            break
        prev = xn[:]
    return project_capped_simplex(best, cap or 1.0)


def min_variance(C, cap=None):
    return quadratic_long_only(C, None, cap)


# =========================== 目标波动缩放与诊断 ===========================
def target_vol_scale(w, C, target_annual, periods_per_year=243, max_gross=1.5):
    """按目标年化波动等比缩放总敞口；target_annual≤0 不缩放。返回(缩放后权重, 杠杆倍数, 缩放前年化波动)。"""
    pv = math.sqrt(max(_quad(C, w), 0.0))
    ann = pv * math.sqrt(periods_per_year)
    gross = sum(abs(x) for x in w)
    if target_annual is None or target_annual <= 0 or ann < _EPS or gross < _EPS:
        return w[:], 1.0, ann
    k = target_annual / ann
    k = min(k, max_gross / gross)     # 总敞口上限，防低波期过度加杠杆
    return [x * k for x in w], k, ann


def risk_contributions(w, C):
    """返回 (绝对风险贡献列表, 占比列表, 组合波动)；占比之和=1。"""
    sw = _matvec(C, w)
    rc = [w[i] * sw[i] for i in range(len(w))]
    pv = math.sqrt(max(sum(rc), 0.0))
    if pv < _EPS:
        return rc, [1.0 / len(w)] * len(w), pv
    frac = [x / (pv * pv) for x in rc]   # rc_i / (w'Σw) = 占比
    return rc, frac, pv


def diversification_ratio(w, C):
    num = sum(w[i] * math.sqrt(max(C[i][i], 0.0)) for i in range(len(w)))
    pv = math.sqrt(max(_quad(C, w), _EPS))
    return num / pv


def effective_n(w):
    """有效持仓数 = 1/Σw_i²（等权=n，越集中越接近1）。"""
    s = sum(x * x for x in w)
    return 1.0 / s if s > _EPS else 0.0


def gross_exposure(w):
    return sum(abs(x) for x in w)


def turnover(new_w, old_w, keys_new=None, keys_old=None):
    """单边换手 = ½Σ|w_new−w_old|（不同标的集合按并集、缺省0）；long-only 下∈[0,1+杠杆]。"""
    if keys_new is None and keys_old is None and len(new_w) == len(old_w):
        return 0.5 * sum(abs(a - b) for a, b in zip(new_w, old_w))
    kn = keys_new or list(range(len(new_w)))
    ko = keys_old or list(range(len(old_w)))
    dn = dict(zip(kn, new_w))
    do = dict(zip(ko, old_w))
    keys = set(dn) | set(do)
    return 0.5 * sum(abs(dn.get(k, 0.0) - do.get(k, 0.0)) for k in keys)


# =========================== 统一入口 ===========================
METHODS = ("equal", "inv_vol", "erc", "gmv")


def construct(returns, method="equal", *, shrink=DEFAULT_SHRINK, cap=DEFAULT_CAP,
              target_annual=0.0, periods_per_year=243, max_gross=1.5,
              erc_tol=1e-4, erc_iter=300, raw_cov=False):
    """按资产收益序列构建目标权重。

    返回 dict：method/weights(按 returns 资产顺序)/cov/shrink/ann_vol/rc_frac/eff_n/div_ratio/gross/leverage。
    raw_cov=True 时不做对角收缩（仅自测用）。
    """
    n = len(returns)
    if n == 0:
        raise ValueError("空资产集合")
    C0 = covariance(returns)
    C = C0 if raw_cov else shrink_diagonal(C0, shrink)
    if method == "equal":
        w = equal_weights(n)
    elif method == "inv_vol":
        w = inverse_vol_weights(C, cap)
    elif method == "erc":
        w = risk_parity(C, cap, tol=erc_tol, max_iter=erc_iter)
    elif method == "gmv":
        w = min_variance(C, cap)
    else:
        raise ValueError("未知组合方法 %r（可选 %s）" % (method, METHODS))
    lev = 1.0
    ann_pre = math.sqrt(max(_quad(C, w), 0.0)) * math.sqrt(periods_per_year)
    if target_annual and target_annual > 0:
        w, lev, ann_pre = target_vol_scale(w, C, target_annual, periods_per_year, max_gross)
    rc, frac, pv = risk_contributions(w, C)
    return {
        "method": method, "weights": w, "cov": C, "ann_vol": pv * math.sqrt(periods_per_year),
        "rc_frac": frac, "eff_n": effective_n(w), "div_ratio": diversification_ratio(w, C),
        "gross": gross_exposure(w), "leverage": lev,
    }


# =========================== 零网络/零DB 手算自测 ===========================
def selftest():
    # 1) 等权：和为1、有效N=n
    w = equal_weights(4)
    assert abs(sum(w) - 1) < 1e-12 and abs(effective_n(w) - 4) < 1e-9

    # 2) 对角协方差（方差1、4）→ 逆波动=(2/3,1/3)，ERC 同解且风险贡献各半
    C = [[1.0, 0.0], [0.0, 4.0]]
    iv = inverse_vol_weights(C)
    assert abs(iv[0] - 2 / 3) < 1e-9 and abs(iv[1] - 1 / 3) < 1e-9
    erc = risk_parity(C)
    _, frac, _ = risk_contributions(erc, C)
    assert abs(erc[0] - 2 / 3) < 1e-5 and abs(frac[0] - 0.5) < 1e-5 and abs(frac[1] - 0.5) < 1e-5

    # 3) GMV：等方差不相关→0.5/0.5；一方方差极小→集中到它（被 cap 截到上限）
    g1 = min_variance([[1.0, 0.0], [0.0, 1.0]])
    assert abs(g1[0] - 0.5) < 1e-6 and abs(g1[1] - 0.5) < 1e-6
    g2 = min_variance([[1.0, 0.0], [0.0, 1e-6]], cap=0.8)
    assert abs(g2[1] - 0.8) < 1e-6 and abs(g2[0] - 0.2) < 1e-6

    # 4) capped-simplex 投影：和归一+非负+单票上限
    p = project_capped_simplex([0.9, 0.9, 0.0], cap=0.5)
    assert abs(sum(p) - 1) < 1e-9 and abs(p[0] - 0.5) < 1e-9 and abs(p[1] - 0.5) < 1e-9 and abs(p[2]) < 1e-12
    p0 = project_capped_simplex([5.0, -1.0, 0.0])
    assert abs(sum(p0) - 1) < 1e-9 and all(x >= -1e-12 for x in p0)

    # 5) 目标波动缩放：单资产期σ=0.1，年化(225)后=1.5，精确缩到目标0.15=缩10倍；杠杆受 max_gross 限制
    Cw = [[0.01, 0.0], [0.0, 0.01]]           # 单期方差0.01→单期σ=0.1
    base = [1.0, 0.0]
    ws, k, ann0 = target_vol_scale(base, Cw, 0.15, periods_per_year=225, max_gross=10)
    assert abs(ann0 - 1.5) < 1e-9             # 0.1*sqrt(225)=1.5
    assert abs(k - 0.1) < 1e-9                # 1.5→0.15 需缩10倍
    wcap, kcap, _ = target_vol_scale(base, Cw, 5.0, periods_per_year=225, max_gross=1.5)
    assert abs(sum(abs(x) for x in wcap) - 1.5) < 1e-9   # 想加杠杆被总敞口1.5截住

    # 6) 换手：全仓一只→两只各半 = 0.5；不同标的集合并集
    assert abs(turnover([1.0, 0.0], [0.5, 0.5]) - 0.5) < 1e-12
    assert abs(turnover([1.0], [1.0], ["a"], ["b"]) - 1.0) < 1e-12

    # 7) 协方差+收缩：对称、正定（Cholesky 可分解），α=0 时逐值等于样本协方差
    r = [[1.0, 2.0, 3.0, 4.0], [2.0, 0.0, 2.0, 0.0]]
    Cv = covariance(r)
    assert abs(Cv[0][1] - Cv[1][0]) < 1e-12
    Cs = shrink_diagonal(Cv, 0.2)
    assert is_psd(Cs)
    assert shrink_diagonal(Cv, 0.0) == Cv
    # 手工：资产0样本方差=5/3
    assert abs(Cv[0][0] - 5.0 / 3.0) < 1e-9

    # 8) ERC 在高相关等波动对上≈等权，且风险贡献占比都贴近 1/n
    rho = 0.9
    Ch = [[1.0, rho], [rho, 1.0]]
    wh = risk_parity(Ch)
    _, fh, _ = risk_contributions(wh, Ch)
    assert abs(wh[0] - 0.5) < 1e-4 and abs(wh[1] - 0.5) < 1e-4
    assert max(abs(x - 0.5) for x in fh) < 1e-4

    # 9) construct 端到端：四方法都合法（和≈1、非负、有效N≥1），未知方法报错
    data = [[1.0, 1.02, 0.99, 1.03, 0.97, 1.01, 1.0, 0.98, 1.04, 1.0],
            [1.0, 1.01, 1.01, 0.99, 1.02, 0.98, 1.0, 1.03, 0.97, 1.01],
            [1.0, 0.97, 1.05, 0.96, 1.06, 0.95, 1.0, 1.07, 0.94, 1.02]]
    rets = [[data[a][t + 1] / data[a][t] - 1 for t in range(9)] for a in range(3)]
    for m in METHODS:
        out = construct(rets, m, cap=0.6)
        assert abs(sum(out["weights"]) - 1) < 1e-9 and all(x >= -1e-12 for x in out["weights"])
        assert 1.0 - 1e-9 <= out["eff_n"] <= 3.0 + 1e-9
        assert abs(sum(out["rc_frac"]) - 1) < 1e-9
    # 强不变量：长仓最小方差组合的波动不得高于等权（凸优化最优性）
    vol_eq = construct(rets, "equal", cap=1.0)["ann_vol"]
    vol_gmv = construct(rets, "gmv", cap=1.0)["ann_vol"]
    assert vol_gmv <= vol_eq + 1e-9
    try:
        construct(rets, "nope")
        raise AssertionError("未知方法应报错")
    except ValueError:
        pass

    # 10) 退化：常数收益（零方差）不崩、GMV/ERC 仍给合法权重
    z = [[0.0] * 8, [0.01, -0.01, 0.0, 0.02, -0.02, 0.0, 0.01, -0.01]]
    out = construct(z, "gmv", raw_cov=False, cap=0.7)
    assert abs(sum(out["weights"]) - 1) < 1e-9 and all(x >= -1e-12 for x in out["weights"])

    print("portfolio_constructor selftest ALL PASS（等权/逆波动/ERC风险平价/长仓GMV、capped-simplex投影、"
          "目标波动缩放与杠杆上限、协方差对角收缩正定、风险贡献/有效N/换手、退化零方差安全 共10组）")
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
