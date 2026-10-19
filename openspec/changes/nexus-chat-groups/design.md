## Context

群组由 Nexus 的 channel 与每个成员各自的 AgentScope Session 组成；人类消息同时写入
各成员 Session 作为上下文。旧 `/timeline` 用 `merge_timeline` 压成人类/回复/同事提示三类文本。

## Decisions

**D1 新增 `/timeline/native`，不改旧 `/timeline`。** 旧响应仍是旧页面的契约；
原生响应按 stream 返回完整 `Msg`，并带每位成员的 `event_cursor`。

**D2 人类消息以 Nexus 记录为准。** 成员 Session 里的副本（`metadata.message_id` 相同）
在原生时间线里剔除，避免同一条消息出现多次。

**D3 每位成员一条 raw 订阅。** 复用 `raw=1` 的透传语义，前端按 `session_id` 分桶；
停止按成员发到 `/members/{id}/interrupt`。

**D4 发言人标注在气泡外。** 上游气泡不显示姓名，群组视图在气泡上方渲染员工名，
不改写消息正文，也不改 vendored 组件。

**D5 不用 Agent Team。** 群组是平等成员，`contact_employee` 与 `@` 路由保持不变。

## Rejected Alternatives

- 把群组合并成一条 Session：丢失成员归属，且与 Nexus 的多 Session 模型冲突。
- 在前端轮询整条时间线代替事件流：延迟不可控，且重复拉取完整历史。
