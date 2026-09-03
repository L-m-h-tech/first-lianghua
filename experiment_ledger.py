# -*- coding: utf-8 -*-
r"""G27①（第44轮）统一实验台账 experiment_ledger.py：纯标准库、零网络、根模块（不被 main import）。

背景（总纲 G27「统一实验台账 + walk-forward 稳定性/成本敏感性」的第一切片）：
研究/回测侧实验（portfolio_lab / trade_journal / research_review / portfolio --compare-risk、
后续 walk-forward 与成本敏感性等）此前各自落 reports/*.txt|.json|.csv，靠文件名与时间戳区分，
"同参数是否重跑过、结果漂了多少、用哪份数据跑的、一键复现命令是什么"没有统一登记处；生产库
backtest_runs 只登记日线回测且属 storage 主链（研究工具纪律=只读生产库，不往里写）。

本模块只做"登记与查询"这一件事（MLflow 只借台账思想、不引服务；vectorbt 只学实验组织、不引
numba/numpy）：
- 追加式 JSONL 台账 reports/experiment_runs.jsonl（gitignore 的运行产物，人工实验日志，绝不覆盖
  任何既有报告/CSV，主链与综合分零改动）；
- 每条记录登记：实验类型/规范化参数/输入数据指纹/关键指标/产物清单/结论/一键复现命令/版本；
- config_hash=对「实验类型 + 参数 + 输入数据内容身份」做规范化 SHA256（**键序无关、不含 mtime**），
  故同配置重跑（哪怕输入文件被重新生成、只要内容逐字节不变）hash 一致，可识别重复与指标漂移；
- append 时若同 config_hash 已存在，写 repeat_of=上一条 run_id（两条都保留、串联不覆盖）。

纪律（照 G21–G30 研究侧惯例与总纲第15条零重依赖红线）：
- 纯标准库、零网络、不 import 任何 tools、不被 main/analyzer import；
- 只新增登记能力与查询 CLI，不改任何既有回测/研究产物的内容与口径；
- 宿主工具一律走 safe_record（内部全 try，台账失败只返回 None，绝不拖垮宿主）；
- JSON 一律 allow_nan=False（非有限浮点在 json_safe 阶段转 None），写文件 utf-8、LF。

G27②walk-forward 滚动评估、③成本敏感性曲面/换手容量留后续轮次，本模块为其预留登记入口。
"""
import argparse
import hashlib
import io
import json
import math
import os
import sys
import threading
import datetime as _dt

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LEDGER = os.path.join(_HERE, "reports", "experiment_runs.jsonl")
VERSION_FILE = os.path.join(_HERE, "VERSION")

# 环境变量重定向/关闭台账（测试隔离用：conftest 会把它指到临时文件，保证测试里的 run() 钩子不写真实 reports）
ENV_LEDGER = "FUTURES_EXPERIMENT_LEDGER"
DISABLE_TOKENS = {"", "0", "off", "false", "none", "disable", "disabled"}

# 输入文件小于此大小才计算内容 sha256（避免对分钟库/大 CSV/DB 全量哈希）；超过只登记大小
MAX_HASH_BYTES = 2 * 1024 * 1024
HASH_HEAD_TAIL = 64 * 1024          # 大文件改用 头+尾 采样指纹的阈值保留位（当前仅登记大小）
CONFIG_HASH_LEN = 16                # config_hash 取 16 位十六进制（碰撞概率对人工台账可忽略）

SEP = "=" * 96


# =========================== 基础工具 ===========================
def _now():
    return _dt.datetime.now()


def read_version():
    """读 VERSION 文件（如 0.44.0）；不 subprocess 调 git，缺失/损坏安全返回 None。"""
    try:
        with io.open(VERSION_FILE, "r", encoding="utf-8") as f:
            v = f.read().strip()
        return v or None
    except Exception:
        return None


def json_safe(o):
    """把任意嵌套结构转成 JSON 可序列化对象：非有限浮点→None、datetime→字符串、set/tuple→list、
    字典键转 str；与各研究工具 _json_safe 同纪律（allow_nan=False 的前置清洗）。"""
    if isinstance(o, dict):
        return {str(k): json_safe(v) for k, v in sorted(o.items(), key=lambda kv: str(kv[0]))}
    if isinstance(o, (list, tuple)):
        return [json_safe(v) for v in o]
    if isinstance(o, (set, frozenset)):
        return [json_safe(v) for v in sorted(o, key=lambda x: str(x))]
    if isinstance(o, (_dt.datetime, _dt.date)):
        return o.strftime("%Y-%m-%d %H:%M:%S") if isinstance(o, _dt.datetime) else o.strftime("%Y-%m-%d")
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if o is None or isinstance(o, (bool, int, str)):
        return o
    return str(o)


def canonical_bytes(obj):
    """规范化序列化：排序键、紧凑分隔、ensure_ascii=False → UTF-8 字节。
    同一逻辑对象无论 dict 键的插入顺序、数字 int/long 形态如何，字节恒一致。"""
    safe = json_safe(obj)
    text = json.dumps(safe, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
    return text.encode("utf-8")


def canonical_hash(experiment, params, data_identity):
    """配置身份哈希：只取决于 实验类型 + 规范化参数 + 输入数据【内容身份】。
    刻意不含运行时间、文件 mtime、产物，保证'同配置两次实验 hash 一致'（G27 验收点）。"""
    payload = {"experiment": str(experiment), "params": json_safe(params or {}),
               "data_identity": json_safe(data_identity or {})}
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()[:CONFIG_HASH_LEN]


def file_fingerprint(path, max_hash_bytes=MAX_HASH_BYTES):
    """单个输入/产物文件指纹：exists/size/mtime/mtime_iso；小文件额外给 sha256（内容身份用）。
    路径不存在返回 exists=False（不抛错）；任何异常软降级为 exists=False + error。"""
    fp = {"path": os.path.abspath(path) if path else None,
          "name": os.path.basename(str(path)) if path else None,
          "exists": False, "size": None, "mtime": None, "mtime_iso": None, "sha256": None}
    try:
        if not path or not os.path.isfile(path):
            return fp
        st = os.stat(path)
        fp["exists"] = True
        fp["size"] = int(st.st_size)
        fp["mtime"] = round(float(st.st_mtime), 3)
        fp["mtime_iso"] = _dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        if st.st_size <= max_hash_bytes:
            h = hashlib.sha256()
            with io.open(path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            fp["sha256"] = h.hexdigest()
    except Exception as e:  # 指纹绝不能拖垮登记
        fp["error"] = "%s: %s" % (type(e).__name__, e)
    return fp


def build_manifest(paths, max_hash_bytes=MAX_HASH_BYTES):
    """对一组输入/产物路径批量取指纹，返回 {绝对路径: 指纹}；None/空路径与重复自动跳过。"""
    out = {}
    for p in paths or []:
        if not p:
            continue
        ap = os.path.abspath(p)
        if ap in out:
            continue
        out[ap] = file_fingerprint(ap, max_hash_bytes=max_hash_bytes)
    return out


def data_identity_from_manifest(manifest):
    """从输入指纹提取【内容身份】：有 sha256 用 sha256，否则退化为 size；刻意排除 mtime。
    这样：输入被逐字节重写（mtime 变、内容不变）→ 身份不变、config_hash 不变。"""
    ident = {}
    for ap, fp in sorted((manifest or {}).items()):
        if not isinstance(fp, dict) or not fp.get("exists"):
            ident[ap] = {"exists": False}
            continue
        ident[ap] = {"name": fp.get("name"),
                     "sha256": fp.get("sha256"),
                     "size": fp.get("size") if fp.get("sha256") is None else None}
    return ident


# =========================== 记录构造 ===========================
def get_default_ledger_path():
    """默认台账路径解析：环境变量 FUTURES_EXPERIMENT_LEDGER 可重定向（测试隔离）；
    置空/0/off/false/none=显式关闭（返回 None，safe_record 直接跳过不登记）。"""
    v = os.environ.get(ENV_LEDGER)
    if v is None:
        return DEFAULT_LEDGER
    if v.strip().lower() in DISABLE_TOKENS:
        return None
    return v


def make_record(experiment, params, metrics=None, *, inputs=None, artifacts=None,
                conclusion=None, reproduce=None, now=None, extra=None):
    """构造一条实验记录 dict（不落盘）。config_hash 只认 实验+参数+输入内容身份；
    reproduce 传 None 自动取 sys.argv，传 False 显式不记；extra 放实验特有补充字段。"""
    now = now or _now()
    in_manifest = build_manifest(list(inputs or []))
    art_manifest = build_manifest(list(artifacts or []))
    data_id = data_identity_from_manifest(in_manifest)
    cfg_hash = canonical_hash(experiment, params, data_id)
    if reproduce is None:
        try:
            reproduce = " ".join(sys.argv)
        except Exception:
            reproduce = None
    rec = {
        "run_id": now.strftime("%Y%m%d-%H%M%S") + "-" + cfg_hash[:8],
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "experiment": str(experiment),
        "config_hash": cfg_hash,
        "repeat_of": None,
        "version": read_version(),
        "py": "%d.%d" % sys.version_info[:2],
        "params": json_safe(params or {}),
        "data_identity": data_id,
        "inputs": in_manifest,
        "artifacts": art_manifest,
        "metrics": json_safe(metrics or {}),
        "conclusion": (str(conclusion) if conclusion else None),
        "reproduce": reproduce if reproduce is not False else None,
    }
    if extra:
        for k, v in extra.items():
            rec.setdefault(k, json_safe(v))
    # 落库前预检：不得含 NaN（与全项目 sidecar 同纪律）
    json.dumps(rec, ensure_ascii=False, allow_nan=False)
    return rec


# =========================== 追加式 JSONL 台账 ===========================
class LedgerStore:
    """追加式台账：每行一条紧凑 JSON（utf-8、LF）。读时宽容（空行/坏行跳过并计数），
    写时原子替换（同目录临时文件 + os.replace），进程内 RLock 串行化。"""

    def __init__(self, path=None):
        self.path = os.path.abspath(path) if path else (get_default_ledger_path() or "")
        self._lock = threading.RLock()
        self.bad_lines = 0

    @property
    def disabled(self):
        return not self.path

    def load_all(self):
        """正序返回全部记录；关闭态/文件不存在→[]；坏行/空行跳过并累计 bad_lines（不抛错）。"""
        records, bad = [], 0
        if self.disabled or not os.path.isfile(self.path):
            self.bad_lines = 0
            return records
        with io.open(self.path, "r", encoding="utf-8-sig") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                    if isinstance(obj, dict):
                        records.append(obj)
                    else:
                        bad += 1
                except Exception:
                    bad += 1
        self.bad_lines = bad
        return records

    def find_same_config(self, config_hash, exclude_run_id=None):
        """返回同 config_hash 的最近一条历史记录（无则 None），用于 repeat_of 串联。"""
        prior = None
        for r in self.load_all():
            if r.get("config_hash") == config_hash and r.get("run_id") != exclude_run_id:
                prior = r
        return prior

    def _unique_run_id(self, record, records):
        """同一秒+同 hash 重跑会撞 run_id：追加 -r2/-r3… 保证 run_id 唯一。"""
        existing = {r.get("run_id") for r in records}
        rid = record["run_id"]
        if rid not in existing:
            return rid
        k = 2
        while "%s-r%d" % (rid, k) in existing:
            k += 1
        return "%s-r%d" % (rid, k)

    def append(self, record):
        """追加一条记录（写 repeat_of、唯一化 run_id），原子落盘，返回写入的记录；关闭态抛 RuntimeError。"""
        if self.disabled:
            raise RuntimeError("experiment ledger disabled by %s" % ENV_LEDGER)
        record = json_safe(record)
        with self._lock:
            records = self.load_all()
            prior = self.find_same_config(record.get("config_hash"))
            if prior:
                record["repeat_of"] = prior.get("run_id")
            record["run_id"] = self._unique_run_id(record, records)
            records.append(record)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = "%s.tmp.%d" % (self.path, os.getpid())
            with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
                for r in records:
                    f.write(json.dumps(r, ensure_ascii=False, sort_keys=False,
                                       separators=(",", ":"), allow_nan=False))
                    f.write("\n")
            os.replace(tmp, self.path)
            return record

    def filter(self, experiment=None, limit=None):
        records = self.load_all()
        if experiment:
            records = [r for r in records if r.get("experiment") == experiment]
        if limit:
            records = records[-int(limit):]
        return records


def safe_record(experiment, params, metrics=None, *, inputs=None, artifacts=None,
                conclusion=None, reproduce=None, now=None, ledger_path=None,
                extra=None):
    """宿主工具统一入口：构造+追加一条台账，任何异常都吞掉返回 None（台账是旁路，绝不拖垮宿主）。
    ledger_path=None 时走环境变量/默认路径解析；环境变量显式关闭则直接返回 None。"""
    try:
        path = ledger_path if ledger_path is not None else get_default_ledger_path()
        if not path:
            return None
        rec = make_record(experiment, params, metrics, inputs=inputs, artifacts=artifacts,
                          conclusion=conclusion, reproduce=reproduce, now=now, extra=extra)
        return LedgerStore(path).append(rec)
    except Exception:
        return None


# =========================== 文本渲染 ===========================
def _short(v, n=22):
    s = str(v)
    return s if len(s) <= n else s[:n - 1] + "…"


def format_list(records, *, show_repeat=True):
    """台账列表：一行一次实验（时间/类型/hash/重复/关键指标/结论摘要）。"""
    if not records:
        return "（台账为空：尚无实验登记）"
    L = [SEP, "统一实验台账 experiment_runs（共 %d 条，正序；同 config_hash=同实验类型+参数+输入内容）" % len(records),
         SEP,
         "  %-20s %-22s %-10s %-6s %s" % ("run_id", "实验", "config", "重复", "关键指标/结论")]
    for r in records:
        metrics = r.get("metrics") or {}
        mtxt = _metric_flat(metrics)
        rep = "↻" + str(r.get("repeat_of"))[:17] if r.get("repeat_of") else "—"
        head = "  %-20s %-22s %-10s %-20s %s" % (
            r.get("run_id", "")[:20], _short(r.get("experiment"), 22),
            str(r.get("config_hash", ""))[:10], rep, _short(mtxt, 60))
        L.append(head)
        if not show_repeat and r.get("repeat_of"):
            pass
    L.append(SEP)
    L.append("config_hash 相同=可复现配置；repeat 列 ↻ 指向同配置上一次 run_id，可用 --show 对比指标漂移。")
    return "\n".join(L)


def _metric_flat(metrics, max_items=4):
    """把 metrics dict（允许一层嵌套）压成 'k=v …' 的紧凑串。"""
    if not isinstance(metrics, dict) or not metrics:
        return ""
    flat = []

    def walk(prefix, obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if len(flat) >= max_items:
                    return
                key = "%s.%s" % (prefix, k) if prefix else str(k)
                if isinstance(v, dict):
                    walk(key, v)
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    flat.append("%s=%s" % (key, _num(v)))
                elif isinstance(v, str):
                    flat.append("%s=%s" % (key, v))
        elif isinstance(obj, (int, float)):
            flat.append("%s=%s" % (prefix, _num(obj)))
    walk("", metrics)
    return " ".join(flat[:max_items])


def _num(v):
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return str(v)
    try:
        if abs(v) >= 1000 or (v != 0 and abs(v) < 0.01):
            return "%.3g" % v
        return "%.4g" % v
    except Exception:
        return str(v)


def format_record(rec):
    """单条记录全文。"""
    L = [SEP, "实验记录 %s" % rec.get("run_id"), SEP]
    rows = [("实验类型", rec.get("experiment")), ("创建时间", rec.get("created_at")),
            ("config_hash", rec.get("config_hash")), ("重复自", rec.get("repeat_of")),
            ("版本", rec.get("version")), ("Python", rec.get("py")),
            ("结论", rec.get("conclusion")), ("复现命令", rec.get("reproduce"))]
    for k, v in rows:
        L.append("  %-10s: %s" % (k, v if v not in (None, "") else "—"))
    L.append("-" * 96)
    L.append("  参数：")
    L.append(_indent_json(rec.get("params") or {}))
    L.append("  关键指标：")
    L.append(_indent_json(rec.get("metrics") or {}))
    di = rec.get("data_identity") or {}
    L.append("  输入数据身份（%d 项，不含 mtime）：" % len(di))
    for ap, info in sorted(di.items()):
        if isinstance(info, dict) and info.get("exists") is False:
            L.append("    ✗ %s（缺失）" % ap)
        else:
            tag = info.get("sha256") if isinstance(info, dict) else None
            L.append("    ✓ %s  %s" % ((info or {}).get("name") if isinstance(info, dict) else ap,
                                      ("sha=" + str(tag)[:16]) if tag else
                                      ("size=" + str((info or {}).get("size")))))
    arts = rec.get("artifacts") or {}
    L.append("  产物（%d 项）：" % len(arts))
    for ap, fp in sorted(arts.items()):
        if isinstance(fp, dict):
            L.append("    · %s  %sB  %s" % (fp.get("name"), fp.get("size"), fp.get("mtime_iso")))
    return "\n".join(L)


def _indent_json(obj, indent=2):
    return "\n".join((" " * indent + ln) for ln in
                     json.dumps(json_safe(obj), ensure_ascii=False, indent=1, allow_nan=False).splitlines())


def format_repeats(records):
    """按 config_hash 分组，列出跑过≥2 次的配置及其指标漂移。"""
    groups = {}
    order = []
    for r in records:
        h = r.get("config_hash")
        if h not in groups:
            groups[h] = []
            order.append(h)
        groups[h].append(r)
    L = [SEP, "重复实验（同 config_hash 多次运行）与指标漂移", SEP]
    any_rep = False
    for h in order:
        rs = groups[h]
        if len(rs) < 2:
            continue
        any_rep = True
        L.append("● %s  %s ×%d" % (h, rs[0].get("experiment"), len(rs)))
        for r in rs:
            L.append("    %s  repeat_of=%s  %s" %
                     (r.get("run_id"), r.get("repeat_of") or "首跑", _metric_flat(r.get("metrics") or {})))
    if not any_rep:
        L.append("（无重复实验：每个 config_hash 目前只跑过一次）")
    return "\n".join(L)


# =========================== CLI ===========================
def run(argv=None):
    ap = argparse.ArgumentParser(description="G27① 统一实验台账：查询/对比各研究与回测实验登记（只读查询）")
    ap.add_argument("--ledger", default=None,
                    help="台账 JSONL 路径，默认 reports/experiment_runs.jsonl；可用环境变量 %s 重定向/关闭" % ENV_LEDGER)
    ap.add_argument("--list", action="store_true", help="列出实验（默认动作）")
    ap.add_argument("--experiment", help="只看某实验类型，如 portfolio_lab")
    ap.add_argument("--limit", type=int, default=30, help="最多列出最近 N 条，默认30")
    ap.add_argument("--show", help="显示某 run_id 全文")
    ap.add_argument("--repeats", action="store_true", help="只看重复实验与指标漂移")
    ap.add_argument("--export", help="把台账导出为 JSON 数组文件")
    args = ap.parse_args(argv)

    ledger_path = args.ledger or get_default_ledger_path()
    if not ledger_path:
        print("实验台账已被环境变量 %s 关闭" % ENV_LEDGER)
        return 0
    store = LedgerStore(ledger_path)
    if args.show:
        target = None
        for r in store.load_all():
            if r.get("run_id") == args.show or str(r.get("run_id", "")).startswith(args.show):
                target = r
        if target is None:
            print("未找到 run_id=%s" % args.show)
            return 1
        print(format_record(target))
        return 0
    if args.repeats:
        print(format_repeats(store.load_all()))
        return 0
    records = store.filter(experiment=args.experiment, limit=args.limit)
    if args.export:
        od = os.path.dirname(os.path.abspath(args.export))
        if od and not os.path.isdir(od):
            os.makedirs(od, exist_ok=True)
        with io.open(args.export, "w", encoding="utf-8", newline="\n") as f:
            json.dump(records, f, ensure_ascii=False, indent=1, allow_nan=False)
        print("已导出 %d 条 → %s" % (len(records), args.export))
        return 0
    print(format_list(records))
    if store.bad_lines:
        print("（提示：%d 行损坏已跳过）" % store.bad_lines)
    return 0


# =========================== 零网络/零DB 合成断言 ===========================
def selftest():
    import tempfile
    fixed = _dt.datetime(2026, 9, 3, 15, 0, 0)
    tmp = tempfile.mkdtemp()

    # 1) 规范化哈希：键序无关、值变则 hash 变、含中文稳定、可复现
    h1 = canonical_hash("exp", {"b": 1, "a": [1, 2]}, {})
    h2 = canonical_hash("exp", {"a": [1, 2], "b": 1}, {})
    assert h1 == h2 and len(h1) == CONFIG_HASH_LEN
    assert canonical_hash("exp", {"b": 2, "a": [1, 2]}, {}) != h1
    assert canonical_hash("exp2", {"a": 1}, {}) != canonical_hash("exp", {"a": 1}, {})
    h3 = canonical_hash("实验", {"中文键": "值"}, {})
    assert h3 == canonical_hash("实验", {"中文键": "值"}, {})

    # 2) 非有限浮点被清洗；canonical_bytes 内部先 json_safe，故 NaN 安全转 None 不抛
    rec = make_record("e", {"x": float("nan"), "y": float("inf"), "z": 1.0},
                      metrics={"m": float("-inf")}, now=fixed)
    assert rec["params"]["x"] is None and rec["params"]["y"] is None and rec["metrics"]["m"] is None
    assert canonical_bytes({"x": float("nan")}) == canonical_bytes({"x": None})
    # 未清洗的原始 json.dumps 在 allow_nan=False 下必须拒绝 NaN（底层防线）
    try:
        json.dumps({"x": float("nan")}, allow_nan=False)
        raise AssertionError("allow_nan=False 应拒绝裸 NaN")
    except ValueError:
        pass

    # 3) 文件指纹：存在性/大小/小文件 sha；缺失安全；内容身份排除 mtime
    p1 = os.path.join(tmp, "in.csv")
    with io.open(p1, "w", encoding="utf-8", newline="\n") as f:
        f.write("a,b\n1,2\n")
    fp = file_fingerprint(p1)
    assert fp["exists"] and fp["size"] == 8 and fp["sha256"]
    miss = file_fingerprint(os.path.join(tmp, "nope.csv"))
    assert miss["exists"] is False and miss["sha256"] is None
    man = build_manifest([p1])
    ident1 = data_identity_from_manifest(man)
    # 重写同样内容（mtime 变化），内容身份不变
    import time as _t
    _t.sleep(1.05)
    with io.open(p1, "w", encoding="utf-8", newline="\n") as f:
        f.write("a,b\n1,2\n")
    ident2 = data_identity_from_manifest(build_manifest([p1]))
    assert ident1 == ident2, "内容不变则数据身份必须不变（排除 mtime）"
    # 内容改变 → sha 改变
    with io.open(p1, "a", encoding="utf-8") as f:
        f.write("3,4\n")
    ident3 = data_identity_from_manifest(build_manifest([p1]))
    assert ident3 != ident1

    # 4) make_record：run_id 形态、复现命令可关、字段齐全
    r0 = make_record("lab", {"k": "v"}, {"sharpe": 0.5}, inputs=[p1], artifacts=[],
                     now=fixed, reproduce=False)
    assert r0["run_id"] == "20260903-150000-" + r0["config_hash"][:8]
    assert r0["reproduce"] is None and r0["repeat_of"] is None and r0["version"] is not None
    assert set(r0) >= {"run_id", "created_at", "experiment", "config_hash", "params",
                      "metrics", "inputs", "artifacts", "data_identity"}

    # 5) LedgerStore 追加/回读 + 同配置 repeat_of 串联（同参数两次 hash 一致）
    led = os.path.join(tmp, "ledger.jsonl")
    st = LedgerStore(led)
    a = st.append(make_record("lab", {"window": 126}, {"sharpe": 0.42}, now=fixed))
    assert st.bad_lines == 0 and len(st.load_all()) == 1
    b = st.append(make_record("lab", {"window": 126}, {"sharpe": 0.45},
                              now=fixed + _dt.timedelta(minutes=5)))
    assert b["config_hash"] == a["config_hash"] and b["repeat_of"] == a["run_id"], "同配置必须串联"
    c = st.append(make_record("lab", {"window": 63}, {"sharpe": 0.40},
                              now=fixed + _dt.timedelta(minutes=10)))
    assert c["repeat_of"] is None and c["config_hash"] != a["config_hash"]

    # 6) 同秒同配置 run_id 碰撞自动加 -r2
    d = st.append(make_record("lab", {"window": 126}, {"sharpe": 0.5}, now=fixed))
    assert d["run_id"].endswith("-r2") and d["repeat_of"] is not None

    # 7) 坏行/空行宽容：手写一条坏 JSON 与空行，load_all 不抛、bad_lines 计数
    with io.open(led, "a", encoding="utf-8", newline="\n") as f:
        f.write("\n{bad json,,,}\n")
    st_bad = LedgerStore(led)
    recs = st_bad.load_all()
    assert len(recs) == 4 and st_bad.bad_lines == 1

    # 8) filter 按实验/limit
    st2 = LedgerStore(led)
    assert len(st2.filter(experiment="lab", limit=2)) == 2
    assert st2.filter(experiment="none") == []

    # 9) safe_record 永不抛错：坏路径/坏参数也安全返记录或 None
    ok = safe_record("lab", {"a": 1}, None, inputs=[os.path.join(tmp, "missing_x.csv")],
                     artifacts=None, now=fixed, ledger_path=os.path.join(tmp, "s2.jsonl"))
    assert ok and ok["run_id"]
    blocker = os.path.join(tmp, "afile")          # 以普通文件充当目录→台账无法落盘
    with io.open(blocker, "w", encoding="utf-8") as f:
        f.write("x")
    bad = safe_record("lab", {"a": 1}, now=fixed,
                      ledger_path=os.path.join(blocker, "s3.jsonl"))
    assert bad is None  # 落盘失败必须被安全吞掉返回 None

    # 10) 文本渲染：列表/单条/重复分组都含关键信息且不抛
    allr = LedgerStore(led).load_all()
    lst = format_list(allr)
    assert "统一实验台账" in lst and "↻" in lst
    one = format_record(allr[0])
    assert "参数" in one and "关键指标" in one and "输入数据身份" in one
    rep_txt = format_repeats(allr)
    assert "重复实验" in rep_txt and "×" in rep_txt
    assert format_list([]).startswith("（台账为空")
    assert "无重复实验" in format_repeats([{"config_hash": "z", "experiment": "x", "run_id": "1"}])

    # 11) _metric_flat 嵌套一层 + _num 格式
    assert "erc.sharpe=0.55" in _metric_flat({"equal": {"sharpe": 0.42}, "erc": {"sharpe": 0.55}})
    assert _num(0.123456) == "0.1235" and _num(12345) == "12345"

    # 12) CLI：--list/--show/--repeats/--export/找不到 run 均行为正确
    assert run(["--ledger", led, "--list", "--limit", "10"]) == 0
    assert run(["--ledger", led, "--repeats"]) == 0
    assert run(["--ledger", led, "--show", a["run_id"]]) == 0
    assert run(["--ledger", led, "--show", "___no_such___"]) == 1
    exp_path = os.path.join(tmp, "export.json")
    assert run(["--ledger", led, "--export", exp_path]) == 0
    exported = json.load(io.open(exp_path, "r", encoding="utf-8"))
    assert isinstance(exported, list) and len(exported) == 4

    # 13) 环境变量重定向/关闭：显式关闭时 safe_record 直接 None、LedgerStore disabled
    old = os.environ.get(ENV_LEDGER)
    try:
        os.environ[ENV_LEDGER] = "off"
        assert get_default_ledger_path() is None
        assert safe_record("lab", {"a": 1}, now=fixed) is None
        assert LedgerStore().disabled and LedgerStore().load_all() == []
        os.environ[ENV_LEDGER] = os.path.join(tmp, "env_led.jsonl")
        rr = safe_record("lab", {"a": 1}, now=fixed)
        assert rr and os.path.isfile(os.environ[ENV_LEDGER])
    finally:
        if old is None:
            os.environ.pop(ENV_LEDGER, None)
        else:
            os.environ[ENV_LEDGER] = old

    print("experiment_ledger selftest OK（13 组）")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 1:
        raise SystemExit(selftest())
    raise SystemExit(run())
