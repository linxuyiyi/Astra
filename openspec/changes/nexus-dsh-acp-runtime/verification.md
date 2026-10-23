# DSH ACP 阶段核验（2026-10-23）

## 原因与范围

用户指定的需求分支 `feat/nexus-dsh-acp-runtime` / `a3511ddba` 只有设计文档。基于该分支创建独立工作树 `/Users/niuniu/.codex/worktrees/nexus-dsh-acp/Astra`、分支 `codex/nexus-dsh-acp-runtime`；原 `/Users/niuniu/程序/Astra` 的未提交文件未覆盖。

**本次完成探针开发和部分真实 P0 验证，完整 DSH 适配尚未完成。** 没有启用正式 Worker 派单，没有增加 Runtime migration 或 DSH 工作台，不将阶段结果描述为产品交付或生产验收。OpenSpec 唯一实施清单目前 **1/12 完成**，保持活动状态，不 sync/archive。

## 修改内容

- `ops/runtime/dsh`：固定官方 DSH `0.2.1-rc.2`、ACP Python `0.8.1` 与 npm/venv 锁文件。核对实际安装包版本、依赖集合及配置/源码指纹，不信任 DSH ACP 返回的 `agentInfo.version=0.0.1`。
- `app/worker_agent/dsh_acp`：官方 Python SDK 连接、POSIX 独立进程组和 DSH_HOME 锁、原生创建/恢复/提交/取消、人工一次授权关联、stdout 协议与 stderr 脱敏隔离、固定 v4/plain 原生轨迹分页与完整性检查。
- Profile 只保留明确的 Nexus Gateway 别名，移除厂商模型/账户、搜索和动态装配入口；调用 key 在测试独立私有凭据文件中，不进入 argv/报告。
- `tools/e2e/dsh_acp_probe.py` 与真实 MCP fixture：网关目录、双轮、MCP 实际调用证明、同 Session 重启、取消回执、SIGKILL 故障恢复、拒绝/allow-once、原生用量和清理证据。失败/缺项返回非零，不自动换模型或引擎重跑。
- 新增 31 项针对性反例测试，覆盖重复 Host、跨 Session/未知工具/超时授权、通知不等于停止、无本地 future 不等于原生停止、锁文件不可移植/包漂移、轨迹乱序/缺失/符号链接/未结束轮次，以及非法 URL 凭据不能进入报告。

这些是 P0 基础设施，尚不是完整 `DshAcpDriver`：未完成 Gateway 接线、持久提交账本、generation/fencing、生产恢复、系统权限/网络/MCP 白名单与安装器。

## 真实运行结果

| 验证 | 本轮证据与边界 |
| --- | --- |
| Linux QA 固定安装 | Ubuntu 24.04 / aarch64 / glibc 2.39 / Python 3.12.3 / Node 22.23.3；独立目录 `/tmp/nexus-dsh-acp-20261023`；npm ci 安装 618 个当前平台依赖，venv 通过 hash 锁安装 |
| Linux 最终真实 ACP | **17 项 PASS、2 项 UNVERIFIED**，整体 `INCOMPLETE` / exit 2。网关目录、初始化、新建、原生模型选择、双轮、MCP、恢复及续轮、取消、断线、审批、原生轨迹、reasoning、token 与整组清理通过；长 Session/限流未验证 |
| Linux 原生事实 | Session `9a225411-3c03-4e0b-8965-a9ec1fba55bd`；103 条连续原生事件，7 个 turn/start 与 7 个 turn/end，2 次审批；无未结束轮次；12 条原生用量记录。取消实际收到 `cancelled`，SIGKILL 后恢复原 ID，不自动重投 delay 工具 |
| Linux 网关路径 | Worker 侧 `127.0.0.1:18531/v1` 经本轮 SSH 反向隧道到 Mac 实际 Nexus Gateway `18511/v1`，模型别名 `deepseek-flash`；没有直连厂商。不是公司跨主机/HTTPS 部署验收 |
| Mac 最终同源码复测 | arm64 / Python 3.14.3 / Node 25.4.0；固定安装、目录、初始化、新建、模型选择与首轮通过，第二轮失败。原生错误为网关 `503 / NO_ELIGIBLE_NODE / CIRCUIT_OPEN`；本轮较早独立尝试已经验证双轮/恢复/取消，但不能替代最终复测失败 |
| Qwen | 已配置 `qwen2.5:3b` 目录可见；真实首轮因 `503 / NO_ELIGIBLE_NODE / HEALTH_UNHEALTHY` 失败。此结果证明网关节点不可调度，不能推断 DSH 不支持 Qwen；没有绕过网关 |
| GLM | 当前真实目录未提供 GLM 路由，未验证；没有新增虚假配置 |
| 用户指定 Linux | 已从此前用户聊天找回 `root@47.77.219.113:22`；本轮连接可达，历史密码与本机现有密钥认证均失败。没有修改该主机、安装或重启 Worker。Linux QA 不能替代该目标的 agentwork 验收 |

最终 Linux 与 Mac 运行源码 fingerprint 一致，且与本地最终代码逐文件一致：

`ffbd5b5a23b72addabb9203391a2344d7c45f47662d85278d514229ade114877`

npm lock fingerprint：

`28baa120aa63691494beb36c7a84cfb2c5baa5df20bb68a4ce43b99e1290791a`

脱敏原始报告见 `docs/reviews/evidence/nexus-dsh-acp-p0/`：`linux-aarch64-deepseek.json`、`mac-deepseek-final.json`、`gateway-failure-observations.json`。原生全文只保留在测试 DSH_HOME，不提交到仓库。

## 自动检查

定向回归包含 `tests/dsh`、Worker Gateway/故障/数据通道、phase2 faults、占用和 Runtime：

- Mac：**123 passed / 8 skipped**，39.44 秒。
- Linux：**123 passed / 8 skipped**，38.04 秒。
- 上述 8 项为显式要求 `ASTRA_TEST_REDIS_URL` 的跨进程 Redis 测试；已在专用临时 Redis 补跑：Mac **8 passed**（5.28 秒），Linux **8 passed**（5.29 秒）。两端各覆盖 131 个不同用例，分两组执行；原生 Redis 服务与回环隧道已由本轮监督进程清理。
- 两端 `ruff check app nexus_api tests`、探针工具 Ruff、OpenSpec strict validate 与 `git diff --check` 通过。
- 本轮没有前端代码、schema 或主要业务流改动，未运行前端构建、MySQL migration、任务大厅/工作台 E2E。不能以现有单元测试替代这些后续接入验收。

## Scenario 对照与 OpenSpec verify

| Requirement | 当前实现与验证 |
| --- | --- |
| 固定版本真实门禁 | 固定依赖/指纹、实际 ACP 与网关探针、失败非零、未测项保留；完整平台/模型门禁仍未通过 |
| 引擎绑定不可混用 | 只有独立探针的配置/工作区/原生 ID 绑定；正式数据库/API 与迁移未实施 |
| 唯一受理与可证明停止 | 单进程拒绝重叠 prompt、原生取消和进程组证明有测试；正式持久去重/fencing/Worker 占用未实施 |
| 人工交互与任务完成 | 原生工具审批关联与默认拒绝有实机/反例证据；Task MCP WAITING/人工完成未接入 |
| 统一工作台/并行隔离 | 未实施，包含 2560×1440、同员工两个任务、切换/刷新/重连 E2E |
| 原生持久证据与复盘 | 原生轨迹读分页/版本/完整性可验证；MAP/REDUCE 与 candidate 尚未接入 |
| 灰度发布门禁 | 保持未启用；100 任务/72 小时、公司/Windows 和回退演练未做 |

按仓库 `openspec-verify-change` 检查完整性、正确性与一致性：**NOT READY**。11 项实施任务仍未完成，是完整适配的阻塞项；没有把未实现的需求划成非目标或勾选完成。当前基础设施与设计方向一致，但不能据此归档整个 change。

## 剩余风险与下一步

1. 先恢复目标 agentwork 的安全 SSH 认证，并提供健康的已登记 GLM/Qwen Gateway 路由；复跑模型、长 Session、限流及公司 ARM64/glibc 2.28、Windows 实机门禁。Mac 网关熔断原因需要在网关/上游健康链路继续定位，不能把节点 `HEALTHY` 当请求一定可调度。
2. P1 继续实现离线安装装配、MCP/网络白名单、独立系统身份、持久受理/fencing 和 Worker Gateway；当前同系统用户仍能读本地凭据，Bash 文件沙箱不承担网络隔离。
3. P2 按现有契约接 migration、Runtime 驱动、通用工作台、并行隔离和经验复盘；不更改已发布 init，不伪造 OpenCode 消息。
4. 完成产品闭环、故障/长任务实证与灰度门禁后，才考虑切换新任务默认引擎。本次仅本地阶段提交，未推送、合并或部署正式服务。
