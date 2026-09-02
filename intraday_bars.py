# -*- coding: utf-8 -*-
"""分钟K线数据层（新浪主连全周期为主 + 东财具体合约兜底 + 通达信可选冗余 + 通用周期聚合）。

为什么需要：
  - 日内/平今回测（第15轮 WP-D1/D2）必须有带时间戳的分钟 bar；免费源历史分钟窗口有限，
    长期、自有、永不丢的分钟库根本上靠程序 7×24 常驻、每几分钟自采一次滚动积累。

选源实测（2026-09-01 晚两次实测定型；**当晚补测纠正第14轮"新浪无1分钟"的误判**）：
  * 新浪主连 getFewMinLine（**主源，全周期 1/5/15/30/60m**）：主连代码直接给（RB0，无需
    合约转换/换月跟随），每个周期固定 1023 根——实测 1m≈2.5个交易日、5m≈3周、15m≈3月、
    30m≈6月、60m≈12.5月，64/64 品种全覆盖、零断连、单请求0.1s级；具体合约（RB2701/MA610）
    同样可取。字段 d/o/h/l/c/v/p（p=持仓量，无成交额）。
  * 东财 push2his（具体合约兜底）：有全周期，但无主力连续、secid=市场号.具体合约；
    本机两晚实测该行情域名按 IP 临时限流/直接断连（RemoteDisconnected），故仅在新浪与
    通达信都失败时兜底；保留低并发+全局限流+镜像轮换+熔断，任何失败软降级 []。
    注意：东财 datacenter 基本面域名、push2 实时快照域名与此不同、实测稳定，互不影响。
  * 通达信 pytdx（可选冗余，tdx_bars.py）：公共 7709 只同步股票、期货所在 7727 不可达，
    probe() 自动探测，确认能取期货才启用，不可用零成本跳过；未装 pytdx 也不影响运行。

能力天花板（诚实声明）：免费源无历史 L2 逐笔；新浪主连是比例复权连续序列（换月点为近似），
具体合约真实价格由东财/通达信在可用时补充；分钟长期历史靠常驻自采滚动积累。
"""
import threading
import time
from datetime import datetime, timedelta

import config
from http_client import http
from utils import LOG

# 新浪主连分钟K支持的周期（分钟）；2026-09-01 晚补测 type=1（一分钟）同样返回1023根
SINA_MIN_PERIODS = (1, 5, 15, 30, 60)


# ---------------- 合约代码 / secid 转换（东财具体合约） ----------------

def em_contract_code(sym, ex, yy, mm):
    """项目 (yy,mm) -> 东财分钟K用的具体合约代码（小写）。
    CZCE（郑商所）东财用3位、年份取个位：MA2610 -> ma610、TA2701 -> ta701；其余交易所4位：rb2701/m2701/si2611。"""
    sym = str(sym).lower()
    if ex == "CZCE":
        return f"{sym}{yy % 10}{mm:02d}"
    return f"{sym}{yy:02d}{mm:02d}"


def project_contract_code(sym, yy, mm):
    """项目内部统一的具体合约代码（新浪式大写4位年月，如 RB2701/MA2610），用于入库与跨表对齐。"""
    return f"{str(sym).upper()}{yy:02d}{mm:02d}"


def em_secid(sym, ex, yy, mm):
    """组装东财 secid（市场号.合约代码）；未知交易所返回空串（调用方据此跳过）。"""
    mkt = config.MINUTE_MARKET.get(ex)
    if not mkt:
        return ""
    return f"{mkt}.{em_contract_code(sym, ex, yy, mm)}"


def _parse_line(line, sym, ex, yy, mm, period):
    """解析东财一根 klines 文本：'2026-09-01 09:30,开,收,高,低,量,额'（开-收-高-低顺序）。"""
    parts = str(line).split(",")
    if len(parts) < 7:
        return None
    try:
        o, c, h, l = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
        v, amount = float(parts[5]), float(parts[6])
    except (TypeError, ValueError):
        return None
    dt_text = parts[0]
    if c <= 0 or h <= 0 or l <= 0:
        return None
    return {"dt": dt_text, "trade_date": dt_text[:10],
            "o": o, "h": h, "l": l, "c": c, "v": v, "amount": amount,
            "sym": str(sym).upper(), "contract": project_contract_code(sym, yy, mm),
            "exchange": ex, "period": int(period), "src": "em"}


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


# ---------------- 东财采集器（线程安全：全局限流 + 镜像轮换 + 退避重试 + 熔断） ----------------

class MinuteBarFetcher:
    def __init__(self):
        self.lock = threading.Lock()
        self._last_req = 0.0
        self._host_turn = 0
        self.fail_streak = 0          # 连续连接级失败次数（整站限流/断连累计，任一成功即清零）
        self.cooldown_until = 0.0     # 熔断到期时间戳；冷却期内 fetch 直接返回[]，避免整站不可达时逐任务空耗重试

    def _throttle(self):
        """全局限流：保证任意两线程相邻请求间隔不小于 MINUTE_REQ_GAP，规避东财快速断连。"""
        with self.lock:
            wait = config.MINUTE_REQ_GAP - (time.time() - self._last_req)
            if wait > 0:
                time.sleep(wait)
            self._last_req = time.time()

    def _ordered_hosts(self):
        """镜像子域按轮次错位起始，把负载分散到 push2his / 1~3.push2his。"""
        hosts = config.MINUTE_EM_HOSTS
        with self.lock:
            start = self._host_turn % len(hosts)
            self._host_turn += 1
        return hosts[start:] + hosts[:start]

    def _note_success(self):
        with self.lock:
            self.fail_streak = 0
            self.cooldown_until = 0.0

    def _note_failure(self):
        with self.lock:
            self.fail_streak += 1
            if self.fail_streak >= config.MINUTE_CIRCUIT_FAILS:
                self.cooldown_until = time.time() + config.MINUTE_CIRCUIT_COOLDOWN
                self.fail_streak = 0
                LOG.warning("东财分钟K连续失败达%d次，熔断%d秒（疑似整站限流/断连，期间跳过自采，到期自动重试）",
                            config.MINUTE_CIRCUIT_FAILS, config.MINUTE_CIRCUIT_COOLDOWN)

    @property
    def in_cooldown(self):
        return time.time() < self.cooldown_until

    def fetch(self, sym, ex, yy, mm, period, lmt):
        """拉取某具体合约某周期最近 lmt 根分钟K，升序返回 bar dict 列表；任何失败软降级为 []。"""
        secid = em_secid(sym, ex, yy, mm)
        if not secid or self.in_cooldown:
            return []
        period, lmt = int(period), int(lmt)
        url_tpl = ("http://{host}/api/qt/stock/kline/get?secid=" + secid
                   + f"&klt={period}&fqt=0&lmt={lmt}&end=20500101"
                     "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57")
        backoff = config.MINUTE_RETRY_WAIT
        last_note = ""
        for _round in range(config.MINUTE_RETRY):
            if self.in_cooldown:
                return []
            for host in self._ordered_hosts():
                if self.in_cooldown:
                    return []
                self._throttle()
                try:
                    resp = http.get(
                        url_tpl.format(host=host),
                        headers={"Referer": "https://quote.eastmoney.com/",
                                 "Accept": "*/*", "Connection": "close"},
                        timeout=12)
                    data = (resp.json() or {}).get("data") or {}
                    raw = data.get("klines") or []
                    bars = [b for line in raw
                            if (b := _parse_line(line, sym, ex, yy, mm, period))]
                    if bars:
                        bars.sort(key=lambda b: b["dt"])
                        self._note_success()
                        return bars
                    last_note = "返回空"
                except Exception as e:    # 含 RemoteDisconnected：换镜像/退避后重试，并累计熔断计数
                    last_note = f"{type(e).__name__}:{str(e)[:40]}"
                    self._note_failure()
                    # 连接级异常(RemoteDisconnected/Timeout，均为 OSError 子类)是 IP 级整站封锁，
                    # 几秒内不会恢复、镜像同域一起被封，用短退避尽快凑满熔断次数（约2~3s熔断），
                    # 不拖慢 --once 启动；只有"返回空"等疑似抖动才走指数退避。
                    conn_err = isinstance(e, OSError)
                    time.sleep(0.3 if conn_err else backoff)
                    if not conn_err:
                        backoff = min(backoff * 2, 8.0)
            time.sleep(backoff)
            backoff = min(backoff * 2, 8.0)
        LOG.debug("分钟K获取失败 %s %s 周期%d: %s", sym, secid, period, last_note)
        return []


# ---------------- 多源统一采集器：新浪主连全周期(含1m)优先，通达信/东财具体合约兜底 ----------------

class MinuteCollector:
    """对单个品种单个周期选源采集，对上层屏蔽三源差异。

    选源顺序（2026-09-01 晚补测后定型，全周期统一链路）：
      新浪主连（1/5/15/30/60m，稳、深、免换月，type=1一分钟K同样1023根）
        → 通达信具体合约（若 probe 可用）
        → 东财具体合约兜底（push2his 限流期自动跳过）。
    任一源成功即返回 (bars, 源名)；全部失败返回 ([], "")，调用方只计数不阻断。
    主力具体合约未知(yy/mm=None)时只能用新浪主连——而新浪全周期可用，故1m不再依赖合约探测。
    """

    def __init__(self, em=None, tdx=None):
        self.em = em or MinuteBarFetcher()
        self.tdx = tdx
        self.lock = threading.Lock()
        self.stats = {"sina": 0, "em": 0, "tdx": 0, "empty": 0}

    def _note(self, src):
        with self.lock:
            self.stats[src if src else "empty"] = self.stats.get(src if src else "empty", 0) + 1

    def reset_stats(self):
        with self.lock:
            for k in self.stats:
                self.stats[k] = 0

    def collect(self, sym, ex, sina_code, yy, mm, period, lmt):
        period = int(period)
        has_contract = yy is not None and mm is not None   # 具体合约源（tdx/em）需要主力yy/mm
        bars, src = [], ""
        # 第一选择：新浪主连（全周期，无需合约转换/换月跟随）
        bars = fetch_sina_minute(sina_code, ex, period, lmt)
        if bars:
            src = "sina"
        # 第二选择：通达信具体合约（仅当 probe 点亮）
        elif has_contract and self.tdx is not None and getattr(self.tdx, "available", False):
            bars = self.tdx.fetch(sym, ex, yy, mm, period, lmt)
            if bars:
                src = "tdx"
        # 第三选择：东财具体合约兜底
        if not bars and has_contract:
            bars = self.em.fetch(sym, ex, yy, mm, period, lmt)
            if bars:
                src = "em"
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
