# -*- coding: utf-8 -*-
"""第94轮 A4/A5/A6 http_client 测试：每源会话+cookie 持久化 / 限流退避 / dev 缓存重放。
全零网络（只调模块内部纯逻辑与缓存读写，不真正发请求）。"""
import os
import time

import http_client
import requests


def test_get_session_per_source():
    s1 = http_client.get_session("src_a")
    s2 = http_client.get_session("src_a")
    s3 = http_client.get_session("src_b")
    assert s1 is s2                      # 同源复用
    assert s1 is not s3                  # 异源隔离
    assert http_client.get_session(None) is http_client.SESSION
    assert http_client.get_session("__global__") is http_client.SESSION


def test_cookie_persist_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(http_client, "_COOKIE_FILE", str(tmp_path / "cookies.json"))
    monkeypatch.setattr(http_client, "_PERSIST_DEBOUNCE", 0.0)
    sess = http_client.get_session("src_cookie")
    sess.cookies.set("PHPSESSID", "abc123")
    http_client._persist_cookies(force=True)
    assert (tmp_path / "cookies.json").exists()
    # 新进程视角：另一个源会话读取持久化文件内容正确
    import json
    store = json.load(open(tmp_path / "cookies.json", encoding="utf-8"))
    assert store["src_cookie"]["PHPSESSID"] == "abc123"


def test_throttle_decision_and_record(monkeypatch):
    monkeypatch.setattr(http_client, "THROTTLE_DISABLED", False)
    # 干净状态：不发请求不阻塞
    now = time.monotonic()
    blocked, wait = http_client._throttle_decision("h.test", now)
    assert not blocked and wait == 0.0
    # 429 → 进入退避
    http_client._record_result("h.test", 429, None, now)
    blocked, wait = http_client._throttle_decision("h.test", now + 0.1)
    assert blocked and wait > 0
    # 退避到期后恢复
    st = http_client._THROTTLE["h.test"]
    http_client._THROTTLE["h.test"] = {"fail": st["fail"], "backoff_until": 0.0}
    blocked, wait = http_client._throttle_decision("h.test", time.monotonic())
    assert not blocked and wait == 0.0
    # 成功逐步恢复 fail 计数
    http_client._record_result("h.test", 200, None, time.monotonic())
    assert http_client._THROTTLE["h.test"]["fail"] == 0


def test_retry_after_honored(monkeypatch):
    monkeypatch.setattr(http_client, "THROTTLE_DISABLED", False)
    t0 = time.monotonic()
    http_client._record_result("h.ra", 200, "45", t0)   # 200 但带 Retry-After
    blocked, wait = http_client._throttle_decision("h.ra", t0 + 1)
    assert blocked and 35 <= wait <= 55   # 含±10%抖动


def test_synthetic_503_not_hitting_network(monkeypatch):
    monkeypatch.setattr(http_client, "THROTTLE_DISABLED", False)
    t0 = time.monotonic()
    http_client._record_result("h.blk", 429, None, t0)
    resp = http_client._synthetic_503("https://h.blk/x")
    assert resp.status_code == 503 and resp.text == ""


def test_dev_cache_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(http_client, "_DEV_CACHE", True)
    monkeypatch.setattr(http_client, "_DEV_DIR", str(tmp_path))
    # 模拟一次真实响应并写入缓存
    resp = requests.models.Response()
    resp.status_code = 200
    resp.url = "https://x.test/api"
    resp.encoding = "utf-8"
    resp._content = '{"ok": 1}'.encode("utf-8")
    resp.headers = {"Content-Type": "application/json"}
    http_client._dev_store("GET", resp.url, "src_x", resp)
    # 重放：同键命中且内容一致
    r2 = http_client._dev_replay("GET", resp.url, "src_x")
    assert r2 is not None and r2.status_code == 200
    assert r2.json()["ok"] == 1
    # 不同键不命中
    assert http_client._dev_replay("GET", resp.url, "other") is None
    monkeypatch.setattr(http_client, "_DEV_CACHE", False)
    assert http_client._dev_replay("GET", resp.url, "src_x") is None