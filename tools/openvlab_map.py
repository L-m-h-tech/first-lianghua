# -*- coding: utf-8 -*-
r"""第96轮（网页学习探索结合）：OpenVLab 全市场波动率地图采集器 tools/openvlab_map.py。

来源：https://www.openvlab.cn/market（期权波动率专业站，FastAPI 匿名 REST）。
学习笔记见 界面操作收集装置/网页学习探索/02_openvlab_market/。

采集 `GET /api/ctamap-all?add_overseas=true`——83 品种（含海外）期权波动率地图，字段：
  sector/sector_alias/product(EG_O)/product_alias(乙二醇)/prodUnd(EG)/is_overseas/exchange/
  has_night_trading/exp(202610)/expiry_date/price/frontfwd_mom/atmv_current(ATM隐波%)/
  atmv_percentile/atmv_1dchg/skew_current/skew_percentile/rv22/carry

输出：
  reports/openvlab_map.txt / .json        （看板"研究报告(全部)"页签自动聚合）
  cache/openvlab_map.db 表 option_vol_map（按 sym 当日幂等覆盖）
  A1 解析探针记录（openvlab_ctamap）
与本地 iv_surface 交叉校验：读 reports/iv_surface.json 的 T链反推 atm_iv，与官方 atmv_current
对比偏差>2vol 标注（跨源一致性，补充 B5 官方IV 的另一个独立对照）。

CLI：python tools/openvlab_map.py（采集+报告）| --selftest（零网络合成）
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
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import config                     # noqa: E402
from http_client import http      # noqa: E402

API = "https://www.openvlab.cn/api/ctamap-all?add_overseas=true"
DB_PATH = os.path.join(_ROOT, "cache", "openvlab_map.db")
TXT = os.path.join(_ROOT, "reports", "openvlab_map.txt")
JSON = os.path.join(_ROOT, "reports", "openvlab_map.json")
IV_SURFACE_JSON = os.path.join(_ROOT, "reports", "iv_surface.json")
VOL_DIFF_THRESH = 2.0            # 与本地 iv_surface 偏差(vol)超过该值标注
TIMEOUT = 15
_HEADERS = {"User-Agent": config.HEADERS_COMMON["User-Agent"],
            "Referer": "https://www.openvlab.cn/market",
            "Accept": "application/json, text/plain, */*"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS option_vol_map(
    sym TEXT PRIMARY KEY,
    ts TEXT, variety TEXT, sector TEXT, exchange TEXT,
    exp TEXT, expiry_date TEXT, price REAL,
    atmv_current REAL, atmv_percentile REAL, atmv_1dchg REAL,
    skew_current REAL, skew_percentile REAL, rv22 REAL,
    carry REAL, frontfwd_mom REAL, has_night_trading INTEGER,
    raw_json TEXT, created_real REAL
);
"""


def _f(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def fetch_map(fetcher=None):
    """拉取 ctamap-all，返回 [{sym, variety, sector, ...}] 列表；失败返回 []（A1 探针记录）。"""
    try:
        r = (fetcher or http.get)(API, headers=_HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            _probe(False, 0, "http_%d" % r.status_code)
            return []
        blob = r.json()
        rows = (blob or {}).get("result") or []
        out = []
        for it in rows:
            prod = it.get("product") or ""
            sym = (it.get("prodUnd") or prod.split("_")[0] or "").upper()
            if not sym:
                continue
            out.append({
                "sym": sym, "variety": it.get("product_alias") or sym,
                "sector": it.get("sector") or it.get("sector_alias") or "",
                "exchange": it.get("exchange") or "",
                "exp": it.get("exp") or "", "expiry_date": it.get("expiry_date") or "",
                "price": _f(it.get("price")),
                "atmv_current": _f(it.get("atmv_current")),
                "atmv_percentile": _f(it.get("atmv_percentile")),
                "atmv_1dchg": _f(it.get("atmv_1dchg")),
                "skew_current": _f(it.get("skew_current")),
                "skew_percentile": _f(it.get("skew_percentile")),
                "rv22": _f(it.get("rv22")),
                "carry": _f(it.get("carry")),
                "frontfwd_mom": _f(it.get("frontfwd_mom")),
                "has_night_trading": 1 if it.get("has_night_trading") else 0,
            })
        _probe(True, len(out), "ok")
        return out
    except Exception as e:
        _probe(False, 0, "error:%s" % str(e)[:120])
        return []


def _probe(ok, n, detail=""):
    try:
        import parser_health
        parser_health.record("openvlab_ctamap", ok, n, detail)
    except Exception:
        pass


def store(db_path=None, rows=None):
    """落 cache/openvlab_map.db（option_vol_map，按 sym 当日幂等覆盖）。"""
    rows = rows or []
    if not rows:
        return 0
    db_path = db_path or DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_SCHEMA)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for r in rows:
            conn.execute(
                """INSERT OR REPLACE INTO option_vol_map(
                       sym,ts,variety,sector,exchange,exp,expiry_date,price,
                       atmv_current,atmv_percentile,atmv_1dchg,skew_current,skew_percentile,
                       rv22,carry,frontfwd_mom,has_night_trading,raw_json,created_real)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["sym"], now, r["variety"], r["sector"], r["exchange"], r["exp"],
                 r["expiry_date"], r["price"], r["atmv_current"], r["atmv_percentile"],
                 r["atmv_1dchg"], r["skew_current"], r["skew_percentile"], r["rv22"],
                 r["carry"], r["frontfwd_mom"], r["has_night_trading"],
                 json.dumps(r, ensure_ascii=False), time.time()))
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def cross_check(rows):
    """与本地 iv_surface.json（T链反推 atm_iv）交叉校验：偏差>VOL_DIFF_THRESH 标注。"""
    local = {}
    try:
        with open(IV_SURFACE_JSON, encoding="utf-8") as f:
            blob = json.load(f)
        for it in blob.get("ivs", []):
            sym = str(it.get("sym") or "").upper()
            iv = it.get("atm_iv")
            if sym and iv:
                local[sym] = float(iv)
    except (OSError, ValueError, KeyError):
        pass
    checks = []
    for r in rows:
        if r["sym"] not in local or r["atmv_current"] is None:
            continue
        diff = abs(r["atmv_current"] - local[r["sym"]])
        checks.append({"sym": r["sym"], "atmv_openvlab": r["atmv_current"],
                       "atmv_local": local[r["sym"]], "diff": round(diff, 2),
                       "warn": diff > VOL_DIFF_THRESH})
    return checks


def render(rows, checks, txt_path=None, js_path=None):
    txt_path = txt_path or TXT
    js_path = js_path or JSON
    os.makedirs(os.path.dirname(txt_path), exist_ok=True)
    lines = ["=" * 78,
             " OpenVLab 全市场期权波动率地图（ctamap-all %d 品种 · asof %s）"
             % (len(rows), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
             "=" * 78,
             "%-6s %-10s %-5s %-8s %9s %8s %8s %9s %8s %7s %7s" %
             ("sym", "品种", "板块", "交易所", "最新价", "ATM隐波", "隐波百分位", "1日变化",
              "偏度", "偏度分位", "RV22")]
    rows_sorted = sorted(rows, key=lambda r: (r["atmv_percentile"] or -1) if r["atmv_percentile"] is not None else -1,
                         reverse=True)
    for r in rows_sorted[:83]:
        lines.append("%-6s %-10s %-5s %-8s %9s %8s %8s %9s %8s %7s %7s" % (
            r["sym"], (r["variety"] or "")[:8], (r["sector"] or "")[:4],
            (r["exchange"] or "")[:7],
            "%.2f" % r["price"] if r["price"] is not None else "-",
            "%.2f" % r["atmv_current"] if r["atmv_current"] is not None else "-",
            "%.1f" % r["atmv_percentile"] if r["atmv_percentile"] is not None else "-",
            "%+.2f" % r["atmv_1dchg"] if r["atmv_1dchg"] is not None else "-",
            "%.2f" % r["skew_current"] if r["skew_current"] is not None else "-",
            "%.1f" % r["skew_percentile"] if r["skew_percentile"] is not None else "-",
            "%.2f" % r["rv22"] if r["rv22"] is not None else "-"))
    lines.append("-" * 78)
    lines.append("【与本地 iv_surface（T链反推）交叉校验 · 偏差>%.0fvol 标注 ⚠️】" % VOL_DIFF_THRESH)
    if checks:
        for c in checks:
            lines.append(" %s openvlab=%.2f 本地=%.2f 偏差=%.2f %s" % (
                c["sym"], c["atmv_openvlab"], c["atmv_local"], c["diff"],
                "⚠️" if c["warn"] else "✅"))
    else:
        lines.append(" （无对照数据：本地 iv_surface.json 缺失或为空，属正常冷启动）")
    lines += ["", "口径：匿名 GET ctamap-all?add_overseas=true（83品种含海外）；隐波为ATM平值隐含波动率(%)；",
              "      数据仅做研究侧采集，不进综合分；来源 openvlab.cn（期权专业站，与 Legend 桌面端同源）。"]
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(js_path, "w", encoding="utf-8") as f:
        json.dump({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "n": len(rows), "rows": rows, "checks": checks},
                  f, ensure_ascii=False, indent=1)
    return txt_path


# ---------------- selftest（零网络） ----------------

def selftest():
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))
        if not cond:
            raise AssertionError("FAIL: " + name)

    sample = {"code": 0, "result": [
        {"product": "EG_O", "product_alias": "乙二醇", "prodUnd": "EG", "sector": "EN",
         "exchange": "DCE", "has_night_trading": True, "exp": 202610, "expiry_date": "2026-10-14",
         "price": 5740.0, "frontfwd_mom": 0.3669, "atmv_current": 46.19,
         "atmv_percentile": 88.02, "atmv_1dchg": -5.36, "skew_current": 2.1,
         "skew_percentile": 60.0, "rv22": 46.16, "carry": 0.05}]}

    def fake_fetcher(url, **kw):
        class R:
            status_code = 200

            def json(self):
                return sample
        return R()

    rows = fetch_map(fetcher=fake_fetcher)
    ck("合成解析1品种", len(rows) == 1 and rows[0]["sym"] == "EG")
    ck("字段提取", rows[0]["atmv_current"] == 46.19 and rows[0]["atmv_percentile"] == 88.02)
    # 落库幂等
    import tempfile
    dbp = os.path.join(tempfile.gettempdir(), "ovl_map_selftest.db")
    try:
        os.remove(dbp)
    except OSError:
        pass
    ck("落库1行", store(dbp, rows) == 1)
    ck("幂等覆盖", store(dbp, rows) == 1)
    conn = sqlite3.connect(dbp)
    try:
        n = conn.execute("SELECT COUNT(*) FROM option_vol_map").fetchone()[0]
        ck("覆盖后仍1行", n == 1)
    finally:
        conn.close()
    try:
        os.remove(dbp)
    except OSError:
        pass
    # 坏响应 → 空
    def bad(url, **kw):
        class R:
            status_code = 403

            def json(self):
                raise ValueError
        return R()
    ck("非200降级空", fetch_map(fetcher=bad) == [])
    return 0 if all(ok for _, ok in checks) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="OpenVLab 全市场波动率地图采集（研究侧）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    rows = fetch_map()
    n = store(rows=rows)
    checks = cross_check(rows)
    render(rows, checks)
    print("openvlab_map: %d 品种 / 落库 %d / 交叉校验 %d 条 → %s" % (len(rows), n, len(checks), TXT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())