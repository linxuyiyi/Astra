## Why
群聊误引入本地账号、密码与权限系统，与后续使用公司登录的约定冲突。
## What Changes
- **BREAKING** 删除本地注册、登录、退出、账号列表和权限编辑 API 与表单。
- 新 migration 删除密码、会话和用户权限；不改已发布迁移。
- 保留群角色、业务归属、员工能力与服务间认证，身份交由公司登录接入。
## Capabilities
### New Capabilities
- `nexus-company-identity-boundary`: 公司身份接入边界与本地账号清理。
### Modified Capabilities
无；不修改 Astra 历史契约。
## Impact
统一 API、群聊、Task MCP、Vue 页面、数据库与测试。
