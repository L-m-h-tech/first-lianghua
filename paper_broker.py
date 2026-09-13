# -*- coding: utf-8 -*-
"""G1 纸面交易引擎 PaperBroker（第27轮：表 + 撮合状态机；第28轮：平今/平昨 owner + 账户视图，
main/报告/看板同期接入）。

它补的是本系统唯一塌陷的"订单执行层"：信号原本止于 analyzer 综合分与一句"建议手数"，
signal_outcomes 只判固定周期方向对错（不含手续费、不连续持仓、没有资金曲线）。PaperBroker
在【不花真钱、不接实盘】的前提下，把每一轮综合分信号串成一笔笔虚拟委托/成交，用一个共享
资金池账户持续盯市，第一次能量化回答："严格按综合分 + 资金管理做，含真实成本后的账户净值 /
最大回撤 / 换手 / 与 hit 率是否一致"。对标 freqtrade dry-run、vnpy SimNow、nautilus
"回测/实盘同构"。先 paper，永远不自动接实盘（实盘门槛见融合总纲 G20）。

设计要点（三铁律：不动实时监控主链与综合分口径；默认影子/开关缺省等价旧版；零新增依赖）：
  1. 账户内核【直接复用】portfolio.Portfolio——三种 sizing、单品种/板块/可用资金/持仓数约束链、
     逐轮盯市、触发线/安全线两段式强平状态机、真实费率，全部不写第二套；本模块只做"实时轮询
     信号 -> 委托 -> 成交"的状态机和持久化。
  2. 成交两档（与 backtest G4 对齐）：
     - close：信号轮当轮最新价成交（与回测 close 口径一致）；
     - next（影子默认、保守）：信号轮只挂单，下一轮首个新价成交，成交严格晚于信号；
       下一轮锁板/无价则继续挂（顺延），不虚构成交。
  3. 三阈值迟滞状态机（防抖动反复打脸）：|综合分|>=PAPER_ENTRY_SCORE 才开仓/反手；持仓后
     |分|<PAPER_EXIT_SCORE 才离场；二者之间继续持有，不反复开平。
  4. 实时锁板：复用 config.FUTURES_LIMIT_MOVE，相对昨结整根贴板才拦截（买入撞涨停、卖出撞跌停
     都买/卖不出去），判定不了（缺昨结/缺涨跌停表）则放行，与回测"疑似锁板"同样保守。
  5. 成本：成交价【内含滑点】（买=盘面价×(1+slip)、卖=盘面价×(1-slip)）；手续费走 Portfolio
     的真实费率表（data/futures_fees.csv，缺表回退兜底比例）。双边成本都可逐笔断言。
  6. 三表持久化（storage）：paper_orders 委托流水 / paper_trades 开平仓成交 / paper_equity
     每轮权益快照；进程重启可由三表恢复持仓、已实现盈亏与挂单，支持连续影子 >=4 周对照。
  7. 纯标准库、零网络、db 可空（纯内存便于合成断言）；PAPER_ENABLED=False 时 main 根本不实例化。

诚实边界：免费数据是 5 分钟级轮询快照、非逐笔/L2，"下一轮首个新价"是下一次轮询价而非真实
开盘竞价；平今/平昨按交易所结算交易日 owner 实时判定（与 intraday_backtest.owner_of_dt 同口径，
判不了保守按平昨）；保证金为公司常态档估算。
以上都不改变"严格按信号做、含成本后到底赚不赚钱"这个核心问题的可证伪性。不构成投资建议。

自检：D:\\Python\\python.exe paper_broker.py --selftest
"""
import argparse
import threading
from datetime import datetime

import config
import portfolio as portfolio_mod
import circuit_breaker
from backtest import load_fee_schedule
from storage import score_band_name
from utils import LOG   # 第121轮修复：原缺 LOG 定义导致 639 行补仓日志 NameError 被吞


# =========================== 纯函数（无状态、零网络，可直接合成断言） ===========================

def _default_owner_of_ts(ts):
    """把时间戳映射到【交易所结算交易日】（平今/平昨判定用），与 intraday_backtest.owner_of_dt
    同口径（夜盘21点后归下一交易日、凌晨归当日），两者必须一致、不可混用 utils 的日切口径。
    解析/日历失败一律返回 None——调用方据此保守按"平昨"计费（等价第27轮行为，绝不虚增平今免费）。"""
    try:
        from intraday_backtest import owner_of_dt
        d = datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
        return owner_of_dt(d)
    except Exception:
        return None

def want_position(score, held_dir, entry_score, exit_score):
    """三阈值迟滞状态机。返回 (want_dir, action)。

    held_dir：当前持仓方向 1多/-1空/0空仓；want_dir：本轮目标方向；
    action：open 开仓 / close 离场 / reverse 反手（先平后开）/ hold 不动。
    """
    if score is None:
        return held_dir, "hold"
    if score >= entry_score:
        sig = 1
    elif score <= -entry_score:
        sig = -1
    else:
        sig = 0
    if held_dir == 0:
        if sig != 0:
            return sig, "open"
        return 0, "hold"
    # 持仓中
    if abs(score) < exit_score:
        return 0, "close"
    if sig != 0 and sig == -held_dir:
        return sig, "reverse"
    return held_dir, "hold"


def locked_at_quote(quote, limit_move, buying, eps=None):
    """实时锁板判定（与 portfolio._locked / intraday 整根封死同口径）。

    buying=True 买入（开多/平空）撞涨停买不进；False 卖出（开空/平多）撞跌停卖不出。
    需昨结 prev_settle 与高/低价；缺数据或无涨跌停幅度则放行（不拦截）。
    """
    eps = config.PAPER_LIMIT_EPS if eps is None else eps
    if not quote:
        return False
    base = float(quote.get("prev_settle") or 0.0)
    latest = float(quote.get("latest") or quote.get("price") or 0.0)
    if base <= 0 or latest <= 0 or not limit_move or limit_move >= 1:
        return False
    if buying:
        up_limit = base * (1.0 + limit_move)
        low = float(quote.get("low") or latest)
        return latest >= up_limit * (1.0 - eps) and low >= up_limit * (1.0 - eps)
    down_limit = base * (1.0 - limit_move)
    high = float(quote.get("high") or latest)
    return latest <= down_limit * (1.0 + eps) and high <= down_limit * (1.0 + eps)


def apply_slip(price, side, slip_rate):
    """成交价内含滑点：buy 向上、sell 向下。side: 'buy'/'sell'。"""
    if price <= 0:
        return 0.0
    if side == "buy":
        return price * (1.0 + slip_rate)
    if side == "sell":
        return price * (1.0 - slip_rate)
    return price


def sector_map():
    """从 config.VARIETIES 构建 {sym: 板块}，供 Portfolio 板块上限约束。"""
    out = {}
    for meta in getattr(config, "VARIETIES", {}).values():
        sym = (meta.get("sym") or "").upper()
        if sym:
            out[sym] = meta.get("cat")
    return out


def _side_of(direction, leg):
    """direction 持仓/目标方向，leg=open 开仓/close 平仓，返回买卖方向 buy/sell。"""
    if leg == "open":
        return "buy" if direction > 0 else "sell"
    return "sell" if direction > 0 else "buy"   # 平多卖出、平空买回


# =========================== G1续（第63轮）OMS/成交回报/持仓对账 纯函数 ===========================

def reconcile_position_sets(internal, external, price_tol=1e-6):
    """持仓对账纯函数：把内部持仓 {sym:{direction,lots,entry_price}} 与外部/托管台账逐品种比对。

    每个 sym 归入 matched 或 breaks；break 类型可叠加（如 "direction+lots"）：
      missing_external 内部有、外部无（内部幽灵仓/外部漏报）；missing_internal 外部有、内部无（内部漏记）；
      direction 多空方向相反；lots 手数不符（带 lots_delta=外部-内部）；entry_price 开仓价差超 price_tol（None=不比价）。
    返回 {matched,breaks,n_matched,n_breaks,clean}；纯函数、零 IO，便于确定性单测。"""
    internal = internal or {}
    external = external or {}
    matched, breaks = [], []
    for sym in sorted(set(internal) | set(external)):
        i, e = internal.get(sym), external.get(sym)
        if i and not e:
            breaks.append({"sym": sym, "type": "missing_external",
                           "internal": dict(i), "external": None})
            continue
        if e and not i:
            breaks.append({"sym": sym, "type": "missing_internal",
                           "internal": None, "external": dict(e)})
            continue
        problems = []
        if int(i.get("direction", 0)) != int(e.get("direction", 0)):
            problems.append("direction")
        lots_delta = int(e.get("lots", 0)) - int(i.get("lots", 0))
        if lots_delta != 0:
            problems.append("lots")
        if price_tol is not None and \
                abs(float(i.get("entry_price", 0.0) or 0.0)
                    - float(e.get("entry_price", 0.0) or 0.0)) > price_tol:
            problems.append("entry_price")
        if problems:
            breaks.append({"sym": sym, "type": "+".join(problems), "lots_delta": lots_delta,
                           "internal": dict(i), "external": dict(e)})
        else:
            matched.append(sym)
    return {"matched": matched, "breaks": breaks, "n_matched": len(matched),
            "n_breaks": len(breaks), "clean": not breaks}


def aggregate_fills(fills):
    """成交回报汇总纯函数：对一批 fill(trade) dict 聚合笔数/手数/名义/费/滑点/已实现/多空开平。"""
    agg = {"n_fills": len(fills), "lots": 0, "notional": 0.0, "fee_yuan": 0.0,
           "slip_yuan": 0.0, "realized_yuan": 0.0,
           "n_open": 0, "n_close": 0, "open_long": 0, "open_short": 0,
           "close_long": 0, "close_short": 0, "n_forced": 0}
    for t in fills:
        lots = int(t.get("lots", 0) or 0)
        agg["lots"] += lots
        agg["notional"] += float(t.get("notional", 0.0) or 0.0)
        agg["fee_yuan"] += float(t.get("fee_yuan", 0.0) or 0.0)
        agg["slip_yuan"] += float(t.get("slip_yuan", 0.0) or 0.0)
        agg["realized_yuan"] += float(t.get("realized_yuan", 0.0) or 0.0)
        agg["n_forced"] += int(t.get("forced", 0) or 0)
        if t.get("side") == "open":
            agg["n_open"] += 1
            if int(t.get("direction", 0)) > 0:
                agg["open_long"] += lots
            else:
                agg["open_short"] += lots
        else:
            agg["n_close"] += 1
            if int(t.get("direction", 0)) > 0:
                agg["close_long"] += lots
            else:
                agg["close_short"] += lots
    return agg


# next 档开仓时遇到这些【临时性】约束，挂单保持 pending 顺延等约束缓解（而非直接拒单丢弃）；
# 而"无合约乘数/策略目标不足1手"这类确定性约束才立即 rejected。
RETRYABLE_SKIP = {"同时持仓数达上限", "可用资金不足1手", "板块名义上限",
                    "策略目标不足1手(高价品种/名义权重偏小)"}


# =========================== 纸面经纪 ===========================

def _locked(fn):
    """第103轮：RLock 互斥装饰器——撮合/报告读取方法与 paper_ticker 线程互斥。

    用 with self._lock 包住整个方法体（RLock 同线程可重入，防自死锁）；
    装饰的全部方法需在 __init__ 中 restore() 之前初始化 self._lock。"""

    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)

    return wrapper


class PaperBroker:
    """实时轮询驱动的纸面经纪；内部组合一个 portfolio.Portfolio 作为账户内核。

    第102轮扩展：支持多账户（name + 独立 db_path + priority 参与方式 + 期权撮合）。
    - priority: futures_first / equal / option_first / option_only（小资金优先期权）
    - opt_premium_ratio: 单笔买方权利金占权益上限
    - stop_loss_ratio: 权利金止损线（激进 0.40 / 基准 0.50 / 保守 0.60）
    - futures_max / options_max: equal/option_first 档同时持有的期货/期权数上限
    """

    def __init__(self, *, db=None, db_path=None, name=None,
                 equity0=None, fill_mode=None, entry_score=None,
                 exit_score=None, sizing=None, margin_table=None, fee_table=None,
                 sector_of=None, slip_rate=None, restore=True, clock=None, owner_fn=None,
                 risk_sizing=None, risk_gross=None, circuit=None,
                 # 第102轮多账户扩展：仓位/期权/优先策略（默认 None → 回退 config）
                 per_symbol=None, max_symbol_weight=None, max_sector_weight=None,
                 max_concurrent=None, risk_liquidate=None, risk_safe=None,
                 opt_premium_ratio=None, stop_loss_ratio=None,
                 priority="futures_first", futures_max=None, options_max=None,
                 priority_expiry_days=None, target_basis=None,
                 max_daily_orders=None, max_active_per_sym=None):   # 第141轮：账户级委托流控覆盖
        # 第102轮：独立数据库文件（每账户独立 SQLite）
        if db_path and db is None:
            import storage as _storage  # noqa: F401
            self.db = _storage.MonitorDB(path=db_path)
        else:
            self.db = db
        self.name = str(name or "").strip() if name else None  # 第102轮：账户名（如"10万_基准"）
        self.priority = (priority or "futures_first").strip()
        self.futures_max = futures_max  # equal/option_first 档：期货同时持仓上限（None=共用 max_concurrent）
        self.options_max = options_max  # 续：期权同时持仓上限（None=无额外限制，option_only 档按此限制）
        lo = int(priority_expiry_days) if priority_expiry_days is not None else None
        self.opt_expiry_days = lo if lo and lo >= 1 else (3 if self.priority == "option_only" else 5)
        self.fill_mode = fill_mode or getattr(config, "PAPER_FILL_MODE", "next")
        if self.fill_mode not in ("close", "next"):
            self.fill_mode = "next"
        self.entry_score = entry_score if entry_score is not None else config.PAPER_ENTRY_SCORE
        self.exit_score = exit_score if exit_score is not None else config.PAPER_EXIT_SCORE
        self.opt_premium_ratio = (opt_premium_ratio if opt_premium_ratio is not None
                                  else getattr(config, "PAPER_OPT_PREMIUM_MAX_RATIO", 0.03))
        self.stop_loss_ratio = (stop_loss_ratio if stop_loss_ratio is not None
                                else getattr(config, "PAPER_OPT_STOP_LOSS_RATIO", 0.50))
        self.slip_rate = slip_rate if slip_rate is not None else config.PAPER_SLIP_RATE
        self._cur_quote = {}    # G14 接线：on_cycle 时注入当前轮 by_quote，供 _ob_exec_price 读 bid/ask
        self._clock = clock or (lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        # 实时平今/平昨判定：时间戳->交易所结算交易日（可注入，测试零网络零日历依赖）
        self._owner_fn = owner_fn or _default_owner_of_ts
        self._sector_of = sector_of if sector_of is not None else sector_map()
        # 账户内核：费率/保证金表复用既有加载器（文件缺失返回空表，Portfolio 内部兜底）
        self.fee_table = fee_table if fee_table is not None else load_fee_schedule()
        self.margin_table = margin_table if margin_table is not None else \
            portfolio_mod.load_margin_schedule()
        equity0 = equity0 if equity0 is not None else config.PAPER_EQUITY0
        self.pf = portfolio_mod.Portfolio(
            equity0, self.margin_table, self.fee_table,
            sizing=sizing or config.PAPER_SIZING,
            per_symbol=per_symbol if per_symbol is not None else config.PAPER_PER_SYMBOL,
            risk_per_trade=config.PAPER_RISK_PER_TRADE,
            max_symbol_weight=(max_symbol_weight if max_symbol_weight is not None
                               else config.PAPER_MAX_SYMBOL_WEIGHT),
            max_sector_weight=(max_sector_weight if max_sector_weight is not None
                               else config.PAPER_MAX_SECTOR_WEIGHT),
            risk_liquidate=(risk_liquidate if risk_liquidate is not None
                            else config.PAPER_RISK_LIQUIDATE),
            risk_safe=(risk_safe if risk_safe is not None else config.PAPER_RISK_SAFE),
            default_margin=config.PAPER_DEFAULT_MARGIN,
            max_concurrent=max_concurrent if max_concurrent is not None else config.PAPER_MAX_CONCURRENT,
            fee_rate=config.PAPER_FEE_RATE, slip_rate=self.slip_rate,
            use_real_fees=config.PAPER_USE_REAL_FEES, sector_of=self._sector_of,
            # 第41轮 G26续：风险型横截面sizing能力位（默认None=逐字节等价旧版）；实时权重源（K线历史
            # 协方差）尚未接线，须先在组合回测影子对照达标后再议，未注入权重时内核自动回退等名义。
            risk_sizing=risk_sizing,
            risk_gross=config.PRS_GROSS if risk_gross is None else risk_gross,
            target_basis=target_basis)
        self.pending = {}          # sym -> [order, ...] next 档待成交队列（先平后开）
        self._open_seq = {}        # sym -> 开仓序号（生成 pos_ref）
        self.pos_ref = {}          # sym -> 当前持仓 pos_ref
        # 第140轮 R3：日订单总数 / 每品种活动委托上限（防信号抖动频繁开平）
        self._daily_orders = {}    # 交易日 -> {(sym): 当日累计委托数}
        self._daily_orders_day = None
        self._max_daily_orders = max_daily_orders or getattr(config, "PAPER_MAX_DAILY_ORDERS", 30)
        self._max_active_per_sym = max_active_per_sym or getattr(config, "PAPER_MAX_ACTIVE_ORDERS_PER_SYM", 3)
        # G1续（第63轮）：内存级 OMS 全状态委托台账（id->最新委托快照）与成交回报流水，
        # 让纯内存模式也能像 DB 模式一样回溯任意终态委托/全部成交；纯增量、不改变既有撮合输出。
        self._orders_by_id = {}
        self.fill_ledger = []
        self.last_summary = None   # 最近一轮 on_cycle 结果
        self.restored = False
        # G5④（第48轮）组合层单日浮亏熔断：显式传入优先；否则仅在 config 开启且 paper_halt 模式才挂。
        # 默认 CIRCUIT_ACTION='observe' -> self.breaker=None，阶段B不过滤任何委托、成交逐字节等价旧版。
        if circuit is not None:
            self.breaker = circuit
        elif getattr(config, "CIRCUIT_ENABLED", False) and \
                getattr(config, "CIRCUIT_ACTION", circuit_breaker.OBSERVE) in \
                (circuit_breaker.PAPER_HALT, circuit_breaker.PAPER_DELEVER):
            self.breaker = circuit_breaker.CircuitBreaker.from_config()
        else:
            self.breaker = None
        self._last_circuit = None
        # 第95轮：最后已知合约映射（回补探测前空合约，修复 paper_account 合约列显示）
        self._known_contract: dict = {}   # sym -> (contract_code, main_month)
        # 第102轮：期权持仓（pos_ref -> {record}）与独立资金池
        self.opt_positions = {}      # pos_ref -> 期权持仓 record
        self.opt_realized = 0.0      # 期权已实现净盈亏（含手续费）
        self.opt_fees = 0.0          # 期权累计手续费
        self.opt_skipped = []        # 期权被拒/跳过的原因记录
        self.opt_last_summary = None # 最近一轮 on_cycle_options 结果
        self.opt_equity0 = float(equity0)  # 期权资金池初始资金
        # 第103轮：线程安全锁（RLock 允许同线程重入，防自死锁；项目惯例见 trade_calendar/storage/FundamentalFetcher）
        # 必须在 restore() 之前初始化（restore 内部可能调用带 _locked 装饰的方法）。
        self._lock = threading.RLock()
        if restore and self.db is not None:
            self.restore()

    # ---------------- 持久化辅助（db 为空时全部静默跳过，纯内存可跑） ----------------

    def _ins_order(self, order):
        if self.db is None:
            order["id"] = order.get("id") or (id(order) & 0x7fffffff)
            self._orders_by_id[order["id"]] = dict(order)
            return order["id"]
        try:
            order["id"] = self.db.insert_paper_order(order)
            self._orders_by_id[order["id"]] = dict(order)
            return order["id"]
        except Exception:
            return None

    def _upd_order(self, order, **fields):
        order.update(fields)
        if order.get("id"):
            self._orders_by_id[order["id"]] = dict(order)
        if self.db is not None and order.get("id"):
            try:
                self.db.update_paper_order(order["id"], **fields)
            except Exception:
                pass

    def _ins_trade(self, t):
        self.fill_ledger.append(dict(t))     # 内存成交回报流水（DB 模式同时落库）
        if self.db is None:
            return None
        try:
            return self.db.insert_paper_trade(t)
        except Exception:
            return None

    # ---------------- 订单/成交构造 ----------------

    def _make_order(self, ts, row, action, side, direction, signal_price, status="pending"):
        return {"ts": ts, "sym": row["sym"], "name": row.get("name", ""),
                "sector": row.get("cat", ""), "action": action, "side": side,
                "direction": direction, "lots": 0, "signal_price": signal_price,
                "score": row.get("score"), "band": score_band_name(row.get("score") or 0.0),
                "fill_mode": self.fill_mode, "status": status,
                "fill_ts": "", "fill_price": None, "raw_price": None,
                "reason": "", "order_ref": "", "pos_ref": self.pos_ref.get(row["sym"], ""),
                "contract_code": row.get("contract_code") or self._known_contract.get(row["sym"], ("", ""))[0],
                "main_month": row.get("main_month") or self._known_contract.get(row["sym"], ("", ""))[1],
                "raw": {"atr": row.get("atr")}}

    def _roll_daily_orders(self, ts):
        """按结算日归零当日订单计数（跨交易日自动重置）。"""
        day = str(ts or "")[:10]
        if self._daily_orders_day != day:
            self._daily_orders = {}
            self._daily_orders_day = day

    def _r3_allow_new_orders(self, ts, sym, orders):
        """第140轮 R3：日订单总数上限（对开/反手开新腿生效）。

        返回 (可下单的 orders, 被拦截的订单)；平仓/反手平仓腿不受限（只防频繁开仓）。
        超限时记入 pf.skipped 并诚实标注。活动委托上限在 _enqueue 入队时闸口执行。"""
        open_legs = [o for o in orders if (o.get("action") or "").startswith("open") or
                     (o.get("action") or "").startswith("reverse_open")]
        if not open_legs:
            return orders, []
        keep, blocked = [], []
        self._roll_daily_orders(ts)
        day = str(ts or "")[:10]
        for o in orders:
            if (o.get("action") or "").startswith("open") or (o.get("action") or "").startswith("reverse_open"):
                daily_n = self._daily_orders.get(day, {}).get(sym, 0)
                if daily_n >= self._max_daily_orders:
                    blocked.append(o)
                    self.pf.skipped.append({
                        "dt": ts, "sym": sym,
                        "reason": "R3委托流控(当日累计%d/上限%d)" % (
                            daily_n, self._max_daily_orders),
                        "available": self.pf.available(),
                        "price": float(o.get("signal_price") or 0.0)})
                    continue
            keep.append(o)
        return keep, blocked

    def _next_pos_ref(self, sym):
        n = self._open_seq.get(sym, 0) + 1
        self._open_seq[sym] = n
        return f"{sym}-{n}"

    # ---------------- 单腿成交（真正调用 Portfolio） ----------------

    def _owner_of(self, ts):
        """时间戳->交易所结算交易日，owner_fn 自身异常/判不了一律 None（调用方保守按平昨）。"""
        try:
            return self._owner_fn(ts)
        except Exception:
            return None

    def _close_leg(self, pos, ts):
        """实时平今/平昨判定：开仓与平仓同属一个交易所结算交易日=平今(today)，否则平昨(close)。
        判不了（开仓 owner 缺失/时间戳或日历不可用）一律保守按平昨，与第27轮口径逐值一致。"""
        cur_owner = self._owner_of(ts)
        entry_owner = getattr(pos, "entry_owner", None)
        if cur_owner is not None and entry_owner is not None and entry_owner == cur_owner:
            return "today"
        return "close"

    # ---------------- 单腿成交（真正调用 Portfolio） ----------------
    def _ob_exec_price(self, raw_price, side, by_quote, sym):
        """G14 盘口保守成交价（第124轮接线）：真实 bid/ask 存在且有效时，
        买=ask、卖=bid（真实价差内成交，比统一比例滑点更保守、口径更真实）；
        无盘口/非法档位回落 apply_slip 统一比例。返回 (fill_price, use_ob)。
        """
        if getattr(config, "PAPER_SLIP_USE_ORDERBOOK", True):
            q = (by_quote or {}).get(sym) or {}
            bid = float(q.get("bid") or 0.0)
            ask = float(q.get("ask") or 0.0)
            latest = float(q.get("latest") or q.get("price") or 0.0)
            if bid > 0 and ask > 0 and ask >= bid:
                px = ask if side == "buy" else bid
                if px > 0:
                    return px, True
            if side == "buy" and ask > 0:
                return ask, True
            if side == "sell" and bid > 0:
                return bid, True
            if latest > 0:
                raw_price = raw_price if raw_price > 0 else latest
        return apply_slip(raw_price, side, self.slip_rate), False

    def _fill_leg(self, ts, order, raw_price):
        """把一条委托腿按盘面价 raw_price（内含滑点后）成交，返回 trade dict；失败返回 None。"""
        sym = order["sym"]
        pf = self.pf
        action = order["action"]
        is_open = action in ("open", "reverse_open")
        direction = order["direction"]
        side = order["side"]
        fill_price, _use_ob = self._ob_exec_price(
            raw_price, side, getattr(self, "_cur_quote", None), sym)
        if fill_price <= 0:
            self._upd_order(order, status="blocked", reason="无价/非法价，顺延")
            return None
        atr = (order.get("raw") or {}).get("atr")

        if is_open:
            pos = pf.open(sym, order["name"], order["sector"], direction, fill_price, ts,
                          atr=atr, score=order.get("score"), owner=self._owner_of(ts),
                          contract_code=order.get("contract_code") or "",
                          main_month=order.get("main_month") or "")
            if pos is None:
                why = pf.skipped[-1]["reason"] if pf.skipped else "未成交"
                # next 档临时约束（持仓上限/资金/板块）：保持挂单顺延，等约束缓解再成交
                if self.fill_mode == "next" and why in RETRYABLE_SKIP:
                    order["status"] = "pending"
                    order["reason"] = why + "，挂单顺延"
                    return None
                self._upd_order(order, status="rejected", raw_price=raw_price, reason=why)
                return None
            pos_ref = self._next_pos_ref(sym)
            self.pos_ref[sym] = pos_ref
            order["pos_ref"] = pos_ref
            lots = pos.lots
            notional = fill_price * pos.mult * lots
            slip_yuan = abs(fill_price - raw_price) * pos.mult * lots
            t = {"ts": ts, "pos_ref": pos_ref, "sym": sym, "name": order["name"],
                 "sector": order["sector"], "side": "open",
                 "dir_text": "多" if direction > 0 else "空", "direction": direction,
                 "lots": lots, "price": fill_price, "raw_price": raw_price,
                 "notional": notional, "slip_yuan": slip_yuan,
                 "fee_yuan": pos.open_fee_yuan, "realized_yuan": 0.0, "leg": "开仓",
                 "reason": "信号开仓" if action == "open" else "反手开仓",
                 "forced": 0, "order_id": order.get("id"), "entry_ts": ts,
                 "entry_price": fill_price, "score": order.get("score"),
                 "margin_rate": pos.margin_rate,
                 "contract_code": order.get("contract_code") or pos.contract_code or "",
                 "main_month": order.get("main_month") or pos.main_month or ""}
            self._ins_trade(t)
            self._upd_order(order, status="filled", fill_ts=ts, fill_price=fill_price,
                            raw_price=raw_price, lots=lots, pos_ref=pos_ref)
            return t

        # 平仓腿
        held = pf.positions.get(sym)
        if held is None:
            self._upd_order(order, status="cancelled", raw_price=raw_price,
                            reason="已无持仓，撤单")
            return None
        close_leg = self._close_leg(held, ts)
        reduce_lots = order.get("reduce_lots")     # G5④ delever 部分减仓：None=整仓全平（旧路径）
        close_reason = order.get("close_reason") or \
            ("信号离场" if action == "close" else "反手平仓")
        rec = pf.close(sym, fill_price, ts, close_reason, leg=close_leg,
                       reduce_lots=reduce_lots)
        if rec is None:
            self._upd_order(order, status="blocked", raw_price=raw_price, reason="平仓失败，顺延")
            return None
        if rec.get("remaining", 0) <= 0:
            self.pos_ref.pop(sym, None)           # 整仓平完才清 pos_ref；部分减仓保留持仓
        lots = rec["lots"]
        mult = held.mult
        notional = fill_price * mult * lots
        slip_yuan = abs(fill_price - raw_price) * mult * lots
        t = {"ts": ts, "pos_ref": rec.get("pos_ref") or order.get("pos_ref", ""),
             "sym": sym, "name": order["name"], "sector": order["sector"], "side": "close",
             "dir_text": rec["dir"], "direction": held.direction, "lots": lots,
             "price": fill_price, "raw_price": raw_price, "notional": notional,
             "slip_yuan": slip_yuan, "fee_yuan": rec["close_fee_yuan"],
             "realized_yuan": rec["net_yuan"], "leg": rec["leg"],
             "reason": rec["reason"], "forced": 1 if rec.get("forced") else 0,
             "order_id": order.get("id"), "entry_ts": str(rec["entry_dt"]),
             "entry_price": rec["entry_px"], "score": rec.get("entry_score"),
             "margin_rate": rec.get("margin_rate"),
             "contract_code": getattr(held, "contract_code", "") or order.get("contract_code") or "",
             "main_month": getattr(held, "main_month", "") or order.get("main_month") or ""}
        self._ins_trade(t)
        self._upd_order(order, status="filled", fill_ts=ts, fill_price=fill_price,
                        raw_price=raw_price, lots=lots)
        return t

    # ---------------- next 档：阶段A 成交上一轮挂单 ----------------

    def _process_pending(self, ts, by_sym, by_quote):
        events = []
        for sym in list(self.pending.keys()):
            queue = self.pending.get(sym) or []
            row = by_sym.get(sym)
            raw_price = float(row["price"]) if row and float(row.get("price") or 0) > 0 else 0.0
            idx = 0
            while idx < len(queue):
                order = queue[idx]
                if raw_price <= 0:
                    order["reason"] = "本轮无有效价，挂单顺延"
                    break   # 无价：整组队列保留，等下一轮
                move = (config.FUTURES_LIMIT_MOVE or {}).get(sym)
                if locked_at_quote(by_quote.get(sym), move, order["side"] == "buy"):
                    order["reason"] = "锁板封死，挂单顺延"
                    break   # 锁板：保留队列顺延（先平后开的后续腿也一并等）
                t = self._fill_leg(ts, order, raw_price)
                if t is None and order["status"] in ("blocked", "pending"):
                    break   # 锁板/无价/临时约束：整组队列保留顺延（后续腿也一起等）
                queue.pop(idx)       # filled / rejected / cancelled 才出队
                if t:
                    events.append(t)
            if not queue:
                self.pending.pop(sym, None)
        return events

    def _enqueue(self, orders):
        for o in orders:
            # 第140轮 R3：活动委托上限（每品种 pending 队列长度）——入队闸口
            if self._max_active_per_sym and (o.get("action") or "").startswith(("open", "reverse_open")):
                sym = o["sym"]
                if len(self.pending.get(sym) or []) >= self._max_active_per_sym:
                    self.pf.skipped.append({
                        "dt": o.get("ts", ""), "sym": sym,
                        "reason": "R3委托流控(活动委托%d/上限%d)" % (
                            len(self.pending.get(sym) or []), self._max_active_per_sym),
                        "available": self.pf.available(),
                        "price": float(o.get("signal_price") or 0.0)})
                    continue
            self.pending.setdefault(o["sym"], []).append(o)
            self._ins_order(o)

    def _cancel_pending(self, sym, reason="新信号覆盖旧挂单"):
        for o in self.pending.pop(sym, []):
            self._upd_order(o, status="cancelled", reason=reason)

    # ---------------- 信号决策：阶段B ----------------

    def _decide(self, ts, row):
        sym = row["sym"]
        score = row.get("score")
        held = self.pf.positions.get(sym)
        held_dir = held.direction if held is not None else 0
        want, action = want_position(score, held_dir, self.entry_score, self.exit_score)
        if action == "hold":
            return []
        raw_price = float(row.get("price") or 0.0)
        orders = []
        if action == "reverse":
            # 先平后开两条腿
            orders.append(self._make_order(ts, row, "reverse_close",
                                           _side_of(held_dir, "close"), held_dir, raw_price))
            orders.append(self._make_order(ts, row, "reverse_open",
                                           _side_of(want, "open"), want, raw_price))
        elif action == "open":
            orders.append(self._make_order(ts, row, "open",
                                           _side_of(want, "open"), want, raw_price))
        else:  # close
            orders.append(self._make_order(ts, row, "close",
                                           _side_of(held_dir, "close"), held_dir, raw_price))
        return orders

    # ---------------- 强平：阶段C ----------------

    def _liquidate(self, ts, by_sym):
        events, ord_events = [], []
        pf = self.pf

        def price_getter(sym):
            held = pf.positions.get(sym)
            row = by_sym.get(sym)
            raw = float(row["price"]) if row else 0.0
            if raw <= 0:
                raw = pf._last_prices.get(sym, held.entry_price if held else 0.0)
            side = "sell" if held and held.direction > 0 else "buy"
            return apply_slip(raw, side, self.slip_rate)

        def leg_getter(sym):
            held_now = pf.positions.get(sym)
            return self._close_leg(held_now, ts) if held_now is not None else "close"

        # 触发线/安全线两段式状态机在 Portfolio 内；强平同样按实时 owner 判平今/平昨
        liq = pf.liquidate(ts, price_getter, leg_getter=leg_getter)
        for rec in liq:
            sym = rec["sym"]
            # 第112轮修复：强平 close 必须带上被平持仓的 pos_ref——否则 restore 的
            # paper_open_position_trades 按 pos_ref 配对时永远配不上，每次进程重启都恢复出
            # 幽灵持仓并再次强平（"开1手平N次"），破坏开平对应守恒。在 pop 之前取值。
            pos_ref = self.pos_ref.get(sym, "") or ""
            self.pos_ref.pop(sym, None)
            self._cancel_pending(sym, "风控强平撤销挂单")
            held_dir = 1 if rec["dir"] == "多" else -1
            held = pf.positions.get(sym)
            order = self._make_order(ts, {"sym": sym, "name": rec.get("name", ""),
                                          "cat": rec.get("sector", ""), "score": rec.get("entry_score")},
                                     "liquidate", _side_of(held_dir, "close"), held_dir,
                                     rec["exit_px"], status="filled")
            order.update({"fill_ts": ts, "fill_price": rec["exit_px"],
                          "raw_price": rec["exit_px"], "lots": rec["lots"],
                          "pos_ref": pos_ref, "reason": rec["reason"],
                          "contract_code": getattr(held, "contract_code", "") or "",
                          "main_month": getattr(held, "main_month", "") or ""})
            self._ins_order(order)
            mult = pf.mult_of(sym)
            t = {"ts": ts, "pos_ref": pos_ref, "sym": sym, "name": rec.get("name", ""),
                 "sector": rec.get("sector", ""), "side": "close", "dir_text": rec["dir"],
                 "direction": held_dir, "lots": rec["lots"], "price": rec["exit_px"],
                 "raw_price": rec["exit_px"], "notional": rec["exit_px"] * mult * rec["lots"],
                 "slip_yuan": 0.0, "fee_yuan": rec["close_fee_yuan"],
                 "realized_yuan": rec["net_yuan"], "leg": rec["leg"], "reason": rec["reason"],
                 "forced": 1, "order_id": order.get("id"), "entry_ts": str(rec["entry_dt"]),
                 "entry_price": rec["entry_px"], "score": rec.get("entry_score"),
                 "margin_rate": rec.get("margin_rate"),
                 "contract_code": rec.get("contract_code") or order.get("contract_code") or "",
                 "main_month": rec.get("main_month") or order.get("main_month") or ""}
            self._ins_trade(t)
            events.append(t)
            ord_events.append(order)
        return events

    # 第95轮：一次性 DB 补仓——用信号表最新 contract/main_month 回填纸面空合约行
    def _backfill_empty_contracts(self):
        if self.db is None:
            return
        try:
            latest = {r["sym"]: (r["contract_code"], r["main_month"])
                      for r in self.db.conn.execute(
                          "SELECT sym, contract_code, main_month FROM signals s"
                          " WHERE contract_code IS NOT NULL AND contract_code != ''"
                          " AND ts = (SELECT MAX(ts) FROM signals WHERE sym = s.sym)").fetchall()
                      if r["contract_code"]}
            if not latest:
                return
            n = 0
            for sym, (cc, mm) in latest.items():
                for tbl in ("paper_trades", "paper_orders"):
                    n += self.db.conn.execute(
                        "UPDATE %s SET contract_code=?, main_month=? "
                        "WHERE sym=? AND (contract_code IS NULL OR contract_code='')" % tbl,
                        (cc, mm, sym)).rowcount or 0
            self.db.conn.commit()
            self._known_contract.update(latest)
            if n:
                LOG.info("纸面合约补仓: 回填 %d 行空 contract_code (from signals)", n)
        except Exception:
            pass


    # ---------------- G5④ 阶段A2：paper_delever 自动减仓（只平不反向） ----------------

    def _delever_cut(self, ts, by_sym, by_quote):
        """断路器处于 delever 档且模式=paper_delever 时，对当前持仓按比例自动减仓。
        决策来自断路器（其上一轮阶段D更新，本轮价成交=严格晚一轮、无未来）；当日各品种只减一次
        （breaker 侧 _delever_done，日切清空）；不足1手不减、只减不清、绝不反向；锁板/无价顺延不登记。
        返回 (trades, orders)；非 paper_delever 模式或未到 delever 档一律 ([],[])，默认路径零影响。"""
        trades, ord_events = [], []
        b = self.breaker
        if b is None or b.action_mode != circuit_breaker.PAPER_DELEVER:
            return trades, ord_events
        pf = self.pf
        brief = [{"sym": s, "direction": p.direction, "lots": p.lots}
                 for s, p in pf.positions.items()]
        plan = b.delever_targets(brief)
        if not plan:
            return trades, ord_events
        for item in plan:
            sym = item["sym"]
            held = pf.positions.get(sym)
            if held is None:
                b.mark_delevered(sym)          # 已无持仓，免下轮重复计算
                continue
            row = by_sym.get(sym) or {}
            raw = float(row.get("price") or 0.0)
            if raw <= 0:
                raw = float(pf._last_prices.get(sym, held.entry_price) or 0.0)
            if raw <= 0:
                continue                       # 本轮无价：不成交、不登记，下轮重试
            side = _side_of(held.direction, "close")
            move = (config.FUTURES_LIMIT_MOVE or {}).get(sym)
            if locked_at_quote(by_quote.get(sym), move, side == "buy"):
                continue                       # 锁板封死：顺延、不登记
            order = self._make_order(
                ts, {"sym": sym, "name": held.name, "cat": held.sector,
                     "score": getattr(held, "score", None)},
                "close", side, held.direction, raw, status="pending")
            order["reduce_lots"] = item["reduce_lots"]
            order["close_reason"] = "熔断自动减仓"
            self._ins_order(order)
            t = self._fill_leg(ts, order, raw)
            if t:
                b.mark_delevered(sym)          # 成交后登记，当日不再减该品种
                trades.append(t)
                ord_events.append(order)
        return trades, ord_events

    # ---------------- 权益快照：阶段D ----------------

    def _snapshot(self, ts, prices_raw):
        pf = self.pf
        pf.record(ts, prices_raw)
        point = pf.curve[-1]
        positions = {s: {"dir": p.direction, "lots": p.lots, "entry": p.entry_price,
                         "sector": p.sector, "score": p.score}
                     for s, p in sorted(pf.positions.items())}
        # 第104轮统一资金池：期权盈亏/占用并入权益口径（缺链时用最近期权快照近似）。
        if getattr(config, "PAPER_UNIFIED_POOL", True):
            ua = self.unified_account()
            snap = {"ts": ts, "static_equity": ua["static"], "float_pnl": ua["float_pnl"],
                    "equity": ua["equity"], "margin_used": ua["margin_used"],
                    "available": ua["available"], "risk_degree": ua["risk_degree"],
                    "drawdown": point["drawdown"],
                    "n_positions": point["npos"] + sum(
                        1 for r in self.opt_positions.values() if r.get("status") == "open"),
                    "realized": pf.realized + (self.opt_realized - self.opt_fees),
                    "fees_paid": pf.fees_paid + self.opt_fees,
                    "n_trades": len(pf.closed), "positions": positions}
        else:
            snap = {"ts": ts, "static_equity": point["static"], "float_pnl": point["float"],
                    "equity": point["equity"], "margin_used": point["margin"],
                    "available": point["available"], "risk_degree": point["risk"],
                    "drawdown": point["drawdown"], "n_positions": point["npos"],
                    "realized": pf.realized, "fees_paid": pf.fees_paid,
                    "n_trades": len(pf.closed), "positions": positions}
        if self.db is not None:
            try:
                self.db.insert_paper_equity(snap)
            except Exception:
                pass
        return snap

    # ---------------- 主入口：每轮一次 ----------------

    @_locked
    def on_cycle(self, ts, fut_rows, quotes=None):
        """驱动一轮纸面撮合。ts 为本轮时间戳字符串；fut_rows 为 analyzer 结果列表；
        quotes 为 {code: 实时行情dict}（提供 prev_settle/high/low 供锁板判定，可空）。
        返回本轮 summary（orders/trades/liquidations/snapshot 计数与快照）。"""
        ts = str(ts or self._clock())[:19]
        quotes = quotes or {}
        by_sym, by_quote, prices_raw = {}, {}, {}
        for row in fut_rows:
            sym = (row.get("sym") or "").upper()
            if not sym:
                continue
            # 第95轮：从有合约的 row 更新最后已知映射（早周期空合约回补用）
            if row.get("contract_code"):
                self._known_contract[sym] = (row["contract_code"] or "", row.get("main_month") or "")
            by_sym[sym] = row
            px = float(row.get("price") or 0.0)
            if px > 0:
                prices_raw[sym] = px
            q = quotes.get(row.get("code")) or {}
            if q:
                by_quote[sym] = q
        self._cur_quote = by_quote   # G14 接线：供 _ob_exec_price 读取真实 bid/ask

        cycle_orders, cycle_trades = [], []
        # 阶段A：next 档先成交上一轮挂单（先平后开，严格晚于信号）
        cycle_trades += self._process_pending(ts, by_sym, by_quote)
        # 阶段A2：G5④ paper_delever 自动减仓（断路器决策来自上一轮阶段D，本轮价成交=无未来）
        n_delever = 0
        if self.breaker is not None:
            dv_trades, dv_orders = self._delever_cut(ts, by_sym, by_quote)
            cycle_trades += dv_trades
            cycle_orders += dv_orders
            n_delever = len(dv_trades)
        # 阶段B：本轮信号决策
        for row in fut_rows:
            sym = (row.get("sym") or "").upper()
            if not sym:
                continue
            orders = self._decide(ts, row)
            # 第140轮 R3：委托流控（日订单总数/活动委托上限）——先于 R1/R2 过滤但只拦新开仓
            if orders:
                orders, _blocked = self._r3_allow_new_orders(ts, sym, orders)
                if orders:
                    for o in orders:
                        if (o.get("action") or "").startswith("open") or (o.get("action") or "").startswith("reverse_open"):
                            self._roll_daily_orders(ts)
                            day = str(ts or "")[:10]
                            self._daily_orders.setdefault(day, {}).setdefault(sym, 0)
                            self._daily_orders[day][sym] += 1
            # 第140轮 R1：委托级风控上链——row["risk"] 为 veto（risk_gate.apply_gate 已写入，
            # 管道同源于 run_cycle）时，剔除开仓/反手开仓腿（保留平仓/反手平仓腿）。风控只拦新仓不拦离场。
            if orders and (row.get("risk") or {}).get("level") == "veto":
                kept = [o for o in orders if (o.get("action") or "") in ("close", "reverse_close")]
                if len(kept) != len(orders):
                    dropped = [o for o in orders if o not in kept]
                    self.rg_veto_skips = getattr(self, "rg_veto_skips", 0) + len(dropped)
                    self.pf.skipped.append({
                        "dt": ts, "sym": sym,
                        "reason": "风控veto拦截(%s)" % "；".join((row.get("risk") or {}).get("veto") or []),
                        "available": self.pf.available(), "price": float(row.get("price") or 0.0)})
                    orders = kept
            # G5④ 组合熔断：断路器停开时剔除开新仓腿（保留平仓腿）；breaker=None(默认observe)时原样返回
            if self.breaker is not None:
                orders = circuit_breaker.filter_orders(orders, self.breaker.open_allowed())
            if self.fill_mode == "next":
                new_sig = [(o["action"], o["direction"]) for o in orders]
                old_q = self.pending.get(sym)
                old_sig = [(o["action"], o["direction"]) for o in old_q] if old_q else None
                if not orders:
                    # 本轮无开/平/反手意图（信号转中性/迟滞带内）：撤销该品种遗留挂单，不再排队
                    if old_q:
                        for o in old_q:
                            self._upd_order(o, status="cancelled", reason="信号转中性/消失，撤单")
                        self.pending.pop(sym, None)
                    continue
                if old_sig == new_sig:
                    # 同一意图的挂单仍在排队（等锁板打开/资金/仓位空出），不撤不重挂、避免委托虚增
                    continue
                # 意图变了（如反手/转离场）：先撤旧挂单再挂新
                self._cancel_pending(sym)
                for o in orders:
                    o["status"] = "pending"
                self._enqueue(orders)
                cycle_orders += orders
                continue
            if not orders:
                continue
            # close：当轮立即成交；锁板则 blocked，下轮信号自然重试（等价顺延）
            raw = float(row.get("price") or 0.0)
            for o in orders:
                o["status"] = "pending"
                self._ins_order(o)
                move = (config.FUTURES_LIMIT_MOVE or {}).get(sym)
                if raw <= 0:
                    self._upd_order(o, status="blocked", reason="本轮无有效价")
                elif locked_at_quote(by_quote.get(sym), move, o["side"] == "buy"):
                    self._upd_order(o, status="blocked", raw_price=raw, reason="锁板封死，顺延")
                else:
                    t = self._fill_leg(ts, o, raw)
                    if t:
                        cycle_trades.append(t)
                cycle_orders.append(o)
        # 阶段C：盯市后风控强平（被动成交也含滑点）
        cycle_trades += self._liquidate(ts, by_sym)
        # 阶段D：权益快照（一轮一条，同 ts 覆盖、重跑幂等）
        snap = self._snapshot(ts, prices_raw)
        # G5④ 用本轮最新权益更新熔断状态（供下一轮阶段B使用，严格无未来函数）；observe/None 时不挂
        if self.breaker is not None:
            self._last_circuit = self.breaker.update(
                snap["ts"], snap["equity"], risk_degree=snap.get("risk_degree"),
                n_positions=snap.get("n_positions"))
        n_pending = sum(len(q) for q in self.pending.values())
        summary = {"ts": ts, "snapshot": snap, "n_orders": len(cycle_orders),
                   "n_trades": len(cycle_trades), "n_pending": n_pending,
                   "n_positions": len(self.pf.positions), "n_delever": n_delever,
                   "n_skipped": len(self.pf.skipped), "circuit": self._last_circuit,
                   "orders": cycle_orders, "trades": cycle_trades,
                   # 第136轮：ERC 影子落账标记（报告/对账可用；未开启=等名义不标注）
                   "risk_sizing": self.pf.risk_sizing if getattr(self.pf, "risk_sizing", None) else None,
                   "risk_meta": getattr(self.pf, "risk_meta", None)}
        self.last_summary = summary
        return summary

    # ================= 第102轮：期权纸面撮合 =================

    def _find_chain_leg(self, chain_map, sym, strike, cp):
        """从 chain_map（{(sym,yy,mm): chain}）找 sym 的链中指定行权价/看涨跌的腿。
        返回 (chain, leg) 或 (None, None)。"""
        strike = float(strike or 0)
        best = None
        for key, chain in (chain_map or {}).items():
            if key[0].upper() != str(sym).upper():
                continue
            legs = chain.get("puts") if cp == "put" else chain.get("calls")
            for leg in legs or []:
                if float(leg.get("strike") or 0) == strike:
                    best = chain
                    return chain, leg
        return best, None

    @staticmethod
    def _leg_price(leg, side):
        """期权成交参考价：买方用 ask（滑点保守用 ask），可回退 mid/last。
        这里 side='buy' 表示买开/买平，用 ask；否则用 bid。"""
        ask = leg.get("ask")
        bid = leg.get("bid")
        last = leg.get("last")
        mid = None
        if ask is not None and bid is not None and ask > 0 and bid > 0:
            mid = (ask + bid) / 2.0
        if side == "buy":
            return ask if ask and ask > 0 else (mid or last or 0)
        return bid if bid and bid > 0 else (mid or last or 0)

    def _opt_px(self, pos_rec, chain_map):
        """期权持仓盯市参考价：公开链优先，缺时回退记录开仓价。"""
        strike = pos_rec.get("strike")
        cp = pos_rec.get("cp")
        sym = pos_rec.get("sym")
        chain, leg = self._find_chain_leg(chain_map, sym, strike, cp)
        if leg:
            px = self._leg_price(leg, "sell" if pos_rec.get("direction", 1) > 0 else "buy")
            if px and px > 0:
                return px
        return float(pos_rec.get("fill_prem") or 0)

    def _opt_margin_of(self, pos_rec, px=None):
        """期权持仓保证金：买方=当前权利金×乘数（无杠杆），持仓时按权利金占用。"""
        px = px if px is not None else pos_rec.get("fill_prem") or 0
        multiplier = float(pos_rec.get("multiplier") or self.pf.mult_of(pos_rec.get("sym") or ""))
        return px * multiplier * int(pos_rec.get("lots") or 1)

    def _opt_fee_yuan(self, pos_rec):
        """期权单边手续费（按权利金×乘数×费率）。"""
        prem = float(pos_rec.get("fill_prem") or 0)
        multiplier = float(pos_rec.get("multiplier") or self.pf.mult_of(pos_rec.get("sym") or ""))
        lots = int(pos_rec.get("lots") or 1)
        return prem * multiplier * lots * self.pf.fee_rate

    def _opt_float_pnl(self, chain_map=None):
        """在途期权浮盈（纯函数）：有链逐仓盯市；缺链回退最近期权快照浮盈（滞后≤1轮，Policy A 口径）。"""
        if not chain_map:
            ols = getattr(self, "opt_last_summary", None) or {}
            osnap = ols.get("snapshot")
            if osnap and osnap.get("float_pnl") is not None:
                return float(osnap["float_pnl"])
            return 0.0
        total = 0.0
        for rec in self.opt_positions.values():
            if rec.get("status") != "open":
                continue
            px = self._opt_px(rec, chain_map)
            total += (px - float(rec.get("fill_prem") or 0)) \
                * float(rec.get("multiplier") or self.pf.mult_of(rec.get("sym") or "")) \
                * int(rec.get("lots") or 1)
        return total

    def _opt_premium_locked(self, chain_map=None):
        """在途期权权利金占用（纯函数）：买方=当前权利金×乘数×手数；缺链回退最近快照占用。"""
        if not chain_map:
            ols = getattr(self, "opt_last_summary", None) or {}
            osnap = ols.get("snapshot")
            if osnap and osnap.get("margin_used") is not None:
                return float(osnap["margin_used"])
            return 0.0
        total = 0.0
        for rec in self.opt_positions.values():
            if rec.get("status") != "open":
                continue
            px = self._opt_px(rec, chain_map)
            total += self._opt_margin_of(rec, px)
        return total

    def _opt_net_pnl(self, chain_map=None):
        """期权净贡献（纯函数）：已实现(扣费) + 在途浮盈 —— 统一池叠加到期货基座上的增量。"""
        return (self.opt_realized - self.opt_fees) + self._opt_float_pnl(chain_map)

    def unified_account(self, chain_map=None):
        """统一资金池账户（纯函数）：一个钱包两张持仓表。

        期权盈亏/占用叠加到期货 Portfolio 上；初始资本只计一次（pf.equity0）。
        返回统一 equity/static/float_pnl/margin_used/available/risk_degree，
        附带期权净贡献与占用供明细展示。期货强平触发仍走 pf 独立风险度（本方法仅统一口径）。"""
        chain_map = chain_map or {}
        pf = self.pf
        opt_net = self._opt_net_pnl(chain_map)
        opt_locked = self._opt_premium_locked(chain_map)
        equity = pf.equity() + opt_net
        static = pf.static_equity() + (self.opt_realized - self.opt_fees)
        margin = pf.margin_used() + opt_locked
        available = max(0.0, equity - margin)
        risk = (margin / equity) if equity > 1e-9 else 0.0
        return {"equity": equity, "static": static,
                "float_pnl": pf.float_pnl() + self._opt_float_pnl(chain_map),
                "margin_used": margin, "available": available,
                "risk_degree": risk, "opt_net_pnl": opt_net,
                "opt_premium_locked": opt_locked}

    def _opt_summary(self, ts, chain_map=None):
        """期权权益快照（期权明细表：paper_option_equity，含占用/浮盈，供统一池汇总与看板明细）。"""
        chain_map = chain_map or {}
        eq = float(getattr(self, "opt_equity0", config.PAPER_EQUITY0)) \
            + self.opt_realized - self.opt_fees
        float_pnl = self._opt_float_pnl(chain_map)
        margin_used = self._opt_premium_locked(chain_map)
        n_open = sum(1 for r in self.opt_positions.values() if r.get("status") == "open")
        equity = eq + float_pnl
        risk = (margin_used / equity) if equity > 0 else 0.0
        snap = {"ts": ts, "static_equity": eq, "float_pnl": float_pnl, "equity": equity,
                "margin_used": margin_used, "available": max(0.0, equity - margin_used),
                "risk_degree": risk, "drawdown": 0.0, "n_positions": n_open,
                "realized": self.opt_realized, "fees_paid": self.opt_fees}
        if self.db is not None and self.name and hasattr(self.db, "insert_paper_option_equity"):
            try:
                self.db.insert_paper_option_equity(self.name, snap)
            except Exception:
                pass
        return snap

    @_locked
    def on_cycle_options(self, ts, strat_rows, chain_map=None, fut_rows=None, opt_rows=None):
        """驱动一轮期权纸面撮合。
        strat_rows: option_strategies.recommend 结果列表（有 all_pass/legs/_decide 用途）。
        chain_map: {(sym,yy,mm): chain}（新浪T链，成交价/盯市）。
        fut_rows: 本轮期货行（供综合分取当前 score —— 期权离场/入场用标的综合分）。
        opt_rows: 第110轮新增 —— analyze_option 结果列表（单腿期权，all_pass/kind/K/yy/mm/opt_code/prem 字段齐全）。
                  此前只消费 strat_rows、analyze_option 的 9515 次 all_pass 从未接进撮合（结构性断链）；
                  本轮接入 option_only / option_first 档，作为"单腿买方"信号源（其余档位不启用，保持纪律）。
        返回本期权 summary dict（含 snapshot/n_buy/n_close/n_skipped）。"""
        ts = str(ts or self._clock())[:19]
        chain_map = chain_map or {}
        score_map = {}
        for row in (fut_rows or []):
            sym = (row.get("sym") or "").upper()
            if sym:
                score_map[sym] = float(row.get("score") or 0.0)
        cycle_trades = []
        n_buy, n_close, n_skipped = 0, 0, 0

        # ---------- A. 先处理平仓（三触发：分线反转/权利金止损/到期） ----------
        to_close = []
        for pos_ref, rec in list(self.opt_positions.items()):
            if rec.get("status") != "open":
                continue
            sym = str(rec.get("sym") or "")
            score = abs(score_map.get(sym.upper(), rec.get("score") or 0))
            sk, cp = rec.get("strike"), rec.get("cp")
            chain, leg = self._find_chain_leg(chain_map, sym, sk, cp)
            px = self._leg_price(leg, "sell") if leg else None
            if px is None:
                px = self._opt_px(rec, chain_map)
            ret = (px - float(rec.get("fill_prem") or 0)) / float(rec.get("fill_prem") or 1)
            # 触发1：标的综合分反转（低于 exit_score → 平）
            if score < self.exit_score:
                to_close.append((pos_ref, "综合分反转(%.1f<%.1f)" % (score, self.exit_score)))
            # 触发2：权利金止损（跌幅 ≥ stop_loss_ratio）
            elif ret <= -self.stop_loss_ratio:
                to_close.append((pos_ref, "权利金止损(%.0f%%)" % (ret * 100)))
            # 触发3：剩余到期天数过近（到期前 opt_expiry_days 天自动平仓）
            else:
                dleft = rec.get("days_left")
                if dleft is not None and 0 <= dleft <= self.opt_expiry_days:
                    to_close.append((pos_ref, "临近到期(%d天)" % int(dleft)))
        for pos_ref, reason in to_close:
            rec = self.opt_positions.pop(pos_ref, None)
            if not rec:
                continue
            px_sell = self._opt_px(rec, chain_map)
            px_buy = float(rec.get("fill_prem") or 0)
            multiplier = float(rec.get("multiplier") or self.pf.mult_of(rec.get("sym") or ""))
            lots = int(rec.get("lots") or 1)
            fee = self._opt_fee_yuan(rec)
            # 第121轮修复：原 realized 直接把平仓费扣进 opt_realized（净额），而所有对外汇总
            # 又是 opt_realized - opt_fees（opt_fees 含该笔平仓费），导致平仓费双重扣减、净值被低估。
            # 修复：opt_realized 累计毛利（不含费），手续费统一进 opt_fees，对外净值 = opt_realized - opt_fees。
            gross = (px_sell - px_buy) * multiplier * lots
            realized = gross - fee      # 单笔净值（含该笔平仓费，落库展示用）
            self.opt_realized += gross  # 毛利进累计
            self.opt_fees += fee
            t = dict(rec)
            t.update({"ts": ts, "action": "close", "side": "close", "status": "closed",
                      "reason": reason, "realized_yuan": realized,
                      "fill_prem": px_sell, "fill_ts": ts})
            if self.db is not None and self.name and hasattr(self.db, "insert_paper_option_trade"):
                try:
                    self.db.insert_paper_option_trade(self.name, t)
                except Exception:
                    pass
            n_close += 1
            cycle_trades.append(t)

        # ---------- B. 新一轮买入（单腿买方，与 priority 配合） ----------
        for strat in (strat_rows or []):
            if not strat.get("all_pass"):
                continue
            legs = strat.get("legs") or []
            if len(legs) != 1:               # 只做单腿
                continue
            leg = legs[0]
            if not leg.get("buy"):           # 只做买方
                continue
            cp = (leg.get("kind") or "").lower()
            if cp not in ("call", "put"):
                continue
            sym = (strat.get("variety") or "").upper()
            if not sym:
                continue
            # 数量上限（options_max 优先，其次用 opt 存量判断）
            if self.options_max is not None and len(self.opt_positions) >= self.options_max:
                self.opt_skipped.append({"ts": ts, "sym": sym, "reason": "期权持仓数达上限"})
                n_skipped += 1
                continue
            if any(r.get("sym", "").upper() == sym for r in self.opt_positions.values() if r.get("status") == "open"):
                continue  # 已持有同品种期权，不加仓
            chain, _leg = self._find_chain_leg(chain_map, sym, leg.get("K"), cp)
            if not _leg:
                self.opt_skipped.append({"ts": ts, "sym": sym, "reason": "链上无该行权价"})
                n_skipped += 1
                continue
            px = self._leg_price(_leg, "buy")
            if not px or px <= 0:
                self.opt_skipped.append({"ts": ts, "sym": sym, "reason": "无有效期权价"})
                n_skipped += 1
                continue
            # 期权乘数（复用期货乘数）
            multiplier = self.pf.mult_of(sym)
            if multiplier <= 0:
                self.opt_skipped.append({"ts": ts, "sym": sym, "reason": "无合约乘数"})
                n_skipped += 1
                continue
            premium = px * multiplier
            # 第104轮统一资金池：权利金从统一可用资金里扣 + 集中度分母改为统一权益；
            # 回退（PAPER_UNIFIED_POOL=False）时仍按期权池 opt_equity0 检查（旧行为逐字节）。
            if getattr(config, "PAPER_UNIFIED_POOL", True):
                ua = self.unified_account()
                if premium > ua["available"]:
                    self.opt_skipped.append({"ts": ts, "sym": sym, "reason": "统一可用资金不足(%.0f>%.0f)"
                                             % (premium, ua["available"])})
                    n_skipped += 1
                    continue
                if premium > ua["equity"] * self.opt_premium_ratio:
                    self.opt_skipped.append({"ts": ts, "sym": sym, "reason": "权利金超统一权益上限(%.0f>%.0f)"
                                             % (premium, ua["equity"] * self.opt_premium_ratio)})
                    n_skipped += 1
                    continue
            else:
                eq0 = float(getattr(self, "opt_equity0", config.PAPER_EQUITY0))
                if premium > eq0 * self.opt_premium_ratio:
                    self.opt_skipped.append({"ts": ts, "sym": sym, "reason": "权利金超上限(%.0f>%d)"
                                             % (premium, eq0 * self.opt_premium_ratio)})
                    n_skipped += 1
                    continue
            pos_ref = "o%s-%d" % (sym, int(getattr(self, "_opt_seq", 0)) + 1)
            self._opt_seq = int(getattr(self, "_opt_seq", 0)) + 1
            rec = {"ts": ts, "pos_ref": pos_ref, "sym": sym, "name": strat.get("name", ""),
                   "variety": strat.get("variety", ""), "action": "open", "side": "open",
                   "direction": 1, "lots": 1, "strike": leg.get("K"), "cp": cp,
                   "expiry": strat.get("month_label", ""), "entry_prem": px,
                   "fill_prem": px, "fill_ts": ts, "option_code": leg.get("code", ""),
                   "legs": [leg], "notional": premium, "margin_used": premium,
                   "fee_yuan": self._opt_fee_yuan({"fill_prem": px, "sym": sym, "lots": 1,
                                                   "multiplier": multiplier}),
                   "realized_yuan": 0.0, "status": "open", "entry_score": strat.get("net"),
                   "fill_mode": self.fill_mode, "multiplier": multiplier,
                   "days_left": strat.get("days_left"), "score": strat.get("net")}
            # 存储为 trade 记录（account 维度；side=open）
            if self.db is not None and self.name and hasattr(self.db, "insert_paper_option_trade"):
                try:
                    self.db.insert_paper_option_trade(self.name, rec)
                except Exception:
                    pass
            self.opt_positions[pos_ref] = rec
            self.opt_fees += rec.get("fee_yuan", 0.0)
            n_buy += 1
            cycle_trades.append(dict(rec))

        # ---------- B2（第110轮）：option_only / option_first 档接入 analyze_option 单腿信号 ----------
        # 背景：此前只消费 strat_rows（option_strategies.recommend），而 recommend 从不输出单腿买入
        # （目录优先级单腿最低、永远被多腿优选），on_cycle_options 又只执行单腿 -> 期权撮合永远 0 成交。
        # analyze_option（main.py 第 4 步）单腿 all_pass=1 有 9515 次历史通过、字段 K/kind/yy/mm/opt_code/prem 齐全，
        # 但从未被传入撮合。本轮接入：仅 option_only / option_first 档启用，走既有链价/统一资金池双重预算约束。
        if self.priority in ("option_only", "option_first") and opt_rows:
            for ao in opt_rows:
                if not ao or not ao.get("all_pass"):
                    continue
                ao_sym = str((ao.get("name") or "").upper())
                if not ao_sym:
                    continue
                kind = str(ao.get("kind") or "").lower()
                if kind not in ("call", "put"):
                    continue
                # 已持有同品种期权不加仓（与 strat_rows 路径同一纪律）
                if any(r.get("sym", "").upper() == ao_sym for r in self.opt_positions.values() if r.get("status") == "open"):
                    continue
                chain, leg = self._find_chain_leg(chain_map, ao_sym, ao.get("K"), kind)
                if not leg:
                    self.opt_skipped.append({"ts": ts, "sym": ao_sym, "reason": "单腿分析-链上无该行权价"})
                    n_skipped += 1
                    continue
                px = self._leg_price(leg, "buy")
                if not px or px <= 0:
                    self.opt_skipped.append({"ts": ts, "sym": ao_sym, "reason": "单腿分析-无有效期权价"})
                    n_skipped += 1
                    continue
                multiplier = self.pf.mult_of(ao_sym)
                if multiplier <= 0:
                    self.opt_skipped.append({"ts": ts, "sym": ao_sym, "reason": "单腿分析-无合约乘数"})
                    n_skipped += 1
                    continue
                premium = px * multiplier
                ua = self.unified_account() if getattr(config, "PAPER_UNIFIED_POOL", True) else None
                if ua is not None:
                    if premium > ua["available"]:
                        self.opt_skipped.append({"ts": ts, "sym": ao_sym, "reason": "单腿分析-统一可用资金不足(%.0f>%.0f)"
                                                 % (premium, ua["available"])})
                        n_skipped += 1
                        continue
                    if premium > ua["equity"] * self.opt_premium_ratio:
                        self.opt_skipped.append({"ts": ts, "sym": ao_sym, "reason": "单腿分析-权利金超统一权益上限(%.0f>%.0f)"
                                                 % (premium, ua["equity"] * self.opt_premium_ratio)})
                        n_skipped += 1
                        continue
                pos_ref = "o%s-%d" % (ao_sym, int(getattr(self, "_opt_seq", 0)) + 1)
                self._opt_seq = int(getattr(self, "_opt_seq", 0)) + 1
                rec = {"ts": ts, "pos_ref": pos_ref, "sym": ao_sym, "name": ao.get("name", ""),
                       "variety": ao.get("name", ""), "action": "open", "side": "open",
                       "direction": 1, "lots": 1, "strike": ao.get("K"),
                       "cp": kind, "expiry": ao.get("month_label", ""), "entry_prem": px,
                       "fill_prem": px, "fill_ts": ts, "option_code": ao.get("opt_code", ""),
                       "legs": [], "notional": premium, "margin_used": premium,
                       "fee_yuan": self._opt_fee_yuan({"fill_prem": px, "sym": ao_sym, "lots": 1,
                                                       "multiplier": multiplier}),
                       "realized_yuan": 0.0, "status": "open", "entry_score": ao.get("score"),
                       "fill_mode": self.fill_mode, "multiplier": multiplier,
                       "days_left": ao.get("days"), "score": ao.get("score"),
                       "src": "analyze_option"}
                if self.db is not None and self.name and hasattr(self.db, "insert_paper_option_trade"):
                    try:
                        self.db.insert_paper_option_trade(self.name, rec)
                    except Exception:
                        pass
                self.opt_positions[pos_ref] = rec
                self.opt_fees += rec.get("fee_yuan", 0.0)
                n_buy += 1
                cycle_trades.append(dict(rec))

        snap = self._opt_summary(ts, chain_map)
        summary = {"ts": ts, "snapshot": snap, "n_buy": n_buy, "n_close": n_close,
                   "n_skipped": n_skipped, "n_positions": snap.get("n_positions", 0),
                   "trades": cycle_trades, "positions": list(self.opt_positions.values())}
        self.opt_last_summary = summary
        return summary

    # ---------------- 重启恢复 ----------------

    def restore(self):
        """从三表重建账户内核：未平仓成交重建持仓、历史成交重建已实现盈亏/手续费、pending 重建挂单。"""
        if self.db is None or self.restored:
            return False
        pf = self.pf
        open_trades = self.db.paper_open_position_trades() if hasattr(self.db, "paper_open_position_trades") else []
        realized_sum, fees_sum = 0.0, 0.0
        try:
            realized_sum, fees_sum = self.db.paper_realized_fees()
        except Exception:
            pass
        open_fees = 0.0
        for t in open_trades:
            sym = t["sym"]
            mult = pf.mult_of(sym)
            direction = t["direction"]
            pos = portfolio_mod.Position(
                sym=sym, name=t["name"], sector=t["sector"], direction=direction,
                lots=int(t["lots"]), entry_price=t["price"], entry_dt=t["ts"],
                stop=None, target=None, atr=None, score=t.get("score"),
                margin_rate=t.get("margin_rate") or pf.margin_rate_of(sym),
                mult=mult, open_fee_yuan=t.get("fee_yuan") or 0.0,
                entry_owner=self._owner_of(t["ts"]), entry_i=0, block=0, calib_mult=1.0,
                contract_code=t.get("contract_code") or "",
                main_month=t.get("main_month") or "")
            pf.positions[sym] = pos
            pf._last_prices[sym] = t["price"]
            self.pos_ref[sym] = t["pos_ref"]
            open_fees += t.get("fee_yuan") or 0.0
            # 第95轮：开仓成交更新最后已知映射
            if t.get("contract_code"):
                self._known_contract[sym] = (t["contract_code"] or "", t.get("main_month") or "")
            suffix = int(t["pos_ref"].split("-")[-1]) if str(t.get("pos_ref", "")).split("-")[-1].isdigit() else 0
            self._open_seq[sym] = max(self._open_seq.get(sym, 0), suffix)
        # 已实现净盈亏：已平仓腿的净盈亏合计；仍持仓开仓费在开仓时已付、尚未计入任何平仓腿，需补扣
        pf.realized = float(realized_sum) - open_fees
        pf.fees_paid = float(fees_sum)
        # 恢复 pending（每 sym 最近一条仍 pending 的委托队列，next 档语义连续）
        try:
            for o in self.db.paper_orders_recent(500)[::-1]:
                if o.get("status") != "pending":
                    continue
                sym = o["sym"]
                if sym in pf.positions and o["action"] in ("open",):
                    continue
                order = dict(o)
                order.pop("id", None)
                db_id = o.get("id")
                order["id"] = db_id
                self.pending.setdefault(sym, []).insert(0, order)
        except Exception:
            pass
        # G1续：重启后回填内存 OMS 台账与成交回报流水，使 orders_view/fills_view 跨进程连续
        try:
            for o in self.db.paper_orders_recent(5000):
                if o.get("id"):
                    self._orders_by_id[o["id"]] = dict(o)
        except Exception:
            pass
        try:
            self.fill_ledger = [dict(t) for t in self.db.paper_trades_recent(100000)][::-1] \
                if hasattr(self.db, "paper_trades_recent") else self.fill_ledger
        except Exception:
            pass
        # 第95轮：一次性补仓——paper_trades/orders 空合约用最新信号同 sym 回填，DB 持久化
        self._backfill_empty_contracts()
        # 内存持仓同步补仓结果（restore 先建 Position、后补 DB，需回写内存对象）
        try:
            for p in pf.positions.values():
                if not getattr(p, "contract_code", ""):
                    cc, mm = self._known_contract.get(p.sym, ("", ""))
                    p.contract_code, p.main_month = cc, mm
        except Exception:
            pass
        # 第102轮：恢复期权持仓（按账户过滤，重启续跑）
        if self.name and self.db is not None and hasattr(self.db, "paper_open_option_positions"):
            try:
                for t in self.db.paper_open_option_positions(self.name):
                    pos_ref = t["pos_ref"]
                    rec = dict(t)
                    rec["status"] = "open"
                    self.opt_positions[pos_ref] = rec
            except Exception:
                pass
            try:
                self.opt_realized, self.opt_fees = self.db.paper_option_realized_fees(self.name)
            except Exception:
                pass

        self.restored = True
        return True

    # ---------------- 第41轮 G26续：风险型目标权重透传（实时协方差源接线前为空操作安全回退） ----------------

    def set_risk_weights(self, wmap, meta=None):
        """向账户内核注入横截面目标权重 {sym: 目标名义占比}；未注入/未开启 risk_sizing 时等价旧版。"""
        self.pf.set_risk_weights(wmap, meta)

    # ---------------- 账户摘要/视图（第28轮报告+看板用） ----------------

    def order_status_counts(self):
        """委托全生命周期计数（以三表为准；纯内存/查询失败返回全 0 dict）。
        注意区分：pending=在途排队（临时约束/锁板等缓解后仍会成交，不是拒单）；
        rejected=确定性拒单（资金不足1手/无乘数等）；blocked=曾锁板/无价阻塞（close档）。"""
        out = {"pending": 0, "filled": 0, "blocked": 0, "rejected": 0, "cancelled": 0}
        if self.db is not None and hasattr(self.db, "paper_order_status_counts"):
            try:
                out.update({k: int(v) for k, v in self.db.paper_order_status_counts().items()})
            except Exception:
                pass
        else:
            # 纯内存模式：只剩在途 pending 可统计（终态委托不留内存）
            out["pending"] = sum(len(q) for q in self.pending.values())
        return out

    @_locked
    def positions_view(self):
        """当前持仓明细行（含最新价/浮动盈亏/占用保证金/开仓结算交易日），供 paper_account.txt。"""
        pf = self.pf
        rows = []
        for sym in sorted(pf.positions):
            p = pf.positions[sym]
            last = float(pf._last_prices.get(sym, p.entry_price) or 0.0)
            mult = p.mult or pf.mult_of(sym)
            float_yuan = p.direction * (last - p.entry_price) * mult * p.lots
            margin = last * mult * p.lots * p.margin_rate
            rows.append({"sym": sym, "name": p.name or "", "sector": p.sector or "",
                         "dir": "多" if p.direction > 0 else "空", "direction": p.direction,
                         "lots": p.lots, "entry_dt": str(p.entry_dt), "entry_price": p.entry_price,
                         "last": last, "float_yuan": float_yuan, "margin": margin,
                         "entry_owner": str(getattr(p, "entry_owner", "") or ""),
                         "score": p.score,
                         "contract_code": getattr(p, "contract_code", "") or "",
                         "main_month": getattr(p, "main_month", "") or ""})
        return rows

    @_locked
    def pending_view(self):
        """当前在途挂单明细（拍平成行列表），供 paper_account.txt。"""
        rows = []
        for sym in sorted(self.pending):
            for o in self.pending[sym]:
                rows.append({"sym": sym, "name": o.get("name", ""), "action": o.get("action", ""),
                             "side": o.get("side", ""), "direction": o.get("direction", 0),
                             "ts": o.get("ts", ""), "signal_price": o.get("signal_price"),
                             "score": o.get("score"), "reason": o.get("reason", ""),
                             "contract_code": o.get("contract_code") or "",
                             "main_month": o.get("main_month") or ""})
        return rows

    @_locked
    def account_summary(self):
        pf = self.pf
        perf = None
        if pf.curve:
            perf = pf.performance()
        # 第104轮统一资金池：权益/可用/风险 = 期货+期权合并口径；期货风控触发不变。
        ua = self.unified_account() if getattr(config, "PAPER_UNIFIED_POOL", True) else None
        out = {"equity0": pf.equity0, "static": ua["static"] if ua else pf.static_equity(),
               "equity": ua["equity"] if ua else pf.equity(),
               "float_pnl": ua["float_pnl"] if ua else pf.float_pnl(),
               "realized": pf.realized + (self.opt_realized - self.opt_fees) if ua else pf.realized,
               "fees_paid": pf.fees_paid + self.opt_fees if ua else pf.fees_paid,
               "margin_used": ua["margin_used"] if ua else pf.margin_used(),
               "available": ua["available"] if ua else pf.available(),
               "risk_degree": ua["risk_degree"] if ua else pf.risk_degree(),
               "n_positions": len(pf.positions),
               "n_pending": sum(len(q) for q in self.pending.values()),
               "n_closed": len(pf.closed), "n_liquidations": len(pf.liquidations),
               "n_skipped": len(pf.skipped), "status": self.order_status_counts(),
               "fill_mode": self.fill_mode,
               "pending": {s: [dict(o) for o in q]
                           for s, q in self.pending.items()},
               "performance": perf,
               # 第102轮：期权持仓明细（统一池时仅作展示明细；独立池时含独立权益）
               "opt": {"n_positions": len(self.opt_positions),
                       "realized": self.opt_realized, "fees_paid": self.opt_fees,
                       "n_skipped": len(self.opt_skipped)}}
        # 期权权益快照（统一池时 opt_equity 保留供 report 摘要兼容，非独立账户权益）
        ols = getattr(self, "opt_last_summary", None) or {}
        osnap = ols.get("snapshot")
        if osnap:
            out["opt_equity"] = osnap.get("equity")
            out["opt_float_pnl"] = osnap.get("float_pnl")
            out["opt_equity0"] = osnap.get("static_equity")
        else:
            out["opt_equity"] = None
            out["opt_float_pnl"] = None
            out["opt_equity0"] = getattr(self, "opt_equity0", None)
        return out

    # ---------------- G1续（第63轮）：OMS 全状态台账 / 主动撤单 / 成交回报 / 持仓对账 ----------------

    def orders_view(self, status=None, sym=None):
        """OMS 全状态委托台账（不只在途 pending；含 filled/rejected/cancelled/blocked 终态）。

        内存模式取 _orders_by_id，DB 模式以三表为准回填；可按 status/sym 过滤，按 (ts,id) 升序。"""
        rows = list(self._orders_by_id.values())
        if self.db is not None and hasattr(self.db, "paper_orders_recent"):
            try:
                rows = self.db.paper_orders_recent(100000)[::-1]
            except Exception:
                pass
        out = []
        for o in rows:
            if status is not None and o.get("status") != status:
                continue
            if sym is not None and o.get("sym") != sym:
                continue
            out.append(dict(o))
        out.sort(key=lambda o: (str(o.get("ts", "")), int(o.get("id") or 0)))
        return out

    def cancel_order(self, *, sym=None, order_id=None, reason="手动撤单(OMS)"):
        """主动撤销在途挂单：按 order_id 或某 sym 整组撤；返回撤掉的委托数。只动 pending，不碰已成交。"""
        n = 0
        if order_id is not None:
            for s in list(self.pending):
                keep = []
                for o in self.pending[s]:
                    if o.get("id") == order_id:
                        self._upd_order(o, status="cancelled", reason=reason)
                        n += 1
                    else:
                        keep.append(o)
                if keep:
                    self.pending[s] = keep
                else:
                    self.pending.pop(s, None)
            return n
        if sym is None:
            return 0
        for o in self.pending.pop(sym, []):
            self._upd_order(o, status="cancelled", reason=reason)
            n += 1
        return n

    def fills_view(self, sym=None, side=None, since=None):
        """成交回报流水（全部 fill，可按品种/开平/时间戳下界过滤），时间升序。"""
        rows = self.fill_ledger
        if self.db is not None and hasattr(self.db, "paper_trades_recent") and not rows:
            try:
                rows = self.db.paper_trades_recent(100000)[::-1]
            except Exception:
                rows = self.fill_ledger
        out = []
        for t in rows:
            if sym is not None and t.get("sym") != sym:
                continue
            if side is not None and t.get("side") != side:
                continue
            if since is not None and str(t.get("ts", "")) < str(since):
                continue
            out.append(dict(t))
        out.sort(key=lambda t: (str(t.get("ts", "")), int(t.get("id") or 0)))
        return out

    @_locked
    def fill_report(self, since=None):
        """成交回报汇总：since 以来（默认全部）成交的笔数/手数/名义/费/滑点/已实现/多空开平拆分。"""
        return aggregate_fills(self.fills_view(since=since))

    def _internal_position_set(self):
        return {r["sym"]: {"direction": r["direction"], "lots": int(r["lots"]),
                           "entry_price": float(r["entry_price"] or 0.0)}
                for r in self.positions_view()}

    def reconcile_positions(self, external, price_tol=1e-6):
        """内部持仓 vs 外部/托管台账 {sym:{direction,lots,entry_price}} 对账，返回 matched/breaks 明细。"""
        return reconcile_position_sets(self._internal_position_set(), external, price_tol)

    def reconcile_against_db(self, price_tol=1e-9):
        """自洽对账：用三表里"仍持仓的开仓腿"重建外部持仓，与内存账户内核逐品种核对（捕获持久化漂移）。

        db 为空（纯内存）返回 None。"""
        if self.db is None or not hasattr(self.db, "paper_open_position_trades"):
            return None
        external = {}
        for t in self.db.paper_open_position_trades():
            external[t["sym"]] = {"direction": int(t["direction"]), "lots": int(t["lots"]),
                                  "entry_price": float(t["price"])}
        return self.reconcile_positions(external, price_tol)


# =========================== 合成自检（零网络） ===========================

def _row(sym, name, cat, score, price, atr=10.0, prev=None, hi=None, lo=None):
    """构造 analyzer 结果行（只取 PaperBroker 用到的字段）。"""
    row = {"sym": sym, "name": name, "cat": cat, "code": sym + "0",
           "score": score, "price": price, "atr": atr}
    return row


def _quote(price, prev, move, locked=False):
    """构造实时行情；locked=True 时高/低也贴板。"""
    if locked:
        px = prev * (1 + move)
        return {"latest": px, "prev_settle": prev, "high": px, "low": px}
    return {"latest": price, "prev_settle": prev, "high": price * 1.002, "low": price * 0.998}


def selftest():
    """零网络合成断言：开/持/反手/离场/锁板顺延/双边费+滑点/强平/next晚于信号/重启恢复。"""
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))
        if not cond:
            raise AssertionError("FAIL: " + name)

    # 1) 三阈值迟滞
    ck("空仓低分不动", want_position(1.0, 0, 4.0, 2.0) == (0, "hold"))
    ck("空仓强分开多", want_position(5.0, 0, 4.0, 2.0) == (1, "open"))
    ck("持多回中性不离场(>=exit)", want_position(2.5, 1, 4.0, 2.0) == (1, "hold"))
    ck("持多跌回中性带离场", want_position(1.0, 1, 4.0, 2.0) == (0, "close"))
    ck("持多转强空=反手", want_position(-5.0, 1, 4.0, 2.0) == (-1, "reverse"))

    # 2) 锁板判定
    move = 0.05
    ck("涨停封死买不进", locked_at_quote(_quote(None, 100, move, True), move, True))
    ck("未封板可买", not locked_at_quote(_quote(101, 100, move), move, True))
    ck("缺昨结放行", not locked_at_quote({"latest": 101}, move, True))

    # 3) 滑点方向
    ck("买价上滑", abs(apply_slip(100.0, "buy", 0.0001) - 100.01) < 1e-9)
    ck("卖价下滑", abs(apply_slip(100.0, "sell", 0.0001) - 99.99) < 1e-9)

    # 4) next 档：成交严格晚于信号（内存账户，给足资金/大名义上限避免被约束链拒单）
    import config as _cfg
    _cfg.PAPER_PER_SYMBOL = 0.05
    _cfg.PAPER_MAX_SYMBOL_WEIGHT = 1.0
    _cfg.PAPER_MAX_SECTOR_WEIGHT = 1.0
    _cfg.PAPER_MAX_CONCURRENT = 64
    pb = PaperBroker(db=None, fill_mode="next", equity0=10_000_000,
                     slip_rate=0.0001, restore=False)
    s1 = pb.on_cycle("2026-09-02 09:05:00", [_row("RB", "螺纹钢", "黑色", 5.0, 3000.0)])
    ck("next信号轮只挂单不成交", s1["n_trades"] == 0 and s1["n_pending"] == 1
       and s1["n_positions"] == 0)
    s2 = pb.on_cycle("2026-09-02 09:10:00", [_row("RB", "螺纹钢", "黑色", 5.0, 3010.0)])
    ck("next次轮才成交", s2["n_trades"] == 1 and s2["n_positions"] == 1)
    o = s2["orders"]
    tr = s2["trades"][0]
    ck("成交晚于信号(挂单ts=09:05)", pb.pf.positions["RB"].entry_dt == "2026-09-02 09:10:00")
    ck("开仓含买入滑点", tr["price"] > tr["raw_price"] and tr["slip_yuan"] > 0)
    ck("开仓扣了手续费", tr["fee_yuan"] > 0)

    # 5) 反手先平后开（next 档跨一轮），平仓双边成本
    s3 = pb.on_cycle("2026-09-02 09:15:00", [_row("RB", "螺纹钢", "黑色", -5.0, 3005.0)])
    ck("反手轮挂平+开两腿", s3["n_pending"] == 2 and pb.pf.positions.get("RB") is not None)
    s4 = pb.on_cycle("2026-09-02 09:20:00", [_row("RB", "螺纹钢", "黑色", -5.0, 2990.0)])
    ck("反手后持空", pb.pf.positions["RB"].direction == -1)
    closed = pb.pf.closed
    ck("反手产生一笔平仓(含双边费)", len(closed) == 1 and closed[0]["net_yuan"] != 0)

    # 6) 离场
    pb.on_cycle("2026-09-02 09:25:00", [_row("RB", "螺纹钢", "黑色", -5.0, 2990.0)])
    s6 = pb.on_cycle("2026-09-02 09:30:00", [_row("RB", "螺纹钢", "黑色", 1.0, 2991.0)])
    ck("离场轮挂平仓", s6["n_pending"] == 1)
    s7 = pb.on_cycle("2026-09-02 09:35:00", [_row("RB", "螺纹钢", "黑色", 1.0, 2992.0)])
    ck("次轮平掉空仓", len(pb.pf.positions) == 0 and len(pb.pf.closed) == 2)

    # 7) close 档：信号轮当轮立即成交
    pbc = PaperBroker(db=None, fill_mode="close", equity0=10_000_000,
                      slip_rate=0.0, restore=False)
    sc = pbc.on_cycle("2026-09-02 10:00:00", [_row("CU", "铜", "有色", 6.0, 70000.0)])
    ck("close当轮成交", sc["n_trades"] == 1 and len(pbc.pf.positions) == 1)

    # 8) 锁板顺延（close 档当轮 blocked，不成交）
    pbl = PaperBroker(db=None, fill_mode="close", equity0=10_000_000,
                      slip_rate=0.0, restore=False)
    locked_q = {"CU0": _quote(None, 70000.0, 0.09, locked=True)}
    row = _row("CU", "铜", "有色", 6.0, 70000.0 * 1.09)
    sl = pbl.on_cycle("2026-09-02 10:05:00", [row], locked_q)
    ck("涨停锁死开多被blocked", sl["orders"][0]["status"] == "blocked"
       and len(pbl.pf.positions) == 0)

    # 9) 强平：把强平线压到 0，下一轮必触发，持仓被砍
    pbf = PaperBroker(db=None, fill_mode="close", equity0=10_000_000,
                      slip_rate=0.0, restore=False)
    pbf.on_cycle("2026-09-02 11:00:00", [_row("AU", "黄金", "贵金属", 6.0, 500.0)])
    ck("强平前有持仓", len(pbf.pf.positions) == 1)
    pbf.pf.risk_liquidate = 0.0
    pbf.pf.risk_safe = 0.0
    sf = pbf.on_cycle("2026-09-02 11:05:00", [_row("AU", "黄金", "贵金属", 6.0, 500.0)])
    ck("触发强平后空仓", len(pbf.pf.positions) == 0 and len(pbf.pf.liquidations) >= 1
       and any(t["forced"] for t in sf["trades"]))

    # 10) 资金不足拒单（1手都买不起 -> rejected，不持仓）
    pbp = PaperBroker(db=None, fill_mode="close", equity0=2000.0,
                      slip_rate=0.0, restore=False)
    sp = pbp.on_cycle("2026-09-02 13:30:00", [_row("CU", "铜", "有色", 6.0, 70000.0)])
    ck("资金不足拒单", len(pbp.pf.positions) == 0 and sp["orders"][0]["status"] == "rejected")

    # 11) 实时平今/平昨 owner 判定（注入确定性 owner_fn 与显式费率表，零日历/网络依赖）
    from datetime import date as _date
    def _fee_row(mult, today_free):
        return {"multiplier": mult, "open_amt_rate": 1e-4, "open_per_lot": 3.0,
                "close_amt_rate": 1e-4, "close_per_lot": 3.0,
                "today_amt_rate": 0.0 if today_free else 1e-4,
                "today_per_lot": 0.0 if today_free else 3.0}
    own_map = {"2026-09-02 10:00:00": _date(2026, 9, 2),
               "2026-09-02 14:00:00": _date(2026, 9, 2),
               "2026-09-03 10:00:00": _date(2026, 9, 3)}
    pbo = PaperBroker(db=None, fill_mode="close", equity0=10_000_000, slip_rate=0.0,
                      restore=False, margin_table={"RB": {"broker_margin": 0.1,
                      "limit_basic": 0.05, "multiplier": 10}},
                      fee_table={"RB": _fee_row(10, True)}, sector_of={"RB": "黑色"},
                      owner_fn=lambda ts: own_map.get(str(ts)[:19]))
    pbo.on_cycle("2026-09-02 10:00:00", [_row("RB", "螺纹钢", "黑色", 5.0, 3000.0)])
    s_today = pbo.on_cycle("2026-09-02 14:00:00", [_row("RB", "螺纹钢", "黑色", 1.0, 3000.0)])
    rec_today = pbo.pf.closed[-1]
    ck("同一结算交易日=平今", rec_today["leg"] == "平今" and rec_today["close_fee_yuan"] == 0.0)
    own_map["2026-09-02 14:00:00"] = _date(2026, 9, 2)
    pbo2 = PaperBroker(db=None, fill_mode="close", equity0=10_000_000, slip_rate=0.0,
                       restore=False, margin_table={"RB": {"broker_margin": 0.1,
                       "limit_basic": 0.05, "multiplier": 10}},
                       fee_table={"RB": _fee_row(10, True)}, sector_of={"RB": "黑色"},
                       owner_fn=lambda ts: own_map.get(str(ts)[:19]))
    pbo2.on_cycle("2026-09-02 10:00:00", [_row("RB", "螺纹钢", "黑色", 5.0, 3000.0)])
    pbo2.on_cycle("2026-09-03 10:00:00", [_row("RB", "螺纹钢", "黑色", 1.0, 3000.0)])
    rec_yest = pbo2.pf.closed[-1]
    ck("跨结算交易日=平昨(收费)", rec_yest["leg"] == "平昨" and rec_yest["close_fee_yuan"] > 0.0)
    # owner_fn 失效时保守按平昨（不虚构平今免费）
    pbo3 = PaperBroker(db=None, fill_mode="close", equity0=10_000_000, slip_rate=0.0,
                       restore=False, margin_table={"RB": {"broker_margin": 0.1,
                       "limit_basic": 0.05, "multiplier": 10}},
                       fee_table={"RB": _fee_row(10, True)}, sector_of={"RB": "黑色"},
                       owner_fn=lambda ts: None)
    pbo3.on_cycle("2026-09-02 10:00:00", [_row("RB", "螺纹钢", "黑色", 5.0, 3000.0)])
    pbo3.on_cycle("2026-09-02 14:00:00", [_row("RB", "螺纹钢", "黑色", 1.0, 3000.0)])
    ck("owner判不了保守平昨", pbo3.pf.closed[-1]["leg"] == "平昨")
    # 账户视图字段齐全
    view = pbo.account_summary()
    ck("账户摘要含状态计数/视图", set(["pending", "filled", "blocked", "rejected",
       "cancelled"]).issubset(view["status"]) and "float_pnl" in view and "n_pending" in view)

    # 12) G1续 OMS 全状态台账 + 主动撤单（pbc 为 group7 close 档持 CU 多）
    ck("OMS台账含已成交终态", any(o["status"] == "filled" for o in pbc.orders_view())
       and len(pbc.orders_view()) >= 1)
    ck("OMS按状态过滤", len(pbc.orders_view(status="filled")) >= 1
       and len(pbc.orders_view(status="rejected")) == 0)
    pbq = PaperBroker(db=None, fill_mode="next", equity0=10_000_000, slip_rate=0.0, restore=False)
    pbq.on_cycle("2026-09-02 09:05:00", [_row("RB", "螺纹钢", "黑色", 5.0, 3000.0)])
    ck("挂单在途1", pbq.order_status_counts()["pending"] == 1)
    ck("主动撤单返回1且清在途", pbq.cancel_order(sym="RB") == 1
       and pbq.order_status_counts()["pending"] == 0)
    ck("撤单落 cancelled 终态", any(o["status"] == "cancelled" for o in pbq.orders_view()))

    # 13) 成交回报汇总（broker 方法 + 纯聚合函数）
    fr = pbc.fill_report()
    ck("成交回报笔数/开平/费", fr["n_fills"] == 1 and fr["lots"] >= 1 and fr["n_open"] == 1
       and fr["open_long"] >= 1 and fr["fee_yuan"] >= 0.0)
    agg = aggregate_fills([
        {"side": "open", "direction": 1, "lots": 2, "notional": 100.0, "fee_yuan": 1.0,
         "slip_yuan": 0.2, "realized_yuan": 0.0, "forced": 0},
        {"side": "close", "direction": 1, "lots": 2, "notional": 100.0, "fee_yuan": 1.0,
         "slip_yuan": 0.2, "realized_yuan": 5.0, "forced": 1}])
    ck("成交回报聚合多空开平/强平", agg["lots"] == 4 and agg["open_long"] == 2
       and agg["close_long"] == 2 and agg["n_forced"] == 1 and abs(agg["realized_yuan"] - 5.0) < 1e-9)

    # 14) 持仓对账：纯函数五类 break + broker 方法
    internal = {"RB": {"direction": 1, "lots": 2, "entry_price": 3000.0},
                "CU": {"direction": -1, "lots": 1, "entry_price": 70000.0}}
    ck("对账完全一致=clean", reconcile_position_sets(internal, dict(internal))["clean"])
    ext_dir = dict(internal)
    ext_dir["RB"] = {"direction": -1, "lots": 2, "entry_price": 3000.0}
    t_dir = {b["sym"]: b["type"] for b in reconcile_position_sets(internal, ext_dir)["breaks"]}
    ck("对账识别方向反", t_dir.get("RB") == "direction")
    ext_miss = dict(internal)
    ext_miss["AU"] = {"direction": 1, "lots": 1, "entry_price": 500.0}
    t_miss = {b["sym"]: b["type"] for b in reconcile_position_sets(internal, ext_miss)["breaks"]}
    ck("对账识别内部漏记(missing_internal)", t_miss.get("AU") == "missing_internal")
    t_ghost = {b["sym"]: b["type"]
               for b in reconcile_position_sets(internal, {"CU": internal["CU"]})["breaks"]}
    ck("对账识别外部漏仓(missing_external)", t_ghost.get("RB") == "missing_external")
    ext_lots = dict(internal)
    ext_lots["CU"] = {"direction": -1, "lots": 3, "entry_price": 70000.0}
    t_lots = {b["sym"]: b["type"] for b in reconcile_position_sets(internal, ext_lots)["breaks"]}
    ck("对账识别手数不符带delta", t_lots.get("CU") == "lots")
    own_ext = {x["sym"]: {"direction": x["direction"], "lots": x["lots"],
                          "entry_price": x["entry_price"]} for x in pbc.positions_view()}
    ck("broker对账自洽clean", pbc.reconcile_positions(own_ext)["clean"])
    bad_ext = {s: {"direction": v["direction"], "lots": v["lots"] + 1,
                   "entry_price": v["entry_price"]} for s, v in own_ext.items()}
    ck("broker对账抓手数差", not pbc.reconcile_positions(bad_ext)["clean"])
    ck("纯内存无DB自洽对账返回None", pbc.reconcile_against_db() is None)

    # 15) 第103轮：threading.RLock 存在 + 同线程可重入（普通 Lock 会在此自死锁）
    import threading as _th
    _lk = pb._lock
    # Python 3.x 中 threading.RLock 是函数不是类，用 acquire 行为检测
    ck("broker._lock 存在", _lk is not None)
    _lk.acquire()
    _ok_reentrant = True
    try:
        _lk.acquire(timeout=1.0)
    except Exception:
        _ok_reentrant = False
    _lk.release()
    _lk.release()
    ck("RLock 同线程可重入", _ok_reentrant)

    # 16) 第104轮：统一资金池公式（一个钱包两张持仓表；初始资本只计一次）
    ua0 = PaperBroker(db=None, fill_mode="close", equity0=10_000_000,
                      slip_rate=0.0001, restore=False).unified_account()
    ck("无操作统一权益==初始资金", abs(ua0["equity"] - 10_000_000) < 1e-6)
    ck("无操作统一占用==0", ua0["margin_used"] == 0.0)
    ck("无操作统一权益公式", abs(ua0["equity"] - 10_000_000) < 1e-6)
    # 期货盈利场景：统一权益==期货口径（期权贡献为0）
    _cfg.PAPER_PER_SYMBOL = 0.05
    _cfg.PAPER_MAX_SYMBOL_WEIGHT = 1.0
    _cfg.PAPER_MAX_SECTOR_WEIGHT = 1.0
    _cfg.PAPER_MAX_CONCURRENT = 64
    _fut_broker = PaperBroker(db=None, fill_mode="close", equity0=1_000_000.0,
                              slip_rate=0.0001, restore=False,
                              margin_table={"RB": {"broker_margin": 0.1,
                                                   "limit_basic": 0.05, "multiplier": 10}})
    _fut_broker.on_cycle("2026-09-02 09:05:00", [_row("RB", "螺纹钢", "黑色", 5.0, 3000.0)])
    uaf = _fut_broker.unified_account()
    ck("期货盈利统一权益=期货口径", abs(uaf["equity"] - _fut_broker.pf.equity()) < 1e-6)
    ck("无期权时统一占用=期货保证金", abs(uaf["margin_used"] - _fut_broker.pf.margin_used()) < 1e-6)

    print("paper_broker --selftest：%d 项断言全部通过" % len(checks))
    for n, _ in checks:
        print("  PASS", n)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="G1 纸面交易引擎 PaperBroker")
    ap.add_argument("--selftest", action="store_true", help="零网络合成自检")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
