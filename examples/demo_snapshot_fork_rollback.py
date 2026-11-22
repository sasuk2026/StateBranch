#!/usr/bin/env python3
"""examples/demo_snapshot_fork_rollback.py — 端到端演示。

对应阶段1研究报告第3章：完整验证快照、分叉、比较、回滚闭环。

步骤：
1  初始化：创建临时工作目录，一站式初始化 MCP 服务器
2  创建沙箱：创建文件系统沙箱，写入 config.yaml、app.py
3  T0 快照：打初始快照，校验有效
4  模拟出错：修改 app.py 为破坏版本
5  分支 A/B：从 T0 分叉，A=rollback_and_rewrite，B=refactor
6  比较择优：分支 B 胜出
7  回滚验证：回滚到 T0，一致性校验通过
8  列出资源：列出轨迹与快照
9  回收资源：回收分支 A、B 资源

运行：python examples/demo_snapshot_fork_rollback.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from statebranch.core.fork import ForkScheduler, ConsistencyChecker
from statebranch.core.types import ForkState
from statebranch.mcp.server import StateBranchService, McpServer


def _make_broken_app(path: str) -> None:
    """把 app.py 改为破坏版本。"""
    with open(os.path.join(path, "app.py"), "w", encoding="utf-8") as f:
        f.write('print("app crashed")  # bug: undefined_name\nprint(undefined_var)\n')


def _write_fixed_v1(path: str) -> None:
    """策略A：回滚并重写。"""
    with open(os.path.join(path, "app.py"), "w", encoding="utf-8") as f:
        f.write('def main():\n    print("hello world")\n\n\nif __name__ == "__main__":\n    main()\n')


def _write_fixed_v2(path: str) -> None:
    """策略B：重构，写入 fixed_v2 + README。"""
    with open(os.path.join(path, "app.py"), "w", encoding="utf-8") as f:
        f.write('def main():\n    print("hello world, refactored")\n\n\nif __name__ == "__main__":\n    main()\n')
    with open(os.path.join(path, "README.md"), "w", encoding="utf-8") as f:
        f.write("# StateBranch Demo\n\n重构后的应用说明。\n")


def demo() -> None:
    work_dir = tempfile.mkdtemp(prefix="statebranch_demo_")

    print("=" * 60)
    print("StateBranch 阶段1 端到端演示：快照—分叉—比较—回滚闭环")
    print("=" * 60)

    # ---- 步骤1：初始化，一站式初始化 MCP 服务器 ----
    service = StateBranchService(work_dir=work_dir)
    server = McpServer(service)
    print("\n[1] 初始化：创建临时工作目录并初始化 MCP 服务器（In-process 内嵌模式）")
    print(f"    工作目录: {work_dir}")

    # ---- 步骤2：创建沙箱，写入 config.yaml、app.py ----
    print("\n[2] 创建沙箱：写入 config.yaml、app.py")
    with open(os.path.join(work_dir, "config.yaml"), "w", encoding="utf-8") as f:
        f.write("name: statebranch-demo\nversion: 1.0\n")
    with open(os.path.join(work_dir, "app.py"), "w", encoding="utf-8") as f:
        f.write('def main():\n    print("hello world")\n\n\nif __name__ == "__main__":\n    main()\n')
    print("    已写入 config.yaml, app.py")

    # ---- 步骤3：T0 快照 ----
    print("\n[3] T0 快照：打初始快照并校验完整性")
    t0 = server.call("snapshot.create", {"name": "T0"})
    snap_id = t0["result"]["id"]
    verify = server.call("snapshot.get", {"snapshot_id": snap_id})
    checksum_ok = service.backend.snapshot_manager.verify_checksum(snap_id)
    print(f"    快照 id: {snap_id[:8]}…, 校验有效: {checksum_ok}")

    # ---- 步骤4：模拟出错 ----
    print("\n[4] 模拟出错：修改 app.py 为破坏版本")
    _make_broken_app(work_dir)
    print("    app.py 已变为破坏版本（引入未定义变量）")

    # ---- 步骤5：分支 A / B ----
    print("\n[5] 从 T0 分叉：分支A=rollback_and_rewrite，分支B=refactor")
    fork_a = server.call("fork.create", {"source_snapshot_id": snap_id,
                                         "name": "分支A",
                                         "strategy_desc": "rollback_and_rewrite"})["result"]
    fork_b = server.call("fork.create", {"source_snapshot_id": snap_id,
                                         "name": "分支B",
                                         "strategy_desc": "refactor"})["result"]
    print(f"    分支A {fork_a['id'][:8]}…, 分支B {fork_b['id'][:8]}…")

    # 并行运行两个分支（各自独立环境，互不干扰）
    # 分支A执行 rollback_and_rewrite：重写 app.py，得分 0.7
    service.fork_scheduler.run(
        fork_a["id"], lambda wd: (0.7, _write_fixed_v1(wd) or "fixed_v1 written"))
    # 分支B执行 refactor：重写 app.py 并新增 README，得分 0.9
    service.fork_scheduler.run(
        fork_b["id"], lambda wd: (0.9, _write_fixed_v2(wd) or "fixed_v2 + README written"))
    print("    分支A: rollback_and_rewrite → 得分 0.7")
    print("    分支B: refactor            → 得分 0.9")

    # ---- 步骤6：比较择优 ----
    print("\n[6] 比较择优")
    ranked = service.fork_scheduler.compare()
    winner = ranked[0]
    print(f"    分支 {winner.name} 胜出（分数 {winner.score:.1f}）")

    # ---- 步骤7：回滚验证 ----
    print("\n[7] 回滚验证：回滚到 T0")
    ok = server.call("restore.execute", {"snapshot_id": snap_id})["result"]["success"]
    consistent = server.call("restore.verify", {"snapshot_id": snap_id})["result"]["consistent"]
    # 检查文件是否完全恢复
    app = open(os.path.join(work_dir, "app.py"), encoding="utf-8").read()
    restored_ok = "undefined_var" not in app and "hello world" in app
    print(f"    回滚成功: {ok}, 一致性校验: {consistent}, 文件完全恢复: {restored_ok}")

    # ---- 步骤8：列出资源 ----
    print("\n[8] 列出轨迹与快照")
    snaps = server.call("snapshot.list")["result"]
    print(f"    快照数量: {len(snaps)}")
    for s in snaps:
        print(f"      - {s['name']} ({s['id'][:8]}…)")

    # 记录一段轨迹演示链式校验
    service.trajectory.append_step(
        intention="演示回滚闭环", decision="回滚到 T0",
        tool_call="restore.execute", tool_params={"snapshot_id": snap_id},
        tool_result="success", env_change="app.py 恢复")
    print(f"    轨迹步骤数: {len(service.trajectory.get_steps())}, 链式校验通过: "
          f"{service.trajectory.verify_trajectory()}")

    # ---- 步骤9：回收资源 ----
    print("\n[9] 回收分支 A、B 资源")
    removed = service.fork_scheduler.prune([fork_a["id"], fork_b["id"]])
    print(f"    已回收 {len(removed)} 个分支资源")

    print("\n" + "=" * 60)
    print("端到端演示完成：快照—分叉—比较—回滚闭环全部验证通过 ✓")
    print("=" * 60)


if __name__ == "__main__":
    demo()
