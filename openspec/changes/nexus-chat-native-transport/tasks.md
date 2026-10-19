## 1. 契约测试（先写）

- [x] 1.1 原生历史：完整 content 块、空 Session、非本人、上游错误
  - 完成标准：四条场景都有测试，非本人返回拒绝且响应体不含消息
  - 验证：`PYTHONPATH=services/nexus-api services/nexus-api/.venv/bin/pytest services/nexus-api/tests/test_chat_native.py`（10 passed）
- [x] 1.2 事件流：`raw=1` 不注入收尾事件、`REPLY_END` 后不关闭、默认模式行为不变
  - 完成标准：用假上游响应覆盖两种模式，断言默认模式仍有 `NEXUS_RUN_FINISHED`
- [x] 1.3 停止：归属校验、绑定缺失拒绝、跨 owner 不产生上游调用
- [x] 1.4 附件：聊天岛先上传 `/api/v1/attachments`，DataBlock 的 URL source 携带附件 id，
      发送时还原成 `attachment_ids`；归属与 20 MB 限制仍由 Nexus 校验
  - 结果：浏览器选 md/png 各一次，上传 201，气泡显示文件卡/缩略图，模型读到内容

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

- [x] 4.1 真实模型的一轮对话（发送 → 流式 → 停止 → 刷新）
  - 结果：DeepSeek flash 两轮各约 4 秒；`stream?raw=1` 事件完整；停止后
    `is_running=false`、`finished_reason=interrupted`
  - 证据：`docs/reports/evidence/nexus-chat-island/acceptance-deepseek-20261020.png`
- [x] 4.2 浏览器内“输入 → 发送 → 流式 → 停止 → 刷新”
  - 结果：CDP 驱动真实 Chrome，Enter 发送后用户消息与助手回复都进入界面；
    流式期间出现停止按钮，点击后 4 秒内回到空闲
  - 证据：`live-send-20261020.png`、`stop-20261020.png`
- [x] 4.3 回归既有测试
  - 验证：`PYTHONPATH=services/nexus-api ... pytest services/nexus-api/tests`（98 passed, 4 skipped）；
    `services/nexus-api/.venv/bin/ruff check services/nexus-api/nexus_api services/nexus-api/tests`；
    `cd web/nexus && npm run type-check && npm run build`
