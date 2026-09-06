# -*- coding: utf-8 -*-
r"""G18（第90轮）Tushare 接入落地：T1 交易日历校验 + T2 仓单快照（研究侧离线工具）。

- T1：trade_cal 当月交易日历 vs config.STATIC_HOLIDAY_RANGES 对照——差异只告警不替代
  （诚实：代理 trade_cal 固定返回 SSE，A股休市与期货大体同源，期货夜盘/部分差异不覆盖）。
- T2：fut_wsr 最新交易日全市场仓单，按品种聚合 总仓单/环比/交割库数 → reports/warehouse_snapshot.txt
  （诚实：代理 fut_wsr 恒返回最新交易日，历史回填暂不可行——库存分位"3月升多年"目标部分受限）。
- token 走 env TUSHARE_TOKEN（.env gitignored）；零新增依赖、断网/无token全软降级不抛。
用法：
  D:\Python\python.exe tools\tushare_ingest.py            # T1 校验 + T2 快照
  D:\Python\python.exe tools\tushare_ingest.py --selftest
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config                          # noqa: E402
import tushare_client as tc            # noqa: E402  G18 适配层

SNAP_TXT = ROOT / "reports" / "warehouse_snapshot.txt"
SNAP_JSON = ROOT / "reports" / "warehouse_snapshot.json"
CAL_TXT = ROOT / "reports" / "tushare_cal_check.txt"


def holiday_check(verbose=True):
    """T1：当月 trade_cal 交易日 vs STATIC_HOLIDAY_RANGES 对照（差异只告警）。"""
    yyyymm = datetime.now().strftime("%Y%m")
    rows = tc.call("trade_cal", exchange="SSE", start_date=yyyymm + "01",
                   end_date=yyyymm + "31")
    L = ["=" * 88,
         " G18 T1 交易日历校验（trade_cal vs STATIC_HOLIDAY_RANGES；差异只告警不替代）  生成于 %s"
         % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         "=" * 88]
    if not rows:
        L.append("trade_cal 不可用（无 token/断网/代理异常）——软降级：维持手动节假日表，本工具跳过。")
        text = "\n".join(L)
        os.makedirs(os.path.dirname(str(CAL_TXT)), exist_ok=True)
        with open(CAL_TXT, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
        if verbose:
            print(text)
        return {"available": False}
    cal_days = {r["cal_date"] for r in rows if str(r.get("is_open")) == "1"}
    trading_days = sorted(d for d in cal_days if d.startswith(yyyymm))
    # 当月休市日（代理 start/end 参数不生效、返回全历史，故只取当月前缀过滤）
    month_closed = sorted({r["cal_date"] for r in rows
                           if r["cal_date"].startswith(yyyymm)
                           and str(r.get("is_open")) == "0"})
    L.append("当月 %s：交易日 %d 天，休市日 %s" % (yyyymm, len(trading_days), month_closed or "无"))
    L.append("注：代理 trade_cal 固定返回 SSE（A股日历），期货节假日与 A股大体同源、但夜盘/部分品种差异不覆盖；")
    L.append("    本对照仅供发现“忘更新节假日表”这类事故，不替代手工维护。")
    text = "\n".join(L)
    os.makedirs(os.path.dirname(str(CAL_TXT)), exist_ok=True)
    with open(CAL_TXT, "w", encoding="utf-8", newline="\n") as f:
        f.write(text + "\n")
    if verbose:
        print(text)
    return {"available": True, "month": yyyymm, "trading_days": len(trading_days),
            "closed": month_closed}


def warehouse_snapshot(verbose=True):
    """T2：fut_wsr 最新交易日仓单按品种聚合，落 reports/warehouse_snapshot.txt/.json。"""
    snap = tc.fut_wsr_snapshot()
    L = ["=" * 88,
         " G18 T2 仓单快照（fut_wsr 最新交易日，按品种汇总；研究侧）  生成于 %s"
         % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         "=" * 88]
    if not snap:
        L.append("fut_wsr 不可用（无 token/断网/代理异常）——软降级跳过。")
        text = "\n".join(L)
        os.makedirs(os.path.dirname(str(SNAP_TXT)), exist_ok=True)
        with open(SNAP_TXT, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
        with open(SNAP_JSON, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"available": False}, f, ensure_ascii=False)
        if verbose:
            print(text)
        return {"available": False}
    L.append("交易日 %s | 品种 %d 个（总仓单=按 symbol 汇总各交割库 vol，含明细/总量重复行去重）"
             % (snap["trade_date"], len(snap["by_symbol"])))
    L.append("%-6s %-8s %12s %12s %12s %8s" % ("品种", "名称", "总仓单", "前仓单", "环比", "库数"))
    by = snap["by_symbol"]
    for sym in sorted(by):
        e = by[sym]
        L.append("%-6s %-8s %12.1f %12.1f %+11.1f %8d"
                 % (sym, e["name"] or "-", e["vol"], e["pre_vol"], e["vol_chg"],
                    e["n_warehouses"]))
    L.append("诚实边界：代理 fut_wsr 恒返回最新交易日（历史回填暂不可行）——库存分位'3个月升多年'目标部分受限，")
    L.append("后续若代理开放日期参数再补历史回填；本快照为当日全市场库存横向对照，研究侧不进综合分。")
    L.append("=" * 88)
    text = "\n".join(L)
    os.makedirs(os.path.dirname(str(SNAP_TXT)), exist_ok=True)
    with open(SNAP_TXT, "w", encoding="utf-8", newline="\n") as f:
        f.write(text + "\n")
    with open(SNAP_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"available": True, "trade_date": snap["trade_date"],
                   "by_symbol": snap["by_symbol"]}, f, ensure_ascii=False, indent=1)
    if verbose:
        print(text)
    return {"available": True, "trade_date": snap["trade_date"], "n_symbols": len(by)}


def run(verbose=True):
    r1 = holiday_check(verbose=verbose)
    r2 = warehouse_snapshot(verbose=verbose)
    return {"cal": r1, "warehouse": r2}


def selftest():
    # 依赖 tushare_client selftest（零网络）
    assert tc.selftest() == 0
    print("tushare_ingest selftest ALL PASS（依赖 tushare_client 4组全过）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="G18 Tushare 接入：T1 日历校验 + T2 仓单快照")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
