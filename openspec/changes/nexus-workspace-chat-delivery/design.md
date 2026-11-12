## Decisions

- 群历史按接收者游标有界读取。AgentScope 2.0.10 的公开 HTTP 只提供原生历史查询，因此在 Agent Service 增加一个内部追加入口。它在原生 Session 锁内更新 Session 上下文与消息日志，以原始 Msg.id 幂等处理重试。补送期间不调用 Agent Loop。实际发言使用固定的讨论轮次 ID。
- 私聊在调用原生 `/chat/` 前，用现有消息表登记正文摘要、附件及目标 Session。Agent Service 从原生消息日志确认接受。若 Nexus 写确认失败，同一消息重试可直接确认原先的原生消息。新的 Session 不接收旧 Session 中结果不明的消息。
- 附件落在 AgentScope Workspace。Nexus 保存归属和展示引用，Agent Service 生成模型可见的附件提示并按需读取。私聊恢复沿用原 Workspace，写入新绑定前校验文件并将缺失引用置为失效。
- 群成员撤权由 Nexus 成员关系和讨论授权立即执行。现有群轮询器负责重试回收。查询、远程回收、按旧 Session 更新绑定分三步执行。Agent Service 停止原生 Session 后清理附件副本，重试已完成的回收不会碰后续新会话。
- 私聊与群聊共用资源就绪检查，复用已就绪的装配。客户端统一订阅 AgentScope 原生 SSE，使用原生回放游标。
- 本轮以最终结构从空库部署。附件引用、群顺序游标和资源挂载名直接建在 `0001_init`。
