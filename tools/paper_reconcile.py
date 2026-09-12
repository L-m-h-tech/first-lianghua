# -*- coding: utf-8 -*-
r"""纸面账户三方对账 tools/paper_reconcile.py（G1 验收预备，第128轮，研究侧只读/零网络/零第三方依赖）。

对账三方（回答 G1 唯一验收问题："含真实成本后综合分策略是否仍为正"）：
  ①账本自洽（每账户内部守恒）：paper_equity 逐行推算 equity0 = static_equity - realized，
    全表离散度≈0 即账本守恒；pos_ref 配对完整性（有开无平=在途）、强平笔数、费用/滑点合计。
  ②信号对照（纸面 × monitor.db signal_outcomes）：同品种同方向、纸面入场时刻 ±窗口内的
    已评估结果——方向一致率、信号平均方向收益 vs 纸面平均净收益（口径差异诚实标注：
    信号=纯方向价格收益不含成本；纸面=含费含滑点、持仓期与信号回看期不同，只比方向与相对量级）。
  ③回测参照（可选降级）：reports/portfolio_equity.csv / backtest_report.txt 存在时给出对照行，
    缺失则如实标注"未生成"。

输出 reports/paper_reconcile.txt / .json（看板"研究报告(全部)"自动聚合）。
纪律：只读所有库、不写任何生产表、样本<20 笔标注"样本不足"、缺数据不编造。

CLI: python tools/paper_reconcile.py [--accounts-dir ...] [--monitor-db ...] [--reports-dir ...]
     [--match-window-min 30] [--selftest]
"""
import argparse
import glob
import json
import os
import sqlite3
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config                     # noqa: E402  （VARIETIES: 中文名→sym）

# 中文名→sym；兜底 code 主连去尾 0（SH0→SH）
_VAR_TO_SYM = {vn: str(vc.get("sym", "")).upper() for vn, vc in config.VARIETIES.items()}


def _sym_of(variety, code):
    if variety in _VAR_TO_SYM:
        return _VAR_TO_SYM[variety]
    code = str(code or "").upper()
    return code[:-1] if code.endswith("0") and len(code) > 1 else code


def _ts(s):
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


# =========================== ① 账本自洽 ===========================
def reconcile_account(db_path, name=None):
    """单个纸面账户库对账：返回守恒/配对/汇总指标 dict（库不存在返 None）。"""
    if not os.path.isfile(db_path):
        return None
    name = name or os.path.splitext(os.path.basename(db_path))[0]
    conn = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        trades = [dict(r) for r in conn.execute(
            "SELECT * FROM paper_trades ORDER BY ts, id")]
        equity = [dict(r) for r in conn.execute(
            "SELECT * FROM paper_equity ORDER BY ts, id")]
        orders_pending = conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE status IN ('pending','queued')").fetchone()[0]
    finally:
        conn.close()

    # 配对：pos_ref → 腿
    trips = {}
    for t in trades:
        trips.setdefault(t["pos_ref"] or "?", []).append(t)
    closed, open_pos = [], []
    for ref, legs in trips.items():
        has_close = any((t.get("side") or "").startswith("close") for t in legs)
        if has_close:
            closed.append(legs)
        else:
            open_pos.append(legs)
    wins = [sum(l["realized_yuan"] or 0 for l in legs) for legs in closed]
    net_list = wins
    gross_win = sum(p for p in net_list if p > 0)
    gross_loss = abs(sum(p for p in net_list if p < 0))
    fees_total = sum(t["fee_yuan"] or 0 for t in trades)
    slip_total = sum(t["slip_yuan"] or 0 for t in trades)
    forced_n = sum(1 for t in trades if t.get("forced"))
    hold_min = []
    for legs in closed:
        e = _ts(legs[0].get("entry_ts") or legs[0].get("ts"))
        x = max((_ts(t["ts"]) for t in legs if _ts(t["ts"])), default=None)
        if e and x:
            hold_min.append((x - e).total_seconds() / 60.0)

    # 资金守恒：equity0 = static_equity - realized 应逐行恒定（无权益行 → None=无法评估，不算违规）
    implied = [r["static_equity"] - (r["realized"] or 0) for r in equity
               if r.get("static_equity") is not None]
    e0_spread = (max(implied) - min(implied)) if implied else None
    identity_ok = (e0_spread < 0.01) if e0_spread is not None else None
    last = equity[-1] if equity else {}

    return {
        "name": name, "db": db_path,
        "n_trades": len(trades), "n_closed": len(closed), "n_open": len(open_pos),
        "forced_n": forced_n, "orders_pending": orders_pending,
        "net_total": round(sum(net_list), 2),
        "win_rate": round(sum(1 for p in net_list if p > 0) / len(net_list), 4) if net_list else None,
        "pf": round(gross_win / gross_loss, 3) if gross_loss > 0 and gross_win > 0
              else (None if not net_list else 999.0 if gross_loss == 0 else None),
        "avg_net": round(sum(net_list) / len(net_list), 2) if net_list else None,
        "avg_hold_min": round(sum(hold_min) / len(hold_min), 1) if hold_min else None,
        "fees_total": round(fees_total, 2), "slip_total": round(slip_total, 2),
        "identity_ok": identity_ok, "equity0_implied": round(implied[-1], 2) if implied else None,
        "equity0_spread": round(e0_spread, 4) if e0_spread is not None else None,
        "equity_last": round(last["equity"], 2) if last.get("equity") is not None else None,
        "drawdown_last": last.get("drawdown"),
        "closed_details": [
            {"pos_ref": legs[0]["pos_ref"], "sym": legs[0]["sym"],
             "direction": legs[0]["direction"], "entry_ts": legs[0].get("entry_ts"),
             "entry_notional": next((l["notional"] for l in legs if l["side"] == "open"), 0.0),
             "net_pnl": round(sum(l["realized_yuan"] or 0 for l in legs), 2),
             "fee": round(sum(l["fee_yuan"] or 0 for l in legs), 2),
             "slip": round(sum(l["slip_yuan"] or 0 for l in legs), 2)}
            for legs in closed],
    }


# =========================== ② 信号对照 ===========================
def match_signals(closed_details, outcome_rows, window_min=30):
    """已平仓回合 × signal_outcomes（已评估）匹配：同品种同方向、entry_ts ±window_min。
    返回 (matched, agg)：matched=[(trip, outcome)]，agg 含方向一致率/双方均值。"""
    evaluated = []
    for o in outcome_rows:
        if (o.get("status") != "evaluated" or o.get("ret") is None):
            continue
        ts = _ts(o.get("entry_ts"))
        if ts:
            evaluated.append((o, ts))
    matched = []
    for trip in closed_details:
        t_ts = _ts(trip.get("entry_ts"))
        if not t_ts:
            continue
        sym = str(trip.get("sym") or "").upper()
        best, best_gap = None, None
        for o, ts in evaluated:
            if _sym_of(o.get("variety"), o.get("code")) != sym:
                continue
            if (o.get("direction_int") or 0) * (trip.get("direction") or 0) <= 0:
                continue                                  # 同方向才配
            gap = abs((ts - t_ts).total_seconds())
            if gap <= window_min * 60 and (best_gap is None or gap < best_gap):
                best, best_gap = o, gap
        if best is not None:
            matched.append((trip, best))
    n = len(matched)
    if not n:
        return [], {"n": 0}
    dir_ok = sum(1 for t, o in matched
                 if (t["net_pnl"] > 0) == (float(o["ret"]) > 0))
    paper_ret = [(t["net_pnl"] / t["entry_notional"]) if t["entry_notional"] else None
                 for t, _ in matched]
    paper_ret = [x for x in paper_ret if x is not None]
    agg = {"n": n,
           "dir_consistency": round(dir_ok / n, 3),
           "signal_ret_mean": round(sum(float(o["ret"]) for _, o in matched) / n, 5),
           "paper_ret_mean": round(sum(paper_ret) / len(paper_ret), 5) if paper_ret else None,
           "signal_win": round(sum(1 for _, o in matched if float(o["ret"]) > 0) / n, 3),
           "paper_win": round(sum(1 for t, _ in matched if t["net_pnl"] > 0) / n, 3)}
    return matched, agg


# =========================== ③ 回测参照（可选） ===========================
def backtest_reference(reports_dir):
    """读组合/回测产物尾部摘要；缺失返回 None（诚实降级，不编造）。"""
    ref = {}
    pe = os.path.join(reports_dir, "portfolio_equity.csv")
    if os.path.isfile(pe):
        try:
            with open(pe, "r", encoding="utf-8-sig") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            if len(lines) >= 2:
                head = lines[0].split(",")
                last = lines[-1].split(",")
                row = dict(zip(head, last))
                ref["portfolio_equity_csv"] = {
                    "rows": len(lines) - 1,
                    "last_date": row.get("date") or row.get("ts") or "",
                    "last_equity": row.get("equity") or row.get("dynamic_equity") or ""}
        except OSError:
            pass
    bt = os.path.join(reports_dir, "backtest_report.txt")
    ref["backtest_report_txt_exists"] = os.path.isfile(bt)
    return ref or None


# =========================== 汇总/渲染 ===========================
def reconcile_all(accounts_dir=None, monitor_db=None, reports_dir=None, window_min=30):
    accounts_dir = accounts_dir or os.path.join(_ROOT, "data", "paper_accounts")
    monitor_db = monitor_db or config.MONITOR_DB
    reports_dir = reports_dir or os.path.join(_ROOT, "reports")
    accounts = []
    for db in sorted(glob.glob(os.path.join(accounts_dir, "paper_*.db"))):
        r = reconcile_account(db)
        if r:
            accounts.append(r)
    outcome_rows = []
    if os.path.isfile(monitor_db):
        conn = sqlite3.connect("file:%s?mode=ro" % monitor_db.replace("\\", "/"), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            outcome_rows = [dict(r) for r in conn.execute("SELECT * FROM signal_outcomes")]
        finally:
            conn.close()
    for a in accounts:
        _, a["signal_match"] = match_signals(a["closed_details"], outcome_rows, window_min)
        a.pop("closed_details")               # 明细不进汇总 JSON（体量控制）
    total_closed = sum(a["n_closed"] for a in accounts)
    total_net = round(sum(a["net_total"] for a in accounts), 2)
    wins = [a for a in accounts if a["net_total"] > 0]
    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window_min": window_min, "n_accounts": len(accounts),
        "total_closed_trips": total_closed, "total_net": total_net,
        "g1_verdict": ("样本不足（全部账户合计平仓 %d 笔 < 20），暂不下结论" % total_closed)
                      if total_closed < 20 else
                      ("含真实成本后为正（汇总净 %+.0f 元）——G1 验收继续观察"
                       if total_net > 0 else
                       "含真实成本后为负（汇总净 %+.0f 元）——按纪律回退阈值/控规模" % total_net),
        "accounts": accounts,
        "backtest_reference": backtest_reference(reports_dir),
    }


def render(res):
    L = ["纸面账户三方对账（G1 验收预备，第128轮）", "=" * 60,
         "生成: %s ｜ 账户 %d 个 ｜ 已平仓回合合计 %d" % (
             res["generated"], res["n_accounts"], res["total_closed_trips"]),
         "匹配窗口: ±%d 分钟 ｜ 汇总净盈亏(含费含滑点): %+.2f 元" % (
             res["window_min"], res["total_net"]),
         "G1 验收结论: %s" % res["g1_verdict"], ""]
    L.append("%-14s %5s %6s %9s %7s %7s %7s %6s %6s %5s" % (
        "账户", "平仓", "胜率", "净盈亏", "PF", "均笔", "均持仓", "费用", "滑点", "守恒"))
    for a in sorted(res["accounts"], key=lambda x: x["net_total"], reverse=True):
        L.append("%-14s %5d %6s %9s %7s %7s %7s %6.0f %6.0f %5s" % (
            a["name"][:14], a["n_closed"],
            ("%.0f%%" % (a["win_rate"] * 100)) if a["win_rate"] is not None else "—",
            "%+.0f" % a["net_total"],
            ("%.2f" % a["pf"]) if a["pf"] is not None and a["pf"] < 999 else ("∞" if a["pf"] == 999.0 else "—"),
            ("%+.0f" % a["avg_net"]) if a["avg_net"] is not None else "—",
            ("%.0f分" % a["avg_hold_min"]) if a["avg_hold_min"] is not None else "—",
            a["fees_total"], a["slip_total"],
            "—" if a["identity_ok"] is None else
            ("ok" if a["identity_ok"] else "***差%.2f" % (a["equity0_spread"] or -1))))
    sm = [a for a in res["accounts"] if a.get("signal_match", {}).get("n")]
    L.append("")
    L.append("② 信号对照（纸面入场 ±%d 分钟内已评估的 signal_outcomes）：" % res["window_min"])
    if not sm:
        L.append("  （暂无可匹配样本：信号未到期评估或纸面尚无平仓——诚实留空）")
    for a in sm:
        m = a["signal_match"]
        L.append("  %-14s 匹配 %d 对｜方向一致率 %.0f%%｜信号均收 %+.3f%% vs 纸面净均收 %+.3f%%"
                 "｜信号胜率 %.0f%% vs 纸面胜率 %.0f%%" % (
                     a["name"][:14], m["n"], m["dir_consistency"] * 100,
                     m["signal_ret_mean"] * 100,
                     (m["paper_ret_mean"] or 0) * 100,
                     m["signal_win"] * 100, m["paper_win"] * 100))
    L.append("  （口径：信号=纯方向价格收益不含成本；纸面=含费含滑点、持仓期≠信号回看期，只比方向与相对量级）")
    L.append("")
    L.append("③ 回测参照：%s" % (
        json.dumps(res["backtest_reference"], ensure_ascii=False)
        if res["backtest_reference"] else "（portfolio_equity.csv / backtest_report.txt 均未生成，诚实降级）"))
    L.append("")
    L.append("（只读对账：不改任何生产表/参数；对账为负按纪律回退阈值/控规模，不自动操作）")
    return "\n".join(L)


def selftest():
    """零网络合成断言：守恒恒等式/配对/胜率PF/信号匹配/渲染。"""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="paper_reconcile_")
    try:
        db = os.path.join(tmp, "paper_测试.db")
        conn = sqlite3.connect(db)
        conn.execute("""CREATE TABLE paper_trades(id INTEGER PRIMARY KEY, ts TEXT, pos_ref TEXT,
            sym TEXT, side TEXT, direction INTEGER, lots INTEGER, price REAL, notional REAL,
            slip_yuan REAL, fee_yuan REAL, realized_yuan REAL, leg TEXT, reason TEXT,
            forced INTEGER, entry_ts TEXT, entry_price REAL)""")
        conn.execute("""CREATE TABLE paper_equity(id INTEGER PRIMARY KEY, ts TEXT,
            static_equity REAL, float_pnl REAL, equity REAL, realized REAL, fees_paid REAL,
            n_trades INTEGER)""")
        conn.execute("CREATE TABLE paper_orders(id INTEGER PRIMARY KEY, status TEXT)")
        # 胜的一单：open→close，净 +200；亏的一单：净 -80；一笔在途；一笔强平
        rows = [
            ("2026-09-01 09:30:00", "RB-1", "RB", "open", 1, 1, 3000, 30000, 3, 6, 0, "开仓", 0, "2026-09-01 09:30:00", 3000),
            ("2026-09-01 14:30:00", "RB-1", "RB", "close", 1, 1, 3020, 30200, 3, 6, 200, "平仓", 0, "2026-09-01 09:30:00", 3000),
            ("2026-09-02 09:30:00", "MA-1", "MA", "open", -1, 1, 2500, 25000, 2.5, 5, 0, "开仓", 0, "2026-09-02 09:30:00", 2500),
            ("2026-09-02 14:30:00", "MA-1", "MA", "close", -1, 1, 2504, 25040, 2.5, 5, -80, "平仓", 0, "2026-09-02 09:30:00", 2500),
            ("2026-09-03 09:30:00", "CU-1", "CU", "open", 1, 1, 70000, 700000, 70, 25, 0, "开仓", 0, "2026-09-03 09:30:00", 70000),
            ("2026-09-04 09:30:00", "I-1", "I", "open", 1, 1, 800, 8000, 8, 6, 0, "开仓", 1, "2026-09-04 09:30:00", 800),
            ("2026-09-04 09:35:00", "I-1", "I", "close", 1, 1, 799, 7990, 8, 6, -10, "平仓", 1, "2026-09-04 09:30:00", 800),
        ]
        for r in rows:
            conn.execute("INSERT INTO paper_trades(ts,pos_ref,sym,side,direction,lots,price,"
                         "notional,slip_yuan,fee_yuan,realized_yuan,leg,forced,entry_ts,entry_price)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
        # 权益两行：equity0=100000，realized 分别 0 和 110（200-80-10）
        conn.execute("INSERT INTO paper_equity(ts,static_equity,float_pnl,equity,realized,fees_paid,n_trades)"
                     " VALUES('2026-09-01 09:35:00',100000,0,100000,0,6,1)")
        conn.execute("INSERT INTO paper_equity(ts,static_equity,float_pnl,equity,realized,fees_paid,n_trades)"
                     " VALUES('2026-09-04 09:40:00',100110,5,100115,110,22,3)")
        conn.execute("INSERT INTO paper_orders(id,status) VALUES(1,'filled'),(2,'pending')")
        conn.commit()
        conn.close()
        acc = reconcile_account(db, name="paper_测试")
        assert acc["identity_ok"] is True and acc["equity0_implied"] == 100000.0, acc
        assert acc["n_closed"] == 3 and acc["n_open"] == 1 and acc["forced_n"] == 2
        assert acc["net_total"] == 110.0 and acc["win_rate"] == 0.3333
        assert acc["pf"] == 2.222 and acc["orders_pending"] == 1
        # 信号匹配：RB 多头 09-01 09:45 评估 ret=+0.5%（方向/时刻/品种均匹配）
        outcomes = [{"variety": "螺纹钢", "code": "RB0", "direction_int": 1,
                     "entry_ts": "2026-09-01 09:45:00", "ret": 0.005, "status": "evaluated"},
                    {"variety": "螺纹钢", "code": "RB0", "direction_int": -1,
                     "entry_ts": "2026-09-01 09:45:00", "ret": -0.005, "status": "evaluated"}]
        matched, agg = match_signals(acc.pop("closed_details"), outcomes, window_min=30)
        assert len(matched) == 1 and matched[0][0]["pos_ref"] == "RB-1"
        assert agg["dir_consistency"] == 1.0 and agg["signal_ret_mean"] == 0.005
        # 渲染不抛 + 关键行存在
        res = {"generated": "t", "window_min": 30, "n_accounts": 1, "total_closed_trips": 2,
               "total_net": 110.0, "g1_verdict": "样本不足", "accounts": [acc],
               "backtest_reference": None}
        txt = render(res)
        assert "G1 验收结论" in txt and "信号对照" in txt
        print("paper_reconcile selftest OK")
        return 0
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="纸面账户三方对账（G1 验收，研究侧只读）")
    ap.add_argument("--accounts-dir", default=os.path.join(_ROOT, "data", "paper_accounts"))
    ap.add_argument("--monitor-db", default=config.MONITOR_DB)
    ap.add_argument("--reports-dir", default=os.path.join(_ROOT, "reports"))
    ap.add_argument("--match-window-min", type=int, default=30)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    res = reconcile_all(args.accounts_dir, args.monitor_db, args.reports_dir,
                        window_min=args.match_window_min)
    txt = render(res)
    os.makedirs(args.reports_dir, exist_ok=True)
    txt_path = os.path.join(args.reports_dir, "paper_reconcile.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    js_path = os.path.join(args.reports_dir, "paper_reconcile.json")
    with open(js_path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(txt)
    print("\n已写出: %s / %s" % (txt_path, js_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main() if len(sys.argv) > 1 else selftest())
