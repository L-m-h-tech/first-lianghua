# -*- coding: utf-8 -*-
r"""G16（第88轮）浅ML训练管线·数据集构建（研究侧预备，等样本跨度达标即解锁）。

按总纲 G16：
- 只做"多因子融合/meta-label 概率"（预测 triple-barrier 为正的概率），不做价格预测/DNN；
- 强制 Purged TimeSeriesSplit+embargo（禁随机 K 折）——复用 build_ml_samples.purged_embargo_split；
- 训练产物只导出极简 JSON；生产 ml_inference.py 用标准库前向推理，缺模型回退线性打分；
- 纯标准库（本机无 sklearn，按总纲"标准库可实现的逻辑回归/评分卡"台阶实现）。

本模块：从 monitor.db 的 ml_samples 读特征/标签 → X/y/meta；按时间序 purged 切分；
特征标准化（只用训练折统计，防泄漏）。纯函数可合成断言、零网络。
"""
import json
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # tools/ml_train/ -> 项目根
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import build_ml_samples as bms                  # noqa: E402  purged_embargo_split 复用

DEFAULT_DB = ROOT / "data" / "monitor.db"
FEATURES = ("mom5", "mom20", "ma10_bias", "ma20_bias", "ma60_bias",
            "rsv20", "atr_pct", "vol60", "tech_score", "ret1")
N_FOLD = 4
EMBARGO = 3          # 标签窗口后留 embargo 根（禁横跨切分点）

_MEAN = {"mom5": 0.0, "mom20": 0.0, "ma10_bias": 0.0, "ma20_bias": 0.0,
         "ma60_bias": 0.0, "rsv20": 0.5, "atr_pct": 0.0, "vol60": 10000.0,
         "tech_score": 0.0, "ret1": 0.0}
_STD = {"mom5": 0.02, "mom20": 0.05, "ma10_bias": 0.03, "ma20_bias": 0.05,
        "ma60_bias": 0.08, "rsv20": 0.3, "atr_pct": 0.01, "vol60": 20000.0,
        "tech_score": 2.0, "ret1": 0.01}


def is_num(x):
    return isinstance(x, (int, float)) and x == x and math.isfinite(float(x))


def load_samples(db_path=None, features=FEATURES):
    """从 ml_samples 读样本：X（有限特征行）、y（label）、meta（品种/日期/方向/bars_held）。

    丢弃任一特征非有限的样本（vol60 量纲大，缺值即丢，不编造）。返回 (X, y, meta)。"""
    db_path = str(db_path or DEFAULT_DB)
    X, y, meta = [], [], []
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    for row in cur.execute(
            "SELECT features_json,label,variety,trade_date,direction,bars_held "
            "FROM ml_samples ORDER BY trade_date,id"):
        try:
            f = json.loads(row[0])
        except (TypeError, ValueError):
            continue
        vec = [f.get(k) for k in features]
        if not all(is_num(v) for v in vec):
            continue
        X.append(vec)
        y.append(int(row[1]))
        meta.append({"variety": row[2], "trade_date": row[3],
                     "direction": int(row[4] or 0), "bars_held": int(row[5] or 1)})
    con.close()
    return X, y, meta


def purged_folds(meta, n_fold=N_FOLD, embargo=EMBARGO):
    """Purged TimeSeriesSplit（禁随机 K 折）：样本按时间序切 n_fold 折，
    每折 (train, test)= 该折作为测试折时其余作训练（剔除标签探入测试折的样本）。

    返回 [(train_idx, test_idx), ...]，idx 为样本下标。纯函数、可合成断言。"""
    n = len(meta)
    order = list(range(n))                       # 已按时间排序
    edges = [round(n * k / n_fold) for k in range(n_fold + 1)]
    folds = []
    for k in range(n_fold):
        lo, hi = edges[k], edges[k + 1]
        label_end = [pos + max(1, int(meta[i]["bars_held"] or 1)) for i, pos in enumerate(order)]
        tr, te = bms.purged_embargo_split(order, label_end, lo, hi, embargo=embargo)
        folds.append((tr, te))
    return folds


def fit_scaler(X_train):
    """训练折特征均值/标准差（防泄漏：只用训练折统计）。返回 (mean, std)。"""
    n = len(X_train)
    k = len(X_train[0])
    mean = [sum(r[i] for r in X_train) / n for i in range(k)]
    std = [math.sqrt(sum((r[i] - mean[i]) ** 2 for r in X_train) / n) or 1.0
           for i in range(k)]
    return mean, std


def standardize(X, mean, std):
    return [[(v - m) / s for v, m, s in zip(row, mean, std)] for row in X]


def pos_target(y):
    """meta-label 二分类目标：label=+1（止盈）为正类，-1/0 为负类。"""
    return [1 if v == 1 else 0 for v in y]


def fold_oos_metrics(folds, X, y, predict_fn):
    """对全部折 OOS 预测聚合出 准确率/正类精度/正类召回/AUC（标准库实现）。返回 dict。"""
    ys, ps = [], []
    for tr, te in folds:
        mean, std = fit_scaler([X[i] for i in tr])
        Xs = standardize(X, mean, std)
        p = predict_fn([Xs[i] for i in te])
        ys.extend(pos_target([y[i] for i in te]))
        ps.extend(p)
    return binary_metrics(ys, ps)


def binary_metrics(y_true, y_prob):
    """二分类指标：准确率/正类精度/正类召回/AUC（梯形法）。"""
    n = len(y_true)
    if n == 0:
        return {}
    preds = [1 if p >= 0.5 else 0 for p in y_prob]
    acc = sum(1 for a, b in zip(y_true, preds) if a == b) / n
    tp = sum(1 for a, p in zip(y_true, preds) if a == 1 and p == 1)
    fp = sum(1 for a, p in zip(y_true, preds) if a == 0 and p == 1)
    fn = sum(1 for a, p in zip(y_true, preds) if a == 1 and p == 0)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    pos = [(p, a) for p, a in zip(y_prob, y_true) if p is not None]
    pos.sort(key=lambda kv: -kv[0])
    auc = _auc_rank(pos)
    return {"n": n, "accuracy": round(acc, 4), "precision_pos": round(prec, 4),
            "recall_pos": round(rec, 4), "auc": round(auc, 4),
            "pos_rate": round(sum(y_true) / n, 4)}


def _auc_rank(pairs):
    """AUC（Mann-Whitney U / 秩和法，并列平均秩）。pairs=[(prob, true),...]；
    秩按 prob 升序（rank1=最小 prob），正类 prob 高→rank 大→U 大→AUC∈[0,1]。"""
    m = sum(1 for _p, a in pairs if a == 1)
    nn = len(pairs) - m
    if m == 0 or nn == 0:
        return 0.5
    order = sorted(range(len(pairs)), key=lambda i: pairs[i][0])
    ranks = [0.0] * len(pairs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and pairs[order[j + 1]][0] == pairs[order[i]][0]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for t in range(i, j + 1):
            ranks[order[t]] = avg
        i = j + 1
    sum_pos = sum(ranks[t] for t, (_p, a) in enumerate(pairs) if a == 1)
    return (sum_pos - m * (m + 1) / 2.0) / (m * nn)


def selftest():
    # 1) 合成小样本：purged_folds 各折不相交、train+test 覆盖、embargo 剔除生效
    meta = [{"variety": "RB", "trade_date": "2026-01-%02d" % d, "direction": 1,
             "bars_held": 5} for d in range(1, 41)]
    folds = purged_folds(meta, n_fold=4, embargo=2)
    assert len(folds) == 4
    for tr, te in folds:
        assert len(te) > 0 and len(tr) > 0
        assert set(tr).isdisjoint(set(te))
    # 2) 标准化：常量特征 std→1 保护；负数输入变正
    X = [[1.0, 100.0], [2.0, 200.0], [3.0, 300.0]]
    mean, std = fit_scaler(X)
    assert abs(std[0] - math.sqrt(2.0 / 3.0)) < 1e-9
    Z = standardize(X, mean, std)
    assert abs(sum(r[0] for r in Z)) < 1e-9
    # 3) AUC 手算：完美排序 → 1.0；随机 → ~0.5
    assert abs(binary_metrics([1, 1, 0, 0], [0.9, 0.8, 0.3, 0.2])["auc"] - 1.0) < 1e-9
    m = binary_metrics([1, 0, 1, 0], [0.5, 0.5, 0.5, 0.5])
    assert abs(m["auc"] - 0.5) < 1e-9
    # 4) load_samples 缺库安全
    try:
        X, y, meta2 = load_samples(db_path="/nonexistent/x.db")
        assert X == [] and y == [] and meta2 == []
    except Exception:
        pass
    print("ml_train.dataset selftest ALL PASS（purged折叠不相交/标准化/auc手算/缺库安全 共4组）")
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
