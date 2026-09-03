# -*- coding: utf-8 -*-
"""组合资金账户与权益曲线（第16轮 WP-E，零新增第三方依赖）。

解决的问题：backtest.py / intraday_backtest.py 都是"逐品种独立"统计交易，总体净值只能按
交易序列复利近似（实盘多品种同时持仓时该口径不成立）。本模块提供一个多品种【共享资金池】
的统一账户，逐 bar 盯市，输出组合级净值/回撤/夏普/风险度序列，并按期货公司口径做强制减仓。

两层结构：
1. Portfolio（纯计算、零网络、零DB，可直接合成断言）：
   - 静态权益 = 初始权益 + 已实现净盈亏（开/平仓手续费即时扣除）；
   - 浮动盈亏 = Σ 方向×(最新价-开仓价)×合约乘数×手数；动态权益 = 静态权益 + 浮盈；
   - 保证金占用 = Σ 最新价×乘数×手数×保证金率（data/futures_margins.csv，期货公司收取档）；
   - 可用资金 = 动态权益 - 保证金占用；风险度 = 保证金占用/动态权益（期货公司同口径）；
   - 三种手数分配：equal_notional 等名义 / equal_risk 等风险(ATR止损预算) / score 按综合分档；
     均再受 单品种名义上限、板块名义上限、可用资金（买得起才开）、同时持仓数 共同约束；
   - 风险度≥强平线时，按"浮动亏损最大优先"逐仓强平，直到回到安全线；记录强平事件；
   - 保证金表缺失品种回退 config.PORTFOLIO_DEFAULT_MARGIN 并【显式登记】，绝不静默用错。
2. 组合回放引擎：把多品种 bar（分钟来自 intraday_backtest 同一套装载/信号，日线来自 backtest
   同一套装载/信号）合并到统一时间轴，严格沿用既有撮合口径——信号 i 收盘确认、i+1 开盘成交、
   入场当根不查止损、止损/止盈预埋单（跳空开盘成交/触及触发价成交/同根双触按止损）、分钟日终
   强平、锁板整根封死拦截、零量不成交、平今按交易所结算交易日 owner 判定——所有成交作用于同一个
   Portfolio，从而真实刻画"同时持仓共享资金、保证金互相挤占、风控强平"。

诚实边界：
- 保证金率是期货公司披露的【常态收取档估算】（国君期货日历表 2026-08-28），临近交割/长假会
  上浮、期货公司可临时调整，非实时精确值；交易所基准档无干净免费源，CSV 中 exchange_margin 留空；
- 乘数为"每手报价单位个数"口径（鸡蛋报价元/500kg、1手=10个报价单位，与手续费表按吨记的5不同）；
- bar 内成交顺序不可知，同时间戳多品种按代码字母序确定性处理；仍非逐笔/L2回放，不构成投资建议。

运行示例：
  D:\\Python\\python.exe portfolio.py --all --period 30
  D:\\Python\\python.exe portfolio.py --codes RB,CU,MA --period 5 --sizing equal_risk
  D:\\Python\\python.exe portfolio.py --daily --all --days 250 --sizing score
"""
import argparse
import bisect
import csv
import math
import os
import statistics
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import config
import metrics
import portfolio_constructor as pc
from backtest import load_fee_schedule, side_fee, ratio_adjusted_bars, technical_score, score_band

_MARGIN_CACHE = {}

# 第41轮 G26续：允许接入共享内核的横截面风险型分配方法（GMV 第40轮已证过集中，不在接入列）
RISK_SIZING_METHODS = ("inv_vol", "erc")


def load_margin_schedule(path=None, force=False):
    """读取保证金率CSV，返回 {sym: row(broker_margin/limit_basic/multiplier 已转float)}；缺失返回{}。"""
    path = os.path.abspath(path or config.FUTURES_MARGINS_FILE)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    cached = _MARGIN_CACHE.get(path)
    if not force and cached and cached["mtime"] == mtime:
        return cached["rows"]
    rows = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            sym = (r.get("sym") or "").strip().upper()
            if not sym:
                continue
            for k in ("broker_margin", "limit_basic", "multiplier"):
                try:
                    r[k] = float(r.get(k) or 0.0)
                except (TypeError, ValueError):
                    r[k] = 0.0
            rows[sym] = r
    _MARGIN_CACHE[path] = {"mtime": mtime, "rows": rows}
    return rows


class Position:
    __slots__ = ("sym", "name", "sector", "direction", "lots", "entry_price", "entry_dt",
                 "stop", "target", "atr", "score", "margin_rate", "mult", "open_fee_yuan",
                 "entry_owner", "entry_i", "block", "calib_mult", "mfe", "mae")

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))


class Portfolio:
    """多品种共享资金池的统一账户。价格口径：盘面价；金额单位：人民币元。"""

    def __init__(self, equity0, margin_table, fee_table=None, *, sizing="equal_notional",
                 per_symbol=0.15, risk_per_trade=0.01, stop_atr=1.2, score_weights=None,
                 max_symbol_weight=0.30, max_sector_weight=0.60, risk_liquidate=1.0,
                 risk_safe=0.80, default_margin=0.12, max_concurrent=12,
                 fee_rate=0.00005, slip_rate=0.0001, use_real_fees=True, sector_of=None,
                 calibrator=None, risk_sizing=None, risk_gross=1.0):
        self.equity0 = float(equity0)
        self.margin_table = margin_table or {}
        self.fee_table = fee_table or {}
        self.sizing = sizing
        # 第41轮 G26续：横截面风险型目标权重（inv_vol/erc）。None=关闭、手数决策逐字节等价旧版；
        # 开启后由引擎在每个重估点用"仅当前bar之前"的收益序列算 {sym:目标名义权重} 经 set_risk_weights 注入，
        # 该宇宙内品种按权重定目标名义、宇宙外/未算出的品种安全回退等名义 per_symbol。
        self.risk_sizing = risk_sizing if risk_sizing in RISK_SIZING_METHODS else None
        self.risk_gross = float(risk_gross)
        self.risk_weights = {}        # sym -> 目标名义占权益比例（已按 risk_gross 缩放）
        self.risk_meta = None         # 最近一次重估的诊断（有效N/年化波动/样本数/asof）
        self.risk_meta_log = []       # 每次重估诊断留痕（影子对照报告用）
        self.per_symbol = per_symbol
        self.risk_per_trade = risk_per_trade
        self.stop_atr = stop_atr
        self.score_weights = score_weights or {}
        self.max_symbol_weight = max_symbol_weight
        self.max_sector_weight = max_sector_weight
        self.risk_liquidate = risk_liquidate
        self.risk_safe = min(risk_safe, risk_liquidate)   # 安全线不得高于强平线
        self.default_margin = default_margin
        self.max_concurrent = max_concurrent
        self.fee_rate = fee_rate
        self.slip_rate = slip_rate
        self.use_real_fees = use_real_fees
        self.sector_of = sector_of or {}
        # WP-F2 A3：历史胜率校准器（signal_calibrator.SignalCalibrator）；None=不校准、手数逐值不变
        self.calibrator = calibrator
        self.calib_log = []           # 每次实际应用乘子的开仓记录 {dt,sym,score,mult,level,n}
        self._last_calib_mult = 1.0
        self.realized = 0.0           # 已实现净盈亏（累计；手续费已在其中扣除）
        self.fees_paid = 0.0          # 累计手续费
        self.positions = {}           # sym -> Position
        self.closed = []              # 已平仓成交记录
        self.liquidations = []        # 强平记录
        self.skipped = []             # 资金/上限不足而拒绝开仓
        self.fallback_margins = set() # 用到兜底保证金率的品种
        self.curve = []               # 权益/风险度曲线
        self.peak_equity = self.equity0
        self._last_prices = {}        # 最近成交价（无新bar时沿用盯市）

    # ---------- 第41轮 G26续：横截面风险型目标权重注入 ----------
    def set_risk_weights(self, wmap, meta=None):
        """由回放引擎在重估点注入 {sym: 目标名义权重}（只用当前时刻之前的数据，PIT）。"""
        self.risk_weights = {s: float(w) for s, w in (wmap or {}).items() if w and w > 0}
        self.risk_meta = meta or None
        if meta is not None:
            self.risk_meta_log.append(meta)

    def avg_risk_eff_n(self):
        """历次重估的平均有效持仓数（无则 None，影子对照报告用）。"""
        vals = [m.get("eff_n") for m in self.risk_meta_log if m.get("eff_n")]
        return sum(vals) / len(vals) if vals else None

    # ---------- 静态参数查询 ----------
    def margin_rate_of(self, sym):
        row = self.margin_table.get(sym)
        if row and row.get("broker_margin", 0.0) > 0:
            return float(row["broker_margin"])
        self.fallback_margins.add(sym)
        return self.default_margin

    def mult_of(self, sym):
        """每手报价单位个数（名义价值=盘面价×该乘数）。优先保证金表，回退费表。"""
        row = self.margin_table.get(sym)
        if row and row.get("multiplier", 0.0) > 0:
            return float(row["multiplier"])
        fr = self.fee_table.get(sym)
        if fr and fr.get("multiplier", 0.0) > 0:
            return float(fr["multiplier"])
        return 0.0

    def fee_yuan(self, sym, price, leg, lots=1):
        """单笔单边手续费（人民币，全部手数）。真实表缺失时按兜底比例×名义价值。"""
        row = self.fee_table.get(sym) if self.use_real_fees else None
        if row:
            return side_fee(row, price, leg)[1] * lots
        return price * self.mult_of(sym) * lots * self.fee_rate

    # ---------- 盯市 ----------
    def _price_of(self, pos, prices):
        return float(prices.get(pos.sym, self._last_prices.get(pos.sym, pos.entry_price)))

    def float_pnl(self, prices=None):
        prices = prices or self._last_prices
        total = 0.0
        for pos in self.positions.values():
            px = self._price_of(pos, prices)
            total += pos.direction * (px - pos.entry_price) * pos.mult * pos.lots
        return total

    def margin_used(self, prices=None):
        prices = prices or self._last_prices
        total = 0.0
        for pos in self.positions.values():
            px = self._price_of(pos, prices)
            total += px * pos.mult * pos.lots * pos.margin_rate
        return total

    def static_equity(self):
        return self.equity0 + self.realized

    def equity(self, prices=None):
        return self.static_equity() + self.float_pnl(prices)

    def available(self, prices=None):
        eq = self.equity(prices)
        return eq - self.margin_used(prices)

    def risk_degree(self, prices=None):
        eq = self.equity(prices)
        used = self.margin_used(prices)
        return used / eq if eq > 1e-9 else math.inf

    def sector_notional(self, sector, prices=None):
        prices = prices or self._last_prices
        total = 0.0
        for pos in self.positions.values():
            if pos.sector == sector:
                total += self._price_of(pos, prices) * pos.mult * pos.lots
        return total

    # ---------- 手数决策 ----------
    def decide_lots(self, sym, direction, price, *, atr=None, score=None, prices=None,
                    parts=None):
        """返回 (手数≥0, 未成交原因或None)。约束链：策略目标 → 名义/板块上限 → 可用资金/持仓数。"""
        prices = prices or self._last_prices
        mult = self.mult_of(sym)
        if mult <= 0 or price <= 0:
            return 0, "无合约乘数"
        if self.max_concurrent and sym not in self.positions and \
                len(self.positions) >= self.max_concurrent:
            return 0, "同时持仓数达上限"
        eq = self.equity(prices)
        per_lot_notional = price * mult

        # 1) 策略目标手数（原始，未取整）
        if self.sizing == "equal_risk" and atr and atr > 0:
            per_lot_risk = self.stop_atr * atr * mult     # 单手打到止损的最大亏损
            raw = eq * self.risk_per_trade / per_lot_risk if per_lot_risk > 0 else 0.0
        elif self.sizing == "score" and score is not None:
            band = score_band(score)
            w = self.score_weights.get(band, self.per_symbol)
            raw = eq * w / per_lot_notional
        else:  # equal_notional（也是其余模式数据不足时的回退）
            raw = eq * self.per_symbol / per_lot_notional
        # 第41轮 G26续：横截面风险型权重覆盖目标名义（仅 risk_sizing 开启且该品种在最新权重宇宙内）；
        # 宇宙外/尚未估出 -> 保留上面的等名义 raw（安全回退，缺省 risk_sizing=None 时整段不进入、逐字节等价旧版）
        self._last_target_weight = None
        if self.risk_sizing is not None:
            rw = self.risk_weights.get(sym)
            if rw is not None and rw > 0:
                self._last_target_weight = rw
                raw = eq * rw / per_lot_notional
        # WP-F2 A3：历史同类信号胜率校准乘子。仅当显式传入 calibrator 才生效（默认 None=逐值不变）；
        # 回测无九因子拆分时 parts=None，校准器自动回退到「方向×分档」层。
        self._last_calib_mult = 1.0
        if self.calibrator is not None and score is not None:
            _ci = self.calibrator.lookup(score, direction_int=direction, parts=parts)
            if _ci.get("calibrated"):
                self._last_calib_mult = float(_ci["mult"])
                raw *= self._last_calib_mult
        if raw < 1.0:
            return 0, "策略目标不足1手(高价品种/名义权重偏小)"

        # 2) 单品种名义上限
        cap_symbol = self.max_symbol_weight * eq / per_lot_notional
        # 3) 板块名义上限（扣掉同板块已占用名义）
        sector = self.sector_of.get(sym)
        cap_sector = math.inf
        if sector is not None:
            room = self.max_sector_weight * eq - self.sector_notional(sector, prices)
            cap_sector = max(0.0, room) / per_lot_notional
        # 4) 可用资金：每手需保证金 + 开仓费（留 1% 现金缓冲，避免取整临界）
        rate = self.margin_rate_of(sym)
        need_per_lot = price * mult * rate + self.fee_yuan(sym, price, "open", 1)
        cap_cash = max(0.0, self.available(prices) * 0.99) / need_per_lot if need_per_lot > 0 else 0.0

        binding = min((("单品种名义上限", cap_symbol), ("板块名义上限", cap_sector),
                       ("可用资金不足1手", cap_cash)), key=lambda x: x[1])
        lots = int(math.floor(min(raw, cap_symbol, cap_sector, cap_cash)))
        if lots <= 0:
            return 0, binding[0]
        return lots, None

    # ---------- 开/平仓 ----------
    def open(self, sym, name, sector, direction, price, dt, *, atr=None, score=None,
             owner=None, i=0, stop=None, target=None, parts=None):
        if sym in self.positions or price <= 0:
            return None
        mult = self.mult_of(sym)
        if mult <= 0:
            self.skipped.append({"dt": dt, "sym": sym, "reason": "无合约乘数"})
            return None
        lots, why = self.decide_lots(sym, direction, price, atr=atr, score=score, parts=parts)
        if lots <= 0:
            self.skipped.append({"dt": dt, "sym": sym, "reason": why or "未成交",
                                 "available": self.available(), "price": price})
            return None
        rate = self.margin_rate_of(sym)
        open_fee = self.fee_yuan(sym, price, "open", lots)
        self.realized -= open_fee
        self.fees_paid += open_fee
        pos = Position(sym=sym, name=name, sector=sector, direction=direction, lots=lots,
                       entry_price=price, entry_dt=dt, stop=stop, target=target, atr=atr,
                       score=score, margin_rate=rate, mult=mult, open_fee_yuan=open_fee,
                       entry_owner=owner, entry_i=i, block=0, calib_mult=self._last_calib_mult)
        if self._last_calib_mult != 1.0 and self.calibrator is not None:
            _ci = self.calibrator.lookup(score, direction_int=direction, parts=parts)
            self.calib_log.append({"dt": dt, "sym": sym, "score": score,
                                   "mult": self._last_calib_mult,
                                   "level": _ci.get("level", ""), "n": _ci.get("n", 0)})
        self.positions[sym] = pos
        self._last_prices[sym] = price
        return pos

    def close(self, sym, price, dt, reason, *, leg="close", forced=False, hold_bars=0):
        pos = self.positions.pop(sym, None)
        if pos is None or price <= 0:
            return None
        close_fee = self.fee_yuan(sym, price, leg, pos.lots)
        gross_yuan = pos.direction * (price - pos.entry_price) * pos.mult * pos.lots
        net_yuan = gross_yuan - pos.open_fee_yuan - close_fee
        self.realized += gross_yuan - close_fee     # 开仓费开仓时已扣
        self.fees_paid += close_fee
        self._last_prices[sym] = price
        rec = {"sym": sym, "name": pos.name, "sector": pos.sector,
               "dir": "多" if pos.direction > 0 else "空", "lots": pos.lots,
               "entry_dt": pos.entry_dt, "exit_dt": dt, "entry_px": pos.entry_price,
               "exit_px": price, "leg": "平今" if leg == "today" else "平昨",
               "hold_bars": hold_bars, "gross_yuan": gross_yuan,
               "open_fee_yuan": pos.open_fee_yuan, "close_fee_yuan": close_fee,
               "net_yuan": net_yuan, "reason": reason, "forced": forced,
               "entry_score": pos.score, "margin_rate": pos.margin_rate,
               "mfe": getattr(pos, "mfe", None) or 0.0,
               "mae": getattr(pos, "mae", None) or 0.0,
               "calib_mult": getattr(pos, "calib_mult", 1.0)}
        self.closed.append(rec)
        if forced:
            self.liquidations.append(rec)
        return rec

    def liquidate(self, dt, price_getter, *, leg_getter=None):
        """风险度破强平线时强制减仓：浮亏最大优先、整仓平掉，触发后一路砍到【安全线】以下
        （避免刚跌破强平线就停、下一根轻微波动又反复触发）。返回强平列表。"""
        events = []
        triggered = False
        guard = 0
        while self.positions:
            guard += 1
            if guard > 10000:
                break
            eq = self.equity()
            if eq <= 1e-9:  # 穿仓：全部平掉
                for sym in sorted(self.positions):
                    px = price_getter(sym)
                    leg = leg_getter(sym) if leg_getter else "close"
                    rec = self.close(sym, px, dt, "穿仓强平", leg=leg, forced=True)
                    if rec:
                        events.append(rec)
                break
            rd = self.risk_degree()
            if rd >= self.risk_liquidate:
                triggered = True
            # 未触发看强平线；已触发看安全线（一路降到底，避免下一根再次破线）
            threshold = self.risk_safe if triggered else self.risk_liquidate
            if rd < threshold:
                break
            # 浮动亏损最大者（数值最小=亏最多）
            worst = min(self.positions,
                        key=lambda s: self.positions[s].direction *
                        (self._price_of(self.positions[s], {}) - self.positions[s].entry_price)
                        * self.positions[s].mult * self.positions[s].lots)
            px = price_getter(worst)
            leg = leg_getter(worst) if leg_getter else "close"
            rec = self.close(worst, px, dt, "风控强平", leg=leg, forced=True)
            if rec:
                events.append(rec)
        return events

    def record(self, dt, prices):
        """记录一个时间点的账户快照。"""
        for s, p in prices.items():
            if p and p > 0:
                self._last_prices[s] = p
        # G3：逐仓累计 MFE/MAE（相对开仓价的最大有利/不利偏移，正小数；纯增量、不改任何金额）
        for pos in self.positions.values():
            px = self._price_of(pos, prices)
            if pos.entry_price and pos.entry_price > 0 and px and px > 0:
                fav = pos.direction * (px - pos.entry_price) / pos.entry_price
                pos.mfe = max(getattr(pos, "mfe", None) or 0.0, max(fav, 0.0))
                pos.mae = max(getattr(pos, "mae", None) or 0.0, max(-fav, 0.0))
        eq = self.equity()
        used = self.margin_used()
        self.peak_equity = max(self.peak_equity, eq)
        dd = 1.0 - eq / self.peak_equity if self.peak_equity > 0 else 0.0
        self.curve.append({"dt": dt, "static": self.static_equity(), "float": self.float_pnl(),
                           "equity": eq, "margin": used, "available": eq - used,
                           "risk": used / eq if eq > 1e-9 else math.inf,
                           "drawdown": max(0.0, dd), "npos": len(self.positions)})

    def close_all(self, dt, price_getter, *, leg_getter=None, reason="样本末清仓"):
        recs = []
        for sym in sorted(self.positions):
            leg = leg_getter(sym) if leg_getter else "close"
            rec = self.close(sym, price_getter(sym), dt, reason, leg=leg)
            if rec:
                recs.append(rec)
        return recs

    # ---------- 绩效 ----------
    def performance(self, bars_per_year=243):
        """基于权益曲线计算组合级绩效（日度口径年化）。"""
        if not self.curve:
            return None
        # 按自然日取每日最后一个权益点，组成日度权益序列
        daily = {}
        for pt in self.curve:
            d = pt["dt"].date() if hasattr(pt["dt"], "date") else pt["dt"]
            daily[d] = pt["equity"]
        dates = sorted(daily)
        eq_series = [self.equity0] + [daily[d] for d in dates]
        # 日度收益与其归属日（对齐，供月度矩阵）；保留旧 rets 口径不变
        rets, ret_dates = [], []
        for k in range(1, len(eq_series)):
            if eq_series[k - 1] > 0:
                rets.append(eq_series[k] / eq_series[k - 1] - 1.0)
                ret_dates.append(dates[k - 1])
        end_eq = self.curve[-1]["equity"]
        total_ret = end_eq / self.equity0 - 1.0
        max_dd = max(pt["drawdown"] for pt in self.curve)
        dd_bottom = min(self.curve, key=lambda p: p["equity"])
        peak_pt = max(self.curve, key=lambda p: p["equity"])
        avg_risk = statistics.mean(pt["risk"] for pt in self.curve if math.isfinite(pt["risk"]))
        max_risk = max((pt["risk"] for pt in self.curve if math.isfinite(pt["risk"])), default=0.0)
        max_npos = max((pt["npos"] for pt in self.curve), default=0)
        if rets:
            mu = statistics.mean(rets)
            sd = statistics.stdev(rets) if len(rets) >= 2 else 0.0
            downside = [r for r in rets if r < 0]
            dsd = statistics.stdev(downside) if len(downside) >= 2 else 0.0
            ann_ret = mu * bars_per_year
            sharpe = mu / sd * math.sqrt(bars_per_year) if sd > 1e-12 else 0.0
            sortino = mu / dsd * math.sqrt(bars_per_year) if dsd > 1e-12 else 0.0
        else:
            ann_ret = sharpe = sortino = 0.0
        # G3：在旧键之外增补完整绩效指标（子项样本不足为 None，绝不影响旧口径）
        g3 = metrics.tear_sheet(rets, ret_dates, bars_per_year=bars_per_year) if rets else {}
        tstats = metrics.trade_stats([t["net_yuan"] for t in self.closed])
        excursion = metrics.mae_mfe_summary(
            [{"mfe": t.get("mfe") or 0.0, "mae": t.get("mae") or 0.0,
              "win": t["net_yuan"] > 0} for t in self.closed])
        wins = [t for t in self.closed if t["net_yuan"] > 0]
        losses = [t for t in self.closed if t["net_yuan"] < 0]
        avg_win = statistics.mean([t["net_yuan"] for t in wins]) if wins else 0.0
        avg_loss = statistics.mean([t["net_yuan"] for t in losses]) if losses else 0.0
        return {"total_ret": total_ret, "end_equity": end_eq, "ann_ret": ann_ret,
                "max_dd": max_dd, "sharpe": sharpe, "sortino": sortino,
                "calmar": g3.get("calmar"), "omega": g3.get("omega"),
                "ulcer": g3.get("ulcer"), "var95": g3.get("var"),
                "cvar95": g3.get("cvar"), "monthly": g3.get("monthly"),
                "profit_factor": (tstats or {}).get("profit_factor"),
                "max_win_streak": (tstats or {}).get("max_win_streak", 0),
                "max_loss_streak": (tstats or {}).get("max_loss_streak", 0),
                "mae_mfe": excursion,
                "dd_bottom_dt": dd_bottom["dt"], "dd_bottom_eq": dd_bottom["equity"],
                "peak_dt": peak_pt["dt"], "peak_eq": peak_pt["equity"],
                "avg_risk": avg_risk, "max_risk": max_risk, "max_npos": max_npos,
                "n_trades": len(self.closed), "win_rate": len(wins) / len(self.closed) if self.closed else 0.0,
                "avg_win": avg_win, "avg_loss": avg_loss,
                "pl_ratio": avg_win / abs(avg_loss) if avg_loss < 0 else None,
                "total_pnl": sum(t["net_yuan"] for t in self.closed),
                "fees_paid": self.fees_paid, "n_liquidations": len(self.liquidations),
                "n_skipped": len(self.skipped), "days": len(dates)}


# =========================== 组合回放引擎 ===========================
def _sig_dir(score, entry_th):
    if score is None:
        return 0
    return 1 if score >= entry_th else (-1 if score <= -entry_th else 0)


def _locked(bar, base, move, eps, buying):
    """与 intraday_backtest.locked_at 同口径：整根封死在板价才拦截。"""
    if not base or base <= 0 or not move or move >= 1:
        return False
    if buying:
        return bar["l"] >= base * (1.0 + move) * (1.0 - eps)
    return bar["h"] <= base * (1.0 - move) * (1.0 + eps)


class SymbolFeed:
    """单品种回放原料：bars/scores/atrs/owners/bases 已对齐同长度，按 dt 建索引。"""
    def __init__(self, sym, name, sector, bars, scores, atrs, owners, bases,
                 fee_row, limit_move):
        self.sym, self.name, self.sector = sym, name, sector
        self.bars = bars
        self.scores, self.atrs = scores, atrs
        self.owners, self.bases = owners, bases
        self.fee_row, self.limit_move = fee_row, limit_move
        self.by_dt = {b["dt"]: k for k, b in enumerate(bars)}
        self.dts = [b["dt"] for b in bars]      # 有序，供无bar时刻二分定位
        self.pos = None        # 引擎层持仓（与 Portfolio 同步）
        self.pending = None
        self.blocked_entry = 0
        self.blocked_exit = 0

    def owner_at(self, t):
        """t 时刻该品种所属结算交易日 owner；t 无该品种bar（如无夜盘品种在夜盘）取最近已收盘bar。"""
        if self.owners is None:
            return None
        i = self.by_dt.get(t)
        if i is not None:
            return self.owners[i]
        k = bisect.bisect_right(self.dts, t) - 1
        return self.owners[k] if k >= 0 else None


def trailing_risk_weights(feeds, t, method, *, window=126, min_hist=40, shrink=0.10,
                          cap=0.20, gross=1.0):
    """第41轮 G26续：在时刻 t 用【严格早于 t】的各品种收盘价构造收益矩阵，调 portfolio_constructor
    出横截面风险型目标权重（inv_vol/erc）。严格 PIT：t 当根及其后一律不看（入场发生在 t 开盘）。

    feeds: {sym: SymbolFeed}；返回 (wmap {sym: 权重×gross}, meta)。
    可估品种<2 或公共历史不足 min_hist 时返回 ({}, meta)，调用方安全回退等名义，绝不抛错。"""
    own_close = {}
    for sym, f in feeds.items():
        k = bisect.bisect_left(f.dts, t) - 1      # 最后一根 dt < t 的 bar（t 当根排除=无未来）
        if k < 1:
            continue
        lo = max(0, k - window + 1)
        m = {}
        for b in f.bars[lo:k + 1]:
            c = b.get("c")
            if c is not None and c > 0:
                m[b["dt"]] = float(c)
        if len(m) >= min_hist + 1:
            own_close[sym] = m
    if len(own_close) < 2:
        return {}, {"method": method, "n": len(own_close), "asof": str(t),
                    "reason": "满足最小历史的可估品种<2，全部回退等名义"}
    # 公共时间戳稠密对齐（与 portfolio_lab 同原则：协方差必须同一时刻配对）
    common = None
    for m in own_close.values():
        ks = set(m)
        common = ks if common is None else (common & ks)
    common = sorted(common)
    syms, rets_map = sorted(own_close), {}
    for s in syms:
        m = own_close[s]
        closes = [m[d] for d in common]
        r = [closes[i + 1] / closes[i] - 1.0 for i in range(len(closes) - 1) if closes[i] > 0]
        if len(r) >= min_hist:
            rets_map[s] = r
    if len(rets_map) < 2:
        return {}, {"method": method, "n": len(rets_map), "asof": str(t),
                    "reason": "公共对齐后收益历史不足min_hist，全部回退等名义"}
    T = min(len(r) for r in rets_map.values())
    syms = sorted(rets_map)
    R = [rets_map[s][-T:] for s in syms]
    out = pc.construct(R, method, shrink=shrink, cap=cap, target_annual=0.0)
    w = out["weights"]
    wmap = {s: w[i] * gross for i, s in enumerate(syms)}
    meta = {"method": method, "n": len(syms), "T": T, "asof": str(t),
            "eff_n": out["eff_n"], "ann_vol": out["ann_vol"], "div_ratio": out["div_ratio"],
            "gross_base": sum(w), "gross": gross,
            "excluded": sorted(set(feeds) - set(syms))}
    return wmap, meta


def _reset_feeds(feeds):
    """清空引擎层可变状态（持仓/挂单/锁板计数），使同一批 feeds 可被确定性地重复回放（影子对照用）。"""
    for f in feeds.values():
        f.pos = None
        f.pending = None
        f.blocked_entry = 0
        f.blocked_exit = 0


def run_portfolio(feeds, pf, *, entry_th, stop_atr, target_atr, flat_eod, max_bars,
                  use_limit, limit_eps, minute_mode, hold_days=10, risk_cfg=None):
    """统一时间轴逐bar驱动共享账户。feeds: {sym: SymbolFeed}；pf: Portfolio。
    risk_cfg 非空时（第41轮 G26续）按 rebalance 间隔用仅过去数据重估横截面风险权重并注入 pf。"""
    timeline = sorted({b["dt"] for f in feeds.values() for b in f.bars})
    risk_step = 0
    for t in timeline:
        if risk_cfg is not None:
            risk_step += 1
            if risk_step == 1 or (risk_step - 1) % int(risk_cfg.get("rebalance", 20)) == 0:
                wmap, rmeta = trailing_risk_weights(
                    feeds, t, risk_cfg["method"], window=risk_cfg.get("window", 126),
                    min_hist=risk_cfg.get("min_hist", 40), shrink=risk_cfg.get("shrink", 0.10),
                    cap=risk_cfg.get("cap", 0.20), gross=risk_cfg.get("gross", 1.0))
                pf.set_risk_weights(wmap, rmeta)
        for sym in sorted(feeds):                      # 同时间戳按代码字母序，确定性
            f = feeds[sym]
            i = f.by_dt.get(t)
            if i is None:
                continue                               # 该品种此刻无bar（如无夜盘），沿用旧价盯市
            bar = f.bars[i]
            owner = f.owners[i] if f.owners else None
            base = f.bases[i] if f.bases else (f.bars[i - 1]["c"] if i > 0 else None)

            # 1) 上一根挂出的委托本根开盘成交
            if f.pending is not None:
                kind = f.pending[0]
                if kind == "entry":
                    d, sig_i, sig_score = f.pending[1], f.pending[2], f.pending[3]
                    locked = use_limit and _locked(
                        bar, base, f.limit_move, limit_eps, d > 0)
                    if bar.get("v", 1) <= 0 or locked:
                        f.blocked_entry += 1
                    else:
                        px = bar["o"] * (1.0 + d * pf.slip_rate)
                        atr = f.atrs[sig_i] if f.atrs else None
                        stop = target = None
                        if minute_mode and atr:
                            stop = px - d * stop_atr * atr
                            target = px + d * target_atr * atr
                        pos = pf.open(f.sym, f.name, f.sector, d, px, t, atr=atr,
                                      score=sig_score, owner=owner, i=i, stop=stop,
                                      target=target)
                        if pos is not None:
                            f.pos = pos
                            # 入场当根不查止损；但分钟日内模式该根即交易日末根 -> 立即收盘强平
                            if minute_mode and flat_eod and (
                                    i == len(f.bars) - 1 or f.owners[i + 1] != owner):
                                _engine_close(f, pf, bar["c"] * (1.0 - d * pf.slip_rate), t, i,
                                              "样本末强平" if i == len(f.bars) - 1 else "日终强平",
                                              owner, use_limit, base, limit_eps)
                    f.pending = None
                else:  # exit
                    reason = f.pending[1]
                    d = f.pos.direction
                    locked = use_limit and _locked(
                        bar, base, f.limit_move, limit_eps, d <= 0)
                    f.pending = None
                    if locked:
                        f.blocked_exit += 1
                        if f.pos:
                            f.pos.block += 1
                    else:
                        px = bar["o"] * (1.0 - d * pf.slip_rate)
                        _engine_close(f, pf, px, t, i, reason, owner,
                                      use_limit, base, limit_eps)
                        continue

            # 2) 持仓管理
            if f.pos is not None:
                handled = False
                d = f.pos.direction
                if minute_mode:
                    xpx, reason = None, None
                    if d > 0:
                        if bar["o"] <= f.pos.stop:
                            xpx, reason = bar["o"] * (1.0 - pf.slip_rate), "止损(跳空)"
                        elif bar["l"] <= f.pos.stop:
                            xpx, reason = f.pos.stop * (1.0 - pf.slip_rate), "止损"
                        elif bar["o"] >= f.pos.target:
                            xpx, reason = bar["o"] * (1.0 + pf.slip_rate), "止盈(跳空)"
                        elif bar["h"] >= f.pos.target:
                            xpx, reason = f.pos.target * (1.0 + pf.slip_rate), "止盈"
                    else:
                        if bar["o"] >= f.pos.stop:
                            xpx, reason = bar["o"] * (1.0 + pf.slip_rate), "止损(跳空)"
                        elif bar["h"] >= f.pos.stop:
                            xpx, reason = f.pos.stop * (1.0 + pf.slip_rate), "止损"
                        elif bar["o"] <= f.pos.target:
                            xpx, reason = bar["o"] * (1.0 - pf.slip_rate), "止盈(跳空)"
                        elif bar["l"] <= f.pos.target:
                            xpx, reason = f.pos.target * (1.0 - pf.slip_rate), "止盈"
                    if xpx is not None:
                        locked = use_limit and _locked(
                            bar, base, f.limit_move, limit_eps, d <= 0)
                        if not locked:
                            _engine_close(f, pf, xpx, t, i, reason, owner,
                                          use_limit, base, limit_eps)
                            handled = True
                        else:
                            f.blocked_exit += 1
                            f.pos.block += 1
                    if not handled and f.pos is not None:
                        last_owner = (i == len(f.bars) - 1) or (f.owners[i + 1] != owner)
                        if (flat_eod and last_owner) or i == len(f.bars) - 1:
                            xpx = bar["c"] * (1.0 - d * pf.slip_rate)
                            reason = "样本末强平" if i == len(f.bars) - 1 else "日终强平"
                            locked = use_limit and _locked(
                                bar, base, f.limit_move, limit_eps, d <= 0)
                            if (not locked) or i == len(f.bars) - 1:
                                _engine_close(f, pf, xpx, t, i, reason, owner,
                                              use_limit, base, limit_eps)
                                handled = True
                            else:
                                f.blocked_exit += 1
                                f.pos.block += 1
                    if not handled and f.pos is not None:
                        sig = _sig_dir(f.scores[i], entry_th)
                        if sig == -d:
                            f.pending = ("exit", "反向信号")
                        elif (not flat_eod) and (i - f.pos.entry_i) >= max_bars:
                            f.pending = ("exit", "到期")
                else:
                    # 日线模式：反向信号或持有到期，下一根开盘离场
                    held = i - f.pos.entry_i
                    sig = _sig_dir(f.scores[i], entry_th)
                    if sig == -d or held >= hold_days:
                        f.pending = ("exit", "反向信号" if sig == -d else "持有到期")

            # 3) 空仓：本根收盘决策，下一根开盘入场
            if f.pos is None and f.pending is None and i < len(f.bars) - 1:
                sig = _sig_dir(f.scores[i], entry_th)
                if sig != 0:
                    if minute_mode and (f.atrs[i] is None or f.atrs[i] <= 0):
                        pass  # 分钟无ATR不入场
                    else:
                        f.pending = ("entry", sig, i, f.scores[i])

        # 4) 统一盯市 + 记录权益 + 风控强平（收盘价；无bar品种沿用最近价）
        prices = {sym: (f.bars[f.by_dt[t]]["c"] if t in f.by_dt else pf._last_prices.get(sym, 0.0))
                  for sym, f in feeds.items()}
        pf.record(t, prices)
        events = pf.liquidate(
            t, lambda s: feeds[s].bars[feeds[s].by_dt[t]]["c"] if t in feeds[s].by_dt
            else pf._last_prices.get(s, 0.0),
            leg_getter=(lambda s: "today" if (minute_mode and feeds[s].pos is not None
                                              and feeds[s].pos.entry_owner == feeds[s].owner_at(t))
                        else "close") if minute_mode else None)
        for rec in events:                       # 强平后同步清除引擎层持仓/挂单
            fs = feeds[rec["sym"]]
            fs.pos = None
            fs.pending = None
        if events:                               # 补记强平后快照（同时间戳，曲线末点为强平后状态）
            pf.record(t, prices)

    # 5) 时间轴末端：清掉残留持仓（按各品种最后收盘价；分钟日内模式循环内已平日终，这里通常无仓）
    end_recs = []
    for sym in sorted(feeds):
        f = feeds[sym]
        if f.pos is not None:
            last = f.bars[-1]
            d = f.pos.direction
            rec = _engine_close(f, pf, last["c"] * (1.0 - d * pf.slip_rate), last["dt"],
                                len(f.bars) - 1, "样本末清仓",
                                f.owners[-1] if f.owners else None, False, None, None)
            if rec:
                end_recs.append(rec)
    if timeline and end_recs:   # 仅当末端确有清仓时补记，避免与循环内末根快照重复
        prices = {sym: f.bars[-1]["c"] for sym, f in feeds.items()}
        pf.record(timeline[-1], prices)
    return pf


def _engine_close(f, pf, px, t, i, reason, owner, use_limit, base, limit_eps):
    """引擎层平仓：判定平今/平昨，同步清持仓。返回成交记录。"""
    if f.pos is None:
        return None
    leg = "close"
    if f.owners is not None and owner is not None:
        leg = "today" if f.pos.entry_owner == owner else "close"
    hold = i - f.pos.entry_i
    rec = pf.close(f.sym, px, t, reason, leg=leg, hold_bars=hold)
    f.pos = None
    return rec


# =========================== 数据装载 ===========================
def _bar_dt(b):
    if isinstance(b.get("dt"), datetime):
        return b["dt"]
    d = b.get("d") or b.get("dt")
    if isinstance(d, datetime):
        return d
    s = str(d)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:16] if "%H" in fmt else s[:10], fmt)
        except ValueError:
            continue
    return None


def load_minute_feed(item, args, fee_table, margin_table):
    """分钟品种原料（复用 intraday_backtest 的装载与信号，保证口径一致）。"""
    import storage
    import intraday_backtest as ib
    sym, code, name = item
    db = storage.MonitorDB()
    try:
        raw, _src = ib.load_minute_bars(db, sym, args.period, args.lookback, args.aggregate_from)
    finally:
        db.close()
    if len(raw) < config.INTRADAY_BT_WARMUP + 5:
        return sym, None, f"分钟bar不足({len(raw)}根)"
    bars, _roll = ratio_adjusted_bars(raw)
    scores_atr = ib.prepare_series(bars, args.sig_window)
    closes, highs, lows, scores, atrs = scores_atr
    owners, bases = ib.build_owner_meta(bars)
    bars = [{**b, "dt": b["dt"]} for b in bars]
    meta = config.VARIETIES.get(name, {})
    move = config.FUTURES_LIMIT_MOVE.get(sym, config.INTRADAY_BT_LIMIT_MOVE)
    feed = SymbolFeed(sym, name, meta.get("cat"), bars, scores, atrs, owners, bases,
                      fee_table.get(sym), move)
    return sym, feed, None


def load_daily_feed(item, days, hold, entry, fee_table):
    """日线品种原料（复用 backtest 的装载与技术分）。"""
    import futures_data
    import time as _time
    name, code = item
    sym = code.rstrip("0").upper()
    try:
        raw = None
        for attempt in range(2):                 # 外层再兜一次瞬时抖动
            try:
                raw = futures_data.fetch_daily_kline(code)[-days:]
                break
            except RuntimeError:
                if attempt == 1:
                    raise
                _time.sleep(0.8)
        prepared = __import__("backtest").prepare_symbol(raw)
        if prepared is None:
            return sym, None, "日K不足"
        bars, series = prepared["bars"], prepared["series"]
        idx_set = {s["i"] for s in series}
        scores = [None] * len(bars)
        for s in series:
            scores[s["i"]] = technical_score(s["ind"])
        out = []
        for b in bars:
            dt = _bar_dt(b)
            if dt is None:
                continue
            out.append({"dt": dt, "o": futures_data._f(b["o"]), "h": futures_data._f(b["h"]),
                        "l": futures_data._f(b["l"]), "c": futures_data._f(b["c"]),
                        "v": futures_data._f(b.get("v", 1)) or 1.0})
        # 对齐 scores（prepare 从 i=60 起，前面补 None）
        if len(out) != len(scores):
            n = min(len(out), len(scores))
            out, scores = out[:n], scores[:n]
        meta = config.VARIETIES.get(name, {})
        move = config.FUTURES_LIMIT_MOVE.get(sym, config.BACKTEST_LIMIT_LOCK)
        atrs = [None] * len(out)
        owners = bases = None
        feed = SymbolFeed(sym, name, meta.get("cat"), out, scores, atrs, owners, bases,
                          fee_table.get(sym), move)
        return sym, feed, None
    except Exception as e:
        return sym, None, f"{type(e).__name__}: {e}"


# =========================== 报告 ===========================
def _money(x):
    return f"{x:,.0f}"


def _pct(x, d=2):
    return "--" if x is None else f"{x * 100:.{d}f}%"


def build_report(pf, perf, args, feeds, errors, span, compare_block=""):
    L = ["=" * 108,
         f" 组合资金账户回测（第16轮 WP-E；第41轮 G26续 风险型sizing）  生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
         "=" * 108]
    mode = ("分钟%dm·%s" % (args.period, "日内(当日强平/平今)" if not args.swing else "摆动(跨日)")) \
        if not args.daily else "日线"
    sizing_desc = {"equal_notional": f"等名义(单品种目标名义{_pct(args.per_symbol, 0)})",
                   "equal_risk": f"等风险(单笔风险预算{_pct(args.risk_per_trade, 1)},止损{args.stop_atr}×ATR)",
                   "score": "按综合分档加权"}[args.sizing]
    if getattr(pf, "risk_sizing", None):
        rc = getattr(args, "_risk_cfg", None) or {}
        rname = {"inv_vol": "逆波动", "erc": "ERC风险平价"}.get(pf.risk_sizing, pf.risk_sizing)
        sizing_desc += (" + 横截面%s(过去%d根bar估协方差/每%d根重估/目标总敞口%.2f/单票上限%.0f%%,严格无未来)"
                        % (rname, rc.get("window", 126), rc.get("rebalance", 20),
                           pf.risk_gross, rc.get("cap", 0.2) * 100))
    L.append(f" 模式: {mode}  |  初始权益: {_money(args.equity)}元  |  手数分配: {sizing_desc}")
    L.append(f" 单品种名义上限{_pct(args.max_symbol, 0)} / 板块上限{_pct(args.max_sector, 0)} / "
             f"同时持仓≤{args.max_concurrent}  |  强平线风险度{_pct(args.risk_liquidate, 0)}→安全线{_pct(args.risk_safe, 0)}")
    n_real = len([s for s in feeds if s in pf.margin_table])
    fb = sorted(pf.fallback_margins)
    margin_txt = f"保证金表真实命中 {n_real}/{len(feeds)} 品种（期货公司收取档，as_of 见CSV）"
    if fb:
        margin_txt += f"；⚠ {len(fb)}品种缺表用兜底率{_pct(pf.default_margin, 0)}：{','.join(fb)}（估算，非精确值）"
    else:
        margin_txt += "；无兜底品种"
    L.append(" " + margin_txt)
    cost_txt = "零成本" if args.no_cost else f"真实券商手续费+单边滑点{args.slip_rate*1e4:.1f}‱"
    L.append(f" 成本: {cost_txt}  |  数据窗口: {span or '—'}  |  锁板: {'关闭' if args.no_limit_filter else '开启'}")
    if getattr(pf, "calibrator", None) is not None:
        applied = [x for x in pf.calib_log]
        if applied:
            avg_m = sum(x["mult"] for x in applied) / len(applied)
            L.append(" 历史胜率校准: 已启用（%d分钟周期、贝叶斯平滑、乘子裁剪%.1f~%.1f）；"
                     "实际调整开仓%d次，平均乘子%.3f，未达最小样本的信号仍按乘子1.0处理"
                     % (pf.calibrator.horizon, config.CALIBRATOR_MULT_LO, config.CALIBRATOR_MULT_HI,
                        len(applied), avg_m))
        else:
            L.append(" 历史胜率校准: 已启用但无分组达到最小样本，本轮全部信号乘子=1.0（等价未校准）")
    L.append("")
    if perf is None:
        L.append("无有效成交。")
        return "\n".join(L) + "\n"

    L.append("【一、组合账户绩效】（共享资金池、逐bar盯市；金额=人民币元）")
    L.append(f"  期末权益 {_money(perf['end_equity'])}（期初{_money(args.equity)}）｜"
             f"总收益 {_pct(perf['total_ret'])}｜年化 {_pct(perf['ann_ret'])}｜"
             f"夏普 {perf['sharpe']:.2f}｜索提诺 {perf['sortino']:.2f}｜最大回撤 {_pct(perf['max_dd'])}")
    L.append(f"  权益峰值 {_money(perf['peak_eq'])}（{_dt(perf['peak_dt'])}）｜"
             f"回撤谷底 {_money(perf['dd_bottom_eq'])}（{_dt(perf['dd_bottom_dt'])}）｜"
             f"覆盖 {perf['days']} 个交易日")
    L.append(f"  平均风险度 {_pct(perf['avg_risk'], 1)}｜峰值风险度 {_pct(perf['max_risk'], 1)}｜"
             f"最大同时持仓 {perf['max_npos']} 品种｜风控强平 {perf['n_liquidations']} 次｜"
             f"未开仓信号 {perf['n_skipped']} 次")
    if pf.skipped:
        sk = defaultdict(int)
        for x in pf.skipped:
            sk[x["reason"]] += 1
        L.append("  未开仓原因分布：" + "；".join(f"{k} {v}次" for k, v in
                 sorted(sk.items(), key=lambda x: -x[1])))
    L.append("")

    L.append("【二、成交与盈亏】")
    L.append(f"  平仓 {perf['n_trades']} 笔（组合层面，含手数）｜胜率 {_pct(perf['win_rate'], 1)}｜"
             f"平均盈 {_money(perf['avg_win'])} / 平均亏 {_money(perf['avg_loss'])}｜"
             f"盈亏比 {('--' if perf['pl_ratio'] is None else f'{perf['pl_ratio']:.2f}')}")
    L.append(f"  净盈亏合计 {_money(perf['total_pnl'])} 元｜累计手续费 {_money(perf['fees_paid'])} 元｜"
             f"强平仓次 {perf['n_liquidations']}")
    for label, pred in (("多头", lambda t: t["dir"] == "多"), ("空头", lambda t: t["dir"] == "空"),
                        ("平今", lambda t: t["leg"] == "平今"), ("风控强平", lambda t: t["forced"])):
        sub = [t for t in pf.closed if pred(t)]
        if sub:
            pnl = sum(t["net_yuan"] for t in sub)
            wr = sum(1 for t in sub if t["net_yuan"] > 0) / len(sub)
            L.append(f"  {label:<5} {len(sub):>4}笔  胜率{wr*100:5.1f}%  净盈亏{_money(pnl):>12}元")
    L.append("")

    L.append("【三、分品种成交汇总】（按净盈亏排序）")
    L.append("  品种   名称        板块     笔数  总手数  胜率    净盈亏(元)    手续费(元)  强平")
    by_sym = defaultdict(list)
    for t in pf.closed:
        by_sym[t["sym"]].append(t)
    for sym in sorted(by_sym, key=lambda s: sum(t["net_yuan"] for t in by_sym[s])):
        sub = by_sym[sym]
        wr = sum(1 for t in sub if t["net_yuan"] > 0) / len(sub)
        lots = sum(t["lots"] for t in sub)
        pnl = sum(t["net_yuan"] for t in sub)
        fee = sum(t["open_fee_yuan"] + t["close_fee_yuan"] for t in sub)
        nf = sum(1 for t in sub if t["forced"])
        L.append(f"  {sym:<6}{sub[0]['name']:<10}{str(sub[0]['sector']):<7}{len(sub):>4}{lots:>7}"
                 f"{wr*100:>7.1f}%{pnl:>14,.0f}{fee:>13,.0f}{nf:>5}")
    L.append("")

    if pf.liquidations:
        L.append("【四、风控强平事件（风险度破线，浮亏最大优先）】")
        for t in pf.liquidations[:30]:
            L.append(f"  {_dt(t['exit_dt'])} {t['sym']:<5}{t['dir']} {t['lots']}手 "
                     f"@ {t['exit_px']:.2f} 净盈亏 {t['net_yuan']:,.0f}元 原因:{t['reason']}")
        if len(pf.liquidations) > 30:
            L.append(f"  ……其余 {len(pf.liquidations) - 30} 起见 portfolio_trades.csv")
        L.append("")
    if errors:
        L.append("数据失败品种：" + "；".join(f"{s}({e})" for s, e in errors[:20]))
        L.append("")
    if compare_block:
        L.append(compare_block.rstrip("\n"))
        L.append("")
    L.append("-" * 108)
    L.append(" 口径：保证金率为期货公司常态收取档【估算】（临近交割/长假会上浮、公司可临时调整），"
             "非交易所/期货公司实时精确值；bar内成交按保守假设、非逐笔L2回放；不构成投资建议。")
    L.append("=" * 108)
    return "\n".join(L) + "\n"


def build_compare_block(runs):
    """第41轮 G26续：同宇宙 equal 基线 vs inv_vol/erc 影子对照（同一批 feeds 重置后确定性回放）。"""
    L = ["【附、同宇宙影子对照：横截面风险型 sizing（严格无未来；仅目标名义不同，约束链/撮合/成本完全一致）】"]
    L.append("  %-14s%12s%9s%8s%7s%9s%9s%8s%7s%9s" %
             ("方法", "期末权益", "总收益", "年化", "夏普", "最大回撤", "平均风险度", "最大持仓", "平仓", "平均有效N"))
    for label, pf, perf in runs:
        if perf is None:
            L.append(f"  {label:<14} 无有效成交")
            continue
        eff_n = pf.avg_risk_eff_n()
        L.append("  %-14s%12s%9s%8s%7.2f%9s%9s%8d%7d%9s" %
                 (label, _money(perf["end_equity"]), _pct(perf["total_ret"]),
                  _pct(perf["ann_ret"]), perf["sharpe"], _pct(perf["max_dd"]),
                  _pct(perf["avg_risk"], 1), perf["max_npos"], perf["n_trades"],
                  "--" if eff_n is None else f"{eff_n:.1f}"))
    L.append(" 说明：风险型只按协方差分配目标名义、不预测涨跌；低权重高价品种可能目标不足1手而不开仓（故笔数可不同，约束链一致）；")
    L.append("       未另计权重调仓换手成本；信号驱动持仓、实际为部分敞口；平均有效N越大越分散。")
    return "\n".join(L)


def _dt(x):
    return x.strftime("%Y-%m-%d %H:%M") if isinstance(x, datetime) else str(x)


EQUITY_FIELDS = ["dt", "static", "float", "equity", "margin", "available", "risk",
                 "drawdown", "npos"]
TRADE_FIELDS = ["sym", "name", "sector", "dir", "lots", "entry_dt", "exit_dt", "entry_px",
                "exit_px", "leg", "hold_bars", "gross_yuan", "open_fee_yuan", "close_fee_yuan",
                "net_yuan", "reason", "forced", "entry_score", "margin_rate"]


def write_outputs(pf, report):
    os.makedirs(os.path.dirname(config.PORTFOLIO_REPORT_FILE), exist_ok=True)
    with open(config.PORTFOLIO_REPORT_FILE, "w", encoding="utf-8-sig") as f:
        f.write(report)
    with open(config.PORTFOLIO_EQUITY_FILE, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EQUITY_FIELDS, lineterminator="\n")
        w.writeheader()
        for pt in pf.curve:
            row = dict(pt)
            row["dt"] = _dt(pt["dt"])
            w.writerow(row)
    with open(config.PORTFOLIO_TRADES_FILE, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS, lineterminator="\n")
        w.writeheader()
        for t in sorted(pf.closed, key=lambda x: (_dt(x["entry_dt"]), x["sym"])):
            w.writerow({k: t.get(k) for k in TRADE_FIELDS})


# =========================== CLI ===========================
def resolve_daily_items(codes_arg, limit=0):
    import backtest
    return backtest.resolve_codes(codes_arg, limit if limit > 0 else None)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="组合资金账户回测（多品种共享资金池+保证金+强平+权益曲线）")
    p.add_argument("--codes", default="", help="品种：RB/CU0/中文名/逗号分隔；留空=全64品种")
    p.add_argument("--all", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--daily", action="store_true", help="日线模式（默认分钟模式）")
    p.add_argument("--period", type=int, default=config.INTRADAY_BT_PERIOD, choices=(1, 5, 15, 30, 60))
    p.add_argument("--aggregate-from", type=int, default=0, choices=(0, 1, 5, 15, 30))
    p.add_argument("--lookback", type=int, default=config.INTRADAY_BT_LOOKBACK)
    p.add_argument("--sig-window", type=int, default=config.INTRADAY_BT_SIG_WINDOW)
    p.add_argument("--days", type=int, default=config.BACKTEST_LOOKBACK_DAYS, help="日线模式样本天数")
    p.add_argument("--hold", type=int, default=config.BACKTEST_HOLD_DAYS, help="日线固定持有根数")
    p.add_argument("--entry", type=float, default=config.INTRADAY_BT_ENTRY)
    p.add_argument("--stop-atr", type=float, default=config.INTRADAY_BT_STOP_ATR)
    p.add_argument("--target-atr", type=float, default=config.INTRADAY_BT_TARGET_ATR)
    p.add_argument("--max-bars", type=int, default=config.INTRADAY_BT_MAX_BARS)
    p.add_argument("--swing", action="store_true", help="分钟摆动模式（允许跨交易日）")
    p.add_argument("--equity", type=float, default=config.PORTFOLIO_EQUITY0)
    p.add_argument("--sizing", choices=("equal_notional", "equal_risk", "score"),
                   default=config.PORTFOLIO_SIZING)
    p.add_argument("--per-symbol", type=float, default=config.PORTFOLIO_PER_SYMBOL)
    p.add_argument("--risk-per-trade", type=float, default=config.PORTFOLIO_RISK_PER_TRADE)
    p.add_argument("--max-symbol", type=float, default=config.PORTFOLIO_MAX_SYMBOL_WEIGHT)
    p.add_argument("--max-sector", type=float, default=config.PORTFOLIO_MAX_SECTOR_WEIGHT)
    p.add_argument("--risk-liquidate", type=float, default=config.PORTFOLIO_RISK_LIQUIDATE)
    p.add_argument("--risk-safe", type=float, default=config.PORTFOLIO_RISK_SAFE)
    p.add_argument("--max-concurrent", type=int, default=config.PORTFOLIO_MAX_CONCURRENT)
    p.add_argument("--margins-file", default=config.FUTURES_MARGINS_FILE)
    p.add_argument("--fees-file", default=config.FUTURES_FEES_FILE)
    p.add_argument("--fee-rate", type=float, default=config.INTRADAY_BT_FEE_RATE)
    p.add_argument("--slip-rate", type=float, default=config.INTRADAY_BT_SLIP_RATE)
    p.add_argument("--no-real-fees", action="store_true")
    p.add_argument("--no-cost", action="store_true")
    p.add_argument("--no-limit-filter", action="store_true")
    p.add_argument("--calibrate", action="store_true",
                   help="WP-F2：启用历史同类信号胜率校准乘子作用于手数（默认关闭=影子，逐值与旧版一致）")
    # 第41轮 G26续：横截面风险型 sizing（默认全关=逐字节等价旧等名义）
    p.add_argument("--risk-sizing", choices=("", "inv_vol", "erc"), default="",
                   help="横截面风险型目标权重：inv_vol逆波动/erc风险平价；留空=关闭走旧sizing")
    p.add_argument("--risk-window", type=int, default=config.PRS_WINDOW,
                   help="估协方差的历史bar数（日线=交易日；分钟=bar根数，只用当前bar之前=PIT）")
    p.add_argument("--risk-rebalance", type=int, default=config.PRS_REBAL, help="权重重估间隔（bar根数）")
    p.add_argument("--risk-min-hist", type=int, default=config.PRS_MIN_HIST, help="纳入宇宙的最少收益根数")
    p.add_argument("--risk-gross", type=float, default=config.PRS_GROSS, help="权重和=1后的目标总敞口")
    p.add_argument("--risk-cap", type=float, default=config.PRS_CAP, help="单品种目标权重上限")
    p.add_argument("--compare-risk", action="store_true",
                   help="同宇宙影子对照：等名义基线+inv_vol+erc 各回放一次并出对照表（基线CSV不变）")
    p.add_argument("--workers", type=int, default=6)
    args = p.parse_args(argv)
    args.flat_eod = not args.swing
    args.use_limit = not args.no_limit_filter
    args.use_real_fees = not args.no_real_fees
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.no_cost:
        args.fee_rate, args.slip_rate = 0.0, 0.0
        args.use_real_fees = False
    fee_table = load_fee_schedule(args.fees_file) if args.use_real_fees else {}
    margin_table = load_margin_schedule(args.margins_file)
    sector_of = {m["sym"]: m["cat"] for m in config.VARIETIES.values()}
    name_of = {m["sym"]: name for name, m in config.VARIETIES.items()}

    if args.daily:
        items = resolve_daily_items(args.codes, args.limit)
        label = f"日线组合：{len(items)}品种"
    else:
        import intraday_backtest as ib
        items = ib.resolve_items(args.codes, args.limit)
        label = f"分钟{args.period}m组合：{len(items)}品种"
    print(label + f"，初始权益{args.equity:,.0f}元，手数策略{args.sizing}，保证金表{len(margin_table)}品种")

    feeds, errors = {}, []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        if args.daily:
            futs = {ex.submit(load_daily_feed, it, args.days, args.hold, args.entry, fee_table): it[0]
                    for it in items}
        else:
            futs = {ex.submit(load_minute_feed, it, args, fee_table, margin_table): it[0]
                    for it in items}
        for fut in as_completed(futs):
            sym, feed, err = fut.result()
            if err:
                errors.append((sym, err))
                print(f"  [跳过] {sym}: {err}")
            else:
                feeds[sym] = feed
    if not feeds:
        print("无可用品种，退出。")
        return 2

    calib, _cdb = None, None
    if getattr(args, "calibrate", False):
        import storage
        import signal_calibrator
        _cdb = storage.MonitorDB()
        calib = signal_calibrator.SignalCalibrator(_cdb)
        print("已启用历史胜率校准：%d分钟周期、分组样本≥%d，内存统计分组%d个（样本不足自动回退层级）"
              % (calib.horizon, calib.min_n, len(calib.groups)))

    def _risk_cfg(method):
        return None if not method else {
            "method": method, "window": args.risk_window, "rebalance": args.risk_rebalance,
            "min_hist": args.risk_min_hist, "shrink": config.PC_SHRINK,
            "cap": args.risk_cap, "gross": args.risk_gross}

    def _run_once(method):
        """同一批 feeds 重置后确定性回放一次；method 为空=旧等名义基线（risk_cfg=None）。"""
        _reset_feeds(feeds)
        rcfg = _risk_cfg(method)
        pf = Portfolio(args.equity, margin_table, fee_table, sizing=args.sizing,
                       calibrator=calib,
                       per_symbol=args.per_symbol, risk_per_trade=args.risk_per_trade,
                       stop_atr=args.stop_atr, score_weights=config.PORTFOLIO_SCORE_WEIGHTS,
                       max_symbol_weight=args.max_symbol, max_sector_weight=args.max_sector,
                       risk_liquidate=args.risk_liquidate, risk_safe=args.risk_safe,
                       default_margin=config.PORTFOLIO_DEFAULT_MARGIN,
                       max_concurrent=args.max_concurrent, fee_rate=args.fee_rate,
                       slip_rate=args.slip_rate, use_real_fees=args.use_real_fees,
                       sector_of=sector_of, risk_sizing=method or None,
                       risk_gross=args.risk_gross)
        args._risk_cfg = rcfg
        run_portfolio(feeds, pf, entry_th=args.entry, stop_atr=args.stop_atr,
                      target_atr=args.target_atr, flat_eod=args.flat_eod, max_bars=args.max_bars,
                      use_limit=args.use_limit, limit_eps=config.INTRADAY_BT_LIMIT_TICK_EPS,
                      minute_mode=not args.daily, hold_days=args.hold, risk_cfg=rcfg)
        return pf, pf.performance()

    # 基线（旧等名义；--risk-sizing 指定时基线改为该风险型单次运行）
    primary_method = args.risk_sizing
    pf, perf = _run_once(primary_method)
    compare_block = ""
    if args.compare_risk:
        labels = {"": "等名义(基线)", "inv_vol": "逆波动", "erc": "ERC风险平价"}
        # 对照始终以等名义为基线首行（三种方法同宇宙、同撮合、同成本，仅目标名义不同）
        compare_runs = []
        for m in ("", "inv_vol", "erc"):
            pfp, pp = _run_once(m)
            compare_runs.append((labels[m], pfp, pp))
            if m == primary_method:
                pf, perf = pfp, pp
        compare_block = build_compare_block(compare_runs)
    firsts = sorted(f.bars[0]["dt"] for f in feeds.values())
    lasts = sorted(f.bars[-1]["dt"] for f in feeds.values())
    span = f"{_dt(firsts[0])} ~ {_dt(lasts[-1])}"
    report = build_report(pf, perf, args, feeds, errors, span, compare_block=compare_block)
    write_outputs(pf, report)
    # G27① 统一实验台账：仅 --compare-risk 影子对照时登记三法对照（旁路，绝不改基线CSV/报告口径）
    if args.compare_risk:
        try:
            import experiment_ledger as el
            method_key = {"等名义(基线)": "equal", "逆波动": "inv_vol", "ERC风险平价": "erc"}
            cr_metrics = {}
            for label, _pfp, pp in compare_runs:
                key = method_key.get(label, label)
                cr_metrics[key] = None if pp is None else {
                    k: pp.get(k) for k in
                    ("end_equity", "total_ret", "ann_ret", "sharpe", "max_dd",
                     "avg_risk", "max_npos", "n_trades")}
            el.safe_record(
                "portfolio.compare_risk",
                {"mode": "daily" if args.daily else "%dm" % args.period,
                 "codes": sorted(feeds.keys()), "n_symbols": len(feeds), "sizing": args.sizing,
                 "risk_window": args.risk_window, "risk_rebalance": args.risk_rebalance,
                 "risk_min_hist": args.risk_min_hist, "risk_gross": args.risk_gross,
                 "risk_cap": args.risk_cap, "entry": args.entry, "stop_atr": args.stop_atr,
                 "target_atr": args.target_atr, "real_fees": args.use_real_fees},
                cr_metrics,
                inputs=[args.fees_file, args.margins_file],
                artifacts=[config.PORTFOLIO_REPORT_FILE, config.PORTFOLIO_EQUITY_FILE,
                           config.PORTFOLIO_TRADES_FILE],
                conclusion="同宇宙三法对照 %s（%d品种，基线CSV逐字节不变）" % (span, len(feeds)))
        except Exception:
            pass
    print("\n" + report[:3500])
    if compare_block and compare_block not in report[:3500]:
        print("\n" + compare_block)   # 报告头被截断时补打对照表；未截断则不重复
    print(f"\n报告: {config.PORTFOLIO_REPORT_FILE}\n权益曲线: {config.PORTFOLIO_EQUITY_FILE}"
          f"\n组合成交: {config.PORTFOLIO_TRADES_FILE}")
    if _cdb is not None:
        _cdb.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
