## Context

现有 `install_nexus.sh` 同时承担代码获取、依赖安装、配置生成、迁移、systemd/Python 双路径启动和健康检查，默认分支与失败判定不满足独立产品部署要求。Nexus 的生产常驻进程由 systemd 管理已有基础，但 `nexus-astra` oneshot 又托管 Backend/Celery/MCP 等进程，配置与数据仍落在发布目录内。

## Goals / Non-Goals

**Goals:**

- 一条可重复、可验证、可恢复的生产部署路径。
- 单一服务定义驱动 install/upgrade/restart/status/health。
- 发布目录不可变、数据外置、配置与密钥持久化。
- 迁移、备份、发布记录和入口 readiness 都有明确失败语义。

**Non-Goals:**

- 不迁移旧 Astra 数据，不兼容旧部署行为。
- 不重构 Task/Session/Worker 状态机，不改 AgentScope 与前端业务功能。
- 不承诺数据库自动无损降级，不引入 Kubernetes。

## Decisions

### 编排语言与入口

`ops/deploy/install_nexus.sh` 保留为兼容入口的薄封装，只解析少量参数并调用 `ops/deploy/nexus_deploy` CLI。复杂逻辑用 Python 标准库实现，避免 Bash 下的状态、锁和 JSON 记录难以测试。

考虑过继续扩写 Bash：被否决，原因是部署锁、原子记录、探针解析和故障注入测试成本过高。

### 发布与数据布局

- `NEXUS_RELEASES_DIR`（默认 `/opt/nexus/releases`）保存不可变 `${commit}` 目录。
- `NEXUS_CURRENT_LINK`（默认 `/opt/nexus/current`）通过临时符号链接 + `os.replace` 原子切换。
- 用户配置 `NEXUS_CONFIG_DIR`（默认 `/etc/nexus`）：`nexus.env` 与 `secrets.env`。
- 数据 `NEXUS_DATA_DIR`（默认 `/var/lib/nexus`）、日志 `NEXUS_LOG_DIR`（默认 `/var/log/nexus`）、状态 `NEXUS_STATE_DIR`（默认 `/var/lib/nexus`）。
- Task 数据外置为 `ASTRA_DATA_DIR`、`ASTRA_WORKSPACE_ROOT`、`ASTRA_RUNTIME_DIR`、`ASTRA_CONFIG_DIR`；Nexus API 为 `NEXUS_DATA_DIR` 下 `nexus-api`；Agent Service 为 `AGENTSCOPE_WORKSPACE_DIR`、`AGENTSCOPE_QDRANT_PATH`、`AGENTSCOPE_BLOB_DIR`；Celery schedule 落在 runtime 目录。

所有路径都可由部署配置覆盖，服务 unit 只引用生成配置中的路径。

### 服务定义

一个 Python 服务目录统一定义服务名、unit 名、命令、工作目录、必需/可选、端口、就绪探针和依赖。生产模式每个长期进程一个 unit：Task API、Celery Beat、Celery Control Worker、Celery Agent Worker、Task MCP、Agent Service、Nexus API；Gateway 与 Library MCP 启用时必需；Web 由 Nginx 托管构建产物。OpenCode Server 由 Task API 的既有 Runtime 管理器按需启动，不新增与现有管理器冲突的独立 unit。

`astra_service.py pro` 改为薄封装：拒绝迁移、构建和托管，提示使用生产 CLI；`dev` 保持开发态行为。`preflight.py` 保留为开发自检，不作为生产成功判据。

### 配置分层

1. 用户配置：`nexus.env`，只由用户维护，部署不覆盖，只校验必需项。
2. 密钥：`secrets.env`，首次生成后保留，权限 0600，日志只输出键名或摘要。
3. 生成运行配置：`runtime.env`，每次准备阶段原子重建，非敏感差异输出摘要。
4. 服务 unit 通过 `EnvironmentFile` 同时加载用户配置、密钥与生成配置。

Task MCP 与 Nexus API/Agent Service 共享的密钥只生成一次，使用真实消费者字段 `ASTRA_TASK_MCP_KEY` 与 `NEXUS_TASK_MCP_KEY`。OpenCode 使用 Nexus 专属 `OPENCODE_CONFIG` 与配置目录，显式写入 provider/model/base URL，不读取或改写全局配置。

### 部署阶段与故障语义

`preflight -> prepare -> backup/verify -> stop -> migrate once -> switch -> start -> readiness/entry probes -> atomic success record`。

- 全程持有 `flock` 独占锁。
- 准备阶段在版本目录内安装依赖和构建，不影响现网；失败清理该目录。
- 停止与启动只操作 Nexus 自己的 unit，禁止按端口杀未知进程。
- 迁移前记录 revision；迁移后记录新 revision。启动/readiness 失败时，若 revision 未变或迁移声明兼容，恢复上一发布；否则阻断并输出人工数据库恢复步骤。
- 成功记录写 `releases/<commit>/deploy.json` 与状态目录的 latest/success 指针；失败记录写状态目录的 failed 记录，不写成功标记。

### 探针

- HTTP：要求状态码符合预期并解析/校验业务结构，不只检查连接。
- Redis：`PING`。
- SQL：`SELECT 1`。
- Celery：对指定 hostname 执行 `ping` 并检查目标队列消费者。
- MCP：使用共享密钥做认证的无副作用协议探测。
- Nginx：`nginx -t` 加首页与各启用路由的端到端请求，拒绝把任意 401/403 或无内容 SPA 页面当作成功。
- 端口归属：`ss` 得到监听 PID，再与 systemd unit 的 `MainPID`/cgroup 比对；测试可注入 runner。

### 恢复与备份

SQLite 使用 `sqlite3.Connection.backup` 生成副本并执行 `PRAGMA integrity_check`。MySQL 使用 `mysqldump`（存在时）并检查退出码与输出，不实现伪自动恢复；工具缺失即失败并给出人工恢复要求。发布回退只回退代码和服务，不自动逆向数据库。

## Risks / Trade-offs

- [systemd 与探针只能在 Linux 真机验收] → 在 macOS 用 stub `systemctl`/runner 做行为故障注入，另附 Linux smoke 脚本并明确未验证项。
- [MySQL 备份验证依赖外部工具] → 明确工具要求，缺失时阻断升级，不伪造成功。
- [迁移兼容性无法自动判定] → 版本清单中声明 `database_compatible_with_previous`，未声明时默认阻断旧代码回退。
- [服务启动顺序与就绪顺序不同] → systemd 控制启动顺序，部署器统一按业务 readiness 判定成功。

## Migration Plan

1. 在隔离分支建立契约与测试。
2. 逐步替换部署入口、服务托管、配置生成、发布切换与健康检查。
3. 每个阶段在临时 Git 仓库、临时 SQLite、真实本地端口和 stub systemctl 上验证，再合并回集成分支。
4. 目标 Linux 服务器的真实 systemd、重启、迁移失败与回滚演练不在本机声称已完成。

## Open Questions

无阻塞问题。迁移兼容性由目标版本在版本清单中显式声明。
