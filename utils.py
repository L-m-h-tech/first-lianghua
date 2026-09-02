# -*- coding: utf-8 -*-
"""通用工具。
【需求⑩ + P0-1/P0-3/P0-4】交易时段判定与轮动调度：
  - 日盘 09:00-11:30 / 13:30-15:00 全品种一致；夜盘 21:00 开盘后按品种分档收市
    （多数 23:00、有色等 01:00、黄金/白银/原油 02:30），调度按"最晚 02:30"全局判定，
    单品种是否在交易用 is_variety_trading（夜盘分档/无夜盘品种各自正确）；
  - 时段前30分钟对齐5分钟刻度、之后对齐20分钟刻度、非交易时段每1分钟；
  - 交易日历（trade_calendar）识别法定节假日休市与调休；
  - RotatingFileHandler 日志轮转，monitor.log 不再无限增长。
rotation_desc 供报告块头标明本轮时间/节奏/下一轮计划。
其余为通用能力：终端安全文本、对齐、正态分布函数等。"""
import logging
import math
import os
import sys
import unicodedata
from datetime import datetime, timedelta, time as dtime
from logging.handlers import RotatingFileHandler

import trade_calendar

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG = logging.getLogger("monitor")

# 日盘两段（分钟轴）；夜盘 21:00 开盘、全局最晚次日02:30 收（分档见 config.night_end_min）
_DAY_SESSIONS = ((9 * 60, 11 * 60 + 30), (13 * 60 + 30, 15 * 60))
_NIGHT_START = dtime(21, 0)
_NIGHT_LAST_END = dtime(2, 30)          # 全局最晚收市（黄金/白银/原油）


def setup_environment():
    """初始化目录、日志（按大小轮转）、标准输出编码（避免Windows控制台GBK编码报错）"""
    import config
    for d in ("reports", "logs", "cache", "data"):
        os.makedirs(os.path.join(BASE_DIR, d), exist_ok=True)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # 显式、幂等地挂载 handler（不依赖 basicConfig "仅首次生效" 的语义，避免被更早的
    # basicConfig 抢占导致轮转文件挂不上；重复调用也不会累积重复句柄）
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [h for h in root.handlers
                     if not isinstance(h, (logging.StreamHandler, RotatingFileHandler))]
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = RotatingFileHandler(
        os.path.join(BASE_DIR, "logs", "monitor.log"),
        maxBytes=config.LOG_MAX_BYTES, backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(sh)
    root.addHandler(fh)


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _axis_minute(now):
    """now 的分钟轴（0点起算，含秒小数）"""
    return now.hour * 60 + now.minute + now.second / 60.0


def current_session(now=None):
    """当前所处的全局交易时段：返回 (start_dt, end_dt, '日盘'/'夜盘')，不在任何时段返回 None。
    - 日盘：当天须为交易日；
    - 夜盘：开盘日 21:00 起、全局最晚次日 02:30 收；凌晨段归属"前一交易日晚"的夜盘
      （周六凌晨=周五夜盘延续；周一凌晨无夜盘；法定节假日前一晚不夜盘）。"""
    now = now or datetime.now()
    d = now.date()
    t = _axis_minute(now)
    # 1) 今日日盘
    if trade_calendar.is_trade_day(d):
        for s_min, e_min in _DAY_SESSIONS:
            if s_min <= t < e_min:
                return (datetime.combine(d, dtime(s_min // 60, s_min % 60)),
                        datetime.combine(d, dtime(e_min // 60, e_min % 60)), "日盘")
        # 2) 今晚夜盘（21:00 之后，且今晚开夜盘）
        if t >= 21 * 60 and trade_calendar.has_night_session(d):
            return (datetime.combine(d, _NIGHT_START),
                    datetime.combine(d + timedelta(days=1), _NIGHT_LAST_END), "夜盘")
    # 3) 凌晨 00:00-02:30：前一交易日晚夜盘的延续
    if t < 2 * 60 + 30:
        prev_d = d - timedelta(days=1)
        if trade_calendar.is_trade_day(prev_d) and trade_calendar.has_night_session(prev_d):
            end_dt = datetime.combine(d, _NIGHT_LAST_END)
            if now < end_dt:
                return (datetime.combine(prev_d, _NIGHT_START), end_dt, "夜盘")
    return None


def _night_phase_desc(now):
    """夜盘当前阶段的文字（哪些档位的品种还在交易）"""
    t = now.hour * 60 + now.minute
    axis = t if t >= 21 * 60 else t + 1440     # 映射到 21:00 起的夜盘轴
    if axis < 23 * 60:
        return "夜盘交易时段(21:00开盘,多数品种23:00收市)"
    if axis < 24 * 60 + 60:
        return "夜盘交易时段(有色系列01:00收市,黄金/白银/原油02:30收市)"
    return "夜盘交易时段(仅黄金/白银/原油交易至02:30)"


def is_trading_time(now=None):
    """全局是否还有任意品种在交易（决定轮动节奏与报告分流）。
    返回 (是否交易时段, 时段描述)。单品种判定见 is_variety_trading。"""
    now = now or datetime.now()
    sess = current_session(now)
    if sess:
        if sess[2] == "日盘":
            return True, "日盘交易时段(09:00-11:30/13:30-15:00)"
        return True, _night_phase_desc(now)
    t = _axis_minute(now)
    if now.weekday() >= 5:
        return False, "周末休市"
    if 9 * 60 <= t <= 15 * 60 and not trade_calendar.is_trade_day(now.date()):
        return False, "法定节假日休市"
    return False, "非交易时段"


def is_variety_trading(meta, now=None):
    """某品种当前是否在其自身交易时段内：
    日盘全部品种一致；夜盘按该品种 sym 的收市分档（无夜盘品种夜盘段恒 False）。"""
    import config
    now = now or datetime.now()
    sess = current_session(now)
    if sess is None:
        return False
    if sess[2] == "日盘":
        return True
    end_min = config.night_end_min(meta["sym"])
    if end_min is None:
        return False
    t = now.hour * 60 + now.minute
    axis = t if t >= 21 * 60 else t + 1440
    return config.NIGHT_START_MIN <= axis < end_min


def cycle_interval(now=None):
    """当前应使用的轮动间隔（秒）：
    交易时段前30分钟每5分钟一轮，之后每20分钟一轮；非交易时段每1分钟一轮"""
    import config
    now = now or datetime.now()
    sess = current_session(now)
    if not sess:
        return config.REPORT_INTERVAL
    elapsed_min = (now - sess[0]).total_seconds() / 60.0
    return (config.SESSION_EARLY_INTERVAL if elapsed_min < config.SESSION_EARLY_MINUTES
            else config.SESSION_INTERVAL)


def next_session_start(now, within_days=12):
    """now 之后第一个交易时段开盘时刻（日盘09:00 或 夜盘21:00），找不到返回 None"""
    d = now.date()
    for i in range(0, within_days + 1):
        day = d + timedelta(days=i)
        if not trade_calendar.is_trade_day(day):
            continue
        cands = [datetime.combine(day, dtime(9, 0))]
        if trade_calendar.has_night_session(day):
            cands.append(datetime.combine(day, _NIGHT_START))
        for c in sorted(cands):
            if c > now:
                return c
    return None


def next_cycle_time(now=None):
    """下一轮轮动的计划时刻（真实 datetime 计算，天然支持跨零点夜盘与周六凌晨）：
      - 交易时段开盘前30分钟：对齐开盘后 5 分钟刻度；
      - 交易时段30分钟之后：对齐 20 分钟刻度（首档=开盘后30分钟）；
      - 时段最后一轮之后安排在收盘后1分钟；
      - 非交易时段：下一整分钟；若1分钟内将开盘则直接对齐开盘时刻。"""
    import config
    now = now or datetime.now()
    sess = current_session(now)
    if sess:
        s, e, _kind = sess
        early_end = s + timedelta(minutes=config.SESSION_EARLY_MINUTES)
        if now < early_end:                                  # 开盘前30分钟：5分钟刻度
            step = config.SESSION_EARLY_INTERVAL
            n = int((now - s).total_seconds() // step) + 1
            nxt = s + timedelta(seconds=n * step)
            if nxt > early_end:
                nxt = early_end
        else:                                                # 之后：20分钟刻度
            step = config.SESSION_INTERVAL
            n = int((now - early_end).total_seconds() // step) + 1
            nxt = early_end + timedelta(seconds=n * step)
        return nxt if nxt < e else e + timedelta(minutes=1)
    # 非交易时段：下一整分钟；若马上开盘（1分钟内），对齐到开盘时刻
    candidate = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    nxt_open = next_session_start(now)
    if nxt_open and nxt_open <= candidate:
        return nxt_open
    return candidate


def next_transition(now=None, within_days=12):
    """now 之后最近的一次"交易状态翻转"时刻（开盘点或收盘点），供主循环做时段边沿触发。
    - 当前正处于某交易时段：返回该时段收盘点 sess.end（交易→非交易），午休/夜盘收盘都覆盖；
    - 当前非交易：返回最近的下一个开盘点——日盘上午09:00、下午13:30、夜盘21:00 三个候选，
      交易日历过滤法定节假日、当晚是否有夜盘由 has_night_session 过滤（周五晚无夜盘、
      周六凌晨=周五夜盘延续等情形与 current_session 严格对称）；
    找不到返回 None。纯函数、可注入 now 做零网络单测。
    注意：next_session_start 只列 09:00/21:00（服务于 next_cycle_time 的1分钟对齐），
    本函数额外覆盖 13:30 下午开盘，保证午休结束也能被边沿唤醒。"""
    now = now or datetime.now()
    sess = current_session(now)
    if sess:
        return sess[1]
    d = now.date()
    for i in range(0, within_days + 1):
        day = d + timedelta(days=i)
        cands = []
        if trade_calendar.is_trade_day(day):
            cands.append(datetime.combine(day, dtime(9, 0)))     # 日盘上午开盘
            cands.append(datetime.combine(day, dtime(13, 30)))  # 日盘下午开盘
        if trade_calendar.has_night_session(day):
            cands.append(datetime.combine(day, _NIGHT_START))   # 当晚夜盘开盘
        for c in sorted(cands):
            if c > now:
                return c
    return None


def trade_owner_date(now=None):
    """当前时刻所属交易日（date）：夜盘及其次日凌晨归属"夜盘开盘日"（前一交易日）；
    非交易时段归属最近一个交易日。供日切清理与每日复盘按交易日（而非自然日）归并。"""
    now = now or datetime.now()
    sess = current_session(now)
    if sess:
        return sess[0].date()
    t = _axis_minute(now)
    if t >= 15 * 60 and trade_calendar.is_trade_day(now.date()):
        return now.date()
    return trade_calendar.prev_trade_day(now.date())


def review_is_due(owner_date, now=None):
    """归属交易日 owner_date 的全部交易是否已结束
    （有夜盘→次日02:30后；无夜盘→当日15:00后）"""
    now = now or datetime.now()
    if trade_calendar.has_night_session(owner_date):
        end_dt = datetime.combine(owner_date + timedelta(days=1), _NIGHT_LAST_END)
    else:
        end_dt = datetime.combine(owner_date, dtime(15, 0))
    return now > end_dt


def rotation_desc(now=None):
    """当前轮动节奏的文字描述（写入报告，标明本轮时间、节奏与下一轮计划时间）"""
    import config
    now = now or datetime.now()
    trading, sess_desc = is_trading_time(now)
    nxt = next_cycle_time(now).strftime("%H:%M")
    cur = now.strftime("%H:%M")
    sess = current_session(now)
    if not sess:
        return f"{sess_desc}·每1分钟分析一轮（本轮{cur}，下一轮约{nxt}）"
    s, e, kind = sess
    if kind == "日盘":
        win = f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}"
    else:
        win = f"{s.strftime('%H:%M')}-次日{e.strftime('%H:%M')}"
    elapsed_min = (now - s).total_seconds() / 60.0
    if elapsed_min < config.SESSION_EARLY_MINUTES:
        return (f"{win} {kind}·开盘前{config.SESSION_EARLY_MINUTES}分钟每5分钟轮动"
                f"（本轮{cur}，下一轮约{nxt}）")
    return f"{win} {kind}·每20分钟轮动（本轮{cur}，下一轮约{nxt}）"


def sanitize(text):
    """去掉emoji等无法显示/编码的字符，避免打印时报错"""
    if not text:
        return ""
    out = []
    for ch in text:
        o = ord(ch)
        if o >= 0x10000:          # emoji等增补平面字符
            continue
        if unicodedata.category(ch) == "Cc" and ch != " ":
            continue              # 控制字符
        out.append(ch)
    return "".join(out)


def disp_width(s):
    """按东亚全角字符计算显示宽度"""
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def pad(s, width):
    """按显示宽度左对齐补空格"""
    s = sanitize(str(s))
    gap = width - disp_width(s)
    return s + " " * max(gap, 1)


def clip(x, lo, hi):
    return max(lo, min(hi, x))


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def fmt_pct(x):
    return f"{x * 100:+.2f}%"


def fmt_px(x):
    if x is None or x <= 0:
        return "-"
    if x >= 10000:
        return f"{x:,.0f}"
    if x >= 100:
        return f"{x:.1f}"
    return f"{x:.3f}"
