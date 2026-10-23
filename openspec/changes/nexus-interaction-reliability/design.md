## Context

复核 c3bd9c81e 的真实报告及源码。输入拒绝、预算限额、dont_ask 和单私聊分别是既有行为；本次调整明确拒绝语义与预算计量，保留后两者。

## Goals / Non-Goals

完成真实缺陷的反例、修复、回归与证据矩阵。不新增审批、多私聊、Agent DAG；不扩张 mailbox 的任务权限；不修改已发布 init。

## Decisions

- 并发采用显式拒绝；恢复输入及失败提示由客户端实现，不增加持久化队列和状态机。
- 交接只投影既有 peer_deliveries 对应的 AgentScope mailbox 正文；下一轮以 assistant Msg 内的 HintBlock 提供给 A（2.0.10 的 user Msg 只接受 text/data），不替 B 写答复或自动唤醒 A。读取不复制对话存储。投递 ID 直接作为 HintBlock ID 关联回复，不再用原文匹配；没有关联标识的历史交接不猜测回填，需重新投递。
- 提及先屏蔽代码与引用，再复用既有选择/有界 followup。自然同事问答保留，单人点名保持严格。
- tokens 字段继续记录 input+output 的实际计费量；仅生成 token 参与生成预算判断；时间、轮次、单次输出限制继续存在。
- 根据工具 state 判断成功汇总；不足一秒的已完成 thinking 使用真实运行状态选择文案，不用 0s 代替运行状态，不伪造 finished_at。
- Task 分类复用 TaskCategory 与原鉴权，类型在工具 schema 限定。邮箱同步时移除任务 MCP；普通私聊/群仍按可信绑定装载。
- SQLite 锁反例证明同步写锁等待阻塞了同一事件循环的异步 Task 提交；群调度的锁等待在线程中完成，持锁后立即提交短事务。取消时等待线程完成后才释放连接，不添加重试状态机。

## Risks / Trade-offs

拒绝模式不承诺连续输入自动排队。交接完成后刷新可读取，下一轮带回上下文；实时到达展示需实测。原始 DSML 根因须读取真实上游边界，不猜写私有协议解析器。生产 Worker/第三方 MCP 只在真实可用环境验证。
