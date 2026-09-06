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


def fetch_quotes(codes):
    """批量拉取品种最新行情（自动分批，每批40个），返回 {code: {...}}，失败品种不返回。

    主源=新浪 hq.sinajs 主连快照；新浪整批失败或个别品种缺失时，用东财 push2 主连快照
    （secid=市场号.品种小写m，如113.rbm，2026-09-01实测稳定、字段f111=持仓量）只补缺失项，
    新浪正常时不产生任何额外请求（主备降级，保证不比单源差）。"""
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
        # G11：东财兜底源若处于熔断冷却期则直接跳过（它本来也连续失败，避免向坏源空发请求）；
        # 健康时该 allow() 恒为 True，行为与旧版逐字节一致。
        em_health = REGISTRY.source("quote_em")
        if not em_health.allow():
            em_health.note_skipped()
            LOG.info("东财行情兜底源熔断冷却中（剩余%.0fs），本轮跳过兜底",
                     em_health.snapshot()["cooldown_remaining"])
            return quotes
        try:
            em_quotes = _fetch_quotes_em(missing)
            if em_quotes:
                quotes.update(em_quotes)
                LOG.info("新浪行情缺失%d个品种，东财主连快照兜底补回%d个",
                         len(missing), len(em_quotes))
            REGISTRY.record("quote_em", True)
        except Exception as e:
            LOG.warning("东财行情兜底失败（不影响主流程）: %s", e)
            REGISTRY.record("quote_em", False)
    return quotes


# 主连code(RB0) -> (sym,ex) 缓存（东财兜底用）
_CODE_META_CACHE = None


def _fetch_quotes_em(codes):
    """东财 push2 主连快照兜底，返回结构与新浪 _parse_quote 完全一致的 {code: quote}。

    实测字段（fltt=2）：f2最新/f3涨跌幅%/f5成交量(手)/f6成交额/f12代码/f13市场号/f14名称/
    f15最高/f16最低/f17开盘/f18昨结/f111持仓量；东财主连代码=品种小写+m（rbm/mm/mam…）。
    任何异常软降级返回已拿到的部分，绝不抛出影响主监控。"""
    global _CODE_META_CACHE
    if _CODE_META_CACHE is None:
        _CODE_META_CACHE = {meta["code"]: (meta["sym"], meta["ex"])
                            for meta in config.VARIETIES.values()}
    sec2code, secids = {}, []
    for code in codes:
        info = _CODE_META_CACHE.get(code)
        if not info:
            continue
        sym, ex = info
        mkt = config.MINUTE_MARKET.get(ex)
        if not mkt:
            continue
        sec = f"{mkt}.{sym.lower()}m"
        sec2code[sec] = code
        secids.append(sec)
    if not secids:
        return {}
    out = {}
    today = time.strftime("%Y-%m-%d")
    for i in range(0, len(secids), 40):
        chunk = secids[i:i + 40]
        # 故意不带 fltt/invt：实测裸请求 f2 为正常价格、f111 为真实持仓量；带 fltt=2 时
        # 限流边缘曾返回 f111=1 的残缺数据。任何残缺/异常条目直接丢弃（宁可不兜底也不污染）。
        url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?secids="
               + ",".join(chunk)
               + "&fields=f2,f3,f5,f6,f12,f13,f14,f15,f16,f17,f18,f111")
        try:
            r = http.get(url, headers={"User-Agent": config.HEADERS_COMMON["User-Agent"],
                                       "Referer": "https://quote.eastmoney.com/"},
                         timeout=config.TIMEOUT)
            diff = ((r.json() or {}).get("data") or {}).get("diff") or []
        except Exception as e:
            LOG.warning("东财快照批次请求失败: %s", e)
            continue
        for row in diff:
            sec = f"{row.get('f13', '')}.{str(row.get('f12', '')).lower()}"
            code = sec2code.get(sec)
            if not code:
                continue
            latest = _f(row.get("f2"))
            prev = _f(row.get("f18"))
            oi = _f(row.get("f111"))
            # 有效性校验：主连快照必须价格/昨结为正、持仓量达到合理量级（限流边缘残缺响应
            # 曾给出 f111=1、价格错位的脏数据，此处一并拦截）
            if latest <= 0 or prev <= 0 or oi < 100:
                LOG.debug("东财快照条目残缺已丢弃 %s: latest=%s prev=%s oi=%s", code, latest, prev, oi)
                continue
            pct = _f(row.get("f3")) / 100.0
            out[code] = {"name": str(row.get("f14") or code), "latest": latest,
                         "open": _f(row.get("f17")), "high": _f(row.get("f15")),
                         "low": _f(row.get("f16")), "prev_settle": prev,
                         "chg_pct": (latest / prev - 1.0) if prev > 0 else pct,
                         "open_interest": oi, "volume": _f(row.get("f5")),
                         "date": today}
    return out


def _parse_quote(code, text, quotes):
    m = re.search(r'hq_str_nf_%s="([^"]*)"' % code, text)
    if not m:
        return
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


def fetch_daily_kline(symbol, retry=2):
    """新浪期货日线K线，返回 [{d,o,h,l,c,v,p,s}, ...]（可能失败，调用方需兜底）"""
    url = (f"https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
           f"InnerFuturesNewService.getDailyKLine?symbol={symbol}")
    last_err = None
    for _ in range(retry + 1):
        try:
            r = http.get(url, headers=config.HEADERS_COMMON,
                             timeout=config.TIMEOUT)
            r.encoding = "utf-8"
            m = re.search(r"\((\[.*\])\)", r.text, re.S)
            if m:
                return json.loads(m.group(1))
            last_err = "响应中未找到K线数组"
        except Exception as e:
            last_err = str(e)
        time.sleep(0.5)
    raise RuntimeError(f"日线获取失败({symbol}): {last_err}")


def fetch_intraday_kline(symbol, period=30, retry=1):
    """新浪期货分钟K线。实测 getFewMinLine 支持 type=1/5/15/30/60，返回结构同日K。

    2026-09-01 晚补测（第14轮曾误判"新浪无1分钟"）：type=1 一分钟K同样固定返回1023根、
    64/64品种全覆盖、零断连（约覆盖最近2.5个交易日），主连与具体合约均可取；故1m主源
    由东财push2his（本机持续限流）切换为新浪主连。"""
    period = int(period)
    if period not in (1, 5, 15, 30, 60):
        raise ValueError(f"不支持的分钟周期: {period}")
    url = (f"https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
           f"InnerFuturesNewService.getFewMinLine?symbol={symbol}&type={period}")
    last_err = None
    for _ in range(retry + 1):
        try:
            r = http.get(url, headers=config.HEADERS_COMMON, timeout=config.TIMEOUT)
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


class KlineCache:
    """日线指标缓存（默认30分钟刷新），失败时回退到板块默认波动率"""

    def __init__(self):
        self.cache = {}
        self.intraday_cache = {}
        self.lock = threading.Lock()

    def get(self, code, cat):
        now = time.time()
        with self.lock:
            hit = self.cache.get(code)
            if hit and now - hit[0] < config.KLINE_TTL:
                return hit[1], True
        try:
            bars = fetch_daily_kline(code)
            ind = compute_indicators(bars)
            with self.lock:
                self.cache[code] = (now, ind)
            return ind, True
        except Exception as e:
            LOG.warning("%s 日线指标获取失败，使用默认波动率: %s", code, e)
            fallback = {"close": 0.0, "prev_close": 0.0, "day_chg": 0.0,
                        "hv20": config.DEFAULT_HV.get(cat, 0.25),
                        "hv60": config.DEFAULT_HV.get(cat, 0.25),
                        "ma5": 0.0, "ma10": 0.0, "ma20": 0.0,
                        "atr": 0.0, "ret5": 0.0, "ret20": 0.0,
                        "ret63": None, "ret126": None, "ret252": None,
                        "tsmom63": None, "tsmom126": None, "tsmom252": None,
                        "tsmom_blend": None, "tsmom_n_valid": 0,
                        "tech": {}, "hv_percentile": None, "vol_cone": {},
                        "last_date": ""}
            return fallback, False

    def refresh_if_stale(self, code, cat, margin=0.9):
        """缓存即将过期时提前在后台刷新，避免主分析周期被拉长"""
        now = time.time()
        with self.lock:
            hit = self.cache.get(code)
            if hit and now - hit[0] < config.KLINE_TTL * margin:
                return
        try:
            bars = fetch_daily_kline(code)
            ind = compute_indicators(bars)
            with self.lock:
                self.cache[code] = (time.time(), ind)
        except Exception as e:
            LOG.debug("%s 日线后台预刷新失败: %s", code, e)

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
        """一轮分析前并发预热分钟K线，返回 {code: (ind, ok)}；失败品种不阻断主流程。"""
        pairs = list(code_cat_pairs)
        workers = max(1, workers or config.INTRADAY_WORKERS)
        now = time.time()
        stale = []
        out = {}
        with self.lock:
            for code, cat in pairs:
                hit = self.intraday_cache.get(code)
                if hit and now - hit[0] < config.INTRADAY_KLINE_TTL:
                    out[code] = (hit[1], True)
                else:
                    stale.append(code)
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
