# -*- coding: utf-8 -*-
r"""第94轮 B2（对标 scrapling 的 LLM 辅助自适应选择器生成）：解析器损坏时用 LLM 给修复建议。

定位（与 llm_reviewer 同纪律）：**只出建议、不改代码、软降级**——当 A1 parser_health 发出
某源解析告警时，把这个源的 HTML 片段 + 期望字段交给 LLM（走现有 http_client + DeepSeek key，
约 2K token≈0.002 元/次），强制 JSON schema 返回：候选解析规则（正则/字段位置/表格列号）、
理由、置信度。输出 reports/selector_heal.txt / .jsonl 供人工审阅采纳。

不接 main；研究侧工具，CLI 用法：
  python tools/selector_heal.py --source em_inventory --html-file cache/debug.html \
      --expect "库存,仓单,环比"            # 喂 HTML 文件给 LLM 出建议
  python tools/selector_heal.py --selftest   # 零网络合成断言
无 key / 断网 / 坏 JSON 全部软降级（输出 degraded 记录，绝不抛）。
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import llm_reviewer            # 复用 G13 的 key/base_url/model/enabled（同一 DeepSeek key）
from http_client import http   # noqa: E402

REPORT_TXT = os.path.join(_ROOT, "reports", "selector_heal.txt")
REPORT_JSONL = os.path.join(_ROOT, "reports", "selector_heal.jsonl")
MAX_HTML_CHARS = 6000          # 喂给 LLM 的 HTML 片段上限（成本/上下文控制）
TIMEOUT = 30

_PROMPT = (
    "你是期货数据采集系统的 HTML 解析修复专家。给定一个解析失败的数据源、其网页片段和期望字段，"
    "请给出**可落地的新解析规则建议**。只输出一个 JSON 对象（无代码块、无多余文本），schema："
    '{"source":"源标识","suggestions":[{"what":"提取字段或数据结构说明","how":"具体做法：'
    '正则表达式/字段在逗号分隔中的位置/HTML表格列号/锚点文本","confidence":0.0到1.0的小数}],'
    '"reason":"200字以内的诊断","html_notes":"该页面与常见结构不同的地方（若有）"}'
)


def enabled():
    return llm_reviewer.enabled()


def _ask_llm(source, html, expect):
    payload = {"model": llm_reviewer.model(),
               "messages": [{"role": "system", "content": _PROMPT},
                            {"role": "user", "content": (
                                f"源：{source}\n期望字段：{expect}\n"
                                f"页面前{MAX_HTML_CHARS}字符：\n{html[:MAX_HTML_CHARS]}")}],
               "temperature": 0.2, "max_tokens": 600}
    r = http.post(llm_reviewer.base_url() + "/chat/completions",
                  headers={"Content-Type": "application/json",
                           "Authorization": "Bearer " + (llm_reviewer.key() or "")},
                  json=payload, timeout=TIMEOUT)
    if r.status_code != 200:
        return {"degraded": "http_%d" % r.status_code}
    try:
        content = (r.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return {"degraded": "bad_response"}
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return {"degraded": "no_json"}
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return {"degraded": "bad_json"}
    if not isinstance(obj, dict):
        return {"degraded": "bad_schema"}
    obj["_raw"] = content[:2000]
    return obj


def heal(source, html, expect):
    """对单个源出修复建议；无 key/异常软降级为 degraded 记录。返回 dict。"""
    try:
        if not enabled():
            return {"degraded": "no_key"}
        result = _ask_llm(source, html or "", expect or "")
        _log(result, source)
        return result
    except Exception as e:
        out = {"degraded": "exception", "err": str(e)[:200]}
        try:
            _log(out, source)
        except Exception:
            pass
        return out


def _log(result, source):
    import time
    os.makedirs(os.path.dirname(REPORT_JSONL), exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "source": source, "result": result}
    with open(REPORT_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _render_txt()


def _render_txt():
    rows = []
    try:
        with open(REPORT_JSONL, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    rows.append(json.loads(ln))
    except OSError:
        pass
    with open(REPORT_TXT, "w", encoding="utf-8") as f:
        f.write("=" * 70 + "\n")
        f.write(" B2 LLM 解析器修复建议（只出建议不改代码；采纳需人工审阅）\n")
        f.write("=" * 70 + "\n")
        for rec in rows[-20:]:
            r = rec["result"]
            state = "degraded(%s)" % r.get("degraded") if "degraded" in r else "suggestions"
            f.write("\n[%s] %s → %s\n" % (rec["ts"], rec["source"], state))
            if "degraded" not in r:
                for s in (r.get("suggestions") or [])[:5]:
                    f.write("  · %s: %s (置信度 %.2f)\n"
                            % (s.get("what", ""), s.get("how", ""), s.get("confidence", 0.0)))
    return REPORT_TXT


# ---------------- selftest（零网络） ----------------

def selftest():
    """零网络合成断言：prompt 结构 / JSON 提取 / 软降级路径。"""
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))
        if not cond:
            raise AssertionError("FAIL: " + name)

    ck("prompt 含 schema 要求", "JSON" in _PROMPT and "confidence" in _PROMPT)
    # 坏 JSON → degraded
    ck("坏JSON降级", _parse_content("不是JSON")["degraded"] == "no_json")
    # 嵌套 JSON 提取
    r = _parse_content('好的：{"source":"x","suggestions":[{"what":"a","how":"b","confidence":0.9}],"reason":"r"}')
    ck("嵌套JSON提取成功", r.get("suggestions") and r["suggestions"][0]["what"] == "a")
    # 无 key → 软降级 no_key（不真发请求）
    saved = llm_reviewer
    try:
        llm_reviewer._FAKE_KEY = None
        # 用 monkeypatch 思路：直接 patch 模块函数更稳
        import config as _cfg
        old = os.environ.get("FUTURES_MONITOR_LLM_KEY")
        os.environ.pop("FUTURES_MONITOR_LLM_KEY", None)
        try:
            ck("无key零请求降级", heal("x", "<html></html>", "库存")["degraded"] == "no_key")
        finally:
            if old is not None:
                os.environ["FUTURES_MONITOR_LLM_KEY"] = old
    finally:
        pass
    _render_txt()
    ck("报告已生成", os.path.exists(REPORT_TXT))
    return 0 if all(ok for _, ok in checks) else 1


def _parse_content(content):
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return {"degraded": "no_json"}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {"degraded": "bad_schema"}
    except ValueError:
        return {"degraded": "bad_json"}


def main(argv=None):
    ap = argparse.ArgumentParser(description="B2 LLM 解析器修复建议（研究侧，只出建议）")
    ap.add_argument("--source", default="unknown", help="源标识")
    ap.add_argument("--html-file", default=None, help="HTML 片段文件路径")
    ap.add_argument("--expect", default="", help="期望字段，逗号分隔")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    html = ""
    if args.html_file:
        try:
            html = Path(args.html_file).read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            print("读取 HTML 文件失败: %s" % e)
            return 1
    result = heal(args.source, html, args.expect)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    print("→ %s" % REPORT_TXT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())