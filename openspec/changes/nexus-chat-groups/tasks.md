## 1. 后端

- [x] 1.1 群组原生时间线（按 stream 返回完整 Msg，人类消息只出现一次）
  - 验证：`test_chat_native.py` 覆盖人类去重、成员块保留、旧扁平响应不变
- [x] 1.2 成员流 raw 透传 + 每位成员的 `event_cursor`
- [x] 1.3 成员级停止（校验群组与成员绑定后转发）
  - 验证：`test_member_interrupt_reaches_the_member_session`

## 2. 前端

- [x] 2.1 群组视图：合并多 Session、气泡外标注发言人
- [x] 2.2 发送走 channel 入口，`@` 路由由后端解析；发送后重载并订阅新成员的流
- [x] 2.3 停止按成员下发，不影响其他成员
- [x] 2.4 `?island=1` 时群组也交给聊天岛，旧适配器不再订阅

## 3. 验收

- [x] 3.1 真实群组：两位员工、两条 `@` 消息，回复按发言人分标题渲染
  - 证据：`docs/reports/evidence/nexus-chat-island/group-20261020.png`
- [ ] 3.2 群组内停止的浏览器验收（接口与单元测试已覆盖）
