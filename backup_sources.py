# -*- coding: utf-8 -*-
r"""第116轮：备用数据源模块 backup_sources.py。

项目数据层长期依赖新浪(主)+东财(兜底)，2026-09-10 起新浪对盘后日线请求返回
WAF 拦截页、东财 push2his 对 Python http 客户端按 TLS 指纹封锁——单一来源风险
暴露。本模块聚合 ABC 三级备用源，供主链在既有源失败时降级：

  A 级（零新依赖，akshare 已装）：
    - daily_kline_akshare  : 新浪 stock 路径日线（akshare 封装，与项目 stock2 路径不同）
    - inventory_em_akshare : 东财库存近60日（futures_inventory_em）
    - basis_daily_akshare  : 生意社历史基差（futures_spot_price_daily）
    - hold_pos_sina_akshare: 新浪会员持仓排名（futures_hold_pos_sina）
  B 级（需注册账户，免费）：
    - tqsdk_quote / tqsdk_kline : 天勤 TqSdk（装 E 盘 vendor，快期账户可选）
  C 级（可选依赖）：
    - curl_cffi 封装 : TLS 指纹伪装请求（curl_cffi 已装），供东财 TLS 封锁场景

设计铁律（与主链的契约）：
  1. 全部可选加载：任一依赖缺失 -> 对应源 None，主链行为与旧版逐字节一致。
  2. 失败静默：任何异常返回 None / {}，绝不抛出（调用方按空结果降级）。
  3. 不写主链缓存：返回裸数据，由调用方（KlineCache/DataRouter）决定缓存策略。
  4. 结构对齐：日线返回 [{d,o,h,l,c,v,p,s}, ...]（同 futures_data.fetch_daily_kline）。
"""
import os
import sys
import time
import threading

import config
from utils import LOG

# ---------------- 可选依赖探测（零影响主链） ----------------

_AK = None          # akshare 模块或 None
try:
    import akshare as _AK
    _AK_AVAILABLE = True
except Exception as _e:  # pragma: no cover - 环境缺依赖时静默降级
    _AK_AVAILABLE = False
    LOG.debug("akshare 不可用: %s", _e)

_TQSDK_AVAILABLE = False
try:
    # E 盘 vendor：两个候选路径——项目根目录旁/量化目录旁/BACKUP_VENDOR_DIR 覆盖
    _vendor_candidates = [
        getattr(config, "BACKUP_VENDOR_DIR", None),
        os.path.join(os.path.dirname(os.path.dirname(config.BASE_DIR)), "vendor"),  # E:\LHsystem\vendor
        os.path.join(os.path.dirname(config.BASE_DIR), "vendor"),                  # E:\LHsystem\量化\vendor
    ]
    for _v in [v for v in _vendor_candidates if v and os.path.isdir(v)]:
        if _v not in sys.path:
            sys.path.insert(0, _v)
        import tqsdk  # noqa: F401
        _TQSDK_AVAILABLE = True
        break
except Exception as _e:  # pragma: no cover
    LOG.debug("tqsdk 不可用: %s", _e)

_CURL_AVAILABLE = False
try:
    import curl_cffi  # noqa: F401
    _CURL_AVAILABLE = True
except Exception as _e:  # pragma: no cover
    LOG.debug("curl_cffi 不可用: %s", _e)


def availability():
    """返回各备用源可用性摘要（供看板/日志展示）。"""
    return {
        "akshare": _AK_AVAILABLE,
        "tqsdk": _TQSDK_AVAILABLE,
        "curl_cffi": _CURL_AVAILABLE,
    }


# ---------------- A 级：akshare 封装 ----------------

# akshare 的 futures_zh_daily_sina 返回列：date open high low close volume hold settle
_DAILY_COLS = ("date", "open", "high", "low", "close", "volume", "hold", "settle")


def _df_to_daily(df):
    """akshare 日线 DataFrame -> 项目日线 [{d,o,h,l,c,v,p,s}]（对齐 fetch_daily_kline）。"""
    if df is None or getattr(df, "empty", True):
        return None
    bars = []
    try:
        for _, r in df.iterrows():
            d = str(r.get("date", ""))[:10]
            o, h, l, c = (float(r.get(k, 0.0)) for k in ("open", "high", "low", "close"))
            v = float(r.get("volume", 0.0))
            p = float(r.get("hold", 0.0))
            s = float(r.get("settle", 0.0))
            if c <= 0 or h <= 0 or l <= 0:
                continue
            bars.append({"d": d, "o": o, "h": h, "l": l, "c": c, "v": v, "p": p, "s": s})
    except Exception as _e:  # pragma: no cover
        LOG.debug("akshare 日线转换失败: %s", _e)
        return None
    return bars or None


def daily_kline_akshare(code, retry=1):
    """日线第4源：akshare 新浪 stock 路径（futures_zh_daily_sina，symbol=RB0）。
    与项目 stock2.finance.sina.com.cn 是不同域名/路径，新浪封锁 stock2 时此源独立可用。
    返回 [{d,o,h,l,c,v,p,s}] 或 None。"""
    if not _AK_AVAILABLE:
        return None
    last_err = None
    for _ in range(max(1, retry + 1)):
        try:
            df = _AK.futures_zh_daily_sina(symbol=str(code).upper())
            bars = _df_to_daily(df)
            if bars:
                return bars
            last_err = "empty"
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
            time.sleep(0.5)
    LOG.debug("日线第4源 akshare(%s) 失败: %s", code, last_err)
    return None


def inventory_em_akshare(symbol_cn):
    """东财库存近60日（akshare futures_inventory_em，symbol 为中文名如 螺纹钢）。
    返回 [{d, v}] 或 None。"""
    if not _AK_AVAILABLE:
        return None
    try:
        df = _AK.futures_inventory_em(symbol=str(symbol_cn))
        if df is None or df.empty:
            return None
        rows = []
        # 列通常为 日期/库存；兼容常见列名
        dcol = next((c for c in df.columns if "日期" in str(c)), df.columns[0])
        vcol = next((c for c in df.columns if "库存" in str(c) or "仓单" in str(c)),
                    df.columns[1] if len(df.columns) > 1 else None)
        if vcol is None:
            return None
        for _, r in df.iterrows():
            try:
                rows.append({"d": str(r[dcol])[:10], "v": float(r[vcol])})
            except (TypeError, ValueError):
                continue
        return rows or None
    except Exception as _e:  # pragma: no cover
        LOG.debug("东财库存 akshare(%s) 失败: %s", symbol_cn, _e)
        return None


def basis_daily_akshare(start_day, end_day, vars_list):
    """生意社历史基差（akshare futures_spot_price_daily）。
    返回 [(d, symbol, spot, near_contract_price, dominant_contract_price)] 或 None。"""
    if not _AK_AVAILABLE:
        return None
    try:
        df = _AK.futures_spot_price_daily(start_day=str(start_day), end_day=str(end_day),
                                          vars_list=list(vars_list))
        if df is None or df.empty:
            return None
        rows = []
        for _, r in df.iterrows():
            try:
                rows.append((
                    str(r.get("date", ""))[:10],
                    str(r.get("symbol", "")),
                    float(r.get("spot_price", 0.0) or 0.0),
                    float(r.get("near_contract_price", 0.0) or 0.0),
                    float(r.get("dominant_contract_price", 0.0) or 0.0),
                ))
            except (TypeError, ValueError):
                continue
        return rows or None
    except Exception as _e:  # pragma: no cover
        LOG.debug("历史基差 akshare 失败: %s", _e)
        return None


def hold_pos_sina_akshare(symbol, contract, date_str):
    """新浪会员持仓排名前20（akshare futures_hold_pos_sina）。
    返回 DataFrame.to_dict('records') 或 None。"""
    if not _AK_AVAILABLE:
        return None
    try:
        df = _AK.futures_hold_pos_sina(symbol=str(symbol), contract=str(contract),
                                       date=str(date_str))
        if df is None or df.empty:
            return None
        return df.to_dict("records")
    except Exception as _e:  # pragma: no cover
        LOG.debug("新浪持仓排名 akshare(%s/%s) 失败: %s", symbol, contract, _e)
        return None


# ---------------- B 级：天勤 TqSdk（E 盘 vendor，快期账户可选） ----------------
# 设计：调用线程内维护 TqApi 单例 + 按需订阅（只订请求的品种）。
# 全量订阅 64 品种实测过慢（每品种 get_quote 约 5s），而 fetch_quotes 兜底场景
# 通常只有几个品种缺失，按需订阅每次约 1-8 秒即可拿到。

_tq_lock = threading.Lock()
_tq_api = None        # 调用线程内 TqApi 单例（不跨线程共享）
_tq_cache = {}        # code(RB0) -> Quote 引用
_tq_connecting = False


def _tq_auth():
    """从环境变量读取快期账户；未配置返回 None（TqSdk 不启动）。"""
    acct = os.environ.get("TQ_ACCOUNT", "").strip()
    pwd = os.environ.get("TQ_PASSWORD", "").strip()
    if not acct or not pwd:
        return None
    return acct, pwd


def _tq_pct(v):
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _tq_build_quote(code, q):
    """把天勤 Quote 对象转成与 futures_data._parse_quote 对齐的 dict。"""
    return {
        "name": str(code).rstrip("0"),
        "latest": _tq_pct(getattr(q, "last_price", 0.0)),
        "open": _tq_pct(getattr(q, "open", 0.0)),
        "high": _tq_pct(getattr(q, "highest", 0.0)),
        "low": _tq_pct(getattr(q, "lowest", 0.0)),
        "prev_settle": _tq_pct(getattr(q, "pre_settlement", 0.0)),
        "open_interest": _tq_pct(getattr(q, "open_interest", 0.0)),
        "volume": _tq_pct(getattr(q, "volume", 0.0)),
        "bid": _tq_pct(getattr(q, "bid_price1", 0.0)),
        "ask": _tq_pct(getattr(q, "ask_price1", 0.0)),
        "bid_vol": _tq_pct(getattr(q, "bid_volume1", 0.0)),
        "ask_vol": _tq_pct(getattr(q, "ask_volume1", 0.0)),
        "date": "",
        "quote_time": str(getattr(q, "datetime", ""))[:19],
    }


def _tq_ins_of(code):
    """新浪主连代码(RB0) → 天勤主连合约(KQ.m@SHFE.rb)。无映射返回 None。"""
    code = str(code).upper()
    sym = code.rstrip("0")
    meta = next((m for m in config.VARIETIES.values() if m.get("code") == code), None)
    if not meta:
        return None
    ex = meta.get("ex")
    _exmap = {"SHFE": "SHFE", "DCE": "DCE", "CZCE": "CZCE", "CFFEX": "CFFEX",
              "GFEX": "GFEX", "INE": "INE"}
    if not ex or ex not in _exmap:
        return None
    return "KQ.m@%s.%s" % (_exmap[ex], sym.lower())


def _tq_wait_with_timeout(api, timeout=20):
    """天勤 wait_update 带超时看门狗（wait_update(timeout=N) 官方有 bug 不可用，用线程实现）。
    超时后记录警告并继续——数据可能不完整，调用方兜底处理。"""
    done = threading.Event()

    def _wait():
        try:
            api.wait_update()
        except Exception:
            pass
        done.set()

    t = threading.Thread(target=_wait, daemon=True)
    t.start()
    if not done.wait(timeout):
        LOG.debug("天勤 wait_update 超时（%d秒），跳过本轮数据同步", timeout)


def _tq_get_api():
    """惰性创建 TqApi（调用线程内单例）。
    有账户时用快期账户登录；无账户时用免登录模式（天勤访客，可访问主连行情/K线）。
    连接过程包在 30 秒超时内，防止服务器无响应时永久挂起。"""
    global _tq_api, _tq_connecting
    if _tq_api is not None:
        return _tq_api
    if _tq_connecting:
        return None
    auth = _tq_auth()
    _tq_connecting = True
    _conn_result = [None]
    _conn_error = [None]

    def _connect():
        try:
            from tqsdk import TqApi, TqAuth
            if auth:
                api = TqApi(auth=TqAuth(*auth), disable_print=True)
                LOG.info("天勤 TqSdk 连接成功（快期账户模式）")
            else:
                api = TqApi(disable_print=True)
                LOG.info("天勤 TqSdk 连接成功（免登录模式）")
            _conn_result[0] = api
        except Exception as e:
            _conn_error[0] = e

    try:
        conn_thread = threading.Thread(target=_connect, daemon=True)
        conn_thread.start()
        conn_thread.join(timeout=30)
        if conn_thread.is_alive():
            LOG.warning("天勤 TqSdk 连接超时（30秒），跳过")
            return None
        if _conn_error[0]:
            LOG.debug("天勤 TqSdk 连接失败: %s", _conn_error[0])
            return None
        _tq_api = _conn_result[0]
        return _tq_api
    finally:
        _tq_connecting = False


def tqsdk_start():
    """兼容入口：幂等预热（首次调用即建连接；后续 tqsdk_quote 直接用）。"""
    _tq_get_api()


def tqsdk_quote(codes):
    """天勤行情按需订阅读取：返回 {code: quote} 或 {}。
    未配置账户/无依赖/连接失败 -> {}。
    首次连接+订阅：每个新品种约 8-10 秒（天勤 WebSocket 首帧推送延迟）；
    后续同品种秒级（TqApi 缓存有效）。"""
    if not _TQSDK_AVAILABLE:
        return {}
    api = _tq_get_api()
    if not api:
        return {}
    out = {}
    has_new = False
    # 1) 按需订阅：只订尚未订阅的品种
    for code in codes:
        ins = _tq_ins_of(code)
        if not ins:
            continue
        with _tq_lock:
            if code in _tq_cache:
                continue
        try:
            q = api.get_quote(ins)
            with _tq_lock:
                _tq_cache[code] = q
            has_new = True
        except Exception:
            continue
    # 2) 对新订阅的品种：sleep(8) + wait_update() 等待首帧到达
    #    与手动测试成功路径一致（wait_update(timeout=N) 有bug，不用带 timeout），
    #    用 _tq_wait_with_timeout 加 20 秒看门狗防挂起
    if has_new:
        time.sleep(8)
        _tq_wait_with_timeout(api, timeout=20)
    # 3) 读取所有请求的品种
    with _tq_lock:
        for code in codes:
            q = _tq_cache.get(code)
            if not q:
                continue
            try:
                px = _tq_pct(getattr(q, "last_price", 0.0))
                if px <= 0:
                    continue
                out[code] = _tq_build_quote(code, q)
            except Exception:
                continue
    return out


# 天勤日线 K 线缓存（code -> [{d,o,h,l,c,v,p,s}] 或 None）
_tq_kline_cache = {}


def tqsdk_daily_kline(code, num_bars=540):
    """天勤历史日线 K 线：返回 [{d,o,h,l,c,v,p,s}, ...]（与 fetch_daily_kline 格式对齐）。
    天勤 get_kline_serial 首次调用约 8 秒，后续同品种命中缓存秒级。
    未配置/无依赖/连接失败 -> None。"""
    if not _TQSDK_AVAILABLE:
        return None
    with _tq_lock:
        cached = _tq_kline_cache.get(code)
        if cached is not None:
            return cached
    api = _tq_get_api()
    if not api:
        return None
    ins = _tq_ins_of(code)
    if not ins:
        return None
    try:
        import datetime as _dt
        kline = api.get_kline_serial(ins, 86400, data_length=num_bars)
        # 首次等待行情数据到达（天勤需 wait_update 驱动一次；看门狗 20 秒防挂起）
        time.sleep(3)
        _tq_wait_with_timeout(api, timeout=20)
        bars = []
        if hasattr(kline, "iterrows"):
            for _, row in kline.iterrows():
                try:
                    dt_ns = row.get("datetime", 0)
                    d = _dt.datetime.fromtimestamp(dt_ns / 1e9).strftime("%Y-%m-%d")
                    bars.append({
                        "d": d,
                        "o": float(row["open"]),
                        "h": float(row["high"]),
                        "l": float(row["low"]),
                        "c": float(row["close"]),
                        "v": float(row["volume"]),
                        "p": float(row.get("open_oi", 0.0) or 0.0),
                        "s": 0.0,
                    })
                except Exception:
                    continue
        result = bars or None
        with _tq_lock:
            _tq_kline_cache[code] = result
        return result
    except Exception as e:  # pragma: no cover
        LOG.debug("天勤日线 %s 失败: %s", code, e)
        with _tq_lock:
            _tq_kline_cache[code] = None
        return None


# 天勤分钟K缓存（(code, period) -> [{dt,o,h,l,c,v,p}, ...]）
_tq_min_cache = {}


def tqsdk_minute_kline(code, period=30, num_bars=1023):
    """天勤分钟 K 线：返回 [{dt,o,h,l,c,v,p}, ...]（dt 格式 'YYYY-MM-DD HH:MM'，与新浪 fetch_sina_minute 对齐）。
    天勤 get_kline_serial 周期用秒：1m=60、5m=300、15m=900、30m=1800、60m=3600。
    首次调用约 8 秒，后续同品种命中缓存秒级。未配置/无依赖/连接失败 -> []。"""
    if not _TQSDK_AVAILABLE:
        return []
    period = int(period)
    if period not in (1, 5, 15, 30, 60):
        return []
    cache_key = (code, period)
    with _tq_lock:
        cached = _tq_min_cache.get(cache_key)
        if cached is not None:
            return cached
    api = _tq_get_api()
    if not api:
        return []
    ins = _tq_ins_of(code)
    if not ins:
        return []
    try:
        import datetime as _dt
        duration = period * 60   # 秒
        kline = api.get_kline_serial(ins, duration, data_length=int(num_bars))
        time.sleep(2)
        _tq_wait_with_timeout(api, timeout=20)
        bars = []
        if hasattr(kline, "iterrows"):
            for _, row in kline.iterrows():
                try:
                    dt_ns = row.get("datetime", 0)
                    dt_text = _dt.datetime.fromtimestamp(dt_ns / 1e9).strftime("%Y-%m-%d %H:%M")
                    bars.append({
                        "dt": dt_text,
                        "trade_date": dt_text[:10],
                        "o": float(row["open"]),
                        "h": float(row["high"]),
                        "l": float(row["low"]),
                        "c": float(row["close"]),
                        "v": float(row["volume"]),
                        "amount": 0.0,
                        "sym": code,
                        "contract": code,
                        "exchange": "",
                        "period": period,
                        "src": "tq",
                    })
                except Exception:
                    continue
        bars.sort(key=lambda b: b["dt"])
        result = bars or []
        with _tq_lock:
            _tq_min_cache[cache_key] = result
        return result
    except Exception as e:  # pragma: no cover
        LOG.debug("天勤分钟K %s %dmin 失败: %s", code, period, e)
        with _tq_lock:
            _tq_min_cache[cache_key] = []
        return []


# ---------------- C 级：curl_cffi TLS 指纹伪装（东财封锁场景备选） ----------------

def curl_get(url, headers=None, timeout=None, impersonate="chrome"):
    """用 curl_cffi 以浏览器 TLS 指纹发 GET。curl_cffi 缺失返回 None。
    返回 (status_code, text) 或 None。仅作东财等 TLS 封锁场景的最后一搏。"""
    if not _CURL_AVAILABLE:
        return None
    try:
        from curl_cffi import requests as _creq
        t = timeout or getattr(config, "TIMEOUT", 10)
        resp = _creq.get(url, headers=headers or {}, timeout=t, impersonate=impersonate)
        return resp.status_code, resp.text
    except Exception as _e:  # pragma: no cover
        LOG.debug("curl_cffi 请求失败: %s", _e)
        return None


# ---------------- 自检（python backup_sources.py 直接运行） ----------------

if __name__ == "__main__":
    import json
    print("备用源可用性:", json.dumps(availability(), ensure_ascii=False, indent=1))
    if _AK_AVAILABLE:
        bars = daily_kline_akshare("RB0", retry=0)
        print("日线第4源 RB0: %d 根, 最新=%s" % (len(bars) if bars else 0,
                                              bars[-1] if bars else None))
    else:
        print("akshare 未装，跳过日线自检")
