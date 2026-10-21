## ADDED Requirements

### Requirement: 服务环境可恢复

部署 MUST 持久化执行模式、数据库及网关连接配置和稳定的内部调用凭据，服务重启 MUST 使用同一配置。远程模式 MUST NOT 安装、探测或启动控制机的 OpenCode；实际 Worker 的版本校验 MUST 保留。

#### Scenario: 干净进程启动远程部署
- **WHEN** 安装器从独立配置文件生成运行环境，服务在无父进程环境的情况下启动
- **THEN** 执行模式为 worker，网关凭据与地址可用
- **AND** 重复安装不生成新的调用密钥

### Requirement: 统一模型代理

员工聊天、Worker、知识库 embedding 与平台复盘的模型请求 MUST 通过 Nexus 网关；上游厂商凭据只由网关节点持有。嵌入调用 MUST 使用现有网关鉴权、路由、额度、并发租约、故障与审计语义，MUST NOT 退回厂商直连。

#### Scenario: 嵌入请求转发
- **WHEN** 授权客户端向嵌入入口提交模型与输入
- **THEN** 网关使用配置路由映射模型并转发嵌入请求
- **AND** 返回向量及用量，释放租约并记录请求

#### Scenario: 嵌入调用拒绝
- **WHEN** 密钥无效、路由不可用、超过额度或提交流式嵌入请求
- **THEN** 调用明确失败，不产生上游副作用

#### Scenario: 知识库使用网关模型
- **WHEN** 图书馆发布文档并配置嵌入路由
- **THEN** 使用指向网关的凭据创建官方知识库
- **AND** 不接受独立厂商 credential 配置

#### Scenario: Worker 地址规范
- **WHEN** 运维提供带或不带 /v1 的合法网关根地址
- **THEN** Worker 使用唯一 /v1 前缀进行模型查询与推理

#### Scenario: 受管 Serve 禁止绕过网关
- **WHEN** 平台创建或复用本机、共享或远程 Serve
- **THEN** 主模型与小模型均使用唯一 nexus-gateway provider
- **AND** 复用进程必须通过实际配置校验，特殊测试模型名不能跳过路由与鉴权

### Requirement: 外部知识服务使用真实项目

接入已有 llm_wiki MUST 使用明确的现有鉴权和项目标识并检查项目访问，MUST NOT 随机生成无法在外部服务注册的值。外部服务的模型配置 MUST 指向其可达的 Nexus 网关，由该服务运维管理。

#### Scenario: 既有服务缺少项目配置
- **WHEN** 安装时启用 llm_wiki 却缺少 token 或项目标识
- **THEN** 安装在服务修改前明确失败并提示配置项

#### Scenario: 项目不可访问
- **WHEN** llm_wiki 健康接口成功但项目鉴权或查询失败
- **THEN** 安装报告失败，不以健康接口代替项目可用性
