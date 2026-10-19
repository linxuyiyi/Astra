# Nexus v1.4.4 生产部署、升级、回退与排障

本文只描述独立 Nexus 的生产部署。部署入口是 `ops/deploy/install_nexus.sh`，它调用 `ops/deploy/nexus_deploy`。默认路径如下，均可在 `/etc/nexus/nexus.env` 覆盖：

```text
/opt/nexus/releases/<commit>/   不可变发布目录
/opt/nexus/current -> releases/<commit>
/etc/nexus/nexus.env            用户配置，部署不覆盖
/etc/nexus/secrets.env          0600，首次生成后保留
/etc/nexus/runtime.env          部署生成的运行配置
/etc/nexus/opencode/opencode.json  Nexus 专属 OpenCode 配置
/var/lib/nexus/                 状态、发布记录、备份
/var/lib/nexus/data/            Task、Nexus API、Agent Service 数据
/var/log/nexus/                 systemd 服务日志
```

## 前置条件

- Linux x86_64/aarch64、systemd、Python 3.11+、Node 20+、npm、git、nginx、Redis。
- 部署账号能写 `/opt/nexus`、`/etc/nexus`、`/var/lib/nexus`、`/var/log/nexus`、systemd unit 目录与 Nginx 配置目录。推荐以 root 运行，服务以 `NEXUS_RUN_USER` 运行；部署会把生成文件和数据目录归属到该账号，用户配置文件内容不改。
- MySQL 后端还需要 `mysql` 与 `mysqldump`。数据库密码和模型密钥只放 secrets.env 或首次由部署生成。
- 不需要全局 `~/.config/opencode/opencode.json`。部署不读写它。

## 首次安装

```bash
cp ops/env/nexus-production.env.example /etc/nexus/nexus.env
chmod 600 /etc/nexus/nexus.env
# 填写 NEXUS_REPO_URL、完整 40 位 NEXUS_COMMIT、模型上游，以及数据库配置
ops/deploy/install_nexus.sh preflight
ops/deploy/install_nexus.sh install
```

部署阶段：

```text
preflight -> prepare/build -> backup/verify -> config -> stop -> migrate once
          -> switch current -> write units/nginx -> start -> readiness/entry probes
          -> atomic success record
```

- 构建与依赖安装在新的 `releases/<commit>` 中完成，失败不影响当前发布。
- 部署全程持有 `/var/lib/nexus/deploy.lock`；并发部署直接失败。
- 数据库迁移在停止服务后只执行一次。服务启动不会自动迁移。
- 成功记录写入 `/var/lib/nexus/success.json` 和 `/var/lib/nexus/installed`；失败只写 `latest.json` 与 `deployments/*-failed.json`，保留上次成功记录。
- 所有必需服务和入口探针通过后才写成功标记。

## 升级

```bash
# 修改 /etc/nexus/nexus.env 中的 NEXUS_COMMIT 为目标完整 commit
ops/deploy/install_nexus.sh upgrade
```

构建失败不会停止现网。切换后启动或 readiness 失败时：

- 数据库 revision 未变化，或目标发布 `ops/deploy/release.json` 声明 `database_compatible_with_previous: true`：自动切回上一发布并启动。
- 迁移失败：即使 revision 未变，也可能已执行部分 DDL，部署停止新服务、阻断旧代码自动启动，并要求先用已验证备份恢复数据库。
- 成功迁移后启动/readiness 失败：数据库 revision 已变化且未声明兼容时阻断自动回退；声明兼容才恢复上一发布。

`ops/deploy/release.json` 是目标发布对迁移兼容性的声明，默认 `false`。没有把握时不要改成 `true`。

## 状态、健康与重启

```bash
ops/deploy/install_nexus.sh status
ops/deploy/install_nexus.sh status --json
ops/deploy/install_nexus.sh health
ops/deploy/install_nexus.sh restart
ops/deploy/install_nexus.sh restart --service task-api --service nexus-api
```

- `status` 输出当前 commit、上一成功/最近尝试、各 systemd unit 的 active 与 MainPID。
- `health` 检查 unit/MainPID、业务 HTTP 结构、Redis PING、SQL `SELECT 1`、Celery 指定 hostname ping 与队列、MCP 认证 initialize、Nginx `-t` 和入口路由。
- 模型计费探测未实现真实请求，记录为 `skipped`；CLI 不提供假的 `--model-probe` 开关。
- `health` 输出 `healthy` / `degraded` / `failed`；只有必需项全通过才是 healthy。
- 启用 Gateway 时，部署会在 Task API 启动后通过现有 `/api/gateway/keys` 幂等创建 `nexus-deploy-probe` 探针 key，保存到 `secrets.env`（0600），重复部署复用；不会输出或覆盖已有 key。`/v1/models` 必须用该 key 做真实只读探针。缺失或创建失败时 `readiness` 失败，不用 401/403 冒充可用。
- 默认路由登记只创建 pool/node/route，不自动启用节点或绕过健康门。节点未 `ACTIVE`、模型密钥缺失等未完成项会作为非必需检查如实写入记录，`health` 显示 `degraded`，不会 warn 后假成功。
- 生产重启只重启 systemd unit，不迁移、不构建。

## 服务与端口

| 服务 | unit | 默认端口 | 必需 |
|---|---|---|---|
| Task API | `nexus-task-api` | 18100 | 是 |
| Celery Beat | `nexus-runtime-beat` | - | 是 |
| Celery Control Worker | `nexus-runtime-control` | - | 是 |
| Celery Agent Worker | `nexus-runtime-agent` | - | 是 |
| Task MCP | `nexus-task-mcp` | 18202 | 是 |
| Employee OpenCode | `nexus-opencode-server` | 14096 | 是（未配置外部 URL 时） |
| Assistant OpenCode | `nexus-assistant-opencode` | 14097 | 是（未配置外部 URL 且助手启用时） |
| Agent Service | `nexus-agent-service` | 18000 | 是 |
| Nexus API | `nexus-api` | 18120 | 是 |
| Gateway | `nexus-gateway` | 18110 | 启用时必需 |
| Library MCP | `nexus-library-mcp` | 18201 | 启用时必需 |
| Nginx | 系统 unit | 80 | 是（托管 Web） |

每个常驻进程一个 unit。`nexus-astra` oneshot 二次托管已移除；`ops/dev/astra_service.py pro` 会明确拒绝并指向生产 CLI。开发态继续使用 `ops/dev/astra_service.py dev`。

两个 OpenCode Server 使用现有 `app.runtime.opencode_server` 与 `app.assistant.server` 模块入口，各自一个 unit。配置了 `ASTRA_OPENCODE_SERVER_URL` / `ASTRA_ASSISTANT_OPENCODE_URL` 时不创建本地 unit，只检查外部 `/global/health`、`healthy=true` 和版本。14096/14097 若被本地 unit 占用，监听 PID 必须属于对应 unit 的 MainPID 或 cgroup。

## 回退

```bash
ops/deploy/install_nexus.sh rollback
ops/deploy/install_nexus.sh rollback --commit <previous-full-commit>
# 数据库 revision 与目标成功记录不一致时，先恢复已验证备份，再显式声明：
ops/deploy/install_nexus.sh rollback --commit <commit> --restored-revision <目标成功 revision>
```

- 回退只切换代码与 systemd 服务，不自动逆向数据库。
- 相同 schema 直接回退。数据库 revision 不同时，拒绝无条件回退；必须先用备份恢复并传入与目标成功记录一致的 `--restored-revision`。没有 `--force` 逃生口。
- 回退恢复上一发布保存的 runtime/OpenCode/chat-model/optional/unit/Nginx 快照，并删除本次新增 unit，避免重启后重复启动。
- 上一成功发布缺少快照时，回退被明确阻断并记录失败，不按当前配置猜测重建服务集合；需按人工恢复手册处理。
- 数据库恢复步骤：

```text
1. 停止 nexus-* 服务。
2. SQLite：用 /var/lib/nexus/backups/<stamp>-<commit>/nexus.db 覆盖当前库；
   MySQL：按人工手册导入 nexus.sql，并确认 alembic_version。
3. 执行 install_nexus.sh rollback --commit <上一发布>。
4. 执行 health，确认全部必需项通过。
```

SQLite 备份使用备份 API 并执行 `PRAGMA integrity_check`。部署会同时备份两个持久 SQLite：

- Task Platform：`/var/lib/nexus/data/task-platform/nexus.db`
- Nexus API：`/var/lib/nexus/data/nexus-api/nexus.sqlite3`（员工目录、会话绑定、附件索引等）

MySQL 备份使用 `mysqldump`，只在显式设置 `NEXUS_MYSQL_BACKUP_ACKNOWLEDGED=1` 后继续；自动恢复演练不会假装完成。

### Redis、向量与文件数据

AgentScope 的 Agent、Session、对话记忆、队列和邮箱事实源在 Redis（默认 DB 1）。部署只做 `PING` 健康检查，**不备份 Redis**。生产必须由运维显式配置并演练：

```text
Redis：开启 AOF（appendonly yes）或周期性 RDB，快照目录独立于 Nexus release；
      升级前手动触发 BGSAVE/AOF fsync，并复制 dump.rdb/appendonlydir 到 /var/lib/nexus/backups；
      恢复时先停止 nexus-*，恢复 Redis 数据后启动，再用 health 验证。
```

- `AGENTSCOPE_WORKSPACE_DIR`：每个 Session 的工作区，属于持久数据；备份边界按业务需要人工复制。
- `AGENTSCOPE_QDRANT_PATH`：知识库向量数据，必须与 AgentScope/Redis 的恢复点一致；本部署不自动备份。
- `AGENTSCOPE_BLOB_DIR`：附件/二进制对象，与 Nexus API 的附件索引需成对恢复。

Redis、Qdrant、blob 与 workspaces 不属于 SQL 备份的自动恢复范围。没有这些数据的恢复演练，不得声称完整灾难恢复。

## 可选模块

可选模块不在核心安装路径中，核心部署不会检查其依赖。

```bash
# LLM Wiki；需要 LLM_WIKI_BUNDLE，本机模式需要 docker，远端模式需要 ssh/sshpass，ingest 需要 Chrome
ops/deploy/install_nexus.sh optional-wiki

# 远端 Worker；需要 REMOTE_WORKER_SSH_PASSWORD
ops/deploy/install_nexus.sh optional-remote-worker --remote-worker 10.0.0.8
```

可选模块使用独立的 `optional.env` 与 systemd unit；核心升级不会覆盖它。LLM Wiki 的 ingest 依赖放在数据目录下的独立 venv，不修改不可变 release。可选模块失败只报告自身，不影响核心安装记录。

## 排障

| 现象 | 检查 |
|---|---|
| 预检提示端口被未知进程占用 | `systemctl show -p MainPID -p ControlGroup nexus-<name>` 与 `ss -ltnp` 对照；部署不会杀未知进程 |
| `current` 不是符号链接 | 旧版本原地安装残留。先人工迁移目录，不要让部署覆盖 |
| 部署失败但有旧版本 | 查看 `status --json` 的 `last_attempt.rollback`；确认服务已恢复 |
| 迁移失败 | 部署不会写成功标记；即使 revision 未变也可能有部分 DDL。先用完整性校验通过的备份恢复数据库，不要直接回旧代码 |
| readiness 失败 | 查对应 unit 日志 `/var/log/nexus/<unit>.log` 和 `journalctl -u <unit>`；探针不会把监听端口当作就绪 |
| Nginx 入口失败 | `nginx -t`、`/var/log/nginx/error.log`，并检查 `NEXUS_ENTRY_URL` 与启用路由 |
| 升级后配置丢失 | 用户配置与密钥不会被覆盖；检查是否改错了 `/etc/nexus/nexus.env` 或 `secrets.env` |
| 员工本机 OpenCode 报 Workspace 未提供配置 | 工作区没有 `opencode.json` 时，Runtime 会使用部署生成的 `NEXUS_OPENCODE_CONFIG`；工作区配置仍优先。检查 `/etc/nexus/opencode/opencode.json` 和 `OPENCODE_CONFIG` |

## 真实 Linux 验收

仓库提供 `ops/deploy/tests/linux_smoke.sh`，覆盖安装、状态、重启、Celery worker 崩溃后 systemd 自恢复和健康检查。该脚本只在明确设置 `NEXUS_SMOKE_EXECUTE=1` 的目标 Linux 主机运行。macOS 上的 Python/pytest 不能证明真实 systemd、Nginx、MySQL 或目标机型行为。

本机已完成的单元/契约检查、R01-R10 矩阵和未验证项见 `docs/reports/NEXUS_V144_DEPLOYMENT_20261019.md`。真实 Linux 安装、systemd、Nginx、MySQL/Redis/Qdrant/blob/workspaces 恢复演练和模型计费探测未执行，任务保持未完成。
