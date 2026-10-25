## Context
群聊新增的 auth.py 管理密码、Cookie 和本地权限，并耦合群成员和工具裁剪。
## Goals / Non-Goals
删除本地账号与 RBAC；保留可信身份接入点、群角色与员工能力。不实现公司登录协议。
## Decisions
按用户明确约定恢复 localStorage nexus_owner_id 与 nexus_uid Cookie 临时记录 UID；X-Owner-Id 用于请求关联。这不是身份认证，公司登录后替换这一处。目录仅记录临时 ID 和展示名，不存密码或权限。历史群成员与会话 owner 原样保留，不猜测映射公司账号。通过新 migration 删除本地账号和会话表。
员工工具与 MCP 依据员工配置，群成员、generation、lease、内部密钥和 Worker 凭据仍校验。
## Risks / Trade-offs
公司登录尚未接入；真实公司身份验证留待对接。数据库清理不可逆，部署执行前按流程备份。
