"""第156轮 A3：云服务器健康度页签——探测 data/server_ips.txt 各台 /health 端点，写 reports/server_health.json。

- 探测对象：config.SINA_SERVER_URLS（由 data/server_ips.txt 动态读取，每天 IP 变化只改一个文件）
- 端点：tools/sina_proxy_server.py 的 GET /health → {status, uptime_s, daily, minute, pops, fail}
- 后台线程 server_health_loop 周期探测（默认 5 分钟），看板"云服务器健康"页签读 JSON 渲染表格；
  任一探测失败只记离线（error），不影响监控主链。纯标准库+requests，零新增运行依赖。
"""

import concurrent.futures
import json
import threading
from datetime import datetime

import config

try:
    import requests
except Exception:  # pragma: no cover - 生产依赖缺失时探测整体失败，看板显空态
    requests = None

_LOCK = threading.Lock()


def _probe_one(url, timeout):
    """探测单台 /health，返回状态 dict；失败只记 error，不抛。"""
    if requests is None:
        return {"url": url, "online": False, "error": "requests 缺失"}
    try:
        r = requests.get(url.rstrip("/") + "/health", timeout=timeout)
        if r.status_code != 200:
            return {"url": url, "online": False, "error": "HTTP %s" % r.status_code}
        d = r.json()
        ok = d.get("status") == "ok"
        out = {"url": url, "online": ok, "error": ""}
        if ok:
            out.update(
                {
                    "uptime_s": d.get("uptime_s"),
                    "daily": int(d.get("daily", 0) or 0),
                    "minute": int(d.get("minute", 0) or 0),
                    "pops": int(d.get("pops", 0) or 0),
                    "fail": int(d.get("fail", 0) or 0),
                }
            )
        else:
            out["error"] = "status=%s" % d.get("status")
        return out
    except Exception as e:
        return {"url": url, "online": False, "error": str(e)[:120]}


def probe_servers(urls=None, timeout=None):
    """并发探测全部服务器 /health，返回看板负载：
    {generated_at, n_total, n_online, servers:[{url, online, error, uptime_s, daily, minute, pops, fail}]}。"""
    urls = list(urls) if urls is not None else list(config.SINA_SERVER_URLS or [])
    timeout = timeout if timeout is not None else config.SERVER_HEALTH_TIMEOUT
    base = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n_total": len(urls),
        "n_online": 0,
        "servers": [],
    }
    if not urls:
        return base
    servers = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(urls))) as ex:
        for s in ex.map(lambda u: _probe_one(u, timeout), urls):
            servers.append(s)
    base["servers"] = servers
    base["n_online"] = sum(1 for s in servers if s.get("online"))
    return base


def write_server_health():
    """探测一次并写 reports/server_health.json；失败返回 False（看板显空态，不影响主链）。"""
    try:
        d = probe_servers()
        with _LOCK:
            with open(config.SERVER_HEALTH_FILE, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
        return True
    except Exception:
        return False


def health_loop(state, interval=None):
    """后台线程：启动即探一次，之后每 interval 秒周期探测写 JSON。state.stop 置位即退出。"""
    interval = interval if interval is not None else config.SERVER_HEALTH_INTERVAL
    write_server_health()
    while not state.stop.is_set():
        if state.stop.wait(interval):
            return
        try:
            write_server_health()
        except Exception:
            pass
