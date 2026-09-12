# -*- coding: utf-8 -*-
"""G19（第46轮）db_backup 零网络/零生产库确定性测试：全部在 tmp_path 造临时 sqlite，绝不碰 data/monitor.db。"""
import os
import sqlite3
from datetime import datetime

import pytest

import db_backup as B


def _make_db(path, rows=10, extra_empty=True):
    if os.path.exists(path):
        os.remove(path)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    c.executemany("INSERT INTO t(v) VALUES(?)", [("r%d" % i,) for i in range(rows)])
    if extra_empty:
        c.execute("CREATE TABLE empty_t(id INTEGER)")
    c.commit(); c.close()
    return path


# ---------- 纯函数：文件名/解析/滚动计划 ----------
def test_filename_and_parse_roundtrip():
    st = datetime(2026, 9, 3, 16, 45, 0)
    fn = B.backup_filename(st)
    assert fn == "monitor_20260903-164500.db"
    assert B.parse_backup_stamp(fn) == st


@pytest.mark.parametrize("bad", ["random.db", "monitor_bad.db", "monitor_20260903-164500.db.json",
                                 "monitor_20261301-000000.db", "", "xmonitor_20260903-164500.db"])
def test_parse_rejects_non_tool_names(bad):
    assert B.parse_backup_stamp(bad) is None


def test_prune_plan():
    names = ["monitor_2026090%d-000000.db" % d for d in range(1, 8)]  # 7 份升序
    drop, stay = B.prune_plan(names, 3)
    assert drop == names[:4] and stay == names[4:]
    drop, stay = B.prune_plan(names, 30)
    assert drop == [] and stay == names            # 不足保留数不删
    drop, stay = B.prune_plan(names, 0)
    assert drop == [] and stay == names            # keep<=0 全保留
    drop, stay = B.prune_plan([], 5)
    assert drop == [] and stay == []


def test_list_only_recognizes_tool_files(tmp_path):
    for n in ["monitor_20260903-100000.db", "monitor_20260903-110000.db",
              "notes.txt", "other.db", "monitor_20260903-100000.db.json"]:
        open(tmp_path / n, "w").close()
    files = B.list_backup_files(str(tmp_path))
    assert files == ["monitor_20260903-100000.db", "monitor_20260903-110000.db"]
    assert B.list_backup_files(str(tmp_path / "nope")) == []


# ---------- 在线热备 ----------
def test_online_backup_consistent_and_source_readonly(tmp_path):
    src = _make_db(str(tmp_path / "s.db"), 12)
    dst = str(tmp_path / "b.db")
    nbytes = B.online_backup(src, dst)
    assert nbytes > 0 and os.path.isfile(dst)
    c = sqlite3.connect(dst)
    assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 12
    assert c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    c.close()
    # 源未被改动
    c = sqlite3.connect(src)
    assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 12
    c.close()


def test_backup_once_writes_sidecar_and_counts(tmp_path):
    src = _make_db(str(tmp_path / "monitor.db"), 7)
    bdir = str(tmp_path / "backup")
    import json
    r = B.backup_once(src, bdir, keep=30, stamp=datetime(2026, 9, 3, 10, 0, 0))
    assert r["backup_quick_check"] == "ok" and r["source_quick_check"] == "ok"
    assert r["table_rows"]["t"] == 7 and r["table_rows"]["empty_t"] == 0
    meta = json.load(open(r["sidecar"], encoding="utf-8"))
    assert meta["backup_quick_check"] == "ok" and meta["table_rows"]["t"] == 7


def test_backup_rolling_prune_deletes_oldest_and_sidecars(tmp_path):
    src = _make_db(str(tmp_path / "monitor.db"), 1)
    bdir = str(tmp_path / "backup")
    for h in range(10, 15):                      # 5 份，keep=2
        _make_db(src, h)
        B.backup_once(src, bdir, keep=2, stamp=datetime(2026, 9, 3, h, 0, 0))
    files = B.list_backup_files(bdir)
    assert files == ["monitor_20260903-130000.db", "monitor_20260903-140000.db"]
    # 旧 sidecar 同步删除
    assert not os.path.exists(os.path.join(bdir, "monitor_20260903-100000.db.json"))
    assert os.path.exists(os.path.join(bdir, "monitor_20260903-140000.db.json"))


def test_backup_same_second_collision(tmp_path):
    src = _make_db(str(tmp_path / "monitor.db"), 3)
    bdir = str(tmp_path / "backup")
    st = datetime(2026, 9, 3, 10, 0, 0)
    r1 = B.backup_once(src, bdir, keep=0, stamp=st, do_prune=False)
    r2 = B.backup_once(src, bdir, keep=0, stamp=st, do_prune=False)
    assert r1["name"] != r2["name"]               # 同秒不覆盖
    assert os.path.isfile(r1["backup"]) and os.path.isfile(r2["backup"])


def test_backup_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        B.backup_once(str(tmp_path / "nope.db"), str(tmp_path / "b"))


# ---------- 恢复 ----------
def test_restore_moves_old_and_recovers(tmp_path):
    src = _make_db(str(tmp_path / "monitor.db"), 5)
    bdir = str(tmp_path / "backup")
    B.backup_once(src, bdir, keep=30, stamp=datetime(2026, 9, 3, 10, 0, 0))
    bk = B.list_backup_files(bdir)[0]
    _make_db(src, 999)                           # 现场被改成 999 行
    info = B.restore_backup(os.path.join(bdir, bk), src, stamp=datetime(2026, 9, 3, 20, 0, 0))
    c = sqlite3.connect(src)
    assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 5   # 回到备份点
    c.close()
    assert info["verify"] == "ok" and info["old_moved"] and os.path.isfile(info["old_moved"])


def test_restore_rejects_broken_backup(tmp_path):
    src = str(tmp_path / "monitor.db"); _make_db(src, 1)
    bad = str(tmp_path / "broken.db"); open(bad, "w").write("garbage")
    with pytest.raises(RuntimeError):
        B.restore_backup(bad, src)


def test_quick_check_bad_db(tmp_path):
    bad = tmp_path / "broken.db"
    bad.write_text("not sqlite")
    assert B.quick_check(str(bad)).startswith("OPEN_ERROR")


# ---------- 第127轮：纸面账户库纳入备份范围 + 每日热备补跑判断 ----------

def test_paper_filename_and_parse_roundtrip():
    st = datetime(2026, 9, 12, 15, 1, 0)
    fn = B.paper_backup_filename("paper_10万_激进", st)
    assert fn == "paper_10万_激进_20260912-150100.db"
    assert B.parse_paper_backup(fn) == ("paper_10万_激进", st)   # 源库名含下划线


@pytest.mark.parametrize("bad", ["notes.db", "paper_x_badstamp.db", "_20260912-150100.db",
                                 "paper_x_20260912-150100.db.json", "", "random_20260912-150100.db"])
def test_paper_parse_rejects_bad_names(bad):
    assert B.parse_paper_backup(bad) is None


def test_list_and_prune_paper_grouped(tmp_path):
    pdir = tmp_path / "pb"
    pdir.mkdir()
    for name in ["paper_a_20260910-090000.db", "paper_a_20260911-090000.db",
                 "paper_b_20260911-090000.db", "stray.txt"]:
        (pdir / name).write_text("")
    grouped = B.list_paper_backups(str(pdir))
    assert grouped == {"paper_a": ["paper_a_20260910-090000.db", "paper_a_20260911-090000.db"],
                       "paper_b": ["paper_b_20260911-090000.db"]}
    assert B.prune_paper_plan(grouped, 1) == ["paper_a_20260910-090000.db"]
    assert B.prune_paper_plan(grouped, 5) == []


def test_backup_all_covers_monitor_and_paper(tmp_path):
    data = tmp_path / "data"
    (data / "paper_accounts").mkdir(parents=True)
    mon = str(data / "monitor.db")
    _make_db(mon, 5)
    _make_db(str(data / "paper_accounts" / "paper_x.db"), 3)
    _make_db(str(data / "paper_accounts" / "paper_y.db"), 4)
    (data / "paper_accounts" / "paper_bad.db").write_text("not sqlite")   # 坏库不拖垮整体
    bdir = tmp_path / "backup"
    st = datetime(2026, 9, 12, 15, 1, 0)
    res = B.backup_all(keep=3, backup_dir=str(bdir), monitor_src=mon,
                       paper_dir=str(data / "paper_accounts"), stamp=st)
    assert res["monitor"]["backup_quick_check"] == "ok" and res["monitor"]["table_rows"]["t"] == 5
    assert set(res["paper"]) == {"paper_x", "paper_y"}
    assert "paper_bad" in res["paper_errors"]
    assert all(r["backup_quick_check"] == "ok" for r in res["paper"].values())
    pdir = bdir / "paper_accounts"
    assert (pdir / "paper_x_20260912-150100.db").is_file()
    assert (pdir / "paper_x_20260912-150100.db.json").is_file()


def test_backup_all_paper_rolling_per_source(tmp_path):
    data = tmp_path / "data"
    (data / "paper_accounts").mkdir(parents=True)
    mon = str(data / "monitor.db")
    _make_db(mon, 1)
    px = str(data / "paper_accounts" / "paper_x.db")
    bdir = tmp_path / "backup"
    for h, rows in ((10, 1), (11, 2), (12, 3)):
        _make_db(px, rows)
        B.backup_all(keep=1, backup_dir=str(bdir), monitor_src=mon,
                     paper_dir=str(data / "paper_accounts"),
                     stamp=datetime(2026, 9, 12, h, 0, 0))
    grouped = B.list_paper_backups(str(bdir / "paper_accounts"))
    assert grouped["paper_x"] == ["paper_x_20260912-120000.db"]      # 每源库各自只留最新
    assert B.list_backup_files(str(bdir)) == ["monitor_20260912-120000.db"]
    c = sqlite3.connect(str(bdir / "paper_accounts" / "paper_x_20260912-120000.db"))
    assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3
    c.close()


@pytest.mark.parametrize("now,last,expect", [
    (datetime(2026, 9, 12, 15, 1), None, True),                       # 从未备份
    (datetime(2026, 9, 12, 15, 1), datetime(2026, 9, 12, 15, 1), False),   # 刚备过
    (datetime(2026, 9, 12, 18, 0), datetime(2026, 9, 11, 15, 1), True),    # 错过15:01→补跑当日
    (datetime(2026, 9, 12, 10, 0), datetime(2026, 9, 11, 15, 1), False),   # 未到15:01且<26h
    (datetime(2026, 9, 12, 10, 0), datetime(2026, 9, 10, 15, 0), True),    # 欠账26h+兜底
    (datetime(2026, 9, 12, 16, 0), datetime(2026, 9, 12, 15, 1), False),   # 重启时当日已备
])
def test_backup_due_schedule(now, last, expect):
    assert B.backup_due(now, last, daily_hhmm="15:01", min_interval_h=20) is expect


def test_newest_monitor_backup_time(tmp_path):
    assert B.newest_monitor_backup_time(str(tmp_path)) is None       # 目录不存在
    src = _make_db(str(tmp_path / "monitor.db"), 1)
    st = datetime(2026, 9, 12, 15, 1, 0)
    B.backup_once(src, str(tmp_path), keep=1, stamp=st)
    assert B.newest_monitor_backup_time(str(tmp_path)) == st


# ---------- 自启导出 ----------
def test_task_xml_contents():
    xml = B.build_task_xml("T", r"D:\Python\python.exe", r"C:\p\db_backup.py", r"C:\p", "16:30")
    assert xml.startswith('<?xml') and "CalendarTrigger" in xml and "LogonTrigger" in xml
    assert "2026-01-01T16:30:00" in xml and "--once" in xml and "LeastPrivilege" in xml
    xml2 = B.build_task_xml("T", "py", "s", "w", "07:05")
    assert "07:05:00" in xml2


def test_bat_contents():
    bat = B.build_bat(r"D:\Python\python.exe", r"C:\p\db_backup.py", r"C:\p")
    assert "chcp 65001" in bat and "--once" in bat and 'cd /d "C:\\p"' in bat


def test_read_version_missing_safe(tmp_path):
    assert B.read_version(str(tmp_path)) == "unknown"


def test_human_mb():
    assert B.human_mb(1048576) == "1.0MB"
    assert B.human_mb(0) == "0.0MB"
