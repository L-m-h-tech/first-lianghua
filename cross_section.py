# -*- coding: utf-8 -*-
"""WP-F1（P0）B1：横截面相对强弱。

绝对综合分回答"这个品种本身有多强"，横截面回答"同一时刻它比其余品种强还是弱"：
  - 对全部品种的综合分、当日涨跌幅分别做**稳健标准化**（中位数 + MAD 中位绝对偏差，
    0.6745*x/MAD），比均值/标准差更抗极端值，单个涨停品种不会把整张表拉偏；
  - 综合横截面强度 xs = XS_SCORE_W*综合分z + XS_CHG_W*涨跌幅z；
  - 按板块聚合得到板块强弱榜，并给相对最强/最弱 Top N 与全市场多空广度。

**只做信息增量：xs 仅用于横向比较与展示，绝不回改任何品种的 score/label/advice。**
纯标准库、零网络、零新增第三方依赖；任何输入异常都安全降级为空结果。
"""
import statistics

import config


def _robust_z(values):
    """返回与 values 等长的稳健 z 列表（0.6745*(x-中位数)/MAD），MAD 失效时退回总体标准差 z。"""
    n = len(values)
    if n == 0:
        return []
    med = statistics.median(values)
    mad = statistics.median([abs(x - med) for x in values])
    if mad > 1e-9:
        zs = [0.6745 * (x - med) / mad for x in values]
    else:
        sd = statistics.pstdev(values)
        zs = [(x - med) / sd for x in values] if sd > 1e-9 else [0.0] * n
    cap = config.XS_Z_CLIP
    return [max(-cap, min(cap, z)) for z in zs]


def rank(fut_rows):
    """计算横截面强弱。

    入参：analyzer.analyze_variety 产出的 row 列表（需含 name/cat/score/chg/price/label）。
    返回 dict：
      rows        [{name,cat,score,chg,score_z,chg_z,xs,rank}] 按 xs 降序
      sectors     {cat: {"n","up","down","avg_xs","avg_score","avg_chg"}}
      sector_rank [cat,...] 按 avg_xs 降序
      top_long / top_short : 最强/最弱各 XS_TOP_N 个 row 摘要
      breadth     {"bull","bear","neutral","n","avg_chg"}
      robust      bool，是否成功做了稳健标准化（样本不足为 False，xs 退回绝对分归一）
    """
    empty = {"rows": [], "sectors": {}, "sector_rank": [], "top_long": [],
             "top_short": [], "breadth": {}, "robust": False}
    try:
        rows_in = [r for r in (fut_rows or []) if r and r.get("name")]
        n = len(rows_in)
        if n == 0:
            return empty
        scores = [float(r.get("score", 0.0) or 0.0) for r in rows_in]
        chgs = [float(r.get("chg", 0.0) or 0.0) for r in rows_in]
        robust = n >= config.XS_MIN_SAMPLE
        if robust:
            sz = _robust_z(scores)
            cz = _robust_z(chgs)
        else:
            sz, cz = [0.0] * n, [0.0] * n
        ws, wc = config.XS_SCORE_W, config.XS_CHG_W
        out = []
        for r, s, c, zs, zc in zip(rows_in, scores, chgs, sz, cz):
            if robust:
                xs = ws * zs + wc * zc
            else:
                # 样本太少不做横向 z，退回绝对分归一（仍 clip 到可比量级），仅供排序
                xs = max(-config.XS_Z_CLIP, min(config.XS_Z_CLIP, s / max(config.SCORE_MID, 1e-9)))
            out.append({"name": r["name"], "cat": r.get("cat", "—"),
                        "score": s, "chg": c, "score_z": round(zs, 2),
                        "chg_z": round(zc, 2), "xs": round(xs, 2),
                        "label": r.get("label", "")})
        out.sort(key=lambda x: -x["xs"])
        for i, o in enumerate(out):
            o["rank"] = i + 1

        sectors = {}
        for o in out:
            s = sectors.setdefault(o["cat"], {"n": 0, "up": 0, "down": 0,
                                              "xs": [], "score": [], "chg": []})
            s["n"] += 1
            if o["chg"] > 1e-6:
                s["up"] += 1
            elif o["chg"] < -1e-6:
                s["down"] += 1
            s["xs"].append(o["xs"])
            s["score"].append(o["score"])
            s["chg"].append(o["chg"])
        sec_out = {}
        for cat, s in sectors.items():
            sec_out[cat] = {
                "n": s["n"], "up": s["up"], "down": s["down"],
                "avg_xs": round(statistics.mean(s["xs"]), 2),
                "avg_score": round(statistics.mean(s["score"]), 2),
                "avg_chg": statistics.mean(s["chg"]),
            }
        sector_rank = sorted(sec_out, key=lambda c: -sec_out[c]["avg_xs"])

        topn = config.XS_TOP_N
        bull = sum(1 for s in scores if s >= config.SCORE_NEUTRAL)
        bear = sum(1 for s in scores if s <= -config.SCORE_NEUTRAL)
        breadth = {"bull": bull, "bear": bear, "neutral": n - bull - bear, "n": n,
                   "avg_chg": statistics.mean(chgs)}
        return {"rows": out, "sectors": sec_out, "sector_rank": sector_rank,
                "top_long": out[:topn], "top_short": out[-topn:][::-1],
                "breadth": breadth, "robust": robust}
    except Exception:
        return empty


def _brief(o):
    return "%s(%+.1f,z%+.1f)" % (o["name"], o["score"], o["xs"])


def format_block(cs):
    """把 rank() 结果压成报告文本行列表（纯展示）；无有效结果返回空列表。"""
    if not cs or not cs.get("rows"):
        return []
    L = []
    L.append("【横截面强弱】(跨%d品种稳健z=0.6745(x-中位数)/MAD抗极端值; 综合强度=%.1f综合分z+%.1f涨跌幅z; "
             "只做横向比较、不改变原综合分/信号)"
             % (cs["breadth"]["n"], config.XS_SCORE_W, config.XS_CHG_W))
    secs = cs["sectors"]
    if secs:
        parts = []
        for cat in cs["sector_rank"]:
            s = secs[cat]
            parts.append("%s%+.2f(涨%d/跌%d)" % (cat, s["avg_xs"], s["up"], s["down"]))
        L.append(" 板块强弱: " + " > ".join(parts))
    L.append(" 相对最强Top%d: " % config.XS_TOP_N
             + ("、".join(_brief(o) for o in cs["top_long"]) or "无"))
    L.append(" 相对最弱Top%d: " % config.XS_TOP_N
             + ("、".join(_brief(o) for o in cs["top_short"]) or "无"))
    b = cs["breadth"]
    L.append(" 全市场广度: 偏多%d家/中性%d家/偏空%d家，平均涨跌%+.2f%%%s"
             % (b["bull"], b["neutral"], b["bear"], b["avg_chg"] * 100,
                "" if cs["robust"] else "（样本不足，按绝对分归一，未做稳健z）"))
    L.append("")
    return L
