# -*- coding: utf-8 -*-
"""【需求④⑧⑨⑩ + P0-1】报告生成与落盘（按时段分流，**新轮次的块始终写在文件最前面**）：
  日盘 09:00-11:30/13:30-15:00；夜盘 21:00 起按品种分档收市（23:00 / 次日01:00 / 次日02:30），
  全局只要还有品种在交易即按"交易时段"分流：
    - latest_report.txt : 滚动窗口，只保留最近 KEEP_ROUNDS(5) 轮交易时段报告（最新在最前）
    - signals.csv       : 滚动窗口，最近5轮交易时段信号流水（最新轮在最前）
    - history_report.txt: 交易时段当日归档，新块插在最前；新交易日启动时清掉上一交易日块
  全部品种收市后（非交易时段）：
    - offhours_report.txt : 滚动保留最近5轮非交易时段报告（最新在最前）
    - offhours_history.txt: 非交易时段当日归档（夜盘跨零点块同属一个交易日，不被误清）
  每交易日：
    - daily_review.txt  : 复盘报告（全部夜盘结束即次日02:30后生成；无夜盘日15:00后），
                          新交易日在最前，**永不删除**
    - 实时报告.html      : 多页签实时看板，探测 report_status.js，有新报告（含紧急轮动）才自动刷新
  新交易日首次运行时，按"交易日归属"（凌晨夜盘归属前一交易日）清除更早的轮动块。
  写入鲁棒性：文件被 Excel/编辑器占用时自动短暂重试，且每个文件独立写入、互不影响。
"""
import csv
import io
import json
import os
import re
import time
from collections import deque
from datetime import datetime, timedelta

import config
import cross_section
import data_health
import charts
from utils import (LOG, is_trading_time, now_str, pad, sanitize, trade_owner_date)

DISCLAIMER = (
    "免责声明: 本报告由公开数据(新浪财经/金十数据)与规则引擎自动生成，仅供学习研究参考，"
    "不构成任何投资建议。期货及期权杠杆交易风险极高，据此操作风险自负。"
)

CSV_HEADER = ["时间", "轮次", "类型", "品种", "价格", "涨跌%", "综合分", "信号", "建议"]

_HIST_CACHE = {}      # 归档文件内容缓存（新块在最前），避免每轮重读大文件
_rollover_date = None
_seen_news = set()

_BLOCK_HDR_RE = re.compile(r"^(.*第\d+轮 \| (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}).*)$", re.M)


def _owner_of_ts(ts):
    """时间戳（'YYYY-MM-DD HH:MM:SS' 或前19字符）归属的交易日（date）：
    凌晨 0 点至 9 点前属于前一交易日（夜盘延续），其余属于自然日当天。"""
    try:
        dt = datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None
    return (dt - timedelta(days=1)).date() if dt.hour < 9 else dt.date()


def _block_owner(match):
    """块头正则匹配 -> 该块归属交易日（date）"""
    return _owner_of_ts(f"{match.group(2)} {match.group(3)}")


def _read_file(path, encoding="utf-8-sig"):
    """读取文件；utf-8-sig 可透明剥离 BOM（报告txt统一带BOM写入，保证浏览器/记事本不乱码）"""
    try:
        with open(path, encoding=encoding) as fp:
            return fp.read()
    except FileNotFoundError:
        return ""
    except Exception as e:
        LOG.warning("读取 %s 失败: %s", path, e)
        return ""


def _safe_write(path, content, encoding="utf-8-sig", newline=None, retries=3,
                retry_wait=0.8, update_cache=True):
    """安全写入：文件被 Excel/编辑器占用(PermissionError)时短暂重试；
    最终仍失败只告警、不抛异常（返回 False），保证单个文件被占用不影响其他报告。
    仅在写入成功后更新 _HIST_CACHE，避免缓存与磁盘不一致。"""
    for attempt in range(retries):
        try:
            with open(path, "w", encoding=encoding, newline=newline) as fp:
                fp.write(content)
            if update_cache:
                _HIST_CACHE[path] = content
            return True
        except PermissionError:
            if attempt < retries - 1:
                time.sleep(retry_wait)
                continue
            LOG.warning("文件被占用（可能正在 Excel/编辑器中打开），本轮跳过写入，下轮自动重试: %s",
                        os.path.basename(path))
            return False
        except Exception as e:
            LOG.warning("写入 %s 失败: %s", path, e)
            return False
    return False


def _write_file(path, content):
    return _safe_write(path, content)


def prepend_archive(path, block):
    """把新块插到归档文件最前面（整体重写，保证新报告始终在最前）；
    写入失败（文件被占用）时保留原缓存，下一轮不受影响。"""
    old = _HIST_CACHE.get(path)
    if old is None:
        old = _read_file(path)
    return _safe_write(path, block + old)


def daily_rollover():
    """新交易日首次运行：清除轮动文件中"归属交易日早于当前归属日"的块/行，只保留本交易日。
    夜盘跨零点（21:00~次日02:30 同属一个交易日），凌晨的块归属前一交易日，不能被清掉。"""
    global _rollover_date
    owner = trade_owner_date()
    owner_s = owner.strftime("%Y-%m-%d")
    if _rollover_date == owner_s:
        return []
    cleaned = []
    # signals.csv：保留表头 + 归属交易日 >= 当前归属日 的行（utf-8-sig，Excel直接打开不乱码）
    content = _read_file(config.SIGNALS_CSV, encoding="utf-8-sig")
    if content:
        lines = content.splitlines()
        kept = [lines[0]] if lines else []
        for l in lines[1:]:
            bo = _owner_of_ts(l[:19])
            if bo is None or bo >= owner:
                kept.append(l)
        new = "\n".join(kept) + ("\n" if len(kept) > 1 else "")
        if new != content:
            if _safe_write(config.SIGNALS_CSV, new, encoding="utf-8-sig"):
                cleaned.append("signals.csv")
        else:
            _HIST_CACHE[config.SIGNALS_CSV] = new
    # 四个分块文件（latest_report / history / offhours 两件套）
    for path in (config.REPORT_FILE, config.HISTORY_FILE,
                 config.OFFHOURS_REPORT_FILE, config.OFFHOURS_HISTORY_FILE):
        content = _read_file(path)
        if not content:
            _HIST_CACHE[path] = ""
            continue
        ms = list(_BLOCK_HDR_RE.finditer(content))
        kept = []
        for idx, m in enumerate(ms):
            end = ms[idx + 1].start() if idx + 1 < len(ms) else len(content)
            bo = _block_owner(m)
            if bo is None or bo >= owner:
                kept.append(content[m.start():end])
        new = "".join(kept)
        if new != content:
            if _write_file(path, new):
                cleaned.append(os.path.basename(path))
        else:
            _HIST_CACHE[path] = new
    _rollover_date = owner_s
    if cleaned:
        LOG.info("新交易日(%s)：已清除以下文件中上一交易日的轮动报告: %s",
                 owner_s, ", ".join(cleaned))
    return cleaned


def append_daily_news(news):
    """当日新闻缓存到 cache/news_YYYYMMDD.jsonl（供每日复盘报告使用）"""
    try:
        path = os.path.join(config.NEWS_CACHE_DIR,
                            f"news_{datetime.now().strftime('%Y%m%d')}.jsonl")
        lines = []
        for n in news:
            key = (n.get("content") or "")[:50]
            if key in _seen_news:
                continue
            _seen_news.add(key)
            t = n.get("time")
            lines.append(json.dumps({
                "time": t.strftime("%Y-%m-%d %H:%M:%S") if t else "",
                "source": n.get("source"), "content": n.get("content")},
                ensure_ascii=False))
        if lines:
            with open(path, "a", encoding="utf-8") as fp:
                fp.write("\n".join(lines) + "\n")
    except Exception as e:
        LOG.debug("当日新闻缓存写入失败: %s", e)


_DASHBOARD_TABS = [
    ("latest_report.txt", "交易时段·最近5轮"),
    ("__charts__", "图表看板"),  # 第23轮：图表页同页内嵌渲染（片段来自 charts.dashboard_embed_parts），不再 iframe 套独立页
    ("signals.csv", "信号流水CSV"),
    ("signal_tracking.txt", "信号胜率追踪"),
    ("backtest_report.txt", "最小日线回测"),
    ("backtest_trades.csv", "回测交易CSV"),
    ("intraday_backtest_report.txt", "日内/平今回测"),
    ("intraday_backtest_trades.csv", "日内回测交易CSV"),
    ("portfolio_report.txt", "组合账户回测"),
    ("portfolio_trades.csv", "组合交易CSV"),
    ("history_report.txt", "交易时段·当日归档"),
    ("offhours_report.txt", "非交易时段·最近5轮"),
    ("offhours_history.txt", "非交易时段·当日归档"),
    ("daily_review.txt", "每日复盘(永久)"),
]
# 报告写出比轮动刻度晚的缓冲秒数（分析耗时），看板在"刻度+缓冲"后刷新
_DASHBOARD_WRITE_DELAY_SEC = 20


def _dashboard_html():
    """多页签实时看板：外层页面不刷新，每10秒轻量探测 report_status.js，
    仅当程序真写出新一轮报告（定时轮动或原油急动紧急轮动）时才重载当前页签内容；
    页头展示最新报告时间/轮次与计划下一轮倒计时。时段参数由 config 注入。"""
    tabs = []
    for i, (fname, label) in enumerate(_DASHBOARD_TABS):
        active = " active" if i == 0 else ""
        tabs.append(f'<button class="tab{active}" data-src="{fname}">{label}</button>')
    first = _DASHBOARD_TABS[0][0]
    sessions_js = "[" + ",".join(f"[{s},{e}]" for s, e in config.SESSIONS) + "]"
    early_len = config.SESSION_EARLY_MINUTES
    early_step = config.SESSION_EARLY_INTERVAL // 60
    normal_step = config.SESSION_INTERVAL // 60
    off_step = max(1, config.REPORT_INTERVAL // 60)
    _dashboard_tmpl = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>期货监控实时看板（跟随轮动/原油急动自动刷新，无需关闭重开）</title>
<style>
  * { box-sizing: border-box; }
  html,body { height: 100%%; margin: 0; }
  body { background: #141414; color: #ddd; font-family: "Microsoft YaHei",Consolas,sans-serif; }
  #bar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 6px 10px;
         background: #1f1f1f; border-bottom: 1px solid #333; position: sticky; top: 0; z-index: 2; }
  #bar b { color: #7ecbff; margin-right: 8px; font-size: 14px; }
  .tab { background: #2b2b2b; color: #cfcfcf; border: 1px solid #3a3a3a; border-radius: 4px;
         padding: 5px 10px; cursor: pointer; font-size: 13px; }
  .tab:hover { background: #383838; }
  .tab.active { background: #0e639c; border-color: #0e639c; color: #fff; }
  #meta { margin-left: auto; font-size: 12px; color: #9a9a9a; white-space: nowrap; }
  #view { width: 100%%; height: calc(100vh - 45px); border: 0; background: #fff; }
  #charts-panel { display: none; width: 100%%; height: calc(100vh - 45px); overflow-y: auto; }
  /*__CP_STYLE__*/
</style>
<script src="assets/echarts.min.js"></script>
</head>
<body>
<div id="bar">
  <b>期货监控实时看板</b>
  %s
  <span id="meta"></span>
</div>
<iframe id="view" src="%s"></iframe>
<div id="charts-panel">/*__CP_DOM__*/</div>
<script>
  var cur = "%s";
  var CHARTS_VIEW = "__charts__";   // 图表页签为同页内嵌面板，不走 iframe
  // 轮动参数（由 config.py 注入）：时段[起,止](分钟)、开盘快速轮动长度/步长、常规步长、非交易时段步长（分钟）
  var SESSIONS = %s, EARLY_LEN = %d, EARLY_STEP = %d, NORMAL_STEP = %d, OFF_STEP = %d;
  var WRITE_DELAY = %d;   // 轮动刻度后再等这么多秒，等报告写完
  function show(src) {
    cur = src;
    var isCp = src === CHARTS_VIEW;
    var view = document.getElementById('view');
    var panel = document.getElementById('charts-panel');
    view.style.display = isCp ? 'none' : 'block';
    panel.style.display = isCp ? 'block' : 'none';
    if (isCp) { if (window.ChartPanel) window.ChartPanel.activate(); }
    else { view.src = src + '?t=' + Date.now(); }
    var btns = document.querySelectorAll('.tab');
    for (var i = 0; i < btns.length; i++)
      btns[i].classList.toggle('active', btns[i].getAttribute('data-src') === src);
  }
  document.querySelectorAll('.tab').forEach(function (b) {
    b.onclick = function () { show(b.getAttribute('data-src')); };
  });
  function pad2(x) { return (x < 10 ? '0' : '') + x; }
  // 分钟轴：9点前(凌晨)加1440，与 Python utils 同一套跨日表示（夜盘收于次日02:30=轴1590）
  function axisOf(now) {
    var m = now.getHours()*60+now.getMinutes()+now.getSeconds()/60+now.getMilliseconds()/60000;
    return now.getHours() < 9 ? m + 1440 : m;
  }
  // 开市时间轴：周日全天休；周六仅凌晨夜盘延续(02:30前)；周一凌晨无夜盘（节假日由程序端判断）
  function marketActive(now) {
    var d = now.getDay(), h = now.getHours();
    if (d === 0) return false;
    if (d === 6) return h < 3;
    if (d === 1 && h < 9) return false;
    return true;
  }
  // 与 utils.next_cycle_time 同一套刻度：返回下一轮轮动刻度的"分钟轴"值
  function nextMark(now) {
    var m = axisOf(now), target, inSession = false;
    if (marketActive(now)) {
      for (var i = 0; i < SESSIONS.length; i++) {
        var s = SESSIONS[i][0], e = SESSIONS[i][1], ee = s + EARLY_LEN;
        if (s <= m && m < e) {
          inSession = true;
          if (m < ee) {
            target = (Math.floor(m / EARLY_STEP) + 1) * EARLY_STEP;
            if (target > ee) target = ee;
          } else {
            target = ee + (Math.floor((m - ee) / NORMAL_STEP) + 1) * NORMAL_STEP;
            if (target >= e) target = e + OFF_STEP;      // 收盘后转入非交易1分钟节奏
          }
          break;
        }
      }
    }
    if (!inSession) {
      target = Math.floor(m) + OFF_STEP;                // 非交易时段：下一整分钟
      if (marketActive(now)) {
        var opens = [540, 810, 1260, 1980, 2700];       // 09:00/13:30/21:00/次日09:00/次日21:00(轴)
        for (var j = 0; j < opens.length; j++) {
          if (m < opens[j] && opens[j] <= target) { target = opens[j]; break; }
        }
      }
    }
    return target;
  }
  // ---- 新报告探测：每10秒轻量探测 report_status.js，仅当程序真写出新一轮（定时轮动或原油急动紧急轮动）时才重载当前报告，平时不刷新内容 ----
  var POLL_MS = 10000, lastStatusTs = null;
  function reloadView() {
    if (cur === CHARTS_VIEW) { if (window.ChartPanel) window.ChartPanel.reload(); return; }
    document.getElementById('view').src = cur + '?t=' + Date.now();
  }
  function pollStatus() {
    var sc = document.createElement('script');
    sc.src = 'report_status.js?t=' + Date.now();
    sc.onload = function () {
      var st = window.REPORT_STATUS;
      if (st && st.ts) {
        if (lastStatusTs !== null && st.ts !== lastStatusTs) reloadView();   // 有新报告才刷新
        lastStatusTs = st.ts;
      }
      sc.remove();
    };
    sc.onerror = function () { sc.remove(); };
    document.body.appendChild(sc);
  }
  function tick() {
    var now = new Date();
    var waitSec = nextMark(now) * 60 + WRITE_DELAY - axisOf(now) * 60;
    if (waitSec < 5) waitSec = 5;
    var nextAt = new Date(now.getTime() + waitSec * 1000);
    var mm = Math.floor(waitSec / 60), ss = Math.floor(waitSec %% 60);
    var st = window.REPORT_STATUS;
    var line = st
      ? '最新报告 ' + st.ts + '（第' + st.cycle + '轮·' + st.kind +
        (st.emergency ? '·' + (st.emergency_tag || '紧急轮动') : '') + '）'
      : '等待程序写出第一轮报告';
    line += ' ｜ 计划下一轮 ' + pad2(nextAt.getHours()) + ':' + pad2(nextAt.getMinutes()) +
            ':' + pad2(nextAt.getSeconds()) + '（倒计时 ' + pad2(mm) + ':' + pad2(ss) + '）';
    document.getElementById('meta').textContent = line;
  }
  pollStatus();
  setInterval(pollStatus, POLL_MS);
  tick();
  setInterval(tick, 1000);
</script>
<script>
/*__CP_JS__*/
</script>
</body>
</html>"""
    html = _dashboard_tmpl % ("\n  ".join(tabs), first, first, sessions_js, early_len,
                              early_step, normal_step, off_step, _DASHBOARD_WRITE_DELAY_SEC)
    _cp_style, _cp_dom, _cp_js = charts.dashboard_embed_parts()
    return (html.replace("/*__CP_STYLE__*/", _cp_style)
                .replace("/*__CP_DOM__*/", _cp_dom)
                .replace("/*__CP_JS__*/", _cp_js))


def write_dashboard():
    """生成/刷新实时看板（静态外壳，内容由浏览器按页签自动从同目录文件读取）；
    同时幂等写出 P1-3 图表看板静态页并同步本地 ECharts 资源（失败只告警不影响主看板）。"""
    ok = _safe_write(config.REALTIME_HTML, _dashboard_html(),
                     encoding="utf-8", update_cache=False)
    try:
        charts.ensure_charts_page()
    except Exception as e:
        LOG.debug("图表看板静态页写出失败: %s", e)
    return ok


def _opt_short_verdict(v):
    return v if len(v) <= 26 else v[:25] + "…"


def csv_rows(cycle, now, fut_rows, opt_rows):
    """把本轮期货/期权结果转成信号流水行"""
    rows = []
    for r in fut_rows:
        rows.append([now, cycle, "期货", r["name"], r["price"],
                     round(r["chg"] * 100, 2), round(r["score"], 1),
                     r["label"], r["advice"]])
    for o in opt_rows:
        rows.append([now, cycle, "期权", o["name"], "", "",
                     round(o["score"], 1), o["direction"], o["verdict"]])
    return rows


def _weighted_avg(rows, key="avg_ret", count_key="evaluated"):
    total_n = sum(int(r.get(count_key) or 0) for r in rows)
    if total_n <= 0:
        return 0.0
    return sum(float(r.get(key) or 0.0) * int(r.get(count_key) or 0) for r in rows) / total_n


def _horizon_label(minutes):
    return {30: "30分钟", 120: "2小时", 1440: "次日(约24小时)"}.get(int(minutes), f"{minutes}分钟")


def signal_tracking_text(state):
    """生成信号胜率追踪文本：统计近7天已到期信号，并列出最近评估结果。"""
    db = getattr(state, "db", None)
    sep = "-" * 96
    L = ["=" * 96,
         f" 信号效果追踪（更新于 {now_str()}；统计最近 {config.SIGNAL_TRACK_STAT_DAYS} 天已到期信号）",
         "=" * 96]
    if db is None:
        L.append(" 数据库尚未初始化。")
        return "\n".join(L)
    try:
        stats = db.outcome_stats(config.SIGNAL_TRACK_STAT_DAYS)
        pending_n = db.pending_count()
    except Exception as e:
        LOG.debug("读取信号追踪统计失败: %s", e)
        L.append(" 数据库暂时不可用，本轮跳过胜率统计。")
        L.append(sep)
        return "\n".join(L)
    if not stats:
        L.append(" 暂无可评估样本：信号会在发出后 30分钟/2小时/次日 自动用后续行情回填结果。")
    else:
        groups = {}
        for row in stats:
            groups.setdefault(row["horizon_min"], []).append(row)
        L.append(" 一、分周期胜率（方向收益=信号方向×后续涨跌幅；打平不计胜率分子但计入样本）")
        for horizon in sorted(groups):
            rows = groups[horizon]
            n = sum(int(r["n"]) for r in rows)
            eval_n = sum(int(r.get("evaluated") or 0) for r in rows)
            expired_n = sum(int(r.get("expired") or 0) for r in rows)
            wins = sum(int(r["wins"] or 0) for r in rows)
            avg_ret = _weighted_avg(rows)
            longs = [r for r in rows if r["direction"] == "做多"]
            shorts = [r for r in rows if r["direction"] == "做空"]
            ln = sum(int(r.get("evaluated") or 0) for r in longs)
            lw = sum(int(r["wins"] or 0) for r in longs)
            sn = sum(int(r.get("evaluated") or 0) for r in shorts)
            sw = sum(int(r["wins"] or 0) for r in shorts)
            sample_txt = f"样本{n}" + (f"(过期{expired_n})" if expired_n else "")
            L.append(" " + pad(_horizon_label(horizon), 14) +
                     pad(sample_txt, 14) + pad(f"胜率{wins/eval_n*100:.1f}%" if eval_n else "胜率-", 12) +
                     pad(f"平均方向收益{avg_ret*100:+.2f}%", 18) +
                     (f"多头{lw}/{ln}" if ln else "多头0/0") + "   " +
                     (f"空头{sw}/{sn}" if sn else "空头0/0"))
            for r in sorted(rows, key=lambda x: (x["score_band"], x["direction"])):
                rn = int(r.get("evaluated") or 0)
                wr = (int(r["wins"] or 0) / rn * 100) if rn else 0.0
                L.append("    · " + pad(f"{r['score_band']}/{r['direction']}", 16) +
                         pad(f"样本{rn}", 9) + pad(f"胜率{wr:.1f}%", 11) +
                         f"平均{float(r['avg_ret'] or 0)*100:+.2f}%")
        L.append("")
        L.append(" 二、最近评估的信号")
        L.append(" " + pad("品种", 12) + pad("周期", 12) + pad("方向", 8) +
                 pad("分档", 8) + pad("入场", 10) + pad("评估价", 10) +
                 pad("方向收益", 10) + "结果")
        status_cn = {"hit": "正确", "miss": "错误", "flat": "打平", "expired": "过期"}
        try:
            recent_rows = db.recent_outcomes(15)
        except Exception as e:
            LOG.debug("读取最近信号结果失败: %s", e)
            recent_rows = []
        for r in recent_rows:
            L.append(" " + pad(r["variety"], 12) + pad(_horizon_label(r["horizon_min"]), 12) +
                     pad(r["direction"], 8) + pad(r["score_band"], 8) +
                     pad(f"{float(r['entry_price']):g}", 10) +
                     pad(f"{float(r['exit_price'] or 0):g}", 10) +
                     pad(f"{float(r['ret'] or 0)*100:+.2f}%", 10) +
                     status_cn.get(r["status"], r["status"]))
        # 三、WP-F2 A3 历史同类信号胜率校准（影子模式：只展示，不改变综合分/信号/建议）
        cal = getattr(state, "calibrator", None)
        bt = cal.band_table() if cal is not None else []
        L.append("")
        L.append(" 三、历史同类信号胜率校准（%s周期；贝叶斯平滑；方向×分档；n<%d样本积累中不给乘子）"
                 % (_horizon_label(config.CALIBRATOR_HORIZON), config.CALIBRATOR_MIN_N))
        if not bt:
            L.append(" 校准器未启用或暂无历史样本（信号样本会随运行持续积累）。")
        else:
            L.append(" " + pad("方向", 8) + pad("分档", 8) + pad("样本", 8) +
                     pad("平滑胜率", 10) + pad("平均方向收益", 14) + "sizing乘子（portfolio --calibrate 才生效）")
            for c in bt:
                mult_txt = ("%.2f" % c["mult"]) if c["enough"] else "积累中"
                L.append(" " + pad(c["dir_text"], 8) + pad(c["band"], 8) +
                         pad(str(c["n"]), 8) + pad(f"{c['winrate']*100:.1f}%", 10) +
                         pad(f"{c['avg_ret']*100:+.2f}%", 14) + mult_txt)
            L.append(" 更细的「方向×分档×主导因子」校准见各品种明细卡「校准」行；实时侧仅展示，不改变当前建议。")
    L.extend([sep, f" 当前待评估信号 {pending_n} 条；结构化数据库：{config.MONITOR_DB}",
              " 说明：该统计用于检验规则有效性，不代表未来收益，不构成投资建议。"])
    return "\n".join(L)


def write_signal_tracking(state):
    """每轮写入信号胜率追踪文本，供实时看板页签查看。"""
    try:
        _safe_write(config.SIGNAL_TRACKING_FILE, signal_tracking_text(state),
                    encoding="utf-8-sig", update_cache=False)
    except Exception as e:
        LOG.debug("信号胜率追踪报告写入失败: %s", e)


class ReportStore:
    """运行期滚动缓存：报告与信号流水各保留最近 KEEP_ROUNDS 轮；
    非交易时段(9:00-11:30/13:30-15:00/21:00-23:00之外)的轮次单独滚动保留5轮"""

    def __init__(self):
        self.reports = deque(maxlen=config.KEEP_ROUNDS)      # (轮次, 时间, 报告全文)
        self.signal_rows = deque(maxlen=config.KEEP_ROUNDS)  # (轮次, 时间, 本轮行)
        self.off_reports = deque(maxlen=config.KEEP_ROUNDS)  # 非交易时段轮 (轮次, 时间, 报告, 行)

    def add(self, cycle, now, text, rows):
        self.reports.append((cycle, now, text))
        self.signal_rows.append((cycle, now, rows))

    def add_offhours(self, cycle, now, text, rows):
        self.off_reports.append((cycle, now, text, rows))


def render(state, fut_rows, opt_rows, strat_rows, news_top):
    """生成整份文字报告"""
    sep = "=" * 108
    thin = "-" * 108
    L = []
    L.append(sep)
    L.append(f" 期货全品种监控分析报告   {now_str()}   第{state.cycle}轮   "
             f"数据源: 新浪财经7x24/金十数据   范围: {state.wl_source}")
    L.append(sep)
    L.append(sanitize(state.oil.snapshot_line(verbose=True)))
    L.append(f" 分析范围: {getattr(state, 'universe_note', '')}"
             f"（购买建议中的合约月份由成交量+持仓量自动探测）")
    trading, sess_desc = is_trading_time()
    L.append(f" 交易时段: {sess_desc}"
             + ("" if trading else " —— 非交易时段，重点品种明细已附加【预测走向】(规则预测仅供参考)"))
    L.append(f" 轮动节奏: {getattr(state, 'rotation_desc', '—')}"
             f"（本轮轮动写入时间: {now_str()}）")
    emerg = getattr(state, "emergency_note", "")
    if emerg:
        L.append(f" ★紧急触发: {emerg}")
    L.append("")

    # ---------- 期货分析总表 ----------
    L.append("【期货分析】(综合分范围-10~+10; |分|<2观望, 2~4轻仓, 4~6.5分批建仓, ≥6.5强信号)")
    L.append(" " + pad("品种", 12) + pad("主力合约", 9) + pad("板块", 10) + pad("最新价", 10)
             + pad("较昨结", 9) + pad("综合分", 8) + pad("信号", 8) + "操作建议")
    def _gate_mark(r):
        _lv = (r.get("risk") or {}).get("level")
        return "⛔" if _lv == "veto" else ("⚠" if _lv == "warn" else "")

    for r in fut_rows:
        L.append(" " + pad(r["name"], 12) + pad(r.get("contract_code") or "探测中", 9)
                 + pad(r["cat"], 10)
                 + pad("%.1f" % r["price"] if r["price"] else "-", 10)
                 + pad("%.2f%%" % (r["chg"] * 100), 9)
                 + pad("%+.1f" % r["score"], 8) + pad(r["label"], 8) + _gate_mark(r) + r["advice"])
    L.append("")

    # ---------- 基本面速览（第13轮 WP-C：库存/仓单+龙虎榜+期限carry+基差） ----------
    fund_rows = [r for r in fut_rows if r.get("fundamental")]
    if fund_rows:
        ranked = sorted(fund_rows, key=lambda x: -x["fundamental"]["score"])
        bull = [r for r in ranked if r["fundamental"]["score"] > 0.15][:5]
        bear = [r for r in sorted(fund_rows, key=lambda x: x["fundamental"]["score"])
                if r["fundamental"]["score"] < -0.15][:5]

        def _fbrief(r):
            fp = r["fundamental"]
            tags = []
            sub = fp.get("sub") or {}
            if sub.get("库存仓单"):
                tags.append("库%d%%分位" % (sub["库存仓单"]["pct"] * 100))
            if sub.get("龙虎榜"):
                tags.append("净多%+.1f%%" % (sub["龙虎榜"]["net"] * 100))
            if sub.get("期限carry"):
                tags.append("carry%+.0f%%" % (sub["期限carry"]["annual_carry"] * 100))
            return "%s(%+.2f %s)" % (r["name"], fp["score"], "/".join(tags) or "—")

        L.append("【基本面速览】(库存仓单分位+周环比·龙虎榜前20席净多·期限carry·基差; "
                 "满分±%.1f, 缺项按可得权重自动归一)" % config.FUND_MAX_SCORE)
        L.append(" 偏多: " + ("、".join(_fbrief(r) for r in bull) or "无显著偏多品种"))
        L.append(" 偏空: " + ("、".join(_fbrief(r) for r in bear) or "无显著偏空品种"))
        L.append(" 口径: 库存=东财注册仓单近约3个月滚动分位(样本≥%d); 龙虎榜=前20席会员合计; "
                 "carry=近远月年化; 基差源(生意社)遇反爬自动缺失不编造" % config.FUND_INV_MIN_SAMPLES)
        L.append("")

    # ---------- G6 数据源健康（缺数/陈旧/跳变/熔断，只监控不改分） ----------
    _dh = getattr(state, "last_health", None)
    _dh_block = data_health.format_health_block(_dh)
    if _dh_block:
        L.append(_dh_block)
        L.append("")

    # ---------- 横截面强弱（WP-F1 B1：稳健z/MAD板块榜+多空Top，只展示不改分） ----------
    _cs = getattr(state, "last_cross_section", None)
    if _cs:
        L.extend(cross_section.format_block(_cs))

    # ---------- 重点品种明细 ----------
    focus = [r for r in fut_rows if abs(r["score"]) >= config.SCORE_NEUTRAL]
    L.append("【重点品种操作明细】" + ("(共%d个非中性信号)" % len(focus) if focus else "(当前全部观望)"))
    for r in sorted(focus, key=lambda x: -abs(x["score"])):
        L.extend(_render_detail(r))
    L.append("")

    # ---------- 期权严格分析 ----------
    L.append("【期权严格分析】(仅列出有场内期权的品种; IV优先OpenVlab真实平值，缺失时用HV估计，实盘以盘面为准)")
    if opt_rows:
        L.append(" " + pad("品种", 12) + pad("标的分", 8) + pad("IV", 8)
                 + pad("IV分位", 9) + pad("建议合约", 26) + pad("权利金(估)", 11)
                 + pad("Delta", 8) + pad("Theta/日", 10) + "结论")
        for o in opt_rows:
            if o.get("yy"):
                contract = f"{o['month_label']}月{o['kname']}{o['direction']}K≈{o['K']:g}"
            else:
                contract = f"{o['kname']}{o['direction']}K≈{o['K']:g}"
            iv_pct = o.get("iv_pct")
            iv_pct_txt = "--" if iv_pct is None else f"{iv_pct*100:.0f}%"
            L.append(" " + pad(o["name"], 12) + pad("%+.1f" % o["score"], 8)
                     + pad("%.0f%%" % (o["iv"] * 100), 8)
                     + pad(iv_pct_txt, 9) + pad(contract, 26)
                     + pad("%.1f" % o["prem"], 11) + pad("%.2f" % o["delta"], 8)
                     + pad("%.2f" % o["theta_day"], 10)
                     + _opt_short_verdict(o["verdict"]))
        for o in opt_rows:
            L.append(f"  ● {o['name']} 期权检查({len([c for c in o['checks'] if c[1]])}/{len(o['checks'])}项通过):")
            for item, ok, note in o["checks"]:
                mark = "√" if ok else "×"
                L.append(f"      [{mark}] {item}: {note}")
            if o.get("month_note"):
                L.append(f"      月份说明: {o['month_note']}")
            if o.get("chain_note"):
                L.append(f"      期权链: {o['chain_note']}")
            if o.get("surface_note"):
                L.append(f"      {o['surface_note']}")
                if o.get("surface_matrix"):
                    L.append(f"      {o['surface_matrix']}")
            if o.get("opt_code"):
                L.append(f"      参考代码: {o['opt_code']}（示意，执行价以交易所实际挂牌为准）")
            if o["pos_note"]:
                L.append(f"      执行: {o['pos_note']}")
    else:
        L.append(" (自选中没有带场内期权的品种)")
    L.append("")

    # ---------- 期权策略推荐 ----------
    L.append("【期权策略推荐】(价差/蝶式/比率/备兑/保护性认沽; 严格检查全过才建议执行; 权利金为Black-76估计值)")
    if strat_rows:
        L.append(" " + pad("品种", 12) + pad("策略", 18) + pad("月份", 10)
                 + pad("净支/收", 9) + pad("最大盈", 10) + pad("最大亏", 10) + "结论")
        for s in strat_rows:
            net = s.get("net", 0)
            mp = s.get("max_profit")
            ml = s.get("max_loss")
            mp_txt = "无上限" if mp is None else (f"{mp:.0f}点" if isinstance(mp, (int, float)) else "-")
            ml_txt = "无上限" if ml is None else (f"{ml:.0f}点" if isinstance(ml, (int, float)) else "-")
            L.append(" " + pad(s.get("variety", ""), 12) + pad(s["name"], 18)
                     + pad(s.get("month_label", ""), 10)
                     + pad(f"{net:+.0f}点", 9) + pad(mp_txt, 10) + pad(ml_txt, 10)
                     + _opt_short_verdict(s["verdict"]))
        for s in strat_rows:
            mark = "√" if s["all_pass"] else "×"
            _ml = s.get('month_label', '')
            _mpar = _ml if "/" in _ml else f"{_ml}月份"
            L.append(f"  ● [{mark}] {s.get('variety','')} {s['name']}（{_mpar}）")
            if s.get("legs_text"):
                L.append(f"      腿: {s['legs_text']}")
            L.append(f"      组合Greeks: Δ{s.get('delta',0):+.2f} / Γ{s.get('gamma',0):+.4f} / Vega{s.get('vega',0):+.1f} / Θ{s.get('theta_day',0):+.1f}点每日")
            if s.get("margin_points", 0) > 0:
                L.append(f"      保证金估算: 约{s.get('margin_points',0):.1f}点（点值口径，未乘合约乘数；实盘以交易所/期货公司为准）")
            for item, ok, note in s["checks"]:
                m = "√" if ok else "×"
                L.append(f"      [{m}] {item}: {note}")
            if s.get("be") is not None:
                be = s["be"]
                if isinstance(be, tuple):
                    L.append(f"      盈亏平衡: {be[0]:.0f} / {be[1]:.0f}")
                else:
                    L.append(f"      盈亏平衡: {be:.0f}")
            if s.get("pos_note"):
                L.append(f"      执行: {s['pos_note']}")
    else:
        L.append(" (自选中没有带场内期权的品种)")
    L.append("")

    # ---------- 新闻 ----------
    L.append("【近期有影响力的消息Top】(按|时间衰减后得分|排序)")
    if news_top:
        for s, n in news_top:
            t = n.get("time").strftime("%m-%d %H:%M")
            flag = "存疑·" if n.get("doubtful") else ""
            L.append(f"  {s:+.1f} [{flag}{n.get('source')} {t}] {n.get('content','')[:88]}")
    else:
        L.append("  (暂未捕捉到匹配关键词的消息)")
    # 第14轮 WP-D0：分钟K自采库覆盖（让用户看到自有分钟库积累进度；库为空时不显示）
    try:
        mb_cov = state.db.minute_bars_coverage()
    except Exception:
        mb_cov = {}
    if mb_cov:
        mb_txt = "；".join(f"{p}分钟 {v['bars']}根/{v['contracts']}合约"
                           f"({(v['first'] or '')[5:]}~{(v['last'] or '')[5:]})"
                           for p, v in sorted(mb_cov.items()))
        L.append(f"【分钟K自采库】{mb_txt}；新浪主连全周期(含1m)为主+通达信/东财具体合约兜底，常驻自采，供日内/平今回测长期积累")
        L.append("")
    L.append(thin)
    L.append(" " + DISCLAIMER)
    L.append(sep)
    return "\n".join(L)


def _render_detail(r):
    from analyzer import detail_lines
    return detail_lines(r)


def save(state, text, fut_rows, opt_rows):
    """落盘（按时段分流，**最新轮永远写在文件最前面**，块头标明轮动时间与节奏）：
    交易时段 -> latest_report.txt(滚动5轮,最新在前) + signals.csv(最新轮在前)
               + history_report.txt(新块置顶,次日启动清昨日块)
    非交易时段 -> offhours_report.txt / offhours_history.txt
    每个文件独立安全写入（被 Excel/编辑器占用只跳过该文件，不影响其他文件）；
    同时刷新 实时报告.html 看板（浏览器跟随轮动节奏自动刷新，无需关闭重开）。"""
    daily_rollover()
    time_str = now_str()
    rows = csv_rows(state.cycle, time_str, fut_rows, opt_rows)
    desc = getattr(state, "rotation_desc", "") or "轮动"
    emark = getattr(state, "emergency_tag", "") or ""
    trading, _ = is_trading_time()
    if trading:
        state.store.add(state.cycle, time_str, text, rows)
        # 1) latest_report.txt：滚动5轮，最新轮在最前
        parts = []
        for c, t, txt in reversed(state.store.reports):
            parts.append(f"{'#' * 24} 交易时段 第{c}轮 | {t} | {desc}{emark} {'#' * 24}\n")
            parts.append(txt)
            parts.append("\n\n")
        _safe_write(config.REPORT_FILE, "".join(parts))
        # 2) signals.csv：表头 + 最新轮的流水在最前（utf-8-sig，Excel直接打开不乱码）
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(CSV_HEADER)
        for c, t, rs in reversed(state.store.signal_rows):
            w.writerows(rs)
        _safe_write(config.SIGNALS_CSV, buf.getvalue(), encoding="utf-8-sig", newline="")
        # 3) history_report.txt：新块置顶归档（次日启动时清除昨日块）
        block = (f"\n{'=' * 24} 交易时段 第{state.cycle}轮 | {time_str} | {desc}{emark} "
                 f"{'=' * 24}\n{text}\n---- 本轮信号流水 ----\n")
        for r in rows:
            block += ",".join(str(x) for x in r) + "\n"
        prepend_archive(config.HISTORY_FILE, block)
    else:
        # 非交易时段：专用滚动5轮 + 置顶归档
        state.store.add_offhours(state.cycle, time_str, text, rows)
        parts = []
        for c, t, txt, _rs in reversed(state.store.off_reports):
            parts.append(f"{'#' * 14} 非交易时段 第{c}轮 | {t} | {desc}{emark} {'#' * 14}\n")
            parts.append(txt)
            parts.append("\n\n")
        _safe_write(config.OFFHOURS_REPORT_FILE, "".join(parts))
        block = (f"\n{'=' * 20} 非交易时段 第{state.cycle}轮 | {time_str} | {desc}{emark} "
                 f"{'=' * 20}\n{text}\n---- 本轮信号流水 ----\n")
        for r in rows:
            block += ",".join(str(x) for x in r) + "\n"
        prepend_archive(config.OFFHOURS_HISTORY_FILE, block)
    # 实时看板外壳 + 新报告状态（浏览器探测到新状态才重载报告内容，紧急轮动也能立刻显示）
    write_dashboard()
    write_status(state, trading)
    # P1-3 图表看板数据：每轮把组合曲线/横截面/校准/因子IC汇总成 chart_data.js
    try:
        charts.write_chart_data(state)
    except Exception as e:
        LOG.debug("图表数据写入失败: %s", e)


def write_status(state, trading):
    """写极小状态文件 report_status.js：看板每10秒探测一次，仅当 ts 变化（=有新报告写出，
    含原油急动紧急轮动）时才重载当前报告，平时不刷新报告内容。"""
    try:
        status = {
            "ts": now_str(),
            "cycle": int(getattr(state, "cycle", 0) or 0),
            "kind": "交易时段" if trading else "非交易时段",
            "emergency": getattr(state, "emergency_note", "") or "",
            "emergency_tag": (getattr(state, "emergency_tag", "") or "").strip("[]"),
            "rotation": getattr(state, "rotation_desc", "") or "",
        }
        js = "window.REPORT_STATUS = " + json.dumps(status, ensure_ascii=False) + ";\n"
        _safe_write(config.STATUS_JS, js, encoding="utf-8", update_cache=False)
    except Exception as e:
        LOG.debug("状态文件写入失败: %s", e)


def build_daily_review(state, owner=None):
    """归属交易日 owner 的全部交易结束后调用：汇总该交易日两个归档中的轮动块
    （夜盘跨自然日零点，凌晨块归属前一交易日）+ 当日新闻 → 复盘报告文本"""
    import factors
    if owner is None:
        owner = trade_owner_date()
    owner_s = owner.strftime("%Y-%m-%d")
    rounds = []
    for path, tag in ((config.HISTORY_FILE, "交易时段"),
                      (config.OFFHOURS_HISTORY_FILE, "非交易时段")):
        content = _read_file(path)
        ms = list(_BLOCK_HDR_RE.finditer(content))
        for idx, m in enumerate(ms):
            if _block_owner(m) != owner:
                continue
            end = ms[idx + 1].start() if idx + 1 < len(ms) else len(content)
            block = content[m.end():end]
            rows = []
            if "---- 本轮信号流水 ----" in block:
                data = block.split("---- 本轮信号流水 ----", 1)[1]
                for line in data.strip().splitlines():
                    line = line.strip()
                    if line.count(",") >= 6:
                        rows.append(line.split(","))
            rounds.append({"tag": tag, "day": m.group(2), "time": m.group(3),
                           "hdr": m.group(1).strip("= #"), "rows": rows,
                           "body": block})
    # 跨零点：先按自然日、再按时间排序，凌晨块排在夜盘之后
    rounds.sort(key=lambda x: (x["day"], x["time"]))

    # 品种当日首次轮动 vs 最后一次轮动
    agg = {}
    for rd in rounds:
        for row in rd["rows"]:
            if len(row) >= 8 and row[2] == "期货":
                try:
                    price = float(row[4])
                    score = float(row[6])
                except ValueError:
                    continue
                a = agg.setdefault(row[3], {"first": None, "last": None})
                rec = (rd["time"], price, score, row[7])
                if a["first"] is None:
                    a["first"] = rec
                a["last"] = rec

    # 当日新闻统计（夜盘跨零点：同时读 owner 与 owner+1 两个自然日的缓存，按归属过滤）
    items = []
    seen_news = set()   # 跨重启去重：同内容新闻只计一次
    for day in (owner, owner + timedelta(days=1)):
        news_path = os.path.join(config.NEWS_CACHE_DIR,
                                 f"news_{day.strftime('%Y%m%d')}.jsonl")
        try:
            fp = open(news_path, encoding="utf-8")
        except FileNotFoundError:
            continue
        with fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    it = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # owner+1 文件只收凌晨（9点前，归属前一交易日）的新闻
                if day != owner and _owner_of_ts(it.get("time", "")) != owner:
                    continue
                key = (it.get("content") or "")[:50]
                if key in seen_news:
                    continue
                seen_news.add(key)
                items.append(it)
    pos = neg = 0
    scored = []
    for it in items:
        w = factors._lex_weight(it.get("content") or "", None)
        if w > 0.05:
            pos += 1
        elif w < -0.05:
            neg += 1
        scored.append((abs(w), w, it))
    scored.sort(key=lambda x: -x[0])

    L = []
    L.append(f"{'#' * 20} 复盘报告 | {owner_s} | 生成于 {now_str()} {'#' * 20}")
    if rounds:
        cov = (f"{rounds[0]['day'][5:]} {rounds[0]['time']} ~ "
               f"{rounds[-1]['day'][5:]} {rounds[-1]['time']}")
    else:
        cov = "无"
    n_tr = sum(1 for r in rounds if r["tag"] == "交易时段")
    L.append(f"一、当日轮动概况：共 {len(rounds)} 份轮动报告（交易时段 {n_tr} 份 / "
             f"非交易时段 {len(rounds) - n_tr} 份），覆盖 {cov}")
    L.append("")
    L.append("二、品种当日轮动表现（当日首次轮动 vs 最后一次轮动）：")
    L.append(" " + pad("品种", 12) + pad("首轮价", 10) + pad("末轮价", 10)
             + pad("日内涨跌", 10) + pad("首轮分", 8) + pad("末轮分", 8) + "末轮信号")
    for name in sorted(agg):
        a = agg[name]
        f0, l0 = a["first"], a["last"]
        chg = (l0[1] / f0[1] - 1) if f0[1] else 0.0
        L.append(" " + pad(name, 12) + pad(f"{f0[1]:g}", 10) + pad(f"{l0[1]:g}", 10)
                 + pad(f"{chg * 100:+.2f}%", 10) + pad(f"{f0[2]:+.1f}", 8)
                 + pad(f"{l0[2]:+.1f}", 8) + l0[3])
    L.append("")
    L.append("三、信号效果追踪（最近7天已到期样本，用来检验规则有效性）：")
    db = getattr(state, "db", None)
    if db is not None:
        try:
            track_stats = db.outcome_stats(config.SIGNAL_TRACK_STAT_DAYS)
            pending_n = db.pending_count()
            if track_stats:
                track_groups = {}
                for tr in track_stats:
                    track_groups.setdefault(tr["horizon_min"], []).append(tr)
                for horizon in sorted(track_groups):
                    gr = track_groups[horizon]
                    tn = sum(int(x.get("evaluated") or 0) for x in gr)
                    total_n = sum(int(x.get("n") or 0) for x in gr)
                    expired_n = sum(int(x.get("expired") or 0) for x in gr)
                    tw = sum(int(x["wins"] or 0) for x in gr)
                    avg = _weighted_avg(gr)
                    expire_txt = f"，过期{expired_n}条" if expired_n else ""
                    wr_txt = f"胜率{tw/tn*100:.1f}%" if tn else "胜率-"
                    L.append(f"  {_horizon_label(horizon)}：有效样本{tn}/总样本{total_n}{expire_txt}，{wr_txt}，"
                             f"平均方向收益{avg*100:+.2f}%")
            else:
                L.append("  样本尚在累积；信号会在30分钟/2小时/次日自动回填结果。")
            L.append(f"  当前仍有待评估信号 {pending_n} 条，详见看板『信号胜率追踪』页签。")
        except Exception as e:
            LOG.debug("复盘读取信号追踪失败: %s", e)
            L.append("  数据库暂时不可用，本轮复盘跳过胜率统计。")
    else:
        L.append("  数据库未初始化，本轮无法统计历史胜率。")
    L.append("")
    L.append(f"四、当日消息面复盘：程序共收集新闻 {len(items)} 条，"
             f"命中利多关键词 {pos} 条 / 利空关键词 {neg} 条；影响力Top：")
    shown = 0
    for aw, w, it in scored:
        if aw < 0.05 or shown >= 8:
            break
        L.append(f"  {w:+.1f} [{it.get('source', '')} {(it.get('time') or '')[:16]}] "
                 f"{sanitize(it.get('content') or '')[:80]}")
        shown += 1
    L.append("")
    L.append("五、末轮期权策略推荐回顾：")
    strat_rows = getattr(state, "last_strat_rows", None)
    if strat_rows:
        for s in strat_rows:
            _ml = s.get('month_label', '')
            _mpar = _ml if "/" in _ml else f"{_ml}月份"
            L.append(f"  ● {s.get('variety', '')} {s['name']}（{_mpar}）{s['verdict']}")
    else:
        # 23点后重启程序时内存中无策略数据：从当日最后一份交易时段归档块中兜底提取
        fallback = []
        last_tr = None
        for rd in reversed(rounds):
            if rd["tag"] == "交易时段":
                last_tr = rd
                break
        if last_tr and "【期权策略推荐】" in last_tr["body"]:
            sec = last_tr["body"].split("【期权策略推荐】", 1)[1]
            sec = re.split(r"\n【", sec, 1)[0]
            for ln in sec.splitlines():
                ln = ln.strip()
                if ln.startswith("●"):
                    fallback.append("  " + ln)
        if fallback:
            L.append(f"  （取自当日末轮交易时段报告 {last_tr['time']}）")
            L.extend(fallback)
        else:
            L.append("  （当日无策略推荐数据）")
    L.append("")
    L.append("六、后续关注（最新一次非交易时段预测走向）：")
    fc = getattr(state, "last_forecasts", None) or {}
    if fc:
        for name in sorted(fc):
            L.append(f"  {name}: {fc[name]}")
    else:
        L.append("  （暂无预测走向数据）")
    L.append("")
    L.append(" 说明：复盘基于当日轮动报告与程序运行期间收集的新闻池(最近12小时)。")
    L.append(" " + DISCLAIMER)
    return "\n".join(L)


def write_daily_review(text, owner=None):
    """复盘报告写入 daily_review.txt：新交易日块在最前；同一交易日重新生成时替换旧块；永不删除"""
    owner_s = (owner or trade_owner_date()).strftime("%Y-%m-%d")
    old = _read_file(config.DAILY_REVIEW_FILE)
    if old:
        ms = list(re.finditer(r"^#{16,} 复盘报告 \| (\d{4}-\d{2}-\d{2}).*$", old, re.M))
        kept = []
        for idx, m in enumerate(ms):
            end = ms[idx + 1].start() if idx + 1 < len(ms) else len(old)
            if m.group(1) != owner_s:
                kept.append(old[m.start():end])
        old = "".join(kept)
    _write_file(config.DAILY_REVIEW_FILE, text + "\n\n" + old)
