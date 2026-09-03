# -*- coding: utf-8 -*-
"""G21（第36轮）标准研究面板 + 特征注册表 + PIT/训练-服务一致性 零网络确定性测试。

全部手算可核、不连 monitor.db/不联网：
  - 特征注册表：9个综合分part与 config.ATTR_FACTOR_ORDER 逐字一致、字段/方向/状态合法、动态键归一
  - PIT as-of：严格早于/≤ 边界、基本面当日不可见
  - 逐行面板：暖机行数、ret1d 手算、特征键齐全
  - 结构性无未来函数：扰动未来价格，历史行逐值不变；故意泄漏能被抓
  - 训练-服务一致性：面板行 == 对同一前缀实时 compute_indicators
  - PanelStore：幂等重建逐值一致、主键去重、回读一致、manifest 落表
  - 缓存面板结构审计：干净通过、ret1d 破坏/时间戳越界能被检出
"""
import os
import tempfile

import config
import futures_data
import factors_catalog as fc
import panel_builder as pb
import pit_audit as pa


def _bars(n=60, start=100.0, drift=0.002):
    out = []
    for i in range(n):
        c = start * (1 + drift * i) + (0.3 if i % 3 == 0 else 0.0)
        out.append({"d": "2026-%02d-%02d" % (i // 28 + 1, min(28, i % 28 + 1)),
                    "o": c - 0.1, "h": c + 0.2, "l": c - 0.3,
                    "c": c, "v": 1000 + i, "p": 5000 + i, "s": c})
    return out


# ---------------- 特征注册表 ----------------
def test_catalog_part_keys_match_config():
    assert list(fc.PART_KEYS) == list(config.ATTR_FACTOR_ORDER)
    recs = fc.part_records()
    assert len(recs) == 9 and all(r and r["status"] == "live" for r in recs)


def test_catalog_validate_clean_and_unique():
    assert fc.validate() == []
    keys = fc.all_keys()
    assert len(keys) == len(set(keys))           # key 唯一


def test_catalog_dynamic_key_and_status():
    assert fc.by_key("原油联动(w=0.50)")["key"] == "原油联动"
    assert fc.by_key("不存在") is None
    assert fc.by_key("xsmom_z252")["status"] == "archived"
    assert fc.by_key("carry_cs")["status"] == "tracking"
    assert "新闻消息面" in fc.catalog_text()


# ---------------- PIT as-of ----------------
def test_asof_boundaries():
    ds = ["2026-01-01", "2026-01-03", "2026-01-05"]
    assert pb.asof_before(ds, "2026-01-01", strict=True) == -1
    assert pb.asof_before(ds, "2026-01-01", strict=False) == 0
    assert pb.asof_before(ds, "2026-01-04", strict=True) == 1
    assert pb.asof_before(ds, "2025-01-01", strict=True) == -1
    assert pb.asof_before([], "x") == -1
    assert pb.asof_pick([(d, i) for i, d in enumerate(ds)], "2026-01-05", True) == ("2026-01-03", 1)


def test_fund_strict_asof():
    funds = [("2026-01-15", 0.5, 0.01, 0.0), ("2026-01-20", -0.5, -0.01, 0.0)]
    bars = [{"d": "2026-01-%02d" % (i + 1), "o": 100 + i, "h": 101 + i, "l": 99 + i,
             "c": 100.5 + i, "v": 1, "p": 10, "s": 100.5 + i} for i in range(28)]
    rows, _ = pb.build_symbol_rows("X", "有色", bars, funds, warmup=10)
    by = {r["date"]: r for r in rows}
    assert by["2026-01-15"]["fund_score"] is None       # 当日基本面不可见
    assert by["2026-01-16"]["fund_score"] == 0.5
    assert by["2026-01-20"]["fund_score"] == 0.5
    assert by["2026-01-21"]["fund_score"] == -0.5


# ---------------- 逐行面板 ----------------
def test_build_rows_warmup_and_ret1d():
    raw = _bars(60)
    rows, _ = pb.build_symbol_rows("RB", "黑色", raw, warmup=10)
    assert len(rows) == 51
    assert rows[0]["date"] == raw[9]["d"]
    exp = raw[10]["c"] / raw[9]["c"] - 1
    assert abs(rows[1]["ret1d"] - exp) < 1e-12
    for k in pb.FEATURE_COLS:
        assert k in rows[0]


def test_all_cols_cover_config():
    for k in config.PANEL_FEATURE_KEYS:
        assert k in pb.ALL_COLS
    assert {"sym", "date", "sector", "oi", "ret1d", "fund_score"} <= set(pb.ALL_COLS)


# ---------------- 无未来函数（扰动法） ----------------
def test_no_future_function_by_perturbation():
    raw = _bars(60)

    def build(b):
        rr, _ = pb.build_symbol_rows("RB", "黑色", b, warmup=10)
        return rr
    div = pa.assert_no_future(raw, build, [9, 19, 34, 49], ["ret1d"] + pb.FEATURE_COLS)
    assert div == []


def test_leaky_factor_is_caught():
    raw = _bars(60)

    def leaky(b):
        rr, _ = pb.build_symbol_rows("RB", "黑色", b, warmup=10)
        for r in rr:
            r["ret1d"] = b[-1]["c"]
        return rr
    assert pa.assert_no_future(raw, leaky, [10, 30], ["ret1d"])


def test_timestamp_leak_scan():
    rows = [{"f": "2026-01-02", "e": "2026-01-01"}, {"f": "2026-01-01", "e": "2026-01-01"},
            {"f": None, "e": "2026-01-01"}]
    assert pa.timestamp_leaks(rows, "f", "e") == [0]


# ---------------- 训练-服务一致性 ----------------
def test_training_serving_parity():
    raw = _bars(50)
    rows, _ = pb.build_symbol_rows("RB", "黑色", raw, warmup=10)
    import backtest
    adj, _ = backtest.ratio_adjusted_bars(list(raw))
    d2t = {str(b.get("d", "")): t for t, b in enumerate(adj)}
    for idx in (0, 10, 25, 40):
        row = rows[idx]
        mism = pa.parity_one(adj, row, d2t[row["date"]], pb.FEATURE_COLS)
        assert mism == []


def test_parity_detects_injected_divergence():
    raw = _bars(50)
    rows, _ = pb.build_symbol_rows("RB", "黑色", raw, warmup=10)
    import backtest
    adj, _ = backtest.ratio_adjusted_bars(list(raw))
    d2t = {str(b.get("d", "")): t for t, b in enumerate(adj)}
    hacked = dict(rows[10]); hacked["ma5"] = 999.0
    assert pa.parity_one(adj, hacked, d2t[hacked["date"]], ["ma5"])


# ---------------- PanelStore 幂等/结构审计 ----------------
def test_panelstore_idempotent_and_readback():
    raw = _bars(50)
    rows, roll = pb.build_symbol_rows("RB", "黑色", raw, warmup=10)
    with tempfile.TemporaryDirectory() as td:
        dbp = os.path.join(td, "p.db")
        st = pb.PanelStore(dbp)
        n1 = st.replace_symbol("RB", rows)
        back1 = st.load_rows("RB")
        n2 = st.replace_symbol("RB", rows)        # 重建
        back2 = st.load_rows("RB")
        assert n1 == n2 == len(rows) == len(back1) == len(back2)
        assert back1 == back2
        assert st.count("RB") == len(rows)
        st.record_run(["RB"], 50, 1, n1, rows[0]["date"], rows[-1]["date"], roll)
        assert len(st.manifests()) == 1
        assert abs(back1[5]["ret1d"] - rows[5]["ret1d"]) < 1e-12
        st.close()


def test_audit_panel_db_clean_and_corrupt():
    raw = _bars(50)
    good, _ = pb.build_symbol_rows("RB", "黑色", raw, warmup=10)
    with tempfile.TemporaryDirectory() as td:
        dbp = os.path.join(td, "p.db")
        st = pb.PanelStore(dbp)
        st.replace_symbol("RB", good)
        st.close()
        res = pa.audit_panel_db(dbp)
        assert res["issues"] == []
        # 破坏 ret1d 自洽
        bad = [dict(r) for r in good]; bad[5]["ret1d"] = 9.99
        st = pb.PanelStore(dbp); st.replace_symbol("RB", bad); st.close()
        res2 = pa.audit_panel_db(dbp)
        assert any("ret1d" in x for x in res2["issues"])
