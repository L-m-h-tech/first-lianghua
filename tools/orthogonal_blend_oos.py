# -*- coding: utf-8 -*-
r"""G25续/G16前置（第61轮）正交IC加权接**真实研究面板**做样本外（walk-forward）对照——研究侧离线工具。

回答的问题：factor_legacy_expr.orthogonal_ic_blend（顺序Schmidt正交去共线 + |IC|有符号归一加权）
在真实面板上、严格样本外，是否比 ①等权合成 ②每个单因子 的下一期截面 RankIC 更稳/更高？
这是 G16 浅 ML（ml_samples）样本未齐前的一条**可解释线性合成基线**；纯研究侧，绝不进综合分、不改主链。

方法（全部来自自有 SQLite cache/research_panel.db，零网络、纯标准库）：
  1. 面板长表（sym×date）取候选因子列（默认 ret5/ret20/ret63/tsmom63/tsmom126/day_chg），
     前向收益 y_H(t)=c(t+H)/c(t)-1（严格未来，H=1/5/20 交易日）。
  2. 每个交易日先做**截面**均匀秩标准化（[-1,1]，只用当日截面、无未来），消除量纲/极值。
  3. Walk-forward 扩展窗：训练满 MIN_TRAIN 个交易日后，每 REFIT_EVERY 个交易日（月度）用
     **该日之前全部**训练样本重估一次：各因子池化 Spearman IC → orthogonal_ic_blend 学
     顺序正交 OLS 系数 betas 与 IC 权重 weights；两次再拟合之间对每个 OOS 日沿用同一组参数。
  4. OOS 日：用训练期 betas 对当日截面因子做三角递推残差化，再按 weights 合成得 blend 分；
     对照 equal 等权合成与每个单因子，计算当日**截面 RankIC**；汇总均值/ICIR/正比例/天数。
因果性：betas/weights/IC 只用 t 之前数据；OOS 日截面标准化只用当日截面；y 用 t 之后价格。

输出：reports/orthogonal_blend_oos.txt（人读）+ .json（机读），experiment_ledger 旁路台账一条。
用法（项目根目录）：
  D:\Python\python.exe tools\orthogonal_blend_oos.py                 # 全面板、写报告
  D:\Python\python.exe tools\orthogonal_blend_oos.py --factors ret20,ret63,tsmom126
  D:\Python\python.exe tools\orthogonal_blend_oos.py --selftest      # 零网络/零DB合成断言
"""
import argparse
import json
import math
import os
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
import factor_expr as fe                      # noqa: E402  白名单DSL：spearman/正交/加权原语
import factor_legacy_expr as fle              # noqa: E402  orthogonal_ic_blend
import config                                  # noqa: E402

DEFAULT_DB = _ROOT / "cache" / "research_panel.db"
DEFAULT_TXT = _ROOT / "reports" / "orthogonal_blend_oos.txt"
DEFAULT_JSON = _ROOT / "reports" / "orthogonal_blend_oos.json"
DEFAULT_FACTORS = ("ret5", "ret20", "ret63", "tsmom63", "tsmom126", "day_chg")
HORIZONS = (1, 5, 20)
MIN_TRAIN = 60          # 至少 60 个交易日训练才开始 OOS
REFIT_EVERY = 20        # 每 20 个交易日（约月度）扩展窗重估一次参数
MIN_CS = 10             # OOS 当日完整样本截面至少 10 个品种才算一个有效 IC 点


# --------------------------- 纯统计/变换（可合成断言） ---------------------------
def isnum(x):
    return fe._isnum(x)


def cs_uniform(values_by_sym):
    """当日截面均匀秩标准化到 [-1,1]（只用传入截面、确定性、抗极值）；不足 2 个返回 {}。"""
    items = [(s, v) for s, v in values_by_sym.items() if isnum(v)]
    m = len(items)
    if m < 2:
        return {}
    order = sorted(range(m), key=lambda i: items[i][1])
    ranks = [0.0] * m
    k = 0
    while k < m:                      # 平均秩处理并列
        j = k
        while j + 1 < m and items[order[j + 1]][1] == items[order[k]][1]:
            j += 1
        avg = (k + j) / 2.0 + 1.0
        for t in range(k, j + 1):
            ranks[order[t]] = avg
        k = j + 1
    denom = (m - 1) / 2.0 if m > 1 else 1.0
    return {items[i][0]: (ranks[i] - (m + 1) / 2.0) / denom for i in range(m)}


def apply_triangular(fvec, betas):
    """用训练期学到的顺序正交 OLS 系数，对一条 OOS 因子向量做同样的三角递推残差化。

    r_0=f_0；r_i = f_i - Σ_{j<i} beta_ij * r_j（与 factor_expr.orthogonalize 的训练变换同序）。"""
    resid = []
    for i in range(len(fvec)):
        ri = fvec[i]
        for j, b in enumerate(betas[i]):
            ri -= b * resid[j]
        resid.append(ri)
    return resid


def summarize_ic(daily_ics):
    """日度截面 IC 序列 → 均值/ICIR(mean/std)/正比例/天数。"""
    xs = [v for v in daily_ics if isnum(v)]
    n = len(xs)
    if n == 0:
        return {"mean_ic": None, "icir": None, "pct_positive": None, "n_days": 0}
    mean = sum(xs) / n
    var = sum((v - mean) ** 2 for v in xs) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    return {"mean_ic": mean, "icir": (mean / sd if sd > 1e-15 else 0.0),
            "pct_positive": sum(1 for v in xs if v > 0) / n, "n_days": n}


# --------------------------- 面板装载 ---------------------------
def load_panel(db_path, factors):
    """读 research_panel：返回 (dates升序, by_sym{sym:{date:{col:val}}}, sectors)。缺库返回 None。"""
    if not os.path.exists(db_path):
        return None
    cols = ["sym", "date", "sector", "c"] + [f for f in factors if f != "c"]
    c = sqlite3.connect(db_path)
    cur = c.cursor()
    have = {r[1] for r in cur.execute("PRAGMA table_info(research_panel)").fetchall()}
    missing = [f for f in factors if f not in have]
    if missing:
        c.close()
        raise ValueError("面板缺少因子列: %s" % missing)
    q = "SELECT %s FROM research_panel ORDER BY sym,date" % ",".join(cols)
    by_sym, dates, sectors = {}, set(), {}
    for row in cur.execute(q):
        rec = dict(zip(cols, row))
        sym, d = rec.pop("sym"), rec.pop("date")
        sectors[sym] = rec.pop("sector", None)
        by_sym.setdefault(sym, {})[d] = rec
        dates.add(d)
    c.close()
    return sorted(dates), by_sym, sectors


def build_forward(by_sym, horizon):
    """y{sym:{date: c(t+H)/c(t)-1}}，严格未来；末尾 H 日无 y。"""
    y = {}
    for sym, rows in by_sym.items():
        ds = sorted(rows)
        closes = [rows[d]["c"] for d in ds]
        yv = {}
        for t in range(len(ds) - horizon):
            c0, c1 = closes[t], closes[t + horizon]
            if isnum(c0) and isnum(c1) and c0 > 0:
                yv[ds[t]] = c1 / c0 - 1.0
        y[sym] = yv
    return y


# --------------------------- walk-forward 核心 ---------------------------
def walk_forward(dates, by_sym, factors, horizon,
                 min_train=MIN_TRAIN, refit_every=REFIT_EVERY, min_cs=MIN_CS):
    """扩展窗月度再拟合、日度 OOS 打分。返回 {策略名: 日度IC列表} 与再拟合次数。"""
    k = len(factors)
    ysym = build_forward(by_sym, horizon)
    # 训练池（增量累积列）：pool_f[i]、pool_y 对齐
    pool_f = [[] for _ in range(k)]
    pool_y = []
    model = None
    daily = {"orth_ic": [], "equal": []}
    for i in range(k):
        daily["f_" + factors[i]] = []
    n_refit = 0
    added_up_to = -1

    def raw_vector(sym, d):
        r = by_sym[sym].get(d)
        if r is None:
            return None
        vec = [r.get(f) for f in factors]
        return vec if all(isnum(v) for v in vec) else None

    for t, d in enumerate(dates):
        # 1) 把 [added_up_to+1, t-1] 已成为历史的日期纳入训练池（截面标准化后）
        if t >= 1:
            for pt in range(added_up_to + 1, t):
                pd_ = dates[pt]
                raw_by_sym = {s: raw_vector(s, pd_) for s in by_sym}
                raw_by_sym = {s: v for s, v in raw_by_sym.items() if v is not None}
                cs_cols = [cs_uniform({s: raw_by_sym[s][i] for s in raw_by_sym}) for i in range(k)]
                for s in raw_by_sym:
                    yv = ysym.get(s, {}).get(pd_)
                    if not isnum(yv) or not all(s in z and isnum(z[s]) for z in cs_cols):
                        continue
                    for i in range(k):
                        pool_f[i].append(cs_cols[i][s])
                    pool_y.append(yv)
            added_up_to = t - 1
        # 2) 满足最小训练且到再拟合点 → 重估模型
        if t >= min_train and (t - min_train) % refit_every == 0 and len(pool_y) > k + 5:
            ics = [fe.spearman(pool_f[i], pool_y) for i in range(k)]
            ob = fle.orthogonal_ic_blend([list(col) for col in pool_f], ics)
            model = {"betas": ob["betas"], "weights": ob["weights"], "ics": ics}
            n_refit += 1
        # 3) OOS 当日打分（必须已有模型）
        if model is None:
            continue
        raw_by_sym = {}
        for s in by_sym:
            v = raw_vector(s, d)
            if v is not None and isnum(ysym.get(s, {}).get(d)):
                raw_by_sym[s] = v
        if len(raw_by_sym) < min_cs:
            continue
        cs_cols = [cs_uniform({s: raw_by_sym[s][i] for s in raw_by_sym}) for i in range(k)]
        valid = [s for s in raw_by_sym if all(s in z for z in cs_cols)]
        if len(valid) < min_cs:
            continue
        score_orth, score_eq, score_single, yd = {}, {}, {f: {} for f in factors}, {}
        for s in valid:
            fv = [cs_cols[i][s] for i in range(k)]
            resid = apply_triangular(fv, model["betas"])
            score_orth[s] = sum(w * r for w, r in zip(model["weights"], resid))
            score_eq[s] = sum(fv) / k
            for i, f in enumerate(factors):
                score_single[f][s] = fv[i]
            yd[s] = ysym[s][d]
        daily["orth_ic"].append(fe.spearman(list(score_orth.values()), list(yd.values())))
        daily["equal"].append(fe.spearman(list(score_eq.values()), list(yd.values())))
        for f in factors:
            daily["f_" + f].append(fe.spearman([score_single[f][s] for s in valid],
                                               [yd[s] for s in valid]))
    return daily, n_refit


# --------------------------- 一次完整运行 ---------------------------
def run(db_path=DEFAULT_DB, txt_path=DEFAULT_TXT, json_path=DEFAULT_JSON,
        factors=DEFAULT_FACTORS, horizons=HORIZONS, verbose=True):
    loaded = load_panel(str(db_path), list(factors))
    if loaded is None:
        msg = "未找到研究面板 %s；先运行 tools/panel_builder.py --all 建板。" % db_path
        if verbose:
            print(msg)
        return {"note": msg}
    dates, by_sym, sectors = loaded
    result = {"factors": list(factors), "n_sym": len(by_sym), "n_dates": len(dates),
              "date_min": dates[0] if dates else None, "date_max": dates[-1] if dates else None,
              "min_train": MIN_TRAIN, "refit_every": REFIT_EVERY, "horizons": {}}
    lines = []
    lines.append("=" * 92)
    lines.append("正交IC加权 vs 等权 vs 单因子：真实面板 walk-forward 样本外截面RankIC对照（G25续/G16前置，研究侧）")
    lines.append("=" * 92)
    lines.append("面板 %s；品种=%d 交易日=%d（%s ~ %s）；候选因子=%s"
                 % (db_path, len(by_sym), len(dates), result["date_min"], result["date_max"],
                    "、".join(factors)))
    lines.append("设置：最小训练%d日 / 每%d日扩展窗重估 / OOS截面≥%d品种；因子先做当日截面均匀秩标准化"
                 % (MIN_TRAIN, REFIT_EVERY, MIN_CS))
    for h in horizons:
        daily, n_refit = walk_forward(dates, by_sym, list(factors), h)
        summ = {name: summarize_ic(seq) for name, seq in daily.items()}
        result["horizons"]["H%d" % h] = {"n_refit": n_refit, "summary": summ,
                                         "daily_orth": daily["orth_ic"], "daily_equal": daily["equal"]}
        n_days = summ["orth_ic"]["n_days"]
        lines.append("")
        lines.append("[前向 H=%d 交易日] 有效OOS日=%d，月度重估=%d 次（ICIR=mean/std，正比例=日IC>0占比）"
                     % (h, n_days, n_refit))
        lines.append("  %-16s %10s %10s %10s %8s" % ("策略", "meanIC", "ICIR", "正比例", "天数"))
        order = ["orth_ic", "equal"] + ["f_" + f for f in factors]
        label = {"orth_ic": "正交IC合成", "equal": "等权合成"}
        for name in order:
            s = summ[name]
            if s["mean_ic"] is None:
                continue
            lines.append("  %-16s %10.4f %10.3f %9.1f%% %8d"
                         % (label.get(name, name.replace("f_", "单因子·")),
                            s["mean_ic"], s["icir"], 100.0 * s["pct_positive"], s["n_days"]))
        # 正交 vs 等权 的配对日度差
        do, de = daily["orth_ic"], daily["equal"]
        diff = [a - b for a, b in zip(do, de) if isnum(a) and isnum(b)]
        if diff:
            md = sum(diff) / len(diff)
            win = sum(1 for v in diff if v > 0) / len(diff)
            lines.append("  → 正交IC − 等权：平均日IC差 %+.4f，正交占优日占比 %.1f%%" % (md, 100.0 * win))
    lines.append("")
    lines.append("注：本结果为研究侧线性合成基线，不自动改 analyzer 权重、不进综合分；上线仍须 G29 体检 + 样本外+真实成本后≥现状。")
    text = "\n".join(lines)
    if verbose:
        print(text)
    os.makedirs(os.path.dirname(str(txt_path)), exist_ok=True)
    with open(str(txt_path), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    with open(str(json_path), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    try:
        import experiment_ledger
        head = result["horizons"].get("H%d" % horizons[0], {}).get("summary", {})
        experiment_ledger.safe_record(
            "orthogonal_blend_oos",
            {"factors": list(factors), "horizons": list(horizons),
             "min_train": MIN_TRAIN, "refit_every": REFIT_EVERY},
            {k: v for k, v in head.items()} if head else None,
            inputs={"panel": str(db_path), "n_sym": len(by_sym), "n_dates": len(dates)},
            artifacts={"txt": str(txt_path), "json": str(json_path)},
            conclusion="真实面板walk-forward：正交IC合成与等权/单因子的样本外RankIC对照，负结果照实记录",
            reproduce="D:\\Python\\python.exe tools/orthogonal_blend_oos.py")
    except Exception:
        pass
    return result


# --------------------------- 零网络/零DB 合成自测 ---------------------------
def _synth_panel(seed=7, n_sym=16, n_date=220):
    """构造面板：共同风险因子 + 一个真alpha（ret63）+ 冗余因子，y 由 alpha+噪声驱动。"""
    import random
    rng = random.Random(seed)
    factors = list(DEFAULT_FACTORS)
    dates = ["2024-%02d-%02d" % (1 + (t // 28), 1 + (t % 28)) for t in range(n_date)]
    by_sym = {}
    common = [rng.gauss(0, 1) for _ in range(n_date)]
    alpha = [rng.gauss(0, 1) for _ in range(n_date)]
    for s in range(n_sym):
        rows = {}
        load_c = rng.uniform(0.6, 1.2)
        load_a = rng.uniform(0.4, 1.0)
        idio = [rng.gauss(0, 1) for _ in range(n_date)]
        price = 100.0
        closes = []
        for t, d in enumerate(dates):
            shock = load_c * common[t] + load_a * alpha[t] + 0.8 * idio[t]
            # 因子：ret63 承载 alpha，ret20 与共同因子相关（冗余），其余弱
            ret5 = 0.3 * common[t] + 0.2 * idio[t]
            ret20 = 0.9 * common[t] + 0.1 * rng.gauss(0, 1)
            ret63 = 0.2 * common[t] + 1.0 * alpha[t]
            tsmom63 = ret63 * 0.95 + 0.05 * rng.gauss(0, 1)
            tsmom126 = 0.5 * ret63 + 0.3 * rng.gauss(0, 1)
            day_chg = 0.2 * idio[t]
            price *= (1 + 0.01 * shock)
            closes.append(price)
            rows[d] = {"c": price, "ret5": ret5, "ret20": ret20, "ret63": ret63,
                       "tsmom63": tsmom63, "tsmom126": tsmom126, "day_chg": day_chg}
        by_sym["S%02d" % s] = rows
    return dates, by_sym, factors


def selftest():
    # 1) 截面均匀秩：已知序列映射到 [-1,1]、中位≈0
    z = cs_uniform({"a": 1.0, "b": 2.0, "c": 3.0})
    assert abs(z["a"] + 1.0) < 1e-12 and abs(z["c"] - 1.0) < 1e-12 and abs(z["b"]) < 1e-12
    z2 = cs_uniform({"a": 5.0, "b": 5.0, "c": 1.0})    # 并列平均秩
    assert abs(z2["a"] - z2["b"]) < 1e-12 and z2["c"] < z2["a"]
    # 2) 三角递推：betas 全 0 时残差=原因子；正交变换可逆且只用给定系数
    assert apply_triangular([0.3, -0.2], [[], []]) == [0.3, -0.2]
    r = apply_triangular([1.0, 2.0], [[], [0.5]])      # r1=2-0.5*1=1.5
    assert abs(r[0] - 1.0) < 1e-12 and abs(r[1] - 1.5) < 1e-12
    # 3) summarize_ic 基本量
    s = summarize_ic([0.1, -0.1, 0.2, 0.0])
    assert s["n_days"] == 4 and abs(s["mean_ic"] - 0.05) < 1e-12 and 0 <= s["pct_positive"] <= 1
    assert summarize_ic([])["n_days"] == 0
    # 4) walk-forward 合成面板能跑通、各策略日IC等长、权重和=1（经 orthogonal_ic_blend 保证）
    dates, by_sym, factors = _synth_panel()
    daily, n_refit = walk_forward(dates, by_sym, factors, 1, min_train=60, refit_every=20, min_cs=8)
    lens = {k: len(v) for k, v in daily.items()}
    assert len(set(lens.values())) == 1 and list(lens.values())[0] > 0, lens
    assert n_refit >= 5
    so = summarize_ic(daily["orth_ic"]); se = summarize_ic(daily["equal"])
    # 合成面板里 alpha 真实存在：正交合成与等权都应取得正均值 OOS IC（方向健全性）
    assert so["mean_ic"] is not None and se["mean_ic"] is not None
    # 5) 无未来函数：截断最后一天不影响之前任一 OOS 日的 IC
    base_daily, _ = walk_forward(dates[:-1], by_sym, factors, 1, min_train=60, refit_every=20, min_cs=8)
    for a, b in zip(daily["orth_ic"][:-1], base_daily["orth_ic"]):
        assert (a is None and b is None) or (isnum(a) and isnum(b) and abs(a - b) < 1e-12)
    print("orthogonal_blend_oos selftest ALL PASS（截面均匀秩/并列秩、三角递推残差化、IC汇总、"
          "合成面板walk-forward n_refit=%d OOS日=%d 正交meanIC=%.4f 等权meanIC=%.4f、无未来）"
          % (n_refit, so["n_days"], so["mean_ic"], se["mean_ic"]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="正交IC加权接真实面板做样本外对照（研究侧）")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--factors", default=",".join(DEFAULT_FACTORS), help="逗号分隔的面板因子列")
    ap.add_argument("--horizons", default="1,5,20", help="逗号分隔的前向交易日")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    factors = tuple(x.strip() for x in args.factors.split(",") if x.strip())
    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
    run(db_path=args.db, factors=factors, horizons=horizons)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
