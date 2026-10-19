# Nexus v1.4.4

Nexus 是数字员工的目录和交流产品。推理、会话、技能、知识和记忆由 **AgentScope 2.0.10 Agent Service** 承担。Nexus 只通过 HTTP 调用它。

任务、Agent Worker、API 网关和数字图书馆在 `services/task-platform`。员工目录在 `services/nexus-api`。页面只有 `web/nexus`。

## 目录

```text
services/task-platform   Task API、网关、Task MCP、Worker、Alembic
services/nexus-api       员工目录与会话绑定
services/agent-service   AgentScope Agent Service
web/nexus                唯一前端
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
| Task API | 18100 | `services/task-platform` |
| Gateway | 18110 | `services/task-platform` |
| Nexus API | 18120 | `services/nexus-api` |
| Library MCP | 18201 | `services/task-platform` |
| Task MCP | 18202 | `services/task-platform` |
| Nginx Web | 80 | `web/nexus` 构建产物 |
| 员工 OpenCode | 14096 | 由 Task 平台 Runtime 拉起 |
| 助手 OpenCode | 14097 | 由 Task 平台 Runtime 拉起 |
| Redis | 6379 | Agent Service 与 Celery |

## 本地启动

复制 `ops/env/.env.example` 为 `ops/env/.env` 后填写。脚本只读这一个文件。密钥留在环境变量里，不要写进仓库。

```bash
cp ops/env/.env.example ops/env/.env
redis-server --port 6379
ops/dev/start_agent_service.sh start
ops/dev/start_astra.sh start
ops/dev/start_nexus_api.sh start
```

页面开发：

```bash
cd web/nexus
npm install
npm run dev
```

打开 `http://127.0.0.1:5174`。开发服务器把 `/api/v1` 代理到 `18120`，其余 `/api` 代理到 `18100`。

这些 `.sh` 只加载 `ops/env/.env` 并调用 Python。进程由 `ops/dev/astra_service.py` 和 `ops/dev/service_ctl.py` 拉起：`Popen(start_new_session=True)` 记下 pid、pgid、psutil `create_time` 和 cwd。停止前必须 pid 与 `create_time` 仍一致，然后向进程组发 `SIGTERM`，超时再 `SIGKILL`，并用本机 socket 确认端口已释放。健康检查用 urllib，只接受 HTTP 2xx。停不干净时状态文件保留，进程以非 0 退出。

停止：`ops/dev/start_astra.sh stop`、`ops/dev/start_nexus_api.sh stop`、`ops/dev/start_agent_service.sh stop`、`ops/dev/start_task_mcp.sh stop`。

`NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG` 由 `services/agent-service/bootstrap_chat_model.py` 生成。页面上没有模型或凭证字段。

## 检查

```bash
cd services/task-platform && .venv/bin/ruff check app tests && .venv/bin/pytest
PYTHONPATH=services/nexus-api services/nexus-api/.venv/bin/pytest services/nexus-api/tests
cd web/nexus && npm run type-check && npm run build
```

生产部署见 [ops/deploy/PRODUCTION_DEPLOYMENT.md](ops/deploy/PRODUCTION_DEPLOYMENT.md)。Gateway 探针 key 首次部署后由部署创建；本机 OpenCode 在工作区无配置时使用部署生成的 `NEXUS_OPENCODE_CONFIG`，工作区配置优先。生产入口是 `ops/deploy/install_nexus.sh`，必须显式提供完整 commit；配置模板是 `ops/env/nexus-production.env.example`。`ops/deploy/DEPLOYMENT_GUIDE.md` 保留为入口索引。本机验证与未验证项见 [docs/reports/NEXUS_V144_DEPLOYMENT_20261019.md](docs/reports/NEXUS_V144_DEPLOYMENT_20261019.md)；真实 Linux 验收未完成前不得视为生产通过。Agent Service 的安装细节见 [docs/deploy/agentscope.md](docs/deploy/agentscope.md)。版本钉在 [services/agent-service/agentscope.version](services/agent-service/agentscope.version)。接口记录在 [docs/architecture/agentscope-2.0.10.md](docs/architecture/agentscope-2.0.10.md)。

## 已知限制

- 官方 `get_toolkit` 会挂上任务规划、`ToolStop`、定时任务和 Agent Team。启动脚本在组装之后卸掉这些名字，保留 `contact_employee` 和工作区文件工具。没有修改 AgentScope 安装包。
- 聊天验证目标是 DeepSeek 的 OpenAI 兼容接口：`OPENAI_BASE_URL=https://api.deepseek.com`，`OPENAI_MODEL=deepseek-flash`。密钥用 `OPENAI_API_KEY`，或在它为空时用 `DEEPSEEK_API_KEY`。
- OpenCode 执行模型默认 `ASTRA_OPENCODE_MODEL=deepseek/deepseek-flash`。Worker 自己的 `model_name` 优先。远程 Worker 经网关写成 `astra-gateway/deepseek-flash`。
- 知识库没有启用。`GET /knowledge_bases/` 会是 503。本地嵌入只通过 `services/agent-service/embedding_smoke.py` 做冒烟。
- `PER_SESSION` 只隔离工作目录，不是进程或文件沙箱。
- `X-User-ID` 仍是 Agent Service 的占位身份。私聊用浏览器里的匿名标识隔离，不是登录账号。
