# -*- coding: utf-8 -*-
"""【需求①】新闻/消息数据源：新浪财经7x24直播 + 金十数据快讯（每60秒抓取一次，
供 factors.NewsFactor 生成新闻情绪判断因子；report.append_daily_news 同步缓存当日新闻供需求⑩复盘）"""
import json
import re
from datetime import datetime

import config
from http_client import http
from utils import LOG, sanitize


def fetch_sina_zhibo():
    """新浪财经7x24全球实时财经直播（zhibo_id=152）"""
    url = ("https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=30"
           "&zhibo_id=152&tag_id=0&dire=f&dpc=1")
    r = http.get(url, headers=config.HEADERS_COMMON, timeout=config.TIMEOUT)
    r.encoding = "utf-8"
    r.raise_for_status()
    data = r.json()
    items = (data.get("result", {}).get("data", {})
                 .get("feed", {}).get("list")) or []
    out = []
    for it in items:
        content = sanitize((it.get("rich_text") or "").strip())
        if not content:
            continue
        dt = datetime.now()
        try:
            dt = datetime.strptime(it.get("create_time", ""), "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
        out.append({"source": "新浪7x24", "time": dt,
                    "content": content, "important": False})
    return out


def fetch_jin10():
    """金十数据快讯（官网 flash_newest.js，含全部最新快讯）"""
    url = "https://www.jin10.com/flash_newest.js"
    r = http.get(url, headers=config.HEADERS_COMMON, timeout=config.TIMEOUT)
    r.encoding = "utf-8"
    r.raise_for_status()
    m = re.search(r"\[.*\]", r.text, re.S)
    if not m:
        return []
    items = json.loads(m.group(0))
    out = []
    for it in items:
        if not isinstance(it, dict) or it.get("type") not in (0, None):
            continue
        d = it.get("data") or {}
        content = d.get("content") or d.get("title") or ""
        content = sanitize(re.sub(r"<[^>]+>", "", content)).strip()
        if not content:
            continue
        dt = datetime.now()
        try:
            dt = datetime.strptime(it.get("time", ""), "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
        important = bool(it.get("important"))
        out.append({"source": "金十数据", "time": dt,
                    "content": content, "important": important})
    return out


def fetch_all_news():
    """抓取全部新闻源，单个源失败不影响其他源"""
    news = []
    for fn in (fetch_sina_zhibo, fetch_jin10):
        try:
            news.extend(fn())
        except Exception as e:
            LOG.warning("新闻源 %s 获取失败: %s", fn.__name__, e)
    return news
