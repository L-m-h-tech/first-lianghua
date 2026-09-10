# -*- coding: utf-8 -*-
r"""从信查期货（wj.xcqihuo.cn）API 抓取"交易所标准保证金/手续费"，构建回测运行时 CSV
data/futures_margins.csv + data/futures_fees.csv（替代原银河期货口径，v2 数据接入）。

数据源：https://wj.xcqihuo.cn:4433/  （中国期货交易所合约数据，87 品种/866 合约，无登录，动态页）
  API: GET /webroot/service/79036642-68d9-4e8e-baef-2f9e336b18c0/contract?XXX=20260910
       返回 {"output": [{exchange_name, contract_id, variety_id, variety_name, unit,
                        curr_price, rise_limit, fall_limit, buy_margin, sell_margin,
                        open_fee_amt, open_fee_qty, offset_fee_amt, offset_fee_qty,
                        short_offset_fee_amt, short_offset_fee_qty, remark, ...}]}

口径（用户已确认）：
- 保证金：broker_margin 直接用交易所标准档（buy_margin，投机），exchange_margin 双写同值并注记；
- 手续费加一分：固定费品种 per_lot = 交易所qty + 0.01；比例费品种 amt 保持交易所比例不变、
  另每手保底 +0.01 固定费（per_lot=0.01）。开/平/平今同理；
- limit_basic = (rise_limit - fall_limit)/(rise_limit + fall_limit)（反推已验证：RB 0.0501/C 0.06/AU 0.14/IF 0.10）；
- 乘数：margins 用 API unit（报价口径，JD=10）；fees 用项目既有 MULTIPLIERS（吨口径，JD=5）；
- 品种集：config.VARIETIES（64 品种全齐断言），与既有表结构完全一致（逐字段同名）。

仅维护工具用 requests（运行时仍只读标准库 CSV）。
用法（项目根目录）：
  D:\Python\python.exe tools\build_exchange_tables.py            # 拉当日 API，写 data/xcqihuo_preview/（预览，不覆盖）
  D:\Python\python.exe tools\build_exchange_tables.py --apply    # 确认后正式覆盖 data/futures_*.csv（备份旧表到 data/legacy_galaxy/）
  D:\Python\python.exe tools\build_exchange_tables.py --json data/xcqihuo_raw/contract_20260910.json  # 用本地已落 JSON（离线）
"""
import argparse
import csv
import io
import json
import shutil
import sys
import warnings
from datetime import datetime
from pathlib import Path

import requests

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

API_URL = ("https://wj.xcqihuo.cn:4433/webroot/service/"
           "79036642-68d9-4e8e-baef-2f9e336b18c0/contract")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
MARGIN_FIELDS = ["sym", "name", "exchange", "broker_margin", "exchange_margin",
                 "limit_basic", "multiplier", "as_of", "source", "note"]
FEE_FIELDS = ["sym", "name", "exchange", "account_flag", "multiplier",
              "open_amt_rate", "open_per_lot", "close_amt_rate", "close_per_lot",
              "today_amt_rate", "today_per_lot", "as_of"]
ADD_ONE_FEN = 0.01          # 手续费加一分（元/手，固定费部分）
EXCH_SHORT = {"上海期货交易所": "SHFE", "大连商品交易所": "DCE", "郑州商品交易所": "CZCE",
              "中国金融期货交易所": "CFFEX", "广州期货交易所": "GFEX",
              "上海国际能源交易中心": "INE"}

# 手续费表"吨/物理单位"口径乘数（与 build_fee_table.MULTIPLIERS 一致；JD 报价口径=10、吨口径=5）
FEE_MULTIPLIERS = {
    "RB": 10, "HC": 10, "SS": 5, "CU": 5, "AL": 5, "AO": 20, "ZN": 5, "PB": 5,
    "NI": 1, "SN": 1, "AU": 1000, "AG": 15, "RU": 10, "BR": 5, "FU": 10,
    "BU": 10, "SP": 10,
    "SC": 1000, "NR": 10, "LU": 10, "BC": 5, "EC": 50,
    "A": 10, "B": 10, "M": 10, "Y": 10, "P": 10, "C": 10, "CS": 10, "RR": 10,
    "JD": 5, "LH": 16, "LG": 90, "L": 5, "V": 5, "PP": 5, "EG": 10, "EB": 5,
    "PG": 20, "J": 100, "JM": 60, "I": 100,
    "SR": 10, "CF": 5, "CY": 5, "TA": 5, "MA": 10, "PX": 5, "PF": 5,
    "PR": 15, "SH": 30, "FG": 20, "SA": 20, "UR": 20, "RM": 10, "OI": 10,
    "PK": 5, "AP": 10, "CJ": 5, "SF": 5, "SM": 5,
    "SI": 5, "LC": 1, "PS": 3,
}


def _num(x):
    try:
        return float(x if x not in (None, "") else 0.0)
    except (TypeError, ValueError):
        return 0.0


def fetch(date_str):
    """拉当日全量合约 JSON；失败抛异常（由调用方兜底本地 JSON）。"""
    resp = requests.get(API_URL, params={"XXX": date_str}, timeout=20,
                        headers={"User-Agent": UA, "Accept": "application/json"})
    resp.raise_for_status()
    data = resp.json()
    out = data.get("output")
    assert isinstance(out, list) and out, "API 返回空 output"
    return out


def load_raw(date_str, json_path=None):
    if json_path and Path(json_path).exists():
        with open(json_path, encoding="utf-8") as f:
            return (json.load(f) or {}).get("output") or []
    return fetch(date_str)


def _base_contract(rows):
    """每品种取"基础档"合约——buy_margin 最小的行（常规月份，保证金率最低）；
    保证金最低档通常 = 交易所标准基础档，近月/交割月上浮。"""
    by_sym = {}
    for r in rows:
        by_sym.setdefault(r.get("variety_id"), []).append(r)
    out = {}
    for sym, rs in by_sym.items():
        # 主力优先；若主力保证金率与最低档差异不大（<1pp），取主力；否则取最低档
        main = next((r for r in rs if r.get("remark") == "主力合约"), rs[0])
        lowest = min(rs, key=lambda x: _num(x.get("buy_margin")))
        out[sym] = main if _num(main.get("buy_margin")) <= _num(lowest.get("buy_margin")) + 0.01 else lowest
    return out


def build_tables(raw_rows, date_str):
    """由 API 行生成 (margins rows, fees rows, skipped) 两个 CSV 数据列表。"""
    main_rows = _base_contract(raw_rows)
    cfg = config.VARIETIES
    cfg_syms = {m["sym"] for m in cfg.values()}
    api_syms = set(main_rows)
    missing = cfg_syms - api_syms
    assert not missing, "API 缺品种: %s" % sorted(missing)
    assert set(FEE_MULTIPLIERS) >= cfg_syms, "FEE_MULTIPLIERS 缺品种"

    margin_rows, fee_rows = [], []
    for cname, meta in cfg.items():
        sym = meta["sym"]
        r = main_rows[sym]
        exchange = EXCH_SHORT.get(r.get("exchange_name"), r.get("exchange_name") or meta["ex"])
        margin_rate = _num(r.get("buy_margin"))
        rise, fall = _num(r.get("rise_limit")), _num(r.get("fall_limit"))
        limit_basic = round((rise - fall) / (rise + fall), 4) if (rise + fall) > 0 else 0.0
        quote_mult = int(_num(r.get("unit")) or 0)
        # 不同月份 buy_margin 上浮情况（主力 vs 全合约众数/最大），写入 note
        mono = sorted({_num(x.get("buy_margin")) for x in by_sym_of(raw_rows, sym)})
        note = ("交易所标准保证金(信查期货数据)；全品种月份档=%s" %
                ("/".join("%.2f%%" % (m * 100) for m in mono[:6]) + ("" if len(mono) <= 6 else "…")))
        margin_rows.append({
            "sym": sym, "name": cname, "exchange": exchange,
            "broker_margin": "%.4f" % margin_rate,
            "exchange_margin": "%.4f" % margin_rate,
            "limit_basic": ("%.4f" % limit_basic) if limit_basic else "",
            "multiplier": quote_mult,
            "as_of": date_str, "source": "交易所标准保证金(信查期货,自动抓取)", "note": note,
        })
        fee_rows.append({
            "sym": sym, "name": cname, "exchange": exchange, "account_flag": "投机",
            "multiplier": FEE_MULTIPLIERS[sym],
            "open_amt_rate": _fee(_num(r.get("open_fee_amt")), 7),
            "open_per_lot": _fee(_num(r.get("open_fee_qty")) + ADD_ONE_FEN, 2),
            "close_amt_rate": _fee(_num(r.get("offset_fee_amt")), 7),
            "close_per_lot": _fee(_num(r.get("offset_fee_qty")) + ADD_ONE_FEN, 2),
            "today_amt_rate": _fee(_num(r.get("short_offset_fee_amt")), 7),
            "today_per_lot": _fee(_num(r.get("short_offset_fee_qty")) + ADD_ONE_FEN, 2),
            "as_of": date_str,
        })
    return margin_rows, fee_rows


def by_sym_of(raw_rows, sym):
    return [r for r in raw_rows if r.get("variety_id") == sym]


def _fee(v, nd):
    """格式化费率/费用：接近 0 显示 0.0，否则 ≤6 位有效数字（科学计数保留小数位）。"""
    if v is None or v == 0:
        return "0.0"
    return "%.6g" % v


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def main():
    ap = argparse.ArgumentParser(description="信查期货 API → futures_margins/fees CSV")
    ap.add_argument("--date", default=datetime.now().strftime("%Y%m%d"), help="交易日 yyyyMMdd")
    ap.add_argument("--json", default=None, help="本地已落 JSON（离线）")
    ap.add_argument("--apply", action="store_true", help="正式覆盖 data/futures_*.csv（默认预览到 data/xcqihuo_preview/）")
    args = ap.parse_args()

    raw = load_raw(args.date, args.json)
    margin_rows, fee_rows = build_tables(raw, args.date)
    if args.apply:
        out_dir = ROOT / "data"
        (ROOT / "data" / "legacy_galaxy").mkdir(parents=True, exist_ok=True)
        for name in ("futures_margins.csv", "futures_fees.csv"):
            src = out_dir / name
            if src.exists():
                shutil.copy2(src, ROOT / "data" / "legacy_galaxy" / name)
    else:
        out_dir = ROOT / "data" / "xcqihuo_preview"

    write_csv(out_dir / "futures_margins.csv", MARGIN_FIELDS, margin_rows)
    write_csv(out_dir / "futures_fees.csv", FEE_FIELDS, fee_rows)
    print("wrote %d margin rows, %d fee rows -> %s" % (len(margin_rows), len(fee_rows), out_dir))
    if args.apply:
        print("legacy 备份: data/legacy_galaxy/")


if __name__ == "__main__":
    main()