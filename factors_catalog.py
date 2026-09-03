# -*- coding: utf-8 -*-
"""G21（第36轮）特征/因子注册表（feature registry）——全项目因子的唯一登记处（纯数据、零行为、零第三方依赖）。

此前因子定义散在 analyzer.compute_indicators / fundamental_factors / cross_section / 各研究工具，
没有一处登记"因子名/公式/方向/层级/是否进综合分/现状/在哪计算/IC 档案指针"。本模块把它们统一登记，
供：①研究面板 panel_builder 按注册表决定字段与口径；②训练-服务一致性（pit_audit）核对实时/离线同名因子；
③G25 表达式引擎、G29 因子体检后续按 key 回写 IC 档案；④人工一眼看清"哪些因子真在综合分里、哪些只是影子/已归档"。

status 取值：
  live      = 进入 analyzer 综合分 parts（线上生效）
  shadow    = 只随信号落库跟踪、不进综合分（如 tsmom 长窗）
  research  = 仅研究工具截面/事件层使用、不接实时
  tracking  = 双样本边缘候选、待更长样本（如 carry 含展期口径）
  archived  = 已被双样本证伪/归档（如时序/截面动量）
本模块只登记事实、不做任何计算、不被 main 实时链路 import，故对常驻与综合分零影响。
"""

# 综合分 9 个 part 的规范顺序（必须与 config.ATTR_FACTOR_ORDER 逐字一致，由测试钉死）
PART_KEYS = ("新闻消息面", "原油联动", "机构动向", "日线动量", "技术共振",
             "分钟共振", "盘中动量", "量仓资金", "基本面")

# 每条记录：key/中文名/层级/方向(+1越大越多多,-1反之,0有符号中性)/对综合分贡献界/现状/引入轮次/实时计算处/研究档案
CATALOG = (
    # ---------- 进入综合分的 9 个 part（live） ----------
    {"key": "新闻消息面", "name": "新闻消息面", "layer": "情绪", "direction": +1,
     "bound": (-4.0, 4.0), "status": "live", "introduced": 6,
     "live_at": "factors.NewsFactor+sina_news", "archive": "factor_eval/attribution",
     "formula": "7x24快讯关键词命中+否定反转，按来源置信度/时效衰减累计，单事件有界、跨轮去重衰减"},
    {"key": "原油联动", "name": "原油联动", "layer": "跨品种", "direction": +1,
     "bound": (-5.0, 5.0), "status": "live", "introduced": 6,
     "live_at": "oil_data+analyzer(按 oil_w 传导)", "archive": "factor_eval/attribution",
     "formula": "(布伦特60%+WTI40%)的5/15/60分钟动量与较昨结涨跌，按品种联动权重 oil_w 传导，动态键名'原油联动(w=..)'"},
    {"key": "机构动向", "name": "机构动向", "layer": "情绪", "direction": +1,
     "bound": (-2.0, 2.0), "status": "live", "introduced": 6,
     "live_at": "webdata 交易可查 aireport(≥3家)", "archive": "factor_eval/attribution",
     "formula": "机构研报看多/震荡/看空家数统计映射，≥3家才计分；第35轮归因发现其次日方向为负贡献，列G29复核"},
    {"key": "日线动量", "name": "日线动量", "layer": "技术", "direction": +1,
     "bound": (-4.5, 4.5), "status": "live", "introduced": 6,
     "live_at": "futures_data.compute_indicators→analyzer", "archive": "factor_eval/attribution",
     "formula": "tanh(ret5×160)×2.5+tanh(ret20×70)×2.0+现价相对MA10偏离"},
    {"key": "技术共振", "name": "技术共振", "layer": "技术", "direction": +1,
     "bound": (-1.2, 1.2), "status": "live", "introduced": 8,
     "live_at": "futures_data.technical_profile→analyzer", "archive": "factor_eval/attribution",
     "formula": "短(MA5/5日动量/KDJ)/中(MA20/MACD)/长(MA20/MA60)三组日线指标分别投多/空/中性票，三组同向给满分"},
    {"key": "分钟共振", "name": "分钟共振", "layer": "技术", "direction": +1,
     "bound": (-0.4, 0.4), "status": "live", "introduced": 9,
     "live_at": "futures_data(30m聚合60m)→analyzer", "archive": "factor_eval",
     "formula": "30分钟短中/60分钟中长共四票的多空共振确认/背离，缺失降级为0"},
    {"key": "盘中动量", "name": "盘中动量", "layer": "技术", "direction": +1,
     "bound": (-1.5, 1.5), "status": "live", "introduced": 6,
     "live_at": "analyzer(运行期10/30分钟价差)", "archive": "factor_eval",
     "formula": "常驻运行期间 10 分钟/30 分钟价格变化，重启后样本重置"},
    {"key": "量仓资金", "name": "量仓资金", "layer": "量仓", "direction": +1,
     "bound": (-1.2, 1.2), "status": "live", "introduced": 7,
     "live_at": "flow_tracker→analyzer", "archive": "factor_eval",
     "formula": "增仓上行/增仓下行/减仓回补×放量缩量识别资金方向"},
    {"key": "基本面", "name": "基本面", "layer": "基本面", "direction": +1,
     "bound": (-1.5, 1.5), "status": "live", "introduced": 13,
     "live_at": "fundamental_factors(库存.40/龙虎.30/carry.20/基差.10)", "archive": "factor_eval",
     "formula": "库存仓单分位+周环比、龙虎榜净多率、期限年化carry、现货基差，缺项按可得权重重新归一不编造"},

    # ---------- 基本面四个子项（合成为上面的"基本面"part） ----------
    {"key": "fund_inventory", "name": "库存仓单", "layer": "基本面子项", "direction": -1,
     "bound": (-0.6, 0.6), "status": "live", "introduced": 13,
     "live_at": "fundamental_factors.inventory_factor", "archive": "fundamentals表",
     "formula": "注册仓单滚动分位(去库偏多)+周环比去化，方向-1(库存越高越空)"},
    {"key": "fund_rank", "name": "龙虎榜净多", "layer": "基本面子项", "direction": +1,
     "bound": (-0.45, 0.45), "status": "live", "introduced": 13,
     "live_at": "fundamental_factors.rank_factor", "archive": "fundamentals表",
     "formula": "前20席会员(多-空)/(多+空)净多率及其边际变化"},
    {"key": "fund_carry", "name": "期限carry子项", "layer": "基本面子项", "direction": +1,
     "bound": (-0.3, 0.3), "status": "live", "introduced": 13,
     "live_at": "fundamental_factors.carry_factor", "archive": "fundamentals表/carry_eval",
     "formula": "近月相对远月年化展期收益率，反向Back(近高远低、现货紧)偏多"},
    {"key": "fund_basis", "name": "现货基差", "layer": "基本面子项", "direction": +1,
     "bound": (-0.15, 0.15), "status": "live", "introduced": 13,
     "live_at": "fundamental_factors.basis_factor", "archive": "fundamentals表",
     "formula": "现货相对主力升水(现货坚挺)偏多，生意社源反爬时该子项缺失自动归一"},

    # ---------- 技术指标原始量（compute_indicators 输出，是上面技术类 part 的原料，本身不直接进分） ----------
    {"key": "ret5", "name": "5日收益", "layer": "技术指标", "direction": +1, "bound": None,
     "status": "live", "introduced": 6, "live_at": "futures_data.technical_profile", "archive": "research_panel",
     "formula": "close[t]/close[t-5]-1"},
    {"key": "ret20", "name": "20日收益", "layer": "技术指标", "direction": +1, "bound": None,
     "status": "live", "introduced": 6, "live_at": "futures_data.technical_profile", "archive": "research_panel",
     "formula": "close[t]/close[t-20]-1"},
    {"key": "hv20", "name": "20日历史波动", "layer": "技术指标", "direction": 0, "bound": None,
     "status": "live", "introduced": 6, "live_at": "futures_data._hv_at", "archive": "research_panel",
     "formula": "日收益样本std×√252"},
    {"key": "atr14", "name": "14日ATR", "layer": "技术指标", "direction": 0, "bound": None,
     "status": "live", "introduced": 6, "live_at": "futures_data.compute_indicators", "archive": "research_panel",
     "formula": "近14根真实波幅(TR)均值，用于止损1.2×ATR/目标2×ATR"},

    # ---------- 研究/影子因子（不进综合分） ----------
    {"key": "tsmom63/126/252", "name": "多窗口时序动量z", "layer": "研究影子", "direction": +1,
     "bound": None, "status": "shadow", "introduced": 30,
     "live_at": "futures_data.tsmom_series(影子键)", "archive": "tsmom_eval(已归档)",
     "formula": "z{L}=过去L日累计收益÷(日收益std×√252)，blend=等权tanh(z)；第30轮双样本证伪不进分"},
    {"key": "xsmom_z252", "name": "截面动量多空", "layer": "截面研究", "direction": +1,
     "bound": None, "status": "archived", "introduced": 31,
     "live_at": "无(仅xsmom_eval)", "archive": "xsmom_eval(第32轮8候选无一达标归档)",
     "formula": "调仓日跨品种按z252排序分5档多最强空最弱，市场中性；双样本弱正不达标"},
    {"key": "carry_cs", "name": "截面carry(含展期)", "layer": "截面研究", "direction": +1,
     "bound": None, "status": "tracking", "introduced": 34,
     "live_at": "无(仅carry_eval/term_history)", "archive": "carry_eval(长9.9年t2.55成立/近4.1年t1.39边缘)",
     "formula": "近月连续含roll净值的截面分档多空；赚展期roll的钱非价格方向，近窗达标前不进分"},
)

_BY_KEY = None
_VALID_STATUS = {"live", "shadow", "research", "tracking", "archived"}


def by_key(key):
    """按 key 取登记记录；动态原油键（含括号权重）先归一。找不到返回 None。"""
    global _BY_KEY
    if _BY_KEY is None:
        _BY_KEY = {r["key"]: r for r in CATALOG}
    s = str(key or "").strip()
    for cut in ("(", "（"):
        if cut in s:
            s = s.split(cut, 1)[0].strip()
    return _BY_KEY.get(s)


def part_records():
    """进入综合分的 9 个 part 记录（按 PART_KEYS 顺序）。"""
    return [by_key(k) for k in PART_KEYS]


def all_keys():
    return [r["key"] for r in CATALOG]


def validate():
    """注册表自检：返回问题字符串列表（空=通过）。key 唯一、方向合法、status 合法、part 必为 live。"""
    issues = []
    seen = set()
    for r in CATALOG:
        k = r["key"]
        if k in seen:
            issues.append("重复key:%s" % k)
        seen.add(k)
        for req in ("name", "layer", "direction", "status", "introduced", "live_at", "formula"):
            if req not in r:
                issues.append("%s 缺字段 %s" % (k, req))
        if r.get("direction") not in (-1, 0, +1):
            issues.append("%s direction 非法" % k)
        if r.get("status") not in _VALID_STATUS:
            issues.append("%s status 非法:%s" % (k, r.get("status")))
        b = r.get("bound")
        if b is not None and (not isinstance(b, (list, tuple)) or len(b) != 2 or b[0] > b[1]):
            issues.append("%s bound 非法" % k)
    for k in PART_KEYS:
        rec = by_key(k)
        if rec is None:
            issues.append("综合分part缺登记:%s" % k)
        elif rec["status"] != "live":
            issues.append("综合分part %s 状态应为live" % k)
    return issues


def catalog_text():
    """生成人类可读的因子清单文本（供报告/面板 manifest）。"""
    L = ["特征注册表 factors_catalog（%d 条；live=进综合分 / shadow=影子 / research=研究 / tracking=待样本 / archived=证伪归档）"
         % len(CATALOG)]
    layer_name = {"综合分part": ""}
    for r in CATALOG:
        b = ("[%+.1f,%+.1f]" % r["bound"]) if r.get("bound") else "[—]"
        L.append("  %-16s %-8s 方向%+d 界%-12s %-8s 第%02d轮 @%s"
                 % (r["key"], r["layer"], r["direction"], b, r["status"],
                    r["introduced"], r["live_at"]))
    return "\n".join(L) + "\n"


def selftest():
    issues = validate()
    assert not issues, issues
    # 9 个综合分 part 齐全且顺序固定
    assert len(PART_KEYS) == 9 and len(part_records()) == 9
    assert all(r["status"] == "live" for r in part_records())
    # 动态原油键归一
    assert by_key("原油联动(w=0.50)")["key"] == "原油联动"
    assert by_key("不存在的因子") is None
    # 现状语义：动量已归档、carry 待跟踪
    assert by_key("xsmom_z252")["status"] == "archived"
    assert by_key("carry_cs")["status"] == "tracking"
    txt = catalog_text()
    assert "新闻消息面" in txt and "archived" in txt
    print("factors_catalog selftest ALL PASS（%d条登记/9个综合分part齐全/字段·方向·状态合法/动态键归一/现状语义）"
          % len(CATALOG))
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
