# 回归测试套件（P1-1，第21轮落地）

把前 20 轮散落在各轮、验证后即删的**零网络合成断言**固化为永久 pytest 回归网，防止后续改一处、崩一片。

## 运行方式

```bat
:: 在项目根目录 futures_monitor 下执行（本机解释器 D:\Python）
D:\Python\python.exe -m pytest            # 全量
D:\Python\python.exe -m pytest tests/test_portfolio.py -v   # 单文件
D:\Python\python.exe -m pytest -k liquidate                # 按用例名筛选
```

当前规模：28 个 `test_*.py` + `conftest.py`，共 **371** 个用例，全绿约 4.6 秒。

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
| test_portfolio.py | 盯市、三种手数、约束链、强平状态机、费用对账、无bar时刻、校准乘子（第16轮 WP-E） |
| test_storage.py | 分钟bar去重、ML样本覆盖写、跨合约衔接、完整性（storage） |
| test_config_loader.py | .env解析/真实环境优先、深合并、类型矫正、受保护名、端到端子进程加载（第25轮 G10） |
| test_data_router.py | 熔断CLOSED/OPEN/HALF_OPEN状态机、有序主备降级、残缺即弃、全失败、健康总账（第25轮 G11） |
| test_data_health.py | 缺数/陈旧/跳变体检、跨轮连续缺数与连续全失败、增量折算、报告块（第25轮 G6） |
| test_paper_broker.py | 三阈值迟滞、close/next两档成交时点（next严格晚于信号）、实时锁板顺延、滑点与双边费、反手先平后开、离场撤单、临时约束排队不膨胀、强平、资金不足拒单、三表落库与持仓中/平仓后重启恢复、默认休眠（第27轮 G1） |
| test_attribution.py | 方向化暴露/动态键归一、OLS恢复与奇异、加法归因闭合/零方差剔除/空样本、BHB手算+随机fuzz恒等、板块统计rb无方向、累计曲线闭合、IS-OOS、端到端报告（第35轮 G28） |
| test_tools_selftest.py | factor_eval/carry_eval/attribution/build_ml_samples/backtest_validation/db_archive 研究工具自带合成断言 |
| test_compileall.py | 参数化编译全部生产 .py，语法损坏即变红（防“假全绿”） |
