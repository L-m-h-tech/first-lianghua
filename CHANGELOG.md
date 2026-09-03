# 更新日志（CHANGELOG）

本项目按"轮"迭代，版本号 `主.轮.补丁`，与 `VERSION` 对齐；详细过程见 `上下文摘要.md`。
铁律：生产纯标准库 + 三个直接依赖；默认行为可回退；每轮合成断言 + 真实冒烟 + 负结果诚实呈现。

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
