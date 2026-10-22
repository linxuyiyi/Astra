# Linux 远程员工部署

在「Agent Worker」中新建或编辑 Worker，选择 Linux 远程，填写 SSH 连接和绝对工程目录后保存，再从维护提交「部署」。首次安装可能下载软件或编译依赖；操作持久化执行，页面断开不取消维护。以完整装备核验、在线状态和健康观测确认可用。

## 平台准备

平台的 API 中转必须启用并至少有一个启用的模型路由。员工模型留空时部署选择首个路由，也可填写 `nexus-gateway/<路由模型>`。部署为员工创建专属 Key，重复部署复用有效 Key；已经写入远端配置的 Key 不会因后续步骤失败被盲目吊销；修复后可显式重试。

数字图书馆须运行 HTTP MCP。`ops/env/.env` 设置 `NEXUS_MCP_TRANSPORT=streamable-http`、`NEXUS_MCP_API_KEY=<服务密钥>`，重启平台并运行 `python -m app.mcp_server`（在 `services/nexus-api` 内，使用项目虚拟环境）。平台和图书馆服务须加载同一份配置。服务管理命令见 [部署指南](../../ops/deploy/DEPLOYMENT_GUIDE.md)。

远端 OpenCode 只听 `127.0.0.1` 上的环境端口，默认 14096。Worker 直连内网的 API 中转和图书馆，部署拒绝回环地址。SSH 只用于安装和 Worker 离线时的维护。

| 配置项 | 默认值 | 用途 |
| --- | --- | --- |
| `NEXUS_REMOTE_OPENCODE_VERSION` | `1.18.35` | 固定 OpenCode npm 包版本 |
| `NEXUS_REMOTE_NODE_VERSION` | `22.23.3` | 固定 Node.js 版本 |
| `NEXUS_REMOTE_NPM_REGISTRY` | `https://registry.npmmirror.com` | 当前 SSH 用户的 npm registry |
| `NEXUS_REMOTE_PYTHON` | 空（自动发现） | 远端 Python ≥3.11，例如 `/usr/bin/python3.12` |
| `NEXUS_REMOTE_PYTHON_SOURCE_TGZ` | 空 | 远端已上传 CPython 源码 tgz 的绝对路径；解释器缺失时在私有目录构建 |
| `NEXUS_REMOTE_CA_FILE` | 空 | 远端 CA PEM 绝对路径，供 Worker、OpenCode、Git 信任内部证书 |
| `NEXUS_REMOTE_PIP_INDEX_URL` | 空 | 设置后传给远端 pip 的 `-i` |
| `NEXUS_REMOTE_DEPLOY_TIMEOUT_SECONDS` | `1200` | 单次维护最长秒数 |
| `NEXUS_REMOTE_GATEWAY_LOCAL_URL` | `http://127.0.0.1:18110` | 写到远端的 Gateway 地址。部署前改成 Worker 能访问的内网地址 |
| `NEXUS_MCP_RESOURCE_SERVER_URL` | `http://127.0.0.1:18201/mcp` | 写到远端的图书馆 MCP。同样必须是内网地址 |
| `NEXUS_WORKER_AGENT_GATEWAY_URL` | 空 | Worker Agent 拨入的 WebSocket。部署拒绝空值和回环 |

## 安装和配置范围

- 一键部署需具备写入 `/etc/nexus` 和 `/opt/nexus/workers` 的权限，通常使用 root；root 会安装系统依赖。软件本体按 Worker 隔离。非 root 的真实部署未在本次验收，权限不足时应由管理员准备环境。
- Worker Agent 需要 Python ≥3.11。未设置 `NEXUS_REMOTE_PYTHON` 时，先检查 `python3`，再查找 `python3.13/3.12/3.11`。显式解释器缺失时，可将已上传的 CPython 源码包路径写入 `NEXUS_REMOTE_PYTHON_SOURCE_TGZ`，安装器用系统 Python ≥3.8 解析早期 JSON，在 `~/.local/share/nexus/python/<源码哈希>/` 构建并验证 SSL、SQLite 和 venv；需要预装 make、C 编译器及 OpenSSL/SQLite 开发依赖，必要时增大维护超时。平台不会下载源码或替换系统 Python。Debian/Ubuntu 的已安装解释器缺少 venv 时仍使用对应系统包。
- 在此前提下支持 x86_64 / aarch64 的 Debian/Ubuntu、RHEL 系列。OpenCode 统一使用 `npm install --global --prefix "$HOME/.local/share/nexus/workers/<环境 id>/npm" --registry <配置的镜像> --include=optional opencode-ai@<指定版本>` 安装，npm 验证包完整性，实际可执行版本必须匹配。仅验证成功后才将员工配置的执行路径链接到 npm 安装目录；旧的独立二进制即使版本相同，也会迁移到 npm 管理。已满足要求的 npm 安装会复用。
- 内嵌工作区前端只提供上游 `v1.18.35`。历史 Session 仍保留当时写入的版本号；该版本没有界面包时页面显示「版本未知/界面已退役」，不会改写成 1.18.35。
- CentOS 7 使用独立 Vault repo 配置安装依赖，不改原 yum 源；旧 glibc x86_64 使用 Node glibc-217 构建、npm 包自动选择的 OpenCode 平台二进制和隔离的 Alpine musl/C++ 运行库。仅为 musl 加载器增加运行库路径，不替换系统 glibc。Git 低于 2.20 时编译 2.43.7。CentOS 7 已停止维护，且系统 Python 不能运行 Worker Agent，建议后续迁移到自带 Python ≥ 3.11 的系统。
- 工程目录为空时初始化 Git 和初始提交；非空且不是 Git 仓库时拒绝，已有仓库保留原内容。
- 在 `/etc/nexus/opencode/<环境 id>/opencode.jsonc` 合并 `nexus-gateway` provider、默认模型和 `nexus-library` MCP。`nexus-opencode` 单元用 `OPENCODE_CONFIG` 指向这份文件，并用 `OPENCODE_CONFIG_DIR` 指向同一目录，技能从那里的 `skill/` 加载。通道切换和能力发现也读这份文件。不写用户全局 `~/.config/opencode/opencode.jsonc`，也不改 `XDG_CONFIG_HOME`。写入前备份为 `.nexus-backup-<操作ID>`，配置及备份权限为 0600。有效 JSONC 使用既有解析器合并；无效配置拒绝覆盖，需人工处理。安装缓存放在 `~/.cache/nexus-provision`。
- 和 v0.9 共用同一个 OS 用户时，v0.9 的用户全局 OpenCode 配置仍会叠加进 Nexus。推荐每个产品使用专用的 OS 用户。
- 平台 API 不回显 SSH 密码，维护日志隐藏服务密钥；远端全局配置含访问凭据，请保护该 Linux 账号和配置备份。

## 维护与恢复

部署或重启期间不能再次维护、修改或删除该员工、检测环境或启动任务。有占用任务、待确认提交、未结束 Runtime Session 或忙碌的原生 Session 时拒绝维护。Worker 在线时，重启下发给 Agent，不打开 SSH。Worker 离线时，用 SSH 按相同管理模式重启 OpenCode 和 Agent。安装成功后 SSH 密码留在环境行上。OpenCode 监听 `127.0.0.1` 和环境上的端口，默认 14096。

界面可显式取消操作；浏览器断线不会取消已持久化操作。已执行的系统安装不会回滚，远端脚本还有超时和文件锁保护。失败或取消后先查看失败阶段再显式重试；服务进程崩溃遗留的操作租约到期后可重试。不要同时对同一 Linux 用户的多个员工部署。部署结束会检查 Serve、模型中转认证及图书馆 MCP，模型实际生成还取决于中转上游可用性。


## 无 systemd 的容器

部署按 PID 1 和 systemd 可用性选择管理模式。systemd 环境保留服务单元；容器使用 `/opt/nexus/workers/<环境 id>/*-process.json` 记录 PID、Linux starttime、boot ID、cwd、argv 和进程组，完全重定向 stdin/stdout/stderr 并脱离 SSH 会话。记录不匹配或端口归属不明时拒绝停止进程，不能手工改为 pkill 或裸 PID kill。私有日志在同目录 `*-process.log`。

OpenCode 使用独立的 `/etc/nexus/opencode-<环境 id>.env`；Worker 使用 `worker-<环境 id>.env`。二者不可混用，OpenCode 环境不得包含 Worker token。相同配置重复部署复用进程；容器停止/重启会结束进程，容器重新启动后须由部署入口或容器启动管理重新拉起。本方案不提供容器级自动守护。

## HTTPS、代理与配置生效

`NEXUS_WORKER_AGENT_GATEWAY_URL` 接受 HTTP(S)/WS(S)，控制连接转换成对应 WS(S)，数据通道保留 HTTP(S)。生产代理使用仓库 `ops/deploy/nginx-nexus.conf` 中 `/api/worker-gateway/connect` 精确路由的 HTTP/1.1 Upgrade 配置；已有公司 Nginx 需要同步该 location 后运行 `nginx -t` 再 reload。

内网自签证书需将 CA 安装到远端信任库，或通过 `NEXUS_REMOTE_CA_FILE` 指向远端 PEM。证书链及主机名验证保持开启；不要使用 `verify=False`、`CERT_NONE` 或跳过 Node 的 TLS 校验。

`.env` 属于启动配置：`get_settings()` 每个进程只加载一次。改动后重启 nexus-api，以及单独运行的维护执行器等实际读取该配置的常驻进程，再重新提交维护；HTTP 重试不会重新加载 `.env`。不为修复此问题删除 settings 缓存或在部署中临时读取另一份配置。

通道切换以运行中 OpenCode 的 `/config?directory=...` 核验 Gateway/MCP；健康先就绪而配置尚未就绪时有界等待。磁盘文件存在不能证明已加载；持续缺少 provider/MCP 时维护失败，检查实际进程的配置路径和项目配置覆盖。受管 `model` 与 `small_model` 均指向 Nexus 网关，只启用 `nexus-gateway` 并禁用 `opencode`。
