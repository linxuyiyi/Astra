## Why

数字员工聊天的附件原件落在 Nexus 本地文件目录，导致文件归属和实际推理工作区分离。群消息窗口和逐轮资源装配也影响可靠投递。

## What Changes

- **BREAKING** 上传请求必须绑定私聊或群组，旧客户端需同步更新。
- 文件落到 AgentScope 工作区，群附件仅复制给已获授权的接收 Session。
- 发送保留原生内容块和消息 ID，业务授权在服务端判定。
- 空库直接初始化最终 Schema，删除旧附件迁移和旧事件流分支。

## Capabilities

### New Capabilities

- `nexus-workspace-chat-delivery` 管理聊天附件及数字员工消息投递。

## Impact

Nexus API、聊天岛、Agent Service、数据库迁移与回归测试。AgentScope 保持 2.0.10。
