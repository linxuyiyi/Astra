## ADDED Requirements

### Requirement: 工程任务必须有确定的目标仓库

Task MCP SHALL 提供已登记仓库的只读发现能力，创建工程任务时保存明确仓库；用户指定 Worker 但省略仓库时 SHALL 使用该 Worker 现有绑定。系统 MUST NOT 猜测其他仓库或成功创建无法调度的空目标任务。

#### Scenario: 指定 Worker
- **WHEN** 创建任务指定一个已绑定仓库的 Worker
- **THEN** 任务保存该仓库，可以进入既有调度流程

#### Scenario: 指定仓库进入任务池
- **WHEN** 调用者从仓库列表选定有效仓库，不指定 Worker
- **THEN** 任务保存该仓库，由既有调度选择兼容 Worker

#### Scenario: 缺少或冲突目标
- **WHEN** 既没有仓库也没有已绑定仓库的 Worker，或两者仓库不一致
- **THEN** 创建返回明确失败，不写入任务

#### Scenario: 调整 Worker
- **WHEN** 待领取任务已有明确仓库，改派另一个仓库的 Worker
- **THEN** 拒绝改派，保留原任务目标与 Worker

#### Scenario: 相同内容用于不同仓库
- **WHEN** 同一会话使用相同内容但不同仓库创建任务
- **THEN** 两个任务分别保存目标，不错误复用其他仓库的任务
