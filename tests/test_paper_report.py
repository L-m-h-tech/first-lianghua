# -*- coding: utf-8 -*-
"""第28轮 G1（二）：纸面账户报告块 / paper_account.txt / 看板页签 回归（零网络、确定性）。"""
import pytest

import paper_broker
import report

_FEE = {"multiplier": 10, "open_amt_rate": 1e-4, "open_per_lot": 3.0,
        "close_amt_rate": 1e-4, "close_per_lot": 3.0,
        "today_amt_rate": 0.0, "today_per_lot": 0.0}
_MARGIN = {"RB": {"broker_margin": 0.1, "limit_basic": 0.05, "multiplier": 10}}


def row(sym="RB", name="螺纹钢", cat="黑色", score=5.0, price=3000.0):
    return {"sym": sym, "name": name, "cat": cat, "score": score, "price": price,
            "code": sym + "0", "atr": 20.0}


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
    assert block and block[0].startswith("【纸面账户·影子模拟】")
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
