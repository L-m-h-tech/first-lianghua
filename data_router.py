# -*- coding: utf-8 -*-
"""
G11 数据源主备自动熔断降级链（纯标准库、零网络、时钟可注入，便于确定性单测）。

三件东西：
  1) SourceHealth：单数据源健康状态机。
       CLOSED（健康，放行）→ 连续失败达阈值 → OPEN（熔断，冷却期内快速失败、不发请求）
       → 冷却到期 → HALF_OPEN（只放一次试探请求）→ 成功回 CLOSED / 失败重新 OPEN。
  2) DataRouter：按注册顺序尝试多个同源取数函数，被熔断的源直接跳过；异常或被 validator
       判为残缺（"残缺即弃"）记一次失败并继续下一源；任一源成功即返回，全失败抛 AllSourcesFailed。
  3) REGISTRY（HealthRegistry 单例）：全应用数据源健康总账，G6 数据质量看板/告警从这里读数；
       现有取数链路（futures_data/intraday_bars）用 REGISTRY.record(name, ok) 上报即可，
       不改变既有选源与返回结构。

设计铁律：默认取到的数据与旧版完全一致——熔断器只在"源确实连续失败"时跳过它（本来也拿不到），
健康时永远按原顺序尝试；总开关 DATA_ROUTER_ENABLED=False 时 allow 恒为 True（等价旧行为）。
"""
import threading
import time

try:
    import config
    _FAIL_THRESHOLD = getattr(config, "DATA_ROUTER_FAIL_THRESHOLD", 5)
    _COOLDOWN = getattr(config, "DATA_ROUTER_COOLDOWN_SEC", 300)
    _ENABLED = getattr(config, "DATA_ROUTER_ENABLED", True)
except Exception:  # pragma: no cover - config 恒在，仅理论兜底
    _FAIL_THRESHOLD, _COOLDOWN, _ENABLED = 5, 300, True

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


class AllSourcesFailed(Exception):
    """所有候选源都被熔断或取数失败时抛出，携带各源错误。"""

    def __init__(self, errors=None):
        self.errors = errors or {}
        super().__init__("所有数据源均不可用: %s" % (self.errors or "{}"))


class SourceHealth:
    """单源熔断器。clock 默认 time.monotonic（单调时钟，不受改系统时间影响），测试可注入。"""

    def __init__(self, name, fails_threshold=None, cooldown_sec=None,
                 clock=time.monotonic, enabled=None):
        self.name = name
        self.fails_threshold = _FAIL_THRESHOLD if fails_threshold is None else fails_threshold
        self.cooldown_sec = _COOLDOWN if cooldown_sec is None else cooldown_sec
        self.clock = clock
        self.enabled = _ENABLED if enabled is None else enabled
        self.state = CLOSED
        self.consecutive_fails = 0
        self.open_until = 0.0
        self._trial_outstanding = False
        self.total = 0
        self.success = 0
        self.fail = 0
        self.skipped = 0
        self.trips = 0                 # 累计熔断次数
        self.last_change = None

    def allow(self, now=None):
        """本次是否允许向该源发请求（同时负责 OPEN→HALF_OPEN 的状态迁移）。"""
        if not self.enabled:
            return True
        now = self.clock() if now is None else now
        if self.state == CLOSED:
            return True
        if self.state == OPEN:
            if now < self.open_until:
                return False
            # 冷却到期：放一次试探
            self.state = HALF_OPEN
            self._trial_outstanding = True
            self.last_change = now
            return True
        # HALF_OPEN：上一次试探结果未回来前，不再放第二个请求
        return False

    def record_success(self, now=None):
        now = self.clock() if now is None else now
        self.total += 1
        self.success += 1
        self.consecutive_fails = 0
        self._trial_outstanding = False
        if self.state != CLOSED:
            self.state = CLOSED
            self.last_change = now

    def record_failure(self, now=None):
        now = self.clock() if now is None else now
        self.total += 1
        self.fail += 1
        self.consecutive_fails += 1
        self._trial_outstanding = False
        if not self.enabled:
            return  # 总开关关：只计数、永不熔断（等价旧版逐源尝试）
        if self.state == HALF_OPEN or self.consecutive_fails >= self.fails_threshold:
            self.state = OPEN
            self.open_until = now + self.cooldown_sec
            self.last_change = now
            self.trips += 1

    def note_skipped(self):
        self.skipped += 1

    def availability(self):
        """累计成功率（无请求返回 1.0）。"""
        return (self.success / self.total) if self.total else 1.0

    def snapshot(self, now=None):
        now = self.clock() if now is None else now
        return {
            "name": self.name, "state": self.state,
            "consecutive_fails": self.consecutive_fails,
            "total": self.total, "success": self.success, "fail": self.fail,
            "skipped": self.skipped, "trips": self.trips,
            "availability": round(self.availability(), 4),
            "cooldown_remaining": max(0.0, self.open_until - now) if self.state == OPEN else 0.0,
        }


class DataRouter:
    """有序主备取数：sources=[(name, fn), ...]，按序尝试，返回 RouterResult。

    fn(*args, **kwargs) 取数；validator(result)->bool 判残缺（False 视为该源失败、继续下一源）。
    被熔断（allow=False）的源记 skipped 并直接跳过。
    """

    def __init__(self, sources, fails_threshold=None, cooldown_sec=None,
                 validator=None, clock=time.monotonic, logger=None, enabled=None):
        self.clock = clock
        self.validator = validator
        self.logger = logger
        self.health = {}
        for name, fn in sources:
            self.health[name] = SourceHealth(
                name, fails_threshold=fails_threshold, cooldown_sec=cooldown_sec,
                clock=clock, enabled=enabled)
        self._fns = dict(sources)
        self.order = [name for name, _ in sources]
        self.lock = threading.Lock()

    def request(self, *args, **kwargs):
        errors, tried = {}, []
        for name in self.order:
            h = self.health[name]
            now = self.clock()
            if not h.allow(now):
                h.note_skipped()
                errors[name] = "circuit_open"
                if self.logger:
                    self.logger.warning("数据源 %s 熔断冷却中（剩余%.0fs），跳过",
                                        name, h.snapshot(now)["cooldown_remaining"])
                continue
            tried.append(name)
            try:
                result = self._fns[name](*args, **kwargs)
            except Exception as exc:  # 取数异常：记失败、降级下一源
                with self.lock:
                    h.record_failure(now)
                errors[name] = "%s: %s" % (type(exc).__name__, exc)
                if self.logger:
                    self.logger.warning("数据源 %s 取数失败，尝试下一源: %s", name, exc)
                continue
            if self.validator is not None and not self.validator(result):
                with self.lock:
                    h.record_failure(now)
                errors[name] = "invalid_or_dirty_result"
                if self.logger:
                    self.logger.warning("数据源 %s 返回残缺/校验未过，降级下一源", name)
                continue
            with self.lock:
                h.record_success(now)
            return RouterResult(result, name, tried, h.snapshot(now))
        raise AllSourcesFailed(errors)

    def snapshots(self):
        return {name: h.snapshot() for name, h in self.health.items()}


class RouterResult:
    def __init__(self, value, source, tried, health_snapshot):
        self.value = value
        self.source = source          # 实际成功的源名
        self.tried = tried            # 本次实际尝试过（未被熔断）的源
        self.health = health_snapshot

    def __repr__(self):
        return "RouterResult(source=%s, tried=%s)" % (self.source, self.tried)


class HealthRegistry:
    """进程级数据源健康总账（线程安全单例），供 G6 看板/告警统一读数。"""

    def __init__(self, fails_threshold=None, cooldown_sec=None):
        self._lock = threading.Lock()
        self._sources = {}
        self._fails_threshold = fails_threshold
        self._cooldown = cooldown_sec

    def source(self, name):
        with self._lock:
            h = self._sources.get(name)
            if h is None:
                h = SourceHealth(name, fails_threshold=self._fails_threshold,
                                 cooldown_sec=self._cooldown)
                self._sources[name] = h
            return h

    def record(self, name, ok, now=None):
        """上报一次取数结果：ok=True 成功 / False 失败（驱动熔断计数）。"""
        h = self.source(name)
        with self._lock:
            if ok:
                h.record_success(now)
            else:
                h.record_failure(now)
        return h

    def reset(self):
        with self._lock:
            self._sources.clear()

    def names(self):
        with self._lock:
            return sorted(self._sources)

    def snapshot(self, name):
        return self.source(name).snapshot()

    def snapshots(self):
        with self._lock:
            return {n: h.snapshot() for n, h in sorted(self._sources.items())}

    def open_sources(self):
        """当前处于熔断（OPEN）状态的源快照列表。"""
        with self._lock:
            return [h.snapshot() for h in self._sources.values() if h.state == OPEN]


# 全应用共享的健康总账
REGISTRY = HealthRegistry()
