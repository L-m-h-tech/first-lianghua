# -*- coding: utf-8 -*-
"""期权成交量 PCR 情绪档回归（第137轮，零网络/零生产库依赖）。

覆盖：vol_pcr_of 函数有值/无值/异常路径/品种映射、
analyze_option chain_note 输出含成交量PCR字段、背离提示触发条件。
"""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import option_analyzer as OA
import config


def _make_vol_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE option_pcr_vol(
        sym TEXT NOT NULL, trade_date TEXT NOT NULL, call_vol REAL, put_vol REAL,
        pcr_vol REAL, n_calls INTEGER, n_puts INTEGER, source TEXT,
        created_real REAL, UNIQUE(sym, trade_date))""")
    conn.execute("INSERT INTO option_pcr_vol VALUES('RB','2026-09-11',8,4,0.50,15,15,'tqsdk',NULL)")
    conn.execute("INSERT INTO option_pcr_vol VALUES('ZN','2026-09-11',5,12,2.40,8,8,'tqsdk',NULL)")
    conn.execute("INSERT INTO option_pcr_vol VALUES('CU','2026-09-10',10,8,0.80,6,6,'tqsdk',NULL)")
    conn.commit(); conn.close()


def test_sym_of():
    assert OA._sym_of("螺纹钢") == "RB"
    assert OA._sym_of("铜") == "CU"
    assert OA._sym_of("不存在的品种") == "不存在的品种"
    assert OA._sym_of("") is None
    assert OA._sym_of(None) is None


def test_vol_pcr_of_hit(tmp_path):
    db = str(tmp_path / "m.db")
    _make_vol_db(db)
    assert OA.vol_pcr_of("RB", db_path=db) == 0.50
    assert OA.vol_pcr_of("RB", day="2026-09-10", db_path=db) is None   # 该天无 RB
    assert OA.vol_pcr_of("CU", day="2026-09-10", db_path=db) == 0.80
    assert OA.vol_pcr_of("CU", day="2026-09-11", db_path=db) is None    # 该天无 CU


def test_vol_pcr_of_missing(tmp_path):
    db = str(tmp_path / "m.db")
    _make_vol_db(db)
    assert OA.vol_pcr_of("不存在的品种", db_path=db) is None
    assert OA.vol_pcr_of("", db_path=db) is None
    assert OA.vol_pcr_of("RB", db_path=str(tmp_path / "nonexistent.db")) is None
    # 异常不抛
    assert isinstance(OA.vol_pcr_of("RB", db_path=None), (float, int, type(None)))  # 默认库不抛错
    assert OA.vol_pcr_of("RB", db_path="") is None       # 空路径 → None


def test_vol_pcr_of_caches(tmp_path):
    db = str(tmp_path / "m.db")
    _make_vol_db(db)
    OA._VOL_PCR_CACHE.clear()
    v1 = OA.vol_pcr_of("RB", day="2026-09-11", db_path=db)
    v2 = OA.vol_pcr_of("RB", day="2026-09-11", db_path=db)
    assert v1 == v2 == 0.50


def test_analyze_option_chain_note_has_vol_pcr():
    """chain_note 包含成交量 PCR 数值（来自 option_pcr_vol 表）"""
    fut_row = {
        "name": "螺纹钢", "price": 3200.0, "score": 6.0,
        "opt_month": {"yy": 26, "mm": 11, "opt_days": 40},
        "option_chain": {"pcr_oi": 0.50, "sentiment": "中性",
                         "pcr_vol": None, "n_call": 15, "n_put": 15,
                         "call_oi": 1000, "put_oi": 500, "pcr_pct": 0.6},
        "hv20": 0.15, "hv60": 0.16, "iv": 0.18, "atr": 50.0,
    }
    res = OA.analyze_option("螺纹钢", fut_row)
    assert "成交量PCR=" in res["chain_note"]
    assert "暂无" not in res["chain_note"]   # 旧占位文案不应出现


def test_analyze_option_divergence_hint():
    """量PCR 显著偏离持仓PCR → 生成背离提示"""
    # 偏高：量PCR>>持仓 → 成交偏看跌
    fut_row = {
        "name": "锌", "price": 2000.0, "score": 6.0,
        "opt_month": {"yy": 26, "mm": 11, "opt_days": 40},
        "option_chain": {"pcr_oi": 0.50, "sentiment": "中性",
                         "pcr_vol": None, "n_call": 8, "n_put": 8,
                         "call_oi": 500, "put_oi": 500, "pcr_pct": 0.5},
        "hv20": 0.12, "hv60": 0.13, "iv": 0.15, "atr": 30.0,
    }
    res = OA.analyze_option("锌", fut_row)
    assert "盘中成交偏看跌" in res["chain_note"]

    # 偏低：量PCR<<持仓 → 成交偏看涨
    fut_row2 = {
        "name": "螺纹钢", "price": 3200.0, "score": 6.0,
        "opt_month": {"yy": 26, "mm": 11, "opt_days": 40},
        "option_chain": {"pcr_oi": 1.80, "sentiment": "中性",
                         "pcr_vol": None, "n_call": 15, "n_put": 15,
                         "call_oi": 1000, "put_oi": 1800, "pcr_pct": 0.8},
        "hv20": 0.15, "hv60": 0.16, "iv": 0.18, "atr": 50.0,
    }
    res2 = OA.analyze_option("螺纹钢", fut_row2)
    assert "盘中成交偏看涨" in res2["chain_note"]

    # 小差距：无背离提示
    fut_row3 = {
        "name": "丙烷", "price": 2500.0, "score": 6.0,
        "opt_month": {"yy": 26, "mm": 11, "opt_days": 40},
        "option_chain": {"pcr_oi": 0.80, "sentiment": "中性",
                         "pcr_vol": None, "n_call": 10, "n_put": 10,
                         "call_oi": 500, "put_oi": 500, "pcr_pct": 0.5},
        "hv20": 0.12, "hv60": 0.13, "iv": 0.15, "atr": 30.0,
    }
    res3 = OA.analyze_option("丙烷", fut_row3)   # 丙烷不在 option_pcr_vol → 无背离
    assert "盘中成交偏" not in res3["chain_note"]


def test_analyze_option_divergence_no_vol_data():
    """option_pcr_vol 无数据 → chain_note 诚实显示'暂无'，无背离提示"""
    fut_row = {
        "name": "不存在的品种", "price": 100.0, "score": 6.0,
        "opt_month": {"yy": 26, "mm": 11, "opt_days": 40},
        "option_chain": {"pcr_oi": 0.70, "sentiment": "中性",
                         "pcr_vol": None, "n_call": 10, "n_put": 10,
                         "call_oi": 500, "put_oi": 500, "pcr_pct": 0.5},
        "hv20": 0.10, "hv60": 0.11, "iv": 0.12, "atr": 20.0,
    }
    res = OA.analyze_option("不存在的品种", fut_row)
    assert "成交量PCR: 暂无(option_pcr_vol未采集)" in res["chain_note"]
    assert "盘中成交偏" not in res["chain_note"]
