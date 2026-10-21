## ADDED Requirements

### Requirement: 官方 SQL 持久化

Agent Service MUST 使用固定官方版本的 SQL 存储保存自身 Agent、Session、消息、凭据与资源记录，MUST 与 Nexus 业务 schema 独立管理。

#### Scenario: 新部署初始化与重启
- **WHEN** 运维初始化空 AgentScope MySQL 库并启动 Agent Service
- **THEN** 服务使用官方 SQL 存储读写记录
- **AND** 重启后保留原 Agent、Session、消息和 credential ID

#### Scenario: 同秒消息保持先后与分页
- **WHEN** 同一 Session 在一秒内先后写入多条消息并跨服务重启
- **THEN** 官方消息接口按写入先后返回消息
- **AND** 游标分页不因 MySQL 丢失微秒而重排消息

### Requirement: 持久化与消息总线独立

Agent Service MUST 保留共享消息投递与唤醒能力，MUST NOT 用 SQL 持久化取代消息总线。

#### Scenario: SQL 存储的同事联系
- **WHEN** 使用 SQL 存储的员工收到同事提示
- **THEN** 提示经现有 inbox 和消息总线投递并唤醒目标 Session

### Requirement: 数据库异常不回退

Agent Service MUST 在缺少 SQL 配置、数据库不可用或 schema 未初始化时明确失败，MUST NOT 自动改用其他持久化后端。

#### Scenario: 无法打开配置库
- **WHEN** 配置的 AgentScope SQL 数据库不可用
- **THEN** 服务启动失败并返回不含凭据的诊断
- **AND** 不创建新的本地业务数据库
