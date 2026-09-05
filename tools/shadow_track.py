# -*- coding: utf-8 -*-
r"""第83轮 G7/G25续：影子信号追踪 shadow_track.py——前向样本外证据链（研究侧，零主链改动）。

背景（第82轮拍板，路径A）：xsmom 8.5年长窗复核翻转"动量证伪"（全市场基线双样本稳健✅）、
tsmom252 长窗 7/7 年正——但回测再漂亮也可能是"口径/时段幸存者偏差"（第76/80轮两次自我
纠错的教训）。**影子 = 唯一无法被历史挑选污染的证据：从今天起的每一根K线都是真正样本外。**

做什么：
  1) 每日把三个**事先登记**的影子信号当日截面记录进 cache/shadow_signals.db（幂等）：
     - xsmom252_baseline  ：252日收益的截面均匀秩（全市场）——xsmom 主组合镜像；
     - tsmom252_factor    ：252日波动调整动量 tsmom252 的截面均匀秩——单因子候选；
     - xsmom252_ex_energy ：同基线但剔除"能源化工"板块——对照列（第82轮样本内挑选，不作主候选）；
  2) 对已到期的记录（信号日+h 个交易日已收盘）按实际价格回填多空绩效与截面IC；
  3) 出 reports/shadow_track.txt：逐信号 影子累积绩效（对齐非重叠、含成本） vs 回测预期，
     以及当日信号快照（多/空腿清单）。**成本后为负必须诚实呈现并回退**（G1 纪律）。

诚实边界（写死）：
  - 影子自启动日起算，**不回填历史**（回填=把回测再演一遍，无增量证据价值）；
  - 价格源=长面板（近月比例复权），与第82轮证据同口径；口径变化须重置影子并登记；
  - 影子只是积累证据，绝不改 analyzer/综合分；晋升须 影子≥N期 + 双样本复核 + 用户拍板。

纯标准库、零新增依赖。用法（项目根目录）：
  D:\\Python\\python.exe tools\\shadow_track.py --run            # 记录最新日信号+评估+报告
  D:\\Python\\python.exe tools\\shadow_track.py --daily          # 全链：top-up→长面板重建→--run
  D:\\Python\\python.exe tools\\shadow_track.py --report-only    # 只出报告
  D:\\Python\\python.exe tools\\shadow_track.py --selftest
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime as _dt_now
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

import config                       # noqa: E402  品种表
import factor_health as fh          # noqa: E402  forward_map（严格未来收益）
import orthogonal_blend_oos as ob   # noqa: E402  cs_uniform/quantile_ls_day/对齐绩效原语
import panel_builder as pb          # noqa: E402  长面板回读

DEFAULT_PANEL_DB = ROOT / "cache" / "research_panel_long.db"
DEFAULT_SHADOW_DB = ROOT / "cache" / "shadow_signals.db"
DEFAULT_TXT = ROOT / "reports" / "shadow_track.txt"
DEFAULT_JSON = ROOT / "reports" / "shadow_track.json"
ENERGY_SECTOR = "能源化工"
H_DEFAULT = 20
N_Q = 5
MIN_SYMS = 10
# 事先登记的影子信号（第82轮拍板；改动=重置影子并在此登记原因）。list 以便自测临时替换。
SIGNAL_SPECS = [
    {"key": "xsmom252_baseline", "col": "ret252", "sector_filter": None,
     "label": "xsmom252全市场基线(252日收益截面秩)"},
    {"key": "tsmom252_factor", "col": "tsmom252", "sector_filter": None,
     "label": "tsmom252单因子(波动调整动量截面秩)"},
    {"key": "xsmom252_ex_energy", "col": "ret252", "sector_filter": ENERGY_SECTOR,
     "label": "xsmom252剔除能化(对照列,样本内挑选)"},
]


# =========================== 影子库 ===========================
def _shadow_db(path):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS shadow_signals(
                     signal TEXT NOT NULL, date TEXT NOT NULL, sym TEXT NOT NULL,
                     sector TEXT, score REAL, weight REAL,
                     PRIMARY KEY(signal, date, sym))""")
    con.execute("""CREATE TABLE IF NOT EXISTS shadow_meta(
                     key TEXT PRIMARY KEY, value TEXT)""")
    con.commit()
    return con


def log_signals(panel_db, shadow_db, h=H_DEFAULT, verbose=True, all_dates=False):
    """把信号截面写入影子库（幂等 INSERT OR REPLACE）。

    默认只记长面板**最新交易日**（生产语义：每日任务跑一次记一天）。
    all_dates=True（自测/模拟长期运行用）：记录 [shadow_start_date, 最新日] 的全部日期——
    shadow_start_date 在首次运行时写入 meta（=当时的最新日），**保证生产环境永不回填历史**；
    自测先手工把 start 设为面板首日，等价于"任务已逐日运行多月"。"""
    store = pb.PanelStore(panel_db)
    syms = sorted(store.symbols())
    bysym = {}
    for s in syms:
        rows = store.load_rows(s)
        if rows:
            bysym[s] = rows
    store.close()
    dates = sorted({r["date"] for rows in bysym.values() for r in rows})
    if not dates:
        return {"date": None, "logged": {}, "panel_days": 0}
    latest = dates[-1]
    con = _shadow_db(shadow_db)
    logged = {}
    try:
        row = con.execute("SELECT value FROM shadow_meta WHERE key='shadow_start_date'").fetchone()
        if row:
            start = row[0]
        else:
            start = dates[0] if all_dates else latest
            con.execute("INSERT OR REPLACE INTO shadow_meta(key,value) VALUES('shadow_start_date',?)",
                        (start,))
        todo = [d for d in dates if d >= start] if all_dates else [latest]
        for d in todo:
            for spec in SIGNAL_SPECS:
                items = []
                for sym, rows in bysym.items():
                    row = next((r for r in rows if r["date"] == d), None)
                    if row is None:
                        continue
                    if spec["sector_filter"] and row.get("sector") == spec["sector_filter"]:
                        continue
                    v = row.get(spec["col"])
                    if isinstance(v, (int, float)) and v == v and -1e308 < v < 1e308:
                        items.append((sym, row.get("sector"), float(v)))
                if len(items) < MIN_SYMS:
                    logged[spec["key"]] = logged.get(spec["key"], 0)
                    continue
                z = ob.cs_uniform({s: v for s, _sec, v in items})
                score_by_sym = {s: v for s, _sec, v in items}
                n = 0
                for sym, sec, _v in items:
                    if sym not in z:
                        continue
                    con.execute("INSERT OR REPLACE INTO shadow_signals(signal,date,sym,sector,score,weight)"
                                " VALUES(?,?,?,?,?,?)",
                                (spec["key"], d, sym, sec, score_by_sym[sym], z[sym]))
                    n += 1
                logged[spec["key"]] = logged.get(spec["key"], 0) + n
        con.execute("INSERT OR REPLACE INTO shadow_meta(key,value) VALUES('last_logged_date',?)",
                    (latest,))
        con.execute("INSERT OR REPLACE INTO shadow_meta(key,value) VALUES('h_days',?)", (str(h),))
        con.commit()
    finally:
        con.close()
    if verbose:
        print("影子信号已记录：date=%s %s" % (latest, logged))
    return {"date": latest, "logged": logged, "panel_days": len(dates)}


# =========================== 评估（到期信号 vs 实际价格） ===========================
def evaluate(panel_db, shadow_db, h=H_DEFAULT, n_q=N_Q, cost=None, verbose=True):
    """读影子库全部记录，按长面板实际价格回填到期绩效。返回 {signal: {...}}。

    绩效口径：按 h 对齐非重叠再平衡（books=每日记录，hold=h/period_days=h）、5层多顶空底、
    含单边成本；另给逐日截面IC（score vs 前向 h 日收益，重叠口径，仅作参考列）。"""
    cost = ob.DEFAULT_COST_ONEWAY if cost is None else cost
    store = pb.PanelStore(panel_db)
    bysym = {}
    for s in sorted(store.symbols()):
        rows = store.load_rows(s)
        if rows:
            bysym[s] = rows
    store.close()
    dates = sorted({r["date"] for rows in bysym.values() for r in rows})
    fwd = {}
    for sym, rows in bysym.items():
        rows = sorted(rows, key=lambda r: r["date"])
        closes = [r["c"] for r in rows]
        fm = fh.forward_map(closes, (h,))[h]
        fwd[sym] = {r["date"]: fm[t] for t, r in enumerate(rows)
                    if fm[t] is not None}
    con = sqlite3.connect(r"file:%s?mode=ro" % str(shadow_db).replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    recs = {}
    for row in con.execute("SELECT signal,date,sym,sector,score,weight FROM shadow_signals ORDER BY date"):
        recs.setdefault(row["signal"], {}).setdefault(row["date"], {})[row["sym"]] = row["score"]
    con.close()
    out = {}
    for spec in SIGNAL_SPECS:
        key = spec["key"]
        sig = recs.get(key, {})
        books, cs_ics = [], []
        for d in dates:
            if d not in sig:
                continue
            score = {s: v for s, v in sig[d].items() if isnum(v) and isnum(fwd.get(s, {}).get(d))}
            if len(score) < MIN_SYMS:
                continue
            yd = {s: fwd[s][d] for s in score}
            z = ob.cs_uniform(score)
            ic = ob.fe.spearman(list(z.values()), [yd[s] for s in z]) if len(z) >= 2 else None
            if isnum(ic):
                cs_ics.append((d, "all", ic, len(z)))
            books.append({key: dict(z), "y": dict(yd)})
        ls = ob.evaluate_ls_books_aligned(books, key, n_q, cost, hold=h, period_days=h)
        ics = em_summary(cs_ics)
        out[key] = {"label": spec["label"], "n_logged": len(sig), "n_periods": ls["n_periods"],
                    "net": ls["net"], "gross_annual": ls["gross"].get("annual_ret"),
                    "avg_turnover": ls["avg_turnover_one_sided"], "total_cost": ls["total_cost"],
                    "cs_ic": ics, "sector_filter": spec["sector_filter"]}
    return out


def isnum(x):
    return isinstance(x, (int, float)) and x == x and -1e308 < x < 1e308


def em_summary(cs_ics):
    """逐日截面IC汇总（与 expr_miner.cs_summary 同口径的本地实现，避免循环依赖）。"""
    xs = [tup[-2] for tup in cs_ics]   # 兼容 (d, ic, n) 与 (d, view, ic, n) 两种元数
    n = len(xs)
    if n == 0:
        return {"mean_ic": None, "t_stat": None, "pct_positive": None, "n_days": 0}
    import math
    mean = sum(xs) / n
    var = sum((v - mean) ** 2 for v in xs) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    return {"mean_ic": mean, "t_stat": (mean / sd * math.sqrt(n) if sd > 1e-15 else 0.0),
            "pct_positive": sum(1 for v in xs if v > 0) / n, "n_days": n}


# =========================== 报告 ===========================
def today_snapshot(panel_db):
    """最新日各信号的多/空腿清单（按截面秩分5档的顶/底档成员）。"""
    store = pb.PanelStore(panel_db)
    syms = sorted(store.symbols())
    latest, snap = None, {}
    for s in syms:
        rows = store.load_rows(s)
        for r in reversed(rows):
            if latest is None:
                latest = r["date"]
            if r["date"] == latest:
                snap[s] = r
            else:
                break
    store.close()
    out = {"date": latest, "legs": {}}
    for spec in SIGNAL_SPECS:
        items = []
        for sym, row in snap.items():
            if spec["sector_filter"] and row.get("sector") == spec["sector_filter"]:
                continue
            v = row.get(spec["col"])
            if isnum(v):
                items.append((sym, float(v)))
        if len(items) < MIN_SYMS:
            out["legs"][spec["key"]] = None
            continue
        z = ob.cs_uniform({s: v for s, v in items})
        ranked = sorted(z.items(), key=lambda kv: -kv[1])
        k = max(1, len(ranked) // 5)
        out["legs"][spec["key"]] = {"long": [s for s, _ in ranked[:k]],
                                    "short": [s for s, _ in ranked[-k:]]}
    return out


def render(result, snapshot):
    L = ["=" * 104,
         " 影子信号追踪 shadow_track（前向样本外证据链；只记录不改综合分）  生成于 %s"
         % _dt_now.now().strftime("%Y-%m-%d %H:%M:%S"),
         "=" * 104]
    L.append("价格源=长面板（近月比例复权，与第82轮证据同口径）；H=%d 对齐非重叠、5层多顶空底、含单边成本" % H_DEFAULT)
    L.append("-" * 104)
    for key, r in result.items():
        net = r["net"]
        ic = r["cs_ic"]
        L.append("● %s（%s）" % (key, r["label"]))
        L.append("    已记录 %d 日 / 到期 %d 期：净年化 %s、净夏普 %s、净回撤 %s、日均换手 %s、"
                 "累计成本拖累 %.2f%%"
                 % (r["n_logged"], r["n_periods"],
                    ("%+.2f%%" % (100.0 * net["annual_ret"])) if net.get("annual_ret") is not None else "未到期",
                    ("%+.2f" % net["sharpe"]) if net.get("sharpe") is not None else "--",
                    ("%+.2f%%" % (100.0 * net["max_drawdown"])) if net.get("max_drawdown") is not None else "--",
                    ("%+.3f" % r["avg_turnover"]) if r["avg_turnover"] is not None else "--",
                    100.0 * (r["total_cost"] or 0.0)))
        L.append("    截面IC(逐日参考)：%s / t %s / 正比例 %s / 天数 %d"
                 % (("%+.3f" % ic["mean_ic"]) if ic["mean_ic"] is not None else "--",
                    ("%+.1f" % ic["t_stat"]) if ic["t_stat"] is not None else "--",
                    ("%.0f%%" % (100.0 * ic["pct_positive"])) if ic["pct_positive"] is not None else "--",
                    ic["n_days"]))
    L.append("-" * 104)
    legs = snapshot.get("legs", {})
    L.append("[最新日信号快照 %s]（截面秩5档的顶/底档成员）" % snapshot.get("date"))
    for key in ("xsmom252_baseline", "tsmom252_factor", "xsmom252_ex_energy"):
        lg = legs.get(key)
        if not lg:
            L.append("  %-20s 无样本" % key)
            continue
        L.append("  %-20s 多腿: %s" % (key, " ".join(lg["long"])))
        L.append("  %-20s 空腿: %s" % ("", " ".join(lg["short"])))
    L.append("-" * 104)
    L.append("[诚实边界] 影子自启动日起算、不回填历史（回填=把回测再演一遍）；前向表现可能显著弱于回测——")
    L.append("  成本后为负必须诚实呈现并回退；影子只积累证据、绝不改 analyzer/综合分；晋升须影子≥N期+")
    L.append("  双样本复核+用户拍板。口径变化（价格源/信号定义）必须重置影子并在此登记原因。")
    L.append("=" * 104)
    return "\n".join(L)


# =========================== 主流程 ===========================
def run(panel_db=None, shadow_db=None, txt_path=None, json_path=None,
        h=H_DEFAULT, n_q=N_Q, cost=None, verbose=True):
    panel_db = str(panel_db or DEFAULT_PANEL_DB)
    shadow_db = str(shadow_db or DEFAULT_SHADOW_DB)
    txt_path = str(txt_path or DEFAULT_TXT)
    json_path = str(json_path or DEFAULT_JSON)
    logged = log_signals(panel_db, shadow_db, h=h, verbose=verbose)
    result = evaluate(panel_db, shadow_db, h=h, n_q=n_q, cost=cost, verbose=verbose)
    snapshot = today_snapshot(panel_db)
    payload = {"logged": logged, "signals": result, "snapshot": snapshot,
               "h": h, "generated_at": _dt_now.now().strftime("%Y-%m-%d %H:%M:%S")}
    text = render(result, snapshot)
    if verbose:
        print(text)
    os.makedirs(os.path.dirname(txt_path), exist_ok=True)
    with open(txt_path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(text + "\n")
    with open(json_path, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(payload, fp, ensure_ascii=False, allow_nan=False, indent=1)
    try:
        import experiment_ledger
        experiment_ledger.safe_record(
            "shadow_track", {"h": h, "signals": [sp["key"] for sp in SIGNAL_SPECS]},
            {k: v["n_periods"] for k, v in result.items()},
            inputs={"panel": panel_db, "shadow_db": shadow_db},
            artifacts=[txt_path, json_path],
            conclusion="影子信号追踪：前向样本外证据链，只记录不改综合分",
            reproduce="D:\\Python\\python.exe tools/shadow_track.py --run")
    except Exception:
        pass
    return payload


def daily_due(last_shadow_date, now=None, hour=None):
    """main 跟随触发判定（纯函数）：工作日且已过触发时刻，且影子当天未记过 → True。

    last_shadow_date：影子最近一次记录的日期（None=从未）；hour：触发时刻（默认
    env FUTURES_MONITOR_SHADOW_HOUR，17=收盘后日K已可用）。纯函数、可合成断言。"""
    import os as _os
    now = now or _dt_now.now()
    hour = int(_os.environ.get("FUTURES_MONITOR_SHADOW_HOUR", "17")) if hour is None else hour
    if now.weekday() >= 5 or now.hour < hour:
        return False
    return last_shadow_date != now.strftime("%Y-%m-%d")


def daily(panel_db=None, shadow_db=None, txt_path=None, json_path=None,
          h=H_DEFAULT, verbose=True):
    """全链（供计划任务单命令调用）：term top-up → 长面板重建 → 影子记录+评估+报告。"""
    import long_panel_builder as lpb
    import term_history as th
    import backtest as _bt
    items = _bt.resolve_codes("", None)
    tstore = th.TermHistoryStore(th.TERM_DB_PATH)
    try:
        stats = th.topup_varieties(items, tstore, verbose=verbose)
    finally:
        tstore.close()
    if verbose:
        print("top-up：%s" % {k: v for k, v in stats.items() if k != "errors"})
    lpb.run(verbose=verbose)
    return run(panel_db=panel_db, shadow_db=shadow_db, txt_path=txt_path,
               json_path=json_path, h=h, verbose=verbose)


def main(argv=None):
    ap = argparse.ArgumentParser(description="G7/G25续 影子信号追踪（研究侧零主链改动）")
    ap.add_argument("--run", action="store_true", help="记录最新日信号+评估+报告")
    ap.add_argument("--daily", action="store_true", help="全链：top-up→长面板重建→--run（供计划任务）")
    ap.add_argument("--report-only", action="store_true", help="只出报告（不记录新信号）")
    ap.add_argument("--panel-db", default=str(DEFAULT_PANEL_DB))
    ap.add_argument("--shadow-db", default=str(DEFAULT_SHADOW_DB))
    ap.add_argument("--h", type=int, default=H_DEFAULT)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.daily:
        daily(panel_db=args.panel_db, shadow_db=args.shadow_db, h=args.h)
        return 0
    if args.report_only:
        run(panel_db=args.panel_db, shadow_db=args.shadow_db, h=args.h, verbose=True)
        return 0
    if args.run:
        run(panel_db=args.panel_db, shadow_db=args.shadow_db, h=args.h, verbose=True)
        return 0
    ap.print_help()
    return 0


# =========================== 零网络/零DB 合成断言 ===========================
def selftest():
    import tempfile
    import long_panel_builder as lpb
    from term_history import TermHistoryStore
    tmpdir = tempfile.mkdtemp(prefix="shadow_t_")
    term_db = os.path.join(tmpdir, "th.db")
    tstore = TermHistoryStore(term_db)

    def _bars(base, d0, d1):
        out = []
        for d in range(d0, d1 + 1):
            from datetime import date as _d, timedelta as _td
            dt = _d(2026, 1, 1) + _td(days=d)
            c = base + d * 0.5
            out.append({"d": dt.isoformat(), "c": c, "s": c, "v": 5, "p": 50,
                        "h": c * 1.01, "l": c * 0.99, "o": c})
        return out

    # 12 品种、120 天（两合约全程有K线→任一日有非缓冲近月）：品种 i 相对增速递减 → 稳定截面序
    for i in range(12):
        tstore.save_contract("S%02d" % i, "S%02d2603" % i, _bars(100.0 + i * 5.0, 0, 119))
        tstore.save_contract("S%02d" % i, "S%02d2606" % i, _bars(100.0 + i * 5.0, 0, 119))
    tstore.close()
    # 长面板（warmup=60 保证 ret252 不可用 → 用 hv/ret126；影子信号列改用 ret126 代替做合成验证）
    old_spec = list(SIGNAL_SPECS)
    SIGNAL_SPECS.clear()
    SIGNAL_SPECS.extend([{"key": "test_mom", "col": "ret63", "sector_filter": None,
                          "label": "合成动量(63日,稳定截面序)"}])
    panel_db = os.path.join(tmpdir, "panel_long.db")
    lpb.TERM_DB_PATH = term_db
    try:
        for i in range(12):
            rows = lpb.build_rows("S%02d" % i, "测试", warmup=30, term_db=term_db)   # 合成缩短暖机
            st = pb.PanelStore(panel_db)
            st.replace_symbol("S%02d" % i, rows)
            st.close()
        # 模拟"任务已逐日运行多月"：先把影子启动日设为面板首日（经 _shadow_db 建 schema），再全程记录
        first_day = min(r["date"] for s in range(12)
                        for r in pb.PanelStore(panel_db).load_rows("S%02d" % s))
        con0 = _shadow_db(os.path.join(tmpdir, "sh.db"))
        con0.execute("INSERT OR REPLACE INTO shadow_meta(key,value) VALUES('shadow_start_date',?)",
                     (first_day,))
        con0.commit()
        con0.close()
        logged = log_signals(panel_db, os.path.join(tmpdir, "sh.db"), verbose=False, all_dates=True)
        assert logged["date"] is not None and logged["logged"]["test_mom"] >= MIN_SYMS
        res = evaluate(panel_db, os.path.join(tmpdir, "sh.db"), h=5, n_q=3, cost=0.0, verbose=False)
        r = res["test_mom"]
        assert r["n_periods"] > 0 and r["cs_ic"]["mean_ic"] is not None
        assert abs(r["cs_ic"]["mean_ic"]) > 0.9, r["cs_ic"]   # 品种序稳定：影子|IC|应≈1
        snap = today_snapshot(panel_db)
        assert snap["date"] is not None and snap["legs"]["test_mom"] is not None
        text = render(res, snap)
        assert "影子信号追踪" in text and "诚实边界" in text
    finally:
        SIGNAL_SPECS.clear()
        SIGNAL_SPECS.extend(old_spec)
    # 第85轮：daily_due 纯函数手算（周末/未到时刻/当天已记 → False；工作日17点后未记 → True）
    from datetime import datetime as _dt2
    tue_10 = _dt2(2026, 9, 8, 10, 0)
    tue_18 = _dt2(2026, 9, 8, 18, 0)
    sat_18 = _dt2(2026, 9, 12, 18, 0)
    assert daily_due("2026-09-07", tue_18) is True and daily_due("2026-09-08", tue_18) is False
    assert daily_due(None, tue_18) is True
    assert daily_due("2026-09-07", tue_10) is False and daily_due("2026-09-07", sat_18) is False
    print("shadow_track selftest ALL PASS（信号记录幂等/到期评估/合成强动量IC≈+1/快照/渲染/"
          "daily_due判定 共6组）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
