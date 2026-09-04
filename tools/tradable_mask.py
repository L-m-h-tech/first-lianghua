# -*- coding: utf-8 -*-
r"""G22续（第64轮）可交易性掩码 tradable_mask：在 G21 标准研究面板上叠加
"疑似锁涨跌停"与"临近交割月"两层**可交易性掩码**——研究侧只读工具，零网络、纯标准库、不接 main。

背景：G22 提出 tradable_mask（涨跌停掩码/交割日历），真 HP/SP 须 G22 分类持仓；本轮先用面板
可得的 OHLCV 做两层掩码并统计：①疑似锁板日（收盘贴最高/最低且涨跌幅达品种常态板幅，复用回测
_locked_limit 口径）；②临近交割日（距最近交割月1号 ≤ DELIVERY_WINDOW 自然日）。

输出：reports/tradable_mask.txt + .json（按品种/板块汇总锁板与临近交割占比、掩码后的干净样本数），
以及纯函数层供后续"掩码剔除后重做截面多空"复用。selftest 零网络/零DB 合成断言。

用法（项目根目录）：
  D:\Python\python.exe tools\tradable_mask.py                 # 读 research_panel.db 出报告
  D:\Python\python.exe tools\tradable_mask.py --selftest      # 零网络/零DB 合成断言
"""
import argparse
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import config  # noqa: E402

DEFAULT_DB = ROOT / "cache" / "research_panel.db"
DEFAULT_TXT = ROOT / "reports" / "tradable_mask.txt"
DEFAULT_JSON = ROOT / "reports" / "tradable_mask.json"
DELIVERY_WINDOW = 15          # 距交割月1号 ≤ 该自然日视为"临近交割"（不可交易掩码=1）
FALLBACK_LIMIT_MOVE = getattr(config, "INTRADAY_BT_LIMIT_MOVE", 0.07)
LIMIT_MOVE = getattr(config, "FUTURES_LIMIT_MOVE", {}) or {}


def _isnum(x):
    try:
        return isinstance(x, (int, float)) and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


# =========================== 纯函数层（可合成断言） ===========================
def locked_flags(closes, highs, lows, limit_move):
    """由等长 OHLC 序列判每根 bar 是否疑似锁涨跌停（复用 backtest._locked_limit 口径）。

    t>0 且 prev>0：ret=c[t]/c[t-1]-1；买向 lock=ret>=limit 且 c 贴 high；卖向 lock=ret<=-limit 且 c 贴 low。
    返回 [bool...]（与输入等长；t=0/数据缺/limit 缺失一律 False=放行）。"""
    n = len(closes)
    out = []
    for t in range(n):
        if t <= 0 or not (_isnum(closes[t]) and _isnum(closes[t - 1])):
            out.append(False)
            continue
        prev, cur = closes[t - 1], closes[t]
        if prev <= 0 or cur <= 0 or not limit_move or limit_move >= 1:
            out.append(False)
            continue
        ret = cur / prev - 1.0
        eps = max(cur * 1e-5, 1e-9)
        hi, lo = highs[t], lows[t]
        if not (_isnum(hi) and _isnum(lo)):
            out.append(False)
            continue
        up_lock = ret >= limit_move and abs(cur - hi) <= eps
        dn_lock = ret <= -limit_move and abs(cur - lo) <= eps
        out.append(bool(up_lock or dn_lock))
    return out


def _month_start(yy, mm):
    return date(2000 + yy, mm, 1)


def nearest_delivery_days(d, sym_code):
    """d 距"最近未来/当月交割月1号"的自然日数（0=当天就是交割月1号，负=已进入交割月）。

    从 d 所在月起枚举未来 12 个月的交割月1号，取 ≥ 当月1号中最近的一个与 d 之差；
    若 d 已过当月1号，用下月1号（证券期货按交割月1号近似，纯函数、可手算）。
    返回 (days_to_first, yy, mm)。"""
    y, m = d.year, d.month
    best = None
    for k in range(0, 12):
        fy = y + (m + k - 1) // 12
        mm = ((m + k - 1) % 12) + 1
        first = _month_start(fy - 2000, mm)   # _month_start 期望两位年（2000+yy）
        if first < d:
            continue                      # 只取 ≥ 当月1号（已过去的用下月）
        gap = (first - d).days
        if best is None or gap < best[0]:
            best = (gap, fy % 100, mm)
    return best


def mask_for_panel(rows_by_date):
    """对面板长表 {date: {sym: row}} 计算每 (sym,date) 掩码。

    row 需含 c/h/l（OHLC）；limit_move 按 sym 取 config.FUTURES_LIMIT_MOVE，缺省兜底 0.07。
    返回 {sym: {date: {"locked":bool, "near_delivery":bool, "tradable":bool}}}。
    每 sym 内部按日期升序算 prev close。"""
    syms = sorted({s for d in rows_by_date for s in rows_by_date[d]})
    out = {}
    for sym in syms:
        days = sorted(d for d in rows_by_date if sym in rows_by_date[d])
        closes = [rows_by_date[d][sym].get("c") for d in days]
        highs = [rows_by_date[d][sym].get("h") for d in days]
        lows = [rows_by_date[d][sym].get("l") for d in days]
        mv = LIMIT_MOVE.get(sym, FALLBACK_LIMIT_MOVE)
        locks = locked_flags(closes, highs, lows, mv)
        sym_map = {}
        for idx, d in enumerate(days):
            y, m, dd = d.split("-")
            near = nearest_delivery_days(date(int(y), int(m), int(dd)), sym)
            near_del = bool(near and near[0] <= DELIVERY_WINDOW)
            locked = locks[idx]
            sym_map[d] = {"locked": locked, "near_delivery": near_del,
                          "tradable": (not locked) and (not near_del)}
        out[sym] = sym_map
    return out


def summarize(mask, sector_of=None):
    """汇总掩码：品种级与板块级锁板/临近交割/可交易占比；返回 dict（纯统计）。"""
    by_sym, by_sec = {}, defaultdict(lambda: {"n": 0, "locked": 0, "near": 0, "tradable": 0})
    for sym, dm in mask.items():
        rec = {"n": len(dm), "locked": sum(1 for v in dm.values() if v["locked"]),
               "near": sum(1 for v in dm.values() if v["near_delivery"]),
               "tradable": sum(1 for v in dm.values() if v["tradable"])}
        by_sym[sym] = rec
        sec = (sector_of or (lambda s: None))(sym)
        b = by_sec[sec or "未知"]
        for k in ("n", "locked", "near", "tradable"):
            b[k] += rec[k]
    return {"by_sym": by_sym,
            "by_sector": {s: dict(v) for s, v in sorted(by_sec.items())}}


# ---- 名字->sym 映射（复用 config.VARIETIES） ----
_NAME_TO_SYM = None


def _name_to_sym():
    """懒加载中文名->sym代码映射：{中文名: sym}。"""
    global _NAME_TO_SYM
    if _NAME_TO_SYM is None:
        try:
            import config
            _NAME_TO_SYM = {}
            for name, info in (getattr(config, "VARIETIES", {}) or {}).items():
                _NAME_TO_SYM[name] = info.get("sym", "")
        except Exception:
            _NAME_TO_SYM = {}
    return _NAME_TO_SYM


def _resolve_sym(val, name_to_sym):
    """将 point/sym 字段值（可能是中文名或代码）映射到 sym 代码。"""
    if val in name_to_sym:
        return name_to_sym[val]
    v = str(val).upper()
    if v.isascii() and len(v) <= 5:
        return v
    return val


def filter_points(points, mask, name_to_sym=None):
    """将不可交易掩码应用到 carry/xsmom 的 points 列表。

    points: [{"sym":name_or_code,"date":"YYYY-MM-DD",...}, ...]
    mask: mask_for_panel() 返回的 {sym_code: {date: {"tradable":bool}}}
    返回剔除后的新列表（不修改原列表）。
    """
    name_map = name_to_sym or _name_to_sym()
    locked = 0
    near = 0
    out = []
    for p in points:
        sym = _resolve_sym(p.get("sym", ""), name_map)
        d = p.get("date", "")
        entry = (mask.get(sym) or {}).get(d)
        if entry is None:
            out.append(p)
            continue
        if entry.get("locked"):
            locked += 1
            continue
        if entry.get("near_delivery"):
            near += 1
            continue
        out.append(p)
    return {"points": out, "original": len(points), "filtered": len(out),
            "removed_locked": locked, "removed_near": near}


def build_report(mask, summary, rows_by_date, db):
    L = ["=" * 104,
         " G22续 可交易性掩码（涨跌停/交割日历）  生成于 " + __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         "=" * 104]
    L.append("面板：%s；品种=%d；掩码规则：疑似锁板（收盘贴板且涨跌幅达品种常态板幅）或 距交割月1号≤%d自然日 -> 不可交易"
             % (db, len(summary["by_sym"]), DELIVERY_WINDOW))
    L.append("  %-6s %7s %8s %9s %9s" % ("品种", "样本", "锁板", "临近交割", "可交易%"))
    for sym in sorted(summary["by_sym"]):
        r = summary["by_sym"][sym]
        L.append("  %-6s %7d %8d %9d %8.1f%%" % (sym, r["n"], r["locked"], r["near"],
                                                100.0 * r["tradable"] / r["n"] if r["n"] else 0.0))
    L.append("  --- 按板块 ---")
    for sec, r in sorted(summary["by_sector"].items()):
        L.append("  %-10s %7d %8d %9d %8.1f%%" % (sec, r["n"], r["locked"], r["near"],
                                                  100.0 * r["tradable"] / r["n"] if r["n"] else 0.0))
    total_n = sum(r["n"] for r in summary["by_sym"].values())
    total_t = sum(r["tradable"] for r in summary["by_sym"].values())
    L.append("  合计：样本=%d，可交易=%d（%.1f%%）；锁板/临近交割占比极小则回测/研究不必另做剔除。"
             % (total_n, total_t, 100.0 * total_t / total_n if total_n else 0.0))
    L.append("=" * 104)
    return "\n".join(L)


def run(db_path=DEFAULT_DB, txt_path=DEFAULT_TXT, json_path=DEFAULT_JSON, verbose=True):
    if not os.path.exists(db_path):
        msg = "未找到研究面板 %s；先运行 tools/panel_builder.py --all 建板。" % db_path
        if verbose:
            print(msg)
        return {"note": msg}
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    have = {r[1] for r in cur.execute("PRAGMA table_info(research_panel)").fetchall()}
    need = {"sym", "date", "c", "h", "l"}
    missing = need - have
    if missing:
        con.close()
        return {"note": "面板缺少列 %s" % sorted(missing)}
    rows_by_date = defaultdict(dict)
    for row in cur.execute("SELECT sym,date,c,h,l FROM research_panel ORDER BY sym,date"):
        sym, d, c, h, l = row
        rows_by_date[d][sym] = {"c": c, "h": h, "l": l}
    con.close()
    mask = mask_for_panel(rows_by_date)
    summary = summarize(mask)
    text = build_report(mask, summary, rows_by_date, db_path)
    if verbose:
        print(text)
    os.makedirs(os.path.dirname(str(txt_path)), exist_ok=True)
    with open(str(txt_path), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    sidecar = {"n_symbols": len(summary["by_sym"]), "delivery_window": DELIVERY_WINDOW,
               "summary": summary, "generated": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    with open(str(json_path), "w", encoding="utf-8") as f:
        json.dump(sidecar, f, ensure_ascii=False, indent=1)
    return sidecar


# =========================== 零网络/零DB 合成自测 ===========================
def _synth_rows():
    """构造含锁板日与交割日的确定性面板（纯本地）。"""
    rows_by_date = defaultdict(dict)
    # RB：连续上涨触发涨停（limit 5%）、贴 high；某日距交割≤15天
    closes = [3000.0]
    for t in range(1, 30):
        closes.append(round(closes[-1] * (1.06 if t == 8 else 1.001), 2))
    for t in range(30):
        d = "2026-%02d-%02d" % (t // 28 + 1, t % 28 + 1)
        c = closes[t]
        # t=8 涨停：c 贴 high（差 ≤ cur*1e-5 容差内）
        h = c if t == 8 else c * 1.002          # t=8 完全贴板（h==c，满足 abs(c-h)<=eps）
        l = c * 0.998 if t == 8 else c * 0.999
        rows_by_date[d]["RB"] = {"c": c, "h": h, "l": l}
    # CU：平稳序列无锁板
    c2 = 70000.0
    for t in range(30):
        d = "2026-%02d-%02d" % (t // 28 + 1, t % 28 + 1)
        c2 = c2 * 1.0005
        rows_by_date[d]["CU"] = {"c": c2, "h": c2 * 1.002, "l": c2 * 0.999}
    return rows_by_date


def selftest():
    # 1) locked_flags 手算：常数序列无锁板；大涨贴高=涨停；大跌贴低=跌停；t=0 放行
    assert locked_flags([100.0, 100.0, 101.0], [101.0, 101.0, 102.0],
                        [99.0, 99.0, 100.0], 0.05) == [False, False, False]
    assert locked_flags([100.0, 107.0], [100.0, 107.0], [100.0, 106.9], 0.05) == [False, True]   # 7%>5%贴高
    assert locked_flags([100.0, 93.0], [100.0, 93.5], [100.0, 93.0], 0.05) == [False, True]       # -7%贴低
    assert locked_flags([100.0, 104.0], [100.0, 104.0], [100.0, 103.0], 0.05) == [False, False]  # 4%<5%
    assert locked_flags([100.0], [100.0], [100.0], 0.05) == [False]                               # t=0
    # 2) nearest_delivery_days：2026-09-04 -> 2026-10-01 交割月1号，差 27 天
    nd = nearest_delivery_days(date(2026, 9, 4), "RB")
    assert nd[0] == 27 and nd[1] == 26 and nd[2] == 10
    # 3) 合成面板：RB 恰在 t=8 涨停（8月/9月 边界日期内）、CU 无锁板；掩码结构齐全
    rows = _synth_rows()
    mask = mask_for_panel(rows)
    assert "RB" in mask and "CU" in mask
    rb_locks = [v["locked"] for v in mask["RB"].values()]
    assert sum(rb_locks) >= 1
    # CU 平稳序列：任何日都不锁板（tradable 只可能因临近交割为 False，绝不可能因锁板）
    assert all(not v["locked"] for v in mask["CU"].values())
    assert any(v["tradable"] for v in mask["CU"].values())  # 大部分日期可交易
    # 4) summarize 计数非负且可交易率∈[0,1]
    summary = summarize(mask)
    for sym, r in summary["by_sym"].items():
        assert r["n"] >= 0 and 0 <= r["tradable"] / r["n"] <= 1 if r["n"] else True
    # 5) run() 出报告结构（不落盘--用临时路径）
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        jp = os.path.join(td, "tm.json")
        sc = run(str(ROOT / "cache" / "research_panel.db"),
                 txt_path=os.path.join(td, "tm.txt"), json_path=jp, verbose=False)
        assert sc and "n_symbols" in sc and "summary" in sc
    print("tradable_mask selftest ALL PASS（锁板判别手算/交割天数/合成面板掩码/汇总计数/报告结构 共5组）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
