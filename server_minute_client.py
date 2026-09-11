#!/usr/bin/env python3
"""云服务器分钟K采集客户端（第120轮）

从部署在云服务器上的 sina_proxy_server.py 拉取分钟K数据。
用于 MinuteCollector.collect 的优先出口：本机 IP 不碰新浪 stock2，永不被封。

特性：
- 11 台服务器 round-robin 轮换（自动跳过 456/失败的）
- 并发安全：每线程独立选择服务器，互不竞争
- 超时快速失败：单请求超时 20s，不影响主流程
- 返回格式与 fetch_sina_minute 完全一致：[{d,o,h,l,c,v,p,s}, ...]

与 tools/server_collect.py 的区别：本模块由主程序分钟K自采链路调用，
负责按"单品种单周期"实时拉取（非批量全品种调度），fallback 逻辑内置于 MinuteCollector。
"""
import time
import random
import threading
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import re, json

import config
from utils import LOG

# 服务器轮询状态（各线程独立轮询，互不干扰）
_lock = threading.Lock()
_next = 0
_dead = set()  # 已知失效的服务器（运行中被剔除；下次探测重新评估）

def _fetch_via_server(sina_code, period, lmt):
    """从云服务器拉取分钟K：round-robin 选台，失败自动换台重试。

    返回新浪原始结构 [{d,o,h,l,c,v,p,s}, ...] 或空列表 []（全部失败）。
    与 intraday_bars.fetch_sina_minute 返回格式完全一致，可直接传给 _sina_raw_to_bars。
    """
    servers = getattr(config, "SINA_SERVER_URLS", [])
    if not servers:
        return []
    timeout = getattr(config, "SINA_SERVER_TIMEOUT", 20.0)
    # 有效服务器数：总服务器减去已知失效的
    alive = [s for s in servers if s not in _dead]
    if not alive:
        # 所有标记失效的，重新探测一次（重置 _dead）
        with _lock:
            _dead.clear()
            alive = servers[:]
    if not alive:
        return []

    global _next
    tried = 0
    while tried < min(len(alive), 3):  # 最多试 3 台
        with _lock:
            srv = alive[_next % len(alive)]
            _next += 1
        tried += 1
        url = f"{srv}/minute?symbol={sina_code}&period={int(period)}&lmt={lmt or 20}"
        try:
            req = Request(url, headers={"User-Agent": "futures_monitor/1.0"})
            resp = urlopen(req, timeout=timeout)
            body = resp.read().decode("utf-8", "replace")
            if "456" in body[:128]:
                with _lock:
                    _dead.add(srv)
                    LOG.debug("服务器 %s 返回 456，暂剔除", srv)
                continue
            data = json.loads(body)
            if data and data.get("bars"):
                return data["bars"]
        except HTTPError as e:
            if e.code == 456:
                with _lock:
                    _dead.add(srv)
                    LOG.debug("服务器 %s HTTP 456，暂剔除", srv)
            continue
        except Exception:
            continue
    return []


def _fetch_daily_via_server(sina_code):
    """从云服务器拉取日线：round-robin 选台，失败自动换台重试。

    返回新浪原始结构 [{d,o,h,l,c,v,p,s}, ...] 或空列表 []（全部失败）。
    """
    servers = getattr(config, "SINA_SERVER_URLS", [])
    if not servers:
        return []
    timeout = getattr(config, "SINA_SERVER_TIMEOUT", 20.0)
    alive = [s for s in servers if s not in _dead]
    if not alive:
        with _lock:
            _dead.clear()
            alive = servers[:]
    if not alive:
        return []

    global _next
    tried = 0
    while tried < min(len(alive), 3):
        with _lock:
            srv = alive[_next % len(alive)]
            _next += 1
        tried += 1
        url = f"{srv}/daily?symbol={sina_code}"
        try:
            req = Request(url, headers={"User-Agent": "futures_monitor/1.0"})
            resp = urlopen(req, timeout=timeout)
            body = resp.read().decode("utf-8", "replace")
            if "456" in body[:128]:
                with _lock:
                    _dead.add(srv)
                    LOG.debug("服务器 %s 日线返回 456，暂剔除", srv)
                continue
            data = json.loads(body)
            if data and data.get("bars"):
                return data["bars"]
        except HTTPError as e:
            if e.code == 456:
                with _lock:
                    _dead.add(srv)
                    LOG.debug("服务器 %s 日线 HTTP 456，暂剔除", srv)
            continue
        except Exception:
            continue
    return []
