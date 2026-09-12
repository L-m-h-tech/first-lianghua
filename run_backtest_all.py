#!/usr/bin/env python3
"""
盘后全自动批处理（第121轮三合一 → 第134轮八步全家桶）

用法：
  cd E:\\LHsystem\\量化\\futures_monitor
  python run_backtest_all.py                          # 全部 8 步
  python run_backtest_all.py --limit 5                # 快速抽样（回测只跑前5品种）
  python run_backtest_all.py --skip pcr_vol,shadow    # 跳过指定步骤
  python run_backtest_all.py --list                   # 只列步骤不执行

功能（按依赖顺序执行）：
  1. 成交量PCR采集（pcr_vol_collector，天勤+AKShare协同，增量1日）
  2. 日线最小回测（backtest.py）              → reports/backtest_report.txt
  3. 日内/平今分钟回测（intraday_backtest.py）→ reports/intraday_backtest_report.txt
  4. 组合账户回测（portfolio.py）             → reports/portfolio_report.txt / portfolio_trades.csv
  5. 1-2bar短单病理切片（short_trade_pathology，读组合回测成交）
  6. 纸面账户三方对账（paper_reconcile，读纸面20账户库+signal_outcomes）
  7. PCR因子影子体检（pcr_factor_research，读option_chains持仓量PCR历史）
  8. 规则影子实验室（rule_shadow_lab，R1/R3/R4单变量对照）
设计原则：
  - 独立运行，不依赖 main.py 常驻进程；跑完全套自动退出；手动触发或计划任务均可
  - 每步独立 try/except，某步失败不阻塞后续；报告文件落盘后由看板"研究报告(全部)"自动聚合
  - 新步骤（第134轮）全部为研究侧只读/写自己报告，不改生产参数与综合分口径
  - 顺序有讲究：成交量采集最先（数据新），病理/对账必须在组合回测之后（读其成交CSV）
"""
import sys
import os
import time
import traceback
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))   # 研究侧工具（第134轮并入的步骤所在）
os.chdir(ROOT)


def build_steps(args):
    """按 --skip 过滤后的步骤清单 [(名称, 可调用, argv)]；顺序有依赖讲究（见模块 docstring）。"""
    limit_flag = ["--limit", str(args.limit)] if args.limit > 0 else []
    steps = [
        ("成交量PCR采集", "pcr_vol_collector", ["--mode", "fast", "--backfill", "1"]),
        ("日线最小回测", "backtest", list(limit_flag)),
        ("日内/平今分钟回测", "intraday_backtest", ["--all"] + limit_flag),
        ("组合账户回测", "portfolio", ["--all"] + limit_flag),
        ("1-2bar短单病理切片", "short_trade_pathology", ["--period", "30"]),
        ("纸面账户三方对账", "paper_reconcile", []),
        ("PCR因子影子体检", "pcr_factor_research", []),
        ("规则影子实验室", "rule_shadow_lab", []),
    ]
    skip = {s.strip().lower() for s in (args.skip or "").split(",") if s.strip()}
    if not skip:
        return steps, skip
    # 匹配步骤名或模块名（中文名/pcr_vol 等模块子串均可）
    keep = [(n, m, a) for n, m, a in steps
            if not any(s and (s in n.lower() or s in m.lower()) for s in skip)]
    return keep, skip


def _run_step(name, func, argv=None):
    """执行一个步骤（函数收 argv 列表），返回 (成功/失败, 耗时秒数)。"""
    t0 = time.time()
    try:
        func(argv or [])
        elapsed = time.time() - t0
        print(f"  ✅ {name} 完成（{elapsed:.1f}s）")
        return True, elapsed
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  ❌ {name} 失败（{elapsed:.1f}s）: {type(e).__name__}: {str(e)[:200]}")
        traceback.print_exc()
        return False, elapsed


def main():
    import argparse
    parser = argparse.ArgumentParser(description="盘后全自动批处理（第134轮八步：采集+回测三件套+切片+对账+体检+影子）")
    parser.add_argument("--limit", type=int, default=0,
                        help="回测只跑前N个品种（0=全部；快速抽样用5或10）")
    parser.add_argument("--skip", default="",
                        help="跳过步骤：逗号分隔名称子串（如 pcr_vol,shadow,病理）")
    parser.add_argument("--list", action="store_true", help="只列步骤不执行")
    args = parser.parse_args()

    steps, skip = build_steps(args)
    if args.list:
        for k, (n, m, a) in enumerate(steps, 1):
            print(f"  [{k}/{len(steps)}] {n}  ({m} {a})")
        print(f"跳过: {skip or '无'}")
        return 0

    total_t0 = time.time()
    print(f"{'='*60}")
    print(f"盘后全自动批处理  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"品种: {'全部' if args.limit <= 0 else f'前{args.limit}个'} ｜ 步骤 {len(steps)}/8（跳过: {skip or '无'}）")
    print(f"{'='*60}\n")

    results = []
    for k, (name, mod_name, argv) in enumerate(steps, 1):
        print(f"[{k}/{len(steps)}] {name}...")
        try:
            mod = __import__(mod_name)
            fn = getattr(mod, "main")
        except (ImportError, AttributeError) as e:
            results.append((name, False, 0.0))
            print(f"  ❌ 模块加载失败: {e}")
            continue
        ok, t = _run_step(name, fn, argv)
        results.append((name, ok, t))
        print()

    total_elapsed = time.time() - total_t0
    print(f"{'='*60}")
    print(f"全部完成  总耗时 {total_elapsed:.1f}s ({total_elapsed/60:.1f}min)")
    print(f"{'='*60}")
    print(f"\n结果汇总:")
    n_ok = sum(1 for _, ok, _ in results if ok)
    for name, ok, t in results:
        print(f"  {name:<14} {'✅ 成功' if ok else '❌ 失败'}  {t:.1f}s")
    print(f"  合计 {n_ok}/{len(results)} 步成功")

    # 报告文件存在性核查（研究的全部落盘物）
    from config import (BACKTEST_REPORT_FILE as f1, INTRADAY_BT_REPORT_FILE as f2,
                        PORTFOLIO_REPORT_FILE as f3)
    reports_dir = os.path.join(ROOT, "reports")
    checks = [("日线回测", f1), ("日内回测", f2), ("组合回测", f3),
              ("短单病理", os.path.join(reports_dir, "short_trade_pathology.txt")),
              ("纸面对账", os.path.join(reports_dir, "paper_reconcile.txt")),
              ("PCR体检", os.path.join(reports_dir, "pcr_factor_research.txt")),
              ("规则影子", os.path.join(reports_dir, "rule_shadow_lab.txt"))]
    print(f"\n报告核查:")
    for label, fpath in checks:
        exists = os.path.isfile(fpath)
        mt = ""
        if exists:
            mt = datetime.fromtimestamp(os.path.getmtime(fpath)).strftime("%m-%d %H:%M")
        print(f"  {label} 报告: {'存在 ✅' if exists else '缺失 ❌'} ({os.path.basename(fpath)} {mt})")
    print(f"\n看板'研究报告(全部)'页签将自动聚合以上报告。进程即将退出...")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
