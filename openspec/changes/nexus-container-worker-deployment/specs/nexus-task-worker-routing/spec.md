## ADDED Requirements

### Requirement: 按类型与数字员工选择 Worker
系统 SHALL 按任务类型和数字员工关系选择 Worker。任务未指定责任数字员工时，所有支持该任务类型的 Worker SHALL 可作为候选；Worker 未绑定数字员工时 SHALL 作为公共 Worker 服务任意责任员工。任务与 Worker 均指定数字员工且不一致时 MUST 拒绝分配。任务分类 SHALL 保留为业务属性，不限制 Worker 接单。

#### Scenario: 指定数字员工
- **WHEN** 任务指定责任数字员工
- **THEN** 仅绑定同一员工或未绑定员工、且支持该任务类型的 Worker 可作为候选

#### Scenario: 未指定数字员工
- **WHEN** 任务未指定责任数字员工
- **THEN** 所有支持该任务类型的 Worker 可作为候选，不要求分类或仓库匹配

### Requirement: 仓库绑定与调度分离
系统 SHALL 保留任务与仓库绑定；任务没有仓库、任务与 Worker 的仓库 ID 不一致时 MUST NOT 因此拒绝分配或启动。执行快照、工作区和 base commit SHALL 仍来自实际执行 Worker，不得把任务仓库标记冒充实际执行仓库。

#### Scenario: 不同仓库或没有任务仓库
- **WHEN** 支持任务类型的 Worker 接收不同仓库或未绑定仓库的任务
- **THEN** 分配与启动允许继续，任务仓库不被清空或改写，执行快照如实记录 Worker 环境

### Requirement: 所有分配入口规则一致
系统 SHALL 在自动分配、指定 Worker 的 API/MCP、任务编辑和启动时使用同一资格规则；前端 SHALL 展示相同的候选语义。启用、健康、维护、占用、能力已应用、自动领取设置与父任务依赖等既有执行安全限制 SHALL 保留。

#### Scenario: 指定 Worker 不兼容
- **WHEN** API/MCP 或编辑任务指定不支持类型或属于其他数字员工的专用 Worker
- **THEN** 请求被拒绝；不允许绕过自动分配规则

#### Scenario: Worker 暂不可执行
- **WHEN** Worker 被占用、停用、维护或健康不满足要求
- **THEN** 自动分配或启动继续拒绝，不因删除仓库匹配而放行
