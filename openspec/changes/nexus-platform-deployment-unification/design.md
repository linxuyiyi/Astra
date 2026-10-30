## Context

旧安装器把本地开发启动器、systemd、Redis Docker 和后台启动组合，未纳入 Nginx，重复写环境文件，端口或代码失败可被弱化为警告。

## Goals / Non-Goals

平台单入口、单运行配置、单进程登记。只重构平台部署；Worker 远程逻辑保持现状。AgentScope 官方独立数据库和 Redis 不变，部署器只调用迁移入口。

## Decisions

- 保留 install_nexus.sh 作为极薄 bootstrap，执行标准库 Python 部署器；依赖与 Nginx 用配置化命令准备，后台服务复用 procman 身份原语，不引入新框架。
- 运行配置为 state/nexus.env；旧 ops/env/.env、nexus.env、secrets.env 首次导入必须比较差异，进程环境最高优先级但冲突仍明确确认。生成配置引用同一文件，禁止 Shell source 用户输入。
- 版本及源放 JSON 清单，两种来源模板共用版本。OS 包沿用管理员仓库；显式配置的源不可达即失败。
- Nginx 前台运行并纳入受管身份；使用独立完整配置，不擅自接管已有外部 Nginx。冲突时交互修正端口或中止。HTTPS 证书可发现复用，缺失则提问路径或明确选择自签。
- 先验证、构建、备份配置再停止业务进程与迁移，随后以统一环境启动、逐项健康检查、核对进程与 Celery ping。失败保留现场，不能保证数据库迁移回滚。

## Risks / Trade-offs

旧 systemd 自动拉起的服务须由管理员先停用，部署器不调用 systemctl。无新增守护框架，进程异常或容器重启后执行同一命令恢复，编排平台负责容器生命周期。当前 macOS 只能做隔离的组件端到端，Linux 全栈和内网仍需真实环境验收。
