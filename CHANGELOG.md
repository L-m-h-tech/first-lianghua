# 更新日志（CHANGELOG）

本项目按"轮"迭代，版本号 `主.轮.补丁`，与 `VERSION` 对齐；详细过程见 `上下文摘要.md`。
铁律：生产纯标准库 + 三个直接依赖；默认行为可回退；每轮合成断言 + 真实冒烟 + 负结果诚实呈现。

## [0.29.0] — 2026-09-02 · 第29轮 G3 完整绩效指标包/tear sheet（metrics.py 统一绩效口径 + 看板水下/滚动夏普/月度热力三图）
- **新增 `metrics.py`（纯标准库、零网络、零第三方依赖，45 个生产 py）**：对标 pyfolio/empyrical 统一绩效口径，分两类输入——①周期收益序列（如日度收益率）：累计/算术年化/CAGR/年化波动/夏普/**索提诺（全样本下行偏差 empyrical 口径）/Calmar/Omega/Ulcer 指数/历史法 VaR·CVaR（线性插值分位）/水下回撤序列/滚动夏普（暖机期 None）/自然月复利月度矩阵**；②逐笔盈亏序列：胜率/平均盈亏/期望/**盈亏因子 PF/盈亏比/最大连胜连亏/best·worst**；另有 **MFE/MAE 持仓过程偏移**（多空对称、纯函数）与 mae_mfe_summary、一键 `tear_sheet()`；**样本不足一律返回 None 绝不抛异常**；内置 `--selftest` **47 项手算可复核断言**（rs=[+1,-2,+3,-1,+2]% 逐值推导 Omega=2、Ulcer=0.01、VaR5%=-0.018、Calmar 等）。
- **portfolio.py（账户内核，纯增量不改旧口径）**：Position 增 mfe/mae 槽，`record()` 盯市时累计每仓相对开仓价的最大有利/不利偏移，`close()` 成交流水带 mfe/mae；`performance()` 在**旧键逐值不变**前提下增补 calmar/omega/ulcer/var95/cvar95/monthly/profit_factor/max_win_streak/max_loss_streak/mae_mfe（日度收益与归属日严格对齐）。
- **report.py 纸面账户（仅 PAPER_ENABLED 路径，休眠零影响）**：【组合绩效】新增一行风险调整 G3（Calmar/Omega/Ulcer/VaR95/CVaR95/PF）、一行交易连续性（最大连胜连亏+持仓过程平均 MFE/MAE），并按自然月输出月度收益行；新增 `_num` 空值统一显 "-"。
- **backtest.py 日线回测**：总体表现段在旧指标之后追加一行"G3扩展绩效"（盈亏因子 PF/盈亏比/最大连胜连亏/Omega/Ulcer/Calmar，逐笔非重叠交易为观测、年化按 252/hold 与既有夏普同口径），样本不足显"样本不足"，**旧指标逐值不变**。
- **看板 charts.py（12→15 图）新增第⑥块 G3 绩效**：水下回撤曲线（反转轴+面积，chips 展示年化夏普/Sortino/Calmar/Omega/Ulcer/VaR95/CVaR95 与数据来源）、滚动60交易日夏普（零线、暖机显空）、月度收益热力图（年×12月、红涨绿跌、整行宽度、悬停精确值）；`tear_payload` **优先纸面影子快照、否则组合回测 portfolio_equity.csv**（休眠态也能立刻用既有回测曲线出图），日内多轮先按自然日取最后一点收敛成日度再年化；每块独立 try、缺数据显空态、allow_nan=False 防 NaN 进前端。config 增 METRICS_BARS_PER_YEAR/ROLLING_WINDOW/VAR_ALPHA/TEAR_MAX_POINTS 4 常量。
- 验证：新增 tests/test_metrics.py 6 用例（selftest 全绿/tear 固定键与独立降级/分位手算/日度收敛/多空 MFE-MAE/portfolio 集成键），test_charts +4（三系列对齐与 off-by-one 回归/样本不足/paper 优先再回退 CSV/build_payload 含 tear 块）并同步 CHART_IDS 与容器清单；全量 **pytest 293→304 全绿（约3.5s，24 个测试文件）**，metrics `--selftest` 47 项全过；前端 `node --check` 通过 + **真实浏览器目视**（用既有 portfolio_equity.csv 实测：水下曲线/滚动夏普/月度热力三图正常渲染、chips 数值正确、红绿语义对、控制台零错误，热力图改整行宽度后标签清晰）；**真实 64 品种 `main --once` 冲烟 exit0**：主链与综合分零改动、休眠态零纸面块/不生成 paper_account.txt/chart_data paper:null 而 tear 正常取组合回测出图。生产 44→45 个 py、17460→18178 行，零新增运行依赖。下一站第30轮=G7 多窗口时序动量 TSMOM（1/3/6 月趋势强度对照，防单一窗口过拟合）。

## [0.28.0] — 2026-09-02 · 第28轮 G1 纸面交易引擎（二）：接主循环 + paper_account.txt + 看板净值图（影子账户闭环）
- **主循环接入（main.py，独立 5.5 段、默认完全休眠）**：`State.__init__` 末尾仅当 PAPER_ENABLED=true 才实例化 PaperBroker（try 包裹，失败即 None+告警，零影响主链）；run_cycle 在存储之后、报告渲染之前把本轮 analyzer 行+实时 quotes 喂给 `on_cycle`，落 paper_account.txt 并 LOG 一行委托/成交/持仓/权益/风险度；**绝不回改综合分、信号、建议与任何既有输出**，PAPER_ENABLED=false 时 state.paper=None、零实例化零开销。
- **平今/平昨按交易所结算交易日实时判定（paper_broker.py）**：开仓记录 entry_owner（夜盘21点后归下一交易日，与 intraday_backtest.owner_of_dt 同口径；注意与 utils.trade_owner_date 日切口径区分），平仓时同 owner=平今（享平今免费/优惠）、跨 owner=平昨；owner_fn 可注入（测试零网络零日历），任何异常/判不了**保守按平昨**（不虚增免费）；强平与三表重启恢复均带 owner。委托状态明确区分"在途排队 pending（约束缓解后仍可成交，≠拒单）/确定拒单 rejected/锁板阻塞 blocked/已撤"，新增 order_status_counts/positions_view/pending_view 三视图，账户摘要增补 float_pnl/n_pending/status/fill_mode；selftest 25→29 项断言。
- **reports/paper_account.txt（report.py，utf-8-sig）**：账户概览/组合绩效（日度聚合，样本不足诚实标注）/委托状态统计/当前持仓表（含开仓结算交易日）/在途挂单表（带排队原因）/最近20笔成交/诚实说明（影子≥4周对照、成本后为负必须回退、永不自动接实盘）；实时报告新增 4 行紧凑【纸面账户·影子模拟】块（休眠零输出）；实时看板页签注册 paper_account.txt，且**休眠态自动隐藏该页签**（文件不生成、不点开缺失页）。
- **看板新增 3 张图（charts.py，9→12 图、14→15 页签）**：纸面净值（动态/静态权益双线+期初线，权益轴两位小数避免小波动被抹平）、纸面回撤（反转轴）、纸面风险度+同时持仓数双轴（100%强平线）；paper_payload 读 paper_equity 最近2000条升序（修正原快照查询误取最旧N条）、抽稀1200、空/脏/缺方法全显空态；成功与 onerror 两条加载路径都渲染。
- **storage.py**：paper_equity_series 改为"最近 N 条升序"子查询（原 ORDER BY id ASC LIMIT 取的是最旧 N 条）；新增 paper_order_status_counts（GROUP BY status）。
- 验证：新增 tests/test_paper_report.py 7 个零网络用例（休眠零输出不落盘/紧凑块/平仓后空仓/next挂单/txt落盘/页签登记/页签随开关显隐），test_paper_broker +6（平今免费/跨日平昨/owner失效保守平昨/强平leg/恢复重建owner/视图计数）、test_storage +3、test_charts 同步；全量 **pytest 274→295 全绿（约3s，23 个测试文件）**，paper_broker --selftest **29 项断言全过**；前端 `node --check` 通过 + **真实浏览器双态目视**（启用态三图+9 chips 正常、全 null 空态提示不报错不错位、控制台零错误）；**真实 64 品种两轮集成冒烟**（next 档：首轮挂单19，次轮成交11/持仓11/在途8，真实手续费91元、风险度19.59%、权益快照2点、paper_account.txt 4151字符、实时报告纸面块正确渲染）；休眠路径 `main.py --once --no-launch` exit0：无纸面块、不生成 paper_account.txt、chart_data.js paper:null、看板无纸面页签、主链零异常。生产仍 44 个 py（本轮无新增根 py），纯标准库零新依赖。

## [0.27.0] — 2026-09-02 · 第27轮 G1 纸面交易引擎（一）：表 + 撮合状态机（补订单执行层塌陷）
- **新增 `paper_broker.py`（PaperBroker，纯标准库、零网络、零新增依赖）**：把每轮综合分信号串成虚拟委托/成交，对标 freqtrade dry-run / vnpy SimNow。账户内核**直接复用 portfolio.Portfolio**（三种 sizing、单品种/板块/可用资金/持仓数约束链、逐轮盯市、触发线-安全线两段式强平、真实费率），不写第二套资金逻辑；本模块只做"实时轮询信号→委托→成交"状态机与持久化。
- **成交两档（与 G4 回测对齐）**：`close`=信号轮当轮最新价成交；`next`（影子默认、保守）=信号轮只挂单、下一轮首个新价成交、**成交严格晚于信号**，下一轮锁板/无价/临时资金约束则挂单顺延、绝不虚构成交。
- **三阈值迟滞状态机（防抖）**：|综合分|≥PAPER_ENTRY_SCORE(默认4=轻仓线)才开仓/反手，持仓后|分|<PAPER_EXIT_SCORE(默认2=中性线)才离场，中间继续持有；反手先平后开；信号转中性自动撤销遗留挂单；同向挂单排队不重复挂（委托不膨胀）；临时约束（持仓上限/资金/板块/不足1手）保持 pending 顺延，确定性拒单才 rejected。
- **实时锁板与成本**：复用 config.FUTURES_LIMIT_MOVE，相对昨结整根贴板才拦截（买撞涨停/卖撞跌停），缺昨结/缺幅度放行；成交价**内含滑点**（买×(1+slip)/卖×(1-slip)），手续费走真实费率表 data/futures_fees.csv（缺表回退兜底比例），双边成本逐笔可断言。
- **storage 新增三表（业务表 11→14）**：`paper_orders` 委托流水（pending→filled/blocked/rejected/cancelled 全生命周期）、`paper_trades` 开/平成交（含滑点/手续费/已实现/强平标记/pos_ref 配对）、`paper_equity` 每轮一条权益快照（同 ts 覆盖幂等）；配套 insert/update/恢复查询/保留期清理与 table_counts 接入；**进程重启可由三表重建持仓、已实现盈亏、手续费与挂单**（长期影子≥4周的基础）。
- **config 增 PAPER_* 参数段 20 项 + config.template.json 同步**（总开关 PAPER_ENABLED 默认 **False 完全休眠**，本轮不接 main、不动实时监控主链与综合分口径、不改任何现有输出；第28轮再接 main/报告/看板）。
- 验证：新增 tests/test_paper_broker.py 18 个零网络用例（迟滞全分支/锁板/滑点/两档成交时点/反手先平后开/离场/锁板顺延/双边费/强平/资金不足拒单/临时约束排队不膨胀/三表落库与持仓中+平仓后重启恢复/pending 恢复成交/空输入安全/默认休眠），内置 `paper_broker.py --selftest` 25 项断言；全量 **pytest 255→274 全绿（约3s）**；真实 64 品种行情驱动 5 轮（next 档：42挂单→12成交30排队→不重挂→信号归零撤遗留+12平仓单→全平无遗留，orders=54/trades=24，真实费率全命中、零兜底保证金、风险度24.8%、持仓中与平仓后恢复逐值一致）；`main --once --no-launch` exit0 零 Traceback/ERROR；monitor.db 自动向后兼容建 14 表、integrity/quick_check=ok、外键0违规（paper 三表空，未接主链）；compileall 全过。生产 43→44 个 py、15938→16832 行。下一站第28轮=G1（二）接入 main + paper_account.txt + 看板净值。

## [0.26.0] — 2026-09-02 · 第26轮 G4 回测严谨性（next-bar 对照 + bootstrap 置信区间 + 留档 + 防过拟合引用）
- **成交时点双档对照（`--fill {close,next_open}`，默认 close 旧口径逐值不变）**：close=信号根收盘成交（便于纵向比较）；next_open=信号根收盘决策、次根开盘成交（看到收盘已无法以该价成交，更贴近实盘的保守对照），次根跳空锁板顺延、末根才出的入场信号无次根可成交则如实丢弃计数、反手单次根先平后开；信号层 1/5/20 日衰减与成交时点严格解耦（全量两档样本数逐值一致）。
- **交易级 bootstrap 置信区间（纯标准库、固定种子可复现）**：对逐笔净收益有放回重采样 1000 次，给累计收益/最大回撤 P5–中位–P95，总体+多头+空头分别给区间；样本 <20 笔或 `--no-bootstrap` 不给区间（不做小样本假精确），报告注明 iid 假设在强自相关下偏乐观。
- **样本内外对照（`--oos-ratio`，默认 0 关闭）**：按平仓时间排序切前 (1-r) IS / 后 r OOS，总体与多/空分别并列 IS/OOS 指标，样本外衰减一目了然。
- **冲击成本单列（`--impact-rate`，默认 0 等价旧版）**：单边比例、往返两次，与手续费/滑点分开列示，trades CSV 增 `impact_cost` 列（向后兼容）。
- **回测留档（storage 第 11 张业务表 `backtest_runs`）**：每次日线回测落一行（时间/成交档/成本模式/参数 JSON/指标 JSON/样本量），报告抬头标注"历史第几次、累计收益好于百分之多少的历史运行"，纵向对比防"挑一次最好的"；含保留期清理与 table_counts 接入；留档失败软降级绝不拖垮回测。
- **防过拟合交叉引用**：tools/backtest_validation.py 写 txt 同时产出结构化 sidecar `reports/backtest_validation.json`（DSR/CSCV-PBO/WF 摘要），日线回测抬头自动引用最近结论（缺文件软降级）。
- 验证：新增 tests/test_backtest_rigor.py 17 个零网络用例（两档成交时点/末根不虚构/锁板顺延/反手/冲击成本/bootstrap 可复现与退化/分位/IS-OOS/sidecar/留档与百分位/故障软降级），全量 **pytest 238→255 全绿**；真实数据——全 64 品种 close（1713 笔真实费率全命中）与 next_open（1571 笔、末根丢弃10）两档对照、IS/OOS、bootstrap、留档2条与百分位均正常；backtest_validation RB/HC 出 sidecar；`main --once --no-launch` exit0 零 Traceback/ERROR；monitor.db integrity/quick_check=ok、外键0违规（11 张业务表）；compileall 全过。生产仍 43 个 py、15560→15938 行，零新增运行依赖。

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
