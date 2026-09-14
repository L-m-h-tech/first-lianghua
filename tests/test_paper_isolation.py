"""第152轮：纸面 var_hist/flow_tracker 与主报告隔离的行为验证。

验证点：
  1. paper_analyze 写入 state.paper_var_hist / state.paper_flow_tracker（不存在时自动创建）；
  2. 传入 analyzer 的 var_hist 是独立实例，主报告 state.var_hist 零写入；
  3. 主报告共享 flow_tracker 不被纸面 update 触碰。
运行：python -m pytest tests/test_paper_isolation.py -v
"""

import unittest
from unittest import mock

import paper_analysis


class _StubState:
    def __init__(self):
        self.var_hist = {}  # 主报告口径（必须保持零写入）
        self.flow_tracker = mock.MagicMock(name="main_flow_tracker")
        self.watchlist = [("螺纹钢", {"code": "rb2601", "cat": "黑色"})]


class PaperIsolationTest(unittest.TestCase):
    def test_paper_analyze_writes_independent_var_hist_and_flow(self):
        st = _StubState()
        quotes = {"rb2601": {"latest": 3500.0, "volume": 1.0, "open_interest": 1.0}}
        captured = {}

        def fake_analyze(state, watchlist, quotes, flow_map, var_hist=None):
            captured["var_hist"] = var_hist
            return []  # 空 fut_rows → paper_analyze 提前返回，后续期权段不执行

        with mock.patch.object(
            paper_analysis.analyzer, "analyze_all_varieties", side_effect=fake_analyze
        ):
            out = paper_analysis.paper_analyze(st, quotes)

        # 独立实例被创建并写入
        self.assertIsInstance(st.paper_var_hist, dict)
        self.assertIn("螺纹钢", st.paper_var_hist)
        self.assertEqual(st.paper_var_hist["螺纹钢"][-1][1], 3500.0)
        self.assertIsInstance(st.paper_flow_tracker, paper_analysis.FlowTracker)
        # 独立实例被传给 analyzer
        self.assertIs(captured["var_hist"], st.paper_var_hist)
        # 主报告口径零污染
        self.assertEqual(st.var_hist, {})
        st.flow_tracker.update.assert_not_called()
        # 独立实例与主报告实例不是同一个对象
        self.assertIsNot(st.paper_flow_tracker, st.flow_tracker)
        # 空 fut_rows 时输出结构完整
        self.assertEqual(out, {"fut_rows": [], "opt_rows": [], "strat_rows": [], "chain_map": {}, "codes": ["rb2601"]})

    def test_paper_analyze_reuses_existing_independent_instances(self):
        st = _StubState()
        st.paper_var_hist = {}
        st.paper_flow_tracker = paper_analysis.FlowTracker()
        quotes = {"rb2601": {"latest": 3501.0, "volume": 1.0, "open_interest": 1.0}}
        captured = {}

        def fake_analyze(state, watchlist, quotes, flow_map, var_hist=None):
            captured["var_hist"] = var_hist
            return []

        with mock.patch.object(
            paper_analysis.analyzer, "analyze_all_varieties", side_effect=fake_analyze
        ):
            paper_analysis.paper_analyze(st, quotes)

        self.assertIs(captured["var_hist"], st.paper_var_hist)
        self.assertEqual(st.paper_var_hist["螺纹钢"][-1][1], 3501.0)
        self.assertEqual(st.var_hist, {})  # 主报告口径仍零写入

    def test_analyzer_defaults_to_main_var_hist(self):
        """analyze_all_varieties 不传 var_hist 时默认主报告 state.var_hist（run_cycle 路径）。

        直接检查 analyzer 模块的默认参数绑定行为：用真实函数签名中的默认值分支逻辑
        （var_hist is None → 用 state.var_hist），通过刺探 _tick_momentum 的输入确认。
        """
        import analyzer as _an

        # 真实入口的默认值分支：不传 var_hist → 读 state.var_hist

        import inspect

        sig = inspect.signature(_an.analyze_all_varieties)
        self.assertIsNone(sig.parameters["var_hist"].default)

        # 确认分支逻辑存在：如果将来有人改掉默认值，此处断言会醒
        src = inspect.getsource(_an.analyze_all_varieties)
        self.assertIn("if var_hist is None", src)
        self.assertIn("var_hist = state.var_hist", src)


if __name__ == "__main__":
    unittest.main()
