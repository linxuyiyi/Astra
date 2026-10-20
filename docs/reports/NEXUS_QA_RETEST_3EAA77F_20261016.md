# Nexus `3eaa77f` 本机复验

日期：2026-10-16（Asia/Shanghai）。分支：`cursor/nexus-v1-0-1-phase2-f6f2`。提交：`3eaa77f1bd58fd81199ddf0ee7cc5565e7dd455a`。

用户转来的反馈仍描述 `231ef85`；检查远端发现已有后续修复 `3eaa77f`，因此本轮实际拉取、部署并验证后者。

**结论：上一轮 6 项剩余问题在本轮验证范围内通过。另发现 1 项 macOS 服务停止误判缺陷，尚需 Claude 修复。** 本机页面和服务已恢复，未修改、提交或推送业务源码。旧两轮报告保留，不能将旧提交的失败结果当作当前提交仍然失败。

入口：<http://127.0.0.1:5174/employees>。两端 Task MCP 密钥仍相同、非空，保存在本机受保护环境文件，未放入本报告/证据包。

## 上轮 6 项复验

| 上轮编号 | 结果 | 本轮实际证据 |
|---|---|---|
| R1 通知接口 422 | 通过 | 运行中的 Agent Service OpenAPI 200，无错误的 request query 参数。真实 HttpWakeSender → 真实 HTTP 接收 → Redis inbox → 数字员工推理成功 |
| R2 既有 MCP 密钥不更新 | 通过 | 在真实旧邮箱 Session 安装过期测试凭据；保存员工后官方 MCP 配置被替换为当前凭据，未手动取消再选择来绕过同步 |
| R3 技能文件残留 | 通过 | 取消 task-management 后，官方 MCP 列表和 Skill 列表均为空；重新选择后两者恢复 |
| R4 inflight 被错误确认 | 通过隔离故障注入 | 使用生产 register_task_hint 注册路由；A 投递失败返回 500，投递中的同事件 B 返回 409，未返回假成功。未补全注解或修改生产代码 |
| R5 HTTP 500 被当健康 | 通过 | 独立本机 HTTP 服务固定返回 500，原 wait_http 返回非零；新启动脚本带正确 AgentScope 身份头，启动检查通过 |
| R6 Runtime 原因泛化 | 通过 | 对真实错位 Serve 的 Task start 返回 ok=false、PENDING、WORKSPACE_CONFLICT 和明确提示，不再只有“暂时不可用” |

### 真实通知恢复与员工查询

本轮不是仅将接收器 mock 成 204：

1. 在独立临时 SQLite 建立 QA outbox，初始 attempts=31，目标是本机真实验收员工的真实邮箱 Session。
2. 通过 Nexus API 停用员工后，真实发送器得到 409；outbox 仍为 pending、attempts=32，没有因达到 30 次而永久失败。
3. 重新启用该员工，按下一次重试时间扫描，结果 delivered=1、status=delivered。
4. 向运行中的接口再次传入相同整数 event_id，返回 200；原生记录中本次测试 HintBlock 仅 1 条。
5. 数字员工实际调用 `mcp__nexus-task__nexus_task_get`，报告原 QA 任务当前状态 SUCCEEDED；没有创建、修改、重试或确认任务。

该测试仅将 outbox 放在隔离数据库，接收服务、Redis、AgentScope Session、模型和 Task MCP 均为本机真实服务。没有在运行中的 Task 数据库制造失败记录。验收员工最终恢复为 enabled=true，技能/MCP 也已恢复。

证据：[真实通知恢复与原生工具名](evidence/nexus-qa-retest-3eaa77f/live-wake-proof.json)、[MCP 更新/撤销/恢复](evidence/nexus-qa-retest-3eaa77f/capability-proof.json)、[inflight](evidence/nexus-qa-retest-3eaa77f/inflight-proof.json)、[健康拒绝](evidence/nexus-qa-retest-3eaa77f/readiness-proof.json)、[Runtime 原因](evidence/nexus-qa-retest-3eaa77f/runtime-proof.json)、[OpenAPI](evidence/nexus-qa-retest-3eaa77f/openapi-proof.json)。

## 检查结果

- Task 平台：**525 passed, 4 skipped, 1 deselected**。仍显式排除本机 `.invalid` DNS 被映射到假 IP 的一项，未修改断言；4 项 skip 不算通过。
- Nexus API：**43 passed, 4 skipped**；本轮没有重跑那四个完整真实聊天/团队场景，以这次专项真实通知和模型查询作为新增运行证据。
- Agent Service 去重单测：**1 passed**。
- Task/Nexus 两套 Ruff 通过，Nexus 使用精确 CI 显式配置命令。
- 前端类型检查与构建通过，仍有既有 chunk 体积警告；本轮没有重新执行 UI 全量路由检查。
- Agent Service 8000、Task API 8100、Gateway 8110、Nexus API 8120、Web 5174 的正确健康请求均返回 200。

证据：[Task tests](evidence/nexus-qa-retest-3eaa77f/task-tests.log)、[Nexus tests](evidence/nexus-qa-retest-3eaa77f/nexus-tests.log)、[Agent tests](evidence/nexus-qa-retest-3eaa77f/agent-tests.log)、[Task lint](evidence/nexus-qa-retest-3eaa77f/task-lint.log)、[Nexus lint](evidence/nexus-qa-retest-3eaa77f/nexus-lint.log)、[构建](evidence/nexus-qa-retest-3eaa77f/web-build.log)、[健康](evidence/nexus-qa-retest-3eaa77f/health.json)。

## N1 / P2：macOS 服务停止误判身份，遗留旧进程并导致下一次启动冲突

**位置：** `ops/dev/astra_service.py:154-172`、`:598-615`。

**实际复现：** 更新前调用 `ops/dev/start_astra.sh stop`，管理器对自己先前启动的 Backend、Celery、Library MCP、Gateway、Frontend 打印 `[SKIP] ... is not the managed command`，随后清除状态并输出 `Astra stopped.`、返回 0。旧进程实际上继续监听 8100/8110/5174。

随后启动新版本时，新 Backend/Library/Gateway 进程因端口占用退出，新的 Celery Beat/Worker 与旧实例并存，Vite 可能漂移到其他端口；入口健康请求还可能命中旧进程。本轮第一次 start 最终返回 1，不能把启动过旧服务的端口检查当成新版本部署成功。

**原因：** `command_matches` 只检查 `command[0]` 是否出现在 ps 命令字符串中。本机状态记录的是 `/Users/niuniu/程序/Astra/.venv/bin/python`，macOS ps 显示真实 Homebrew `.../Python.app/.../Python`；npm 记录的是 `.../npm-cli.js`，ps 显示 `npm run dev ...`。因此合法受管进程被误判。停止循环跳过后仍无条件删除状态与返回成功。

**再次只读验证：** 在已经干净重启的新实例上，逐个调用该提交自身的 `command_matches`：Backend、Beat、control/agent Worker、Frontend 均为 false，Task MCP 为 true。证据包含受管 PID、记录的 command[0] 和对应 ps 结果，不依赖猜测 PID 已被复用。

**本机处置：** 逐一核对旧/新受管 PID 的命令、真实工作目录和进程组，只对匹配本次 Astra 实例的进程组发送 SIGTERM；保留证据后清除失效状态，重新完整启动成功。邻接 Nexus 工程、旧独立员工 Serve 和无关进程未被清理。当前服务保持运行，源码中的误判仍存在，因此后续 stop/restart 仍有风险。

**Claude 修复验收：**

- macOS 上用虚拟环境 Python、npm 启动的合法服务可正确识别、停止并确认退出。
- 同时保留 PID 复用防护，不能为了修误判取消身份检查或按宽泛进程名杀进程。
- 有受管进程尚未停止时，不得清除全部记录并报告完全停止；未能确认的进程保留可恢复记录。
- 真正执行 start → stop → 端口释放 → start，验证只有一组 Beat/control/agent 与固定端口的服务；补同平台实测和针对性测试。

证据：[当前新实例的错误身份判断](evidence/nexus-qa-retest-3eaa77f/manager-identity-proof.json)、[第一次启动退出及状态](evidence/nexus-qa-retest-3eaa77f/astra-startup.log)、[失败启动时进程记录](evidence/nexus-qa-retest-3eaa77f/failed-first-start-state.json)、[经身份核验的本机清理](evidence/nexus-qa-retest-3eaa77f/verified-local-cleanup.json)、[干净重启成功](evidence/nexus-qa-retest-3eaa77f/astra-second-start.log)。

## 剩余边界

- 内存去重在重启、多实例、超过 1000 条缓存淘汰后仍可能再次投递；本轮验证了 inflight 错误确认已修复，没有验证持久化跨实例防重，也没有接受“状态机拒绝非法变化”可以覆盖全部员工推理/工具副作用的推论。
- Linux 发布、MySQL、多节点/重启故障演练、远程 Worker、llm_wiki 发布、自动总结等未在本轮重新验收。
- 本次完成的是指定提交的本机专项复验，不代表全部产品或生产部署通过。
