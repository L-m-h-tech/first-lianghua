# -*- coding: utf-8 -*-
r"""G16（第88轮）浅ML 生产推理适配层 ml_inference——标准库前向推理 + 回退登记。

按总纲 G16：
- 生产用**标准库前向推理**（sigmoid(Σw·x+b)），不引 sklearn/lightgbm 进生产；
- 缺模型/版本不符/特征不全 → 回退线性 parts 打分并登记 fallback（_fallback_log）；
- **默认关**（config.ML_ENABLED=False，main 不调用）；即便开启，产出只作 ML 融合小权重因子，
  绝不做价格预测、绝不下单；
- 只对给定特征预测 triple-barrier 正概率（meta-label），不做端到端决策。

用法：
  D:\Python\python.exe ml_inference.py                    # 读 reports/ml_model.json 自检前向
  D:\Python\python.exe ml_inference.py --selftest         # 零文件合成断言
"""
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL_JSON = ROOT / "reports" / "ml_model.json"
FALLBACK_LOG = ROOT / "reports" / "ml_fallback.jsonl"

_FEATURES = ("mom5", "mom20", "ma10_bias", "ma20_bias", "ma60_bias",
             "rsv20", "atr_pct", "vol60", "tech_score", "ret1")


def _isnum(x):
    return isinstance(x, (int, float)) and x == x and math.isfinite(float(x))


def _sigmoid(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def load_model(path=None):
    """读模型 JSON；缺/坏返回 None。"""
    path = str(path or MODEL_JSON)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _fallback_reason(feats):
    return "missing_features:%s" % sorted(set(_FEATURES) - set(feats))


def _log_fallback(reason, feats, verdict=None):
    """回退登记（追加 jsonl）。绝不抛出。"""
    try:
        os.makedirs(os.path.dirname(str(FALLBACK_LOG)), exist_ok=True)
        with open(FALLBACK_LOG, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"reason": reason, "missing": sorted(
                set(_FEATURES) - set(feats)), "verdict": verdict,
                "ts": _now()}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _now():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def infer(feats, model=None, log_fallback=True, min_span_days=250, path=None):
    """标准库前向推理：feats={feature: value} -> {"prob": 止盈概率, "meta": 标签建议}。

    缺模型/特征不全/训练跨度<min_span_days（禁止上线）→ 回退并登记，返回 {
    "fallback": reason, "prob": None, "meta": 0}。默认关（调用方决定是否启用）。
    path：模型 JSON 路径（供测试隔离/显式指定，None=默认 reports/ml_model.json）。"""
    model = model if model is not None else load_model(path)
    if not model:
        reason = "no_model"
        if log_fallback:
            _log_fallback(reason, feats)
        return {"fallback": reason, "prob": None, "meta": 0}
    missing = [k for k in _FEATURES if not _isnum(feats.get(k))]
    if missing:
        reason = "missing_features:%s" % ",".join(missing)[:60]
        if log_fallback:
            _log_fallback(reason, feats)
        return {"fallback": reason, "prob": None, "meta": 0}
    # 跨度硬门槛：模型训练到日期区间长度（交易日数）< min_span_days → 禁止上线
    dmin, dmax = (model.get("date_range") or [None, None])
    if dmin and dmax:
        from datetime import datetime as _dt
        try:
            days = (_dt.strptime(dmax, "%Y-%m-%d") - _dt.strptime(dmin, "%Y-%m-%d")).days
            if days < min_span_days:
                if log_fallback:
                    _log_fallback("span_too_short:%dd" % days, feats)
                return {"fallback": "span_too_short", "prob": None, "meta": 0}
        except ValueError:
            pass
    mean = model.get("scaler_mean") or []
    std = model.get("scaler_std") or [1.0] * len(_FEATURES)
    w = model.get("weights") or []
    inter = model.get("intercept") or 0.0
    if len(w) != len(_FEATURES) or len(mean) != len(_FEATURES):
        if log_fallback:
            _log_fallback("version_mismatch", feats)
        return {"fallback": "version_mismatch", "prob": None, "meta": 0}
    z = 0.0
    for i, k in enumerate(_FEATURES):
        s = std[i] if _isnum(std[i]) and std[i] > 1e-12 else 1.0
        z += w[i] * ((feats[k] - (mean[i] or 0.0)) / s)
    z += inter
    prob = _sigmoid(z)
    meta = 1 if prob >= 0.5 else 0      # meta-label：1=预测止盈(+1)
    return {"fallback": None, "prob": round(prob, 4), "meta": meta, "model_version": model.get("version")}


# ---------------- 独立自检入口（--selftest） ----------------
def selftest():
    # 1) 缺模型回退登记
    r = infer({}, model=None, log_fallback=False, path="/nonexistent/ml_model_debug.json")
    assert r["fallback"] == "no_model" and r["meta"] == 0
    # 2) 特征不全回退
    model = {"features": list(_FEATURES), "weights": [0.1] * 10, "intercept": 0.0,
             "scaler_mean": [0.0] * 10, "scaler_std": [1.0] * 10, "version": 1,
             "date_range": ["2026-03-10", "2026-09-01"]}
    r = infer({"mom5": 1.0}, model=model, log_fallback=False)
    assert r["fallback"] and "missing_features" in r["fallback"]
    # 3) 跨度不足回退（127交易日 < 250）
    r = infer({k: 0.0 for k in _FEATURES}, model=model, log_fallback=False)
    assert r["fallback"] == "span_too_short"
    # 4) 跨度足够时前向与手算一致：z=w·std(x)+b → sigmoid
    model_ok = dict(model, date_range=["2025-01-01", "2026-09-01"])
    feats = {k: 1.0 for k in _FEATURES}
    r = infer(feats, model=model_ok, log_fallback=False)
    expect = _sigmoid(sum(0.1 * ((1.0 - 0.0) / 1.0) for _ in range(10)) + 0.0)
    assert r["prob"] and abs(r["prob"] - round(expect, 4)) < 1e-9
    assert r["meta"] == (1 if expect >= 0.5 else 0)
    # 5) 版本不符回退
    bad = dict(model_ok, weights=[0.1] * 5)
    r = infer({k: 1.0 for k in _FEATURES}, model=bad, log_fallback=False)
    assert r["fallback"] == "version_mismatch"
    print("ml_inference selftest ALL PASS（缺模型/特征不全/跨度不足/前向手算/版本不符 共5组）")
    return 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="G16 浅ML 推理适配层（标准库，默认关）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    # 自检前向：读当前模型 JSON（若存在）打印推理结论
    model = load_model()
    if not model:
        print("无模型 JSON（reports/ml_model.json）——回退 linear 打分路径即默认行为。")
        return 0
    feats = {k: 0.0 for k in _FEATURES}
    r = infer(feats, model=model, log_fallback=False)
    print("模型版本=%s | 训练区间=%s | 推理(feats=0)=%s"
          % (model.get("version"), model.get("date_range"), r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())