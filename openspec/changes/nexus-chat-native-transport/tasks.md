## 1. 契约测试（先写）

- [x] 1.1 原生历史：完整 content 块、空 Session、非本人、上游错误
  - 完成标准：四条场景都有测试，非本人返回拒绝且响应体不含消息
  - 验证：`PYTHONPATH=services/nexus-api services/nexus-api/.venv/bin/pytest services/nexus-api/tests/test_chat_native.py`（10 passed）
- [x] 1.2 事件流：`raw=1` 不注入收尾事件、`REPLY_END` 后不关闭、默认模式行为不变
  - 完成标准：用假上游响应覆盖两种模式，断言默认模式仍有 `NEXUS_RUN_FINISHED`
- [x] 1.3 停止：归属校验、绑定缺失拒绝、跨 owner 不产生上游调用
- [ ] 1.4 发送的附件归属与超限仍由既有 `load_attachments` 覆盖；聊天岛目前只发文本
  - 完成标准：补一条“data 块 → attachment_ids”的测试

## 2. 后端

- [x] 2.1 `read_session` 返回运行态
  - 完成标准：`is_running` 来自上游响应，不从消息或时间推断
- [x] 2.2 新增原生历史路由
  - 完成标准：路由复用 `AgentScopeClient`，未绑定 Session 时返回空态
- [x] 2.3 `proxy_stream` 增加 `raw` 模式
  - 完成标准：`raw=1` 原样转发且不关闭；默认分支代码路径不变
- [x] 2.4 新增停止路由与客户端方法
  - 完成标准：先校验 owner 与绑定，再 `POST /sessions/{sid}/interrupt`

## 3. 前端聊天岛

- [x] 3.1 历史切换到原生接口
  - 完成标准：`useMessages`、`ASMessageBubble` 不改；只换 `src/shims/api.ts`
- [x] 3.2 发送与停止接入 Nexus
  - 完成标准：输入区可用；首次发送返回 Session 后更新绑定并重挂载；停止调用 interrupt
- [x] 3.3 单一消费者
  - 完成标准：`?island=1` 时旧适配器不订阅；切走会话释放连接
  - 验证：`cd web/nexus && npm run type-check && npm test`

## 4. 验收

- [ ] 4.1 真实模型的一轮对话（发送 → 流式 → 停止 → 刷新）
  - 阻塞：本机没有启用的模型路由，网关（18100）未运行，员工无法创建；旧 Session 在新 Redis 里也是 404
  - 已做：AgentScope 2.0.10 起服务并通过 `/health`，确认契约与鉴权可用
- [ ] 4.2 用假上游跑通“发送 → 绑定更新 → 原生历史重载”的浏览器链路
  - 完成标准：提交消息后聊天岛出现助手回复；停止按钮触发 interrupt
- [x] 4.3 回归既有测试
  - 验证：`PYTHONPATH=services/nexus-api ... pytest services/nexus-api/tests`（98 passed, 4 skipped）；
    `services/nexus-api/.venv/bin/ruff check services/nexus-api/nexus_api services/nexus-api/tests`；
    `cd web/nexus && npm run type-check && npm run build`
