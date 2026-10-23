## 1. P0：真实兼容性与固定基线

- [x] 1.1 实现可复跑的固定版本/平台/配置完整性预检、官方 ACP 客户端真实探针和脱敏证据输出；失败返回非零，未测项不可计为通过。
- [ ] 1.2 在 Mac 与 Linux agentwork 实测 init、新建/恢复/取消/授权、双轮、MCP、原生持久轨迹、断线和长 Session，记录平台与版本。
- [ ] 1.3 经已配置 Nexus Gateway 实测 DeepSeek/GLM/Qwen 路由流式、工具、reasoning、token 与限流；公司 ARM64/glibc 2.28 及 Windows x64 加载检查独立留证。

证据见 `verification.md` 和 `docs/reviews/nexus-dsh-acp-runtime-verification.md`。本轮已安全连接目标 Linux；glibc 2.17 / kernel 3.10 不满足固定 Node 22 基线，真实 ACP 原生扩展不能初始化。Linux QA 的产品闭环通过，但最新完整探针遇到模型 nonce 偏差与 Gateway 503；保留历次失败，不替代目标验收。1.2/1.3 保持未完成。

## 2. P1：Worker DSH 引擎

- [ ] 2.1 固定安装包/依赖锁、Profile/Skill/MCP hash 与健康检查；Worker 独立 Python 环境、安全目录和工具/网络白名单有实证。
- [x] 2.2 实现官方 ACP 驱动与每绑定单进程监督，覆盖创建/恢复、权限、快照、原生轨迹分页及 dispose；明确不可用能力。
- [x] 2.3 复用 Worker Gateway 实现持久受理/去重/fencing、事件、取消确认及断线恢复；Linux 故障注入证明不重复提交、不误释放、不跨任务。

2.1 已实现固定依赖、真实 ACP/Gateway/Git 健康检查、Skill/MCP 白名单与冻结 hash、独立 venv 和私有目录；离线归档、公司安装器、独立系统身份与 OS 网络/工具策略实证仍未完成。

## 3. P2：Runtime 与 UI

- [x] 3.1 新增引擎最小绑定 Alembic revision 与选择契约，OpenCode 原生操作收敛到薄驱动；既有 Task 状态、调度与人工确认回归通过。
- [x] 3.2 任务详情按引擎路由通用工作台/原生 OpenCode，支持消息/工具/文件 Diff/审批/停止/继续/人工确认，2560×1440 真页面闭环。
- [x] 3.3 同员工两个并行任务隔离、切换/刷新/断线恢复与重复事件幂等，API/浏览器 E2E 及前端检查通过。
- [x] 3.4 复盘读取 DSH 原生持久证据并校验完整性，MAP/REDUCE 与 candidate 人工确认回归通过。

## 4. P3：验收与灰度

- [ ] 4.1 同网关同模型任务对照与至少 100 任务/72 小时故障/长任务验收，记录正确性、人工修复、用量与吞吐；未达到不得切默认引擎。
- [x] 4.2 按 Scenario 做 OpenSpec verify、差异/凭据检查，逐阶段独立提交；环境阻塞和未完成项留在本清单，不提前归档或宣称生产通过。
