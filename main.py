# -*- coding: utf-8 -*-
"""
期货全品种监控分析 主程序
========================
【需求功能对照（按需求演进顺序，详见 上下文摘要.md）】
  需求① 新闻60s因子+原油10s   -> oil_loop + sina_news/factors/oil_data
  需求② 期货分析购买建议      -> run_cycle 第3步 analyzer
  需求③ 期权严格分析          -> run_cycle 第4步 option_analyzer
  需求④⑧⑨⑩ 报告按时段分流/滚动5轮/复盘/日切/置顶 -> report.save/rollover/review
  需求⑤ 自动开期货通+四大所全品种+合约月份 -> startup_open_ths/build_universe/contracts
  需求⑥ 两网站数据接入+期权策略推荐 -> webdata + option_strategies（run_cycle 第4.5步）
  需求⑦ 浏览器页面直读+非交易时段预测走向 -> breader + analyzer.forecast_line
  增强⑪ 原油急动紧急轮动      -> oil_loop detect_jump + wait_with_emergency
  增强⑫ 全网数据查找3分钟     -> web_scan_loop（新闻/金融/突发事件，可信度分级）
  P1-⑦ 主动告警              -> alerts.AlertManager（声音 + 可选 Webhook，轮末聚合防轰炸）
  P1-⑧ SQLite 结构化落库     -> storage.MonitorDB（quotes/news/signals/options/outcomes）
  P1-⑨ 信号胜率追踪          -> signal_outcomes 30分钟/2小时/次日回填方向收益
  P1-⑪ 量仓资金因子          -> flow_tracker.FlowTracker（增仓上行/下行、放量/缩量）

分析范围：上期所 / 上期能源 / 大商所 / 郑商所 / 广期所 全部品种及对应期权
（购买建议自动标注具体合约：如 "做多 rb2610"、"买入2610月份看涨期权 执行价≈620"）

运行节奏：
  - 每10秒：布伦特/纽约原油实时行情（后台线程）
  - 每3分钟：全网数据查找（后台线程）——东财7x24/新浪滚动/华尔街见闻/同花顺7x24新闻、
    全球金融行情（美元指数/金银/美股/A股）、突发事件；真实源优先、存疑消息降权后排；
    新出现的高影响消息按"紧急轮动"插队处理（同原油急动）
  - 轮动分析：日盘 09:00-11:30 / 13:30-15:00；夜盘 21:00 开盘后按品种分档收市——
    多数品种 23:00、有色系列（铜铝锌铅镍锡/不锈钢/国际铜/氧化铝）次日01:00、
    黄金/白银/原油次日02:30；全局只要还有品种在交易就按交易时段节奏轮动
    （每时段开盘前30分钟每5分钟一轮、之后每10分钟，对齐刻度），非交易时段每1分钟一轮。
    报告块头标明本轮时间、节奏与下一轮计划时间；法定节假日按交易日历自动休市。
    **时段切换边沿触发**：非交易时段启动后，一到开盘点（09:00/13:30/21:00）立即跑一轮交易时段
    轮动、其后严格按交易时段节奏（5/10分钟）排程；交易时段结束（午休/收盘/凌晨收市）同理立即
    轮动一轮并切回非交易节奏（每1分钟），不必等下一个固定刻度。
  - 紧急轮动（原油10s急动 / 全网3分钟扫描发现高影响消息）：立即全品种实时重算、
    全部报告同步写入、看板自动刷新；**原定轮动时刻不重算、不推移**
  - 每轮结构化落 SQLite；|综合分|≥2 的信号自动在30分钟/2小时/次日回填方向收益；
    强信号、跨档、多空翻转、期权策略和紧急轮动可声音/Webhook 主动告警
  - 一个交易日全部结束后（有夜盘则次日02:30后、无夜盘品种日15:00后）自动汇总当日
    全部轮动报告+当日新闻，生成复盘到 reports/daily_review.txt（永久保留、最新在最前）
  - 次日首次运行自动清除五个轮动文件中昨日的轮动块
  - 后台：主力合约月份探测(30分钟)、日线+30/60分钟指标预刷新、期货通调试模式自动打开（仅启动时一次）
  - 实时查看：**程序生成第一轮真实报告后会自动用默认浏览器打开 reports/实时报告.html**
    （config.REPORT_AUTO_OPEN 总开关、--no-launch 可关）；也可双击"查看实时报告.bat"，
    有新报告自动刷新，无需关闭重开

用法:
  python main.py              常驻运行（推荐）
  python main.py --once       只跑一轮分析后退出（测试用）
  python main.py --no-launch  不自动打开同花顺期货通、也不自动弹出首轮实时报告HTML
"""
import argparse
import math
import os
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date

import alerts
import analyzer
import browser_reader
import config
import contracts
import cross_section
import data_health
from data_router import REGISTRY
import factors
import flow_tracker
import fundamental_data
import futures_data
import intraday_bars
import tdx_bars
import iv_surface
import oil_data
import option_analyzer
import option_chain
import option_strategies
import paper_broker
import paper_ticker
import report
import risk_gate
import signal_calibrator
import sina_news
import storage
import ths_app
import trade_calendar
import web_scan
import webdata
from utils import (LOG, clip, is_trading_time, is_variety_trading, next_cycle_time,
                   next_transition, review_is_due, rotation_desc, setup_environment,
                   trade_owner_date)


class State:
    def __init__(self, universe):
        self.stop = threading.Event()
        self.oil = oil_data.OilTracker()          # 原油10秒级行情与因子
        self.news = factors.NewsFactor()          # 新闻滚动池与情绪因子
        self.klines = futures_data.KlineCache()   # 日线指标缓存
        self.contracts = contracts.ContractCache()  # 主力合约月份缓存
        self.opt_chains = option_chain.OptionChainCache()  # 第11轮：期权完整T链/PCR缓存
        self.var_hist = {}                        # 各品种盘中价格序列
        self.flow_tracker = flow_tracker.FlowTracker()  # 成交量/持仓量资金流因子
        self.db = storage.MonitorDB()             # SQLite 结构化落库与信号效果追踪
        self.alerts = alerts.AlertManager()       # 声音/Webhook 主动告警
        self.watchlist = universe                 # [(品种名, meta)] 即分析范围
        self.wl_source = "四大交易所全品种"
        self.universe_note = ""
        self.cycle = 0
        self.store = report.ReportStore()         # 滚动窗口(5轮) + 归档管理
        self.webdata = webdata.WebDataTracker()   # 交易可查机构观点 + OpenVlab期权日历
        self.breader = browser_reader.BrowserReader()  # 浏览器页面直读(调试端口)
        self.webscan = web_scan.WebScanner()      # 全网扫描(新闻/金融/突发事件,每3分钟)
        self.rotation_desc = ""                   # 当前轮动节奏描述（写入报告）
        self.kick = threading.Event()             # 统一紧急事件：原油急动/全网高影响消息都置位它
        self.last_emergency = None                # 最近一次紧急触发信息 {"src": "oil"/"web", ...}
        self.emergency_tag = ""                   # 紧急轮动块头标记（如[全网消息紧急轮动]）
        self.emergency_note = ""                  # 本轮紧急轮动的正文说明（空串=正常定时轮）
        self.review_date = ""                     # 已生成复盘报告的归属交易日
        self.last_strat_rows = []                 # 末轮期权策略（供复盘引用）
        self.last_forecasts = {}                  # 末轮预测走向（供复盘引用）
        self.last_cross_section = {}             # 末轮横截面相对强弱（WP-F1，供报告/看板）
        self.calibrator = signal_calibrator.SignalCalibrator(enabled=False)  # WP-F2 A3 历史胜率校准器（每轮4.7刷新，默认影子）
        self.health_monitor = data_health.HealthMonitor()  # G6 数据质量跨轮监控
        self.last_health = None                           # G6 末轮数据健康结果（供报告渲染）
        # G1（二）纸面交易影子账户：PAPER_ENABLED=False 时为 None 完全休眠（不实例化、零开销、
        # 不动实时主链与综合分口径）；开启后由 run_cycle 第5.5步喂 fut_rows/quotes 持续虚拟撮合。
        self.paper = None
        self.last_paper = None                            # 末轮纸面 on_cycle 结果（供报告渲染）
        self.papers = {}                                  # 第102轮：多账户 {name: broker}
        self.last_papers = {}                             # 第102轮：多账户 {name: summary}
        self._paper_stash = {}                            # 第103轮：run_cycle 末落下的只读快照（纸面 ticker 用）
        if getattr(config, "PAPER_ENABLED", False):
            accounts = getattr(config, "PAPER_ACCOUNTS", [])
            if not accounts:
                # 单账户兼容（无 PAPER_ACCOUNTS 列表时回旧行为）
                try:
                    self.paper = paper_broker.PaperBroker(db=self.db)
                    self.papers = {"基准": self.paper}
                    LOG.info("G1 纸面交易影子账户已启用（fill=%s，初始资金%.0f，平今/平昨按结算交易日判定）",
                             self.paper.fill_mode, config.PAPER_EQUITY0)
                except Exception:
                    LOG.warning("纸面账户初始化失败，本轮完全休眠（不影响监控主链）:\n%s",
                                traceback.format_exc())
                    self.paper = None
            else:
                db_dir = getattr(config, "PAPER_ACCOUNT_DB_DIR",
                                 os.path.join(config.BASE_DIR, "data", "paper_accounts"))
                try:
                    os.makedirs(db_dir, exist_ok=True)
                except Exception:
                    pass
                for acct in accounts:
                    try:
                        name = acct.get("name", "")
                        db_name = name.replace(" ", "_").replace("/", "_")
                        db_path = os.path.join(db_dir, f"paper_{db_name}.db")
                        # 去掉 name/priority/futures_max/options_max（PaperBroker.__init__ 自带默认）
                        kwargs = {k: acct[k] for k in acct if k in (
                            "equity0", "fill_mode", "entry_score", "exit_score",
                            "per_symbol", "max_symbol_weight", "max_sector_weight",
                            "max_concurrent", "risk_liquidate", "risk_safe",
                            "opt_premium_ratio", "stop_loss_ratio", "priority",
                            "futures_max", "options_max", "target_basis")}
                        broker = paper_broker.PaperBroker(
                            db_path=db_path, name=name, **kwargs)
                        self.papers[name] = broker
                    except Exception:
                        LOG.warning("账户 %s 初始化失败（跳过）: %s",
                                    acct.get("name", "??"), traceback.format_exc())
                if self.papers:
                    self.paper = list(self.papers.values())[0]  # 第一个=基准（向后兼容）
                    LOG.info("G1 纸面交易影子账户已启用：%d 个账户%s",
                             len(self.papers),
                             "".join(f"  {n}({b.fill_mode}/{b.entry_score}/{'option_only' if b.priority=='option_only' else b.priority})"
                                     for n, b in self.papers.items())[:200])
                else:
                    LOG.warning("G1 所有纸面账户初始化失败")
        self.heartbeat_ts = time.time()           # 主循环最近一次心跳（看门狗监控卡死）
        self.auto_open_report = False             # 首轮真实报告生成后是否自动用浏览器打开（由 --no-launch 关闭）
        self.report_opened = False                # 实时报告 HTML 是否已自动打开过（全程只开一次）
        # 第13轮 WP-C 基本面：fetcher 负责直连，fund_inv/fund_basis 为后台日频刷新的原料缓存
        self.fetcher = fundamental_data.FundamentalFetcher()
        self.fund_inv = {}                        # sym大写 -> 库存/仓单时序（日频）
        self.fund_basis = None                    # 生意社全市场基差表 {sym: 基差率}，反爬时为None
        self.fund_day = ""                        # 最近一次完成日频刷新的自然日
        # 分钟K：新浪主连全周期(含1m)为主、东财具体合约兜底的采集器（常驻自采，落 minute_bars 表）
        self.minute_fetcher = intraday_bars.MinuteBarFetcher()
        # 通达信可选源（probe 确认能取期货才启用，否则 available=False 零成本跳过）
        self.tdx_minute = tdx_bars.TdxMinuteSource() if config.MINUTE_TDX_ENABLED else None
        # 多源统一采集器：新浪主连全周期为主、东财兜底、通达信可选冗余
        self.minute_collector = intraday_bars.MinuteCollector(self.minute_fetcher, self.tdx_minute)


def build_universe():
    """按交易所归集全部分析品种并排序"""
    uni = [(name, meta) for name, meta in config.VARIETIES.items()
           if meta.get("ex") in config.ANALYZE_EXCHANGES]
    order = {ex: i for i, ex in enumerate(config.EXCHANGE_ORDER)}
    uni.sort(key=lambda x: (order.get(x[1]["ex"], 9), x[0]))
    return uni


# ---------------- 后台线程：原油每10秒刷新 ----------------

def oil_loop(state, interval):
    LOG.info("原油行情线程启动（每%d秒刷新 布伦特/纽约原油）", interval)
    while not state.stop.is_set():
        try:
            quotes = oil_data.fetch_oil_quotes()
            if quotes:
                state.oil.update(quotes)
                print(state.oil.snapshot_line(), flush=True)
                # 原油短时变化过大：置位事件，让主循环跳过等待立即出一轮建议
                jump = state.oil.detect_jump()
                if jump:
                    state.last_emergency = {"src": "oil", **jump}
                    state.kick.set()
                    n_aff = sum(1 for m in config.VARIETIES.values() if m.get("oil_w", 0) > 0)
                    LOG.warning("原油急动: %s 近%d秒 %+.2f%%（%.3f→%.3f），"
                                "立即对全部品种及其期权（含%d个能化联动品种）按实时数据重新分析，"
                                "原定轮动时间不推移",
                                jump["name"], jump["window_sec"], jump["ret"] * 100,
                                jump["base"], jump["price"], n_aff)
        except Exception as e:
            LOG.warning("原油行情获取失败: %s", e)
        state.stop.wait(interval)


# ---------------- 后台线程：全网数据查找每3分钟刷新（新闻/金融/突发事件） ----------------

def web_scan_loop(state, interval):
    LOG.info("全网扫描线程启动（新闻/金融/突发事件 每%d秒；真实优先、存疑后排）", interval)
    while not state.stop.is_set():
        try:
            n_new, trigger, items = state.webscan.refresh()
            if items:
                added = state.news.add(items)          # 进入统一新闻情绪池（内部再去重）
                report.append_daily_news(items)        # 当日新闻缓存（供每日复盘）
                try:
                    state.db.insert_news(items)        # P1：新闻结构化入库，支持后续检索/复盘
                except Exception:
                    LOG.warning("全网新闻结构化入库失败:\n%s", traceback.format_exc())
                LOG.info("全网扫描新增 %d 条（入池 %d 条）%s",
                         n_new, added, state.webscan.status_line())
            # 新出现的高影响消息/突发事件：与原油急动同样"插队"触发一轮全品种分析
            if trigger:
                state.last_emergency = {"src": "web", **trigger}
                state.kick.set()
                it = trigger["item"]
                LOG.warning("全网消息触发紧急轮动: 权重%+.2f %s｜%s｜影响品种: %s",
                            trigger["weight"], it["source"],
                            it["content"][:70], "、".join(trigger["varieties"][:8]) or "全板块")
        except Exception:
            LOG.error("全网扫描异常:\n%s", traceback.format_exc())
        state.stop.wait(interval)


# ---------------- 后台线程：日线指标预刷新 ----------------

def kline_loop(state):
    """后台预刷新日线指标（30分钟TTL），避免某一轮分析被刷新拉长"""
    while not state.stop.is_set():
        if state.stop.wait(60):
            return
        try:
            for key, meta in list(state.watchlist):
                state.klines.refresh_if_stale(meta["code"], meta["cat"])
                state.klines.refresh_intraday_if_stale(meta["code"], meta["cat"])
        except Exception as e:
            LOG.warning("日线/分钟指标预刷新失败: %s", e)


# ---------------- 后台线程：主力合约月份定期刷新 ----------------

def contract_loop(state, syms):
    """主力合约月份每30分钟后台重探（启动时已在主线程探测过一次）"""
    while not state.stop.is_set():
        if state.stop.wait(config.CONTRACT_TTL):
            return
        try:
            state.contracts.refresh(syms, exp_cal=state.webdata.calendar_snapshot())
        except Exception as e:
            LOG.warning("主力合约月份重探失败: %s", e)


# ---------------- 后台线程：基本面日频数据（库存/仓单时序 + 基差表） ----------------

def refresh_fundamentals(state, force=False):
    """并发拉取全品种库存/仓单时序与全市场基差表（日频，收盘后刷新一次；--once启动时force同步拉一次）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    if not force and state.fund_day == today:
        return
    try:
        state.fetcher.inventory_map(force=True)
        syms = [meta["sym"] for _, meta in state.watchlist]
        with ThreadPoolExecutor(max_workers=8) as ex:
            series = list(ex.map(state.fetcher.inventory_series, syms))
        state.fund_inv = {sym: ser for sym, ser in zip(syms, series) if ser}
        state.fund_basis = state.fetcher.basis_table()
        state.fund_day = today
        nb = "反爬不可用(已降级,由carry/库存/龙虎榜补位)" if state.fund_basis is None             else f"{len(state.fund_basis)}个品种"
        LOG.info("基本面日频数据刷新完成：库存/仓单时序 %d 个品种；基差表 %s",
                 len(state.fund_inv), nb)
    except Exception:
        LOG.warning("基本面日频数据刷新失败（不影响主监控）:\n%s", traceback.format_exc())


def fundamentals_loop(state):
    LOG.info("基本面线程启动（每日%d点后刷新库存/仓单+基差；龙虎榜按主力合约日缓存）", config.FUND_REFRESH_HOUR)
    while not state.stop.is_set():
        now = datetime.now()
        due = now.hour >= config.FUND_REFRESH_HOUR or now.weekday() >= 5
        if state.fund_day != now.strftime("%Y-%m-%d") and due:
            refresh_fundamentals(state)
        if state.stop.wait(1800):               # 每30分钟检查是否到刷新点
            return


# ---------------- 后台线程：主力合约分钟K常驻自采（第14轮 WP-D0） ----------------

def collect_minute_bars(state, mode="incr"):
    """多源分钟K并发采集并去重落 minute_bars：全周期(1/5/15/30/60m)优先新浪主连（无需主力合约即可采，
    主连代码直接给、历史窗口深，2026-09-01晚补测 type=1 一分钟K同样1023根），新浪失败时通达信/东财
    具体合约兜底（需 ContractCache 探测出主力，换月自动跟随）；通达信(tdx_minute)为可选冗余源，
    probe 通过才启用。
    mode: backfill=启动回填历史窗口；incr=常驻增量(只取最近几根)；once=--once/冷启动小回填。"""
    if mode == "backfill":
        periods, lmts = config.MINUTE_BACKFILL_PERIODS, config.MINUTE_BACKFILL_LMT
    elif mode == "once":
        periods, lmts = config.MINUTE_BACKFILL_PERIODS, config.MINUTE_ONCE_LMT
    else:
        periods, lmts = config.MINUTE_PERIODS, config.MINUTE_INCR_LMT
    # 通达信可选源：首轮采集前探测一次（公共服务器无期货/7727不可达时 available=False，之后零成本跳过）
    if state.tdx_minute is not None and getattr(state.tdx_minute, "available", None) is None:
        try:
            state.tdx_minute.probe()
        except Exception:
            state.tdx_minute.available = False
    jobs, n_var = [], 0
    for _key, meta in state.watchlist:
        n_var += 1
        cinfo = state.contracts.get(meta["sym"])
        mc = (cinfo or {}).get("main")
        yy, mm = (mc.get("yy"), mc.get("mm")) if mc else (None, None)
        for p in periods:
            # 新浪主连全周期(含1m)均可采、无需主力合约；仅当走tdx/东财具体合约兜底时才需要yy/mm
            jobs.append((meta["sym"], meta["ex"], meta.get("code"), yy, mm, p, int(lmts.get(p, 10))))

    def _one(job):
        sym, ex_, sina_code, yy, mm, p, lmt = job
        try:
            return state.minute_collector.collect(sym, ex_, sina_code, yy, mm, p, lmt)
        except Exception:
            return [], ""

    fetched = inserted = empty = 0
    src_stat = {}
    if jobs:
        with ThreadPoolExecutor(max_workers=config.MINUTE_WORKERS) as pool:
            for bars, src in pool.map(_one, jobs):
                key = src or "empty"
                src_stat[key] = src_stat.get(key, 0) + 1
                if not bars:
                    empty += 1
                    continue
                fetched += len(bars)
                try:
                    inserted += state.db.insert_minute_bars(bars)
                except Exception:
                    LOG.warning("分钟K入库失败（不影响主监控）:\n%s", traceback.format_exc())
    state.minute_collector.reset_stats()
    cov = state.db.minute_bars_coverage()
    cov_txt = "、".join(f"{p}m{v['bars']}根/{v['contracts']}合约" for p, v in sorted(cov.items()))
    src_txt = "、".join(f"{k}{v}任务" for k, v in sorted(src_stat.items()))
    LOG.info("分钟K自采(%s)：%d品种×%d周期=%d任务，拉取%d根、新增%d根、空/失败%d（源分布 %s）；库覆盖 %s",
             mode, n_var, len(periods), len(jobs), fetched, inserted, empty, src_txt or "无", cov_txt or "空")
    return {"tasks": len(jobs), "fetched": fetched, "inserted": inserted, "empty": empty}


def minute_bars_loop(state):
    """常驻分钟自采：启动先回填一次历史窗口，之后交易时段5分钟、非交易时段30分钟增量采集。
    长期看，免费源历史分钟窗口很短，这份由程序滚动自采的 minute_bars 才是日内/平今回测的根本数据。"""
    LOG.info("分钟K自采线程启动（先回填历史窗口，之后交易时段%ds/非交易%ds增量自采）",
             config.MINUTE_LOOP_INTERVAL, config.MINUTE_OFFPEAK_INTERVAL)
    try:
        collect_minute_bars(state, "backfill")
    except Exception:
        LOG.warning("分钟K启动回填失败:\n%s", traceback.format_exc())
    while not state.stop.is_set():
        trading, _ = is_trading_time()
        interval = config.MINUTE_LOOP_INTERVAL if trading else config.MINUTE_OFFPEAK_INTERVAL
        if state.stop.wait(interval):
            return
        try:
            collect_minute_bars(state, "incr")
        except Exception:
            LOG.warning("分钟K增量自采异常:\n%s", traceback.format_exc())


# ---------------- 启动时自动打开期货通 ----------------

def startup_open_ths():
    try:
        if ths_app.ensure_running():
            LOG.info("同花顺期货通已就绪（调试模式，独立 DevTools 窗口）")
        else:
            LOG.info("同花顺期货通未能自动打开（可检查 config.THS_EXE 路径）")
    except Exception as e:
        LOG.warning("自动打开期货通失败: %s", e)


def _cdp_port_busy(port):
    """探测本地 9222/9223 调试端口是否已在监听（避免重复拉起浏览器）。"""
    try:
        import socket
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


# 记录 main 自己拉起的调试浏览器（仅当本进程实际 Popen 了浏览器才非 None；
# 端口已占用跳过/用户手动开的浏览器不归本进程管理，退出时不做任何操作）。
_DEBUG_BROWSER_PID = None
_DEBUG_BROWSER_JOB = None


def _debug_browser_job(pid):
    """把调试浏览器进程挂入 Windows Job Object（KILL_ON_JOB_CLOSE）：
    main 进程退出（含被强杀/看板停止）时系统自动终止该浏览器进程树，杜绝孤儿残留。
    失败静默降级（仍有 finally/看门狗兜底 taskkill）。"""
    global _DEBUG_BROWSER_JOB
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                        ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong),
                        ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong),
                        ("OtherTransferCount", ctypes.c_ulonglong)]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", ctypes.c_ulong),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", ctypes.c_ulong),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", ctypes.c_ulong),
                        ("SchedulingClass", ctypes.c_ulong)]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info),
                                                ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        hproc = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not hproc:
            kernel32.CloseHandle(job)
            return
        ok = kernel32.AssignProcessToJobObject(job, hproc)
        kernel32.CloseHandle(hproc)
        if ok:
            _DEBUG_BROWSER_JOB = job  # 保持句柄打开，进程退出时 OS 自动关闭 job → 杀浏览器
    except Exception:
        _DEBUG_BROWSER_JOB = None


def close_debug_browser():
    """显式关闭 main 拉起的调试浏览器（进程树）；非本进程拉起的无操作。"""
    global _DEBUG_BROWSER_PID
    pid = _DEBUG_BROWSER_PID
    _DEBUG_BROWSER_PID = None
    if not pid:
        return
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                       timeout=15, capture_output=True)
        LOG.info("调试浏览器已随 main 退出关闭（PID %d）", pid)
    except Exception as e:
        LOG.warning("关闭调试浏览器失败（可手动关闭窗口）: %s", e)


def startup_browser_debug():
    """以调试端口(9222)自动拉起浏览器打开 OpenVlab市场页 + 交易可查首页，
    供 browser_reader 页面直读（需求⑦）使用；端口已占用则跳过、任何失败只告警。
    浏览器挂入 Job Object，main 退出时跟随关闭。"""
    global _DEBUG_BROWSER_PID
    try:
        for port in browser_reader.CDP_PORTS:
            if _cdp_port_busy(port):
                LOG.info("调试端口 %d 已在监听，跳过自动拉起调试浏览器", port)
                return
        edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        browser = edge if os.path.exists(edge) else (chrome if os.path.exists(chrome) else None)
        if not browser:
            LOG.info("未找到 Edge/Chrome，跳过自动拉起调试浏览器（可手动运行『打开行情网页(调试模式).bat』）")
            return
        url1 = browser_reader.OVL_URL
        url2 = browser_reader.JYKC_URL
        profile = os.path.join(os.environ.get("TEMP", "."), "ovl_jykc_profile")
        proc = subprocess.Popen([browser, "--remote-debugging-port=9222",
                                 f"--user-data-dir={profile}", url1, url2],
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _DEBUG_BROWSER_PID = proc.pid
        _debug_browser_job(proc.pid)  # 失败静默，兜底靠 finally/看门狗 taskkill
        # 短轮询确认调试端口就绪（最多 ~15s）
        import socket
        for _ in range(30):
            time.sleep(0.5)
            if _cdp_port_busy(9222):
                LOG.info("调试浏览器已就绪（9222），页面直读源打开：OpenVlab + 交易可查")
                return
        LOG.warning("调试浏览器启动后端口未在预期内就绪（不影响监控，breader 持续探测）")
    except Exception as e:
        LOG.warning("自动拉起调试浏览器失败: %s", e)


def open_realtime_report_once(state):
    """第一轮真实分析报告生成并写入实时HTML后，用系统默认浏览器打开一次（全程只开一次）。
    由 state.auto_open_report 控制（--no-launch 关闭、config.REPORT_AUTO_OPEN 总开关）；
    任何失败只告警、绝不影响监控主链路。"""
    if not getattr(state, "auto_open_report", False) or getattr(state, "report_opened", False):
        return
    if not getattr(config, "REPORT_AUTO_OPEN", True):
        state.report_opened = True
        return
    state.report_opened = True
    path = config.REALTIME_HTML
    try:
        if not os.path.exists(path):
            LOG.warning("实时报告尚未生成，跳过自动打开：%s", path)
            return
        if sys.platform.startswith("win"):
            os.startfile(path)          # Windows：用默认浏览器打开，调用本身不阻塞
        else:
            import webbrowser
            webbrowser.open("file:///" + path.replace("\\", "/"))
        LOG.info("已用默认浏览器打开实时报告：%s", path)
    except Exception:
        LOG.warning("自动打开实时报告失败（不影响监控）:\n%s", traceback.format_exc())


# ---------------- 工具 ----------------

# ---------------- 看门狗：主循环心跳与卡死自重启（P0-6） ----------------

def beat_heartbeat(state):
    """每轮分析（含紧急轮）开始时更新心跳；看门狗超时未更新即判定主循环卡死"""
    state.heartbeat_ts = time.time()
    try:
        with open(config.HEARTBEAT_FILE, "w", encoding="utf-8") as fp:
            fp.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 第{state.cycle}轮\n")
    except Exception:
        pass


def watchdog_loop(state):
    """后台监测主循环心跳：超过 HEARTBEAT_TIMEOUT_SEC 无更新则强制退出（退出码3），
    由 start_monitor.bat 的重试循环自动拉起，实现崩溃/卡死自重启。"""
    LOG.info("看门狗线程启动（主循环心跳超时 %d 秒即强制重启）", config.HEARTBEAT_TIMEOUT_SEC)
    while not state.stop.is_set():
        if state.stop.wait(config.WATCHDOG_CHECK_SEC):
            return
        gap = time.time() - state.heartbeat_ts
        if gap > config.HEARTBEAT_TIMEOUT_SEC:
            LOG.critical("主循环 %.0f 秒无响应（超过看门狗阈值 %d 秒），判定卡死，"
                         "强制退出以便外层自动重启", gap, config.HEARTBEAT_TIMEOUT_SEC)
            close_debug_browser()  # 卡死重启前也关闭调试浏览器，避免残留
            ths_app.kill_ths()     # 卡死重启前关闭本程序启动的同花顺，避免残留
            time.sleep(1)
            os._exit(3)


# ---------------- 主分析周期（每60秒） ----------------

# =========================== G13/G22 轻量调度（第91轮抽取，零主周期改动、行为不变） ===========================
def _maybe_review(state, fut_rows):
    """G13 LLM 第二意见调度（无 key 完全休眠；守护线程异步、只写独立 sidecar、绝不改综合分）。"""
    try:
        import llm_reviewer
        if llm_reviewer.enabled():
            _em = getattr(state, "last_emergency", None)
            threading.Thread(target=llm_reviewer.review_async,
                             args=(fut_rows, dict(_em) if _em else None),
                             kwargs={"force": bool(getattr(state, "llm_force", False))},
                             daemon=True).start()
    except Exception:
        LOG.error("G13 dispatch failed (swallowed)")


def _maybe_shadow(state):
    """G22续/G7续 影子信号跟随（每交易日首次周期+17:00后补当日；daemon 零阻塞、当日防重复）。"""
    try:
        import sys as _sys
        _tools_dir = os.path.join(config.BASE_DIR, "tools")
        if _tools_dir not in _sys.path:
            _sys.path.insert(0, _tools_dir)
        import shadow_track
        _now = datetime.now()
        _owner = trade_owner_date(_now).strftime("%Y-%m-%d")
        _seen = getattr(state, "shadow_seen_owner", None)
        _done = getattr(state, "shadow_done_owner", None)
        _slot = (_seen != _owner) or (_now.hour >= config.SHADOW_FOLLOW_HOUR)                 or getattr(state, "shadow_fail", False)
        _attempted = getattr(state, "shadow_attempt", None) == "%s|%s" % (_owner, _now.hour)
        if _slot and not _attempted and _done != _owner:
            state.shadow_seen_owner = _owner
            state.shadow_attempt = "%s|%s" % (_owner, _now.hour)
            state.shadow_fail = False

            def _shadow_daily_thread():
                try:
                    payload = shadow_track.daily(verbose=False)
                    state.shadow_done_owner = _owner
                    LOG.info("影子每日链完成: %s | 快照日 %s",
                             payload.get("logged"), payload.get("snapshot", {}).get("date"))
                except Exception:
                    state.shadow_fail = True
                    LOG.error("影子每日链异常（已吞掉）: %s", traceback.format_exc())

            state.shadow_thread = threading.Thread(target=_shadow_daily_thread, daemon=True)
            state.shadow_thread.start()
    except Exception:
        LOG.error("影子跟随调度失败（已吞掉）: %s", traceback.format_exc())


def _maybe_snapshot(state):
    """G14（第92轮）一档盘口快照采集（新浪主连5分钟级）：同步单批请求、5分钟节流、
    仅交易时段采集、失败全吞零阻塞；落 tick_snapshots 表 + 刷新 orderbook_stats 统计。"""
    try:
        import orderbook_snapshot
        res = orderbook_snapshot.collect_once(state.db)
        if res.get("stored"):
            LOG.info("G14 盘口快照已落库 %d 行", res["stored"])
    except Exception:
        LOG.warning("G14 盘口快照调度失败（已吞掉）: %s", traceback.format_exc())



def _maybe_newdata(state):
    """第96/97轮：新数据源跟随 main 每日采集（openvlab_map + jiaoyikecha）。
    用 checkpoint 按自然日防重（一天一次）、daemon 线程零阻塞、异常全吞。
    采集器自带幂等落库 + A1 探针；研究侧纪律：只采集落库，不进综合分。"""
    try:
        import checkpoint
        day = checkpoint.today_str()
        import threading as _th
        for stage, run in (("openvlab_map", _run_openvlab_map),
                           ("jiaoyikecha", _run_jiaoyikecha)):
            if checkpoint.done(day, stage):
                continue
            _th.Thread(target=run, kwargs={"day": day, "stage": stage},
                       daemon=True).start()
    except Exception:
        LOG.warning("新数据源调度失败（已吞掉）: %s", traceback.format_exc())


def _maybe_device_health(state):
    """B2（协同）：读界面操作收集装置 report_device.json，把装置各源健康状态
    合并进量化 data_health 体系（source 前缀 device_，只读、研究侧、不进综合分）。

    装置为独立进程（--daemon），状态文件由 fusion.StatusHub.save_report() 实时写。
    每轮轮询一次，文件缺失/解析失败静默降级——装置未启动不影响量化主链。"""
    try:
        dev_file = os.path.join(config.BASE_DIR, "reports", "device_status.txt")
        if not os.path.exists(dev_file):
            return 0
        import json as _json
        dev_json = os.path.join(config.BASE_DIR, "..", "..", "界面操作收集装置", "data", "report_device.json")
        dev_json = os.path.abspath(dev_json)
        if not os.path.exists(dev_json):
            # 兜底：量化 reports 目录下也可能由装置写入（fusion.save_report 双写）
            alt = os.path.join(config.BASE_DIR, "reports", "report_device.json")
            dev_json = alt if os.path.exists(alt) else None
        if not dev_json:
            return 0
        with open(dev_json, encoding="utf-8") as fh:
            rep = _json.load(fh)
        ts = str(rep.get("updated") or "")
        rows = []
        for name, s in (rep.get("sources") or {}).items():
            ok = bool(s.get("ok"))
            hits = int(s.get("hits", 0))
            rows.append({"source": "device_" + str(name)[:36],
                         "req": hits, "ok": 1 if ok else 0,
                         "fail": 0 if ok else max(1, hits),
                         "stale": 0, "jump": 0,
                         "latency_ms": 0,
                         "state": "closed" if ok else "open",
                         "note": "装置源"})
        # 软件在线状态也并入（legend/ths）
        for name, s in (rep.get("software") or {}).items():
            ok = bool(s.get("online"))
            rows.append({"source": "device_sw_" + str(name)[:34],
                         "req": 1, "ok": 1 if ok else 0, "fail": 0 if ok else 1,
                         "stale": 0, "jump": 0, "latency_ms": 0,
                         "state": "closed" if ok else "open",
                         "note": str(s.get("detail") or "")[:40]})
        if rows:
            n = state.db.insert_data_health(ts, rows)
            LOG.info("装置健康已并入 data_health: %d 行（source 前缀 device_）", n)
            return n
        return 0
    except Exception:
        LOG.debug("装置健康读取失败（装置未启动则属正常，已吞掉）: %s", traceback.format_exc())
        return 0


def _run_openvlab_map(day, stage):
    try:
        import sys as _sys
        _tools_dir = os.path.join(config.BASE_DIR, "tools")
        if _tools_dir not in _sys.path:
            _sys.path.insert(0, _tools_dir)
        import openvlab_map
        rows = openvlab_map.fetch_map()
        n = openvlab_map.store(rows=rows)
        checks = openvlab_map.cross_check(rows)
        openvlab_map.render(rows, checks)
        import checkpoint
        checkpoint.mark(day, stage)
        LOG.info("openvlab_map 每日采集: %d 品种 / 落库 %d / 交叉校验 %d", len(rows), n, len(checks))
    except Exception:
        LOG.warning("openvlab_map 每日采集失败（已吞掉，明日重试）: %s", traceback.format_exc())


def _run_jiaoyikecha(day, stage):
    try:
        import sys as _sys
        _tools_dir = os.path.join(config.BASE_DIR, "tools")
        if _tools_dir not in _sys.path:
            _sys.path.insert(0, _tools_dir)
        import jiaoyikecha_collector as jykt
        res = jykt.run(verbose=False)
        import checkpoint
        checkpoint.mark(day, stage)
        eps = {k: v["n"] for k, v in res["endpoints"].items()}
        LOG.info("jiaoyikecha 每日采集: 会话%s %s", "OK" if res["session_ok"] else "FAIL", eps)
    except Exception:
        LOG.warning("jiaoyikecha 每日采集失败（已吞掉，明日重试）: %s", traceback.format_exc())


def run_cycle(state):
    state.cycle += 1
    beat_heartbeat(state)
    state.rotation_desc = rotation_desc()
    tag = state.emergency_tag or ""
    tag = ("【%s】" % tag.strip("[]")) if tag else ""
    LOG.info("========== 第 %d 轮分析开始%s（%s）==========",
             state.cycle, tag, state.rotation_desc)

    # 1. 新闻（每轮抓取一次）
    news = sina_news.fetch_all_news()
    if news:
        added = state.news.add(news)
        report.append_daily_news(news)   # 当日新闻缓存（供每日复盘使用）
        try:
            state.db.insert_news(news)   # P1：新闻结构化入库
        except Exception:
            LOG.warning("新闻结构化入库失败（不影响本轮监控）:\n%s", traceback.format_exc())
        LOG.info("新闻抓取 %d 条（新增 %d 条，池内 %d 条）",
                 len(news), added, len(state.news.items))

    # 2. 全品种行情
    watchlist = list(state.watchlist)
    codes = sorted({meta["code"] for _, meta in watchlist})
    quotes = futures_data.fetch_quotes(codes)
    now_ts = time.time()
    for key, meta in watchlist:
        q = quotes.get(meta["code"])
        if q and q.get("latest"):
            state.var_hist.setdefault(key, deque(maxlen=240)).append((now_ts, q["latest"]))
    # P1：量仓资金因子需要相邻轮次快照；行情同时入 SQLite。
    flow_map = state.flow_tracker.update(quotes, now_ts)
    try:
        state.db.insert_quotes(state.cycle, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                               watchlist, quotes)
    except Exception:
        LOG.warning("行情结构化入库失败（不影响本轮监控）:\n%s", traceback.format_exc())

    # 2.5 G6 数据质量监控：缺数/陈旧/跳变体检 + 数据源熔断健康落 data_health 表 + 连续异常告警（只监控不改结果）
    if getattr(config, "DATA_HEALTH_ENABLED", True):
        try:
            _trading_now = is_trading_time()[0]
            health_res = state.health_monitor.observe_cycle(
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), quotes, codes,
                REGISTRY.snapshots(), today_str=datetime.now().strftime("%Y-%m-%d"),
                session_active=_trading_now)
            state.last_health = health_res
            state.db.insert_data_health(health_res["ts"], health_res["rows"])
            if health_res["alert_codes"]:
                state.alerts.emit("数据缺失提醒",
                    "以下品种连续%d轮无行情: %s" % (config.DATA_HEALTH_MISS_ALERT_CYCLES,
                    ",".join(health_res["alert_codes"][:20])),
                    level="info", key="dh_miss", cooldown=1800)
            _src_bad = sorted(set(health_res["alert_sources"]) | set(health_res["open_sources"]))
            if _src_bad:
                state.alerts.emit("数据源熔断提醒", "数据源异常/熔断: %s" % ",".join(_src_bad),
                    level="strong", key="dh_source", cooldown=1800)
        except Exception:
            LOG.warning("数据质量监控失败（不影响本轮监控）:\n%s", traceback.format_exc())

    # 3. 逐品种分析（含主力合约月份 + 机构动向因子 + 浏览器页面数据）
    #     共享入口与 paper_ticker 同一份评分路径（analyzer.analyze_all_varieties），
    #     保证纸面 ticker 每分钟独立信号与主报告开仓口径一致；只读共享缓存，不写报告侧。
    fut_rows = analyzer.analyze_all_varieties(state, watchlist, quotes, flow_map)
    state.last_forecasts = {row["name"]: row["forecast"]
                            for row in fut_rows if row.get("forecast")}

    # 3.9 第11/12轮：期权完整T链（多到期日）并发预热（30分钟缓存，命中零请求）：
    #     主力月份链挂 row["option_chain"]（持仓PCR/ATM/最大持仓行权价，保持第11轮行为）；
    #     最近N个真实挂牌月份的链组装 IV 曲面（微笑/skew/ATM期限/矩阵）挂 row["iv_surface"]。
    opt_cal = state.webdata.calendar_snapshot()
    chain_tasks, underlying_map, variety_expiries = [], {}, {}
    for row in fut_rows:
        if row["name"] not in config.OPTION_VARIETIES or row["price"] <= 0:
            continue
        sym = row["sym"]
        months = []
        cal_months = opt_cal.get(sym) or {}
        for yymm in sorted(cal_months):                    # OpenVlab 真实挂牌月份+到期日
            # 日历键为完整年月6位（202611），新浪T链pinzhong需两位年（2611）；(//100)%100 同时兼容4位
            yy, mm = (yymm // 100) % 100, yymm % 100
            exp_date = cal_months[yymm].get("exp_date")
            dleft = (exp_date - date.today()).days if exp_date \
                else contracts.estimate_option_days(yy, mm)
            if dleft < config.IV_SURFACE_MIN_DAYS:
                continue
            months.append((yy, mm, dleft))
        if not months:                                     # 日历缺失时回退到合约探测的期权月份
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
    chain_map = state.opt_chains.warm(chain_tasks, underlying_map=underlying_map)
    chain_rows_for_db, n_surface = [], 0
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
            chain_rows_for_db.append((row["name"], ch))   # 每月份一行快照，为曲面/PCR积累历史
        om = row.get("opt_month") or {}
        main_label = "%02d%02d" % (int(om["yy"]), int(om["mm"])) if om.get("yy") else None
        if main_label and main_label in chains_by_label:
            row["option_chain"] = chains_by_label[main_label]
        try:
            surf = iv_surface.build_surface(sym, row["ex"], row["price"],
                                            chains_by_label, days_map, main_label=main_label)
            if surf:
                row["iv_surface"] = surf
                n_surface += 1
        except Exception:
            LOG.debug("IV曲面构建失败 %s:\n%s", row["name"], traceback.format_exc())
    if chain_tasks:
        LOG.info("期权完整链就绪 %d 个月份/%d 个品种，IV曲面 %d 个品种（持仓PCR+T链反推IV口径）",
                 len(chain_rows_for_db), len(variety_expiries), n_surface)

    # 4. 期权严格分析（比期货更严格）
    opt_rows = []
    for row in fut_rows:
        if row["name"] not in config.OPTION_VARIETIES or row["price"] <= 0:
            continue
        try:
            opt_rows.append(option_analyzer.analyze_option(row["name"], row))
        except Exception as e:
            LOG.warning("期权分析失败 %s: %s", row["name"], e)

    # 4.5 期权组合策略推荐（只推荐期权策略，同样严格检查）
    strat_rows = []
    for row in fut_rows:
        if row["name"] not in config.OPTION_VARIETIES or row["price"] <= 0:
            continue
        try:
            s = option_strategies.recommend(row["name"], row)
            if s:
                s["variety"] = row["name"]
                strat_rows.append(s)
        except Exception as e:
            LOG.warning("期权策略推荐失败 %s: %s", row["name"], e)
    state.last_strat_rows = strat_rows

    # 4.7 WP-F1（P0）：独立风控闸门逐品种复核 + 横截面相对强弱。
    #     风控默认只标注/告警、不改综合分（RISK_GATE_AUTO_DOWNGRADE=False）；
    #     横截面只做横向比较，结果挂 state，报告新增"横截面强弱"块，均不回改 score/label。
    veto_names = []
    for row in fut_rows:
        try:
            risk_gate.apply_gate(row)
            if (row.get("risk") or {}).get("level") == "veto":
                veto_names.append("%s（%s）" % (row["name"], "；".join(row["risk"]["veto"])))
        except Exception:
            LOG.debug("风控闸门评估失败 %s:\n%s", row.get("name"), traceback.format_exc())
    if veto_names:
        LOG.warning("风控闸门否决 %d 个信号: %s", len(veto_names), "；".join(veto_names))
    try:
        state.last_cross_section = cross_section.rank(fut_rows)
    except Exception:
        state.last_cross_section = {}
        LOG.debug("横截面强弱计算失败:\n%s", traceback.format_exc())

    # 4.7b) WP-F2 A3：用自有DB历史结果构建胜率校准器，给每条信号挂"历史同类胜率"影子标注
    #      （只展示，不改综合分/信号/建议；portfolio --calibrate 才真正作用于手数）
    try:
        state.calibrator = signal_calibrator.SignalCalibrator(state.db)
        for _r in fut_rows:
            state.calibrator.annotate_row(_r)
    except Exception:
        LOG.debug("历史胜率校准失败（不影响本轮监控）:\n%s", traceback.format_exc())

    # 5. 信号效果追踪/结构化入库，再出报告（块头/报告头均标注轮动时间与节奏）
    cycle_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        state.db.update_signal_outcomes(quotes)
        state.db.insert_future_signals(state.cycle, cycle_time, fut_rows)
        state.db.insert_options(state.cycle, cycle_time, opt_rows, strat_rows, fut_rows)
        state.db.insert_option_chains(state.cycle, cycle_time, chain_rows_for_db)
        fund_db = [(r["name"], r["sym"], r.get("fundamental"))
                   for r in fut_rows if r.get("fundamental")]
        state.db.insert_fundamentals(cycle_time, fund_db)
        report.write_signal_tracking(state)
    except Exception:
        LOG.warning("信号追踪/结构化入库失败（不影响本轮监控）:\n%s", traceback.format_exc())

    # 5.5 G1（二）纸面交易：把纸面独立分析管线的综合分喂给影子经纪做虚拟委托/成交/盯市，
    #     并刷新 paper_account.txt。受 PAPER_ENABLED 开关控制（默认休眠）；独立成段、
    #     绝不回改 score/信号/建议与任何现有输出。
    #     第102轮：多账户（15个）循环期货 on_cycle + 期权 on_cycle_options，按 priority 分流。
    #     第114轮：纸面数据与主报告隔离——喂给纸面的是 paper_analysis 独立管线的产出
    #     （不展示、只作用纸面），不再复用主报告 fut_rows/strat_rows/opt_rows/chain_map。
    if getattr(state, "papers", None):
        # 纸面独立分析（最新行情 quotes 已在 run_cycle 开头拉取；期权链 TTL30分钟缓存命中
        # 零额外网络；与主报告分析完全隔离，只喂纸面账户、不写任何报告/DB/展示文件）。
        _pa = {}
        try:
            from paper_analysis import paper_analyze
            _pa = paper_analyze(state, quotes, watchlist)
        except Exception:
            LOG.warning("纸面独立分析失败（本轮纸面跳过，不影响主链）:\n%s", traceback.format_exc())
        _pa_fut = _pa.get("fut_rows") or []
        _pa_codes = _pa.get("codes") or codes
        _pa_strat = _pa.get("strat_rows") or []
        _pa_opt = _pa.get("opt_rows") or []
        _pa_chain = _pa.get("chain_map") or {}
        # 第103轮：run_cycle 末落只读快照，供 paper_ticker 线程交易时段分钟撮合用
        # （全部来自纸面独立管线，之后不再被本线程改动；ticker 失败时回退此快照）。
        state._paper_stash = {
            "fut_rows": list(_pa_fut),
            "codes": list(_pa_codes),
            "strat_rows": list(_pa_strat),
            "opt_rows": list(_pa_opt),
            "chain_map": dict(_pa_chain),
        }
        # 第107轮：非交易时段跳过纸面撮合（PAPER_TRADING_ONLY=True）
        # 交易时段内：执行 on_cycle + write_paper_account + 汇总打印
        # 非交易时段：快照照写（供 ticker 备用），撮合/报告/汇总全跳过（账户状态冻结）
        _trading_now, _ = is_trading_time()
        _skip_paper = (not _trading_now) and getattr(config, "PAPER_TRADING_ONLY", True)
        if not _skip_paper and _pa_fut:
            for _name, _broker in state.papers.items():
                try:
                    _prio = _broker.priority
                    # 期货撮合（option_only 档跳过期货；其余档位按正常逻辑）
                    if _prio != "option_only":
                        state.last_papers[_name] = _broker.on_cycle(cycle_time, _pa_fut, quotes)
                    else:
                        state.last_papers[_name] = _broker.last_summary or {}
                    # 期权撮合（所有账户都做；小资金档 priority 控制参与强度）
                    # 第110轮：额外传入 opt_rows（analyze_option 单腿信号），option_only/option_first 档启用
                    if (_pa_strat or _pa_opt) and _pa_chain:
                        _ols = _broker.on_cycle_options(cycle_time, _pa_strat, _pa_chain,
                                                        _pa_fut, opt_rows=_pa_opt if _pa_opt else None)
                        (state.last_papers.get(_name) or {})["opt"] = _ols
                except Exception:
                    LOG.warning("账户 %s 纸面撮合失败（不影响主链）: %s", _name, traceback.format_exc())
            # 向后兼容：state.paper/state.last_paper 指向第一个账户（基准）
            if state.papers:
                _first = list(state.papers.values())[0]
                state.last_paper = state.last_papers.get(_first.name) \
                    if getattr(_first, "name", None) in state.last_papers else _first.last_summary
            try:
                report.write_paper_account(state)
            except Exception:
                LOG.warning("纸面账户报告写入失败（不影响主链）:")
            # 输出汇总（按账户一行，简述期货+期权；第104轮统一资金池：snapshot.equity 已含期权）
            try:
                _parts = []
                for _name, _broker in state.papers.items():
                    _s = state.last_papers.get(_name) or {}
                    _ls = _s.get("snapshot") or {}
                    _parts.append("%s(权益%.2f/风险%.2f%%)" % (
                        _name, float(_ls.get("equity", 0.0)),
                        float(_ls.get("risk_degree", 0.0)) * 100.0))
                LOG.info("纸面账户本轮: %s", "  ".join(_parts)[:300])
            except Exception:
                pass

    news_top = state.news.top_items(k=8)
    text = report.render(state, fut_rows, opt_rows, strat_rows, news_top)
    print(text, flush=True)
    report.save(state, text, fut_rows, opt_rows)
    # 6.4 G13 LLM 第二意见 + 6.5 影子信号跟随 + 6.6 G14 一档盘口快照 + 6.7 A1 解析健康探针
    _maybe_review(state, fut_rows)
    _maybe_shadow(state)
    _maybe_snapshot(state)
    _maybe_newdata(state)
    _maybe_device_health(state)
    try:
        import parser_health
        parser_health.emit_health_alerts(state)
    except Exception:
        pass
    state.alerts.observe_cycle(state, fut_rows, strat_rows)
    LOG.info("第 %d 轮分析完成，报告已保存到 %s | %s | %s",
             state.cycle, config.REPORT_FILE,
             state.webdata.status_line(), state.breader.status_line())
    # 第一轮真实报告生成并刷新实时HTML后，自动用默认浏览器打开一次（--no-launch/配置可关）
    if state.cycle == 1:
        open_realtime_report_once(state)

    # 6. 每日复盘：归属交易日的全部交易结束后生成一次
    #    （有夜盘→次日02:30后；该交易日无夜盘→当日15:00后；--force-review 立即生成）
    now_dt = datetime.now()
    owner = trade_owner_date(now_dt)
    owner_s = owner.strftime("%Y-%m-%d")
    if (review_is_due(owner, now_dt) or getattr(state, "force_review", False)) \
            and state.review_date != owner_s:
        try:
            review_text = report.build_daily_review(state, owner)
            report.write_daily_review(review_text, owner)
            state.review_date = owner_s
            LOG.info("交易日 %s 复盘报告已生成 → %s", owner_s, config.DAILY_REVIEW_FILE)
        except Exception:
            LOG.error("复盘报告生成失败:\n%s", traceback.format_exc())


# ---------------- 入口 ----------------

def _build_emergency(state, nxt):
    """根据 state.last_emergency 生成 (块头标记, 正文说明)，兼容原油急动/全网消息两类"""
    em = state.last_emergency or {}
    src = em.get("src")
    when = nxt.strftime("%H:%M")
    if src == "oil":
        tag = "[原油急动紧急轮动]"
        note = (
            "原油急动紧急轮动：%s 近%d秒 %+.2f%%（%.3f→%.3f），立即按实时数据重算全部品种及期权建议"
            "（全部报告同步写入、看板同步刷新）；原定%s的计划轮动时间不变，继续等待"
            % (em.get("name", ""), em.get("window_sec", 0), em.get("ret", 0) * 100,
               em.get("base", 0.0), em.get("price", 0.0), when))
    else:
        tag = "[全网消息紧急轮动]"
        it = em.get("item", {})
        src_name = it.get("source", "")
        doubt = "（存疑消息，已降权后排）" if it.get("doubtful") else ""
        kind = "突发事件" if em.get("breaking") else "高影响消息"
        aff = "、".join(em.get("varieties", [])[:10]) or "全板块"
        note = (
            "全网扫描紧急轮动（%s）：%s『%s』%s 影响权重%+.2f，直接影响品种：%s；"
            "立即按实时数据重算全部品种及期权建议（全部报告同步写入、看板同步刷新）；"
            "原定%s的计划轮动时间不变，继续等待"
            % (kind, src_name, it.get("content", "")[:90], doubt,
               em.get("weight", 0.0), aff, when))
    return tag, note


def wait_with_emergency(state, nxt):
    """等待到计划轮动时刻 nxt：期间若出现原油急动或全网高影响消息（state.kick），
    立即"插队"跑一整轮全品种实时分析（全部报告同步写入、看板状态同步更新），
    但 **nxt 计划点不重算、不推移**，紧急轮结束后继续等待同一个 nxt（正常轮动时间接着计算）。
    同时做**交易时段边沿检测**：非交易→交易（开盘）或交易→非交易（收盘/午休）的翻转点一到，
    立即返回 "edge"，由主循环马上跑一轮并按新时段节奏（交易=5/10分钟、非交易=1分钟）重排计划。
    返回值：True=已到计划时刻；"edge"=时段切换需立即轮动；False=收到停止信号。"""
    enter_trading = is_trading_time()[0]      # 进入等待时的交易状态，作为边沿比对基线
    while not state.stop.is_set():
        now = datetime.now()
        # 同时等待"计划点 nxt"与"下一个时段翻转点"，谁先到先醒（翻转点缺失时只等 nxt）
        waits = [(nxt - now).total_seconds()]
        trans = next_transition(now)
        if trans:
            waits.append((trans - now).total_seconds())
        remaining = min(waits)
        wait = remaining if remaining > 0 else 1.0       # 已过时兜底1秒快速重估，不空转
        kicked = state.kick.wait(wait)
        if state.stop.is_set():
            return False
        if kicked:
            # ---- 紧急触发（原油急动 / 全网消息）：插队一轮全品种分析，nxt 保持不变 ----
            state.kick.clear()
            state.emergency_tag, state.emergency_note = _build_emergency(state, nxt)
            LOG.warning(state.emergency_note)
            try:
                run_cycle(state)
            except Exception:
                LOG.error("紧急轮分析异常:\n%s", traceback.format_exc())
            # 紧急轮可能恰好跨过开盘/收盘点，刷新基线避免紧接着误判一次边沿
            enter_trading = is_trading_time()[0]
            continue
        # ---- 等待超时（未被紧急事件唤醒）：先判时段翻转，再判是否到达计划点 ----
        now = datetime.now()
        now_trading = is_trading_time(now)[0]
        if now_trading != enter_trading:
            return "edge"                    # 开盘/收盘瞬间：立即回主循环跑一轮，再按新节奏重排
        if (nxt - now).total_seconds() <= 1.0:
            return True                      # 到达计划时刻，回主循环跑定时轮
        # 距计划点尚有时间、状态也未翻转（翻转点秒级抖动提前醒来）：继续等待
    return False


def _paper_single_instance():
    """纸面账户单实例锁（第112轮）：防止多个 main 进程同时撮合同一套 paper_accounts 库
    （如常驻 daemon 与 --once 并发）导致开平记录叠加、pos_ref 冲突的错账。
    Windows msvcrt 文件锁随进程退出（含崩溃）自动释放，不留死锁。
    返回文件句柄（持有锁）或 None（未抢到锁，调用方应禁用纸面撮合）。"""
    import msvcrt
    try:
        lock_dir = os.path.join(config.BASE_DIR, "data", "paper_accounts")
        os.makedirs(lock_dir, exist_ok=True)
        f = open(os.path.join(lock_dir, ".main.lock"), "a+")
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return f
        except OSError:
            f.close()
            return None
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="期货全品种监控分析")
    parser.add_argument("--once", action="store_true", help="只跑一轮分析后退出")
    parser.add_argument("--no-launch", action="store_true",
                        help="不自动打开同花顺期货通，也不自动弹出首轮实时报告HTML")
    parser.add_argument("--oil-interval", type=int, default=config.OIL_INTERVAL)
    parser.add_argument("--news-interval", type=int, default=config.REPORT_INTERVAL,
                        help="（兼容保留）非交易时段分析周期；交易时段轮动节奏自动接管")
    parser.add_argument("--force-review", action="store_true",
                        help="立即生成当日复盘报告（测试用）")
    parser.add_argument("--llm-force", action="store_true",
                        help="G13：本轮强制触发一次 LLM 第二意见（无自然触发器也复核 top-3；测试/自检用）")
    parser.add_argument("--version", action="store_true",
                        help="打印版本号（读 VERSION）后退出，不启动监控（G19）")
    args = parser.parse_args()

    if getattr(args, "version", False):
        # G19：只读打印版本即退出，不初始化环境/不连库/不启动常驻，默认行为完全不变
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"),
                      "r", encoding="utf-8-sig") as _vf:
                print(_vf.read().strip())
        except OSError:
            print("unknown")
        return 0

    setup_environment()
    LOG.info(trade_calendar.status_line())
    # 第112轮：纸面账户单实例锁——防多个 main 进程并发撮合同一套账户库导致错账；
    # 抢不到锁（已有 main 在撮合）则本次运行禁用纸面，避免叠加开平记录
    paper_lock = None
    if getattr(config, "PAPER_ENABLED", False):
        paper_lock = _paper_single_instance()
        if paper_lock is None:
            LOG.warning("检测到另一 main 进程正在撮合纸面账户，本次运行禁用纸面撮合（防错账）")
            config.PAPER_ENABLED = False
    universe = build_universe()
    counts = {}
    for _, meta in universe:
        counts[meta["ex"]] = counts.get(meta["ex"], 0) + 1
    note = "、".join(f"{config.EXCHANGE_NAMES[ex]}{counts.get(ex, 0)}个"
                     for ex in config.EXCHANGE_ORDER)
    print("=" * 96)
    print(" 期货全品种监控分析  (上期所/上期能源/大商所/郑商所/广期所 全品种+期权 | 原油10s | 消息60s)")
    print(f" 品种覆盖 {len(universe)} 个（{note}） | 报告: {config.REPORT_FILE}")
    print(" 启动时将自动以调试模式打开同花顺期货通（独立 DevTools 窗口）；购买建议自动标注主力合约月份与期权月份")
    print("=" * 96, flush=True)

    state = State(universe)
    state.universe_note = f"共{len(universe)}个品种（{note}）"
    state.force_review = args.force_review
    state.llm_force = args.llm_force
    # --no-launch 同时抑制"同花顺自动打开"与"首轮报告浏览器自动打开"（测试/无界面场景）
    state.auto_open_report = not args.no_launch

    # 新的一天首次运行：清除五个轮动文件中昨日的轮动报告
    report.daily_rollover()
    # 先生成实时看板（即使第一轮报告还没出，浏览器打开也能看到文件结构）
    report.write_dashboard()
    report.write_status(state, is_trading_time()[0])
    # P1-3：首份图表数据（组合CSV/因子JSON/SQLite 已可画；横截面/校准等首轮内存态补齐）
    try:
        import charts
        charts.write_chart_data(state)
    except Exception as _e:
        LOG.warning("首份图表数据生成失败: %s", _e)

    # 启动时先取一次外部数据（交易可查机构观点 + OpenVlab期权日历，约2~5秒）
    LOG.info("正在获取外部数据（交易可查机构观点/OpenVlab期权日历）...")
    try:
        state.webdata._fetch_once()
    except Exception as e:
        LOG.warning("外部数据首次获取失败: %s", e)

    # 启动时先探测一轮主力合约月份（约20~40秒，保证第一份报告就带具体合约）
    syms = [meta["sym"] for _, meta in universe]
    LOG.info("正在探测 %d 个品种的主力合约月份（约20~40秒）...", len(syms))
    try:
        state.contracts.refresh(syms, exp_cal=state.webdata.calendar_snapshot())
    except Exception as e:
        LOG.warning("主力合约月份首次探测失败: %s", e)

    # 第13轮：启动同步拉取一次基本面日频数据（东财库存/仓单时序+生意社基差，并发约10~25秒）
    LOG.info("正在获取基本面日频数据（东财库存/仓单时序 + 生意社基差表）...")
    refresh_fundamentals(state, force=True)

    # 第14轮 WP-D0：启动同步做一次主力合约分钟K小回填（首轮即有自有分钟数据；常驻模式另由后台线程持续自采）
    LOG.info("正在采集分钟K线（新浪主连全周期1/5/15/30/60m为主，启动小回填；通达信/东财兜底，失败自动降级）...")
    try:
        collect_minute_bars(state, "once")
    except Exception as e:
        LOG.warning("启动分钟K采集失败（不影响主监控）: %s", e)

    threading.Thread(target=oil_loop, args=(state, max(args.oil_interval, 3)),
                     daemon=True).start()
    threading.Thread(target=web_scan_loop, args=(state, config.WEB_SCAN_INTERVAL),
                     daemon=True).start()
    threading.Thread(target=state.webdata.loop, daemon=True).start()
    threading.Thread(target=state.breader.loop, daemon=True).start()
    threading.Thread(target=kline_loop, args=(state,), daemon=True).start()
    threading.Thread(target=contract_loop, args=(state, syms), daemon=True).start()
    threading.Thread(target=fundamentals_loop, args=(state,), daemon=True).start()
    if not args.once:
        threading.Thread(target=minute_bars_loop, args=(state,), daemon=True).start()
    threading.Thread(target=watchdog_loop, args=(state,), daemon=True).start()
    # 第103轮：纸面撮合独立 ticker 线程（交易时段每分钟撮合；--once/PAPER关闭/间隔0 均不启动，
    # 间隔0=完全回退 run_cycle 同步驱动=旧行为）
    if not args.once and getattr(config, "PAPER_ENABLED", False) and \
            getattr(config, "PAPER_TICK_INTERVAL", 0) > 0:
        threading.Thread(target=paper_ticker.tick_loop, args=(state,), daemon=True).start()
    if not args.no_launch and config.THS_AUTO_LAUNCH:
        threading.Thread(target=startup_open_ths, daemon=True).start()
    if not args.no_launch and getattr(config, "BROWSER_DEBUG_LAUNCH", True):
        threading.Thread(target=startup_browser_debug, daemon=True).start()

    try:
        while True:
            # 定时轮（到达计划时刻）：非紧急，全品种按实时数据完整分析
            state.emergency_note = ""
            state.emergency_tag = ""
            try:
                run_cycle(state)
            except Exception:
                LOG.error("本轮分析异常:\n%s", traceback.format_exc())
            if args.once:
                state.stop.set()
                # G13：--once 退出会杀掉 daemon 的 LLM 复核线程，退出前有界等待其完成
                try:
                    import llm_reviewer
                    llm_reviewer.wait_last(timeout=90)
                except Exception:
                    pass
                # G22续：--once 退出前同样有界等待影子链线程（防 daemon 被杀）
                _st = getattr(state, "shadow_thread", None)
                if _st is not None and _st.is_alive():
                    _st.join(timeout=300)
                break
            # 计划下一轮时刻只计算一次；等待期间原油急动/全网高影响消息可"插队"出紧急轮，但该时刻不重算、不推移
            nxt = next_cycle_time(datetime.now())
            LOG.info("下一轮计划时间 %s（%.0f秒后；期间原油急动或全网消息会插入紧急轮，但不推移该时间）",
                     nxt.strftime("%H:%M:%S"), (nxt - datetime.now()).total_seconds())
            reason = wait_with_emergency(state, nxt)
            if not reason:                    # False=收到停止信号
                break
            if reason == "edge":              # 交易/非交易时段切换：立即回循环顶部跑一轮，再按新时段节奏重排
                trading_now, sess_desc = is_trading_time()
                if trading_now:
                    LOG.warning("检测到进入交易时段（%s）：立即开始交易时段轮动；本轮之后按该时段节奏"
                                "（开盘前30分钟每5分钟、其后每10分钟）排程", sess_desc)
                else:
                    LOG.warning("检测到交易时段结束（%s）：立即轮动一轮，随后切换为非交易时段节奏"
                                "（每%d秒一轮）", sess_desc, config.REPORT_INTERVAL)
                continue
    except KeyboardInterrupt:
        print("\n收到退出指令，正在停止...", flush=True)
    finally:
        state.stop.set()
        state.kick.set()          # 唤醒主循环等待，快速退出
        try:
            state.db.close()
        except Exception:
            pass
        close_debug_browser()     # 正常退出/KeyboardInterrupt/--once 时关闭调试浏览器
        ths_app.kill_ths()        # 正常退出时联动关闭本程序启动的同花顺
        if paper_lock is not None:      # 释放纸面单实例锁（进程退出/崩溃时系统也会自动释放）
            try:
                paper_lock.close()
            except Exception:
                pass
        LOG.info("程序退出")


if __name__ == "__main__":
    main()
