# -*- coding: utf-8 -*-
r"""1-2bar 短单病理切片 tools/short_trade_pathology.py（第130轮 Phase1，研究侧只读/零第三方依赖）。

回答 Phase1 唯一问题：trade_journal 的 1-2bar 桶（1289 笔、PF=0.57、净 -19.7 万）到底死于什么——
止损打穿？分数震荡 churn？反向信号？平今税？为 R1~R4 候选规则提供机制证据并预登记验收标准。

切片（全部只读 portfolio_trades.csv + 分钟库盘中 h/l，零网络）：
  ①1-2bar 桶离场原因构成（止损/止盈/反向/日终强平 × 笔数/净/均MAE/MFE）
  ②止损单"止损距离 vs 出场后反向回摆"——R2 核心证据：止损单出场后 4/8 根内，
    价格朝原方向回摆 ≥ 止损距离的比例（whipsaw 率，高=止损在噪声内）
  ③R1 证据：1-2bar 桶按入场信号强度分桶（|分|<3 的净亏损 = 提门槛的直接依据）
  ④R3 证据：1-2bar ∩ 反向信号 的笔数与净亏
  ⑤R4 证据：平今 vs 平昨 腿的费用与净亏结构（短单费用/毛利比 对比 3-6bar 桶）
  ⑥品种集中度 Top8
输出 reports/short_trade_pathology.txt / .json（看板"研究报告(全部)"自动聚合）。
纪律：只读不改参；候选规则的验收标准随报告**预登记**（先定标准后跑影子，防事后挑数）。

CLI: python tools/short_trade_pathology.py [--trades ...] [--period 30] [--lookback 8000]
     [--post-bars 8] [--no-bars] [--selftest]
"""
import argparse
import json
import os
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config                     # noqa: E402
import trade_journal as tj        # noqa: E402  复用加载器/回放/分桶

SHORT_MAX_BARS = 2                # 1-2bar 桶口径（与 hold_band 一致，含同根 0）


# ---------------- 出场后回摆（R2 机制证据） ----------------
def post_exit_move(direction, exit_px, bars, t_exit, n_bars, period_min=30):
    """止损出场后 n_bars 根内：(朝原方向有利回摆, 继续不利) 的最大幅度（相对出场价，正小数）。
    用与 range_excursion 相同的盘中 h/l 口径；找不到 bar 返回 (None, None)。"""
    if not bars or not exit_px or t_exit is None or direction == 0:
        return None, None
    from datetime import timedelta
    t_end = t_exit + timedelta(minutes=period_min * n_bars)
    fav = adv = None
    used = 0
    for b in bars:
        dt = b.get("dt")
        if dt is None or dt <= t_exit:
            continue
        if dt > t_end:
            break
        h, l = tj._f(b.get("h"), None), tj._f(b.get("l"), None)
        if not h or not l:
            continue
        if direction > 0:
            fav = max(fav or 0.0, (h - exit_px) / exit_px)
            adv = max(adv or 0.0, (exit_px - l) / exit_px)
        else:
            fav = max(fav or 0.0, (exit_px - l) / exit_px)
            adv = max(adv or 0.0, (h - exit_px) / exit_px)
        used += 1
    return (fav if used else None), (adv if used else None)


# ---------------- 病理切片 ----------------
def build_pathology(trades, bars_by_sym=None, post_bars=8, period_min=30):
    """trades（trade_journal.load_trades 输出，可含 mfe_bar/mae_bar）→ 病理 dict。纯函数。"""
    bars_by_sym = bars_by_sym or {}
    short = [t for t in trades if (t.get("hold_bars") or 0) <= SHORT_MAX_BARS]
    mid = [t for t in trades if 3 <= (t.get("hold_bars") or 0) <= 6]

    def agg(ts_):
        n = len(ts_)
        net = sum(t["net_yuan"] for t in ts_)
        wins = sum(1 for t in ts_ if t["net_yuan"] > 0)
        gross = sum(t["gross_yuan"] for t in ts_)
        fee = sum(t["fee_yuan"] for t in ts_)
        gp = sum(t["net_yuan"] for t in ts_ if t["net_yuan"] > 0)
        gl = abs(sum(t["net_yuan"] for t in ts_ if t["net_yuan"] < 0))
        return {"n": n, "net": round(net, 0), "win_rate": round(wins / n, 3) if n else None,
                "fee": round(fee, 0), "gross": round(gross, 0),
                "pf": round(gp / gl, 2) if gl > 0 else (None if not gp else 999.0)}

    # ① 离场原因构成
    by_reason = defaultdict(list)
    for t in short:
        by_reason[tj.reason_group(t["reason"])].append(t)
    reason_rows = {k: agg(v) for k, v in sorted(by_reason.items(), key=lambda kv: -len(kv[1]))}

    # ② 止损单：止损距离 + 出场后回摆（R2）
    stops = [t for t in short if tj.reason_group(t["reason"]) == "止损"]
    whip4 = whip8 = 0
    stop_dists, post_favs, post_advs = [], [], []
    n_post = 0
    for t in stops:
        d = t["direction"]
        if not d or not t["exit_px"]:
            continue
        dist = abs(t["exit_px"] - t["entry_px"]) / t["entry_px"]
        stop_dists.append(dist)
        bars = bars_by_sym.get(t["sym"]) or []
        fav4, _ = post_exit_move(d, t["exit_px"], bars, t["exit_dt"], 4, period_min)
        fav8, adv8 = post_exit_move(d, t["exit_px"], bars, t["exit_dt"], post_bars, period_min)
        if fav4 is None:
            continue
        n_post += 1
        post_favs.append(fav4)
        if fav8 is not None:
            post_advs.append(adv8 or 0.0)
        if dist > 0 and fav4 is not None and fav4 >= dist:
            whip4 += 1
        if dist > 0 and fav8 is not None and fav8 >= dist:
            whip8 += 1
    stop_dist_med = sorted(stop_dists)[len(stop_dists) // 2] if stop_dists else None
    fav_med = sorted(post_favs)[len(post_favs) // 2] if post_favs else None

    # ③ R1：信号强度（反事实精确口径：|分|<3 = "门槛 2→3" 会排除的全部交易）
    by_score = defaultdict(list)
    for t in short:
        by_score[tj.score_band(t.get("entry_score"))].append(t)
    score_rows = {k: agg(v) for k, v in by_score.items()}
    r1_trades = [t for t in short
                 if t.get("entry_score") is not None and abs(t["entry_score"]) < 3]
    r1_n = len(r1_trades)
    r1_net = sum(t["net_yuan"] for t in r1_trades)

    # ④ R3：反向信号
    rev = agg(by_reason.get("反向信号", []))

    # ⑤ R4：平今税
    legs = defaultdict(list)
    for t in short:
        legs["平今" if "平今" in (t["leg"] or "") else ("平昨" if "平昨" in (t["leg"] or "") else "其他")].append(t)
    leg_rows = {k: agg(v) for k, v in legs.items()}
    fee_ratio_short = (sum(t["fee_yuan"] for t in short) / abs(sum(t["gross_yuan"] for t in short))
                       if short and abs(sum(t["gross_yuan"] for t in short)) > 0 else None)
    fee_ratio_mid = (sum(t["fee_yuan"] for t in mid) / abs(sum(t["gross_yuan"] for t in mid))
                     if mid and abs(sum(t["gross_yuan"] for t in mid)) > 0 else None)

    # ⑥ 品种集中度
    by_sym = defaultdict(list)
    for t in short:
        by_sym[t["sym"] or "?"].append(t)
    sym_rows = sorted(((s, agg(v)) for s, v in by_sym.items()), key=lambda kv: kv[1]["net"])[:8]

    return {
        "short_overall": agg(short), "mid_overall": agg(mid),
        "reason_rows": reason_rows,
        "r2_stop": {"n_stops": len(stops), "n_with_bars": n_post,
                    "stop_dist_median": round(stop_dist_med, 5) if stop_dist_med else None,
                    "post_fav4_median": round(fav_med, 5) if fav_med else None,
                    "whipsaw_rate_4bar": round(whip4 / n_post, 3) if n_post else None,
                    "whipsaw_rate_%dbar" % post_bars: round(whip8 / n_post, 3) if n_post else None},
        "r1_score_rows": score_rows, "r1_weak_n": r1_n, "r1_weak_net": round(r1_net, 0),
        "r3_reverse": rev,
        "r4_leg_rows": leg_rows,
        "fee_ratio_short": round(fee_ratio_short, 3) if fee_ratio_short is not None else None,
        "fee_ratio_mid": round(fee_ratio_mid, 3) if fee_ratio_mid is not None else None,
        "sym_top_loss": [{"sym": s, **a} for s, a in sym_rows],
    }


def render(res):
    so, mo = res["short_overall"], res["mid_overall"]
    L = ["1-2bar 短单病理切片（第130轮 Phase1——机制证据 + R1~R4 预登记）", "=" * 64]
    L.append("1-2bar 桶: %d 笔 / 净 %+.0f / 胜率 %.0f%% / PF %s ｜ 对照 3-6bar 桶: %d 笔 / 净 %+.0f / PF %s" % (
        so["n"], so["net"], (so["win_rate"] or 0) * 100,
        ("%.2f" % so["pf"]) if so["pf"] and so["pf"] < 999 else ("∞" if so["pf"] == 999.0 else "—"),
        mo["n"], mo["net"], ("%.2f" % mo["pf"]) if mo["pf"] else "—"))
    L.append("")
    L.append("① 离场原因构成（1-2bar 桶内）：")
    for k, a in res["reason_rows"].items():
        L.append("   %-8s %5d 笔  净 %+10.0f  胜率 %5.1f%%" % (
            k, a["n"], a["net"], (a["win_rate"] or 0) * 100))
    r2 = res["r2_stop"]
    L.append("")
    L.append("② 止损单病理（R2 核心证据，n=%d，其中 %d 笔有分钟库覆盖）：" % (r2["n_stops"], r2["n_with_bars"]))
    L.append("   止损距离中位数 %.2f%% ｜ 出场后 4 根内有利回摆中位数 %.2f%%" % (
        (r2["stop_dist_median"] or 0) * 100, (r2["post_fav4_median"] or 0) * 100))
    L.append("   whipsaw 率（回摆≥止损距离）: 4根 %.0f%% ｜ 8根 %s" % (
        (r2["whipsaw_rate_4bar"] or 0) * 100,
        ("%.0f%%" % (r2["whipsaw_rate_8bar"] * 100)) if r2.get("whipsaw_rate_8bar") is not None else "—"))
    L.append("   → whipsaw 率高 = 止损落在噪声内（R2 放宽有据）；低 = 止损在正确砍亏损（R2 无据）")
    L.append("")
    L.append("③ R1 预登记（入场门槛 2→3）：1-2bar 桶内 |分|<3 的笔数 %d / 净 %+0.f" % (
        res["r1_weak_n"], res["r1_weak_net"]))
    for k, a in res["r1_score_rows"].items():
        L.append("   %-14s %5d 笔  净 %+10.0f  PF %s" % (
            k, a["n"], a["net"], ("%.2f" % a["pf"]) if a["pf"] and a["pf"] < 999 else ("∞" if a["pf"] == 999.0 else "—")))
    L.append("   验收标准（预登记）：双样本 PF≥1.05 且 笔数下降≥40%")
    L.append("")
    L.append("④ R3 预登记（反向信号只平不反手）：%d 笔 / 净 %+0.f" % (
        res["r3_reverse"]["n"], res["r3_reverse"]["net"]))
    L.append("   验收标准（预登记）：反向桶亏损收窄≥50%，其余桶不变差")
    L.append("")
    L.append("⑤ R4 预登记（平今税）：1-2bar 桶 费用/|毛利| = %s vs 3-6bar 桶 %s" % (
        ("%.1f%%" % (res["fee_ratio_short"] * 100)) if res["fee_ratio_short"] is not None else "—",
        ("%.1f%%" % (res["fee_ratio_mid"] * 100)) if res["fee_ratio_mid"] is not None else "—"))
    for k, a in res["r4_leg_rows"].items():
        L.append("   %-4s %5d 笔  净 %+10.0f  费用 %8.0f" % (k, a["n"], a["net"], a["fee"]))
    L.append("   验收标准（预登记）：持仓≥2根规则下含费净利改善（对照当前口径）")
    L.append("")
    L.append("⑥ 品种集中度（1-2bar 净亏 Top8）：")
    for row in res["sym_top_loss"]:
        L.append("   %-6s %5d 笔  净 %+10.0f" % (row["sym"], row["n"], row["net"]))
    L.append("")
    L.append("（只读切片：候选规则进 Phase2 双样本影子后才允许动 config；本报告即预登记文档，先定标准防事后挑数）")
    return "\n".join(L)


def selftest():
    """零网络合成断言：桶口径/原因构成/whipsaw 分类/门槛反事实/平今腿。"""
    trades = [
        # 1-2bar 止损多单：入场 100 出 99（-1%），出场后 4 根回摆 +1.5%（whipsaw）
        {"sym": "RB", "name": "螺纹钢", "sector": "黑色", "dir": "多", "direction": 1, "lots": 1,
         "entry_px": 100.0, "exit_px": 99.0, "entry_dt": __import__("datetime").datetime(2026, 9, 1, 9, 31),
         "exit_dt": __import__("datetime").datetime(2026, 9, 1, 10, 1), "leg": "平今",
         "hold_bars": 2, "gross_yuan": -100.0, "open_fee_yuan": 3, "close_fee_yuan": 3,
         "fee_yuan": 6.0, "net_yuan": -106.0, "reason": "止损", "forced": False, "entry_score": 1.5,
         "mfe_bar": 0.002, "mae_bar": 0.011},
        # 1-2bar 止损空单：无回摆（继续下行 2%）——正确止损
        {"sym": "MA", "name": "甲醇", "sector": "能源化工", "dir": "空", "direction": -1, "lots": 1,
         "entry_px": 2500.0, "exit_px": 2525.0, "entry_dt": __import__("datetime").datetime(2026, 9, 2, 9, 31),
         "exit_dt": __import__("datetime").datetime(2026, 9, 2, 10, 1), "leg": "平今",
         "hold_bars": 2, "gross_yuan": -250.0, "open_fee_yuan": 2.5, "close_fee_yuan": 2.5,
         "fee_yuan": 5.0, "net_yuan": -255.0, "reason": "止损", "forced": False, "entry_score": 4.5,
         "mfe_bar": 0.001, "mae_bar": 0.012},
        # 1-2bar 反向信号
        {"sym": "CU", "name": "铜", "sector": "有色", "dir": "多", "direction": 1, "lots": 1,
         "entry_px": 70000.0, "exit_px": 70050.0, "entry_dt": __import__("datetime").datetime(2026, 9, 3, 9, 31),
         "exit_dt": __import__("datetime").datetime(2026, 9, 3, 10, 1), "leg": "平今",
         "hold_bars": 1, "gross_yuan": 50.0, "open_fee_yuan": 12, "close_fee_yuan": 12,
         "fee_yuan": 24.0, "net_yuan": -174.0, "reason": "反向信号平仓", "forced": False, "entry_score": 3.2,
         "mfe_bar": 0.001, "mae_bar": 0.001},
        # 3-6bar 对照（止盈）
        {"sym": "RB", "name": "螺纹钢", "sector": "黑色", "dir": "多", "direction": 1, "lots": 1,
         "entry_px": 100.0, "exit_px": 105.0, "entry_dt": __import__("datetime").datetime(2026, 9, 5, 9, 31),
         "exit_dt": __import__("datetime").datetime(2026, 9, 7, 14, 31), "leg": "平昨",
         "hold_bars": 5, "gross_yuan": 500.0, "open_fee_yuan": 3, "close_fee_yuan": 3,
         "fee_yuan": 6.0, "net_yuan": 494.0, "reason": "止盈", "forced": False, "entry_score": 4.0,
         "mfe_bar": 0.06, "mae_bar": 0.005},
    ]
    bars = {"RB": [], "MA": []}
    from datetime import datetime, timedelta
    t0 = datetime(2026, 9, 1, 10, 2)
    for i in range(4):   # RB 止损后 4 根：h=101.5（回摆 +1.5%）→ whipsaw
        t = t0 + timedelta(minutes=30 * i)
        bars["RB"].append({"dt": t, "o": 99.0, "h": 101.5, "l": 98.5, "c": 100.0, "v": 1, "amount": 1})
    t0 = datetime(2026, 9, 2, 10, 2)
    for i in range(4):   # MA 止损后继续下行（l 更低）→ 无回摆
        t = t0 + timedelta(minutes=30 * i)
        bars["MA"].append({"dt": t, "o": 2525.0, "h": 2526.0, "l": 2510.0, "c": 2512.0, "v": 1, "amount": 1})
    res = build_pathology(trades, bars_by_sym=bars, post_bars=4, period_min=30)
    assert res["short_overall"]["n"] == 3 and res["short_overall"]["net"] == -535.0
    assert res["reason_rows"]["止损"]["n"] == 2
    assert res["r2_stop"]["n_stops"] == 2 and res["r2_stop"]["n_with_bars"] == 2
    assert res["r2_stop"]["whipsaw_rate_4bar"] == 0.5, res["r2_stop"]      # 一半 whipsaw
    assert res["r1_weak_n"] == 1 and abs(res["r1_weak_net"] + 106.0) < 1   # |分|<3 仅 RB 一笔（CU 3.2 不在口径内）
    assert res["r3_reverse"]["n"] == 1
    assert "平今" in res["r4_leg_rows"] and res["r4_leg_rows"]["平今"]["n"] == 3
    txt = render(res)
    assert "预登记" in txt and "whipsaw" in txt
    print("short_trade_pathology selftest OK")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="1-2bar 短单病理切片（Phase1，研究侧只读）")
    ap.add_argument("--trades", default=tj.DEFAULT_TRADES)
    ap.add_argument("--period", type=int, default=30)
    ap.add_argument("--lookback", type=int, default=8000)
    ap.add_argument("--post-bars", type=int, default=8, dest="post_bars")
    ap.add_argument("--no-bars", action="store_true", dest="no_bars", help="跳过分钟库回放（无 whipsaw 切片）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    trades = tj.load_trades(args.trades)
    if not trades:
        print("portfolio_trades.csv 缺失或为空，先跑 portfolio 回测")
        return 1
    bars_by_sym = {}
    if not args.no_bars:
        syms = sorted({t["sym"] for t in trades})
        for s in syms:
            bars_by_sym[s] = tj.load_minute_bars_for(s, args.period, args.lookback, 0)
    res = build_pathology(trades, bars_by_sym=bars_by_sym, post_bars=args.post_bars,
                          period_min=args.period)
    txt = render(res)
    os.makedirs(os.path.join(_ROOT, "reports"), exist_ok=True)
    with open(os.path.join(_ROOT, "reports", "short_trade_pathology.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    with open(os.path.join(_ROOT, "reports", "short_trade_pathology.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(txt)
    print("\n已写出: reports/short_trade_pathology.txt / .json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main() if len(sys.argv) > 1 else selftest())
