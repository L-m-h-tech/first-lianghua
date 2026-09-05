# -*- coding: utf-8 -*-
"""G13 LLM 第二意见复核适配层测试：触发器/裁剪/三降级/无key零开销/异常安全（mock transport，零网络）。"""
import json
import os

import pytest

import llm_reviewer as lr


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv("FUTURES_MONITOR_LLM_KEY", "test-key")
    monkeypatch.delenv("FUTURES_MONITOR_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("FUTURES_MONITOR_LLM_MODEL", raising=False)


ROWS = [{"name": "螺纹钢", "score": 7.2, "tech": 2.0, "fundamental": 1.0,
         "parts": {"新闻消息面": 2.5}, "advice": "买入"},
        {"name": "铜", "score": -3.0, "tech": -2.5, "fundamental": 0.5,
         "parts": {"新闻消息面": 2.0}, "advice": "卖出"},
        {"name": "豆粕", "score": 1.0, "tech": 1.0, "fundamental": 0.0,
         "parts": {"新闻消息面": 0.2}, "advice": "观望"}]


def test_no_key_zero_cost(monkeypatch):
    monkeypatch.delenv("FUTURES_MONITOR_LLM_KEY", raising=False)
    assert lr.enabled() is False
    called = {"n": 0}

    def transport(url, payload, timeout):
        called["n"] += 1
        return 200, "{}"

    assert lr.review(ROWS, transport=transport) is None
    assert lr.review_async(ROWS, transport=transport) is None
    assert called["n"] == 0                    # 零请求零开销


def test_triggers_three_kinds():
    assert lr.triggers([ROWS[2]]) == []        # 未达任何触发器（豆粕各行均弱）
    t = lr.triggers(ROWS, emergency={"src": "oil"})
    assert t[0][0] == "emergency" and t[1][0] == "strong_signal" and t[2][0] == "divergence"


def test_parse_and_clip():
    bad = ('{"direction":"暴涨","strength":9,"symbols":["a","b"],"uncertainty":5,'
           '"reason":"' + "x" * 999 + '","agrees_with_lexicon":"yes"}')
    p = lr.parse_review(bad)
    assert p["direction"] == "中性" and p["strength"] == 5 and p["uncertainty"] == 1.0
    assert p["agrees_with_lexicon"] is True and len(p["reason"]) <= 300
    ok = lr.parse_review('```json\n{"direction":"多","strength":4}\n```')
    assert ok["direction"] == "多" and ok["strength"] == 4


def test_degrade_paths(with_key):
    # 非200 → http_503
    assert lr.review(ROWS, transport=lambda u, p, t: (503, "x"))["degraded"] == "http_503"
    # 200 但响应体坏 → bad_response_body
    assert lr.review(ROWS, transport=lambda u, p, t: (200, "not json"))["degraded"] == "bad_response_body"
    # 200 响应体合法但内容坏 JSON → bad_json
    assert lr.review(ROWS, transport=lambda u, p, t:
                     (200, '{"choices":[{"message":{"content":"not json"}}]}'))["degraded"] == "bad_json"
    # transport 抛异常 → 兜底降级，绝不外溢
    def boom(url, payload, timeout):
        raise RuntimeError("boom")
    assert "exception:RuntimeError" in lr.review(ROWS, transport=boom)["degraded"]


def test_full_flow_writes_sidecar(with_key, tmp_path):
    txt = tmp_path / "llm_review.txt"
    jsonl = tmp_path / "llm_review_history.jsonl"

    def transport(url, payload, timeout):
        body = {"choices": [{"message": {"content": json.dumps(
            {"direction": "多", "strength": 4, "symbols": ["螺纹钢"],
             "uncertainty": 0.3, "reason": "趋势与库存同向", "agrees_with_lexicon": True})}}]}
        return 200, json.dumps(body)

    r = lr.review(ROWS, transport=transport)
    assert r["direction"] == "多" and r["strength"] == 4
    lr.persist(r, txt_path=txt, jsonl_path=jsonl)
    assert "G13" in txt.read_text(encoding="utf-8")
    hist = json.loads(jsonl.read_text(encoding="utf-8").splitlines()[-1])
    assert hist["review"]["direction"] == "多"


def test_review_async_never_raises(with_key, tmp_path):
    def boom(url, payload, timeout):
        raise RuntimeError("x")
    # 全 try/except 包裹：即便降级/写盘路径出错也不外溢
    assert lr.review_async(ROWS, transport=boom) is None or True
    assert lr.review_async(ROWS, transport=lambda u, p, t: (200, "bad")) is not None
