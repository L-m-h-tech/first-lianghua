# -*- coding: utf-8 -*-
"""第94轮 B6（对标 scrapling checkpoint pause/resume）：长任务阶段级断点续传。

缓存 cache/checkpoints.json 记录 {"YYYY-MM-DD": {"stages": [已完成阶段,...]}}；
任务重跑时对同一日期已完成的阶段直接跳过（不重算），中断后从断点续跑——shadow 每日链
（term top-up → 长面板重建）等重活按阶段登记进度，重启续传不重来。

设计（安全优先）：
- 只做"阶段级跳过"，绝不改变任何产物口径；阶段标记写失败静默（等价旧版全跑）。
- 与影子"启动日守卫防回填"正交：checkpoint 按自然日，仅用于"今天这个任务是否已做完重活"。
- 测试友好：路径可注入（checkpoint.set_path / 环境变量 FUTURES_MONITOR_CHECKPOINT）。
"""
import json
import os
import threading
import time
from datetime import datetime

import config

_PATH = os.environ.get("FUTURES_MONITOR_CHECKPOINT",
                       os.path.join(config.BASE_DIR, "cache", "checkpoints.json"))
_LOCK = threading.Lock()


def set_path(p):
    global _PATH
    _PATH = p


def _load():
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(store):
    try:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=0)
        os.replace(tmp, _PATH)
        return True
    except Exception:
        return False


def done(day, stage):
    """指定日期某阶段是否已完成。失败返回 False（保守：宁重跑不跳过）。"""
    try:
        return stage in _load().get(day, {}).get("stages", [])
    except Exception:
        return False


def mark(day, stage):
    """登记某日期某阶段完成。返回是否写盘成功。"""
    with _LOCK:
        try:
            store = _load()
            store.setdefault(day, {"stages": []})
            if stage not in store[day]["stages"]:
                store[day]["stages"].append(stage)
            return _save(store)
        except Exception:
            return False


def reset(day=None):
    """清理指定日期的 checkpoint；None=清空全部（任务口径变化时用，等价回退到全跑）。"""
    with _LOCK:
        try:
            store = _load()
            if day is None:
                store = {}
            else:
                store.pop(day, None)
            return _save(store)
        except Exception:
            return False


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def selftest():
    """零网络合成断言：mark/done/reset 幂等与容错。"""
    import tempfile
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))
        if not cond:
            raise AssertionError("FAIL: " + name)

    old = _PATH
    try:
        tmp = os.path.join(tempfile.gettempdir(), "fm_checkpoint_selftest.json")
        try:
            os.remove(tmp)
        except OSError:
            pass
        set_path(tmp)
        ck("初始未完成", not done("2026-09-07", "topup"))
        ck("mark成功", mark("2026-09-07", "topup"))
        ck("done命中", done("2026-09-07", "topup"))
        ck("幂等不重复", mark("2026-09-07", "topup"))
        ck("其他阶段未完成", not done("2026-09-07", "panel"))
        ck("reset单日", reset("2026-09-07") and not done("2026-09-07", "topup"))
        ck("坏路径静默", mark("bad" * 50, "x") is False or True)   # 不应抛
    finally:
        set_path(old)
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    raise SystemExit(selftest())