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
import html
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
    ("paper_account.txt", "纸面·基准"),  # 第28轮 G1（二）：PaperBroker 影子账户快照；第102轮：基准账户
    ("device_status.txt", "数据采集装置(界面操作)"),  # 第N轮：装置 save_report 实时写入（量化报告集成遗留项）
    ("__device__", "装置健康详情"),  # 协同：读取 report_device.json 结构化展示源健康/软件/告警
    ("__paper_cmp__", "纸面账户对比"),  # 第102轮：15账户对比（读 paper_compare.json）
    ("history_report.txt", "交易时段·当日归档"),
    ("offhours_report.txt", "非交易时段·最近5轮"),
    ("offhours_history.txt", "非交易时段·当日归档"),
    ("daily_review.txt", "每日复盘(永久)"),
    ("__newdata__", "新数据因子"),  # 第97轮：openvlab隐波/匿名仓单等新数据源因子研究（静态注入）
    ("__research__", "研究报告(全部)"),  # 第87轮：内嵌聚合全部 reports/*.txt 研究/监控报告（不走 iframe）
]
# 报告写出比轮动刻度晚的缓冲秒数（分析耗时），看板在"刻度+缓冲"后刷新
_DASHBOARD_WRITE_DELAY_SEC = 20


def _dashboard_html():
    """多页签实时看板：外层页面不刷新，每10秒轻量探测 report_status.js，
    仅当程序真写出新一轮报告（定时轮动或原油急动紧急轮动）时才重载当前页签内容；
    页头展示最新报告时间/轮次与计划下一轮倒计时。时段参数由 config 注入。"""
    tabs = []
    for i, (fname, label) in enumerate(_DASHBOARD_TABS):
        if fname == "paper_account.txt" and not getattr(config, "PAPER_ENABLED", False):
            continue  # 第28轮：休眠态不生成 paper_account.txt，页签一并隐藏，避免点开是缺失页
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
  <span id="paper-cd" style="display:none;margin-left:10px;color:#f0a35e;font-size:12px;white-space:nowrap;"></span>
</div>
<iframe id="view" src="%s"></iframe>
<div id="charts-panel">/*__CP_DOM__*/</div>
  <div id="research-panel" style="display:none;width:100%%;height:calc(100vh - 45px);overflow-y:auto;background:#17181c;">/*__RP_DOM__*/</div>
  <div id="newdata-panel" style="display:none;width:100%%;height:calc(100vh - 45px);overflow-y:auto;background:#17181c;color:#e8e8e8;padding:16px;font-size:14px;line-height:1.6;">/*__ND_DOM__*/</div>
  <div id="device-panel" style="display:none;width:100%%;height:calc(100vh - 45px);overflow-y:auto;background:#17181c;color:#e8e8e8;padding:16px;font-size:14px;line-height:1.6;">/*__DV_DOM__*/</div>
  <div id="paper-cmp-panel" style="display:none;width:100%%;height:calc(100vh - 45px);overflow-y:auto;background:#17181c;color:#e8e8e8;padding:16px;font-size:14px;line-height:1.6;">/*__CMP_DOM__*/</div>
<script>
  var cur = "%s";
  var CHARTS_VIEW = "__charts__";   // 图表页签为同页内嵌面板，不走 iframe
  // 轮动参数（由 config.py 注入）：时段[起,止](分钟)、开盘快速轮动长度/步长、常规步长、非交易时段步长（分钟）
  var SESSIONS = %s, EARLY_LEN = %d, EARLY_STEP = %d, NORMAL_STEP = %d, OFF_STEP = %d;
  var WRITE_DELAY = %d;   // 轮动刻度后再等这么多秒，等报告写完
  var PAPER_TICK = %d;    // 纸面撮合 ticker 间隔秒（config.PAPER_TICK_INTERVAL），用于纸面页签刷新倒计时
  function show(src) {
    cur = src;
    var isCp = src === CHARTS_VIEW;
    var isRp = src === '__research__';
    var isNd = src === '__newdata__';
    var isDv = src === '__device__';
    var isCmp = src === '__paper_cmp__';
    var view = document.getElementById('view');
    var panel = document.getElementById('charts-panel');
    var rpanel = document.getElementById('research-panel');
    // 纸面对比页签改为 iframe 加载独立 paper_compare.html（ticker 每分钟重写），
    // 不再用静态内嵌面板——面板保持隐藏以兼容旧 DOM。
    view.style.display = (isCp || isRp || isNd || isDv) ? 'none' : 'block';
    panel.style.display = isCp ? 'block' : 'none';
    rpanel.style.display = isRp ? 'block' : 'none';
    var ndpanel = document.getElementById('newdata-panel');
    if (ndpanel) ndpanel.style.display = isNd ? 'block' : 'none';
    var dvpanel = document.getElementById('device-panel');
    if (dvpanel) dvpanel.style.display = isDv ? 'block' : 'none';
    var cmppanel = document.getElementById('paper-cmp-panel');
    if (cmppanel) cmppanel.style.display = 'none';
    // 纸面页签（基准/对比）显示"距下次纸面刷新"倒计时；其余页签隐藏
    var paperCd = document.getElementById('paper-cd');
    if (paperCd) paperCd.style.display = (src === 'paper_account.txt' || isCmp) ? 'inline' : 'none';
    if (isCp) { if (window.ChartPanel) window.ChartPanel.activate(); }
    else if (isRp) { /* 研究聚合为静态注入，无需重载 */ }
    else if (isNd) { /* 新数据因子为静态注入，无需重载 */ }
    else if (isDv) { /* 装置健康为静态注入，无需重载 */ }
    else if (isCmp) { view.src = 'paper_compare.html?t=' + Date.now(); }
    else { view.src = src + '?t=' + Date.now(); }
    var btns = document.querySelectorAll('.tab');
    for (var i = 0; i < btns.length; i++)
      btns[i].classList.toggle('active', btns[i].getAttribute('data-src') === src);
    try { sessionStorage.setItem('dashTab', src); } catch(_){}
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
    if (cur === '__research__') { return; }   // 静态研究索引：不随轮动重载
    if (cur === '__device__') { location.reload(); return; }   // 静态注入，需整页刷新获取新写入内容
    var view = document.getElementById('view');
    view.src = (cur === '__paper_cmp__' ? 'paper_compare.html' : cur) + '?t=' + Date.now();
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
    // 纸面刷新倒计时：ticker 每 PAPER_TICK_INTERVAL 秒撮合一次，页面仅纸面页签显示
    var paperCd = document.getElementById('paper-cd');
    if (paperCd && (cur === 'paper_account.txt' || cur === '__paper_cmp__')) {
      var tickLeft = PAPER_TICK - (now.getSeconds() %% PAPER_TICK);
      if (tickLeft <= 0) tickLeft = PAPER_TICK;
      paperCd.textContent = '纸面下次刷新 ' + pad2(Math.floor(tickLeft / 60)) + ':' + pad2(tickLeft %% 60);
    }
  }
  pollStatus();
  setInterval(pollStatus, POLL_MS);
  tick();
  setInterval(tick, 1000);
  // 纸面独立刷新：paper_account.txt / paper_compare.html 由 ticker 每 PAPER_TICK 秒写入，
  // 本定时器独立于主报告轮次，强制按 PAPER_TICK 周期重载 iframe。
  setInterval(function () {
    var view = document.getElementById('view');
    if (!view) return;
    if (cur === 'paper_account.txt') {
      view.src = 'paper_account.txt?t=' + Date.now();
    } else if (cur === '__paper_cmp__') {
      view.src = 'paper_compare.html?t=' + Date.now();
    }
  }, PAPER_TICK * 1000);
  // 恢复上次选中的标签页（整页刷新后不丢失选中状态）
  try {
    var saved = sessionStorage.getItem('dashTab');
    if (saved && saved !== cur) show(saved);
  } catch(_){}
</script>
<script>
/*__CP_JS__*/
</script>
</body>
</html>"""
    html = _dashboard_tmpl % ("\n  ".join(tabs), first, first, sessions_js, early_len,
                              early_step, normal_step, off_step, _DASHBOARD_WRITE_DELAY_SEC,
                              getattr(config, "PAPER_TICK_INTERVAL", 60))
    _cp_style, _cp_dom, _cp_js = charts.dashboard_embed_parts()
    return (html.replace("/*__CP_STYLE__*/", _cp_style)
                .replace("/*__CP_DOM__*/", _cp_dom)
                .replace("/*__CP_JS__*/", _cp_js)
                .replace("/*__RP_DOM__*/", _research_reports_html())
                .replace("/*__ND_DOM__*/", _newdata_panel_html())
                .replace("/*__DV_DOM__*/", _device_panel_html())
                .replace("/*__CMP_DOM__*/", _paper_compare_html()))


# =========================== 第87轮：研究报告聚合页签（全部 reports/*.txt 融入实时看板） ===========================
_REPORT_TAB_EXCLUDED = {name for name, _label in _DASHBOARD_TABS if not name.startswith("__")}
_REPORT_CATEGORY = {
    "carry_eval": "G23 carry/期限结构", "tsmom_eval": "G7 时序动量",
    "xsmom_eval": "G7 截面动量", "xsmom_long": "G7 长窗复核",
    "expr_research": "G25 表达式因子", "expr_miner": "G25 自动挖掘",
    "orthogonal_blend_oos": "G25 正交合成OOS", "regime_cond_lab": "G25/G29 regime条件化",
    "factor_eval": "G2 因子评估", "factor_health": "G29 因子体检",
    "factor_regime": "G29 regime分层", "attribution": "G28 归因",
    "tradable_mask": "G22 可交易性掩码", "mask_compare_summary": "G22 掩码汇总",
    "microstructure_lab": "G24 微结构", "spec_pressure_lab": "G24 套保/投机压力",
    "spread_lab": "G24 跨期价差", "portfolio_lab": "G26 组合实验台",
    "portfolio_risk_lab": "G5 组合风险", "circuit_review": "G5 熔断校准",
    "wf_cost_lab": "G27 成本敏感性", "trade_journal": "G30 交易复盘",
    "research_review": "G30 研究复盘", "shadow_track": "G7 影子信号",
    "llm_review": "G13 LLM复核", "experiment_ledger_view": "G27 实验台账",
    "research_panel_manifest": "G21 面板清单", "backtest_validation": "G4 回测严谨性",
}


def _newdata_panel_html():
    """第97轮：新数据因子研究页签——读 reports/newdata_factor_research.txt 并格式化为 HTML 面板。"""
    txt_path = os.path.join(config.BASE_DIR, "reports", "newdata_factor_research.txt")
    try:
        with open(txt_path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        raw = "暂无数据——因子研究需 main 运行后自动积累。"
    lines = raw.splitlines()
    parts = []
    in_table = False
    for ln in lines:
        s_esc = ln.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if ln.startswith("===="):
            parts.append(f'<div style="font-weight:bold;font-size:16px;margin-top:24px;border-bottom:1px solid #444;padding-bottom:6px;">{s_esc}</div>')
        elif ln.startswith("【"):
            title = s_esc.strip("【】").split("】")[0] if "】" in s_esc else s_esc
            parts.append(f'<div style="font-weight:bold;font-size:15px;margin-top:18px;color:#8cf;">{s_esc}</div>')
        elif ln.startswith("  ") or ln.startswith(" "):
            parts.append(f'<div style="font-family:monospace;font-size:13px;color:#b8b8b8;">{s_esc}</div>')
        elif ln.startswith("-") and len(ln) < 10:
            parts.append('<hr style="border:none;border-top:1px solid #444;margin:12px 0 0;">')
        else:
            parts.append(f'<p style="margin:8px 0 0;">{s_esc}</p>')
    return "\n".join(parts)


def _device_panel_html():
    """协同：装置健康详情页签——读 report_device.json（装置 fusion.StatusHub 写）。

    报告位置：装置目录 data/report_device.json（E:\\LHsystem\\界面操作收集装置\\data\\）
    结构化展示：软件在线（Legend/同花顺）、数据源 ok/hits、行情条数、期权链、
    最近告警。文件缺失（装置未启动）时显示提示，不影响看板。
    """
    # 装置 data 目录：E:\LHsystem\界面操作收集装置\data\（BASE_DIR 上一级的上一个项目目录）
    dev_json = os.path.join(os.path.dirname(os.path.dirname(config.BASE_DIR)),
                            "界面操作收集装置", "data", "report_device.json")
    parts = ['<div style="font-weight:bold;font-size:16px;border-bottom:1px solid #444;padding-bottom:6px;">数据采集装置（界面操作）健康详情</div>']
    try:
        import json as _json
        with open(dev_json, encoding="utf-8") as f:
            rep = _json.load(f)
    except OSError:
        parts.append('<p style="margin:12px 0;color:#b8b8b8;">装置未启动或报告未生成——'
                     '先运行 <code>start_all.bat</code>（或装置 <code>run.py --daemon</code>）后刷新。'
                     '装置启动后自动写 reports/device_status.txt 与此 JSON。</p>')
        return "\n".join(parts)
    except Exception as e:
        parts.append(f'<p style="margin:12px 0;color:#b8b8b8;">装置报告解析失败: {e}</p>')
        return "\n".join(parts)

    upd = str(rep.get("updated") or "--")
    cols = str(rep.get("collections") or 0)
    parts.append(f'<p style="margin:8px 0;">最近更新: {upd} ｜ 采集轮数: {cols}</p>')

    # 软件在线
    parts.append('<div style="font-weight:bold;font-size:14px;margin-top:16px;color:#8cf;">软件在线</div>')
    sw = rep.get("software") or {}
    if not sw:
        parts.append('<p style="margin:4px 0;color:#b8b8b8;">无软件状态（未探测）</p>')
    for k, v in sw.items():
        online = bool(v.get("online"))
        badge = '<span style="color:#37c27a;">在线</span>' if online else '<span style="color:#e05b5b;">离线</span>'
        detail = str(v.get("detail") or "")
        parts.append(f'<p style="margin:4px 0;">{badge}  {k}  <span style="color:#b8b8b8;">{detail}</span></p>')

    # 数据源
    parts.append('<div style="font-weight:bold;font-size:14px;margin-top:16px;color:#8cf;">数据源健康</div>')
    srcs = rep.get("sources") or {}
    if not srcs:
        parts.append('<p style="margin:4px 0;color:#b8b8b8;">暂无数据源上报</p>')
    for k, v in srcs.items():
        ok = bool(v.get("ok"))
        hits = int(v.get("hits", 0))
        badge = '<span style="color:#37c27a;">OK</span>' if ok else '<span style="color:#e05b5b;">FAIL</span>'
        parts.append(f'<p style="margin:4px 0;font-family:monospace;font-size:13px;">{badge}  {k}  (hits={hits})</p>')

    # 行情/期权概览
    parts.append('<div style="font-weight:bold;font-size:14px;margin-top:16px;color:#8cf;">采集概览</div>')
    q_total = int(rep.get("quotes_total") or 0)
    parts.append(f'<p style="margin:4px 0;">行情快照: {q_total} 条</p>')
    by_src = rep.get("quotes_by_source") or {}
    if by_src:
        parts.append(f'<p style="margin:4px 0;color:#b8b8b8;">按源: {"，".join(f"{k}={v}" for k, v in by_src.items())}</p>')
    opts = rep.get("options") or []
    parts.append(f'<p style="margin:4px 0;">期权链: {len(opts)} 条</p>')
    cov = rep.get("coverage") or {}
    if cov:
        covs = []
        for k, v in cov.items():
            if isinstance(v, dict) and "bars" in v:
                covs.append(f"{k}m={v['bars']}根/{v.get('contracts', '?')}合约")
            elif isinstance(v, dict) and "rows" in v:
                covs.append(f"option_chains={v['rows']}行")
        if covs:
            parts.append(f'<p style="margin:4px 0;color:#b8b8b8;">覆盖: {"，".join(covs)}</p>')

    # 告警
    alerts = rep.get("alerts") or []
    if alerts:
        parts.append('<div style="font-weight:bold;font-size:14px;margin-top:16px;color:#f0a35e;">最近告警</div>')
        for a in alerts[-8:]:
            ts = str(a.get("ts") or "")
            code = str(a.get("code") or "")
            reason = str(a.get("reason") or "")
            parts.append(f'<p style="margin:4px 0;font-family:monospace;font-size:13px;">[{ts}] {code}: {reason}</p>')
    # 第110轮：看板合一——内嵌装置 dashboard（http://127.0.0.1:{port}/dashboard.html）。
    # 装置 --serve 常驻时直接内嵌其完整显示页；未启动则保留上方结构化摘要（兼容）。
    try:
        dev_cfg = os.path.join(os.path.dirname(os.path.dirname(config.BASE_DIR)),
                               "界面操作收集装置", "config.json")
        port = 8790
        if os.path.exists(dev_cfg):
            import json as _json2
            with open(dev_cfg, encoding="utf-8") as f:
                port = int((_json2.load(f).get("http") or {}).get("serve_port") or 8790)
        dev_dash = "http://127.0.0.1:%d/dashboard.html" % port
        parts.append(
            '<div style="font-weight:bold;font-size:14px;margin-top:16px;color:#8cf;">装置 Dashboard（看板合一）</div>'
            '<iframe src="%s" style="width:100%%;height:520px;border:1px solid #333;border-radius:6px;'
            'background:#0e0f13;"></iframe>'
            '<p style="margin:6px 0;color:#9a9a9a;font-size:12px;">内嵌装置显示页（%s）。'
            '若此框空白：装置 <code>--serve</code> 未启动（先跑 <code>start_all.bat</code>），上方为结构化摘要。</p>'
            % (dev_dash, dev_dash))
    except Exception:
        pass
    return "\n".join(parts)


# ---------- 第102轮：纸面账户对比页签 ----------
def _paper_compare_html():
    """第105轮重构：纸面账户对比看板（读 paper_compare.json 渲染）。

    结构：顶部 5 张档位卡 → 主表（9列，风险度迷你进度条，基准行高亮ⓑ，每行 <details>
    折叠持仓/成交/挂单明细）→ 底部汇总。颜色统一红涨绿跌（与 charts.py 一致）。
    点账户名 → 二级详情页 paper_detail_{name}.html。
    """
    cmp_path = os.path.join(config.BASE_DIR, "reports", "paper_compare.json")
    try:
        import json as _json
        with open(cmp_path, encoding="utf-8") as f:
            rows = _json.load(f)
    except Exception:
        return ('<div style="font-weight:bold;font-size:16px;border-bottom:1px solid #444;padding-bottom:6px;">'
                '纸面账户对比</div><p style="color:#f88;">paper_compare.json 未生成（程序需完成至少一轮分析）</p>')

    def _esc(s):
        return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    # ---- 档位卡统计 ----
    tier_order = [100_000, 10_000, 5_000, 3_000, 1_000]
    tier_label = {100_000: "10万档", 10_000: "1万档", 5_000: "5000档", 3_000: "3000档", 1_000: "1000档"}
    cards = []
    for eq0k in tier_order:
        grp = [r for r in rows if abs((r.get("equity0") or 0) - eq0k) < 1.0]
        if not grp:
            continue
        rets = [r.get("ret") or 0 for r in grp]
        best = max(grp, key=lambda r: r.get("ret") or 0)
        worst = min(grp, key=lambda r: r.get("ret") or 0)
        mdd = [abs(r.get("max_drawdown") or 0) for r in grp]
        mid_mdd = sorted(mdd)[len(mdd) // 2] if mdd else 0.0
        avg = sum(rets) / len(rets)
        _cls = "pos" if avg >= 0 else "neg"
        _cls_best = "pos" if (best.get("ret") or 0) >= 0 else "neg"
        _cls_worst = "pos" if (worst.get("ret") or 0) >= 0 else "neg"
        cards.append(
            f'<div class="tier-card" style="border-left:3px solid {_tier_accent(eq0k)};">'
            f'<div class="tc-name">{tier_label.get(eq0k, str(eq0k))}</div>'
            f'<div class="tc-avg {_cls}">档均 {avg:+.2%}</div>'
            f'<div class="tc-sub">账户 {len(grp)} 个 · 中位回撤 {mid_mdd:.1%}</div>'
            f'<div class="tc-sub">最佳 {_esc(best.get("name",""))} <span class="{_cls_best}">{best.get("ret") or 0:+.2%}</span>'
            f' · 最差 {_esc(worst.get("name",""))} <span class="{_cls_worst}">{worst.get("ret") or 0:+.2%}</span></div>'
            f'</div>')
    # ---- 主表 ----
    def _fcard(label, value, cls=""):
        return ('<div class="f-card"><div class="f-lb">%s</div>'
                '<div class="f-val%s">%s</div></div>'
                % (label, (" " + cls) if cls else "", value))

    def _fmt_perf(key, v):
        """绩效指标格式化：小数比例类显示为百分比，比率/次数直接显示。"""
        if v is None:
            return "—"
        pct_keys = ("ann_ret", "win_rate", "avg_risk")
        if key in pct_keys:
            return "%+.2f%%" % (v * 100.0) if key == "ann_ret" else "%.1f%%" % (v * 100.0)
        if key == "max_drawdown":
            return "%.2f%%" % (abs(v) * 100.0)
        if key == "n_trades":
            return "%d" % int(v)
        return "%.2f" % v

    def _st_badges(r):
        """委托状态小徽章：在途/已成交/拒单/撤单计数（status 缺失或全 0 时隐藏）。"""
        _st = r.get("status") or {}
        if not _st or not any(_st.values()):
            return ""
        _items = (("pending", "#f0a35e", "在途"), ("filled", "#43c589", "已成交"),
                  ("rejected", "#ef6b6b", "拒单"), ("cancelled", "#9a9a9a", "撤单"))
        _html = "".join(
            '<span class="status-badge" style="color:%s;" title="%s">%s %d</span>' % (c, t, t, int(_st.get(k) or 0))
            for k, c, t in _items if int(_st.get(k) or 0) > 0)
        return '<div class="st-badges">%s</div>' % _html if _html else ""

    def _detail_html(r):
        d = r.get("detail") or {}
        pos = d.get("positions") or []
        tr = d.get("trades") or []
        od = d.get("orders") or []
        out = []
        # 第111轮：账户资金概览（4 格卡；旧 JSON 无字段时整块隐藏）
        if any(r.get(k) is not None for k in ("static", "float_pnl", "margin_used", "available")):
            out.append('<div class="dd-sec">账户资金概览</div><div class="perf-grid">'
                       + _fcard("静态权益", _yuan(r.get("static") or 0))
                       + _fcard("浮动盈亏", _yuan(r.get("float_pnl") or 0),
                                "pos" if (r.get("float_pnl") or 0) >= 0 else "neg")
                       + _fcard("保证金占用", _yuan(r.get("margin_used") or 0))
                       + _fcard("可用资金", _yuan(r.get("available") or 0))
                       + '</div>')
        # 第111轮：绩效指标网格（缺失项自动跳过；最大回撤主表已有列，这里不重复）
        _perf_keys = (("ann_ret", "年化"), ("sharpe", "夏普"), ("win_rate", "胜率"),
                      ("pl_ratio", "盈亏比"), ("profit_factor", "利润因子"), ("n_trades", "交易数"),
                      ("avg_win", "平均盈"), ("avg_risk", "平均风险"))
        _perf_cells = [_fcard(_lb, _fmt_perf(_k, r.get(_k)))
                       for _k, _lb in _perf_keys if r.get(_k) is not None]
        if _perf_cells:
            out.append('<div class="dd-sec">绩效指标</div><div class="perf-grid">%s</div>'
                       % "".join(_perf_cells))
        # 第111轮：成交汇总（名义/滑点/开平/强平/跳过，全部为空时隐藏）
        _fill_keys = (("notional", "名义总额"), ("slip_yuan", "滑点"), ("n_opens", "开仓"),
                      ("n_closes", "平仓"), ("n_liquidations", "强平"), ("n_skipped", "跳过"))
        _fill_cells = []
        for _k, _lb in _fill_keys:
            _v = r.get(_k)
            if _v in (None, "", 0, 0.0):
                continue
            if _k in ("notional", "slip_yuan"):
                _txt = _yuan(_v)
                _cls = "neg" if (_k == "slip_yuan" and (r.get(_k) or 0) > 0) else ""
            else:
                _txt = "%d 次" % int(_v)
                _cls = ""
            _fill_cells.append(_fcard(_lb, _txt, _cls))
        if _fill_cells:
            out.append('<div class="dd-sec">成交汇总</div><div class="perf-grid">%s</div>'
                       % "".join(_fill_cells))
        if pos:
            out.append('<div class="dd-sec">持仓明细（%d）</div><table class="dd-table">'
                       '<tr><th>品种</th><th>合约</th><th>方向</th><th>手数</th><th>开仓价</th>'
                       '<th>最新价</th><th>浮动盈亏</th><th>占用保证金</th></tr>'
                       % len(pos))
            for p in pos:
                out.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%d</td><td class="num">%s</td>'
                           '<td class="num">%s</td><td class="num %s">%s</td><td class="num">%s</td></tr>'
                           % (_esc(p.get("sym")), _esc(p.get("contract")), _esc(p.get("dir")),
                              int(p.get("lots") or 0), _fmt(p.get("entry"), 2), _fmt(p.get("last"), 3),
                              "pos" if (p.get("float") or 0) >= 0 else "neg", _fmt(p.get("float"), 0),
                              _fmt(p.get("margin"), 0)))
            out.append("</table>")
        if tr:
            out.append('<div class="dd-sec">成交流水（最近%d笔）</div><table class="dd-table">'
                       '<tr><th>时间</th><th>品种</th><th>合约</th><th>方向</th><th>手数</th><th>开平</th>'
                       '<th>成交价</th><th>手续费</th><th>净盈亏</th><th>原因</th></tr>' % len(tr))
            for t in tr:
                out.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%d</td><td>%s</td>'
                           '<td class="num">%s</td><td class="num">%s</td><td class="num %s">%s</td><td>%s</td></tr>'
                           % (_esc(t.get("ts"))[:19], _esc(t.get("sym")), _esc(t.get("contract")),
                              _esc(t.get("dir")), int(t.get("lots") or 0), _esc(t.get("leg")),
                              _fmt(t.get("price"), 2), _fmt(t.get("fee"), 2),
                              "pos" if (t.get("realized") or 0) >= 0 else "neg", _fmt(t.get("realized"), 0),
                              _esc(t.get("reason"))))
            out.append("</table>")
        if od:
            out.append('<div class="dd-sec">在途挂单（最近%d条）</div><table class="dd-table">'
                       '<tr><th>时间</th><th>品种</th><th>动作</th><th>状态</th><th>手数</th><th>原因</th></tr>'
                       % len(od))
            for o in od:
                out.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%d</td><td>%s</td></tr>'
                           % (_esc(o.get("ts"))[:19], _esc(o.get("sym")), _esc(o.get("action")),
                              _esc(o.get("status")), int(o.get("lots") or 0), _esc(o.get("reason"))))
            out.append("</table>")
        if not out:
            return ('<details class="dd"><summary>展开明细（持仓 0 · 成交 0 · 挂单 0）</summary>'
                    '<div class="dd-sec" style="color:#9a9a9a;">该账户暂无持仓/成交/挂单记录'
                    '（重置后新账或休眠）</div></details>')
        return '<details class="dd"><summary>展开明细（持仓 %d · 成交 %d · 挂单 %d）</summary>%s</details>' % (
            len(pos), len(tr), len(od), "".join(out))

    def _risk_bar(v):
        v = float(v or 0.0)
        pct = max(0.0, min(100.0, v * 100.0))
        color = "#43c589" if v < 0.5 else ("#f9ca24" if v < 0.7 else "#ef6b6b")
        return ('<div class="risk-wrap" title="风险度 %.1f%%">'
                '<div class="risk-bar" style="width:%.1f%%;background:%s;"></div></div>'
                % (v * 100, pct, color))

    parts = ['<div style="font-weight:bold;font-size:16px;border-bottom:1px solid #444;padding-bottom:6px;">'
             '纸面账户对比（%d 个影子账户 · 5档资金 × 3风格+赌徒）</div>' % len(rows)]
    parts.append('<style>'
                 '.tier-cards{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0;}'
                 '.tier-card{background:#1f2127;border:1px solid #333;border-radius:6px;padding:10px 14px;min-width:150px;}'
                 '.tc-name{font-weight:bold;color:#7ecbff;font-size:14px;}'
                 '.tc-avg{font-size:16px;font-weight:bold;margin:4px 0;}'
                 '.tc-sub{color:#9a9a9a;font-size:11px;}'
                 '.cmp-table{border-collapse:collapse;width:100%;font-size:13px;margin-top:6px;}'
                 '.cmp-table th{background:#2a2a2a;padding:6px 8px;text-align:left;border-bottom:2px solid #444;white-space:nowrap;position:sticky;top:0;}'
                 '.cmp-table td{padding:5px 8px;border-bottom:1px solid #2c2c2c;white-space:nowrap;}'
                 '.cmp-table tr.baseline td{background:#14262b;}'
                 '.cmp-table tr:hover{background:#252525;}'
                 '.pos{color:#ef6b6b;}.neg{color:#43c589;}'
                 '.opt-badge{background:#1e3a5f;color:#7ecbff;padding:1px 4px;border-radius:3px;font-size:11px;}'
                 '.b-badge{background:#0e639c;color:#fff;padding:0 4px;border-radius:3px;font-size:11px;}'
                 '.g-badge{background:#e17055;color:#fff;padding:0 4px;border-radius:3px;font-size:11px;}'
                 '.cmp-table tr.gambler td{background:#241812;}'
                 '.risk-wrap{width:90px;height:10px;background:#333;border-radius:5px;overflow:hidden;display:inline-block;vertical-align:middle;}'
                 '.risk-bar{height:100%;}'
                 '.dd{margin:2px 0;}'
                 '.dd summary{cursor:pointer;color:#7ecbff;font-size:12px;padding:3px 0;}'
                 '.dd-table{width:100%;font-size:12px;border-collapse:collapse;margin:4px 0 8px;}'
                 '.dd-table th{background:#23252b;padding:4px 8px;text-align:left;border-bottom:1px solid #444;white-space:nowrap;}'
                 '.dd-table td{padding:3px 8px;border-bottom:1px solid #2c2c2c;white-space:nowrap;}'
                 '.dd-sec{color:#9a9a9a;font-size:12px;margin:6px 0 2px;}'
                 '.num{text-align:right;}'
                 'a.acct{color:#7ecbff;text-decoration:none;}a.acct:hover{text-decoration:underline;}'
                 '.cmp-table th.th-num{text-align:right;}'                # 数字列表头与 .num 数据右对齐
                 '.cmp-table td.wrap{white-space:normal;}'                # 文本长列（可交易性/详情）允许换行
                 '.cmp-scroll{overflow-x:auto;}'                          # 主表横向滚动兜底
                 '.st-badges{display:flex;gap:3px;margin-top:3px;flex-wrap:wrap;}'
                 '.status-badge{font-size:11px;padding:0 4px;border-radius:3px;background:#23252b;border:1px solid #333;white-space:nowrap;}'
                 '.perf-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(125px,1fr));gap:6px;margin:6px 0 10px;}'
                 '.f-card{background:#1f2127;border:1px solid #333;border-radius:5px;padding:5px 8px;}'
                 '.f-lb{color:#9a9a9a;font-size:11px;}'
                 '.f-val{font-size:13px;font-weight:bold;color:#e8e8e8;}'
                 '.f-val.pos{color:#ef6b6b;}.f-val.neg{color:#43c589;}'
                 '.tier-agg td{background:#181a1f;padding:4px 10px;}'     # 档位聚合行（暗底区分）
                 '</style>')
    if cards:
        parts.append('<div class="tier-cards">%s</div>' % "".join(cards))
    # 主表：按档分组，档内固定 激进→基准→保守→赌徒（基准高亮），带 <details> 明细；数字列表头右对齐
    parts.append('<div class="cmp-scroll"><table class="cmp-table">'
                 '<tr><th>账户</th><th class="th-num">权益</th><th class="th-num">收益率</th>'
                 '<th class="th-num">最大回撤</th><th class="th-num">风险度</th>'
                 '<th class="th-num">期/权持仓</th><th class="th-num">已实现</th>'
                 '<th class="th-num">手续费</th><th>可交易性</th><th>详情</th></tr>')
    style_order = {"激进": 0, "基准": 1, "保守": 2, "赌徒": 3}
    for eq0k in tier_order:
        grp = [r for r in rows if abs((r.get("equity0") or 0) - eq0k) < 1.0]
        if not grp:
            continue
        grp.sort(key=lambda r: style_order.get(r.get("style"), 9))
        parts.append(f'<tr class="tier-row" style="background:#1f1f1f;font-weight:bold;color:#7ecbff;">'
                     f'<td colspan="10">{tier_label.get(eq0k, str(eq0k))}（初始 {eq0k:,.0f} 元）</td></tr>')
        # 第111轮续：档位聚合查看——该档全部账户的成交/挂单明细合并（各账户各保留 50 条，按时间合并排序）
        _agg_trades, _agg_orders = [], []
        for _r in grp:
            _d = _r.get("detail") or {}
            for _t in (_d.get("trades") or []):
                _agg_trades.append(dict(_t, _acct=_r.get("name", "")))
            for _o in (_d.get("orders") or []):
                _agg_orders.append(dict(_o, _acct=_r.get("name", "")))
        _agg_trades.sort(key=lambda t: t.get("ts") or "")
        _agg_orders.sort(key=lambda o: o.get("ts") or "")
        _agg_html = ""
        if _agg_trades:
            _agg_html += ('<div class="dd-sec">成交明细（该档 %d 笔，各账户最多 50 笔）</div>'
                          '<table class="dd-table"><tr><th>账户</th><th>时间</th><th>品种</th><th>合约</th>'
                          '<th>方向</th><th>手数</th><th>开平</th><th>成交价</th><th>手续费</th>'
                          '<th>净盈亏</th><th>原因</th></tr>' % len(_agg_trades))
            for _t in _agg_trades:
                _agg_html += ('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%d</td><td>%s</td>'
                              '<td class="num">%s</td><td class="num">%s</td><td class="num %s">%s</td><td>%s</td></tr>'
                              % (_esc(_t.get("_acct")), _esc(_t.get("ts"))[:19], _esc(_t.get("sym")),
                                 _esc(_t.get("contract")), _esc(_t.get("dir")), int(_t.get("lots") or 0),
                                 _esc(_t.get("leg")), _fmt(_t.get("price"), 2), _fmt(_t.get("fee"), 2),
                                 "pos" if (_t.get("realized") or 0) >= 0 else "neg", _fmt(_t.get("realized"), 0),
                                 _esc(_t.get("reason"))))
            _agg_html += "</table>"
        if _agg_orders:
            _agg_html += ('<div class="dd-sec">挂单明细（该档 %d 条，各账户最多 50 条）</div>'
                          '<table class="dd-table"><tr><th>账户</th><th>时间</th><th>品种</th><th>动作</th>'
                          '<th>状态</th><th>手数</th><th>原因</th></tr>' % len(_agg_orders))
            for _o in _agg_orders:
                _agg_html += ('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%d</td><td>%s</td></tr>'
                              % (_esc(_o.get("_acct")), _esc(_o.get("ts"))[:19], _esc(_o.get("sym")),
                                 _esc(_o.get("action")), _esc(_o.get("status")), int(_o.get("lots") or 0),
                                 _esc(_o.get("reason"))))
            _agg_html += "</table>"
        if _agg_html:
            parts.append(
                f'<tr class="tier-agg"><td colspan="10">'
                f'<details class="dd"><summary>查看该档成交/挂单聚合（成交 {len(_agg_trades)} 笔 · 挂单 {len(_agg_orders)} 条）</summary>'
                f'{_agg_html}</details></td></tr>')
        for r in grp:
            ret = r.get("ret") or 0
            _cls = "pos" if ret >= 0 else "neg"
            style = r.get("style", "")
            baseline = (style == "基准")
            gambler = (style == "赌徒")
            _row_cls = ' class="gambler"' if gambler else (' class="baseline"' if baseline else "")
            bd = (' <span class="g-badge">赌徒</span>' if gambler
                  else ' <span class="b-badge">基准</span>' if baseline else "")
            opt_flag = ' <span class="opt-badge">期权</span>' if r.get("n_opt_pos", 0) > 0 else ""
            name_html = ('<a class="acct" href="paper_detail_%s.html" title="打开该账户详情页">%s</a>%s%s'
                         % (r.get("name", "").replace(" ", "_").replace("/", "_"),
                            _esc(r.get("name")), bd, opt_flag))
            dd = _detail_html(r)
            # 第110轮：可交易性说明（能开1手的最便宜品种及一手保证金；纯期权档/资金不足标 None）
            _aff = r.get("affordable_sym")
            if _aff:
                _aff_txt = _esc(_aff)
                if r.get("affordable_margin"):
                    _aff_txt += '(%s/手)' % _fmt(r.get("affordable_margin"), 0)
                aff_html = ('<span style="color:#43c589;" title="该档 per_symbol 预算下能开 1 手的最便宜品种">'
                            '%s</span>' % _aff_txt)
            elif r.get("priority") == "option_only" or r.get("futures_max") == 0:
                aff_html = '<span style="color:#9a9a9a;">纯期权</span>'
            else:
                aff_html = '<span style="color:#ef6b6b;" title="该档 per_symbol 预算不足以开任何一键">无品种 ✗</span>'
            parts.append(
                f'<tr{_row_cls}>'
                f'<td class="wrap">{name_html}{_st_badges(r)}</td>'
                f'<td class="num">{r.get("equity") or 0:,.1f}</td>'
                f'<td class="num {_cls}">{ret:+.2%}</td>'
                f'<td class="num">{abs((r.get("max_drawdown") or 0)) * 100:.2f}%</td>'
                f'<td>{_risk_bar(r.get("risk_degree"))}</td>'
                f'<td class="num">{r.get("n_fut_pos") or 0} / {r.get("n_opt_pos") or 0}</td>'
                f'<td class="num">{r.get("realized") or 0:,.0f}</td>'
                f'<td class="num">{r.get("fees") or 0:,.0f}</td>'
                f'<td class="wrap">{aff_html}</td>'
                f'<td>{dd}</td>'
                f'</tr>')
    parts.append('</table></div>')   # 关闭 cmp-scroll 滚动容器
    # 底部汇总（等权均值/最佳/最差按涨跌着色；新增全账户合计）
    if rows:
        all_ret = [(r.get("ret") or 0) for r in rows]
        best = max(rows, key=lambda r: r.get("ret") or 0)
        worst = min(rows, key=lambda r: r.get("ret") or 0)
        _avg = sum(all_ret) / len(all_ret)
        _tot_fees = sum(r.get("fees") or 0 for r in rows)
        _tot_real = sum(r.get("realized") or 0 for r in rows)
        parts.append(
            '<p style="color:#9a9a9a;font-size:12px;margin-top:10px;">'
            '等权均值 <span class="%s">%+.2f%%</span> · 最佳 <span class="%s">%s(%+.2f%%)</span>'
            ' · 最差 <span class="%s">%s(%+.2f%%)</span> · '
            '全账户合计：手续费 %s 元 / 已实现 %s 元 · '
            '点账户名进入详情页；第104轮起期权与期货统一资金池（权益合并）</p>'
            % ("pos" if _avg >= 0 else "neg", _avg,
               "pos" if (best.get("ret") or 0) >= 0 else "neg", _esc(best.get("name")), best.get("ret") or 0,
               "pos" if (worst.get("ret") or 0) >= 0 else "neg", _esc(worst.get("name")), worst.get("ret") or 0,
               _fmt(_tot_fees, 0), _fmt(_tot_real, 0)))
    return "\n".join(parts)


def _tier_accent(eq0k):
    """档位强调色（非涨跌语义的中性色，与 charts._TIER_COLORS 同家族）。"""
    return {100_000: "#ff7675", 10_000: "#74b9ff", 5_000: "#55efc4",
            3_000: "#a29bfe", 1_000: "#fd79a8"}.get(eq0k, "#7ecbff")


def _research_reports_html(max_rows=14, max_bytes=2200):
    """聚合 reports/*.txt（排除看板实时页签已覆盖的）成卡片网格 HTML。

    每卡：报告名 + 类别 + 更新时间 + 内容摘要(<pre> 前 max_rows 行/前 max_bytes 字符) +
    全文链接（新标签打开 txt）。纯展示、只读、html 转义防注入。"""
    reports_dir = os.path.join(config.BASE_DIR, "reports")
    cards = []
    if os.path.isdir(reports_dir):
        for fn in sorted(os.listdir(reports_dir)):
            if not fn.endswith(".txt") or fn in _REPORT_TAB_EXCLUDED:
                continue
            path = os.path.join(reports_dir, fn)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    head = f.read(max_bytes)
            except OSError:
                continue
            mtime = os.path.getmtime(path)
            try:
                mt = datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M")
            except Exception:
                mt = ""
            base = fn[:-4]
            cat = "其他"
            for pref, c in _REPORT_CATEGORY.items():
                if base.startswith(pref):
                    cat = c
                    break
            lines = head.splitlines()
            body = "\n".join(lines[:max_rows])
            esc = html.escape
            cards.append(
                '<div class="rp-card">'
                '<div class="rp-head"><span class="rp-name">%s</span>'
                '<span class="rp-cat">%s</span><span class="rp-time">%s</span></div>'
                '<pre class="rp-pre">%s</pre>'
                '<div class="rp-foot"><a href="%s" target="_blank" rel="noopener">查看全文（新标签）</a></div>'
                '</div>' % (esc(fn), esc(cat), esc(mt), esc(body), esc(fn)))
    grid = "\n".join(cards)
    return ("""<style>
  #research-panel { padding: 12px; }
  .rp-grid { display: grid; grid-template-columns: repeat(auto-fill,minmax(430px,1fr)); gap: 12px; }
  .rp-card { background:#202229; border:1px solid #33363d; border-radius:6px; padding:10px 12px; }
  .rp-head { display:flex; align-items:baseline; gap:10px; margin-bottom:6px; flex-wrap:wrap; }
  .rp-name { color:#7ecbff; font-weight:bold; font-size:13px; }
  .rp-cat { color:#b8c; font-size:11px; }
  .rp-time { margin-left:auto; color:#8a8f98; font-size:11px; }
  .rp-pre { background:#16171b; border:1px solid #2a2d33; border-radius:4px; padding:8px;
            font:12px/1.5 Consolas,"Microsoft YaHei",monospace; color:#cfd3dc;
            max-height:320px; overflow:auto; white-space:pre-wrap; word-break:break-all; margin:0 0 8px; }
  .rp-foot a { color:#7ecbff; font-size:12px; text-decoration:none; }
  .rp-foot a:hover { text-decoration:underline; }
</style>
<div class="rp-grid">
%s
</div>
<p style="color:#8a8f98;font-size:12px;margin-top:10px">共 %d 份研究报告（自动聚合 reports/*.txt，排除实时看板既有页签；点击卡片内链接可在新标签查看全文）</p>
""" % (grid, len(cards)))


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


# ---------------- G1（二）纸面账户：paper_account.txt + 正文紧凑块（独立成段，不改主链口径） ----------------

def _wan(v, d=2):
    try:
        return "%.*f万" % (d, float(v) / 10000.0)
    except (TypeError, ValueError):
        return "-"


def _yuan(v, d=0):
    try:
        return format(float(v), ",.%df" % d)   # 千分位金额
    except (TypeError, ValueError):
        return "-"


def _pct(v, d=2):
    try:
        return "%.*f%%" % (d, float(v) * 100.0)
    except (TypeError, ValueError):
        return "-"


def _num(v, fmt="%.2f"):
    """G3 指标统一空值显示：None/非有限值显示 '-'。"""
    try:
        if v is None:
            return "-"
        fv = float(v)
        if fv != fv:
            return "-"
        return fmt % fv
    except (TypeError, ValueError):
        return "-"


def _paper_excursion_tail(mm):
    """G3 MAE/MFE 汇总压成行尾补充文本；无样本返回空串。"""
    if not mm or mm.get("n", 0) <= 0:
        return ""
    return "   持仓过程 平均MFE %s / 平均MAE %s（%d笔）" % (
        _pct(mm.get("avg_mfe")), _pct(mm.get("avg_mae")), mm.get("n", 0))


def _paper_monthly_lines(perf):
    """把 portfolio.performance()['monthly']（G3 月度矩阵）压成每年一行紧凑文本；无数据返回[]。"""
    monthly = (perf or {}).get("monthly")
    if not monthly or not monthly.get("matrix"):
        return []
    out = [" 月度收益（自然月复利，%）："]
    for year in monthly["years"]:
        cells = []
        for m in range(1, 13):
            v = monthly["matrix"][year].get(m)
            if v is None:
                continue
            cells.append("%d月%+.2f" % (m, v * 100.0))
        if cells:
            out.append("  %d  " % year + "  ".join(cells))
    return out


_PAPER_ACTION_CN = {"open": "开仓", "close": "离场平仓", "reverse_close": "反手平仓",
                    "reverse_open": "反手开仓", "liquidate": "风控强平"}
_PAPER_SIDE_CN = {"buy": "买入", "sell": "卖出"}


def paper_block(state):
    """实时报告正文中的紧凑纸面账户块（第102轮：多账户循环输出；PAPER_ENABLED 休眠/异常时返回空列表）。"""
    brokers = getattr(state, "papers", {}) or {}
    lines = []
    # 多账户优先；无多账户时回退单账户（paper）
    if not brokers and getattr(state, "paper", None) is not None:
        brokers = {"基准": getattr(state, "paper", None)}
    if not brokers:
        return []
    for name, pb in brokers.items():
        try:
            a = pb.account_summary()
        except Exception:
            continue
        s = (getattr(state, "last_papers", {}) or {}).get(
            getattr(pb, "name", "") or name) or \
            (getattr(state, "last_paper", None) or {} if not getattr(state, "last_papers", {}) else {})
        snap = s.get("snapshot") or {}
        ret = (a["equity"] / a["equity0"] - 1.0) if a["equity0"] else 0.0
        st = a["status"]
        opt = a.get("opt") or {}
        opt_n = opt.get("n_positions", 0)
        opt_line = ""
        if opt_n > 0:
            opt_line = "｜期权持仓%d 已实现%s" % (opt_n, _yuan(opt.get("realized", 0.0)))
        lines += [
            "【纸面·%s】(成交档=%s entry=%s 参与=%s；严格按综合分信号自动虚拟撮合，含真实手续费+滑点)"
            % (name, a["fill_mode"], getattr(pb, "entry_score", 0),
               getattr(pb, "priority", "futures_first")),
            " 动态权益%s(%+.2f%%) 静态%s 浮动%s元 已实现%s元 累计手续费%s元%s" % (
                _wan(a["equity"]), ret * 100.0, _wan(a["static"]),
                _yuan(snap.get("float_pnl", 0.0)), _yuan(a["realized"]), _yuan(a["fees_paid"]),
                opt_line),
            " 保证金占用%s 可用%s 风险度%s｜持仓%d 在途挂单%d 累计平仓%d(强平%d)｜本轮委托%d/成交%d" % (
                _wan(a["margin_used"]), _wan(a["available"]), _pct(a["risk_degree"], 1),
                a["n_positions"], a["n_pending"], a["n_closed"], a["n_liquidations"],
                s.get("n_orders", 0), s.get("n_trades", 0)),
            " 委托状态：已成交%d 在途排队%d 锁板/无价阻塞%d 确定拒单%d 已撤%d｜约束排队尝试(内部日志)%d（完整账户见 paper_account_%s.txt）"
            % (st["filled"], st["pending"], st["blocked"], st["rejected"], st["cancelled"],
               a["n_skipped"], name.replace(" ", "_")),
            "",
        ]
    return lines


def paper_account_text(state, broker=None):
    """完整纸面账户快照（reports/paper_account_{name}.txt，看板页签直接加载；休眠返回空串）。
    broker: 指定账户实例（第102轮多账户扩展，默认用 state.paper 向后兼容）。"""
    pb = broker or getattr(state, "paper", None)
    if pb is None:
        return ""
    a = pb.account_summary()
    s = getattr(state, "last_paper", None) or {}
    # 若 broker 有 last_summary（多账户模式下从 state.last_papers 取），优先用
    if broker is not None:
        _name = getattr(broker, "name", "")
        if _name and hasattr(state, "last_papers"):
            _ls = (state.last_papers or {}).get(_name) or {}
            if _ls:
                s = _ls
    snap = s.get("snapshot") or {}
    perf = a.get("performance")
    sep = "=" * 100
    thin = "-" * 100
    ts = s.get("ts") or now_str()
    ret = (a["equity"] / a["equity0"] - 1.0) if a["equity0"] else 0.0
    _acct_name = getattr(pb, "name", "") or ""
    _tag = " · %s" % _acct_name if _acct_name else ""
    L = [sep,
         " 纸面交易账户（影子模拟 · 非实盘 · 不花真钱 · 不构成投资建议）%s   更新: %s" % (_tag, ts),
         " 成交档: %s（next=信号下一轮首个新价成交、严格晚于信号）；初始资金 %s 元" % (
             a["fill_mode"], _yuan(a["equity0"])),
         " 参与模式: %s | entry_score: %s | opt_premium_ratio: %.0f%%" % (
             getattr(pb, "priority", "-"),
             getattr(pb, "entry_score", 0),
             getattr(pb, "opt_premium_ratio", 0) * 100),
         sep, "",
         "【账户概览】",
         " 动态权益: %s 元（%+.2f%%）   静态权益: %s 元   浮动盈亏: %s 元" % (
             _yuan(a["equity"]), ret * 100.0, _yuan(a["static"]), _yuan(snap.get("float_pnl", 0.0))),
         " 已实现净盈亏: %s 元   累计手续费: %s 元" % (_yuan(a["realized"]), _yuan(a["fees_paid"])),
         " 保证金占用: %s 元   可用资金: %s 元   风险度(占用/动态权益): %s" % (
             _yuan(a["margin_used"]), _yuan(a["available"]), _pct(a["risk_degree"], 2)),
         " 当前持仓 %d 个   在途挂单 %d 个   累计平仓 %d 笔（其中风控强平 %d）   约束排队尝试 %d 次" % (
             a["n_positions"], a["n_pending"], a["n_closed"], a["n_liquidations"], a["n_skipped"]),
         ""]
    # 第104轮统一资金池：期权持仓/已实现作为统一账户内的明细展示
    opt = a.get("opt") or {}
    if opt.get("n_positions", 0) > 0:
        L += ["【期权持仓明细】（第104轮起与期货统一资金池）",
              " 期权持仓: %d 个   期权已实现: %s 元   手续费: %s 元" % (
                  opt.get("n_positions", 0),
                  _yuan(opt.get("realized", 0.0)), _yuan(opt.get("fees_paid", 0.0))),
              ""]
    if perf:
        L += ["【组合绩效】（按自然日聚合、日度口径年化，样本随影子运行持续积累）",
              " 累计收益率 %s   年化(简式) %s   夏普 %.2f   索提诺 %.2f   最大回撤 %s" % (
                  _pct(perf["total_ret"]), _pct(perf["ann_ret"]), perf["sharpe"],
                  perf["sortino"], _pct(perf["max_dd"])),
              " 胜率 %s（%d笔）   平均盈 %s元 / 平均亏 %s元   盈亏比 %s   覆盖自然日 %d 天   峰值风险度 %s" % (
                  _pct(perf["win_rate"], 1), perf["n_trades"], _yuan(perf["avg_win"]),
                  _yuan(perf["avg_loss"]),
                  ("%.2f" % perf["pl_ratio"]) if perf["pl_ratio"] is not None else "-",
                  perf["days"], _pct(perf["max_risk"], 1)),
              " 风险调整(G3)：Calmar %s  Omega %s  Ulcer %s  VaR95(日) %s  CVaR95(日) %s  盈亏因子PF %s" % (
                  _num(perf.get("calmar")), _num(perf.get("omega")), _num(perf.get("ulcer")),
                  _pct(perf.get("var95")) if perf.get("var95") is not None else "-",
                  _pct(perf.get("cvar95")) if perf.get("cvar95") is not None else "-",
                  _num(perf.get("profit_factor"))),
              " 交易连续性：最大连胜 %d 笔 / 最大连亏 %d 笔%s" % (
                  perf.get("max_win_streak", 0) or 0, perf.get("max_loss_streak", 0) or 0,
                  _paper_excursion_tail(perf.get("mae_mfe")))]
        L += _paper_monthly_lines(perf)
        L.append("")
    st = a["status"]
    L += ["【委托状态统计】（在途排队≠确定拒单：临时资金/持仓上限/锁板缓解后，排队单仍可成交）",
          " 已成交 %d   在途排队 %d   锁板/无价阻塞(blocked) %d   确定拒单(rejected) %d   已撤销 %d" % (
              st["filled"], st["pending"], st["blocked"], st["rejected"], st["cancelled"]),
          ""]
    pos_rows = pb.positions_view()
    L.append("【当前持仓】%s" % ("（空仓）" if not pos_rows else ""))
    if pos_rows:
        L.append(" " + pad("品种", 9) + pad("合约", 12) + pad("名称", 10) + pad("方向", 4)
                 + pad("手数", 5) + pad("开仓时间", 16) + pad("开仓价", 11) + pad("最新价", 11)
                 + pad("浮动盈亏", 12) + pad("占用保证金", 13) + "开仓结算交易日")
        for p in pos_rows:
            L.append(" " + pad(p["sym"], 9) + pad(p.get("contract_code") or "—", 12)
                     + pad(p["name"], 10) + pad(p["dir"], 4)
                     + pad(str(p["lots"]), 5) + pad(p["entry_dt"][:16], 16)
                     + pad("%.2f" % p["entry_price"], 11) + pad("%.2f" % p["last"], 11)
                     + pad(format(p["float_yuan"], "+,.0f"), 12) + pad(_yuan(p["margin"]), 13)
                     + p["entry_owner"])
    L.append("")
    pend = pb.pending_view()
    L.append("【在途挂单】%s" % ("（无）" if not pend else ""))
    if pend:
        L.append(" " + pad("品种", 9) + pad("合约", 12) + pad("动作", 10) + pad("买卖", 5)
                 + pad("挂单时间", 20) + pad("信号价", 11) + pad("综合分", 7) + "排队原因")
        for o in pend:
            sig_price = "%.2f" % o["signal_price"] if o["signal_price"] else "-"
            score_txt = "%+.1f" % o["score"] if o["score"] is not None else "-"
            L.append(" " + pad(o["sym"], 9) + pad(o.get("contract_code") or "—", 12)
                     + pad(_PAPER_ACTION_CN.get(o["action"], o["action"]), 10)
                     + pad(_PAPER_SIDE_CN.get(o["side"], o["side"]), 5) + pad(o["ts"], 20)
                     + pad(sig_price, 11) + pad(score_txt, 7)
                     + (o["reason"] or "等待下一轮首个新价成交"))
    L.append("")
    recent = []
    if pb.db is not None:
        try:
            recent = pb.db.paper_trades_recent(20)
        except Exception:
            recent = []
    L.append("【最近成交（最多20笔；全量见 SQLite paper_trades 表）】%s"
             % ("（暂无成交）" if not recent else ""))
    if recent:
        L.append(" " + pad("时间", 20) + pad("品种", 9) + pad("合约", 12) + pad("方向", 4)
                 + pad("手数", 5) + pad("开平", 5) + pad("成交价", 11) + pad("手续费", 9)
                 + pad("净盈亏", 11) + pad("强平", 4) + "原因")
        for t in recent:
            L.append(" " + pad(str(t["ts"])[:19], 20) + pad(t["sym"], 9)
                     + pad(t.get("contract_code") or "—", 12)
                     + pad(t.get("dir_text", ""), 4) + pad(str(t["lots"]), 5)
                     + pad(t.get("leg", ""), 5) + pad("%.2f" % (t["price"] or 0), 11)
                     + pad("%.1f" % (t.get("fee_yuan") or 0), 9)
                     + pad(format(t.get("realized_yuan") or 0, "+,.0f"), 11)
                     + pad("是" if t.get("forced") else "", 4) + (t.get("reason") or ""))
    L += ["", thin,
          " 说明：影子账户严格按 analyzer 综合分三阈值迟滞自动虚拟撮合；成交价内含滑点，手续费取 data/futures_fees.csv"
          "（平今/平昨按交易所结算交易日实时判定，判不了保守按平昨），保证金取 data/futures_margins.csv。"
          "连续影子≥4周后与 signal_outcomes 对照，成本后为负必须诚实呈现并回退；先 paper，永远不自动接实盘（门槛见融合总纲 G20）。",
          sep]
    return "\n".join(L)


def _paper_tier_baselines_text(state):
    """第105轮：5 档基准账户摘要（每档一个基准风格账户），供 paper_account.txt 顶部展示。"""
    papers = getattr(state, "papers", {}) or {}
    if not papers:
        return ""
    tier_order = [100_000, 10_000, 5_000, 3_000, 1_000]
    tier_label = {100_000: "10万", 10_000: "1万", 5_000: "5000", 3_000: "3000", 1_000: "1000"}
    L = ["=" * 104,
         " 纸面·各金额档基准账户（第105轮起；每档展示基准风格账户，完整明细见各账户文件）  更新: %s"
         % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         "-" * 104]
    for eq0k in tier_order:
        grp = []
        for _name, _broker in papers.items():
            try:
                a = _broker.account_summary()
                if abs((a.get("equity0") or 0) - eq0k) >= 1.0:
                    continue
                style = _paper_style_of(_name, a.get("fill_mode", "next"),
                                        getattr(_broker, "entry_score", 0))
                grp.append((style, _name, _broker, a))
            except Exception:
                continue
        if not grp:
            continue
        grp.sort(key=lambda x: {"激进": 0, "基准": 1, "保守": 2, "赌徒": 3}.get(x[0], 9))
        L.append("◆ %s档（初始 %s 元）" % (tier_label.get(eq0k, str(eq0k)), format(eq0k, ",")))
        for style, _name, _broker, a in grp:
            eq = a.get("equity") or 0
            eq0 = a.get("equity0") or 0
            ret = (eq / eq0 - 1.0) if eq0 else 0.0
            opt = a.get("opt") or {}
            _tag = {"基准": "【基准】", "激进": "【激进】", "保守": "【保守】",
                    "赌徒": "【赌徒】"}.get(style, "【%s】" % style)
            # 第110轮：可交易性标注（该档能开1手的最便宜品种；纯期权档/不足标 None）
            _aff_sym, _aff_margin = _paper_affordable(_broker, eq0)
            _aff_txt = ""
            if _aff_sym:
                _aff_txt = " 可交易: %s(%.0f元/手)" % (_aff_sym, _aff_margin)
            elif getattr(_broker, "priority", "") == "option_only" or \
                    getattr(_broker, "futures_max", None) == 0:
                _aff_txt = " 纯期权(期货不可用)"
            L.append("  %-12s %s 权益%s(%+.2f%%) 风险%.1f%% 持仓%d(权%d) 已实现%s 手续费%s%s"
                     % (_name, _tag, _yuan(eq), ret * 100.0, (a.get("risk_degree") or 0) * 100.0,
                        a.get("n_positions", 0), opt.get("n_positions", 0),
                        _yuan(a.get("realized", 0.0)), _yuan(a.get("fees_paid", 0.0)), _aff_txt))
    L.append("-" * 104)
    L.append("")
    return "\n".join(L)


def _paper_tick_note():
    """第103/104轮：口径变更标注行（ticker 撮合粒度 + 统一资金池，按开关显示）。"""
    notes = []
    if getattr(config, "PAPER_TICK_INTERVAL", 0) > 0:
        note = getattr(config, "PAPER_TICK_ANNOTATION", "")
        if note:
            notes.append(note)
    if getattr(config, "PAPER_UNIFIED_POOL", True):
        note = getattr(config, "PAPER_UNIFIED_POOL_ANNOTATION", "")
        if note:
            notes.append(note)
    if notes:
        return "# " + "；".join(notes) + "\n"
    return ""


def _paper_tier_of(equity0):
    """资金档标签（10万/1万/5000/3000/1000）。"""
    for _k, _label in ((100_000, "10万"), (10_000, "1万"), (5_000, "5000"),
                       (3_000, "3000"), (1_000, "1000")):
        if abs(float(equity0 or 0) - _k) < 1.0:
            return _label
    return "%.0f" % float(equity0 or 0)


def _paper_style_of(name, fill_mode, entry_score):
    """账户风格：激进/基准/保守/赌徒（第106轮新增）。"""
    n = str(name or "")
    if "赌徒" in n or "gamble" in n.lower():
        return "赌徒"
    if "激进" in n or fill_mode == "close":
        return "激进"
    if "保守" in n:
        return "保守"
    return "基准"


def _paper_affordable(broker, equity0):
    """第110轮：该账户在 target_basis 口径下"能开 1 手的最便宜品种"及其一手保证金（可交易性说明列）。
    规则：目标预算 = 动态权益×per_symbol；品种成本 = 最新价×乘数×保证金率+开仓费，取预算内成本最小的品种；
    option_only / futures_max=0 的账户无论资金多少都不开期货，标注"纯期权"；找不到可负担品种标 None。
    仅供看板展示，纸面层纯计算不碰行情；失败静默返回 (None, None)。"""
    try:
        pf = getattr(broker, "pf", None)
        if pf is None:
            return None, None
        if getattr(broker, "priority", None) == "option_only" or \
                getattr(broker, "futures_max", None) == 0:
            return None, None
        per_symbol = float(getattr(pf, "per_symbol", 0) or 0)
        if per_symbol <= 0:
            return None, None
        from portfolio import load_margin_schedule
        _margins = load_margin_schedule() or {}
        if not _margins:
            return None, None
        eq = float(pf.equity() or equity0 or 0)
        budget = max(0.0, eq * per_symbol)
        best_sym, best_cost = None, None
        for _sym, _mr in _margins.items():
            try:
                _mul = float(_mr.get("multiplier") or 0)
                _rate = float(_mr.get("broker_margin") or 0)
                _px = float(pf._last_prices.get(_sym, 0) or 0)
                if _px <= 0:
                    continue
                _cost = _px * _mul * _rate + pf.fee_yuan(_sym, _px, "open", 1)
                if _cost <= budget + 1e-9 and (best_cost is None or _cost < best_cost):
                    best_sym, best_cost = _sym, _cost
            except Exception:
                continue
        if not best_sym:
            return None, None
        _name = _margins.get(best_sym, {}).get("name", best_sym)
        return ("%s %s" % (_name, best_sym), round(best_cost, 1))
    except Exception:
        return None, None


def _paper_detail_snapshot(broker):
    """第105轮：提取单账户下钻明细（持仓/成交/在途挂单），供对比页 <details> 折叠查看。"""
    out = {"positions": [], "trades": [], "orders": []}
    try:
        for p in broker.positions_view():
            out["positions"].append({
                "sym": p.get("sym", ""), "contract": p.get("contract_code", ""),
                "name": p.get("name", ""), "dir": p.get("dir", ""),
                "lots": p.get("lots", 0), "entry_dt": p.get("entry_dt", ""),
                "entry": p.get("entry_price", 0.0), "last": p.get("last", 0.0),
                "float": p.get("float_yuan", 0.0), "margin": p.get("margin", 0.0),
                "score": p.get("score")})
    except Exception:
        pass
    try:
        db = getattr(broker, "db", None)
        if db is not None and hasattr(db, "paper_trades_recent"):
            for t in db.paper_trades_recent(50):
                out["trades"].append({
                    "ts": t.get("ts", ""), "sym": t.get("sym", ""),
                    "contract": t.get("contract_code", ""),
                    "dir": t.get("dir_text", ""), "lots": t.get("lots", 0),
                    "leg": t.get("leg", ""), "price": t.get("price", 0.0),
                    "fee": t.get("fee_yuan", 0.0), "realized": t.get("realized_yuan", 0.0),
                    "forced": t.get("forced", 0), "reason": t.get("reason", "")})
    except Exception:
        pass
    try:
        db = getattr(broker, "db", None)
        if db is not None and hasattr(db, "paper_orders_recent"):
            for o in db.paper_orders_recent(50):
                out["orders"].append({
                    "ts": o.get("ts", ""), "sym": o.get("sym", ""),
                    "action": o.get("action", ""), "side": o.get("side", ""),
                    "lots": o.get("lots", 0), "status": o.get("status", ""),
                    "signal_price": o.get("signal_price"), "score": o.get("score"),
                    "reason": o.get("reason", "")})
    except Exception:
        pass
    return out


def _paper_equity_series_json(broker, max_points=600):
    """第105轮：账户权益曲线（归一化到首个快照=1.0 + 原始权益/回撤/风险度），供详情页 ECharts。"""
    import json as _json
    db = getattr(broker, "db", None)
    rows = []
    if db is not None and hasattr(db, "paper_equity_series"):
        try:
            rows = db.paper_equity_series(max_points)
        except Exception:
            rows = []
    dts, eq, risk, dd = [], [], [], []
    for r in rows:
        v = _num_or_none(r.get("equity"))
        if v is None:
            continue
        dts.append(str(r.get("ts") or "")[5:16])
        eq.append(v)
        risk.append(_num_or_none(r.get("risk_degree")) or 0.0)
        dd.append(max(0.0, _num_or_none(r.get("drawdown")) or 0.0))
    base = eq[0] if eq else 1.0
    norm = [v / base if base else 1.0 for v in eq]
    return _json.dumps({"dt": dts, "eq": eq, "norm": norm, "risk": risk, "dd": dd},
                       ensure_ascii=False)


def _num_or_none(v):
    """int/float 且非 NaN → float，否则 None。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and -1e308 < f < 1e308 else None


def _paper_detail_html(state, broker, name, a=None):
    """第105轮：生成单账户二级详情页（静态单文件 HTML，复用 assets/echarts.min.js）。

    内容：账户信息条 + 归一化净值/回撤/风险度三图 + 持仓/成交/挂单三表。
    数据全部来自 broker 现成 API；ECharts 缺失时回退表格（与看板同策略）。
    """
    import json as _json
    a = a if a is not None else broker.account_summary()
    eq0 = a.get("equity0") or 0
    eq = a.get("equity") or 0
    ret = (eq / eq0 - 1.0) if eq0 else 0.0
    opt = a.get("opt") or {}
    perf = a.get("performance") or {}
    series = _paper_equity_series_json(broker)
    detail = _paper_detail_snapshot(broker)

    def _esc(s):
        return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    rows_pos = ""
    for p in detail["positions"]:
        rows_pos += ("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%d</td>"
                     "<td>%s</td><td>%s</td><td>%s</td><td class='num'>%s</td><td class='num'>%s</td></tr>"
                     % (_esc(p["sym"]), _esc(p["contract"]), _esc(p["name"]), _esc(p["dir"]),
                        int(p["lots"] or 0), _esc(p["entry_dt"]), _fmt(p["entry"], 2),
                        _fmt(p["last"], 3), _fmt(p["float"], 0), _fmt(p["margin"], 0)))
    rows_tr = ""
    for t in detail["trades"]:
        rows_tr += ("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%d</td><td>%s</td>"
                    "<td class='num'>%s</td><td class='num'>%s</td><td class='num'>%s</td><td>%s</td>%s</tr>"
                    % (_esc(t["ts"])[:19], _esc(t["sym"]), _esc(t["contract"]), _esc(t["dir"]),
                       int(t["lots"] or 0), _esc(t["leg"]), _fmt(t["price"], 2),
                       _fmt(t["fee"], 2), _fmt(t["realized"], 0),
                       "是" if t.get("forced") else "", _esc(t["reason"] or "")))
    rows_od = ""
    for o in detail["orders"]:
        rows_od += ("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%d</td><td>%s</td>"
                    "<td class='num'>%s</td><td class='num'>%s</td><td>%s</td></tr>"
                    % (_esc(o["ts"])[:19], _esc(o["sym"]), _esc(o["action"]), _esc(o["side"]),
                       int(o["lots"] or 0), _esc(o["status"]),
                       _fmt(o.get("signal_price"), 2), _fmt(o.get("score"), 2), _esc(o["reason"] or "")))

    _now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>纸面详情 · %s</title>
<style>
 body{background:#17181c;color:#e8e8e8;font-family:Consolas,Monaco,"Microsoft YaHei",monospace;margin:0;padding:18px;}
 h1{font-size:20px;margin:0 0 6px;color:#fff;}
 .sub{color:#9a9a9a;font-size:12px;margin-bottom:14px;}
 .stats{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:16px;}
 .card{background:#22242a;border:1px solid #333;border-radius:8px;padding:10px 14px;min-width:120px;}
 .card .lbl{color:#9a9a9a;font-size:11px;}
 .card .val{font-size:18px;font-weight:bold;margin-top:2px;}
 .pos{color:#ef6b6b;}.neg{color:#43c589;}
 h2{font-size:15px;margin:22px 0 8px;color:#7ecbff;border-bottom:1px solid #333;padding-bottom:4px;}
 #charts{width:100%%;height:280px;}
 #charts-dd,#charts-risk{width:100%%;height:180px;}
 table{border-collapse:collapse;width:100%%;font-size:12px;}
 th{background:#2a2a2a;padding:5px 8px;text-align:left;border-bottom:2px solid #444;white-space:nowrap;}
 td{padding:4px 8px;border-bottom:1px solid #2c2c2c;white-space:nowrap;}
 .num{text-align:right;}
 .empty{color:#777;padding:10px;}
</style></head><body>
<h1>纸面详情 · %s</h1>
<div class="sub">数据源=账户独立 SQLite（data/paper_accounts/paper_%s.db）· 生成时间 %s · 撮合 %s · entry_score %s · 参与 %s</div>
<div class="stats">
  <div class="card"><div class="lbl">初始资金</div><div class="val">%s</div></div>
  <div class="card"><div class="lbl">动态权益</div><div class="val">%s</div></div>
  <div class="card"><div class="lbl">累计收益率</div><div class="val %s">%+.2f%%</div></div>
  <div class="card"><div class="lbl">最大回撤</div><div class="val %s">%s</div></div>
  <div class="card"><div class="lbl">风险度</div><div class="val">%s</div></div>
  <div class="card"><div class="lbl">期货持仓/期权持仓</div><div class="val">%d / %d</div></div>
  <div class="card"><div class="lbl">已实现/手续费</div><div class="val">%s</div></div>
</div>
<h2>归一化净值（首个快照=1.0）</h2>
<div id="charts"></div>
<h2>回撤（水下）与风险度</h2>
<div id="charts-dd"></div>
<div id="charts-risk"></div>
<h2>当前持仓（%d）</h2>
<table><tr><th>品种</th><th>合约</th><th>名称</th><th>方向</th><th>手数</th><th>开仓时间</th>
<th>开仓价</th><th>最新价</th><th>浮动盈亏</th><th>占用保证金</th></tr>%s</table>
<h2>最近成交（最多50笔）</h2>
<table><tr><th>时间</th><th>品种</th><th>合约</th><th>方向</th><th>手数</th><th>开平</th>
<th>成交价</th><th>手续费</th><th>净盈亏</th><th>强平</th><th>原因</th></tr>%s</table>
<h2>在途挂单（最近50条）</h2>
<table><tr><th>时间</th><th>品种</th><th>动作</th><th>买卖</th><th>手数</th><th>状态</th>
<th>信号价</th><th>综合分</th><th>原因</th></tr>%s</table>
<script src="assets/echarts.min.js"></script>
<script>
var S = %s;
function wire(id, opt){var el=document.getElementById(id);if(!el||!window.echarts||!echarts.init){if(el)el.innerHTML='<span class="empty">ECharts 缺失，显示表格数据</span>';return;}
var c=echarts.init(el);c.setOption(opt);}
if(S.dt.length){
 wire('charts',{backgroundColor:'transparent',tooltip:{trigger:'axis'},legend:{textStyle:{color:'#ccc'},top:0},
  grid:{left:60,right:16,top:26,bottom:30},
  xAxis:{type:'category',data:S.dt,axisLabel:{color:'#999'}},yAxis:{type:'value',scale:true,axisLabel:{color:'#999'}},
  series:[{name:'归一化净值',type:'line',data:S.norm,smooth:true,connectNulls:true,
    lineStyle:{width:2,color:'#7ecbff'},itemStyle:{color:'#7ecbff'},areaStyle:{opacity:0.15}}]});
 wire('charts-risk',{backgroundColor:'transparent',tooltip:{trigger:'axis'},
  grid:{left:60,right:16,top:10,bottom:26},
  xAxis:{type:'category',data:S.dt,axisLabel:{show:false}},yAxis:{type:'value',max:1,axisLabel:{color:'#999'}},
  series:[{name:'风险度',type:'line',data:S.risk,step:'end',lineStyle:{width:1.5,color:'#f9ca24'}}]});
 wire('charts-dd',{backgroundColor:'transparent',tooltip:{trigger:'axis'},
  grid:{left:60,right:16,top:10,bottom:26},
  xAxis:{type:'category',data:S.dt,axisLabel:{show:false}},yAxis:{type:'value',max:0,axisLabel:{color:'#999'}},
  series:[{name:'回撤',type:'line',data:S.dd,smooth:true,lineStyle:{width:1.5,color:'#ef6b6b'}}]});
}
</script>
</body></html>""" % (
        _esc(name), _esc(name), _esc(name.replace(" ", "_").replace("/", "_")),
        _now,
        _esc(a.get("fill_mode", "next")), _esc(getattr(broker, "entry_score", 0)),
        _esc(getattr(broker, "priority", "futures_first")),
        _fmt(eq0, 0), _fmt(eq, 2), "pos" if ret >= 0 else "neg", ret * 100.0,
        "pos" if (perf.get("max_dd") or 0) <= 0 else "neg",
        ("%.2f%%" % (perf["max_dd"] * 100)) if perf.get("max_dd") is not None else "--",
        ("%.1f%%" % ((a.get("risk_degree") or 0.0) * 100)),
        int(a.get("n_positions", 0)), int(opt.get("n_positions", 0)),
        "%s / %s" % (_fmt(a.get("realized", 0.0), 0), _fmt(a.get("fees_paid", 0.0), 0)),
        len(detail["positions"]), rows_pos or '<tr><td class="empty">（空仓）</td></tr>',
        rows_tr or '<tr><td class="empty">（暂无成交）</td></tr>',
        rows_od or '<tr><td class="empty">（无在途挂单）</td></tr>',
        series)
    return html


def _fmt(v, nd=2):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if f != f:
        return "—"
    return ("{:,.%df}" % nd).format(f)


def write_paper_account(state):
    """每轮刷新 reports/paper_account.txt（基准）+ 各账户独立文件（文件被占用只跳过，绝不影响主报告链路）。
    第102轮：多账户扩展，遍历 state.papers 写各自独立文件，基准始终写 paper_account.txt；
    同时写出 paper_compare.json 供看板「纸面账户对比」页签渲染。"""
    db_dir = getattr(config, "PAPER_ACCOUNT_DB_DIR",
                     os.path.join(config.BASE_DIR, "data", "paper_accounts"))
    try:
        os.makedirs(db_dir, exist_ok=True)
    except Exception:
        pass
    _note = _paper_tick_note()
    # 基准文件 paper_account.txt：第105轮起顶部为 5 档基准摘要，下方保留首账户全文（向后兼容）
    try:
        _tier_txt = _paper_tier_baselines_text(state)
        text = paper_account_text(state)
        if text:
            _safe_write(config.PAPER_ACCOUNT_TXT,
                        (_note + _tier_txt + "\n" if _tier_txt else _note) + text,
                        encoding="utf-8-sig", update_cache=False)
    except Exception as e:
        LOG.debug("纸面账户报告写入失败: %s", e)
    # 多账户：每个 broker 写 paper_account_{name}.txt + paper_detail_{name}.html + 聚合对比 json
    cmp_rows = []
    for name, broker in (getattr(state, "papers", {}) or {}).items():
        try:
            text = paper_account_text(state, broker=broker)
            if text:
                db_name = name.replace(" ", "_").replace("/", "_")
                fp = os.path.join(config.BASE_DIR, "reports", f"paper_account_{db_name}.txt")
                _safe_write(fp, _note + text, encoding="utf-8-sig", update_cache=False)
            a = broker.account_summary()
            eq0 = a.get("equity0") or 0
            eq = a.get("equity") or 0
            opt = a.get("opt") or {}
            perf = a.get("performance") or {}
            # 第105轮：二级详情页（静态单文件，供对比页"点账户名"下钻）
            if getattr(config, "PAPER_DETAIL_ENABLED", True):
                try:
                    _html = _paper_detail_html(state, broker, name, a)
                    _dfp = os.path.join(config.BASE_DIR, "reports",
                                        f"paper_detail_{name.replace(' ', '_').replace('/', '_')}.html")
                    _safe_write(_dfp, _html, encoding="utf-8", update_cache=False)
                except Exception as _e:
                    LOG.debug("纸面详情页 %s 生成失败: %s", name, _e)
            # 第105轮：下钻明细（持仓/成交/在途挂单），供对比页 <details> 折叠查看
            _detail = _paper_detail_snapshot(broker)
            # 第110轮：可交易性说明（该档能开1手的最便宜品种及一手保证金）
            _aff_sym, _aff_margin = _paper_affordable(broker, eq0)
            # 第111轮：成交汇总（名义/滑点/开平次数，供对比页折叠区展示；缺失降级 None）
            _fills = None
            try:
                _fills = broker.fill_report()
            except Exception:
                pass
            cmp_rows.append({
                "name": name,
                "tier": _paper_tier_of(eq0),
                "style": _paper_style_of(name, a.get("fill_mode", "next"),
                                         getattr(broker, "entry_score", 0)),
                "equity0": eq0, "equity": eq,
                "ret": (eq / eq0 - 1.0) if eq0 else 0.0,
                "max_drawdown": perf.get("max_dd") if perf.get("max_dd") is not None else 0.0,
                "n_fut_pos": a.get("n_positions", 0),
                "n_opt_pos": opt.get("n_positions", 0),
                "n_closed": a.get("n_closed", 0),
                "n_pending": a.get("n_pending", 0),
                "realized": a.get("realized", 0.0),
                "fees": a.get("fees_paid", 0.0),
                "fill_mode": a.get("fill_mode", "next"),
                "priority": getattr(broker, "priority", "futures_first"),
                "entry_score": getattr(broker, "entry_score", 0),
                "risk_degree": a.get("risk_degree", 0.0),
                "detail": _detail,
                "affordable_sym": _aff_sym,
                "affordable_margin": _aff_margin,
                # 第111轮：资金四维
                "static": a.get("static"),
                "float_pnl": a.get("float_pnl"),
                "margin_used": a.get("margin_used"),
                "available": a.get("available"),
                # 第111轮：绩效指标（performance 子集，缺失降级 None）
                "ann_ret": perf.get("ann_ret"),
                "sharpe": perf.get("sharpe"),
                "sortino": perf.get("sortino"),
                "win_rate": perf.get("win_rate"),
                "profit_factor": perf.get("profit_factor"),
                "n_trades": perf.get("n_trades"),
                "total_pnl": perf.get("total_pnl"),
                "avg_win": perf.get("avg_win"),
                "avg_loss": perf.get("avg_loss"),
                "pl_ratio": perf.get("pl_ratio"),
                "avg_risk": perf.get("avg_risk"),
                # 第111轮：执纪/风控与委托状态计数
                "n_liquidations": a.get("n_liquidations", 0),
                "n_skipped": a.get("n_skipped", 0),
                "status": a.get("status") or {},
                "opt_realized": opt.get("realized"),
                "opt_fees": opt.get("fees_paid"),
                "opt_equity": a.get("opt_equity"),
                # 第111轮：成交汇总
                "notional": (_fills or {}).get("notional"),
                "slip_yuan": (_fills or {}).get("slip_yuan"),
                "n_opens": (_fills or {}).get("n_open"),
                "n_closes": (_fills or {}).get("n_close"),
            })
        except Exception as e:
            LOG.debug("纸面账户 %s 报告写入失败: %s", name, e)
    if cmp_rows:
        try:
            import json as _json
            _safe_write(os.path.join(config.BASE_DIR, "reports", "paper_compare.json"),
                        _json.dumps(cmp_rows, ensure_ascii=False, indent=1),
                        encoding="utf-8", update_cache=False)
        except Exception:
            pass
    # 第123轮：同步生成独立对比页（完整HTML骨架，供看板"纸面账户对比"页签
    # iframe 加载，60s 周期与 ticker 同步刷新——不依赖 write_dashboard 10分钟周期）。
    # _paper_compare_html 内部已自带完整 <style>（渲染时读最新 paper_compare.json），
    # 此处只包一层独立页面骨架。
    try:
        cp_dom = _paper_compare_html()
        _safe_write(os.path.join(config.BASE_DIR, "reports", "paper_compare.html"),
                    "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                    "<title>纸面账户对比</title></head>"
                    "<body style=\"background:#17181c;color:#e8e8e8;margin:0;padding:14px;"
                    "font-family:'Microsoft YaHei',Consolas,sans-serif;font-size:13px;line-height:1.6;\">"
                    "%s</body></html>" % cp_dom,
                    encoding="utf-8", update_cache=False)
    except Exception as e:
        LOG.debug("纸面对比页独立生成失败(不影响主链路): %s", e)


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

    # ---------- G1（二）纸面账户影子块（PAPER_ENABLED 开启才有；休眠时零输出、等价旧版） ----------
    L.extend(paper_block(state))

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
