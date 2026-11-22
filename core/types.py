"""StateBranch 公共数据类型与常量定义。

阶段1聚焦容器后端原型验证，所有模块基于此处的共享类型协作，
避免循环依赖，并保证数据模型与 MCP 接口层一致。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# 存储策略常量（与阶段0设计一致）
# ---------------------------------------------------------------------------
class StorageStrategy:
    """快照存储策略。"""

    COW = "cow"          # 写时复制 Copy-on-Write
    ROW = "row"          # 写时重定向 Redirect-on-Write
    MIXED = "mixed"      # 混合策略


# ---------------------------------------------------------------------------
# 分支状态机常量（与阶段0 / 阶段1研究报告 2.3 一致）
# ---------------------------------------------------------------------------
class ForkState:
    """分支状态机：created → running → completed | failed | cancelled。"""

    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# MCP 错误码（JSON-RPC 2.0 标准 + 自定义扩展）
# ---------------------------------------------------------------------------
class RpcErrorCode:
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    # 业务扩展错误码
    SNAPSHOT_NOT_FOUND = -32001
    FORK_NOT_FOUND = -32002
    VERIFY_FAILED = -32003
    RESTORE_FAILED = -32004
    TRAJECTORY_INVALID = -32005


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class Snapshot:
    """增量快照对象。

    attributes:
        id: 快照唯一标识
        parent_id: 父快照 id，根快照为 None
        name: 快照名称（可检索）
        timestamp: 创建时间戳（epoch 秒）
        storage_strategy: 存储策略（cow/row/mixed）
        fs_diff: 相对父快照的文件差异 {path: DiffEntry}
        checksum: 本快照的 SHA-256 校验和
        merkle_root: 内容寻址树的 Merkle 根哈希
        context: 附加的 Agent 上下文快照
    """

    id: str
    parent_id: Optional[str]
    name: str
    timestamp: float
    storage_strategy: str = StorageStrategy.MIXED
    fs_diff: Dict[str, "DiffEntry"] = field(default_factory=dict)
    checksum: str = ""
    merkle_root: str = ""
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "name": self.name,
            "timestamp": self.timestamp,
            "storage_strategy": self.storage_strategy,
            "fs_diff": {k: v.to_dict() for k, v in self.fs_diff.items()},
            "checksum": self.checksum,
            "merkle_root": self.merkle_root,
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Snapshot":
        return cls(
            id=data["id"],
            parent_id=data.get("parent_id"),
            name=data.get("name", ""),
            timestamp=data.get("timestamp", 0.0),
            storage_strategy=data.get("storage_strategy", StorageStrategy.MIXED),
            fs_diff={k: DiffEntry.from_dict(v) for k, v in data.get("fs_diff", {}).items()},
            checksum=data.get("checksum", ""),
            merkle_root=data.get("merkle_root", ""),
            context=data.get("context", {}),
        )


@dataclass
class DiffEntry:
    """单个文件路径的差异条目。

    attributes:
        kind: "added" | "modified" | "deleted"
        content_hash: 文件内容的 SHA-256（deleted 时为空）
        content: 文件内容（added/modified 时内联；deleted 时为 None）
        path: 文件相对路径
    """

    kind: str
    path: str
    content_hash: str = ""
    content: Optional[bytes] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "content_hash": self.content_hash,
            # bytes 不能直接 JSON 序列化，编码为 base64 字符串
            "content_b64": _b64encode(self.content) if self.content is not None else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DiffEntry":
        return cls(
            kind=data["kind"],
            path=data["path"],
            content_hash=data.get("content_hash", ""),
            content=_b64decode(data.get("content_b64")),
        )


@dataclass
class TrajectoryStep:
    """动作轨迹中的单个步骤。

    attributes:
        step_id: 步骤唯一标识
        parent_step_index: 父步骤在轨迹中的索引，根步骤为 -1
        intention: 该步骤的意图
        decision: 模型决策
        tool_call: 调用的工具
        tool_params: 工具参数
        tool_result: 工具返回结果
        env_change: 环境变更摘要
        prev_hash: 前一步骤的校验和
        checksum: 本步骤校验和（含前序校验和与本步骤所有字段）
    """

    step_id: str
    parent_step_index: int
    intention: str
    decision: str
    tool_call: str
    tool_params: Dict[str, Any]
    tool_result: Any
    env_change: str
    prev_hash: str = ""
    checksum: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "parent_step_index": self.parent_step_index,
            "intention": self.intention,
            "decision": self.decision,
            "tool_call": self.tool_call,
            "tool_params": self.tool_params,
            "tool_result": self.tool_result,
            "env_change": self.env_change,
            "prev_hash": self.prev_hash,
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrajectoryStep":
        return cls(
            step_id=data["step_id"],
            parent_step_index=data.get("parent_step_index", -1),
            intention=data.get("intention", ""),
            decision=data.get("decision", ""),
            tool_call=data.get("tool_call", ""),
            tool_params=data.get("tool_params", {}),
            tool_result=data.get("tool_result"),
            env_change=data.get("env_change", ""),
            prev_hash=data.get("prev_hash", ""),
            checksum=data.get("checksum", ""),
        )


# ---------------------------------------------------------------------------
# 哈希 / 序列化工具
# ---------------------------------------------------------------------------
def sha256_hex(data: bytes) -> str:
    """计算字节内容的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> bytes:
    """生成用于校验和的规范化 JSON 字节（key 排序保证确定性）。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _b64encode(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode("ascii")


def _b64decode(data: Optional[str]) -> Optional[bytes]:
    import base64
    if data is None:
        return None
    return base64.b64decode(data)
