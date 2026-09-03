# -*- coding: utf-8 -*-
"""第57轮 G8 只读 Web/手机看板 tools/web_dashboard.py 的离线单测（纯逻辑，不起 socket）。"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOLS = os.path.join(_ROOT, "tools")
for p in (_ROOT, _TOOLS):
    if p not in sys.path:
        sys.path.insert(0, p)

import web_dashboard as wd       # noqa: E402


def test_safe_join_normal():
    root = os.path.abspath("reports")
    t = wd.safe_join(root, "/a.txt")
    assert t == os.path.join(root, "a.txt")
    # 中文 percent-decode
    t2 = wd.safe_join(root, "/%E5%9B%BE%E8%A1%A8.html")
    assert t2 is not None and t2.endswith(".html")
    # 根路径 -> root
    assert wd.safe_join(root, "") == root
    assert wd.safe_join(root, "/") == root
    # 查询串/锚点剥离
    assert wd.safe_join(root, "/a.txt?v=1#x").endswith("a.txt")


def test_safe_join_blocks_traversal():
    root = os.path.abspath("reports")
    assert wd.safe_join(root, "/../secret.txt") is None
    assert wd.safe_join(root, "/a/../../b.txt") is None
    assert wd.safe_join(root, "/..%2f..%2fsecret") is None
    assert wd.safe_join(root, "/C:/Windows/x") is None
    assert wd.safe_join(root, "/a\x00.txt") is None
    assert wd.safe_join(root, None) is None


def test_content_type():
    assert wd.content_type("x.html").startswith("text/html")
    assert wd.content_type("x.JS").startswith("application/javascript")
    assert wd.content_type("x.json").startswith("application/json")
    assert wd.content_type("x.bin") == "application/octet-stream"


def test_method_whitelist():
    assert wd.is_allowed_method("GET") and wd.is_allowed_method("HEAD")
    for m in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
        assert not wd.is_allowed_method(m)


def test_list_reports_priority_and_index(tmp_path):
    (tmp_path / "z.txt").write_text("z", encoding="utf-8")
    (tmp_path / "图表看板.html").write_text("<html>", encoding="utf-8")
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    ent = wd.list_reports(str(tmp_path))
    names = [e[0] for e in ent]
    assert names[0] == "图表看板.html"          # 入口优先
    assert names[1:] == sorted(names[1:])       # 其余按名
    idx = wd.render_index(ent, "tmp")
    assert "viewport" in idx and "只读" in idx and "图表看板.html" in idx
    # 大小/时间字段
    assert "KB" in idx or "B" in idx
    # 空目录
    empty = tmp_path / "empty"; empty.mkdir()
    assert wd.list_reports(str(empty)) == []
    assert "暂无文件" in wd.render_index([], "empty")


def test_index_escapes_filenames(tmp_path):
    (tmp_path / "x&y.txt").write_text("x", encoding="utf-8")
    idx = wd.render_index(wd.list_reports(str(tmp_path)))
    assert "x&amp;y.txt" in idx and "x&y.txt" not in idx


def test_handler_factory_readonly_methods():
    h = wd.make_handler(os.path.abspath("reports"))
    for m in ("do_GET", "do_HEAD", "do_POST", "do_PUT", "do_DELETE", "do_PATCH"):
        assert callable(getattr(h, m, None))
