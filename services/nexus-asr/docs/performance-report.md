# Nexus ASR Server 性能测试报告

> 环境：Apple M4（10 核，16 GB），macOS 26.5.2，Python 3.12.13，sherpa-onnx 1.13.2，CPU 推理。
> 原始数据：[`results/perf-mac.json`](../results/perf-mac.json)。
> 测试日期：2026-10-21。

## 1. 测试对象

| 项 | 值 |
|---|---|
| 服务 | FastAPI + uvicorn，单 worker，127.0.0.1:19899 |
| 模型 | SenseVoice-Small INT8，`model.int8.onnx` 239,233,841 B |
| 推理 | sherpa-onnx `OfflineRecognizer.from_sense_voice`，`num_threads=4` |
| 并发上限 | 2（进程级 asyncio.Semaphore） |
| 队列超时 / 请求超时 | 5 s / 30 s |
| 上传上限 / 音频上限 | 12 MB / 60 s |
| 音频解码 | PyAV（内置 FFmpeg），统一重采样到 mono 16 kHz |
| 测试客户端 | `httpx`，同步请求；并发用 `asyncio.gather` |

模型加载发生在 uvicorn lifespan 启动阶段：先校验 228 MB 文件的 SHA256，再创建 ONNX Runtime session，成功后才开始接收请求。

## 2. 测试音频

| ID | 时长 | 来源 | 内容 |
|---|---|---|---|
| zh-5s | 5.03 s | macOS Tingting TTS | 会议纪要 + 三个待办 |
| zh-10s | 9.93 s | TTS | 项目评审会 + 接口文档 |
| zh-30s | 30.26 s | TTS | 约 170 字，含 Nexus / WebAssembly / Windows 等术语 |
| mixed-1 | 9.66 s | TTS | sherpa-onnx / WebAssembly / SenseVoice / Chrome / RTF |
| mixed-2 | 10.20 s | TTS | Nexus / Vue3 / getUserMedia / INT8 / FP32 |
| official-zh | 5.59 s | 官方真人录音 | “开放时间早上9点至下午5点。” |
| official-en | 7.15 s | 官方真人录音 | “The tribal chieftain called for the boy…” |
| WebM 5s | 5.03 s | PyAV 从 zh-5s 转码 | Opus / 48 kHz，模拟 Chrome MediaRecorder |

## 3. 端到端结果

“服务端总耗时”来自响应里的 `total_ms`（上传读取 + 解码 + 推理 + 序列化）；“客户端墙钟”包含 HTTP 往返。

| 样本 | 格式 | n | 服务端 p50 | 服务端 min–max | 推理 p50 | 解码 p50 | RTF p50 |
|---|---|---:|---:|---:|---:|---:|---:|
| 5 s | WAV | 5 | **56.1 ms** | 55.9–56.9 ms | 52.5 ms | 3.2 ms | 0.0104 |
| 5 s | WebM/Opus | 3 | **64.8 ms** | 61.3–65.3 ms | 57.1 ms | 7.5 ms | 0.0113 |
| 10 s | WAV | 3 | **100.2 ms** | 99.6–102.0 ms | 96.1 ms | 3.8 ms | 0.0097 |
| 30 s | WAV | 3 | **332.2 ms** | 331.1–347.4 ms | 325.4 ms | 6.4 ms | 0.0108 |
| mixed-1 | WAV | 2 | 103.1 ms | 102.0–104.2 ms | 99.1 ms | 3.8 ms | 0.0103 |
| mixed-2 | WAV | 2 | 101.6 ms | 101.2–102.0 ms | 97.6 ms | 3.7 ms | 0.0095 |
| official-zh | WAV | 2 | 61.7 ms | 60.8–62.5 ms | 58.0 ms | 3.5 ms | 0.0104 |
| official-en | WAV | 2 | 79.5 ms | 76.9–82.1 ms | 75.5 ms | 3.7 ms | 0.0106 |

### 5 秒目标判定

- 目标：5 秒录音结束后，服务端识别尽量控制在 1 秒内。
- 实测：WAV **56 ms**，WebM **65 ms**，客户端 p50 约 61 ms。
- **达标，余量约 15 倍**。这是原生 ONNX Runtime + CPU 推理相对于浏览器 WASM 的主要收益。

## 4. 连续与并发

| 场景 | 结果 |
|---|---|
| 20 次连续 5 s 识别 | 20/20 成功；客户端 p50 61.4 ms，p95 63.5 ms，max 63.6 ms |
| 8 个并发 5 s 请求 | 8/8 成功；总墙钟 419 ms；单请求 p50 259 ms，max 409 ms；无 429/503 |
| 并发上限 | 2；按队列等待，队列超时 5 s |

说明：8 并发时 2 个请求先执行，其余排队；单请求耗时从 56 ms 上升到约 259 ms，但总吞吐仍高，无失败。

## 5. CPU 与内存

在 30 s 请求执行期间采样服务进程（`ps -p PID -o rss=,time=`）：

| 指标 | 值 |
|---|---:|
| 空闲 RSS | 796 MB |
| 峰值 RSS | 800 MB |
| 30 s 请求墙钟 | 0.34 s |
| 该窗口 CPU 时间 | 1.23 s |
| CPU 占用 | **362% of one core（约 3.6 核）** |
| 模型加载耗时 | 425.6 ms（`/api/asr/models`） |
| 服务重启到 `/health` 就绪 | **802 ms** |

- 内存主要由 228 MB 模型 + ONNX Runtime arena + Python/PyAV 组成，约 800 MB。
- `num_threads=4` 会用满约 3.6 个核；在共享机器上可降到 2，预期延迟略升、CPU 降低。
- 20 次连续请求后 RSS 未增长（无泄漏迹象）。

## 6. 重启与离线

| 场景 | 结果 |
|---|---|
| 停止进程后重新启动 | 802 ms 到 `/health`，日志 `model loaded in 419 ms` |
| `NEXUS_ASR_OFFLINE=1` 启动 | 成功；不发任何下载请求 |
| 离线 5 s 识别 | 89.8 ms，文本正确 |
| 离线且模型缺失 | 启动失败并输出明确错误（不会静默降级） |
| 模型自动下载 | 228 MB / 18.1 s（本机网络），SHA256 校验通过 |

## 7. 识别准确性

CER：转小写、去掉空白/标点/符号后，按字符 Levenshtein ÷ 参考长度。

| 样本 | CER | 典型差异 |
|---|---:|---|
| zh-5s | 4.2% | “待办” → “代办”（同音） |
| zh-10s | 4.7% | “十点” → “10点”（ITN 归一化） |
| zh-30s | 8.0% | “Mac” → “make”；“WebAssembly” → “web assembly”；`nexus` 小写 |
| mixed-1 | 21.5% | “Demo” → “de”；“sherpa-onnx” 丢失；“SenseVoice” → “ense voice”；“RTF” → “rRTF” |
| mixed-2 | 9.6% | “Vue3” → “nexus3”；“INT8” → “int8”；“FP32” → “FT32”；“getUserMedia” → “get user media” |
| official-zh（真人） | **0.0%** | 完全正确 |
| official-en（真人） | **0.0%** | 完全正确（仅缺句末句号，归一化后不计） |

**结论：**

1. 普通中文、官方真人中英文识别质量很好；官方两个真人样本 CER 均为 0。
2. 混合样本的英文术语由中文音色 TTS 朗读，发音本身不标准；模型对 sherpa-onnx、SenseVoice、INT8、FP32、Vue3 仍有错误。
3. 生产上建议：保留可编辑输入框；维护领域术语纠错表；用真实用户录音建立回归集。
4. 版本选择：`sherpa-onnx==1.13.2` 与官方 `zh.wav` 参考一致；1.13.6–1.13.8 会把“开放时间”识别成“开饭时间”，因此固定 1.13.2。速度差异可忽略。

## 8. 验收结论

| 验收项 | 结果 |
|---|---|
| 5 s 录音 1 s 内识别 | ✅ 56 ms（WAV）/ 65 ms（WebM） |
| 10 s / 30 s | ✅ 100 ms / 332 ms |
| 连续 20 次 | ✅ 20/20 |
| 多请求并发 | ✅ 8/8，无错误 |
| 服务重启后加载 | ✅ 802 ms 就绪 |
| 断网识别 | ✅ `NEXUS_ASR_OFFLINE=1` |
| 中英文混合 | ⚠️ 普通句子可用；技术术语需纠错 |
| Linux ARM64 | ⚠️ 未实机验证（见部署文档） |

## 9. 复现

```bash
./start-mac.sh
.venv/bin/python scripts/perf_test.py --output results/perf-mac.json
```

测试会先生成 WebM，再依次执行 5/10/30 s、混合、官方真人、20 连续、8 并发和 30 s 资源采样。

## 10. 风险与后续

- **Linux ARM64 性能未实测**：Mac M4 与目标服务器 CPU 不同，需在目标机复跑本脚本。
- **CPU 占用约 3.6 核**：高并发时与其他服务争抢；可通过 `num_threads` 和 `max_concurrency` 调节。
- **内存约 800 MB**：单实例可接受；不要启动多个 uvicorn worker。
- **模型 228 MB**：部署包和磁盘需预留；模型已从 Git 中分离。
- **术语错误**：前端可编辑 + 术语表是当前最有效的兜底。
