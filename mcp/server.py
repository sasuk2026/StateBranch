"""mcp/server.py — MCP 接口层。

基于 JSON-RPC 2.0 规范，对外暴露 StateBranch 的标准接口，屏蔽后端差异。

本层支持两种运行模式：
1. In-process 内嵌模式（阶段1默认）：模块作为 SDK 库在 Agent 同一进程内运行，
   函数直接调用，不产生独立子进程、不监听网络端口，适合直接植入 Agent 应用底层。
2. 可选 stdio / Streamable-HTTP 传输模式：按需启用，此时模块作为独立 MCP
   服务对外提供能力，可供外部第三方 Agent 客户端远程调用。

本阶段演示全部采用内嵌模式。

暴露的 16 个标准接口：
- 快照：snapshot.create / list / get / delete
- 分支：fork.create / list / get / compare / prune
- 恢复：restore.execute / verify
- 回放：replay.start / pause / stop
- 经验：recall.search / replay

所有请求遵循 JSON-RPC 2.0 规范，标准错误码 -32700 至 -32603。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable, Dict, Optional

from ..backend.filesystem import FileSystemBackend
from ..core.snapshot import SnapshotManager
from ..core.fork import ForkScheduler
from ..core.snapshot import SnapshotNotFoundError, SnapshotVerifyError
from ..core.trajectory import TrajectoryRecorder
from ..core.types import RpcErrorCode


class McpError(Exception):
    """带 JSON-RPC 错误码的异常。"""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class StateBranchService:
    """StateBranch 业务服务：聚合后端、快照、分支、轨迹。

    In-process 内嵌模式直接使用本类；独立服务模式通过 McpServer 包装本类。
    """

    def __init__(self, work_dir: str, store_dir: Optional[str] = None,
                 strategy: str = "mixed", backend: str = "filesystem"):
        self.work_dir = work_dir
        store_dir = store_dir or os.path.join(work_dir, ".statebranch")
        # 阶段2：后端可选（filesystem 原型 / process 进程隔离 / docker 容器）
        if backend == "process":
            from ..backend.process import ProcessIsolatedBackend
            self.backend = ProcessIsolatedBackend(
                root_dir=store_dir,
                snapshot_manager=SnapshotManager(
                    os.path.join(store_dir, ".statebranch"), strategy=strategy))
        elif backend == "docker":
            from ..backend.docker import DockerContainerBackend
            self.backend = DockerContainerBackend(store_dir=store_dir)
        else:
            self.backend = FileSystemBackend(store_dir, strategy=strategy)
        self.backend.active_work_dir = work_dir
        self.fork_scheduler = ForkScheduler(self.backend)
        self.trajectory = TrajectoryRecorder(os.path.join(store_dir, "trajectory"))
        # 阶段2：快照垃圾回收器
        from ..core.gc import RetentionPolicy, SnapshotGC
        self.gc = SnapshotGC(self.backend.snapshot_manager, RetentionPolicy())
        # 阶段2(规划)：经验固化引擎与技能库
        from ..core.experience import ExperienceEngine, SkillLibrary
        self.skill_library = SkillLibrary(os.path.join(store_dir, "skills"))
        self.experience = ExperienceEngine(self.skill_library)
        self._replay_state: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _require_work_dir(self):
        import os
        os.makedirs(self.work_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # 方法：快照
    # ------------------------------------------------------------------
    def snapshot_create(self, name: str = "", context: Optional[dict] = None,
                        strategy: Optional[str] = None) -> dict:
        self._require_work_dir()
        snap = self.backend.storeSnapshot(name=name, context=context)
        return snap.to_dict()

    def snapshot_list(self) -> list:
        return [s.to_dict() for s in self.backend.snapshot_manager.list_snapshots()]

    def snapshot_get(self, snapshot_id: str) -> dict:
        try:
            return self.backend.snapshot_manager.get_snapshot(snapshot_id).to_dict()
        except SnapshotNotFoundError:
            raise McpError(RpcErrorCode.SNAPSHOT_NOT_FOUND,
                           f"快照不存在: {snapshot_id}")

    def snapshot_delete(self, snapshot_id: str) -> dict:
        try:
            ok = self.backend.snapshot_manager.delete_snapshot(snapshot_id)
        except (SnapshotNotFoundError, Exception) as e:
            raise McpError(RpcErrorCode.SNAPSHOT_NOT_FOUND, str(e))
        return {"deleted": snapshot_id, "success": ok}

    # ------------------------------------------------------------------
    # 方法：分支
    # ------------------------------------------------------------------
    def fork_create(self, source_snapshot_id: str, name: str = "",
                    strategy_desc: str = "") -> dict:
        try:
            fork = self.fork_scheduler.create_fork(source_snapshot_id, name, strategy_desc)
        except Exception as e:
            raise McpError(RpcErrorCode.INVALID_PARAMS, f"创建分支失败: {e}")
        return fork.to_dict()

    def fork_list(self) -> list:
        return [f.to_dict() for f in self.fork_scheduler.list_forks()]

    def fork_get(self, fork_id: str) -> dict:
        try:
            return self.fork_scheduler.get_fork(fork_id).to_dict()
        except KeyError:
            raise McpError(RpcErrorCode.FORK_NOT_FOUND, f"分支不存在: {fork_id}")

    def fork_compare(self) -> list:
        return [f.to_dict() for f in self.fork_scheduler.compare()]

    def fork_prune(self, fork_ids: Optional[list] = None) -> dict:
        removed = self.fork_scheduler.prune(fork_ids)
        return {"removed": removed}

    # ------------------------------------------------------------------
    # 方法：恢复
    # ------------------------------------------------------------------
    def restore_execute(self, snapshot_id: str) -> dict:
        try:
            ok = self.backend.restoreSnapshot(snapshot_id)
            return {"restored": snapshot_id, "success": ok}
        except SnapshotVerifyError as e:
            raise McpError(RpcErrorCode.VERIFY_FAILED, str(e))
        except SnapshotNotFoundError as e:
            raise McpError(RpcErrorCode.SNAPSHOT_NOT_FOUND, str(e))
        except Exception as e:
            raise McpError(RpcErrorCode.RESTORE_FAILED, str(e))

    def restore_verify(self, snapshot_id: str) -> dict:
        try:
            consistent = self.backend.verifyConsistency(snapshot_id)
            return {"snapshot_id": snapshot_id, "consistent": consistent}
        except Exception as e:
            raise McpError(RpcErrorCode.VERIFY_FAILED, str(e))

    # ------------------------------------------------------------------
    # 方法：回放
    # ------------------------------------------------------------------
    def replay_start(self, trajectory_id: Optional[str] = None) -> dict:
        self._replay_state["running"] = True
        self._replay_state["position"] = 0
        return {"running": True, "position": 0}

    def replay_pause(self) -> dict:
        self._replay_state["running"] = False
        return {"running": False, "position": self._replay_state.get("position", 0)}

    def replay_stop(self) -> dict:
        self._replay_state["running"] = False
        self._replay_state["position"] = 0
        return {"running": False, "position": 0}

    # ------------------------------------------------------------------
    # 方法：经验
    # ------------------------------------------------------------------
    def recall_search(self, keyword: str = "") -> dict:
        steps = self.trajectory.get_steps()
        hits = []
        for s in steps:
            blob = f"{s.intention} {s.decision} {s.tool_call} {s.env_change}"
            if not keyword or keyword in blob:
                hits.append(s.to_dict())
        return {"hits": hits, "count": len(hits)}

    def recall_replay(self, step_id: Optional[str] = None) -> dict:
        if step_id:
            step = self.trajectory.get_step(step_id)
            return {"step": step.to_dict() if step else None}
        # 回放整条轨迹
        return {"steps": [s.to_dict() for s in self.trajectory.get_steps()]}

    # ------------------------------------------------------------------
    # 阶段2方法：垃圾回收 / 分支轨迹 / 进程并行
    # ------------------------------------------------------------------
    def gc_run(self, keep_last_n: Optional[int] = None,
               keep_named: bool = True, keep_max_age: Optional[float] = None) -> dict:
        from ..core.gc import RetentionPolicy
        self.gc.policy = RetentionPolicy(
            keep_last_n=keep_last_n, keep_named=keep_named,
            keep_max_age=keep_max_age)
        return self.gc.run().to_dict()

    def gc_stats(self) -> dict:
        return self.gc.stats()

    def fork_record_step(self, fork_id: str, intention: str, decision: str,
                         tool_call: str, tool_params: Optional[dict] = None,
                         tool_result: Any = None, env_change: str = "") -> dict:
        return self.fork_scheduler.record_step(
            fork_id, intention, decision, tool_call, tool_params,
            tool_result, env_change)

    def fork_trajectory(self, fork_id: str) -> dict:
        return {"fork_id": fork_id,
                "steps": self.fork_scheduler.get_branch_trajectory(fork_id),
                "valid": self.fork_scheduler.verify_branch_trajectory(fork_id)}

    def fork_run_parallel(self, tasks: Dict[str, str],
                          timeout: float = 60.0) -> dict:
        results = self.fork_scheduler.run_parallel_process(tasks, timeout)
        return {fid: {"score": s if s != "failed" else None, "status": s,
                      "result": r if s != "failed" else str(r)}
                for fid, (s, r) in results.items()}

    def backend_info(self) -> dict:
        info = {"name": getattr(self.backend, "name", "filesystem"),
                "type": type(self.backend).__name__}
        if hasattr(self.backend, "capabilities"):
            info["capabilities"] = self.backend.capabilities
        if hasattr(self.backend, "mode"):
            info["mode"] = self.backend.mode
        return info

    # ------------------------------------------------------------------
    # 阶段2(规划)/阶段3/阶段4 方法：经验、技能、评估
    # ------------------------------------------------------------------
    def skill_extract(self, signature: str, steps: list, name: str = "",
                      source_fork_id: str = "") -> dict:
        skill = self.experience.extract_skill(signature, steps, name, source_fork_id)
        return skill.to_dict()

    def skill_search(self, signature: str, top_k: int = 3) -> dict:
        hits = self.skill_library.search(signature, top_k=top_k)
        return {"hits": [s.to_dict() for s in hits]}

    def replay_plan(self, signature: str) -> dict:
        plan = self.experience.plan_from_experience(signature)
        return {"plan": plan}

    def replay_confirm(self, skill_id: str) -> dict:
        self.experience.confirm_replay_success(skill_id)
        return {"confirmed": skill_id}

    def skill_stats(self) -> dict:
        return self.skill_library.stats()

    def benchmark_run(self, seed: int = 42) -> dict:
        from ..eval.benchmark import BenchmarkSuite
        suite = BenchmarkSuite(out_dir=os.path.join(self.work_dir, ".statebranch", "eval"))
        return suite.run(seed=seed)


# ---------------------------------------------------------------------------
# MCP 服务包装：JSON-RPC 2.0 分发
# ---------------------------------------------------------------------------
class McpServer:
    """JSON-RPC 2.0 服务器，支持内嵌调用与 stdio 传输两种模式。"""

    def __init__(self, service: StateBranchService):
        self.service = service
        # 方法路由表：16 个标准接口
        self._methods: Dict[str, Callable[..., Any]] = {
            # 快照
            "snapshot.create": service.snapshot_create,
            "snapshot.list": service.snapshot_list,
            "snapshot.get": service.snapshot_get,
            "snapshot.delete": service.snapshot_delete,
            # 分支
            "fork.create": service.fork_create,
            "fork.list": service.fork_list,
            "fork.get": service.fork_get,
            "fork.compare": service.fork_compare,
            "fork.prune": service.fork_prune,
            # 恢复
            "restore.execute": service.restore_execute,
            "restore.verify": service.restore_verify,
            # 回放
            "replay.start": service.replay_start,
            "replay.pause": service.replay_pause,
            "replay.stop": service.replay_stop,
            # 经验
            "recall.search": service.recall_search,
            "recall.replay": service.recall_replay,
            # 阶段2：垃圾回收
            "gc.run": service.gc_run,
            "gc.stats": service.gc_stats,
            # 阶段2：分支轨迹持久化
            "fork.record_step": service.fork_record_step,
            "fork.trajectory": service.fork_trajectory,
            # 阶段2：进程级并行
            "fork.run_parallel": service.fork_run_parallel,
            # 阶段2：后端信息
            "backend.info": service.backend_info,
            # 阶段2(规划)：经验固化与回放调度
            "skill.extract": service.skill_extract,
            "skill.search": service.skill_search,
            "skill.stats": service.skill_stats,
            "replay.plan": service.replay_plan,
            "replay.confirm": service.replay_confirm,
            # 阶段4：评估
            "benchmark.run": service.benchmark_run,
        }

    # ------------------------------------------------------------------
    # In-process 内嵌调用
    # ------------------------------------------------------------------
    def call(self, method: str, params: Optional[dict] = None,
             request_id: Any = None) -> dict:
        """内嵌模式：直接以 Python 对象方式调用，不经过序列化。"""
        return self._dispatch(method, params or {}, request_id)

    def _dispatch(self, method: str, params: dict, req_id: Any) -> dict:
        resp: Dict[str, Any] = {"jsonrpc": "2.0"}
        if req_id is not None:
            resp["id"] = req_id
        handler = self._methods.get(method)
        if handler is None:
            resp["error"] = {"code": RpcErrorCode.METHOD_NOT_FOUND,
                             "message": f"方法不存在: {method}"}
            return resp
        try:
            result = handler(**params) if isinstance(params, dict) else handler(params)
            resp["result"] = result
        except McpError as e:
            resp["error"] = {"code": e.code, "message": e.message, "data": e.data}
        except TypeError as e:
            resp["error"] = {"code": RpcErrorCode.INVALID_PARAMS, "message": str(e)}
        except Exception as e:  # noqa: BLE001
            resp["error"] = {"code": RpcErrorCode.INTERNAL_ERROR, "message": str(e)}
        return resp

    # ------------------------------------------------------------------
    # stdio 传输模式
    # ------------------------------------------------------------------
    def handle_jsonrpc(self, raw: str) -> str:
        """解析并响应一条 JSON-RPC 2.0 请求（stdio 行协议）。"""
        try:
            req = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"jsonrpc": "2.0", "id": None,
                               "error": {"code": RpcErrorCode.PARSE_ERROR,
                                         "message": "解析错误"}})
        if isinstance(req, list):  # 批处理：逐条分发
            return json.dumps([self._dispatch(r.get("method", ""),
                                              r.get("params", {}),
                                              r.get("id")) for r in req])
        if not isinstance(req, dict):
            return json.dumps({"jsonrpc": "2.0", "id": None,
                               "error": {"code": RpcErrorCode.INVALID_REQUEST,
                                         "message": "无效请求"}})
        return json.dumps(self._dispatch(req.get("method", ""),
                                         req.get("params", {}),
                                         req.get("id")))

    def serve_stdio(self) -> None:
        """stdio 传输模式主循环：逐行读取 JSON 请求并响应。"""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            resp = self.handle_jsonrpc(line)
            sys.stdout.write(resp + "\n")
            sys.stdout.flush()
