# -*- coding: utf-8 -*-
r"""G14（第92轮）一档盘口低频快照自采 —— 新浪主连快照，5分钟级、非逐笔。

总纲 G14（旧 P2-5）：对**主力合约**每轮（5 分钟级，非逐笔）采一次买一/卖一价量，
落 `tick_snapshots` 表；用途=①统计真实买卖价差、②校准回测滑点、③给 G1 纸面提供保守成交价。
**验收：upsert 去重、断网续传不丢不重。** 纯标准库、复用既有 futures_data 新浪主源，
不新增任何运行依赖；只采集落库+统计，不改评分/撮合口径、不接 G1 成交（接法见总纲 G14 用途）。

接口字段（商品期货 nf_XXX0 实测，2026-09-06）：
  [0]名称 [1]行情时间HHMMSS [2]开盘 [3]最高 [4]最低 [5]未知0 [6]买一价 [7]卖一价 [8]最新价
  [9]未知0 [10]昨结算 [11]买一量 [12]卖一量 [13]持仓 [14]成交量 [15]交易所 [16]品种名 [17]行情日期
中金所(IF/IH等)字段完全不同且无买卖档位，试点品种只覆盖商品期货（config.SNAPSHOT_VARIETIES）。

设计（对照验收）：
- **节流**：进程内 5 分钟节流（config.SNAPSHOT_INTERVAL），非交易时段零请求（SNAPSHOT_ONLY_TRADING）。
- **去重**：桶键=(sym, 行情日期+时间5分钟桶)，storage.upsert_tick_snapshots 按 (sym,bucket)
  INSERT OR REPLACE——同桶重复采集/断网重试/重启续传都只保留最新一行（不丢不重）。
- **断网续传**：采集失败全吞、下轮自动重试；已成功快照永不删除，行情瞬时性决定"没采到的桶"无法回填（如实）。
- **软降级**：bid/ask 缺失或非法（≤0、ask<bid）整行丢弃不编造；东财兜底 dict 无买卖档位→不落盘。
- **输出**：reports/orderbook_stats.txt/.json（可被看板"研究报告(全部)"页签聚合展示）。

CLI：
  python orderbook_snapshot.py            采集一次（force，绕过节流/时段门控）+ 刷新统计
  python orderbook_snapshot.py --stats-only   只刷新统计报告（读库不请求）
  python orderbook_snapshot.py --selftest     零网络合成断言
"""
import argparse
import json
import logging
import os
import time
from datetime import datetime, timedelta

import config
from utils import LOG

# 复用新浪主源解析（_parse_quote 已增量带出 bid/ask/bid_vol/ask_vol/quote_date/quote_time）
import futures_data

_LAST_COLLECT = [None]          # 进程内最近一次采集时刻（5分钟节流用；重启后由 DB 幂等兜底）

STATS_DAYS = 30                 # 价差统计窗口（自然日，覆盖最近约20个交易日）
STATS_MIN_SAMPLE = 10           # 单品种最少样本数才给统计结论（不足诚实标注"样本不足"）
# 价差合理性上限（基点）：超过即视为瞬时异常/坏行情，统计时排除（采集入库仍保留原始行）。
SPREAD_BP_CAP = 500.0           # 500bp=5%（开盘集合竞价/异常时刻常见放大，正常盘口远小于此）


def _pilots():
    """试点品种 [(sym, code, variety中文名)]，按 config.SNAPSHOT_VARIETIES 顺序，缺配置的跳过。"""
    by_sym = {m["sym"]: (m, name) for name, m in config.VARIETIES.items()}
    out = []
    for sym in getattr(config, "SNAPSHOT_VARIETIES", []):
        item = by_sym.get(sym)
        if not item:
            LOG.warning("G14 试点品种 %s 不在 VARIETIES 中，跳过", sym)
            continue
        m, cn = item
        out.append((sym, m.get("code"), cn))
    return out


def _bucket(quote_date, quote_time, now):
    """5分钟桶键：优先用新浪行情自身日期+时间（HHMMSS→HH:MM），缺/坏时退回本机时刻取5分钟桶。"""
    if quote_date and len(quote_time) >= 4 and quote_time.isdigit():
        return "%s %s:%s" % (quote_date, quote_time[0:2], quote_time[2:4])
    return "%s %02d:%02d" % (now.strftime("%Y-%m-%d"), now.hour, (now.minute // 5) * 5)


def collect_once(db, now=None, force=False, fetcher=None):
    """main 每轮调度的采集入口（G14）。返回 dict：stored=实际落库行数、skipped=跳过原因等。

    force=True 绕过交易时段门控与5分钟节流（CLI/测试/--once 冒烟用）；fetcher 可注入（测试替身），
    默认用 futures_data.fetch_quotes（新浪主源+东财兜底；东财无买卖档位→该品种不落盘）。
    任何异常全吞（绝不进主循环），记录日志后返回 {"stored": 0, "skipped": "error"}。
    """
    now = now or datetime.now()
    try:
        if not getattr(config, "SNAPSHOT_ENABLED", True):
            return {"stored": 0, "skipped": "disabled"}
        if not force and getattr(config, "SNAPSHOT_ONLY_TRADING", True):
            from utils import is_trading_time
            trading, _desc = is_trading_time(now)
            if not trading:
                return {"stored": 0, "skipped": "off_hours"}
        last = _LAST_COLLECT[0]
        if not force and last is not None:
            if (now - last).total_seconds() < float(getattr(config, "SNAPSHOT_INTERVAL", 300)):
                return {"stored": 0, "skipped": "throttle"}
        pilots = _pilots()
        if not pilots:
            return {"stored": 0, "skipped": "no_pilots"}
        codes = [p[1] for p in pilots]
        _LAST_COLLECT[0] = now
        quotes = (fetcher or futures_data.fetch_quotes)(codes)
        rows = []
        sig_seen = set()
        for sym, code, variety in pilots:
            q = quotes.get(code) or {}
            bid = float(q.get("bid") or 0.0)
            ask = float(q.get("ask") or 0.0)
            latest = float(q.get("latest") or 0.0)
            # 软降级：买卖档缺失/非法整行丢弃（东财兜底无 bid/ask 键=0，正落在此处被过滤）
            if bid <= 0 or ask <= 0 or latest <= 0 or ask < bid:
                LOG.debug("G14 快照非法跳过 %s: bid=%s ask=%s latest=%s", sym, bid, ask, latest)
                continue
            qd = str(q.get("quote_date") or "")
            qt = str(q.get("quote_time") or "")
            bkt = _bucket(qd, qt, now)
            # 进程内同桶同价量去重（避免重复 upsert 抖动 id；跨重启由 DB (sym,bucket) 幂等兜底）
            sig = (sym, bkt, bid, ask, float(q.get("bid_vol") or 0.0), float(q.get("ask_vol") or 0.0))
            if sig in sig_seen:
                continue
            sig_seen.add(sig)
            rows.append({
                "sym": sym, "variety": variety, "bucket": bkt,
                "quote_date": qd, "quote_time": qt,
                "collected_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "bid": bid, "ask": ask, "latest": latest,
                "bid_vol": float(q.get("bid_vol") or 0.0),
                "ask_vol": float(q.get("ask_vol") or 0.0),
                "spread": round(ask - bid, 6),
                "spread_bp": round((ask - bid) / latest * 1e4, 4) if latest > 0 else 0.0,
                "prev_settle": float(q.get("prev_settle") or 0.0),
                "oi": float(q.get("open_interest") or 0.0),
                "volume": float(q.get("volume") or 0.0),
                "created_real": now.timestamp(),
            })
        stored = db.upsert_tick_snapshots(rows) if rows else 0
        if stored:
            try:
                render_stats(db)
            except Exception:
                LOG.warning("G14 统计报告刷新失败（不影响采集）:\n", exc_info=True)
        LOG.info("G14 盘口快照: 采到 %d 个品种 / 落库 %d 行 (bucket=%s)",
                 len(rows), stored, rows[0]["bucket"] if rows else "-")
        return {"stored": stored, "n_rows": len(rows), "skipped": ""}
    except Exception:
        LOG.warning("G14 盘口快照采集失败（已吞掉，下轮自动重试）: %s", _exc_text())
        return {"stored": 0, "skipped": "error"}


def _exc_text():
    import traceback
    return traceback.format_exc(limit=3)


# ---------------- 统计报告 ----------------

def render_stats(db, days=None, txt=None, js=None):
    """读 tick_snapshots 最近 days 天，输出 reports/orderbook_stats.txt/.json（价差统计）。"""
    days = days or STATS_DAYS
    txt = txt or config.SNAPSHOT_STATS_TXT
    js = js or config.SNAPSHOT_STATS_JSON
    rows = db.recent_tick_snapshots(days=days, limit=200000)
    os.makedirs(os.path.dirname(txt), exist_ok=True)
    by_sym = {}
    for r in rows:
        by_sym.setdefault(r["sym"], []).append(r)
    per_sym, summary = {}, {"days": days, "n_rows": len(rows), "n_syms": 0}
    for sym in sorted(by_sym):
        rs = by_sym[sym]
        bps = [r["spread_bp"] for r in rs if 0 <= (r["spread_bp"] or 0) <= SPREAD_BP_CAP]
        n_bp = len(bps)
        last = rs[-1]
        entry = {
            "n_samples": len(rs), "n_valid_spread": n_bp,
            "first": rs[0]["collected_at"], "last": last["collected_at"],
            "spread_bp_avg": round(sum(bps) / n_bp, 3) if n_bp else None,
            "spread_bp_median": _median(bps) if n_bp else None,
            "spread_bp_max": round(max(bps), 3) if n_bp else None,
            "spread_abs_avg": round(sum(r["spread"] for r in rs if r["spread"] >= 0) / max(len(rs), 1), 6),
            "latest_bid": last["bid"], "latest_ask": last["ask"], "latest": last["latest"],
            "latest_spread_bp": last["spread_bp"],
            "quote_date": last["quote_date"], "quote_time": last["quote_time"],
        }
        per_sym[sym] = entry
        summary["n_syms"] += 1
    summary["per_sym"] = per_sym
    _render_txt(txt, summary, by_sym)
    with open(js, "w", encoding="utf-8") as f:
        json.dump({"asof": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "generated_by": "orderbook_snapshot",
                   "days": days, "per_sym": per_sym, "n_rows": len(rows)}, f, ensure_ascii=False, indent=1)
    return summary


def _median(vals):
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else round((s[n // 2 - 1] + s[n // 2]) / 2.0, 3)


def _hhmm(qt):
    """行情时间 HHMMSS → HH:MM 展示；缺/坏原样返回。"""
    if len(qt) >= 4 and qt.isdigit():
        return "%s:%s" % (qt[:2], qt[2:4])
    return qt


def _render_txt(txt, summary, by_sym):
    lines = []
    lines.append("=" * 78)
    lines.append("G14 一档盘口快照统计（新浪主连 5分钟级；最近 %d 天；asof %s）"
                 % (summary["days"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    lines.append("样本 %d 行 / %d 个试点品种；价差=卖一-买一；bp=价差/最新价×10000" % (summary["n_rows"], summary["n_syms"]))
    lines.append("-" * 78)
    lines.append("%-4s %-8s %8s %9s %9s %9s %8s %14s" %
                 ("sym", "样本数", "均价差bp", "中位bp", "最大bp", "最新bp", "绝对均价差", "最新行情"))
    for sym, e in summary["per_sym"].items():
        avg = "%.2f" % e["spread_bp_avg"] if e["spread_bp_avg"] is not None else "n<min"
        med = "%.2f" % e["spread_bp_median"] if e["spread_bp_median"] is not None else "-"
        mx = "%.2f" % e["spread_bp_max"] if e["spread_bp_max"] is not None else "-"
        lines.append("%-4s %8d %9s %9s %9s %8s %14s %s %s → bid %s ask %s (最新 %s)"
                     % (sym, e["n_samples"], avg, med, mx,
                        "%.2f" % e["latest_spread_bp"] if e["latest_spread_bp"] is not None else "-",
                        "%.6f" % e["spread_abs_avg"],
                        (e["quote_date"] or "") + (" " + _hhmm(e["quote_time"]) if e["quote_time"] else ""),
                        sym, e["latest_bid"], e["latest_ask"], e["latest"]))
    lines.append("-" * 78)
    under = {sym: e for sym, e in summary["per_sym"].items() if (e["n_valid_spread"] or 0) < STATS_MIN_SAMPLE}
    if under:
        lines.append("样本不足(<%d)不纳入均值结论: %s" % (STATS_MIN_SAMPLE, ",".join(sorted(under))))
    lines.append("口径: 主连快照非逐笔; 采集仅在交易时段(5分钟节流); 同桶去重; 非法档位丢弃不编造;")
    lines.append("     用途=统计真实价差/校准回测滑点/给G1保守成交价(尚未接线); 不构成交易建议。")
    with open(txt, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return txt


# ---------------- selftest / CLI ----------------

def selftest():
    """零网络合成断言：桶推导/软降级/upsert去重/节流/时段门控/统计报告。"""
    import tempfile
    from storage import MonitorDB
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))
        if not cond:
            raise AssertionError("FAIL: " + name)

    now = datetime(2026, 9, 6, 21, 7, 0)
    ck("桶=行情日期+HH:MM", _bucket("2026-09-04", "230000", now) == "2026-09-04 23:00")
    ck("桶缺时间回退本机5分钟桶", _bucket("", "", now) == "2026-09-06 21:05")

    def make_fetcher(qtime):
        def fake_fetcher(codes):
            out = {}
            for c in codes:
                if c == "RB0":
                    out[c] = {"name": "螺纹钢连续", "latest": 3173.0, "bid": 3172.0, "ask": 3173.0,
                              "bid_vol": 218.0, "ask_vol": 7.0, "open_interest": 1502783.0,
                              "volume": 277062.0, "prev_settle": 3160.0,
                              "quote_date": "2026-09-04", "quote_time": qtime}
                elif c == "CU0":
                    out[c] = {"name": "铜连续", "latest": 109290.0, "bid": 0.0, "ask": 109290.0,  # 非法档位
                              "bid_vol": 0.0, "ask_vol": 2.0, "quote_date": "2026-09-05", "quote_time": "010000"}
            return out
        return fake_fetcher

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = MonitorDB(tmp.name)
    try:
        res = collect_once(db, now=now, force=True, fetcher=make_fetcher("230000"))
        ck("force采集落库1行(RB有效,CU非法档位被过滤)", res["stored"] == 1)
        ck("upsert幂等: 同桶重复采集仍1行", collect_once(db, now=now + timedelta(minutes=3),
                                                     force=True, fetcher=make_fetcher("230000"))["stored"] == 1
           and db.conn.execute("SELECT COUNT(*) FROM tick_snapshots").fetchone()[0] == 1)
        ck("不同行情桶新增一行", collect_once(db, now=now + timedelta(minutes=6), force=True,
                                           fetcher=make_fetcher("230500"))["stored"] == 1
           and db.conn.execute("SELECT COUNT(*) FROM tick_snapshots").fetchone()[0] == 2)
        # 节流（临时关时段门控，仅验证节流本身；随后恢复）
        _LAST_COLLECT[0] = None
        _saved_gate = config.SNAPSHOT_ONLY_TRADING
        config.SNAPSHOT_ONLY_TRADING = False
        try:
            collect_once(db, now=now, force=True, fetcher=make_fetcher("230000"))
            ck("节流: 5分钟内重复调用跳过", collect_once(db, now=now + timedelta(seconds=60),
                                                      fetcher=make_fetcher("230000"))["skipped"] == "throttle")
        finally:
            config.SNAPSHOT_ONLY_TRADING = _saved_gate
        # 时段门控（周末时刻=确定非交易时段；force 绕过）
        _LAST_COLLECT[0] = None
        weekend = datetime(2026, 9, 6, 12, 0, 0)
        if not hasattr(config, "SNAPSHOT_ONLY_TRADING") or config.SNAPSHOT_ONLY_TRADING:
            ck("非交易时段默认跳过", collect_once(db, now=weekend, fetcher=make_fetcher("230000"))["skipped"] == "off_hours")
            ck("force绕过时段门控", collect_once(db, now=weekend, force=True,
                                               fetcher=make_fetcher("230000"))["stored"] >= 1)
        # 统计报告
        summ = render_stats(db, days=30, txt=os.path.join(tempfile.gettempdir(), "ob_snap_selftest.txt"),
                            js=os.path.join(tempfile.gettempdir(), "ob_snap_selftest.json"))
        ck("统计含RB且样本>=1", summ["per_sym"].get("RB", {}).get("n_samples", 0) >= 1)
        ck("RB均价差bp为正", (summ["per_sym"]["RB"].get("spread_bp_avg") or 0) > 0)
    finally:
        db.close()
        for p in (tmp.name, os.path.join(tempfile.gettempdir(), "ob_snap_selftest.txt"),
                  os.path.join(tempfile.gettempdir(), "ob_snap_selftest.json")):
            try:
                os.remove(p)
            except OSError:
                pass
    LOG.info("orderbook_snapshot selftest: %d/%d 通过", sum(1 for _, ok in checks if ok), len(checks))
    return 0 if all(ok for _, ok in checks) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="G14 一档盘口低频快照自采")
    ap.add_argument("--stats-only", action="store_true", help="只刷新统计报告（读库不请求）")
    ap.add_argument("--selftest", action="store_true", help="零网络合成断言")
    ap.add_argument("--days", type=int, default=STATS_DAYS, help="统计窗口（自然日）")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    from storage import MonitorDB
    db = MonitorDB()
    try:
        if args.stats_only:
            summ = render_stats(db, days=args.days)
            print("stats-only: %d 行 / %d 品种 → %s" % (summ["n_rows"], summ["n_syms"], config.SNAPSHOT_STATS_TXT))
        else:
            res = collect_once(db, force=True)
            print("collect: %s" % json.dumps(res, ensure_ascii=False))
            if res.get("stored"):
                print("stats → %s" % config.SNAPSHOT_STATS_TXT)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
