## Context

联系路由声明字符串字典却返回bool；群工具装配把共享Agent记录预算改为2；任务通知目前只投递AgentScope inbox，结束的群讨论不负责投影后续回复；技能声明summary但未注册。嵌入工作区仅按离线探测恢复，无法补读异常SSE重连期间的历史。

## Goals / Non-Goals

Goals：恢复协作与任务事实闭环，保留预算、归属校验和资源释放。
Non-Goals：不替员工编排任务内部步骤，不自动审核发布经验，不建立另一套Agent Memory或Task状态机，不猜测网络故障根因。

## Decisions

- 修正HTTP返回类型，并以完整路由测试覆盖提交后响应。群专用预算采用会话内副本，有限提升到足够多步操作；保持现有讨论限额；群规则明确结果交还通过contact_employee，公开陈述不等同于唤醒。
- Task/事件/outbox是任务通知事实源。内部通知只接受事件编号，服务端从Task的来源群和Session验证归属；以稳定消息ID写入既有有序群日志并去重，显示系统任务链接。群通知不自动开启员工推理或复用过期讨论授权；私聊仍走原生HintBlock唤醒。
- summary复用TaskTools归属校验、已有复盘Job和Knowledge记录，仅返回有界的业务字段与真实链接，不包含内部租约、source_snapshot或上下文凭证。
- 工作区适配层识别异常关闭后的SSE重连并重载已绑定Session，补读上游历史；主动abort不触发。复用工作区现有连接探测，在真实绑定事实变化时告知父页；任务页订阅已有状态事件及受校验的工作区就绪消息，刷新事实，不以时间或页面推算Runtime。

## Risks / Trade-offs

- 异常流重连会重建嵌入工作区，需排除主动abort并保留现有恢复节流，真实浏览器检验。
- Worker通道离线尚无确定根因；保留原始证据，只有独立反例成立才修改通道逻辑，不能把UI恢复当作网络已修复。
- 无schema变更；API、Agent Service、Task MCP、Celery Worker/Beat和前端构建需要一起更新QA栈才能真实复验。
