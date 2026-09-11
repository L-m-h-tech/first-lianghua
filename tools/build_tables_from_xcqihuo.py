#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""信查期货 commission.xlsx → 同时更新 futures_fees.csv + futures_margins.csv。
自动检测：若两个表的 as_of 已是最新（基于 xlsx 文件修改日期），则跳过直接退出；否则运行一次后停止。

用法：
  python tools/build_tables_from_xcqihuo.py                       # 检测后按需运行
  python tools/build_tables_from_xcqihuo.py --force               # 强制重新生成
  python tools/build_tables_from_xcqihuo.py --xlsx "新路径.xlsx"  # 用指定 xlsx
"""
import sys
import os
import re
import csv
import shutil
import argparse
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent.parent
FEES_CSV = ROOT / "data" / "futures_fees.csv"
MARGINS_CSV = ROOT / "data" / "futures_margins.csv"
DEFAULT_XLSX = Path(r"C:\Users\Lenovo\Desktop\commission.xlsx")

EX_MAP = {
    "上海期货交易所": "SHFE", "大连商品交易所": "DCE", "郑州商品交易所": "CZCE",
    "中国金融期货交易所": "CFFEX", "上海国际能源交易中心": "INE", "广州期货交易所": "GFEX",
}


# ── 工具函数 ──

def xlsx_date_str(path: Path) -> str:
    """用 xlsx 文件修改时间生成日期字符串 (YYYYMMDD)，作为对比基准。"""
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d")


def csv_as_of(csv_path: Path) -> str:
    """从 CSV 读取 as_of 字段。
    fees 表 as_of 在第 12 列(index 11)；margins 表 as_of 在第 8 列(index 7)。
    用表头定位 as_of 列位置，避免取错（margins 表末列是 note）。"""
    try:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            header = next(reader)
            if "as_of" not in header:
                return ""
            idx = header.index("as_of")
            rows = list(reader)
            if not rows:
                return ""
            return (rows[-1][idx] if idx < len(rows[-1]) else "").strip()
    except Exception:
        return ""


def parse_fee(v):
    """解析手续费：金额型 '1.00元' → (0, 1.00)；比例型 '0.50/万分之(3.57元)' → (0.00005, 0)。"""
    if v is None:
        return None, None
    s = str(v).strip()
    if not s:
        return None, None
    m = re.search(r"([\d.]+)/万分之", s)
    if m:
        return float(m.group(1)) / 10000.0, 0.0
    m2 = re.search(r"([\d.]+)\s*元", s)
    if m2:
        return 0.0, float(m2.group(1))
    try:
        return 0.0, float(s)
    except ValueError:
        return None, None


def infer_mult(price, margin_pct, amt):
    """从现价/保证金率/每手反推合约乘数。"""
    try:
        p = float(str(price).replace(",", "").strip())
        m = float(str(margin_pct).replace("%", "").strip()) / 100.0
        a = float(str(amt).replace(",", "").replace("元", "").strip())
        if p > 0 and m > 0 and a > 0:
            return int(round(a / (m * p)))
    except (ValueError, TypeError):
        pass
    return 0


# ── 转换逻辑 ──

EX_CN_MAP = {
    "上海期货交易所": "SHFE", "大连商品交易所": "DCE", "郑州商品交易所": "CZCE",
    "中国金融期货交易所": "CFFEX", "上海国际能源交易中心": "INE", "广州期货交易所": "GFEX",
}


def parse_rows(xlsx_path: str, add_one: bool):
    """解析 xlsx，返回 (fee_rows, margin_rows) 两份数据列表。"""
    wb = load_workbook(xlsx_path, read_only=True)
    ws = wb[wb.sheetnames[0]]
    date = datetime.now().strftime("%Y%m%d")
    fee_rows, margin_rows = [], []
    seen = set()
    for row in ws.iter_rows(min_row=3, values_only=True):
        ex_cn, name_cell = row[0], row[1]
        if not name_cell:
            continue
        text = str(name_cell).strip()
        m = re.search(r"([A-Za-z]{1,3})\d{3,4}\s*(?:\(|$)", text.split("\n")[-1])
        code = m.group(1).upper() if m else None
        if not code or code in seen:
            continue
        seen.add(code)
        name = text.split("\n")[0].replace("（主力）", "").strip()
        exchange = EX_CN_MAP.get(ex_cn or "", "")
        # 保证金率 + 反推乘数
        margin_str = str(row[5]).replace("%", "").strip() if row[5] else "0"
        try:
            broker_margin = float(margin_str) / 100.0
        except:
            broker_margin = 0.0
        mult = infer_mult(row[2], row[5], row[7])
        # 手续费三档
        o_rate, o_lot = parse_fee(row[8])
        c_rate, c_lot = parse_fee(row[9])
        t_rate, t_lot = parse_fee(row[10])
        if add_one and o_lot is not None and o_lot > 0:
            o_lot = round(o_lot + 0.01, 4)
        if add_one and c_lot is not None and c_lot > 0:
            c_lot = round(c_lot + 0.01, 4)
        if add_one and t_lot is not None and t_lot > 0:
            t_lot = round(t_lot + 0.01, 4)
        fee_rows.append({
            "sym": code, "name": name, "exchange": exchange,
            "account_flag": "投机", "multiplier": str(mult),
            "open_amt_rate": str(o_rate or 0.0), "open_per_lot": str(o_lot or 0.0),
            "close_amt_rate": str(c_rate or 0.0), "close_per_lot": str(c_lot or 0.0),
            "today_amt_rate": str(t_rate or 0.0), "today_per_lot": str(t_lot or 0.0),
            "as_of": date,
        })
        margin_rows.append({
            "sym": code, "name": name, "exchange": exchange,
            "broker_margin": f"{broker_margin:.4f}", "exchange_margin": f"{broker_margin:.4f}",
            "limit_basic": "0", "multiplier": str(mult), "as_of": date,
            "source": "交易所标准保证金(信查期货,commission.xlsx)",
            "note": "信查期货 commission.xlsx 手续费表同期提取；买/卖保证金率=broker_margin",
        })
    wb.close()
    return fee_rows, margin_rows


def write_csv(rows, fields, csv_path: Path):
    """带备份写入 CSV。"""
    header = ",".join(fields)
    if csv_path.exists():
        bak = csv_path.parent / (csv_path.name + ".bak_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
        shutil.copy2(csv_path, bak)
        print(f"  备份: {bak.name}")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(header + "\n")
        for r in rows:
            f.write(",".join([r[k] for k in fields]) + "\n")
    print(f"  写入 {csv_path.name}: {len(rows)} 品种, as_of={rows[0]['as_of'] if rows else '?'}")


# ── 主逻辑 ──

def main(argv=None):
    parser = argparse.ArgumentParser(description="信查 commission.xlsx → fees + margins CSV（增量检测，最新跳过）")
    parser.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX, help="commission.xlsx 路径")
    parser.add_argument("--force", action="store_true", help="强制重新生成（忽略日期检测）")
    parser.add_argument("--preview", action="store_true", help="只打印预览，不写文件")
    args = parser.parse_args(argv)

    if not args.xlsx.is_file():
        print(f"文件不存在: {args.xlsx}")
        return 1

    # 日期检测
    xlsx_dt = xlsx_date_str(args.xlsx)
    fees_dt = csv_as_of(FEES_CSV)
    margins_dt = csv_as_of(MARGINS_CSV)

    if not args.force and fees_dt == xlsx_dt and margins_dt == xlsx_dt:
        print(f"已是最新（xlsx={xlsx_dt}, fees={fees_dt}, margins={margins_dt}），无需更新。")
        return 0

    print(f"xlsx={xlsx_dt}  fees={fees_dt}  margins={margins_dt} → 需要更新")
    fee_rows, margin_rows = parse_rows(str(args.xlsx), add_one=True)

    if args.preview:
        print(f"\n--- fees ({len(fee_rows)} 行) ---")
        for r in fee_rows[:6]:
            print(f"  {r['sym']}: 开{r['open_per_lot']}(R{r['open_amt_rate']})  乘{r['multiplier']}  ex={r['exchange']}")
        print(f"\n--- margins ({len(margin_rows)} 行) ---")
        for r in margin_rows[:6]:
            print(f"  {r['sym']}: {r['broker_margin']}  乘{r['multiplier']}")
        return 0

    fee_fields = ["sym", "name", "exchange", "account_flag", "multiplier",
                  "open_amt_rate", "open_per_lot", "close_amt_rate", "close_per_lot",
                  "today_amt_rate", "today_per_lot", "as_of"]
    margin_fields = ["sym", "name", "exchange", "broker_margin", "exchange_margin",
                     "limit_basic", "multiplier", "as_of", "source", "note"]

    print("生成手续费表...")
    write_csv(fee_rows, fee_fields, FEES_CSV)
    print("生成保证金表...")
    write_csv(margin_rows, margin_fields, MARGINS_CSV)
    print(f"\n完成（{len(fee_rows)} 品种，as_of={fee_rows[0]['as_of']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
