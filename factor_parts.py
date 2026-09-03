# -*- coding: utf-8 -*-
"""G2（第58轮·第二切片）综合分 live part 的插件适配器 —— 纯标准库、纯增量、**仍不接主链**。

第一切片（factor_plugin.py，第57轮）只落地了"插件契约 + 有序注册表 + 异常隔离 + catalog 一致性"
扩展点；本切片按既定路线把**第一个 live part「日线动量」**用适配器搬进注册表，并做**逐字节 parity**：

- 主链 analyzer.analyze_variety 仍然以内联公式计算 parts["日线动量"]，**main.py/analyzer.py 不得
  import 本模块**（由 tests 读源码钉死），综合分/双哈希基线（equity=c4da4cdf / trades=50dcc80）
  逐字节不变；本模块只是把同一段公式以 FactorPlugin 的形式**平行实现**一遍，证明"搬进注册表"这一步
  本身零行为变化。后续切片再逐个搬其余 part，全部 parity 通过后，最后一切片才让 analyzer 改为从注册
  表取分；任何一步 parity 不达标都不切，永远保留可一键回退的旧内联路径。

context 契约（未来宿主组装，与 analyzer.analyze_variety 入参一一对应）：
    {
      "kline_ok": bool,          # 日线 K 线是否可用（对应 analyzer 的 kline_ok 入参）
      "price":    float,         # 已解析的现价（quote.latest 兜底 ind.close，对应 analyzer 的 price）
      "ind":      {"ret5": float, "ret20": float, "ma10": float|None, ...}
    }
门控语义与 analyzer 完全一致：仅当 kline_ok 且 price>0 时该 part 存在（插件返回 float），否则该 part
在主链根本不会被加入 parts（插件返回 None 表示"本轮缺失"，宿主不得编造）。

分层依赖：模块顶层只 import 标准库 + factor_plugin/factors_catalog（纯数据/纯宿主，零网络零DB）；
对 analyzer 的真实 parity 采用**函数内惰性 import**，保证纯函数层在缺主链依赖的环境也能自测。
"""
import math
import random

try:
    import factor_plugin as fp
except Exception:                # 极端环境缺宿主时延迟到使用处报错
    fp = None
try:
    import factors_catalog as catalog
except Exception:
    catalog = None

PART_KEY = "日线动量"


# =========================== 纯函数：与 analyzer 内联公式逐行对应 ===========================
def daily_momentum_compute(context):
    """「日线动量」part 的平行实现，逐行对应 analyzer.analyze_variety 第 61~66 行：

        if kline_ok and price > 0:
            momentum = tanh(ret5*160)*2.5 + tanh(ret20*70)*2.0
            if ind.get("ma10"):
                momentum += tanh((price/ma10 - 1)*220)*1.0
            parts["日线动量"] = momentum

    运算顺序、浮点字面量、短路门控全部保持一致以保证逐字节相等；门控关闭返回 None（=主链不加入该 part）。
    """
    ctx = context or {}
    price = ctx.get("price")
    if not ctx.get("kline_ok") or not (isinstance(price, (int, float)) and price > 0):
        return None
    ind = ctx.get("ind") or {}
    momentum = math.tanh(ind["ret5"] * 160) * 2.5 + math.tanh(ind["ret20"] * 70) * 2.0
    if ind.get("ma10"):
        momentum += math.tanh((price / ind["ma10"] - 1) * 220) * 1.0
    return momentum


def legacy_daily_momentum(ind, price):
    """parity 参考实现：analyzer 门控通过后的**逐字内联公式**（不含门控，供纯函数层离线比对）。

    刻意与 daily_momentum_compute 重复书写而非互相调用——parity 的意义就在于"两条独立写出的路径
    得到逐字节相同结果"，互相调用会让 parity 退化为同义反复。
    """
    momentum = math.tanh(ind["ret5"] * 160) * 2.5 + math.tanh(ind["ret20"] * 70) * 2.0
    if ind.get("ma10"):
        momentum += math.tanh((price / ind["ma10"] - 1) * 220) * 1.0
    return momentum


# =========================== 适配器：把纯函数包成 live 插件 ===========================
def daily_momentum_plugin():
    """构造「日线动量」live 插件；元数据（方向/界/层级）必须与 factors_catalog 登记一致。"""
    if fp is None:
        raise RuntimeError("factor_plugin 宿主不可用")
    meta = {"legacy": "analyzer.analyze_variety 内联公式（第61~66行）",
            "slice": 58, "formula": "tanh(ret5*160)*2.5+tanh(ret20*70)*2.0+tanh(price/ma10-1)*220"}
    return fp.make_plugin(PART_KEY, daily_momentum_compute, name=PART_KEY,
                          layer="技术", direction=+1, bound=(-4.5, 4.5),
                          status="live", meta=meta)


def builtin_part_plugins():
    """当前已用适配器搬进注册表的 live part（后续切片逐个追加；顺序无关，最终按 PART_KEYS 规范序）。"""
    return [daily_momentum_plugin()]


def register_builtin_parts(replace=False):
    """把内置 live part 适配器注册进 factor_plugin 注册表，返回注册的 key 列表（调用方负责 clear）。"""
    if fp is None:
        raise RuntimeError("factor_plugin 宿主不可用")
    keys = []
    for pl in builtin_part_plugins():
        fp.register(pl, replace=replace)
        keys.append(pl.key)
    return keys


# =========================== parity 用例（确定性，无随机种子漂移） ===========================
def parity_cases(seed=20260903, n_random=256):
    """生成 parity 测试用例：门控关闭 + 规则网格 + 固定种子随机（含 ma10 有/无、正负、极端值）。

    返回 [{"kline_ok","price","ind"}]；analyzer 侧用同一份 ind/price 驱动，保证输入逐字节相同。
    """
    cases = []
    # 1) 门控关闭类（主链不应出现该 part，插件应返回 None）
    cases += [
        {"kline_ok": False, "price": 3500.0, "ind": {"ret5": 0.01, "ret20": -0.02, "ma10": 3500.0}},
        {"kline_ok": True, "price": 0.0, "ind": {"ret5": 0.01, "ret20": -0.02, "ma10": 3500.0}},
        {"kline_ok": True, "price": -1.0, "ind": {"ret5": 0.01, "ret20": -0.02, "ma10": 3500.0}},
    ]
    # 2) 规则网格（覆盖 ret5/ret20 正负、ma10 偏离正负、ma10 缺失）
    grid_r = (-0.08, -0.02, -0.001, 0.0, 0.001, 0.02, 0.08)
    for r5 in grid_r:
        for r20 in grid_r:
            for ma in (None, 0.0, 3000.0, 3500.0, 4000.0):
                ind = {"ret5": r5, "ret20": r20}
                if ma is not None:
                    ind["ma10"] = ma
                cases.append({"kline_ok": True, "price": 3500.0, "ind": ind})
    # 3) 固定种子随机（含更极端的收益与现价/ma10 比值）
    rng = random.Random(seed)
    for _ in range(n_random):
        r5 = rng.uniform(-0.15, 0.15)
        r20 = rng.uniform(-0.30, 0.30)
        price = rng.uniform(50.0, 100000.0)
        ma = price * (1.0 + rng.uniform(-0.08, 0.08))
        ind = {"ret5": r5, "ret20": r20}
        if rng.random() < 0.85:                      # 多数带 ma10，少数刻意缺失
            ind["ma10"] = ma
        cases.append({"kline_ok": True, "price": price, "ind": ind})
    return cases


def _bits(x):
    """float 的二进制表示，用于逐字节（逐位）相等判定，规避 == 对 -0.0/NaN 的歧义。"""
    return float.hex(x)


def parity_against_formula(cases=None):
    """纯函数层 parity：插件求值 vs 独立书写的 legacy 内联公式，要求**逐位相等**。

    返回 {"n_open","n_closed","max_diff","mismatches"}；门控关闭用例要求插件为 None。
    """
    cases = cases if cases is not None else parity_cases()
    if fp is None:
        raise RuntimeError("factor_plugin 宿主不可用")
    fp.clear()
    fp.register(daily_momentum_plugin(), replace=True)
    n_open = n_closed = 0
    max_diff = 0.0
    mismatches = []
    try:
        for ctx in cases:
            got, err = fp.evaluate(ctx, PART_KEY)
            if err:
                mismatches.append({"ctx": ctx, "reason": "plugin_error:%s" % err})
                continue
            ind, price = ctx["ind"], ctx["price"]
            gate_open = bool(ctx.get("kline_ok")) and isinstance(price, (int, float)) and price > 0
            if not gate_open:
                n_closed += 1
                if got is not None:
                    mismatches.append({"ctx": ctx, "reason": "closed_gate_non_none", "got": got})
                continue
            n_open += 1
            want = legacy_daily_momentum(ind, price)
            if got is None or _bits(got) != _bits(want):
                mismatches.append({"ctx": ctx, "want": want, "got": got})
                if isinstance(got, (int, float)):
                    max_diff = max(max_diff, abs(got - want))
            else:
                max_diff = max(max_diff, abs(got - want))
    finally:
        fp.clear()
    return {"n_open": n_open, "n_closed": n_closed, "max_diff": max_diff,
            "mismatches": mismatches}


def parity_against_analyzer(cases=None, meta=None):
    """**最强 parity**：惰性 import 真主链 analyzer.analyze_variety，用最小桩输入逐例驱动，
    取其 parts["日线动量"] 与插件求值逐位比对；门控关闭用例要求主链无该 part 且插件为 None。

    analyzer 不 import 本模块、本模块顶层不 import analyzer，二者只在本函数里被测试代码并排驱动，
    因此任何一处公式漂移都会在这里暴露。返回结构同 parity_against_formula。
    """
    import analyzer
    cases = cases if cases is not None else parity_cases()
    if fp is None:
        raise RuntimeError("factor_plugin 宿主不可用")
    base_meta = {"code": "RB0", "sym": "RB", "ex": "SHFE", "cat": "黑色", "oil_w": 0.0}
    base_meta.update(meta or {})
    fp.clear()
    fp.register(daily_momentum_plugin(), replace=True)
    n_open = n_closed = 0
    max_diff = 0.0
    mismatches = []
    try:
        for ctx in cases:
            price = ctx.get("price")
            ind = dict(ctx.get("ind") or {})
            ind.setdefault("tech", {})
            ind.setdefault("intraday", {})
            row = analyzer.analyze_variety(
                "parity", dict(base_meta), {"latest": price}, ind, bool(ctx.get("kline_ok")),
                0.0, [], 0.0, 0.0)
            main_val = row["parts"].get(PART_KEY)
            got, err = fp.evaluate(ctx, PART_KEY)
            gate_open = bool(ctx.get("kline_ok")) and isinstance(price, (int, float)) and price > 0
            if not gate_open:
                n_closed += 1
                if PART_KEY in row["parts"]:
                    mismatches.append({"ctx": ctx, "reason": "main_has_part_when_gate_closed"})
                if got is not None or err:
                    mismatches.append({"ctx": ctx, "reason": "plugin_closed_gate", "got": got, "err": err})
                continue
            n_open += 1
            if main_val is None:
                mismatches.append({"ctx": ctx, "reason": "main_missing_part"})
                continue
            if err or got is None or _bits(got) != _bits(main_val):
                mismatches.append({"ctx": ctx, "main": main_val, "got": got, "err": err})
            if isinstance(got, (int, float)):
                max_diff = max(max_diff, abs(got - main_val))
    finally:
        fp.clear()
    return {"n_open": n_open, "n_closed": n_closed, "max_diff": max_diff,
            "mismatches": mismatches}


# =========================== 离线自测 ===========================
def selftest():
    assert fp is not None and catalog is not None
    fp.clear()
    # 1) 插件元数据与 factors_catalog 登记逐字一致（方向/界/状态/层级）
    pl = daily_momentum_plugin()
    rec = catalog.by_key(PART_KEY)
    assert rec is not None and pl.key == PART_KEY == rec["key"]
    assert pl.status == "live" and pl.direction == rec["direction"] == +1
    assert pl.bound == tuple(rec["bound"]) == (-4.5, 4.5) and pl.layer == rec["layer"] == "技术"
    # 2) 门控语义：关闭即 None（与主链"不加入 part"一致）
    assert daily_momentum_compute({"kline_ok": False, "price": 10.0,
                                   "ind": {"ret5": 0.1, "ret20": 0.1}}) is None
    assert daily_momentum_compute({"kline_ok": True, "price": 0.0,
                                   "ind": {"ret5": 0.1, "ret20": 0.1}}) is None
    assert daily_momentum_compute(None) is None
    # 3) 门控开启：无 ma10 仅两项、有 ma10 三项，手算一致
    ctx2 = {"kline_ok": True, "price": 100.0, "ind": {"ret5": 0.0, "ret20": 0.0}}
    assert daily_momentum_compute(ctx2) == 0.0
    ctx3 = {"kline_ok": True, "price": 110.0, "ind": {"ret5": 0.0, "ret20": 0.0, "ma10": 100.0}}
    expect3 = math.tanh((110.0 / 100.0 - 1) * 220) * 1.0
    assert float.hex(daily_momentum_compute(ctx3)) == float.hex(expect3)
    # ma10=0 视为假值（与 analyzer 的 if ind.get("ma10") 一致），退化为两项
    ctx4 = {"kline_ok": True, "price": 110.0, "ind": {"ret5": 0.01, "ret20": -0.01, "ma10": 0.0}}
    assert float.hex(daily_momentum_compute(ctx4)) == float.hex(
        math.tanh(0.01 * 160) * 2.5 + math.tanh(-0.01 * 70) * 2.0)
    # 4) 纯函数层 parity：网格+随机逐位相等、门控关闭全 None
    rep = parity_against_formula()
    assert rep["n_open"] > 300 and rep["n_closed"] >= 3 and not rep["mismatches"], rep["mismatches"][:3]
    assert rep["max_diff"] == 0.0
    # 5) 对**真实 analyzer 主链**的逐位 parity（惰性 import；含门控关闭分支）
    arep = parity_against_analyzer()
    assert arep["n_open"] == rep["n_open"] and arep["n_closed"] == rep["n_closed"]
    assert not arep["mismatches"], arep["mismatches"][:3]
    assert arep["max_diff"] == 0.0
    # 6) 注册内置 live part：catalog 一致性零问题、规范序可排、可清空
    keys = register_builtin_parts()
    assert keys == [PART_KEY]
    assert fp.check_registry_vs_catalog() == []
    assert PART_KEY in fp.ordered_live_keys() and fp.names(status="live") == [PART_KEY]
    v, e = fp.evaluate({"kline_ok": True, "price": 100.0,
                        "ind": {"ret5": 0.01, "ret20": -0.02, "ma10": 101.0}}, PART_KEY)
    assert isinstance(v, float) and e is None
    fp.clear()
    assert fp.names() == []
    # 7) 异常隔离：即便未来某 part 计算抛错，也被宿主隔离为 (None, err)，不拖垮其它插件
    fp.register(fp.make_plugin("boom", lambda c: 1 / 0, meta={"external": True}))
    vv, ee = fp.evaluate({}, "boom")
    assert vv is None and "ZeroDivision" in ee
    fp.clear()
    # 8) 适配器列表可重复构造且彼此独立（无进程级副作用）
    a, b = builtin_part_plugins(), builtin_part_plugins()
    assert len(a) == len(b) == 1 and a[0] is not b[0] and a[0].key == b[0].key == PART_KEY
    fp.clear()
    print("factor_parts selftest ALL PASS（8组：元数据对齐catalog/门控语义/手算公式/公式parity逐位/真analyzer逐位parity/注册一致性/异常隔离/无副作用，parity n_open=%d n_closed=%d）"
          % (rep["n_open"], rep["n_closed"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
