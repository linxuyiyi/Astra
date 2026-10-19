# Tasks

> 本 `tasks.md` 是 v1.4.5 R01-R10 的唯一实施清单。后端维护核心、命名/别名、旧旁路清理与 UI 新 API 接入已完成；真实 Linux 验收仍在进行，不能提前勾选。

## 1. 契约与 OpenSpec

- [x] 1.1 完成 `nexus-v145-worker-maintenance` 的 proposal/design/spec/tasks，明确操作状态机、租约、API JSON、状态分离与旧接口过渡。验证：`openspec validate --strict` 通过，且 `openspec/specs/` 历史契约未修改。
- [x] 1.2 将用户定稿复制到 `docs/design/nexus-v145-worker-deployment-maintenance.md` 便于追踪。验证：内容与定稿一致，不另写计划。

## 2. 持久化与迁移

- [x] 2.1 新增 `WorkerMaintenanceOperation`、`WorkerMaintenanceEvent` 模型，覆盖 state/stage/attempt/lease/cancel/danger/快照/错误字段。验证：模型可建表，唯一活动环境约束生效。
- [x] 2.2 新增 Alembic migration，创建两表、索引与唯一活动环境约束。验证：新 SQLite 库 `upgrade head` 成功，`downgrade` 可回退。

## 3. 维护服务与执行器

- [x] 3.1 实现提交、查询、历史、事件追加与脱敏；提交在短事务内锁环境、检查 canonical 占用并插入活动操作。验证：并发提交只有一个成功。
- [x] 3.2 实现 claim、续租、fencing、finish、过期恢复与 `INTERRUPTED` 持久化；多进程不抢占有效租约。验证：陈旧 owner/attempt 写入被拒绝。
- [x] 3.3 在 FastAPI lifespan 增加数据库驱动 executor 与优雅退出中断；不进入 Celery 队列。验证：断线后继续执行，进程退出后操作可恢复。

## 4. 共享保护与状态分离

- [x] 4.1 把环境保存/检测/删除、Worker 通道、Task 启动与调度器的维护锁统一改为操作表判定。验证：活动维护期间这些入口返回冲突，结束后恢复。
- [x] 4.2 危险步骤前做 canonical 占用与原生 idle 检查，在线 restart 补齐原生 idle；native 检查后做数据库 fenced 复检。验证：WAITING/busy/并发启动被拒绝。
- [x] 4.3 维护失败不无条件置 `UNAVAILABLE`；连接、运行时健康、维护状态与 `schedulable` 分开返回。验证：失败但旧环境健康时仍可调度，未知时禁止调度。

## 5. 远程执行复用与幂等

- [x] 5.1 复用 `RemoteProvisioner` 的 SSH/安装脚本/Worker 命令，改由持久化操作驱动并持久化步骤事件。验证：部署/重启/检查走同一 executor。
- [x] 5.2 `_install_units` 识别现有有效 Worker env/token 与 unit，避免重复 enroll 轮换；重试复用远端有效 Key 与配置。验证：重复维护不轮换 token。
- [x] 5.3 失败只撤销未写入配置的新 Key，已写入配置的 Key 保持；不承诺自动回滚 Git/DB/凭据。验证：失败清理不破坏当前配置。

## 6. API 与旧接口

- [x] 6.1 新增环境维度维护提交/历史、操作详情、events SSE（`Last-Event-ID`/`after_id`）、cancel、retry、health。验证：SSE 续传不重复，取消/重试语义正确。
- [x] 6.2 删除无消费者的旧 `/api/employees/{id}/environment/{deploy|restart}` NDJSON handler；UI 删除最后 `environment/test` 调用后同步删除该路由与 schema，不保留 HTTP 等待 check 旁路。验证：rg 无活跃消费者。
- [x] 6.3 保留 `/api/workers` 只读别名与前端 `/workers` 重定向；新接口使用 environment_id 并返回 employee_id 以区分。验证：现有消费者测试通过。

## 7. 测试与验证

- [x] 7.1 targeted pytest 覆盖：HTTP 断线不取消、SSE 续传、lease/fenced stale writer、两并发维护、Task-start 竞争、WAITING/native busy、取消危险半完成、失败但旧环境健康。验证：新增测试通过。
- [x] 7.2 运行受影响回归（remote provisioning、channel switch、worker occupancy、lifecycle/dispatcher）与 `ruff check` 变更范围。验证：命令输出留档；macOS 上 installer/O_PATH 用例按 Linux 回归处理。
- [x] 7.3 新 SQLite 库执行 Alembic `upgrade head` 并验证 schema。验证：迁移成功。
- [x] 7.4 更新既有 `tests/test_remote_provisioning.py` 等对 JSON operation 的过时断言，避免双套状态兼容。验证：无 `status_detail.operation` 长期状态依赖。
- [x] 7.5 检查 `git diff` 无无关修改、调试代码和旧 JSON/表双套状态，提交 Conventional Commit。验证：diff 仅覆盖本 change。

## 8. 同一 v1.4.5 后续阶段（本阶段不实做，不是范围外）

- [x] 8.1 Web：Agent Worker 列表/详情、维护进度离开页面可恢复、健康四态与阻塞项展示；旧 EmployeeConfig 已迁移到新维护 API 并删除最后 `testEnvironment` 调用。验证：UI type-check/build 由 UI 阶段执行，后端 rg 无旧消费者。
- [ ] 8.2 Git 完整管理：`update` 显式语义、fetch/pull、工作区变更保护、commit 记录与人工冲突提示。
- [ ] 8.3 装备一致性：Skill/MCP/模型/版本期望与实际差异、仅必要时更新。
- [ ] 8.4 Runtime 详细健康核验：OpenCode API、模型网关认证、MCP 连接、Skill 实际发现的真实探测矩阵。
- [ ] 8.5 对外命名清理：`ASTRA_*`/`astra-gateway`/`astra-backup` 与协议/配置/测试同步替换。
- [ ] 8.6 真实 Linux Worker 验收：x86_64/aarch64 部署、重启、断网重连、故障注入、权限与密钥检查。
