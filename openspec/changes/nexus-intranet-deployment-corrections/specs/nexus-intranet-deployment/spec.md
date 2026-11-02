## ADDED Requirements

### Requirement: revision 长度与已验证版本标记一致

系统 MUST 使用统一、长度不超过版本表容量的正式 revision 标记，当前 `0007` 使用 `0007_worker_readiness`。服务启动 SHALL 只校验数据库。公司已完成的 DDL 与短标记 MUST 沿用；本次按全新代码部署，不增加旧标记转换或运行时双标记兼容。

#### Scenario: 空 MySQL 初始化
- **WHEN** 管理员在严格模式 MySQL 8.0.36 的空库执行正式初始化
- **THEN** 初始化完成到唯一正式短标记，业务结构完整，不出现版本长度截断

#### Scenario: 上一发布增量迁移
- **WHEN** 对正常 `0006` 数据库执行升级
- **THEN** 正常迁移完成并写入短标记，保留业务数据

#### Scenario: 公司已完成迁移
- **WHEN** 数据库已经完成 `0007` DDL 且版本为 `0007_worker_readiness`
- **THEN** 版本校验及重复升级成功，不要求再次执行 DDL 或改回长标记

### Requirement: 镜像来源一致与按来源信任

系统 MUST 使用显式部署清单提供安装源，公网默认值与公司配置分离。主服务与 Worker 安装 SHALL 对同一协议执行一致的来源、隔离和证书规则。HTTP PyPI 的信任 MUST 仅限配置的准确主机及必要端口；HTTPS MUST 保留证书验证。额外来源和冲突配置 MUST 在写入前拒绝。

#### Scenario: 显式 HTTP 内网镜像
- **WHEN** 管理员配置合法的 HTTP PyPI 镜像，包括非默认端口
- **THEN** 主服务安装、隔离构建依赖和 Worker 安装均使用该镜像，可信范围不超过该配置来源，无隐式公网回退

#### Scenario: HTTPS 与公司证书
- **WHEN** 管理员使用 HTTPS 镜像
- **THEN** 安装保留证书验证，不附加可信主机绕过或关闭 npm TLS 校验；证书不可信时明确失败

#### Scenario: 多值信任与额外来源冲突
- **WHEN** 环境或任意安装配置 section 含不同主机的可信项、额外索引、find-links 或不同 registry
- **THEN** 系统在更改任何配置前拒绝，原配置保持不变；重复部署不会积累重复配置

### Requirement: Worker 受限入口覆盖完整通信

系统 MUST 通过现有受访问控制的 Worker 入口同时提供网关模型、Library MCP、Worker 连接与 Worker Data 通信。Data 访问 MUST 保留请求路径、认证和连接代数约束，不能开放通用管理接口。管理入口 SHALL 保持 HTTPS，代理依赖与 TLS 配置由显式部署 profile 决定，不能因公司兼容配置而改变公共默认配置。

#### Scenario: 正常 Worker 经过部署代理执行任务
- **WHEN** 授权 Worker 仅能访问受限部署入口并执行真实任务及文件传输
- **THEN** 模型、连接、正文交换和大小范围内的文件传输均成功，路径和内容完整

#### Scenario: 非法正文访问
- **WHEN** 请求缺少认证、属于其他 Worker、使用过期 epoch，或访问不在允许范围的管理路径
- **THEN** 代理或 API 拒绝访问，不能仅因处于允许网段就读取或写入任务正文

#### Scenario: 公司代理兼容配置
- **WHEN** 使用公司已提供的 Nginx 最低 1.12、TLS1.2 部署 profile
- **THEN** 生成符合该配置的代理规则并保留 Data 鉴权与 ACL，公共默认 profile 不变

### Requirement: 前端发布依赖可复现

主前端和聊天岛发布 MUST 使用随发布提交的有效锁文件进行冻结安装，并保留依赖完整性校验及安装脚本限制。镜像选择 SHALL 只改变受控下载位置，不能重新解析依赖版本。

#### Scenario: 内网镜像冻结安装
- **WHEN** 锁文件与声明一致且只使用允许的制品来源，公网不可达但指定镜像可达
- **THEN** 两个前端安装与构建成功，依赖版本及完整性与发布锁一致，实际请求不访问未授权公网源

#### Scenario: 缺锁、锁冲突或非法制品
- **WHEN** 任一发布项目缺少锁、锁与声明不一致，或包含未允许的制品 URL
- **THEN** 发布明确失败，不退回动态依赖解析或修改生产依赖声明

### Requirement: 模型调用统一使用自动生成的 Nginx HTTP 入口

系统 MUST 由平台一键部署生成并持久化唯一的 Nginx HTTP 模型基址，所有平台模型调用、AgentScope 模型 credential、Worker 和本机受管 OpenCode MUST 使用该入口，不能默认为内部网关监听地址。复盘 SHALL 从统一基址派生请求地址，不能独立配置另一模型入口。

#### Scenario: 生成及传播代理地址
- **WHEN** 管理员执行平台一键部署并配置控制机地址和 Nginx 入口端口
- **THEN** Nginx 监听与所有模型客户端配置来自同一生成结果；Worker 一键部署写入相同基址，主模型与 small model 一致

#### Scenario: 修改入口端口并重启
- **WHEN** 管理员更改 Nginx 入口端口并完成平台部署与 Worker 手动同步
- **THEN** 干净重启后聊天、embedding、ASR、复盘与 OpenCode 都访问新 HTTP 代理入口，不回退内部直连地址
