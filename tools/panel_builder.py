# -*- coding: utf-8 -*-
r"""G21（第36轮）标准研究面板层（research panel）——统一"品种×交易日×字段"离线研究底座，纯标准库。

此前 tsmom_eval/xsmom_eval/factor_eval/carry_eval/attribution 各自联网拉数、各自造面板，口径只靠
"共用同一函数"口头保证。本模块把**日线复权行情 + futures_data 技术指标 + 基本面快照(PIT as-of)**
统一成一张标准长表，落**独立** SQLite（cache/research_panel.db，gitignore、删文件即回退现拉），
**不碰生产 monitor.db 表结构、不接 main、不改综合分**（守三铁律）。

两条不允许违背的口径（有合成断言钉死）：
1. **Point-in-time / 无未来函数**：面板第 t 行的每个特征只由 bars[:t+1]（≤当日）经
   `futures_data.compute_indicators` 计算；基本面严格取 **trade_date < 当日** 的最近一条
   （日频基本面收盘后才生成，用当日即偷看未来），as-of 对齐、取不到为 NULL 绝不编造。
2. **训练-服务一致性（training-serving parity）**：面板逐日复算用的就是实时 analyzer 同一个
   futures_data.compute_indicators；pit_audit 抽样断言"面板第 t 行 == 对同一前缀实时算一遍"逐值一致。

缓存可重建且幂等：同一品种重建两次逐值一致（DELETE+INSERT 事务）。manifest 落 research_runs 表。
用法（项目根目录）：
  D:\Python\python.exe tools\panel_builder.py --codes RB0,MA0,CU0 --days 800   # 建几个品种
  D:\Python\python.exe tools\panel_builder.py --all --days 1023               # 全64品种
  D:\Python\python.exe tools\panel_builder.py --selftest                      # 零网络/零DB合成断言
"""
import argparse
import bisect
import math
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import config  # noqa: E402
import futures_data  # noqa: E402
import backtest  # noqa: E402  复用 ratio_adjusted_bars（主连比例复权，与所有研究工具同口径）
import factors_catalog as fc  # noqa: E402

# 面板落库列：标识 + 原始/复权行情 + 日收益 + 技术特征 + 基本面(PIT)
ID_COLS = ["sym", "date", "sector"]
RAW_COLS = list(config.PANEL_RAW_KEYS)          # o h l c v oi（其中 o/h/l/c 为比例复权后）
FEATURE_COLS = list(config.PANEL_FEATURE_KEYS)
FUND_COLS = ["fund_score", "fund_carry", "fund_basis"]
ALL_COLS = ID_COLS + RAW_COLS + ["ret1d"] + FEATURE_COLS + FUND_COLS


# =========================== 纯函数：PIT as-of（可合成断言） ===========================
def asof_before(sorted_dates, target, strict=True):
    """在升序日期序列里返回 **严格早于** target（strict=True）或 ≤target 的最近下标，没有返回 -1。

    研究面板用 strict=True：特征日 target 当天收盘后才产生的基本面不得用于 target 行（防未来函数）。
    """
    if not sorted_dates:
        return -1
    j = bisect.bisect_left(sorted_dates, target) if strict else bisect.bisect_right(sorted_dates, target)
    return j - 1


def asof_pick(pairs, target, strict=True):
    """pairs=[(date,value)...]升序；返回 (date,value) 最近一条满足 as-of 的记录，没有返回 (None,None)。"""
    dates = [p[0] for p in pairs]
    j = asof_before(dates, target, strict=strict)
    return pairs[j] if j >= 0 else (None, None)


# =========================== 纯函数：单品种逐行面板（可合成断言、不联网） ===========================
def _num(v):
    try:
        if v is None:
            return None
        x = float(v)
        return None if (math.isnan(x) or math.isinf(x)) else x
    except (TypeError, ValueError):
        return None


def build_symbol_rows(name, sector, raw_bars, fund_pairs=None, warmup=None,
                      feature_keys=None, strict_fund=True):
    """raw_bars（新浪日K [{d,o,h,l,c,v,p,s}]）→ 逐交易日面板行 list（PIT、无未来函数）。

    fund_pairs: 该品种 [(trade_date, fund_score, carry, basis_rate)...]（任意序，内部排序、严格 as-of）。
    返回 (rows, roll_count)。第 t 行只用 bars[:t+1]；不足 warmup 不入面板。
    """
    warmup = config.PANEL_WARMUP if warmup is None else warmup
    feature_keys = FEATURE_COLS if feature_keys is None else list(feature_keys)
    bars, roll_count = backtest.ratio_adjusted_bars(list(raw_bars))
    funds = sorted(fund_pairs or [], key=lambda x: x[0])
    fund_dates = [f[0] for f in funds]
    rows = []
    prev_close = None
    for t in range(len(bars)):
        b = bars[t]
        close = _num(b.get("c"))
        if close is None or close <= 0:
            prev_close = None
            continue
        if t + 1 < warmup:
            prev_close = close
            continue
        # 实时同函数：只用 ≤t 的前缀算指标（compute_indicators 内部长窗在140截断前用全前缀，天然PIT）
        try:
            ind = futures_data.compute_indicators(bars[:t + 1])
        except RuntimeError:
            prev_close = close
            continue
        row = {"sym": name, "date": str(b.get("d", "")), "sector": sector}
        for k in RAW_COLS:
            src = "p" if k == "oi" else k           # 新浪 p=持仓量
            row[k] = _num(b.get(src))
        row["ret1d"] = (close / prev_close - 1.0) if prev_close else None
        for k in feature_keys:
            row[k] = _num(ind.get(k))
        # 基本面严格 as-of（trade_date < 当日）
        j = asof_before(fund_dates, row["date"], strict=strict_fund)
        if j >= 0:
            _, fscore, fcarry, fbasis = funds[j]
            row["fund_score"], row["fund_carry"], row["fund_basis"] = (
                _num(fscore), _num(fcarry), _num(fbasis))
        else:
            row["fund_score"] = row["fund_carry"] = row["fund_basis"] = None
        rows.append(row)
        prev_close = close
    return rows, roll_count


# =========================== 面板缓存：独立 SQLite（可重建、幂等） ===========================
class PanelStore:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        if os.path.dirname(self.db_path):
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self._init_schema()

    def _col_decl(self):
        cols = ("sym TEXT NOT NULL, date TEXT NOT NULL, sector TEXT, "
                + ", ".join("%s REAL" % c for c in (RAW_COLS + ["ret1d"] + FEATURE_COLS + FUND_COLS))
                + ", PRIMARY KEY(sym,date))")
        return cols

    def _init_schema(self):
        c = self.conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS research_panel (%s" % self._col_decl())
        c.execute("""CREATE TABLE IF NOT EXISTS research_runs(
                      run_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, codes TEXT, days INTEGER,
                      n_sym INTEGER, n_rows INTEGER, date_min TEXT, date_max TEXT,
                      source TEXT, adjust TEXT, roll_count INTEGER, fields TEXT, note TEXT)""")
        self.conn.commit()

    def replace_symbol(self, sym, rows):
        """整品种幂等重建：事务内先删后插，重建两次逐值一致（合成断言钉死）。"""
        c = self.conn.cursor()
        c.execute("DELETE FROM research_panel WHERE sym=?", (sym,))
        placeholders = ",".join("?" * len(ALL_COLS))
        sql = "INSERT INTO research_panel(%s) VALUES(%s)" % (",".join(ALL_COLS), placeholders)
        payload = [[r.get(k) for k in ALL_COLS] for r in rows]
        c.executemany(sql, payload)
        self.conn.commit()
        return len(payload)

    def record_run(self, codes, days, n_sym, n_rows, dmin, dmax, roll_count, note=""):
        self.conn.execute(
            "INSERT INTO research_runs(ts,codes,days,n_sym,n_rows,date_min,date_max,source,adjust,roll_count,fields,note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ",".join(codes), days, n_sym, n_rows,
             dmin, dmax, "sina daily(主连)", "ratio_adjusted(换月跳空置0)", roll_count,
             ",".join(FEATURE_COLS), note))
        self.conn.commit()

    def count(self, sym=None):
        c = self.conn.cursor()
        if sym:
            return c.execute("SELECT COUNT(*) FROM research_panel WHERE sym=?", (sym,)).fetchone()[0]
        return c.execute("SELECT COUNT(*) FROM research_panel").fetchone()[0]

    def date_range(self, sym=None):
        q = "SELECT MIN(date),MAX(date) FROM research_panel"
        args = ()
        if sym:
            q += " WHERE sym=?"
            args = (sym,)
        return self.conn.execute(q, args).fetchone()

    def symbols(self):
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT sym FROM research_panel ORDER BY sym")]

    def load_rows(self, sym=None):
        q = "SELECT %s FROM research_panel" % ",".join(ALL_COLS)
        args = ()
        if sym:
            q += " WHERE sym=?"
            args = (sym,)
        q += " ORDER BY sym,date"
        cur = self.conn.execute(q, args)
        out = []
        for tup in cur:
            out.append(dict(zip(ALL_COLS, tup)))
        return out

    def manifests(self, limit=10):
        return self.conn.execute(
            "SELECT run_id,ts,codes,n_sym,n_rows,date_min,date_max,roll_count FROM research_runs "
            "ORDER BY run_id DESC LIMIT ?", (limit,)).fetchall()

    def close(self):
        self.conn.close()


# =========================== 联网构建（离线工具，失败软降级不编造） ===========================
def _fund_pairs_for(sym):
    """从生产 monitor.db **只读** 取该品种基本面 (trade_date,score,carry,basis)，缺库返回 []。"""
    dbp = config.MONITOR_DB
    if not os.path.exists(dbp):
        return []
    uri = "file:%s?mode=ro" % dbp.replace("\\", "/")
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT trade_date,fund_score,carry,basis_rate FROM fundamentals "
            "WHERE sym=? AND trade_date IS NOT NULL ORDER BY trade_date", (sym,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def resolve_items(codes_arg=None, limit=None):
    """复用 backtest.resolve_codes 的品种→主连代码映射（返回 [(中文名,主连代码,板块,sym)]）。"""
    raw = backtest.resolve_codes(codes_arg, limit)
    items = []
    for name, code in raw:
        meta = config.VARIETIES.get(name, {})
        sym = code.rstrip("0").upper()
        items.append((name, code, meta.get("cat", "未知"), sym))
    return items


# =========================== G21续（第37轮）：面板回读/统一装载层 ===========================
def panel_rows_to_bars(rows):
    """面板行（存的是**已比例复权** OHLC）回读成下游研究工具期望的 bar-dict 序列。

    p=持仓量(面板 oi)；主连时序/截面研究只用收盘价 c，结算价 s 以 c 代（真正含展期口径走 term_history）。
    """
    out = []
    for r in rows:
        out.append({"d": r["date"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"],
                    "v": r["v"], "p": r["oi"], "s": r["c"]})
    return out


def load_adjusted_bars(code, days, prefer_panel=False, db_path=None):
    """统一装载"**已比例复权**日K"，返回 (bars, source)。这是 G21续 让研究工具读面板的唯一入口。

    - prefer_panel=True 且独立面板库有该品种：直接回读已复权 bar（**绝不再二次 ratio_adjusted_bars**——
      实证对已复权序列再复权会因 MAD 阈值变小而把真实大波动误判成换月，SC/J 价位可偏 6%~12%）；
    - 否则（缺省）走旧网络路径 fetch_daily_kline[-days:]→ratio_adjusted_bars，与历史逐值一致（缺省等价旧版）。
    面板比原始序列少最初 PANEL_WARMUP-1 根（暖机前不入面板），对需≥最长回看窗的研究输出无影响。
    """
    sym = str(code).rstrip("0").upper()
    if prefer_panel:
        db_path = db_path or config.PANEL_DB
        if os.path.exists(db_path):
            st = PanelStore(db_path)
            rows = st.load_rows(sym)
            st.close()
            if rows:
                return panel_rows_to_bars(rows)[-days:], "panel"
    raw = futures_data.fetch_daily_kline(code)[-days:]
    bars, _ = backtest.ratio_adjusted_bars(raw)
    return bars, "network"


def build_items(items, days, store=None, use_fund=True, verbose=False):
    """对 (中文名,主连代码,板块,sym) 列表联网拉日K、建面板、写 store；返回每品种结果 dict。"""
    results = []
    total_rows = total_roll = 0
    for name, code, sector, sym in items:
        try:
            raw = futures_data.fetch_daily_kline(code)[-days:]
            funds = _fund_pairs_for(sym) if use_fund else []
            rows, roll = build_symbol_rows(sym, sector, raw, funds)
            n = store.replace_symbol(sym, rows) if store is not None else len(rows)
            dates = [r["date"] for r in rows]
            total_rows += n
            total_roll += roll
            results.append({"name": name, "sym": sym, "n": n, "roll": roll,
                            "dmin": dates[0] if dates else None,
                            "dmax": dates[-1] if dates else None, "err": ""})
            if verbose:
                print("  %-5s %-6s 行=%d 换月=%d %s~%s" %
                      (sym, name, n, roll, dates[0] if dates else "-", dates[-1] if dates else "-"))
        except Exception as e:
            results.append({"name": name, "sym": sym, "n": 0, "roll": 0,
                            "dmin": None, "dmax": None, "err": "%s: %s" % (type(e).__name__, e)})
            if verbose:
                print("  %-5s 失败：%s" % (sym, results[-1]["err"]))
    if store is not None and results:
        ok = [r for r in results if not r["err"]]
        if ok:
            store.record_run([r["sym"] for r in ok], days, len(ok), total_rows,
                             min(r["dmin"] for r in ok if r["dmin"]),
                             max(r["dmax"] for r in ok if r["dmax"]), total_roll)
    return results


def manifest_text(results, days, store=None):
    ok = [r for r in results if not r["err"]]
    bad = [r for r in results if r["err"]]
    n_rows = sum(r["n"] for r in ok)
    L = ["标准研究面板 G21 构建 manifest  生成于 %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         "=" * 96,
         "口径：主连比例复权(换月跳空置0)；逐行只用≤当日bar前缀经 futures_data.compute_indicators 计算(PIT)；",
         "      基本面严格取 trade_date<当日 的最近一条(as-of)；缓存独立 SQLite、可幂等重建、不接main不改综合分。",
         "请求交易日 days=%d；成功品种 %d、失败 %d；面板总行数 %d。" %
         (days, len(ok), len(bad), n_rows)]
    if ok:
        L.append("日期区间 %s ~ %s；累计换月跳空处理 %d 次。" %
                 (min(r["dmin"] for r in ok if r["dmin"]),
                  max(r["dmax"] for r in ok if r["dmax"]), sum(r["roll"] for r in ok)))
    L.append("字段：%s" % ",".join(ALL_COLS))
    L.append("特征注册表：%s" % fc.catalog_text().splitlines()[0])
    if bad:
        L.append("失败品种（软降级、不编造）：")
        for r in bad:
            L.append("  %s %s" % (r["sym"], r["err"]))
    if store is not None:
        L.append("库内累计：品种 %d、总行数 %d、区间 %s" %
                 (len(store.symbols()), store.count(), store.date_range()))
    return "\n".join(L) + "\n"


def run(argv=None):
    ap = argparse.ArgumentParser(description="G21 标准研究面板构建")
    ap.add_argument("--codes", default=None, help="中文名或主连代码逗号分隔，如 RB0,MA0")
    ap.add_argument("--all", action="store_true", help="全64品种")
    ap.add_argument("--days", type=int, default=config.PANEL_DAYS)
    ap.add_argument("--db", default=config.PANEL_DB)
    ap.add_argument("--no-fund", action="store_true", help="不做基本面PIT as-of拼接")
    ap.add_argument("--manifest", default=config.PANEL_MANIFEST)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.all and not args.codes:
        ap.error("请用 --codes RB0,MA0 或 --all")
    items = resolve_items(None if args.all else args.codes)
    store = PanelStore(args.db)
    results = build_items(items, args.days, store=store, use_fund=not args.no_fund, verbose=True)
    text = manifest_text(results, args.days, store)
    with open(args.manifest, "w", encoding="utf-8-sig") as fh:
        fh.write(text)
    print(text)
    store.close()
    return 0


# =========================== 零网络/零DB 合成断言 ===========================
def _synthetic_bars(n=60, start=100.0, drift=0.002, seed=0):
    """确定性合成日K（不用随机，便于手算与未来扰动对照）：close 单调+小幅波动。"""
    bars = []
    price = start
    for i in range(n):
        c = start * (1 + drift * i) + (0.3 if i % 3 == 0 else 0.0)
        o = c - 0.1
        h = c + 0.2
        l = c - 0.3
        bars.append({"d": "2026-%02d-%02d" % (i // 28 + 1, min(28, i % 28 + 1)),
                     "o": o, "h": h, "l": l, "c": c, "v": 1000 + i, "p": 5000 + i, "s": c})
        price = c
    return bars


def selftest():
    import tempfile
    # 1) as-of：严格早于 / ≤ 两档，边界手算
    ds = ["2026-01-01", "2026-01-03", "2026-01-05"]
    assert asof_before(ds, "2026-01-01", strict=True) == -1      # 严格：当日不可用
    assert asof_before(ds, "2026-01-01", strict=False) == 0
    assert asof_before(ds, "2026-01-04", strict=True) == 1
    assert asof_before([], "2026-01-01") == -1
    pairs = [(d, i) for i, d in enumerate(ds)]
    assert asof_pick(pairs, "2026-01-05", strict=True) == ("2026-01-03", 1)
    assert asof_pick(pairs, "2025-01-01") == (None, None)

    # 2) 逐行面板：暖机、ret1d 手算、行数与日期对齐
    raw = _synthetic_bars(60)
    rows, _ = build_symbol_rows("RB", "黑色", raw, warmup=10)
    assert len(rows) == 60 - 9                      # t+1>=10 → 从 t=9 起，共51行
    r0, r1 = rows[0], rows[1]
    assert r0["date"] == raw[9]["d"] and r0["sym"] == "RB"
    expect_ret = raw[10]["c"] / raw[9]["c"] - 1
    assert abs(r1["ret1d"] - expect_ret) < 1e-12
    # 技术特征来自 compute_indicators 同函数（ma5 非空、tsmom 键存在）
    assert r1["ma5"] is not None and "tsmom252" in r1

    # 3) 无未来函数：扰动 t 之后的全部价格，t 及之前的行逐值不变（结构性PIT）
    rows_a, _ = build_symbol_rows("RB", "黑色", raw, warmup=10)
    pert = [dict(b) for b in raw]
    for k in range(40, 60):                        # 篡改第40根以后
        pert[k]["c"] *= 1.5
        pert[k]["h"] *= 1.5
    rows_b, _ = build_symbol_rows("RB", "黑色", pert, warmup=10)
    for ra, rb in zip(rows_a[:31], rows_b[:31]):   # t=9..39 共31行必须完全一致
        for col in (["ret1d"] + FEATURE_COLS):
            assert ra[col] == rb[col], ("未来函数泄漏", ra["date"], col, ra[col], rb[col])

    # 4) 基本面严格 as-of：当日基本面不得进当日行，只能用前一日（日期取在暖机后的面板区间内）
    funds = [("2026-01-15", 0.5, 0.01, 0.0), ("2026-01-20", -0.5, -0.01, 0.0)]
    # 构造日期覆盖 2026-01 的bars（面板从第10根 01-10 起）
    bars = [{"d": "2026-01-%02d" % (i + 1), "o": 100 + i, "h": 101 + i, "l": 99 + i,
             "c": 100.5 + i, "v": 1, "p": 10, "s": 100.5 + i} for i in range(28)]
    fr, _ = build_symbol_rows("X", "有色", bars, funds, warmup=10)
    by_date = {r["date"]: r for r in fr}
    # 2026-01-15 当天：strict 只能取 <01-15 → 无 → None（防当日基本面偷看）
    assert by_date["2026-01-15"]["fund_score"] is None
    # 2026-01-16 可取到 01-15 的 0.5
    assert by_date["2026-01-16"]["fund_score"] == 0.5
    # 01-20 当天仍只能取到前一条 0.5；01-21 起取到更近的 -0.5
    assert by_date["2026-01-20"]["fund_score"] == 0.5
    assert by_date["2026-01-21"]["fund_score"] == -0.5

    # 5) 训练-服务一致性：面板第 t 行特征 == 对同一前缀实时 compute_indicators 逐值一致
    raw5 = _synthetic_bars(50)
    pr, _ = build_symbol_rows("RB", "黑色", raw5, warmup=10)
    for idx in (0, 10, 25, 40):
        t = idx + 9
        live = futures_data.compute_indicators(raw5[:t + 1])
        panel_row = pr[idx]
        for k in FEATURE_COLS:
            a, b = panel_row[k], _num(live.get(k))
            if a is None:
                assert b is None
            else:
                assert abs(a - b) < 1e-12, (idx, k, a, b)

    # 6) PanelStore 幂等重建两次逐值一致、主键去重、manifest 落表、回读一致（临时库）
    with tempfile.TemporaryDirectory() as td:
        dbp = os.path.join(td, "p.db")
        st = PanelStore(dbp)
        rb_rows, roll = build_symbol_rows("RB", "黑色", _synthetic_bars(50), warmup=10)
        n1 = st.replace_symbol("RB", rb_rows)
        rows_back_1 = st.load_rows("RB")
        n2 = st.replace_symbol("RB", rb_rows)          # 再建一次
        rows_back_2 = st.load_rows("RB")
        assert n1 == n2 == len(rb_rows) == len(rows_back_1) == len(rows_back_2)
        assert rows_back_1 == rows_back_2              # 幂等逐值一致
        assert st.count("RB") == len(rb_rows)          # 主键去重不翻倍
        st.record_run(["RB"], 50, 1, n1, rb_rows[0]["date"], rb_rows[-1]["date"], roll)
        assert len(st.manifests()) == 1
        # 回读字段一致
        assert abs(rows_back_1[5]["ret1d"] - rb_rows[5]["ret1d"]) < 1e-12
        st.close()

    # 7) 特征注册表自检联动
    assert not fc.validate()

    # 8) G21续：面板回读==建面板时的已复权bar；且对回读序列再复权为恒等（不再误判换月）
    raw8 = _synthetic_bars(80)
    adj8, adj_roll = backtest.ratio_adjusted_bars(raw8)
    rows8, _ = build_symbol_rows("RB", "黑色", raw8, warmup=10)
    recon8 = panel_rows_to_bars(rows8)
    assert len(recon8) == len(rows8)
    # 面板行存的 c 就是已复权 c；回读逐值一致（对齐暖机后的 adj8）
    for rb_row, bar in zip(rows8, recon8):
        assert bar["d"] == rb_row["date"] and abs(bar["c"] - rb_row["c"]) < 1e-12
        assert bar["p"] == rb_row["oi"]
    # 回读序列（已复权）再跑一次复权：不产生新换月、收盘价不变（幂等，SC/J 类误判在合成平滑序列上为0）
    re_adj, re_roll = backtest.ratio_adjusted_bars(recon8)
    assert re_roll == 0
    for a, b in zip(recon8, re_adj):
        assert abs(a["c"] - b["c"]) < 1e-12
    # load_adjusted_bars 面板路径回读==网络路径复权（同输入、临时库）
    with tempfile.TemporaryDirectory() as td:
        dbp = os.path.join(td, "p.db")
        st = PanelStore(dbp); st.replace_symbol("RB", rows8); st.close()
        pb_bars, src = load_adjusted_bars("RB0", 1023, prefer_panel=True, db_path=dbp)
        assert src == "panel" and len(pb_bars) == len(rows8)
        assert abs(pb_bars[-1]["c"] - adj8[-1]["c"]) < 1e-9
        # 缺品种时面板路径软回退到网络（不编造）；此处断网会抛，故只验证缺库返回 network 分支不命中面板
    print("panel_builder selftest ALL PASS（asof边界/暖机ret1d/未来扰动PIT/基本面严格asof/"
          "训练服务一致/PanelStore幂等/manifest/注册表联动/面板回读不二次复权 共8组）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
