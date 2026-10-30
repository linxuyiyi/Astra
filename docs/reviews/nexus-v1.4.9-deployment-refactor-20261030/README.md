# V1.4.9 部署整改核验（2026-10-30）

## 原因与范围

原平台安装器同时维护 systemd、后台启动和开发启动器分支；配置被重写，代码快进失败仍可继续，没有统一的 Nginx 发布与全服务验收。此前 Agent Worker 远端安装还包含复杂 Shell 和兼容补丁。

本次平台改动未重新修改 Worker 远程安装模块；保留此前已完成的服务端 SSH 重构，仅共用 Settings 的明确运行环境来源。当前 checkout 为 `v1.4.9`，基线 `26ed6e06ab005de96eb41435991a9871c20b4c55`。本报告记录本地修改与测试，不表示已推送或已部署公司环境。

## 新入口与职责

唯一命令为 `sudo bash ops/deploy/install_nexus.sh`，首次和升级相同。公司首次可追加 `--sources ops/deploy/sources/huawei.json`，后续自动复用已保存来源。`--action health/stop` 为同入口运维动作。

- `install_nexus.sh`：极薄 bootstrap，仅准备控制程序所需 Python/Git，执行部署器。
- `deploy.py`：环境/容器 cgroup 配额/代码/来源/依赖探测，发布快照，构建，连接验证，迁移，Nginx 配置及完整验收。
- `configuration.py`：安全解析环境输入、重复及冲突诊断、交互纠错、地址生成、密钥复用和原子备份写入。
- `lifecycle.py`：唯一 11 进程清单，迁移可信旧 Python 进程记录，复用既有 procman 原语启停，核对实际发布和健康。
- `sources/versions.json`：统一版本、两种架构摘要；`public.json/huawei.json` 仅定义不同来源。默认使用目标已有 OS 仓库，可通过 `os_repository_dir` 限定已确认的 OS 仓库目录。

流程为系统/代码检查、配置诊断、依赖与源验证、独立发布准备、数据库和 Redis 实连、前端构建、证书/Nginx 验证、端口归属检查、停止旧进程、迁移和 Key 登记、启动全部服务、完整验收。代码更新后重新执行新程序，应用与构建产物在独立 Git 发布快照中准备，避免覆盖在线旧 venv。同代码已准备发布可复用。

平台运行配置唯一来源为状态目录 `nexus.env`；首次导入旧配置时逐项诊断，生成新格式后不再叠加 checkout 开发配置。正确值及有效密钥不重复询问，错误值保留则停止；文件修改前备份，私有文件 0600。数据库加密主密钥不能通过删除再生。已有数据目录保持绝对原位置。

内部 API、网关、AgentScope 和 MCP 绑定回环。Nginx HTTPS 代理 Web/API/模型/WebSocket/SSE；Worker 专用 HTTP 入口默认主 IP 的 18130，限制明确 CIDR，仅开放模型、Library MCP 和精确 Worker 连接路径，其余管理路径 404，原鉴权保留。优先识别可绑定七段主 IP，并以生成地址实连验证；不能据本机访问推断其他机器和防火墙已放行。

## 清理和修改文件

删除平台安装器中的 systemd 单元生成/启停/退休、systemd 探测、双启动路径、旧远程 Worker CLI、开发 Vite 发布及 Redis Docker 分支。删除静态 `ops/deploy/nginx-nexus.conf`，Nginx 只有一个动态生成器。此前 Worker 任务删除 `app/runtime/remote_install.sh`，部署/更新/重启已统一服务端 SSH，未新增远端部署框架。

完整文件清单见 [modified-files.json](modified-files.json)。平台主要修改：`ops/deploy/install_nexus.sh`、新增三个 Python 模块及 sources 清单、`ops/env/.env.example`、`app/config/settings.py`、部署指南、并行隔离检查及文档、AgentScope UI 代理说明、ASR 验证脚本的旧模板引用。开发脚本和官方 UI 制品构建工具保留，标准部署不再调用开发服务组合。

测试新增 `test_platform_deploy.py`、`test_platform_deploy_e2e.py`、部署模块加载辅助；更新 Nginx、旧服务单元、网关配置持久化、隔离默认值等旧测试。此前 Worker 测试覆盖制品、配置/Git、进程身份、占用及 SSH 锁。

## 来源核验等级

公司确认系统镜像域名为 `mirrors.tools.huawei.com`。以下是配置模板及证据等级，不是公司网络验收成功声明。

| 类别 | 华为模板 | 证据 |
| --- | --- | --- |
| Python | `/python/3.11.17/Python-3.11.17.tar.xz` | 官方文档列出 `/python` 根路径，固定文件按上游命名推导；公司文件未成功访问 |
| Node | `/nodejs/v22.23.3/` + 架构包 | 官方文档列出 `/nodejs`，固定后缀按 Node 官方发布命名推导；公司文件未成功访问 |
| npm | `https://mirrors.tools.huawei.com/npm/` | 官方文档列出；本机实际 metadata 请求 TLS 失败 |
| pip | `https://mirrors.tools.huawei.com/pypi/simple` | 官方文档列出；本机实际包索引请求 TLS 失败 |
| OS | 管理员现有仓库或显式目录 | 公司具体仓库未确认，不拼接 Ubuntu/CentOS/Vault 地址 |

路径依据为[华为 CodeArts 文档](https://support.huaweicloud.com/usermanual-codeci/codeci_01_0008.html)及[华为 PyPI 文档](https://support.huaweicloud.com/intl/en-us/bestpractice-cce/cce_bestpractice_10079.html)。当前平台部署器自身的探测结果见 [source-probes.json](source-probes.json)：公网 PyPI/npm 成功，华为两项均 TLS 失败，未切换来源。Python/Node 公司路径的此前 curl 探测同样 TLS 失败，HTTP 根路径探测为空响应，不因此启用 HTTP。

已实际从官方上游下载 Python 3.11.17 和 Node 22.23.3 Linux x64/arm64 制品，摘要与官方清单一致，见 [upstream-checksums.json](upstream-checksums.json)。这些下载仅用于调查和核对，不是公司部署的备用源。另下载并核对 Node 22.23.3 macOS arm64 包，用于本机版本一致的前端验证；Linux 原生二进制未在 macOS 执行。

Node/Python 制品摘要在版本清单统一维护；OpenCode 原生平台包的上游 metadata、包名及完整性规则已核对，内网实际包仍未下载成功。npm 安装使用明确 registry 与锁文件地址替换，pip 和隔离构建使用同一索引，不配置额外备用索引。全局默认源不替代公司网络的出站限制。

## 实际执行的验证

1. 全量服务回归：在 `services/nexus-api` 执行 `ASTRA_OPENCODE_SERVER_VERSION=1.18.35 NEXUS_TEST_NGINX=<临时官方构建二进制> .venv/bin/pytest tests/platform tests/nexus -q`，结果 **1429 passed、30 skipped**。跳过项目保持未验证；主要警告来自当前 Python 3.14 的 pytest-asyncio 弃用提示。首次发现的两项旧结构断言已更新并完成全量重跑。
2. 平台最终组件回归：**16 passed**，随后源配置保留与容器配额针对性单测 **16 passed**，含真实本地 HTTP 下载与摘要错误/重定向拒绝、真实 Git 快进/拒绝未发布提交/发布快照不变、配置备份与复用、旧 Python 进程记录迁移、真实子进程启停/PID 身份错误拒绝、命令超时停止自身子进程组。
3. 真实 Nginx：在 `/tmp` 从官方 Nginx 1.30.6 源码编译，未安装或更改系统 Nginx。实际 `nginx -t`、HTTPS、WebSocket 101、SSE 首条数据在后续数据前返回、原鉴权保留、管理路径 404、修改 ACL 后 reload 与 403 全部通过。
4. 隔离真实核心服务：临时 SQLite 两个独立库、独立 Redis 端口、临时全部服务端口，执行 Nexus Alembic、AgentScope 官方 SQL 初始化及平台 Gateway Key 登记，启动全部 11 进程；API/AgentScope/Gateway/MCP、三条 Celery 队列 ping、实际发布环境及 Nginx 响应发布标记通过。完整停止后换发布标记重新启动，确认全部 PID 更换并再次健康通过。
补充真实连接纠错验收 **2 passed**：通过部署器验证独立 SQL/Redis 连接，将 Redis 设置为关闭端口后确认失败，明确输入正确值并重新连接成功，未因配置文件已存在而跳过。

5. 在独立前端目录使用 **Node 22.23.3**，两项目分别执行 `npm ci --ignore-scripts --registry <所选来源>`、`npm run type-check`、`npm run build`，全部成功，见 [frontend-node22.json](frontend-node22.json)。测试保留仓库共享 JSON/CSS 的完整相对目录，没有改动工作区已发布前端产物。
6. `ruff check app nexus_api tests`、部署 Python Ruff、Shell 语法、Python 编译、`git diff --check` 和两项 OpenSpec change 严格校验通过。未发现部署器对 systemctl 的调用，旧安装脚本引用已清理。

测试服务全部停止，仅临时制品与日志留在 `/tmp`。现有平台、已有数据库、Redis 及公司机器没有被这些隔离测试重启或修改。

## 未完成的环境验收与恢复限制

- 当前主机为 macOS，真实业务测试使用已有 Python 3.14.3/AgentScope 2.0.10；未执行 Linux root 的标准命令首次安装、系统包准备、Python 源码构建和固定 Linux Node 启动。组件测试不是安装器整体端到端成功。
- 未在 MySQL 8.0.36 执行本次完整部署与升级；SQLite 迁移结果不能替代公司 MySQL 验收。
- 公司镜像资源尚不可达；官方根路径资料及推导后缀不证明固定公司制品存在。
- 本轮真实远端 SSH 尝试发生 `BadHostKeyException`，未覆盖已记录主机密钥，未执行目标安装/启停；真实 Worker 回连、重复部署与容器重启恢复仍未验收。
- 无新增守护框架。进程异常或容器重启后执行标准命令恢复，自动随容器启动由入口/编排平台承担。旧 systemd 自启须管理员先停用；未知进程不接管。
- 失败保留旧发布、配置备份、日志和进程记录；不承诺系统包或数据库迁移整体回滚。任何组件或入口失败均返回失败，不标记整体成功。
