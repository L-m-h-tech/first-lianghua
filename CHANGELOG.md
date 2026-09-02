# 更新日志（CHANGELOG）

本项目按"轮"迭代，版本号 `主.轮.补丁`，与 `VERSION` 对齐；详细过程见 `上下文摘要.md`。
铁律：生产纯标准库 + 三个直接依赖；默认行为可回退；每轮合成断言 + 真实冒烟 + 负结果诚实呈现。

## [0.25.0] — 2026-09-02 · 第25轮 G9 工程化 + G10 配置外置 + G11 数据源熔断降级链 + G6 数据质量（数据底座一组）
- **G9 git 工程化（零改运行代码）**：`git init` 并打基线 tag v0.24.0；新增 `.gitignore`（忽略 data/*.db、cache、logs、reports 运行产物、__pycache__、.env 等，保留源码/测试/文档与 data 下 csv/xlsx 配置表）、`.gitattributes`（统一 LF）；requirements 三个直接依赖锁 `==`（requests 2.34.2 / uiautomation 2.0.29 / websocket-client 1.9.1），requirements-freeze.txt 全量留档；新增 CHANGELOG.md / VERSION。
- **G10 配置外置（零新增依赖，缺配置与历史逐字节一致）**：新增 `config_loader.py`（.env 解析注入且真实环境优先、dict 深合并、按默认类型矫正、只覆盖已存在的全大写可调常量、路径/内部名受保护、非法/未知项跳过不崩）；config.py 启动先载 .env（早于 environ.get）、末尾深合并 config.json；提供 `config.template.json` 与 `.env.example`；可用 FUTURES_MONITOR_CONFIG/FUTURES_MONITOR_ENV 指定外置文件。
- **G11 数据源主备熔断降级链（纯标准库、时钟可注入）**：新增 `data_router.py`——SourceHealth 熔断器（CLOSED→连续失败 OPEN→冷却 HALF_OPEN 半开试探→成功恢复/失败再熔断）、DataRouter 有序主备（熔断跳过、异常/残缺降级下一源、全失败抛 AllSourcesFailed）、进程级 REGISTRY 健康总账；futures_data 实时行情与 intraday_bars 分钟K三源上报健康，东财兜底源熔断期内跳过（健康时行为不变）。
- **G6 数据质量监控（只监控不改结果）**：新增 `data_health.py`（缺数/陈旧价/异常跳变体检、跨轮连续缺数与连续全失败追踪、看板文本块）；storage 新增第 11 张表 `data_health`（按 ts+source 覆盖、含保留期清理）与 insert/recent 方法；main 每轮落表并对连续异常复用 alerts 告警；report 增【数据源健康】小块；新增 `tools/db_archive.py` 零依赖按年导出 SQLite 分库/CSV 年包（只导出不删，含 --selftest）。
- 验证：新增 4 个测试文件共 47 个用例，全量 **pytest 238 全绿（191→238，约2.7s）**；真实 `main.py --once --no-launch` exit0、零 Traceback/ERROR，data_health 落 4 行（行情64/64、分钟源320/320、三源全 closed）、非交易时段报告健康块正确、monitor.db integrity/quick_check=ok、外键无违规（11 张表）。生产 39→43 个 py、14663→15560 行，零新增运行依赖。

## [0.24.0] — 2026-09-02 · 第24轮 第三份全网/GitHub 对标 + 六文档融合（纯文档，零改生产代码）
- 全网调研量化系统十层标准架构与 GitHub 主流量化项目（freqtrade/qlib/vnpy/nautilus/Lean 等），逐项对比本项目，产出《量化系统对标与改进报告.html》《对标能力可视化.html》（真实浏览器验证零报错）。
- 将 2 份新 HTML 与《统一改进路线图》《未完成项落地方案》《AI量化对标与改进方案》《GitHub对标与改进清单》融合为唯一总入口《总体对标与统一改进总纲（融合版）.md》：未实现项统一 G1–G20 编号、按 P0–P3 优先级分类，含不做清单、保持强项、回退表、轮次排期。
- 十维自评：因子 4.5、数据 4、策略研究 4、组合 4、监控 4、回测 3.5、绩效 3、风控 3、工程 3，订单执行 0.5（唯一塌陷，待 G1 纸面交易补齐）。
- 生产规模：39 个 py / 14663 行；tests 191 用例全绿。

## [0.23.0] — 2026-09-02 · 第23轮 实时报告与图表看板合并
- "图表看板"页签由 iframe 套独立页改为同页内嵌 ECharts 渲染（独立直链页保留，共用同一片段）；charts.py 模板拆 STYLE/DOM/JS 三段、IIFE 导出 window.ChartPanel（懒加载/resize/reload）。
- tests 189→191；真实 Chrome file:// 两路径逐屏验证、控制台零报错。

## [0.22.0] — 2026-09-02 · 第22轮 P1-3 看板图表化
- 新增 charts.py（纯标准库）与本地 ECharts 5.5.1 前端资源，看板新增 9 图：权益/回撤/风险度、横截面板块与全品种强弱、因子 IC 与单调性、胜率校准；数据由现有 CSV/SQLite 注入，缺数据显空态，不改任何打分/信号/回测口径。
- tools/factor_eval.py 增加结构化 sidecar reports/factor_eval.json；tests 174→189。

## [0.21.0] — 2026-09-02 · 第21轮 P1-1 pytest 回归体系
- 新增 tests/ 目录，把前 20 轮"验证后即删"的数百条合成断言固化为永久回归网（conftest 确定性日历 + 临时库夹具，全部零网络、确定性）；pytest 仅 dev、不进 requirements。
- 经红/绿双向变异测试验证用例有效；test_compileall 参数化编译全部生产 py 并设数量下限防"假全绿"。

## [0.20.0] — 2026-09-02 · 第20轮 样本外验证与防过拟合
- 新增 tools/backtest_validation.py：Deflated Sharpe(PSR/DSR)、CSCV-PBO、PurgedKFold+Embargo、Walk-forward、参数高原 vs 孤峰；全 64 品种 18 组参数真实网格，诚实呈现"窗口太短、多数 IS 选优 OOS 衰减、暂不应上 ML"。

## [0.19.0] — 2026-09-02 · 第19轮 WP-F2 信号校准与监督学习备料
- signal_calibrator.py：方向×分档×主导因子贝叶斯平滑胜率给 sizing 乘子（默认影子，仅 portfolio --calibrate 改手数）。
- tools/factor_eval.py：九因子 IC/RankIC/ICIR/分档单调性/衰减/WF 同号率（只建议权重不自动改）。
- tools/build_ml_samples.py + storage 第 9 表 ml_samples：triple-barrier 标签、严格 PIT、purge/embargo。

## [0.18.0] — 2026-09-02 · 第18轮 WP-F1 决策增强（默认只增量、不改综合分）
- analyzer 多空双面卡 build_debate；新增 cross_section.py（稳健 z、板块强弱、多空广度）；新增 risk_gate.py 独立风控闸门（pass/warn/veto，默认只标注不降级）；factors 五维情绪。

## [0.17.0] — 2026-09-02 · 第17轮 调度时段边沿触发 + 首轮报告自动打开
- utils.next_transition 补齐 13:30 下午开盘；交易/非交易切换立即轮一轮并跟随对应节奏；首轮真实报告自动用默认浏览器打开（--no-launch 可关）。

## [0.16.0] — 2026-09-01 · 第16轮 WP-E 组合资金账户
- 新增 portfolio.py：共享资金池逐 bar 盯市（静态/动态权益、占用、可用、风险度）、三种 sizing（等名义/等风险/按分档）、板块与持仓数约束、强平状态机、权益曲线；tools/build_margin_table.py 从银河期货保证金页生成 64 品种 data/futures_margins.csv；修复 backtest 主连双零退化。
- 增补：AI 量化全网/GitHub 对标（WP-F 路线）、Tushare 引入可行性分析（均只分析未改码）。

## [0.15.0] — 2026-09-01 · 第15轮 WP-D1/D2 日内/平今回测
- 新增 intraday_backtest.py：vnpy 式 bar 内撮合、交易所结算交易日判平今、精确整根锁板、真实费率每手人民币 + 平今/平昨对照、18 组稳定性网格；全 64 品种 30m 共 10101 笔验证，诚实给出"分钟高频成本后不赚钱"负结果。

## [0.14.0] — 2026-09-01 · 第14轮 WP-D0 分钟 K 自采库
- 新增 intraday_bars.py / tdx_bars.py，storage 第 8 表 minute_bars；全周期统一选源链"新浪主连→通达信可选→东财兜底"，完整回填 327360 根（64 品种×5 周期×1023），零非法 OHLC、幂等。

## [0.13.0] — 2026-09-01 · 第13轮 WP-C 基本面数据包
- fundamental_data.py / fundamental_factors.py：东财库存仓单分位与去化、龙虎榜净多与边际、期限 carry、生意社基差（软降级），加权入综合分（满分 ±1.5），storage 第 7 表 fundamentals。

## [0.12.0] — 2026-09-01 · 第12轮 WP-B 多到期日 IV 曲面 + 日历价差
- iv_surface.py：Black-76 二分反推、put-call 平价、矩阵缺档清洗、日历/反向日历策略与静态空间拦截。

## [0.11.0] — 2026-09-01 · 第11轮 WP-A 完整期权 T 型链 + 持仓量 PCR + 期限结构
- option_chain.py：全月份 T 型链、持仓量 PCR、最大持仓行权价、期限结构组装。

## [0.10.0] — 2026-09-01 · 第10轮 真实券商手续费
- tools/build_fee_table.py 将银河期货 2026-08-28 费率表转为 data/futures_fees.csv（64 品种投机档），backtest 默认读真实费率（按金额 + 按手数叠加、固定费折算），同时给毛/净口径。

## [0.9.0] — 2026-08-31 ~ 09-01 · 第6–9轮 P0/P1 批处理
- 第6轮 P0 六项：夜盘分档、解释器固定、交易日历、日志轮转、连接池、看门狗。
- 第7轮：主动告警 alerts、SQLite 存储（storage）、信号胜率追踪、量仓资金因子。
- 第8轮：最小日线回测 backtest、RSI/MACD/KDJ 日线技术共振、新闻反转、期权 IV 分位/锥/偏度/组合 Greeks。
- 第9轮：30/60 分钟共振、回测成本/锁板/参数稳定性、蝶式/比率/备兑/保护性认沽期权策略、保证金点值。

## [0.5.0] — 2026-08-30 ~ 31 · 第1–5轮 监控基座与首次对标
- 轮动节奏/复盘/日切/实时更新/置顶；原油急动紧急轮动 + 看板事件驱动；全网数据多源扫描（新闻/全球行情/突发事件）；GitHub 同类项目首次对标（只分析）。
