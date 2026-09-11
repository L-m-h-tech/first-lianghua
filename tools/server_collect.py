#!/usr/bin/env python3
"""
多服务器并行采集调度器（第120轮·本机端，动态服务器数量版）
从云服务器并行拉取分钟K/日线数据，聚合写入本地 DB。

用法（首次回填）：
  python tools/server_collect.py --mode=backfill

用法（常驻增量）：
  python tools/server_collect.py --mode=incr

核心特性（v2：动态适应服务器数量）：
  - 支持最多 MAX_SERVERS 台服务器（默认 11），实际几台在线就用几台分摊；
  - 启动时自动探测在线服务器（/health），掉线的跳过、不阻塞；
  - 任务 round-robin 分摊到【在线服务器】上，服务器少→单台任务多但继续跑；
  - 单任务失败自动换下一台在线服务器重试（故障容错，不拖慢整体）；
  - 中途某台挂掉：该台未完成任务由其他台兜底重试。

配置：SERVERS 填满候选列表（可 11 台或更多），在线探测自动决定用几台。
"""
import json
import sys
import os
import time
import argparse
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ---- 服务器候选列表（填满可用的，最多 MAX_SERVERS 台） ----
# 格式：["http://IP:PORT", ...]；在线探测自动剔除不可达的。
SERVERS = [
    "http://8.156.69.136:9001",
    "http://8.156.73.52:9001",
    "http://8.156.69.2:9001",
    "http://8.156.73.27:9001",
    "http://8.156.72.196:9001",
    "http://47.109.195.37:9001",
    "http://8.156.69.191:9001",
    "http://8.156.78.133:9001",
    "http://8.156.66.174:9001",
    "http://8.137.94.172:9001",
    "http://47.108.206.1:9001",
]
MAX_SERVERS = 11          # 最多同时使用的服务器数（候选多于它时取前 MAX 台在线）
HEALTH_TIMEOUT = 5        # 健康探测超时（秒）
REQUEST_TIMEOUT = 30      # 单请求超时（秒）

# ---- 品种列表（与 config.VARIETIES 对齐，独立运行不依赖主项目 import） ----
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
try:
    from config import VARIETIES as _VAR
    SYMS = [(v["code"], v["sym"], v["ex"]) for v in _VAR.values()]
except Exception:
    SYMS = []  # 实际部署时填入或确保能 import config

MINUTE_PERIODS = (1, 5, 30)        # 增量周期（与 config.MINUTE_PERIODS 对齐）
BACKFILL_PERIODS = (60, 30, 15, 5, 1)  # 回填周期
INCR_LMT = {1: 12, 5: 8, 30: 6}       # 增量根数
BACKFILL_LMT = {1: 1023, 5: 1023, 15: 1023, 30: 1023, 60: 1023}

# ---- 工具 ----
LOG = lambda *a: print(f"[{datetime.now():%H:%M:%S}]", *a, flush=True)

def _get(url, timeout=REQUEST_TIMEOUT):
    """请求单台服务器，返回 JSON dict 或 None。"""
    try:
        req = Request(url, headers={"User-Agent": "ServerCollect/1.0"})
        resp = urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None

# ---- 在线探测 ----
def probe_servers(servers):
    """并发探测所有候选服务器 /health，返回在线列表。掉线的跳过，不阻塞。"""
    online = []
    if not servers:
        return online
    def _probe(srv):
        data = _get(f"{srv}/health", timeout=HEALTH_TIMEOUT)
        return srv if data and data.get("status") == "ok" else None
    with ThreadPoolExecutor(max_workers=min(len(servers), 16)) as pool:
        for srv, ok in zip(servers, pool.map(_probe, servers)):
            if ok:
                online.append(ok)
    return online

# ---- 调度核心 ----
_online_lock = threading.Lock()
_online = []          # 当前在线服务器（动态更新）
_next_idx = 0         # round-robin 游标

def _pick_server():
    """从在线列表中轮询取一台；列表空返回 None。"""
    global _next_idx
    with _online_lock:
        if not _online:
            return None
        srv = _online[_next_idx % len(_online)]
        _next_idx += 1
        return srv

def _mark_dead(srv):
    """把故障服务器从在线列表剔除（后续任务自动分流到其他台）。"""
    with _online_lock:
        if srv in _online:
            _online.remove(srv)
            LOG(f"服务器 {srv} 故障，剔除在线列表（剩余 {len(_online)} 台）")

def collect(tasks, timeout=REQUEST_TIMEOUT, workers=None):
    """
    并行采集，动态适应服务器数量。
    tasks = [(code, sym, ex, period, lmt), ...]
    每任务先取一台在线服务器；失败自动换下一台重试（最多轮询在线列表一遍）。
    返回 (all_bars, results)；results 含 success/fail/elapsed/online。
    """
    global _online
    _online = probe_servers(SERVERS[:MAX_SERVERS])
    if not _online:
        LOG("无在线服务器，请检查 SERVERS 列表与安全组/服务状态"); return [], {}
    LOG(f"在线服务器 {len(_online)} 台（候选 {len(SERVERS)}，上限 {MAX_SERVERS}），任务 {len(tasks)} 个")

    results = {"success": 0, "fail": 0, "bars_total": 0, "online": len(_online)}
    t0 = time.time()
    lock = threading.Lock()

    def _fetch_one(task):
        code, sym, ex, period, lmt = task
        url_tpl = "/minute?symbol={c}&period={p}&lmt={l}"
        # 轮询在线列表：本任务失败换台重试
        tried = set()
        while len(tried) < len(_online) + 1:   # +1 容错（列表可能刚剔除）
            srv = _pick_server()
            if srv is None or srv in tried:
                break
            tried.add(srv)
            data = _get(f"{srv}{url_tpl.format(c=code, p=period, l=lmt)}",
                        timeout=timeout)
            if data and data.get("bars"):
                with lock:
                    results["success"] += 1
                    results["bars_total"] += data.get("count", 0)
                return data["bars"]
            elif data and "456" in str(data.get("error", "")):
                _mark_dead(srv)   # 该台被封(456)，剔除
        with lock:
            results["fail"] += 1
        return []

    all_bars = []
    workers = workers or len(_online)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_fetch_one, t) for t in tasks]
        for f in as_completed(futures):
            all_bars.extend(f.result())

    results["elapsed"] = time.time() - t0
    return all_bars, results

# ---- DB 写入（可选，兼容 storage.py 接口） ----
def _write_to_db(bars):
    """尝试写入 monitor.db（如存在）；否则打印到 stdout 供手动导入。"""
    if not bars:
        LOG("无数据可写"); return 0
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from storage import DB
        db = DB(os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "monitor.db"))
        n = db.insert_minute_bars(bars)
        LOG(f"写入 DB: {n} 行新增")
        return n
    except Exception:
        out_path = os.path.join(os.path.dirname(__file__), "collected_bars.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(bars[:100], f, ensure_ascii=False, indent=2)
        LOG(f"DB 不可用，写入样本到 {out_path}（{len(bars)} 条原始数据）")
        return 0

# ---- Main ----
def main():
    global MAX_SERVERS
    parser = argparse.ArgumentParser(description="多服务器并行采集调度器（动态数量）")
    parser.add_argument("--mode", choices=["backfill", "incr", "once"], default="incr",
                        help="backfill=全周期回填; incr=增量; once=小回填")
    parser.add_argument("--timeout", type=int, default=REQUEST_TIMEOUT, help="单请求超时（秒）")
    parser.add_argument("--workers", type=int, default=0, help="并发数（默认=在线服务器数）")
    parser.add_argument("--max-servers", type=int, default=MAX_SERVERS,
                        help="最多使用服务器数（默认 11）")
    args = parser.parse_args()
    MAX_SERVERS = args.max_servers

    periods = BACKFILL_PERIODS if args.mode == "backfill" else MINUTE_PERIODS
    lmt_map = BACKFILL_LMT if args.mode == "backfill" else INCR_LMT

    tasks = []
    for code, sym, ex in SYMS:
        for p in periods:
            tasks.append((code, sym, ex, p, lmt_map.get(p, 10)))

    LOG(f"模式={args.mode}  品种={len(SYMS)}  周期={periods}  总任务={len(tasks)}  "
        f"候选服务器={len(SERVERS)}  上限={MAX_SERVERS}")
    if not SERVERS:
        LOG("请先配置 SERVERS 列表（服务器 IP:PORT）"); return

    bars, stats = collect(tasks, timeout=args.timeout, workers=args.workers)
    if not stats:
        return
    LOG(f"完成：成功={stats['success']}  失败={stats['fail']}  "
        f"总根数={stats['bars_total']}  在线={stats['online']}台  "
        f"耗时={stats['elapsed']:.1f}s  "
        f"速度={len(tasks)/max(0.1,stats['elapsed']):.1f}任务/s")

    if bars:
        _write_to_db(bars)

if __name__ == "__main__":
    main()
