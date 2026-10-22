# Linux ARM64 部署

> 本次验收只在 Mac M4 实机运行 FunASR。Linux ARM64 的脚本、wheel 解析和 systemd 模板已经准备，但**未在 Linux 实机启动模型**；上线前必须按第 7 节清单执行。不要把“wheel 下载成功”当成“Linux 已验收”。

## 1. 目标环境

| 项 | 要求 | 检查 |
|---|---|---|
| 架构 | aarch64 / arm64 | `uname -m` |
| glibc | >= 2.28（PyAV） | `ldd --version` |
| Python | 3.12（venv 独立） | `python3 --version` |
| 进程管理 | systemd | `systemctl --version` |
| 内存 | 建议 >= 4 GB 空闲；常驻约 1.1 GB | `free -m` |
| 磁盘 | 源码 + venv + 模型约 4 GB | `df -h /opt` |
| 网络 | 安装期可访问 PyPI/ModelScope；运行期可离线 | `curl -I https://www.modelscope.cn` |

不依赖 GPU/CUDA；不替换系统 Python；服务只监听 `127.0.0.1:19899`，由 Nexus 网关调用。

## 2. 准备离线包（Mac）

```bash
cd services/nexus-asr

# Linux ARM64 wheels（FunASR、PyTorch、PyAV、FastAPI 等）
./scripts/prepare-linux-wheels.sh

# 固定模型目录（约 936 MB）
./download-model.sh

tar czf nexus-asr-funasr-linux-arm64.tar.gz \
  app static models wheels config.yaml .env.example requirements*.txt \
  nexus-asr.sh start-linux.sh install-offline.sh download-model.sh \
  deploy docs README.md testdata scripts

shasum -a 256 nexus-asr-funasr-linux-arm64.tar.gz
```

注意：

- `prepare-linux-wheels.sh` 会为 `requirements.txt` 下载 aarch64 wheel；PyTorch 的 Linux ARM64 CPU wheel 可能来自 PyPI 或需要 `--extra-index-url`。首次执行请检查日志和 `wheels/` 是否完整。
- 模型目录 `models/SenseVoiceSmall/` 与 VAD 缓存 `models/modelscope/` 必须一起打包，或目标机联网首次下载。
- `models/` 已被 `.gitignore` 忽略，不会进入 Git。

## 3. 上传与安装

```bash
sudo mkdir -p /opt/nexus-asr
sudo tar xzf nexus-asr-funasr-linux-arm64.tar.gz -C /opt/nexus-asr
sudo useradd --system --home /opt/nexus-asr --shell /usr/sbin/nologin nexus-asr
sudo chown -R nexus-asr:nexus-asr /opt/nexus-asr
cd /opt/nexus-asr
sudo -u nexus-asr ./install-offline.sh
```

`install-offline.sh`：

1. 校验 aarch64、Python 3.12、glibc；
2. 在 `/opt/nexus-asr/.venv` 创建独立虚拟环境；
3. `pip install --no-index --find-links wheels`；
4. 用 `app.preflight` 校验模型文件，不启动服务。

## 4. 配置

```bash
cd /opt/nexus-asr
sudo -u nexus-asr cp .env.example .env
sudo -u nexus-asr vi config.yaml
```

生产建议：

```yaml
server:
  host: 127.0.0.1
  port: 19899
  request_timeout_seconds: 60
  max_concurrency: 2
asr:
  provider: funasr
  model: sensevoice-small
  model_dir: /opt/nexus-asr/models/SenseVoiceSmall
  cache_dir: /opt/nexus-asr/models/modelscope
  num_threads: 4
  auto_download: false
  offline: true
```

```dotenv
NEXUS_ASR_CONFIG=/opt/nexus-asr/config.yaml
NEXUS_ASR_HOST=127.0.0.1
NEXUS_ASR_PORT=19899
NEXUS_ASR_OFFLINE=1
NEXUS_ASR_AUTO_DOWNLOAD=0
```

## 5. systemd

```bash
sudo cp deploy/nexus-asr.service /etc/systemd/system/nexus-asr.service
sudo systemctl daemon-reload
sudo systemctl enable --now nexus-asr
sudo systemctl status nexus-asr
curl -fsS http://127.0.0.1:19899/health
curl -fsS http://127.0.0.1:19899/v1/models
curl -X POST -F "file=@testdata/zh-5s.wav" -F "model=sensevoice" \
  http://127.0.0.1:19899/v1/audio/transcriptions
```

日志：`journalctl -u nexus-asr -f`。服务单元只允许写 `models/`，其余文件系统只读。

## 6. 接入 Nexus 网关（不要暴露公网）

在 Nexus **API 网关 → 上游节点**新增：

```text
name:          Linux SenseVoiceSmall
base_url:      http://<linux-host>:19899   # 必须是网关能访问的地址
auth_type:     NONE
probe_strategy: ASR
models:        ["sensevoice-small"]
```

如果网关与 ASR 同机，可用 `http://127.0.0.1:19899`；跨机时不要用 `127.0.0.1`。随后创建 ASR 资源池/路由并在「模型路由」页选择「数字员工语音输入」。

- 不要在公网 Nginx 暴露 `/api/asr/*` 直连 ASR；浏览器只访问 Nexus。
- 跨机时把 ASR 监听地址改为受控内网地址（例如 `0.0.0.0` 或指定网卡），并用防火墙只允许网关 IP。
- 网关节点探测策略必须是 ASR；探测请求会发送 0.5 s 静音 WAV 并校验 `{"text": ...}`。

## 7. Linux 上线验收清单

- [ ] `uname -m` = aarch64；`ldd --version` >= 2.28；`python3 --version` = 3.12
- [ ] `install-offline.sh` 成功；`.venv/bin/python -c "import funasr, torch, av"`
- [ ] `systemctl start nexus-asr`，60 s 内 `/health` 为 `ok`
- [ ] `/v1/models` 返回 `sensevoice-small`
- [ ] 2.9 s / 5 s / 10 s / 28.8 s 中文 WAV 识别成功，记录 `total_ms`/RTF
- [ ] WebM/Opus（浏览器录音）识别成功
- [ ] 中英混合与业务术语记录 CER
- [ ] 静音、噪声、空文件、非音频、13 MiB、61 s 均返回预期错误
- [ ] 连续 10 次热启动，记录 mean/min/max
- [ ] 通过 Nexus 网关转写成功，记录网关额外耗时
- [ ] `systemctl restart` 后模型恢复，记录冷启动时间
- [ ] 进程 RSS/CPU（`systemd-cgtop` 或 `ps`）
- [ ] 日志无音频、无转写文本、无 Key
- [ ] 目标机离线启动（`NEXUS_ASR_OFFLINE=1`）

性能脚本：

```bash
NEXUS_GATEWAY_API_KEY=... .venv/bin/python scripts/benchmark.py \
  --output results/perf-linux-arm64.json
```

## 8. 已知限制

- 未在 Linux ARM64 实机验证 FunASR/PyTorch 推理；Mac 的 RTF/CPU 数据不能直接外推到目标服务器。
- FunASR/PyTorch 的 aarch64 wheel 体积较大，离线包约 2–3 GB（不含模型）；实际以 `wheels/` 为准。
- 服务单 worker；多实例会重复占用约 1.1 GB 内存。
- 模型固定目录不要与旧 sherpa-onnx `models/sensevoice-small-int8/` 混用；`provider` 与 `model_dir` 必须匹配。
