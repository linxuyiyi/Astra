## Context

现有 Task 保存 Worker、目录、原生 Session 指针及待接受输入；保留此模型。SSH 部署已有 Paramiko 边界，远端文件/诊断/工作区辅助可通过 SSH exec 复用。

## Goals / Non-Goals

目标：单一 SSH 连接池、loopback 转发、原生 HTTP/SSE、可恢复任务与隔离工作区。
非目标：历史迁移、双通道、内部步骤编排、新 Session 状态机。

## Decisions

- 每个 API 进程按 Worker 复用一条 SSH 连接和一个本地随机端口转发；远端端口持久化于 Worker，OpenCode 只监听 127.0.0.1。连接管理器关闭转发及 TCP channel，不停止远端进程。
- 所有原生请求使用 OpenCodeServerClient。文件、Git、诊断通过同一运行时 SSH 的 exec，复用现有受限远端辅助；部署保留短生命周期 SSH/SFTP 和远端 flock，避免运行时断线重连释放部署锁。移除自定义网络命令协议。
- 独立 Task MCP 只经既有内部密钥认证的 HTTP 业务入口调用 TaskTools；Celery 只领取任务，启动由 API 巡检负责。经验复盘通过原有 Task 原生工作区代理读取原生消息。只有 API 进程持有运行时 SSH。
- Task 是业务状态和 Worker 占用事实源，OpenCode 是原生 Session 事实源。SSE 唤醒与周期对账并存，无事件落库/游标/回放。对账失败保留状态，下一轮重试。
- 确认 Session 丢失进入 WAITING；未知提交先查询原生消息确认接受情况，串行重发且不停止已有运行。
- 失败仅由原生当前轮消息确认，不能根据 SSE error 或连接错误设置 FAILED。现有完成规则为人工确认。
- 清理未发布空库初始化中的旧通信结构；不增加历史迁移。维护锁与经验复盘租约属于各自业务，继续保留。

## Risks / Trade-offs

- 连接管理是 API 进程内资源，正式服务使用单 API 进程；多 Worker 可并发，单 Worker 仍独占任务。
- 当前环境若没有可用 SSH Worker/模型，则远程公司验收列为未验证；以可执行隔离集成补充。
