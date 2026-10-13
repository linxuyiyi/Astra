# Nexus

Nexus v1.0.1：数字员工管理、数字员工私聊、团队交流。

当前处于阶段三（员工管理）。本仓库已包含独立部署的 **AgentScope Agent
Service**（`agent-service/`）与 **Nexus 业务后端**（`backend/`）。阶段 3.1
已实现员工业务存储、真实原生 Agent 创建/修改、列表/搜索/详情与服务端校验；
资源真实绑定、启停执行约束、删除归档（3.2）与前端页面（3.3）尚未开发，
私聊与团队交流随后进行。

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
scripts/smoke_employees.py           员工管理真实进程冒烟脚本
pyproject.toml / uv.lock             固定依赖
.env.example                         环境变量模板
data/                                运行时数据（被忽略）
evidence/phase2/                     脱敏验证证据
evidence/phase3/                     员工管理脱敏验证证据
```

原生数据持久化在 AgentScope 服务下：

- SQLite：`data/agentscope/agentscope.db`
- 每会话 workspace：`data/agentscope/workspaces/`（`PER_SESSION` 隔离）
- PID：`data/agentscope/run/agentscope.pid`
- 日志：`data/agentscope/logs/agentscope.log`

Nexus 员工业务扩展持久化在：

- SQLite：`data/nexus-backend/nexus.db`（受 `NEXUS_BACKEND_DATA_DIR` 控制，
  PostgreSQL/SQLAlchemy 事务只做短状态读写；新库启动时直接建表，不做旧模型
  迁移）。该库只保存业务分栏、忽略大小写唯一性索引与唯一原生 `agent_id`
  关联；姓名与实际执行 prompt 以 AgentScope 原生 `AgentRecord` 为事实源。

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
  `/nexus/health` 还返回每次 build/start 新生成的非空 `generation_id`：它标识
  当前服务代次，用于判断旧单进程写请求是否已随重启退出（不是可空的控制脚本
  `instance_id`）。
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

## 员工管理接口（阶段 3.1）

无登录系统，员工是全局共享的管理对象（匿名 UUID 只用于会话归属，不按浏览器
复制员工）。接口只通过 Nexus 后端访问，浏览器不直连 AgentScope：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/employees?q=` | 员工列表；`q` 按**原生姓名**/责任领域/职责说明搜索 |
| `POST` | `/api/employees` | 真实创建原生 Agent 并保存员工扩展 |
| `GET` | `/api/employees/{id}` | 详情；合并本地扩展与原生记录 |
| `PATCH` | `/api/employees/{id}` | 编辑名称/领域/职责/工作指令/头像，真实更新原生 |
| `POST` | `/api/employees/{id}/reconcile` | 核对未完成写入与原生记录并恢复到明确状态 |

- 字段约束：`name` 必填、去首尾空白、最多 24 字符；`domain` 必填、最多 42
  字符；`responsibilities` 与 `instructions` 必填；`avatar` 从五个 emoji
  中选；名称与领域忽略大小写唯一，由数据库唯一索引兜底并发。
- 请求体未知字段显式拒绝（`extra="forbid"`）：尚未实现的资源关联与 `enabled`
  启停操作返回 `422`，不静默接受。创建固定 `enabled=true`；`enabled` 的启停
  执行约束与员工删除属于任务 3.2。
- 响应姓名以原生 Agent 为事实源：原生可用时 `name_source="native"`；原生
  缺失/非法时退回本地索引值并标记 `name_source="local"`、`native_available=
  false`、非 `ready`，不冒充原生事实。
- 跨服务写入先持久化**非 ready 与最小写入意图**（`sync_state` +
  `write_intent`），再调用原生：创建先落带稳定员工 ID 标记的占位并记录实际
  服务 `generation_id`，编辑先记录目标 prompt。原生超时/结果未知保持非 ready，
  不盲目重发非幂等请求；本地保存失败不回滚原生而是保留意图，由 `reconcile`
  核对原生记录后恢复 `ready`（原生仍为旧值时视为操作未生效并恢复 `ready`），
  无法判定则保持 `incomplete` 禁止新运行。创建与编辑编译进原生 `system_prompt`
  的稳定员工 ID 标记用于核对，不按名称或时间戳猜测。同一员工的编辑与恢复在
  单进程每员工锁内串行，锁不跨网络，数据库事务只做短状态更新（非分布式事务，
  不宣称原子性）。
- **原生写屏障与单调写入修订（单进程范围）**：AgentScope 服务装配一个公开
  纯 ASGI middleware，对同一 `/agent/{id}` 的原生 `PATCH`/`DELETE` 按
  `agent_id` 串行化**完整下游请求生命周期**：读取 body 前先取业务异步锁；
  外层请求被取消（客户端超时/断开）时 shield 实际下游任务，等待真实写入退出
  才释放锁，使后续写请求排队到真实提交之后。服务在锁内以公开
  `StorageBase.get_agent` 读取已持久化 prompt 的修订，结合本进程已接受高水位，
  拒绝低于当前修订的写请求（`409`），同修订幂等恢复可通过；已接受高水位在通过
  修订校验、进入原生写入前即提升，故即便高修订原生失败，后续低修订也不能迟到
  覆盖。`StorageBase.get_agent` 瞬时失败无法证明该记录非托管，返回脱敏 `503`
  并停止原生 `PATCH`（fail-closed），不放行未知记录。修订只认编译 prompt 末尾
  规范的“员工 ID 标记行 + 修订标记最后一行”对，职责/指令正文含相同标记文本
  不参与解析，且目标员工标记必须与当前员工一致；`revision` 头
  （`X-Nexus-Write-Revision`）必须与目标 prompt 内的修订标记一致。Nexus
  员工扩展保存单调 `write_revision`（每次创建/编辑写入意图递增，失败或恢复都
  不回退）。未带 Nexus 员工标记的原生独立 CRUD 探针维持官方行为。该保证仅限
  本项目单进程部署，不做跨副本协调，也不引入 operation 表或后台编排。
- **托管创建的代次绑定**：创建在原生 `POST /agent/` 前先读取并持久化服务
  `generation_id`，并通过公开头 `X-Nexus-Service-Generation` 携带该代次；
  服务在任何托管创建写入前与当前实例代次比较，不一致（预检后已重启）返回
  `409` 且**不执行**原生创建，使“被记录代次”与“实际受理实例”绑定。未带该头
  的独立原生创建探针维持官方行为。
- **创建未确认占位的释放**：创建结果未知且未在原生找到稳定标记时，仅当当前
  服务 `generation_id` 与创建意图记录的服务代次**明确不同**（即服务已重启、
  旧单进程 `POST` 已退出）且标记仍不存在，才允许 `confirm_not_created` 释放
  占位；同一代次返回 `409`，要求先重启 AgentScope 后再核对，不靠用户确认猜测
  旧请求已结束。

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

# 员工管理真实进程冒烟（无需模型密钥；真实创建/编辑并读取原生 Agent 记录）
uv run python scripts/smoke_employees.py

# 追加“经业务 PATCH 更新职责后新会话生效”的真实模型校验（需注入密钥）
uv run python scripts/smoke_employees.py --with-model
```

`pytest` 覆盖三类：不启动服务的纯单元测试；`httpx.MockTransport` 标记的
适配器/健康/员工故障边界单元测试（不冒充联调）；以及针对真实官方 ASGI 服务
（真实 lifespan + SQLite）的合同测试，包括员工 CRUD、真实原生绑定、唯一性、
原生姓名搜索、并发编辑串行与冲突。模拟故障测试覆盖创建/编辑超时的写入意图
持久化、不二次 POST/PATCH、非 ready、reconcile 恢复与标记失败不假称保护成功，
以及创建占位仅在服务代次变化后释放。真实 ASGI 写屏障测试
（`tests/agent_service/test_write_barrier.py`）对官方存储注入**可控延迟**，
复现“PATCH 受理后客户端超时取消 → 重放/新编辑排队 → 迟到旧写被拒”，验证
锁保持到真实写入结束、受管修订头一致性、未管理原生 CRUD 兼容与 DELETE 串行；
不调用真实模型。

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

员工管理冒烟 `scripts/smoke_employees.py` 通过真实进程验证员工创建、列表/
搜索（按原生姓名）、详情、编辑、忽略大小写重复、校验错误、并发冲突与上游
停止后的明确失败，并读取官方原生 Agent 记录证明真实创建/更新。`--with-model`
需密钥：经**业务 PATCH** 写入口令后用真实模型提问验证新会话生效，不直接覆盖
原生 prompt；未注入时 `model_check` 明确标记 `skipped`、只声明 CRUD 通过，
**不 fallback 假模型**、不写整体模型通过。证据写入 `evidence/phase3/`（脱敏，
不含密钥）。

当前限制：阶段 3.1 只实现员工业务存储、真实创建/修改与列表/搜索/详情及
校验；资源真实绑定、`enabled` 启停执行约束、删除前只读历史归档（3.2）与
前端页面（3.3）尚未实现，因此尚未用真实模型证明“改职责后新对话生效”
（脚本已就绪，待注入密钥验证）。原生写屏障、单调写入修订与创建代次释放只在
**单进程部署**范围内成立：锁与高水位保存在 AgentScope 服务进程内存中，不做
多副本协调；`reconcile` 对未知编辑结果要求先通过同修订幂等 `PATCH` 写屏障再
确认 `desired`，未带 Nexus 标记的原生探针不受影响。其余未验证：真实 Uvicorn
进程的模型多轮推理（待密钥）、附件、知识检索、匿名 owner 过滤与三员工主动
通信；单进程 `InMemoryMessageBus` 不保证多进程事件总线恢复。
