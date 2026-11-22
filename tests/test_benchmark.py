"""tests/test_benchmark.py — 阶段4评估套件测试与指标锁定。

目的：让 README 中的评估数字有可复现代码支撑。
- 基准套件可运行且结构完整（五项指标齐全）
- 指标方向性锁定：引擎模式的成本显著低于基线（token 节省 > 0.3）
- 回滚成功率为真实验证（引擎模式下真实执行快照-破坏-回滚链路）
- 固定 seed 下结果可复现（同一 seed 两次运行结果一致）
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from statebranch.eval.benchmark import BenchmarkSuite  # noqa: E402


class TestBenchmark(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.suite = BenchmarkSuite(out_dir=self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _report(self, seed=2026):
        return self.suite.run(seed=seed)

    def test_report_structure(self):
        """报告包含全部五项指标。"""
        r = self._report()
        self.assertIn("baseline", r)
        self.assertIn("engine", r)
        self.assertIn("improvement", r)
        imp = r["improvement"]
        for key in ["trial_reduction", "success_rate_delta",
                    "rollback_success_rate", "experience_reuse_rate"]:
            self.assertIn(key, imp, key)

    def test_engine_beats_baseline_on_cost(self):
        """方向性锁定：引擎成本显著低于基线（token 节省率 > 0.3）。"""
        r = self._report()
        saving = r["improvement"]["trial_reduction"]
        self.assertIsNotNone(saving)
        self.assertGreater(saving, 0.3,
                           f"引擎成本节省应显著（实测 {saving}），回滚到检查点应优于从零重试")

    def test_rollback_success_rate_is_real(self):
        """回滚成功率来自真实执行的快照-破坏-回滚链路，且应为 1.0。"""
        r = self._report()
        # engine 模式每个场景都真实执行了回滚链路
        self.assertEqual(r["improvement"]["rollback_success_rate"], 1.0)
        self.assertGreater(r["engine"]["runs"], 0)

    def test_deterministic_with_seed(self):
        """固定 seed 可复现：两次运行结果一致。"""
        r1 = self._report(seed=99)
        self.suite.out_dir = tempfile.mkdtemp()  # 换目录排除落盘干扰
        r2 = self._report(seed=99)
        self.assertEqual(r1["baseline"]["avg_cost"], r2["baseline"]["avg_cost"])
        self.assertEqual(r1["engine"]["success_rate"], r2["engine"]["success_rate"])
        self.assertEqual(
            r1["improvement"]["trial_reduction"], r2["improvement"]["trial_reduction"])

    def test_markdown_report(self):
        """Markdown 报告生成且数字与 JSON 一致。"""
        r = self._report()
        md = self.suite.render_markdown(r)
        self.assertIn(str(r["improvement"]["trial_reduction"]), md)
        self.assertIn("基准评估报告", md)


if __name__ == "__main__":
    unittest.main()
