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

本轮追加真实 CentOS 浏览器 E2E，8 个选定场景通过，详见 `docs/reviews/nexus-dsh-acp-runtime-e2e-20261024.md`。修复创建表单遗漏既有仓库字段，以及无效审批误归为传输错误；保持原 Task 归属、一次审批和占用契约，不另建 change。Mac / CentOS DSH 各 64 passed、前端 30 passed、type-check / build、Ruff 和 strict validate 通过，完整性仍为 9/13。

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
| 过期或跨任务审批 | Driver task / binding / native / tool 关联，超时 / 失联默认拒绝，仅一次选项；本轮真实跨任务、无效选项与过期请求均 409，前两者不消费原审批，真实 IO 回归仍为 503；两次页面拒绝后独立 SFTP 证实没有文件效果。本轮未点击 allow-once，此前页面与原生短测另留证据 |
| 引擎 end_turn | 两真实 Task 进入 WAITING 并占用，人工确认后才 SUCCEEDED；确认前回收 Host，现有完成 / 复盘事务复用。本轮两个影子 MAP / REDUCE 成功，摘要与各 11 条消息均独立匹配；旧取消夹具经停止确认后 Worker 持续为 0 |
| 同员工两任务与刷新 | 两 Worker 同合成责任员工、不同 agentwork / native ID；本轮从页面显式选择仓库并创建 / 启动、只改仓库的草稿保护、切换 / 刷新 / 原 Session 续接通过；跨任务绑定 404、文件逃逸拒绝；已完成轮次的 Worker 计划重启保留绑定和 turn/start 数，无重投。本轮不覆盖生产员工身份或执行中 SIGKILL，后者历史证据另列 |
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
- 2026-10-24 当轮未启动真实 Task MCP / AgentScope，复盘仅影子模式；2026-10-25 补验真实服务和非影子候选审核见下，业务澄清的独立场景仍不扩大为已测。
- DSH 仍是预览版。目标 CentOS 补装 bubblewrap 后，默认 workspace-write Bash 写读成功、无需审批绕过；另一台 Linux 曾提示 Landlock 部分生效。两者均不能替代独立系统身份 / 网络出口隔离。
- RuntimeService 保留现有 OpenCode 分支和 Client，提取共用传输 / 确认 / 重启；不为满足类名形式再复制一套大生命周期抽象。

本轮按原 Scenario 与新增边界测试核验，不 sync 历史 `openspec/specs`，不提前 archive。没有权限审批或凭据缺失阻挡代码收尾；剩余项是实际平台、服务配置、隔离与持续运行证据。


## 2026-10-25 当前 v1.4.8 协作闭环补验

当前隔离组合与结果详见 [协作验收](../../../docs/reviews/nexus-dsh-collaboration-20261025/VERIFICATION.md)。任务5.1–5.3 完成，现为 **12/16**；原1.2、1.3、2.1、4.1仍开放，不能归档或切默认引擎。

| Scenario / 补验 | 证据与结论 |
| --- | --- |
| 引擎绑定与真实协议 | 3单真实 AgentScope / Task MCP 创建，由两CentOS Worker执行 DSH 0.2.1-rc.2 / ACP 0.8.1，无 OpenCode fallback；既有绑定历史保留 |
| end_turn 与人工完成分离 | 三单均先 WAITING，第三单确认前占用1；页面人工确认后 SUCCEEDED、两Worker占用0、3个原生owner释放，无子进程 |
| 原生证据与人工复盘 | 三份远端JSONL seq连续，SHA与API及复盘冻结一致，事件/消息分别165/33、132/23、168/32；真实非影子复盘首单2候选，后两单NO_FINDINGS |
| 候选审核及复用 | QA目录发布1条、驳回1条；第三单真实search/get读取本轮新发布条目，SSH独立13测试+7边界+6排列通过；未自动发布 |
| UI与闭环一致性 | 保留主线 UID/ASR/恢复；修复peer同步阻塞、Celery JSON结果、复盘页活跃刷新；真实页面无需刷新到终态，员工总结查询与存储一致 |
| 回归 / 隔离集成 | 迁移顺延0005且不改发布init；1406 passed / 33 skipped，Ruff、Web30测试/type-check/build、严格OpenSpec通过；MySQL实库与公司安装器未测 |

测试和脱敏证据不能消除原先长测/目标平台/OS隔离门禁。保留失败的同事投递、重复联系、一次错误知识参数和驳回候选，不把最终成功改写成全程零干预。没有新增业务编排、全文镜像或状态事实源。
