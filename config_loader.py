# -*- coding: utf-8 -*-
"""
G10 配置外置加载器（纯标准库、零网络、可独立单测）。

职责：
  1) parse_dotenv / load_dotenv：解析极简 .env（KEY=VALUE）注入 os.environ，
     已存在的真实环境变量优先、不被 .env 覆盖；key/token/webhook 只走环境变量。
  2) deep_merge：字典递归深合并（base 不动，返回新 dict）。
  3) coerce_value：按"默认值的类型"把 JSON 值矫正成与内置默认一致的类型，
     类型不符返回 (False, None)，由调用方跳过并保留默认（非法值绝不静默改类型）。
  4) apply_overrides：把 config.json 的覆盖项应用到 config 模块全局——
     只覆盖"已存在的大写可调常量"，受保护的路径/内部名跳过，未知名忽略。

设计铁律：缺 config.json / .env 时行为与历史逐字节一致；任何非法/未知项只告警不抛错。
"""
import os

# 受保护、不允许 config.json 覆盖的内部名（路径/库位/派生结构，机器相关，改动会破坏运行）
PROTECTED_NAMES = {"BASE_DIR", "DATA_DIR"}


# 第138轮 E3：config.json 覆盖值域约束（比类型矫正更严：枚举/范围/格式）
# 命中则必须满足规则，否则跳过保留默认。键为全大写常量名。
SCHEMA_RULES = {
    # 枚举类：值必须命中白名单
    "PAPER_FILL_MODE": ("enum", ("close", "next")),
    "RISK_GATE_ACTION": ("enum", ("observe", "paper_halt", "paper_delever")),
    "CIRCUIT_ACTION": ("enum", ("observe", "paper_halt", "paper_delever")),
    "PORTFOLIO_SIZING": ("enum", ("equal_notional", "equal_risk", "score")),
    # 范围类：(min, max) 闭区间，值必须可数值化且落在区间内
    "DB_BACKUP_KEEP": (1, 60),
    "DB_BACKUP_MIN_INTERVAL_H": (1.0, 48.0),
    "PAPER_TICK_INTERVAL": (0, 3600),
    "SIGNALS_RAWJSON_RETENTION_DAYS": (1, 730),
    "MAX_PRICE_JUMP_PCT": (0.0, 100.0),
    "CONFLICT_DIFF_PCT": (0.0, 100.0),
    "OPT_IV_HV_MAX_RATIO": (1.0, 3.0),
    "RISK_GATE_MIN_VOLUME": (0, 10**9),
    "FUND_BASIS_WEIGHT": (0.0, 1.0),
    "FUND_CARRY_WEIGHT": (0.0, 1.0),
    "FUND_INV_WEIGHT": (0.0, 1.0),
    "FUND_RANK_WEIGHT": (0.0, 1.0),
}


def _check_schema(name, value, default):
    """对 (name,value) 应用 SCHEMA_RULES；不满足返回 (False, 原因)，满足返回 (True, value)。"""
    rule = SCHEMA_RULES.get(name)
    if rule is None:
        return True, value
    kind, spec = (rule[0], rule[1]) if isinstance(rule, tuple) and len(rule) == 2         and isinstance(rule[0], str) and rule[0] in ("enum",) else ("range", rule)
    if kind == "enum":
        if value in spec:
            return True, value
        return False, "值域约束: %s 只允许 %s（给了 %r）" % (name, "/".join(spec), value)
    # range
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False, "值域约束: %s 需为数值（给了 %r）" % (name, value)
    lo, hi = float(spec[0]), float(spec[1])
    if lo <= v <= hi:
        return True, value
    return False, "值域约束: %s 需在 [%s, %s]（给了 %s）" % (name, spec[0], spec[1], value)


def _is_protected(name):
    if name in PROTECTED_NAMES:
        return True
    # 各类文件/目录/页面输出路径统一保护（_FILE/_DB/_DIR/_JS/_HTML/_CSV 结尾）
    for suffix in ("_FILE", "_DB", "_DIR", "_JS", "_HTML", "_CSV"):
        if name.endswith(suffix):
            return True
    return False


def parse_dotenv(text):
    """把 .env 文本解析成 {KEY: VALUE}（纯函数，不碰 os.environ）。

    规则：忽略空行与 # 注释；可选 `export ` 前缀；值两侧成对单/双引号剥离；
    其余按原样字符串保留（不做类型推断，环境变量本来就是字符串）。
    """
    result = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        # 仅当两侧是同一种成对引号时才剥离，避免误伤值内部的引号
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        result[key] = val
    return result


def load_dotenv(path, environ=None):
    """把 .env 文件注入 environ（默认 os.environ）；已存在的键不覆盖（真实环境优先）。

    返回 (注入数量, 跳过数量)；文件不存在返回 (0,0)，不抛错。
    """
    environ = os.environ if environ is None else environ
    if not path or not os.path.exists(path):
        return 0, 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            parsed = parse_dotenv(f.read())
    except OSError:
        return 0, 0
    injected = skipped = 0
    for key, val in parsed.items():
        if key in environ:
            skipped += 1
            continue
        environ[key] = val
        injected += 1
    return injected, skipped


def deep_merge(base, override):
    """递归合并 override 到 base 的副本上；两者同为 dict 才下钻，否则 override 覆盖。"""
    if not isinstance(base, dict) or not isinstance(override, dict):
        return override
    merged = dict(base)
    for key, val in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def coerce_value(value, default):
    """按 default 的类型把 JSON 读入的 value 矫正一致。返回 (ok, coerced)。

    - bool 默认只接受 bool（注意 bool 是 int 子类，必须先判）
    - int 默认接受 int；形如 3.0 的浮点整数也接受，非整数浮点拒绝
    - float 默认接受 int/float
    - str 默认只接受 str（数字不隐式转字符串，防止写错类型）
    - tuple/list 默认接受 list（tuple 化），元素不逐个强转
    - dict 默认接受 dict
    其余类型（None/函数等）一律拒绝覆盖。
    """
    # bool 优先（在 int 之前）
    if isinstance(default, bool):
        return (True, value) if isinstance(value, bool) else (False, None)
    if isinstance(default, int):
        if isinstance(value, bool):
            return False, None
        if isinstance(value, int):
            return True, value
        if isinstance(value, float) and value.is_integer():
            return True, int(value)
        return False, None
    if isinstance(default, float):
        if isinstance(value, bool):
            return False, None
        if isinstance(value, (int, float)):
            return True, float(value)
        return False, None
    if isinstance(default, str):
        return (True, value) if isinstance(value, str) else (False, None)
    if isinstance(default, tuple):
        if isinstance(value, (list, tuple)):
            return True, tuple(value)
        return False, None
    if isinstance(default, list):
        return (True, list(value)) if isinstance(value, (list, tuple)) else (False, None)
    if isinstance(default, dict):
        return (True, value) if isinstance(value, dict) else (False, None)
    # 其它类型（None/函数/模块/集合等）不允许经 config.json 覆盖
    return False, None


def apply_overrides(namespace, overrides, source="config.json"):
    """把 overrides 应用到 config 模块的 namespace（原地改）。

    只覆盖：键为全大写、已存在于 namespace、且非受保护名、且类型可矫正到默认的项。
    未知键/受保护键/类型不符键一律跳过并记录到返回报告，绝不抛异常。
    返回 {"applied": {名: 值}, "skipped": {名: 原因}}，便于日志与测试。
    """
    report = {"applied": {}, "skipped": {}}
    if not isinstance(overrides, dict):
        report["skipped"]["__root__"] = "顶层不是 JSON 对象: %r" % type(overrides).__name__
        return report
    for name, value in overrides.items():
        if not isinstance(name, str) or not name.isupper() or name.startswith("_"):
            report["skipped"][name] = "只允许覆盖全大写常量"
            continue
        if name not in namespace:
            report["skipped"][name] = "config.py 中不存在该常量（未知项忽略）"
            continue
        if _is_protected(name):
            report["skipped"][name] = "路径/内部常量受保护，不可外置覆盖"
            continue
        default = namespace[name]
        # 不允许覆盖函数/模块/类等可调用或模块对象
        if callable(default) or isinstance(default, type(os)):
            report["skipped"][name] = "函数/模块/类不可外置覆盖"
            continue
        if isinstance(default, dict) and isinstance(value, dict):
            merged = deep_merge(default, value)
            namespace[name] = merged
            report["applied"][name] = merged
            continue
        ok, coerced = coerce_value(value, default)
        if not ok:
            report["skipped"][name] = "类型不符（默认 %s，给了 %s），保留默认" % (
                type(default).__name__, type(value).__name__)
            continue
        ok2, reason = _check_schema(name, coerced, default)
        if not ok2:
            report["skipped"][name] = reason
            continue
        namespace[name] = coerced
        report["applied"][name] = coerced
    return report


def load_config_file(path):
    """读取并 json 解析配置文件；不存在返回 (None, None)，损坏返回 (None, 错误串)。"""
    import json
    if not path or not os.path.exists(path):
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except OSError as exc:
        return None, "读取失败: %s" % exc
    except ValueError as exc:  # JSONDecodeError 是 ValueError 子类
        return None, "JSON 解析失败: %s" % exc
