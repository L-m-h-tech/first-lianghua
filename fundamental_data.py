# -*- coding: utf-8 -*-
"""第13轮 WP-C：基本面数据直连（零新增第三方依赖，仅用 requests + 标准库）。

数据源（2026-09-01 实测）：
  * 库存/仓单时序：东方财富数据中心 RPT_FUTU_STOCKDATA（全市场注册仓单/库存，含约3个月历史，
    一次请求拿一个品种的完整时序，可算滚动分位与周环比）；品种代码表 RPT_FUTU_POSITIONCODE。
  * 龙虎榜：东方财富 RPT_FUTU_DAILYPOSITION，TYPE=2 返回某主力合约"本日合计/上日合计"前20席多空。
  * 基差：生意社 sf/day 当日表（标准库 HTML 解析）；该站有 JS-cookie 反爬且不稳定，
    识别到反爬挑战页即判定本次不可用、返回 None，由 carry 等其他子因子补位，绝不编造。
设计：日频缓存（同一自然日只拉一次），线程安全；库存/基差由后台线程批量预热，龙虎榜按主力合约
按需取并缓存，全部失败都软降级（返回 None），不影响主监控管线。
"""
import datetime
import re
import threading
from html.parser import HTMLParser

import config
from http_client import http

EM_API = config.FUND_EM_API
EM_HEADERS = {"Referer": "https://data.eastmoney.com/", "Accept": "application/json, text/plain, */*"}


def _em_get(params, timeout=12):
    """东财 datacenter 统一 GET，返回 result.data 列表；任何异常/空结果返回 []。"""
    try:
        r = http.get(EM_API, params=dict(params, source="WEB", client="WEB"),
                     headers=EM_HEADERS, timeout=timeout)
        if r.status_code != 200:
            return []
        j = r.json()
        res = (j or {}).get("result") or {}
        return res.get("data") or []
    except Exception:
        return []


class _TableGrabber(HTMLParser):
    """提取 HTML 中所有表格，返回 [[[单元格文本,...],行...],表...]（标准库，零依赖）。"""

    def __init__(self):
        super().__init__()
        self.tables, self.cur_table, self.cur_row, self.cell = [], [], [], []
        self.in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.cur_row = []
        elif tag in ("td", "th"):
            self.in_cell, self.cell = True, []

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self.in_cell = False
            self.cur_row.append(re.sub(r"\s+", "", "".join(self.cell)))
        elif tag == "tr" and self.cur_row:
            self.cur_table.append(self.cur_row)
        elif tag == "table":
            if self.cur_table:
                self.tables.append(self.cur_table)
            self.cur_table = []

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)


class FundamentalFetcher:
    def __init__(self):
        self.lock = threading.RLock()
        self._inv_map = None                 # {SYM大写: 东财原始code(保留大小写)}
        self._inv_cache = {}                 # sym大写 -> (自然日str, series)
        self._rank_cache = {}                # 合约代码 -> (自然日str, rank_dict)
        self._basis_cache = {}               # 自然日str -> {SYM大写: basis_rate} 或 None(反爬)
        self._map_day = ""

    # ---------------- 品种代码映射 ----------------
    def inventory_map(self, force=False):
        """东财品种代码表：返回 {项目sym大写: 东财TRADE_CODE原始大小写}。"""
        today = datetime.date.today().isoformat()
        with self.lock:
            if self._inv_map is not None and not force and self._map_day == today:
                return dict(self._inv_map)
        rows = _em_get({"reportName": "RPT_FUTU_POSITIONCODE", "columns": "TRADE_CODE,TRADE_TYPE",
                        "filter": '(IS_MAINCODE="1")', "pageNumber": "1",
                        "pageSize": "500", "sortTypes": "1", "sortColumns": "TRADE_CODE"})
        mp = {}
        for x in rows:
            code = (x.get("TRADE_CODE") or "").strip()
            if code:
                mp[code.upper()] = code       # 广期所东财为小写 si/lc/ps，统一用大写键、保留原值
        with self.lock:
            if mp:
                self._inv_map, self._map_day = mp, today
            return dict(self._inv_map or mp)

    def em_code(self, sym):
        mp = self.inventory_map()
        return mp.get((sym or "").upper())

    # ---------------- 库存/仓单时序 ----------------
    def inventory_series(self, sym):
        """返回升序 [{"date","stock","chg"}]；当日已缓存直接命中，无数据返回 []。"""
        key = (sym or "").upper()
        today = datetime.date.today().isoformat()
        with self.lock:
            hit = self._inv_cache.get(key)
            if hit and hit[0] == today:
                return hit[1]
        em_code = self.em_code(key)
        if not em_code:
            return []
        rows = _em_get({"reportName": "RPT_FUTU_STOCKDATA",
                        "columns": "TRADE_DATE,ON_WARRANT_NUM,ADDCHANGE",
                        "filter": f'(SECURITY_CODE="{em_code}")',
                        "pageNumber": "1", "pageSize": str(config.FUND_EM_PAGE_SIZE),
                        "sortTypes": "1", "sortColumns": "TRADE_DATE"})  # 升序
        series = []
        for x in rows:
            stock = x.get("ON_WARRANT_NUM")
            if stock is None:
                continue
            series.append({"date": (x.get("TRADE_DATE") or "")[:10],
                           "stock": float(stock),
                           "chg": (None if x.get("ADDCHANGE") is None else float(x.get("ADDCHANGE")))})
        with self.lock:
            self._inv_cache[key] = (today, series)
        return series

    # ---------------- 龙虎榜（前20席多空合计） ----------------
    def rank_totals(self, em_code, yy, mm):
        """某主力合约最新交易日的前20席多空合计。em_code 为东财品种原始大小写，yy/mm为两位年月。

        返回 {"date","long","short","prev_long","prev_short"} 或 None。
        """
        if not em_code:
            return None
        sec = f"{em_code}{int(yy):02d}{int(mm):02d}"
        today = datetime.date.today().isoformat()
        with self.lock:
            hit = self._rank_cache.get(sec)
            if hit and hit[0] == today:
                return hit[1]
        rows = _em_get({"reportName": "RPT_FUTU_DAILYPOSITION", "columns": "ALL",
                        "filter": f'(SECURITY_CODE="{sec}")(TYPE="2")',
                        "sortTypes": "-1", "sortColumns": "TRADE_DATE", "pageSize": "3"})
        out = None
        if rows:
            # 按日期分组，取最新交易日的三行（本日合计/上日合计/总量增减）
            latest = (rows[0].get("TRADE_DATE") or "")[:10]
            day = [r for r in rows if (r.get("TRADE_DATE") or "")[:10] == latest]
            today_row = next((r for r in day if r.get("MEMBER_NAME_ABBR") == "本日合计"), None)
            prev_row = next((r for r in day if r.get("MEMBER_NAME_ABBR") == "上日合计"), None)
            if today_row:
                out = {"date": latest,
                       "long": float(today_row.get("LONG_POSITION") or 0),
                       "short": float(today_row.get("SHORT_POSITION") or 0),
                       "prev_long": float(prev_row.get("LONG_POSITION") or 0) if prev_row else 0.0,
                       "prev_short": float(prev_row.get("SHORT_POSITION") or 0) if prev_row else 0.0}
        with self.lock:
            self._rank_cache[sec] = (today, out)
        return out

    # ---------------- 生意社基差（软依赖，反爬即降级） ----------------
    def basis_table(self, day=None):
        """生意社当日全市场基差表，返回 {SYM大写: 基差率=(主力价-现货)/现货 的相反数口径}。

        口径说明：akshare 用 dom_basis_rate=主力价/现货-1（期货升水为正）；本项目基差因子要
        "现货升水为正"，故返回 现货/主力-1 = -dom_basis_rate。
        识别到 JS-cookie 反爬挑战页(HW_CHECK)或无表格时返回 None（当日基差整体不可用）。
        """
        d = day or datetime.date.today()
        if isinstance(d, datetime.date):
            ds = d.strftime("%Y-%m-%d")
        else:
            ds = str(d)
        with self.lock:
            if ds in self._basis_cache:
                return self._basis_cache[ds]
        url = config.FUND_PPI_URL.format(date=ds)
        table = None
        try:
            r = http.get(url, headers={"User-Agent": config.HEADERS_COMMON["User-Agent"],
                                       "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
                         timeout=12)
            text = r.content.decode(r.apparent_encoding or "utf-8", errors="replace")
            if r.status_code != 200 or "HW_CHECK" in text or text.count("<tr") < 5 or len(text) < 8000:
                # JS-cookie 反爬挑战页 / 非交易日空页
                with self.lock:
                    self._basis_cache[ds] = None
                return None
            g = _TableGrabber()
            g.feed(text)
            # 找含"现货/主力"表头、且行数最多的那张表
            cands = [t for t in g.tables if any(
                any(("现货" in c or "主力" in c) for c in row) for row in t[:3])]
            table = max(cands, key=len) if cands else (max(g.tables, key=len) if g.tables else None)
        except Exception:
            table = None
        out = {}
        if table:
            for row in table:
                cells = [c for c in row if c]
                # 期望：名称(含英文代码) 现货价 近月 近月价 主力 主力价 ...；用正则抽英文品种代码与数字
                joined = " ".join(cells)
                m = re.search(r"\b([A-Za-z]{1,2})\d{3,4}\b", joined)
                nums = re.findall(r"-?\d[\d,]*(?:\.\d+)?", joined.replace(",", ""))
                if m and len(nums) >= 3:
                    sym = m.group(1).upper()
                    try:
                        spot = float(nums[0])
                        dom = float(nums[2]) if len(nums) > 2 else float(nums[1])
                        if spot > 0 and dom > 0:
                            out[sym] = spot / dom - 1.0      # 现货升水为正
                    except ValueError:
                        continue
        out = out or None
        with self.lock:
            self._basis_cache[ds] = out
        return out
