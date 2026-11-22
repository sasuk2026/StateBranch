"""core/fork.py — 分支执行调度器 + 一致性校验层。

职责（分支执行调度器）：从快照创建隔离分支，并行运行不同方案，结束后
对比结果并回收资源。

关键设计：
- 分支状态机：created → running → completed | failed | cancelled，
  任意终态后不可再恢复运行。
- 隔离环境：阶段 1 基于文件系统沙箱模拟容器隔离，后续阶段接入 Docker/微虚拟机。
- 并行调度：基于 ThreadPoolExecutor 支持多个分支并行运行。
- 结果比较：支持按得分比较多个分支，择优保留，失败分支回收资源。

职责（一致性校验层）：为快照绑定校验规则，在回滚与恢复后自动检查环境
一致性。支持注册自定义校验规则，默认注册 snapshot_exists 与 checksum_valid
两条规则。

注：代码实现暂放置于 fork.py；逻辑上一致性校验同时服务快照恢复与分支恢复
流程，后续迭代可拆分独立文件。
"""
from __future__ import annotations

import multiprocessing as mp
import os
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..backend.filesystem import FileSystemBackend, IsolatedEnv
from .types import ForkState, canonical_json, sha256_hex


# ---------------------------------------------------------------------------
# 一致性校验层
# ---------------------------------------------------------------------------
class ConsistencyRuleError(Exception):
    """一致性校验规则相关异常。"""


class ConsistencyChecker:
    """一致性校验层。

    注册可自定义的校验规则，在回滚与恢复后自动检查环境一致性。
    默认注册 snapshot_exists 与 checksum_valid 两条规则。
    """

    def __init__(self, backend: FileSystemBackend):
        self.backend = backend
        self._rules: Dict[str, Callable[[FileSystemBackend, str], bool]] = {}
        # 默认注册两条内置规则
        self.register_rule("snapshot_exists", self._rule_snapshot_exists)
        self.register_rule("checksum_valid", self._rule_checksum_valid)

    # ---- 内置规则 ----
    @staticmethod
    def _rule_snapshot_exists(backend: FileSystemBackend, snapshot_id: str) -> bool:
        """规则1：快照必须存在。"""
        try:
            backend.snapshot_manager.get_snapshot(snapshot_id)
            return True
        except Exception:
            return False

    @staticmethod
    def _rule_checksum_valid(backend: FileSystemBackend, snapshot_id: str) -> bool:
        """规则2：快照校验和必须有效。"""
        try:
            return backend.snapshot_manager.verify_checksum(snapshot_id)
        except Exception:
            return False

    # ---- 规则注册 ----
    def register_rule(self, name: str, fn: Callable[[FileSystemBackend, str], bool]) -> None:
        self._rules[name] = fn

    def unregister_rule(self, name: str) -> None:
        self._rules.pop(name, None)

    def list_rules(self) -> List[str]:
        return list(self._rules.keys())

    def check(self, snapshot_id: str) -> bool:
        """执行全部已注册规则，全部通过才返回 True。"""
        for name, fn in self._rules.items():
            if not fn(self.backend, snapshot_id):
                return False
        return True


# ---------------------------------------------------------------------------
# 分支执行调度器
# ---------------------------------------------------------------------------
@dataclass
class Fork:
    """分支对象。"""

    id: str
    name: str
    source_snapshot_id: str
    state: str = ForkState.CREATED
    score: float = 0.0
    result: Any = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    work_dir: str = ""
    strategy_desc: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "source_snapshot_id": self.source_snapshot_id,
            "state": self.state,
            "score": self.score,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "strategy_desc": self.strategy_desc,
        }



# ---------------------------------------------------------------------------
# 阶段2：环境形态兼容（IsolatedEnv 对象 / dict 均支持）
# ---------------------------------------------------------------------------
def _env_workdir(env) -> str:
    if isinstance(env, dict):
        return env.get("work_dir", "")
    return getattr(env, "work_dir", "")


def _env_cleanup(env) -> None:
    if isinstance(env, dict):
        import shutil as _sh
        wd = env.get("work_dir")
        if wd and os.path.isdir(wd):
            _sh.rmtree(wd, ignore_errors=True)
        return
    if hasattr(env, "cleanup"):
        env.cleanup()


class ForkScheduler:
    """分支执行调度器。

    从快照创建隔离分支，并行运行不同方案，结束后对比结果并回收资源。
    """

    def __init__(self, backend: FileSystemBackend, max_workers: int = 4,
                 trajectory_store_dir: Optional[str] = None):
        self.backend = backend
        self.max_workers = max_workers
        self._forks: Dict[str, Fork] = {}
        self._envs: Dict[str, IsolatedEnv] = {}
        # 阶段2：分支轨迹持久化存储
        self.branch_trajectory = BranchTrajectoryStore(
            trajectory_store_dir or os.path.join(
                backend.snapshot_manager.base_dir, "fork_trajectories"))
        # 阶段2：进程级并行运行器
        self.process_runner = ProcessParallelRunner(max_workers=max_workers)

    # ------------------------------------------------------------------
    # 分支 CRUD
    # ------------------------------------------------------------------
    def create_fork(self, source_snapshot_id: str, name: str = "",
                    strategy_desc: str = "") -> Fork:
        """从快照创建隔离分支。"""
        # 一致性：分支来源快照须存在且有效
        checker = ConsistencyChecker(self.backend)
        if not checker.check(source_snapshot_id):
            raise ConsistencyRuleError(
                f"来源快照 {source_snapshot_id} 未通过一致性校验，无法创建分支")
        env = self.backend.createIsolatedEnv(source_snapshot_id)
        fork = Fork(
            id=uuid.uuid4().hex,
            name=name or f"fork-{len(self._forks) + 1}",
            source_snapshot_id=source_snapshot_id,
            strategy_desc=strategy_desc,
            work_dir=_env_workdir(env),
        )
        self._forks[fork.id] = fork
        self._envs[fork.id] = env
        return fork

    def get_fork(self, fork_id: str) -> Fork:
        if fork_id not in self._forks:
            raise KeyError(f"分支不存在: {fork_id}")
        return self._forks[fork_id]

    def list_forks(self) -> List[Fork]:
        return list(self._forks.values())

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def run(self, fork_id: str, task: Callable[[str], tuple], timeout: float = 30.0) -> Fork:
        """在分支隔离环境中运行任务。

        task 接收隔离环境的工作目录路径，返回 (score, result)。
        """
        fork = self.get_fork(fork_id)
        if fork.state != ForkState.CREATED:
            raise RuntimeError(f"分支 {fork_id} 当前状态 {fork.state}，仅 created 可运行")
        env = self._envs[fork_id]
        fork.state = ForkState.RUNNING

        def _exec():
            try:
                score, result = task(env.work_dir)
                fork.score = float(score)
                fork.result = result
                fork.state = ForkState.COMPLETED
                fork.error = ""
            except Exception as e:  # noqa: BLE001
                fork.state = ForkState.FAILED
                fork.error = str(e)
            finally:
                fork.finished_at = time.time()
            return fork

        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_exec)
            try:
                fut.result(timeout=timeout)
            except TimeoutError:
                fork.state = ForkState.FAILED
                fork.error = "执行超时"
                fork.finished_at = time.time()
            return fork

    def run_parallel(self, fork_ids: List[str],
                     tasks: Dict[str, Callable[[str], tuple]],
                     timeout: float = 30.0) -> List[Fork]:
        """并行运行多个分支，各自执行对应任务。"""
        results: List[Fork] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {}
            for fid in fork_ids:
                task = tasks.get(fid)
                if task is None:
                    raise KeyError(f"分支 {fid} 缺少对应任务")
                futures[pool.submit(self.run, fid, task, timeout)] = fid
            for fut in futures:
                results.append(fut.result())
        # 按完成时间排序保持稳定
        results.sort(key=lambda f: f.finished_at or 0)
        return results

    # ------------------------------------------------------------------
    # 比较与回收
    # ------------------------------------------------------------------
    def compare(self, fork_ids: Optional[List[str]] = None) -> List[Fork]:
        """按得分比较分支，返回降序排序（仅比较终态 completed/failed 的分支）。"""
        forks = [self.get_fork(f) for f in fork_ids] if fork_ids else list(self._forks.values())
        finished = [f for f in forks if f.state in (ForkState.COMPLETED, ForkState.FAILED)]
        # 得分降序，失败分支排最后（score 视为 -inf 概念）
        finished.sort(
            key=lambda f: (f.state == ForkState.COMPLETED, f.score), reverse=True)
        return finished

    def prune(self, fork_ids: Optional[List[str]] = None) -> List[str]:
        """回收失败或指定分支的资源，返回被回收的分支 id。"""
        targets: List[str] = []
        if fork_ids:
            for fid in fork_ids:
                if fid in self._forks:
                    targets.append(fid)
        else:
            targets = [f.id for f in self._forks.values()
                       if f.state in (ForkState.FAILED, ForkState.CANCELLED)]
        removed: List[str] = []
        for fid in targets:
            env = self._envs.pop(fid, None)
            if env:
                _env_cleanup(env)
            # 阶段2：回收分支轨迹
            self.branch_trajectory.delete(fid)
            removed.append(fid)
            self._forks.pop(fid, None)
        return removed

    # ------------------------------------------------------------------
    # 阶段2：分支轨迹持久化
    # ------------------------------------------------------------------
    def record_step(self, fork_id: str, intention: str, decision: str,
                    tool_call: str, tool_params: Optional[Dict[str, Any]] = None,
                    tool_result: Any = None, env_change: str = "",
                    parent_step_index: int = -1) -> Dict[str, Any]:
        """记录一个步骤并持久化到分支轨迹（防篡改链式校验和）。"""
        self.get_fork(fork_id)  # 确保分支存在
        prev_hash = ""
        existing = self.branch_trajectory.load(fork_id)
        if existing:
            prev_hash = existing[-1].get("checksum", "")
        step = {
            "step_id": uuid.uuid4().hex,
            "parent_step_index": parent_step_index,
            "intention": intention,
            "decision": decision,
            "tool_call": tool_call,
            "tool_params": tool_params or {},
            "tool_result": tool_result,
            "env_change": env_change,
            "prev_hash": prev_hash,
            "checksum": "",
        }
        step["checksum"] = sha256_hex(canonical_json(
            {k: v for k, v in step.items() if k != "checksum"}))
        self.branch_trajectory.append(fork_id, step)
        return step

    def get_branch_trajectory(self, fork_id: str) -> List[Dict[str, Any]]:
        """读取分支持久化轨迹。"""
        self.get_fork(fork_id)
        return self.branch_trajectory.load(fork_id)

    def verify_branch_trajectory(self, fork_id: str) -> bool:
        """校验分支轨迹密码学链完整性。"""
        steps = self.branch_trajectory.load(fork_id)
        prev = ""
        for s in steps:
            if s.get("prev_hash") != prev:
                return False
            calc = sha256_hex(canonical_json(
                {k: v for k, v in s.items() if k != "checksum"}))
            if calc != s.get("checksum"):
                return False
            prev = s["checksum"]
        return True

    # ------------------------------------------------------------------
    # 阶段2：进程级并行
    # ------------------------------------------------------------------
    def run_parallel_process(self, tasks: Dict[str, str],
                             timeout: float = 60.0) -> Dict[str, tuple]:
        """以独立进程并行运行多个分支任务。

        tasks: {fork_id: python 源码}；源码中设置 __sb_result__=(score, result)。
        每个分支在独立进程中执行：崩溃/超时不影响其他分支。
        """
        payloads = {}
        for fid, code in tasks.items():
            fork = self.get_fork(fid)
            payloads[fid] = {"code": code, "work_dir": fork.work_dir}
        results = self.process_runner.run_map(payloads)
        # 同步分支状态
        for fid, (status, val) in results.items():
            fork = self._forks.get(fid)
            if fork is None:
                continue
            fork.finished_at = time.time()
            if status == "failed":
                fork.state = ForkState.FAILED
                fork.error = str(val)
            else:
                fork.state = ForkState.COMPLETED
                fork.score = float(status)
                fork.result = val
                # 记录步骤到分支轨迹
                self.record_step(
                    fid, intention="fork task completed",
                    decision=f"score={fork.score}", tool_call="process.run",
                    tool_result=val, env_change="isolated process run")
        return results

    def cancel(self, fork_id: str) -> Fork:
        fork = self.get_fork(fork_id)
        if fork.state not in (ForkState.CREATED, ForkState.RUNNING):
            raise RuntimeError(f"分支 {fork_id} 已处于终态，不可取消")
        fork.state = ForkState.CANCELLED
        fork.finished_at = time.time()
        return fork

# ---------------------------------------------------------------------------
# 阶段2：分支绑定轨迹持久化
# ---------------------------------------------------------------------------
class BranchTrajectoryStore:
    """分支轨迹持久化存储：每个分支拥有独立轨迹文件（阶段2能力）。

    解决阶段1限制「轨迹未持久化到分支」：分支运行期间产生的每一步
    （含子进程内步骤）都追加写入 store_dir/<fork_id>.trajectory.json，
    进程崩溃后轨迹仍可回放校验。
    """

    def __init__(self, store_dir: str):
        self.store_dir = store_dir
        os.makedirs(store_dir, exist_ok=True)

    def _path(self, fork_id: str) -> str:
        return os.path.join(self.store_dir, f"{fork_id}.trajectory.json")

    def append(self, fork_id: str, step: Dict[str, Any]) -> None:
        import json
        path = self._path(fork_id)
        steps = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                steps = json.load(f).get("steps", [])
        steps.append(step)
        tmp = path + f".tmp-{uuid.uuid4().hex}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"fork_id": fork_id, "steps": steps}, f, ensure_ascii=False)
        os.replace(tmp, path)

    def load(self, fork_id: str) -> List[Dict[str, Any]]:
        import json
        path = self._path(fork_id)
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("steps", [])

    def delete(self, fork_id: str) -> None:
        path = self._path(fork_id)
        if os.path.exists(path):
            os.remove(path)


# ---------------------------------------------------------------------------
# 阶段2：进程级并行任务执行器
# ---------------------------------------------------------------------------
def _exec_task_isolated(task_payload: Dict[str, Any], out_queue) -> None:
    """子进程入口：exec 任务代码并回传 (score, result)。

    task_payload: {"code": <python 源码>, "work_dir": <隔离目录>}
    任务代码中通过 ns['__sb_result__'] = (score, result) 返回结果。
    """
    import multiprocessing as _mp  # noqa: F401
    try:
        wd = task_payload.get("work_dir", "")
        if wd and os.path.isdir(wd):
            os.chdir(wd)
        ns: Dict[str, Any] = {"__file__": wd}
        exec(compile(task_payload["code"], "<fork-task>", "exec"), ns)
        out_queue.put(("ok", ns.get("__sb_result__", (0.0, None))))
    except Exception as e:  # noqa: BLE001
        out_queue.put(("error", f"{type(e).__name__}: {e}"))


class ProcessParallelRunner:
    """进程级并行运行器（阶段2）。

    解决阶段1限制「分支执行为同步模拟」：使用 multiprocessing spawn
    上下文，每个分支在独立操作系统进程中运行，崩溃/超时互不影响。
    """

    def __init__(self, max_workers: int = 4, timeout: float = 60.0):
        self.max_workers = max_workers
        self.timeout = timeout
        self._ctx = mp.get_context("spawn")

    def run_map(self, payloads: Dict[str, Dict[str, Any]]) -> Dict[str, tuple]:
        """并行执行多个分支任务，返回 {fork_id: (score, result)}。

        payload: {"code": ..., "work_dir": ...}
        失败分支的值为 ("failed", 错误信息)。
        """
        results: Dict[str, tuple] = {}
        items = list(payloads.items())
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self._run_one, fid, pl): fid
                       for fid, pl in items}
            for fut in futures:
                fid = futures[fut]
                results[fid] = fut.result()
        return results

    def _run_one(self, fork_id: str, payload: Dict[str, Any]) -> tuple:
        q = self._ctx.Queue()
        p = self._ctx.Process(target=_exec_task_isolated, args=(payload, q),
                              daemon=True)
        p.start()
        p.join(self.timeout)
        if p.is_alive():
            p.terminate()
            p.join(5)
            return ("failed", "timeout")
        if q.empty():
            return ("failed", "no result")
        status, val = q.get()
        return (val[0], val[1]) if status == "ok" else ("failed", val)
