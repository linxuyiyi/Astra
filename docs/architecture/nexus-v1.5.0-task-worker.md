# Nexus v1.5.0 Task / Worker 重构落地

## 原因与调用关系

原实现把执行拆成 Task、RuntimeSession、AgentWorker、WorkerInstance、OpenCodeExecutionEnvironment，并在控制面保存重复的原生会话状态、连接历史和恢复台账。v1.5.0 直接收敛为：

```text
数字员工 / 人工 → Task → AgentWorker → Worker Gateway → OpenCode 原生 Session
人工确认 → ExperienceReviewJob 来源快照 → Worker + 原生消息分页
          → MAP / REDUCE / MERGE → 知识候选 + Evidence → 人工审核 → 图书馆
维护操作 → AgentWorker → SSH 部署 / Gateway 维护 → Worker 本地配置
```

公司执行模式仍为 `ASTRA_EXECUTOR_TYPE=worker`，控制机不安装或启动 OpenCode；显式本机 `opencode_server` 保留为开发调试，其检查直接验证带认证的原生 HTTP 健康与版本，不走远程 Gateway。AgentScope Agent Service 仍独立管理数字员工的 Agent、Session 和对话记忆。

## 数据结构与删除内容

[最终全部表与字段](nexus-v1.5.0-schema.md)：52 张 Nexus 业务表，另有 `alembic_version`。核心为 Task 27 列、AgentWorker 26 列、WorkerCommand 20 列、WorkerMaintenanceOperation 24 列。全新数据库只执行 `0001_init`，不提供旧库升级、双写和接口适配。

删除以下表及 ORM、外键、路由和关联业务逻辑：

- `t_nexus_runtime_sessions`
- `t_nexus_worker_instances`
- `t_nexus_opencode_execution_environments`
- `t_nexus_worker_connections`

同时删除旧 Session 状态机、提交恢复台账、历史 Session / 用量查询、独立实例 / 环境 CRUD、远程 CLI / Server 旧执行器、静态员工绑定、自动执行开关、占用计数和形象配置。独立手工 CLI 保留。知识中的原生会话引用仍为字符串来源标识；旧 Session 状态快照列已经删除。API 网关自身的模型用量统计继续保留。

Worker 管理统一为 `/api/workers`；维护为 `/api/workers/{id}/maintenance`。Task 使用最终的 `agent_worker_id`、`origin_employee_id`、`source`、`external_task_id` 等字段。Worker 写入使用 POST / PUT；读取接口不返回密文、Token 摘要及待重发正文。

## 状态、占用与取消

Task 只有 PENDING / RUNNING / WAITING / SUCCEEDED / CANCELED；Worker 只有 OFFLINE / IDLE / WORKING / PAUSED。

- Task 与 Worker 行锁串行化领取；一个 Worker 只占用一个 RUNNING / WAITING Task。维护占用也阻止领取。
- 原生 busy / retry（含当前 Session 子会话）映射为 RUNNING，提问、权限及执行结束映射为 WAITING。
- 只有人工确认才落 SUCCEEDED；原生执行结束不表示业务完成。
- 掉线不解除 Task 绑定。取消先退役待提交命令，停止父子 Session 并核实不再 busy / retry，然后才落 CANCELED 并释放。
- 不能确认停止时继续占用；强制释放要求操作者填写原因并留下接受后果的审计事件。
- PAUSED 在掉线、重连期间保持，不会隐式恢复调度。原任务仍能查询、停止及处理已有待重发输入。人工恢复后根据当前连接和占用决定 OFFLINE / IDLE / WORKING。

当前原生指针是工作区、生命周期和复盘来源的唯一执行关联；不再维护 Nexus 历史 Session。活动 Task 独立巡检，缓慢 Worker 不阻塞其他 Task。

## 未知提交与连接隔离

发送前持久化 pending 消息、正文和当前 Session；确认收到后更新 last_message_id 并清空 pending。

未知结果保留原 Task / Worker / Session。先检查原消息是否存在；原生仍忙时等待，停止边界确认后串行重发。只在原 Session 确认不存在时新建，并替换签名上下文。重发使用新 request_id，原命令被退役；旧 epoch、旧 Session、旧消息和旧 request_id 不得覆盖新轮次。

OpenCode 明确拒绝的输入进入 WAITING，并清除待提交输入，保留 Worker 占用供人工处理；不会把已知拒绝当作未知结果循环重发。

此实现接受重复执行，不承诺 exactly-once。Worker 原生写入串行，连接关闭时等待已经发出的原生写入结束，防止重连产生并发写入者。

Worker 只保存 Token 摘要。一键部署重新签发凭据，通过 Worker 本地配置交付，不从数据库恢复旧 Token。重连递增 epoch，拒绝旧连接断开、旧响应和旧正文领取。SSH / Git 凭据继续加密并脱敏。

## 部署维护与来源路由

OpenCode 路径、监听端口由部署脚本与 Worker 本地配置管理；数据库 `port` 仅表示 SSH 端口。保留一键部署、检查、更新、重启、租约续期、取消、重试、事件分页和 SSE 游标续传。前端复用既有维护 API / SSE，当前日志限制为 200 条并支持续页。Task 列表使用后端分页；Worker 配置列表由接口最多返回 200 个资源，不在前端合并状态或全量渲染无界数据。

租约过期、控制进程退出或破坏性维护结果未知，都不等于远端进程停止。此时保留互斥占用，进入 `stop_unconfirmed` 阶段。操作者确认目标机器的进程已停止后，通过 `confirm-stopped` 释放。确定返回的业务失败按真实结果结束。

执行 Worker 与原数字员工是两种身份。来源用 `(source, external_task_id, origin_employee_id)`：私聊定位原 conversation；群聊定位 channel 内原员工；OpenCode 来源核验原会话归属。同一来源可以创建多项 Task。

群聊创建任务时复用事件冻结讨论发起者，通知前再次核验其群权限，只唤醒原员工。Outbox 投递、重试和事件幂等继续保留。

## 经验复盘

确认时冻结 root / child Session、Worker、Workspace、消息锚点和水位。读取器直接按 Job 快照连接 Worker 并分页读取原生消息，验证来源属于冻结的根会话树，不查询 RuntimeSession。

保留 Job / Segment / Evidence、MAP / REDUCE / MERGE、候选验证、失败重试、人工审核和图书馆发布。Task 重启不保存旧 Session 列表，本轮接受只复盘当前可定位的根会话及子会话。来源丢失时真实失败或重试，不生成虚假证据。

## 验证边界

原测试并非原样通过：针对废弃表、字段和接口的测试已经退役，保留并调整底层网关、命令、部署、文件安全、复盘和对话回归，新增 v1.5.0 核心场景。

本次使用隔离的 MySQL 8.0.36、Redis、SQLite 和统一服务验证；桌面维护 UI 使用 API 夹具，不操作真实 Worker。历史 reviews / OpenSpec 文档及用户原有未跟踪文件未改写。

没有重新部署公司 Linux 目标机，也没有调用真实模型完成 OpenCode / AgentScope 业务验收。真实模型验收入口为 `tools/e2e/nexus_v150_task_worker.py`；可选 pytest 为 `test_real_opencode_v150.py`，需显式提供隔离环境 URL 与 Worker ID。

## 本次最终验证结果（2026-11-05）

| 检查 | 结果 |
| --- | --- |
| platform / nexus / MySQL 核心回归，使用隔离 MySQL 8.0.36 与 Redis | 865 passed，16 skipped |
| 部署版本入口、网关与核心架构定向回归 | 65 passed，1 skipped |
| 原生连接、认证、本机检查等定向回归 | 61 passed |
| web 单元测试 | 73 passed |
| chat-island 单元测试 | 17 passed |
| Python Ruff / git diff --check | 通过 |
| web / chat-island 类型检查、生产构建 | 通过；构建仍提示主 chunk 较大 |
| 新 MySQL 的表、字段与最终 ORM 逐一精确比对 | 52 张业务表全部一致 |
| 新 SQLite Alembic、统一服务启动、公开 CRUD 与 OpenAPI | 通过；版本 1.5.0，Operation ID 无重复 |
| 桌面 Worker 页面回归 | 通过；维护锁确认、部署、SSE 日志、配置编辑，浏览器无脚本错误 |

16 个跳过项为：Linux openat2 / O_PATH 文件操作 9 项、真实 AgentScope 4 项、真实 Worker / 模型执行 1 项、Nginx 实际部署 1 项、本机 DNS 导致无法成立的无效域名断言 1 项。跳过不作为验收通过。

后端完整命令：

```bash
cd services/nexus-api
.venv/bin/pytest tests/platform tests/nexus tests/mysql/test_runtime_convergence.py -q -rs
.venv/bin/ruff check app nexus_api tests
```

该结果使用显式隔离的 `ASTRA_REDIS_URL` / `ASTRA_TEST_REDIS_URL`、`ASTRA_TEST_MYSQL_URL` / `NEXUS_TEST_MYSQL_URL`；测试数据库已先按最终 Alembic 初始化。测试实例与业务库分离，不操作公司环境。发布分支为 `v1.5.0`，标准部署器从该分支获取已发布代码。
