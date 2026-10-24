## Context

基线 `origin/feat/nexus-dsh-acp-runtime` / `a3511ddba` 只有设计，Runtime 原生操作直接依赖 OpenCode。保留 `Task → RuntimeSession → AgentWorker → Worker → 原生 Session` 现有业务链。DSH 固定官方 tag `dsh-v0.2.1-rc.2`，Python ACP 客户端固定版本，官方 ACP 使用 `session/resume` 而非 `session/load`，且不会重放历史。

## Goals / Non-Goals

目标：在独立工作树实施兼容性探针、可监督 Worker 驱动、引擎绑定和统一工作台，提供 Linux 真机证据。

非目标：跨引擎热切换、AgentScope 聊天重构、DSH Web 集成、伪装 OpenCode API、自动接受危险操作、直接正式切流。

## Decisions

1. **先探针后切流**：先实测固定运行时的初始化、新建/恢复、双轮、MCP、停止、审批、持久轨迹与 Gateway 路由。真实兼容性失败按阻塞记录，不用 mock 宣布通过。探针与驱动基础可继续开发，正式派单必须受门禁保护。
2. **薄驱动与单写入者**：每活跃绑定单独 ACP 子进程、独立 DSH_HOME。Worker 监督进程组并锁定持久目录。引擎与 `worker_channel` 分离，OpenCode 仍使用现有通道。ACP SDK 负责协议，Nexus 不另写 JSON-RPC。
3. **受理与停止**：提交以 binding/submission/generation/fence 记账，先持久受理再执行，重投只回原回执；重启中的 executing 记录为结果未知。取消必须观察到原 prompt 的 cancelled 回执或证明归属进程组已结束；不得以已发送 cancel 表示停止成功。
4. **事实与存储**：业务任务及人工完成确认在 Nexus；DSH Session 与轨迹在 DSH；Nexus 只保存绑定、状态和结果索引。新增 revision，不修改 `0001_init`。在线事件按需转发，不写平台全文消息表。复盘使用固定版本只读原生记录，证据不完整拒绝产出候选。
5. **网关与配置**：Profile 从官方固定版本配置派生并锁定装配 hash；只允许已配置 Nexus `/v1` 路由，不继承厂商凭据。配置选择使用原生模型 option，不能自动覆盖人工选择。权限回调默认等待人工，失联/超时拒绝，不默认 allow。
6. **界面**：既有任务详情按明确引擎字段选原生 OpenCode 或 Nexus 通用工作台。事件/审批/停止均校验 task + binding + native Session，刷新按 seq 恢复。沿用文件和 Git Diff API。缺失终端、计划、待办明确不可用。2560×1440 真页面验收。
7. **区分装配与完整 Worker 基线**：Node / ACP 的 glibc 与内核基线不等于文件通道兼容。DSH 健康检查复用现有安全文件通道实际探测 openat2，不支持或被策略禁止就拒绝标为可用并暴露明确原因；不增加非原子路径访问降级。Bash 沙箱后端另以真实调用验证，不能用手工放行替代。

## Risks / Trade-offs

- DSH 为预览版且原生依赖多 → 固定包版本、完整性与环境指纹；不能把支持平台列表当实测。
- ACP 在线流不能作为完整轨迹 → 检查原生持久日志，版本变更必须重新验证解析器。
- Linux 测试机可能为旧 glibc x86_64，与公司 ARM64/glibc 2.28 不同 → 分开记录，不替代公司平台门禁。
- 100 个任务/72 小时及 Windows 实机不能以短测代替 → 独立保留未验收项，默认继续 OpenCode，不自动切流。
