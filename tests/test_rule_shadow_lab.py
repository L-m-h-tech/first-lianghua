# -*- coding: utf-8 -*-
"""Phase2 规则影子实验室回归（第131轮）：引擎语义（R3封锁/解除、R4延迟、默认关=基线）零网络合成断言。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))


def test_shadow_lab_selftest():
    import rule_shadow_lab as RL
    assert RL.selftest() == 0
