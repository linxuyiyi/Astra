# 2026-10-30 同文档聊天重构场景复核

核对候选 6402ef94c 与首页基线 9743a09cb 的集成实现。现有任务清单仍为 tasks.md，第 6 节是本次范围。阶段 1–5 的历史模型验收不算本次真实模型证据。

| Requirement / Scenario | 当前实现与证据 | 结论 |
|---|---|---|
| Native history / 内容、空 Session、owner、运行态 | nativeMessage.ts + adapters.ts；test_chat_native.py、test_api.py；projection 4 项；浏览器快回复、附件、thinking、工具错误 | 通过代码/受控验证 |
| Long-lived stream / 两轮与游标 | api.readEventStream、PrivateChatAdapter；transport 回归；浏览器同一私聊连续两轮与刷新 | 通过代码/受控验证 |
| Legacy stream unchanged | 后端 proxy raw/default 分支未改；旧 chat-island 17 项与构建通过；test_chat_native.py | 通过回归 |
| Nexus send / 绑定、附件归属 | 沿用 Nexus 发送与上传；test_api.py、test_chat_native.py；浏览器 Markdown/PNG 上传与正文预览 | 通过代码/受控验证 |
| Stop / 正确 Session、owner、权限策略 | 原后端中断路径；浏览器停止失败重试、群聊中断；无本地 resolveHitl 或超时宣布终态 | 通过代码/受控验证 |
| Single consumer / 切换、迟到绑定 | Runtime suspend/resume、isActive、abort；8 项 runtime 回归；浏览器上游连接 1→0 | 通过代码/受控验证 |
| Group contract / sender、event、client_id、read、before | 单一 events 流；transport 单元；test_collaboration/test_groups；55 条浏览器分页、两员工响应、关联任务跳转 | 通过代码/受控验证 |
| Fast reply / 相同毫秒顺序、迟到历史 | sessionState 微秒排序；private replay_cursor 防旧覆盖；修复前反例失败、修复后通过 | 通过回归与浏览器 |
| Unexpected confirmation | readonly HitlCard，dont_ask 保持；浏览器 asking 无允许/拒绝按钮，仍可停止 | 通过受控验证；确认通道未实现 |
| Narrow viewport | chat 专属视口样式及可关闭抽屉；390×844 输入可见、无横向溢出、pageScroll=0 | 通过浏览器 |

`openspec validate nexus-chat-native-transport --strict` 通过。当前仓库/可用技能未提供可执行的 /opsx-verify；本文件记录逐 Scenario 核对，不把 strict validate 当作 E2E。

真实环境 18120 的 AgentScope reachable=false。模型、实际任务工具/Worker、远端 Linux、ASR 麦克风和生产稳定性仍未验收，不据此归档为完成真实运行验收。完整结果与截图：docs/reviews/nexus-chat-window-review-20261030.md。


## 后续真实部署补验（2026-10-30）

在既有 18520 本机环境部署后，AgentScope 2.0.10 reachable=true。真实私聊、停止、B/C 群聊、附件单发、Task MCP→Linux Worker→业务确认→资源释放，以及 API 重启后的历史与新推理均已验证。ASR 恢复后通过真实本地识别。完整证据见 docs/reviews/nexus-v149-full-deploy-20261030.md。

前述18120不可达和受控上游报告是此前环境的历史事实，不再作为本次真实补验的结论；公司 MySQL / 长稳、12条历史缺失 Agent 绑定与可选 Ollama 不可用仍未解决，不将其计为通过。
