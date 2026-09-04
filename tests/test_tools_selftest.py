# -*- coding: utf-8 -*-
"""把研究侧工具自带的零网络合成断言纳入 pytest（factor_eval/tsmom_eval/xsmom_eval/carry_eval/attribution/panel_builder/pit_audit/build_ml_samples/backtest_validation/db_archive，及根模块 factors_catalog）。"""
import factor_eval
import tsmom_eval
import xsmom_eval
import carry_eval
import attribution
import panel_builder
import pit_audit
import build_ml_samples
import backtest_validation
import db_archive
import factors_catalog
import factor_plugin
import factor_parts
import factor_legacy_expr
import factor_health
import factor_expr
import expr_research
import factor_regime
import microstructure_lab
import spread_lab
import spec_pressure_lab
import web_dashboard
import portfolio_constructor
import portfolio_lab
import trade_journal
import research_review
import experiment_ledger
import wf_cost_lab
import db_backup
import portfolio_risk
import portfolio_risk_lab
import circuit_breaker
import orthogonal_blend_oos
import tradable_mask


def test_factor_eval_selftest():
    assert factor_eval.selftest() == 0


def test_tsmom_eval_selftest():
    assert tsmom_eval.selftest() == 0


def test_xsmom_eval_selftest():
    assert xsmom_eval.selftest() == 0


def test_carry_eval_selftest():
    assert carry_eval.selftest() == 0


def test_build_ml_samples_selftest():
    assert build_ml_samples.selftest() == 0


def test_backtest_validation_selftest():
    # 模块内函数名为 _selftest（含 DSR/CSCV/PurgedKFold/WF/高原期 合成断言，内部自带 assert）
    backtest_validation._selftest()


def test_db_archive_selftest():
    db_archive._selftest()


def test_attribution_selftest():
    """G28（第35轮）因子收益归因+BHB板块归因 --selftest：零网络/零DB合成断言。"""
    assert attribution.selftest() == 0


def test_factors_catalog_selftest():
    """G21（第36轮）特征注册表 --selftest：登记完整/方向状态合法/动态键归一。"""
    assert factors_catalog.selftest() == 0


def test_factor_plugin_selftest():
    """G2（第57轮第一切片）插件宿主 --selftest：契约校验/注册表/异常隔离/catalog一致性/PART顺序 共8组。"""
    assert factor_plugin.selftest() == 0


def test_factor_parts_selftest():
    """G2（第58轮第二切片/第59轮第三切片）live part 适配器 --selftest：日线+其余7part 对真analyzer逐位parity 共10组。"""
    assert factor_parts.selftest() == 0


def test_factor_legacy_expr_selftest():
    """G25续（第59轮）旧因子过程式→表达式 --selftest：ret逐字节/SMA容差/日线动量tanh声明式逐位/无未来 共7组。"""
    assert factor_legacy_expr.selftest() == 0


def test_panel_builder_selftest():
    """G21（第36轮）标准研究面板 --selftest：PIT asof/未来扰动/训练服务一致/PanelStore幂等。"""
    assert panel_builder.selftest() == 0


def test_pit_audit_selftest():
    """G21（第36轮）PIT/训练-服务一致性审计 --selftest：泄漏扫描/扰动/parity/结构审计。"""
    assert pit_audit.selftest() == 0


def test_factor_health_selftest():
    """G29（第37轮）因子体检 --selftest：滚动IC/块自助/失效预警/日频IC衰减半衰期合成断言。"""
    assert factor_health.selftest() == 0


def test_factor_expr_selftest():
    """G25（第38轮）表达式引擎 --selftest：白名单安全解析/时序截面算子手算/正交加权/parity。"""
    assert factor_expr.selftest() == 0


def test_expr_research_selftest():
    """G25（第38轮）表达式研究台 --selftest：面板/bar parity、表达式==实时ret5、前向无未来。"""
    assert expr_research.selftest() == 0


def test_factor_regime_selftest():
    """G29续（第39轮）regime分层/换手/衰减形态 --selftest：PIT标签、分层IC、持续性、指数vs幂律择优。"""
    assert factor_regime.selftest() == 0


def test_microstructure_lab_selftest():
    """G24（第54轮）微结构/持仓/季节因子族 --selftest：ΔOI/Amihud/特异波动/偏度PIT、前向IC、日历季节 共8组。"""
    assert microstructure_lab.selftest() == 0


def test_spread_lab_selftest():
    """G12（第55/56轮）产业链/跨期价差+盘面利润 --selftest：尾窗z/分位、近-次价差形态、对齐比价、产业链z、盘面利润额系数 共10组。"""
    assert spread_lab.selftest() == 0


def test_spec_pressure_lab_selftest():
    """G24续（第57轮）投机/套保压力代理 --selftest：成交/持仓投机度、尾窗z/分位、量仓四象限、近月集中度自身分位换月信号 共9组。"""
    assert spec_pressure_lab.selftest() == 0


def test_web_dashboard_selftest():
    """G8（第57轮）只读Web看板 --selftest：safe_join穿越防护、content-type、方法白名单、首页转义/viewport 共6组。"""
    assert web_dashboard.selftest() == 0


def test_portfolio_constructor_selftest():
    """G26（第40轮）组合构建器 --selftest：等权/逆波动/ERC/长仓GMV、capped-simplex、目标波动、风险贡献/换手。"""
    assert portfolio_constructor.selftest() == 0


def test_portfolio_lab_selftest():
    """G26（第40轮）组合实验台 --selftest：稠密面板/固定宇宙/滚动样本外无未来且GMV波动≤等权/快照合法。"""
    assert portfolio_lab.selftest() == 0


def test_trade_journal_selftest():
    """G30（第42轮）交易复盘journal --selftest：分桶手算/信号强度绝对值/日周聚合/盘中MFE-MAE多空镜像/空数据安全。"""
    assert trade_journal.selftest() == 0


def test_research_review_selftest():
    """G30③（第43轮）研究侧一键复盘编排器 --selftest：sidecar安全装载/新鲜度/equity-BOM/信号正则/各段提取/规则待办/空目录降级。"""
    assert research_review.selftest() == 0


def test_experiment_ledger_selftest():
    """G27①（第44轮）统一实验台账 --selftest：规范哈希/数据身份排mtime/重复串联/坏行宽容/safe_record吞错/CLI。"""
    assert experiment_ledger.selftest() == 0


def test_wf_cost_lab_selftest():
    """G27②③（第45轮）WF参数稳定性+成本曲面/换手容量 --selftest：零网络/零DB合成断言。"""
    assert wf_cost_lab.selftest() == 0


def test_db_backup_selftest():
    """G19（第46轮）sqlite在线热备/滚动保留/恢复/任务XML/bat --selftest：tmp造库零网络零生产库。"""
    assert db_backup.selftest() == 0


def test_portfolio_risk_selftest():
    """G5（第47轮）组合风险纯函数：相关矩阵/历史&参数VaR/ES/原油beta压力 11组零网络自测。"""
    assert portfolio_risk.selftest() == 0


def test_portfolio_risk_lab_selftest():
    """G5（第47轮）组合风险实验台：窗口切片/超长窗取全/四方案渲染不崩 3组自测。"""
    assert portfolio_risk_lab.selftest() == 0


def test_circuit_breaker_selftest():
    """G5④（第48轮）组合层单日浮亏熔断：日切/粘性/动作模式/委托过滤 11组零网络自测。"""
    assert circuit_breaker.selftest() == 0


def test_tradable_mask_selftest():
    """G22续（第64轮）可交易性掩码 --selftest：锁板判别手算/交割天数/合成面板掩码/汇总计数/报告结构 共5组。"""
    assert tradable_mask.selftest() == 0


def test_orthogonal_blend_oos_selftest():
    """G25续/G16前置（第61轮）正交IC接真实面板样本外 --selftest：截面秩/三角残差化/walk-forward无未来。"""
    assert orthogonal_blend_oos.selftest() == 0
