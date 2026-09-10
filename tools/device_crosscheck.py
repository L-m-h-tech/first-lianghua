# -*- coding: utf-8 -*-
r"""装置行情交叉校验 tools/device_crosscheck.py（研究侧，不进综合分）。

数据来源对比：
  A) 装置行情：monitor.db quotes 表 cycle=99（界面操作收集装置经 fusion 写入，
     含 legend_ui / ths_ui / openvlab 三个源，cat 字段打标）
  B) 量化主链行情：futures_data.fetch_quotes（新浪主连 + 东财兜底，实时）

本工具做同品种价格偏差校验：|装置价 - 主链价| / 主链价 > 阈值即标注。
只读 monitor.db + 网络拉一次主链行情；不改 analyzer、不进综合分、不影响任何既有输出。

输出：
  reports/device_crosscheck.txt / .json    （看板"研究报告(全部)"自动聚合）

CLI：python tools/device_crosscheck.py | --selftest（零网络合成）
"""
import argparse
import json
import os
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config                     # noqa: E402

DB_PATH = os.path.join(config.DATA_DIR, "monitor.db")
TXT = os.path.join(_ROOT, "reports", "device_crosscheck.txt")
JSON = os.path.join(_ROOT, "reports", "device_crosscheck.json")
DIFF_THRESH_PCT = 1.0             # 价格偏差阈值（与装置 quality.conflict_diff_pct 一致）
MAX_ROWS = 64


def load_device_quotes(conn, cycle=99):
    """读装置写入 quotes 表（cycle=99）的最新一轮：{sym: {source, price, ts}}"""
    n = 0
    out = {}
    for code, cat, price, ts in conn.execute(
            "SELECT code, cat, price, ts FROM quotes WHERE cycle=? "
            "ORDER BY ts DESC LIMIT ?", (int(cycle), 2000)):
        sym = str(code).upper()
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            continue
        if price_f <= 0:
            continue
        # 取该 sym 最近一条（先到先得）
        if sym not in out:
            out[sym] = {"source": str(cat or ""), "price": price_f, "ts": ts}
            n += 1
    return out, n


def _sym_code(sym):
    """sym -> 新浪主连代码（RB -> RB0）。"""
    s = str(sym).strip().upper()
    if not s:
        return None
    # 数字结尾视为已是连续代码（如 RB0）
    if s[-1].isdigit():
        return s
    return s + "0"


def compare(device, live):
    """逐 sym 对照。:return: [{"sym","device_source","device_price","live_price","diff_pct","ok"}]"""
    rows = []
    for sym, d in device.items():
        code = _sym_code(sym)
        lq = live.get(code) or live.get(sym) or live.get(str(code).lower())
        if not lq:
            continue
        try:
            lp = float(lq.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if lp <= 0:
            continue
        diff = abs(d["price"] - lp) / lp * 100.0
        rows.append({"sym": sym, "device_source": d["source"],
                     "device_price": round(d["price"], 2),
                     "live_price": round(lp, 2),
                     "diff_pct": round(diff, 2), "ok": diff <= DIFF_THRESH_PCT})
    rows.sort(key=lambda r: r["diff_pct"], reverse=True)
    return rows


def render(device, live, rows):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = ["装置行情 vs 量化主链行情 交叉校验（研究侧）",
             "=" * 60,
             "生成: %s | 装置 cycle=99 行情 %d 条 | 偏差阈值 %.1f%%" % (
                 now, len(device), DIFF_THRESH_PCT), ""]
    matched = len(rows)
    ok_n = sum(1 for r in rows if r["ok"])
    bad_n = matched - ok_n
    lines.append("可对照 %d 品种（装置有+主链有）｜一致 %d ｜超阈值 %d" % (matched, ok_n, bad_n))
    lines.append("")
    if bad_n:
        lines.append("一、偏差超阈值品种（>%.1f%%）" % DIFF_THRESH_PCT)
        lines.append("  %-8s %-10s %10s %10s %8s" % ("品种", "装置源", "装置价", "主链价", "偏差%"))
        for r in rows:
            if not r["ok"]:
                lines.append("  %-8s %-10s %10.2f %10.2f %7.2f" % (
                    r["sym"], r["device_source"], r["device_price"], r["live_price"], r["diff_pct"]))
        lines.append("")
    lines.append("二、全部可对照行情（偏差降序 Top %d）" % min(MAX_ROWS, len(rows)))
    lines.append("  %-8s %-10s %10s %10s %8s %s" % ("品种", "装置源", "装置价", "主链价", "偏差%", "状态"))
    for r in rows[:MAX_ROWS]:
        lines.append("  %-8s %-10s %10.2f %10.2f %7.2f %s" % (
            r["sym"], r["device_source"], r["device_price"], r["live_price"],
            r["diff_pct"], "OK" if r["ok"] else "!!"))
    lines.append("")
    lines.append("说明：装置行情来自界面操作收集装置（Legend/同花顺/OpenVLab 界面采集），"
                 "只读对照、研究侧、不进综合分。")
    summary = {"generated": now, "device_rows": len(device), "matched": matched,
               "ok": ok_n, "over_threshold": bad_n, "rows": rows[:MAX_ROWS]}
    return "\n".join(lines) + "\n", summary


def save(lines, summary):
    os.makedirs(os.path.dirname(TXT), exist_ok=True)
    with open(TXT, "w", encoding="utf-8-sig") as fh:
        fh.write(lines)
    with open(JSON, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    return TXT, JSON


def selftest():
    """合成数据零网络：构造装置/主链行情并校验对照逻辑。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE quotes(code TEXT, cycle INT, cat TEXT, price REAL, ts TEXT)")
    conn.executemany("INSERT INTO quotes VALUES(?,99,?,?,?)", [
        ("RB", "legend_ui", 3200.0, "2026-09-08 20:00:00"),
        ("CU", "openvlab", 71000.0, "2026-09-08 20:00:00"),
        ("AG", "ths_ui", 7500.0, "2026-09-08 20:00:00"),
    ])
    device, n = load_device_quotes(conn, cycle=99)
    assert n == 3, n
    live = {"RB0": {"price": 3205.0}, "CU0": {"price": 72000.0}, "AG0": {"price": 7510.0}}
    rows = compare(device, live)
    assert len(rows) == 3
    rb = next(r for r in rows if r["sym"] == "RB")
    ag = next(r for r in rows if r["sym"] == "AG")
    assert rb["ok"] is True and abs(rb["diff_pct"] - 0.16) < 0.1, rb
    assert ag["ok"] is True
    lines, summary = render(device, live, rows)
    assert "交叉校验" in lines and summary["matched"] == 3
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="装置行情 vs 主链行情 交叉校验（研究侧）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    conn = sqlite3.connect(DB_PATH)
    try:
        device, _n = load_device_quotes(conn)
    finally:
        conn.close()
    # 主链实时（新浪 + 东财兜底，与量化主循环同源）
    import futures_data
    live_codes = set()
    for r in list(device.keys()):
        c = _sym_code(r)
        if c:
            live_codes.add(c)
    live = futures_data.fetch_quotes(sorted(live_codes)) or {}
    rows = compare(device, live)
    lines, summary = render(device, live, rows)
    txt, js = save(lines, summary)
    print("device_crosscheck: 装置 %d 条 / 可对照 %d / 超阈值 %d → %s" % (
        len(device), len(rows), summary["over_threshold"], txt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())