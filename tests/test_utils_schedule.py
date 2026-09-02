# -*- coding: utf-8 -*-
"""调度与通用工具回归（对应第6/17轮合成断言，零网络、注入确定性日历）。"""
from datetime import datetime as dt, timedelta

import utils


def D(h, m, s=0, day=1):
    """2026-09-01 周二为基准构造时刻：D(时,分,秒=0,day=1=周二)。"""
    return dt(2026, 9, day, h, m, s)


# ---------------- next_transition 时段翻转点（第17轮9个边界） ----------------
def test_next_transition_open_and_breaks(flat_calendar):
    assert utils.next_transition(D(8, 30)) == D(9, 0)          # 盘前 -> 09:00
    assert utils.next_transition(D(11, 35)) == D(13, 30)       # 午休 -> 13:30（旧函数漏的下午开盘）
    assert utils.next_transition(D(10, 0)) == D(11, 30)        # 日盘上午中 -> 上午收盘
    assert utils.next_transition(D(15, 30)) == D(21, 0)        # 日盘收 -> 当晚夜盘开盘
    assert utils.next_transition(D(22, 0)) == D(2, 30, day=2)  # 夜盘中 -> 次日02:30收
    assert utils.next_transition(D(2, 0, day=2)) == D(2, 30, day=2)   # 凌晨属前夜盘
    assert utils.next_transition(D(2, 31, day=2)) == D(9, 0, day=2)   # 夜盘收后 -> 当日09:00


def test_next_transition_weekend_and_no_friday_night(flat_calendar):
    # 周六 -> 下周一09:00（09-05 周六，09-07 周一）
    assert utils.next_transition(dt(2026, 9, 5, 10, 0)) == dt(2026, 9, 7, 9, 0)
    # 周五晚无夜盘（09-04 周五）-> 下周一09:00
    assert utils.next_transition(dt(2026, 9, 4, 22, 0)) == dt(2026, 9, 7, 9, 0)


def test_is_trading_flips_at_transition(flat_calendar):
    """翻转点前后1秒 is_trading_time 必反转（核心不变量）。"""
    cases = [
        (D(8, 59, 59), False), (D(9, 0, 1), True),
        (D(11, 29, 59), True), (D(11, 30, 1), False),
        (D(13, 29, 59), False), (D(13, 30, 1), True),
        (D(14, 59, 59), True), (D(15, 0, 1), False),
        (D(20, 59, 59), False), (D(21, 0, 1), True),
        (D(2, 29, 59, day=2), True), (D(2, 30, 1, day=2), False),
    ]
    for t, expect in cases:
        flag, _ = utils.is_trading_time(t)
        assert flag is expect, (t, flag, expect)


def test_weekend_closed(flat_calendar):
    flag, desc = utils.is_trading_time(dt(2026, 9, 5, 10, 0))
    assert flag is False and desc == "周末休市"


# ---------------- next_cycle_time 刻度对齐 ----------------
def test_next_cycle_grid(flat_calendar):
    assert utils.next_cycle_time(D(9, 5)) == D(9, 10)       # 开盘前30分钟：5分钟刻度
    assert utils.next_cycle_time(D(9, 29)) == D(9, 30)      # 早段末点对齐09:30
    assert utils.next_cycle_time(D(9, 35)) == D(9, 50)      # 之后20分钟刻度
    assert utils.next_cycle_time(D(11, 31)) == D(11, 32)    # 非交易：下一整分钟
    assert utils.next_cycle_time(D(14, 59)) == D(15, 1)     # 收盘后一轮安排在15:01


def test_cycle_interval(flat_calendar):
    assert utils.cycle_interval(D(9, 10)) == 300           # 前30分钟5分钟
    assert utils.cycle_interval(D(10, 0)) == 1200          # 之后20分钟
    assert utils.cycle_interval(D(12, 0)) == 60            # 非交易60秒


# ---------------- 交易日归属 / 复盘到期 ----------------
def test_trade_owner_date(flat_calendar):
    assert utils.trade_owner_date(D(10, 0)) == dt(2026, 9, 1).date()
    assert utils.trade_owner_date(D(22, 0)) == dt(2026, 9, 1).date()       # 夜盘归开盘日
    assert utils.trade_owner_date(D(1, 0, day=2)) == dt(2026, 9, 1).date()  # 凌晨归前一交易日
    assert utils.trade_owner_date(dt(2026, 9, 5, 12, 0)) == dt(2026, 9, 4).date()  # 周六归周五


def test_review_is_due(flat_calendar):
    tue = dt(2026, 9, 1).date()       # 有夜盘 -> 次日02:30后才到期
    fri = dt(2026, 9, 4).date()       # 周五无夜盘 -> 当日15:00后到期
    assert utils.review_is_due(tue, D(2, 0, day=2)) is False
    assert utils.review_is_due(tue, D(3, 0, day=2)) is True
    assert utils.review_is_due(fri, dt(2026, 9, 4, 14, 0)) is False
    assert utils.review_is_due(fri, dt(2026, 9, 4, 16, 0)) is True


# ---------------- 品种级夜盘分档 ----------------
def test_variety_night_tiers(flat_calendar):
    rb = {"sym": "RB"}    # 多数品种23:00收
    au = {"sym": "AU"}    # 黄金02:30收
    jd = {"sym": "JD"}    # 无夜盘
    assert utils.is_variety_trading(rb, D(22, 0)) is True
    assert utils.is_variety_trading(rb, D(23, 30)) is False
    assert utils.is_variety_trading(au, D(2, 0, day=2)) is True
    assert utils.is_variety_trading(jd, D(22, 0)) is False
    assert utils.is_variety_trading(jd, D(10, 0)) is True     # 日盘所有品种一致
    assert utils.is_variety_trading(rb, D(12, 0)) is False    # 非交易时段恒False


# ---------------- 小工具 ----------------
def test_clip_and_norm():
    assert utils.clip(5, 0, 3) == 3
    assert utils.clip(-1, 0, 3) == 0
    assert utils.clip(2, 0, 3) == 2
    assert abs(utils.norm_cdf(0) - 0.5) < 1e-12
    assert abs(utils.norm_cdf(1.96) - 0.975) < 1e-3
    assert abs(utils.norm_cdf(-1.96) - 0.025) < 1e-3


def test_disp_width_and_pad():
    assert utils.disp_width("abc") == 3
    assert utils.disp_width("螺纹") == 4          # 全角中文各占2
    out = utils.pad("螺纹", 8)
    assert utils.disp_width(out) == 8


def test_sanitize_strips_control_and_emoji():
    assert utils.sanitize("a\x01b") == "ab"
    assert "\U0001f600" not in utils.sanitize("涨\U0001f600")
    assert utils.sanitize("") == ""
