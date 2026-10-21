# Nexus v1.4.8 全新部署指南

适用于公司 MySQL **8.0.36**、Linux 控制机和远程 Linux Worker。Nexus 不升级旧数据库，不导入历史 SQLite；初次部署使用两个空库。后续 schema 变化新增 revision，不改已发布的 `0001_init`。

## 1. 服务与数据归属

| 服务 | 默认端口 | 数据与职责 |
| --- | --- | --- |
| Nexus API + Web | 18120 | `/api/v1` 员工与会话绑定、`/api` Task/Worker/图书馆/网关控制面、Worker WebSocket、前端 |
| Nexus 网关数据面 | 18110 | `/v1/models`、`/v1/chat/completions`、`/v1/embeddings` |
| AgentScope 2.0.10 | 18000 | 官方 Agent、Session、消息、凭据、Skill、Knowledge；独立 SQL 数据库 |
| Library MCP / Task MCP | 18201 / 18202 | 独立基础设施进程 |
| Redis | 6379 | AgentScope MessageBus、Celery、网关并发/额度/吊销运行态 |
| MySQL 8.0.36 | 3306 | Nexus 与 AgentScope 两个独立数据库 |
| Worker OpenCode | 每个 Worker 独立 | 在 Worker 机器运行，保留固定版本校验 |
| llm_wiki | 由外部服务指定 | 已有服务器，Nexus 通过 Source API 与鉴权项目接口访问 |

`ASTRA_EXECUTOR_TYPE=worker` 时，控制机不安装、检查或启动本机 OpenCode。显式 `opencode_server` 仅供本机调试；受管 Serve 的主模型与 small model 同样只走 Nexus 网关。独立手工 CLI fallback 不属于平台受管执行。

## 2. 准备配置

控制机需要 Python ≥3.11、Node ≥20、Git、curl 和 Redis 来源；Linux glibc ≥2.28，建议内存至少 4GB、可用磁盘至少 10GB。Worker 需要自己的 Linux、Python、Node/OpenCode 和网络连通性，部署过程独立检查。

从已发布分支取代码：

```bash
git clone -b v1.4.8 https://github.com/linxuyiyi/Astra /opt/nexus
cp /opt/nexus/ops/env/.env.example /opt/nexus/ops/env/.env
chmod 600 /opt/nexus/ops/env/.env
```

由 DBA 建立 `nexus`、`nexus_agentscope` 两个空库，使用 InnoDB、utf8mb4、utf8mb4_bin，并分别授权初始化所需的 DDL 和业务读写权限。AgentScope 初始化需要修改消息时间字段精度；无需全局管理员权限。

| 配置 | 含义 |
| --- | --- |
| ASTRA_DATABASE_URL | `mysql+asyncmy://用户:编码密码@主机:3306/nexus?charset=utf8mb4` |
| AGENTSCOPE_SQL_URL | 独立 `nexus_agentscope` 库的异步 SQL URL |
| ASTRA_EXECUTOR_TYPE | 公司使用 `worker` |
| NEXUS_GATEWAY_OPENAI_BASE | AgentScope/本机受管 Serve 能访问的网关 `/v1` 地址 |
| NEXUS_GATEWAY_API_KEY | 可留空让安装器稳定生成并登记；已有 key 不得失效、过期或吊销 |
| ASTRA_SERVICE_GATEWAY_HOST / PORT | 网关真实监听地址/端口；Worker 直连时填可达内网监听地址，经反代时可保留 loopback |
| NEXUS_REMOTE_GATEWAY_LOCAL_URL | Worker 能访问的网关 `/v1` 地址，例如 `http://10.0.0.8:18110/v1` |
| NEXUS_MCP_RESOURCE_SERVER_URL | Worker 能访问的 Library MCP 地址 |
| NEXUS_WORKER_AGENT_GATEWAY_URL | Worker 可达的 WebSocket 地址，例如 `ws://10.0.0.8/api/worker-gateway/connect`（经反代） |
| NEXUS_DEFAULT_MODEL | 已启用的网关模型别名，用于安装器可选 Worker 创建 |
| NEXUS_KB_EMBEDDING_JSON | 可选 `{"model_route_id":"嵌入路由ID","dimensions":1024}`；不接受厂商 credential_id |

密钥和数据库密码不提交代码。URL 中的特殊字符必须编码。安装不要求 `DEEPSEEK_API_KEY` 或 `OPENAI_*`；供应商连接与凭据只放到 API 网关上游节点。

## 3. 安装与重启

```bash
/opt/nexus/ops/deploy/install_nexus.sh --env-file /opt/nexus/ops/env/.env --preflight-only
/opt/nexus/ops/deploy/install_nexus.sh --env-file /opt/nexus/ops/env/.env
```

可用选项：`--with-llm-wiki`、`--remote-worker 主机`、`--skip-web-build`、`--no-systemd`。初次先启动平台、在网关配置有效节点和路由，再创建员工或运行可选 Worker 部署。没有预置厂商节点或模型。

安装器在 `NEXUS_STATE_DIR`（默认 `/var/lib/nexus`）生成 `nexus.env`、`secrets.env`，权限 0600。两个文件均使用 shell 安全转义；systemd 的 ExecStart/ExecStop 和非 systemd 路径通过 shell 加载同一文件。不要把它们另当作 JSON 或普通 systemd EnvironmentFile 解析。

安装器显式保存执行模式、网关监听和客户端地址；稳定保存内部网关 key，只向数据库登记摘要。重复安装复用 key，不复活已吊销/删除/过期 key。运行环境通过 `NEXUS_RUNTIME_ENV_FILE` 被启动预检识别，不依赖目标仓库另有 `.env`。

有 systemd 时生成 `nexus-astra`、`nexus-agent-service`、`nexus-api` 服务；否则使用带 PID、create_time、cwd 归属记录的本地进程管理。Task MCP、Celery、网关等由 `astra_service.py` 管理。修改运行环境后重启相应受管服务，并验证实际接口；端口监听不是完整 readiness。

## 4. 手工初始化

在受保护环境配置已导入、Python 依赖已安装后：

```bash
set -a; . /opt/nexus/ops/env/.env; set +a
cd /opt/nexus/services/nexus-api
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m app.deployment gateway-key
cd /opt/nexus/services/agent-service
.venv-agentscope/bin/python agentscope_sql.py init
.venv-agentscope/bin/python agentscope_sql.py check
```

手工执行 `gateway-key` 前须提供自己生成的 `agw_` 调用 key；自动生成由安装器完成。Nexus 以单个 `0001_init` 创建 53 张业务表加版本表。AgentScope 使用官方 2.0.10 迁移；部署只额外将 `messages.created_at` 设为 `DATETIME(6)`，保持同秒消息和游标顺序，不修改官方包。

旧版本、已有未标记表、不完整 schema 会失败。不要清库、stamp 或调用 create_all 绕过。API/Celery 启动只校验 schema，不偷偷补字段。

## 5. 配置统一模型入口

在 API 网关控制面建立上游节点、池和路由。上游节点填写根地址，模型路由可使用业务别名并通过 `target_model` 映射实际模型。

- 聊天节点选择 SSE 或非流式探测；embedding-only 节点选择 `EMBEDDING`（页面“向量嵌入”）。完成所选策略的真实探测、达到健康阈值后再启用调度。
- 客户端地址为 `http://网关主机:端口/v1`。Worker 配置接受根地址或 `/v1` 并规范化，不接受任意其他子路径。
- 员工选择启用的路由；Nexus 在 AgentScope 登记指向网关的 credential。Worker 模型为 `nexus-gateway/模型别名`。
- 图书馆嵌入配置使用路由 ID 和维度，自动取得同一网关 credential；没有配置就不映射，不回退厂商直连。
- embedding 复用网关鉴权、模型权限、额度、节点选择、并发租约、错误和日志。它不支持流式请求。
- 显式本机调试模式通过 OpenCode 内联配置限制 provider 为 `nexus-gateway`，保留工作区工具设置，不覆盖用户全局文件。旧本机 Serve 配置不符时需显式重启。

可先用网关 key 调用 `/v1/models` 确认授权模型；再分别验证聊天和 embedding。模型列表 200 不代表实际推理成功。不要在命令输出、截图或工单中暴露 key。

## 6. 已有 llm_wiki

配置 `ASTRA_LLM_WIKI_API_BASE_URL`、真实 `ASTRA_LLM_WIKI_API_TOKEN` 和已有 `ASTRA_LLM_WIKI_PROJECT_ID`，启用 `--with-llm-wiki`。缺少 token 或项目时预检失败，不随机生成。

安装使用相同 token 访问 health 和项目 search；health 成功但鉴权/项目不存在仍失败，验证后才触发图书馆重新同步。Nexus 不部署 llm_wiki 容器、不创建项目、不建立 SSH 隧道。

外部服务运维必须在 llm_wiki 服务端将聊天和可选 embedding 端点设为该机器能访问的 Nexus 网关 `/v1` 地址，配置对应模型别名及授权 key。Nexus 没有该服务器的模型配置管理接口，项目检索通过不能证明外部推理已经经过网关；需用真实 ingest/query 与网关请求日志现场确认。

## 7. 验收与恢复

逐项验证：两个 MySQL schema 与 ID 持久化、Redis、主 API、AgentScope（health 携带 `X-User-ID`）、网关授权模型与真实聊天/嵌入、两个 MCP、Worker WebSocket/健康、员工/私聊/团队与任务创建、停止和重启、llm_wiki 鉴权项目写入/检索，以及可信反向代理的身份和路由。

对外统一 API 与 Web 建议使用 `ops/deploy/nginx-nexus.conf`。反代须保留 WebSocket、SSE 和身份边界。远程 Worker 不能使用控制机的回环地址；控制机需开放实际内网监听或反代入口。

备份包含两个 MySQL 库、Redis 持久化、加密根密钥和运行凭据、Nexus 附件/资源、AgentScope workspace/blobs/Qdrant、外部知识服务数据。恢复需保留 ID、文件与密钥关联。公司 TLS、账号权限、sql_mode、容量、真实多节点并发和备份恢复需要现场验收，本机测试不能替代。
