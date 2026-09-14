#!/usr/bin/env python3
"""
新浪 stock2 采集代理服务器（第120轮·多开部署版）
部署到 5 台云服务器，每台对外暴露 HTTP API，本机调度器并行请求 5 台。

功能：
  - GET /daily?symbol=RB0   → 日线 [{d,o,h,l,c,v,p,s}, ...]
  - GET /minute?symbol=RB0&period=30&lmt=1023 → 分钟K

零依赖：只用 Python 标准库（http.server + urllib + threading），无需 pip install。
5 台分摊 320 任务：每台 64 任务/60s ≈ 64次/min（间隔 0.93s/请求），在安全线内。
本机 IP 完全不碰新浪，永不被封。

用法（5 台分别启动）：
  # 服务器A: python sina_proxy_server.py --port 9001 --gap 0.9
  # 服务器B: python sina_proxy_server.py --port 9001 --gap 0.9
  # ...（IP 不同，端口一致或不同均可）

验证：本机 curl http://<IP>:9001/daily?symbol=RB0
"""

import argparse
import json
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

# ---- 限流器 ----
_gate = threading.Lock()
_last_ts = 0.0
_gap = 0.9  # 基准间隔（秒）；实际等待 = gap * (0.7~1.3) 随机抖动，打散"等间隔"规律指纹
_GAP_JITTER = (0.7, 1.3)  # 随机抖动系数范围（防新浪识别"精准等间隔爬虫"）


def throttle():
    """全局限流：两次请求间隔 >= gap*抖动 秒。抖动化避免规律请求被 WAF 识别。"""
    global _last_ts
    with _gate:
        wait = _last_ts + _gap * random.uniform(*_GAP_JITTER) - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_ts = time.time()


# ---- 新浪请求 ----
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn/",
}


def _sina_get(url, timeout=10):
    """带限流的新浪 stock2 请求，返回响应文本或 None。"""
    throttle()
    req = Request(url, headers=HEADERS)
    try:
        resp = urlopen(req, timeout=timeout)
        body = resp.read().decode("utf-8", "replace")
        if resp.status == 456:
            return None, "456"
        if "456" in body[:128]:
            return None, "456"
        return body, None
    except HTTPError as e:
        if e.code == 456:
            return None, "456"
        return None, str(e)
    except Exception as e:
        return None, str(e)


def _parse_jsonp(text):
    """从 JSONP 响应中提取 K线数组。"""
    m = re.search(r"\((\[.*\])\)", text, re.S)
    if m and len(m.group(1)) > 50:
        return json.loads(m.group(1))
    return []


def fetch_daily(symbol):
    """日线：返回 bars list 或 None。"""
    url = (
        f"https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
        f"InnerFuturesNewService.getDailyKLine?symbol={symbol}"
    )
    text, err = _sina_get(url)
    if text is None:
        return None, err
    return _parse_jsonp(text), None


def fetch_minute(symbol, period, lmt):
    """分钟K：返回 bars list 或 None。"""
    url = (
        f"https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
        f"InnerFuturesNewService.getFewMinLine?symbol={symbol}&type={period}"
    )
    text, err = _sina_get(url)
    if text is None:
        return None, err
    bars = _parse_jsonp(text)
    if lmt and bars:
        bars = bars[-int(lmt) :]
    return bars, None


# ---- hq.sinajs.cn 期权成交量快照转发（第151轮） ----
_HQ_URL = "https://hq.sinajs.cn/etag.php?list=%s"


def fetch_pop_volumes(codes_str):
    """转发 hq.sinajs.cn/etag.php P_OP_ 批量快照 → {code: volume} 或 None。
    codes_str: 逗号分隔的完整代码（如 "P_OP_rb2701C3000,P_OP_rb2701P3000"），不需要带前缀。
    hq 域未被 456 封锁，秒级返回；走服务器限流器以分散负载、防未来封锁。"""
    url = _HQ_URL % codes_str
    throttle()  # 共享限流器（与 stock2 同频，防止服务器带宽/并发超限）
    req = Request(url, headers=HEADERS)
    try:
        resp = urlopen(req, timeout=8)
        text = resp.read().decode("gbk", errors="replace")
        out = {}
        for line in text.splitlines():
            m = re.match(r'var hq_str_P_OP_([A-Za-z0-9]+)="(.*)"', line)
            if not m:
                continue
            code = m.group(1)
            fields = m.group(2).split(",")
            # 第110轮：成交量字段 index 11（不同交易所有偏移，保守取 P_OP_ 快照第11位=成交量）
            # 验证：rb2610C3000 原始返回第11位=0.500（当日成交量）
            vol = 0.0
            if len(fields) > 11:
                try:
                    vol = float(fields[11])
                except (ValueError, IndexError):
                    pass
            if vol > 0:
                out[code] = vol
        return out
    except Exception as e:
        return None


# ---- HTTP Handler ----
_stats = {"daily": 0, "minute": 0, "fail": 0, "start": time.time()}
_stats_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 静默日志，避免刷屏

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/daily":
            symbol = params.get("symbol", [""])[0].upper()
            if not symbol:
                self._json({"error": "missing symbol"}, 400)
                return
            bars, err = fetch_daily(symbol)
            with _stats_lock:
                _stats["daily"] += 1
            if bars is None:
                with _stats_lock:
                    _stats["fail"] += 1
                self._json({"error": err, "symbol": symbol}, 502)
            else:
                self._json({"symbol": symbol, "count": len(bars), "bars": bars})

        elif parsed.path == "/minute":
            symbol = params.get("symbol", [""])[0].upper()
            period = int(params.get("period", ["30"])[0])
            lmt = int(params.get("lmt", ["0"])[0])
            if not symbol:
                self._json({"error": "missing symbol"}, 400)
                return
            bars, err = fetch_minute(symbol, period, lmt)
            with _stats_lock:
                _stats["minute"] += 1
            if bars is None:
                with _stats_lock:
                    _stats["fail"] += 1
                self._json({"error": err, "symbol": symbol, "period": period}, 502)
            else:
                self._json({"symbol": symbol, "period": period, "count": len(bars), "bars": bars})

        elif parsed.path == "/pops":
            # 第151轮：新浪 hq.sinajs.cn 期权成交量快照（P_OP_ 批量）；codes 逗号分隔不带前缀
            codes = params.get("codes", [""])[0].strip()
            if not codes:
                self._json(
                    {
                        "error": "missing codes",
                        "usage": "/pops?codes=P_OP_rb2701C3000,P_OP_rb2701P3000",
                    },
                    400,
                )
                return
            vols = fetch_pop_volumes(codes)
            with _stats_lock:
                _stats["pops"] = _stats.get("pops", 0) + 1
            if vols is None:
                with _stats_lock:
                    _stats["fail"] += 1
                self._json({"error": "hq fetch failed", "codes": codes}, 502)
            else:
                self._json({"count": len(vols), "volumes": vols})

        elif parsed.path == "/health":
            uptime = time.time() - _stats["start"]
            self._json({"status": "ok", "uptime_s": int(uptime), **_stats})

        else:
            self._json(
                {
                    "error": "unknown path",
                    "usage": "/daily?symbol=RB0 | /minute?symbol=RB0&period=30&lmt=1023 | /pops?codes=P_OP_xxx | /health",
                },
                404,
            )


# ---- Main ----
def main():
    global _gap
    parser = argparse.ArgumentParser(description="新浪 stock2 采集代理服务器")
    parser.add_argument("--port", type=int, default=9001, help="监听端口（默认 9001）")
    parser.add_argument(
        "--gap", type=float, default=0.8, help="每请求最小间隔（秒，默认 0.8 ≈ 75次/min）"
    )
    args = parser.parse_args()
    _gap = args.gap

    server = HTTPServer(("0.0.0.0", args.port), Handler)
    print(f"新浪采集代理已启动，端口 {args.port}，限流 {args.gap}s/请求")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.server_close()


if __name__ == "__main__":
    main()
