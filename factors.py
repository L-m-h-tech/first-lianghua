# -*- coding: utf-8 -*-
"""【需求①】判断因子引擎（新闻情绪部分）：
1) 新闻情绪因子 —— 关键词词典（分板块加权）+ 正则规则（库存/PMI/非农/USDA等数据），
   时间衰减（半衰期约2.5小时）、重要快讯加权、品种名命中加权、泛词上下文闸门防误伤。
   【增强⑫】打分/趋势/Top排序均乘以消息可信度 confidence（web_scan 提供，存疑消息×0.4后排）。
【需求⑥】机构观点因子见 webdata.py + analyzer（机构动向）。
【需求⑦】trend() 消息面趋势（近4小时 vs 之前4~12小时），供非交易时段预测走向投票。
（原油联动/技术动量因子见 oil_data.py / futures_data.py，综合分组装见 analyzer.analyze_variety）
"""
import math
import re
from collections import deque
from datetime import datetime

import config
from utils import LOG, clip, sanitize

EN, BL, ME, PM, AG, FI = config.EN, config.BL, config.ME, config.PM, config.AG, config.FI
ALL = "ALL"

# ---------------- 关键词情绪词典 ----------------
# (关键词, 权重[正=利多/负=利空], 影响板块ALL或集合)
LEXICON = [
    # 宏观流动性（影响全部商品）
    ("美联储降息", 2.0, ALL), ("降息", 0.6, ALL), ("降准", 1.2, ALL),
    ("宽松", 0.7, ALL), ("美联储加息", -1.8, ALL), ("加息", -0.6, ALL),
    ("缩表", -1.0, ALL), ("紧缩", -0.7, ALL), ("鹰派", -0.8, ALL), ("鸽派", 0.8, ALL),
    ("经济刺激", 1.2, ALL), ("刺激政策", 1.0, ALL), ("稳增长", 0.8, ALL),
    ("经济衰退", -1.5, ALL), ("衰退", -1.2, ALL), ("经济数据不及预期", -1.0, ALL),
    ("数据不及预期", -0.8, ALL), ("通胀回升", 0.4, ALL), ("滞胀", 0.5, ALL),
    ("美元走强", -0.8, ALL), ("美元指数上涨", -0.8, ALL), ("美元指数走低", 0.8, ALL),
    ("美元走弱", 0.8, ALL), ("美元回落", 0.8, ALL),
    ("关税", -0.6, ALL), ("贸易摩擦", -0.6, ALL), ("贸易协议", 0.8, ALL),
    ("贸易谈判", 0.5, ALL), ("关税豁免", 0.6, ALL),
    # 地缘/能源供给
    ("减产", 1.8, {EN}), ("延长减产", 2.2, {EN}), ("自愿减产", 2.0, {EN}),
    ("超预期减产", 2.2, {EN}), ("供应中断", 2.0, {EN}), ("供给中断", 1.8, {EN}),
    ("断供", 1.6, {EN}), ("停产", 1.1, {EN}), ("检修增多", 0.6, {EN}),
    ("地缘紧张", 1.4, {EN}), ("地缘冲突", 1.6, {EN}), ("地缘政治风险", 1.5, {EN}),
    ("军事冲突", 1.5, {EN}), ("导弹袭击", 1.8, {EN}), ("袭击", 1.2, {EN}),
    ("爆炸", 1.0, {EN}), ("无人机", 0.8, {EN}), ("制裁", 0.9, {EN}),
    ("禁运", 1.5, {EN}), ("封锁", 1.3, {EN}),
    ("飓风", 1.2, {EN}), ("寒潮", 0.9, {EN}), ("极端天气", 0.8, {EN, AG}),
    ("高温天气", 0.5, {EN}),
    ("库存下降", 1.3, {EN}), ("去库", 1.0, {EN}), ("库存减少", 1.2, {EN}),
    ("低库存", 0.8, {EN, ME}),
    ("需求回升", 1.0, {EN}), ("需求强劲", 1.2, {EN}), ("需求旺盛", 1.1, {EN}),
    ("需求回暖", 1.0, {EN}), ("开工率回升", 0.5, {EN}),
    ("增产", -1.8, {EN}), ("恢复生产", -1.0, {EN}), ("复产", -0.8, {EN, ME}),
    ("扩产", -0.8, {EN}), ("累库", -1.2, {EN}), ("库存增加", -1.3, {EN}),
    ("库存累积", -1.3, {EN}), ("库存高企", -1.0, {EN, BL, ME}), ("胀库", -1.2, {EN}),
    ("需求疲软", -1.3, {EN}), ("需求疲弱", -1.3, {EN}), ("需求担忧", -1.2, {EN}),
    ("需求下滑", -1.2, {EN}), ("需求走弱", -1.2, {EN}),
    ("停火", -1.4, {EN}), ("停战", -1.2, {EN}), ("和谈", -1.2, {EN}),
    ("休战", -1.0, {EN}), ("局势缓和", -1.0, {EN}), ("紧张局势降温", -1.2, {EN}),
    ("解除制裁", -1.0, {EN}), ("谈判取得进展", -0.9, {EN}),
    # 黑色（地产/钢厂）
    ("地产政策", 0.9, {BL}), ("房地产政策", 1.0, {BL}), ("降首付", 1.0, {BL}),
    ("限购放松", 1.0, {BL}), ("城中村改造", 0.8, {BL}), ("专项债", 0.8, {BL}),
    ("粗钢压减", 1.2, {BL}), ("钢厂减产", 1.2, {BL}), ("限产", 1.0, {BL}),
    ("环保限产", 1.0, {BL}), ("铁水产量回升", 0.6, {BL}), ("金九银十", 0.5, {BL}),
    ("基建投资", 0.9, {BL}),
    ("地产低迷", -1.0, {BL}), ("地产下行", -1.0, {BL}), ("新开工下滑", -0.9, {BL}),
    ("地产数据不及预期", -0.9, {BL}), ("淡季", -0.7, {BL}), ("台风", -0.5, {BL}),
    # 有色/贵金属
    ("罢工", 1.4, {ME}), ("矿山事故", 1.3, {ME}), ("矿山停产", 1.4, {ME}),
    ("精矿供应紧张", 1.2, {ME}), ("电力紧张", 0.8, {ME}), ("设备更新", 0.5, {ME}),
    ("新能源需求", 0.7, {ME}), ("收储", 1.0, {ME}),
    ("库存大增", -1.0, {ME}), ("产能释放", -0.7, {ME}), ("抛储", -0.8, {ME, AG}),
    ("避险情绪", 1.0, {PM}), ("避险", 0.8, {PM}), ("央行购金", 1.3, {PM}),
    ("实际利率下行", 0.8, {PM}), ("实际利率上行", -0.8, {PM}),
    # 农产品
    ("干旱", 1.4, {AG}), ("霜冻", 1.4, {AG}), ("洪涝", 1.0, {AG}),
    ("减种", 1.0, {AG}), ("种植面积下调", 1.2, {AG}), ("拉尼娜", 1.0, {AG}),
    ("厄尔尼诺", 0.7, {AG}), ("出口限制", 1.0, {AG}), ("禁止出口", 1.4, {AG}),
    ("进口减少", 0.8, {AG}), ("天气升水", 0.8, {AG}), ("生猪产能去化", 0.8, {AG}),
    ("丰产", -1.2, {AG}), ("丰收", -1.0, {AG}), ("天气良好", -0.9, {AG}),
    ("增产预期", -1.0, {AG}), ("进口增加", -0.6, {AG}), ("养殖亏损", -0.8, {AG}),
    # 金融
    ("利好政策", 0.8, {FI}), ("政策发力", 0.7, {FI}), ("北向资金流入", 0.6, {FI}),
    ("降印花税", 1.2, {FI}), ("汇金增持", 0.9, {FI}), ("平准基金", 1.0, {FI}),
    ("美股大跌", -0.8, {FI}), ("海外暴跌", -1.0, {FI}), ("外资流出", -0.6, {FI}),
]

# ---------------- 数据类正则规则（EIA/API/PMI/非农/CPI/USDA/OPEC） ----------------
SPECIAL_RULES = [
    (r"EIA[^。]{0,30}(减少|下降|降)", 1.6, {EN}), (r"EIA[^。]{0,30}(增加|上升)", -1.4, {EN}),
    (r"API[^。]{0,30}(减少|下降)", 1.2, {EN}), (r"API[^。]{0,30}(增加|上升)", -1.0, {EN}),
    (r"(PMI|采购经理指数)[^。]{0,20}(高于|超预期|回升|扩张)", 1.0, ALL),
    (r"(PMI|采购经理指数)[^。]{0,20}(低于|不及|回落|收缩)", -1.0, ALL),
    (r"非农[^。]{0,20}(高于|强劲|超预期|大增)", -0.8, ALL),
    (r"非农[^。]{0,20}(低于|不及|疲软|减少)", 0.8, ALL),
    (r"CPI[^。]{0,20}(高于|超预期)", -0.5, ALL),
    (r"CPI[^。]{0,20}(低于|回落|不及)", 0.6, ALL),
    (r"USDA[^。]{0,30}(下调|调低)", 1.2, {AG}), (r"USDA[^。]{0,30}(上调|调高)", -1.0, {AG}),
    (r"欧佩克[^。]{0,15}减产", 1.8, {EN}), (r"欧佩克[^。]{0,15}增产", -1.6, {EN}),
    (r"OPEC\+?[^。]{0,15}减产", 1.8, {EN}), (r"OPEC\+?[^。]{0,15}增产", -1.6, {EN}),
    (r"美联储[^。]{0,20}降息", 2.0, ALL), (r"美联储[^。]{0,20}加息", -1.8, ALL),
    (r"(降息|降准)[^。]{0,15}落地", 1.0, ALL),
]

# ---------------- 上下文闸门 ----------------
# 泛词容易误伤普通新闻（公司财报里的"增产"、救灾新闻里的"无人机"等），
# 命中这些词时还必须同时出现闸门正则里的上下文词，才计入因子。
KEYWORD_GATES = {
    "增产": r"(欧佩克|OPEC|原油|石油|页岩|油田|产量|供应|出口)",
    "减产": r"(欧佩克|OPEC|原油|石油|页岩|油田|产量|供应|出口)",
    "无人机": r"(袭击|攻击|打击|军事|油田|炼厂|管道|原油|导弹)",
    "停产": r"(油田|炼厂|装置|化工|工厂|钢厂|矿山|煤矿)",
    "复产": r"(油田|炼厂|装置|化工|工厂|钢厂|矿山)",
    "扩产": r"(油田|炼厂|装置|化工|工厂|矿山)",
    "检修增多": r"(炼厂|装置|化工|工厂|PX|PTA|甲醇)",
    "制裁": r"(原油|石油|伊朗|俄罗斯|委内瑞拉|出口|石油禁运)",
    "袭击": r"(油田|炼厂|管道|油轮|军事|导弹|港口|红海|油罐)",
    "爆炸": r"(油田|炼厂|管道|油轮|化工|工厂|港口|煤矿|油罐)",
    "停火": r"(俄乌|中东|伊朗|加沙|红海|冲突|战争|地缘|以色列|哈马斯|停火协议)",
    "停战": r"(俄乌|中东|伊朗|加沙|红海|冲突|战争|地缘|以色列)",
    "和谈": r"(俄乌|中东|伊朗|加沙|红海|冲突|战争|地缘|以色列)",
    "休战": r"(俄乌|中东|伊朗|加沙|红海|冲突|战争|地缘)",
    "封锁": r"(红海|苏伊士|霍尔木兹|海峡|港口|航道|油轮)",
    "飓风": r"(墨西哥湾|美国|海湾|得州|德州|炼厂|原油|风暴|气旋|飓风)",
    "寒潮": r"(气温|用气|天然气|供暖|电力|冷空气)",
    "高温天气": r"(用电|电力|气温|空调|电网|高温)",
    "罢工": r"(矿山|铜矿|港口|码头|铝厂|锌|镍|钢厂|工会|锂矿)",
    "恢复生产": r"(油田|炼厂|装置|化工|工厂|钢厂|矿山)",
    "去库": r"(库存|炼厂|港口|社会库存|仓单)",
    "累库": r"(库存|炼厂|港口|社会库存|仓单)",
}


# 否定/落空/证伪类转折词：只在关键词或正则命中点的局部窗口内生效，避免“前文否定、后文另一件事利多”误反转
NEGATION_RE = re.compile(
    r"并未|并没有|没有|未能|难以|不会|并非|不予|不再|不再会|落空|证伪|未兑现|未能兑现|"
    r"不及预期|低于预期|差于预期|令人失望|取消|推迟|延后|延期")
NEGATION_WINDOW = 16


def _span_polarity(content, start, end, ignore_inside=False):
    """判断某个命中片段附近是否存在否定/转折。返回1=原极性，-1=反转极性。
    ignore_inside=True 时，若转折词本身就在命中片段内（如正则已显式写了“低于预期”），不重复反转。"""
    left = max(0, start - NEGATION_WINDOW)
    right = min(len(content), end + NEGATION_WINDOW)
    for m in NEGATION_RE.finditer(content, left, right):
        if ignore_inside and m.start() >= start and m.end() <= end:
            continue
        return -1
    return 1


def _lex_weight(content, cat=None):
    """计算一条新闻在指定板块(cat=None表示跨全部板块)的关键词权重和，带闸门与局部否定反转。"""
    w = 0.0
    for kw, weight, cats in LEXICON:
        if cat is not None and cats != ALL and cat not in cats:
            continue
        normal_hits, reversed_hits = 0, 0
        for m in re.finditer(re.escape(kw), content):
            gate = KEYWORD_GATES.get(kw)
            if gate and not re.search(gate, content):
                continue
            if _span_polarity(content, m.start(), m.end()) < 0:
                reversed_hits += 1
            else:
                normal_hits += 1
        if normal_hits:
            w += weight
        elif reversed_hits:
            w -= weight
    for rx, weight, cats in SPECIAL_RULES:
        if cat is not None and cats != ALL and cat not in cats:
            continue
        for m in re.finditer(rx, content):
            # 正则本身已经编码方向（如“PMI低于”），其内部的“低于预期”不能再二次反转；只处理外部否定。
            w += weight * _span_polarity(content, m.start(), m.end(), ignore_inside=True)
    return w


class NewsFactor:
    """滚动新闻缓冲池 -> 板块/品种情绪分"""

    def __init__(self, max_hours=12):
        self.items = deque(maxlen=3000)
        self._seen = deque(maxlen=5000)
        self._seen_set = set()
        self.max_hours = max_hours

    def add(self, news_list):
        """加入新闻（自动去重，按内容前50字），返回新增条数"""
        added = 0
        for n in news_list or []:
            key = (n.get("source", ""), n.get("content", "")[:50])
            if key in self._seen_set:
                continue
            self._seen.append(key)
            self._seen_set.add(key)
            self.items.append(n)
            added += 1
        # 清理超龄新闻
        cutoff = datetime.now().timestamp() - self.max_hours * 3600
        while self.items and self.items[0].get("time", datetime.now()).timestamp() < cutoff:
            self.items.popleft()
        return added

    def _item_weight(self, content, cat):
        return clip(_lex_weight(content, cat), -3.5, 3.5)

    def score(self, cat, variety=None):
        """返回 (归一化分[-4,+4], 命中列表[(得分, 新闻)])"""
        now = datetime.now()
        total = 0.0
        hits = []
        for n in self.items:
            age_h = (now - n.get("time", now)).total_seconds() / 3600.0
            if age_h < -0.1 or age_h > self.max_hours:
                continue
            content = n.get("content", "")
            w = self._item_weight(content, cat)
            if abs(w) < 0.05:
                continue
            decay = math.exp(-max(age_h, 0) / 2.5)
            s = w * decay
            if n.get("important"):
                s *= 1.6
            s *= n.get("confidence", 1.0)    # 可信度：真实源全额，存疑消息打折后排
            if variety and variety in content:
                s *= 1.5
            total += s
            hits.append((s, n))
        norm = math.tanh(total * 0.28) * 4.0
        hits.sort(key=lambda x: -abs(x[0]))
        return norm, hits[:3]

    def trend(self):
        """消息面趋势：近4小时得分 - 之前(4~12小时)得分，用于预测走向"""
        now = datetime.now()

        def window(h0, h1):
            tot = 0.0
            for n in self.items:
                age = (now - n.get("time", now)).total_seconds() / 3600.0
                if h0 <= age < h1:
                    w = self._item_weight(n.get("content", ""), None)
                    if abs(w) > 0.05:
                        tot += w * (1.6 if n.get("important") else 1.0) * n.get("confidence", 1.0)
            return math.tanh(tot * 0.28) * 4.0

        return window(0, 4) - window(4, 12)

    def top_items(self, k=8):
        """全局最有影响力的新闻（供报告展示）"""
        now = datetime.now()
        rows = []
        for n in self.items:
            age_h = (now - n.get("time", now)).total_seconds() / 3600.0
            if age_h < -0.1 or age_h > self.max_hours:
                continue
            best = _lex_weight(n.get("content", ""), cat=None)
            if abs(best) < 0.05:
                continue
            decay = math.exp(-max(age_h, 0) / 2.5)
            s = best * decay * (1.6 if n.get("important") else 1.0) * n.get("confidence", 1.0)
            rows.append((s, n))
        rows.sort(key=lambda x: -abs(x[0]))
        return rows[:k]


def oil_chain_part(oil_score, oil_w):
    """原油因子按联动权重映射到具体品种"""
    return oil_score * oil_w


# ================= WP-F1 D1：五维情绪（强度/不确定性/相关性/前瞻性/事件类型） =================
# 只做信息增量：polarity 复用既有 _lex_weight（数值口径不变），其余四维为新增刻画，
# 供消息角标展示与风控闸门使用，不回改 NewsFactor.score 的综合分。
# 强度词：刻画情绪"烈度"，不区分多空方向（暴涨/暴跌都算高强度）
INTENSITY_WORDS = (
    "大幅", "飙升", "暴涨", "暴跌", "激增", "骤降", "骤增", "剧烈", "强劲", "重挫",
    "大涨", "大跌", "涨停", "跌停", "创纪录", "历史新高", "历史新低", "超预期", "远超预期",
    "大幅上涨", "大幅下跌", "急升", "急跌", "飙升至", "崩了", "暴涨至", "放量大涨", "重挫逾",
)
# 不确定性词：消息确定性折扣（越多越应谨慎、降置信）
UNCERTAINTY_WORDS = (
    "或", "可能", "预计", "据悉", "有待", "尚不确定", "存疑", "不确定", "或将", "或于",
    "疑似", "传闻", "据称", "市场猜测", "猜测", "分歧", "不明", "待观察", "有待观察",
    "尚待", "未必", "也许", "据称是", "传言", "未证实", "风险", "担忧", "忧虑",
)
# 前瞻性词：指向未来而非已发生事实（预期/计划/将于）
FORWARD_WORDS = (
    "预计", "未来", "将于", "下周", "下月", "明年", "预期", "展望", "有望", "或将",
    "计划", "拟于", "远期", "长期看", "后续", "预计于", "年内", "季度", "财年",
    "目标价", "指引", "前瞻",
)
# 事件类型归类（按顺序取首个命中类别；关键词与 LEXICON 同源，便于和打分互相印证）
EVENT_TYPES = [
    ("货币政策", ("美联储", "降息", "加息", "降准", "缩表", "宽松", "紧缩", "鸽派", "鹰派",
                "非农", "CPI", "利率", "货币政策", "逆回购", "MLF", "LPR")),
    ("地缘", ("地缘", "冲突", "袭击", "制裁", "军事", "导弹", "战争", "停火", "油轮",
              "红海", "伊朗", "俄罗斯", "乌克兰", "以色列", "哈马斯", "罢工")),
    ("供给", ("减产", "增产", "停产", "复产", "检修", "供应", "供给", "产能", "矿山",
              "禁运", "断供", "扩产", "装置", "投产")),
    ("需求库存", ("库存", "去库", "累库", "需求", "开工", "仓单", "消费", "补库", "去化")),
    ("天气", ("干旱", "霜冻", "洪涝", "飓风", "寒潮", "天气", "拉尼娜", "厄尔尼诺", "高温",
              "台风", "降雨")),
    ("汇率股市", ("美元", "汇率", "人民币", "美股", "A股", "北向", "汇金", "指数", "纳指",
                  "道指", "黄金避险")),
    ("贸易", ("关税", "贸易", "出口", "进口", "协议", "豁免", "摩擦")),
    ("资金情绪", ("避险", "情绪", "资金", "持仓", "净多", "净空", "投机", "多头", "空头")),
]


def _distinct_hits(text, words):
    """去重命中词数：同一个词在文中出现多次只计一次，避免长文反复刷高维度值。"""
    hit = set()
    for w in words:
        if w and w in text:
            hit.add(w)
    return len(hit)


def sentiment_facets(text, variety=None, cat=None):
    """把一条新闻文本拆成五个维度（全部 0~1，polarity 除外）。

    返回 dict：
      polarity    极性分（复用 _lex_weight，板块内口径，范围约[-3.5,3.5]，正多负空）
      intensity   强度 0~1（情绪烈度，不分方向）
      uncertainty 不确定性 0~1（越高越应谨慎）
      relevance   相关性 0~1（直接点名品种=1.0，否则按板块/宏观 0.35）
      forwardness 前瞻性 0~1（指向未来的预期/计划占比感）
      event       事件类型字符串（货币政策/地缘/供给/需求库存/天气/汇率股市/贸易/资金情绪/综合）
    纯函数、零网络、零新增依赖；任何异常都退回中性维度，绝不抛给主流程。
    """
    text = text or ""
    facets = {"polarity": 0.0, "intensity": 0.0, "uncertainty": 0.0,
              "relevance": 0.35, "forwardness": 0.0, "event": "综合"}
    try:
        facets["polarity"] = clip(_lex_weight(text, cat), -3.5, 3.5)
        facets["intensity"] = round(
            math.tanh(_distinct_hits(text, INTENSITY_WORDS) * config.SENTIMENT_INTENSITY_K), 3)
        facets["uncertainty"] = round(
            math.tanh(_distinct_hits(text, UNCERTAINTY_WORDS) * config.SENTIMENT_UNCERT_K), 3)
        facets["forwardness"] = round(
            math.tanh(_distinct_hits(text, FORWARD_WORDS) * config.SENTIMENT_FORWARD_K), 3)
        if variety and (variety in text or variety.replace(" ", "") in text):
            facets["relevance"] = 1.0
        for etype, kws in EVENT_TYPES:
            if any(k in text for k in kws):
                facets["event"] = etype
                break
    except Exception:
        return {"polarity": 0.0, "intensity": 0.0, "uncertainty": 0.0,
                "relevance": 0.35, "forwardness": 0.0, "event": "综合"}
    return facets


def facet_tags(facets, min_value=None):
    """把五维情绪压成简短角标串（供消息行展示），如 '强0.8·前瞻·不确定0.6·地缘'。"""
    if not facets:
        return ""
    if min_value is None:
        min_value = config.SENTIMENT_FACET_TAG_MIN
    tags = []
    if facets.get("intensity", 0) >= min_value:
        tags.append("强%.1f" % facets["intensity"])
    if facets.get("forwardness", 0) >= min_value:
        tags.append("前瞻")
    if facets.get("uncertainty", 0) >= min_value:
        tags.append("不确定%.1f" % facets["uncertainty"])
    ev = facets.get("event")
    if ev and ev != "综合":
        tags.append(ev)
    return "·".join(tags)

