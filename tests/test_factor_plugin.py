# -*- coding: utf-8 -*-
"""第57轮 G2第一切片 factor_plugin 插件宿主的单测：契约校验/注册表/隔离求值/catalog一致性/主链零接入。"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import factor_plugin as fp       # noqa: E402


@pytest.fixture(autouse=True)
def _clean_registry():
    fp.clear()
    yield
    fp.clear()


def test_construction_validation():
    fp.make_plugin("k", lambda c: 1)
    with pytest.raises(ValueError):
        fp.make_plugin("", lambda c: 1)
    with pytest.raises(ValueError):
        fp.make_plugin("k", None)
    with pytest.raises(ValueError):
        fp.make_plugin("k", lambda c: 1, direction=2)
    with pytest.raises(ValueError):
        fp.make_plugin("k", lambda c: 1, status="nope")
    with pytest.raises(ValueError):
        fp.make_plugin("k", lambda c: 1, bound=(1, 0))
    # bound 合法
    pl = fp.make_plugin("k", lambda c: 1, bound=(-1, 1))
    assert pl.bound == (-1, 1) and pl.status == "research"


def test_immutable():
    pl = fp.make_plugin("k", lambda c: 1)
    with pytest.raises(AttributeError):
        pl.key = "x"


def test_register_duplicate_replace_filter():
    fp.register(fp.make_plugin("a", lambda c: 1, meta={"external": True}))
    with pytest.raises(ValueError):
        fp.register(fp.make_plugin("a", lambda c: 2))
    fp.register(fp.make_plugin("a", lambda c: 2, meta={"external": True}), replace=True)
    assert fp.evaluate({}, "a")[0] == 2
    fp.register(fp.make_plugin("b", lambda c: 3, status="shadow", meta={"external": True}))
    assert fp.names() == ["a", "b"] and fp.names(status="shadow") == ["b"]
    assert fp.unregister("b") and not fp.unregister("b")


def test_evaluate_isolation():
    fp.register(fp.make_plugin("boom", lambda c: 1 / 0, meta={"external": True}))
    fp.register(fp.make_plugin("ok", lambda c: c.get("x", 0) + 1, meta={"external": True}))
    vals, errs = fp.evaluate_all({"x": 4})
    assert vals["ok"] == 5 and vals["boom"] is None
    assert "ZeroDivision" in errs["boom"]
    assert fp.evaluate({}, "missing") == (None, "missing")
    # context=None 安全（宿主补空 dict）
    vals2, _ = fp.evaluate_all(None, keys=["ok"])
    assert vals2["ok"] == 1


def test_wrap_function():
    def f(ctx):
        return 7
    fp.register(fp.wrap_function("wf", f, meta={"external": True}))
    assert fp.evaluate({}, "wf") == (7, None)


def test_catalog_conformance():
    # external 研究插件：无问题
    for pl in fp.example_plugins():
        fp.register(pl)
    assert fp.check_registry_vs_catalog() == []
    # 非 PART_KEYS 的伪 live 必被拦
    bad = [fp.make_plugin("伪分项", lambda c: 0, status="live")]
    probs = fp.check_registry_vs_catalog(bad)
    assert any("PART_KEYS" in x for x in probs)
    # 未登记且非 external 被拦
    unreg = [fp.make_plugin("未登记", lambda c: 0, status="research")]
    assert any("CATALOG" in x for x in fp.check_registry_vs_catalog(unreg))


def test_ordered_live_keys_follow_part_order():
    pk = list(__import__("factors_catalog").PART_KEYS)
    fp.register(fp.make_plugin(pk[2], lambda c: 0, status="live"))
    fp.register(fp.make_plugin(pk[0], lambda c: 0, status="live"))
    fp.register(fp.make_plugin(pk[1], lambda c: 0, status="live"))
    assert fp.ordered_live_keys() == [pk[0], pk[1], pk[2]]


def test_example_plugins_values():
    for pl in fp.example_plugins():
        fp.register(pl, replace=True)
    v, err = fp.evaluate({"close": [100.0] * 5 + [105.0]}, "plugin_demo_ret5")
    assert err is None and abs(v - 0.05) < 1e-12
    v2, _ = fp.evaluate({"volume": 80, "oi": 100}, "plugin_demo_turnover")
    assert abs(v2 - 0.8) < 1e-12


def test_main_chain_plugin_boundary():
    """G2 切片铁律：main.py 永不接插件层；analyzer 第60轮最后一切片起仅允许函数内惰性 import、受默认关开关门控。"""
    main_src = open(os.path.join(_ROOT, "main.py", ), "r", encoding="utf-8").read()
    assert "factor_plugin" not in main_src and "factor_parts" not in main_src
    az = open(os.path.join(_ROOT, "analyzer.py"), "r", encoding="utf-8").read()
    # 不得有模块顶层 import（行首无缩进）插件层
    for line in az.splitlines():
        if line.startswith(("import ", "from ")):
            assert "factor_plugin" not in line and "factor_parts" not in line
    # 最后一切片必须有开关（第61轮起默认开）与回退 helper
    import config
    assert config.PLUGIN_PARTS_ENABLED is True and "_parts_via_plugins" in az
