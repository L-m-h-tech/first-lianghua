# -*- coding: utf-8 -*-
r"""第114轮：纸面账户独立分析管线 paper_analysis.py。

与主报告完全隔离的纸面专用分析：输入最新行情 quotes，产出
fut_rows（含 option_chain/iv_surface 装饰）+ opt_rows + strat_rows + chain_map，
只喂纸面账户（paper_ticker 每分钟 + run_cycle 5.5），不写任何报告/DB/展示文件。

设计要点：
  1. 复刻 run_cycle 中"主报告分析"的数据管线（analyze_all_varieties -> 期权链
     装饰 -> analyze_option -> recommend），但产出是独立的一份 fut_rows 列表，
     与主报告 fut_rows 互不修改。
  2. 无副作用：不写 state.last_forecasts / 不入库 / 不渲染；失败静默降级
     （调用方按空结果处理）。
  3. 复用只读共享缓存（klines TTL30min / option_chain TTL30min / rank 日缓存），
     每分钟调用命中缓存，零额外网络（除 quotes 本身）。
  4. analyzer.analyze_all_varieties 内部 _analyze_lock(RLock) 与 run_cycle 互斥，
     线程安全；broker 侧 on_cycle/on_cycle_options 由 _locked 保护。
"""
import time
from collections import deque
from datetime import date

import analyzer
import config
import contracts
import iv_surface
import option_analyzer
import option_strategies


def paper_analyze(state, quotes, watchlist=None):
    """纸面独立分析入口：行情 -> 期货综合分 + 期权链装饰 + 期权单腿/组合策略。

    state    : 主监控状态（复用其缓存：var_hist/flow_tracker/webdata/opt_chains/db）
    quotes   : {code: 行情dict}（futures_data.fetch_quotes 产出，调用方已拉取）
    watchlist: [(name, meta)]；None 时取 state.watchlist（主报告口径）
    返回 {"fut_rows": [], "opt_rows": [], "strat_rows": [], "chain_map": {}, "codes": []}；
    任何环节失败都只跳过对应部分（静默），绝不抛出影响 ticker/run_cycle。
    """
    watchlist = list(watchlist) if watchlist is not None else list(state.watchlist)
    quotes = quotes or {}
    codes = sorted({meta["code"] for _, meta in watchlist})
    out = {"fut_rows": [], "opt_rows": [], "strat_rows": [], "chain_map": {}, "codes": codes}
    if not watchlist or not codes:
        return out

    # 1. 期货综合分（与主报告同一评分路径；内部只读共享缓存）
    now_ts = time.time()
    try:
        for key, meta in watchlist:
            q = quotes.get(meta["code"])
            if q and q.get("latest"):
                state.var_hist.setdefault(key, deque(maxlen=240)).append((now_ts, q["latest"]))
        flow_map = state.flow_tracker.update(quotes, now_ts) if hasattr(state, "flow_tracker") else {}
        fut_rows = analyzer.analyze_all_varieties(state, watchlist, quotes, flow_map)
    except Exception:
        fut_rows = []
    if not fut_rows:
        return out
    out["fut_rows"] = fut_rows

    # 2. 期权链装饰（挂 option_chain/iv_surface，供 analyze_option/recommend 消费）
    chain_map = _decorate_option_chains(state, fut_rows)
    out["chain_map"] = chain_map or {}

    # 3. 期权单腿严格分析 + 4. 组合策略推荐（纯函数，逐品种隔离异常）
    for row in fut_rows:
        if row["name"] not in config.OPTION_VARIETIES or (row.get("price") or 0) <= 0:
            continue
        try:
            out["opt_rows"].append(option_analyzer.analyze_option(row["name"], row))
        except Exception:
            pass
        try:
            s = option_strategies.recommend(row["name"], row)
            if s:
                s["variety"] = row["name"]
                out["strat_rows"].append(s)
        except Exception:
            pass
    return out


def _decorate_option_chains(state, fut_rows):
    """复刻 run_cycle 的期权链装饰段（main.py 3.9 节）：主力月份链挂 row["option_chain"]、
    多到期日链组装 IV 曲面挂 row["iv_surface"]。仅装饰传入的 fut_rows，无 DB/报告副作用。

    链缓存 TTL=OPTION_CHAIN_TTL(30分钟)，重复调用命中缓存零网络。
    返回 chain_map（{(sym,yy,mm): summary}），供调用方透传给 broker 期权撮合。
    """
    try:
        opt_cal = state.webdata.calendar_snapshot()
    except Exception:
        opt_cal = {}
    chain_tasks, underlying_map, variety_expiries = [], {}, {}
    for row in fut_rows:
        if row["name"] not in config.OPTION_VARIETIES or (row.get("price") or 0) <= 0:
            continue
        sym = row["sym"]
        months = []
        cal_months = opt_cal.get(sym) or {}
        for yymm in sorted(cal_months):
            # 日历键为完整年月6位（202611），新浪T链pinzhong需两位年（2611）
            yy, mm = (yymm // 100) % 100, yymm % 100
            exp_date = cal_months[yymm].get("exp_date")
            dleft = (exp_date - date.today()).days if exp_date \
                else contracts.estimate_option_days(yy, mm)
            if dleft < config.IV_SURFACE_MIN_DAYS:
                continue
            months.append((yy, mm, dleft))
        if not months:  # 日历缺失时回退到合约探测的期权月份
            om0 = row.get("opt_month") or {}
            if om0.get("yy"):
                months = [(om0["yy"], om0["mm"], om0.get("opt_days", config.OPT_ASSUMED_DAYS))]
        months = months[:config.IV_SURFACE_EXPIRIES]
        if not months:
            continue
        variety_expiries[sym] = months
        for yy, mm, _d in months:
            chain_tasks.append((sym, row["ex"], yy, mm))
        underlying_map[sym] = row["price"]
    if not chain_tasks:
        return {}
    try:
        chain_map = state.opt_chains.warm(chain_tasks, underlying_map=underlying_map)
    except Exception:
        return {}

    for row in fut_rows:
        sym = row["sym"]
        months = variety_expiries.get(sym)
        if not months:
            continue
        chains_by_label, days_map = {}, {}
        for yy, mm, dleft in months:
            ch = chain_map.get((sym, int(yy), int(mm)))
            if not ch:
                continue
            try:
                ch["pcr_pct"] = state.db.pcr_percentile(sym, ch.get("pcr_oi"))
            except Exception:
                ch["pcr_pct"] = None
            label = "%02d%02d" % (int(yy), int(mm))
            chains_by_label[label] = ch
            days_map[label] = dleft
        om = row.get("opt_month") or {}
        main_label = "%02d%02d" % (int(om["yy"]), int(om["mm"])) if om.get("yy") else None
        if main_label and main_label in chains_by_label:
            row["option_chain"] = chains_by_label[main_label]
        try:
            surf = iv_surface.build_surface(sym, row["ex"], row["price"],
                                            chains_by_label, days_map, main_label=main_label)
            if surf:
                row["iv_surface"] = surf
        except Exception:
            pass
    return chain_map