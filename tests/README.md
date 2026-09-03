# 回归测试套件（P1-1，第21轮落地）

把前 20 轮散落在各轮、验证后即删的**零网络合成断言**固化为永久 pytest 回归网，防止后续改一处、崩一片。

## 运行方式

```bat
:: 在项目根目录 futures_monitor 下执行（本机解释器 D:\Python）
D:\Python\python.exe -m pytest            # 全量
D:\Python\python.exe -m pytest tests/test_portfolio.py -v   # 单文件
D:\Python\python.exe -m pytest -k liquidate                # 按用例名筛选
```

当前规模：39 个 `test_*.py` + `conftest.py`，共 **610** 个用例，全绿约 8 秒。

## 设计纪律（必须保持）

1. **零网络、确定性、可重复**：不允许任何用例访问新浪/东财/金十等外部接口。
   - 涉及交易日历的用例统一用 `conftest.py` 的 `flat_calendar` 夹具注入“周一~周五交易、无节假日、周一~周四有夜盘”的确定性日历；
   - 涉及 SQLite 的用例用 `tmp_db` 夹具在 `tmp_path` 建临时库，**绝不读写生产 `data/monitor.db`**。
2. **pytest 只是 dev 工具**：生产 `requirements.txt` 不添加 pytest，常驻监控 `main.py` 不依赖 pytest。
3. 用例按模块组织，文件名即被测模块；新增纯函数逻辑时应同步补对应用例。

## 覆盖模块

| 测试文件 | 覆盖内容（对应历史轮次） |
|---|---|
| test_utils_schedule.py | 交易时段翻转点/5·20分钟刻度/交易日归属/复盘到期/夜盘分档（第6、17轮） |
| test_cross_section.py | 稳健z(MAD/退化/裁剪)、板块聚合、全市场广度（第18轮 B1） |
| test_risk_gate.py | pass/warn/veto 规则、背离、HV、量仓冲突、默认只标注不改分（第18轮 A2） |
| test_signal_calibrator.py | 贝叶斯平滑、四级回退、乘子裁剪、影子模式（第19轮 A3） |
| test_fundamental.py | 库存/龙虎榜/carry/基差四子因子、缺项权重重归一（第13轮 WP-C） |
| test_factors_sentiment.py | 情绪词典极性/否定反转/上下文闸门、五维情绪、NewsFactor 去重衰减（第18轮 D1） |
| test_analyzer_debate.py | 评级四档边界、多空双面卡、同源去重（analyzer） |
| test_option_chain.py | T型链解析、PCR 五档、ATM 定位（第11轮 WP-A） |
| test_iv_surface.py | 报价质量分级、Black-76 二分反推互逆、call/put 合并（第12轮 WP-B） |
| test_intraday_bars.py | 分钟K周期聚合、跨时段不硬拼、合约代码构造（第14轮 WP-D0） |
| test_flow_tracker.py | 增/减仓×涨/跌、跨日重建、量能重置（第7轮） |
| test_backtest_fees.py | 金额费+固定费叠加、固定费折算、绩效统计、分档（第9、10轮） |
| test_backtest_rigor.py | next_open次根成交/末根信号不虚构/锁板顺延/反手、冲击成本、bootstrap可复现与退化、分位、IS/OOS切分、sidecar读取、backtest_runs留档与历史百分位、故障软降级（第26轮 G4） |
| test_portfolio.py | 盯市、三种手数、约束链、强平状态机、费用对账、无bar时刻、校准乘子（第16轮 WP-E）；第41轮 G26续：风险型sizing默认等价/权重定手数与回退/严格PIT无未来/权重和=gross与上限/重置确定性回放/引擎注入留痕（+6） |
| test_storage.py | 分钟bar去重、ML样本覆盖写、跨合约衔接、完整性（storage） |
| test_config_loader.py | .env解析/真实环境优先、深合并、类型矫正、受保护名、端到端子进程加载（第25轮 G10） |
| test_data_router.py | 熔断CLOSED/OPEN/HALF_OPEN状态机、有序主备降级、残缺即弃、全失败、健康总账（第25轮 G11） |
| test_data_health.py | 缺数/陈旧/跳变体检、跨轮连续缺数与连续全失败、增量折算、报告块（第25轮 G6） |
| test_paper_broker.py | 三阈值迟滞、close/next两档成交时点（next严格晚于信号）、实时锁板顺延、滑点与双边费、反手先平后开、离场撤单、临时约束排队不膨胀、强平、资金不足拒单、三表落库与持仓中/平仓后重启恢复、默认休眠（第27轮 G1） |
| test_attribution.py | 方向化暴露/动态键归一、OLS恢复与奇异、加法归因闭合/零方差剔除/空样本、BHB手算+随机fuzz恒等、板块统计rb无方向、累计曲线闭合、IS-OOS、端到端报告（第35轮 G28） |
| test_research_panel.py | 特征注册表与config一致/唯一/动态键、PIT严格asof边界与基本面当日不可见、暖机ret1d手算、扰动法无未来+故意泄漏反向用例、时间戳扫描、训练-服务parity一致+注入检出、PanelStore幂等重建回读、缓存结构审计干净/破坏检出、G21续面板回读roundtrip与xsmom/tsmom面板路径==网络路径/load_adjusted_bars不二次复权（第36/37轮 G21） |
| test_tools_selftest.py | factor_eval/carry_eval/attribution/panel_builder/pit_audit/factor_health/factor_expr/expr_research/factor_regime/portfolio_constructor/portfolio_lab/trade_journal/research_review/build_ml_samples/backtest_validation/db_archive 及根模块 factors_catalog/experiment_ledger/db_backup/portfolio_risk、tools/wf_cost_lab/portfolio_risk_lab/circuit_review 自带合成断言 |
| test_trade_journal.py | 第42轮 G30 交易复盘journal：CSV往返类型/排序、原因归并、信号强度按|分|、七维分桶手算PF、日周ISO聚合、盘中MFE/MAE多空镜像与闭区间、monkeypatch注入bars不碰生产库、全胜桶不误报/弱桶命中、run端到端txt+json、空数据安全 |
| test_research_review.py | 第43轮 G30③ 研究侧一键复盘编排器：sidecar缺/损安全、新鲜度三态、equity的BOM/末尾空记录/全表回撤、signal正则、各段提取排序与弱势桶门槛、规则待办WARN优先/可选降级/全OK、collect空目录与合成sidecar、七段成稿与allow_nan |
| test_experiment_ledger.py | 第44轮 G27① 统一实验台账：config_hash键序无关/同配置一致/参数类型数据敏感、json_safe清洗NaN/时间/集合、文件指纹与数据身份排除mtime内容变才变、记录字段与run_id形态、追加回读/repeat_of串联/同秒碰撞-r2/坏行宽容/原子LF/filter、safe_record成功与不可写吞错、list/show/repeats/export CLI、环境变量关闭重定向 |
| test_portfolio_risk.py | 第47轮 G5①②③ 组合风险纯函数：相关阵(正/负/零方差/板块块)、线性插值分位数参数化、组合收益序列、历史VaR/ES(全正序列VaR为负)、参数VaR精确值与√h缩放及未知分位报错、原油OLS beta精确线性与线性压力方向、分散化收益(不相关为正/完全相关为0)、risk_snapshot端到端与零方差退化（零网络零面板） |
| test_circuit_breaker.py | 第48轮 G5④ 组合层单日浮亏熔断：日期解析/浮亏口径/三档边界/委托过滤/非法参数(参数化)、observe即使delever也恒可开、当日粘性不回落解锁与日切重置、warn仍可开、风险度第二触发与非法安全、from_config，及与PaperBroker的close档集成（paper_halt下新仓被拦、反手只留平仓腿；默认observe无断路器照常开新仓） |
| test_portfolio_nav.py | 第49轮 G5⑤ 组合历史净值曲线：nav_curve复利/不改入参/空序列、drawdown_window手算回撤与峰值谷底日期及安全退化、rolling_proxy的idx与dates对齐且四方法等长共用再平衡日历、玩具面板gmv波动≤等权且净值有限（零网络零面板） |
| test_circuit_review.py | 第50轮 G5④ 熔断阈值历史校准台：loss_of/forward_compound远期复利手算与不足返None、threshold_events含等号穿越、level_counts三档分档、续跌vs反弹条件远期分布对照基准、_dist统计、sweep_halt阈值网格单调与占比有界、analyze_method结构与空序列安全、render三段成稿、selftest（零网络零DB） |
| test_db_backup.py | 第46轮 G19 在线热备/灾备：备份文件名解析与非法名反例(参数化)、prune_plan滚动保留/全保留、只认本工具命名不误删、online_backup一致性+源只读、sidecar表行数、滚动删最旧连同sidecar、同秒碰撞不覆盖、缺源报错、restore恢复到备份点且旧库改名留存、坏备份拒绝恢复、坏库quick_check返OPEN_ERROR、任务XML/bat内容、版本缺失安全（全 tmp_path 造库、绝不碰生产 monitor.db） |
| test_wf_cost_lab.py | 第45轮 G27②③ WF稳定性+成本曲面/换手容量：笔统计与复利、成本曲面沿fee/slip单调与基准格定位、break-even 转负/全程为正/基准已亏三态、WF 稳定-漂移-越界chosen防御与评级、combo名解析、容量手算（市场日均名义/单手名义/参与率上限手数/年换手/空与零乘数降级/字符串日期）、成稿与JSON无NaN（注入假runner零DB） |
| test_factor_expr.py | 第38轮 G25 表达式引擎：21个危险/畸形解析反向用例（未知算子/dunder/属性点/语句拼接/非常量窗口/元数）、delay-delta-窗口统计-ts_rank-minmax-decay_linear-corr嵌套手算、无未来扰动、截面cross_rank/scale/zscore、pearson-spearman/OLS正交恢复β/IC·ICIR加权、实时离线结构性parity、表达式因子必登记且validate干净 |
| test_compileall.py | 参数化编译全部生产 .py，语法损坏即变红（防“假全绿”） |
