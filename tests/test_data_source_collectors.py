# -*- coding: utf-8 -*-
"""第96轮：新数据源采集器专项测试（openvlab_map / jiaoyikecha_collector），零网络。"""
import json
import os
import sqlite3
import tempfile

import openvlab_map as ovlm
import jiaoyikecha_collector as jykt


def test_ovlm_parse_and_store(tmp_path):
    sample = {"code": 0, "result": [
        {"product": "EG_O", "product_alias": "乙二醇", "prodUnd": "EG", "sector": "EN",
         "exchange": "DCE", "has_night_trading": True, "exp": 202610,
         "expiry_date": "2026-10-14", "price": 5740.0, "atmv_current": 46.19,
         "atmv_percentile": 88.02, "atmv_1dchg": -5.36, "skew_current": -0.11,
         "skew_percentile": 25.4, "rv22": 46.16, "carry": 0.05}]}

    class R:
        status_code = 200

        def json(self):
            return sample

    rows = ovlm.fetch_map(fetcher=lambda url, **kw: R())
    assert len(rows) == 1 and rows[0]["sym"] == "EG"
    assert rows[0]["atmv_current"] == 46.19
    dbp = str(tmp_path / "ovl.db")
    assert ovlm.store(dbp, rows) == 1
    assert ovlm.store(dbp, rows) == 1          # 幂等
    conn = sqlite3.connect(dbp)
    assert conn.execute("SELECT COUNT(*) FROM option_vol_map").fetchone()[0] == 1
    conn.close()
    # 渲染（写 tmp 路径）
    txt = str(tmp_path / "ovl.txt")
    js = str(tmp_path / "ovl.json")
    ovlm.render(rows, [], txt_path=txt, js_path=js)
    assert os.path.exists(txt) and os.path.exists(js)
    assert "乙二醇" in open(txt, encoding="utf-8").read()


def test_ovlm_cross_check_missing_local(tmp_path, monkeypatch):
    monkeypatch.setattr(ovlm, "IV_SURFACE_JSON", str(tmp_path / "iv_surface.json"))
    rows = [{"sym": "EG", "atmv_current": 46.19}]
    assert ovlm.cross_check(rows) == []        # 无本地 iv_surface → 空对照
    with open(tmp_path / "iv_surface.json", "w", encoding="utf-8") as f:
        json.dump({"ivs": [{"sym": "EG", "atm_iv": 44.0}]}, f)
    checks = ovlm.cross_check(rows)
    assert len(checks) == 1 and checks[0]["diff"] == 2.19 and checks[0]["warn"] is True


def test_jykt_parsers(tmp_path):
    wr = jykt.parse_daily_wr([{"name": "螺纹钢", "symbol": "RB", "total_vol": 134546,
                               "wr_unit": 0.1, "wr_pct": 2.5}])
    assert wr[0]["sym"] == "RB" and wr[0]["total_vol"] == 134546
    hg = jykt.parse_hg([{"variety": "螺纹钢", "symbol": "RB", "code": "rb2701",
                         "current_price": 3156, "support": 3100, "resistance": 3200}])
    assert hg[0]["support"] == 3100
    bt = jykt.parse_broker_trend([{"name": "国泰君安", "grade": "A", "money": 1904738480}])
    assert bt[0]["money"] == 1904738480
    lh = jykt.parse_longhu([{"name": "沪金", "code": "au2612", "longhu": 82.8}], "longhu")
    assert lh[0]["value"] == 82.8
    assert jykt.parse_daily_wr([]) == [] and jykt.parse_daily_wr([{"foo": 1}]) == []


def test_jykt_store_regression(tmp_path):
    dbp = str(tmp_path / "jykt.db")
    wr = jykt.parse_daily_wr([{"name": "螺纹钢", "symbol": "RB", "total_vol": 10, "wr_unit": 0.1, "wr_pct": 1}])
    assert jykt._store(dbp, "jykt_wr", wr) == 1
    assert jykt._store(dbp, "jykt_wr", wr) == 1   # 幂等
    conn = sqlite3.connect(dbp)
    assert conn.execute("SELECT COUNT(*) FROM jykt_wr").fetchone()[0] == 1
    conn.close()
