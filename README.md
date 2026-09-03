# 期货全品种监控分析程序

根据你的需求实现的常驻监控程序：

| 需求 | 实现 |
| --- | --- |
| ① 新浪财经+金十数据实时新闻，每60秒抓取生成判断因子；布伦特/纽约原油每10秒刷新 | `sina_news.py`（60s）、`oil_data.py`（10s，后台线程） |
| ② 每次运行自动打开同花顺期货通；分析上期所/大商所/郑商所/广期所**全部品种和对应期权** | `ths_app.py`（启动时自动打开）、`config.VARIETIES`（64个品种）、`contracts.py`（主力合约月份自动探测） |
| ③ 购买建议写清楚是哪个时间段的合约和对应期权 | 期货建议标注具体合约（如"做多 rb2610"）；期权建议标注月份+执行价+代码示意（如"买入看跌期权（2701月份·平值·执行价≈5650），参考代码 TA701P5650"） |
| ④ 期权分析比期货更严格（时间价值、标的价格、隐含波动率等） | `option_analyzer.py`（Black-76定价 + 希腊字母 + 六项检查） |
| ⑤ 接入交易可查(机构多空看法)+OpenVlab(期权数据)，每10秒刷新，纳入综合分析 | `webdata.py`（10秒线程）：交易可查 `/api/v2/aireport` 机构研报看多/震荡/看空统计 → 综合分新增"机构动向"因子(±2)；OpenVlab `/api/product-exps` 真实期权挂牌月份与到期日 → 期权月份校验+精确剩余天数 |
| ⑥ 学习期权策略并推荐**适合的期权策略**（只给期权策略）+严格分析 | `option_strategies.py`：单腿、牛/熊市价差、跨式、铁鹰、蝶式、1:2比率、备兑看涨、保护性认沽；严格检查方向/机构配合/隐波/幅度/到期/风险边界，输出买卖腿、盈亏结构、组合Greeks和保证金点值估算 |
| P1-⑦ 主动告警 | `alerts.py`：本机声音提醒；可选飞书/钉钉/企业微信/Server酱/通用 Webhook；紧急轮动、跨4分/6.5强信号、多空翻转、期权策略全通过均带冷却；同一轮多事件自动汇总，只响一次最高级别提示音、只发一条汇总 Webhook |
| P1-⑧⑨ SQLite 结构化数据库 + 信号胜率追踪 | `storage.py`：标准库 sqlite3，落库行情/新闻/非中性期货信号/期权；信号自动在30分钟、2小时、约24小时后回填方向收益并统计胜率 |
| P1-⑪ 量仓资金因子 | `flow_tracker.py`：识别增仓上行/增仓下行/减仓回补、放量/缩量，作为“量仓资金”因子加入综合分 |
| P1-⑫ 技术指标与多周期共振 | `futures_data.py`：RSI14、MACD(12,26,9)、KDJ(9,3,3)、BOLL(20,2)、MA60；日线短/中/长投票形成“技术共振”（±1.2），并接入新浪30分钟K线、两根30m聚合60m，形成“分钟共振”（±0.4）确认/背离 |
| P1-⑬ 新闻否定/转折识别 | `factors.py`：关键词命中点局部识别“并未/没有/落空/证伪/取消/推迟/不及预期”等，自动反转极性，避免“减产预期落空”被误判为利多 |
| P1-⑭ 期权IV分位/偏度/组合Greeks/保证金 | OpenVlab页面解析真实平值IV、隐波百分位、偏度；无页面时用HV历史分位和波动率锥代理；组合输出Δ/Γ/Vega/Θ，并对卖方/备兑/比率结构给保守保证金点值估算 |
| P1-⑩ 最小日线回测 | `backtest.py`：零新增运行依赖，主连比例复权；默认读取`data/futures_fees.csv`的真实券商手续费（按金额+按手数，开仓+平仓），另扣滑点、过滤疑似锁涨跌停、输出非重叠交易CSV和3×3参数稳定性扫描，同时保留1/5/20日信号衰减 |
| 第27轮 G1 纸面交易引擎（一）表+撮合 | 新增 `paper_broker.py`（PaperBroker：复用 portfolio.Portfolio 账户内核，close/next 两档成交、next 严格晚于信号，三阈值迟滞开/平/反手、实时锁板、滑点入价+真实费率、临时约束顺延/信号消失撤单、重启由三表恢复）；storage 增第12–14张业务表 `paper_orders/paper_trades/paper_equity`；config 增 PAPER_* 20 项、PAPER_ENABLED 默认 False 休眠（本轮不接 main，第28轮接账户+看板）；18 零网络用例 + selftest 25 断言、真实64品种5轮冒烟 |
| 第26轮 G4 回测严谨性 | `backtest.py` 增 `--fill {close,next_open}` 成交时点双档（默认close旧口径，next_open次根开盘保守对照，锁板顺延/末根信号不虚构/反手先平后开）、固定种子交易级 bootstrap 1000 次给累计与回撤 P5–中位–P95（总体+多空）、`--oos-ratio` 样本内外分段、`--impact-rate` 冲击成本单列；每次运行落 storage 第11张业务表 `backtest_runs` 留档并在抬头给历史百分位；`tools/backtest_validation.py` 同步产出 `backtest_validation.json` sidecar（DSR/PBO）供回测抬头交叉引用 |
| 第15轮 日内/平今回测（WP-D1/D2） | `intraday_backtest.py`：零新增依赖，回放自采`minute_bars`分钟库（1/5/15/30/60m，可选1m边界对齐后聚合交叉验证）；vnpy式bar内保守撮合（信号收盘确认、下一根开盘成交，止损/止盈预埋单、同根双触按止损、跳空以开盘成交）；按【交易所结算交易日】判定平今/平昨（前晚夜盘+次日日盘为同一交易日），开open/平today或close走真实券商费率并输出每手人民币与平今对照；精确锁板（前收×品种常态涨跌停、整根封死才拦截）；日内模式日终强平不隔夜，摆动模式可跨日；输出逐笔CSV与入场×止损×止盈18组稳定性网格，看板新增两页签 |
| 第16轮 组合资金账户/权益曲线（WP-E） | `portfolio.py`：零新增依赖，多品种【共享一个资金池】统一时间轴回放（分钟复用intraday_backtest信号/日线复用backtest信号，撮合口径完全一致）；按`data/futures_margins.csv`真实公司保证金率逐bar盯市算静态/动态权益、保证金占用、可用资金、风险度；三种手数分配（等名义/等风险ATR/按综合分档）并受单品种与板块名义上限、可用资金、同时持仓数共同约束；风险度破线按浮亏最大优先强平、降到安全线为止；输出组合净值/年化/回撤/夏普/索提诺、权益曲线CSV、含手数与强平标记的逐笔成交CSV，看板新增两页签。顺带修复新浪日K主连代码（RB00→RB0，接口已不再认双零）导致backtest日线回测退化为"样本不足"的问题 |
| 第11轮 期权完整链/PCR + 期限结构 | `option_chain.py`：新浪商品期权T型报价（五大所57个期权品种实测，零新增依赖），输出每腿买卖量/最新价/持仓量、**持仓量PCR（认沽/认购比）**、ATM定位、最大持仓行权价（支撑/压力）、PCR情绪档与近30日分位，30分钟缓存、每轮6线程并发预热、快照落`option_chains`表；`contracts.term_structure` 用全月份合约零额外请求组装近远月价差/年化展期收益率/正向(Contango)-反向(Back)结构（本轮只展示，不进综合分） |
| 第12轮 多到期日IV曲面 + 日历价差 | `iv_surface.py`：对最近3个真实挂牌月份（≥30天）的新浪T链逐腿 Black-76 二分反推IV，输出 ATM IV期限结构(Contango/Backwardation)、25Δ风险反转/蝶式、5档moneyness×多月份曲面矩阵，含窄价差质量分级/离群过滤/缺档不插值；`option_strategies.py` 新增日历价差(卖近买远,正Vega)/反向日历(买近卖远)，|近-远ATM IV差|≥3vol且静态盈亏空间为正才推荐；`option_analyzer.py` IV改三级优先级(页面真实>T链反推>HV估计)并交叉校验；多月份链快照各落一行`option_chains`表，零新增依赖 |
| 第13轮 基本面数据包（库存/仓单+龙虎榜+期限carry+基差） | 新增 `fundamental_data.py`（东财数据中心直连，零新增依赖）+`fundamental_factors.py`（纯函数）：东财注册仓单近3个月时序算滚动分位+周环比去化、东财龙虎榜前20席会员多空合计净多率与边际、复用期限结构年化carry（Back近高远低=现货紧偏多）、生意社基差（遇JS-cookie反爬自动降级）；四子项按权重.40/.30/.20/.10加权、缺项自动重归一，作为“基本面”因子(±1.5)加入综合分；日频后台线程刷新、分析前8线程并发预取龙虎榜，快照落新增`fundamentals`表，报告加【基本面速览】多空榜 |

| 第14轮 分钟K自采库（WP-D0，为日内/平今回测积累自有分钟数据） | `intraday_bars.py`（**新浪主连为主+东财补1m+通达信可选冗余**的三源选源、周期聚合纯函数）+新增 `tdx_bars.py`（pytdx延迟导入、启动并发探测、取不到期货零成本降级）；`storage`新增第8张表`minute_bars`（唯一键去重、保留400天）；`main`启动小回填+后台线程交易5分钟/非交易30分钟增量自采；实测回填261888根（64品种×5/15/30/60m×1023根），60m回溯约12.5月。选型证据见《数据源选型与通达信替换可行性分析.md》 |

> ⚠️ 免责声明：本程序输出由公开数据与规则引擎自动生成，仅供学习研究参考，不构成任何投资建议。期货及期权杠杆交易风险极高，据此操作风险自负。

---

## 一、在 PyCharm 中运行

1. 打开 PyCharm → `File → Open` → 选择文件夹 `Desktop\量化\futures_monitor`
2. 右键运行 `main.py`（依赖 requests、uiautomation 已装好；换环境时 `pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`）
3. 也可以直接双击 `start_monitor.bat`

### 命令行参数

| 参数 | 作用 |
| --- | --- |
| （无） | 常驻运行：新闻60s/原油10s；交易时段前30分钟5分钟一轮、之后20分钟一轮，非交易时段1分钟一轮 |
| `--once` | 只跑一轮分析后退出（测试用） |
| `--no-launch` | 启动时不自动打开同花顺期货通 |
| `--force-review` | 立即生成当日复盘报告（测试用，正常情况下在归属交易日全部夜盘结束后自动生成） |

单独运行最小回测（不启动常驻监控）：

```powershell
D:\Python\python.exe backtest.py --codes RB0,MA0 --days 250 --hold 10
D:\Python\python.exe backtest.py --all --days 250
# 默认读取data/futures_fees.csv真实手续费，另加滑点万1；敏感性测试可回退统一费率/关闭成本/锁板过滤/参数网格
D:\Python\python.exe backtest.py --codes RB0,MA0 --no-real-fees
D:\Python\python.exe backtest.py --codes RB0,MA0 --no-cost --no-limit-filter --no-stable
# G4 严谨性对照（第26轮）：
D:\Python\python.exe backtest.py --all --fill next_open        # 保守档：信号次根开盘成交(默认close=信号根收盘)
D:\Python\python.exe backtest.py --all --oos-ratio 0.3         # 后30%交易为样本外OOS，与前70%IS并列对照
D:\Python\python.exe backtest.py --all --impact-rate 0.00005   # 另计单边万0.5冲击成本(往返两次,默认0)
D:\Python\python.exe backtest.py --all --no-bootstrap --no-archive --no-validation-ref  # 关区间/留档/DSR引用
# 每次运行默认落 backtest_runs 留档表并在抬头标注历史百分位；bootstrap固定种子(可--seed)可复现，交易<20笔不给区间
```

单独运行日内/平今回测（读常驻自采的分钟库，不联网）：

```powershell
D:\Python\python.exe intraday_backtest.py --all --period 30                 # 全品种30m日内模式(默认)
D:\Python\python.exe intraday_backtest.py --codes RB,CU --period 5          # 指定品种/中文名/RB0均可
D:\Python\python.exe intraday_backtest.py --codes RB --period 5 --aggregate-from 1  # 1m边界对齐聚合5m交叉验证
D:\Python\python.exe intraday_backtest.py --all --period 30 --swing --max-bars 32   # 摆动模式(允许跨交易日,出现平昨)
D:\Python\python.exe intraday_backtest.py --all --period 30 --no-cost       # 零费零滑看纯信号毛收益
```

单独运行组合资金账户回测（多品种共享资金池、真实保证金占用与风控强平）：

```powershell
D:\Python\python.exe portfolio.py --all --period 30                  # 全品种30m分钟组合(默认等名义15%/品种)
D:\Python\python.exe portfolio.py --daily --all --days 250           # 日线组合(信号口径同backtest)
D:\Python\python.exe portfolio.py --all --period 30 --sizing equal_risk       # 等风险:1%风险预算×1.2ATR止损定手数
D:\Python\python.exe portfolio.py --all --period 30 --sizing score            # 按综合分轻仓/分批/强信号分档定手数
D:\Python\python.exe portfolio.py --all --period 30 --equity 500000 --per-symbol 0.2 --max-concurrent 8
D:\Python\python.exe portfolio.py --all --period 30 --risk-liquidate 1.0 --risk-safe 0.8   # 强平线/安全线可调
# 输出 reports/portfolio_report.txt + portfolio_equity.csv(逐bar权益/风险度) + portfolio_trades.csv(含手数/强平)
# 保证金表缺失品种回退默认率并在报告抬头显式列出；高价品种15%名义买不起1手时如实计入"未开仓原因分布"
D:\Python\python.exe portfolio.py --all --period 30 --calibrate   # WP-F2：按历史同类信号胜率给手数乘子(默认关闭)
```

WP-F2 研究侧工具（离线、读自有DB、零网络、不进常驻链路）：

```powershell
D:\Python\python.exe tools\factor_eval.py --days 9999        # 九因子 IC/RankIC/ICIR/分档单调性/walk-forward，出 reports/factor_eval.txt
D:\Python\python.exe tools\build_ml_samples.py --all --period 30   # triple-barrier三分类标签+PIT特征快照落第9表 ml_samples
D:\Python\python.exe tools\build_ml_samples.py --codes RB --no-db --audit  # 单品种试跑/不写库/PIT无穿越审计
D:\Python\python.exe tools\factor_eval.py --selftest         # 零网络合成断言(单调RankIC≈1/无关≈0)
D:\Python\python.exe tools\attribution.py               # 第35轮G28：因子收益归因+BHB板块归因，出 reports/attribution.txt/.json/_curve.csv
D:\Python\python.exe tools\attribution.py --selftest     # 零网络/零DB断言(加法归因闭合/BHB三效应恒等式)
D:\Python\python.exe tools\panel_builder.py --all --days 1023   # 第36轮G21：标准研究面板(品种×交易日×30字段)落独立缓存 cache/research_panel.db
D:\Python\python.exe tools\panel_builder.py --codes RB0,MA0,CU0 # 只建指定品种；--no-fund 关基本面PIT拼接；--selftest 零网络断言
D:\Python\python.exe tools\pit_audit.py --db cache\research_panel.db   # G21：缓存面板结构/PIT审计(零网络)；--codes RB0 联网做实时/离线parity
D:\Python\python.exe tools\tsmom_eval.py --panel     # 第37轮G21续：tsmom/xsmom/carry 加 --panel 读已复权面板(缺省仍联网现拉，逐值等价)
D:\Python\python.exe tools\factor_health.py          # 第37轮G29：因子体检卡(事件层滚动IC/块bootstrapCI/失效预警+日频层IC衰减半衰期)，出 reports/factor_health.txt/.json
D:\Python\python.exe tools\factor_health.py --selftest   # 零网络/零DB断言(正负IC裁决/噪声不误判/失效预警/半衰期拟合)
D:\Python\python.exe tools\expr_research.py          # 第38轮G25：表达式因子研究台(纯离线读面板)，实时/离线同表达式parity+前向RankIC
D:\Python\python.exe tools\expr_research.py --selftest   # 零网络断言(面板/bar parity=0、表达式动量==实时ret5、前向无未来)
D:\Python\python.exe factor_expr.py --selftest       # G25表达式引擎自测(白名单安全/时序截面算子手算/OLS正交/IC加权)
D:\Python\python.exe tools\factor_regime.py         # 第39轮G29续：因子regime分层(牛熊×高低波IC)+换手稳定性+指数vs幂律衰减形态
D:\Python\python.exe tools\factor_regime.py --selftest  # 零网络断言(PIT标签/分层IC/持续性/衰减形态择优与安全降级)
D:\Python\python.exe tools\build_ml_samples.py --selftest    # 止盈/止损/同根双触/跳空/超时/PIT/embargo 断言
D:\Python\python.exe tools\backtest_validation.py --selftest           # DSR/CSCV-PBO/PurgedKFold/Walk-forward/参数高原 断言
D:\Python\python.exe tools\backtest_validation.py --grid RB --period 30 # 单品种18组参数网格样本外验证
D:\Python\python.exe tools\backtest_validation.py --all-grid            # 全品种，出 reports/backtest_validation.txt + 同名 .json sidecar（供backtest抬头引用DSR/PBO）
```

### 启动流程

程序启动后会依次做三件事（约1~2分钟后出第一份报告）：
1. **自动打开同花顺期货通**（已运行则跳过；路径 `config.THS_EXE`）
2. **探测64个品种的主力合约月份**（按未来8个月各月份合约的成交量+持仓量排序，约20~40秒）
3. 第一轮全品种分析（首次需预取64个品种的日线，约30~60秒；之后走缓存）

---

## 二、分析范围与合约月份

- **范围**：上期所17个、上期能源5个（原油/20号胶/低硫燃油/国际铜/集运欧线，通常与上期所同页显示）、大商所20个、郑商所19个、广期所3个，共 **64个品种**；其中有场内期权的40个品种会额外做期权严格分析。中金所股指国债不在范围内（如需可在 `config.ANALYZE_EXCHANGES` 增删）。
- **主力合约探测**：`contracts.py` 每30分钟重新探测一次。对每个品种枚举未来8个月的具体合约（`nf_RB2701` 等），按"成交量+持仓量"排序取最活跃月份作为主力。购买建议直接写明合约，如：
  - 期货：`方向:做空 ss2610 | 参考开仓 13,950 | 止损 14,190(1.2×ATR) | 目标 13,549(2×ATR)`
  - 期权：`买入看跌期权（2701月份·平值·执行价≈5650）| 参考代码 TA701P5650（示意）`
- **期权月份自动顺延**：若主力月份期权临近到期（估算剩余<14天），自动改推荐下一个活跃月份并在报告中说明。
- **交割月提示**：主力合约距交割月不足45天时，报告自动提示"临近交割注意移仓换月"。
- 注意：建议中的执行价按近似档位取整、期权代码为示意格式（郑商所3位月份如 TA701、大商所 m2701-C-5650、上期所 ru2701C18900），**实际交易前请以期货通/交易所挂牌的合约和执行价为准**。
- **期限结构（第11轮组装、第13轮carry入分）**：重点品种明细用同一次月份探测的数据（零额外请求）按日历月排列全部月份合约，给出近-远月价差、年化展期收益率和正向(Contango，近低远高)/反向(Back，近高远低)结构；其年化展期收益率（carry）自第13轮起作为基本面四子项之一进入综合分（反向市场=现货偏紧→偏多）。

---

## 三、数据源与刷新频率

| 数据 | 来源 | 频率 |
| --- | --- | --- |
| 新浪财经7x24全球直播 | `zhibo.sina.com.cn/api/zhibo/feed` | 60秒 |
| 金十数据快讯 | `jin10.com/flash_newest.js` | 60秒 |
| **全网扫描·新闻/突发事件**（东财7x24、新浪财经滚动(财经+全球)、华尔街见闻、同花顺7x24，单点失败互不影响） | `web_scan.py` 多源聚合，约140条/轮，自动去重 | **3分钟** |
| **全网扫描·金融数据**（美元指数、纽约黄金/白银、美铜、纳指/道指/标普、上证/深成） | 新浪全球行情 `hq.sinajs.cn`；指标3分钟内急变超 `WEB_MACRO_THRESHOLDS` 即合成一条"实测金融消息"进入同一分析管线 | 3分钟 |
| 布伦特原油/纽约原油 | 新浪 `hq.sinajs.cn/list=hf_OIL,hf_CL` | 10秒 |
| **原油急动紧急轮动** | 10秒原油行情中，布伦特/WTI 任一在 `OIL_JUMP_WINDOW_SEC`(默认60秒) 内涨跌幅≥`OIL_JUMP_REL`(默认0.6%) 即判定"变化过大"，**立即"插队"跑一整轮：全部品种（不只是能化链）按实时数据重新分析、期权重算、五个报告文件同步写入、看板10秒内刷新**；冷却 `OIL_JUMP_COOLDOWN_SEC` 默认180秒，阈值都在 config.py 可调。**原定的下一轮轮动时刻不重算、不推移**，紧急轮结束后继续等到原计划点（多次急动可多次插队） | 触发时 |
| **全网消息紧急轮动** | 3分钟全网扫描中出现**新的**高影响消息：全局影响权重≥`WEB_IMPACT_TRIGGER`(默认1.5)，或源标"重要"/含突发词且≥`WEB_IMPORTANT_TRIGGER`(默认1.0)，处理方式与原油急动**完全相同**（全品种实时重算、全部报告同步写入、看板刷新、计划点不推移），报告块头标 `[全网消息紧急轮动]` 并列出直接影响品种 | 触发时 |
| **机构多空看法**（各期货公司研报观点统计） | 交易可查 `jiaoyikecha.com/api/v2/aireport`（公开接口） | 10秒 |
| **期权真实挂牌月份与到期日** | OpenVlab `openvlab.cn/api/product-exps`（公开接口） | 30分钟 |
| **浏览器页面直读**（真实平值隐波/隐波榜单/波动率溢价榜/头条多空动向） | 本机浏览器(调试端口9222)中打开的两个网页，`browser_reader.py` 经CDP读取 | 30秒 |
| 全品种主力连续+各月份合约行情 | 新浪 `nf_XXX`（自动分批） | 每轮60秒 |
| 日线K线（HV/ATR/均线/RSI/MACD/KDJ/BOLL/波动率锥） | 新浪 `InnerFuturesNewService.getDailyKLine` | 30分钟缓存，后台预刷新；`backtest.py`按需拉取 |
| 30分钟K线（两根30m本地聚合60m，做分钟共振） | 新浪 `InnerFuturesNewService.getFewMinLine?type=30` | 10分钟缓存，每轮分析前6线程并发预热，失败只降级不阻断 |
| **分钟K自采库 minute_bars**（1/5/15/30/60m，供第15轮日内/平今回测） | 主源新浪主连 `getFewMinLine`（5/15/30/60m各1023根、64品种全覆盖、零断连）；1m由东财`push2his`具体合约补位（限流时熔断自愈）；通达信`pytdx`为可选冗余（公共口无期货时自动不启用） | 启动全量回填+交易5分钟/非交易30分钟增量，唯一键去重、保留400天 |
| **商品期权完整T型链/PCR**（每行权价的买卖量/最新价/持仓量，持仓量PCR、ATM、最大持仓行权价） | 新浪 `OptionService.getOptionData`（SHFE/INE/DCE/GFEX品种带`_o`后缀、CZCE无后缀；月份取自OpenVlab日历） | 30分钟缓存，期权分析前6线程并发预热，单品种失败只降级；快照落`option_chains`表积累PCR分位 |
| **基本面日频数据**：注册仓单/库存时序、龙虎榜前20席会员多空合计、现货基差 | 东财数据中心 `datacenter-web.eastmoney.com`（RPT_FUTU_STOCKDATA/RPT_FUTU_POSITIONCODE/RPT_FUTU_DAILYPOSITION，零新增依赖）；基差走生意社（有反爬，抓不到自动降级）；carry复用上面的月份行情零请求 | **日频**：收盘16点后后台8线程并发刷新一次，龙虎榜按主力合约分析前并发预取、当日缓存，不拖慢实时轮；快照落`fundamentals`表 |
| **期货保证金率/板幅**（组合账户占用与风险度） | **银河期货**官网"结算时起各品种最新交易保证金比例"静态表（用户开户公司、投机档，最贴近实盘）；`tools/build_margin_table.py`半自动解析（支持`--url`指最新期页面或`--html`离线另存解析），生成`data/futures_margins.csv`；乘数为每手报价单位个数口径（鸡蛋报价元/500kg故=10），与手续费表交叉校验 | 公司调整时手动重跑工具（非实时接口） |

说明：交易可查的席位级"机构持仓明细"需要登录/VIP，程序接入的是其公开的机构观点统计；
OpenVlab市场页的隐波排行走内部POST接口暂无法稳定获取，程序以历史波动率估计IV并全程标注"估"，
实盘请以盘面隐波为准。全网扫描优先直连权威站点公开接口（比搜索引擎抓取稳定）；每条消息带
**可信度系数**：权威快讯/实测金融数据≈1.0，一般转载媒体0.75~0.9，含"传闻/据称/网传/未经证实/
疑似"等存疑词的消息系数×0.4并在报告Top消息前标"存疑·"，打分与排序自动靠后（真实的优先、
存疑的决定因素往后排）。

---

## 四、判断因子与期货建议规则

每个品种的**综合分（-10 ~ +10）** 由九类因子加总：

1. **新闻消息面**（±4）：约70个关键词 + 15条数据类正则（EIA/API库存、PMI、非农、CPI、USDA、OPEC），按板块加权；带2.5小时半衰期、重要快讯×1.6、品种名命中×1.5、**可信度系数（真实源全额、存疑消息×0.4后排）**；易误伤的泛词有"上下文闸门"过滤。
2. **原油联动**（±5×权重）：原油5/15/60分钟动量+较昨结涨跌（布伦特60%+WTI40%），按品种联动权重传导（SC 1.0、燃油/沥青0.9、甲醇/PTA 0.5、PP/塑料0.4、玻璃0.1、黑色有色农产品0）。
3. **机构动向**（±2，交易可查）：AI研报统计的各期货公司看多/看空家数比 → `tanh(多空比×2)×2`，机构观点少于3家不计入。
4. **日线动量**（±4.5）：5日/20日收益 + 现价相对MA10位置。
5. **日线技术共振**（±1.2）：短周期（MA5/5日动量/KDJ）、中周期（MA20/MACD）、长周期（MA20/MA60趋势）分别投多/空/中性票，三周期同向给满分；RSI超买/超卖只作风险提示，不机械反向。
6. **分钟共振**（±0.4）：30分钟K线看短/中周期，两根30m聚合出的60分钟K线看中/长周期；分钟方向与综合分背离时进入风险提示，数据缺失时自动降级为0，不阻断报告。
7. **盘中动量**（±1.5）：运行期间10分钟/30分钟价格变化。
8. **量仓资金**（±1.2）：相邻轮次比较价格、日累计成交量和持仓量。增仓代表新资金入场，权重高于减仓回补；本轮成交量增量高于近几轮均值视为放量并增强，低于60%视为缩量并衰减；新交易日成交量归零、交易日切换或主力连续换月会重建基线，不会被误判为缩量/异常增仓。
9. **基本面**（±1.5，第13轮，日频更新）：四个子项方向均为"现货/主力偏紧→偏多"，权重 库存仓单0.40 / 龙虎榜0.30 / 期限carry 0.20 / 基差0.10，**某子项缺数据时按可得子项权重重新归一、不编造**：
   - **库存/仓单**：东财注册仓单近约3个月时序，60%看历史滚动分位（低库存偏多、高库存偏空）、40%看周环比（去库偏多、累库偏空），样本<15个交易日不计；
   - **龙虎榜**：主力合约前20席会员多空合计，60%看净多率(多−空)/(多+空)、40%看较昨日的边际变化；
   - **期限carry**：近月相对远月的年化展期收益率，反向市场（近高远低、现货紧）偏多，直接复用期限结构、零新增请求；
   - **基差**：现货相对期货主力升水（现货坚挺）偏多；生意社源有反爬时该子项自动缺失，其含义大部分由carry覆盖。

新闻关键词还会做局部否定/转折判断：例如“OPEC减产预期落空”“并未增产”会按反转后的方向计分；数据类正则内部已经编码方向的“低于预期”不会二次反转。

信号分级：|分|<2 观望；2~4 轻仓试探(≤20%仓位)；4~6.5 分批建仓(20%~40%)；≥6.5 强信号顺势持有(40%~60%)。止损/目标基于 ATR14（1.2×ATR / 2×ATR）。

### 4.5 WP-F1 决策增强（多空双面卡 / 横截面强弱 / 独立风控闸门 / 五维情绪）

在**不改变上述综合分与信号分级**的前提下，新增四层"信息增量 + 独立复核"（全部零新增第三方依赖的纯规则，均可在 config 一键关闭/回退）：

1. **多空双面论证卡**（`analyzer.build_debate`，明细"多空:"行）：每个重点品种同时列出"多[…] vs 空[…]"两方全部证据（九类因子、当日涨跌、量仓资金、机构净多空、基本面、波动分位），给"多方占优/空方占优/多空均衡"。结论与综合分一致，价值在于**强制摆出反方依据**，避免只报喜。
2. **横截面相对强弱**（`cross_section.py`，报告"【横截面强弱】"块）：对全部品种的综合分、当日涨跌幅分别做**稳健标准化** z=0.6745(x−中位数)/MAD（比均值/标准差更抗单个涨停极端值），综合强度=0.6 综合分z+0.4 涨跌幅z；输出板块强弱榜（含各板块涨/跌家数）、相对最强/最弱 Top5、全市场多空广度。绝对综合分回答"品种本身多强"，横截面回答"它比同期其他品种强还是弱"；**只横向比较，不回改任何 score/label/advice**。
3. **独立风控闸门**（`risk_gate.py`，与打分解耦的第二道防线，逐品种给 pass/warn/veto）：
   - **veto 建议暂缓**：无有效价或成交量低于 `RISK_GATE_MIN_VOLUME`、强信号(|分|≥6.5)与当日涨跌反向超过 `RISK_GATE_DIVERGE_CHG`(防追高/摸顶)、HV20 分位≥`RISK_GATE_HV_EXTREME`(极端波动、ATR 止损易被打穿)；
   - **warn 提示**：HV 分位≥`RISK_GATE_HV_HIGH`、信号方向与量仓资金反向、临近交割/期权到期、强信号但置信度偏低；
   - 默认 `RISK_GATE_AUTO_DOWNGRADE=False`：veto **只在期货总表标 ⛔、明细"风控:"行列原因、并走声音/Webhook 告警，并不改综合分/建议**；只有显式置 True 才把信号自动降级为"暂缓"（原 label/advice 保留备查）。`RISK_GATE_ENABLED=False` 可整体关闭。
4. **五维情绪**（`factors.sentiment_facets`）：每条命中消息在原极性分之外，额外刻画 强度/不确定性/相关性/前瞻性 四个 0~1 维度 + 事件类型（货币政策/地缘/供给/需求库存/天气/汇率股市/贸易/资金情绪），在明细消息行以角标呈现（如"·强0.9·前瞻·货币政策"）；同一词反复出现按去重计数，**不回改消息情绪主分**。

### 4.6 WP-F2 信号校准与监督学习备料（历史胜率校准 / 因子IC评估 / triple-barrier 样本）

延续 WP-F1"默认只做信息增量、可一键回退"的纪律，全部只用自有 SQLite、生产侧零新增第三方依赖：

1. **历史同类信号胜率校准（meta-labeling 轻量版，`signal_calibrator.py`）**：用 `signal_outcomes` 已评估结果，按「方向 × 综合分档 × 主导因子」分组做**贝叶斯平滑胜率**（先验 0.5、强度 `CALIBRATOR_PRIOR_N`，避免 1/1=100% 虚高），再线性映射为 sizing 乘子 `clip(1+(胜率−0.5)×slope, 0.5, 1.2)`。任一层 n<`CALIBRATOR_MIN_N`(默认20) 就向上回退（方向×分档×主导因子 → 方向×分档 → 方向 → 全局），全部不足返回乘子 1.0。
   - **默认影子模式**：实时报告只在明细"校准:"行、信号追踪页"三、历史同类信号胜率校准"展示"历史同类胜率 x%(n 笔)→乘子"，**不改综合分/信号/建议/手数**；
   - 只有 `portfolio.py --calibrate` 才把乘子真正乘到手数上（回测无九因子时自动回退到方向×分档层），成交记录带 `calib_mult`、报告抬头统计实际调整次数与平均乘子；`CALIBRATOR_ENABLED=False` 整体关闭。
2. **因子 IC 评估（`tools/factor_eval.py`，研究侧定期人工跑）**：把信号发出时的九因子拆分与 30m/2h/次日结果配对，主口径 meta RankIC=Spearman(因子值×信号方向, 方向收益)，另给 Pearson IC、月度 ICIR、5 档单调性与多空价差、30m→次日衰减、逐月 walk-forward 的 IS/OOS 同号率，最后只给**建议权重区间（相对当前权重的乘数），绝不自动改 analyzer 权重**，输出 `reports/factor_eval.txt`。
3. **triple-barrier 监督学习样本（`tools/build_ml_samples.py`，为 WP-F4 备料）**：沿用日内回测同一套信号与撮合口径（信号 i 收盘确认、i+1 开盘入场、入场当根不查、止损优先、跳空开盘成交），打上轨止盈=+1 / 下轨止损=−1 / 最长 `ML_SAMPLE_MAX_BARS` 根超时按到期方向收益定符号（走平=0）的三分类标签；特征严格 PIT（技术特征只用 ≤i、标签路径全 >i，`--audit` 扰动未来价格断言特征不变），并就近匹配同交易时段的九因子/截面稳健z/五维情绪快照；落第 9 张表 `ml_samples`（UNIQUE(sym,period,bar_dt) 可重复跑、长期保留），另提供 purged+embargo 切分函数，标签窗口横跨测试折的训练样本一律剔除。
4. **因子收益归因 + BHB 板块归因（`tools/attribution.py`，第35轮 G28，研究侧定期人工跑）**：回答复盘核心问题"已实现盈亏该记到哪个因子、哪个板块头上"。样本=信号 parts_json ⨝ signal_outcomes 的有效事件，因子暴露=part×信号方向（同 factor_eval）、y=方向收益；①带截距 OLS 做**加法归因 mean(y)=α+Σβ·mean(x)（严格闭合）**，给每因子边际收益β/t值/贡献占比/IC/支持时胜率与 IS-OOS β方向一致性；②**BHB（Brinson-Hood-Beebower）板块三效应**：相对"全市场品种板块只数占比×板块无方向均涨"基准，把超额拆成配置 AR/选择 SR/交互 IR（AR+SR+IR=组合−基准严格闭合）；③逐事件累计归因曲线落 CSV。**只归因、不自动改任何 analyzer 权重**，输出 `reports/attribution.txt/.json/attribution_curve.csv`。当前真实结论（2026-08 起样本）：短周期盈亏主要由技术共振贡献、次日主要由原油联动贡献，机构动向次日为负贡献（留待 G29 复核），超额以板块内"选择效应"为主、板块"配置效应"≈0。

5. **标准研究面板 + 特征注册表 + PIT/训练-服务一致性（第36轮 G21，研究侧地基）**：`factors_catalog.py` 是全项目因子唯一登记处（25条：综合分9 part/基本面子项/技术指标/影子·归档·待跟踪/第38轮5个表达式研究因子，含方向·贡献界·现状·实时计算处·公式，测试钉死其 part 顺序与 config 一致）；`tools/panel_builder.py` 把"主连比例复权行情+compute_indicators 技术指标+基本面快照"统一成品种×交易日×30字段标准长表，落**独立** SQLite `cache/research_panel.db`（gitignore、删文件即回退、整品种幂等重建逐值一致），**PIT** 上每行特征只用 ≤当日 bar 前缀、基本面严格取前一交易日 as-of（取不到为 NULL 不编造）；`tools/pit_audit.py` 做时间戳泄漏扫描、扰动法无未来函数（含反向用例）、**实时/离线 parity**（面板行==对同一前缀走实时 compute_indicators）与缓存结构审计。真实已建**全64品种 61353 行**、结构审计与 parity 全过。**第37轮 G21续**：panel_builder 增 `load_adjusted_bars/panel_rows_to_bars` 回读层，tsmom/xsmom/carry 三工具加 `--panel` 直接读已复权面板（**不再二次复权**——已复权序列再跑 MAD 换月检测会把真实大波动误判换月，实测 SC/J 价位偏6%~13%），缺省仍联网现拉、真实重叠点逐值等价(maxAbsDiff=0)。不接 main、不改综合分。

6. **因子体检（`tools/factor_health.py`，第37轮 G29，研究侧定期人工跑）**：给每个因子一张"健康卡"，回答现在还有没有力、稳不稳、衰减多快。**事件层**（信号 part×方向 对 方向收益，三周期30/120/1440）：整体 RankIC、滚动IC（窗60/步20，连续≥3弱/翻转窗=失效预警）、**block bootstrap 置信区间**（块长20保留时序自相关、500次确定性重采样，"健康"要求 CI 保守边也越过0.03且同号率≥0.95，纯噪声不会误判）、多/空×轻仓/分批/强信号 regime 代理；**日频层**（读 G21 面板）：ret*/tsmom* 对未来1~60交易日的池化 RankIC、Q5-Q1 价差与**指数 IC 半衰期**。体检结论回写 `factors_catalog.HEALTH_SNAPSHOT`。当前真实结论：**机构动向次日 IC=−0.230、CI[−0.341,−0.118] 稳定显著为负="健康(反向)"（反转信号，与第35轮互证，本轮不改线上权重）**、原油联动次日 +0.276 健康、日频单因子池化 |IC|<0.10 无稳定预测。输出 `reports/factor_health.txt/.json`，只体检不改权重。

7. **表达式因子引擎（`factor_expr.py` + `tools/expr_research.py`，第38轮 G25，G2插件化/G16浅ML 共同前置）**：把因子定义成**表达式字符串+元数据**，一个**白名单、无 eval/exec/属性访问/导入**的递归下降 DSL 统一求值——时序算子 delay/delta/ts_mean/ts_std/ts_rank/ts_minmax/decay_linear/corr（尾窗严格无未来、窗口必须正整数字面量、支持嵌套），截面算子 cross_rank/scale/zscore，同一棵 AST 在离线面板与实时 bar 两种上下文逐值一致（**training-serving parity**）；另含因子治理 pearson/spearman、OLS **正交残差**、等权/IC/ICIR 加权合成。安全边界由21个危险/畸形反向用例钉死（未知算子、dunder、属性点、语句拼接、非常量窗口一律拒绝）。`tools/expr_research.py` 纯离线读 G21 面板：全64品种面板列 vs bar回读同表达式 **30.3万点 maxAbsDiff=0**、表达式动量对齐实时 ret5 仅1.1e-16；5个 research 表达式因子前向 H=1/5/20 |IC| 均<0.06 无稳定预测（负结果诚实，维持 research 不进分）。**默认只承载新研究因子，旧技术/基本面因子保持过程式原实现、综合分逐字节不变，引擎不被 main import**。输出 `reports/expr_research.txt/.json`。

8. **因子 regime 分层/换手/衰减形态（`tools/factor_regime.py`，第39轮 G29续，研究侧定期人工跑）**：在因子体检之上回答三个更细的问题——①**regime 条件有效性**：用面板 PIT 的 ret126 分牛/熊/震荡、hv60 过去120日分位（G25 ts_rank、无未来）分高/中/低波，桶内算前向 RankIC；②**持续性与隐含换手**：因子滚动分位在 1/5/20 日再平衡间隔的秩自相关与平均|分位变动|；③**衰减形态**：同一 IC(H) 曲线分别拟合指数/幂律、按 R² 择优，不硬套半衰期。真实结论：**长周期动量 ret252/tsmom252 只在低波 regime 有效（H20 低波 IC≈+0.10）、高波 regime 转负（动量高波崩溃）**，短周期各 regime 均≈0；信号 lag1 自相关约0.83、月度隐含换手约0.39；近零 IC 无干净衰减形态。输出 `reports/factor_regime.txt/.json`，只研究、不改权重不进分。

### 4.7 样本外验证与防过拟合工具箱（`tools/backtest_validation.py`，WP-F4 前置 / AFML ch7、11-12）

研究侧、纯标准库、离线只读，回答一个问题——**"在一堆候选参数/因子里挑出回测最好的那个"，这个挑选动作本身有多大概率在拟合噪声**。五块方法（公式来自公开论文，代码自写）：
1. **Deflated Sharpe（DSR/PSR，Bailey & López de Prado 2014）**：在普通 Sharpe 之外同时校正样本长度、收益偏度/峰度（非正态），以及"试了 N 个候选才挑到最好"的多重试验偏差（期望最大 Sharpe 阈值 SR0）；DSR≥0.95 才说明优势经得住多重试验校正。默认对 `portfolio_equity.csv` 按交易日聚合计算（`--trials` 为候选个数，默认18）。
2. **CSCV-PBO（Bailey et al. 2014）**：把时间均分 S 块、枚举对半的对称划分，每折用样本内 Sharpe 选最优参数、看它在样本外的相对排名，PBO=样本内最优落入样本外下半区的折数占比；PBO<0.2 泛化良好、≥0.5 选优基本在拟合噪声。
3. **PurgedKFold+Embargo（AFML ch7）**：时序样本禁止随机 K 折——标签窗口横跨测试折的训练样本一律 purge，折两侧再加 embargo；这是 WP-F4 训练 ml_samples 的强制切分器。
4. **Walk-forward**：滚动"前 IS 窗选参、后 OOS 窗验证"，量化 IS→OOS 衰减、OOS 跑赢候选中位数比例、相邻段最优参数切换率。
5. **参数高原 vs 孤峰**：最优点邻域是否同样稳健（plateau_ratio/邻域正收益占比/粗糙度）；**全网格最优点都亏损时不给"稳健"假象，直接提示先解决信号方向/成本**。
> 该工具只评估、不改动任何生产参数。当前约 6 个月分钟窗口下的真实结论（见 reports/backtest_validation.txt）：多数品种 18 组参数全样本微亏、IS→OOS 明显衰减，提示样本尚短、应继续积累而非重仓单点参数——与"跑不赢规则不上线"铁律一致。

---

## 五、期权严格分析（比期货更严格）

Black-76 模型（国内商品期权均为期货期权）+ Delta/Gamma/Vega/Theta 希腊字母。**六项检查全部通过才建议买入**：

1. 标的综合分 |分| ≥ 5（期货只需2分）
2. 波动率不贵：IV/HV ≤ 1.35，且真实IV分位（无页面时用HV代理分位）≤75%，同时对照10/20/40/60日波动率锥
3. 预期行情幅度 ≥ 1.5 倍平值权利金（时间价值覆盖）
4. 剩余到期 ≥ 14 天（按合约月份估算）
5. 建议合约 |Delta| ∈ [0.35, 0.60]（强信号虚一档、中等信号平值）
6. 每日Theta损耗 ≤ 权利金3%

隐含波动率说明：浏览器打开OpenVlab页面时，程序使用页面真实平值IV、隐波百分位和偏度；页面不可读时按 **OpenVlab真实IV > 新浪T链反推IV > HV估计** 三级优先级取IV，并用滚动HV分位和波动率锥判断贵贱。**PCR认沽/认购比已在第11轮接入**：`option_chain.py` 直连新浪商品期权T型报价（与行情同源、零新增依赖），主口径为持仓量PCR=Σ看跌持仓/Σ看涨持仓，另给C/P腿数、最大持仓行权价、ATM、情绪档与近30日分位；成交量PCR需逐腿成交量（T链不含），留待交易所期权日行情备用源。已知缺口：新浪T链暂无INE低硫燃料油(LU)期权，该品种自动降级为无链模式（期权分析照常，仅缺PCR）。

**第12轮接入多到期日 IV 曲面（`iv_surface.py`，零新增依赖）**：从 OpenVlab 到期日历取最近3个真实挂牌月份（剩余≥30天），对每个月份的新浪T链逐腿用 Black-76 **二分反推隐含波动率**（40次迭代，区间[0.01%,500%]），再按行权价聚合成微笑曲线，输出三类信息：
- **ATM IV 期限结构**：近月→远月 ATM 隐波连线，判定 Contango（近低远高/远月更贵）、Backwardation（近高远低/近期事件推高近月）、平坦，并给近-远 vol 差；
- **25Δ 风险反转(RR25)与蝶式(Fly25)**：RR 正=看跌保护偏贵（左偏）、负=看涨更贵（右偏），Fly 衡量微笑凸性；
- **曲面矩阵**：K/F = 0.90/0.95/1.00/1.05/1.10 五档 moneyness × 各到期月份的 IV 网格，缺档显示 `--`、**远月深虚值不插值**。

反推报价质量清洗（免费T链有单边挂单/宽价差/临近到期误差，必须清洗否则IV失真）：①窄价差（买卖价差≤15%）取中间价为高质量腿，宽价差回退最近成交价为低质量腿，宽价差且无成交直接丢弃；②单腿反推IV>250%丢弃；③ATM 必须由高质量腿构成、高质量腿不足3档则该到期日不输出曲面；④剩余<30天的月份不参与（最后30天时间价值小、报价误差被放大）；⑤微笑点IV相对ATM在[0.2×,3.5×]带外标 outlier，不参与25Δ与矩阵。call/put 同行权价按持仓量加权融合，两侧反推偏差>3vol 时取窄价差可信侧并标注档数。**口径限制**：IV为从收盘价反推的静态值，盘中实时性最好在夜盘/日盘盘中抽验；免费源无历史L2逐笔，远月深虚值不插值。

---

## 五.5、期权组合策略推荐（只推荐期权策略）

在单腿六项检查之外，程序还会按市况从策略库中挑选**最适合的期权组合策略**并做严格检查：

| 市况 | 推荐策略 | 结构 |
| --- | --- | --- |
| 强方向(≥5分) + 隐波不贵 | 单腿买入看涨/看跌 | 裸买，Delta最大 |
| 方向明确(≥2分)，隐波偏贵或中等 | 牛市看涨价差 / 熊市看跌价差 | 买近值腿+卖虚值腿（净支出，限风险） |
| 消息面与技术面大分歧 + 预期波动大 | 买入跨式 | 双买平值（不限方向） |
| 中性盘整(<2分) + 隐波偏贵 | 铁鹰式(四腿) | 双卖+双买护翼（净收入，限定风险，需保证金） |
| 中性窄幅震荡 + 隐波不极端 | 买入蝶式(1:2:1) | 买翼/卖中轴/买翼，借方限定风险 |
| 温和方向(2~6.5分) + 隐波偏贵 | 1:2比率价差 | 买1张近值、卖2张更远虚值，含1张裸腿，明确提示无限尾部风险 |
| 近月IV显著高于远月(Backwardation) | 看涨/看跌日历价差(卖近买远) | 同行权价卖近月+买远月，净支出、风险=净支出、正Vega，赚近月快Theta衰减与期限结构回归 |
| 近月IV显著低于远月(Contango) | 看涨/看跌反向日历(买近卖远) | 同行权价买近月+卖远月，净收入、近月到期后远月裸露（理论风险无上限，仅小仓位），且要求静态盈亏空间为正才推荐 |
| 温和偏多 + 已持有/愿买期货 | 备兑看涨 | 期货多头 + 卖虚值Call，收权利金但封顶上方收益 |
| 强偏多但想防黑天鹅 | 保护性认沽 | 期货多头 + 买Put保险，限定组合最大亏损 |

严格检查：**方向信号、机构观点配合、隐波状态、幅度/区间覆盖、剩余到期、风险边界**。买方IV/HV≤1.35且IV分位≤75%；普通价差IV/HV≤1.6且分位≤85%；卖方IV/HV≥1.15且分位≥55%；保护性认沽作为保险腿可放宽到IV/HV≤1.8、分位≤85%。每个组合输出Δ/Γ/Vega/Θ；涉及卖方、备兑或比率结构时，再输出保守保证金点值估算（未乘合约乘数，实盘以交易所/期货公司为准）。

**日历价差专项规则（第12轮）**：取IV曲面最近两个相邻月份（相隔1~2个月、近月剩余≥30天、两月均≥5档可反推行权价），同行权价取近月ATM；|近月ATM IV − 远月ATM IV|≥3vol 才触发——近月更贵(差≥+3vol)做**卖近买远**借方日历（正Vega，最大亏损=净支出），近月更便宜(差≤−3vol)做**买近卖远**贷方反向日历（负Vega，远月裸腿按10%名义保证金估算、上行风险诚实标注"理论无上限"）。两条额外硬检查：①**静态盈亏空间**——假设IV不变、近月到期标的恰好收在同行权价（日历最优点）时估算盈利必须为正，否则时间价值损耗后无利可图、直接观望（贷方反向日历尤其可能静态最优仍亏损，此时不推荐）；②方向由期货综合分决定看涨/看跌，且仍需机构观点配合。日历价差优先级=7（高于铁鹰/蝶式6，低于跨式8与方向价差10）。

输出示例（报告【期权策略推荐】板块）：
```
● [√] 橡胶 牛市看涨价差（2701月份）
    腿: 买看涨K=19000(约937.0点,ru2701C19000) + 卖看涨K=20300(约470.2点,ru2701C20300)
    [√] 机构观点配合: 机构看多7/看空0，与策略同向
    [√] 幅度覆盖: 预期波动≈3358.4点 vs 盈亏平衡需561.8点（覆盖6.0倍）
    盈亏平衡: 19467
    执行: 净支出466.8点；最大盈利833.2点 / 最大亏损466.8点；建议支出≤账户5%
```
权利金与盈亏均为 Black-76 + 估计隐波计算（估），下单以期货通实际挂牌合约与报价为准。

---

## 六、输出文件（reports 目录）

**轮动节奏**：交易时段为 **9:00-11:30 / 13:30-15:00 / 21:00-次日02:30（不同品种按23:00、01:00、02:30分档收市）**；每个时段**开盘前30分钟每5分钟一轮**（对齐 5 分钟刻度，如 9:00、9:05…9:25），**之后每20分钟一轮**（对齐 20 分钟刻度，如 9:30、9:50、10:10…）；非交易时段每1分钟一轮。每份报告的块头都写明本轮时间、当前节奏与下一轮计划时间。**每一轮新报告都写在文件最前面**（最新在最上）。

| 文件 | 内容 |
| --- | --- |
| `reports/latest_report.txt` | **仅交易时段轮次**，滚动保留最近5轮，**最新轮在最前**，每轮以 `#### 交易时段 第N轮 | 时间 | 节奏(含下一轮时间) ####` 分隔 |
| `reports/signals.csv` | **仅交易时段轮次**的信号流水，滚动保留最近5轮（**最新轮在最前**，utf-8-sig，Excel可直接打开），每行含时间与轮次 |
| `reports/signal_tracking.txt` | **信号胜率追踪页签**：一、分周期/分档胜率与平均方向收益；二、最近评估信号；三、WP-F2 历史同类信号胜率校准（方向×分档贝叶斯平滑胜率与 sizing 乘子，n 不足显示"积累中"） |
| `reports/backtest_report.txt` / `backtest_signals.csv` / `backtest_trades.csv` | 手动运行 `backtest.py` 生成的最小日线技术回测报告、逐信号1/5/20日结果、逐笔非重叠交易（毛收益/手续费/滑点/净收益/每手费用/锁板顺延）；看板含“最小日线回测”和“回测交易CSV”页签 |
| `reports/intraday_backtest_report.txt` / `intraday_backtest_trades.csv` | 手动运行 `intraday_backtest.py` 生成的日内/平今回测报告与逐笔交易（进出场时间、交易所交易日归属、平今/平昨leg、持仓分钟、毛/净收益、开平仓每手人民币费用、平今vs平昨对照、离场原因、锁板顺延）；看板含“日内/平今回测”和“日内回测交易CSV”页签 |
| `reports/portfolio_report.txt` / `portfolio_equity.csv` / `portfolio_trades.csv` | 手动运行 `portfolio.py` 生成的组合资金账户回测：组合级净值/年化/回撤/夏普/风险度序列报告、逐bar权益曲线（静态权益/浮盈/动态权益/占用/可用/风险度/回撤/持仓数）、含实际手数与风控强平标记的逐笔成交（`--calibrate` 时每笔带历史胜率乘子 calib_mult）；看板含“组合账户回测”和“组合交易CSV”页签 |
| `reports/factor_eval.txt` / `factor_eval.json` | 由 `tools/factor_eval.py` 生成的九因子预测力评估：分周期 IC/RankIC/ICIR、分档单调性与多空价差、walk-forward 同号率、建议权重区间（仅建议不自动改）；**.json 为第22轮新增结构化 sidecar（同一次计算，供图表看板消费）** |
| `reports/attribution.txt` / `attribution.json` / `attribution_curve.csv` | 第35轮 G28 由 `tools/attribution.py` 生成的**收益归因复盘**：多因子 OLS 加法归因（每因子 β/t/贡献/IC/胜率，mean(y)=α+Σβ·mean(x) 闭合）、IS-OOS β 一致性、BHB 板块配置/选择/交互三效应（恒等闭合）、逐事件累计归因曲线；只归因不改线上权重 |
| `cache/research_panel.db` / `reports/research_panel_manifest.txt` | 第36轮 G21 由 `tools/panel_builder.py` 生成的**标准研究面板**（独立 SQLite、gitignore、可幂等重建）：research_panel 品种×交易日×30字段（复权OHLC/量/OI/ret1d/17技术指标/3基本面PIT）、research_runs 构建 manifest；第37轮 G21续补齐全64品种61353行，tsmom/xsmom/carry 可 `--panel` 直读；`tools/pit_audit.py` 对其做结构/PIT/实时-离线 parity 审计 |
| `reports/factor_health.txt` / `factor_health.json` | 第37轮 G29 由 `tools/factor_health.py` 生成的**因子体检卡**：事件层 RankIC/滚动IC失效预警/块bootstrap CI/多空·档位regime，日频层 IC 期限曲线/指数半衰期/Q5-Q1；结论同步回写 factors_catalog.HEALTH_SNAPSHOT，只体检不改线上权重 |
| `reports/expr_research.txt` / `expr_research.json` | 第38轮 G25 由 `tools/expr_research.py` 生成的**表达式因子研究台**结果：面板列 vs bar回读同表达式全64品种 parity（maxAbsDiff=0）、表达式动量对齐实时 ret5、5个表达式因子对未来1/5/20日的逐品种/池化 RankIC 与截面 cross_rank 演示；research 因子不进综合分 |
| `reports/factor_regime.txt` / `factor_regime.json` | 第39轮 G29续 由 `tools/factor_regime.py` 生成的**因子 regime/换手/衰减形态**：牛熊震荡×高中低波分桶前向 RankIC、1/5/20日秩自相关与隐含换手、指数vs幂律衰减 R² 择优；只研究不改权重 |
| `reports/backtest_validation.txt` | 由 `tools/backtest_validation.py` 生成的防过拟合报告：组合 DSR、逐品种参数网格 CSCV-PBO、Walk-forward 衰减、参数高原/孤峰与全市场结论（只评估不改参） |
| `data/futures_fees.csv` | 由用户券商手续费表通过`tools/build_fee_table.py`转换的64品种真实费率：投机账户、按金额费率、按手数固定金额、合约乘数；回测运行时只用标准库csv读取，原始xlsx归档在同目录 |
| `data/futures_margins.csv` | 由`tools/build_margin_table.py`从银河期货保证金比例页解析的64品种**公司保证金率（投机档）+基础板幅+每手报价单位乘数**，组合账户`portfolio.py`运行时只用标准库csv读取；交易所基准档无干净免费源故`exchange_margin`列留空不编造，临近交割/长假公司会上浮、以公司通知为准 |
| `data/monitor.db` | SQLite 结构化数据库：`quotes` 行情（相同快照自动去重）、`signals` 非中性期货信号、`news` 新闻、`options` 单腿/组合策略、`signal_outcomes` 信号后续结果、`option_chains` 期权完整链/PCR快照（第12轮起每品种按真实挂牌月份存多行：同一ts下不同expiry各一行，供PCR历史分位与跨月IV曲面）、`fundamentals` 基本面日频快照（第13轮：库存分位/周环比、龙虎榜净多、carry、基差、基本面综合分，每品种每交易日一行、长期保留）、`minute_bars` 分钟K自采库（第14轮：1/5/15/30/60m，唯一键 contract+period+bar_dt 去重，主连RB0与具体合约按sym共存，默认保留400天）、`ml_samples` triple-barrier监督学习样本库（WP-F2：三分类标签+PIT特征快照，UNIQUE(sym,period,bar_dt)可重复跑、约保留10年）；行情/新闻/期权/链快照明细默认保留180天，可交易信号、评估结果、基本面日频快照与分钟K长期积累用于回测调参 |
| `reports/history_report.txt` | **仅交易时段轮次**的当日归档：每轮打包 `============ 交易时段 第N轮 | 时间 | 节奏 ============` + 完整报告 + 本轮信号流水，**新块置顶**；**次日启动时自动清除其中昨日的轮动块** |
| `reports/offhours_report.txt` | **仅非交易时段轮次**，滚动保留最近5轮（**最新在最前**），含预测走向 |
| `reports/offhours_history.txt` | **仅非交易时段轮次**的当日归档，**新块置顶**；**次日启动时自动清除其中昨日的轮动块** |
| `reports/daily_review.txt` | **每日复盘报告（永久保留，从不删除）**：交易日全部夜盘结束后自动生成一次——汇总当日轮动表现、最近7天信号胜率/平均方向收益、当日新闻（利多/利空统计、影响力Top）、末轮期权策略回顾与后续关注；**新一天的复盘写在最前面**，同日重复生成会替换当天旧块 |
| `reports/实时报告.html` | **多页签实时看板**：双击打开（或双击根目录 `查看实时报告.bat`），页签切换交易轮次、**图表看板（第22轮 ECharts 9图，第23轮起同页内嵌渲染、不再 iframe 套独立页）**、CSV流水、信号胜率追踪、日线/日内/组合三套回测、当日归档、非交易时段和每日复盘（共14个页签=13个txt/csv走iframe+1个内嵌图表）。页面每10秒轻量探测 `reports/report_status.js`（极小状态文件），**只有程序真写出新一轮报告时才重载内容**——定时轮动（5/20/1分钟刻度）与原油急动紧急轮动都能在10秒内显示，平时不刷新（图表页签改为重新注入 chart_data.js 重渲染）；页头显示最新报告时间/轮次、紧急标记与计划下一轮倒计时 |
| `reports/图表看板.html` / `chart_data.js` / `assets/echarts.min.js` | **第22轮新增 P1-3 图表看板、第23轮改为片段拼装**：本地 ECharts 5.5.1（Apache-2.0，离线可看、零 Python 依赖）渲染 9 张图——组合权益/回撤/风险度、横截面板块与全品种强弱、因子 RankIC 与分档单调性、信号胜率校准；chart_data.js 每轮由 charts.py 从既有 CSV/SQLite/内存态注入（缺数据显空态）；echarts 从根目录 assets/ 幂等同步，删了下轮自动补；独立页保留可直链打开，与实时看板内嵌的图表页签共用 charts.py 的 _PANEL_STYLE/DOM/JS 同一片段 |
| `reports/report_status.js` | 看板专用状态文件，每轮落盘时自动更新（无需查看、勿删） |
| `logs/monitor.log` | 运行日志（含每轮开始/完成时间、下一轮计划时间、日切清理记录） |

说明：
- 五个轮动文件按"交易时段 / 非交易时段"分流：交易时段写 latest_report.txt、signals.csv、history_report.txt；非交易时段写 offhours_report.txt、offhours_history.txt。
- **次日首次运行程序时，会先把 latest_report.txt、signals.csv、history_report.txt、offhours_report.txt、offhours_history.txt 中"昨日及以前"的轮动内容清掉，只保留当天**；daily_review.txt 不受影响、永久累积。
- 程序常驻跨过零点时，零点后第一轮也会自动执行同样的日切清理。
- **实时查看建议用 `实时报告.html` 看板**：Windows 记事本不会自动重载已打开的 txt；若用 Excel 打开 signals.csv，文件会被占用，程序会自动短暂重试、仍占用时仅跳过该文件这一轮（下一轮自动补写），不影响其他报告落盘——因此看盘期间不建议用 Excel 长期打开 signals.csv。
- 程序重启后滚动窗口从新一轮开始（第1轮起），滚动文件随之重写；两份当日归档文件跨运行持续累积到当天结束。

---

## 六.5、主动告警与信号追踪

### 1. 本机声音
默认开启，不同事件使用不同提示音：紧急轮动、期货强信号、期货跨档信号、期权策略触发。若需要临时静音，PowerShell 启动前执行：

```powershell
$env:FUTURES_MONITOR_SOUND='0'; D:\Python\python.exe main.py
```

### 2. Webhook 推送到手机
不新增依赖，复用程序已有 HTTP 连接池，支持飞书、钉钉、企业微信群机器人、Server 酱和接收原始 JSON 的通用地址。PowerShell 示例：

```powershell
$env:FUTURES_MONITOR_WEBHOOK='你的机器人Webhook地址'; D:\Python\python.exe main.py
```

默认 `ALERT_WEBHOOK_TYPE="auto"`，会按 URL 自动识别平台；识别不准时可在 `config.py` 改成 `feishu/dingtalk/wecom/serverchan/generic`。推送在守护线程执行，Webhook 失败只写日志，不影响监控主循环。

### 3. 触发规则与冷却
- 程序刚启动的第一轮只提醒 ≥6.5 强信号，避免64个品种基线刷屏；
- 运行中分数跨到4分档、跨到6.5强信号档，或多空方向翻转时提醒；
- 新出现“六项全过”的期权组合策略时提醒；
- 紧急轮动冷却300秒；同一品种、同一方向、同一分档信号冷却1800秒，跨档升级或多空翻转不受该同向冷却拦截；均可在 `config.py` 调整；
- 同一轮若同时出现多个信号，程序会自动聚合成“期货监控告警（N条）”，只按最高级别响一次声音、只推送一条汇总消息，避免极端行情刷屏。

### 4. 胜率统计口径
`|综合分|≥2` 的期货信号会自动建立30分钟、2小时、约24小时三个评估任务。到期后用后续行情计算：

```text
方向收益 = 信号方向（多=1，空=-1）×（评估价/入场价-1）
```

正收益记为“正确”，负收益记为“错误”，零变化记为“打平”；该品种自身休市导致长期没有新价格、或到期后长期取不到行情时记为“过期”，不纳入胜率分子/有效分母，也不会硬算成错误。同一品种、同一方向、同一分档只要还有待评估任务，就不重复建单，避免每轮刷屏。

---

## 七、浏览器页面直读 与 非交易时段预测走向

**页面直读**：双击 `打开行情网页(调试模式).bat`（用调试端口9222启动Edge并打开两个网页），
再运行本程序，程序会每30秒直接读取这两个网页的可见内容：
- OpenVlab：**真实平值隐波、隐波百分位、偏度/偏度百分位**（真实IV直接替换估计值参与期权策略检查）、隐波最大上升/下降榜、
  波动率溢价最高/最低榜（溢价高→买方阈值更严、卖方更松）；
- 交易可查：AI研报多空一览、头条精华多空动向（乾坤归一/大佬动向/外资动向等，轮播内容多次读取自动累积）。
页签缺失时程序会自动补开；没开浏览器/没有调试端口时程序照常用公开接口数据，不受影响。
报告品种明细中的"页面数据/页面动向"行即来自此功能。

**非交易时段预测走向**：在该品种自身日盘/夜盘时段之外运行时，
每个品种的建议会额外附加一条"预测走向"——由 综合因子方向、机构观点、消息面近4小时趋势、
日线动量、原油隔夜方向 五路规则投票生成，输出方向倾向+参考概率(50%~68%)+依据，
并明确标注"规则预测仅供参考，开盘后以实际行情校验"。

## 七.5、工程化与数据质量底座（第25轮 G9/G10/G11/G6）

1. **git 工程化（G9）**：项目已纳入 git（基线 tag 0.24.0）。.gitignore 忽略 data/*.db、cache、logs、reports 运行产物、__pycache__、.env 等，只版本化源码/测试/文档与 data 下 csv/xlsx 配置表；.gitattributes 统一 LF；
equirements.txt 锁定三个直接依赖版本，
equirements-freeze.txt 为开发解释器完整冻结留档；版本看 VERSION、变更看 CHANGELOG.md。
2. **配置外置（G10，零新增依赖）**：不改代码即可调参——复制 config.template.json 为 config.json（已 gitignore）改阈值/开关/账户参数，启动时深合并覆盖内置默认；密钥/webhook 只走 .env（复制 .env.example，真实环境变量优先）。缺这些文件时行为与历史完全一致；非法类型/未知键/路径常量会被安全跳过。也可用环境变量 FUTURES_MONITOR_CONFIG、FUTURES_MONITOR_ENV 指定外置文件。
3. **数据源熔断降级链（G11，data_router.py）**：每个数据源有独立熔断器（连续失败 N 次→熔断冷却→半开试探一次→成功恢复/失败再熔断），行情（新浪主、东财兜底）与分钟K（新浪/通达信/东财）三源结果都汇入进程级 REGISTRY；健康时取数顺序与结果不变，仅在源确实连续失败时跳过它并自动降级，避免向坏源空发请求。
4. **数据质量监控（G6，data_health.py + storage 第 11 张表 data_health）**：每轮体检全品种行情的缺数/陈旧价/异常跳变，按源统计请求/成功/失败并落库；某品种连续缺数或某源连续全失败/熔断时复用告警推送；报告新增【数据源健康】小块。研究侧可用 	ools/db_archive.py --year YYYY [--csv] 把历史按年零依赖导出为 SQLite 分库/CSV 年包（只导出不删除），配合主库保留期长期控体积。

## 七.6、纸面交易引擎 PaperBroker（第27-28轮 G1，补订单执行层塌陷；默认休眠）

把每轮综合分信号串成虚拟委托/成交，在不接实盘的前提下跑出第一条含成本模拟净值，对标 freqtrade dry-run / vnpy SimNow。**账户内核直接复用 `portfolio.Portfolio`**（三种 sizing、单品种/板块/资金/持仓数约束链、逐轮盯市、两段式强平、真实费率），不写第二套资金逻辑；`paper_broker.py` 只做“实时轮询信号→委托→成交”状态机与持久化：
- **成交两档**：`close`=信号轮当轮最新价成交；`next`（影子默认、保守）=信号轮只挂单、下一轮首个新价成交、成交严格晚于信号，下一轮锁板/无价/临时资金约束则挂单顺延、绝不虚构成交。
- **三阈值迟滞防抖**：|综合分|≥PAPER_ENTRY_SCORE（默认4=轻仓线）才开/反手，持仓后|分|<PAPER_EXIT_SCORE（默认2=中性线）才离场，中间继续持有；反手先平后开；信号转中性自动撤遗留挂单，同向排队不重复挂。
- **成本与锁板**：成交价内含滑点（买×(1+slip)/卖×(1-slip)），手续费走 data/futures_fees.csv 真实费率（缺表回退兜底比例）；相对昨结整根贴板才拦截，缺数据放行。
- **三表与重启恢复**：storage 新增 `paper_orders`（委托全生命周期）、`paper_trades`（开/平成交、滑点/手续费/已实现/pos_ref 配对）、`paper_equity`（每轮一条、同 ts 覆盖幂等），进程重启可由三表重建持仓/已实现/手续费/挂单，是长期影子对照的基础。
- **总开关 PAPER_ENABLED 默认 False（第28轮已接主循环，仍保持休眠，手动开启才运行）**：在 config.json 置 `PAPER_ENABLED: true` 后重启监控即进入影子模拟——run_cycle 每轮自动撮合，实时报告出现【纸面账户·影子模拟】紧凑块，`reports/paper_account.txt` 逐轮刷新（账户概览/组合绩效/委托状态/当前持仓/在途挂单/最近20笔成交/诚实说明），图表看板新增"⑤纸面账户·影子净值"区块三张图（动态/静态权益、回撤、风险度+持仓数，共12图），实时看板出现"纸面账户(影子)"页签（休眠时页签自动隐藏）。
- **平今/平昨**：按交易所结算交易日实时判定（夜盘21点后归下一交易日，与日内回测同口径），开仓记录结算日、平仓同结算日=平今、跨日=平昨，判不了保守按平昨。
- **如何对照验收**：连续影子运行≥4周后，用 paper_trades/paper_equity 与 signal_outcomes 对照"含真实成本后综合分策略是否仍为正"；成本后为负必须诚实呈现并回退阈值。纸面影子永不自动接实盘（实盘门槛见融合总纲 G20）。

## 八、常见问题

- **第一份报告要等1~2分钟**：启动时要探测64个品种的主力月份+首次预取日线，属正常；之后每60秒准时更新。
- **周末/节假日价格不动**：接口返回最近交易日数据，报告会提示"休市"。
- **期货通没有自动打开**：检查 `config.THS_EXE` 路径是否与安装位置一致；程序只负责打开它，分析数据完全来自新浪/金十公开接口，期货通没打开不影响监控。
- **想调整范围/阈值/关键词**：`config.py`（交易所范围、品种表、期权品种、阈值）、`factors.py`（关键词词典与闸门）。

## 九、回归测试（开发用，不影响常驻监控）

`tests/` 目录是第21轮落地、历轮持续扩充的 **pytest 零网络回归网**，把历轮“验证后即删”的合成断言固化下来：30 个测试文件、440 个用例，约 6.8 秒跑完，覆盖调度时段、横截面稳健z、风控闸门、胜率校准、基本面/情绪因子、期权T链与IV曲面、分钟K聚合、量仓资金、回测费用、**回测严谨性（第26轮 test_backtest_rigor 17 例：next_open 次根成交/末根不虚构/锁板顺延/反手、bootstrap 区间、IS-OOS、backtest_runs 留档）**、**纸面交易（第27轮 test_paper_broker 18 例：迟滞/两档成交时点/锁板顺延/滑点双边费/反手先平后开/强平/拒单与临时约束排队/三表落库与重启恢复）**、**研究面板与PIT（第36/37轮 test_research_panel 18 例：特征注册表一致/严格asof/扰动无未来+反向泄漏/训练-服务parity/PanelStore幂等/结构审计/G21续面板回读与网络路径逐值等价、不二次复权）**、**因子体检（第37轮 factor_health 自测：滚动IC/块bootstrap/失效预警/IC半衰期）**、**表达式引擎（第38轮 test_factor_expr 37 例：21个危险/畸形解析反向用例、时序截面算子手算、无未来扰动、OLS正交恢复β、IC/ICIR加权、实时离线结构性parity、表达式因子必登记）**、组合账户强平、存储去重、图表数据层及研究工具自测（含第39轮 factor_regime 自测：PIT regime标签边界/分层IC只在有效桶显著/秩自相关换手/指数vs幂律衰减择优与安全降级）。

```bat
D:\Python\python.exe -m pytest          # 项目根目录下全量运行
```

- 用例全部**零网络、确定性**：交易日历由夹具注入、数据库用临时库，不访问外部接口、不碰生产 `data/monitor.db`；
- pytest 只是开发工具，**生产 `requirements.txt` 不含 pytest**，常驻 `main.py` 不依赖它；
- 详见 `tests/README.md`。改动任何纯函数逻辑后应先跑一遍 `pytest` 再交付。

## 八、参考来源（期权定价影响因素）

- [场外期权基础知识丨希腊字母的影响因素 - 财联社](https://m.cls.cn/detail/1962180)
- [期权价格与风险系数（希腊值）- CME Group](https://www.cmegroup.com/cn-s/education/courses/option-greeks/options-the-greeks-options-premium-and-the-greeks.html)
- [期权进阶-希腊字母全解析 - 知乎](https://zhuanlan.zhihu.com/p/715875950)
- [期权维加值(Vega) - CME Group](https://www.cmegroup.com/cn-s/education/courses/option-greeks/options-vega-the-greeks.html)
- [期权风险管理（郑振龙）](http://efinance.org.cn/cn/FEshuo/chp19.pdf)
