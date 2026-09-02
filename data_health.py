# -*- coding: utf-8 -*-
"""
G6 数据质量监控（纯标准库、零网络、纯函数可单测）。

- evaluate_quotes：对一轮全品种行情快照做静态体检——缺数 / 陈旧价 / 异常跳变。
- HealthMonitor：跨轮追踪"某品种连续缺数""某源连续全失败"，把 data_router.REGISTRY 的
  累计计数折算成单轮增量，产出 storage.data_health 落表行与需要告警的对象。
- format_health_block：渲染看板【数据源健康】小块。

只做监控与告警，不改任何打分/信号/取数结果；总开关 config.DATA_HEALTH_ENABLED。
"""
import time

try:
    import config
except Exception:  # pragma: no cover
    config = None


def _cfg(name, default):
    return getattr(config, name, default) if config is not None else default


def evaluate_quotes(quotes, expected_codes, today_str=None, jump_pct=None,
                    session_active=True):
    """对一轮行情做静态体检（纯函数）。

    quotes: {code: {latest, chg_pct, date, ...}}；expected_codes: 本轮应到的主连 code 列表。
    today_str: 交易日期 'YYYY-MM-DD'，session_active=True 时用它判陈旧（非交易时段不按日期判陈旧）。
    返回 dict: present/missing/stale/jump 四个 code 列表 + 计数。
    """
    if jump_pct is None:
        jump_pct = _cfg("DATA_HEALTH_JUMP_PCT", 0.30)
    expected = [c for c in expected_codes if c]
    present, stale, jump = [], [], []
    for code in expected:
        q = quotes.get(code)
        if not q:
            continue
        latest = float(q.get("latest") or 0.0)
        present.append(code)
        # 陈旧/无效：最新价非正；交易时段内日期不是今天也算陈旧（非交易时段快照停在上一交易日属正常）
        if latest <= 0:
            stale.append(code)
        elif session_active and today_str and q.get("date") and str(q.get("date"))[:10] != today_str:
            stale.append(code)
        # 异常跳变：|涨跌幅| 超阈值（真实商品期货极罕见，多为错位脏价）
        chg = q.get("chg_pct")
        if latest > 0 and chg is not None:
            try:
                if abs(float(chg)) >= jump_pct:
                    jump.append(code)
            except (TypeError, ValueError):
                pass
    present_set = set(present)
    missing = [c for c in expected if c not in present_set]
    return {"present": present, "missing": missing, "stale": stale, "jump": jump,
            "n_expected": len(expected), "n_present": len(present_set)}


class HealthMonitor:
    """跨轮维护连续缺数/连续全失败计数，产出落表行与告警对象。"""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.miss_streak = {}          # code -> 连续缺数轮数
        self.source_fail_streak = {}   # source -> 连续全失败轮数
        self._last_totals = {}         # source -> (total, success, fail) 上轮累计值
        self.last_result = None

    def _source_deltas(self, snapshots):
        """把 REGISTRY 的累计计数折算成本轮增量。"""
        deltas = {}
        for name, snap in snapshots.items():
            tot, ok, fail = snap["total"], snap["success"], snap["fail"]
            prev = self._last_totals.get(name, (0, 0, 0))  # 首次见：基线为0，累计即增量
            d_req = max(0, tot - prev[0])
            d_ok = max(0, ok - prev[1])
            d_fail = max(0, fail - prev[2])
            deltas[name] = {"req": d_req, "ok": d_ok, "fail": d_fail,
                            "state": snap["state"], "availability": snap["availability"]}
            self._last_totals[name] = (tot, ok, fail)
        return deltas

    def observe_cycle(self, ts, quotes, expected_codes, snapshots,
                      today_str=None, session_active=True):
        """一轮结束后调用。snapshots=data_router.REGISTRY.snapshots()。

        返回 {"rows","alert_codes","alert_sources","open_sources","eval","coverage"}。
        """
        miss_cycles = _cfg("DATA_HEALTH_MISS_ALERT_CYCLES", 2)
        fail_cycles = _cfg("DATA_HEALTH_SOURCE_FAIL_CYCLES", 2)
        ev = evaluate_quotes(quotes, expected_codes, today_str=today_str,
                             session_active=session_active)

        # 1) 品种连续缺数追踪
        alert_codes = []
        missing_set = set(ev["missing"])
        for code in expected_codes:
            if code in missing_set:
                self.miss_streak[code] = self.miss_streak.get(code, 0) + 1
                if self.miss_streak[code] >= miss_cycles:
                    alert_codes.append(code)
            else:
                self.miss_streak[code] = 0
        # 清理已不在观察列表的旧 code
        for code in list(self.miss_streak):
            if code not in set(expected_codes):
                self.miss_streak.pop(code, None)

        # 2) 数据源增量 + 连续全失败追踪
        deltas = self._source_deltas(snapshots)
        alert_sources, open_sources, rows = [], [], []
        for name, d in deltas.items():
            all_fail_this_cycle = d["req"] > 0 and d["ok"] == 0
            if all_fail_this_cycle:
                self.source_fail_streak[name] = self.source_fail_streak.get(name, 0) + 1
            elif d["req"] > 0:
                self.source_fail_streak[name] = 0
            streak = self.source_fail_streak.get(name, 0)
            if streak >= fail_cycles and all_fail_this_cycle:
                alert_sources.append(name)
            if d["state"] == "open":
                open_sources.append(name)
            if d["req"] > 0 or d["state"] == "open":
                rows.append({"source": name, "req": d["req"], "ok": d["ok"],
                             "fail": d["fail"], "stale": 0, "jump": 0,
                             "state": d["state"],
                             "note": "avail=%.2f" % d["availability"]})

        # 3) 行情聚合行：把缺数/陈旧/跳变归到 __quotes__
        rows.append({"source": "__quotes__", "req": ev["n_expected"],
                     "ok": ev["n_present"], "fail": len(ev["missing"]),
                     "stale": len(ev["stale"]), "jump": len(ev["jump"]),
                     "state": "open" if ev["n_present"] == 0 and ev["n_expected"] else "closed",
                     "note": "missing=%s stale=%s jump=%s" % (
                         ",".join(ev["missing"][:12]), ",".join(ev["stale"][:12]),
                         ",".join(ev["jump"][:12]))})

        coverage = (ev["n_present"] / ev["n_expected"]) if ev["n_expected"] else 1.0
        result = {"ts": ts, "rows": rows, "alert_codes": sorted(alert_codes),
                  "alert_sources": sorted(alert_sources),
                  "open_sources": sorted(open_sources), "eval": ev,
                  "coverage": coverage}
        self.last_result = result
        return result

    def reset(self):
        self.miss_streak.clear()
        self.source_fail_streak.clear()
        self._last_totals.clear()
        self.last_result = None


def format_health_block(result):
    """把 observe_cycle 的结果渲染成看板文本块；无结果返回空串。"""
    if not result:
        return ""
    ev = result["eval"]
    lines = []
    lines.append("【数据源健康】本轮行情覆盖 %d/%d（%.0f%%），缺数%d、陈旧价%d、异常跳变%d" % (
        ev["n_present"], ev["n_expected"], result["coverage"] * 100.0,
        len(ev["missing"]), len(ev["stale"]), len(ev["jump"])))
    src_bits = []
    for r in result["rows"]:
        if r["source"] == "__quotes__":
            continue
        flag = {"open": "熔断", "half_open": "试探", "closed": "正常"}.get(r["state"], r["state"])
        src_bits.append("%s[%s 请求%d/成功%d/失败%d]" % (
            r["source"], flag, r["req"], r["ok"], r["fail"]))
    if src_bits:
        lines.append("  数据源: " + "；".join(src_bits))
    if result["open_sources"]:
        lines.append("  ⚠ 熔断中: " + ",".join(result["open_sources"]))
    if result["alert_codes"]:
        lines.append("  ⛔ 连续缺数≥阈值: " + ",".join(result["alert_codes"][:16]))
    if ev["stale"]:
        lines.append("  ⚠ 陈旧/无效价: " + ",".join(ev["stale"][:16]))
    if ev["jump"]:
        lines.append("  ⚠ 异常跳变(疑似脏价): " + ",".join(ev["jump"][:16]))
    return "\n".join(lines)
