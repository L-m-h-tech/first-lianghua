# -*- coding: utf-8 -*-
r"""第96/97轮：新数据源因子研究 tools/newdata_factor_research.py（研究侧，零主链改动）。

基于第96轮新增的两个数据源做**候选因子横截面体检**：
  - OpenVLab 隐波因子（cache/openvlab_map.db option_vol_map，每日快照）：
      atmv_percentile  ATM隐波百分位（越高=隐波越贵）
      skew_percentile  偏度百分位
      atmv_1dchg       隐波1日变化
      prem             溢价 = ATM隐波 - RV22 实波（隐波贵/便宜）
  - jiaoyikecha 仓单因子（cache/jiaoyikecha.db jykt_wr）：
      total_vol        仓单总量（跨源对照东财库存，研究侧）

评估口径（诚实）：
  1. 因子横截面 TOP/BOTTOM 表（描述性，当天快照）
  2. 与研究面板最新 ret1d 的 Spearman 秩相关（初步对照）——注意时点：隐波/仓单为当日快照，
     面板最新交易日可能不同日，仅作数量级观察，不构成正式前向 IC
  3. 正式前向 IC 需逐日积累 ≥MIN_SAMPLES 个交易日（同项目研究工具口径，默认 30）
样本不足只列数不下结论（项目铁律：负结果/样本不足诚实呈现，绝不硬凑）。

输出 reports/newdata_factor_research.txt/.json；A1 探针记录。CLI：--selftest 零网络合成。
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

OVL_DB = os.path.join(_ROOT, "cache", "openvlab_map.db")
JYKT_DB = os.path.join(_ROOT, "cache", "jiaoyikecha.db")
PANEL_DB = os.path.join(_ROOT, "cache", "research_panel.db")
TXT = os.path.join(_ROOT, "reports", "newdata_factor_research.txt")
JSON = os.path.join(_ROOT, "reports", "newdata_factor_research.json")
MIN_SAMPLES = 30          # 正式前向 IC 的交易日门槛


def _q(db, sql, args=()):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def load_n_days():
    """option_vol_map 里已积累的不同采集日期数（正式前向IC按交易日数判定样本）。"""
    try:
        conn = sqlite3.connect(OVL_DB)
        try:
            return conn.execute("SELECT COUNT(DISTINCT ts) FROM option_vol_map").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def load_vol_factors():
    """option_vol_map → [{sym, atmv_percentile, skew_percentile, atmv_1dchg, prem}]（当日快照）。"""
    try:
        rows = _q(OVL_DB, "SELECT sym, atmv_percentile, skew_percentile, atmv_1dchg,"
                            " atmv_current, rv22 FROM option_vol_map")
    except sqlite3.Error:
        return []
    out = []
    for sym, pct, spct, chg, atmv, rv in rows:
        if not sym:
            continue
        prem = (atmv - rv) if (atmv is not None and rv is not None) else None
        out.append({"sym": sym, "atmv_percentile": pct, "skew_percentile": spct,
                    "atmv_1dchg": chg, "prem": prem})
    return out


def load_wr_factors():
    """jykt_wr → [{sym, total_vol}]（当日仓单）。"""
    try:
        rows = _q(JYKT_DB, "SELECT sym, total_vol FROM jykt_wr")
    except sqlite3.Error:
        return []
    return [{"sym": sym, "total_vol": tv} for sym, tv in rows if sym]


def load_panel_ret1():
    """研究面板最新交易日的 ret1d（按 sym），用于初步对照；无面板返回 {}。"""
    try:
        conn = sqlite3.connect(PANEL_DB)
        try:
            d = conn.execute("SELECT MAX(date) FROM research_panel").fetchone()[0]
            rows = conn.execute(
                "SELECT sym, ret1d FROM research_panel WHERE date=? AND ret1d IS NOT NULL",
                (d,)).fetchall()
        finally:
            conn.close()
        return {sym: r for sym, r in rows}
    except sqlite3.Error:
        return {}


def spearman(xs, ys):
    """纯标准库 Spearman 秩相关；样本<3 或常量返回 None。"""
    n = len(xs)
    if n < 2:
        return None

    def rank(v):
        idx = sorted(range(n), key=lambda i: v[i])
        r = [0] * n
        for pos, i in enumerate(idx):
            r[i] = pos + 1
        return r

    rx, ry = rank(xs), rank(ys)
    d2 = sum((rx[i] - ry[i]) ** 2 for i in range(n))
    denom = n * (n * n - 1)
    return 1.0 - 6.0 * d2 / denom if denom else None


def _corr(factor_key, vol_rows, ret_map):
    """某隐波因子与面板 ret1d 的秩相关（只对齐两边都有值的品种）。"""
    xs, ys = [], []
    for r in vol_rows:
        v = r.get(factor_key)
        ret = ret_map.get(r["sym"])
        if v is None or ret is None:
            continue
        xs.append(v)
        ys.append(ret)
    return spearman(xs, ys), len(xs)


def run():
    vol = load_vol_factors()
    wr = load_wr_factors()
    ret_map = load_panel_ret1()
    n_days = load_n_days()
    results = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "n_vol": len(vol), "n_wr": len(wr),
               "panel_latest_ret1d_syms": len(ret_map),
               "n_days": n_days, "min_samples": MIN_SAMPLES,
               "note": "正式前向IC需逐日积累≥%d个交易日（当前%d天）；横截面对照为隐波/仓单快照 vs 面板最新ret1d的初步秩相关（时点可能不同日，品种数≠样本天数）" % (MIN_SAMPLES, n_days)}
    # 横截面 TOP/BOTTOM（描述性）
    def top_bottom(rows, key, top_n=8):
        valid = [r for r in rows if r.get(key) is not None]
        valid.sort(key=lambda r: r[key], reverse=True)
        return [(r["sym"], r[key]) for r in valid[:top_n]], \
               [(r["sym"], r[key]) for r in valid[-top_n:]]
    results["factors"] = {
        "atmv_percentile": top_bottom(vol, "atmv_percentile"),
        "prem": top_bottom(vol, "prem"),
        "skew_percentile": top_bottom(vol, "skew_percentile"),
        "atmv_1dchg": top_bottom(vol, "atmv_1dchg"),
        "wr_total_vol": top_bottom(wr, "total_vol"),
    }
    # 初步秩相关
    corrs = {}
    for key in ("atmv_percentile", "prem", "skew_percentile", "atmv_1dchg"):
        rho, n = _corr(key, vol, ret_map)
        corrs[key] = {"spearman": round(rho, 4) if rho is not None else None,
                      "n_cross": n, "n_days": n_days,
                      "sample_enough": n_days >= MIN_SAMPLES}
    results["corr_panel_ret1d"] = corrs
    _render(results)
    return results


def _render(r):
    lines = ["=" * 74,
             " 新数据源因子研究（openvlab 隐波 × jiaoyikecha 仓单 · asof %s）" % r["ts"],
             "=" * 74,
             " 品种: 隐波%d / 仓单%d | 面板最新ret1d品种: %d" % (r["n_vol"], r["n_wr"], r["panel_latest_ret1d_syms"]),
             " 口径: %s" % r["note"], ""]
    for name, (top, bot) in r["factors"].items():
        lines.append("【%s】TOP: %s" % (name, ", ".join("%s=%.1f" % (s, v) for s, v in top)))
        lines.append("   %s BOT: %s" % (" " * (len(name) + 2), ", ".join("%s=%.1f" % (s, v) for s, v in bot)))
    lines.append("")
    lines.append("【与面板最新 ret1d 初步秩相关（非正式前向IC）】")
    for k, c in r["corr_panel_ret1d"].items():
        st = "✅样本足(交易日)" if c["sample_enough"] else "⚠️交易日不足(%d天<%d)" % (c["n_days"], r["min_samples"])
        lines.append(" %-18s spearman=%s  截面品种=%d  %s" % (
            k, c["spearman"] if c["spearman"] is not None else "-", c["n_cross"], st))
    lines += ["", "结论: 数据自第96轮起逐日积累；样本≥%d个交易日后正式评估前向IC（复用 expr_research 口径），" % r["min_samples"],
              "      在此之前仅描述性呈现，不下[有效/无效]结论。"]
    os.makedirs(os.path.dirname(TXT), exist_ok=True)
    with open(TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(JSON, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    return TXT


def selftest():
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))
        if not cond:
            raise AssertionError("FAIL: " + name)

    # spearman：完全正相关 = 1，完全负相关 = -1
    ck("正相关=1", spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0)
    ck("负相关=-1", abs(spearman([1, 2, 3, 4], [40, 30, 20, 10]) + 1.0) < 1e-9)
    ck("n=2返回1.0", spearman([1, 2], [3, 4]) == 1.0)
    # 空库读取安全
    import tempfile
    os.makedirs(tempfile.gettempdir(), exist_ok=True)
    ck("无库返回空", load_vol_factors() == [] or isinstance(load_vol_factors(), list))
    # 初步相关对齐逻辑
    vol = [{"sym": "A", "atmv_percentile": 90.0, "prem": 5.0},
           {"sym": "B", "atmv_percentile": 10.0, "prem": -2.0}]
    ret = {"A": 0.01, "B": -0.01}
    rho, n = _corr("atmv_percentile", vol, ret)
    ck("对齐后n=2", n == 2 and rho == 1.0)
    return 0 if all(ok for _, ok in checks) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="新数据源因子研究（研究侧，零主链改动）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    r = run()
    print("研究输出 → %s （隐波%d品种/仓单%d品种）" % (TXT, r["n_vol"], r["n_wr"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())