# -*- coding: utf-8 -*-
r"""G19（第46轮）数据库在线热备份 + 滚动保留 + 开机自启/定时任务导出 + 灾备恢复：db_backup.py。

纯标准库（sqlite3/os/shutil/argparse/json），**只读源库、只写 backup/**，不接 main 主循环、不改综合分、
不改任何生产数据。定位是运维安全网：monitor.db（WAL、GB 级，含分钟库/信号/成交/研究样本等全部家当）
与 data/paper_accounts/ 下 20 个纸面账户库（第110轮迁出、第127轮纳入备份）此前长期无自动备份，
磁盘损坏/误操作/写坏即全损（纸面历史尤其不可再生）。

能力：
- **在线热备**：用 sqlite3 官方 `Connection.backup()`（Online Backup API），源库以**只读 URI** 打开，
  程序常驻写库时也能得到一致性快照（对 WAL 安全），不需要停 main，不持长锁；比 `VACUUM INTO` 更通用、
  可分页，且不改变源库。备份后对**副本**跑 PRAGMA quick_check，校验不过即删除坏副本并报错（不留假备份）。
- **全量家当（第127轮）**：`--once` 默认备份 monitor.db **和** `data/paper_accounts/*.db` 全部 20 库
  （`backup/paper_accounts/<源库名>_<stamp>.db`，每库各自滚动；`--no-paper` 跳过）；
  `backup_all()` 供 main 每日热备线程调用，`backup_due()` 实现错过计划时刻/长期未开的补跑判断。
- **滚动保留**：backup/monitor_YYYYMMDD-HHMMSS.db，默认保留最近 30 份（--keep 调），按时间戳删最旧；
  只认本工具命名前缀，绝不误删目录里其它文件；每份配一个同名 .json sidecar 记录源大小/表行数/版本/校验。
- **校验/列举/恢复**：--list 列备份（时间/大小/源qc，含纸面分节）、--verify 对副本 quick_check、
  --restore 用备份反向热恢复（先把现有库安全改名 .before_restore_<时间戳>，绝不直接覆盖丢失现场）。
- **自启导出（不擅自改系统）**：--emit-bat 生成内层看门狗 run_backup.bat；--emit-task-xml 生成 Windows
  任务计划程序可直接导入的 XML（每日收盘后定时 + 登录时补一次）。**只生成文件、不执行 schtasks /register**，
  是否注册由用户决定（导入步骤写进灾备 runbook）。
- **--version**：读 VERSION 打印版本。

无参 = 零网络/零生产库的合成自测（在 tmp 目录造库演练备份/滚动/恢复/XML），带参才执行真实动作。
"""
import argparse
import io
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

DEFAULT_BACKUP_DIR = os.path.join(_HERE, "backup")
BACKUP_PREFIX = "monitor_"
BACKUP_SUFFIX = ".db"
DEFAULT_KEEP = 30
RESTORE_OLD_PREFIX = "monitor.db.before_restore_"
# 第127轮：纸面账户库纳入备份范围（第110轮迁出 monitor.db 后一直零备份）
PAPER_ACCOUNTS_DIR = os.path.join(_HERE, "data", "paper_accounts")
PAPER_BACKUP_SUBDIR = "paper_accounts"


# =========================== 纯函数/小工具（不碰生产库，可确定性单测） ===========================
def read_version(root=_HERE):
    """读 VERSION（去 BOM/空白）；缺失返 unknown，不抛。"""
    p = os.path.join(root, "VERSION")
    try:
        with io.open(p, "r", encoding="utf-8-sig") as f:
            return f.read().strip() or "unknown"
    except OSError:
        return "unknown"


def backup_filename(stamp):
    """stamp=datetime → backup/monitor_YYYYMMDD-HHMMSS.db 文件名（只给文件名，不含目录）。"""
    return "%s%s%s" % (BACKUP_PREFIX, stamp.strftime("%Y%m%d-%H%M%S"), BACKUP_SUFFIX)


_FN_LEN = len("monitor_YYYYMMDD-HHMMSS.db")


def parse_backup_stamp(filename):
    """从本工具命名的备份文件名解析时间戳 datetime；不合规返 None（用于只认自己的文件、防误删）。"""
    if not (filename.startswith(BACKUP_PREFIX) and filename.endswith(BACKUP_SUFFIX)):
        return None
    mid = filename[len(BACKUP_PREFIX):-len(BACKUP_SUFFIX)]
    try:
        return datetime.strptime(mid, "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def list_backup_files(backup_dir):
    """返回备份目录里合规备份文件名列表，按时间戳升序；目录不存在返 []。其它文件一律忽略。"""
    out = []
    if not os.path.isdir(backup_dir):
        return out
    for name in os.listdir(backup_dir):
        if parse_backup_stamp(name) is not None and name.endswith(BACKUP_SUFFIX):
            fp = os.path.join(backup_dir, name)
            if os.path.isfile(fp):
                out.append(name)
    out.sort(key=parse_backup_stamp)
    return out


def prune_plan(filenames, keep):
    """给定升序备份文件名与保留份数，返回 (应删除列表, 保留列表)。keep<=0 表示全保留。纯函数。"""
    if keep is None or keep <= 0:
        return [], list(filenames)
    if len(filenames) <= keep:
        return [], list(filenames)
    drop = list(filenames[:len(filenames) - keep])
    stay = list(filenames[len(filenames) - keep:])
    return drop, stay


def human_mb(n_bytes):
    try:
        return "%.1fMB" % (float(n_bytes) / 1048576.0)
    except (TypeError, ValueError):
        return "?"


# =========================== 纸面账户库（第127轮纳入备份范围） ===========================
def paper_backup_filename(db_name, stamp):
    """源库名（paper_10万_激进）+ stamp → paper_10万_激进_YYYYMMDD-HHMMSS.db（不含目录）。"""
    return "%s_%s%s" % (db_name, stamp.strftime("%Y%m%d-%H%M%S"), BACKUP_SUFFIX)


def parse_paper_backup(filename):
    """paper_<源库名>_YYYYMMDD-HHMMSS.db → (源库名, datetime)；不合规返 None（防误删其它文件）。
    源库名自身含下划线（paper_10万_激进），用最后一个下划线分隔；且只认 paper_ 前缀源名
    （与 config PAPER_ACCOUNTS 的 paper_{name}.db 命名一致），其余文件一律不识别不清理。"""
    if not filename.endswith(BACKUP_SUFFIX):
        return None
    stem = filename[:-len(BACKUP_SUFFIX)]
    db_name, sep, stamp_str = stem.rpartition("_")
    if not sep or not db_name or not db_name.startswith("paper_"):
        return None
    try:
        return db_name, datetime.strptime(stamp_str, "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def list_paper_backups(backup_dir):
    """纸面备份目录 → {源库名: [备份文件名，按时间升序]}；目录不存在/空返 {}。其它文件一律忽略。"""
    out = {}
    if not os.path.isdir(backup_dir):
        return out
    for name in os.listdir(backup_dir):
        parsed = parse_paper_backup(name)
        if parsed and os.path.isfile(os.path.join(backup_dir, name)):
            out.setdefault(parsed[0], []).append(name)
    for names in out.values():
        names.sort(key=lambda n: parse_paper_backup(n)[1])
    return out


def prune_paper_plan(grouped, keep):
    """{源库名: [文件名升序]} → 应删除文件名列表（每个源库各自滚动保留最新 keep 份）。纯函数。"""
    drop = []
    for names in grouped.values():
        d, _ = prune_plan(names, keep)
        drop.extend(d)
    return drop


def newest_monitor_backup_time(backup_dir=DEFAULT_BACKUP_DIR):
    """最新一份 monitor 备份的时间戳 datetime；尚无备份返 None（main 热备线程补跑判断用）。"""
    files = list_backup_files(backup_dir)
    return parse_backup_stamp(files[-1]) if files else None


def backup_due(now, last, daily_hhmm="15:01", min_interval_h=20.0):
    """是否应执行一次备份（纯函数；main 热备线程每 10 分钟轮询调用）。第127轮。

    规则：
    - 从未备份 → True（启动即补上第一份）；
    - 距上次 >= min_interval_h 且 已过当日计划时刻 → True（main 晚启动/错过 15:01 也能补跑当日）；
    - 距上次 >= 26h 的欠账兜底 → True（即使未到当日计划时刻也补，main 长期没开也不至于缺口过大）；
    - 其余（距上次尚近，如刚重启时 15:01 已备过）→ False，不重复备份。
    """
    if last is None:
        return True
    try:
        hh, mm = (int(x) for x in str(daily_hhmm).split(":"))
    except (ValueError, AttributeError):
        hh, mm = 15, 1
    daily = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    gap_h = (now - last).total_seconds() / 3600.0
    if gap_h < float(min_interval_h):
        return False
    return now >= daily or gap_h >= 26.0


def table_row_counts(conn):
    """各用户表行数（排除 sqlite 内部表），用于 sidecar 概览；失败返 {}。"""
    counts = {}
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for (name,) in rows:
            try:
                counts[name] = int(conn.execute("SELECT COUNT(*) FROM \"%s\"" % name).fetchone()[0])
            except sqlite3.DatabaseError:
                counts[name] = None
    except sqlite3.DatabaseError:
        return {}
    return counts


def quick_check(db_path):
    """对指定库跑 PRAGMA quick_check，返回结果字符串（'ok' 或首个问题）；打不开返 'OPEN_ERROR: ...'。"""
    con = None
    try:
        con = sqlite3.connect(db_path)
        row = con.execute("PRAGMA quick_check").fetchone()
        return row[0] if row else "EMPTY"
    except sqlite3.DatabaseError as e:
        return "OPEN_ERROR: %s" % e
    finally:
        if con is not None:
            con.close()


# =========================== 在线热备（IO 层） ===========================
def online_backup(src_path, dst_path):
    """把 src 一致性热备到 dst（覆盖已存在的 dst）。源以**只读 URI** 打开，不干扰常驻写库。
    返回 dst 字节数。用官方 Online Backup API（pages=-1 一次性全量，数百 MB 仅秒级），WAL 安全。"""
    src_uri = "file:%s?mode=ro" % src_path.replace("\\", "/")
    src = sqlite3.connect(src_uri, uri=True)
    if os.path.exists(dst_path):
        os.remove(dst_path)
    dst = sqlite3.connect(dst_path)
    try:
        # pages=-1：一步拷完整个主库（在线一致性快照，自动处理源库并发写与 WAL）
        src.backup(dst, pages=-1)
        dst.commit()
    finally:
        dst.close()
        src.close()
    return os.path.getsize(dst_path)


def backup_once(src_path, backup_dir=DEFAULT_BACKUP_DIR, keep=DEFAULT_KEEP, stamp=None,
                write_sidecar=True, do_prune=True, name=None):
    """执行一次完整备份：源 quick_check（抢救性仍备份但标注）→ 在线热备 → 副本 quick_check
    （不过即删副本抛错）→ 写 sidecar → 滚动清理。返回结果 dict。
    name：显式指定备份文件名（纸面库用 <源库名>_<stamp>.db 命名）；缺省 monitor_<stamp>.db。
    注意 do_prune 只滚 monitor_ 前缀文件；纸面备份由 backup_all 统一按源库分组滚动。"""
    if not os.path.isfile(src_path):
        raise FileNotFoundError("源库不存在: %s" % src_path)
    stamp = stamp or datetime.now()
    os.makedirs(backup_dir, exist_ok=True)
    name = name or backup_filename(stamp)
    dst_path = os.path.join(backup_dir, name)
    if os.path.exists(dst_path):  # 同秒碰撞加序号
        k = 2
        while os.path.exists(dst_path + (".%d" % k)):
            k += 1
        dst_path = dst_path + (".%d" % k)
        name = os.path.basename(dst_path)

    src_qc = quick_check(src_path)
    src_size = os.path.getsize(src_path)
    online_backup(src_path, dst_path)
    dst_qc = quick_check(dst_path)
    if dst_qc != "ok":
        # 副本损坏：不留假备份
        try:
            os.remove(dst_path)
        except OSError:
            pass
        raise RuntimeError("备份副本 quick_check 未通过，已删除坏副本: %s" % dst_qc)

    counts = {}
    con = None
    try:
        con = sqlite3.connect(dst_path)
        counts = table_row_counts(con)
    finally:
        if con is not None:
            con.close()
    dst_size = os.path.getsize(dst_path)

    sidecar = None
    if write_sidecar:
        sidecar = dst_path + ".json"
        meta = {"backup_file": name, "created": stamp.strftime("%Y-%m-%d %H:%M:%S"),
                "source": os.path.abspath(src_path), "source_bytes": src_size,
                "source_quick_check": src_qc, "backup_bytes": dst_size,
                "backup_quick_check": dst_qc, "table_rows": counts,
                "version": read_version(), "tool": "db_backup.py"}
        with io.open(sidecar, "w", encoding="utf-8", newline="\n") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1, allow_nan=False)

    dropped = []
    if do_prune:
        files = list_backup_files(backup_dir)
        drop_names, _ = prune_plan(files, keep)
        for dn in drop_names:
            for ext in ("", ".json"):
                fp = os.path.join(backup_dir, dn + ext) if ext else os.path.join(backup_dir, dn)
                try:
                    os.remove(fp)
                except OSError:
                    pass
            dropped.append(dn)

    return {"backup": dst_path, "name": name, "source_bytes": src_size,
            "source_quick_check": src_qc, "backup_bytes": dst_size,
            "backup_quick_check": dst_qc, "table_rows": counts,
            "sidecar": sidecar, "pruned": dropped}


# =========================== 一次全量家当备份（第127轮） ===========================
def backup_all(keep=DEFAULT_KEEP, backup_dir=DEFAULT_BACKUP_DIR, monitor_src=None,
               paper_dir=None, include_paper=True, stamp=None):
    """备份全部结构化家当：monitor.db + data/paper_accounts/*.db（第127轮，共享同一时间戳）。

    - monitor：backup/monitor_<stamp>.db，原 backup_once 路径 + 滚动 keep 份；
    - 纸面：backup/paper_accounts/<源库名>_<stamp>.db，每个源库各自滚动 keep 份
      （20 库合计约 63MB，7 份约 440MB；任一库失败不拖垮整体，记入 paper_errors 继续）；
    - monitor_src/paper_dir 可注入（测试用 tmp 目录）；include_paper=False 退化为旧行为；
    - 返回 {"monitor":…, "paper": {源库名: 结果}, "paper_errors": {源库名: 错误},
            "pruned": […], "paper_pruned": […], "paper_dir": dest}。
    """
    stamp = stamp or datetime.now()
    if monitor_src is None:
        try:
            import config
            monitor_src = config.MONITOR_DB
        except Exception:
            monitor_src = os.path.join(_HERE, "data", "monitor.db")
    res = {"monitor": backup_once(monitor_src, backup_dir, keep=keep, stamp=stamp),
           "paper": {}, "paper_errors": {}, "paper_dir": None,
           "pruned": [], "paper_pruned": []}
    res["pruned"] = list(res["monitor"].get("pruned") or [])
    if not include_paper:
        return res
    pdir = paper_dir or PAPER_ACCOUNTS_DIR
    dest = os.path.join(backup_dir, PAPER_BACKUP_SUBDIR)
    if not os.path.isdir(pdir):
        return res
    os.makedirs(dest, exist_ok=True)
    res["paper_dir"] = dest
    for fn in sorted(os.listdir(pdir)):
        if not fn.endswith(BACKUP_SUFFIX):
            continue                          # 跳过 -wal/-shm 与其它文件
        src_path = os.path.join(pdir, fn)
        if not os.path.isfile(src_path):
            continue
        db_name = fn[:-len(BACKUP_SUFFIX)]
        try:
            res["paper"][db_name] = backup_once(
                src_path, dest, keep=keep, stamp=stamp,
                name=paper_backup_filename(db_name, stamp), do_prune=False)
        except Exception as e:                # 单库失败不影响其余与 monitor
            res["paper_errors"][db_name] = str(e)
    res["paper_pruned"] = prune_paper_plan(list_paper_backups(dest), keep)
    for dn in res["paper_pruned"]:
        for ext in ("", ".json"):
            fp = os.path.join(dest, dn + ext) if ext else os.path.join(dest, dn)
            try:
                os.remove(fp)
            except OSError:
                pass
    return res


# =========================== 恢复 ===========================
def restore_backup(backup_path, src_path, stamp=None, move_old=True):
    """用备份反向热恢复到 src_path。安全策略：现有 src 先改名 .before_restore_<ts>（不直接删），
    再用 Online Backup 把备份一致性写到新 src。返回 {old_moved, restored, verify}。"""
    if not os.path.isfile(backup_path):
        raise FileNotFoundError("备份不存在: %s" % backup_path)
    if quick_check(backup_path) != "ok":
        raise RuntimeError("待恢复备份 quick_check 非 ok，拒绝恢复: %s" % backup_path)
    stamp = stamp or datetime.now()
    old_moved = None
    if move_old and os.path.isfile(src_path):
        old_moved = src_path + RESTORE_OLD_PREFIX + stamp.strftime("%Y%m%d-%H%M%S")
        # 同时把 wal/shm 挪走，避免覆盖后旧 WAL 回灌
        for ext in ("", "-wal", "-shm"):
            cand = src_path + ext
            if os.path.exists(cand):
                os.replace(cand, old_moved + ext)
    online_backup(backup_path, src_path)
    verify = quick_check(src_path)
    if verify != "ok":
        raise RuntimeError("恢复后目标库 quick_check 非 ok: %s" % verify)
    return {"old_moved": old_moved, "restored": src_path, "verify": verify}


# =========================== 自启/定时任务导出（只生成文件，不改系统） ===========================
def build_task_xml(task_name, python_exe, script_path, workdir, daily_hhmm="16:30", author="futures_monitor"):
    """生成 Windows 任务计划程序可导入的 XML：每日 daily_hhmm 跑一次 + 用户登录时补一次。
    纯字符串、确定性；不调用 schtasks。"""
    hh, mm = daily_hhmm.split(":")
    start_boundary = "2026-01-01T%s:%s:00" % (hh, mm)
    # 命令行带引号，XML 转义
    cmd = '"%s" "%s" --once' % (python_exe, script_path)
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>%s</Author>
    <Description>futures_monitor G19: 每日收盘后在线热备份 monitor.db（滚动保留最近若干份）；登录时补跑一次。只生成不自动注册。</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>%s</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%s</Command>
      <WorkingDirectory>%s</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
""" % (author, start_boundary, cmd.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), workdir)


def build_bat(python_exe, script_path, workdir):
    """内层看门狗 bat：切到项目目录、执行一次在线备份，末尾暂停失败窗口。"""
    return ("@echo off\r\n"
            "REM futures_monitor G19 sqlite online hot-backup watchdog (Task Scheduler / logon / double-click)\r\n"
            "chcp 65001 >nul\r\n"
            "cd /d \"%s\"\r\n"
            "\"%s\" \"%s\" --once\r\n"
            "if errorlevel 1 ( echo [db_backup] FAILED & pause ) else ( echo [db_backup] OK )\r\n") % (
        workdir, python_exe, script_path)


# =========================== 文本视图 ===========================
def render_list(backup_dir):
    files = list_backup_files(backup_dir)
    L = ["monitor.db 在线备份目录：%s" % backup_dir,
         "合规备份 %d 份（按时间升序）：" % len(files),
         "%-28s %10s %10s %8s %s" % ("备份文件", "大小", "源大小", "副本qc", "版本")]
    total_bytes = 0
    for name in files:
        fp = os.path.join(backup_dir, name)
        size = os.path.getsize(fp) if os.path.isfile(fp) else 0
        total_bytes += size
        sq = bq = ver = "—"
        meta_fp = fp + ".json"
        if os.path.isfile(meta_fp):
            try:
                with io.open(meta_fp, "r", encoding="utf-8-sig") as f:
                    m = json.load(f)
                sq = str(m.get("source_quick_check", "—"))
                bq = str(m.get("backup_quick_check", "—"))
                ver = str(m.get("version", "—"))
            except (OSError, ValueError):
                pass
        L.append("%-28s %10s %10s %8s %s" % (name, human_mb(size), "—", bq, ver))
    if not files:
        L.append("（空：尚未备份，运行  python db_backup.py --once）")
    L.append("  小计 %d 份 / %s" % (len(files), human_mb(total_bytes)))

    # 纸面账户库备份（第127轮，backup/paper_accounts/）
    pdir = os.path.join(backup_dir, PAPER_BACKUP_SUBDIR)
    grouped = list_paper_backups(pdir)
    L.append("")
    if not grouped:
        L.append("纸面账户库备份：（空：尚无备份，--once 会随 monitor.db 一并备份）")
        return "\n".join(L)
    ptotal = 0
    L.append("纸面账户库备份目录：%s（%d 个源库）" % (pdir, len(grouped)))
    for db_name in sorted(grouped):
        names = grouped[db_name]
        sz = sum(os.path.getsize(os.path.join(pdir, n)) for n in names if os.path.isfile(os.path.join(pdir, n)))
        ptotal += sz
        L.append("  %-24s %2d 份 / %s（最新 %s）" % (db_name, len(names), human_mb(sz), names[-1]))
    L.append("  纸面小计 %s" % human_mb(ptotal))
    return "\n".join(L)


# =========================== CLI ===========================
def _default_python():
    return sys.executable


def run(argv=None):
    ap = argparse.ArgumentParser(description="G19 monitor.db 在线热备份/滚动保留/校验/恢复/自启导出（只读源库、只写backup/）")
    ap.add_argument("--once", action="store_true", help="执行一次在线热备份（monitor.db + 纸面账户库，滚动保留 --keep 份）")
    ap.add_argument("--src", default=None, help="只备份指定源库路径（单文件旧路径；缺省=全部家当）")
    ap.add_argument("--no-paper", action="store_true", dest="no_paper", help="--once 时跳过纸面账户库")
    ap.add_argument("--dir", default=DEFAULT_BACKUP_DIR, dest="backup_dir", help="备份目录，默认 backup/")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="滚动保留份数，默认30；<=0 全保留")
    ap.add_argument("--list", action="store_true", help="列出合规备份")
    ap.add_argument("--verify", action="store_true", help="对所有（或最新--latest-n份）备份跑 quick_check")
    ap.add_argument("--latest-n", type=int, default=0, dest="latest_n", help="--verify 只校验最新 N 份，0=全部")
    ap.add_argument("--restore", default=None, metavar="BACKUP.db", help="用指定备份恢复到 --src（现有库先改名留存）")
    ap.add_argument("--yes", action="store_true", help="恢复时跳过交互确认（脚本/任务用）")
    ap.add_argument("--emit-bat", action="store_true", dest="emit_bat", help="生成 run_backup.bat 看门狗")
    ap.add_argument("--emit-task-xml", action="store_true", dest="emit_xml",
                    help="生成 Windows 任务计划 XML（每日+登录），不自动注册")
    ap.add_argument("--python", default=_default_python(), help="bat/XML 里写的 python.exe，默认当前解释器")
    ap.add_argument("--daily", default="16:30", help="任务计划每日触发时刻，默认16:30")
    ap.add_argument("--version", action="store_true", help="打印 VERSION 并退出")
    args = ap.parse_args(argv)

    if args.version:
        print(read_version())
        return 0

    src = args.src
    if src is None:
        try:
            import config
            src = config.MONITOR_DB
        except Exception:
            src = os.path.join(_HERE, "data", "monitor.db")

    if args.emit_bat:
        bat = os.path.join(_HERE, "run_backup.bat")
        with io.open(bat, "w", encoding="utf-8", newline="") as f:
            f.write(build_bat(args.python, os.path.join(_HERE, "db_backup.py"), _HERE))
        print("已生成看门狗：%s" % bat)
    if args.emit_xml:
        xml = os.path.join(_HERE, "backup", "futures_monitor_db_backup_task.xml")
        os.makedirs(os.path.dirname(xml), exist_ok=True)
        # Windows 任务计划导入偏好 UTF-16
        with io.open(xml, "w", encoding="utf-16", newline="\r\n") as f:
            f.write(build_task_xml("FuturesMonitor_DbBackup", args.python,
                                   os.path.join(_HERE, "db_backup.py"), _HERE, args.daily))
        print("已生成任务计划XML（导入后即每日%s+登录各备份一次，未自动注册）：%s" % (args.daily, xml))

    if args.list:
        print(render_list(args.backup_dir))
    if args.verify:
        files = list_backup_files(args.backup_dir)
        if args.latest_n > 0:
            files = files[-args.latest_n:]
        bad = 0
        for name in files:
            r = quick_check(os.path.join(args.backup_dir, name))
            flag = "ok" if r == "ok" else "*** %s" % r
            if r != "ok":
                bad += 1
            print("  %-28s %s" % (name, flag))
        paper_dir = os.path.join(args.backup_dir, PAPER_BACKUP_SUBDIR)
        paper = list_paper_backups(paper_dir)
        for db_name in sorted(paper):
            for name in paper[db_name]:
                r = quick_check(os.path.join(paper_dir, name))
                flag = "ok" if r == "ok" else "*** %s" % r
                if r != "ok":
                    bad += 1
                print("  %-28s %s" % (name, flag))
        print("校验 %d 份，异常 %d 份" % (len(files) + sum(len(v) for v in paper.values()), bad))
        return 1 if bad else 0
    if args.restore:
        if not args.yes:
            print("将用备份 %s 恢复到 %s；现有库会先改名 .before_restore_* 留存。输入 yes 继续："
                  % (args.restore, src))
            ans = sys.stdin.readline().strip().lower() if sys.stdin else ""
            if ans != "yes":
                print("已取消")
                return 2
        info = restore_backup(args.restore, src)
        print("恢复完成：%s（旧库改名为 %s，quick_check=%s）" %
              (info["restored"], info["old_moved"], info["verify"]))
        return 0
    if args.once:
        if args.src:
            info = backup_once(args.src, args.backup_dir, keep=args.keep)
            print("备份完成：%s" % info["name"])
            print("  源 %s quick_check=%s → 副本 %s quick_check=%s" %
                  (human_mb(info["source_bytes"]), info["source_quick_check"],
                   human_mb(info["backup_bytes"]), info["backup_quick_check"]))
            print("  用户表 %d 张，总行数 %s；滚动清理 %d 份：%s" %
                  (len(info["table_rows"]),
                   "{:,}".format(sum(v for v in info["table_rows"].values() if isinstance(v, int))),
                   len(info["pruned"]), ",".join(info["pruned"]) or "无"))
            return 0
        res = backup_all(keep=args.keep, backup_dir=args.backup_dir,
                         include_paper=not args.no_paper)
        m = res["monitor"]
        print("全量家当备份完成（%d 个源库）：" % (1 + len(res["paper"])))
        print("  monitor.db：源 %s qc=%s → 副本 %s qc=%s；滚动清理 %d 份" %
              (human_mb(m["source_bytes"]), m["source_quick_check"],
               human_mb(m["backup_bytes"]), m["backup_quick_check"], len(res["pruned"])))
        if res["paper"]:
            print("  纸面账户库 %d 个已备份 → %s；滚动清理 %d 份" %
                  (len(res["paper"]), res["paper_dir"], len(res["paper_pruned"])))
        if res["paper_errors"]:
            print("  纸面失败 %d 个（不影响其余）：%s" %
                  (len(res["paper_errors"]),
                   "; ".join("%s: %s" % (k, v[:60]) for k, v in list(res["paper_errors"].items())[:5])))
        return 0
    if not (args.list or args.emit_bat or args.emit_xml):
        ap.print_help()
    return 0


# =========================== 零网络/零生产库 合成自测 ===========================
def selftest():
    import tempfile

    def make_db(path, rows=10):
        if os.path.exists(path):
            os.remove(path)
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        c.executemany("INSERT INTO t(v) VALUES(?)", [("row%d" % i,) for i in range(rows)])
        c.execute("CREATE TABLE empty_t(id INTEGER)")
        c.commit(); c.close()

    # 1) 文件名/解析/排序/防误认
    from datetime import datetime as _dt
    st = _dt(2026, 9, 3, 16, 45, 0)
    fn = backup_filename(st)
    assert fn == "monitor_20260903-164500.db"
    assert parse_backup_stamp(fn) == st
    assert parse_backup_stamp("random.db") is None
    assert parse_backup_stamp("monitor_badstamp.db") is None
    assert parse_backup_stamp("monitor_20260903-164500.db.json") is None

    tmp = tempfile.mkdtemp(prefix="dbbackup_selftest_")
    try:
        src = os.path.join(tmp, "monitor.db")
        bdir = os.path.join(tmp, "backup")
        make_db(src, 10)
        # 2) 在线热备：副本存在、quick_check ok、数据一致
        r1 = backup_once(src, bdir, keep=30, stamp=_dt(2026, 9, 3, 10, 0, 0))
        assert os.path.isfile(r1["backup"]) and r1["backup_quick_check"] == "ok"
        assert r1["table_rows"].get("t") == 10 and r1["table_rows"].get("empty_t") == 0
        assert os.path.isfile(r1["sidecar"])
        c = sqlite3.connect(r1["backup"]); n = c.execute("SELECT COUNT(*) FROM t").fetchone()[0]; c.close()
        assert n == 10
        # 3) 源以只读打开：备份过程不改源（行数仍10）
        c = sqlite3.connect(src); assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 10; c.close()
        # 4) 多备份 + 滚动保留 keep=3：造5份，留最新3份
        for h in (11, 12, 13, 14):
            make_db(src, h)  # 改源数据再备份
            backup_once(src, bdir, keep=3, stamp=_dt(2026, 9, 3, h, 0, 0))
        files = list_backup_files(bdir)
        assert len(files) == 3, files
        # 最旧两份（10点、11点）被 prune，保留12/13/14
        assert "monitor_20260903-100000.db" not in files
        assert files[-1] == "monitor_20260903-140000.db"
        # sidecar 一并删除
        assert not os.path.exists(os.path.join(bdir, "monitor_20260903-100000.db.json"))
        # 5) prune_plan 纯函数：keep<=0 全保留、不足 keep 不删
        d, s2 = prune_plan(["a", "b"], 30); assert d == [] and len(s2) == 2
        d, s2 = prune_plan(["a", "b"], 0); assert d == [] and s2 == ["a", "b"]
        d, s2 = prune_plan(["a", "b", "c", "d", "e"], 2); assert d == ["a", "b", "c"] and s2 == ["d", "e"]
        # 6) 目录里混入其它文件不被误认/误删
        stray = os.path.join(bdir, "notes.txt"); open(stray, "w").write("x")
        assert len(list_backup_files(bdir)) == 3 and os.path.isfile(stray)
        # 7) 恢复：改坏当前源（追加到100行后"损坏现场"），用14点备份（14行）恢复，旧库被改名留存
        make_db(src, 100)
        latest_backup = os.path.join(bdir, files[-1])
        info = restore_backup(latest_backup, src, stamp=_dt(2026, 9, 3, 20, 0, 0))
        c = sqlite3.connect(src); got = c.execute("SELECT COUNT(*) FROM t").fetchone()[0]; c.close()
        assert got == 14 and info["verify"] == "ok" and info["old_moved"] and os.path.isfile(info["old_moved"])
        # 8) 拒绝恢复坏备份
        bad = os.path.join(tmp, "broken.db"); open(bad, "w").write("not a sqlite db")
        try:
            restore_backup(bad, os.path.join(tmp, "x.db"))
            raise AssertionError("坏备份应被拒绝")
        except RuntimeError:
            pass
        # 9) quick_check 对坏文件返回 OPEN_ERROR 而非抛
        assert quick_check(bad).startswith("OPEN_ERROR")
        # 10) 不存在源报错
        try:
            backup_once(os.path.join(tmp, "nope.db"), bdir)
            raise AssertionError("缺源应报错")
        except FileNotFoundError:
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 11) XML / bat 确定性与关键内容
    xml = build_task_xml("T", r"D:\Python\python.exe", r"C:\p\db_backup.py", r"C:\p", "16:30")
    assert "CalendarTrigger" in xml and "LogonTrigger" in xml and "2026-01-01T16:30:00" in xml
    assert "--once" in xml and "db_backup.py" in xml
    bat = build_bat(r"D:\Python\python.exe", r"C:\p\db_backup.py", r"C:\p")
    assert "chcp 65001" in bat and "--once" in bat and "cd /d" in bat
    # XML 特殊字符转义（这里路径无特殊，校验函数不抛即可）
    xml2 = build_task_xml("T", "C:\\Program Files\\Py\\python.exe", "C:\\p\\x.py", "C:\\p", "07:05")
    assert "07:05:00" in xml2

    # 12) read_version 缺失不抛
    assert read_version("Z:\\no\\such\\dir") == "unknown"
    assert isinstance(read_version(), str) and read_version() != "unknown"

    # 13) human_mb / table_row_counts 防御
    assert human_mb(1048576) == "1.0MB"

    # 14) 纸面命名/解析/分组（第127轮）：源库名含下划线也正确
    st2 = _dt(2026, 9, 12, 15, 1, 0)
    pfn = paper_backup_filename("paper_10万_激进", st2)
    assert pfn == "paper_10万_激进_20260912-150100.db"
    assert parse_paper_backup(pfn) == ("paper_10万_激进", st2)
    assert parse_paper_backup("notes.db") is None
    assert parse_paper_backup("paper_x_badstamp.db") is None
    assert parse_paper_backup("_20260912-150100.db") is None
    pdir2 = os.path.join(tmp, "pbackup")
    os.makedirs(pdir2, exist_ok=True)
    open(os.path.join(pdir2, paper_backup_filename("paper_a", _dt(2026, 9, 10, 9, 0, 0))), "w").close()
    open(os.path.join(pdir2, paper_backup_filename("paper_a", _dt(2026, 9, 11, 9, 0, 0))), "w").close()
    open(os.path.join(pdir2, paper_backup_filename("paper_b", _dt(2026, 9, 11, 9, 0, 0))), "w").close()
    open(os.path.join(pdir2, "stray.txt"), "w").close()
    grouped = list_paper_backups(pdir2)
    assert grouped == {"paper_a": ["paper_a_20260910-090000.db", "paper_a_20260911-090000.db"],
                       "paper_b": ["paper_b_20260911-090000.db"]}
    assert prune_paper_plan(grouped, 1) == ["paper_a_20260910-090000.db"]

    # 15) backup_all：monitor + 多个纸面库共享同一时间戳、各自滚动 keep、坏库不拖垮整体
    src_root = os.path.join(tmp, "data", "paper_accounts")
    os.makedirs(src_root, exist_ok=True)
    mon = os.path.join(tmp, "data", "monitor.db")
    make_db(mon, 5)
    p1 = os.path.join(src_root, "paper_x.db")
    make_db(p1, 3)
    make_db(os.path.join(src_root, "paper_y.db"), 4)
    open(os.path.join(src_root, "paper_bad.db"), "w").write("not sqlite")
    adir = os.path.join(tmp, "allbackup")
    st3 = _dt(2026, 9, 12, 15, 1, 0)
    ra = backup_all(keep=1, backup_dir=adir, monitor_src=mon, paper_dir=src_root, stamp=st3)
    assert os.path.isfile(ra["monitor"]["backup"]) and ra["monitor"]["backup_quick_check"] == "ok"
    assert set(ra["paper"]) == {"paper_x", "paper_y"} and "paper_bad" in ra["paper_errors"]
    assert all(r["backup_quick_check"] == "ok" for r in ra["paper"].values())
    pfiles = os.listdir(os.path.join(adir, "paper_accounts"))
    assert "paper_x_20260912-150100.db" in pfiles and "paper_y_20260912-150100.db" in pfiles
    # 第二轮 keep=1：旧行滚动删除
    make_db(p1, 9)
    ra2 = backup_all(keep=1, backup_dir=adir, monitor_src=mon, paper_dir=src_root,
                     stamp=_dt(2026, 9, 12, 15, 2, 0))
    pgrouped = list_paper_backups(os.path.join(adir, "paper_accounts"))
    assert pgrouped["paper_x"] == ["paper_x_20260912-150200.db"]
    assert ra2["paper"]["paper_x"]["table_rows"]["t"] == 9

    # 16) backup_due 补跑判断（第127轮）
    base = _dt(2026, 9, 12, 15, 1, 0)
    assert backup_due(base, None) is True                       # 从未备份
    assert backup_due(base, base) is False                      # 刚备份过
    assert backup_due(_dt(2026, 9, 12, 18, 0, 0), _dt(2026, 9, 11, 15, 1, 0)) is True   # 错过15:01→启动补跑
    assert backup_due(_dt(2026, 9, 12, 10, 0, 0), _dt(2026, 9, 11, 15, 1, 0)) is False  # 未到15:01且<26h
    assert backup_due(_dt(2026, 9, 12, 10, 0, 0), _dt(2026, 9, 10, 15, 0, 0)) is True   # 欠账26h+兜底
    assert backup_due(base, _dt(2026, 9, 12, 14, 0, 0)) is False                        # 1h前刚备过

    # 17) newest_monitor_backup_time
    bdir3 = os.path.join(tmp, "nb")
    assert newest_monitor_backup_time(bdir3) is None
    backup_once(mon, bdir3, keep=1, stamp=st3)
    assert newest_monitor_backup_time(bdir3) == st3

    print("db_backup selftest OK（17 组）")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 1:
        raise SystemExit(selftest())
    raise SystemExit(run())
