# -*- coding: utf-8 -*-
"""同花顺期货通调试模式启动（DataCenter.xml Cef Console=true）回归（零网络/零DB/零进程）。

覆盖：_patch_debug_console 的四种结构补丁与幂等（已开启/Console未启/Cef无Console/无Cef/无Debug）；
ensure_debug_mode 文件写回与 .bak 备份、已开启不重写、开关关闭、文件缺失。
全部走 tmp_path + monkeypatch，不触碰真实 E:\同花顺期货通。
"""
import os
import re

import pytest

import config
import ths_app
from ths_app import _patch_debug_console, ensure_debug_mode

BASE = ('<?xml version="1.0" encoding="utf-8"?>\n'
        '<DataCenter>\n'
        '  <Debug>\n'
        '    <Cef>\n'
        '      <Console enable="true"/>\n'
        '    </Cef>\n'
        '    <trade enable="true"/>\n'
        '  </Debug>\n'
        '</DataCenter>\n')


# ---------------- _patch_debug_console 纯函数 ----------------

def test_already_enabled_noop():
    assert _patch_debug_console(BASE) is None


def test_enable_false_to_true():
    src = BASE.replace('<Console enable="true"/>', '<Console enable="false"/>')
    out = _patch_debug_console(src)
    assert out is not None
    assert '<Console enable="true"/>' in out
    assert '<Console enable="false"/>' not in out


def test_console_without_attr():
    src = BASE.replace('<Console enable="true"/>', '<Console/>')
    out = _patch_debug_console(src)
    assert out is not None
    assert '<Console enable="true"/>' in out


def test_console_other_attr():
    src = BASE.replace('<Console enable="true"/>', '<Console Log="1"/>')
    out = _patch_debug_console(src)
    assert out is not None
    assert 'enable="true"' in out and 'Log="1"' in out


def test_cef_without_console():
    src = BASE.replace('      <Console enable="true"/>\n', '')
    out = _patch_debug_console(src)
    assert out is not None
    assert '<Console enable="true"/>' in out


def test_no_cef_block():
    src = BASE.replace('    <Cef>\n      <Console enable="true"/>\n    </Cef>\n', '')
    out = _patch_debug_console(src)
    assert out is not None
    assert '<Cef>' in out and '<Console enable="true"/>' in out
    assert '<trade enable="true"/>' in out  # 原有内容保留


def test_no_debug_block():
    src = re.sub(r'\s*<Debug>.*?</Debug>', '', BASE, flags=re.S)
    out = _patch_debug_console(src)
    assert out is not None
    assert '<Debug>' in out and '<Cef>' in out and '<Console enable="true"/>' in out


def test_unrecognized_structure_noop():
    # 完全没有 DataCenter 包裹的结构：不改动
    assert _patch_debug_console('<root><Foo/></root>') is None


def test_all_other_content_preserved():
    src = BASE.replace('<Console enable="true"/>', '<Console enable="false"/>')
    out = _patch_debug_console(src)
    assert out is not None
    assert '<trade enable="true"/>' in out
    assert '<?xml version="1.0" encoding="utf-8"?>' in out


# ---------------- ensure_debug_mode 文件写回 ----------------

def test_ensure_writes_and_backs_up(tmp_path, monkeypatch):
    src = BASE.replace('enable="true"', 'enable="false"')
    xml = tmp_path / "DataCenter.xml"
    xml.write_text(src, encoding="utf-8")
    monkeypatch.setattr(config, "THS_DATA_CENTER_XML", str(xml))
    monkeypatch.setattr(config, "THS_DEBUG_MODE", True)
    assert ensure_debug_mode() is True
    assert 'enable="true"' in xml.read_text(encoding="utf-8")
    assert (tmp_path / "DataCenter.xml.bak").exists()
    assert (tmp_path / "DataCenter.xml.bak").read_text(encoding="utf-8") == src


def test_ensure_already_enabled_no_write(tmp_path, monkeypatch):
    xml = tmp_path / "DataCenter.xml"
    xml.write_text(BASE, encoding="utf-8")
    monkeypatch.setattr(config, "THS_DATA_CENTER_XML", str(xml))
    monkeypatch.setattr(config, "THS_DEBUG_MODE", True)
    before = xml.read_bytes()
    assert ensure_debug_mode() is True
    assert xml.read_bytes() == before
    assert not (tmp_path / "DataCenter.xml.bak").exists()


def test_ensure_switch_off_no_touch(tmp_path, monkeypatch):
    src = BASE.replace('enable="true"', 'enable="false"')
    xml = tmp_path / "DataCenter.xml"
    xml.write_text(src, encoding="utf-8")
    monkeypatch.setattr(config, "THS_DATA_CENTER_XML", str(xml))
    monkeypatch.setattr(config, "THS_DEBUG_MODE", False)
    assert ensure_debug_mode() is False
    assert 'enable="false"' in xml.read_text(encoding="utf-8")
    assert not (tmp_path / "DataCenter.xml.bak").exists()


def test_ensure_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "THS_DATA_CENTER_XML", str(tmp_path / "nope.xml"))
    monkeypatch.setattr(config, "THS_DEBUG_MODE", True)
    assert ensure_debug_mode() is False


def test_ensure_bak_not_overwritten(tmp_path, monkeypatch):
    # 第二次补丁（如未来开关被外部改回）不覆盖首次 .bak
    src1 = BASE.replace('enable="true"', 'enable="false"')
    xml = tmp_path / "DataCenter.xml"
    xml.write_text(src1, encoding="utf-8")
    monkeypatch.setattr(config, "THS_DATA_CENTER_XML", str(xml))
    monkeypatch.setattr(config, "THS_DEBUG_MODE", True)
    assert ensure_debug_mode() is True
    bak = tmp_path / "DataCenter.xml.bak"
    assert bak.read_text(encoding="utf-8") == src1
    # 再把文件改回未启用，跑第二次：不新建/不覆盖 bak
    xml.write_text(src1, encoding="utf-8")
    assert ensure_debug_mode() is True
    assert bak.read_text(encoding="utf-8") == src1


def test_launch_ths_not_triggered_in_test():
    # 防御性：确认测试未误伤真实启动（_launched_once 未置位、未 Popen）
    assert ths_app._launched_once is False


# ---------------- 重启判定/进程工具纯函数 ----------------

def test_should_restart_switch_on(monkeypatch):
    monkeypatch.setattr(config, "THS_DEBUG_RESTART", True)
    # 已运行且无 DevTools 窗口 → 需要重启
    assert ths_app._should_restart(True, False) is True
    # 已带 DevTools 窗口 → 不需要
    assert ths_app._should_restart(True, True) is False
    # 未在运行 → 走拉起，不判定重启
    assert ths_app._should_restart(False, False) is False


def test_should_restart_switch_off(monkeypatch):
    monkeypatch.setattr(config, "THS_DEBUG_RESTART", False)
    assert ths_app._should_restart(True, False) is False


def test_taskkill_path_resolves():
    p = ths_app._taskkill_path()
    assert isinstance(p, str) and len(p) > 0
    if os.path.exists(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                   "System32", "taskkill.exe")):
        assert p.endswith("taskkill.exe")


def test_dec_utf8_and_gbk():
    assert ths_app._dec("OK".encode("utf-8")) == "OK"
    assert ths_app._dec("成功".encode("gbk")) == "成功"


# ---------------- 启动 PID 记录 + 退出联动关闭（kill_ths） ----------------

def test_launch_records_pid_and_kill_ths(monkeypatch):
    import subprocess
    import os

    class FakePopen:
        def __init__(self, *a, **kw):
            self.pid = 99999

    tk_calls = []

    def fake_run(cmd, **kw):
        tk_calls.append(cmd)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    monkeypatch.setattr(config, "THS_EXE", r"C:\fake\happ.exe")
    monkeypatch.setattr(config, "THS_DEBUG_MODE", False)   # 不碰真实 DataCenter.xml
    monkeypatch.setattr(ths_app, "_launched_once", False)
    monkeypatch.setattr(ths_app, "_ths_pid", None)
    monkeypatch.setattr(ths_app, "_taskkill_path", lambda: "taskkill")
    try:
        assert ths_app.launch_ths() is True
        assert ths_app._ths_pid == 99999
        ths_app.kill_ths()
        assert tk_calls and tk_calls[0][:2] == ["taskkill", "/PID"]
        assert "99999" in tk_calls[0]
        assert ths_app._ths_pid is None
        # 第二次 kill 为空操作
        ths_app.kill_ths()
        assert len(tk_calls) == 1
    finally:
        ths_app._launched_once = False   # 清理模块态，不影响其他用例
        ths_app._ths_pid = None


def test_launch_does_not_record_when_already_once(monkeypatch):
    import subprocess
    import os

    class FakePopen:
        def __init__(self, *a, **kw):
            self.pid = 88888

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    monkeypatch.setattr(config, "THS_DEBUG_MODE", False)
    monkeypatch.setattr(ths_app, "_launched_once", True)   # 已启动过 → 不再拉起
    monkeypatch.setattr(ths_app, "_ths_pid", None)
    try:
        assert ths_app.launch_ths() is False
        assert ths_app._ths_pid is None
    finally:
        ths_app._launched_once = False
        ths_app._ths_pid = None
