# 数字员工对话浏览器验收

## 实验条件

在 Apple M4、macOS（Darwin 25.5.0）、Node 22.23.3、Playwright 1.62.1 与 Chromium 151.0.7922.34 上，打开生产构建的 `/chat?employee=qa`。旧版从提交 `364fc5744456d0c4dd6996fd1fc482918db6ccb8` 单独归档后构建。每轮使用新的浏览器上下文和独立同源 HTTP 服务，固定 1440 × 900 视口与中文区域设置。

两版的服务端各有相同的 320 条私聊历史、95 条群聊历史。每次 API 响应增加约 15 ms 等待，提交后 700 ms 开始下发，连续发送 160 个文本增量，间隔 4 ms。私聊 GET 按产品请求的分页参数返回数据。执行发送、浏览历史、私聊切群聊、两员工接续发言、返回私聊及刷新，冷启和刷新都等待消息进入 DOM 后再等两个浏览器动画帧。浏览器用的是合成业务数据和真实生产前端构建，没有调用真实模型、Redis、AgentScope 或 Worker。

首条可见反馈取按下 Enter 到用户消息正文首次出现后的首个动画帧。输入指标取每轮 16 次键入中，input 到第二个动画帧的 P95。滚动测试以 16 个连续帧移动消息视口，记录最慢帧。高频事件期间另测距底部 350 px 处是否仍留在历史位置。脚本、布局与主线程任务耗时来自 Chromium Performance 域的窗口差值，DOM 变更数来自整个页面的 MutationObserver。均为浏览器观测近似值。

## 实测结果

2026 年 11 月 19 日，每版三个独立浏览器上下文。表中数值为三轮中位数。

| 指标 | v1.5.0 基线 | v1.6.0 |
| --- | ---: | ---: |
| 打开私聊至历史可见 | 350 ms | 299 ms |
| Enter 至已发送消息可见 | 73 ms | 32 ms |
| 首屏全页 DOM 元素 | 4,453 | 726 |
| 流式窗口内主线程任务耗时 | 892 ms | 251 ms |
| 流式窗口内脚本耗时 | 11 ms | 129 ms |
| 流式窗口内布局耗时 | 56 ms | 12 ms |
| 流式窗口内 DOM 变更记录 / 回调 | 90 / 64 | 68 / 31 |
| 初始历史输入响应 P95 | 19 ms | 28 ms |
| 加载至 322 条消息后的输入响应 P95 | 20 ms | 27 ms |
| 加载至 322 条消息后的滚动最慢帧 | 18 ms | 18 ms |
| 私聊切换到群聊历史可见 | 98 ms | 114 ms |
| 刷新后历史可见 | 271 ms | 171 ms |

三轮均确认在底部时跟随输出，上滚后保持阅读位置。每批增量只重新转换有变化的消息，历史消息沿用已有引用。新页面减少了初始 DOM、整体任务时间和布局开销。流式窗口内脚本耗时较基线高，输入延迟和切换群聊耗时也有所增加。这些数字适用于上述固定数据和网络条件。

[基线原始结果](evidence/nexus-chat-v160/before/results.json) · [重构后原始结果](evidence/nexus-chat-v160/after/results.json)

## 复现

上表保留 v1.5.0 的实测基线。当前夹具只模拟 v1.6.0 的接口与页面。在仓库根目录使用 Node 22.23.3，并准备 Playwright 1.62.1 及其 Chromium。可通过 `NEXUS_PLAYWRIGHT_MODULE` 指定该包的绝对 ESM 入口。

```sh
(cd services/nexus-api/chat-island && npm ci && npm run build)
(cd services/nexus-api/web && npm ci && npm run build)
node tools/e2e/nexus_conversation_benchmark.mjs services/nexus-api/web/dist /tmp/nexus-chat-after 3
node tools/e2e/nexus_conversation_regression.mjs services/nexus-api/web/dist /tmp/nexus-chat-after
```

## 功能回归

- 生产前端构建的 Chromium 夹具回归覆盖历史分页、连续 MCP 工具折叠、任务入口、等待确认、附件和图片、草稿隔离、刷新后幂等重试、已发送与等待反馈、断线恢复、停止、群撤权和空会话。虚拟麦克风验证录音和转写插入草稿。测试期间没有页面脚本异常。[十一项浏览器检查](evidence/nexus-chat-v160/after/regression.json)
- 隔离链路由 Nexus API、AgentScope 2.0.10、Redis 和两份独立 SQLite 组成。模型端使用固定 OpenAI SSE 响应。通过真实 `contact_employee`、邮箱投递及 wakeup 验证同事回复，实测两轮私聊、文本附件刷新恢复、群内两位员工及讨论终态。另用两个 Chromium 进程验证群邀请、人类互发、并发消息顺序和离群撤权。[私聊实录](evidence/nexus-chat-v160/native/private.png) · [群聊实录](evidence/nexus-chat-v160/native/group.png) · [双用户验收](evidence/nexus-chat-v160/native/multiuser.json)
- Nexus 两套后端测试 828 通过、22 跳过。另起仅监听临时 Unix socket 的 MySQL 8.0.36，原先按环境跳过的四项群聊 MySQL 测试全部通过。`ruff check app nexus_api tests` 通过。聊天岛 16 项、Vue 35 项前端测试通过。两套前端均通过类型检查和生产构建，OpenSpec 严格校验通过。

## 桌面截图

旧版截图使用 v1.5.0 实际 Vue 路由，员工正文有边框，工具日志默认折叠。新版将员工正文、工具摘要和工作状态放进时间线，展开后可读参数和输出。私聊工作摘要和群聊协作截图分别使用 1440 × 900 与 1280 × 720，正文和任务卡片使用合成数据。

[旧版私聊](evidence/nexus-chat-v160/before/01-private.png) · [旧版群聊](evidence/nexus-chat-v160/before/02-group.png) · [新版私聊](evidence/nexus-chat-v160/after/01-private.png) · [新版群聊](evidence/nexus-chat-v160/after/02-group.png)

[私聊工作摘要](evidence/nexus-chat-v160/after/04-work-summary.png) · [群聊协作](evidence/nexus-chat-v160/after/05-group-collaboration.png) · [等待响应](evidence/nexus-chat-v160/after/03-waiting.png) · [空会话](evidence/nexus-chat-v160/after/06-empty.png)

## 双专家评审补验

本节对比评审修复前后保存的两份 v1.6.0 生产构建。使用同一台 M4、Chromium 151.0.7922.34 和相同的私聊 320 条、群聊 95 条历史夹具。页面停在底部，统计消息视口内完整可见的消息行，部分可见的行不计入。正文均为 15 px，计算行高从 26.25 px 降至 23.25 px。头栏、输入区与段间距调整后，两种尺寸的消息视口均增加 21 px，均无横向溢出。

| 视口 | 私聊完整可见消息 | 群聊完整可见消息 |
| --- | ---: | ---: |
| 1440 × 900 | 3 → 5 | 7 → 10 |
| 1280 × 720 | 0 → 2 | 5 → 7 |

[私聊修复前](evidence/nexus-chat-v160/repair/before-private-1440.png) · [私聊修复后](evidence/nexus-chat-v160/repair/after-private-1440.png) · [群聊修复前](evidence/nexus-chat-v160/repair/before-group-1280.png) · [群聊修复后](evidence/nexus-chat-v160/repair/after-group-1280.png) · [密度测量记录](evidence/nexus-chat-v160/repair/density.json)

同网络夹具的三轮定时测试仍使用约 15 ms 的 API 等待、700 ms 的回复前等待和间隔 4 ms 的 160 次文本增量。六轮均在底部跟随输出，上滚后保持阅读位置。下表为各三轮的中位数，小幅差异以本次样本为限。

| 指标 | 修复前 | 修复后 |
| --- | ---: | ---: |
| 打开私聊至历史可见 | 288 ms | 290 ms |
| Enter 至已发送消息可见 | 34 ms | 32 ms |
| 流式窗口内主线程任务耗时 | 251 ms | 243 ms |
| 流式窗口内 DOM 变更记录 / 回调 | 69 / 32 | 65 / 30 |
| 加载至 322 条消息后的输入响应 P95 | 26 ms | 25 ms |
| 加载至 322 条消息后的滚动最慢帧 | 17 ms | 17 ms |
| 私聊切换到群聊历史可见 | 114 ms | 113 ms |
| 刷新后历史可见 | 170 ms | 171 ms |

[修复前定时结果](evidence/nexus-chat-v160/repair/before-benchmark.json) · [修复后定时结果](evidence/nexus-chat-v160/repair/after-benchmark.json)

- 私聊使用真实 AgentScope 2.0.10、Redis 和独立 SQLite，模型以 1,600 个文本增量复现长回复。浏览器未刷新即显示全部 4,814 个字符，与服务持久结果一致，且可继续发送。双用户群聊用同一隔离栈验证并发顺序、两位员工依次回复及离群撤权。工作者事件中的身份与随后落盘的员工消息逐一匹配。[长回复复现](evidence/nexus-chat-v160/repair/native-replay.json) · [群聊事件与双用户结果](evidence/nexus-chat-v160/repair/collaboration.json)
- Chromium 由原先 11 项扩展至 14 项回归，补验明确拒绝后的附件和草稿恢复、历史位置主动发送及键盘导航。各用例均无页面脚本异常。聊天岛 21 项、Vue 35 项通过，两包均完成类型检查和生产构建。后端 828 项通过、22 项跳过。真实 MySQL 8.0.36 单独跑通 4 项群聊测试。Ruff 校验通过。[14 项浏览器结果](evidence/nexus-chat-v160/repair/regression.json)

保留修复前构建的目录后，可按相同环境复测。以下 `baseline_dist` 是保留的修复前 v1.6.0 构建路径。

```sh
node tools/e2e/nexus_conversation_density.mjs "$baseline_dist" services/nexus-api/web/dist /tmp/nexus-chat-density
node tools/e2e/nexus_conversation_benchmark.mjs "$baseline_dist" /tmp/nexus-chat-repair-before 3
node tools/e2e/nexus_conversation_benchmark.mjs services/nexus-api/web/dist /tmp/nexus-chat-repair-after 3
```

## 单一契约复验

2026 年 11 月 21 日，隔离服务使用实际创建的私聊和群聊验证旧顶层 `text` 请求均返回 422。OpenAPI 只登记一种发送结构，群成员随消息快照返回，旧的认领和独立成员接口已下线。AgentScope 2.0.10、Redis 和 SQLite 链路通过两轮私聊、附件刷新恢复、员工协作、群邀请与离群撤权。

后端 828 项通过、22 项跳过，独立 MySQL 8.0.36 群聊测试 4 项通过。前端 56 项测试、14 项 Chromium 回归、类型检查和生产构建通过。长会话测试加载至 322 条消息，验证底部跟随、上滚保持、切换和刷新。模拟语音识别的浏览器检查验证了转写进入草稿、编辑后手动发送及回复展示。

## 验收边界

公司模型网关、实际 Worker 执行、现场部署和物理麦克风未参与本次隔离验收。数字员工对话当前只展示服务返回的人工请求。可执行的审批沿用现有任务入口。
