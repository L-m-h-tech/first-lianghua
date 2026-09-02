# -*- coding: utf-8 -*-
"""【需求⑥】外部网站公开接口数据（10秒刷新线程）：
1. 交易可查 /api/v2/aireport  机构研报多空统计（看多/震荡/看空家数）
   -> analyzer"机构动向"因子(±2) + option_strategies"机构观点配合"检查
2. OpenVlab /api/product-exps  各期权品种真实挂牌月份与真实到期日
   -> contracts 期权月份校验 + 精确剩余天数（需求③的到期检查）
（页面级数据直读见 browser_reader.py）

1. 交易可查 (jiaoyikecha.com) —— 机构多空看法
   - GET /api/v2/aireport      各品种机构研报多空统计：viewBullish看多家数 /
                               viewVolatile震荡家数 / viewBearish看空家数（按品种代码RB/MA...）
   - GET /api/v2/aireportHot   热门品种多空统计（品种名如"沪金"，用别名表映射）
   说明：席位级"机构持仓明细"需登录/VIP，公开接口以机构观点数据为准。

2. OpenVlab (openvlab.cn) —— 期权数据
   - GET /api/product-exps     各期权品种真实挂牌月份与真实到期日（expDate），
                               用于：期权月份校验（只在真实挂牌月份中选）+ 精确剩余天数
   （其市场页隐波排行走内部POST接口，程序以历史波动率估计IV并明确标注，实盘以盘面为准）
"""
import threading
import time
from datetime import datetime

import config
from http_client import http
from utils import LOG

_UA = config.HEADERS_COMMON["User-Agent"]
JYKC_HEADERS = {"User-Agent": _UA, "Referer": "https://www.jiaoyikecha.com/",
                "X-Requested-With": "XMLHttpRequest"}
OVL_HEADERS = {"User-Agent": _UA, "Referer": "https://www.openvlab.cn/market"}

# aireportHot 等接口里的品种简称 -> 标准品种名
NAME_ALIAS = {
    "沪金": "黄金", "沪银": "白银", "沪铜": "铜", "沪铝": "铝", "沪锌": "锌",
    "沪铅": "铅", "沪镍": "镍", "沪锡": "锡", "螺纹": "螺纹钢", "热卷": "热卷",
    "铁矿": "铁矿石", "郑醇": "甲醇", "燃油": "燃料油", "低硫燃油": "低硫燃料油",
    "塑料": "塑料", "聚乙烯": "塑料", "PVC": "PVC", "聚氯乙烯": "PVC",
    "PP": "聚丙烯", "菜油": "菜籽油", "郑棉": "棉花", "郑糖": "白糖",
}


def _sym_to_variety(sym):
    """品种代码(RB/MA/SI...) -> 标准品种名"""
    sym = (sym or "").strip().upper()
    if not sym:
        return None
    for vname, meta in config.VARIETIES.items():
        if meta["sym"] == sym:
            return vname
    return None


def fetch_inst_views(timeout=8):
    """交易可查机构观点 -> {品种名: {bullish, volatile, bearish, total}}"""
    out = {}
    for url in ("https://www.jiaoyikecha.com/api/v2/aireport",
                "https://www.jiaoyikecha.com/api/v2/aireportHot"):
        try:
            r = http.get(url, headers=JYKC_HEADERS, timeout=timeout)
            r.encoding = "utf-8"
            data = r.json().get("data") or []
        except Exception as e:
            LOG.debug("交易可查 %s 获取失败: %s", url, e)
            continue
        for it in data:
            key = _sym_to_variety(it.get("symbol"))
            if key is None:
                key = NAME_ALIAS.get((it.get("varietyName") or "").strip())
            if key is None:
                continue
            b = int(it.get("viewBullish") or 0)
            vo = int(it.get("viewVolatile") or 0)
            be = int(it.get("viewBearish") or 0)
            old = out.get(key)
            # 同一品种两个接口都有时取观点总数更多的
            if old and (old["bullish"] + old["volatile"] + old["bearish"]) >= (b + vo + be):
                continue
            out[key] = {"bullish": b, "volatile": vo, "bearish": be,
                        "total": b + vo + be}
    return out


def fetch_option_calendar(timeout=10):
    """OpenVlab期权日历 -> {sym: {yymm(int): {"exp_date": date}}}（仅四大交易所范围）"""
    out = {}
    try:
        r = http.get("https://www.openvlab.cn/api/product-exps",
                         headers=OVL_HEADERS, timeout=timeout)
        r.encoding = "utf-8"
        items = r.json().get("result") or []
    except Exception as e:
        LOG.warning("OpenVlab期权日历获取失败: %s", e)
        return out
    for it in items:
        ex = it.get("exchange")
        if ex not in config.ANALYZE_EXCHANGES:
            continue
        parts = (it.get("symbol_und") or "").split("_")   # FUT_CZCE_MA
        if len(parts) < 3:
            continue
        sym = parts[2].upper()
        months = {}
        for e in it.get("exps") or []:
            try:
                exp = int(e.get("exp"))
                ymd = datetime.strptime(str(e.get("expDate")), "%Y%m%d").date()
            except (TypeError, ValueError):
                continue
            months[exp] = {"exp_date": ymd}
        if months:
            out[sym] = months
    return out


class WebDataTracker:
    """10秒线程：机构观点每10秒刷新；期权日历每30分钟刷新"""

    def __init__(self):
        self.lock = threading.Lock()
        self.views = {}          # 品种名 -> 机构多空统计
        self.calendar = {}       # sym -> {yymm: {"exp_date": date}}
        self.views_updated = None
        self.cal_updated = None
        self._last_cal = 0.0
        self._fail = 0
        self.stop = threading.Event()

    def views_snapshot(self):
        with self.lock:
            return dict(self.views)

    def calendar_snapshot(self):
        with self.lock:
            return {k: dict(v) for k, v in self.calendar.items()}

    def status_line(self):
        with self.lock:
            n = len(self.views)
            t = self.views_updated.strftime("%H:%M:%S") if self.views_updated else "-"
            c = len(self.calendar)
        return f"[外部数据] 机构观点{n}个品种(更新{t}) | 期权日历{c}个品种"

    def _fetch_once(self):
        try:
            v = fetch_inst_views()
            if v:
                with self.lock:
                    self.views = v
                    self.views_updated = datetime.now()
                    self._fail = 0
        except Exception as e:
            self._fail += 1
            if self._fail <= 3 or self._fail % 30 == 0:
                LOG.warning("交易可查机构观点刷新失败(第%d次): %s", self._fail, e)
        now = time.time()
        if now - self._last_cal > config.CONTRACT_TTL:
            cal = fetch_option_calendar()
            if cal:
                with self.lock:
                    self.calendar = cal
                    self.cal_updated = datetime.now()
                self._last_cal = now
                LOG.info("OpenVlab期权日历已更新（%d个品种，含真实到期日）", len(cal))
            else:
                self._last_cal = now

    def loop(self, interval=10):
        LOG.info("外部数据线程启动（交易可查/OpenVlab 每%d秒）", interval)
        while not self.stop.is_set():
            try:
                self._fetch_once()
            except Exception as e:
                LOG.debug("外部数据刷新异常: %s", e)
            self.stop.wait(interval)
