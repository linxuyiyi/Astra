## ADDED Requirements

### Requirement: 无 systemd 容器部署和重启
系统 SHALL 在受支持的 Linux 容器中部署和重启 Worker 与 OpenCode，保留现有维护租约、占用和原生空闲检查。后台进程 MUST 不阻塞 SSH，MUST 只停止身份已确认的受管进程，不得按模糊名称或裸 PID 杀进程。重复部署 MUST 复用有效配置和 token。

#### Scenario: 无 systemd 的首次和重复部署
- **WHEN** 远端 PID 1 不是 systemd，管理员部署 Worker
- **THEN** Worker 与 OpenCode 脱离 SSH 会话运行，重复同配置部署不重启健康进程

#### Scenario: 进程标识过期
- **WHEN** 保存的进程标识已被复用或端口由其他进程占用
- **THEN** 系统拒绝停止该进程，维护不能宣告成功

### Requirement: 统一入口与 TLS 验证
系统 SHALL 将 HTTP(S) Worker 入口用于相应 WS(S) 控制连接及 HTTP(S) 数据请求。代理 SHALL 支持 WebSocket Upgrade，客户端 MUST 保留服务器证书和主机名验证。

#### Scenario: HTTPS 入口
- **WHEN** Worker 入口配置为 HTTPS
- **THEN** 控制连接使用 WSS，数据通道使用 HTTPS，未受信任证书被拒绝

### Requirement: 真实运行配置和模型约束
系统 SHALL 为启动及重启的 OpenCode 传入受管配置路径和安全 Git 环境，MUST 不传 Worker token。切换通道 SHALL 有界等待运行配置就绪，MUST 不以磁盘配置代替运行配置核验。受管主模型和 small model SHALL 只走 Nexus 网关。

#### Scenario: 已运行的 OpenCode 切换
- **WHEN** 空闲 OpenCode 已有监听且需要重载
- **THEN** 系统停止后启动新进程，并核对运行配置中的真实 Gateway/MCP 地址

#### Scenario: 配置端点延迟或缺字段
- **WHEN** 健康端点就绪但运行配置暂时不可读或缺少地址
- **THEN** 系统在时限内等待；持续缺失时拒绝宣告已核验

### Requirement: Python 引导
系统 SHALL 将早期配置解析与 Worker Python 3.11+ 分离；显式配置远端 Python 源码包时 SHALL 支持在受管目录构建并校验解释器，失败 SHALL 给出具体原因。

#### Scenario: 目标解释器尚未安装
- **WHEN** 远端已有可用于引导的 Python，但目标解释器不存在且配置源码包
- **THEN** 系统能够先读取配置再构建 Python，不得误报源码包未配置

### Requirement: 健康并发保护
系统 SHALL 在 MySQL 与开发 SQLite 中持久化真实健康观测，MUST 拒绝覆盖网络探测期间发生的配置、健康或 Worker epoch 变化，MUST 不依赖 JSON 与 SQL 字符串隐式等值比较。

#### Scenario: 健康写入发生竞争
- **WHEN** 探测期间另一个事务更新环境或 Worker 重连
- **THEN** 陈旧结果不覆盖新状态
