"""tests/test_phase2.py — 阶段2集成测试。

覆盖：
- 后端适配器协议与探测
- 进程隔离后端：真并行执行、故障隔离、超时终止
- 快照垃圾回收：保留策略、命名保护、孤儿对象回收
- 分支轨迹持久化：链式校验、崩溃后可回放
- MCP 阶段2接口
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from statebranch.backend.base import BackendAdapter, detect_backend  # noqa: E402
from statebranch.backend.process import ProcessIsolatedBackend  # noqa: E402
from statebranch.core.snapshot import SnapshotManager  # noqa: E402
from statebranch.core.gc import RetentionPolicy, SnapshotGC  # noqa: E402
from statebranch.mcp.server import McpServer, StateBranchService  # noqa: E402


def _task_code(score: float, delay: float = 0.0, fail: bool = False) -> str:
    """生成分支任务源码：写文件 + 返回 (score, result)。"""
    return (
        "import time\n"
        f"time.sleep({delay})\n"
        f"with open('out.txt', 'w') as f:\n    f.write('{score}')\n"
        + (f"raise RuntimeError('boom')\n" if fail else "")
        + f"__sb_result__ = ({score}, 'done-{score}')\n"
    )


class TestBackendProtocol(unittest.TestCase):
    def test_detect_backend(self):
        """后端探测：无 Docker 时回退 process/filesystem。"""
        b = detect_backend()
        self.assertIn(b, ("docker", "process", "filesystem"))

    def test_process_backend_implements_protocol(self):
        backend = ProcessIsolatedBackend()
        self.assertIsInstance(backend, BackendAdapter)


class TestProcessIsolation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.backend = ProcessIsolatedBackend(root_dir=os.path.join(self._tmp, "store"))
        self.work = os.path.join(self._tmp, "work")
        os.makedirs(self.work)
        with open(os.path.join(self.work, "a.txt"), "w") as f:
            f.write("base")
        self.backend.active_work_dir = self.work
        self.snap = self.backend.storeSnapshot(name="T0")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_run_in_env_isolated_process(self):
        """任务在独立进程中执行并回传结果。"""
        env = self.backend.createIsolatedEnv(self.snap.id)
        score, result = self.backend.run_in_env(
            env, {"code": _task_code(0.8)}, timeout=30)
        self.assertEqual(score, 0.8)
        with open(os.path.join(env["work_dir"], "out.txt")) as f:
            self.assertEqual(f.read(), "0.8")
        self.backend.cleanup_env(env)

    def test_fault_isolation(self):
        """失败分支抛异常但不影响主进程；成功分支照常。"""
        env1 = self.backend.createIsolatedEnv(self.snap.id)
        env2 = self.backend.createIsolatedEnv(self.snap.id)
        with self.assertRaises(RuntimeError):
            self.backend.run_in_env(env1, {"code": _task_code(0, fail=True)}, timeout=30)
        score, _ = self.backend.run_in_env(env2, {"code": _task_code(0.5)}, timeout=30)
        self.assertEqual(score, 0.5)
        self.backend.cleanup_env(env1)
        self.backend.cleanup_env(env2)

    def test_timeout_terminates(self):
        """超时任务的子进程被强制终止。"""
        env = self.backend.createIsolatedEnv(self.snap.id)
        t0 = time.time()
        with self.assertRaises(TimeoutError):
            self.backend.run_in_env(env, {"code": _task_code(0, delay=30)}, timeout=2)
        self.assertLess(time.time() - t0, 15)
        self.backend.cleanup_env(env)

    def test_env_isolation_from_parent(self):
        """分支写入不污染父工作目录。"""
        env = self.backend.createIsolatedEnv(self.snap.id)
        self.backend.run_in_env(env, {"code": _task_code(1.0)}, timeout=30)
        self.assertFalse(os.path.exists(os.path.join(self.work, "out.txt")))
        self.backend.cleanup_env(env)


class TestSnapshotGC(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.work = os.path.join(self._tmp, "work")
        os.makedirs(self.work)
        self.mgr = SnapshotManager(os.path.join(self._tmp, "store"))
        self.mgr.set_work_dir(self.work)
        self.gc = SnapshotGC(self.mgr, RetentionPolicy(keep_last_n=3))

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _mk(self, name=None):
        with open(os.path.join(self.work, f"f-{time.time_ns()}.txt"), "w") as f:
            f.write("x" * 100)
        return self.mgr.create_snapshot(name=name or "")

    def test_keep_last_n(self):
        for _ in range(6):
            self._mk()
        result = self.gc.run()
        # 根快照受默认保护（allow_root_delete=False），保留根+最近3个
        self.assertEqual(len(self.mgr.list_snapshots()), 4)
        self.assertEqual(len(result.removed_snapshots), 2)

    def test_named_protected(self):
        """命名快照受 keep_named 保护。"""
        for i in range(5):
            self._mk(name="checkpoint" if i == 0 else None)
        self.gc.run()
        names = [s.name for s in self.mgr.list_snapshots()]
        self.assertIn("checkpoint", names)

    def test_chain_reference_protected(self):
        """被引用的父快照不回收。"""
        for _ in range(5):
            self._mk()
        self.gc.run()
        ids = {s.id for s in self.mgr.list_snapshots()}
        for s in self.mgr.list_snapshots():
            if s.parent_id:
                self.assertIn(s.parent_id, ids)

    def test_orphan_object_reclaim(self):
        """删除快照后，未被引用的对象文件被回收。"""
        s1 = self._mk()
        obj_dir = self.mgr.objects_dir
        before = len(os.listdir(obj_dir))
        # 直接删除元数据制造孤儿对象
        self.mgr._meta_path(s1.id) and os.remove(self.mgr._meta_path(s1.id))
        removed = SnapshotGC(self.mgr)._reclaim_orphan_objects()
        after = len(os.listdir(obj_dir))
        self.assertGreater(removed, 0)
        self.assertEqual(after, before - removed)

    def test_restore_survives_gc(self):
        """GC 后保留的快照仍可恢复。"""
        snaps = [self._mk() for _ in range(5)]
        self.gc.run()
        last = self.mgr.list_snapshots()[-1]
        self.assertTrue(self.mgr.verify_checksum(last.id))


class TestBranchTrajectory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.service = StateBranchService(work_dir=self._tmp)
        with open(os.path.join(self._tmp, "a.txt"), "w") as f:
            f.write("x")
        snap = self.service.backend.storeSnapshot()
        self.fork = self.service.fork_scheduler.create_fork(snap.id, name="A")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_record_and_verify(self):
        """记录步骤并校验分支轨迹链。"""
        for i in range(3):
            self.service.fork_scheduler.record_step(
                self.fork.id, intention=f"step-{i}", decision="d",
                tool_call="t", tool_result=f"r-{i}", env_change="e")
        steps = self.service.fork_scheduler.get_branch_trajectory(self.fork.id)
        self.assertEqual(len(steps), 3)
        self.assertTrue(self.service.fork_scheduler.verify_branch_trajectory(self.fork.id))
        # prev_hash 链
        for i in range(1, len(steps)):
            self.assertEqual(steps[i]["prev_hash"], steps[i - 1]["checksum"])

    def test_persisted_across_objects(self):
        """轨迹在文件系统持久化，可从磁盘重读。"""
        self.service.fork_scheduler.record_step(
            self.fork.id, intention="s", decision="d", tool_call="t")
        # 重建调度器（模拟重启）
        from statebranch.core.fork import BranchTrajectoryStore
        store = BranchTrajectoryStore(
            os.path.join(self.service.backend.snapshot_manager.base_dir,
                         "fork_trajectories"))
        self.assertEqual(len(store.load(self.fork.id)), 1)

    def test_tamper_detected(self):
        """篡改分支轨迹被检测。"""
        self.service.fork_scheduler.record_step(
            self.fork.id, intention="s", decision="d", tool_call="t")
        steps = self.service.fork_scheduler.get_branch_trajectory(self.fork.id)
        steps[0]["intention"] = "tampered"
        # 直接写回
        store = self.service.fork_scheduler.branch_trajectory
        import json
        with open(store._path(self.fork.id), "w", encoding="utf-8") as f:
            json.dump({"fork_id": self.fork.id, "steps": steps}, f)
        self.assertFalse(self.service.fork_scheduler.verify_branch_trajectory(self.fork.id))


class TestPhase2Mcp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.service = StateBranchService(work_dir=self._tmp)
        self.server = McpServer(self.service)
        with open(os.path.join(self._tmp, "a.txt"), "w") as f:
            f.write("x")
        r = self.server.call("snapshot.create", {"name": "T0"}, 1)
        self.snap_id = r["result"]["id"]

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_backend_info(self):
        r = self.server.call("backend.info", {}, 1)
        self.assertEqual(r["result"]["name"], "filesystem")

    def test_new_methods_routable(self):
        for m in ["gc.run", "gc.stats", "fork.record_step", "fork.trajectory",
                  "fork.run_parallel", "backend.info"]:
            resp = self.server._dispatch(m, {}, 1)
            self.assertNotEqual(resp.get("error", {}).get("code"), -32601, m)

    def test_gc_via_rpc(self):
        r = self.server.call("gc.run", {"keep_last_n": 2}, 1)
        self.assertIn("removed_snapshots", r["result"])
        r2 = self.server.call("gc.stats", {}, 2)
        self.assertIn("snapshot_count", r2["result"])

    def test_fork_parallel_and_trajectory_via_rpc(self):
        fa = self.server.call("fork.create", {"source_snapshot_id": self.snap_id,
                                              "name": "A"}, 1)["result"]
        fb = self.server.call("fork.create", {"source_snapshot_id": self.snap_id,
                                              "name": "B"}, 2)["result"]
        code_a = "with open('a.txt','w') as f: f.write('A')\n__sb_result__ = (0.7, 'A done')\n"
        code_b = "with open('a.txt','w') as f: f.write('B')\n__sb_result__ = (0.9, 'B done')\n"
        r = self.server.call("fork.run_parallel",
                             {"tasks": {fa["id"]: code_a, fb["id"]: code_b}}, 3)
        self.assertEqual(r["result"][fb["id"]]["score"], 0.9)
        self.assertEqual(r["result"][fa["id"]]["score"], 0.7)
        # 分支轨迹已持久化
        t = self.server.call("fork.trajectory", {"fork_id": fb["id"]}, 4)
        self.assertTrue(t["result"]["valid"])
        self.assertGreaterEqual(len(t["result"]["steps"]), 1)


if __name__ == "__main__":
    unittest.main()
