"""基本面四子因子与缺项权重重归一回归（第13轮 WP-C，纯函数零网络）。"""

import config
import fundamental_factors as ff


def _series(stocks):
    return [{"date": "2026-08-%02d" % (i + 1), "stock": s} for i, s in enumerate(stocks)]


def test_inventory_low_destock_bullish():
    # 库存一路去化、当前为区间最低 -> 低分位+去库 -> 偏多
    stocks = [100 - i for i in range(20)]  # 末值81为最低
    score, detail = ff.inventory_factor(_series(stocks))
    assert score > 0.5
    assert detail["pct"] <= 0.1 and detail["wow"] < 0
    assert detail["n"] == 20


def test_inventory_high_restock_bearish():
    stocks = [80 + i for i in range(20)]  # 末值99为最高、累库
    score, detail = ff.inventory_factor(_series(stocks))
    assert score < -0.5 and detail["pct"] >= 0.95 and detail["wow"] > 0


def test_inventory_insufficient_samples():
    assert ff.inventory_factor(_series([10, 9, 8])) is None
    assert ff.inventory_factor(None) is None


def test_rank_factor():
    s, d = ff.rank_factor(7000, 3000)  # 净多率40%
    assert s > 0 and abs(d["net"] - 0.4) < 1e-9
    # 较昨日净多率回升 -> 分更高
    s2, _ = ff.rank_factor(7000, 3000, 6000, 4000)  # 昨净多20%
    assert s2 > s
    # 净空占优 -> 负分
    s3, _ = ff.rank_factor(3000, 7000)
    assert s3 < 0
    assert ff.rank_factor(0, 0) is None


def test_carry_factor():
    s, detail = ff.carry_factor({"annual_carry": 0.15, "shape": "Back"})
    assert s > 0 and detail["annual_carry"] == 0.15
    s2, _ = ff.carry_factor(-0.15)  # Contango 远月更贵 -> 偏空
    assert s2 < 0
    assert ff.carry_factor(None) is None
    assert ff.carry_factor({"shape": "x"}) is None  # 缺 annual_carry


def test_basis_factor():
    assert ff.basis_factor(0.05)[0] > 0
    assert ff.basis_factor(-0.05)[0] < 0
    assert ff.basis_factor(None) is None


def test_build_fundamental_all_missing_none():
    assert ff.build_fundamental() is None


def test_build_weight_renormalize_on_missing():
    inv = ff.inventory_factor(_series([100 - i for i in range(20)]))
    # 只给库存一项：权重应按可得项归一（等价于全权重压在库存上），不放大超界
    out = ff.build_fundamental(inv=inv)
    assert out is not None
    assert set(out["parts"]) == {"库存仓单"}
    assert abs(out["raw"] - ff._clamp(inv[0])) < 1e-9
    assert abs(out["score"]) <= config.FUND_MAX_SCORE + 1e-9
    assert "基本面偏多" in out["note"]


def test_build_fundamental_combined_and_tone():
    rank = ff.rank_factor(8000, 2000)
    carry = ff.carry_factor(0.2)
    basis = ff.basis_factor(0.05)
    out = ff.build_fundamental(rank=rank, carry=carry, basis=basis)  # 缺库存
    avail_w = config.FUND_RANK_WEIGHT + config.FUND_CARRY_WEIGHT + config.FUND_BASIS_WEIGHT
    expected = (
        config.FUND_RANK_WEIGHT * ff._clamp(rank[0])
        + config.FUND_CARRY_WEIGHT * ff._clamp(carry[0])
        + config.FUND_BASIS_WEIGHT * ff._clamp(basis[0])
    ) / avail_w
    assert abs(out["raw"] - expected) < 1e-9
    assert set(out["parts"]) == {"龙虎榜", "期限carry", "基差"}


# ---- 第145轮 #9：PIT 对齐（数据日 as_of + 入库 trade_date + asof 查询） ----


def _inv_series(n=16, last="2026-09-11"):
    """构造 n 个升序样本，末个日期为 last。"""
    import datetime as _dt

    base = _dt.date.fromisoformat(last)
    return [
        {
            "date": (base - _dt.timedelta(days=n - 1 - i)).isoformat(),
            "stock": 100.0 - i,
            "chg": -1.0,
        }
        for i in range(n)
    ]


def test_build_fundamental_as_of_from_inventory():
    """build_fundamental 的 as_of 取自子项最晚数据日期（库存 last_date）。"""
    import fundamental_factors as ff

    inv = ff.inventory_factor(_inv_series())
    assert inv is not None
    rank = ff.rank_factor(8000, 2000)
    out = ff.build_fundamental(inv=inv, rank=rank)
    assert out["as_of"] == "2026-09-11"  # 库存数据最晚日期


def test_build_fundamental_as_of_none_when_no_dates():
    """无任何子项带日期（纯 carry+龙虎数值）时 as_of 为 None（消费端回退采集日）。"""
    import fundamental_factors as ff

    rank = ff.rank_factor(8000, 2000)
    carry = ff.carry_factor(0.2)
    out = ff.build_fundamental(rank=rank, carry=carry)
    assert out is not None
    assert out["as_of"] is None


def test_build_fundamental_as_of_uses_max_across_subs():
    """多个子项带日期时取最晚者。"""
    import fundamental_factors as ff

    inv = ff.inventory_factor(_inv_series(last="2026-09-10"))
    assert inv is not None
    rank = ff.rank_factor(8000, 2000)
    basis = ff.basis_factor(0.05)
    out = ff.build_fundamental(inv=inv, rank=rank, basis=basis)
    assert out["as_of"] == "2026-09-10"
