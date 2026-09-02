# -*- coding: utf-8 -*-
"""
G6 零依赖历史归档工具（研究/运维侧，标准库 sqlite3/csv，不进常驻链路、不新增依赖）。

用途：monitor.db 常驻运行会持续增长，本工具在不删数据的前提下把指定年份的历史明细
导出成「按年 SQLite 分库」或「CSV 年包」，之后可放心让 storage.prune 按保留期清理主库。

用法（项目根目录，用固定解释器）：
  D:/Python/python.exe tools/db_archive.py --year 2026 --out data/archive
  D:/Python/python.exe tools/db_archive.py --year 2025 --csv --out data/archive
  D:/Python/python.exe tools/db_archive.py --selftest

说明：
  - 只导出、不删除主库任何行；删除由主程序 prune 按 DB_RETENTION_DAYS/MINUTE_BARS_RETENTION_DAYS 负责。
  - 日期列按表实际字段（ts/trade_date/bar_dt）取前4位判年份；无法解析的行不进年包、留在主库。
  - 导出的年包可随时用 sqlite3 ATTACH 回读做长周期研究。
"""
import argparse
import csv
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 表 -> 用于判年份的日期列
DATE_COLUMN = {
    "quotes": "ts", "signals": "ts", "news": "ts", "options": "ts",
    "signal_outcomes": "entry_ts", "option_chains": "ts", "fundamentals": "trade_date",
    "minute_bars": "trade_date", "ml_samples": "bar_dt", "data_health": "ts",
}


def _existing_tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def export_year_sqlite(db_path, year, out_path):
    """把 year 年的各表数据导出到独立 sqlite 文件，返回 {表: 导出行数}。"""
    year = str(year)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    if os.path.exists(out_path):
        os.remove(out_path)
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(out_path)
    counts = {}
    try:
        dst.execute("ATTACH DATABASE ? AS src", (db_path,))
        tables = _existing_tables(src)
        for table, col in DATE_COLUMN.items():
            if table not in tables:
                continue
            cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})").fetchall()]
            if col not in cols:
                continue
            dst.execute(f"CREATE TABLE {table} AS SELECT * FROM src.{table} WHERE substr({col},1,4)=?",
                        (year,))
            counts[table] = dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        dst.commit()
        # 完整性自检
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError("导出库完整性异常: %s" % ok)
    finally:
        src.close()
        dst.close()
    return counts


def export_year_csv(db_path, year, out_dir):
    """把 year 年各表导出为 CSV（每表一个文件，utf-8-sig，Excel 可直接开），返回 {表: 行数}。"""
    year = str(year)
    os.makedirs(out_dir, exist_ok=True)
    src = sqlite3.connect(db_path)
    src.row_factory = sqlite3.Row
    counts = {}
    try:
        tables = _existing_tables(src)
        for table, col in DATE_COLUMN.items():
            if table not in tables:
                continue
            cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})").fetchall()]
            if col not in cols:
                continue
            rows = src.execute(
                f"SELECT * FROM {table} WHERE substr({col},1,4)=? ORDER BY 1", (year,)).fetchall()
            fp = os.path.join(out_dir, f"{table}_{year}.csv")
            with open(fp, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                if cols:
                    w.writerow(cols)
                    for r in rows:
                        w.writerow([r[c] for c in cols])
            counts[table] = len(rows)
    finally:
        src.close()
    return counts


def _selftest():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "m.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE quotes(ts TEXT, code TEXT, price REAL)")
    c.execute("CREATE TABLE minute_bars(trade_date TEXT, sym TEXT, c REAL)")
    c.executemany("INSERT INTO quotes VALUES(?,?,?)", [
        ("2025-12-31 23:00:00", "RB0", 100), ("2026-01-02 10:00:00", "RB0", 101),
        ("2026-06-01 10:00:00", "MA0", 99), ("2027-01-01 10:00:00", "RB0", 102)])
    c.executemany("INSERT INTO minute_bars VALUES(?,?,?)", [
        ("2026-01-02", "RB", 101), ("2025-01-02", "RB", 98)])
    c.commit(); c.close()

    out = os.path.join(d, "archive", "monitor_2026.db")
    n = export_year_sqlite(db, 2026, out)
    assert n["quotes"] == 2 and n["minute_bars"] == 1, n
    chk = sqlite3.connect(out)
    assert chk.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 2
    # 主库不被删除
    main = sqlite3.connect(db)
    assert main.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 4
    chk.close(); main.close()

    csvdir = os.path.join(d, "csv")
    cn = export_year_csv(db, 2026, csvdir)
    assert cn["quotes"] == 2 and cn["minute_bars"] == 1 and os.path.exists(
        os.path.join(csvdir, "quotes_2026.csv")), cn
    print("db_archive selftest PASS:", n, cn)


def main():
    ap = argparse.ArgumentParser(description="monitor.db 按年零依赖归档")
    ap.add_argument("--year", type=int, help="要导出的年份，如 2026")
    ap.add_argument("--out", default="data/archive", help="输出目录")
    ap.add_argument("--csv", action="store_true", help="导出 CSV 年包（默认导出 SQLite 分库）")
    ap.add_argument("--db", default=None, help="主库路径，默认 config.MONITOR_DB")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        _selftest(); return
    if not args.year:
        ap.error("需要 --year（或 --selftest）")
    db_path = args.db
    if db_path is None:
        import config
        db_path = config.MONITOR_DB
    if args.csv:
        counts = export_year_csv(db_path, args.year, args.out)
    else:
        out = os.path.join(args.out, "monitor_%d.db" % args.year)
        counts = export_year_sqlite(db_path, args.year, out)
        print("已导出 SQLite 年包:", out)
    for t, n in sorted(counts.items()):
        print("  %-16s %d 行" % (t, n))


if __name__ == "__main__":
    main()
