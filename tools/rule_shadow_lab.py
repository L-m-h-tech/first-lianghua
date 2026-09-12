# -*- coding: utf-8 -*-
r"""Phase2 规则影子实验室 tools/rule_shadow_lab.py（第131轮，研究侧只读/零第三方依赖）。

把 Phase1 病理切片（short_trade_pathology）预登记的候选规则做**单变量影子回放**：
同一批 feeds 确定性重放（_reset_feeds），仅规则不同——
  基线 / R1 入场门槛 1.5→3.0 / R3 禁反手 / R4 最小持仓 2 根 / R1+R4；
附前半/后半样本切分稳定性对照。预登记验收标准（第130轮病理报告）：
  - R1: PF≥1.05 且 交易笔数较基线下降≥40%
  - R3: 总净利改善（引擎级代理口径；反向桶亏损收窄的近似——封锁的反手单不再存在）
  - R4: 含费净利改善
只读 monitor.db；不写生产 reports/portfolio_*（只写 reports/rule_shadow_lab.*）。

CLI: python tools/rule_shadow_lab.py [--codes RB,HC,SS] [--period 30] [--lookback 1023]
     [--r1-entry 3.0] [--r4-min-hold 2] [--selftest]
"""
import argparse
import json
import os
import statistics
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config                     # noqa: E402


# ---------------- 引擎语义自测（合成 feed，零 DB） ----------------
def selftest():
    """合成 feed 断言：R4 延迟反向离场 / R3 封锁首个反手 / 默认关=基线一致。"""
    import portfolio as pf_mod

    def flat_bars(n, day="2026-09-01", start=(9, 31)):
        from datetime import datetime, timedelta
        t0 = datetime(2026, 9, 1, start[0], start[1])
        out = []
        for i in range(n):
            t = t0 + timedelta(minutes=30 * (i + 1))
            out.append({"dt": t, "o": 100.0, "h": 100.6, "l": 99.4, "c": 100.0, "v": 10.0,
                        "amount": 1000.0})
        return out

    def make_feed(n=12, scores=None):
        import portfolio as P
        bars = flat_bars(n)
        sc = scores or ([0.0] * 3 + [3.0] * 2 + [-3.0] * (n - 5))
        atrs = [1.0] * n
        return pf_mod.SymbolFeed("RB", "螺纹钢", "黑色", bars, sc, atrs,
                                 owners=["2026-09-01"] * n, bases=None, fee_row=None,
                                 limit_move=0.04)

    margin_table = {"RB": {"broker_margin": 0.07, "limit_basic": 0.04, "multiplier": 10.0}}
    fee_table = {}

    def run(n=12, scores=None, min_hold=0, no_reverse=False, entry=2.0):
        f = make_feed(n, scores)
        feeds = {"RB": f}
        pf = pf_mod.Portfolio(1_000_000.0, margin_table, fee_table, sizing="equal_notional",
                              per_symbol=0.30, stop_atr=1.0, default_margin=0.08,
                              fee_rate=1e-05, slip_rate=1e-05, use_real_fees=False,
                              sector_of={"RB": "黑色"}, max_symbol_weight=1.0,
                              max_sector_weight=1.0, max_concurrent=8)
        pf_mod.run_portfolio(feeds, pf, entry_th=entry, stop_atr=1.0, target_atr=2.0,
                             flat_eod=False, max_bars=999, use_limit=False, limit_eps=0.0,
                             minute_mode=True, min_hold=min_hold, no_reverse=no_reverse)
        return pf

    # 基线：多头开仓 → 反向信号平仓 → 立即反手做空
    base = run()
    assert any(t["dir"] == "空" for t in base.closed), "基线应存在反手做空回合"
    # R3：封锁首个反手（分数持续 -3 不回中性 → 不再入场）
    r3 = run(no_reverse=True)
    assert all(t["dir"] != "空" for t in r3.closed), "R3 应封锁反手做空回合"
    # R3 解除：分数回中性一次后允许同方向入场
    sc = [0.0] * 3 + [3.0] * 2 + [-3.0] * 2 + [0.0] * 2 + [-3.0] * 3
    r3b = run(n=12, scores=sc, no_reverse=True)
    assert any(t["dir"] == "空" for t in r3b.closed), "R3 中性区出现后应解除封锁"
    # R4：反向离场延迟（held≥2 才挂反向退出）
    base_exits = [t["exit_dt"] for t in base.closed if (t.get("reason") or "") == "反向信号"]
    r4 = run(min_hold=2)
    r4_exits = [t["exit_dt"] for t in r4.closed if (t.get("reason") or "") == "反向信号"]
    assert r4_exits and base_exits and r4_exits[0] > base_exits[0], (base_exits, r4_exits)
    # 默认关：min_hold=0/no_reverse=False 与基线一致
    d = run()
    assert [(t["dir"], t["exit_dt"]) for t in d.closed] == \
           [(t["dir"], t["exit_dt"]) for t in base.closed]
    print("rule_shadow_lab selftest OK")
    return 0


# ---------------- 真实影子回放 ----------------
def _fmt_pf(v):
    if v is None:
        return "—"
    return "∞" if v >= 999 else "%.2f" % v


def _stats(pf):
    closed = pf.closed
    n = len(closed)
    net = sum(t["net_yuan"] for t in closed)
    wins = sum(1 for t in closed if t["net_yuan"] > 0)
    gp = sum(t["net_yuan"] for t in closed if t["net_yuan"] > 0)
    gl = abs(sum(t["net_yuan"] for t in closed if t["net_yuan"] < 0))
    short_n = sum(1 for t in closed if (t.get("hold_bars") or 0) <= 2)
    stop_n = sum(1 for t in closed if (t.get("reason") or "").startswith("止损"))
    rev = [t for t in closed if "反向" in (t.get("reason") or "")]
    rev_net = sum(t["net_yuan"] for t in rev)
    fees = sum((t.get("open_fee_yuan") or 0.0) + (t.get("close_fee_yuan") or 0.0)
               for t in closed)
    exits = sorted(t["exit_dt"] for t in closed)
    return {"n": n, "net": round(net, 0), "win": round(wins / n, 3) if n else None,
            "pf": round(gp / gl, 2) if gl > 0 else (None if not gp else 999.0),
            "short_n": short_n, "stop_n": stop_n, "rev_n": len(rev), "rev_net": round(rev_net, 0),
            "fees": round(fees, 0),
            "median_exit": exits[n // 2].strftime("%Y-%m-%d %H:%M:%S") if n else None,
            "closed": closed}


def run_lab(codes=None, period=30, lookback=None, r1_entry=3.0, r4_min_hold=2, workers=6):
    import portfolio as pf_mod
    from concurrent.futures import ThreadPoolExecutor, as_completed
    lookback = lookback or config.INTRADAY_BT_LOOKBACK

    # 品种：缺省取当前 portfolio_trades.csv 的品种（与 Phase1 病理报告同宇宙）
    if not codes:
        csvp = os.path.join(_ROOT, "reports", "portfolio_trades.csv")
        syms = set()
        try:
            import csv as _csv
            with open(csvp, "r", encoding="utf-8-sig") as f:
                for r in _csv.DictReader(f):
                    if r.get("sym"):
                        syms.add(r["sym"].strip())
        except OSError:
            pass
        codes = ",".join(sorted(syms))
    if not codes:
        raise SystemExit("未指定 --codes 且找不到历史 portfolio_trades.csv 品种")

    args = pf_mod.parse_args(["--codes", codes, "--period", str(period),
                              "--lookback", str(lookback)])
    fee_table = pf_mod.load_fee_schedule(args.fees_file) if args.use_real_fees else {}
    margin_table = pf_mod.load_margin_schedule(args.margins_file)
    sector_of = {m["sym"]: m["cat"] for m in config.VARIETIES.values()}
    name_of = {m["sym"]: name for name, m in config.VARIETIES.items()}

    import intraday_backtest as ib
    items = ib.resolve_items(args.codes, args.limit)
    feeds, errors = {}, []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(pf_mod.load_minute_feed, it, args, fee_table, margin_table): it[0]
                for it in items}
        for fut in as_completed(futs):
            sym, feed, err = fut.result()
            if err:
                errors.append((sym, err))
            else:
                feeds[sym] = feed
    if not feeds:
        raise SystemExit("无可用品种 feeds")

    def _run(entry, min_hold, no_reverse):
        pf_mod._reset_feeds(feeds)
        pf = pf_mod.Portfolio(args.equity, margin_table, fee_table, sizing=args.sizing,
                              per_symbol=args.per_symbol, stop_atr=args.stop_atr,
                              score_weights=config.PORTFOLIO_SCORE_WEIGHTS,
                              max_symbol_weight=args.max_symbol,
                              max_sector_weight=args.max_sector,
                              risk_liquidate=args.risk_liquidate, risk_safe=args.risk_safe,
                              default_margin=config.PORTFOLIO_DEFAULT_MARGIN,
                              max_concurrent=args.max_concurrent, fee_rate=args.fee_rate,
                              slip_rate=args.slip_rate, use_real_fees=args.use_real_fees,
                              sector_of=sector_of)
        pf_mod.run_portfolio(feeds, pf, entry_th=entry, stop_atr=args.stop_atr,
                             target_atr=args.target_atr, flat_eod=args.flat_eod,
                             max_bars=args.max_bars, use_limit=args.use_limit,
                             limit_eps=config.INTRADAY_BT_LIMIT_TICK_EPS,
                             minute_mode=True, hold_days=args.hold, risk_cfg=None,
                             min_hold=min_hold, no_reverse=no_reverse)
        st = _stats(pf)
        st["perf"] = {"total_ret": round(pf.performance()["total_ret"], 4),
                      "max_dd": round(pf.performance()["max_dd"], 4),
                      "sharpe": round(pf.performance()["sharpe"], 2)}
        return st

    variants = [
        ("基线", args.entry, 0, False),
        ("R1 门槛%.1f" % r1_entry, r1_entry, 0, False),
        ("R3 禁反手", args.entry, 0, True),
        ("R4 最小持仓%d" % r4_min_hold, args.entry, r4_min_hold, False),
        ("R1+R4", r1_entry, r4_min_hold, False),
    ]
    results = []
    for label, entry, mh, nr in variants:
        st = _run(entry, mh, nr)
        st.pop("closed")
        results.append({"label": label, "entry_th": entry, "min_hold": mh,
                        "no_reverse": nr, **st})
    base = results[0]

    # 双半样本：按基线中位出场时间切，各变体分前/后半计算净利与PF（ trades 从 closed 重建）
    halves = []
    for label, entry, mh, nr in variants:
        pf_mod._reset_feeds(feeds)
        pf = pf_mod.Portfolio(args.equity, margin_table, fee_table, sizing=args.sizing,
                              per_symbol=args.per_symbol, stop_atr=args.stop_atr,
                              score_weights=config.PORTFOLIO_SCORE_WEIGHTS,
                              max_symbol_weight=args.max_symbol,
                              max_sector_weight=args.max_sector,
                              risk_liquidate=args.risk_liquidate, risk_safe=args.risk_safe,
                              default_margin=config.PORTFOLIO_DEFAULT_MARGIN,
                              max_concurrent=args.max_concurrent, fee_rate=args.fee_rate,
                              slip_rate=args.slip_rate, use_real_fees=args.use_real_fees,
                              sector_of=sector_of)
        pf_mod.run_portfolio(feeds, pf, entry_th=entry, stop_atr=args.stop_atr,
                             target_atr=args.target_atr, flat_eod=args.flat_eod,
                             max_bars=args.max_bars, use_limit=args.use_limit,
                             limit_eps=config.INTRADAY_BT_LIMIT_TICK_EPS,
                             minute_mode=True, hold_days=args.hold, risk_cfg=None,
                             min_hold=mh, no_reverse=nr)
        closed = pf.closed
        if not closed:
            halves.append({"label": label, "h1_net": 0.0, "h2_net": 0.0})
            continue
        med = sorted(t["exit_dt"] for t in closed)[len(closed) // 2]
        h1 = [t["net_yuan"] for t in closed if t["exit_dt"] <= med]
        h2 = [t["net_yuan"] for t in closed if t["exit_dt"] > med]
        halves.append({"label": label,
                       "h1_net": round(sum(h1), 0), "h2_net": round(sum(h2), 0)})

    # 预登记验收判定
    verdicts = {}
    b_stats = base

    def _pf_of(r):
        return r["pf"] if r["pf"] is not None and r["pf"] < 999 else (10.0 if r["pf"] == 999.0 else 0.0)

    r1 = next((r for r in results if r["label"].startswith("R1 ")), None)
    if r1:
        drop = 1 - r1["n"] / base["n"] if base["n"] else 0.0
        verdicts["R1"] = ("通过" if (_pf_of(r1) >= 1.05 and drop >= 0.40)
                          else "未通过（PF=%.2f, 笔数降%.0f%%）" % (_pf_of(r1), drop * 100))
    r4 = next((r for r in results if r["label"].startswith("R4 ")), None)
    if r4:
        verdicts["R4"] = "通过" if r4["net"] > base["net"] else \
            "未通过（净 %+0.f vs 基线 %+0.f）" % (r4["net"], base["net"])
    r3 = next((r for r in results if r["label"].startswith("R3")), None)
    if r3:
        verdicts["R3"] = "通过" if r3["net"] > base["net"] else \
            "未通过（净 %+0.f vs 基线 %+0.f，代理口径）" % (r3["net"], base["net"])

    return {"generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "codes": codes, "period": period, "lookback": lookback,
            "entry_baseline": args.entry, "r1_entry": r1_entry, "r4_min_hold": r4_min_hold,
            "n_syms": len(feeds), "errors": [e[0] for e in errors],
            "results": results, "halves": halves, "verdicts": verdicts}


def render(res):
    L = ["Phase2 规则影子实验室（第131轮——单变量影子回放，同批 feeds 确定性重放）", "=" * 66,
         "生成: %s ｜ 品种(%d): %s ｜ %dm × %d 根 ｜ 基线门槛 %.1f ｜ R1=%.1f ｜ R4=%d根" % (
             res["generated"], res["n_syms"], res["codes"], res["period"], res["lookback"],
             res["entry_baseline"], res["r1_entry"], res["r4_min_hold"]), ""]
    L.append("%-14s %5s %10s %6s %7s %6s %6s %9s %7s %7s" % (
        "配置", "笔数", "净盈亏", "胜率", "PF", "1-2bar", "止损", "最大回撤", "夏普", "费用"))
    for r in res["results"]:
        L.append("%-14s %5d %10s %6s %7s %6d %6d %9s %7s %7.0f" % (
            r["label"], r["n"], format(r["net"], "+,.0f"),
            ("%.0f%%" % (r["win"] * 100)) if r["win"] is not None else "—",
            _fmt_pf(r["pf"]), r["short_n"], r["stop_n"],
            ("%.1f%%" % (r["perf"]["max_dd"] * 100)) if r["perf"] else "—",
            str(r["perf"]["sharpe"]) if r["perf"] else "—", r["fees"]))
    L.append("")
    L.append("前半/后半样本净利（稳定性对照）：")
    for h in res["halves"]:
        L.append("   %-14s 前半 %+10.0f ｜ 后半 %+10.0f" % (h["label"], h["h1_net"], h["h2_net"]))
    L.append("")
    L.append("预登记验收判定：")
    for k, v in res["verdicts"].items():
        L.append("   %s: %s" % (k, v))
    L.append("")
    L.append("（影子回放：不改生产参数/CSV；通过项进入 10 月 G1 对账与 Phase3 拍板，默认仍关闭）")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Phase2 规则影子实验室（单变量影子回放）")
    ap.add_argument("--codes", default="", help="品种，缺省取当前 portfolio_trades.csv 品种")
    ap.add_argument("--period", type=int, default=30)
    ap.add_argument("--lookback", type=int, default=None)
    ap.add_argument("--r1-entry", type=float, default=3.0, dest="r1_entry")
    ap.add_argument("--r4-min-hold", type=int, default=2, dest="r4_min_hold")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    res = run_lab(codes=args.codes or None, period=args.period, lookback=args.lookback,
                  r1_entry=args.r1_entry, r4_min_hold=args.r4_min_hold, workers=args.workers)
    txt = render(res)
    os.makedirs(os.path.join(_ROOT, "reports"), exist_ok=True)
    with open(os.path.join(_ROOT, "reports", "rule_shadow_lab.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    with open(os.path.join(_ROOT, "reports", "rule_shadow_lab.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(txt)
    print("\n已写出: reports/rule_shadow_lab.txt / .json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main() if len(sys.argv) > 1 else selftest())
