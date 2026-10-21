## 身份解析

维护 API 复用 Nexus 现有的 UID 解析顺序：`X-Verified-UID`（可信代理）→ `X-Owner-Id`（手动输入 UID 模式）→ `nexus_uid` Cookie（页面同源请求）。三者都缺失时返回 401，与 `require_owner` 的错误语义一致。解析规则统一放在 `app/identity.py`，聊天与维护路由的 FastAPI 依赖都调用它，`app` 层不反向依赖 `nexus_api`。Cookie 按浏览器的 encodeURIComponent 规则还原一次，身份头保留原值，不能把编码文本当成用户 UID。

## 请求体

`MaintenanceSubmit` 只保留 `action`。请求体里的 `requested_by` 不再读取（Pydantic 默认忽略额外字段），避免旧页面或脚本继续用任意文本冒充请求人。前端 `submitMaintenance()` 同步移除默认值参数。

## 审计写入

- 提交：`requested_by` 保存解析后的 UID。
- 取消 / 重试 / 只读核对：新增可选 `actor` 参数，写入对应维护事件的 `detail.requested_by`，不改操作行的 `requested_by`。
- 系统动作（`worker-channel`、`api`）沿用现有调用，不传 `actor`。

## 历史数据

`console-admin` 与 `api` 视为旧版本无 UID 记录，只在展示层标注，不迁移、不追填。保留原值便于追溯。

## 被舍弃的方案

- 前端把 `currentUid()` 放进请求体：仍是客户端自证，且手动模式以外的部署拿不到值。
- 给操作表新增 `canceled_by`、`retried_by` 列：当前只需要审计追溯，维护事件表已具备 detail 字段；新增列与迁移会把展示问题扩大成数据模型变更。
- 把 `console-admin` 统一回填成当前用户：没有证据，会制造错误归属。

## 展示收敛

列表只展示接单状态，历史维护结果只在维护记录中出现。连接和 OpenCode 健康移入折叠的运行诊断；记录详情通过抽屉查看，版本与日志继续折叠。配置抽屉只编辑配置；部署/更新/重启入口只对后端支持的远程环境展示。当前占用任务直接读取健康接口的 occupied_task 引用，任务状态不用于另造占用规则。任务记录分页展示。
