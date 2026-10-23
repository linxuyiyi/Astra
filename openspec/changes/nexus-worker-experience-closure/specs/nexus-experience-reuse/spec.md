## ADDED Requirements

### Requirement: 执行上下文支持受控知识读取

Worker 的正式执行模型 SHALL 收到当前 Session 的内部工具上下文，工具校验其真实绑定；上下文 MUST NOT 出现在复盘模型输入、经验证据或候选正文中。

#### Scenario: 新任务读取正式经验
- **WHEN** 新任务通过图书馆工具查询和读取已发布经验
- **THEN** 工具接受当前有效上下文，记录真实 Task 与 Session
- **AND** 复盘读取同一原生轨迹时内部凭证已脱敏

### Requirement: 复盘成功和证据关联准确

成功复盘 SHALL 清除已恢复的旧错误，每条新增证据 SHALL 关联产生它的复盘作业。合法任务 Git worktree 的轨迹 SHALL 可读取，跨仓库或未注册兄弟目录 MUST 被拒绝。

#### Scenario: 失败恢复
- **WHEN** 原生来源恢复且重试成功
- **THEN** 作业显示成功，不继续显示旧失败；候选证据可定位到本次作业、Session、message 和 part

### Requirement: 经验读取回溯到确定来源

系统 SHALL 使用 Wiki 来源页的结构化元数据和已发布源文件绑定关联读取记录，不用标题或正文示例猜测。无法确定唯一来源时 SHALL 保留外部读取事实，不虚构本地知识关联。

#### Scenario: 编译来源页
- **WHEN** Worker 读取只有一个明确 Nexus 来源路径的已发布 Wiki 来源页
- **THEN** 读取记录关联该知识和当前 Task，引用次数增加，知识详情可看到使用任务

#### Scenario: 模糊或伪造引用
- **WHEN** 只有正文示例包含知识 ID，或来源不唯一
- **THEN** 不按示例或猜测关联本地知识
