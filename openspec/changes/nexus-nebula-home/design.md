## 决策

### 只读聚合层

跨 `nexus_api` 的 sqlite3 员工目录与 `app` 的 SQLAlchemy 任务/Worker 数据层时，按 ID 批量读取后在内存组装；不写跨层 JOIN，也不包装分布式事务。投影不复制状态机，`Task.status` / `RuntimeSession.status` 原样暴露，前端只做展示映射。

### 归属与冲突

`AgentWorker.digital_employee_id` 是 Worker 星系归属唯一依据。任务责任员工优先取 `responsible_employee_id`，为空时才由执行 Worker 解析。两者冲突时任务保留责任员工计数并标记 `TASK_OWNER_CONFLICT`，不画执行光路；未知员工或缺失 Worker 进入数据冲突/待分配列表，不猜测归属。

### 执行光路与协作线

同一员工—Worker 的多个 `RUNNING/WAITING` 任务聚合成一条边，候选任务 ID 限 5 个。`WAITING` 光路静止并显示原因；`runtimeConnection=UNAVAILABLE` 的边显示“状态待确认”，不播放执行粒子。职责请求以 `(scope, sender, target, message_key)` 去重，双向合并显示，强度使用 `log1p` 映射；只有快照检测到真实新增请求或新时间时才播放限时脉冲。

### 实时与降级

快照是权威事实，Event 只是失效通知。15 秒轮询兜底；SSE 指数退避重连；数据源异常返回 `degradedSources`，保留最近确认快照并明确展示失效时间。`snapshotVersion` 由数据水位和内容摘要组成，前端只用于判断是否变化。

### 视觉与交互

采用 Canvas 2D + 三维坐标透视投影；布局由员工 ID 稳定哈希与确定性分离生成，任务状态变化不重排星系。镜头按选中星系世界坐标聚焦并计算适配缩放；标签有优先级与避让，远景折叠次要 Worker 标签。画布与 Element Plus 面板分层，聊天岛通过 iframe 复用 `/nexus-chat/index.html`。

### 安全边界

投影不返回 Worker 主机、令牌、口令或模型请求正文。职责协作详情中的私聊来源只对当前 owner 暴露会话 ID；快照不携带会话/群组 ID。首页不提供模拟业务按钮，唯一写操作是既有任务完成确认，且经过服务端状态校验。

## 被舍弃方案

- 直接 iframe 原型 HTML：不能满足真实数据、权限和业务闭环，放弃。
- Three.js/WebGL：本期无需引入大型引擎，Canvas 2D 足以达到 V3.4 视觉基线；保留 DTO 与渲染适配层，未来可替换。
- 前端聚合多个既有接口：会形成第二套派生状态和 N+1 请求，放弃；由服务端统一一次快照。
