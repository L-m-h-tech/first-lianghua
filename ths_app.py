# -*- coding: utf-8 -*-
"""【需求②/⑤】同花顺期货通控制：程序每次启动时自动打开期货通（仅打开备用，
分析数据全部来自公开接口，不依赖期货通）。早期版本(需求②)曾通过UIAutomation
读取自选，后按需求⑤改为分析四大交易所全部品种，不再读取自选。"""
import os
import subprocess
import time

import config
from utils import LOG

_launched_once = False


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


def launch_ths():
    """启动期货通（每个程序生命周期只尝试一次）"""
    global _launched_once
    if _launched_once or not os.path.exists(config.THS_EXE):
        return False
    try:
        subprocess.Popen([config.THS_EXE], cwd=os.path.dirname(config.THS_EXE))
        _launched_once = True
        LOG.info("已启动同花顺期货通(%s)，等待窗口就绪...", config.THS_EXE)
        return True
    except Exception as e:
        LOG.warning("启动期货通失败: %s", e)
        return False


def ensure_running():
    """确保期货通在运行：已在运行直接返回True，否则启动并等待窗口出现"""
    if find_ths_windows():
        return True
    if launch_ths():
        deadline = time.time() + config.THS_LAUNCH_WAIT
        while time.time() < deadline:
            time.sleep(5)
            if find_ths_windows():
                return True
    return False
