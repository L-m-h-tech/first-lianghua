# -*- coding: utf-8 -*-
r"""G30（第42轮）交易复盘 journal：tools/trade_journal.py，纯标准库、零网络、只读不写主链。

输入 portfolio.py 回测产出的 portfolio_trades.csv（每行=一笔完整开平 round-trip，净盈亏已含
开平费与平今腿），可选叠加 portfolio_equity.csv 与自采分钟库（--bars，盘中 h/l 重放 MFE/MAE），
按【品种/板块/多空/平仓原因组/平今平昨/信号档位/持仓时长档】分桶出 胜率/期望/盈亏比/利润因子/
连胜连亏/费用占比，给出日·周节奏、最佳最差单、规则化观察结论，一键成稿 reports/trade_journal.txt
（+ .json sidecar）。

纪律（照 G21–G30 研究侧惯例）：
- 只读分析+新增报告，不改 main/analyzer/综合分，不改 portfolio 默认 CSV 输出（哈希基线不动）；
- 分钟回测成交是 bar 内规则假设（开盘成交/触及止损止盈/日终强平），不是真实队列（总纲不做清单1），
  报告固定声明；纸面 vs 回测真实成交一致性（G30②，需 G14 盘口）本轮不做、留续；
- 空文件/0 笔/缺 equity/缺分钟库 全部安全降级，绝不抛错。
"""
import argparse
import bisect
import csv
import datetime as _dt
import io
import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import config                                   # noqa: E402
import metrics                                  # noqa: E402
import experiment_ledger as el                  # noqa: E402  G27① 统一实验台账（旁路登记）

DEFAULT_TRADES = os.path.join(_ROOT, "reports", "portfolio_trades.csv")
DEFAULT_EQUITY = os.path.join(_ROOT, "reports", "portfolio_equity.csv")
DEFAULT_OUT = os.path.join(_ROOT, "reports", "trade_journal.txt")
DEFAULT_JSON = os.path.join(_ROOT, "reports", "trade_journal.json")

# =========================== 解析 ===========================
def parse_dt(s):
    """宽容解析 'YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DD HH:MM' / 'YYYY-MM-DD'；失败返回 None。"""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _f(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _i(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def load_trades(path=DEFAULT_TRADES):
    """读 portfolio_trades.csv -> 升序（按 exit_dt，其次 entry_dt）dict 列表；缺文件返回 []。"""
    if not path or not os.path.exists(path):
        return []
    out = []
    with io.open(path, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            t = {
                "sym": (r.get("sym") or "").strip(),
                "name": (r.get("name") or "").strip(),
                "sector": (r.get("sector") or "未分类").strip() or "未分类",
                "dir": (r.get("dir") or "").strip(),
                "lots": _i(r.get("lots")),
                "entry_dt": parse_dt(r.get("entry_dt")),
                "exit_dt": parse_dt(r.get("exit_dt")),
                "entry_px": _f(r.get("entry_px")),
                "exit_px": _f(r.get("exit_px")),
                "leg": (r.get("leg") or "").strip(),
                "hold_bars": _i(r.get("hold_bars")),
                "gross_yuan": _f(r.get("gross_yuan")),
                "open_fee_yuan": _f(r.get("open_fee_yuan")),
                "close_fee_yuan": _f(r.get("close_fee_yuan")),
                "net_yuan": _f(r.get("net_yuan")),
                "reason": (r.get("reason") or "").strip(),
                "forced": str(r.get("forced") or "").strip().lower() == "true",
                "entry_score": _f(r.get("entry_score"), None) if r.get("entry_score") not in (None, "") else None,
                "margin_rate": _f(r.get("margin_rate"), None) if r.get("margin_rate") not in (None, "") else None,
            }
            t["direction"] = 1 if t["dir"] == "多" else (-1 if t["dir"] == "空" else 0)
            t["fee_yuan"] = t["open_fee_yuan"] + t["close_fee_yuan"]
            out.append(t)
    out.sort(key=lambda t: ((t["exit_dt"] or _dt.datetime.min), (t["entry_dt"] or _dt.datetime.min)))
    return out


def load_equity(path=DEFAULT_EQUITY):
    """读 portfolio_equity.csv -> [{dt,equity,drawdown,npos,...}]；缺文件返回 []。"""
    if not path or not os.path.exists(path):
        return []
    out = []
    with io.open(path, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            out.append({"dt": parse_dt(r.get("dt")),
                        "equity": _f(r.get("equity")), "static": _f(r.get("static")),
                        "float": _f(r.get("float")), "risk": _f(r.get("risk")),
                        "drawdown": _f(r.get("drawdown")), "npos": _i(r.get("npos"))})
    return out


# =========================== 分桶（纯统计，可手算断言） ===========================
def reason_group(reason):
    """平仓原因归并为四组+其他。"""
    r = reason or ""
    if r.startswith("止盈"):
        return "止盈"
    if r.startswith("止损"):
        return "止损"
    if ("强平" in r) or ("清仓" in r):
        return "日终/样本强平"
    if "反向" in r:
        return "反向信号"
    return r or "其他"


def score_band(score):
    """信号【强度】档位（按|分|，空头入场分为负，方向已由独立分桶承载），
    阈值沿用综合分口径 config.SCORE_NEUTRAL/LIGHT/MID。"""
    if score is None:
        return "无分"
    a = abs(score)
    if a < config.SCORE_NEUTRAL:
        return "弱(|分|<%.0f)" % config.SCORE_NEUTRAL
    if a < config.SCORE_LIGHT:
        return "轻仓[%.0f,%.0f)" % (config.SCORE_NEUTRAL, config.SCORE_LIGHT)
    if a < config.SCORE_MID:
        return "分批[%.0f,%.0f)" % (config.SCORE_LIGHT, config.SCORE_MID)
    return "强信号(>=%.0f)" % config.SCORE_MID


def hold_band(bars):
    """持仓时长档（按回测 bar 数；分钟模式下 1 bar=1 个周期）。"""
    b = bars or 0
    if b <= 2:
        return "1极短(1-2)"
    if b <= 6:
        return "2短(3-6)"
    if b <= 12:
        return "3中(7-12)"
    return "4长(13+)"


def day_key(t):
    d = t.get("exit_dt")
    return d.strftime("%Y-%m-%d") if d else "未知日"


def week_key(t):
    d = t.get("exit_dt")
    if not d:
        return "未知周"
    iso = d.isocalendar()
    return "%04d-W%02d" % (iso[0], iso[1])


def bucket(trades, key_fn):
    """按 key_fn 分桶 -> {key: [trades]}，保持首次出现顺序。"""
    groups = {}
    for t in trades:
        k = key_fn(t)
        groups.setdefault(k, []).append(t)
    return groups


def bucket_table(trades, key_fn, min_n=1):
    """每桶复用 metrics.trade_stats，并补 总费用/均持仓/总净/毛盈亏，返回行 dict 列表（按总净升序最差在前）。"""
    rows = []
    for k, ts in bucket(trades, key_fn).items():
        st = metrics.trade_stats([t["net_yuan"] for t in ts])
        if st is None:
            continue
        fees = sum(t["fee_yuan"] for t in ts)
        gross = sum(t["gross_yuan"] for t in ts)
        net = sum(t["net_yuan"] for t in ts)
        holds = [t["hold_bars"] for t in ts if t["hold_bars"] > 0]
        rows.append({
            "key": k, "n": st["n"], "win_rate": st["win_rate"],
          "net": net, "avg_net": st["avg_pnl"], "payoff": st["payoff_ratio"],
            "pf": st["profit_factor"], "fees": fees, "gross": gross,
            "fee_over_gross": (fees / abs(gross)) if abs(gross) > 1e-9 else None,
            "avg_hold": (sum(holds) / len(holds)) if holds else 0.0,
            "max_ws": st["max_win_streak"], "max_ls": st["max_loss_streak"],
        })
    rows.sort(key=lambda r: (r["net"], -r["n"]))
    return [r for r in rows if r["n"] >= min_n]


def period_pnl(trades, key_fn):
    """日/周盈亏节奏：按期聚合 笔数/胜数/净盈亏/毛盈亏/费用。"""
    rows = []
    for k, ts in bucket(trades, key_fn).items():
        wins = sum(1 for t in ts if t["net_yuan"] > 0)
        rows.append({"key": k, "n": len(ts), "win": wins,
                     "net": sum(t["net_yuan"] for t in ts),
                     "gross": sum(t["gross_yuan"] for t in ts),
                     "fees": sum(t["fee_yuan"] for t in ts)})
    rows.sort(key=lambda r: r["key"])
    return rows


def cumulative_curve(trades):
    """按平仓顺序的累计净盈亏曲线 [(exit_dt, cum)]。"""
    cum = 0.0
    out = []
    for t in trades:
        cum += t["net_yuan"]
        out.append((t["exit_dt"], cum))
    return out


def equity_drawdown_days(equity):
    """从 equity 曲线抽取每日末权益与当日最大回撤，供日复盘对照；空返回 []。"""
    by_day = {}
    for r in equity:
        if not r["dt"]:
            continue
        k = r["dt"].strftime("%Y-%m-%d")
        cur = by_day.get(k)
        if cur is None or r["dt"] >= cur["dt"]:
            by_day[k] = {"dt": r["dt"], "equity": r["equity"], "dd": r["drawdown"]}
    return [by_day[k] for k in sorted(by_day)]


# =========================== MFE/MAE 盘中重放（可选，--bars） ===========================
def load_minute_bars_for(sym, period, lookback, aggregate_from):
    """与 portfolio.load_minute_feed 同口径装载+比例复权，返回升序 bars；失败返回 []。"""
    try:
        import storage
        import intraday_backtest as ib
        from backtest import ratio_adjusted_bars
        db = storage.MonitorDB()
        try:
            raw, _src = ib.load_minute_bars(db, sym, int(period), int(lookback), int(aggregate_from or 0))
        finally:
            db.close()
        bars, _roll = ratio_adjusted_bars(raw)
        return bars
    except Exception:
        return []


def range_excursion(direction, entry_px, bars, t0, t1):
    """用区间内每根 bar 的盘中 h/l 算 MFE/MAE（正小数）。多: MFE看h、MAE看l；空反之。
    闭区间 [t0,t1]（含入场根全根极值，属保守上界，报告标注）；返回 (mfe,mae,used_bars)。"""
    if not bars or entry_px is None or entry_px <= 0 or t0 is None:
        return None, None, 0
    dts = [b["dt"] for b in bars]
    lo = bisect.bisect_left(dts, t0)
    hi = bisect.bisect_right(dts, t1 or t0)
    mfe = mae = 0.0
    used = 0
    for b in bars[lo:hi]:
        h, l = _f(b.get("h"), None), _f(b.get("l"), None)
        if h is None or l is None or h <= 0 or l <= 0:
            continue
        if direction > 0:
            mfe = max(mfe, (h - entry_px) / entry_px)
            mae = max(mae, (entry_px - l) / entry_px)
        elif direction < 0:
            mfe = max(mfe, (entry_px - l) / entry_px)
            mae = max(mae, (h - entry_px) / entry_px)
        else:
            continue
        used += 1
    return (mfe if used else None), (mae if used else None), used


def attach_excursions(trades, *, period=30, lookback=8000, aggregate_from=0, verbose=False):
    """就地给每笔 trade 写 mfe_bar/mae_bar/xcov_n；按品种只装载一次分钟库。
    返回覆盖率 meta。找不到库/区间的笔保持 None（安全降级）。"""
    cache = {}
    ok = miss = 0
    for t in trades:
        sym = t["sym"]
        if not sym or t["direction"] == 0:
            miss += 1
            continue
        if sym not in cache:
            cache[sym] = load_minute_bars_for(sym, period, lookback, aggregate_from)
        bars = cache[sym]
        mfe, mae, used = range_excursion(t["direction"], t["entry_px"], bars,
                                         t["entry_dt"], t["exit_dt"])
        t["mfe_bar"] = mfe
        t["mae_bar"] = mae
        t["xcov_n"] = used
        if mfe is not None and used > 0:
            ok += 1
        else:
            miss += 1
    total = len(trades)
    return {"with_excursion": ok, "missing": miss, "total": total,
            "coverage": (ok / total) if total else 0.0,
            "period": period, "loaded_syms": sum(1 for b in cache.values() if b)}


def excursion_summary(trades):
    """汇总盘中 MFE/MAE（全体/盈利单/亏损单），并给'曾经浮盈'线索；无数据返回 None。"""
    def _avg(xs):
        xs = [x for x in xs if x is not None and math.isfinite(x)]
        return sum(xs) / len(xs) if xs else None
    win = [t for t in trades if t.get("mfe_bar") is not None and t["net_yuan"] > 0]
    loss = [t for t in trades if t.get("mfe_bar") is not None and t["net_yuan"] < 0]
    allx = [t for t in trades if t.get("mfe_bar") is not None]
    if not allx:
        return None
    # 亏损单曾经的平均浮盈（>0 说明'由盈转亏/扛单回吐'），盈利单曾经的平均浮亏（回撤后仍赚）
    loss_mfe = _avg([t["mfe_bar"] for t in loss])
    win_mae = _avg([t["mae_bar"] for t in win])
    return {
        "n": len(allx),
        "avg_mfe": _avg([t["mfe_bar"] for t in allx]),
        "avg_mae": _avg([t["mae_bar"] for t in allx]),
        "avg_mfe_win": _avg([t["mfe_bar"] for t in win]),
        "avg_mae_win": win_mae,
        "avg_mfe_loss": loss_mfe,
        "avg_mae_loss": _avg([t["mae_bar"] for t in loss]),
        "loss_once_green": sum(1 for t in loss if (t.get("mfe_bar") or 0.0) > 0.001),
        "n_loss": len(loss),
    }


# =========================== 报告 ===========================
def _money(x):
    return "{:+,.0f}".format(x) if x is not None else "—"


def _pct(x, d=1):
    return ("%." + str(d) + "f%%") % (x * 100) if x is not None else "—"


def _f2(x, d=2):
    return ("%." + str(d) + "f") % x if x is not None else "—"


def _bucket_lines(title, rows, key_w=16):
    lines = ["【%s】" % title,
             "  %-*s %5s %7s %12s %10s %8s %8s %9s %8s" %
             (key_w, "桶", "笔数", "胜率", "净盈亏", "均笔", "盈亏比", "PF", "费用", "均持bar")]
    for r in rows:
        lines.append("  %-*s %5d %7s %12s %10s %8s %8s %9s %8.1f" %
                     (key_w, str(r["key"])[:key_w], r["n"], _pct(r["win_rate"]),
                      _money(r["net"]), _money(r["avg_net"]), _f2(r["payoff"]),
                      _f2(r["pf"]), _money(r["fees"]), r["avg_hold"]))
    return lines


def observations(trades, overall, exsum):
    """确定性规则化观察（只陈述数据事实，不做投资建议外推）。"""
    obs = []
    n = overall["n"] if overall else 0
    if n == 0:
        return ["  · 无成交，跳过观察。"]
    # 1) 期望分解
    wr, aw, al = overall["win_rate"], overall["avg_win"], overall["avg_loss"]
    if aw is not None and al is not None:
        e_win, e_loss = wr * aw, (1 - wr) * al
        obs.append("  · 期望拆解：赢面贡献 %s/笔，亏面拖累 %s/笔，净期望 %s/笔（胜率%s×均盈%s / 败率%s×均亏%s）。" %
                   (_money(e_win), _money(e_loss), _money(overall["expectancy"]),
                    _pct(wr), _money(aw), _pct(1 - wr), _money(al)))
    # 2) 分桶里 n>=10 且 PF<0.7 的弱势桶（不含原因组——止盈/止损按定义即全赢/全亏，PF无信息量）
    for title, kf in (("品种", lambda t: t["sym"]), ("板块", lambda t: t["sector"]),
                      ("方向", lambda t: t["dir"]),
                      ("信号强度档", lambda t: score_band(t["entry_score"])),
                      ("持仓档", lambda t: hold_band(t["hold_bars"]))):
        for r in bucket_table(trades, kf):
            if r["n"] >= 10 and r["pf"] is not None and r["pf"] < 0.7:
                obs.append("  · 弱势桶提示：%s=%s 共%d笔 PF=%s 净%s，成本后期望为负，优先复盘该桶。" %
                           (title, r["key"], r["n"], _f2(r["pf"]), _money(r["net"])))
    # 3) 费用
    fees = sum(t["fee_yuan"] for t in trades)
    gross_abs = abs(sum(t["gross_yuan"] for t in trades))
    if gross_abs > 1e-9:
        obs.append("  · 总费用 %s，占|毛盈亏| 的 %s（平今腿拉高成本，留意高频小盈被费用吃掉）。" %
                   (_money(fees), _pct(fees / gross_abs)))
    # 4) 强平占比
    rg = bucket_table(trades, lambda t: reason_group(t["reason"]))
    for r in rg:
        if "强平" in r["key"]:
            obs.append("  · %s %d笔（%s）净%s：日终不留仓是纪律成本，观察其是否系统性割在不利位置。" %
                       (r["key"], r["n"], _pct(r["n"] / n), _money(r["net"])))
    # 5) MFE/MAE 由盈转亏
    if exsum and exsum["n_loss"] > 0:
        obs.append("  · 盘中MFE/MAE：亏损单%d笔中有%d笔（%s）盘中曾浮盈>0.1%%仍以亏损平仓（由盈转亏/回吐线索）；"
                   "盈利单平均盘中浮亏 %s（能扛住的正常回撤）。" %
                   (exsum["n_loss"], exsum["loss_once_green"],
                    _pct(exsum["loss_once_green"] / exsum["n_loss"] if exsum["n_loss"] else 0),
                    _pct(exsum["avg_mae_win"])))
    if not obs:
        obs.append("  · 未触发规则化异常提示。")
    return obs


def build_report(trades, *, equity=None, exmeta=None, exsum=None,
                 trades_path="", equity_path="", review="both", bar_period=30):
    L = []
    L.append("交易复盘 Journal（G30，第42轮 tools/trade_journal.py，纯标准库只读）")
    L.append("生成时间：%s" % _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    L.append("输入：成交=%s（%d笔）；权益=%s（%d点）；盘中MFE/MAE=%s" %
             (trades_path or "默认", len(trades), equity_path or "无",
              len(equity or []), ("开启%dm重放 覆盖率%s" % (bar_period, _pct(exmeta["coverage"]))
                                  if exmeta else "关闭（加 --bars 开启）")))
    L.append("口径声明：分钟回测成交为 bar 内规则假设（开盘成交/触及止损止盈/日终强平），非真实盘口队列；"
             "净盈亏已含开/平费与平今腿；纸面vs真实成交一致性（G30②，需G14盘口）本轮未做。")
    L.append("=" * 96)
    if not trades:
        L.append("无成交记录（文件缺失或0笔），安全降级：不出分桶。请先运行 portfolio.py 生成 portfolio_trades.csv。")
        return "\n".join(L) + "\n"

    overall = metrics.trade_stats([t["net_yuan"] for t in trades])
    fees_all = sum(t["fee_yuan"] for t in trades)
    gross_all = sum(t["gross_yuan"] for t in trades)
    t0 = min((t["entry_dt"] for t in trades if t["entry_dt"]), default=None)
    t1 = max((t["exit_dt"] for t in trades if t["exit_dt"]), default=None)
    holds = [t["hold_bars"] for t in trades if t["hold_bars"] > 0]
    L.append("一、总览（区间 %s ~ %s）" %
             (t0.strftime("%Y-%m-%d %H:%M") if t0 else "—",
              t1.strftime("%Y-%m-%d %H:%M") if t1 else "—"))
    L.append("  总笔数 %d（多 %d / 空 %d / 强平 %d）；胜率 %s；期望 %s/笔；盈亏比 %s；利润因子PF %s" %
             (overall["n"], sum(1 for t in trades if t["dir"] == "多"),
              sum(1 for t in trades if t["dir"] == "空"),
              sum(1 for t in trades if t["forced"]),
              _pct(overall["win_rate"]), _money(overall["expectancy"]),
              _f2(overall["payoff_ratio"]), _f2(overall["profit_factor"])))
    L.append("  净盈亏合计 %s（毛 %s）；总费用 %s（占|毛| %s）；均持仓 %.1f bar；最长连胜 %d / 最长连亏 %d；最佳 %s / 最差 %s" %
             (_money(sum(t["net_yuan"] for t in trades)), _money(gross_all), _money(fees_all),
              _pct(fees_all / abs(gross_all)) if abs(gross_all) > 1e-9 else "—",
              sum(holds) / len(holds) if holds else 0.0,
              overall["max_win_streak"], overall["max_loss_streak"],
              _money(overall["best"]), _money(overall["worst"])))
    if equity:
        eqs = [r["equity"] for r in equity if r["equity"] > 0]
        if eqs:
            peak = max(eqs)
            dd = max((r["drawdown"] for r in equity), default=0.0)
            L.append("  权益曲线：期初 {:,.0f} → 期末 {:,.0f}；区间峰值 {:,.0f}；最大回撤 {}。".format(
                     eqs[0], eqs[-1], peak, _pct(dd)))
    L.append("=" * 96)
    L.append("二、分桶复盘（按净盈亏升序，最差在前）")
    L += _bucket_lines("品种 sym", bucket_table(trades, lambda t: t["sym"]))
    L += _bucket_lines("板块 sector", bucket_table(trades, lambda t: t["sector"]))
    L += _bucket_lines("方向", bucket_table(trades, lambda t: t["dir"]), key_w=10)
    L += _bucket_lines("平仓原因组", bucket_table(trades, lambda t: reason_group(t["reason"])), key_w=18)
    L += _bucket_lines("平今/平昨", bucket_table(trades, lambda t: t["leg"] or "未知"), key_w=10)
    L += _bucket_lines("信号强度(|入场分|,多空同档)", bucket_table(trades, lambda t: score_band(t["entry_score"])), key_w=22)
    L += _bucket_lines("持仓时长档(bar)", bucket_table(trades, lambda t: hold_band(t["hold_bars"])), key_w=14)
    L.append("=" * 96)

    if review in ("daily", "both"):
        L.append("三、日节奏（按平仓日）")
        rows = period_pnl(trades, day_key)
        L.append("  %-12s %5s %6s %12s %12s %10s" % ("日", "笔数", "胜数", "净盈亏", "毛盈亏", "费用"))
        for r in rows:
            L.append("  %-12s %5d %6d %12s %12s %10s" %
                     (r["key"], r["n"], r["win"], _money(r["net"]), _money(r["gross"]), _money(r["fees"])))
        win_days = sum(1 for r in rows if r["net"] > 0)
        L.append("  合计 %d 个交易日，盈利日 %d（%s），最佳日 %s / 最差日 %s。" %
                 (len(rows), win_days, _pct(win_days / len(rows)) if rows else "—",
                 _money(max((r["net"] for r in rows), default=0)),
                 _money(min((r["net"] for r in rows), default=0))))
        if review == "both":
            L.append("-" * 96)
    if review in ("weekly", "both"):
        L.append("四、周节奏（ISO周，按平仓日归属）")
        rows = period_pnl(trades, week_key)
        L.append("  %-10s %5s %6s %12s %12s %10s" % ("周", "笔数", "胜数", "净盈亏", "毛盈亏", "费用"))
        for r in rows:
            L.append("  %-10s %5d %6d %12s %12s %10s" %
                     (r["key"], r["n"], r["win"], _money(r["net"]), _money(r["gross"]), _money(r["fees"])))
        win_w = sum(1 for r in rows if r["net"] > 0)
        L.append("  合计 %d 周，盈利周 %d（%s）。" %
                 (len(rows), win_w, _pct(win_w / len(rows)) if rows else "—"))
        L.append("=" * 96)

    if exsum:
        L.append("五、盘中 MFE/MAE（h/l 重放，正小数；覆盖率 %d/%d=%s，含入场根全根极值为保守上界）" %
                 (exsum["n"], len(trades), _pct(exsum["n"] / len(trades))))
        L.append("  全体：平均MFE %s / 平均MAE %s（比值%s，>1 说明持仓体验顺）。" %
                 (_pct(exsum["avg_mfe"], 2), _pct(exsum["avg_mae"], 2),
                  _f2(exsum["avg_mfe"] / exsum["avg_mae"], 2) if exsum["avg_mae"] else "—"))
        L.append("  盈利单：平均MFE %s / 平均MAE %s；亏损单：平均MFE %s / 平均MAE %s。" %
                 (_pct(exsum["avg_mfe_win"], 2), _pct(exsum["avg_mae_win"], 2),
                  _pct(exsum["avg_mfe_loss"], 2), _pct(exsum["avg_mae_loss"], 2)))
        L.append("=" * 96)

    L.append("六、最佳/最差 5 单")
    ordered = sorted(trades, key=lambda t: t["net_yuan"])
    L.append("  最差5单：")
    for t in ordered[:5]:
        L.append(_trade_line(t))
    L.append("  最佳5单：")
    for t in ordered[-5:][::-1]:
        L.append(_trade_line(t))
    L.append("=" * 96)
    L.append("七、规则化观察（确定性、只陈述本批数据事实）")
    L += observations(trades, overall, exsum)
    return "\n".join(L) + "\n"


def _trade_line(t):
    return ("  %-4s %-2s %2d手 %s→%s 持%3dbar 净%10s [%s|入场分%s|%s]" %
            (t["sym"], t["dir"], t["lots"],
             t["entry_dt"].strftime("%m-%d %H:%M") if t["entry_dt"] else "—",
             t["exit_dt"].strftime("%m-%d %H:%M") if t["exit_dt"] else "—",
             t["hold_bars"], _money(t["net_yuan"]), t["reason"],
             _f2(t["entry_score"], 2) if t["entry_score"] is not None else "—", t["leg"]))


def _json_safe(o):
    if isinstance(o, dict):
        return {str(k): _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    if isinstance(o, (_dt.datetime, _dt.date)):
        return o.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    return o


def build_json_payload(trades, equity, exmeta, exsum, review):
    overall = metrics.trade_stats([t["net_yuan"] for t in trades]) if trades else None
    payload = {
        "n_trades": len(trades), "overall": overall,
        "by_sym": bucket_table(trades, lambda t: t["sym"]),
        "by_sector": bucket_table(trades, lambda t: t["sector"]),
        "by_dir": bucket_table(trades, lambda t: t["dir"]),
        "by_reason": bucket_table(trades, lambda t: reason_group(t["reason"])),
        "by_leg": bucket_table(trades, lambda t: t["leg"] or "未知"),
        "by_score_band": bucket_table(trades, lambda t: score_band(t["entry_score"])),
        "by_hold_band": bucket_table(trades, lambda t: hold_band(t["hold_bars"])),
        "daily": period_pnl(trades, day_key) if review in ("daily", "both") else [],
        "weekly": period_pnl(trades, week_key) if review in ("weekly", "both") else [],
        "excursion_meta": exmeta, "excursion": exsum,
    }
    return _json_safe(payload)


# =========================== CLI ===========================
def run(argv=None):
    ap = argparse.ArgumentParser(description="G30 交易复盘 journal（只读 portfolio_trades.csv）")
    ap.add_argument("--trades", default=DEFAULT_TRADES, help="portfolio_trades.csv 路径")
    ap.add_argument("--equity", default=DEFAULT_EQUITY, help="portfolio_equity.csv 路径（可选，传空字符串关闭）")
    ap.add_argument("--bars", action="store_true", help="从自采分钟库按 h/l 重放盘中 MFE/MAE")
    ap.add_argument("--period", type=int, default=30, help="分钟周期，须与回测一致（默认30）")
    ap.add_argument("--lookback", type=int, default=8000, help="每品种装载 bar 上限")
    ap.add_argument("--aggregate-from", type=int, default=0, dest="aggregate_from")
    ap.add_argument("--review", choices=("none", "daily", "weekly", "both"), default="both")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--json-out", default=DEFAULT_JSON, dest="json_out")
    args = ap.parse_args(argv)

    trades = load_trades(args.trades)
    equity = load_equity(args.equity) if args.equity else []
    exmeta = exsum = None
    if args.bars and trades:
        exmeta = attach_excursions(trades, period=args.period, lookback=args.lookback,
                                   aggregate_from=args.aggregate_from)
        exsum = excursion_summary(trades)
    report = build_report(trades, equity=equity, exmeta=exmeta, exsum=exsum,
                          trades_path=args.trades, equity_path=args.equity,
                          review=args.review, bar_period=args.period)
    if args.out:
        od = os.path.dirname(os.path.abspath(args.out))
        if od and not os.path.isdir(od):
            os.makedirs(od, exist_ok=True)
        with io.open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(report)
    payload = build_json_payload(trades, equity, exmeta, exsum, args.review)
    if args.json_out:
        with io.open(args.json_out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1, allow_nan=False)
    # G27① 统一实验台账（旁路：登记失败绝不影响本工具产物）
    try:
        ov = payload.get("overall") or {}
        ex = payload.get("excursion") or {}
        j_metrics = {"n_trades": payload.get("n_trades", 0),
                     "win_rate": ov.get("win_rate"), "profit_factor": ov.get("profit_factor"),
                     "payoff_ratio": ov.get("payoff_ratio"), "expectancy": ov.get("expectancy"),
                     "max_loss_streak": ov.get("max_loss_streak")}
        if ex:
            j_metrics["loss_once_green"] = ex.get("loss_once_green")
            j_metrics["n_loss"] = ex.get("n_loss")
        el.safe_record(
            "trade_journal",
            {"bars": bool(args.bars), "period": args.period, "review": args.review,
             "lookback": args.lookback, "aggregate_from": args.aggregate_from,
             "trades": os.path.basename(args.trades)},
            j_metrics,
            inputs=[p for p in (args.trades, args.equity) if p],
            artifacts=[p for p in (args.out, args.json_out) if p],
            conclusion="%d笔 胜率%s PF%s%s"
                       % (payload.get("n_trades", 0), ov.get("win_rate", "—"),
                          ov.get("profit_factor", "—"),
                          " 含盘中MFE/MAE" if args.bars else ""))
    except Exception:
        pass
    print(report)
    return 0


# =========================== 零网络/零DB 合成断言 ===========================
def _mk(sym, sector, d, net, gross=None, *, reason="日终强平", leg="平今",
        score=3.0, hold=6, fees=10.0, entry="2026-01-02 10:00:00", exit_="2026-01-02 15:00:00",
        entry_px=100.0):
    return {"sym": sym, "name": sym, "sector": sector, "dir": d, "lots": 1,
            "entry_dt": parse_dt(entry), "exit_dt": parse_dt(exit_),
            "entry_px": entry_px, "exit_px": entry_px, "leg": leg, "hold_bars": hold,
            "gross_yuan": net + fees if gross is None else gross,
            "open_fee_yuan": fees / 2, "close_fee_yuan": fees / 2,
            "fee_yuan": fees, "net_yuan": net, "reason": reason, "forced": "强平" in reason,
            "entry_score": score, "margin_rate": 0.1,
            "direction": 1 if d == "多" else -1}


def selftest():
    # 1) 解析/空安全
    assert load_trades("___no_such_file__.csv") == []
    assert build_report([]).startswith("交易复盘 Journal")
    assert metrics.trade_stats([]) is None
    assert period_pnl([], day_key) == []

    # 2) 手算 6 笔：黑色 RB 三笔（+100,-50,-50），化工 MA 两笔（+200,+0? 用+200,-20），有色 CU 一笔 -80
    ts = [
        _mk("RB", "黑色", "多", 100, reason="止盈", score=5.0, hold=4, exit_="2026-01-02 15:00"),
        _mk("RB", "黑色", "空", -50, reason="止损", score=3.0, hold=8, exit_="2026-01-05 15:00"),
        _mk("RB", "黑色", "多", -50, reason="反向信号", score=2.5, hold=20, exit_="2026-01-06 15:00"),
        _mk("MA", "能源化工", "多", 200, reason="止盈(跳空)", score=7.0, hold=3, exit_="2026-01-07 15:00"),
        _mk("MA", "能源化工", "空", -20, reason="日终强平", score=3.5, hold=6, exit_="2026-01-08 15:00"),
        _mk("CU", "有色", "多", -80, reason="止损(跳空)", score=4.5, hold=1, exit_="2026-01-09 15:00"),
    ]
    nets = [t["net_yuan"] for t in ts]
    assert sum(nets) == 100, sum(nets)
    st = metrics.trade_stats(nets)
    assert st["n"] == 6 and st["n_win"] == 2 and st["n_loss"] == 4
    assert abs(st["win_rate"] - 2 / 6) < 1e-12
    assert abs(st["best"] - 200) < 1e-9 and abs(st["worst"] + 80) < 1e-9
    assert abs(st["gross_profit"] - 300) < 1e-9 and abs(st["gross_loss"] + 200) < 1e-9
    assert abs(st["profit_factor"] - 1.5) < 1e-12
    # 连胜连亏：+,-,-,+,-,- → 最长连胜1、最长连亏2
    assert st["max_win_streak"] == 1 and st["max_loss_streak"] == 2

    # 3) 分桶
    by_sym = {r["key"]: r for r in bucket_table(ts, lambda t: t["sym"])}
    assert by_sym["RB"]["n"] == 3 and abs(by_sym["RB"]["net"] - 0.0) < 1e-9
    assert by_sym["MA"]["n"] == 2 and abs(by_sym["MA"]["net"] - 180) < 1e-9
    assert by_sym["CU"]["n"] == 1 and abs(by_sym["CU"]["net"] + 80) < 1e-9
    by_reason = {r["key"]: r for r in bucket_table(ts, lambda t: reason_group(t["reason"]))}
    assert set(by_reason) == {"止盈", "止损", "日终/样本强平", "反向信号"}
    assert by_reason["止盈"]["n"] == 2 and by_reason["止损"]["n"] == 2
    # 档位（按|分|，空头负分与多头同强度档）
    assert score_band(7.0) == "强信号(>=%.0f)" % config.SCORE_MID
    assert score_band(-7.0) == "强信号(>=%.0f)" % config.SCORE_MID
    assert score_band(5.0).startswith("分批")
    assert score_band(3.0).startswith("轻仓")
    assert score_band(-3.87).startswith("轻仓")
    assert score_band(1.0).startswith("弱(")
    assert score_band(None) == "无分"
    # 持仓档
    assert hold_band(1) != hold_band(8) and hold_band(20).startswith("4长")
    # leg 桶
    assert {r["key"] for r in bucket_table(ts, lambda t: t["leg"])} == {"平今"}

    # 4) 日/周键与聚合
    days = period_pnl(ts, day_key)
    assert len(days) == 6 and abs(sum(r["net"] for r in days) - 100) < 1e-9
    wk = week_key(_mk("X", "x", "多", 1, exit_="2026-01-08 15:00"))
    assert wk == "2026-W02", wk
    cum = cumulative_curve(ts)
    assert len(cum) == 6 and abs(cum[-1][1] - 100) < 1e-9

    # 5) 盘中 MFE/MAE 手算（多头 entry=100，区间 h/l 给定）
    bars = [
        {"dt": parse_dt("2026-01-02 09:30:00"), "h": 99.0, "l": 97.0},
        {"dt": parse_dt("2026-01-02 10:00:00"), "h": 103.0, "l": 99.0},
        {"dt": parse_dt("2026-01-02 11:00:00"), "h": 105.0, "l": 101.0},
        {"dt": parse_dt("2026-01-02 15:00:00"), "h": 102.0, "l": 98.0},
    ]
    mfe, mae, used = range_excursion(1, 100.0, bars,
                                     parse_dt("2026-01-02 10:00:00"), parse_dt("2026-01-02 15:00:00"))
    assert used == 3 and abs(mfe - 0.05) < 1e-12 and abs(mae - 0.02) < 1e-12, (mfe, mae, used)
    # 空头镜像：entry=100，区间最低98 → MFE=2%；最高105 → MAE=5%
    mfe2, mae2, used2 = range_excursion(-1, 100.0, bars,
                                        parse_dt("2026-01-02 10:00:00"), parse_dt("2026-01-02 15:00:00"))
    assert used2 == 3 and abs(mfe2 - 0.02) < 1e-12 and abs(mae2 - 0.05) < 1e-12
    # 区间外/空路径安全
    assert range_excursion(1, 100.0, [], None, None) == (None, None, 0)
    mfe3, mae3, u3 = range_excursion(1, 100.0, bars,
                                     parse_dt("2027-01-01 00:00:00"), parse_dt("2027-01-02 00:00:00"))
    assert u3 == 0 and mfe3 is None

    # 6) attach + summary（不给 DB：全部 miss，覆盖率0但不抛错）
    meta = attach_excursions(ts, period=30, lookback=100)
    assert meta["total"] == 6 and meta["with_excursion"] == 0 and meta["coverage"] == 0.0
    assert excursion_summary(ts) is None
    # 手工挂 mfe 验证汇总
    ts2 = [_mk("A", "x", "多", 10, exit_="2026-01-02 15:00"),
           _mk("B", "x", "多", -10, exit_="2026-01-03 15:00")]
    ts2[0]["mfe_bar"], ts2[0]["mae_bar"] = 0.04, 0.01
    ts2[1]["mfe_bar"], ts2[1]["mae_bar"] = 0.02, 0.03
    ex = excursion_summary(ts2)
    assert ex["n"] == 2 and ex["loss_once_green"] == 1 and abs(ex["avg_mfe"] - 0.03) < 1e-12

    # 7) 报告与 json 成稿不抛错、关键字都在
    rep = build_report(ts, review="both", bar_period=30)
    for kw in ("总览", "分桶", "日节奏", "周节奏", "最佳/最差", "规则化观察", "PF"):
        assert kw in rep, kw
    payload = build_json_payload(ts, [], None, None, "both")
    s = json.dumps(_json_safe(payload), ensure_ascii=False, allow_nan=False)
    assert json.loads(s)["n_trades"] == 6
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 1:
        raise SystemExit(selftest())
    raise SystemExit(run())
