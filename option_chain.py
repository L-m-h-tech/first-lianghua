# -*- coding: utf-8 -*-
"""第11轮 WP-A：新浪商品期权完整T型报价链 + PCR（认沽/认购比），零新增运行时依赖。

接口（2026-09-01 对五大交易所57个期权品种全部实测通过）：
  GET http://stock.finance.sina.com.cn/futures/api/openapi.php/
      OptionService.getOptionData?type=futures&product={p}&exchange={ex}&pinzhong={pin}
  - product：SHFE/INE/DCE/GFEX = 品种字母小写 + "_o"（cu_o/sc_o/m_o/si_o）；
             CZCE = 品种字母小写、无后缀（ma/sa/ta）
  - exchange：交易所代码小写（shfe/ine/dce/czce/gfex）
  - pinzhong：品种字母小写 + 4位年月（cu2610、ma2610），月份取自 OpenVlab 期权日历
  - result.data.up=看涨腿列表、down=看跌腿列表；每腿：
      [买量, 买价, 最新价, 卖价, 卖量, 持仓量, 涨跌%, (行权价,部分交易所), 合约代码]
    SHFE/INE/GFEX 9 元素（含独立行权价位），DCE/CZCE 8 元素（行权价仅在合约代码尾部），
    本模块统一以合约代码正则解析行权价，兼容两种长度。

输出口径：
  - 持仓量 PCR = Σ看跌持仓 / Σ看涨持仓（主口径，T链直接给出，机构最常用）；
  - 成交量 PCR 需要逐腿成交量（T链不含该字段），后续由交易所期权日行情补齐，本轮不做、不猜字段；
  - 另给最大持仓行权价（支撑/压力参考）、ATM 定位、腿数与挂单量、情绪参考区间。
任何单品种抓取/解析失败都返回 None，由调用方降级，绝不拖垮主循环。

已知数据源缺口（2026-09-01 实测）：新浪T链未提供 INE 低硫燃料油(LU)期权（各月份/参数组合均空），
该品种自动降级为"无链模式"（期权分析照常，仅缺PCR/全链），后续轮次用交易所期权日行情补备用源。
"""
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
from http_client import http
from utils import LOG

_CHAIN_URL = ("http://stock.finance.sina.com.cn/futures/api/openapi.php/"
              "OptionService.getOptionData?type=futures"
              "&product=%s&exchange=%s&pinzhong=%s")
_CHAIN_HEADERS = {"User-Agent": config.HEADERS_COMMON["User-Agent"],
                  "Referer": "https://stock.finance.sina.com.cn/"}
# 合约代码：字母(品种) + 3~4位年月 + C/P + 数字行权价，如 cu2610C100000、m2609P2500、MA610C2500
_LEG_RE = re.compile(r"^([a-z]+)(\d{3,4})([CP])(\d+)$", re.IGNORECASE)


def product_code(sym, ex):
    """新浪T链 product 参数：郑商所无后缀，其余交易所加 _o。"""
    s = (sym or "").lower()
    return s if ex == "CZCE" else s + "_o"


def pinzhong(sym, yy, mm):
    """新浪T链 pinzhong 参数：品种小写 + 4位年月。"""
    return "%s%02d%02d" % (sym.lower(), int(yy), int(mm))


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_leg(row, cp):
    """解析单腿 -> 标准dict；无法识别返回 None。兼容 8/9 元素两种返回长度。"""
    if not row:
        return None
    code = str(row[-1]).strip()
    m = _LEG_RE.match(code)
    if not m:
        return None
    strike = _to_float(m.group(4))                 # 行权价一律以代码为准
    if len(row) >= 9:                              # 带独立行权价位时与代码交叉校验
        ks = _to_float(row[7])
        if ks > 0:
            strike = ks
    return {"code": code, "cp": cp, "strike": strike,
            "bid_vol": _to_float(row[0]), "bid": _to_float(row[1]),
            "last": _to_float(row[2]), "ask": _to_float(row[3]),
            "ask_vol": _to_float(row[4]), "oi": _to_float(row[5]),
            "chg_pct": _to_float(row[6])}


def pcr_sentiment(pcr):
    """持仓量PCR的情绪参考文本（只做呈现，不单独构成交易结论）。"""
    if pcr is None:
        return ""
    if pcr >= config.PCR_EXTREME_HIGH:
        return "看跌持仓极占优(情绪极值,反向指标需结合趋势)"
    if pcr >= config.PCR_HIGH:
        return "看跌/对冲持仓占优,情绪偏谨慎"
    if pcr <= config.PCR_EXTREME_LOW:
        return "看涨持仓极占优(情绪偏热,警惕一致预期)"
    if pcr <= config.PCR_LOW:
        return "看涨持仓占优,情绪偏乐观"
    return "多空持仓相对均衡"


def _max_oi_strike(legs):
    if not legs:
        return None
    top = max(legs, key=lambda x: x["oi"])
    return top["strike"] if top["oi"] > 0 else None


def build_summary(sym, ex, yy, mm, calls, puts):
    """由分腿列表组装链摘要（PCR/腿数/最大持仓行权价/挂单量）。"""
    calls = sorted(calls, key=lambda x: x["strike"])
    puts = sorted(puts, key=lambda x: x["strike"])
    call_oi = sum(x["oi"] for x in calls)
    put_oi = sum(x["oi"] for x in puts)
    pcr_oi = (put_oi / call_oi) if call_oi > 0 else None
    call_bid_vol = sum(x["bid_vol"] for x in calls) + sum(x["bid_vol"] for x in puts)
    call_ask_vol = sum(x["ask_vol"] for x in calls) + sum(x["ask_vol"] for x in puts)
    label = "%02d%02d" % (int(yy), int(mm))
    chain = {"sym": sym, "ex": ex, "yy": int(yy), "mm": int(mm), "label": label,
             "calls": calls, "puts": puts,
             "n_call": len(calls), "n_put": len(puts),
             "call_oi": call_oi, "put_oi": put_oi,
             "pcr_oi": pcr_oi, "pcr": pcr_oi,          # pcr=持仓量PCR主口径，兼容分析器取值
             "max_call_oi_strike": _max_oi_strike(calls),
             "max_put_oi_strike": _max_oi_strike(puts),
             "bid_vol": call_bid_vol, "ask_vol": call_ask_vol,
             "atm_strike": None, "atm_distance_pct": None,
             "pcr_pct": None, "updated": time.strftime("%H:%M:%S")}
    chain["sentiment"] = pcr_sentiment(pcr_oi)
    return chain


def locate_atm(chain, underlying):
    """按标的最新价定位平值行权价与偏离度；返回同一 chain（就地补充）。"""
    if not chain or underlying <= 0:
        return chain
    legs = (chain.get("calls") or []) + (chain.get("puts") or [])
    strikes = sorted({x["strike"] for x in legs if x["strike"] > 0})
    if strikes:
        atm = min(strikes, key=lambda k: abs(k - underlying))
        chain["atm_strike"] = atm
        chain["atm_distance_pct"] = atm / underlying - 1.0
    return chain


def fetch_chain(sym, ex, yy, mm, timeout=None):
    """抓取并解析单个品种单个月份的完整期权链；失败抛异常由缓存层/调用方处理。"""
    timeout = timeout or config.OPTION_CHAIN_TIMEOUT
    url = _CHAIN_URL % (product_code(sym, ex), ex.lower(), pinzhong(sym, yy, mm))
    r = http.get(url, headers=_CHAIN_HEADERS, timeout=timeout)
    r.encoding = "utf-8"
    data = (r.json().get("result") or {}).get("data") or {}
    calls = [x for x in (parse_leg(row, "C") for row in data.get("up") or []) if x]
    puts = [x for x in (parse_leg(row, "P") for row in data.get("down") or []) if x]
    if not calls and not puts:
        raise RuntimeError("期权链为空")
    return build_summary(sym, ex, yy, mm, calls, puts)


class OptionChainCache:
    """期权链缓存（默认 OPTION_CHAIN_TTL），一轮分析前用 warm() 并发预热，模式同 KlineCache。"""

    def __init__(self):
        self.cache = {}
        self.lock = threading.Lock()

    @staticmethod
    def _key(sym, yy, mm):
        return sym.upper(), int(yy), int(mm)

    def get(self, sym, yy, mm):
        now = time.time()
        with self.lock:
            hit = self.cache.get(self._key(sym, yy, mm))
            if hit and now - hit[0] < config.OPTION_CHAIN_TTL:
                return hit[1]
        return None

    def _load(self, sym, ex, yy, mm):
        chain = fetch_chain(sym, ex, yy, mm)
        with self.lock:
            self.cache[self._key(sym, yy, mm)] = (time.time(), chain)
        return chain

    def warm(self, tasks, workers=None, underlying_map=None):
        """tasks: [(sym, ex, yy, mm)]；返回 {key: chain}。
        缓存命中直接取，未命中并发拉取；单品种失败不阻断其余品种。
        underlying_map: {SYM: 标的价}，提供时就地补 ATM 定位。"""
        tasks = list(tasks)
        workers = max(1, workers or config.OPTION_CHAIN_WORKERS)
        now = time.time()
        out, stale = {}, []
        with self.lock:
            for sym, ex, yy, mm in tasks:
                key = self._key(sym, yy, mm)
                hit = self.cache.get(key)
                if hit and now - hit[0] < config.OPTION_CHAIN_TTL:
                    out[key] = hit[1]
                else:
                    stale.append((sym, ex, yy, mm))
        if stale:
            with ThreadPoolExecutor(max_workers=min(workers, len(stale))) as pool:
                futs = {pool.submit(self._load, sym, ex, yy, mm): (sym, yy, mm)
                        for sym, ex, yy, mm in stale}
                for fut in as_completed(futs):
                    sym, yy, mm = futs[fut]
                    try:
                        out[self._key(sym, yy, mm)] = fut.result()
                    except Exception as e:
                        LOG.debug("期权链并发预热失败 %s %02d%02d: %s", sym, int(yy), int(mm), e)
        underlying_map = underlying_map or {}
        for (sym, yy, mm), chain in out.items():
            locate_atm(chain, underlying_map.get(sym.upper(), 0.0))
        return out

    def status_line(self):
        with self.lock:
            return "期权链缓存%d个月份" % len(self.cache)
