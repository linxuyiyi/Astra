# Linux ARM64 离线部署

> 本机没有 Linux ARM64 实机，也没有可用的 Docker/模拟器；本文的命令、wheel 集合和离线解析已经验证，**但目标服务器上的运行验收仍需执行第 8 节清单**。不要把“离线解析通过”当成“Linux 实机已验证”。

## 1. 目标环境

| 项 | 要求 | 检查命令 |
|---|---|---|
| 架构 | aarch64 / arm64 | `uname -m` |
| glibc | **>= 2.28**（PyAV 19 的 manylinux_2_28 wheel） | `ldd --version` |
| Python | **3.12**（离线 wheels 为 cp312） | `python3 --version` |
| 进程管理 | systemd | `systemctl --version` |
| 反向代理 | Nginx（现有 Nexus 站点） | `nginx -v` |
| 磁盘 | 源码 + venv + 模型约 1.2 GB | `df -h /opt` |
| 内存 | 建议 >= 2 GB 空闲；常驻约 800 MB | `free -m` |

- 不依赖 GPU、CUDA、外网。
- 不安装/替换系统 Python；venv 只在应用目录内。
- 服务默认只监听 `127.0.0.1:19899`。

## 2. 在 Mac 上准备离线包

```bash
cd services/nexus-asr

# 1) 下载 Linux ARM64 的完整 wheel 集合（20 个，约 66 MB）
./scripts/prepare-linux-wheels.sh

# 2) 下载并校验模型（228 MB）
./download-model.sh

# 3) 打包（不含 .venv、缓存、测试结果）
tar czf nexus-asr-offline-linux-arm64.tar.gz \
  app static models wheels \
  config.yaml .env.example requirements.txt \
  start-linux.sh install-offline.sh download-model.sh \
  deploy docs README.md testdata

shasum -a 256 nexus-asr-offline-linux-arm64.tar.gz   # Linux: sha256sum
```

- `wheels/` 含 `sherpa_onnx-1.13.2-cp312-cp312-manylinux2014_aarch64...whl`、`av-19.0.1-cp312-abi3-manylinux_2_28_aarch64.whl`、numpy、pydantic-core 等。
- 下载脚本使用两套平台标签（`manylinux_2_28_aarch64` + `manylinux2014_aarch64`），因为不同依赖的 wheel 标签不同。
- 所有 wheel 已用 `pip install --dry-run --no-index` 做过完整性解析。
- 模型校验：大小 + SHA256（见 README）。

## 3. 上传与解压

```bash
# 在公司服务器上
sudo mkdir -p /opt/nexus-asr
sudo tar xzf nexus-asr-offline-linux-arm64.tar.gz -C /opt/nexus-asr --strip-components=0
sudo useradd --system --home /opt/nexus-asr --shell /usr/sbin/nologin nexus-asr
sudo chown -R nexus-asr:nexus-asr /opt/nexus-asr
```

如果压缩包内还有顶层目录，请按实际结构调整，确保 `/opt/nexus-asr/app/main.py` 存在。

## 4. 离线安装

```bash
cd /opt/nexus-asr
sudo -u nexus-asr ./install-offline.sh
```

脚本会：

1. 检查 `uname -m` 为 aarch64/arm64；
2. 检查 Python 3.12、glibc >= 2.28；
3. 在当前目录创建 `.venv`（不碰系统 Python）；
4. 用 `pip install --no-index --find-links wheels --only-binary=:all:` 离线安装；
5. 校验模型大小 + SHA256。

如果失败：

- `Python 3.12 is required`：安装 3.12 到 `/usr/local` 或用户目录，不要覆盖系统 Python；
- `glibc >= 2.28`：目标系统过旧，需要升级系统或改用兼容的 PyAV 构建；
- `No matching distribution`：`wheels/` 不完整，回 Mac 重新执行 `prepare-linux-wheels.sh`。

## 5. 配置

```bash
cd /opt/nexus-asr
sudo -u nexus-asr cp .env.example .env
sudo -u nexus-asr vi .env
```

生产建议：

```dotenv
NEXUS_ASR_CONFIG=/opt/nexus-asr/config.yaml
NEXUS_ASR_HOST=127.0.0.1
NEXUS_ASR_PORT=19899
NEXUS_ASR_LOG_LEVEL=info
NEXUS_ASR_OFFLINE=1
```

并把 `config.yaml` 的 `asr.auto_download` 改为 `false`，避免运行期任何下载尝试。

## 6. systemd

```bash
sudo cp deploy/nexus-asr.service /etc/systemd/system/nexus-asr.service
sudo systemctl daemon-reload
sudo systemctl enable --now nexus-asr
sudo systemctl status nexus-asr
```

- `ExecStartPre` 会再次校验模型；
- `ExecStartPost` 轮询 `/health`，60 s 未就绪则单元失败；
- `Restart=always`，异常退出后 3 s 重启；
- 日志：`journalctl -u nexus-asr -f`；
- 服务被 `ProtectSystem=strict` 限制，只有 `models/` 可写；
- 如果 `/usr/bin/curl` 不存在，请安装 curl 或改用脚本做健康检查。

验证：

```bash
curl -fsS http://127.0.0.1:19899/health
curl -fsS http://127.0.0.1:19899/api/asr/models
curl -X POST -F "file=@testdata/zh-5s.wav" \
  http://127.0.0.1:19899/api/asr/transcribe
```

## 7. 接入 Nexus API 网关

按 [Nexus 集成说明](nexus-integration.md) 登记 ASR 节点和路由，执行专项探测、启用节点，并保存数字员工语音输入路由。公司跨机部署时，上游地址必须从网关主机可达；同机可使用回环监听。

Nginx 使用 `ops/deploy/nginx-nexus.conf` 的统一入口，删除旧的 `/api/asr/*` 直连 ASR 配置。浏览器请求由统一 Nexus 服务验证身份、读取路由，再携服务端 Key 调用网关。不要公开无认证的 ASR 服务。

## 8. 上线验收清单

在目标 Linux ARM64 上执行：

- [ ] `uname -m` = aarch64，`ldd --version` >= 2.28，`python3 --version` = 3.12
- [ ] `install-offline.sh` 成功，`.venv/bin/python -c "import sherpa_onnx, av"`
- [ ] `systemctl start nexus-asr`，`/health` 在 60 s 内 `status=ok`
- [ ] 5 s / 10 s / 30 s 中文 WAV 识别，记录 `total_ms`、`elapsed_ms`、RTF
- [ ] WebM/Opus（浏览器录音）识别
- [ ] 中英混合与技术术语
- [ ] 连续 20 次，全部成功
- [ ] 8 并发，无 5xx/429（或按预期限流）
- [ ] `systemctl restart` 后模型加载时间
- [ ] 断网/`NEXUS_ASR_OFFLINE=1` 后识别
- [ ] `ps`/`systemd-cgtop` 记录 RSS 与 CPU
- [ ] Nginx 代理路径 + 登录鉴权 + 失败重试
- [ ] 日志中无音频、无识别文本

推荐直接运行：

```bash
.venv/bin/python scripts/perf_test.py --output results/perf-linux-arm64.json
```

## 9. 升级与回滚

- 新版本解压到 `/opt/nexus-asr-new`，安装并验证后再切换 systemd 的 `WorkingDirectory`；
- 模型目录可共用，避免重复下载；
- 保留上一版目录，回滚时改回 unit 并 `daemon-reload`；
- 模型升级需同时更新 `app/model_store.py` 的 SHA256 和 URL，并清空旧 `models/`（不可混用）。

## 10. 已知限制

- 本次未在 Linux ARM64 实机运行；性能数据来自 Mac M4，目标机需复测。
- PyAV 的 manylinux_2_28 要求 glibc >= 2.28；老系统不满足。
- 离线包约 310 MB（模型 241 MB + wheels 66 MB + 源码）。
- 服务单 worker；systemd 不要配置多实例共享同一端口。
