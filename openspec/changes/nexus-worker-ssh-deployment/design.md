## Context

已有 RemoteProvisioner 在 Nexus 进程通过 SSH 控制远端，维护操作与租约是唯一控制事实源。当前安装器及其前置原生状态检查却均依赖远端 python3；Worker Agent 本身也要求 Python 3.11。目标不是让远端零运行时，而是摆脱其原始 Python 版本。

## Goals / Non-Goals

目标：新增控制端 Python 引导模块，以有限的 SSH 命令安装独立 CPython，默认使用华为源，复用已有维护安全边界。保留网页入口。
非目标：替换 Worker 运行协议、引入第二套部署状态机、修改系统 Python、包管理器全局源或系统 glibc。

## Decisions

- 空 NEXUS_REMOTE_PYTHON 选择每 Worker 独立 CPython 3.11.17；显式配置只校验且不静默替换。源码包与 SHA256 均来自华为，固定构建产物版本；安装到最终版本目录，失败不触碰已有解释器。Python 引导只依赖 SSH/Bash 与系统工具。
- 控制端分阶段执行依赖检查、编译和解释器验证；代码留在 Nexus。远端配置/Git 事务脚本保留作为受锁保护的原子工作区操作，避免拆散既有安全边界。
- Git 采用 2.18 门槛，不自动编译或覆盖系统 Git。工作区分支读取使用 symbolic-ref，公共 Git 目录使用 --git-common-dir 并按工作区解析相对路径；不使用 --show-current 或 --path-format。以部署、worktree、diff、fetch/merge 实际所需命令核实，不把用户口述的 1.18 当成已确认的版本。
- Node 和 Alpine 从华为下载。旧 glibc x64 使用华为实际提供的 musl Node 包，并复用已有 musl 运行库装配；不虚构 glibc-217 的华为镜像。
- 系统依赖变更前先做原生空闲检查：已有 Agent 使用其解释器读取原生事实；尚无解释器时，只在 Linux procfs 确认目标端口没有监听后开始引导，已有监听或未知一律停止。
- 引导、安装、健康检查统一使用已选解释器；离线重启使用已安装的 Agent venv，避免再次依赖 PATH 中 python3。

## Risks / Trade-offs

- 源码编译耗时：使用已有部署超时、有限并发和阶段进度；复用成功产物。
- 旧 RHEL 系列 OpenSSL 可能不足：验证 SSL 导入并失败说明；不关闭 TLS 或替换系统 OpenSSL。需要管理员提供支持的开发库或显式可用解释器。
- 安装器仍有 Shell 配置事务：本次解决运行时引导依赖，未将所有 Shell 操作改写成 Python。后续拆分必须保持锁、原生忙碌检查与幂等语义。
- 企业网络不能从开发机可达性推断：单独记录真实公司环境未验证。
