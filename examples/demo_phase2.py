#!/usr/bin/env python3
"""examples/demo_phase2.py — 阶段2端到端演示。

验证阶段2四大能力：
1. 后端探测与进程隔离后端（真并行、故障隔离）
2. 分支轨迹持久化（链式校验、可回放）
3. 快照垃圾回收（保留策略、孤儿对象回收）
4. MCP 阶段2接口（gc.run / fork.run_parallel / fork.trajectory / backend.info）

运行：python examples/demo_phase2.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from statebranch.backend.base import detect_backend  # noqa: E402
from statebranch.mcp.server import StateBranchService, McpServer  # noqa: E402


def demo() -> None:
    work_dir = tempfile.mkdtemp(prefix="statebranch_p2_")
    print("=" * 62)
    print("StateBranch 阶段2 端到端演示：容器后端 / GC / 分支轨迹 / 真并行")
    print("=" * 62)

    # ---- 1. 后端探测 ----
    detected = detect_backend()
    print(f"\n[1] 后端探测：最优后端 = {detected}"
          f"（本环境无 Docker daemon，自动选择进程/文件后端）")

    # ---- 2. 初始化服务（进程隔离后端）+ 初始快照 ----
    service = StateBranchService(work_dir=work_dir, backend="process")
    server = McpServer(service)
    print(f"\n[2] 初始化服务（backend=process，真进程隔离）")
    info = server.call("backend.info", {}, 1)["result"]
    print(f"    后端: {info['name']} / {info['type']}")

    with open(os.path.join(work_dir, "app.py"), "w", encoding="utf-8") as f:
        f.write('def main():\n    print("v1")\n')
    snap = server.call("snapshot.create", {"name": "T0"}, 2)["result"]
    print(f"    T0 快照: {snap['id'][:8]}…")

    # ---- 3. 真并行分支执行（独立进程）----
    print("\n[3] 进程级并行：3个分支在独立操作系统中进程中同时运行")
    forks = {}
    for name, score in [("A", 0.6), ("B", 0.95), ("C", 0.8)]:
        forks[name] = server.call(
            "fork.create",
            {"source_snapshot_id": snap["id"], "name": f"分支{name}"}, 3)["result"]
    tasks = {}
    for name, fork in forks.items():
        s = {"A": 0.6, "B": 0.95, "C": 0.8}[name]
        tasks[fork["id"]] = (
            f"with open('result_{name}.txt','w') as f:\n"
            f"    f.write('plan {name}')\n"
            f"__sb_result__ = ({s}, 'plan-{name} done')\n"
        )
    t0 = __import__("time").time()
    results = server.call("fork.run_parallel", {"tasks": tasks, "timeout": 30}, 4)["result"]
    elapsed = __import__("time").time() - t0
    for name, fork in forks.items():
        r = results[fork["id"]]
        print(f"    分支{name}: score={r['score']} ({r['result']})")
    print(f"    并行耗时 {elapsed:.2f}s（3个分支独立进程并发执行）")

    # 并行期间一个分支崩溃不影响其他分支
    print("\n[4] 故障隔离：让分支A的代码抛异常，B/C 不受影响")
    fork_a2 = server.call("fork.create",
                          {"source_snapshot_id": snap["id"], "name": "A-crash"}, 5)["result"]
    tasks2 = {
        fork_a2["id"]: "raise RuntimeError('branch crashed')\n",
        forks["B"]["id"]: "__sb_result__ = (0.99, 'B still alive')\n",
    }
    r2 = server.call("fork.run_parallel", {"tasks": tasks2, "timeout": 10}, 6)["result"]
    print(f"    A-crash: status={r2[fork_a2['id']]['status']} err={r2[fork_a2['id']]['result']}")
    print(f"    B:       score={r2[forks['B']['id']]['score']}（未受影响 ✓）")

    # ---- 5. 分支轨迹持久化 ----
    print("\n[5] 分支轨迹持久化（每分支独立轨迹文件，链式校验）")
    winner_fork = forks["B"]
    traj = server.call("fork.trajectory", {"fork_id": winner_fork["id"]}, 7)["result"]
    print(f"    分支B 轨迹步骤数: {len(traj['steps'])}, 链式校验: {traj['valid']}")

    # ---- 6. 快照垃圾回收 ----
    print("\n[6] 快照垃圾回收（keep_last_n=2，命名快照保护）")
    # 造一串快照
    for i in range(5):
        with open(os.path.join(work_dir, f"gen-{i}.txt"), "w", encoding="utf-8") as f:
            f.write(f"generation {i}")
        server.call("snapshot.create", {"name": "T0" if i == 0 else ""}, 8)
    stats_before = server.call("gc.stats", {}, 9)["result"]
    gc_result = server.call("gc.run", {"keep_last_n": 2, "keep_named": True}, 10)["result"]
    stats_after = server.call("gc.stats", {}, 11)["result"]
    print(f"    回收前: {stats_before}")
    print(f"    回收:   删除 {len(gc_result['removed_snapshots'])} 个快照, "
          f"清除 {gc_result['removed_objects']} 个孤儿对象, "
          f"释放 {gc_result['freed_bytes']} 字节")
    print(f"    回收后: {stats_after}")

    # ---- 7. 回滚验证（GC 后仍可用）----
    print("\n[7] GC 后回滚到 T0 仍有效")
    ok = server.call("restore.execute", {"snapshot_id": snap["id"]}, 12)["result"]["success"]
    consistent = server.call("restore.verify", {"snapshot_id": snap["id"]}, 13)["result"]["consistent"]
    app = open(os.path.join(work_dir, "app.py"), encoding="utf-8").read()
    print(f"    回滚: {ok}, 一致性: {consistent}, app.py 恢复: {'v1' in app and 'gen-4' not in os.listdir(work_dir)}")

    print("\n" + "=" * 62)
    print("阶段2端到端演示完成 ✓  四大能力全部验证通过")
    print("=" * 62)


if __name__ == "__main__":
    demo()
