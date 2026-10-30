## ADDED Requirements

### Requirement: 按目标能力安装
系统 SHALL 在 Linux x86_64 和 aarch64 上验证权限、系统依赖与现有运行时；优先复用已验证的 Python，缺失时构建 Worker 独享 Python。正常 glibc 环境 SHALL 使用受管 Node；其他 libc 环境 SHALL 验证管理员显式提供的 Node，MUST 不自动改造全局 loader 或安装跨 Worker 共享运行库。

#### Scenario: 有可用运行时
- **WHEN** 已有 Python 或显式 Node 通过完整能力、版本检查
- **THEN** 安装复用对应运行时，不编译 Python、不覆盖系统解释器或 Node

#### Scenario: 不满足安装条件
- **WHEN** 权限不足、缺少系统依赖或 libc 不支持受管 Node
- **THEN** 在安装运行时和修改工作区前明确失败，并提示缺失条件

### Requirement: 幂等部署和验收
系统 SHALL 保留每 Worker 隔离、租约与原生空闲检查、配置备份及工作区保护；部署、更新和重启 SHALL 使用同一配置及由 Nexus 经 SSH 控制的受管进程方式。安装成功 MUST 通过运行版本、网关、MCP 和 Worker 真实状态核验。

#### Scenario: 更新失败
- **WHEN** 制品版本不符、下载失败或工作区非快进/不干净
- **THEN** 不将失败标记为成功、不破坏原有可用执行文件或用户数据

#### Scenario: 无 systemd 容器
- **WHEN** 目标没有实际运行 systemd
- **THEN** 通过独立受管进程启动并在维护后核验健康与 Worker 上线，不调用 systemctl
