## Why

公司确认系统镜像域名为 mirrors.tools.huawei.com，现有一键部署却把多个软件源拼在 repo.huaweicloud.com 下，并为旧系统自动引入共享 musl 运行库。需要按可验证来源和目标能力重整部署契约，避免错误源、隐式公网访问和跨 Worker 的系统改造。

## What Changes

- 独立配置 Python 源码、Node.js 分发、npm 和 PyPI；统一由控制端解析为部署清单，Shell 不再拼接公共镜像根。
- 优先复用已验证的目标 Python；缺失时按固定版本与摘要构建独立 Python。系统依赖使用管理员现有 apt/dnf/yum 仓库，不猜公司系统仓库路径。
- 正常 glibc x86_64/aarch64 使用受管 Node；旧 glibc 或 musl 使用显式预装、验证过的 Node，不自动安装 Alpine 或修改共享 loader。
- 限制安装工具继承的源配置、重定向和 registry 包地址，明确失败阶段与原因；保留任务门禁、工作区保护和真实运行验收；移除 systemd，统一 SSH 进程控制。
- 提供可复现的源检查与回归证据，对内网不可访问内容标为待验证。

## Capabilities

### New Capabilities
- `nexus-worker-ssh-deployment-control`: 服务端决策、逐步执行、远端锁与统一进程管理。
- `nexus-worker-deployment-sources`: 下载源清单、失败透明性和无隐式公网回退。
- `nexus-worker-runtime-installation`: 按目标能力安装 Python/Node 与执行部署验收。

### Modified Capabilities
无；历史 Astra Spec 不修改，上一轮容器支持原样保留并纳入回归。

## Impact

涉及统一服务的 Worker SSH 部署模块、远端安装脚本、配置项、安装器配置持久化、测试和部署说明。新增服务端 SSH 部署控制 capability，不改变任务生命周期、Agent 内部编排、API 或数据库。
