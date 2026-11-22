"""backend/docker.py — Docker 容器后端适配器（阶段2）。

真实容器隔离实现，语义与 Docker Engine API 对齐：
- createIsolatedEnv ≈ containers.run(image, detach=True)
- run_in_env        ≈ container.exec_run()
- commit 快照       ≈ container.commit() → image（镜像即快照）
- restoreSnapshot   ≈ 由快照镜像 run 新容器
- cleanup_env       ≈ container.remove(force=True)

运行时探测：docker SDK 未安装或 daemon 不可达时，构造本类会抛出
DockerUnavailableError；上层用 detect_backend() 决定是否启用。
"""
from __future__ import annotations

import json
import os
import tarfile
import tempfile
import time
import uuid
from typing import Any, Callable, Dict, Optional

from ..core.snapshot import SnapshotManager
from ..core.types import sha256_hex
from .base import BackendAdapter


class DockerUnavailableError(RuntimeError):
    """Docker SDK 不可用或 daemon 不可达。"""


class DockerContainerBackend(BackendAdapter):
    """Docker 容器后端：快照 = 容器 commit 得到的镜像。"""

    name = "docker"

    def __init__(self, base_image: str = "python:3.11-slim",
                 store_dir: Optional[str] = None):
        try:
            import docker
        except ImportError as e:
            raise DockerUnavailableError(f"docker SDK 未安装: {e}")
        try:
            self.client = docker.from_env()
            self.client.ping()
        except Exception as e:
            raise DockerUnavailableError(f"Docker daemon 不可达: {e}")
        self.base_image = base_image
        self.store_dir = store_dir or tempfile.mkdtemp(prefix="sb_docker_")
        self.snapshot_manager = SnapshotManager(
            os.path.join(self.store_dir, ".statebranch"))
        # 容器内工作路径
        self.container_workdir = "/workspace"
        # snapshot_id → image tag
        self._image_tags: Dict[str, str] = {}
        # fork_id → container
        self._containers: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _pull_if_needed(self) -> None:
        try:
            self.client.images.get(self.base_image)
        except Exception:
            self.client.images.pull(self.base_image)

    def _state_tar(self, state: Dict[str, bytes]) -> str:
        """把状态打包为可投放进容器的 tar。"""
        fd, path = tempfile.mkstemp(suffix=".tar")
        os.close(fd)
        with tarfile.open(path, "w") as tar:
            for rel, content in state.items():
                import io
                info = tarfile.TarInfo(name=rel)
                info.size = len(content)
                info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(content))
        return path

    # ------------------------------------------------------------------
    # 契约实现
    # ------------------------------------------------------------------
    def collectState(self) -> Dict[str, bytes]:
        self.snapshot_manager.set_work_dir(self.store_dir)
        return self.snapshot_manager._walk()

    def storeSnapshot(self, name: str = "", context: Optional[dict] = None):
        self._pull_if_needed()
        # 起一个临时容器承载快照镜像
        container = self.client.containers.run(
            self.base_image, command="sleep infinity",
            detach=True, working_dir=self.container_workdir)
        try:
            # 投放当前状态
            state = self.collectState()
            tar_path = self._state_tar(state)
            with open(tar_path, "rb") as f:
                container.put_archive(self.container_workdir, f.read())
            os.remove(tar_path)
            # commit → 镜像即快照
            snap_meta = self.snapshot_manager.create_snapshot(name=name, context=context)
            image = container.commit(
                repository=f"statebranch-snap",
                tag=f"{snap_meta.id[:16]}",
                changes=f'WORKDIR {self.container_workdir}')
            self._image_tags[snap_meta.id] = image.tags[0] if image.tags else image.id
            return snap_meta
        finally:
            container.remove(force=True)

    def restoreSnapshot(self, snapshot_id: str) -> bool:
        tag = self._image_tags.get(snapshot_id)
        if not tag:
            return False
        # 从快照镜像 run 新容器，把状态取回 store_dir
        container = self.client.containers.run(tag, command="sleep infinity",
                                               detach=True)
        try:
            bits, _ = container.get_archive(self.container_workdir, ".")
            fd, tar_path = tempfile.mkstemp(suffix=".tar")
            with os.fdopen(fd, "wb") as f:
                for chunk in bits:
                    f.write(chunk)
            with tarfile.open(tar_path) as tar:
                tar.extractall(self.store_dir)
            os.remove(tar_path)
            return True
        finally:
            container.remove(force=True)

    def createIsolatedEnv(self, snapshot_id: str) -> Dict[str, Any]:
        """从快照镜像创建隔离容器（≈ containers.run(snapshot_image)）。"""
        tag = self._image_tags.get(snapshot_id)
        if not tag:
            raise KeyError(f"快照镜像不存在: {snapshot_id}")
        container = self.client.containers.run(
            tag, command="sleep infinity", detach=True,
            working_dir=self.container_workdir)
        fork_env_id = uuid.uuid4().hex
        self._containers[fork_env_id] = container
        return {"fork_env_id": fork_env_id, "container_id": container.id,
                "image": tag}

    def run_in_env(self, env: Dict[str, Any], task_payload: Dict[str, Any],
                   timeout: float = 60.0) -> tuple:
        """在容器内执行任务脚本（≈ container.exec_run）。

        task_payload: {"code": <python 源码>}，返回 (score, result) JSON。
        """
        container = self._containers[env["fork_env_id"]]
        code = task_payload["code"]
        script = (
            "import json,sys\n"
            f"ns={{}}\n"
            f"exec({code!r}, ns)\n"
            "print('__SB_RESULT__' + json.dumps(ns['__sb_result__]))\n"
        )
        exit_code, out = container.exec_run(
            ["python", "-c", script], workdir=self.container_workdir)
        text = out.decode("utf-8", "replace")
        marker = "__SB_RESULT__"
        if marker in text:
            payload = json.loads(text[text.index(marker) + len(marker):].strip())
            return float(payload[0]), payload[1]
        raise RuntimeError(f"容器内任务失败(exit={exit_code}): {text[-500:]}")

    def verifyConsistency(self, snapshot_id: str, env: Any = None) -> bool:
        return self.snapshot_manager.verify_checksum(snapshot_id)

    def cleanup_env(self, env: Dict[str, Any]) -> None:
        container = self._containers.pop(env["fork_env_id"], None)
        if container is not None:
            container.remove(force=True)

    def close(self) -> None:
        for env in list(self._containers.values()):
            self.cleanup_env(env)
