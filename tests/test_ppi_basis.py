# -*- coding: utf-8 -*-
"""生意社基差抓取/解析回归（第126轮，纯函数零网络）。

页面按 2026-09-11 实测格式合成：每行8格 = 商品|现货价|最近月|最近价|现期差对(嵌套小表)|
主力月|主力价|现期差对(嵌套小表)；嵌套小表经 lxml extract_tables 提取后为粘连文本
（如 '1384.40%'），与生产一致。旧行内"RB2610 式完整合约代码"已从该站消失，不测。
"""
import re
from datetime import date, timedelta

import fundamental_data as fd


# ---------------- 合成页面（镜像真实 HTML 结构） ----------------

def _nest(gap, rate):
    color = "red" if gap > 0 else "green"
    return ('<td><table width="100%%"><tr><td width="50%%" align="center">'
            '<font color=%s>%d</font></td><td width="50%%" align="center">'
            '<font color=%s>%.2f%%</font></td></tr></table></td>' % (color, gap, color, rate))


def _row_html(name, spot, recent, recent_px, dom, dom_px):
    """一行8格：现货对最近合约、现货对主力合约两个现期差均为嵌套小表（与真实页一致）。"""
    gap1 = float(spot.replace(",", "")) - float(recent_px.replace(",", ""))
    gap2 = float(spot.replace(",", "")) - float(dom_px.replace(",", ""))
    r1 = abs(gap1) / float(spot.replace(",", "")) * 100
    r2 = abs(gap2) / float(dom_px.replace(",", "")) * 100
    return ('<tr align="center" bgcolor="#fafdff">'
            '<td><a href="https://www.100ppi.com/sf/927.html">%s</a></td>'
            '<td>%s&nbsp;</td><td>%s&nbsp;</td><td>%s&nbsp;</td>%s'
            '<td>%s&nbsp;</td><td>%s&nbsp;</td>%s</tr>'
            % (name, spot, recent, recent_px, _nest(gap1, r1), dom, dom_px, _nest(gap2, r2)))


def _page(rows_html):
    trs = ['<tr align="center"><td>商品</td><td colspan="1">现货</td>'
           '<td colspan="2">最近合约</td><td colspan="3">主力合约</td></tr>']
    trs.extend(rows_html)
    filler = "生意社商品现货与期货价格对比 " * 400        # 撑过生产 8KB 数据页阈值
    return ('<html><head><meta charset="utf-8"><title>生意社：商品现货与期货价格对比表</title></head>'
            '<body><div class="nav">%s</div><table width="1000">%s</table></body></html>'
            % (filler, "".join(trs)))


PAGE = _page([
    _row_html("螺纹钢", "3138.00", "2609", "3000", "2701", "3108"),
    _row_html("天然橡胶", "14500", "2609", "14600", "2701", "14650"),   # 别名 -> RU，现货贴水
    _row_html("菜籽油OI", "9300", "2609", "9100", "2701", "9050"),      # 尾缀交易所代码 -> OI
    _row_html("纯苯", "7000", "2605", "6900", "2606", "6950"),          # 项目未覆盖品种 -> 诚实跳过
    _row_html("线材", "3600", "2601", "3500", "2601", "3500"),          # 同上
])


# ---------------- 解析 ----------------

def test_parse_current_format_values():
    out = fd.parse_ppi_basis_html(PAGE)
    assert set(out) == {"RB", "RU", "OI"}
    assert abs(out["RB"] - (3138.0 / 3108.0 - 1.0)) < 1e-12
    assert out["RU"] < 0                                     # 现货贴水为负（现货/主力-1）
    assert abs(out["OI"] - (9300.0 / 9050.0 - 1.0)) < 1e-12


def test_parse_skips_nested_rows_and_unmapped():
    # 嵌套小表行（首格数字）与未映射品种不得出现在结果里
    out = fd.parse_ppi_basis_html(PAGE)
    assert "纯苯" not in out and "线材" not in out
    assert all(isinstance(v, float) and abs(v) < 0.5 for v in out.values())


def test_parse_challenge_and_empty_pages_none():
    assert fd.parse_ppi_basis_html("") is None
    assert fd.parse_ppi_basis_html(None) is None
    # HW_CHECK 挑战页（636B 量级、无表格）
    assert fd.parse_ppi_basis_html('<html><script>var HW_CHECK=1;</script></html>') is None
    # 周末空页（有导航表格但无产品行）
    assert fd.parse_ppi_basis_html(
        '<html><table><tr><td>导航</td></tr><tr><td>首页</td></tr></table></html>') is None


def test_parse_missing_dominant_pair_skipped():
    # 只有最近合约、无主力月/主力价的行 -> 无法算对主力基差，诚实跳过
    row = ('<tr align="center"><td>螺纹钢</td><td>3138.00</td><td>2609</td><td>3000</td>'
           '<td><table width="100%"><tr><td>138</td><td>4.40%</td></tr></table></td></tr>')
    assert fd.parse_ppi_basis_html('<html><table>%s</table></html>' % row) is None


def test_parse_multiline_and_commas():
    # 数字含千分位逗号 / 品种名两侧空白：碳酸锂映射 LC（VARIETIES 原生名）
    row = _row_html("碳酸锂", "143,480", "2701", "140,000", "2701", "141,000").replace("碳酸锂", " 碳酸锂 ")
    out = fd.parse_ppi_basis_html('<html><table>%s</table></html>' % row)
    assert set(out) == {"LC"}
    assert abs(out["LC"] - (143480.0 / 141000.0 - 1.0)) < 1e-12


# ---------------- 品种名映射 ----------------

def test_sym_mapping_exact_alias_and_suffix():
    assert fd._ppi_sym("螺纹钢") == "RB"
    assert fd._ppi_sym("天然橡胶") == "RU"        # 别名
    assert fd._ppi_sym("石油沥青") == "BU"
    assert fd._ppi_sym("热轧卷板") == "HC"
    assert fd._ppi_sym("聚乙烯") == "L"
    assert fd._ppi_sym("菜籽油OI") == "OI"        # 尾缀交易所代码剥离
    assert fd._ppi_sym("甲醇MA") == "MA"
    assert fd._ppi_sym("纯苯") is None            # 该站特有、项目未覆盖
    assert fd._ppi_sym("上海期货交易所") is None   # 分组行
    assert fd._ppi_sym("-410") is None            # 嵌套行/噪音


def test_ppi_page_ok_predicate():
    assert not fd.ppi_page_ok(None)
    assert not fd.ppi_page_ok("")                  # 空文本
    assert not fd.ppi_page_ok("<html>HW_CHECK challenge</html>")
    assert not fd.ppi_page_ok("<tr>a</tr>" * 3)    # 行数过少
    assert fd.ppi_page_ok(PAGE)                    # 正常数据页
    assert fd.ppi_page_ok("<tr>x</tr>" * 10 + "x" * 9000)  # 体量过阈值的普通页


# ---------------- basis_table 编排（抓取函数打桩，零网络） ----------------

def test_basis_table_walkback_to_last_trading_day(monkeypatch):
    f = fd.FundamentalFetcher()
    today = date.today()
    calls = []

    def fake_fetch(ds):
        calls.append(ds)
        return PAGE if ds == (today - timedelta(days=1)).strftime("%Y-%m-%d") else None

    monkeypatch.setattr(fd.FundamentalFetcher, "_fetch_ppi_page", staticmethod(fake_fetch))
    out = f.basis_table()
    assert out and "RB" in out
    assert calls[0] == today.strftime("%Y-%m-%d")   # 先试当日
    assert len(calls) == 2                          # 当日未发布 -> 回退一天命中
    # 结果缓存到请求日：同实例再次调用不再发请求
    out2 = f.basis_table()
    assert out2 == out and len(calls) == 2


def test_basis_table_same_day_hit_no_walkback(monkeypatch):
    f = fd.FundamentalFetcher()
    today = date.today().strftime("%Y-%m-%d")
    calls = []
    monkeypatch.setattr(fd.FundamentalFetcher, "_fetch_ppi_page",
                        staticmethod(lambda ds: (calls.append(ds), PAGE)[1]))
    out = f.basis_table(date.today())
    assert "RB" in out and calls == [today]


def test_basis_table_all_days_missing_none(monkeypatch):
    f = fd.FundamentalFetcher()
    monkeypatch.setattr(fd.FundamentalFetcher, "_fetch_ppi_page", staticmethod(lambda ds: None))
    assert f.basis_table() is None


def test_basis_table_str_day_param(monkeypatch):
    f = fd.FundamentalFetcher()
    seen = []

    def fake_fetch(ds):
        seen.append(ds)
        return PAGE if ds == "2026-09-11" else None   # 周五有数据、周六(请求日)无

    monkeypatch.setattr(fd.FundamentalFetcher, "_fetch_ppi_page", staticmethod(fake_fetch))
    out = f.basis_table("2026-09-12")
    assert out and "RB" in out and seen == ["2026-09-12", "2026-09-11"]


def test_fetch_ppi_page_rejects_challenge(monkeypatch):
    # 打桩 http.get：返回挑战页 -> _fetch_ppi_page 返回 None；返回数据页 -> 原文透传
    class _Resp:
        status_code = 200
        content = '<html>HW_CHECK</html>'.encode("utf-8")
        encoding = "utf-8"

    class _Resp2:
        status_code = 200
        content = PAGE.encode("utf-8")
        encoding = "utf-8"

    import fundamental_data
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        seen["kw"] = kw
        return _Resp2 if "2026-09-11" in url else _Resp

    monkeypatch.setattr(fundamental_data.http, "get", fake_get)
    assert fd.FundamentalFetcher._fetch_ppi_page("2026-09-12") is None
    assert fd.FundamentalFetcher._fetch_ppi_page("2026-09-11") == PAGE
    assert "impersonate" in seen["kw"] and seen["kw"]["impersonate"] == "chrome"
    assert seen["kw"]["source"] == "100ppi"
