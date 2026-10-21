## Why

真实页面暴露了没有接入的实时语音、以会话编号冒充记忆的入口，以及纯附件无法发送的问题。移除群成员会丢失 Session 绑定，不能继续作为完整可用的成员维护功能提供。

## What Changes

- 移除空壳记忆 Tab、未接入的实时语音操作；独立 ASR 服务就绪后才展示转写入口。
- **BREAKING**：群组维护保留改名、目标修改与添加成员，移除会丢失上下文的成员移除操作；不删除已有数据。
- 修复纯附件发送、会话打开失败重试、群组设置同步，以及输入按钮的可访问名称。
- 修复群聊混合时间格式排序与流式文本增长时不跟随到底部，以及阻塞新 Session 创建的模型路由接口 500。
- 从页面验证私聊、点名群聊、停止、附件、筛选、设置与刷新后的历史。

## Capabilities

### New Capabilities

- `nexus-chat-capability-boundary`：明确当前页面的可用功能边界及失败反馈，收敛早期 change 中尚未完整实现的记忆与成员维护承诺。

### Modified Capabilities

无。`openspec/specs/` 为 Astra 历史契约，不修改；既有 Nexus 聊天 change 作为需求对照。

## Impact

修改 `services/nexus-api/web` 与 `chat-island`；网关只移除一个不兼容的客户端构造参数并补真实依赖回归测试。不新增业务 API、数据表或 Runtime 状态机。AgentScope 仍是消息和记忆唯一事实源。网关本地环境修复与页面代码验证分别记录，原目录的网关开发不在此次范围内。
