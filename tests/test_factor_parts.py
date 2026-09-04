# -*- coding: utf-8 -*-
"""G2（第58二切片 / 第59三切片 / 第60四切片+最后一切片）综合分 live part 适配器 parity 测试。

钉死四件事：
  1. 各 part 适配器元数据与 factors_catalog 登记逐字一致；
  2. 门控语义/手算公式与 analyzer 内联式一致；
  3. **逐字节 parity**：网格+固定随机用例上，插件 vs 独立 legacy 公式、插件 vs 真实 analyzer
     主链 analyze_variety 全部 float.hex 逐位相等（门控关闭分支同样一致）；二切片=日线动量、
     三切片=7 个简单门控 part、四切片=最复杂的「基本面」（9/9 全齐）；最后一切片=注册表宿主装配
     assemble_live_parts 与内联 parts 逐键逐位一致、analyzer 开关两路逐字节相同；
  4. main.py 永不接插件层；analyzer 仅允许**函数内惰性 import 且受默认关开关门控**（旧内联路径可回退）。
"""
import math
import os

import config
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
        # 第四切片后 9 个 live part 全齐（按 PART_KEYS 规范序）
        expected = list(catalog.PART_KEYS)
        assert keys == expected and len(keys) == 9
        # 注册后与 catalog 零冲突、规范序可排
        assert fp.check_registry_vs_catalog() == []
        assert fp.ordered_live_keys() == expected
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


# ---------------- 第59轮·第三切片：其余 7 个 live part ----------------
def test_third_slice_metadata_matches_catalog():
    for key, ctor in factor_parts._PLUGINS.items():
        pl = ctor()
        rec = catalog.by_key(key)
        assert pl.key == key and pl.status == "live"
        assert pl.direction == rec["direction"]
        assert pl.bound == tuple(rec["bound"]) and pl.layer == rec["layer"]
        assert isinstance(pl, fp.FactorPlugin)


def test_third_slice_gate_semantics():
    # 新闻无门控恒透传
    assert factor_parts.news_compute({"news_score": 0.31}) == 0.31
    # 原油 oil_w>0 才开门
    assert factor_parts.oil_link_compute({"oil_w": 0.0, "oil_score": 1.0}) is None
    assert factor_parts.oil_link_compute({"oil_w": -0.2, "oil_score": 1.0}) is None
    # 机构 total>=3 才开门
    assert factor_parts.institution_compute({"inst": {"total": 2}}) is None
    # 阈值 0.01：恰等关门、越过才开（四个阈值 part 一致）
    assert factor_parts.intraday_momentum_compute({"tick_mom": 0.01}) is None
    assert factor_parts.intraday_momentum_compute({"tick_mom": -0.0101}) == -0.0101
    assert factor_parts.flow_capital_compute({"flow_score": 0.01}) is None
    assert factor_parts.minute_resonance_compute({"intraday_ok": True, "intra_resonance": 0.01}) is None
    assert factor_parts.minute_resonance_compute({"intraday_ok": False, "intra_resonance": 0.3}) is None
    assert factor_parts.tech_resonance_compute({"kline_ok": False, "price": 100.0, "resonance": 0.5}) is None
    assert factor_parts.tech_resonance_compute({"kline_ok": True, "price": 0.0, "resonance": 0.5}) is None
    assert factor_parts.tech_resonance_compute({"kline_ok": True, "price": 100.0, "resonance": 0.01}) is None


def test_third_slice_cases_deterministic():
    for key in factor_parts.THIRD_SLICE_KEYS:
        a = factor_parts.part_parity_cases(key, seed=7, n_random=32)
        b = factor_parts.part_parity_cases(key, seed=7, n_random=32)
        assert a == b and len(a) > 30


def test_third_slice_parity_real_analyzer_bit_exact():
    reps = factor_parts.parity_all_against_analyzer()
    expect_keys = set(factor_parts.THIRD_SLICE_KEYS) | {factor_parts.FOURTH_SLICE_KEY}
    assert set(reps) == expect_keys
    for key, rep in reps.items():
        assert rep["mismatches"] == [], (key, rep["mismatches"][:3])
        min_open = 40 if key == factor_parts.FOURTH_SLICE_KEY else 100
        assert rep["max_diff"] == 0.0 and rep["n_open"] >= min_open
    # 新闻无门控：用例全部开门；原油含 oil_w<=0 关门；基本面含全缺关门
    assert reps["新闻消息面"]["n_closed"] == 0
    assert reps["原油联动"]["n_closed"] >= 1
    assert reps["基本面"]["n_closed"] >= 1


# ---------------- 第60轮·第四切片：基本面（最复杂 part） ----------------
def test_fourth_slice_fundamental_gate_and_formula():
    import fundamental_factors as ff
    # 四子项全缺 -> None（主链不加入该 part）
    assert factor_parts.fundamental_compute({"term": None, "fund_raw": None}) is None
    assert factor_parts.fundamental_compute({}) is None
    # 仅基差：值=build_fundamental 后的 score，且 |score|>0.01 才出
    ctx = {"term": None, "fund_raw": {"basis": 0.05}}
    bf = ff.basis_factor(0.05)
    pack = ff.build_fundamental(None, None, None, bf)
    got = factor_parts.fundamental_compute(ctx)
    assert float.hex(got) == float.hex(pack["score"])


def test_fourth_slice_fundamental_parity_real_analyzer_bit_exact():
    rep = factor_parts.parity_part_against_analyzer("基本面")
    assert rep["mismatches"] == [], rep["mismatches"][:3]
    assert rep["max_diff"] == 0.0 and rep["n_open"] >= 40 and rep["n_closed"] >= 1


# ---------------- 第60轮·最后一切片：注册表宿主装配 vs analyzer 内联 ----------------
def _full_row_inputs(rng):
    import contracts as ct
    oil_w = rng.choice((0.0, 0.3, 0.5))
    kline_ok = rng.random() < 0.8
    price = rng.choice((0.0, 3500.0))
    ind = {"ret5": rng.uniform(-0.08, 0.08), "ret20": rng.uniform(-0.08, 0.08),
           "tech": {"resonance_score": rng.uniform(-1.2, 1.2)},
           "intraday": {"ok": rng.random() < 0.6, "resonance_score": rng.uniform(-0.4, 0.4)}}
    if rng.random() < 0.8:
        ind["ma10"] = 3500.0 * (1 + rng.uniform(-0.05, 0.05))
    total = rng.choice((0, 2, 5, 10))
    inst = (factor_parts._inst_dict(total, rng.randint(0, total), total - rng.randint(0, total))
            if total else None)
    info = {"list": [{"code": "RB%02d%02d" % (26, 9 + i), "yy": 26, "mm": 9 + i,
                      "latest": 3000 * (1 + rng.uniform(-0.05, 0.05)), "oi": 1000}
                     for i in range(rng.randint(2, 4))]}
    term = ct.term_structure(info)
    fund_raw = {"inv": [{"date": "d", "stock": rng.uniform(50, 500)} for _ in range(20)] if rng.random() < 0.6 else None,
                "rank": (rng.randint(0, 2000), rng.randint(0, 2000), None, None) if rng.random() < 0.6 else None,
                "basis": rng.uniform(-0.1, 0.1) if rng.random() < 0.6 else None}
    return dict(oil_w=oil_w, kline_ok=kline_ok, price=price, ind=ind, inst=inst,
                info=info, term=term, fund_raw=fund_raw,
                news=rng.uniform(-4, 4), oil_score=rng.uniform(-3, 3),
                tick=rng.uniform(-1.5, 1.5), flow_score=rng.uniform(-1.2, 1.2))


def test_assemble_live_parts_matches_inline_analyzer():
    import random
    import analyzer
    factor_parts.register_builtin_parts(replace=True)
    saved = config.PLUGIN_PARTS_ENABLED
    config.PLUGIN_PARTS_ENABLED = False     # 对照真值取内联路径，避免 analyzer helper 清空外部注册表
    try:
        rng = random.Random(20260904)
        for _ in range(200):
            z = _full_row_inputs(rng)
            assembled = factor_parts.assemble_live_parts(
                news_score=z["news"], oil_w=z["oil_w"], oil_score=z["oil_score"], inst=z["inst"],
                ind=z["ind"], kline_ok=z["kline_ok"], price=z["price"], tick_mom=z["tick"],
                flow={"score": z["flow_score"]}, term=z["term"], fund_raw=z["fund_raw"])
            row = analyzer.analyze_variety(
                "RB", {"code": "RB0", "sym": "RB", "ex": "SHFE", "cat": "黑色", "oil_w": z["oil_w"]},
                {"latest": z["price"]}, z["ind"], z["kline_ok"], z["news"], [], z["oil_score"],
                z["tick"], contract=z["info"], inst=z["inst"],
                flow={"score": z["flow_score"]}, fund_raw=z["fund_raw"])
            mp = row["parts"]
            assert set(assembled) == set(mp), (set(assembled), set(mp))
            for k, v in assembled.items():
                assert float.hex(v) == float.hex(mp[k]), (k, v, mp[k])
    finally:
        fp.clear()
        config.PLUGIN_PARTS_ENABLED = saved


def test_analyzer_plugin_switch_default_on_and_byte_identical():
    import random
    import analyzer
    # 第61轮起默认**开启**注册表取分（双路已证逐字节等价；内联路径仍保留、可把开关置 False 回退）
    assert config.PLUGIN_PARTS_ENABLED is True
    rng = random.Random(4242)
    saved = config.PLUGIN_PARTS_ENABLED
    try:
        for _ in range(120):
            z = _full_row_inputs(rng)
            kw = dict(meta={"code": "RB0", "sym": "RB", "ex": "SHFE", "cat": "黑色", "oil_w": z["oil_w"]},
                      quote={"latest": z["price"]}, ind=z["ind"], kline_ok=z["kline_ok"],
                      news_score=z["news"], news_hits=[], oil_score=z["oil_score"], tick_mom=z["tick"],
                      contract=z["info"], inst=z["inst"], flow={"score": z["flow_score"]},
                      fund_raw=z["fund_raw"])
            config.PLUGIN_PARTS_ENABLED = False
            off = analyzer.analyze_variety("RB", **kw)
            config.PLUGIN_PARTS_ENABLED = True
            on = analyzer.analyze_variety("RB", **kw)
            assert set(off["parts"]) == set(on["parts"])
            assert all(float.hex(off["parts"][k]) == float.hex(on["parts"][k]) for k in off["parts"])
            assert float.hex(off["score"]) == float.hex(on["score"])
    finally:
        config.PLUGIN_PARTS_ENABLED = saved


def test_main_chain_plugin_boundary():
    # main.py 永不接插件层
    main_src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    for banned in ("factor_plugin", "factor_parts"):
        assert banned not in main_src, "main.py 不得提及 %s" % banned
    # analyzer 只允许函数内**惰性** import（行首有缩进），不得有模块顶层 import；第61轮起开关默认开
    az = open(os.path.join(ROOT, "analyzer.py"), encoding="utf-8").read()
    for line in az.splitlines():
        if line.startswith(("import ", "from ")):
            assert "factor_plugin" not in line and "factor_parts" not in line, "analyzer 顶层不得 import 插件层: " + line
    assert "PLUGIN_PARTS_ENABLED" in az and config.PLUGIN_PARTS_ENABLED is True
