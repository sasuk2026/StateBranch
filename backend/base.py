"""backend/base.py — 后端适配器协议（阶段2）。

定义统一的后端适配器接口契约，屏蔽底层隔离实现差异。

协议语义与 Docker Engine API 对齐：
- createIsolatedEnv()  ≈ containers.run(image, detach=True)
- commit()             ≈ container.commit() → image（镜像即快照）
- restoreSnapshot()    ≈ containers.run(snapshot_image)
- cleanup()            ≈ container.remove(force=True)

阶段2提供两个实现：
- DockerContainerBackend（backend/docker.py）：真实 Docker 容器，环境探测可用时启用。
- ProcessIsolatedBackend（backend/process.py）：multiprocessing 独立进程隔离，
  在无 Docker 的隔离沙箱环境中提供真正的进程级并行与故障隔离。

阶段1的 FileSystemBackend 继续作为轻量原型实现保留。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional


class BackendAdapter(ABC):
    """后端适配器协议：所有隔离后端必须实现本契约。"""

    name: str = "abstract"

    @abstractmethod
    def collectState(self) -> Dict[str, bytes]:
        """状态采集：捕获当前环境的完整状态。"""

    @abstractmethod
    def storeSnapshot(self, name: str = "", context: Optional[dict] = None) -> Any:
        """快照存储：保存当前状态为可恢复快照。"""

    @abstractmethod
    def restoreSnapshot(self, snapshot_id: str) -> bool:
        """快照恢复：将环境恢复到指定快照状态。"""

    @abstractmethod
    def createIsolatedEnv(self, snapshot_id: str) -> Any:
        """分支隔离环境创建：从快照派生相互隔离的独立环境。"""

    @abstractmethod
    def verifyConsistency(self, snapshot_id: str, env: Any = None) -> bool:
        """一致性校验：校验快照完整性及环境与快照的一致性。"""

    @abstractmethod
    def run_in_env(self, env: Any, task: Callable[[str], tuple],
                   timeout: float = 60.0) -> tuple:
        """在隔离环境中运行任务，返回 (score, result)。

        隔离等级由后端决定：容器后端为进程+内核隔离；
        进程后端为独立操作系统进程隔离。
        """

    @abstractmethod
    def cleanup_env(self, env: Any) -> None:
        """回收隔离环境资源（≈ container.remove(force=True)）。"""


def detect_backend(prefer: str = "auto") -> str:
    """探测当前环境可用的最优后端。

    返回 "docker" | "process" | "filesystem"。
    """
    if prefer == "docker" or prefer == "auto":
        try:
            import docker  # noqa: F401
            import socket
            # 探测 Docker daemon 是否可达
            client = docker.from_env()
            client.ping()
            return "docker"
        except Exception:
            if prefer == "docker":
                raise RuntimeError("指定 docker 后端但 Docker daemon 不可达")
    if prefer in ("process", "auto"):
        return "process"
    return "filesystem"
