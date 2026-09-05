# -*- coding: utf-8 -*-
r"""第81轮 #8：G21 面板长期化——term 长序列 → 长面板 DB 回填（研究侧离线工具）。

背景：G21 research_panel.db 来自新浪主连（约1023根上限），只有约4年（2022-07 起），
是第80轮"低波异象时段性"结论的最大样本约束。term_history 的逐合约缓存经
`adjusted_near_ohlc` 可重建 2018 年起的近月比例复权 OHLC 长序列（换月拼接连续、
ret126/hv60 现算），本工具把它回填成与 G21 面板同 schema 的长面板
`cache/research_panel_long.db`（v/oi/其余特征列为 None——消费端按缺列容错）。

诚实边界：
  - 近月拼接的复权口径与主连复权**不完全等价**（换月缓冲选择不同、旧合约 h/l 质量无保障），
    跨口径对照时结论必须标注来源（第80轮已实证两者同段 IC 有差异）；
  - 纯研究侧：不写 G21 主面板、不被 main import、零网络。

用法（项目根目录）：
  D:\\Python\\python.exe tools\\long_panel_builder.py                # 全品种回填
  D:\\Python\\python.exe tools\\long_panel_builder.py --codes 螺纹钢,铜
  D:\\Python\\python.exe tools\\long_panel_builder.py --selftest
"""
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

import config                       # noqa: E402  品种表
import panel_builder as pb          # noqa: E402  PanelStore（同 schema 复用）
import term_history as th           # noqa: E402  adjusted_near_ohlc 长序列

DEFAULT_DB = ROOT / "cache" / "research_panel_long.db"
DEFAULT_TERM_DB = ROOT / "cache" / "term_history.db"


def build_rows(sym, sector, warmup=126, term_db=None):
    """单品种：term 缓存 → 近月复权长序列 rows（含 sym/sector/ret126/hv60）。"""
    store = th.TermHistoryStore(term_db or th.TERM_DB_PATH)
    try:
        rows = th.adjusted_near_ohlc(sym, store, warmup=warmup)
    finally:
        store.close()
    if not rows:
        return []
    # ret126/hv60 已由 adjusted_near_ohlc 现算；补充空缺键为 None（PanelStore 按 ALL_COLS 取值）
    out = []
    for r in rows:
        out.append({"sym": sym, "date": r["date"], "sector": sector,
                    "o": r.get("o"), "h": r.get("h"), "l": r.get("l"), "c": r.get("c"),
                    "v": None, "oi": None, "ret126": r.get("ret126"), "hv60": r.get("hv60")})
    return out


def run(db_path=None, term_db=None, codes="", limit=0, verbose=True):
    db_path = str(db_path or DEFAULT_DB)
    items = backtest_resolve(codes, limit)
    store = pb.PanelStore(db_path)
    n_sym = n_rows = 0
    dmin = dmax = None
    for name, main_code in items:
        meta = config.VARIETIES.get(name, {})
        sym = meta.get("sym") or main_code.rstrip("0")
        sector = meta.get("cat", "其他")
        rows = build_rows(sym, sector, term_db=term_db)
        if not rows:
            continue
        store.replace_symbol(sym, rows)
        n_sym += 1
        n_rows += len(rows)
        d0, d1 = rows[0]["date"], rows[-1]["date"]
        dmin = d0 if dmin is None or d0 < dmin else dmin
        dmax = d1 if dmax is None or d1 > dmax else dmax
    store.record_run([n for n, _ in items], 0, n_sym, n_rows, dmin, dmax, 0,
                     note="第81轮 long_panel_builder：term 近月复权长序列（v/oi/其余特征列为 None）")
    store.close()
    msg = ("long_panel：%d 品种 / %d 行（%s ~ %s）-> %s"
           % (n_sym, n_rows, dmin, dmax, db_path))
    if verbose:
        print(msg)
    return {"n_symbols": n_sym, "n_rows": n_rows, "date_min": dmin,
            "date_max": dmax, "db": db_path, "msg": msg}


def backtest_resolve(codes, limit):
    """与 carry_eval 同源：中文名/主连 → [(name, main_code)]。"""
    import backtest
    return backtest.resolve_codes(codes or "", limit if limit and limit > 0 else None)


def selftest():
    import tempfile
    import factor_expr as fx
    from term_history import TermHistoryStore
    tmpdir = tempfile.mkdtemp(prefix="lpb_t_")
    term_db = os.path.join(tmpdir, "th.db")
    tstore = TermHistoryStore(term_db)

    from datetime import date as _d, timedelta as _td

    def _bars(code_price, d0, d1):
        out = []
        for d in range(d0, d1 + 1):
            dt = _d(2026, 1, 1) + _td(days=d)
            c = code_price + d * 0.5
            out.append({"d": dt.isoformat(), "c": c, "s": c, "v": 5, "p": 50,
                        "h": c * 1.01, "l": c * 0.99, "o": c})
        return out

    tstore.save_contract("XX", "XX2603", _bars(100.0, 0, 44))
    tstore.save_contract("YY", "YY2603", _bars(500.0, 0, 44))
    tstore.save_contract("XX", "XX2604", _bars(200.0, 30, 119))
    tstore.save_contract("YY", "YY2604", _bars(600.0, 30, 119))
    tstore.save_contract("XX", "XX2605", _bars(300.0, 60, 119))
    tstore.save_contract("YY", "YY2605", _bars(700.0, 60, 119))
    try:
        rows = build_rows("XX", "测试", warmup=5, term_db=term_db)   # 合成120天，暖机缩短
        assert rows and rows[0]["date"] < rows[-1]["date"]
        closes = [r["c"] for r in rows]
        rets = [abs(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes))]
        assert max(rets) < 0.02                              # 拼接连续
        assert any(r["ret126"] is not None for r in rows)
        assert any(r["hv60"] is not None for r in rows)
        # 回填到临时长面板并读回（schema 兼容 PanelStore）
        panel_db = os.path.join(tmpdir, "panel_long.db")
        pstore = pb.PanelStore(panel_db)
        pstore.replace_symbol("XX", rows)
        pstore.close()
        rd = pb.PanelStore(panel_db)
        back = rd.load_rows("XX")
        rd.close()
        assert len(back) == len(rows)
        b0 = back[0]
        assert b0["sym"] == "XX" and b0["c"] is not None
        assert any(r["ret126"] is not None for r in back)   # 暖机后 ret126 现算可用
        # None 列容错：series_from_rows 对 v/oi 缺值安全（不崩、None 进 DSL 为缺失）
        import expr_miner as em
        sr = em.series_from_rows(back)
        assert len(sr["close"]) == len(back)
        assert all(v is None for v in sr["volume"])         # 长面板 v 列为 None
    finally:
        tstore.close()
    print("long_panel_builder selftest ALL PASS（term长序列rows/拼接连续/ret126+hv60现算/"
          "PanelStore schema 兼容回读/缺列容错 共5组）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="G21 面板长期化：term 长序列→长面板（研究侧）")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--term-db", default=None)
    ap.add_argument("--codes", default="", help="逗号分隔中文名/主连，缺省=全品种")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    run(db_path=args.db, term_db=args.term_db, codes=args.codes, limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
