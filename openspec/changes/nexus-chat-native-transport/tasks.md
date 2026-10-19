## 1. 契约测试（先写）

- [x] 1.1 原生历史：完整 content 块、空 Session、非本人、运行态
  - 完成标准：四条场景都有测试，非本人返回拒绝且响应体不含消息
  - 验证：`PYTHONPATH=services/nexus-api services/nexus-api/.venv/bin/pytest services/nexus-api/tests/test_chat_native.py`
- [x] 1.2 事件流：`raw=1` 不注入收尾事件、`REPLY_END` 后不关闭、默认模式行为不变
  - 完成标准：用假上游响应覆盖两种模式，断言默认模式仍有 `NEXUS_RUN_FINISHED`
- [ ] 1.3 发送与停止：`request_id` 回显、附件归属、跨 owner 拒绝
  - 完成标准：跨 owner 的停止请求不产生上游调用

## 2. 后端

- [x] 2.1 `read_session` 返回运行态
  - 完成标准：`is_running` 来自上游响应，不从消息或时间推断
  - 验证：单元测试覆盖“有回复在跑”和“空闲”两种响应
- [x] 2.2 新增原生历史路由
  - 完成标准：路由复用 `AgentScopeClient`，不新建代理客户端；未绑定 Session 时返回空态
- [x] 2.3 `proxy_stream` 增加 `raw` 模式
  - 完成标准：`raw=1` 原样转发且不关闭；默认分支代码路径不变
- [ ] 2.4 新增停止路由与客户端方法
  - 完成标准：先校验 owner 与绑定，再 `POST /sessions/{sid}/interrupt`
- [ ] 2.5 `MessageBody.request_id` 可选回显
  - 完成标准：旧客户端不传也能工作，响应逐字回显传入值

## 3. 前端聊天岛

- [ ] 3.1 历史切换到原生接口
  - 完成标准：`useMessages`、`ASMessageBubble` 不改；只换 `src/shims/api.ts`
  - 验证：`cd web/nexus-chat && npm run build`
- [ ] 3.2 发送与停止接入 Nexus
  - 完成标准：输入区解除禁用；首次发送后按返回绑定重挂载；停止调用 interrupt
- [ ] 3.3 乐观消息按 `request_id` 对账
  - 完成标准：重试同一请求不出现重复用户消息
- [ ] 3.4 单一消费者
  - 完成标准：`?island=1` 时旧适配器不订阅；切走会话释放连接
  - 验证：`cd web/nexus && npm run type-check && npm test`

## 4. 验收

- [ ] 4.1 本地起 AgentScope 2.0.10，用一条新建私聊会话完成首轮发送、流式渲染、刷新历史、停止
  - 完成标准：四条都通过；连续两轮回复都在同一条订阅上收到
  - 验证：记录请求/事件日志与截图；本机旧 Session 重启后已失效，必须先建新会话
- [ ] 4.2 回归旧聊天页
  - 完成标准：不带 `?island=1` 时旧渲染的发送、流式、任务刷新不变
  - 验证：`PYTHONPATH=services/nexus-api services/nexus-api/.venv/bin/pytest services/nexus-api/tests`；
    `cd web/nexus && npm run build`
- [ ] 4.3 记录无法验证项
  - 完成标准：报告里写明哪些场景缺真实环境（如 `dont_ask` 下的确认卡、群组）
