# -*- coding: utf-8 -*-
r"""G13 LLM 第二意见复核适配层（无 key 完全休眠）——第84轮解锁落地。

定位（总纲 G13，铁律第7条）：LLM 只做**第二意见文本复核**，永不改综合分、永不下单、
不做端到端决策；异常绝不进主循环。

只对三类小流量触发（G13 设计原文）：
  ① 紧急轮动新闻（state.last_emergency 非空）；
  ② |综合分|≥6.5 的强信号（含其新闻面 part）；
  ③ 词典情绪（新闻消息面 part）与技术分明显背离（异号且双方|值|≥1.5）。

协议：OpenAI 兼容 /chat/completions（走现有 http_client，DeepSeek 级云端或本地 Ollama）；
key 走环境变量 FUTURES_MONITOR_LLM_KEY / FUTURES_MONITOR_LLM_BASE_URL（默认 api.deepseek.com）/
FUTURES_MONITOR_LLM_MODEL（默认 deepseek-chat）。守护线程异步调用（main 在 report.save 后启动），
**无 key 零请求零开销**；超时/非200/坏 JSON 全部软降级为 {"degraded": 原因} 记录，绝不抛出。

输出：reports/llm_review.txt（最新一次）+ reports/llm_review_history.jsonl（追加）。
强制 JSON schema（越界一律裁剪）：direction∈{多,空,中性} / strength∈[1,5] / symbols≤10 /
uncertainty∈[0,1] / reason≤300字 / agrees_with_lexicon∈bool。
"""
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import http_client
import threading

LOG = logging.getLogger("llm_reviewer")

_LAST_THREAD = [None]          # 最近一次复核线程引用（--once 模式退出前 join，防守护线程被杀）

ROOT = Path(__file__).resolve().parent        # 本模块在项目根（区别于 tools/ 的 parents[1]）
REPORT_TXT = ROOT / "reports" / "llm_review.txt"
REPORT_JSONL = ROOT / "reports" / "llm_review_history.jsonl"

SCORE_TRIGGER = 6.5          # ②强信号阈值（G13 设计原文）
DIVERGE_EDGE = 1.5           # ③背离双方的最小绝对值
TIMEOUT = 30
MAX_CONTEXT_SIGNALS = 3      # 上下文只带 |综合分| 最大的前3条（小流量纪律）
DIRECTIONS = ("多", "空", "中性")

_SYSTEM_PROMPT = (
    "你是期货程序化交易系统的第二意见复核员。给定某轮信号上下文（品种/综合分/九因子分项/"
    "词典情绪与技术分），你只输出**一个严格 JSON 对象**（无代码块、无多余文本），schema："
    '{"direction":"多|空|中性","strength":1到5的整数,"symbols":["品种名"...],'
    '"uncertainty":0.0到1.0的小数,"reason":"不超过200字的理由","agrees_with_lexicon":true或false}。'
    "你的意见仅作人工复核参考，不会改变系统评分。"
)


# ---------------- 环境与开关（无 key 零请求零开销） ----------------
def key():
    return os.environ.get("FUTURES_MONITOR_LLM_KEY") or None


def base_url():
    return (os.environ.get("FUTURES_MONITOR_LLM_BASE_URL") or "https://api.deepseek.com").rstrip("/")


def model():
    return os.environ.get("FUTURES_MONITOR_LLM_MODEL") or "deepseek-chat"


def enabled():
    """无 key → False：main 不启动线程、零请求零开销（G13 验收第一条）。"""
    return bool(key())


def wait_last(timeout=90):
    """--once 模式退出前有界等待最后一次复核完成（守护线程会随进程被杀，须 join）。"""
    t = _LAST_THREAD[0]
    if t is not None and t.is_alive():
        t.join(timeout=timeout)
    return _LAST_THREAD[0] is not None and not t.is_alive() if t else True


# ---------------- 三类触发器（G13 设计原文） ----------------
def _isnum(x):
    return isinstance(x, (int, float)) and x == x and -1e308 < x < 1e308


def diverges(row):
    """③词典情绪（新闻消息面 part）与技术分明显背离：异号且双方 |值|≥1.5。"""
    parts = row.get("parts") or {}
    news = parts.get("新闻消息面")
    tech = row.get("tech")
    if not _isnum(news) or not _isnum(tech):
        return False
    return (news >= DIVERGE_EDGE and tech <= -DIVERGE_EDGE) or \
           (news <= -DIVERGE_EDGE and tech >= DIVERGE_EDGE)


def triggers(fut_rows, emergency=None):
    """返回触发原因列表 [("emergency"|"strong_signal"|"divergence", 描述)]；空=本轮不调用。"""
    out = []
    if emergency:
        out.append(("emergency", "①紧急轮动新闻"))
    strong = sorted((r for r in fut_rows
                     if _isnum(r.get("score")) and abs(r["score"]) >= SCORE_TRIGGER),
                    key=lambda r: -abs(r["score"]))
    if strong:
        names = "、".join("%s(%+.1f)" % (r.get("name"), r["score"]) for r in strong[:3])
        out.append(("strong_signal", "②强信号|综合分|≥%.1f：%s" % (SCORE_TRIGGER, names)))
    div = [r for r in fut_rows if diverges(r)]
    if div:
        names = "、".join(str(r.get("name")) for r in div[:3])
        out.append(("divergence", "③词典情绪与技术/基本面背离：%s" % names))
    return out


def build_context(fut_rows, emergency=None):
    """紧凑上下文（小流量纪律：|综合分| 最大的前 MAX_CONTEXT_SIGNALS 条 + 触发器 + 紧急事件）。"""
    rows = [r for r in fut_rows if _isnum(r.get("score"))]
    rows.sort(key=lambda r: -abs(r["score"]))
    signals = []
    for r in rows[:MAX_CONTEXT_SIGNALS]:
        parts = r.get("parts") or {}
        signals.append({
            "variety": r.get("name"), "score": r.get("score"),
            "advice": r.get("advice"), "tech": r.get("tech"),
            "fundamental": r.get("fundamental"),
            "news_part": parts.get("新闻消息面"),
            "diverges": diverges(r),
        })
    return {"generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "triggers": triggers(fut_rows, emergency),
            "emergency": emergency, "signals": signals,
            "note": "LLM 只做第二意见，不改综合分"}


# ---------------- schema 解析与裁剪（验收：越界裁剪/坏 JSON 降级） ----------------
def parse_review(text):
    """LLM 文本 → 裁剪后的 dict；无法解析抛 ValueError（由调用方降级）。"""
    s = str(text).strip()
    s = re.sub(r"^```(?:json)?|```$", "", s.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        raise ValueError("no json object")
    obj = json.loads(m.group(0))
    direction = str(obj.get("direction", "中性")).strip()
    if direction not in DIRECTIONS:
        direction = "中性"
    try:
        strength = int(round(float(obj.get("strength", 3))))
    except (TypeError, ValueError):
        strength = 3
    strength = max(1, min(5, strength))
    try:
        unc = float(obj.get("uncertainty", 0.5))
    except (TypeError, ValueError):
        unc = 0.5
    unc = max(0.0, min(1.0, unc))
    symbols = [str(x)[:20] for x in (obj.get("symbols") or [])][:10]
    reason = str(obj.get("reason", ""))[:300]
    agrees = bool(obj.get("agrees_with_lexicon"))
    return {"direction": direction, "strength": strength, "symbols": symbols,
            "uncertainty": round(unc, 2), "reason": reason,
            "agrees_with_lexicon": agrees}


# ---------------- LLM 调用（transport 可注入，测试用 mock） ----------------
def _default_transport(url, payload, timeout):
    """OpenAI 兼容 /chat/completions 调用（走现有 http_client 连接池，http 为会话实例）。返回 (status, text)。"""
    headers = {"Content-Type": "application/json",
               "Authorization": "Bearer %s" % key()}
    r = http_client.http.post(url, json=payload, headers=headers, timeout=timeout)
    return r.status_code, r.text


def review(fut_rows, emergency=None, transport=None, force=False):
    """同步复核：触发器全空或无 key → None（零请求）；否则返回
    {"trigger_reasons", "context", "review"(裁剪后dict), "model", "raw"} 或
    {"degraded": 原因, "trigger_reasons", "context"}。**绝不抛出**。"""
    try:
        if not key():
            return None
        ctx = build_context(fut_rows, emergency=emergency)
        if not ctx["triggers"]:
            if not force:
                return None                              # 三类触发器全空：零调用
            ctx["triggers"] = [("force", "人工强制(--llm-force,无自然触发器)")]
        transport = transport or _default_transport
        payload = {"model": model(),
                   "messages": [{"role": "system", "content": _SYSTEM_PROMPT},
                                {"role": "user", "content": json.dumps(ctx, ensure_ascii=False)}],
                   "temperature": 0.2, "max_tokens": 512, "stream": False}
        status, text = transport("%s/chat/completions" % base_url(), payload, TIMEOUT)
        if status != 200:
            return {"degraded": "http_%d" % status, "trigger_reasons": ctx["triggers"],
                    "context": ctx}
        try:
            body = json.loads(text)
            content = body["choices"][0]["message"]["content"]
        except Exception:
            return {"degraded": "bad_response_body", "trigger_reasons": ctx["triggers"],
                    "context": ctx}
        try:
            parsed = parse_review(content)
        except Exception:
            return {"degraded": "bad_json", "raw": str(content)[:200],
                    "trigger_reasons": ctx["triggers"], "context": ctx}
        parsed.update({"trigger_reasons": ctx["triggers"], "model": model(),
                       "context": ctx, "raw": str(content)[:400]})
        return parsed
    except Exception as e:                               # 兜底：异常绝不进主循环
        return {"degraded": "exception:%s:%s" % (type(e).__name__, e)}


# ---------------- 落盘与异步入口 ----------------
def render(result):
    L = ["=" * 96,
         " G13 LLM 第二意见复核（只读复核，不改综合分/不下单）  生成于 %s"
         % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         "=" * 96]
    if result is None:
        L.append("本轮未触发（无 key / 三类触发器全空）。")
        L.append("=" * 96)
        return "\n".join(L)
    if "degraded" in result:
        L.append("[降级] %s（软降级：本轮无第二意见，主循环不受影响）" % result["degraded"])
    else:
        L.append("触发：%s" % "；".join(d for _c, d in result.get("trigger_reasons", [])))
        rv = {k: v for k, v in result.items()
              if k not in ("trigger_reasons", "context", "raw")}
        L.append(json.dumps(rv, ensure_ascii=False, indent=1))
    ctx = result.get("context") or {}
    for sig in ctx.get("signals", []) or []:
        L.append("  上下文·%s 综合分%+.1f tech%s 基本面%s 新闻面%s 建议=%s"
                 % (sig.get("variety"), sig.get("score") or 0.0,
                    sig.get("tech"), sig.get("fundamental"),
                    sig.get("news_part"), sig.get("advice")))
    if result is not None and "degraded" not in result:
        L.append("  原始输出(≤400字): %s" % (result.get("raw") or ""))
    L.append("=" * 96)
    return "\n".join(L)


def persist(result, txt_path=REPORT_TXT, jsonl_path=REPORT_JSONL):
    """写最新报告 + 追加历史（review_async 专用，独立 sidecar、不碰报告主文件）。"""
    os.makedirs(os.path.dirname(str(txt_path)), exist_ok=True)
    with open(txt_path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(render(result) + "\n")
    slim = {k: v for k, v in (result or {}).items()
            if k not in ("context",)}                      # context 已在 txt 里，jsonl 存精简版
    with open(jsonl_path, "a", encoding="utf-8", newline="\n") as fp:
        fp.write(json.dumps({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             "review": slim}, ensure_ascii=False) + "\n")


# ---------------- 成本节流（第84轮补丁：每日上限 + 同品种同日去重） ----------------
def max_per_day():
    """每日 LLM 调用上限（env FUTURES_MONITOR_LLM_MAX_PER_DAY 可调，默认3）。--llm-force 不受限。"""
    try:
        return max(0, int(os.environ.get("FUTURES_MONITOR_LLM_MAX_PER_DAY", "3") or 3))
    except ValueError:
        return 3


def _today_calls(jsonl_path=REPORT_JSONL):
    """今天已发生的复核次数（从 history.jsonl 的 ts 统计，无独立状态文件）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    n = 0
    if os.path.exists(jsonl_path):
        with open(jsonl_path, encoding="utf-8") as fp:
            for line in fp:
                try:
                    if json.loads(line).get("ts", "").startswith(today):
                        n += 1
                except Exception:
                    continue
    return n


def _reviewed_varieties_today(jsonl_path=REPORT_JSONL):
    """今天已被复核过的品种集合（来自 history 的 context.signals）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    out = set()
    if os.path.exists(jsonl_path):
        with open(jsonl_path, encoding="utf-8") as fp:
            for line in fp:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not rec.get("ts", "").startswith(today):
                    continue
                for sig in (rec.get("review", {}).get("context", {}) or {}).get("signals", []) or []:
                    if sig.get("variety"):
                        out.add(str(sig["variety"]))
    return out


def throttle_reason(fut_rows, jsonl_path=REPORT_JSONL):
    """返回节流原因字符串（应跳过）或 None（放行）。规则：
    1) 今日调用数已达 max_per_day → cap；
    2) 本轮 top 信号品种今天已全部被复核过 → dedup（同一强信号持续多轮不重复喂 LLM）。"""
    cap = max_per_day()
    if _today_calls(jsonl_path) >= cap:
        return "cap(每日上限%d)" % cap
    rows = [r for r in fut_rows if _isnum(r.get("score"))]
    rows.sort(key=lambda r: -abs(r["score"]))
    tops = {str(r.get("name")) for r in rows[:MAX_CONTEXT_SIGNALS] if r.get("name")}
    if tops and tops <= _reviewed_varieties_today(jsonl_path):
        return "dedup(top品种今日已复核)"
    return None


def review_async(fut_rows, emergency=None, force=False, transport=None, jsonl_path=None):
    """守护线程入口（main 在 report.save 后以 daemon 线程调用）：**全 try/except 包裹，绝不抛出**。"""
    _LAST_THREAD[0] = threading.current_thread()
    try:
        if not force:
            th = throttle_reason(fut_rows, jsonl_path=jsonl_path or REPORT_JSONL)
            if th:
                LOG.info("G13 节流跳过：%s", th)
                return None
        r = review(fut_rows, emergency=emergency, transport=transport, force=force)
        if r is not None:
            persist(r)
        return r
    except Exception as e:                               # 双保险：线程内任何异常都不外溢
        try:
            LOG.error("G13 review_async exception swallowed")
        except Exception:
            pass
        return None

def selftest():
    """零网络自检：触发器/裁剪/解析/渲染/键缺失（被 pytest 与 --selftest 复用）。"""
    os.environ["FUTURES_MONITOR_LLM_KEY"] = "test-key"   # 测试用：call-time 读 env
    os.environ.pop("FUTURES_MONITOR_LLM_BASE_URL", None)
    os.environ.pop("FUTURES_MONITOR_LLM_MODEL", None)
    rows = [{"name": "螺纹钢", "score": 7.2, "tech": 2.0, "fundamental": 1.0,
             "parts": {"新闻消息面": 2.5}, "advice": "买入"},
            {"name": "铜", "score": -3.0, "tech": -2.5, "fundamental": 0.5,
             "parts": {"新闻消息面": 2.0}, "advice": "卖出"}]
    tg = triggers(rows, emergency=None)
    assert ("strong_signal", ) == (tg[0][0], ) and ("divergence", ) == (tg[1][0], )
    assert triggers([{"score": 1.0}], emergency=None) == []          # 未触发
    # 裁剪：越界 direction/strength/uncertainty、超长 symbols/reason
    bad = '{"direction":"暴涨","strength":9,"symbols":["%s" % i for i in range(0)],' \
          '"uncertainty":5,"reason":"x"*999,"agrees_with_lexicon":"yes"}'
    bad = '{"direction":"暴涨","strength":9,"symbols":["a","b"],"uncertainty":5,' \
          '"reason":"' + "x" * 999 + '","agrees_with_lexicon":"yes"}'
    p = parse_review(bad)
    assert p["direction"] == "中性" and p["strength"] == 5 and p["uncertainty"] == 1.0
    assert len(p["symbols"]) <= 10 and len(p["reason"]) <= 300 and p["agrees_with_lexicon"] is True
    # 坏 JSON / 非200 / 异常 → 降级 dict
    assert review(rows, transport=lambda u, p, t: (200, "not json"))["degraded"] == "bad_response_body"
    assert review(rows, transport=lambda u, p, t:
                  (200, '{"choices":[{"message":{"content":"not json"}}]}'))["degraded"] == "bad_json"
    assert review(rows, transport=lambda u, p, t: (503, "x"))["degraded"] == "http_503"
    def _boom(url, payload, timeout):
        raise RuntimeError("boom")
    assert "exception:RuntimeError" in review(rows, transport=_boom)["degraded"]
    # codefence 包裹也能解析
    ok = parse_review('```json\n{"direction":"多","strength":4}\n```')
    assert ok["direction"] == "多" and ok["strength"] == 4
    # 渲染
    assert "G13" in render({"degraded": "http_503"})
    # 无 key 路径：enabled()=False、review()=None（零请求零开销，G13 验收第一条）
    os.environ.pop("FUTURES_MONITOR_LLM_KEY", None)
    assert enabled() is False and review(rows) is None
    print("llm_reviewer selftest ALL PASS（触发器三类/裁剪/坏JSON与HTTP与异常三降级/"
          "codefence解析/渲染 共5组）")
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
