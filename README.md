# StateBranch · Agent 底层可分支状态执行引擎

**同一引擎，可插拔隔离后端**：快照 — 分叉 — 比较 — 回滚 — 经验复用闭环。
全部代码仅依赖 Python 标准库（3.9+），零第三方依赖。

## 快速开始

```python
from statebranch.mcp.server import StateBranchService, McpServer

# 创建服务（backend 可选: filesystem / process / docker / microvm）
service = StateBranchService(work_dir=".", backend="process")
server = McpServer(service)

# 快照 → 分支 → 并行 → 择优 → 回滚
snap = server.call("snapshot.create", {"name": "T0"}, 1)["result"]
fork = server.call("fork.create",
                   {"source_snapshot_id": snap["id"], "name": "A"}, 2)["result"]
results = server.call("fork.run_parallel",
                      {"tasks": {fork["id"]: "__sb_result__ = (0.9, 'ok')\n"}}, 3)["result"]
server.call("restore.execute", {"snapshot_id": snap["id"]}, 4)

# 经验固化：成功轨迹 → 技能 → 后续任务回放优先
server.call("skill.extract",
            {"signature": "修复配置并回滚",
             "steps": [{"tool_call": "restore.execute", "tool_params": {},
                        "env_change": "ok"}]}, 5)
plan = server.call("replay.plan", {"signature": "修复配置并回滚"}, 6)["result"]["plan"]
```

运行演示与测试：

```bash
python examples/demo_snapshot_fork_rollback.py  # 阶段1闭环演示
python examples/demo_phase2.py                  # 容器后端/GC/分支轨迹/真并行
python examples/demo_final.py                   # 经验固化/跨平台/评估
python -m unittest discover -s tests -v         # 全部 58 项测试
```

## 架构：同仓库 · 可插拔后端 · 单一引擎

```
statebranch/
├── core/            # 引擎核心（阶段0冻结的数据模型与状态机）
│   ├── types.py     #     数据类型 / RPC 错误码
│   ├── snapshot.py  #     增量快照（SHA-256 去重 / Merkle 根 / 校验和）
│   ├── trajectory.py#     轨迹密码学链（prevHash/checksum 防篡改）
│   ├── fork.py      #     分支调度器 + 一致性校验层 + 分支轨迹持久化
│   ├── gc.py        #     快照垃圾回收（保留策略 / 链压缩差异合并 / 孤儿回收）
│   └── experience.py#     技能库 + 回放优先调度 + 复用统计
├── backend/         # 可插拔隔离后端（统一 BackendAdapter 契约）
│   ├── base.py      #     契约定义 + detect_backend 环境探测
│   ├── filesystem.py#     文件沙箱原型（阶段1）
│   ├── process.py   #     进程隔离（multiprocessing spawn 真并行）
│   ├── docker.py    #     Docker 容器（Engine API run/commit 语义）
│   ├── vm.py        #     微虚拟机（Firecracker snapshot 语义，能力声明）
│   └── android.py   #     安卓无障碍 Agent（节点树快照 / 导航回溯轻量回滚）
├── mcp/server.py    # MCP 接口层（30 个 JSON-RPC 2.0 接口，内嵌/stdio 双模式）
├── eval/benchmark.py# 评估套件（基线 vs 引擎量化对比）
├── examples/        # 端到端演示 ×3
└── tests/           # 58 项单元/集成测试
```

**设计原则**（阶段0冻结）：
1. 同仓库、分后端模块——阶段1与阶段2+是同一引擎换适配器，不拆仓库；
2. 后端可插拔——文件模拟 / 进程 / Docker / 微虚拟机 / 安卓实现同一契约，一参切换；
3. 阶段1为后续铺垫——core 模块全程复用，引擎核心零重构；
4. 职责分层、开关控制、按需启用——阶段1可独立运行，不依赖容器环境。

## MCP 接口（30 个）

| 分组 | 方法 |
|------|------|
| 快照 | `snapshot.create/list/get/delete` |
| 分支 | `fork.create/list/get/compare/prune` |
| 恢复 | `restore.execute/verify` |
| 回放 | `replay.start/pause/stop` |
| 经验 | `recall.search/replay` |
| 垃圾回收 | `gc.run/stats` |
| 分支轨迹 | `fork.record_step/trajectory`、`fork.run_parallel` |
| 后端 | `backend.info` |
| 技能库 | `skill.extract/search/stats`、`replay.plan/confirm` |
| 评估 | `benchmark.run` |

## 后端能力矩阵

| 后端 | 隔离等级 | 并行 | 快照语义 | 特有能力 |
|------|---------|------|---------|---------|
| filesystem | 目录级 | ✗ | 文件 diff | 零依赖，原型/单机 |
| process | OS 进程 | ✓ | 文件 diff | 故障/超时隔离 |
| docker | 容器 | ✓ | 镜像 commit | 真容器隔离（需 daemon） |
| microvm | 独立内核 | ✓ | 内存+磁盘快照 | ms 级快照（需 Firecracker） |
| android | 应用沙箱 | ✗ | 节点树+导航栈 | 无障碍操控、导航回溯轻量回滚 |

## 评估基准（阶段4）

标准任务集（调试修复/配置探索/流程迭代/长任务恢复），基线 vs 引擎：
- Token 节省率 ≈ **65%**（失败回滚到检查点 vs 从零重试）
- 回滚成功率 **100%**（真实执行快照-回滚链路）
- 经验复用率 ~56%（回放优先调度命中）

## 已知限制

- Docker/microvm 适配器在无 daemon/socket 环境自动降级或明确报错，能力差异以 `backend.info` 返回的 capabilities 为准。
- 轨迹密码链防篡改，但不能避免 Agent 本身产生错误决策与错误工具调用记录。
- 安卓后端为无障碍语义模拟，真机接入需替换设备层（契约不变）。

## 许可

MIT
