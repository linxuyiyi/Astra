## ADDED Requirements

### Requirement: 原生远程通信与资源释放
系统 SHALL 经 SSH 访问 Worker loopback 上的原生服务，复用连接和转发，删除旧通信接口。

#### Scenario: 并发访问及关闭
- **WHEN** 同一 Worker 的 HTTP、事件订阅和工作区并发访问
- **THEN** 复用 SSH 与转发，关闭控制服务释放本地连接但不停止远端执行

#### Scenario: 独立进程复用统一连接所有者
- **WHEN** 调度进程领取任务、独立任务工具控制任务或复盘进程读取原生消息
- **THEN** 只有统一控制服务建立运行时 SSH；工具经内部认证 HTTP 调用业务入口，复盘经任务代理读取，调度进程不直接启动执行

### Requirement: 任务状态以原生状态对账
系统 SHALL 将 busy/retry 映射 RUNNING，问题/权限映射 WAITING，idle 按完成规则等待确认，人工确认 SUCCEEDED，确认取消 CANCELED，确认执行失败 FAILED。

#### Scenario: 通知触发核实
- **WHEN** 收到状态事件或周期巡检到期
- **THEN** 查询原生状态、交互请求和当前轮结果，不能凭单个事件确认完成或失败

### Requirement: 临时断线恢复
系统 SHALL 重连 SSH、重订阅事件、重试对账并保持原任务与占用，不重启正在执行的进程。

#### Scenario: 连接恢复
- **WHEN** SSH 或 SSE 中断后恢复
- **THEN** 查询原 Session 并恢复监听；确认 Session 不存在时进入 WAITING 等待人工

### Requirement: 隔离与完整任务流程
系统 SHALL 保留任务创建分配、独占 Worker、人工交互、补充输入、确认、取消及原生工作区。

#### Scenario: 多 Worker 并行
- **WHEN** 两个 Worker 各运行任务与子 Session
- **THEN** 状态、事件、目录和代理请求只作用于各自绑定，不串用其他任务 Session
