# -*- coding: utf-8 -*-
"""G1 纸面交易引擎 PaperBroker 回归（第27轮，零网络、确定性）。

覆盖：三阈值迟滞/锁板/滑点纯函数；close 与 next 两档成交时点；反手先平后开；
锁板阻断顺延；双边手续费+滑点；风控强平；资金不足拒单；三表落库与进程重启恢复；
权益快照幂等；默认开关休眠。账户表全部显式注入，不依赖外部 CSV、不触网。
"""
import pytest

import config
import paper_broker as pb_mod
from paper_broker import PaperBroker, want_position, locked_at_quote, apply_slip


# ---------------- 确定性账户表/行情构造 ----------------

MARGIN = {"RB": {"broker_margin": 0.10, "limit_basic": 0.05, "multiplier": 10},
          "CU": {"broker_margin": 0.12, "limit_basic": 0.09, "multiplier": 5},
          "AU": {"broker_margin": 0.10, "limit_basic": 0.14, "multiplier": 1000}}


def _fee(sym, mult, amt=1e-4, per_lot=3.0):
    return {"multiplier": mult, "open_amt_rate": amt, "open_per_lot": per_lot,
            "close_amt_rate": amt, "close_per_lot": per_lot,
            "today_amt_rate": 0.0, "today_per_lot": 0.0}


FEE = {"RB": _fee("RB", 10), "CU": _fee("CU", 5), "AU": _fee("AU", 1000)}
SECTOR = {"RB": "黑色", "CU": "有色", "AU": "贵金属"}


@pytest.fixture
def loose(monkeypatch):
    """放宽资金/上限约束，让信号都能成交，聚焦撮合时点与状态机本身。"""
    monkeypatch.setattr(config, "PAPER_PER_SYMBOL", 0.05)
    monkeypatch.setattr(config, "PAPER_MAX_SYMBOL_WEIGHT", 1.0)
    monkeypatch.setattr(config, "PAPER_MAX_SECTOR_WEIGHT", 1.0)
    monkeypatch.setattr(config, "PAPER_MAX_CONCURRENT", 64)
    monkeypatch.setattr(config, "PAPER_RISK_LIQUIDATE", 1.0)
    monkeypatch.setattr(config, "PAPER_RISK_SAFE", 0.8)


def make_broker(fill_mode="next", equity0=10_000_000, slip=0.0001, db=None,
                restore=False, loose_on=True):
    return PaperBroker(db=db, equity0=equity0, fill_mode=fill_mode,
                       slip_rate=slip, margin_table=MARGIN, fee_table=FEE,
                       sector_of=SECTOR, restore=restore)


def row(sym, name, cat, score, price, atr=10.0):
    return {"sym": sym, "name": name, "cat": cat, "code": sym + "0",
            "score": score, "price": price, "atr": atr}


def quote(price, prev, move, locked=False):
    if locked:
        px = prev * (1 + move)
        return {"latest": px, "prev_settle": prev, "high": px, "low": px}
    return {"latest": price, "prev_settle": prev,
            "high": price * 1.002, "low": price * 0.998}


# ---------------- 纯函数 ----------------

def test_want_position_hysteresis():
    e, x = 4.0, 2.0
    assert want_position(1.0, 0, e, x) == (0, "hold")
    assert want_position(5.0, 0, e, x) == (1, "open")
    assert want_position(-5.0, 0, e, x) == (-1, "open")
    assert want_position(2.5, 1, e, x) == (1, "hold")       # 迟滞带内继续持有
    assert want_position(1.0, 1, e, x) == (0, "close")      # 跌回中性带离场
    assert want_position(-5.0, 1, e, x) == (-1, "reverse")  # 反手
    assert want_position(None, 1, e, x) == (1, "hold")      # 缺分不动作


def test_locked_at_quote():
    assert locked_at_quote(quote(None, 100, 0.05, locked=True), 0.05, True)
    assert not locked_at_quote(quote(101, 100, 0.05), 0.05, True)
    assert not locked_at_quote({"latest": 101}, 0.05, True)       # 缺昨结放行
    assert not locked_at_quote(quote(101, 100, 0.05), None, True)  # 缺幅度放行
    # 跌停封死、卖不出去
    dq = {"latest": 95.0, "prev_settle": 100.0, "high": 95.0, "low": 95.0}
    assert locked_at_quote(dq, 0.05, False)


def test_apply_slip():
    assert apply_slip(100.0, "buy", 0.0001) == pytest.approx(100.01)
    assert apply_slip(100.0, "sell", 0.0001) == pytest.approx(99.99)
    assert apply_slip(0.0, "buy", 0.1) == 0.0


# ---------------- close 档：信号轮当轮成交 ----------------

def test_close_fills_same_cycle(loose):
    pb = make_broker("close", slip=0.0)
    s = pb.on_cycle("2026-09-02 10:00:00", [row("RB", "螺纹钢", "黑色", 5.0, 3000.0)])
    assert s["n_trades"] == 1
    assert len(pb.pf.positions) == 1
    assert pb.pf.positions["RB"].direction == 1
    assert pb.pf.positions["RB"].entry_dt == "2026-09-02 10:00:00"


# ---------------- next 档：成交严格晚于信号 ----------------

def test_next_fill_strictly_after_signal(loose):
    pb = make_broker("next")
    s1 = pb.on_cycle("2026-09-02 09:05:00", [row("RB", "螺纹钢", "黑色", 5.0, 3000.0)])
    assert s1["n_trades"] == 0 and s1["n_pending"] == 1 and s1["n_positions"] == 0
    s2 = pb.on_cycle("2026-09-02 09:10:00", [row("RB", "螺纹钢", "黑色", 5.0, 3010.0)])
    assert s2["n_trades"] == 1 and s2["n_positions"] == 1
    pos = pb.pf.positions["RB"]
    assert pos.entry_dt == "2026-09-02 09:10:00"   # 成交价时间晚于信号 09:05
    assert pos.entry_price == pytest.approx(3010.0 * 1.0001)


def test_next_missing_price_keeps_pending(loose):
    pb = make_broker("next", slip=0.0)
    pb.on_cycle("2026-09-02 09:05:00", [row("RB", "螺纹钢", "黑色", 5.0, 3000.0)])
    # 下一轮无价（0），挂单保留顺延、不虚构
    s = pb.on_cycle("2026-09-02 09:10:00", [row("RB", "螺纹钢", "黑色", 5.0, 0.0)])
    assert s["n_trades"] == 0 and s["n_pending"] == 1 and len(pb.pf.positions) == 0
    # 再下一轮有价才成交
    s2 = pb.on_cycle("2026-09-02 09:15:00", [row("RB", "螺纹钢", "黑色", 5.0, 3002.0)])
    assert s2["n_trades"] == 1 and len(pb.pf.positions) == 1


def test_next_retryable_constraint_keeps_queue(monkeypatch, loose):
    # 同时持仓上限=1：CU 先成交，RB 受临时约束保持挂单顺延，且同向不重复挂、不 rejected
    monkeypatch.setattr(config, "PAPER_MAX_CONCURRENT", 1)
    pb = make_broker("next", slip=0.0)
    rows = [row("CU", "铜", "有色", 5.0, 70000.0), row("RB", "螺纹钢", "黑色", 5.0, 3000.0)]
    s1 = pb.on_cycle("t1", rows)
    assert s1["n_pending"] == 2 and s1["n_trades"] == 0
    s2 = pb.on_cycle("t2", rows)   # CU 字母序先成交占满上限，RB 顺延
    assert s2["n_trades"] == 1 and "CU" in pb.pf.positions
    assert s2["n_orders"] == 0 and s2["n_pending"] == 1   # 同向不重挂、委托不膨胀
    rb_order = pb.pending["RB"][0]
    assert rb_order["status"] == "pending" and "上限" in rb_order["reason"]
    s3 = pb.on_cycle("t3", rows)   # 仍占满，继续顺延，不产生 rejected/新委托
    assert s3["n_trades"] == 0 and s3["n_orders"] == 0 and s3["n_pending"] == 1




# ---------------- 反手先平后开 / 离场 ----------------

def test_reverse_close_then_open(loose):
    pb = make_broker("next", slip=0.0)
    pb.on_cycle("t1", [row("RB", "螺纹钢", "黑色", 5.0, 3000.0)])
    pb.on_cycle("t2", [row("RB", "螺纹钢", "黑色", 5.0, 3000.0)])     # 开多
    s3 = pb.on_cycle("t3", [row("RB", "螺纹钢", "黑色", -5.0, 3000.0)])  # 反手信号
    assert s3["n_pending"] == 2                                    # 平+开两腿
    s4 = pb.on_cycle("t4", [row("RB", "螺纹钢", "黑色", -5.0, 2990.0)])
    assert pb.pf.positions["RB"].direction == -1
    assert len(pb.pf.closed) == 1                                  # 先平掉多单


def test_exit_when_back_to_neutral(loose):
    pb = make_broker("next", slip=0.0)
    pb.on_cycle("t1", [row("RB", "螺纹钢", "黑色", -5.0, 3000.0)])
    pb.on_cycle("t2", [row("RB", "螺纹钢", "黑色", -5.0, 3000.0)])  # 开空
    pb.on_cycle("t3", [row("RB", "螺纹钢", "黑色", 1.0, 3000.0)])   # 回中性带->挂平
    assert pb.pending.get("RB") and len(pb.pending["RB"]) == 1
    pb.on_cycle("t4", [row("RB", "螺纹钢", "黑色", 1.0, 3000.0)])
    assert len(pb.pf.positions) == 0 and len(pb.pf.closed) == 1


# ---------------- 锁板阻断 / 顺延 ----------------

def test_locked_blocks_close_then_releases(loose):
    pb = make_broker("next", slip=0.0)
    pb.on_cycle("t1", [row("CU", "铜", "有色", 6.0, 70000.0)])
    # t2 涨停封死，买单无法成交、挂单顺延
    lq = {"CU0": quote(None, 70000.0, 0.09, locked=True)}
    locked_row = row("CU", "铜", "有色", 6.0, 70000.0 * 1.09)
    s2 = pb.on_cycle("t2", [locked_row], lq)
    assert s2["n_trades"] == 0 and s2["n_pending"] == 1
    # t3 打开涨停，正常成交
    s3 = pb.on_cycle("t3", [row("CU", "铜", "有色", 6.0, 70100.0)],
                     {"CU0": quote(70100.0, 70000.0, 0.09)})
    assert s3["n_trades"] == 1 and len(pb.pf.positions) == 1


# ---------------- 双边手续费 + 滑点 ----------------

def test_round_trip_costs(loose):
    pb = make_broker("close", slip=0.0001)
    pb.on_cycle("t1", [row("RB", "螺纹钢", "黑色", 5.0, 3000.0)])
    pos = pb.pf.positions["RB"]
    lots = pos.lots
    # 开仓手续费=名义×万1 + 3元/手；买入滑点抬高成本
    expect_open_fee = pos.entry_price * 10 * lots * 1e-4 + 3.0 * lots
    assert pos.open_fee_yuan == pytest.approx(expect_open_fee, rel=1e-9)
    assert pos.entry_price == pytest.approx(3000.0 * 1.0001)
    # 平仓：净盈亏=毛盈亏-开仓费-平仓费，且卖出价含下滑点
    pb.on_cycle("t2", [row("RB", "螺纹钢", "黑色", 1.0, 3050.0)])
    assert len(pb.pf.closed) == 1
    rec = pb.pf.closed[0]
    assert rec["exit_px"] == pytest.approx(3050.0 * (1 - 0.0001))
    assert rec["open_fee_yuan"] > 0 and rec["close_fee_yuan"] > 0
    assert rec["net_yuan"] == pytest.approx(
        rec["gross_yuan"] - rec["open_fee_yuan"] - rec["close_fee_yuan"])


# ---------------- 风控强平 / 资金不足拒单 ----------------

def test_forced_liquidation(loose):
    pb = make_broker("close", slip=0.0)
    pb.on_cycle("t1", [row("AU", "黄金", "贵金属", 6.0, 500.0)])
    assert len(pb.pf.positions) == 1
    pb.pf.risk_liquidate = 0.0
    pb.pf.risk_safe = 0.0
    s = pb.on_cycle("t2", [row("AU", "黄金", "贵金属", 6.0, 500.0)])
    assert len(pb.pf.positions) == 0
    assert len(pb.pf.liquidations) >= 1
    assert any(t["forced"] for t in s["trades"])


def test_insufficient_cash_rejected(loose):
    pb = make_broker("close", equity0=2000.0, slip=0.0)
    s = pb.on_cycle("t1", [row("CU", "铜", "有色", 6.0, 70000.0)])
    assert len(pb.pf.positions) == 0
    assert s["orders"][0]["status"] == "rejected"


def test_blank_inputs_safe(loose):
    pb = make_broker("close", equity0=config.PAPER_EQUITY0)
    s = pb.on_cycle("t1", [])
    assert s["n_trades"] == 0 and s["snapshot"]["equity"] == config.PAPER_EQUITY0


# ---------------- 三表落库 + 重启恢复 ----------------

def test_persistence_and_restore(loose, tmp_db):
    db = tmp_db
    pb1 = make_broker("next", db=db, restore=False)
    pb1.on_cycle("t1", [row("RB", "螺纹钢", "黑色", 5.0, 3000.0)])
    pb1.on_cycle("t2", [row("RB", "螺纹钢", "黑色", 5.0, 3000.0)])   # 开多落库
    counts = db.table_counts()
    assert counts["paper_orders"] >= 1 and counts["paper_trades"] == 1
    assert counts["paper_equity"] == 2
    # 同 ts 权益快照覆盖幂等
    pb1.on_cycle("t2", [row("RB", "螺纹钢", "黑色", 5.0, 3000.0)])
    assert db.table_counts()["paper_equity"] == 2

    # 新进程重建：持仓/手数/方向/静态权益一致
    pb2 = make_broker("next", db=db, restore=True)
    assert "RB" in pb2.pf.positions
    p1, p2 = pb1.pf.positions["RB"], pb2.pf.positions["RB"]
    assert p2.direction == p1.direction and p2.lots == p1.lots
    assert p2.entry_price == pytest.approx(p1.entry_price)
    assert pb2.pf.static_equity() == pytest.approx(pb1.pf.static_equity())


def test_restore_after_close(loose, tmp_db):
    db = tmp_db
    pb1 = make_broker("close", db=db, restore=False, slip=0.0)
    pb1.on_cycle("t1", [row("RB", "螺纹钢", "黑色", 5.0, 3000.0)])
    pb1.on_cycle("t2", [row("RB", "螺纹钢", "黑色", 1.0, 3020.0)])  # 开平各一笔
    assert len(pb1.pf.positions) == 0 and len(pb1.pf.closed) == 1
    net = pb1.pf.closed[0]["net_yuan"]
    pb2 = make_broker("close", db=db, restore=True, slip=0.0)
    assert len(pb2.pf.positions) == 0
    assert pb2.pf.realized == pytest.approx(net)   # 已实现净盈亏完整恢复


def test_restore_pending_then_fill(loose, tmp_db):
    db = tmp_db
    pb1 = make_broker("next", db=db, restore=False, slip=0.0)
    pb1.on_cycle("t1", [row("RB", "螺纹钢", "黑色", 5.0, 3000.0)])  # 只挂单
    assert pb1.pending.get("RB")
    pb2 = make_broker("next", db=db, restore=True, slip=0.0)
    assert pb2.pending.get("RB") and len(pb2.pending["RB"]) == 1    # 挂单恢复
    pb2.on_cycle("t2", [row("RB", "螺纹钢", "黑色", 5.0, 3001.0)])
    assert len(pb2.pf.positions) == 1                              # 下一轮成交


def test_default_switch_off():
    # 默认总开关关闭：main 侧据此决定是否实例化，引擎休眠是回退承诺
    assert config.PAPER_ENABLED is False
