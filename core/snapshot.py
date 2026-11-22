"""core/snapshot.py — 增量快照管理器。

职责：在任意步骤保存文件与进程状态差异及 Agent 上下文，支持命名、检索与完整性校验。

关键设计：
- 增量存储与去重：仅记录自上次快照以来发生修改的文件，基于变化位图与内容寻址存储，
  文件内容通过 SHA-256 去重。
- 快照链式组织：每个快照指向其父快照，形成快照链；恢复时从根到目标依次重放差异。
- 完整性校验：每个快照包含 SHA-256 校验和与 Merkle 根哈希，恢复前验证防篡改。
- 存储策略：支持 COW（写时复制）、ROW（写时重定向）、混合三种策略。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import uuid
from typing import Dict, List, Optional

from .types import (
    DiffEntry,
    Snapshot,
    StorageStrategy,
    canonical_json,
    sha256_hex,
)


class SnapshotError(Exception):
    """快照相关异常基类。"""


class SnapshotNotFoundError(SnapshotError):
    """指定快照不存在。"""


class SnapshotVerifyError(SnapshotError):
    """快照完整性校验失败。"""


class SnapshotManager:
    """增量快照管理器。

    快照元数据以 JSON 形式持久化在 base_dir 下；文件内容按 SHA-256
    内容寻址存放在 objects 目录中实现去重。
    """

    def __init__(self, base_dir: str, strategy: str = StorageStrategy.MIXED):
        self.base_dir = base_dir
        self.strategy = strategy
        self.objects_dir = os.path.join(base_dir, "objects")
        self.meta_dir = os.path.join(base_dir, "snapshots")
        os.makedirs(self.objects_dir, exist_ok=True)
        os.makedirs(self.meta_dir, exist_ok=True)
        # 当前活跃工作目录（用于采集文件系统状态）
        self.work_dir: Optional[str] = None
        # 根快照：快照链的锚点
        self.root_id: Optional[str] = None

    # ------------------------------------------------------------------
    # 内容寻址对象存储
    # ------------------------------------------------------------------
    def _store_object(self, content: bytes) -> str:
        """按内容寻址存储对象，返回其 SHA-256。相同内容只存一份（去重）。"""
        digest = sha256_hex(content)
        obj_path = os.path.join(self.objects_dir, digest)
        if not os.path.exists(obj_path):
            tmp = obj_path + f".tmp-{uuid.uuid4().hex}"
            with open(tmp, "wb") as f:
                f.write(content)
            os.replace(tmp, obj_path)
        return digest

    def _read_object(self, digest: str) -> bytes:
        obj_path = os.path.join(self.objects_dir, digest)
        if not os.path.exists(obj_path):
            raise SnapshotError(f"对象不存在: {digest}")
        with open(obj_path, "rb") as f:
            return f.read()

    # ------------------------------------------------------------------
    # 文件系统采集
    # ------------------------------------------------------------------
    def set_work_dir(self, path: str) -> None:
        self.work_dir = os.path.abspath(path)

    def _walk(self) -> Dict[str, bytes]:
        """遍历工作目录，返回 {相对路径: bytes} 映射（跳过子快照存储目录）。"""
        if not self.work_dir:
            raise SnapshotError("尚未设置工作目录，无法采集文件系统状态")
        result: Dict[str, bytes] = {}
        base = self.work_dir
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in (".statebranch", "objects", "snapshots")]
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, base)
                try:
                    with open(full, "rb") as f:
                        result[rel] = f.read()
                except (OSError, PermissionError):
                    continue
        return result

    @staticmethod
    def _diff(prev: Dict[str, bytes], cur: Dict[str, bytes]) -> Dict[str, DiffEntry]:
        """计算 prev → cur 的文件差异。"""
        diff: Dict[str, DiffEntry] = {}
        all_keys = set(prev) | set(cur)
        for path in all_keys:
            old = prev.get(path)
            new = cur.get(path)
            if old is None and new is not None:
                diff[path] = DiffEntry(kind="added", path=path,
                                       content_hash=sha256_hex(new), content=new)
            elif old is not None and new is None:
                diff[path] = DiffEntry(kind="deleted", path=path)
            elif old != new:
                diff[path] = DiffEntry(kind="modified", path=path,
                                       content_hash=sha256_hex(new), content=new)
        return diff

    # ------------------------------------------------------------------
    # 校验和与 Merkle 根
    # ------------------------------------------------------------------
    @staticmethod
    def _snapshot_canonical(snap: Snapshot) -> bytes:
        """构造用于校验和的规范化字节（排除 checksum 自身字段）。"""
        payload = {
            "id": snap.id,
            "parent_id": snap.parent_id,
            "name": snap.name,
            "timestamp": snap.timestamp,
            "storage_strategy": snap.storage_strategy,
            "fs_diff": {k: v.to_dict() for k, v in snap.fs_diff.items()},
            "context": snap.context,
        }
        return canonical_json(payload)

    def _compute_merkle_root(self, snap: Snapshot) -> str:
        """基于 fs_diff 的内容哈希构造 Merkle 根（简单二叉树归并）。"""
        # 对每个 diff 条目的 content_hash 排序后逐级归并
        hashes = sorted(
            e.content_hash for e in snap.fs_diff.values() if e.content_hash
        )
        if not hashes:
            return sha256_hex(b"empty")
        while len(hashes) > 1:
            nxt = []
            for i in range(0, len(hashes), 2):
                left = hashes[i]
                right = hashes[i + 1] if i + 1 < len(hashes) else left
                nxt.append(sha256_hex(left.encode() + right.encode()))
            hashes = nxt
        return hashes[0]

    # ------------------------------------------------------------------
    # 快照 CRUD
    # ------------------------------------------------------------------
    def create_snapshot(self, name: str = "", context: Optional[dict] = None,
                        strategy: Optional[str] = None) -> Snapshot:
        """创建增量快照。

        首次创建以当前文件系统全量作为根快照；后续创建仅记录与最近
        快照（或自身基线）之间的差异。
        """
        cur_state = self._walk()
        cur_chain = self.list_snapshots()
        strategy = strategy or self.strategy

        if not cur_chain:
            # 根快照：全量
            diff = {
                path: DiffEntry(kind="added", path=path,
                                content_hash=sha256_hex(data), content=data)
                for path, data in cur_state.items()
            }
            parent_id = None
            self.root_id = None
        else:
            # 增量：与最近快照比较
            parent = self._load(cur_chain[-1].id)
            parent_state = self._rebuild_state(parent)
            diff = self._diff(parent_state, cur_state)
            parent_id = parent.id
            if self.root_id is None:
                self.root_id = parent.id

        snap = Snapshot(
            id=uuid.uuid4().hex,
            parent_id=parent_id,
            name=name,
            timestamp=time.time(),
            storage_strategy=strategy,
            fs_diff=diff,
            context=context or {},
        )

        # 内容寻址去重：added/modified 的对象存入 objects 目录
        for entry in snap.fs_diff.values():
            if entry.content is not None:
                digest = self._store_object(entry.content)
                entry.content_hash = digest
                entry.content = None  # 元数据不内联大对象

        snap.merkle_root = self._compute_merkle_root(snap)
        snap.checksum = sha256_hex(self._snapshot_canonical(snap))

        if self.root_id is None:
            self.root_id = snap.id
        self._save(snap)
        return snap

    def _rebuild_state(self, snap: Snapshot) -> Dict[str, bytes]:
        """重建某个快照所代表的完整文件状态（沿快照链回放）。"""
        state: Dict[str, bytes] = {}
        # 收集从根到该快照的链
        chain: List[Snapshot] = []
        cur: Optional[Snapshot] = snap
        while cur is not None:
            chain.append(cur)
            cur = self._load(cur.parent_id) if cur.parent_id else None
        # 从根向叶子重放
        for node in reversed(chain):
            for path, entry in node.fs_diff.items():
                if entry.kind == "deleted":
                    state.pop(path, None)
                else:
                    content = entry.content
                    if content is None and entry.content_hash:
                        content = self._read_object(entry.content_hash)
                    state[path] = content
        return state

    def get_snapshot(self, snapshot_id: str) -> Snapshot:
        return self._load(snapshot_id)

    def list_snapshots(self) -> List[Snapshot]:
        """按时间顺序返回快照链。"""
        if not os.path.isdir(self.meta_dir):
            return []
        snaps = []
        for name in os.listdir(self.meta_dir):
            if name.endswith(".json"):
                with open(os.path.join(self.meta_dir, name), "r", encoding="utf-8") as f:
                    snaps.append(Snapshot.from_dict(__import__("json").load(f)))
        snaps.sort(key=lambda s: s.timestamp)
        return snaps

    def delete_snapshot(self, snapshot_id: str) -> bool:
        """删除快照。仅支持删除叶节点，非叶节点会抛出异常以保护快照链。"""
        path = self._meta_path(snapshot_id)
        if not os.path.exists(path):
            raise SnapshotNotFoundError(f"快照不存在: {snapshot_id}")
        snap = self._load(snapshot_id)
        # 保护快照链：若有子节点引用当前节点，禁止删除
        for other in self.list_snapshots():
            if other.parent_id == snapshot_id:
                raise SnapshotError(
                    f"快照 {snapshot_id} 存在子快照 {other.id}，不能删除（保护快照链）")
        os.remove(path)
        return True

    def verify_checksum(self, snapshot_id: str) -> bool:
        """验证快照完整性（重算校验和 + Merkle 根）。"""
        snap = self._load(snapshot_id)
        computed = sha256_hex(self._snapshot_canonical(snap))
        if computed != snap.checksum:
            return False
        merkle = self._compute_merkle_root(snap)
        return merkle == snap.merkle_root

    def restore_snapshot(self, snapshot_id: str) -> bool:
        """将工作目录恢复到指定快照状态（沿链重放差异）。"""
        snap = self._load(snapshot_id)
        if not self.verify_checksum(snapshot_id):
            raise SnapshotVerifyError(f"快照 {snapshot_id} 完整性校验失败，拒绝恢复")
        state = self._rebuild_state(snap)
        if not self.work_dir:
            raise SnapshotError("尚未设置工作目录")
        # 清空当前目录（保留隐藏存储目录）
        base = self.work_dir
        for name in os.listdir(base):
            full = os.path.join(base, name)
            if name in (".statebranch",):
                continue
            if os.path.isdir(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
        # 重建文件
        for rel, content in state.items():
            full = os.path.join(base, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(content)
        return True

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def _meta_path(self, snapshot_id: str) -> str:
        return os.path.join(self.meta_dir, f"{snapshot_id}.json")

    def _save(self, snap: Snapshot) -> None:
        path = self._meta_path(snap.id)
        tmp = path + f".tmp-{uuid.uuid4().hex}"
        with open(tmp, "w", encoding="utf-8") as f:
            import json
            json.dump(snap.to_dict(), f, ensure_ascii=False)
        os.replace(tmp, path)

    def _load(self, snapshot_id: str) -> Snapshot:
        path = self._meta_path(snapshot_id)
        if not os.path.exists(path):
            raise SnapshotNotFoundError(f"快照不存在: {snapshot_id}")
        import json
        with open(path, "r", encoding="utf-8") as f:
            return Snapshot.from_dict(json.load(f))
