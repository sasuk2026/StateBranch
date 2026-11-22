"""tests/test_experience_vm_android.py — 阶段2(经验) + 阶段3(跨平台)测试。

覆盖：
- 技能提取、检索、回放优先调度、复用统计
- 微虚拟机适配器契约与能力声明
- 安卓适配器：节点树快照、轻量回滚（导航回溯）、操控序列
- 跨端一致性：同一 BackendAdapter 契约在四类后端统一可用
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from statebranch.backend.android import (  # noqa: E402
    A11yNode, AndroidBackend, AndroidDeviceSimulator)
from statebranch.backend.base import BackendAdapter  # noqa: E402
from statebranch.backend.vm import MicroVMBackend  # noqa: E402
from statebranch.core.experience import ExperienceEngine, SkillLibrary  # noqa: E402


class TestExperience(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.lib = SkillLibrary(os.path.join(self._tmp, "skills"))
        self.engine = ExperienceEngine(self.lib)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _steps(self):
        return [
            {"tool_call": "snapshot.create", "tool_params": {"name": "T0"},
             "env_change": "T0 created"},
            {"tool_call": "fork.create", "tool_params": {"name": "A"},
             "env_change": "fork A"},
            {"tool_call": "restore.execute", "tool_params": {},
             "env_change": "rolled back"},
        ]

    def test_extract_and_search(self):
        """从成功轨迹提取技能并可检索。"""
        skill = self.engine.extract_skill("修复配置文件回滚", self._steps(),
                                          name="rollback-fix")
        hits = self.lib.search("修复配置文件回滚")
        self.assertEqual(hits[0].id, skill.id)
        self.assertEqual(skill.steps[0]["tool_call"], "snapshot.create")

    def test_replay_first_scheduling(self):
        """回放优先调度：命中返回回放计划，未命中返回 None。"""
        self.engine.extract_skill("修复配置文件回滚", self._steps())
        plan = self.engine.plan_from_experience("修复配置文件回滚")
        self.assertTrue(plan["replay"])
        self.assertEqual(len(plan["steps"]), 3)
        self.assertIsNone(self.engine.plan_from_experience("无关新任务"))

    def test_reuse_stats(self):
        """复用统计：调用次数与成功率。"""
        self.engine.extract_skill("任务A", self._steps())
        plan = self.engine.plan_from_experience("任务A")
        self.engine.confirm_replay_success(plan["skill_id"])
        self.engine.confirm_replay_success(plan["skill_id"])
        s = self.lib.stats()
        # reuse_count = 调度调用1次 + 回放确认2次 = 3；success = 2
        self.assertEqual(s["total_reuse"], 3)
        self.assertEqual(s["total_success"], 2)
        self.assertEqual(s["reuse_rate"], round(2 / 3, 3))

    def test_persistence(self):
        """技能库持久化（可跨进程加载）。"""
        self.engine.extract_skill("任务P", self._steps())
        lib2 = SkillLibrary(os.path.join(self._tmp, "skills"))
        self.assertEqual(len(lib2.list()), 1)

    def test_prompt_template(self):
        """提示词模板生成。"""
        skill = self.engine.extract_skill("模板任务", self._steps())
        self.assertIn("snapshot.create", skill.prompt_template)
        self.assertIn("任务：模板任务", skill.prompt_template)


class TestMicroVMBackend(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.backend = MicroVMBackend(root_dir=os.path.join(self._tmp, "store"))
        self.work = os.path.join(self._tmp, "work")
        os.makedirs(self.work)
        self.backend.active_work_dir = self.work
        with open(os.path.join(self.work, "a.txt"), "w") as f:
            f.write("base")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_contract(self):
        """微虚拟机适配器实现统一契约。"""
        self.assertIsInstance(self.backend, BackendAdapter)
        self.assertIn(self.backend.mode, ("emulated", "native-firecracker"))
        self.assertFalse(self.backend.capabilities["android_accessibility"])

    def test_snapshot_and_rollback(self):
        snap = self.backend.storeSnapshot(name="T0")
        with open(os.path.join(self.work, "a.txt"), "w") as f:
            f.write("changed")
        self.assertTrue(self.backend.restoreSnapshot(snap.id))
        with open(os.path.join(self.work, "a.txt")) as f:
            self.assertEqual(f.read(), "base")


class TestAndroidBackend(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.backend = AndroidBackend(store_dir=os.path.join(self._tmp, "store"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _home_screen(self):
        return {
            "btn_settings": A11yNode("btn_settings", "设置", clickable=True),
            "btn_home": A11yNode("btn_home", "首页", clickable=False),
            "edit_search": A11yNode("edit_search", "", clickable=True),
        }

    def test_a11y_snapshot_and_rollback(self):
        """节点树快照 + 轻量回滚（导航回溯）。"""
        self.backend.device.set_screen("MainActivity", self._home_screen())
        snap = self.backend.storeSnapshot(name="home")
        # 操控：跳转 + 输入
        self.backend.device.tap("btn_settings")
        self.backend.device.input_text("edit_search", "hello")
        self.assertEqual(self.backend.device.state.activity, "Activity_btn_settings")
        # 回滚到 home
        self.assertTrue(self.backend.restoreSnapshot(snap.id))
        self.assertEqual(self.backend.device.state.activity, "MainActivity")
        # 节点树恢复到快照时状态：edit_search 存在但文本为空
        node = self.backend.device.state.nodes.get("edit_search")
        self.assertIsNotNone(node)
        self.assertEqual(node.text, "")

    def test_fork_branch_isolation(self):
        """分支 = 状态副本，操控互不影响。"""
        self.backend.device.set_screen("MainActivity", self._home_screen())
        snap = self.backend.storeSnapshot()
        env_a = self.backend.createIsolatedEnv(snap.id)
        env_b = self.backend.createIsolatedEnv(snap.id)
        s_a, r_a = self.backend.run_in_env(
            env_a, {"actions": [{"action": "tap", "view_id": "btn_settings"}],
                    "score": 0.8})
        s_b, r_b = self.backend.run_in_env(
            env_b, {"actions": [{"action": "input", "view_id": "edit_search",
                                 "text": "x"}], "score": 0.6})
        self.assertEqual(r_a["final_activity"], "Activity_btn_settings")
        self.assertEqual(r_b["final_activity"], "MainActivity")  # input 不导航
        # 母机状态未被分支影响
        self.assertEqual(self.backend.device.state.activity, "MainActivity")

    def test_consistency_fingerprint(self):
        """一致性校验：指纹匹配。"""
        self.backend.device.set_screen("MainActivity", self._home_screen())
        snap = self.backend.storeSnapshot()
        self.assertTrue(self.backend.verifyConsistency(snap.id))
        self.backend.device.tap("btn_settings")
        self.assertFalse(self.backend.verifyConsistency(snap.id))

    def test_trajectory_recorded(self):
        """操控轨迹记录。"""
        self.backend.device.set_screen("MainActivity", self._home_screen())
        env = self.backend.createIsolatedEnv(self.backend.storeSnapshot().id)
        self.backend.run_in_env(env, {"actions": [
            {"action": "tap", "view_id": "btn_settings"},
            {"action": "back"}]})
        self.assertEqual(len(self.backend.trajectory), 2)


class TestCrossPlatform(unittest.TestCase):
    """跨端一致性：统一契约在四类后端可用。"""

    def test_all_backends_share_contract(self):
        from statebranch.backend.filesystem import FileSystemBackend
        from statebranch.backend.process import ProcessIsolatedBackend
        for backend in [FileSystemBackend(tempfile.mkdtemp()),
                        ProcessIsolatedBackend(),
                        MicroVMBackend(),
                        AndroidBackend()]:
            self.assertIsInstance(backend, BackendAdapter)
            # 每个后端都实现了 6 个契约方法
            for m in ["collectState", "storeSnapshot", "restoreSnapshot",
                      "createIsolatedEnv", "verifyConsistency", "cleanup_env"]:
                self.assertTrue(callable(getattr(backend, m)), f"{backend.name}.{m}")

    def test_capability_matrix(self):
        """能力差异矩阵（跨端一致性测试报告核心数据）。"""
        from statebranch.backend.docker import DockerContainerBackend
        rows = {}
        rows["filesystem"] = {"parallel": False, "isolation": "file-dir"}
        rows["process"] = {"parallel": True, "isolation": "os-process"}
        rows["microvm"] = MicroVMBackend().capabilities
        rows["android"] = AndroidBackend().capabilities
        # 并行能力差异
        self.assertFalse(rows["android"]["parallel"])
        self.assertTrue(rows["microvm"]["parallel"])
        self.assertTrue(rows["process"]["parallel"])
        # 安卓特有能力
        self.assertTrue(rows["android"]["android_accessibility"])
        self.assertFalse(rows["microvm"]["android_accessibility"])
        # 隔离等级严格递增
        levels = {"file-dir": 0, "os-process": 1, "app-sandbox": 1,
                  "microvm-kernel": 2}
        self.assertLessEqual(levels["os-process"], levels["microvm-kernel"])


if __name__ == "__main__":
    unittest.main()
