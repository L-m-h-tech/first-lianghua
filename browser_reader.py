# -*- coding: utf-8 -*-
"""【需求⑦】浏览器页面直读：自动发现本机带调试端口(9222/9223)的浏览器页签，
直接读取 OpenVlab市场页 与 交易可查首页 的可见内容（配合"打开行情网页(调试模式).bat"）。
数据用途：真实平值隐波/溢价榜 -> option_strategies 隐波检查(需求⑥⑦)；
头条多空动向/AI研报 -> 报告"页面数据/页面动向"行。
缺失页签自动补开；无调试端口时自动降级，不影响运行。

读取内容：
  OpenVlab /market 页面文本 →
    1) 隐波最大上升/下降榜：品种、涨幅%、隐波变化
    2) 波动率溢价最高/最低榜：品种、隐波、实波、溢价(IV-HV)
    3) 主表：品种、合约代码、平值隐波(真实IV!)、隐波变化、剩余天数、溢价
  交易可查 首页文本 →
    1) 头条精华：乾坤归一/大佬动向/外资动向/亏货动向 等的多空动向
    2) AI研报多空一览：看多X家/震荡X家/看空X家
    3) 多空领先指标：偏多氛围(0~100)

使用方法：双击 "打开行情网页(调试模式).bat"（用调试端口打开两个网页），
或任何带 --remote-debugging-port=9222 启动的浏览器；程序每30秒自动读取，
发现页签缺失时会自动补开。没有调试端口时程序仍用公开接口数据，不受影响。
"""
import json
import re
import threading
import time
from datetime import datetime

import config
from http_client import http
from utils import LOG

CDP_PORTS = (9222, 9223)
OVL_URL = "https://www.openvlab.cn/market"
JYKC_URL = "https://www.jiaoyikecha.com/"
OVL_CTAMAP_URL = "https://www.openvlab.cn/api/ctamap-all?add_overseas=true"
OVL_HEADERS = {"User-Agent": config.HEADERS_COMMON.get("User-Agent", ""),
               "Referer": "https://www.openvlab.cn/market"}

_NAME_ALIAS = {
    "沪金": "黄金", "沪银": "白银", "沪铜": "铜", "沪铝": "铝", "沪锌": "锌",
    "沪铅": "铅", "沪镍": "镍", "沪锡": "锡", "螺纹": "螺纹钢", "郑醇": "甲醇",
    "铁矿": "铁矿石", "燃油": "燃料油", "低硫燃油": "低硫燃料油", "塑料": "塑料",
    "聚乙烯": "塑料", "聚氯乙烯": "PVC", "PP": "聚丙烯", "菜油": "菜籽油",
    "郑棉": "棉花", "郑糖": "白糖", "LPG": "液化石油气", "20号胶": "20号胶",
}
_SKIP_NAMES = {"创业板ETF", "科创50ETF", "科创板50ETF", "上证50ETF", "沪深300ETF"}

_LABELS = ("乾坤归一", "日内推土机", "外资动向", "大佬动向", "亏货动向", "多空领先指标")
_DIR_MAP = {"看多": 1, "看空": -1, "最大流多": 1, "最大流空": -1,
            "布局做多": 1, "布局做空": -1, "扎堆做多": 1, "扎堆做空": -1}


def _map_name(raw):
    raw = (raw or "").strip()
    if not raw or raw in _SKIP_NAMES:
        return None
    if raw in config.VARIETIES:
        return raw
    return _NAME_ALIAS.get(raw)


def _is_float(s):
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def parse_openvlab(text):
    """解析OpenVlab市场页文本 → {"rank":{}, "atm_iv":{}, "prem":{}}（键为标准品种名）"""
    out = {"rank": {}, "atm_iv": {}, "prem": {}}
    if not text:
        return out
    lines = [l.strip() for l in text.split("\n")]
    n = len(lines)
    i = 0
    while i < n:
        l = lines[i]
        if l in ("隐波最大上升", "隐波最大下降"):
            i += 1
            while i < n and lines[i] in ("名称", "涨幅%", "隐波变化", "分时预览"):
                i += 1
            while i + 2 < n and lines[i + 1].startswith(("+", "-")) \
                    and lines[i + 1].endswith("%") and _is_float(lines[i + 2]):
                v = _map_name(lines[i])
                if v:
                    try:
                        out["rank"][v] = {"list": l,
                                          "chg": float(lines[i + 1].rstrip("%")),
                                          "iv_chg": float(lines[i + 2])}
                    except ValueError:
                        pass
                i += 3
            continue
        if l in ("波动率溢价最高", "波动率溢价最低"):
            i += 1
            while i < n and lines[i] in ("名称", "隐波", "实波", "溢价"):
                i += 1
            while i + 3 < n and _is_float(lines[i + 1]) and _is_float(lines[i + 2]) \
                    and _is_float(lines[i + 3]):
                v = _map_name(lines[i])
                if v:
                    try:
                        out["prem"][v] = {"list": l, "iv": float(lines[i + 1]),
                                          "hv": float(lines[i + 2]),
                                          "prem": float(lines[i + 3])}
                    except ValueError:
                        pass
                i += 4
            continue
        i += 1

    # 主表：品种 / 主 / 合约代码 / (价格数字被逐字拆行) / 涨幅% / 剩余X天 / 平值隐波 / 隐波变化 / 涨速 / 实波 / 溢价
    for m in re.finditer(r"\n([\u4e00-\u9fa5A-Za-z0-9]{1,8})\n主\n([A-Z]{1,2}\d{3,4})\n", text):
        v = _map_name(m.group(1))
        if not v:
            continue
        tail = text[m.end(): m.end() + 260]
        mm = re.search(r"\n([+-]?\d+\.\d+)%\n(\d+)天\n(\d+\.\d+)\n([+-][\d.]+)\n"
                       r"([+-]?[\d.]+)\n([\d.]+)\n([+-]?[\d.]+)\n"
                       r"([+-]?[\d.]+)\n(\d+)%\n(\d+)", tail)
        if mm:
            try:
                out["atm_iv"][v] = {"code": m.group(2), "chg": float(mm.group(1)),
                                    "days": int(mm.group(2)), "atm_iv": float(mm.group(3)),
                                    "iv_chg": float(mm.group(4)), "hv": float(mm.group(6)),
                                    "prem": float(mm.group(7)), "skew": float(mm.group(8)),
                                    "iv_pct": float(mm.group(9)) / 100.0,
                                    "skew_pct": float(mm.group(10)) / 100.0}
            except ValueError:
                pass
    return out


def parse_openvlab_ctamap(rows):
    """openvlab /api/ctamap-all 结果列表 -> {"atm_iv": {标准品种名: {...}}}

    字段映射（对齐装置侧 openvlab_collector.parse_ctamap_rows，复用已实测口径）：
      product_alias  -> 中文品种名（经 _map_name 转标准名）
      product        -> 合约代码（"EG_O" -> "eg"）
      price          -> 最新价
      atmv_current   -> atm_iv（平值隐波 %）
      atmv_1dchg     -> iv_chg（隐波1日变化）
      atmv_percentile-> iv_pct（隐波历史百分位,0~100）
      skew_current   -> skew（偏度）
      skew_percentile-> skew_pct
      rv22/hv        -> hv（20日实波）
      exp/expiry_date-> 到期
      frontfwd_mom   -> 前月/远月动量
    """
    out = {"atm_iv": {}}
    for item in rows or []:
        prod_und = (item.get("prodUnd") or "").strip()
        if not prod_und:
            continue  # 跳过 prodUnd 为空的海外品种（与装置侧口径一致）
        v = _map_name(item.get("product_alias") or "")
        if not v:
            continue
        code = (item.get("product") or prod_und).split("_")[0].lower()
        try:
            out["atm_iv"][v] = {
                "code": code,
                "price": _f(item.get("price")),
                "atm_iv": _f(item.get("atmv_current")),
                "iv_chg": _f(item.get("atmv_1dchg")),
                "iv_pct": _f(item.get("atmv_percentile")),
                "skew": _f(item.get("skew_current")),
                "skew_pct": _f(item.get("skew_percentile")),
                "hv": _f(item.get("rv22")),
                "carry": _f(item.get("ctn")),
                "frontfwd_mom": _f(item.get("frontfwd_mom")),
                "exp": (item.get("exp") or ""),
                "source": "ctamap",
            }
        except (ValueError, TypeError):
            continue
    return out


def _f(x):
    try:
        if x is None:
            return None
        return float(x)
    except (ValueError, TypeError):
        return None


def parse_jiaoyikecha(text):
    """解析交易可查首页文本 → {"views":{}, "headlines":[], "mood":None,
    "external":{}, "rating":{}, "fundamentals":[], "feed":[]}"""
    out = {"views": {}, "headlines": [], "mood": None,
           "external": {}, "rating": {}, "fundamentals": [], "feed": []}
    if not text:
        return out
    lines = [l.strip() for l in text.split("\n")]
    n = len(lines)
    # 1) 头条精华：指标名 -> 方向 -> 品种行（轮播，单次抓取可能只捕获部分条目）
    pending_label = None
    pending_dir = None
    for i, l in enumerate(lines):
        if l in _LABELS:
            pending_label, pending_dir = l, None
            continue
        if pending_label and l in _DIR_MAP:
            pending_dir = _DIR_MAP[l]
            continue
        if pending_label and pending_dir is not None and l:
            m = re.match(r"^([\u4e00-\u9fa5A-Za-z]{2,6}?)(?=\d|亿|$)", l)
            v = _map_name(m.group(1)) if m else None
            if v:
                out["headlines"].append({"label": pending_label, "dir": pending_dir,
                                         "variety": v, "text": l[:30]})
            pending_label = pending_dir = None
    # 2) AI研报多空一览：品种名与标题同行("沪金 AI研报多空一览")或上一行
    for i, l in enumerate(lines):
        name = None
        m_same = re.match(r"^([\u4e00-\u9fa5A-Za-z0-9]{2,8}?)\s*AI研报多空一览", l)
        if m_same:
            name = m_same.group(1)
        elif "AI研报多空一览" in l and i >= 1:
            name = lines[i - 1]
        if not name:
            continue
        v = _map_name(name)
        if not v:
            continue
        got = {}
        for j in range(i + 1, min(i + 6, n)):
            mm = re.match(r"^(看多|震荡|看空)：(\d+)家$", lines[j])
            if mm:
                got[mm.group(1)] = int(mm.group(2))
        if got:
            out["views"][v] = {"bullish": got.get("看多", 0),
                               "volatile": got.get("震荡", 0),
                               "bearish": got.get("看空", 0),
                               "total": sum(got.values())}
    # 3) 外盘比价表：innerText 中表格字段间有空行，过滤空行后 每5行一组
    #    （外盘品种/基准时间/基准价格/最新价格/偏离基准，品种名在 _EXT_MAP）
    nb = [l for l in lines if l]
    for bi, l in enumerate(nb):
        if l == "外盘品种":
            # 表头行后可能是列头(基准时间/基准价格/...)，先跳到第一个品种行
            j = bi + 1
            while j < len(nb) and nb[j] not in _EXT_MAP:
                j += 1
            while j + 4 < len(nb):
                name = nb[j]
                if name not in _EXT_MAP:
                    break
                try:
                    out["external"][name] = {
                        "time": nb[j + 1],
                        "base": _fnum(nb[j + 2]),
                        "last": _fnum(nb[j + 3]),
                        "dev": _fnum(nb[j + 4].rstrip("%")),
                        "dev_pct": nb[j + 4],
                    }
                except ValueError:
                    pass
                j += 5
            break
    # 4) 乾坤归一综合评级：标题行("乾坤归一综合评级...")与其后的品种/涨跌幅行。
    #    每2行一组 = "合约 评级价格" / "涨跌幅"。标题行也可能与首个品种同
    #    一行（如 "乾坤归一综合评级（...）玻璃2701 弱多972"），故先裁剪标题前缀。
    _GRADE_WORDS = "强多|中多|弱多|中性|强空|中空|弱空"
    for i, l in enumerate(lines):
        if "乾坤归一综合评级" in l:
            head = l.split("乾坤归一综合评级", 1)[1]
            j = i
            prev = head
            while j < n:
                # 行内可能残留 "（本数据仅供参考..."实际无此；统一先裁再匹配
                cur = lines[j] if j != i else head
                mm = re.match(r"^.*?([\u4e00-\u9fa5A-Za-z0-9]{1,10}?\d{4})"
                              rf"\s*({_GRADE_WORDS})([+-]?[\d.]+)$", cur)
                mv = lines[j + 1].rstrip("%") if j + 1 < n else ""
                if mm and (mv == "0" or _is_float(mv)):
                    key = (mm.group(1) + " " + mm.group(2) + mm.group(3)).strip()
                    # 品种名标准化：合约中文部分（去尾部数字）→ 标准品种名（供 analyzer 精确匹配）
                    cn = re.sub(r"\d+$", "", mm.group(1))
                    std_v = _map_name(cn)
                    out["rating"][key] = {
                        "contract": mm.group(1),
                        "grade": mm.group(2),
                        "price": _fnum(mm.group(3)),
                        "chg": _fnum(mv),
                        "chg_pct": lines[j + 1],
                        "variety": std_v or cn,
                    }
                    j += 2
                    continue
                j += 1
                # 已过完整评级表仍未匹配则退出（评级表是连续块）
                if lines[j - 1] and not any(w in lines[j - 1] for w in
                                            ("强多", "中多", "弱多", "中性", "强空", "中空", "弱空")):
                    if j - i > 3:
                        break
            break
    # 5) 最新基本面数据：标题行("最新基本面数据 新")，过滤空行后每组4行
    #    = 商品 / 指标 / "值 / 变化" / 日期
    fb = None
    for i, l in enumerate(lines):
        if l.startswith("最新基本面数据"):
            fb = i
            break
    if fb is not None:
        nb2 = [l for l in lines[fb + 1:] if l]
        j = 0
        # 跳过标题残留/表头（更多/商品/数据）
        while j < len(nb2) and nb2[j] in ("更多", "商品", "数据", ">>"):
            j += 1
        while j + 3 < len(nb2):
            name, metric = nb2[j], nb2[j + 1]
            val, date_v = nb2[j + 2], nb2[j + 3]
            m_dt = re.match(r"^\d{4}-\d{2}-\d{2}$", date_v or "")
            main_v = re.match(r"^(.*?)\s*/\s*([+-]?[\d.]+)$", val or "")
            if not metric or not m_dt:
                j += 1
                continue
            out["fundamentals"].append({
                "item": name, "metric": metric, "value": val,
                "base": (main_v.group(1) if main_v else None),
                "chg": (main_v.group(2) if main_v else None),
                "date": date_v})
            j += 4
    # 6) 信息流：时间戳行(MM/DD HH:MM) 后跟 分类标题 + 内容（跨行）
    _FEED_KINDS = ("商品盈亏席位", "牛熊线", "龙虎比", "商品持仓", "实时资讯",
                   "商品资讯", "现货信息", "资金流入流出", "最亏席位", "最佳席位")
    i = 0
    while i < n:
        m_ts = re.match(r"^(\d{2}/\d{2}\s+\d{2}:\d{2})$", lines[i])
        if not m_ts:
            i += 1
            continue
        ts, j = m_ts.group(1), i + 1
        while j < n and not lines[j]:
            j += 1
        if j < n and lines[j] in _FEED_KINDS:
            kind = lines[j]
            j += 1
            while j < n and not lines[j]:
                j += 1
            body, k = [], j
            while k < n and not re.match(r"^\d{2}/\d{2}\s+\d{2}:\d{2}$", lines[k]):
                body.append(lines[k])
                k += 1
            content = "\n".join(x for x in body if x).strip()
            if content:
                out["feed"].append({"ts": ts, "kind": kind, "text": content[:120]})
            i = k
            continue
        i = j
    # 去重（页面重复渲染导致的时间戳/内容完全一致条目）
    seen, uniq = set(), []
    for f in out["feed"]:
        key = (f["kind"], f["text"][:60])
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    out["feed"] = uniq
    # 7) 市场氛围：动态标签「偏多/震荡/偏空氛围」（Raphaël SVG 仪表盘，
    #    数值 0~100 不渲染为文本，仅能可靠取到方向标签）
    for l in lines:
        m = re.match(r"^(偏多|震荡|偏空)氛围$", l)
        if m:
            out["mood"] = {"label": l, "dir": {"偏多": 1, "震荡": 0, "偏空": -1}[m.group(1)]}
            break
    return out


# 外盘比价表品种名（17个，页面「外盘比价」区块）
_EXT_MAP = {"伦铜", "伦镍", "伦铅", "伦锌", "伦铝", "伦锡", "美豆油", "美白银",
            "美黄金", "铁矿FE", "马棕油", "美棉花", "美豆", "美玉米", "布原油",
            "美豆粕", "美原油"}


def _fnum(x):
    """宽松数字解析（容忍千分位逗号/百分号），失败返回 None。"""
    if x is None:
        return None
    s = str(x).replace(",", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


class BrowserReader:
    """30秒线程：读取带调试端口的浏览器中打开的两个网页"""

    def __init__(self):
        self.lock = threading.Lock()
        self.ovl = {"rank": {}, "atm_iv": {}, "prem": {}}
        self.jykc = {"views": {}, "headlines": [], "mood": None,
                     "external": {}, "rating": {}, "fundamentals": [], "feed": []}
        self._head_acc = {}   # 头条轮播，跨多次读取累积 (label,variety)->headline
        self.status = "未检测到调试端口"
        self.updated = None
        self.stop = threading.Event()
        self._notified = False

    # ---------- CDP 基础 ----------
    @staticmethod
    def _find_cdp():
        for port in CDP_PORTS:
            try:
                r = http.get(f"http://127.0.0.1:{port}/json", timeout=2)
                tabs = r.json()
                if isinstance(tabs, list):
                    return port, tabs
            except Exception:
                continue
        return None, []

    @staticmethod
    def _cdp_eval(ws_url, expr, timeout=8):
        import websocket
        ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        try:
            ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                "params": {"expression": expr, "returnByValue": True}}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == 1:
                    return msg.get("result", {}).get("result", {}).get("value")
        finally:
            ws.close()

    def _ensure_tab(self, port, tabs, url_kw, url):
        """CDP在线但目标页签缺失时自动补开"""
        if not any(url_kw in (t.get("url") or "") for t in tabs if t.get("type") == "page"):
            try:
                http.put(f"http://127.0.0.1:{port}/json/new?{url}", timeout=4)
                time.sleep(4)
                return True
            except Exception:
                pass
        return False

    # ---------- 业务 ----------
    def refresh(self):
        port, tabs = self._find_cdp()
        if not port:
            with self.lock:
                self.status = "未检测到调试端口(9222/9223)——可用'打开行情网页(调试模式).bat'启动浏览器以启用页面直读"
            if not self._notified:
                LOG.info("浏览器页面直读未启用: %s", self.status)
                self._notified = True
            # CDP不可用时仍尝试 REST 补全 OpenVlab（REST 不依赖调试端口）
            self._refresh_openvlab_rest()
            return
        changed = self._ensure_tab(port, tabs, "openvlab.cn", OVL_URL)
        changed |= self._ensure_tab(port, tabs, "jiaoyikecha", JYKC_URL)
        if changed:
            _, tabs = self._find_cdp()
        got_ovl = got_jyk = False
        try:
            for t in tabs:
                if t.get("type") != "page":
                    continue
                url = t.get("url") or ""
                if "openvlab.cn" in url and not got_ovl:
                    txt = self._cdp_eval(t["webSocketDebuggerUrl"],
                                         "document.body.innerText") or ""
                    with self.lock:
                        ovl_parsed = parse_openvlab(txt)
                        self.ovl["rank"].update(ovl_parsed["rank"])
                        self.ovl["prem"].update(ovl_parsed["prem"])
                        self.ovl["atm_iv"].update(ovl_parsed["atm_iv"])
                    got_ovl = True
                elif "jiaoyikecha" in url and not got_jyk:
                    txt = self._cdp_eval(t["webSocketDebuggerUrl"],
                                         "document.body.innerText") or ""
                    parsed = parse_jiaoyikecha(txt)
                    with self.lock:
                        self.jykc["views"].update(parsed["views"])
                        for h in parsed["headlines"]:
                            self._head_acc[(h["label"], h["variety"])] = h
                        self.jykc["headlines"] = list(self._head_acc.values())[-60:]
                        # 新增字段合并（增量，保留最新结果）
                        self.jykc["external"] = parsed.get("external") or {}
                        self.jykc["rating"] = parsed.get("rating") or {}
                        self.jykc["fundamentals"] = parsed.get("fundamentals") or []
                        self.jykc["feed"] = parsed.get("feed") or []
                        if parsed.get("mood") is not None:
                            self.jykc["mood"] = parsed["mood"]
                    got_jyk = True
            # REST 补全 OpenVlab atm_iv（83品种全量覆盖，弥补 CDP 主表只显示7个的不足）
            self._refresh_openvlab_rest()
            with self.lock:
                self.updated = datetime.now()
                self.status = (f"页面直读中: OpenVlab榜单{len(self.ovl['rank'])}条/真实隐波"
                               f"{len(self.ovl['atm_iv'])}个, 交易可查头条{len(self.jykc['headlines'])}条"
                               + (f", 外盘{len(self.jykc['external'])}个" if self.jykc['external'] else "")
                               + (f", 评级{len(self.jykc['rating'])}" if self.jykc['rating'] else "")
                               + (f", 基本面{len(self.jykc['fundamentals'])}项" if self.jykc['fundamentals'] else "")
                               + (f", 氛围{self.jykc['mood'].get('label','')}" if self.jykc.get("mood") else ""))
            self._notified = False
        except Exception as e:
            with self.lock:
                self.status = f"页面读取失败: {e}"
            LOG.debug("浏览器页面读取失败: %s", e)

    def _refresh_openvlab_rest(self):
        """REST 补全 OpenVlab atm_iv（ctamap-all 83品种全量，不依赖调试端口）"""
        try:
            r = http.get(OVL_CTAMAP_URL, headers=OVL_HEADERS, timeout=8)
            data = (r.json() or {}).get("result")
            if data:
                parsed = parse_openvlab_ctamap(data)
                if parsed.get("atm_iv"):
                    with self.lock:
                        self.ovl["atm_iv"].update(parsed["atm_iv"])
        except Exception:
            pass  # REST 失败静默，CDP 页面读取已保证基线可用

    def loop(self, interval=30):
        LOG.info("浏览器页面直读线程启动（每%d秒）", interval)
        while not self.stop.is_set():
            try:
                self.refresh()
            except Exception as e:
                LOG.debug("页面直读异常: %s", e)
            self.stop.wait(interval)

    # ---------- 对外快照 ----------
    def page_info(self, variety):
        """某品种的页面数据汇总（供因子/预测/策略使用）

        返回字段：rank/atm_iv/prem/view/headlines/mood（既有，保持签名）+ 新增
        rating（乾坤评级, 全品种表里含该品种的行）、external（外盘比价全表）、
        fundamentals（基本面全表）、feed（信息流全窗口）。"""
        with self.lock:
            rank = dict(self.ovl["rank"].get(variety) or {})
            atm = dict(self.ovl["atm_iv"].get(variety) or {})
            prem = dict(self.ovl["prem"].get(variety) or {})
            view = dict(self.jykc["views"].get(variety) or {})
            heads = [h for h in self.jykc["headlines"] if h.get("variety") == variety]
            mood = self.jykc.get("mood")
            rating = dict(self.jykc["rating"]) or {}
            external = dict(self.jykc["external"]) or {}
            fundamentals = list(self.jykc["fundamentals"]) or []
            feed = list(self.jykc["feed"]) or []
        return {"rank": rank, "atm_iv": atm, "prem": prem, "view": view,
                "headlines": heads, "mood": mood, "rating": rating,
                "external": external, "fundamentals": fundamentals, "feed": feed}

    def status_line(self):
        with self.lock:
            return f"[页面直读] {self.status}"
