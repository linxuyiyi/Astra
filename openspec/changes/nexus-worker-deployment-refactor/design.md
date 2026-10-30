## Context

最终要求是服务端 Python + Paramiko/SFTP 主导全部 Worker 部署与进程启停，彻底移除 systemd。此前临时加入的容器处理只保留身份核对、隔离和安全环境等有效机制。

## Goals / Non-Goals

控制与决策只在 Nexus：系统探测、选择依赖、选择固定制品、Git 判断、配置合并、进程策略和验收。远端只执行逐条命令和短小固定原语；不引入远端部署代理或进程框架。不改变 Task → Run → Employee → Worker → OpenCode Session 的事实源。

## Decisions

- `RemoteProvisioner` 仍为唯一持久化维护入口。配套 Python 模块分别实现目标探测、制品校验、Git/配置、全局源配置与进程控制；不是另一套状态机。删除 remote_install.sh、临时远端包安装器与 systemd 路径。
- SSH 获取操作系统、架构、root 权限、HOME、libc、现有工具和解释器。服务端选择复用或安装；包管理器使用管理员现有 OS 源。无足够能力时清楚失败，不猜公司仓库、不植入 Alpine/musl loader。
- Python 优先显式、已有受管或可用系统解释器；需要时才下载固定源码、校验摘要并逐条执行 configure/make/install。Node 只下载目标 glibc 架构的固定包，其他 libc 要求显式可运行 Node。
- 包下载由目标机执行 curl。服务端读取远端 metadata、选择单个平台包、验证来源和摘要，再下发安全提取和运行版本检查。拒绝重定向到未知位置，不运行 opencode-ai 的 postinstall，不启动隐式 registry 重试。
- 全局源写入 SSH 用户 HOME 的 pip.conf/.npmrc；同源配置不改，冲突明确报错。两种服务进程都显式使用该 HOME。全局值是默认路由，不阻止有意传入不同命令参数，不能替代公司出站网络策略。
- 双重 flock 通过保持 stdin 打开的 SSH channel 持有，断开后由 OS 自动释放。进程启动和停止由 Nexus 根据远端 /proc 身份与端口所有权决定；目标端短小原语只执行再次核对后的信号、spawn 和原子记录。
- 进程没有额外守护框架。Worker 自身保持现有重连；OpenCode/Worker 异常退出或容器整体重启后需由平台显式重启。自动随容器启动属于入口或编排平台策略，本次不伪装成系统服务。

## Risks / Trade-offs

公司镜像当前 TLS 不可达；路径资料、命名规则和实测结论分别记录。仓库保存的远端 SSH 连接尝试发生 BadHostKeyException，未覆盖已记录密钥，无法完成真实公司安装验收。旧 systemd/手工进程不自动接管，需管理员确认停止后重新部署。
