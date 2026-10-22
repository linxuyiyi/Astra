# Nexus v1.4.8 Push-to-Talk 语音输入验收记录

- 目标分支：`v1.4.8`（从 `v1.5.0` 的 ASR 提交合入，再提交 PTT 改动）
- 日期：2026-10-23
- 环境：Apple M4 / 16 GB / macOS 26.5.2 / Chromium（Playwright）
- 范围：数字员工对话输入框的按住说话快捷键。ASR 服务、Nexus 网关、SenseVoiceSmall 与 AgentScope 链路不变。

## 1. 交互实现

```text
按住 Option + K
  → 浏览器 getUserMedia + MediaRecorder
  → 松开 K 或 Option
  → POST /api/asr/transcribe（Nexus 统一服务）
  → Nexus 网关 /v1/audio/transcriptions
  → SenseVoiceSmall
  → 文字按光标插入当前会话输入框
  → 用户按 Enter 发送（不自动发送）
```

| 行为 | 实现 |
|---|---|
| 默认快捷键 | `Alt + K`；macOS 展示 `⌥ K` / `Option + K`，Windows/Linux 展示 `Alt + K` |
| 起录条件 | 完整组合键且输入/页面已获得焦点；单独 Alt、K、Space、Enter 不触发 |
| 结束 | 松开组合键任一必需部分；支持先松 K 或先松 Alt |
| Esc | 取消录音，不创建识别请求，释放麦克风 |
| 长按重复 keydown | `event.repeat` 与活动锁双保险，不重复创建 MediaRecorder |
| 输入法 | `isComposing`/229 与 compositionstart 期间不处理快捷键 |
| 失焦/页面隐藏/卸载 | 自动取消录音、释放麦克风；不提交 |
| 权限未完成即松开 | start token 失效，promise 返回后直接停止轨道，不延迟起录 |
| 识别失败 | 保留原输入、显示错误、可重试或重新录音 |
| 会话绑定 | 录音开始时记录 conversation/channel key；跨会话丢弃转写 |
| 防重复 | 一次录音对应一次 stop/一次上传；转写成功只插入一次 |
| 麦克风按钮 | 保留，和快捷键共用同一个 recorder 状态机 |

## 2. 快捷键配置

- 优先复用页面本地偏好，不新增服务端启动配置：`localStorage` key `nexus:asr:shortcut:v1`。
- 点击输入框旁 `⌥ K 按住说话` 打开轻量设置弹窗：
  - 按下新的组合键；
  - `恢复默认`；
  - 保存后立即生效；
  - 刷新页面后保持。
- 校验：
  - 必须包含 Alt/Option、Ctrl 或 Command；
  - 禁止 Space、Enter、Esc、Tab、Backspace、方向键等普通/导航键；
  - 拒绝已知系统/浏览器保留组合（如 `Command+Q`、`Command+Space`、`Alt+Tab`、`Ctrl+W`、`Alt+F4`）；
  - Nexus 现有输入框快捷键 Enter（发送）、Tab（补全）、方向键（确认卡）都属于禁止项。
- 不注册系统级全局快捷键，只在聊天岛页面生命周期内监听。

## 3. 改动文件

| 文件 | 改动 |
|---|---|
| `chat-island/src/lib/asrShortcut.ts` | 快捷键模型、默认值、格式化、事件解析、校验、localStorage |
| `chat-island/tests/asr-shortcut.test.ts` | 15 项单测（含默认值、平台展示、禁止键、释放顺序、持久化） |
| `chat-island/src/hooks/useAsrRecorder.ts` | 新增 `start/stop/cancel/source/requesting`；start token、权限未完成取消、会话绑定、转写取消、去重 |
| `chat-island/src/hooks/useAsrHotkey.ts` | 组合键 keydown/keyup、Esc、重复事件、IME、失焦/隐藏/卸载 |
| `chat-island/src/components/chat/AsrShortcutSettings.tsx` | 快捷键设置弹窗与捕获 |
| `chat-island/src/components/chat/TextInput.tsx` | 快捷键提示、录音/取消/加载状态、设置入口、快捷键接入 |
| `chat-island/src/i18n/locales/{zh,en}.json` | PTT 与设置文案 |
| `services/nexus-asr/scripts/e2e-ptt.mjs` | 真实浏览器 + ASR + AgentScope 验收脚本 |
| `docs/reviews/assets/nexus-v1.4.8-ptt/` | 截图与 `acceptance.json` |

## 4. 真实验证

执行：

```bash
cd services/nexus-asr
NEXUS_SCREENSHOT=../../docs/reviews/assets/nexus-v1.4.8-ptt/final.png \
NEXUS_REPORT=../../docs/reviews/assets/nexus-v1.4.8-ptt/acceptance.json \
node scripts/e2e-ptt.mjs
```

结果：**31 项检查全部通过，0 errors**。原始结果：[acceptance.json](assets/nexus-v1.4.8-ptt/acceptance.json)。

| 验收项 | 结果 |
|---|---|
| 默认提示 `⌥ K 按住说话` | 通过 |
| 单独 Space / K 不触发 | 通过 |
| 输入框聚焦时 Alt+K 起录 | 通过 |
| 松开 K 先结束 | 通过 |
| 松开 Alt 先结束 | 通过 |
| 重复 keydown 不重复上传 | 通过（1 次请求） |
| 输入框不出现 `˚` | 通过 |
| Esc 取消且不上传 | 通过 |
| 页面隐藏取消且不上传 | 通过 |
| 失焦取消且不上传 | 通过 |
| 快速按放不卡死/不重复 | 通过 |
| 真实 IME 组合输入不触发 | 通过（CDP `Input.imeSetComposition`） |
| 权限未完成即松开不延迟起录 | 通过（0 请求） |
| 权限被拒绝提示 | 通过（Web API 拒绝，0 请求） |
| 修改快捷键立即生效 | 通过（Alt+J） |
| 刷新后保持 | 通过 |
| 恢复默认 | 通过 |
| 原麦克风按钮仍可用 | 通过 |
| 语音文字进入输入框 | 通过 |
| 不自动发送 | 通过（消息数不变） |
| Enter 发送并获 AgentScope 回复 | 通过 |
| 单聊/群聊 | 均通过 |
| 识别中切换会话不串写 | 通过 |
| ASR 停止时保留草稿并提示 | 通过 |
| ASR 恢复后重试成功 | 通过 |

关键证据：

- [录音中状态](assets/nexus-v1.4.8-ptt/final-recording.png)：`录音中 00:00 · 松开结束 · Esc 取消`，红色停止按钮，快捷键提示保留。
- [快捷键设置](assets/nexus-v1.4.8-ptt/final-settings.png)：当前 `⌥ K` / `Option + K`，新组合键捕获区、恢复默认。
- [最终发送与回复](assets/nexus-v1.4.8-ptt/final.png)：语音文本“你好，小牛马，介绍一下你自己。”经 Enter 发送，DeepSeek 验收员工真实回复。
- [群聊 PTT](assets/nexus-v1.4.8-ptt/final-group.png)：`@阿明，` 后追加语音文本，未自动发送。

## 5. 回归

```text
chat-island npm test:                 15 passed
chat-island type-check/build:         通过
web type-check/build:                 通过
ASR pytest（链路未改）:               26 passed（此前基线）
```

PTT 使用已有 `/api/asr/transcribe` → 网关 → SenseVoiceSmall，不新增 ASR 服务，不绕过网关。

## 6. 未解决/限制

1. **真实硬件麦克风与 macOS 系统级 Option+K 未被自动化**：Chromium 使用 fake microphone；浏览器键盘事件、MediaRecorder、ASR 与 AgentScope 是真实的。需要在真机手动按一次 Option+K 复核系统/键盘布局差异。
2. **系统/浏览器保留组合无法强制捕获**：如 Command+Q、Command+Space、Alt+Tab 会被系统或浏览器优先处理；界面会拒绝这些已知组合并提示更换，但无法保证所有浏览器扩展的占用。
3. **群聊内容使用“阿明”**：当前员工库没有“小牛码”；`@` 路由规则已用真实群成员验证。
4. **快捷键只在聊天岛 iframe 页面内有效**：离开数字员工页面、切换标签或关闭页面后不生效，符合“不提供系统级全局快捷键”的约束。
5. **未做快捷键的服务端同步**：当前按需求使用轻量本地偏好；清理浏览器 localStorage 后会恢复默认。
6. **麦克风权限弹窗期间的按键行为依赖浏览器**：已在 Web API 层用延迟 promise 验证“权限未完成即松开不会延迟起录”；真实系统权限弹窗的焦点行为仍需真机确认。
7. **中英混合 ASR 准确率**沿用现有 FunASR 结果（混合术语仍有错误），PTT 不改变模型。
