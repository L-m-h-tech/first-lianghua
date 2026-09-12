# -*- coding: utf-8 -*-
r"""第103轮：纸面撮合独立 ticker 线程 paper_ticker.py——交易时段每分钟撮合一次。

背景（第102轮后用户拍板）：纸面 on_cycle 原本随 run_cycle 同节奏（交易时段 5/10 分钟），
交易时段内挂单/盯市/强平最多 10 分钟才刷一次。本模块把"撮合刷新"与"分析报告"解耦：
  - run_cycle 5.5 段照旧每轮也撮合一次 + 写 paper_account.txt（报告节奏完全不动）；
  - 本 ticker 只在**交易时段（含夜盘）**每 PAPER_TICK_INTERVAL 秒独立撮合一次：
    拉最新行情 -> paper_analysis 独立管线重算全量信号 -> on_cycle ->
    盯市/成交/强平/equity 入库 + 落盘纸面报告文件（分钟级权益曲线）。

设计要点（三铁律：不动实时主链与综合分口径；默认开关缺省等效旧版；零新增依赖）：
  1. tick_once 与 run_cycle 5.5 段逐账户循环同构（option_only 档跳过期货；链空跳过期权）；
  2. 第114轮：ticker 撮合数据来自 paper_analysis 独立管线（fut_rows/chain_map/
     strat_rows/opt_rows 全部实时重算、与主报告隔离），quotes 每次 tick 全新拉取
     （1-2 次 HTTP，亚秒级）；分析失败回退 run_cycle 快照（旧行为）；
  3. 每 broker.on_cycle/on_cycle_options 已被 paper_broker._locked 装饰（RLock），
     ticker 与 run_cycle 同分钟重叠时由锁互斥，毫秒级等待，无死锁/撕裂。
  4. **绝不** 调用 beat_heartbeat / 不踏 state.kick——看门狗只该看主循环死活；
     ticker 静默失败（逐账户 try/except，与 run_cycle 5.5 段同构），绝不拖垮主进程。

配置（config.py）：
  PAPER_TICK_INTERVAL   = 60  交易时段撮合间隔（秒）；0=关闭（run_cycle 同步驱动=旧行为）
  PAPER_TICK_TRADING_ONLY = True  仅交易时段生效；非交易 run_cycle 已每分钟且行情冻结

若后续需要期权链定向加速刷新（force=True）：在此处收集各账户活跃期权品种子集调
option_chain.warm(force=True) 并合并进 chain_map；当前 paper_analysis 已自行 warm。
"""
import threading
from datetime import datetime

import config
import futures_data



def _MinFeed(bars):
    """极简 SymbolFeed 兼容物：只给 trailing_risk_weights 需要的 dts/bars（dt/c）。"""
    from datetime import datetime as _dt
    bars2 = []
    for b in bars:
        d = b.get("dt")
        if d is None:
            continue
        if isinstance(d, str):
            try:
                d = _dt.strptime(d[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
        bars2.append({"dt": d, "c": b.get("c")})
    class _Feed:
        pass
    f = _Feed()
    f.bars = bars2
    f.dts = [b["dt"] for b in bars2]
    return f


def _inject_risk_weights(state, broker, ts, syms=None):
    """第136轮：ERC 影子账户实时权重喂入。

    对开启了 risk_sizing 的纸面账户，用 minute_bars 实时历史（严格 PIT：只用到当前时刻）
    调 portfolio.trailing_risk_weights 算风险平价目标权重，经 broker.set_risk_weights 注入内核。
    - 权重缺失/品种不足<2/历史不足 min_hist：内核自动回退等名义（与非 ERC 账户一致）；
    - 每轮都算（分钟级），与 paper_ticker 撮合节奏同步——目标权重随行情协方差滚动更新。
    """
    pf = getattr(broker, "pf", None)
    rs = getattr(pf, "risk_sizing", None)          # PaperBroker 不存，读内核（main 透传后在这）
    if not rs:
        return None
    try:
        import portfolio as pf_mod
        syms = list(syms) if syms else [m["sym"] for _, m in getattr(state, "watchlist", [])]
        if len(syms) < 2 or not getattr(state, "db", None):
            return None
        db = state.db
        feeds = {}
        for sym in syms:
            try:
                bars = db.minute_bars_for_sym(sym, 60, limit=config.PRS_WINDOW + 8)
            except Exception:
                bars = []
            if len(bars) >= 2:
                feeds[sym] = _MinFeed(bars)
        if len(feeds) < 2:
            return None
        from datetime import datetime as _dt
        t = ts
        if isinstance(t, str):
            try:
                t = _dt.strptime(t[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        wmap, meta = pf_mod.trailing_risk_weights(
            feeds, t, rs,
            window=getattr(config, "PRS_WINDOW", 126),
            min_hist=getattr(config, "PRS_MIN_HIST", 40),
            shrink=getattr(config, "PC_SHRINK", 0.10),
            cap=getattr(config, "PRS_CAP", 0.25),
            gross=getattr(pf, "risk_gross", None) or getattr(config, "PRS_GROSS", 1.5))
        if wmap:
            broker.set_risk_weights(wmap, meta)
        return meta
    except Exception:
        return None


def tick_once(state, ts, quotes):
    """单次纸面撮合驱动（纯逻辑、可单测）。返回 {name: summary|None}。

    第114轮：驱动数据来自 paper_analysis 独立管线——拉最新行情 quotes，只喂纸面
    账户的独立分析（fut_rows/chain_map/strat_rows/opt_rows），与主报告完全隔离、
    不展示；撮合后同步落盘纸面报告文件。与 run_cycle 5.5 段逐账户循环同构；
    每账户独立 try/except 吞错不影响其余账户。

    PAPER_TICK_REPRICE=True（默认）时：调 paper_analysis.paper_analyze 完整独立
    重算（综合分/期权链/单腿/组合全部来自最新行情）；False 或分析失败时回退
    run_cycle 落下的只读快照 state._paper_stash（第103轮旧行为）。
    """
    snap = getattr(state, "_paper_stash", None) or {}
    papers = getattr(state, "papers", None) or {}
    if not papers:
        return {}
    # 第108/113轮：独立管线完整重算（默认开；False/失败=回退快照=第103轮旧行为）
    if getattr(config, "PAPER_TICK_REPRICE", False):
        try:
            # 第115轮：夜盘只对活跃子集做独立分析（tick_loop 已写入 state._paper_watchlist）；
            # 无活跃子集（如刚进夜盘、子集为空）回退快照与旧行为一致。
            watchlist = list(getattr(state, "_paper_watchlist", None)
                             or getattr(state, "watchlist", None) or [])
            if watchlist:
                from paper_analysis import paper_analyze
                _pa = paper_analyze(state, quotes, watchlist)
                fut_rows = _pa.get("fut_rows") or []
                chain_map = _pa.get("chain_map") or {}
                strat_rows = _pa.get("strat_rows") or []
                opt_rows = _pa.get("opt_rows") or []
            else:
                # watchlist 缺失（测试/异常场景）：回退快照，与旧行为一致
                fut_rows = snap.get("fut_rows") or []
                chain_map = snap.get("chain_map") or {}
                strat_rows = snap.get("strat_rows") or []
                opt_rows = snap.get("opt_rows") or []
        except Exception:
            LOG.error("纸面 ticker 独立分析失败（回退快照）: ", exc_info=True)
            fut_rows = snap.get("fut_rows") or []
            chain_map = snap.get("chain_map") or {}
            strat_rows = snap.get("strat_rows") or []
            opt_rows = snap.get("opt_rows") or []
    else:
        fut_rows = snap.get("fut_rows") or []
        chain_map = snap.get("chain_map") or {}
        strat_rows = snap.get("strat_rows") or []
        opt_rows = snap.get("opt_rows") or []   # 第110轮：analyze_option 单腿信号
    if not fut_rows:
        # run_cycle 尚未落下本轮快照（首轮前/纸面刚开）：无信号输入，安全跳过不空推
        return {}
    last_papers = getattr(state, "last_papers", None)
    if last_papers is None:
        last_papers = {}
        state.last_papers = last_papers
    out = {}
    for _name, _broker in papers.items():
        try:
            # 第136轮：ERC 影子账户——对开启 risk_sizing 的 broker 在 on_cycle 前注入实时权重
            _inject_risk_weights(state, _broker, ts)
            _prio = _broker.priority
            if _prio != "option_only":
                last_papers[_name] = _broker.on_cycle(ts, fut_rows, quotes)
            else:
                last_papers[_name] = _broker.last_summary or {}
            if (strat_rows or opt_rows) and chain_map:
                _ols = _broker.on_cycle_options(ts, strat_rows, chain_map, fut_rows,
                                                opt_rows=opt_rows if opt_rows else None)
                (last_papers.get(_name) or {})["opt"] = _ols
            out[_name] = last_papers.get(_name)
        except Exception:
            continue
    # 向后兼容：基准账户
    if papers:
        _first = next(iter(papers.values()))
        state.last_paper = (last_papers.get(getattr(_first, "name", None))
                            if getattr(_first, "name", None) in last_papers
                            else _first.last_summary)
    # 第114轮：撮合后同步落盘纸面报告（文件与主报告隔离；失败静默不拖垮 ticker）
    try:
        import report
        report.write_paper_account(state)
    except Exception:
        LOG.error("纸面 ticker 报告落盘失败（已吞掉）: ", exc_info=True)
    return out


def tick_loop(state):
    """daemon 主循环（照抄 oil_loop 模式）：每 PAPER_TICK_INTERVAL 秒撮合一次。"""
    interval = max(1, int(getattr(config, "PAPER_TICK_INTERVAL", 60) or 1))
    trading_only = bool(getattr(config, "PAPER_TICK_TRADING_ONLY", True))
    LOG.info("纸面 ticker 线程启动（每 %d 秒撮合一次%s）",
             interval, "，仅交易时段" if trading_only else "")
    while not state.stop.is_set():
        state.stop.wait(interval)
        if state.stop.is_set():
            break
        try:
            if trading_only and not _is_trading():
                continue
            # 行情代码：reprice 模式用 watchlist（不依赖 run_cycle 快照是否已落）；
            # 旧模式沿用快照 codes（向后兼容）
            # 第115轮：夜盘时段只拉"正在自身交易时段"的品种子集——无夜盘品种夜盘行情
            # 冻结为上一收盘价，拉取并撮合会产生无意义成交；活跃子集由 utils.trading_subset 筛。
            if getattr(config, "PAPER_TICK_REPRICE", False):
                from utils import trading_subset
                _wl = trading_subset(getattr(state, "watchlist", None) or [])
                codes = sorted({meta["code"] for _, meta in _wl})
                if _wl and codes:
                    state._paper_watchlist = list(_wl)   # 供 tick_once 用同子集做独立分析
            else:
                snap = getattr(state, "_paper_stash", None) or {}
                codes = snap.get("codes") or []
            if not codes:
                continue
            quotes = futures_data.fetch_quotes(codes)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            tick_once(state, ts, quotes)
        except Exception:
            LOG.error("纸面 ticker 本轮异常（已吞掉）: ", exc_info=True)


def _is_trading():
    try:
        from utils import is_trading_time
        return bool(is_trading_time()[0])
    except Exception:
        return True  # 判定失败保守当交易中（不跳过撮合，宁多勿少）


try:  # noqa: F401  LOG 仅在 main/报告语境可用；独立运行/测试时静默
    from utils import LOG  # noqa: F401
except Exception:
    import logging
    LOG = logging.getLogger("paper_ticker")
    LOG.addHandler(logging.NullHandler())