# Nexus 语音输入集成

Nexus v1.5.0 的语音输入链路：

```text
services/nexus-api/chat-island  (React 输入框)
  └── useAsrRecorder: getUserMedia + MediaRecorder(WebM/Opus)
        │  POST /api/asr/transcribe          (同源，浏览器不持有网关 Key)
        ▼
services/nexus-api  app/api/speech_routes.py
  ├── 校验用户身份（X-Verified-UID / X-Owner-Id / nexus_uid）
  ├── 读取配置的语音路由，校验 model_type=ASR + 启用 + 资源池启用
  ├── 拒绝客户端覆盖 model；限制 12 MiB；只允许单个 file
  └── 用服务端稳定 Key 调用网关
        │  POST /v1/audio/transcriptions
        ▼
Nexus Gateway（app.gateway.server，18110）
  ├── Gateway Key 鉴权、模型授权、吊销/额度、并发租约
  ├── route.model_type == ASR 校验、请求模型 → 目标模型
  ├── 节点健康/调度、超时、熔断、请求日志
  └── multipart 解析/重建，有界转发
        │  POST http://127.0.0.1:19899/v1/audio/transcriptions
        ▼
Nexus ASR（FunASR SenseVoiceSmall，127.0.0.1:19899）
  └── {"text": "..."}；标签清理；不落盘、不记录音频/文本
```

浏览器只认识 Nexus 的同源 `/api/asr/transcribe`；不存在直连 ASR 的 Nginx location 或前端回退。

## 1. 配置中心

打开 **API 网关 → 模型路由**：

1. **上游节点**：Base URL 填 ASR 服务根地址（本机 `http://127.0.0.1:19899`），探测策略选 **语音识别（ASR）**，模型必须包含 ASR 实际模型名，例如 `sensevoice-small`。
2. 探测到 `HEALTHY` 后启用节点并进入调度；首次探测需要达到成功连击阈值，必要时再点一次探测。
3. **资源池**只放 ASR 节点。
4. **模型路由**：请求模型 `sensevoice-small`、类型 **语音识别（ASR）**、目标模型 `sensevoice-small`、启用。
5. 页面顶部 **数字员工语音输入** 选择该路由并保存。私聊/群聊共用。

保存后：

- Nexus 的 `/api/asr/transcribe` 立即读取数据库中的路由，禁用/类型错误会立刻返回 422。
- 网关配置约 10 s 轮询一次；节点地址、路由类型/启用状态修改后需要一个轮询周期生效。
- ASR 路由不会进入数字员工/Worker 的 LLM 模型下拉列表（前端按 `model_type === 'CHAT'` 过滤，后端 `require_enabled_route` 同样默认 CHAT）。
- 地址和凭据只在网关节点维护；语音输入配置只保存 `route_id`。

如果网关不在本机：节点 Base URL 改成网关能访问的 Mac 内网地址，并让 ASR 监听该地址/端口或通过受控反向代理访问；不要使用 `127.0.0.1` 指向另一台机器。前端仍然只调用 Nexus。

## 2. 前端交互

麦克风在 `chat-island/src/components/chat/TextInput.tsx`，录音/上传状态机在 `useAsrRecorder.ts`：

```text
idle ──点击──> recording ──点击/60 s──> transcribing ──成功──> idle（文字插入光标）
  ▲                 │                        │
  └──── 失败/重试 ───┴──────── error <───────┘
```

- 录音中显示“录音中 00:05”，再次点击停止；停止后立即释放麦克风轨道。
- 录音中/识别中禁用重复提交；`transcribing` 时按钮显示 spinner。
- 识别成功只把文字插入当前光标位置，**不自动发送**；原有草稿保留。
- 失败显示中文错误和“重试”；重试复用上一段录音，不覆盖输入框。
- 单聊和群聊复用同一组件；群聊发送仍走原有 `@` 路由规则。
- 识别结果发送后走原 `/api/v1/conversations/{id}/messages` 或 `/api/v1/channels/{id}/messages`，AgentScope 只看到普通文本。
- 麦克风权限被拒绝/没有设备/浏览器不支持分别给出明确提示；权限拒绝时不上传。
- 需要安全上下文：`https://` 或 `http://localhost` / `http://127.0.0.1`。通过普通 HTTP 访问远程 IP 时浏览器不会提供 `getUserMedia`。

### Push-to-Talk

麦克风按钮旁显示当前快捷键（默认 `⌥ K 按住说话`），点击可修改：

- 按住完整组合键开始录音，松开任意必需键结束；Esc 取消且不上传。
- 只接受含 Alt/Option、Ctrl 或 Command 的组合；Space/Enter/Esc/Tab 和系统保留组合会被拒绝。
- 偏好保存在 iframe 源下的 `localStorage`（`nexus:asr:shortcut:v1`），保存后立即生效、刷新保持；没有系统级全局快捷键。
- 快捷键和麦克风按钮共用同一个 recorder；转写仍插入光标、不自动发送。
- 录音时绑定 conversation/channel；切换会话不会把结果写入新会话。

## 3. 错误码

ASR 4xx 的标准化 `code` 会由 Nexus 转发给前端，前端优先使用本地化文案：

| code | 前端提示 |
|---|---|
| `empty_audio` | 没有识别到语音 |
| `unsupported_audio` | 音频格式无法识别 |
| `audio_too_long` | 录音超过 60 秒 |
| `payload_too_large` | 录音文件超过大小限制 |
| `recognition_timeout` | 识别超时，请重试 |
| `model_not_ready` | 语音服务尚未就绪 |
| `server_busy` / `rate_limited` | 服务繁忙 / 请求太频繁 |
| `gateway_asr_failed` | 语音识别网关调用失败（5xx/鉴权等，不透出上游细节） |

## 4. 安全

- 浏览器不保存上游 Key；Key 只在 Nexus 网关数据库加密保存。
- 客户端不能提供上游 URL、节点地址或任意 model；`/api/asr/transcribe` 只接受一个 `file`，其余字段拒绝。
- 音频不持久化；ASR 只在内存中解码，日志不含音频或转写文本。
- 上传 12 MiB、音频 60 s、并发 2、请求超时 60 s、进程级限流 120 r/min；网关层另有大小、超时、配额与熔断。
- ASR 默认仅监听 `127.0.0.1`；公网访问必须经 Nexus 登录鉴权与网关。
- 远程 HTTP 下麦克风不可用，这是浏览器安全上下文限制，不能靠前端绕过。

## 5. 验证

```bash
cd services/nexus-asr
./scripts/verify-deployment.sh

# 真实浏览器：私聊
NEXUS_EMPLOYEE_ID=emp_xxx NEXUS_AUDIO=/tmp/case1-48k.wav \
  NEXUS_FINAL_TEXT='你好小牛码，介绍一下你自己。' \
  node scripts/e2e-voice.mjs

# 真实浏览器：群聊
MODE=group NEXUS_CHANNEL_ID=ch_xxx NEXUS_AUDIO=/tmp/case3-48k.wav \
  NEXUS_FINAL_TEXT='@阿明，介绍一下你负责的工作。' \
  node scripts/e2e-voice.mjs

# 快速点击、识别中切会话、权限拒绝、ASR 宕机与重试
node scripts/interaction-tests.mjs

# Push-to-Talk 全量浏览器验收（组合键、IME、失焦、权限、改键、发送）
NEXUS_EMPLOYEE_ID=emp_xxx NEXUS_CHANNEL_ID=ch_xxx node scripts/e2e-ptt.mjs
```

2026-10-23 在本机实测：私聊/群聊语音识别、手动编辑发送、AgentScope 回复均成功；识别文字从停止录音到回填约 0.5–0.8 s，发送到界面显示回复约 2.2 s。详细记录见 [`docs/reviews/nexus-v1.5.0-asr-20261023.md`](../../../docs/reviews/nexus-v1.5.0-asr-20261023.md)。Push-to-Talk 的 31 项浏览器验收见 [`docs/reviews/nexus-v1.4.8-ptt-20261023.md`](../../../docs/reviews/nexus-v1.4.8-ptt-20261023.md)。
