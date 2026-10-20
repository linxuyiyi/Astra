# Nebula Home

## ADDED Requirements

### Requirement: 星系快照只读聚合

系统 SHALL 提供只读首页快照，按数字员工、Agent Worker、Task、RuntimeSession 与职责请求的真实事实生成星系、Worker、任务统计、执行光路和协作关系。

#### Scenario: 初始加载

- WHEN 用户打开 `/`
- THEN 页面读取一次 `GET /api/v1/nebula/snapshot`
- AND 员工、Worker、任务、协作数量与服务端事实一致
- AND 页面不写入任何业务状态

#### Scenario: 数据源降级

- WHEN Worker/任务数据源暂不可用
- THEN 快照仍返回员工目录并标记 `degradedSources`
- AND 页面显示部分数据不可用并保留最近确认状态

### Requirement: 员工—Worker—任务归属

系统 SHALL 以 `AgentWorker.digital_employee_id` 作为 Worker 星系归属唯一依据，以任务 `responsible_employee_id` 优先确定责任员工。

#### Scenario: Worker 归属冲突

- WHEN 任务责任员工与执行 Worker 的数字员工不同
- THEN 任务标记数据冲突
- AND 不绘制该任务到错误星系的执行光路

#### Scenario: 未绑定对象

- WHEN Worker 没有 `digital_employee_id`，或任务没有可解析的责任员工
- THEN 对象进入待绑定/待分配入口
- AND 系统不随机归属到任何星系

#### Scenario: 无执行资源

- WHEN 员工没有任何 Agent Worker
- THEN 星系仍完整显示
- AND 明确提示尚未配备执行资源

### Requirement: 执行光路与任务态势

系统 SHALL 将同一员工—Worker 的多个执行中/待人工任务聚合为一条光路，并保持任务状态与 Runtime 事实分离。

#### Scenario: 多任务聚合

- WHEN 同一 Worker 有多个 `RUNNING/WAITING` 任务
- THEN 只绘制一条光路
- AND 显示任务数量与状态分布

#### Scenario: 等待人工

- WHEN 光路包含 `WAITING` 任务
- THEN 光路停止流动并显示等待原因
- AND Worker 占用状态不显示为空闲

#### Scenario: 状态未确认

- WHEN Runtime 连接不可用或 Session 状态未知
- THEN 页面显示“状态待确认”
- AND 不播放成功或执行完成动画

### Requirement: 跨员工职责协作

系统 SHALL 只根据 `t_nexus_peer_deliveries` 的真实职责请求生成星系间关系，不把普通消息或后台通信计为协作。

#### Scenario: 重复请求

- WHEN 同一 scope、发送方、目标与 message_key 重复出现
- THEN 协作次数只计一次

#### Scenario: 双向关系

- WHEN 两位员工互相发起职责请求
- THEN 星系间显示一条关系
- AND 详情保留每个方向的次数与最近时间

#### Scenario: 新协作事件

- WHEN 快照检测到真实新增职责请求
- THEN 关系线播放一次限时脉冲
- AND 历史关系保持静止

### Requirement: 首页交互复用既有业务闭环

系统 SHALL 复用既有私聊、任务详情、任务完成确认与 Worker 详情能力，不新增模拟操作。

#### Scenario: 点击员工节点

- WHEN 用户点击员工主星体
- THEN 镜头聚焦该星系
- AND 显示该员工的轻量提示
- AND 提示可跳转到既有员工对话页和任务大厅

#### Scenario: 任务操作

- WHEN 用户打开任务详情
- THEN 仅在 `WAITING + sessionStatus=SUCCEEDED + 无 pendingMessageId` 时显示完成确认
- AND 确认调用既有 `POST /api/tasks/{id}/confirm`
- AND 服务端冲突时提示并刷新，不自行修改状态

#### Scenario: 返回保持上下文

- WHEN 用户从首页进入任务中心后返回
- THEN 恢复选中员工、Worker、任务与节点提示

### Requirement: 实时同步与可访问性

系统 SHALL 在事件断开时保留最近确认事实，并在减少动态效果时停止持续动画。

#### Scenario: 事件断线

- WHEN SSE 连接中断
- THEN 页面提示连接中断并按退避重连
- AND 重连后重新拉取快照

#### Scenario: 减少动态效果

- WHEN 系统 `prefers-reduced-motion` 或页面设置开启
- THEN 停止镜头巡航、粒子和持续流动
- AND 状态仍通过文本、颜色和线型可辨

#### Scenario: 后台标签页

- WHEN 浏览器标签页不可见
- THEN 暂停高频画布渲染
- AND 回到前台后立即核对快照
