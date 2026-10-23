# Linux Agent Worker 部署

在「Agent Worker」中新建或编辑 Worker，选择 Linux 远程，填写 IP、SSH 端口、用户名、密码和绝对工程目录后保存。检测失败时配置仍保存，可以直接点击「一键部署」。Nexus 控制机运行 Python，通过 SSH 分步骤检查系统、准备独立 Python、安装 Node/OpenCode 和 Worker；目标机无需预装受支持的系统 Python。维护操作由后台执行，关闭页面后仍继续，可重新打开查看进度。

## 平台准备

平台的 API 中转必须启用并至少有一个启用的模型路由。员工模型留空时部署选择首个路由，也可填写 `nexus-gateway/<路由模型>`。部署为员工创建专属 Key，重复部署复用有效 Key；失败时吊销本次新建 Key，修复后可再次部署。

数字图书馆须运行 HTTP MCP。`ops/env/.env` 设置 `NEXUS_MCP_TRANSPORT=streamable-http`、`NEXUS_MCP_API_KEY=<服务密钥>`，重启平台并运行 `python -m app.mcp_server`（在 `services/nexus-api` 内，使用项目虚拟环境）。平台和图书馆服务须加载同一份配置。服务管理命令见 [部署指南](../../ops/deploy/DEPLOYMENT_GUIDE.md)。

远端 OpenCode 只听 `127.0.0.1` 上的环境端口，默认 14096。Worker 直连内网的 API 中转和图书馆，部署拒绝回环地址。SSH 只用于安装和 Worker 离线时的维护。

| 配置项 | 默认值 | 用途 |
| --- | --- | --- |
| `NEXUS_REMOTE_OPENCODE_VERSION` | `1.18.35` | 固定 OpenCode npm 包版本 |
| `NEXUS_REMOTE_NODE_VERSION` | `22.23.3` | 固定 Node.js 版本 |
| `NEXUS_REMOTE_DOWNLOAD_BASE_URL` | `https://repo.huaweicloud.com` | Python、Node、Alpine 与 CentOS 临时归档源的 HTTPS 镜像根地址 |
| `NEXUS_REMOTE_NPM_REGISTRY` | `https://repo.huaweicloud.com/repository/npm/` | 本次 Worker 安装使用的 npm registry，不修改全局 npm 配置 |
| `NEXUS_REMOTE_PYTHON` | 空（受管 Python） | 空时准备 `/opt/nexus/workers/<id>/python-3.11.17/bin/python3.11`；显式值必须已可用 |
| `NEXUS_REMOTE_PIP_INDEX_URL` | `https://repo.huaweicloud.com/repository/pypi/simple` | 本次 Worker pip 的 `-i` |
| `NEXUS_REMOTE_DEPLOY_TIMEOUT_SECONDS` | `1200` | 单次维护最长秒数 |
| `NEXUS_REMOTE_GATEWAY_LOCAL_URL` | `http://127.0.0.1:18110/v1` | 写到远端的 Gateway 地址。部署前改成 Worker 能访问的内网地址 |
| `NEXUS_MCP_RESOURCE_SERVER_URL` | `http://127.0.0.1:18201/mcp` | 写到远端的图书馆 MCP。同样必须是内网地址 |
| `NEXUS_WORKER_AGENT_GATEWAY_URL` | 空 | Worker Agent 拨入的 WebSocket。部署拒绝空值和回环 |

## 安装和配置范围

- 当前网页自动部署按 root SSH 账号执行：需要写 `/opt/nexus`、`/etc/nexus` 与 systemd 单元，没有自动 sudo 升权。预装 Python 不代表非 root 完整部署已受支持。
- 默认由控制端从华为镜像下载固定 CPython 3.11.17 源码，以固定 SHA256 校验后，在每个 Worker 的独立目录编译。引导命令不调用系统 Python；安装后验证 Python 版本与 SSL、venv、ensurepip、sqlite3、ctypes、lzma。已有可用受管解释器直接复用，不改系统 Python 或其软链接。Worker Agent 自身仍需要远端 Python 运行。
- 显式配置 `NEXUS_REMOTE_PYTHON` 时只验证，不自动换解释器。管理员需要提供完整的 Python 3.11+，不完整时先修复指定解释器再重试。源码构建需要 C 编译器及 OpenSSL ≥1.1.1 等开发库；CentOS 7 自带 OpenSSL 通常不足，需管理员提供支持的开发库或预装完整解释器。不会替换系统 OpenSSL 或关闭 TLS。
- 系统依赖使用管理员既有的 apt/yum 源，不改全局源；华为语言镜像不可达、内容缺失或校验失败会明确终止，不回退到其他公网源。安装中的旧配置若显式设置了 `NEXUS_REMOTE_NPM_REGISTRY` / `NEXUS_REMOTE_PIP_INDEX_URL`，需在 `ops/env/.env` 同步改成上述华为地址并重启 Nexus，默认值不会覆盖显式配置。
- 在此前提下支持 x86_64 / aarch64 的 Debian/Ubuntu、RHEL 系列。OpenCode 统一使用 `npm install --global --prefix "$HOME/.local/share/nexus/workers/<环境 id>/npm" --registry <配置的镜像> --include=optional opencode-ai@<指定版本>` 安装，npm 验证包完整性，实际可执行版本必须匹配。仅验证成功后才将员工配置的执行路径链接到 npm 安装目录；旧的独立二进制即使版本相同，也会迁移到 npm 管理。已满足要求的 npm 安装会复用。
- 内嵌工作区前端只提供上游 `v1.18.35`。历史 Session 仍保留当时写入的版本号；该版本没有界面包时页面显示「版本未知/界面已退役」，不会改写成 1.18.35。
- CentOS 7 使用临时华为 Vault repo 安装依赖，不改原 yum 源。旧 glibc x86_64 使用华为 Node musl 构建与独立 Alpine musl/C++ 运行库，不替换系统 glibc。该路径仍需真实公司系统验收；aarch64 的旧 glibc 不受支持。
- Git 最低要求 2.18，已有 2.18/2.19 直接使用。低于门槛时提示管理员升级，不自动编译或覆盖全局 Git。公司口述的“1.18/1.19”应以 `git --version` 实际输出确认，不能据此宣称 Git 1.x 受支持。
- 工程目录为空时初始化 Git 和初始提交；非空且不是 Git 仓库时拒绝，已有仓库保留原内容。
- 在 `/etc/nexus/opencode/<环境 id>/opencode.jsonc` 合并 `nexus-gateway` provider、默认模型和 `nexus-library` MCP。`nexus-opencode` 单元用 `OPENCODE_CONFIG` 指向这份文件，并用 `OPENCODE_CONFIG_DIR` 指向同一目录，技能从那里的 `skill/` 加载。通道切换和能力发现也读这份文件。不写用户全局 `~/.config/opencode/opencode.jsonc`，也不改 `XDG_CONFIG_HOME`。写入前备份为 `.nexus-backup-<操作ID>`，配置及备份权限为 0600。合法配置按托管范围合并；无效配置或托管字段冲突时拒绝覆盖，需人工处理。安装缓存放在 `~/.cache/nexus-provision`。
- 和 v0.9 共用同一个 OS 用户时，v0.9 的用户全局 OpenCode 配置仍会叠加进 Nexus。推荐每个产品使用专用的 OS 用户。
- 平台 API 不回显 SSH 密码，维护日志隐藏服务密钥；远端全局配置含访问凭据，请保护该 Linux 账号和配置备份。

## 维护与恢复

部署或重启期间不能再次维护、修改或删除该员工、检测环境或启动任务。有占用任务、待确认提交、未结束 Runtime Session 或忙碌的原生 Session 时拒绝维护。Worker 在线时，重启下发给 Agent，不打开 SSH。Worker 离线时，用 SSH 重启 OpenCode 和 Agent 单元。安装成功后 SSH 密码留在环境行上。OpenCode 监听 `127.0.0.1` 和环境上的端口，默认 14096。

界面可显式取消操作；关闭页面不取消后台维护。引导步骤与工作区事务纳入既有维护租约，在阶段边界检查取消；运行中的命令有超时，已执行的系统安装不会自动回滚。远端文件锁防止同环境并发安装。失败或取消后先查看失败阶段再显式重试；服务进程崩溃遗留的操作租约到期后可重试。不要同时对同一 Linux 用户的多个员工部署。部署结束会检查 Serve、模型中转认证及图书馆 MCP，模型实际生成还取决于中转上游可用性。

## 控制端与远端的边界

网页仍提交同一套持久化维护操作。Nexus Python 负责 SSH 连接、环境引导、阶段进度和 Worker 装配；远端运行 Worker Agent、独立 Python、Node 与 OpenCode。Git/配置事务仍由受文件锁保护的 Shell 完成，不因移走 Python 引导而拆散占用保护。不是把控制机的 Python 当作远端的运行时。

首次源码编译会较慢，默认总超时为 1200 秒，可按机器性能调整到最多 3600 秒。失败后查看具体阶段；不要替换系统 Python 以绕过错误。此文档描述代码契约，开发机镜像可达不代表公司网络已验收。
