# -*- coding: utf-8 -*-
"""把研究侧工具自带的零网络合成断言纳入 pytest（factor_eval/build_ml_samples/backtest_validation/db_archive）。"""
import factor_eval
import build_ml_samples
import backtest_validation
import db_archive


def test_factor_eval_selftest():
    assert factor_eval.selftest() == 0


def test_build_ml_samples_selftest():
    assert build_ml_samples.selftest() == 0


def test_backtest_validation_selftest():
    # 模块内函数名为 _selftest（含 DSR/CSCV/PurgedKFold/WF/高原期 合成断言，内部自带 assert）
    backtest_validation._selftest()


def test_db_archive_selftest():
    db_archive._selftest()
