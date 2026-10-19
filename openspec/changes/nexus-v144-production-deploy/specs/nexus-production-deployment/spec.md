## ADDED Requirements

### Requirement: 部署版本锁定

生产安装与升级 SHALL 以完整 Git commit 为唯一版本输入，并在修改现网前校验仓库地址、工作树状态和 HEAD。仓库、commit 或 Git 更新不符时，部署 MUST 以非 0 退出且不改变当前发布。

#### Scenario: 指定 commit 部署成功

- **WHEN** 操作者提供匹配的仓库地址与存在的完整 commit，且工作树可安全获取该提交
- **THEN** 部署只在版本化目录中构建该 commit，发布记录写入完整 commit

#### Scenario: commit 不存在或仓库不匹配

- **WHEN** 指定 commit 不存在、短于完整哈希，或实际远程与配置仓库不一致
- **THEN** 部署在准备阶段以非 0 退出，`current` 指针和已运行服务保持不变

#### Scenario: Git 获取或工作树异常

- **WHEN** fetch/checkout 失败、工作树含未提交修改，或 HEAD 与目标 commit 不一致
- **THEN** 部署立即停止并报告原因，不继续安装、迁移或切换

### Requirement: 配置与密钥保护

部署 SHALL 将用户维护配置、密钥和生成运行配置分离存放。重复安装或升级 MUST 保留用户自定义配置和已有密钥，生成配置以原子方式替换并只展示非敏感差异或摘要。密钥文件权限 MUST 为 0600，秘密值 MUST NOT 出现在日志或命令行参数中。

#### Scenario: 重复升级保留配置与密钥

- **WHEN** 用户修改了非敏感配置并已有生成密钥，随后升级到新 commit
- **THEN** 修改后的配置和原密钥保持不变，生成配置按新版本重建

#### Scenario: 首次生成共享密钥

- **WHEN** 部署首次运行且 Task MCP 与 Agent Service/Nexus API 需要同一调用密钥
- **THEN** 只生成一次并写入密钥文件，两个消费者引用同一个值

#### Scenario: 不修改全局 OpenCode 配置

- **WHEN** 部署配置 OpenCode provider 与模型
- **THEN** 用户 `$HOME/.config/opencode/opencode.json` 内容哈希保持不变，Nexus 使用独立配置目录和显式 provider/model/base URL

### Requirement: 统一服务托管

生产常驻进程 SHALL 由 systemd 以每进程一个 unit 独立管理。安装、升级、重启、状态查询与健康检查 MUST 使用同一服务定义；systemd 不得通过 oneshot unit 托管其他常驻进程，生产入口 MUST NOT 在启动时执行数据库迁移或前端构建。

#### Scenario: 查询与重启单个服务

- **WHEN** 操作者查询或重启 Task API、Celery Beat、Celery Worker、Task MCP、Library MCP、Gateway、Agent Service 或 Nexus API
- **THEN** 只操作该服务对应的 unit，状态来自 systemd 与业务 readiness，不来自端口猜测

#### Scenario: 生产入口不重复托管

- **WHEN** 以生产模式调用统一服务入口
- **THEN** 入口不启动或停止 systemd 已管理的进程，不执行迁移和构建，并明确指向生产部署命令

#### Scenario: 服务定义一致

- **WHEN** 安装、升级、重启、状态查询或健康检查执行
- **THEN** 使用同一组服务名、unit 名、工作目录、运行账号和环境文件

### Requirement: 数据迁移独占与可恢复

同一部署 SHALL 在停止受影响服务后只执行一次 Alembic 迁移。部署全程 MUST 持有独占锁，并发部署 MUST 被拒绝。迁移失败 MUST NOT 写入成功记录，且 MUST 按已声明的迁移兼容策略阻断或恢复。

#### Scenario: 并发部署被拒绝

- **WHEN** 另一个部署进程已持有部署锁
- **THEN** 新部署以非 0 退出且不修改代码、配置、数据库或服务

#### Scenario: 迁移失败不写成功

- **WHEN** Alembic 迁移返回非 0
- **THEN** 部署写入失败记录，保留上一成功记录，不创建本次成功标记

#### Scenario: 数据库 revision 变化时回退

- **WHEN** 新版本迁移了数据库且启动或 readiness 失败
- **THEN** 若迁移未声明向后兼容，部署阻断自动回退并输出人工数据库恢复步骤；若声明兼容，才恢复上一发布

### Requirement: 发布目录与原子切换

部署 SHALL 在版本化发布目录中准备依赖与构建产物，通过一次性原子替换 `current` 指针切换版本。构建失败 MUST 不影响当前运行版本；切换或启动失败 MUST 恢复上一可用发布的服务。

#### Scenario: 构建失败保持现网

- **WHEN** 依赖安装、Web 构建或版本清单校验失败
- **THEN** 现网 `current` 与运行服务保持不变，失败发布目录被清理或标记不可用

#### Scenario: 启动失败恢复上一发布

- **WHEN** 切换后任一必需服务无法启动或 readiness 失败，且数据库兼容策略允许回退
- **THEN** `current` 指针恢复到上一可用 commit，上一发布服务重新启动并通过 readiness，发布记录包含回退结果

### Requirement: 健康检查与成功判定

部署 SHALL 仅在全部必需检查通过后原子写入成功记录。检查 MUST 覆盖进程归属、业务 HTTP/API readiness、Redis ping、SQL `SELECT 1`、Celery 指定 hostname 的 ping 与队列、启用模块的 MCP 认证无副作用探测、Nginx 配置与入口路由。监听端口存在、PID 存在或日志包含 ready 字样 MUST NOT 单独作为就绪依据。可选模型计费探测 MUST 明确标记为 skipped。

#### Scenario: 有效业务响应

- **WHEN** 必需 HTTP 服务返回成功状态但响应结构无效
- **THEN** 该服务判定为未就绪，部署失败

#### Scenario: Celery 无消费者

- **WHEN** Redis 可达但指定 hostname 的 Celery worker 不响应 ping 或队列未消费
- **THEN** 部署判定失败且不写成功记录

#### Scenario: 入口路由不可用

- **WHEN** Nginx 配置校验失败，或首页与任一启用 API 路由返回不符合预期的状态或内容
- **THEN** 部署判定失败，不把任意 401/403 或无内容首页视为可用

#### Scenario: 可选检查跳过

- **WHEN** 需要计费的模型探测未启用
- **THEN** 记录中标记 skipped，且不作为失败或成功依据

### Requirement: 端口与进程归属校验

预检 SHALL 对每个需要监听的端口验证实际监听者属于对应服务 unit 的 MainPID 或 cgroup。已有成功安装记录或 PID 存在 MUST NOT 被视为端口归属证据。

#### Scenario: 端口被未知进程占用

- **WHEN** 所需端口由非 Nexus 对应 unit 的进程监听
- **THEN** 预检失败并指出端口与监听者，不停止该进程

#### Scenario: 端口由对应 unit 监听

- **WHEN** 监听进程的 PID 与对应 unit 的 MainPID 或 cgroup 一致
- **THEN** 该端口视为 Nexus 自身服务占用，可继续升级

### Requirement: 备份与数据库恢复策略

升级前 SHALL 备份数据库与配置并记录当前版本、配置摘要、数据库 revision。SQLite 备份 MUST 使用备份 API 并通过 `integrity_check` 验证；MySQL 备份 MUST 使用显式工具并验证恢复条件。部署 MUST NOT 声称未经验证的自动无损降级能力。

#### Scenario: SQLite 备份验证

- **WHEN** 使用 SQLite 的部署进入备份阶段
- **THEN** 生成一致备份副本并通过 `PRAGMA integrity_check`，失败则终止升级

#### Scenario: MySQL 工具缺失

- **WHEN** 使用 MySQL 但所需备份或恢复工具不可用
- **THEN** 部署明确失败并给出所需工具与人工恢复要求，不伪装备份成功

### Requirement: 发布记录与状态查询

每次部署 SHALL 留下可追溯记录，成功记录包含 commit、时间、配置摘要、数据库 revision 与检查结果；失败记录包含失败阶段与原因。失败部署 MUST NOT 残留本次成功标记，且 MUST 保留上次成功记录。状态查询 SHALL 暴露当前发布、服务状态与最近部署结果。

#### Scenario: 失败记录

- **WHEN** 部署在预检、准备、迁移、启动或验证阶段失败
- **THEN** 写入带阶段的失败记录，成功标记保持上一版本，不出现本次成功记录

#### Scenario: 状态查询

- **WHEN** 操作者查询部署状态
- **THEN** 输出当前 commit、上一成功 commit、各必需服务状态与最近部署结果

### Requirement: 可选模块隔离

核心部署 SHALL 不安装或检查未启用的 LLM Wiki 与远端 Worker 依赖。每个可选模块 MUST 有独立入口、依赖检查、失败报告与重试方式，且不得引入 Kubernetes 等新的平台依赖。

#### Scenario: 未启用可选模块

- **WHEN** 未启用 LLM Wiki 或远端 Worker
- **THEN** 核心部署不检查其专属依赖，也不把其缺失计为失败

#### Scenario: 可选模块失败隔离

- **WHEN** 已启用某个可选模块但该模块预检或启动失败
- **THEN** 该模块报告独立失败，不影响核心部署已完成部分的记录

### Requirement: 可重复执行与文档一致

相同版本重复安装 SHALL 不产生重复服务、重复模型/网关路由或覆盖已有密钥。README、环境模板、Nginx、systemd 与安装命令 MUST 使用一致的默认端口与 Nexus 命名。

#### Scenario: 同版本重复安装

- **WHEN** 对同一 commit 再次执行安装
- **THEN** 不新增重复 unit、路由或密钥，部署成功记录可更新而不破坏现网

#### Scenario: 文档与默认值一致

- **WHEN** 操作者按 README、环境模板和安装命令部署
- **THEN** 文档中的默认端口、路径、服务名与实际脚本和 unit 一致，对外名称为 Nexus
