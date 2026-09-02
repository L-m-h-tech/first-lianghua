# -*- coding: utf-8 -*-
"""P1-3 看板图表化（第22轮）：把原本只能 iframe 嵌 txt 的关键结果画成 ECharts 图表。

设计原则（与 WP-F1/F2 一致）：
  - **只做展示层，不改任何打分/信号/回测口径**；数据全部来自既有产物：
      组合账户曲线 -> reports/portfolio_equity.csv（portfolio.py 产物）
      横截面强弱   -> state.last_cross_section（cross_section.rank，每轮内存态）
      因子 IC      -> reports/factor_eval.json（tools/factor_eval.py 的 JSON sidecar）
      胜率校准     -> state.calibrator.band_table() + storage.outcome_stats
  - 纯标准库、零网络、零新增运行依赖；ECharts 是**本地前端资源**（assets/echarts.min.js，
    Apache-2.0），启动/每轮自动同步到 reports/assets/，离线 file:// 直接可看。
  - file:// 下 fetch 不可用（与 report_status.js 同一约束），数据走 window.CHART_DATA
    全局变量（chart_data.js 动态 <script> 注入）；任何一块数据缺失都显空态、绝不报错。
  - 解析/聚合全部写成可合成断言的纯函数，IO 与渲染壳分开，便于 tests/ 固化。

公开接口：
  parse_equity_csv(path)            纯函数：组合权益 CSV -> 图表数据（缺失/损坏安全降级）
  cross_section_payload(cs)         纯函数：cross_section.rank 结果 -> JSON 安全结构
  calibration_payload(band_rows)    纯函数：signal_calibrator.band_table -> 图表结构
  outcomes_payload(db)              纯函数：signal_outcomes 分周期/多空胜率
  factor_payload(path)              纯函数：factor_eval.json -> 图表结构（坏文件返回 None）
  paper_payload(state)              纯函数：storage.paper_equity 纸面影子净值 -> 图表结构（空表 None）
  build_payload(state)              汇总五块（每块独立 try，缺一块不影响其他块）
  write_chart_data(state)           落 reports/chart_data.js（每轮 save 调用）
  charts_page_html()                静态图表看板 HTML（无 Python 变量注入）
  ensure_charts_page()              写静态页 + 同步本地 ECharts 资源（幂等）
"""
import csv
import json
import math
import os
import shutil
from datetime import datetime

import config
import metrics

# ---------------- 通用小工具 ----------------

def _f(x, default=None):
    """宽松 float：空串/None/非法值 -> default（不抛异常，防单行脏数据拖垮整图）。"""
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def downsample(*arrays, max_points=1200):
    """对若干等长并行数组做确定性等距抽稀（首尾必保留），返回抽稀后的元组。

    点数不超过 max_points 时原样返回；图表只用于肉眼看曲线形态，等距抽稀不改变
    首尾值/极值结构，且不触碰原始 CSV（portfolio 绩效口径仍以 txt/CSV 为准）。
    """
    n = len(arrays[0]) if arrays else 0
    if n == 0:
        return tuple([] for _ in arrays)
    if n <= max_points or max_points < 2:
        return tuple(list(a) for a in arrays)
    step = (n - 1) / (max_points - 1)
    idx = []
    seen = set()
    for k in range(max_points):
        i = int(round(k * step))
        if i not in seen:
            seen.add(i)
            idx.append(i)
    if idx[-1] != n - 1:
        idx[-1] = n - 1
    return tuple([a[i] for i in idx] for a in arrays)


# ---------------- ① 组合账户权益/回撤/风险度（portfolio_equity.csv） ----------------

EQUITY_FIELDS = ("dt", "static", "float", "equity", "margin",
                 "available", "risk", "drawdown", "npos")


def parse_equity_csv(path, max_points=1200):
    """解析 portfolio.py 产出的逐 bar 权益曲线 CSV 为图表结构。

    CSV 表头：dt,static,float,equity,margin,available,risk,drawdown,npos
    其中 risk/drawdown 都是**正的小数比例**（0.05=5%）。文件不存在/全坏返回 None（显空态）。"""
    if not path or not os.path.exists(path):
        return None
    cols = {k: [] for k in EQUITY_FIELDS if k != "dt"}
    dts = []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            need = set(cols) | {"dt"}
            if not reader.fieldnames or not need.issubset(set(reader.fieldnames)):
                return None
            for row in reader:
                eq = _f(row.get("equity"))
                if eq is None:
                    continue  # 非法行直接跳过，不中断整图
                dts.append((row.get("dt") or "").strip())
                cols["equity"].append(eq)
                cols["static"].append(_f(row.get("static"), 0.0))
                cols["float"].append(_f(row.get("float"), 0.0))
                cols["margin"].append(_f(row.get("margin"), 0.0))
                cols["available"].append(_f(row.get("available"), 0.0))
                cols["risk"].append(_f(row.get("risk"), 0.0))
                cols["drawdown"].append(max(0.0, _f(row.get("drawdown"), 0.0)))
                try:
                    cols["npos"].append(int(float(row.get("npos") or 0)))
                except (TypeError, ValueError):
                    cols["npos"].append(0)
    except (OSError, UnicodeDecodeError, csv.Error):
        return None
    if not dts:
        return None
    dts, eq, static, flt, margin, avail, risk, dd, npos = downsample(
        dts, cols["equity"], cols["static"], cols["float"], cols["margin"],
        cols["available"], cols["risk"], cols["drawdown"], cols["npos"],
        max_points=max_points)
    n = len(dts)
    final_eq = eq[-1]
    init_eq = eq[0]
    max_dd = max(dd) if dd else 0.0
    risks = [r for r in risk if r is not None]
    avg_risk = sum(risks) / len(risks) if risks else 0.0
    max_risk = max(risks) if risks else 0.0
    total_ret = (final_eq / init_eq - 1.0) if init_eq else 0.0
    return {
        "dt": dts, "equity": eq, "static": static, "float": flt,
        "margin": margin, "available": avail, "risk": risk,
        "drawdown": dd, "npos": npos, "points": n,
        "summary": {
            "init_equity": round(init_eq, 2),
            "final_equity": round(final_eq, 2),
            "total_return": total_ret,          # 小数
            "max_drawdown": max_dd,             # 小数（正）
            "avg_risk": avg_risk,               # 平均风险度
            "max_risk": max_risk,               # 峰值风险度
            "max_npos": max(npos) if npos else 0,
        },
    }


# ---------------- ② 横截面相对强弱（cross_section.rank 的内存结果） ----------------

def cross_section_payload(cs):
    """cross_section.rank() 返回值 -> JSON 安全、字段收敛的图表结构；空结果返回 None。"""
    if not cs or not cs.get("rows"):
        return None
    try:
        rows = [{"name": r["name"], "cat": r.get("cat", "—"),
                 "score": round(float(r.get("score", 0.0)), 2),
                 "chg": float(r.get("chg", 0.0)),
                 "xs": float(r.get("xs", 0.0)),
                 "score_z": float(r.get("score_z", 0.0)),
                 "chg_z": float(r.get("chg_z", 0.0)),
                 "label": r.get("label", "")} for r in cs["rows"]]
        sectors = []
        for cat in cs.get("sector_rank", []):
            s = cs["sectors"][cat]
            sectors.append({"cat": cat, "n": int(s["n"]), "up": int(s["up"]),
                            "down": int(s["down"]), "avg_xs": float(s["avg_xs"]),
                            "avg_chg": float(s.get("avg_chg", 0.0))})
        b = cs.get("breadth") or {}
        return {
            "rows": rows, "sectors": sectors,
            "top_long": [{"name": r["name"], "xs": r["xs"], "score": r["score"]}
                         for r in cs.get("top_long", [])],
            "top_short": [{"name": r["name"], "xs": r["xs"], "score": r["score"]}
                          for r in cs.get("top_short", [])],
            "breadth": {
                "bull": int(b.get("bull", 0)), "bear": int(b.get("bear", 0)),
                "neutral": int(b.get("neutral", 0)), "n": int(b.get("n", 0)),
                "avg_chg": float(b.get("avg_chg", 0.0)),
            },
            "robust": bool(cs.get("robust", False)),
        }
    except (KeyError, TypeError, ValueError):
        return None


# ---------------- ③ 信号胜率校准（signal_calibrator.band_table + outcome_stats） ----------------

def calibration_payload(band_rows):
    """signal_calibrator.SignalCalibrator.band_table() -> 图表结构；空表返回 None。"""
    if not band_rows:
        return None
    out = []
    for c in band_rows:
        out.append({
            "dir": int(c.get("dir", 0)),
            "dir_text": c.get("dir_text", ""),
            "band": c.get("band", ""),
            "n": int(c.get("n", 0)),
            "hits": int(c.get("hits", 0)),
            "winrate": float(c.get("winrate", 0.0)),     # 贝叶斯平滑胜率 0~1
            "avg_ret": float(c.get("avg_ret", 0.0) or 0.0),
            "mult": (None if c.get("mult") is None else float(c.get("mult"))),
            "enough": bool(c.get("enough", False)),
        })
    # 固定展示顺序：做多 强信号→观望，再做空（与 txt 表方向一致）
    order = {"强信号": 0, "分批": 1, "轻仓": 2, "观望": 3}
    out.sort(key=lambda x: (-x["dir"], order.get(x["band"], 9)))
    return out


def outcomes_payload(db, days=None):
    """storage.outcome_stats 聚合成分周期胜率（总/多/空），供校准页辅助柱图；不可用返回 None。"""
    if db is None:
        return None
    try:
        stats = db.outcome_stats(days or config.SIGNAL_TRACK_STAT_DAYS)
    except Exception:
        return None
    if not stats:
        return None
    groups = {}
    for r in stats:
        g = groups.setdefault(int(r["horizon_min"]), {"n": 0, "wins": 0,
                                                       "long_n": 0, "long_w": 0,
                                                       "short_n": 0, "short_w": 0,
                                                       "ret_w": 0.0})
        en = int(r.get("evaluated") or 0)
        wins = int(r.get("wins") or 0)
        g["n"] += en
        g["wins"] += wins
        g["ret_w"] += float(r.get("avg_ret") or 0.0) * en
        if r["direction"] == "做多":
            g["long_n"] += en
            g["long_w"] += wins
        elif r["direction"] == "做空":
            g["short_n"] += en
            g["short_w"] += wins
    labels = {30: "30分钟", 120: "2小时", 1440: "次日"}
    out = []
    for h in sorted(groups):
        g = groups[h]
        if g["n"] <= 0:
            continue
        out.append({
            "horizon": h, "label": labels.get(h, "%d分钟" % h),
            "n": g["n"],
            "winrate": g["wins"] / g["n"],
            "avg_ret": (g["ret_w"] / g["n"]) if g["n"] else 0.0,
            "long_winrate": (g["long_w"] / g["long_n"]) if g["long_n"] else None,
            "long_n": g["long_n"],
            "short_winrate": (g["short_w"] / g["short_n"]) if g["short_n"] else None,
            "short_n": g["short_n"],
        })
    return out or None


# ---------------- ④ 因子 IC（tools/factor_eval.py 写的 JSON sidecar） ----------------

def paper_payload(state=None, max_points=1200):
    """⑤ 纸面账户影子净值：从 storage.paper_equity 每轮快照取最近窗口（升序），结构对齐
    parse_equity_csv 以便前端复用同一套权益/回撤/风险度渲染。无 state/无表/空表返回 None（显空态）。"""
    db = getattr(state, "db", None) if state is not None else None
    if db is None or not hasattr(db, "paper_equity_series"):
        return None
    try:
        rows = db.paper_equity_series(2000)
    except Exception:
        return None
    if not rows:
        return None
    dts, eq, static, flt, margin, avail, risk, dd, npos = ([] for _ in range(9))
    fees_last, trades_last, realized_last = 0.0, 0, 0.0
    for r in rows:
        v = _f(r.get("equity"))
        if v is None:
            continue
        dts.append(str(r.get("ts") or "")[5:16])          # MM-DD HH:MM，轴标签更短
        eq.append(v)
        static.append(_f(r.get("static_equity"), 0.0))
        flt.append(_f(r.get("float_pnl"), 0.0))
        margin.append(_f(r.get("margin_used"), 0.0))
        avail.append(_f(r.get("available"), 0.0))
        risk.append(_f(r.get("risk_degree"), 0.0))
        dd.append(max(0.0, _f(r.get("drawdown"), 0.0)))
        try:
            npos.append(int(r.get("n_positions") or 0))
        except (TypeError, ValueError):
            npos.append(0)
        fees_last = _f(r.get("fees_paid"), fees_last)
        realized_last = _f(r.get("realized"), realized_last)
        try:
            trades_last = int(r.get("n_trades") or trades_last)
        except (TypeError, ValueError):
            pass
    if not dts:
        return None
    dts, eq, static, flt, margin, avail, risk, dd, npos = downsample(
        dts, eq, static, flt, margin, avail, risk, dd, npos, max_points=max_points)
    risks = [r for r in risk if r is not None and math.isfinite(r)]
    init_eq = eq[0]
    fill_mode = getattr(getattr(state, "paper", None), "fill_mode", "next")
    return {
        "dt": dts, "equity": eq, "static": static, "float": flt,
        "margin": margin, "available": avail, "risk": risk, "drawdown": dd,
        "npos": npos, "points": len(dts), "fill_mode": fill_mode,
        "summary": {
            "init_equity": round(init_eq, 2),
            "final_equity": round(eq[-1], 2),
            "total_return": (eq[-1] / init_eq - 1.0) if init_eq else 0.0,
            "max_drawdown": max(dd) if dd else 0.0,
            "avg_risk": sum(risks) / len(risks) if risks else 0.0,
            "max_risk": max(risks) if risks else 0.0,
            "max_npos": max(npos) if npos else 0,
            "fees_paid": round(fees_last, 2),
            "realized": round(realized_last, 2),
            "n_trades": trades_last,
        },
    }


def factor_payload(path=None):
    """读取 factor_eval.json（研究工具离线产出）。文件缺失/损坏返回 None（图表显空态）。"""
    path = path or config.FACTOR_EVAL_JSON
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("factors"), list):
            return None
        return data
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


# ---------------- 汇总与落盘 ----------------

def _tear_from_series(dts, equity, source, max_points=1200):
    """等长 dts/equity 原始权益序列 -> G3 绩效三件（水下曲线/滚动夏普/月度热力）+标量摘要。

    水下按原始逐点序列计算并抽稀；滚动夏普/月度先按自然日收敛成日度收益（一天多轮取最后一点）。
    样本不足（少于2个日度点）返回 None，由前端显空态。"""
    if not dts or len(dts) != len(equity) or len(equity) < 2:
        return None
    raw_rets = metrics.returns_from_equity(equity)
    underwater = [0.0] + metrics.drawdown_series(raw_rets)   # 与 dts 等长、首点 0
    uw_dt, underwater = downsample(list(dts), underwater, max_points=max_points)
    days, day_eq = metrics.daily_last_equity(dts, equity)
    if len(days) < 2:
        return None
    day_rets = metrics.returns_from_equity(day_eq)
    ret_days = days[1:1 + len(day_rets)]          # 每笔日度收益归属其结束日（等长对齐）
    ppy = config.METRICS_BARS_PER_YEAR
    win = config.METRICS_ROLLING_WINDOW
    sheet = metrics.tear_sheet(day_rets, ret_days, bars_per_year=ppy,
                               var_alpha=config.METRICS_VAR_ALPHA, rolling_window=win)
    roll = metrics.rolling_sharpe(day_rets, win, ppy)
    monthly = metrics.monthly_returns(day_rets, ret_days)
    years, cells = [], []
    if monthly:
        years = monthly["years"]
        yidx = {y: i for i, y in enumerate(years)}
        for y, m, v in monthly["cells"]:
            cells.append([m - 1, yidx[y], round(v, 6)])   # [月索引0-11, 年索引, 收益小数]

    def _r(k, nd=4):
        v = sheet.get(k)
        return round(v, nd) if isinstance(v, float) and math.isfinite(v) else v

    summary = {"n": sheet.get("n"), "annualized": _r("annualized"),
               "sharpe": _r("sharpe"), "sortino": _r("sortino"),
               "calmar": _r("calmar"), "omega": _r("omega"),
               "ulcer": _r("ulcer"), "max_drawdown": _r("max_drawdown"),
               "var": _r("var"), "cvar": _r("cvar")}
    return {"source": source, "uw_dt": uw_dt, "underwater": underwater,
            "rs_dt": ret_days,
            "rolling_sharpe": [None if x is None else round(x, 3) for x in roll],
            "rolling_window": win, "monthly_years": years, "monthly_cells": cells,
            "summary": summary}


def tear_payload(state=None, max_points=None):
    """⑥ G3 绩效：优先纸面影子（storage paper_equity 全量快照），否则组合回测 CSV；都没有返回 None。"""
    mp = max_points or config.TEAR_MAX_POINTS
    db = getattr(state, "db", None) if state is not None else None
    if db is not None and hasattr(db, "paper_equity_series"):
        try:
            rows = db.paper_equity_series(20000)
        except Exception:
            rows = []
        dts, eq = [], []
        for r in (rows or []):
            v = _f(r.get("equity"))
            if v is None:
                continue
            dts.append(str(r.get("ts") or ""))
            eq.append(v)
        if len(dts) >= 2:
            t = _tear_from_series(dts, eq, "paper", mp)
            if t is not None:
                return t
    try:
        p = parse_equity_csv(config.PORTFOLIO_EQUITY_FILE, max_points=20000)
    except Exception:
        p = None
    if p and len(p["dt"]) >= 2:
        return _tear_from_series(p["dt"], p["equity"], "portfolio", mp)
    return None


def build_payload(state=None):
    """把四块数据汇总为 window.CHART_DATA 负载；每块独立 try，缺数据只置 None，不抛。"""
    payload = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    # ① 组合账户曲线（离线 CSV，与 state 无关，监控停了也能展示最近一次回测）
    try:
        payload["portfolio"] = parse_equity_csv(config.PORTFOLIO_EQUITY_FILE)
    except Exception:
        payload["portfolio"] = None
    # ② 横截面（仅本轮内存态有）
    try:
        payload["cross_section"] = cross_section_payload(
            getattr(state, "last_cross_section", None))
    except Exception:
        payload["cross_section"] = None
    # ③a 胜率校准（方向×分档）
    try:
        cal = getattr(state, "calibrator", None)
        payload["calibration"] = calibration_payload(
            cal.band_table() if cal is not None else None)
    except Exception:
        payload["calibration"] = None
    # ③b 分周期胜率（SQLite）
    try:
        payload["outcomes"] = outcomes_payload(getattr(state, "db", None))
    except Exception:
        payload["outcomes"] = None
    # ④ 因子 IC（研究工具 JSON）
    try:
        payload["factor_ic"] = factor_payload()
    except Exception:
        payload["factor_ic"] = None
    # ⑤ 纸面账户影子净值（storage paper_equity 每轮快照；休眠/空表自动 None 显空态）
    try:
        payload["paper"] = paper_payload(state)
    except Exception:
        payload["paper"] = None
    # ⑥ G3 绩效三件（水下/滚动夏普/月度热力；优先纸面、否则组合回测；独立 try）
    try:
        payload["tear"] = tear_payload(state)
    except Exception:
        payload["tear"] = None
    return payload


def payload_to_js(payload):
    """dict -> `window.CHART_DATA = {...};`（防 </script> 转义；中文不转 ASCII）。"""
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    text = text.replace("</", "<\\/")
    return "window.CHART_DATA = " + text + ";\n"


def _write_text(path, text):
    """直接写文本（图表文件被占用只跳过，不影响监控主链路）。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return True
    except OSError:
        return False


def write_chart_data(state=None):
    """每轮报告落盘时调用：生成 chart_data.js 供图表页动态注入。"""
    try:
        return _write_text(config.CHART_DATA_JS, payload_to_js(build_payload(state)))
    except Exception:
        return False


def sync_echarts_asset():
    """把项目内置 assets/echarts.min.js 同步到 reports/assets/（幂等；缺失/大小变化才复制）。

    看板以 file:// 打开 reports/图表看板.html，其相对路径 assets/echarts.min.js 必须落在
    reports 目录下；canonical 源文件随仓库走，reports 是运行产物、可被清理，故每次幂等同步。"""
    try:
        src = config.ECHARTS_SRC
        dst = config.ECHARTS_DST
        if not os.path.exists(src):
            return False
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if (not os.path.exists(dst)) or os.path.getsize(dst) != os.path.getsize(src):
            shutil.copyfile(src, dst)
        return True
    except OSError:
        return False


def ensure_charts_page():
    """写静态图表看板页 + 同步 ECharts 本地资源（幂等，可反复调用）。"""
    ok1 = _write_text(config.CHARTS_PAGE_HTML, charts_page_html())
    ok2 = sync_echarts_asset()
    return ok1 and ok2


# ---------------- 静态图表页（无 Python 变量注入；数据全部运行时读 chart_data.js） ----------------

def charts_page_html():
    """独立图表看板页（直链打开用；外层实时看板的内嵌页签复用同一套片段，两处不重复维护）。"""
    return (_PAGE_SHELL_HEAD + _PANEL_STYLE + _PAGE_SHELL_MID + _PANEL_DOM
            + _PAGE_SHELL_BOOT + _PANEL_JS + _PAGE_SHELL_TAIL)


def dashboard_embed_parts():
    """供 report._dashboard_html 内嵌到实时看板：返回 (style, dom, js) 三段纯片段。

    片段不含 <html>/<head>/<body> 外壳、不自动启动；内嵌后由外层"图表看板"页签
    首次激活时调用 window.ChartPanel.activate() 启动，新报告轮次调 reload() 重渲染。"""
    return _PANEL_STYLE, _PANEL_DOM, _PANEL_JS


# ---------------- 图表面板片段（独立页/内嵌页共用；样式全部限定 #charts-panel 作用域） ----------------

# 片段①样式
_PANEL_STYLE = r"""
#charts-panel { margin: 0; background: #141414; color: #d6d6d6;
         font-family: "Microsoft YaHei", Consolas, sans-serif; font-size: 13px; }
#charts-panel.cp-standalone { min-height: 100vh; }
#charts-panel .cp-head { padding: 10px 14px; background: #1f1f1f; border-bottom: 1px solid #333;
          position: sticky; top: 0; z-index: 5; }
#charts-panel .cp-head b { color: #7ecbff; font-size: 15px; margin-right: 12px; }
#charts-panel .cp-gen { color: #9a9a9a; font-size: 12px; }
#charts-panel .cp-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding: 12px; }
#charts-panel .card { background: #1c1c1c; border: 1px solid #2e2e2e; border-radius: 6px; padding: 10px 12px; }
#charts-panel .card.full { grid-column: 1 / span 2; }
#charts-panel .card h3 { margin: 0 0 8px; font-size: 13px; color: #cfe6ff; font-weight: 600; }
#charts-panel .card h3 .sub { color: #8a8a8a; font-weight: 400; font-size: 12px; margin-left: 8px; }
#charts-panel .chart { width: 100%; }
#charts-panel .chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
#charts-panel .chip { background: #262626; border: 1px solid #383838; border-radius: 4px;
          padding: 3px 10px; font-size: 12px; color: #cfcfcf; }
#charts-panel .chip b { color: #ffd66b; font-weight: 600; }
#charts-panel .chip.up b { color: #ef6b6b; }
#charts-panel .chip.down b { color: #43c589; }
#charts-panel .empty { color: #777; padding: 26px 10px; text-align: center; font-size: 12px; }
#charts-panel .note { color: #7d7d7d; font-size: 11px; margin: 6px 2px 0; line-height: 1.6; }
"""

# 片段②DOM（9 个图容器 id 固定，JS 按 id 渲染）
_PANEL_DOM = r"""<div class="cp-head"><b>期货监控 · 图表看板</b><span class="cp-gen" id="cp-gen">正在读取 chart_data.js …</span>
  <span style="color:#666">（随每轮监控自动刷新；数值口径以对应 txt/CSV 为准，图表仅作可视化，不构成投资建议）</span>
</div>
<div class="cp-grid">
  <div class="card full">
    <h3>① 组合账户回测·权益曲线 <span class="sub">portfolio_equity.csv（portfolio.py 离线回测产物）</span></h3>
    <div class="chips" id="eq-chips"></div>
    <div id="c-equity" class="chart" style="height:320px"></div>
  </div>
  <div class="card">
    <h3>权益回撤 <span class="sub">相对历史峰值，越深越红</span></h3>
    <div id="c-dd" class="chart" style="height:240px"></div>
  </div>
  <div class="card">
    <h3>保证金风险度 / 同时持仓数 <span class="sub">风险度=占用÷动态权益；100% 触强平线</span></h3>
    <div id="c-risk" class="chart" style="height:240px"></div>
  </div>
  <div class="card">
    <h3>② 横截面·板块强弱 <span class="sub">板块平均横截面强度 xs（稳健z合成，只横向比较不改综合分）</span></h3>
    <div id="c-sector" class="chart" style="height:300px"></div>
  </div>
  <div class="card">
    <h3>横截面·全品种强度与多空广度 <span class="sub">红=相对偏强 / 绿=相对偏弱</span></h3>
    <div class="chips" id="xs-chips"></div>
    <div id="c-xs" class="chart" style="height:300px"></div>
  </div>
  <div class="card">
    <h3>③ 因子预测力·分周期 meta RankIC <span class="sub">&gt;0=因子越支持信号后续越赚（factor_eval.json）</span></h3>
    <div id="c-ic" class="chart" style="height:300px"></div>
  </div>
  <div class="card">
    <h3>主周期因子分档单调性 <span class="sub">沿因子方向强度分5档的平均方向收益(%)，理想=逐级抬升</span></h3>
    <div id="c-mono" class="chart" style="height:300px"></div>
  </div>
  <div class="card">
    <h3>④ 历史同类信号胜率校准 <span class="sub">贝叶斯平滑胜率（柱，50%参考线）与 sizing 乘子（线，仅 --calibrate 生效）</span></h3>
    <div id="c-cal" class="chart" style="height:300px"></div>
  </div>
  <div class="card">
    <h3>分周期实际胜率 <span class="sub">signal_outcomes 已到期样本（总/做多/做空）</span></h3>
    <div id="c-out" class="chart" style="height:300px"></div>
  </div>
  <div class="card full">
    <h3>⑤ 纸面账户·影子净值 <span class="sub">paper_equity 每轮快照（PAPER_ENABLED 开启后积累；含真实手续费+滑点，虚拟资金非实盘）</span></h3>
    <div class="chips" id="peq-chips"></div>
    <div id="c-paper" class="chart" style="height:320px"></div>
  </div>
  <div class="card">
    <h3>纸面账户回撤 <span class="sub">相对历史峰值，越深越红</span></h3>
    <div id="c-paper-dd" class="chart" style="height:240px"></div>
  </div>
  <div class="card">
    <h3>纸面风险度 / 同时持仓数 <span class="sub">风险度=占用÷动态权益；100% 触强平线</span></h3>
    <div id="c-paper-risk" class="chart" style="height:240px"></div>
  </div>
  <div class="card full">
    <h3>⑥ G3 绩效·水下回撤曲线 <span class="sub">逐点相对历史峰值回撤（与 Ulcer 同源；优先纸面影子、否则组合回测）</span></h3>
    <div class="chips" id="tear-chips"></div>
    <div id="c-tear-uw" class="chart" style="height:280px"></div>
  </div>
  <div class="card">
    <h3>滚动夏普 <span class="sub">近窗口个交易日的年化夏普（默认60日，暖机样本不足显空）</span></h3>
    <div id="c-tear-rs" class="chart" style="height:280px"></div>
  </div>
  <div class="card full">
    <h3>月度收益热力图 <span class="sub">自然月复利收益（%），红涨绿跌（空白=该月无交易日；悬停看精确值）</span></h3>
    <div id="c-tear-m" class="chart" style="height:240px"></div>
  </div>
</div>
"""

# 片段③JS：IIFE 命名空间，避免污染外层看板；独立页自启动、内嵌页由外层页签激活
_PANEL_JS = r"""(function () {
var UP = "#ef6b6b", DOWN = "#43c589", NEUT = "#8a8a8a", BLUE = "#7ecbff", GOLD = "#ffd66b";
var AXIS = "#9a9a9a", SPLIT = "#2c2c2c", BG = "#1c1c1c";
var CHART_IDS = ["c-equity", "c-dd", "c-risk", "c-sector", "c-xs",
                 "c-ic", "c-mono", "c-cal", "c-out",
                 "c-paper", "c-paper-dd", "c-paper-risk",
                 "c-tear-uw", "c-tear-rs", "c-tear-m"];
var inst = {};
function mk(id) {
  var el = document.getElementById(id);
  if (el && el.querySelector(".empty")) { el.innerHTML = ""; el.style.height = ""; }
  var exist = inst[id];
  if (exist) return exist;
  var c = echarts.init(el, null, {renderer: "canvas"});
  inst[id] = c;
  return c;
}
function empty(id, msg) {
  var el = document.getElementById(id);
  if (!el) return;
  if (inst[id]) { inst[id].dispose(); delete inst[id]; }
  el.innerHTML = '<div class="empty">' + msg + '</div>';
  el.style.height = "auto";
}
function baseGrid(extra) {
  return Object.assign({left: 56, right: 46, top: 34, bottom: 28, containLabel: true}, extra || {});
}
function axisStyle(name) {
  return {name: name || "", nameTextStyle: {color: AXIS}, axisLabel: {color: AXIS},
          axisLine: {lineStyle: {color: "#444"}}, splitLine: {lineStyle: {color: SPLIT}}};
}
function pct(v, d) { return (v * 100).toFixed(d == null ? 2 : d) + "%"; }
function wan(v) { return (v / 10000).toFixed(1) + "万"; }
function signedColor(v) { return v > 1e-9 ? UP : (v < -1e-9 ? DOWN : NEUT); }

function renderEquity(p) {
  if (!p || !p.dt.length) {
    empty("c-equity", "暂无组合回测曲线：先运行 portfolio.py（如 python portfolio.py --all），下一轮监控后自动出图。");
    empty("c-dd", "暂无回撤数据。"); empty("c-risk", "暂无风险度数据。");
    return;
  }
  var s = p.summary;
  var chips = [
    ["期初权益", wan(s.init_equity), ""], ["期末权益", wan(s.final_equity), ""],
    ["累计收益", pct(s.total_return), s.total_return >= 0 ? "up" : "down"],
    ["最大回撤", pct(s.max_drawdown), "down"], ["平均风险度", pct(s.avg_risk, 1), ""],
    ["峰值风险度", pct(s.max_risk, 1), ""], ["峰值同时持仓", s.max_npos + " 个", ""]
  ];
  document.getElementById("eq-chips").innerHTML = chips.map(function (t) {
    return '<span class="chip ' + t[2] + '">' + t[0] + ' <b>' + t[1] + '</b></span>';
  }).join("");
  mk("c-equity").setOption({
    backgroundColor: BG, tooltip: {trigger: "axis", valueFormatter: function (v) { return wan(v); }},
    legend: {data: ["动态权益", "静态权益"], textStyle: {color: AXIS}, top: 2},
    grid: baseGrid({right: 56}),
    xAxis: Object.assign({type: "category", data: p.dt, boundaryGap: false}, axisStyle()),
    yAxis: Object.assign({type: "value", scale: true, axisLabel: {color: AXIS, formatter: function (v) { return wan(v); }}},
                         {splitLine: {lineStyle: {color: SPLIT}}, axisLine: {lineStyle: {color: "#444"}}}),
    series: [
      {name: "动态权益", type: "line", data: p.equity, showSymbol: false, lineStyle: {width: 1.6, color: BLUE},
       areaStyle: {color: "rgba(126,203,255,0.08)"},
       markLine: {silent: true, symbol: "none", lineStyle: {color: "#888", type: "dashed"},
                  data: [{yAxis: s.init_equity, label: {position: "insideEndTop",
                          formatter: "期初 " + wan(s.init_equity), color: "#aaa"}}]}},
      {name: "静态权益", type: "line", data: p.static, showSymbol: false,
       lineStyle: {width: 1, color: "#b58cf0", opacity: 0.7}}
    ]
  });
  mk("c-dd").setOption({
    backgroundColor: BG, tooltip: {trigger: "axis", valueFormatter: function (v) { return pct(v); }},
    grid: baseGrid(),
    xAxis: Object.assign({type: "category", data: p.dt, boundaryGap: false}, axisStyle()),
    yAxis: {type: "value", inverse: true, min: 0, splitNumber: 4,
            max: function (v) { return Math.max(v.max * 1.15, 0.005); },
            axisLabel: {color: AXIS, formatter: function (v) { return (v * 100).toFixed(1) + "%"; }},
            splitLine: {lineStyle: {color: SPLIT}}, axisLine: {lineStyle: {color: "#444"}}},
    series: [{name: "回撤", type: "line", data: p.drawdown, showSymbol: false,
              lineStyle: {color: UP, width: 1.2}, areaStyle: {color: "rgba(239,107,107,0.25)"}}]
  });
  mk("c-risk").setOption({
    backgroundColor: BG, tooltip: {trigger: "axis"},
    legend: {data: ["风险度", "持仓数"], textStyle: {color: AXIS}, top: 2},
    grid: baseGrid(),
    xAxis: Object.assign({type: "category", data: p.dt, boundaryGap: false}, axisStyle()),
    yAxis: [
      // 实际风险度通常只有几个百分点：轴上限按数据自适应（至少10%），避免被100%强平参考线压成贴底直线
      {type: "value", min: 0, max: function (v) { return Math.max(0.10, v.max * 1.3); },
       axisLabel: {color: AXIS, formatter: function (v) { return (v * 100).toFixed(0) + "%"; }},
       splitLine: {lineStyle: {color: SPLIT}}, axisLine: {lineStyle: {color: "#444"}}},
      {type: "value", minInterval: 1, axisLabel: {color: AXIS}, splitLine: {show: false}}
    ],
    series: [
      {name: "风险度", type: "line", data: p.risk, showSymbol: false, lineStyle: {color: GOLD, width: 1.3},
       markLine: {silent: true, symbol: "none", lineStyle: {color: UP, type: "dashed"},
                  data: [{yAxis: 1, label: {formatter: "强平线100%", color: UP}}]}},
      {name: "持仓数", type: "bar", yAxisIndex: 1, data: p.npos, itemStyle: {color: "rgba(126,203,255,0.35)"}}
    ]
  });
}

function renderPaper(p) {
  if (!p || !p.dt.length) {
    empty("c-paper", "暂无纸面账户净值：在 config.json 置 PAPER_ENABLED=true 开启影子模拟，监控逐轮积累后自动出图（虚拟资金、非实盘）。");
    empty("c-paper-dd", "暂无纸面回撤数据。"); empty("c-paper-risk", "暂无纸面风险度数据。");
    return;
  }
  var s = p.summary;
  function eq(v) { return (v / 10000).toFixed(2) + "万"; }   // 权益轴两位小数，避免小波动被一位小数抹平
  var chips = [
    ["期初权益", eq(s.init_equity), ""], ["最新权益", eq(s.final_equity), ""],
    ["累计收益", pct(s.total_return), s.total_return >= 0 ? "up" : "down"],
    ["最大回撤", pct(s.max_drawdown), "down"], ["平均风险度", pct(s.avg_risk, 1), ""],
    ["峰值持仓", s.max_npos + " 个", ""], ["累计平仓", s.n_trades + " 笔", ""],
    ["累计手续费", wan(s.fees_paid), ""], ["已实现盈亏", wan(s.realized), s.realized >= 0 ? "up" : "down"]
  ];
  document.getElementById("peq-chips").innerHTML = chips.map(function (t) {
    return '<span class="chip ' + t[2] + '">' + t[0] + ' <b>' + t[1] + '</b></span>';
  }).join("");
  mk("c-paper").setOption({
    backgroundColor: BG, tooltip: {trigger: "axis", valueFormatter: function (v) { return eq(v); }},
    legend: {data: ["动态权益", "静态权益"], textStyle: {color: AXIS}, top: 2},
    grid: baseGrid({right: 56}),
    xAxis: Object.assign({type: "category", data: p.dt, boundaryGap: false}, axisStyle()),
    yAxis: Object.assign({type: "value", scale: true, axisLabel: {color: AXIS, formatter: function (v) { return eq(v); }}},
                         {splitLine: {lineStyle: {color: SPLIT}}, axisLine: {lineStyle: {color: "#444"}}}),
    series: [
      {name: "动态权益", type: "line", data: p.equity, showSymbol: false, lineStyle: {width: 1.6, color: GOLD},
       areaStyle: {color: "rgba(255,214,107,0.08)"},
       markLine: {silent: true, symbol: "none", lineStyle: {color: "#888", type: "dashed"},
                  data: [{yAxis: s.init_equity, label: {position: "insideEndTop",
                          formatter: "期初 " + eq(s.init_equity), color: "#aaa"}}]}},
      {name: "静态权益", type: "line", data: p.static, showSymbol: false,
       lineStyle: {width: 1, color: "#b58cf0", opacity: 0.7}}
    ]
  });
  mk("c-paper-dd").setOption({
    backgroundColor: BG, tooltip: {trigger: "axis", valueFormatter: function (v) { return pct(v); }},
    grid: baseGrid(),
    xAxis: Object.assign({type: "category", data: p.dt, boundaryGap: false}, axisStyle()),
    yAxis: {type: "value", inverse: true, min: 0, splitNumber: 4,
            max: function (v) { return Math.max(v.max * 1.15, 0.005); },
            axisLabel: {color: AXIS, formatter: function (v) { return (v * 100).toFixed(1) + "%"; }},
            splitLine: {lineStyle: {color: SPLIT}}, axisLine: {lineStyle: {color: "#444"}}},
    series: [{name: "纸面回撤", type: "line", data: p.drawdown, showSymbol: false,
              lineStyle: {color: UP, width: 1.2}, areaStyle: {color: "rgba(239,107,107,0.25)"}}]
  });
  mk("c-paper-risk").setOption({
    backgroundColor: BG, tooltip: {trigger: "axis"},
    legend: {data: ["风险度", "持仓数"], textStyle: {color: AXIS}, top: 2},
    grid: baseGrid(),
    xAxis: Object.assign({type: "category", data: p.dt, boundaryGap: false}, axisStyle()),
    yAxis: [
      {type: "value", min: 0, max: function (v) { return Math.max(0.10, v.max * 1.3); },
       axisLabel: {color: AXIS, formatter: function (v) { return (v * 100).toFixed(0) + "%"; }},
       splitLine: {lineStyle: {color: SPLIT}}, axisLine: {lineStyle: {color: "#444"}}},
      {type: "value", minInterval: 1, axisLabel: {color: AXIS}, splitLine: {show: false}}
    ],
    series: [
      {name: "风险度", type: "line", data: p.risk, showSymbol: false, lineStyle: {color: GOLD, width: 1.3},
       markLine: {silent: true, symbol: "none", lineStyle: {color: UP, type: "dashed"},
                  data: [{yAxis: 1, label: {formatter: "强平线100%", color: UP}}]}},
      {name: "持仓数", type: "bar", yAxisIndex: 1, data: p.npos, itemStyle: {color: "rgba(255,214,107,0.30)"}}
    ]
  });
}

function renderTear(t) {
  if (!t) {
    empty("c-tear-uw", "暂无绩效曲线：先运行 portfolio.py（如 python portfolio.py --all），或在 config.json 置 PAPER_ENABLED=true 开启影子，下一轮监控后自动出图。");
    empty("c-tear-rs", "暂无滚动夏普：需要不少于窗口长度的日度权益。");
    empty("c-tear-m", "暂无月度收益：需要跨自然月的日度权益序列。");
    var c0 = document.getElementById("tear-chips"); if (c0) c0.innerHTML = "";
    return;
  }
  var s = t.summary;
  var srcTxt = t.source === "paper" ? "纸面影子" : "组合回测";
  function f2(v) { return (v == null || isNaN(v)) ? "-" : (+v).toFixed(2); }
  var chips = [
    ["数据来源", srcTxt, ""], ["年化夏普", f2(s.sharpe), ""], ["Sortino", f2(s.sortino), ""],
    ["Calmar", f2(s.calmar), ""], ["Omega", f2(s.omega), ""], ["Ulcer", s.ulcer==null?"-":(+s.ulcer*100).toFixed(2)+"%", ""],
    ["VaR95(日)", s.var==null?"-":(s.var*100).toFixed(2)+"%", "down"],
    ["CVaR95(日)", s.cvar==null?"-":(s.cvar*100).toFixed(2)+"%", "down"]
  ];
  document.getElementById("tear-chips").innerHTML = chips.map(function (x) {
    return '<span class="chip ' + x[2] + '">' + x[0] + ' <b>' + x[1] + '</b></span>';
  }).join("");
  mk("c-tear-uw").setOption({
    backgroundColor: BG, tooltip: {trigger: "axis", valueFormatter: function (v) { return pct(v); }},
    grid: baseGrid(),
    xAxis: Object.assign({type: "category", data: t.uw_dt, boundaryGap: false}, axisStyle()),
    yAxis: {type: "value", inverse: true, min: 0, splitNumber: 4,
            max: function (v) { return Math.max(v.max * 1.15, 0.005); },
            axisLabel: {color: AXIS, formatter: function (v) { return (v * 100).toFixed(1) + "%"; }},
            splitLine: {lineStyle: {color: SPLIT}}, axisLine: {lineStyle: {color: "#444"}}},
    series: [{name: "水下回撤", type: "line", data: t.underwater, showSymbol: false,
              lineStyle: {color: UP, width: 1.2}, areaStyle: {color: "rgba(239,107,107,0.25)"}}]
  });
  var rs = t.rolling_sharpe;
  mk("c-tear-rs").setOption({
    backgroundColor: BG, tooltip: {trigger: "axis"},
    grid: baseGrid(),
    xAxis: Object.assign({type: "category", data: t.rs_dt, boundaryGap: false}, axisStyle()),
    yAxis: {type: "value", scale: true, axisLabel: {color: AXIS},
            splitLine: {lineStyle: {color: SPLIT}}, axisLine: {lineStyle: {color: "#444"}}},
    series: [{name: "滚动" + t.rolling_window + "日夏普", type: "line", data: rs,
              showSymbol: false, connectNulls: true, lineStyle: {color: BLUE, width: 1.4},
              markLine: {silent: true, symbol: "none", lineStyle: {color: "#888", type: "dashed"},
                         data: [{yAxis: 0, label: {formatter: "0", color: "#aaa"}}]}}]
  });
  var years = t.monthly_years.map(String);
  var cells = t.monthly_cells.map(function (c) { return [c[0], c[1], c[2]]; });
  var maxAbs = cells.length ? Math.max.apply(null, cells.map(function (c) { return Math.abs(c[2]); })) : 0;
  mk("c-tear-m").setOption({
    backgroundColor: BG,
    tooltip: {position: "top", formatter: function (p) {
      return years[p.value[1]] + "年" + (p.value[0] + 1) + "月：" + (p.value[2] * 100).toFixed(2) + "%"; }},
    grid: baseGrid({top: 20, bottom: 48}),
    xAxis: {type: "category", data: ["1月","2月","3月","4月","5月","6月","7月","8月","9月","10月","11月","12月"],
            axisLabel: {color: AXIS}, splitArea: {show: false}, axisLine: {lineStyle: {color: "#444"}}},
    yAxis: {type: "category", data: years, axisLabel: {color: AXIS},
            splitArea: {show: false}, axisLine: {lineStyle: {color: "#444"}}},
    visualMap: {min: -maxAbs || -0.01, max: maxAbs || 0.01, calculable: false, show: false,
                inRange: {color: [DOWN, "#333333", UP]}},
    series: [{name: "月度收益", type: "heatmap", data: cells,
              label: {show: true, color: "#fff", fontSize: 10, fontWeight: "bold",
                      formatter: function (p) { var v = p.value[2] * 100; return (v >= 0 ? "+" : "") + v.toFixed(2); }},
              itemStyle: {borderColor: BG, borderWidth: 3}}]
  });
}

function renderCross(cs) {
  if (!cs || !cs.rows.length) {
    empty("c-sector", "暂无横截面数据：监控完成一轮分析后自动生成（非交易时段同样生成）。");
    empty("c-xs", "暂无横截面数据。"); return;
  }
  var sec = cs.sectors.slice().sort(function (a, b) { return a.avg_xs - b.avg_xs; });
  mk("c-sector").setOption({
    backgroundColor: BG,
    tooltip: {trigger: "axis", axisPointer: {type: "shadow"},
              formatter: function (ps) { var d = sec[ps[0].dataIndex];
                return d.cat + "<br/>平均xs " + d.avg_xs.toFixed(2) + "（涨" + d.up + "/跌" + d.down + "，" + d.n + "个）"; }},
    grid: baseGrid({right: 60}),
    xAxis: Object.assign({type: "value"}, axisStyle("平均xs")),
    yAxis: {type: "category", data: sec.map(function (d) { return d.cat; }),
            axisLabel: {color: AXIS}, axisLine: {lineStyle: {color: "#444"}}},
    series: [{type: "bar", data: sec.map(function (d) {
      return {value: d.avg_xs, itemStyle: {color: signedColor(d.avg_xs)}}; }),
      barWidth: 16, label: {show: true, position: "right", color: AXIS,
      formatter: function (p) { return p.value.toFixed(2); }} }]
  });
  var b = cs.breadth;
  document.getElementById("xs-chips").innerHTML =
    '<span class="chip up">偏多 <b>' + b.bull + '</b></span>' +
    '<span class="chip">中性 <b>' + b.neutral + '</b></span>' +
    '<span class="chip down">偏空 <b>' + b.bear + '</b></span>' +
    '<span class="chip">平均涨跌 <b style="color:' + signedColor(b.avg_chg) + '">' + pct(b.avg_chg) + '</b></span>' +
    (cs.robust ? "" : '<span class="chip">样本不足，未做稳健z</span>');
  var rows = cs.rows.slice().sort(function (a, b) { return a.xs - b.xs; });
  mk("c-xs").setOption({
    backgroundColor: BG,
    tooltip: {trigger: "axis", axisPointer: {type: "shadow"},
              formatter: function (ps) { var d = rows[ps[0].dataIndex];
                return d.name + "（" + d.cat + "）<br/>xs " + d.xs.toFixed(2) +
                  "｜综合分 " + d.score.toFixed(1) + "｜当日 " + pct(d.chg) + "｜" + d.label; }},
    grid: baseGrid({left: 16, right: 44}),
    dataZoom: [{type: "inside"}, {type: "slider", height: 12, bottom: 4,
               textStyle: {color: AXIS}, borderColor: "#333"}],
    xAxis: Object.assign({type: "value"}, axisStyle("xs")),
    yAxis: {type: "category", data: rows.map(function (d) { return d.name; }),
            axisLabel: {color: AXIS, fontSize: 10}, axisLine: {lineStyle: {color: "#444"}}},
    series: [{type: "bar", data: rows.map(function (d) {
      return {value: d.xs, itemStyle: {color: signedColor(d.xs)}}; }), barWidth: 9}]
  });
}

function renderFactor(f) {
  if (!f || !f.factors.length) {
    empty("c-ic", "暂无因子IC评估：离线运行 python tools/factor_eval.py 后生成 factor_eval.json，下一轮监控自动出图。");
    empty("c-mono", "暂无因子分档数据。"); return;
  }
  var hs = f.horizons.map(String), colors = {30: "#b58cf0", 120: BLUE, 1440: GOLD};
  var names = f.factors.map(function (x) { return x.name; });
  var series = hs.map(function (h) {
    return {name: (f.horizon_labels || {})[h] || h, type: "bar",
            itemStyle: {color: colors[h] || NEUT},
            data: f.factors.map(function (x) {
              var m = x.by_h[h]; return m ? +m.rank_ic.toFixed(3) : null; })};
  });
  mk("c-ic").setOption({
    backgroundColor: BG, tooltip: {trigger: "axis", axisPointer: {type: "shadow"}},
    legend: {textStyle: {color: AXIS}, top: 2},
    grid: baseGrid({top: 40}),
    xAxis: {type: "category", data: names, axisLabel: {color: AXIS, rotate: 30, fontSize: 10},
            axisLine: {lineStyle: {color: "#444"}}},
    yAxis: Object.assign({type: "value", name: "meta RankIC"}, axisStyle()),
    series: series.concat([{type: "line", data: names.map(function () { return 0; }),
      silent: true, showSymbol: false, lineStyle: {color: "#777", type: "dashed", width: 1}}])
  });
  var mainH = String(f.main_h);
  var qx = ["Q1最弱", "Q2", "Q3", "Q4", "Q5最强"];
  var mono = f.factors.filter(function (x) {
    var m = x.by_h[mainH]; return m && m.n >= 20 && m.buckets;
  }).map(function (x) {
    return {name: x.name, type: "line", smooth: false, symbol: "circle", symbolSize: 5,
            data: x.by_h[mainH].buckets.map(function (b) { return +(b[1] * 100).toFixed(3); })};
  });
  if (!mono.length) { empty("c-mono", "主周期因子样本均不足（n≥20 才画线），样本随常驻监控持续积累。"); return; }
  mk("c-mono").setOption({
    backgroundColor: BG, tooltip: {trigger: "axis"},
    legend: {type: "scroll", top: 2, textStyle: {color: AXIS, fontSize: 10}},
    grid: baseGrid({top: 44}),
    xAxis: {type: "category", data: qx, axisLabel: {color: AXIS}, boundaryGap: false,
            axisLine: {lineStyle: {color: "#444"}}},
    yAxis: Object.assign({type: "value", name: "平均方向收益(%)"}, axisStyle()),
    series: mono
  });
}

function renderCalib(rows, outs) {
  if (!rows || !rows.length) {
    empty("c-cal", "暂无胜率校准数据：signal_outcomes 样本随监控积累，方向×分档样本充足后自动出图。");
  } else {
    var cats = rows.map(function (r) { return r.dir_text + "·" + r.band; });
    mk("c-cal").setOption({
      backgroundColor: BG, tooltip: {trigger: "axis", axisPointer: {type: "shadow"},
        formatter: function (ps) { var r = rows[ps[0].dataIndex];
          var line = cats[ps[0].dataIndex] + "（n=" + r.n + "）<br/>平滑胜率 " + pct(r.winrate, 1) +
            "｜平均方向收益 " + pct(r.avg_ret);
          if (r.mult != null) line += "<br/>sizing乘子 ×" + r.mult.toFixed(2);
          else line += "<br/>样本积累中，不给乘子";
          return line; }},
      legend: {data: ["平滑胜率", "乘子"], textStyle: {color: AXIS}, top: 2},
      grid: baseGrid({top: 40, right: 56}),
      xAxis: {type: "category", data: cats, axisLabel: {color: AXIS, rotate: 28, fontSize: 10, interval: 0},
              axisLine: {lineStyle: {color: "#444"}}},
      yAxis: [
        Object.assign({type: "value", min: 0, max: 1,
          axisLabel: {color: AXIS, formatter: function (v) { return (v * 100).toFixed(0) + "%"; }}},
          {splitLine: {lineStyle: {color: SPLIT}}, axisLine: {lineStyle: {color: "#444"}}}),
        {type: "value", min: 0.4, max: 1.3, axisLabel: {color: AXIS}, splitLine: {show: false}}
      ],
      series: [
        {name: "平滑胜率", type: "bar", barWidth: 20,
         data: rows.map(function (r) { return {value: +r.winrate.toFixed(4),
           itemStyle: {color: r.winrate >= 0.5 ? UP : DOWN, opacity: r.enough ? 0.9 : 0.45}}; }),
         markLine: {silent: true, symbol: "none", lineStyle: {color: "#aaa", type: "dashed"},
                    data: [{yAxis: 0.5, label: {formatter: "50%", color: "#aaa"}}]}},
        {name: "乘子", yAxisIndex: 1, type: "line", connectNulls: false, symbolSize: 7,
         lineStyle: {color: GOLD, width: 1.6}, itemStyle: {color: GOLD},
         data: rows.map(function (r) { return r.mult; })}
      ]
    });
  }
  if (!outs || !outs.length) {
    empty("c-out", "暂无已到期信号样本：信号发出后 30分钟/2小时/次日自动回填结果。"); return;
  }
  mk("c-out").setOption({
    backgroundColor: BG, tooltip: {trigger: "axis", axisPointer: {type: "shadow"}},
    legend: {data: ["总胜率", "做多", "做空"], textStyle: {color: AXIS}, top: 2},
    grid: baseGrid({top: 40}),
    xAxis: {type: "category", data: outs.map(function (o) { return o.label + "\nn=" + o.n; }),
            axisLabel: {color: AXIS, interval: 0, lineHeight: 15}, axisLine: {lineStyle: {color: "#444"}}},
    yAxis: {type: "value", min: 0, max: 1, axisLabel: {color: AXIS,
      formatter: function (v) { return (v * 100).toFixed(0) + "%"; }},
      splitLine: {lineStyle: {color: SPLIT}}, axisLine: {lineStyle: {color: "#444"}}},
    series: [
      {name: "总胜率", type: "bar", itemStyle: {color: BLUE}, data: outs.map(function (o) { return +o.winrate.toFixed(4); }),
       markLine: {silent: true, symbol: "none", lineStyle: {color: "#aaa", type: "dashed"},
                  data: [{yAxis: 0.5}]}},
      {name: "做多", type: "line", itemStyle: {color: UP}, data: outs.map(function (o) { return o.long_winrate == null ? null : +o.long_winrate.toFixed(4); })},
      {name: "做空", type: "line", itemStyle: {color: DOWN}, data: outs.map(function (o) { return o.short_winrate == null ? null : +o.short_winrate.toFixed(4); })}
    ]
  });
}

function setGen(text) {
  var el = document.getElementById("cp-gen");
  if (el) el.textContent = text;
}
function loadAndRender() {
  if (typeof echarts === "undefined") {
    setGen("本地 ECharts 资源缺失（assets/echarts.min.js）：运行 python charts.py --rebuild 或等下一轮监控自动同步。");
    CHART_IDS.forEach(function (id) { empty(id, "图表库未加载"); });
    return;
  }
  var sc = document.createElement("script");
  sc.src = "chart_data.js?t=" + Date.now();
  sc.onload = function () {
    sc.remove();
    var D = window.CHART_DATA;
    if (!D) { setGen("chart_data.js 暂无有效数据（等下一轮监控写出）"); return; }
    setGen("数据更新于 " + D.generated_at);
    renderEquity(D.portfolio);
    renderCross(D.cross_section);
    renderFactor(D.factor_ic);
    renderCalib(D.calibration, D.outcomes);
    renderPaper(D.paper);
    renderTear(D.tear);
  };
  sc.onerror = function () { sc.remove(); setGen(
    "未找到 chart_data.js（运行一轮监控后自动生成；各图先显示空态）");
    renderEquity(null); renderCross(null); renderFactor(null); renderCalib(null, null);
    renderPaper(null); renderTear(null); };
  document.body.appendChild(sc);
}
function resizeAll() { Object.keys(inst).forEach(function (k) { inst[k].resize(); }); }

var started = false;
function activate() {                 // 外层页签首次激活：加载并渲染；再次激活：resize 兜底
  if (!started) { started = true; loadAndRender(); } else { resizeAll(); }
}
function reload() {                   // 新报告轮次：仅当面板曾被打开过才重渲染，省后台开销
  if (started) loadAndRender();
}
window.ChartPanel = {activate: activate, reload: reload, resizeAll: resizeAll};
window.addEventListener("resize", resizeAll);
if (window.__CHARTS_STANDALONE__) { started = true; loadAndRender(); }   // 独立页打开即自启
})();
"""

_PAGE_SHELL_HEAD = ('<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
                    '<title>期货监控·图表看板（ECharts 本地渲染，数据随每轮监控自动刷新）</title>\n'
                    '<style>')
_PAGE_SHELL_MID = ('\n</style>\n<script src="assets/echarts.min.js"></script>\n</head>\n<body>\n'
                   '<div id="charts-panel" class="cp-standalone">\n')
_PAGE_SHELL_BOOT = ('\n</div>\n<script>window.__CHARTS_STANDALONE__ = true;</script>\n<script>\n')
_PAGE_SHELL_TAIL = '\n</script>\n</body>\n</html>\n'



# ---------------- 离线手动刷新（python charts.py --rebuild） ----------------

def _rebuild_from_db():
    """监控未运行时也能从 SQLite + CSV/JSON 重建 chart_data.js（横截面/校准内存态留空态）。"""
    import storage
    db = storage.MonitorDB()

    class _State:
        pass

    st = _State()
    st.db = db
    st.last_cross_section = {}
    try:
        import signal_calibrator
        st.calibrator = signal_calibrator.SignalCalibrator(db)
    except Exception:
        st.calibrator = None
    try:
        ok = write_chart_data(st)
        ensure_charts_page()
        return 0 if ok else 1
    finally:
        db.close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="图表看板数据/静态页手动重建（P1-3）")
    ap.add_argument("--rebuild", action="store_true", help="从 DB+CSV/JSON 重建 chart_data.js 与静态页")
    args = ap.parse_args()
    if args.rebuild:
        raise SystemExit(_rebuild_from_db())
    # 无参数：仅确保静态页与本地 ECharts 资源就位
    ensure_charts_page()
    print("charts page -> %s" % config.CHARTS_PAGE_HTML)
