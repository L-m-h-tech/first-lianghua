# -*- coding: utf-8 -*-
"""
全局配置文件：所有可调参数集中在这里，改完保存重启程序即可生效。

【需求功能对照】
  需求①  新闻60s/原油10s刷新      -> NEWS_INTERVAL / OIL_INTERVAL
  需求⑤  四大交易所全品种+对应期权 -> ANALYZE_EXCHANGES / VARIETIES / OPTION_VARIETIES
  需求⑩  轮动节奏(时段前30分钟每5分钟、之后每20分钟)与复盘 -> SESSIONS / SESSION_EARLY_*
         / SESSION_INTERVAL / DAILY_REVIEW_FILE / REALTIME_HTML / KEEP_ROUNDS
  增强⑪  原油急动紧急轮动         -> OIL_JUMP_WINDOW_SEC / OIL_JUMP_REL / OIL_JUMP_COOLDOWN_SEC
  增强⑫  全网数据查找(3分钟)      -> WEB_SCAN_* / WEB_IMPACT_TRIGGER / WEB_DOUBTFUL_WORDS 等
  需求④⑧⑨ 各报告文件路径与时段分流 -> 输出段 REPORT_FILE/OFFHOURS_*/DAILY_REVIEW_FILE
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# G10 配置外置：最先加载 .env（key/token/webhook 只走环境变量），必须早于下方任何 os.environ.get。
# 真实环境变量优先；.env 不存在/为空时零影响。路径可用环境变量 FUTURES_MONITOR_ENV 指定。
from config_loader import load_dotenv as _load_dotenv
_ENV_PATH = os.environ.get("FUTURES_MONITOR_ENV", os.path.join(BASE_DIR, ".env"))
_load_dotenv(_ENV_PATH)

# ---------------- 调度间隔 ----------------
NEWS_INTERVAL = 60            # 新闻/消息刷新间隔（秒）—— 题目要求60s
OIL_INTERVAL = 10             # 布伦特/纽约原油行情刷新间隔（秒）—— 题目要求10s
REPORT_INTERVAL = 60          # 非交易时段主分析周期（秒）
KLINE_TTL = 30 * 60           # 日线指标（HV/ATR等）缓存时长（秒）
INTRADAY_KLINE_TTL = 10 * 60  # 30/60分钟共振指标缓存时长（秒）
INTRADAY_WORKERS = 6          # 每轮并发预热30分钟K线的线程数（标准库线程池）
CONTRACT_TTL = 30 * 60        # 主力合约月份探测缓存时长（秒）
CONTRACT_CANDIDATES = 8       # 探测主力月份时往后枚举的月份数
# 交易时段（分钟轴：0点起算的分钟数；夜盘跨日，结束分钟 >1440 表示次日凌晨）：
#   日盘两段 09:00-11:30 / 13:30-15:00（全部品种一致）
#   夜盘 21:00 统一开盘，结束时间按品种分三档 23:00 / 次日01:00 / 次日02:30（见下方分档集合），
#   全局调度/报告分流按"最晚收市档 02:30"判定——只要还有品种在交易，就按交易时段节奏轮动。
# 元素为 (开始分钟, 结束分钟)；utils 调度以真实 datetime 计算（支持跨零点与周六凌晨），
# 本表同时注入看板 JS，JS 端对凌晨时刻把分钟轴 +1440 后比较。
SESSIONS = [(9 * 60, 11 * 60 + 30),
            (13 * 60 + 30, 15 * 60),
            (21 * 60, 24 * 60 + 2 * 60 + 30)]     # 21:00 - 次日02:30（全局最晚收市）
SESSION_EARLY_MINUTES = 30    # 时段开头按"快速轮动"处理的前多少分钟
SESSION_EARLY_INTERVAL = 300  # 5分钟
SESSION_INTERVAL = 1200       # 20分钟
THS_LAUNCH_WAIT = 45          # 期货通启动后等待窗口出现的最长秒数

# ---------------- 夜盘分档（结束时间为"分钟轴"，跨日 >1440；来源：上期所/INE/大商所/郑商所官网交易时间） ----------------
NIGHT_END_2300 = 23 * 60              # 1380 多数品种夜盘到 23:00
NIGHT_END_0100 = 24 * 60 + 1 * 60     # 1500 有色/不锈钢/国际铜/氧化铝到次日01:00
NIGHT_END_0230 = 24 * 60 + 2 * 60 + 30  # 1590 黄金/白银/原油到次日02:30
NIGHT_START_MIN = 21 * 60             # 夜盘统一 21:00 开盘
# 到次日 01:00 的品种（sym）：上期所铜铝锌铅镍锡不锈钢 + INE国际铜 + 上期所氧化铝
NIGHT_END_0100_SYMS = {"CU", "AL", "ZN", "PB", "NI", "SN", "SS", "BC", "AO"}
# 到次日 02:30 的品种（sym）：黄金、白银、原油
NIGHT_END_0230_SYMS = {"AU", "AG", "SC"}
# 完全无夜盘（仅日盘）的品种（sym）：
#   大商所 鸡蛋/生猪/原木；郑商所 苹果/红枣/花生/硅铁/锰硅/尿素；
#   广期所全部品种（工业硅/碳酸锂/多晶硅，广期所暂无夜盘）；INE 集运欧线
NO_NIGHT_SYMS = {"JD", "LH", "LG", "AP", "CJ", "PK", "SF", "SM", "UR",
                 "SI", "LC", "PS", "EC"}


def night_end_min(sym):
    """某品种夜盘结束的分钟轴（21:00=1260 起，跨日 >1440）；无夜盘返回 None"""
    if sym in NO_NIGHT_SYMS:
        return None
    if sym in NIGHT_END_0230_SYMS:
        return NIGHT_END_0230
    if sym in NIGHT_END_0100_SYMS:
        return NIGHT_END_0100
    return NIGHT_END_2300


# ---------------- 交易日历（法定节假日休市/调休，P0-3；实现见 trade_calendar.py） ----------------
# 动态交易日来自东方财富指数日K（免key、稳定），静态法定休市表内置在 trade_calendar.py
TRADE_CAL_CACHE = os.path.join(BASE_DIR, "cache", "trade_dates.txt")  # 每行一个 YYYY-MM-DD

# ---------------- 看门狗 / 心跳（主线程卡死时强制退出，由 start_monitor.bat 自动重启，P0-6） ----------------
HEARTBEAT_FILE = os.path.join(BASE_DIR, "logs", "heartbeat.txt")
HEARTBEAT_TIMEOUT_SEC = 600    # 主循环超过该秒数没更新心跳即判定卡死，os._exit 由 bat 拉起
WATCHDOG_CHECK_SEC = 30

# ---------------- 日志轮转（monitor.log 不再无限增长，P0-4） ----------------
LOG_MAX_BYTES = 2 * 1024 * 1024   # 单个日志文件 2MB
LOG_BACKUP_COUNT = 5              # 保留 monitor.log.1 ~ .5

# ---------------- P1：结构化数据库 / 信号效果追踪 ----------------
DATA_DIR = os.path.join(BASE_DIR, "data")
MONITOR_DB = os.path.join(DATA_DIR, "monitor.db")   # 标准库 sqlite3，零新增依赖
DB_RETENTION_DAYS = 180          # 行情/新闻明细保留天数；信号与复盘结果长期保留
SIGNAL_TRACKING_FILE = os.path.join(BASE_DIR, "reports", "signal_tracking.txt")
# 信号发出后自动回看的周期：30分钟 / 2小时 / 次日（约24小时）
SIGNAL_OUTCOME_HORIZONS = (30, 120, 1440)
SIGNAL_OUTCOME_MAX_WAIT_SEC = 6 * 3600   # 到期后若长期无新成交价，最多等待多久后记为过期
SIGNAL_TRACK_STAT_DAYS = 7               # 看板/复盘默认统计最近7天已到期信号

# ---------------- P1：量仓资金因子（成交量/持仓量已由行情接口解析，此前未参与打分） ----------------
FLOW_MAX_SCORE = 1.2             # 量仓因子对综合分的最大贡献
FLOW_OI_K = 80.0                 # 持仓变化率的 tanh 灵敏度
FLOW_VOLUME_STRONG = 1.2         # 本轮增量成交量 > 近几轮均值×1.2 视为放量
FLOW_VOLUME_WEAK = 0.6           # < 均值×0.6 视为缩量
FLOW_HISTORY_LEN = 20            # 每品种保留最近20轮快照

# ---------------- P1：主动告警（本机声音 + 可选 Webhook，均带冷却） ----------------
ALERT_SOUND_ENABLED = os.environ.get("FUTURES_MONITOR_SOUND", "1") != "0"  # 设环境变量=0可静音
ALERT_WEBHOOK_URL = os.environ.get("FUTURES_MONITOR_WEBHOOK", "")  # 飞书/钉钉/企业微信/Server酱/通用JSON
ALERT_WEBHOOK_TYPE = "auto"      # auto / feishu / dingtalk / wecom / serverchan / generic
ALERT_WEBHOOK_TIMEOUT = 5
ALERT_EMERGENCY_COOLDOWN_SEC = 300
ALERT_SIGNAL_COOLDOWN_SEC = 1800
ALERT_MID_CROSS_ENABLED = True   # 跨4分（分批建仓档）是否提醒；强信号6.5始终提醒
ALERT_MID_SCORE = 4.0            # 分批建仓档跨档阈值
ALERT_STRONG_SCORE = 6.5         # 强信号阈值
ALERT_OPTION_STRATEGY = True     # 新出现“六项全过”的期权策略时提醒

# ---------------- 原油急动紧急轮动（原油10s行情触发，跳过等待立即出一轮报告） ----------------
OIL_JUMP_WINDOW_SEC = 60      # 与多少秒前的原油价格比较（短时波动窗口）
OIL_JUMP_REL = 0.006          # 布伦特/WTI 任一在窗口内涨跌幅绝对值≥0.6%即判定"变化过大"
OIL_JUMP_COOLDOWN_SEC = 180   # 两次紧急轮动的最小间隔（秒），防止剧烈行情中连续触发

# ---------------- 全网扫描（新闻 / 金融 / 突发事件，多源聚合，每3分钟） ----------------
WEB_SCAN_INTERVAL = 180       # 全网数据查找刷新间隔（秒）—— 题目要求3分钟
WEB_SCAN_PAGE_SIZE = 30       # 每个文字源每次拉取的条数
WEB_IMPACT_TRIGGER = 1.5      # 新消息"全局影响权重"≥该值 -> 触发紧急轮动（插队，计划点不推移）
WEB_IMPORTANT_TRIGGER = 1.0   # 源标记"重要/重磅"或含突发词的消息，触发门槛降到该值
# 突发事件词：命中且影响权重达标即视为突发事件
WEB_BREAKING_WORDS = ("突发", "重磅", "紧急", "黑天鹅", "战争", "袭击", "爆炸", "地震",
                      "政变", "违约", "爆雷", "军事冲突", "供应中断", "断供", "禁运")
# 存疑标记词：命中即判定为"存疑决定因素"，可信度打折并在报告中标注，排序往后排
WEB_DOUBTFUL_WORDS = ("传闻", "据传", "据称", "网传", "市场传言", "市场传闻", "未经证实",
                      "有待证实", "疑似", "消息人士称", "有消息称", "猜测", "尚不确定",
                      "未获证实", "或将如此")
# 金融数据：全球指数/美元/金银/美股 在扫描间隔内急变达到阈值时，合成一条"实测金融消息"进入同一分析管线
WEB_MACRO_THRESHOLDS = {"美元指数": 0.004, "纽约黄金": 0.006, "纽约白银": 0.008,
                        "美铜": 0.007, "上证指数": 0.010, "深证成指": 0.012,
                        "纳斯达克": 0.015, "道琼斯": 0.015, "标普500": 0.015}
WEB_MACRO_ALERT_COOLDOWN = 300  # 同一金融指标两次合成消息的最小间隔（秒）

# ---------------- 分析范围 ----------------
# 四大交易所全部品种（INE为上期能源，原油SC等归属此处，通常与上期所同页显示；不需要可删）
ANALYZE_EXCHANGES = ["SHFE", "INE", "DCE", "CZCE", "GFEX"]
EXCHANGE_ORDER = ["SHFE", "INE", "DCE", "CZCE", "GFEX"]
EXCHANGE_NAMES = {"SHFE": "上期所", "INE": "上期能源", "DCE": "大商所",
                  "CZCE": "郑商所", "GFEX": "广期所"}

# ---------------- 网络请求 ----------------
TIMEOUT = 10
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS_SINA = {           # 新浪行情接口必须带 Referer，否则被拒绝
    "User-Agent": _UA,
    "Referer": "https://finance.sina.com.cn/",
}
HEADERS_COMMON = {"User-Agent": _UA}

# ---------------- 同花顺期货通 ----------------
THS_EXE = r"C:\同花顺期货通\bin\happ.exe"   # 已检测到的本机安装路径
THS_AUTO_LAUNCH = True                      # 每次启动程序时自动打开期货通

# ---------------- 期货分析阈值（综合分范围 -10 ~ +10） ----------------
SCORE_NEUTRAL = 2.0    # |分| < 2        -> 观望
SCORE_LIGHT = 4.0      # 2 <= |分| < 4   -> 偏多/偏空，轻仓试探
SCORE_MID = 6.5        # 4 <= |分| < 6.5 -> 看多/看空，分批建仓；>= 6.5 强信号

# ---------------- 技术指标与多周期共振（P1第三批） ----------------
TECH_RESONANCE_MAX = 1.2       # 短/中/长三周期共振对综合分的最大贡献
TECH_RSI_PERIOD = 14
TECH_RSI_OVERBOUGHT = 80.0
TECH_RSI_OVERSOLD = 20.0
TECH_MACD_FAST = 12
TECH_MACD_SLOW = 26
TECH_MACD_SIGNAL = 9
TECH_KDJ_PERIOD = 9
TECH_BOLL_PERIOD = 20
TECH_BOLL_STD = 2.0
TECH_LONG_MA = 60
TECH_VOL_PERCENTILE_MIN = 30   # HV滚动样本少于该值时，不把历史分位作为否决项
INTRADAY_RESONANCE_MAX = 0.4   # 30/60分钟共振对综合分的最大贡献，只做日线方向的确认/背离
INTRADAY_30M_BARS = 240        # 30分钟指标最多使用的K线数（约10个交易日）
INTRADAY_60M_MIN_BARS = 60     # 60分钟聚合指标最少K线数

# ---------------- 期权分析（比期货更严格） ----------------
OPT_SCORE_MIN = 5.0          # 期权要求标的综合分更高（期货只需2.0）
OPT_IV_HV_RATIO_MAX = 1.35   # 估计隐波/历史波动率 超过该值说明权利金太贵，不做买方
OPT_MIN_DAYS = 14            # 买入期权距到期最少天数（避免临近到期的Gamma/Theta风险）
OPT_ASSUMED_DAYS = 35        # 探测不到合约月份时的兜底假设剩余天数
OPT_EXPECT_COVER = 1.5       # 预期行情幅度需覆盖 1.5 倍平值权利金（时间价值）
OPT_DELTA_BAND = (0.35, 0.60)  # 建议买入合约的 |Delta| 区间
OPT_THETA_DAY_MAX = 0.03     # 每天时间价值损耗占权利金比例上限 3%
OPT_IV_PCT_BUY_MAX = 0.75    # 裸买/跨式：真实IV或HV代理分位高于75%视为偏贵
OPT_IV_PCT_SPREAD_MAX = 0.85 # 价差结构可容忍更高IV分位
OPT_IV_PCT_SELL_FLOOR = 0.55 # 卖方结构偏好IV分位高于55%
OPT_VOL_CONE_PCT_HIGH = 0.90
OPT_VOL_CONE_PCT_LOW = 0.10
# 卖方/组合保证金的保守点值估算（不是交易所精确公式；无合约乘数时只输出“点”，实盘以期货公司为准）
OPT_SELLER_MARGIN_RATE = 0.10
OPT_SELLER_MARGIN_MIN_RATE = 0.05
OPT_FUTURES_MARGIN_RATE = 0.12

# ---------------- 期权完整链 / PCR（第11轮，新浪商品期权T型报价，零新增依赖） ----------------
# 接口实测（2026-09-01，五大所57个期权品种全部通过）：
#   OptionService.getOptionData?type=futures&product={p}&exchange={ex}&pinzhong={sym小写+4位年月}
#   SHFE/INE/DCE/GFEX: product=sym小写+"_o"（cu_o/sc_o/m_o/si_o）；CZCE: product=sym小写无后缀（ma）
#   每腿：[买量,买价,最新价,卖价,卖量,持仓量,涨跌%,(行权价,仅部分交易所),合约代码]
OPTION_CHAIN_TTL = 30 * 60     # 期权链缓存时长（秒），与期权日历/合约月份同节奏
OPTION_CHAIN_WORKERS = 6       # 一轮分析前并发拉取期权链的线程数
OPTION_CHAIN_TIMEOUT = 10
# 持仓量PCR情绪参考区间（只做呈现与极值提示，不单独构成交易结论）
PCR_LOW = 0.7                  # <0.7 看涨持仓占优，情绪偏乐观
PCR_HIGH = 1.2                 # >1.2 看跌/对冲持仓占优，情绪偏谨慎
PCR_EXTREME_LOW = 0.5
PCR_EXTREME_HIGH = 1.5
PCR_LOOKBACK_DAYS = 30         # PCR历史分位回看天数（依赖option_chains表积累）
# 期限结构（全月份合约组装，零新增数据源；第11轮只展示不进综合分，基本面因子在后续轮次接入）
TERM_MIN_MONTHS = 2            # 至少2个有效月份合约才输出期限结构
TERM_ANNUAL_DAYS = 365.0

# ---------------- IV曲面 / 日历价差（第12轮 WP-B，T链权利金反推，零新增依赖） ----------------
# 原料=第11轮 option_chain 的多到期日T型链（每腿买/卖/最新价+行权价+持仓量），
# 用项目已有 Black-76 二分反推每腿IV，组装 微笑/skew + ATM IV期限结构 + 曲面矩阵。
IV_SURFACE_EXPIRIES = 3        # 每品种参与曲面的最多到期月份数（按到期升序取最近N个真实挂牌月份）
IV_SURFACE_MIN_DAYS = 30       # 剩余天数低于该值的月份不参与反推（最后30天时间价值小、报价误差被放大；按真实到期日过滤）
IV_SURFACE_MIN_STRIKES = 5     # 单到期日至少多少个可反推行权价才输出微笑/曲面
IV_MAX_SPREAD_RATIO = 0.15     # 买卖价差/中间价超过15%视为脏报价：不用中间价、回退最新价并标低质量
IV_SURFACE_IV_CAP = 2.5        # 单腿反推IV超过250%判为脏价格丢弃（真实商品期权罕见，多为错价/陈旧价）
IV_OUTLIER_RATIO = (0.2, 3.5)  # 微笑点IV相对ATM合理倍数带[0.2x,3.5x]，带外视为离群(深虚值小价格误差)，不参与矩阵/25Δ
IV_SURFACE_NEED_CLEAN_ATM = True  # ATM必须由窄价差高质量腿构成，否则该到期日不输出曲面（宁缺毋滥）
IV_BISECT_ITERS = 40           # IV二分反推迭代次数
IV_BISECT_LO = 1e-4            # IV反推下界（年化）
IV_BISECT_HI = 5.0             # IV反推上界（年化500%，超出判为坏价格不反推、不插值）
IV_MONEYNESS_GRID = (0.90, 0.95, 1.00, 1.05, 1.10)  # 曲面矩阵列：K/F 档位
IV_RR25_TARGET = 0.25          # 25Δ风险反转/蝶式的目标delta
IV_PARITY_DIFF = 0.03          # 同一行权价 call/put 反推IV差超过3个vol点时标注（流动性/报价问题）
IV_CROSS_CHECK_DIFF = 0.02     # 页面真实IV与T链反推ATM IV差超过2个vol点时标注"不一致"（不否决）
# 日历价差（同行权价、跨到期月）
IV_CALENDAR_MIN_DIFF = 0.03    # 近-远月ATM IV差≥3个vol点（期限结构显著倾斜）才推荐日历价差
IV_CALENDAR_MAX_MONTH_GAP = 2  # 日历价差两腿最大月份跨度（1~2个月）
IV_CALENDAR_NEAR_MIN_DAYS = 30 # 日历近月最少剩余天数（与曲面门槛一致，规避最后30天Gamma/价格敏感）
IV_CAL_MARGIN_RATE = 0.10      # 反向日历（含裸腿）保守保证金点值率

# ---------------- 基本面因子（第13轮 WP-C：库存仓单 + 龙虎榜 + 期限carry + 基差，东财直连零依赖） ----------------
# 实测（2026-09-01）：东财 datacenter 库存时序 RPT_FUTU_STOCKDATA 与龙虎榜 RPT_FUTU_DAILYPOSITION 稳定返回干净JSON；
#   生意社基差 sf 页有 JS-cookie 反爬且不稳定，按"尽力抓取、识别反爬即降级为None"处理，不编造。
FUND_MAX_SCORE = 1.5           # 基本面因子对综合分的最大贡献（与量仓1.2同量级）
FUND_REFRESH_HOUR = 16         # 收盘后几点刷新日频基本面（东财库存/龙虎榜约15:30后出齐）
FUND_RETRY_SEC = 3600          # 全量刷新失败后的重试间隔（秒）
FUND_INV_WEIGHT = 0.40         # 库存/仓单子权重（去库+低分位偏多）
FUND_RANK_WEIGHT = 0.30        # 龙虎榜子权重（前20席净多率+边际变化）
FUND_CARRY_WEIGHT = 0.20       # 期限结构carry子权重（Back近高远低=现货紧=偏多，零新增请求，复用第11轮term）
FUND_BASIS_WEIGHT = 0.10       # 基差子权重（现货升水偏多；生意社反爬缺失时自动按可得子项归一化）
FUND_INV_MIN_SAMPLES = 15      # 库存时序最少样本数，不足不给库存分（东财免费窗口约3个月，宁缺毋滥）
FUND_INV_WOW_DAYS = 5          # 库存周环比间隔（约5个交易日）
FUND_INV_WOW_K = 0.10          # 库存周环比 tanh 灵敏度（10%的周变化即接近饱和）
FUND_RANK_NET_K = 6.0          # 前20席净多率 tanh 灵敏度（净多率约16%即接近饱和）
FUND_RANK_DELTA_K = 30.0       # 前20席净多率较昨日变化的 tanh 灵敏度
FUND_CARRY_K = 0.15            # 年化展期收益率 tanh 灵敏度（年化15%即接近饱和）
FUND_BASIS_K = 0.05            # 基差率 tanh 灵敏度（5%基差率即接近饱和）
FUND_EM_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"  # 东财数据中心统一入口
FUND_EM_PAGE_SIZE = 400        # 东财库存时序一次拉取条数（免费窗口约3个月）
FUND_PPI_URL = "https://www.100ppi.com/sf/day-{date}.html"  # 生意社当日基差表（反爬时自动降级）

# ---------------- 分钟K线数据层（第14轮 WP-D0 落地；2026-09-01晚增补：新浪1m主源化，零新增依赖） ----------------
# 实测（2026-09-01 晚两次）：①新浪 getFewMinLine type=1/5/15/30/60 全部固定1023根、64/64品种零断连，
#   主连RB0与具体合约RB2701/MA610均可取——纠正第14轮"新浪无1分钟"误判，1m主源由东财切换为新浪；
#   ②东财 push2his（走http；secid=市场号.具体合约，无主连；SHFE113/DCE114/CZCE115/INE142/GFEX225；
#   CZCE年份取个位MA2610->ma610；f51时/f52开/f53收/f54高/f55低/f56量/f57额，开-收-高-低顺序），
#   该域名两晚持续 RemoteDisconnected（IP级限流），仅作新浪与通达信之后的兜底，保留节流+镜像轮换+熔断；
#   ③东财 push2 实时快照口（非push2his）稳定，主连secid=市场号.品种小写m（113.rbm），f111=持仓量，
#   已用于 futures_data.fetch_quotes 的新浪缺失兜底；④腾讯免费接口不覆盖国内商品期货（实测none_match）。
#   免费窗口：1m约2.5交易日、5m约3周、15m约3月、30m约6月、60m约12.5月，长期深度靠常驻自采滚动积累。
MINUTE_EM_HOSTS = ["push2his.eastmoney.com", "1.push2his.eastmoney.com",
                   "2.push2his.eastmoney.com", "3.push2his.eastmoney.com"]  # 镜像子域轮换，单个被断连时换下一个
MINUTE_MARKET = {"SHFE": "113", "DCE": "114", "CZCE": "115", "INE": "142", "GFEX": "225"}
MINUTE_PERIODS = (1, 5, 30)              # 常驻增量自采周期（分钟）；全部走新浪主连（含1m，type=1实测1023根）
MINUTE_BACKFILL_PERIODS = (60, 30, 15, 5, 1)  # 启动回填顺序：深周期先落，1m新浪同样1023根一次到位
MINUTE_BACKFILL_LMT = {1: 1023, 5: 1023, 15: 1023, 30: 1023, 60: 1023}  # 新浪固定最多1023根，一次回填到位
MINUTE_INCR_LMT = {1: 12, 5: 8, 15: 8, 30: 6, 60: 6}  # 增量只取最近N根（UNIQUE去重后实际新增很少）
MINUTE_ONCE_LMT = {1: 1023, 5: 200, 15: 200, 30: 300, 60: 300}  # --once同步小回填条数（新浪秒回，1m可全量）
MINUTE_WORKERS = 6                       # 并发线程数（主源新浪稳定可并发；东财任务内部另有全局限流）
MINUTE_TDX_ENABLED = True                # 通达信可选源：启动自动探测，确认能取期货才启用、否则零成本跳过
MINUTE_REQ_GAP = 0.25                    # 相邻请求最小间隔（秒，全局限流）
MINUTE_RETRY = 4                         # 单合约单周期失败重试轮数（每轮遍历全部镜像子域）
MINUTE_RETRY_WAIT = 1.2                  # 重试退避基数（秒，逐轮加倍、封顶8秒）
MINUTE_CIRCUIT_FAILS = 8                # 连续连接级失败多少次触发熔断（东财整站限流/断连时）
MINUTE_CIRCUIT_COOLDOWN = 60            # 熔断冷却秒数：期间自采直接跳过不发请求，到期自动重试
MINUTE_LOOP_INTERVAL = 300               # 交易时段常驻增量自采间隔（秒，5分钟，对齐1/5分钟bar）
MINUTE_OFFPEAK_INTERVAL = 1800           # 非交易时段自采间隔（秒，30分钟；返回的仍是收盘bar，去重后不膨胀）
MINUTE_BARS_RETENTION_DAYS = 400         # 分钟K保留天数（长期自采库，到期prune；sqlite可轻松承载）

# ---------------- 最小日线回测（P1第三批，零新增依赖） ----------------
BACKTEST_REPORT_FILE = os.path.join(BASE_DIR, "reports", "backtest_report.txt")
BACKTEST_SIGNALS_FILE = os.path.join(BASE_DIR, "reports", "backtest_signals.csv")
BACKTEST_TRADES_FILE = os.path.join(BASE_DIR, "reports", "backtest_trades.csv")
BACKTEST_LOOKBACK_DAYS = 250
BACKTEST_HOLD_DAYS = 10
BACKTEST_ENTRY_SCORE = 2.0
BACKTEST_WORKERS = 6
BACKTEST_FEE_RATE = 0.00005       # 找不到真实手续费表时的兜底单边费率（按价格比例，万0.5）
BACKTEST_SLIP_RATE = 0.00010      # 单边滑点近似（万1；可用CLI覆盖）
FUTURES_FEES_FILE = os.path.join(DATA_DIR, "futures_fees.csv")  # 用户券商手续费表转换出的64品种真实费率
BACKTEST_USE_REAL_FEES = True     # 默认优先使用FUTURES_FEES_FILE；--no-real-fees可回退统一比例费率
BACKTEST_LIMIT_LOCK = 0.07        # 同时满足收盘在最高/最低且涨跌幅≥7%，视为疑似锁涨跌停
BACKTEST_STABLE_HOLDS = (5, 10, 20)
BACKTEST_STABLE_ENTRIES = (1.5, 2.0, 2.5)
BACKTEST_ROLL_GAP_MAD = 5.0   # 主连换月跳空：超过5倍中位日波动且6%视为换月，收益置0并比例复权
BACKTEST_ROLL_GAP_ABS = 0.06

# ---------------- 日内/平今回测（第15轮 WP-D1/D2，minute_bars 自采库驱动，零新增依赖） ----------------
INTRADAY_BT_REPORT_FILE = os.path.join(BASE_DIR, "reports", "intraday_backtest_report.txt")
INTRADAY_BT_TRADES_FILE = os.path.join(BASE_DIR, "reports", "intraday_backtest_trades.csv")
INTRADAY_BT_PERIOD = 30          # 默认回放周期（分钟）：30m库深约6个月；可选1/5/15/30/60
INTRADAY_BT_LOOKBACK = 1023      # 每品种最多取多少根分钟bar（新浪单周期上限1023）
INTRADAY_BT_WARMUP = 60          # 指标预热最少bar数（MA60/分钟ATR14需要）
INTRADAY_BT_SIG_WINDOW = 120     # 滚动计算技术信号的回看窗口（bar数，固定窗口控成本）
INTRADAY_BT_ENTRY = 1.5          # 分钟技术分入场阈值
INTRADAY_BT_ATR_PERIOD = 14      # 分钟ATR周期（bar数）
INTRADAY_BT_STOP_ATR = 1.2       # 止损=入场价∓1.2×分钟ATR
INTRADAY_BT_TARGET_ATR = 2.0     # 止盈=入场价±2.0×分钟ATR
INTRADAY_BT_MAX_BARS = 48        # 摆动模式单笔最长持有bar数；日内模式由日终强平接管
INTRADAY_BT_FLAT_EOD = True      # 默认日内模式：交易日结束前强平、不跨交易日（走平今费）
INTRADAY_BT_FEE_RATE = 0.00005   # 真实费率表缺失时的兜底单边费率
INTRADAY_BT_SLIP_RATE = 0.00010  # 单边滑点（按价格比例，万1）
INTRADAY_BT_LIMIT_MOVE = 0.07    # 品种表缺失时的兜底涨跌停幅度
INTRADAY_BT_LIMIT_TICK_EPS = 0.0008  # 锁板贴边容差（整根bar距板价≤0.8%才判封死，避免误杀）
INTRADAY_BT_STABLE_ENTRIES = (1.0, 1.5, 2.0)
INTRADAY_BT_STABLE_STOPS = (1.2, 2.0)
INTRADAY_BT_STABLE_TARGETS = (1.5, 2.0, 3.0)
# 各品种常态涨跌停板幅度（主力合约档），来源：期货公司2026-09-01披露（与当前分钟库回测窗口同期）。
# 诚实口径：交易所长假前/临近交割月会临时扩板，且涨跌停按昨结算计算；本回测无昨结算字段，
# 以"前一交易日收盘价×(1±幅度)"近似，仅做"整根bar封死在板价"的保守锁板识别，非逐笔盘口；
# 可用 --limit-move 覆盖、--no-limit-filter 关闭。
FUTURES_LIMIT_MOVE = {
    "RB": 0.05, "HC": 0.05, "SS": 0.05, "SP": 0.05,
    "CU": 0.09, "AL": 0.09, "ZN": 0.09, "PB": 0.09, "NI": 0.10, "SN": 0.12, "AO": 0.09,
    "AU": 0.14, "AG": 0.20,
    "RU": 0.07, "BR": 0.10, "FU": 0.14, "BU": 0.10,
    "SC": 0.14, "LU": 0.14, "NR": 0.07, "BC": 0.09, "EC": 0.20,
    "A": 0.06, "B": 0.06, "M": 0.06, "Y": 0.06, "P": 0.07, "C": 0.06, "CS": 0.05,
    "RR": 0.05, "JD": 0.06, "LH": 0.06, "LG": 0.05,
    "L": 0.06, "V": 0.09, "PP": 0.06, "EG": 0.09, "EB": 0.06, "PG": 0.09,
    "J": 0.08, "JM": 0.08, "I": 0.09,
    "SR": 0.05, "CF": 0.06, "CY": 0.04, "TA": 0.06, "MA": 0.09, "PX": 0.06,
    "PF": 0.06, "PR": 0.06, "SH": 0.07, "FG": 0.08, "SA": 0.07, "UR": 0.07,
    "RM": 0.06, "OI": 0.06, "PK": 0.06, "AP": 0.08, "CJ": 0.07, "SF": 0.06, "SM": 0.06,
    "SI": 0.08, "LC": 0.13, "PS": 0.09,
}

# ---------------- 组合资金账户/权益曲线（第16轮 WP-E，零新增依赖） ----------------
# 统一资金池：多品种共享一个账户，逐bar盯市，算保证金占用/可用资金/风险度，风险度破线按规则强平。
# 保证金率来自 tools/build_margin_table.py 半自动维护的 data/futures_margins.csv
# （国君期货日历表的"期货公司实际收取档"，已含公司加收；交易所基准无干净免费源故留空不编造）。
PORTFOLIO_REPORT_FILE = os.path.join(BASE_DIR, "reports", "portfolio_report.txt")
PORTFOLIO_EQUITY_FILE = os.path.join(BASE_DIR, "reports", "portfolio_equity.csv")
PORTFOLIO_TRADES_FILE = os.path.join(BASE_DIR, "reports", "portfolio_trades.csv")
FUTURES_MARGINS_FILE = os.path.join(DATA_DIR, "futures_margins.csv")
PORTFOLIO_EQUITY0 = 1_000_000        # 初始权益（元）
PORTFOLIO_SIZING = "equal_notional"  # 手数：equal_notional等名义/equal_risk等风险(ATR)/score按综合分档
PORTFOLIO_PER_SYMBOL = 0.15          # 等名义：单品种目标名义本金占权益比例
PORTFOLIO_RISK_PER_TRADE = 0.01      # 等风险：单笔风险预算占权益比例（止损距离=STOP_ATR×ATR）
PORTFOLIO_SCORE_WEIGHTS = {"轻仓": 0.05, "分批": 0.10, "强信号": 0.15}  # 按分档加权的目标名义比例
PORTFOLIO_MAX_SYMBOL_WEIGHT = 0.30   # 单品种名义价值占动态权益上限
PORTFOLIO_MAX_SECTOR_WEIGHT = 0.60   # 单板块名义价值合计占动态权益上限
PORTFOLIO_RISK_LIQUIDATE = 1.00      # 风险度（保证金占用/动态权益）≥该值触发强制减仓
PORTFOLIO_RISK_SAFE = 0.80           # 强平到风险度低于该安全线为止
PORTFOLIO_DEFAULT_MARGIN = 0.12      # 保证金表缺失品种的兜底估算率（显式标注，不允许静默用错）
PORTFOLIO_MAX_CONCURRENT = 12        # 同时持仓品种数软上限（0=不限制）

# ---- G26（第40轮）组合构建器 portfolio_constructor：横截面目标权重（风险型，不依赖预期收益）----
PC_METHODS = ("equal", "inv_vol", "erc", "gmv")   # 等权/逆波动/风险平价ERC/最小方差GMV；默认 equal=旧等名义口径
PC_LOOKBACK = 126                     # 用过去126个交易日日收益估协方差（只用过去、PIT）
PC_SHRINK = 0.10                      # 协方差向对角收缩强度（保正定/条件数，Ledoit-Wolf 简化版）
PC_MAX_WEIGHT = 0.20                  # 单品种目标权重上限（GMV/ERC 求解后做 capped-simplex 约束）
PC_REBAL = 20                         # 组合代理回测再平衡间隔（交易日，呼应第39轮换手结论）
PC_TARGET_VOL_ANNUAL = 0.15           # 目标年化波动（target_vol 缩放，0=不做波动目标缩放）
PC_MAX_GROSS = 1.50                   # 目标波动缩放的总敞口（Σ|w|）上限，防低波期过度加杠杆
PC_PERIODS_PER_YEAR = 243             # 商品期货日频年化交易日
PC_ERC_TOL = 1e-4                      # ERC 风险贡献均衡相对收敛阈值（全牛顿法，尺度无关）
PC_ERC_MAX_ITER = 300                  # ERC 最大迭代（全牛顿+回溯通常30次内收敛）
PC_FILE = os.path.join(BASE_DIR, "reports", "portfolio_lab.txt")
PC_JSON = os.path.join(BASE_DIR, "reports", "portfolio_lab.json")

# ---------------- 输出 ----------------
REPORT_FILE = os.path.join(BASE_DIR, "reports", "latest_report.txt")
SIGNALS_CSV = os.path.join(BASE_DIR, "reports", "signals.csv")
# 交易时段当日归档（新块置顶；次日启动时清除昨日块，长期保留请看 DAILY_REVIEW_FILE）
HISTORY_FILE = os.path.join(BASE_DIR, "reports", "history_report.txt")
KEEP_ROUNDS = 5              # latest_report.txt / signals.csv 只保留最近5轮
# 非交易时段(9:00-11:30/13:30-15:00/21:00-23:00之外)专用文件
OFFHOURS_REPORT_FILE = os.path.join(BASE_DIR, "reports", "offhours_report.txt")
# 非交易时段当日归档（新块置顶；次日启动时清除昨日块）
OFFHOURS_HISTORY_FILE = os.path.join(BASE_DIR, "reports", "offhours_history.txt")
# 每日复盘报告（归属交易日全部夜盘结束后自动生成，新复盘置顶，永不删除）与当日新闻缓存
DAILY_REVIEW_FILE = os.path.join(BASE_DIR, "reports", "daily_review.txt")
NEWS_CACHE_DIR = os.path.join(BASE_DIR, "cache")
REALTIME_HTML = os.path.join(BASE_DIR, "reports", "实时报告.html")
REPORT_AUTO_OPEN = True     # 首轮真实报告生成后是否自动用默认浏览器打开实时报告HTML（命令行 --no-launch 可临时关闭）
# 看板探测"是否有新报告写出"用的极小状态文件（每轮落盘时更新，页面轮询它、只在新报告时重载内容）
STATUS_JS = os.path.join(BASE_DIR, "reports", "report_status.js")

# ---------------- P1-3 看板图表化（第22轮，本地 ECharts，零 Python 运行依赖） ----------------
# 图表看板静态页（iframe 页签之一）；其数据走 chart_data.js（window.CHART_DATA 全局，
# file:// 下 fetch 不可用，沿用 report_status.js 的动态 script 注入模式）。
CHARTS_PAGE_HTML = os.path.join(BASE_DIR, "reports", "图表看板.html")
CHART_DATA_JS = os.path.join(BASE_DIR, "reports", "chart_data.js")
# ECharts 本地前端资源：canonical 随仓库（assets/），运行时幂等同步到 reports/assets/。
ECHARTS_SRC = os.path.join(BASE_DIR, "assets", "echarts.min.js")
ECHARTS_DST = os.path.join(BASE_DIR, "reports", "assets", "echarts.min.js")
CHARTS_EQUITY_MAX_POINTS = 1200   # 权益曲线抽稀上限（点太多浏览器卡；不改原始 CSV）

# ---------------- 品种映射 ----------------
# 品种名 -> 新浪主力连续代码 / 合约代码字母 / 交易所 / 板块 / 原油联动权重
# ex: SHFE上期所 INE上期能源 DCE大商所 CZCE郑商所 GFEX广期所
# 板块: 能源化工 / 黑色 / 有色 / 贵金属 / 农产品 / 金融
EN, BL, ME, PM, AG, FI = "能源化工", "黑色", "有色", "贵金属", "农产品", "金融"

VARIETIES = {
    # ---- 上期所 SHFE ----
    "螺纹钢":     {"code": "RB0", "sym": "RB", "ex": "SHFE", "cat": BL, "oil_w": 0.0},
    "热卷":       {"code": "HC0", "sym": "HC", "ex": "SHFE", "cat": BL, "oil_w": 0.0},
    "不锈钢":     {"code": "SS0", "sym": "SS", "ex": "SHFE", "cat": BL, "oil_w": 0.0},
    "铜":         {"code": "CU0", "sym": "CU", "ex": "SHFE", "cat": ME, "oil_w": 0.0},
    "铝":         {"code": "AL0", "sym": "AL", "ex": "SHFE", "cat": ME, "oil_w": 0.0},
    "氧化铝":     {"code": "AO0", "sym": "AO", "ex": "SHFE", "cat": ME, "oil_w": 0.0},
    "锌":         {"code": "ZN0", "sym": "ZN", "ex": "SHFE", "cat": ME, "oil_w": 0.0},
    "铅":         {"code": "PB0", "sym": "PB", "ex": "SHFE", "cat": ME, "oil_w": 0.0},
    "镍":         {"code": "NI0", "sym": "NI", "ex": "SHFE", "cat": ME, "oil_w": 0.0},
    "锡":         {"code": "SN0", "sym": "SN", "ex": "SHFE", "cat": ME, "oil_w": 0.0},
    "黄金":       {"code": "AU0", "sym": "AU", "ex": "SHFE", "cat": PM, "oil_w": 0.0},
    "白银":       {"code": "AG0", "sym": "AG", "ex": "SHFE", "cat": PM, "oil_w": 0.0},
    "橡胶":       {"code": "RU0", "sym": "RU", "ex": "SHFE", "cat": EN, "oil_w": 0.35},
    "丁二烯橡胶": {"code": "BR0", "sym": "BR", "ex": "SHFE", "cat": EN, "oil_w": 0.30},
    "燃料油":     {"code": "FU0", "sym": "FU", "ex": "SHFE", "cat": EN, "oil_w": 0.90},
    "沥青":       {"code": "BU0", "sym": "BU", "ex": "SHFE", "cat": EN, "oil_w": 0.85},
    "纸浆":       {"code": "SP0", "sym": "SP", "ex": "SHFE", "cat": EN, "oil_w": 0.15},
    # ---- 上期能源 INE ----
    "原油":       {"code": "SC0", "sym": "SC", "ex": "INE", "cat": EN, "oil_w": 1.00},
    "20号胶":     {"code": "NR0", "sym": "NR", "ex": "INE", "cat": EN, "oil_w": 0.35},
    "低硫燃料油": {"code": "LU0", "sym": "LU", "ex": "INE", "cat": EN, "oil_w": 0.90},
    "国际铜":     {"code": "BC0", "sym": "BC", "ex": "INE", "cat": ME, "oil_w": 0.0},
    "集运欧线":   {"code": "EC0", "sym": "EC", "ex": "INE", "cat": EN, "oil_w": 0.20},
    # ---- 大商所 DCE ----
    "豆一":       {"code": "A0",  "sym": "A",  "ex": "DCE", "cat": AG, "oil_w": 0.0},
    "豆二":       {"code": "B0",  "sym": "B",  "ex": "DCE", "cat": AG, "oil_w": 0.0},
    "豆粕":       {"code": "M0",  "sym": "M",  "ex": "DCE", "cat": AG, "oil_w": 0.0},
    "豆油":       {"code": "Y0",  "sym": "Y",  "ex": "DCE", "cat": AG, "oil_w": 0.0},
    "棕榈油":     {"code": "P0",  "sym": "P",  "ex": "DCE", "cat": AG, "oil_w": 0.0},
    "玉米":       {"code": "C0",  "sym": "C",  "ex": "DCE", "cat": AG, "oil_w": 0.0},
    "淀粉":       {"code": "CS0", "sym": "CS", "ex": "DCE", "cat": AG, "oil_w": 0.0},
    "粳米":       {"code": "RR0", "sym": "RR", "ex": "DCE", "cat": AG, "oil_w": 0.0},
    "鸡蛋":       {"code": "JD0", "sym": "JD", "ex": "DCE", "cat": AG, "oil_w": 0.0},
    "生猪":       {"code": "LH0", "sym": "LH", "ex": "DCE", "cat": AG, "oil_w": 0.0},
    "原木":       {"code": "LG0", "sym": "LG", "ex": "DCE", "cat": AG, "oil_w": 0.0},
    "塑料":       {"code": "L0",  "sym": "L",  "ex": "DCE", "cat": EN, "oil_w": 0.40},
    "PVC":        {"code": "V0",  "sym": "V",  "ex": "DCE", "cat": EN, "oil_w": 0.30},
    "聚丙烯":     {"code": "PP0", "sym": "PP", "ex": "DCE", "cat": EN, "oil_w": 0.40},
    "乙二醇":     {"code": "EG0", "sym": "EG", "ex": "DCE", "cat": EN, "oil_w": 0.50},
    "苯乙烯":     {"code": "EB0", "sym": "EB", "ex": "DCE", "cat": EN, "oil_w": 0.50},
    "液化石油气": {"code": "PG0", "sym": "PG", "ex": "DCE", "cat": EN, "oil_w": 0.60},
    "焦炭":       {"code": "J0",  "sym": "J",  "ex": "DCE", "cat": BL, "oil_w": 0.0},
    "焦煤":       {"code": "JM0", "sym": "JM", "ex": "DCE", "cat": BL, "oil_w": 0.0},
    "铁矿石":     {"code": "I0",  "sym": "I",  "ex": "DCE", "cat": BL, "oil_w": 0.0},
    # ---- 郑商所 CZCE ----
    "白糖":       {"code": "SR0", "sym": "SR", "ex": "CZCE", "cat": AG, "oil_w": 0.0},
    "棉花":       {"code": "CF0", "sym": "CF", "ex": "CZCE", "cat": AG, "oil_w": 0.0},
    "棉纱":       {"code": "CY0", "sym": "CY", "ex": "CZCE", "cat": AG, "oil_w": 0.0},
    "PTA":        {"code": "TA0", "sym": "TA", "ex": "CZCE", "cat": EN, "oil_w": 0.55},
    "甲醇":       {"code": "MA0", "sym": "MA", "ex": "CZCE", "cat": EN, "oil_w": 0.50},
    "对二甲苯":   {"code": "PX0", "sym": "PX", "ex": "CZCE", "cat": EN, "oil_w": 0.60},
    "短纤":       {"code": "PF0", "sym": "PF", "ex": "CZCE", "cat": EN, "oil_w": 0.45},
    "瓶片":       {"code": "PR0", "sym": "PR", "ex": "CZCE", "cat": EN, "oil_w": 0.40},
    "烧碱":       {"code": "SH0", "sym": "SH", "ex": "CZCE", "cat": EN, "oil_w": 0.30},
    "玻璃":       {"code": "FG0", "sym": "FG", "ex": "CZCE", "cat": EN, "oil_w": 0.10},
    "纯碱":       {"code": "SA0", "sym": "SA", "ex": "CZCE", "cat": EN, "oil_w": 0.15},
    "尿素":       {"code": "UR0", "sym": "UR", "ex": "CZCE", "cat": EN, "oil_w": 0.10},
    "菜粕":       {"code": "RM0", "sym": "RM", "ex": "CZCE", "cat": AG, "oil_w": 0.0},
    "菜籽油":     {"code": "OI0", "sym": "OI", "ex": "CZCE", "cat": AG, "oil_w": 0.0},
    "花生":       {"code": "PK0", "sym": "PK", "ex": "CZCE", "cat": AG, "oil_w": 0.0},
    "苹果":       {"code": "AP0", "sym": "AP", "ex": "CZCE", "cat": AG, "oil_w": 0.0},
    "红枣":       {"code": "CJ0", "sym": "CJ", "ex": "CZCE", "cat": AG, "oil_w": 0.0},
    "硅铁":       {"code": "SF0", "sym": "SF", "ex": "CZCE", "cat": BL, "oil_w": 0.0},
    "锰硅":       {"code": "SM0", "sym": "SM", "ex": "CZCE", "cat": BL, "oil_w": 0.0},
    # ---- 广期所 GFEX ----
    "工业硅":     {"code": "SI0", "sym": "SI", "ex": "GFEX", "cat": ME, "oil_w": 0.0},
    "碳酸锂":     {"code": "LC0", "sym": "LC", "ex": "GFEX", "cat": ME, "oil_w": 0.0},
    "多晶硅":     {"code": "PS0", "sym": "PS", "ex": "GFEX", "cat": ME, "oil_w": 0.0},
}

# 有场内期权的品种（期权分析只对这些品种做，可按需增删）
OPTION_VARIETIES = {
    "原油", "低硫燃料油", "铜", "铝", "锌", "黄金", "白银", "螺纹钢", "橡胶", "氧化铝",
    "豆粕", "玉米", "铁矿石", "液化石油气", "塑料", "PVC", "聚丙烯", "棕榈油",
    "乙二醇", "苯乙烯", "豆一", "豆二", "豆油", "原木",
    "白糖", "棉花", "PTA", "甲醇", "菜粕", "菜籽油", "花生",
    "纯碱", "短纤", "玻璃", "尿素", "对二甲苯", "烧碱",
    "工业硅", "碳酸锂", "多晶硅",
}

# 期权合约代码前缀 -> 品种名（备用：从期权合约行反查标的）
OPTION_CODE2NAME = {
    "SC": "原油", "LU": "低硫燃料油", "CU": "铜", "AL": "铝", "ZN": "锌", "PB": "铅",
    "NI": "镍", "SN": "锡", "AU": "黄金", "AG": "白银", "RB": "螺纹钢", "HC": "热卷",
    "RU": "橡胶", "BR": "丁二烯橡胶", "BU": "沥青", "SP": "纸浆", "AO": "氧化铝",
    "M": "豆粕", "Y": "豆油", "A": "豆一", "B": "豆二", "C": "玉米", "CS": "淀粉",
    "P": "棕榈油", "L": "塑料", "V": "PVC", "PP": "聚丙烯", "EG": "乙二醇",
    "EB": "苯乙烯", "PG": "液化石油气", "JD": "鸡蛋", "LH": "生猪", "I": "铁矿石",
    "J": "焦炭", "JM": "焦煤", "LG": "原木", "RR": "粳米",
    "SR": "白糖", "CF": "棉花", "CY": "棉纱", "TA": "PTA", "MA": "甲醇", "RM": "菜粕",
    "OI": "菜籽油", "PK": "花生", "AP": "苹果", "CJ": "红枣", "SA": "纯碱",
    "UR": "尿素", "PF": "短纤", "FG": "玻璃", "PX": "对二甲苯", "SH": "烧碱",
    "PR": "瓶片", "SI": "工业硅", "LC": "碳酸锂", "PS": "多晶硅", "EC": "集运欧线",
}

# 日线数据拉取失败时各板块的默认年化波动率（估计值）
DEFAULT_HV = {EN: 0.30, BL: 0.28, ME: 0.20, PM: 0.16, AG: 0.22, FI: 0.18}


def strike_step(price):
    """按标的价格量级近似期权执行价间距（用于把建议执行价取整到挂牌档位附近）"""
    for bound, step in ((10, 0.25), (20, 0.5), (50, 1), (100, 2), (300, 5),
                        (1000, 10), (3000, 20), (10000, 50), (50000, 100),
                        (200000, 500)):
        if price < bound:
            return step
    return 1000


# ================= WP-F1（P0）：五维情绪 / 横截面强弱 / 独立风控闸门 =================
# 设计原则：默认只做"信息增量"——不改变既有综合分/信号/建议；风控 veto 默认仅显著标注+告警，
# 只有显式打开 RISK_GATE_AUTO_DOWNGRADE 才会自动降级，保证可回退、可对照。
# ---- D1 五维情绪（强度/不确定性/相关性/前瞻性/事件类型）----
SENTIMENT_INTENSITY_K = 1.2    # 强度词命中数 -> 0~1 的 tanh 系数（越大越快饱和）
SENTIMENT_UNCERT_K = 1.0       # 不确定性词命中数 -> 0~1
SENTIMENT_FORWARD_K = 1.0      # 前瞻性词命中数 -> 0~1
SENTIMENT_FACET_TAG_MIN = 0.25 # 维度值达到该阈值才在消息角标中显示，避免噪声

# ---- B1 横截面强弱（稳健 z-score / MAD，跨64品种横向比较）----
XS_SCORE_W = 0.6               # 横截面综合强度中"综合分稳健z"权重
XS_CHG_W = 0.4                 # 横截面综合强度中"当日涨跌幅稳健z"权重
XS_Z_CLIP = 3.0                # 稳健 z 截断，防极端值主导
XS_TOP_N = 5                   # 相对最强/最弱各列出 N 个
XS_MIN_SAMPLE = 8              # 有效样本少于该值不做稳健标准化（退回原始分排序，不硬算 z）

# ---- A2 独立风控闸门（独立于打分，只做否决/警示）----
RISK_GATE_ENABLED = True          # 总开关：False 时完全不评估、不标注、不告警
RISK_GATE_AUTO_DOWNGRADE = False  # True=veto 时自动把信号降级为观望；False(默认)=只标注+告警，不改综合分/建议
RISK_GATE_MIN_VOLUME = 200        # 成交量(手)低于此视为流动性不足（price 缺失同样 veto）
RISK_GATE_DIVERGE_CHG = 0.02      # 强信号(|分|>=SCORE_MID)但当日涨跌幅反向超过 2% -> veto（防追高/摸顶）
RISK_GATE_HV_EXTREME = 0.95       # HV20 滚动分位>=该值 -> veto（历史极端波动，ATR 止损易被打穿）
RISK_GATE_HV_HIGH = 0.80          # HV 分位>=该值 -> warn（波动偏高，建议降杠杆）
RISK_GATE_FLOW_CONFLICT = True    # 信号方向与量仓资金方向相反 -> warn（价涨资金撤）
RISK_GATE_NEAR_DELIVERY = True    # 主力合约临近交割/月份异常 -> warn
RISK_GATE_ALERT = True            # veto 是否走声音/Webhook 告警通道（复用 alerts 聚合，自动限流）


# ================= WP-F2（P1-2）：信号胜率校准 / 因子IC评估 / triple-barrier 样本资产 =================
# 设计原则同 WP-F1：默认只做信息增量——实时侧仅展示"历史同类信号胜率"（影子模式），
# 不改变综合分/信号/建议；组合回测 portfolio.py 需显式 --calibrate 才把乘子作用于手数。
# ---- A3 历史胜率校准 sizing（meta-labeling 零依赖版，signal_calibrator.py）----
CALIBRATOR_ENABLED = True          # 实时侧是否计算并展示历史同类胜率（False=完全不计算，等价旧版）
CALIBRATOR_HORIZON = 120           # 默认采用哪个评估周期的历史结果（30分钟/120分钟/1440分钟）
CALIBRATOR_MIN_N = 20              # 分组有效样本不足该值则向上回退分组层级，全部不足返回乘子1.0
CALIBRATOR_PRIOR_N = 2.0           # 贝叶斯平滑先验强度（Beta先验，先验胜率0.5；越大越保守）
CALIBRATOR_MULT_LO = 0.5           # 置信乘子下限（历史胜率显著低时最多减半仓）
CALIBRATOR_MULT_HI = 1.2           # 置信乘子上限（历史胜率显著高时最多加20%仓）
CALIBRATOR_MULT_SLOPE = 2.0        # mult=clip(1+(平滑胜率-0.5)*slope, 下限, 上限)
CALIBRATOR_STAT_DAYS = 365         # 取最近多少天的历史结果做校准（信号样本长期积累）

# ---- B2 因子IC评估（tools/factor_eval.py，研究侧定期人工跑，不进常驻链路）----
FACTOR_EVAL_N_QUANTILE = 5         # 因子分档数（分档单调性/多空价差）
FACTOR_EVAL_MIN_SAMPLE = 30        # 单因子单周期最少配对样本，不足不给结论只列样本数
FACTOR_EVAL_FILE = os.path.join(BASE_DIR, "reports", "factor_eval.txt")
FACTOR_EVAL_JSON = os.path.join(BASE_DIR, "reports", "factor_eval.json")  # P1-3 图表用结构化 sidecar

# ---- B3 triple-barrier 样本集（tools/build_ml_samples.py，为 WP-F4 备料）----
ML_SAMPLE_TARGET_ATR = 2.0         # 上轨(止盈)距离 = target_atr × ATR
ML_SAMPLE_STOP_ATR = 1.2           # 下轨(止损)距离 = stop_atr × ATR（与日内回测默认一致）
ML_SAMPLE_MAX_BARS = 48            # 纵向时间壁垒：最多观察多少根bar，超时按到期方向收益符号定标签
ML_SAMPLE_EMBARGO_BARS = 2         # 标签跨越训练/测试切分点时额外隔离的bar数（purged+embargo）
ML_SAMPLES_RETENTION_DAYS = 3650   # ml_samples 长期保留（约10年，监督学习样本资产）

# ---------------- G11 数据源主备熔断降级链（data_router.py） ----------------
DATA_ROUTER_ENABLED = True          # 总开关；False=熔断器永不拦截（等价旧版逐源尝试）
DATA_ROUTER_FAIL_THRESHOLD = 5      # 单源连续失败多少次进入熔断 OPEN
DATA_ROUTER_COOLDOWN_SEC = 300      # 熔断冷却秒数：到期放一次半开试探，成功恢复/失败重新熔断
DATA_ROUTER_ALERT_AFTER = 2         # G6：某源连续多少轮处于熔断/全失败则复用 alerts 告警

# ---------------- G6 数据质量监控（data_health.py + storage.data_health 表） ----------------
DATA_HEALTH_ENABLED = True          # 总开关；False=不评估/不落表/不告警（等价旧版）
DATA_HEALTH_RETENTION_DAYS = 365    # data_health 健康记录保留天数（体积极小，默认留1年）
DATA_HEALTH_JUMP_PCT = 0.30         # 单品种|涨跌幅|≥30%判为异常跳变（疑似脏价，真实商品期货罕见）
DATA_HEALTH_MISS_ALERT_CYCLES = 2   # 某品种连续多少轮缺行情才告警（避免单轮抖动误报）
DATA_HEALTH_SOURCE_FAIL_CYCLES = 2  # 某数据源连续多少轮全失败才告警

# ---------------- G4 回测严谨性（第26轮；默认值全部等价旧版，只增量展示/留档） ----------------
BACKTEST_FILL_MODE = "close"        # 成交时点：close=信号根收盘成交(旧口径,默认保可比)；next_open=次根开盘成交(保守对照,贴近实盘)
BACKTEST_IMPACT_RATE = 0.0          # 单边冲击成本率(按价格比例)，默认0=不额外计；与滑点分开列示，往返计两次
BACKTEST_BOOTSTRAP_N = 1000         # 交易序列iid bootstrap重采样次数(固定种子可复现)；0=关闭置信区间
BACKTEST_BOOTSTRAP_SEED = 20260902  # bootstrap固定随机种子，保证同输入逐值可复现
BACKTEST_BOOTSTRAP_CI = (0.05, 0.95)  # 置信区间分位(下界,上界)
BACKTEST_BOOTSTRAP_MIN_TRADES = 20  # 净交易笔数少于该值不给区间(小样本不做假精确)
BACKTEST_OOS_RATIO = 0.0            # 样本外占比：0=关闭(旧口径)；如0.3=按时间排序后30%交易为OOS、与前70%IS并列对照
BACKTEST_VALIDATION_JSON = os.path.join(BASE_DIR, "reports", "backtest_validation.json")  # DSR/PBO sidecar(研究工具产出)
BACKTEST_RUNS_RETENTION_DAYS = 3650  # backtest_runs 回测留档保留天数(约10年，体积极小)

# ---------------- G1 纸面交易引擎（第27轮；默认影子独立，不接入主链/不改任何现有输出；第28轮接报告/看板） ----------------
PAPER_ENABLED = False             # 总开关：False=纸面引擎完全休眠（默认；第28轮接入 main 后再评估默认开影子）
PAPER_FILL_MODE = "next"          # 成交时点：next=信号下一轮首个新价成交(保守,影子默认,严格晚于信号)；close=信号轮当轮最新价成交
PAPER_EQUITY0 = 1_000_000         # 纸面账户初始资金（人民币元，与 portfolio 默认一致）
PAPER_ENTRY_SCORE = 4.0           # |综合分|>=该值才开仓（默认=SCORE_LIGHT 轻仓线；低于此只观望不下单）
PAPER_EXIT_SCORE = 2.0            # 持仓后 |综合分|<该值（回到中性带）则平仓离场（默认=SCORE_NEUTRAL，迟滞防抖）
PAPER_SIZING = "equal_notional"   # 手数算法，复用 portfolio 三选一：equal_notional/equal_risk/score
PAPER_PER_SYMBOL = 0.15           # equal_notional：单品种目标名义占权益比例
PAPER_RISK_PER_TRADE = 0.01       # equal_risk：单手打到止损的最大亏损占权益比例
PAPER_MAX_SYMBOL_WEIGHT = 0.30    # 单品种名义上限（占权益）
PAPER_MAX_SECTOR_WEIGHT = 0.60    # 单板块名义上限（占权益）
PAPER_MAX_CONCURRENT = 12         # 最多同时持仓品种数
PAPER_RISK_LIQUIDATE = 1.00       # 风险度(占用/权益)>=该值启动强平
PAPER_RISK_SAFE = 0.80            # 触发强平后一路砍到该安全线以下，防阈值附近反复触发
PAPER_DEFAULT_MARGIN = 0.12       # 缺保证金率表时的兜底保证金率
PAPER_USE_REAL_FEES = True        # 优先读 data/futures_fees.csv 真实费率（缺表回退兜底比例）
PAPER_FEE_RATE = 0.00005          # 兜底单边手续费率（真实费率表缺失时）
PAPER_SLIP_RATE = 0.0001          # 单边滑点率：买价=盘面价*(1+slip)、卖价=盘面价*(1-slip)，成交价内含滑点
PAPER_LIMIT_EPS = 0.0008          # 实时锁板判定的贴板容差（与 INTRADAY_BT_LIMIT_TICK_EPS 同量级）
PAPER_ALLOW_ADD = False           # 持仓且同向更强信号是否加仓（默认False=只持有/反手/离场，不反复加仓）
PAPER_RETENTION_DAYS = 3650       # paper_orders/trades/equity 保留天数（纸面需长期影子对照，默认约10年）
PAPER_ACCOUNT_TXT = os.path.join(BASE_DIR, "reports", "paper_account.txt")  # 第28轮纸面账户报告路径

# ================= G3（第29轮）：完整绩效指标包 / tear sheet（纯展示，不改综合分与主链） =================
METRICS_BARS_PER_YEAR = 243       # 日度口径年化周期数（国内期货约243个交易日）
METRICS_ROLLING_WINDOW = 60       # 滚动夏普窗口（个交易日）
METRICS_VAR_ALPHA = 0.05          # 历史法 VaR/CVaR 左尾概率（5%）
TEAR_MAX_POINTS = 1200            # 看板水下曲线最大绘制点数（确定性等距抽稀）

# ================= G7（第30轮）：多窗口时序动量 TSMOM(63/126/252)，本轮=影子评估阶段，绝不进综合分 =================
# 铁律：本块所有能力默认"只记录/只研究"——实时侧把影子值挂到分析行（随 signals.raw_json 落库），
# 不加入 analyzer.parts、不改变综合分一兵一卒；只有离线 tools/tsmom_eval.py 证明"确定不更差"后，
# 后续轮次才允许并入"日线动量"，且保留一键回退（TSMOM_SHADOW=False 即与本轮之前逐字节等价）。
TSMOM_LOOKBACKS = (63, 126, 252)  # 多窗口回看交易日（约1/3/6个月）；现网仅 ret5/ret20 短窗，补中长期趋势
TSMOM_ANN = 252                   # 波动标准化的年化交易日（z=窗口累计收益÷窗口日收益样本std×√252，跨窗口量纲一致）
TSMOM_Z_CLIP = 3.0                # 单窗口波动调整动量的极值压缩带，防单一窗口主导等权合成
TSMOM_SHADOW = True               # 影子总开关：True=只把 tsmom_shadow 挂到分析行（不改分）；False=完全等价旧版
# ---- 离线研究侧 tools/tsmom_eval.py（不进常驻链路、可联网拉日K、零新增依赖） ----
TSMOM_FORECAST_HORIZONS = (5, 20, 60)   # 预测未来收益的持有交易日（约1周/1月/1季），用于看预测力随期限衰减
TSMOM_EVAL_DAYS = 1023            # 拉取日K根数（新浪主连固定上限1023，足以覆盖最长252窗口+60预测+暖机）
TSMOM_EVAL_WORKERS = 6            # 并发拉取品种数（与 BACKTEST_WORKERS 同量级）
TSMOM_EVAL_MIN_SAMPLE = 120       # 单因子×单地平线 pooled 配对最小样本，不足只列数不下结论
TSMOM_EVAL_OOS_RATIO = 0.3        # IC加权合成的样本外占比（后30%为OOS），防IC权重自欺
TSMOM_EVAL_RIC_GATE = 0.02        # "可考虑并入"判据门槛：主地平线 |RankIC| 下限（弱有效阈值，宁严勿滥）
TSMOM_EVAL_FILE = os.path.join(BASE_DIR, "reports", "tsmom_eval.txt")
TSMOM_EVAL_JSON = os.path.join(BASE_DIR, "reports", "tsmom_eval.json")

# ========= G7（第31轮）：截面动量多空 XSMOM——时序动量的"截面替代"，本轮=离线评估，绝不进综合分 =========
# 与第30轮"品种内时序动量 TSMOM（自己过去预测自己未来，已被证伪）"不同：XSMOM 在每个调仓日跨全部
# 品种按长窗波动调整动量 z 排序，做多最强一档、做空最弱一档，构建市场中性多空组合，赚相对强弱的钱、
# 对冲全市场同涨同跌（第30轮 pooled 弱正主要来自这一截面成分，本轮把它纯净提取出来单独检验）。
# 铁律同第30轮：本轮只做离线 tools/xsmom_eval.py 证据评估，不碰 analyzer/cross_section 主链与综合分，
# 证明"确定不更差"后，后续轮次才允许在 cross_section 挂长窗动量排序影子，且保留一键回退。
XSMOM_LOOKBACKS = (20, 63, 126, 252)   # 截面排序回看窗（20=短窗对照，63/126/252=1/3/6月长窗）
XSMOM_HORIZONS = (5, 20, 60)           # 调仓持有交易日 H（约1周/1月/1季）
XSMOM_MAIN_L = 252                     # 主组合回看窗（对齐第30轮 z252，便于直接对照）
XSMOM_MAIN_H = 20                      # 主组合持有期
XSMOM_N_Q = 5                          # 截面分档数（Q1最弱…Q5最强，多 Q5 空 Q1）
XSMOM_MIN_NAMES = 16                   # 调仓日最少可得品种数（不足则跳过、不硬凑组合）
XSMOM_MIN_SECTOR_NAMES = 6             # 板块内单独做截面多空所需的最少品种数（不足不列）
XSMOM_OOS_RATIO = 0.30                 # 样本外占比（按调仓日排序，后 30% 为 OOS，防自欺）
XSMOM_TMIN = 1.5                       # 净多空 t 统计门槛（非重叠调仓期，宁严勿滥）
XSMOM_MONO_GATE = 0.75                 # 分档单调性门槛（5 档至少 3/4 个相邻档递增）
XSMOM_MAX_SECTOR_DRIVE = 0.60          # 单一板块对多空腿的最大贡献占比（超过即判板块偏置）
XSMOM_EVAL_DAYS = 1023                 # 拉取日K根数（与 TSMOM_EVAL_DAYS 同口径，结果可直接对照）
XSMOM_EVAL_WORKERS = 6                 # 并发拉取品种数
XSMOM_EVAL_FILE = os.path.join(BASE_DIR, "reports", "xsmom_eval.txt")
XSMOM_EVAL_JSON = os.path.join(BASE_DIR, "reports", "xsmom_eval.json")
# ---- 第32轮：截面动量条件化（板块池/多头腿）+ 双样本稳健硬检验（仍纯离线、不进综合分） ----
XSMOM_ROBUST_DAYS = 2500               # 第二样本（长）日K根数：一次拉满，内存截最近 EVAL_DAYS 得短样本，同源可比
XSMOM_COND_MIN_NAMES = 8               # 板块池条件化时调仓日最少品种数（板块品种少，需≥2×分档且留余量）
XSMOM_DECAY_TOL = 0.5                  # 双样本稳健容差：长样本净 t 不得比短样本低过该值（防近4年regime偶然）
XSMOM_LONG_N_RATIO = 1.5              # 长窗非重叠期数须≥短窗×该倍数：板块品种上市晚、长窗无增量时不算双样本（防同源小样本冒充稳健）
# 条件化候选：(名称, 板块池None=全市场 或 板块元组, 腿模式 ls=多空/lex=多头超额(long-全市场等权)/long=纯多头)
XSMOM_COND_CANDIDATES = (
    ("全市场·多空(基线)", None, "ls"),
    ("有色内·多空", ("有色",), "ls"),
    ("农产品内·多空", ("农产品",), "ls"),
    ("有色+农产品池·多空", ("有色", "农产品"), "ls"),
    ("剔除能化·多空", ("黑色", "有色", "贵金属", "农产品"), "ls"),
    ("全市场·多头超额", None, "lex"),
    ("有色+农产品·多头超额", ("有色", "农产品"), "lex"),
    ("全市场·纯多头(含beta)", None, "long"),
)

# ============ G28（第35轮）：因子收益归因 + BHB 板块归因（研究/复盘侧，只读DB，不改综合分） ============
# 样本=signals.parts_json ⨝ signal_outcomes 的方向化事件；因子暴露=part×信号方向（meta-labeling，同 factor_eval）。
# 加法归因 mean(y)=α+Σβ·mean(x) 严格闭合；BHB 三效应 AR+SR+IR=组合−基准 严格闭合。仅 tools/attribution.py 使用。
ATTR_HORIZONS = (30, 120, 1440)       # 三个评估周期（30分钟/2小时/次日）
ATTR_MAIN_HORIZON = 1440             # 主周期=次日（最接近日级因子收益，累计曲线按它出）
ATTR_MIN_SAMPLE = 40                 # 单周期最小有效事件数，不足只计数不下结论
ATTR_OOS_RATIO = 0.30                # IS/OOS β方向一致性检验的后30%占比（防过拟合）
ATTR_X_EPS = 0.05                    # |方向化暴露|超过它才算因子"支持/反对"这条信号
ATTR_FACTOR_ORDER = (                # 综合分9个part的规范顺序与中文名（动态"原油联动(w=..)"归一到原油联动）
    "新闻消息面", "原油联动", "机构动向", "日线动量", "技术共振",
    "分钟共振", "盘中动量", "量仓资金", "基本面",
)
ATTR_FILE = os.path.join(BASE_DIR, "reports", "attribution.txt")
ATTR_JSON = os.path.join(BASE_DIR, "reports", "attribution.json")
ATTR_CURVE = os.path.join(BASE_DIR, "reports", "attribution_curve.csv")

# ============ G21（第36轮）：标准研究面板 + 特征注册表 + PIT/训练-服务一致性（研究侧地基） ============
# 面板=品种×交易日×字段的统一离线研究底座，独立 SQLite（cache，gitignore），不碰生产 monitor.db 表结构、不接 main。
# 逐字段只用 ≤当日 的bar前缀经 futures_data.compute_indicators 计算（与实时同一函数=训练-服务一致），绝不取未来。
PANEL_DB = os.path.join(BASE_DIR, "cache", "research_panel.db")  # 独立缓存库，删文件即回退现拉
PANEL_DAYS = 1023               # 默认拉取交易日数（与 xsmom/carry 主样本对齐约4.1年）
PANEL_WARMUP = 10               # compute_indicators 至少10根，之前的日期不入面板
# 面板逐日落库的 compute_indicators 扁平标量字段（嵌套 tech/vol_cone 不入库，需要时由注册表回溯）
PANEL_FEATURE_KEYS = ("day_chg", "hv20", "hv60", "ma5", "ma10", "ma20", "atr",
                      "ret5", "ret20", "ret63", "ret126", "ret252",
                      "tsmom63", "tsmom126", "tsmom252", "tsmom_blend", "tsmom_n_valid")
PANEL_RAW_KEYS = ("o", "h", "l", "c", "v", "oi")   # 原始OHLC+成交量+持仓量(p)
PANEL_MANIFEST = os.path.join(BASE_DIR, "reports", "research_panel_manifest.txt")
PANEL_PARITY_SAMPLE = 24        # 训练-服务一致性抽样的每品种时点个数（均匀抽样）

# ============ G29（第37轮）：因子体检 factor health（研究/监控记录层，不改交易、不接main） ============
# 两层：①事件层=信号9part⨝signal_outcomes 的滚动IC/失效预警/block bootstrap/regime；②日频层=读G21面板算IC衰减半衰期。
HEALTH_HORIZONS = (30, 120, 1440)        # 事件层三周期（同 factor_eval/attribution）
HEALTH_MAIN_HORIZON = 1440
HEALTH_ROLL_WINDOW = 60                 # 滚动IC窗口（事件个数）
HEALTH_ROLL_STEP = 20                   # 滚动步长（事件个数）
HEALTH_FAIL_WINDOWS = 3                 # 连续多少个弱/翻转窗判"失效预警"
HEALTH_IC_EPS = 0.03                    # |RankIC| 低于此=弱窗
HEALTH_DECAY_H = (1, 2, 3, 5, 10, 20, 40, 60)   # 日频层未来收益期限网格（交易日）
HEALTH_DAILY_FACTORS = ("ret5", "ret20", "ret63", "ret126", "ret252",
                        "tsmom63", "tsmom126", "tsmom252", "tsmom_blend")
HEALTH_BOOT_B = 500                     # block bootstrap 次数（确定性种子）
HEALTH_BLOCK = 20                       # 块长（保留自相关）
HEALTH_SEED = 20260903
HEALTH_N_Q = 5                          # Q5-Q1 分档
HEALTH_FILE = os.path.join(BASE_DIR, "reports", "factor_health.txt")
HEALTH_JSON = os.path.join(BASE_DIR, "reports", "factor_health.json")

# G29续（第39轮）因子体检的 regime 分层/换手稳定性/衰减形态：纯研究侧、只读 G21 面板、不接 main
REGIME_TREND_FIELD = "ret126"          # 用面板已PIT落库的126日动量判牛/熊/震荡
REGIME_TREND_FLAT = 0.02               # |ret126|<2% 判为震荡(flat)，否则 up/down
REGIME_VOL_FIELD = "hv60"              # 用60日历史波动率判高低波
REGIME_VOL_LOOKBACK = 120              # 波动率 regime 用过去120日 ts_rank（只用过去、PIT；纯Py滚动秩，窗不宜过大）
REGIME_VOL_LOW = 1.0 / 3.0             # ts_rank<1/3=低波
REGIME_VOL_HIGH = 2.0 / 3.0            # ts_rank>2/3=高波，其间=中波
REGIME_HORIZONS = (5, 20)              # regime 分层 IC 的未来持有期（交易日）
REGIME_TURNOVER_LAGS = (1, 5, 20)      # 因子秩自相关/隐含换手的再平衡间隔（交易日）
REGIME_RANK_WIN = 20                   # 换手稳定性用的短滚动 ts_rank 窗（纯Py O(n·win)，取短窗）
REGIME_MIN_N = 40                      # 单 regime 桶最少样本，不足不给 IC
REGIME_DECAY_H = HEALTH_DECAY_H        # 衰减形态拟合复用日频期限网格
REGIME_FILE = os.path.join(BASE_DIR, "reports", "factor_regime.txt")
REGIME_JSON = os.path.join(BASE_DIR, "reports", "factor_regime.json")


# ---------------- G10 配置外置：config.json 深合并覆盖（缺文件=与历史逐字节一致） ----------------
# 只覆盖本文件已定义的全大写可调常量（阈值/开关/账户/自选等），路径类与未知项受保护跳过；
# 类型不符的项保留内置默认并记入报告，绝不抛异常中断启动。可用 FUTURES_MONITOR_CONFIG 指定其它文件。
from config_loader import load_config_file as _load_config_file, apply_overrides as _apply_overrides
_CONFIG_PATH = os.environ.get("FUTURES_MONITOR_CONFIG", os.path.join(BASE_DIR, "config.json"))
_cfg_obj, _cfg_err = _load_config_file(_CONFIG_PATH)
CONFIG_OVERRIDE_SOURCE = _CONFIG_PATH if _cfg_obj is not None else None
CONFIG_OVERRIDE_REPORT = _apply_overrides(globals(), _cfg_obj) if _cfg_obj is not None else {"applied": {}, "skipped": {}}
if _cfg_err:
    # 文件损坏只记录、不中断：全部沿用内置默认
    CONFIG_OVERRIDE_REPORT["skipped"]["__file__"] = _cfg_err


