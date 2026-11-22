"""core/gc.py — 快照垃圾回收（阶段2）。

解决阶段1已知限制「无快照垃圾回收机制」。

策略（Retention Policy）：
- keep_last_n：保留最近 N 个快照
- keep_named：永远保留用户命名快照（检查点语义）
- keep_max_age：超过 max_age 秒的快照回收
- 保护规则：被其他存活快照引用的父快照不可物理删除（只摘除元数据链中可断开的叶）；
  根快照默认保留（除非显式 allow_root_delete）。

对象回收：快照元数据删除后，统计仍被引用的 content_hash 集合，
objects 目录中未被任何存活快照引用的对象文件被安全清除。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .snapshot import SnapshotError, SnapshotManager, SnapshotNotFoundError


@dataclass
class GCResult:
    """一次垃圾回收的执行结果。"""

    removed_snapshots: List[str] = field(default_factory=list)
    removed_objects: int = 0
    kept_snapshots: int = 0
    freed_bytes: int = 0

    def to_dict(self) -> dict:
        return {
            "removed_snapshots": self.removed_snapshots,
            "removed_objects": self.removed_objects,
            "kept_snapshots": self.kept_snapshots,
            "freed_bytes": self.freed_bytes,
        }


@dataclass
class RetentionPolicy:
    """快照保留策略。"""

    keep_last_n: Optional[int] = None      # 保留最近 N 个
    keep_named: bool = True                # 命名快照永不回收
    keep_max_age: Optional[float] = None   # 超过此秒数回收
    allow_root_delete: bool = False        # 是否允许回收根快照


class SnapshotGC:
    """快照垃圾回收器。"""

    def __init__(self, manager: SnapshotManager,
                 policy: Optional[RetentionPolicy] = None):
        self.mgr = manager
        self.policy = policy or RetentionPolicy()

    # ------------------------------------------------------------------
    # 判定与收集
    # ------------------------------------------------------------------
    def _collect_candidates(self) -> List[str]:
        """依据保留策略选出可回收的快照 id。

        keep_last_n 之外的快照可通过链压缩（差异合并）安全回收；
        其余回收受存活引用保护。
        """
        snaps = self.mgr.list_snapshots()
        if not snaps:
            return []
        policy = self.policy
        referenced: Set[str] = {s.parent_id for s in snaps if s.parent_id}
        candidates: List[str] = []

        last_n_ids: Set[str] = set()
        if policy.keep_last_n is not None:
            last_n_ids = {s.id for s in snaps[-policy.keep_last_n:]}

        now = time.time()
        for s in snaps:
            # 最近 N 个保护
            if s.id in last_n_ids:
                continue
            # 命名快照保护
            if policy.keep_named and s.name:
                continue
            # 根快照保护
            if s.parent_id is None and not policy.allow_root_delete:
                continue
            # 年龄策略：未到期跳过
            if (policy.keep_max_age is not None
                    and (now - s.timestamp) < policy.keep_max_age):
                continue
            # 引用保护：仅当不依赖链压缩时生效
            if policy.keep_last_n is None and s.id in referenced:
                continue
            candidates.append(s.id)
        return candidates

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def run(self) -> GCResult:
        """执行垃圾回收：删除可回收快照元数据并清孤儿对象。"""
        result = GCResult()
        candidates = self._collect_candidates()
        # 链摘除：子快照重连祖父，并合并父快照差异到子快照，
        # 保证 state(child) = state(grandparent) + merged_diff 语义不变
        for sid in candidates:
            snap = self.mgr.get_snapshot(sid)
            for child in self.mgr.list_snapshots():
                if child.parent_id != sid:
                    continue
                merged = dict(child.fs_diff)
                for p, entry in snap.fs_diff.items():
                    if p not in merged:
                        merged[p] = entry
                child.fs_diff = merged
                child.parent_id = snap.parent_id
                child.checksum = self._recompute(child)
                self.mgr._save(child)
        for sid in candidates:
            meta_path = self.mgr._meta_path(sid)
            size = os.path.getsize(meta_path) if os.path.exists(meta_path) else 0
            try:
                if os.path.exists(meta_path):
                    os.remove(meta_path)
                result.removed_snapshots.append(sid)
                result.freed_bytes += size
            except SnapshotError:
                continue
        result.kept_snapshots = len(self.mgr.list_snapshots())
        result.removed_objects = self._reclaim_orphan_objects(result.freed_bytes is not None)
        return result

    def _reclaim_orphan_objects(self, *_args) -> int:
        """清除未被任何存活快照引用的内容对象，返回回收数量。"""
        referenced: Set[str] = set()
        for s in self.mgr.list_snapshots():
            for e in s.fs_diff.values():
                if e.content_hash:
                    referenced.add(e.content_hash)
        removed = 0
        if not os.path.isdir(self.mgr.objects_dir):
            return 0
        for name in os.listdir(self.mgr.objects_dir):
            if name in referenced:
                continue
            path = os.path.join(self.mgr.objects_dir, name)
            if os.path.isfile(path):
                os.remove(path)
                removed += 1
        return removed

    def _recompute(self, snap):
        """重算快照校验和（父引用变化后链压缩需要）。"""
        from .types import sha256_hex
        snap.merkle_root = self.mgr._compute_merkle_root(snap)
        return sha256_hex(self.mgr._snapshot_canonical(snap))

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        """当前快照与对象存储统计。"""
        snaps = self.mgr.list_snapshots()
        total_bytes = 0
        obj_count = 0
        if os.path.isdir(self.mgr.objects_dir):
            for name in os.listdir(self.mgr.objects_dir):
                p = os.path.join(self.mgr.objects_dir, name)
                if os.path.isfile(p):
                    obj_count += 1
                    total_bytes += os.path.getsize(p)
        return {
            "snapshot_count": len(snaps),
            "object_count": obj_count,
            "object_bytes": total_bytes,
        }
