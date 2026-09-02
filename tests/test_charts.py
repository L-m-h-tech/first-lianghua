# -*- coding: utf-8 -*-
"""P1-3 看板图表化回归（第22轮）：charts.py 纯函数 + 落盘 + 空态安全，零网络、确定性。

纪律：不碰生产 reports/ 与 data/，路径全部 monkeypatch 到 tmp_path；
图表只做展示层，这里同时固化"不改原始口径"的边界（抽稀不动输入、横截面只读）。
"""
import csv
import json

import charts
import cross_section as xs_mod

EQUITY_HEADER = ["dt", "static", "float", "equity", "margin",
                 "available", "risk", "drawdown", "npos"]


def _write_equity(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(EQUITY_HEADER)
        for r in rows:
            w.writerow(r)


def _equity_rows(n, base=1_000_000.0):
    rows = []
    for i in range(n):
        eq = base + i * 1000
        rows.append(["2026-08-%02d 09:%02d:00" % (1 + i // 20, i % 20),
                     base, i * 1000.0, eq, eq * 0.02, eq * 0.98,
                     0.02 + (i % 5) * 0.003, (i % 7) * 0.001, i % 3])
    return rows


# ---------------- 抽稀 ----------------

def test_downsample_passthrough_and_empty():
    a, b = charts.downsample([1, 2, 3], [4, 5, 6], max_points=10)
    assert a == [1, 2, 3] and b == [4, 5, 6]
    empty, = charts.downsample([], max_points=10)
    assert empty == []


def test_downsample_keeps_endpoints_and_aligned():
    n = 1000
    dts = ["t%d" % i for i in range(n)]
    eqs = [float(i) for i in range(n)]
    d2, e2 = charts.downsample(dts, eqs, max_points=500)
    assert len(d2) == len(e2) == 500
    assert d2[0] == "t0" and d2[-1] == "t999"      # 首尾必保留
    assert e2[0] == 0.0 and e2[-1] == 999.0
    # 并行数组同下标对齐（时间与权益不错位）
    for t, e in zip(d2, e2):
        assert t == "t%d" % int(e)


# ---------------- 权益 CSV ----------------

def test_parse_equity_missing_and_bad_header(tmp_path):
    assert charts.parse_equity_csv(str(tmp_path / "nope.csv")) is None
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    assert charts.parse_equity_csv(str(bad)) is None


def test_parse_equity_valid_summary_and_skip_dirty(tmp_path):
    p = tmp_path / "eq.csv"
    rows = _equity_rows(10)
    rows.append(["2026-08-01 x", "", "", "not-a-number", "", "", "", "", ""])  # 脏行被跳过
    _write_equity(p, rows)
    out = charts.parse_equity_csv(str(p))
    assert out["points"] == 10
    assert out["dt"][0].startswith("2026-08") and len(out["equity"]) == 10
    s = out["summary"]
    assert s["init_equity"] == 1_000_000.0
    assert abs(s["final_equity"] - 1_009_000.0) < 1e-6
    assert abs(s["total_return"] - 0.009) < 1e-9
    assert s["max_drawdown"] >= 0.0
    assert 0 < s["avg_risk"] < 0.1 and s["max_npos"] == 2


def test_parse_equity_respects_cap(tmp_path):
    p = tmp_path / "eq.csv"
    _write_equity(p, _equity_rows(3000))
    out = charts.parse_equity_csv(str(p), max_points=400)
    assert out["points"] == 400
    assert out["dt"][-1] == "2026-08-%02d 09:%02d:00" % (1 + 2999 // 20, 2999 % 20)


# ---------------- 横截面 ----------------

def _cross_section():
    rows = [{"name": "V%d" % i, "cat": ["黑色", "有色", "能化"][i % 3],
             "score": v, "chg": (i - 5) * 0.001, "price": 100.0, "label": "x"}
            for i, v in enumerate(range(-5, 6))]
    return xs_mod.rank(rows)


def test_cross_section_payload_mapping():
    assert charts.cross_section_payload(None) is None
    assert charts.cross_section_payload({}) is None
    assert charts.cross_section_payload({"rows": []}) is None
    p = charts.cross_section_payload(_cross_section())
    json.dumps(p, ensure_ascii=False)                    # JSON 安全
    assert {s["cat"] for s in p["sectors"]} == {"黑色", "有色", "能化"}
    assert p["breadth"]["n"] == 11
    assert p["top_long"] and p["top_short"]
    # 字段已 round，不会把 numpy/长浮点带进前端
    assert isinstance(p["rows"][0]["score"], float)


# ---------------- 校准 / 分周期胜率 ----------------

def test_calibration_payload_order_and_mult():
    assert charts.calibration_payload([]) is None
    rows = [
        {"dir": -1, "dir_text": "做空", "band": "轻仓", "n": 40, "hits": 20,
         "winrate": 0.5, "avg_ret": 0.001, "mult": 1.0, "enough": True},
        {"dir": 1, "dir_text": "做多", "band": "观望", "n": 12, "hits": 4,
         "winrate": 0.4, "avg_ret": -0.001, "mult": None, "enough": False},
        {"dir": 1, "dir_text": "做多", "band": "强信号", "n": 30, "hits": 20,
         "winrate": 0.62, "avg_ret": 0.003, "mult": 1.12, "enough": True},
    ]
    out = charts.calibration_payload(rows)
    # 做多在前、按 强信号→观望 排序
    assert [c["band"] for c in out if c["dir"] == 1] == ["强信号", "观望"]
    assert out[0]["dir"] == 1
    assert out[1]["mult"] is None and out[0]["mult"] == 1.12


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def outcome_stats(self, days=None):
        return self._rows


def test_outcomes_payload_aggregation():
    assert charts.outcomes_payload(None) is None
    assert charts.outcomes_payload(_FakeDB([])) is None
    rows = [
        {"horizon_min": 30, "direction": "做多", "n": 10, "evaluated": 10,
         "expired": 0, "wins": 6, "avg_ret": 0.001},
        {"horizon_min": 30, "direction": "做空", "n": 6, "evaluated": 6,
         "expired": 0, "wins": 3, "avg_ret": 0.002},
        {"horizon_min": 120, "direction": "做多", "n": 8, "evaluated": 8,
         "expired": 0, "wins": 4, "avg_ret": 0.0},
    ]
    out = charts.outcomes_payload(_FakeDB(rows))
    assert [o["horizon"] for o in out] == [30, 120]
    h30 = out[0]
    assert h30["n"] == 16 and abs(h30["winrate"] - 9 / 16) < 1e-12
    assert abs(h30["long_winrate"] - 0.6) < 1e-12
    assert abs(h30["short_winrate"] - 0.5) < 1e-12


def test_outcomes_payload_db_raises_safe():
    class Boom:
        def outcome_stats(self, days=None):
            raise RuntimeError("db locked")
    assert charts.outcomes_payload(Boom()) is None


# ---------------- 因子 JSON ----------------

def test_factor_payload(tmp_path):
    assert charts.factor_payload(str(tmp_path / "no.json")) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert charts.factor_payload(str(bad)) is None
    good = tmp_path / "f.json"
    data = {"main_h": 120, "horizons": [30, 120, 1440],
            "factors": [{"name": "动量", "by_h": {"120": {"n": 50, "rank_ic": 0.1}}}]}
    good.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    p = charts.factor_payload(str(good))
    assert p["factors"][0]["name"] == "动量"
    # 结构非法（factors 不是 list）-> None
    wrong = tmp_path / "w.json"
    wrong.write_text(json.dumps({"factors": 1}), encoding="utf-8")
    assert charts.factor_payload(str(wrong)) is None


# ---------------- ⑤ 纸面账户影子净值（第28轮 G1 二） ----------------

class _PaperDB:
    def __init__(self, rows):
        self._rows = rows

    def paper_equity_series(self, limit=2000):
        return self._rows[-limit:]


def _paper_rows(n, base=1_000_000.0):
    rows = []
    for i in range(n):
        eq = base - i * 500.0
        rows.append({"ts": "2026-09-02 %02d:%02d:00" % (9 + i // 20, (i % 20) * 3),
                     "static_equity": base - i * 200.0, "float_pnl": -i * 300.0,
                     "equity": eq, "margin_used": eq * 0.05, "available": eq * 0.95,
                     "risk_degree": 0.05 + (i % 4) * 0.01, "drawdown": (i % 6) * 0.001,
                     "n_positions": i % 5, "realized": -i * 200.0, "fees_paid": i * 7.5,
                     "n_trades": i // 2})
    return rows


class _PaperState:
    def __init__(self, rows):
        self.db = _PaperDB(rows)

    class paper:
        fill_mode = "next"


def test_paper_payload_empty_safe():
    assert charts.paper_payload(None) is None
    assert charts.paper_payload(_PaperState([])) is None

    class _Boom:
        def paper_equity_series(self, limit=2000):
            raise RuntimeError("locked")
    st = _PaperState([])
    st.db = _Boom()
    assert charts.paper_payload(st) is None
    # 缺方法的 db 也安全
    st2 = _PaperState([])
    st2.db = object()
    assert charts.paper_payload(st2) is None


def test_paper_payload_mapping_and_summary():
    st = _PaperState(_paper_rows(6))
    p = charts.paper_payload(st)
    assert p["points"] == 6 and len(p["dt"]) == 6
    assert p["fill_mode"] == "next"
    # 数组等长、数值对齐
    for k in ("equity", "static", "float", "margin", "available", "risk", "drawdown", "npos"):
        assert len(p[k]) == 6
    assert p["equity"][0] == 1_000_000.0 and p["equity"][-1] == 1_000_000.0 - 5 * 500
    s = p["summary"]
    assert abs(s["total_return"] - (p["equity"][-1] / 1e6 - 1)) < 1e-12
    assert s["max_npos"] == 4 and s["n_trades"] == 2
    assert abs(s["fees_paid"] - 5 * 7.5) < 1e-9
    # 脏行（equity 非法）被跳过
    rows = _paper_rows(3)
    rows.insert(1, {"ts": "x", "equity": None})
    p2 = charts.paper_payload(_PaperState(rows))
    assert p2["points"] == 3


def test_paper_payload_respects_cap():
    p = charts.paper_payload(_PaperState(_paper_rows(2000)), max_points=1200)
    assert p["points"] == 1200
    assert p["dt"][0] and p["dt"][-1]               # 首尾保留


# ---------------- 汇总 / JS / 落盘 ----------------

class _FakeCal:
    def band_table(self):
        return [{"dir": 1, "dir_text": "做多", "band": "分批", "n": 30, "hits": 18,
                 "winrate": 0.58, "avg_ret": 0.002, "mult": 1.08, "enough": True}]


class _State:
    pass


def test_build_payload_full_and_empty(tmp_path, monkeypatch):
    eq = tmp_path / "eq.csv"
    _write_equity(eq, _equity_rows(5))
    fj = tmp_path / "f.json"
    fj.write_text(json.dumps({"main_h": 120, "horizons": [120], "factors": []}),
                  encoding="utf-8")
    monkeypatch.setattr(charts.config, "PORTFOLIO_EQUITY_FILE", str(eq))
    monkeypatch.setattr(charts.config, "FACTOR_EVAL_JSON", str(fj))
    st = _State()
    st.db = _FakeDB([{"horizon_min": 30, "direction": "做多", "n": 4,
                      "evaluated": 4, "expired": 0, "wins": 2, "avg_ret": 0.0}])
    st.calibrator = _FakeCal()
    st.last_cross_section = _cross_section()
    p = charts.build_payload(st)
    js = charts.payload_to_js(p)
    assert js.startswith("window.CHART_DATA = ") and js.rstrip().endswith(";")
    # 可反序列化回 dict（allow_nan=False 已在构建时挡住 NaN/Infinity）
    decoded = json.loads(js[len("window.CHART_DATA = "):-2])
    assert decoded["portfolio"]["points"] == 5
    assert decoded["cross_section"]["breadth"]["n"] == 11
    assert len(decoded["calibration"]) == 1
    assert decoded["outcomes"][0]["n"] == 4
    assert decoded["factor_ic"]["main_h"] == 120
    # _FakeDB 无 paper_equity_series -> 纸面块安全 None（休眠等价）
    assert decoded["paper"] is None

    # 带纸面快照时第五块正常产出
    st2 = _State()
    st2.db = _PaperDB(_paper_rows(4))
    st2.paper = _PaperState(_paper_rows(1)).paper
    p2 = charts.build_payload(st2)
    assert p2["paper"] is not None and p2["paper"]["points"] == 4

    # 全空 state：每块独立降级为 None，不抛异常，JS 仍合法
    empty = charts.build_payload(None)
    assert empty["cross_section"] is None and empty["calibration"] is None
    assert empty["paper"] is None
    json.loads(charts.payload_to_js(empty)[len("window.CHART_DATA = "):-2])


def test_payload_escapes_script_close():
    js = charts.payload_to_js({"x": "</script><img>"})
    assert "</script>" not in js
    assert "<\\/script>" in js


def test_write_chart_data_and_page(tmp_path, monkeypatch):
    js_path = tmp_path / "chart_data.js"
    page_path = tmp_path / "图表看板.html"
    src_asset = tmp_path / "echarts.min.js"
    dst_asset = tmp_path / "assets" / "echarts.min.js"
    src_asset.write_text("/* echarts stub */", encoding="utf-8")
    monkeypatch.setattr(charts.config, "CHART_DATA_JS", str(js_path))
    monkeypatch.setattr(charts.config, "CHARTS_PAGE_HTML", str(page_path))
    monkeypatch.setattr(charts.config, "ECHARTS_SRC", str(src_asset))
    monkeypatch.setattr(charts.config, "ECHARTS_DST", str(dst_asset))
    monkeypatch.setattr(charts.config, "PORTFOLIO_EQUITY_FILE",
                        str(tmp_path / "none.csv"))
    assert charts.write_chart_data(None) is True
    assert js_path.exists() and "window.CHART_DATA" in js_path.read_text(encoding="utf-8")
    assert charts.ensure_charts_page() is True
    assert page_path.exists() and dst_asset.exists()
    html = page_path.read_text(encoding="utf-8")
    # 静态页关键结构：本地 echarts、15 个图容器、动态注入 chart_data.js
    assert 'src="assets/echarts.min.js"' in html
    for cid in ("c-equity", "c-dd", "c-risk", "c-sector", "c-xs",
                "c-ic", "c-mono", "c-cal", "c-out",
                "c-paper", "c-paper-dd", "c-paper-risk",
                "c-tear-uw", "c-tear-rs", "c-tear-m"):
        assert 'id="%s"' % cid in html
    assert "chart_data.js" in html
    # 幂等：重复调用不报错、资源不重复复制也不缺
    assert charts.ensure_charts_page() is True


def test_sync_asset_missing_source_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(charts.config, "ECHARTS_SRC", str(tmp_path / "no.js"))
    monkeypatch.setattr(charts.config, "ECHARTS_DST", str(tmp_path / "a" / "b.js"))
    assert charts.sync_echarts_asset() is False


# ---------------- 第23轮：片段拆分 + 实时看板内嵌（两页合并） ----------------

CHART_IDS = ("c-equity", "c-dd", "c-risk", "c-sector", "c-xs",
             "c-ic", "c-mono", "c-cal", "c-out",
             "c-paper", "c-paper-dd", "c-paper-risk",
             "c-tear-uw", "c-tear-rs", "c-tear-m")


def test_dashboard_embed_parts_are_fragments():
    style, dom, js = charts.dashboard_embed_parts()
    # 12 个图容器全在 DOM 片段里
    for cid in CHART_IDS:
        assert 'id="%s"' % cid in dom
    # 片段是纯片段，不带独立页外壳
    for shell in ("<!doctype", "<html", "<head", "<body", "</html>"):
        assert shell not in dom and shell not in style and shell not in js
    # 样式限定在 #charts-panel 作用域，不污染外层看板
    assert "#charts-panel .cp-grid" in style
    # JS 为 IIFE 命名空间，暴露 activate/reload 给外层页签调用
    assert js.strip().startswith("(function")
    assert "window.ChartPanel" in js
    assert "function activate" in js and "function reload" in js
    # 独立页与内嵌片段共用同一份 DOM/JS（不重复维护）
    page = charts.charts_page_html()
    assert page.count(dom) == 1 and page.count(js) == 1
    assert "window.__CHARTS_STANDALONE__" in page      # 独立页打开即自启
    assert "__CHARTS_STANDALONE__" not in dom          # 片段本身不绑定启动方式


def test_realtime_dashboard_embeds_charts_panel(monkeypatch):
    import report
    monkeypatch.setattr(report.config, "PAPER_ENABLED", True)  # 启用态下纸面页签才渲染
    h = report._dashboard_html()
    # 外层看板只引一次本地 echarts、只含一个面板容器，12 个图直接内嵌
    assert h.count('src="assets/echarts.min.js"') == 1
    assert 'id="charts-panel"' in h
    assert "window.ChartPanel" in h and "(function () {" in h
    for cid in CHART_IDS:
        assert 'id="%s"' % cid in h
    # 图表页签为内嵌标记，不再 iframe 套独立页
    assert 'data-src="__charts__"' in h
    assert 'data-src="图表看板.html"' not in h
    # 其余 txt/csv 页签全部保留走 iframe（paper_account 仅启用态渲染）
    for fname in ("latest_report.txt", "signals.csv", "signal_tracking.txt",
                  "daily_review.txt", "offhours_report.txt", "paper_account.txt"):
        assert 'data-src="%s"' % fname in h
    # 占位符必须全部被真实片段替换
    assert "/*__CP_" not in h
    # 初始仍停在第一个文本页签，面板默认隐藏、懒加载
    assert '<iframe id="view" src="latest_report.txt"></iframe>' in h
    assert "#charts-panel { display: none;" in h

# ---------------- 第29轮 G3：完整绩效 tear（水下/滚动夏普/月度热力） ----------------

def _multiday_equity(days, base=1_000_000.0, step=1000.0):
    """构造跨自然日的等长 dts/equity（每天2个快照，15:00 为当日收盘=日度取值）。

    日增幅按 0.6/1.0/1.4 倍 step 交替，保证恒为正且不相等（stdev>0，滚动夏普为正）。"""
    from datetime import datetime, timedelta
    d0 = datetime(2026, 4, 1)
    dts, eq, cum = [], [], 0.0
    for i in range(days):
        ds = (d0 + timedelta(days=i)).strftime("%Y-%m-%d")
        inc = step * (1.0 + 0.4 * ((i % 3) - 1))
        dts.append(ds + " 09:00:00")
        eq.append(base + cum)
        cum += inc
        dts.append(ds + " 15:00:00")
        eq.append(base + cum)
    return dts, eq


def test_tear_from_series_alignment_and_monthly():
    dts, eq = _multiday_equity(70)            # 跨 2 个自然月
    t = charts._tear_from_series(dts, eq, "portfolio")
    assert t is not None and t["source"] == "portfolio"
    # 水下曲线与原始时间轴等长、首点 0、全部非负
    assert len(t["uw_dt"]) == len(t["underwater"])
    assert t["underwater"][0] == 0.0 and all(x >= 0 for x in t["underwater"])
    # 日度收益与其标签、滚动夏普三者等长（修复过的 off-by-one）
    n_day_ret = len(t["rs_dt"])
    assert n_day_ret == 69 and len(t["rolling_sharpe"]) == n_day_ret
    # 单调上行权益：水下恒 0、滚动夏普为正（暖机期 None）
    assert max(t["underwater"]) == 0.0
    assert t["rolling_sharpe"][0] is None
    assert all(x is None or x > 0 for x in t["rolling_sharpe"])
    # 月度热力：月索引 0-11、年索引连续、收益小数
    assert t["monthly_years"] == [2026]
    for mi, yi, v in t["monthly_cells"]:
        assert 0 <= mi <= 11 and yi == 0 and isinstance(v, float)
    assert len(t["monthly_cells"]) >= 2
    # 标量摘要键齐全且可 JSON 化
    for k in ("sharpe", "sortino", "calmar", "omega", "ulcer", "var", "cvar"):
        assert k in t["summary"]
    json.dumps(t, ensure_ascii=False)


def test_tear_from_series_insufficient_samples():
    # 只有一个自然日（同日多快照）-> 日度收益不足 -> None
    dts = ["2026-04-01 09:00", "2026-04-01 15:00"]
    assert charts._tear_from_series(dts, [1e6, 1.001e6], "x") is None
    assert charts._tear_from_series([], [], "x") is None


def test_tear_payload_prefers_paper_then_csv(tmp_path, monkeypatch):
    # 1) 有纸面快照优先 paper
    rows = [{"ts": "2026-04-%02d 15:00:00" % (1 + i), "equity": 1e6 - i * 500.0}
            for i in range(70)]

    class _DB:
        def paper_equity_series(self, limit=20000):
            return rows

    class _St:
        db = _DB()
    t = charts.tear_payload(_St())
    assert t is not None and t["source"] == "paper"

    # 2) 无纸面（state=None）时回退组合回测 CSV
    p = tmp_path / "eq.csv"
    dts, eq = _multiday_equity(70)
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["dt", "static", "float", "equity", "margin",
                    "available", "risk", "drawdown", "npos"])
        for d, e in zip(dts, eq):
            w.writerow([d, 1e6, 0.0, e, e * 0.02, e * 0.98, 0.02, 0.0, 1])
    monkeypatch.setattr(charts.config, "PORTFOLIO_EQUITY_FILE", str(p))
    t2 = charts.tear_payload(None)
    assert t2 is not None and t2["source"] == "portfolio"

    # 3) 都没有 -> None（空态安全，不抛）
    miss = tmp_path / "miss.csv"
    monkeypatch.setattr(charts.config, "PORTFOLIO_EQUITY_FILE", str(miss))
    assert charts.tear_payload(None) is None


def test_build_payload_contains_tear_block(tmp_path, monkeypatch):
    monkeypatch.setattr(charts.config, "PORTFOLIO_EQUITY_FILE", str(tmp_path / "n.csv"))
    p = charts.build_payload(None)
    assert "tear" in p and p["tear"] is None       # 无数据安全降级为 None
    decoded = json.loads(charts.payload_to_js(p)[len("window.CHART_DATA = "):-2])
    assert decoded["tear"] is None
