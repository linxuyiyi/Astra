## Why

当前部署链路可以完成代码获取、依赖安装、迁移和启动，但默认仓库与分支不锁定、健康检查失败仍写入成功标记、生产服务由 oneshot 进程二次托管、配置与数据随发布目录变化，无法满足 Nexus 独立产品的可重复安装与可恢复升级要求。

## What Changes

- 新增唯一生产部署 CLI：`install_nexus.sh` 作为薄封装，编排和探针由标准库 Python 模块实现，提供 install/upgrade/restart/status/health/rollback 与部署记录查询。
- **BREAKING**：部署必须显式指定完整 commit，仓库地址、工作树和 HEAD 不匹配立即失败；失败不修改当前发布。
- **BREAKING**：生产常驻进程由 systemd 每进程一个 unit 管理，停止使用 `astra_service.py` oneshot 二次守护；`astra_service.py pro` 不再迁移、构建或托管服务。
- 版本化发布目录与 `current` 原子切换；部署全程独占锁，迁移只执行一次。
- 配置分层：用户配置、密钥、生成运行配置分离；不覆盖用户配置和已有密钥，不修改全局 OpenCode 配置。
- 数据目录全部外置到发布目录之外，升级不丢失 Task 数据库、workspaces、runtime、config、Nexus API SQLite、Agent Service workspace/qdrant/blob 与 Celery schedule。
- 健康检查改为业务 readiness：HTTP 结构校验、Redis ping、SQL、Celery worker/queue、MCP 认证探测、Nginx 配置与入口路由；仅全部必需项通过才原子写入成功记录。
- 失败恢复：构建失败不影响现网；切换后失败恢复上一可用发布；数据库 revision 变化不得盲目回旧代码，须声明兼容策略或阻断并给出人工恢复步骤。
- 备份使用 SQLite 备份 API + integrity_check；MySQL 使用显式工具并验证恢复条件，不伪装自动回滚。
- 可选 LLM Wiki、远端 Worker 从核心安装分离，各自独立入口、依赖检查与错误报告。
- 文档与配置模板统一为 Nexus，覆盖安装、升级、回退、排障与验证矩阵。

## Capabilities

### New Capabilities

- `nexus-production-deployment`: Nexus 独立产品的生产安装、升级、服务托管、配置与密钥保护、迁移、备份恢复、发布记录、健康验证和可选模块隔离。

### Modified Capabilities

无。`openspec/specs/` 中的历史 Astra 契约不修改。

## Impact

- `ops/deploy/install_nexus.sh`、`ops/deploy/nginx-nexus.conf`、`ops/env/` 模板与部署文档。
- 新增 `ops/deploy/nexus_deploy/` Python 编排模块、systemd unit 模板与测试。
- `ops/dev/astra_service.py`、`ops/dev/preflight.py` 的生产入口行为。
- 不改 Task/Session/Worker 业务状态机、AgentScope 实现与前端业务功能。
