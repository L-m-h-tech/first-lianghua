# -*- coding: utf-8 -*-
r"""装置数据研究工具 tools/device_research.py（研究侧，不进综合分）。

数据来源：界面操作收集装置（E:\LHsystem\界面操作收集装置）经 fusion.py 写入
quant monitor.db 的 device_jykc 表（jiaoyikecha 7 类快照，按自然日幂等）。
表结构（装置侧 fusion.py 定义）：
  device_jykc(item_type, code, variety, data_date, value_json, source, updated_real)

本工具把装置数据与量化侧已有基本面数据（fundamentals 表/东财库存、futures_margins
保证金表）做跨源对照，输出研究侧报告（只读 monitor.db，不写主表、不改 analyzer）。

输出：
  reports/device_research.txt / .json    （看板"研究报告(全部)"自动聚合）
  读库、零网络、零第三方依赖；参照 openvlab_map.py 的纪律：诚实、缺数据不编造。

CLI：python tools/device_research.py | --selftest（合成数据零网络）
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config                     # noqa: E402

# config.VARIETIES: 中文品种名 -> {sym, ex, cat, ...}；用于装置数据(中文名)与
# 量化表(sym 大写)之间的标准化对照。
_VARIETY_TO_SYM = {}
for _vn, _vcfg in getattr(config, "VARIETIES", {}).items():
    _VARIETY_TO_SYM[_vn] = str(_vcfg.get("sym", "")).upper()
# 兼容代码直接就是 sym 的（如"RB"）
_SYM_ALIAS = {"沪金": "AU", "沪银": "AG", "沪铝": "AL", "沪铜": "CU", "沪锌": "ZN",
              "沪铅": "PB", "沪镍": "NI", "沪锡": "SN", "原油": "SC",
              "螺纹": "RB", "豆一": "A", "豆二": "B"}


def variety_to_sym(name):
    """中文品种名/别名 -> 标准 sym（大写）；无法识别返回 name 原样大写。"""
    name = str(name or "").strip()
    if not name:
        return ""
    if name in _VARIETY_TO_SYM:
        return _VARIETY_TO_SYM[name]
    if name in _SYM_ALIAS:
        return _SYM_ALIAS[name]
    # 已是 sym 形态（纯字母）
    if name.isalpha() and len(name) <= 3:
        return name.upper()
    return name.upper()


DB_PATH = os.path.join(config.DATA_DIR, "monitor.db")
TXT = os.path.join(_ROOT, "reports", "device_research.txt")
JSON = os.path.join(_ROOT, "reports", "device_research.json")
MAX_RECENT_DAYS = 7               # 报告取最近 N 个自然日


def _conn(path=None):
    """:return: sqlite3.Connection（默认 monitor.db；--selftest 用内存合成库）。"""
    return sqlite3.connect(path or DB_PATH)


# ---------------------------------------------------------------- 读取

def load_device_jykc(conn, item_types=None, days=MAX_RECENT_DAYS):
    """读装置 jykc 快照。:return: {item_type: {code: (variety, value_dict, data_date)}}"""
    q = "SELECT item_type, code, variety, data_date, value_json FROM device_jykc"
    params = []
    if item_types:
        q += " WHERE item_type IN (%s)" % ",".join("?" * len(item_types))
        params = list(item_types)
    out = {}
    for item_type, code, variety, data_date, value_json in conn.execute(q, params):
        try:
            v = json.loads(value_json or "{}")
        except (TypeError, ValueError):
            v = {}
        out.setdefault(item_type, {})[code] = (variety, v, data_date)
    return out


def load_fundamentals(conn, days=MAX_RECENT_DAYS):
    """读量化 fundamentals 表最近库存/基差，做跨源对照。"""
    q = """SELECT sym, trade_date, inventory, basis_rate
           FROM fundamentals WHERE trade_date >= ?
             AND inventory IS NOT NULL
           ORDER BY trade_date DESC"""
    since = (datetime.now().strftime("%Y-%m-%d"))
    # trade_date 是 text yyyy-mm-dd，取最近 N 天里任意一天即可（示意）
    out = {}
    for sym, trade_date, inv, basis in conn.execute(
            "SELECT sym, trade_date, inventory, basis_rate FROM fundamentals "
            "WHERE inventory IS NOT NULL ORDER BY trade_date DESC LIMIT 5000"):
        out.setdefault(str(sym).upper(), {"inv": inv, "basis": basis,
                                          "date": str(trade_date)})
    return out


def load_margins():
    """读 futures_margins.csv 乘数（与 seed_ctp_instruments 同源）。"""
    p = os.path.join(config.DATA_DIR, "futures_margins.csv")
    if not os.path.exists(p):
        return {}
    out = {}
    with open(p, encoding="utf-8-sig") as fh:
        for i, line in enumerate(fh):
            if i == 0:
                continue
            parts = line.strip().split(",")
            if len(parts) < 7:
                continue
            try:
                out[parts[0].upper()] = {"name": parts[1], "mult": float(parts[6])}
            except (TypeError, ValueError):
                pass
    return out


# ---------------------------------------------------------------- 分析与报告

def _warehouse_ranking(jykc):
    """仓库日报(wr)按品种最新 total_vol 排序：仓单绝对量与边际变化 Top。"""
    rows = []
    for code, (variety, v, dt) in jykc.get("wr", {}).items():
        tv = v.get("total_vol")
        if tv is None:
            continue
        rows.append({"code": code, "variety": variety or code, "total_vol": tv,
                     "chge_rate": v.get("chge_rate"), "date": dt})
    rows.sort(key=lambda r: (r.get("total_vol") or 0), reverse=True)
    return rows


def _net_position_top(jykc, margins, n=10):
    """净持仓(net_position)按品种维度聚合净多头，对照乘数。"""
    out = {}
    for code, (variety, v, dt) in jykc.get("net_position", {}).items():
        # variety 是中文品种名 -> 标准 sym
        sym = variety_to_sym(variety)
        if not sym:
            continue
        np_ = v.get("net_position")
        if np_ is None:
            continue
        o = out.setdefault(sym, {"net_pos_tot": 0.0, "date": dt, "variety": variety or sym})
        o["net_pos_tot"] += float(np_)
        o["date"] = dt
    ranked = sorted(out.values(), key=lambda r: r["net_pos_tot"], reverse=True)
    for r in ranked:
        # 乘数查询用标准 sym（margins key 是 sym 大写），品种中文名先标准化
        m = margins.get(variety_to_sym(r["variety"]))
        if not m:
            m = margins.get(r["variety"].upper())
        r["mult"] = m.get("mult") if m else None
    return ranked[:n]


def _cross_vs_fundamentals(jykc, fund):
    """装置仓单(wr) vs 量化 fundamentals.inventory（东财口径）对照。"""
    rows = []
    for code, (variety, v, dt) in jykc.get("wr", {}).items():
        # wr 的 code 常是中文品种名（如"螺纹钢"），variety 也可能同名
        sym = variety_to_sym(variety or code)
        f = fund.get(sym)
        if not f:
            f = fund.get(str(code).upper())
        if not f:
            continue
        device_vol = v.get("total_vol")
        rows.append({"sym": sym, "device_wr": device_vol,
                     "em_inventory": f.get("inv"), "basis": f.get("basis"),
                     "date": dt})
    return rows


def render(jykc, fund, margins):
    """生成研究报告文本。:return: (lines, summary_dict)"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = ["装置数据研究（界面操作收集装置 → 量化跨源对照）",
             "=" * 60,
             "生成: %s | 数据源: monitor.db device_jykc（装置写入）" % now, ""]
    summary = {"generated": now, "counts": {k: len(v) for k, v in jykc.items()}}

    # 1. 仓单日报 Top
    wr = _warehouse_ranking(jykc)
    lines.append("一、装置仓单日报(wr) 总量 Top10")
    lines.append("  %-10s %-10s %10s %8s" % ("品种", "名称", "仓单量", "环比%"))
    for r in wr[:10]:
        lines.append("  %-10s %-10s %10.0f %8s" % (
            r["code"], r["variety"], r["total_vol"],
            "" if r["chge_rate"] is None else "%.1f" % r["chge_rate"]))
    summary["wr_top"] = wr[:10]
    lines.append("")

    # 2. 席位净持仓 Top
    np_top = _net_position_top(jykc, margins)
    lines.append("二、席位净持仓(net_position) 品种级净多头 Top8")
    lines.append("  %-12s %12s %8s" % ("品种", "净持仓合计", "乘数"))
    for r in np_top:
        lines.append("  %-12s %12.0f %8s" % (
            r["variety"], r["net_pos_tot"], "" if r["mult"] is None else "%.0f" % r["mult"]))
    summary["net_top"] = np_top
    lines.append("")

    # 3. 跨源对照：装置仓单 vs 量化东财库存
    x = _cross_vs_fundamentals(jykc, fund)
    lines.append("三、仓单跨源对照（装置 jykc.wr vs 量化 fundamentals.inventory）")
    if x:
        lines.append("  %-10s %12s %12s" % ("品种", "装置仓单", "东财库存"))
        for r in x[:10]:
            lines.append("  %-10s %12.0f %12s" % (
                r["sym"], r["device_wr"],
                "" if r["em_inventory"] is None else "%.0f" % r["em_inventory"]))
        summary["cross"] = x[:10]
    else:
        lines.append("  （quant fundamentals 表暂无装置品种对应库存，缺数据诚实标注）")
    lines.append("")
    lines.append("说明：以上数据来自界面操作收集装置（jiaoyikecha 采集），只读对照、研究侧、不进综合分。")
    summary["ok"] = True
    return "\n".join(lines) + "\n", summary


def save(lines, summary):
    os.makedirs(os.path.dirname(TXT), exist_ok=True)
    with open(TXT, "w", encoding="utf-8-sig") as fh:
        fh.write(lines)
    with open(JSON, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    return TXT, JSON


def selftest(conn_factory=None):
    """合成数据零网络：验证解析/排序/对照不抛异常。"""
    c = conn_factory() if conn_factory else sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE device_jykc(item_type TEXT, code TEXT, variety TEXT,
                 data_date TEXT, value_json TEXT, source TEXT, updated_real REAL)""")
    c.executemany("INSERT INTO device_jykc VALUES(?,?,?,?,?,?,?)", [
        ("wr", "RB", "螺纹钢", "2026-09-08", '{"total_vol": 134546.0, "chge_rate": 1.2}', "jj", 1),
        ("wr", "CU", "铜", "2026-09-08", '{"total_vol": 92000.0}', "jj", 1),
        ("net_position", "西南期货", "PTA", "2026-09-08", '{"net_position": 600.0}', "jj", 1),
        ("net_position", "国泰君安", "RB", "2026-09-08", '{"net_position": 400.0}', "jj", 1),
        ("hg", "LU2610", "低硫油", "2026-09-08", '{"min15": "偏多，支撑：5023-5029"}', "jj", 1),
    ])
    jykc = load_device_jykc(c)
    assert set(jykc.keys()) == {"wr", "net_position", "hg"}, jykc.keys()
    wr = _warehouse_ranking(jykc)
    assert wr and wr[0]["code"] == "RB", wr
    np_top = _net_position_top(jykc, {"PTA": {"mult": 5.0}})
    assert np_top and np_top[0]["variety"] == "PTA", np_top
    lines, summary = render(jykc, {}, {"PTA": {"mult": 5.0}})
    assert "仓单日报" in lines and "跨源对照" in lines
    return 0 if (jykc and wr and np_top and summary.get("ok")) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="装置数据研究（jykc 跨源对照，研究侧）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    conn = _conn()
    try:
        jykc = load_device_jykc(conn)
        fund = load_fundamentals(conn)
    finally:
        conn.close()
    margins = load_margins()
    lines, summary = render(jykc, fund, margins)
    txt, js = save(lines, summary)
    print("device_research: 装置 jykc %d 类 / 仓单 %d / 净持仓 %d → %s" % (
        len(jykc), len(jykc.get("wr", {})), len(jykc.get("net_position", {})), txt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())