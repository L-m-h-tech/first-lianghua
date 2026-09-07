# -*- coding: utf-8 -*-
r"""第96轮（网页学习探索结合）：jiaoyikecha 交易可查采集器 tools/jiaoyikecha_collector.py。

来源：https://www.jiaoyikecha.com（期货席位持仓/龙虎牛熊/仓单/基本面数据分析站，LayUI SPA + PHP ajax）。
学习笔记见 界面操作收集装置/网页学习探索/01_jiaoyikecha/。

**会话三步（必须，否则数据接口 403）**：
  1. GET  https://www.jiaoyikecha.com/www.jiaoyikecha.com   （拿页面外壳）
  2. POST /ajax/session.php?v=5f6760cc → JSON 里 {cookie:{PHPSESSID:...}}，写入 cookie
  3. POST /ajax/<端点>.php?v=5f6760cc 带 PHPSESSID → 匿名数据接口

本采集器精选**匿名可用（手册实测 code=0）**的高价值端点（深度分析/资金/研报类需登录，跳过）：
  all_varieties.php  89 品种（品种表校正）
  daily_wr.php       76 品种仓单日报（量化侧缺仓单源，跨源对照东财库存）
  hg.php             77 品种支撑压力位
  broker_trend.php   70 席位资金动向
  longhu_list.php    10 品种龙虎榜
  niuxiong_list.php  10 品种牛熊榜

输出：
  reports/jykt_all_varieties.json / daily_wr.json / hg.json / broker_trend.json /
          longhu.json / niuxiong.json    （txt 汇总 reports/jykt_summary.txt）
  cache/jiaoyikecha.db（jykt_wr / jykt_longhu / jykt_broker_trend / jykt_hg 幂等 upsert）
  A1 解析探针记录（jykt_<端点>）

工程护栏（复用第94轮 scrapling 对标成果）：A4 每源会话+cookie 持久化（source="jiaoyikecha"）、
A5 请求级限流退避（http_client 内置）+ 本采集器礼貌限速 0.6~1.2s、A1 解析健康探针。

CLI：python tools/jiaoyikecha_collector.py（会话+采集+报告）| --selftest（零网络合成）
"""
import argparse
import json
import os
import random
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

BASE = "https://www.jiaoyikecha.com"
HOME = BASE + "/www.jiaoyikecha.com"
AJAX = BASE + "/ajax"
VER = "5f6760cc"
SOURCE = "jiaoyikecha"
DB_PATH = os.path.join(_ROOT, "cache", "jiaoyikecha.db")
TXT = os.path.join(_ROOT, "reports", "jykt_summary.txt")
TIMEOUT = 20
MIN_SLEEP, MAX_SLEEP = 0.6, 1.2        # 礼貌限速
_HEADERS = {"User-Agent": config.HEADERS_COMMON["User-Agent"],
            "Referer": HOME,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest"}

# (端点, 说明, 落表, 幂等键) —— 只收手册实测匿名 code=0 的高价值端点
ENDPOINTS = [
    ("all_varieties.php", "全部品种", None, None),
    ("daily_wr.php", "仓单日报", "jykt_wr", ("sym", "trade_date")),
    ("hg.php", "支撑压力位", "jykt_hg", ("sym",)),
    ("broker_trend.php", "席位资金动向", "jykt_broker_trend", ("name",)),
    ("longhu_list.php", "龙虎榜", "jykt_longhu", ("name",)),
    ("niuxiong_list.php", "牛熊榜", "jykt_longhu", ("name",)),
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jykt_wr(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, trade_date TEXT, sym TEXT, name TEXT,
    total_vol REAL, wr_unit REAL, wr_pct REAL,
    raw_json TEXT, created_real REAL,
    UNIQUE(sym, trade_date)
);
CREATE TABLE IF NOT EXISTS jykt_hg(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, sym TEXT, name TEXT, code TEXT,
    current_price REAL, support REAL, resistance REAL,
    raw_json TEXT, created_real REAL,
    UNIQUE(sym)
);
CREATE TABLE IF NOT EXISTS jykt_broker_trend(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, name TEXT, grade TEXT, money REAL,
    raw_json TEXT, created_real REAL,
    UNIQUE(name)
);
CREATE TABLE IF NOT EXISTS jykt_longhu(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, kind TEXT, name TEXT, code TEXT,
    value REAL, raw_json TEXT, created_real REAL,
    UNIQUE(kind, name)
);
"""


def _f(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _probe(ep, ok, n, detail=""):
    try:
        import parser_health
        parser_health.record("jykt_" + ep.split(".")[0], ok, n, detail)
    except Exception:
        pass


def init_session(force=False):
    """会话三步（1+2）：GET 首页 + POST session.php 拿 PHPSESSID 写 cookie。返回是否成功。
    用 http_client 的 source 会话（A4 持久化），重启续用。"""
    try:
        sess = http.get(HOME, source=SOURCE, headers=_HEADERS, timeout=TIMEOUT)
        r = http.post(AJAX + "/session.php?v=" + VER, source=SOURCE,
                      headers=_HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return False
        try:
            sid = r.json()["cookie"]["PHPSESSID"]
        except (ValueError, KeyError, TypeError):
            return False
        import http_client
        http_client.get_session(SOURCE).cookies.set(
            "PHPSESSID", sid, domain="www.jiaoyikecha.com", path="/")
        return bool(sid)
    except Exception:
        return False


def fetch_endpoint(ep):
    """POST /ajax/<ep>?v=... 匿名取数，返回解析后的 dict（data 段）或 None。"""
    try:
        r = http.post(AJAX + "/" + ep + "?v=" + VER, source=SOURCE,
                      headers=_HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            _probe(ep, False, 0, "http_%d" % r.status_code)
            return None
        blob = r.json()
        if blob.get("code") != 0:
            _probe(ep, False, 0, "code_%s" % blob.get("code"))
            return None
        data = blob.get("data")
        n = len(data) if isinstance(data, list) else (len(data or {}) if data else 0)
        _probe(ep, True, n, "ok")
        return data
    except Exception as e:
        _probe(ep, False, 0, "error:%s" % str(e)[:100])
        return None


def _store(db_path, table, rows):
    if not rows:
        return 0
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for r in rows:
            if table == "jykt_wr":
                conn.execute(
                    """INSERT OR REPLACE INTO jykt_wr(ts,trade_date,sym,name,total_vol,wr_unit,wr_pct,raw_json,created_real)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (now, r["trade_date"], r["sym"], r["name"], r["total_vol"], r["wr_unit"],
                     r["wr_pct"], json.dumps(r, ensure_ascii=False), time.time()))
            elif table == "jykt_hg":
                conn.execute(
                    """INSERT OR REPLACE INTO jykt_hg(ts,sym,name,code,current_price,support,resistance,raw_json,created_real)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (now, r["sym"], r["name"], r["code"], r["current_price"], r["support"],
                     r["resistance"], json.dumps(r, ensure_ascii=False), time.time()))
            elif table == "jykt_broker_trend":
                conn.execute(
                    """INSERT OR REPLACE INTO jykt_broker_trend(ts,name,grade,money,raw_json,created_real)
                       VALUES(?,?,?,?,?,?)""",
                    (now, r["name"], r["grade"], r["money"],
                     json.dumps(r, ensure_ascii=False), time.time()))
            elif table == "jykt_longhu":
                conn.execute(
                    """INSERT OR REPLACE INTO jykt_longhu(ts,kind,name,code,value,raw_json,created_real)
                       VALUES(?,?,?,?,?,?,?)""",
                    (now, r["kind"], r["name"], r["code"], r["value"],
                     json.dumps(r, ensure_ascii=False), time.time()))
        conn.commit()
        return len(rows)
    finally:
        conn.close()


# ---------------- 端点解析（结构来自手册 + api_probe 实测，缺字段安全 None） ----------------

def parse_daily_wr(data):
    out = []
    for it in (data or []):
        sym = str(it.get("symbol") or it.get("name") or "").upper()
        name = str(it.get("name") or it.get("variety") or "")
        if not sym:
            continue
        out.append({"sym": sym, "name": name, "trade_date": datetime.now().strftime("%Y-%m-%d"),
                    "total_vol": _f(it.get("total_vol")), "wr_unit": _f(it.get("wr_unit")),
                    "wr_pct": _f(it.get("wr_pct"))})
    return out


def parse_hg(data):
    out = []
    for it in (data or []):
        sym = str(it.get("symbol") or it.get("variety") or "").upper()
        if not sym:
            continue
        out.append({"sym": sym, "name": str(it.get("variety") or ""),
                    "code": str(it.get("code") or ""),
                    "current_price": _f(it.get("current_price")),
                    "support": _f(it.get("support")), "resistance": _f(it.get("resistance"))})
    return out


def parse_broker_trend(data):
    out = []
    for it in (data or []):
        nm = str(it.get("name") or "")
        if not nm:
            continue
        out.append({"name": nm, "grade": str(it.get("grade") or ""),
                    "money": _f(it.get("money"))})
    return out


def parse_longhu(data, kind):
    out = []
    for it in (data or []):
        nm = str(it.get("name") or "")
        if not nm:
            continue
        val_key = "longhu" if kind == "longhu" else "niuxiong"
        out.append({"kind": kind, "name": nm, "code": str(it.get("code") or ""),
                    "value": _f(it.get(val_key))})
    return out


# ---------------- 主流程 ----------------

def run(verbose=True):
    ok_session = init_session()
    results = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "session_ok": ok_session, "endpoints": {}}
    stored_total = 0
    for ep, desc, table, _ in ENDPOINTS:
        time.sleep(random.uniform(MIN_SLEEP, MAX_SLEEP))   # 礼貌限速
        data = fetch_endpoint(ep)
        rows = []
        if ep == "all_varieties.php":
            rows = data or []
        elif ep == "daily_wr.php":
            rows = parse_daily_wr(data)
        elif ep == "hg.php":
            rows = parse_hg(data)
        elif ep == "broker_trend.php":
            rows = parse_broker_trend(data)
        elif ep == "longhu_list.php":
            rows = parse_longhu(data, "longhu")
        elif ep == "niuxiong_list.php":
            rows = parse_longhu(data, "niuxiong")
        # 落 json sidecar
        os.makedirs(os.path.join(_ROOT, "reports"), exist_ok=True)
        fname = os.path.join(_ROOT, "reports", "jykt_" + ep.replace(".php", "") + ".json")
        with open(fname, "w", encoding="utf-8") as f:
            json.dump({"ts": results["ts"], "endpoint": ep, "desc": desc,
                       "n": len(rows), "rows": rows}, f, ensure_ascii=False, indent=1)
        # 落库
        if table and rows:
            stored_total += _store(DB_PATH, table, rows)
        results["endpoints"][ep] = {"desc": desc, "n": len(rows)}
        if verbose:
            print("  %-24s %s: %d 条" % (ep, desc, len(rows)))
    render_txt(results)
    return results


def render_txt(results):
    lines = ["=" * 72,
             " jiaoyikecha 交易可查采集汇总（asof %s · 会话%s）" % (
                 results["ts"], "OK" if results["session_ok"] else "失败"),
             "=" * 72]
    for ep, info in results["endpoints"].items():
        lines.append(" %-24s %-14s %d 条" % (ep, info["desc"], info["n"]))
    lines += ["", "说明：研究侧采集，不进综合分；来源 jiaoyikecha.com（席位持仓/龙虎牛熊/仓单/基本面），",
              "      深度分析/资金/研报类端点需登录，未采集；限速 0.6~1.2s/请求。"]
    os.makedirs(os.path.dirname(TXT), exist_ok=True)
    with open(TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------- selftest（零网络，合成会话与端点响应） ----------------

def selftest():
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))
        if not cond:
            raise AssertionError("FAIL: " + name)

    # 端点解析（合成响应）
    wr = parse_daily_wr([{"name": "螺纹钢", "symbol": "RB", "total_vol": 134546,
                          "wr_unit": 0.1, "wr_pct": 2.5}])
    ck("仓单解析", wr[0]["sym"] == "RB" and wr[0]["total_vol"] == 134546)
    hg = parse_hg([{"variety": "螺纹钢", "symbol": "RB", "code": "rb2701",
                    "current_price": 3156, "support": 3100, "resistance": 3200}])
    ck("支撑压力解析", hg[0]["support"] == 3100 and hg[0]["resistance"] == 3200)
    bt = parse_broker_trend([{"name": "国泰君安", "grade": "A", "money": 1904738480}])
    ck("席位资金解析", bt[0]["grade"] == "A" and bt[0]["money"] == 1904738480)
    lh = parse_longhu([{"name": "沪金", "code": "au2612", "longhu": 82.8}], "longhu")
    ck("龙虎榜解析", lh[0]["value"] == 82.8 and lh[0]["kind"] == "longhu")
    # 落库幂等
    import tempfile
    dbp = os.path.join(tempfile.gettempdir(), "jykt_selftest.db")
    try:
        os.remove(dbp)
    except OSError:
        pass
    ck("仓单落库", _store(dbp, "jykt_wr", wr) == 1)
    ck("仓单幂等覆盖", _store(dbp, "jykt_wr", wr) == 1)
    ck("席位落库", _store(dbp, "jykt_broker_trend", bt) == 1)
    conn = sqlite3.connect(dbp)
    try:
        ck("仓单仍1行", conn.execute("SELECT COUNT(*) FROM jykt_wr").fetchone()[0] == 1)
    finally:
        conn.close()
    try:
        os.remove(dbp)
    except OSError:
        pass
    # 缺字段安全
    ck("坏行跳过", parse_daily_wr([{"foo": 1}]) == [])
    return 0 if all(ok for _, ok in checks) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="jiaoyikecha 交易可查采集（研究侧）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    run(verbose=True)
    print("汇总 → %s" % TXT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())