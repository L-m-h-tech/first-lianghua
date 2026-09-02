# -*- coding: utf-8 -*-
"""交易日历（P0-3）：解决"只按 weekday 判休市"的两个问题——
  1) 周一~周五的法定节假日仍在空转（应判休市）；
  2) 调休补班的周末交易所其实仍休市（不能判成交易日）。

数据采用"动态 + 静态"双保险，均不依赖 akshare/pandas：
  动态：期货主连日 K 的日期即期货交易日（与监控对象同一市场，最贴合）。
        主源新浪螺纹主连 RB0（程序数据层长期使用、实测稳定），
        备源东方财富上证指数日 K（push2his，免 key，偶发限流故仅作兜底）；
        本地缓存 cache/trade_dates.txt。
  静态：STATIC_HOLIDAYS 内置当年法定休市区间（证监会每年底发布次年安排，
        覆盖日 K 拿不到的"未来日期"，保证 next_trade_day/节前夜盘判断正确）。
  动态拉取失败时：静态表 + 周末规则仍可正常工作；两者都不可用时退化为工作日兜底并告警。

夜盘归属：夜盘属于其"开盘日"那个交易日（周五晚夜盘延续到周六凌晨）；
法定节假日前一交易日晚不夜盘——has_night_session() 用"下一交易日间隔"判断。

【维护】每年 12 月证监会发布次年休市安排后，把区间补进 STATIC_HOLIDAYS 即可；
不更新也只是次年新节假日期间按工作日误判，动态日 K 会在节假日发生后自动校正。
"""
import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta

import config
from http_client import http

LOG = logging.getLogger("monitor")    # 与 utils.LOG 同名，共享日志配置（避免与 utils 循环导入）
# 可重入锁：ensure() 持锁后会调用同样需要锁的 refresh()，必须用 RLock 避免线程自死锁
_lock = threading.RLock()
_dates = None            # set[date]：动态日 K 得到的交易日
_max_cached = None
_last_refresh_try = 0.0
_warned_fallback = False

# 静态法定休市区间（自然日，含首尾）。来源：证监会《关于2026年部分节假日放假和休市安排的通知》
# （证监办发〔2025〕130号，深交所/上交所2025-12-22发布）。周末本就恒休，列入仅为完整。
STATIC_HOLIDAY_RANGES = [
    (date(2026, 1, 1), date(2026, 1, 3)),     # 元旦
    (date(2026, 2, 15), date(2026, 2, 23)),   # 春节
    (date(2026, 4, 4), date(2026, 4, 6)),     # 清明节
    (date(2026, 5, 1), date(2026, 5, 5)),     # 劳动节
    (date(2026, 6, 19), date(2026, 6, 21)),   # 端午节
    (date(2026, 9, 25), date(2026, 9, 27)),   # 中秋节
    (date(2026, 10, 1), date(2026, 10, 7)),   # 国庆节
]


def _daterange(a, b):
    cur = a
    while cur <= b:
        yield cur
        cur += timedelta(days=1)


STATIC_HOLIDAYS = {d for a, b in STATIC_HOLIDAY_RANGES for d in _daterange(a, b)}


def _fetch_sina():
    """主源：新浪螺纹主连 RB0 日 K（jsonp），返回 [date,...]；期货交易日历"""
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
           "var%20t=/InnerFuturesNewService.getDailyKLine?symbol=RB0")
    r = http.get(url, headers=config.HEADERS_SINA, timeout=8)
    m = re.search(r"(\[.*\])", r.text, re.S)
    if not m:
        raise RuntimeError("新浪主连日K响应无法解析")
    arr = json.loads(m.group(1))
    return [datetime.strptime(x["d"][:10], "%Y-%m-%d").date() for x in arr if x.get("d")]


def _fetch_eastmoney():
    """备源：东财上证指数日 K（偶发限流，失败由上层兜底）"""
    y = date.today().year
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           "secid=1.000001&fields1=f1,f2,f3&fields2=f51&klt=101&fqt=1"
           f"&beg={y - 1}0101&end={y + 1}1231")
    r = http.get(url, headers=config.HEADERS_COMMON, timeout=6)
    ks = (r.json().get("data") or {}).get("klines") or []
    return [datetime.strptime(str(k)[:10], "%Y-%m-%d").date() for k in ks]


def _fetch_dynamic_dates():
    """依次尝试主/备动态源，返回交易日列表；全失败抛异常"""
    errs = []
    for fetcher in (_fetch_sina, _fetch_eastmoney):
        for _ in range(2):                       # 每个源最多重试1次（应对偶发断连）
            try:
                ds = fetcher()
                if len(ds) >= 50:
                    return ds
                errs.append("%s 返回条数异常(%d)" % (fetcher.__name__, len(ds)))
            except Exception as e:
                errs.append("%s: %s" % (fetcher.__name__, e))
                time.sleep(0.8)
    raise RuntimeError("; ".join(errs))


def _load_cache():
    """从本地缓存载入动态交易日集合，返回是否成功"""
    global _dates, _max_cached
    try:
        with open(config.TRADE_CAL_CACHE, encoding="utf-8") as fp:
            ds = []
            for line in fp:
                line = line.strip()
                if len(line) >= 10:
                    try:
                        ds.append(datetime.strptime(line[:10], "%Y-%m-%d").date())
                    except ValueError:
                        continue
        if ds:
            _dates = set(ds)
            _max_cached = max(ds)
            return True
    except FileNotFoundError:
        return False
    except Exception as e:
        LOG.warning("交易日历缓存读取失败: %s", e)
    return False


def _save_cache(ds):
    try:
        os.makedirs(os.path.dirname(config.TRADE_CAL_CACHE), exist_ok=True)
        tmp = config.TRADE_CAL_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            fp.write("\n".join(d.strftime("%Y-%m-%d") for d in sorted(ds)))
        os.replace(tmp, config.TRADE_CAL_CACHE)
    except Exception as e:
        LOG.warning("交易日历缓存写入失败: %s", e)


def refresh(force=False):
    """联网（东财日K）刷新动态交易日；成功返回 True，失败但有旧缓存时继续用旧缓存"""
    global _dates, _max_cached, _last_refresh_try
    with _lock:
        try:
            fetched = _fetch_dynamic_dates()
            cutoff = date.today() - timedelta(days=800)   # 只缓存近2年多，避免文件过大
            ds = {d for d in fetched if d >= cutoff}
            if len(ds) < 50:                              # 异常响应保护
                raise RuntimeError("动态交易日条数异常(%d)" % len(ds))
            if _dates:
                ds |= {d for d in _dates if d >= cutoff}
            _dates = ds
            _max_cached = max(ds)
            _last_refresh_try = time.time()
            _save_cache(ds)
            LOG.info("交易日历已更新：动态日K至 %s（共%d个交易日，静态休市%d天）",
                     _max_cached, len(ds), len(STATIC_HOLIDAYS))
            return True
        except Exception as e:
            _last_refresh_try = time.time()
            if _dates:
                LOG.warning("交易日历联网刷新失败，暂用本地缓存: %s", e)
            return False


def ensure():
    """确保日历已载入：先读缓存，缺失或过旧时联网刷新一次（惰性、线程安全）"""
    global _warned_fallback
    if _dates is not None:
        fresh = _max_cached and (date.today() - _max_cached).days <= 7
        if fresh:
            return True
        if time.time() - _last_refresh_try < 3600:    # 刷新失败后1小时内不反复重试
            return True
        refresh()
        return _dates is not None
    with _lock:
        if _dates is not None:
            return True
        ok = _load_cache()
        if not ok or not _max_cached or (date.today() - _max_cached).days > 7:
            refresh()
        if _dates:
            return True
        if not _warned_fallback:
            LOG.warning("动态交易日历不可用（无缓存且联网失败），改用静态休市表+周末规则；"
                        "恢复网络后重启即可自动校正")
            _warned_fallback = True
        return False


def is_trade_day(d=None):
    """d(date/datetime) 是否交易日。
    周末恒 False（调休补班周末也休市）；工作日先看静态休市表，再用动态日K校正，
    日K未覆盖的未来工作日默认 True。"""
    if isinstance(d, datetime):
        d = d.date()
    d = d or date.today()
    if d.weekday() >= 5:
        return False
    if d in STATIC_HOLIDAYS:
        return False
    ensure()
    if _dates:
        if d in _dates:
            return True
        if d <= _max_cached:
            return False                            # 已覆盖但不是交易日 = 临时休市
    return d.weekday() < 5                          # 未来工作日兜底


def next_trade_day(d, max_step=15):
    """d 之后最近的交易日（不含 d 本身）"""
    if isinstance(d, datetime):
        d = d.date()
    ensure()
    for i in range(1, max_step + 1):
        cand = d + timedelta(days=i)
        if cand.weekday() >= 5 or cand in STATIC_HOLIDAYS:
            continue
        if _dates and cand <= _max_cached and cand not in _dates:
            continue
        return cand
    return d + timedelta(days=1)


def prev_trade_day(d, max_step=15):
    """d 之前最近的交易日（不含 d 本身）"""
    if isinstance(d, datetime):
        d = d.date()
    ensure()
    lo = min(_dates) if _dates else None
    for i in range(1, max_step + 1):
        cand = d - timedelta(days=i)
        if cand.weekday() >= 5 or cand in STATIC_HOLIDAYS:
            continue
        # 仅当该日期落在动态日K覆盖区间内、却又不是交易日时，才判为临时休市；
        # 晚于最新K线的未来日期动态数据没有，交给周末/静态表判断，不能误跳过
        if lo and _max_cached and lo <= cand <= _max_cached and cand not in _dates:
            continue
        return cand
    return d - timedelta(days=1)


def has_night_session(d=None):
    """交易日 d 当晚是否开夜盘。规则：夜盘衔接下一交易日；
    仅跨普通周末（周五晚→下周一，间隔3天）或直接衔接次日（间隔1天）才有夜盘，
    中间隔着法定节假日（间隔>3天）的节前最后一晚不夜盘。"""
    if isinstance(d, datetime):
        d = d.date()
    d = d or date.today()
    if not is_trade_day(d):
        return False
    gap = (next_trade_day(d) - d).days
    return gap in (1, 3)


def status_line():
    """日历状态简述（启动日志用）"""
    ensure()
    static_note = f"静态休市至{max(STATIC_HOLIDAYS)}"
    if _dates:
        return f"交易日历: 动态日K至{_max_cached}（{len(_dates)}日），{static_note}"
    return f"交易日历: 动态不可用，{static_note}（工作日兜底）"
