# Nexus

Nexus v1.0.1：数字员工管理、数字员工私聊、团队交流。

2026-10-15 产品阶段核验：第一阶段数字员工体系尚未交付，技术初始化完成不能代替产品验收。缺项见 [第一阶段门槛](docs/PHASE1_GATE.md)；用户指定历史分支的 [第二阶段复用分析](docs/PHASE2_REUSE_ANALYSIS.md) 已整理，但尚未开始功能迁移。自主多员工职责协作延后第三阶段，不作为第二阶段前置。

当前处于阶段三（员工管理）。本仓库已包含独立部署的 **AgentScope Agent
Service**（`agent-service/`）与 **Nexus 业务后端**（`backend/`）。阶段 3.1
已实现员工业务存储、真实原生 Agent 创建/修改、列表/搜索/详情与服务端校验；
3.2.1 已通过公开 WorkspaceManager 扩展修复同员工双 Session 的物理 workspace
目录隔离并补真实写入与进程重启证据；3.2.2 已实现真实资源目录、员工默认资源
关联与启停、共同原生 Session factory（Conversation 绑定）与服务端运行 guard
（工具允许目录 + 内部授权回调）。删除归档（3.2.3）与前端页面（3.3）尚未开发，
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
scripts/smoke_resources.py           资源目录/Session 绑定/运行 guard 真实进程冒烟
scripts/smoke_knowledge.py           知识库本地 embedding 真实索引/检索/重启冒烟
tests/backend/fixtures.py            公开 SkillHubBase 测试夹具（真实 tar.gz 归档）
tests/backend/mcp_fixture_http.py    测试用真实无状态 Streamable HTTP MCP（正向）
tests/backend/mcp_fixture_stdio.py   测试用 stdio MCP（明确不支持的负向夹具）
pyproject.toml / uv.lock             固定依赖
.env.example                         环境变量模板
data/                                运行时数据（被忽略）
evidence/phase2/                     脱敏验证证据
evidence/phase3/                     员工管理脱敏验证证据
```

原生数据持久化在 AgentScope 服务下：

- SQLite：`data/agentscope/agentscope.db`
- 每会话 workspace：`data/agentscope/workspaces/`。原生 Session 持久化
  `PER_SESSION` workspace ID，Nexus 用公开 `WorkspaceManagerBase` 扩展
  （`SessionWorkspaceManager`）按该 ID 的 SHA-256 建独立目录，因此同员工
  不同 Session 的物理文件互不复用；官方 `LocalWorkspaceManager` 2.0.10 的
  workdir 固定为 `basedir/agent_id`，不能提供这一隔离。
- 知识库（可选，启用时）：向量库与文档 blob 默认从实际
  `AGENTSCOPE_DATA_DIR` 派生为 `<data_dir>/knowledge/vector` 与
  `<data_dir>/knowledge/blobs`，隔离实例不会落入项目默认目录。
- 原生持久写入观察索引（任务 3.2.3.1）：`data/agentscope/persistence/`，
  只保存消息/State 提交证明**摘要**（不保存消息/State 全文），生命周期由服务
  lifespan 管理；打开/校验失败服务不进入就绪。

运行冻结扩展与原生服务同时装配：`POST /chat/` 在同一 Agent 短栅栏内核对 Nexus
只读运行事实，Session 运行锁取得前后再次核对；员工进入持久 `deleting` 后禁止新
运行。内部 `POST /nexus/internal/freeze` 需要内部 token 和精确租户，等待真实
chat/background Task、运行锁与 registry 自然排空，再读回未确认写入；失败或超时
不允许删除。内部准备操作只保存稳定删除身份，尚未提供业务 DELETE/归档。
`accepted_input_archival=not_verified` 单独报告，不能将执行排空当作输入归档完整。
真实模型取消尾部验证命令为 `python scripts/smoke_freeze.py --with-model --keep-evidence`，
模型凭据仅从环境读取；失败保留数据，缺凭据退出 2 并标 skipped。

- PID：`data/agentscope/run/agentscope.pid`
- 日志：`data/agentscope/logs/agentscope.log`

Nexus 员工业务扩展持久化在：

- SQLite：`data/nexus-backend/nexus.db`（受 `NEXUS_BACKEND_DATA_DIR` 控制，
  SQLite/SQLAlchemy 事务只做短状态读写；新库启动时直接建表，不做旧模型
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
| `NEXUS_INTERNAL_TOKEN` | 空 | 服务端内部秘密（运行 guard 回调鉴权），两进程共享；留空时控制脚本生成并复用 |
| `NEXUS_GUARD_URL` | `http://127.0.0.1:8765` | AgentScope 回调 Nexus 内部授权端点地址（仅回环） |
| `AGENTSCOPE_KNOWLEDGE_ENABLED` | `false` | 是否启用官方知识库 |
| `AGENTSCOPE_KNOWLEDGE_VECTOR_DIR` | 由 `AGENTSCOPE_DATA_DIR` 派生 | 持久化 Qdrant 向量库目录（显式覆盖时按项目根解析） |
| `AGENTSCOPE_KNOWLEDGE_BLOB_DIR` | 由 `AGENTSCOPE_DATA_DIR` 派生 | 知识文档 BlobStore 目录 |
| `AGENTSCOPE_EMBEDDING_HOST` | `http://127.0.0.1:11434` | 外部 Ollama 地址（模型层依赖，本服务不管理该进程） |
| `AGENTSCOPE_EMBEDDING_MODEL` | `embeddinggemma:300m` | embedding 模型 |
| `AGENTSCOPE_EMBEDDING_DIMENSIONS` | `768` | 向量维度 |
| `AGENTSCOPE_EMBEDDING_CREDENTIAL_ID` | `nexus-default-ollama-embedding` | 固定凭据 id（重启复用） |

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
- 请求体未知字段显式拒绝（`extra="forbid"`）。创建/修改支持 `enabled` 与真实
  Knowledge/Skill/MCP ID 关联，资源按实际目录校验，启停由服务端运行 guard 生效；
  员工删除与只读归档仍未完成。
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

## 资源目录、默认关联与运行 guard（阶段 3.2.2）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/resources` | 三类资源目录安全投影（`skills`/`mcps`/`knowledge`） |
| `GET` | `/api/resources/{kind}` | 单类目录（`kind`＝`skill`/`mcp`/`knowledge`） |
| `POST` | `/api/conversations` | 创建 Conversation 并调用共同原生 Session factory（需 `X-Nexus-Owner`） |
| `POST` | `/api/internal/authorize-run` | 内部运行授权（需 `X-Nexus-Internal-Token`，仅服务端） |

- **资源目录投影**：读取官方 `GET /skill`、`GET /mcp`、`GET /knowledge_bases/`，
  每类区分 `available=true` 的真实条目（可为真实空数组）与 `available=false`
  的不可用（官方 503 feature-disabled 或传输/协议错误）并给出安全原因；只投影
  顶层 ID、名称、描述、enabled/可用状态与 `selectable`/`reason`，绝不返回 MCP
  原始 config、env、认证 URL 或 credential。投影为严格契约：任一官方条目不符合
  契约（非法 name/enabled）时整类标记不可用，绝不用“少一条的成功空目录”冒充
  正常；知识库目录按 `total/page/page_size` **完整分页**，第 101 个资源不会被
  误判为不存在。**MCP 支持范围（v1.0.1）**：只支持官方**无状态 Streamable
  HTTP**，由最小纯函数 `mcp_support` 统一判定（`is_stateful is False`；
  `mcp_config.type == 'http_mcp'`；HTTP(S) URL 且 path 不以 `/sse` /
  `/messages/` 结尾）。官方 `GET /mcp` 的 `MCPView` 刻意隐藏 `mcp_config`，
  无法据此判断传输类型；因此在独立 AgentScope 应用新增极小只读
  `GET /nexus/mcp-support`，按同一内部租户从公开 `StorageBase.list_mcps` 读取
  真实 `MCPRecord.client` 计算，仅返回 `[{id,selectable,reason}]`，不连接 MCP、
  绝不返回配置/URL/env/header/credential。库目录与该安全支持目录按 ID **一一
  完整合并**，缺项/重复/非法字段/请求失败一律 `available=false`，绝不 fallback
  为可选。stdio、SSE 与 stateful 条目安全投影为 `selectable=false` + 安全原因，
  绝不把 stdio 错配成无状态；Skill/Knowledge 正常 `selectable=true`。
- **员工默认资源关联与启停**：`POST/PATCH /api/employees` 支持 `enabled` 与
  `knowledge`/`skills`/`tools`（列表仅稳定 ID，`tools` 对应 MCP）。保存前核对
  真实目录资源存在且 enabled，MCP 还须 `selectable`；失效、不可选或目录不可用
  明确失败（`422`/`503`）；禁用可
  持久，启用只允许在一致（ready）且未删除状态。职责/资源/启停的组合保存复用
  既有短事务与写入意图：原生未确认前不先应用本地资源/启停，避免部分成功。
- **共同 Session factory**：`POST /api/conversations` 要求合法 UUID 的
  `X-Nexus-Owner`（缺失/非法拒绝，不退化为共享默认身份）与**必填**、有界的
  `client_key`（缺失/空/超长 `422`，保证每次创建可重复定位）。运行 guard 未配置
  时拒绝创建可运行会话。每个 Conversation 真实创建原生 Session、绑定
  `/nexus/defaults` 模型、按员工默认关联落实 Knowledge config 与 Skill/MCP
  from-library，并**再次读回** workspace 实际能力；核对原生实际
  `chat_model_config`、非空稳定 `workspace_id` 与 Knowledge/Skill/MCP（含 MCP
  真实 `is_healthy is True` 与传输支持）全部一致前不返回 `ready`。创建意图
  （原生 Agent id、期望模型、Knowledge/Skill/MCP 稳定 ID、服务代次）在占位落盘
  时持久化，恢复只按该意图与精确 `nexus-conv-*` Session 名核对，不从后续被编辑
  的员工默认值重新猜目标。**所有创建/恢复路径进同一员工锁并重新读行**，避免在途
  能力 I/O 与同 key 重试交错删除已返回 `ready` 的 Session。同一 owner +
  `client_key`：已 ready 返回 `200`，参数不同返回 `409`，未完成则按精确标记最小
  核对恢复，**不重复 POST**；异常 2xx/5xx/超时保留稳定标记，仅明确未受理的 4xx
  清理占位。安全释放必须先确证当前代次与记录代次不同，再**重新扫描**精确标记并
  核对扫描前后代次稳定（绝不复用更早的 absence）；重扫到既有记录按原意图恢复，
  代次再变/读取失败保留占位。已知 Session id 且原生确认缺失时同代次即可收敛清理
  （与未知 POST 的 marker 缺失区分），DELETE 成功或确认原生已不存在都收敛为已清理
  （不吞任意 404/其他失败、不重建新 Session）。绑定与清理均校验真实
  `Session.agent_id` 与本 Conversation 精确 `Session.name` marker：不匹配的 Session
  绝不 DELETE（官方 delete 忽略 agent_id），保留未完成并明确失败。
- **运行 guard（始终安装、fail-closed）**：AgentScope 服务通过公开
  `extra_agent_middlewares` 在每次真实运行前（含后台触发）回调
  `/api/internal/authorize-run`。**正式 build 始终安装 guard**：配置了内部秘密
  时回调 Nexus，否则使用拒绝一切的 `DenyAllAuthorizer`，没有“离线跳过”的生产
  开关（测试可显式注入 authorizer 并标记 mock）。`/nexus/health` 与
  `/nexus/defaults` 返回 `guard_configured`，Nexus 不会创建被当作 ready 却缺
  guard 的会话。端点核实员工启用/ready/未删除、可信 Conversation/Session 绑定、
  当前 Knowledge/Skill/MCP 有效性（与资源投影同源，含目录 `enabled`/`selectable`
  与 MCP workspace `is_healthy is True`）与实际装载，并在目录网络核对
  **之后**用短事务再读一次 Employee 与 Conversation（禁用/冻结/非 ready 已生效
  即拒绝）。失联、鉴权失败或资源失效一律 fail-closed。内部秘密仅服务端配置、
  恒定时间比对、不下发前端、不写日志；控制脚本保证两个进程共享同一秘密，
  status/stop 不产生秘密文件。
- **工具允许目录（精确允许集合）**：授权端点返回可信 Session 能力快照对应的
  **所选 MCP 名称、实际允许 Skill 名与 MCP 工具名**；guard 据此在 `on_reply`
  **重建** `agent.toolkit`，**只装配精确所选**的 Skill 与受控工具本地 facade，
  空选择视为空（绝不当作通配），彻底移除 Bash/PowerShell、Read/Write/Edit/Glob/
  Grep、Task/Team/Schedule、`reset_tools`、未选资源与动态 `mcps` 源。工具名可能
  跨 MCP 冲突（`a`+`b__c` 与 `a__b`+`c` 都得 `mcp__a__b__c`），因此 guard 先按
  授权返回的 selected MCP 名称精确筛选客户端（逐项存在/唯一/支持），再仅枚举
  这些客户端核对 expected 工具完整覆盖与跨所选歧义；未选客户端绝不被
  `list_tools` 调用，所选 MCP 真实 0 工具也仍核对存在/支持，绝不靠工具名前缀
  推断 MCP 身份。实际安装必须**完整覆盖**授权返回的 expected 名称：缺 Skill/
  工具即运行前 fail-closed，绝不因官方 workspace 吞掉失败而退化为裸 LLM 成功。
  只读夹具工具以官方 `ToolAnnotations(readOnlyHint=True)` 如实声明语义，原生
  `PermissionEngine` 读取真实 schema 后对只读工具走 ALLOW 快速路径；生产非只读
  工具的原生 ASK/DENY 语义保持不变，不强制 ALLOW、不设 BYPASS、不伪造工具结果。
  facade 泛化为受控 `ToolBase`（`is_mcp=False`/`is_state_injected=True`/
  `is_external_tool=False`），保留原工具属性与安全语义并委托其
  `__call__`/`check_permissions`/`check_read_only`，以真实 `agent.state` 的
  session 身份核对闭包 Session；**原生 Skill 工具同样套受控 facade**（委托原
  技能逻辑），未选不虚构 Skill，`CompressContext` 仅真实存在时保留。
  `on_model_call` 过滤 schema；`on_check_permission` **只拒绝未选工具**，选中
  工具交回原生权限链并保留其 DENY/ASK，不把 ALLOW 改成默认绕过。因
  `is_state_injected=True`，官方 `ToolOffloadMiddleware` 会**同步执行**受控工具
  （含 Skill），长工具随原生 run 等待/可取消，不转入后台任务、不冒充 external。
  读取 Skill Markdown 不等同执行脚本，脚本型技能缺少允许工具时明确能力限制。

## 知识库本地检索（阶段 3.2.2）

原生知识库（建库/上传/索引/检索）由 AgentScope 服务管理，Nexus **不新增知识
库管理页面或第二套 CRUD**；Nexus 只做三件事：把资源目录和员工默认关联暴露给
产品路径、在共同 Session factory 里把所选 KB 落到原生 Session 的
`knowledge_config`（显式 `{"mode":"static","top_k":3,"emit_hint_event":true}`，
官方默认 `agentic` 不保证首轮检索）、并在运行 guard 里核实所选 KB 都已真实
检索。

- **模型层依赖（Ollama，非业务微服务、非模型配置页面）**：官方
  `OllamaEmbeddingModel` 经 `OllamaCredential` 记录连接信息；服务启动时按固定
  id `nexus-default-ollama-embedding` 用公开 `storage.upsert_credential` 引导
  凭据（重启同 id 复用），配置只从服务环境读取：

  | 变量 | 默认 | 说明 |
  | --- | --- | --- |
  | `AGENTSCOPE_KNOWLEDGE_ENABLED` | `false` | 是否启用官方知识库 |
  | `AGENTSCOPE_EMBEDDING_HOST` | `http://127.0.0.1:11434` | 外部 Ollama 地址 |
  | `AGENTSCOPE_EMBEDDING_MODEL` | `embeddinggemma:300m` | embedding 模型 |
  | `AGENTSCOPE_EMBEDDING_DIMENSIONS` | `768` | 向量维度 |
  | `AGENTSCOPE_EMBEDDING_CREDENTIAL_ID` | `nexus-default-ollama-embedding` | 固定凭据 id |

  向量库（官方公开 `QdrantStore`）与文档 blob（公开 `LocalBlobStore`）目录由
  Nexus 从实际 `AGENTSCOPE_DATA_DIR` 派生，其进入/退出由官方 `create_app`
  生命周期管理；不重复进入、不另建资源层。Ollama 服务由部署方提供并启停，
  **本服务不自动管理外部 Ollama 进程**。标准启停/下载：

  ```bash
  ollama serve                       # 启动本地模型服务（默认 127.0.0.1:11434）
  ollama pull embeddinggemma:300m    # 下载 embedding 模型
  curl -s http://127.0.0.1:11434/api/tags   # 健康检查
  ```

- **回环代理绕过**：官方 `OllamaEmbeddingModel` 直接构造
  `ollama.AsyncClient(host=...)`，没有公开的 `trust_env`/`client_kwargs` 入口，
  无法在其内部禁用代理。`scripts/services.py` 启动子进程时会合并标准
  `NO_PROXY`/`no_proxy`（保留用户既有列表与云端代理）并补入
  `127.0.0.1,localhost,::1`；手动 `python -m nexus_agent_service` 时需自行导出
  等价 `NO_PROXY`（模板见 `.env.example`），不做全局禁用代理或改系统设置。
- **严格检索边界（公开最小扩展，不重做 RAG）**：官方装配会**跳过**无法解析的
  KB，并在 static 检索异常时 log-and-skip 继续裸模型调用。服务用公开子类
  `ObservedCollectionPerKbManager`（继承官方 `CollectionPerKbManager`，只覆写
  `get_knowledge`）把原生 `KnowledgeBase` 包一层，`search` **仍委托原生实现**，
  仅在成功/失败时把本轮结果写入模块级 `ContextVar`（按轮独立、原生
  `asyncio.gather` 并发检索共享同一记录、结束清理）。运行 guard 在**模型调用
  之前**（下游首个事件、`MODEL_CALL_START` 之前）用可信授权快照中的 KB id
  核对：漏装/被跳过/检索异常一律 `RunNotAuthorizedError`，**不发布
  `MODEL_CALL_START`、不进入模型**；零结果的成功检索允许继续。不全局/session
  共享最近检索标记、不持久检索副本、不另建 RAG 状态机、不改核心。
- **知识库原生资源管理入口（官方公开 HTTP，供部署/联调）**：
  `POST /credential/` 引导 embedding 凭据；`POST /knowledge_bases/` 建库；
  `POST /knowledge_bases/{kb}/documents`（multipart `file`，`content_type`）
  上传；`GET /knowledge_bases/{kb}/documents/status?ids=` 轮询到 `ready` 且
  `chunk_count>0`（没有 `indexed` 枚举）；`POST /knowledge_bases/{kb}/search`
  按 `query`/`top_k` 检索。这些是 AgentScope 原生路由，不是 Nexus 产品接口。

## 测试与验证

```bash
# 配置、适配器合同与扩展边界单元测试（含真实官方 ASGI 服务合同）
uv run pytest

# 真实进程冒烟：原生 Agent CRUD、Session、同员工双 Session 物理目录隔离、重启恢复
uv run python tests/agent_service/smoke_service.py

# 物理隔离专项冒烟：真实进程 start/stop/start + 官方 /workspace/files 读写核对
uv run python tests/agent_service/smoke_workspace.py

# 后端真实健康连通（无需模型密钥；实际启动两个真实进程并核对版本）
uv run python scripts/smoke_runtime.py --connectivity-only

# 真实模型运行时联调（需注入 AGENTSCOPE_MODEL_API_KEY）
uv run python scripts/smoke_runtime.py

# 员工管理真实进程冒烟（无需模型密钥；真实创建/编辑并读取原生 Agent 记录）
uv run python scripts/smoke_employees.py

# 追加“经业务 PATCH 更新职责后新会话生效”的真实模型校验（需注入密钥）
uv run python scripts/smoke_employees.py --with-model

# 资源目录 / 员工默认资源关联 / 共同 Session 绑定 / 运行 guard / 重启恢复冒烟
# （真实两个进程；默认不推理，用“启用时有 MODEL_CALL_START、禁用后无”对照归因）
uv run python scripts/smoke_resources.py

# 追加真实模型启用轮与所选 MCP 真实工具轮（需 Root 注入 AGENTSCOPE_MODEL_API_KEY）
uv run python scripts/smoke_resources.py --with-model

# 知识库本地 embedding 真实索引/检索/失败边界/重启恢复冒烟（需外部只读 Ollama）
uv run python scripts/smoke_knowledge.py

# 追加真实模型 Knowledge 回答轮（需注入 AGENTSCOPE_MODEL_API_KEY）
uv run python scripts/smoke_knowledge.py --with-model
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

workspace 物理隔离由 `tests/agent_service/test_workspace_manager.py`（真实
`LocalWorkspace` + 真实 SQLite + 官方 ASGI）与
`tests/agent_service/smoke_workspace.py`（真实服务进程 start/stop/start + 官方
`/workspace/files`）覆盖：同员工双 Session 的 workspace ID 与实际物理目录不同，
A 写入的文件 B 经公开 workspace 读取能力不可读，真实进程重启后同 ID 重建同目录
且文件仍在，并核对路径穿越防护、并发同 ID 只初始化一次与 `close`/`close_all`。
`get_workspace`/`close`/`close_all` 共用一把生命周期锁：暂停初始化时并发关闭会
等待初始化结束，不会复活、漏关或产生同路径双实例。冒烟严格要求 `start`/`stop`
退出码为 0，并在重启前后核对服务 `generation_id` 已变化（否则视为未真实重启）。
**阶段二 `evidence/phase2/` 的早期证据只证明两个 Session 的 workspace ID 不同，
不证明物理文件隔离**；物理隔离以 3.2.1 的上述证据为准。

`scripts/smoke_runtime.py` 真实模型段（需密钥）已适配阶段 3.2.2 的共同业务
Session 绑定：通过后端 `POST /api/employees` 与 `POST /api/conversations` 建立
员工与两个 Conversation，再在绑定后的原生 Session 上跑随机口令两轮上下文与
隔离校验；**不再直接裸建原生 Session**（运行 guard 会拒绝未知 Session）。每轮
先读历史游标，再触发 `POST /chat`，随后按该游标订阅
`GET /sessions/{sid}/stream`（覆盖触发到订阅之间的 replay），收集
`REPLY_START`/`TEXT_BLOCK_DELTA`/`REPLY_END`，要求每轮 `finished_reason=
completed` 且含 REPLY_START、非空 TEXT_BLOCK_DELTA 与 REPLY_END，等待 `/status`
真正 `idle` 后再按历史校准回复；随后在 AgentScope 真实进程 stop/start 且**不注入
env 密钥**后继续第三轮、验证持久化与凭据复用。未配置密钥时脚本明确标记未验证，
**不 fallback 假模型、不伪造回复**。证据写入 `evidence/phase2/`（脱敏，不含密钥）。
`scripts/smoke_employees.py --with-model` 同样经 `POST /api/conversations` 建立
会话后真实提问，不绕过 guard。

资源/会话/guard 冒烟 `scripts/smoke_resources.py` 通过真实两个进程验证：资源
目录真实空态与不可用区分；**独立启动一个测试专用真实无状态 Streamable HTTP
MCP**（`mcp_fixture_http.py`，`is_stateful=False`、`http_mcp`、`enable_tools`
精确选择一个工具），用官方 `POST /workspace/mcp` 建立 library 并保存为员工
默认工具；新建 Conversation 后 workspace 读回健康且仅含所选工具；内部授权端点
在启用时放行、禁用后拒绝；并通过“启用状态下直接原生 `/chat` 出现
`MODEL_CALL_START`、禁用后同一操作无 `MODEL_CALL_START` 且以 guard 失败结束”
对照，把无模型调用归因于 guard，而非把任意失败当成功；重启严格要求 start/stop
退出码为 0 且服务 `generation_id` 变化，员工、资源关联与原生 Session 绑定恢复，
并实际重新发现 workspace 所选 MCP。
`--with-model`（Root 注入密钥）追加真实模型启用轮（要求 `completed` 且有文本）
与所选 MCP 真实工具轮（要求 `TOOL_CALL_START` 且回复含真实 MCP `compute_plus`
计算的 `42`），重启后真实再次调用所选工具；未注入时明确标记 `skipped`，不以
“错误密钥无回复”冒充 guard 生效。脚本不读取用户 Home，密钥由 Root 注入。

知识库冒烟 `scripts/smoke_knowledge.py` 通过真实两个进程与**外部只读**的本地
Ollama 实例验证完整原生 RAG：两个不同主题 KB + 真实 TXT 上传、轮询
`documents/status` 到 `ready`（`chunk_count>0`）、真实 `search` 命中含文档独有
随机值的 chunk；经产品路径把 KB 关联员工并用共同 factory 建会话，核对 Nexus
目录投影、员工关联与原生 Session `knowledge_config`（`static` 参数）对齐。运行
边界无需真实 chat 模型：空库零结果检索必须进入模型（出现 `MODEL_CALL_START`），
而 embedding 不可达（凭据 host 指向未监听端口）、两个所选 KB 之一不可构建、KB
被删除三种情形都必须真实流 `error` 且**没有** `MODEL_CALL_START`/成功回答（不
以“错误密钥无回复”冒充 guard 生效）。随后 stop/start 两个进程（严格退出码 0、
`generation_id` 变化）核对 KB/文档 chunk/检索/Session 关联仍恢复。`--with-model`
（Root 注入密钥）追加真实 SSE 含 `HINT_BLOCK` 且回答含文档独有值，重启后再次
验证；未注入时 `model_check` 明确 `skipped`。脚本只使用隔离 data_dir，绝不
停止/删除外部 Ollama 实例或模型目录；清理只删除本次创建的 KB/Agent。
证据写入 `evidence/phase3/`（脱敏）。

新增/更新自动化测试：`tests/backend/test_resources.py`（真实/模拟目录投影、
严格契约与 KB 跨页、脱敏、`mcp_support` 纯函数边界、安全支持目录完整合并失败/
无状态 SSE 明确不可选/secret 不出响应）、
`tests/backend/test_conversations.py`（真实 ASGI + 公开 SkillHub fixture 的
共同 Session 绑定、真实无状态 HTTP MCP 装载读回、stdio 负向验收、owner 隔离、
`200/409` 幂等、`client_key` 必填、并发同 key 单一致结果、首次绑定失败后重试、
异常 2xx/超时保留标记、按精确 marker 恢复且不再 POST、结果未知创建仅代次变化
后释放、释放前重扫 marker（竞态恢复/代次再变保留）、已知 id 缺失同代次收敛、
非本会话 Session 绝不 DELETE、确认缺失收敛清理、失效关联不可 ready）、
`tests/backend/test_mcp_http.py`（真实 FastMCP HTTP 服务经官方 `MCPClient` 的
发现/调用、`enable_tools` 精确选择、协议错误/超时/慢调用取消后再次可用、无悬挂
transport 任务、`readOnlyHint` 经原生 `PermissionEngine` 得 ALLOW）、
`tests/backend/test_guard.py`（真实内部授权端点、网络核对后
复核禁用、目录 enabled/selectable 与真实 workspace 客户端传输核对、工具名跨 MCP
碰撞不得用未选客户端补位且不枚举未选客户端、所选 0 工具仍核对存在/支持、有 tools
缺 mcps 即拒绝、guard 中间件精确装配/完整覆盖/缺项与同名歧义 fail-closed、空选择
不装任意 Skill、原生 Skill facade、`on_check_permission` 只拒绝未选并保留原工具
DENY、授权响应字段非法即拒绝、recording model 证明鉴权失败不进入模型）、
`tests/agent_service/test_services_control.py`
（内部秘密 env 优先/0600 原子创建/落盘失败明确失败/status 不产生秘密）、
`tests/agent_service/test_write_barrier.py`（托管 Agent/Session 创建代次头、
串行化与写屏障）。真实无状态 HTTP MCP 以 `tests/backend/mcp_fixture_http.py`
提供可验证计算工具（官方 `ToolAnnotations(readOnlyHint=True)`）；stdio MCP 以
`tests/backend/mcp_fixture_stdio.py` 作为**明确不支持**的负向验收夹具。

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

当前限制：阶段 3.1 实现员工业务存储、真实创建/修改与列表/搜索/详情及校验；
3.2.1 已用公开 `WorkspaceManagerBase` 扩展修复同员工双 Session 的物理目录
隔离并补真实写入与进程重启证据；3.2.2 已实现真实资源目录、员工默认资源关联与
启停、共同 Session factory（Conversation 绑定、能力读回）与服务端运行 guard
（工具允许目录 + 内部授权回调，含真实进程冒烟）；删除前只读历史归档（3.2.3）
与前端页面（3.3）尚未实现。真实模型已证明“改职责后新对话生效”，证据为
`evidence/phase3/smoke-employees-20261014T144442Z.json`。知识库默认未启用，未配置 embedding 时目录明确不可用；
3.2.2 已用 `scripts/smoke_knowledge.py` 在本机隔离进程对**外部只读** Ollama
（`embeddinggemma:300m`，768 维）完成**真实本地 embedding → 索引 → 原生
static 检索 → 员工 KB 绑定/会话 → 进程重启恢复**验证，并覆盖 embedding 不可达/
部分 KB 不可构建/KB 删除时模型调用前严格中止；**真实模型据检索内容作答**与
`HINT_BLOCK` 已经 `--with-model` 验收，证据为
`evidence/phase3/smoke-knowledge-20261014T151147Z.json`。启用入口为 `qdrant-client`
与 `agentscope[model-ollama]`（已在交付依赖内）及 `AGENTSCOPE_KNOWLEDGE_ENABLED`。
原生写屏障、单调写入修订与创建代次释放只在 **单进程部署**范围内成立：锁与高
水位保存在 AgentScope 服务进程内存中，不做多副本协调；`reconcile` 对未知编辑
结果要求先通过同修订幂等 `PATCH` 写屏障再确认 `desired`，未带 Nexus 标记的原生
探针不受影响。运行 guard 与工具允许目录同样只在单进程、单服务实例语义下保证。
剩余未验证：完整产品私聊/附件/历史界面、删除归档与基础团队交流；自主三员工
通信延后第三阶段，未宣称完成。真实模型多轮、资源工具与知识作答已有对应证据。
单进程 `InMemoryMessageBus` 不保证多进程事件总线恢复。所有 Agent 运行都
必须绑定到已授权 Conversation；直接裸建原生会话的旧探针会被 guard 拒绝。

**MCP 支持边界（v1.0.1，明确限制）**：AgentScope 2.0.10 的 stateful MCP 将
anyio transport scopes 保存在连接 task，跨 task/多连接关闭存在非 LIFO 退出问题，
公开封装不足以完整修复。因此本版本**只支持官方无状态 Streamable HTTP MCP**；
stdio、SSE 与 stateful 条目安全投影为不可选并在保存/恢复/运行处拒绝，不修改或
复制核心、不把 stdio 错配成无状态。真实无状态 HTTP 路径已用真实 FastMCP 服务
与官方 `MCPClient` 验证发现/调用及超时/取消后再次可用；远端操作取消不承诺回滚。
内部秘密在未显式配置时由
`scripts/services.py` 以 0600 原子写入 `data/.nexus-internal-token`（被忽略）；
落盘失败明确非零且不 spawn，不提供 ephemeral 回退。
