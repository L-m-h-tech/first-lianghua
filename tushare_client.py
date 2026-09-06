# -*- coding: utf-8 -*-
r"""G18 Tushare 零依赖适配层（第90轮解锁落地）——裸 HTTP、token 走环境变量、软降级。

项目纪律：不 pip 装 tushare SDK（版本停更+拖依赖），只用现有 http_client 裸调代理端点。
用户提供代理 token（xiaodefa.top），token **只走环境变量 TUSHARE_TOKEN**（.env gitignored），
本文件与仓库绝不含 token 明文。

功能：
  - call(api_name, **params)：POST 代理端点，把 tushare 标准返回 {fields, items}
    解包为 list[dict]；缺 token/断网/非200/坏返回 → None（软降级，绝不抛）。
  - trade_cal_month(exchange, yyyymm)：某月交易日历（本代理实测 exchange 参数不生效、
    固定返回 SSE，节假日校验够用；期货专属差异诚实标注）。
  - fut_wsr_snapshot()：最新交易日全市场仓单快照，按 (symbol, fut_name) 汇总总仓单/环比。

诚实边界（实测确认）：
  - fut_wsr 的 trade_date/start_date/end_date 参数被代理忽略（恒返回最新交易日）→
    历史仓单回填不可行，仅当日快照；库存分位从"3个月升多年"的 T2 目标部分受限。
  - trade_cal 只回 SSE（代理侧限制）→ 节假日校验与 A股同源，期货夜盘/部分品种差异不覆盖。

用法：
  D:\Python\python.exe tools\tushare_ingest.py            # T1 日历校验 + T2 仓单快照
  D:\Python\python.exe tushare_client.py --selftest
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import http_client                                   # noqa: E402  现有连接池

DEFAULT_PROXY = "https://t.xiaodefa.top/"


def token():
    return os.environ.get("TUSHARE_TOKEN") or None


def proxy_url():
    return (os.environ.get("TUSHARE_PROXY_URL") or DEFAULT_PROXY).rstrip("/") + "/"


def call(api_name, timeout=40, **params):
    """调用代理接口；成功返回 list[dict]，失败返回 None（软降级）。"""
    t = token()
    if not t:
        return None
    try:
        payload = {"api_name": api_name, "token": t, **params}
        r = http_client.http.post(proxy_url(), json=payload, timeout=timeout)
        if r.status_code != 200:
            return None
        obj = r.json()
        if not obj or obj.get("code") != 0:
            return None
        data = obj.get("data") or {}
        fields = data.get("fields") or []
        items = data.get("items") or []
        return [dict(zip(fields, row)) for row in items]
    except Exception:
        return None


# ---------------- T1 交易日历 ----------------
def trade_cal_month(exchange="SSE", yyyymm=None):
    """某月交易日历（is_open=1 的交易日日期集合）。yyyymm='YYYYMM'，缺省当月。"""
    yyyymm = yyyymm or datetime.now().strftime("%Y%m")
    start = yyyymm + "01"
    # 月尾取 31 日稳妥（is_open=0 也会返回，过滤即可）
    end = yyyymm + "31"
    rows = call("trade_cal", exchange=exchange, start_date=start, end_date=end)
    if not rows:
        return None
    return sorted({r["cal_date"] for r in rows if str(r.get("is_open")) == "1"})


# ---------------- T2 仓单快照 ----------------
def fut_wsr_snapshot():
    """最新交易日全市场仓单：按 (symbol, fut_name) 汇总 总仓单/环比。

    实测代理恒返回最新交易日；同一 (symbol,warehouse) 可能出现两行（明细+总量），
    此处按 symbol 汇总 vol 并去 fut_name 取非空名。返回 {"trade_date", "by_symbol": {...}}。"""
    rows = call("fut_wsr")
    if not rows:
        return None
    dates = sorted({r.get("trade_date") for r in rows if r.get("trade_date")})
    by = {}
    for r in rows:
        sym = r.get("symbol")
        if not sym:
            continue
        try:
            v = float(r.get("vol") or 0)
        except (TypeError, ValueError):
            v = 0.0
        name = r.get("fut_name") or by.get(sym, {}).get("name")
        ent = by.setdefault(sym, {"name": name, "vol": 0.0, "pre_vol": 0.0, "warehouses": set()})
        ent["vol"] += v
        ent["name"] = name or ent["name"]
        w = r.get("warehouse")
        if w:
            ent["warehouses"].add(w)
        try:
            ent["pre_vol"] += float(r.get("pre_vol") or 0)
        except (TypeError, ValueError):
            pass
    out = {"trade_date": dates[-1] if dates else None, "by_symbol": {}}
    for sym, ent in by.items():
        out["by_symbol"][sym] = {"name": ent["name"], "vol": round(ent["vol"], 1),
                                 "pre_vol": round(ent["pre_vol"], 1),
                                 "vol_chg": round(ent["vol"] - ent["pre_vol"], 1),
                                 "n_warehouses": len(ent["warehouses"])}
    return out


def selftest():
    # 1) 无 token → call 返回 None（零请求）
    os.environ.pop("TUSHARE_TOKEN", None)
    assert token() is None and call("trade_cal") is None
    # 2) 标准返回解包（mock transport 注入）
    os.environ["TUSHARE_TOKEN"] = "test"
    _orig_post = http_client.http.post

    def fake_post(url, json=None, timeout=None, **kw):
        class R:
            status_code = 200
            def json(self):
                return {"code": 0, "data": {"fields": ["a", "b"],
                                            "items": [[1, 2], [3, 4]]}}
        return R()

    http_client.http.post = fake_post
    try:
        rows = call("any_api", x=1)
        assert rows == [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    finally:
        http_client.http.post = _orig_post
    # 3) 非200/坏返回 → None
    def bad_post(url, json=None, timeout=None, **kw):
        class R:
            status_code = 500
            def json(self):
                return {}
        return R()
    http_client.http.post = bad_post
    try:
        assert call("x") is None
    finally:
        http_client.http.post = _orig_post
    # 4) 仓单聚合：重复行去名 + 按 symbol 汇总
    os.environ["TUSHARE_TOKEN"] = "test"
    def wsr_post(url, json=None, timeout=None, **kw):
        class R:
            status_code = 200
            def json(self):
                return {"code": 0, "data": {"fields": ["trade_date", "symbol", "fut_name",
                                                        "warehouse", "pre_vol", "vol"],
                                            "items": [["20260904", "A", None, "库1", 10, 20],
                                                      ["20260904", "A", "豆一", "库1", 10, 20],
                                                      ["20260904", "A", "豆一", "库2", 30, 40],
                                                      ["20260904", "B", None, "库X", 1, 2]]}}
        return R()
    http_client.http.post = wsr_post
    try:
        snap = fut_wsr_snapshot()
        assert snap["trade_date"] == "20260904"
        assert snap["by_symbol"]["A"]["vol"] == 80.0      # 20+20+40
        assert snap["by_symbol"]["A"]["name"] == "豆一"
        assert snap["by_symbol"]["A"]["n_warehouses"] == 2
        assert snap["by_symbol"]["B"]["vol"] == 2.0
    finally:
        http_client.http.post = _orig_post
    os.environ.pop("TUSHARE_TOKEN", None)
    print("tushare_client selftest ALL PASS（无token零请求/标准解包/非200降级/仓单聚合 共4组）")
    return 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="G18 Tushare 零依赖适配层（token 走 env）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    t = token()
    print("TUSHARE_TOKEN 已配置:", bool(t))
    print("代理:", proxy_url())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
