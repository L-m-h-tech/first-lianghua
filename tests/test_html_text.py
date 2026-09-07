# -*- coding: utf-8 -*-
"""第94轮 A3 html_text 统一文本/表格提取测试：零网络、确定性（lxml 与 stdlib 双后端都验）。"""
import pytest

import html_text


@pytest.fixture(autouse=True)
def _force_both_backends(monkeypatch):
    """同一组断言分别在 lxml 与 stdlib 后端下跑（模拟未装 lxml 的回退路径）。"""
    yield
    monkeypatch.setattr(html_text, "_HAS_LXML", True)


HTML = """<html><head><script>var x=1;</script><style>.a{color:red}</style>
<title>标题</title></head><body>
<p>螺纹钢 <b>RB2610</b></p><p>最新价 3156</p>
<table><tr><th>品种</th><th>库存</th></tr><tr><td>RB</td><td>123,456</td></tr></table>
</body></html>"""


def test_clean_text_removes_script_style():
    text = html_text.clean_text(HTML)
    assert "var x=1" not in text and ".a{color:red}" not in text
    assert "螺纹钢" in text and "RB2610" in text
    assert "最新价" in text


def test_clean_text_empty_and_bad():
    assert html_text.clean_text("") == ""
    assert html_text.clean_text(None) == ""
    assert html_text.clean_text("纯文本没有标签") == "纯文本没有标签"
    assert html_text.clean_text("<div><b>only bold</b></div>") == "only bold"


def test_extract_tables():
    tables = html_text.extract_tables(HTML)
    assert len(tables) == 1
    assert tables[0][0] == ["品种", "库存"]
    assert tables[0][1] == ["RB", "123,456"]


def test_extract_tables_none():
    assert html_text.extract_tables("<p>没有表格</p>") == []
    assert html_text.extract_tables("") == []


def test_find_anchor_line():
    txt = "\n".join(["第一行随便", "库存日报表", "品种 数值", "RB 12345"])
    assert html_text.find_anchor_line(txt, ["库存日报", "仓单日报"]) == 1
    assert html_text.find_anchor_line(txt, ["完全不存在的内容"]) == -1
    assert html_text.find_anchor_line("", ["x"]) == -1


def test_stdlib_fallback_path(monkeypatch):
    """强制走纯标准库后端（模拟 lxml 缺失），输出与 lxml 一致。"""
    monkeypatch.setattr(html_text, "_HAS_LXML", False)
    assert "螺纹钢" in html_text.clean_text(HTML)
    tables = html_text.extract_tables(HTML)
    assert tables and tables[0][1] == ["RB", "123,456"]
    assert html_text.backend() == "stdlib"


def test_clean_text_max_len():
    text = html_text.clean_text("<p>abcdefghij</p>", max_len=5)
    assert len(text) == 5


def test_first_valid_candidate_chain():
    """A2：多候选解析链——首个通过校验的候选命中，全失败返回 None。"""
    candidates = [("bad", lambda: {"n": 0}),
                  ("good", lambda: {"n": 5}),
                  ("never", lambda: {"n": 9})]
    label, res = html_text.first_valid(candidates, lambda r: r["n"] > 0)
    assert label == "good" and res["n"] == 5
    label2, res2 = html_text.first_valid([("x", lambda: {"n": 0})], lambda r: r["n"] > 0)
    assert label2 is None and res2 is None
    # 异常候选被跳过
    label3, res3 = html_text.first_valid(
        [("boom", lambda: 1 / 0), ("ok", lambda: 42)], lambda r: r == 42)
    assert label3 == "ok" and res3 == 42
