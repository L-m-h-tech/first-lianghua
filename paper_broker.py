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
from datetime import datetime

import config
import portfolio as portfolio_mod
import circuit_breaker
from backtest import load_fee_schedule
from storage import score_band_name


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


# next 档开仓时遇到这些【临时性】约束，挂单保持 pending 顺延等约束缓解（而非直接拒单丢弃）；
# 而"无合约乘数/策略目标不足1手"这类确定性约束才立即 rejected。
RETRYABLE_SKIP = {"同时持仓数达上限", "可用资金不足1手", "板块名义上限",
                    "策略目标不足1手(高价品种/名义权重偏小)"}


# =========================== 纸面经纪 ===========================

class PaperBroker:
    """实时轮询驱动的纸面经纪；内部组合一个 portfolio.Portfolio 作为账户内核。"""

    def __init__(self, *, db=None, equity0=None, fill_mode=None, entry_score=None,
                 exit_score=None, sizing=None, margin_table=None, fee_table=None,
                 sector_of=None, slip_rate=None, restore=True, clock=None, owner_fn=None,
                 risk_sizing=None, risk_gross=None, circuit=None):
        self.db = db
        self.fill_mode = fill_mode or getattr(config, "PAPER_FILL_MODE", "next")
        if self.fill_mode not in ("close", "next"):
            self.fill_mode = "next"
        self.entry_score = entry_score if entry_score is not None else config.PAPER_ENTRY_SCORE
        self.exit_score = exit_score if exit_score is not None else config.PAPER_EXIT_SCORE
        self.slip_rate = slip_rate if slip_rate is not None else config.PAPER_SLIP_RATE
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
            per_symbol=config.PAPER_PER_SYMBOL,
            risk_per_trade=config.PAPER_RISK_PER_TRADE,
            max_symbol_weight=config.PAPER_MAX_SYMBOL_WEIGHT,
            max_sector_weight=config.PAPER_MAX_SECTOR_WEIGHT,
            risk_liquidate=config.PAPER_RISK_LIQUIDATE,
            risk_safe=config.PAPER_RISK_SAFE,
            default_margin=config.PAPER_DEFAULT_MARGIN,
            max_concurrent=config.PAPER_MAX_CONCURRENT,
            fee_rate=config.PAPER_FEE_RATE, slip_rate=self.slip_rate,
            use_real_fees=config.PAPER_USE_REAL_FEES, sector_of=self._sector_of,
            # 第41轮 G26续：风险型横截面sizing能力位（默认None=逐字节等价旧版）；实时权重源（K线历史
            # 协方差）尚未接线，须先在组合回测影子对照达标后再议，未注入权重时内核自动回退等名义。
            risk_sizing=risk_sizing,
            risk_gross=config.PRS_GROSS if risk_gross is None else risk_gross)
        self.pending = {}          # sym -> [order, ...] next 档待成交队列（先平后开）
        self._open_seq = {}        # sym -> 开仓序号（生成 pos_ref）
        self.pos_ref = {}          # sym -> 当前持仓 pos_ref
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
        if restore and self.db is not None:
            self.restore()

    # ---------------- 持久化辅助（db 为空时全部静默跳过，纯内存可跑） ----------------

    def _ins_order(self, order):
        if self.db is None:
            order["id"] = order.get("id") or (id(order) & 0x7fffffff)
            return order["id"]
        try:
            order["id"] = self.db.insert_paper_order(order)
            return order["id"]
        except Exception:
            return None

    def _upd_order(self, order, **fields):
        order.update(fields)
        if self.db is not None and order.get("id"):
            try:
                self.db.update_paper_order(order["id"], **fields)
            except Exception:
                pass

    def _ins_trade(self, t):
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
                "raw": {"atr": row.get("atr")}}

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

    def _fill_leg(self, ts, order, raw_price):
        """把一条委托腿按盘面价 raw_price（内含滑点后）成交，返回 trade dict；失败返回 None。"""
        sym = order["sym"]
        pf = self.pf
        action = order["action"]
        is_open = action in ("open", "reverse_open")
        direction = order["direction"]
        side = order["side"]
        fill_price = apply_slip(raw_price, side, self.slip_rate)
        if fill_price <= 0:
            self._upd_order(order, status="blocked", reason="无价/非法价，顺延")
            return None
        atr = (order.get("raw") or {}).get("atr")

        if is_open:
            pos = pf.open(sym, order["name"], order["sector"], direction, fill_price, ts,
                          atr=atr, score=order.get("score"), owner=self._owner_of(ts))
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
                 "margin_rate": pos.margin_rate}
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
             "margin_rate": rec.get("margin_rate")}
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
            self.pos_ref.pop(sym, None)
            self._cancel_pending(sym, "风控强平撤销挂单")
            held_dir = 1 if rec["dir"] == "多" else -1
            order = self._make_order(ts, {"sym": sym, "name": rec.get("name", ""),
                                          "cat": rec.get("sector", ""), "score": rec.get("entry_score")},
                                     "liquidate", _side_of(held_dir, "close"), held_dir,
                                     rec["exit_px"], status="filled")
            order.update({"fill_ts": ts, "fill_price": rec["exit_px"],
                          "raw_price": rec["exit_px"], "lots": rec["lots"],
                          "pos_ref": "", "reason": rec["reason"]})
            self._ins_order(order)
            mult = pf.mult_of(sym)
            t = {"ts": ts, "pos_ref": "", "sym": sym, "name": rec.get("name", ""),
                 "sector": rec.get("sector", ""), "side": "close", "dir_text": rec["dir"],
                 "direction": held_dir, "lots": rec["lots"], "price": rec["exit_px"],
                 "raw_price": rec["exit_px"], "notional": rec["exit_px"] * mult * rec["lots"],
                 "slip_yuan": 0.0, "fee_yuan": rec["close_fee_yuan"],
                 "realized_yuan": rec["net_yuan"], "leg": rec["leg"], "reason": rec["reason"],
                 "forced": 1, "order_id": order.get("id"), "entry_ts": str(rec["entry_dt"]),
                 "entry_price": rec["entry_px"], "score": rec.get("entry_score"),
                 "margin_rate": rec.get("margin_rate")}
            self._ins_trade(t)
            events.append(t)
            ord_events.append(order)
        return events

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
            by_sym[sym] = row
            px = float(row.get("price") or 0.0)
            if px > 0:
                prices_raw[sym] = px
            q = quotes.get(row.get("code")) or {}
            if q:
                by_quote[sym] = q

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
                   "orders": cycle_orders, "trades": cycle_trades}
        self.last_summary = summary
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
                entry_owner=self._owner_of(t["ts"]), entry_i=0, block=0, calib_mult=1.0)
            pf.positions[sym] = pos
            pf._last_prices[sym] = t["price"]
            self.pos_ref[sym] = t["pos_ref"]
            open_fees += t.get("fee_yuan") or 0.0
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
                         "score": p.score})
        return rows

    def pending_view(self):
        """当前在途挂单明细（拍平成行列表），供 paper_account.txt。"""
        rows = []
        for sym in sorted(self.pending):
            for o in self.pending[sym]:
                rows.append({"sym": sym, "name": o.get("name", ""), "action": o.get("action", ""),
                             "side": o.get("side", ""), "direction": o.get("direction", 0),
                             "ts": o.get("ts", ""), "signal_price": o.get("signal_price"),
                             "score": o.get("score"), "reason": o.get("reason", "")})
        return rows

    def account_summary(self):
        pf = self.pf
        perf = None
        if pf.curve:
            perf = pf.performance()
        return {"equity0": pf.equity0, "static": pf.static_equity(),
                "equity": pf.equity(), "float_pnl": pf.float_pnl(),
                "realized": pf.realized, "fees_paid": pf.fees_paid,
                "margin_used": pf.margin_used(), "available": pf.available(),
                "risk_degree": pf.risk_degree(), "n_positions": len(pf.positions),
                "n_pending": sum(len(q) for q in self.pending.values()),
                "n_closed": len(pf.closed), "n_liquidations": len(pf.liquidations),
                "n_skipped": len(pf.skipped), "status": self.order_status_counts(),
                "fill_mode": self.fill_mode,
                "pending": {s: [dict(o) for o in q]
                            for s, q in self.pending.items()},
                "performance": perf}


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
