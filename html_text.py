# -*- coding: utf-8 -*-
"""第94轮 A3（对标 scrapling 的 lxml 级文本/表格提取）：统一的 HTML → 文本 / 表格提取。

背景：此前新浪新闻/全网扫描用 `re.sub(r"<[^>]+>","",html)` 粗暴去标签，会夹带 script/style/
注释噪音；fundamental_data 又各写一套 html.parser 表格逻辑。本模块把两类能力做成一处，
各源复用，也是 A2 自愈选择器 / B7 markdown 资产 / B3 模糊锚点的底座。

实现：lxml 已收编进 requirements（第94轮决策门通过，本机早已安装）；lxml 可用时优先
（C 加速 + html.fromstring 健壮容错），否则自动回退纯标准库 html.parser——两种后端对外
接口完全一致，生产行为等价（回退路径由 `_BACKEND` 暴露便于测试两种实现）。
"""
import re
from html.parser import HTMLParser

try:                                   # 决策门：lxml 已收编；导入失败自动回退 stdlib
    from lxml import html as _lhtml
    _HAS_LXML = True
except Exception:                      # pragma: no cover - 本机已装，防御性回退
    _HAS_LXML = False

_BACKEND = "lxml" if _HAS_LXML else "stdlib"
_TAG_RE = re.compile(r"<[^>]+>")
_SKIP_TAGS = {"script", "style", "noscript", "template", "iframe", "svg", "head", "title"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
               "table", "thead", "tbody", "tfoot", "tr", "section", "article", "ul", "ol", "blockquote"}


# ---------------- 文本提取 ----------------

class _TextParser(HTMLParser):
    """stdlib 后端：去 script/style/注释，按块级标签换行，文本规范化。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._blocks = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        if self._skip == 0 and tag in _BLOCK_TAGS:
            self._blocks.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip == 0 and data.strip():
            self._blocks.append(data)


def clean_text(html, max_len=None):
    """HTML → 干净文本：去 script/style/注释、块级换行、空白折叠。缺/坏返回空串。"""
    if not html:
        return ""
    if _HAS_LXML:
        try:
            root = _lhtml.fromstring(html)
            for el in root.xpath("//script|//style|//noscript|//template|//iframe|//head"):
                el.getparent().remove(el)
            text = root.text_content() or ""
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n\s*\n+", "\n", text)
        except Exception:
            text = _stdlib_text(html)
    else:
        text = _stdlib_text(html)
    text = text.strip()
    return text[:max_len] if max_len else text


def _stdlib_text(html):
    p = _TextParser()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    text = "".join(p._blocks)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


# ---------------- 表格提取 ----------------

class _TableParser(HTMLParser):
    """stdlib 后端：把 <table> 提取为 [[单元格,...],...]（行列结构）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._in_table = 0
        self._row = None
        self._cell = []
        self._cell_open = False
        self.tables = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            if self._in_table == 0:
                self.tables.append([])      # 新表开始：建行容器
            self._in_table += 1
            return
        if self._in_table == 0:
            return
        if tag in ("tr", "thead", "tbody", "tfoot"):
            if self._cell_open:                     # 行内遇到行标签：收掉当前单元格
                self._row.append(re.sub(r"\s+", "", "".join(self._cell)))
                self._cell, self._cell_open = [], False
            if tag == "tr":
                if self._row is not None:
                    self.tables[-1].append(self._row)
                self._row = []
        elif tag in ("td", "th"):
            if self._cell_open:
                self._row.append(re.sub(r"\s+", "", "".join(self._cell)))
                self._cell = []
            self._cell_open = True

    def handle_endtag(self, tag):
        if tag == "table":
            if self._row is not None:
                self.tables[-1].append(self._row)
            self._row = None
            self._in_table = max(0, self._in_table - 1)
            return
        if self._in_table == 0:
            return
        if tag in ("td", "th") and self._cell_open:
            self._row.append(re.sub(r"\s+", "", "".join(self._cell)))
            self._cell, self._cell_open = [], False
        elif tag == "tr" and self._row is not None:
            self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._in_table > 0 and self._cell_open:
            self._cell.append(data)


def extract_tables(html):
    """HTML → [ [[单元格,...],...], ... ]（每张表一行=一行）。缺/无表返回 []。"""
    if not html:
        return []
    if _HAS_LXML:
        try:
            root = _lhtml.fromstring(html)
            out = []
            for tbl in root.xpath("//table"):
                rows = []
                for tr in tbl.xpath(".//tr"):
                    cells = [re.sub(r"\s+", "", (td.text_content() or ""))
                             for td in tr.xpath("./td|./th")]
                    if cells:
                        rows.append(cells)
                if rows:
                    out.append(rows)
            return out
        except Exception:
            pass
    p = _TableParser()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    return p.tables


def find_anchor_line(text, anchors, min_ratio=0.6):
    """B3 模糊锚点定位（difflib）：在一段文本行里找最接近任一锚点的行号（0 起）；无则 -1。
    供"网站改版后按稳定锚点文本重定位目标"使用（解析降级，绝不做脏数据）。"""
    import difflib
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    best, best_ratio = -1, 0.0
    for i, ln in enumerate(lines):
        for a in anchors:
            r = difflib.SequenceMatcher(None, ln.strip(), a).ratio()
            if r > best_ratio:
                best, best_ratio = i, r
    return best if best_ratio >= min_ratio else -1


def first_valid(candidates, validator):
    """A2 多候选解析链：按序尝试 candidates=[(名称,callable)]，第一个通过 validator 的返回 (名称,结果)；全失败 (None,None)。"""
    for label, fn in candidates:
        try:
            res = fn()
        except Exception:
            continue
        if validator(res):
            return label, res
    return None, None


def backend():
    """当前生效的后端名（lxml / stdlib），测试用（动态判定，含运行时降级）。"""
    return "lxml" if _HAS_LXML else "stdlib"
