## ADDED Requirements

### Requirement: 受管 Worker 安装与验收

系统 SHALL 从 Worker 主机检查受管入口，使用平台启用的当前 Skill 版本和已绑定 MCP 的连接参数，并在维护操作时限内以本次认证连接和当前装备观测判断就绪。

#### Scenario: 入口不可达

- **WHEN** Worker 无法访问受管代理入口或必要路由
- **THEN** 安装报告出错的路由和网络或 HTTP 诊断

#### Scenario: 绑定含认证头的 MCP 与平台 Skill

- **WHEN** Worker 绑定了带自定义认证头的 MCP 和启用的平台 Skill
- **THEN** OpenCode 收到认证头与当前 Skill 版本，日志不得回显凭据

#### Scenario: 启动缓慢或 MCP 瞬时失败

- **WHEN** 安装完成但本次连接或装备尚未就绪
- **THEN** 验收在部署总时限内等待，并有限重连已知瞬时失败的必需 MCP；成功须基于当前观测，失败须提供实际阻塞原因
