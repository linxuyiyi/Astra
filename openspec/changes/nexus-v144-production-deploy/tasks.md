## 1. 契约与测试基线

- [x] 1.1 建立 `ops/deploy/nexus_deploy` 模块骨架与部署配置数据模型，覆盖路径、端口、仓库、commit、可选模块与配置分层。
  - 验证：配置解析测试覆盖默认值、覆盖值和缺失必需值。
- [x] 1.2 建立临时 Git 仓库、临时 SQLite、本地 HTTP/端口和 stub systemctl 的测试夹具。
  - 验证：测试夹具在 macOS 可运行，不依赖共享 venv 安装。

## 2. 阶段一：部署入口收敛（P1）

- [x] 2.1 将 `install_nexus.sh` 改为唯一生产 CLI 的薄封装，保留参数解析与帮助。
  - 验证：`bash -n` 与 CLI `--help` 通过。
- [x] 2.2 实现完整 commit 锁定、仓库匹配、fetch/checkout 与 HEAD 校验，失败立即退出。
  - 验证：临时仓库覆盖成功、commit 不存在、网络/checkout 失败、工作树脏。
- [x] 2.3 实现版本化发布目录准备、依赖安装、Web 构建与版本清单校验，构建失败不影响现网。
  - 验证：故障注入确认失败目录清理且 `current` 不变。
- [x] 2.4 实现部署独占锁、失败/成功发布记录与状态查询，失败不残留成功标记。
  - 验证：并发部署被拒绝；失败记录保留上一成功记录。
- [x] 2.5 迁移端口归属校验，验证监听 PID 属于对应 unit 的 MainPID/cgroup。
  - 验证：未知进程占用失败、对应 unit 占用通过。

## 3. 阶段二：服务托管收敛（P2）

- [x] 3.1 建立统一服务定义：Task API、Celery Beat、Control/Agent Worker、Task MCP、Agent Service、Nexus API 必需；Gateway、Library MCP 启用则必需。
  - 验证：服务定义测试断言 unit、命令、端口、必需性一致。
- [x] 3.2 生成每进程一个 systemd unit，设置 Restart、超时、工作目录、运行账号与日志，移除 oneshot 二次守护。
  - 验证：渲染测试确认无 oneshot 托管常驻进程、无重复 unit。
- [x] 3.3 让 install/upgrade/restart/status/health 共用服务定义与启停路径，只操作 Nexus unit。
  - 验证：stub systemctl 记录调用并断言服务集合。
- [x] 3.4 将 `astra_service.py pro` 改为生产薄封装，禁止迁移/构建/托管；`dev` 保留。
  - 验证：现有开发测试调整后通过，生产入口测试断言明确拒绝。

## 4. 阶段三：配置与密钥隔离（P3）

- [x] 4.1 实现用户配置、密钥、生成运行配置分层与原子写入，保留用户值和已有密钥。
  - 验证：重复生成不覆盖用户配置与密钥；生成差异只含非敏感摘要。
- [x] 4.2 外置全部数据目录：Task DB/workspaces/runtime/config、Nexus API SQLite、Agent Service workspace/qdrant/blob、Celery schedule。
  - 验证：生成配置测试断言路径位于发布目录之外。
- [x] 4.3 生成共享 Task MCP 密钥并同步 `ASTRA_TASK_MCP_KEY`/`NEXUS_TASK_MCP_KEY`，确保权限 0600、秘密不进日志。
  - 验证：首次生成一次、重复安装不变、输出不包含密钥值。
- [x] 4.4 使用 Nexus 专属 OpenCode 配置目录与显式 provider/model/base URL，不改全局配置。
  - 验证：全局配置哈希不变；生成配置包含显式 provider 与 base URL。

## 5. 阶段四：发布、迁移与恢复（P4）

- [x] 5.1 实现备份阶段：SQLite 备份 API + `integrity_check`；MySQL 显式工具与恢复条件检查。
  - 验证：SQLite 备份可查询且完整；MySQL 工具缺失时明确失败。
- [x] 5.2 实现停止受影响服务后单次迁移、revision 记录和并发保护。
  - 验证：迁移只调用一次；失败不写成功记录。
- [x] 5.3 实现 `current` 原子切换、启动与失败回退；数据库 revision 变化按兼容声明阻断或恢复。
  - 验证：构建失败、启动失败、readiness 失败均能回到上一可用发布；不兼容迁移阻断并输出人工步骤。
- [x] 5.4 输出发布记录：commit、配置摘要、数据库 revision、检查结果与回退结果。
  - 验证：成功/失败记录字段测试。

## 6. 阶段五：健康检查与文档（P5）

- [x] 6.1 实现 HTTP 结构校验、Redis ping、SQL `SELECT 1`、Celery hostname/queue 检查与 MCP 认证无副作用探测。
  - 验证：无效结构、无 worker、MCP 未认证分别失败；模型计费探测标记 skipped。
- [x] 6.2 实现 Nginx `-t` 与首页、`/api/`、`/api/v1/`、`/v1/`、`/agentscope-api/` 等启用路由检查。
  - 验证：401/403/无内容首页不被判成功。
- [x] 6.3 将 LLM Wiki 与远端 Worker 拆为独立入口，只检查已启用模块自身依赖。
  - 验证：未启用时核心不受影响；启用失败有独立报告。
- [x] 6.4 更新 README、env 模板、Nginx、systemd 说明与安装/升级/回退/排障文档，统一 Nexus 命名与默认端口。
  - 验证：文档默认值与代码服务定义一致。
- [x] 6.5 新增 Linux systemd/重启/恢复 smoke 脚本与测试矩阵，标注本机不可执行项。
  - 验证：脚本通过 `bash -n`；真实 Linux 项在报告中标记 unchecked。

## 7. 验证与交付

- [x] 7.1 运行相关 pytest、ruff、`bash -n` 与 OpenSpec strict validate。
- [x] 7.2 按 `/opsx-verify` 生成事实验证报告，逐项核对 R01-R10 场景。
- [x] 7.3 检查最终 diff，按阶段分支提交并合并回 `codex/nexus-v1.4.4-deploy`；未做真实 Linux 验收前不归档 change。
- [ ] 7.4 在真实 Linux 主机执行 `ops/deploy/tests/linux_smoke.sh`，并完成 systemd 崩溃自恢复、Nginx 入口、MySQL 备份恢复、Redis/Qdrant/blob/workspaces 恢复演练与模型计费探测。
  - 验证：保留目标主机日志与恢复记录；未执行前不得标记通过，也不归档 change。

本机验证证据见 `docs/reports/NEXUS_V144_DEPLOYMENT_20261019.md` 与 `docs/reports/evidence/nexus-v144-deploy/`。macOS 平台限制导致的完整全量测试失败不计为通过。
