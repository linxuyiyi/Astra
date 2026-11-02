## Why

当前 Agent Worker 的一键部署与状态探测叠加了历史 `ssh -> worker` 通道切换、三套冲突的修订快照、双裸进程管理、8 状态 `INTERRUPTED/reconcile` 维护锁、控制面高频拉取巡检以及跨 22 个核心文件的 DSH 分支穿插，已出现保存环境名称抹掉部署态阻断调度、`INTERRUPTED` 前后端 `409` 互锁、`opencode serve` 崩溃无自愈、以及单台过期节点阻塞全局调度等缺陷。需要将部署、守护与状态探测收敛为无锁、自愈且纯 OpenCode 的单一事实源架构。

## What Changes

- **独立分支归档并剥离 DSH（BREAKING）**：在将当前含 DSH 实现归档至独立分支 `archive/dsh-acp` 后，从 `v1.4.9` 主线移除全部 DSH 专属模块与公共文件穿插分支；后续 DSH 演进基于稳定后的 OpenCode 基线另开独立分支维护，不再耦合。
- **期望配置与运行观测物理分表**：环境配置表仅保存管理员提交的期望配置；运行观测快照与目标指纹仅由 Worker Gateway 按 `epoch` 原子写入 Worker 实例表。保存模型、Skill、MCP 或仓库配置时**不自动触发在线热同步**、不影响正在执行的任务，仅由单一就绪纯函数对比期望与观测快照并在不一致时展示“待同步/一键部署”入口；修改环境名称或标签不再破坏就绪态。
- **无 `systemd` 单入口守护与子进程自愈**：保留已验证的 Python 3.11、Node 与 OpenCode 安装引导；废除双裸进程 JSON 记录及历史通道切换，改为单守护进程持有内核文件排他锁并托管 `opencode serve` 子进程（崩溃退避拉起、孤儿进程清理、启动报错直推前端），提供容器启动聚合脚本与离线轻量 SSH 快速唤醒。
- **增量推送探测与单接口列表直出**：下线后台全表拉取巡检与离线 SSH 探测，改为 Worker 建连与心跳通道增量推送本地自检快照；`GET /api/agent-workers` 单接口直出就绪与装备摘要，消除前端列表 N+1 `/health` 请求，并修复调度器候选循环的队头阻塞。
- **5 状态无死锁维护操作（BREAKING）**：移除维护操作 `INTERRUPTED` 状态及 `/reconcile` 接口，状态收敛为 `QUEUED | RUNNING | SUCCEEDED | FAILED | CANCELED`；`enabled` 启停回归同步属性接口，不再进入异步维护队列。

## Capabilities

### New Capabilities
- `nexus-worker-daemon-supervision`: 无 `systemd` 环境下的 Worker 单入口排他守护、`opencode serve` 父子进程保活与孤儿清理、容器聚合启动脚本及离线快速唤醒。
- `nexus-worker-readiness-and-maintenance`: 期望配置与观测快照物理隔离、网关心跳增量推送探测、单一就绪判定与不阻塞调度、5 状态无死锁维护动作及列表单接口状态直出。

### Modified Capabilities
无；`openspec/specs/` 中现存文档为 Astra 历史契约，本次不修改。

## Impact

- **后端**：`services/nexus-api/app/runtime`、`app/worker_agent`、`app/worker_gateway`、`app/api` 及 DSH 相关模块；新增一个 Alembic migration（扩展 `t_nexus_worker_instances` 观测字段并清理废弃维护/通道字段，不修改 `0001_init`）。
- **API 与前端**：`services/nexus-api/web` 员工/Worker 列表与维护抽屉改为单接口读取就绪摘要、移除 `/reconcile` 与列表并发 `/health` 请求、下线 `disable/restore` 异步维护动作。
- **非目标**：不引入 `systemd`，不替换既有 Python 3.11 / Node / OpenCode 制品源与构建引导，不新增保存环境配置时的自动在线热同步。
