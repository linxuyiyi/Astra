# 目标 CentOS 8 实测证据

日期：2026-10-24。目标机 `47.77.219.113`，测试目录 `/root/agentwork/nexus-dsh-qa-20261024`，固定 DSH `0.2.1-rc.2` / ACP `0.8.1`，仅通过已登记的 Nexus Gateway `deepseek-flash`。

当前实测为 CentOS Linux 8.2.2004 / x86_64 / glibc 2.28 / kernel 5.15.223 / Python 3.11.15 / Node 22.23.3 / Git 2.27.0。保留旧 4.18 内核与启动项。运行时、Python 和测试服务为手工独立装配，没有把它记为公司安装器或私有 Git 服务认证验收。

| 证据 | 含义 |
| --- | --- |
| `centos-full.json` / `centos-protocol.json` | 原版 4.18 上的 ACP 协议探针；不能据此推断文件通道可用 |
| `centos-task-verification.json` / `health-before-file-gate.json` | 旧健康检查 AVAILABLE，但文件 API 因 openat2 ENOSYS 返回 503 的反例 |
| `health-after-file-gate.json` | 修复后在同一 4.18 环境明确 UNAVAILABLE，错误传递到设备状态 |
| `centos-bash-probe-report.json` / `centos-first-approval.json` | 默认 bubblewrap 沙箱的零审批 Bash 实测与旧任务的一次人工批准分别留证 |
| `kernel-upgrade-authorization.json` / `kernel-packages-proof.json` / `kernel-upgrade-proof.json` | 明确授权、三包签名和 SHA256、新旧内核、initramfs、重启 boot ID 与真实文件探测 |
| `health-after-kernel515.json` | 升级后现有维护操作 SUCCEEDED、文件通道验证 true、Worker AVAILABLE |
| `centos-kernel515-full.json` / `centos-probe-exit-codes.json` | 新内核 17 PASS / 0 FAIL / 2 UNVERIFIED，退出码 2；长 Session 与限流未验收 |
| `test-invocation.json` / `centos-kernel515-pytest.txt` | 当前代码定向 207 passed / 0 skipped，真实 Redis DB15；包含新增文件门禁反例 |
| `centos-pytest-with-redis.txt` / `centos-file-gate-pytest-failed-attempt.txt` | 旧版本 140 passed；其后一次回归因 QA Redis SSH 转发中断而失败的原始输出，不覆盖失败 |
| `kernel515-file-file.json` / `kernel515-diff-*.json` / `kernel515-access-controls.json` | 正确文件与两种 Diff、越界 403、错误 Session 404 |
| `kernel515-second-turn-observation.json` / `kernel515-stop-proof.json` / `kernel515-task-verification.json` | 同 Session 续接、受控长命令停止、独立文件存在性检查、人工确认与占用 1→0 |
| `centos-native-review.json` / `native-review-crosscheck.json` | 两个真实 MAP/REDUCE 影子复盘 SUCCEEDED；独立重读原生磁盘核对摘要及消息数 12 / 21，无正式候选发布 |
| `centos-source-manifest.json` | 288 个实际远端源码文件与工作树一致；记录基线、覆盖文件和逐文件 SHA256 |
| `centos-platform-stop-proof.json` / `centos-cleanup-proof.json` / `local-cleanup-proof.json` | 原生轨迹独立计数、Worker 退出、临时凭据与自有进程清理；首次远端进程清单包含当时验证命令的父 bash，最终清单排除当前验证器祖先进程 |

文件读取 API 沿用既有 `worker_read_text` 约定：缺失文件返回空内容。因此，停止验证中最初“缺失应为 404”的 QA 假设被纠正，最终以认证 SFTP stat 证明延迟文件不存在，没有把空内容或 HTTP 200 当作存在性证明。

仅保存脱敏元数据和测试输出。原生全文留在远端 0700 测试目录，未复制到 Git；实际 Gateway key、Worker token、SSH 密码与私有配置均不在本目录。所有短测通过不替代其它平台 / 模型、安全隔离、离线部署或 100 任务 / 72 小时门禁。
