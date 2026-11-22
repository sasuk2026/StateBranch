"""backend/filesystem.py — 文件系统后端适配器。

实现后端适配器接口契约，模拟容器后端沙箱。

本阶段隔离环境为文件拷贝式模拟沙箱，属于原型验证手段，并非操作系统级
容器/微虚拟机隔离。真实 Docker/Firecracker 容器后端接入工作安排在阶段2完成。

实现的接口契约：
- collectState(): 状态采集，捕获文件系统当前状态。
- storeSnapshot(): 增量快照存储。
- restoreSnapshot(): 快照恢复。
- createIsolatedEnv(): 分支隔离环境创建。
- verifyConsistency(): 一致性校验。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from ..core.snapshot import SnapshotManager
from ..core.types import DiffEntry, sha256_hex
from .base import BackendAdapter


@dataclass
class IsolatedEnv:
    """分支隔离环境（文件系统沙箱）。

    每个分支拥有独立的工作目录，状态从父快照重建，分支间互不干扰。
    """

    fork_id: str
    work_dir: str
    state: Dict[str, bytes] = field(default_factory=dict)

    def cleanup(self) -> None:
        """回收隔离环境资源。"""
        if os.path.isdir(self.work_dir):
            shutil.rmtree(self.work_dir, ignore_errors=True)


class FileSystemBackend(BackendAdapter):
    """文件系统后端适配器（阶段1原型）：实现统一 BackendAdapter 契约。"""

    name = "filesystem"

    def __init__(self, root_dir: str, strategy: str = "mixed"):
        self.root_dir = os.path.abspath(root_dir)
        os.makedirs(self.root_dir, exist_ok=True)
        # 快照存储目录
        self._snapshot_store = os.path.join(self.root_dir, ".statebranch")
        os.makedirs(self._snapshot_store, exist_ok=True)
        self.snapshot_manager = SnapshotManager(self._snapshot_store, strategy=strategy)
        # 活动工作目录（Agent 当前环境）
        self.active_work_dir: Optional[str] = None

    # ------------------------------------------------------------------
    # 接口契约
    # ------------------------------------------------------------------
    def collectState(self) -> Dict[str, bytes]:
        """状态采集：捕获当前活动工作目录的文件状态。"""
        if not self.active_work_dir:
            raise RuntimeError("未设置活动工作目录")
        self.snapshot_manager.set_work_dir(self.active_work_dir)
        return self.snapshot_manager._walk()

    def storeSnapshot(self, name: str = "", context: Optional[dict] = None):
        """增量快照存储：基于当前活动目录创建快照。"""
        self.snapshot_manager.set_work_dir(self.active_work_dir)
        return self.snapshot_manager.create_snapshot(name=name, context=context)

    def restoreSnapshot(self, snapshot_id: str) -> bool:
        """快照恢复：将活动工作目录恢复到指定快照状态。"""
        self.snapshot_manager.set_work_dir(self.active_work_dir)
        return self.snapshot_manager.restore_snapshot(snapshot_id)

    def createIsolatedEnv(self, snapshot_id: str) -> IsolatedEnv:
        """分支隔离环境创建：从指定快照重建独立工作目录。

        基于文件拷贝式沙箱模拟隔离，后续阶段接入真实容器/微虚拟机。
        """
        snap = self.snapshot_manager.get_snapshot(snapshot_id)
        state = self.snapshot_manager._rebuild_state(snap)
        work_dir = tempfile.mkdtemp(prefix=f"fork_{uuid.uuid4().hex}_", dir=self.root_dir)
        # 写入快照状态
        for rel, content in state.items():
            full = os.path.join(work_dir, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(content)
        return IsolatedEnv(fork_id=snapshot_id, work_dir=work_dir, state=state)

    def run_in_env(self, env: "IsolatedEnv", task: Callable[[str], tuple],
                   timeout: float = 60.0) -> tuple:
        """阶段1原型语义：在隔离环境目录内同步执行任务。"""
        work_dir = env.work_dir if hasattr(env, "work_dir") else env.get("work_dir", "")
        result = task(work_dir)
        return float(result[0]), result[1]

    def cleanup_env(self, env: "IsolatedEnv") -> None:
        env.cleanup()

    def verifyConsistency(self, snapshot_id: str, work_dir: Optional[str] = None) -> bool:
        """一致性校验：校验快照完整性与当前环境是否与快照一致。"""
        if not self.snapshot_manager.verify_checksum(snapshot_id):
            return False
        target_dir = work_dir or self.active_work_dir
        if not target_dir:
            return False
        snap = self.snapshot_manager.get_snapshot(snapshot_id)
        expected = self.snapshot_manager._rebuild_state(snap)
        # 采集目标目录当前状态
        prev_wd = self.snapshot_manager.work_dir
        self.snapshot_manager.set_work_dir(target_dir)
        try:
            actual = self.snapshot_manager._walk()
        finally:
            self.snapshot_manager.set_work_dir(prev_wd)
        # 比较
        if set(expected) != set(actual):
            return False
        for path, content in expected.items():
            if sha256_hex(actual.get(path, b"")) != sha256_hex(content):
                return False
        return True
