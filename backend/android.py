"""backend/android.py — 安卓无障碍操控 Agent 后端适配器（阶段3·规划 4.4）。

安卓端特点与处理（移动端权限与环境限制）：
- 状态采集：以「无障碍节点树快照」替代文件系统状态——每个 UI 节点
  (view_id, text, clickable, bounds) 是状态原子；差异 = 节点树 diff。
- 轨迹记录：意图/决策/操控动作（tap/swipe/input）+ 前后节点树变化。
- 轻量回滚：UI 状态不可像文件一样任意重写，回滚语义是「导航回溯」——
  记录导航路径（activity 栈等价物），回滚 = 按路径反向操控。
- 权限限制：无障碍服务权限、后台启动限制在适配器内以 capability 声明。

本实现提供可运行的无障碍语义模拟（节点树 + 操控动作 + 导航栈），
接口与 docker/process/microvm 完全一致（BackendAdapter 契约）。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..core.snapshot import SnapshotManager
from ..core.types import canonical_json, sha256_hex
from .base import BackendAdapter


# ---------------------------------------------------------------------------
# 安卓状态模型：无障碍节点树
# ---------------------------------------------------------------------------
@dataclass
class A11yNode:
    """无障碍节点。"""

    view_id: str
    text: str = ""
    clickable: bool = False
    bounds: str = "[0,0][0,0]"

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class AndroidState:
    """安卓状态：节点树 + 导航栈。"""

    package: str
    activity: str                      # 当前界面（导航栈顶）
    nodes: Dict[str, A11yNode] = field(default_factory=dict)
    nav_stack: List[str] = field(default_factory=list)  # 界面导航栈

    def fingerprint(self) -> str:
        """状态指纹（一致性校验用）。"""
        payload = {
            "package": self.package, "activity": self.activity,
            "nav": self.nav_stack,
            "nodes": sorted(k for k, v in self.nodes.items()),
        }
        return sha256_hex(canonical_json(payload))

    def to_dict(self) -> dict:
        return {"package": self.package, "activity": self.activity,
                "nav_stack": self.nav_stack,
                "nodes": {k: v.to_dict() for k, v in self.nodes.items()}}

    @classmethod
    def from_dict(cls, d: dict) -> "AndroidState":
        return cls(
            package=d["package"], activity=d["activity"],
            nav_stack=list(d.get("nav_stack", [])),
            nodes={k: A11yNode(**v) for k, v in d.get("nodes", {}).items()})


class AndroidDeviceSimulator:
    """安卓设备模拟器：无障碍节点树 + 导航栈 + 操控动作。"""

    def __init__(self):
        self.state = AndroidState(package="com.example.app", activity="MainActivity")

    def tap(self, view_id: str) -> Dict[str, Any]:
        """点击节点：可点击节点触发界面跳转（模拟）。"""
        node = self.state.nodes.get(view_id)
        if node and node.clickable:
            target = f"Activity_{view_id}"
            self.state.activity = target
            self.state.nav_stack.append(target)
            return {"action": "tap", "view_id": view_id, "navigated": True,
                    "activity": target}
        return {"action": "tap", "view_id": view_id, "navigated": False}

    def input_text(self, view_id: str, text: str) -> Dict[str, Any]:
        node = self.state.nodes.get(view_id)
        if node:
            node.text = text
        return {"action": "input", "view_id": view_id, "text_set": bool(node)}

    def back(self) -> Dict[str, Any]:
        """返回键：导航栈回退（轻量回滚原语）。"""
        if len(self.state.nav_stack) > 1:
            self.state.nav_stack.pop()
            self.state.activity = self.state.nav_stack[-1]
        else:
            self.state.activity = self.state.nav_stack[0] if self.state.nav_stack else "MainActivity"
        return {"action": "back", "activity": self.state.activity}

    def set_screen(self, activity: str, nodes: Dict[str, A11yNode]) -> None:
        """（模拟器）直接设置界面与节点树。"""
        self.state.activity = activity
        self.state.nodes = nodes
        if not self.state.nav_stack or self.state.nav_stack[-1] != activity:
            self.state.nav_stack.append(activity)


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------
class AndroidBackend(BackendAdapter):
    """安卓无障碍操控 Agent 后端。"""

    name = "android"

    capabilities = {
        "isolation": "app-sandbox",          # 安卓应用沙箱
        "snapshot": "a11y-tree+nav",         # 节点树+导航栈快照（轻量）
        "parallel": False,                   # 单设备无并行分支
        "process_isolation": False,          # 无独立内核/进程
        "android_accessibility": True,
        "snapshot_latency": "ms-level",
        "permissions": ["accessibility-service", "foreground-service"],
    }

    def __init__(self, store_dir: Optional[str] = None):
        self.store_dir = os.path.abspath(store_dir or tempfile.mkdtemp(prefix="sb_android_"))
        os.makedirs(self.store_dir, exist_ok=True)
        self.device = AndroidDeviceSimulator()
        # 快照元数据以 JSON 存（复用 SnapshotManager 的元数据目录约定，
        # 但状态是安卓语义，因此直接内联存储）
        self._meta_dir = os.path.join(self.store_dir, "snapshots")
        os.makedirs(self._meta_dir, exist_ok=True)
        self._envs: Dict[str, AndroidState] = {}
        self.trajectory: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 契约实现（安卓语义）
    # ------------------------------------------------------------------
    def collectState(self) -> AndroidState:
        """状态采集：无障碍节点树 + 导航栈。"""
        return copy.deepcopy(self.device.state)

    def storeSnapshot(self, name: str = "", context: Optional[dict] = None):
        state = self.collectState()
        snap = {
            "id": uuid.uuid4().hex,
            "name": name,
            "timestamp": time.time(),
            "kind": "android-a11y",
            "fingerprint": state.fingerprint(),
            "state": state.to_dict(),
            "context": context or {},
            "parent_id": getattr(self, "_last_parent", None),
        }
        self._last_parent = snap["id"]
        path = os.path.join(self._meta_dir, f"{snap['id']}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, path)
        return _AndroidSnapshot(snap)

    def restoreSnapshot(self, snapshot_id: str) -> bool:
        """轻量回滚：恢复节点树并按导航栈反向操控回目标界面。"""
        snap = self._load(snapshot_id)
        state = AndroidState.from_dict(snap["state"])
        # 反向导航到目标 activity（轻量回滚语义）
        while self.device.state.activity != state.activity and self.device.state.nav_stack:
            self.device.back()
        self.device.state.activity = state.activity
        self.device.state.nodes = copy.deepcopy(state.nodes)
        self.device.state.nav_stack = list(state.nav_stack)
        return True

    def createIsolatedEnv(self, snapshot_id: str) -> Dict[str, Any]:
        """分支 = 快照状态的独立副本（单设备，环境级隔离）。"""
        snap = self._load(snapshot_id)
        state = AndroidState.from_dict(snap["state"])
        env_id = uuid.uuid4().hex
        self._envs[env_id] = state
        return {"fork_env_id": env_id, "state": state}

    def run_in_env(self, env: Dict[str, Any], task_payload: Dict[str, Any],
                   timeout: float = 60.0) -> tuple:
        """在分支状态副本上执行操控序列。

        task_payload: {"actions": [{"action":"tap","view_id":...}, ...]}
        返回 (score, result)。
        """
        work_state: AndroidState = copy.deepcopy(env["state"])
        sim = AndroidDeviceSimulator()
        sim.state = copy.deepcopy(work_state)
        results = []
        for act in task_payload.get("actions", []):
            a = act.get("action")
            if a == "tap":
                results.append(sim.tap(act["view_id"]))
            elif a == "input":
                results.append(sim.input_text(act["view_id"], act.get("text", "")))
            elif a == "back":
                results.append(sim.back())
            # 轨迹记录
            self.trajectory.append({
                "action": act, "after_activity": sim.state.activity,
                "ts": time.time()})
        score = float(task_payload.get("score", 0.0))
        return score, {"final_activity": sim.state.activity, "actions": results}

    def verifyConsistency(self, snapshot_id: str, env: Any = None) -> bool:
        """一致性校验：当前状态指纹 vs 快照指纹。"""
        try:
            snap = self._load(snapshot_id)
        except FileNotFoundError:
            return False
        if isinstance(env, dict) and "state" in env:
            return env["state"].fingerprint() == snap["fingerprint"]
        return self.collectState().fingerprint() == snap["fingerprint"]

    def cleanup_env(self, env: Dict[str, Any]) -> None:
        self._envs.pop(env.get("fork_env_id"), None)

    # ------------------------------------------------------------------
    def _load(self, snapshot_id: str) -> dict:
        path = os.path.join(self._meta_dir, f"{snapshot_id}.json")
        if not os.path.exists(path):
            raise FileNotFoundError(f"快照不存在: {snapshot_id}")
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def list_snapshots(self) -> List[dict]:
        out = []
        for n in os.listdir(self._meta_dir):
            if n.endswith(".json"):
                with open(os.path.join(self._meta_dir, n), encoding="utf-8") as f:
                    out.append(json.load(f))
        out.sort(key=lambda s: s["timestamp"])
        return out

    def info(self) -> dict:
        return {"name": self.name, "capabilities": self.capabilities}


class _AndroidSnapshot:
    """安卓快照句柄（对齐 SnapshotManager 返回形态）。"""

    def __init__(self, data: dict):
        self.id = data["id"]
        self.name = data.get("name", "")
        self.fingerprint = data.get("fingerprint", "")
        self.timestamp = data.get("timestamp", 0.0)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name,
                "fingerprint": self.fingerprint, "kind": "android-a11y"}
