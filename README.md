# Nexus v1.4.8

Nexus 是数字员工的目录和交流产品。推理、会话、技能、知识和记忆由 **AgentScope 2.0.10 Agent Service** 承担。Nexus 只通过 HTTP 调用它。

`services/nexus-api` 是唯一的 Nexus 服务：同一进程提供员工对话与目录、任务、Agent Worker、API 网关控制面、数字图书馆、Worker Gateway WebSocket 和前端静态资源。OpenCode 网关数据面、Task MCP、Library MCP 与 AgentScope 仍是独立基础设施进程。

公司正式部署使用 **MySQL 8.0.36 空库**。Nexus 的 53 张自有业务表以 `t_nexus_` 开头，由一个 Alembic `0001_init` 初始化。AgentScope 官方 SQL 存储使用独立数据库。SQLite 仅用于显式配置的开发/测试环境；不支持历史库升级或导入。Redis 继续承担消息总线、Celery 和网关运行态。

## 当前版本

v1.4.8 在 v1.4.7 基础上新增**星系首页**：

- **正式首页 `/`**：数字员工是星系核心，专属 Agent Worker 沿轨运行，排队任务形成任务卫星，任务执行与跨员工职责请求形成可追溯光路。
- **只读投影 API**：`GET /api/v1/nebula/snapshot`、员工详情、员工任务、Worker 执行光路任务、职责协作与单任务投影；不新增业务状态机，不提供 `/nebula/*` 写接口。
- **真实业务闭环**：全屏星系首页不内嵌业务面板；点击节点显示轻量提示，并跳转既有私聊、任务中心、Worker 详情或员工配置。
- **实时与降级**：复用 `/api/events/stream` 做失效通知，15 秒周期快照；断线重连、数据源降级、连接异常与状态未知都保留最近确认事实。

v1.4.7 包含：

- **配置中心**：Skill 的创建、编辑、版本与启停，MCP 的创建、编辑、连接测试与启停，以及员工多选绑定和逐会话同步。
- **API 网关**：资源池与模型路由独立页面，请求模型 → 目标模型的透明路由，TTFT / 输出速度指标与统一统计窗口。
- **数字员工**：Element Plus 表格、工具与 MCP 名称辨识、只读详情和编辑抽屉。
- **对话**：统一聊天岛、私聊/群时间线、附件、停止与多会话切换。
- **Worker**：远程部署、维护、装备核对与真实健康投影。

## 目录

```text
services/nexus-api       唯一 Nexus 服务（对话 + 任务 + Runtime + Worker + 网关控制面 + 图书馆）
  nexus_api/             对话、员工目录、会话绑定、Skill/MCP 资源、统一入口
  app/                   任务、Runtime、Worker、网关控制面、图书馆（Alembic）
  web/                   唯一前端（Vue 3 + Element Plus）
  chat-island/           聊天岛（React 子应用）
  tests/                 nexus/ 与 platform/ 两套测试
services/agent-service   AgentScope Agent Service
web/workspace-ui         OpenCode 工作台静态资源
ops/dev                  本地启动与停止
ops/deploy               安装脚本与部署说明
ops/env/.env.example     唯一环境模板
tools/e2e                验证脚本
docs/product             产品说明与需求资料
docs/architecture        架构与运行时说明
docs/deploy              部署说明
docs/reports             验证记录与证据
```

`openspec/`、`.opencode/` 和 `opencode.json` 留在仓库根目录。

## 端口

| 服务 | 端口 | 目录 |
|---|---|---|
| Agent Service | 18000 | `services/agent-service` |
| Nexus API + Web | 18120 | `services/nexus-api`（`nexus_api.service:app`） |
| Gateway 数据面 | 18110 | `services/nexus-api/app/gateway/server.py` |
| Library MCP | 18201 | `services/nexus-api/app/mcp_server.py` |
| Task MCP | 18202 | `services/nexus-api/app/task_mcp.py` |
| Schedule MCP | 18203 | `services/nexus-api/nexus_api/schedule_mcp.py` |
| Worker OpenCode | 每个 Worker 独立端口 | 在 Worker 机器运行；控制机默认不安装 |
| Redis | 6379 | Agent Service 与 Celery |

内置 MCP 使用标准 HTTP，默认监听 `127.0.0.1`，不配置共享 Bearer key。Task/Schedule 通过 AgentScope 官方 Session 工作区接口挂接；连接和工具发现不要求运行中的讨论，工具调用仍校验员工、会话、任务归属、讨论租约或日程来源。

## 本地启动

先创建两个 MySQL 空库并安装 Python 依赖（见部署指南）。复制 `ops/env/.env.example` 为 `ops/env/.env` 后填写并设为 0600。公司使用 `ASTRA_EXECUTOR_TYPE=worker`：控制机只提供平台与网关，远程 Worker 执行任务；控制机不检查 OpenCode 版本，Worker 保留固定版本检查。密钥不提交仓库。

安装器将执行模式、网关地址和监听配置写入运行环境，稳定保存内部网关 key，并登记其摘要。systemd 和手动重启读取相同文件；无需在新目录补造仓库 `.env`。安装器不创建 DeepSeek 等厂商节点。

```bash
cp ops/env/.env.example ops/env/.env
set -a; . ops/env/.env; set +a
redis-server --port 6379
(cd services/nexus-api && .venv/bin/python -m alembic upgrade head)
services/agent-service/.venv-agentscope/bin/python services/agent-service/agentscope_sql.py init
# 手工启动须先填好已登记的 NEXUS_GATEWAY_API_KEY；安装器会稳定生成并登记。
(set -a; . ops/env/.env; set +a; cd services/nexus-api && .venv/bin/python -m app.deployment gateway-key)
ops/dev/start_agent_service.sh start
ops/dev/start_astra.sh start      # Celery、Gateway、Task MCP、Schedule MCP；worker 模式不启动本机 OpenCode
ops/dev/start_nexus_api.sh start  # 唯一 API + 已构建前端，18120
```

打开 `http://127.0.0.1:18120`。页面需要先构建：`cd services/nexus-api/web && npm ci && npm run build`（聊天岛先构建到 `web/public/nexus-chat`）。

页面开发时可单独启动 Vite：

```bash
cd services/nexus-api/chat-island && npm run build
cd services/nexus-api/web && npm install && npm run dev
```

开发服务器把 `/api` 与 `/api/v1` 都代理到 `18120`，`/v1` 代理到 `18110`，`/agentscope-api` 先代理到 `18120` 再由 Nexus 转发 Agent Service。

这些 `.sh` 只加载 `ops/env/.env` 并调用 Python。进程由 `ops/dev/astra_service.py` 和 `ops/dev/service_ctl.py` 拉起：`Popen(start_new_session=True)` 记下 pid、pgid、psutil `create_time` 和 cwd。停止前必须 pid 与 `create_time` 仍一致，然后向进程组发 `SIGTERM`，超时再 `SIGKILL`，并用本机 socket 确认端口已释放。健康检查用 urllib，只接受 HTTP 2xx。停不干净时状态文件保留，进程以非 0 退出。

停止：`ops/dev/start_astra.sh stop`、`ops/dev/start_nexus_api.sh stop`、`ops/dev/start_agent_service.sh stop`、`ops/dev/start_task_mcp.sh stop`、`ops/dev/start_schedule_mcp.sh stop`。

聊天模型不再从部署环境读取：员工选择已启用的网关路由后，Nexus 以 `NEXUS_GATEWAY_OPENAI_BASE` + `NEXUS_GATEWAY_API_KEY` 在 Agent Service 登记指向网关的 credential。页面上没有模型或凭证字段。

## 模型与外部知识服务

员工聊天、Worker、平台复盘和 embedding 都通过 Nexus 网关。厂商凭据只保存在网关上游节点；AgentScope 的 credential 指向网关。客户端 Base URL 使用 `/v1`；网关节点的上游 Base URL 填根地址。

- 在网关配置上游节点及启用的模型路由，数字员工选择 `model_route_id`。Worker 选择同一网关中的模型别名。
- 网关提供 `/v1/models`、`/v1/chat/completions`、`/v1/embeddings`；嵌入模型节点选择“向量嵌入”探测策略。
- 配置 `NEXUS_KB_EMBEDDING_JSON='{"model_route_id":"实际嵌入路由ID","dimensions":1024}'` 后，图书馆发布使用该路由与网关 credential。未配置时不启用映射；不接受独立厂商 credential_id。
- 已有 llm_wiki 需提供真实 token、project_id，安装会验证鉴权项目检索。其运维需在 llm_wiki 服务器配置可达的 Nexus 网关 `/v1` 地址和模型别名；Nexus 不接管该服务器的模型配置。
- 显式 `opencode_server` 本机调试模式也只启用 `nexus-gateway`，主模型与小模型均走网关；不覆写用户全局配置。独立手工 CLI fallback 不属于平台受管 Serve。

## 任务经验复盘

人工确认任务完成后，可可选开启平台侧独立复盘，把真实执行轨迹沉淀为领域经验与工作方法改进候选。默认关闭：

```bash
NEXUS_EXPERIENCE_REVIEW_ENABLED=true
NEXUS_EXPERIENCE_REVIEW_MODEL=deepseek-flash
```

复盘只生成 candidate，必须人工审核发布；不会自动修改员工配置。详见 [docs/architecture/task-experience-review.md](docs/architecture/task-experience-review.md)。

## 检查

```bash
cd services/nexus-api && .venv/bin/ruff check app nexus_api tests && .venv/bin/pytest
cd services/nexus-api/web && npm run type-check && npm run build
cd services/nexus-api/chat-island && npm run type-check && npm run build
```

部署见 [ops/deploy/DEPLOYMENT_GUIDE.md](ops/deploy/DEPLOYMENT_GUIDE.md)。Agent Service 的安装细节见 [docs/deploy/agentscope.md](docs/deploy/agentscope.md)。版本钉在 [services/agent-service/agentscope.version](services/agent-service/agentscope.version)。接口记录在 [docs/architecture/agentscope-2.0.10.md](docs/architecture/agentscope-2.0.10.md)。

v1.4.7 的复审结论见 [docs/reviews/nexus-v1.4.7-final-review.md](docs/reviews/nexus-v1.4.7-final-review.md)，Skill/MCP 配置中心记录见 [docs/reports/NEXUS_SKILL_MCP_CONFIG_CENTER_20261020.md](docs/reports/NEXUS_SKILL_MCP_CONFIG_CENTER_20261020.md)。

## 已知限制

- 知识库使用 AgentScope 官方 SQL 元数据、Qdrant 与文件存储。需配置可用的嵌入网关路由与正确维度；聊天模型不能代替 embedding。`embedding_smoke.py` 仅用于独立本机模型实验。
- 官方 `get_toolkit` 会挂上任务规划、`ToolStop`、定时任务和 Agent Team。启动脚本在组装之后卸掉这些名字，保留 `contact_employee` 和工作区文件工具。没有修改 AgentScope 安装包。
- 私聊和团队会话使用 `dont_ask`，避免没有审批 UI 时卡在 `asking`。私聊里的普通 Bash 会被拒绝；绑定 MCP 的会话使用 `bypass`，否则 MCP Tool 在 `dont_ask` 下会被转成 `DENY`。
- `PER_SESSION` 只隔离工作目录，不是进程或文件沙箱。
- `X-User-ID` 仍是 Agent Service 的占位身份。私聊用浏览器里的匿名标识隔离，不是登录账号。
