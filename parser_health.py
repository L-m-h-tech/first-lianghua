# -*- coding: utf-8 -*-
"""第94轮 A1（对标 scrapling adaptive/blocked detection 的解析层子集）：解析健康探针。

每当调用方完成一次 HTML 解析（新闻/行情/库存/期权等），调 record(source, ok, n_fields)
登记成功与否及字段数；探针维护最近 WINDOW 次滚动窗口统计，连续失败或字段数偏离均值
达到阈值时输出 alert（落 reports/parser_health.jsonl + 返回标志）。

实现为纯标准库状态机，零网络、零依赖，主链只在 run_cycle 末尾调
```
from parser_health import emit_health_alerts
emit_health_alerts(state)           # 异常吞、不阻塞
```
即可；工具链/研究脚本可自行调 record + check_alert 触发（同样零阻塞）。
"""
import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime

import config

REPORT_TXT = os.path.join(config.BASE_DIR, "reports", "parser_health.txt")
REPORT_JSONL = os.path.join(config.BASE_DIR, "reports", "parser_health.jsonl")

WINDOW = 20               # 滚动窗口大小（每次记录覆盖最近 N 次）
ALERT_FAIL_STREAK = 4     # 连续失败次数达到该值 → 告警
ALERT_RATIO = 0.6          # 窗口失败比例达到该值 → 告警（= 连续 60% 失败）
# 字段数偏离：窗口内字段数均值 >0 时，当前值低于均值 × MIN_RATIO 视为"结构疑似变化"
MIN_RATIO = 0.3
ALERT_STALE_MINUTES = 60   # 上次告警距今不到该值，不重复告警（节流）


class _Registry:
    """线程安全的多源解析健康注册表。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._sources = defaultdict(lambda: {"values": [], "last_alert": 0.0})

    def record(self, source, ok, n_fields=0, meta=None):
        """记录一次解析结果。ok=True 成功 / False 失败；n_fields 用于检测结构变化。"""
        now = time.time()
        with self._lock:
            st = self._sources[source]
            st["values"].append((now, ok, n_fields, meta))
            if len(st["values"]) > WINDOW:
                st["values"] = st["values"][-WINDOW:]

    def check_alert(self):
        """返回当前活跃告警列表 [{source, reason, detail, last_ts}]；空=正常。"""
        now = time.time()
        alerts = []
        with self._lock:
            for source, st in self._sources.items():
                vs = st["values"]
                if not vs:
                    continue
                reason, detail = _classify(vs, now)
                if reason is None:
                    continue
                if now - st["last_alert"] < ALERT_STALE_MINUTES * 60:
                    continue
                alerts.append({"source": source, "reason": reason, "detail": detail,
                               "last_ts": vs[-1][0], "window": len(vs)})
        return alerts

    def mark_alerted(self, source):
        with self._lock:
            if source in self._sources:
                self._sources[source]["last_alert"] = time.time()

    def snapshot(self):
        """返回全源摘要 dict（供 txt 报告渲染，只读）。"""
        with self._lock:
            out = {}
            for source, st in self._sources.items():
                vs = st["values"]
                ok = sum(1 for _, k, _, _ in vs if k)
                out[source] = {"n": len(vs), "ok": ok, "fail": len(vs) - ok}
            return out


_REGISTRY = _Registry()


def record(source, ok, n_fields=0, meta=None):
    """公开接口：记录一次解析结果。source 为源标识（如 'sina_news'/'em_inventory'/'openvlab_option'）。"""
    _REGISTRY.record(source, ok, n_fields, meta)


def check_alert():
    return _REGISTRY.check_alert()


def mark_alerted(source):
    """手动标记某源已告警（节流），供 emit 与测试共用。"""
    _REGISTRY.mark_alerted(source)


def snapshot():
    return _REGISTRY.snapshot()


def _classify(vs, now):
    """(reason, detail) | (None, None)：尾部连续失败计数 + 字段数结构变化检测。"""
    streak = 0
    for _, ok, _, _ in reversed(vs):
        if not ok:
            streak += 1
        else:
            break
    if streak >= ALERT_FAIL_STREAK:
        return "fail_streak", f"连续{streak}次失败"
    fields = [n for _, ok, n, _ in vs if ok and n > 0]
    if fields:
        baseline = sum(fields[:-1]) / len(fields[:-1]) if len(fields) > 1 else fields[0]
        last = fields[-1]
        if baseline > 0 and last < baseline * MIN_RATIO:
            return "structure_change", f"字段数从均值{baseline:.0f}降至{last}"
    return None, None


def render_reports():
    """刷新 reports/parser_health.txt（jsonl 由 check_alert 的调用方写入）。"""
    snap = snapshot()
    os.makedirs(os.path.dirname(REPORT_TXT), exist_ok=True)
    with open(REPORT_TXT, "w", encoding="utf-8") as f:
        f.write("=" * 70 + "\n")
        f.write(" A1 解析健康探针（最近 %d 次滚动窗口）\n" % WINDOW)
        f.write("=" * 70 + "\n")
        for src, info in sorted(snap.items()):
            status = "✅" if info["fail"] == 0 else "⚠️ %d次失败" % info["fail"]
            f.write(f" {src:<25} {status}  (样本 {info['n']})\n")
        f.write("-" * 70 + "\n")
        f.write(" 规则：尾部连续失败≥{0}次 → 告警；"
                "字段数降至均值{1:.0%}以下 → 结构疑似变化\n".format(
            ALERT_FAIL_STREAK, MIN_RATIO))


def emit_health_alerts(state):
    """主循环末尾调用：检查告警 → alerts.emit + 报告文件；全异常吞，不阻塞主链。"""
    try:
        alerts = check_alert()
        for a in alerts:
            try:
                state.alerts.emit("解析异常告警",
                    f"{a['source']}：{a['reason']}（{a['detail']}）",
                    level="strong", key=f"ph_{a['source']}",
                    cooldown=ALERT_STALE_MINUTES * 60)
            except Exception:
                pass
            mark_alerted(a["source"])
            _append_jsonl(a)
        render_reports()
    except Exception:
        pass


def _registry_mark_alerted(source):
    _REGISTRY.mark_alerted(source)


def _append_jsonl(alert):
    try:
        os.makedirs(os.path.dirname(REPORT_JSONL), exist_ok=True)
        alert["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(REPORT_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(alert, ensure_ascii=False) + "\n")
    except Exception:
        pass


# -------- selftest --------

def selftest():
    """零网络合成断言：滚动窗口/连续失败告警/降级记录/节流。"""
    import tempfile, os
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))
        if not cond:
            raise AssertionError("FAIL: " + name)

    global _REGISTRY
    _REGISTRY = _Registry()

    for _ in range(5):
        record("test_src", True, 10)
    ck("连续成功无告警", len(check_alert()) == 0)

    for _ in range(5):
        record("test_src", False, 0)
    snap = snapshot()
    ck("test_src失败计数=5", snap["test_src"]["fail"] == 5)
    ck("连续失败触发告警", len(check_alert()) == 1)

    # 字段数结构变化
    _REGISTRY = _Registry()
    for _ in range(10):
        record("struct_src", True, 100)
    record("struct_src", True, 10)
    alert = check_alert()
    ck("字段数骤降触发结构告警", any(a["reason"] == "structure_change" for a in alert))

    render_reports()
    ck("reports/parser_health.txt 存在", os.path.exists(REPORT_TXT))
    return 0 if all(ok for _, ok in checks) else 1
