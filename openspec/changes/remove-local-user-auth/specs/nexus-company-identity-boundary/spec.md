## ADDED Requirements
### Requirement: 不提供本地账号权限系统
Nexus MUST NOT 提供注册、密码登录、退出、账号列表与用户权限编辑。
#### Scenario: 删除旧入口
- **WHEN** 访问旧账号 API 或页面
- **THEN** API 不存在且没有对应表单
### Requirement: 身份与业务边界
Nexus SHALL 使用临时记录 UID 关联操作人，不依据本地用户权限裁剪员工能力或页面，保留业务归属、群角色与服务认证。
#### Scenario: 临时 UID
- **WHEN** 用户输入之前使用的 UID
- **THEN** 浏览器临时记录并用于聊天归属，不要求注册或密码，旧登录 Cookie 无效
#### Scenario: 员工工具
- **WHEN** 执行群聊工具或任务
- **THEN** 校验成员、讨论与员工配置，不读取本地用户权限
### Requirement: 新迁移清理
Nexus SHALL 通过新 migration 删除账号凭据、权限和登录会话，保留业务归属 ID。
#### Scenario: 数据库升级
- **WHEN** 执行清理迁移
- **THEN** 本地账号与会话表删除，群成员与会话 owner 不被改写
