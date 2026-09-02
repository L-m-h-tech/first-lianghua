# -*- coding: utf-8 -*-
r"""半自动构建 64 品种保证金率表 data/futures_margins.csv（第16轮 WP-E）。

主数据源（用户开户公司 = 银河期货，最贴近实盘占用）：官网"结算时起各品种最新交易保证金比例"
  例 https://www.yhqh.com.cn/col71/27539.html（2026-08-28 结算时起）
  页面为静态 table，requests + pandas.read_html 直接解析；列为
  交易所 | 代码 | 品种 | 板幅% | 公司保证金比例(投机/套保)；只取【纯字母代码的品种基础档+投机档】，
  剔除 cu2609-2702 等近月上浮行、l_f 等月均价行（近月上浮写入 note 提示）。
  文章ID每期变化，可用 --url 指到最新页，或浏览器"另存为"后用 --html 离线解析。

对照/排除记录（勿重复踩坑）：
- 国泰君安日历表 https://www.gtjaqh.com/pc/calendar 亦可解析、64品种全覆盖，曾作首版来源；
  但各期货公司加收不同（如 AG 国君30%/银河35%、RB 国君11%/银河12%），以用户本公司银河为准；
- akshare futures_rule_em 的东财 GetPZJYInfo 三个域名均返回 HTML 壳（接口已变更），不逆向；
- 交易所官网 SHFE dailystock 404、DCE WAF 412（第13轮验证），无干净免费"交易所基准档"单源，
  故 exchange_margin 列保留留空、绝不编造；回测用公司档更保守。
- 合约乘数银河页不提供：内置 QUOTE_MULTIPLIERS（每手报价单位个数口径，鸡蛋报价元/500kg、
  1手=10个报价单位故=10，与手续费表按吨记的5不同；其余品种两口径一致），并与 futures_fees.csv
  交叉校验。乘数几乎不变，沿用 build_fee_table.py 的硬编码维护模式。

仅本维护工具用 requests+pandas；portfolio.py 运行时只读标准库CSV，不新增运行依赖。
用法（项目根目录）：
  D:\Python\python.exe tools\build_margin_table.py --url https://www.yhqh.com.cn/col71/27539.html --date 20260828
  D:\Python\python.exe tools\build_margin_table.py --html 页面另存.html --date 20260828
"""
from pathlib import Path
import argparse
import csv
import io
import re
import sys
import warnings

import requests

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

DEFAULT_URL = "https://www.yhqh.com.cn/col71/27539.html"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
FIELDS = ["sym", "name", "exchange", "broker_margin", "exchange_margin",
          "limit_basic", "multiplier", "as_of", "source", "note"]
CODE_RE = re.compile(r"^[A-Za-z]+$")

# 每手"报价单位个数"口径乘数（名义价值=盘面价×该乘数）。除鸡蛋外与 build_fee_table 物理口径一致。
QUOTE_MULTIPLIERS = {
    # SHFE
    "RB": 10, "HC": 10, "SS": 5, "CU": 5, "AL": 5, "AO": 20, "ZN": 5, "PB": 5,
    "NI": 1, "SN": 1, "AU": 1000, "AG": 15, "RU": 10, "BR": 5, "FU": 10,
    "BU": 10, "SP": 10,
    # INE
    "SC": 1000, "NR": 10, "LU": 10, "BC": 5, "EC": 50,
    # DCE（JD 报价元/500千克，1手5吨=10个500千克 → 10；fees表按吨记5）
    "A": 10, "B": 10, "M": 10, "Y": 10, "P": 10, "C": 10, "CS": 10, "RR": 10,
    "JD": 10, "LH": 16, "LG": 90, "L": 5, "V": 5, "PP": 5, "EG": 10, "EB": 5,
    "PG": 20, "J": 100, "JM": 60, "I": 100,
    # CZCE
    "SR": 10, "CF": 5, "CY": 5, "TA": 5, "MA": 10, "PX": 5, "PF": 5,
    "PR": 15, "SH": 30, "FG": 20, "SA": 20, "UR": 20, "RM": 10, "OI": 10,
    "PK": 5, "AP": 10, "CJ": 5, "SF": 5, "SM": 5,
    # GFEX
    "SI": 5, "LC": 1, "PS": 3,
}
# 与手续费表乘数的已知口径差异白名单：{sym: (fees口径, 报价口径, 说明)}
MULT_KNOWN_DIFF = {"JD": (5.0, 10.0, "报价元/500kg,1手=10个报价单位;fees表5为吨口径")}


def _num(x):
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return None


def parse_yinhe_html(html_text):
    """解析银河期货保证金页，返回 {sym_upper: {margin, hedge, limit_basic, cname}}。"""
    import pandas as pd
    dfs = pd.read_html(io.StringIO(html_text))
    if not dfs:
        raise RuntimeError("页面未解析到 table")
    df = dfs[0]
    out = {}
    for _, row in df.iterrows():
        code = str(row[1]).strip()
        if not CODE_RE.fullmatch(code):       # 只留品种基础档（剔除近月/月均价）
            continue
        sym = code.upper()
        spec = _num(row[4])                   # 投机档
        hedge = _num(row[5])                  # 套保档
        limit_b = _num(row[3])
        if spec is None:
            continue
        out[sym] = {"margin": spec / 100.0,
                    "hedge": hedge / 100.0 if hedge is not None else None,
                    "limit_basic": limit_b / 100.0 if limit_b is not None else None,
                    "cname": str(row[2]).strip()}
    return out


def fetch_html(url=None, html_path=None):
    if html_path:
        return Path(html_path).read_text(encoding="utf-8", errors="ignore")
    r = requests.get(url or DEFAULT_URL, headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def convert(url, html_path, date, out: Path, source_name="银河期货"):
    cfg_syms = {m["sym"] for m in config.VARIETIES.values()}
    assert set(QUOTE_MULTIPLIERS) == cfg_syms, \
        f"内置乘数品种与config不一致: 缺{sorted(cfg_syms - set(QUOTE_MULTIPLIERS))} " \
        f"多{sorted(set(QUOTE_MULTIPLIERS) - cfg_syms)}"
    parsed = parse_yinhe_html(fetch_html(url, html_path))
    missing = sorted(s for s in cfg_syms if s not in parsed)
    if missing:
        raise RuntimeError(f"银河保证金页缺失品种 {missing}（检查页面是否为最新/代码是否改名）")

    # 手续费表乘数交叉校验
    fee_mult = {}
    fee_path = ROOT / "data" / "futures_fees.csv"
    if fee_path.exists():
        with fee_path.open("r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                fee_mult[r["sym"].strip().upper()] = float(r["multiplier"])
    warn = []
    rows = []
    for cname, meta in config.VARIETIES.items():
        sym = meta["sym"]
        info = parsed[sym]
        mult = float(QUOTE_MULTIPLIERS[sym])
        if sym in MULT_KNOWN_DIFF:
            fee_m, q_m, why = MULT_KNOWN_DIFF[sym]
            if abs(fee_mult.get(sym, -1) - fee_m) > 1e-9 or abs(q_m - mult) > 1e-9:
                warn.append(f"{sym}: 白名单口径异常 内置{mult}/费率{fee_mult.get(sym)} 预期{q_m}/{fee_m}")
            notes = [why]
        else:
            if sym in fee_mult and abs(fee_mult[sym] - mult) > 1e-9:
                warn.append(f"{sym}: 内置乘数{mult} != 费率表{fee_mult[sym]}（非白名单，需人工核对）")
            notes = []
        if info["hedge"] is not None and abs(info["hedge"] - info["margin"]) > 1e-9:
            notes.append(f"套保档{info['hedge'] * 100:.0f}%")
        notes.append("近月/交割月保证金上浮见公司最新通知")
        rows.append({
            "sym": sym, "name": cname, "exchange": meta["ex"],
            "broker_margin": f"{info['margin']:.4f}", "exchange_margin": "",
            "limit_basic": f"{info['limit_basic']:.4f}" if info["limit_basic"] is not None else "",
            "multiplier": int(mult), "as_of": date,
            "source": f"{source_name}交易保证金比例(投机档)", "note": "；".join(notes),
        })

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return len(rows), warn, parsed


def main():
    p = argparse.ArgumentParser(description="构建64品种期货保证金率CSV（默认银河期货投机档）")
    p.add_argument("--url", default=DEFAULT_URL, help="银河期货保证金比例页面URL")
    p.add_argument("--html", default="", help="本地另存的页面HTML（断网/URL变动时用，优先于--url）")
    p.add_argument("--date", default="20260828", help="生效结算日 YYYYMMDD")
    p.add_argument("--out", default=str(ROOT / "data" / "futures_margins.csv"))
    args = p.parse_args()
    n, warn, parsed = convert(args.url, args.html or None, args.date, Path(args.out))
    print(f"wrote {args.out} rows={n}（银河期货投机档，as_of={args.date}）")
    # 打印保证金分布供人工过目
    vals = sorted((v["margin"] for v in parsed.values()))
    print(f"页面解析品种 {len(parsed)} 个；保证金率区间 {vals[0]*100:.0f}%~{vals[-1]*100:.0f}%")
    if warn:
        print("乘数交叉校验警告：")
        for w in warn:
            print("  " + w)
    else:
        print("乘数与 futures_fees.csv 交叉校验通过（JD 为已知报价口径差异）")


if __name__ == "__main__":
    main()
