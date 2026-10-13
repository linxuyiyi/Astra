# Nexus

Nexus v1.0.1：数字员工管理、数字员工私聊、团队交流。

当前处于阶段二。本仓库已包含独立部署的 **AgentScope Agent Service**
（`agent-service/`）与 **Nexus 业务后端最小服务**（`backend/`）。前端在
后续任务中建立；员工、私聊、团队等业务尚未开发。

## 架构与边界

- AgentScope 2.0.10 为官方固定版本，独立进程部署，Nexus 通过其公开 HTTP
  接口集成；不复制、Fork 或修改官方核心源码，也不替换原生路由。
- Nexus 后端只保存到 AgentScope 的连接配置（服务 URL、内部租户），不保存
  模型密钥或模型参数；模型配置统一由 AgentScope 服务管理。后端只通过公开
  HTTP 路由访问上游，不导入官方私有执行模块。
- 两个服务均只监听回环地址（AgentScope 默认 `127.0.0.1:8766`，后端默认
  `127.0.0.1:8765`），不直接暴露给浏览器。
- 无登录系统：浏览器匿名 UUID 只用于受控验证环境的会话隔离，不是可信
  认证凭据，不适用于正式生产环境。
- 模型凭据与默认模型绑定只由 AgentScope 服务管理；Nexus 页面不提供模型
  配置入口。

## 目录

```
agent-service/nexus_agent_service/   AgentScope 服务装配（配置、引导、app、入口）
backend/nexus_backend/               Nexus 后端（配置、HTTP 适配器、app、入口）
scripts/services.py                  两个服务的统一 start/stop/status 控制脚本
scripts/smoke_runtime.py             真实进程 + 真实模型运行时联调脚本
pyproject.toml / uv.lock             固定依赖
.env.example                         环境变量模板
data/                                运行时数据（被忽略）
evidence/phase2/                     脱敏验证证据
```

原生数据持久化在 AgentScope 服务下：

- SQLite：`data/agentscope/agentscope.db`
- 每会话 workspace：`data/agentscope/workspaces/`（`PER_SESSION` 隔离）
- PID：`data/agentscope/run/agentscope.pid`
- 日志：`data/agentscope/logs/agentscope.log`

## 安装

需要 [uv](https://docs.astral.sh/uv/) 与 Python 3.12（uv 会自动管理）。

```bash
uv sync
```

## 配置

复制模板并按需填写；`.env` 与任何密钥都不会被提交。

```bash
cp .env.example .env
# 编辑 .env，至少可留空 AGENTSCOPE_MODEL_API_KEY 先只跑原生 CRUD
```

AgentScope 服务关键变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AGENTSCOPE_SERVICE_HOST` | `127.0.0.1` | 监听地址，仅回环 |
| `AGENTSCOPE_SERVICE_PORT` | `8766` | 监听端口 |
| `AGENTSCOPE_DATA_DIR` | `data/agentscope` | 持久化目录 |
| `AGENTSCOPE_INTERNAL_USER_ID` | `nexus-service` | 内部服务租户（后端共用） |
| `AGENTSCOPE_MODEL_API_KEY` | 空 | DeepSeek 密钥；缺省时模型未配置 |
| `AGENTSCOPE_MODEL_BASE_URL` | `https://api.deepseek.com` | 模型服务地址 |
| `AGENTSCOPE_MODEL_NAME` | `deepseek-flash` | 默认模型 |
| `AGENTSCOPE_MODEL_PARAMETERS` | 空 | 可选，默认模型 parameters（JSON 对象） |

Nexus 后端关键变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `NEXUS_BACKEND_HOST` | `127.0.0.1` | 后端监听地址，仅回环 |
| `NEXUS_BACKEND_PORT` | `8765` | 后端监听端口 |
| `NEXUS_BACKEND_DATA_DIR` | `data/nexus-backend` | 后端运行数据目录 |
| `AGENTSCOPE_SERVICE_URL` | `http://127.0.0.1:8766` | 后端连接的服务地址（仅回环） |
| `NEXUS_BACKEND_REQUEST_TIMEOUT` | `30` | 上游请求超时（秒） |
| `NEXUS_BACKEND_CONNECT_TIMEOUT` | `5` | 上游连接超时（秒） |

配置只从进程环境或仓库根 `.env` 读取，不读取用户 Home 凭据，也不输出密钥。

## 启停与健康检查

两个服务共用同一套 nonce/锁/归属控制，通过统一脚本操作：

```bash
uv run python scripts/services.py start agentscope
uv run python scripts/services.py start nexus-backend
uv run python scripts/services.py status agentscope
uv run python scripts/services.py status nexus-backend
uv run python scripts/services.py stop nexus-backend
uv run python scripts/services.py stop agentscope
```

脚本只操作自己启动的进程：每次 start 生成随机实例标识（nonce）并以
`--instance-id` 传给子进程、写入 JSON PID 记录；同 data_dir 的启停用
文件锁互斥，防止双 start 覆盖。`start` 若发现已存在的本实例进程，会按
健康接口比对同一 nonce、HTTP 200 与 `ready=true`（对应各自服务健康语义）
决定退出码：健康返回 0、未就绪返回 1，**绝不自动重启**；`status` 用同一
判据。`stop` 依 `ps` 精确校验解释器、模块与 nonce 的 argv 归属，因此即使
实例健康接口未就绪也能安全停止它；归属不符则拒绝，避免误杀。只加载被
操作服务自己的配置，另一个服务配置非法不会阻塞本次启停；未知服务或目标
服务配置非法时明确失败。旧纯整数 PID 文件不再猜测兼容，直接拒绝操作。
端口被占用时启动会明确失败。

健康检查分层：

- 原生 `GET /health`（需 `X-User-ID` 头）：只证明组件已挂载与初始化，
  **不探测数据库、模型或网络**，通过不等于模型可用。
- 扩展 `GET /nexus/health`、`GET /nexus/defaults`：区分“服务健康”与
  “模型未配置”。`/nexus/health` 在 lifespan 完成前返回 `503`，退出后
  清除就绪；`model_configured` 只表示配置存在，不代表真实模型已可用。
- 后端 `GET /api/health`：实际调用上游 `/health` 与 `/nexus/health`，核对
  固定版本（2.0.10）与初始化状态；上游不可达、版本不符或未就绪时返回
  `503` 且 `status=unavailable`，不伪造成功。`ready` 仅表示后端进程
  lifespan 已完成。

`GET /nexus/defaults` 返回供 Nexus 创建原生 Session 的默认
`ChatModelConfig`（`credential_id`/`type`/`model`/`parameters`），**绝不
返回密钥**。

## 默认模型引导

服务启动时用官方公开 `CredentialBase`/`StorageBase` API 幂等写入一条
固定 ID 的 DeepSeek 凭据，重复重启不会新建重复凭据。若本次未提供
`AGENTSCOPE_MODEL_API_KEY`，但原生存储中已存在该内部租户 + 固定 ID 的有效
DeepSeek 凭据，则复用该持久凭据并仍视为已配置，无需每次重启重复提交密钥。
密钥只来自服务配置或官方存储，绝不出现在日志与响应中。
`AGENTSCOPE_MODEL_PARAMETERS` 可选，用于设置原生默认模型 `parameters`
（如 `{"max_tokens":1024,"thinking":false}`）；留空则保持官方默认值，不
额外配置其它模型。

## 测试与验证

```bash
# 配置、适配器合同与扩展边界单元测试（含真实官方 ASGI 服务合同）
uv run pytest

# 真实进程冒烟：原生 Agent CRUD、Session、PER_SESSION 隔离、重启恢复
uv run python tests/agent_service/smoke_service.py

# 后端真实健康连通（无需模型密钥；实际启动两个真实进程并核对版本）
uv run python scripts/smoke_runtime.py --connectivity-only

# 真实模型运行时联调（需注入 AGENTSCOPE_MODEL_API_KEY）
uv run python scripts/smoke_runtime.py
```

`pytest` 覆盖三类：不启动服务的纯单元测试；`httpx.MockTransport` 标记的
适配器/健康边界单元测试（不冒充联调）；以及针对真实官方 ASGI 服务
（真实 lifespan + SQLite）的合同测试。

`scripts/smoke_runtime.py` 通过适配器实际创建原生测试 Agent 与两个
`PER_SESSION` Session；每轮先读历史游标，再触发 `POST /chat`，随后按该
游标订阅 `GET /sessions/{sid}/stream`（覆盖触发到订阅之间的 replay），
收集 `REPLY_START`/`TEXT_BLOCK_DELTA`/`REPLY_END`，要求每轮
`finished_reason=completed` 且含 REPLY_START、非空 TEXT_BLOCK_DELTA 与
REPLY_END，等待 `/status` 真正 `idle` 后再按历史校准回复；验证随机口令两轮
上下文、会话隔离，并在 AgentScope 真实进程 stop/start 且**不注入 env 密钥**
后继续第三轮、验证持久化与凭据复用。未配置密钥时脚本明确退出非零并标记
未验证，**不 fallback 假模型、不伪造回复**。证据写入 `evidence/phase2/`
（脱敏，不含密钥）。

## 验证层级与限制

准确区分以下层级，未实际运行的检查不得报告为通过：

- **SDK/源码研究**：固定版本源码与 OpenAPI 契约核对，不等于运行验证。
- **ASGI 合同测试**：真实官方 lifespan + SQLite，进程内，不经过网络。
- **真实进程验证**：`scripts/services.py` 启停真实 Uvicorn 进程与真实 HTTP
  连通（`--connectivity-only`）。
- **真实模型验证**：需有效 `AGENTSCOPE_MODEL_API_KEY`，由外部安全注入后
  运行 `scripts/smoke_runtime.py`；未注入时如实标记未验证。

当前限制：未验证真实 Uvicorn 进程的模型多轮推理（待密钥）、附件、知识
检索、员工删除归档、匿名 owner 过滤与三员工主动通信；单进程
`InMemoryMessageBus` 不保证多进程事件总线恢复。
