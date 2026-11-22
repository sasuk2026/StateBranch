"""core/trajectory.py — 动作轨迹固化模块。

职责：记录意图、决策、工具调用、返回与环境变更，形成不可篡改的执行时间线。

关键设计：
- 密码学链式记录：每个步骤包含 prevHash（前一步骤校验和）和 checksum
  （本步骤校验和，包含前序校验和与本步骤所有字段）。
- 防篡改：任何对历史步骤的篡改都会断裂链条，在 verify_trajectory() 时被检测。
- 步骤树：支持 parentStepIndex 构建步骤树，根步骤为 -1。

注：密码学轨迹链防止历史日志被篡改，但无法阻止 Agent 输出错误的工具执行日志
（文档约束，逻辑机制在阶段2增强）。
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, List, Optional

from .types import TrajectoryStep, canonical_json, sha256_hex


class TrajectoryError(Exception):
    """轨迹相关异常基类。"""


class TrajectoryVerifyError(TrajectoryError):
    """轨迹完整性校验失败。"""


class TrajectoryRecorder:
    """动作轨迹固化器：以密码学链式结构记录不可篡改的执行时间线。"""

    def __init__(self, store_dir: str):
        self.store_dir = store_dir
        os.makedirs(store_dir, exist_ok=True)
        self._steps: List[TrajectoryStep] = []
        self._load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def _path(self) -> str:
        return os.path.join(self.store_dir, "trajectory.json")

    def _save(self) -> None:
        payload = {"steps": [s.to_dict() for s in self._steps]}
        tmp = self._path() + f".tmp-{uuid.uuid4().hex}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, self._path())

    def _load(self) -> None:
        if os.path.exists(self._path()):
            with open(self._path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            self._steps = [TrajectoryStep.from_dict(s) for s in data.get("steps", [])]
        else:
            self._steps = []

    # ------------------------------------------------------------------
    # 步骤校验和
    # ------------------------------------------------------------------
    @staticmethod
    def _step_canonical(step: TrajectoryStep) -> bytes:
        payload = {
            "step_id": step.step_id,
            "parent_step_index": step.parent_step_index,
            "intention": step.intention,
            "decision": step.decision,
            "tool_call": step.tool_call,
            "tool_params": step.tool_params,
            "tool_result": step.tool_result,
            "env_change": step.env_change,
            "prev_hash": step.prev_hash,
        }
        return canonical_json(payload)

    # ------------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------------
    def append_step(
        self,
        intention: str,
        decision: str,
        tool_call: str,
        tool_params: Optional[Dict[str, Any]] = None,
        tool_result: Any = None,
        env_change: str = "",
        parent_step_index: int = -1,
    ) -> TrajectoryStep:
        """追加一个轨迹步骤。

        链式校验和：checksum(N) = SHA-256(步骤 N 所有字段 + prevHash(N))，
        其中 prevHash(N) = checksum(N-1)。
        """
        prev_hash = self._steps[-1].checksum if self._steps else ""
        step = TrajectoryStep(
            step_id=uuid.uuid4().hex,
            parent_step_index=parent_step_index,
            intention=intention,
            decision=decision,
            tool_call=tool_call,
            tool_params=tool_params or {},
            tool_result=tool_result,
            env_change=env_change,
            prev_hash=prev_hash,
            checksum="",
        )
        step.checksum = sha256_hex(self._step_canonical(step))
        self._steps.append(step)
        self._save()
        return step

    # ------------------------------------------------------------------
    # 校验与查询
    # ------------------------------------------------------------------
    def verify_trajectory(self) -> bool:
        """验证整条轨迹的密码学链完整性。

        任何对历史步骤的篡改（字段改动、步骤删除/插入/重排）都会导致
        其后续所有步骤的校验和不匹配，返回 False。
        """
        prev_hash = ""
        for step in self._steps:
            if step.prev_hash != prev_hash:
                return False
            computed = sha256_hex(self._step_canonical(step))
            if computed != step.checksum:
                return False
            prev_hash = step.checksum
        return True

    def get_steps(self) -> List[TrajectoryStep]:
        return list(self._steps)

    def get_step(self, step_id: str) -> Optional[TrajectoryStep]:
        for s in self._steps:
            if s.step_id == step_id:
                return s
        return None

    def build_tree(self) -> Dict[str, Any]:
        """基于 parentStepIndex 构建步骤树（根步骤 parent=-1）。"""
        children: Dict[int, List[TrajectoryStep]] = {}
        for i, s in enumerate(self._steps):
            children.setdefault(s.parent_step_index, []).append((i, s))
        roots = children.get(-1, [])

        def node(idx, step):
            return {
                "step_id": step.step_id,
                "index": idx,
                "intention": step.intention,
                "tool_call": step.tool_call,
                "children": [node(i, s) for i, s in children.get(idx, [])],
            }

        return {"roots": [node(i, s) for i, s in roots]}

    def clear(self) -> None:
        self._steps = []
        if os.path.exists(self._path()):
            os.remove(self._path())
