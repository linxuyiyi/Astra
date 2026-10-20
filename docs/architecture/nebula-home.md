# 星系首页（Nebula Home）

v1.4.8 新增的 Nexus 正式首页。页面路由是 `/`，原 `/chat`、`/tasks`、`/settings/workers/:id` 继续可用。

首页是**只读投影**：它读取员工目录、Agent Worker、Task、RuntimeSession 和员工间职责请求，生成星系、执行光路、协作关系与统计；不拥有任务状态机，也不提供 `/nebula/*` 写接口。

## 事实源与关联

```text
数字员工 t_nexus_employees.id
  └─ AgentWorker.digital_employee_id = t_nexus_employees.id
       ├─ OpenCodeExecutionEnvironment.employee_id = AgentWorker.id
       │    └─ WorkerInstance.environment_id = environment.id
       └─ Task.employee_id = AgentWorker.id（实际执行绑定）
Task.responsible_employee_id = t_nexus_employees.id（责任归属）
t_nexus_peer_deliveries.sender_agent_id / target_agent_id
  └─ t_nexus_employees.agentscope_agent_id（职责请求映射）
```

规则：

- `AgentWorker.digital_employee_id` 是 Worker 星系归属的唯一依据。空值进入“待绑定 Worker”；指向不存在员工时列入数据冲突，不随机归属。
- `Task.responsible_employee_id` 优先作为责任员工；为空时才通过 `Task.employee_id → AgentWorker.digital_employee_id` 解析。
- 责任员工与执行 Worker 归属不一致时，任务保留在责任员工计数中并标记 `TASK_OWNER_CONFLICT`，但不绘制执行光路。前端不修写数据。
- 任务没有责任员工且无法通过 Worker 解析时进入“待分配/未关联任务”。
- 同一员工—Worker 的多个 `RUNNING/WAITING` 任务聚合为一条执行光路；`PENDING` 任务只形成任务卫星，不指向 Worker。
- 职责请求按 `(scope, sender_agent_id, target_agent_id, message_key)` 去重后聚合；双向请求合并为一条关系，详情保留方向、次数、最近时间。`recent` 只表示 24 小时内有请求，不代表对方已执行或完成。

## HTTP 契约

前缀 `/api/v1/nebula`，全部只读。

| 方法与路径 | 作用 | 说明 |
|---|---|---|
| `GET /snapshot` | 首页一致性快照 | 员工、Worker、任务统计与预览、执行光路、协作边、待绑定对象、数据冲突 |
| `GET /employees/{id}` | 员工星系详情 | 员工、Worker 列表、活跃/排队/待处理任务、协作关系；需要 `require_owner` 才能解析私聊来源引用 |
| `GET /employees/{id}/tasks` | 员工任务分页 | `status`、`priority`、`q`、`page`、`pageSize`；默认按 `updated_at` 降序 |
| `GET /links/{worker_id}/tasks` | 执行光路任务 | 按 `Task.employee_id` 查询，返回状态分布；用于点击光路后的明细 |
| `GET /tasks/{task_id}` | 单任务投影 | 任务详情刷新与确认完成后的重新读取 |
| `GET /collaborations` | 职责协作详情 | `fromEmployeeId`、`toEmployeeId`、`since`；私聊来源引用按当前 owner 校验 |

`GET /snapshot` 返回的关键字段：

```ts
type NebulaSnapshot = {
  snapshotVersion: string;   // 数据水位 + 内容摘要，仅用于判断快照是否变化
  generatedAt: string;       // UTC ISO8601
  timezone: string;          // 与 Nexus 配置时区一致
  degradedSources: string[];
  totals: {
    employees: number; workers: number; workersOnline: number; workersOffline: number;
    tasksRunning: number; tasksQueued: number; tasksWaiting: number;
    tasksFailed: number; completedToday: number;
  };
  employees: NebulaEmployee[];
  workers: NebulaWorker[];
  taskEdges: NebulaExecutionEdge[];
  collaborationEdges: NebulaCollaborationEdge[];
  queuedTasks: NebulaTask[];      // 每员工最多 5 个排队卫星
  attentionTasks: NebulaTask[];   // 待人工、失败、连接异常
  unassignedTasks: NebulaTask[];  // 待分配任务预览
  unassigned: { workerCount: number; taskCount: number; counts: NebulaCounts };
  dataConflicts: NebulaConflict[];
  dataWarnings: NebulaWarning[];
};
```

任务 DTO 同时暴露业务状态与 Runtime 事实：`status`、`runtimeConnection`、`sessionStatus`、`pendingMessageId`、`waitingReason`、`waitingReasonCode`、`failureReason`。`waitingReason` 是给用户看的文案，`waitingReasonCode` 保留后端原始码。

### 统计口径

- `queued` = `PENDING`；`running` = `RUNNING`；`waiting` = `WAITING`；`failed` = `FAILED`（当前未清除的失败任务）。
- `completedToday` 只统计 `SUCCEEDED` 且 `completed_at` 落在 Nexus 配置时区“今天”的任务，按唯一 Task 计数；重新执行不会重复累加。
- Worker 健康与占用分开：健康来自 WorkerInstance/执行环境真实状态；占用来自 `Task.employee_id` 的 `RUNNING/WAITING` 绑定，等待人工仍视为占用。
- 远程 Worker 心跳超过 `worker_gateway_ping_seconds + ping_timeout_seconds` 时不会显示在线。
- 快照的 `snapshotVersion` 由各表最大更新时间、职责请求时间与内容摘要组成；前端只比较是否变化，不用它推断业务状态。

## 实时同步

1. 首次加载 `GET /api/v1/nebula/snapshot`。
2. 复用 `GET /api/events/stream` 作为变更通知；收到任务事件后 350ms 防抖刷新快照，不把 Event 当作完整任务数据。
3. 15 秒周期快照兜底员工、Worker、职责请求变化。
4. SSE 断线后指数退避重连（1s → 30s）；恢复后刷新快照；页面回到前台时立即核对。
5. 数据源不可用时返回 `degradedSources` 并保留最近确认快照，前端显示“事件连接中断/部分数据不可用”，不用模拟数据填充。

## 前端模块

```text
web/src/views/NebulaHomeView.vue                 首页容器、选中/路由恢复、交互
web/src/features/nebula/
  types.ts                                       DTO 契约
  api.ts                                         首页只读投影 API
  layout.ts                                      稳定星系布局、轨道、光路与协作曲线
  camera.ts                                      透视投影、聚焦、绕鼠标点旋转、缩放/平移/巡航
  renderer.ts                                    Canvas 2D 渲染与命中检测
  stores/nebulaStore.ts                          快照、SSE、断线、脉冲/完成反馈
  components/NebulaCanvas.vue                    画布与键鼠交互
  components/NebulaSummary.vue                   全局指标与连接状态
  components/AttentionEntry.vue                  待人工/失败/异常 Worker 入口
  components/NodeHint.vue                        点击节点后的轻量提示与业务页跳转
```

- 首页不内嵌对话或业务面板。点击员工、Worker、任务卫星、执行光路、协作线后显示轻量 `NodeHint`；提示中的链接跳转到既有聊天页、任务中心、Worker 详情和员工配置。
- 首页不提供“添加任务/分配 Worker/完成任务/发起协作”等模拟按钮，也不在首页写任务状态。所有业务动作都跳转既有页面完成。
- 画布只消费标准化场景；拖动旋转以按下时的鼠标屏幕点为临时轴心，选中状态写入路由 query，直接打开或从其他页面返回时可恢复选中对象。
- 全屏首页不渲染站点顶部导航；右上角仅保留“工作台”入口和画布控制。
- `prefers-reduced-motion` 或页面“减少动效”开启时停止巡航、粒子和流动光点；后台标签页暂停渲染循环。
- 视觉资源全部本地打包，无运行时公网 CDN。

## 验证

```bash
cd services/nexus-api && .venv/bin/pytest tests/platform/test_nebula.py
cd services/nexus-api/web && npm run type-check && npm run build
```

`tests/platform/test_nebula.py` 覆盖：星系/Worker/任务聚合、执行光路、责任与 Worker 归属冲突、待绑定对象、今日完成时区、职责请求去重与方向、私聊引用权限、任务分页、链接任务、单任务投影、数据源降级与 HTTP 参数校验。

## 已知限制

- `/api/v1/me` 仍是占位身份；生产多人访问前需要统一认证，不能把匿名私聊来源引用当作组织全局可见。
- 职责请求表目前没有“已接收/处理中/完成”状态；首页只显示请求事实，不推断执行结果。
- WorkerInstance 变更没有独立事件通道，健康状态依赖 15 秒快照和 Worker Gateway 自身的下线清扫。
