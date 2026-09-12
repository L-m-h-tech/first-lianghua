# -*- coding: utf-8 -*-
r"""PCR 因子影子体检 tools/pcr_factor_research.py（第129轮，阶段D影子，研究侧只读/零网络/零第三方依赖）。

回答阶段D遗留问题："PCR 接入综合分"有没有预测力证据（Legend T链载体已废，数据源用量化自己的
新浪T链 option_chains.pcr_oi，2026-09-01 起积累 8 万+ 行、39 品种）。

数据源（全部只读）：
  - PCR 面板：monitor.db option_chains（cycle>=1）→ 每品种每日 PCR = 当日最后一个快照时点上、
    全部到期月 OI 合计 put/call（当日多快照取末次，避免日内重复计权）；
  - 收益：monitor.db minute_bars 60m 末日收盘 → 日收盘序列 → ret1d/ret5（|ret1d|>10% 视为
    换月跳变剔除——未复权口径的诚实近似；研究面板只到 09-02，分钟库自算不受面板新鲜度限制）。

候选因子（双向假说都测，方向让数据说话）：
  - pcr_level  当日 PCR 水平（高=put OI 占优：反向假说=恐慌见底偏多 / 确认假说=趋势偏空）
  - pcr_chg    PCR 日变化
  - pcr_pct30  30 交易日滚动分位（历史不足 30 天如实标"样本不足"，当前即此状态）
预测口径（严格 PIT）：因子(t)（t 日末 23:00 快照已可得）对 ret1d(t→t+1) / ret5(t→t+5) 的
Spearman 秩相关——逐日截面 RankIC 均值 ± t 估计 + 全样本池化 + 五分位多空价差。

输出 reports/pcr_factor_research.txt / .json（看板"研究报告(全部)"自动聚合）。
纪律：只读、不写生产表、不进综合分；结论只做"继续积累/放弃"的决策素材。
CLI: python tools/pcr_factor_research.py [--monitor-db ...] [--selftest]
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config                     # noqa: E402

PCT_WINDOW = 30                   # 滚动分位窗口（交易日）
RET_OUTLIER = 0.10                # |ret1d|>10% 视为换月跳变剔除（未复权口径）


def _q(db, sql, args=()):
    conn = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# ---------------- PCR 日度面板 ----------------
def load_pcr_daily(monitor_db):
    """option_chains(cycle>=1, pcr_oi 非 NULL) → {sym: [(date, pcr), ...]} 按日升序。

    每日取当日最后一个快照时点，跨到期月 OI 合计 put/call；call 合计<=0 的时点跳过。"""
    rows = _q(monitor_db, """SELECT sym, substr(ts,1,10) d, ts, put_oi, call_oi
                             FROM option_chains WHERE cycle>=1 AND pcr_oi IS NOT NULL
                             ORDER BY sym, ts""")
    per_ts = {}
    for sym, d, ts, put, call in rows:
        if put is None or call is None:
            continue
        per_ts.setdefault((sym, d, ts), [0.0, 0.0])
        per_ts[(sym, d, ts)][0] += float(put)
        per_ts[(sym, d, ts)][1] += float(call)
    daily = {}
    for (sym, d, ts), (put, call) in per_ts.items():
        if call > 0 and put > 0:
            daily.setdefault(sym, {}).setdefault(d, []).append((ts, put / call))
    out = {}
    for sym, by_day in daily.items():
        out[sym] = [(d, sorted(ts_list)[-1][1]) for d, ts_list in sorted(by_day.items())]
    return out


# ---------------- 收益面板（分钟库 60m 末日收盘） ----------------
def load_closes_from_minutes(monitor_db, syms):
    """minute_bars(period=60) 末日收盘 → {sym: [(date, close), ...]}（按日升序）。"""
    out = {}
    for sym in syms:
        rows = _q(monitor_db, """SELECT substr(bar_dt,1,10) d, bar_dt, c FROM minute_bars
                                 WHERE sym=? AND period=60 ORDER BY bar_dt""", (sym,))
        by_day = {}
        for d, dt, c in rows:
            if c:
                by_day[d] = float(c)          # 升序遍历，末次覆盖=当日收盘
        out[sym] = sorted(by_day.items())
    return out


def forward_returns(closes):
    """日收盘序列 → {date: (ret1d_fwd, ret5_fwd)}：ret1d_fwd(t)=c(t+1)/c(t)-1，ret5_fwd(t)=c(t+5)/c(t)-1。"""
    fwd = {}
    for i in range(len(closes)):
        d, c0 = closes[i]
        r1 = r5 = None
        if i + 1 < len(closes):
            c1 = closes[i + 1][1]
            if c0 > 0 and abs(c1 / c0 - 1) <= RET_OUTLIER:
                r1 = c1 / c0 - 1
        if i + 5 < len(closes):
            c5 = closes[i + 5][1]
            if c0 > 0 and abs(c5 / c0 - 1) <= RET_OUTLIER:
                r5 = c5 / c0 - 1
        fwd[d] = (r1, r5)
    return fwd


# ---------------- 统计（纯函数） ----------------
def _ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs, ys):
    """Spearman 秩相关（并列取平均秩）；n<3 或零方差返 None。"""
    n = len(xs)
    if n < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx ** 0.5 * vy ** 0.5)


def quintile_spread(xs, ys, q=5):
    """按因子五分位：返回 (低组均值y, 高组均值y, 高-低)；分组不足返 None。"""
    n = len(xs)
    if n < q * 2:
        return None
    idx = sorted(range(n), key=lambda i: xs[i])
    size = n // q
    lo = [ys[i] for i in idx[:size]]
    hi = [ys[i] for i in idx[-size:]]
    return sum(lo) / len(lo), sum(hi) / len(hi), sum(hi) / len(hi) - sum(lo) / len(lo)


# ---------------- 因子构造与评估 ----------------
def build_factors(pcr_daily):
    """{sym: [(date, pcr)]} → {factor: {(sym, date): value}}；pcr_pct30 不足窗口的品种跳过。"""
    fac = {"pcr_level": {}, "pcr_chg": {}, "pcr_pct30": {}}
    for sym, series in pcr_daily.items():
        for i, (d, pcr) in enumerate(series):
            fac["pcr_level"][(sym, d)] = pcr
            if i > 0:
                prev = series[i - 1][1]
                if prev > 0:
                    fac["pcr_chg"][(sym, d)] = pcr / prev - 1.0
            if len(series) >= PCT_WINDOW and i >= PCT_WINDOW - 1:
                window = [p for _, p in series[i - PCT_WINDOW + 1:i + 1]]
                below = sum(1 for p in window if p <= pcr)
                fac["pcr_pct30"][(sym, d)] = below / len(window)
    return fac


def evaluate(fac_value, ret_map, horizon):
    """因子 → (池化RankIC, 逐日截面RankIC均值, 逐日数, 五分位高-低, n)。ret_map: {(sym,date): ret}。"""
    xs, ys, by_day = [], [], {}
    for (sym, d), v in fac_value.items():
        r = ret_map.get((sym, d))
        if r is None:
            continue
        xs.append(v)
        ys.append(r)
        by_day.setdefault(d, ([], []))
        by_day[d][0].append(v)
        by_day[d][1].append(r)
    pooled = spearman(xs, ys)
    day_ics = [spearman(vx, vy) for vx, vy in by_day.values()]
    day_ics = [x for x in day_ics if x is not None]
    mean_ic = sum(day_ics) / len(day_ics) if day_ics else None
    tstat = None
    if len(day_ics) >= 3 and mean_ic is not None:
        var = sum((x - mean_ic) ** 2 for x in day_ics) / (len(day_ics) - 1)
        if var > 0:
            tstat = mean_ic / (var ** 0.5) * (len(day_ics) ** 0.5)
    qs = quintile_spread(xs, ys)
    return {"pooled_spearman": round(pooled, 4) if pooled is not None else None,
            "day_mean_ic": round(mean_ic, 4) if mean_ic is not None else None,
            "n_days": len(day_ics),
            "day_ic_t": round(tstat, 2) if tstat is not None else None,
            "q_spread": round(qs[2], 5) if qs else None,
            "n": len(xs)}


def run(monitor_db=None):
    monitor_db = monitor_db or config.MONITOR_DB
    pcr_daily = load_pcr_daily(monitor_db)
    closes = load_closes_from_minutes(monitor_db, list(pcr_daily))
    # 因子(t) → 前向收益(t→t+1 / t→t+5)
    fac = build_factors(pcr_daily)
    ret1, ret5 = {}, {}
    for sym, series in pcr_daily.items():
        fwd = forward_returns(closes.get(sym, []))
        for d, _ in series:
            r1, r5 = fwd.get(d, (None, None))
            if r1 is not None:
                ret1[(sym, d)] = r1
            if r5 is not None:
                ret5[(sym, d)] = r5
    res = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "pcr_days": min((len(s) for s in pcr_daily.values()), default=0),
           "max_pcr_days": max((len(s) for s in pcr_daily.values()), default=0),
           "n_syms": len(pcr_daily), "horizons": {}}
    for fname in ("pcr_level", "pcr_chg", "pcr_pct30"):
        res["horizons"][fname] = {
            "ret1d": evaluate(fac[fname], ret1, "ret1d"),
            "ret5": evaluate(fac[fname], ret5, "ret5"),
            "sample_note": ("30日分位需 %d 天历史" % PCT_WINDOW
                            if fname == "pcr_pct30" and res["max_pcr_days"] < PCT_WINDOW else "")}
    return res


def render(res):
    L = ["PCR 因子影子体检（第129轮，阶段D影子——只测预测力，不进综合分）", "=" * 64,
         "生成: %s ｜ 品种 %d ｜ PCR 历史 %d 个交易日（滚动分位需 30 天）" % (
             res["generated"], res["n_syms"], res["pcr_days"]),
         "口径: 因子(t)=当日末次快照(23:00后已可得) → 前向收益 ret1d(t→t+1)/ret5(t→t+5)，严格 PIT；",
         "      收益自 minute_bars 60m 末日收盘（未复权，|ret|>10% 剔除）；逐日截面 RankIC。", ""]
    L.append("%-10s %8s %10s %10s %6s %7s %12s %5s" % (
        "因子", "期限", "池化RankIC", "逐日IC均值", "天数", "t估计", "五分位高-低", "n"))
    for fname, hs in res["horizons"].items():
        note = hs.get("sample_note") or ""
        for h in ("ret1d", "ret5"):
            e = hs[h]
            L.append("%-10s %8s %10s %10s %6d %7s %12s %5d %s" % (
                fname, h,
                str(e["pooled_spearman"]) if e["pooled_spearman"] is not None else "—",
                str(e["day_mean_ic"]) if e["day_mean_ic"] is not None else "—",
                e["n_days"],
                str(e["day_ic_t"]) if e["day_ic_t"] is not None else "—",
                str(e["q_spread"]) if e["q_spread"] is not None else "—",
                e["n"], ("⚠" + note) if note else ""))
    L.append("")
    L.append("解读（双向假说）: pcr_level IC>0 支持'确认假说'（高PCR顺势偏空获利=因子应反向使用需谨慎），")
    L.append("IC<0 支持'反向假说'（高PCR恐慌见底偏多）；|逐日IC t|<2 或天数<10 一律视为'证据不足，继续积累'。")
    L.append("决策门: 连续 20+ 交易日 IC 方向稳定且 |t|>=2 才可提交 factors_catalog 注册评审；当前仅为影子观察。")
    L.append("（只读体检：不改综合分/不写生产表；PCR 情绪档在期权严格分析中已在用，此处只评估期货综合分扩展）")
    return "\n".join(L)


def selftest():
    """零网络合成断言：PCR聚合取末次快照/因子构造/前向对齐PIT/ spearman与五分位。"""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="pcr_res_")
    try:
        db = os.path.join(tmp, "m.db")
        conn = sqlite3.connect(db)
        conn.execute("""CREATE TABLE option_chains(id INTEGER PRIMARY KEY, ts TEXT, cycle INTEGER,
            sym TEXT, expiry TEXT, put_oi REAL, call_oi REAL, pcr_oi REAL)""")
        conn.execute("""CREATE TABLE minute_bars(id INTEGER PRIMARY KEY, sym TEXT, period INTEGER,
            bar_dt TEXT, c REAL)""")
        # RB：09-01 两个快照（取末次），09-02 三个到期月同快照（跨月合计）
        chain_rows = [
            ("2026-09-01 15:00:03", 1, "RB", "2610", 100.0, 200.0, 0.5),
            ("2026-09-01 23:00:03", 1, "RB", "2610", 120.0, 200.0, 0.6),   # 末次快照 → 0.6
            ("2026-09-02 23:00:03", 1, "RB", "2610", 200.0, 200.0, 1.0),
            ("2026-09-02 23:00:03", 1, "RB", "2701", 100.0, 100.0, 1.0),   # 跨月合计 300/300=1.0
        ]
        for r in chain_rows:
            conn.execute("INSERT INTO option_chains(ts,cycle,sym,expiry,put_oi,call_oi,pcr_oi)"
                         " VALUES(?,?,?,?,?,?,?)", r)
        # RB 60m 收盘：09-01=100 → 09-02=101 → 09-08=103（ret1d 09-01=+1%）
        closes = [("2026-09-01", "2026-09-01 23:00:00", 100.0),
                  ("2026-09-02", "2026-09-02 23:00:00", 101.0),
                  ("2026-09-08", "2026-09-08 23:00:00", 103.0)]
        for d, dt, c in closes:
            conn.execute("INSERT INTO minute_bars(sym,period,bar_dt,c) VALUES('RB',60,?,?)", (dt, c))
        conn.commit()
        conn.close()
        pcr = load_pcr_daily(db)
        assert pcr["RB"] == [("2026-09-01", 0.6), ("2026-09-02", 1.0)], pcr   # 末次快照+跨月合计
        fac = build_factors(pcr)
        assert abs(fac["pcr_level"][("RB", "2026-09-02")] - 1.0) < 1e-9
        assert abs(fac["pcr_chg"][("RB", "2026-09-02")] - (1.0 / 0.6 - 1)) < 1e-9
        closes_map = load_closes_from_minutes(db, ["RB"])
        fwd = forward_returns(closes_map["RB"])
        assert abs(fwd["2026-09-01"][0] - 0.01) < 1e-9                        # 前向1日=+1%
        assert fwd["2026-09-02"][0] is None or abs(fwd["2026-09-02"][0] - 0.019802) < 1e-4
        # spearman 方向性：因子与收益同向 → +1
        assert abs(spearman([1, 2, 3, 4], [10, 20, 30, 40]) - 1.0) < 1e-9
        assert spearman([1, 2], [1, 2]) is None                                # n<3
        qs = quintile_spread([1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                             [1, 1, 1, 1, 1, 2, 2, 2, 2, 2])
        assert qs[2] == 1.0                                                    # 高-低=1
        print("pcr_factor_research selftest OK")
        return 0
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="PCR 因子影子体检（阶段D影子，研究侧只读）")
    ap.add_argument("--monitor-db", default=config.MONITOR_DB)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    res = run(args.monitor_db)
    txt = render(res)
    os.makedirs(os.path.join(_ROOT, "reports"), exist_ok=True)
    txt_path = os.path.join(_ROOT, "reports", "pcr_factor_research.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    js_path = os.path.join(_ROOT, "reports", "pcr_factor_research.json")
    with open(js_path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(txt)
    print("\n已写出: %s / %s" % (txt_path, js_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main() if len(sys.argv) > 1 else selftest())
