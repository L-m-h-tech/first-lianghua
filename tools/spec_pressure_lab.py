# -*- coding: utf-8 -*-
"""G24续（第57轮）投机/套保压力代理实验室 spec_pressure_lab —— 纯离线、只读、只监控、不进综合分。

背景与诚实边界（务必先读）：
- 真正的套保压力 HP / 投机压力 SP 需要**分类持仓**（交易所会员龙虎榜多空、CFTC COT 商业/非商业口径），
  本项目当前只采到全合约总持仓（research_panel.oi 为主力合约持仓、ckline.p 为各合约持仓），**没有多空/
  套保-投机分类**（G22 分类持仓缺口仍在）。因此本工具不冒充真 HP/SP，只给三个**行为代理**：
    1) 投机度 = 成交量 / 持仓量（turnover ratio）：日内换手相对沉淀仓位的倍数，文献常用的投机活跃度代理；
       值越高越偏短线投机博弈、越低越偏持仓沉淀（套保/配置占比相对高的必要非充分条件）。
    2) 量仓四象限：近 N 日 收益方向 × 持仓变化方向 → 增仓上行(多头主动)/增仓下行(空头主动)/
       减仓上行(空头回补)/减仓下行(多头离场)，是经典期货微观结构读法，仍不区分交易者身份。
    3) 近月持仓集中度（来自 ckline 各合约持仓）：主力合约持仓占全品种比例 + 活跃合约数；主力占比异常低
       = 换月/持仓向后迁移（roll pressure 代理），同样不是分类持仓。
- 全部统计用 PIT 尾窗（默认120交易日，最少40点），不使用未来数据；只写 reports/，只读 cache/*.db，零网络。

用法：
    python tools/spec_pressure_lab.py              # 读 cache 两库，出 reports/spec_pressure_lab.txt|.json
    python tools/spec_pressure_lab.py --selftest   # 零网络/零DB 合成自测
"""
import argparse
import json
import math
import os
import statistics
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import panel_builder as pb       # noqa: E402  G21 面板
import experiment_ledger as el   # noqa: E402  旁路台账

PANEL_DB = os.path.join(_ROOT, "cache", "research_panel.db")
TERM_DB = os.path.join(_ROOT, "cache", "term_history.db")
LAB_TXT = os.path.join(_ROOT, "reports", "spec_pressure_lab.txt")
LAB_JSON = os.path.join(_ROOT, "reports", "spec_pressure_lab.json")

Z_WIN = 120          # 投机度尾窗 z/分位（交易日，约半年）
Z_MIN = 40           # 尾窗最少有效点
CHG_WIN = 5          # 量仓四象限用近 N 日收益/持仓变化
HOT_Z = 1.5          # 投机度 |z| 异常阈值
CONC_DAYS = 200      # 近月集中度只取最近 N 个交易日（控制读取量，足够120尾窗）
CONC_LOW_PCT = 0.10  # 主力占比低于自身尾窗该分位 = 换月/持仓向后迁移


def _isnum(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# =========================== 纯函数 ===========================
def rolling_z(xs, win=Z_WIN, min_n=Z_MIN):
    """尾窗 z：z[t]=(x[t]-尾窗均值)/尾窗总体std；不足/零方差为 None。不改入参。"""
    out = [None] * len(xs)
    for t in range(len(xs)):
        lo = max(0, t - win + 1)
        w = [x for x in xs[lo:t + 1] if _isnum(x)]
        if len(w) < min_n:
            continue
        m = sum(w) / len(w)
        var = sum((x - m) ** 2 for x in w) / len(w)
        if var <= 1e-18:
            continue
        if _isnum(xs[t]):
            out[t] = (xs[t] - m) / math.sqrt(var)
    return out


def rolling_percentile(xs, win=Z_WIN, min_n=Z_MIN):
    """尾窗经验分位（0~1）；不足为 None。"""
    out = [None] * len(xs)
    for t in range(len(xs)):
        lo = max(0, t - win + 1)
        w = [x for x in xs[lo:t + 1] if _isnum(x)]
        if len(w) < min_n or not _isnum(xs[t]):
            continue
        out[t] = sum(1 for x in w if x <= xs[t]) / len(w)
    return out


def turnover_series(vols, ois):
    """投机度=成交量/持仓量；任一缺失或持仓非正为 None。"""
    out = []
    for v, p in zip(vols, ois):
        out.append(v / p if _isnum(v) and _isnum(p) and p > 0 and v >= 0 else None)
    return out


def quadrant(ret, doi_pct, eps=1e-12):
    """量仓四象限：(近窗收益, 近窗持仓变化比例) -> 中文状态；任一非数值为 None。

    增仓=doi_pct≥0；收益正负以 eps 为界。经典读法（不区分交易者身份，仅行为代理）。
    """
    if not (_isnum(ret) and _isnum(doi_pct)):
        return None
    up = ret > eps
    add = doi_pct >= 0
    if add and up:
        return "增仓上行(多头主动)"
    if add and not up:
        return "增仓下行(空头主动)"
    if (not add) and up:
        return "减仓上行(空头回补)"
    return "减仓下行(多头离场)"


def _pct_change(xs, n):
    """近 n 步变化率 = xs[-1]/xs[-1-n]-1；端点缺失/非正为 None。"""
    if len(xs) <= n:
        return None
    a, b = xs[-1 - n], xs[-1]
    if _isnum(a) and _isnum(b) and a > 0:
        return b / a - 1.0
    return None


def symbol_stat(dates, close, vol, oi, win=Z_WIN, min_n=Z_MIN, chg_win=CHG_WIN):
    """单品种：投机度最新值/尾窗z/分位/均值 + 近 chg_win 日收益、持仓变化与四象限。纯函数。"""
    if not dates:
        return None
    to = turnover_series(vol, oi)
    z = rolling_z(to, win, min_n)
    pc = rolling_percentile(to, win, min_n)
    ti = None
    for t in range(len(to) - 1, -1, -1):
        if _isnum(to[t]):
            ti = t
            break
    if ti is None:
        return None
    valid_to = [x for x in to if _isnum(x)]
    ret = _pct_change(close, chg_win)
    doi = _pct_change(oi, chg_win)
    return {
        "date": dates[ti], "turnover": to[ti], "turn_z": z[ti], "turn_pctile": pc[ti],
        "turn_mean": (sum(valid_to) / len(valid_to)) if valid_to else None,
        "n_valid": len(valid_to), "ret%d" % chg_win: ret, "oi_chg%d" % chg_win: doi,
        "quadrant": quadrant(ret, doi)}


def concentration_series(by_date):
    """{date: [各合约持仓p,...]} 按日期升序 -> (dates, 主力占比series, 活跃合约数series, HHI series)。"""
    dates = sorted(by_date)
    share, nact, hhi = [], [], []
    for d in dates:
        ps = [p for p in by_date[d] if _isnum(p) and p > 0]
        tot = sum(ps)
        if tot <= 0:
            share.append(None); nact.append(None); hhi.append(None); continue
        sh = max(ps) / tot
        share.append(sh); nact.append(len(ps)); hhi.append(sum((p / tot) ** 2 for p in ps))
    return dates, share, nact, hhi


def concentration_stat(by_date, win=Z_WIN, min_n=Z_MIN, low_pctile=0.10):
    """近月持仓集中度：最新主力占比/活跃合约数/HHI + 主力占比尾窗分位。

    换月/分散信号**只看自身尾窗分位**（当前主力占比低于自身近120日 10% 分位），不用绝对阈值——
    因不同品种挂牌合约数差异极大（有色常态挂10+合约、主力占比天然仅40%），绝对阈值会系统性误报。
    """
    dates, share, nact, hhi = concentration_series(by_date)
    if not dates:
        return None
    pc = rolling_percentile(share, win, min_n)
    ti = None
    for t in range(len(share) - 1, -1, -1):
        if _isnum(share[t]):
            ti = t
            break
    if ti is None:
        return None
    rolling = _isnum(pc[ti]) and pc[ti] < low_pctile
    return {"date": dates[ti], "main_share": share[ti], "n_active": nact[ti], "hhi": hhi[ti],
            "share_pctile": pc[ti], "rolling": bool(rolling)}


# =========================== 渲染 ===========================
def _fmt(x, nd=2, pct=False, signed=False):
    if not _isnum(x):
        return "—"
    if pct:
        return ("%+.*f%%" % (nd, x * 100)) if signed else ("%.*f%%" % (nd, x * 100))
    return ("%+.*f" % (nd, x)) if signed else ("%.*f" % (nd, x))


def render(meta, sym_rows, quad_dist, conc_rows):
    L = []
    add = L.append
    add("G24续 投机/套保压力代理 spec_pressure_lab（纯离线只读 research_panel+term_history，只监控不下单、不进综合分）")
    add("=" * 100)
    add("窗口 %s ~ %s；投机度尾窗%d日(最少%d)、量仓四象限近%d日；覆盖 %d 品种。"
        % (meta.get("panel_d0"), meta.get("panel_d1"), meta["z_win"], meta["z_min"],
           meta["chg_win"], meta["n_sym"]))
    add("诚实边界：无多空/套保-投机分类持仓(G22缺口)，下列为成交活跃度/量仓行为**代理**，非真HP/SP。")
    add("")
    add("【一】投机度（成交量/持仓量）总览")
    add("  全市场投机度z中位数=%s；异常活跃(|z|≥%.1f) %d 只、异常清淡 %d 只。"
        % (_fmt(meta.get("med_z"), 2), HOT_Z, meta["n_hot"], meta["n_cold"]))
    hot = [r for r in sym_rows if _isnum(r.get("turn_z"))]
    hot.sort(key=lambda r: (r["turn_z"] if _isnum(r.get("turn_z")) else 0), reverse=True)
    add("  投机度最活跃 Top12（z降序；倍数=当前成交/持仓）：")
    add("  %-6s %-8s %8s %7s %7s %8s  %s" % ("品种", "板块", "投机度", "z", "分位", "均值", "量仓四象限"))
    for r in hot[:12]:
        add("  %-6s %-8s %8s %7s %7s %8s  %s" % (
            r["sym"], (r.get("sector") or "未知")[:8], _fmt(r.get("turnover"), 2),
            _fmt(r.get("turn_z"), 2, signed=True),
            _fmt(r.get("turn_pctile"), 2, pct=True), _fmt(r.get("turn_mean"), 2),
            r.get("quadrant") or "—"))
    cold = [r for r in hot if _isnum(r.get("turn_z"))]
    cold.sort(key=lambda r: r["turn_z"])
    add("  投机度最清淡 Bottom8（z升序）：")
    for r in cold[:8]:
        add("  %-6s %-8s %8s %7s %7s  %s" % (
            r["sym"], (r.get("sector") or "未知")[:8], _fmt(r.get("turnover"), 2),
            _fmt(r.get("turn_z"), 2, signed=True), _fmt(r.get("turn_pctile"), 2, pct=True),
            r.get("quadrant") or "—"))
    add("")
    add("【二】量仓四象限分布（近%d日 收益方向×持仓变化）" % meta["chg_win"])
    tot = sum(quad_dist.values()) or 1
    for q in ("增仓上行(多头主动)", "增仓下行(空头主动)", "减仓上行(空头回补)", "减仓下行(多头离场)"):
        n = quad_dist.get(q, 0)
        add("  %-18s %3d 只  %5.1f%%" % (q, n, 100.0 * n / tot))
    add("")
    add("【三】近月持仓集中度 / 换月压力（ckline 各合约持仓，覆盖 %d 品种）" % meta.get("conc_n", 0))
    rolling = [r for r in conc_rows if r.get("rolling")]
    add("  主力占比处自身尾窗极低分位(<%.0f%%，换月/持仓向后迁移) %d 只：%s"
        % (CONC_LOW_PCT * 100, len(rolling),
           "、".join("%s(%.0f%%)" % (r["sym"], 100 * r["main_share"]) for r in rolling[:14]) or "无"))
    add("  %-6s %9s %8s %8s %7s  %s" % ("品种", "主力占比", "活跃合约", "HHI", "占比分位", "状态"))
    for r in sorted(conc_rows, key=lambda x: x.get("main_share") or 1)[:12]:
        add("  %-6s %9s %8s %8s %7s  %s" % (
            r["sym"], _fmt(100 * r["main_share"], 1) + "%", r.get("n_active"),
            _fmt(r.get("hhi"), 3), _fmt(r.get("share_pctile"), 2, pct=True),
            "换月/分散" if r.get("rolling") else "正常"))
    add("")
    add("【四】诚实边界 / 注意")
    add("  1) 投机度高只代表换手活跃，可能由流动性提升或事件驱动，不直接等价于价格方向性投机压力；")
    add("  2) 量仓四象限是总量行为读法，无法区分多空双方身份，真·多空/套保压力须 G22 分类持仓（会员/COT）；")
    add("  3) 不同品种换手中枢差异大（如黑色普遍高于农产品），跨品种比较以各自尾窗 z 为准而非原始倍数；")
    add("  4) 近月集中度受合约挂牌/到期节奏影响，换月信号应结合主力切换日历确认，避免误读为趋势性减仓。")
    return "\n".join(L)


# =========================== 数据读取与运行 ===========================
def _load_panel(panel_db):
    """面板 -> {sym: {'dates':[],'c':[],'v':[],'oi':[],'sector':str}}（按日期升序）。"""
    store = pb.PanelStore(panel_db)
    rows = store.load_all() if hasattr(store, "load_all") else _load_all(store, sorted(store.symbols()))
    if hasattr(store, "close"):
        store.close()
    out = {}
    for r in rows:
        s = r["sym"]
        b = out.setdefault(s, {"dates": [], "c": [], "v": [], "oi": [], "sector": r.get("sector") or "未知"})
        b["dates"].append(r["date"]); b["c"].append(r.get("c"))
        b["v"].append(r.get("v")); b["oi"].append(r.get("oi"))
        if r.get("sector"):
            b["sector"] = r["sector"]
    for b in out.values():
        order = sorted(range(len(b["dates"])), key=lambda i: b["dates"][i])
        for k in ("dates", "c", "v", "oi"):
            b[k] = [b[k][i] for i in order]
    return out


def _load_all(store, syms):
    out = []
    for s in syms:
        out.extend(store.load_rows(s))
    return out


def _load_concentration(term_db, conc_days=CONC_DAYS):
    """ckline 最近 conc_days 个交易日各合约持仓 -> {sym: {date:[p,...]}}。任何异常返回 {}。"""
    import sqlite3
    if not os.path.exists(term_db):
        return {}
    try:
        conn = sqlite3.connect(term_db)
        sql = ("SELECT sym,d,p FROM ckline WHERE d >= "
               "(SELECT MIN(d) FROM (SELECT DISTINCT d FROM ckline ORDER BY d DESC LIMIT ?))")
        by = {}
        for sym, d, p in conn.execute(sql, (conc_days,)):
            by.setdefault(sym, {}).setdefault(d, []).append(p)
        conn.close()
        return by
    except Exception:
        return {}


def run(panel_db=PANEL_DB, term_db=TERM_DB, txt_path=LAB_TXT, json_path=LAB_JSON, verbose=True):
    panel = _load_panel(panel_db)
    sym_rows, all_z = [], []
    quad_dist = {}
    pd0, pd1 = [], []
    for s, b in panel.items():
        st = symbol_stat(b["dates"], b["c"], b["v"], b["oi"])
        if not st:
            continue
        st["sym"] = s; st["sector"] = b["sector"]
        sym_rows.append(st)
        if _isnum(st.get("turn_z")):
            all_z.append(st["turn_z"])
        if st.get("quadrant"):
            quad_dist[st["quadrant"]] = quad_dist.get(st["quadrant"], 0) + 1
        pd0.append(b["dates"][0]); pd1.append(st["date"])
    n_hot = sum(1 for z in all_z if z >= HOT_Z)
    n_cold = sum(1 for z in all_z if z <= -HOT_Z)

    conc_all = _load_concentration(term_db)
    conc_rows = []
    for s, by_date in conc_all.items():
        cs = concentration_stat(by_date)
        if cs:
            cs["sym"] = s
            conc_rows.append(cs)

    meta = {"panel_d0": min(pd0) if pd0 else None, "panel_d1": max(pd1) if pd1 else None,
            "z_win": Z_WIN, "z_min": Z_MIN, "chg_win": CHG_WIN, "hot_z": HOT_Z,
            "n_sym": len(sym_rows), "med_z": statistics.median(all_z) if all_z else None,
            "n_hot": n_hot, "n_cold": n_cold, "conc_n": len(conc_rows)}
    text = render(meta, sym_rows, quad_dist, conc_rows)
    if verbose:
        print(text)
    os.makedirs(os.path.dirname(txt_path), exist_ok=True)
    with open(txt_path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(text + "\n")
    payload = {"meta": meta, "quadrant_dist": quad_dist,
               "symbols": sym_rows, "concentration": conc_rows}
    with open(json_path, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(payload, fp, ensure_ascii=False, allow_nan=False, indent=1)
    try:
        hot_names = [r["sym"] for r in sorted(
            (r for r in sym_rows if _isnum(r.get("turn_z"))),
            key=lambda r: r["turn_z"], reverse=True)[:12]]
        roll_names = [r["sym"] for r in conc_rows if r.get("rolling")]
        el.safe_record(
            "spec_pressure_lab",
            {"z_win": Z_WIN, "chg_win": CHG_WIN, "hot_z": HOT_Z,
             "panel_db": os.path.basename(panel_db), "term_db": os.path.basename(term_db)},
            {"n_sym": len(sym_rows), "med_z": meta["med_z"], "n_hot": n_hot, "n_cold": n_cold,
             "quadrant": quad_dist, "conc_n": len(conc_rows), "rolling": roll_names[:14],
             "hot_top": hot_names},
            inputs=[panel_db, term_db], artifacts=[txt_path, json_path],
            conclusion="G24续投机/套保压力代理：%d品种投机度z中位%.2f、异常活跃%d/清淡%d；四象限%s；近月集中度覆盖%d、换月分散%s；无分类持仓故仅行为代理"
                       % (len(sym_rows), meta["med_z"] or 0.0, n_hot, n_cold,
                          "/".join("%s%d" % (q[:2], quad_dist[q]) for q in sorted(quad_dist)),
                          len(conc_rows), ",".join(roll_names[:10]) or "无"))
    except Exception:
        pass
    return payload


# =========================== 零网络/零DB 合成断言 ===========================
def selftest():
    # 1) turnover：v/oi，持仓非正/缺失为 None
    to = turnover_series([10.0, 0.0, 5.0], [5.0, 0.0, None])
    assert abs(to[0] - 2.0) < 1e-12 and to[1] is None and to[2] is None
    # 2) rolling_z：平稳后突增 z 很大；常数零方差 None；样本不足 None
    z = rolling_z([1.0] * 50 + [3.0], 60, 20)
    assert z[-1] is not None and z[-1] > 4
    assert rolling_z([1.0] * 60, 60, 20)[-1] is None
    assert rolling_z([1.0, 2.0], 60, 20)[-1] is None
    # 3) rolling_percentile：单调序列末值分位=1
    pc = rolling_percentile([float(i) for i in range(60)], 60, 20)
    assert pc[-1] == 1.0
    # 4) quadrant 四象限 + 缺失
    assert quadrant(0.01, 0.02).startswith("增仓上行")
    assert quadrant(-0.01, 0.02).startswith("增仓下行")
    assert quadrant(0.01, -0.02).startswith("减仓上行")
    assert quadrant(-0.01, -0.02).startswith("减仓下行")
    assert quadrant(None, 0.1) is None
    # 5) _pct_change
    xs = [100.0, 105.0, 110.0]
    assert abs(_pct_change(xs, 2) - 0.10) < 1e-12 and _pct_change(xs, 5) is None
    # 6) symbol_stat：构造投机度从平稳到抬升，最新 z 为正、象限正确
    n = 80
    dates = ["2026-%02d-%02d" % (1 + i // 28, 1 + i % 28) for i in range(n)]
    close = [100.0 + i for i in range(n)]             # 缓涨
    vol = [1000.0] * (n - 5) + [3000.0] * 5           # 近5日放量
    oi = [2000.0 + 10 * i for i in range(n)]          # 持仓缓增
    st = symbol_stat(dates, close, vol, oi)
    assert st and st["turn_z"] is not None and st["turn_z"] > 1.0
    assert st["quadrant"].startswith("增仓上行")
    assert 0.0 <= (st["turn_pctile"] or 0) <= 1.0
    # 7) concentration：主力占比=最大/总和；换月分散按**自身尾窗分位**触发（键名零填充保证可排序）
    by = {"d%03d" % i: [90.0, 10.0] for i in range(60)}      # 主力常态90%
    cs = concentration_stat(by)
    assert abs(cs["main_share"] - 0.9) < 1e-12 and cs["n_active"] == 2 and not cs["rolling"]
    by2 = dict(by)
    by2["d060"] = [40.0, 35.0, 25.0]                        # 末日持仓分散→处自身极低分位
    cs2 = concentration_stat(by2)
    assert abs(cs2["main_share"] - 0.40) < 1e-12 and cs2["n_active"] == 3 and cs2["rolling"]
    # 常态就多合约分散（主力占比一直40%）不应误报换月
    by3 = {"d%03d" % i: [40.0, 35.0, 25.0] for i in range(60)}
    assert not concentration_stat(by3)["rolling"]
    # 8) 空输入健壮性
    assert symbol_stat([], [], [], []) is None
    assert concentration_stat({}) is None
    # 9) render 不抛异常且含四段
    meta = {"panel_d0": "a", "panel_d1": "b", "z_win": 120, "z_min": 40, "chg_win": 5,
            "n_sym": 1, "med_z": 0.2, "n_hot": 0, "n_cold": 0, "conc_n": 1}
    txt = render(meta, [{"sym": "RB", "sector": "黑色", "turnover": 0.6, "turn_z": 0.5,
                         "turn_pctile": 0.6, "turn_mean": 0.5, "quadrant": "增仓上行(多头主动)"}],
                 {"增仓上行(多头主动)": 1}, [{"sym": "RB", "main_share": 0.9, "n_active": 2,
                 "hhi": 0.82, "share_pctile": 0.5, "rolling": False}])
    for sec in ("【一】", "【二】", "【三】", "【四】"):
        assert sec in txt
    print("spec_pressure_lab selftest ALL PASS（9组）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="G24续 投机/套保压力代理实验室（纯离线只读）")
    ap.add_argument("--panel-db", default=PANEL_DB)
    ap.add_argument("--term-db", default=TERM_DB)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        selftest()
        return 0
    run(panel_db=args.panel_db, term_db=args.term_db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
