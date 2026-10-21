# 与上游 v2.0.10 的差异

基线：`agentscope-ai/agentscope` `v2.0.10`，commit `e6c014f0be49b2a6f6f62a9453082a0b975484e1`，
目录 `examples/web_ui/frontend`。逐文件哈希见 `UPSTREAM.sha256`。

## 文件级改动

- `src/components/markdown/index.tsx`
  移除了 `@streamdown/mermaid`。聊天区不渲染 Mermaid 图，去掉后少一个插件依赖。
  其余逻辑与上游一致。

## 上游文件未改动，但模块被替换（Vite alias + tsconfig paths）

| 上游模块 | Nexus 实现 | 原因 |
| --- | --- | --- |
| `@/api` | `src/shims/api.ts` | 上游走 `/sessions`、`/chat`；Nexus 的会话绑定与鉴权在后端，不能让浏览器直连 |
| `@/context/AudioContext` | `src/shims/AudioContext.tsx` | Nexus 不做语音播报，去掉 WebRTC / Web Audio |
| `@/components/dialog/WorkingDirectoryDialog` | `src/shims/WorkingDirectoryDialog.tsx` | 工作目录由建 Session 时固定，聊天区不提供修改入口 |
| `@/lib/browserWebRTCTransport` | `src/shims/browserWebRTCTransport.ts` | 只提供 `RealtimeConnectionState` 类型 |

`TextInput.tsx` 已包含 Nexus 本地 ASR 与输入能力适配，不能再按上游逐字节一致处理。
2026-10-21 的页面能力审查进一步修正纯附件发送、无回调的实时语音按钮、ASR
就绪探测、按钮可访问名称，以及按 conversation/channel 隔离的标签会话草稿恢复。
上游升级时需保留这些适配，并对照 `tests/drafts.test.ts` 与浏览器验收报告。

## 未搬的部分

`src/api/**`、`src/pages/**`、`src/components/panel/**`、`src/components/dialog/**`、
语音/实时相关模块、官方侧栏与模型配置都未进入本包。它们属于官方完整应用，不是聊天区所需。
