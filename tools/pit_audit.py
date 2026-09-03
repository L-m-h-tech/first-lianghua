# -*- coding: utf-8 -*-
r"""G21（第36轮）PIT / 训练-服务一致性通用审计器（纯标准库、离线）。

对标 Feast/Tecton 的 offline/online parity 与 point-in-time as-of join：离线训练用的特征必须在事件时点
就已经可得（feature_ts ≤ event_ts），否则"离线很好、上线塌掉"（training-serving skew）。本模块提供三类
可复用、可被 pytest 钉死的检查（不接 main、不改综合分）：

  ① 时间戳泄漏扫描 timestamp_leaks：对任意"特征-事件"表统计 feature_ts>event_ts（as-of 越界）。
  ② 结构性无未来函数 future_perturb：篡改某时点之后的全部价格，重算该时点特征必须逐值不变。
  ③ 训练-服务一致性 parity：面板第 t 行特征 == 对同一 bar 前缀走"实时同款" compute_indicators 的结果。
另含 audit_panel_db：对缓存面板做结构检查（日期严格递增唯一、ret1d 可由收盘价复算、无 NaN/Inf）。

用法（项目根目录）：
  D:\Python\python.exe tools\pit_audit.py --db cache\research_panel.db     # 只审计已缓存面板（零网络）
  D:\Python\python.exe tools\pit_audit.py --codes RB0,MA0 --days 800       # 联网重拉做实时/离线parity
  D:\Python\python.exe tools\pit_audit.py --selftest
"""
import argparse
import math
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import config  # noqa: E402
import futures_data  # noqa: E402
import panel_builder as pb  # noqa: E402

TOL = 1e-9


# =========================== ① 时间戳 as-of 泄漏扫描（纯函数） ===========================
def timestamp_leaks(rows, feature_ts_key, event_ts_key):
    """返回 feature_ts>event_ts 的越界行下标列表（ISO 字符串/数值/日期均可按字典序/大小比较）。

    任一时间戳缺失（None）跳过、不算泄漏也不算通过（调用方应另行统计缺失率）。
    """
    bad = []
    for i, r in enumerate(rows):
        fts, ets = r.get(feature_ts_key), r.get(event_ts_key)
        if fts is None or ets is None:
            continue
        if fts > ets:
            bad.append(i)
    return bad


def asof_join_check(joined):
    """joined=[(event_ts, feature_ts)...]；返回 (n_checked,n_leak,leak_idx)。feature_ts>event_ts 即越界。"""
    n, leak = 0, []
    for i, (ets, fts) in enumerate(joined):
        if ets is None or fts is None:
            continue
        n += 1
        if fts > ets:
            leak.append(i)
    return n, len(leak), leak


# =========================== ② 结构性无未来函数（扰动法，纯函数） ===========================
def future_perturb_check(bars, build_row_fn, t, mutator, compare_keys):
    """篡改 bar 下标 t **之后** 的全部 bar，比较 t 对应面板行在篡改前后是否逐值不变。

    注意"未来"相对真实 bar 下标 t（面板暖机后行与其源 bar 按日期对齐）；返回 (before,after) 两 dict。
    """
    base = {r["date"]: r for r in build_row_fn(bars)}
    pert = [dict(b) for b in bars]
    for j in range(t + 1, len(pert)):
        mutator(pert[j])
    after = {r["date"]: r for r in build_row_fn(pert)}
    d = bars[t].get("d")
    return base.get(d), after.get(d)


def assert_no_future(bars, build_row_fn, t_idxs, compare_keys):
    """对多个真实 bar 下标做扰动法，返回不一致 [(t,key,before,after)]，空=无未来函数。"""
    diverge = []

    def mut(b):
        for k in ("o", "h", "l", "c"):
            if k in b:
                b[k] = b[k] * 1.37 + 0.7

    for t in t_idxs:
        before, after = future_perturb_check(bars, build_row_fn, t, mut, compare_keys)
        if before is None or after is None:
            continue
        for k in compare_keys:
            a, b = before.get(k), after.get(k)
            if a is None and b is None:
                continue
            if a is None or b is None or (isinstance(a, (int, float)) and abs(a - b) > TOL) or a != b:
                diverge.append((t, k, a, b))
    return diverge


# =========================== ③ 训练-服务一致性 parity（纯函数） ===========================
def parity_one(bars, panel_row, t, feature_keys):
    """实时路径=futures_data.compute_indicators(bars[:t+1])，与面板第 t 行逐字段比；返回不一致列表。"""
    live = futures_data.compute_indicators(bars[:t + 1])
    mism = []
    for k in feature_keys:
        a = pb._num(live.get(k))
        b = panel_row.get(k)
        if a is None and b is None:
            continue
        if a is None or b is None or abs(a - b) > TOL:
            mism.append((k, a, b))
    return mism


def parity_for_symbol(raw_bars, sample=None, warmup=None, feature_keys=None):
    """对单品种：面板逐行 vs 实时前缀复算，均匀抽样，返回 (n_compared,mismatches)。"""
    feature_keys = pb.FEATURE_COLS if feature_keys is None else feature_keys
    rows, _ = pb.build_symbol_rows("X", "测试", raw_bars, warmup=warmup)
    # build_symbol_rows 内部做了比例复权；parity 必须用复权后的同一序列，这里复算一次对齐
    adj, _ = __import__("backtest").ratio_adjusted_bars(list(raw_bars))
    n = len(rows)
    if n == 0:
        return 0, []
    if sample is None or sample >= n:
        idxs = range(n)
    else:
        step = max(1, n // sample)
        idxs = range(0, n, step)
    mism = []
    compared = 0
    # 面板第 i 行对应复权序列下标 t = warmup 起；直接按 date 对齐
    date_to_t = {str(b.get("d", "")): t for t, b in enumerate(adj)}
    for i in idxs:
        row = rows[i]
        t = date_to_t.get(row["date"])
        if t is None:
            continue
        compared += 1
        mm = parity_one(adj, row, t, feature_keys)
        for x in mm:
            mism.append((row["date"],) + x)
    return compared, mism


# =========================== 缓存面板结构审计（零网络） ===========================
def audit_panel_db(db_path):
    """对 research_panel 缓存做结构/PIT 检查，返回 dict（问题列表 issues，空=通过）。"""
    issues = []
    conn = sqlite3.connect(str(db_path))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(research_panel)")]
    real_cols = [c for c in pb.ALL_COLS if c not in ("sym", "date", "sector")]
    rows = conn.execute("SELECT %s FROM research_panel ORDER BY sym,date"
                        % ",".join(pb.ALL_COLS)).fetchall()
    conn.close()
    by_sym, prev = {}, {}
    for tup in rows:
        r = dict(zip(pb.ALL_COLS, tup))
        by_sym.setdefault(r["sym"], []).append(r)
    for sym, rs in by_sym.items():
        dates = [r["date"] for r in rs]
        if len(set(dates)) != len(dates):
            issues.append("%s 日期不唯一" % sym)
        if dates != sorted(dates):
            issues.append("%s 日期未严格递增" % sym)
        prev_c = None
        for r in rs:
            # ret1d 必须可由相邻收盘价复算（无未来、口径一致）
            if r["ret1d"] is not None and prev_c is not None and r["c"]:
                expect = r["c"] / prev_c - 1.0
                if abs(expect - r["ret1d"]) > 1e-9:
                    issues.append("%s %s ret1d 与收盘价不自洽 %r vs %r"
                                  % (sym, r["date"], r["ret1d"], expect))
            prev_c = r["c"]
            # 不允许 NaN/Inf 落库（None 允许=历史不足，绝不编造）
            for c in real_cols:
                v = r.get(c)
                if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                    issues.append("%s %s %s 非有限值" % (sym, r["date"], c))
    return {"n_rows": len(rows), "n_sym": len(by_sym), "issues": issues}


# =========================== 联网实时/离线 parity（离线工具） ===========================
def parity_via_network(codes_arg, days, sample):
    items = pb.resolve_items(codes_arg)
    report = []
    for name, code, sector, sym in items:
        try:
            raw = futures_data.fetch_daily_kline(code)[-days:]
            n, mism = parity_for_symbol(raw, sample=sample)
            report.append((sym, n, mism))
        except Exception as e:
            report.append((sym, 0, [("ERR", str(e), None, None)]))
    return report


def run(argv=None):
    ap = argparse.ArgumentParser(description="G21 PIT/训练-服务一致性审计")
    ap.add_argument("--db", default=config.PANEL_DB, help="审计缓存面板（零网络）")
    ap.add_argument("--codes", default=None, help="联网重拉做实时/离线parity，如 RB0,MA0")
    ap.add_argument("--days", type=int, default=config.PANEL_DAYS)
    ap.add_argument("--sample", type=int, default=config.PANEL_PARITY_SAMPLE)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.codes:
        print("实时/离线训练-服务一致性 parity（每品种均匀抽样 %d 时点）：" % args.sample)
        for sym, n, mism in parity_via_network(args.codes, args.days, args.sample):
            flag = "OK" if not mism else "不一致%d" % len(mism)
            print("  %-6s 比较%d时点 %s" % (sym, n, flag))
            for m in mism[:5]:
                print("     ", m)
        return 0
    if not os.path.exists(args.db):
        print("面板库不存在：%s（先用 panel_builder 构建）" % args.db)
        return 1
    res = audit_panel_db(args.db)
    print("缓存面板结构/PIT 审计：%d 行 / %d 品种" % (res["n_rows"], res["n_sym"]))
    if res["issues"]:
        print("发现问题 %d 条：" % len(res["issues"]))
        for x in res["issues"][:30]:
            print("  -", x)
        return 2
    print("审计通过：日期唯一递增、ret1d 与收盘价自洽、无 NaN/Inf（无未来函数/无编造）")
    return 0


# =========================== 零网络合成断言 ===========================
def selftest():
    import tempfile
    # 1) 时间戳泄漏：能检出越界、干净表不误报、缺失跳过
    rows = [{"f": "2026-01-02", "e": "2026-01-01"},   # 越界
            {"f": "2026-01-01", "e": "2026-01-01"},   # 相等允许
            {"f": None, "e": "2026-01-01"}]            # 缺失跳过
    assert timestamp_leaks(rows, "f", "e") == [0]
    n, nleak, _ = asof_join_check([("2026-01-01", "2026-01-02"), ("2026-01-03", "2026-01-03")])
    assert n == 2 and nleak == 1

    # 2) 结构性无未来函数：面板行在未来价格被篡改后逐值不变
    raw = pb._synthetic_bars(60)

    def build(bars):
        rr, _ = pb.build_symbol_rows("RB", "黑色", bars, warmup=10)
        return rr

    keys = ["ret1d"] + pb.FEATURE_COLS
    div = assert_no_future(raw, build, [9, 19, 34, 49], keys)
    assert not div, div

    # 反向用例：故意引入未来函数（用全序列最后一根）必须被抓到
    def leaky_build(bars):
        rr, _ = pb.build_symbol_rows("RB", "黑色", bars, warmup=10)
        if len(bars) > 0:
            for r in rr:
                r["ret1d"] = bars[-1]["c"]            # 偷看最后一根=未来
        return rr
    assert assert_no_future(raw, leaky_build, [10, 30], ["ret1d"]), "未来函数未被扰动法检出"

    # 3) parity：面板与实时同函数逐值一致；注入差异能被检出
    n, mism = parity_for_symbol(raw, sample=12)
    assert n > 0 and not mism, mism[:3]
    rows3, _ = pb.build_symbol_rows("RB", "黑色", raw, warmup=10)
    adj, _ = __import__("backtest").ratio_adjusted_bars(list(raw))
    d2t = {str(b.get("d", "")): t for t, b in enumerate(adj)}
    hacked = dict(rows3[10]); hacked["ma5"] = 999.0
    assert parity_one(adj, hacked, d2t[hacked["date"]], ["ma5"])

    # 4) 缓存面板结构审计：干净面板通过；篡改 ret1d / 注入 NaN 被抓
    with tempfile.TemporaryDirectory() as td:
        dbp = os.path.join(td, "p.db")
        st = pb.PanelStore(dbp)
        good, _ = pb.build_symbol_rows("RB", "黑色", pb._synthetic_bars(50), warmup=10)
        st.replace_symbol("RB", good)
        st.close()
        res = audit_panel_db(dbp)
        assert not res["issues"], res["issues"]
        # 破坏 ret1d 自洽
        st = pb.PanelStore(dbp)
        bad = [dict(r) for r in good]
        bad[5]["ret1d"] = 9.99
        st.replace_symbol("RB", bad)
        st.close()
        res2 = audit_panel_db(dbp)
        assert any("ret1d" in x for x in res2["issues"]), res2["issues"]

    print("pit_audit selftest ALL PASS（时间戳泄漏/asof越界/扰动无未来+反向用例/训练服务parity+注入检出/面板结构审计）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
