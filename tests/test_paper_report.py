# -*- coding: utf-8 -*-
"""第28轮 G1（二）：纸面账户报告块 / paper_account.txt / 看板页签 回归（零网络、确定性）。"""
import os

import pytest

import config
import paper_broker
import report
from portfolio import Portfolio

_FEE = {"multiplier": 10, "open_amt_rate": 1e-4, "open_per_lot": 3.0,
        "close_amt_rate": 1e-4, "close_per_lot": 3.0,
        "today_amt_rate": 0.0, "today_per_lot": 0.0}
_MARGIN = {"RB": {"broker_margin": 0.1, "limit_basic": 0.05, "multiplier": 10}}


def row(sym="RB", name="螺纹钢", cat="黑色", score=5.0, price=3000.0,
          contract_code="", main_month=""):
    return {"sym": sym, "name": name, "cat": cat, "score": score, "price": price,
            "code": sym + "0", "atr": 20.0,
            "contract_code": contract_code, "main_month": main_month}


def make_broker(fill="close", db=None):
    return paper_broker.PaperBroker(
        db=db, fill_mode=fill, equity0=1_000_000.0, entry_score=4.0, exit_score=2.0,
        margin_table=_MARGIN, fee_table={"RB": _FEE},
        sector_of={"RB": "黑色"}, slip_rate=0.0, restore=False,
        owner_fn=lambda ts: None)


class _State:
    pass


def _active_state(fill="close"):
    st = _State()
    st.paper = make_broker(fill)
    st.last_paper = None
    return st


def test_dormant_state_emits_nothing(tmp_path, monkeypatch):
    st = _State()
    st.paper = None
    st.last_paper = None
    assert report.paper_block(st) == []
    assert report.paper_account_text(st) == ""
    out = tmp_path / "should_not_exist.txt"
    monkeypatch.setattr(report.config, "PAPER_ACCOUNT_TXT", str(out))
    report.write_paper_account(st)
    assert not out.exists()                        # 休眠不落盘


def test_paper_block_and_account_text():
    st = _active_state()
    st.last_paper = st.paper.on_cycle("2026-09-02 10:00:00", [row()])
    block = report.paper_block(st)
    assert block and block[0].startswith("【纸面·基准】")
    joined = "\n".join(block)
    assert "动态权益" in joined and "风险度" in joined and "确定拒单" in joined
    assert len(block) <= 5                         # 紧凑块不膨胀

    text = report.paper_account_text(st)
    for title in ("【账户概览】", "【委托状态统计】", "【当前持仓】", "【在途挂单】",
                  "【最近成交", "不构成投资建议"):
        assert title in text
    assert "螺纹钢" in text and "RB" in text       # 持仓明细落文本


def test_paper_account_text_after_close():
    st = _active_state()
    st.paper.on_cycle("2026-09-02 10:00:00", [row()])
    st.last_paper = st.paper.on_cycle("2026-09-02 10:05:00",
                                      [row(score=1.0, price=3020.0)])  # 平仓
    text = report.paper_account_text(st)
    assert "（空仓）" in text and "累计平仓 1 笔" in text


def test_paper_account_text_pending_next_mode():
    st = _active_state("next")
    st.last_paper = st.paper.on_cycle("t1", [row()])   # next 档只挂单
    text = report.paper_account_text(st)
    assert "在途挂单 1 个" in text
    pv = st.paper.pending_view()
    assert len(pv) == 1 and pv[0]["sym"] == "RB"


def test_write_paper_account_file(tmp_path, monkeypatch):
    out = tmp_path / "paper_account.txt"
    monkeypatch.setattr(report.config, "PAPER_ACCOUNT_TXT", str(out))
    st = _active_state()
    st.last_paper = st.paper.on_cycle("2026-09-02 10:00:00", [row()])
    report.write_paper_account(st)
    assert out.exists()
    body = out.read_text(encoding="utf-8-sig")
    assert "纸面交易账户" in body and "不构成投资建议" in body


def test_paper_tick_annotation_in_txt(tmp_path, monkeypatch):
    """第103/104轮：口径变更标注（ticker 撮合粒度 + 统一资金池，按开关显示）。"""
    import os
    monkeypatch.setattr(report.config, "PAPER_TICK_INTERVAL", 60)
    monkeypatch.setattr(report.config, "PAPER_TICK_ANNOTATION",
                        "自2026-09-09(第103轮)起交易时段纸面撮合粒度由5/10分钟改为1分钟(ticker线程)")
    monkeypatch.setattr(report.config, "PAPER_UNIFIED_POOL", True)
    monkeypatch.setattr(report.config, "PAPER_UNIFIED_POOL_ANNOTATION",
                        "自2026-09-09(第104轮)起期权与期货统一资金池")
    out = tmp_path / "paper_account.txt"
    monkeypatch.setattr(report.config, "PAPER_ACCOUNT_TXT", str(out))
    st = _active_state()
    st.last_paper = st.paper.on_cycle("2026-09-09 10:00:00", [row()])
    report.write_paper_account(st)
    body = out.read_text(encoding="utf-8-sig")
    assert body.startswith("# 自2026-09-09")
    assert "第103轮" in body and "第104轮" in body and "统一资金池" in body
    # 全关（ticker=0 且 独立池）时不加标注
    monkeypatch.setattr(report.config, "PAPER_TICK_INTERVAL", 0)
    monkeypatch.setattr(report.config, "PAPER_UNIFIED_POOL", False)
    report.write_paper_account(st)
    body2 = out.read_text(encoding="utf-8-sig")
    assert not body2.startswith("# 自")


def test_dashboard_tab_registered():
    tabs = [t[0] for t in report._DASHBOARD_TABS]
    assert "paper_account.txt" in tabs
    assert tabs.index("paper_account.txt") == tabs.index("portfolio_trades.csv") + 1


def test_dashboard_tab_visibility_follows_switch(monkeypatch):
    # 静态页签表始终登记；渲染出的看板在休眠态隐藏、启用态显示
    monkeypatch.setattr(report.config, "PAPER_ENABLED", False)
    assert 'data-src="paper_account.txt"' not in report._dashboard_html()
    monkeypatch.setattr(report.config, "PAPER_ENABLED", True)
    assert 'data-src="paper_account.txt"' in report._dashboard_html()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))


def test_research_reports_tab_and_aggregator(tmp_path, monkeypatch):
    """第87轮：研究报告(全部)页签登记 + 聚合器产出卡片/排除实时页签/转义防注入。"""
    # 1) 页签已登记
    assert ("__research__", "研究报告(全部)") in report._DASHBOARD_TABS
    # 2) 聚合器对临时目录生效：造两个 txt（正常 + 含 HTML 注入字符）
    monkeypatch.setattr(report.config, "BASE_DIR", str(tmp_path))
    (tmp_path / "reports").mkdir(exist_ok=True)
    (tmp_path / "reports" / "shadow_track.txt").write_text(
        "影子信号追踪\n已记录 1 日", encoding="utf-8")
    (tmp_path / "reports" / "evil_report.txt").write_text(
        "<script>alert(1)</script>&<b>", encoding="utf-8")
    h = report._research_reports_html(max_rows=6)
    assert "shadow_track.txt" in h
    # HTML 注入被转义：脚本标签不得原样出现
    assert "<script>alert(1)</script>" not in h
    assert "&lt;script&gt;" in h and "&amp;&lt;b&gt;" in h
    # 3) 排除了实时看板页签文件
    assert "latest_report.txt" not in h
    # 4) _dashboard_html 注入研究页签 + research-panel 容器
    html = report._dashboard_html()
    assert '__research__' in html and 'research-panel' in html


def test_account_text_shows_contract(tmp_db):
    """第93轮：paper_account.txt 持仓/成交显示具体合约列（开仓说明是哪个时间段的合约）。"""
    st = _active_state()
    st.last_paper = st.paper.on_cycle(
        "2026-09-02 10:00:00",
        [row(contract_code="RB2610", main_month="2610")])
    text = report.paper_account_text(st)
    assert "合约" in text and "RB2610" in text          # 表头"合约"列 + 持仓行具体合约
    # 带 DB：开+平后，最近成交表同样带合约
    st2 = _State()
    st2.paper = make_broker("close", db=tmp_db)
    st2.last_paper = st2.paper.on_cycle("2026-09-02 11:00:00",
                                        [row(score=5.0, contract_code="RB2610")])
    st2.last_paper = st2.paper.on_cycle("2026-09-02 11:05:00",
                                        [row(score=1.0, contract_code="RB2610")])
    text2 = report.paper_account_text(st2)
    assert "RB2610" in text2


def test_dashboard_newdata_tab():
    """第97轮：实时看板含"新数据因子"页签（__newdata__ 静态注入，不走 iframe）。"""
    html = report._dashboard_html()
    assert "新数据因子" in html                      # 页签标题
    assert "data-src=\"__newdata__\"" in html         # 页签绑定
    assert "newdata-panel" in html                    # 面板 DOM
    assert "__ND_DOM__" not in html                   # 占位符已替换
    assert "atmv_percentile" in html or "暂无数据" in html   # 因子内容注入


def test_newdata_panel_html_fallback(tmp_path, monkeypatch):
    """报告缺失时页签显示占位文案而非崩溃。"""
    monkeypatch.setattr(report.config, "BASE_DIR", str(tmp_path))
    out = report._newdata_panel_html()
    assert "暂无数据" in out


def test_paper_compare_html_unified_no_opt_eq_column(tmp_path, monkeypatch):
    """第105轮：对比表重构（5档卡+9列+details折叠+基准高亮+b-badge+点击链接详情页）。"""
    import json
    monkeypatch.setattr(report.config, "BASE_DIR", str(tmp_path))
    (tmp_path / "reports").mkdir(exist_ok=True)
    rows = [
        {"name": "10万_激进", "tier": "10万", "style": "激进", "equity0": 100_000, "equity": 100_376,
         "ret": 0.00376, "max_drawdown": -0.02, "risk_degree": 0.21, "n_fut_pos": 6, "n_opt_pos": 0,
         "n_closed": 2, "n_pending": 0, "realized": -59.0, "fees": 15.0, "fill_mode": "close",
         "priority": "futures_first", "entry_score": 3.0,
         "detail": {"positions": [{"sym": "CS", "contract": "cs2611", "name": "淀粉", "dir": "空",
                                    "lots": 1, "entry": 2544.75, "last": 2551.0, "float": -63.0, "margin": 2806.0}],
                    "trades": [{"ts": "2026-09-09 15:36", "sym": "SM", "contract": "SM611", "dir": "空",
                                "lots": 1, "leg": "开仓", "price": 5939.41, "fee": 2.1, "realized": 0.0,
                                "forced": 0, "reason": "信号开仓"}],
                    "orders": []}},
        {"name": "10万_基准", "tier": "10万", "style": "基准", "equity0": 100_000, "equity": 100_100,
         "ret": 0.001, "max_drawdown": -0.01, "risk_degree": 0.03, "n_fut_pos": 1, "n_opt_pos": 0,
         "n_closed": 0, "n_pending": 0, "realized": 0.0, "fees": 0.0, "fill_mode": "next",
         "priority": "futures_first", "entry_score": 4.0,
         "detail": {"positions": [], "trades": [], "orders": []}},
        {"name": "1万_激进", "tier": "1万", "style": "激进", "equity0": 10_000, "equity": 10_000,
         "ret": 0.0, "max_drawdown": 0.0, "risk_degree": 0.0, "n_fut_pos": 0, "n_opt_pos": 0,
         "n_closed": 0, "n_pending": 0, "realized": 0.0, "fees": 0.0, "fill_mode": "close",
         "priority": "futures_first", "entry_score": 3.5,
         "detail": {"positions": [], "trades": [], "orders": []}},
    ]
    (tmp_path / "reports" / "paper_compare.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    html = report._paper_compare_html()
    # 第105轮结构断言
    assert "tier-cards" in html                       # 顶部档位卡
    assert "10万档" in html                            # 档位卡标签
    assert "10万_基准" in html and "100,100.0" in html  # 统一权益渲染
    assert "baseline" in html                          # 基准行高亮 class
    assert "b-badge" in html                           # ⓑ 徽章
    assert "paper_detail_10万_激进.html" in html       # 点击链接详情页
    assert "统一资金池" in html                         # 页脚
    assert "期权权益" not in html                       # 旧列已删
    # details 折叠（10万_激进 有持仓和成交）
    assert html.count("<details") >= 1                # 至少1个 details
    assert "展开明细" in html
    # 账户 HTML 配对
    # 档位卡 + 档位聚合折叠 + 向后兼容（旧 rows 也应渲染空占位）
    assert "查看该档成交/挂单聚合" in html
    assert "该账户暂无持仓/成交/挂单记录" in html
    assert html.count("<details") == html.count("</details>")
    assert html.count("<table") == html.count("</table>")


def test_paper_compare_html_new_detail_sections(tmp_path, monkeypatch):
    """第111轮：对比页新增资金概览/绩效指标/成交汇总折叠区 + 委托状态徽章 +
    数字表头右对齐(th-num) + 可交易金额显示 + 页尾合计。"""
    import json
    monkeypatch.setattr(report.config, "BASE_DIR", str(tmp_path))
    (tmp_path / "reports").mkdir(exist_ok=True)
    rows = [
        {"name": "10万_基准", "tier": "10万", "style": "基准", "equity0": 100_000, "equity": 100_376,
         "ret": 0.00376, "max_drawdown": -0.02, "risk_degree": 0.21, "n_fut_pos": 2, "n_opt_pos": 0,
         "n_closed": 1, "n_pending": 1, "realized": 123.0, "fees": 15.0, "fill_mode": "next",
         "priority": "futures_first", "entry_score": 4.0,
         "static": 100_200.0, "float_pnl": 176.0, "margin_used": 21_000.0, "available": 79_200.0,
         "ann_ret": 0.12, "sharpe": 1.5, "win_rate": 0.55, "pl_ratio": 1.8, "profit_factor": 1.9,
         "n_trades": 20, "avg_risk": 0.10,
         "n_liquidations": 0, "n_skipped": 3,
         "status": {"pending": 1, "filled": 5, "blocked": 0, "rejected": 2, "cancelled": 1},
         "notional": 500_000.0, "slip_yuan": 12.5, "n_opens": 6, "n_closes": 4,
         "affordable_sym": "玉米", "affordable_margin": 1925.0,
         "detail": {"positions": [], "trades": [], "orders": []}},
    ]
    (tmp_path / "reports" / "paper_compare.json").write_text(json.dumps(rows), encoding="utf-8")
    html = report._paper_compare_html()
    # 折叠区新网格
    assert "账户资金概览" in html and "静态权益" in html and "可用资金" in html
    assert "绩效指标" in html and "年化" in html and "盈亏比" in html
    assert "成交汇总" in html and "名义总额" in html and "滑点" in html
    # 状态徽章 + 对齐 + 可交易金额 + 页尾合计
    assert "st-badges" in html and "拒单 2" in html
    assert "th-num" in html
    assert "玉米(1,925/手)" in html
    assert "全账户合计" in html
    # 回撤统一绝对值口径
    assert "2.00%" in html
    assert html.count("<details") == html.count("</details>")
    assert html.count("<table") == html.count("</table>")


def test_write_paper_account_cmp_rows_carries_new_fields(tmp_path, monkeypatch):
    """第111轮：paper_compare.json 每条记录携带资金四维/绩效/委托状态/成交汇总字段。"""
    import json as _json
    monkeypatch.setattr(report.config, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(report.config, "PAPER_ACCOUNT_TXT",
                        str(tmp_path / "reports" / "paper_account.txt"))
    (tmp_path / "reports").mkdir(exist_ok=True)
    st = _active_state()
    st.papers = {"基准": st.paper}
    st.last_papers = {}
    report.write_paper_account(st)
    cmp_path = tmp_path / "reports" / "paper_compare.json"
    assert cmp_path.exists()
    rows = _json.loads(cmp_path.read_text(encoding="utf-8"))
    assert rows and len(rows) == 1
    r = rows[0]
    for k in ("static", "float_pnl", "margin_used", "available", "ann_ret", "sharpe",
              "win_rate", "profit_factor", "n_trades", "avg_risk", "n_liquidations",
              "n_skipped", "status", "notional", "slip_yuan", "n_opens", "n_closes"):
        assert k in r, "cmp_rows 缺少字段 %s" % k


def test_paper_account_text_unified_no_opt_pool_section(monkeypatch):
    """第104轮统一展示：paper_account.txt 无期权持仓时不输出"期权账户（独立资金池）"段。"""
    st = _active_state()
    st.last_paper = st.paper.on_cycle("2026-09-09 10:00:00", [row()])
    text = report.paper_account_text(st)
    assert "期权账户（独立资金池）" not in text
    assert "独立资金池" not in text
    # 期权有持仓时才显示"期权持仓明细"（统一池内明细）——此处合成直接挂 opt 明细验证不崩溃
    st2 = _active_state()
    st2.paper.opt_positions = {"oRB-1": {"status": "open", "sym": "RB", "strike": 3000.0,
                                          "cp": "call", "fill_prem": 6.0, "lots": 1,
                                          "multiplier": 10, "days_left": 10}}
    st2.last_paper = st2.paper.on_cycle("2026-09-09 10:00:00", [row()])
    text2 = report.paper_account_text(st2)
    assert "期权持仓明细" in text2
    assert "独立资金池" not in text2                   # 不再自称独立池


def test_paper_detail_html_generation():
    """第105轮：二级详情页生成（ECharts 容器 + 持仓/成交表头 + 账户信息条，纯内存假对象）。"""
    class _DB:
        def paper_equity_series(self, limit=600):
            return [{"ts": "2026-09-09 10:00:00", "equity": 100000.0, "risk_degree": 0.1, "drawdown": 0.0},
                    {"ts": "2026-09-09 10:05:00", "equity": 101000.0, "risk_degree": 0.2, "drawdown": -0.01}]

        def paper_trades_recent(self, limit=50):
            return [{"ts": "2026-09-09 10:00:00", "sym": "RB", "contract_code": "RB2610", "dir_text": "多",
                     "lots": 1, "leg": "开仓", "price": 3000.0, "fee_yuan": 1.5, "realized_yuan": 0.0,
                     "forced": 0, "reason": "信号开仓"}]

        def paper_orders_recent(self, limit=50):
            return []

    class _Broker:
        name = "10万_激进"
        entry_score = 4.0
        priority = "futures_first"
        fill_mode = "close"

        def __init__(self):
            self.db = _DB()

        def account_summary(self):
            return {"equity0": 100000, "equity": 101000, "static": 100000, "float_pnl": 1000,
                    "realized": -50, "fees_paid": 15, "margin_used": 20000, "available": 81000,
                    "risk_degree": 0.25, "n_positions": 1, "n_pending": 0, "n_closed": 1,
                    "n_liquidations": 0, "n_skipped": 0,
                    "status": {"filled": 1, "pending": 0, "blocked": 0, "rejected": 0, "cancelled": 0},
                    "fill_mode": "close", "performance": {"max_dd": -0.01},
                    "opt": {"n_positions": 0, "realized": 0.0, "fees_paid": 0.0, "n_skipped": 0},
                    "opt_equity": None, "opt_float_pnl": None, "opt_equity0": None}

        def positions_view(self):
            return [{"sym": "RB", "contract_code": "RB2610", "name": "螺纹钢", "dir": "多", "lots": 1,
                     "entry_dt": "2026-09-09 10:00:00", "entry_price": 3000.0, "last": 3010.0,
                     "float_yuan": 100.0, "margin": 3000.0, "score": 5.0}]

        def pending_view(self):
            return []

    class _State:
        pass

    # 由于 _paper_detail_html(state, broker, name, a) 需要 account_summary 的 a 参数，以 broker 为回调
    b = _Broker()
    st = _State()
    html = report._paper_detail_html(st, b, "10万_激进", b.account_summary())
    assert "归一化净值" in html and "charts" in html       # ECharts 容器
    assert "echarts.min.js" in html                          # 引入本地 ECharts
    assert "RB2610" in html and "持仓" in html              # 持仓表
    assert "成交" in html and "3,000.00" in html            # 成交表（千分位格式）
    assert "10万_激进" in html and "数据源" in html         # 账户信息条


def test_paper_tier_baselines_text():
    """第105轮：paper_account.txt 顶部 5 档基准摘要（各资金档含基准风格账户）。"""
    b10j = _active_broker(100_000, "close", 3.0)
    b10z = _active_broker(100_000, "next", 4.0)
    b1z = _active_broker(10_000, "next", 4.5)
    class _State:
        pass
    st = _State()
    st.papers = {"10万_激进": b10j, "10万_基准": b10z, "1万_基准": b1z}
    txt = report._paper_tier_baselines_text(st)
    assert "10万档" in txt and "1万档" in txt
    assert "10万_基准" in txt and "【基准】" in txt
    assert "10万_激进" in txt and "【激进】" in txt
    assert "各金额档基准账户" in txt


def _active_broker(eq0, fill, entry):
    import paper_broker as _pb
    return _pb.PaperBroker(db=None, fill_mode=fill, equity0=eq0, entry_score=entry,
                           margin_table={"RB": {"broker_margin": 0.1, "limit_basic": 0.05, "multiplier": 10}},
                           fee_table={"RB": {"multiplier": 10, "open_amt_rate": 1e-4, "open_per_lot": 3.0,
                                             "close_amt_rate": 1e-4, "close_per_lot": 3.0,
                                             "today_amt_rate": 0.0, "today_per_lot": 0.0}},
                           sector_of={"RB": "黑色"}, slip_rate=0.0, restore=False, name="x")


def test_paper_gambler_style_and_config():
    """第106轮：赌徒账户参数（5档各1个，opt_premium_ratio=0.75/止损0.70/option_first/期权数量无上限）
    与 _paper_style_of 识别。"""
    import config as _cfg
    accounts = getattr(_cfg, "PAPER_ACCOUNTS", [])
    gamblers = [a for a in accounts if a["name"].endswith("赌徒")]
    assert len(gamblers) == 5, f"每资金档应有1个赌徒账户，got {len(gamblers)}"
    assert len(accounts) >= 20, f"至少20个账户，got {len(accounts)}"
    for g in gamblers:
        assert abs(float(g.get("opt_premium_ratio", 0)) - 0.75) < 1e-9, (g["name"], "权利金占比应75%")
        assert abs(float(g.get("stop_loss_ratio", 0)) - 0.70) < 1e-9, (g["name"], "止损应70%")
        assert g.get("priority") == "option_first", (g["name"], "赌徒应期权优先(option_first)")
        assert g.get("options_max") is None, (g["name"], "期权持仓应无上限")
    # 风格识别
    assert report._paper_style_of("10万_赌徒", "close", 3.0) == "赌徒"
    assert report._paper_style_of("10万_激进", "close", 3.0) == "激进"
    assert report._paper_style_of("10万_基准", "next", 4.0) == "基准"
    assert report._paper_style_of("10万_保守", "next", 5.0) == "保守"
    # 各档 opt_premium_ratio 梯度确认（激进80 > 基准(50-60) > 保守35；赌徒75）
    for g in gamblers:
        tier = g["name"].split("_")[0]
        mates = {a["name"].split("_")[1]: a for a in accounts if a["name"].startswith(tier + "_")}
        if tier == "10万":
            assert abs(float(mates["激进"]["opt_premium_ratio"]) - 0.80) < 1e-9
            assert abs(float(mates["基准"]["opt_premium_ratio"]) - 0.60) < 1e-9
            assert abs(float(mates["保守"]["opt_premium_ratio"]) - 0.35) < 1e-9
        elif tier == "1万":
            assert abs(float(mates["激进"]["opt_premium_ratio"]) - 0.80) < 1e-9
            assert abs(float(mates["基准"]["opt_premium_ratio"]) - 0.50) < 1e-9
            assert abs(float(mates["保守"]["opt_premium_ratio"]) - 0.35) < 1e-9
        elif tier == "5000":
            assert abs(float(mates["激进"]["opt_premium_ratio"]) - 0.80) < 1e-9
            assert abs(float(mates["基准"]["opt_premium_ratio"]) - 0.60) < 1e-9
            assert abs(float(mates["保守"]["opt_premium_ratio"]) - 0.35) < 1e-9
        else:  # 3000 / 1000
            assert abs(float(mates["激进"]["opt_premium_ratio"]) - 0.80) < 1e-9
            assert abs(float(mates["基准"]["opt_premium_ratio"]) - 0.50) < 1e-9
            assert abs(float(mates["保守"]["opt_premium_ratio"]) - 0.35) < 1e-9


# ---------------- 第110轮：target_basis 配置 + 可交易性字段 ----------------

def test_paper_accounts_all_have_target_basis_margin():
    """第110轮：全部 20 个纸面账户 target_basis='margin'（保证金口径 sizing）。"""
    accounts = config.PAPER_ACCOUNTS
    assert len(accounts) >= 20
    assert all(a.get("target_basis") == "margin" for a in accounts), \
        [a["name"] for a in accounts if a.get("target_basis") != "margin"]


def test_paper_accounts_margin_tier_affordability():
    """第110轮：低档位 per_symbol 按新表核算——1万/5000/3000 可开 1 手玉米，1000 纯期权。"""
    accounts = {a["name"]: a for a in config.PAPER_ACCOUNTS}
    # 玉米一手保证金≈1588（现价2269×0.07×10），目标预算 = equity0 × per_symbol
    cases = {
        "1万_保守": (10_000, 0.16), "5000_基准": (5_000, 0.32), "5000_保守": (5_000, 0.32),
        "3000_激进": (3_000, 0.54), "3000_赌徒": (3_000, 0.59),   # 第141轮：赌徒 per_symbol 上调 0.5
    }
    for name, (eq0, ps) in cases.items():
        a = accounts[name]
        assert abs(float(a["per_symbol"]) - ps) < 1e-9, (name, "per_symbol 应按新表核算")
        assert eq0 * ps >= 1580, (name, "目标保证金预算应 ≥ 一手玉米保证金")
    # 1000 档 option_only 期货不可用
    for name in ("1000_激进", "1000_基准", "1000_保守"):
        assert accounts[name]["priority"] == "option_only"
        assert accounts[name]["futures_max"] == 0


def test_paper_affordable_helper():
    """_paper_affordable：低档位返回最便宜可交易品种；option_only 返回 None。"""
    from portfolio import load_margin_schedule
    _margins = load_margin_schedule()
    if not _margins:
        pytest.skip("futures_margins.csv 不存在或为空，跳过")

    class _B:
        priority = "futures_first"
        futures_max = None
        def __init__(self, eq0, ps, msw):
            self.pf = Portfolio(eq0, _margins, sizing="equal_notional", per_symbol=ps,
                                max_symbol_weight=msw, target_basis="margin")
            self.pf._last_prices = {"C": 2269.0, "M": 2850.0}

    b = _B(5_000, 0.32, 0.50)
    sym, cost = report._paper_affordable(b, 5_000)
    assert sym and "玉米" in sym and cost and cost > 0
    # option_only 返回 None
    b2 = _B(1_000, 0.50, 0.55)
    b2.priority = "option_only"
    b2.futures_max = 0
    assert report._paper_affordable(b2, 1_000) == (None, None)
