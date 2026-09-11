# -*- coding: utf-8 -*-
"""分钟K线数据层（新浪主连全周期唯一源 + 通用周期聚合）。

为什么需要：
  - 日内/平今回测（第15轮 WP-D1/D2）必须有带时间戳的分钟 bar；免费源历史分钟窗口有限，
    长期、自有、永不丢的分钟库根本上靠程序 7×24 常驻、每几分钟自采一次滚动积累。

选源实测定型（2026-09-01 晚补测纠正第14轮"新浪无1分钟"的误判；第118轮用户决策删除其余源）：
  * 新浪主连 getFewMinLine（**唯一分钟K源，全周期 1/5/15/30/60m**）：主连代码直接给（RB0，无需
    合约转换/换月跟随），每个周期固定 1023 根——实测 1m≈2.5个交易日、5m≈3周、15m≈3月、
    30m≈6月、60m≈12.5月，64/64 品种全覆盖、零断连、单请求0.1s级。字段 d/o/h/l/c/v/p（p=持仓量）。
  * ~~东财 push2his~~（第118轮删除）：具体合约兜底，本机持续 TLS 指纹封锁（RemoteDisconnected），
    删除 CDP/curl 调试浏览器兜底——不再向被封锁域名发请求、不再拉起调试浏览器空白页。
  * ~~通达信 pytdx~~（第118轮删除）：公共 7709 只同步股票、期货所在 7727 不可达，直接删除。

能力天花板（诚实声明）：免费源无历史 L2 逐笔；新浪主连是比例复权连续序列（换月点为近似）；
分钟长期历史靠常驻自采滚动积累。
"""
import json
import threading
import time
from datetime import datetime, timedelta

import config
from http_client import http
from data_router import REGISTRY
from utils import LOG

# 新浪主连分钟K支持的周期（分钟）；2026-09-01 晚补测 type=1（一分钟）同样返回1023根
SINA_MIN_PERIODS = (1, 5, 15, 30, 60)


# ---------------- 新浪主连分钟K（主源，主连代码 RB0，5/15/30/60m） ----------------

def fetch_sina_minute(sina_code, ex, period, lmt=None):
    """新浪主力连续分钟K（支持 1/5/15/30/60m），升序返回统一 bar dict；不支持的周期/任何失败返回 []。

    复用 futures_data.fetch_intraday_kline（走全局 http_client 连接池、自带重试），
    返回原始字段 d 时间(秒级)、o/h/l/c、v 成交量、p 持仓量（无成交额，amount 记 0）。
    主连 contract 直接用 sina_code（如 RB0），与具体合约 bar 在 minute_bars 表中按 sym 共存。
    """
    period = int(period)
    if period not in SINA_MIN_PERIODS:
        return []
    try:
        from futures_data import fetch_intraday_kline  # 延迟导入：避免数据层之间的循环导入
        raw = fetch_intraday_kline(str(sina_code), period=period, retry=1)
    except Exception:
        return []
    sym = "".join(ch for ch in str(sina_code) if ch.isalpha()).upper()
    bars = []
    for r in raw or []:
        dt = str(r.get("d") or "")[:16]
        try:
            o, h, l, c = float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])
        except (KeyError, TypeError, ValueError):
            continue
        if c <= 0 or not dt:
            continue
        bars.append({"dt": dt, "trade_date": dt[:10], "o": o, "h": h, "l": l, "c": c,
                     "v": float(r.get("v") or 0), "amount": 0.0,
                     "sym": sym, "contract": str(sina_code).upper(),
                     "exchange": ex, "period": period, "src": "sina"})
    bars.sort(key=lambda b: b["dt"])
    if lmt:
        bars = bars[-int(lmt):]
    return bars


# ---------------- 多源统一采集器：新浪主连优先，代理池/天勤兜底 ----------------
# 第118轮（用户决策）：删除东财/通达信分钟K采集（东财 push2his 按 TLS 指纹持续封锁、
# 通达信公共服务器 7727 不可达；东财 CDP/curl 调试浏览器兜底一并删除——不再向被封锁
# 域名发请求、不再拉起调试浏览器空白页）。新浪 stock2（日线+分钟K同域）被 WAF 456 封锁时，
# 先代理池（秒级独立出口绕过），再天勤 TqSdk（独立通道）。

def _sina_raw_to_bars(raw, sina_code, ex, period):
    """把新浪 getFewMinLine 原始返回 [{d,o,h,l,c,v,p,s}] 转成统一 bar 格式（与 fetch_sina_minute 对齐）。"""
    sym = "".join(ch for ch in str(sina_code) if ch.isalpha()).upper()
    bars = []
    for r in raw or []:
        dt = str(r.get("d") or "")[:16]
        try:
            o, h, l, c = float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])
        except (KeyError, TypeError, ValueError):
            continue
        if c <= 0 or not dt:
            continue
        bars.append({"dt": dt, "trade_date": dt[:10], "o": o, "h": h, "l": l, "c": c,
                     "v": float(r.get("v") or 0), "amount": 0.0,
                     "sym": sym, "contract": str(sina_code).upper(),
                     "exchange": ex, "period": int(period), "src": "proxy"})
    bars.sort(key=lambda b: b["dt"])
    return bars


class MinuteCollector:
    """对单个品种单个周期选源采集：新浪主连优先，代理池/天勤 TqSdk 兜底。

    新浪主连（1/5/15/30/60m，稳、深、免换月，type=1一分钟K同样1023根）为主源；
    新浪 stock2 被 WAF 456 封锁时——先代理池（第118轮：代理 IP 独立出口秒级绕过），
    再天勤 TqSdk（独立通道）。任一失败返回 ([], "")，调用方只计数不阻断。
    """

    def __init__(self, em=None, tdx=None):
        self.lock = threading.Lock()
        self.stats = {"sina": 0, "proxy": 0, "tq": 0, "empty": 0}

    def _note(self, src):
        with self.lock:
            self.stats[src if src else "empty"] = self.stats.get(src if src else "empty", 0) + 1

    def reset_stats(self):
        with self.lock:
            for k in self.stats:
                self.stats[k] = 0

    def collect(self, sym, ex, sina_code, yy, mm, period, lmt):
        period = int(period)
        bars, src = [], ""
        # 第120轮：云服务器优先（SINA_SERVER_ENABLED=True 时分钟K从云服务器拉取，本机 IP 不碰新浪 stock2，
        # 永不被封；服务器失败自动回落本机链路）。返回格式与 fetch_sina_minute 一致（新浪原始结构）。
        if getattr(config, "SINA_SERVER_ENABLED", False):
            try:
                from server_minute_client import _fetch_via_server
                raw = _fetch_via_server(sina_code, period, lmt)
                if raw:
                    bars = _sina_raw_to_bars(raw, sina_code, ex, period)
                    src = "server"
                    REGISTRY.record("minute_server", True)
                else:
                    REGISTRY.record("minute_server", False)
            except Exception:
                REGISTRY.record("minute_server", False)
        # 新浪主连（全周期；SINA_MINUTE_DISABLED=True 时跳过——stock2 被 WAF 456 封锁，等待新 IP 后改 False 恢复）
        if not bars and not getattr(config, "SINA_MINUTE_DISABLED", True):
            bars = fetch_sina_minute(sina_code, ex, period, lmt)
            if bars:
                src = "sina"
                REGISTRY.record("minute_sina", True)
            else:
                REGISTRY.record("minute_sina", False)   # G11 主源健康上报
        # 第118轮：新浪禁用/失败时——先代理池（秒级、代理IP独立出口绕过封锁），再天勤 TqSdk（独立通道）
        if not bars:
            try:
                from futures_data import _fetch_intraday_via_proxy
                raw = _fetch_intraday_via_proxy(sina_code, period, lmt)
                if raw:
                    bars = _sina_raw_to_bars(raw, sina_code, ex, period)
                    src = "proxy"
                    REGISTRY.record("minute_proxy", True)
            except Exception:
                REGISTRY.record("minute_proxy", False)
        if not bars:
            try:
                from backup_sources import tqsdk_minute_kline
                tq_bars = tqsdk_minute_kline(sina_code, period, num_bars=lmt or 20)
                if tq_bars:
                    bars = tq_bars
                    src = "tq"
                    REGISTRY.record("minute_tq", True)
            except Exception:
                REGISTRY.record("minute_tq", False)
        self._note(src)
        return bars, src


# ---------------- 通用分钟周期聚合（纯函数，零网络；供第15轮日内回测把细周期聚合成粗周期） ----------------

def _parse_dt(text):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(text), fmt)
        except ValueError:
            continue
    return None


def aggregate_bars(bars, base_min, factor):
    """把 base_min 分钟的升序 bar 每连续 factor 根聚合成一根更粗周期（如 1m×5->5m、30m×2->60m）。

    规则（泛化自 futures_data.aggregate_30m_to_60m）：
      - 仅当相邻两根细 bar 的时间差恰好等于 base_min 才视为同一连续交易段；跨午休/夜盘休市段不硬拼；
      - 合成 bar：开=段首根开、收=段末根收、高=段内最高、低=段内最低、量/额=段内求和，时间戳取段末根；
      - 段尾不足 factor 根的零散 bar 不合成（不编造半根周期）。
    返回新列表，元素字段与输入一致（dt/o/h/l/c/v/amount 及透传的 sym/contract/period 等）。
    """
    base_min, factor = int(base_min), int(factor)
    if factor <= 1 or base_min <= 0:
        return [dict(b) for b in bars]
    out, seg, prev_dt = [], [], None
    for b in bars:
        dt = _parse_dt(b.get("dt"))
        if dt is None:
            continue
        contiguous = prev_dt is not None and abs((dt - prev_dt).total_seconds() - base_min * 60) < 1
        if not contiguous:
            seg = []                       # 跨休市段：另起
        seg.append((dt, b))
        if len(seg) == factor:
            dts, items = zip(*seg)
            merged = dict(items[-1])
            merged["dt"] = items[-1].get("dt")
            merged["o"] = float(items[0]["o"]); merged["c"] = float(items[-1]["c"])
            merged["h"] = max(float(x["h"]) for x in items)
            merged["l"] = min(float(x["l"]) for x in items)
            merged["v"] = sum(float(x.get("v") or 0) for x in items)
            merged["amount"] = sum(float(x.get("amount") or 0) for x in items)
            if "period" in merged:
                merged["period"] = base_min * factor
            out.append(merged)
            seg = []
        prev_dt = dt
    return out
