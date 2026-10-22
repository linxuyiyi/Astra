## Why

群组按 @ 独立触发员工，未等待前序回复；浏览器自报 UID 和无成员鉴权又使多人协作无法建立可信边界。需要把群组收敛为有真实身份、共享记录和有限讨论的协作空间。

## What Changes

- **BREAKING**：服务端账号与 HttpOnly 会话替代浏览器 UID；群组所有入口检查成员与角色。
- 统一人类与员工成员关系，加入邀请、退出、移除、角色、已读和在线状态。
- 公开群消息统一排序、分页、幂等和 SSE 回放；员工 Session 保留独立上下文。
- 规则选择参与者，按真实已完成发言逐个调用员工，限制轮次、时间、输出与工具权限，新消息取消旧讨论。
- 复用现有聊天岛渲染，修复自己/他人消息布局；Vue 成员管理沿用 Element Plus。

## Capabilities

### New Capabilities

- `nexus-collaborative-groups`：可信多人类与数字员工协作、共享历史、自然有限轮次与权限。

### Modified Capabilities

无。现存 Astra 历史契约保持原样。

## Impact

涉及 nexus_api 群聊与认证、Agent Service 群会话工具边界、Task MCP 调用主体、web 与 chat-island；新增 Alembic revision。普通聊天不创建 Task，不改变 Worker 执行状态机。不引入 Leader 或 Agent Team。
