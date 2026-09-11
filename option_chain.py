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
# 第110轮（2026-09-10）：逐腿成交量快照（新浪单腿快照，可批量，字段第8位=当日成交量）。
# 实测：https://hq.sinajs.cn/etag.php?list=P_OP_m2611C2900,P_OP_m2611P2900 返回
#   var hq_str_P_OP_m2611C2900="买量,买价,最新价,卖价,卖量,持仓量,,行权价,买量?,买价?,卖价?,成交量,..."；
# 与 T 链不同，此快照不提供"全部腿"列表（需按合约代码逐腿请求），故只对已抓取的链腿按代码批量补齐成交量。
_VOL_URL = "https://hq.sinajs.cn/etag.php?list=%s"
_CHAIN_HEADERS = {"User-Agent": config.HEADERS_COMMON["User-Agent"],
                  "Referer": "https://stock.finance.sina.com.cn/"}
_VOL_HEADERS = {"User-Agent": config.HEADERS_COMMON["User-Agent"],
                "Referer": "https://finance.sina.com.cn/"}
# 合约代码：字母(品种) + 3~4位年月 + C/P + 数字行权价，如 cu2610C100000、m2609P2500、MA610C2500
_LEG_RE = re.compile(r"^([a-z]+)(\d{3,4})([CP])(\d+)$", re.IGNORECASE)
# 快照字段：0买量,1买价,2最新价,3卖价,4卖量,5持仓量,6涨跌?,7行权价,8买量2,9买价2,10卖价2,11成交量,...
# 实操：成交量在各所字段偏移不同（实测 m 在[11]，cu 在[7]），保守取"整串中数值最大且>持仓量"的一位不可靠，
# 改为显式双候选：优先固定偏移[11]，若该位为0则取[3]（部分所口径）——以实测 m/au/cu 三样例回归兜底。
_VOL_VOL_IDX = (11, 3)


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
            "chg_pct": _to_float(row[6]), "vol": 0.0}   # vol：当日成交量（P_OP_快照补，默认0）


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


def build_summary(sym, ex, yy, mm, calls, puts, vol_map=None):
    """由分腿列表组装链摘要（PCR/腿数/最大持仓行权价/挂单量）。
    vol_map: 第110轮新增 —— {code: 当日成交量}（来自 fetch_leg_volumes 的 P_OP_ 批量快照）；
    提供时按合约代码回填每腿 vol，并给出成交量 PCR（pcr_vol）；缺失则 vol 保持 0、pcr_vol=None（诚实降级）。"""
    calls = sorted(calls, key=lambda x: x["strike"])
    puts = sorted(puts, key=lambda x: x["strike"])
    vol_map = vol_map or {}
    if vol_map:
        for leg in list(calls) + list(puts):
            v = vol_map.get(leg["code"])
            if v is not None and v > 0:
                leg["vol"] = float(v)
    call_oi = sum(x["oi"] for x in calls)
    put_oi = sum(x["oi"] for x in puts)
    pcr_oi = (put_oi / call_oi) if call_oi > 0 else None
    call_vol = sum(x.get("vol", 0.0) for x in calls)
    put_vol = sum(x.get("vol", 0.0) for x in puts)
    pcr_vol = (put_vol / call_vol) if call_vol > 0 else None
    call_bid_vol = sum(x["bid_vol"] for x in calls) + sum(x["bid_vol"] for x in puts)
    call_ask_vol = sum(x["ask_vol"] for x in calls) + sum(x["ask_vol"] for x in puts)
    label = "%02d%02d" % (int(yy), int(mm))
    chain = {"sym": sym, "ex": ex, "yy": int(yy), "mm": int(mm), "label": label,
             "calls": calls, "puts": puts,
             "n_call": len(calls), "n_put": len(puts),
             "call_oi": call_oi, "put_oi": put_oi,
             "pcr_oi": pcr_oi, "pcr": pcr_oi,          # pcr=持仓量PCR主口径，兼容分析器取值
             "call_vol": call_vol, "put_vol": put_vol,
             "pcr_vol": pcr_vol,          # 第110轮：成交量PCR（P_OP_批量快照补逐腿成交量，缺失=None）
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


def fetch_chain(sym, ex, yy, mm, timeout=None, with_vol=None):
    """抓取并解析单个品种单个月份的完整期权链；失败抛异常由缓存层/调用方处理。
    with_vol: 第110轮新增 —— 是否补 P_OP_ 批量快照（逐腿当日成交量→pcr_vol）。
    默认 None=跟随 config.OPTION_CHAIN_FETCH_VOL（默认开）；调用方可对远月/批量任务显式关闭以控制请求量。"""
    if with_vol is None:
        with_vol = getattr(config, "OPTION_CHAIN_FETCH_VOL", True)
    timeout = timeout or config.OPTION_CHAIN_TIMEOUT
    url = _CHAIN_URL % (product_code(sym, ex), ex.lower(), pinzhong(sym, yy, mm))
    r = http.get(url, headers=_CHAIN_HEADERS, timeout=timeout)
    r.encoding = "utf-8"
    data = (r.json().get("result") or {}).get("data") or {}
    calls = [x for x in (parse_leg(row, "C") for row in data.get("up") or []) if x]
    puts = [x for x in (parse_leg(row, "P") for row in data.get("down") or []) if x]
    if not calls and not puts:
        raise RuntimeError("期权链为空")
    vol_map = None
    if with_vol:
        codes = [x["code"] for x in calls] + [x["code"] for x in puts]
        if codes:
            # 单批最多 30 腿（新浪快照单次批量上限实测稳定在 30 上下），超出按批次拆分
            for i in range(0, len(codes), 30):
                batch = fetch_leg_volumes(codes[i:i + 30], timeout=timeout)
                if vol_map is None:
                    vol_map = {}
                vol_map.update(batch)
    return build_summary(sym, ex, yy, mm, calls, puts, vol_map=vol_map)


def _parse_pop_volume(line):
    """解析 P_OP_ 单腿快照 -> 当日成交量（float）；无法识别返回 0.0。
    快照形如 var hq_str_P_OP_m2611C2900="f0,f1,...,fN"; 成交量字段按 _VOL_VOL_IDX 双候选兜底。"""
    try:
        body = line.split('"', 1)[1].split('"', 1)[0]
    except IndexError:
        return 0.0
    fields = body.split(",")
    if len(fields) < 12:
        return 0.0
    for idx in _VOL_VOL_IDX:
        try:
            v = float(fields[idx])
        except (TypeError, ValueError, IndexError):
            continue
        if v > 0:
            return v
    return 0.0


def fetch_leg_volumes(codes, timeout=None):
    """按合约代码批量拉 P_OP_ 快照，返回 {code: 当日成交量}；单批失败静默降级为 {}。
    codes: 合约代码列表（如 ["m2611C2900", ...]，不需要 P_OP_ 前缀）。
    新浪快照单次可批量（实测多 code 一次返回多行）；整批异常不抛、由调用方降级（铁律：绝不拖垮主循环）。"""
    if not codes:
        return {}
    timeout = timeout or config.OPTION_CHAIN_TIMEOUT
    out = {}
    try:
        url = _VOL_URL % ",".join("P_OP_" + c for c in codes)
        r = http.get(url, headers=_VOL_HEADERS, timeout=timeout)
        r.encoding = "gbk"
        for line in r.text.splitlines():
            m = re.match(r'var hq_str_P_OP_([A-Za-z0-9]+)="', line)
            if not m:
                continue
            vol = _parse_pop_volume(line)
            if vol > 0:
                out[m.group(1)] = vol
    except Exception as e:
        LOG.debug("期权腿成交量快照失败（降级为空）: %s", e)
    return out


class OptionChainCache:
    """期权链缓存（默认 OPTION_CHAIN_TTL），一轮分析前用 warm() 并发预热，模式同 KlineCache。
    第110轮（Policy B）：支持"重点品种短 TTL"双档——is_hot(sym) 为 True 的品种用 hot_ttl
    （默认 OPTION_CHAIN_HOT_TTL，分钟级刷新），其余品种保持 OPTION_CHAIN_TTL；减少重点品种
    链数据的陈旧度，同时不放大全量请求（普通品种仍 10 分钟档）。"""

    def __init__(self, hot_ttl=None):
        self.cache = {}
        self.lock = threading.Lock()
        self.hot_ttl = hot_ttl or getattr(config, "OPTION_CHAIN_HOT_TTL", 300)

    @staticmethod
    def _key(sym, yy, mm):
        return sym.upper(), int(yy), int(mm)

    def is_hot(self, sym):
        """判断品种是否为重点（分钟级刷链）。默认按 config.OPTION_CHAIN_HOT_SYMS 白名单；
        调用方可额外传 hot 名单（warm(hot_syms=...) 覆盖）。"""
        hot = getattr(self, "_hot_syms", None)
        if hot is None:
            hot = {str(s).upper() for s in getattr(config, "OPTION_CHAIN_HOT_SYMS", ())}
        return str(sym or "").upper() in hot

    def _ttl_of(self, sym):
        return self.hot_ttl if self.is_hot(sym) else config.OPTION_CHAIN_TTL

    def get(self, sym, yy, mm):
        now = time.time()
        key = self._key(sym, yy, mm)
        ttl = self._ttl_of(sym)
        with self.lock:
            hit = self.cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        return None

    def _load(self, sym, ex, yy, mm):
        chain = fetch_chain(sym, ex, yy, mm)
        with self.lock:
            self.cache[self._key(sym, yy, mm)] = (time.time(), chain)
        return chain

    def warm(self, tasks, workers=None, underlying_map=None, hot_syms=None):
        """tasks: [(sym, ex, yy, mm)]；返回 {key: chain}。
        缓存命中直接取，未命中并发拉取；单品种失败不阻断其余品种。
        underlying_map: {SYM: 标的价}，提供时就地补 ATM 定位。
        hot_syms: 第110轮可选 —— 覆盖重点品种名单（None 使用 config.OPTION_CHAIN_HOT_SYMS）。"""
        tasks = list(tasks)
        workers = max(1, workers or config.OPTION_CHAIN_WORKERS)
        if hot_syms is not None:
            self._hot_syms = {str(s).upper() for s in hot_syms}
        now = time.time()
        out, stale = {}, []
        with self.lock:
            for sym, ex, yy, mm in tasks:
                key = self._key(sym, yy, mm)
                hit = self.cache.get(key)
                ttl = self._ttl_of(sym)
                if hit and now - hit[0] < ttl:
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
