# Linux 远程员工部署

在「数字员工」中新建或编辑员工，选择 Linux 远程，填写 IP、SSH 端口、用户名、密码和绝对工程目录后保存。检测失败时配置仍保存，可以直接点击「一键部署」。首次安装需要下载软件或编译 Git，请保持页面连接；完成后的环境状态为「可用」。已有环境可点击「重启 OpenCode Serve」。

## 平台准备

平台的 API 中转必须启用并至少有一个启用的模型路由。员工模型留空时部署选择首个路由，也可填写 `nexus-gateway/<路由模型>`。部署为员工创建专属 Key，重复部署复用有效 Key；失败时吊销本次新建 Key，修复后可再次部署。

数字图书馆须运行 HTTP MCP。`ops/env/.env` 设置 `NEXUS_MCP_TRANSPORT=streamable-http`、`NEXUS_MCP_API_KEY=<服务密钥>`，重启平台并运行 `python -m app.mcp_server`（在 `services/nexus-api` 内，使用项目虚拟环境）。平台和图书馆服务须加载同一份配置。服务管理命令见 [部署指南](../../ops/deploy/DEPLOYMENT_GUIDE.md)。

远端 OpenCode 只听 `127.0.0.1` 上的环境端口，默认 14096。Worker 直连内网的 API 中转和图书馆，部署拒绝回环地址。SSH 只用于安装和 Worker 离线时的维护。

| 配置项 | 默认值 | 用途 |
| --- | --- | --- |
| `NEXUS_REMOTE_OPENCODE_VERSION` | `1.18.35` | 固定 OpenCode npm 包版本 |
| `NEXUS_REMOTE_NODE_VERSION` | `22.23.3` | 固定 Node.js 版本 |
| `NEXUS_REMOTE_NPM_REGISTRY` | `https://registry.npmmirror.com` | 当前 SSH 用户的 npm registry |
| `NEXUS_REMOTE_PYTHON` | 空（使用 `python3`） | 远端已安装的 Python ≥ 3.11，例如 `/usr/bin/python3.12` |
| `NEXUS_REMOTE_PIP_INDEX_URL` | 空 | 设置后传给远端 pip 的 `-i` |
| `NEXUS_REMOTE_DEPLOY_TIMEOUT_SECONDS` | `1200` | 单次维护最长秒数 |
| `NEXUS_REMOTE_GATEWAY_LOCAL_URL` | `http://127.0.0.1:18110` | 写到远端的 Gateway 地址。部署前改成 Worker 能访问的内网地址 |
| `NEXUS_MCP_RESOURCE_SERVER_URL` | `http://127.0.0.1:18201/mcp` | 写到远端的图书馆 MCP。同样必须是内网地址 |
| `NEXUS_WORKER_AGENT_GATEWAY_URL` | 空 | Worker Agent 拨入的 WebSocket。部署拒绝空值和回环 |

## 安装和配置范围

- root 用户自动安装系统依赖；非 root 用户必须已有必要系统工具、可用 Git，软件安装到用户目录，缺少管理员权限时明确失败。
- Worker Agent 需要远端已经安装的 Python ≥ 3.11。未设置 `NEXUS_REMOTE_PYTHON` 时使用 `python3`。Ubuntu 22.04 自带 3.10，CentOS 7 的系统 Python 也低于 3.11，这两种系统不能直接跑 Agent；要先自行安装 Python 3.11 或更新版本，并把 `NEXUS_REMOTE_PYTHON` 指到该解释器，例如 `/usr/bin/python3.12`。解释器缺失或版本过低时，部署立刻失败并提示「需要 Python ≥3.11」。平台不在远端下载 Python，也不使用 uv。Debian/Ubuntu 上以 root 部署时，若该解释器缺少 venv/ensurepip，会安装对应的 `pythonX.Y-venv`，装不上再安装 `python3-venv`。
- 在此前提下支持 x86_64 / aarch64 的 Debian/Ubuntu、RHEL 系列。OpenCode 统一使用 `npm install --global --prefix "$HOME/.local/share/nexus/workers/<环境 id>/npm" --registry <配置的镜像> --include=optional opencode-ai@<指定版本>` 安装，npm 验证包完整性，实际可执行版本必须匹配。仅验证成功后才将员工配置的执行路径链接到 npm 安装目录；旧的独立二进制即使版本相同，也会迁移到 npm 管理。已满足要求的 npm 安装会复用。
- 内嵌工作区前端只提供上游 `v1.18.35`。历史 Session 仍保留当时写入的版本号；该版本没有界面包时页面显示「版本未知/界面已退役」，不会改写成 1.18.35。
- CentOS 7 使用独立 Vault repo 配置安装依赖，不改原 yum 源；旧 glibc x86_64 使用 Node glibc-217 构建、npm 包自动选择的 OpenCode 平台二进制和隔离的 Alpine musl/C++ 运行库。仅为 musl 加载器增加运行库路径，不替换系统 glibc。Git 低于 2.20 时编译 2.43.7。CentOS 7 已停止维护，且系统 Python 不能运行 Worker Agent，建议后续迁移到自带 Python ≥ 3.11 的系统。
- 工程目录为空时初始化 Git 和初始提交；非空且不是 Git 仓库时拒绝，已有仓库保留原内容。
- 在 `/etc/nexus/opencode/<环境 id>/opencode.jsonc` 合并 `nexus-gateway` provider、默认模型和 `nexus-library` MCP。`nexus-opencode` 单元用 `OPENCODE_CONFIG` 指向这份文件，并用 `OPENCODE_CONFIG_DIR` 指向同一目录，技能从那里的 `skill/` 加载。通道切换和能力发现也读这份文件。不写用户全局 `~/.config/opencode/opencode.jsonc`，也不改 `XDG_CONFIG_HOME`。写入前备份为 `.nexus-backup-<操作ID>`，配置及备份权限为 0600。已有 JSONC 或无效 JSON 时拒绝覆盖，需人工合并。安装缓存放在 `~/.cache/nexus-provision`。
- 和 v0.9 共用同一个 OS 用户时，v0.9 的用户全局 OpenCode 配置仍会叠加进 Nexus。推荐每个产品使用专用的 OS 用户。
- 平台 API 不回显 SSH 密码，维护日志隐藏服务密钥；远端全局配置含访问凭据，请保护该 Linux 账号和配置备份。

## 维护与恢复

部署或重启期间不能再次维护、修改或删除该员工、检测环境或启动任务。有占用任务、待确认提交、未结束 Runtime Session 或忙碌的原生 Session 时拒绝维护。Worker 在线时，重启下发给 Agent，不打开 SSH。Worker 离线时，用 SSH 重启 OpenCode 和 Agent 单元。安装成功后 SSH 密码留在环境行上。OpenCode 监听 `127.0.0.1` 和环境上的端口，默认 14096。

界面可取消操作；浏览器断线同样取消等待并关闭 SSH 连接。已执行的系统安装不会回滚，远端脚本还有超时和文件锁保护。失败或取消后先查看失败阶段再显式重试；服务进程崩溃遗留的操作租约到期后可重试。不要同时对同一 Linux 用户的多个员工部署。部署结束会检查 Serve、模型中转认证及图书馆 MCP，模型实际生成还取决于中转上游可用性。
