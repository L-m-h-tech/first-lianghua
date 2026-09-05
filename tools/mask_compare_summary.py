# -*- coding: utf-8 -*-
r"""G22续（第70轮）掩码前后对照汇总 mask_compare_summary：聚合 carry_eval/xsmom_eval 两个
sidecar 的 mask_compare 数据，输出统一对照汇总报告（含剔除统计、前后绩效对比、诚实结论），
供复盘（research_review/人工）直接查看。纯标准库、零网络、只读 reports/。

用法（项目根目录）：
  D:\Python\python.exe tools\mask_compare_summary.py                 # 读 reports/ 汇总
  D:\Python\python.exe tools\mask_compare_summary.py --selftest      # 零网络合成断言
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_REPORTS = ROOT / "reports"
DEFAULT_TXT = _REPORTS / "mask_compare_summary.txt"
CARRY_JSON = _REPORTS / "carry_eval.json"
XSMOM_JSON = _REPORTS / "xsmom_eval.json"


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _pf_line(pf):
    if not pf:
        return "无样本"
    return "期数%d/净t%+.2f/净均收%+.3f%%/胜率%.0f%%/单调%.0f%%/Q5-Q1%+.3f" % (
        pf.get("n_periods", 0), pf.get("net_t", 0.0) if pf.get("net_t") is not None else 0.0,
        (pf.get("net_mean") or 0.0) * 100, (pf.get("win") or 0.0) * 100,
        ((pf.get("bands") or {}).get("mono") or 0.0) * 100,
        ((pf.get("bands") or {}).get("spread") or 0.0))


def _extract(path, key):
    """读 sidecar JSON，取 mask_compare 数据；缺文件/缺键返回 None。"""
    if not os.path.exists(path):
        return None
    try:
        d = json.load(open(path, encoding="utf-8"))
        mc = d.get(key)
        if not mc:
            return None
        raw = mc.get("raw") or {}
        mask = mc.get("mask") or {}
        removed = mc.get("removed") or {}
        raw_pf = {"n_periods": raw.get("n_periods"), "net_t": (raw.get("pf") or {}).get("net_t"),
                  "net_mean": (raw.get("pf") or {}).get("net_mean"),
                  "win": (raw.get("pf") or {}).get("win"),
                  "bands": raw.get("bands")}
        mask_pf = {"n_periods": mask.get("n_periods"), "net_t": (mask.get("pf") or {}).get("net_t"),
                   "net_mean": (mask.get("pf") or {}).get("net_mean"),
                   "win": (mask.get("pf") or {}).get("win"),
                   "bands": mask.get("bands")}
        return {"raw": raw_pf, "mask": mask_pf, "removed": removed}
    except Exception:
        return None


def build_report():
    """聚合两个 sidecar，返回人类可读汇总文本。"""
    carry = _extract(str(CARRY_JSON), "mask_compare")
    xs = _extract(str(XSMOM_JSON), "mask_compare_xs")
    L = ["=" * 104,
         " G22续 掩码前后截面多空绩效对照汇总（carry_eval/xsmom_eval）  生成于 %s" % _now(),
         "=" * 104]
    L.append("来源：carry_eval.json(mask_compare) / xsmom_eval.json(mask_compare_xs)；")
    L.append("掩码规则=疑似锁板(收盘贴板达品种常态板幅) 或 距交割月1号≤15自然日 -> 剔除。")
    L.append("")
    L.append("【carry_eval（主因子 carry）】")
    if carry:
        L.append("  原始(无掩码) : %s" % _pf_line(carry["raw"]))
        L.append("  掩码后       : %s" % _pf_line(carry["mask"]))
        r = carry["removed"]
        if r:
            L.append("  剔除统计     : 原%d点→剔锁板%d+临近交割%d→剩%d点" %
                     (r.get("original", 0), r.get("locked", 0), r.get("near", 0), r.get("filtered", 0)))
    else:
        L.append("  （缺 carry_eval.json 的 mask_compare，先跑 tools/carry_eval.py --mask-compare）")
    L.append("")
    L.append("【xsmom_eval（主因子 z{main_l}）】")
    if xs:
        L.append("  原始(无掩码) : %s" % _pf_line(xs["raw"]))
        L.append("  掩码后       : %s" % _pf_line(xs["mask"]))
        r = xs["removed"]
        if r:
            L.append("  剔除统计     : 原%d点→剔锁板%d+临近交割%d→剩%d点" %
                     (r.get("original", 0), r.get("locked", 0), r.get("near", 0), r.get("filtered", 0)))
    else:
        L.append("  （缺 xsmom_eval.json 的 mask_compare_xs，先跑 tools/xsmom_eval.py --mask-compare）")
    L.append("")
    L.append("【诚实结论】")
    L.append("  两工具一致：日线级疑似锁板几乎不出现（全样本仅个位数）；“临近交割”剔除（15天窗口）")
    L.append("  会砍掉约一半样本、主窗（1023交易日上限）下掩码后期数减半、绩效波动大（净t/单调不稳定）——")
    L.append("  掩码价值在日线级在于防锁板，交割剔除主要作用是降样本量而非改善信号；研究侧如实记录，不进综合分。")
    L.append("=" * 104)
    return "\n".join(L)


def run(txt_path=None, verbose=True):
    text = build_report()
    if verbose:
        print(text)
    txt_path = txt_path or str(DEFAULT_TXT)
    os.makedirs(os.path.dirname(txt_path), exist_ok=True)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    return {"txt": txt_path}


# =========================== 零网络/零DB 合成自测 ===========================
def _mk_fake(path, key):
    """写一个最小 fake sidecar（合成断言用）。"""
    import tempfile
    d = {key: {"raw": {"n_periods": 12, "pf": {"net_t": 1.36, "net_mean": 0.0235, "win": 0.67},
                       "bands": {"mono": 0.75, "spread": 0.0241}},
               "mask": {"n_periods": 6, "pf": {"net_t": 0.95, "net_mean": 0.0330, "win": 0.67},
                        "bands": {"mono": 0.50, "spread": 0.0335}},
               "removed": {"original": 5952, "locked": 1, "near": 3144, "filtered": 2807}}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)


def selftest():
    # 1) _pf_line 格式化
    pf = {"n_periods": 12, "net_t": 1.36, "net_mean": 0.0235, "win": 0.67,
          "bands": {"mono": 0.75, "spread": 0.0241}}
    s = _pf_line(pf)
    assert "12" in s and "1.36" in s and "67" in s
    assert "无样本" in _pf_line(None)
    # 2) _extract 缺文件返回 None
    assert _extract(str(_REPORTS / "no_such_file.json"), "mask_compare") is None
    # 3) build_report 在缺 sidecar 时也出报告（提示行），不抛错
    txt = build_report()
    assert "掩码前后" in txt
    assert "carry_eval" in txt and "xsmom_eval" in txt
    print("mask_compare_summary selftest ALL PASS（绩效行格式化/缺文件安全/报告结构 共3组）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="G22续 掩码前后对照汇总（研究侧）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--out", default=str(DEFAULT_TXT))
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    run(txt_path=args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
