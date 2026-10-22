# Nexus 语音输入：统一经 API 网关

链路：浏览器录音 → Nexus `/api/asr/transcribe` → API 网关 `/v1/audio/transcriptions` → 独立 ASR 模型 API。私聊与群聊共用同一语音输入配置，识别文字追加到当前草稿，用户手动发送。失败保留草稿与录音重试入口。

## 部署与配置

1. 按 README 或 Linux 部署文档安装独立 ASR。接口支持 `POST /v1/audio/transcriptions`，multipart 字段 `model=sensevoice-small-int8`、`file`，可选 `response_format=json`；只支持非流式 JSON。`GET /v1/models` 列出实际模型。
2. 对 Nexus 数据库执行 `alembic upgrade head`，新增 `0002_gateway_speech`，不修改已发布 init。升级后所有 Nexus API / 网关进程使用同一份代码。
3. 在 API 网关新增资源池与节点。节点 Base URL 填根地址，例如 `http://127.0.0.1:19899`；远程部署填网关进程可访问的内网地址。模型填 `sensevoice-small-int8`，认证按实际服务配置，内置本地服务使用“无认证”；探测策略选择“语音识别（ASR）”。
4. 保存节点，执行 ASR 探测至健康达标，再启用与进入调度。探测上传半秒静音 WAV，只检查真实转写协议返回的 `text`，不调用 chat。
5. 新增模型路由，类型选“语音识别（ASR）”，请求模型如 `nexus-speech`，实际模型 `sensevoice-small-int8`，选择对应资源池。
6. 在同一“模型路由”页的“数字员工语音输入”选择该路由并保存。这里不维护上游地址、厂商或密钥。配置保存在 Nexus 数据库，重启后继续生效。
7. Nexus 服务端必须配置 `NEXUS_GATEWAY_OPENAI_BASE=http://<gateway-host>:18110/v1` 与稳定 `NEXUS_GATEWAY_API_KEY`，该 Key 的允许模型包含语音请求模型。与员工对话共用服务端调用 Key 时，保留对话模型授权。Key 只在服务端使用，不写入浏览器构建变量。

现存路由新增明确的 CHAT / EMBEDDING / ASR 类型。迁移将纯 EMBEDDING 探测节点池的既有路由标记为 EMBEDDING；混合池需要管理员核对。对话员工只可选择 CHAT，知识库需要 EMBEDDING，语音输入只可选择启用的 ASR。

## Nginx 与开发代理

移除旧的 `/api/asr/*` → `127.0.0.1:19899` 精确代理。使用 `ops/deploy/nginx-nexus.conf`：`/api/` 指向统一服务（默认 18120），`/v1/` 指向网关（默认 18110）。保留公司登录鉴权与可信身份头；语音上传要求 Nexus 身份。前端开发也将 `/api/asr` 交给统一服务。

ASR 只向网关主机开放；同机部署绑定回环，跨机部署用内网监听和网络访问控制，不将无认证模型服务暴露公网。13 MiB 的 Nginx 上传限制包含表单开销，音频文件本身最多 12 MiB；应用识别最长 60 秒。网关保留 Key 授权、吊销、并发、调度与调用元数据，不保存音频或识别正文；未返回 Token 用量保持 unknown，不估计为零。

## 验证

```bash
curl -fsS http://127.0.0.1:19899/health
curl -fsS http://127.0.0.1:19899/v1/models
curl -fsS -F model=sensevoice-small-int8 -F file=@testdata/zh-5s.wav \
  http://127.0.0.1:19899/v1/audio/transcriptions
```

再通过 Nexus 同源上传测试，并在网关调用记录确认请求模型与实际模型、节点和状态。不要把直接访问 ASR 成功当成网关接入成功。禁用路由后输入应明确报错；不得回退本机服务。硬件录音需要浏览器允许麦克风及 HTTPS 或 localhost 安全上下文。
