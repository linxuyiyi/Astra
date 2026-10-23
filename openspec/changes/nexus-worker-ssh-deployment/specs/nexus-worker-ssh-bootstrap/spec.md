## ADDED Requirements

### Requirement: 不依赖系统 Python 的部署引导
系统 SHALL 从控制端准备独立的 Worker Python 3.11+，目标机没有 Python 或版本过低时仍能开始引导。系统 MUST 不改写系统 Python。管理员显式指定解释器时 MUST 验证其版本、SSL、venv 与 ensurepip；失败时明确报错。

#### Scenario: 目标机没有受支持的 Python
- **WHEN** 管理员没有指定远端解释器且目标机的系统 Python 不存在或低于 3.11
- **THEN** 系统准备独立的受管 Python，验证成功后再安装 Worker，不调用系统 Python 完成引导

#### Scenario: 已准备的受管解释器可用
- **WHEN** 重复部署且受管 Python 满足版本与模块要求
- **THEN** 系统复用该解释器，不重复编译，不变更系统解释器

#### Scenario: 指定解释器不可用
- **WHEN** 管理员指定的解释器缺失、版本过低或关键模块不可用
- **THEN** 系统在变更工作区、密钥与服务前报告原因，不静默换用其他解释器

### Requirement: 公司可访问的软件源
系统 SHALL 默认通过华为 HTTPS 镜像下载 Python、Node.js、npm 包、pip 包与旧系统必要运行库。下载必须校验完整性；失败 MUST 停止并报告阶段，不静默回退到外部公网源。系统包管理使用管理员已有软件源，CentOS 7 的临时归档源使用华为。

#### Scenario: 常规环境安装
- **WHEN** 使用默认配置部署受支持的 Linux Worker
- **THEN** 语言运行时与语言包下载使用华为镜像，版本检查通过后才继续装配

#### Scenario: 镜像缺失或校验失败
- **WHEN** 安装包或校验文件不存在，或校验结果不匹配
- **THEN** 部署在对应步骤失败，保留已有可用运行环境，不访问其他软件源继续尝试

### Requirement: 最低 Git 门槛
系统 SHALL 接受 Git 2.18 及以上版本，保留满足门槛的系统 Git。低于门槛 MUST 明确提示管理员升级，不自行覆盖全局 Git。

#### Scenario: 公司已有 Git 2.19
- **WHEN** 目标机提供 Git 2.19
- **THEN** 部署直接复用，不下载或编译替代 Git

#### Scenario: 目标机 Git 低于 2.18
- **WHEN** 目标机 Git 低于 2.18
- **THEN** 部署在工作区变更前失败，输出最低版本要求

### Requirement: 复用维护安全边界
部署引导 SHALL 纳入已有持久化维护操作、租约、取消与进度记录。工作区和运行服务变更 MUST 继续执行占用、原生 Session 与远端文件锁保护。

#### Scenario: 无解释器时已有监听
- **WHEN** 目标端口已有 TCP 监听，但尚无可用解释器确认原生 Session 状态
- **THEN** 系统在安装系统依赖之前停止引导；已有可用 Agent 解释器时必须通过原生空闲检查后才继续

#### Scenario: 引导期间租约丢失或取消
- **WHEN** 部署在阶段边界发现取消或维护租约已失效
- **THEN** 系统停止后续阶段，不继续安装 Worker 或重启服务
