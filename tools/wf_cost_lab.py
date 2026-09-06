# -*- coding: utf-8 -*-
r"""G27②③（第45轮）walk-forward 参数稳定性 + 成本敏感性曲面/换手容量：tools/wf_cost_lab.py。

纯标准库、零网络；真实回放层惰性 import 项目内核（intraday_backtest/backtest_validation/config/
storage/backtest），纯函数层不 import 任何项目模块（零 DB 可确定性单测）。研究侧只读 monitor.db、
只写 reports/wf_cost_lab.txt|.json，不接 main、不改综合分、不改任何既有回测/默认 CSV。

两块能力（总纲 G27 剩余的②③；①统一实验台账已在第44轮 experiment_ledger 落地）：
- ② **walk-forward 滚动参数稳定性轨迹**：复用 backtest_validation.build_param_grid_matrix（同一套
  信号/撮合，对 entry/stop/target 稳定性网格逐组合回放成 T×N 日收益矩阵）+ walk_forward（滚动 IS 窗
  选最优参数、下一 OOS 窗验证）。本工具补"跨品种批量组织 + 选中参数轨迹 + 稳定度评级 + sidecar"，
  回答"最优参数是稳定锚定还是每窗都在换、样本内优势到样本外衰减多少"。
- ③ **fee/slip 成本敏感性曲面 + 换手容量**：固定全样本最优参数，在 每腿费率×滑点 网格上重放，出
  净复利/夏普/胜率曲面与"成本加到多少策略由盈转亏（break-even）"；并用分钟 bar 成交量做换手率与
  可承载资金的**数量级**估算（免费数据无盘口深度，明确为线性、忽略冲击的粗估，精确容量待 G14）。

口径声明：成本曲面用 simulate 的"兜底比例费率"模式（use_real_fees=False，每腿收 fee_rate、滑点 slip
按方向不利偏移成交价），以便精确扫描成本档位；与真实费率表（手续费+平今减免/乘数）口径不同，结果用于
"相对成本敏感度/安全垫倍数"，不是绝对盈亏预测。
"""
import argparse
import io
import json
import os
import statistics
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DEFAULT_OUT = os.path.join(_ROOT, "reports", "wf_cost_lab.txt")
DEFAULT_JSON = os.path.join(_ROOT, "reports", "wf_cost_lab.json")

# 默认成本扫描网格（每腿费率 / 单边滑点率），基准格 = fee 5e-5、slip 1e-4（=config 兜底默认）
DEFAULT_FEE_GRID = (0.0, 2.5e-5, 5e-5, 1.0e-4, 2.0e-4)
DEFAULT_SLIP_GRID = (0.0, 0.5e-4, 1.0e-4, 2.0e-4, 4.0e-4)
BASE_FEE = 5e-5
BASE_SLIP = 1e-4
TRADING_DAYS_YEAR = 243          # 商品期货年交易日近似
DEFAULT_PARTICIPATION_CAP = 0.10  # 容量估算：策略日均成交不超过市场日均名义的 10%（参与率上限）
DEFAULT_CODES = ("RB", "MA", "I", "TA")   # 默认代表品种（流动性好、覆盖黑色/化工），--codes/--all 覆盖

SEP = "=" * 96
SUB = "-" * 96


# =========================== 纯统计（不依赖项目模块） ===========================
def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else 0.0


def _std(xs):
    xs = [x for x in xs if x is not None]
    return statistics.pstdev(xs) if len(xs) >= 2 else 0.0


def per_trade_sharpe(nets):
    """笔口径夏普=均值/标准差（不年化，仅用于同口径横向比较）；<2笔返0。"""
    if len(nets) < 2:
        return 0.0
    sd = _std(nets)
    return (_mean(nets) / sd) if sd > 1e-15 else 0.0


def compound(nets):
    """逐笔净收益复利：∏(1+r)-1。"""
    v = 1.0
    for r in nets:
        v *= (1.0 + r)
    return v - 1.0


def summarize_trades(trades):
    """单笔列表 → 关键指标（全 .get 防御，空列表给零值结构）。"""
    n = len(trades)
    nets = [t.get("net", 0.0) for t in trades]
    gross = [t.get("gross", 0.0) for t in trades]
    holds = [t.get("hold_bars") for t in trades if t.get("hold_bars") is not None]
    wins = [x for x in nets if x > 0]
    gross_sum = sum(gross)
    cost_sum = gross_sum - sum(nets)
    return {
        "n_trades": n,
        "win_rate": (len(wins) / n) if n else None,
        "mean_net": (_mean(nets) if n else None),
        "median_net": (statistics.median(nets) if n else None),
        "total_compound": (compound(nets) if n else 0.0),
        "sum_gross": gross_sum,
        "sum_cost": cost_sum,
        "cost_per_trade": (cost_sum / n if n else None),
        "cost_to_gross": (cost_sum / abs(gross_sum) if abs(gross_sum) > 1e-15 else None),
        "per_trade_sharpe": per_trade_sharpe(nets),
        "avg_hold_bars": (_mean(holds) if holds else None),
    }


# =========================== ③ 成本敏感性曲面 ===========================
def build_cost_surface(runner, fee_grid=DEFAULT_FEE_GRID, slip_grid=DEFAULT_SLIP_GRID):
    """runner(fee, slip) -> trades 列表（真实层闭包到 ib.simulate；测试层注入假 runner）。
    返回 {fee_grid, slip_grid, rows:[{fee, cells:[按slip的summarize]}], base:{fee,slip,idx}}。"""
    fee_grid = tuple(fee_grid); slip_grid = tuple(slip_grid)
    rows = []
    for fee in fee_grid:
        cells = []
        for slip in slip_grid:
            trades = runner(fee, slip)
            cells.append(summarize_trades(trades))
        rows.append({"fee": fee, "cells": cells})
    base = {"fee": BASE_FEE, "slip": BASE_SLIP,
            "fi": _nearest_idx(fee_grid, BASE_FEE), "si": _nearest_idx(slip_grid, BASE_SLIP)}
    return {"fee_grid": list(fee_grid), "slip_grid": list(slip_grid), "rows": rows, "base": base}


def _nearest_idx(grid, val):
    return min(range(len(grid)), key=lambda i: abs(grid[i] - val))


def surface_matrix(surface, key="total_compound"):
    """抽成 [fee][slip] 的标量矩阵。"""
    return [[c.get(key) for c in row["cells"]] for row in surface["rows"]]


def breakeven_cost(surface):
    """沿基准 slip 列扫 fee、沿基准 fee 行扫 slip，找指标(total_compound)由正转负的临界档。
    返回 {fee:{base_value, first_negative, breakeven_between, safety_x}, slip:{...}}；
    safety_x = 临界相对基准的倍数（成本安全垫，越大越扛成本）。"""
    out = {}
    fi, si = surface["base"]["fi"], surface["base"]["si"]
    comp = surface_matrix(surface, "total_compound")
    # 沿 fee（固定基准 slip 列 si）
    out["fee"] = _breakeven_axis([surface["fee_grid"][i] for i in range(len(comp))],
                                 [comp[i][si] for i in range(len(comp))],
                                 surface["base"]["fee"])
    # 沿 slip（固定基准 fee 行 fi）
    out["slip"] = _breakeven_axis(surface["slip_grid"], comp[fi], surface["base"]["slip"])
    return out


def _breakeven_axis(grid, vals, base_val):
    first_neg = None
    for g, v in zip(grid, vals):
        if v is not None and v <= 0:
            first_neg = g
            break
    # 安全垫：首个转负档相对基准的倍数（基准本身就≤0 记 0；全程为正记 None=扛住整档）
    if first_neg is None:
        safety = None
    elif base_val <= 0:
        safety = 0.0
    else:
        safety = (first_neg / base_val) if base_val > 0 else None
    return {"grid": list(grid), "values": vals, "base": base_val,
            "first_negative": first_neg, "safety_x": safety}


# =========================== ② walk-forward 参数稳定性 ===========================
def wf_stability(segments, names):
    """从 backtest_validation.walk_forward 的 segments 提炼参数稳定性轨迹与评级。
    segments: [{chosen(候选idx), is_sharpe, oos_sharpe, oos_best, oos_median, beat_median}]
    names: 候选参数名列表（idx→名）。"""
    k = len(segments)
    chosen_idx = [seg.get("chosen") for seg in segments]
    chosen_names = [names[i] if (i is not None and 0 <= i < len(names)) else "?" for i in chosen_idx]
    # 每个候选被选次数
    votes = {}
    for nm in chosen_names:
        votes[nm] = votes.get(nm, 0) + 1
    top_name, top_votes = (max(votes.items(), key=lambda kv: kv[1]) if votes else ("?", 0))
    top_share = (top_votes / k) if k else 0.0
    switches = sum(1 for a, b in zip(chosen_idx, chosen_idx[1:]) if a != b)
    is_list = [seg.get("is_sharpe") for seg in segments if seg.get("is_sharpe") is not None]
    oos_list = [seg.get("oos_sharpe") for seg in segments if seg.get("oos_sharpe") is not None]
    best_list = [seg.get("oos_best") for seg in segments if seg.get("oos_best") is not None]
    beat = [seg for seg in segments if seg.get("beat_median")]
    oos_pos = sum(1 for v in oos_list if v > 0)
    mean_is = _mean(is_list); mean_oos = _mean(oos_list); mean_best = _mean(best_list)
    decay = (mean_oos - mean_is) if is_list and oos_list else None      # IS→OOS 衰减（负=缩水）
    regret = (mean_best - mean_oos) if best_list and oos_list else None  # 选参遗憾=事后最优-实际选
    # 评级：锚定率 top_share 为主，辅以 OOS 正段比与跑赢中位数比例
    if k == 0:
        grade = "样本不足"
    elif top_share >= 0.6 and (oos_pos / k) >= 0.6:
        grade = "稳定"
    elif top_share >= 0.4:
        grade = "一般"
    else:
        grade = "漂移"
    return {"n_segments": k, "chosen_sequence": chosen_names, "votes": votes,
            "top_param": top_name, "top_share": top_share, "switches": switches,
            "switch_rate": (switches / (k - 1)) if k >= 2 else None,
            "mean_is_sharpe": mean_is, "mean_oos_sharpe": mean_oos,
            "mean_oos_best": mean_best, "is_oos_decay": decay, "selection_regret": regret,
            "oos_positive_rate": (oos_pos / k) if k else None,
            "beat_median_rate": (len(beat) / k) if k else None, "grade": grade}


# =========================== 换手与容量（数量级估算） ===========================
def estimate_turnover_capacity(bars, trades, multiplier, participation_cap=DEFAULT_PARTICIPATION_CAP,
                               days_year=TRADING_DAYS_YEAR):
    """用分钟 bar 成交量与成交记录估换手率与可承载资金（数量级、线性、忽略冲击；精确容量待 G14 盘口）。
    bars: [{dt/d,o,h,l,c,v}]；trades: simulate 输出；multiplier: 合约乘数（每手对应标的数量）。"""
    mult = float(multiplier) if multiplier else 0.0
    # 市场侧：按交易日聚合成交量与均价
    day_vol, day_notional, days_seen = {}, {}, set()
    for b in bars:
        dt = b.get("dt")
        day = (dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10])
        c = float(b.get("c") or 0.0); v = float(b.get("v") or 0.0)
        days_seen.add(day)
        day_vol[day] = day_vol.get(day, 0.0) + v
        day_notional[day] = day_notional.get(day, 0.0) + v * c * mult
    n_days = len(days_seen)
    mkt_daily_notional = _mean(list(day_notional.values())) if day_notional else 0.0
    # 策略侧（默认按 1 手计）
    n = len(trades)
    entry_px = [float(t.get("entry_px") or 0.0) for t in trades]
    avg_px = _mean(entry_px)
    notional_per_lot = avg_px * mult
    trades_per_day = (n / n_days) if n_days else 0.0
    strat_daily_notional_1lot = trades_per_day * notional_per_lot
    annual_turnover_lots = trades_per_day * days_year      # 1手口径年换手笔数
    # 参与率上限下可承载的"同时等效手数/资金（名义口径）"
    max_notional = mkt_daily_notional * participation_cap
    max_lots = (max_notional / notional_per_lot / trades_per_day) if (notional_per_lot > 0 and trades_per_day > 0) else None
    max_capital_notional = (max_lots * notional_per_lot) if max_lots is not None else None
    return {"n_days": n_days, "mkt_daily_notional": mkt_daily_notional,
            "n_trades": n, "trades_per_day": trades_per_day,
            "notional_per_lot": notional_per_lot, "strat_daily_notional_1lot": strat_daily_notional_1lot,
            "annual_turnover_lots_1lot": annual_turnover_lots,
            "participation_cap": participation_cap,
            "max_lots_per_trade": max_lots, "max_capital_notional": max_capital_notional,
            "assumption": "线性、忽略价格冲击；市场名义=Σ分钟量×收盘价×乘数；免费数据无盘口深度，仅数量级，精确容量待G14"}


# =========================== 真实回放层（惰性 import 项目内核） ===========================
def _parse_combo_name(name):
    """'e1.5/s2/t2' -> (1.5,2.0,2.0)。"""
    e = s = t = None
    for part in name.split("/"):
        if part.startswith("e"):
            e = float(part[1:])
        elif part.startswith("s"):
            s = float(part[1:])
        elif part.startswith("t"):
            t = float(part[1:])
    return e, s, t


def run_symbol(sym, period=30, wf_train=20, wf_test=10,
               fee_grid=DEFAULT_FEE_GRID, slip_grid=DEFAULT_SLIP_GRID,
               participation_cap=DEFAULT_PARTICIPATION_CAP, verbose=False,
               purge=0, embargo=0, wf_presets=None):
    """单品种：参数网格 WF 稳定性 + 最优参数成本曲面 + 换手容量。真实读分钟库（只读）。

    第52轮 G27续：purge/embargo=AFML 防前视隔离带（透传 bv.walk_forward，默认0等价旧版）；
    wf_presets=多周期 [(train,test),...]，对每个窗口规格各跑一次 WF 稳定性做对照，
    缺省 None=仅 (wf_train,wf_test) 单规格（旧行为），其结果仍填 stability/wf_summary。"""
    import config
    import backtest_validation as bv
    import intraday_backtest as ib

    # ② 参数网格 + walk-forward（复用既有引擎）
    grid = bv.build_param_grid_matrix(sym, period=period)
    names = grid["names"]; matrix = grid["matrix"]
    presets = wf_presets or [(wf_train, wf_test)]
    wf_multi = []
    for (tr, te) in presets:
        wf_x = bv.walk_forward(matrix, tr, te, purge=purge, embargo=embargo) \
            if len(matrix) >= tr + embargo + te else {"segments": [], "n_segments": 0}
        stab_x = wf_stability(wf_x.get("segments", []), names)
        wf_multi.append({"train": tr, "test": te, "purge": purge, "embargo": embargo,
                         "n_segments": wf_x.get("n_segments", 0),
                         "stability": stab_x,
                         "wf_summary": {k: wf_x.get(k) for k in
                                        ("mean_is_sharpe", "mean_oos_sharpe", "mean_oos_best",
                                         "is_oos_decay", "oos_beat_median_rate", "param_switch_rate")}})
    # 默认组=第一个 preset，向后兼容字段
    wf = {"segments": [], "n_segments": wf_multi[0]["n_segments"]}
    stab = wf_multi[0]["stability"]

    # 选全样本笔夏普最高参数做成本曲面（若 WF 段不足，仍可做成本曲面）
    total_perf = grid.get("total_perf", {})
    best_name = max(total_perf, key=lambda k: total_perf[k]) if total_perf else names[0]
    e, s, t = _parse_combo_name(best_name)

    # 重新装载一次 bars/prepare（build_param_grid_matrix 内部已装载但未返回对象，这里独立装载以跑曲面）
    import storage
    from backtest import ratio_adjusted_bars
    db = storage.MonitorDB()
    try:
        items = ib.resolve_items(sym)
        if not items:
            raise ValueError("无法解析品种 %r" % sym)
        sym_code, code, cname = items[0]
        raw, src = ib.load_minute_bars(db, sym_code, period, 0, 0)
    finally:
        db.close()
    from backtest import load_fee_schedule
    bars, _roll = ratio_adjusted_bars(raw)
    prepared = ib.prepare_series(bars, config.INTRADAY_BT_SIG_WINDOW)
    owners, bases = ib.build_owner_meta(bars)
    move = config.FUTURES_LIMIT_MOVE.get(sym_code, config.INTRADAY_BT_LIMIT_MOVE)
    # 曲面用兜底费率精确扫档（simulate 内 multiplier 会为0），乘数单独从真实费率表取供容量估算
    fee_row = (load_fee_schedule(config.FUTURES_FEES_FILE).get(sym_code) or {})
    real_multiplier = fee_row.get("multiplier")

    def runner(fee, slip):
        tr, _, _ = ib.simulate(
            sym_code, bars, prepared, owners, bases, e, s, t,
            config.INTRADAY_BT_FLAT_EOD, config.INTRADAY_BT_MAX_BARS,
            slip, None, False, fee, True, move, config.INTRADAY_BT_LIMIT_TICK_EPS)
        return tr

    surface = build_cost_surface(runner, fee_grid, slip_grid)
    be = breakeven_cost(surface)
    base_cell = surface["rows"][surface["base"]["fi"]]["cells"][surface["base"]["si"]]
    # 容量用基准档成交记录
    base_trades = runner(BASE_FEE, BASE_SLIP)
    multiplier = real_multiplier or (base_trades[0].get("multiplier") if base_trades else None)
    capacity = estimate_turnover_capacity(bars, base_trades, multiplier, participation_cap)

    return {"sym": sym_code, "name": cname, "period": period, "bars": grid["bars"],
            "n_days": len(grid["days"]), "n_combos": len(names),
            "wf_train": wf_train, "wf_test": wf_test, "purge": purge, "embargo": embargo,
            "wf_summary": wf_multi[0]["wf_summary"], "wf_multi": wf_multi,
            "stability": stab, "best_param": best_name,
            "surface": surface, "breakeven": be, "base_cell": base_cell,
            "capacity": capacity, "src": grid.get("src")}


# =========================== 成稿 ===========================
def _pct(x, nd=1):
    if x is None:
        return "—"
    try:
        return "{:,.{n}f}%".format(float(x) * 100.0, n=nd)
    except (TypeError, ValueError):
        return "—"


def _bp(x, nd=2):
    """费率/滑点转 bp（×10000）。"""
    if x is None:
        return "—"
    return "%gbp" % round(float(x) * 1e4, nd)


def _money_wan(x):
    if not x:
        return "—"
    return "%.1f万" % (x / 1e4)


def render_symbol(res):
    L = []
    st = res["stability"]; cap = res["capacity"]; be = res["breakeven"]; base = res["base_cell"]
    L.append("◆ %s（%s，%dm，%d根bar/%d交易日，%d组参数网格，WF=%d训/%d测）" %
             (res["sym"], res.get("name", ""), res["period"], res["bars"], res["n_days"],
              res["n_combos"], res["wf_train"], res["wf_test"]))
    L.append(SUB)
    # ② 稳定性
    L.append("  ② walk-forward 参数稳定性（评级：%s）" % st["grade"])
    seq = " → ".join(st["chosen_sequence"]) if st["chosen_sequence"] else "（样本不足，窗太短）"
    L.append("     选中参数轨迹：%s" % seq)
    L.append("     最常被选：%s（%s 窗次，占比%s）；参数切换 %d 次（切换率%s）" %
             (st["top_param"], st["votes"].get(st["top_param"], 0), _pct(st["top_share"]),
              st["switches"], _pct(st["switch_rate"])))
    L.append("     样本内夏普均值 %s → 样本外 %s（衰减 %s）；OOS为正段占比%s、跑赢OOS中位数%s、选参遗憾%s" %
             (_r(st["mean_is_sharpe"], 3), _r(st["mean_oos_sharpe"], 3), _r(st["is_oos_decay"], 3),
              _pct(st["oos_positive_rate"]), _pct(st["beat_median_rate"]), _r(st["selection_regret"], 3)))
    # 第52轮：多周期窗口 + purge/embargo 隔离带对照（默认单规格且无隔离带时不重复输出）
    multi = res.get("wf_multi") or []
    iso = res.get("purge", 0) or res.get("embargo", 0)
    if len(multi) > 1 or iso:
        L.append("     多周期/防前视对照（purge=%s、embargo=%s；隔离带越严OOS越可信，评级应不随窗口跳变才算稳）："
                 % (res.get("purge", 0), res.get("embargo", 0)))
        for mm in multi:
            sx = mm["stability"]
            L.append("       窗%2d训/%2d测：%d段 评级%-4s 最常选%-12s 锚定%s 切换%s OOS夏普%s（IS%s 衰减%s）"
                     % (mm["train"], mm["test"], mm["n_segments"], sx["grade"], sx["top_param"],
                        _pct(sx["top_share"]), sx["switches"], _r(sx["mean_oos_sharpe"], 3),
                        _r(sx["mean_is_sharpe"], 3), _r(sx["is_oos_decay"], 3)))
    # ③ 成本曲面（固定最优参数）
    L.append("  ③ 成本敏感性曲面（固定全样本最优参数 %s；行=每腿费率，列=单边滑点；值=逐笔复利净收益）" % res["best_param"])
    surf = res["surface"]
    head = "           " + "".join("%12s" % _bp(sp) for sp in surf["slip_grid"])
    L.append("    " + head)
    for i, row in enumerate(surf["rows"]):
        mark_i = i == surf["base"]["fi"]
        cells = []
        for j, c in enumerate(row["cells"]):
            v = c["total_compound"]
            tag = "*" if (mark_i and j == surf["base"]["si"]) else " "
            cells.append("%11s%s" % (_pct(v, 1), tag))
        L.append("    %8s%s" % (_bp(row["fee"]), "".join(cells)))
    L.append("     基准格（*，fee%s/slip%s）：%d笔 胜率%s 笔夏普%s 复利%s；成本/|毛利|=%s" %
             (_bp(BASE_FEE), _bp(BASE_SLIP), base["n_trades"], _pct(base["win_rate"]),
              _r(base["per_trade_sharpe"], 3), _pct(base["total_compound"], 1), _pct(base["cost_to_gross"])))
    for axis, label in (("fee", "每腿费率"), ("slip", "单边滑点")):
        b = be[axis]
        if b["first_negative"] is None:
            L.append("     沿%s：扫描全程净收益未转负（扛住最高档 %s）" % (label, _bp(max(b["grid"]))))
        elif b["safety_x"] == 0:
            L.append("     沿%s：基准档已不盈利，首个转负档 %s" % (label, _bp(b["first_negative"])))
        else:
            L.append("     沿%s：首个净收益转负档 %s ≈ 基准的 %.1f 倍（成本安全垫）" %
                     (label, _bp(b["first_negative"]), b["safety_x"]))
    # 换手容量
    L.append("     换手/容量（数量级估算，参与率上限%.0f%%）：日均%.2f笔、1手年换手%.0f笔；市场日均名义%s、单手名义%s；"
             % (cap["participation_cap"] * 100, cap["trades_per_day"], cap["annual_turnover_lots_1lot"],
                _money_wan(cap["mkt_daily_notional"]), _money_wan(cap["notional_per_lot"])))
    L.append("             参与率上限下可承载每笔约 %.0f 手、对应名义资金约 %s（%s，库内bar覆盖口径）" %
             ((cap["max_lots_per_trade"] or 0.0), _money_wan(cap["max_capital_notional"]), "线性忽略冲击，精确待G14"))
    L.append("")
    return "\n".join(L)


def _r(x, nd=3):
    if x is None:
        return "—"
    try:
        return "{:,.{n}f}".format(float(x), n=nd)
    except (TypeError, ValueError):
        return "—"


def build_report(results, args_codes, period):
    iso_txt = ""
    if results:
        r0 = results[0]
        multi = r0.get("wf_multi") or []
        if len(multi) > 1:
            iso_txt += " ｜ WF窗口 " + "/".join("%d训%d测" % (m["train"], m["test"]) for m in multi)
        if r0.get("purge", 0) or r0.get("embargo", 0):
            iso_txt += " ｜ purge=%d embargo=%d（AFML防前视隔离带）" % (r0.get("purge", 0), r0.get("embargo", 0))
    L = [SEP,
         "G27②③ walk-forward 参数稳定性 + 成本敏感性曲面/换手容量（wf_cost_lab，研究侧只读、纯标准库）",
         "品种 %s ｜ 周期 %dm ｜ 成本网格 fee=%s × slip=%s（bp）%s ｜ 生成 %s" %
         (",".join(args_codes), period,
          "/".join(_bp(x, 1) for x in DEFAULT_FEE_GRID),
          "/".join(_bp(x, 1) for x in DEFAULT_SLIP_GRID), iso_txt,
          __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
         SEP]
    if not results:
        L.append("（无有效品种结果：分钟库可能缺数，换 --codes 指定有数据的品种）")
        L.append(SEP)
        return "\n".join(L)
    # 总览表
    L.append("〇、总览（稳定评级 / 最优参数 / OOS夏普 / 基准复利 / 成本安全垫fee×slip / 日均笔数）")
    L.append(SUB)
    for res in results:
        st = res["stability"]; sx_fee = res["breakeven"]["fee"]["safety_x"]; sx_sl = res["breakeven"]["slip"]["safety_x"]
        L.append("  %-4s 评级%-4s 最优%-12s OOS夏普%s 基准复利%8s 安全垫 %s×%s 日均%.2f笔" %
                 (res["sym"], st["grade"], res["best_param"], _r(st["mean_oos_sharpe"], 2),
                  _pct(res["base_cell"]["total_compound"], 1),
                  ("∞" if sx_fee is None else _r(sx_fee, 1)),
                  ("∞" if sx_sl is None else _r(sx_sl, 1)),
                  res["capacity"]["trades_per_day"]))
    L.append("")
    for res in results:
        L.append(render_symbol(res))
    L.append(SEP)
    L.append("口径：成本曲面用兜底比例费率模式（每腿 fee_rate、滑点按方向不利偏移），用于相对敏感度/安全垫，非绝对盈亏；")
    L.append("      WF 复用 backtest_validation 同一信号/撮合与滚动 IS选参-OOS验证，无未来函数；容量为线性数量级估算（无盘口深度，精确待 G14）。")
    L.append("      研究侧只读、不接 main、不改综合分/默认CSV；结论只提示风险，调参仍走双样本+影子+默认回退。")
    L.append(SEP)
    return "\n".join(L)


def build_json_payload(results):
    payload = {"generated": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "n_symbols": len(results), "results": results}
    json.dumps(payload, ensure_ascii=False, allow_nan=False)
    return payload


# =========================== CLI ===========================
def _all_codes():
    import config
    return [c for c in getattr(config, "INTRADAY_BT_UNIVERSE", []) or []]


def run(argv=None):
    ap = argparse.ArgumentParser(description="G27②③ WF参数稳定性+成本敏感性曲面/换手容量（研究侧只读）")
    ap.add_argument("--codes", default=",".join(DEFAULT_CODES), help="逗号分隔品种，默认 RB,MA,I,TA")
    ap.add_argument("--all", action="store_true", help="用 config 回测宇宙全品种")
    ap.add_argument("--period", type=int, default=30, choices=(1, 5, 15, 30, 60))
    ap.add_argument("--wf-train", default="20", help="IS 窗，可逗号多值做多周期对照，如 20,40")
    ap.add_argument("--wf-test", default="10", help="OOS 窗，与 --wf-train 配对，如 10,20")
    ap.add_argument("--purge", type=int, default=0, help="IS 尾部 purge 行数（剔除标签跨入OOS的样本）")
    ap.add_argument("--embargo", type=int, default=0, help="IS/OOS 间 embargo 禁送行数")
    ap.add_argument("--participation", type=float, default=DEFAULT_PARTICIPATION_CAP)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--json-out", default=DEFAULT_JSON, dest="json_out")
    args = ap.parse_args(argv)

    codes = _all_codes() if args.all else [c.strip().upper() for c in args.codes.split(",") if c.strip()]
    # 多周期：--wf-train/--wf-test 逗号列表按位置配对；长度不齐时短的循环取最后一个
    tr_list = [int(x) for x in str(args.wf_train).split(",") if x.strip()]
    te_list = [int(x) for x in str(args.wf_test).split(",") if x.strip()]
    n_pre = max(len(tr_list), len(te_list), 1)
    presets = [(tr_list[min(k, len(tr_list) - 1)], te_list[min(k, len(te_list) - 1)]) for k in range(n_pre)]
    primary_train, primary_test = presets[0]
    results, errors = [], []
    for sym in codes:
        try:
            res = run_symbol(sym, period=args.period, wf_train=primary_train, wf_test=primary_test,
                             participation_cap=args.participation, purge=args.purge, embargo=args.embargo,
                             wf_presets=presets)
            results.append(res)
            print("  %s 完成：评级%s 最优%s" % (sym, res["stability"]["grade"], res["best_param"]))
        except Exception as e:
            errors.append("%s: %s" % (sym, e))
            print("  %s 跳过：%s" % (sym, e))
    report = build_report(results, codes, args.period)
    if errors:
        report += "\n跳过品种：" + "；".join(errors) + "\n"
    if args.out:
        od = os.path.dirname(os.path.abspath(args.out))
        if od and not os.path.isdir(od):
            os.makedirs(od, exist_ok=True)
        with io.open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(report)
    if args.json_out:
        with io.open(args.json_out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(build_json_payload(results), f, ensure_ascii=False, indent=1, allow_nan=False)
    # G27① 统一实验台账（惰性导入、旁路失败不影响本工具）
    try:
        import experiment_ledger as el
        ov = [{"sym": r["sym"], "grade": r["stability"]["grade"],
               "oos_sharpe": r["stability"]["mean_oos_sharpe"],
               "base_compound": r["base_cell"]["total_compound"],
               "fee_safety_x": r["breakeven"]["fee"]["safety_x"],
               "slip_safety_x": r["breakeven"]["slip"]["safety_x"]} for r in results]
        el.safe_record(
            "wf_cost_lab",
            {"codes": codes, "period": args.period, "wf_presets": presets,
             "purge": args.purge, "embargo": args.embargo, "participation": args.participation,
             "fee_grid": list(DEFAULT_FEE_GRID), "slip_grid": list(DEFAULT_SLIP_GRID)},
            {"n_ok": len(results), "n_skip": len(errors), "symbols": ov},
            inputs=[], artifacts=[p for p in (args.out, args.json_out) if p],
            conclusion="%d品种 WF稳定评级+成本曲面：%s" %
                       (len(results), "、".join("%s=%s" % (r["sym"], r["stability"]["grade"]) for r in results)))
    except Exception:
        pass
    print(report[:2500])
    return 0


# =========================== 零网络/零DB 合成断言 ===========================
def selftest():
    # 1) 统计与复利
    assert abs(per_trade_sharpe([1, -1, 1, -1])) < 1e-9
    assert per_trade_sharpe([1]) == 0.0
    assert abs(compound([0.1, -0.1]) - (1.1 * 0.9 - 1)) < 1e-12
    # 2) summarize_trades：手算
    tr = [{"net": 0.1, "gross": 0.12}, {"net": -0.05, "gross": -0.03}, {"net": 0.02, "gross": 0.04}]
    sm = summarize_trades(tr)
    assert sm["n_trades"] == 3 and abs(sm["win_rate"] - 2 / 3) < 1e-12
    assert abs(sm["sum_gross"] - 0.13) < 1e-12 and abs(sm["sum_cost"] - (0.13 - 0.07)) < 1e-12
    assert summarize_trades([])["n_trades"] == 0 and summarize_trades([])["win_rate"] is None
    # 3) 成本曲面：假 runner，成本越高复利越低
    def fake_runner(fee, slip):
        # 毛收益每笔 +0.002，成本=2*fee+2*slip（开平各一次），共100笔
        r = 0.002 - 2 * fee - 2 * slip
        return [{"net": r, "gross": 0.002} for _ in range(100)]
    surf = build_cost_surface(fake_runner, (0.0, 5e-5, 1e-3), (0.0, 1e-4, 1e-3))
    mat = surface_matrix(surf)
    assert mat[0][0] > mat[-1][-1]                  # 零成本最好、高成本最差
    assert surf["base"]["fi"] == 1 and abs(surf["fee_grid"][1] - 5e-5) < 1e-15  # 基准定位
    # 单调性：沿 fee 递增复利递减
    col = [mat[i][surf["base"]["si"]] for i in range(3)]
    assert col[0] >= col[1] >= col[2]
    # 4) breakeven：构造一个在 fee=1e-3 转负的情形
    be = breakeven_cost(surf)
    assert be["fee"]["first_negative"] is not None and be["fee"]["safety_x"]
    # 全程为正 → first_negative=None/safety None
    def winner(fee, slip):
        return [{"net": 0.01, "gross": 0.012} for _ in range(20)]
    surf2 = build_cost_surface(winner, (0.0, 1e-4), (0.0, 1e-4))
    assert breakeven_cost(surf2)["fee"]["first_negative"] is None
    # 基准已亏 → safety 0
    def loser(fee, slip):
        return [{"net": -0.01, "gross": -0.005} for _ in range(20)]
    surf3 = build_cost_surface(loser, (0.0, 5e-5), (0.0, 1e-4))
    assert breakeven_cost(surf3)["slip"]["safety_x"] == 0.0
    # 5) wf_stability：构造稳定（总选0号）与漂移两种
    segs_stable = [{"chosen": 0, "is_sharpe": 0.5, "oos_sharpe": 0.3, "oos_best": 0.4,
                    "oos_median": 0.1, "beat_median": True} for _ in range(5)]
    names = ["e1/s1/t1", "e2/s2/t2"]
    ws = wf_stability(segs_stable, names)
    assert ws["top_param"] == "e1/s1/t1" and ws["top_share"] == 1.0 and ws["switches"] == 0
    assert ws["grade"] == "稳定" and abs(ws["is_oos_decay"] - (-0.2)) < 1e-12
    assert abs(ws["selection_regret"] - 0.1) < 1e-12 and ws["oos_positive_rate"] == 1.0
    names4 = ["p0", "p1", "p2", "p3"]
    segs_drift = [{"chosen": i % 4, "is_sharpe": 0.2, "oos_sharpe": -0.1, "oos_best": 0.1,
                   "oos_median": 0.0, "beat_median": False} for i in range(10)]
    wd = wf_stability(segs_drift, names4)
    assert wd["switches"] == 9 and wd["grade"] == "漂移" and abs(wd["top_share"] - 0.3) < 1e-9
    assert wf_stability([], names)["grade"] == "样本不足"
    # 越界 chosen 防御
    wx = wf_stability([{"chosen": 9, "is_sharpe": 1, "oos_sharpe": 1, "oos_best": 1,
                        "oos_median": 0, "beat_median": True}], names)
    assert wx["chosen_sequence"] == ["?"]
    # 6) 换手容量：造2天、每天10根、每根100手、价100、乘数10；2笔成交 entry=100
    class _DT:
        def __init__(self, d): self.d = d
        def strftime(self, f): return self.d
    bars = [{"dt": _DT("2026-09-0%d" % (1 if i < 10 else 2)), "c": 100.0, "v": 100.0} for i in range(20)]
    trades = [{"entry_px": 100.0, "multiplier": 10.0}, {"entry_px": 100.0, "multiplier": 10.0}]
    cap = estimate_turnover_capacity(bars, trades, 10.0, participation_cap=0.10, days_year=243)
    assert cap["n_days"] == 2 and cap["n_trades"] == 2 and abs(cap["trades_per_day"] - 1.0) < 1e-9
    # 市场日均名义 = 每天10根×100手×100价×10乘数 = 1,000,000
    assert abs(cap["mkt_daily_notional"] - 1_000_000.0) < 1e-6
    assert abs(cap["notional_per_lot"] - 1000.0) < 1e-9
    # 参与率10%→可投日均名义100000；日均1笔×1000/手 → 100手
    assert abs(cap["max_lots_per_trade"] - 100.0) < 1e-6
    assert estimate_turnover_capacity([], [], 10)["n_days"] == 0
    # 7) combo 名解析
    assert _parse_combo_name("e1.5/s2/t3") == (1.5, 2.0, 3.0)
    # 8) 成稿不抛且含关键段（用合成 res）
    fake_res = {"sym": "RB", "name": "螺纹", "period": 30, "bars": 1000, "n_days": 80,
                "n_combos": 18, "wf_train": 20, "wf_test": 10,
                "stability": ws, "best_param": "e1/s1/t1", "surface": surf,
                "breakeven": be, "base_cell": surf["rows"][1]["cells"][1], "capacity": cap}
    rep = render_symbol(fake_res)
    assert "walk-forward 参数稳定性" in rep and "成本敏感性曲面" in rep and "安全垫" in rep
    full = build_report([fake_res], ["RB"], 30)
    assert "总览" in full and "螺纹" in full
    # json payload allow_nan
    pay = build_json_payload([fake_res]); assert pay["n_symbols"] == 1
    json.dumps(pay, allow_nan=False)
    # 9) 第52轮 G27续：walk_forward purge/embargo 隔离带（惰性 import 真实引擎）+ 多周期 preset 组织
    import backtest_validation as bv
    mat = [[0.01 * (j + 1) + 0.001 * (t % 5) for j in range(3)] for t in range(60)]
    wf0 = bv.walk_forward(mat, 20, 10)                 # 默认无隔离带
    assert wf0["purge"] == 0 and wf0["embargo"] == 0 and wf0["n_segments"] >= 1
    wf_emb = bv.walk_forward(mat, 20, 10, embargo=4)   # OOS 后移4，总样本需求增加，段数不增
    assert wf_emb["embargo"] == 4 and wf_emb["n_segments"] <= wf0["n_segments"]
    wf_purge = bv.walk_forward(mat, 20, 10, purge=4)   # IS 尾部剔4行仍可跑
    assert wf_purge["purge"] == 4 and wf_purge["n_segments"] == wf0["n_segments"]
    assert bv.walk_forward(mat, 3, 10, purge=2)["n_segments"] == 0  # IS-purge<2 安全返空
    # 多周期 preset：模拟 run_symbol 内的组织逻辑（不触 DB），两窗口规格各自出稳定性
    presets = [(20, 10), (40, 20)]
    multi = []
    for tr, te in presets:
        wfx = bv.walk_forward(mat, tr, te, purge=1, embargo=1)
        multi.append(wf_stability(wfx.get("segments", []), ["p0", "p1", "p2"]))
    assert len(multi) == 2 and all("grade" in x for x in multi)
    print("wf_cost_lab selftest OK（9 组）")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 1:
        raise SystemExit(selftest())
    raise SystemExit(run())
