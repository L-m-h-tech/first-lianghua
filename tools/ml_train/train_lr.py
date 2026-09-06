# -*- coding: utf-8 -*-
r"""G16（第88轮）浅 ML 训练：纯标准库逻辑回归（meta-label 概率）+ 极简 JSON 导出（研究侧预备）。

按总纲 G16：训练产物只导出极简 JSON（特征列表/版本/训练区间/权重/样本外指标）；
生产 ml_inference.py 用标准库前向推理；本机无 sklearn，按总纲"标准库可实现的逻辑回归/
评分卡"台阶实现（IRLS/牛顿法，标准化特征）。研究侧、不上线（样本跨度不足，结论仅管线验证）。

用法：
  D:\Python\python.exe tools\ml_train\train_lr.py                # 全量训练+OOS 折叠评估+导出
  D:\Python\python.exe tools\ml_train\train_lr.py --selftest
"""
import argparse
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import ml_train.dataset as ds                     # noqa: E402  数据集/purged切分/指标

MODEL_JSON = ROOT / "reports" / "ml_model.json"
REPORT_TXT = ROOT / "reports" / "ml_train.txt"
LR_ITERS = 300
LR_L2 = 1e-3


# ================= 纯标准库逻辑回归（IRLS，可合成断言） =================
def sigmoid(z):
    if z >= 0:
        exp_neg = math.exp(-z)
        return 1.0 / (1.0 + exp_neg)
    e = math.exp(z)
    return e / (1.0 + e)


def predict_proba(X, weights, intercept):
    """标准前向：sigmoid(Σw·x + b)。X=标准化特征矩阵；返回概率 list。"""
    return [sigmoid(sum(w * x for w, x in zip(weights, row)) + intercept) for row in X]


def fit_lr(X, y, iters=LR_ITERS, l2=LR_L2):
    """IRLS（迭代重加权最小二乘）二分类逻辑回归；X 应为标准化特征。返回 (weights, intercept)。"""
    n = len(X)
    k = len(X[0]) if X else 0
    if n == 0 or k == 0:
        return [], 0.0
    w = [0.0] * k
    b = 0.0
    for _ in range(iters):
        z = [sum(w[i] * row[i] for i in range(k)) + b for row in X]
        p = [sigmoid(zi) for zi in z]
        # 梯度（带 L2 对 w 的正则）
        gw = [sum((p[t] - y[t]) * X[t][i] for t in range(n)) / n + l2 * w[i] for i in range(k)]
        gb = sum(p[t] - y[t] for t in range(n)) / n
        # 对角 Hessian 近似（Newton 步，对角线）
        hw = [sum(p[t] * (1 - p[t]) * X[t][i] * X[t][i] for t in range(n)) / n + l2
              for i in range(k)]
        hb = sum(p[t] * (1 - p[t]) for t in range(n)) / n
        for i in range(k):
            if hw[i] > 1e-12:
                w[i] -= gw[i] / hw[i]
        if hb > 1e-12:
            b -= gb / hb
    return w, b


# ================= 全流程 =================
def run(db_path=None, out_json=None, out_txt=None, n_fold=ds.N_FOLD,
        embargo=ds.EMBARGO, verbose=True):
    db_path = str(db_path or ds.DEFAULT_DB)
    out_json = str(out_json or MODEL_JSON)
    out_txt = str(out_txt or REPORT_TXT)
    X, y, meta = ds.load_samples(db_path)
    if len(X) < 200:
        if verbose:
            print("样本不足：n=%d（G16 硬门槛=跨度≥250交易日，当前仅管线验证）" % len(X))
        return {"note": "样本不足", "n": len(X)}
    folds = ds.purged_folds(meta, n_fold=n_fold, embargo=embargo)
    target = ds.pos_target(y)
    # 用第一折训练 fit；全部折聚合 OOS 指标
    tr0, te0 = folds[0]
    mean, std = ds.fit_scaler([X[i] for i in tr0])
    Xs = ds.standardize(X, mean, std)
    weights, intercept = fit_lr([Xs[i] for i in tr0], [target[i] for i in tr0])
    oos = ds.fold_oos_metrics(folds, X, y, lambda te: predict_proba(te, weights, intercept))
    dates = sorted({m["trade_date"] for m in meta})
    model = {
        "version": 1,
        "trained_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date_range": [dates[0], dates[-1]],
        "n_samples": len(X),
        "features": list(ds.FEATURES),
        "weights": [round(v, 6) for v in weights],
        "intercept": round(intercept, 6),
        "scaler_mean": [round(v, 6) for v in mean],
        "scaler_std": [round(v, 6) for v in std],
        "target": "label==+1 (triple-barrier 止盈) meta-label 概率",
        "n_fold": n_fold, "embargo": embargo,
        "oos_metrics": oos,
        "note": "G16 研究侧管线验证：样本跨度不足（<250交易日），此模型不得上线；等跨度达标后重训",
    }
    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    with open(out_json, "w", encoding="utf-8", newline="\n") as f:
        json.dump(model, f, ensure_ascii=False, indent=1)
    lines = [
        "=" * 96,
        " G16 浅ML 训练管线（纯标准库逻辑回归·meta-label概率）  生成于 %s" % model["trained_at"],
        "=" * 96,
        "样本 %d 条（%s ~ %s）| 特征 %d | purged %d 折 embargo %d | 目标=止盈(label==+1)概率" % (
            len(X), dates[0], dates[-1], len(ds.FEATURES), n_fold, embargo),
        "OOS 折叠指标：accuracy=%.4f precision_pos=%.4f recall_pos=%.4f AUC=%.4f（正类率 %.1f%%）" % (
            oos.get("accuracy", 0), oos.get("precision_pos", 0), oos.get("recall_pos", 0),
            oos.get("auc", 0), 100.0 * oos.get("pos_rate", 0)),
        "特征权重：" + ", ".join("%s=%+.4f" % (f, w) for f, w in zip(ds.FEATURES, weights)),
        "【诚实边界】样本跨度仅 %d 交易日（G16 硬门槛≥250），本模型仅供管线验证，禁止上线；" % len(set(dates)),
        "模型 JSON 已导出 %s（ml_inference.py 标准库前向推理，缺模型回退线性打分）。" % out_json,
        "=" * 96,
    ]
    text = "\n".join(lines)
    if verbose:
        print(text)
    with open(out_txt, "w", encoding="utf-8", newline="\n") as f:
        f.write(text + "\n")
    return model


def selftest():
    # 1) sigmoid 边界与单调
    assert abs(sigmoid(0) - 0.5) < 1e-9 and sigmoid(-10) < 1e-4 and sigmoid(10) > 0.999
    # 2) fit_lr 恢复可分数据：X=[[1,0],[0,1],[1,1],[0,0]]，y=[1,1,1,0] 的权重 w0>0,w1>0,b<0
    X = [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0], [0.0, 0.0]]
    y = [1, 1, 1, 0]
    w, b = fit_lr(X, y, iters=600, l2=0.0)
    p = predict_proba(X, w, b)
    assert p[3] < 0.3 and p[0] > 0.7 and p[1] > 0.7 and p[2] > 0.9
    # 3) predict_proba 与手算一致
    assert abs(predict_proba([[1.0, 2.0]], [0.5, -0.5], 0.1)[0] -
               sigmoid(0.5 * 1.0 + -0.5 * 2.0 + 0.1)) < 1e-12
    print("ml_train.train_lr selftest ALL PASS（sigmoid边界/可分恢复/前向手算 共3组）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="G16 浅ML 训练（标准库逻辑回归，研究侧预备）")
    ap.add_argument("--db", default=str(ds.DEFAULT_DB))
    ap.add_argument("--out-json", default=str(MODEL_JSON))
    ap.add_argument("--out-txt", default=str(REPORT_TXT))
    ap.add_argument("--n-fold", type=int, default=ds.N_FOLD)
    ap.add_argument("--embargo", type=int, default=ds.EMBARGO)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    run(db_path=args.db, out_json=args.out_json, out_txt=args.out_txt,
        n_fold=args.n_fold, embargo=args.embargo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
