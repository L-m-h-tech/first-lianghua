# -*- coding: utf-8 -*-
"""全局 HTTP 连接池（P0-5）：全项目共用一个 requests.Session，
复用 TCP/TLS 连接（此前每个请求都新建连接，64 品种 × 多数据源每轮握手开销大）。

用法（与 requests.get/post/put 参数完全兼容，可直接替换）：
    from http_client import http
    r = http.get(url, headers=..., timeout=...)
也可直接取会话：from http_client import SESSION

说明：
- 默认带浏览器 UA；调用处传入的 headers 会与会话默认头合并（同名以调用处为准）。
- timeout 默认取 config.TIMEOUT，调用处仍可自行指定。
- 不在这里做自动重试：业务层已有各自的重试/降级逻辑，避免重复放大请求。
"""
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


class _Http:
    """薄包装：把默认 timeout 注入，其余参数原样透传给全局 Session"""

    @staticmethod
    def get(url, **kwargs):
        kwargs.setdefault("timeout", config.TIMEOUT)
        return SESSION.get(url, **kwargs)

    @staticmethod
    def post(url, **kwargs):
        kwargs.setdefault("timeout", config.TIMEOUT)
        return SESSION.post(url, **kwargs)

    @staticmethod
    def put(url, **kwargs):
        kwargs.setdefault("timeout", config.TIMEOUT)
        return SESSION.put(url, **kwargs)

    @staticmethod
    def request(method, url, **kwargs):
        kwargs.setdefault("timeout", config.TIMEOUT)
        return SESSION.request(method, url, **kwargs)


http = _Http()
