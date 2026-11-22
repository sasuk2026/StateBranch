"""tests/test_fork_mcp.py — 分支调度器与 MCP 接口层集成测试。

覆盖：
- 分支状态机流转（created → running → completed/failed）
- 分支隔离与并行比较择优
- 分支资源回收
- 一致性校验层默认规则（snapshot_exists / checksum_valid）
- MCP 16 个标准接口的 JSON-RPC 2.0 分发
- stdio 传输模式的 JSON 协议
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from statebranch.core.fork import ConsistencyChecker, ForkScheduler  # noqa: E402
from statebranch.core.types import ForkState  # noqa: E402
from statebranch.mcp.server import McpServer, StateBranchService  # noqa: E402


class TestForkScheduler(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.work_dir = os.path.join(self._tmp, "work")
        os.makedirs(self.work_dir, exist_ok=True)
        self.service = StateBranchService(work_dir=self.work_dir)
        self._write("app.py", "def main():\n    print('hi')\n")
        self.snap_id = self.service.backend.storeSnapshot(name="T0").id

    def _write(self, rel, content):
        full = os.path.join(self.work_dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ------------------------------------------------------------------
    def test_fork_state_machine(self):
        """分支状态机：created → running → completed。"""
        fork = self.service.fork_scheduler.create_fork(self.snap_id, name="A")
        self.assertEqual(fork.state, ForkState.CREATED)
        result = self.service.fork_scheduler.run(
            fork.id, lambda wd: (0.8, _write_file(wd, "a.txt", "x")))
        self.assertEqual(result.state, ForkState.COMPLETED)
        self.assertEqual(result.score, 0.8)

    def test_fork_isolation(self):
        """分支隔离：分支写入不影响父环境。"""
        f1 = self.service.fork_scheduler.create_fork(self.snap_id, name="A")
        f2 = self.service.fork_scheduler.create_fork(self.snap_id, name="B")
        self.service.fork_scheduler.run(f1.id, lambda wd: (1.0, _write_file(wd, "a.txt", "AAA")))
        self.service.fork_scheduler.run(f2.id, lambda wd: (1.0, _write_file(wd, "a.txt", "BBB")))
        # 两个分支环境内容各自独立
        env1 = self.service.fork_scheduler._envs[f1.id]
        env2 = self.service.fork_scheduler._envs[f2.id]
        with open(os.path.join(env1.work_dir, "a.txt")) as f:
            self.assertEqual(f.read(), "AAA")
        with open(os.path.join(env2.work_dir, "a.txt")) as f:
            self.assertEqual(f.read(), "BBB")

    def test_fork_compare_and_prune(self):
        """比较择优并回收资源。"""
        f1 = self.service.fork_scheduler.create_fork(self.snap_id, name="A")
        f2 = self.service.fork_scheduler.create_fork(self.snap_id, name="B")
        self.service.fork_scheduler.run(f1.id, lambda wd: (0.6, "ok"))
        self.service.fork_scheduler.run(f2.id, lambda wd: (0.9, "ok"))
        ranked = self.service.fork_scheduler.compare()
        self.assertEqual(ranked[0].id, f2.id, "B 得分更高，应排第一")
        removed = self.service.fork_scheduler.prune([f1.id, f2.id])
        self.assertEqual(len(removed), 2)
        self.assertEqual(len(self.service.fork_scheduler.list_forks()), 0)

    def test_consistency_rules(self):
        """一致性校验层默认规则。"""
        checker = ConsistencyChecker(self.service.backend)
        self.assertIn("snapshot_exists", checker.list_rules())
        self.assertIn("checksum_valid", checker.list_rules())
        self.assertTrue(checker.check(self.snap_id))
        self.assertFalse(checker.check("nonexistent"))


class TestMcpServer(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.work_dir = os.path.join(self._tmp, "work")
        os.makedirs(self.work_dir, exist_ok=True)
        self.service = StateBranchService(work_dir=self.work_dir)
        self.server = McpServer(self.service)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ------------------------------------------------------------------
    def test_16_standard_methods_routable(self):
        """16 个标准接口均可路由。"""
        methods = [
            "snapshot.create", "snapshot.list", "snapshot.get", "snapshot.delete",
            "fork.create", "fork.list", "fork.get", "fork.compare", "fork.prune",
            "restore.execute", "restore.verify",
            "replay.start", "replay.pause", "replay.stop",
            "recall.search", "recall.replay",
        ]
        for m in methods:
            resp = self.server._dispatch(m, {}, 1)
            # 无参数调用可能报参数错误，但不应是方法不存在
            self.assertNotEqual(resp.get("error", {}).get("code"), -32601, f"{m} 未路由")

    def test_snapshot_crud_via_rpc(self):
        """通过 RPC 完成快照 CRUD。"""
        os.makedirs(self.work_dir, exist_ok=True)
        with open(os.path.join(self.work_dir, "f.txt"), "w") as f:
            f.write("hi")
        r1 = self.server.call("snapshot.create", {"name": "T0"}, 1)
        self.assertEqual(r1["id"], 1)
        snap_id = r1["result"]["id"]
        r2 = self.server.call("snapshot.get", {"snapshot_id": snap_id}, 2)
        self.assertEqual(r2["result"]["id"], snap_id)
        r3 = self.server.call("snapshot.list", {}, 3)
        self.assertEqual(len(r3["result"]), 1)
        r4 = self.server.call("snapshot.delete", {"snapshot_id": snap_id}, 4)
        self.assertTrue(r4["result"]["success"])

    def test_method_not_found(self):
        """未知方法返回 -32601。"""
        resp = self.server.call("unknown.method", {}, 1)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_stdio_protocol(self):
        """stdio 传输模式的 JSON-RPC 协议。"""
        os.makedirs(self.work_dir, exist_ok=True)
        with open(os.path.join(self.work_dir, "f.txt"), "w") as f:
            f.write("hi")
        req = json.dumps({"jsonrpc": "2.0", "id": 42, "method": "snapshot.create",
                          "params": {"name": "T0"}})
        resp = json.loads(self.server.handle_jsonrpc(req))
        self.assertEqual(resp["id"], 42)
        self.assertIn("result", resp)

    def test_invalid_json(self):
        """非法 JSON 返回 -32700。"""
        resp = json.loads(self.server.handle_jsonrpc("{bad json"))
        self.assertEqual(resp["error"]["code"], -32700)


def _write_file(wd, rel, content):
    full = os.path.join(wd, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return content


if __name__ == "__main__":
    unittest.main()
