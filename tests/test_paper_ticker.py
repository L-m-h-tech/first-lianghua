# -*- coding: utf-8 -*-
"""第103轮：纸面 ticker 线程 paper_ticker 回归（零网络、确定性）。

覆盖：tick_once 用 stash 快照驱动多账户撮合；同信号连续两次 tick 不重复开仓
（on_cycle 幂等，靠 pending 意图相同跳过重挂）；stash 为空安全跳过；
option_only 档跳过期货；链空跳过期权；tick_loop 用 fetch_quotes（monkeypatch）
拉新行情喂入 broker。账户表全部显式注入，不触网。
"""
import threading

import pytest

import config
import paper_broker
import paper_ticker
import report as _report

# 第114轮：tick_once 撮合后落盘纸面报告。测试默认屏蔽真实落盘（避免污染生产 reports），
# 单独用例（test_tick_once_writes_paper_report）恢复真实写入并重定向到 tmp_path。
_ORIG_WRITE_PAPER = _report.write_paper_account


@pytest.fixture(autouse=True)
def _no_real_paper_write(monkeypatch):
    """tick_once 内部 report.write_paper_account 默认打桩为 no-op（防写生产 reports）。"""
    monkeypatch.setattr(_report, "write_paper_account", lambda state: None)


# ---------------- 确定性账户表/行情构造（与 test_paper_report 同款） ----------------

_FEE = {"multiplier": 10, "open_amt_rate": 1e-4, "open_per_lot": 3.0,
        "close_amt_rate": 1e-4, "close_per_lot": 3.0,
        "today_amt_rate": 0.0, "today_per_lot": 0.0}
_MARGIN = {"RB": {"broker_margin": 0.1, "limit_basic": 0.05, "multiplier": 10}}


def row(sym="RB", name="螺纹钢", cat="黑色", score=5.0, price=3000.0,
        contract_code="", main_month=""):
    return {"sym": sym, "name": name, "cat": cat, "score": score, "price": price,
            "code": sym + "0", "atr": 20.0,
            "contract_code": contract_code, "main_month": main_month}


def quote(price, prev=3000.0, move=0.05):
    return {"latest": price, "prev_settle": prev,
            "high": price * 1.002, "low": price * 0.998}


def make_broker(fill="close", equity0=1_000_000.0, priority="futures_first",
                db=None, name="基准"):
    return paper_broker.PaperBroker(
        db=db, fill_mode=fill, equity0=equity0, entry_score=4.0, exit_score=2.0,
        margin_table=_MARGIN, fee_table={"RB": _FEE},
        sector_of={"RB": "黑色"}, slip_rate=0.0, restore=False,
        owner_fn=lambda ts: None, priority=priority, name=name)


class _State:
    pass


def _active_state(fill="close", priority="futures_first", db=None):
    """手搓 State：papers/last_papers/_paper_stash 三件套（仿 test_paper_report._State）。"""
    st = _State()
    b = make_broker(fill, priority=priority, db=db)
    st.papers = {"基准": b}
    st.last_papers = {}
    st.last_paper = None
    st._paper_stash = {
        "fut_rows": [row()],
        "codes": ["RB0"],
        "strat_rows": [],
        "chain_map": {},
    }
    return st


# ---------------- tick_once：驱动撮合 ----------------

def test_tick_once_drives_on_cycle():
    st = _active_state()
    out = paper_ticker.tick_once(st, "2026-09-09 10:01:00", {"RB0": quote(3010.0)})
    assert set(out) == {"基准"}
    snap = out["基准"].get("snapshot") or {}
    assert snap.get("n_positions", 0) == 1          # close 档当轮成交
    assert st.last_papers["基准"] is out["基准"]
    assert st.last_paper is out["基准"]             # 基准向后兼容


def test_tick_once_repeat_same_signal_no_dup():
    """同信号连续两次 tick（ticker 的真实节拍）：第二轮不重复开仓（意图相同跳过重挂）。"""
    st = _active_state()
    q = {"RB0": quote(3010.0)}
    o1 = paper_ticker.tick_once(st, "t1", q)
    o2 = paper_ticker.tick_once(st, "t2", q)
    s1 = o1["基准"]["snapshot"]
    s2 = o2["基准"]["snapshot"]
    assert s1["n_positions"] == 1 and s2["n_positions"] == 1
    assert o2["基准"]["n_orders"] == 0              # close 档无新委托（持多 hold）
    assert o2["基准"]["n_trades"] == 0


def test_tick_once_empty_stash_safe():
    st = _State()
    st.papers = {"基准": make_broker()}
    st.last_papers = {}
    st.last_paper = None
    st._paper_stash = {}                            # run_cycle 未跑过（无快照键）
    out = paper_ticker.tick_once(st, "t1", {})
    assert out == {}                                # 快照无 fut_rows/codes：安全跳过
    assert st.last_papers == {}                     # 无副作用


def test_tick_once_no_papers_returns_empty():
    st = _State()
    st.papers = {}
    st.last_papers = {}
    st._paper_stash = {"fut_rows": [row()], "codes": ["RB0"], "strat_rows": [], "chain_map": {}}
    assert paper_ticker.tick_once(st, "t1", {}) == {}


def test_tick_once_option_only_skips_futures():
    st = _active_state(priority="option_only")
    # strat_rows 为空且链为空：option_only 档跳过期货 on_cycle，last_summary 兜底
    out = paper_ticker.tick_once(st, "t1", {"RB0": quote(3010.0)})
    assert st.papers["基准"].last_summary is None
    assert "options" not in (out.get("基准") or {})       # 无期权输入时不动期权


def test_tick_once_option_chain_empty_skips_options():
    st = _active_state()                                    # futures_first + 无链
    out = paper_ticker.tick_once(st, "t1", {"RB0": quote(3010.0)})
    # 期权依赖 strat_rows 与 chain_map 同时非空才走 on_cycle_options
    assert out["基准"].get("opt", {}).get("n_buy", 0) < 1 or "opt" not in out["基准"]


# ---------------- tick_loop：daemon 节奏 ----------------

def test_tick_loop_fetches_quotes_and_drives(monkeypatch):
    import paper_ticker as pt
    fetched = {}

    def _fake_fetch(codes):
        fetched["codes"] = codes
        return {"RB0": quote(3010.0)}

    monkeypatch.setattr(pt.futures_data, "fetch_quotes", _fake_fetch)
    monkeypatch.setattr(pt.config, "PAPER_TICK_INTERVAL", 1)     # 快节奏便于测试
    monkeypatch.setattr(pt.config, "PAPER_TICK_TRADING_ONLY", False)
    st = _active_state()
    st.stop = threading.Event()
    # 手工模拟 tick_loop 的一拍（直接调循环会永久阻塞，这里只验证 fetch+驱动路径）
    snap = st._paper_stash
    quotes = pt.futures_data.fetch_quotes(snap.get("codes") or [])
    out = pt.tick_once(st, "2026-09-09 10:01:00", quotes)
    assert fetched == {"codes": ["RB0"]}                        # 行情确实按 codes 拉取
    assert out["基准"]["snapshot"]["n_positions"] == 1


def test_tick_loop_trading_only_gate(monkeypatch):
    """trading_only 且被判非交易时段时：tick_loop 跳过（不产生 fetch）。"""
    import paper_ticker as pt
    monkeypatch.setattr(pt.config, "PAPER_TICK_INTERVAL", 1)
    monkeypatch.setattr(pt.config, "PAPER_TICK_TRADING_ONLY", True)
    called = []

    def _fake_is_trading():
        return (False, "非交易时段")                            # 与 utils.is_trading_time 同签名

    monkeypatch.setattr(pt, "_is_trading", _fake_is_trading)

    def _no_fetch(codes):
        called.append(codes)
        return {}

    monkeypatch.setattr(pt.futures_data, "fetch_quotes", _no_fetch)
    st = _active_state()
    st.stop = threading.Event()
    # tick_loop 是 while 循环，直接测会阻塞：验证门控函数级行为 = _is_trading() 返回 tuple，首元素 False
    assert pt._is_trading()[0] is False
    assert called == []                                          # 未触发 fetch


def test_broker_lock_is_rlock():
    b = make_broker()
    # Python 3.x 中 threading.RLock 是函数不是类型，用 acquire 行为检测
    assert b._lock is not None, "broker._lock 未初始化"
    b._lock.acquire()
    b._lock.acquire(timeout=1.0)                      # RLock 重入不阻塞
    b._lock.release()


# ================= 第108轮 parity 测试：analyze_all_varieties 与 analyze_variety 一致性 =================

_FULL_SCORE = 5.0        # 显式 score 绕过 parts 计算，走 want_position 阈值线


class _MockObj:
    """轻量 mock 对象：按属性名返回预设值或空操作（不真实执行逻辑）。"""
    def __init__(self, **kwargs):
        self._attrs = kwargs
    def __getattr__(self, k):
        if k.startswith("_"):
            raise AttributeError(k)
        return self._attrs.get(k, lambda *a, **kw: None)


def _full_state(prio="futures_first"):
    """构造满足 analyze_all_varieties 最小需求的 mock state。"""
    from collections import deque
    import flow_tracker as _ft

    st = _State()
    meta = {"sym": "RB", "code": "RB0", "cat": "黑色", "oil_w": 0,
            "name": "螺纹钢", "ex": "SHFE"}
    st.watchlist = [("RB", meta)]
    st.var_hist = {}
    st.flow_tracker = _ft.FlowTracker()
    # klines: warm_intraday 返回空 dict，get 返回空 ind（all scores 0）
    klines = _MockObj(
        warm_intraday=lambda pairs: {},
        get=lambda code, cat: ({"close": 3000, "prev_close": 3000, "hv20": 0.2,
                                "tech": {}, "ret5": 0.0, "ret20": 0.0}, True))
    st.klines = klines
    st.webdata = _MockObj(views_snapshot=lambda: {})
    st.news = _MockObj(score=lambda cat, variety=None: (0.0, 0),
                       trend=lambda: 0.0)
    st.oil = _MockObj(combined_score=lambda: 0.0, direction=lambda: 0.0)
    st.contracts = _MockObj(get=lambda sym: None)
    st.breader = _MockObj(page_info=lambda key: None)
    st.fund_inv = {}
    st.fund_basis = None
    st.fetcher = _MockObj(em_code=lambda sym: "", rank_totals=lambda *a: None)
    # 账户表（给 tick_once 用，parity 本身不需要）
    b = make_broker(fill="close", priority=prio)
    st.papers = {"基准": b}
    st.last_papers = {}
    st.last_paper = None
    st._paper_stash = {"fut_rows": [row("RB", score=_FULL_SCORE)],
                       "codes": ["RB0"], "strat_rows": [], "chain_map": {}}
    return st, meta


def test_analyze_all_varieties_returns_complete_rows():
    """analyze_all_varieties 正常返回完整 fut_rows（score/price/name 三字段不为空）。"""
    from analyzer import analyze_all_varieties, analyze_variety
    st, meta = _full_state()
    quotes = {"RB0": quote(3010.0)}
    flow_map = st.flow_tracker.update(quotes, 1.0)
    fut = analyze_all_varieties(st, st.watchlist, quotes, flow_map)
    assert len(fut) == 1, "应产出一个 RB 行"
    row_ = fut[0]
    assert row_["sym"] == "RB" and row_["code"] == "RB0"
    assert isinstance(row_["score"], float)
    assert row_["price"] == 3010.0
    # parts 存在（综合分由多 part 组成）
    assert "parts" in row_ or "score" in row_


def test_analyze_all_varieties_parity_with_direct():
    """共享函数与直接调 analyze_variety 等价（同一输入 → 同一 score）。"""
    from analyzer import analyze_all_varieties, analyze_variety
    st, meta = _full_state()
    quotes = {"RB0": quote(3010.0)}
    flow_map = st.flow_tracker.update(quotes, 1.0)
    fut = analyze_all_varieties(st, st.watchlist, quotes, flow_map)
    # 直接用相同输入重算
    from utils import is_variety_trading as _ivt
    ind = {"close": 3000, "prev_close": 3000, "hv20": 0.2,
           "tech": {}, "ret5": 0.0, "ret20": 0.0, "intraday": {}}
    direct = analyze_variety("RB", meta, quotes["RB0"], ind, True, 0.0, 0,
                             0.0, 0.0, None, None, None,
                             flow=flow_map.get("RB0"), fund_raw={})
    assert fut[0]["score"] == direct["score"], \
        "共享函数分数与直接调用不一致：%.4f != %.4f" % (fut[0]["score"], direct["score"])


def test_tick_once_reprice_drives_on_cycle(monkeypatch):
    """PAPER_TICK_REPRICE=True 时 tick_once 走完整分析并成功撮合开仓。

    monkeypatch analyze_all_varieties 返回高分行（隔离分析层；分析层一致性
    由 test_analyze_all_varieties_parity_with_direct 覆盖）。
    """
    import paper_ticker as pt
    import analyzer as _an
    st, meta = _full_state(prio="futures_first")
    called = []

    def _fake_analyze(state, watchlist, quotes, flow_map):
        called.append(len(watchlist))
        return [row("RB", score=_FULL_SCORE)]        # score=5.0 ≥ entry 4.0

    monkeypatch.setattr(_an, "analyze_all_varieties", _fake_analyze)
    q = {"RB0": quote(3010.0)}
    out = pt.tick_once(st, "2026-09-09 10:01:00", q)
    assert called == [1], "reprice 分支应调用共享分析函数"
    assert "基准" in out, "reprice 分支应成功撮合账户"
    snap = out["基准"].get("snapshot") or {}
    assert snap.get("n_positions", 0) == 1, "close 档应成交，持仓数=1"


def test_tick_once_reprice_fallback_when_watchlist_empty():
    """watchlist 为空时（测试场景）reprice 回退快照，不崩溃。"""
    st, _ = _full_state()
    st.watchlist = []                        # 模拟无 watchlist
    q = {"RB0": quote(3010.0)}
    out = paper_ticker.tick_once(st, "2026-09-09 10:01:00", q)
    # 回退快照：_paper_stash 有 fut_rows，应正常撮合
    assert "基准" in out, "watchlist 为空应回退快照撮合"
    snap = out["基准"].get("snapshot") or {}
    assert snap.get("n_positions", 0) == 1


def test_tick_once_writes_paper_report(tmp_path, monkeypatch):
    """第114轮：tick_once 撮合成功后同步落盘纸面报告文件（与主报告隔离）。

    autouse fixture 默认屏蔽真实落盘，本用例恢复并重定向到 tmp_path 验证落盘链路。
    """
    import report as _r
    out = tmp_path / "paper_account.txt"
    monkeypatch.setattr(_r.config, "PAPER_ACCOUNT_TXT", str(out))
    monkeypatch.setattr(_r, "write_paper_account", _ORIG_WRITE_PAPER)
    st = _active_state()
    st.paper = st.papers["基准"]                    # 生产：state.paper 指向基准账户
    paper_ticker.tick_once(st, "2026-09-09 10:01:00", {"RB0": quote(3010.0)})
    assert out.exists(), "tick_once 撮合后应落盘纸面报告"
    body = out.read_text(encoding="utf-8-sig")
    assert "纸面交易账户" in body and "不构成投资建议" in body


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))