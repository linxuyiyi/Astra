# V1.4.9 Linux Agent Worker 部署

Agent Worker 部署、更新和重启全部由 Nexus 服务端 Python 经 Paramiko SSH 控制。远端仅执行命令和短小的探测、校验、解包、信号及启动原语，SFTP 传输 wheel 和配置；不上传复杂安装脚本，不安装、探测或调用 systemd，不因 Worker 在线而改走另一套重启路径。

在「Agent Worker」选择 Linux 远程，填写 SSH、root 账号和绝对工作区。目标支持 Linux x86_64/aarch64，需要 timeout、SSH 及到所配镜像、Nexus 网关、Library MCP 和 Worker Gateway 的网络。缺失依赖通过目标既有 apt/dnf/yum 源安装，不改系统源，无自动 sudo 或系统运行库补丁。

## 执行流程

1. 创建持久化维护操作和租约，检查任务占用、待确认提交和原生 Runtime 空闲；连接 SSH 并遵守主机密钥校验，探测系统、架构、权限、HOME、libc、工具及软件版本。
2. 取得 Worker 与规范工作区两把 flock；锁由保持 stdin 的 SSH channel 持有，连接关闭后释放。写入及启停前重新检查租约、取消和锁存活。
3. 验证显式 Python，或复用受管/系统完整 Python >=3.11，含 SSL、venv、ensurepip、sqlite3、ctypes、lzma 和 tarfile 安全过滤。无可用解释器才从目标机下载固定 3.11.17 源码、校验固定 SHA256、逐条构建；不替换系统 Python。显式解释器无效直接失败。
4. 复用匹配版本 Node，否则安装目标架构固定 22.23.3 制品并验证 SHA256。普通制品要求 glibc >=2.28；其他 libc 要求显式提供可运行 Node，不注入 Alpine/musl loader。OpenCode 默认 1.18.35，直接取得目标原生 npm 平台包，验证来源、SHA512 与实际版本，不执行 opencode-ai postinstall 或其嵌套 npm 重试。
5. 诊断并持久化部署用户的全局 pip/npm 默认源。检查 Git >=2.18，服务端判断工作区/origin/分支及干净状态，仅允许干净仓库快进；保留现有业务数据。仓库 token 通过临时 0600 文件和 askpass 使用后清理。
6. 服务端合并并备份受管 provider、MCP、模型与技能配置，保留人工及无关字段；冲突或无效配置拒绝覆盖。上传 Worker wheel，复用未变化的 venv 和 token，写入私有环境文件。
7. Nexus 通过 SSH 启动受管 OpenCode，等待真实 `/global/health` 后启动 Worker；验收实际版本、网关鉴权、MCP 和 Worker 回连后才报告成功。部署健康不等于模型生成任务成功。

## 来源与全局包配置

来源事实集中在 `app/config/worker_deployment.py`，没有 Shell 重复硬编码。平台统一安装器会为新配置填入所选来源；已有独立 Worker 源设置保留。可独立配置：

| 配置 | Worker 原始默认 | 验证等级 |
| --- | --- | --- |
| `NEXUS_REMOTE_PYTHON_SOURCE_URL` | `https://mirrors.tools.huawei.com/python/3.11.17/Python-3.11.17.tar.xz` | 官方资料支持 `/python` 根路径，固定文件按上游规则推导，内网未验证 |
| `NEXUS_REMOTE_NODE_BASE_URL` | `https://mirrors.tools.huawei.com/nodejs` | 官方资料支持根路径，追加 `/v22.23.3/` 和架构包，内网未验证 |
| `NEXUS_REMOTE_NPM_REGISTRY` | `https://mirrors.tools.huawei.com/npm/` | 官方文档列出，当前网络 TLS 失败 |
| `NEXUS_REMOTE_PIP_INDEX_URL` | `https://mirrors.tools.huawei.com/pypi/simple` | 官方文档列出，当前网络 TLS 失败 |

上述路径依据：[华为 CodeArts](https://support.huaweicloud.com/usermanual-codeci/codeci_01_0008.html)、[华为 PyPI 配置](https://support.huaweicloud.com/intl/en-us/bestpractice-cce/cce_bestpractice_10079.html)。官方上游 Python/Node 制品已实际下载校验，但不是部署备用源。系统包仓库的华为内网位置未确认，代码不拼接猜测路径。

`NEXUS_REMOTE_DOWNLOAD_BASE_URL` 已停用，非空值会要求迁移为独立来源。`NEXUS_REMOTE_NPM_ARTIFACT_HOSTS` 默认空，仅允许同 registry 主机制品；其他内网制品主机须确认后显式登记。下载发生在目标机，DNS/TLS/HTTP/重定向或摘要失败明确中止，不自动切公网、不关闭证书校验；HTTP 只能显式配置。

读取系统、用户及环境覆盖配置后，缺失默认源写入 `~/.config/pip/pip.conf`、`~/.npmrc`；同源已有配置不改，不同源、额外索引、find-links 或禁用 TLS 等冲突明确报错。OpenCode 与 Worker 统一使用部署用户 HOME，后续普通 pip/npm 命令能读取默认源。默认值不能阻止用户有意指定其他地址或包脚本自行联网，严格限制由公司网络策略保证。

## 进程管理与恢复

OpenCode 只监听 `127.0.0.1:<环境端口>`，默认 14096。配置在 `/etc/nexus/opencode/<id>/opencode.jsonc`，环境文件 `/etc/nexus/{opencode,worker}-<id>.env` 权限 0600；OpenCode 环境剥离 Worker 凭据。进程身份与日志在 `/opt/nexus/workers/<id>/{opencode,worker}-process.{json,log}`，每个 Worker 独立安装、venv 和目录。

独立进程组、关闭 stdin、日志重定向保证 SSH 结束后继续运行。停止前核对 PID、进程组、启动时间、boot ID、命令、工作区及监听归属；发送信号原语再次确认身份。PID 复用、未知端口、无效记录或在线 Worker 缺记录均拒绝接管，不能按进程名或裸 PID 强杀。同配置重复部署复用安装、token 和进程，配置或 wheel 改变才重启相关进程。

旧 systemd/手工进程须管理员确认停止并取消其自启后重新部署，Nexus 不调用 systemctl。无额外进程守护框架；异常退出或容器重启后由平台 SSH 重启，自动拉起由容器入口/编排平台承担。容器重建应持久化 `/opt/nexus/workers`、`/etc/nexus`、用户 `.local/share/nexus/workers`、全局包源配置及工作区。

默认总维护超时 1200 秒，可配置至最多 3600 秒。失败保留日志、配置和制品备份；系统包安装不整体回滚，修复后显式重试。当前真实 SSH 尝试遇到主机密钥不匹配，未绕过校验，尚无公司 Worker 安装/启停/回连的端到端成功证据。
