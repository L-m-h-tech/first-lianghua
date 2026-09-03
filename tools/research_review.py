# -*- coding: utf-8 -*-
r"""G30③（第43轮）研究侧一键复盘编排器：tools/research_review.py，纯标准库、零网络、只读。

定位（总纲 G30③「一键日/周复盘：行情→因子表现 G29→信号命中→交易归因 G28→风险 G3/G5→待办」）：
把各研究工具**已经落盘**的 reports/*.json sidecar + 组合权益 CSV + 主链信号追踪文本聚合成一份
"收盘研究简报 + 规则化待办清单"，秒级出 reports/research_review.txt（+ .json）。

为什么不 subprocess 重跑各工具：①解耦——任一工具缺失/报错不影响成稿；②秒级、零副作用；
③各研究工具本来就按各自节奏人工跑，编排器只负责"把最新结论串起来并指出该先做什么"。

命名说明：主链已有 reports/daily_review.txt（report.build_daily_review，实时轮动+新闻、永久保留，
属 main 主链，铁律不动），故研究侧编排器与产物命名为 research_review.* 以彻底隔离，不覆盖主链文件。

纪律（照 G21–G30 研究侧惯例）：
- 只读 reports，不 import 任何项目生产模块、不被 main import、不改主链/综合分/默认 CSV；
- 任一 sidecar 缺失/损坏/字段不全/陈旧，全部安全降级（标注状态与刷新命令），绝不抛错；
- 待办是"提示与决策素材"不是"调参令"：任何策略改动仍须另开轮次走双样本+影子+默认回退。
"""
import argparse
import csv
import datetime as _dt
import io
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_REPORTS = os.path.join(_ROOT, "reports")
DEFAULT_OUT = os.path.join(_REPORTS, "research_review.txt")
DEFAULT_JSON = os.path.join(_REPORTS, "research_review.json")

SEP = "=" * 96
SUB = "-" * 96

# sidecar 台账：key=(文件名, 中文标签, 刷新命令)；顺序即报告与新鲜度表顺序
SOURCES = [
    ("factor_health.json", "G29 因子体检(IC/失效预警/半衰期)", r"D:\Python\python.exe tools\factor_health.py"),
    ("factor_regime.json", "G29续 因子regime/换手/衰减形态", r"D:\Python\python.exe tools\factor_regime.py"),
    ("microstructure_lab.json", "G24 微结构/持仓/季节因子族(ΔOI/Amihud/特异波动/偏度/日历)", r"D:\Python\python.exe tools\microstructure_lab.py"),
    ("attribution.json", "G28 收益归因(OLS/BHB/板块)", r"D:\Python\python.exe tools\attribution.py"),
    ("trade_journal.json", "G30① 交易复盘journal(分桶/MFE-MAE)", r"D:\Python\python.exe tools\trade_journal.py --bars --period 30"),
    ("portfolio_lab.json", "G26 组合构建实验台(等权/逆波/ERC/GMV)", r"D:\Python\python.exe tools\portfolio_lab.py"),
    ("backtest_validation.json", "WP-F4 防过拟合(DSR/CSCV-PBO)", r"D:\Python\python.exe tools\backtest_validation.py"),
    ("expr_research.json", "G25 表达式因子研究(可选)", r"D:\Python\python.exe tools\expr_research.py"),
    ("portfolio_equity.csv", "组合账户逐bar权益/风险度(回测)", r"D:\Python\python.exe portfolio.py --all --period 30"),
    ("signal_tracking.txt", "主链信号效果追踪(最近7天,自动产出)", "由 main 常驻监控自动生成，无需手动跑"),
]

# 阈值（只用于"提示"，不驱动任何交易/改参）
STALE_HOURS_DEFAULT = 168          # sidecar 超过 7 天视为陈旧
OPTIONAL_SOURCES = {"expr_research.json"}   # 缺失只提示不告警的可选产物
JOURNAL_PF_WARN = 1.0              # 组合整体 PF 低于此=成本后期望为负
BUCKET_PF_WEAK = 0.7               # 分桶 PF 低于此且 n>=10 视为弱势桶
BUCKET_N_MIN = 10
EQUITY_DD_WARN = 0.15              # 组合回测最大回撤超过 15% 提示
EQUITY_RISK_WARN = 0.80            # 期末风险度（保证金/权益）超过 80% 提示
DSR_PASS = 0.95                    # DSR 通过多重试验校正的门槛


# =========================== 通用装载 ===========================
def _now():
    return _dt.datetime.now()


def load_sidecar(path, now=None):
    """读 json sidecar，返 (obj, mtime)；文件缺失/损坏返 (None, mtime_or_None)，绝不抛错。"""
    now = now or _now()
    if not path or not os.path.exists(path):
        return None, None
    try:
        mt = _dt.datetime.fromtimestamp(os.path.getmtime(path))
    except OSError:
        mt = None
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            return json.load(f), mt
    except Exception:
        return None, mt


def freshness_state(mtime, now=None, stale_hours=STALE_HOURS_DEFAULT):
    """missing / ok / stale 三态。"""
    if mtime is None:
        return "missing"
    now = now or _now()
    age_h = (now - mtime).total_seconds() / 3600.0
    return "stale" if age_h > stale_hours else "ok"


def age_label(mtime, now=None):
    if mtime is None:
        return "—"
    now = now or _now()
    age_h = (now - mtime).total_seconds() / 3600.0
    if age_h < 1:
        return "%d分钟前" % max(1, int(age_h * 60))
    if age_h < 48:
        return "%.1f小时前" % age_h
    return "%.1f天前" % (age_h / 24.0)


def load_equity_summary(path):
    """从 portfolio_equity.csv 读期末权益/风险度/持仓数，并全表扫最大回撤。缺文件/坏值安全返 {}。"""
    out = {}
    if not path or not os.path.exists(path):
        return out
    try:
        with io.open(path, "r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return out
        max_dd = 0.0
        valid = []
        for r in rows:
            if not r or not (r.get("dt") or "").strip():
                continue                    # 跳过文件末尾换行产生的空记录
            try:
                dd = abs(float(r.get("drawdown") or 0.0))
                max_dd = max(max_dd, dd)
            except (TypeError, ValueError):
                pass
            valid.append(r)
        if not valid:
            return out
        first, last = valid[0], valid[-1]
        out["n_bars"] = len(valid)
        out["start_dt"] = first.get("dt", "")
        g = lambda r, k: float(r.get(k) or 0.0)
        out["end_dt"] = last.get("dt", "")
        out["equity"] = g(last, "equity")
        out["start_equity"] = g(first, "equity")
        out["static"] = g(first, "static") or g(first, "equity")
        out["risk"] = g(last, "risk")
        out["margin"] = g(last, "margin")
        out["npos"] = int(g(last, "npos"))
        out["max_drawdown"] = max_dd
        out["ret"] = (out["equity"] / out["start_equity"] - 1.0) if out["start_equity"] else 0.0
    except Exception:
        return {}
    return out


_SIG_PERIOD_RE = re.compile(r"^\s*(\S+?)\s+样本\s*(\d+).*?胜率\s*([\d.]+)%.*?平均方向收益\s*([+-]?[\d.]+)%")


def load_signal_tracking(path):
    """从主链 signal_tracking.txt 提取每个周期首行：周期/样本/胜率/平均方向收益。缺文件安全返 []。"""
    if not path or not os.path.exists(path):
        return []
    out = []
    try:
        with io.open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                m = _SIG_PERIOD_RE.match(line.strip())
                if m:
                    out.append({
                        "period": m.group(1),
                        "n": int(m.group(2)),
                        "win_rate": float(m.group(3)) / 100.0,
                        "avg_dir_ret": float(m.group(4)) / 100.0,
                    })
    except Exception:
        return out
    return out


# =========================== 各 sidecar 段提取（缺字段安全返 {}） ===========================
def sec_factor_health(obj):
    """G29：event[30] 各因子 verdict/IC/连续失败；daily 各列 H=5 IC 与半衰期。"""
    if not isinstance(obj, dict):
        return {}
    out = {"alerts": [], "event_rows": [], "daily_ic": [], "halflife": []}
    ev = obj.get("event") or {}
    e30 = ev.get("30") or ev.get(30) or {}
    if isinstance(e30, dict):
        for fname, st in e30.items():
            if not isinstance(st, dict):
                continue
            row = {"factor": fname, "ic": st.get("ic"), "verdict": st.get("verdict", ""),
                   "n": st.get("n"), "max_consec_fail": st.get("max_consec_fail"),
                   "frac_fail": st.get("frac_fail")}
            out["event_rows"].append(row)
            if isinstance(row["verdict"], str) and ("失效" in row["verdict"] or "预警" in row["verdict"]):
                out["alerts"].append(row)
    daily = obj.get("daily") or {}
    if isinstance(daily, dict):
        for col, blk in daily.items():
            if not isinstance(blk, dict):
                continue
            h5 = blk.get("5") or blk.get(5)
            if isinstance(h5, dict) and h5.get("ic") is not None:
                out["daily_ic"].append({"factor": col, "ic": h5.get("ic"), "n": h5.get("n")})
            hl = blk.get("halflife")
            if isinstance(hl, dict) and hl.get("half_life") is not None:
                out["halflife"].append({"factor": col, "half_life": hl.get("half_life")})
        out["daily_ic"].sort(key=lambda r: (r["ic"] if r["ic"] is not None else 0.0))
    return out


def sec_attribution(obj, horizon="30"):
    """G28：指定周期 alpha/R²/n、因子贡献 top、板块 BHB effect top。"""
    if not isinstance(obj, dict):
        return {}
    hs = obj.get("horizons") or {}
    h = hs.get(horizon) or hs.get(str(horizon))
    if not isinstance(h, dict):
        return {}
    facs = [f for f in (h.get("factors") or []) if isinstance(f, dict)]
    facs_sorted = sorted(facs, key=lambda x: (x.get("contrib") or 0.0))
    secs = [s for s in (h.get("bhb_sectors") or []) if isinstance(s, dict)]
    secs_sorted = sorted(secs, key=lambda x: (x.get("effect") or 0.0))
    return {
        "horizon": horizon, "n": h.get("n"), "enough": h.get("enough"),
        "alpha": h.get("alpha"), "r2": h.get("r2"),
        "factor_bottom": facs_sorted[:3],
        "factor_top": list(reversed(facs_sorted[-3:])),
        "sector_bottom": secs_sorted[:3],
        "sector_top": list(reversed(secs_sorted[-3:])),
    }


def sec_journal(obj):
    """G30①：总览 + 持仓档/信号档弱势桶 + 由盈转亏比例。"""
    if not isinstance(obj, dict):
        return {}
    ov = obj.get("overall") or {}
    if not ov:
        return {}

    def weak(buckets):
        rows = []
        for b in (buckets or []):
            if not isinstance(b, dict):
                continue
            pf, n = b.get("pf"), b.get("n", 0) or 0
            if pf is not None and n >= BUCKET_N_MIN and pf < BUCKET_PF_WEAK:
                rows.append({"key": b.get("key"), "n": n, "pf": pf, "net": b.get("net")})
        rows.sort(key=lambda r: r["pf"])
        return rows

    ex = obj.get("excursion") or {}
    green_ratio = None
    if isinstance(ex, dict) and ex.get("n_loss"):
        green_ratio = (ex.get("loss_once_green") or 0) / float(ex["n_loss"])
    return {
        "n_trades": obj.get("n_trades"),
        "win_rate": ov.get("win_rate"), "pf": ov.get("profit_factor"),
        "payoff": ov.get("payoff_ratio"), "expectancy": ov.get("expectancy"),
        "max_win_streak": ov.get("max_win_streak"), "max_loss_streak": ov.get("max_loss_streak"),
        "weak_hold": weak(obj.get("by_hold_band")),
        "weak_score": weak(obj.get("by_score_band")),
        "loss_once_green": ex.get("loss_once_green") if isinstance(ex, dict) else None,
        "n_loss": ex.get("n_loss") if isinstance(ex, dict) else None,
        "green_ratio": green_ratio,
    }


def sec_lab(obj):
    """G26：rolling_stats 四方法风险调整后表现对比 + snapshot 有效N。"""
    if not isinstance(obj, dict):
        return {}
    rs = obj.get("rolling_stats") or {}
    methods = {}
    for m, st in rs.items():
        if isinstance(st, dict):
            methods[m] = {k: st.get(k) for k in ("ann_ret", "ann_vol", "sharpe", "maxdd", "ann_turnover", "avg_eff_n")}
    snap = obj.get("snapshot") or {}
    effn = {}
    for m, st in snap.items():
        if isinstance(st, dict):
            effn[m] = st.get("eff_n")
    return {"n_universe": obj.get("n_universe"), "n_days": obj.get("n_days"),
            "methods": methods, "snapshot_eff_n": effn}


def sec_validation(obj):
    """WP-F4：组合 DSR/裁决、参数网格 PBO 概况。"""
    if not isinstance(obj, dict):
        return {}
    dsr = obj.get("dsr") or {}
    grid = obj.get("grid") or {}
    return {
        "n_days": dsr.get("n_days"), "sr_obs": dsr.get("sr_obs"), "sr0": dsr.get("sr0"),
        "dsr": dsr.get("dsr"), "verdict": dsr.get("verdict"), "n_trials": dsr.get("n_trials"),
        "grid_n": grid.get("n"), "pbo_good": grid.get("pbo_good"),
        "oos_pos": grid.get("oos_pos"), "all_loss": grid.get("all_loss"),
    }


# =========================== 规则化待办引擎 ===========================
def build_actions(bundle, freshness, now=None):
    """据各段结果与新鲜度生成 [(level, text)]，level∈WARN/INFO/OK；WARN 在前。纯规则、不驱动交易。"""
    acts = []

    def warn(t):
        acts.append(("WARN", t))

    def info(t):
        acts.append(("INFO", t))

    # 1) 缺失/陈旧/损坏
    refresh = {name: cmd for name, _lab, cmd in SOURCES}
    labels = {name: lab for name, lab, _cmd in SOURCES}
    for name, st in freshness.items():
        if st["state"] == "missing":
            # signal_tracking 由主链自动产出、expr_research 为可选项：缺失只作 INFO
            if name == "signal_tracking.txt":
                info("暂无主链信号追踪（%s）：启动一次 main 常驻监控后自动生成。" % labels.get(name, name))
            elif name in OPTIONAL_SOURCES:
                info("可选产物 %s（%s）未生成，需要时运行：%s" % (name, labels.get(name, name), refresh.get(name, "")))
            else:
                warn("缺少研究产物 %s（%s），先运行：%s" % (name, labels.get(name, name), refresh.get(name, "")))
        elif st["state"] == "stale":
            info("%s（%s）已陈旧（%s生成，距今%s），建议刷新：%s" %
                 (name, labels.get(name, name), st.get("mtime", "—"), st.get("age", "—"), refresh.get(name, "")))
        elif st["state"] == "broken":
            warn("%s 解析失败（文件可能写了一半），重跑：%s" % (name, refresh.get(name, "")))

    # 2) 因子体检预警
    fh = bundle.get("factor_health") or {}
    for a in fh.get("alerts", []):
        warn("G29 因子体检：事件因子「%s」%s（IC=%s，最长连续失败%s，失败占比%s）——观察是否 regime 切换，勿直接删因子。"
             % (a.get("factor"), a.get("verdict"), _pct(a.get("ic"), 3, signed=True),
                a.get("max_consec_fail"), _pct(a.get("frac_fail"), 1)))

    # 3) journal 弱势结构
    j = bundle.get("journal") or {}
    if j:
        if j.get("pf") is not None and j["pf"] < JOURNAL_PF_WARN:
            warn("G30① 交易复盘：整体 PF=%.2f<1（成本后期望为负，期望 %s元/笔、胜率%s），先控规模再谈优化。"
                 % (j["pf"], _num(j.get("expectancy"), 0), _pct(j.get("win_rate"), 1)))
        for b in j.get("weak_hold", [])[:2]:
            warn("G30① 持仓弱势桶「%s」：%d笔 PF=%.2f 净%s——极短噪声单失血线索，改离场规则须另开轮次双样本。"
                 % (b["key"], b["n"], b["pf"], _num(b.get("net"), 0)))
        for b in j.get("weak_score", [])[:1]:
            info("G30① 信号弱势档「%s」：%d笔 PF=%.2f，弱信号可考虑提高入场门槛（须影子验证）。"
                 % (b["key"], b["n"], b["pf"]))
        if j.get("green_ratio") is not None and j["green_ratio"] >= 0.5:
            info("G30① %s 的亏损单盘中曾浮盈>0.1%%（%s/%s）：止盈/移动止损纪律线索，只出证据不改参。"
                 % (_pct(j["green_ratio"], 1), j.get("loss_once_green"), j.get("n_loss")))

    # 4) 组合账户风险
    eq = bundle.get("equity") or {}
    if eq:
        if eq.get("max_drawdown", 0) >= EQUITY_DD_WARN:
            warn("组合回测最大回撤 %s 超 %.0f%%（期末风险度 %s、持仓 %s 个），检查敞口与强平约束。"
                 % (_pct(eq.get("max_drawdown"), 1), EQUITY_DD_WARN * 100,
                    _pct(eq.get("risk"), 1), eq.get("npos")))
        elif eq.get("risk", 0) >= EQUITY_RISK_WARN:
            warn("组合期末风险度 %s 偏高（保证金占用/权益），注意追保与强平。" % _pct(eq.get("risk"), 1))

    # 5) 防过拟合
    v = bundle.get("validation") or {}
    if v and v.get("dsr") is not None and v["dsr"] < DSR_PASS:
        info("WP-F4 防过拟合：组合 DSR=%s<%.2f（%s，试了%s组参数），当前参数优势不能排除多重试验偶然性，勿据此加仓。"
             % (_num(v.get("dsr"), 4), DSR_PASS, v.get("verdict", ""), v.get("n_trials")))

    # 6) 归因
    at = bundle.get("attribution") or {}
    if at and at.get("alpha") is not None and at.get("n"):
        if at["alpha"] < -1e-3:
            info("G28 归因：%s分钟周期 alpha=%s/根、R²=%s（n=%s），因子化残差偏负，结合体检看是否短周期失效。"
                 % (at["horizon"], _num(at.get("alpha"), 5), _pct(at.get("r2"), 1), at.get("n")))

    # 7) 组合构建器：ERC 是否显著优于等权（决策素材，不自动改 sizing）
    lab = bundle.get("lab") or {}
    mm = lab.get("methods") or {}
    if "equal" in mm and "erc" in mm:
        es, er = mm["equal"], mm["erc"]
        if es.get("sharpe") is not None and er.get("sharpe") is not None and er["sharpe"] - es["sharpe"] > 0.08:
            info("G26 组合实验台：ERC 滚动夏普 %.2f 高于等权 %.2f、回撤 %s vs %s——是否启用 --risk-sizing erc 的决策素材（默认仍关闭）。"
                 % (er["sharpe"], es["sharpe"], _pct(er.get("maxdd"), 1), _pct(es.get("maxdd"), 1)))

    if not acts:
        acts.append(("OK", "各研究侧产物齐全新鲜，无失效预警、无弱势桶/风险/过拟合提示。"))
    order = {"WARN": 0, "INFO": 1, "OK": 2}
    acts.sort(key=lambda x: order[x[0]])
    return acts


# =========================== 格式化小工具 ===========================
def _num(x, nd=2):
    if x is None:
        return "—"
    try:
        return "{:,.{n}f}".format(float(x), n=nd)
    except (TypeError, ValueError):
        return "—"


def _pct(x, nd=1, signed=False):
    if x is None:
        return "—"
    try:
        v = float(x) * 100.0
        return ("{:+,.{n}f}%".format(v, n=nd)) if signed else ("{:,.{n}f}%".format(v, n=nd))
    except (TypeError, ValueError):
        return "—"


def _state_cn(st):
    return {"ok": "新鲜", "stale": "陈旧", "missing": "缺失", "broken": "损坏"}.get(st, st)


# =========================== 报告成稿 ===========================
def collect(reports_dir=_REPORTS, now=None, stale_hours=STALE_HOURS_DEFAULT):
    """装载全部 sidecar 并提取，返 (bundle, freshness)。任何单项失败不影响其它。"""
    now = now or _now()
    freshness = {}
    raw = {}
    mtimes = {}
    for name, _lab, _cmd in SOURCES:
        path = os.path.join(reports_dir, name)
        if name.endswith(".csv"):
            mtime = _dt.datetime.fromtimestamp(os.path.getmtime(path)) if os.path.exists(path) else None
            raw[name] = None
        elif name.endswith(".txt"):
            mtime = _dt.datetime.fromtimestamp(os.path.getmtime(path)) if os.path.exists(path) else None
            raw[name] = None
        else:
            obj, mtime = load_sidecar(path, now=now)
            raw[name] = obj
            if obj is None and mtime is not None:
                state = "broken"          # 文件在但 JSON 解析失败
            else:
                state = freshness_state(mtime, now=now, stale_hours=stale_hours)
            freshness[name] = {"state": state, "mtime": mtime.strftime("%Y-%m-%d %H:%M") if mtime else "—",
                               "age": age_label(mtime, now)}
            continue
        state = freshness_state(mtime, now=now, stale_hours=stale_hours)
        freshness[name] = {"state": state, "mtime": mtime.strftime("%Y-%m-%d %H:%M") if mtime else "—",
                           "age": age_label(mtime, now)}

    bundle = {}
    if raw.get("factor_health.json") is not None:
        bundle["factor_health"] = sec_factor_health(raw["factor_health.json"])
    if raw.get("attribution.json") is not None:
        bundle["attribution"] = sec_attribution(raw["attribution.json"], "30")
    if raw.get("trade_journal.json") is not None:
        bundle["journal"] = sec_journal(raw["trade_journal.json"])
    if raw.get("portfolio_lab.json") is not None:
        bundle["lab"] = sec_lab(raw["portfolio_lab.json"])
    if raw.get("backtest_validation.json") is not None:
        bundle["validation"] = sec_validation(raw["backtest_validation.json"])
    eq = load_equity_summary(os.path.join(reports_dir, "portfolio_equity.csv"))
    if eq:
        bundle["equity"] = eq
    sig = load_signal_tracking(os.path.join(reports_dir, "signal_tracking.txt"))
    if sig:
        bundle["signals"] = sig
    return bundle, freshness


def build_report(bundle, freshness, now=None, stale_hours=STALE_HOURS_DEFAULT, reports_dir=_REPORTS):
    now = now or _now()
    L = []
    L.append(SEP)
    L.append("研究侧一键复盘 Research Review（G30③，聚合各研究工具已落盘 sidecar，只读、不重跑、不改主链）")
    L.append("生成时间 %s ｜ 陈旧阈值 %d 小时 ｜ 产物目录 %s" % (now.strftime("%Y-%m-%d %H:%M:%S"), stale_hours, reports_dir))
    L.append(SEP)

    # 0) 数据源新鲜度总表
    L.append("〇、数据源新鲜度（缺失项可按末尾/待办中的命令补齐；编排器不替你重跑）")
    L.append(SUB)
    for name, lab, _cmd in SOURCES:
        st = freshness.get(name, {}).get("state", "missing")
        mt = freshness.get(name, {}).get("mtime", "—")
        ag = freshness.get(name, {}).get("age", "—")
        L.append("  [%-2s] %-26s %-34s 生成 %s（%s）" % (_state_cn(st), name, lab, mt, ag))
    L.append("")

    # 1) 信号命中（主链 signal_tracking）
    L.append("一、信号命中（主链最近7天信号效果追踪，方向收益=信号方向×后续涨跌）")
    L.append(SUB)
    sig = bundle.get("signals") or []
    if sig:
        for r in sig:
            L.append("  %-8s 样本%-5d 胜率%s  平均方向收益%s" %
                     (r["period"], r["n"], _pct(r["win_rate"], 1), _pct(r["avg_dir_ret"], 2, signed=True)))
    else:
        L.append("  （无 signal_tracking.txt，启动 main 常驻监控后自动生成；本节安全跳过）")
    L.append("")

    # 2) 因子体检 G29
    L.append("二、因子表现 G29（事件因子 30 分钟周期 IC/裁决；日频因子 5 日 RankIC）")
    L.append(SUB)
    fh = bundle.get("factor_health") or {}
    if fh:
        rows = fh.get("event_rows", [])
        if rows:
            rows = sorted(rows, key=lambda r: (r["ic"] if r["ic"] is not None else 0.0))
            for r in rows:
                mark = "  <==" if ("失效" in (r["verdict"] or "") or "预警" in (r["verdict"] or "")) else ""
                L.append("  %-10s IC=%s  n=%-4d 最长连失%s 裁决：%s%s" %
                         (r["factor"], _pct(r["ic"], 3, signed=True), r["n"] or 0,
                          r["max_consec_fail"], r["verdict"], mark))
        dic = fh.get("daily_ic", [])
        if dic:
            bottom = ", ".join("%s=%s" % (r["factor"], _pct(r["ic"], 3, signed=True)) for r in dic[:3])
            top = ", ".join("%s=%s" % (r["factor"], _pct(r["ic"], 3, signed=True)) for r in list(reversed(dic[-3:])))
            L.append("  日频5日RankIC 最弱：%s" % bottom)
            L.append("  日频5日RankIC 最强：%s" % top)
        hl = [h for h in fh.get("halflife", []) if h["half_life"]]
        if hl:
            L.append("  IC半衰期(根)：" + "，".join("%s=%.0f" % (h["factor"], h["half_life"]) for h in hl[:6]))
    else:
        L.append("  （无 factor_health.json，运行 tools/factor_health.py 后补齐）")
    L.append("")

    # 3) 交易归因 G28
    L.append("三、交易归因 G28（30分钟周期 OLS 因子贡献 + 板块 BHB 配置/选股效应）")
    L.append(SUB)
    at = bundle.get("attribution") or {}
    if at:
        L.append("  n=%s alpha=%s/根 R²=%s（加法归因闭合，残差~0）" %
                 (at.get("n"), _num(at.get("alpha"), 5), _pct(at.get("r2"), 1)))
        fb = at.get("factor_bottom", [])
        ft = at.get("factor_top", [])
        if fb:
            L.append("  贡献最负因子：" + "，".join("%s=%s" % (x.get("factor"), _num(x.get("contrib"), 5)) for x in fb))
        if ft:
            L.append("  贡献最正因子：" + "，".join("%s=%s" % (x.get("factor"), _num(x.get("contrib"), 5)) for x in ft))
        sb = at.get("sector_bottom", [])
        stp = at.get("sector_top", [])
        if sb:
            L.append("  板块效应最负：" + "，".join("%s=%s" % (x.get("sector"), _num(x.get("effect"), 5)) for x in sb))
        if stp:
            L.append("  板块效应最正：" + "，".join("%s=%s" % (x.get("sector"), _num(x.get("effect"), 5)) for x in stp))
    else:
        L.append("  （无 attribution.json，运行 tools/attribution.py 后补齐）")
    L.append("")

    # 4) 交易复盘 journal G30①
    L.append("四、交易复盘 G30①（组合回测成交分桶，成本后）")
    L.append(SUB)
    j = bundle.get("journal") or {}
    if j:
        L.append("  %s笔 胜率%s 盈亏比%s PF=%s 期望%s元/笔 最长连胜%s/连亏%s" %
                 (j.get("n_trades"), _pct(j.get("win_rate"), 1), _num(j.get("payoff"), 2),
                  _num(j.get("pf"), 2), _num(j.get("expectancy"), 0),
                  j.get("max_win_streak"), j.get("max_loss_streak")))
        for b in j.get("weak_hold", [])[:3]:
            L.append("  弱势持仓桶「%s」：%d笔 PF=%.2f 净%s" % (b["key"], b["n"], b["pf"], _num(b.get("net"), 0)))
        if j.get("green_ratio") is not None:
            L.append("  亏损单盘中曾浮盈>0.1%%比例：%s（%s/%s）" %
                     (_pct(j["green_ratio"], 1), j.get("loss_once_green"), j.get("n_loss")))
    else:
        L.append("  （无 trade_journal.json，先跑 portfolio.py 再 tools/trade_journal.py --bars）")
    L.append("")

    # 5) 组合与风险 G3/G5
    L.append("五、组合与风险 G3/G5（回测权益曲线 + G26 四方法滚动样本外对比）")
    L.append(SUB)
    eq = bundle.get("equity") or {}
    if eq:
        L.append("  权益 %s→%s（区间收益%s）逐bar%d行（%s~%s）；最大回撤%s；期末风险度%s、保证金%s、持仓%d个" %
                 (_num(eq.get("start_equity"), 0), _num(eq.get("equity"), 0), _pct(eq.get("ret"), 1, signed=True),
                  eq.get("n_bars", 0), eq.get("start_dt", ""), eq.get("end_dt", ""),
                  _pct(eq.get("max_drawdown"), 1), _pct(eq.get("risk"), 1),
                  _num(eq.get("margin"), 0), eq.get("npos", 0)))
    else:
        L.append("  （无 portfolio_equity.csv，运行 portfolio.py --all --period 30 后补齐）")
    lab = bundle.get("lab") or {}
    mm = lab.get("methods") or {}
    if mm:
        L.append("  G26 滚动样本外（%d日/%d品种）：" % (lab.get("n_days") or 0, lab.get("n_universe") or 0))
        for m in ("equal", "inv_vol", "erc", "gmv"):
            st = mm.get(m)
            if st:
                L.append("    %-8s 年化%s 波动%s 夏普%s 回撤%s 年换手%s" %
                         (m, _pct(st.get("ann_ret"), 1, signed=True), _pct(st.get("ann_vol"), 1),
                          _num(st.get("sharpe"), 2), _pct(st.get("maxdd"), 1), _pct(st.get("ann_turnover"), 2)))
    L.append("")

    # 6) 防过拟合 WP-F4
    L.append("六、防过拟合 WP-F4（组合 DSR/多重试验校正 + 参数网格 PBO）")
    L.append(SUB)
    v = bundle.get("validation") or {}
    if v:
        L.append("  组合 %s天 观测夏普%s、期望最大阈值SR0=%s、DSR=%s（试%s组）：%s" %
                 (v.get("n_days"), _num(v.get("sr_obs"), 3), _num(v.get("sr0"), 3),
                  _num(v.get("dsr"), 4), v.get("n_trials"), v.get("verdict", "")))
        if v.get("grid_n"):
            L.append("  参数网格 %s 个品种：PBO良好 %s、样本外为正 %s、全亏 %s" %
                     (v.get("grid_n"), v.get("pbo_good"), v.get("oos_pos"), v.get("all_loss")))
    else:
        L.append("  （无 backtest_validation.json，运行 tools/backtest_validation.py 后补齐）")
    L.append("")

    # 7) 规则化待办
    acts = build_actions(bundle, freshness, now=now)
    L.append("七、规则化待办（WARN 优先；仅提示与决策素材，不自动改参/改主链）")
    L.append(SUB)
    for i, (lv, t) in enumerate(acts, 1):
        L.append("  %2d. [%-4s] %s" % (i, lv, t))
    L.append("")
    L.append(SEP)
    L.append("说明：本简报由各 sidecar 聚合而成，分钟回测为 bar 内规则假设成交、非真实队列（总纲不做清单1）；")
    L.append("      待办只指出关注点，任何策略/参数变更须另开轮次走时间双样本+事件层互证+默认回退。")
    L.append(SEP)
    return "\n".join(L)


def build_json_payload(bundle, freshness, now=None, stale_hours=STALE_HOURS_DEFAULT):
    now = now or _now()
    actions = build_actions(bundle, freshness, now=now)
    payload = {
        "generated": now.strftime("%Y-%m-%d %H:%M:%S"),
        "stale_hours": stale_hours,
        "freshness": freshness,
        "bundle": bundle,
        "actions": [{"level": lv, "text": t} for lv, t in actions],
    }
    json.dumps(payload, ensure_ascii=False, allow_nan=False)   # 预检：不得含 NaN
    return payload


# =========================== CLI ===========================
def run(argv=None):
    ap = argparse.ArgumentParser(description="G30③ 研究侧一键复盘编排器（聚合 reports 下各 sidecar，只读）")
    ap.add_argument("--reports-dir", default=_REPORTS, help="研究产物目录，默认 reports/")
    ap.add_argument("--stale-hours", type=float, default=STALE_HOURS_DEFAULT, dest="stale_hours",
                    help="sidecar 陈旧阈值小时数，默认168(7天)")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--json-out", default=DEFAULT_JSON, dest="json_out")
    args = ap.parse_args(argv)

    bundle, freshness = collect(args.reports_dir, stale_hours=args.stale_hours)
    report = build_report(bundle, freshness, stale_hours=args.stale_hours, reports_dir=args.reports_dir)
    if args.out:
        od = os.path.dirname(os.path.abspath(args.out))
        if od and not os.path.isdir(od):
            os.makedirs(od, exist_ok=True)
        with io.open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(report)
    if args.json_out:
        payload = build_json_payload(bundle, freshness, stale_hours=args.stale_hours)
        with io.open(args.json_out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1, allow_nan=False)
    else:
        payload = build_json_payload(bundle, freshness, stale_hours=args.stale_hours)
    # G27① 统一实验台账（惰性导入：保持本模块模块级"不 import 任何项目模块"的纪律；旁路失败不影响成稿）
    try:
        if _ROOT not in sys.path:      # 本工具按纪律不把项目根放 sys.path，惰性导入前补一次
            sys.path.insert(0, _ROOT)
        import experiment_ledger as el
        state_count = {"ok": 0, "stale": 0, "missing": 0}
        for _name, fr in freshness.items():
            st = fr.get("state") if isinstance(fr, dict) else None
            if st in state_count:
                state_count[st] += 1
        act_count = {}
        for a in payload.get("actions", []):
            act_count[a.get("level")] = act_count.get(a.get("level"), 0) + 1
        src_paths = [os.path.join(args.reports_dir, name) for name, _l, _c in SOURCES]
        el.safe_record(
            "research_review",
            {"stale_hours": args.stale_hours, "reports_dir": os.path.basename(os.path.abspath(args.reports_dir))},
            {"sources": state_count, "actions": act_count},
            inputs=[p for p in src_paths if os.path.isfile(p)],
            artifacts=[p for p in (args.out, args.json_out) if p],
            conclusion="数据源 ok%d/陈旧%d/缺失%d；待办 %s"
                       % (state_count["ok"], state_count["stale"], state_count["missing"],
                          "/".join("%s%d" % (k, v) for k, v in sorted(act_count.items()))))
    except Exception:
        import traceback as _tb
        with io.open(os.path.join(_ROOT, "cache", "r44_rr_hook_err.txt"), "w", encoding="utf-8") as _ef:
            _ef.write(_tb.format_exc())
    print(report)
    return 0


# =========================== 零网络/零DB 合成断言 ===========================
def selftest():
    fixed = _dt.datetime(2026, 9, 3, 15, 0, 0)

    # 1) 装载安全：不存在/损坏
    assert load_sidecar("___nope__.json", now=fixed) == (None, None)
    assert freshness_state(None) == "missing"
    assert freshness_state(fixed - _dt.timedelta(hours=1), now=fixed) == "ok"
    assert freshness_state(fixed - _dt.timedelta(hours=200), now=fixed, stale_hours=168) == "stale"
    assert age_label(None) == "—"
    assert "小时前" in age_label(fixed - _dt.timedelta(hours=5), now=fixed)
    assert load_equity_summary("___nope__.csv") == {}
    assert load_signal_tracking("___nope__.txt") == []

    # 2) equity 汇总：手造三行，最大回撤取全表最大，期末取末行
    import tempfile
    tmp = tempfile.mkdtemp()
    eqp = os.path.join(tmp, "equity.csv")
    with io.open(eqp, "w", encoding="utf-8-sig", newline="") as f:  # 带 BOM，须用 utf-8-sig 读
        f.write("dt,static,float,equity,margin,available,risk,drawdown,npos\n")
        f.write("t1,1000000,0,1000000,0,1000000,0.0,0.0,0\n")
        f.write("t2,1000000,0,900000,500000,400000,0.5556,0.10,3\n")
        f.write("t3,1000000,0,950000,200000,750000,0.2105,0.05,2\n")
        f.write(",,,,,,,,\n")   # 末尾空字段记录不得覆盖末行
    eq = load_equity_summary(eqp)
    assert eq["n_bars"] == 3 and eq["npos"] == 2 and eq["equity"] == 950000
    assert eq["end_dt"] == "t3" and eq["start_equity"] == 1000000
    assert abs(eq["max_drawdown"] - 0.10) < 1e-9 and abs(eq["risk"] - 0.2105) < 1e-6
    assert abs(eq["ret"] - (-0.05)) < 1e-9

    # 3) signal_tracking 正则：中文周期+样本+胜率+方向收益
    sigp = os.path.join(tmp, "sig.txt")
    with io.open(sigp, "w", encoding="utf-8") as f:
        f.write(" 30分钟        样本464(过期40) 胜率49.3%   平均方向收益-0.01% 多头156/324\n")
        f.write("   无关行不匹配\n")
        f.write(" 2小时         样本441 胜率50.9%   平均方向收益+0.02%\n")
    sig = load_signal_tracking(sigp)
    assert len(sig) == 2 and sig[0]["period"] == "30分钟"
    assert abs(sig[0]["win_rate"] - 0.493) < 1e-9 and abs(sig[0]["avg_dir_ret"] + 0.0001) < 1e-12
    assert abs(sig[1]["avg_dir_ret"] - 0.0002) < 1e-12

    # 4) factor_health：一个失效预警、一个正常；daily IC 排序、halflife
    fh_obj = {
        "event": {"30": {
            "日线动量": {"n": 100, "ic": -0.05, "verdict": "失效预警", "max_consec_fail": 5, "frac_fail": 0.5},
            "量仓资金": {"n": 100, "ic": 0.08, "verdict": "有效", "max_consec_fail": 1, "frac_fail": 0.1},
        }},
        "daily": {"ret5": {"5": {"ic": -0.01, "n": 10}, "halflife": {"half_life": 41.0}},
                  "ret252": {"5": {"ic": 0.10, "n": 10}, "halflife": None}},
    }
    fh = sec_factor_health(fh_obj)
    assert len(fh["alerts"]) == 1 and fh["alerts"][0]["factor"] == "日线动量"
    assert fh["daily_ic"][0]["factor"] == "ret5" and fh["daily_ic"][-1]["factor"] == "ret252"
    assert len(fh["halflife"]) == 1 and fh["halflife"][0]["half_life"] == 41.0
    assert sec_factor_health(None) == {} and sec_factor_health({"event": {}})["alerts"] == []

    # 5) attribution：因子/板块按贡献排序，缺周期安全返 {}
    at_obj = {"horizons": {"30": {
        "n": 50, "alpha": -0.002, "r2": 0.05,
        "factors": [{"factor": "A", "contrib": 0.001}, {"factor": "B", "contrib": -0.003},
                    {"factor": "C", "contrib": 0.002}],
        "bhb_sectors": [{"sector": "黑色", "effect": -0.001}, {"sector": "农产品", "effect": 0.002}],
    }}}
    at = sec_attribution(at_obj, "30")
    assert at["factor_bottom"][0]["factor"] == "B" and at["factor_top"][0]["factor"] == "C"
    assert at["sector_bottom"][0]["sector"] == "黑色"
    assert sec_attribution(at_obj, "999") == {}

    # 6) journal：弱势桶识别（n 门槛 + PF 阈值）、由盈转亏比例
    j_obj = {"n_trades": 30, "overall": {"win_rate": 0.4, "profit_factor": 0.8, "payoff_ratio": 1.3,
             "expectancy": -20, "max_win_streak": 5, "max_loss_streak": 9},
             "by_hold_band": [{"key": "1极短(1-2)", "n": 20, "pf": 0.5, "net": -1000},
                              {"key": "3-6", "n": 20, "pf": 1.3, "net": 500},
                              {"key": "小样本", "n": 3, "pf": 0.2, "net": -5}],
             "by_score_band": [{"key": "弱", "n": 15, "pf": 0.6, "net": -300}],
             "excursion": {"loss_once_green": 8, "n_loss": 10}}
    j = sec_journal(j_obj)
    assert len(j["weak_hold"]) == 1 and j["weak_hold"][0]["key"].startswith("1极短")  # n=3 的被门槛挡掉
    assert len(j["weak_score"]) == 1 and abs(j["green_ratio"] - 0.8) < 1e-9
    assert sec_journal({}) == {}

    # 7) lab：四方法提取
    lab_obj = {"n_universe": 61, "n_days": 300,
               "rolling_stats": {"equal": {"sharpe": 0.42, "maxdd": 0.095, "ann_ret": 0.04, "ann_vol": 0.09, "ann_turnover": 0},
                                 "erc": {"sharpe": 0.55, "maxdd": 0.06, "ann_ret": 0.05, "ann_vol": 0.07, "ann_turnover": 0.3}},
               "snapshot": {"equal": {"eff_n": 61.0}, "erc": {"eff_n": 20.0}}}
    lb = sec_lab(lab_obj)
    assert abs(lb["methods"]["erc"]["sharpe"] - 0.55) < 1e-12 and lb["snapshot_eff_n"]["equal"] == 61.0

    # 8) validation
    v_obj = {"dsr": {"n_days": 87, "sr_obs": -0.17, "sr0": 0.2, "dsr": 0.0001, "verdict": "无法排除", "n_trials": 18},
             "grid": {"n": 2, "pbo_good": 1, "oos_pos": 0, "all_loss": 2}}
    vv = sec_validation(v_obj)
    assert abs(vv["dsr"] - 0.0001) < 1e-12 and vv["grid_n"] == 2

    # 9) build_actions：WARN 优先排序 + 各规则命中
    bundle = {"factor_health": fh, "journal": j, "equity": {"max_drawdown": 0.20, "risk": 0.3, "npos": 4},
              "validation": vv, "attribution": at, "lab": lb}
    fr = {name: {"state": "missing", "mtime": "—", "age": "—"} for name, _l, _c in SOURCES}
    fr["factor_health.json"] = {"state": "ok", "mtime": "x", "age": "1小时前"}
    acts = build_actions(bundle, fr, now=fixed)
    levels = [a[0] for a in acts]
    assert levels == sorted(levels, key={"WARN": 0, "INFO": 1, "OK": 2}.__getitem__)
    texts = " ".join(t for _l, t in acts)
    assert "失效预警" in texts and "PF=0.80" in texts and "持仓弱势桶" in texts
    assert "超 15%" in texts and "DSR=0.0001" in texts and "缺少研究产物" in texts
    assert "可选产物" in texts                       # expr_research 缺失降级为 INFO
    # 全 OK 路径
    ok_acts = build_actions({}, {name: {"state": "ok", "mtime": "x", "age": "1小时前"} for name, _l, _c in SOURCES}, now=fixed)
    assert ok_acts and ok_acts[0][0] == "OK"

    # 10) collect 对空目录安全降级 + build_report 不抛错且含七段标题
    bd = collect(tmp, now=fixed)
    assert isinstance(bd[0], dict) and isinstance(bd[1], dict)
    empty_bundle, empty_fr = {}, {name: {"state": "missing", "mtime": "—", "age": "—"} for name, _l, _c in SOURCES}
    rep = build_report(empty_bundle, empty_fr, now=fixed, reports_dir=tmp)
    for h in ("一、信号命中", "二、因子表现", "三、交易归因", "四、交易复盘", "五、组合与风险", "六、防过拟合", "七、规则化待办"):
        assert h in rep
    payload = build_json_payload(empty_bundle, empty_fr, now=fixed)
    assert payload["actions"] and "freshness" in payload

    # 11) 数值格式化
    assert _num(None) == "—" and _pct(0.1234, 1) == "12.3%" and _pct(-0.05, 1, signed=True) == "-5.0%"
    assert _state_cn("stale") == "陈旧"

    print("research_review selftest OK（11 组）")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 1:
        raise SystemExit(selftest())
    raise SystemExit(run())
