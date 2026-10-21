# Nexus v1.4.8 完整部署指南

> 依据 2026-10-14 ~ 10-20 在部署机（box，Debian 13 x86_64）与 `LLM_WIKI_HOST` 指向的远程主机（CentOS 7.3 x86_64）上的实际部署整理，基础设施部署经验保留；v1.4.8 数据库按 MySQL 8.0.36 全新初始化，见 §5.2。
> 代码：<https://github.com/linxuyiyi/Astra>，分支 `v1.4.8`（Nexus 初始化为 `0001_init`，不支持历史库升级）。
> 一键脚本：`ops/deploy/install_nexus.sh`（配置模板 `ops/env/.env.example`）。本文每一步都可以手工执行，脚本只是把它们串起来。

---

## 0. 架构与端口一览

| 组件 | 端口（默认值） | 进程 / 启动方式 | 说明 |
|---|---|---|---|
| MySQL 8.0.36 | 3306 | DBA 维护 | Nexus 与 AgentScope 各使用独立数据库 |
| Redis | 6379 | redis-server 或 `services/agent-service/docker-compose.yml` | AgentScope 消息总线、Nexus Celery/网关运行态与吊销表 |
| Agent Service（AgentScope 2.0.10） | 18000 | `services/agent-service/agent_service.py` | 员工会话、私聊/邮箱/团队讨论、知识库。默认绑定 `0.0.0.0`，对外部署建议改 `127.0.0.1` 或加反代 |
| Nexus API Gateway（API 中转数据面） | 18110 | 同上（`app.gateway.server`） | OpenAI 兼容 `/v1`，远程 Worker 直连内网地址 |
| Nexus API（统一服务） | 18120 | `uvicorn nexus_api.service:app` | 员工对话 `/api/v1`、任务 `/api`、Worker Gateway WebSocket、前端静态资源。唯一对外 HTTP 服务 |
| Library MCP | 18201 | 同 astra_service（`nexus-library-mcp`，streamable-http） | 员工/OpenCode 查询数字图书馆 |
| Task MCP | 18202 | 同 astra_service（`astra-task-mcp`） | 员工会话里的 `nexus_task_*` 工具 |
| 平台 OpenCode | 14096 | astra_service 拉起 `opencode serve` | 只监听 127.0.0.1。本地 Worker 不要占用它 |
| 本地 Worker OpenCode | 自定（如 14196） | Nexus runtime 拉起 | 每个 Worker 一个端口 |
| llm_wiki Web 版（远端） | 18080（网关）/ 19829（内部） | Docker 容器 `llm-wiki-web` | 只绑 127.0.0.1 |
| llm_wiki 本机隧道 | 19828 | `ssh -L 19828:127.0.0.1:18080` | Nexus 的 `ASTRA_LLM_WIKI_API_BASE_URL` |
| 远程 Worker OpenCode | 环境端口，默认 14096（远端 127.0.0.1） | 一键部署写入 | Worker 直连内网 Gateway 18110 与图书馆 MCP 18201 |

---

## 1. 基础环境检查

| 项 | 要求 | 检查命令 | 实测/备注 |
|---|---|---|---|
| OS / 架构 | Linux x86_64 或 aarch64 | `uname -sm; cat /etc/os-release` | box：Debian 13 x86_64 |
| glibc | ≥ 2.28（llm_wiki Web 版要求；新 Node/Python 轮子也需要） | `ldd --version` | **CentOS 7 = 2.17，不可直接跑，只能放容器** |
| CPU | ≥ 2 核（主控机建议 4 核+） | `nproc` | |
| 内存 | 主控机 ≥ 4 GB（建议 8 GB）；llm_wiki 主机 ≥ 4 GB | `free -m` | **1.7 GB 的远程主机实测不够**：仿真的 llm-wiki-server ~670 MB + 两个 opencode ~560 MB，ingest 时 load 22、SSH 失联约 30 分钟 |
| 磁盘 | ≥ 10 GB 可用（venv、node_modules、镜像、工作区） | `df -h` | |
| 端口 | §0 表内端口未被占用 | `ss -ltnp` | 平台 Serve 使用 14096，本地 Worker 不要占用它 |
| Python | ≥ 3.11（backend、AgentScope 2.0.10） | `python3 --version` | box：3.13.5 |
| Node.js / npm | ≥ 20 | `node --version` | box：20.19.2；远程部署默认装 22.23.3 |
| Redis | 6.x+ 或 Docker | `redis-cli ping` | box：Redis 8.0.2 |
| git, curl, ss | 必需 | | |
| Docker | 可选（Redis 容器化、llm_wiki） | `docker --version` | CentOS 7 上用官方静态包 27.3.1 + 自写 systemd unit |
| OpenCode CLI | **1.18.35**（唯一支持版本） | `opencode --version` | `npm i -g --prefix ~/.local opencode-ai@1.18.35` |
| 浏览器 | 仅 llm_wiki ingest pump 需要 Chrome/Chromium | `google-chrome --version` | 见 §9 限制 |

一键预检：`ops/deploy/install_nexus.sh --preflight-only [--with-llm-wiki] [--remote-worker HOST]`，失败立即退出且不做修改。

---

## 2. 外部依赖与配置

### 2.1 DeepSeek（聊天模型）
- OpenAI 兼容：`OPENAI_BASE_URL=https://api.deepseek.com`，模型 `deepseek-flash`，密钥 `DEEPSEEK_API_KEY`（脚本把它同时作为 `OPENAI_API_KEY`）。
- 三处用到：Agent Service 凭证（`bootstrap_chat_model.py` 登记）、Worker 的 OpenCode（provider `deepseek` 或经网关 `nexus-gateway/deepseek-flash`）、llm_wiki 的 LLM 配置（customEndpoint `https://api.deepseek.com/v1`）。
- 上游繁忙时会返回 "Service is too busy"/503 或长时间 `: keep-alive`，见 §7。
- 检查：`curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $DEEPSEEK_API_KEY" https://api.deepseek.com/models` 应为 200。

### 2.2 Embedding
- DeepSeek 没有嵌入模型，**不要**把 deepseek-flash 配成 embedding。
- AgentScope 知识库：已接 `CollectionPerKbManager`，向量库为本地 Qdrant（`AGENTSCOPE_QDRANT_PATH`，默认 `services/agent-service/data/qdrant`）。创建知识库仍需在 AgentScope 登记可用的 embedding 模型；DeepSeek 聊天凭证不能当 embedding（见 §9）。
- llm_wiki：未启用 embedding，搜索 `mode=keyword`/`hybrid`（无 vector 命中），可接受。需要向量时可配 OpenAI 兼容 embedding 端点（如本地 Ollama/LM Studio），本机 sentence-transformers `all-MiniLM-L6-v2` 仅做过冒烟（`services/agent-service/embedding_smoke.py`），不能直接接 llm_wiki。

### 2.3 llm_wiki
- **必须用 Web Edition（验证版本 0.6.11，aarch64 包）**。桌面版 v0.6.12（含 main）**没有** `PUT/DELETE /projects/{id}/sources/file`，Nexus 同步全部 405。
- Web 版后端 `bin/llm-wiki-server`（127.0.0.1:19829）+ Node 网关（Web UI、`/api/v1`、`/mcp`），要求 glibc ≥ 2.28、Node ≥ 20。
- Nexus 只认 `http://127.0.0.1:<port>/api/v1` 形式的地址；远端部署时用 SSH `-L` 隧道。

### 2.4 远程 Worker SSH
- root（或有 sudo 的用户）+ 密码，密码存于 Nexus 数据库（API 只回 `has_ssh_password`）。
- 远端需能访问 npm 源（默认 `https://registry.npmmirror.com`）安装 Node/OpenCode。
- 网关 18110 与图书馆 MCP 18201 由远端 Worker 直连内网地址。部署拒绝 `127.0.0.1` / `localhost`。设置 `NEXUS_REMOTE_GATEWAY_LOCAL_URL`、`NEXUS_MCP_RESOURCE_SERVER_URL` 和 `NEXUS_WORKER_AGENT_GATEWAY_URL` 为 Worker 能访问的地址。OpenCode 仍只听远端 `127.0.0.1` 上的环境端口（默认 14096）。

### 2.5 密钥
| 密钥 | 生成 | 说明 |
|---|---|---|
| `ASTRA_GATEWAY_MASTER_KEY` | `python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())'` | 加密网关节点下游凭据；**丢失后已有节点凭据无法解密** |
| `NEXUS_MCP_API_KEY` | `python -c 'import secrets;print(secrets.token_urlsafe(32))'` | Library MCP Bearer；远程部署拒绝 stdio MCP |
| `ASTRA_LLM_WIKI_API_TOKEN` | 同上 | 写入 llm_wiki `apiConfig.token`（Web 版实际未强制，见 §9） |
脚本保存在 `$NEXUS_STATE_DIR/secrets.env`（0600），已存在则复用。box 上旧部署是 `/workspace/nexus-p2/.gwmaster`、`.mcpkey`、`.llmwikitoken`、`.llmwikiproject`（均 0600）。

### 2.6 环境变量总表

**Task Platform（start_astra.sh / astra_service.py 读取，前缀 `ASTRA_`）**

| 变量 | 必填 | 默认 | 作用 |
|---|---|---|---|
| ASTRA_GATEWAY_MASTER_KEY | 是 | — | 网关 Fernet 主密钥 |
| NEXUS_MCP_TRANSPORT | 是（远程 Worker） | stdio | 设 `streamable-http` |
| NEXUS_MCP_API_KEY | 是（streamable-http） | — | Library MCP Bearer |
| NEXUS_MCP_HOST / NEXUS_MCP_PORT | 否 | 127.0.0.1 / 18201 | Library MCP 监听 |
| NEXUS_TASK_MCP_PORT | 否 | 18202 | Task MCP 端口 |
| ASTRA_GATEWAY_PROBE_TIMEOUT_SECONDS | 建议 | — | 设 30，避免探测过慢 |
| ASTRA_REDIS_URL | 是 | redis://localhost:6379/1 | Celery、API 中转，以及 Worker Gateway 的默认连接总线。不用 db 0，避免和 v0.9 抢同一个库 |
| NEXUS_WORKER_GATEWAY_BUS | 否 | auto | `auto` 在 `ASTRA_REDIS_URL` 非空时用 Redis，使 API 与 Celery 共用一个连接所有者。URL 为空时退回 memory，启动日志和 `/api/worker-gateway/health` 的 `warning` 为「仅单进程可用」。需要单进程或强制 Redis 时分别设 `memory`、`redis` |
| NEXUS_WORKER_GATEWAY_ADMIN_KEY | 否 | 启动脚本生成一次 | Worker Gateway 登记、查询和下发的内部密钥，请求头 `X-Astra-Worker-Gateway-Key`。留空时 `ops/dev/astra_service.py` 在拉起 API 前写入 `ops/env/.env`（加锁、原子替换、权限 0600）。API 进程不各自生成；密钥缺失时管理接口返回 401，进程仍可启动。WebSocket 使用 Worker token，健康检查不需要这把密钥 |
| ASTRA_OUTBOX_INTERVAL / ASTRA_OUTBOX_RETRY_SECONDS | 否 | — | 唤醒 outbox 轮询（验证用 2 / 5） |
| ASTRA_OPENCODE_MODEL | 是 | — | `deepseek/deepseek-flash`（provider/model） |
| ASTRA_OPENCODE_SERVER_PORT | 否 | 14096 | 平台 OpenCode，只监听 127.0.0.1 |
| ASTRA_OPENCODE_SERVER_VERSION | 否 | 1.18.35 | 唯一支持的 OpenCode 版本 |
| ASTRA_NEXUS_API_BASE | 是 | — | `http://127.0.0.1:18120`（task-hint 唤醒） |
| ASTRA_AGENTSCOPE_BASE_URL / ASTRA_AGENTSCOPE_USER_ID | 是 | — | `http://127.0.0.1:18000` / `nexus` |
| ASTRA_LLM_WIKI_ENABLED | 图书馆需要 | false | 打开 llm_wiki 集成 |
| ASTRA_LLM_WIKI_API_BASE_URL | 同上 | http://127.0.0.1:19828/api/v1 | 必须 `http://127.0.0.1:<port>/api/v1` |
| ASTRA_LLM_WIKI_API_TOKEN | 同上 | — | Bearer token |
| ASTRA_LLM_WIKI_PROJECT_ID | 同上 | — | llm_wiki 项目 UUID（`.llm-wiki/project.json`） |
| ASTRA_LLM_WIKI_TIMEOUT | 否 | 10 | 远端仿真时建议 30 |
| NEXUS_REMOTE_OPENCODE_VERSION / NEXUS_REMOTE_NODE_VERSION / NEXUS_REMOTE_NPM_REGISTRY | 否 | 1.18.35 / 22.23.3 / npmmirror | 远程一键部署 |
| ASTRA_DATABASE_URL | 是 | 无 | Nexus 空 MySQL 库，使用 mysql+asyncmy URL |
| NEXUS_API_PORT / ASTRA_SERVICE_GATEWAY_PORT | 否 | 18120 / 18110 | 统一 API 与网关数据面端口 |

**Agent Service（`ops/env/.env.example`）**

| 变量 | 必填 | 默认 | 作用 |
|---|---|---|---|
| AGENTSCOPE_SQL_URL | 是 | 无 | 独立 AgentScope SQL 库，不能复用 Nexus schema |
| AGENTSCOPE_HOST / AGENTSCOPE_PORT | 否 | 0.0.0.0 / 18000 | 监听（对外建议 127.0.0.1 + 反代） |
| AGENTSCOPE_REDIS_HOST / _PORT / _PASSWORD | 否 | localhost / 6379 / 空 | Redis |
| AGENTSCOPE_WORKSPACE_DIR | 否 | services/agent-service/data/workspaces | 每 Session 工作目录 |
| NEXUS_API_BASE | 是 | http://127.0.0.1:18120 | `contact_employee` 解析 |

**Nexus API（start_nexus_api.sh）**

| 变量 | 必填 | 默认 | 作用 |
|---|---|---|---|
| NEXUS_DATA_DIR | 否 | services/nexus-api/data | 附件和自建资源文件；MySQL 部署不在此创建业务库 |
| NEXUS_AGENTSCOPE_BASE_URL / NEXUS_AGENTSCOPE_USER_ID | 是 | http://127.0.0.1:18000 / nexus | Agent Service |
| NEXUS_TASK_MCP_URL | 是 | — | `http://127.0.0.1:18202/mcp` |
| NEXUS_CHAT_PROVIDER | 是 | ollama | 设 `openai` |
| OPENAI_BASE_URL / OPENAI_MODEL / OPENAI_API_KEY | 是 | — | DeepSeek；`OPENAI_API_KEY` 空时读 `DEEPSEEK_API_KEY` |
| NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG | 是 | — | `bootstrap_chat_model.py` 打印的 JSON（只含 credential_id，不含密钥）；**为空则会话无模型、不出文本** |

**llm_wiki Web 容器**：`LLM_WIKI_DATA_DIR=/var/lib/llm-wiki`、`LLM_WIKI_WEB_HOST=127.0.0.1`、`LLM_WIKI_WEB_PORT=18080`、`LLM_WIKI_BACKEND_READY_TIMEOUT=180`（仿真启动慢）。

**一键脚本额外变量**：见 `ops/env/.env.example`（`NEXUS_ROOT`、`NEXUS_STATE_DIR`、`LLM_WIKI_HOST/BUNDLE/...`、`REMOTE_WORKER_*`）。

---

## 3. 一键部署

> `NEXUS_REPO_URL` 与 `NEXUS_BRANCH` 必须指向实际发布仓库和已推送的分支。本文按 `https://github.com/linxuyiyi/Astra` 的 `codex/nexus-v1.4.7` 编写；换仓库或分支时同步修改这两项。

```bash
git clone -b codex/nexus-v1.4.7 https://github.com/linxuyiyi/Astra /opt/nexus
cp /opt/nexus/ops/env/.env.example /opt/nexus/ops/env/.env && chmod 600 /opt/nexus/ops/env/.env   # 填 DEEPSEEK_API_KEY 等
/opt/nexus/ops/deploy/install_nexus.sh --preflight-only
/opt/nexus/ops/deploy/install_nexus.sh [--with-llm-wiki] [--remote-worker 1.2.3.4] [--skip-web-build] [--no-systemd]
```
脚本幂等：已有 venv/密钥/项目/网关路由/Worker 均复用；有 systemd 时由安装脚本生成 `nexus-*.service`（不读取 `ops/deploy/systemd` 静态文件），否则由 `ops/dev/service_ctl.py` 拉起进程（身份记录在 `$NEXUS_STATE_DIR/service-ctl.json`，日志在 `$NEXUS_STATE_DIR/logs`）。Task MCP 与管理台由 `astra_service.py dev` 拉起，安装脚本不再单独启动它们。
> 安装脚本在 `ops/deploy/install_nexus.sh`，环境模板在 `ops/env/.env.example`，llm_wiki 工具在 `tools/llm_wiki/`。

---

## 4. （手工）部署顺序总览

1. Redis → 2. nexus-api venv + alembic → 3. `astra_service.py`（Celery、Gateway、OpenCode、Task MCP、Library MCP）→ 4. Agent Service + 聊天模型登记 → 5. Nexus API（统一服务，含前端）→ 6. 网关路由 deepseek-flash → 7. llm_wiki 部署与项目创建 → 8. 远程 Worker 部署。Task MCP 和 Gateway 不单独作为对外 API。

## 5. 分步说明

以下 `R=/opt/nexus`（box 上为 `/workspace/nexus-p2`）。

### 5.1 Redis
```bash
bash $R/services/agent-service/redis.sh        # 已有 PONG 则跳过；有 docker 用 compose，否则本机 redis-server
redis-cli ping                       # PONG
```

### 5.2 两个空 MySQL 库与初始化

公司版本为 MySQL **8.0.36**。先由 DBA 创建 `nexus` 与 `nexus_agentscope` 两个独立空库，使用 InnoDB、utf8mb4、utf8mb4_bin collation；分别授权给对应服务账号。账号需要建表/索引权限及业务读写权限，不需要全局管理员权限。

在受保护的环境文件中配置 `ASTRA_DATABASE_URL` 与 `AGENTSCOPE_SQL_URL`（模板在 `ops/env/.env.example`）；密码及特殊字符必须 URL 编码。不要把真实 URL 写入工单或命令输出。

```bash
cd $R/services/nexus-api
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m alembic upgrade head       # 单个 0001_init，53 张当前业务表

cd $R/services/agent-service
python3 -m venv .venv-agentscope
.venv-agentscope/bin/pip install -r requirements.txt
.venv-agentscope/bin/python agentscope_sql.py init
.venv-agentscope/bin/python agentscope_sql.py check
```

以上命令执行前应将受保护环境文件导入当前进程；Agent Service 不自动读取 Nexus 的 Settings 文件。安装器会安全注入两个 URL，并在启动前分别初始化。

重复初始化不会清空数据。旧版本、已有未标记表或不完整 schema 会明确失败；不允许用 stamp 掩盖，也不提供旧 SQLite 导入工具。服务启动和 Celery 只检查 schema，不偷偷补表/补列。

Nexus 历史迁移链已收敛成 `0001_init`；AgentScope 使用官方 2.0.10 的内部迁移，不复制其迁移链，不修改官方包。针对官方 MySQL 消息时间默认秒精度，初始化额外将 `messages.created_at` 固定为 `DATETIME(6)`，启动检查该精度，保证同秒消息的先后与游标分页。SQLite 仅用于显式配置的开发/回归环境。

### 5.3 Task API / Celery / OpenCode
OpenCode：`npm i -g --prefix ~/.local opencode-ai@1.18.35`，`~/.config/opencode/opencode.json` 增加 deepseek provider（apiKey 写 `{env:DEEPSEEK_API_KEY}`，不要写明文）。
```bash
export PATH=$HOME/.local/bin:$PATH
cd $R/services/nexus-api && ASTRA_GATEWAY_MASTER_KEY=$(cat ../.gwmaster) ASTRA_GATEWAY_PROBE_TIMEOUT_SECONDS=30 \
  NEXUS_MCP_TRANSPORT=streamable-http NEXUS_MCP_API_KEY=$(cat ../.mcpkey) \
  ASTRA_LLM_WIKI_ENABLED=true ASTRA_LLM_WIKI_API_BASE_URL=http://127.0.0.1:19828/api/v1 \
  ASTRA_LLM_WIKI_API_TOKEN=$(cat ../.llmwikitoken) ASTRA_LLM_WIKI_PROJECT_ID=$(cat ../.llmwikiproject) ASTRA_LLM_WIKI_TIMEOUT=30 \
  ASTRA_REDIS_URL=redis://localhost:6379/1 ASTRA_OUTBOX_INTERVAL=2 ASTRA_OUTBOX_RETRY_SECONDS=5 \
  ASTRA_OPENCODE_MODEL=deepseek/deepseek-flash ASTRA_NEXUS_API_BASE=http://127.0.0.1:18120 \
  ASTRA_AGENTSCOPE_BASE_URL=http://127.0.0.1:18000 ASTRA_AGENTSCOPE_USER_ID=nexus \
  .venv/bin/python ../ops/dev/astra_service.py dev start
```
（即 `ops/dev/start_astra.sh [start|restart|stop|status]`。）Python 用新会话拉起 Gateway 18110、Library MCP 18201、Task MCP 18202、Celery beat、队列 `nexus-control` 和 agent、OpenCode 14096，并记录 pid 与 create_time。统一 API 与前端由 `nexus-api` 服务（18120）提供。健康检查只认 HTTP 2xx。停不干净时保留 `astra-service.json` 并以非 0 退出。启动前会做自检。

### 5.4 Task MCP

Task MCP（18202）由 `ops/dev/astra_service.py` 与 Task API、Celery、OpenCode、管理台一起拉起。启停用 Python 脚本，macOS 与 Linux 同一条命令；`start_astra.sh` 只加载 `ops/env/.env` 再调用它：

```bash
ops/dev/start_astra.sh start     # 或 restart / stop / status
```


### AgentScope 官方 Web UI（配置中心）

配置中心的 AgentScope 子页内嵌官方 AgentScope v2.0.10 Web UI：

- 静态产物：`services/nexus-api/web/public/agentscope-ui/`，随前端构建复制到 `dist/agentscope-ui/`。
- 同源 API：开发由 `vite.config.ts` 的 `/agentscope-api/` 代理，生产由 `nginx-nexus.conf` 代理到 AgentScope（默认 `127.0.0.1:18000`）。保留浏览器 `X-User-ID`。
- 默认入口：`/settings/agentscope`；无需手工填写 `server_url`。`/api/v1/runtime` 的 `agentscope_user_id` 用于写入同源 localStorage。
- 原生 Skill/MCP Hub：`services/agent-service/agent_service.py` 注入 `ClawSkillHub`、`GitHubMCPHub`；health 的 `skill_hubs`/`mcp_hubs` 应为 `ok`。
- 升级/重建：`ops/deploy/build_agentscope_web_ui.sh <官方 examples/web_ui/frontend>`；上游 commit 与三处补丁见 `docs/architecture/agentscope-web-ui.md`。

### 5.5 Agent Service（AgentScope 2.0.10）与聊天模型
```bash
python3 -m venv $R/services/agent-service/.venv-agentscope
$R/services/agent-service/.venv-agentscope/bin/pip install -r $R/services/agent-service/requirements.txt
# start_agent_service.sh 交给 service_ctl.py，健康检查要求 /health 返回 2xx 且带 X-User-ID
ops/dev/start_agent_service.sh start
# 登记模型（只需一次，凭证存 Redis）
NEXUS_CHAT_PROVIDER=openai OPENAI_BASE_URL=https://api.deepseek.com OPENAI_MODEL=deepseek-flash \
  $R/services/agent-service/.venv-agentscope/bin/python $R/services/agent-service/bootstrap_chat_model.py
# 输出形如 {"type":"openai_credential","credential_id":"…","model":"deepseek-flash","parameters":{}}
```
知识库向量与文件默认落在 `services/agent-service/data/qdrant`、`services/agent-service/data/blobs`，可用 `AGENTSCOPE_QDRANT_PATH`、`AGENTSCOPE_BLOB_DIR` 改到持久目录。创建知识库前需在 AgentScope 登记可用的 embedding 模型；DeepSeek 聊天凭证不能当 embedding。

### 5.6 Nexus API
```bash
cd $R/services/nexus-api && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
CFG='<上一步输出的 JSON>'
NEXUS_DATA_DIR=$R/services/nexus-api/data NEXUS_AGENTSCOPE_BASE_URL=http://127.0.0.1:18000 NEXUS_AGENTSCOPE_USER_ID=nexus \
NEXUS_TASK_MCP_URL=http://127.0.0.1:18202/mcp NEXUS_CHAT_PROVIDER=openai OPENAI_BASE_URL=https://api.deepseek.com \
OPENAI_MODEL=deepseek-flash OPENAI_API_KEY="$DEEPSEEK_API_KEY" NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG="$CFG" \
ops/dev/start_nexus_api.sh start
```

### 5.7 Nexus Web
```bash
cd $R/services/nexus-api/web && npm ci && npm run build        # vue-tsc + vite build → dist/
```
生产由统一 `nexus-api` 进程（18120）直接托管 `web/dist`，不要再单独执行 `vite preview`。开发模式可单独起 Vite：`cd services/nexus-api/web && npm run dev`；`vite.config.ts` 把 `/api` 与 `/api/v1` 都代理到 `18120`、`/v1` 代理到 `18110`。对外发布请用 nginx 反代（参考 `ops/deploy/nginx-nexus.conf`）。反向代理注入 `X-Verified-UID`（示例取上游 `X-Remote-User`），浏览器不能自己带这个头。`GET /api/v1/me` 返回当前 UID。没有代理时，页面把本地输入的 UID 放在 `X-Owner-Id` 里发送。不要使用 `?uid=`。

### 5.8 网关路由 deepseek-flash
在管理台「API 中转」或 API：
```bash
B=http://127.0.0.1:18120/api/gateway
curl -X POST $B/pools  -H 'Content-Type: application/json' -d '{"name":"deepseek-pool"}'
curl -X POST $B/nodes  -H 'Content-Type: application/json' -d '{"name":"deepseek","base_url":"https://api.deepseek.com","pool_id":"<pool>","api_key":"<从环境注入>","models":["deepseek-flash"],"request_timeout_seconds":90}'
curl -X POST $B/routes -H 'Content-Type: application/json' -d '{"model":"deepseek-flash","pool_id":"<pool>"}'
curl -X POST $B/nodes/<node>/probe     # 直到 HEALTHY → ACTIVE
```
远程 Worker 的模型写 `nexus-gateway/deepseek-flash`，baseURL 为内网 Gateway，例如 `http://10.0.0.8:18110/v1`。
路由可带 `target_model` 做请求模型 A → 实际模型 B 的透明转发；不填时目标模型等于请求模型。流式响应体不会回写 `model`，调用日志同时记录请求模型和 `actual_model`。

### 5.9 llm_wiki Web 版部署与项目创建
**主机选择**：首选 aarch64 + glibc ≥ 2.28 的 Linux 直接运行（`LLM_WIKI_DATA_DIR=/var/lib/llm-wiki ./start.sh` 或包内 `llm-wiki-web.service`）。x86_64 主机只能 qemu 仿真（实测可用但慢、吃内存）。CentOS 7 必须容器化。

在 `LLM_WIKI_HOST`（x86_64 CentOS 7）上的实际做法：
1. Docker：官方静态包 `docker-27.3.1.tgz` → `/usr/bin`，自写 `/etc/systemd/system/docker.service`（`ExecStart=/usr/bin/dockerd`），加 2 GB swap。
2. qemu：`/usr/bin/qemu-aarch64-static`（multiarch v7.2.0-1），手工注册 binfmt（内核 3.10 不支持 F 标志，因此还要把 qemu 拷进镜像）：
   ```bash
   echo ':qemu-aarch64:M::\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\xb7\x00:\xff\xff\xff\xff\xff\xff\xff\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff:/usr/bin/qemu-aarch64-static:' > /proc/sys/fs/binfmt_misc/register
   ```
   **重启后失效**，需加入开机脚本。
3. 镜像（`/opt/llm-wiki-web-build/Dockerfile`）：
   ```dockerfile
   FROM --platform=linux/arm64 ubuntu:22.04
   COPY qemu-aarch64-static /usr/bin/qemu-aarch64-static
   ADD node.tar.xz /opt/                       # node-v20.18.1-linux-arm64
   ENV PATH=/opt/node-v20.18.1-linux-arm64/bin:$PATH
   RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
   COPY app/llm-wiki-web-0.6.11-linux-aarch64/ /opt/llm-wiki-web/
   WORKDIR /opt/llm-wiki-web
   CMD ["./start.sh"]
   ```
   先 `sha256sum -c` 校验包内 tar.gz。
4. 运行：
   ```bash
   docker run -d --name llm-wiki-web --restart=always --network host \
     -e LLM_WIKI_DATA_DIR=/var/lib/llm-wiki -e LLM_WIKI_WEB_HOST=127.0.0.1 -e LLM_WIKI_WEB_PORT=18080 \
     -e LLM_WIKI_BACKEND_READY_TIMEOUT=180 -v /var/lib/llm-wiki:/var/lib/llm-wiki llm-wiki-web:0.6.11
   curl 127.0.0.1:18080/api/v1/health    # ok=true status=running mode=web
   ```
5. 隧道（box）：远端 `authorized_keys` 写受限公钥（CentOS 7 的 OpenSSH 6.6 **不支持 `restrict`**，用旧语法）：
   `no-pty,no-agent-forwarding,no-X11-forwarding,command="/bin/false",permitopen="127.0.0.1:18080" ssh-ed25519 … llmwiki-tunnel`
   然后 `tools/llm_wiki/llm_wiki_tunnel.sh`（`ssh -N -L 127.0.0.1:19828:127.0.0.1:18080` 的重连循环；修改后必须重启循环进程本身）。
6. 项目（无 GUI，走 Web runtime API）：
   - `POST /api/web/invoke {"command":"create_project","args":{"name":"astra","path":"/var/lib/llm-wiki/projects"}}`
   - `write_file` 写 `.llm-wiki/project.json` = `{"id":"<UUID>","createdAt":<ms>}`
   - `POST /api/web/store` 逐个 `set` 后 `save`：`lastProject`、`recentProjects`、`projectRegistry`、`apiConfig{enabled,token,mcpEnabled}`、`llmConfig{provider:"custom",customEndpoint:"https://api.deepseek.com/v1",model:"deepseek-flash",apiMode:"chat_completions",apiKey}`、`sourceWatchConfig{enabled:true,autoIngest:true,includeExtensions:["md","txt"],…}`、`embeddingConfig{enabled:false}`
   - `invoke open_project`；`GET /api/v1/projects` 应显示 `current:true`。
   - 验证 Source API：`PUT /api/v1/projects/<id>/sources/file {"path":"astra/probe.md","content":"…"}` → 200 `action: created`；`DELETE …?path=astra/probe.md` → 200。
7. Ingest pump（box）：`tools/llm_wiki/llm_wiki_ingest_pump.py` 保持无头 Chrome（`--user-data-dir ~/.llmwiki-chrome --remote-debugging-port=9333`）打开 Web UI，每 30 s 把 `raw/sources/**/*.md` 内容变化的文件 `enqueueBatch`，并在队列被自动暂停（`restoredBacklogWaiting`）时 `resumeProcessing()`。依赖 `websocket-client`。**脚本引用了打包资源的哈希文件名，换版本必须更新**。
8. Nexus：在 `start_astra.sh` 加 `ASTRA_LLM_WIKI_*` 后重启，`POST /api/library/service/resync`。

### 5.10 远程 Worker
1. 管理台/`POST /api/employees` 创建员工，`execution_environment`：`mode=LINUX_REMOTE`、`host`、`port=22`、`username=root`、`ssh_password`、`opencode_server_port=14096`、`workspace_root=/root/nexus-remote`、`model_name=nexus-gateway/deepseek-flash`。
2. `POST /api/employees/<id>/environment/deploy`（NDJSON 流，11 步：SSH、配置、依赖、Node、Git、OpenCode、工作区、全局配置、Serve 健康、网关鉴权+模型路由、图书馆 MCP）。
3. `POST …/environment/restart` 可重复执行；部署会改写远端 `~/.config/opencode/opencode.json`（自动备份）。
前提：`NEXUS_MCP_TRANSPORT=streamable-http` + `NEXUS_MCP_API_KEY`、`ASTRA_GATEWAY_MASTER_KEY` 已配置，网关路由 ACTIVE。

---

## 6. 验证清单

| # | 检查 | 命令 / 期望 |
|---|---|---|
| 1 | Redis | `redis-cli ping` → PONG |
| 2 | 统一 API | `curl :18120/api/health` → `{"status":"ok","product":"nexus"}` |
| 3 | Agent Service | `bash services/agent-service/healthcheck.sh` |
| 4 | Nexus API | `curl :18120/api/v1/knowledge`（200；AgentScope 可达且已配置知识库时 `available:true`） |
| 5 | Gateway / MCP | `ss -ltn` 有 18110、18201、18202 |
| 6 | Web | `curl -I :18120/`；`/chat /tasks /library /gateway/overview /settings/employees /settings/skills /settings/mcps` 200 |
| 7 | 网关 | 管理台节点 ACTIVE；`/api/gateway/routes` 含 deepseek-flash |
| 8 | 图书馆 | `curl :18120/api/library/service/status` → `enabled:true, healthy:true`；`POST /service/resync` 后知识 `sync_status=synced` |
| 9 | llm_wiki 检索 | `POST :19828/api/v1/projects/<id>/search {"query":"pytest","topK":5}` 有 `wiki/…` 命中 |
| 10 | 端到端 | `services/nexus-api/.venv/bin/python tools/e2e/e2e_library.py`（带 `ASTRA_LLM_WIKI_*`；仿真主机建议 `ASTRA_LIBRARY_E2E_WAIT=600`）→ exit 0，结果 `.astra/library-e2e-result.json` |
| 11 | 单测 | `cd services/nexus-api && .venv/bin/pytest tests/platform tests/nexus`；`ruff check app nexus_api tests`；均需全部通过 |
| 12 | 任务冒烟 | 员工私聊让其 `nexus_task_create` → Worker 执行 → 唤醒到源会话（见 LIVE_REPORT.md 用例 1-5） |
| 13 | 远程 Worker | deploy NDJSON 全部 PASS；派发任务后远端 OpenCode 以 `nexus-gateway/deepseek-flash` 运行 |

---

## 7. 排障（均为实际遇到过的问题）

| 现象 | 原因 | 处理 |
|---|---|---|
| 网关探测卡十几分钟、uvicorn 停在 "Waiting for background tasks" | DeepSeek 繁忙时持续发 `: keep-alive`，httpx 读超时被重置 | 修复：probe 外包 `asyncio.wait_for`（remote_fixes.diff）；`ASTRA_GATEWAY_PROBE_TIMEOUT_SECONDS=30`；必要时 kill 进程 |
| 远程 OpenCode "Response decompression failed / ZlibError" | 网关透传 `Content-Encoding: gzip` 但正文已解压 | `_STRIPPED_RESPONSE_HEADERS` 加 `content-encoding` |
| 503 NO_ELIGIBLE_NODE / "Service is too busy" | DeepSeek 上游不稳，节点被降级 | 稍后重试，手动 probe |
| Task MCP 启动报 `InvalidSignature: return type dict` | mcp structured_output 拒绝裸 `dict` | 注解改 `dict[str, Any]`（phase2_live_fixes.diff） |
| 私聊工具调用永远 `asking` | 私聊会话默认权限，Nexus Web 无审批 UI | 私聊也用 `UNATTENDED_PERMISSION`（副作用：私聊 Bash 被拒，见 §9） |
| dont_ask 会话里 nexus_task_* 全被拒 | MCPTool 非 readOnly 默认 ASK→DENY | agent_service.py 放行 nexus-task 工具 |
| `/nexus/task-hint` 一直 422 | `TaskHintBody` 定义在函数内 + `from __future__ import annotations` | 移到模块级 |
| 新员工邮箱没有任务技能/MCP | `attach_task_capability` 时 agent_id 还是 None | 传入新 agent_id；老员工重新保存 |
| 拒绝的运行时操作报 MissingGreenlet | rollback 后访问过期对象 | 调用前缓存 task_id |
| 暂停已结束回合的任务又跳回 RUNNING | lifecycle reconcile 短路条件不全 | lifecycle.py 修复 |
| 对端回复不出现在私聊 | `json.dumps` 转义换行导致 key 不匹配；跳过了含回复的消息 | fix_peer_replies.diff |
| 本地 Worker 环境测试失败 | OpenCode 端口与平台 Serve 14096 冲突、工作区目录不存在 | 改用其他端口并先建目录 |
| 任务卡 WAITING：`external_directory` | 仓库在任务工作区外，OpenCode 请求权限 | 回复 always，或把仓库放进工作区 |
| `./start_astra.sh restart` 后仍是旧配置，或提示没有停干净 | 旧进程的 pid 与 create_time 对不上，或 18120/18110 仍接受连接 | `astra_service.py dev stop` 会保留 `astra-service.json` 并以非 0 退出。确认记录里的 pid 已退出、18120/18110 不再接受连接后再 start |
| 部署 deploy 被拒：stdio MCP | 远程要求 streamable-http | 设 `NEXUS_MCP_TRANSPORT`/`NEXUS_MCP_API_KEY` |
| 节点凭据无法解密 | 换了/丢了 `ASTRA_GATEWAY_MASTER_KEY` | 固定保存主密钥；丢失只能重建节点 |
| 图书馆 "知识服务：异常 — 未启用 llm_wiki" | `ASTRA_LLM_WIKI_ENABLED` 未设 | 按 §2.6 设置并重启 |
| 同步全部 `HTTP 405` | 用了桌面版 llm_wiki（无 Source file API） | 换 Web Edition 0.6.11 |
| 知识是 candidate、不同步 | 只有 published 才同步 | `POST /api/library/knowledge/<id>/publish {"space_id":…}` |
| llm_wiki 已同步但搜索 0 命中 | Web 版 ingest 在浏览器端执行，无页面就不加工 | 运行 ingest pump（§5.9-7） |
| ingest 队列 `paused/restoredBacklogWaiting` | 页面重载时恢复了旧任务 | pump 自动 `resumeProcessing()` |
| `glibc` 太旧 / AppImage 跑不起来 | CentOS 7 | 容器（Ubuntu 22.04） |
| `authorized_keys` 里 `restrict` 导致公钥登录失败 | OpenSSH 6.6 不支持 | 用 `no-pty,…,permitopen=` 旧语法 |
| 修改隧道脚本后不生效 | 循环脚本已被 shell 读入 | kill 循环进程再启动 |
| 远程主机 SSH 无响应、load 20+ | 1.7 GB 主机同时跑桌面版容器 + 仿真 Web 版 + 两个 opencode | 删除旧容器、停掉无用 `opencode serve`（已停 17302，释放 ~230 MB）；加内存 |
| `e2e_library.py` 中途 "HTTP 请求超时" | 仿真主机 ingest 时过载 | 提高 `ASTRA_LLM_WIKI_TIMEOUT`、换更大主机 |
| live 测试偶发 Redis TimeoutError | 基础设施抖动 | 重跑 |
| 任务 `current_progress` 停在旧错误文本 | RUNNING→RUNNING 不刷新（未修） | 以 session 状态为准 |
| Nexus 员工抽屉读不到知识库 | AgentScope 未登记 embedding 模型，或知识库列表为空 | 在 AgentScope 登记可用的 embedding 模型并创建知识库；DeepSeek 聊天凭证不能当 embedding |

---

## 8. 重启与升级

**重启**（box 现状）：
```bash
cd /workspace/nexus-p2
ops/dev/start_astra.sh restart        # Task API、网关、Task MCP、管理台、Celery、OpenCode
ops/dev/start_agent_service.sh start  # 18000
ops/dev/start_nexus_api.sh start      # 18120
```
一键脚本部署的机器：`systemctl restart nexus-astra nexus-agent-service nexus-api [nexus-llm-wiki-tunnel nexus-llm-wiki-pump]`。Task MCP 与管理台随 `nexus-astra` 里的 `astra_service.py` 一起重启。
远端：`docker restart llm-wiki-web`（重启主机后先重新注册 binfmt）。

**升级**：
```bash
git -C $R fetch origin && git -C $R merge --ff-only origin/v1.4.8
cd $R/services/nexus-api && .venv/bin/pip install -e '.[dev]' && .venv/bin/python -m alembic upgrade head
cd $R/services/nexus-api && .venv/bin/pip install -r requirements.txt
cd $R/services/nexus-api/chat-island && npm ci && npm run build
cd $R/services/nexus-api/web && npm ci && npm run build
# 然后按上面重启；或直接重跑 ops/deploy/install_nexus.sh（幂等）
```
原地升级重跑安装脚本时，若还留着旧的独立单元 `nexus-task-mcp` 或 `nexus-web`，脚本会 `stop`、`disable`、删除 `/etc/systemd/system` 下对应单元文件并 `daemon-reload`。单元不存在时这一步直接跳过，不报错。Task MCP 与管理台之后只随 `astra_service.py` 启停。

v1.4.8 数据库版本边界为全新初始化，不支持从此前版本原地升级。不要把旧库 URL 直接用于这个版本；先准备两个新空库。后续同一 init 基线的更新才能追加迁移。

备份应包含 Nexus MySQL 库、AgentScope MySQL 库、Redis 持久化、AgentScope workspace/blobs/Qdrant、Nexus 附件与资源文件、llm_wiki 数据和受保护的凭据/加密根密钥。恢复必须保持数据库 ID 与文件/密钥一致，不能只备份数据库。正式 Linux 部署和业务恢复需在公司环境独立验收。

---

## 9. 已知限制

1. **知识库依赖 embedding 模型**：启动器已传 `CollectionPerKbManager` 和 Qdrant（`vdb-qdrant`）。知识库路由可用，但创建知识库仍需在 AgentScope 登记可用的 embedding 模型；未登记时 `GET /knowledge_bases/` 返回空列表，创建失败。DeepSeek 聊天凭证不能当 embedding。
2. **私聊 dont_ask**：为避免无审批 UI 时卡在 asking，私聊会话也是 dont_ask，私聊中普通 Bash 会被拒；绑定 MCP 的会话改用 `bypass`，否则 MCP Tool 会被转成 `DENY`。完整方案是给私聊加审批 UI。
3. **llm_wiki Web 版 ingest 需打开的网页**：加工逻辑在前端 JS，服务端不能推事件；无头 Chrome + pump 是变通方案，依赖打包文件名，不处理删除，进程不随开机自启（一键脚本用 systemd 托管）。
4. **aarch64 仿真**：x86_64 上靠 qemu-user，慢且内存大；binfmt 重启失效；生产应使用原生 aarch64 主机或 x86_64 构建。
5. **llm_wiki token 未强制**：Web 模式下 `/api/v1` 无 token 也返回 200，只靠 127.0.0.1 绑定 + SSH 隧道保护；不要开放端口。
6. 归档/删除来源后共享 Wiki 页面仍可能被检索（`archive_remaining_hits`），`library_related` 返回 unsupported。
7. Agent Service 的 `X-User-ID` 只是占位身份，对外前须加真实认证/反代。
8. 远程 Worker 隧道属于 Task API 进程，Task API 重启期间远端无法访问网关/MCP。
9. **透明路由与流式响应**：非流式响应会保留请求模型；流式 SSE 不会回写响应体的 `model` 字段，调用日志仍记录请求模型和 `actual_model`。
