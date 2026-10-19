## ADDED Requirements

### Requirement: 维护操作独立持久化
系统 SHALL 将 Agent Worker 的部署、重启与检查作为独立维护操作持久化，包含操作 ID、环境 ID、动作、状态、阶段、请求人、attempt、租约、目标版本、前后快照、错误与取消标记。执行 MUST NOT 依赖 HTTP 连接生命周期。

#### Scenario: 浏览器断开后操作继续
- **WHEN** 客户端提交维护操作后关闭页面、刷新或断开事件流
- **THEN** 服务端继续执行该操作，操作状态和阶段可再次查询

#### Scenario: 刷新后恢复进度
- **WHEN** 客户端重新查询或订阅已有操作
- **THEN** 系统返回已持久化的阶段、步骤事件和最终结果，而不是重新提交执行

#### Scenario: 取消必须显式请求
- **WHEN** 客户端只断开事件流而没有调用取消接口
- **THEN** 操作不被取消

### Requirement: 维护状态机与租约恢复
系统 SHALL 使用 `QUEUED → PREFLIGHT → RUNNING → VERIFYING → SUCCEEDED` 的成功路径，失败进入 `FAILED`，显式取消进入 `CANCELED`，执行进程失联或租约过期进入 `INTERRUPTED`。执行器 MUST 通过短事务原子领取操作、续租并携带 attempt/fencing 写入，陈旧执行者 MUST NOT 覆盖新结果。

#### Scenario: 两个执行者竞争同一操作
- **WHEN** 多个 API 进程同时尝试领取同一 `QUEUED` 操作
- **THEN** 只有一个执行者取得租约并运行，其余执行者不执行该操作

#### Scenario: 陈旧执行者写回
- **WHEN** 操作已被另一个 attempt 或执行者接手后，旧执行者返回
- **THEN** 旧执行者的状态、阶段和结果写入被拒绝

#### Scenario: 执行进程退出
- **WHEN** 执行进程正常退出或租约到期
- **THEN** 自有未完成操作被持久化为 `INTERRUPTED`，并保留远端可能已变更的标记，供人工核对或显式重试

#### Scenario: 多进程不抢占有效租约
- **WHEN** 另一个 API 进程仍持有未过期租约
- **THEN** 恢复器不把该操作标记为中断

### Requirement: 任务与原生执行保护
系统 SHALL 在维护提交时检查任务占用、待人工确认与未结束 Session；SHALL 在锁定后、每次写入配置/更换装备/重启前使用同一 canonical 占用判定再次检查，并在原生 Session 检查后做数据库 fenced 复检。任一保护成立时 MUST 拒绝危险维护，且不得清理 Task 或 Session 占用来放行维护。

#### Scenario: 执行中或等待人工确认
- **WHEN** 员工存在占用 Task、`WAITING` Task、pending 提交或 RUNNING/WAITING/UNKNOWN Session
- **THEN** 提交或危险阶段被拒绝，并返回阻塞原因

#### Scenario: 原生 Session 忙碌
- **WHEN** 数据库没有占用但 OpenCode 原生 Session 处于 busy 或 retry
- **THEN** 危险维护被拒绝，不重启或改写运行配置

#### Scenario: 只读检查
- **WHEN** 环境存在占用任务但请求只读检查
- **THEN** 系统允许读取状态，不执行写配置、安装或重启

#### Scenario: 维护与任务启动并发
- **WHEN** Task 启动与维护提交同时发生
- **THEN** 二者通过共享原子环境锁串行化，只能有一方进入危险阶段

### Requirement: 状态分离与可调度性
系统 SHALL 分别给出维护状态、Worker 连接状态、运行时健康状态与 `schedulable`。维护失败 MUST NOT 无条件把此前健康的环境标记为 `UNAVAILABLE`；真实探测失败或无法确认 MUST NOT 允许调度。

#### Scenario: 维护失败但旧环境健康
- **WHEN** 维护在未改变远端的阶段失败，旧 Worker 与 OpenCode 仍健康
- **THEN** 环境保持可用，维护记录为 `FAILED`

#### Scenario: 远端结果未知
- **WHEN** 危险步骤可能已执行但无法确认结果
- **THEN** 环境不进入 `AVAILABLE`，`schedulable` 为 false，并标记需要核对

#### Scenario: 连接与运行时健康分开
- **WHEN** Worker Gateway 在线但 OpenCode 健康探测失败
- **THEN** 连接状态仍为在线，运行时健康为异常，调度被拒绝

### Requirement: 显式取消、重试与远端未知事实
系统 SHALL 区分显式取消、执行进程崩溃与危险步骤已发出的半完成状态。取消 MUST 通过接口显式请求；危险步骤已发出时 MUST 核实或进入 `INTERRUPTED`，MUST NOT 直接恢复调度。重试 MUST 创建新 attempt 并重新预检，MUST NOT 盲目轮换 token 或重复重启。

#### Scenario: 危险步骤前取消
- **WHEN** 操作仍在排队或尚未发出危险远端命令时被取消
- **THEN** 操作进入 `CANCELED`，释放维护锁

#### Scenario: 危险步骤中取消
- **WHEN** 安装、写配置或重启命令已发出后收到取消
- **THEN** 操作进入 `INTERRUPTED` 或等价待核对状态，维护锁保持，不直接恢复调度

#### Scenario: 崩溃后重试
- **WHEN** 操作处于 `INTERRUPTED` 且用户显式重试
- **THEN** 系统以新 attempt 从预检开始，复用远端有效配置和 Worker 凭据，不盲目重新 enroll 或重复重启

### Requirement: 维护事件订阅
系统 SHALL 为每个操作持久化脱敏步骤事件，并支持按事件 ID 分页与 SSE 续传；`Last-Event-ID` 或等价游标之前的事件 MUST NOT 重复发送。

#### Scenario: SSE 断线续传
- **WHEN** 客户端携带最后的 `Last-Event-ID` 重新订阅
- **THEN** 系统只发送该 ID 之后的事件，并继续跟随新事件

#### Scenario: 敏感信息
- **WHEN** 事件包含 SSH 密码、token、Key 或 Authorization
- **THEN** 持久化和 API 返回的内容必须脱敏

### Requirement: 维护 API 与 Worker 环境标识
系统 SHALL 以 Worker 环境 ID 提供维护提交、历史、详情、事件、取消、重试与健康接口。旧员工 ID 的部署/重启与 `/environment/test` HTTP 旁路 MUST NOT 保留。旧的 `/api/workers` 只读别名与 `/workers` 前端重定向 MUST NOT 被删除。

#### Scenario: 新接口提交与查询
- **WHEN** 调用者向环境 ID 提交维护
- **THEN** 返回操作 ID，并可通过操作详情、历史与事件接口查看状态

#### Scenario: 旧旁路清理
- **WHEN** 检查旧部署、重启或 `/environment/test` 路由
- **THEN** 仓库中不存在这些 HTTP 旁路，维护统一走新 API

#### Scenario: 既有别名保留
- **WHEN** 现有聊天页或面包屑访问 `/api/workers`、`/workers`
- **THEN** 原行为保持可用

### Requirement: Worker 生命周期动作
系统 SHALL 以统一维护操作支持 `check/deploy/update/restart/disable/restore`。`disable` 只暂停新调度，不停止 OpenCode、不取消现有 Task；`restore` 必须基于真实健康探测恢复；`update` 必须显式 fetch 并只做 fast-forward，不得在 `deploy` 中隐式更新已有仓库。

#### Scenario: 暂停调度
- **WHEN** 管理者对存在运行中任务的 Worker 执行 `disable`
- **THEN** 现有 Task/Session 继续执行，仅新调度被拒绝，且不重启 OpenCode

#### Scenario: 恢复调度
- **WHEN** 管理者对停用 Worker 执行 `restore` 且真实健康探测通过
- **THEN** 系统重新启用该员工；探测未通过时保持停用并提示先重启

#### Scenario: 更新已有仓库
- **WHEN** 管理者对已绑定仓库执行 `update`
- **THEN** 系统只 fetch 目标分支并尝试 fast-forward；工作区脏、分支不一致或分叉时停止并保留现场

### Requirement: 装备与健康事实
系统 SHALL 用真实只读探测得出 OpenCode、模型认证、MCP、Skill 与 Git 的期望/实际/差异，并按 freshness 与 Worker epoch 投影健康；调度与手动启动 MUST 拒绝过期健康、离线、停用、活动维护或装备未应用的 Worker。维护失败 MUST NOT 直接改写此前健康，只有真实探测失败或未知才写 `UNAVAILABLE`/`UNKNOWN`。

#### Scenario: 在线但 OpenCode 崩溃
- **WHEN** Worker 连接在线但 OpenCode 健康或 Session 探测失败
- **THEN** 连接状态仍为在线，运行时健康为异常，`schedulable=false`

#### Scenario: 健康观测过期
- **WHEN** 缓存的健康观测超过 freshness 窗口
- **THEN** 健康接口和调度都把环境视为未知并拒绝新任务

#### Scenario: 安装失败但旧环境可用
- **WHEN** 维护在未改变远端或核对确认旧环境仍健康时失败
- **THEN** 环境保持可用，维护记录失败原因，不误报 Worker 不可用
