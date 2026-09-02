# -*- coding: utf-8 -*-
"""全量生产模块语法编译回归：任何一个 .py 语法损坏都会让套件变红（确定性、零导入副作用）。"""
import glob
import os
import py_compile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _all_production_py():
    files = glob.glob(os.path.join(ROOT, "*.py"))
    files += glob.glob(os.path.join(ROOT, "tools", "*.py"))
    return sorted(files)


@pytest.mark.parametrize("path", _all_production_py(), ids=lambda p: os.path.basename(p))
def test_production_module_compiles(path, tmp_path):
    cfile = tmp_path / (os.path.basename(path) + "c")
    py_compile.compile(path, cfile=str(cfile), doraise=True)


def test_production_module_count_sanity():
    # 防止 glob 路径写错导致“假全绿”：生产模块数量应在合理下限以上
    assert len(_all_production_py()) >= 30
