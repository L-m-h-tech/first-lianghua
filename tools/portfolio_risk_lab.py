# -*- coding: utf-8 -*-
r"""G5（第47轮，研究侧先行）组合风险实验台 tools/portfolio_risk_lab.py：纯标准库、零网络、只读 G21 面板
（cache/research_panel.db，mode=ro 由 panel_builder 负责），用根模块 portfolio_risk 的纯函数对固定商品篮子做：
  ① 相关结构：平均绝对/带符号相关、板块×板块平均相关、最强/最弱相关对；
  ② 组合 VaR：对 equal/inv_vol/erc/gmv 四套权重，分别给历史模拟法 VaR+ES、参数法 VaR(单日/10日)、
     肥尾溢价（历史/参数）、分散化收益，回答"风险型权重能否真正降低组合尾部风险"；
  ③ 原油压力：以 SC 为驱动，各品种 OLS beta，原油 ±5%/−10% 经 beta 线性传导的组合损益与主要贡献品种。
**只读研究、不接 main、不改 risk_gate 动作/综合分/既有 sizing/持仓**；G5 的熔断动作、第四种 sizing、
组合历史净值回测留后续。出 reports/portfolio_risk_lab.txt|.json，末尾经统一实验台账旁路登记一条。
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import config                                  # noqa: E402
import portfolio_constructor as pc             # noqa: E402
import portfolio_risk as pr                    # noqa: E402
import portfolio_lab as pl                     # noqa: E402 复用稠密面板对齐
import experiment_ledger as el                 # noqa: E402

RISK_TXT = os.path.join(_ROOT, "reports", "portfolio_risk_lab.txt")
RISK_JSON = os.path.join(_ROOT, "reports", "portfolio_risk_lab.json")
OIL_SYM = "SC"                                  # INE 原油，压力情景驱动源
METHODS = ("equal", "inv_vol", "erc", "gmv")
METHOD_CN = {"equal": "等权", "inv_vol": "逆波动", "erc": "风险平价", "gmv": "最小方差"}
LEVELS = (0.95, 0.99)


def _window_by_asset(mat, lookback):
    """行式 mat 取最近 lookback 行，转成 portfolio_risk/constructor 约定的"按资产"序列。"""
    lo = max(0, len(mat) - lookback)
    n = len(mat[0])
    return [[mat[t][i] for t in range(lo, len(mat))] for i in range(n)], lo


def analyze(db_path=os.path.join(_ROOT, "cache", "research_panel.db"),
            lookback=config.PC_LOOKBACK, methods=METHODS):
    """返回 (syms, sectors, common_meta, per_method dict, equal_snap 用于相关/压力展示)。"""
    return_map, sectors, all_syms = pl.load_return_map(db_path)
    dates, syms, mat = pl.dense_matrix(return_map)
    rab, lo = _window_by_asset(mat, lookback)
    sector_of = lambda s: sectors.get(s) or "未知"
    oil_idx = syms.index(OIL_SYM) if OIL_SYM in syms else None

    per = {}
    for m in methods:
        c = pc.construct(rab, m, shrink=config.PC_SHRINK, cap=config.PC_MAX_WEIGHT)
        w = c["weights"]
        snap = pr.risk_snapshot(rab, w, levels=LEVELS, horizons=(1, 10),
                                shrink=config.PC_SHRINK, oil_idx=oil_idx,
                                sector_of=sector_of, syms=syms)
        # 记录权重 Top 与有效N，便于解释 VaR 差异来源
        order = sorted(range(len(syms)), key=lambda i: -w[i])
        snap["top_weights"] = [{"sym": syms[i], "w": w[i]} for i in order[:8] if w[i] > 1e-6]
        snap["eff_n"] = c["eff_n"]
        per[m] = snap
    meta = {"n_universe": len(syms), "n_all": len(all_syms), "window": [dates[lo], dates[-1]],
            "n_days": len(rab[0]), "oil": OIL_SYM if oil_idx is not None else None,
            "lookback": lookback, "shrink": config.PC_SHRINK, "cap": config.PC_MAX_WEIGHT}
    return syms, sectors, meta, per


def _pct(x):
    return ("%.2f%%" % (x * 100)) if isinstance(x, (int, float)) else str(x)


def render(meta, per):
    L = []
    L.append("=" * 108)
    L.append("G5 组合风险实验台 portfolio_risk_lab（纯离线读 G21 面板；只读研究，不接 main、不改 risk_gate/综合分/sizing/持仓）")
    L.append("固定宇宙=%d/%d 品种，风险窗 %s~%s 共%d日；协方差对角收缩%.2f、单票上限%.0f%%、满仓多头权重和=1（未加杠杆）；VaR/ES 损失为正"
             % (meta["n_universe"], meta["n_all"], meta["window"][0], meta["window"][1], meta["n_days"],
                meta["shrink"], meta["cap"] * 100))
    L.append("-" * 108)

    eq = per["equal"]
    # 【一】相关结构（等权宇宙的客观结构，与权重方法无关）
    L.append("【一】相关结构（篮子客观联动，与权重方法无关）")
    L.append("  平均绝对相关 %.3f（系统性联动强度，越接近1越难分散）、平均带符号相关 %+.3f"
             % (eq["avg_abs_corr"], eq["avg_signed_corr"]))
    if "sector_order" in eq:
        secs, block = eq["sector_order"], eq["sector_block"]
        L.append("  板块×板块平均相关（行/列：%s）：" % "、".join(secs))
        L.append("        " + "".join("%-8s" % s[:6] for s in secs))
        for i, s in enumerate(secs):
            L.append("  %-6s" % s[:6] + "".join("%-8.3f" % block[i][j] for j in range(len(secs))))
    L.append("  最强相关对 Top8：" + "、".join("%s-%s=%.2f" % (a, b, r) for a, b, r in eq["strongest_pairs"]))
    L.append("  最弱相关对 Top8：" + "、".join("%s-%s=%.2f" % (a, b, r) for a, b, r in eq["weakest_pairs"]))
    L.append("-" * 108)

    # 【二】组合 VaR 对照
    L.append("【二】组合 VaR / ES 对照（同一篮子、四套权重；历史法含真实肥尾，参数法假设正态）")
    L.append("  %-8s %8s %9s %9s %9s %9s %9s %9s %8s %8s" %
             ("权重", "年化波动", "历史VaR95", "历史ES95", "历史VaR99", "参数VaR95", "参数VaR99", "10日VaR95", "肥尾溢价", "有效N"))
    for m in METHODS:
        s = per[m]
        h95, h99 = s["hist"]["levels"][0.95], s["hist"]["levels"][0.99]
        p95 = s["param"]["levels"][0.95]["var_by_horizon"]
        p99 = s["param"]["levels"][0.99]["var_by_horizon"][1]
        premium = (h95["var"] / p95[1] - 1) if p95[1] > 1e-12 else 0.0
        L.append("  %-8s %8s %9s %9s %9s %9s %9s %9s %+7.0f%% %8.1f%s"
                 % (METHOD_CN[m], _pct(s["param"]["ann_vol"]), _pct(h95["var"]), _pct(h95["es"]),
                    _pct(h99["var"]), _pct(p95[1]), _pct(p99), _pct(p95[10]), premium * 100, s["eff_n"],
                    "  ←基线" if m == "equal" else ""))
    L.append("  读法：历史VaR>参数VaR（肥尾溢价为正）说明真实左尾比正态更厚、参数法会低估风险；ES≥VaR 是条件尾部平均损失；")
    L.append("        风险型权重(inv_vol/erc/gmv)的价值应体现为 VaR/ES 与年化波动更低，而非收益更高。")
    L.append("  分散化收益（1−组合参数VaR/加权单体VaR）：" +
             "、".join("%s %.1f%%" % (METHOD_CN[m], per[m]["div_benefit"] * 100) for m in METHODS))
    L.append("-" * 108)

    # 【三】原油压力
    L.append("【三】原油（%s）压力情景：各品种对原油日收益 OLS beta，冲击经 beta 线性一阶传导（忽略非线性/相关突变）"
             % meta["oil"])
    if "oil_stress" not in eq:
        L.append("  固定宇宙中未找到 %s，跳过原油压力（如需可改 OIL_SYM 为能源代理品种）。" % OIL_SYM)
    else:
        # beta 最强的品种
        betas = eq["oil_betas"]
        syms_universe = _syms_from_snap(eq)
        order_b = sorted(range(len(betas)), key=lambda i: -abs(betas[i]))
        L.append("  对原油 |beta| 最强品种 Top8：" +
                 "、".join("%s=%.2f" % (syms_universe[i], betas[i]) for i in order_b[:8]))
        L.append("  %-14s %10s %10s %10s" % ("权重方案\\原油冲击", "−5%", "−10%", "+5%"))
        for m in METHODS:
            st = per[m]["oil_stress"]
            L.append("  %-16s %+9s %+9s %+9s" %
                     (METHOD_CN[m], _pct(st[-0.05]["total"]), _pct(st[-0.10]["total"]),
                      _pct(st[0.05]["total"])))
        d5 = eq["oil_stress"][-0.05]
        L.append("  等权组合原油−5% 主要拖累：" +
                 "、".join("%s(beta%.2f)=%+.2f%%" % (t["sym"], t["beta"], t["pnl"] * 100)
                           for t in d5["top"][:6]))
    L.append("-" * 108)
    L.append("诚实边界：日收益来自比例复权主连面板、固定宇宙有幸存者偏差；历史法窗仅%d日、极端分位样本少；"
             "参数法假设正态且多日用√h(i.i.d.)；原油压力为线性一阶、beta 用历史窗不代表危机时联动；"
             "本结果只用于研究'组合层风险有多大、风险型权重能否降尾部风险'，不直接产生任何减仓/熔断动作。" % meta["n_days"])
    return "\n".join(L)


def _syms_from_snap(snap):
    """risk_snapshot 未回传 syms，这里从 strongest/top 无法复原全序，故由 analyze 注入；兜底用占位。"""
    return snap.get("_syms", [])


def run(db_path=None, txt_path=RISK_TXT, json_path=RISK_JSON, verbose=True):
    db_path = db_path or os.path.join(_ROOT, "cache", "research_panel.db")
    syms, sectors, meta, per = analyze(db_path)
    for m in per:                    # 注入 syms 供 render 取 beta 标签
        per[m]["_syms"] = syms
    text = render(meta, per)
    if verbose:
        print(text)
    os.makedirs(os.path.dirname(txt_path), exist_ok=True)
    with open(txt_path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(text + "\n")

    def clean(snap):
        return {k: v for k, v in snap.items() if k not in ("corr", "cov", "_syms")}
    payload = {"meta": meta,
               "per_method": {m: clean(per[m]) for m in per},
               "corr_equal": per["equal"]["corr"]}
    with open(json_path, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(payload, fp, ensure_ascii=False, allow_nan=False, indent=1)

    # 统一实验台账（旁路：失败不影响产物）
    try:
        metrics = {}
        for m in METHODS:
            s = per[m]
            metrics[m] = {
                "ann_vol": s["param"]["ann_vol"],
                "hist_var95": s["hist"]["levels"][0.95]["var"],
                "hist_es95": s["hist"]["levels"][0.95]["es"],
                "hist_var99": s["hist"]["levels"][0.99]["var"],
                "param_var95": s["param"]["levels"][0.95]["var_by_horizon"][1],
                "param_var95_10d": s["param"]["levels"][0.95]["var_by_horizon"][10],
                "div_benefit": s["div_benefit"], "eff_n": s["eff_n"],
                "oil_minus5": s.get("oil_stress", {}).get(-0.05, {}).get("total")}
        el.safe_record(
            "portfolio_risk_lab",
            {"lookback": meta["lookback"], "shrink": meta["shrink"], "cap": meta["cap"],
             "levels": list(LEVELS), "oil": meta["oil"], "panel_db": os.path.basename(db_path)},
            metrics, inputs=[db_path], artifacts=[txt_path, json_path],
            conclusion="固定宇宙%d品种 %s~%s：等权历史VaR95=%.2f%%/参数VaR95=%.2f%%、平均绝对相关%.2f、"
                       "erc历史VaR95=%.2f%%、gmv历史VaR95=%.2f%%、原油-5%%等权损益%+.2f%%"
                       % (meta["n_universe"], meta["window"][0], meta["window"][1],
                          per["equal"]["hist"]["levels"][0.95]["var"] * 100,
                          per["equal"]["param"]["levels"][0.95]["var_by_horizon"][1] * 100,
                          per["equal"]["avg_abs_corr"],
                          per["erc"]["hist"]["levels"][0.95]["var"] * 100,
                          per["gmv"]["hist"]["levels"][0.95]["var"] * 100,
                          (per["equal"].get("oil_stress", {}).get(-0.05, {}).get("total") or 0) * 100))
    except Exception:
        pass
    return payload


def selftest():
    # 工具层只验证"窗口切片 + 渲染不崩 + 键齐"，数值正确性由 portfolio_risk.selftest 与 pytest 保证
    mat = [[0.01 * (t % 3 - 1) + 0.001 * i for i in range(5)] for t in range(40)]
    rab, lo = _window_by_asset(mat, 20)
    assert len(rab) == 5 and all(len(x) == 20 for x in rab) and lo == 20
    rab0, lo0 = _window_by_asset(mat, 1000)      # 窗比数据长→取全部
    assert lo0 == 0 and all(len(x) == 40 for x in rab0)
    fake_per = {}
    for m in METHODS:
        w = [0.2] * 5
        snap = pr.risk_snapshot(rab0, w, levels=LEVELS, horizons=(1, 10), oil_idx=0,
                                sector_of=lambda s: "S", syms=["a", "b", "c", "d", "e"])
        snap["eff_n"] = 5.0
        snap["top_weights"] = []
        snap["_syms"] = ["a", "b", "c", "d", "e"]
        fake_per[m] = snap
    meta = {"n_universe": 5, "n_all": 5, "window": ["d0", "d39"], "n_days": 40, "oil": "a",
            "lookback": 126, "shrink": 0.1, "cap": 0.2}
    txt = render(meta, fake_per)
    assert "组合 VaR" in txt and "原油" in txt and "板块" in txt
    print("portfolio_risk_lab selftest ALL PASS（窗口切片/超长窗取全/四方案渲染与压力段不崩 共3组）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="G5 组合风险实验台（纯离线读面板）")
    ap.add_argument("--db", default=os.path.join(_ROOT, "cache", "research_panel.db"))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    run(db_path=args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
