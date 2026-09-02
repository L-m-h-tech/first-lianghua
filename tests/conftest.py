# -*- coding: utf-8 -*-
"""pytest 公共夹具与导入路径（P1-1 回归测试体系）。

纪律（与《统一改进路线图》3.1 一致）：
  - 全部用例零网络、确定性、可重复；涉及交易日历的一律用 flat_calendar 注入，
    绝不触发 trade_calendar 的新浪/东财动态拉取；
  - 需要 SQLite 的用 tmp_db（tmp_path 下临时文件，测完即弃），不碰生产 data/monitor.db；
  - 生产 requirements.txt 不含 pytest，pytest 只是 dev 侧工具。
"""
import os
import sys
from datetime import timedelta

import pytest

# 项目根目录（tests 的上一级）与 tools 目录加入 sys.path，测试里可直接 import 生产模块
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
for p in (ROOT, TOOLS):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def flat_calendar(monkeypatch):
    """注入确定性交易日历：周一~周五为交易日、无任何法定节假日；
    夜盘仅在周一~周四晚开启（与现实一致：周五晚无夜盘、周末凌晨不延续）。

    同时 patch trade_calendar 模块与 utils 内已绑定的引用，保证零网络。
    """
    import trade_calendar
    import utils

    def is_trade_day(d=None):
        from datetime import datetime as _dt
        d = d or _dt.now().date()
        return d.weekday() < 5

    def has_night_session(d=None):
        from datetime import datetime as _dt
        d = d or _dt.now().date()
        return d.weekday() in (0, 1, 2, 3)   # 周一~周四晚有夜盘

    def prev_trade_day(d, max_step=15):
        for _ in range(max_step):
            d = d - timedelta(days=1)
            if d.weekday() < 5:
                return d
        return d

    def next_trade_day(d, max_step=15):
        for _ in range(max_step):
            d = d + timedelta(days=1)
            if d.weekday() < 5:
                return d
        return d

    fakes = {"is_trade_day": is_trade_day, "has_night_session": has_night_session,
             "prev_trade_day": prev_trade_day, "next_trade_day": next_trade_day}
    for name, fn in fakes.items():
        monkeypatch.setattr(trade_calendar, name, fn)
        monkeypatch.setattr(utils.trade_calendar, name, fn, raising=False)
    return fakes


@pytest.fixture
def tmp_db(tmp_path):
    """在临时目录构建一个全新 MonitorDB（9 张表全建），测试结束关闭，绝不触碰生产库。"""
    import storage
    db = storage.MonitorDB(str(tmp_path / "test_monitor.db"))
    yield db
    db.close()
