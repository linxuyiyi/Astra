# DSH ACP 契约核验（2026-10-24）

## 范围与结论

本轮延续原 P0，已实现 P1 Worker / Gateway 和 P2 Runtime / 工作台 / 复盘。完整运行证据、平台失败、源码和截图见 `docs/reviews/nexus-dsh-acp-runtime-verification.md` 及其 evidence 目录；旧 P0 报告保留为历史证据，不覆盖本轮失败。

按仓库 `.opencode/skills/openspec-verify-change/SKILL.md` 对本 change 执行 status、instructions apply、代码 / Scenario 对照和 strict validate。CLI 的 planning artifacts complete 只表示文档齐备，不能表示实施或生产验收完成。

| 维度 | 核验 |
| --- | --- |
| 完整性 | 完成 9/13；新增文件通道门禁已完成，4 项外部 / 部署 / 长测门禁保留未完成 |
| 正确性 | 5 项功能 Requirement 有代码、测试和 Linux 产品闭环；固定版本兼容性、灰度发布 2 项仍有真实门禁缺口 |
| 一致性 | 复用 Task / Worker / Git / Gateway / 人工确认 / 复盘，不镜像全文、不伪造 OpenCode API、不另建 Run；默认引擎保持 OpenCode |

**NOT READY FOR ARCHIVE / PRODUCTION CUTOVER**。这不抹掉已完成的功能开发，也不将失败平台写成通过。

## Scenario 对照

| Scenario | 代码与验证证据 |
| --- | --- |
| 运行环境不兼容 | `dsh_acp/preflight.py` / `test_installation.py`；云端 glibc 2.17 / kernel 3.10 预检明确失败，实际 ACP 原生 binding 初始化失败另留报告 |
| 真实协议验收 | 固定版 `process.py`、`dsh_acp_probe.py`；重装后的目标 CentOS 8 / kernel 5.15.223 完整短测 17 PASS / 2 UNVERIFIED；历次 nonce 偏差 / Gateway 503 仍保留，长 Session / 限流未测，完整 P0 不通过 |
| ACP 可初始化但 Worker 文件通道不可用 | `inspect_worker_file_channel()` 复用现有 `open_workspace` / `validate_inside`；ENOSYS / EPERM 反例验证拒绝、关闭 FD、未调用 Gateway / ACP；控制面要求显式 true 并传递错误。目标 4.18 健康检查 UNAVAILABLE，升级 5.15 后 AVAILABLE，文件 / 两种 Diff / 403 越界 / 404 错误 Session 实测通过。CentOS 定向 207 passed，Mac DSH 59 passed |
| 旧 Session 继续执行 | `RuntimeSession.runtime_engine`、`lifecycle.py`、`dsh_lifecycle.py`；禁止重启时跨引擎，恢复保留 Worker / workspace / profile / native ID；旧 OpenCode 回归通过 |
| 人工显式重新开始 | Workbench 只允许当前绑定发起 restart；已结束任务输入仍 409，历史绑定 restart 409。新增接口反例测试，页面已实测切换到会话 #2 和新原生 ID |
| 重复投递 | SQLite receipt / FULL / 单写入锁；不同 Gateway request ID 的相同 submission 只调用一次 prompt，返回旧 receipt，不以当前轮覆盖旧受理证据 |
| 断线或 Worker 重启 | 真实受理 running 后 SIGKILL 自有 Worker，重启 unknown，Task 保持占用，新输入 409；原生文件 / turn/start / receipt 各 1，未自动重投 |
| 停止无回执 | 取消 fence 先落盘，只有核实归属进程组退出才确认；单元反例覆盖 PID 复用、通知 / future 不等于停止、迟到提交；页面长命令停止后无 LATE 文件 |
| 过期或跨任务审批 | Driver task / binding / native / tool 关联，超时 / 失联默认拒绝，仅一次选项；跨 Session 单测与真实页面拒绝 / allow-once，通过文件效果核实 |
| 引擎 end_turn | 两真实 Task 进入 WAITING 并占用，人工确认后才 SUCCEEDED；确认前回收 Host，现有完成 / 复盘事务复用 |
| 同员工两任务与刷新 | 两 Worker 同测试责任员工、不同 agentwork / native ID；切换 / 刷新 / 原 Session 续接，跨任务 / native 404、文件逃逸 403；在线增量按 stream + seq 去重 |
| 原生特性不可用 | UI 明确不支持计划 / 待办 / 终端，reasoning capability 为 false；OpenCode 保留原生 iframe；2560×1440 真实截图、滚动与尺寸检查 |
| 轨迹缺失 / 格式变化 | 固定 v4 plain 解析、页间 SHA / seq / cursor / native / workspace 校验；损坏、symlink、部分尾行、重复和不完整都拒绝成功复盘 |
| 确认时证据不可用 | 原生 end_turn + Host 回收可证明时仍保留人工业务完成，复盘 source_error、用量 unknown；反例 `test_incomplete_trace_does_not_redefine_human_completion` |
| 灰度失败 | 默认 OpenCode，失败不自动换模型 / 引擎、不改旧绑定，DSH 不支持的安装操作被拒绝；100 任务 / 72 小时、正式回退演练未做 |

## CRITICAL：归档前仍须完成

1. **1.2**：目标 CentOS 已通过真实短测与产品闭环；继续在 Linux / Mac 完成含长 Session 的 P0，不以本次短测替代持续运行证据。
2. **1.3**：提供健康的已登记 GLM / Qwen 路由，完成流式、工具、reasoning、原生用量 / 限流及公司 ARM64 / glibc 2.28、Windows 加载实测。
3. **2.1**：补完整离线归档 / 公司安装器、独立系统身份、OS 工具与网络白名单实证。当前目录权限、配置与 hash 不能作为完整 OS 隔离证据。
4. **4.1**：同模型 / 同网关任务对照、至少 100 任务 / 72 小时，记录正确性、人工修复、用量、吞吐与故障恢复；完成前不得切默认引擎。

## WARNING：实证边界

- MySQL 8.0.36 实库迁移未运行；SQLite 增量迁移 / 数据保留与 MySQL 方言检查不能替代它。
- 没有启动真实 Task MCP 业务澄清、AgentScope 聊天或正式候选发布服务；现有业务机制由定向 / 全量回归覆盖。实际复盘仅影子模式 MAP / REDUCE。
- DSH 仍是预览版。目标 CentOS 补装 bubblewrap 后，默认 workspace-write Bash 写读成功、无需审批绕过；另一台 Linux 曾提示 Landlock 部分生效。两者均不能替代独立系统身份 / 网络出口隔离。
- RuntimeService 保留现有 OpenCode 分支和 Client，提取共用传输 / 确认 / 重启；不为满足类名形式再复制一套大生命周期抽象。

本轮按原 Scenario 与新增边界测试核验，不 sync 历史 `openspec/specs`，不提前 archive。没有权限审批或凭据缺失阻挡代码收尾；剩余项是实际平台、服务配置、隔离与持续运行证据。
