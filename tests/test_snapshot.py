"""tests/test_snapshot.py — 快照 CRUD 与完整性校验单元测试。

覆盖：
- create_snapshot / get_snapshot / list_snapshots / delete_snapshot
- 增量差异（去重）与内容寻址
- verify_checksum 哈希校验（篡改检测）
- restore_snapshot 回滚恢复
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from statebranch.core.snapshot import (  # noqa: E402
    SnapshotManager,
    SnapshotNotFoundError,
    SnapshotVerifyError,
)


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.base_dir = os.path.join(self._tmp, "store")
        self.work_dir = os.path.join(self._tmp, "work")
        os.makedirs(self.work_dir, exist_ok=True)
        self.mgr = SnapshotManager(self.base_dir)
        self.mgr.set_work_dir(self.work_dir)

    def _write(self, rel, content):
        full = os.path.join(self.work_dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)

    def _read(self, rel):
        with open(os.path.join(self.work_dir, rel), "r", encoding="utf-8") as f:
            return f.read()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ------------------------------------------------------------------
    def test_create_and_get(self):
        """创建快照并可检索。"""
        self._write("a.txt", "hello")
        snap = self.mgr.create_snapshot(name="T0")
        self.assertEqual(snap.name, "T0")
        got = self.mgr.get_snapshot(snap.id)
        self.assertEqual(got.id, snap.id)
        self.assertEqual(got.parent_id, None)

    def test_incremental_diff(self):
        """第二次快照仅记录增量差异（内容寻址去重）。"""
        self._write("a.txt", "AAA")
        self._write("b.txt", "BBB")
        snap1 = self.mgr.create_snapshot(name="T1")
        # 修改 a，新增 c，删除 b
        self._write("a.txt", "AAA-modified")
        self._write("c.txt", "CCC")
        os.remove(os.path.join(self.work_dir, "b.txt"))
        snap2 = self.mgr.create_snapshot(name="T2")

        self.assertEqual(snap2.parent_id, snap1.id)
        kinds = {k: e.kind for k, e in snap2.fs_diff.items()}
        self.assertEqual(kinds.get("a.txt"), "modified")
        self.assertEqual(kinds.get("c.txt"), "added")
        self.assertEqual(kinds.get("b.txt"), "deleted")

    def test_content_addressing_dedup(self):
        """相同内容的对象只存一份（SHA-256 去重）。"""
        self._write("x.txt", "SAME-CONTENT")
        snap1 = self.mgr.create_snapshot()
        obj_dir = os.path.join(self.base_dir, "objects")
        count1 = len(os.listdir(obj_dir))
        # 相同内容写入不同路径再次快照
        self._write("y.txt", "SAME-CONTENT")
        snap2 = self.mgr.create_snapshot()
        count2 = len(os.listdir(obj_dir))
        self.assertEqual(count1, count2, "相同内容不应重复存储")
        self.assertEqual(snap1.checksum != snap2.checksum, True)

    def test_list_returns_chain(self):
        """list_snapshots 按时间返回快照链。"""
        self._write("a.txt", "1")
        s1 = self.mgr.create_snapshot(name="A")
        self._write("a.txt", "2")
        s2 = self.mgr.create_snapshot(name="B")
        snaps = self.mgr.list_snapshots()
        self.assertEqual([s.id for s in snaps], [s1.id, s2.id])

    def test_delete_leaf(self):
        """删除叶节点快照成功。"""
        self._write("a.txt", "1")
        self.mgr.create_snapshot(name="A")
        leaf = self.mgr.create_snapshot(name="B")
        self.assertTrue(self.mgr.delete_snapshot(leaf.id))
        with self.assertRaises(SnapshotNotFoundError):
            self.mgr.get_snapshot(leaf.id)

    def test_delete_non_leaf_protected(self):
        """删除非叶节点受保护（存在子快照时禁止删除）。"""
        self._write("a.txt", "1")
        parent = self.mgr.create_snapshot(name="A")
        self.mgr.create_snapshot(name="B")
        with self.assertRaises(Exception):
            self.mgr.delete_snapshot(parent.id)

    # ------------------------------------------------------------------
    def test_verify_checksum_valid(self):
        """校验和验证通过。"""
        self._write("a.txt", "data")
        snap = self.mgr.create_snapshot()
        self.assertTrue(self.mgr.verify_checksum(snap.id))

    def test_verify_checksum_tampered(self):
        """篡改快照元数据后校验失败。"""
        self._write("a.txt", "data")
        snap = self.mgr.create_snapshot()
        # 篡改 fs_diff 内容
        import json
        meta_path = os.path.join(self.base_dir, "snapshots", f"{snap.id}.json")
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["fs_diff"]["a.txt"]["content_b64"] = "TEFNRV9DT05URU5U"  # "LAME_CONTENT"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.assertFalse(self.mgr.verify_checksum(snap.id))

    # ------------------------------------------------------------------
    def test_restore(self):
        """回滚恢复：从破坏状态恢复回快照状态。"""
        self._write("app.py", "def main():\n    print('hi')\n")
        snap = self.mgr.create_snapshot(name="T0")
        # 破坏
        self._write("app.py", "print(undefined_var)\n")
        self._write("junk.txt", "junk")
        # 回滚
        ok = self.mgr.restore_snapshot(snap.id)
        self.assertTrue(ok)
        self.assertEqual(self._read("app.py"), "def main():\n    print('hi')\n")
        self.assertFalse(os.path.exists(os.path.join(self.work_dir, "junk.txt")))

    def test_restore_chain(self):
        """沿链回滚到中间快照。"""
        self._write("a.txt", "v1")
        s1 = self.mgr.create_snapshot(name="S1")
        self._write("a.txt", "v2")
        self.mgr.create_snapshot(name="S2")
        self._write("a.txt", "v3")
        self.mgr.create_snapshot(name="S3")
        # 回滚到 S2
        self.mgr.restore_snapshot(s1.id)
        self.assertEqual(self._read("a.txt"), "v1")

    def test_restore_rejects_tampered(self):
        """完整性校验失败的快照拒绝恢复。"""
        self._write("a.txt", "data")
        snap = self.mgr.create_snapshot()
        import json
        meta_path = os.path.join(self.base_dir, "snapshots", f"{snap.id}.json")
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["name"] = "hacked"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        with self.assertRaises(SnapshotVerifyError):
            self.mgr.restore_snapshot(snap.id)


if __name__ == "__main__":
    unittest.main()
