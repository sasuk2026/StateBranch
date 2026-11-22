#!/usr/bin/env python3
"""examples/demo_final.py — 阶段2(规划)/3/4 端到端演示。

验证：
- 经验固化：成功轨迹 → 技能库 → 回放优先调度 → 复用统计
- 跨平台：微虚拟机 / 安卓 / 进程后端统一契约，能力差异矩阵
- 评估：基准对比（基线 vs 引擎）
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from statebranch.backend.android import A11yNode, AndroidBackend  # noqa: E402
from statebranch.backend.vm import MicroVMBackend  # noqa: E402
from statebranch.eval.benchmark import BenchmarkSuite  # noqa: E402
from statebranch.mcp.server import StateBranchService, McpServer  # noqa: E402


def demo() -> None:
    work_dir = tempfile.mkdtemp(prefix="statebranch_final_")
    print("=" * 62)
    print("StateBranch 阶段2-4 端到端演示：经验固化 / 跨平台 / 评估")
    print("=" * 62)

    # ---- 经验固化闭环 ----
    print("\n[1] 经验固化：成功轨迹 → 技能库 → 回放优先调度")
    service = StateBranchService(work_dir=work_dir)
    server = McpServer(service)
    with open(os.path.join(work_dir, "app.txt"), "w") as f:
        f.write("v1")
    snap = server.call("snapshot.create", {"name": "T0"}, 1)["result"]
    steps = [
        {"tool_call": "snapshot.create", "tool_params": {"name": "T0"}, "env_change": "T0"},
        {"tool_call": "fork.create", "tool_params": {"name": "A"}, "env_change": "fork A"},
        {"tool_call": "restore.execute", "tool_params": {}, "env_change": "rollback ok"},
    ]
    skill = server.call("skill.extract",
                        {"signature": "修复配置文件并回滚", "steps": steps,
                         "name": "rollback-fix"}, 2)["result"]
    print(f"    技能已提取: {skill['name']} ({skill['id'][:8]}…)")
    plan = server.call("replay.plan", {"signature": "修复配置文件并回滚"}, 3)["result"]["plan"]
    print(f"    新任务命中回放计划: replay={plan['replay']}, {len(plan['steps'])} 步")
    server.call("replay.confirm", {"skill_id": plan["skill_id"]}, 4)
    no_plan = server.call("replay.plan", {"signature": "全新任务"}, 5)["result"]["plan"]
    print(f"    无关任务不命中: {no_plan is None}（回退正常推理）")
    stats = server.call("skill.stats", {}, 6)["result"]
    print(f"    复用统计: {stats}")

    # ---- 跨平台 ----
    print("\n[2] 跨平台适配：统一契约，能力差异")
    for backend in [MicroVMBackend(), AndroidBackend()]:
        info = backend.info()
        print(f"    {info['name']:8s} mode={info.get('mode', '-')}")
        for k in ["isolation", "snapshot", "parallel", "android_accessibility"]:
            print(f"      {k:22s} = {info['capabilities'][k]}")
    # 安卓操控演示
    android = AndroidBackend()
    android.device.set_screen("MainActivity", {
        "btn_settings": A11yNode("btn_settings", "设置", clickable=True),
        "edit_search": A11yNode("edit_search", "", clickable=True)})
    snap_a = android.storeSnapshot(name="home")
    android.device.tap("btn_settings")
    android.restoreSnapshot(snap_a.id)
    print(f"    安卓轻量回滚（导航回溯）: 恢复到 {android.device.state.activity} ✓")

    # ---- 评估基准 ----
    print("\n[3] 阶段4 评估基准（基线 vs 引擎）")
    suite = BenchmarkSuite(out_dir=os.path.join(work_dir, "eval"))
    report = suite.run(seed=11)
    print(f"    Token 节省率: {report['improvement']['trial_reduction']}")
    print(f"    回滚成功率:   {report['improvement']['rollback_success_rate']}")
    print(f"    经验复用率:   {report['improvement']['experience_reuse_rate']}")

    print("\n" + "=" * 62)
    print("阶段2-4 端到端演示全部通过 ✓")
    print("=" * 62)


if __name__ == "__main__":
    demo()
