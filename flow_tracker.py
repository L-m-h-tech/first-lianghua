# -*- coding: utf-8 -*-
"""P1-11 量仓资金因子。

新浪商品期货行情本身已经提供成交量与持仓量，早期只解析、未参与分析。
本模块用相邻轮次快照识别：
- 增仓上行：价格上涨且持仓增加，新多入场，确认多头；
- 增仓下行：价格下跌且持仓增加，新空入场，确认空头；
- 减仓上行/下行：更多是旧仓回补推动，趋势确认度减半；
- 放量/缩量：本轮新增成交量相对最近几轮平均增量的比值，用于增强或衰减信号。

只使用标准库，输出给 analyzer.analyze_variety 的 flow 字典。
"""
import math
import threading
import time
from collections import defaultdict, deque

import config
from utils import clip


class FlowTracker:
    """按主力连续代码记录最近若干轮 price/volume/open_interest 快照。"""

    def __init__(self, history_len=None):
        self.history_len = history_len or config.FLOW_HISTORY_LEN
        self.hist = defaultdict(lambda: deque(maxlen=self.history_len))
        self.lock = threading.Lock()

    def update(self, quotes, now_ts=None):
        """输入 fetch_quotes 的结果，返回 {code: flow_dict}。"""
        now_ts = now_ts or time.time()
        result = {}
        with self.lock:
            for code, q in quotes.items():
                price = float(q.get("latest") or 0.0)
                volume = float(q.get("volume") or 0.0)
                oi = float(q.get("open_interest") or 0.0)
                trade_date = str(q.get("date") or "")
                if price <= 0:
                    continue
                series = self.hist[code]
                # 交易日切换（含主力连续换月导致的量仓跳变）时重新建立基线，避免把隔夜/换月误判成放量或增仓。
                if trade_date and series and series[-1][4] and series[-1][4] != trade_date:
                    series.clear()
                series.append((now_ts, price, volume, oi, trade_date))
                flow = self._evaluate(code, series)
                if flow:
                    result[code] = flow
            return result

    @staticmethod
    def _evaluate(code, series):
        if len(series) < 2:
            return None
        now_ts, price, volume, oi, _trade_date = series[-1]
        prev_ts, prev_price, prev_volume, prev_oi, _prev_date = series[-2]
        if prev_price <= 0:
            return None

        price_ret = price / prev_price - 1.0
        if abs(price_ret) < 1e-8:
            direction = 0
        else:
            direction = 1 if price_ret > 0 else -1

        oi_chg = oi - prev_oi
        oi_pct = oi_chg / prev_oi if prev_oi > 0 else 0.0

        # 成交量是日累计值；先转成相邻轮次增量，再与此前几轮平均增量比较。
        # 新交易日日累计量会从小重新累计，不能把跨日重置误判成缩量。
        volume_reset = volume + 1e-9 < prev_volume
        vol_inc = 0.0 if volume_reset else max(0.0, volume - prev_volume)
        prior_incs = []
        for i in range(max(1, len(series) - 6), len(series) - 1):
            dt = series[i][0] - series[i - 1][0]
            dv = series[i][2] - series[i - 1][2]
            if dt > 0 and dv >= 0:
                # 粗略归一到相同时间长度，避免不同轮动间隔导致比值失真。
                prior_incs.append(dv / dt)
        avg_inc_per_sec = sum(prior_incs) / len(prior_incs) if prior_incs else 0.0
        dt = max(now_ts - prev_ts, 1e-6)
        expected_inc = avg_inc_per_sec * dt
        if volume_reset:
            volume_ratio = 1.0
        elif expected_inc > 0:
            volume_ratio = vol_inc / expected_inc
        else:
            volume_ratio = 1.0
        volume_ratio = clip(volume_ratio, 0.0, 8.0)

        score = 0.0
        pattern = "量仓平稳"
        if direction != 0:
            # 增仓代表新资金进场，权重高；减仓代表老仓位回补，趋势延续性弱。
            oi_part = math.tanh(abs(oi_pct) * config.FLOW_OI_K)
            oi_part *= 0.70 if oi_chg >= 0 else 0.35
            vol_part = 0.50 * math.tanh(max(volume_ratio - 1.0, 0.0) * 1.5)
            score = direction * (oi_part + vol_part)
            if volume_ratio < config.FLOW_VOLUME_WEAK:
                score *= 0.60
            if direction > 0:
                pattern = "增仓上行" if oi_chg >= 0 else "减仓上行(空头回补)"
            else:
                pattern = "增仓下行" if oi_chg >= 0 else "减仓下行(多头减仓)"
            if volume_ratio >= config.FLOW_VOLUME_STRONG:
                pattern += "·放量"
            elif volume_ratio < config.FLOW_VOLUME_WEAK:
                pattern += "·缩量"

        return {
            "code": code,
            "price": price,
            "prev_price": prev_price,
            "price_ret": price_ret,
            "volume": volume,
            "prev_volume": prev_volume,
            "volume_inc": vol_inc,
            "volume_reset": volume_reset,
            "volume_ratio": volume_ratio,
            "open_interest": oi,
            "prev_open_interest": prev_oi,
            "oi_chg": oi_chg,
            "oi_pct": oi_pct,
            "direction": direction,
            "pattern": pattern,
            "score": clip(score, -config.FLOW_MAX_SCORE, config.FLOW_MAX_SCORE),
        }
