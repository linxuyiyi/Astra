# Nexus 部署简化与在线配置开发交接方案

> 状态说明（2026-10-20）：本文是 2026-10-17 的设计交接材料，文中的代码核对基线保留为当时快照。当前产品版本为 Nexus v1.4.7，分支 `codex/nexus-v1.4.7`；实施前以当前仓库、`AGENTS.md` 和 `openspec/config.yaml` 为准。

日期：2026-10-17。用途：交给 Grok 开发。本文是设计交接材料，尚未实施。

代码核对基线（2026-10-17 快照）：Astra 仓库中的 Nexus 分支 cursor/nexus-v1-0-1-phase2-f6f2，提交 c046abeda9cb4ade5d891df51e22948fba68f996。开发前重新核对仓库、分支、HEAD 和 git status，不覆盖已有未提交文件。

## 1. 目标与已经确定的取舍

用户的目标是：容易安装，能够快速打开系统，常改配置可以在网页中修改，关键配置持久保存，升级或重启后继续使用。

本轮按以下方案实施：

1. **默认 SQLite，不要求安装 MySQL。** MySQL 保留为现有部署的可选后端，不作为首启条件。SQLite 数据文件必须位于独立、固定的持久目录。
2. **平台动态配置保存到现有 task platform 数据库。** 网关、Worker、任务分类复用已有表，不再建立另一份副本。浏览器只保存界面偏好。
3. **没有模型 Key、知识库或远程 Worker 配置时，也能启动管理页面。** 页面说明哪些能力待配置；缺配置的能力不得伪装可用。
4. **保留 Redis、Celery 和独立 Agent Service。** Redis 继续承担 MessageBus、网关运行态和 Celery；不使用 InMemoryMessageBus 替代正式部署的消息总线。
5. **本轮不合并 API/MCP/网关进程，不重写 Runtime 状态机。** 统一 CLI 与发布方式即可改善使用体验，不承诺四个进程或四个容器。
6. **发布包携带预构建前端和匹配的 OpenCode Workspace UI。** 生产启动不再通过 Vite，也不要求现场构建前端。原有开发方式保留。
7. **配置属于哪个服务，就由哪个服务保存。** Agent、Session、模型凭据和对话记忆仍属于 AgentScope；Nexus 不复制一份到平台配置表。

模型页面的范围必须明确：当前 AGENTS.md 与 Nexus agentscope-deployment Spec 禁止 Nexus 提供模型、供应商和模型凭据编辑界面。因此，**首期在线配置覆盖平台、知识库连接、网关与 Worker；不新增聊天模型配置表单**。模型仍通过现有 bootstrap_chat_model.py 或同等 CLI 子命令登记到 Agent Service。若用户后续明确要求管理员网页也管理聊天模型，再新增相应契约，通过官方 AgentScope HTTP 接口保存，不能私自突破现有边界。

本方案允许调整 Agent Service 的官方存储适配器为 SQLite，但仍保留独立服务、RedisMessageBus 和 PER_SESSION 隔离。此变化需要在 Nexus 的新 OpenSpec capability 中说明，不修改 Astra 历史 Spec。

## 2. 真实代码基础与复用边界

| 已核实的事实 | 源码位置 | 开发要求 |
|---|---|---|
| task platform 默认支持 SQLite，并设置 WAL 与 busy_timeout | services/task-platform/app/config/settings.py、app/database.py | 复用数据库、Session 和 Alembic；不新建平台数据库 |
| 当前 get_settings 使用进程缓存，部分模块启动时捕获配置 | services/task-platform/app/config/settings.py、app/runtime/celery_app.py | 动态配置不能继续只从全局 Settings 读取 |
| 网关节点、路由、额度已落库，数据面周期刷新 | app/gateway/models.py、config.py、security.py | 原表和刷新机制继续使用 |
| Worker 环境已落库；/api/workers 当前主要是只读别名 | app/models.py、api/worker_routes.py、api/routes.py | 编辑和部署复用现有环境接口，不假设已有完整 Worker CRUD |
| AgentScope 当前使用 RedisStorage 与 RedisMessageBus | services/agent-service/agent_service.py | 不把 SQL 存储和内存总线绑定在一起 |
| AgentScope 2.0.10 官方提供 AsyncSQLAlchemyStorage | 已安装官方包 app/storage/_sql/_storage.py | 使用官方适配器；storage-sql extra 不包含 aiosqlite，须显式安装 |
| task platform 与 AgentScope 的 mcp 版本约束冲突 | task-platform/pyproject.toml、AgentScope METADATA | 必须保持独立 Python 环境 |
| 安装器未生成 Task MCP 的共享密钥 | ops/deploy/install_nexus.sh、app/task_mcp.py | init 补齐 ASTRA_TASK_MCP_KEY 与 NEXUS_TASK_MCP_KEY 的一致映射 |
| Workspace UI 依赖精确 OpenCode 版本及仓库资源路径 | app/api/workspace_routes.py、web/nexus/public/opencode-workspace | 发布清单与后端资源定位一起验证 |
| llm_wiki 的设置主要保存为 app-state.json | llm_wiki/src/lib/project-store.ts | 借鉴操作方式；平台动态配置改用数据库 |

本次读取到 task platform Alembic 单 head 为 0036_experience_space_and_key_last_used。开发时必须重新运行 alembic heads，再确定新 revision 的父节点，不能照抄编号。

当前 openspec/config.yaml 还包含“新代码放在 nexus/”的旧路径约束，与现行 AGENTS.md 和已迁移的 services/、web/nexus 目录不一致。实施前在 proposal 中列明并收敛这处文档冲突，采用现行产品目录；不要把未跟踪的旧 nexus/ 树当成开发根目录。

## 3. 用户操作流程

### 3.1 新安装

1. 下载发行包，执行安装入口；安装器准备运行依赖，不要求填写模型 Key、MySQL 密码或 wiki 配置。
2. 执行 nexus start。首次运行自动完成初始化；默认只监听 127.0.0.1。
3. 打开 CLI 输出的管理页面地址，进入“快速设置”。页面启动和模型可用性分别展示。
4. 快速设置提供知识库、网关、Worker 的连接入口，未启用的能力可以跳过。
5. 支持“测试连接”“保存并应用”，显示持久化结果、实际连通状态和配置生效范围。
6. 后续启动从持久目录恢复配置；不重复询问，不重新生成已有密钥。

快速设置是配置页面的引导视图，完成后仍可在设置页修改，不再维护第二套表单和状态。

### 3.2 既有安装

1. 先备份，再运行一次性旧配置导入。
2. 数据库中已经保存过的设置不得被旧 .env、安装脚本或默认值覆盖。
3. AgentScope 原 RedisStorage 部署保持原后端，完成独立的冷迁移后才能切换 SQLite。
4. 切换前后保留 Agent、Session、credential 和 Nexus 绑定标识，不能通过重建员工绕过迁移。

## 4. 配置分类与首期字段

| 分类 | 首期配置 | 事实源 | 生效规则 |
|---|---|---|---|
| 知识库连接 | enabled、api_base_url、project_id、timeout_seconds、api_token | task platform 新设置表 | 下一个知识库操作读取新配置 |
| 外部 NR 任务源 | enabled、endpoint、discover_path、result_path、token | task platform 新设置表 | 下次 scout 周期读取；默认关闭 |
| 网关 | 节点、路由、Key、额度、并发、探测参数 | 现有网关表 | 沿用现有周期刷新，不另写配置副本 |
| Worker | SSH 地址与端口、用户、工程目录、OpenCode 路径与端口、现有执行开关 | 现有 Worker/环境表 | 沿用空闲检查、部署互斥与环境维护流程 |
| 任务分类与提示词 | 分类、排序、启用、任务/问题提示词 | 现有分类表 | 后续任务使用 |
| 聊天模型与凭据 | 官方 credential、Agent/Session 绑定 | AgentScope 官方存储 | 首期保留 CLI 登记，无 Nexus 模型编辑表单 |
| 启动参数 | 数据目录、数据库定位、监听地址与端口、Redis 地址、服务内部地址、存储后端 | 持久 bootstrap 文件及明确启动参数 | 重启生效 |
| 内部身份密钥 | 管理密钥、加密主密钥、Library/Task MCP 共享密钥 | 持久 secrets 文件 | init 自动生成；不在通用设置表中编辑 |

首期不把所有环境变量都搬进网页。心跳阈值、Runtime 恢复周期、Celery beat 间隔、进程端口、数据库地址、Redis 地址和根目录仍是启动配置，页面只读展示。核心 Runtime 超时等在线调整留到独立行为评审，不借本轮顺手扩展。

知识库地址仍遵守当前 http://127.0.0.1:<port>/api/v1 约束；远端服务通过已有隧道接入。不为支持在线表单而放宽地址策略或增加一层猜测式 fallback。

已有知识数据时，首期不允许直接覆盖 project_id；返回 409 并说明需要先备份和完成项目迁移。首次配置、无知识数据时可修改。Token、超时和同一项目的连接参数可以更新；不得清空现有知识记录或静默改写同步状态。

## 5. SQLite 与持久目录

本轮默认单机部署，无 MySQL 依赖。不要把不同服务的数据强行合成同一张表或同一数据库。

建议目录如下；现有部署允许显式指定等价绝对路径：

~~~text
$NEXUS_STATE_DIR/
  bootstrap.json              # 启动定位信息，不含业务模型 Key
  secrets.env                 # 0600；目录 0700；已有密钥不重建
  task-platform/astra.db      # 任务、网关、平台设置等；沿用现有库
  nexus-api/nexus.sqlite3     # 员工目录、会话绑定、待接受消息
  nexus-api/files/            # 附件
  agent-service/agentscope.db # 新安装使用官方 SQL Storage
  agent-service/workspaces/   # AgentScope 按 Session 隔离的目录
  opencode/                   # 纳管 OpenCode 的配置及实际状态路径
  redis/                      # 纳管 Redis 的持久数据
  logs/
  run/
  backups/
~~~

代码目录与上述目录分离。代码升级、切换发行版、重建容器不能删除或替换此目录。不能把 SQLite 放在临时目录或共享网络文件系统；多主机共享写库不属于此 SQLite 默认方案。

Linux 用户态默认目录建议为 $HOME/.local/share/nexus；macOS 为 $HOME/Library/Application Support/Nexus。系统部署可用 /var/lib/nexus。所有子进程均由 CLI 注入相同的绝对路径，不依赖启动时 cwd。

任务库继续复用现有 WAL/busy_timeout。其他 SQLite 库也需要验证实际并发读写和锁等待；不要仅因改成文件数据库就认为不会发生锁竞争。

OpenCode 的状态位置必须通过所钉住版本的实际路径发现能力核实，并写入备份清单。不得只新建 state/opencode 目录就声称会话数据已经迁入其中；保留用户工程的项目配置和非纳管的全局配置。

## 6. 平台设置的数据模型

在 task platform 现有 SQLAlchemy 数据库新增 platform_settings。只允许明确的 section，不做任意键值配置中心。

| 字段 | 约束与用途 |
|---|---|
| section | 主键，首期仅 library、nr_source |
| values | JSON，只放经过对应 Pydantic schema 校验的非敏感字段 |
| secrets_encrypted | 可空密文；保存该 section 的敏感字段对象 |
| revision | 非负整数；系统初始默认记录为 0；用户保存或成功导入后递增，用于并发更新检查 |
| updated_at | UTC 时间 |
| updated_by | 已认证的管理身份，不能直接相信浏览器自报角色 |

初始化为 library.enabled=false、nr_source.enabled=false，revision=0。其余字段采用当前代码有效默认值。即使用户显式保存的值恰好等于默认值，也要把 revision 从 0 推进到 1，表示已经由用户配置，防止后续导入覆盖。启用时缺少必要字段返回 422；停用不删除已保存 Token。

复用现有 CredentialCipher 加密新增 wiki/NR Token。API 只返回 token_configured 等布尔值，不返回明文或密文。请求中密钥缺省表示保持；replace 和 clear 必须显式表达。空字符串不能意外清掉已有密钥。

不新增网关、Worker、分类的镜像记录。当前 Worker SSH 密码字段并没有统一加密，本轮不能因此宣称“全部历史凭据已加密”；若处理这部分，使用单独的一次性回填及无双读迁移，不将其混入设置表。

## 7. 在线配置接口

新接口放在 task platform，经现有 /api 入口访问。网关、分类和 Worker 继续使用原 API。

| 接口 | 行为 |
|---|---|
| GET /api/setup/status | 返回配置完整性和能力就绪状态；不包含秘密，不以模型未配置阻断页面启动 |
| GET /api/settings | 返回允许在线修改的 section 列表、脱敏值和 revision |
| GET /api/settings/{section} | 获取该 section 的脱敏配置与字段约束 |
| PUT /api/settings/{section} | 原子校验、保存；要求 If-Match revision；冲突返回 409 |
| POST /api/settings/{section}/test | 测试当前草稿，不保存，不派任务，不写知识库 |
| GET /api/settings/export | 导出带格式版本的脱敏诊断配置；明确这不是完整恢复备份 |

保存示例，以下只是协议示例，不包含真实秘密：

~~~http
PUT /api/settings/library
If-Match: "3"
Content-Type: application/json

{
  "values": {
    "enabled": true,
    "api_base_url": "http://127.0.0.1:19828/api/v1",
    "project_id": "<project-id>",
    "timeout_seconds": 30
  },
  "secrets": {
    "api_token": {"action": "keep"}
  }
}
~~~

replace 时额外提交 value；clear 不带 value。所有错误、日志、请求跟踪、审计记录必须脱敏。

保存成功表示数据库事务已提交。新 revision 对后续新操作生效；外部服务健康与配置保存是两个独立结果。服务暂时不可达时，仍可保存结构合法的配置，界面显示“已保存，连接失败”，不得把连通失败当成已健康。

测试连接只做有超时的只读检查：知识库检查健康、认证与项目匹配；NR 检查既有健康接口。不得把测试按钮实现成发现任务、触发 ingest 或实际执行任务。

管理员权限必须真实校验。当前代码中的默认 admin 或客户端传入的 X-Astra-Role 不是独立的身份认证。首期可使用 init 生成的单管理员管理密钥，并以受限会话访问新设置接口；不新增完整账号、SSO、RBAC 平台。管理密钥通过本机 CLI 获取，不置于前端包、URL 查询参数或 localStorage。浏览器会话使用 HttpOnly、SameSite Cookie，写请求校验来源；对外部署沿用受信入口和 HTTPS。已有管理接口的鉴权缺口单独列明，不能宣称本轮已完成全站权限改造。

## 8. 配置生效的具体实现原则

新增小型 PlatformConfigService，负责 section 的类型校验、数据库读取、密文处理和 revision 更新；不新增配置微服务、消息中心或另一套任务状态机。

首期两个 section 直接在新请求或周期作业入口从数据库读取一致快照，避免另建跨进程配置广播。数据库提交后才返回成功；未保存的表单草稿不能改变运行值。

必须覆盖的消费路径：

- KnowledgeService 的 HTTP、Library MCP、助手调用、写入和同步路径统一获取 library 配置；不能只改页面对应的 HTTP 路由。
- LlmWikiClient 接收已解析配置，不再自己回读旧环境变量。
- Celery scout 每轮读取 nr_source 配置。当前 beat_schedule 会在启动时根据 Token 判断是否登记 scout；需改为始终登记周期，关闭时任务快速返回，确保网页启用后无需重启 beat。
- NR 结果回写、健康检查和任务发现使用同一个配置来源。正在进行的一次操作保留开始时快照，下一次才使用新 revision。enabled 只控制新任务发现，关闭不取消已有任务，也不停止其必要的结果回写。存在未结束的 NR 来源任务时，禁止切换 endpoint/结果路径或清除回写凭据，返回 409；使用现有任务终态规则，不新增一份状态判断。
- 网关继续使用原 ConfigLoader/ConfigRefresher，页面说明现有刷新延迟；不改为每个代理请求查库。
- Worker 继续保留活跃任务、部署互斥和归属检查；不能在执行中直接替换工程目录或终止无法确认归属的进程。

get_settings 只承载仍需启动时确定的参数。禁止用 clear lru_cache、修改 .env 或修改一个进程的 os.environ 冒充所有服务都已热更新。

数据库不可读时不能悄悄回退到旧 .env。新配置操作返回明确错误，正在进行的操作可完成其已有快照。配置页不得展示“全部已生效”而没有对应消费者证据。

## 9. AgentScope 的数据库持久化

为了让关键模型配置和凭据也具备独立数据库备份能力，**新安装默认采用官方 AsyncSQLAlchemyStorage + SQLite，消息总线仍用 RedisMessageBus**。

实施要求：

- 固定 agentscope==2.0.10；在 Agent Service 独立环境增加 storage-sql 与 aiosqlite，保留 service、storage-redis 及现用模型 extra。
- Agent、Session、credential、Skill、Knowledge 等全部继续由官方 Storage 管理；不复制到 platform_settings 或 Nexus SQLite。
- AgentScope 数据库与 task platform Alembic 分别迁移。使用官方 SQL Storage 的迁移能力，以独占的一次性部署步骤执行；普通服务启动不让多个进程同时自动迁移。
- LocalWorkspaceManager 继续使用 IsolationPolicy.PER_SESSION，已有工作目录一起持久保存。
- 启动配置显式声明 agentscope_storage_backend=sqlite 或 redis。每次启动只选一个，不双写、不在读取失败后自动切到另一后端。

已有 RedisStorage 的冷迁移必须单独闭环：

1. 停止 Agent Service 的新输入和推理，确认无活跃运行；备份 Redis 及工作目录。
2. 通过官方存储接口导出并导入，保留所有记录 ID、关联、凭据与会话内容。不能只迁“聊天历史”或重新创建 credential。
3. 覆盖源库实际存在的所有记录类型；遇到不能导出的类型必须阻止切换，保持原后端。
4. 校验记录数量、关键关联、Session 内容和 Nexus 绑定；MessageBus 仍留在原 Redis，不清队列、不清共享 Redis 数据。
5. 完成校验后切换 bootstrap 中的后端。源数据在验收前保留，禁止边运行边双写。
6. 回滚仅能在尚未接受新写入时直接切回；接受新写入后必须先完成反向迁移，不能静默丢掉 SQLite 中的新数据。

不允许安装器见到 SQLite 空库就把已有 Redis 员工和会话当成不存在。旧部署未完成迁移时可继续使用 redis 后端，不阻塞平台配置页面交付。

## 10. CLI、发布包与启动配置

提供统一 nexus 命令，复用 procman.py、service_ctl.py、astra_service.py 中成熟能力。职责明确如下：

| 命令 | 职责 |
|---|---|
| nexus init | 幂等创建目录、bootstrap、内部密钥、数据库默认记录；不询问业务模型 Key |
| nexus start / stop / status / logs | 管理原有服务，输出真实就绪结果和各能力状态；重复 start 不产生第二套进程 |
| nexus doctor | 按基础平台、wiki、远程 Worker 分组检查依赖，关闭的增强功能不阻断基础启动 |
| nexus migrate | 备份检查、确认 Alembic head、执行各所属数据库迁移；不自行拉取代码 |
| nexus import-legacy --env-file <path> | 一次性预览和导入旧业务变量，不执行文件中的 shell 代码 |
| nexus model bootstrap | 调用现有官方 credential 登记逻辑；模型 Key 由受保护输入或环境传入，避免命令行参数泄露 |
| nexus backup / restore | 按下一节规则执行离线备份和恢复 |

init 至少生成并复用：ASTRA_GATEWAY_MASTER_KEY、管理密钥、ASTRA_MCP_API_KEY、Task MCP 共享密钥。启动时保证 ASTRA_TASK_MCP_KEY 与 NEXUS_TASK_MCP_KEY 是同一值。内部秘密用原子文件写入及权限控制，不被安装器或普通 start 重置。

nexus model bootstrap 成功后，原子保存不含 Key 的 credential_id/type/model/parameters 绑定引用到 bootstrap，启动 Nexus API 时映射为现有 NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG，避免只在终端打印 JSON 而下次启动丢失。模型 Key 只提交到 AgentScope。相同输入重复执行复用现有 credential；明确更换 Key 时调用官方 PATCH /credential/{credential_id}，不能因 endpoint 相同就跳过更新。更新默认绑定只影响后续新建 Session；现有 Session 不重建、不批量改绑。需要重新读取启动引用时只重启 Nexus API，不重启正在执行任务的 Runtime。

路径、数据库定位和内部服务地址统一从 bootstrap 注入；不要让几个脚本各自从不同 cwd 重新推导一套默认路径。多份 .env 的优先级先做一次性解析和冲突报告，再收敛为明确来源。

发布采用保留现有目录布局的 release tarball，首期不打一个路径尚未改造的 wheel。发行包包括 services、ops、内置 Task Skill、必要 configs、Alembic、AGENTS/opencode.json/.opencode 能力文件、web/nexus/dist 及被后端检查的对应 Workspace UI 资源。

前端构建在 CI 完成。默认浏览器入口由 Task API 的根路径提供静态页面；/api 保持原接口，/api/v1 转发 Nexus API，/v1 转发原 Gateway。SSE 必须流式透传，并在断连时释放上游连接。SPA fallback 只处理页面导航，缺失的 API、脚本和 Workspace UI 文件返回原有错误或 404，不能返回 index.html。

这是入口路由汇合，不是在 Task API 内导入 Nexus API 或 AgentScope。开发环境仍使用 Vite 5174；既有 nginx 静态托管与代理方式保留。CLI 打印生产入口，不把 Vite 端口伪装为必须的生产依赖。

Python 保持两个隔离环境：core 放 task platform 与 Nexus API 的依赖，agentscope 单独。Nexus API 的 python-multipart 等依赖必须显式包含，不能依赖偶然的传递安装。若合并环境对当前部署有风险，可先保留三个，不把减少一个 venv 当成交付前置。

可用 uv 生成锁定依赖，但不能把存在冲突的两个服务放入普通共享 workspace 并认为一次 uv sync 能自动得到两个独立环境。运行机不强制安装 uv。发布依赖应锁定可复现，开发依赖不进入默认生产安装。

Linux 默认继续使用多个 systemd 单元；CLI 统一操作它们，不先重写一个总管。无 systemd 的前台运行需要真实监督子进程、处理 SIGTERM 和失败退出，不能仅启动后台进程就声称服务受监督。macOS 继续复用原生进程管理方式，并单独验证。

## 11. 旧配置导入与防覆盖

旧 .env 只在显式 import-legacy 时作为迁移来源；之后动态配置由数据库负责，不能每次启动再覆盖。

导入必须支持 dry-run，输出字段名、来源、目标 section 和冲突数量，不输出秘密。仅填充尚未初始化的 section，即 revision=0 的系统默认记录；revision>0 的配置跳过并报告，覆盖需明确操作。旧 NR Token 非空时导入为 enabled=true，空时为 false，保留旧行为。

旧模型 Key 只通过官方 credential 登记路径处理，不导入平台设置表。已有 NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG 仍支持作为部署期引用；平台设置不能建立另一份模型凭据。

发现已有数据库、Redis 部署或历史 .gwmaster/.mcpkey 等持久秘密时，先导入现有定位与密钥。已有密文数据库却没有可恢复主密钥时，明确报告无法解密；禁止自动生成新主密钥后继续启动成“正常”。

导入成功要有持久版本标记；同一输入再次执行不得产生重复 credential、Key、Worker 或设置 revision。导入器是一次性工具，日常业务代码不保留“新字段为空就猜旧字段”的兼容路径。

## 12. 备份、恢复与外部依赖

首期提供离线备份和恢复，避免在管理页面里停止自身服务或新建后台任务系统。备份前确认纳管写进程已停、没有活跃任务继续改工作区；不自动取消任务、不杀未知进程。

SQLite 使用数据库备份能力或经验证的一致快照，不能运行中只复制 .db 而遗漏 WAL。备份清单包含数据库、secrets、bootstrap、附件、AgentScope 工作目录、纳管 OpenCode 状态与纳管 Redis 持久数据，并记录格式版本、代码版本、数据库 revision、文件校验值和排除项。

完整备份是敏感文件，采用受保护路径与权限。诊断配置导出始终脱敏，不能作为完整恢复备份。恢复先校验版本和主密钥，再在停服务状态执行；普通启动或升级不自动覆盖现有数据。远端 Worker 工程、外置 wiki 数据和外置 Redis 明确列为另行备份范围，不能把缺这些数据的归档标为全系统备份。

依赖必须按功能分组，不得重新许诺“无外部依赖”：

| 场景 | 依赖要求 |
|---|---|
| 基础平台 | 支持的 Python、Git、OpenCode、Redis、SQLite 驱动、可写持久目录；发布包已含前端 |
| AgentScope SQL 存储 | 官方 storage-sql、aiosqlite、独立迁移及工作目录 |
| llm_wiki 完整入库 | 对应 Web Edition 包、现用 Node/容器运行环境、Chrome、websocket-client、浏览器 profile、必要隧道 |
| 远程 Worker | SSH、SFTP、正反向 TCP 转发；远端 Git、Node/npm/OpenCode 及下载源 |
| 第一次安装与插件加载 | PyPI、npm、GitHub/发行源、模型服务；公司环境的代理和 CA；离线部署需预置完整依赖 |

llm_wiki pump 当前绑定特定带 hash 的前端资源名，必须随 wiki 发行包校验；不能只检查 HTTP 健康就报告自动入库可用。数据库保存配置也不会自动安装这些外部服务。

OpenCode 可改用固定版本官方二进制以减少主平台系统 Node 依赖；但其插件安装、远程安装器和用户工程仍可能依赖 npm/Node。不能承诺所有功能都不需要 Node。

## 13. 验收场景

以下应转换为实施 change 的 Scenario 和唯一 tasks.md，不能只勾选页面已完成。

| 编号 | 场景 | 必须有的证据 |
|---|---|---|
| A01 | 新机器不安装 MySQL，也未提供模型 Key/wiki/远程 Worker 配置 | 基础平台启动、真实浏览器可访问设置页；待配置能力如实展示 |
| A02 | init/start 重复执行 | 数据目录、主密钥、内部 Key 和设置不变；无重复进程 |
| A03 | 保存 wiki 设置后刷新页面、重启全部服务 | 值和密钥配置状态保留；新请求确实使用新地址/Token/超时 |
| A04 | 浏览器清缓存或换浏览器 | 平台配置仍从数据库读取，不依赖 localStorage |
| A05 | 两个管理页面同时修改同一 section | 旧 revision 保存返回 409，不覆盖新修改 |
| A06 | 校验失败或数据库提交失败 | 数据库和后续有效配置保持原值；页面不提示成功 |
| A07 | 测试未保存草稿 | 只读测试，不修改配置、不发现任务、不写知识库 |
| A08 | 外部服务暂时不可达 | 合法配置可持久保存；保存状态与连通状态分别显示 |
| A09 | 关闭后在线启用 NR 任务源 | 无需重启 beat；下个周期读取数据库新值；关闭后不再拉取但保留已有任务回写；有未结束任务时切换来源/清除回写凭据被拒绝 |
| A10 | HTTP、Library MCP、助手分别读取知识库 | 三条真实消费路径使用同一已保存配置，不残留旧环境值 |
| A11 | 保持、替换、清除 Token | 行为明确；响应和日志无秘密；关闭不自动删除 Token |
| A12 | 缺失或错误的管理身份 | 新配置读写/测试接口拒绝访问；不能伪造角色头绕过 |
| A13 | Task MCP 完整链路 | init 后双方 Key 一致；真实员工 Session 的工具调用通过，而非只测 /health |
| A14 | Worker 有活跃任务时修改敏感环境字段 | 沿用现有拒绝/互斥规则，任务与 Session 不被替换 |
| A15 | 网关配置修改及 Key 吊销 | 沿用 Redis 原子状态和原有生效语义，没有第二套计数 |
| A16 | 旧 .env 导入两次，且数据库已有新设置 | 新设置和 credential 不被覆盖或重复创建；输出脱敏 |
| A17 | 新安装 AgentScope SQL + RedisMessageBus | Agent/Session/credential 重启后仍可读；PER_SESSION 隔离正常；私聊和同事唤醒正常 |
| A18 | 有历史数据的 RedisStorage 冷迁移 | ID、数量、关联、会话和绑定校验通过；失败不切换后端、不清原 Redis |
| A19 | 发布机没有系统 Node/npm | 在支持的基础功能范围内启动预构建 UI；SSE、SPA、API 404 和精确版本 Workspace UI 正常 |
| A20 | 升级与恢复备份 | 持久目录保留；配置可解密；历史会话/附件可用；缺主密钥明确失败 |

先跑针对性测试，再跑受影响服务的完整检查。至少包括：

~~~bash
# 替换为实际检出的仓库绝对路径
nexus_repo=/path/to/nexus-checkout
cd "$nexus_repo/services/task-platform"
.venv/bin/alembic heads
.venv/bin/pytest tests/<本次相关测试>
.venv/bin/ruff check app tests

cd "$nexus_repo"
PYTHONPATH=services/nexus-api services/nexus-api/.venv/bin/pytest services/nexus-api/tests

cd "$nexus_repo/web/nexus"
npm run type-check
npm run build
~~~

同时执行实际配置保存、重启保留、Task MCP 调用、AgentScope 消息唤醒和发布入口的浏览器/E2E 验证。真实 Redis、旧部署迁移、MySQL 兼容、远程 Worker 或 wiki 入库没有测试条件时，逐项写明未验证；不能用 mock 或一次健康检查代替。

## 14. 开发组织与交付要求

先创建一个 Nexus OpenSpec change，例如 nexus-persistent-settings-and-bootstrap。根据本方案明确 capability 和 Scenario；以该 change 的 tasks.md 为唯一实施清单。本文件不另建平行任务清单，也不要求把每个工具调用拆成任务。

建议按以下依赖顺序交付：

| 阶段 | 可验收产物 |
|---|---|
| 契约与现状核实 | 确认目录冲突、数据归属、模型页面边界、管理员权限和迁移 head |
| 持久化与 API | 设置表、密文处理、revision、测试/保存 API、真实消费者读取、一次性导入 |
| 页面与快速设置 | 复用现有设置入口；知识库/NR 配置；网关/Worker/分类跳转复用 |
| 初始化与发布 | 幂等 init、统一 CLI、独立持久目录、预构建静态入口、Task MCP 密钥闭环 |
| AgentScope 存储与恢复 | 新安装官方 SQLite、旧 Redis 冷迁移、备份恢复与真实重启验证 |
| 验收 | Scenario 对照证据、构建测试、真实页面、opsx-verify；必要时 sync/archive |

建议改动落点如下，文件名可以结合真实结构小幅调整，不新建平行业务树：

- services/task-platform/app/models.py 与 alembic/versions：平台设置表及迁移。
- services/task-platform/app/settings_service.py、api/settings_routes.py：类型化配置、事务、密文和接口；在 app/main.py 挂接。
- app/llm_wiki.py、library_service.py、sources/nr_test_system.py、runtime/celery_app.py：统一真实消费路径。
- services/task-platform/app/cli.py 与 pyproject.toml console script：统一入口；复用 ops/dev 的现有进程管理实现。
- app/web_entry.py 与 app/main.py：预构建静态页面、必要代理与流式连接清理。
- services/agent-service/agent_service.py、requirements.txt：官方 SQL 存储装配与显式 aiosqlite 依赖；不修改安装包源码。
- services/nexus-api：只调整持久目录和部署期绑定引用的加载，继续通过 HTTP 调用 AgentScope。
- web/nexus/src/views/SettingsView.vue 及 views/settings：统一设置/快速设置入口；复用现有网关、Worker、分类页面，不复制表单。
- ops/deploy、发布流水线、相关 tests/E2E：安装兼容、发布清单、依赖分组与真实验证。

最终只报告问题原因、修改内容、验证结果和剩余风险。提交前检查 git diff，无调试代码、无无关生成文件、无秘密、无重复事实源和临时双读 fallback。

### 可直接发给 Grok 的开发指令

请按本文件实现 Nexus 的部署简化与在线配置。默认使用 SQLite，不要求 MySQL；平台动态配置入现有数据库，复用网关、Worker 和分类，不复制 AgentScope 数据。保留 Redis/Celery 与当前 Runtime 链路，完成幂等初始化、真实消费路径生效、一次性旧配置导入、预构建前端启动以及备份恢复。聊天模型网页编辑不在首期范围，保持当前契约；模型登记仍调用 AgentScope 官方接口。先核对真实分支和数据关系，再用一个 OpenSpec change 的 tasks.md 实施，按上表 Scenario 提供实际测试与浏览器证据。不要只做前端表单、修改 .env 或健康检查，不要把尚未验证的迁移和恢复宣称完成。

## 15. 本交接文档的验证范围

已完成：读取原部署简化材料、核对 c046abe 真实代码、检查 AgentScope 2.0.10 安装元数据与官方存储实现、读取 llm_wiki 的配置保存实现，并运行 task platform 的 alembic heads。

未完成：上述功能编码、数据库迁移执行、干净机器安装、真实冷迁移、备份恢复、在线配置和相关浏览器验收。本文不得被当成这些功能已经完成的证据。
