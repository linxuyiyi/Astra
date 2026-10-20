# Nexus 聊天岛（阶段 A）

聊天区中间栏改由 React 子应用渲染，复用 AgentScope v2.0.10 的原生消息组件。
外层 Vue 页面（联系人/群组列表、顶部信息、右侧任务面板、底部导航）不变，
Element Plus 也不变——聊天岛在独立 iframe 文档里跑，Tailwind 的 preflight 不会污染全站。

## 状态

**阶段 B：私聊读写已接管。** 聊天岛读原生消息、订阅 `raw=1` 事件流、经 Nexus 发送与停止；
`?island=1` 时旧适配器不再订阅，避免双消费者。群组、附件、人工确认仍未迁移。

阶段 D 已收敛：聊天岛是默认渲染，`?island=1` 开关、旧消息列表/输入区、
旧 SSE 适配器与相关 CSS 都已删除。

切换与布局（2026-10-20）：A 联系人 → 阿明 → 切回 A，iframe 的 conversation 参数随之
变化，旧文档销毁即取消订阅，切回后历史完整；1440×900 与 1920×1080 下仍是三栏
（`shell-1920-20261020.png`）。

真实模型的一轮对话还没验：本机没有启用的模型路由，网关（18100）未运行，员工建不出来。

## 目录

```
web/nexus-chat/                 React 子应用（独立 npm 包）
  UPSTREAM.json                 vendored 版本、commit、被替换的模块
  UPSTREAM.sha256               逐文件哈希，用来检测与上游的漂移
  CHANGES.md                    与上游的差异
  src/components/               vendored：chat 组件 + 14 个 ui 原语 + markdown
  src/hooks/useMessages.ts      vendored：事件归并、历史游标、确认、停止
  src/i18n/                     vendored：useI18n + zh/en 文案
  src/index.css                 vendored：上游全局样式与主题变量
  src/shims/                    Nexus 实现，按模块替换上游的环境依赖
  src/nexus/                    岛入口、绑定、主题桥接、鉴权
  vite.config.ts                产物输出到 ../nexus/public/nexus-chat/
```

上游文件只在 `markdown/index.tsx` 改了一处（去掉 Mermaid 插件）。其余通过
Vite alias + tsconfig paths 替换模块，`useMessages.ts`、`ChatContent.tsx`、
`TextInput.tsx`、`ASMessageBubble.tsx` 与上游逐字节一致，升级时可直接比对。

## 构建

```bash
cd web/nexus-chat && npm install && npm run build   # 产出 web/nexus/public/nexus-chat/
cd web/nexus && npm run type-check && npm run build
```

产物（315 个文件、12 MB）**不入库**：`web/nexus/public/nexus-chat/` 在 `.gitignore` 里，
由 `ops/deploy/install_nexus.sh` 在 `web/nexus` 之前构建（聊天岛产物必须先进 public，
Vite 才会把它拷进 dist）。本地 `npm run dev` 前要先跑一次 `web/nexus-chat` 的 build。

聊天岛的改版必须走上面的构建，不要手改 `assets/*` 的哈希文件。

## 数据路径

```
Vue 聊天页 ──iframe──> /nexus-chat/index.html?conversation=<id>
聊天岛 ──> /api/v1/conversations/{id} + /api/v1/employees   （只读）
```

- 只接受 conversation id，agent / session 由后端数据推导；URL 里不能直接指定 AgentScope
  的 agent/session，避免改参数读别人的会话。
- 鉴权口径与 `web/nexus/src/api.ts` 一致：SSO 由 nginx 注入 `X-Remote-User`；没有 SSO 时
  回退到 localStorage 的 `nexus_owner_id`（`X-Owner-Id`）。
- 不使用设置页 iframe 那套 `server_url=/agentscope-api/` + `username=nexus` 的浏览器自称身份。

### 模块替换

| 上游模块 | Nexus 实现 | 说明 |
| --- | --- | --- |
| `@/api` | `src/shims/api.ts` | 历史读取；发送/停止/确认在阶段 A 直接拒绝 |
| `@/context/AudioContext` | `src/shims/AudioContext.tsx` | 不做语音播报，去掉 WebRTC / Web Audio |
| `@/components/dialog/WorkingDirectoryDialog` | `src/shims/WorkingDirectoryDialog.tsx` | 工作目录由建 Session 时固定 |
| `@/lib/browserWebRTCTransport` | `src/shims/browserWebRTCTransport.ts` | 只提供类型 |

## 主题

`src/nexus/theme.css` 直接 `@import` 了 `web/nexus/src/styles/tokens.css`，再把它映射到
shadcn 的语义变量（`--primary`、`--border`、`--muted`…），所以薄荷绿只有一份定义。
iframe 是独立文档，父级 CSS 不会进来，必须走这个桥接。

## 预览

```bash
cd web/nexus && npm run dev
# 打开 http://localhost:15174/chat?employee=<员工 id>
# 群组：http://localhost:15174/chat?group=<群组 id>
```

打开联系人时 Nexus 会先建会话，聊天岛随即接管；还没有 Session 时显示空态，
首条消息发送后由 Nexus 建 Session。

## 已验证 / 未验证

已验证：

- `web/nexus-chat`：`tsc --noEmit` 通过，`vite build` 通过（主包 1.9 MB / gzip 568 KB）。
- `web/nexus`：`npm run type-check`、`npm run build` 通过，`dist/nexus-chat/` 有产物。
- `npm test`：17 个既有测试通过。
- 无头 Chrome（1280×860）加载构建产物，用 mock 的 `/api/v1/conversations/{id}` 渲染一段
  含 Markdown、代码块、工具调用的历史：用户气泡右对齐、助手消息左对齐、代码高亮、
  工具卡折叠、时间分隔线、输入区禁用，均正常；无控制台异常。
- 鉴权失败时渲染错误态而不是白屏。
- 四个环境模块替换后，38 个 vendored 文件里只有 `components/markdown/index.tsx`
  与上游不同（`UPSTREAM.sha256` 逐文件比对确认）。

发现并修复的问题：

- 首次渲染白屏：radix 的 `TooltipProvider` 由官方 `App.tsx` 提供，聊天岛漏了，
  导致 `Tooltip must be used within TooltipProvider`。已在 `IslandApp` 补上。
- 无 `max-w-[48rem]` 容器时消息横向溢出被裁切。已按上游 `ChatViewport` 的容器复刻。

未验证（缺运行环境）：

- 真实会话的历史渲染。本机 AgentScope Agent Service（18000）未运行，`/api/v1/conversations/{id}`
  拿不到远端历史；mock 只能证明组件链可用，不等于真实数据正确。
- 浏览器内的视觉验收（字体、间距、气泡观感）尚未由人确认。构建产物里的确看到：
  CJK 插件会把中文按字切成 span 做淡入，历史消息打开时会逐字播放一遍。
  这是上游默认行为，本阶段没改；是否要关掉由视觉验收决定。
- 群组、发送、停止、确认、附件、HITL。这些属于阶段 B/C。

## 阶段 B 进度

- [x] 保真历史：`GET /api/v1/conversations/{id}/messages`（原生 `Msg[]` + 游标 + `is_running`）
- [x] 原生事件流：`stream?raw=1`，不注入收尾事件、不中途关闭
- [x] 停止：`POST /api/v1/conversations/{id}/interrupt`，先校验归属与绑定
- [x] 首次发送：Nexus 返回 Session 后更新绑定，`useMessages` 重挂载并加载原生历史
- [ ] 附件：`MessageBody` 仍是 `text + attachment_ids`，聊天岛目前只发文本；
      文件要先经 `/api/v1/attachments` 上传，再把 id 带进发送请求
- [ ] 人工确认：`UNATTENDED_PERMISSION = "dont_ask"`，确认卡在当前策略下不会出现

## 本地免费模型验收

没有可用模型 Key 时，可以用本机 Ollama 跑通完整链路（无需 Key、无外网费用）：

```bash
# 1. 模型（1.9 GB）
OLLAMA_MODELS=/tmp/ollama-models ollama serve &
OLLAMA_MODELS=/tmp/ollama-models ollama pull qwen2.5:3b

# 2. 网关数据面（18110）与 Task API（18100）
. ops/env/.env
(cd services/task-platform && .venv/bin/uvicorn app.main:app --port 18100 &)
(cd services/task-platform && .venv/bin/uvicorn app.gateway.server:app --port 18110 &)

# 3. 建池 / 节点 / 路由（base_url 不带 /v1，网关自己追加 /v1/chat/completions）
curl -X POST localhost:18100/api/gateway/pools -d '{"name":"本地 Ollama"}'
curl -X POST localhost:18100/api/gateway/nodes -d '{"name":"Ollama qwen2.5:3b","base_url":"http://127.0.0.1:11434","pool_id":"<pool>","auth_type":"NONE","models":["qwen2.5:3b"],"health_path":"/api/tags"}'
curl -X POST localhost:18100/api/gateway/routes -d '{"model":"qwen2.5:3b","pool_id":"<pool>","target_model":"qwen2.5:3b"}'
curl -X POST localhost:18100/api/gateway/nodes/<node>/probe -d '{"probe_types":["NETWORK","SERVICE","MODEL","STREAM"]}'
curl -X POST localhost:18100/api/gateway/nodes/<node>/activate

# 4. 网关 Key，然后把 Nexus API 指到网关
curl -X POST localhost:18100/api/gateway/keys -d '{"name":"本地验收","allowed_models":["qwen2.5:3b"]}'
export NEXUS_GATEWAY_API_KEY=<返回的 key>
export NEXUS_AGENTSCOPE_BASE_URL=http://127.0.0.1:8000   # 本地 dev 端口见 ops/env/.env
ops/dev/start_nexus_api.sh stop && ops/dev/start_nexus_api.sh start
```

验收记录（2026-10-20）：新建员工 → 首条发送建 Session → 原生历史返回
`hint / tool_call / tool_result / text` 四类块 → `stream?raw=1` 收到
`REPLY_START … REPLY_END` 且无 `NEXUS_RUN_FINISHED` → 聊天岛渲染两轮真实对话。
截图：`docs/reports/evidence/nexus-chat-island/acceptance-20261020.png`。

注意：`qwen2.5:3b` 只有 3B，会误调工具、偶尔不遵守中文指令，只适合验证链路，不适合验收回复质量。

### DeepSeek flash（推荐）

本地 3B 只适合验证链路。真实回复质量用 DeepSeek 官方 API：

- 节点 `base_url=https://api.deepseek.com`，`auth_type=BEARER`，`models=["deepseek-flash"]`，`health_path=/models`
- 路由 `model=deepseek-flash`，员工 `model_route_id` 选该路由
- Nexus API 需要 `NEXUS_GATEWAY_API_KEY`（网关 Key，`allowed_models` 要包含 `deepseek-flash`），
  以及 `NEXUS_AGENTSCOPE_BASE_URL` 指向本机 AgentScope（dev 默认 `8000`）
- Key 只写在 `ops/env/.env`（已被 gitignore），不要写进代码或提交

验收记录（2026-10-20）：DeepSeek flash 首轮 4 秒返回「收到」，原生历史为
`['hint', 'text']`；第二条「好的」同样直出。`stream?raw=1` 收到
`REPLY_START → TEXT_BLOCK_* → REPLY_END`，无私有事件、连接不关闭。
截图：`docs/reports/evidence/nexus-chat-island/acceptance-deepseek-20261020.png`。

### 浏览器交互验收（2026-10-20）

用 Chrome DevTools Protocol 驱动真实浏览器（无人工点击）：

- 在输入框写入文本并按 Enter → 用户消息立即出现（乐观插入），POST 返回后保持
- 同一会话连续多轮：助手回复通过 `stream?raw=1` 增量进入界面，无需刷新
- 流式期间出现停止按钮（0.5s 内），点击后 4s 内回到空闲；Nexus 侧
  `is_running=false`，该条助手消息 `finished_reason=interrupted`
- 刷新后原生历史完整渲染（含 hint、thinking、工具块与 token 用量）

证据：`live-send-20261020.png`、`stop-20261020.png`。

已知上游表现：被中断的 thinking 块若耗时为 0s，标头显示「思考中」（上游按
`elapsedText === '0s'` 选文案），块本身已结束、可展开查看部分内容；不是本项目的回归。

## 群组（阶段 C）

群组不是一条 Session，而是一个 channel + 每位员工各一条 Session：

- `GET /api/v1/channels/{id}/timeline/native` 按来源返回完整 `Msg` 与每位成员的
  `event_cursor`；成员 Session 里的人类消息副本按 `metadata.message_id` 剔除，
  只剩 Nexus 记录的那一条
- 前端为每位成员订阅 `members/{id}/stream?raw=1`，按 `session_id` 分桶合并；
  一次回复结束连接不关闭
- 停止按成员下发到 `POST /channels/{id}/members/{id}/interrupt`
- 发言人标题渲染在**气泡外**，不改写正文，也不改上游组件
- 发送仍走 `/api/v1/channels/{id}/messages`，`@` 路由与去重由后端负责

已知取舍：聊天岛接管群组后，旧 Vue 输入区的「点击 @成员」快捷按钮不再出现，
`@名字` 仍可手输；需要按钮的话要改上游 TextInput 暴露插入接口。

验收（2026-10-20）：

- 两位员工、两条 `@` 消息，回复按发言人分标题渲染（`group-20261020.png`）
- 停止：长回复期间 0.5s 出现停止按钮，点击后只有被点名的成员变为
  `finished_reason=interrupted`，其他成员不受影响
- 附件：`@成员 + 附件` 会把附件写进该成员 Session，模型能读出内容
- 网关瞬时 503 时成员回复会变成 `finished_reason=error`，界面显示「回复失败」；
  这是上游可用性问题，不是聊天岛的缺陷

## 长文本与长历史（2026-10-20）

**长文本（真实 DeepSeek flash）**：一次要求 1500 字，实际返回 3,106 字，
含 4 个二级标题、Markdown 表格、20 行 TypeScript 代码块和有序列表。

- 流式约 10 秒，滚动高度从 2,590px 增长到 4,479px，期间始终贴底（自动跟随）
- 标题、表格、代码块、行内代码、列表均正常；输入区在底部不被遮挡
- 滚到顶部后出现「回到底部」按钮，点击可回到最新消息
- 证据：`long-text-20261020.png`

**长历史（120 条合成消息，走同一套原生历史接口）**：

- 首屏内容 327 ms 出现；滚动高度 19,767px；DOM 节点 21,529 个
- 初始定位在底部；滚轮向上正常（`scrollTop` 19,132 → 17,934）
- 顶部渲染无空白块，`content-visibility` 没有造成缺块
- 证据：`long-history-20261020.png`

**风险**：原生历史接口一次返回全部消息，DOM 节点约 180 个/条；500 条以上
首屏与内存会明显变差。要支持超长会话，需要加分页（接口已有 `has_more` 位）
或虚拟滚动，目前未做。

## 上游升级

1. 在临时目录 checkout 新 tag，按 `UPSTREAM.sha256` 清单重新拷贝同名文件。
2. `sha256sum -c` 对不上的一律人工 review，更新 `CHANGES.md`。
3. `npm run build`，跑本文「已验证」里的三条命令。
