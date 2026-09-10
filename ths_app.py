# -*- coding: utf-8 -*-
"""【需求②/⑤】同花顺期货通控制：程序每次启动时自动以调试模式打开期货通（仅打开备用，
分析数据全部来自公开接口，不依赖期货通）。早期版本(需求②)曾通过UIAutomation
读取自选，后按需求⑤改为分析四大交易所全部品种，不再读取自选。
调试模式（2026-09-10 实测）：`bin/workspace/DataCenter.xml` 的
`<Debug><Cef><Console enable="true"/></Cef>` 开启后，重启 happ.exe 即弹出独立
DevTools 窗口（Chromium 全套面板）；`--remote-debugging-port`/CDP 对该客户端无效。
启动流程：确保调试配置 → 未运行则拉起；已运行但无 DevTools 窗口则自动重启应用
调试开关（THS_DEBUG_RESTART 可关）。"""
import os
import re
import shutil
import subprocess
import sys
import time

import config
from utils import LOG

_launched_once = False
_ths_pid = None                 # 本模块亲手 Popen 的同花顺 PID（退出/重启前联动关闭）
_ths_job = None                 # Windows Job Object 句柄：main 被强杀时系统自动终止同花顺


def _ths_job_attach(pid):
    """把同花顺进程挂入 Windows Job Object（KILL_ON_JOB_CLOSE）：
    main 进程退出（含被强杀）时系统自动终止该进程，杜绝孤儿残留；失败静默降级。"""
    global _ths_job
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                        ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong),
                        ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong),
                        ("OtherTransferCount", ctypes.c_ulonglong)]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", ctypes.c_ulong),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", ctypes.c_ulong),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", ctypes.c_ulong),
                        ("SchedulingClass", ctypes.c_ulong)]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info),
                                                ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        hproc = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not hproc:
            kernel32.CloseHandle(job)
            return
        ok = kernel32.AssignProcessToJobObject(job, hproc)
        kernel32.CloseHandle(hproc)
        if ok:
            _ths_job = job   # 保持句柄打开；main 退出时 OS 自动关闭 job → 杀同花顺
    except Exception:
        _ths_job = None


def kill_ths():
    """关闭由本模块启动的同花顺进程（main 退出/看门狗重启前调用）。
    只杀自己 Popen 过的实例；任务结束失败静默；同时释放 Job Object。"""
    global _ths_pid, _ths_job
    pid = _ths_pid
    _ths_pid = None
    if not pid:
        return
    try:
        r = subprocess.run([_taskkill_path(), "/PID", str(pid), "/F", "/T"],
                           capture_output=True, timeout=30)
        LOG.info("已关闭本程序启动的同花顺进程(pid=%d) rc=%s", pid, r.returncode)
    except Exception as e:
        LOG.warning("关闭同花顺进程(pid=%d)失败: %s", pid, e)
    finally:
        if _ths_job:
            try:
                import ctypes
                ctypes.WinDLL("kernel32").CloseHandle(_ths_job)
            except Exception:
                pass
            _ths_job = None


def _patch_debug_console(text):
    """纯函数：确保 DataCenter.xml 文本含 <Debug><Cef><Console enable="true"/></Cef>。
    已启用或结构无法识别时返回 None（不写回），其余情况返回补丁后的完整文本。"""
    m = re.search(r"<Console\b([^>]*?)/?>", text)
    if m:
        attrs = m.group(1).strip()
        if re.search(r'\benable="true"', attrs):
            return None
        attrs = re.sub(r'\benable="[^"]*"', 'enable="true"', attrs)
        if attrs and not re.search(r"\benable=", attrs):
            attrs += ' enable="true"'
        elif not attrs:
            attrs = 'enable="true"'
        return text[:m.start()] + "<Console " + attrs + "/>" + text[m.end():]
    m = re.search(r"(<Cef\b[^>]*>)(.*?)(</Cef>)", text, re.S)
    if m:
        lead = "\n" + " " * 6 if "\n" in m.group(2) else " "
        return m.group(1) + lead + '<Console enable="true"/>' + m.group(2) + m.group(3)
    m = re.search(r"(<Debug\b[^>]*>)(.*?)(</Debug>)", text, re.S)
    if m:
        block = '\n    <Cef>\n      <Console enable="true"/>\n    </Cef>'
        return m.group(1) + block + m.group(2) + m.group(3)
    m = re.search(r"(<DataCenter\b[^>]*>)", text)
    if m:
        block = '\n  <Debug>\n    <Cef>\n      <Console enable="true"/>\n    </Cef>\n  </Debug>'
        return m.group(1) + block + text[m.end():]
    return None


def ensure_debug_mode():
    """确保 DataCenter.xml 已开启 Cef Console 调试开关（幂等：已开启不重写；
    首次修改前备份为 .bak）。只改配置、不杀进程；已运行中的 happ 需重启才生效。"""
    if not getattr(config, "THS_DEBUG_MODE", True):
        return False
    path = getattr(config, "THS_DATA_CENTER_XML", None)
    if not path:
        path = os.path.join(os.path.dirname(config.THS_EXE), "workspace", "DataCenter.xml")
    if not os.path.exists(path):
        LOG.warning("DataCenter.xml 不存在(%s)，无法开启同花顺调试模式", path)
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        LOG.warning("读取 DataCenter.xml 失败: %s", e)
        return False
    patched = _patch_debug_console(text)
    if patched is None:
        LOG.info("同花顺调试模式已开启（DataCenter.xml 已含 Console enable=true）")
        return True
    try:
        bak = path + ".bak"
        if not os.path.exists(bak):
            shutil.copy2(path, bak)
        nl = "\r\n" if "\r\n" in text else "\n"
        with open(path, "w", encoding="utf-8", newline=nl) as f:
            f.write(patched)
        LOG.info("已开启同花顺调试模式（DataCenter.xml Cef Console=true，原文件备份 %s）", bak)
        return True
    except OSError as e:
        LOG.warning("写入 DataCenter.xml 失败: %s", e)
        return False


def find_ths_windows():
    """查找标题包含'期货通'的窗口"""
    try:
        import uiautomation as auto
    except ImportError:
        return []
    wins = []
    try:
        for w in auto.GetRootControl().GetChildren():
            try:
                nm = w.Name or ""
            except Exception:
                continue
            if "期货通" in nm:
                wins.append(w)
    except Exception as e:
        LOG.debug("遍历顶层窗口失败: %s", e)
    return wins


def find_debug_window():
    """查找标题含'DevTools'的顶层窗口（调试模式开启后 happ 会弹出的独立调试窗口）"""
    try:
        import uiautomation as auto
    except ImportError:
        return None
    try:
        for w in auto.GetRootControl().GetChildren():
            try:
                nm = w.Name or ""
            except Exception:
                continue
            if "DevTools" in nm:
                return w
    except Exception as e:
        LOG.debug("遍历顶层窗口失败: %s", e)
    return None


def _should_restart(has_main_win, has_debug_win):
    """已运行但无 DevTools 调试窗口时需要重启以应用调试模式（THS_DEBUG_RESTART 总开关）。"""
    return has_main_win and not has_debug_win and getattr(config, "THS_DEBUG_RESTART", True)


def _taskkill_path():
    """定位 taskkill.exe：优先用 SystemRoot 环境变量所指系统盘的 System32 绝对路径（绕开 PATH 缺失）"""
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    p = os.path.join(root, "System32", "taskkill.exe")
    return p if os.path.exists(p) else "taskkill"


def _dec(text):
    """taskkill 输出按 UTF-8/GBK 尝试解码（中文 Windows 控制台多为 GBK）"""
    for enc in ("utf-8", "gbk"):
        try:
            return text.decode(enc)
        except UnicodeDecodeError:
            continue
    return text.decode("utf-8", "ignore")


def _wait_for_ready():
    """等待期货通主窗口（Name 含'期货通'）出现，最长 config.THS_LAUNCH_WAIT 秒"""
    deadline = time.time() + config.THS_LAUNCH_WAIT
    while time.time() < deadline:
        time.sleep(5)
        if find_ths_windows():
            return True
    return False


def launch_ths():
    """启动期货通（每个程序生命周期只尝试一次）；启动前先确保调试模式开关已写入配置"""
    global _launched_once, _ths_pid
    if _launched_once or not os.path.exists(config.THS_EXE):
        return False
    ensure_debug_mode()
    try:
        p = subprocess.Popen([config.THS_EXE], cwd=os.path.dirname(config.THS_EXE))
        _launched_once = True
        _ths_pid = p.pid
        _ths_job_attach(p.pid)
        LOG.info("已以调试模式启动同花顺期货通(%s, pid=%d)，等待窗口就绪...", config.THS_EXE, p.pid)
        return True
    except Exception as e:
        LOG.warning("启动期货通失败: %s", e)
        return False


def _restart_ths():
    """结束已运行的同花顺期货通进程，重新以调试模式拉起（应用 DataCenter.xml 调试开关）。
    任务结束失败则放弃重启（保持旧实例运行），不阻断主程序启动。"""
    global _launched_once, _ths_pid
    if not os.path.exists(config.THS_EXE):
        return False
    _launched_once = True       # 本次生命周期只重启这一次
    exe_name = os.path.basename(config.THS_EXE)
    try:
        r = subprocess.run([_taskkill_path(), "/IM", exe_name, "/F"],
                           capture_output=True, timeout=30)
        LOG.info("已结束同花顺进程(%s) rc=%s %s", exe_name, r.returncode,
                 _dec(r.stdout or r.stderr or b"").strip())
    except Exception as e:
        LOG.warning("结束同花顺进程失败: %s", e)
        return False
    time.sleep(3)               # 等进程/窗口彻底退出，避免旧窗口误判
    try:
        p = subprocess.Popen([config.THS_EXE], cwd=os.path.dirname(config.THS_EXE))
        _ths_pid = p.pid
        _ths_job_attach(p.pid)
        LOG.info("已重新以调试模式启动同花顺期货通(%s, pid=%d)，等待窗口就绪...", config.THS_EXE, p.pid)
        return True
    except Exception as e:
        LOG.warning("重启同花顺失败: %s", e)
        return False


def ensure_running():
    """确保期货通以调试模式在运行：先确保调试配置；未运行则拉起并等待；
    已运行但无 DevTools 调试窗口则自动重启（应用调试开关）；已带调试窗口直接返回True。"""
    ensure_debug_mode()
    if find_ths_windows():
        if find_debug_window():
            return True
        if _should_restart(True, False):
            LOG.info("检测到同花顺期货通已运行但无调试(DevTools)窗口，自动重启以应用调试模式...")
            if _restart_ths() and _wait_for_ready():
                return True
            return True         # 重启失败/窗口未现也保持现状，不阻断主程序
        return True
    if launch_ths():
        return _wait_for_ready()
    return False
