# -*- coding: utf-8 -*-
"""全局 HTTP 连接池（P0-5）：全项目共用一个 requests.Session，
复用 TCP/TLS 连接（此前每个请求都新建连接，64 品种 × 多数据源每轮握手开销大）。

用法（与 requests.get/post/put 参数完全兼容，可直接替换）：
    from http_client import http
    r = http.get(url, headers=..., timeout=...)
也可直接取会话：from http_client import SESSION / get_session(source)

说明：
- 默认带浏览器 UA；调用处传入的 headers 会与会话默认头合并（同名以调用处为准）。
- timeout 默认取 config.TIMEOUT，调用处仍可自行指定。
- 不在这里做自动重试：业务层已有各自的重试/降级逻辑，避免重复放大请求。

第94轮（对标 scrapling 的 session 持久化 / AutoThrottle / dev-mode 缓存）新增：
- A4 每源独立会话 + cookie 持久化：get_session(source) 返回按源缓存的 Session，cookie 落
  cache/cookies.json（重启续用）；默认 http 仍走全局 SESSION，行为与旧版逐字节一致。
- A5 请求级限流退避：识别 429/503/Retry-After，指数退避+抖动；退避期内返回合成 503
  （不发请求）；成功逐步恢复。默认开启（仅在真实被限流时才会改变时序，正常路径零影响）。
- A6 dev 缓存/重放：env FUTURES_MONITOR_DEV_CACHE=1 时按 (源,url,日期) 落
  cache/http_replay/，同键命中直接重放不发请求（调试/复现解析问题用，默认关）。
"""
import base64
import hashlib
import json
import os
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

import config

SESSION = requests.Session()
# 连接池：后台线程（原油/全网/外部数据/浏览器）+ 主循环会并发请求，池大小留足余量
_ADAPTER = HTTPAdapter(pool_connections=12, pool_maxsize=32, max_retries=0)
SESSION.mount("https://", _ADAPTER)
SESSION.mount("http://", HTTPAdapter(pool_connections=6, pool_maxsize=16, max_retries=0))
SESSION.headers.update({"User-Agent": config.HEADERS_COMMON["User-Agent"],
                        "Connection": "keep-alive"})

_LOCK = threading.Lock()

# ---------------- A4：每源独立会话 + cookie 持久化 ----------------

_SESSIONS = {}                 # source -> requests.Session
_COOKIE_FILE = getattr(config, "HTTP_COOKIE_JAR", os.path.join(config.BASE_DIR, "cache", "cookies.json"))
_PERSIST_DEBOUNCE = 30.0       # cookie 落盘节流（秒），避免每请求写盘
_LAST_PERSIST = [0.0]


def _load_persisted_cookies():
    try:
        with open(_COOKIE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _persist_cookies(force=False):
    """把全局+各源会话 cookie 汇总落盘（节流）；失败静默（cookie 丢了下次重拿，不影响主链）。"""
    now = time.monotonic()
    if not force and now - _LAST_PERSIST[0] < _PERSIST_DEBOUNCE:
        return
    _LAST_PERSIST[0] = now
    try:
        store = {}
        for name, sess in list(_SESSIONS.items()) + [("__global__", SESSION)]:
            cd = {k: v for k, v in sess.cookies.items()}
            if cd:
                store[name] = cd
        os.makedirs(os.path.dirname(_COOKIE_FILE), exist_ok=True)
        tmp = _COOKIE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=0)
        os.replace(tmp, _COOKIE_FILE)
    except Exception:
        pass


def get_session(source="__global__"):
    """返回按 source 缓存的独立 Session（A4）：cookie 与全局隔离、重启续用。
    source 未指定/默认路径仍返回全局 SESSION（等价旧版）。"""
    if source in (None, "", "__global__"):
        return SESSION
    with _LOCK:
        sess = _SESSIONS.get(source)
        if sess is None:
            sess = requests.Session()
            sess.mount("https://", _ADAPTER)
            sess.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0))
            sess.headers.update({"User-Agent": config.HEADERS_COMMON["User-Agent"],
                                 "Connection": "keep-alive"})
            saved = _load_persisted_cookies().get(source)
            if saved:
                try:
                    sess.cookies.update(saved)
                except Exception:
                    pass
            _SESSIONS[source] = sess
        return sess


# ---------------- A5：请求级限流退避（对标 scrapling AutoThrottle / blocked detection） ----------------

_THROTTLE = {}                 # host -> {"fail": 连续失败, "backoff_until": monotonic, "last": 单调时间}
_RETRYABLE = {429, 503}
THROTTLE_DISABLED = not getattr(config, "HTTP_THROTTLE_ENABLED", True)


def _host_of(url):
    try:
        return urlparse(url).netloc or "unknown"
    except Exception:
        return "unknown"


def _throttle_decision(host, now):
    """返回 (should_block, 建议退避秒数)。退避期内不发请求，直接返回合成 503。"""
    if THROTTLE_DISABLED:
        return False, 0.0
    with _LOCK:
        st = _THROTTLE.get(host)
        if not st or st["backoff_until"] <= now:
            return False, 0.0
        return True, st["backoff_until"] - now


def _record_result(host, status, retry_after, now=None):
    """请求结果登记：429/503 → 指数退避（含 Retry-After 覆盖）；成功 → 逐步恢复。"""
    if THROTTLE_DISABLED:
        return
    now = now if now is not None else time.monotonic()
    with _LOCK:
        st = _THROTTLE.setdefault(host, {"fail": 0, "backoff_until": 0.0})
        st["last"] = now
        if status in _RETRYABLE or (retry_after is not None and int(retry_after) > 0):
            st["fail"] += 1
            backoff = float(retry_after) if retry_after is not None and str(retry_after).strip().isdigit() \
                else min(getattr(config, "HTTP_THROTTLE_BACKOFF0", 5.0) * (2 ** (st["fail"] - 1)),
                         getattr(config, "HTTP_THROTTLE_MAX", 120.0))
            # 抖动 ±20%，防止多进程/多线程同步打点
            backoff *= (1.0 + ((hash(host) % 20) - 10) / 100.0)
            st["backoff_until"] = now + max(backoff, 1.0)
        else:
            st["fail"] = max(0, st["fail"] - 1)
            if st["fail"] == 0:
                st["backoff_until"] = 0.0


def _synthetic_503(url):
    resp = requests.models.Response()
    resp.status_code = 503
    resp.url = url
    resp.encoding = "utf-8"
    resp._content = b""
    resp.headers = {"Retry-After": "1"}
    return resp


# ---------------- A6：dev 模式响应缓存/重放（默认关） ----------------

_DEV_CACHE = os.environ.get("FUTURES_MONITOR_DEV_CACHE", "0") == "1"
_DEV_DIR = os.path.join(config.BASE_DIR, "cache", "http_replay")


def dev_cache_enabled():
    return _DEV_CACHE


def _dev_key(method, url, source):
    return hashlib.sha1(f"{source}|{method}|{url}".encode("utf-8")).hexdigest()[:20]


def _dev_replay(method, url, source):
    if not _DEV_CACHE:
        return None
    day = datetime.now().strftime("%Y%m%d")
    path = os.path.join(_DEV_DIR, day, _dev_key(method, url, source) + ".json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            blob = json.load(f)
        resp = requests.models.Response()
        resp.status_code = blob["status"]
        resp.url = url
        resp.encoding = blob.get("encoding") or "utf-8"
        resp.headers = blob.get("headers") or {}
        resp._content = base64.b64decode(blob["content"])
        return resp
    except (OSError, ValueError, KeyError):
        return None


def _dev_store(method, url, source, resp):
    if not _DEV_CACHE or resp is None:
        return
    day = datetime.now().strftime("%Y%m%d")
    d = os.path.join(_DEV_DIR, day)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, _dev_key(method, url, source) + ".json")
    blob = {"status": resp.status_code, "encoding": getattr(resp, "encoding", None) or "utf-8",
            "headers": {k: v for k, v in (resp.headers or {}).items()},
            "content": base64.b64encode(resp.content).decode("ascii")}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(blob, f)
    except OSError:
        pass


class _Http:
    """薄包装：把默认 timeout 注入，其余参数原样透传；A5 退避 / A6 缓存在此层统一处理。"""

    @staticmethod
    def _request(method, url, source, **kwargs):
        kwargs.setdefault("timeout", config.TIMEOUT)
        host = _host_of(url)
        # A6：dev 缓存命中直接重放（默认关）
        replay = _dev_replay(method, url, source or "")
        if replay is not None:
            return replay
        # A5：退避期内不发请求（合成 503）
        blocked, _ = _throttle_decision(host, time.monotonic())
        if blocked:
            return _synthetic_503(url)
        sess = get_session(source)
        resp = sess.request(method, url, **kwargs)
        # A5：登记结果（429/503/Retry-After → 指数退避；成功恢复）
        _record_result(host, resp.status_code, resp.headers.get("Retry-After") if resp.headers else None)
        # A4：cookie 节流落盘
        _persist_cookies()
        # A6：写缓存（供下次重放）
        _dev_store(method, url, source or "", resp)
        return resp

    @staticmethod
    def get(url, **kwargs):
        return _Http._request("GET", url, kwargs.pop("source", None), **kwargs)

    @staticmethod
    def post(url, **kwargs):
        return _Http._request("POST", url, kwargs.pop("source", None), **kwargs)

    @staticmethod
    def put(url, **kwargs):
        return _Http._request("PUT", url, kwargs.pop("source", None), **kwargs)

    @staticmethod
    def request(method, url, **kwargs):
        return _Http._request(method.upper(), url, kwargs.pop("source", None), **kwargs)


http = _Http()
