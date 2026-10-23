# Nexus Worker SSH 部署核验（2026-10-23）

## 原因与改动

基线为主 checkout `v1.4.8` / `c3bd9c81e`。改动在独立 worktree `/Users/niuniu/.codex/worktrees/worker-ssh-deploy/Astra`，分支 `codex/worker-ssh-deploy`；未覆盖主 checkout 未提交内容。本记录保留实现阶段的测试基线，发布状态以 Git 记录为准。

原代码要求远端预装 Python >=3.11；Git <2.20 会由 root 从 kernel.org 编译 2.43.7；Node、旧 glibc 构建、Alpine 与 pip/npm 来源分散。Nexus 原本已有 Python SSH 控制入口，问题在于上传的整体安装脚本及前置探测过早依赖远端 python3。

本次新增控制端 `remote_bootstrap.py`，经 SSH 准备并校验独立 Python 3.11.17，安装/探测使用选定解释器，离线重启使用 Agent venv。Python、Node、npm、pip、Alpine 和 CentOS 临时归档源默认采用华为；不改管理员全局包管理源。现有 Git >=2.18 直接使用，低版本明确拒绝，不再自动覆盖 Git。替换工作区读取的 `--show-current` 和 `--path-format=absolute`，保留分支、detached HEAD 和仓库名返回行为。新增配置进入安装器运行环境持久化清单。

## 验证结果

| 检查 | 本轮证据 |
| --- | --- |
| Linux / 真实 Git 2.18.0 回归 | Ubuntu 24.04 aarch64，8 个相关测试文件，**212 passed**，17.24 秒，exit 0 |
| macOS 同组回归 | **200 passed / 12 skipped**，22.64 秒；跳过项均有 Linux 条件，包括安装器与 openat2 文件命令 |
| Python 实际引导 | 从华为下载固定源码并校验 SHA256，编译后版本 3.11.17，SSL/venv/ensurepip/sqlite3/ctypes/lzma 导入通过 |
| 无系统 Python | 在隔离 PATH 屏蔽 python、python3 及 python3.6～3.14 命令（统一 exit 127），另一独立目录仍编译成功，输出 `BUILD_WITHOUT_SYSTEM_PYTHON=OK` |
| 重复引导 | 已完成的解释器在 10 秒超时内复用成功，没有重新编译 |
| Node / OpenCode | 使用安装脚本中的相同步骤，从华为下载并核验 Node v22.23.3，npm 经华为安装 OpenCode 1.18.35，两个实际版本通过 |
| Worker Agent | 在新编译 Python 的独立 venv，通过华为 PyPI 安装当前 wheel 与依赖，`app.worker_agent.connection` 导入通过 |
| 配置重启恢复 | 新测试以干净环境读取生成的运行配置，镜像地址、解释器及超时恢复一致 |
| 静态检查 | `ruff check app tests`、两个安装脚本 `bash -n`、OpenSpec strict validate、`git diff --check` 均通过 |

Linux 测试使用单独的 `/tmp/nexus-worker-bootstrap-20261023.7RxnRO`，没有为本变更部署或重启真实 Worker 服务。Git 2.18.0 是隔离测试工具，从官方源码编译，未覆盖系统 Git；该测试构建不含 HTTP/OpenSSL 支持，验证范围为本地 Git 命令、clone/fetch、worktree 与配置流程，未证明公司 Git 的 HTTPS/TLS 能力。默认产品安装流程没有新增该源码下载。

## Scenario 对照

| 场景 | 实现与验证 |
| --- | --- |
| 系统 Python 缺失或过低 | `prepare_python` 选择独立目录；无系统 Python 的真实构建及 `test_missing_system_python_uses_private_bootstrap` |
| 已完成环境复用 | 解释器模块探测与系统工具快速路径；真实重复引导及 `test_redeploy_does_not_build_available_managed_python` |
| 指定解释器不可用 | 在系统依赖、工作区、配置和密钥变更前失败；对应明确失败测试 |
| 默认华为镜像安装 | Settings 默认值、下载根地址、真实 Python/Node/npm/pip 安装 |
| 镜像缺失或完整性失败 | curl fail/有限重试、Python 固定 SHA256 与 Node/Alpine 清单；真实子进程校验失败不解包且清理不完整 Python 的测试 |
| Git 2.19 复用 | 版本边界测试接受 2.19；更低的真实 2.18.0 完成整组 212 项回归 |
| Git 低于 2.18 | 边界测试拒绝 2.17.9 和 1.19.0；无自动 Git 下载/安装分支 |
| 无解释器时已有监听 | Linux 真实 socket 测试证明在包安装前阻止引导；已有 Agent 的原生检查与 busy 阻止测试 |
| 租约丢失或取消 | 阶段边界调用现有安全检查，lease loss 不执行后续编译；既有维护与取消回归 |

6/6 实施任务完成，4 项 Requirement 均有实现与验证。按 `openspec-verify-change` 检查完整性、正确性与设计一致性，无阻塞实现缺口；保留以下环境验收警告，不归档为生产验收完成。

## 剩余风险与边界

- 尚未连接公司执行机，无法从开发机/QA 的镜像可达性推出公司网络可达；没有进行网页到真实 Worker 注册、调度及模型执行的完整 E2E。
- 旧 glibc x86_64 改用华为实际提供的 Node musl 包并复用现有 Alpine 运行库装配，尚未在对应架构/公司 EulerOS 或 CentOS 系统实机验收。
- 编译 Python 仍需 C 工具链和 OpenSSL >=1.1.1 开发库；旧 CentOS 7 默认 OpenSSL 往往不满足。此时明确失败，不替换系统 Python/OpenSSL，不关闭 TLS。可由管理员提供完整解释器。
- 本次保留受锁保护的 Git/配置 Shell 事务，环境引导移到控制端 Python；未声称所有 Shell 已改写为 Python。Worker 仍需要远端运行时。
- 全新源码编译耗时较长，使用既有总部署超时。已有显式 npm/pip 配置需要同步改为华为值，默认值不覆盖管理员配置。
- 当前完整自动部署仍要求 root SSH，未增加 sudo/非 root 支持。无前端代码变更，未运行前端构建或浏览器 E2E；未做 MySQL、完整平台全套测试或生产验收。

## 发布前集成复测

按用户要求发布到 `origin/v1.4.8`。本次提交已无冲突接到远端基线 `55e21fbf6`（包含原基线之后的 5 个提交）之后，保留远端已有改动。提交前对本次 20 个暂存文件扫描私钥及常见真实凭据格式，未发现命中。

在集成源码上重新执行原 8 组测试，并增加远端新引入的 `test_unified_worker_auth.py`：Linux aarch64 / 真实 Git 2.18.0 为 **219 passed**（17.32 秒）；macOS 为 **207 passed / 12 skipped**（23.69 秒）。`ruff check app tests`、OpenSpec strict validation、Shell 语法与 Git diff 检查通过。以上为发布前源码复测，未改变上述公司环境与生产验收边界。
