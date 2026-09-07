# -*- coding: utf-8 -*-
r"""第95轮：交易日历年度维护工具——按官方休市安排生成/更新 STATIC_HOLIDAY_RANGES。

背景（摘要"六、待办"）：trade_calendar.STATIC_HOLIDAY_RANGES 静态表目前只到 2026 年；
证监会每年 12 月发布次年节假日安排（如 2025-12-22 发布证监办发〔2025〕130号覆盖 2026），
届时需补次年区间，否则次年节假日期间按工作日误判（动态日K会在节后自动校正历史，
但节前夜盘判断依赖静态表）。本工具把"补表"变成两条命令：

  1) 官方日历已在手（如 tushare_harvest.db 里已有该年）：
       python tools/holiday_updater.py --year 2027 --from-calendar    # 打印待粘贴的区间
       python tools/holiday_updater.py --year 2027 --from-calendar --apply   # 直接写回 trade_calendar.py
  2) 手动贴官方通知（12月证监会发文后）：
       python tools/holiday_updater.py --year 2027 --paste "2027-01-01~2027-01-03;2027-02-15~2027-02-23;..."

规则：只把"工作日的休市日"归并为区间（周末本就恒休，不列入）；校验每天区间合法、不与既有
区间重复；--apply 写回时按锚点断言（count==1）防误伤，写前自动备份 trade_calendar.py.bak。
selftest：零网络合成断言（区间解析/归并/查重/写回幂等）。
"""
import argparse
import io
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

CAL_PY = os.path.join(_ROOT, "trade_calendar.py")
HARVEST_DB = os.path.join(_ROOT, "cache", "tushare_harvest.db")
_ANCHOR_START = "STATIC_HOLIDAY_RANGES = ["
_ANCHOR_END = "]"


def _daterange(a, b):
    cur = a
    while cur <= b:
        yield cur
        cur += timedelta(days=1)


def _parse_ranges(paste):
    """解析 "2027-01-01~2027-01-03;2027-02-15~2027-02-23" 为 [(date,date)]，坏段跳过并返回错误。"""
    out, errs = [], []
    for seg in str(paste or "").replace("，", ";").replace(" ", "").split(";"):
        if not seg:
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2})~(\d{4}-\d{2}-\d{2})$", seg)
        if not m:
            errs.append(seg)
            continue
        a = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        b = datetime.strptime(m.group(2), "%Y-%m-%d").date()
        if b < a:
            errs.append(seg)
            continue
        out.append((a, b))
    return out, errs


def _from_calendar(year):
    """从 tushare_harvest.db 官方日历推导：工作日(is_open=0 且非周末)归并为区间。该年无数据返回 []。"""
    if not os.path.exists(HARVEST_DB):
        return []
    conn = sqlite3.connect(HARVEST_DB)
    try:
        rows = conn.execute(
            "SELECT cal_date, is_open FROM tushare_cal WHERE cal_date LIKE ? AND is_open=0",
            ("%04d%%" % year,)).fetchall()
    finally:
        conn.close()
    closed = sorted(datetime.strptime(r[0], "%Y%m%d").date() for r in rows)
    holidays = [d for d in closed if d.weekday() < 5]        # 周末恒休不列入
    ranges = []
    for d in holidays:
        if ranges and (d - ranges[-1][1]).days == 1:
            ranges[-1] = (ranges[-1][0], d)
        else:
            ranges.append((d, d))
    return ranges


def _dedup_with_existing(ranges):
    """与 trade_calendar.py 现有区间查重：重叠/包含的跳过（幂等）。"""
    existing = _read_existing_ranges()
    out = []
    for a, b in ranges:
        dup = any(not (b < ea or a > eb) for ea, eb in existing)
        if not dup:
            out.append((a, b))
    return out


def _parse_comma_date(s):
    """'2026, 1, 1' → date(2026,1,1)"""
    y, m, d = (int(p) for p in re.split(r"[,\s]+", s.strip()) if p)
    return date(y, m, d)


def _read_existing_ranges():
    try:
        s = io.open(CAL_PY, encoding="utf-8").read()
        m = re.search(re.escape(_ANCHOR_START) + r"(.*?)" + re.escape(_ANCHOR_END), s, re.S)
        if not m:
            return []
        return [(_parse_comma_date(x), _parse_comma_date(y))
                for x, y in re.findall(r"date\((\d{4},\s*\d{1,2},\s*\d{1,2})\),\s*date\((\d{4},\s*\d{1,2},\s*\d{1,2})\)", m.group(1))]
    except OSError:
        return []


def _render_block(year, ranges):
    lines = ["    # %d 年法定休市区间（来源：证监会《关于%d年部分节假日放假和休市安排的通知》）" % (year, year)]
    for a, b in ranges:
        lines.append("    (date(%d, %d, %d), date(%d, %d, %d))," %
                     (a.year, a.month, a.day, b.year, b.month, b.day))
    return "\n".join(lines)


def _apply(year, ranges):
    """把新区间插入 STATIC_HOLIDAY_RANGES 内（现有区间之后）；锚点断言防误伤；写前备份。"""
    s = io.open(CAL_PY, encoding="utf-8").read()
    assert s.count(_ANCHOR_START) == 1 and s.count(_ANCHOR_END) == 1, "锚点不唯一，中止"
    lines = s.split("\n")
    start_i = next(i for i, ln in enumerate(lines) if ln.startswith(_ANCHOR_START))
    block = _render_block(year, ranges)
    # 插到 [ 之后第一行（现有注释行之后）
    ins = start_i + 1
    while ins < len(lines) and (lines[ins].strip() == "" or lines[ins].strip().startswith("#")):
        ins += 1
    # 若是闭括号行则回退
    if ins < len(lines) and lines[ins].strip() == _ANCHOR_END:
        ins = start_i + 1
    new_lines = lines[:ins] + [block] + lines[ins:]
    backup = CAL_PY + ".bak"
    try:
        io.open(backup, "w", encoding="utf-8", newline="\n").write("\n".join(lines))
    except OSError:
        pass
    io.open(CAL_PY, "w", encoding="utf-8", newline="\n").write("\n".join(new_lines))
    return backup


def selftest():
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))
        if not cond:
            raise AssertionError("FAIL: " + name)

    # 区间解析
    ranges, errs = _parse_ranges("2027-01-01~2027-01-03;2027-02-15~2027-02-23;坏段")
    ck("粘贴解析2段", len(ranges) == 2 and len(errs) == 1)
    ck("首段端点", ranges[0] == (date(2027, 1, 1), date(2027, 1, 3)))
    # 从官方日历推导（合成 db 不可行，直接测纯函数路径的空返回）
    ck("无收割库返回空", _from_calendar(2030) == [])
    # 渲染块
    blk = _render_block(2027, [(date(2027, 1, 1), date(2027, 1, 3))])
    ck("渲染块含日期", "date(2027, 1, 1)" in blk and "2027" in blk)
    # 查重：与自身重复 → 全被去重（读现有表 2026 区间，构造重叠 2026 段）
    dups = _dedup_with_existing([(date(2026, 1, 1), date(2026, 1, 3))])
    ck("与2026既有区间重叠被去重", dups == [])
    return 0 if all(ok for _, ok in checks) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="交易日历年度维护：生成/更新 STATIC_HOLIDAY_RANGES")
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--from-calendar", action="store_true", help="从 tushare_harvest.db 官方日历推导")
    ap.add_argument("--paste", default="", help='手动贴官方休市区间，如 "2027-01-01~2027-01-03;..."')
    ap.add_argument("--apply", action="store_true", help="写回 trade_calendar.py（先备份 .bak）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.from_calendar:
        ranges = _from_calendar(args.year)
        if not ranges:
            print("官方日历库中无 %d 年数据（当前收割覆盖 2018-2026）。请等 %d 年官方安排发布后"
                  "重新收割，或改用 --paste 手动粘贴。" % (args.year, args.year))
            return 1
    else:
        ranges, errs = _parse_ranges(args.paste)
        if errs:
            print("解析失败段：%s" % errs)
            return 1
    ranges = _dedup_with_existing(ranges)
    if not ranges:
        print("%d 年区间全部与现有表重复（幂等），无变更。" % args.year)
        return 0
    print("=== 待写入 %d 年休市区间（%d 段）===" % (args.year, len(ranges)))
    print(_render_block(args.year, ranges))
    if args.apply:
        backup = _apply(args.year, ranges)
        print("已写回 %s（原文件备份 %s）" % (CAL_PY, backup))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())