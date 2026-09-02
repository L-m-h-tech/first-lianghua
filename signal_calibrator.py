# -*- coding: utf-8 -*-
"""WP-F2（P1-2）A3：历史信号胜率校准器（meta-labeling 零依赖版）。

思路（López de Prado meta-labeling 的轻量落地）：
  一条信号"该不该做、做多大"，除了看当前综合分，还可以看历史上【同类信号】的实际胜率。
  这里不训练任何模型，只用自有 storage 里 signal_outcomes 的已评估结果，按
  「方向 × 综合分档 × 主导因子」分组做贝叶斯平滑胜率，再线性映射为 sizing 置信乘子。

关键设计（与 WP-F1 同一套纪律）：
  1. 纯标准库、零网络、不新增第三方依赖；统计只来自本地 DB，任何异常都安全降级为乘子1.0；
  2. **默认影子模式**：实时侧只在报告展示"历史同类信号胜率 x%（n 笔）"，不改综合分/信号/建议；
     组合回测 portfolio.py 需显式 --calibrate 才把乘子真正乘到手数上，可对照、可一键回退；
  3. 小样本不校准：任一分组 n < CALIBRATOR_MIN_N 就向上回退分组层级
     （方向×分档×主导因子 → 方向×分档 → 方向 → 全局），全部不足返回乘子1.0；
  4. 贝叶斯平滑：胜率 = (胜数 + 先验伪胜数) / (n + 先验强度)，避免 n 很小时 1/1=100% 的虚高；
  5. 乘子严格裁剪到 [CALIBRATOR_MULT_LO, CALIBRATOR_MULT_HI]，胜率0.5对应乘子1.0。

可合成断言的纯函数：canonical_factor / dominant_factor / bayes_winrate / mult_from_winrate。
"""
import config

# 分组层级名（报告展示用）
LV_FACTOR = "方向×分档×主导因子"
LV_BAND = "方向×分档"
LV_DIR = "方向"
LV_ALL = "全局"
DIR_TEXT = {1: "做多", -1: "做空"}
FACTOR_FALLBACK = "综合"
NEUTRAL_NOTE = "calibrated-neutral"


def canonical_factor(key):
    """把 parts_json 的因子键归一为短名，如 '原油联动(w=0.30)' -> '原油联动'。"""
    if not key:
        return FACTOR_FALLBACK
    s = str(key).strip()
    for cut in ("(", "（"):
        if cut in s:
            s = s.split(cut, 1)[0].strip()
    return s or FACTOR_FALLBACK


def dominant_factor(parts, direction_int):
    """返回沿信号方向贡献最大的因子短名（主导因子）；无有效贡献时返回"综合"。

    parts 为 analyzer 的因子拆分数值 dict（带方向：正=利多、负=利空）。沿信号方向 d 的
    有效贡献为 d*v（做多时 v 越大越支持、做空时 v 越负越支持），取最大者；全部为负贡献
    （没有一个因子真正支持该方向）时归为"综合"，不硬安一个主导因子。
    """
    if not isinstance(parts, dict) or direction_int not in (1, -1):
        return FACTOR_FALLBACK
    best, best_val = FACTOR_FALLBACK, 0.0
    for k, v in parts.items():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        aligned = direction_int * fv
        if aligned > best_val:
            best, best_val = canonical_factor(k), aligned
    return best


def bayes_winrate(hits, n, prior_n=None):
    """Beta 先验平滑胜率：(hits + prior_n*0.5) / (n + prior_n)。

    prior_n 为先验伪样本数（先验胜率0.5）；n=0 时返回先验0.5。输入非法返回0.5（中性）。
    """
    if prior_n is None:
        prior_n = config.CALIBRATOR_PRIOR_N
    try:
        hits, n, prior_n = float(hits), float(n), float(prior_n)
    except (TypeError, ValueError):
        return 0.5
    if n < 0 or hits < 0 or hits > n + 1e-9 or prior_n < 0:
        return 0.5
    a = prior_n * 0.5
    return (hits + a) / (n + prior_n) if (n + prior_n) > 1e-12 else 0.5


def mult_from_winrate(winrate, slope=None, lo=None, hi=None):
    """平滑胜率 -> sizing 置信乘子：clip(1+(wr-0.5)*slope, lo, hi)。胜率0.5对应1.0。"""
    slope = config.CALIBRATOR_MULT_SLOPE if slope is None else slope
    lo = config.CALIBRATOR_MULT_LO if lo is None else lo
    hi = config.CALIBRATOR_MULT_HI if hi is None else hi
    try:
        wr = float(winrate)
    except (TypeError, ValueError):
        return 1.0
    m = 1.0 + (wr - 0.5) * slope
    return max(lo, min(hi, m))


def _band_of_score(score):
    """与 analyzer/backtest 完全一致的综合分档（避免循环依赖，本地按 config 阈值实现）。"""
    s = abs(float(score))
    if s < config.SCORE_NEUTRAL:
        return "观望"
    if s < config.SCORE_LIGHT:
        return "轻仓"
    if s < config.SCORE_MID:
        return "分批"
    return "强信号"


def _empty_result():
    return {"calibrated": False, "mult": 1.0, "winrate": None, "raw_winrate": None,
            "n": 0, "hits": 0, "avg_ret": None, "level": "", "band": "",
            "factor": FACTOR_FALLBACK, "note": ""}


class SignalCalibrator:
    """从历史 signal_outcomes 构建分组胜率索引，并对当前信号给出置信乘子。

    用法：
        cal = SignalCalibrator(db)                 # 从 MonitorDB 加载（默认周期/参数取 config）
        info = cal.lookup(row["score"], parts=row.get("parts"))
        cal.annotate_row(row)                      # 直接给 analyzer row 挂 row["calib"]
    也可传入 rows=storage.calibration_pairs(...) 的结果做零DB合成测试。
    """

    def __init__(self, db=None, *, horizon=None, min_n=None, enabled=None, days=None, rows=None):
        self.horizon = int(horizon or config.CALIBRATOR_HORIZON)
        self.min_n = int(min_n if min_n is not None else config.CALIBRATOR_MIN_N)
        self.days = int(days or config.CALIBRATOR_STAT_DAYS)
        self.enabled = bool(config.CALIBRATOR_ENABLED if enabled is None else enabled)
        # 四级分组聚合：key 层级见 LV_*；agg = [n, hits, sum_ret]
        self.groups = {}
        self.loaded = False
        if rows is not None:
            self.load(rows)
        elif db is not None and self.enabled:
            self.from_db(db)

    # ---------- 构建索引 ----------
    def _bump(self, key, hit, ret):
        agg = self.groups.setdefault(key, [0, 0, 0.0])
        agg[0] += 1
        agg[1] += 1 if hit else 0
        if ret is not None:
            try:
                agg[2] += float(ret)
            except (TypeError, ValueError):
                pass

    def load(self, rows):
        """从配对样本行（storage.calibration_pairs 输出）重建四级分组统计。"""
        self.groups = {}
        for r in rows or []:
            try:
                d = int(r.get("direction_int") or 0)
                if d not in (1, -1):
                    continue
                band = r.get("score_band") or _band_of_score(r.get("score", 0.0))
                hit = 1 if int(r.get("hit") or 0) == 1 else 0
                ret = r.get("ret")
                fac = dominant_factor(_safe_json(r.get("parts_json")), d)
                self._bump(("ALL",), hit, ret)
                self._bump(("DIR", d), hit, ret)
                self._bump(("BAND", d, band), hit, ret)
                self._bump(("FAC", d, band, fac), hit, ret)
            except Exception:
                continue
        self.loaded = True
        return self

    def from_db(self, db):
        try:
            self.load(db.calibration_pairs(self.horizon, self.days))
        except Exception:
            self.groups = {}
            self.loaded = False
        return self

    # ---------- 查询 ----------
    def _agg_of(self, key):
        agg = self.groups.get(key)
        if not agg or agg[0] < self.min_n:
            return None
        n, hits, sum_ret = agg
        return {"n": n, "hits": hits,
                "avg_ret": sum_ret / n if n else None,
                "raw_winrate": hits / n if n else 0.0}

    def lookup(self, score, direction_int=None, parts=None):
        """对一条当前信号返回校准信息（含 mult 乘子）。逐级回退，样本不足返回未校准。"""
        res = _empty_result()
        if not self.enabled or not self.groups:
            return res
        try:
            s = float(score)
        except (TypeError, ValueError):
            return res
        if direction_int is None:
            direction_int = 1 if s > 0 else (-1 if s < 0 else 0)
        if direction_int not in (1, -1):
            return res
        band = _band_of_score(s)
        fac = dominant_factor(parts, direction_int)
        res["band"], res["factor"] = band, fac
        # 逐级回退：最细 → 最粗
        candidates = [
            (("FAC", direction_int, band, fac), LV_FACTOR),
            (("BAND", direction_int, band), LV_BAND),
            (("DIR", direction_int), LV_DIR),
            (("ALL",), LV_ALL),
        ]
        for key, level in candidates:
            agg = self._agg_of(key)
            if agg is None:
                continue
            wr = bayes_winrate(agg["hits"], agg["n"])
            res.update({"calibrated": True, "winrate": wr,
                        "raw_winrate": agg["raw_winrate"], "n": agg["n"],
                        "hits": agg["hits"], "avg_ret": agg["avg_ret"],
                        "level": level, "mult": mult_from_winrate(wr)})
            res["note"] = self.format_note(res, direction_int)
            return res
        return res  # 全部层级样本不足：mult=1.0、不标注

    def annotate_row(self, row):
        """给 analyzer.analyze_variety 的结果行挂 row['calib']（影子标注，异常软降级）。"""
        try:
            row["calib"] = self.lookup(row.get("score", 0.0), parts=row.get("parts"))
        except Exception:
            row["calib"] = _empty_result()
        return row

    def format_note(self, info, direction_int=None):
        """生成报告展示文案，如：历史同类(做多·分批·日线动量)胜率55.2%(n=34)→乘子1.11（影子）。"""
        if not info or not info.get("calibrated"):
            return ""
        d_txt = DIR_TEXT.get(direction_int, "")
        bits = [x for x in (d_txt, info.get("band"),
                            info.get("factor") if info.get("level") == LV_FACTOR else None) if x]
        scope = "·".join(bits) if bits else "全局"
        wr = info["winrate"] * 100
        return ("历史同类(%s)胜率%.1f%%(n=%d,平滑前%.1f%%)→sizing乘子%.2f（影子，不改变当前建议）"
                % (scope, wr, info["n"], info["raw_winrate"] * 100, info["mult"]))

    # ---------- 报告用汇总 ----------
    def band_table(self):
        """返回方向×分档层(LV_BAND)的汇总行列表，供信号追踪看板展示（只列样本充足组）。"""
        out = []
        for d in (1, -1):
            for band in ("强信号", "分批", "轻仓", "观望"):
                agg = self.groups.get(("BAND", d, band))
                if not agg:
                    continue
                n, hits, sum_ret = agg
                wr = bayes_winrate(hits, n)
                out.append({"dir": d, "dir_text": DIR_TEXT.get(d, ""), "band": band,
                            "n": n, "hits": hits, "winrate": wr,
                            "avg_ret": sum_ret / n if n else 0.0,
                            "mult": mult_from_winrate(wr) if n >= self.min_n else None,
                            "enough": n >= self.min_n})
        return out


def _safe_json(text):
    """parts_json 文本 -> dict；已是 dict 原样返回；失败返回 {}。"""
    if isinstance(text, dict):
        return text
    if not text:
        return {}
    import json
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}
