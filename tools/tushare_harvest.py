# -*- coding: utf-8 -*-
r"""G18续（第90轮）Tushare 限时 token 一次性收割：把可留存的资产全部落库（断点续传、软降级）。

实测结论（决定收割范围）：
  - trade_cal：代理返回**全历史**（1990~2026）→ 永久交易日历（T1 完整达成）；
  - fut_basic：全量合约元数据（乘数/交易所/名称）→ 永久合约表（T3 部分）；
  - 其余接口被代理锁单日：fut_daily/ft_limit/fut_settle/fut_mapping/fut_wsr 恒返回最新交易日、
    fut_holding 锁远古 → **只能当日快照**，历史回填不可行；
  - 当日快照的价值=校准：ft_limit.m_ratio（官方保证金率）核对 data/futures_margins.csv、
    fut_daily.settle（结算价）核对回测结算口径。

落库 cache/tushare_harvest.db（幂等 upsert，token 失效已拉的不丢）：
  tushare_cal(8年历史) / fut_basic(全量) / snap_daily / snap_limit / snap_settle / snap_wsr

用法：
  D:\Python\python.exe tools\tushare_harvest.py --cal    # 只拉交易日历（最重要，先做）
  D:\Python\python.exe tools\tushare_harvest.py --selftest
  D:\Python\python.exe tools\tushare_harvest.py          # 全收割（cal+basic+当日快照）
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tushare_client as tc            # noqa: E402  适配层（token 走 env）

HARVEST_DB = ROOT / "cache" / "tushare_harvest.db"
REPORT_TXT = ROOT / "reports" / "tushare_harvest.txt"
CAL_START = "20180101"
CAL_END = "20301231"
EXCHANGES = ("SHFE", "INE", "DCE", "CZCE", "CFFEX", "GFEX")


def _conn():
    con = sqlite3.connect(str(HARVEST_DB))
    con.execute("CREATE TABLE IF NOT EXISTS tushare_cal("
                "exchange TEXT,cal_date TEXT,is_open INT,pretrade_date TEXT,"
                "PRIMARY KEY(exchange,cal_date))")
    con.execute("CREATE TABLE IF NOT EXISTS fut_basic("
                "ts_code TEXT PRIMARY KEY,symbol TEXT,exchange TEXT,name TEXT,"
                "fut_code TEXT,multiplier REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS snap_daily("
                "ts_code TEXT,trade_date TEXT,pre_close REAL,pre_settle REAL,"
                "open REAL,high REAL,low REAL,close REAL,settle REAL,"
                "PRIMARY KEY(ts_code,trade_date))")
    con.execute("CREATE TABLE IF NOT EXISTS snap_limit("
                "trade_date TEXT,ts_code TEXT,name TEXT,up_limit REAL,down_limit REAL,"
                "m_ratio REAL,PRIMARY KEY(trade_date,ts_code))")
    con.execute("CREATE TABLE IF NOT EXISTS snap_settle("
                "ts_code TEXT,trade_date TEXT,settle REAL,PRIMARY KEY(ts_code,trade_date))")
    con.execute("CREATE TABLE IF NOT EXISTS snap_wsr("
                "trade_date TEXT,symbol TEXT,fut_name TEXT,warehouse TEXT,"
                "pre_vol REAL,vol REAL,PRIMARY KEY(trade_date,symbol,warehouse))")
    con.commit()
    return con


def _upsert(con, table, cols, rows):
    """幂等 upsert；rows=[tuple,...] 按 cols 对齐。返回写入条数。"""
    if not rows:
        return 0
    ph = ",".join("?" * len(cols))
    con.executemany("INSERT OR REPLACE INTO %s(%s) VALUES(%s)" % (table, ",".join(cols), ph),
                    rows)
    con.commit()
    return len(rows)


def harvest_cal(con):
    """trade_cal 8年历史 → tushare_cal（幂等；已落>5000 条则跳过）。"""
    if con.execute("SELECT COUNT(*) FROM tushare_cal").fetchone()[0] > 5000:
        return 0
    rows = tc.call("trade_cal", start_date=CAL_START, end_date=CAL_END)
    if not rows:
        return 0
    data = [(r.get("exchange"), r.get("cal_date"), int(r.get("is_open") or 0),
             r.get("pretrade_date")) for r in rows if r.get("cal_date") >= CAL_START]
    return _upsert(con, "tushare_cal", ("exchange", "cal_date", "is_open", "pretrade_date"), data)


def harvest_basic(con):
    """fut_basic 六交易所全量 → fut_basic 表（幂等）。"""
    n = 0
    for ex in EXCHANGES:
        rows = tc.call("fut_basic", exchange=ex, limit=9000)
        if not rows:
            continue
        data = [(r.get("ts_code"), r.get("symbol"), r.get("exchange"), r.get("name"),
                 r.get("fut_code"), r.get("multiplier")) for r in rows]
        n += _upsert(con, "fut_basic", ("ts_code", "symbol", "exchange", "name",
                                        "fut_code", "multiplier"), data)
    return n


def harvest_snapshot(con):
    """当日快照（fut_daily/ft_limit/fut_settle/fut_wsr）→ snap_* 表。返回 {表: 条数}。"""
    stats = {}
    rows = tc.call("fut_daily")
    if rows:
        stats["snap_daily"] = _upsert(con, "snap_daily",
            ("ts_code", "trade_date", "pre_close", "pre_settle", "open", "high", "low",
             "close", "settle"),
            [(r.get("ts_code"), r.get("trade_date"), r.get("pre_close"), r.get("pre_settle"),
              r.get("open"), r.get("high"), r.get("low"), r.get("close"), r.get("settle"))
             for r in rows])
    rows = tc.call("ft_limit")
    if rows:
        stats["snap_limit"] = _upsert(con, "snap_limit",
            ("trade_date", "ts_code", "name", "up_limit", "down_limit", "m_ratio"),
            [(r.get("trade_date"), r.get("ts_code"), r.get("name"),
              r.get("up_limit"), r.get("down_limit"), r.get("m_ratio")) for r in rows])
    rows = tc.call("fut_settle")
    if rows:
        stats["snap_settle"] = _upsert(con, "snap_settle",
            ("ts_code", "trade_date", "settle"),
            [(r.get("ts_code"), r.get("trade_date"), r.get("settle")) for r in rows])
    rows = tc.call("fut_wsr")
    if rows:
        stats["snap_wsr"] = _upsert(con, "snap_wsr",
            ("trade_date", "symbol", "fut_name", "warehouse", "pre_vol", "vol"),
            [(r.get("trade_date"), r.get("symbol"), r.get("fut_name"), r.get("warehouse"),
              r.get("pre_vol"), r.get("vol")) for r in rows])
    return stats


def selftest():
    """纯逻辑：_upsert 幂等（合成表）+ 依赖 tushare_client 4组。零网络。"""
    assert tc.selftest() == 0
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t(x TEXT PRIMARY KEY, v REAL)")
    assert _upsert(con, "t", ("x", "v"), [("a", 1.0), ("b", 2.0)]) == 2
    assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    # 幂等：重复 upsert 不增行
    assert _upsert(con, "t", ("x", "v"), [("a", 9.0)]) == 1
    assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    assert con.execute("SELECT v FROM t WHERE x='a'").fetchone()[0] == 9.0
    con.close()
    print("tushare_harvest selftest ALL PASS（依赖 tushare_client 4组 + upsert 幂等 共5组）")
    return 0


def run(verbose=True):
    con = _conn()
    counts = {"tushare_cal": harvest_cal(con),
              "fut_basic": harvest_basic(con)}
    snap = harvest_snapshot(con)
    counts.update(snap)
    # 表内计数（含上次已落、断点续传场景）
    for t in ("tushare_cal", "fut_basic", "snap_daily", "snap_limit", "snap_settle", "snap_wsr"):
        counts[t] = con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
    con.close()
    lines = [
        "=" * 88,
        " G18续 限时 token 收割报告（可留存资产落库）  生成于 %s"
        % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "=" * 88,
        "tushare_cal=%d（8年交易日历,永久可用） fut_basic=%d（合约元数据,永久）" % (
            counts["tushare_cal"], counts["fut_basic"]),
        "snap_daily=%d snap_limit=%d snap_settle=%d snap_wsr=%d（当日快照,校准用）" % (
            counts["snap_daily"], counts["snap_limit"], counts["snap_settle"], counts["snap_wsr"]),
        "用途：tushare_cal 校验手工节假日表/交易日历；fut_basic 减硬编码乘数；snap_limit.m_ratio 核对",
        "      data/futures_margins.csv 保证金率；snap_daily.settle 核对回测结算价口径。",
        "诚实边界：其余接口被代理锁单日（历史回填不可行）；token 失效后已落库数据仍可用。",
        "=" * 88,
    ]
    text = "\n".join(lines)
    if verbose:
        print(text)
    os.makedirs(os.path.dirname(str(REPORT_TXT)), exist_ok=True)
    with open(REPORT_TXT, "w", encoding="utf-8", newline="\n") as f:
        f.write(text + "\n")
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description="G18续 限时 token 一次性收割")
    ap.add_argument("--cal", action="store_true", help="只拉交易日历（最重要，先做）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.cal:
        con = _conn()
        n = harvest_cal(con)
        total = con.execute("SELECT COUNT(*) FROM tushare_cal").fetchone()[0]
        con.close()
        print("trade_cal 本次写入 %d，累计 %d 条（2018-2030 交易日历）" % (n, total))
        return 0
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
