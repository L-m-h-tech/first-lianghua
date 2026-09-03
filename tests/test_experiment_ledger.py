# -*- coding: utf-8 -*-
"""G27①（第44轮）统一实验台账 零网络确定性测试（experiment_ledger.py）。

不连任何 DB/网络、不读真实 reports（全部 tmp_path 合成）：
  - 规范化 config_hash：键序无关、参数/类型/数据内容敏感、同配置两次一致（验收点）
  - json_safe：非有限浮点/时间/集合/键类型清洗
  - 文件指纹与数据身份：缺失安全、内容身份排除 mtime、内容变才变
  - 记录构造：字段齐全、run_id 形态、reproduce 可关
  - JSONL 台账：追加回读、repeat_of 串联、同秒碰撞 -r2、坏行宽容、filter
  - safe_record：成功落盘 / 不可写路径安全吞掉
  - 文本渲染与 CLI：--list/--show/--repeats/--export/找不到 run
"""
import datetime as dt
import io
import json
import os
import time

import experiment_ledger as el

FIXED = dt.datetime(2026, 9, 3, 15, 0, 0)


# ---------------- 规范化哈希 ----------------
def test_canonical_hash_key_order_independent():
    h1 = el.canonical_hash("exp", {"b": 1, "a": [1, 2], "c": {"x": 1}}, {})
    h2 = el.canonical_hash("exp", {"c": {"x": 1}, "a": [1, 2], "b": 1}, {})
    assert h1 == h2 and len(h1) == el.CONFIG_HASH_LEN


def test_canonical_hash_sensitive():
    base = el.canonical_hash("exp", {"w": 126}, {})
    assert el.canonical_hash("exp", {"w": 63}, {}) != base
    assert el.canonical_hash("exp2", {"w": 126}, {}) != base
    # 数据身份不同也应不同
    assert el.canonical_hash("exp", {"w": 126}, {"f": {"sha256": "aa"}}) != \
        el.canonical_hash("exp", {"w": 126}, {"f": {"sha256": "bb"}})


def test_same_config_twice_same_hash_even_different_now():
    """G27 验收：同配置两次实验 hash 一致，与运行时刻无关。"""
    r1 = el.make_record("lab", {"w": 1}, now=FIXED, reproduce=False)
    r2 = el.make_record("lab", {"w": 1}, now=FIXED + dt.timedelta(hours=2), reproduce=False)
    assert r1["config_hash"] == r2["config_hash"]
    assert r1["run_id"] != r2["run_id"]            # 时间不同 → run_id 不同，但配置身份相同


def test_json_safe_sanitizes():
    out = el.json_safe({"nan": float("nan"), "inf": float("inf"), "t": dt.date(2026, 9, 3),
                        "s": {1, 2}, "nested": {"x": float("-inf")}})
    assert out["nan"] is None and out["inf"] is None and out["nested"]["x"] is None
    assert out["t"] == "2026-09-03" and out["s"] == [1, 2]
    assert el.canonical_bytes({"x": float("nan")}) == el.canonical_bytes({"x": None})


# ---------------- 文件指纹 / 数据身份 ----------------
def test_file_fingerprint_existing_and_missing(tmp_path):
    p = tmp_path / "in.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8", newline="\n")
    fp = el.file_fingerprint(str(p))
    assert fp["exists"] and fp["size"] == 8 and fp["sha256"] and fp["mtime_iso"]
    miss = el.file_fingerprint(str(tmp_path / "nope.csv"))
    assert miss["exists"] is False and miss["sha256"] is None


def test_data_identity_excludes_mtime(tmp_path):
    p = tmp_path / "in.csv"
    p.write_text("same", encoding="utf-8", newline="\n")
    id1 = el.data_identity_from_manifest(el.build_manifest([str(p)]))
    time.sleep(1.05)
    p.write_text("same", encoding="utf-8", newline="\n")     # 内容相同、mtime 变
    id2 = el.data_identity_from_manifest(el.build_manifest([str(p)]))
    assert id1 == id2
    p.write_text("different-content", encoding="utf-8", newline="\n")
    id3 = el.data_identity_from_manifest(el.build_manifest([str(p)]))
    assert id3 != id1


def test_build_manifest_dedup_and_missing(tmp_path):
    p = tmp_path / "a"
    p.write_text("x", encoding="utf-8")
    man = el.build_manifest([str(p), str(p), str(tmp_path / "miss")])
    assert len(man) == 2                                      # 去重 + 缺失各一条
    assert all(isinstance(v, dict) for v in man.values())


# ---------------- 记录构造 ----------------
def test_make_record_fields(tmp_path):
    p = tmp_path / "src.csv"
    p.write_text("1", encoding="utf-8")
    rec = el.make_record("lab", {"w": 126}, {"sharpe": 0.5}, inputs=[str(p)],
                         artifacts=[], now=FIXED, reproduce=False)
    assert rec["run_id"].startswith("20260903-150000-")
    assert rec["experiment"] == "lab" and rec["repeat_of"] is None
    assert rec["reproduce"] is None and rec["version"] is not None
    assert set(rec) >= {"run_id", "created_at", "config_hash", "params", "metrics",
                        "inputs", "artifacts", "data_identity", "version", "py"}
    json.dumps(rec, allow_nan=False)                          # 无 NaN


def test_make_record_default_reproduce():
    rec = el.make_record("x", {}, now=FIXED)
    assert isinstance(rec["reproduce"], str)


# ---------------- 台账存取 ----------------
def test_append_and_repeat_link(tmp_path):
    led = str(tmp_path / "ledger.jsonl")
    st = el.LedgerStore(led)
    a = st.append(el.make_record("lab", {"w": 126}, {"s": 0.4}, now=FIXED))
    b = st.append(el.make_record("lab", {"w": 126}, {"s": 0.5}, now=FIXED + dt.timedelta(minutes=5)))
    c = st.append(el.make_record("lab", {"w": 63}, {"s": 0.4}, now=FIXED + dt.timedelta(minutes=10)))
    assert b["config_hash"] == a["config_hash"] and b["repeat_of"] == a["run_id"]
    assert c["repeat_of"] is None
    assert len(st.load_all()) == 3


def test_run_id_collision_gets_suffix(tmp_path):
    led = str(tmp_path / "ledger.jsonl")
    st = el.LedgerStore(led)
    a = st.append(el.make_record("lab", {"w": 1}, now=FIXED))
    b = st.append(el.make_record("lab", {"w": 1}, now=FIXED))   # 同秒同配置
    assert a["run_id"] != b["run_id"] and b["run_id"].endswith("-r2")


def test_load_tolerates_bad_lines(tmp_path):
    led = tmp_path / "ledger.jsonl"
    st = el.LedgerStore(str(led))
    st.append(el.make_record("lab", {"w": 1}, now=FIXED))
    with io.open(str(led), "a", encoding="utf-8", newline="\n") as f:
        f.write("\n{broken,,,}\n")
    st2 = el.LedgerStore(str(led))
    recs = st2.load_all()
    assert len(recs) == 1 and st2.bad_lines == 1


def test_missing_ledger_empty(tmp_path):
    st = el.LedgerStore(str(tmp_path / "none.jsonl"))
    assert st.load_all() == [] and st.bad_lines == 0


def test_filter_experiment_and_limit(tmp_path):
    led = str(tmp_path / "l.jsonl")
    st = el.LedgerStore(led)
    for i in range(3):
        st.append(el.make_record("a", {"i": i}, now=FIXED + dt.timedelta(minutes=i)))
    st.append(el.make_record("b", {}, now=FIXED))
    assert len(st.filter(experiment="a")) == 3
    assert st.filter(experiment="b")[0]["experiment"] == "b"
    assert len(st.filter(limit=2)) == 2


def test_ledger_atomic_lf_lines(tmp_path):
    led = str(tmp_path / "l.jsonl")
    st = el.LedgerStore(led)
    st.append(el.make_record("a", {}, now=FIXED))
    raw = io.open(led, "rb").read()
    assert raw.endswith(b"\n") and b"\r\n" not in raw          # LF、无 CRLF
    for line in raw.decode("utf-8").splitlines():
        assert isinstance(json.loads(line), dict)


# ---------------- safe_record ----------------
def test_safe_record_ok(tmp_path):
    led = str(tmp_path / "s.jsonl")
    rec = el.safe_record("lab", {"w": 1}, {"s": 0.1}, now=FIXED, ledger_path=led)
    assert rec and rec["run_id"] and os.path.isfile(led)


def test_safe_record_swallows_failure(tmp_path):
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    bad = el.safe_record("lab", {"w": 1}, now=FIXED,
                         ledger_path=str(tmp_path / "afile" / "sub" / "l.jsonl"))
    assert bad is None


# ---------------- 渲染 ----------------
def test_format_outputs():
    recs = [el.make_record("lab", {"w": 126}, {"erc": {"sharpe": 0.55}}, now=FIXED, reproduce=False),
            el.make_record("lab", {"w": 126}, {"erc": {"sharpe": 0.6}},
                           now=FIXED + dt.timedelta(minutes=5), reproduce=False)]
    recs[1]["repeat_of"] = recs[0]["run_id"]
    lst = el.format_list(recs)
    assert "统一实验台账" in lst and "↻" in lst
    one = el.format_record(recs[0])
    assert "参数" in one and "关键指标" in one and "输入数据身份" in one
    rep = el.format_repeats(recs)
    assert "×2" in rep
    assert el.format_list([]).startswith("（台账为空")
    assert "无重复实验" in el.format_repeats([{"config_hash": "z", "run_id": "1", "experiment": "x"}])


def test_metric_flat_and_num():
    s = el._metric_flat({"equal": {"sharpe": 0.42}, "erc": {"sharpe": 0.55}})
    assert "equal.sharpe=0.42" in s and "erc.sharpe=0.55" in s
    assert el._num(0.123456) == "0.1235" and el._num(12345) == "12345"


# ---------------- CLI ----------------
def test_cli_list_show_repeats_export(tmp_path):
    led = str(tmp_path / "l.jsonl")
    st = el.LedgerStore(led)
    a = st.append(el.make_record("lab", {"w": 1}, now=FIXED))
    st.append(el.make_record("lab", {"w": 1}, now=FIXED + dt.timedelta(minutes=5)))
    assert el.run(["--ledger", led, "--list"]) == 0
    assert el.run(["--ledger", led, "--repeats"]) == 0
    assert el.run(["--ledger", led, "--show", a["run_id"]]) == 0
    assert el.run(["--ledger", led, "--show", "no_such"]) == 1
    exp = str(tmp_path / "out.json")
    assert el.run(["--ledger", led, "--export", exp]) == 0
    assert len(json.load(io.open(exp, encoding="utf-8"))) == 2


def test_cli_list_empty_ledger(tmp_path):
    assert el.run(["--ledger", str(tmp_path / "e.jsonl"), "--list"]) == 0


# ---------------- 环境变量重定向/关闭 ----------------
def test_env_disable_and_redirect(monkeypatch, tmp_path):
    monkeypatch.setenv(el.ENV_LEDGER, "off")
    assert el.get_default_ledger_path() is None
    assert el.safe_record("lab", {"a": 1}, now=FIXED) is None
    assert el.LedgerStore().disabled and el.LedgerStore().load_all() == []
    p = str(tmp_path / "env.jsonl")
    monkeypatch.setenv(el.ENV_LEDGER, p)
    rec = el.safe_record("lab", {"a": 1}, now=FIXED)
    assert rec and os.path.isfile(p)
