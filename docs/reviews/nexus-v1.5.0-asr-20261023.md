# Nexus v1.5.0 本地语音输入验收记录

- 分支：`v1.5.0`（基线 `a3f429e7c`，产品版本仍显示 v1.4.8；ASR 服务版本 1.1.0）
- 日期：2026-10-23
- 环境：Apple M4 / 16 GB / macOS 26.5.2 / Python 3.12.13
- 范围：SenseVoiceSmall 本地部署、FunASR 推理、Nexus 网关 ASR 路由、数字员工私聊/群聊麦克风输入。不含 TTS、Omni、实时流式 ASR。

## 1. 实现

```text
浏览器 chat-island
  getUserMedia + MediaRecorder（WebM/Opus）
    → POST /api/asr/transcribe（Nexus 统一服务，用户身份）
    → POST /v1/audio/transcriptions（Nexus 网关，Key/路由/节点）
    → POST /v1/audio/transcriptions（ASR 127.0.0.1:19899）
    → FunASR SenseVoiceSmall（CPU）
    → 文本回填输入框（不自动发送）
    → 用户确认 → 原 /api/v1/conversations|channels/.../messages
    → AgentScope + 现有 LLM
```

| 模块 | 改动 |
|---|---|
| `services/nexus-asr` | 默认 FunASR AutoModel；SenseVoiceSmall 固定模型目录；OpenAI 兼容 `/v1/audio/transcriptions`、`/v1/models`；标签清理；PyAV 解码 WebM；上传/时长/并发/限流/超时；冷启动预加载；`nexus-asr.sh` 启停/重启/状态/健康/日志；真实性能与 E2E 脚本 |
| `services/nexus-api/app/gateway` | 路由类型 CHAT/EMBEDDING/ASR；ASR 节点探测；`/v1/audio/transcriptions` multipart 解析与转发；ASR 4xx 不计节点失败；语音输入路由配置表 |
| `services/nexus-api/app/api/speech_routes.py` | 用户身份、读取 ASR 路由、12 MiB 限制、拒绝客户端 model、服务端 Key 调网关、标准化错误 |
| `nexus_api/model_route.py` | 模型路由类型校验（默认 CHAT；知识库 EMBEDDING） |
| `services/nexus-api/web` | 模型路由页增加 ASR 类型、ASR 探测、语音输入配置；员工模型下拉过滤 CHAT |
| `services/nexus-api/chat-island` | 麦克风按钮/录音状态/识别/错误/重试；结果按光标插入、不自动发送；错误码本地化；移除直连 ASR dev proxy |
| `ops/deploy/nginx-nexus.conf` | 删除 `/api/asr/*` 直连 ASR；`/api/` 13m、`/v1/` 20m |
| `ops/dev` | 新增 gateway 服务控制；修复 Task MCP 用 `python -c` 启动（cwd 进入 sys.path） |

## 2. 部署

| 项 | 值 |
|---|---|
| 模型 | ModelScope `iic/SenseVoiceSmall`，`model.pt` 936,291,369 B |
| 模型路径 | `/Users/niuniu/程序/Astra/services/nexus-asr/models/SenseVoiceSmall/` |
| VAD 缓存 | `.../models/modelscope/` |
| 虚拟环境 | `services/nexus-asr/.venv`，Python 3.12.13，FunASR 1.4.16 / torch 2.14.1 |
| 启动 | `services/nexus-asr/nexus-asr.sh start`（后台，pid/log 在 `.run/`） |
| ASR 地址 | `http://127.0.0.1:19899`（仅回环） |
| 网关 | `http://127.0.0.1:18110/v1`，节点 `Mac SenseVoiceSmall`，路由 `sensevoice-small`（ASR） |
| Nexus | `http://127.0.0.1:18120`，语音输入路由 `gr_d1d58671eced` |
| Task MCP | `http://127.0.0.1:8202/mcp`（本地开发端口，已启动） |

冷启动：模型加载 **5.92 s**，`nexus-asr.sh restart` 到 `/health` **7.82 s**。模型目录固定，正常启动不重复下载。

## 3. 端到端验收

### 测试用例 1：基本对话

- 音频：TTS“你好小牛码，介绍一下你自己。”（3.0 s 语音 + 2 s 静音）
- 识别：`你好，小牛马介绍一下你自己。 你。`（ASR 同音错误 + 假麦克风尾部伪影）
- 编辑：改为“你好小牛码，介绍一下你自己。（v1.5.0 最终验收）”
- 停止录音到回填：**1056 ms**（含浏览器上传/网关/ASR）
- 发送到界面显示回复：**2276 ms**
- 结果：AgentScope 真实回复，未自动发送，输入框可编辑。
- 截图：[私聊最终结果](assets/nexus-v1.5.0-asr/private-voice.png) / [回填未发送](assets/nexus-v1.5.0-asr/private-voice-transcript.png)

### 测试用例 2：业务对话（真实任务 MCP）

- 音频：“帮我查看一下当前有哪些正在执行的任务。”
- 员工：`E2E验收员工`（`emp_6f7a0cec2f89`，绑定 E2E任务MCP）
- 识别：`帮我查看一下当前有哪些正在执行的任务。 帮。`
- 停止录音到回填：**1260 ms**；发送到回复：**2287 ms**
- 结果：AgentScope 实际调用 `nexus_task_list`，返回 `{"ok": true, "tasks": []}`；回复明确说明这是该员工责任范围、当前无执行中任务，没有编造。
- 该 MCP 服务最初未启动，Agent 曾如实报告“连接失败”；启动 `ops/dev/start_task_mcp.sh` 后复验通过。
- 截图：[任务 MCP 语音结果](assets/nexus-v1.5.0-asr/business-voice-task-mcp.png)

### 测试用例 3：群聊

- 音频：“阿明，介绍一下你负责的工作。”
- 群：`验收群聊`（`ch_8236da560e51`）。**环境中没有名为“小牛码”的员工，使用群成员“阿明”完成等价 @ 路由验证。**
- 识别：`阿明介绍一下你负责的工作。 I mean.`（假麦克风尾部伪影）
- 编辑：`@阿明，介绍一下你负责的工作。（v1.5.0 群聊验收）`
- 停止录音到回填：**1482 ms**；发送到回复：**2313 ms**
- 结果：仅触发 `@阿明`，会话路由正确，AgentScope 真实回复；未自动发送。
- 截图：[群聊最终结果](assets/nexus-v1.5.0-asr/group-voice.png) / [回填未发送](assets/nexus-v1.5.0-asr/group-voice-transcript.png)

### 交互与异常

| 场景 | 结果 |
|---|---|
| 快速重复点击 | 1 次上传，回到 idle，提示“没有识别到语音”，无 pageerror |
| 识别中切换会话 | 1 次上传；新会话输入框为空，旧转写未泄漏 |
| 输入框已有草稿 | 识别结果插入草稿之后；值以“已有草稿：”开头，未覆盖原内容 |
| 麦克风权限拒绝（Web API 拒绝） | 0 次上传，显示“麦克风权限被拒绝，请在浏览器设置中允许后重试” |
| 无头 Chromium CDP 拒绝 | 不显示权限文案（Chromium 假设备限制，见未验证项） |
| ASR 停止 | 显示可重试错误；原草稿“原有草稿：”保留 |
| ASR 重启后重试 | 成功，文字追加到原草稿 |
| 空文件 | 直连 422 `empty_audio`；网关 400 `INVALID_REQUEST` |
| 非音频 | 415 `unsupported_audio` |
| 13 MiB | 413 |
| 静音/噪声 | 200 `{"text":""}` |
| 路由禁用/类型改 CHAT | Nexus 立即 422；网关约 10 s 后 404/400；恢复后 200 |
| 配置中心 | 路由页显示“语音识别”和“数字员工语音输入”；员工模型下拉仅 deepseek-flash / qwen2.5:3b，无 ASR |

配置中心证据：[路由与语音输入](assets/nexus-v1.5.0-asr/gateway-routes.png) / [员工模型下拉过滤](assets/nexus-v1.5.0-asr/employee-model-filter.png)。

## 4. 性能（真实数据）

完整报告：[`services/nexus-asr/docs/performance-report.md`](../../services/nexus-asr/docs/performance-report.md)，原始 JSON：[`perf-v1.5.0.json`](../../services/nexus-asr/results/perf-v1.5.0.json)。

| 场景 | 音频 | 直连 ASR 总耗时 mean/min/max | RTF | CER | 网关额外耗时 p50 |
|---|---:|---:|---:|---:|---:|
| 短语音（10 次） | 2.92 s | 451.9 / 443.2 / 477.5 ms | 0.154 | 0.0% | 10.77 ms |
| 普通对话（10 次） | 5.03 s | 536.4 / 485.2 / 673.3 ms | 0.106 | 4.2% | 10.67 ms |
| 普通对话 | 9.93 s | 604.6 ms | 0.060 | 4.6% | 10.59 ms |
| 较长输入 | 28.83 s | 1109.7 ms | 0.038 | 6.1% | 12.72 ms |
| 中英混合 1 | 9.66 s | 662.2 ms | 0.068 | 20.0% | 11.13 ms |
| 中英混合 2 | 10.20 s | 659.8 ms | 0.064 | 13.5% | 11.66 ms |
| 业务术语 | 4.96 s | 500.7 ms | 0.100 | 0.0% | 10.44 ms |

- 进程 RSS **1.07–1.14 GB**；短语音/5 s CPU 平均 94.4% / 98.6%，峰值 141.3% / 139.3%（单核百分比）。
- 网关额外耗时稳定 **10–14 ms**，无成功率和超时退化；语音转写 4xx 不再触发节点熔断。
- 短语音和普通对话各 10 次热启动，全部成功。

## 5. 回归

```text
ASR pytest:                              26 passed
Nexus 聚焦回归（gateway/speech/storage/nexus/task routing）: 315 passed, 8 skipped
chat-island npm test:                    10 passed
web npm test:                            30 passed
chat-island type-check/build:            通过
web type-check/build:                    通过
ruff check app nexus_api tests:          通过
git diff --check:                        通过
```

全量 `tests/platform tests/nexus` 在本机出现既有环境相关失败（本地 `ops/env/.env` 覆盖 installer、macOS 上远程安装脚本用例），并在 `test_worker_agent_files.py::test_external_final_symlink...` 触发 macOS `SIGSYS` 崩溃；这些路径与本次语音改动无关，已用聚焦回归覆盖相关模块。详见最终遗留问题。

## 6. 未验证/遗留

1. **真实硬件麦克风权限弹窗**未自动化。无头 Chromium 的假设备无法稳定产生 `NotAllowedError`（去掉 fake UI 时返回 `NotSupportedError`）。已用浏览器 Web API 拒绝验证 UI 分支，并在报告中明确标注，不把它当作硬件权限实测。
2. **群聊音频内容**使用“@阿明”，因为当前环境没有“小牛码”员工；群聊 @ 路由本身已验证。如需完全复现原文，需先创建“小牛码”员工和群成员。
3. **Linux ARM64** 未实机部署；FunASR/PyTorch wheel、模型缓存和目标机性能待验证。
4. **中英混合术语**仍有明显错误（20.0% / 13.5% CER）；输入框可编辑 + 人工确认发送是当前兜底。
5. **假麦克风音频**在句尾可能产生重复/截断伪影（`你。`、`帮。`、`I mean.`），这是 Chromium fake audio 循环/边界效应，不是生产硬件录音行为；验收不依赖该伪影。
6. **全量测试**受本机 macOS 环境和既有测试隔离问题影响，未获得单次全绿；聚焦回归全部通过。
