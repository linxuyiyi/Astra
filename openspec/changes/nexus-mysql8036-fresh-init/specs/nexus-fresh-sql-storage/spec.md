## ADDED Requirements

### Requirement: 全新数据库一次初始化

Nexus MUST 在空 MySQL 8.0.36 数据库上一次创建当前完整业务 schema，全部自有业务表 MUST 使用 `t_nexus_` 前缀，初始化 MUST 幂等。

#### Scenario: 空库初始化并重复执行
- **WHEN** 运维对空库执行初始化并再次执行
- **THEN** 全部当前业务表存在且版本到达唯一初始版本
- **AND** 第二次执行不删除数据、不重复表或初始记录

### Requirement: 统一业务数据事实源

Nexus MUST 将员工、聊天绑定、团队、IM、资源目录、任务、Worker、网关与图书馆保存到同一个配置数据库，MUST NOT 另写本地 SQLite 业务库作为 fallback。

#### Scenario: 使用 MySQL 的完整 API
- **WHEN** 配置 MySQL 后创建员工、私聊绑定、团队及任务
- **THEN** 数据写入同一个 MySQL 库
- **AND** 服务重启后保留原有 ID 和关联
- **AND** 数据目录不存在新增 SQLite 业务库

### Requirement: 明确拒绝旧库

初始化与启动 MUST 拒绝历史版本或不完整的已有业务 schema，MUST NOT 自动清库、猜测回填或伪造版本。

#### Scenario: 发现历史版本
- **WHEN** 数据库包含本初始版本之前的迁移标记或未标记业务表
- **THEN** 操作失败并提示使用新的空数据库
- **AND** 原库数据及版本不变

### Requirement: 维持消息幂等和事务边界

存储切换 MUST 保持 IM 入站认领、投递确认与资源同步的去重语义，MUST NOT 因驱动差异重复产生业务副作用。

#### Scenario: 重复 IM 入站
- **WHEN** 同一外部事件并发或重试进入
- **THEN** 只有成功认领的调用投递消息
- **AND** 已接受事件返回重复确认并保留原 Session

#### Scenario: 未提交写入退出
- **WHEN** 请求或连接在提交前异常退出
- **THEN** 未提交数据回滚
- **AND** 后续连接不继承未完成事务
