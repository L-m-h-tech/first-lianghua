# -*- coding: utf-8 -*-
"""P1-7 主动告警：本机声音 + 可选 Webhook。

设计目标：
1. 零新增依赖：Windows 声音使用标准库 winsound；HTTP 复用项目全局 http_client；
2. 不阻塞主分析循环：声音和网络推送都在守护线程内执行；
3. 冷却防轰炸：同一品种/同一事件在冷却期内只提醒一次；
4. Webhook 兼容飞书、钉钉、企业微信群机器人、Server酱，以及接收原始 JSON 的通用地址。

配置方法（无需改代码）：
    Windows 环境变量 FUTURES_MONITOR_WEBHOOK 设置为机器人 Webhook 后重启程序；
    也可直接改 config.ALERT_WEBHOOK_URL。
"""
import json
import threading
import time
import urllib.parse

import config
from http_client import http
from utils import LOG, fmt_px

try:
    import winsound
except ImportError:  # 非 Windows 环境静默降级为只推送 Webhook
    winsound = None


def score_band(score):
    """综合分分档：0观望 / 1轻仓 / 2分批 / 3强信号。"""
    s = abs(score)
    if s < config.SCORE_NEUTRAL:
        return 0
    if s < config.ALERT_MID_SCORE:
        return 1
    if s < config.ALERT_STRONG_SCORE:
        return 2
    return 3


class AlertManager:
    """管理跨档提醒、紧急轮动提醒、期权策略提醒与冷却。"""

    _LEVEL_RANK = {"info": 0, "signal": 1, "option": 2, "strong": 3, "emergency": 4}

    def __init__(self):
        self._last_sent = {}
        self._signal_state = {}
        self._strategy_state = set()
        self._lock = threading.Lock()
        self._cycle_events = None      # 非 None 时，一轮内的事件先聚合，避免多信号同时轰炸

    def _allow(self, key, cooldown_sec):
        now = time.time()
        with self._lock:
            last = self._last_sent.get(key, 0.0)
            if now - last < cooldown_sec:
                return False
            self._last_sent[key] = now
            return True

    def observe_cycle(self, state, fut_rows, strat_rows):
        """每轮报告落盘后调用：只在跨档/强信号/新策略出现时提醒。

        同一轮可能同时出现多个强信号；先聚合事件，最后只响一次最高级别声音，
        Webhook 也只发送一条汇总，避免极端行情下声音叠加和手机刷屏。
        """
        events = []
        self._cycle_events = events
        try:
            emerg = getattr(state, "emergency_note", "")
            if emerg:
                em = getattr(state, "last_emergency", {}) or {}
                self.emit("紧急轮动", emerg, level="emergency",
                          key="emergency:%s" % em.get("src", "common"),
                          cooldown=config.ALERT_EMERGENCY_COOLDOWN_SEC)

            for row in sorted(fut_rows or [], key=lambda r: -abs(r.get("score", 0.0))):
                self._observe_future(row)

            if config.ALERT_OPTION_STRATEGY:
                self._observe_strategies(strat_rows or [])
        finally:
            self._cycle_events = None
        self._flush_cycle_events(events)

    def _observe_future(self, row):
        name = row.get("name", "")
        score = float(row.get("score", 0.0))
        band = score_band(score)
        direction = 1 if score > 0 else (-1 if score < 0 else 0)
        # WP-F1 A2：独立风控闸门否决时单独提醒（与跨档信号相互独立，自带冷却限流）
        _risk = row.get("risk") or {}
        if (config.RISK_GATE_ENABLED and config.RISK_GATE_ALERT
                and _risk.get("level") == "veto"):
            self.emit("风控闸门否决",
                      "%s 综合分%+.1f 被风控建议暂缓：%s"
                      % (name, score, "；".join(_risk.get("veto", []))),
                      level="strong", key="risk-veto:%s" % name,
                      cooldown=config.ALERT_SIGNAL_COOLDOWN_SEC)
        prev = self._signal_state.get(name)
        trigger = False
        if band >= 2:
            if prev is None:
                trigger = band >= 3       # 程序刚启动时只提醒强信号，避免64品种基线刷屏
            else:
                prev_dir, prev_band = prev
                trigger = direction != prev_dir or band > prev_band
            if trigger and not (band >= 3 or (config.ALERT_MID_CROSS_ENABLED and band >= 2)):
                trigger = False
        self._signal_state[name] = (direction, band)
        if not trigger:
            return

        if band >= 3:
            level, cooldown = "strong", config.ALERT_SIGNAL_COOLDOWN_SEC
            title = "期货强信号"
        else:
            level, cooldown = "signal", config.ALERT_SIGNAL_COOLDOWN_SEC
            title = "期货跨档信号"

        side = "做多" if score > 0 else "做空"
        contract = row.get("contract_code") or "主力合约探测中"
        parts = " | ".join(f"{k} {v:+.1f}" for k, v in (row.get("parts") or {}).items())
        flow = row.get("flow") or {}
        flow_line = ""
        if flow.get("pattern"):
            flow_line = f"\n量仓: {flow['pattern']}，持仓{flow.get('oi_pct', 0)*100:+.2f}%，量比{flow.get('volume_ratio', 1):.2f}"
        content = (
            f"{name} {row.get('label','')}，综合分 {score:+.1f}\n"
            f"方向: {side} {contract}，最新价 {fmt_px(row.get('price', 0))}\n"
            f"止损 {fmt_px(row.get('stop', 0))} / 目标 {fmt_px(row.get('target', 0))}\n"
            f"因子: {parts}{flow_line}\n"
            f"建议: {row.get('advice','')}"
        )
        # key 带方向和分档：重复同向同档冷却，跨档升级/多空翻转即使在冷却内也要提醒。
        self.emit(title, content, level=level,
                  key="signal:%s:%d:%d" % (name, direction, band),
                  cooldown=cooldown)

    def _observe_strategies(self, strat_rows):
        current = set()
        for s in strat_rows:
            if not s.get("all_pass"):
                continue
            variety = s.get("variety", "")
            name = s.get("name", "")
            key_name = f"{variety}:{name}"
            current.add(key_name)
            if key_name in self._strategy_state:
                continue
            content = (
                f"{variety} {name} 通过全部严格检查\n"
                f"{s.get('verdict','')}\n{s.get('pos_note','')}"
            )
            self.emit("期权策略触发", content, level="option",
                      key="strategy:%s" % key_name,
                      cooldown=config.ALERT_SIGNAL_COOLDOWN_SEC)
        self._strategy_state = current

    def emit(self, title, content, level="info", key=None, cooldown=0):
        """发出一条告警；key+cooldown 用于限流。observe_cycle 内先聚合，轮末统一发送。"""
        if key and cooldown and not self._allow(key, cooldown):
            return
        event = {"title": title, "content": content[:1800], "level": level}
        if self._cycle_events is not None:
            self._cycle_events.append(event)
            return
        threading.Thread(target=self._dispatch,
                         args=(title, event["content"], level),
                         daemon=True, name="alert").start()

    def _flush_cycle_events(self, events):
        if not events:
            return
        top = max(events, key=lambda e: self._LEVEL_RANK.get(e["level"], 0))
        if len(events) == 1:
            title, content = events[0]["title"], events[0]["content"]
        else:
            title = f"期货监控告警（{len(events)}条）"
            blocks = []
            for i, e in enumerate(events, 1):
                blocks.append(f"【{i}.{e['title']}】\n{e['content']}")
            content = "\n\n".join(blocks)[:3500]
        threading.Thread(target=self._dispatch, args=(title, content, top["level"]),
                         daemon=True, name="alert").start()

    def _dispatch(self, title, content, level):
        try:
            self._play_sound(level)
        except Exception as e:
            LOG.debug("告警声音播放失败: %s", e)
        url = (config.ALERT_WEBHOOK_URL or "").strip()
        if not url:
            return
        try:
            self._post_webhook(url, title, content)
            LOG.info("Webhook告警已发送: %s", title)
        except Exception as e:
            LOG.warning("Webhook告警发送失败（不影响监控主流程）: %s", e)

    @staticmethod
    def _play_sound(level):
        if not config.ALERT_SOUND_ENABLED or winsound is None:
            return
        sequences = {
            "emergency": [(880, 180), (1175, 220), (880, 180), (1175, 260)],
            "strong": [(784, 160), (988, 220)],
            "option": [(659, 140), (880, 200)],
            "signal": [(659, 120), (784, 160)],
            "info": [(659, 120)],
        }
        for freq, dur in sequences.get(level, sequences["info"]):
            winsound.Beep(freq, dur)
            time.sleep(0.04)

    @staticmethod
    def _detect_type(url):
        kind = (config.ALERT_WEBHOOK_TYPE or "auto").lower()
        if kind != "auto":
            return kind
        low = url.lower()
        if "feishu.cn" in low or "larksuite.com" in low:
            return "feishu"
        if "dingtalk" in low:
            return "dingtalk"
        if "qyapi.weixin.qq.com" in low:
            return "wecom"
        if "ftqq.com" in low or "serverchan" in low:
            return "serverchan"
        return "generic"

    def _post_webhook(self, url, title, content):
        kind = self._detect_type(url)
        text = f"{title}\n{content}"
        if kind == "feishu":
            payload = {"msg_type": "text", "content": {"text": text}}
            r = http.post(url, json=payload, timeout=config.ALERT_WEBHOOK_TIMEOUT)
        elif kind == "dingtalk":
            payload = {"msgtype": "text", "text": {"content": text}}
            r = http.post(url, json=payload, timeout=config.ALERT_WEBHOOK_TIMEOUT)
        elif kind == "wecom":
            payload = {"msgtype": "text", "text": {"content": text}}
            r = http.post(url, json=payload, timeout=config.ALERT_WEBHOOK_TIMEOUT)
        elif kind == "serverchan":
            data = urllib.parse.urlencode({"title": title, "desp": content})
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            r = http.post(url, data=data.encode("utf-8"), headers=headers,
                          timeout=config.ALERT_WEBHOOK_TIMEOUT)
        else:
            payload = {"title": title, "content": content, "text": text}
            r = http.post(url, json=payload, timeout=config.ALERT_WEBHOOK_TIMEOUT)
        if getattr(r, "status_code", 200) >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
        # 多数机器人业务错误也在 200 JSON 中，记录但不重复抛异常刷屏。
        try:
            body = r.json()
            if isinstance(body, dict):
                code = body.get("code", 0)
                errcode = body.get("errcode", 0)
                if code not in (0, None) or errcode not in (0, None):
                    LOG.warning("Webhook返回业务异常: %s", json.dumps(body, ensure_ascii=False)[:200])
        except Exception:
            pass
