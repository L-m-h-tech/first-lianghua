# -*- coding: utf-8 -*-
"""第13轮 WP-C：基本面数据直连（零新增第三方依赖，仅用 requests + 标准库）。

数据源（2026-09-01 实测）：
  * 库存/仓单时序：东方财富数据中心 RPT_FUTU_STOCKDATA（全市场注册仓单/库存，含约3个月历史，
    一次请求拿一个品种的完整时序，可算滚动分位与周环比）；品种代码表 RPT_FUTU_POSITIONCODE。
  * 龙虎榜：东方财富 RPT_FUTU_DAILYPOSITION，TYPE=2 返回某主力合约"本日合计/上日合计"前20席多空。
  * 基差：生意社 sf/day 表（第126轮重写：curl_cffi 浏览器 TLS 指纹抓取 + 中文品名映射解析）。
    该站按 JA3 指纹识别 Python-requests 并返回 HW_CHECK 挑战页（cookie/头怎么带都过不去），
    且页面已改版去掉完整合约代码——旧"首页拿cookie+合约代码正则"双路径自上线从未取到数据
    （monitor.db basis_rate 全空）。新链路：chrome 指纹直过挑战 + 中文品名映射 config.VARIETIES，
    当日未发布/非交易日自动回退最近交易日，失败软降级绝不编造。
设计：日频缓存（同一自然日只拉一次），线程安全；库存/基差由后台线程批量预热，龙虎榜按主力合约
按需取并缓存，全部失败都软降级（返回 None），不影响主监控管线。
"""
import datetime
import re
import threading

import config
from http_client import http
import html_text

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


# ---------------- 生意社基差解析（第126轮：纯函数、离线可测） ----------------

# 生意社页面商品名与 config.VARIETIES 命名不同的实测差异项（2026-09-11 实测 52 行产品、
# 精确映射 40 + 别名 7；其余如 原油/20号胶/氧化铝/花生 等该站无对应现货行，诚实跳过不编造）。
_PPI_NAME_ALIAS = {
    "天然橡胶": "橡胶",        # RU
    "石油沥青": "沥青",        # BU
    "热轧卷板": "热卷",        # HC
    "菜籽粕": "菜粕",          # RM
    "聚氯乙烯": "PVC",         # V
    "聚乙烯": "塑料",          # L
    "涤纶短纤": "短纤",        # PF
}

_PPI_SYM_MAP = {}
for _name, _meta in config.VARIETIES.items():
    _PPI_SYM_MAP[_name] = str(_meta.get("sym", "")).upper()
for _alias, _name in _PPI_NAME_ALIAS.items():
    _meta = config.VARIETIES.get(_name) or {}
    if _meta.get("sym"):
        _PPI_SYM_MAP[_alias] = str(_meta["sym"]).upper()


def _ppi_float(cell):
    """单元格文本 -> 正 float（去逗号/空白/nbsp）；失败或非正返回 None（'%'/粘连文本恒失败）。"""
    s = re.sub(r"[,\s]|&nbsp;?", "", str(cell or ""))
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _ppi_sym(name):
    """生意社商品名 -> 本项目 sym：精确/别名命中，或剥离尾缀交易所代码（菜籽油OI/甲醇MA）；未匹配 None。"""
    n = re.sub(r"\s+", "", str(name or ""))
    if n in _PPI_SYM_MAP:
        return _PPI_SYM_MAP[n]
    return _PPI_SYM_MAP.get(re.sub(r"[A-Za-z]+$", "", n))


def _ppi_product_rows(tables):
    """extract_tables 结果 -> 产品行最多那张表的有效行（首格可映射品种名、次格为正数价格）。"""
    best = None
    for tbl in tables:
        rows = [cells for cells in tbl
                if len(cells) >= 6 and _ppi_sym(cells[0]) and _ppi_float(cells[1])]
        if rows and (best is None or len(rows) > len(best)):
            best = rows
    return best or []


def parse_ppi_basis_html(text):
    """生意社 sf/day 页 HTML -> {sym大写: 基差率=现货/主力-1（现货升水为正）}。纯函数、离线可测。

    适配 2026-09 页面（每行8格）：商品|现货价|最近月|最近价|现期差对|主力月|主力价|现期差对。
    现期差两格是嵌套小表，lxml extract 后被压成粘连文本（如 '1384.40%'，含 '%' 恒不可解析），
    解析不使用；主力价=行内**从右往左**第一对"4位月份+可解析价格"——注意主力价本身可能恰为
    4位数字（如 3108），向右取价失败须继续向左扫（不可 break），才能落到真正的月份格。
    挑战页/非数据页/无可解析行返回 None。html.extract_tables 的 .//tr 会把嵌套小表行混入
    同表（首格为数字），由"首格须可映射品种名"过滤，绝不误读。
    """
    if not text:
        return None
    out = {}
    for cells in _ppi_product_rows(html_text.extract_tables(text)):
        spot = _ppi_float(cells[1])
        dom = None
        for i in range(len(cells) - 2, 0, -1):
            if re.fullmatch(r"\d{4}", str(cells[i]).strip()):
                v = _ppi_float(cells[i + 1])
                if v:
                    dom = v
                    break
        if spot and dom:
            out[_ppi_sym(cells[0])] = spot / dom - 1.0
    return out or None


def ppi_page_ok(text):
    """生意社数据页判定：非挑战页且行数/体量过阈值（挑战页636B/0行、周末空页21KB但无产品行）。"""
    return bool(text) and "HW_CHECK" not in text and text.count("<tr") >= 5 and len(text) >= 8000


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

    # ---------------- 生意社基差（第126轮重写：TLS指纹抓取 + 中文名映射解析） ----------------

    @staticmethod
    def _fetch_ppi_page(ds):
        """抓某日基差页 HTML 文本；挑战页/非数据页/请求失败返回 None（软降级）。

        impersonate="chrome"（http_client B7）：生意社按 TLS/JA3 指纹拦 Python-requests，
        挑战页不种 cookie、旧"首页拿cookie"路径无效；浏览器指纹直过（curl_cffi 缺失时
        http_client 自动回退普通 requests，行为同旧版——挑战页→None 降级）。"""
        url = config.FUND_PPI_URL.format(date=ds)
        try:
            r = http.get(url, source="100ppi", impersonate="chrome", timeout=12)
            if r.status_code != 200:
                return None
            text = r.content.decode(getattr(r, "encoding", None) or "utf-8", errors="replace")
        except Exception:
            return None
        return text if ppi_page_ok(text) else None

    def basis_table(self, day=None):
        """生意社当日全市场基差表，返回 {SYM大写: 基差率=现货/主力-1（现货升水为正）} 或 None。

        第126轮重写背景：旧实现（首页拿cookie + 合约代码正则）自上线从未取到过数据
        （monitor.db fundamentals.basis_rate 全表无一行非空）——HW_CHECK 挑战页 + 页面改版
        后已无完整合约代码双重卡死。新实现：
        - 抓取走 chrome TLS 指纹（2026-09-12 实测 0.3s 拿到 58KB 完整表）；
        - 解析按中文品名映射 config.VARIETIES（+_PPI_NAME_ALIAS 别名 + 尾码剥离）；
        - 当日页未发布（生意社约16:30更新）或周末/假期时**逐日回退最多4天**取最近有数据页；
        - 全部失败返回 None：基差子项缺失，库存/龙虎榜/carry 三子项按权重重归一，绝不编造。
        缓存键=请求日（回退命中的结果也缓存到当日，进程内同日只抓一轮）。"""
        if isinstance(day, datetime.date):
            start = day
        elif isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day.strip() or ""):
            y, m, dd = (int(x) for x in day.strip().split("-"))
            start = datetime.date(y, m, dd)
        else:
            start = datetime.date.today()
        key = start.strftime("%Y-%m-%d")
        with self.lock:
            if key in self._basis_cache:
                return self._basis_cache[key]
        out = None
        for offset in range(0, 5):
            ds = (start - datetime.timedelta(days=offset)).strftime("%Y-%m-%d")
            out = parse_ppi_basis_html(self._fetch_ppi_page(ds) or "")
            if out:
                break
        try:
            import parser_health
            parser_health.record("ppi_basis", out is not None, len(out or {}))
        except Exception:
            pass
        with self.lock:
            self._basis_cache[key] = out
        return out
