# -*- coding: utf-8 -*-
"""G2（第58轮第二切片 / 第59轮第三切片）综合分 live part 的插件适配器 —— 纯标准库、纯增量、**仍不接主链**。

第一切片（factor_plugin.py，第57轮）只落地了"插件契约 + 有序注册表 + 异常隔离 + catalog 一致性"
扩展点；第二切片（第58轮）把「日线动量」适配器化并做逐字节 parity；第三切片（第59轮）再把其余 7 个
门控简单的 part（新闻消息面/原油联动/机构动向/技术共振/分钟共振/盘中动量/量仓资金）全部适配器化，
同样对**真实 analyzer 主链**逐位 parity。至此 9 个 live part 已搬 8 个，**仅剩「基本面」**（依赖
fundamental_factors + 期限结构 term，留到下一切片）。

- 主链 analyzer.analyze_variety 仍然以内联公式计算各 part，**main.py/analyzer.py 不得
  import 本模块**（由 tests 读源码钉死），综合分/双哈希基线（equity=c4da4cdf / trades=50dcc80）
  逐字节不变；本模块只是把同一段公式以 FactorPlugin 的形式**平行实现**一遍，证明"搬进注册表"这一步
  本身零行为变化。最后一切片（9 part 全 parity 后）才让 analyzer 改为从注册表取分；任何一步 parity
  不达标都不切，永远保留可一键回退的旧内联路径。

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


def _part_plugin(key, compute, meta):
    """按 factors_catalog 登记的方向/界/层级构造 live part 插件（元数据由唯一注册表派生，杜绝漂移）。"""
    if fp is None or catalog is None:
        raise RuntimeError("factor_plugin/factors_catalog 宿主不可用")
    rec = catalog.by_key(key)
    if rec is None:
        raise RuntimeError("part %r 未在 factors_catalog 登记" % key)
    return fp.make_plugin(key, compute, name=key, layer=rec["layer"],
                          direction=rec["direction"], bound=tuple(rec["bound"]),
                          status="live", meta=meta)


def builtin_part_plugins():
    """当前已用适配器搬进注册表的 live part（按 PART_KEYS 规范序；后续切片追加，仅剩「基本面」）。"""
    return [news_plugin(), oil_link_plugin(), institution_plugin(), daily_momentum_plugin(),
            tech_resonance_plugin(), minute_resonance_plugin(),
            intraday_momentum_plugin(), flow_capital_plugin()]


def register_builtin_parts(replace=False):
    """把内置 live part 适配器注册进 factor_plugin 注册表，返回注册的 key 列表（调用方负责 clear）。"""
    if fp is None:
        raise RuntimeError("factor_plugin 宿主不可用")
    keys = []
    for pl in builtin_part_plugins():
        fp.register(pl, replace=replace)
        keys.append(pl.key)
    return keys


# =========================== 第59轮·第三切片：其余 7 个 live part（仍不接主链） ===========================
def _isnum(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


# ---- 新闻消息面：主链 parts 初始化即放入（无门控），值=news_score 透传 ----
def news_compute(context):
    ctx = context or {}
    return ctx.get("news_score")


def legacy_news(news_score):
    return news_score


# ---- 原油联动：门控 meta["oil_w"]>0；值=oil_score*oil_w（主链键名动态带 (w=..)，规范键=原油联动） ----
def oil_link_compute(context):
    ctx = context or {}
    w, s = ctx.get("oil_w"), ctx.get("oil_score")
    if not (_isnum(w) and w > 0) or not _isnum(s):
        return None
    return s * w


def legacy_oil_link(oil_score, oil_w):
    return oil_score * oil_w


# ---- 机构动向：门控 inst 且 total>=3；tanh((bull-bear)/total*2)*2 ----
def institution_compute(context):
    inst = (context or {}).get("inst")
    if not inst or inst.get("total", 0) < 3:
        return None
    ratio = (inst["bullish"] - inst["bearish"]) / inst["total"]
    return math.tanh(ratio * 2.0) * 2.0


def legacy_institution(inst):
    ratio = (inst["bullish"] - inst["bearish"]) / inst["total"]
    return math.tanh(ratio * 2.0) * 2.0


# ---- 技术共振：门控 kline_ok&price>0，且 |resonance|>0.01（值透传 tech.resonance_score） ----
def tech_resonance_compute(context):
    ctx = context or {}
    price = ctx.get("price")
    if not ctx.get("kline_ok") or not (_isnum(price) and price > 0):
        return None
    r = float(ctx.get("resonance") or 0.0)
    return r if abs(r) > 0.01 else None


# ---- 分钟共振：门控 intraday.ok，且 |intra_resonance|>0.01 ----
def minute_resonance_compute(context):
    ctx = context or {}
    r = float(ctx.get("intra_resonance") or 0.0)
    return r if ctx.get("intraday_ok") and abs(r) > 0.01 else None


# ---- 盘中动量：门控 |tick_mom|>0.01 ----
def intraday_momentum_compute(context):
    x = float((context or {}).get("tick_mom") or 0.0)
    return x if abs(x) > 0.01 else None


# ---- 量仓资金：门控 |flow_score|>0.01（复刻主链 float(flow.get("score") or 0)） ----
def flow_capital_compute(context):
    x = float((context or {}).get("flow_score") or 0.0)
    return x if abs(x) > 0.01 else None


_COMPUTE = {
    "新闻消息面": news_compute, "原油联动": oil_link_compute, "机构动向": institution_compute,
    "技术共振": tech_resonance_compute, "分钟共振": minute_resonance_compute,
    "盘中动量": intraday_momentum_compute, "量仓资金": flow_capital_compute,
}
THIRD_SLICE_KEYS = tuple(_COMPUTE.keys())


def news_plugin():
    return _part_plugin("新闻消息面", news_compute, {"legacy": "parts 初始化透传 news_score", "slice": 59})


def oil_link_plugin():
    return _part_plugin("原油联动", oil_link_compute,
                        {"legacy": "oil_score*oil_w，门控 oil_w>0，主链动态键", "slice": 59})


def institution_plugin():
    return _part_plugin("机构动向", institution_compute,
                        {"legacy": "tanh((bull-bear)/total*2)*2，门控 total>=3", "slice": 59})


def tech_resonance_plugin():
    return _part_plugin("技术共振", tech_resonance_compute,
                        {"legacy": "tech.resonance_score 透传，门控 kline&price>0 且|r|>0.01", "slice": 59})


def minute_resonance_plugin():
    return _part_plugin("分钟共振", minute_resonance_compute,
                        {"legacy": "intraday.resonance_score 透传，门控 ok 且|r|>0.01", "slice": 59})


def intraday_momentum_plugin():
    return _part_plugin("盘中动量", intraday_momentum_compute,
                        {"legacy": "tick_mom 透传，门控 |x|>0.01", "slice": 59})


def flow_capital_plugin():
    return _part_plugin("量仓资金", flow_capital_compute,
                        {"legacy": "flow.score 透传，门控 |x|>0.01", "slice": 59})


_PLUGINS = {
    "新闻消息面": news_plugin, "原油联动": oil_link_plugin, "机构动向": institution_plugin,
    "技术共振": tech_resonance_plugin, "分钟共振": minute_resonance_plugin,
    "盘中动量": intraday_momentum_plugin, "量仓资金": flow_capital_plugin,
}


# ---- 各 part 的确定性 parity 用例（门控关闭/阈值边界/正负/固定随机） ----
def _inst_dict(total, bull, bear):
    return {"bullish": bull, "bearish": bear, "volatile": total - bull - bear, "total": total}


def part_parity_cases(key, seed=20260904, n_random=160):
    rng = random.Random(seed + sum(ord(ch) for ch in key))
    cases = []
    if key == "新闻消息面":
        for x in (-4.0, -1.0, -0.001, 0.0, 0.001, 1.0, 4.0):
            cases.append({"news_score": x})
        for _ in range(n_random):
            cases.append({"news_score": rng.uniform(-4.0, 4.0)})
    elif key == "原油联动":
        for w in (0.0, -0.2, 0.1, 0.3, 0.5, 1.0):
            for s in (-3.0, -1.0, 0.0, 1.0, 3.0):
                cases.append({"oil_w": w, "oil_score": s})
        for _ in range(n_random):
            cases.append({"oil_w": rng.uniform(-0.2, 1.0), "oil_score": rng.uniform(-3.0, 3.0)})
    elif key == "机构动向":
        for total in (0, 2, 3, 5, 10):
            for bull in (0, total // 2, total):
                cases.append({"inst": _inst_dict(total, bull, total - bull)})
        for _ in range(n_random):
            total = rng.choice((0, 1, 2, 3, 5, 8, 12))
            bull = rng.randint(0, total)
            cases.append({"inst": _inst_dict(total, bull, total - bull)})
    elif key == "技术共振":
        for k in (False, True):
            for price in (0.0, 3500.0):
                for r in (-1.2, -0.02, -0.01, 0.0, 0.01, 0.02, 1.2):
                    cases.append({"kline_ok": k, "price": price, "resonance": r})
        for _ in range(n_random):
            cases.append({"kline_ok": rng.random() < 0.85, "price": rng.uniform(0.0, 10000.0),
                          "resonance": rng.uniform(-1.2, 1.2)})
    elif key == "分钟共振":
        for ok in (False, True):
            for r in (-0.4, -0.02, -0.01, 0.0, 0.01, 0.02, 0.4):
                cases.append({"intraday_ok": ok, "intra_resonance": r})
        for _ in range(n_random):
            cases.append({"intraday_ok": rng.random() < 0.7, "intra_resonance": rng.uniform(-0.4, 0.4)})
    elif key in ("盘中动量", "量仓资金"):
        field = "tick_mom" if key == "盘中动量" else "flow_score"
        for x in (-1.5, -0.02, -0.01, 0.0, 0.01, 0.02, 1.5):
            cases.append({field: x})
        for _ in range(n_random):
            cases.append({field: rng.uniform(-1.5, 1.5)})
    else:
        raise KeyError(key)
    return cases


def _drive_main(key, ctx):
    """把插件 context 映射为 analyze_variety 最小桩入参，隔离出目标 part（其余 part 置空/关门）。"""
    import analyzer
    meta = {"code": "RB0", "sym": "RB", "ex": "SHFE", "cat": "黑色", "oil_w": 0.0}
    quote = {"latest": 0.0}
    ind = {"tech": {}, "intraday": {}}
    kline_ok = False
    news_score, news_hits, oil_score, tick_mom = 0.0, [], 0.0, 0.0
    inst, flow, contract, fund_raw = None, {}, None, None
    if key == "新闻消息面":
        news_score = ctx["news_score"]
    elif key == "原油联动":
        meta["oil_w"] = ctx["oil_w"]
        oil_score = ctx["oil_score"]
    elif key == "机构动向":
        inst = ctx.get("inst")
    elif key == "技术共振":
        kline_ok = bool(ctx.get("kline_ok"))
        quote = {"latest": ctx.get("price")}
        # 门控开时主链同时计算日线动量，需补 ret5/ret20（不影响目标 part 提取）
        ind.update({"ret5": 0.0, "ret20": 0.0,
                    "tech": {"resonance_score": ctx.get("resonance")}})
    elif key == "分钟共振":
        ind["intraday"] = {"ok": bool(ctx.get("intraday_ok")),
                           "resonance_score": ctx.get("intra_resonance")}
    elif key == "盘中动量":
        tick_mom = ctx.get("tick_mom")
    elif key == "量仓资金":
        flow = {"score": ctx.get("flow_score")}
    return analyzer.analyze_variety(
        "parity", dict(meta), quote, ind, kline_ok, news_score, news_hits,
        oil_score, tick_mom, contract=contract, inst=inst, flow=flow, fund_raw=fund_raw)


def _main_value(row, key):
    """从真实主链结果取目标 part；原油键名动态，按前缀归一。返回 (present, value)。"""
    parts = row["parts"]
    if key == "原油联动":
        for k, v in parts.items():
            if k.startswith("原油联动"):
                return True, v
        return False, None
    if key in parts:
        return True, parts[key]
    return False, None


def parity_part_against_analyzer(key, cases=None):
    """单个 part 对**真实 analyzer 主链**的逐位 parity：插件有值⇔主链有该 part，且 float.hex 逐位相等。"""
    if key not in _PLUGINS:
        raise KeyError(key)
    cases = cases if cases is not None else part_parity_cases(key)
    fp.clear()
    fp.register(_PLUGINS[key](), replace=True)
    n_open = n_closed = 0
    max_diff = 0.0
    mismatches = []
    try:
        for ctx in cases:
            got, err = fp.evaluate(ctx, key)
            present, main_val = _main_value(_drive_main(key, ctx), key)
            if got is None:
                n_closed += 1
                if err:
                    mismatches.append({"key": key, "ctx": ctx, "reason": "plugin_error:%s" % err})
                if present:
                    mismatches.append({"key": key, "ctx": ctx, "reason": "main_has_but_plugin_none",
                                       "main": main_val})
                continue
            n_open += 1
            if not present:
                mismatches.append({"key": key, "ctx": ctx, "reason": "plugin_has_but_main_none",
                                   "got": got})
                continue
            if _bits(got) != _bits(main_val):
                mismatches.append({"key": key, "ctx": ctx, "got": got, "main": main_val})
            if _isnum(got) and _isnum(main_val):
                max_diff = max(max_diff, abs(got - main_val))
    finally:
        fp.clear()
    return {"key": key, "n_open": n_open, "n_closed": n_closed,
            "max_diff": max_diff, "mismatches": mismatches}


def parity_all_against_analyzer():
    """第三切片 7 个 part 全部对真实主链逐位 parity；返回 {key: report}。"""
    return {key: parity_part_against_analyzer(key) for key in THIRD_SLICE_KEYS}


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
    # 6) 注册内置 live part（第三切片后共 8 个，仅余「基本面」）：catalog 一致性零问题、按 PART_KEYS 规范序
    expected = [k for k in catalog.PART_KEYS if k != "基本面"]
    keys = register_builtin_parts()
    assert keys == expected and len(keys) == 8, keys
    assert fp.check_registry_vs_catalog() == []
    assert fp.ordered_live_keys() == expected and fp.names(status="live") == expected
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
    # 8) 适配器列表可重复构造且彼此独立（无进程级副作用），第三切片后 8 个
    a, b = builtin_part_plugins(), builtin_part_plugins()
    assert len(a) == len(b) == 8 and all(x is not y for x, y in zip(a, b))
    assert [x.key for x in a] == [y.key for y in b] == expected
    fp.clear()
    # 9) 第三切片 7 part：元数据对齐 catalog + 门控/公式手算
    for key, ctor in _PLUGINS.items():
        p = ctor()
        r = catalog.by_key(key)
        assert p.key == key and p.status == "live"
        assert p.direction == r["direction"] and p.bound == tuple(r["bound"]) and p.layer == r["layer"]
    # 新闻无门控恒透传；原油门控 oil_w>0 且乘法；机构 total>=3 且 tanh
    assert news_compute({"news_score": 0.37}) == 0.37 and news_compute({"news_score": 0.0}) == 0.0
    assert oil_link_compute({"oil_w": 0.0, "oil_score": 1.0}) is None
    assert oil_link_compute({"oil_w": -0.1, "oil_score": 1.0}) is None
    assert float.hex(oil_link_compute({"oil_w": 0.3, "oil_score": 2.0})) == float.hex(legacy_oil_link(2.0, 0.3))
    assert institution_compute({"inst": _inst_dict(2, 1, 1)}) is None
    inst_open = _inst_dict(5, 4, 1)
    assert float.hex(institution_compute({"inst": inst_open})) == float.hex(legacy_institution(inst_open))
    # 阈值 0.01 边界：|x|>0.01 才计分（恰 0.01 关门），与 analyzer 完全一致
    assert intraday_momentum_compute({"tick_mom": 0.01}) is None
    assert intraday_momentum_compute({"tick_mom": -0.011}) == -0.011
    assert flow_capital_compute({"flow_score": 0.0}) is None
    assert minute_resonance_compute({"intraday_ok": False, "intra_resonance": 0.3}) is None
    assert minute_resonance_compute({"intraday_ok": True, "intra_resonance": 0.01}) is None
    assert tech_resonance_compute({"kline_ok": True, "price": 0.0, "resonance": 0.5}) is None
    assert tech_resonance_compute({"kline_ok": True, "price": 100.0, "resonance": 0.01}) is None
    # 10) 第三切片 7 part 全部对**真实 analyzer 主链**逐位 parity（门控开/闭一致、float.hex 相等）
    allrep = parity_all_against_analyzer()
    tot_open = tot_closed = 0
    for key, rr in allrep.items():
        assert not rr["mismatches"], (key, rr["mismatches"][:3])
        assert rr["max_diff"] == 0.0, (key, rr["max_diff"])
        assert rr["n_open"] >= 100, (key, rr["n_open"])
        tot_open += rr["n_open"]
        tot_closed += rr["n_closed"]
    assert allrep["新闻消息面"]["n_closed"] == 0   # 新闻无门控，用例全部开门
    assert allrep["原油联动"]["n_closed"] >= 10     # oil_w<=0 关门
    fp.clear()
    print("factor_parts selftest ALL PASS（10组：日线元数据/门控/手算/公式parity/真analyzer parity；"
          "注册8part一致性/异常隔离/无副作用；第三切片7part元数据+门控手算+对真analyzer逐位parity；"
          "日线 n_open=%d n_closed=%d；第三切片合计 n_open=%d n_closed=%d）"
          % (rep["n_open"], rep["n_closed"], tot_open, tot_closed))
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
