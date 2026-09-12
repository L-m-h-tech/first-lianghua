# -*- coding: utf-8 -*-
r"""成交量 PCR 采集器 tools/pcr_vol_collector.py（第132轮，研究侧）。

补齐期权成交量 PCR 数据源（option_chains.pcr_vol 预留字段的独立日表实现）：
持仓量 PCR 是慢变量，成交量 PCR 捕捉**日内情绪变化**——数据先入库，
是否做成因子等 PCR 持仓量影子体检（第129轮）决策依据到位后再议（用户拍板：先建能力、等证据）。

数据源：天勤 TqSdk（E:\LHsystem\vendor，.env 快期账户）——
  query_options(underlying) 枚举期权系列全部合约 → 逐合约日K volume → 按日合计
  call_vol / put_vol → pcr_vol = put_vol / call_vol。
合约月：取 monitor.db option_chains（cycle>=1 量化新浪T链）每品种最新 expiry——
与 PCR 持仓量影子体检同一条链，保证后续两口径可直接对照。

落库：monitor.db 新表（幂等建表，独立于 option_chains 快照语义）：
  option_pcr_vol(sym, trade_date, call_vol, put_vol, pcr_vol, n_calls, n_puts, source,
                 created_real, PRIMARY KEY(sym, trade_date))
纪律：只读行情、只写本表；任一品种失败不拖垮整体；未到决策门**不接任何因子/综合分**。

三源协同（第133轮，加快获取速度）：
  1. AKShare 交易所日行情（option_hist_{shfe,czce,dce,gfex}）——品种级×日期级批量接口，
     一次调用覆盖该品种当日全部合约（快源；INE 无接口、DCE 接口偶发挂→自动落天勤）；
  2. 天勤 TqSdk 逐合约日K——只补 AKShare 未覆盖的 (品种,日期) 缺口（慢源，精度兜底）；
  3. 交易所官网直取（上期所/广期所直连、郑商所/大商所 scrapling 绕412）——留作冗余第二源。
source 字段记录实际来源（akshare/tqsdk），便于后续口径对照。

CLI: python tools/pcr_vol_collector.py [--backfill 15] [--syms RB,MA,SR] [--limit 0]
     [--selftest]
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config                     # noqa: E402  （导入即 load_dotenv：TQ_ACCOUNT/TQ_PASSWORD）

# vendor 路径（与 backup_sources 同款候选）
for _v in (os.path.join(os.path.dirname(os.path.dirname(config.BASE_DIR)), "vendor"),
           os.path.join(os.path.dirname(config.BASE_DIR), "vendor")):
    if _v and os.path.isdir(_v) and _v not in sys.path:
        sys.path.insert(0, _v)

_BJT = timezone(timedelta(hours=8))
_PCR_VOL_DDL = """
CREATE TABLE IF NOT EXISTS option_pcr_vol(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sym TEXT NOT NULL, trade_date TEXT NOT NULL,
    call_vol REAL, put_vol REAL, pcr_vol REAL,
    n_calls INTEGER, n_puts INTEGER, source TEXT,
    created_real REAL, UNIQUE(sym, trade_date)
);
"""


def _q(db, sql, args=()):
    """只读查询 helper（打开 file:...?mode=ro）。"""
    conn = sqlite3.connect("file:%s?mode=ro" % db.replace(chr(92), "/"), uri=True)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _ns_to_date(ns):
    """天勤K线 datetime（ns）→ 北京时区日期字符串 YYYY-MM-DD。"""
    if ns is None:
        return None
    return datetime.fromtimestamp(float(ns) / 1e9, _BJT).strftime("%Y-%m-%d")


def _tq_symbols(ex, sym, expiry):
    """品种+到期月(YYMM) → 天勤合约符号候选列表（按序尝试，首个查询成功者生效）。

    郑商所历史为 3 位月（SR611），近年新挂合约亦见 4 位（MA2611）——双格式都试；
    产品代码用大写（郑商所官方合约代码口径 SR611C5600），小写作为兜底。"""
    sym_u, sym_l = sym.upper(), sym.lower()
    ex = str(ex).upper()
    if ex == "CZCE":
        return ["CZCE.%s%s" % (sym_u, str(expiry)[1:]), "CZCE.%s%s" % (sym_u, expiry),
                "CZCE.%s%s" % (sym_l, expiry)]
    return ["%s.%s%s" % (ex, sym_l, expiry)]


def _retry(fn, attempts=3, delays=(3, 6)):
    """天勤合约服务/行情查询重试（周六晚例行维护窗口服务不稳；TqTimeoutError 常见）。"""
    import time as _time
    last = None
    for k in range(attempts):
        try:
            return fn()
        except Exception as e:                    # noqa: BLE001
            last = e
            if k < attempts - 1:
                _time.sleep(delays[min(k, len(delays) - 1)])
    raise last


# ---------------- AKShare 交易所快源（第133轮） ----------------
_EX_FN = {"SHFE": "option_hist_shfe", "CZCE": "option_hist_czce",
          "DCE": "option_hist_dce", "GFEX": "option_hist_gfex"}
_AK_NAME_FIX = {"热卷": "热轧卷板"}          # config 中文名与交易所期权品种名的实测差异


def _ak_option_name(sym):
    """sym → akshare option_hist_* 的 symbol 参数（中文品种名+期权）。"""
    for vn, vc in config.VARIETIES.items():
        if str(vc.get("sym", "")).upper() == sym.upper():
            vn = _AK_NAME_FIX.get(vn, vn)
            return vn + "期权"
    return None


def _parse_contract(code):
    """期权合约代码 → (品种前缀大写, C/P, 行权价)；不匹配返 None。
    兼容 rb2610C2600 / MA610C2100 / m2611-C-3100（大商所连字符式）。"""
    import re
    m = re.match(r"^([A-Za-z]{1,3})(\d{3,4})[-]?([CP])[-]?(\d+(\.\d+)?)$", str(code or "").strip())
    if not m:
        return None
    return m.group(1).upper(), m.group(3), float(m.group(4))


def _ak_vol_column(df):
    """在日行情 df 里找成交量列（各交易所列名不同：成交量/成交量(手)...）。"""
    for c in df.columns:
        if "成交量" in str(c):
            return c
    return None


def akshare_variety_day(sym, ex, date_ymd):
    """AKShare 交易所日行情：单品种单日全合约成交量合计 → (call_vol, put_vol) 或 None。

    一次接口调用覆盖该品种当日全部合约（快源）；接口缺失/调用失败/解析失败返 None
    （调用方落天勤补位）。INE 无 akshare 接口直接 None。"""
    fn_name = _EX_FN.get(str(ex).upper())
    if not fn_name:
        return None
    try:
        import akshare as ak
        fn = getattr(ak, fn_name, None)
        if fn is None:
            return None
        opt_name = _ak_option_name(sym)
        if not opt_name:
            return None
        df = fn(symbol=opt_name, trade_date=date_ymd.replace("-", ""))
        if df is None or df.empty:
            return None
        vol_col = _ak_vol_column(df)
        if vol_col is None:
            return None
        call = put = 0.0
        hit = False
        for _, row in df.iterrows():
            parsed = _parse_contract(row.get(df.columns[0]))
            if not parsed or parsed[0] != sym.upper():
                continue
            v = row.get(vol_col)
            v = float(v) if v not in (None, "") else 0.0
            hit = True
            if parsed[1] == "C":
                call += v
            else:
                put += v
        return (call, put) if hit else None
    except Exception:
        return None


def _kline_daily_volumes(api, opt_id, days, today=None):
    """单期权合约日K → {date: volume}（只保留有成交记录、且 ≤ 北京今天 的日期）。

    周末/节假日时 TqSdk 日线会附带**下一交易日的空壳占位行**（datetime=未来交易日 00:00、
    volume=0、close 沿用旧值）——不过滤会把未来日期写进库（2026-09-12 周六实测混入 09-14）。"""
    kl = api.get_kline_serial(opt_id, duration_seconds=86400, data_length=days + 5)
    today = today or datetime.now(_BJT).strftime("%Y-%m-%d")
    out = {}
    for _, row in kl.iterrows():
        d = _ns_to_date(row.get("datetime"))
        v = row.get("volume")
        if d and d <= today and v is not None and float(v) > 0:
            out[d] = out.get(d, 0.0) + float(v)
    return out


def collect_variety(api, sym, ex, expiry, days, source="tqsdk", today=None):
    """单品种：枚举 C/P 合约 → 逐合约日K 成交量 → 按日合计 → PCR 日序列。
    返回 [(sym, date, call_vol, put_vol, pcr_vol, n_c, n_p)]；失败抛异常由调用方记入 errors。"""
    under_used = None
    ids_c = ids_p = None
    for under in _tq_symbols(ex, sym, expiry):
        try:
            ids_c = _retry(lambda: api.query_options(under, option_class="CALL") or [])
            ids_p = _retry(lambda: api.query_options(under, option_class="PUT") or [])
        except Exception:
            continue
        if ids_c and ids_p:
            under_used = under
            break
    if not (ids_c and ids_p):
        raise RuntimeError("query_options 全候选失败: %s" % "/".join(_tq_symbols(ex, sym, expiry)))
    call_daily, put_daily = {}, {}
    for oid in ids_c:
        for d, v in _retry(lambda: _kline_daily_volumes(api, oid, days, today=today)).items():
            call_daily[d] = call_daily.get(d, 0.0) + v
    for oid in ids_p:
        for d, v in _retry(lambda: _kline_daily_volumes(api, oid, days, today=today)).items():
            put_daily[d] = put_daily.get(d, 0.0) + v
    out = []
    for d in sorted(set(call_daily) | set(put_daily))[-days:]:
        cv, pv = call_daily.get(d, 0.0), put_daily.get(d, 0.0)
        out.append({"sym": sym, "trade_date": d, "call_vol": cv, "put_vol": pv,
                    "pcr_vol": round(pv / cv, 4) if cv > 0 else None,
                    "n_calls": len(ids_c), "n_puts": len(ids_p),
                    "source": source})
    return out


def write_rows(db, rows):
    if not rows:
        return 0
    conn = sqlite3.connect(db)
    try:
        conn.executescript(_PCR_VOL_DDL)
        n = 0
        for r in rows:
            conn.execute("""INSERT OR REPLACE INTO option_pcr_vol
                (sym, trade_date, call_vol, put_vol, pcr_vol, n_calls, n_puts, source, created_real)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (r["sym"], r["trade_date"], r["call_vol"], r["put_vol"], r["pcr_vol"],
                 r["n_calls"], r["n_puts"], r["source"], datetime.now().timestamp()))
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def latest_expiries(monitor_db, syms=None):
    """option_chains(cycle>=1) 每品种最新到期月 → {sym: expiry}。"""
    conn = sqlite3.connect("file:%s?mode=ro" % monitor_db.replace(chr(92), "/"), uri=True)
    try:
        rows = conn.execute("""SELECT sym, expiry, MAX(ts) FROM option_chains
                               WHERE cycle>=1 GROUP BY sym ORDER BY sym""").fetchall()
    finally:
        conn.close()
    out = {}
    for sym, expiry, _ in rows:
        if syms and sym not in syms:
            continue
        if expiry and str(expiry).isdigit() and len(str(expiry)) == 4:
            out[sym] = str(expiry)
    return out


def recent_trading_days(monitor_db, n):
    """minute_bars(period=60) 的最近 n 个交易日（只读、离线——有 60m bar 的日必是交易日）。"""
    rows = _q(monitor_db, """SELECT DISTINCT substr(bar_dt,1,10) d FROM minute_bars
                             WHERE period=60 ORDER BY d DESC LIMIT ?""", (n,))
    return sorted(r[0] for r in rows)


def _default_tq_api():
    from tqsdk import TqApi, TqAuth
    acc = (os.environ.get("TQ_ACCOUNT"), os.environ.get("TQ_PASSWORD"))
    return TqApi(auth=TqAuth(*acc), disable_print=True) if all(acc) else TqApi(disable_print=True)


def _ak_day(sym, ex, date_ymd):
    """AKShare 快源单品种单日；None=未覆盖（调用方落天勤）。测试可注入替换。"""
    return akshare_variety_day(sym, ex, date_ymd)


def run(backfill=1, syms=None, limit=0, monitor_db=None, mode="fast",
        api_factory=None, ak_day_fn=None):
    """成交量 PCR 采集。mode:
      - "fast"（默认，第133轮协同）：AKShare 交易所日行情批量快源（品种级×日期级，
        一次调用覆盖全合约）→ 未覆盖的 (品种,日期) 缺口由天勤逐合约补齐；
      - "tqsdk"：旧路径，全品种逐合约日K（慢但独立于 akshare）。
    api_factory/ak_day_fn 可注入（测试用）；返回统计 dict。"""
    monitor_db = monitor_db or config.MONITOR_DB
    expiries = latest_expiries(monitor_db, syms)
    if not expiries:
        raise SystemExit("option_chains 无可用 (sym, expiry)——先让主链积累期权链数据")
    if limit > 0:
        expiries = dict(list(expiries.items())[:limit])
    # sym(大写) -> 交易所：VARIETIES 键为中文品种名，需按值反查
    ex_of = {str(vc.get("sym", "")).upper(): str(vc.get("ex", "")).upper()
             for vc in config.VARIETIES.values()}
    all_rows, errors = [], {}

    if mode == "tqsdk":
        api = (api_factory or _default_tq_api)()
        try:
            for sym, expiry in expiries.items():
                try:
                    rows = collect_variety(api, sym, ex_of.get(sym, ""), expiry, backfill)
                    all_rows.extend(rows)
                    last = rows[-1] if rows else {}
                    print("  %-4s %s: %d 日（最新 %s pcr_vol=%s）" % (
                        sym, expiry, len(rows), last.get("trade_date"), last.get("pcr_vol")))
                except Exception as e:
                    errors[sym] = "%s: %s" % (type(e).__name__, str(e)[:400])
                    print("  %-4s %s: 失败 [%s] %s" % (sym, expiry, type(e).__name__, str(e)[:300]))
        finally:
            api.close()
        n = write_rows(monitor_db, all_rows)
        return {"written": n, "mode": mode, "n_syms": len(expiries),
                "ok_syms": len(expiries) - len(errors), "errors": errors, "rows": all_rows}

    # ---- fast 协同模式（第133轮） ----
    from concurrent.futures import ThreadPoolExecutor
    dates = recent_trading_days(monitor_db, backfill)
    if not dates:
        raise SystemExit("minute_bars 无交易日可推——先让主链积累分钟数据")
    print("快模式: %d 个交易日 × %d 品种，AKShare 批量优先、天勤补缺口" % (len(dates), len(expiries)))

    ak_day = ak_day_fn or _ak_day
    pairs = [(d, sym) for d in dates for sym in expiries]
    ak_covered = {}                       # (sym, date) -> {call,put}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for (d, sym), r in zip(pairs, pool.map(lambda p: ak_day(p[1], ex_of.get(p[1], ""), p[0]), pairs)):
            if r is not None:
                ak_covered[(sym, d)] = {"call_vol": r[0], "put_vol": r[1],
                                        "pcr_vol": round(r[1] / r[0], 4) if r[0] > 0 else None,
                                        "sym": sym, "trade_date": d, "source": "akshare",
                                        "n_calls": None, "n_puts": None}
    print("  AKShare 覆盖 %d/%d 个 (品种,日期) 对" % (len(ak_covered), len(pairs)))

    # 天勤补缺口：按品种分组（一次 collect_variety 拿全日期，只写缺失对）
    missing_by_sym = defaultdict(set)
    for d in dates:
        for sym in expiries:
            if (sym, d) not in ak_covered:
                missing_by_sym[sym].add(d)
    tq_rows, n_tq = [], 0
    if missing_by_sym:
        api = (api_factory or _default_tq_api)()
        try:
            for sym, want in sorted(missing_by_sym.items()):
                try:
                    rows = collect_variety(api, sym, ex_of.get(sym, ""), expiries[sym],
                                           max(backfill, len(want) + 5),
                                           today=max(dates))
                    for r in rows:
                        if r["trade_date"] in want:
                            r["source"] = "tqsdk"
                            tq_rows.append(r)
                            n_tq += 1
                    print("  %-4s %s: 天勤补 %d/%d 日" % (
                        sym, expiries[sym], min(n_tq, len(want)), len(want)))
                except Exception as e:
                    errors[sym] = "%s: %s" % (type(e).__name__, str(e)[:400])
                    print("  %-4s %s: 天勤补位失败 [%s] %s" % (
                        sym, expiries[sym], type(e).__name__, str(e)[:200]))
        finally:
            api.close()
    n = write_rows(monitor_db, list(ak_covered.values()) + tq_rows)
    return {"written": n, "mode": mode, "n_syms": len(expiries),
            "ak_pairs": len(ak_covered), "tq_rows": n_tq,
            "ok_syms": len(expiries) - len(errors), "errors": errors,
            "dates": dates}


def render(res):
    L = ["成交量 PCR 采集（第132轮建能力 / 第133轮三源协同——不接因子/综合分）", "=" * 60,
         "生成: %s ｜ 模式 %s ｜ 品种 %d（成功 %d，失败 %d）｜ 入库 %d 行" % (
             res["generated"], res["mode"], res["n_syms"], res["ok_syms"],
             len(res["errors"]), res["written"])]
    if res["mode"] == "fast":
        L.append("AKShare 批量覆盖 %d 个 (品种,日期) ｜ 天勤补缺口 %d 行" % (
            res.get("ak_pairs", 0), res.get("tq_rows", 0)))
    if res["errors"]:
        L.append("失败: " + "; ".join("%s:%s" % (k, v[:50]) for k, v in
                                      list(res["errors"].items())[:8]))
    L.append("用途：成交量 PCR=日内情绪（持仓量 PCR=慢变量）；是否做成因子等第129轮 PCR 影子")
    L.append("体检决策依据到位后由用户拍板——本采集器只保证数据在库（option_pcr_vol 表）。")
    return "\n".join(L)


def selftest():
    """零网络合成断言：ns→日期/郑商所符号/日合计与 PCR/落库幂等。"""
    import tempfile
    # ① ns → 北京日期
    assert _ns_to_date(1788970000 * 1e9)[:10] >= "2026-09"
    # 周末占位行过滤：未来日期的零量行不得入库（2026-09-12 周六实测混入 09-14）
    class _Api:
        def query_options(self, under, option_class=None):
            return ["X"]
        def get_kline_serial(self, oid, duration_seconds, data_length):
            import pandas as _pd
            return _pd.DataFrame([
                {"datetime": 1789056000 * 1e9, "volume": 8.0},    # 09-11（周五，真实）
                {"datetime": 1789342400 * 1e9, "volume": 0.0},    # 09-14（周一，未来占位）
            ])
    rows = collect_variety(_Api(), "RB", "SHFE", "2611", days=5, today="2026-09-12")
    assert all(r["trade_date"] <= "2026-09-12" for r in rows), rows
    assert len(rows) == 1 and rows[0]["call_vol"] == 8.0
    # ② 合约符号
    assert _tq_symbols("SHFE", "RB", "2611") == ["SHFE.rb2611"]
    assert _tq_symbols("CZCE", "SR", "2611")[0] == "CZCE.SR611"   # 3位月优先
    assert _tq_symbols("CZCE", "SR", "2611")[1] == "CZCE.SR2611"  # 4位月候选
    assert _tq_symbols("DCE", "m", "2611") == ["DCE.m2611"]
    # ③ 按日合计 + PCR（合成 kline 行）
    class _FakeApi:
        def query_options(self, under, option_class=None):
            calls = ["SHFE.rb2611C100"]
            puts = ["SHFE.rb2611P100"]
            return puts if option_class == "PUT" else calls
        def get_kline_serial(self, oid, duration_seconds, data_length):
            import pandas as pd
            data = {"SHFE.rb2611C100": [(1788900000 * 1e9, 10.0), (1788986400 * 1e9, 20.0)],
                    "SHFE.rb2611P100": [(1788900000 * 1e9, 5.0), (1788986400 * 1e9, 0.0)]}
            import pandas as _pd
            rows = data[oid]
            return _pd.DataFrame([{"datetime": d, "volume": v} for d, v in rows])
    rows = collect_variety(_FakeApi(), "RB", "SHFE", "2611", days=5, source="tqsdk")
    by_d = {r["trade_date"]: r for r in rows}
    assert len(rows) == 2 and rows[0]["call_vol"] == 10.0 and rows[0]["put_vol"] == 5.0
    assert abs(rows[0]["pcr_vol"] - 0.5) < 1e-9
    assert rows[1]["pcr_vol"] is None or rows[1]["pcr_vol"] == 0.0   # call=0 → None
    # ④ 落库幂等（tmp 库）
    tmp = os.path.join(tempfile.mkdtemp(prefix="pcrvol_"), "m.db")
    assert write_rows(tmp, rows) == 2
    assert write_rows(tmp, rows) == 2                                 # REPLACE 幂等
    conn = sqlite3.connect(tmp)
    n = conn.execute("SELECT COUNT(*) FROM option_pcr_vol").fetchone()[0]
    conn.close()
    assert n == 2
    # ⑤ fast 协同编排：AKShare 只覆盖 RB → MA 落天勤补位（fake 注入，零网络）
    def fake_ak(sym, ex, date_ymd):
        return (10.0, 5.0) if sym == "RB" else None

    class _TqApi:
        def close(self):
            pass

        def query_options(self, under, option_class=None):
            return ["X.c"] if option_class == "CALL" else ["X.p"]

        def get_kline_serial(self, oid, duration_seconds, data_length):
            import pandas as _pd
            return _pd.DataFrame([{"datetime": 1789056000 * 1e9, "volume": 4.0},
                                  {"datetime": 1789342400 * 1e9, "volume": 0.0}])

    tmp2 = os.path.join(tempfile.mkdtemp(prefix="pcrvol2_"), "m2.db")
    conn = sqlite3.connect(tmp2)
    conn.executescript(_PCR_VOL_DDL)               # 先建表：latest_expiries 以只读打开，文件须存在
    conn.execute("CREATE TABLE option_chains(id INTEGER PRIMARY KEY, ts TEXT, cycle INTEGER,"
                 " sym TEXT, expiry TEXT, put_oi REAL, call_oi REAL, pcr_oi REAL)")
    conn.execute("CREATE TABLE minute_bars(id INTEGER PRIMARY KEY, sym TEXT, period INTEGER,"
                 " bar_dt TEXT, c REAL)")
    for d in ("2026-09-10", "2026-09-11"):
        for s in ("RB", "MA"):
            conn.execute("INSERT INTO minute_bars(sym,period,bar_dt,c) VALUES(?,60,?,1)",
                         (s, d + " 23:00"))
    conn.execute("INSERT INTO option_chains(ts,cycle,sym,expiry,put_oi,call_oi,pcr_oi)"
                 " VALUES('2026-09-11 23:00:03',1,'RB','2611',1,2,0.5),"
                 "('2026-09-11 23:00:03',1,'MA','2611',1,2,0.5)")
    conn.commit()
    conn.close()
    res = run(backfill=2, monitor_db=tmp2, mode="fast", api_factory=_TqApi, ak_day_fn=fake_ak)
    assert res["ak_pairs"] == 2, res          # RB 两天被 AKShare 覆盖
    assert res["tq_rows"] == 1, res           # MA 09-11 落天勤；09-10 天勤无数据 → 诚实缺口不编造
    conn = sqlite3.connect(tmp2)
    srcs = dict(conn.execute("SELECT sym, source FROM option_pcr_vol"))
    dates_ma = [r[0] for r in conn.execute("SELECT trade_date FROM option_pcr_vol WHERE sym='MA'")]
    conn.close()
    assert srcs == {"RB": "akshare", "MA": "tqsdk"}, srcs
    assert dates_ma == ["2026-09-11"]
    print("pcr_vol_collector selftest OK")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="成交量 PCR 采集器（天勤TqSdk，研究侧）")
    ap.add_argument("--backfill", type=int, default=1,
                    help="fast模式默认1=增量最新交易日；tqsdk模式建议15")
    ap.add_argument("--mode", choices=("fast", "tqsdk"), default="fast",
                    help="fast=AKShare批量+天勤补缺口（默认）；tqsdk=全品种逐合约日K（旧路径）")
    ap.add_argument("--syms", default="", help="品种逗号分隔；缺省=option_chains 全部 sym")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个品种（0=全部）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    syms = set(s.strip().upper() for s in args.syms.split(",") if s.strip()) or None
    res = run(backfill=args.backfill, syms=syms, limit=args.limit, mode=args.mode)
    res.pop("rows")
    res["generated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(render(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main() if len(sys.argv) > 1 else selftest())
