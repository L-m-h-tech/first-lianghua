# -*- coding: utf-8 -*-
"""G2（第58轮·第二切片）综合分 live part 适配器 parity 测试。

钉死四件事：
  1. 「日线动量」适配器元数据与 factors_catalog 登记逐字一致；
  2. 门控语义/手算公式与 analyzer 内联式一致；
  3. **逐字节 parity**：网格+固定随机用例上，插件 vs 独立 legacy 公式、插件 vs 真实 analyzer
     主链 analyze_variety 全部 float.hex 逐位相等（门控关闭分支同样一致）；
  4. 主链 main.py/analyzer.py 源码不得 import factor_plugin/factor_parts（仍不切主链）。
"""
import math
import os

import factor_parts
import factor_plugin as fp
import factors_catalog as catalog

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_plugin_metadata_matches_catalog():
    pl = factor_parts.daily_momentum_plugin()
    rec = catalog.by_key("日线动量")
    assert pl.key == "日线动量" and pl.status == "live"
    assert pl.direction == rec["direction"] == +1
    assert pl.bound == tuple(rec["bound"]) == (-4.5, 4.5)
    assert pl.layer == rec["layer"] == "技术"
    # make_plugin 构造时已做契约校验（非法字段会抛 ValueError）
    assert isinstance(pl, fp.FactorPlugin)


def test_gate_semantics():
    # 门控关闭 -> None（主链此时根本不加入该 part）
    closed = [
        {"kline_ok": False, "price": 100.0, "ind": {"ret5": 0.01, "ret20": 0.0}},
        {"kline_ok": True, "price": 0.0, "ind": {"ret5": 0.01, "ret20": 0.0}},
        {"kline_ok": True, "price": -3.0, "ind": {"ret5": 0.01, "ret20": 0.0}},
        None, {},
    ]
    for ctx in closed:
        assert factor_parts.daily_momentum_compute(ctx) is None


def test_hand_computed_formula():
    # 无 ma10：仅两项；全零 -> 0
    ctx = {"kline_ok": True, "price": 100.0, "ind": {"ret5": 0.0, "ret20": 0.0}}
    assert factor_parts.daily_momentum_compute(ctx) == 0.0
    # 有 ma10：三项，手算逐位相等
    ctx = {"kline_ok": True, "price": 110.0,
           "ind": {"ret5": 0.012, "ret20": -0.03, "ma10": 100.0}}
    expect = (math.tanh(0.012 * 160) * 2.5 + math.tanh(-0.03 * 70) * 2.0
              + math.tanh((110.0 / 100.0 - 1) * 220) * 1.0)
    got = factor_parts.daily_momentum_compute(ctx)
    assert float.hex(got) == float.hex(expect)
    # ma10=0 是假值：退化为两项（与 analyzer 的 if ind.get("ma10") 一致）
    ctx0 = {"kline_ok": True, "price": 110.0,
            "ind": {"ret5": 0.012, "ret20": -0.03, "ma10": 0.0}}
    expect0 = math.tanh(0.012 * 160) * 2.5 + math.tanh(-0.03 * 70) * 2.0
    assert float.hex(factor_parts.daily_momentum_compute(ctx0)) == float.hex(expect0)


def test_parity_against_legacy_formula_bit_exact():
    rep = factor_parts.parity_against_formula()
    assert rep["n_open"] >= 300 and rep["n_closed"] >= 3
    assert rep["max_diff"] == 0.0 and rep["mismatches"] == []


def test_parity_against_real_analyzer_bit_exact():
    # 最强 parity：驱动真实主链 analyze_variety，门控开/闭两类用例全部逐位一致
    rep = factor_parts.parity_against_analyzer()
    assert rep["n_open"] >= 300 and rep["n_closed"] >= 3
    assert rep["max_diff"] == 0.0 and rep["mismatches"] == []


def test_parity_cases_deterministic():
    a = factor_parts.parity_cases(seed=1, n_random=16)
    b = factor_parts.parity_cases(seed=1, n_random=16)
    assert a == b
    c = factor_parts.parity_cases(seed=2, n_random=16)
    assert a != c


def test_register_builtin_parts_and_cleanup():
    fp.clear()
    keys = factor_parts.register_builtin_parts()
    try:
        assert keys == ["日线动量"]
        # 注册后与 catalog 零冲突、规范序可排
        assert fp.check_registry_vs_catalog() == []
        assert "日线动量" in fp.ordered_live_keys()
        v, err = fp.evaluate({"kline_ok": True, "price": 3500.0,
                              "ind": {"ret5": 0.01, "ret20": -0.02, "ma10": 3500.0}},
                             "日线动量")
        assert err is None and isinstance(v, float)
        # 重复注册不覆盖报错（replace=False）
        try:
            factor_parts.register_builtin_parts()
            assert False, "重复注册应报 ValueError"
        except ValueError:
            pass
    finally:
        fp.clear()
    assert fp.names() == []


def test_main_chain_does_not_import_plugin_layer():
    # 仍不切主链：main.py / analyzer.py 源码不得出现 factor_plugin / factor_parts
    for fn in ("main.py", "analyzer.py"):
        src = open(os.path.join(ROOT, fn), encoding="utf-8").read()
        for banned in ("factor_plugin", "factor_parts"):
            assert banned not in src, "%s 不得 import/提及 %s（第二切片仍不接主链）" % (fn, banned)
