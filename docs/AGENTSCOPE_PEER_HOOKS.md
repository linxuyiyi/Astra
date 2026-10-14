# PeerSend 公开扩展契约

固定 AgentScope 2.0.10，commit `e6c014f0be49b2a6f6f62a9453082a0b975484e1`。按用户 2026-10-15 的阶段更新，自主 PeerSend 与多员工职责协作延后产品第三阶段；本文只保留源码研究，不是当前第一阶段实施清单或第二阶段前置，尚未代表真实三员工通信通过。

## 可信绑定

公开 tool factory 仅接收 `(user_id, agent_id, session_id)`，身份必须由这些真实值闭包绑定。模型参数只提供接收员工和正文，不允许自行声明 sender、exchange 或 tool call ID。

`MiddlewareBase.on_reply` 接收 `input_kwargs["inputs"]`，`on_acting` 接收 `input_kwargs["tool_call"]`。两个 `next_handler` 都是 async generator，必须 `async for` 传递，不能直接 await。通过两个独立 ContextVar 绑定：

- 当前轮次 Turn：真实 actor、服务端核实的 exchange/owner。
- 当前工具 Call：真实 actor、agent.state.reply_id、tool_call.id/name。

Turn/Call 使用不可变值，禁止放在 middleware/tool 实例的 mutable current 字段中。官方 gather/create_task 并发调用工具，ContextVar 会在创建子任务时复制上下文；共享 dict 会串用。每次 on_reply 都显式设置 Turn（包括 None）并清空 Call；on_acting 只为 PeerSend 设置当前 Call；各自在 finally reset token。

Msg.metadata 不是天然可信：浏览器不能传原生 Msg，Nexus 必须生成 exchange metadata；service 继续按持久 exchange 与 Session 绑定核实。`inputs=None` 的后台唤醒不能取“Session 最近一次 exchange”；需要时依赖持久 reply→exchange 关联，否则 PeerSend 拒绝。

## ToolBase 行为

自定义工具实现 `async call`，保留基类 `__call__` 的 middleware 包装。PeerSend 设置 `is_state_injected=True`、`is_external_tool=False`、`is_read_only=False`，读取注入 `_agent_state` 核对真实 session_id/reply_id 与上述上下文一致。

官方 ToolOffloadMiddleware 跳过 state-injected 工具；PeerSend 仅完成短事务的 envelope/预算/幂等保存并返回确认，不等待目标模型。这样避免十秒 offload 后迟到投递，也避免 A 等 B、B 等 A 的锁环。幂等键使用 `(source_session_id, reply_id, tool_call_id)`，实际投递前再核对 owner、成员、enabled、删除冻结和预算。

相关生成器应在原消费任务中完整消费/关闭，不能将已启动的 hook generator 交给另一任务续消费。

## 固定源码依据

- [公开 MiddlewareBase](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/middleware/_base.py#L68)
- [Toolkit.call_tool](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/tool/_toolkit.py#L225)
- [ToolBase.__call__/call](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/tool/_base.py#L159)
- [extra tools factory 调用](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_service/_toolkit.py#L253)
- [并发工具执行](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/agent/_agent.py#L2255)
- [offload 排除条件](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/middleware/_tool_offload_middleware.py#L144)
- [Python Task 上下文规则](https://docs.python.org/3.12/library/asyncio-task.html#asyncio.create_task)
