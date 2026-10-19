# Nexus v1.4.5 Worker 维护证据索引（2026-10-19）

最终状态：**PASS**。本索引按内容中的 host、unit、PID、路径核对架构，当前结论以“最终证据”为准。

## 架构来源核对

| 文件 | 核对依据 | 架构 |
| --- | --- | --- |
| `arm-acceptance-final5-*`、`arm-acceptance-final3-*` | VM `nexus-v145-qa`，路径 `/opt/nexus-v145-regression`，`arch=aarch64` | ARM / aarch64 Lima |
| `final-deploy-aarch64-idempotent-*` | VM `nexus-v145-qa`，端口 44135，PID 22283/28171 | ARM / aarch64 Lima |
| `arm-worker-a-final-idempotency-*` | 引用 `final-deploy-latest.json`，PID 22283/28171，路径 `/root/nexus-v145/worker-a` | ARM / aarch64 Lima |
| `arm-fault1-*`、`arm-fault2-*` | 路径 `/root/nexus-v145/worker-a`，端口 44135，PID 22283/30812/31208 | ARM / aarch64 Lima |
| `arm-fault3-*`、`arm-fault4-*`、`arm-offline-*`、`arm-fault-matrix-final-*` | journal host `lima-nexus-v145-qa`，unit `nexus-opencode-1`，路径 `/root/nexus-v145/worker-a` | ARM / aarch64 Lima |
| `x86-fault-matrix-batch1-*` | target `47.77.219.113`，CentOS 7 x86_64，unit `nexus-*-1/2`，路径 `/opt/nexus-v145-qa-20261019` | 用户 x86_64 |
| `x86-equipment-faults-*` | environment 2 / `worker-x86-b`，路径 `/opt/nexus-v145-qa-20261019`，MCP unit `nexus-v145-qa-mcp-20261019.service` | 用户 x86_64 |
| `x86-git-http-fixture-*` | bind `172.17.13.60:18045`，unit `nexus-v145-qa-git-http-20261019.service` | 用户 x86_64 |
| `x86-secret-scan-*` | `reports_scanned=/opt/nexus-v145-qa-20261019/reports` | 用户 x86_64 |
| `x86-ui-*` | 页面显示 `worker-x86-a/b Linux`、`root@172.17.13.60:22`、`/opt/nexus-v145-qa-20261019/workers/*` | 用户 x86_64 |

## 最终证据

| 文件 | 架构 | 状态 | 用途 |
| --- | --- | --- | --- |
| `arm-acceptance-final5-460-20261019T054735Z.json` | ARM | PASS | 最终回归：460 passed / 0 failed / 1 skipped（MySQL-only）；Redis DB14、ruff、快照 |
| `arm-acceptance-final5-pytest-460-20261019T054735Z.log` | ARM | PASS | 完整 pytest 输出 |
| `arm-acceptance-final5-ruff-20261019T054735Z.log` | ARM | PASS | ruff 通过 |
| `arm-acceptance-final5-prepare-20261019T054735Z.log` | ARM | PASS | 依赖准备与快照 |
| `arm-acceptance-final5-redis-preflight-20261019T054735Z.log` | ARM | PASS | Redis PING / 端口 / 归属 |
| `final-deploy-aarch64-idempotent-20261019T052402Z.json` | ARM | PASS | aarch64 full deploy 与同配置重复 deploy；6 项装备 verified |
| `arm-worker-a-final-idempotency-20261019T052328Z.json` | ARM | PASS | ARM 同配置幂等：PID/token/config/unit/worktree 不变 |
| `arm-fault1-occupancy-20261019T052552Z.json` | ARM | PASS | WAITING 占用在 Worker 重启后保持，不释放 |
| `arm-fault2-native-busy-20261019T052747Z.json` | ARM | PASS | 原生 busy 时 deploy/update/restart 全部拒绝且现场不变 |
| `arm-fault3-retest-20261019T054219Z.json` | ARM | PASS | 停机后正式 restart、SIGKILL 恢复、持久停止可观测 |
| `arm-fault4-controller-crash-20261019T054402Z.json` | ARM | PASS | API kill 后操作 INTERRUPTED、SSE 续传无重复、reconcile 安全、retry 成功 |
| `arm-offline-ssh-recovery-final-20261019T054840Z.json` | ARM | PASS | 离线 SSH 停机后正式 restart 无需 reconcile 即 SUCCEEDED，6 项 verified、可调度、token 不变 |
| `arm-offline-ssh-recovery-final-20261019T054840Z.log` | ARM | PASS | 离线复验过程日志；无手动 start |
| `arm-offline-readiness-117-20261019T054807Z.log` | ARM | PASS | 离线/在线就绪等待定向 Linux 测试 117 passed |
| `arm-fault-matrix-final-20261019T054952Z.json` | ARM | PASS | 最终故障矩阵：deploy 幂等、fault3、fault4、offline SSH 全 PASS；最终 Worker 6 项 verified |
| `x86-fault-matrix-batch1-20261019T053515Z.json` | x86 | PASS | 双 Worker 健康、幂等、dirty/wrong branch/ff update、disable/restore、占用、HTTP Git |
| `x86-equipment-faults-final-20261019T053946Z.json` | x86 | PASS | MCP 断连、Skill 缺失、模型 401 三项均被识别并恢复 |
| `x86-equipment-faults-final-health-20261019T053946Z.json` | x86 | PASS | 故障后 env1/env2 均 online/AVAILABLE/schedulable，6 项 verified |
| `x86-equipment-faults-secret-scan-20261019T053946Z.json` | x86 | PASS | 26 份装备报告扫描无原始 secret |
| `x86-git-http-fixture-20261019T053515Z.json` | x86 | PASS | 自有私有 HTTP Git fixture：正确/错误 PAT、clone/fetch/pull/push、日志无 PAT/Authorization |
| `x86-secret-scan-20261019T053515Z.json` | x86 | PASS | x86 报告目录无原始 secret |
| `ui-final-validation-20261019.json` | 本地 Web | PASS | 最终中文阶段标签补齐后 15/15 tests、type-check、build |
| `ui-final-validation-20261019.log` | 本地 Web | PASS | 命令与结果原始输出；不含会话元数据 |
| `x86-ui-worker-a-final-1280x720.png` | x86 | PASS | 1280x720 真实页面：中文健康、连接在线、OpenCode 正常、维护成功、可调度 |
| `x86-ui-worker-a-overview-final-1280x720.png` | x86 | PASS | 1280x720 Worker A 运行概览 |
| `x86-ui-workers-list-final-1280x720.png` | x86 | PASS | 1280x720 双 Worker 列表：两台均连接在线、OpenCode 正常 |

## 历史证据（已被最终轮次替代）

| 文件 | 说明 |
| --- | --- |
| `arm-acceptance-final3-445-*`、`acceptance-final-*` | 早期 415/445 回归；最终以 `arm-acceptance-final5-460-*` 为准 |
| `arm-fault3-opencode-superseded-*`、`arm-fault3-crash-journal-*`、`arm-fault3-worker-restart-timeout-*` | 早先 fault3 失败与就绪竞态；最终由 `arm-fault3-retest`、`arm-offline-ssh-recovery-final` 替代 |
| `arm-fault-matrix-summary-*` | 早先 partial 汇总；最终以 `arm-fault-matrix-final-*` 为准 |
| `first-linux-deploy-*`、`backend-fix-*`、`ui-browser-*`、`ui-validation-summary-*` | 历史失败/会话/摘要；仅保留溯源 |

## 边界说明

- MySQL 未执行真实回归，唯一 skip 为 MySQL-only 方言用例。
- 模型只验证 `/models` 认证与模型可见性，未调用真实推理。
- HTTP Git 为自有私有 fixture，验证认证、clone/fetch/pull/push 与日志脱敏；不宣称第三方托管或用户生产仓库凭证。
- Node/npm/pip 安装失败由 Linux 安装脚本 fixture 验证，未通过真实断网破坏全局包。
- 证据只保留脱敏结果、日志、截图和哈希；未复制完整 OpenCode 会话、SSH 私钥或实际 secret。
