# -*- coding: utf-8 -*-
r"""G12（第55轮）产业链 / 跨期价差监控实验台 tools/spread_lab.py。

总纲 G12：**只监控、不下单、不进综合分、不接 main** 的价差/期限结构雷达。纯标准库、零网络、
只读两个既有离线库，负/异常结果诚实呈现：
  - **跨期（期限结构）**：读 cache/term_history.db（G22 逐合约日K底座，term_history 复用、不重写），
    用 term_history.build_term_series 重建每品种近月/次月/远月结算价，算近-次价差%、年化展期 carry、
    曲线 level/slope/curv、backwardation/contango 形态，并用**过去 Z_WIN 日 PIT 尾窗**给当前价差打
    z-score 与分位（回答"当前跨期价差处在自身历史什么位置"），再汇总全市场 backwardation 广度。
  - **产业链（跨品种相对价差）**：读 cache/research_panel.db（G21 面板）收盘价，对固定的产业链腿对
    算**无量纲比价 A/B**（不同合约单位不同，直接相减无经济意义，故用比价；同单位腿另注原始价差），
    同样给尾窗 z-score/分位/近60日变化，监控产业链利润的相对偏离。

**诚实数据缺口**：精确加工/压榨/焦化"利润"需要合约乘数、出成率、加工费等物理换算系数（如压榨利润
=0.18×豆粕+0.80×豆油−大豆−加工费），本工具不硬编这些易过期系数，只用稳健的无量纲比价做偏离监控，
精确产业利润留待补系数表后再做；跨期用日频结算价、未计手续费/滑点/保证金/换月；research 结论不交易。
"""
import argparse
import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import term_history as th        # noqa: E402  G22 期限结构底座（build_term_series/TermHistoryStore）
import panel_builder as pb       # noqa: E402  G21 面板
import experiment_ledger as el   # noqa: E402  旁路台账

TERM_DB = os.path.join(_ROOT, "cache", "term_history.db")
PANEL_DB = os.path.join(_ROOT, "cache", "research_panel.db")
LAB_TXT = os.path.join(_ROOT, "reports", "spread_lab.txt")
LAB_JSON = os.path.join(_ROOT, "reports", "spread_lab.json")

Z_WIN = 120          # 价差 z-score 尾窗（交易日，约半年）
Z_MIN = 40           # 尾窗最少有效点
CHG_WIN = 60         # 产业链比价近 N 日变化
# (板块, 中文名, 腿A, 腿B, 说明)；只用无量纲比价 A/B；两条腿都在面板里才启用
CHAINS = (
    ("黑色", "卷螺比价(热卷/螺纹)", "HC", "RB", "板材-长材强弱，同单位(元/吨)可另看 HC-RB"),
    ("黑色", "矿螺比价(铁矿/螺纹)", "I", "RB", "原料-成材，钢厂利润反向代理"),
    ("黑色", "焦化比价(焦炭/焦煤)", "J", "JM", "焦化环节利润代理"),
    ("油脂油料", "豆棕比价(豆油/棕榈)", "Y", "P", "油脂替代，同单位(元/吨)可另看 Y-P"),
    ("油脂油料", "豆菜粕比价(豆粕/菜粕)", "M", "RM", "蛋白粕替代"),
    ("油脂油料", "油粕比价(豆油/豆粕)", "Y", "M", "压榨出油-出粕相对价值"),
    ("能化", "醇碱比价(甲醇/纯碱)", "MA", "SA", "煤化工链相对强弱"),
    ("能化", "聚酯比价(PTA/乙二醇)", "TA", "EG", "聚酯链 PTA-MEG 相对成本"),
    ("能化", "聚烯烃比价(塑料/PP)", "L", "PP", "PE-PP 同链替代"),
    ("有色", "铜铝比价(铜/铝)", "CU", "AL", "工业金属强弱"),
    ("贵金属", "金银比价(金/银)", "AU", "AG", "避险-工业属性，经典宏观比价"),
)
# G12续（第56轮）：产业链**盘面虚拟利润额**（元/吨主产品）。这些腿报价单位都是元/吨，故只需投料/出成
# 系数（权重）与加工费，不需要合约乘数（乘数只在真实配手数时才用，本工具只监控不配仓）。
# (板块, 中文名, ((腿, 带符号权重),...), 加工费元/吨主产品, 口径说明)；权重正=产出、负=投料
MARGINS = (
    ("油脂油料", "盘面压榨利润(元/吨大豆)", (("M", 0.785), ("Y", 0.185), ("A", -1.0)), 110.0,
     "1吨大豆≈0.785吨豆粕+0.185吨豆油，减加工费约110元/吨；DCE一号A为食用国产豆，与进口大豆压榨口径有偏差"),
    ("黑色", "盘面焦化利润(元/吨焦炭)", (("J", 1.0), ("JM", -1.3)), 150.0,
     "约1.3吨焦煤炼1吨焦炭，减加工费约150元/吨（行业经验系数，随工艺/地区浮动）"),
    ("黑色", "虚拟钢厂毛利(元/吨螺纹)", (("RB", 1.0), ("I", -1.6), ("J", -0.5)), 0.0,
     "1吨螺纹≈1.6吨铁矿+0.5吨焦炭的虚拟盘面毛利；未计废钢/合金/能源/人工等其他制造成本，故不减固定费"),
    ("黑色", "卷螺差(元/吨)", (("HC", 1.0), ("RB", -1.0)), 0.0,
     "热卷-螺纹，板材对长材，同单位(元/吨)直接相减"),
    ("油脂油料", "豆棕价差(元/吨)", (("Y", 1.0), ("P", -1.0)), 0.0,
     "豆油-棕榈油，油脂替代，同单位(元/吨)直接相减"),
)


def _isnum(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# =========================== 纯函数：尾窗 z / 分位（PIT，只用尾窗） ===========================
def rolling_z(xs, win=Z_WIN, min_n=Z_MIN):
    """对齐序列的尾窗 z：z[t]=(x[t]-mean(尾窗))/std(尾窗，总体)；不足/零方差为 None。不改入参。"""
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
    """对齐序列的尾窗经验分位（0~1=当前值在尾窗中的排名位置）；不足为 None。"""
    out = [None] * len(xs)
    for t in range(len(xs)):
        lo = max(0, t - win + 1)
        w = [x for x in xs[lo:t + 1] if _isnum(x)]
        if len(w) < min_n or not _isnum(xs[t]):
            continue
        le = sum(1 for x in w if x <= xs[t])
        out[t] = le / len(w)
    return out


def spread_pct_series(term_series):
    """近月相对次月价差率 = near_s/next_s-1（>0=近高远低 backwardation 现货升水；<0=contango）。"""
    out = []
    for r in term_series:
        a, b = r.get("near_s"), r.get("next_s")
        out.append(a / b - 1.0 if _isnum(a) and _isnum(b) and b > 0 else None)
    return out


def _last_valid(xs):
    """返回最后一个非 None 的 (下标,值)，没有则 (None,None)。"""
    for t in range(len(xs) - 1, -1, -1):
        if _isnum(xs[t]):
            return t, xs[t]
    return None, None


def term_symbol_stat(term_series, win=Z_WIN, min_n=Z_MIN):
    """单品种期限结构监控：最新近/次月、价差%、年化carry、形态、价差与carry的尾窗z/分位。纯函数。"""
    if not term_series:
        return None
    sp = spread_pct_series(term_series)
    carry = [r.get("carry_nn") for r in term_series]
    zsp = rolling_z(sp, win, min_n)
    psp = rolling_percentile(sp, win, min_n)
    zcr = rolling_z(carry, win, min_n)
    ti, cur_sp = _last_valid(sp)
    if ti is None:
        return None
    row = term_series[ti]
    ci, cur_carry = _last_valid(carry)
    return {
        "date": row.get("date"), "near": row.get("near"), "next": row.get("next"),
        "near_s": row.get("near_s"), "next_s": row.get("next_s"),
        "spread_pct": cur_sp, "carry_ann": row.get("carry_nn"),
        "slope": row.get("slope"), "curve": "back" if (cur_sp or 0) > 0 else "contango",
        "spread_z": zsp[ti], "spread_pctile": psp[ti],
        "carry_z": zcr[ci] if ci is not None else None,
        "oi_sum": row.get("oi_sum"), "n_live": row.get("n_live"), "n_days": len(term_series)}


def aligned_ratio(dates_a, close_a, dates_b, close_b):
    """按公共交易日对齐两条腿收盘价，返回 (公共日期升序, 比价A/B)；任一条腿非正/缺失跳过。"""
    mb = dict(zip(dates_b, close_b))
    od, ratio = [], []
    for d, ca in zip(dates_a, close_a):
        cb = mb.get(d)
        if _isnum(ca) and _isnum(cb) and ca > 0 and cb > 0:
            od.append(d); ratio.append(ca / cb)
    return od, ratio


def chain_stat(dates, ratio, win=Z_WIN, min_n=Z_MIN, chg_win=CHG_WIN):
    """一条产业链比价的监控统计：最新比价、尾窗z/分位、近 chg_win 日变化、样本数。纯函数。"""
    if not ratio:
        return None
    z = rolling_z(ratio, win, min_n)
    pc = rolling_percentile(ratio, win, min_n)
    t = len(ratio) - 1
    chg = None
    if t - chg_win >= 0 and _isnum(ratio[t - chg_win]) and ratio[t - chg_win] != 0:
        chg = ratio[t] / ratio[t - chg_win] - 1.0
    return {"date": dates[t], "ratio": ratio[t], "z": z[t], "pctile": pc[t],
            "chg60": chg, "n": len(ratio)}


def aligned_margin(close_map, weights, fee=0.0):
    """按公共交易日算盘面虚拟利润额：value[d]=Σ w_i*close_i[d]-fee；任一条腿缺失/非正跳过。

    weights=((sym, 带符号权重),...)，正=产出、负=投料。返回 (公共日期升序, 利润额序列，元/吨)。纯函数。
    """
    legs = [(s, w, close_map.get(s)) for s, w in weights]
    if any(m is None for _, _, m in legs):
        return [], []
    common = set(legs[0][2].keys())
    for _, _, m in legs[1:]:
        common &= set(m.keys())
    od, vals = [], []
    for d in sorted(common):
        px = [m.get(d) for _, _, m in legs]
        if all(_isnum(x) and x > 0 for x in px):
            v = sum(w * x for (_, w, _), x in zip(legs, px)) - (fee or 0.0)
            od.append(d); vals.append(v)
    return od, vals


def margin_stat(dates, values, win=Z_WIN, min_n=Z_MIN, chg_win=CHG_WIN):
    """一条盘面利润额序列的监控统计：最新利润额、尾窗z/分位、近 chg_win 日绝对变化(元/吨)、样本数。纯函数。"""
    if not values:
        return None
    z = rolling_z(values, win, min_n)
    pc = rolling_percentile(values, win, min_n)
    t = len(values) - 1
    chg = values[t] - values[t - chg_win] if t - chg_win >= 0 and _isnum(values[t - chg_win]) else None
    return {"date": dates[t], "value": values[t], "z": z[t], "pctile": pc[t],
            "chg60": chg, "n": len(values)}


# =========================== 报告 / 运行 ===========================
def _zstr(z):
    if not _isnum(z):
        return "  -- "
    flag = "↑" if z >= 1.5 else ("↓" if z <= -1.5 else " ")
    return "%+.2f%s" % (z, flag)


def render(meta, term_stats, chain_stats, margin_stats, breadth):
    L = []
    L.append("=" * 108)
    L.append("G12 产业链/跨期价差监控 spread_lab（纯离线只读 term_history+research_panel，只监控不下单、不进综合分）")
    L.append("跨期样本 %s~%s；z=当前价差相对过去%d日尾窗（|z|≥1.5 标↑/↓）；全市场 backwardation 广度 %d/%d（%.0f%%）"
             % (meta["d0"], meta["d1"], Z_WIN, breadth["back"], breadth["n"],
                100.0 * breadth["back"] / breadth["n"] if breadth["n"] else 0.0))
    L.append("-" * 108)
    L.append("【一】跨期价差 / 期限结构（按年化 carry 升序：越负=contango 远月升水越深、展期负carry越重）")
    L.append("  %-6s %-8s %-7s %9s %9s %8s %8s %8s %7s %10s"
             % ("品种", "近月", "次月", "近月价", "次月价", "价差%", "年化carry", "价差z", "分位", "形态"))
    rows = sorted(term_stats, key=lambda r: (r["carry_ann"] is None, r["carry_ann"] if r["carry_ann"] is not None else 0))
    for r in rows:
        sp = "%+.3f%%" % (r["spread_pct"] * 100) if _isnum(r["spread_pct"]) else "  -- "
        ca = "%+.2f%%" % (r["carry_ann"] * 100) if _isnum(r["carry_ann"]) else "  -- "
        pc = ("%.2f" % r["spread_pctile"]) if _isnum(r["spread_pctile"]) else " -- "
        L.append("  %-6s %-8s %-7s %9s %9s %8s %8s %8s %7s %10s"
                 % (r["sym"], r["near"] or "--", r["next"] or "--",
                    ("%g" % r["near_s"]) if _isnum(r["near_s"]) else "--",
                    ("%g" % r["next_s"]) if _isnum(r["next_s"]) else "--",
                    sp, ca, _zstr(r["spread_z"]), pc, r["curve"]))
    L.append("  读法：价差%>0/back=近高远低(现货升水、backwardation，多头展期有正carry)；<0/contango=远月升水；"
             "价差z极端=期限结构相对自身历史异常拉伸/收敛。")
    L.append("-" * 108)
    L.append("【二】产业链比价（无量纲 A/B；z=相对过去%d日偏离，近%d日变化看趋势；只监控相对强弱，不是利润额）"
             % (Z_WIN, CHG_WIN))
    L.append("  %-8s %-22s %-5s %9s %8s %7s %9s" % ("板块", "产业链比价", "腿", "最新比价", "z", "分位", "近60日"))
    for c in chain_stats:
        st = c["stat"]
        if st is None:
            continue
        chg = ("%+.2f%%" % (st["chg60"] * 100)) if _isnum(st["chg60"]) else "  -- "
        pc = ("%.2f" % st["pctile"]) if _isnum(st["pctile"]) else " -- "
        L.append("  %-8s %-22s %s/%-4s %9.4f %8s %7s %9s"
                 % (c["sector"], c["name"], c["a"], c["b"], st["ratio"], _zstr(st["z"]), pc, chg))
    L.append("-" * 108)
    L.append("【三】产业链盘面虚拟利润额（投料/出成系数+加工费，元/吨主产品；z=利润额相对过去%d日偏离，近%d日变化单位元/吨）"
             % (Z_WIN, CHG_WIN))
    L.append("  %-8s %-24s %10s %8s %7s %10s   口径")
    for mg in margin_stats:
        st = mg["stat"]
        if st is None:
            continue
        chg = ("%+.0f" % st["chg60"]) if _isnum(st["chg60"]) else "  -- "
        pc = ("%.2f" % st["pctile"]) if _isnum(st["pctile"]) else " -- "
        L.append("  %-8s %-24s %10.0f %8s %7s %10s   %s"
                 % (mg["sector"], mg["name"], st["value"], _zstr(st["z"]), pc, chg, mg["note"]))
    L.append("-" * 108)
    L.append("【四】诚实边界：跨期=日频结算价、未计手续费/滑点/保证金/换月；【二】无量纲比价只看相对强弱（不同合约单位不可直接相减），")
    L.append("【三】盘面虚拟利润用行业经验投料/出成系数与固定加工费（随工艺/地区/时间浮动，非精确财务利润）、虚拟钢厂毛利未计其他制造成本、")
    L.append("DCE一号大豆为食用国产豆与进口压榨口径有偏差；固定面板有幸存者偏差；全程只监控不交易、不进综合分。")
    return "\n".join(L)


def _load_panel_close(panel_db):
    """面板 -> ({sym: {date: close}}, {sym: sector}, 全部日期min/max)。"""
    store = pb.PanelStore(panel_db)
    rows = store.load_all() if hasattr(store, "load_all") else _load_all(store, sorted(store.symbols()))
    if hasattr(store, "close"):
        store.close()
    close_map, sector, dates = {}, {}, []
    for r in rows:
        s = r["sym"]
        close_map.setdefault(s, {})[r["date"]] = r.get("c")
        sector[s] = r.get("sector") or sector.get(s) or "未知"
        dates.append(r["date"])
    return close_map, sector, (min(dates) if dates else None, max(dates) if dates else None)


def _load_all(store, syms):
    out = []
    for s in syms:
        out.extend(store.load_rows(s))
    return out


def run(term_db=TERM_DB, panel_db=PANEL_DB, txt_path=LAB_TXT, json_path=LAB_JSON, verbose=True):
    # ---- A. 跨期 / 期限结构 ----
    tstore = th.TermHistoryStore(term_db)
    term_syms = sorted(r[0] for r in tstore.conn.execute("SELECT DISTINCT sym FROM ckline ORDER BY sym"))
    term_stats = []
    all_d0, all_d1 = [], []
    for s in term_syms:
        bars = tstore.load_contract_bars(s)
        ts = th.build_term_series(bars)
        if ts:
            all_d0.append(ts[0]["date"]); all_d1.append(ts[-1]["date"])
        st = term_symbol_stat(ts)
        if st:
            st["sym"] = s
            term_stats.append(st)
    tstore.close()
    back = sum(1 for r in term_stats if r["curve"] == "back")
    breadth = {"back": back, "n": len(term_stats)}

    # ---- B. 产业链比价 ----
    close_map, sector, (pd0, pd1) = _load_panel_close(panel_db)
    chain_stats = []
    for sec, name, a, b, hint in CHAINS:
        ma, mb = close_map.get(a), close_map.get(b)
        st = None
        if ma and mb:
            da = sorted(ma); ca = [ma[d] for d in da]
            db_ = sorted(mb); cb = [mb[d] for d in db_]
            od, ratio = aligned_ratio(da, ca, db_, cb)
            st = chain_stat(od, ratio)
        chain_stats.append({"sector": sec, "name": name, "a": a, "b": b, "hint": hint, "stat": st})

    # ---- C. 产业链盘面虚拟利润额（系数表） ----
    margin_stats = []
    for sec, name, weights, fee, note in MARGINS:
        od, vals = aligned_margin(close_map, weights, fee)
        st = margin_stat(od, vals)
        margin_stats.append({"sector": sec, "name": name,
                             "legs": [{"sym": s, "w": w} for s, w in weights],
                             "fee": fee, "note": note, "stat": st})

    meta = {"term_symbols": len(term_syms), "term_stats_n": len(term_stats),
            "d0": min(all_d0) if all_d0 else None, "d1": max(all_d1) if all_d1 else None,
            "panel_d0": pd0, "panel_d1": pd1, "z_win": Z_WIN, "chg_win": CHG_WIN,
            "chain_n": sum(1 for c in chain_stats if c["stat"]),
            "margin_n": sum(1 for m in margin_stats if m["stat"]),
            "backwardation": breadth}
    text = render(meta, term_stats, chain_stats, margin_stats, breadth)
    if verbose:
        print(text)
    os.makedirs(os.path.dirname(txt_path), exist_ok=True)
    with open(txt_path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(text + "\n")
    payload = {"meta": meta, "term": term_stats,
               "chains": [{k: v for k, v in c.items() if k != "hint"} for c in chain_stats],
               "margins": [{k: v for k, v in m.items() if k != "note"} for m in margin_stats]}
    with open(json_path, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(payload, fp, ensure_ascii=False, allow_nan=False, indent=1)
    try:
        extreme = [r["sym"] for r in term_stats if _isnum(r["spread_z"]) and abs(r["spread_z"]) >= 1.5]
        el.safe_record(
            "spread_lab",
            {"z_win": Z_WIN, "chg_win": CHG_WIN,
             "term_db": os.path.basename(term_db), "panel_db": os.path.basename(panel_db)},
            {"term_n": len(term_stats), "back_n": back, "z_extreme": extreme,
             "chain_n": sum(1 for c in chain_stats if c["stat"]),
             "margin_n": sum(1 for m in margin_stats if m["stat"]),
             "margin_latest": {m["name"]: (round(m["stat"]["value"], 1) if m["stat"] and _isnum(m["stat"]["value"]) else None)
                               for m in margin_stats if m["stat"]}},
            inputs=[term_db, panel_db], artifacts=[txt_path, json_path],
            conclusion="G12跨期/产业链价差监控：%d品种backwardation %d只、|价差z|≥1.5异常 %s；产业链比价%d对、盘面虚拟利润%d条，只监控不下单"
                       % (len(term_stats), back, ",".join(extreme[:12]) or "无",
                          sum(1 for c in chain_stats if c["stat"]),
                          sum(1 for m in margin_stats if m["stat"])))
    except Exception:
        pass
    return payload


# =========================== 零网络/零DB 合成断言 ===========================
def selftest():
    # 1) rolling_z：常数序列零方差→None；偏离均值2σ→z≈±2
    z = rolling_z([1.0] * 50 + [3.0], win=60, min_n=20)
    assert z[-1] is not None and z[-1] > 4
    assert rolling_z([1.0] * 60, win=60, min_n=20)[-1] is None     # 零方差
    assert rolling_z([1.0, 2.0], win=60, min_n=20)[-1] is None     # 样本不足
    # 2) rolling_percentile：最大值分位=1、严格单调
    xs = [float(i) for i in range(60)]
    pc = rolling_percentile(xs, win=60, min_n=20)
    assert pc[-1] == 1.0 and 0.0 < pc[30] <= 1.0
    # 3) spread_pct：近高远低为正=back、缺腿 None
    ts = [{"near_s": 101.0, "next_s": 100.0, "carry_nn": 0.05},
          {"near_s": 99.0, "next_s": 100.0, "carry_nn": -0.05},
          {"near_s": None, "next_s": 100.0, "carry_nn": None}]
    sp = spread_pct_series(ts)
    assert abs(sp[0] - 0.01) < 1e-12 and abs(sp[1] + 0.01) < 1e-12 and sp[2] is None
    # 4) term_symbol_stat：造一段 contango 序列，最新形态正确、字段齐
    series = []
    for t in range(80):
        series.append({"date": "2025-%02d-%02d" % (t // 28 + 1, t % 28 + 1),
                       "near": "X2510", "next": "X2511", "near_s": 100.0, "next_s": 101.0 + 0.01 * t,
                       "carry_nn": -0.04, "slope": -0.01, "oi_sum": 1000, "n_live": 8})
    st = term_symbol_stat(series)
    assert st["curve"] == "contango" and st["near"] == "X2510" and _isnum(st["spread_z"])
    assert term_symbol_stat([]) is None
    # 5) aligned_ratio：只保留公共日、比价=A/B、错位日期不配对
    od, r = aligned_ratio(["d1", "d2", "d3"], [2.0, 4.0, 6.0],
                          ["d2", "d3", "d4"], [2.0, 2.0, 3.0])
    assert od == ["d2", "d3"] and abs(r[0] - 2.0) < 1e-12 and abs(r[1] - 3.0) < 1e-12
    # 非正价跳过
    od2, r2 = aligned_ratio(["d1"], [0.0], ["d1"], [1.0])
    assert od2 == [] and r2 == []
    # 6) chain_stat：末点显著抬高→z为正、chg60可算；空序列 None
    ratio = [1.0 + 0.001 * i for i in range(80)] + [1.6]
    cs = chain_stat(["d%d" % i for i in range(81)], ratio, chg_win=60)
    assert cs["z"] is not None and cs["z"] > 1.0 and _isnum(cs["chg60"]) and cs["n"] == 81
    assert chain_stat([], []) is None
    # 7) _last_valid 跳过尾部 None
    assert _last_valid([1.0, None, 2.0, None]) == (2, 2.0)
    assert _last_valid([None, None]) == (None, None)
    # 8) aligned_margin：手算压榨利润 0.785*M+0.185*Y-A-fee；缺腿/非正跳过
    cm = {"M": {"d1": 3000.0}, "Y": {"d1": 9000.0}, "A": {"d1": 5000.0}}
    odm, vm = aligned_margin(cm, (("M", 0.785), ("Y", 0.185), ("A", -1.0)), 110.0)
    assert odm == ["d1"] and abs(vm[0] - (0.785 * 3000 + 0.185 * 9000 - 5000 - 110)) < 1e-9
    cm_miss = dict(cm); cm_miss.pop("A")
    assert aligned_margin(cm_miss, (("M", 0.785), ("Y", 0.185), ("A", -1.0))) == ([], [])
    cm_bad = {"M": {"d1": 0.0}, "Y": {"d1": 9000.0}, "A": {"d1": 5000.0}}
    assert aligned_margin(cm_bad, (("M", 0.785), ("Y", 0.185), ("A", -1.0))) == ([], [])
    # 9) margin_stat：利润额序列末点极端→z正、chg60为绝对差(元/吨)；空 None
    vals = [100.0 + i for i in range(80)] + [300.0]
    ms = margin_stat(["d%d" % i for i in range(81)], vals, chg_win=60)
    assert ms["z"] > 1.0 and abs(ms["chg60"] - (300.0 - 120.0)) < 1e-9 and ms["n"] == 81
    assert margin_stat([], []) is None
    # 10) render 端到端不崩、含四块标题与广度
    meta = {"term_symbols": 1, "term_stats_n": 1, "d0": "2025-01-01", "d1": "2025-08-01",
            "panel_d0": "2025-01-01", "panel_d1": "2025-08-01", "z_win": 120, "chg_win": 60,
            "chain_n": 1, "margin_n": 1, "backwardation": {"back": 0, "n": 1}}
    one = [{"sym": "X", "date": "2025-08-01", "near": "X2510", "next": "X2511",
            "near_s": 100.0, "next_s": 101.0, "spread_pct": -0.0099, "carry_ann": -0.04,
            "slope": -0.01, "curve": "contango", "spread_z": -1.8, "spread_pctile": 0.1,
            "carry_z": -1.2, "oi_sum": 1000, "n_live": 8, "n_days": 80}]
    cc = [{"sector": "贵金属", "name": "金银比价(金/银)", "a": "AU", "b": "AG",
           "stat": {"date": "2025-08-01", "ratio": 80.0, "z": 1.7, "pctile": 0.9, "chg60": 0.05, "n": 80}}]
    mg = [{"sector": "黑色", "name": "卷螺差(元/吨)", "note": "HC-RB",
           "stat": {"date": "2025-08-01", "value": 12.0, "z": 0.3, "pctile": 0.6, "chg60": 5.0, "n": 80}}]
    txt = render(meta, one, cc, mg, {"back": 0, "n": 1})
    for marker in ("【一】", "【二】", "【三】", "【四】", "backwardation 广度", "卷螺差"):
        assert marker in txt
    print("spread_lab selftest ALL PASS（尾窗z/分位PIT、近-次价差形态、对齐比价、产业链z、盘面利润额系数、端到端 共10组）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="G12 产业链/跨期价差监控（纯离线只读，只监控不下单）")
    ap.add_argument("--term-db", default=TERM_DB)
    ap.add_argument("--panel-db", default=PANEL_DB)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    run(term_db=args.term_db, panel_db=args.panel_db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
