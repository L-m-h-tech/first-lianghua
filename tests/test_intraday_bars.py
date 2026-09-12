# -*- coding: utf-8 -*-
"""分钟K周期聚合 + 合约代码构造回归（第14轮 WP-D0，纯函数零网络）。"""
import intraday_bars as ib


def _bar(dt, o, h, l, c, v=10, amount=1000):
    return {"dt": dt, "o": o, "h": h, "l": l, "c": c, "v": v, "amount": amount,
            "sym": "RB", "contract": "RB0", "period": 1}


def test_aggregate_basic_ohlcv():
    bars = [_bar("2026-09-01 09:0%d" % i, 100 + i, 102 + i, 99 + i, 101 + i) for i in range(1, 6)]
    out = ib.aggregate_bars(bars, 1, 5)
    assert len(out) == 1
    m = out[0]
    assert m["o"] == bars[0]["o"] and m["c"] == bars[-1]["c"]
    assert m["h"] == max(b["h"] for b in bars)
    assert m["l"] == min(b["l"] for b in bars)
    assert m["v"] == 50 and m["amount"] == 5000
    assert m["dt"] == bars[-1]["dt"] and m["period"] == 5


def test_aggregate_does_not_merge_across_break():
    # 09:02 -> 09:05 跨午休/缺口，不连续段不硬拼；factor=2 只合并前两根
    bars = [_bar("2026-09-01 09:01", 1, 2, 0, 1),
            _bar("2026-09-01 09:02", 1, 2, 0, 1),
            _bar("2026-09-01 09:05", 1, 2, 0, 1)]
    out = ib.aggregate_bars(bars, 1, 2)
    assert len(out) == 1 and out[0]["dt"] == "2026-09-01 09:02"


def test_aggregate_trailing_partial_dropped():
    bars = [_bar("2026-09-01 09:0%d" % i, 1, 2, 0, 1) for i in range(1, 8)]  # 7根，factor=3
    out = ib.aggregate_bars(bars, 1, 3)
    assert len(out) == 2                              # 末根零散不合成半根周期


def test_aggregate_factor_one_copies():
    bars = [_bar("2026-09-01 09:01", 1, 2, 0, 1)]
    out = ib.aggregate_bars(bars, 1, 1)
    assert len(out) == 1 and out[0] is not bars[0]    # 浅拷贝、非同一对象


def test_aggregate_skips_bad_dt():
    bars = [_bar("bad", 1, 2, 0, 1), _bar("2026-09-01 09:01", 1, 2, 0, 1),
            _bar("2026-09-01 09:02", 1, 2, 0, 1)]
    assert len(ib.aggregate_bars(bars, 1, 2)) == 1


# ---------- 第130轮：时段锚点分桶（修复第121轮"聚合相位对齐仅覆盖1m"存疑） ----------

def _range_bars(start_h, start_m, n, base=1, day="2026-09-01"):
    """从 start_h:start_m 起连续 n 根 base 分钟 bar（bar_dt 为桶末口径）。"""
    from datetime import datetime, timedelta
    t0 = datetime.strptime("%s %02d:%02d" % (day, start_h, start_m), "%Y-%m-%d %H:%M")
    out = []
    for i in range(n):
        t = t0 + timedelta(minutes=base * (i + 1))     # 第 i 根的末时间
        out.append(_bar(t.strftime("%Y-%m-%d %H:%M"), 100, 101, 99, 100))
    return out


def test_anchor_phase_realigned_from_mid_window():
    # 取数窗口从 21:13 起（不在 5m 边界）：旧法整段漂移（21:17/21:22...），
    # 锚点法：21:13/21:14 属 (21:10,21:15] 不满桶丢弃，21:15..21:19 满桶 → 21:20
    bars = _range_bars(21, 13, 12)                    # 21:14..21:25? 末根 21:13+12min=21:25
    out = ib.aggregate_bars(bars, 1, 5, session_starts=ib.SESSION_STARTS)
    dts = [b["dt"] for b in out]
    assert dts == ["2026-09-01 21:20", "2026-09-01 21:25"], dts


def test_anchor_cross_midnight_uses_prev_day_2100():
    # 凌晨 01:01..01:05（夜盘跨日）：锚点回溯前一日 21:00 → 桶 (01:00,01:05] → dt 01:05
    bars = _range_bars(0, 58, 7)                      # 00:59..01:05
    out = ib.aggregate_bars(bars, 1, 5, session_starts=ib.SESSION_STARTS)
    assert [b["dt"] for b in out] == ["2026-09-01 01:05"], out


def test_anchor_60m_boundary_1030():
    # 60m 聚合：10:30 时段锚点（旧 minute%60 取模判不出 10:30/13:30 边界）
    bars = _range_bars(10, 30, 60)                    # 10:31..11:30 共 60 根
    out = ib.aggregate_bars(bars, 1, 60, session_starts=ib.SESSION_STARTS)
    assert [b["dt"] for b in out] == ["2026-09-01 11:30"], out


def test_anchor_partial_buckets_dropped_not_fabricated():
    # 段头/段尾不足整桶一律丢弃（不编造半根）：09:31..09:34（4根<5）→ 无输出
    bars = _range_bars(9, 31, 4)
    assert ib.aggregate_bars(bars, 1, 5, session_starts=ib.SESSION_STARTS) == []


def test_anchor_contiguous_segment_boundary_1015():
    # 跨 10:15-10:30 休市：两段各自锚定（09:00 段 09:31..10:15 全满桶、10:30 段头不足整桶丢弃）
    bars = _range_bars(9, 30, 45) + _range_bars(10, 31, 10)   # 09:31..10:15 + 10:32..10:41
    out = ib.aggregate_bars(bars, 1, 5, session_starts=ib.SESSION_STARTS)
    dts = [b["dt"] for b in out]
    assert dts[0] == "2026-09-01 09:35" and dts[-1] == "2026-09-01 10:40"
    assert "2026-09-01 10:15" in dts and "2026-09-01 10:40" in dts
    assert all(d <= "2026-09-01 10:15" or d >= "2026-09-01 10:40" for d in dts)
