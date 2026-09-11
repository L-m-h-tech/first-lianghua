#!/usr/bin/env python3
"""
盘后全自动回测（第121轮）

用法：
  cd E:\\LHsystem\\量化\\futures_monitor
  python run_backtest_all.py            # 运行后自动退出
  python run_backtest_all.py --limit 5  # 快速抽样测试（只回测5个品种）

功能（按顺序执行）：
  1. 日线最小回测（backtest.py）      → reports/backtest_report.txt
  2. 日内/平今分钟回测（intraday_backtest.py）→ reports/intraday_backtest_report.txt
  3. 组合账户回测（portfolio.py）      → reports/portfolio_report.txt

设计原则：
  - 独立运行，不依赖 main.py 常驻进程
  - 跑完全套自动退出，手动触发或计划任务均可
  - 每步独立 try/except，某步失败不阻塞后续
  - 支持 --limit N 快速抽样验证（N=0 或缺省=全品种64个）
  - 运行结束打印用时和报告路径，方便确认
"""
import sys
import os
import time
import traceback

# 确保项目根目录在 path（脚本可从任何目录调用）
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# 延迟导入（避免 __init__ 问题）
import backtest
import intraday_backtest
import portfolio


def _run_step(name, func, args=None):
    """执行一个回测步骤，返回 (成功/失败, 耗时秒数)"""
    args = args or []
    t0 = time.time()
    try:
        func(args)
        elapsed = time.time() - t0
        print(f"  ✅ {name} 完成（{elapsed:.1f}s）")
        return True, elapsed
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  ❌ {name} 失败（{elapsed:.1f}s）: {type(e).__name__}: {e}")
        traceback.print_exc()
        return False, elapsed


def main():
    import argparse
    parser = argparse.ArgumentParser(description="盘后全自动回测（三合一）")
    parser.add_argument("--limit", type=int, default=0,
                        help="只回测前N个品种（0=全部64个；快速抽样用5或10）")
    args = parser.parse_args()

    limit_flag = ["--limit", str(args.limit)] if args.limit > 0 else []

    total_t0 = time.time()
    print(f"{'='*60}")
    print(f"盘后全自动回测  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"品种: {'全部' if not limit_flag else f'前{args.limit}个'}")
    print(f"{'='*60}\n")

    results = []

    # ---- 步骤 1：日线最小回测 ----
    print(f"[1/3] 日线最小回测...")
    ok, t = _run_step("日线回测", backtest.main, limit_flag)
    results.append(("日线回测", ok, t))

    # ---- 步骤 2：日内/平今分钟回测 ----
    print(f"\n[2/3] 日内/平今分钟回测...")
    ib_args = ["--all"] + limit_flag  # --all 覆盖全部品种（含非重点品种）
    ok, t = _run_step("日内回测", intraday_backtest.main, ib_args)
    results.append(("日内回测", ok, t))

    # ---- 步骤 3：组合账户回测 ----
    print(f"\n[3/3] 组合账户回测...")
    pf_args = ["--all"] + limit_flag  # --all + --period 使用默认值(30m)
    ok, t = _run_step("组合回测", portfolio.main, pf_args)
    results.append(("组合回测", ok, t))

    # ---- 汇总 ----
    total_elapsed = time.time() - total_t0
    print(f"\n{'='*60}")
    print(f"全部完成  总耗时 {total_elapsed:.1f}s ({total_elapsed/60:.1f}min)")
    print(f"{'='*60}")
    print(f"\n结果汇总:")
    for name, ok, t in results:
        status = "✅ 成功" if ok else "❌ 失败"
        print(f"  {name:<10} {status}  {t:.1f}s")

    # 检查报告文件是否生成
    from config import BACKTEST_REPORT_FILE as f1, INTRADAY_BT_REPORT_FILE as f2, PORTFOLIO_REPORT_FILE as f3
    for label, fpath in [("日线回测", f1), ("日内回测", f2), ("组合回测", f3)]:
        exists = os.path.isfile(fpath)
        print(f"  {label} 报告: {'存在 ✅' if exists else '缺失 ❌'} ({os.path.basename(fpath)})")

    # 进程自然退出（不需要手动退出，无挂起的 daemon 线程）
    print(f"\n进程即将退出...")


if __name__ == "__main__":
    from datetime import datetime
    main()
