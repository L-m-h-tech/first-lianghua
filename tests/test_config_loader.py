# -*- coding: utf-8 -*-
"""G10 配置外置加载器零网络回归。"""
import json
import os
import subprocess
import sys

import config_loader as cl


# ---------- parse_dotenv ----------
def test_parse_dotenv_basic():
    text = "# c\nexport FOO=bar\nQ=\"hello world\"\nS='x'\nEMPTY=\nbadline\n"
    d = cl.parse_dotenv(text)
    assert d == {"FOO": "bar", "Q": "hello world", "S": "x", "EMPTY": ""}


def test_parse_dotenv_blank():
    assert cl.parse_dotenv("\n  # note\n") == {}


def test_load_dotenv_no_override_existing(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=file\nB=new\n", encoding="utf-8")
    env = {"A": "real"}
    inj, skip = cl.load_dotenv(str(p), env)
    assert (inj, skip) == (1, 1)
    assert env["A"] == "real" and env["B"] == "new"


def test_load_dotenv_missing(tmp_path):
    assert cl.load_dotenv(str(tmp_path / "no.env"), {}) == (0, 0)


# ---------- deep_merge ----------
def test_deep_merge_recursive_and_non_destructive():
    base = {"a": 1, "b": {"x": 1, "y": 2}}
    out = cl.deep_merge(base, {"b": {"y": 9, "z": 3}, "c": 4})
    assert out == {"a": 1, "b": {"x": 1, "y": 9, "z": 3}, "c": 4}
    assert base["b"] == {"x": 1, "y": 2}  # 不改原 dict


def test_deep_merge_non_dict_replaces():
    assert cl.deep_merge({"a": {"x": 1}}, {"a": 5}) == {"a": 5}


# ---------- coerce_value ----------
def test_coerce_int_rules():
    assert cl.coerce_value(3, 1) == (True, 3)
    assert cl.coerce_value(3.0, 1) == (True, 3)
    assert cl.coerce_value(3.5, 1) == (False, None)
    assert cl.coerce_value(True, 1) == (False, None)
    assert cl.coerce_value("3", 1) == (False, None)


def test_coerce_bool_before_int():
    assert cl.coerce_value(True, False) == (True, True)
    assert cl.coerce_value(1, False) == (False, None)


def test_coerce_float_str_tuple_list_dict():
    assert cl.coerce_value(2, 1.0) == (True, 2.0)
    assert cl.coerce_value("x", "d") == (True, "x")
    assert cl.coerce_value(1, "d") == (False, None)
    ok, v = cl.coerce_value([1, 2], (0,))
    assert ok and v == (1, 2)
    ok, v = cl.coerce_value((1,), [])
    assert ok and v == [1]
    assert cl.coerce_value("bad", []) == (False, None)
    assert cl.coerce_value({"k": 1}, {}) == (True, {"k": 1})
    assert cl.coerce_value(1, {}) == (False, None)


# ---------- apply_overrides ----------
def _ns():
    return {"INT": 60, "FLT": 1.5, "FLAG": True, "TUP": (1, 2), "D": {"x": 1},
            "MONITOR_DB": "C:/x.db", "BASE_DIR": "C:/", "FUNC": lambda: 0, "lower": 1}


def test_apply_overrides_applies_valid():
    ns = _ns()
    rep = cl.apply_overrides(ns, {"INT": 30, "FLT": 2, "FLAG": False,
                                  "TUP": [3, 4], "D": {"y": 2}})
    assert ns["INT"] == 30 and isinstance(ns["FLT"], float) and ns["FLT"] == 2.0
    assert ns["FLAG"] is False and ns["TUP"] == (3, 4)
    assert ns["D"] == {"x": 1, "y": 2}
    assert set(rep["applied"]) == {"INT", "FLT", "FLAG", "TUP", "D"}


def test_apply_overrides_protects_and_skips():
    ns = _ns()
    rep = cl.apply_overrides(ns, {"MONITOR_DB": "hack", "BASE_DIR": "hack",
                                  "FUNC": 1, "lower": 9, "UNKNOWN": 1, "INT": "bad"})
    assert ns["MONITOR_DB"] == "C:/x.db" and ns["BASE_DIR"] == "C:/"
    assert callable(ns["FUNC"]) and ns["lower"] == 1 and ns["INT"] == 60
    for k in ("MONITOR_DB", "BASE_DIR", "FUNC", "lower", "UNKNOWN", "INT"):
        assert k in rep["skipped"]
    assert rep["applied"] == {}


def test_apply_overrides_non_object_root():
    rep = cl.apply_overrides(_ns(), ["not", "dict"])
    assert "__root__" in rep["skipped"]


# ---------- load_config_file ----------
def test_load_config_file_missing(tmp_path):
    obj, err = cl.load_config_file(str(tmp_path / "x.json"))
    assert obj is None and err is None


def test_load_config_file_bad_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    obj, err = cl.load_config_file(str(p))
    assert obj is None and err


def test_load_config_file_ok(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text(json.dumps({"NEWS_INTERVAL": 42}), encoding="utf-8")
    obj, err = cl.load_config_file(str(p))
    assert obj == {"NEWS_INTERVAL": 42} and err is None


# ---------- 端到端：子进程以 FUTURES_MONITOR_CONFIG 加载 config 模块 ----------
def test_config_module_end_to_end(tmp_path):
    cfg = {"NEWS_INTERVAL": 42, "RISK_GATE_ENABLED": False,
           "SIGNAL_OUTCOME_HORIZONS": [15, 60],
           "WEB_MACRO_THRESHOLDS": {"美元指数": 0.99},
           "MONITOR_DB": "protected", "NO_SUCH_KEY": 1}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    code = ("import config;"
            "assert config.NEWS_INTERVAL==42;"
            "assert config.RISK_GATE_ENABLED is False;"
            "assert config.SIGNAL_OUTCOME_HORIZONS==(15,60);"
            "assert config.WEB_MACRO_THRESHOLDS['美元指数']==0.99;"
            "assert config.MONITOR_DB.endswith('monitor.db');"
            "assert 'NO_SUCH_KEY' in config.CONFIG_OVERRIDE_REPORT['skipped'];"
            "print('OK')")
    env = dict(os.environ)
    env["FUTURES_MONITOR_CONFIG"] = str(p)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, cwd=root)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout
