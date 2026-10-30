## ADDED Requirements

### Requirement: 部署控制归属 Nexus
Nexus SHALL 通过 Python 决定环境要求、依赖、运行时安装、Git 处理和配置写入，逐步经 SSH 执行并收集退出状态。远端 MUST 不再运行承担完整部署决策的 Shell/Python 安装器；SFTP 仅传送必需的包、凭据与配置。

#### Scenario: 分阶段失败
- **WHEN** 任一步骤失败或租约失效
- **THEN** 后续安装与启动停止，返回该阶段的明确错误，不宣告整体成功

### Requirement: 无 systemd 的统一进程控制
部署、更新与重启 SHALL 始终由 Nexus 经 SSH 负责进程控制，不依赖 systemd，不因 Worker 在线而切换为另一套重启入口。系统 MUST 只停止完整身份已确认的受管进程，在进程退出与端口释放后启动新进程，并验证真实健康与 Worker 连接。

#### Scenario: 未受管的旧进程
- **WHEN** 端口由未知进程占用或在线 Worker 缺少受管进程记录
- **THEN** 平台拒绝停止或创建重复进程，要求人工确认旧部署后重新部署

#### Scenario: 重启
- **WHEN** 管理员发起重启且占用、原生空闲、租约检查通过
- **THEN** Nexus 使用保存的 SSH 信息控制进程，不调用 systemctl；缺少 SSH 信息时提前拒绝

### Requirement: 远端并发与中断保护
系统 SHALL 保持环境和工作区双重互斥，远端锁与 SSH 会话绑定；断开连接后释放，不引入常驻部署代理。

#### Scenario: 并发部署
- **WHEN** 另一个部署持有相同环境或规范工作区的锁
- **THEN** 新部署不能修改运行时、仓库或配置
