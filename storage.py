# -*- coding: utf-8 -*-
"""P1-8/9 SQLite 结构化存储与信号效果追踪。

零新增依赖（Python 标准库 sqlite3），承担四类长期数据：
- quotes：逐轮行情快照（价格、涨跌幅、成交量、持仓量）；
- signals：非中性期货信号的综合分、因子拆分、止损目标、量仓状态；
- news：新闻、关键词原始权重、可信度、存疑标记；
- options：单腿期权与组合策略、IV/Greeks/检查项/腿结构。

signal_outcomes 记录可交易信号在 30分钟/2小时/次日 三个周期后的实际方向收益，
供 reports/signal_tracking.txt 与每日复盘统计胜率、平均收益和多空命中率。
"""
import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta

import config
from utils import is_variety_trading

# 行情代码 -> 品种元数据，用于信号到期时判断该品种自身是否仍在交易
_CODE_TO_META = {m.get("code"): m for m in config.VARIETIES.values()}


def _dt(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return datetime.now()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return datetime.now()


def _json(value):
    """JSON 序列化，兼容 datetime/tuple；失败时保留字符串，不允许拖垮主循环。"""
    def default(obj):
        if isinstance(obj, datetime):
            return obj.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(obj, (set, tuple)):
            return list(obj)
        return str(obj)
    try:
        return json.dumps(value, ensure_ascii=False, default=default)
    except Exception:
        return json.dumps(str(value), ensure_ascii=False)


def score_band_name(score):
    s = abs(score)
    if s < config.SCORE_NEUTRAL:
        return "观望"
    if s < config.SCORE_LIGHT:
        return "轻仓"
    if s < config.SCORE_MID:
        return "分批"
    return "强信号"


class MonitorDB:
    """线程安全的 SQLite 封装；主循环与后台新闻线程都可调用。"""

    def __init__(self, path=None):
        self.path = path or config.MONITOR_DB
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        # 非交易时段接口会持续返回同一份快照；用内存签名跳过重复行情，避免数据库在周末空转膨胀。
        self._last_quote_sig = {}
        self._init_schema()
        self.prune()

    def close(self):
        with self.lock:
            self.conn.close()

    def _init_schema(self):
        with self.lock:
            cur = self.conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS quotes(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, cycle INTEGER,
                    variety TEXT, code TEXT, sym TEXT, exchange TEXT, cat TEXT,
                    price REAL, chg_pct REAL, open REAL, high REAL, low REAL,
                    prev_settle REAL, volume REAL, open_interest REAL,
                    created_real REAL
                );
                CREATE INDEX IF NOT EXISTS idx_quotes_ts_code ON quotes(ts, code);
                CREATE INDEX IF NOT EXISTS idx_quotes_variety ON quotes(variety, created_real);

                CREATE TABLE IF NOT EXISTS signals(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, cycle INTEGER,
                    variety TEXT, code TEXT, sym TEXT, exchange TEXT, cat TEXT,
                    price REAL, chg_pct REAL, score REAL,
                    direction TEXT, direction_int INTEGER, label TEXT,
                    score_band TEXT, advice TEXT, stop REAL, target REAL, atr REAL,
                    contract_code TEXT, main_month TEXT,
                    volume REAL, open_interest REAL,
                    parts_json TEXT, flow_json TEXT, raw_json TEXT,
                    created_real REAL
                );
                CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
                CREATE INDEX IF NOT EXISTS idx_signals_variety ON signals(variety, created_real);

                CREATE TABLE IF NOT EXISTS news(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, source TEXT, content TEXT,
                    weight REAL, confidence REAL, important INTEGER, doubtful INTEGER,
                    content_hash TEXT, raw_json TEXT, created_real REAL,
                    UNIQUE(content_hash, source, ts)
                );
                CREATE INDEX IF NOT EXISTS idx_news_ts ON news(ts);

                CREATE TABLE IF NOT EXISTS options(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, cycle INTEGER, record_type TEXT,
                    variety TEXT, strategy_name TEXT, direction TEXT, score REAL,
                    underlying_price REAL, iv REAL, iv_ratio REAL,
                    delta REAL, gamma REAL, vega REAL, theta_day REAL,
                    prem REAL, net REAL, max_profit REAL, max_loss REAL,
                    contract TEXT, verdict TEXT, all_pass INTEGER,
                    checks_json TEXT, legs_json TEXT, greeks_json TEXT,
                    raw_json TEXT, created_real REAL
                );
                CREATE INDEX IF NOT EXISTS idx_options_ts ON options(ts);
                CREATE INDEX IF NOT EXISTS idx_options_variety ON options(variety, created_real);

                CREATE TABLE IF NOT EXISTS signal_outcomes(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id INTEGER NOT NULL,
                    variety TEXT, code TEXT, horizon_min INTEGER,
                    direction TEXT, direction_int INTEGER, score REAL, score_band TEXT,
                    entry_ts TEXT, entry_price REAL, due_ts TEXT,
                    eval_ts TEXT, exit_price REAL, ret REAL, hit INTEGER,
                    status TEXT DEFAULT 'pending', raw_json TEXT, created_real REAL,
                    UNIQUE(signal_id, horizon_min),
                    FOREIGN KEY(signal_id) REFERENCES signals(id)
                );
                CREATE INDEX IF NOT EXISTS idx_outcomes_status_due ON signal_outcomes(status, due_ts);
                CREATE INDEX IF NOT EXISTS idx_outcomes_eval ON signal_outcomes(eval_ts);

                CREATE TABLE IF NOT EXISTS option_chains(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, cycle INTEGER, sym TEXT, variety TEXT, expiry TEXT,
                    n_call INTEGER, n_put INTEGER, call_oi REAL, put_oi REAL, pcr_oi REAL,
                    atm_strike REAL, max_call_strike REAL, max_put_strike REAL,
                    raw_json TEXT, created_real REAL,
                    UNIQUE(sym, expiry, ts)
                );
                CREATE INDEX IF NOT EXISTS idx_chains_sym ON option_chains(sym, created_real);

                CREATE TABLE IF NOT EXISTS fundamentals(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, trade_date TEXT, sym TEXT, variety TEXT,
                    inventory REAL, inv_pct REAL, inv_wow REAL, inv_n INTEGER,
                    rank_long REAL, rank_short REAL, rank_net REAL, rank_delta REAL,
                    carry REAL, basis_rate REAL, fund_score REAL,
                    raw_json TEXT, created_real REAL,
                    UNIQUE(sym, trade_date)
                );
                CREATE INDEX IF NOT EXISTS idx_fund_sym ON fundamentals(sym, created_real);

                CREATE TABLE IF NOT EXISTS minute_bars(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sym TEXT, contract TEXT, exchange TEXT, period INTEGER,
                    bar_dt TEXT NOT NULL, trade_date TEXT,
                    o REAL, h REAL, l REAL, c REAL, v REAL, amount REAL,
                    created_real REAL,
                    UNIQUE(contract, period, bar_dt)
                );
                CREATE INDEX IF NOT EXISTS idx_mb_sym ON minute_bars(sym, period, bar_dt);
                CREATE INDEX IF NOT EXISTS idx_mb_date ON minute_bars(trade_date);

                CREATE TABLE IF NOT EXISTS ml_samples(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sym TEXT, variety TEXT, period INTEGER,
                    bar_dt TEXT NOT NULL, trade_date TEXT, direction INTEGER,
                    entry_price REAL, atr REAL, tp_price REAL, sl_price REAL,
                    exit_dt TEXT, exit_price REAL, label INTEGER, exit_reason TEXT,
                    bars_held INTEGER, ret_dir REAL, tech_score REAL,
                    features_json TEXT, created_real REAL,
                    UNIQUE(sym, period, bar_dt)
                );
                CREATE INDEX IF NOT EXISTS idx_ml_sym ON ml_samples(sym, period, bar_dt);
                CREATE INDEX IF NOT EXISTS idx_ml_label ON ml_samples(label);
                CREATE INDEX IF NOT EXISTS idx_ml_date ON ml_samples(trade_date);

                CREATE TABLE IF NOT EXISTS data_health(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, source TEXT NOT NULL,
                    req INTEGER DEFAULT 0, ok INTEGER DEFAULT 0, fail INTEGER DEFAULT 0,
                    stale INTEGER DEFAULT 0, jump INTEGER DEFAULT 0,
                    latency_ms REAL, state TEXT, note TEXT, created_real REAL,
                    UNIQUE(ts, source)
                );
                CREATE INDEX IF NOT EXISTS idx_dh_ts ON data_health(ts);
                CREATE INDEX IF NOT EXISTS idx_dh_source ON data_health(source, created_real);

                CREATE TABLE IF NOT EXISTS backtest_runs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_ts TEXT NOT NULL, kind TEXT NOT NULL,
                    fill_mode TEXT, cost_mode TEXT,
                    n_symbols INTEGER DEFAULT 0, n_trades INTEGER DEFAULT 0,
                    sample_days INTEGER DEFAULT 0,
                    params_json TEXT, metrics_json TEXT,
                    cumulative REAL, max_dd REAL, sharpe REAL, win_rate REAL,
                    created_real REAL
                );
                CREATE INDEX IF NOT EXISTS idx_btr_kind ON backtest_runs(kind, created_real);

                CREATE TABLE IF NOT EXISTS paper_orders(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, sym TEXT, name TEXT, sector TEXT,
                    action TEXT, side TEXT, direction INTEGER,
                    lots INTEGER DEFAULT 0, signal_price REAL,
                    score REAL, band TEXT, fill_mode TEXT, status TEXT,
                    fill_ts TEXT, fill_price REAL, raw_price REAL,
                    reason TEXT, order_ref TEXT, pos_ref TEXT,
                    raw_json TEXT, created_real REAL
                );
                CREATE INDEX IF NOT EXISTS idx_po_sym ON paper_orders(sym, created_real);
                CREATE INDEX IF NOT EXISTS idx_po_status ON paper_orders(status, created_real);

                CREATE TABLE IF NOT EXISTS paper_trades(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, pos_ref TEXT, sym TEXT, name TEXT, sector TEXT,
                    side TEXT, dir_text TEXT, direction INTEGER,
                    lots INTEGER, price REAL, raw_price REAL, notional REAL,
                    slip_yuan REAL DEFAULT 0, fee_yuan REAL DEFAULT 0, realized_yuan REAL DEFAULT 0,
                    leg TEXT, reason TEXT, forced INTEGER DEFAULT 0,
                    order_id INTEGER, entry_ts TEXT, entry_price REAL,
                    score REAL, margin_rate REAL, created_real REAL
                );
                CREATE INDEX IF NOT EXISTS idx_pt_sym ON paper_trades(sym, created_real);
                CREATE INDEX IF NOT EXISTS idx_pt_pos ON paper_trades(pos_ref);

                CREATE TABLE IF NOT EXISTS paper_equity(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL UNIQUE,
                    static_equity REAL, float_pnl REAL, equity REAL,
                    margin_used REAL, available REAL, risk_degree REAL, drawdown REAL,
                    n_positions INTEGER, realized REAL, fees_paid REAL, n_trades INTEGER,
                    positions_json TEXT, created_real REAL
                );
                CREATE INDEX IF NOT EXISTS idx_pe_ts ON paper_equity(created_real);

                CREATE TABLE IF NOT EXISTS tick_snapshots(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sym TEXT NOT NULL, variety TEXT,
                    bucket TEXT NOT NULL,
                    quote_date TEXT, quote_time TEXT,
                    collected_at TEXT,
                    bid REAL, ask REAL, latest REAL,
                    bid_vol REAL, ask_vol REAL,
                    spread REAL, spread_bp REAL,
                    prev_settle REAL, oi REAL, volume REAL,
                    created_real REAL,
                    UNIQUE(sym, bucket)
                );
                CREATE INDEX IF NOT EXISTS idx_ts_sym ON tick_snapshots(sym, created_real);
                """
            )
            self.conn.commit()

    # ---------------- 写入：行情 ----------------

    def insert_quotes(self, cycle, ts, watchlist, quotes):
        rows = []
        row_sigs = {}
        now_real = datetime.now().timestamp()
        for name, meta in watchlist:
            code = meta.get("code")
            q = quotes.get(code) or {}
            price = float(q.get("latest") or 0.0)
            if price <= 0:
                continue
            volume = float(q.get("volume") or 0.0)
            open_interest = float(q.get("open_interest") or 0.0)
            sig = (price, round(float(q.get("chg_pct") or 0.0), 8), volume, open_interest)
            if self._last_quote_sig.get(code) == sig:
                continue
            row_sigs[code] = sig
            rows.append((ts, cycle, name, code, meta.get("sym"),
                         meta.get("ex"), meta.get("cat"), price,
                         float(q.get("chg_pct") or 0.0), float(q.get("open") or 0.0),
                         float(q.get("high") or 0.0), float(q.get("low") or 0.0),
                         float(q.get("prev_settle") or 0.0),
                         volume, open_interest, now_real))
        if not rows:
            return 0
        with self.lock:
            self.conn.executemany(
                """INSERT INTO quotes(ts,cycle,variety,code,sym,exchange,cat,price,chg_pct,
                   open,high,low,prev_settle,volume,open_interest,created_real)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
            self.conn.commit()
        self._last_quote_sig.update(row_sigs)
        return len(rows)

    # ---------------- 写入：新闻 ----------------

    def insert_news(self, items):
        if not items:
            return 0
        # 延迟导入，避免 storage <-> factors 初始化循环
        import factors
        n = 0
        now_real = datetime.now().timestamp()
        with self.lock:
            for item in items:
                content = item.get("content") or ""
                if not content:
                    continue
                dt = _dt(item.get("time"))
                weight = factors._lex_weight(content, None)
                h = hashlib.md5((item.get("source", "") + "|" + content[:200]).encode("utf-8")).hexdigest()
                try:
                    cur = self.conn.execute(
                        """INSERT OR IGNORE INTO news(ts,source,content,weight,confidence,
                           important,doubtful,content_hash,raw_json,created_real)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (dt.strftime("%Y-%m-%d %H:%M:%S"), item.get("source", ""), content,
                         float(weight), float(item.get("confidence", 1.0)),
                         1 if item.get("important") else 0,
                         1 if item.get("doubtful") else 0, h, _json(item), now_real))
                    n += int(cur.rowcount or 0)
                except sqlite3.Error:
                    continue
            self.conn.commit()
        return n

    # ---------------- 写入：期货信号 + 后续追踪任务 ----------------

    @staticmethod
    def _direction(score):
        if score > 0:
            return "做多", 1
        if score < 0:
            return "做空", -1
        return "震荡", 0

    def insert_future_signals(self, cycle, ts, fut_rows):
        if not fut_rows:
            return 0
        dt = _dt(ts)
        now_real = datetime.now().timestamp()
        n = 0
        with self.lock:
            for r in fut_rows:
                score = float(r.get("score", 0.0))
                # signals 表保存“可交易信号”；中性行每分钟都会批量出现，只保留在行情表/文本报告中，避免数据库空转膨胀。
                if abs(score) < config.SCORE_NEUTRAL:
                    continue
                direction, dir_int = self._direction(score)
                cur = self.conn.execute(
                    """INSERT INTO signals(ts,cycle,variety,code,sym,exchange,cat,price,chg_pct,
                       score,direction,direction_int,label,score_band,advice,stop,target,atr,
                       contract_code,main_month,volume,open_interest,parts_json,flow_json,
                       raw_json,created_real)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (ts, cycle, r.get("name"), r.get("code"), r.get("sym"), r.get("ex"),
                     r.get("cat"), float(r.get("price") or 0), float(r.get("chg") or 0), score,
                     direction, dir_int, r.get("label"), score_band_name(score),
                     r.get("advice"), float(r.get("stop") or 0), float(r.get("target") or 0),
                     float(r.get("atr") or 0), r.get("contract_code", ""),
                     r.get("main_month", ""), float(r.get("volume") or 0),
                     float(r.get("open_interest") or 0), _json(r.get("parts") or {}),
                     _json(r.get("flow") or {}), _json(r), now_real))
                signal_id = cur.lastrowid
                if abs(score) >= config.SCORE_NEUTRAL:
                    self._create_outcomes_for_signal(signal_id, r, dt, direction, dir_int)
                n += 1
            self.conn.commit()
        return n

    def _create_outcomes_for_signal(self, signal_id, row, entry_dt, direction, dir_int):
        """同一品种/方向/分档仍有未到期任务时不重复建单，避免每轮重复刷同一信号。"""
        score = float(row.get("score", 0.0))
        band = score_band_name(score)
        existed = self.conn.execute(
            """SELECT o.id FROM signal_outcomes o JOIN signals s ON s.id=o.signal_id
               WHERE s.variety=? AND o.direction_int=? AND o.score_band=?
                 AND o.status='pending' LIMIT 1""",
            (row.get("name"), dir_int, band)).fetchone()
        if existed:
            return
        entry_ts = entry_dt.strftime("%Y-%m-%d %H:%M:%S")
        entry_price = float(row.get("price") or 0.0)
        if entry_price <= 0:
            return
        for horizon in config.SIGNAL_OUTCOME_HORIZONS:
            due_dt = entry_dt + timedelta(minutes=int(horizon))
            self.conn.execute(
                """INSERT OR IGNORE INTO signal_outcomes(signal_id,variety,code,horizon_min,
                   direction,direction_int,score,score_band,entry_ts,entry_price,due_ts,
                   status,created_real)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',?)""",
                (signal_id, row.get("name"), row.get("code"), int(horizon), direction,
                 dir_int, score, band, entry_ts, entry_price,
                 due_dt.strftime("%Y-%m-%d %H:%M:%S"), datetime.now().timestamp()))

    # ---------------- 写入：期权 ----------------

    def insert_options(self, cycle, ts, opt_rows, strat_rows=None, fut_rows=None):
        fut_map = {r.get("name"): r for r in (fut_rows or [])}
        now_real = datetime.now().timestamp()
        n = 0
        with self.lock:
            for o in opt_rows or []:
                self.conn.execute(
                    """INSERT INTO options(ts,cycle,record_type,variety,strategy_name,direction,
                       score,underlying_price,iv,iv_ratio,delta,gamma,vega,theta_day,prem,
                       contract,verdict,all_pass,checks_json,legs_json,greeks_json,raw_json,created_real)
                       VALUES(?,?, 'single', ?, '单腿期权', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ts, cycle, o.get("name"), o.get("direction"),
                     float(o.get("score") or 0), float(o.get("underlying_price") or 0),
                     float(o.get("iv") or 0), float(o.get("iv_ratio") or 0),
                     float(o.get("delta") or 0), float(o.get("gamma") or 0),
                     float(o.get("vega") or 0), float(o.get("theta_day") or 0),
                     float(o.get("prem") or 0), o.get("opt_code", ""), o.get("verdict", ""),
                     1 if o.get("all_pass") else 0, _json(o.get("checks") or []),
                     _json([]), _json({k: o.get(k) for k in ("delta", "gamma", "vega", "theta_day")}),
                     _json(o), now_real))
                n += 1
            for s in strat_rows or []:
                fr = fut_map.get(s.get("variety")) or {}
                self.conn.execute(
                    """INSERT INTO options(ts,cycle,record_type,variety,strategy_name,direction,
                       score,underlying_price,delta,gamma,vega,theta_day,net,max_profit,max_loss,
                       verdict,all_pass,checks_json,legs_json,greeks_json,raw_json,created_real)
                       VALUES(?,?, 'strategy', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ts, cycle, s.get("variety"), s.get("name"),
                     "多头" if s.get("direction", 0) > 0 else ("空头" if s.get("direction", 0) < 0 else "中性"),
                     float(fr.get("score") or 0), float(fr.get("price") or 0),
                     float(s.get("delta") or 0), float(s.get("gamma") or 0),
                     float(s.get("vega") or 0), float(s.get("theta_day") or 0), float(s.get("net") or 0),
                     None if s.get("max_profit") is None else float(s.get("max_profit")),
                     None if s.get("max_loss") is None else float(s.get("max_loss")),
                     s.get("verdict", ""), 1 if s.get("all_pass") else 0,
                     _json(s.get("checks") or []), _json(s.get("legs") or []),
                     _json({k: s.get(k) for k in ("delta", "gamma", "vega", "theta_day", "margin_points", "margin_note")}),
                     _json(s), now_real))
                n += 1
            self.conn.commit()
        return n

    # ---------------- 写入：期权完整链快照（第11轮，PCR历史分位积累） ----------------

    def insert_option_chains(self, cycle, ts, chain_rows):
        """chain_rows: [(variety_name, chain_dict)]；chain 由 option_chain.build_summary 产出。"""
        if not chain_rows:
            return 0
        now_real = datetime.now().timestamp()
        n = 0
        with self.lock:
            for variety, ch in chain_rows:
                pcr = ch.get("pcr_oi")
                if pcr is None:
                    continue
                self.conn.execute(
                    """INSERT OR IGNORE INTO option_chains(ts,cycle,sym,variety,expiry,
                       n_call,n_put,call_oi,put_oi,pcr_oi,atm_strike,
                       max_call_strike,max_put_strike,raw_json,created_real)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (ts, cycle, ch.get("sym"), variety, ch.get("label"),
                     int(ch.get("n_call", 0)), int(ch.get("n_put", 0)),
                     float(ch.get("call_oi", 0)), float(ch.get("put_oi", 0)), float(pcr),
                     ch.get("atm_strike"), ch.get("max_call_oi_strike"),
                     ch.get("max_put_oi_strike"), _json(ch), now_real))
                n += 1
            self.conn.commit()
        return n

    def pcr_percentile(self, sym, current_pcr, days=None):
        """当前PCR在近days日历史样本中的分位（0~1）；样本不足返回None。"""
        if current_pcr is None:
            return None
        days = days or config.PCR_LOOKBACK_DAYS
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            rows = self.conn.execute(
                "SELECT pcr_oi FROM option_chains WHERE sym=? AND ts>=? AND pcr_oi IS NOT NULL",
                (sym, since)).fetchall()
        hist = [r["pcr_oi"] for r in rows]
        if len(hist) < 10:
            return None
        below = sum(1 for v in hist if v <= current_pcr)
        return below / len(hist)

    # ---------------- 写入：基本面日频快照（第13轮） ----------------

    def insert_fundamentals(self, ts, rows):
        """rows: [(variety_name, sym, fund_pack)]，fund_pack 由 fundamental_factors.build_fundamental 产出。
        同一(sym,trade_date)覆盖更新（INSERT OR REPLACE），日频数据量小、长期保留用于分位回看。"""
        if not rows:
            return 0
        now_real = datetime.now().timestamp()
        trade_date = datetime.now().strftime("%Y-%m-%d")
        n = 0
        with self.lock:
            for variety, sym, pack in rows:
                if not pack:
                    continue
                sub = pack.get("sub") or {}
                inv = sub.get("库存仓单") or {}
                rk = sub.get("龙虎榜") or {}
                cy = sub.get("期限carry") or {}
                bs = sub.get("基差") or {}
                self.conn.execute(
                    """INSERT OR REPLACE INTO fundamentals(ts,trade_date,sym,variety,inventory,
                       inv_pct,inv_wow,inv_n,rank_long,rank_short,rank_net,rank_delta,
                       carry,basis_rate,fund_score,raw_json,created_real)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (ts, trade_date, sym, variety,
                     inv.get("current"), inv.get("pct"), inv.get("wow"),
                     inv.get("n"), rk.get("long"), rk.get("short"), rk.get("net"),
                     rk.get("delta"), cy.get("annual_carry"), bs.get("basis_rate"),
                     float(pack.get("score") or 0), _json(pack), now_real))
                n += 1
            self.conn.commit()
        return n

    def latest_fundamentals(self):
        """取最近一个交易日的全部品种基本面快照 {sym: pack_dict}，供程序重启后当日复用。"""
        with self.lock:
            row = self.conn.execute(
                "SELECT MAX(trade_date) AS d FROM fundamentals").fetchone()
            if not row or not row["d"]:
                return {}
            rows = self.conn.execute(
                "SELECT sym,raw_json FROM fundamentals WHERE trade_date=?", (row["d"],)).fetchall()
        import json as _json_mod
        out = {}
        for r in rows:
            try:
                out[r["sym"]] = _json_mod.loads(r["raw_json"])
            except Exception:
                continue
        return out

    # ---------------- 写入/读取：分钟K线（第14轮 WP-D0 常驻自采库） ----------------

    def insert_minute_bars(self, bars):
        """bars: intraday_bars 产出的 bar dict 列表。按 (contract,period,bar_dt) 去重
        （INSERT OR IGNORE），重启/重复自采不产生重复行，返回实际新增行数。"""
        if not bars:
            return 0
        now_real = datetime.now().timestamp()
        n = 0
        with self.lock:
            for b in bars:
                cur = self.conn.execute(
                    """INSERT OR IGNORE INTO minute_bars(sym,contract,exchange,period,bar_dt,
                       trade_date,o,h,l,c,v,amount,created_real)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (b.get("sym"), b.get("contract"), b.get("exchange"), int(b.get("period", 0)),
                     b.get("dt"), b.get("trade_date") or str(b.get("dt", ""))[:10],
                     float(b.get("o") or 0), float(b.get("h") or 0), float(b.get("l") or 0),
                     float(b.get("c") or 0), float(b.get("v") or 0), float(b.get("amount") or 0),
                     now_real))
                n += int(cur.rowcount or 0)
            self.conn.commit()
        return n

    def upsert_tick_snapshots(self, rows):
        """G14（第92轮）一档盘口快照落库：按 (sym, bucket) INSERT OR REPLACE 幂等去重——
        同一 5 分钟桶重复采集/断网重试/重启续传都只保留最新一行（upsert 去重、不丢不重）。
        rows: orderbook_snapshot 产出的 dict 列表。返回受影响行数（写入行数）。"""
        if not rows:
            return 0
        with self.lock:
            cur = self.conn.executemany(
                """INSERT OR REPLACE INTO tick_snapshots(
                       sym, variety, bucket, quote_date, quote_time, collected_at,
                       bid, ask, latest, bid_vol, ask_vol, spread, spread_bp,
                       prev_settle, oi, volume, created_real)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(r.get("sym"), r.get("variety"), r.get("bucket"),
                  r.get("quote_date"), r.get("quote_time"), r.get("collected_at"),
                  float(r.get("bid") or 0), float(r.get("ask") or 0), float(r.get("latest") or 0),
                  float(r.get("bid_vol") or 0), float(r.get("ask_vol") or 0),
                  float(r.get("spread") or 0), float(r.get("spread_bp") or 0),
                  float(r.get("prev_settle") or 0), float(r.get("oi") or 0),
                  float(r.get("volume") or 0), r.get("created_real") or 0.0)
                 for r in rows])
            self.conn.commit()
            return cur.rowcount if cur.rowcount else len(rows)

    def recent_tick_snapshots(self, days=30, sym=None, limit=5000):
        """按需读取一档盘口快照（G14 统计用），升序返回 dict 列表。"""
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        sql = ("SELECT sym, variety, bucket, quote_date, quote_time, collected_at,"
               " bid, ask, latest, bid_vol, ask_vol, spread, spread_bp, oi, volume"
               " FROM tick_snapshots WHERE collected_at >= ?")
        args = [cutoff]
        if sym:
            sql += " AND sym = ?"
            args.append(sym)
        sql += " ORDER BY collected_at ASC, sym ASC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def minute_bars_for_sym(self, sym, period, since=None, limit=None):
        """按品种跨具体合约取某周期分钟bar（换月后新旧主力按时间自然衔接），升序返回 dict 列表，
        供第15轮主连分钟拼接+比例复权（backtest.ratio_adjusted_bars）。"""
        sql = ("SELECT sym,contract,exchange,period,bar_dt AS dt,trade_date,"
               "o,h,l,c,v,amount FROM minute_bars WHERE sym=? AND period=?")
        args = [str(sym).upper(), int(period)]
        if since:
            sql += " AND bar_dt>=?"
            args.append(since)
        sql += " ORDER BY bar_dt ASC, contract ASC"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def minute_bars_coverage(self):
        """各周期覆盖 {period: {bars,contracts,first,last}}，供日志/报告核对自采进度。"""
        out = {}
        with self.lock:
            rows = self.conn.execute(
                """SELECT period, COUNT(*) AS n, COUNT(DISTINCT contract) AS nc,
                          MIN(bar_dt) AS first, MAX(bar_dt) AS last
                   FROM minute_bars GROUP BY period ORDER BY period""").fetchall()
        for r in rows:
            out[int(r["period"])] = {"bars": r["n"], "contracts": r["nc"],
                                     "first": r["first"], "last": r["last"]}
        return out

    # ---------------- WP-F2 A3/B2：历史信号-结果配对（校准器与因子IC评估共用） ----------------

    def _outcome_join_sql(self, where_extra=""):
        return ("""SELECT o.direction_int AS direction_int, o.score AS score,
                          o.score_band AS score_band, o.horizon_min AS horizon_min,
                          o.ret AS ret, o.hit AS hit, o.status AS status,
                          o.entry_ts AS entry_ts, o.eval_ts AS eval_ts,
                          o.variety AS variety, s.parts_json AS parts_json
                   FROM signal_outcomes o JOIN signals s ON s.id=o.signal_id
                   WHERE o.status IN ('hit','miss','flat') """ + where_extra +
                " ORDER BY o.eval_ts ASC")

    def calibration_pairs(self, horizon=None, days=None):
        """已评估信号与其发出时因子拆分的配对样本（供 signal_calibrator 贝叶斯胜率统计）。
        hit/miss/flat 为有效样本（expired 不进分母，与 outcome_stats 口径一致）。"""
        where, args = [], []
        if horizon:
            where.append("o.horizon_min=?")
            args.append(int(horizon))
        if days:
            since = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
            where.append("o.eval_ts>=?")
            args.append(since)
        sql = self._outcome_join_sql((" AND " + " AND ".join(where)) if where else "")
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def factor_outcome_pairs(self, horizons=(30, 120, 1440), days=None):
        """B2 因子IC评估：按多个评估周期返回配对样本（因子拆分 + 远期方向收益）。"""
        ph = ",".join("?" * len(horizons))
        where = " AND o.horizon_min IN (%s)" % ph
        args = [int(h) for h in horizons]
        if days:
            since = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
            where += " AND o.eval_ts>=?"
            args.append(since)
        sql = self._outcome_join_sql(where)
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # ---------------- WP-F2 B3：triple-barrier 样本集 ml_samples（第9张表） ----------------

    def insert_ml_samples(self, rows):
        """rows: build_ml_samples 产出的样本 dict 列表；按 (sym,period,bar_dt) 覆盖去重，返回写入数。"""
        if not rows:
            return 0
        now_real = datetime.now().timestamp()
        n = 0
        with self.lock:
            for r in rows:
                cur = self.conn.execute(
                    """INSERT OR REPLACE INTO ml_samples(sym,variety,period,bar_dt,trade_date,
                       direction,entry_price,atr,tp_price,sl_price,exit_dt,exit_price,label,
                       exit_reason,bars_held,ret_dir,tech_score,features_json,created_real)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (r.get("sym"), r.get("variety"), int(r.get("period", 0)),
                     r.get("bar_dt"), r.get("trade_date"), int(r.get("direction", 0)),
                     r.get("entry_price"), r.get("atr"), r.get("tp_price"), r.get("sl_price"),
                     r.get("exit_dt"), r.get("exit_price"), int(r.get("label", 0)),
                     r.get("exit_reason"), int(r.get("bars_held", 0)), r.get("ret_dir"),
                     r.get("tech_score"), _json(r.get("features") or {}), now_real))
                n += 1
            self.conn.commit()
        return n

    def ml_sample_rows(self, sym=None, period=None, limit=None):
        """导出 ml_samples（features_json 反序列化），供 WP-F4 训练/研究侧使用。"""
        where, args = [], []
        if sym:
            where.append("sym=?")
            args.append(str(sym).upper())
        if period:
            where.append("period=?")
            args.append(int(period))
        sql = "SELECT * FROM ml_samples"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY bar_dt ASC"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["features"] = json.loads(d.pop("features_json") or "{}")
            except (TypeError, ValueError):
                d["features"] = {}
            out.append(d)
        return out

    # ---------------- G6 数据质量：data_health ----------------

    def insert_data_health(self, ts, rows):
        """rows: [{source,req,ok,fail,stale,jump,latency_ms,state,note}]，按 (ts,source) 覆盖。"""
        if not rows:
            return 0
        now_real = datetime.now().timestamp()
        n = 0
        with self.lock:
            for r in rows:
                self.conn.execute(
                    """INSERT OR REPLACE INTO data_health(ts,source,req,ok,fail,stale,jump,
                       latency_ms,state,note,created_real) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (ts, str(r.get("source"))[:40], int(r.get("req", 0)), int(r.get("ok", 0)),
                     int(r.get("fail", 0)), int(r.get("stale", 0)), int(r.get("jump", 0)),
                     r.get("latency_ms"), str(r.get("state") or ""),
                     str(r.get("note") or "")[:200], now_real))
                n += 1
            self.conn.commit()
        return n

    def data_health_recent(self, limit=200):
        """最近的数据质量记录（按时间倒序），供看板/复盘读取。"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM data_health ORDER BY created_real DESC, source ASC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 写入：回测留档（G4） ----------------

    def insert_backtest_run(self, run):
        """落一行回测运行留档。run 为 dict：run_ts/kind/fill_mode/cost_mode/
        n_symbols/n_trades/sample_days/params(dict)/metrics(dict)/cumulative/max_dd/sharpe/win_rate。
        返回新行 id；字段缺失以 None/0 兜底，绝不抛错拖垮离线回测。"""
        now_real = datetime.now().timestamp()
        params = run.get("params")
        metrics = run.get("metrics")
        with self.lock:
            cur = self.conn.execute(
                """INSERT INTO backtest_runs(run_ts,kind,fill_mode,cost_mode,n_symbols,n_trades,
                   sample_days,params_json,metrics_json,cumulative,max_dd,sharpe,win_rate,created_real)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(run.get("run_ts"))[:19], str(run.get("kind") or "daily")[:20],
                 str(run.get("fill_mode") or "")[:20], str(run.get("cost_mode") or "")[:60],
                 int(run.get("n_symbols") or 0), int(run.get("n_trades") or 0),
                 int(run.get("sample_days") or 0),
                 _json(params) if params is not None else None,
                 _json(metrics) if metrics is not None else None,
                 run.get("cumulative"), run.get("max_dd"),
                 run.get("sharpe"), run.get("win_rate"), now_real))
            self.conn.commit()
            return cur.lastrowid

    def backtest_run_history(self, kind="daily", limit=500):
        """按时间正序返回某类回测的历史留档（含本次），供纵向对比/百分位排名。"""
        with self.lock:
            rows = self.conn.execute(
                """SELECT * FROM backtest_runs WHERE kind=?
                   ORDER BY created_real ASC, id ASC LIMIT ?""",
                (str(kind or "daily"), int(limit))).fetchall()
        return [dict(r) for r in rows]

    # ---------------- G1 纸面交易（paper_orders/paper_trades/paper_equity） ----------------

    def insert_paper_order(self, order):
        """落一条纸面委托（next 档先以 status=pending 落库，成交后 update_paper_order 回填）。
        order dict 字段缺失一律安全兜底，绝不抛错拖垮主循环；返回新行 id。"""
        now_real = datetime.now().timestamp()
        with self.lock:
            cur = self.conn.execute(
                """INSERT INTO paper_orders(ts,sym,name,sector,action,side,direction,lots,
                   signal_price,score,band,fill_mode,status,fill_ts,fill_price,raw_price,
                   reason,order_ref,pos_ref,raw_json,created_real)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(order.get("ts"))[:19], str(order.get("sym") or "")[:16],
                 str(order.get("name") or "")[:24], str(order.get("sector") or "")[:16],
                 str(order.get("action") or "")[:20], str(order.get("side") or "")[:8],
                 order.get("direction"), int(order.get("lots") or 0),
                 order.get("signal_price"), order.get("score"),
                 str(order.get("band") or "")[:8], str(order.get("fill_mode") or "")[:8],
                 str(order.get("status") or "pending")[:12],
                 str(order.get("fill_ts") or "")[:19] if order.get("fill_ts") else None,
                 order.get("fill_price"), order.get("raw_price"),
                 str(order.get("reason") or "")[:80],
                 str(order.get("order_ref") or "")[:40],
                 str(order.get("pos_ref") or "")[:40],
                 _json(order["raw"]) if order.get("raw") is not None else None, now_real))
            self.conn.commit()
            return cur.lastrowid

    def update_paper_order(self, order_id, **fields):
        """成交/拒单/阻断时回填委托：status/fill_ts/fill_price/raw_price/lots/reason。"""
        if not order_id or not fields:
            return 0
        allowed = {"status", "fill_ts", "fill_price", "raw_price", "lots", "reason"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append(f"{k}=?")
            vals.append(str(v)[:80] if k == "reason" else v)
        if not sets:
            return 0
        vals.append(order_id)
        with self.lock:
            cur = self.conn.execute(
                f"UPDATE paper_orders SET {', '.join(sets)} WHERE id=?", vals)
            self.conn.commit()
            return cur.rowcount

    def insert_paper_trade(self, t):
        """落一条纸面成交（开/平各一条）。返回新行 id；字段缺失安全兜底。"""
        now_real = datetime.now().timestamp()
        with self.lock:
            cur = self.conn.execute(
                """INSERT INTO paper_trades(ts,pos_ref,sym,name,sector,side,dir_text,direction,
                   lots,price,raw_price,notional,slip_yuan,fee_yuan,realized_yuan,leg,reason,
                   forced,order_id,entry_ts,entry_price,score,margin_rate,created_real)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(t.get("ts"))[:19], str(t.get("pos_ref") or "")[:40],
                 str(t.get("sym") or "")[:16], str(t.get("name") or "")[:24],
                 str(t.get("sector") or "")[:16], str(t.get("side") or "")[:8],
                 str(t.get("dir_text") or "")[:4], t.get("direction"),
                 int(t.get("lots") or 0), t.get("price"), t.get("raw_price"),
                 t.get("notional"), t.get("slip_yuan"), t.get("fee_yuan"), t.get("realized_yuan"),
                 str(t.get("leg") or "")[:8], str(t.get("reason") or "")[:40],
                 1 if t.get("forced") else 0, t.get("order_id"),
                 str(t.get("entry_ts") or "")[:19] if t.get("entry_ts") else None,
                 t.get("entry_price"), t.get("score"), t.get("margin_rate"), now_real))
            self.conn.commit()
            return cur.lastrowid

    def insert_paper_equity(self, snap):
        """落一轮一条账户权益快照（同 ts 覆盖，保证一轮一条、重跑幂等）。"""
        now_real = datetime.now().timestamp()
        with self.lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO paper_equity(ts,static_equity,float_pnl,equity,
                   margin_used,available,risk_degree,drawdown,n_positions,realized,fees_paid,
                   n_trades,positions_json,created_real)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(snap.get("ts"))[:19], snap.get("static_equity"), snap.get("float_pnl"),
                 snap.get("equity"), snap.get("margin_used"), snap.get("available"),
                 snap.get("risk_degree"), snap.get("drawdown"), int(snap.get("n_positions") or 0),
                 snap.get("realized"), snap.get("fees_paid"), int(snap.get("n_trades") or 0),
                 _json(snap["positions"]) if snap.get("positions") is not None else None,
                 now_real))
            self.conn.commit()

    def paper_open_position_trades(self):
        """返回每个 pos_ref 当前仍未平仓的【开仓成交】（有 open 无对应 close），供重启恢复持仓。"""
        with self.lock:
            rows = self.conn.execute(
                """SELECT * FROM paper_trades t
                   WHERE side='open' AND NOT EXISTS(
                       SELECT 1 FROM paper_trades c WHERE c.pos_ref=t.pos_ref AND c.side='close')
                   ORDER BY id ASC""").fetchall()
        return [dict(r) for r in rows]

    def paper_realized_fees(self):
        """汇总历史已实现净盈亏与手续费（开仓费计入平仓 realized，这里直接累加成交金额）。"""
        with self.lock:
            row = self.conn.execute(
                """SELECT COALESCE(SUM(realized_yuan),0.0) AS realized,
                          COALESCE(SUM(fee_yuan),0.0) AS fees FROM paper_trades""").fetchone()
        return float(row["realized"]), float(row["fees"])

    def paper_last_equity(self):
        """最近一条权益快照（无则 None）。"""
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM paper_equity ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def paper_orders_recent(self, limit=200):
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM paper_orders ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def paper_trades_recent(self, limit=500):
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def paper_equity_series(self, limit=2000):
        """最近 limit 条权益快照、按时间升序返回（长期影子后表会很大，图表/看板只取最近窗口；
        子查询先 DESC 截断再外层 ASC 复原曲线顺序）。"""
        with self.lock:
            rows = self.conn.execute(
                """SELECT * FROM (
                       SELECT * FROM paper_equity ORDER BY id DESC LIMIT ?
                   ) ORDER BY id ASC""", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def paper_order_status_counts(self):
        """委托按状态聚合计数（pending/filled/blocked/rejected/cancelled），供账户报告区分
        "在途排队"与"确定拒单"。"""
        out = {"pending": 0, "filled": 0, "blocked": 0, "rejected": 0, "cancelled": 0}
        with self.lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM paper_orders GROUP BY status").fetchall()
        for r in rows:
            if r["status"] in out:
                out[r["status"]] = int(r["n"])
        return out

    # ---------------- 信号到期评估 ----------------

    def update_signal_outcomes(self, quotes):
        now = datetime.now()
        now_s = now.strftime("%Y-%m-%d %H:%M:%S")
        updated = 0
        with self.lock:
            pending = self.conn.execute(
                """SELECT o.*, s.code FROM signal_outcomes o JOIN signals s ON s.id=o.signal_id
                   WHERE o.status='pending' AND o.due_ts<=?""", (now_s,)).fetchall()
            for row in pending:
                due = _dt(row["due_ts"])
                overdue_sec = (now - due).total_seconds()
                q = quotes.get(row["code"]) or {}
                exit_price = float(q.get("latest") or 0.0)
                entry_price = float(row["entry_price"] or 0.0)
                if entry_price <= 0:
                    continue
                if exit_price <= 0:
                    # 数据源短暂缺失时等待；长期缺失（如代码失效）不能让 pending 永久堆积。
                    if overdue_sec <= config.SIGNAL_OUTCOME_MAX_WAIT_SEC:
                        continue
                    self.conn.execute(
                        """UPDATE signal_outcomes SET eval_ts=?,exit_price=0,ret=0,hit=0,status='expired'
                           WHERE id=?""", (now_s, row["id"]))
                    updated += 1
                    continue
                same_price = abs(exit_price - entry_price) < 1e-12
                meta = _CODE_TO_META.get(row["code"])
                variety_active = is_variety_trading(meta) if meta else False
                # 休市/接口返回旧价时等待；若该品种自身正在交易且价格确实没动，则按“打平”及时评估。
                if same_price and not variety_active and (now - due).total_seconds() < config.SIGNAL_OUTCOME_MAX_WAIT_SEC:
                    continue
                if same_price and not variety_active:
                    status, hit, ret = "expired", 0, 0.0
                elif same_price:
                    status, hit, ret = "flat", 0, 0.0
                else:
                    ret = row["direction_int"] * (exit_price / entry_price - 1.0)
                    if ret > 0:
                        status, hit = "hit", 1
                    elif ret < 0:
                        status, hit = "miss", 0
                    else:
                        status, hit = "flat", 0
                self.conn.execute(
                    """UPDATE signal_outcomes SET eval_ts=?,exit_price=?,ret=?,hit=?,status=?
                       WHERE id=?""", (now_s, exit_price, ret, hit, status, row["id"]))
                updated += 1
            if updated:
                self.conn.commit()
        return updated

    def outcome_stats(self, days=None):
        days = days or config.SIGNAL_TRACK_STAT_DAYS
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            rows = self.conn.execute(
                """SELECT horizon_min, score_band, direction,
                          COUNT(*) AS n,
                          SUM(CASE WHEN status IN ('hit','miss','flat') THEN 1 ELSE 0 END) AS evaluated,
                          SUM(CASE WHEN status='hit' THEN 1 ELSE 0 END) AS wins,
                          SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END) AS expired,
                          AVG(CASE WHEN status IN ('hit','miss','flat') THEN ret END) AS avg_ret,
                          AVG(CASE WHEN status IN ('hit','miss','flat') AND ret>0 THEN ret END) AS avg_win,
                          AVG(CASE WHEN status IN ('hit','miss','flat') AND ret<0 THEN ret END) AS avg_loss
                   FROM signal_outcomes
                   WHERE status IN ('hit','miss','flat','expired') AND eval_ts>=?
                   GROUP BY horizon_min,score_band,direction
                   ORDER BY horizon_min,score_band,direction""", (since,)).fetchall()
            return [dict(r) for r in rows]

    def pending_count(self):
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) AS n FROM signal_outcomes WHERE status='pending'").fetchone()["n"]

    def recent_outcomes(self, limit=15):
        with self.lock:
            rows = self.conn.execute(
                """SELECT variety,horizon_min,direction,score,score_band,entry_ts,entry_price,
                          eval_ts,exit_price,ret,hit,status
                   FROM signal_outcomes WHERE status IN ('hit','miss','flat','expired')
                   ORDER BY eval_ts DESC,id DESC LIMIT ?""", (int(limit),)).fetchall()
            return [dict(r) for r in rows]

    def table_counts(self):
        out = {}
        with self.lock:
            for table in ("quotes", "signals", "news", "options", "signal_outcomes", "option_chains", "fundamentals", "minute_bars", "ml_samples", "data_health", "backtest_runs", "paper_orders", "paper_trades", "paper_equity", "tick_snapshots"):
                out[table] = self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        return out

    def prune(self):
        """高频明细默认保留半年；可交易信号及其效果追踪长期保留用于调参。"""
        cutoff = (datetime.now() - timedelta(days=config.DB_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            self.conn.execute("DELETE FROM quotes WHERE ts < ?", (cutoff,))
            self.conn.execute("DELETE FROM news WHERE ts < ?", (cutoff,))
            self.conn.execute("DELETE FROM options WHERE ts < ?", (cutoff,))
            self.conn.execute("DELETE FROM option_chains WHERE ts < ?", (cutoff,))
            mb_cut = (datetime.now() - timedelta(days=config.MINUTE_BARS_RETENTION_DAYS)).strftime("%Y-%m-%d")
            self.conn.execute("DELETE FROM minute_bars WHERE trade_date < ?", (mb_cut,))
            # 中性信号不产生 outcome，无需长期保留；非中性信号被 signal_outcomes 外键引用，长期保留。
            self.conn.execute(
                "DELETE FROM signals WHERE ts < ? AND ABS(score) < ?",
                (cutoff, config.SCORE_NEUTRAL))
            # ml_samples 是监督学习样本资产，按更长的保留期清理（默认约10年，近似长期保留）。
            ml_cut = (datetime.now() - timedelta(days=config.ML_SAMPLES_RETENTION_DAYS)).strftime("%Y-%m-%d")
            self.conn.execute("DELETE FROM ml_samples WHERE bar_dt < ?", (ml_cut,))
            dh_cut = (datetime.now() - timedelta(days=config.DATA_HEALTH_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
            self.conn.execute("DELETE FROM data_health WHERE ts < ?", (dh_cut,))
            btr_cut = (datetime.now() - timedelta(days=config.BACKTEST_RUNS_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
            self.conn.execute("DELETE FROM backtest_runs WHERE run_ts < ?", (btr_cut,))
            paper_cut = (datetime.now() - timedelta(days=config.PAPER_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
            self.conn.execute("DELETE FROM paper_orders WHERE ts < ?", (paper_cut,))
            self.conn.execute("DELETE FROM paper_trades WHERE ts < ?", (paper_cut,))
            self.conn.execute("DELETE FROM paper_equity WHERE ts < ?", (paper_cut,))
            snap_cut = (datetime.now() - timedelta(days=config.SNAPSHOT_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
            self.conn.execute("DELETE FROM tick_snapshots WHERE collected_at < ?", (snap_cut,))
            self.conn.commit()
