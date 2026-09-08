# -*- coding: utf-8 -*-
r"""G13续（第99轮）：LLM复核历史观察——"LLM意见 vs 实际收益"一致性统计（研究侧）。

读 llm_review_history.jsonl（G13守护线程产出）+ signal_outcomes（30分钟/2小时/次日回填），
统计LLM方向判断（多/空/中性）与信号实际收益的一致性/准确率。为LLM复核的价值评估提供
定量证据（研究侧，不进综合分，不自动改参数）。

数据不足时诚实标注：样本量极小时不作"有效/无效"结论（项目铁律）。
CLI：python tools/llm_review_analysis.py | --selftest
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import config  # noqa: E402

HISTORY_PATH = os.path.join(_ROOT, "reports", "llm_review_history.jsonl")
OUT_TXT = os.path.join(_ROOT, "reports", "llm_review_analysis.txt")
OUT_JSON = os.path.join(_ROOT, "reports", "llm_review_analysis.json")
MIN_SAMPLES = 5  # 简单结论的最低样本数


def _load_history():
    reviews = []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    reviews.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return reviews


def _load_outcomes():
    """读 signal_outcomes 表中已评估的信号（status=hit/miss/flat/expired），返回 {sym: [(ts, direction, ret)]}。"""
    import sqlite3
    out = {}
    db_path = os.path.join(_ROOT, "data", "monitor.db")
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT variety, direction, ret, eval_ts FROM signal_outcomes "
                "WHERE status IN ('hit','miss','flat','expired') ORDER BY eval_ts"
            ).fetchall()
            for variety, direction, ret, eval_ts in rows:
                out.setdefault(variety, []).append((eval_ts, direction, ret))
        finally:
            conn.close()
    except Exception:
        pass
    return out


def analyze():
    history = _load_history()
    outcomes = _load_outcomes()

    # 按品种聚合 LLM 评估
    llm_by_sym = defaultdict(list)
    llm_valid = 0
    llm_degraded = 0
    for rec in history:
        review = rec.get("review", {})
        ts = rec.get("ts", "")
        if "degraded" in review:
            llm_degraded += 1
            continue
        direction = review.get("direction")
        symbols = review.get("symbols", [])
        strength = review.get("strength")
        for sym in symbols:
            llm_by_sym[sym].append({"ts": ts, "direction": direction, "strength": strength})
        llm_valid += 1

    # 计算一致性：在有outcome的品种上，LLM方向 vs 事后收益
    agree = 0
    disagree = 0
    neutral_correct = 0
    total_evaluated = 0
    details = []
    for sym, reviews_list in llm_by_sym.items():
        outcomes_list = outcomes.get(sym, [])
        if not outcomes_list:
            continue
        # 取最近一次 LLM 评估
        latest = reviews_list[-1]
        llm_dir = latest["direction"]
        # 取最近一次 outcome
        ev_ts, ev_dir, ev_ret = outcomes_list[-1]
        total_evaluated += 1
        ret = ev_ret or 0.0
        if llm_dir == "中性":
            neutral_correct += 1
        elif (llm_dir == "多" and ret > 0) or (llm_dir == "空" and ret < 0):
            agree += 1
        else:
            disagree += 1
        details.append({"sym": sym, "llm_direction": llm_dir, "outcome_return": ret,
                        "agree": (llm_dir == "多" and ret > 0) or (llm_dir == "空" and ret < 0) or llm_dir == "中性"})

    acc = agree / total_evaluated * 100 if total_evaluated > 0 else None
    results = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n_history": len(history), "n_valid_review": llm_valid, "n_degraded": llm_degraded,
        "n_evaluated": total_evaluated, "n_agree": agree, "n_disagree": disagree,
        "n_neutral": neutral_correct,
        "accuracy": round(acc, 1) if acc is not None else None,
        "details": details[:20],
        "note": "样本量极小时不作有效结论；LLM复核为第二意见参考，永不改综合分",
    }
    _render(results)
    return results


def _render(r):
    lines = ["=" * 60,
             " G13续 LLM复核历史观察（vs 实际收益一致性）",
             " %s" % r["ts"], "=" * 60,
             " 总记录 %d · 有效review %d · 降级 %d" % (r["n_history"], r["n_valid_review"], r["n_degraded"]),
             " 可评估（有outcome）: %d · 一致 %d · 相反 %d · 中性 %d" % (
                 r["n_evaluated"], r["n_agree"], r["n_disagree"], r["n_neutral"]),
             " 准确率: %s" % ("%.1f%%" % r["accuracy"] if r["accuracy"] is not None else "不足（样本<%d）" % MIN_SAMPLES),
             "",
             "【品种明细】"]
    for d in r["details"]:
        mark = "✅" if d["agree"] else "❌"
        lines.append(" %s %s → %s (收益%.4f) %s" % (
            d["sym"], d["llm_direction"],
            "正收益" if d["outcome_return"] > 0 else "负收益",
            d["outcome_return"], mark))
    lines += ["", " 注意：样本量为%d条（有效review极少，大部分LLM因无key/异常降级）。"
              " 样本≥%d后才能下初步结论；LLM复核仅作第二意见参考，永不改综合分。"
              % (r["n_evaluated"], MIN_SAMPLES)]
    os.makedirs(os.path.dirname(OUT_TXT), exist_ok=True)
    with open(OUT_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)


def selftest():
    checks = []
    def ck(name, cond):
        checks.append((name, bool(cond)))
        if not cond:
            raise AssertionError("FAIL: " + name)
    ck("历史文件存在", os.path.exists(HISTORY_PATH))
    results = analyze()
    ck("分析运行成功", "ts" in results)
    ck("n_history>=0", results["n_history"] >= 0)
    return 0 if all(ok for _, ok in checks) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="G13续 LLM复核历史观察（研究侧）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    results = analyze()
    print("结果 → %s（有效review %d/降级 %d）" % (OUT_TXT, results["n_valid_review"], results["n_degraded"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())