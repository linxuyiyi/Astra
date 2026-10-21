# Tasks

## 1. 契约

- [x] 1.1 完成 proposal / spec / design / tasks，并确认 `openspec/specs/` 历史契约未修改。验证：`openspec validate nexus-worker-maintenance-actor --strict` 通过。

## 2. 后端身份与审计

- [x] 2.1 维护路由新增身份依赖，按 `X-Verified-UID` → `X-Owner-Id` → `nexus_uid` 解析；缺失返回 401。验证：无身份提交返回 401，不写入操作。
- [x] 2.2 提交接口只接受 `action`，请求人取解析结果；请求体 `requested_by` 被忽略。验证：伪造请求人时记录真实 UID。
- [x] 2.3 取消、重试、核对接受操作者并写入维护事件 `detail.requested_by`，不覆盖发起人。验证：不同 UID 重试后发起人不变、事件含操作者。

- [x] 2.4 聊天与维护共用 UID 解析；Cookie 编码还原、身份头优先级与原值保留通过测试。

## 3. 前端展示裁剪

- [x] 3.1 语义色集中到一处映射，中性色由主题层提供；列表、详情、维护记录与日志共用。验证：未知/取消/空闲为灰色，离线/失败为红色，进行中为蓝色。
- [x] 3.2 Worker 列表移除统计卡、重复维护列与工作路径/仓库长地址，折叠低频筛选，保留名称、数字员工、部署位置、可接任务、当前任务与操作。验证：默认宽度下无需横向滚动即可看到可接任务与操作。
- [x] 3.3 Worker 详情收敛为「任务 / 配置 / 维护」，取消运行概览与用量页签；当前任务归任务页，配置事实归配置页，暂不可接任务原因紧跟接单状态。验证：三页签可达，#overview/#usage 旧链接落到任务页。
- [x] 3.4 维护区只保留一处操作栏；进度、失败原因与记录优先，版本差异、日志、装备明细按需展开；装备只保留「组件 / 验证结果 / 原因」摘要，未验证不显示成功。验证：首屏先看到结果与操作，展开后才出现明细。
- [x] 3.5 前端请求人移除默认值，展示历史未记录 UID 标注。验证：新记录显示真实 UID，旧 `console-admin` 显示“历史记录，UID 未记录”。

- [x] 3.6 列表与详情使用后端 occupied_task 引用展示当前占用，不按任务状态猜测；任务记录分页。
- [x] 3.7 配置抽屉移除重复维护区，本地环境不出现远程部署、更新、重启与保存并部署入口。

## 4. 验证

- [x] 4.1 后端针对性测试：`pytest tests/platform/test_worker_maintenance.py tests/platform/test_worker_maintenance_actor.py -q`。
- [x] 4.2 前端 `node --test tests/*.test.ts`、`npm run type-check`、`npm run build`。
- [x] 4.3 复查 `git diff`，确认无无关修改；真实浏览器检查列表、原因、任务/配置/维护、详情日志、诊断折叠与旧 hash 入口，无法触发的运行状态明确标注。

本轮验证：后端维护/身份及 Nexus API 回归 136 passed；前端测试 30 passed；type-check、build、针对性 Ruff 与 OpenSpec strict validate 通过。浏览器读取当前真实服务数据，未触发部署、重启或维护重试；运行中任务与真实模型消耗不作为本轮实页验收结论。
