# AI 量化全网/GitHub 对标与改进方案（第 16 轮增补 · 只分析未改业务代码）

> 日期：2026-09-01 深夜；方法：全网检索学术与工程资料 + 精读 GitHub 代表项目 + 逐文件核对本项目真实代码（非纸面推断）。
> 约束（不可违背）：生产运行链路零重依赖（仅 requests/uiautomation/websocket-client，pandas 及 ML 库只允许出现在 `tools/`）；只采纳"确定不更差"的改动；每个改动配零网络合成断言；负结果诚实呈现。
> 定位：本文是《GitHub对标与改进清单.md》（第 5 轮，23 条，偏工程）的**姊妹篇**，专补"AI/机器学习/LLM"这一第 5 轮未展开的维度。

---

## 〇、结论先行（TL;DR）

1. **AI 量化在 GitHub 上就四族**：①多智能体 LLM 决策（TradingAgents 69k★、ai-hedge-fund 62k★）；②机器学习因子/预测平台（微软 Qlib、López de Prado《金融机器学习》方法论）；③深度强化学习（FinRL 系）；④金融 NLP/LLM 情绪（FinBERT、中文轻量 LLM、期货多维情绪论文）。
2. **学术界和开源实践同时给出一条冷证据**：商品期货上，**时序/截面动量是最稳的预测信号，复杂深度模型经常跑不赢简单趋势规则**（Stanford 期货趋势 ML 对比、E-mini 神经网络二分类 49% 低于随机、XGBoost 52% 命中但 alpha 为负；正面样本 Bayes-CID 2026 商品 GBDT 也是"时序动量主导 + 8 周期集成 + 严格 purged 交叉验证"才做出样本外夏普 2.4）。**结论：本项目不应追 DNN/DRL 热点，应把 AI 用在"分层裁决、横截面、因子评估闭环、情绪多维化、LLM 只做文本复核"这五件高确定性的事上。**
3. **本项目的 AI 化底子比多数开源项目好**：8 张 SQLite 表（结构化样本库）、`signal_outcomes` 已在产出 hit/miss 标签、三套回测 + 18 组稳定性网格（天然防过拟合）、`intraday_backtest` 的止损/止盈/超时撮合（天然 triple-barrier 标签生成器）、组合资金账户与强平状态机（多数 AI 项目反而没有真实资金层）。缺的不是"上模型"，而是**把这些数据资产用起来的评估闭环和分层决策结构**。
4. 落地按四个工作包推进：**WP-F1（纯规则、零依赖、生产侧，先做）**、**WP-F2（用自有 DB 做统计校准/因子评估/样本集）**、**WP-F3（LLM 情绪复核可选适配层，无 key 降级）**、**WP-F4（tools/ 研究侧 LightGBM，跑赢规则才允许导出 JSON 给生产零依赖推理）**。**明确不做**：端到端 LLM 决策、深度强化学习实盘、价格序列深度预测、把 ML 重依赖装进生产。
5. 对现有使用的影响：F1/F2 全部标准库实现且**默认不改变现有综合分行为**（新增信息层 + 配置开关），F3 无 key 零成本降级，F4 不装 sklearn 也能正常运行（缺模型文件自动回退线性规则）——与引入 tushare/tdx 同一套"可选增强、绝不绑架主链路"范式。

---

## 一、调研对象：四族 AI 量化 × 代表项目 × 对本项目裁决

### 1.1 多智能体 LLM 决策框架

| 项目 | 形态 | 可借鉴的"结构"（不是抄 LLM） | 不采纳的部分 |
| --- | --- | --- | --- |
| **TradingAgents**（TauricResearch，UCLA/MIT，论文 arXiv:2412.20138，69k★，LangGraph） | 基本面/情绪/技术分析师 → **多头研究员 vs 空头研究员结构化辩论** → 交易员 → **风控团队独立否决** | ①任何强信号必须同时生成"反方观点"（多空双面论证）；②风控是**独立于信号的否决层**，不是信号里的一行提示；③角色分工对应本项目已有因子，只需重组裁决结构 | 全程 LLM 调用（慢、贵、非确定性，作者自己声明仅研究用途、结果随 temperature 漂移）；股票导向 |
| **ai-hedge-fund**（virattt，62k★，18 agent） | 12 投资大师 + 估值/情绪/基本面/技术分析师 + **Risk Manager 算敞口定仓位上限** + **Portfolio Manager 汇总裁决出单** | ①"信号层 → 风险层 → 组合层"三段式，正好对应本项目 analyzer→portfolio 的现状，缺的是中间的**实时风控闸门**；②多风格投票 ≈ 本项目多因子，但要显式展示"几票多/几票空/谁反对" | 大师 prompt 对商品期货不适用；纯 LLM 出单不可复现、不可回测 |
| TradingAgents-CN（中文增强） | 中文新闻过滤/辩论 | 中文期货新闻的结构化抽取思路（并入 1.4） | 同上 |

### 1.2 机器学习因子/预测（本项目最该学的一族）

| 来源 | 核心方法 | 对本项目的具体价值 |
| --- | --- | --- |
| **微软 Qlib**（microsoft/qlib，AI 量化平台，模型库 LightGBM/XGBoost/CatBoost/MLP/LSTM/Transformer，Alpha158 因子集） | 工作流 DataHandler→Dataset→Model→组合→执行→**Analyser（IC/RankIC/分层回测）**；**PIT 数据防未来函数**；滚动训练；模型集成 | ①学它的**因子评估闭环**（本项目权重是拍的，应用自有 signal_outcomes 算 IC/分档单调性来校准）；②PIT 理念落到样本构造（特征只用 t 之前、标签用 t 之后）；③模型只做"多因子融合器"，不做价格预测 |
| **López de Prado《Advances in Financial ML》**（金融 ML 方法论，GitHub: How-To-Backtest-Correctly、mlfinpy） | **Triple-Barrier 标签**（上轨止盈/下轨止损/纵向超时，谁先碰到定标签）、**Meta-Labeling（主模型定方向、次模型定"这笔下多大/做不做"）**、**Purged K-Fold + Embargo 防时序泄漏**、CPCV、样本权重、SHAP 选特征 | ①本项目 `intraday_backtest` 已有止盈/止损/最大根数，**可直接离线生成 triple-barrier 标签集**；②meta-labeling 思想可用标准库落地为"信号方向不变、用历史胜率校准手数"（见 B/A3）；③未来任何 ML 必须走 purged CV，禁止随机 K 折 |
| 商品 CTA 工程界 | **时序动量 + 截面动量双驱动**、多品种等权多空、周/日再平衡（2026 仍在实盘的主流 CTA 形态） | 本项目只有**单品种时序**信号，**缺横截面排序**——64 个品种每天都在打分却从不横向排名，这是最确定的一块补强项（见 B1） |

**反面证据（决定"不做什么"，和正面方法同等重要）**

| 来源 | 结果 | 启示 |
| --- | --- | --- |
| Stanford MS&E 2019《期货市场趋势策略的 ML 方法》 | 简单趋势跟踪夏普最高，学到的模型没捕捉到趋势之外的逻辑，**准确率提升不等于盈利** | 先把动量/截面做扎实，再谈模型 |
| GitHub lrud/FUTURES_NN（E-mini SP500） | 神经网络二分类准确率 **49.08%，低于随机基线** | 裸价格序列喂 DNN 不可取 |
| GitHub regime-aware（金银 XGBoost/RF） | 准确率 52–55%，但样本外 **alpha 大幅为负（-29%~-244%）** | 命中方向 ≠ 覆盖成本后赚钱（本项目第 15/16 轮已用真实成本得到同样结论） |
| Bayes-CID 2026 夏（41 商品/30 年/126 因子 GBDT） | 样本外夏普 2.4，但靠的是**时序动量主导 + 偏度辅助 + 8 周期集成 + 严格交叉验证** | ML 有效是有前提的：稳信号、多周期、防泄漏、样本外成本后评估 |

### 1.3 深度强化学习（FinRL 系）——裁决：远期/不做

FinRL/FinRL-X（AI4Finance，A2C/PPO/DDPG/TD3/SAC，论文在美股 DJIA 上夏普 2+）和 Qlib RL 订单执行。**不采纳为项目方向**，理由三条且每条都与本项目现实绑定：①DRL 论文漂亮结果集中在股票、且公认存在非平稳/高方差/过拟合问题（Safe-FinRL 自己都在打补丁）；②本项目分钟历史自 2026-09-01 才开始自采、1m 仅数个交易日，样本量远不足以训 RL；③RL 训练依赖 torch 生态，与生产零依赖架构根本冲突。**唯一可吸收的是 FinRL-X 的 S/A/T/R 分层思想（选股-配置-择时-风险覆盖），用规则实现即可**（已并入 A2/A 层设计）。

### 1.4 金融 NLP / LLM 情绪——裁决：多维化（规则先行）+ LLM 仅复核

- 现状学术：FinBERT 主导英文金融情绪；2025《北京师范大学学报》"**大模型驱动的期货市场新闻多主题多层次情感**"用"整体—主题—方面/事件"层次化 + 方面观点情感三元组；arXiv 2026《WTI 原油期货多维 LLM 情绪》用**相关性/极性/强度/不确定性/前瞻性**五维预测周收益，优于单极性。
- 本项目现状：`factors.py` 约 70 词词典 + 15 条正则 + 局部否定/转折反转，输出**一维极性分**（再乘可信度）。差距=只有"多空"，没有"多确定/多强/是不是真相关/是预期还是落地/属于哪类事件"。
- 成本现实（2026 行情）：云端 DeepSeek 级 API 已到**每百万 token 元级**、新用户送额度；本地 Ollama + Qwen2.5 7B(Q4) 约 5–8GB 内存、无独显 CPU 可跑（慢）。→ **LLM 复核在成本和通道上可行，但必须是"少量、异步、可降级"的配角**。

---

## 二、本项目已有的 AI 化底子（先盘点，避免重复造轮子）

| AI 量化系统该有的东西 | 本项目现状 | 评价 |
| --- | --- | --- |
| 结构化样本库 | SQLite 八表（quotes/signals/news/options/signal_outcomes/option_chains/fundamentals/minute_bars），WAL、180 天/长期保留 | **多数 GitHub AI 项目要现造，本项目已有** |
| 标签 | `signal_outcomes` 对 ≥2 分信号自动做 30m/2h/1d 三周期评估（hit/miss/flat/expired），已有胜率统计 | 现成的监督学习标签 |
| 路径依赖标签能力 | `intraday_backtest` 止损/止盈/最大持仓根数/锁板撮合 | 天然 triple-barrier 标注器 |
| 防过拟合文化 | 18 组参数稳定性网格、毛/净双口径、真实手续费+滑点+公司保证金、负结果照实写 | 与 purged CV/样本外同价值观 |
| 多因子 | 新闻/原油/机构/日线动量/技术共振/分钟共振/盘中动量/量仓/基本面 9 类因子线性融合（analyzer.py:46-91 `parts`） | 特征工程已具规模，缺评估与非线性融合 |
| 资金/风控层 | `portfolio.py` 共享资金池、三种 sizing、板块/名义/可用/持仓数约束链、强平状态机 | 比多数 AI demo 完整得多 |
| 可复现 | 零网络合成断言、确定性字母序回放、干净进程复跑 | ML 训练/验证可直接继承 |

**真正缺的是 5 块**：①线性单点打分，没有多空对立审查和独立风控闸门；②没有横截面比较；③因子权重没有数据评估闭环；④情绪只有一维；⑤没有"研究重/生产轻"的 ML 引入通道。下面逐条给具体改法。

---

## 三、具体改进方法（到文件/函数级，按层分组）

> 标注约定：【F1/F2/F3/F4】=所属工作包；每条给"现状 → 改法 → 触碰文件 → 零依赖做法 → 验证断言 → 默认行为"。

### A 层：决策结构（多空双面 + 独立风控闸门 + 胜率校准）

**A1 多空双面论证卡（bull/bear debate 的规则化）【F1】**
- 现状：`analyzer.analyze_variety` 把 `parts` 正负值直接 `sum` 成一个分，分歧被加总抵消，报告只看到结论。
- 改法：在 score 算出后（analyzer.py:91 之后），**不改变 score**，另产出 `debate = {"bull":[正向因子名+值+一句理由], "bear":[反向...], "decisive": 绝对值差最大的因子}`；重点品种明细（`detail_lines`）加一行"多: 技术+2.1/基本面+1.1｜空: 新闻-1.8｜决定项=技术"。
- 文件：`analyzer.py`（纯函数 `build_debate(parts, notes)` 便于单测）、`report.py` 明细渲染。
- 验证：合成 parts 断言多/空清单分组正确、全中性时不出卡、decisive 取值正确。
- 默认：纯展示增强，综合分与告警逻辑不变。

**A2 独立风控闸门（Risk Manager 的规则化，区别于现有 risks 提示）【F1】**
- 现状：`risks[]` 只是**文字提示**，不影响建议；真正的硬约束只存在于离线回测 `portfolio`。
- 改法：新增纯函数 `risk_gate(row, ctx)`（建议放 `analyzer.py` 或新 `risk_gate.py`），输出 `gate = {"verdict": pass/warn/veto, "level": 0/1/2, "reasons":[...]}`，检查项全部可计算：锁板/接近涨跌停（用 FUTURES_LIMIT_MOVE 或 ft_limit 板价）、流动性（成交量/持仓量低于品种分位）、信号方向与主力期限结构 carry 背离、综合分来自单一因子（如仅新闻拉动、技术/基本面全中立）、板块拥挤度（同板块同向信号过多，依赖 B1 截面结果）、临近交割（已有 dd<45 提示，升级为硬降级）、HV 极端分位。**veto=建议降级为观望/不下手数，warn=减档**；在 main 拿到 row 后、告警/报告前应用。
- 文件：新增 `risk_gate.py`（零依赖纯函数）、`analyzer.py`/`main.py` 接线、`config.py` 加 RISK_GATE_* 开关与阈值、`report.py`/`alerts.py`（被 veto 的信号不推送"可交易"告警，改提示）。
- 验证：逐检查项构造多/空/边界合成断言；veto 后不出交易建议、warn 后降档；开关关闭时行为与现状逐值一致（回归保护）。
- 默认：`RISK_GATE_ENABLED=True` 但仅 veto 极端情形（阈值保守），保证"确定不更差"。

**A3 方向与仓位分离：历史胜率校准 sizing（meta-labeling 的零依赖版）【F2】**
- 现状：手数档位只看当下综合分（portfolio 的 score sizing：轻仓 5%/分批 10%/强信号 15%）；`signal_outcomes` 已积累"什么分档/什么因子组合→历史 hit 率"却没用于 sizing。
- 改法：新增 `signal_calibrator.py`，从 `storage` 读 signal_outcomes，按"方向 × 综合分档 × 主导因子"做**贝叶斯平滑胜率/平均方向收益**（(hits+α)/(n+α+β)，小样本不激进），输出置信度乘子 `conf_mult∈[0.5,1.2]`；`portfolio.py` 的 score/equal_risk sizing 乘该乘子，实时侧仅展示"历史同类信号胜率 x%（n 笔）"。样本不足阈值（如 n<20）不校准、返回 1.0。
- 文件：新增 `signal_calibrator.py`（标准库）、`storage.py` 加分组聚合查询、`portfolio.py` 接可选乘子、`report.py` 信号追踪页扩展。
- 验证：合成 hit/miss 序列断言平滑公式、小样本回退 1.0、乘子裁剪区间；真实 DB 跑一遍只统计不改动交易（影子模式）先观察。
- 默认：先影子模式（只展示不下发），确认分布后再启用，且可 `--no-calibrate` 关闭。

### B 层：因子与信号（横截面 + 因子评估闭环 + ML 样本集）

**B1 横截面强弱排行（商品 CTA 最稳的缺失块）【F1】**
- 现状：64 品种每轮各自打分（main 收集 `fut_rows`），从不横向比较；GitHub 清单条目⑮"板块强弱"一直未做。
- 改法：新增 `cross_section.py` 纯函数：对当轮全部品种的综合分/日线动量分/基本面分分别做**横截面 z-score 与分位**（稳健版用中位数/MAD），按板块（config 已有板块归属）聚合板块均值，输出：全市场多空 Top/Bottom、板块强弱榜、板块内排名、"同板块多空打架"拥挤提示（喂给 A2）。`main.run_cycle` 在全部 analyze 完成后统一调用一次；`report.py` 期货总表后加【横截面强弱】块；可作为组合层"只做多空各前 N"的候选过滤（portfolio 加 `--cross-section` 开关）。
- 文件：新增 `cross_section.py`、`main.py`/`report.py`/`config.py`、`portfolio.py` 可选过滤。
- 验证：构造已知分布断言 z-score/分位/板块聚合/极值稳健（MAD 不被单点拉爆）；缺值品种不参与也不报错。
- 默认：展示层立即生效；组合过滤默认关，回测对比后再定。

**B2 因子 IC/RankIC 评估闭环（学 Qlib Analyser，替代拍脑袋权重）【F2】**
- 现状：9 类因子的裁剪幅度/权重是经验设定，从未用自有数据检验过预测力。
- 改法：`tools/factor_eval.py`（研究侧、允许 pandas/numpy）：从 monitor.db 导出 signals（含各因子拆分，已入库）+ signal_outcomes，计算每个因子对 30m/2h/1d 远期方向收益的 **IC（Pearson）、RankIC（Spearman，手写相关系数或 numpy）、ICIR、分档单调性、多空价差、半衰期衰减**，产出 `reports/factor_eval.txt`；跑 walk-forward（用前 K 月统计、后 1 月验证，滚动），给出"建议权重区间"供人工调整 config，**不自动改权重**（避免过拟合 + 保留可解释）。
- 文件：新增 `tools/factor_eval.py`、`storage.py` 加因子拆分导出查询；文档记录评估口径。
- 验证：构造因子与收益完全单调/完全无关两组合成数据，断言 RankIC≈1/≈0；断言标签窗口严格晚于特征（无泄漏单测）。
- 注意：这是定期人工跑的研究工具，不进常驻链路。

**B3 Triple-Barrier 样本集（为 F4 备料）【F2】**
- 现状：要上任何监督模型，先得有正确标签；固定窗口"未来 N 根涨跌"是错误标签（学术已证）。
- 改法：`tools/build_ml_samples.py` 复用 `intraday_backtest` 的撮合口径，对 minute_bars/daily 离线生成路径依赖标签：每信号点挂 +k·ATR 止盈 / -k·ATR 止损 / 最长 M 根超时，先碰到谁定 label {1,-1,0}，同时落特征快照（9 类因子 + 截面 z + 多维情绪），写新表 `ml_samples`（storage 第 9 张表，UNIQUE(sym,bar_dt)，长期保留）。严格 PIT：特征列全部 ≤t、标签路径 >t，并加 embargo（标签跨越的样本不进相邻训练折）。
- 文件：`tools/build_ml_samples.py`、`storage.py` 加表、复用 intraday_backtest 纯函数。
- 验证：构造先触止盈/先触止损/横盘超时/跳空穿越四类合成路径断言标签；断言无特征穿越。

### C 层：机器学习模型通道（研究重、生产轻）

**C1 建立"tools 训练 → 导出 JSON → 标准库推理"的唯一合规通道【F4，架构规则】**
- 改法：`tools/ml_train/` 内允许 scikit-learn/lightgbm（与 pandas 同等待遇，**不进 requirements、不进生产**）；训练产物只导出为极简 JSON（逻辑回归=权重向量/树模型=分箱+分裂阈值表/特征列表/版本/训练区间/样本外指标）。生产新增 `ml_inference.py` 用**标准库实现该 JSON 的前向推理**（线性点积、或深度受限决策树的 if-else 遍历），缺模型文件/版本不符 → 自动回退现有线性 `parts` 打分并登记 fallback。这样生产环境永远零重依赖。
- 验证：用 sklearn 训练 → 导出 → 标准库推理，断言两者预测逐值一致（容差 1e-9）；缺文件回退断言。

**C2 第一阶段模型 = 多因子融合/meta-label，不碰深度预测【F4】**
- 改法：目标不是预测价格，而是学习"给定 9 因子+截面+多维情绪特征，未来 triple-barrier 为正的概率"，模型限定 LightGBM/逻辑回归这种可解释浅模型；**强制** Purged TimeSeriesSplit + embargo、样本外、真实成本后评估；**上线门槛写死：样本外成本后净值与稳定性必须 ≥ 现有线性规则，否则不上线**（呼应 Stanford/49% 那类反面证据与本项目"确定不更差"铁律）。评估通过也只作为综合分的一个 `ML融合` 因子（裁剪小权重）或 A3 的校准器，不端到端接管决策。
- 文件：`tools/ml_train/`、生产 `ml_inference.py`、`analyzer.py` 加可选因子位、config 开关（默认关，评估通过再默认开）。

**C3 明确不做清单（写进文档防止以后重复踩）**：端到端 LLM 自动交易、DRL 实盘仓位、裸 K 线 LSTM/Transformer 价格预测、在线学习热更新（不可复现）、把 torch/sklearn 装进生产 requirements。

### D 层：情绪 NLP（多维化 + LLM 复核）

**D1 五维情绪（规则先行，零依赖）【F1】**
- 现状：`factors.NewsFactor` 只输出极性。
- 改法：在不改极性主分的前提下，为每条新闻额外打五个维度（对齐北师大学报/WTI 论文）：**polarity 极性**（现有）、**intensity 强度**（程度副词/“大幅/骤/飙/巨”×系数）、**uncertainty 不确定性**（“或/预计/据称/传闻/待确认/有望”，与现有 doubtful 可信度衔接但语义独立）、**relevance 相关性**（是否直接点名品种/板块词典，0/0.5/1）、**forwardness 前瞻性**（“预期/计划/拟/明年”=前瞻 vs “已/落地/公布”=事实）、再加 event_type 事件类别（供给/需求/库存/宏观/政策/地缘/汇率）。聚合时：最终情绪分 = 极性×强度×相关性，不确定性高的降权（已有 confidence 机制扩展），前瞻与事实分开计数。
- 文件：`factors.py`（词典补强度/不确定/前瞻词与事件类型映射，纯函数 `sentiment_facets(text)` 单测）、`storage.news` 表加维度列（或塞 raw_json，兼容旧行）、`report.py` 重点新闻展示维度角标。
- 验证：逐维构造句子断言（如"传闻拟大幅减产"=前瞻+不确定+高强度+供给类）；旧一维调用路径保持兼容。

**D2 LLM 情绪/信号复核适配层（可选、异步、可降级）【F3】**
- 现状：P2-22 与第 5 轮都预留了"LLM 仅复核紧急消息"，未落地。
- 改法：新增 `llm_reviewer.py`：**只**对三类小流量文本调用——①触发紧急轮动的新闻；②|综合分|≥6.5 强信号的相关新闻簇；③词典情绪与技术/基本面方向明显背离的样本。走 OpenAI 兼容 HTTP（DeepSeek/本地 Ollama 同一套 chat/completions，用现有 http_client），**强制 JSON 输出 schema**（方向∈{多,空,少}/强度1-5/涉及品种[]/不确定性1-5/一句话理由/与词典是否一致），守护线程异步执行、超时/无 key/HTTP 失败一律软降级为"无复核"，结果入库并在报告标"AI复核:…"，**只做第二意见与分歧标注，永不直接改综合分、永不自动下单**。key 走环境变量 `FUTURES_MONITOR_LLM_KEY/BASE_URL/MODEL`（同 webhook 先例）。成本估算：每轮仅 0~数条短文本，元级 API 下月成本可忽略。
- 文件：新增 `llm_reviewer.py`、`config.py` LLM_* 段、`main.py` 守护线程/提交点、`storage` 可选表或 news.raw_json、`report.py` 角标。
- 验证：用本地 mock HTTP（标准库起假 server 或 monkeypatch http）断言 schema 解析、字段越界裁剪、超时/坏 JSON/无 key 三条降级路径、绝不抛进主循环；断言主分不被复核结果修改。
- 默认：无环境变量即完全不启用、零请求零开销。

### E 层：研究与工程基础设施（与既有 P2 合并推进）

- **E1 PIT/防泄漏规范固化**：把"特征 ≤t、标签 >t、跨标签窗口 embargo、禁随机 K 折、时间序列 purged 折"写进 `tools/` 研究规范和 B2/B3/C2 的断言（学 Qlib PIT）。【F2】 ✅防过拟合验证器已落地（第20轮 tools/backtest_validation.py：PurgedKFold+embargo、DSR、CSCV-PBO、walk-forward、参数高原，含合成断言与64品种真实报告）
- **E2 模型/因子回归测试**：结合 P2-②pytest，把"因子单调性、标签无泄漏、导出模型与训练库推理一致、样本外成本后不低于规则"做成 tests/ 固定断言。【F2/F4，依赖 P2 pytest 先落地】
- **E3 截面/因子可视化**：结合 P2-①看板 ECharts，画截面多空热力图、因子 IC 条形、权益/回撤曲线（替代 iframe 嵌 txt）。【F1 展示数据先备好】
- **E4 与 tushare 增强联动**：若后续引入 tushare（见《Tushare引入可行性分析.md》），fut_wsr 全历史仓单/ft_limit 真实板价会直接提升 D 层基本面因子质量与 A2 锁板判定，两条路线互补不冲突。

---

## 四、工作包编排（补进第 16 轮之后的路线，与 P2 工程项并列）

| 工作包 | 内容 | 依赖 | 生产依赖 | 预期产出 | 优先级 |
| --- | --- | --- | --- | --- | --- |
| **WP-F1 规则化决策与截面（先做，确定性最高）** | A1 多空双面卡、A2 风控闸门、B1 横截面强弱、D1 五维情绪 | 无，全部现有数据 | **零新增、标准库** | risk_gate.py/cross_section.py 新模块，analyzer/report/main/config 增量，综合分默认不变 | 最高，建议紧接 P2 之前或并行 |
| **WP-F2 数据驱动校准与样本资产** | A3 胜率校准 sizing、B2 因子 IC 评估、B3 triple-barrier 样本表、E1/E2 规范 | signal_outcomes/minute_bars 数据量（持续积累中） | 生产零新增；tools 可用 numpy | signal_calibrator.py、tools/factor_eval.py、tools/build_ml_samples.py、storage 第 9 表 ml_samples | 高 |
| **WP-F3 LLM 复核配角** | D2 llm_reviewer 适配层 | 需要用户自备 key（可选） | 零新增（HTTP 走现有 client） | llm_reviewer.py，无 key 完全休眠 | 中，按需 |
| **WP-F4 浅 ML 融合（严格门槛）** | C1 研究重/生产轻通道、C2 LightGBM/逻辑回归融合、导出 JSON 标准库推理 | F2 样本集 + P2 pytest | tools 才装 sklearn/lightgbm；**生产仍零依赖** | tools/ml_train/、ml_inference.py、模型 JSON，跑不赢规则不上线 | 低，水到渠成 |
| **不做** | 端到端 LLM 决策、DRL 实盘、裸价格深度预测、ML 重依赖进生产、在线热更新 | — | — | — | — |

**与现有路线的关系**：WP-A~E 已全部完成（第 11~16 轮）；P2 工程项（看板图表化/pytest/配置外置/数据源降级链/板块价差）继续，其中板块价差与 B1 横截面合并、看板图表化承接 E3、pytest 承接 E2。**WP-F1/F2 不依赖 P2，可独立先行；F4 必须在 P2-②pytest 之后。**

---

## 五、对"现有使用"的影响评估（直接回答）

1. **运行依赖**：F1/F2/F3 生产侧**不新增任何第三方包**（标准库 + 现有 requests/http_client）；F4 的 sklearn/lightgbm 只装在 tools 研究环境，生产用标准库读 JSON 推理，缺模型自动回退——requirements.txt 生产部分保持三行不变。
2. **现有行为**：F1 的多空卡/横截面/五维情绪默认是**信息增量**；A2 风控闸门默认只对极端情形 veto（阈值保守、可一键关，关闭时与现状逐值回归对齐）；A3 先影子模式；F3 无 key 完全休眠；F4 默认关闭。即**升级后不配置任何东西，监控、告警、三套回测、看板的原有输出与现在一致，只是多了增强信息**。
3. **性能**：横截面每轮一次 O(n) 纯计算（64 品种）、五维情绪是词典级字符串处理、胜率校准读本地 SQLite，均为毫秒~十毫秒级；LLM 复核在守护线程异步，不阻塞 run_cycle（与 alerts/web_scan 线程同范式）。
4. **数据与可复现**：新表 ml_samples、news 维度扩展均向后兼容（INSERT OR REPLACE/raw_json 兜底旧行）；所有新纯函数配零网络合成断言，沿用 16 轮验证范式；ML 模型带版本/训练区间/样本外指标，结果可复现，不做在线学习。
5. **风险与边界（诚实声明）**：①AI/ML 不改变本项目已验证的负结果——分钟高频在真实成本后不赚钱，模型不会凭空创造 alpha，只可能小幅提升信号质量与仓位合理性；②商品 64 品种、分钟样本仅自 2026-09-01 起积累，F4 真正可用要等样本积累，**不提前上模型**；③LLM 输出有非确定性，故只做复核标注、不进决策闭环；④所有"AI 建议"继续标注"不构成投资建议"。

---

## 六、主要参考来源（全网 + GitHub）

- TradingAgents（多智能体 LLM，UCLA/MIT）：https://github.com/TauricResearch/TradingAgents ；论文 https://arxiv.org/pdf/2412.20138v7
- ai-hedge-fund（大师/分析师/Risk/PM 多智能体）：https://github.com/viratt（社区镜像与解读，62k★，LangGraph 三段式裁决）
- 微软 Qlib（AI 量化平台/模型库/PIT/IC 评估）：https://github.com/microsoft/qlib ；模型 https://qlib.readthedocs.io/en/stable/component/model.html
- López de Prado 金融 ML（triple-barrier/meta-labeling/purged CV/CPCV）：https://github.com/Neyt/How-To-Backtest-Correctly ；https://paperswithbacktest.com/course/meta-labeling
- FinRL / FinRL-X（DRL，S/A/T/R 分层）：https://arxiv.org/pdf/2111.09395.pdf ；https://ai4finance.org/FinRL-Paper.pdf
- 商品期货 ML 现实证据：Stanford 趋势 ML（简单趋势胜出）http://stanford.edu/class/msande448/2019/Final_presentations/gr5.pdf ；FUTURES_NN 49% 分类 https://github.com/lrud/FUTURES_NN ；Bayes-CID 2026 商品 GBDT（动量主导/集成/夏普2.4）https://www.bayes-cid.com/pdf/issues/2026-summer/publications/CID-Summer-2026-Guida.pdf
- 期货 LLM 多维情绪：北师大学报 2025（期货新闻多层次情感）http://www.bnujournal.com/article/doi/10.12202/j.0476-0301.2025142 ；WTI 五维情绪 https://arxiv.org/html/2603.11408v1 ；轻量 LLM 情绪对比 https://arxiv.org/html/2512.00946v1
- LLM 成本/本地部署（2026）：DeepSeek API 元级定价与 Ollama+Qwen2.5 7B 本地部署资料（CSDN/掘金，价格以官方页为准）
- 本项目内部依据：上下文摘要.md（第 5/7/8/13/14/15/16 轮）、GitHub对标与改进清单.md、analyzer.py(parts:46-91)、storage.py(signal_outcomes)、intraday_backtest.py、portfolio.py
