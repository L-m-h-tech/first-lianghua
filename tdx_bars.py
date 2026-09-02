# -*- coding: utf-8 -*-
"""第14轮 WP-D0 附：通达信(pytdx)期货分钟K【可选】适配源（延迟导入、自动探测、不可用零开销）。

为什么是"可选"而不是主源——2026-09-01 本机穷尽实测（重庆/当前运营商，证据见《未完成项落地方案.md》）：
  * pytdx 走通达信公共行情：标准行情 7709 端口，官方 103 台主机中 28 台 TCP 可达，
    但逐台验证全部只同步沪深股票（市场0/1），期货市场 28郑商/29大商/30上期/47中金 在
    0~120 全市场号枚举中合约列表与分钟K全部为空；
  * 商品期货真正所在的扩展行情端口 7727（TdxExHq_API），103 台 0 可达，7720/7721/7729/7730 亦全关；
  * 社区同样记载"7727 扩展行情 10 台仅 1 台可连且数据接口全返回 None"（sickate/pytdx 2026-08）；
  * 结论：免费公共服务器当前拿不到商品期货。仅当①本机常驻登录通达信金融终端(本地代理)、
    ②网络环境变化后公共服务器恢复期货、③接入券商/付费TDX网关 之一时，本模块才会被 probe() 自动点亮。
因此：主链路不依赖它，pytdx 不进 requirements 强制依赖（函数内延迟导入，未安装则永久降级为不可用）。

能力边界（协议层事实，不随网络变化）：通达信行情协议只有 K线/五档快照/分笔/证券列表，
  没有期货库存仓单、龙虎榜会员持仓、新闻快讯、交易日历——这些东财 datacenter 接口无法被通达信替代。
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from utils import LOG

PROBE_WORKERS = 20          # 启动探测并发数（公共服务器多，串行太慢）
PROBE_DEADLINE = 15.0       # 探测总时限（秒），超时未找到带期货的服务器即判定不可用，不拖慢启动

# 标准行情期货市场号（通达信协议固定编号；非 pytdx 常量，社区多份实现一致）
TDX_FUT_MARKET = {"CZCE": 28, "DCE": 29, "SHFE": 30, "INE": 30, "CFFEX": 47}
# 分钟周期(分钟) -> pytdx category（标准行情口径；1m=8, 5m=0, 15m=1, 30m=2, 60m=3）
TDX_CATEGORY = {1: 8, 5: 0, 15: 1, 30: 2, 60: 3}

# 优先服务器：2026-09-01 本机实测 7709 TCP 可达的 28 台（排序大体按延迟）；
# probe 时还会动态合并 pytdx 官方 hosts 全表，公共服务器可用性随时间/地区漂移，多备一些。
_PREFERRED = [
    "60.191.117.167", "60.12.136.250", "115.238.90.165", "218.75.126.9",
    "115.238.56.198", "180.153.18.170", "117.34.114.13", "117.34.114.15",
    "117.34.114.16", "117.34.114.20", "117.34.114.27", "119.29.19.242",
    "123.125.108.14", "123.125.108.90", "175.6.5.153", "182.118.47.151",
    "182.131.3.245", "183.60.224.177", "183.60.224.178", "202.100.166.27",
    "218.106.92.182", "218.106.92.183", "218.6.170.47", "220.178.55.71",
    "220.178.55.86", "58.63.254.191", "58.63.254.217", "59.36.5.11",
]


def tdx_contract(sym, ex, yy, mm):
    """项目 (yy,mm) -> 通达信合约代码。
    郑商所(CZCE)三位、年份取个位、大写：MA2610 -> MA610、TA2701 -> TA701；
    其余所四位小写：rb2701/m2701/sc2610/si2611。"""
    sym = str(sym)
    if ex == "CZCE":
        return f"{sym.upper()}{yy % 10}{mm:02d}"
    return f"{sym.lower()}{yy:02d}{mm:02d}"


class TdxMinuteSource:
    """通达信分钟K单连接源（线程安全：pytdx 单连接不支持并发，全部调用串行化）。"""

    def __init__(self, servers=None, port=7709, timeout=5, probe_codes=None):
        self.port = port
        self.timeout = timeout
        self.lock = threading.RLock()
        self.api = None
        self.server = ""
        self.available = None         # None=尚未探测，True/False=探测结论
        self._servers = list(dict.fromkeys(servers or _PREFERRED))
        # 探测用样本：(市场号, 合约代码)，任一能取到分钟K即判定该服务器带期货数据
        self._probe_codes = probe_codes or [(30, "rb2610"), (29, "m2609"), (28, "MA609")]

    def _all_servers(self):
        ips = list(self._servers)
        try:  # 动态并入 pytdx 官方主机表（装了 pytdx 才有）
            from pytdx.config.hosts import hq_hosts
            for _name, ip in hq_hosts:
                if ip not in ips:
                    ips.append(ip)
        except Exception:
            pass
        return ips

    @staticmethod
    def _new_api():
        try:
            from pytdx.hq import TdxHq_API
        except Exception:
            return None
        return TdxHq_API(raise_exception=False, auto_retry=False)

    def _try_server(self, ip):
        api = self._new_api()
        if api is None:
            return None
        try:
            if not api.connect(ip, self.port, time_out=self.timeout):
                return None
            for mkt, code in self._probe_codes:
                try:
                    bars = api.get_security_bars(2, mkt, code, 0, 2)  # 30m
                except Exception:
                    bars = None
                if bars:  # 该服务器确实同步了期货：保留连接
                    return api
            api.disconnect()
        except Exception:
            try:
                api.disconnect()
            except Exception:
                pass
        return None

    def probe(self):
        """启动探测一次（并发+总时限，不拖慢启动）：找到带期货数据的服务器即点亮；
        全不可用则 available=False，之后 fetch 零成本返回 []。"""
        with self.lock:
            if self.available is not None:
                return self.available
            if self._new_api() is None:
                self.available = False
                LOG.info("通达信分钟源：未安装 pytdx（可选依赖），该源不启用（不影响其他数据源）")
                return False
            servers = self._all_servers()
            t0 = time.time()
            winner = None
            pool = ThreadPoolExecutor(max_workers=PROBE_WORKERS)
            futures = {pool.submit(self._try_server, ip): ip for ip in servers}
            try:
                for fut in as_completed(list(futures), timeout=PROBE_DEADLINE):
                    api = fut.result()
                    if api is not None:
                        winner = (futures[fut], api)
                        break
            except Exception:
                pass
            for fut, ip in futures.items():          # 回收：关闭其余已建立的连接、取消未完成任务
                if winner and fut.done():
                    try:
                        other = fut.result()
                        if other is not None and other is not winner[1]:
                            other.disconnect()
                    except Exception:
                        pass
                else:
                    fut.cancel()
            pool.shutdown(wait=False)
            if winner:
                ip, api = winner
                self.api, self.server = api, ip
                self.available = True
                LOG.info("通达信分钟源：已连接 %s:%s 且验证可取期货分钟K（%.1fs探测），作为可选冗余源启用",
                         ip, self.port, time.time() - t0)
                return True
            self.available = False
            LOG.info("通达信分钟源：%d台公共服务器在%.0fs内均无商品期货数据（7709仅股票/7727扩展口不可达），"
                     "该源不启用，分钟K走新浪主连全周期(含1m)+东财兜底", len(servers), time.time() - t0)
            return False

    def _reconnect(self):
        """当前连接失效：换服务器重连，全部失败返回False。"""
        for ip in self._all_servers():
            if ip == self.server:
                continue
            api = self._try_server(ip)
            if api is not None:
                self.api, self.server = api, ip
                return True
        # 原服务器也再试一次
        api = self._try_server(self.server) if self.server else None
        if api is not None:
            self.api = api
            return True
        self.available = False
        return False

    def fetch(self, sym, ex, yy, mm, period, count):
        """取某具体合约最近 count 根分钟K，升序返回统一 bar dict；不可用/不支持/失败均返回 []。"""
        with self.lock:
            if self.available is False:
                return []
            if self.available is None and not self.probe():
                return []
            mkt = TDX_FUT_MARKET.get(ex)
            cat = TDX_CATEGORY.get(int(period))
            if mkt is None or cat is None:
                return []  # GFEX 广期所公共TDX不提供；非分钟周期不支持
            code = tdx_contract(sym, ex, yy, mm)
            for attempt in range(2):
                try:
                    raw = self.api.get_security_bars(cat, mkt, code, 0, int(count))
                    if raw is None and attempt == 0 and self._reconnect():
                        continue
                    bars = []
                    for r in raw or []:
                        dt = str(r.get("datetime") or "")[:16]
                        try:
                            o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        if c <= 0:
                            continue
                        bars.append({"dt": dt, "trade_date": dt[:10], "o": o, "h": h, "l": l, "c": c,
                                     "v": float(r.get("vol") or 0), "amount": float(r.get("amount") or 0),
                                     "sym": str(sym).upper(),
                                     "contract": f"{str(sym).upper()}{yy:02d}{mm:02d}",
                                     "exchange": ex, "period": int(period), "src": "tdx"})
                    bars.sort(key=lambda b: b["dt"])
                    return bars
                except Exception:
                    if attempt == 0 and self._reconnect():
                        continue
                    return []
            return []

    def close(self):
        with self.lock:
            try:
                if self.api:
                    self.api.disconnect()
            except Exception:
                pass
            self.api = None
