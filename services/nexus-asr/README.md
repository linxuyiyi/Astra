# Nexus ASR Server

Mac 本地 **SenseVoiceSmall + FunASR** 语音识别服务。浏览器录音经 Nexus API 与 Nexus 网关转发到这里，CPU 推理，不依赖 GPU、云端 ASR 或 AgentScope 语音能力。

```text
浏览器 MediaRecorder
  → POST /api/asr/transcribe        (Nexus 统一服务，用户身份)
  → POST /v1/audio/transcriptions   (Nexus 网关，Key + 路由 + 节点)
  → POST /v1/audio/transcriptions   (本服务，127.0.0.1:19899)
  → SenseVoiceSmall (FunASR / PyTorch CPU)
  → {"text": "..."} → 回填输入框
```

音频只在内存中解码，不落盘；日志记录请求 ID、字节数、时长、耗时和状态，不记录音频或识别文本。

## 当前部署（Mac M4）

| 项 | 值 |
|---|---|
| 模型 | SenseVoiceSmall（ModelScope `iic/SenseVoiceSmall`） |
| 推理框架 | FunASR 1.4.16 + PyTorch 2.14.1，CPU |
| 模型目录 | `services/nexus-asr/models/SenseVoiceSmall/`（约 936 MB） |
| VAD 缓存 | `services/nexus-asr/models/modelscope/` |
| 服务地址 | `http://127.0.0.1:19899`（只监听回环） |
| 模型 ID | `sensevoice-small`（兼容别名 `sensevoice`、`sensevoice-small-int8`、`whisper-1`） |
| 版本 | 1.1.0 |

> 旧版 sherpa-onnx INT8 实现仍保留为可选 `provider: sherpa-onnx`，用于低内存机器；默认和本次验收均使用 FunASR。

## 快速开始

前置：macOS ARM64、Python 3.12（脚本可用 `uv` 自动准备）。

```bash
cd services/nexus-asr

./setup-mac.sh          # 创建 .venv 并安装 FunASR/PyTorch 依赖
./download-model.sh     # 下载 936 MB 模型到固定目录
./nexus-asr.sh start    # 启动并等待 /health
```

`start-mac.sh` 等价于 `./nexus-asr.sh start`。

```bash
./nexus-asr.sh status
./nexus-asr.sh health
./nexus-asr.sh logs          # tail -f .run/nexus-asr.log
./nexus-asr.sh restart
./nexus-asr.sh stop
```

打开 <http://127.0.0.1:19899/> 是独立测试页：录音、上传、查看时长/耗时/RTF。它不是数字员工聊天入口；正式入口在 Nexus 聊天输入框的麦克风按钮。

## API

### 健康与模型

```bash
curl http://127.0.0.1:19899/health
curl http://127.0.0.1:19899/v1/models
curl http://127.0.0.1:19899/api/asr/models
```

### OpenAI 兼容转写

```bash
curl -X POST \
  -F "file=@testdata/zh-5s.wav" \
  -F "model=sensevoice" \
  http://127.0.0.1:19899/v1/audio/transcriptions
```

```json
{"text": "帮我把今天的会议纪要整理一下，重点列出三个代办事项。"}
```

- 支持 WAV、WebM/Opus（Chrome MediaRecorder）、MP4/AAC 等，服务端用 PyAV 统一解码为单声道 16 kHz。
- 只支持 `response_format=json` 和非流式；`stream=true` 会被拒绝。
- 响应头带 `x-request-id`、`x-asr-elapsed-ms`、`x-asr-total-ms`、`x-asr-duration`，供网关性能测试读取；响应体只有 `text`。
- 模型标签（`<|zh|><|NEUTRAL|><|Speech|><|withitn|>`）在返回前清除，不会污染用户消息。

### 内部详细接口

```bash
curl -X POST -F "file=@testdata/zh-5s.wav" \
  http://127.0.0.1:19899/api/asr/transcribe
```

返回 `text`、`duration`、`elapsed_ms`、`decode_ms`、`total_ms`、`rtf`、`model`、`language`、`request_id`。聊天前端不直接调用它；Nexus 的 `/api/asr/transcribe` 使用网关。

## 配置

`config.yaml` 是唯一事实源；少量部署项可用 `NEXUS_ASR_*` 环境变量覆盖。

```yaml
server:
  host: 127.0.0.1
  port: 19899
  max_upload_mb: 12
  max_audio_seconds: 60
  max_concurrency: 2
  queue_timeout_seconds: 5
  request_timeout_seconds: 60
  rate_limit_per_minute: 120

asr:
  provider: funasr
  model: sensevoice-small
  model_dir: ./models/SenseVoiceSmall
  cache_dir: ./models/modelscope
  vad_model: fsmn-vad
  vad_max_segment_ms: 30000
  hub: ms
  device: cpu
  num_threads: 4
  language: auto
  use_itn: true
  auto_download: true
  offline: false
```

- 环境变量：`NEXUS_ASR_PROVIDER`、`NEXUS_ASR_CONFIG`、`NEXUS_ASR_HOST`、`NEXUS_ASR_PORT`、`NEXUS_ASR_LOG_LEVEL`、`NEXUS_ASR_MODEL_DIR`、`NEXUS_ASR_CACHE_DIR`、`NEXUS_ASR_NUM_THREADS`、`NEXUS_ASR_MAX_CONCURRENCY`、`NEXUS_ASR_VAD_MODEL`、`NEXUS_ASR_AUTO_DOWNLOAD`、`NEXUS_ASR_OFFLINE`。
- 启动先校验模型文件（存在 + 字节数），成功后加载；缺文件且 `auto_download: false` / `offline: true` 时启动失败并给出明确提示。
- 模型文件不重复下载；VAD 缓存在 `models/modelscope/`。
- 生产建议 `offline: true` 且 `auto_download: false`。

## Nexus 网关配置

浏览器只访问 Nexus，不保存网关 Key。配置在 **API 网关 → 模型路由** 页面完成：

1. **上游节点**：名称 `Mac SenseVoiceSmall`，Base URL `http://127.0.0.1:19899`，认证 `无认证`，探测策略 `语音识别（ASR）`，模型 `sensevoice-small`。
2. 探测两次，节点健康态变为 `HEALTHY`，启用并进入调度。
3. **资源池**：`Mac 本地 SenseVoiceSmall`，只包含该节点。
4. **模型路由**：请求模型 `sensevoice-small`，类型 `语音识别（ASR）`，目标模型 `sensevoice-small`，启用。
5. **数字员工语音输入**：选择该 ASR 路由并保存。私聊和群聊共用。

同一份配置的示例（实际以页面/数据库为准）：

```text
pool:  Mac 本地 SenseVoiceSmall
node:  http://127.0.0.1:19899, probe_strategy=ASR, models=[sensevoice-small]
route: model=sensevoice-small, model_type=ASR, target_model=sensevoice-small
speech_input: route_id=<route id>
```

- 网关 `base_url` 必须是**网关进程能访问的地址**。如果网关在容器或其他机器，不能用 `127.0.0.1`，要改成 Mac 的受控内网地址，并保证 ASR 监听地址/防火墙允许该来源。
- 模型配置保存后网关约 10 秒（配置轮询周期）生效；Nexus 的语音路由选择对禁用/类型变化立即生效。
- ASR 路由不会进入数字员工/Worker 的 LLM 模型下拉列表。
- 浏览器只调用 Nexus 的 `/api/asr/transcribe`，不存在直连 ASR 的回退。

## 错误与隐私

| 场景 | 行为 |
|---|---|
| 空录音 | ASR 422 `empty_audio`；Nexus 转发 code/message；前端提示“没有识别到语音” |
| 静音 / 背景噪声 | 200，`{"text": ""}`；前端按空结果提示 |
| 无效文件 | ASR 415 `unsupported_audio`；不尝试识别 |
| 超过 12 MiB | 网关/服务返回 413；Nexus 前端提示超过大小限制 |
| 超过 60 s | ASR 422 `audio_too_long` |
| 上游不可用 | 网关 503/502；Nexus 返回可重试错误；原草稿保留 |
| 模型未加载 | 503 `model_not_ready` |
| 鉴权失败 | Nexus 401；不读取、不转发音频 |

- 音频只在内存中处理；不写临时文件、不持久化。
- 日志不打印音频、完整转写文本或网关 Key。
- ASR 默认只监听 `127.0.0.1`；公网/跨机访问必须经过 Nexus 网关和受控网络。
- 前端不提供任意上游 URL；模型由服务端路由决定。

## 性能

实测（Mac M4 / 16 GB / CPU / 4 threads）：

| 场景 | 音频 | 平均总耗时 | RTF | CER |
|---|---:|---:|---:|---:|
| 短语音 | 2.92 s | 452 ms | 0.154 | 0.0% |
| 普通对话 | 5.03 s | 536 ms | 0.106 | 4.2% |
| 普通对话 | 9.93 s | 605 ms | 0.060 | 4.6% |
| 较长输入 | 28.83 s | 1110 ms | 0.038 | 6.1% |
| 中英混合 1 | 9.66 s | 662 ms | 0.068 | 20.0% |
| 中英混合 2 | 10.20 s | 660 ms | 0.064 | 13.5% |
| 业务术语 | 4.96 s | 501 ms | 0.100 | 0.0% |

- 网关额外耗时（网关墙钟 − ASR 服务端总耗时）p50 约 **10.4–12.7 ms**。
- 冷启动模型加载约 **5.9 s**，服务重启到 `/health` 就绪 **7.82 s**。
- 进程 RSS 约 **1.07–1.14 GB**；短语音/普通对话 CPU 平均约 **94–99%**、峰值约 **139–144%**（多核并行）。
- 完整数据：[`results/perf-v1.5.0.json`](results/perf-v1.5.0.json)；报告：[`docs/performance-report.md`](docs/performance-report.md)。

## 测试

```bash
# 单元/API（含 FunASR 真实推理）
.venv/bin/python -m pytest -q tests

# 真实性能/网关对比（需要 ASR + 网关已启动）
NEXUS_GATEWAY_API_KEY=... .venv/bin/python scripts/benchmark.py \
  --output results/perf-v1.5.0.json

# 浏览器录音 → Nexus → 网关 → ASR → AgentScope 完整 E2E
NEXUS_EMPLOYEE_ID=emp_xxx NEXUS_AUDIO=/tmp/case1-48k.wav \
  node scripts/e2e-voice.mjs
```

完整交互脚本见 [`scripts/interaction-tests.mjs`](scripts/interaction-tests.mjs)。脚本默认使用仓库 `frontend/node_modules/playwright`，也可用 `PLAYWRIGHT_PATH` 覆盖。

## 目录

```text
services/nexus-asr/
├── app/
│   ├── main.py           # FastAPI、并发/超时/限流、OpenAI 兼容入口
│   ├── config.py         # config.yaml + 环境变量
│   ├── model_store.py    # FunASR/SenseVoice 模型校验与下载
│   ├── audio.py          # PyAV 解码 + 16 kHz 单声道
│   ├── engine.py         # FunASR AutoModel（默认）/ sherpa-onnx（可选）
│   └── limiter.py        # 进程级滑动窗口限流
├── static/               # 独立测试网页
├── tests/                # pytest
├── scripts/              # benchmark / e2e / 浏览器交互
├── models/               # 模型（gitignore）
├── deploy/               # systemd
├── config.yaml / .env.example / requirements*.txt
├── setup-mac.sh / download-model.sh / nexus-asr.sh / start-mac.sh
└── start-linux.sh / install-offline.sh
```

## Linux 部署

见 [`docs/deployment-linux-arm64.md`](docs/deployment-linux-arm64.md)。服务本身不依赖 macOS；Linux ARM64 需要 Python 3.12、glibc ≥ 2.28、PyTorch/FunASR 的 ARM64 wheel，以及约 1.5 GB 内存。**本次未在 Linux 实机验证**，上线前按文档清单执行。

## 已知限制

- 不支持实时流式字幕；按句录音、停止后整段识别。
- 中英混合的技术术语（`sherpa-onnx`、`SenseVoice`、`INT8`、`FP32`、`Vue3` 等）仍有明显错误；输入框保持可编辑。
- FunASR 首次安装/模型下载需要网络；之后固定目录离线运行。
- 单进程单 worker；多 worker 会重复占用约 1 GB 内存。
