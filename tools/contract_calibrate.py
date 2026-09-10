# -*- coding: utf-8 -*-
r"""合约元数据校准 tools/contract_calibrate.py（研究侧，不进综合分）。

数据来源对比：
  A) 装置 ctp_instruments 表（界面操作收集装置 fusion.seed_ctp_instruments 从
     量化 futures_margins.csv/futures_fees.csv 初始化，Legend 采集可补充合约级元数据）
  B) 量化本地配置表：data/futures_margins.csv（乘数/保证金）与 data/futures_fees.csv

本工具对照 ctp_instruments 的 volume_multiple（乘数）与本地表是否一致，输出校准报告，
供 portfolio/paper_broker 手数换算核对（研究侧只读，不改任何配置、不进综合分）。

输出：
  reports/contract_calibrate.txt / .json    （看板"研究报告(全部)"自动聚合）

CLI：python tools/contract_calibrate.py | --selftest（零网络合成）
"""
import argparse
import csv
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
TXT = os.path.join(_ROOT, "reports", "contract_calibrate.txt")
JSON = os.path.join(_ROOT, "reports", "contract_calibrate.json")
MARGINS_CSV = os.path.join(config.DATA_DIR, "futures_margins.csv")
FEES_CSV = os.path.join(config.DATA_DIR, "futures_fees.csv")


def load_ctp_instruments(conn):
    """读装置 ctp_instruments 表：{sym: {volume_multiple, source, updated}}"""
    out = {}
    try:
        for iid, mult, name, source in conn.execute(
                "SELECT instrument_id, volume_multiple, instrument_name, source "
                "FROM ctp_instruments WHERE volume_multiple > 0"):
            out[str(iid).upper()] = {"volume_multiple": float(mult),
                                     "name": str(name or ""), "source": str(source or "")}
    except sqlite3.OperationalError:
        pass  # 表不存在（装置未写过）
    return out


def load_local_tables():
    """读量化本地 margin/fee CSV：{sym: {"margin_mult":..,"fee_mult":..,"name":..}}"""
    out = {}
    for path, key in ((MARGINS_CSV, "margin_mult"), (FEES_CSV, "fee_mult")):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rd = csv.DictReader(fh)
            for row in rd:
                sym = str(row.get("sym") or "").strip().upper()
                if not sym:
                    continue
                o = out.setdefault(sym, {"name": row.get("name", ""), "margin_mult": None,
                                         "fee_mult": None})
                try:
                    o[key] = float(row.get("multiplier") or 0)
                except (TypeError, ValueError):
                    pass
    return out


def compare(dev, local):
    """逐 sym 对照乘数。:return: [{"sym","device_mult","local_mult","match","name"}]"""
    rows = []
    for sym in sorted(set(dev) | set(local)):
        d = dev.get(sym)
        l = local.get(sym)
        dm = d["volume_multiple"] if d else None
        lm = (l["margin_mult"] or l["fee_mult"]) if l else None
        if dm is None and lm is None:
            continue
        match = None
        if dm is not None and lm is not None:
            match = abs(dm - lm) < 1e-6
        rows.append({"sym": sym, "name": (d or l or {}).get("name", ""),
                     "device_mult": dm, "local_mult": lm, "match": match})
    return rows


def render(dev, local, rows):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = ["合约元数据校准（装置 ctp_instruments vs 量化本地配置表）",
             "=" * 60,
             "生成: %s | ctp_instruments %d 条 / 本地表 %d 条" % (
                 now, len(dev), len(local)), ""]
    matched = sum(1 for r in rows if r["match"] is True)
    mism = sum(1 for r in rows if r["match"] is False)
    only_dev = sum(1 for r in rows if r["device_mult"] is not None and r["local_mult"] is None)
    only_local = sum(1 for r in rows if r["device_mult"] is None and r["local_mult"] is not None)
    lines.append("可对照 %d 品种｜一致 %d｜不一致 %d｜仅装置 %d｜仅本地 %d" % (
        len(rows), matched, mism, only_dev, only_local))
    lines.append("")
    if mism:
        lines.append("一、乘数不一致品种")
        lines.append("  %-8s %-10s %10s %10s" % ("品种", "名称", "装置乘数", "本地乘数"))
        for r in rows:
            if r["match"] is False:
                lines.append("  %-8s %-10s %10s %10s" % (
                    r["sym"], r["name"], r["device_mult"], r["local_mult"]))
        lines.append("")
    lines.append("二、全部品种（装置有值优先展示）")
    lines.append("  %-8s %-10s %10s %10s %s" % ("品种", "名称", "装置乘数", "本地乘数", "状态"))
    for r in rows:
        st = "一致" if r["match"] is True else ("不一致" if r["match"] is False else
                                                ("仅装置" if r["device_mult"] is not None else "仅本地"))
        lines.append("  %-8s %-10s %10s %10s %s" % (
            r["sym"], r["name"], r["device_mult"], r["local_mult"], st))
    lines.append("")
    lines.append("说明：ctp_instruments 由界面操作收集装置从本地 CSV seed + Legend 采集补充，"
                 "只读对照、研究侧、不进综合分、不改任何配置。")
    summary = {"generated": now, "device_rows": len(dev), "local_rows": len(local),
               "matched": matched, "mismatch": mism,
               "only_device": only_dev, "only_local": only_local, "rows": rows}
    return "\n".join(lines) + "\n", summary


def save(lines, summary):
    os.makedirs(os.path.dirname(TXT), exist_ok=True)
    with open(TXT, "w", encoding="utf-8-sig") as fh:
        fh.write(lines)
    with open(JSON, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    return TXT, JSON


def selftest():
    """合成数据零网络：验证对照逻辑。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ctp_instruments(instrument_id TEXT, volume_multiple REAL,"
                 " instrument_name TEXT, source TEXT)")
    conn.executemany("INSERT INTO ctp_instruments VALUES(?,?,?,?)", [
        ("RB", 10.0, "螺纹钢", "seed"), ("CU", 5.0, "铜", "seed"),
        ("AG", 15.0, "白银", "seed"),
    ])
    dev = load_ctp_instruments(conn)
    assert set(dev) == {"RB", "CU", "AG"}, dev
    local = {"RB": {"margin_mult": 10.0, "fee_mult": 10.0, "name": "螺纹钢"},
             "CU": {"margin_mult": 5.0, "fee_mult": 5.0, "name": "铜"},
             "AU": {"margin_mult": 1000.0, "fee_mult": 1000.0, "name": "黄金"}}
    rows = compare(dev, local)
    rb = next(r for r in rows if r["sym"] == "RB")
    au = next(r for r in rows if r["sym"] == "AU")
    assert rb["match"] is True and au["match"] is None, rows
    lines, summary = render(dev, local, rows)
    assert "校准" in lines and summary["matched"] == 2
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="合约元数据校准（研究侧）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    conn = sqlite3.connect(DB_PATH)
    try:
        dev = load_ctp_instruments(conn)
    finally:
        conn.close()
    local = load_local_tables()
    rows = compare(dev, local)
    lines, summary = render(dev, local, rows)
    txt, js = save(lines, summary)
    print("contract_calibrate: ctp_instruments %d / 可对照 %d / 不一致 %d → %s" % (
        len(dev), len(rows), summary["mismatch"], txt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())