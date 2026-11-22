"""tests/test_trajectory.py — 轨迹密码学链式校验单元测试。

覆盖：
- 链式记录（prevHash / checksum）
- verify_trajectory() 完整链通过
- 篡改任一历史步骤后链断裂检测
- 步骤树构建
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from statebranch.core.trajectory import TrajectoryRecorder  # noqa: E402


class TestTrajectory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store_dir = os.path.join(self._tmp, "trajectory")
        self.rec = TrajectoryRecorder(self.store_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _append_n(self, n=3):
        for i in range(n):
            self.rec.append_step(
                intention=f"intent-{i}", decision=f"decision-{i}",
                tool_call="tool", tool_params={"i": i},
                tool_result=f"result-{i}", env_change=f"env-{i}")

    def test_chain_linking(self):
        """链式记录：每一步 prevHash 等于上一步 checksum。"""
        self._append_n(3)
        steps = self.rec.get_steps()
        self.assertEqual(steps[0].prev_hash, "")
        for i in range(1, len(steps)):
            self.assertEqual(steps[i].prev_hash, steps[i - 1].checksum)

    def test_verify_valid(self):
        """未篡改轨迹校验通过。"""
        self._append_n(4)
        self.assertTrue(self.rec.verify_trajectory())

    def test_tamper_step_detected(self):
        """篡改任一历史步骤后链断裂检测。"""
        self._append_n(3)
        steps = self.rec.get_steps()
        # 篡改中间步骤的工具结果
        steps[1].tool_result = "TAMPERED"
        self.assertFalse(self.rec.verify_trajectory())

    def test_tamper_first_step_detected(self):
        """篡改首步骤同样破坏整条链。"""
        self._append_n(3)
        steps = self.rec.get_steps()
        steps[0].intention = "evil-intent"
        self.assertFalse(self.rec.verify_trajectory())

    def test_reordering_detected(self):
        """步骤重排会被检测。"""
        self._append_n(3)
        steps = self.rec.get_steps()
        # 交换前两个步骤的决策字段
        steps[0].decision, steps[1].decision = steps[1].decision, steps[0].decision
        self.assertFalse(self.rec.verify_trajectory())

    def test_persistence(self):
        """轨迹持久化后仍可校验。"""
        self._append_n(2)
        # 重新加载（模拟进程重启）
        rec2 = TrajectoryRecorder(self.store_dir)
        self.assertEqual(len(rec2.get_steps()), 2)
        self.assertTrue(rec2.verify_trajectory())

    def test_build_tree(self):
        """步骤树构建。"""
        self.rec.append_step(intention="root", decision="d0", tool_call="t",
                             tool_result="r", env_change="e", parent_step_index=-1)
        self.rec.append_step(intention="child", decision="d1", tool_call="t",
                             tool_result="r", env_change="e", parent_step_index=0)
        tree = self.rec.build_tree()
        self.assertEqual(len(tree["roots"]), 1)
        self.assertEqual(tree["roots"][0]["intention"], "root")
        self.assertEqual(len(tree["roots"][0]["children"]), 1)


if __name__ == "__main__":
    unittest.main()
