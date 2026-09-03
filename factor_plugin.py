# -*- coding: utf-8 -*-
"""G2（第57轮·第一切片）因子/策略插件化宿主 factor_plugin —— 纯标准库、纯增量、当前不接主链。

为什么是"第一切片"而不是一次性重写 analyzer：
- 综合分 9 个 part 的实时装配散在 analyzer.compute_indicators/factors/fundamental_factors，直接改成插件
  调度属于高风险纯重构，必须保证综合分逐字节等价（双哈希基线 equity=c4da4cdf / trades=50dcc80）。本轮**不
  切换主链**（main/analyzer 不 import 本模块），只先把"插件契约 + 注册表 + 隔离求值 + 与 factors_catalog
  一致性校验"这套扩展点做扎实并测试钉死；后续切片再逐个把 live part 用适配器搬进注册表、做逐字节 parity，
  最后才让 analyzer 从注册表取分。任何一步不达标都不切，综合分永远有可回退的旧路径。

插件契约 FactorPlugin（make_plugin 构造，字段冻结）：
- key：唯一标识；若要进综合分，key 必须是 factors_catalog.PART_KEYS 之一，否则只能 shadow/research；
- compute(context)：纯函数，输入一个 context 字典（行情/面板/已算part等，由未来宿主组装），输出数值或 None
  （None=本轮缺失，宿主不得编造）；禁止网络/写库等副作用；
- direction ∈ {-1,0,+1}、status ∈ factors_catalog 的五态、bound=(lo,hi) 可选；
- 注册表按注册顺序确定求值顺序；单个插件抛错被隔离（记 error、值 None），不影响其它插件与宿主。

本模块 import 零副作用（不自动注册任何插件）；示例见 example_plugins()，selftest 注册后即 clear。
"""
import inspect

try:
    import factors_catalog as _catalog
except Exception:          # 允许在缺依赖的极端环境导入，一致性校验时再报
    _catalog = None

VALID_STATUS = ("live", "shadow", "research", "tracking", "archived")
VALID_DIRECTION = (-1, 0, 1)


class FactorPlugin(object):
    """一个不可变因子/策略插件（轻量、显式字段，不用第三方）。"""
    __slots__ = ("key", "name", "layer", "direction", "bound", "status", "compute", "meta")

    def __init__(self, key, compute, name=None, layer="自定义", direction=0,
                 bound=None, status="research", meta=None):
        if not isinstance(key, str) or not key:
            raise ValueError("plugin key 必须是非空字符串")
        if not callable(compute):
            raise ValueError("plugin %r 的 compute 必须可调用" % key)
        if direction not in VALID_DIRECTION:
            raise ValueError("plugin %r direction 必须是 -1/0/+1" % key)
        if status not in VALID_STATUS:
            raise ValueError("plugin %r status 非法: %r" % (key, status))
        if bound is not None:
            if (not isinstance(bound, (tuple, list)) or len(bound) != 2
                    or bound[0] is None or bound[1] is None or bound[0] > bound[1]):
                raise ValueError("plugin %r bound 必须是 (lo,hi) 且 lo<=hi" % key)
        # 显式拒绝可变默认共享
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "name", name or key)
        object.__setattr__(self, "layer", layer)
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "bound", tuple(bound) if bound is not None else None)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "compute", compute)
        object.__setattr__(self, "meta", dict(meta or {}))

    def __setattr__(self, k, v):
        raise AttributeError("FactorPlugin 不可变")

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__ if k != "compute"}


def make_plugin(key, compute, **kw):
    """构造 FactorPlugin 的便捷函数（kw 见 FactorPlugin 字段）。"""
    return FactorPlugin(key, compute, **kw)


def wrap_function(key, fn, **kw):
    """把一个现成的纯函数 fn(context)->number 包成插件（适配器，不改 fn）。"""
    if not callable(fn):
        raise ValueError("wrap_function 需要可调用对象")
    kw.setdefault("name", getattr(fn, "__name__", key))
    return make_plugin(key, fn, **kw)


# =========================== 注册表（进程内、有序、可测） ===========================
_REGISTRY = {}


def register(plugin, replace=False):
    """注册插件；key 重复且未 replace 抛 ValueError。返回插件 key。"""
    if not isinstance(plugin, FactorPlugin):
        raise ValueError("register 只接受 FactorPlugin（用 make_plugin/wrap_function 构造）")
    if plugin.key in _REGISTRY and not replace:
        raise ValueError("插件 key 已存在: %r（如需覆盖请 replace=True）" % plugin.key)
    _REGISTRY[plugin.key] = plugin
    return plugin.key


def unregister(key):
    """移除插件；返回是否移除过。"""
    return _REGISTRY.pop(key, None) is not None


def get(key):
    return _REGISTRY.get(key)


def names(status=None, layer=None):
    """按 status/layer 过滤的已注册 key 列表（保持注册顺序）。"""
    out = []
    for k, p in _REGISTRY.items():
        if status is not None and p.status != status:
            continue
        if layer is not None and p.layer != layer:
            continue
        out.append(k)
    return out


def all_plugins():
    return list(_REGISTRY.values())


def clear():
    """清空注册表（主要给测试/宿主重建用）。"""
    _REGISTRY.clear()


# =========================== 隔离求值 ===========================
def evaluate(context, key):
    """求单个插件；未注册返 (None,'missing')，compute 抛错隔离为 (None, 错误信息)。"""
    p = _REGISTRY.get(key)
    if p is None:
        return None, "missing"
    try:
        val = p.compute(context if context is not None else {})
        return val, None
    except Exception as exc:           # 研究插件失败绝不允许拖垮宿主
        return None, "%s: %s" % (type(exc).__name__, exc)


def evaluate_all(context=None, keys=None, status=None, collect_errors=True):
    """按顺序求多个插件，返回 (values{key:val}, errors{key:msg})。

    keys=None 时取（可按 status 过滤后的）全部注册插件；单个插件异常被隔离，值记 None。
    """
    if keys is None:
        keys = names(status=status)
    values, errors = {}, {}
    for k in keys:
        val, err = evaluate(context, k)
        values[k] = val
        if err and collect_errors:
            errors[k] = err
    return values, errors


# =========================== 与 factors_catalog 的一致性校验 ===========================
def check_registry_vs_catalog(plugins=None):
    """返回问题列表（空=通过）：live 插件 key 必须是综合分 PART_KEYS；非 external 插件 key 应在 CATALOG 登记。"""
    problems = []
    items = list(plugins) if plugins is not None else all_plugins()
    if _catalog is None:
        return ["factors_catalog 不可用，无法核对"]
    cat_keys = {c["key"] for c in _catalog.CATALOG}
    part_keys = set(_catalog.PART_KEYS)
    for p in items:
        if p.status == "live" and p.key not in part_keys:
            problems.append("live 插件 %r 不在综合分 PART_KEYS，禁止以 live 进分" % p.key)
        if p.key not in cat_keys and not p.meta.get("external"):
            problems.append("插件 %r 未在 factors_catalog.CATALOG 登记（或显式 meta.external=True）" % p.key)
        if p.status == "live" and p.direction not in VALID_DIRECTION:
            problems.append("live 插件 %r direction 非法" % p.key)
    return problems


def ordered_live_keys():
    """live 插件按 factors_catalog.PART_KEYS 的规范顺序返回（未在 PART 的 live 会被 check 拦下）。"""
    if _catalog is None:
        return sorted(names(status="live"))
    order = {k: i for i, k in enumerate(_catalog.PART_KEYS)}
    live = names(status="live")
    return sorted(live, key=lambda k: order.get(k, len(order)))


def example_plugins():
    """两个**研究态**示例插件（不进综合分），演示函数式与带状态两种写法；调用方自行 register/clear。"""
    def _ret5(ctx):
        c = ctx.get("close")
        if not c or len(c) < 6:
            return None
        return c[-1] / c[-6] - 1.0

    def _turnover(ctx):
        v, oi = ctx.get("volume"), ctx.get("oi")
        if v is None or not oi:
            return None
        return v / oi
    return [
        make_plugin("plugin_demo_ret5", _ret5, name="示例·5日收益", layer="表达式研究",
                    direction=1, status="research", meta={"external": True}),
        make_plugin("plugin_demo_turnover", _turnover, name="示例·投机度", layer="量仓",
                    direction=0, bound=(0.0, 50.0), status="research", meta={"external": True}),
    ]


# =========================== 离线自测 ===========================
def selftest():
    clear()
    # 1) 构造期校验：key/compute/direction/status/bound
    for bad in (lambda: make_plugin("", lambda c: 1),
                lambda: make_plugin("k", None),
                lambda: make_plugin("k", lambda c: 1, direction=2),
                lambda: make_plugin("k", lambda c: 1, status="bogus"),
                lambda: make_plugin("k", lambda c: 1, bound=(1, 0))):
        try:
            bad(); assert False, "应当校验失败"
        except ValueError:
            pass
    # 2) 不可变
    p = make_plugin("a", lambda c: 1)
    try:
        p.key = "b"; assert False
    except AttributeError:
        pass
    # 3) 注册/重复/覆盖/过滤/注销
    register(make_plugin("a", lambda c: 2, status="research", meta={"external": True}))
    register(make_plugin("b", lambda c: 3, status="shadow", meta={"external": True}))
    try:
        register(make_plugin("a", lambda c: 9)); assert False
    except ValueError:
        pass
    assert names() == ["a", "b"] and names(status="shadow") == ["b"]
    register(make_plugin("a", lambda c: 9, meta={"external": True}), replace=True)
    assert evaluate({}, "a")[0] == 9
    assert unregister("b") and not unregister("b") and get("b") is None
    # 4) 隔离求值：抛错插件不拖垮其它；缺 context 安全；missing
    register(make_plugin("boom", lambda c: 1 / 0, meta={"external": True}))
    register(make_plugin("ok", lambda c: c.get("x", 0) + 1, meta={"external": True}))
    vals, errs = evaluate_all({"x": 4})
    assert vals["ok"] == 5 and vals["boom"] is None and "ZeroDivision" in errs["boom"]
    assert evaluate({}, "nope") == (None, "missing")
    vals2, _ = evaluate_all(None, keys=["ok"])
    assert vals2["ok"] == 1
    # 5) wrap_function 适配器
    def f(ctx):
        return 42
    register(wrap_function("wf", f, meta={"external": True}), replace=True)
    assert evaluate({}, "wf")[0] == 42
    # 6) catalog 一致性：external 研究插件不报错；伪 live 非 PART key 必被拦
    assert check_registry_vs_catalog() == []
    bad_live = [make_plugin("不是综合分项", lambda c: 1, status="live")]
    probs = check_registry_vs_catalog(bad_live)
    assert probs and any("PART_KEYS" in x for x in probs)
    unreg_live = [make_plugin("未登记key", lambda c: 1, status="research")]
    assert any("CATALOG" in x for x in check_registry_vs_catalog(unreg_live))
    # 7) example_plugins 可注册、可求值、可清
    for pl in example_plugins():
        register(pl, replace=True)
    v, e = evaluate({"close": [100.0] * 5 + [105.0], "volume": 80, "oi": 100}, "plugin_demo_ret5")
    assert abs(v - 0.05) < 1e-12 and not e
    v2, _ = evaluate({"volume": 80, "oi": 100}, "plugin_demo_turnover")
    assert abs(v2 - 0.8) < 1e-12
    # 8) ordered_live_keys 遵循 PART_KEYS 顺序（逆序注册也应排回规范序）
    if _catalog is not None:
        pk = list(_catalog.PART_KEYS)
        clear()
        register(make_plugin(pk[2], lambda c: 0, status="live"))
        register(make_plugin(pk[0], lambda c: 0, status="live"))
        register(make_plugin(pk[1], lambda c: 0, status="live"))
        assert ordered_live_keys() == [pk[0], pk[1], pk[2]]
    clear()
    assert names() == []
    print("factor_plugin selftest ALL PASS（8组）")
    return 0
