## Why

远程 Worker 部署要求预装 Python 3.11，且多个安装源在公司网络不可达。控制端已有 SSH 能力，但环境准备与工作区变更集中在一个远端 Shell 脚本，失败定位困难。

## What Changes

- 由 Nexus Python 控制端分步骤通过 SSH 准备系统依赖与独立 Python，不再要求目标机预装受支持的 Python，也不替换系统解释器。
- Python、Node.js、npm、pip 及旧系统必要运行库默认使用华为镜像；保留显式解释器与软件源配置，不静默访问其他公网源。
- Git 最低门槛按真实命令需求调整为 2.18，保留公司已有 2.19；版本低于门槛时明确停止，不自动覆盖全局 Git。
- 继续复用现有维护操作、租约、占用检查、远端锁与工作区配置事务；安装控制逻辑逐步从整体 Shell 中分离。

## Capabilities

### New Capabilities

- `nexus-worker-ssh-bootstrap`: 控制端驱动的远端运行环境准备、华为镜像与 Git 门槛。

### Modified Capabilities

无。历史 Astra capability 不修改；现有 Nexus Worker 维护的安全约束继续有效。

## Impact

影响 `app/runtime/remote_provisioning.py`、远端安装脚本、运行配置、部署文档和测试。不改变 API、数据库或任务状态机。Worker Agent 的 Python 3.11+ 运行需求保留。此次不增加独立于网页维护 API 的第二套部署 CLI，也不承诺所有企业发行版均可自动编译 Python。
