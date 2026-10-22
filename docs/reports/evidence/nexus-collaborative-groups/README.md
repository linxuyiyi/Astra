# 群协作真实浏览器验收（2026-10-23）

两个独立 Chromium 进程、两名新注册人类用户、真实 Nexus API / Agent Service 2.0.10 / 网关模型。

- 入口：`NEXUS_E2E_URL=http://127.0.0.1:18240`
- 脚本：`tools/e2e/nexus_group_collaboration.mjs`
- 运行方式：`NEXUS_E2E_OWNER_REGISTER=1` 让群主与受邀者都使用独立账号，可重复执行。
- 结果：`results.json`，`browser-run.log` 为同次运行的标准输出。
- 截图：
  - `owner.png`：群主视角，人类消息与员工讨论。
  - `colleague.png`：受邀者视角，只有自己的消息靠右。
  - `discussion.png`：两名数字员工按真实公开上下文连续交流。

本次验证覆盖：建群、邀请链接加入、两名人类实时互见、消息去重与并发顺序、当前用户靠右/他人靠左、员工按 @ 顺序发言、后发言者读取前序真实回复、退出后立即失去历史读取权限；浏览器无 page error。
