# Nexus `231ef85` 修复复验反馈

日期：2026-10-16（Asia/Shanghai）。基线：`cursor/nexus-v1-0-1-phase2-f6f2`，提交 `231ef85`。本报告用于交给 Claude 继续修复；原 QA 报告保留，不覆盖历史证据。

**独立结论：未通过全部验收。** 此提交确实修复了若干问题，但真实通知链路被新增接口问题阻断；已有 MCP 配置的升级、技能文件撤销、去重确认和健康检查仍有遗漏。没有修改业务源码，没有提交/推送代码。

## 本机配置与实际测试

- 已拉取并运行该提交；`ASTRA_TASK_MCP_KEY` / `NEXUS_TASK_MCP_KEY` 已在本机 `ops/env/.env` 配置为相同的非空值，0600，不在报告/证据中输出。
- 本机 Agent Service、Task API、Gateway、Nexus API、Web 的正确健康请求均 200，进程保持运行。入口：<http://127.0.0.1:5174/employees>。
- 旧会话存在下文 R2。为恢复本机验收员工的工具，仅通过已有 API 取消再选择任务技能、移除再安装 MCP，未修改业务代码。真实 DeepSeek 私聊随后实际调用 `nexus_task_get`，返回已完成任务 `T-20261016-39F2BE / SUCCEEDED`。
- Task 平台：**520 passed, 4 skipped, 1 deselected**。单独排除此前已记录的 `.invalid` 本机 DNS 用例，不能把这 1 项当作通过；这也解释与对方“521 passed”的数量差异。
- Nexus API：**42 passed, 4 skipped**；Agent Service 新增去重单测 **1 passed**；前端类型检查和生产构建通过；Task Ruff 通过。
- 使用新 CI 的显式 `--config services/nexus-api/pyproject.toml` 命令，**Nexus Ruff 通过**。自动发现配置的另一种调用出现 I001，不能代替精确 CI 命令的结果；该项已从缺陷列表撤下。没有运行任何 `--fix`。
- 本轮未再次运行全部四项聊天/团队真实联调，也未重跑完整远程部署。此前通过的历史结果不算本轮新证据。本轮新增真实模型验证仅为恢复密钥后的 Task MCP 查询。

证据：[Task tests](evidence/nexus-qa-retest-231ef85/task-tests.log)、[Nexus tests](evidence/nexus-qa-retest-231ef85/nexus-tests.log)、[Agent tests](evidence/nexus-qa-retest-231ef85/agent-tests.log)、[构建](evidence/nexus-qa-retest-231ef85/web-build.log)、[精确 CI Ruff](evidence/nexus-qa-retest-231ef85/nexus-ci-lint.log)、[真实 MCP 查询](evidence/nexus-qa-retest-231ef85/chat-mcp-proof.json)、[健康](evidence/nexus-qa-retest-231ef85/health.json)。

## 原问题逐条复验

| 原编号 | 独立结论 | 本轮证据/限制 |
|---|---|---|
| QA-01 身份与归属 | 基础鉴权、归属检查通过；升级现有会话未闭环 | 无/错误密钥 401；不同员工查询返回 ok=false；合法责任员工 ok=true。但已有 MCP 不更新密钥，见 R2 |
| QA-02 技能撤销 | 部分修复 | 取消后 `nexus-task` MCP 已移除；“任务管理”技能文件仍存在，见 R3 |
| QA-03 可靠唤醒 | **未通过，有新增阻断** | 退避和租约的模拟测试通过；真实 sender→HTTP→Agent Service 返回 422，第一次就转 failed，见 R1；去重另见 R4 |
| QA-04 缺少 bundle | 通过本机复现 | 缺 `1.18.35` 静态资源返回 404；任务详情显示“未安装”，iframe 数量 0，未嵌套 Nexus |
| QA-05 停用员工 | 目标解析检查通过，重新启用后完整补投未通过 | 停用时 target 409，启用后 200；后续补投被 R1 阻断 |
| QA-06 Runtime 错误 | 部分修复 | 真实错位 Serve 返回结构化 ok=false / PENDING；仍抹去了具体冲突原因，见 R6 |
| QA-07 发送遮挡 | 通过本机复现 | 1440×1000 发送按钮 y=798，中心不再命中 dock；无 pageerror |
| QA-08 本机启动与健康 | 部分修复 | macOS 已可启动，无需 setsid；HTTP 500 仍被健康函数判为成功，见 R5 |
| QA-09 Serve 连接检测 | 通过本机复现 | 读取真实 `14096` 错位 Serve，检测明确返回“不一致”；测试未写入或接管该 Serve |
| QA-10 Ruff / CI | 通过精确 CI 命令 | 原 E501 消失；新 CI 的显式配置 Ruff 命令本机通过。没有据此声称远端 CI 全部通过 |
| 执行中进度文案 | 已有代码修正，本轮未新增工程执行验证 | `TaskService.transition` 新增 RUNNING 文案更新；不据静态代码单独声称真实生命周期已验收 |

证据：[鉴权](evidence/nexus-qa-retest-231ef85/mcp-auth-proof.json)、[撤销和密钥](evidence/nexus-qa-retest-231ef85/skill-and-key-upgrade.json)、[浏览器](evidence/nexus-qa-retest-231ef85/browser-proof.json)、[连接检测/失败结果](evidence/nexus-qa-retest-231ef85/runtime-proof.json)、[停用/启用](evidence/nexus-qa-retest-231ef85/disabled-target-proof.json)。

## 尚需修复的 6 项问题

### R1 / P1：新的任务提示接口不可调用，outbox 首次投递就永久失败

**位置：** `services/agent-service/agent_service.py:21`、`:180-186`；`services/task-platform/app/runtime/task_wakeup.py` 的 HTTP body 和 `_raise_for_status`。

独立存在两处契约错误：

1. 文件启用 `from __future__ import annotations`，但 `Request` 只在 `main()` 局部 import。FastAPI 无法按 Request 类型注入，把 `request` 当成必填 query。**即使 event_id 已传字符串，POST 仍 422**：`loc=["query","request"] / Field required`。`GET /openapi.json` 同时 500，后台明确报 `ForwardRef('Request') is not fully defined`。
2. `TaskHintBody.event_id` 声明为 str，发送器实际发送数据库里的整数 `row.event_id`。Pydantic 另报 `loc=["body","event_id"] / Input should be a valid string`。请求头已有事件 id 不会绕过 body 校验。

**实际影响：** 在独立临时 SQLite 创建 pending row，调用真实 `HttpWakeSender`，目标解析到本机真实 QA 员工邮箱；真实投递返回 422，`deliver_pending` 结果为 **delivered=0、status=failed、attempts=1**。因此暂时失败退避的单测通过，并不能证明实际自动通知可用。测试未在运行中业务库制造失败记录。

**验收：** 不带额外 query 的真实请求可被接口接受；两端事件 id 类型一致；使用实际发送器 + 实际接收路由覆盖 HTTP 校验，验证失败/恢复、停用/重新启用、inbox 接收后确认。不能仅 mock 成 204。

**证据：** [真实发送器结果](evidence/nexus-qa-retest-231ef85/wake-sender-end-to-end.json)、[整数 body](evidence/nexus-qa-retest-231ef85/wake-int-event-proof.json)、[仅字符串仍失败](evidence/nexus-qa-retest-231ef85/wake-route-request-proof.json)。

### R2 / P1：新增共享密钥没有同步到既有 Session 的 MCP 配置

**位置：** `services/nexus-api/nexus_api/agentscope.py:244-258`，`task_skill.py:95-108`。

**真实复现：** 上一版已挂接的 QA 员工邮箱 MCP 没有 Authorization。更新本机两端相同密钥、重启服务、重新保存员工后，API 仍返回 `agentscope.ok=true`，但读取官方 `/workspace/mcp`，旧配置仍不包含当前凭据。

**原因：** 安装时只 POST 同名 `nexus-task`，遇到 409 就当成成功返回，没有比较/替换既有配置。密钥轮换时也会发生；不只是第一次升级。

**影响/本机处置：** 单纯配置环境变量和重启后，已有聊天继续用旧 headers，调用会 401。手动取消技能、删除 MCP、重新选择后，当前密钥才进入配置；本机验收员工已这样恢复。此操作不等于代码已经支持升级。

**验收：** 更新密钥后，邮箱、既有私聊、团队 Session 的配置得到受控更新，并验证旧密钥被拒绝、新调用成功；保存/同步结果不能把配置冲突当成功。

**证据：** [before_resave / after_resave / after_remove_and_reinstall](evidence/nexus-qa-retest-231ef85/skill-and-key-upgrade.json)。证据只记录凭据是否匹配，不包含凭据。

### R3 / P2：撤销技能删错名称，Skill 文件仍可加载

**位置：** `services/nexus-api/nexus_api/task_skill.py:113`。

**真实复现：** 取消后员工 `skills=[]`，MCP 清单已为空，但官方 `/workspace/skill` 仍返回 `name="任务管理"`。

**原因：** 上传的 SKILL.md frontmatter 名称为“任务管理”，AgentScope 实际以该名称保存；删除却传本地选择 id `task-management`，404 被忽略。新增单测只验证发出了这个 DELETE，没有验证官方服务实际保存的名称。

**验收：** 以真实 Skill 标识删除并重新读取确认；覆盖重新添加。MCP 工具撤销与 Skill 文件撤销分别验证。当前不能再调用已删除的 MCP，这部分修复应保留。

**证据：** [after_revoke](evidence/nexus-qa-retest-231ef85/skill-and-key-upgrade.json)。

### R4 / P1：去重把“投递中”误当成“已接收”，可能丢通知

**位置：** `services/agent-service/agent_service.py:37-38`、`:187-188`。

**独立复现：** 同 event 的请求 A 开始投递，尚未被 inbox 接收；请求 B 同时到达，`claim_event` 因 inflight 返回 false，接口对 B 立即响应 **200 / ok=true**。随后 A 的 inbox 投递失败，A 响应 500。结果是 **inbox 接收 0 次，但 B 已得到成功确认**，发送端可据此标 delivered。

这是“错误确认导致丢失”，与对方反馈的“重启后至多多投一次”是不同问题，任务状态机拒绝非法变化不能补救没有收到的通知。

**验证边界：** 使用该提交实际注册的路由和隔离 ASGI 调用，模拟 inbox 失败；仅在独立测试进程补全 `module.Request` 的全局绑定，以绕过 R1 的注解问题、单独验证去重。没有改业务源码或运行中的 Agent Service；本条属于隔离故障注入，不能描述为已在生产并发下发生。

**验收：** 区分已完成与投递中；后者等待真实结果或返回可重试状态。成功确认必须意味着 inbox 已接收。另覆盖进程重启、多实例和超过 1000 条内存去重缓存淘汰的边界。

**证据：** [first_http=500 / second_http=200 / inbox_accepted=0](evidence/nexus-qa-retest-231ef85/inflight-ack-proof.json)。

### R5 / P2：健康检查仍将 HTTP 500 判为启动成功

**位置：** `ops/dev/daemon.sh:35-37`。

**复现：** 启动临时本机 HTTP 服务，`/health` 固定返回 500；调用原始 `wait_http`，退出码为 0。函数只排除空状态和 `000`，404/422/500 都被当成功。本机实际 Agent Service 启动时 `/health` 因缺 X-User-ID 返回 422，脚本也打印 ready；之后用正确身份头检查才返回 200。

**验收：** 健康检查发送所需认证/身份头，校验预期状态及必要健康字段；500 不得成功，不能只验证端口可响应。已修好的 macOS 无 setsid 启动路径应保留。

**证据：** [health_http=500 / wait_http_exit_code=0](evidence/nexus-qa-retest-231ef85/false-readiness.json)。

### R6 / P2：Runtime 失败已结构化，但仍抹去可操作原因

**位置：** `services/task-platform/app/task_tools.py:312-313`。

独立数据库绑定真实错位 Serve，连接检测正确给出“Serve 的启动 Workspace 与员工配置不一致”；Task MCP start 现在返回 `ok=false / PENDING`，但 error 只有“执行环境暂时不可用”。本次验收认可结构化修复；原 QA-06 要求的明确原因仍未满足，员工无法区分工作区冲突、网络不通或超时。

**验收：** 已知 Runtime 故障保留脱敏的错误码/原因/建议，不将所有 Exception 都泛化，也不泄漏密钥或凭据；任务状态仍由原服务管理。

**证据：** [runtime-proof.json](evidence/nexus-qa-retest-231ef85/runtime-proof.json)。

## 下一轮复验范围

先修 R1，再验证停用期间保留、启用后补投的真实闭环；随后处理 R2/R4，再收尾 R3/R5/R6。对共享密钥、会话配置和通知确认分别写真实集成检查，避免继续用 MockTransport 的 204 替代请求体校验。

内存去重在重启、多实例、缓存淘汰后会重复投递的风险仍存在；本轮没有做完整多节点/重启故障演练，不能用“状态机拒绝非法操作”替代对员工推理和工具副作用的防重验证。

本机服务已更新到本提交并保持运行；自动 Task 通知仍受 R1 阻断。DNS `.invalid`、关闭的平台助手、已知 Knowledge Base 503 本轮仍不列为产品缺陷。未修改这些环境边界，也未直接接手业务修复。
