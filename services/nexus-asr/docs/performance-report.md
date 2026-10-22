# Nexus v1.5.0 SenseVoiceSmall 性能报告

> 环境：Apple M4（10 核），16 GB 统一内存，macOS 26.5.2，Python 3.12.13。
> 推理：SenseVoiceSmall + FunASR 1.4.16 + PyTorch 2.14.1，CPU，`num_threads=4`。
> 服务：FastAPI/uvicorn 单 worker，127.0.0.1:19899，`max_concurrency=2`。
> 测试时间：2026-10-23。原始数据：[`results/perf-v1.5.0.json`](../results/perf-v1.5.0.json)。

本报告只使用本机真实推理与真实 HTTP 请求；短语音与普通对话各执行 **10 次热启动**，其余场景 2–3 次。旧版 sherpa-onnx 报告已被本报告取代（历史数据仍在 Git 记录中）。

## 1. 模型与测试集

| 项 | 值 |
|---|---|
| 模型 | ModelScope `iic/SenseVoiceSmall` |
| 模型文件 | `model.pt` 936,291,369 B；`tokens.json` 352,064 B；`am.mvn`；BPE 模型 |
| 模型目录 | `services/nexus-asr/models/SenseVoiceSmall/` |
| VAD | FSMN-VAD，缓存于 `models/modelscope/` |
| 解码 | PyAV（内置 FFmpeg），统一 mono 16 kHz |
| 冷启动 | 模型加载 **5.92 s**（`/api/asr/models.load_ms`）；重启到 `/health` 就绪 **7.82 s** |

| ID | 时长 | 来源 | 内容 |
|---|---:|---|---|
| short-3s | 2.92 s | macOS Tingting TTS | 你好，查询当前任务情况。 |
| zh-5s | 5.03 s | TTS | 会议纪要 + 三个待办 |
| zh-10s | 9.93 s | TTS | 项目评审会 + 接口文档 |
| long-30s | 28.83 s | TTS | 语音输入接入 Nexus 的约 150 字方案 |
| mixed-1 | 9.66 s | TTS | Demo / WebAssembly / SenseVoice / Chrome / RTF |
| mixed-2 | 10.20 s | TTS | Nexus / Vue3 / getUserMedia / INT8 / FP32 |
| business-4s | 4.96 s | TTS | 自动化测试、任务执行、Worker、MCP |

## 2. 直接调用 ASR（10 次热启动）

“总耗时”是 ASR 服务端 `total_ms`（读上传 + 解码 + 推理 + 序列化）；“推理”是 `elapsed_ms`；RTF = 推理耗时 / 音频时长。

| 场景 | 音频 | 总耗时 mean / min / max | 推理 mean | RTF | 进程 CPU 平均 / 峰值 | 峰值 RSS |
|---|---:|---:|---:|---:|---:|---:|
| 短语音 | 2.92 s | **451.9 / 443.2 / 477.5 ms** | 448.7 ms | 0.154 | 94.4% / 141.3% | 1067 MB |
| 普通对话 | 5.03 s | **536.4 / 485.2 / 673.3 ms** | 532.6 ms | 0.106 | 98.6% / 139.3% | 1076 MB |
| 普通对话 | 9.93 s | 604.6 ms（n=3） | 599.8 ms | 0.060 | 109.0% / 144.2% | 1077 MB |
| 较长输入 | 28.83 s | 1109.7 ms（n=2） | 1102.3 ms | 0.038 | 135.9% / 185.4% | 1142 MB |
| 中英混合 1 | 9.66 s | 662.2 ms（n=2） | 657.7 ms | 0.068 | 108.1% / 139.8% | 1096 MB |
| 中英混合 2 | 10.20 s | 659.8 ms（n=2） | 655.5 ms | 0.064 | 108.0% / 144.1% | 1097 MB |
| 业务术语 | 4.96 s | 500.7 ms（n=3） | 497.0 ms | 0.100 | 96.2% / 128.7% | 1097 MB |

说明：

- CPU 百分比以单核为 100%；FunASR 使用 4 个 ONNX/PyTorch 线程，峰值可达约 1.4–1.9 核。
- RSS 包含 PyTorch、FunASR、FSMN-VAD 与模型，稳定在 **1.07–1.14 GB**；30 s 音频略高，未观察到持续增长。
- 短语音 10 次热启动的最慢一次 477.5 ms，仍低于 1 s。

## 3. 通过 Nexus 网关

网关入口 `POST http://127.0.0.1:18110/v1/audio/transcriptions`，请求模型 `sensevoice-small`，服务端 Key 鉴权、ASR 路由、节点调度与超时均由网关处理。响应头 `x-asr-total-ms` 来自 ASR 服务；网关额外耗时 = 网关墙钟 − ASR 服务端总耗时。

| 场景 | 直连 ASR 总耗时（均值） | 网关 ASR 总耗时（均值） | 网关额外耗时 p50 / mean / max | 成功率 |
|---|---:|---:|---:|---:|
| 短语音 | 451.9 ms | 448.7 ms | **10.77 / 10.73 / 11.71 ms** | 10/10 |
| 普通对话 5 s | 536.4 ms | 518.4 ms | **10.67 / 10.74 / 12.12 ms** | 10/10 |
| 普通对话 10 s | 604.6 ms | 613.4 ms | 10.59 / 10.85 / 11.52 ms | 3/3 |
| 较长输入 | 1109.7 ms | 1117.7 ms | 12.72 / 12.72 / 13.95 ms | 2/2 |
| 中英混合 1 | 662.2 ms | 627.4 ms | 11.13 / 11.13 / 11.34 ms | 2/2 |
| 中英混合 2 | 659.8 ms | 592.7 ms | 11.66 / 11.66 / 12.36 ms | 2/2 |
| 业务术语 | 500.7 ms | 479.0 ms | 10.44 / 10.55 / 10.88 ms | 3/3 |

**结论：网关没有引入明显延迟。** 代理、鉴权、路由、日志与租约的额外开销稳定在 **10–14 ms**，约为 ASR 推理耗时的 1–3%。个别“网关总耗时低于直连”是两次独立采样的热波动，不代表网关加速；以同一请求的 `网关墙钟 − x-asr-total-ms` 为准。

## 4. 异常输入

| 输入 | 直连 ASR | 通过网关 |
|---|---|---|
| 3 s 静音 | 200 `{"text":""}`，约 17 ms | 200 `{"text":""}` |
| 3 s 背景噪声 | 200 `{"text":""}`，约 17 ms | 200 `{"text":""}` |
| 空文件 | 422 `empty_audio` | 400 `INVALID_REQUEST`（网关在表单层拒绝，未到 ASR） |
| 非音频字节 | 415 `unsupported_audio` | 415 `unsupported_audio`（上游原始 code/message） |
| 13 MiB | 413 `payload_too_large` | 413 `PAYLOAD_TOO_LARGE` |
| ASR 停止 | 连接失败 | 网关 503/502；前端显示可重试错误，原草稿保留 |

修复项：语音转写的上游 **4xx 客户端错误不再计为节点失败**，不会因为用户上传坏音频而把 ASR 节点熔断；401/403/429/5xx 仍计为节点失败。

## 5. 识别准确率（CER）

CER：转小写、去掉空白/标点后按字符 Levenshtein ÷ 参考长度。

| 样本 | CER | 主要差异 |
|---|---:|---|
| short-3s | **0.0%** | 完全正确 |
| zh-5s | **4.2%** | “待办” → “代办” |
| zh-10s | **4.6%** | “十点” → “10点”（ITN 归一化，按字符计为错误） |
| long-30s | **6.1%** | “SenseVoice” → “sense voice”；“Mac” → “m”；“鉴权” → “健全” |
| mixed-1 | **20.0%** | “WebAssembly” → “web assembly”；“SenseVoice” → “ense voice”；“RTF” → “RRTF” |
| mixed-2 | **13.5%** | “Vue3” → “nexus3”；“getUserMedia” → “get us media”；“FP32” → “FT32” |
| business-4s | **0.0%** | 自动化测试、任务执行、Worker、MCP 全部正确 |

中文普通对话、短句和业务术语可用；中英混合技术术语仍有明显错误。产品上保留可编辑输入框，不自动发送，是当前最有效的兜底。

## 6. 冷启动、重启与恢复

| 操作 | 结果 |
|---|---|
| `./nexus-asr.sh restart` 到 `/health` | **7.82 s** |
| 模型加载（`/api/asr/models.load_ms`） | **5.92 s** |
| 重启后首次转写 | 成功，文本正确 |
| 网关在 ASR 重启后继续请求 | 首次请求前重新探测节点，HTTP 200 恢复 |
| 固定目录重复启动 | 不重新下载模型；离线可启动 |
| `NEXUS_ASR_OFFLINE=1` 且模型缺失 | 启动失败并明确报错，不静默降级 |

## 7. 复现

```bash
cd services/nexus-asr
./nexus-asr.sh start

# 真实性能 + 网关对比 + 资源采样
NEXUS_GATEWAY_API_KEY=... .venv/bin/python scripts/benchmark.py \
  --output results/perf-v1.5.0.json

# 部署链路自检
./scripts/verify-deployment.sh
```

浏览器端 E2E 与交互测试见 [`scripts/e2e-voice.mjs`](../scripts/e2e-voice.mjs) 和 [`scripts/interaction-tests.mjs`](../scripts/interaction-tests.mjs)。

## 8. 风险

- Linux ARM64 未实机验证；FunASR/PyTorch 的 Linux aarch64 依赖与模型缓存需在目标机复测。
- 中英混合术语错误仍是主要准确率风险。
- 单进程 RSS 约 1.1 GB；16 GB Mac 可运行，但不要启动多个 worker。
- 冷启动约 6–8 s；服务重启期间语音输入不可用，需等待健康检查恢复。
- 网关配置轮询约 10 s；修改 ASR 路由/node 后需等待一个周期，Nexus 的语音路由选择对启用/类型变化立即生效。
