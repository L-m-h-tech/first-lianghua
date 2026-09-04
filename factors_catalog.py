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
    # ===== G25（第38轮）表达式引擎承载的新研究因子：同一条表达式实时/离线同引擎求值，默认 research 不进综合分 =====
    {"key": "expr_ma_bias5", "name": "5日价格动量(表达式版)", "layer": "表达式研究", "direction": +1,
     "bound": None, "status": "research", "introduced": 38,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "expr_research",
     "formula": "delta(close,5)/delay(close,5)；等价ret5，作实时/离线parity基准"},
    {"key": "expr_ma_ratio", "name": "短长均线比", "layer": "表达式研究", "direction": +1,
     "bound": None, "status": "research", "introduced": 38,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "expr_research",
     "formula": "ts_mean(close,5)/ts_mean(close,20)-1"},
    {"key": "expr_trend_per_vol", "name": "单位波动趋势", "layer": "表达式研究", "direction": +1,
     "bound": None, "status": "research", "introduced": 38,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "expr_research",
     "formula": "(close/ts_mean(close,20)-1)/(ts_std(close,20)+1e-6)，风险调整动量"},
    {"key": "expr_price_accel", "name": "价格二阶加速度", "layer": "表达式研究", "direction": +1,
     "bound": None, "status": "research", "introduced": 38,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "expr_research",
     "formula": "delta(delta(close,5),5)/delay(close,10)，嵌套时序算子"},
    {"key": "expr_illiq", "name": "非流动性代理", "layer": "表达式研究", "direction": -1,
     "bound": None, "status": "research", "introduced": 38,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "expr_research",
     "formula": "abs(delta(close,1)/delay(close,1))/(volume+1)，Amihud|收益|/量的无量纲代理"},
    # ===== G25续（第59轮）旧技术因子过程式→表达式：ret 按过程式同运算序书写以逐字节镜像；SMA 末位容差；日线动量用 tanh 声明式复刻 =====
    {"key": "expr_ret5_exact", "name": "5日收益(过程式逐字节镜像)", "layer": "表达式研究", "direction": +1,
     "bound": None, "status": "research", "introduced": 59,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "factor_legacy_expr",
     "formula": "close/delay(close,5)-1，与 futures_data.technical_profile.ret5 同运算序、float.hex 逐位相等"},
    {"key": "expr_ret20_exact", "name": "20日收益(过程式逐字节镜像)", "layer": "表达式研究", "direction": +1,
     "bound": None, "status": "research", "introduced": 59,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "factor_legacy_expr",
     "formula": "close/delay(close,20)-1，与 ret20 同运算序逐位相等"},
    {"key": "expr_ma10", "name": "10日均线(表达式版)", "layer": "表达式研究", "direction": 0,
     "bound": None, "status": "research", "introduced": 59,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "factor_legacy_expr",
     "formula": "ts_mean(close,10)，对应增量式 _sma_series，窗内求和与累加仅末位1e-15级差异"},
    {"key": "expr_part_momentum_decl", "name": "日线动量part(声明式复刻)", "layer": "表达式研究", "direction": +1,
     "bound": None, "status": "research", "introduced": 59,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "factor_legacy_expr",
     "formula": "tanh(ret5*160)*2.5+tanh(ret20*70)*2.0+tanh(price/ma10-1)*220，输入已算标量，逐位复刻 analyzer 日线动量part（不切主链）"},
    # ===== G25续（第60轮）：更多过程式技术量同运算序表达式化（ma5/20/60 末位容差；boll_std/hv20 逐字节） =====
    {"key": "expr_ma5", "name": "5日均线(表达式版)", "layer": "表达式研究", "direction": 0,
     "bound": None, "status": "research", "introduced": 60,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "factor_legacy_expr",
     "formula": "ts_mean(close,5)，对应增量式 _sma_series，仅末位1e-15级差异"},
    {"key": "expr_ma20", "name": "20日均线(表达式版)", "layer": "表达式研究", "direction": 0,
     "bound": None, "status": "research", "introduced": 60,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "factor_legacy_expr",
     "formula": "ts_mean(close,20)，布林中轨同源，与增量SMA仅末位差异"},
    {"key": "expr_ma60", "name": "60日均线(表达式版)", "layer": "表达式研究", "direction": 0,
     "bound": None, "status": "research", "introduced": 60,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "factor_legacy_expr",
     "formula": "ts_mean(close,60)=TECH_LONG_MA，与增量SMA仅末位差异"},
    {"key": "expr_boll_std20", "name": "20日样本标准差(表达式版)", "layer": "表达式研究", "direction": 0,
     "bound": None, "status": "research", "introduced": 60,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "factor_legacy_expr",
     "formula": "ts_std(close,20)，与 _sample_std(close[-20:]) 同求和序、float.hex 逐位相等"},
    {"key": "expr_hv20", "name": "20日历史波动率年化(表达式版)", "layer": "表达式研究", "direction": 0,
     "bound": None, "status": "research", "introduced": 60,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "factor_legacy_expr",
     "formula": "ts_std(log(close/delay(close,1)),20)*sqrt252，与 _hv_at(.,20) 同运算序逐位相等"},
    # ===== G25续（第61轮）状态量 MACD/RSI 表达式化（ts_ema/ts_rma 状态递推算子） =====
    {"key": "expr_macd_dif", "name": "MACD-DIF(表达式版)", "layer": "表达式研究", "direction": 0,
     "bound": None, "status": "research", "introduced": 61,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "factor_legacy_expr",
     "formula": "ts_ema(close,12)-ts_ema(close,26)，SMA播种，与 technical_profile dif 逐位相等"},
    {"key": "expr_macd_dea", "name": "MACD-DEA(表达式版)", "layer": "表达式研究", "direction": 0,
     "bound": None, "status": "research", "introduced": 61,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "factor_legacy_expr",
     "formula": "ts_ema(DIF,9) 嵌套状态递推，对DIF连续子序列再EMA，与 dea 逐位相等"},
    {"key": "expr_macd_hist", "name": "MACD柱(表达式版)", "layer": "表达式研究", "direction": 0,
     "bound": None, "status": "research", "introduced": 61,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "factor_legacy_expr",
     "formula": "(DIF-DEA)*2，与 macd_hist 逐位相等"},
    {"key": "expr_rsi14", "name": "Wilder RSI14(表达式版)", "layer": "表达式研究", "direction": 0,
     "bound": None, "status": "research", "introduced": 61,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "factor_legacy_expr",
     "formula": "100-100/(1+ts_rma(涨,14)/ts_rma(跌,14))，非平盘逐位；avg_loss≈0强制100分支差异已钉死"},
    # ===== 第63轮 G25续：EMA 列 + KDJ 表达式化（KDJ 非 close-only，输入带 high/low） =====
    {"key": "expr_ema12", "name": "12日EMA(表达式版)", "layer": "表达式研究", "direction": 0,
     "bound": None, "status": "research", "introduced": 63,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "factor_legacy_expr",
     "formula": "ts_ema(close,12)，SMA播种，MACD快线，与 _ema_series(close,12) 逐位相等"},
    {"key": "expr_ema26", "name": "26日EMA(表达式版)", "layer": "表达式研究", "direction": 0,
     "bound": None, "status": "research", "introduced": 63,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "factor_legacy_expr",
     "formula": "ts_ema(close,26)，SMA播种，MACD慢线，与 _ema_series(close,26) 逐位相等"},
    {"key": "expr_kdj_k", "name": "KDJ-K(表达式版)", "layer": "表达式研究", "direction": 0,
     "bound": None, "status": "research", "introduced": 63,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "factor_legacy_expr",
     "formula": "kdj_sm(kdj_rsv(high,low,close,9),9)，固定初值50、α=1/3，与 _kdj_series K 逐位相等"},
    {"key": "expr_kdj_d", "name": "KDJ-D(表达式版)", "layer": "表达式研究", "direction": 0,
     "bound": None, "status": "research", "introduced": 63,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "factor_legacy_expr",
     "formula": "对K再套一次同系数平滑(当拍新K)，与 _kdj_series D 逐位相等"},
    {"key": "expr_kdj_j", "name": "KDJ-J(表达式版)", "layer": "表达式研究", "direction": 0,
     "bound": None, "status": "research", "introduced": 63,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "factor_legacy_expr",
     "formula": "3K-2D，与 _kdj_series J 逐位相等"},
    # ===== 第64轮 G25续：ATR14 表达式化（TR 非 close-only，吃 high/low/前收；嵌套 max 二元） =====
    {"key": "expr_atr14", "name": "14日ATR(表达式版)", "layer": "表达式研究", "direction": 0,
     "bound": None, "status": "research", "introduced": 64,
     "live_at": "factor_expr(研究,不进综合分)", "archive": "factor_legacy_expr",
     "formula": "ts_mean(max(max(h-l,|h-prev_c|),|l-prev_c|),14)，与 compute_indicators 的 TR/ATR 同求和序逐位相等"},
)

_BY_KEY = None
_VALID_STATUS = {"live", "shadow", "research", "tracking", "archived"}

# G29（第37轮）因子体检最近一次真实快照（tools/factor_health.py 产出 reports/factor_health.txt/.json；
# 这里只登记结论、不做计算；重跑后人工/脚本刷新，key 必须是已登记因子，由 validate 钉死）。
# 主周期=次日1440分钟；verdict：健康 / 健康(反向)=稳定非零但方向为负(反转信号) / 走弱·不稳定 / 失效预警 / 样本不足。
HEALTH_SNAPSHOT = {
    "asof": "2026-09-03", "tool": "tools/factor_health.py", "horizon_min": 1440,
    "method": "信号part×方向 对 方向收益 RankIC；块长20自助500次CI；滚动60/步20、连续3窗失效预警",
    "cards": {
        "新闻消息面": {"ic": +0.147, "ci": [-0.135, +0.302], "verdict": "失效预警", "note": "次日点估正但滚动窗连续翻转、CI跨零，不稳"},
        "原油联动": {"ic": +0.276, "ci": [+0.045, +0.407], "verdict": "健康", "note": "次日CI不跨零、同号率0.98，9.3起样本转正"},
        "机构动向": {"ic": -0.230, "ci": [-0.341, -0.118], "verdict": "健康(反向)",
                    "note": "次日稳定显著为负(同号率1.00)，与第35轮t=-2.77互证：当前方向化口径下是反转信号而非确认，多头侧ic=-0.297"},
        "日线动量": {"ic": +0.043, "ci": [-0.135, +0.273], "verdict": "失效预警", "note": "次日近零且滚动连续翻转"},
        "技术共振": {"ic": +0.228, "ci": [-0.038, +0.461], "verdict": "走弱/不稳定", "note": "点估正、CI下界微跨零，需更长样本"},
        "分钟共振": {"ic": +0.017, "ci": [-0.069, +0.259], "verdict": "走弱/不稳定", "note": "次日近零"},
        "盘中动量": {"ic": 0.0, "ci": None, "verdict": "样本不足", "note": "重启即重置，n=9"},
        "量仓资金": {"ic": 0.0, "ci": None, "verdict": "样本不足", "note": "n=9"},
        "基本面": {"ic": +0.109, "ci": [-0.127, +0.226], "verdict": "走弱/不稳定",
                  "note": "次日跨零；但2小时周期ic=+0.123、CI[+0.045,+0.233]健康，短周期更有效"},
    },
    "daily_layer": "G21面板日频因子(ret5..tsmom_blend)对未来1~60交易日的池化RankIC绝对值均<0.10、且多数随H变号不构成单调衰减，"
                   "说明单独日频回看收益在4年池化样本上无稳定横截面预测力（与tsmom/xsmom双样本证伪一致）",
}


def get_health(key):
    """取某因子最近体检卡；无快照返回 None。"""
    return HEALTH_SNAPSHOT["cards"].get(str(key or "").strip())


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
    # G29 体检卡只能回写到已登记因子
    for k in HEALTH_SNAPSHOT["cards"]:
        if by_key(k) is None:
            issues.append("体检卡引用了未登记因子:%s" % k)
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
    # G29 体检快照：9 part 均有卡、机构动向次日反向结论被锁定
    assert set(HEALTH_SNAPSHOT["cards"]) == set(PART_KEYS)
    assert get_health("机构动向")["verdict"] == "健康(反向)" and get_health("机构动向")["ic"] < 0
    print("factors_catalog selftest ALL PASS（%d条登记/9个综合分part齐全/字段·方向·状态合法/动态键归一/现状语义/G29体检卡回写）"
          % len(CATALOG))
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
