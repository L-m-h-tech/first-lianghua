# -*- coding: utf-8 -*-
"""【需求①/⑤】国内期货行情与日线指标：
- fetch_quotes 批量拉取主力连续与任意月份合约行情（分批40个/请求），供需求⑤主力月份探测使用
- fetch_daily_kline/compute_indicators 计算HV20/HV60、MA、ATR14、5/20日动量（技术因子+期权HV基准）
【需求③】HV20/HV60 是期权隐波估计与"波动率不贵"检查的基准

接口实测字段（nf_RB0 示例，商品期货）:
  螺纹钢连续,230000,3160,3180,3159,0,3177,3178,3178,0,3151,6,8,1152964,221502,沪,螺纹钢,2026-08-28,1,...
  [0]名称 [2]开盘 [3]最高 [4]最低 [6]买价 [7]卖价 [8]最新价 [10]昨结算 [13]持仓 [14]成交量 [15]交易所 [16]品种名 [17]日期
中金所(IF/IH等)字段不同: [0]开盘 [1]最高 [2]最低 [3]最新价
"""
import json
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import config
from http_client import http
from data_router import REGISTRY
from utils import LOG, clip


def _f(s):
    try:
        v = float(s)
        return v if v == v else 0.0
    except (TypeError, ValueError):
        return 0.0


def _in_trading():
    """当前是否在任意品种的交易时段（日盘+夜盘）。
    收盘后日线数据不再变化，且新浪 stock2 对盘后请求返回 WAF 拦截页（200 但无K线数组），
    造成大量无效请求；盘后直接复用已有缓存，跳过新浪/CDP 日线拉取。"""
    try:
        from utils import is_trading_time
        return bool(is_trading_time()[0])
    except Exception:
        return True  # 判定失败保守当交易中，不阻断正常链路


# ---------------- 第116轮：新浪 WAF 封锁状态机（防请求放大器） ----------------
# 核心问题：456 封锁期间 KlineCache 失败缓存 TTL=5分钟 < 分析周期10分钟，
# 导致每 5-10 分钟全 64 品种重新打新浪 stock2——封了继续打，越打越封（恶性循环）。
# 修复：封锁期完全短路日线请求 + 失败缓存动态拉长到 60 分钟 + 整站封锁自动检测。

_sina_lock = threading.Lock()
_sina_block_until = 0.0       # 456 封锁到期（time.monotonic）
_sina_block_streak = 0        # 连续 456 触发次数（用于递增封锁时长）
_WAF_BLOCK_BASE = 60 * 60     # 456 封锁基础时长 1 小时（秒）
_WAF_BLOCK_MAX = 6 * 60 * 60  # 连续触发时封锁时长上限 6 小时

# 失败率统计（整站封锁检测）：连续失败率 ≥80% 判定整站封锁
_KLINE_WINDOW = 600.0          # 统计窗口 10 分钟
_KLINE_STAT = {"start": None, "ok": 0, "fail": 0}
_FAIL_RATIO_TRIGGER = 0.80     # 失败率阈值
_FAIL_RATIO_MIN_TOTAL = 16     # 至少 16 次请求才判定（避免小样本误判）


def _sina_waf_blocked():
    """是否处于新浪 WAF 封锁冷却期（期间日线/akshare/CDP 全部短路）。"""
    with _sina_lock:
        return time.time() < _sina_block_until


def _sina_block_info():
    """返回当前封锁剩余秒数（供日志/看板使用），未封锁返回 0。"""
    with _sina_lock:
        remain = _sina_block_until - time.time()
        return max(0.0, remain)


def _sina_note_block():
    """检测到 456：进入封锁期，时长随连续封锁次数指数递增（1h→2h→...封顶6h），
    并立即重置成功计数。同时记录到 REGISTRY（供数据健康看板展示）。"""
    global _sina_block_until, _sina_block_streak
    with _sina_lock:
        _sina_block_streak += 1
        dur = min(_WAF_BLOCK_BASE * (2 ** (_sina_block_streak - 1)), _WAF_BLOCK_MAX)
        _sina_block_until = time.time() + dur
        LOG.warning("新浪WAF 456封锁：连续第%d次，封锁 %d 分钟（期间日线/akshare/CDP零请求）",
                    _sina_block_streak, dur // 60)
        try:
            from data_router import REGISTRY
            REGISTRY.record("kline_sina_waf", False)
        except Exception:
            pass


def _kline_note_success():
    """日线请求成功：重置连续封锁计数 + 更新失败率统计。"""
    global _sina_block_streak
    with _sina_lock:
        _sina_block_streak = 0  # 成功=封锁解除
        st = _KLINE_STAT
        if st["start"] is None or time.time() - st["start"] > _KLINE_WINDOW:
            st["start"] = time.time()
            st["ok"], st["fail"] = 0, 0
        st["ok"] += 1


def _kline_note_fail():
    """日线请求失败（网络异常/456/响应异常）：累计失败计数，判断是否触发整站封锁。
    无参数设计：统计窗口内的请求/失败计数，不关心具体品种。"""
    global _sina_block_until, _sina_block_streak
    with _sina_lock:
        st = _KLINE_STAT
        now = time.time()
        if st["start"] is None or now - st["start"] > _KLINE_WINDOW:
            st["start"] = now
            st["ok"], st["fail"] = 0, 1
            return
        st["fail"] += 1
        total = st["ok"] + st["fail"]
        if total >= _FAIL_RATIO_MIN_TOTAL and st["fail"] / total >= _FAIL_RATIO_TRIGGER:
            ok_val, fail_val = st["ok"], st["fail"]
            st["ok"], st["fail"] = 0, 0
            LOG.warning("新浪日线整站失败率%.0f%%（%d/%d次），判定WAF封锁 %d 分钟",
                        100 * fail_val / max(1, total), fail_val, total, _WAF_BLOCK_BASE // 60)
            _sina_block_until = now + _WAF_BLOCK_BASE
            _sina_block_streak += 1
            try:
                from data_router import REGISTRY
                REGISTRY.record("kline_sina_waf", False)
            except Exception:
                pass


# ---------------- 新浪白名单出口（2026-09-11） ----------------
# 优质云主机白名单 IP：配置 SINA_WHITELIST_PROXY 后，新浪 stock2 请求优先走它（豁免 456 频控）。
# 未配置/失败/超时自动回落本机直连（False=不拦截），与第116轮 WAF 状态机正交（封锁仍按本机判定）。

def _sina_whitelist_proxy():
    """当前生效的白名单代理 "host:port" 或 ""（未配置/未生效）。"""
    try:
        return getattr(config, "SINA_WHITELIST_PROXY", "") or ""
    except Exception:
        return ""


# ---- 全局新浪 stock2 限流（2026-09-11 第120轮实测结论） ----
# 320 任务瞬间并发（6线程）把白名单 IP 和本机 IP 同时打进 456 封锁——新浪对任意 IP 高频必封，
# 不存在"白名单豁免"。故对 stock2（日线 getDailyKLine + 分钟K getFewMinLine）做全局串行化限流：
# 任意时刻只有一次请求进入新浪，且间隔 >= SINA_REQ_GAP 秒（默认 3s ≈ 20次/min 安全线以内）。
# 白名单/本机共用同一节奏，最坏情况单 IP 频率减半，双保险不触发 456。
_sina_gate = threading.Lock()
_sina_last_req = 0.0

def _sina_throttle():
    """新浪 stock2 全局限流：恒速 >= SINA_REQ_GAP 秒/次。自动补齐等待，不阻塞主流程。"""
    global _sina_last_req
    gap = float(getattr(config, "SINA_REQ_GAP", 3.0) or 3.0)
    with _sina_gate:
        wait = _sina_last_req + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        _sina_last_req = time.time()


def _sina_whitelist_get(url, timeout=None):
    """经白名单代理发送新浪请求，成功返回解析后的 K线数组（list），失败返回 None。

    与既有代理池同款机制（urllib ProxyHandler）；仅当返回正文含 K线数组且非 456 才算成功。
    未配置白名单代理 / 白名单被 456 / 超时 / 无数组 一律返回 None，由调用方回落本机直连。
    """
    proxy = _sina_whitelist_proxy()
    if not proxy:
        return None
    try:
        import urllib.request as _ur
        handler = _ur.ProxyHandler({
            "http": "http://%s" % proxy,
            "https": "http://%s" % proxy})
        opener = _ur.build_opener(handler)
        req = _ur.Request(url, headers=config.HEADERS_SINA)
        body = opener.open(req, timeout=timeout or getattr(config, "SINA_WHITELIST_TIMEOUT", 8)).read()
        text = body.decode("utf-8", "replace")
        if "456" in text[:64]:
            LOG.warning("白名单出口也被新浪WAF拦截(456)，回落本机直连")
            _sina_note_block()
            return None
        m = re.search(r"\((\[.*\])\)", text, re.S)
        if m and len(m.group(1)) > 100:
            return json.loads(m.group(1))
        return None
    except Exception as e:
        LOG.debug("白名单出口请求失败(%s): %s", proxy, e)
        return None


def fetch_quotes(codes):
    """批量拉取品种最新行情（自动分批，每批40个），返回 {code: {...}}，失败品种不返回。

    主源=新浪 hq.sinajs 主连快照；新浪整批失败或个别品种缺失时，用天勤 TqSdk 行情兜底
    （第119轮：删除东财 push2 兜底——本机 IP 被东财 TLS 指纹封锁，批量接口 RemoteDisconnected
    持续断连，保留无意义）。"""
    codes = [c for c in codes if c]
    quotes = {}
    for i in range(0, len(codes), 40):
        chunk = codes[i:i + 40]
        url = "https://hq.sinajs.cn/list=" + ",".join("nf_" + c for c in chunk)
        try:
            r = http.get(url, headers=config.HEADERS_SINA, timeout=config.TIMEOUT)
            r.encoding = "gbk"
        except Exception as e:
            LOG.warning("期货行情请求失败: %s", e)
            REGISTRY.record("quote_sina", False)   # G11 主源健康上报
            continue
        REGISTRY.record("quote_sina", True)
        for code in chunk:
            _parse_quote(code, r.text, quotes)
    missing = [c for c in codes if c not in quotes]
    if missing:
        # 天勤 TqSdk 兜底（可选依赖+可选账户；未配置时零请求返回 {}）。
        # 后台线程订阅缓存，此处纯读取零阻塞；只补新浪没拿到的品种。
        # 第119轮：删除原东财 push2 第二兜底（TLS 指纹封锁持续断连）。
        try:
            from backup_sources import tqsdk_quote, tqsdk_start
            tqsdk_start()   # 幂等：首次调用启动后台订阅，之后直接读缓存
            tq_quotes = tqsdk_quote(missing)
            if tq_quotes:
                quotes.update(tq_quotes)
                LOG.info("新浪缺失%d个品种，天勤TqSdk补回%d个",
                         len(missing), len(tq_quotes))
            REGISTRY.record("quote_tq", bool(tq_quotes))
        except Exception as e:
            LOG.debug("天勤行情兜底失败（不影响主流程）: %s", e)
            REGISTRY.record("quote_tq", False)
    # A1（第94轮）：解析健康探针——行情覆盖数（64品种全齐=64）
    try:
        import parser_health
        parser_health.record("sina_em_quotes", bool(quotes), len(quotes))
    except Exception:
        pass
    return quotes


def _parse_quote(code, text, quotes):
    """解析新浪行情行（字段布局以 docstring 为准；A2 改版变体经评估为臆测布局会产出脏数据，
    按"宁缺毋滥"纪律不做字段移位猜测——保留 A1 探针上报零命中供人工/LLM 介入）。"""
    try:
        import parser_health
    except Exception:
        parser_health = None
    if not _parse_quote_inner(code, text, quotes):
        if parser_health is not None:
            try:
                parser_health.record("sina_quote", False, 0)
            except Exception:
                pass
        return
    if parser_health is not None:
        try:
            parser_health.record("sina_quote", True, 1)
        except Exception:
            pass


def _parse_quote_inner(code, text, quotes):
    m = re.search(r'hq_str_nf_%s="([^"]*)"' % code, text)
    if not m:
        return False
    f = m.group(1).split(",")
    try:
        float(f[0])
        is_cffex = True          # 中金所行情第一字段就是数字
    except (ValueError, IndexError):
        is_cffex = False

    q = {}
    if not is_cffex and len(f) >= 18:
        latest = _f(f[8])
        prev = _f(f[10])
        q = {"name": f[16], "latest": latest, "open": _f(f[2]),
             "high": _f(f[3]), "low": _f(f[4]),
             "prev_settle": prev,
             "chg_pct": (latest / prev - 1.0) if (latest > 0 and prev > 0) else 0.0,
             "open_interest": _f(f[13]), "volume": _f(f[14]),
             "date": f[17] if len(f) > 17 else "",
             # G14（第92轮）：一档盘口快照字段（[6]买一价 [7]卖一价 [11]买一量 [12]卖一量
             # [17]行情日期 [1]行情时间HHMMSS）。仅新浪主源有；东财兜底 dict 无这些键，消费端按 0 处理。
             "bid": _f(f[6]), "ask": _f(f[7]),
             "bid_vol": _f(f[11]) if len(f) > 11 else 0.0,
             "ask_vol": _f(f[12]) if len(f) > 12 else 0.0,
             "quote_date": f[17] if len(f) > 17 else "",
             "quote_time": f[1] if len(f) > 1 else ""}
    elif is_cffex and len(f) >= 4:
        latest = _f(f[3])
        q = {"name": f[-1] if f[-1] else code, "latest": latest,
             "open": _f(f[0]), "high": _f(f[1]), "low": _f(f[2]),
             "prev_settle": 0.0, "chg_pct": 0.0,
             "open_interest": 0.0, "volume": _f(f[4]), "date": "",
             "bid": 0.0, "ask": 0.0, "bid_vol": 0.0, "ask_vol": 0.0,
             "quote_date": "", "quote_time": ""}
    if q.get("latest", 0) > 0:
        quotes[code] = q
        return True
    return False


def fetch_daily_kline(symbol, retry=2):
    """期货日线K线，返回 [{d,o,h,l,c,v,p,s}, ...]（可能失败，调用方需兜底）。

    2026-09-10 第115/116轮：新浪 stock2 对无 Referer 及高频请求返回 HTTP 456（IP 级 WAF 封锁）
    且东财 push2his 对 Python http 按 TLS 指纹封锁——本机 IP 在封锁期反复触发。
    第117轮（用户决策）：**新浪主源显式禁用（代码保留、开关关闭）**——`config.SINA_DAILY_DISABLED`
    默认 True，等新 IP 后改 False 恢复；**删除全部旁路源**：akshare（新浪系）、免费代理池、东财 CDP
    （这些不再向被封锁域名发任何日K请求）。**日K实际可用源 = 天勤 TqSdk 单一通道**（独立于新浪/东财
    域名，不受 WAF/TLS 封锁影响）。
    云服务器优先（2026-09-11）：SINA_SERVER_ENABLED=True 时日线先走云服务器出口
    （与分钟K同一批 10 台，本机 IP 不碰新浪 stock2），失败回落天勤/代理池。
    """
    # 云服务器优先（与 MinuteCollector 同源；返回新浪原始结构，直接返回）
    if getattr(config, "SINA_SERVER_ENABLED", False):
        try:
            from server_minute_client import _fetch_daily_via_server
            srv_bars = _fetch_daily_via_server(symbol)
            if srv_bars:
                _kline_note_success()
                return srv_bars
        except Exception:
            pass
    # 新浪主源（显式禁用：SINA_DAILY_DISABLED=True 时不发任何 stock2 请求，代码保留待新 IP 后恢复）
    if not getattr(config, "SINA_DAILY_DISABLED", True):
        url = (f"https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
               f"InnerFuturesNewService.getDailyKLine?symbol={symbol}")
        # 白名单出口优先（云主机白名单 IP 豁免 456 频控）：配置生效时走它，失败/未配置回落本机直连。
        _sina_throttle()
        wl_bars = _sina_whitelist_get(url)
        if wl_bars:
            _kline_note_success()
            return wl_bars
        # 白名单未配置/失败 → 本机直连（原有逻辑；WAF 封锁期短路）
        _sina_blocked = _sina_waf_blocked()
        last_err = "新浪WAF封锁(456)短路" if _sina_blocked else None
        if not _sina_blocked:
            last_err = None
            for _ in range(retry + 1):
                _sina_throttle()
                try:
                    r = http.get(url, headers=config.HEADERS_SINA,
                                     timeout=config.TIMEOUT)
                    r.encoding = "utf-8"
                    if r.status_code == 456:
                        last_err = "IP被新浪WAF封锁(456)"
                        _sina_note_block()
                        break
                    m = re.search(r"\((\[.*\])\)", r.text, re.S)
                    if m:
                        _kline_note_success()
                        return json.loads(m.group(1))
                    last_err = "响应中未找到K线数组"
                except Exception as e:
                    last_err = str(e)
                time.sleep(0.5)
            _kline_note_fail()
    # 天勤 TqSdk 日线（主要活跃源，独立通道不受新浪/东财封锁影响；连接幂等、失败不影响主流程）
    try:
        from backup_sources import tqsdk_daily_kline, tqsdk_start
        tqsdk_start()   # 幂等：确保连接就绪
        tq_bars = tqsdk_daily_kline(symbol)
        if tq_bars:
            _kline_note_success()
            return tq_bars
    except Exception as e:
        LOG.debug("天勤日线获取失败（不影响主流程）: %s", e)
    # 第115轮：代理池兜底（天勤不覆盖的品种如郑商所 PTA 等，走代理池绕新浪封锁获取日K）
    proxy_bars = _fetch_daily_via_proxy(symbol)
    if proxy_bars:
        _kline_note_success()
        return proxy_bars
    raise RuntimeError("日线获取失败(%s): 天勤TqSdk+代理池均不可用（新浪日线已禁用）" % symbol)


# ==========================================================================
# 第115轮：新浪 WAF 456 封锁免费代理池
# 本机 IP 被新浪 WAF 封锁(456)时，代理 IP 是独立出口可绕过。实测 20 代理池轮换
# 重试（max_attempts=6，0.35s 间隔）对 30 品种成功率 93%。池懒加载+20分钟自动刷新+
# 失败降权（连续失败的代理后移）。免费代理有噪声，重试策略+KlineCache 5分钟 TTL
# 确保最差情况也不会放大请求量。
# ==========================================================================
_SINA_PROXY_SOURCES = [
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
]
_SINA_PROXY_TTL = 20 * 60           # 代理池刷新间隔（秒）
_SINA_PROXY_MAX_ATTEMPTS = 6        # 单品种最多尝试代理数
_SINA_PROXY_TEST_TIMEOUT = 8        # 单个代理连通性测试超时（秒）
_SINA_PROXY_REQ_GAP = 0.35          # 相邻代理请求间隔（秒）
_sina_proxy_state = {"list": [], "updated": 0.0, "fails": {}}
_sina_proxy_lock = threading.Lock()


def _fetch_proxy_candidates():
    """从 GitHub 公开列表拉取代理 IP（返回去重 IP:PORT 候选）。"""
    try:
        import urllib.request as _urllib_req
        cands = set()
        for src in _SINA_PROXY_SOURCES:
            try:
                body = _urllib_req.urlopen(src, timeout=10).read().decode("utf-8", "replace")
                for line in body.splitlines():
                    line = line.strip()
                    if re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", line):
                        cands.add(line)
            except Exception:
                continue
        return list(cands)
    except Exception:
        return []


def _test_sina_proxy(proxy, timeout=None):
    """用新浪 stock2 日线接口（RB0）测试代理可达性；返回 bool。"""
    timeout = timeout or _SINA_PROXY_TEST_TIMEOUT
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
           "InnerFuturesNewService.getDailyKLine?symbol=RB0")
    try:
        import urllib.request as _urllib_req
        handler = _urllib_req.ProxyHandler(
            {"http": f"http://{proxy}", "https": f"http://{proxy}"})
        opener = _urllib_req.build_opener(handler)
        r = opener.open(_urllib_req.Request(url, headers=config.HEADERS_SINA),
                        timeout=timeout)
        body = r.read().decode("utf-8", "replace")
        return bool(re.search(r"\((\[.*\])\)", body, re.S))
    except Exception:
        return False


def _sina_proxy_seed():
    """从本地种子文件加载可用代理（data/sina_proxy_seed.json）。
    种子是已验证可用（新浪 stock2 通）的稳定 IP，即时使用无需等 GitHub 拉取。"""
    try:
        seed_path = os.path.join(config.BASE_DIR, "data", "sina_proxy_seed.json")
        if os.path.exists(seed_path):
            with open(seed_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [str(x).strip() for x in data if re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", str(x).strip())]
    except Exception:
        pass
    return []


def _refresh_sina_proxy_pool(force=False):
    """拉取公开代理列表并按新浪连通性过滤，结果写入池缓存。限并发15避免压垮代理源。
    首次启动优先加载本地种子（秒级可用），GitHub 拉取仅作为补充并后台等待。"""
    now = time.time()
    with _sina_proxy_lock:
        if not force and now - _sina_proxy_state["updated"] < _SINA_PROXY_TTL and _sina_proxy_state["list"]:
            return _sina_proxy_state["list"]
    # 种子文件兜底：立即返回已验证的稳定 IP（不等网络拉取）
    seed = _sina_proxy_seed()
    with _sina_proxy_lock:
        if not _sina_proxy_state["list"]:
            _sina_proxy_state["list"] = list(seed)
            _sina_proxy_state["updated"] = now
            _sina_proxy_state["fails"] = {p: 0 for p in seed}
    cands = _fetch_proxy_candidates()
    if not cands:
        return _sina_proxy_state["list"]
    working = []

    def _check(p):
        if _test_sina_proxy(p, timeout=6):
            working.append(p)

    threads = []
    for p in cands[:60]:
        th = threading.Thread(target=_check, args=(p,))
        th.start()
        threads.append(th)
        if len(threads) >= 15:
            for t in threads:
                t.join()
            threads = []
    for t in threads:
        t.join()
    # 合并：种子 + GitHub 新发现（种子优先，GitHub 补充）
    merged = []
    for p in seed + working:
        if p not in merged:
            merged.append(p)
    with _sina_proxy_lock:
        _sina_proxy_state["list"] = merged
        _sina_proxy_state["updated"] = now
        _sina_proxy_state["fails"] = {p: 0 for p in merged}
    LOG.info("新浪代理池刷新：种子%d + 新发现%d = 共%d（TTL %d秒）",
             len(seed), len(working), len(merged), _SINA_PROXY_TTL)
    return merged


def _sina_proxy_pool():
    """获取代理池（懒加载/自动刷新）。
    优先秒级返回已验证种子（不阻塞）；种子不足或过期时才触发 GitHub 拉取补充。"""
    with _sina_proxy_lock:
        pool = list(_sina_proxy_state["list"])
        if pool:
            return pool
    # 种子文件兜底（本地磁盘读取，毫秒级）
    seed = _sina_proxy_seed()
    if seed:
        with _sina_proxy_lock:
            if not _sina_proxy_state["list"]:
                _sina_proxy_state["list"] = list(seed)
                _sina_proxy_state["updated"] = time.time()
                _sina_proxy_state["fails"] = {p: 0 for p in seed}
        return list(seed)
    return _refresh_sina_proxy_pool(force=True)


def _fetch_daily_via_proxy(symbol):
    """新浪日线走代理池：多代理随机起点轮换重试，返回 bars 或 []。
    每次请求先测试连通性（同域名日线），失败换下一个。请求间隔0.35s防限流。"""
    pool = _sina_proxy_pool()
    if not pool:
        return []
    url = (f"https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
           f"InnerFuturesNewService.getDailyKLine?symbol={symbol}")
    # 随机起点轮换：避免每轮都从同一批开始被限流
    import random as _rnd
    start = _rnd.randint(0, max(0, len(pool) - 1))
    order = pool[start:] + pool[:start]
    for proxy in order[:_SINA_PROXY_MAX_ATTEMPTS]:
        # 跳过连续失败较多的代理（降权但不完全剔除，因为免费代理不稳定）
        with _sina_proxy_lock:
            fail_count = _sina_proxy_state["fails"].get(proxy, 0)
        if fail_count > 3:
            time.sleep(_SINA_PROXY_REQ_GAP)
            continue
        try:
            import urllib.request as _urllib_req
            handler = _urllib_req.ProxyHandler(
                {"http": f"http://{proxy}", "https": f"http://{proxy}"})
            opener = _urllib_req.build_opener(handler)
            r = opener.open(_urllib_req.Request(url, headers=config.HEADERS_SINA),
                            timeout=_SINA_PROXY_TEST_TIMEOUT)
            body = r.read().decode("utf-8", "replace")
            if r.status == 456:
                with _sina_proxy_lock:
                    _sina_proxy_state["fails"][proxy] = _sina_proxy_state["fails"].get(proxy, 0) + 1
                time.sleep(_SINA_PROXY_REQ_GAP)
                continue
            m = re.search(r"\((\[.*\])\)", body, re.S)
            if m and len(m.group(1)) > 100:
                with _sina_proxy_lock:
                    _sina_proxy_state["fails"][proxy] = 0
                return json.loads(m.group(1))
            with _sina_proxy_lock:
                _sina_proxy_state["fails"][proxy] = _sina_proxy_state["fails"].get(proxy, 0) + 1
        except Exception:
            with _sina_proxy_lock:
                _sina_proxy_state["fails"][proxy] = _sina_proxy_state["fails"].get(proxy, 0) + 1
        time.sleep(_SINA_PROXY_REQ_GAP)
    return []


def _fetch_intraday_via_proxy(symbol, period=30, lmt=20):
    """新浪分钟K走代理池（第118轮：stock2 被 WAF 456 封锁时，代理 IP 独立出口可绕过）。
    返回新浪原始结构 [{d,o,h,l,c,v,p,s}, ...] 或 []。多代理随机起点轮换重试。"""
    pool = _sina_proxy_pool()
    if not pool:
        return []
    url = (f"https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
           f"InnerFuturesNewService.getFewMinLine?symbol={symbol}&type={int(period)}")
    import random as _rnd
    start = _rnd.randint(0, max(0, len(pool) - 1))
    order = pool[start:] + pool[:start]
    for proxy in order[:_SINA_PROXY_MAX_ATTEMPTS]:
        with _sina_proxy_lock:
            fail_count = _sina_proxy_state["fails"].get(proxy, 0)
        if fail_count > 3:
            time.sleep(_SINA_PROXY_REQ_GAP)
            continue
        try:
            import urllib.request as _urllib_req
            handler = _urllib_req.ProxyHandler(
                {"http": f"http://{proxy}", "https": f"http://{proxy}"})
            opener = _urllib_req.build_opener(handler)
            r = opener.open(_urllib_req.Request(url, headers=config.HEADERS_SINA),
                            timeout=_SINA_PROXY_TEST_TIMEOUT)
            body = r.read().decode("utf-8", "replace")
            if r.status == 456:
                with _sina_proxy_lock:
                    _sina_proxy_state["fails"][proxy] = _sina_proxy_state["fails"].get(proxy, 0) + 1
                time.sleep(_SINA_PROXY_REQ_GAP)
                continue
            m = re.search(r"\((\[.*\])\)", body, re.S)
            if m and len(m.group(1)) > 100:
                with _sina_proxy_lock:
                    _sina_proxy_state["fails"][proxy] = 0
                bars = json.loads(m.group(1))
                return bars[-int(lmt):] if lmt else bars
            with _sina_proxy_lock:
                _sina_proxy_state["fails"][proxy] = _sina_proxy_state["fails"].get(proxy, 0) + 1
        except Exception:
            with _sina_proxy_lock:
                _sina_proxy_state["fails"][proxy] = _sina_proxy_state["fails"].get(proxy, 0) + 1
        time.sleep(_SINA_PROXY_REQ_GAP)
    return []



def fetch_intraday_kline(symbol, period=30, retry=1):
    """新浪期货分钟K线。实测 getFewMinLine 支持 type=1/5/15/30/60，返回结构同日K。

    2026-09-01 晚补测（第14轮曾误判"新浪无1分钟"）：type=1 一分钟K同样固定返回1023根、
    64/64品种全覆盖、零断连（约覆盖最近2.5个交易日），主连与具体合约均可取；故1m主源
    由东财push2his（本机持续限流）切换为新浪主连。
    云服务器优先（2026-09-11 第120轮）：SINA_SERVER_ENABLED=True 时分钟K先走云服务器出口
    （10 台 round-robin，本机 IP 不碰新浪 stock2，永不被封），失败回落白名单/本机直连。
    白名单优先（2026-09-11）：SINA_WHITELIST_PROXY 生效时走云主机白名单出口，失败回落本机直连。"""
    period = int(period)
    if period not in (1, 5, 15, 30, 60):
        raise ValueError(f"不支持的分钟周期: {period}")
    # 云服务器优先（与 MinuteCollector.collect 同源；返回新浪原始结构 [{d,o,h,l,c,v,p,s},...]，
    # 与下方 jsonp 解析结果格式完全一致，可直接返回）
    if getattr(config, "SINA_SERVER_ENABLED", False):
        try:
            from server_minute_client import _fetch_via_server
            srv_bars = _fetch_via_server(symbol, period, 1023)
            if srv_bars:
                return srv_bars
        except Exception:
            pass
    url = (f"https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
           f"InnerFuturesNewService.getFewMinLine?symbol={symbol}&type={period}")
    # 全局新浪节流（3s/次，防并发触发 456）→ 白名单出口优先 → 本机直连
    _sina_throttle()
    wl_bars = _sina_whitelist_get(url)
    if wl_bars:
        return wl_bars
    # 白名单未配置/失败 → 本机直连（原有逻辑）
    last_err = None
    for _ in range(retry + 1):
        _sina_throttle()
        try:
            r = http.get(url, headers=config.HEADERS_SINA, timeout=config.TIMEOUT)
            r.encoding = "utf-8"
            m = re.search(r"\((\[.*\])\)", r.text, re.S)
            if m:
                return json.loads(m.group(1))
            last_err = "响应中未找到分钟K线数组"
        except Exception as e:
            last_err = str(e)
        time.sleep(0.3)
    raise RuntimeError(f"{period}分钟K线获取失败({symbol}): {last_err}")


def _mean(values):
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _sample_std(values):
    values = list(values)
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    return math.sqrt(sum((x - mean) ** 2 for x in values) / (len(values) - 1))


# ================= G7（第30轮）：多窗口时序动量 TSMOM(63/126/252)，纯函数、零网络、实时/离线共用同一口径 =================
def _lookback_return(closes, end, lookback):
    """时点 end 相对 end-lookback 的累计简单收益（与 ret5/ret20 同口径）；历史不足/价格非法返回 None。"""
    j = end - int(lookback)
    if j < 0 or lookback <= 0:
        return None
    base, now = closes[j], closes[end]
    if not (base > 0 and now > 0 and math.isfinite(base) and math.isfinite(now)):
        return None
    return now / base - 1.0


def _window_std(closes, end, window):
    """[end-window+1, end] 区间日简单收益的样本标准差；样本<2 返回 None。只用 end 及之前数据，无未来信息。"""
    lo = end - int(window)
    if lo < 0 or window < 2:
        return None
    rets = [closes[k] / closes[k - 1] - 1.0
            for k in range(lo + 1, end + 1) if closes[k - 1] > 0]
    if len(rets) < 2:
        return None
    return _sample_std(rets)


def tsmom_at(closes, end, lookbacks=None, ann=None, z_clip=None):
    """单时点 end 的多窗口时序动量特征（纯函数）。

    对每个回看窗 L：
      ret{L}   = close[end]/close[end-L]-1，原始累计收益（历史不足为 None）；
      tsmom{L} = ret{L} / (过去 L 日日收益样本std * sqrt(ann))，即"每单位年化波动的趋势收益"，
                 跨窗口量纲一致、可等权合成（AQR time-series momentum 的波动调整 z 分版本）；
      blend    = 对可得窗口 tanh(clip(tsom{L},±z_clip)) 等权平均 ∈(-1,1)，影子合成因子。
    历史不足的窗口缺省 None、绝不编造；至少一个窗口可得时才有 blend。
    """
    lookbacks = tuple(lookbacks or config.TSMOM_LOOKBACKS)
    ann = int(ann or config.TSMOM_ANN)
    z_clip = float(config.TSMOM_Z_CLIP if z_clip is None else z_clip)
    feat, zs = {}, []
    for L in lookbacks:
        r = _lookback_return(closes, end, L)
        feat["ret%d" % L] = r
        z = None
        if r is not None:
            sd = _window_std(closes, end, L)
            if sd is not None and sd > 1e-12:
                val = r / (sd * math.sqrt(ann))
                if math.isfinite(val):
                    z = val
                    zs.append(max(-z_clip, min(z_clip, val)))
        feat["tsmom%d" % L] = z
    feat["blend"] = (sum(math.tanh(z) for z in zs) / len(zs)) if zs else None
    feat["n_valid"] = len(zs)
    return feat


def tsmom_features(closes, lookbacks=None, ann=None, z_clip=None):
    """序列最后时点的 TSMOM 特征（实时侧 compute_indicators 用）。"""
    if not closes:
        return _tsmom_empty(lookbacks)
    return tsmom_at(closes, len(closes) - 1, lookbacks=lookbacks, ann=ann, z_clip=z_clip)


def _tsmom_empty(lookbacks=None):
    lookbacks = tuple(lookbacks or config.TSMOM_LOOKBACKS)
    feat = {}
    for L in lookbacks:
        feat["ret%d" % L] = None
        feat["tsmom%d" % L] = None
    feat["blend"] = None
    feat["n_valid"] = 0
    return feat


def tsmom_series(closes, lookbacks=None, ann=None, z_clip=None):
    """每个时点 t 的 TSMOM 特征（离线 IC 评估用）；返回 {键: 与 closes 等长列表，暖机期为 None}，不在内部切片、O(n)。"""
    lookbacks = tuple(lookbacks or config.TSMOM_LOOKBACKS)
    keys = ["ret%d" % L for L in lookbacks] + ["tsmom%d" % L for L in lookbacks] + ["blend"]
    out = {k: [None] * len(closes) for k in keys}
    out["n_valid"] = [0] * len(closes)
    for t in range(len(closes)):
        f = tsmom_at(closes, t, lookbacks=lookbacks, ann=ann, z_clip=z_clip)
        for k in keys:
            out[k][t] = f[k]
        out["n_valid"][t] = f["n_valid"]
    return out


def _sma_series(values, period):
    out = [None] * len(values)
    if period <= 0:
        return out
    acc = 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= period:
            acc -= values[i - period]
        if i >= period - 1:
            out[i] = acc / period
    return out


def _ema_series(values, period):
    """标准EMA：前 period-1 个点为空，第 period 个点用SMA播种。"""
    out = [None] * len(values)
    if len(values) < period or period <= 0:
        return out
    alpha = 2.0 / (period + 1.0)
    ema = _mean(values[:period])
    out[period - 1] = ema
    for i in range(period, len(values)):
        ema = alpha * values[i] + (1.0 - alpha) * ema
        out[i] = ema
    return out


def _rsi_series(closes, period=14):
    """Wilder RSI 序列。"""
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains, losses = [], []
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain, avg_loss = _mean(gains), _mean(losses)
    out[period] = 100.0 if avg_loss <= 1e-12 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        avg_gain = ((period - 1) * avg_gain + max(delta, 0.0)) / period
        avg_loss = ((period - 1) * avg_loss + max(-delta, 0.0)) / period
        out[i] = 100.0 if avg_loss <= 1e-12 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def _kdj_series(highs, lows, closes, period=9):
    """KDJ 序列：K/D 初值50，J=3K-2D。"""
    ks = [None] * len(closes)
    ds = [None] * len(closes)
    js = [None] * len(closes)
    if len(closes) < period:
        return ks, ds, js
    k = d = 50.0
    for i in range(period - 1, len(closes)):
        hh = max(highs[i - period + 1:i + 1])
        ll = min(lows[i - period + 1:i + 1])
        rsv = 50.0 if abs(hh - ll) < 1e-12 else (closes[i] - ll) / (hh - ll) * 100.0
        k = 2.0 / 3.0 * k + 1.0 / 3.0 * rsv
        d = 2.0 / 3.0 * d + 1.0 / 3.0 * k
        ks[i], ds[i], js[i] = k, d, 3.0 * k - 2.0 * d
    return ks, ds, js


def _hv_at(closes, end, period):
    if end < period or period <= 0:
        return None
    seg = closes[end - period:end + 1]
    rets = [math.log(seg[i] / seg[i - 1]) for i in range(1, len(seg)) if seg[i - 1] > 0]
    if len(rets) < 5:
        return None
    return _sample_std(rets) * math.sqrt(252)


def _quantile(values, q):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return values[lo]
    return values[lo] * (hi - pos) + values[hi] * (pos - lo)


def _volatility_profile(closes):
    """HV历史分位 + 波动率锥（10/20/40/60日，分位数10/50/90）。"""
    hv20_series = [_hv_at(closes, i, 20) for i in range(len(closes))]
    hv20_hist = [v for v in hv20_series[:-1] if v is not None]
    hv20 = hv20_series[-1]
    if hv20 is not None and len(hv20_hist) >= config.TECH_VOL_PERCENTILE_MIN:
        below = sum(1 for v in hv20_hist if v <= hv20)
        hv_percentile = below / len(hv20_hist)
    else:
        hv_percentile = None
    cone = {}
    for win in (10, 20, 40, 60):
        vals = [_hv_at(closes, i, win) for i in range(len(closes))]
        vals = [v for v in vals if v is not None]
        if len(vals) >= config.TECH_VOL_PERCENTILE_MIN:
            cone[str(win)] = {"p10": _quantile(vals, 0.10),
                              "p50": _quantile(vals, 0.50),
                              "p90": _quantile(vals, 0.90),
                              "current": vals[-1],
                              "samples": len(vals)}
    return hv_percentile, cone


def _majority_side(bull_flags, bear_flags):
    bull, bear = sum(bool(x) for x in bull_flags), sum(bool(x) for x in bear_flags)
    if bull > bear:
        return 1
    if bear > bull:
        return -1
    return 0


def technical_profile(closes, highs, lows):
    """RSI/MACD/KDJ/BOLL + 短中长三周期共振，供实时分析和回测共用。"""
    n = len(closes)
    ma5_s, ma10_s, ma20_s, ma60_s = (_sma_series(closes, p) for p in (5, 10, 20, config.TECH_LONG_MA))
    ema_fast = _ema_series(closes, config.TECH_MACD_FAST)
    ema_slow = _ema_series(closes, config.TECH_MACD_SLOW)
    dif_s, dea_s = [None] * n, [None] * n
    hist_s = [None] * n
    dif_values = []
    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            dif_values.append((i, ema_fast[i] - ema_slow[i]))
    if dif_values:
        dif_only = [v for _, v in dif_values]
        dea_only = _ema_series(dif_only, config.TECH_MACD_SIGNAL)
        for (i, dif), dea in zip(dif_values, dea_only):
            dif_s[i] = dif
            dea_s[i] = dea
            hist_s[i] = None if dea is None else (dif - dea) * 2.0
    rsi_s = _rsi_series(closes, config.TECH_RSI_PERIOD)
    k_s, d_s, j_s = _kdj_series(highs, lows, closes, config.TECH_KDJ_PERIOD)

    c = closes[-1]
    ma5, ma10, ma20, ma60 = ma5_s[-1], ma10_s[-1], ma20_s[-1], ma60_s[-1]
    dif, dea, hist = dif_s[-1] or 0.0, dea_s[-1] or 0.0, hist_s[-1] or 0.0
    rsi, kdj_k, kdj_d, kdj_j = rsi_s[-1], k_s[-1], d_s[-1], j_s[-1]
    boll_mid = ma20 or 0.0
    boll_std = _sample_std(closes[-config.TECH_BOLL_PERIOD:]) if n >= config.TECH_BOLL_PERIOD else 0.0
    boll_up = boll_mid + config.TECH_BOLL_STD * boll_std
    boll_low = boll_mid - config.TECH_BOLL_STD * boll_std
    ret5 = c / closes[-6] - 1.0 if n >= 6 and closes[-6] > 0 else 0.0
    ret20 = c / closes[-21] - 1.0 if n >= 21 and closes[-21] > 0 else 0.0

    short_vote = _majority_side(
        [ma5 and c > ma5, ret5 > 0, kdj_k is not None and kdj_d is not None and kdj_k > kdj_d],
        [ma5 and c < ma5, ret5 < 0, kdj_k is not None and kdj_d is not None and kdj_k < kdj_d])
    medium_vote = _majority_side(
        [ma20 and c > ma20, dif >= dea],
        [ma20 and c < ma20, dif < dea])
    long_vote = _majority_side(
        [ma60 and c > ma60, ma20 and ma60 and ma20 > ma60],
        [ma60 and c < ma60, ma20 and ma60 and ma20 < ma60])
    vote_sum = short_vote + medium_vote + long_vote
    resonance_score = clip(vote_sum / 3.0 * config.TECH_RESONANCE_MAX,
                           -config.TECH_RESONANCE_MAX, config.TECH_RESONANCE_MAX)
    labels = {1: "多", -1: "空", 0: "中"}
    rsi_note = ""
    if rsi is not None and rsi >= config.TECH_RSI_OVERBOUGHT:
        rsi_note = "RSI超买"
    elif rsi is not None and rsi <= config.TECH_RSI_OVERSOLD:
        rsi_note = "RSI超卖"
    resonance_note = (f"短{labels[short_vote]}/中{labels[medium_vote]}/长{labels[long_vote]}"
                      f"，共振分{resonance_score:+.2f}")
    hv_percentile, vol_cone = _volatility_profile(closes)
    return {"ma5": ma5 or 0.0, "ma10": ma10 or 0.0, "ma20": ma20 or 0.0,
            "ma60": ma60 or 0.0, "ret5": ret5, "ret20": ret20,
            "macd_dif": dif, "macd_dea": dea, "macd_hist": hist,
            "rsi14": rsi if rsi is not None else 0.0,
            "kdj_k": kdj_k if kdj_k is not None else 0.0,
            "kdj_d": kdj_d if kdj_d is not None else 0.0,
            "kdj_j": kdj_j if kdj_j is not None else 0.0,
            "boll_up": boll_up, "boll_mid": boll_mid, "boll_low": boll_low,
            "short_vote": short_vote, "medium_vote": medium_vote,
            "long_vote": long_vote, "vote_sum": vote_sum,
            "resonance_score": resonance_score, "resonance_note": resonance_note,
            "rsi_note": rsi_note, "hv_percentile": hv_percentile,
            "vol_cone": vol_cone}


def _bar_dt(bar):
    text = str(bar.get("d") or "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def aggregate_30m_to_60m(bars):
    """把连续两根30分钟K线聚合成60分钟；午休/夜盘间隔不连续时不跨休市段硬拼。"""
    out = []
    pending = None
    pending_dt = None
    for b in bars:
        c = _f(b.get("c"))
        if c <= 0:
            continue
        nb = {"d": b.get("d"), "o": _f(b.get("o")), "h": _f(b.get("h")),
              "l": _f(b.get("l")), "c": c, "v": _f(b.get("v"))}
        dt = _bar_dt(nb)
        if pending is None or dt is None or pending_dt is None or \
                abs(dt - pending_dt - timedelta(minutes=30)).total_seconds() > 1:
            pending, pending_dt = nb, dt
            continue
        merged = {"d": nb["d"], "o": pending["o"],
                  "h": max(pending["h"], nb["h"]),
                  "l": min(pending["l"], nb["l"]), "c": nb["c"],
                  "v": pending["v"] + nb["v"]}
        out.append(merged)
        pending, pending_dt = None, None
    return out


def compute_intraday_resonance(bars30):
    """30分钟做短/中周期，30m聚合出的60分钟做中/长周期，输出分钟级共振。"""
    bars30 = [b for b in (bars30 or []) if _f(b.get("c")) > 0][-config.INTRADAY_30M_BARS:]
    if len(bars30) < 35:
        return {"ok": False, "resonance_score": 0.0, "resonance_note": "30分钟K线不足",
                "bars30": len(bars30), "bars60": 0}
    c30, h30, l30 = ([_f(b[k]) for b in bars30] for k in ("c", "h", "l"))
    p30 = technical_profile(c30, h30, l30)
    bars60 = aggregate_30m_to_60m(bars30)
    if len(bars60) < config.INTRADAY_60M_MIN_BARS:
        return {"ok": False, "resonance_score": 0.0, "resonance_note": "60分钟聚合K线不足",
                "bars30": len(bars30), "bars60": len(bars60)}
    c60, h60, l60 = ([_f(b[k]) for b in bars60] for k in ("c", "h", "l"))
    p60 = technical_profile(c60, h60, l60)
    vote30 = p30["short_vote"] + p30["medium_vote"]      # -2..2
    vote60 = p60["medium_vote"] + p60["long_vote"]      # -2..2
    total = vote30 + vote60
    side = 1 if total > 0 else (-1 if total < 0 else 0)
    score = clip(side * abs(total) / 4.0 * config.INTRADAY_RESONANCE_MAX,
                 -config.INTRADAY_RESONANCE_MAX, config.INTRADAY_RESONANCE_MAX)
    labels = {1: "多", -1: "空", 0: "中"}
    note = (f"30m短{labels[p30['short_vote']]}/中{labels[p30['medium_vote']]}，"
            f"60m中{labels[p60['medium_vote']]}/长{labels[p60['long_vote']]}，"
            f"分钟共振分{score:+.2f}")
    return {"ok": True, "resonance_score": score, "resonance_note": note,
            "vote30": vote30, "vote60": vote60, "p30": p30, "p60": p60,
            "bars30": len(bars30), "bars60": len(bars60),
            "last30_time": bars30[-1].get("d", ""), "last60_time": bars60[-1].get("d", "")}


def compute_indicators(bars, max_bars=140):
    """由日线计算 HV20/HV60、MA/ATR/动量、RSI/MACD/KDJ/BOLL、多周期共振与波动率锥。"""
    all_valid = [b for b in bars if _f(b.get("c")) > 0]
    # G7：ret252 需≥253根，必须在下面 max_bars=140 截断之前用完整序列计算；
    # 仅新增影子键、不参与综合分，截断后的旧指标输入与历史逐字节一致。
    tsmom = tsmom_features([_f(b["c"]) for b in all_valid])
    bars = all_valid[-max_bars:]
    if len(bars) < 10:
        raise RuntimeError("K线数据不足")
    closes = [_f(b["c"]) for b in bars]
    highs = [_f(b["h"]) for b in bars]
    lows = [_f(b["l"]) for b in bars]

    def ann_std(n):
        return _hv_at(closes, len(closes) - 1, n) or 0.0

    hv20, hv60 = ann_std(20), ann_std(60)
    tech = technical_profile(closes, highs, lows)
    trs = []
    for i in range(1, len(bars)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    atr = sum(trs[-14:]) / len(trs[-14:]) if trs else closes[-1] * 0.015
    n = len(closes)
    return {"close": closes[-1], "prev_close": closes[-2] if n >= 2 else closes[-1],
            "day_chg": (closes[-1] / closes[-2] - 1.0) if n >= 2 else 0.0,
            "hv20": hv20, "hv60": hv60,
            "ma5": tech["ma5"], "ma10": tech["ma10"], "ma20": tech["ma20"],
            "atr": atr, "ret5": tech["ret5"], "ret20": tech["ret20"],
            # G7 多窗口时序动量（影子键，不进 analyzer 综合分；历史不足为 None）
            "ret63": tsmom["ret63"], "ret126": tsmom["ret126"], "ret252": tsmom["ret252"],
            "tsmom63": tsmom["tsmom63"], "tsmom126": tsmom["tsmom126"],
            "tsmom252": tsmom["tsmom252"], "tsmom_blend": tsmom["blend"],
            "tsmom_n_valid": tsmom["n_valid"],
            "tech": tech, "hv_percentile": tech["hv_percentile"],
            "vol_cone": tech["vol_cone"],
            "last_date": bars[-1].get("d", "")}


def _kline_fallback(cat):
    """日线指标失败/盘后跳过时的统一回退值（默认波动率，结构恒等，供失败缓存复用）。"""
    return {"close": 0.0, "prev_close": 0.0, "day_chg": 0.0,
            "hv20": config.DEFAULT_HV.get(cat, 0.25),
            "hv60": config.DEFAULT_HV.get(cat, 0.25),
            "ma5": 0.0, "ma10": 0.0, "ma20": 0.0,
            "atr": 0.0, "ret5": 0.0, "ret20": 0.0,
            "ret63": None, "ret126": None, "ret252": None,
            "tsmom63": None, "tsmom126": None, "tsmom252": None,
            "tsmom_blend": None, "tsmom_n_valid": 0,
            "tech": {}, "hv_percentile": None, "vol_cone": {},
            "last_date": ""}


class KlineCache:
    """日线指标缓存（默认30分钟刷新），失败时回退到板块默认波动率。
    第116轮新增收盘边沿定格：交易→非交易切换时一次性补拉全品种日线，
    盘后全程零请求（get/refresh 已跳过），复用收盘定格缓存。
    第116轮P0/P1：新浪 WAF 封锁期间失败缓存拉长到 60 分钟 + 封锁期完全短路，
    防止封锁期每 5-10 分钟全品种重试放大 WAF（此前 KLINE_FAIL_TTL=5min < 分析周期10min）。"""

    def __init__(self):
        self.cache = {}
        self.fail_cache = {}       # code -> (失败时间戳, fallback)。第115轮：新浪456封锁期缓存失败，
                                   # 第116轮P0：封锁期 TTL 动态拉长到 60 分钟（防放大器）
        self.intraday_cache = {}
        self.lock = threading.Lock()
        self._was_trading = None   # 第116轮：收盘边沿检测，True→False 时补拉一次

    @staticmethod
    def _fail_ttl():
        """失败缓存有效时长：WAF 封锁期 60 分钟（防重试放大），否则 5 分钟。"""
        if _sina_waf_blocked():
            return 60 * 60
        return config.KLINE_FAIL_TTL

    def get(self, code, cat):
        now = time.time()
        fallback = _kline_fallback(cat)
        with self.lock:
            hit = self.cache.get(code)
            if hit and now - hit[0] < config.KLINE_TTL:
                return hit[1], True
            # 失败短期缓存：封锁期 60 分钟/正常 5 分钟内不重试（封锁期直接复用 fallback）。
            # 第118轮：历史脏数据可能把 None 写入 fail_cache（refresh_if_stale 旧版），
            # 命中时若值非 dict 一律替换为默认 fallback，杜绝 analyzer dict(None) 崩溃。
            fhit = self.fail_cache.get(code)
            if fhit and now - fhit[0] < self._fail_ttl():
                cached_ind = fhit[1]
                if cached_ind is None or not isinstance(cached_ind, dict):
                    self.fail_cache[code] = (now, fallback)
                    cached_ind = fallback
                return cached_ind, False
        # 第116轮P0：新浪 WAF 封锁期——有缓存/失败缓存直接复用；
        # 无缓存且失败缓存已过期时仍调 fetch_daily_kline（内部跳过新浪/akshare，
        # 但会尝试天勤独立通道），失败落入 60 分钟失败缓存，不再高频重试。
        if _sina_waf_blocked():
            if hit:
                return hit[1], True
            fallback = _kline_fallback(cat)
            try:
                bars = fetch_daily_kline(code)   # 内部已短路新浪，走天勤/CDP
                if bars:
                    ind = compute_indicators(bars)
                    with self.lock:
                        self.cache[code] = (now, ind)
                        self.fail_cache.pop(code, None)
                    return ind, True
            except Exception:
                pass
            with self.lock:
                self.fail_cache[code] = (now, fallback)
            return fallback, False
        # 第116轮：新浪日线收盘后（非交易时段）不再请求
        # 收盘后价格不再变化，且新浪 stock2 对盘后请求返回 WAF 拦截页（HTTP 200 但无K线数组），
        # 造成大量「响应中未找到K线数组」报警（9/10 实测 1765 次）；盘后直接复用已有缓存即可。
        if not _in_trading():
            # 有交易时段内缓存的数据（收盘时的最终值），直接复用，价格不再变化
            if hit:
                return hit[1], True
            # 程序启动时可能在非交易时段、缓存尚空：用失败回退值（默认波动率），不发任何请求
            fallback = _kline_fallback(cat)
            with self.lock:
                self.fail_cache[code] = (now, fallback)
            return fallback, False
        try:
            bars = fetch_daily_kline(code)
            ind = compute_indicators(bars)
            with self.lock:
                self.cache[code] = (now, ind)
                self.fail_cache.pop(code, None)
            return ind, True
        except Exception as e:
            LOG.warning("%s 日线指标获取失败，使用默认波动率: %s", code, e)
            fallback = _kline_fallback(cat)
            with self.lock:
                self.fail_cache[code] = (now, fallback)
            return fallback, False

    def refresh_if_stale(self, code, cat, margin=0.9):
        """缓存即将过期时提前在后台刷新，避免主分析周期被拉长。
        第116轮：非交易时段跳过——收盘后日线数据不变，新浪 stock2 会对盘后请求返回WAF拦截页。
        第116轮P1：WAF 封锁期完全跳过——失败缓存已拉长到60分钟，不需要后台刷新重试。"""
        if not _in_trading():
            return
        if _sina_waf_blocked():
            return
        now = time.time()
        with self.lock:
            hit = self.cache.get(code)
            fhit = self.fail_cache.get(code)
            if hit and now - hit[0] < config.KLINE_TTL * margin:
                return
            if fhit and now - fhit[0] < config.KLINE_FAIL_TTL * margin:
                return
        try:
            bars = fetch_daily_kline(code)
            ind = compute_indicators(bars)
            with self.lock:
                self.cache[code] = (time.time(), ind)
                self.fail_cache.pop(code, None)
        except Exception as e:
            LOG.debug("%s 日线后台预刷新失败: %s", code, e)
            with self.lock:
                # 后台预刷新失败也进入失败缓存，防止主线程下一轮再次命中实时请求。
                # 必须存 fallback dict（默认波动率）而非 None——否则 get() 命中返回 (None, False)，
                # analyzer `ind = dict(ind)` 直接 TypeError 崩溃（9/11 夜盘实测 01:47:38 触雷）。
                if code not in self.fail_cache:
                    self.fail_cache[code] = (time.time(), _kline_fallback(cat))

    def maybe_close_snapshot(self, watchlist):
        """第116轮：收盘边沿一次性定格——检测 交易→非交易 切换，
        在切换瞬间对全品种补拉一次日线（新浪此时通常尚未封锁或刚开始封锁，
        可用）；之后盘后 get/refresh 均跳过，直接复用这份收盘定格缓存。
        午休(11:30-13:30) 期间虽然非交易，但不是收盘（下午继续交易同一日线），
        不触发定格。首次启动仅记录状态不补拉，避免启动即轰炸新浪。"""
        trading = _in_trading()
        was = self._was_trading
        self._was_trading = trading
        # 仅在 True→False（刚收盘）时触发；首次调用/持续盘中/重新开盘均跳过
        if was is None or not was or trading:
            return
        # 午休过滤：11:30-13:30 切出非交易不是收盘，日线下午还会更新
        t = datetime.now().hour * 60 + datetime.now().minute
        if 11 * 60 + 30 <= t < 13 * 60 + 30:
            return
        code_cats = [(meta["code"], meta["cat"]) for _name, meta in (watchlist or [])]
        if not code_cats:
            return
        LOG.info("收盘定格：补拉 %d 个品种日线并固化缓存（盘后不再请求）", len(code_cats))
        now = time.time()
        for code, cat in code_cats:
            try:
                bars = fetch_daily_kline(code)
                if not bars:
                    continue
                ind = compute_indicators(bars)
                with self.lock:
                    self.cache[code] = (now, ind)
                    self.fail_cache.pop(code, None)
            except Exception as e:
                # 收盘瞬间若新浪已封锁（HTTP 200 无K线数组），沿用盘中缓存，不写 fail_cache
                LOG.debug("%s 收盘定格补拉失败（沿用盘中缓存）: %s", code, e)

    def _load_intraday(self, code):
        bars = fetch_intraday_kline(code, period=30, retry=1)
        ind = compute_intraday_resonance(bars)
        if not ind.get("ok"):
            raise RuntimeError(ind.get("resonance_note", "分钟共振不可用"))
        return ind

    def get_intraday(self, code, cat=None):
        now = time.time()
        with self.lock:
            hit = self.intraday_cache.get(code)
            if hit and now - hit[0] < config.INTRADAY_KLINE_TTL:
                return hit[1], True
        # 第116轮：盘后分钟K冻结不再变化，且同属被新浪封锁的 stock2 主机，跳过请求
        if not _in_trading():
            if hit:
                return hit[1], True
            return {"ok": False, "resonance_score": 0.0,
                    "resonance_note": "分钟级暂缺", "bars30": 0, "bars60": 0}, False
        try:
            ind = self._load_intraday(code)
            with self.lock:
                self.intraday_cache[code] = (now, ind)
            return ind, True
        except Exception as e:
            LOG.debug("%s 30/60分钟共振获取失败: %s", code, e)
            return {"ok": False, "resonance_score": 0.0,
                    "resonance_note": "分钟级暂缺", "bars30": 0, "bars60": 0}, False

    def refresh_intraday_if_stale(self, code, cat=None, margin=0.9):
        # 第116轮：盘后跳过——分钟K冻结，同 stock2 主机被新浪盘后封锁
        if not _in_trading():
            return
        now = time.time()
        with self.lock:
            hit = self.intraday_cache.get(code)
            if hit and now - hit[0] < config.INTRADAY_KLINE_TTL * margin:
                return
        try:
            ind = self._load_intraday(code)
            with self.lock:
                self.intraday_cache[code] = (time.time(), ind)
        except Exception as e:
            LOG.debug("%s 30/60分钟后台预刷新失败: %s", code, e)

    def warm_intraday(self, code_cat_pairs, workers=None):
        """一轮分析前并发预热分钟K线，返回 {code: (ind, ok)}；失败品种不阻断主流程。
        第116轮：盘后分钟K冻结且同属新浪封锁主机，直接复用缓存/回退，不发请求。"""
        pairs = list(code_cat_pairs)
        workers = max(1, workers or config.INTRADAY_WORKERS)
        now = time.time()
        trading = _in_trading()
        stale = []
        out = {}
        with self.lock:
            for code, cat in pairs:
                hit = self.intraday_cache.get(code)
                if hit and now - hit[0] < config.INTRADAY_KLINE_TTL:
                    out[code] = (hit[1], True)
                elif not trading and hit:
                    out[code] = (hit[1], True)   # 盘后复用过期缓存（分钟K不再变化）
                else:
                    stale.append(code)
        if not trading:
            for code in stale:
                out[code] = ({"ok": False, "resonance_score": 0.0,
                              "resonance_note": "分钟级暂缺",
                              "bars30": 0, "bars60": 0}, False)
            return out
        if stale:
            with ThreadPoolExecutor(max_workers=min(workers, len(stale))) as pool:
                futs = {pool.submit(self._load_intraday, code): code for code in stale}
                for fut in as_completed(futs):
                    code = futs[fut]
                    try:
                        ind = fut.result()
                        with self.lock:
                            self.intraday_cache[code] = (time.time(), ind)
                        out[code] = (ind, True)
                    except Exception as e:
                        LOG.debug("%s 30/60分钟并发预热失败: %s", code, e)
                        out[code] = ({"ok": False, "resonance_score": 0.0,
                                      "resonance_note": "分钟级暂缺",
                                      "bars30": 0, "bars60": 0}, False)
        return out
