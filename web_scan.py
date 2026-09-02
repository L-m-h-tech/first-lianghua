# -*- coding: utf-8 -*-
"""【增强⑫】全网数据查找（每 WEB_SCAN_INTERVAL=180 秒一轮）：
聚合"新闻 / 金融 / 突发事件"三类全网公开数据源，喂给同一个新闻情绪分析管线
（factors.NewsFactor），并对**新出现**的高影响消息触发与原油急动相同的"紧急轮动"。

文字源（全部公开接口、单点失败互不影响）：
  1. 东方财富 7x24 快讯        getFastNewsList
  2. 新浪财经滚动（财经/全球）  feed.mix.sina.com.cn
  3. 华尔街见闻 全球快讯        api-one-wscn.awtmt.com
  4. 同花顺 7x24 快讯           news.10jqka.com.cn
  （新浪7x24、金十由 sina_news 在每轮分析时抓取，不在此重复）
金融数据（新浪全球行情 hq.sinajs.cn）：
  美元指数 / 纽约黄金白银 / 美铜 / 美股三大指数 / A股两大指数，
  扫描间隔内急变超阈值即合成一条"【金融数据】实测消息"（可信度1.0，属于真实数据）。

可信度分级（真实的优先、存疑的决定因素往后排）：
  - 权威/实测源 confidence≈1.0；一般转载源 0.75~0.9；
  - 含"传闻/据称/网传/未经证实/疑似…"等存疑词的消息 confidence×0.4 并打 doubtful 标记，
    在 factors 打分与报告 Top 消息排序中自然靠后。
"""
import re
import threading
import time
from datetime import datetime

import config
import factors
from http_client import http
from utils import LOG, sanitize

# 源可信度（真实优先）：权威快讯/实测数据=1.0；一线财经媒体≈0.9；转载聚合≈0.75
SOURCE_CREDIBILITY = {
    "东财7x24": 1.0, "华尔街见闻": 0.95, "同花顺7x24": 0.9,
    "全网扫描·金融数据": 1.0,
}
# 新浪滚动稿的署名媒体在白名单内视为权威（1.0），否则按一般媒体 0.8
AUTH_MEDIA = {
    "新华社", "新华财经", "央视新闻", "央视财经", "人民日报", "证券时报", "中国证券报",
    "上海证券报", "证券日报", "经济参考报", "第一财经", "财新网", "澎湃新闻", "界面新闻",
    "21世纪经济报道", "经济观察报", "中国基金报", "期货日报", "国际金融报", "中证网",
}
_HEADERS = {"User-Agent": config.HEADERS_COMMON["User-Agent"]}
_SINA_HEADERS = {"User-Agent": config.HEADERS_COMMON["User-Agent"],
                 "Referer": "https://finance.sina.com.cn/"}


# ============================ 文字源抓取 ============================

def fetch_eastmoney_flash():
    """东方财富 7x24 快讯"""
    trace = str(int(time.time() * 1000))
    url = (f"https://np-listapi.eastmoney.com/comm/web/getFastNewsList?client=web"
           f"&biz=web_724&fastColumn=102&sortEnd=&pageSize={config.WEB_SCAN_PAGE_SIZE}"
           f"&req_trace={trace}")
    r = http.get(url, headers=_HEADERS, timeout=config.TIMEOUT)
    items = (r.json().get("data") or {}).get("fastNewsList") or []
    out = []
    for it in items:
        content = sanitize((it.get("summary") or it.get("title") or "").strip())
        if not content:
            continue
        dt = datetime.now()
        try:
            dt = datetime.strptime(it.get("showTime", ""), "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
        out.append({"source": "东财7x24", "time": dt, "content": content,
                    "important": bool(it.get("titleColor"))})
    return out


def fetch_sina_roll():
    """新浪财经滚动新闻（财经 lid=2516 + 全球 lid=2509），署名媒体决定可信度"""
    out = []
    for lid in (2516, 2509):
        try:
            url = (f"https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid={lid}"
                   f"&num={config.WEB_SCAN_PAGE_SIZE}&page=1")
            r = http.get(url, headers=_HEADERS, timeout=config.TIMEOUT)
            data = (r.json().get("result") or {}).get("data") or []
        except Exception:
            continue
        for it in data:
            title = sanitize((it.get("title") or "").strip())
            intro = sanitize((it.get("intro") or "").strip())
            content = (title + ("。" + intro if intro and intro != title else "")).strip("。")
            if not content:
                continue
            try:
                dt = datetime.fromtimestamp(int(it.get("ctime", 0)))
            except Exception:
                dt = datetime.now()
            media = (it.get("media_name") or "新浪财经").strip()
            out.append({"source": media if media in AUTH_MEDIA else "新浪滚动·" + media[:10],
                        "time": dt, "content": content, "important": False})
    return out


def fetch_wallstreetcn():
    """华尔街见闻全球快讯"""
    url = ("https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=global-channel"
           f"&limit={config.WEB_SCAN_PAGE_SIZE}")
    r = http.get(url, headers=_HEADERS, timeout=config.TIMEOUT)
    items = ((r.json().get("data") or {}).get("items")) or []
    out = []
    for it in items:
        content = sanitize(re.sub(r"<[^>]+>", "",
                                  it.get("content_text") or it.get("title") or "").strip())
        if not content:
            continue
        try:
            dt = datetime.fromtimestamp(int(it.get("display_time", 0)))
        except Exception:
            dt = datetime.now()
        out.append({"source": "华尔街见闻", "time": dt, "content": content,
                    "important": bool(it.get("is_major"))})
    return out


def fetch_10jqka():
    """同花顺 7x24 快讯"""
    url = ("https://news.10jqka.com.cn/tapp/news/push/stock/"
           f"?page_size={config.WEB_SCAN_PAGE_SIZE}&track=website&tag=&page=1")
    r = http.get(url, headers=_HEADERS, timeout=config.TIMEOUT)
    items = ((r.json().get("data") or {}).get("list")) or []
    out = []
    for it in items:
        title = sanitize((it.get("title") or "").strip())
        digest = sanitize((it.get("digest") or "").strip())
        content = (title + ("。" + digest if digest and title not in digest else "")).strip("。")
        if not content:
            continue
        try:
            dt = datetime.fromtimestamp(int(it.get("ctime", 0)))
        except Exception:
            dt = datetime.now()
        important = str(it.get("import", "")) in ("1", "2") or bool(it.get("color"))
        out.append({"source": "同花顺7x24", "time": dt, "content": content,
                    "important": important})
    return out


# ============================ 金融数据（实测） ============================

# 新浪全球行情代码 -> (展示名, 比较方式: scan=与上轮扫描比 / day=用接口日涨跌)
_MACRO_SYMBOLS = [
    ("DINIW", "美元指数", "scan"),
    ("hf_GC", "纽约黄金", "scan"),
    ("hf_SI", "纽约白银", "scan"),
    ("hf_HG", "美铜", "scan"),
    ("gb_ixic", "纳斯达克", "day"),
    ("gb_dji", "道琼斯", "day"),
    ("gb_inx", "标普500", "day"),
    ("s_sh000001", "上证指数", "day"),
    ("s_sz399001", "深证成指", "day"),
]


def fetch_macro_quotes():
    """新浪全球行情 -> {展示名: (最新价, 日涨跌幅%分数或None)}"""
    codes = ",".join(s for s, _, _ in _MACRO_SYMBOLS)
    r = http.get(f"https://hq.sinajs.cn/list={codes}", headers=_SINA_HEADERS,
                     timeout=config.TIMEOUT)
    r.encoding = "gbk"
    snap = {}
    for sym, name, mode in _MACRO_SYMBOLS:
        m = re.search(rf'hq_str_{sym}="([^"]*)"', r.text)
        if not m:
            continue
        f = m.group(1).split(",")
        try:
            if sym.startswith("s_"):                 # 上证/深成: 名称,点位,涨跌,涨跌%
                price, daypct = float(f[1]), float(f[3]) / 100.0
            elif sym == "DINIW":                      # 美元指数: 时间,最新价,...
                price, daypct = float(f[1]), None
            elif sym.startswith("hf_"):              # 外盘期货: 最新,...,昨结=f[7]
                price, prev = float(f[0]), float(f[7])
                daypct = (price / prev - 1.0) if prev else None
            else:                                    # 美股: 名称,最新,涨跌%
                price, daypct = float(f[1]), float(f[2]) / 100.0
            snap[name] = (price, daypct)
        except (IndexError, ValueError):
            continue
    return snap


def _macro_text(name, pct, mode):
    """把金融指标急变转写成能被关键词词典识别的"实测消息"（措辞全部取自既有词典）"""
    up = pct >= 0
    span = "日内" if mode == "day" else "近3分钟"
    d = "上涨" if up else "下跌"
    val = f"{abs(pct) * 100:.2f}%"
    if name == "美元指数":
        return (f"【金融数据】美元指数{span}{d}{val}，" +
                ("美元指数上涨、美元走强，对大宗商品整体形成压制" if up
                 else "美元指数走低、美元走弱、美元回落，对大宗商品整体形成利多"))
    if name in ("纽约黄金", "纽约白银"):
        return (f"【金融数据】{name}{span}{d}{val}，" +
                ("避险情绪升温，央行购金预期增强，贵金属板块走强" if up
                 else "避险情绪降温，实际利率上行，贵金属板块走弱"))
    if name == "美铜":
        return (f"【金融数据】美铜{span}{d}{val}，" +
                ("精矿供应紧张、新能源需求向好，有色板块走强" if up
                 else "库存大增、产能释放，有色板块走弱"))
    if name in ("纳斯达克", "道琼斯", "标普500"):
        return (f"【金融数据】{name}{span}{d}{val}，" +
                ("海外市场风险偏好回升，宽松预期升温" if up
                 else "美股大跌、海外暴跌，避险情绪升温，经济衰退担忧加重"))
    return (f"【金融数据】{name}{span}{d}{val}，" +
            ("政策发力、利好政策频出，国内市场情绪回暖" if up
             else "市场风险偏好下降，经济数据不及预期"))


# ============================ 可信度分级 ============================

def tag_credibility(item):
    """就地补充 confidence/doubtful 字段：真实优先，存疑打折并标记"""
    src = item["source"]
    if src.startswith("新浪滚动·"):
        conf = 0.8
    else:
        conf = SOURCE_CREDIBILITY.get(src, 0.75)
    doubtful = any(w in item["content"] for w in config.WEB_DOUBTFUL_WORDS)
    if doubtful:
        conf *= 0.4
    item["confidence"] = round(conf, 3)
    item["doubtful"] = doubtful
    return item


# ============================ 扫描器 ============================

class WebScanner:
    """每3分钟全网扫描一次：抓取→可信度分级→（金融急变合成消息）→影响评估"""

    def __init__(self):
        self.stop = threading.Event()
        self._seen = set()                    # 已见过的消息键（只对"新消息"评估触发）
        self._macro_last = {}                 # 展示名 -> (ts, 价格)，scan类指标的比较基准
        self._macro_alert_at = {}             # 展示名 -> 上次合成消息的时间戳（冷却）
        self._baselined = False               # 首轮只建基线，不触发紧急轮动
        self.last_summary = "全网扫描尚未运行"
        self._fail = 0

    # ---------- 一次完整抓取 ----------
    def scan_once(self):
        items, n_src_ok = [], 0
        for fn in (fetch_eastmoney_flash, fetch_sina_roll,
                   fetch_wallstreetcn, fetch_10jqka):
            try:
                got = fn()
                if got:
                    items.extend(got)
                    n_src_ok += 1
            except Exception as e:
                LOG.warning("全网扫描源 %s 获取失败: %s", fn.__name__, e)
        items.extend(self._macro_items())
        for it in items:
            tag_credibility(it)
        self._fail = 0 if n_src_ok else self._fail + 1
        self.last_summary = f"全网扫描: 文字源在线{n_src_ok}/4，本轮{len(items)}条"
        return items

    # ---------- 金融数据急变 -> 合成实测消息 ----------
    def _macro_items(self):
        out = []
        try:
            snap = fetch_macro_quotes()
        except Exception as e:
            LOG.debug("全球金融行情获取失败: %s", e)
            return out
        now = time.time()
        for name, pct in self._macro_pcts(snap, now):
            thr = config.WEB_MACRO_THRESHOLDS.get(name)
            if thr is None or abs(pct) < thr:
                continue
            if now - self._macro_alert_at.get(name, 0) < config.WEB_MACRO_ALERT_COOLDOWN:
                continue
            self._macro_alert_at[name] = now
            mode = dict((n, m) for _, n, m in _MACRO_SYMBOLS)[name]
            out.append({"source": "全网扫描·金融数据", "time": datetime.now(),
                        "content": _macro_text(name, pct, mode), "important": False,
                        "confidence": 1.0, "doubtful": False,
                        "_macro": (name, pct)})
            LOG.warning("金融数据急变: %s %+.2f%% 达触发阈值，合成实测消息", name, pct * 100)
        return out

    def _macro_pcts(self, snap, now):
        """返回 [(name, pct)]，并推进 scan 类指标的比较基准"""
        rows = []
        for _, name, mode in _MACRO_SYMBOLS:
            if name not in snap:
                continue
            price, daypct = snap[name]
            if mode == "day":
                if daypct is not None:
                    rows.append((name, daypct))
            else:
                prev = self._macro_last.get(name)
                self._macro_last[name] = (now, price)   # 每轮更新基准
                if prev:
                    pct = price / prev[1] - 1.0
                    rows.append((name, pct))
        return rows

    # ---------- 影响评估：新消息是否足以触发紧急轮动 ----------
    def evaluate(self, new_items):
        """返回影响最大的一条触发信息 dict，或 None"""
        best = None
        for n in new_items:
            conf = n.get("confidence", 1.0)
            w = factors._lex_weight(n["content"], None) * conf
            if n.get("important"):
                w *= 1.6
            breaking = any(bw in n["content"] for bw in config.WEB_BREAKING_WORDS)
            trig = config.WEB_IMPACT_TRIGGER if not (n.get("important") or breaking) \
                else config.WEB_IMPORTANT_TRIGGER
            if abs(w) < trig:
                continue
            if best is None or abs(w) > abs(best["weight"]):
                aff = self._affected(n["content"], conf)
                best = {"weight": w, "item": n, "breaking": breaking, "varieties": aff}
        return best

    @staticmethod
    def _affected(content, conf):
        """该消息直接影响到的品种（板块词典权重 + 品种名直接命中）"""
        out = []
        for vname, meta in config.VARIETIES.items():
            wv = factors._lex_weight(content, meta["cat"]) * conf
            if vname in content:
                wv += 1.0
            if abs(wv) >= 0.8:
                out.append((vname, round(wv, 2)))
        out.sort(key=lambda x: -abs(x[1]))
        return [v for v, _ in out[:12]]

    # ---------- 一轮刷新：返回 (新增条数, 触发信息或None, 全部条目) ----------
    def refresh(self):
        items = self.scan_once()
        fresh = []
        for n in items:
            key = (n["source"], n["content"][:50])
            if key in self._seen:
                continue
            self._seen.add(key)
            fresh.append(n)
        if len(self._seen) > 6000:
            self._seen = set(list(self._seen)[-4000:])
        trigger = None
        if self._baselined and fresh:
            trigger = self.evaluate(fresh)
        self._baselined = True
        return len(fresh), trigger, items

    def status_line(self):
        return f"[{self.last_summary}]"
