# -*- coding: utf-8 -*-
r"""G23（第34轮）商品 carry / 期限结构因子族 截面多空 + 双样本硬检验（研究侧，不进常驻、不改综合分）。

第33轮全网对标结论：**展期收益 carry 是商品期货最持久、比动量更稳的 alpha**（湘财因子筛选、清华
动量+近远月价差+持仓三因子、高盛期限结构、155年商品收益=现货+carry、Bayes-CID 2026 商品九族）。
项目 fundamental_factors 早有 carry/basis，但只是"单品种当前时点 tanh 打分进综合分"，从未做跨品种
截面排序/分层/多空/双样本。本工具用第31-32轮 XSMOM 的同一套严谨框架把它严格检验一遍：

  - 每个调仓日 t，把当时全部可得品种按 carry（年化展期收益，近高远低=Back 为正）跨品种排序分 5 档，
    等权做多 carry 最高一档、做空最低一档，得市场中性多空组合（赚"谁的展期结构更陡"的相对钱）；
  - 因子族横向对比：carry(近-远)/carry_nn(近-次)/carry_mom(展期变化=basis momentum)/slope/curv
    (Nelson-Siegel 斜率曲率)/doi(总持仓变化，G24 预览对照)；
  - 近4.1年 vs 长样本**同源双样本**，直接复用 xsmom_eval.robust_verdict（含"长窗非重叠期数≥短窗×1.5"
    防伪判据，自动识破上市晚品种凑出的同源小样本）；分板块、留一板块、IS/OOS、真实成本。

数据（与 XSMOM 同链路、结果可直接对照）：
  - 目标收益=主连比例后复权日K的未来 H 日收益（backtest.ratio_adjusted_bars，换月跳空置0）；
  - 因子=term_history 用**真实逐合约日K**重建的历史期限结构（近/次/远月结算价、年化carry、NS载荷、
    总持仓OI），逐合约缓存 cache/term_history.db（可重建、不入库）；
  - t 日因子只用 t 及之前数据（carry 平滑/变化均为滞后窗），无未来函数。

纯标准库、零新增第三方依赖。输出 reports/carry_eval.txt + .json。
用法（项目根目录）：
  D:\Python\python.exe tools\carry_eval.py                  # 全品种、自动下载缺失逐合约并缓存
  D:\Python\python.exe tools\carry_eval.py --codes 螺纹钢,铜 --days 1100
  D:\Python\python.exe tools\carry_eval.py --selftest       # 零网络合成断言
"""
import argparse
import math
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import config  # noqa: E402
import futures_data  # noqa: E402
import backtest  # noqa: E402
import term_history as th  # noqa: E402
import panel_builder as pb  # noqa: E402  G21续：--panel 主连读已复权面板（期限序列仍走 term_history）
import xsmom_eval as xs  # noqa: E402  复用截面/绩效/双样本全套纯函数
import tradable_mask as tmask  # noqa: E402  G22续：可交易性掩码(锁板/交割)截面剔除

# 因子族：(point键, 中文名, 方向+1=因子越大未来收益越高)
CARRY_FACTORS = [
    ("carry", "年化展期(近-远)", +1),
    ("carry_nn", "年化展期(近-次)", +1),
    ("carry_mom", "展期变化basis-mom", +1),
    ("slope", "期限斜率(NS)", +1),
    ("curv", "期限曲率(NS)", +1),
    ("doi", "总持仓变化", +1),
]
MAIN_FACTOR = "carry"


# =========================== 单品种面板点（纯函数，可合成断言） ===========================
def _term_maps(term_series, smooth, mom_k):
    """把期限序列整理成 {date: 因子值}：carry/carry_nn 做 smooth 日平滑，carry_mom=平滑carry的mom_k差分，
    doi=总持仓 mom_k 日变化率；slope/curv 直接取。"""
    dates = [r["date"] for r in term_series]
    cf = [r["carry_far"] for r in term_series]
    cn = [r["carry_nn"] for r in term_series]
    cf_s = th.moving_mean(cf, smooth)
    cn_s = th.moving_mean(cn, smooth)
    cf_chg = th.basis_change(cf_s, mom_k)
    oi = [r.get("oi_sum") for r in term_series]
    doi = [None] * len(oi)
    for i in range(mom_k, len(oi)):
        if oi[i] and oi[i - mom_k] and oi[i - mom_k] > 0:
            doi[i] = oi[i] / oi[i - mom_k] - 1.0
    out = defaultdict(dict)
    for i, d in enumerate(dates):
        out[d] = {"carry": cf_s[i], "carry_nn": cn_s[i], "carry_mom": cf_chg[i],
                  "slope": term_series[i]["slope"], "curv": term_series[i]["curv"], "doi": doi[i]}
    return out


def carry_points_from_adjusted(name, sector, bars, term_series, horizons,
                                smooth=5, mom_k=20, vol_lb=63):
    """对齐主连复权收益与期限因子，产出逐时点点集（不联网、纯函数）。

    每个点同时带两套未来 H 日收益：
      fwd{H}  = 主连比例复权收益（换月跳空置0，**不含展期 roll**，与 XSMOM 同口径、可直接对照）；
      fwdn{H} = 近月连续净值收益（th.near_roll_nav，持续持有当时近月、**含展期 roll**，学术 carry 口径）。
    两套都给，避免"用抹掉 roll 的主连去检验 carry"造成错误证伪。
    """
    if len(bars) < vol_lb + min(horizons) + 2:
        return []
    closes = [futures_data._f(b["c"]) for b in bars]
    dates = [str(b.get("d", "")) for b in bars]
    fwd = xs.forward_returns(closes, tuple(horizons))
    # 近月连续净值（含 roll）按主连交易日轴对齐
    nav_term = th.near_roll_nav(term_series)
    nav_map = {r["date"]: v for r, v in zip(term_series, nav_term)}
    near_nav = [nav_map.get(d) for d in dates]
    fwdn = xs.forward_returns([(v if v is not None else 0.0) for v in near_nav], tuple(horizons))
    # near_nav 暖机/缺失处（None）对应前向收益置 None
    for H in horizons:
        for t in range(len(dates)):
            if near_nav[t] is None:
                fwdn[H][t] = None
    fmaps = _term_maps(term_series, smooth, mom_k)
    # G23续（第65轮）：合约级成交量/结算价对齐表（按交易日轴，与 main bars 对齐）
    term_vol_map = {r["date"]: r for r in term_series}
    pts = []
    for t in range(vol_lb, len(closes)):
        d = dates[t]
        fm = fmaps.get(d)
        if fm is None or fm.get(MAIN_FACTOR) is None:
            continue
        p = {"sym": name, "sector": sector, "date": d}
        for fk, _lab, _dir in CARRY_FACTORS:
            p[fk] = fm.get(fk)
        p["vol%d" % vol_lb] = futures_data._window_std(closes, t, vol_lb)
        # G23续（第64轮）换手/容量：v=当日成交量(手)、oi=总持仓(手)、vol_turn=换手率代理、amount=成交额代理(元)
        b = bars[t]
        p["v"] = futures_data._f(b.get("v"))
        p["oi"] = futures_data._f(b.get("p") if b.get("p") is not None else b.get("oi"))
        p["vol_turn"] = (p["v"] / p["oi"]) if (p["v"] and p["oi"] and p["oi"] > 0) else None
        p["amount"] = (closes[t] * p["v"]) if (p["v"] and closes[t] > 0) else None
        # G23续（第65轮）：真实逐合约成交额——近月合约成交量×结算价（term_history 合约级 vol/结算）
        tr = term_vol_map.get(d) or {}
        p["near_vol"] = tr.get("near_vol")
        p["vol_sum"] = tr.get("vol_sum")
        p["near_amount"] = (tr.get("near_s") * p["near_vol"]) if (tr.get("near_s") and p.get("near_vol")) else None
        ok = True
        for H in horizons:
            p["fwd%d" % H] = fwd[H][t]
            p["fwdn%d" % H] = fwdn[H][t]
            if p["fwd%d" % H] is None or p["fwdn%d" % H] is None:
                ok = False
        if ok and all(math.isfinite(p[fk]) for fk, _l, _d in CARRY_FACTORS
                      if p[fk] is not None):
            pts.append(p)
    return pts


def build_carry_points(name, sector, raw_main_bars, term_series, horizons,
                       smooth=5, mom_k=20, vol_lb=63, days=2500):
    """网络旧路径：主连 raw[-days:] 比例复权 -> carry_points_from_adjusted（历史逐值一致）。"""
    bars, _roll = backtest.ratio_adjusted_bars(raw_main_bars[-days:])
    return carry_points_from_adjusted(name, sector, bars, term_series, horizons,
                                      smooth, mom_k, vol_lb)


def retarget(points, target):
    """切换截面收益目标：'main'=主连复权(不含roll)原样；'near'=把 fwdn{H}(近月连续含roll) 覆盖到 fwd{H}。"""
    if target == "main":
        return points
    out = []
    for p in points:
        q = dict(p)
        for k, v in p.items():
            if k.startswith("fwdn"):
                q["fwd" + k[4:]] = v
        out.append(q)
    return out


# =========================== 联网采集（逐合约缓存，缺失才下载） ===========================
def _ym_range_for(days, future_months=9, back_buffer_months=8):
    """由需要的交易日天数推算逐合约枚举的起止年月：前推(覆盖最早近月合约的上市期)+后延(覆盖未来近月)。"""
    years = days / 252.0
    back = int(years * 12) + back_buffer_months
    today = datetime.today()
    sy, sm = today.year, today.month
    for _ in range(back):
        sm -= 1
        if sm < 1:
            sm, sy = 12, sy - 1
    ey, em = today.year, today.month
    for _ in range(future_months):
        em += 1
        if em > 12:
            em, ey = 1, ey + 1
    return (sy % 100, sm, ey % 100, em)


def _fetch_one_carry(item, days, horizons, smooth, mom_k, vol_lb, workers_inner, store,
                         prefer_panel=False):
    name, main_code = item
    meta = config.VARIETIES.get(name, {})
    sym = meta.get("sym") or main_code.rstrip("0")
    sector = meta.get("cat", "其他")
    try:
        syy, smm, eyy, emm = _ym_range_for(days)
        th.build_symbol_range(sym, syy, smm, eyy, emm, store, workers=workers_inner, pause=0.0)
        term_series = th.term_series_for(sym, store)
        if prefer_panel:
            bars, _src = pb.load_adjusted_bars(main_code, days, prefer_panel=True)
            pts = carry_points_from_adjusted(name, sector, bars, term_series, horizons,
                                             smooth, mom_k, vol_lb)
        else:
            raw = futures_data.fetch_daily_kline(main_code)
            pts = build_carry_points(name, sector, raw, term_series, horizons,
                                     smooth, mom_k, vol_lb, days)
        if not pts:
            return name, [], "期限/暖机不足(term天数=%d)" % len(term_series)
        return name, pts, ""
    except Exception as e:
        return name, [], "%s: %s" % (type(e).__name__, e)


def collect_carry_points(items, days, horizons, smooth, mom_k, vol_lb, workers, workers_inner, store,
                         prefer_panel=False):
    points, errors = [], []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = [pool.submit(_fetch_one_carry, it, days, horizons, smooth, mom_k,
                            vol_lb, workers_inner, store, prefer_panel) for it in items]
        for k, fut in enumerate(as_completed(futs), 1):
            name, pts, err = fut.result()
            if pts:
                points.extend(pts)
            elif err:
                errors.append((name, err))
            if k % 8 == 0:
                print("  ...%d/%d 品种完成，用时%.0fs" % (k, len(futs), time.time() - t0))
    points.sort(key=lambda p: (p["date"], p["sym"]))
    return points, errors


# =========================== 报告 ===========================
def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _pf_line(pf):
    if pf is None:
        return "无样本"
    return ("n=%d 毛均%+.3f%% 净均%+.3f%% 净t=%+.2f 胜率%.0f%% 净累计%+.1f%% 年化%+.1f%% 夏普%.2f 回撤%.1f%%"
            % (pf["n"], pf["gross_mean"] * 100, pf["net_mean"] * 100, pf["net_t"],
               pf["win"] * 100, pf["net_cum"] * 100, pf["annual"] * 100, pf["sharpe"], pf["max_dd"] * 100))


def build_report(points, errors, long_panel, main_dates, factor, horizons, main_h, vol_lb,
                 n_q, min_names, cond_min, cost_round, tmin, mono_gate, max_drive,
                 decay_tol, long_n_ratio, smooth, mom_k, days, main_days_n, near_panel=None):
    long_dates, long_by = long_panel
    if near_panel is not None:
        near_dates, near_by = near_panel
    else:
        near_dates, near_by = long_dates, long_by
    main_by = long_by  # by_date 共享，main_dates 做窗口截断
    n_sym = len({p["sym"] for p in points})
    L = []
    L.append("=" * 108)
    L.append(" G23 商品 carry/期限结构 截面多空双样本评估  生成于 %s" % _now())
    L.append("=" * 108)
    L.append("样本：%d 个品种、%d 个(品种×交易日)点；主窗=全局最近 %d 个调仓日(交易日)，长样本 %d 交易日；"
             % (n_sym, len(points), main_days_n, days))
    L.append("      因子来自真实逐合约重建的历史期限结构(近/次/远结算价)，carry平滑%d日、basis-mom/DOI窗%d日；"
             % (smooth, mom_k))
    L.append("      目标=主连比例复权未来%d日收益；每调仓日跨品种按因子排序分%d档、多最高档/空最低档(市场中性)。"
             % (main_h, n_q))
    L.append("成本：单方向一次往返 %.4f%%、多空两腿 %.4f%%（银河真实费+滑点口径，与 XSMOM 一致）。"
             % (cost_round * 100, 2 * cost_round * 100))
    if errors:
        L.append("取数不足/失败品种 %d 个(示例)：%s" %
                 (len(errors), "；".join("%s:%s" % (n, e) for n, e in errors[:6])))

    # ---------- 一、主因子分档 + 多空绩效（主窗） ----------
    L.append("\n一、主因子【%s】主窗截面分档与多空绩效" % factor)
    pers = xs.cross_section_periods(main_dates, main_by, factor, main_h, vol_lb, n_q,
                                    min_names, "equal", main_h)
    pf = xs.perf_stats(pers, main_h, cost_round, "ls")
    bp = xs.bands_profile(pers, n_q) if pers else None
    if bp:
        qline = "  ".join("Q%d:%+.3f%%" % (i + 1, v * 100) for i, v in enumerate(bp["means"]))
        L.append("  各档平均未来收益：" + qline)
        L.append("  分档单调性 %.0f%%、档位列序RankIC=%+.3f、Q%d-Q1价差%+.3f%%"
                 % (bp["mono"] * 100, bp["col_rank_ic"], n_q, bp["spread"] * 100))
    L.append("  多空(净)：" + _pf_line(pf))
    pf_gross = xs.perf_stats(pers, main_h, 0.0, "ls")
    L.append("  多空(毛)：" + _pf_line(pf_gross))
    # IS/OOS
    isp, osp = xs.split_is_oos(pers, 0.3)
    L.append("  IS(前70%%)：%s" % _pf_line(xs.perf_stats(isp, main_h, cost_round, "ls")))
    L.append("  OOS(后30%%)：%s" % _pf_line(xs.perf_stats(osp, main_h, cost_round, "ls")))
    # 近月连续（含展期 roll）口径——学术 carry 的正确收益，排除"主连复权抹掉 roll"的方法瑕疵
    pers_n = xs.cross_section_periods(main_dates, near_by, factor, main_h, vol_lb, n_q,
                                      min_names, "equal", main_h)
    pf_near = xs.perf_stats(pers_n, main_h, cost_round, "ls")
    L.append("  【对照·近月连续含roll口径】：%s" % _pf_line(pf_near))

    # ---------- 二、持有期 H 网格（主因子，主窗） ----------
    L.append("\n二、主因子持有期 H 网格（净多空 t / 净均收%% / 单调性）")
    L.append("  H      净t      净均收%%    胜率%%    单调%%    Q%d-Q1%%" % n_q)
    grid = {}
    for H in horizons:
        pp = xs.cross_section_periods(main_dates, main_by, factor, H, vol_lb, n_q,
                                      min_names, "equal", H)
        pfp = xs.perf_stats(pp, H, cost_round, "ls")
        bpp = xs.bands_profile(pp, n_q) if pp else None
        grid[H] = {"perf": pfp, "bands": bpp}
        if pfp and bpp:
            L.append("  %-5d  %+6.2f   %+7.3f   %5.0f   %5.0f   %+6.3f"
                     % (H, pfp["net_t"], pfp["net_mean"] * 100, pfp["win"] * 100,
                        bpp["mono"] * 100, bpp["spread"] * 100))

    # ---------- 三、因子族横向对比（主窗，主连 vs 近月含roll 两口径） ----------
    L.append("\n三、因子族横向对比（主窗 H=%d；净多空 t：主连不含roll / 近月含roll）" % main_h)
    L.append("  因子            主连t   近月t   主连净均%%   夏普    单调%%   Q%d-Q1%%" % n_q)
    family = {}
    for fk, lab, _d in CARRY_FACTORS:
        pp = xs.cross_section_periods(main_dates, main_by, fk, main_h, vol_lb, n_q,
                                      cond_min, "equal", main_h)
        ppn = xs.cross_section_periods(main_dates, near_by, fk, main_h, vol_lb, n_q,
                                       cond_min, "equal", main_h)
        pfp = xs.perf_stats(pp, main_h, cost_round, "ls")
        pfp_n = xs.perf_stats(ppn, main_h, cost_round, "ls")
        bpp = xs.bands_profile(pp, n_q) if pp else None
        family[fk] = {"perf": pfp, "perf_near": pfp_n, "bands": bpp, "label": lab}
        if pfp and bpp:
            tn = pfp_n["net_t"] if pfp_n else float("nan")
            L.append("  %-14s  %+6.2f %+6.2f  %+7.3f  %6.2f  %5.0f  %+6.3f"
                     % (fk, pfp["net_t"], tn, pfp["net_mean"] * 100, pfp["sharpe"],
                        bpp["mono"] * 100, bpp["spread"] * 100))
        else:
            L.append("  %-14s  样本不足" % fk)

    # ---------- 四、板块分解 + 留一板块 ----------
    L.append("\n四、板块内截面与留一板块(LO-SO)稳健性（主因子，毛多空均收）")
    sec_int = xs.sector_internal(main_dates, main_by, factor, main_h, vol_lb, n_q, cond_min, main_h)
    for sec, v in sorted(sec_int.items(), key=lambda kv: -kv[1]["mean"]):
        L.append("  板块内 %-6s n=%-3d 均收%+.3f%% 胜率%.0f%%" % (sec, v["n"], v["mean"] * 100, v["win"] * 100))
    exposure, loso = xs.sector_breakdown(pers)
    if exposure:
        worst = max(exposure.items(), key=lambda kv: abs(kv[1]["net"]))
        L.append("  多空腿净敞口最大板块：%s（占比%.0f%%）" % (worst[0], abs(worst[1]["net"]) * 100))
    if loso:
        L.append("  留一板块后多空毛均收：" +
                 " ".join("%s%+.3f%%" % (s, v * 100) for s, v in sorted(loso.items())))

    # ---------- 五、双样本稳健（近4.1年 vs 长样本；多候选） ----------
    L.append("\n五、双样本稳健硬检验（短窗=主窗最近%d交易日，长窗=%d交易日；长窗期数须≥短窗×%.1f）"
             % (main_days_n, days, long_n_ratio))
    candidates = [
        ("全市场·多空(基线)", None, "ls"),
        ("全市场·多头超额", None, "lex"),
        ("全市场·纯多头", None, "long"),
    ]
    for sec in sorted({p["sector"] for p in points}):
        candidates.append(("%s池·多空" % sec, (sec,), "ls"))
    windows = [("近窗", (main_dates, main_by)), ("长窗", (long_dates, long_by))]
    scan = xs.conditional_scan(windows, factor, vol_lb, main_h, n_q, cond_min,
                               cost_round, candidates)
    L.append("  候选                 短窗n/t/净均%            长窗n/t/净均%            稳健?")
    n_robust = 0
    for cname, row in scan.items():
        sp, lp = row["windows"]["近窗"], row["windows"]["长窗"]
        ok, why = xs.robust_verdict(row, tmin, decay_tol, long_n_ratio)
        n_robust += 1 if ok else 0
        ss = ("n=%d,t=%+.2f,%+.3f" % (sp["n"], sp["net_t"], sp["net_mean"] * 100)) if sp else "无"
        ll = ("n=%d,t=%+.2f,%+.3f" % (lp["n"], lp["net_t"], lp["net_mean"] * 100)) if lp else "无"
        L.append("  %-18s %-22s %-22s %s" % (cname[:18], ss, ll, "稳健✔" if ok else "不稳健✘"))
        if not ok:
            for w in why:
                L.append("        └ %s" % w)

    # ---------- 五·补、近月连续(含roll)口径双样本（学术 carry 正确收益口径） ----------
    near_candidates = [("近月口径·全市场多空", None, "ls"),
                       ("近月口径·多头超额", None, "lex"),
                       ("近月口径·纯多头", None, "long")]
    near_windows = [("近窗", (main_dates, near_by)), ("长窗", (near_dates, near_by))]
    scan_near = xs.conditional_scan(near_windows, factor, vol_lb, main_h, n_q, cond_min,
                                    cost_round, near_candidates)
    n_robust_near = 0
    L.append("\n五·补、近月连续(含展期roll)口径双样本（排除主连复权抹掉roll的可能）")
    for cname, row in scan_near.items():
        sp, lp = row["windows"]["近窗"], row["windows"]["长窗"]
        ok, why = xs.robust_verdict(row, tmin, decay_tol, long_n_ratio)
        n_robust_near += 1 if ok else 0
        ss = ("n=%d,t=%+.2f,%+.3f" % (sp["n"], sp["net_t"], sp["net_mean"] * 100)) if sp else "无"
        ll = ("n=%d,t=%+.2f,%+.3f" % (lp["n"], lp["net_t"], lp["net_mean"] * 100)) if lp else "无"
        L.append("  %-20s %-22s %-22s %s" % (cname, ss, ll, "稳健✔" if ok else "不稳健✘"))
        for w in why:
            L.append("        └ %s" % w)

    # ---------- 裁决 ----------
    leg_long = sum(p["long"] for p in pers) / len(pers) if pers else 0.0
    leg_short = sum(p["short_pnl"] for p in pers) / len(pers) if pers else 0.0
    oos_pf = xs.perf_stats(osp, main_h, cost_round, "ls")
    ok_gate, why_gate = xs.gate_verdict(pf, oos_pf, bp, leg_long, leg_short, exposure,
                                        tmin, mono_gate, max_drive)
    L.append("\n六、主组合裁决（确定不更差门槛：净t≥%.1f、净均收>0、OOS不转负、单调性≥%.0f%%、单板块敞口≤%.0f%%）"
             % (tmin, mono_gate * 100, max_drive * 100))
    L.append("  结果：%s" % ("通过✔（可进入下一轮'影子对照'讨论，仍不自动进综合分）" if ok_gate else "不通过✘（维持研究归档，不进分、不挂影子）"))
    for w in why_gate:
        L.append("  └ %s" % w)
    L.append("  双样本稳健候选数：%d / %d（主连口径）；近月含roll口径稳健 %d / %d（=0 则与 TSMOM/XSMOM 同样按负结果诚实归档）"
             % (n_robust, len(candidates), n_robust_near, len(near_candidates)))

    # ---------- 七、两口径机制判读（防止把"展期收益"误读成"价格择时"） ----------
    main_t = pf["net_t"] if pf else 0.0
    near_t_s = pf_near["net_t"] if pf_near else 0.0
    near_long_row = scan_near["近月口径·全市场多空"]["windows"]["长窗"]
    near_t_l = near_long_row["net_t"] if near_long_row else 0.0
    near_mean_l = near_long_row["net_mean"] if near_long_row else 0.0
    L.append("\n七、机制判读（两口径对照，务必一起读）")
    L.append("  · 主连复权口径(不含roll)净t=%+.2f：衡量 carry 对未来【价格方向】的预测；近月连续含roll口径"
             "短窗t=%+.2f、长窗t=%+.2f（长窗净均%+.3f%%/期），衡量学术 carry 的【展期+价格】全收益。"
             % (main_t, near_t_s, near_t_l, near_mean_l * 100))
    if near_t_l >= tmin and near_t_s > 0 and main_t < 0:
        reading = ("边缘候选（区别于动量的双样本全负）：carry 在含展期的学术口径下长样本显著为正(t=%+.2f)、"
                   "方向与'商品最持久alpha=展期收益'文献一致，而主连价格口径为负(t=%+.2f)——说明它赚的是"
                   "【展期roll的钱、不是价格方向的钱】，不是择时信号。卡点：近4年短窗含roll t=%+.2f 未达门槛%.1f，"
                   "故本轮仍不进综合分、不挂实时影子，归档为'长样本成立/近窗边际减弱'的待跟踪候选，"
                   "下一轮可做更长样本、板块条件化与换手/容量检验后再议。" % (near_t_l, main_t, near_t_s, tmin))
    elif n_robust_near >= 1 or (near_t_s >= tmin and near_t_l >= tmin):
        reading = "近月含roll口径双样本均达标，可进入下一轮'影子对照'讨论（仍不自动进综合分）。"
    else:
        reading = ("两口径均未过双样本门槛：主连t=%+.2f、近月短窗t=%+.2f/长窗t=%+.2f，按负结果归档，不进分。"
                   % (main_t, near_t_s, near_t_l))
    L.append("  · 判读：" + reading)

    # ---------- 七·补、G23续 换手/容量检验（研究边缘：capacity 数量级精确仍待 G14 一档盘口） ----------
    L.append("\n七·补、换手与容量（G23续；容量=多空腿成交额日合计×参与率/名义，参与率 1% 数量级估算，精确待 G14）")
    try:
        cap_infos = []
        for p in pers or []:
            syms_all = [s for s in (p.get("long_syms") or []) + (p.get("short_syms") or [])]
            amt_by_sym = {q["sym"]: (q.get("amount") or 0.0) for q in points
                          if q.get("date") == p.get("date") and q.get("sym") in syms_all}
            namt_by_sym = {q["sym"]: (q.get("near_amount") or 0.0) for q in points
                           if q.get("date") == p.get("date") and q.get("sym") in syms_all}
            total_amt = sum(amt_by_sym.values())
            total_namt = sum(namt_by_sym.values())
            cap_infos.append({"date": p.get("date"), "long_syms": p.get("long_syms") or [],
                              "short_syms": p.get("short_syms") or [], "total_amount": total_amt,
                              "total_near_amount": total_namt})
        if cap_infos:
            amts = [c["total_amount"] for c in cap_infos if c["total_amount"] > 0]
            if amts:
                med_amt = sorted(amts)[len(amts) // 2]
                L.append("  每调仓日多空腿【主连代理】成交额合计：中位%.0f万元；按参与率1%%估算单期容量=%.0f万元"
                         % (med_amt / 1e4, med_amt * 0.01 / 1e4))
            # G23续（第65轮）：真实逐合约口径（近月合约结算价×近月成交量，term_history 合约级 vol）
            namts = [c["total_near_amount"] for c in cap_infos if c["total_near_amount"] > 0]
            if namts:
                med_namt = sorted(namts)[len(namts) // 2]
                L.append("  每调仓日多空腿【真实逐合约】成交额合计：中位%.0f万元；按参与率1%%估算单期容量=%.0f万元"
                         "（近月结算×近月成交量；精确逐笔容量仍待 G14 一档盘口）"
                         % (med_namt / 1e4, med_namt * 0.01 / 1e4))
            # 换手代理：多空腿成员的相对持仓集中度（腿内等权=1/n，n 越少越集中）
            n_lens = [len(c["long_syms"]) + len(c["short_syms"]) for c in cap_infos if c["long_syms"] and c["short_syms"]]
            if n_lens:
                L.append("  多空腿总成员数：平均%.0f个/期（腿数越少换手越敏感；日频多空持仓换手须 G14 盘口精确）"
                         % (sum(n_lens) / len(n_lens)))
    except Exception:
        L.append("  容量/换手代理计算异常（无 v/oi 时跳过）——不阻断主报告")
    L.append("=" * 108)
    text = "\n".join(L)

    sidecar = {"n_symbols": n_sym, "factor": factor, "main_perf": pf,
               "main_perf_near": pf_near, "grid": {str(H): v for H, v in grid.items()},
               "family": {fk: {"perf": v["perf"], "perf_near": v.get("perf_near"),
                               "mono": v["bands"]["mono"] if v["bands"] else None}
                          for fk, v in family.items()},
               "sector_internal": sec_int, "conditional": {
                   cname: {"windows": {wn: (None if pp is None else {k: pp[k] for k in
                          ("n", "net_mean", "net_t", "win", "sharpe")}) for wn, pp in row["windows"].items()},
                           "n_periods": row["n_periods"],
                           "robust": xs.robust_verdict(row, tmin, decay_tol, long_n_ratio)[0]}
                   for cname, row in scan.items()},
               "conditional_near": {
                   cname: {"robust": xs.robust_verdict(row, tmin, decay_tol, long_n_ratio)[0],
                           "windows": {wn: (None if pp is None else {k: pp[k] for k in
                                      ("n", "net_mean", "net_t", "win", "sharpe")})
                                      for wn, pp in row["windows"].items()}}
                   for cname, row in scan_near.items()},
               "verdict": {"ok": ok_gate, "reasons": why_gate,
                           "n_robust": n_robust, "n_robust_near": n_robust_near,
                           "main_t": main_t, "near_t_short": near_t_s,
                           "near_t_long": near_t_l, "reading": reading},
               "errors": errors, "generated": _now()}
    return text, sidecar, {"ok": ok_gate, "reasons": why_gate, "main": pf}


# =========================== 主流程 ===========================
def run(argv=None):
    ap = argparse.ArgumentParser(description="G23 商品 carry 截面多空双样本评估（研究侧）")
    ap.add_argument("--codes", default="", help="逗号分隔中文名/主连，缺省=全品种")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--days", type=int, default=2500, help="长样本日K根数（默认2500≈9.9年）")
    ap.add_argument("--main-days", type=int, default=config.XSMOM_EVAL_DAYS, help="主窗交易日数（默认1023≈4.1年）")
    ap.add_argument("--factor", default=MAIN_FACTOR, choices=[f[0] for f in CARRY_FACTORS])
    ap.add_argument("--horizons", default="5,20,60")
    ap.add_argument("--main-h", type=int, default=20)
    ap.add_argument("--smooth", type=int, default=5, help="carry 平滑交易日数")
    ap.add_argument("--mom-k", type=int, default=20, help="basis-momentum/DOI 变化窗")
    ap.add_argument("--vol-lb", type=int, default=63)
    ap.add_argument("--quantiles", type=int, default=5)
    ap.add_argument("--min-names", type=int, default=16)
    ap.add_argument("--cond-min-names", type=int, default=8)
    ap.add_argument("--tmin", type=float, default=config.XSMOM_TMIN)
    ap.add_argument("--mono-gate", type=float, default=0.75)
    ap.add_argument("--max-sector-drive", type=float, default=config.XSMOM_MAX_SECTOR_DRIVE)
    ap.add_argument("--decay-tol", type=float, default=config.XSMOM_DECAY_TOL)
    ap.add_argument("--long-n-ratio", type=float, default=config.XSMOM_LONG_N_RATIO)
    ap.add_argument("--fee-rate", type=float, default=config.BACKTEST_FEE_RATE)
    ap.add_argument("--slip-rate", type=float, default=config.BACKTEST_SLIP_RATE)
    ap.add_argument("--workers", type=int, default=4, help="品种级并发")
    ap.add_argument("--workers-inner", type=int, default=8, help="品种内逐合约并发")
    ap.add_argument("--db", default=th.TERM_DB_PATH)
    ap.add_argument("--out", default="reports/carry_eval.txt")
    ap.add_argument("--json", default="reports/carry_eval.json")
    ap.add_argument("--panel", action="store_true", help="G21续：主连读已复权面板（期限仍走term_history；面板约1023日，长2500样本请用缺省网络）")
    ap.add_argument("--mask", action="store_true", help="G22续：读 research_panel.db 算可交易性掩码（疑似锁板/距交割月1号≤15天）并剔除不可交易点后重做截面多空对照")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
    main_h = args.main_h if args.main_h in horizons else horizons[len(horizons) // 2]
    cost_round = 2.0 * (args.fee_rate + args.slip_rate)
    items = backtest.resolve_codes(args.codes, args.limit if args.limit > 0 else None)
    store = th.TermHistoryStore(args.db)
    try:
        print("carry_eval：%d 个品种，逐合约缺失才下载并缓存到 %s ..." % (len(items), args.db))
        points, errors = collect_carry_points(
            items, args.days, horizons, args.smooth, args.mom_k, args.vol_lb,
            args.workers, args.workers_inner, store, getattr(args, 'panel', False))
    finally:
        store.close()
    if not points:
        print("无可用样本，错误示例：%s" % errors[:5])
        return 2
    points_main = retarget(points, "main")
    points_near = retarget(points, "near")
    # G22续：可交易性掩码剔除（--mask；只读 research_panel.db，零网络）
    mask_notes = ""
    if args.mask:
        try:
            from collections import defaultdict as _dd
            db_p = str(ROOT / "cache" / "research_panel.db")
            if os.path.exists(db_p):
                import sqlite3 as _sq
                con = _sq.connect(db_p)
                rows_by_date = _dd(dict)
                for row in con.execute("SELECT sym,date,c,h,l FROM research_panel ORDER BY sym,date"):
                    rows_by_date[row[1]][row[0]] = {"c": row[2], "h": row[3], "l": row[4]}
                con.close()
                mask = tmask.mask_for_panel(rows_by_date)
                fm_main = tmask.filter_points(points_main, mask)
                fm_near = tmask.filter_points(points_near, mask)
                points_main = fm_main["points"]
                points_near = fm_near["points"]
                mask_notes = ("；G22续掩码：原%d点→剔锁板%d+临近交割%d→剩%d点" %
                              (fm_main["original"], fm_main["removed_locked"],
                               fm_main["removed_near"], fm_main["filtered"]))
            else:
                mask_notes = "；G22续掩码：research_panel.db 不存在，跳过"
        except Exception as e:
            mask_notes = "；G22续掩码计算失败（不影响主流程）: %s" % type(e).__name__
    long_dates, long_by = xs.build_panel(points_main)
    _, near_by = xs.build_panel(points_near)   # 交易日历与 main 相同，只 fwd 值不同（含 roll）
    main_dates = xs.truncate_dates(long_dates, args.main_days)
    main_set = set(main_dates)
    main_points = [p for p in points_main if p["date"] in main_set]
    text, sidecar, verdict = build_report(
        main_points, errors, (long_dates, long_by), main_dates, args.factor, horizons,
        main_h, args.vol_lb, args.quantiles, args.min_names, args.cond_min_names,
        cost_round, args.tmin, args.mono_gate, args.max_sector_drive,
        args.decay_tol, args.long_n_ratio, args.smooth, args.mom_k, args.days, len(main_dates),
        near_panel=(long_dates, near_by))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8-sig") as f:
        f.write(text)
    import json
    with open(args.json, "w", encoding="utf-8") as f:
        f.write(json.dumps(sidecar, ensure_ascii=False, indent=1))
    print(text)
    print("品种时点 %d、覆盖品种 %d；主裁决 ok=%s；双样本稳健候选 %d%s；报告 -> %s；JSON -> %s"
          % (len(main_points), sidecar["n_symbols"], verdict["ok"],
             sidecar["verdict"]["n_robust"], mask_notes, args.out, args.json))
    return 0


# =========================== 合成自测（零网络） ===========================
def _synthetic_carry_panel(n_sym=20, n_days=320, seed=7):
    """构造确定性面板：品种 i 的 carry 固定从负到正排列，且未来收益与 carry 正相关（Back 越深未来越强），
    另给无关的 OI/噪声；-> carry 截面多空为正、分档单调，而 doi 无预测力。直接产出 points。"""
    import random
    rng = random.Random(seed)
    carries = [(-0.04 + 0.08 * i / (n_sym - 1)) for i in range(n_sym)]  # 从 Contango 到 Back
    points = []
    Hs = (5, 20)
    for si in range(n_sym):
        base = carries[si]
        price = 100.0
        closes, dates = [], []
        carry_path = []
        for t in range(n_days):
            # 未来一日漂移与 carry 正相关（信号强于噪声，保证最高carry档未来收益稳定最高）
            drift = base * 0.08 + rng.gauss(0, 0.003)
            price = max(1.0, price * (1 + drift))
            closes.append(price)
            dates.append("2025-%02d-%02d" % (t // 28 % 12 + 1, t % 28 + 1))
            carry_path.append(base + rng.gauss(0, 0.002))
        fwd = xs.forward_returns(closes, Hs)
        for t in range(63, len(closes)):
            p = {"sym": "V%02d" % si, "sector": "板块%d" % (si % 4), "date": dates[t],
                 "carry": carry_path[t], "carry_nn": carry_path[t],
                 "carry_mom": carry_path[t] - carry_path[t - 20],
                 "slope": carry_path[t], "curv": 0.0,
                 "doi": rng.gauss(0, 0.01), "vol63": 0.01,
                 "v": 1000 + si * 10, "oi": 5000 + si * 20,
                 "vol_turn": (1000 + si * 10) / (5000 + si * 20),
                 "amount": price * (1000 + si * 10)}
            ok = True
            for H in Hs:
                p["fwd%d" % H] = fwd[H][t]
                if p["fwd%d" % H] is None:
                    ok = False
            if ok:
                points.append(p)
    return points


def selftest():
    # 1) _term_maps：平滑、basis-mom、DOI 变化率，头部暖机为 None
    ts = [{"date": "d%d" % i, "carry_far": float(i), "carry_nn": float(i),
           "slope": 0.1, "curv": 0.0, "oi_sum": 100 + i} for i in range(10)]
    fm = _term_maps(ts, smooth=2, mom_k=2)
    assert fm["d0"]["carry"] is None            # 2日平滑暖机
    assert abs(fm["d3"]["carry"] - 2.5) < 1e-12
    assert fm["d1"]["carry_mom"] is None
    assert abs(fm["d3"]["carry_mom"] - (2.5 - 0.5)) < 1e-12
    assert abs(fm["d2"]["doi"] - (102 / 100 - 1)) < 1e-12

    # 2) _ym_range_for 返回合法两位年月、终点在未来
    sy, sm, ey, em = _ym_range_for(1023)
    assert 1 <= sm <= 12 and 1 <= em <= 12 and (ey, em) >= (sy, sm)

    # 3) build_carry_points 端到端对齐（用构造的主连+期限序列，不联网）
    raw = [{"d": "2025-%02d-%02d" % (t // 28 % 12 + 1, t % 28 + 1),
            "o": 100 + t, "h": 101 + t, "l": 99 + t, "c": 100 + t, "v": 1, "p": 1}
           for t in range(120)]
    term = [{"date": raw[t]["d"], "near": "A", "next": "B", "far": "C",
             "carry_far": 0.02, "carry_nn": 0.01, "slope": 0.001, "curv": 0.0,
             "near_s": 100.0 + t, "oi_sum": 1000 + t, "oi_near": 100, "n_live": 3}
            for t in range(120)]
    pts = build_carry_points("测试", "板块X", raw, term, (5, 20), 5, 20, 63, 9999)
    assert pts and all("carry" in p and "fwd20" in p and "fwdn20" in p and p["carry"] is not None for p in pts)
    # retarget：near 口径把 fwdn 覆盖到 fwd；main 原样
    assert retarget([{"fwd5": 1, "fwdn5": 2}], "near")[0]["fwd5"] == 2
    assert retarget([{"fwd5": 1, "fwdn5": 2}], "main")[0]["fwd5"] == 1

    # 4) 合成 carry 面板：截面多空为正、t>0、分档单调；无关因子 doi 无方向
    points = _synthetic_carry_panel()
    dates, by = xs.build_panel(points)
    pers = xs.cross_section_periods(dates, by, "carry", 20, 63, 5, 16, "equal", 20)
    assert len(pers) >= 5
    pf = xs.perf_stats(pers, 20, 0.0003, "ls")
    bp = xs.bands_profile(pers, 5)
    assert pf["gross_mean"] > 0 and pf["net_t"] > 0, pf
    assert bp["mono"] == 1.0 and bp["spread"] > 0, bp
    pers_doi = xs.cross_section_periods(dates, by, "doi", 20, 63, 5, 16, "equal", 20)
    pf_doi = xs.perf_stats(pers_doi, 20, 0.0003, "ls")
    assert abs(pf_doi["net_t"]) < 2.0  # 纯噪声因子不应显著

    # 5) build_report 全量跑通、裁决/双样本键齐全
    main_dates = xs.truncate_dates(dates, 200)
    main_points = [p for p in points if p["date"] in set(main_dates)]
    text, sc, verdict = build_report(
        main_points, [], (dates, by), main_dates, "carry", (5, 20), 20, 63, 5, 16, 8,
        0.0003, 1.5, 0.75, 0.6, 0.5, 1.5, 5, 20, 320, len(main_dates))
    assert "carry" in text and set(verdict) >= {"ok", "reasons", "main"}
    assert sc["n_symbols"] == 20 and "conditional" in sc and "family" in sc
    assert set(next(iter(sc["conditional"].values()))["windows"]) == {"近窗", "长窗"}
    print("carry_eval selftest ALL PASS（期限映射/年月范围/收益对齐/合成carry多空单调/"
          "噪声因子不显著/报告与双样本结构 共5组）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
