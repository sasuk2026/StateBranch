"""backend/process.py — 进程隔离后端（阶段2）。

在无 Docker 的隔离沙箱环境中提供**真正的操作系统进程级隔离**：
- 每个分支在独立子进程中运行（multiprocessing，spawn 上下文），
  分支崩溃/超时不影响主进程与其他分支；
- 分支环境基于独立工作目录（文件系统隔离）+ 独立进程（执行隔离）；
- 任务结果通过管道序列化传回，环境与主进程无共享可变状态。

与 Docker 后端实现同一 BackendAdapter 契约，上层调度器无感切换。
"""
from __future__ import annotations

import multiprocessing as mp
import os
import shutil
import tempfile
import time
import uuid
from typing import Any, Callable, Dict, Optional

from ..core.snapshot import SnapshotManager
from .base import BackendAdapter
from .filesystem import FileSystemBackend


class ProcessIsolatedBackend(BackendAdapter):
    """进程隔离后端：文件系统隔离 + 独立进程执行隔离。"""

    name = "process"

    def __init__(self, root_dir: Optional[str] = None,
                 snapshot_manager: Optional[SnapshotManager] = None):
        self.root_dir = os.path.abspath(root_dir or tempfile.mkdtemp(prefix="sb_proc_"))
        os.makedirs(self.root_dir, exist_ok=True)
        # 复用文件系统后端做快照存储（快照/恢复逻辑一致）
        self._fs = FileSystemBackend(self.root_dir)
        if snapshot_manager is not None:
            self._fs.snapshot_manager = snapshot_manager
        self._envs: Dict[str, Dict[str, Any]] = {}
        self._ctx = mp.get_context("spawn")

    # ------------------------------------------------------------------
    # 代理到文件系统后端的状态接口
    # ------------------------------------------------------------------
    @property
    def snapshot_manager(self) -> SnapshotManager:
        return self._fs.snapshot_manager

    @property
    def active_work_dir(self) -> Optional[str]:
        return self._fs.active_work_dir

    @active_work_dir.setter
    def active_work_dir(self, v: Optional[str]) -> None:
        self._fs.active_work_dir = v

    def collectState(self) -> Dict[str, bytes]:
        return self._fs.collectState()

    def storeSnapshot(self, name: str = "", context: Optional[dict] = None):
        return self._fs.storeSnapshot(name=name, context=context)

    def restoreSnapshot(self, snapshot_id: str) -> bool:
        return self._fs.restoreSnapshot(snapshot_id)

    def verifyConsistency(self, snapshot_id: str, env: Any = None) -> bool:
        work_dir = env.get("work_dir") if isinstance(env, dict) else None
        return self._fs.verifyConsistency(snapshot_id, work_dir)

    # ------------------------------------------------------------------
    # 隔离环境
    # ------------------------------------------------------------------
    def createIsolatedEnv(self, snapshot_id: str) -> Dict[str, Any]:
        env_fs = self._fs.createIsolatedEnv(snapshot_id)
        env = {"fork_env_id": uuid.uuid4().hex, "work_dir": env_fs.work_dir}
        self._envs[env["fork_env_id"]] = env
        return env

    def run_in_env(self, env: Dict[str, Any],
                   task_payload: Dict[str, Any], timeout: float = 60.0) -> tuple:
        """在独立子进程中执行任务（真进程隔离）。

        task_payload: {"code": <python 源码>}。子进程内 exec 代码，
        代码须设置 ns['__sb_result__'] = (score, result)。
        子进程崩溃/超时以异常形式抛出，不影响主进程。
        """
        code = task_payload["code"]
        q: "mp.Queue" = self._ctx.Queue()

        p = self._ctx.Process(
            target=_process_worker,
            args=(q, env["work_dir"], code), daemon=True)
        p.start()
        p.join(timeout)
        if p.is_alive():
            p.terminate()
            p.join(5)
            raise TimeoutError(f"分支任务执行超时({timeout}s)，进程已终止")
        if q.empty():
            raise RuntimeError("分支进程异常退出且未返回结果")
        status, payload = q.get()
        if status == "error":
            raise RuntimeError(f"分支任务失败: {payload}")
        return float(payload[0]), payload[1]

    def cleanup_env(self, env: Dict[str, Any]) -> None:
        work_dir = env.get("work_dir")
        if work_dir and os.path.isdir(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
        self._envs.pop(env.get("fork_env_id"), None)

    def close(self) -> None:
        for env in list(self._envs.values()):
            self.cleanup_env(env)


def _process_worker(queue, work_dir: str, src: str) -> None:
    """模块级子进程入口（spawn 可 pickle）。"""
    try:
        if work_dir and os.path.isdir(work_dir):
            os.chdir(work_dir)  # 任务相对路径落在隔离目录内
        ns: Dict[str, Any] = {"__file__": work_dir}
        exec(compile(src, "<branch-task>", "exec"), ns)
        queue.put(("ok", ns.get("__sb_result__", (0.0, None))))
    except Exception as e:  # 子进程内异常也回传
        queue.put(("error", f"{type(e).__name__}: {e}"))
