# Nexus v1.4.7

Nexus 是数字员工的目录和交流产品。推理、会话、技能、知识和记忆由 **AgentScope 2.0.10 Agent Service** 承担。Nexus 只通过 HTTP 调用它。

`services/nexus-api` 是唯一的 Nexus 服务：同一进程提供员工对话与目录、任务、Agent Worker、API 网关控制面、数字图书馆、Worker Gateway WebSocket 和前端静态资源。OpenCode 网关数据面、Task MCP、Library MCP 与 AgentScope 仍是独立基础设施进程。

所有业务表以 `t_nexus_` 开头，统一存放在 `services/nexus-api/data/nexus.db`。

## 当前版本

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
tools/llm_wiki           llm_wiki 隧道与 ingest pump
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
| 员工 OpenCode | 14096 | 由 Task 平台拉起 |
| Redis | 6379 | Agent Service 与 Celery |

## 本地启动

复制 `ops/env/.env.example` 为 `ops/env/.env` 后填写。脚本只读这一个文件。密钥留在环境变量里，不要写进仓库。

```bash
cp ops/env/.env.example ops/env/.env
redis-server --port 6379
ops/dev/start_agent_service.sh start
ops/dev/start_astra.sh start      # Celery、Gateway、Task MCP、OpenCode 等辅助进程
ops/dev/start_nexus_api.sh start  # 唯一 API + 已构建前端，18120
```

打开 `http://127.0.0.1:18120`。页面需要先构建：`cd services/nexus-api/web && npm ci && npm run build`（聊天岛先构建到 `web/public/nexus-chat`）。

页面开发时可单独启动 Vite：

```bash
cd services/nexus-api/chat-island && npm run build
cd services/nexus-api/web && npm install && npm run dev
```

开发服务器把 `/api` 与 `/api/v1` 都代理到 `18120`，`/v1` 代理到 `18110`，`/agentscope-api` 代理到 `18000`。

这些 `.sh` 只加载 `ops/env/.env` 并调用 Python。进程由 `ops/dev/astra_service.py` 和 `ops/dev/service_ctl.py` 拉起：`Popen(start_new_session=True)` 记下 pid、pgid、psutil `create_time` 和 cwd。停止前必须 pid 与 `create_time` 仍一致，然后向进程组发 `SIGTERM`，超时再 `SIGKILL`，并用本机 socket 确认端口已释放。健康检查用 urllib，只接受 HTTP 2xx。停不干净时状态文件保留，进程以非 0 退出。

停止：`ops/dev/start_astra.sh stop`、`ops/dev/start_nexus_api.sh stop`、`ops/dev/start_agent_service.sh stop`、`ops/dev/start_task_mcp.sh stop`。

`NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG` 由 `services/agent-service/bootstrap_chat_model.py` 生成。页面上没有模型或凭证字段。

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

- 知识库已接入 AgentScope：启动时传入 `CollectionPerKbManager`，向量库使用本地 Qdrant（`AGENTSCOPE_QDRANT_PATH`）。创建知识库仍需在 AgentScope 登记可用的 embedding 模型；DeepSeek 聊天凭证不能当作 embedding。未登记时 `GET /knowledge_bases/` 返回空列表，创建会失败。`services/agent-service/embedding_smoke.py` 只做本机嵌入冒烟。
- 官方 `get_toolkit` 会挂上任务规划、`ToolStop`、定时任务和 Agent Team。启动脚本在组装之后卸掉这些名字，保留 `contact_employee` 和工作区文件工具。没有修改 AgentScope 安装包。
- 聊天验证目标是 DeepSeek 的 OpenAI 兼容接口：`OPENAI_BASE_URL=https://api.deepseek.com`，`OPENAI_MODEL=deepseek-flash`。密钥用 `OPENAI_API_KEY`，或在它为空时用 `DEEPSEEK_API_KEY`。
- OpenCode 执行模型默认 `ASTRA_OPENCODE_MODEL=deepseek/deepseek-flash`。Worker 自己的 `model_name` 优先。远程 Worker 经网关写成 `nexus-gateway/deepseek-flash`。
- 透明路由在流式响应中不回写响应体的 `model` 字段；请求日志仍记录请求模型与实际模型。
- 私聊和团队会话使用 `dont_ask`，避免没有审批 UI 时卡在 `asking`。私聊里的普通 Bash 会被拒绝；绑定 MCP 的会话使用 `bypass`，否则 MCP Tool 在 `dont_ask` 下会被转成 `DENY`。
- `PER_SESSION` 只隔离工作目录，不是进程或文件沙箱。
- `X-User-ID` 仍是 Agent Service 的占位身份。私聊用浏览器里的匿名标识隔离，不是登录账号。
