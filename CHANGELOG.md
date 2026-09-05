# 更新日志（CHANGELOG）

本项目按"轮"迭代，版本号 `主.轮.补丁`，与 `VERSION` 对齐；详细过程见 `上下文摘要.md`。
铁律：生产纯标准库 + 三个直接依赖；默认行为可回退；每轮合成断言 + 真实冒烟 + 负结果诚实呈现。

## [0.77.0] — 2026-09-05 · 第77轮 expr_research 补逐日截面层（双工具口径统一） + catalog 研究卡体检回写 + term_history 增量补K线
- **G25续② 体检管线统一：expr_research 补逐日截面IC层**：对全部 33 条 LIBRARY 因子输出【时序层 meanIC/pooledIC + 截面层 逐日跨品种 mean/ICIR/t/正比例】双口径（复用 expr_miner.cross_section_ics/cs_summary，同口径不再割裂）；报告新增每因子截面行+截面上榜区；`--min_cs` 参数可调（默认与 expr_miner 同为 10）。selftest 5→6组（临时面板端到端跑 run() 验 cs 结构/强截面健全性）。
- **真实验证（64品种/33因子）**：**新发现 expr_tsmom252 截面正IC H20 +0.081/t+11.0**（252日动量全样本截面延续，与"动量证伪"策略口径不矛盾、与第39轮"低波有效/高波反转"regime 分层共存——判"待复核"，未过 placebo 不晋升）；**低波异象族互证**：expr_hv20 H20 -0.058/t-8.8 与 range_pct5/20（-0.068/-0.066）同向——高波动/高振幅品种系统性跑输；截面上榜共 4 条全量列出。
- **G29续① catalog 体检卡回写**：factors_catalog.HEALTH_SNAPSHOT 新增 research_cards 段（asof 2026-09-05），登记 expr_range_pct5/20（健康·反向·纯截面，含第76轮 placebo 撤回注记）、expr_tsmom252（待复核）、expr_hv20（健康·反向·随族观察）四张卡——体检卡从 9-part 事件层扩展到研究因子日频面板层，结论脚本可刷新、key 由 CATALOG 钉死。
- **G22续④ term_history 增量补K线（top-up，修"缓存不回补"缺口）**：第74轮探针发现 fetch_one_contract 对已缓存合约整体跳过、缓存永不回补——新增 Store.max_bar_date + topup_decide（纯函数：无缓存→new / 仍挂牌且末根落后 stale_days→stale / 已退市不补）+ topup_varieties（重拉按 INSERT OR REPLACE 幂等合并）+ CLI `--topup`（fetch_one_contract 加 force 参数）。**真实验证**：检查384个近月合约→49个在挂牌"末根落后"合约全部补拉成功（末根从≤08-26 前进到 09-02=新浪日K当前前沿，RB2701/RB2609 验证）；41个从未缓存的旧月份合约被新浪拒绝（RuntimeError，软降级登记留待重试）。
- selftest：expr_research 5→6组、term_history 8→9组；pytest 746 全绿；研究侧零改动主链（隔离 grep 合规）。

## [0.76.0] — 2026-09-05 · 第76轮 placebo 证伪第75轮条件化解读（诚实撤回） + range_pct 晋升 LIBRARY 研究卡 + carry roll口径条件化不增益
- **⚠️ 诚实撤回（元方法生效）**：regime_cond_lab 新增 `--robust` 稳健链（H网格/子期分段/placebo标签重排，判定标准先于结果写死）——**placebo（确定性种子把"日期→标签向量"整体重排）后的"伪低波"视图 净年化 -12.40%/IC -0.149，与真实低波口径 -11.03%/-0.148 几乎相同** ⇒ 第75轮"低波条件化把 |IC| 放大到 0.148"是**分桶/样本构成假象，不是 regime 效应**，"低波反向≈+11%年化"的解读撤回（幸好第75轮已标注"仅相对命题、不进综合分、阈值不调参"）。真正的教训写进报告：placebo 是因子×regime 研究的必要门槛。
- **稳健链中依然成立的**：range_pct 全样本截面负 IC 跨 H 稳定（H5/10/20/40 = -0.039/-0.071/-0.082/-0.079）且跨子期方向一致（前半 -0.111/后半 -0.061）——因子本身的"高振幅品种未来跑输"是稳健负结果（研究卡合法），条件化增强不成立。
- **② range_pct5/20 晋升 LIBRARY+catalog（人工复核路径）**：factor_expr LIBRARY 31→33（expr_range_pct5/expr_range_pct20，白名单 DSL、仅用 high/low/close）、factors_catalog 52→54（introduced=76，layer=表达式研究，status=research 不进综合分；登记链=第74轮 expr_miner 全池体检上榜 → 第75轮 regime 实验 → 第76轮 placebo+跨H/子期复核）；LIBRARY 遍历/parity 循环无需新字段（high/low 已有）。expr_research 重跑刷新研究卡 IC。
- **④ G23续 compare_regime 加近月含roll口径**（points_near 关键字参数+渲染近月子截面块）：**近月含roll 全样本 52期/净t+1.39 → 仅低波 33期/+0.74 → 仅高波 24期/+1.36**——展期收益不依赖低波状态，与主连口径（低波 t-0.14）一致确认 **regime 条件化对 carry 两口径均无增益**。
- **③ 偏度算子评估收口（不硬做）**：真偏度需乘方/条件聚合算子（ts_count_if、pow），涉及白名单 DSL 安全审计扩面（元数/溢出/类型），收益存疑（振幅族已部分覆盖），维持搁置并记录。
- selftest：regime_cond_lab 5→6组（placebo 确定性/保量、robust_chain 结构、渲染含 H网格/placebo）；pytest 746 全绿；研究侧零改动主链（隔离 grep 合规）。

## [0.75.0] — 2026-09-05 · 第75轮 G25/G29续 regime条件化实验台（低波振幅反转确认） + 对齐口径年化bug修正 + G23续 carry条件化对照
- **⚠️ 修正（审计诚实）**：`evaluate_ls_books_aligned`（第64轮）把 H>1 的**期收益当"日收益"年化**（annual=mean×252、sharpe=×√252）——对齐口径的年化/夏普被 **×h 虚高**（H=20 时"正交净年化+24.90%"实为 +1.24%/夏普0.22，等权+0.24%、反转+0.13%）。新增 `period_days` 参数按真实持有期折算（annualize=252/period_days），同 h 内相对排序不受影响；第64-74轮 CHANGELOG/摘要中引用的对齐口径绝对年化数字应按此更正读取（H=1 逐日口径不受影响）。selftest 增年化折算断言（hold=1×252 / hold=2,period_days=2×126 / 常数收益期折算夏普一致）。
- **新增 tools/regime_cond_lab.py（G25/G29续① regime条件化分层多空实验台，研究侧红线门控）**：对【面板列或白名单表达式因子】（G25引擎求值，expr_miner 同口径装配）按 PIT regime 标签（复用 factor_regime.compute_labels：vol=hv60过去120日ts_rank三分位）筛当日截面，做按 H 对齐非重叠分层多空（复用 orthogonal_blend_oos 分档/换手/成本原语），输出【全样本/仅低波/仅高波】三口径对照（净绩效+逐日截面IC）；诚实边界写死=因子定义有全样本选择偏差、只回答"条件化是否增强"相对命题、regime 阈值沿用 config.REGIME_* 不调参。
- **真实验证①（64品种/1013日/range_pct5×H20，修正后口径）**：全样本净年化 -4.10%/IC-0.082；**仅低波 -11.03%/夏普-1.60/IC-0.148**（|IC| 由 0.082 放大到 0.148，t-3.7）；仅高波 **+9.06%**/IC+0.049。方向注记：账本=多高振幅/空低振幅（因子原始方向），**反向（多低振幅/空高振幅）在低波状态镜像≈+11.0%年化、高波状态≈-9.1%失效**——低波振幅反转的 regime 条件化结构在交易口径下确认（换手 0.63/期单边、年成本≈12bp 可忽略），列为后续影子研究候选（仍不进综合分）。
- **G23续③ carry_eval 加 `--regime-compare`**：按同一 regime 标签把 carry 主窗截面切成全样本/仅低波/仅高波三个子截面多空对照（复用 compare_mask 管线，_load_regime_map+filter_points_by_regime+compare_regime+render_regime_compare）。**真实验证**：全样本 52期/净t-1.52 → 仅低波 33期/净t-0.14（近零）→ 仅高波 24期/净t-0.56——**条件化减轻主连口径负绩效但不转正、不增益**（负结果照实；与第74轮"carry 赚展期不赚价差"机制判读一致）。
- **G25续② 评估收口（不硬做）**：偏度类派生量需白名单新增条件聚合算子（无乘方/条件求和），涉及 DSL 安全审计扩面，先搁置；期限结构字段进 expr_miner 池需 term_history 字段入 G21 面板（schema 变更），留待与 G21续 合并评估。
- selftest：regime_cond_lab 5组（新增注册）、orthogonal_blend_oos 增年化修正组、carry_eval selftest 全过；pytest 744→746 全绿；研究侧零改动主链（隔离 grep 合规）。

## [0.74.0] — 2026-09-05 · 第74轮 G25续 候选池扩容+振幅截面异象 + G29续 表达式因子体检 + G23续 4000日真双样本 + G24 调研收口
- **G25续② expr_miner 候选池 29→43条**：新增振幅/影线/K线实体/净上行占比/Amihud非流动性/短长波动比/量仓变动冲击/持仓均线比/量仓相关/跳期动量(12-1、36-1)六族14条派生量（全白名单 DSL、量纲无关），series_from_rows 增 open 字段（缺失回退收盘）与缺键容错。**真实验证（64品种，约1分钟）**：时序上榜 7→9 条（新增 range_pos_60、mom_skip_12_1——跳期未消除反转）；**截面上榜新增 range_pct_5/20（日均振幅，H20 截面 -0.068/-0.066、t-9.7/-9.2）=全池最强截面信号，即"高振幅品种未来跑输"的低波动异象商品版表达**。
- **G29续① factor_regime 加 `--expr` 表达式因子注入**：'EXPR[:名称]' 经 G25 引擎逐品种求值（expr_miner.series_from_rows 同口径、缺键容错）后注入行 dict（键 x_<名>，仅本进程内存不落库），与原生因子共用 regime 分层/秩自相关换手/衰减形态全套体检。selftest 6→7组。
- **真实验证（4条注入体检）**：**range_pct 的截面负IC高度集中在低波状态**（低波桶 H20 -0.124/-0.135、牛低波/熊低波同量级，高波桶≈0），且持续性极强（lag1 自相关 0.79/0.89、换手 0.097/0.144=信号慢成本低）、衰减形态指数/幂律均不成立（IC 不随 H 单调衰减）——regime 结构清晰，列为后续研究候选（仍不进综合分）；vol_chg5 H5 +0.023 无 regime 结构、oi_surge5 H20 全桶负 -0.038，确认弱信号。
- **G23续③ carry 4000日真双样本探针（--days 4000，独立输出 reports/carry_eval_long.*，不覆盖既有 sidecar）**：发现旧 2500 日口径的"长样本"因逐合约缓存上限实际只有 n=19 期（长窗=短窗，双样本退化同窗自比）；4000日探针补下载更早年代旧合约后**首次拉开真双样本**：近月含roll口径 短窗 n=52/t=+1.39 vs **长窗 n=95/t=+2.55（净均+1.209%）**——carry 赚展期收益的机制第一次获跨样本验证；主连价格口径长窗 t=+0.06 不显著。近窗 t=+1.39 仍未达 1.5 硬门槛，主组合裁决维持 0/8、0/3 归档不变；掩码对照（原98385→68602点）原始52期 t-1.38 vs 掩码后 t+0.34（单调仅25%不下结论）。
- **G24④ 调研收口（真HP/SP 分类持仓）**：交易所只公布套保**额度审批/管理办法**（审批结果走会员电子化系统查询），无逐日品种×套保/投机分类持仓的免费结构化源（东财前20席多空代理已是免费数据上限）——真HP/SP 维持"无源"归档，需商业数据或 Tushare 积分路径再确认。
- pytest 744 全绿；expr_miner/factor_regime selftest 全过；研究侧零改动主链（隔离 grep 合规）。

## [0.73.0] — 2026-09-05 · 第73轮 G25续 动量反转反向利用样本外对照 + expr_miner 逐日截面IC层 + G28续 attribution 掩码对照
- **G25续① 反转动量账本（orthogonal_blend_oos `--rev-factor`，默认 ret63）**：rev=-截面秩(rev_factor) 与正交/等权并列出 OOS 截面 RankIC+分层多空净绩效；rev 不在候选列时仅作附加列装载不进 blend。
- **G25续② expr_miner 逐日截面 RankIC 体检层**：每候选新增逐交易日跨品种 Spearman（mean/ICIR/t值/正比例），与既有逐品种时序层并列；cross_rank 等截面单调变换不改变截面 Spearman，故截面体检直接覆盖全部候选（无需单列 cross_rank 版）。
- **G28续/G22续③ attribution `--mask-compare`**：按 tradable_mask 剔除锁板/临交割/无覆盖事件重跑归因对照（正文保持全量口径、对照表并列全量 vs 掩码后），回答"盈亏归因是否被不可交易日信号污染"。
- **真实验证（三个负结果/弱结果照实）**：①纯反转动量账本 OOS 截面 IC 微正不稳（H1 +0.0003/H5 +0.0132/H20 +0.0091），按H=20对齐非重叠净年化仅 +2.59%/夏普0.10，远弱于正交合成 +24.90%/0.99——**动量反转的肉主要在时序维而非截面维**（与 expr_miner 第73轮截面层互证：mom_60 H20 时序 meanIC -0.117 → 截面仅 -0.014/t-1.8）；②expr_miner 截面上榜 0 条（时序上榜 7 条不变），新增发现 vol_chg_5 H5 截面 +0.025/t+4.8、oi_surge_5 H20 -0.032/t-6.9 量级未达 0.05 门槛仅记录；③attribution 掩码对照覆盖率仅 28~31%（临交割剔除 235 条与掩码系列"砍样本"结论一致、锁板 0 例）、β 符号一致率 50~62.5%——样本太薄不下强结论。
- selftest：expr_miner 7→10组（截面IC手算/截面零方差/t统计量与渲染）、orthogonal_blend_oos 增 rev 对偶性（rev IC≡-因子IC、附加列路径、多空反向恒等）、attribution 8→11组（掩码拆分/对照摘要/报告渲染）；pytest 744 全绿；研究侧零改动主链（隔离 grep 合规）。

## [0.72.0] — 2026-09-05 · 第72轮 G25续 表达式因子自动挖掘 expr_miner（红线门控） + G22续 掩码系列归档 + G23续/G16/G1 阻塞核实
- **新增 tools/expr_miner.py（G25续 表达式因子自动挖掘，研究侧、红线门控）**：确定性穷举（非随机/非遗传/非LLM）白名单 DSL 候选池 29 条（动量/量能/持仓变化/均线比/风险调整动量/量价相关/时序z/区间位置，窗口 5/10/20/60），逐候选前向 H=1/5/20 Spearman RankIC（逐品种 meanIC + 全样本 pooledIC，与 expr_research 同口径、严格无未来）；**绝不写 LIBRARY/catalog、不被 main import、不自动改权重**，只落 reports/expr_miner.txt/.json + experiment_ledger 台账。
- **真实验证（64品种/6.1万点）**：|meanIC|≥0.05 上榜 7 条**全部为动量系且 IC 为负**（mom_60 H20 meanIC -0.117、mom_z_60 -0.106、5/10/20日动量 H5 -0.030~-0.067）——与项目"动量已被证伪"结论互证，呈现稳定**反转**特征；其余 22 条 |meanIC|<0.05 无稳定预测力，负结果照实。上榜与否由数据动态判定写入报告，不预设结论。
- **顺手修正**：expr_miner/orthogonal_blend_oos 的 ledger artifacts 传 dict 导致路径按键名拼接（键"txt"被当相对路径）→ 统一改列表口径。
- **G22续 掩码系列归档**：tradable_mask(64轮)→carry/xsmom 截面剔除(65/67轮)→对照表(66轮)→汇总工具(70轮)→portfolio_lab --mask(71轮) 全链齐整，本轮正式归档（总纲 G22 状态更新）。
- **G23续/G16/G1 阻塞核实（只核实不硬做）**：G23 主板 OI 深度容量仍硬阻塞 G14（monitor.db 无 tick_snapshots 表，一档盘口自采未启动）；G16 ml_samples 42937 样本/64品种/特征全填但跨度仅 127 个交易日（2026-03-10~09-01），维持"跨度短不上模型"；G1 纸面三表 paper_orders/trades/equity 全为 0 行、PAPER_ENABLED 默认 False——影子从未实际启动，三方对账无对象（此前摘要"未到期"表述不准，已更正），需显式开 PAPER_ENABLED 后连续运行≥4周。
- selftest 7组（候选池全编译/前向IC严格未来/单因子手算/报告结构/排序/上榜动态判定/max_abs_ic）；pytest 742→744 全绿（+selftest +compileall）；CHANGELOG 顺修 [0.63.0] 块错位（原被误置于文件最顶部）。

## [0.71.0] — 2026-09-05 · 第71轮 G22续 掩码接入 portfolio_lab
- **portfolio_lab 新增 `--mask` 选项**：读 research_panel.db 算可交易性掩码（锁板/交割），apply_mask_to_returns 剔不可交易日后重做组合实验。
- 真实验证：剔不可交易日点 32613；固定宇宙 61→54 品种、504→472 日；等权年化 -2.35%/波动9.22%/夏普-0.26（掩码后口径）。
- selftest 12→13组（apply_mask_to_returns 剔除不改入参）；pytest 742 全绿；研究侧单文件增量、隔离合规。
## [0.70.0] — 2026-09-05 · 第70轮 G22续 掩码前后对照汇总工具
- **新增 tools/mask_compare_summary.py**：聚合 carry_eval.json(mask_compare) 与 xsmom_eval.json(mask_compare_xs) 两个 sidecar，输出统一对照汇总（前后绩效、剔除统计、诚实结论）。
- 真实汇总：carry 19期vs9期（剔8862→4173）、xsmom 12期vs6期（剔5952→2807）；结论=锁板罕见、交割剔除砍半样本、掩码价值在防锁板而非改善信号。
- pytest 740→742（新工具 selftest + compileall）；研究侧单文件增量、隔离合规。
## [0.69.0] — 2026-09-05 · 第69轮 G25续 量仓因子接入 expr_research 前向IC体检小结
- **expr_research 报告新增"量仓类表达式因子前向 IC 小结"段落**：自动识别 vol/oi/amount 系 key，汇总 H1/H5 IC。
- 真实验证（64品种）：vol_chg5/oi_chg5 短期微正（H5 +0.023）、vol_oi_ratio/amount_proxy/oi_corr_price20 均 |IC|<0.05 无稳定预测力——诚实记录不进综合分。
- 核实第68轮量仓因子本就被 run() 全 LIBRARY 遍历自动体检，本段仅使结论显式可见。
- pytest 740 全绿；研究侧单文件增量、隔离合规。

## [0.68.0] — 2026-09-05 · 第68轮 G25续 量仓类表达式因子（vol/oi 衍生量）
- **LIBRARY +5 量仓因子（26→31条）**：expr_vol_chg5（成交量5日变化率）/expr_oi_chg5（持仓量5日变化率）/expr_vol_oi_ratio（量仓比换手代理）/expr_amount_proxy（成交额代理=收盘×量）/expr_oi_corr_price20（价仓相关性20日）；全部白名单 DSL、无未来、可编译。
- **factors_catalog 47→52条**（全 research 不进综合分）；LIBRARY 编译循环/parity 注入 oi 字段（factor_expr/factor_legacy_expr/test_factor_expr/expr_research 四处）。
- 真实 RB 面板验证全可算（vol_chg +25%、oi_chg +1.2%、量仓比0.65、成交额28.7亿、价仓相关-0.88）。
- pytest 740 全绿；研究侧零改动主链（隔离 grep 合规）。

## [0.67.0] — 2026-09-05 · 第67轮 G22续 掩码前后对照模式接到 xsmom_eval
- **xsmom_eval 新增 `--mask-compare`**：掩码前后截面多空绩效对照表（与 carry_eval 第66轮同思路，主因子 z{main_l}）。
- 新增纯函数 `_xs_ls_summary` / `compare_mask_xs` / `render_mask_compare_xs`（可复用、只读、零网络）。
- 真实验证（24品种，L=252 H=20）：原始 12期/净t1.36/净均收2.35%/单调75% vs 掩码后 6期/净t0.95/净均收3.30%/单调50%；剔除 5952→2807 点。诚实结论：掩码后样本减半、净t略降、净均收反升但单调性大降，印证"临近交割剔除主要降样本量而非改善信号"。
- selftest 14→15组（fake_mask 剔除统计 + 渲染结构）；pytest 740 全绿；研究侧单文件增量、隔离合规。

## [0.66.0] — 2026-09-05 · 第66轮 G22续 掩码前后截面多空绩效对照表
- **carry_eval 新增 `--mask-compare`**：同一输入分别跑无掩码/有掩码截面多空，输出并列绩效对照表（口径/期数/净t/净均收/胜率/单调/Q5-Q1）。
- 新增纯函数 `_load_panel_mask` / `_ls_summary` / `compare_mask` / `render_mask_compare`（可复用、只读、零网络）。
- 真实验证（24品种）：原始 19期/净t-0.13/单调25% vs 掩码后 9期/净t-1.04/单调50%；剔除 8862→4173 点。诚实结论：临近交割剔除砍半样本、主窗下掩码后仅9期绩效波动大，掩码价值在日线级（锁板罕见）。
- selftest 5→6组（fake_mask 剔除统计非零 + 渲染结构）；pytest 740 全绿；研究侧单文件增量、隔离合规。

## [0.65.0] — 2026-09-05 · 第65轮 G25续 TSMOM 表达式化 + G22续 掩码接入截面剔除 + G23续 真实逐合约成交额容量
- **G25续 TSMOM 家族 z 表达式化**：tsmom_z_expr(z=ret/(窗口日收益样本std×sqrt252))，单窗口 z63/126/252 对 futures_data.tsmom_at **逐位相等**（357/294/168 对 float.hex 全一致）；blend 因 CPython sum() pairwise vs DSL 左结合的求和序差异 1~2 ULP（已钉死 BLEND_REL_TOL=1e-14，暖机期动态分母单独计数）；LIBRARY 23→26、catalog 44→47；selftest 12→13组。
- **G22续 tradable_mask 接入截面剔除**：新增 filter_points（按 sym+date 剔除锁板/临近交割）+ _name_to_sym（中文名↔代码映射）；carry_eval/xsmom_eval 均加 `--mask`（读面板→剔不可交易→主窗长窗同步重建→重做截面）；真实24品种 carry 8862→4173 点、xsmom 5952→2807 点；验证锁板影响可忽略。
- **G23续 carry 容量接真实逐合约成交额**：term_history 增 vol_sum/near_vol 字段；carry points 增 near_vol/vol_sum/near_amount（近月结算×近月成交量）；报告七·补新增"真实逐合约"容量行（24品种 4835 万元 vs 主连代理 12567 万元）；精确容量仍待 G14。
- pytest 740 全绿；研究侧零改动主链（隔离 grep 合规）。

## [0.64.0] — 2026-09-04 · 第64轮 G25续 按H对齐非重叠再平衡+ATR14表达式化 + G22续 可交易性掩码 + G23续 carry 换手/容量
- **G25续(a) 分层组合按H对齐非重叠再平衡**：orthogonal_blend_oos 新增 evaluate_ls_books_aligned——每 H 个交易日调仓一次、期不重叠、净收益可真实复利；H20 正交净年化+24.90%/净夏普0.99（消除重叠虚高）。
- **G25续(b) ATR14/TR 表达式化**：ATR14_EXPR（嵌套 max 二元、吃 high/low/前收）对 compute_indicators 过程式 float.hex 逐位 parity；LIBRARY+expr_atr14、catalog 43→44。
- **G22续 可交易性掩码**：新增 tools/tradable_mask.py——locked_flags（疑似锁板）+ nearest_delivery_days（交割日历）+ mask_for_panel/summarize；真实64品种锁板仅3例、距交割≤15天占46.8%；真HP/SP仍待G22分类持仓。
- **G23续 carry 换手/容量**：carry points 增 v/oi/vol_turn/amount；报告新增"七·补 换手与容量"（多空腿成交额、1%参与率容量估算，精确待G14）。
- **G5④ 核实**：circuit_breaker 三动作模式已在第51轮清账，本轮不重复开发。
- pytest 738→742 全绿；研究侧零改动主链（隔离 grep 合规）。

## [0.63.0] — 2026-09-04 · 第63轮 G1 纸面引擎补 OMS/成交回报/持仓对账 + G25续 KDJ/EMA 表达式化 + OOS 分层多空/换手/成本
- **G1 paper_broker 补 OMS/成交回报/持仓对账**：纯函数 reconcile_position_sets（五类 break）/aggregate_fills；内存级 _orders_by_id/fill_ledger；新方法 orders_view（全状态台账）/cancel_order（主动撤单）/fills_view/fill_report/reconcile_positions/reconcile_against_db；restore 跨进程回填。
- **G25续 KDJ/EMA 表达式化**：DSL 加 kdj_rsv/kdj_sm 算子；EMA12/26、KDJ K/D/J 对过程式 float.hex 逐位 parity；LIBRARY+5、catalog 38→43。
- **OOS 分层多空/换手/成本**：orthogonal_blend_oos 加 quantile_ls_day/turnover_between/evaluate_ls_books；真实面板负结果照实。
- pytest 733→738 全绿；远程 origin 配置 + 隐私 xlsx 移除 + 全量 push 完成（161 commit + 34 tag）。

## [0.62.0] — 2026-09-04 · 第62轮 G4 续：滚动 walk-forward 样本外 + 对照基准（回测严谨性补齐）
- **缺口审计**：第26轮 G4 已落地 next-bar 双档成交、交易级 bootstrap 置信区间（含多空）、单次 IS/OOS 静态切分、真实费率+滑点+冲击成本分列、3×3 参数网格、backtest_runs 留档、DSR/PBO 引用。本轮对照用户五要素（walk-forward/费+滑点+冲击/样本外/多空分桶/对照基准），确认**真正缺口只有"滚动 walk-forward"与"对照基准"**，其余已具备，不重复造。
- **新增 `backtest_rigor.py`（纯标准库、零网络、模拟器可注入便于单测）**：slice_prepared 因果切窗（series 全局 i 重映射为窗内局部，指标沿用全局因果结果、无未来函数）；buy_hold_window/benchmark_for_prepared/pooled_buy_hold/excess/beat_benchmark_pairs 买入持有基准与超额；wf_folds 折划分（首折 OOS 起点=预热+训练窗、折间 OOS 不重叠、尾折不足2根丢弃）；select_best_param 只在 IS 段 3×3 网格按净均收选参（最小交易数门槛、并列取先者保确定性）；walk_forward_symbol 逐折"前段 IS 选参→后段 OOS 交易"拼接纯样本外轨迹并标注 wf_fold/wf_hold/wf_entry；param_usage/is_vs_oos_avg 选参分布与 IS→OOS 衰减。
- **接入 backtest.py（默认策略数值逐值不变）**：fetch_and_run 每品种算 buy_hold 基准；报告"一、总体"新增【对照基准】块（等权篮子买入持有累计 vs 策略净累计、超额百分点、逐品种跑赢数，`--no-benchmark` 关）；新增 `--walk-forward`（默认关）+ `--wf-train/--wf-test`，输出 OOS 拼接总体+多空分桶、折数/兜底折数、折级 IS→OOS 均收衰减与选参分布。config 增 BACKTEST_BENCHMARK/BACKTEST_WF_* 常量。
- **验证**：新增 tests/test_wf_benchmark.py 14 例（切窗重映射/无未来、基准首有效价、等权/超额/跑赢、折划分连续不重叠、选参门槛与并列、OOS 与 IS 严格不交且交易带折标、IS 不足回退默认、选参分布/衰减）；注意第26轮既有 G4 用例文件名也叫 test_backtest_rigor.py（17例，测 backtest 模块），本轮新测改名为 test_wf_benchmark.py 避免覆盖。全量 **pytest 718→733 全绿**（+14 与 compileall 新模块1例）。真实回测：默认 8 品种策略净累计 -25.2% vs 等权买入持有 +8.5%、超额 -33.7pp、仅 1/8 跑赢（诚实暴露技术规则跑不赢持有）；--walk-forward 8品种16折0兜底、折级 IS +0.44%→OOS +0.01% 衰减可见；--oos-ratio/--impact-rate/多空分桶与新块共存正常。生产 76→77 个 py、35325→35603 行，零新增运行依赖。

## [0.52.0] — 2026-09-03 · 第52轮 研究/看板侧三项增强（①G26续二 全64品种×多gross(1.0/1.2/1.5)网格影子+换手成本；②组合净值/熔断校准接看板新页签；③wf_cost_lab 多周期+AFML purged/embargo 隔离带；全部纯标准库、只读、默认等价旧版、不接main/不改综合分）
- **任务与边界**：第51轮 G5④ delever 自动减仓已完整交付（tag v0.51.0）。本轮三项均为**研究工具与看板展示侧**增强，不碰 main/analyzer/综合分/portfolio 默认 CSV/storage 表结构：①把第49轮已齐的四种 sizing（equal/inv_vol/erc/gmv）延伸到**总敞口 gross 杠杆网格×换手成本**影子，并补全"全64品种"覆盖率口径对照；②把第49轮组合净值、第50轮熔断校准两份离线研究产物**接进图表看板**新页签；③给 walk-forward 加 AFML ch7 的 **purge/embargo 防前视隔离带**并让 wf_cost_lab 支持多窗口周期对照。charts 虽被 main 每轮 write_chart_data 调用，但两块新 payload 独立 try、缺文件返 None，绝不拖垮既有图表或主链。
- **① gross 网格×换手成本（tools/portfolio_lab.py）**：rolling_proxy 每方法新增 `seg_bounds`（每个再平衡段的 start/length/entry_turnover，首段无前置权重 entry_turnover=None）；新增纯函数 `gross_net_daily`（日收益先整体×gross 线性放大，再在每个再平衡段首日扣 `gross×段首日单边换手×one_way_cost`，gross=1/cost=0 逐点恒等）与 `gross_cost_grid`（对 gross(1/1.2/1.5) 出年化净/毛收益、净/毛夏普、净波动、净回撤、年成本拖累，净=扣费、毛=零成本对照，保证净夏普≤毛夏普、波动随 gross 单调放大）；DEFAULT_GROSS_GRID=(1,1.2,1.5)、DEFAULT_ONEWAY_COST=1.5e-4（1.5bp，对齐 wf_cost_lab fee5e-5+slip1e-4 数量级）。dense_matrix 加 `fill_missing`：False=旧稠密行为（默认等价，coverage≥0.95 得61/64），True=全64口径（缺失日收益补0=当日无敞口）。run() 主固定宇宙跑三档 gross 网格、全品种 fill_missing 只跑 gross=1 对照，报告新增【三】文本块、落 **reports/portfolio_gross_grid.csv（长表12列）**、json 增 gross_grid/gross_one_way_cost/gross_grid_csv/all_universe。selftest 8→11组。
- **①真实数据诚实结论（固定宇宙61品种、样本外378日）**：gross 1.0→1.5 等比放大收益与波动、毛夏普基本不变（等权年化3.97%→5.66%、波动9.48%→14.22%、夏普0.42→0.40、回撤9.54%→14.06%；逆波动夏普0.55→0.53 仍最优；gmv 夏普0.35→0.33 仍垫底）；**单边1.5bp、每20日再平衡下换手成本拖累极小**（等权/逆波动≈0.00%、ERC 0.01%、gmv 最高也仅0.03~0.04%/年，因 gmv 调仓最频繁）=杠杆放大的是风险而非风险调整收益。**全64口径（3个稀疏品种缺失补0）夏普整体下移**：等权0.42→0.35、逆波动0.55→0.48、ERC0.53→0.38、gmv0.34→**−0.18**，揭示固定宇宙稠密筛选存在轻微覆盖偏差、稀疏品种补0稀释 gmv，故主结论仍以稠密61宇宙为准、全64仅稳健性对照。诚实标注：gross 为**线性多头杠杆近似，未计保证金/强平/融资成本**。
- **③ walk_forward purge/embargo + wf_cost_lab 多周期**：`tools/backtest_validation.py` 的 walk_forward 加 `purge=0,embargo=0`（默认0时 IS/OOS 切窗与旧版逐段一致）：IS 有效行 [start,start+train−purge)（purge 掉标签持有期向前伸进 OOS 的样本）、OOS 整体后移 [train+embargo, train+embargo+test)，滚动终止条件相应加 embargo，返回 dict 与每段回传 purge/embargo，train−purge<2 安全返空。`tools/wf_cost_lab.py` run_symbol 增 purge/embargo/wf_presets，对多组 (train,test) 各跑一次 walk_forward+wf_stability 收集 `wf_multi`，**默认组(20/10/0/0)结果仍填 stability/wf_summary 向后兼容**；CLI `--wf-train/--wf-test` 接受逗号多值配对、新增 `--purge/--embargo`，报告头与每品种增"多周期/防前视对照"小段，台账参数同步。selftest 8→9组、backtest_validation selftest 增隔离带断言。
- **③真实数据诚实结论（RB/MA/I/TA、30m、purge=2 embargo=1、20训10测 vs 40训20测）**：加隔离带后参数不稳被进一步暴露——RB/I 两窗口均"漂移"（短窗6段 OOS 夏普 −0.116/−0.172、锚定仅33%、切换4次），MA 长窗略改善（OOS +0.141、锚定50%、切换1），TA 短窗+0.072 转长窗 −0.029；**没有一个品种在两种窗口+隔离带下都稳定占优**，与第45轮"30m 参数高原本就脆弱"结论一致，属诚实负结果：这些 CTA 参数的样本外优势经不起防前视收紧，不宜据此上实盘杠杆。
- **② 看板接组合净值/熔断校准（charts.py）**：新增 `portfolio_nav_payload`（读 reports/portfolio_nav.csv，四方法净值多线抽稀+期末净值/最深回撤摘要，缺/坏表返 None）与 `circuit_review_payload`（读 reports/circuit_review.json，抽阈值网格六档四方法触发数、1%校准档 T+1/3/5/10 条件远期 vs 全样本基准、三档计数）；build_payload 各以独立 try 挂两键；_PANEL_DOM 增三张 full/半宽卡（c-pnav 四方法净值曲线、c-creview-sweep 阈值触发柱、c-creview-fwd 条件远期柱），CHART_IDS 15→18，新增 renderPnav/renderCreview（ES5 function、无箭头函数、暗色配色复用 BLUE/GOLD/UP）并接入 loadAndRender 与 onerror 空态。真实产物验证：nav 378点四方法（期末1.0624/1.0723/1.0597/1.0268）、review 等权阈值触发 72/32/18/2/1/0、条件远期 T+1 −0.16% vs 基准+0.01%，charts --rebuild 后 chart_data.js 含两键、图表看板.html 含三新卡与渲染函数。
- **测试与零改动证据**：test_portfolio_nav 增3例（gross_net_daily 段首日收费/首段None不收费/手算、gross 网格波动单调且净≤毛、fill_missing 保全部品种）、test_charts 增2例（两 payload 合成解析+缺文件/坏表 None），selftest 扩到 portfolio_lab 11组/wf_cost_lab 9组/backtest_validation 增隔离带组。全量 **pytest 618→623 全绿**（0失败/错误/跳过，cache/r52_junit.xml）、compileall（含main）过。默认8品种 equity=c4da4cdf61f3bcdc/trades=50dcc800d326f8e9 双 sha256 **逐字节一致**；grep 证明 main/analyzer/portfolio 对 portfolio_lab/wf_cost_lab/backtest_validation 零 import（仅 tools 互引与 tests）；运行依赖仍仅 requests/uiautomation/websocket-client。
- **规模**：生产 68 py/30418→30797 行（根47/20438→20606、tools21/9980→10191）；tests 仍39 文件/6210→6301 行；用例 618→623；看板 15→18 图。代码 commit 589fd2e。

## [0.51.0] — 2026-09-03 · 第51轮 G5④ delever 由"只给建议文字"升级为显式开关下的纸面自动减仓执行（新增 paper_delever 模式 + 账户内核部分平仓，默认 observe 完全不动、不传逐字节等价，G5④ 含增强全部清账）
- **任务与边界**：第48轮熔断在 delever 档只输出"建议减仓约50%"文字、filter_orders 只剔开新仓腿，没有真正减仓；第50轮已把阈值在真实长序列校准。本轮补齐 G5④ 最后一个增强欠账：新增第三种动作模式 `paper_delever`（与 observe/paper_halt 并列），在断路器进入 delever 档时对当前持仓**按比例纸面自动减仓**。默认 `CIRCUIT_ACTION='observe'` 时 `self.breaker=None`、on_cycle 完全不进减仓分支；PAPER_ENABLED 默认 False、真实账户永不自动操作。main/analyzer/综合分零改动。
- **A. 账户内核支持部分平仓（默认全平逐字节等价）**：`Portfolio.close` 新增 `reduce_lots=None`。None/≥持仓=整仓全平走原路径（**全平开仓费直接取 pos.open_fee_yuan，不做 ×n/n，避免浮点末位误差**——这是本轮双哈希一度失配后定位并修复的关键点）；正且<持仓=部分减仓，按手数比例分摊开仓费、剩余持仓 lots 扣减并保留不 pop、绝不反向；`reduce_lots≤0` 返 None。平仓记录 rec 增 `partial/remaining`（lots=本次平仓手数），closed 两种都记。同价零成本下"分批平净盈亏之和==一次全平"有测试保证。
- **B. 断路器（纯决策，不直接下单）**：新增 `PAPER_DELEVER` 与 `_HALT_MODES=(paper_halt,paper_delever)`；纯函数 `reduce_lots_of`（floor 向下取整、不足1手返0=绝不把减半变清仓、非法安全返0）与 `delever_plan`（只收 1≤减仓手数<持仓、跳过当日已减 done、按 sym 排序、不改入参）；方法 `delever_targets`（仅 paper_delever 且 level==delever 出计划，否则 []）/`mark_delevered`（成交后登记，`_delever_done` 当日各品种只减一次、`_reset_day` 日切清空=新一天可再评估）；open_allowed 改用 `_HALT_MODES`；decision 增 `auto_delever` 标志、delever 文案按三模式区分。selftest 11→15 组。
- **C. 纸面经纪执行（严格无未来）**：构造挂载条件扩到 paper_delever；`_fill_leg` 平仓分支透传 reduce_lots、**仅整仓平完（remaining≤0）才清 pos_ref**（部分减仓保留持仓与 pos_ref）、trade.lots 天然=减仓手数、reason="熔断自动减仓"；新增 `_delever_cut` 在 on_cycle **阶段A挂单成交后、阶段B信号前**执行——断路器决策来自上一轮阶段D权益快照（本轮价成交=晚一轮、无未来），逐项取价（行价→最新价→开仓价）、锁板/无价顺延且**不 mark 下轮重试**、当轮立即成交（风控减仓不再等 next）、只平不反向；summary 增 `n_delever`。
- **纪律与回退**：只减不清（floor 保证不足1手不动）、只平不反向、当日一次、日切重置；三模式 observe（恒不动）/paper_halt（停开但不自动砍）/paper_delever（停开+自动减）均有测试。config 仅更新 CIRCUIT_* 注释、**值全部不变**。
- **测试与零改动证据**：test_circuit_breaker 增4例（reduce_lots_of 边界/delever_plan 规则与不改入参/paper_delever 停开+计划+当日一次+日切/observe·paper_halt 不出计划，参数化）、test_paper_broker 增3例（内核部分平仓剩余保留+分批净盈亏==一次全平+0手不减/超持仓全平、paper_delever 晚一轮减一半且当日只减一次、observe 与 paper_halt 零减仓）。全量 **pytest 610→618 全绿**（0失败/错误/跳过，cache/r51_junit.xml）、compileall（含 main）过、circuit_breaker selftest 15组。默认8品种 equity=c4da4cdf61f3bcdc/trades=50dcc800d326f8e9 双 sha256 **逐字节一致**（修复全平开仓费浮点末位后复算一致）；运行依赖仍仅 requests/uiautomation/websocket-client。
- **规模**：生产 68 py/30237→30418 行（portfolio/circuit_breaker/paper_broker/config 共 +181 净行）；tests 仍39 文件/6092→6210 行；用例 610→618。代码 commit 7a20548。**至此 G5④ 从状态机（48轮）→阈值历史校准（50轮）→显式自动减仓执行（本轮）完整闭环，G5 五项①~⑤及④的两项增强欠账全部清账。**

## [0.50.0] — 2026-09-03 · 第50轮 G5④阈值校准 组合层熔断阈值历史校准台（新增 tools/circuit_review.py，日频逐日代理只读 G21 面板复现四方法日收益，不接 main/不改 circuit_breaker 默认值/综合分/持仓）
- **任务与边界**：第48轮 circuit_breaker 落地时留了明确欠账"阈值用合成断言、未在真实长序列回放校准"；第49轮产出四方法逐日组合净值（portfolio_nav/rolling_proxy），本轮把两者打通，做**组合层熔断阈值的历史校准研究**。新增研究侧工具 `tools/circuit_review.py`（约320行纯标准库、零网络、只读 cache/research_panel.db，经 portfolio_lab 复现滚动样本外日收益，出 reports/circuit_review.txt|.json，末尾经统一实验台账旁路登记 kind=circuit_review）。main/analyzer/circuit_breaker/paper_broker **零改动**，默认8品种 CSV 双哈希不变。
- **三组纯函数（合成可断言）**：①`loss_of/forward_compound`=日收益转单日损失（损失为正）与触发后 1..h 日累计复利收益（不含触发当日、后续不足返 None）；②`threshold_events/level_counts`=逐日损失穿越阈值事件、按 circuit_breaker 同口径分 warn/halt/delever 三档（日频每日独立=每日日切重置）；③`conditional_forwards/sweep_halt`=触发点集合在 T+1/3/5/10 的条件远期收益分布（n/均值/中位/下跌占比）**对照全样本无条件基准**，回答"大跌后续跌（停开避险有价值）还是反弹（停开误杀）"，并对 halt 阈值网格(0.5%~3%)统计触发频次/占比/条件收益。analyze_method 端到端、render 三段文本（默认三档穿越计数/校准观察档条件远期/阈值网格）。
- **真实数据诚实结论（固定宇宙61/64品种、稠密2024-08-06~2026-09-02共504日、样本外曲线378日、满仓多头无成本）**：**默认 warn2%/halt3%/delever5% 在日频分散组合上几乎永不触发**——四方法378日里 halt/delever 全0次、warn 仅等权/逆波动各1次，最差单日仅 等权-2.15%/逆波动-2.16%/ERC-1.91%/gmv-1.70%（3%约为组合日σ的5倍，对61品种分散组合是极端尾部）；说明这套阈值真正保护的是**日内/集中持仓/加杠杆账户**，日频全分散组合层面天然难触发。下移到1%校准观察档才有样本（等权18/逆波动10/ERC7/gmv1）：等权 T+1 条件均值-0.16% vs 基准+0.01%（差-0.18%、下跌占比61% vs 49%）=**仅次日有微弱续跌惯性**，T+3 转正+0.22%、T+10 +0.07%=3日后转均值回归；阈值网格触发数随阈值单调降（0.5%72次→1%18→1.5%2→3%0），各档 T+5 条件均值在零附近、无"越跌越续跌"强信号。**结论：熔断在分散组合层面以尾部保险/纪律为主、不产生统计上显著的避险 alpha；默认阈值维持不动（为日内/杠杆场景保留），若要日频组合层保护需把 halt 下移到约1~1.5%，但样本太少需更长历史，本轮只校准不改默认。**
- **口径诚实边界**：circuit_breaker 实盘是日内状态机（日初权益→当前、当日粘性、一天多快照），本台只有日频收盘净值，"单日浮亏"=收盘对前收、每日一根=每天重置，**无法复现当日锁定，真实日内触发只会更多**，属保守逐日代理；固定宇宙有幸存者偏差、未计手续费/滑点/保证金/换月；不构成投资建议。
- **测试与零改动证据**：新增 `tests/test_circuit_review.py`（9例零网络/零DB：损失口径与远期复利手算、事件穿越含等号、三档分档、续跌vs反弹条件分布、_dist统计、阈值网格单调与占比有界、analyze_method结构与空序列安全、render三段、selftest），模块自带8组 selftest。全量 **pytest 600→610 全绿**（0失败/错误/跳过，cache/r50_junit.xml）；compileall（含main）过。默认8品种 equity=c4da4cdf61f3bcdc/trades=50dcc800d326f8e9 双 sha256 逐字节一致；grep 证明 main/analyzer/paper_broker 零 import circuit_review；运行依赖仍仅 requests/uiautomation/websocket-client。
- **规模**：生产 py 67→68（tools 20→21 新增 circuit_review 约323行）/29914→30237 行；tests 38→39 文件/5982→6092 行；用例 600→610。统一实验台账由6类增至7类（新增 circuit_review）。代码 commit e728215。G5④ 的阈值校准欠账补齐；G5④ 唯一剩余增强项=delever 自动减仓执行（当前停开+建议文字）。

## [0.49.0] — 2026-09-03 · 第49轮 G5⑤ 第四种风险型 sizing=gmv 最小方差接入回测内核 + 多品种组合历史净值曲线（portfolio 默认等名义零改动，研究侧 portfolio_lab 增逐日净值，600用例全绿）
- **任务与边界**：G5 总纲⑤要求"第四种 sizing=相关性/风险平价约束 + 多品种组合历史净值回测（替代 backtest 自陈的交易序列复利近似）"。第40轮 portfolio_constructor 已有 equal/inv_vol/erc/gmv 四方法、第41轮只把 inv_vol/erc 接入 portfolio 回测内核，本轮**补入第四种 gmv**，并把 portfolio_lab 滚动代理的日收益落成**逐日组合历史净值曲线**。默认 `--risk-sizing` 留空=旧等名义，不传完全等价（双哈希为证）；main/analyzer/综合分零改动。
- **① gmv 接入（默认关闭）**：`RISK_SIZING_METHODS` 增 gmv、`--risk-sizing` choices 增 gmv、`--compare-risk` 由三法扩为**四法影子对照**（等名义基线/逆波动/ERC/最小方差，同宇宙同撮合同成本仅目标名义不同）、报告方法名与台账 method_key 同步；gmv 复用既有 trailing_risk_weights（严格 PIT、只用 t 之前收盘价估协方差）与 decide_lots 风险权重覆盖通道，宇宙外/历史不足安全回退等名义。
- **② 多品种组合历史净值曲线（研究侧只读）**：portfolio_lab 增纯函数 `nav_curve`（日收益→逐日复利净值，不改入参/空序列安全）与 `drawdown_window`（净值→最大回撤及峰值/谷底日期，手算可验）；rolling_proxy 同步记录每个日收益对应的全局行号 idx（与 dates 对齐、四方法共用同一再平衡日历）；run() 落 **reports/portfolio_nav.csv（date + 四方法日收益/净值，379日）**、json 增 nav_summary（期末/最高/最低净值、最深回撤区间）、报告【一】增"期末净值/净值最深回撤"两行。
- **真实数据诚实结论（固定宇宙61/64品种、2024-08-06~2026-09-02共504日、窗126/再平衡20/收缩0.10/单票上限20%/满仓多头无成本）**：期末净值（初始1.0）等权1.0624/逆波动1.0723/ERC1.0597/**gmv1.0268**；年化波动 等权9.48%→逆波动8.35%(−11.9%)→ERC7.11%(−25.0%)→**gmv4.93%(−48.0%)**；最大回撤 9.54%/7.79%/6.46%/**5.26%** 单调下降；夏普 等权0.42/逆波动0.55/ERC0.53/**gmv0.35（降波动最猛但收益牺牲更多、夏普反降）**；平均有效N 61→54.8→39.4→**11.4（gmv高度集中）**、年化换手0→0.27→0.64→**1.77（gmv调仓最频繁）**。8品种日线60日四法影子对照 gmv 正常求解（有效N6.2、平仓105笔略少于基线109=集中致部分目标不足1手）。结论与第40轮一致：**gmv 只宜作对照、不默认启用**。
- **测试与零改动证据**：新增 `tests/test_portfolio_nav.py`（6例零网络：nav复利/不改入参/回撤窗口手算与安全/rolling的idx日期对齐四方法等长/玩具面板gmv波动≤等权且净值有限）；test_portfolio 补 gmv 进 trailing 权重属性参数化 + 白名单/默认关闭用例；portfolio_lab selftest 5→8组（净值复利、idx对齐、回撤手算）。全量 **pytest 593→600 全绿（0失败/错误/跳过，cache/r49_junit.xml）**、compileall（含 main）过。默认8品种 equity=c4da4cdf61f3bcdc/trades=50dcc800d326f8e9 双 sha256 逐字节一致；实验台账仍6类各1条；运行依赖仍仅 requests/uiautomation/websocket-client。诚实边界：净值为日频已复权面板、固定宇宙有幸存者偏差、未计手续费/滑点/保证金/换月。**至此 G5 五项①②③④⑤ 全部落地。**

## [0.48.0] — 2026-09-03 · 第48轮 G5④ 组合层单日浮亏熔断：risk_gate 由"只标注"升级为"可配置动作"（新增根模块 circuit_breaker.py 纯决策状态机 + paper_broker 默认旁路挂钩，默认 observe 只标注、显式 paper_halt 才停开新仓，主链/综合分/默认纸面成交零改动）
- **任务与边界**：G5 总纲④要求 risk_gate 单日浮亏熔断由"只标注"升级为"可配置动作（纸面层停开/减仓、建议层文字，默认仍只标注）"。现有 `risk_gate.py` 是**单品种/信号级** veto（无有效价、量不足、信号与涨跌背离、HV极端），本轮补的是与之**正交的组合账户级**维度。新增**根模块 `circuit_breaker.py`（约250行纯标准库、零网络、纯决策不直接下单/不改持仓）**；`paper_broker.py` 加默认旁路挂钩（默认 `CIRCUIT_ACTION='observe'` 时 `self.breaker=None`、阶段B不过滤任何委托，成交逐字节等价旧版）；main/analyzer/综合分**零改动**，⑤风险平价sizing与组合历史净值回测仍留后续。
- **三档+第二触发源（单日浮亏，损失为正）**：warn≥2% 只提示降杠杆；halt≥3% 停开新仓（只允许平仓/减仓）；delever≥5% 在停开基础上给"建议减仓约50%"文字（**仅建议、不自动砍仓**，自动减仓留后续）；保证金风险度 risk_degree≥95% 作为第二触发源抬到 halt。阈值全部走 config（CIRCUIT_ENABLED/CIRCUIT_ACTION/CIRCUIT_WARN_LOSS/HALT_LOSS/DELEVER_LOSS/RISK_HALT/DELEVER_RATIO），from_config 缺项回退默认。
- **两条关键纪律**：①**当日粘性、日切重置**——级别按"当日最深浮亏 peak_loss"定档，触发后当日不回落解锁（防阈值附近反复抖动），跨交易日（ts 日期前缀变化）自动重置、重计日初基准；②**动作模式默认 observe**——observe 下 allow_open 恒 True（即便 delever 也不拦），只有显式 paper_halt 才在 halt/delever 真正拦截纸面层开新仓，真实账户永不自动操作。断路器在 on_cycle 阶段D快照后用最新权益更新、供下一轮阶段B使用，严格无未来函数；filter_orders 停开时剔 open/reverse_open 腿、保留 close/reverse_close（反手只平不反向开）。
- **测试与零改动证据**：新增 `tests/test_circuit_breaker.py`（19例零网络：日期解析/浮亏口径/三档边界/委托过滤/非法参数/observe恒可开/当日粘性与日切重置/warn仍可开/风险度第二触发/from_config，及与 PaperBroker 的 close 档集成——paper_halt 下新仓被拦、反手只留平仓腿，默认 observe 无断路器照常开新仓）+ test_tools_selftest 注册 circuit_breaker 11组 selftest；全量 **pytest 572→593 全绿（0失败/错误/跳过，cache/r48_junit.xml）**、compileall 过。默认8品种 equity=c4da4cdf61f3bcdc/trades=50dcc800d326f8e9 双 sha256 逐字节一致；main --once 正常退出；运行依赖仍仅 requests/uiautomation/websocket-client。诚实边界：PAPER_ENABLED 默认 False、paper_equity 当前无历史曲线，本轮熔断阈值用合成断言验证状态机，未在真实纸面长序列上回放统计触发频次（待 paper 影子积累后补历史回放校准阈值）。

## [0.47.0] — 2026-09-03 · 第47轮 G5 组合层风险（研究侧①②③）相关矩阵+组合VaR(历史/参数)+原油压力传导（新增根模块 portfolio_risk.py 纯函数 + tools/portfolio_risk_lab.py 只读面板，主链/risk_gate/综合分/sizing 零改动）
- **任务与边界**：G5 总纲含①相关矩阵②组合VaR③原油压力④risk_gate熔断可配置动作⑤第四种风险平价sizing+组合历史净值回测。本轮**只做研究侧①②③的只读度量**，④熔断动作/⑤sizing与净值回测留后续（任何接入仍须"默认等价旧版、不传不变"）。新增**根模块 `portfolio_risk.py`（347行纯标准库、零网络纯函数，协方差复用 portfolio_constructor）**与**研究工具 `tools/portfolio_risk_lab.py`（241行，只读 G21 面板 cache/research_panel.db，出 reports/portfolio_risk_lab.txt|.json，末尾经统一实验台账旁路登记一条）**；main/analyzer/portfolio/risk_gate 经 grep 证明**零 import 新模块**。
- **① 相关结构**：correlation_matrix（协方差→Pearson，零方差安全记0）、平均绝对/带符号相关（系统性联动强度）、板块×板块平均相关 sector_corr_block、最强/最弱相关对 top_pairs、线性插值分位数 percentile。
- **② 组合 VaR/ES（双口径对照）**：历史模拟法 historical_var（组合日收益经验分位，VaR 损失为正、ES=超 VaR 尾部条件均值、最差日，不假设正态、含真实肥尾）；参数法 parametric_var（σ_p=sqrt(w'Σw)、VaR=z·σ_p，95/99 分位，多日按 √h 缩放给10日）；分散化收益=1−组合参数VaR/加权单体VaR。对 equal/inv_vol/erc/gmv 四套权重同篮子对照，回答"风险型权重能否真正降尾部风险"。
- **③ 原油压力**：以 SC 为驱动，oil_betas 从协方差算各品种 OLS 斜率 β=C(i,oil)/C(oil,oil) 与 R²，stress_oil 线性一阶传导组合损益=Σw_iβ_i·shock，给原油 −5%/−10%/+5% 三情景总损益与主要贡献品种（明确标注忽略非线性/危机时相关突变，属数量级情景）。
- **真实数据诚实结论（固定宇宙61/64品种、风险窗2026-03-04~09-02共126日、收缩0.10/单票上限20%/满仓多头未加杠杆）**：平均绝对相关仅 **0.161**（商品篮子整体易分散），但板块内联动强——贵金属0.598、能源化工0.346、黑色0.300、有色0.279，贵金属对能源化工 −0.160 具跨板块对冲；最强相关对为产业链 PX-TA=0.81、BC-CU=0.78、L-PP=0.77、J-JM=0.72，最弱为黄金对化工 AU-PF=−0.40；**等权组合单日历史VaR95=1.06%/ES95=1.21%/VaR99=1.31%，参数VaR95=0.98%（肥尾溢价+8%，正态略低估左尾）、10日VaR95=3.11%**；风险型权重显著降尾部——erc 历史VaR95=0.66%、gmv=0.40%（代价是有效N 61→34.8/12.3、更集中），分散化收益等权59.7%→gmv71.8%；**原油−5% 等权组合−0.61%（SC/FU/LU/EB 拖累最大），gmv 仅−0.07%**。
- **测试与零改动证据**：新增 `tests/test_portfolio_risk.py`（139行19例零网络/零面板：相关阵正负与零方差、平均相关、板块块、分位数参数化、组合收益序列、历史VaR/ES与全正序列VaR为负、参数VaR精确值与√h及未知分位报错、原油beta精确线性/零方差、压力方向排序、分散化不相关为正完全相关为0、端到端与零方差退化）+ test_tools_selftest 注册 portfolio_risk 11组与 portfolio_risk_lab 3组 selftest；全量 **pytest 549→572 全绿（0失败/错误/跳过，cache/r47_junit.xml）**、compileall 过。默认8品种 equity=c4da4cdf61f3bcdc/trades=50dcc800d326f8e9 双 sha256 逐字节一致；main/analyzer/portfolio/risk_gate 零引用；运行依赖仍仅 requests/uiautomation/websocket-client。
- **规模**：生产 py 64→66（根45→46新增 portfolio_risk 347行、tools19→20新增 portfolio_risk_lab 241行）/28897→29485 行；tests 35→36 文件/5597→5748 行；用例 549→572；统一实验台账6类各1条（新增 portfolio_risk_lab）。代码 commit d87983f。G5 研究侧①②③闭环，④熔断动作/⑤风险平价sizing与组合历史净值回测为后续。

## [0.46.0] — 2026-09-03 · 第46轮 G19 数据库在线热备份+滚动保留+开机自启/定时任务导出+灾备恢复+main --version（新增根模块 db_backup.py，只读源库只写backup/，主链与默认CSV零改动）
- **任务与定位**：补运维安全短板——monitor.db（WAL、约360MB，含 quotes/minute_bars/signals/signal_outcomes/news/options/paper_*/backtest_runs 全部家当）此前**没有任何自动备份**，磁盘损坏/误写/误删即全损。新增**根模块 `db_backup.py`（533行，纯标准库 sqlite3/os/shutil，零网络，不接 main 主循环、不改综合分、不改任何生产数据）**。区别于 tools/db_archive.py（按年导出归档快照）：本工具做**高频在线热备+滚动保留+一键恢复+自启导出**。
- **在线热备（不用停程序）**：用 SQLite 官方 **Online Backup API**（`src.backup(dst,pages=-1)`），源库以**只读 URI（mode=ro）**打开，main 常驻写库时也得到事务一致性快照、对 WAL 安全、不持长锁；约360MB 实测约10秒。备份后对**副本**跑 `PRAGMA quick_check`，不通过立即删除坏副本并报错——**不留"看着有、实际坏"的假备份**；每份配同名 `.json` sidecar（源/副本大小、双 quick_check、各表行数、VERSION、时间）。
- **滚动保留/只认自己命名**：`backup/monitor_YYYYMMDD-HHMMSS.db`，默认保留最近 **30 份**（--keep 调，<=0 全保留），按时间戳删最旧、sidecar 同步删；parse_backup_stamp 严格校验命名，目录里其它文件（notes.txt/别的.db）**绝不误删**；同秒碰撞自动加序号不覆盖。
- **校验/列举/恢复**：--list（时间/大小/副本qc/版本）、--verify（全部或 --latest-n 最新N份 quick_check，异常返退出码1）、--restore（先校验备份非坏→现有 monitor.db 及 -wal/-shm **改名留存为 .before_restore_时间戳、绝不直接覆盖丢现场**→反向 Online Backup 写回→新库再 quick_check；交互需输 yes，--yes 供脚本）。
- **自启/定时只导出、不擅自改系统**：--emit-bat 生成 `run_backup.bat`（chcp65001+切目录+--once，失败 pause，可双击/任务调用）；--emit-task-xml 生成 Windows 任务计划可直接导入的 `backup/futures_monitor_db_backup_task.xml`（**每日16:30 + 用户登录**各一次、最小权限 LeastPrivilege、错过补跑 StartWhenAvailable、30分钟超时、IgnoreNew 防重入）；**不执行 schtasks /register**，导入步骤（图形/命令行二选一）与异地保管写进《灾备恢复Runbook.md》。
- **main 只读 --version（G19④，默认等价）**：main.py 新增 `--version`，parse 后最早分支读 VERSION 打印即 return，**不 setup_environment/不连库/不启动常驻**；不传该参数代码路径完全不变（默认8品种 equity/trades 双 sha256 逐字节一致为证）。db_backup 自身也有 --version。
- **真实冒烟诚实结果**：对真实 monitor.db 备份成功——源359.8MB→副本359.8MB、14张用户表467,695行、源/副本 quick_check 均 ok、约10秒；--list/--verify 正常识别且只认本工具文件；selftest 在 tmp 库演练"备份→改坏现场→恢复到备份点、旧库留存、坏备份拒绝恢复"全链路。`.gitignore` 忽略 backup/*.db、*.db.json、*.before_restore_*（二进制不入库），任务 XML 模板入库。
- **测试与零改动证据**：新增 `tests/test_db_backup.py`（161行22例零网络/零生产库，全部 tmp_path 造临时 sqlite，绝不碰 data/monitor.db：文件名解析反例参数化、prune_plan、只认前缀、在线备份一致性+源只读、sidecar行数、滚动删最旧连同sidecar、同秒碰撞、缺源报错、恢复留存旧库、坏备份拒绝、坏库quick_check返OPEN_ERROR、XML/bat内容、版本缺失安全）+ test_tools_selftest 注册模块13组 selftest；全量 **pytest 526→549 全绿（0失败/错误/跳过）**、compileall 过。默认8品种(I,MA,RB,SA,TA,AU,AG,CU) equity=c4da4cdf61f3bcdc/trades=50dcc800d326f8e9 与 cache/base 双 sha256 逐字节一致；真实 `main --once` exit0、stderr 0行零异常；运行依赖仍仅 requests/uiautomation/websocket-client 零新增。
- **规模**：生产 py 63→64（根44→45新增 db_backup 533行、tools19不变）/28352→28897 行；tests 34→35 文件/5430→5597 行；用例 526→549。代码 commit 2f86514。运维侧下一步：用户按 Runbook 导入任务计划即实现每日自动备份；G19 主体闭环。

## [0.45.0] — 2026-09-03 · 第45轮 G27②③ walk-forward 参数稳定性 + fee/slip 成本敏感性曲面/换手容量（新增 tools/wf_cost_lab.py，复用既有 WF/回放内核，主链与默认CSV零改动）
- **任务与定位**：补齐总纲 G27 剩余两切片（①统一实验台账已在第44轮 experiment_ledger 落地）。新增**研究侧工具 `tools/wf_cost_lab.py`（601行，纯标准库、零网络、只读 monitor.db、只写 reports/wf_cost_lab.txt|.json，不被 main/analyzer import）**：②滚动 walk-forward 检验"最优参数是稳定锚定还是每窗都在换、样本内优势到样本外衰减多少"；③在每腿费率×单边滑点网格上重放，出净复利/夏普/胜率曲面与"成本加到多少策略由盈转亏"的安全垫，并用分钟 bar 成交量做换手率与可承载资金的**数量级**估算。MLflow 只借实验组织思想、不引服务；不引 numpy/pandas/vectorbt。
- **复用而非重写（关键工程决策）**：第34轮 backtest_validation 已实现同一套信号/撮合的参数网格回放 `build_param_grid_matrix`（对 config 的 entry/stop/target 稳定性网格逐组合回放、按平仓交易日聚合成 T×N 日收益矩阵）与 AFML 式 `walk_forward`（滚动 IS 窗按夏普选最优参数、下一 OOS 窗验证，输出衰减/跑赢中位数比例/切换率）。本轮**不重写 WF 引擎**，wf_cost_lab 只补"跨品种批量组织 + 选中参数轨迹 + 稳定度评级 + 成本曲面 + 换手容量 + sidecar/台账登记"。
- **② walk-forward 参数稳定性 `wf_stability`**：从 WF 分段提炼选中参数名轨迹、各候选被选次数/最高频占比（锚定率）、切换次数/率、IS→OOS 夏普衰减、选参遗憾（事后最优−实际选）、OOS 为正段占比、跑赢 OOS 中位数比例；评级规则 **稳定（锚定率≥60% 且 OOS正段≥60%）/ 一般（锚定率≥40%）/ 漂移（<40%）**，样本不足显式标注。
- **③ 成本敏感性曲面 `build_cost_surface/breakeven_cost`**：固定全样本笔夏普最高参数，在 每腿费率(0/0.25/0.5/1/2bp)×单边滑点(0/0.5/1/2/4bp) 的 5×5 网格用 simulate **兜底比例费率模式**（use_real_fees=False、每腿收 fee_rate、滑点按方向不利偏移成交价，以便精确扫档；与真实费率表口径不同，结果用于相对敏感度/安全垫而非绝对盈亏）重放，每格汇总笔数/胜率/笔夏普/逐笔复利/成本占毛利比；沿基准行/列找首个净收益转负档，给出相对基准的**成本安全垫倍数**（全程不转负记∞、基准已亏记0）。
- **换手与容量 `estimate_turnover_capacity`（数量级、诚实标注假设）**：按交易日聚合分钟 bar 成交量×收盘价×合约乘数估"市场日均名义"，统计策略日均笔数/单手名义/1手年换手笔数；在**参与率上限默认10%**下反推每笔可承载手数与对应名义资金。明确为线性、忽略价格冲击、基于库内 bar 覆盖时段的数量级估算（免费数据无盘口深度，精确容量待 G14 一档盘口自采）；乘数在兜底费率模式 simulate 内为0，改为单独从真实费率表 load_fee_schedule 取。
- **真实数据诚实结论（默认代表品种 RB,MA,I,TA，30m，各1047bar/83交易日/18组网格，WF=20训/10测=6段）**：**RB 漂移**（最优 e2/s2/t1.5，OOS夏普-0.10，基准复利-3.6%，成本/毛利64.3%，基准档已不盈利→安全垫0）；**MA 一般**（e2/s1.2/t3，OOS+0.04，基准复利+17.8%，成本/毛利仅6.9%，fee/slip 扫到最高档全程不转负=∞，最扛成本）；**I 漂移**（e1.5/s2/t2，OOS-0.14，-10.1%，基准已亏）；**TA 漂移但正收益**（e1.5/s1.2/t3，OOS+0.06，+6.5%，费率全程扛住、滑点到4bp才转负≈基准4倍安全垫）。容量数量级：RB 市场日均名义约154亿、单手3.1万、10%参与率每笔约3.1万手/名义约9.7亿；TA 约204亿、单手3.0万、约4.2万手/12.3亿（低频策略容量本就大，仅数量级）。结论与第34轮一致：该30m入场参数在近窗样本外优势弱、参数普遍漂移，**RB/I 连零成本附近都难盈利、对成本无安全垫，不应进入默认**；MA/TA 有成本缓冲但仍需双样本+影子，调参不覆盖默认。
- **测试与零改动证据**：新增 `tests/test_wf_cost_lab.py`（179行14例零网络/零DB：统计/曲面单调/break-even三态/WF稳定-漂移-越界防御/容量手算/成稿/JSON无NaN，曲面用注入假 runner 不碰回测内核）+ test_tools_selftest 注册模块自带8组 selftest；全量 **pytest 510→526 全绿（0失败/错误/跳过）**、compileall 过。**铁律零改动**：默认8品种 portfolio equity/trades 与 cache/base **双 sha256 逐字节一致**（equity c4da4cdf61f3bcdc / trades 50dcc800d326f8e9）；grep 证明 main.py/analyzer.py 零 import wf_cost_lab；真实 `main --once` exit0、stderr 0行零 ERROR/Traceback。运行依赖仍仅 requests/uiautomation/websocket-client，零新增。
- **G27①台账联动**：wf_cost_lab 运行末尾经 experiment_ledger.safe_record 旁路登记一条 kind=wf_cost_lab（参数/各品种评级与OOS夏普/基准复利/双轴安全垫），登记失败不影响本工具；真实台账最终保持5类各1条干净（trade_journal/research_review/portfolio_lab/portfolio.compare_risk/wf_cost_lab），reports 恢复16品种口径（trade_journal 2563笔）。
- **规模**：生产 py 62→63（tools 18→19）/27751→28352 行；tests 33→34 文件/5245→5430 行；用例 510→526。代码 commit 36f9068。**至此 G27 三项（①台账②WF稳定性③成本曲面/容量）全部落地**；后续可做：曲面扩到多参数/多周期、WF 扩 purged+embargo 组合、精确容量待 G14 盘口。

## [0.44.0] — 2026-09-03 · 第44轮 G27① 统一实验台账 experiment_ledger（追加式JSONL登记各研究/回测实验，config_hash同配置一致+repeat_of漂移串联，4宿主旁路钩子，主链与默认CSV零改动）
- **任务与定位**：落地总纲 G27「统一实验台账 + walk-forward 稳定性/成本敏感性」的第一切片。此前 portfolio_lab/trade_journal/research_review/portfolio --compare-risk 等研究实验各自落 reports，靠文件名与时间戳区分，"同参数是否重跑过、结果漂了多少、用哪份数据跑的、一键复现命令"没有统一登记处；生产库 backtest_runs 只登记日线回测且属 storage 主链（研究工具纪律=只读生产库、不往里写）。新增**根模块 `experiment_ledger.py`（626行，纯标准库、零网络、不被 main/analyzer import）**，只做"登记与查询"（MLflow 只借台账思想不引服务、vectorbt 只学实验组织不引 numba/numpy）：追加式 `reports/experiment_runs.jsonl`（gitignore 运行日志，绝不覆盖任何既有报告/CSV）。G27②walk-forward 滚动评估、③fee/slip 成本敏感性曲面/换手容量预留登记入口、留续。
- **`experiment_ledger.py`（626行，13组零网络自测）**：
  - **配置身份 config_hash（G27 验收点）**：canonical_bytes 排序键+紧凑分隔+UTF-8（键序无关、中文稳定），canonical_hash=sha256(实验类型+规范化参数+**输入数据内容身份**)取16位，**刻意不含运行时间/文件 mtime/产物**——输入被逐字节重写（mtime 变、内容不变）hash 不变，故同配置两次实验 hash 一致；json_safe 前置清洗（非有限浮点→None、datetime→串、set/tuple→list、键转 str），落库前 `json.dumps(allow_nan=False)` 预检。
  - **数据指纹与身份**：file_fingerprint（exists/size/mtime/小文件≤2MB 算全量 sha256，超限只登记 size，避免对分钟库/大 CSV/DB 全量哈希）、build_manifest 批量去重、data_identity_from_manifest 有 sha 用 sha 否则退化 size 且**排除 mtime**。
  - **追加式台账 LedgerStore**：每行一条紧凑 JSON（utf-8、LF），读时宽容（空行/坏行跳过并计 bad_lines），写时**原子替换**（同目录 tmp+os.replace）+进程内 RLock；同 config_hash 已存在则写 `repeat_of=上一条 run_id`（两条都保留、串联不覆盖），同秒同 hash 撞 run_id 自动加 -r2/-r3；filter 按实验类型/limit。
  - **宿主统一入口 safe_record**：构造+追加，任何异常全吞返回 None（台账是旁路、绝不拖垮宿主）；环境变量 `FUTURES_EXPERIMENT_LEDGER` 可重定向台账路径，置 0/off/false/none 显式关闭（测试隔离用）。
  - **查询 CLI**：`--list`（一行一次实验：时间/类型/config/重复↻/关键指标）、`--show run_id`（单条全文：参数/指标/输入数据身份/产物/复现命令/版本/py）、`--repeats`（同 config 多次运行与指标漂移）、`--export`（导 JSON 数组）、`--experiment/--limit/--ledger`。
- **4 个宿主挂"旁路登记钩子"（全部 try 包裹、登记失败绝不影响宿主产物与返回值，不改任何产物口径）**：
  - `portfolio.py`：**仅 `--compare-risk` 影子对照时**登记三法（等名义/逆波动/ERC）end_equity/total_ret/ann_ret/sharpe/max_dd/avg_risk/npos/n_trades 与全套 sizing 参数，普通回测路径完全不触发；
  - `tools/portfolio_lab.py`：登记四方法滚动 ann_ret/ann_vol/sharpe/maxdd/calmar/有效N/换手与组合构建设置；
  - `tools/trade_journal.py`：登记笔数/胜率/PF/盈亏比/期望/最长连亏/由盈转亏（payload 构造提到 json 分支外以便钩子复用）；
  - `tools/research_review.py`：**惰性 import**（守其模块级不 import 项目模块的纪律，导入前补 _ROOT 到 sys.path），登记数据源 ok/陈旧/缺失计数与待办 WARN/INFO 计数。
  - `tests/conftest.py`：测试启动把台账重定向到系统临时文件并清空，防止测试调用宿主 run() 时污染真实 reports；`.gitignore` 增 `reports/*.jsonl`。
- **验证（全绿且主链零影响）**：新增 `tests/test_experiment_ledger.py`（22例零网络确定性测试：规范哈希键序无关/同配置一致/敏感性、json_safe、指纹与排 mtime、记录构造、追加与 repeat 串联、run_id 碰撞、坏行宽容、原子 LF、safe_record 成功/吞错、渲染、CLI、环境变量关闭重定向）+ selftest 注册 + compileall 随新根模块，全量 **pytest 486→510 全绿（0失败0错误0跳过）**、13组模块自测过、compileall 过；**真实冒烟4宿主钩子全部登记成功**，真实演示同配置两次 config_hash 完全一致（trade_journal fc30b0d5ce、research_review 3fe4f997a1）且 repeat_of 正确串联；**默认8品种(I,MA,RB,SA,TA,AU,AG,CU) equity/trades 与 cache 基线双 sha256 逐字节一致**（证明 portfolio.py 主链改动零影响）；grep 证明 main.py/analyzer.py 零 import 台账；真实 main --once exit0、stderr 空、零 ERROR/Traceback。生产 py 61→62（根43→44、tools18不变）27021→27751 行，tests 32→33 文件 5011→5245 行，运行依赖仍仅 requests/uiautomation/websocket-client。

## [0.43.0] — 2026-09-03 · 第43轮 G30③ 研究侧一键复盘编排器 research_review（聚合各研究sidecar七段成稿+规则化待办，纯标准库只读，不重跑/不import主链/不覆盖主链daily_review）
- **任务与定位**：承接第42轮 G30① trade_journal，落地总纲 G30③「一键日/周复盘：行情→因子表现G29→信号命中→交易归因G28→风险G3/G5→待办」。新增 `tools/research_review.py`：把各研究工具**已落盘**的 reports/*.json sidecar + 组合权益 CSV + 主链信号追踪文本聚合成一份"收盘研究简报+规则化待办清单"，秒级出 reports/research_review.txt+.json。**设计取舍：不 subprocess 重跑任何工具**（解耦、秒级、零副作用；各工具按各自节奏人工跑，编排器只串联最新结论并指出先做什么），任一 sidecar 缺失/损坏/字段不全/陈旧全部安全降级并给出刷新命令。**命名隔离**：主链已有 reports/daily_review.txt（report.build_daily_review 实时轮动+新闻、永久保留，属 main 铁律不动），故研究侧模块/产物命名 research_review.*，绝不覆盖主链文件。守三铁律：纯标准库零新依赖、不 import 任何生产模块、不被 main import、不改主链/综合分/默认CSV。
- **`tools/research_review.py`（777行，11组零网络自测，SOURCES 台账登记9个数据源与刷新命令）**：
  - **装载与新鲜度**：load_sidecar 坏 JSON 返(None,mtime)区分 missing/broken；freshness_state 三态（ok/stale/missing，默认陈旧阈值168h）+ age_label；**utf-8-sig 读 CSV/TXT 兼容 Windows BOM**（真实 portfolio_equity.csv 带 BOM，首列键否则变 `\ufeffdt`——本轮踩出的坑）；load_equity_summary 跳过末尾空记录、期初取首行/期末取末行、全表扫最大回撤；load_signal_tracking 正则提取主链信号追踪各周期样本/胜率/方向收益。
  - **六段提取器（缺字段安全返{}）**：sec_factor_health（G29 事件因子 verdict 收集"失效预警"、日频5日RankIC排序、IC半衰期）、sec_attribution（G28 指定周期 alpha/R²、因子贡献与板块BHB effect 排序）、sec_journal（G30① 总览+持仓/信号弱势桶[n≥10且PF<0.7]+由盈转亏比例）、sec_lab（G26 四方法滚动年化/波动/夏普/回撤/换手+快照有效N）、sec_validation（WP-F4 DSR/SR0/裁决+网格PBO）。
  - **规则化待办引擎 build_actions**：纯规则产出 (WARN/INFO/OK,文本) 并按级排序；覆盖 sidecar 缺失（可选产物 expr_research 与主链 signal_tracking 缺失降 INFO）、陈旧、因子失效预警、journal 整体PF<1与弱势桶、由盈转亏、组合回撤>15%/风险度>80%、DSR<0.95、归因alpha偏负、ERC显著优于等权的决策素材（不自动改 sizing）；全绿给 OK。**待办是提示不是调参令**，改参仍须另开轮次双样本+影子。
  - **成稿/CLI**：build_report 抬头+〇数据源新鲜度总表+一信号命中+二G29+三G28+四journal+五组合风险+六防过拟合+七规则待办+固定声明；build_json_payload allow_nan=False；CLI `--reports-dir/--stale-hours/--out/--json-out`，空目录也能出全降级报告。
- **真实聚合诚实结论（reports 9个数据源全部新鲜，秒级成稿）**：
  - 信号命中：30分钟464样本胜率49.3%/方向收益-0.01%、2小时441/50.9%/+0.02%、**次日315/55.2%/+0.25%（方向优势随持有期拉长才显现，分钟级≈掷硬币）**。
  - G29 事件因子 30m **4个失效预警**：新闻消息面（IC+0.711%、最长连失6、失败占比62.5%）、机构动向（+2.355%/连失3）、日线动量（-4.519%/连失5）、技术共振（+7.656%/连失5）；日频5日RankIC 最弱 ret20 -2.673%、最强 tsmom252 +1.118%（长周期动量仍最稳，与第39轮 regime 结论一致）。
  - G28 30m n=376 alpha=-0.00021/根、R²仅2.7%，贡献最负日线动量、最正技术共振，板块农产品 effect+0.00099 最正。
  - 组合风险：默认8品种回测 100万→92.09万（-7.91%）、最大回撤10.5%；G26 滚动504日61品种 equal夏普0.42/回撤9.5% vs inv_vol0.55/7.8%、erc0.53/6.5%、gmv0.35/5.3%（年换手177%过高）；WP-F4 组合 DSR=0.0001（试18组）无法排除多重试验偶然性、2品种网格0个样本外为正。
  - 一键待办 6 WARN+3 INFO，把"4个失效因子、journal PF0.90与极短桶PF0.57、59%由盈转亏、DSR不达标、ERC降回撤素材"按优先级一次列齐，免去人翻7份报告。
- **验证**：新增 tests/test_research_review.py 15 例零网络（缺/损sidecar、新鲜度三态、equity BOM+末尾空记录+全表回撤、信号正则、各段提取排序与弱势桶门槛、待办WARN优先/可选降级/全OK、collect空目录与合成sidecar、七段成稿与allow_nan），test_tools_selftest 注册 selftest（+1）、test_compileall 随新生产py自动+1；全量 **pytest 469→486 全绿（约7.3s、32个测试文件+conftest）0失败0错误0跳过**、compileall 过；**默认8品种（I,MA,RB,SA,TA,AU,AG,CU）组合回测 equity/trades CSV 与 cache 基线哈希逐字节一致**（本轮 git 仅新增 tools/research_review.py 与两 tests、主链零改动，trades/equity 双哈希复现一致）；真实 main --once 43s exit0 无 ERROR/Traceback。规模：生产 py 60→**61（根43/tools17→18含research_review）、26244→27021 行**；测试 31→32文件、4806→5011行；运行依赖仍仅 requests/uiautomation/websocket-client。总纲 G30 段补③落地、3.5表/6.3排期更新。tag v0.43.0。

## [0.42.0] — 2026-09-03 · 第42轮 G30 交易复盘 journal（七维分桶+日周节奏+盘中MFE/MAE一键成稿，研究侧纯标准库只读，不碰主链/综合分/默认CSV）
- **任务与定位**：总纲 G30「交易复盘 journal + 纸面/回测一致性 + 一键日/周复盘」的第一块（复盘三件套 G28归因✅/G29体检✅ 之后补齐 journal）。现状是有逐笔成交与纸面账户，但"钱亏在哪类单上"靠人翻报告。本轮新增 `tools/trade_journal.py`：读 `portfolio.py` 产出的 portfolio_trades.csv（每行=一笔完整开平 round-trip，净盈亏已含开平费与平今腿）与可选 equity CSV，一键出分桶/节奏/MFE-MAE/典型单/规则化观察的 txt+json 报告。守三铁律：纯标准库零新依赖、**只读分析不改 main/analyzer/综合分、不改 portfolio 默认 CSV 输出（8品种基线哈希仍逐字节一致）**；G30②纸面vs真实成交一致性（需 G14 自采盘口）与全链路 daily_review 编排器本轮不做、留续。
- **`tools/trade_journal.py`（710行，17组零网络自测，复用 metrics.trade_stats/excursion 同口径）**：
  - **装载与安全降级**：load_trades 宽容解析两种时间格式/坏值/负分空头/缺文件返回[]；按 exit_dt 升序。空文件/0笔/缺 equity/缺分钟库全部出降级报告不抛错（总纲验收要求）。
  - **七维分桶 bucket_table**：品种/板块/多空/平仓原因组（止盈·止损·日终强平·反向信号归并）/平今平昨/**信号强度（按|入场分|，空头入场分为负、方向已独立成桶，多空同档）**/持仓时长档；每桶出 笔数/胜率/净盈亏/均笔期望/盈亏比payoff/利润因子PF/费用/费用占|毛盈亏|/均持仓/最长连胜连亏，按净盈亏升序最差在前。日·周节奏 period_pnl（ISO周）、累计净值曲线、equity 曲线期初末/回撤对照。
  - **盘中 MFE/MAE（可选 `--bars`）**：与 portfolio.load_minute_feed 同口径装载+`backtest.ratio_adjusted_bars` 比例复权，bisect 取 [entry,exit] 闭区间，用每根 bar 的**盘中 h/l**（多: MFE看h/MAE看l，空镜像）算最大有利/不利偏移；按品种只装载一次，覆盖率透明标注、缺区间笔安全跳过。另出"亏损单盘中曾浮盈仍亏损平仓=由盈转亏/回吐"比例。
  - **一键成稿**：总览/七维分桶/日节奏/周节奏/MFE-MAE/最佳最差5单/规则化观察（期望拆解 wr×均盈−败率×均亏、n≥10且PF<0.7 弱势桶、费用占比、强平结构、由盈转亏；全胜桶 PF=None 不误报、原因组定义性全赢全亏不参与弱势扫描）；CLI `--trades/--equity/--bars/--period/--review {none,daily,weekly,both}/--out/--json-out`，出 reports/trade_journal.txt+.json（allow_nan=False）。
- **真实数据诚实结论（16品种30m、2026-05-11~09-01 共2563笔，--bars 覆盖率100%，1.8s）**：
  - 总览：胜率39.7%、盈亏比1.36、**PF 0.90（成本后期望为负，−31元/笔）**、净 −79,101（毛 −42,768、费用 36,334 占|毛|85.0%）、均持3.9根30m bar、最长连胜13/连亏15；多1279笔 PF0.83 亏64,033、空1284笔 PF0.96 亏15,068（样本偏震荡）；17周仅5周盈利（29.4%）。
  - **最有行动价值的结构——亏损高度集中在极短持仓噪声单**：持1-2bar 1289笔（占50.3%）胜率30.3%/**PF0.57 净−196,770**；3-6bar 981笔 PF1.26 +63,851；7-12bar 286笔 胜率59.4%/**PF2.16 +53,177**；13bar+ 7笔 PF3.07。**拿得住的单才赚钱、1-2根bar被噪声扫出的单系统性失血**。
  - 平仓结构：止损970笔 −616,730 vs 止盈530笔 +532,993（均笔+1,006）；**日终强平870笔反而胜率54.5%/PF1.78/+61,736**（日终不留仓不是亏损来源）；反向信号193笔 PF0.03 −57,099（反向即走在这批样本里代价高）。信号强度单调改善：弱(|分|<2)526笔 胜率34.2%/PF0.82 → 轻仓[2,4)1546笔 40.1%/0.89 → 分批[4,6)491笔 44.2%/**1.00打平**（无≥6.5桶，分钟级入场分到不了强档），alpha 随信号强度上升、弱信号纯失血。品种仅 JM 为正（+6,996/PF1.09），RB/FG/CF 最弱。
  - 盘中 MFE/MAE：全体均 MFE0.46%/MAE0.45%；盈利单 MFE0.84%/MAE0.20%（浮盈路径顺畅），亏损单 MFE0.22%/MAE0.61%；**亏损单1546笔中912笔（59.0%）盘中曾浮盈>0.1%仍以亏损平仓**——离场/止盈纪律的改进线索（本轮只出证据、不改策略参数）。
- **验证**：新增 tests/test_trade_journal.py 17 例零网络（CSV往返类型/排序、原因归并、|分|档位、分桶手算PF、日周ISO聚合、MFE-MAE多空镜像与闭区间、monkeypatch注入bars不碰生产库、全胜桶不误报/弱桶命中、run端到端txt+json），test_tools_selftest 注册 selftest（+1）、test_compileall 随新生产py自动+1；全量 **pytest 450→469 全绿（约8.9s、31个测试文件+conftest）0失败0错误0跳过**、compileall 过；**默认8品种组合回测 equity/trades CSV 与基线哈希逐字节一致**（证明主链零改动）；真实 main --once 42.6s exit0 无 ERROR。规模：生产 py 59→**60（根43/tools16→17含trade_journal）、25534→26244 行**；测试 30→31文件、4580→4806行；运行依赖仍仅 requests/uiautomation/websocket-client。总纲 G30 段补第42轮进展、3.5表/6.3排期更新。tag v0.42.0。

## [0.41.0] — 2026-09-03 · 第41轮 G26续 风险型横截面sizing接入组合共享内核（inv_vol/ERC可选，默认逐字节等价旧等名义 + 同宇宙影子对照）
- **任务与定位**：承接第40轮 G26（离线组合构建器+portfolio_lab 证明 ERC/逆波动降险价值），本轮按总纲 G26「实施顺序②」把**逆波动/ERC 风险型目标权重接入 `portfolio.py` 共享账户内核**（组合回测与 paper_broker 都走它）。守三铁律：纯标准库零新依赖、**默认 risk_sizing=None 时手数决策逐字节等价旧等名义（CSV 哈希级回归）**、不碰 main/analyzer/综合分；GMV 第40轮已证过集中（有效N仅11）故**不接入**；实时/paper 的协方差权重源本轮**不接线**（先在回测影子对照达标后再议，未注入权重时内核自动回退等名义）。
- **`portfolio.py` 共享内核增量（1018→1191行）**：
  - `Portfolio` 新增 `risk_sizing(None/inv_vol/erc)`、`risk_gross` 参数与 `risk_weights/risk_meta/risk_meta_log` 状态、`set_risk_weights()` 注入与 `avg_risk_eff_n()`；`decide_lots` 在三种旧 sizing 分支之后插入**横截面权重覆盖**：宇宙内品种目标名义=权益×权重（×gross），**宇宙外/未估出安全回退等名义 per_symbol**；下游单品种/板块/现金/持仓数上限链与校准乘子原样复用。`risk_sizing=None` 时整段不进入。
  - 新增纯函数 `trailing_risk_weights(feeds,t,method,...)`：在时刻 t 用**严格早于 t** 的收盘价（bisect_left−1，t 当根及以后一律不看=严格 PIT），各品种按公共时间戳稠密对齐收益、≥min_hist 才纳入协方差宇宙，调 `portfolio_constructor.construct` 出 inv_vol/erc 权重并×gross；可估品种<2/历史不足返回 `({},meta带reason)` 绝不抛错；缺历史品种进 `excluded`。
  - `run_portfolio` 增 `risk_cfg`：按 rebalance 间隔重估并注入权重（默认 None=零行为变化）；新增 `_reset_feeds()` 清引擎层持仓/挂单/锁板计数，使同一批 feeds 可确定性重复回放。
  - CLI 增 `--risk-sizing/--risk-window/--risk-rebalance/--risk-min-hist/--risk-gross/--risk-cap/--compare-risk`；`--compare-risk` 同宇宙把 等名义/逆波动/ERC **各确定性回放一次**出对照表（期末权益/收益/年化/夏普/回撤/平均风险度/最大持仓/平仓笔数/平均有效N），**基线 CSV 仍写等名义、逐字节不变**；报告头部标注风险型口径。
- **`paper_broker.py`（830→840行）**：`PaperBroker` 增 `risk_sizing/risk_gross` 能力位透传给内核 + `set_risk_weights()` 透传；默认 None 逐字节等价旧版，实时权重源未接线（注释明示）。**`config.py`** 增 PRS_* 常量簇（默认全关、PRS_METHOD=erc、窗口126/重估20/最少40/gross1.0/单票上限20%）。
- **真实数据影子证据（本地分钟库、30m、严格无未来，仅目标名义不同）**：
  - 8品种小样本：三法平仓同为995笔（证明只改手数不改信号路径）；等名义 −3.91%/回撤5.91%/平均风险度6.7%，逆波动 −6.00%/回撤7.30%，ERC −6.45%/回撤7.90%（gross1.0在小宇宙敞口偏保守、有效N仅6.5）。
  - **16品种宽样本（RB/HC/I/JM/CU/AL/MA/TA/SA/FG/AU/AG/M/Y/SR/CF）**：等名义 −7.91%/回撤10.50%/平均风险度15.2%/2563笔；**逆波动 −2.55%/回撤3.37%/风险度5.0%/1791笔/有效N13.2；ERC −2.67%/回撤3.29%/风险度5.0%/1828笔/有效N12.8**——宽宇宙下风险型显著降回撤/降风险度（其设计目标），笔数减少是"低权重高价品种目标不足1手不开仓"的约束链一致结果（已在对照表说明，非未来函数）。结论方向与第40轮日级 portfolio_lab 一致：**ERC/逆波动的价值在降险而非增收益，值得继续在 paper 影子；是否默认启用仍须更长样本与换手成本评估，本轮不默认开**。
- **验证**：tests/test_portfolio.py +6 组零网络断言（默认关注入权重也逐值不变、权重定手数+宇宙外回退手算、**严格PIT篡改未来bar权重不变**、权重和=gross/非负/上限/excluded、_reset_feeds 重复回放逐值一致、引擎 risk_cfg=None 与不传逐值一致+重估留痕）；全量 **pytest 444→450 全绿（0失败0错误0跳过）**、compileall 全过；**默认组合回测 equity/trades CSV 与改动前基线哈希逐字节一致**（报告仅标题行变化）；真实 main --once 48s exit0 证明主链/综合分零改动。规模：生产 py 仍59（根43/tools16）、25341→**25534 行**；测试仍30文件、→4580行；运行依赖仍仅 requests/uiautomation/websocket-client。总纲 G26 段补第41轮进展、6.3 排期补第41轮。tag v0.41.0。

## [0.40.0] — 2026-09-03 · 第40轮 G26 组合构建器（纯标准库风险型横截面权重：等权/逆波动/ERC风险平价/长仓GMV + 目标波动，默认equal等价旧口径，研究侧不接main不改综合分/sizing）
- **任务与定位**：G1 纸面账户/G4 回测严谨性/G21-G25-G29 研究链就位后，补"模拟整合"环节的资本分配层——现有 `portfolio.decide_lots` 是**逐品种独立**按名义/ATR/分档定手数，缺一个**横截面一次性**回答"同一篮子谁分多少"的组合构建器。守三铁律：纯标准库零新依赖、**默认 equal 等价旧等名义口径、不接 main、不改综合分与既有 sizing**；风险型方法（只用协方差、不预测预期收益，契合项目"不轻信 ER/ML"立场）。
- **新增根模块 `portfolio_constructor.py`（约430行，10组零网络手算自测，不 import tools/不被 main import）**：
  - 协方差 `covariance`（样本）+ `shrink_diagonal`（Ledoit-Wolf 简化对角收缩保正定/改善条件数）、幂迭代最大特征值（定步长）、Cholesky 判 PSD、自包含高斯消元 `_gauss_solve`。
  - 四方法：`equal_weights` 等权（基线/回退）、`inverse_vol_weights` 逆波动 w∝1/σ、`risk_parity` **ERC 等风险贡献**、`min_variance`/`quadratic_long_only` **长仓最小方差**（FISTA 加速投影梯度+capped-simplex 投影，凸问题全局最优）。
  - **ERC 算法踩坑（务必沿用，勿回退）**：乘性定点/对数障碍梯度/对角 Newton 都会在61资产上停滞（风险贡献残差卡在0.019），根因是**迭代内归一化破坏 F(w)=½w'Σw−Σlnw 的自然尺度**；最终用**全 Newton**（Hessian=Σ+diag(1/w²)，解 H·d=g + 回溯线搜索保 w>0 与目标下降，迭代中不归一、仅末尾归一+上限投影），真实61资产约30次迭代即收敛到各品种风险贡献精确=1/n（maxRC=0.0164）。
  - 约束与诊断：`project_capped_simplex`（和=1+非负+单票上限的二分闭式投影）、`target_vol_scale`（按目标年化波动等比缩放总敞口、`max_gross` 杠杆上限防低波期过度加杠杆）、`risk_contributions`（边际/占比）、`diversification_ratio`、`effective_n`（1/Σw² 有效持仓数）、`turnover`（½Σ|Δw|，支持不同标的并集）。统一入口 `construct(returns,method,...)`。
- **新增 `tools/portfolio_lab.py`（约300行，5组自测，纯离线/零网络/只读 G21 面板）**：固定宇宙（最近504日覆盖率≥95%，真实入选61/64品种、稠密对齐）；①最新快照（过去126日协方差，各方法权重/年化波动/有效N/分散化度/最大单品种风险贡献/目标波动杠杆+前六大权重）；②**滚动样本外代理回测**（每20日用"仅当时可得的过去126日"估协方差定权并持有，严格无未来），对比年化收益/波动/夏普/最大回撤/Calmar/平均有效N/年化换手，equal 为基线。出 reports/portfolio_lab.txt/.json（allow_nan=False）。config 增 PC_* 常量簇。
- **真实数据诚实结论（61商品、2024-08-06~2026-09-02 共504日、每20日再平衡、无成本多头代理）**：
  - **风险型分配确实降波动/回撤（其设计目标），且 ERC 风险调整后最优**：等权 年化+3.97%/波动9.48%/夏普0.42/最大回撤9.54%/有效N61/零换手；**逆波动 +4.59%/8.35%(−11.9%)/夏普0.55/回撤7.79%/年化换手仅0.27（最省）**；**ERC +3.80%/7.11%(−25.0%)/夏普0.53/回撤6.46%(−32%)/有效N39.4/换手0.64（风险最均衡，maxRC仅1.6%）**；GMV +1.72%/4.93%(−48%)/夏普仅0.35/回撤5.26%/**有效N仅11.4（高度集中）/年化换手1.77**。
  - **判读**：GMV 降波动最猛但以集中（单票顶20%上限）、牺牲收益与高换手为代价、夏普反降，不适合直接用；**ERC 在降险与分散/成本间最平衡、逆波动最廉价**，二者值得后续在 paper/backtest 以"默认 equal、可切换影子对照、缺省逐字节等价旧版"接入；目标波动15%在当前低波商品上均触发1.5倍总敞口上限（说明该目标偏激进、杠杆须谨慎）。代理有幸存者偏差、未计费用/保证金/换月，**本轮只出证据、不改线上**。
- **验证**：test_tools_selftest 纳 portfolio_constructor/portfolio_lab（+2）、test_compileall 随2个新生产 py 自动+2；全量 **pytest 440→444 全绿（约7.9s、30个测试文件）0失败0错误0跳过**；两模块 `--selftest` 全过（含 GMV 波动≤等权的凸优化最优性不变量、对角协方差 ERC=逆波动手算、capped-simplex、目标波动杠杆上限、滚动无未来）、compileall 过；全树 grep 确认仅 portfolio_lab 引用 portfolio_constructor、main/analyzer/portfolio 零 import；真实全61品种 portfolio_lab 14.9s exit0 出 txt/json；真实 main --once 一轮冲烟证明主链与综合分零改动。规模：生产 py 57→**59（根42→43含 portfolio_constructor、tools15→16含 portfolio_lab）、24604→25341 行**；测试仍30文件；运行依赖仍仅 requests/uiautomation/websocket-client。总纲 G26 段补第40轮进展、6.3 排期补第40轮。tag v0.40.0。

## [0.39.0] — 2026-09-03 · 第39轮 G29续 因子 regime 分层 + 换手稳定性 + 幂律/指数衰减形态（研究侧纯标准库，只读G21面板，不接main不改综合分）
- **任务与定位**：承接第37轮 G29 因子体检刻意留下的三块增量——①因子是否只在特定市场状态有效（regime 分层）；②信号多稳、调仓换手多大（持续性/换手）；③IC 随持有期衰减到底是指数还是幂律形态（第37轮只拟合了指数）。守三铁律：纯标准库零新依赖、只读 G21 面板、**不接 main、不改任何线上权重与综合分**；复用 G25 表达式引擎的 ts_rank（PIT 滚动分位）、factor_health 日频层与 factor_eval.spearman，不重造轮子。
- **新增 `tools/factor_regime.py`（约360行，6组零网络/零DB自测）**：
  - **regime 标签（PIT、只用过去）**：trend_labels 用面板已 PIT 落库的 ret126 判牛/熊/震荡（|ret126|<2%=flat）；vol_labels 用 hv60 在过去120日的 ts_rank（G25引擎、尾窗无未来）分低/中/高波；compute_labels 每品种只算一次（滚动 ts_rank 是纯 Py O(n·win)，曾按因子×期限重复计算导致运行数分钟，重构为只算一次+缩短秩窗后正常）。
  - **regime_stratified_ic**：跨品种池化，按 trend(up/down/flat)、vol(low/mid/high) 及牛熊×低高波四组合分桶，桶内算前向 H=5/20 日 RankIC（单桶 n<40 不给 IC）。
  - **factor_persistence**：因子短滚动分位（ts_rank 窗20）在再平衡间隔 k=1/5/20 日的**秩自相关**（spearman）与平均|分位变动|=**隐含换手代理**（0~1，越低越稳/调仓越省）。
  - **fit_decay_shapes**：同一 |IC(H)| 曲线分别拟合指数 ln|IC|=a−H/τ（半衰期 τ·ln2）与幂律 ln|IC|=a−β·lnH，比较对数空间 R² 选 prefer；不衰减（斜率不倒）/有效点<3 安全返回 None（合成指数/幂律/常数三用例钉死择优）。输出 reports/factor_regime.txt/.json（allow_nan=False）。config 增 REGIME_* 常量簇。
- **真实数据诚实结论（全64品种面板61353行，九因子=ret5/20/63/126/252+tsmom63/126/252/blend）**：
  - **长周期动量是"低波专用、高波反转"的 regime 条件因子**：ret252/tsmom252 在 H=20 的**低波桶 IC≈+0.10（+0.100/+0.107）、熊低波 +0.112/+0.100**，而**高波桶转负（−0.037/−0.040）、熊高波 −0.022/−0.048**；短周期（ret5/tsmom63）各 regime 都在0附近。这与"动量在高波动/危机期崩溃（momentum crash）"的经典结论方向一致，是比"全样本IC≈0"更细的可操作结构——但按三铁律**本轮只记录、不改分不挂影子**，须再做双样本/事件层互证。
  - **持续性/换手**：九因子 lag1 秩自相关 0.72~0.85（信号平滑）、lag5 降到 ~0.47、lag20 ≈0；隐含换手 0.13(lag1)→0.27(lag5)→0.39(lag20)——月度调仓已换掉约四成分位、周度调仓能留住大部分信号，为后续 G26 组合调仓频率提供成本侧依据。
  - **衰减形态**：除 ret5 勉强指数（R²仅0.057、半衰期41日但几乎不解释）外，其余因子"指数不衰减/幂律不成立"——IC 在0附近变号、两种形态都不贴合，再次印证第37/38轮"单因子无稳定期限衰减"的负结果，不硬套半衰期。
- **验证**：test_tools_selftest 纳 factor_regime（+1）、test_compileall 随新生产 py 自动+1；全量 **pytest 438→440 全绿（约6.8s、30个测试文件）0失败0错误0跳过**；factor_regime `--selftest` 6组全过、compileall 过；全树 grep 确认 factor_regime 不被任何根/tools 生产模块 import；真实全64面板 factor_regime exit0 出 txt/json；真实 main --once 一轮冲烟证明主链与综合分零改动。规模：生产 py 56→**57（根42不变、tools14→15含 factor_regime）、24226→24604 行**；测试仍30文件；运行依赖仍仅 requests/uiautomation/websocket-client。总纲 G29 段补第39轮续进展、6.3 排期补第39轮。tag v0.39.0。

## [0.38.0] — 2026-09-03 · 第38轮 G25 落地：纯标准库表达式因子引擎（白名单DSL+时序/截面同引擎坐实training-serving parity）+ 因子正交/IC·ICIR加权治理（研究侧，不接main不改综合分，旧因子保持过程式原实现）
- **任务与定位**：G21（标准面板+注册表）、G21续（研究工具读面板）、G29（因子体检）三地基就位后，落地第37轮排定的第38轮首选 **G25**。此前每加一个因子都要写一段过程式代码、实时 analyzer 与离线 panel 各接一次，口径只靠"调用同一函数"口头保证，是 G2 插件化与 G16 浅 ML 的共同前置。本轮对标 Qlib Alpha158 表达式引擎、gplearn/AlphaGen 算子体系（**只借算子思想、不引依赖、不让自动挖掘直接上线**），落地一个**白名单、无 eval/exec、无属性访问、无导入**的表达式 DSL，因子=表达式字符串+元数据，实时与离线调同一引擎。守三铁律与 G25 回退条款：**引擎先只承载新研究因子，旧技术/基本面因子保持原过程式实现、综合分逐字节不变，factor_expr 不被 main 实时链路 import**。
- **① 新增根模块 `factor_expr.py`（约480行，9组零网络自测）**：
  - **安全解析器（递归下降）**：词法只认数字/标识符/四则/括号/逗号；全局禁 `__`(dunder)、`;`(语句拼接)，非数字一部分的属性点 `.` 由分词器按非法字符拒绝，`import/eval/exec/lambda/globals/getattr` 等即便写成名字也只是"输入字段名"绝不执行、作为函数调用则过不了白名单。函数调用**必须命中算子白名单**，未知算子/错误元数/非常量窗口/括号不匹配一律 ExprError（21 个危险/畸形反向用例钉死）。窗口参数强制为**正整数字面量**（静态、杜绝数据相关的未来函数）。
  - **时序算子（尾窗、严格无未来）**：delay/delta/ts_sum/ts_mean/ts_std(样本标准差)/ts_min/ts_max/ts_rank(窗内平均秩→0..1)/ts_minmax/decay_linear(线性衰减加权 Qlib 同式)/corr(滚动Pearson，≥3对)，支持任意嵌套（delta(delta(...))）；单一递归求值器 `_eval_ts`，暖机/除零/零方差/非正log 一律 None 不崩。
  - **截面算子（同一时点跨品种）**：cross_rank(平均秩→0..1)/scale(Σ|w|=1)/zscore；时序算子在截面上下文、截面算子在时序上下文均显式报错，两套上下文共用同一棵 AST。
  - **因子治理（纯标准库自含、不 import tools）**：pearson/spearman(并列平均秩)、高斯消元 solve、**orthogonalize 正交残差**（target 对多基 OLS 估 β、返回残差与 β，共线/样本不足安全降级）、equal/ic(|IC|归一保留方向)/icir(滚动IC均值/标准差)三套权重与逐点加权 combine。
  - **表达式因子库 LIBRARY**：5 个 research 因子（expr_ma_bias5=等价ret5作parity基准、expr_ma_ratio短长均线比、expr_trend_per_vol单位波动趋势、expr_price_accel二阶加速度、expr_illiq非流动性代理），同步登记进 factors_catalog（20→25条，layer=表达式研究、status=research、不进综合分），validate 仍零问题。
- **② 新增研究工具 `tools/expr_research.py`（约290行，5组零网络/零DB自测）**：纯离线只读 G21 面板，证明三件事并出 reports/expr_research.txt/.json（allow_nan=False）：
  - **training-serving parity**：(a) 面板列直读（离线）vs `panel_rows_to_bars` 回读成 bar 再取 c/v（实时链路拿到的形状）喂**同一条表达式**；(b) 表达式版5日动量 `delta(close,5)/delay(close,5)` 对齐面板里实时管线 compute_indicators 落库的 ret5 列（真正把引擎接到线上指标口径）。
  - **前向 RankIC 体检（G29式、严格只向未来取收益）**：每个表达式因子算对未来 H=1/5/20 交易日收益的逐品种均值 IC 与全样本池化 IC；截面 cross_rank 跨品种排序演示。
- **真实数据诚实结论（全64品种面板61353行）**：①**parity 完美**——面板列 vs bar回读同表达式 **303309 个有限点 maxAbsDiff=0.000e+00、零不一致**；表达式动量 vs 实时 ret5 **61033 比对点 maxAbsDiff=1.11e-16（纯浮点ε）**，training-serving parity 在全市场坐实；②**5个表达式因子前向 |IC| 全部<0.06、无稳定预测力**（最强 expr_ma_ratio 的 H=5 逐品种均值+0.056 但池化仅+0.029，其余在0附近且 H=20 多转负），与 tsmom/xsmom 双样本证伪、G29日频九因子|IC|<0.10 完全一致——简单价量表达式在4年池化样本上同样不构成边际，**全部维持 research、不挂影子不进分**（负结果诚实）。
- **验证**：新增 tests/test_factor_expr.py（37个零网络用例：21安全反向参数化+逐算子手算+无未来扰动+截面+OLS恢复β+加权合成+结构性parity+因子库必登记），test_tools_selftest 纳 factor_expr/expr_research（+2），test_compileall 随2个新生产py自动+2；全量 **pytest 397→438 全绿（约6.7s、30个测试文件）0失败0错误0跳过**；factor_expr/expr_research/factors_catalog 的 --selftest 全过、compileall 通过；确认仅 expr_research 引用 factor_expr、main/analyzer 实时主链零 import；真实 main --once 一轮冲烟证明主链与综合分零改动。规模：生产 py 54→**56（根42+tools14）、23178→24226 行**；测试 29→30 文件；运行依赖仍仅 requests/uiautomation/websocket-client。总纲 G25 标注本轮落地、6.3 排期补第38轮。tag v0.38.0。

## [0.37.0] — 2026-09-03 · 第37轮 G21续（研究工具统一读标准面板）+ G29 因子体检（滚动IC/块自助/失效预警/IC衰减半衰期，研究侧纯标准库，不接main不改综合分）
- **任务与定位**：分两段。**G21续**补齐第36轮 G21 刻意留下的最后一条验收——把 tsmom/xsmom/carry 三个真正消费日K的研究工具从"各自联网现拉→内部复权"改为可选读 G21 标准面板，并以"改前改后逐值一致"回归钉死；**G29** 承接第35轮归因发现的"机构动向次日负贡献(t−2.77/IC−0.203)"，给每个因子一张可复算的"体检卡"（现在还有没有力、稳不稳、衰减多快）。两段均守三铁律：纯标准库零新增依赖、研究/监控记录层、**不接 main、不改交易、不改综合分**；新能力默认开关、缺省等价旧版。
- **G21续① 面板回读/统一装载层（`tools/panel_builder.py`）**：新增 `panel_rows_to_bars(rows)`（面板行→bar-dict：d/o/h/l/c/v、p=oi）与 `load_adjusted_bars(code,days,prefer_panel=False)`（prefer_panel 且库内有→回读**已复权**bar、source="panel"；否则走旧网络路径 fetch[-days:]→ratio_adjusted_bars、source="network"）。**实证踩坑并定解法**：面板存的是**已复权**OHLC，若回读后再喂一次 `backtest.ratio_adjusted_bars` 会因"首次复权把真换月跳空置0→全序列MAD变小→换月阈值降低"把真实大波动**二次误判为换月**（联网实测 SC 价位偏6.31%、J 偏12.7%，换月计数31→3/12→2）；故面板路径**禁止二次复权**，直接返回已复权bar。selftest 7→8 组（新增"回读==建面板时已复权c / 回读再复权 roll=0 且价位不变 / 临时库面板路径==网络复权末值"）。
- **G21续② 三工具 `--panel` 开关（缺省网络路径逐字节不变）**：tsmom_eval 抽 `records_from_adjusted`、xsmom_eval 抽 `points_from_adjusted`、carry_eval 抽 `carry_points_from_adjusted`——把"复权之后的纯计算"与"取数+复权"分离；公开 build_symbol_records/build_symbol_points/build_carry_points 保持"网络现拉→ratio_adjust→内部纯函数"的旧路径不变，`--panel` 时 worker 用 load_adjusted_bars 取已复权bar直调内部纯函数（carry 的期限序列仍走 term_history，面板只替代主连价）。
- **G21续③ 全64品种面板补齐 + 等价回归**：面板从46品种补到**全市场64品种/61353行、0失败、区间2022-06-28~2026-09-02、累计换月处理**（多数1014行，新品种按上市日自然较短）。**真实联网等价实证（非合成）**：RB/MA/CU/SC/J/AU/M/TA 八品种同 days=1023，xsmom 6096 个、tsmom 5616 个重叠 (品种,日) 点，面板路径 vs 网络路径 z/ret/fwd **maxAbsDiff=0.000e+00、零 None 不一致**（面板仅少 PANEL_WARMUP-1=9 根暖机bar，不影响输出）；carry `--panel` 四品种真实 exit0。零网络回归：test_research_panel 14→18（面板行回读roundtrip、xsmom/tsmom 面板路径==网络路径、load_adjusted_bars 面板源且再复权 roll=0）。
- **G29① 新增 `tools/factor_health.py`（约430行，8组零网络/零DB自测），两层**：
  - **事件层**（复用 attribution.load_events 只读 monitor.db，方向化暴露 x=part×dir，三周期30/120/1440）：每因子整体 **RankIC**；**滚动IC**（窗60/步20事件）统计弱窗(|IC|<0.03)/翻窗(与整体异号)/最长连续失效窗，连续≥3=**失效预警**；**block bootstrap CI**（块长20保留时序自相关、500次、确定性种子，给 p5/p50/p95 与同号概率）；裁决 健康 / **健康(反向)**（稳定非零但IC为负=反转信号）/ 走弱·不稳定 / 失效预警 / 样本不足，"健康"要求 **CI 朝0保守边也越过0.03 且同号率≥0.95**（防纯噪声小样本被误判健康，合成噪声用例钉死）；另给多/空 × 轻仓/分批/强信号的 regime 代理 IC 矩阵。
  - **日频层**（读 G21 面板，已PIT/复权）：对 ret5/20/63/126/252、tsmom63/126/252/blend 九个日频因子，算未来 H=1/2/3/5/10/20/40/60 交易日的**池化 RankIC 与 Q5-Q1 价差**（前向收益严格只向未来取、无未来函数），并拟合 **IC(H)=A·exp(−H/τ) 的指数半衰期 τ·ln2**（|IC|>1e-4 且单调衰减、≥3有效点才给，不衰减/增强如实返回 None，不编造）。输出 reports/factor_health.txt + .json（allow_nan=False）。
- **G29② 体检卡回写特征注册表**：`factors_catalog.py` 增静态 `HEALTH_SNAPSHOT`（asof/方法/9个part的ic·CI·裁决·note + 日频层总结）与 `get_health(key)`，validate 钉死"体检卡只能回写已登记因子"、selftest 锁定机构动向反向结论；config 增 HEALTH_* 常量簇。
- **G29 真实数据诚实结论（monitor.db 2026-08以来事件 + 全64面板4.1年）**：①**机构动向次日 RankIC=−0.230、CI[−0.341,−0.118] 不跨零、同号率1.00，裁决"健康(反向)"**——统计上稳定显著但方向为负，与第35轮 t=−2.77/IC−0.203 两轮互证：当前方向化口径下它是**反转信号而非确认信号**（多头侧IC−0.297，轻仓/分批/强信号各档一致为负），后续应研究反向用或降权，本轮不改线上；②**原油联动次日 IC=+0.276、CI[+0.045,+0.407] 健康**（同号0.98、强信号档IC+0.584），基本面在2小时周期健康(+0.123)、次日跨零；③30分钟短周期多数 part 为失效预警/走弱（样本噪声大、滚动翻转多），盘中动量/量仓资金样本不足(n=9)；④**日频层九个单因子对未来1~60日池化 |IC| 均<0.10、多数随 H 变号不构成单调衰减**——单独日频回看收益在4年池化样本上无稳定横截面预测力，与 tsmom/xsmom 双样本证伪一致（负结果诚实呈现）。
- **验证**：新增 factor_health 自测注册进 test_tools_selftest（+1）、test_compileall 随新生产 py 自动+1，test_research_panel +4；全量 **pytest 391→397 全绿（约4.7s、29个测试文件）0失败0错误**；panel_builder/三研究工具/factor_health/factors_catalog 的 --selftest 全过；真实 main --once 一轮冲烟证明主链与综合分零改动。规模：生产 py 53→**54（根41+tools13）、22603→23178 行**；测试仍29文件（+conftest30）、397用例/4338行；运行依赖仍仅 requests/uiautomation/websocket-client。两次提交（G21续 9dca232 / G29），总纲 G21续标注完成、G29 落地、6.3 排期补第37轮。tag v0.37.0。

## [0.36.0] — 2026-09-03 · 第36轮 G21 落地：标准研究面板层 + 特征注册表 + PIT/训练-服务一致性校验（研究侧地基，独立缓存库不碰生产表/不接main/不改综合分）
- **任务与定位**：第33轮规划的剩余 P1 地基项 **G21**（G22/G23/G28 已在34/35轮落地）。此前 tsmom/xsmom/factor/carry/attribution 各研究工具每次各自联网拉数、各自造"品种×交易日"面板，口径只靠"共用同一函数"口头保证；因子定义散在 analyzer/fundamental_factors/cross_section，无一处登记；build_ml_samples 的 PIT 审计是一次性脚本、非常驻。本轮对标 Qlib 数据层/表达式与 Feast/Tecton 的 offline-online parity、point-in-time as-of join，落地三件套，守三铁律：纯标准库零新增依赖、研究侧先行、**面板用独立 SQLite（cache/research_panel.db，gitignore、删文件即回退）不碰生产 monitor.db 表结构、不接 main、不改综合分**。
- **① 新增根模块 `factors_catalog.py`（195行，特征/因子唯一登记处，纯数据零行为）**：登记 20 条因子（综合分9个part live、基本面4子项、4个技术指标原料、tsmom影子/xsmom已归档/carry待跟踪），每条含 key/中文名/层级/方向(+1/-1/0)/对综合分贡献界/status(live/shadow/research/tracking/archived)/引入轮次/实时计算处/公式/研究档案指针；`validate()` 自检（key唯一、字段齐全、方向·状态合法、9个part必为live）、动态原油键"原油联动(w=..)"归一、`catalog_text()`。测试钉死 **PART_KEYS 与 config.ATTR_FACTOR_ORDER 逐字一致**，后续 G25 表达式引擎/G29 体检按 key 回写。
- **② 新增研究工具 `tools/panel_builder.py`（430行，7组零网络/零DB自测）**：把"主连比例复权行情 + futures_data 技术指标 + 基本面快照"统一成标准长表（品种×交易日×30字段：标识+OHLC/量/持仓OI+ret1d+17个compute_indicators扁平标量+3个基本面PIT字段）。**两条硬口径（合成断言钉死）**：(a) **PIT 无未来函数**——第 t 行每个特征只用 bars[:t+1] 经实时同款 `futures_data.compute_indicators` 计算，基本面**严格取 trade_date<当日** 的最近一条（日频基本面收盘后才生成，用当日即偷看未来），as-of 对齐、取不到为 NULL 绝不编造；(b) **训练-服务一致性**——逐日复算与实时 analyzer 同一函数。`PanelStore` 独立 SQLite，整品种 DELETE+INSERT 事务**幂等重建两次逐值一致**、主键(sym,date)去重、research_runs 落 manifest（品种/区间/行数/源/复权/换月次数/字段/时间）。复用 backtest.ratio_adjusted_bars/resolve_codes、futures_data，不重造轮子；config 增 PANEL_* 常量簇（库路径/默认1023日/暖机10/17特征键/抽样24/manifest路径）。
- **③ 新增研究工具 `tools/pit_audit.py`（291行，通用审计器，5组自测）**：①时间戳 as-of 泄漏扫描 `timestamp_leaks/asof_join_check`（统计 feature_ts>event_ts、缺失跳过）；②**结构性无未来函数扰动法** `assert_no_future`（篡改某bar之后全部价格、重算该bar行必须逐值不变，含"故意偷看最后一根"的反向用例确保能抓到泄漏）；③**训练-服务一致性 parity** `parity_one/parity_for_symbol`（面板行 == 对同一bar前缀走实时 compute_indicators，均匀抽样逐字段比）；④`audit_panel_db` 缓存面板结构审计（每品种日期唯一严格递增、ret1d 可由相邻收盘价复算自洽、无 NaN/Inf 落库）。
- **真实数据验证（联网，非合成）**：构建覆盖五大板块 11 品种（RB/I/J 黑色、MA/TA/SC 能化、CU/AL 有色、M/Y 农产品、AU 贵金属）各1014行=**11154行、0失败、区间2022-07-01~2026-09-02（约4.1年与研究主样本对齐）、累计换月跳空处理86次**；`pit_audit --db` 对真实缓存**结构审计全过**（日期唯一递增/ret1d自洽/无NaN）；`pit_audit --codes RB0,MA0,CU0` 联网**实时/离线 parity 每品种25抽样时点全部 OK 零不一致**；RB 真实**幂等重建**：重建前后均1014行、库总数稳定11154不翻倍、回读与内存逐值一致；真实基本面 PIT as-of 生效（如 RB 末日取到前一日 fund_score）。
- **验证**：新增 tests/test_research_panel.py（14个零网络用例：注册表一致性/唯一/动态键、asof边界、基本面严格asof、暖机ret1d手算、列覆盖、扰动无未来+反向泄漏、时间戳扫描、parity一致+注入检出、PanelStore幂等回读、结构审计干净/破坏检出）；test_tools_selftest 纳 factors_catalog/panel_builder/pit_audit（+3），test_compileall 随3个新生产py自动+3；全量 **pytest 371→391 全绿（约4.7s、29个测试文件）0失败0错误**；三模块 --selftest 全过、compileall 全树通过；真实 `main.py --once --no-launch` 冲烟（见下）证明主链与综合分零改动。规模：生产 py 50→**53（根41+tools12）、21672→22603 行**；测试 28→29 文件/3997→4267 行；运行依赖仍仅 requests/uiautomation/websocket-client。**本轮刻意不把 tsmom/xsmom/factor/carry 改读面板（避免大改既有研究工具），仅证明面板与实时同函数逐值一致；既有研究工具切换读面板留作下一轮增量**。总纲 G21 标注本轮落地、6.3 排期补第36轮。tag v0.36.0。

## [0.35.0] — 2026-09-03 · 第35轮 G28 落地：因子收益归因 + BHB 板块归因（复盘"钱是谁赚的"，研究/复盘侧纯标准库，加法归因与BHB恒等式严格闭合，不接main不改综合分）
- **任务与定位**：第33轮全网对标把"复盘归因"列为五环节最后短板（综合分由9个part相加、信号结果早已落库 signal_outcomes，却从没回答盈亏该记到哪个因子/板块头上）。本轮落地 P1 高性价比项 **G28**，守三铁律：纯标准库零新增依赖、只读 monitor.db（mode=ro 绝不写库）、研究/复盘侧先行、不接 main 常驻、不改任何线上权重。
- **新增研究工具 `tools/attribution.py`（654行，8组零网络/零DB自测）**，两层教科书归因 + 累计曲线：
  - **多因子加法归因（带截距OLS）**：样本=signals.parts_json ⨝ signal_outcomes 的有效事件（hit/miss/flat）；方向化暴露 x=part×信号方向（meta-labeling，与 factor_eval 完全一致，做空时正part为负暴露），y=方向收益（storage 口径 dir×(评估价/入场价−1)）；正规方程+高斯消元（复用 tsmom_eval._solve），零方差/共线列自动剔除、奇异安全降级；**恒等式 mean(y)=α+Σβ_k·mean(x_k) 严格闭合（真实数据闭合误差≈1e-19）**，即把平均盈亏逐笔记到9因子与残差α；另给每因子 β 的 t 值（(X'X)^-1对角）、IC、"支持时胜率/均收 vs 反对均收"、IS70/OOS30 的β方向一致性（防过拟合）。
  - **BHB 板块归因（Brinson-Hood-Beebower 1986，CFA三效应）**：组合P=实际信号（板块事件占比 w_p、板块内方向化均收 R_p），基准B=全市场64品种板块只数占比 w_b（在有事件板块集内归一）×板块"无方向"平均绝对涨跌 R_b（=y/dir）；配置 AR=Σ(wp−wb)Rb、选择 SR=Σwb(Rp−Rb)、交互 IR=Σ(wp−wb)(Rp−Rb)，**恒等式 AR+SR+IR=R_p−R_b 严格闭合（200组随机fuzz恒等）**；另出评估月月度三效应序列。
  - **累计归因曲线**：事件按时间排序逐笔累计总收益/残差/各因子β·x（末端严格闭合=Σy），落 reports/attribution_curve.csv 供后续看板（本轮不接 charts）。输出 reports/attribution.txt + .json（sidecar 无 NaN、allow_nan=False）。复用 factor_eval.pearson/spearman/_canon（动态"原油联动(w=..)"归一）不重造轮子。config 增 ATTR_* 常量簇（三周期/主周期次日/最小样本40/OOS30%/支持阈值0.05/9因子规范序/三输出路径）。
- **真实数据诚实结论（monitor.db：30分n=370、2时n=368、次日n=235，均为2026-08以来实盘信号事件）**：
  - **因子层面**：①短周期靠**技术共振**——30分钟它贡献+0.123%（β t=2.35、IC0.095，是平均收益+0.022%的主来源）、2小时+0.230%（t=3.69、IC0.154，平均+0.061%主来源），新闻消息面2小时也显著（t=3.44）；②**次日（主周期）靠原油联动**：贡献+0.341%占平均收益+0.350%的97%（β t=6.04、IC0.393、因子支持时胜率70.1%、支持均收+1.148% vs 反对−0.015%，n=98个油系事件），技术共振次之+0.132%；③**机构动向次日为负贡献−0.126%（t=−2.77、IC=−0.203，支持时均收仅+0.039%、反对反而+0.765%）**——与第28轮 factor_eval"机构因子预测力弱"互证，是后续因子体检(G29)的重点复核对象；日线动量暴露最大(均值≈4)但β≈0，只做门槛不贡献边际收益；④OLS R² 0.027/0.078/0.193（次日最高），线性可解释部分有限、残差α在短周期为负、次日为正；⑤**IS/OOS β方向一致仅 4~5/8**，多数因子贡献在样本外翻转，因样本仅约1个月（自2026-08起），现阶段结论用于"定位结构"而非调权重。
  - **板块层面（BHB）**：三周期一致显示**选择效应远大于配置效应**（配置AR≈0）——系统的超额主要来自板块内的方向/择时，而非把信号押到板块结构更优处；次日选择+0.113%/配置+0.082%/交互−0.041%=超额+0.153%，农产品板块内选择为正(+0.199%)、能化为负(−0.163%)。分组交叉验证：30分/2时空头均收(+0.15%/+0.16%)略强多头、次日多头(+0.399%)强于空头(+0.168%)；轻仓档短周期为负、强信号/分批档为正，与信号校准器方向一致。
  - **诚实边界**：事件条件样本（只在|分|≥2发信号时观测、非连续组合、时段品种有偏）；OLS为线性加法、不刻画交互/非线性、β是相关非因果；BHB基准是事件条件下板块无方向均涨、非逐日连续基准；**本轮不改任何线上权重，仅产出可复算的复盘归因**。
- **验证**：新增 tests/test_attribution.py（16个零网络用例：方向化暴露/动态键归一/坏行、OLS恢复与奇异、加法闭合与零方差剔除/空样本、BHB手算两板块+200组fuzz恒等、板块统计rb无方向、基准归一、累计曲线末端闭合、IS-OOS有序、端到端报告与sidecar JSON安全）；test_tools_selftest 纳 attribution（+1），test_compileall 随新生产文件自动+1；全量 **pytest 353→371 全绿（约4.6s、28个测试文件）0失败0错误**，两渠道 --selftest 全过、compileall 全树通过；真实 monitor.db 三周期归因 exit0、数字可复现、闭合误差≈1e-19；真实 `main.py --once --no-launch` 一轮完成 exit0、ERROR/Traceback=0、分钟K 320任务空/失败0、数据源全成，证明主链与综合分零改动。规模：生产 py 49→**50（根40+tools10）、21002→21672 行**；测试 27→28 文件/3997 行；运行依赖仍仅 requests/uiautomation/websocket-client。总纲 G28 标注本轮落地、6.3 排期补第35轮。tag v0.35.0。

## [0.34.0] — 2026-09-03 · 第34轮 G22+G23 落地：多合约期限结构/OI/近月连续数据底座 + 商品carry截面双样本（两口径对照：主连价格方向为负、近月含展期长样本t=2.55成立但近窗边缘→不进分归档待跟踪）
- **任务**：扫描总纲未完成项并按优先级排队后，落地第33轮规划的 P1 首选链 G22（期限结构/OI 数据底座）→ G23（carry 截面双样本）。纯标准库零新增依赖，研究侧先行、不接入 main 常驻、不改综合分（守三铁律）。
- **G22 新增根模块 `term_history.py`（469行，8组零网络自测）**：①实测确认新浪日K四所统一"大写品种+4位年月"可取任意已摘牌合约完整生命周期（字段 p=持仓量、s=结算价，郑商所三位年 TA501 取不到、须 TA2501）；②月份枚举/`select_curve`（换月缓冲剔除临交割与无量合约、选近/次/远月）/`annual_carry`（与 contracts.term_structure 同号）/Nelson-Siegel level·slope·curv 固定载荷（期限结构 PCA 三主成分的零依赖等权近似）/`build_term_series` 重建逐日历史期限结构与总持仓 OI；③**`near_roll_nav` 近月连续净值（同近月内吃结算价变动以保留展期 roll、换月不跨合约计盈亏）——这是学术 carry 的正确收益口径**；④`TermHistoryStore` SQLite 逐合约缓存（cache/term_history.db，gitignore、增量跳过、空合约标记、多线程下载）。已真实缓存全64品种约5千合约/数十万根K线。**未做（下轮续）**：涨跌停板幅 tradable_mask（本轮只做交割/流动性筛选）、季节交割日历、常驻增量采集、与 G21 面板对接。
- **G23 新增研究工具 `tools/carry_eval.py`（588行，5组自测）**：复用 xsmom_eval 的截面分档/绩效/板块/IS-OOS/双样本 robust_verdict（含"长窗期数≥短窗×1.5"防伪）全套；6 因子族 carry/carry_nn/carry_mom/slope/curv/doi；**刻意做两套收益口径对照**以排除"主连复权抹掉 roll"的方法瑕疵。真实64品种、近4.1年(1023交易日)+长9.9年(2500)双样本、真实成本（单腿往返3e-4、两腿6e-4）：
  - **口径A 主连比例复权（不含roll，测价格方向）：carry 截面多空为负**——近窗净 t=-1.52、净均-0.82%/20日、最深Back档(Q5)未来20日反而最差(-1.06%)、RankIC=-0.3，carry_mom/slope/curv 同为负或不显著；即 carry 对未来**价格方向**无正向预测、近4年甚至偏反转/拥挤。
  - **口径B 近月连续含roll（学术carry全收益）：长9.9年净 t=+2.55、净均+1.21%/20日、年化+12.4%、夏普0.68、胜率62%、长窗不衰减反增强（95期≥短窗52×1.5，非同源小样本）；近4.1年净 t=+1.39（差0.11未达门槛1.5）**；carry_nn 近月 t=+1.45。
  - **机制结论（本轮最有价值的发现）：carry 赚的是展期 roll 的钱、不是价格方向择时的钱**——主连把换月 roll 抹掉后信号消失甚至反向。按三铁律，近窗 t=1.39 未达1.5，**不进综合分、不挂实时影子**，归档为"长样本成立/近窗边际减弱"的**待跟踪候选**（性质区别于 TSMOM/XSMOM 双样本全负的彻底证伪）；有色板块内主连口径 t=2.33/1.85 但被同源小样本判据拦下（氧化铝/碳酸锂2024才上市），留待样本积累。报告 reports/carry_eval.txt/.json。
- **验证**：新增 tests/test_term_history.py（8用例）、test_tools_selftest 加 carry_eval；pytest **342→353 全绿（约4.6s）**；真实 `main.py --once --no-launch` 一轮完成、ERROR/Traceback=0、分钟K空/失败0、DB 正常；两模块 --selftest 全过。规模：生产 py 47→**49（根40+tools9）、19945→21002 行**；运行依赖仍仅 requests/uiautomation/websocket-client。总纲 G22/G23 标注本轮进展、6.3 排期补第34轮。tag v0.34.0。

## [0.33.0] — 2026-09-03 · 第33轮 AI量化对标补路（全网+GitHub 2026 调研 × 数据/因子/模拟/复盘五环节，新增 G21–G30 入总纲，纯文档零改生产代码）
- **任务与方法**：重做全网+GitHub AI量化对标（2026-09），聚焦用户点名的五环节——数据收集、数据整理分析、因子生成、模拟数据整合分析、结束后的复盘；先以磁盘代码为准盘点现状（futures_data/intraday_bars/data_router/data_health 采集治理、analyzer/factors/fundamental_factors/cross_section/iv_surface 因子、backtest/intraday_backtest/portfolio/paper_broker 模拟、metrics/signal_calibrator/backtest_validation 复盘），再与 Qlib/RD-Agent/FactorEngine/vectorbt/pyfolio 及商品期货学术九因子、carry 期限结构、PIT 特征仓库、风险平价/BHB 归因/IC 衰减等对标，形成 10 个 G1–G20 未覆盖的改进项 G21–G30，按优先级补进《总体对标与统一改进总纲（融合版）.md》（404→517 行）。
- **关键对标结论（决定后续路线）**：①**carry/展期收益是商品期货最持久稳健的 alpha、强于已被证伪的动量**（湘财证券因子筛选、清华三因子、高盛、155年商品收益分解、Bayes-CID 2026 GBDT 九族一致）；项目 fundamental_factors 已有 carry/basis 但只是**单品种当前时点 tanh 打分、从未截面化与双样本检验**——这是动量证伪后的首选方向（G23）。②学术九族里项目尚缺 basis momentum、持仓量(OI)变化、Amihud 非流动性、特异波动、偏度、套保/投机压力、季节/交割日历（G22 采数、G24 因子）。③数据层缺统一研究面板/特征注册表与 point-in-time、训练-服务一致性的**常驻**校验（现 audit_pit 仅一次性）→G21。④组合层只有等权/固定/信号三 sizing，缺风险平价 ERC/最小方差/目标波动（纯标准库实现，默认旧等权）→G26；实验登记分散→G27。⑤复盘层说不清"盈亏由哪个因子/板块贡献"，缺多因子归因+BHB 板块归因（G28，P1 高性价比）、IC 衰减半衰期与因子失效预警（G29）、交易 journal 与一键日/周复盘（G30）。⑥明确**不做**：不引 numba/vectorbt/numpy/pandas 重依赖（守零依赖，只学其实验组织）、不让 LLM/遗传规划自动挖的因子直接上线（仍须双样本+体检、受 G13/G16 门控）。
- **新增 G21–G30（优先级与依赖已写入总纲 3.5/一页纸总览/回退表/排期/7.5 来源）**：**P1**=G21 标准研究面板+特征注册表+PIT/训练-服务一致性校验、G22 多合约期限结构+持仓量连续历史+可交易性掩码、G23 carry/期限结构因子截面化双样本硬检验（复用 xsmom_eval 框架与 robust_verdict）、G28 因子收益归因+BHB 板块归因；**P2**=G24 微结构/持仓/季节因子族、G25 纯标准库表达式因子引擎+因子正交/IC加权（G16 前置）、G26 组合构建器（ERC/GMV/目标波动，默认等权）、G27 统一实验台账+walk-forward 稳定性/成本敏感性、G29 因子体检（IC 衰减半衰期/换手衰减/滚动失效预警/regime 分层）、G30 交易复盘 journal+纸面-回测一致性+一键日/周复盘。依赖：G22→G23、G21→G25→G16、G28/G29/G30 并行、G26 宜在 G2 后。每项均给"现状事实→对标来源→文件级改法→验收→默认/回退"，全程守三铁律（纯标准库、研究/采集/报告侧先行、默认等价旧版、双样本稳健才谈影子、不自动进综合分）。
- **验证与边界**：本轮为**纯文档轮，零生产代码改动**（仍 47 个 py、19945 行）；全量 **pytest 342 全绿（约4.6s）** 确认无回归；总纲新增内容经回读逐项核对（总览表 10 行、3.5 全章、不做清单 15/16、6.2 回退行、6.3 排期两行、7.5 来源五组）。下一站第34轮=按 P1 落地 G22（期限结构/OI 采集）→G23（carry 截面双样本），或并行 G21/G28。tag v0.33.0。

## [0.32.0] — 2026-09-02 · 第32轮 G7 截面动量条件化（板块池/多头腿 × 近4年+10年双样本稳健硬检验：8候选无一达标，XSMOM 归档边缘效应）
- **背景与边界**：第31轮全市场无条件 XSMOM 弱正不达标（近4年 t=1.43 差一点、10年 t=1.37 反降、做空腿长样本转亏），留下三条可救线索：①板块内截面有色/农产品为正、能化反向；②长样本全靠多头腿；③显著性必须随样本量增大而稳定（否则是近4年 regime 偶然）。本轮把"条件化能否把边缘效应增强到稳健达标"做成**一次拉满、同源双样本（近4.1年 vs 长9.9年）× 8 个条件化候选的硬对照**，仍严守三铁律：纯离线、不碰 analyzer/cross_section 主链、不改综合分、缺省不带新参数时与第31轮行为等价（仅多第五章）。
- **`tools/xsmom_eval.py` 扩展（766→977 行，纯标准库）**：①`cross_section_periods` 增 `sector_scope`（成员收集时按板块集合过滤，池内等权基准 mkt 随之限定在池内）并逐期增 `long_excess=long-mkt`（只做多最强档但扣池内等权基准的纯选股 alpha，单腿成本）；②三种腿模式 `LEG_KEY`：`ls`=多空价差(两腿成本)、`lex`=多头超额(单腿)、`long`=纯多头(含 beta、单腿)，`perf_stats` 既有 `legs=2 if key=="ls" else 1` 直接兼容；③`truncate_dates` 取全局日历最近 N 个交易日（by_date 共享、不重建），实现"一次拉 2500 根、内存截最近 1023 得短样本"的**同源双样本**，两窗口因子值完全一致、只差区间；④`conditional_scan` 对 config 的 8 候选×双窗批量算净绩效，`robust_verdict` 双样本稳健判据=两窗净均>0 且净 t≥1.5、长窗 t 不比短窗衰减超 0.5、**且长窗非重叠期数≥短窗×1.5（XSMOM_LONG_N_RATIO，本轮新增的关键防伪判据：板块品种上市晚、长窗拿不到更长历史时两窗实为同源小样本，不算双样本）**；⑤`build_report` 增"五、条件化增强×双样本对照"章（每候选列两窗净均/t/夏普/期数与否决原因，缺省 robust_panel=None 时整章不输出=旧行为），主组合支持 `main_scope/main_leg`（--scope/--leg，缺省全市场/多空=旧行为）；⑥`run` 默认 --days=2500 拉满、--main-days=1023 截主窗，新增 --scope/--leg/--main-days/--long-n-ratio/--no-conditional；`--selftest` 10→**14 组**（板块池过滤+long_excess 手算+单/双腿成本、窗口截断、conditional_scan 结构、robust_verdict 达标/衰减/为负/同源小样本/窗口不足五分支、第五章端到端与缺省无第五章）。config 增 XSMOM_ROBUST_DAYS/COND_MIN_NAMES/DECAY_TOL/LONG_N_RATIO/COND_CANDIDATES(8候选) 常量簇。
- **真实 64 品种、59404 个(品种×交易日)点的诚实结论（条件化也救不回，关键负结果）**：**口径修正**——本轮拉满 2500 根再截最近 1023 交易日，z252 用足 252 日历史、主样本是真正 4.1 年（n=51 个非重叠期）；而第31轮直接拉 1023 根会扣掉 252 日暖机、实际只有最近约 3 年（n=37）。在这个**更正确的口径下全市场基线近4年净 t 只有 +0.45（而非第31轮暖机截断口径的 1.43）、净夏普 0.22、IS t=-0.06**，本身就证明"差一点达标"是样本窗口敏感的脆弱结果。8 候选×双样本（近4.1年/长9.9年净 t）：全市场多空基线 0.45/1.37、有色内多空 **2.01/2.01（两窗 n 都=25 完全相同）**、农产品内 0.79/1.95、有色+农产品池 1.21/1.50、**剔除能化 1.48/2.54（最接近）**、全市场多头超额 0.64/1.17、有色+农产品多头超额 1.18/0.53、全市场纯多头 0.19/1.73，**无一通过**。两个最像"有戏"的都被严格否决：①有色内 t=2.01 最强，但诊断发现有色要凑齐分5档所需的≥10 只（CU/AL/ZN/PB/NI/SN 六老 + BC + SI + 氧化铝AO/碳酸锂LC，后两者 2024-08 才上市）其有效截面历史仅约 2 年/25 期，近4年与10年窗口对它是**同一段小样本**（期数零增量），被 LONG_N_RATIO 判据拦下——t 高只是小样本+板块内每档仅2只的高集中度，不可外推；②"剔除能化"双窗都为正、长窗更强且样本充足，但近4年 t=1.48 仍差 0.02 到门槛，且"剔除能化"属事后按已知结果选板块（data snooping），不为此放宽门槛。多头超额/纯多头两候选双窗都弱，说明第31轮"长样本靠多头腿"只是 beta 现象，扣掉市场基准后选股 alpha 很薄。
- **裁决：❌ 8 个条件化候选（板块池/只做多/板块合并）全部不达标，截面动量 XSMOM 与时序动量 TSMOM 一并归档为"存在弱截面排序形态、但扣费后经济偏薄、显著性随区间与口径漂移、无可稳健交易子域"的边缘效应，不并入、不挂影子、不改综合分**。G7 动量因子探索到此闭环：国内商品期货上经典时序/截面动量在 2017–2026 样本内均不构成"确定不更差"的信号。工具与双样本报告（reports/xsmom_eval.txt/.json，一份报告同时含主样本四章+条件化第五章）留档可复算；最接近的"剔除能化·多空（近 t1.48/长 t2.54）"仅作未来观察项，若后续积累更多 OOS 且不再靠事后选板块，可再议。
- **验证**：tests/test_xsmom.py 15→**21 用例**（板块池+long_excess 手算、单/双腿成本、窗口截断不改原序列、robust_verdict 五分支含同源小样本、conditional_scan 结构与样本不足、第五章/主组合腿标注），test_tools_selftest 的 xsmom selftest 同步 14 组；全量 **pytest 336→342 全绿（约4.4s，26 个测试文件）**，compileall 全过；真实 64 品种 2500 根双样本评估 exit0、数字可复现，并额外写诊断脚本核实"有色两窗 n=25 同源"的根因（品种上市日期）；真实 `main --once` 冲烟 exit0：分钟K 320任务空/失败0、行情覆盖 64/64、缺数0/陈旧价0/跳变0、零 Traceback，主链与综合分零改动（本轮仅改离线研究工具+config）。生产仍 47 个 py、19718→19945 行（xsmom_eval 766→977），零新增运行依赖。下一站见上下文摘要"下一站"。

## [0.31.0] — 2026-09-02 · 第31轮 G7 截面动量多空 XSMOM（时序动量的截面替代：离线评估，长窗弱正但不达标，维持纯研究）
- **背景与边界**：第30轮时序动量 TSMOM 已证伪——品种内"自己过去预测自己未来"在国内商品近4年偏反转，pooled RankIC 的弱正主要来自跨品种混合的**截面成分**。本轮按上轮留档方向，把这一截面成分**纯净剥离**：在每个调仓日跨全部品种按长窗波动调整动量 z 排序，做多最强一档、做空最弱一档，构建**市场中性多空组合**（Jegadeesh-Titman / Asness-Moskowitz-Pedersen 截面动量口径），赚相对排序的钱、对冲全市场 beta。严守三铁律：本轮**只做离线评估，不碰 analyzer/cross_section 主链、不改综合分、不挂实时信号**（截面排序需全市场同时点数据，本就不适合挂在单品种分析行；证据不足前连影子都不加）。
- **新增研究工具 `tools/xsmom_eval.py`（离线、可联网拉日K、纯标准库零新增依赖，766 行）**：复用 backtest.resolve_codes/fetch_daily_kline/ratio_adjusted_bars（主连换月跳空置0比例复权）、futures_data.tsmom_series（排序因子 z{L}=ret÷窗口日收益std×√252，实时离线同一函数）、factor_eval.pearson/spearman。①`build_symbol_points/build_panel` 构造全市场(品种×交易日)面板，暖机后入面板、未来收益未成熟保留 None 由组合层过滤（**无未来函数**，合成断言钉死）；②`cross_section_periods` **非重叠调仓**（每 H 个交易日调一次、持有 H 日，期与期不重叠、可复利），当日按因子升序分 N 档（与 factor_eval 同切法、不重不漏），多最强档/空最弱档，支持**等权与反波动率加权 ivol**（AQR 口径，低波动权重高、波动率缺失安全退回等权），当日可得品种 < min_names 或 < 2N 档则跳过不硬凑；③`perf_stats` 给净多空的期数/毛净均收/**t=期均÷(期std/√n)**/胜率/复利累计/年化/夏普(√252/H)/最大回撤，净=毛-两腿往返成本（默认 fee 万0.5+滑点万1、两腿万6/期，可 CLI 覆盖）；④`bands_profile` 分档单调性+档位列序 Spearman+多空价差，两腿分别拆"多头最强档/做空最弱档/全市场等权基准"，并算多空与市场基准相关（验证是否真市场中性）；⑤IS70%/OOS30% 分段、日频重叠调仓稳健对照（明确标注重叠使 t 偏乐观仅参考）、原始 ret 因子对照；⑥`sector_breakdown/sector_internal` 板块条件化——多空腿板块净敞口（查单一板块偏置）+ 留一板块 LOSO + 板块内各自截面多空；⑦L∈{20,63,126,252}×H∈{5,20,60} 参数网格总表；⑧`gate_verdict` 截面专属"确定不更差"判据（净 t≥1.5、净均>0、OOS 不转负、分档单调≥75%且价差>0、至少一条腿方向对、单一板块净敞口≤60%）。产出 reports/xsmom_eval.txt + .json；`--selftest` 10 组零网络断言（远期收益无泄漏/分档不重不漏/等权·ivol 手算/强者恒强面板多空为正且单调=1/成本扣减/样本不足降级/IS-OOS 有序/绩效手算/裁决各否决门/报告端到端）。
- **真实 64 品种、45801 个(品种×交易日)点的诚实结论（弱正但不达标，关键边缘结果）**：近约 4.1 年（1023 根，对齐第30轮口径）主组合 z252/H20 毛多空价差 +1.01%/期、净 +0.95%、**净 t=+1.43（差一点点未过 1.5）**、净夏普 0.83、胜率 65%、累计 +37.9%、回撤 12%；分档 Q1..Q5=-0.41/-0.20/-0.35/-0.03/+0.60%（单调性 75%、档位列序 Spearman=0.90），**两腿方向都对**（多头 +0.60%、做空最弱档 +0.41%），多空与市场基准相关 -0.20（确实市场中性、没吃 beta）；但 IS 段很弱（t=0.54）、正收益主要靠 OOS（t=1.44、胜率75%）。参数网格仅长窗(126/252)为正、短窗(20/63)偏负（与第30轮"长窗才弱正、短窗反转"一致），没有一格 t≥1.5。**决定性稳健性——拉长到约 9.9 年（2500 根、112 个非重叠期，reports/xsmom_eval_long.txt）：净 t 不升反降到 +1.37、净夏普降到 0.46、回撤放大到 22.7%、做空腿转为亏损（最弱档绝对涨幅+0.16%，多空全靠多头腿）、IS/OOS 均仅 0.91/1.12**，说明近4年的 t=1.43 含 regime 偶然性。板块：全市场组合净敞口很分散（最大仅±16%、无单一板块偏置），但板块内截面多空**有色 +2.11%(10年+2.75%)、农产品 +1.07%(+0.88%) 为正、能源化工 -0.66%(-0.29%) 为负，2/3 板块为正**，跨两个样本稳健复现。
- **裁决：❌ 不并入、维持纯离线研究**。截面动量在国内商品上是"统计弱正、经济偏薄（扣费年化 6~12%、长样本回撤 20%+）、主要靠多头腿、能化板块反向、样本拉长后显著性不增强"的**边缘效应**，明显强于被证伪的时序动量（时序品种内是反转、截面至少长窗弱正且两腿/市场中性形态健康），但达不到"确定不更差"的稳健门槛；与"中国商品截面动量存在但弱于股指、时强时弱"的已知经验一致。工具与两份样本报告全部留档可复算，为后续"板块条件化（只在有色/农产品内）、结合持仓/库存、积累更多 OOS、多头腿-only 时序叠加"预留统一口径。
- **验证**：新增 tests/test_xsmom.py 15 个零网络用例（远期无泄漏/分档划分/等权·ivol 手算/非重叠步长/多空价差手算/成本与 t 手算/回撤/分档单调/IS-OOS/板块敞口与板块内/裁决六道否决门/暖机无未来/合成趋势面板端到端/空样本安全降级），test_tools_selftest 纳入 xsmom_eval +1；全量 **pytest 319→336 全绿（约4.4s，26 个测试文件）**，xsmom_eval `--selftest` 10 组全过、compileall 全过；真实 64 品种两档样本（1023/2500）均 exit0 产出报告且数字可复现；真实 `main --once` 冲烟主链与综合分零改动（本轮无任何生产主链文件改动）。config 增 XSMOM_* 常量簇（G10 深合并对 tuple 安全）。生产 46→47 个 py、18930→19718 行，零新增运行依赖。下一站见上下文摘要"下一站"。

## [0.30.0] — 2026-09-02 · 第30轮 G7 多窗口时序动量 TSMOM(63/126/252)（影子评估阶段：离线IC证伪、综合分零改动）
- **背景与边界**：analyzer 日线动量原仅 ret5/ret20 短窗（tanh(ret5×160)×2.5+tanh(ret20×70)×2.0+ma10偏离）。G7 拟补 1/3/6 月趋势；严守三铁律，本轮只做"离线先评估 + 生产侧影子记录"，**不并入综合分、不改任何线上权重、不动看板**，保留一键回退（TSMOM_SHADOW=False 即与本轮前逐字节等价）。
- **futures_data.py（纯增量、旧值逐字节不变）**：新增 `_lookback_return/_window_std/tsmom_at/tsmom_features/tsmom_series/_tsmom_empty` 纯函数（零网络纯标准库，实时与离线共用同一口径，杜绝两套算法）；ret{L}=过去L日累计收益，tsmom{L}=z=ret÷(过去L日日收益样本std×√252) 的波动调整趋势（跨品种量纲可比），blend=mean(tanh(clip(z,±3)))，历史不足/零波动一律 None 绝不编造；`compute_indicators` 在 **max_bars=140 截断之前**用完整序列计算（保证 ret252 有≥253根，且截断后旧 RSI/MACD/KDJ 输入序列逐字节不变——合成回归断言逐字段相等），新增 ret63/ret126/ret252/tsmom63/tsmom126/tsmom252/tsmom_blend/tsmom_n_valid 八个影子键；KlineCache 取数失败 fallback 同步补键。
- **analyzer.py（影子只记录不进分）**：开关 TSMOM_SHADOW（默认True）下把 tsmom_shadow 挂到分析行（随 signals.raw_json 自动落库做长期跟踪，不改表结构、不进 parts_json），**绝不加入 parts**；合成测试断言开关两态 score/parts 逐值相等。
- **新增研究工具 `tools/tsmom_eval.py`（离线、可联网拉日K、零新增依赖）**：复用 backtest.resolve_codes/fetch_daily_kline/ratio_adjusted_bars（主连换月跳空置0的比例复权）与 factor_eval 的 pearson/spearman/月度IC/ICIR/分档，构造全市场(品种×交易日) pooled 面板，输出 ①各窗口（原始ret/波动调整z/等权blend）×未来5/20/60日 的 IC/RankIC/ICIR 与期限衰减 ②分档单调性/多空价差/方向命中 ③窗口互相关矩阵 ④剔除现有ret5/ret20后的 OLS 残差边际增量（自带高斯消元多元OLS、奇异降级） ⑤IS70%估IC权重/OOS30%验证的等权vs IC加权合成 ⑥分品种一致性 + "确定不更差"自动裁决（TSMOM须品种内过半为正，防 pooled 截面伪相关）；产出 reports/tsmom_eval.txt + .json；`--selftest` 10 组零网络断言（远期收益无泄漏/OLS残差正交/持续趋势正IC·正弦半周期反转负IC/IS-OOS切分/伪相关裁决等）。
- **真实64品种、41961个(品种×交易日)、近约4年日K的诚实结论（关键负结果）**：pooled 层面 z252 对未来20日 RankIC 仅 +0.023、60日 +0.036（且随期限增强）、OOS +0.046、剔除短窗残差 +0.011（看似弱正、一度触发 pooled 判据）；**但加入分品种一致性后仅 9/64 品种为正、品种内中位 RankIC=-0.163，63/126 窗更弱（仅12%~19%品种为正、中位约 -0.13~-0.14）**——证明 pooled 的弱正主要来自跨品种价格水平混合的**截面伪相关**，真正的品种内时序动量在国内商品近4年不成立（品种内反而偏反转），等权三窗被63窗拖累亦无效（残差-0.015）。**故 63/126/252 三窗口及等权/IC加权合成全部裁决❌不并入、维持纯影子**；这正是"影子先行、确定不更差才采纳、负结果诚实"纪律避免把伪相关因子加进综合分的价值。留档事实：z252 与现有短窗相关性低（0.14/0.32，信息不重复）、三长窗彼此相关 0.58~0.73，为后续"截面多空排序/更长历史样本/结合持仓·库存/不同持有期"再评估预留统一口径。
- **验证**：新增 tests/test_tsmom.py 13 用例（纯函数手算/零波动与历史不足降级/序列对齐/ compute 旧 tech 逐字节回归 /影子开关 score 相等铁律/OLS正交/暖机无未来函数等），tools selftest 纳入 tsmom_eval +1、compileall +1，全量 **pytest 304→319 全绿（约3.6s，25 个测试文件）**，tsmom_eval `--selftest` 10 组全过；真实64品种 tsmom_eval exit0 并产出报告；真实 `main --once` 冲烟 exit0、主链与综合分零改动。config 增 TSMOM_* 常量簇（G10 深合并对 tuple 安全）。生产 45→46 个 py，零新增运行依赖。下一站见上下文摘要"下一站"。

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
