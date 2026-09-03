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


def test_panel_builder_selftest():
    """G21（第36轮）标准研究面板 --selftest：PIT asof/未来扰动/训练服务一致/PanelStore幂等。"""
    assert panel_builder.selftest() == 0


def test_pit_audit_selftest():
    """G21（第36轮）PIT/训练-服务一致性审计 --selftest：泄漏扫描/扰动/parity/结构审计。"""
    assert pit_audit.selftest() == 0
