# -*- coding: utf-8 -*-
"""【需求①】布伦特/纽约(WTI)原油实时行情：新浪外盘 hf_ 接口，每10秒刷新一次
（main.oil_loop 驱动），并基于滚动窗口计算原油动量因子（供能化品种联动）。
【需求⑦】direction() 提供原油隔夜方向，供非交易时段预测走向投票。
【增强⑪】detect_jump() 原油短时急动检测：窗口内涨跌幅超阈值即触发紧急轮动。

接口实测字段（hf_OIL 示例）:
  88.331,,88.280,88.290,88.750,87.260,05:59:54,88.520,88.510,0,1,4,2026-08-29,布伦特原油,267339
  [0]最新价 [4]最高 [5]最低 [6]时间 [7]昨结 [8]开盘 [12]日期
"""
import math
import re
import threading
import time
from collections import deque

import config
from http_client import http
from utils import clip, fmt_pct, fmt_px

OIL_LIST = [("布伦特原油", "hf_OIL"), ("纽约原油", "hf_CL")]


def _f(s):
    try:
        v = float(s)
        return v if v == v else 0.0   # 过滤NaN
    except (TypeError, ValueError):
        return 0.0


def fetch_oil_quotes():
    """拉取布伦特(hf_OIL)与纽约原油(hf_CL)实时报价"""
    codes = ",".join(c for _, c in OIL_LIST)
    url = "https://hq.sinajs.cn/list=" + codes
    r = http.get(url, headers=config.HEADERS_SINA, timeout=config.TIMEOUT)
    r.encoding = "gbk"
    quotes = {}
    for name, code in OIL_LIST:
        m = re.search(r'hq_str_%s="([^"]*)"' % code, r.text)
        if not m:
            continue
        f = m.group(1).split(",")
        if len(f) < 13 or not f[0]:
            continue
        price = _f(f[0])
        if price <= 0:
            continue
        prev = _f(f[7])
        quotes[name] = {
            "price": price,
            "high": _f(f[4]),
            "low": _f(f[5]),
            "open": _f(f[8]),
            "prev_settle": prev,
            "day_chg": (price / prev - 1.0) if prev > 0 else 0.0,
            "time": (f[12] or "") + " " + (f[6] or ""),
        }
    return quotes


class OilTracker:
    """维护原油10秒级价格序列，计算多周期动量因子"""

    def __init__(self):
        self.hist = {name: deque(maxlen=3600) for name, _ in OIL_LIST}
        self.last_quotes = {}
        self.lock = threading.Lock()
        self._last_jump_ts = 0.0    # 上一次急动紧急触发时间（冷却控制）

    def update(self, quotes):
        if not quotes:
            return
        with self.lock:
            self.last_quotes.update(quotes)
            now = time.time()
            for name, q in quotes.items():
                self.hist[name].append((now, q["price"]))

    def detect_jump(self):
        """检测原油短时急涨急跌（10s刷新时调用）：窗口内布伦特/WTI任一涨跌幅绝对值
        达到 config.OIL_JUMP_REL 即返回波动信息；冷却期内或数据不足返回 None。"""
        window = config.OIL_JUMP_WINDOW_SEC
        threshold = config.OIL_JUMP_REL
        now = time.time()
        if now - self._last_jump_ts < config.OIL_JUMP_COOLDOWN_SEC:
            return None
        hit = None
        with self.lock:
            for name, _ in OIL_LIST:
                h = self.hist[name]
                if len(h) < 2:
                    continue
                now_ts, now_px = h[-1]
                target = now_ts - window
                base = None
                for ts, px in h:                # 窗口起点附近最早的一个价格
                    if ts >= target:
                        base = px
                        break
                if not base:                   # 运行不足一个窗口：用最早价（更严格）
                    base = h[0][1]
                if base <= 0:
                    continue
                ret = now_px / base - 1.0
                if abs(ret) >= threshold and (hit is None or abs(ret) > abs(hit["ret"])):
                    hit = {"name": name, "ret": ret, "price": now_px,
                           "base": base, "window_sec": int(now_ts - h[0][0])}
        if hit:
            self._last_jump_ts = now
        return hit

    def _ret(self, name, minutes):
        """minutes分钟前的价格到现在的涨跌幅"""
        with self.lock:
            h = self.hist[name]
            if len(h) < 2:
                return 0.0
            now_ts, now_px = h[-1]
            target = now_ts - minutes * 60
            base = None
            for ts, px in h:
                if ts >= target:
                    base = px
                    break
            if not base:
                return 0.0
            return now_px / base - 1.0

    def _span(self, name):
        with self.lock:
            h = self.hist[name]
            if len(h) < 2:
                return 0.0
            return h[-1][0] - h[0][0]

    def _score_one(self, name):
        r5, r15, r60 = (self._ret(name, m) for m in (5, 15, 60))
        q = self.last_quotes.get(name) or {}
        day = q.get("day_chg", 0.0)
        s = (math.tanh(r5 * 1500) * 1.2 +
             math.tanh(r15 * 1200) * 1.6 +
             math.tanh(r60 * 700) * 1.2 +
             math.tanh(day * 450) * 1.5)
        return clip(s, -5.0, 5.0)

    def combined_score(self):
        """综合原油因子：布伦特60% + WTI 40%，范围约 -5 ~ +5"""
        b = self._score_one("布伦特原油")
        w = self._score_one("纽约原油")
        return 0.6 * b + 0.4 * w

    def trend_label(self, name):
        """5分钟EMA vs 20分钟EMA 判断短线趋势"""
        with self.lock:
            h = list(self.hist[name])
        if len(h) < 130:
            return "积累中"
        px = [p for _, p in h[-600:]]
        k = len(px)
        alpha = 2.0 / (1.0 + 30)
        fast = px[0]
        for p in px:
            fast = fast + alpha * (p - fast)
        alpha2 = 2.0 / (1.0 + 120)
        slow = px[0]
        for p in px:
            slow = slow + alpha2 * (p - slow)
        diff = (fast / slow - 1.0) if slow else 0.0
        if diff > 0.0006:
            return "偏多"
        if diff < -0.0006:
            return "偏空"
        return "震荡"

    def direction(self):
        """原油近期方向（用于预测走向）: +1偏多 / -1偏空 / 0中性"""
        v = 0.0
        for name, _ in OIL_LIST:
            q = self.last_quotes.get(name)
            day = q.get("day_chg", 0.0) if q else 0.0
            v += math.tanh(day * 450) * 0.6 + math.tanh(self._ret(name, 60) * 700) * 0.4
        v /= len(OIL_LIST)
        if v > 0.15:
            return 1
        if v < -0.15:
            return -1
        return 0

    def snapshot_line(self, verbose=False):
        """生成一行原油实时行情文本（每10秒刷新）"""
        parts = []
        for name, _ in OIL_LIST:
            q = self.last_quotes.get(name)
            if not q:
                parts.append("%s 等待数据" % name)
                continue
            r5, r15 = self._ret(name, 5), self._ret(name, 15)
            line = (f"{name} {fmt_px(q['price'])} ({fmt_pct(q['day_chg'])}) "
                    f"5m{fmt_pct(r5)} 15m{fmt_pct(r15)} 趋势:{self.trend_label(name)}")
            if verbose:
                line += f" [{q['time']}]"
            parts.append(line)
        bq = self.last_quotes.get("布伦特原油")
        wq = self.last_quotes.get("纽约原油")
        if bq and wq:
            parts.append(f"布伦特-WTI价差 {bq['price'] - wq['price']:.2f}")
        return " [原油10s] " + " | ".join(parts)
