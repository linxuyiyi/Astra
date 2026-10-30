# Nexus·数字空间 V1.4.9 统一部署

首次安装、代码升级和容器重启后的恢复使用同一入口。在目标 Linux 的 `v1.4.9` Git checkout 中执行：

```bash
sudo bash ops/deploy/install_nexus.sh
```

公司内网首次部署显式选择来源配置，后续仍执行同一条命令即可：

```bash
sudo bash ops/deploy/install_nexus.sh --sources ops/deploy/sources/huawei.json
```

源配置会持久化，后续不带 `--sources` 也会复用。默认新部署使用 `sources/public.json`；两份来源模板共用 `sources/versions.json` 的版本与摘要。没有开发/生产模式选择，没有 systemd 分支。代码仓库应先克隆到目标机器，安装器仅对当前分支做 fetch 和严格快进，拒绝脏工作区、分叉或未发布提交；代码变化后重新执行新版本部署程序。

## 环境与交互

支持 root、Linux x86_64/aarch64，至少 2 CPU、1900 MiB 内存和 10 GiB 可用磁盘，建议 4 GiB 以上内存。极薄入口通过管理员现有 OS 仓库准备 bootstrap Python 和 Git；控制程序要求 Python >=3.8，业务运行时要求完整 Python >=3.11。优先复用合格 Python 和固定版本 Node，否则根据源清单下载并验证 SHA256 后安装到状态目录，不修改系统解释器或运行库。默认受管 Node 需要 glibc >=2.28；其他系统需要显式可运行 Node，真正不支持的环境明确失败。

自动探测系统、容器、架构、权限、资源、IPv4、软件版本、已有配置和端口。新部署优先选择可绑定的七段 IPv4；部署完成还通过生成地址实际访问入口。本机验证不证明远程防火墙、路由或其他机器已经可达。

首次部署无需复制或手改环境文件。无法确定的信息由程序提问：

- Nexus 与 AgentScope 两个独立数据库连接；公司使用 DBA 已创建并授权的 MySQL 8.0.36 数据库，URL 为 `mysql+asyncmy://用户:编码密码@主机:3306/库名?charset=utf8mb4`。不自动创建 DBA 用户或数据库，不导入旧库。
- Redis：发现本机监听时验证其真实连接，否则询问已有服务 URL；不会把一个 TCP 监听当成 Redis 成功。AgentScope 当前接受 default 用户的 `redis://`，不伪装支持其他认证/TLS 组合。
- Worker HTTP 入口允许网段，禁止 `0.0.0.0/0`，不猜测公司边界。
- HTTPS 证书：优先验证并复用配置或已发现的 Nginx 证书；缺失、过期、地址或私钥不匹配时，可输入路径，或明确确认生成自签证书。自签证书需由访问方信任；安装器不会关闭 TLS 验证。

非交互运行必须通过 `--env-file` 提供必要信息；出现需要确认的冲突则失败，不默认同意覆盖。`ops/env/.env.example` 是可选高级配置参考，不是必须填写的步骤。上游厂商节点、模型路由继续在网关控制面维护，部署器不收集厂商密钥。

## 配置来源与保护

默认状态目录 `/var/lib/nexus`，可用 `--state` 或 `NEXUS_STATE_DIR` 指定。运行配置唯一来源为该目录的 `nexus.env`，权限 0600；服务启动器安全解析后传给所有子进程，Settings 显式读取同一路径，不再混入发布目录内的开发 `.env`。

首次迁移会检查 checkout 的 `ops/env/.env`、旧 `secrets.env` 和旧运行环境；新格式生成后只读统一运行文件。显式 `--env-file` 和当前进程环境作为覆盖输入，但有不同值仍须确认。重复定义、来源冲突、过期参数、未知参数、相对数据路径、拓扑错误和真实连接失败均会诊断，敏感值脱敏。提供保留、修改或重新初始化的选择；保留无效值会停止部署，不能绕过验收。

有效密钥、SQL URL、扩展业务配置均复用。数据库加密主密钥无效时须恢复原值，禁止通过删除再生。修改文件前创建带时间戳的 0600 备份，原子替换，不删除数据库和已有工作区。旧相对数据目录须确认转换为原 checkout 下的绝对位置，避免切换发布目录后误用新空目录。

源配置独立保存在 `sources.json`。Python、Node、pip、npm 地址可分别指定；访问失败报告 TLS/HTTP/重定向或摘要问题，可明确输入修正地址，不自动改用公网。OS 依赖默认沿用管理员仓库；可在源清单的 `os_repository_dir` 指定已有系统仓库配置目录，此时仅使用该目录，避免混入其他 OS 仓库。公司 OS 镜像具体路径未确认，模板不编造它们。

华为模板的根地址为 `mirrors.tools.huawei.com`，具体路径及验证等级见本次核验报告。默认 Python 3.11.17、Node 22.23.3。实际 bootstrap/复用解释器版本也会记录。自定义版本须同步提供正确制品地址与两种架构摘要，并作为新发布提交；不在同一已准备发布中修改运行工具版本。

## 发布和服务管理

流程为：系统与代码检查 → 配置诊断 → 来源/依赖检查 → 独立发布快照与虚拟环境准备 → SQL/Redis/Settings 验证 → 聊天岛和 Web 构建 → 证书及 `nginx -t` → 端口归属检查 → 停止旧进程 → Nexus Alembic 与 AgentScope 官方迁移 → 网关平台 Key 登记 → 启动全部服务 → 健康、实际发布版本及入口验收。

发布快照在 `releases/<Git提交>/repo`，通过 `git archive` 生成，包含独立 venv 与构建产物，不修改在线旧发布。相同代码已完成的构建可复用。npm 使用指定 registry、锁文件校验和禁止安装生命周期脚本，随后显式执行仓库构建命令；pip 安装和隔离构建使用同一索引。地址默认配置不替代公司网络出站策略。

唯一 Python 进程清单管理以下 11 个进程：AgentScope、网关、Nexus API、Library/Task/Schedule MCP、三个 Celery Worker、Celery Beat、Nginx。复用 `ops/dev/procman.py` 的进程组原语，记录 PID、创建时间、进程组、工作目录和发布提交；停止前重新核对，等待旧进程与端口退出再启动。已有正确的 Python 进程记录可迁移；未知进程/端口拒绝接管，可交互修改端口。旧 systemd 自启服务需管理员先停用，安装器不调用 systemctl。

验收包括所有 HTTP/MCP 健康、进程身份与发布环境、Nginx 实际配置响应版本、三条 Celery 队列 ping、HTTPS、Worker HTTP 管理路径隔离及网关鉴权。任意失败返回非零并显示阶段，不把部分启动算成功。模型路由未配置时不验证真实模型生成任务。

```bash
# 同一入口的运维操作，不是另一套部署方案
sudo bash ops/deploy/install_nexus.sh --action health
sudo bash ops/deploy/install_nexus.sh --action stop
```

没有新增守护框架；异常退出或容器重启后执行同一部署命令恢复。容器入口/编排平台负责自动拉起策略。须持久化状态目录、SQL/Redis 数据和既有工作区。部署失败保留进程记录、日志、旧发布与配置备份；修复后重新执行标准命令。系统包安装和数据库迁移不能承诺整体回滚。

## 网络拓扑

| 入口/服务 | 默认地址 | 访问范围 |
| --- | --- | --- |
| Web / API / 网关 / WebSocket / SSE | `https://主IP:443` | Nginx HTTPS，代理到同机回环服务 |
| Worker 专用入口 | `http://主IP:18130` | 指定 CIDR；仅 `/v1/`、`/mcp`、精确 Worker WebSocket 连接路径，保留鉴权，其余 404 |
| Nexus API / 网关 | `127.0.0.1:18120` / `127.0.0.1:18110` | 同容器内部 |
| AgentScope | `127.0.0.1:18000` | 同容器内部 |
| Library / Task / Schedule MCP | `127.0.0.1:18201/18202/18203` | 同容器内部，Library 经专用入口提供给 Worker |

Nginx 使用部署器生成的完整配置，前台受管运行，不覆盖系统 Nginx 配置。已有外部 Nginx 占用入口时，明确处理冲突后才继续。平台自动生成非回环 Worker 地址；HTTPS 可通过 `NEXUS_DEPLOY_PUBLIC_HOST` 使用与证书匹配的域名。

## 验证边界

本机隔离验证覆盖真实 Nginx TLS/WebSocket/SSE/ACL、11 个服务启停、SQLite 迁移、独立 Redis 和队列回包；不等于 Linux root 首次安装、MySQL 8.0.36 或公司内网验收。当前公司镜像连接失败，远端 SSH 主机密钥不匹配，也未完成真实 Worker 回连。详细命令和结果见核验报告。开发辅助启动脚本和官方 UI 构建工具保留用于开发/制品维护，不是平台部署入口。
