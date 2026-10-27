# 验证记录

日期：2026-10-28（Asia/Shanghai）。基线：`v1.4.8` / `5ae0401b700e879a14f8e7ed9e84f80f681e603b`。本次为工作区修改，未提交、未推送、未部署；原有未跟踪目录保持不变。

## 问题原因与实现

Task MCP 原来只有主动暂停和请求人工处理，没有查询/回答原生 Worker 问题的接口；任务技能把 WAITING 一概解释成需要人处理。群任务事件只公开通知，不会唤醒员工。DSH 提示词还引用了 Worker 未装配的 Task MCP 求助能力。

新增 `nexus_task_assistance`、`nexus_task_reply` 和 `nexus_task_input`，让责任员工读取实时原生请求/DSH 输出，先判断能否处理；确需业务信息或新授权才在原对话提问，再将明确答案送回同一任务。判断和聊天上下文由 AgentScope 员工处理，Nexus 不生成答案。原生请求仍是唯一事实源，任务验收规则不变。

## OpenSpec verify

| 维度 | 结论 |
| --- | --- |
| 完整性 | 4/4 实施项完成；3 项 Requirement，8 个 Scenario 均有实现路径 |
| 正确性 | 工具、Runtime 投影、来源绑定、通知和群讨论已有确定性验证；模型判断及真人对话链路未做真实服务验收 |
| 一致性 | 复用原 Runtime/Worker/Session 与 AgentScope，不新增 schema、Agent、任务状态机或回答副本 |

| 场景 | 实现与验证证据 |
| --- | --- |
| 已知业务答案 | `tests/platform/test_worker_assistance.py` 验证查询后原生答复、状态恢复 RUNNING、重复请求拒绝；员工是否利用已有事实由技能约束，真实模型未验证 |
| 当前操作已有授权 | OpenCode 与 DSH 测试验证一次性允许/拒绝、永久放行拒绝、原生 Session 隔离；授权语义的模型判断未验证 |
| 缺少业务信息 | 任务技能明确原对话询问、重查请求再回传、模糊多任务先澄清；工具路径已验证，真人多轮聊天未验证。群聊请人 @责任员工，普通群聊不自动当作答案 |
| 群任务求助 | `test_worker_assistance_starts_new_authorized_employee_discussion` 验证新讨论、原始审计身份、只安排责任员工、成员撤销、幂等投递、内部提示不公开 |
| 多任务问题隔离 | 同员工另一任务的原生请求被拒绝；DSH t1 续答不写入 t2，来源会话不一致被拒绝 |
| 过期与停止中 | 覆盖请求已消费、历史绑定、结束任务、取消记录、待确认输入、查询期间 Session 变化；回复先重读绑定/fence，续答校验 submission |
| DSH 业务问题 | `tests/dsh/test_worker_assistance.py` 验证持久 trace 分页、同一 Session 续答、旧 submission 拒绝；运行中审批不读取尚在写入的 trace |
| 执行环境不可达 | 查询/回复异常返回失败且不泄漏测试敏感值；受理成功但刷新失败返回 accepted=true，避免重复回复 |

另验证：连续 WAITING 的新权限请求再次唤醒，同一请求重查不重复唤醒；已有私聊唤醒前严格更新任务能力，失败返回 503；版本请求头令旧 MCP 重新挂接。

## 检查结果

- `services/nexus-api/.venv/bin/ruff check app nexus_api tests`：通过。
- `node .opencode/tools/openspec.mjs validate nexus-employee-worker-assistance --strict`：通过。
- `git diff --check`：通过。
- 相关回归：**464 passed, 6 skipped**，约 70 秒；包含全部 Nexus 测试、全部 DSH 测试，以及 Task、Task MCP、唤醒、Runtime 上下文和原生工作区相关测试。

执行环境原虚拟环境缺少可选的 `agent-client-protocol`；本次仅在临时目录安装仓库锁定的 `0.8.1`，通过 PYTHONPATH 使用，没有修改共享虚拟环境或依赖声明。回归命令（在 `services/nexus-api` 执行，ACP_TEST_DIR 指向该临时依赖目录）：

```bash
PYTHONPATH="$ACP_TEST_DIR:." .venv/bin/pytest \
  tests/platform/test_worker_assistance.py \
  tests/platform/test_task_responsibility.py \
  tests/platform/test_task_wakeup.py \
  tests/platform/test_session_workspace.py \
  tests/platform/test_runtime.py \
  tests/platform/test_runtime_context.py \
  tests/nexus tests/dsh -q --disable-warnings --maxfail=4
```

全量 `tests/platform tests/nexus` 的尝试没有通过：安装器测试读取了本机 `ops/env/.env`，导致“无 Wiki 凭据应失败”的前提不成立；另有测试声明 OpenCode 1.18.35，而当前锁定版本是 1.18.33，等待就绪时超时。观察到 3 项失败后中止该次扩大回归。这些测试及 `app/runtime/opencode_server.py` 本次均无修改，没有顺带修改版本或安装器。未声称全量测试通过。

## 首轮验证时的边界（历史记录，已由后续 E2E 补充）

- 6 项跳过来自独立 MySQL、真实 AgentScope/模型测试的环境条件。没有公司 Linux AgentWork、真实模型、真实页面中的“自动回答/转问人/回传/释放资源”闭环证据，不能用替身测试替代生产验收。
- 员工判断是否能答、是否已有授权依赖模型及实际对话；此次交付为机制与技能修复，仍需部署后用真实模型检查这两类决策。
- ACP 原生审批超时策略未修改；人回复时请求可能已过期，必须重新查询，不能把旧授权用于新操作。
- 原生回复发送到受理期间持有 Task 行锁；通道故障时停止操作可能等待该调用超时。慢查询阶段不持锁。
- 上线需更新 Nexus API、Task MCP 和相关后台进程；私聊在任务唤醒前、群聊在新讨论前更新技能/MCP。已有 Worker Session 中的旧提示不会自动重写；新任务使用修正后的提示。

无已发现且未修复的本次实现缺项；真实环境验收为 WARNING，保留 change 供后续验收，未执行 sync/archive。


## 2026-10-28 真实双引擎 E2E 续验

已完成独立 Linux ARM64 Worker + AgentScope 2.0.10 + 真实 Nexus Gateway 模型 + 真实页面验证。OpenCode/DSH 的已知答案直接回传、未知答案转问人均完成；真实 ACP 一次性权限与群内新讨论唤醒、原生请求回传也已验证。6 条成功、1 条失败样本取消，两位 Worker 占用归零，DSH owner/子进程释放，临时栈已清理。

E2E 发现并修复残留 Workspace 文案、群 user HintBlock 导致官方 /chat/ 422，以及 OpenCode assistance 无执行输出导致员工要求人查工作区的问题。复用原生分页消息与脱敏投影补齐输出，新增分页及排除用户提示测试，真实员工重查读取 exit_code=0 并在群内完成汇报及人工确认。

最终相关回归 **491 passed, 6 skipped**；Ruff、diff check、OpenSpec 严格校验通过。逐条 Scenario 核对及证据详见 [真实 E2E 报告](../../../docs/reviews/nexus-worker-assistance-e2e-20261028/README.md)。

仍为带边界的测试环境验收：DSH 发生真实网关 503 并显式恢复，群模型回复曾失败，不能称首次全程无故障；公司 MySQL/CentOS/安装器/耐久未验证，早前全量回归失败未消除。未提交、推送或部署既有服务，未 sync/archive。


## 状态中文展示补查

增加面向人汇报状态的契约场景。默认状态事件、唤醒提示与聊天侧栏复用既有中文状态定义；员工任务工具同时返回原状态码与 `runtime_status_label`，技能要求使用中文汇报。审计 `from/to`、状态枚举及流转不变，未知员工状态不再直接显示英文原值。

后端针对性回归 266 项通过、6 项跳过；前端 30 项通过，类型检查/构建、Ruff、OpenSpec 严格校验通过。历史原始证据不改写，未重复真实模型端到端，未部署。


## 发布前最终核验（2026-10-28）

按 `openspec-verify-change` 复核现有 change：9/9 项任务完成，3 项 Requirement、10 项 Scenario 均有实现与验证对应；中文状态场景由统一展示定义和状态码不变测试覆盖。完整性、正确性及设计一致性未发现阻止提交的问题。保留已记录的模型网关不稳定和公司生产环境未验收两类警告，不将短期真实 E2E 或页面替身验证提升为生产验收。

最终组合回归 **502 项通过、6 项跳过**；聊天岛 **17 项通过**、主站 **30 项通过**，Ruff、类型检查、两处构建及 OpenSpec 严格校验通过。独立工作树先构建聊天岛，再构建主站，按仓库约定将两份聊天静态资源及主站产物纳入发布；检查源文件一致、public 与 dist 聊天产物逐文件哈希一致。

本节之后已获用户授权提交并推送 `origin` 的 `refs/heads/v1.4.8`；前文“未提交/未推送/产物未替换”为各轮验证当时的历史状态，不代表最终发布状态。具体发布提交以 Git 记录与最终远端 SHA 核对为准。未部署公司服务，保留 change 及原始验证边界，不执行历史 Spec 改写或归档。
