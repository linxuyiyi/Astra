# Nexus Phase 2：c046abe 本机验收报告

日期：2026-10-17，Asia/Shanghai。验收对象：`linmumuxx-ux/Astra` 的 `cursor/nexus-v1-0-1-phase2-f6f2`，完整提交 `c046abeda9cb4ade5d891df51e22948fba68f996`。

**结论：本轮 Mac 验收通过，上一轮 N1 启停问题已在真实服务上验证修复，未发现新增阻断问题。** 已完成新管理器启动、停止、端口释放、再次启动，真实任务交付及员工通知闭环，四个真实聊天/团队集成测试，以及主要 UI 路由走查。服务保持运行，可从 <http://127.0.0.1:5174/employees> 试用。

## 问题原因与本机更新

上一轮旧管理器把虚拟环境 Python/npm 的实际命令显示误判为其他进程，跳过停止后仍清除记录，造成旧服务残留和下一次启动冲突。本次提交改为共用 Python 进程管理逻辑，以 PID 和 `psutil.create_time` 核对身份，停止后检查进程组及端口，未能确认停止时保留记录并返回失败。

本机已拉取该提交，安装 task-platform 依赖 `psutil==7.2.2`。`ASTRA_TASK_MCP_KEY`、`NEXUS_TASK_MCP_KEY` 已验证为相同的非空值，环境文件权限为 0600；报告和证据包不包含密钥值。

首次迁移时，旧记录不具备新管理器要求的进程创建时间。逐一核对命令、工作目录、创建时间和进程组后，手动停止了 11 个旧受管进程，再清除失效记录。本步骤属于旧实例迁移处置；后面的正常停止完全通过新脚本完成。更新前保留了 SQLite 数据库备份，原暂存和历史验收资料保留。

本轮只更新环境、执行验证并添加验收文档与证据，没有修改业务源码，没有提交、推送、创建 PR 或操作 master。

证据：[部署及进程身份](evidence/nexus-qa-retest-c046abe-20261017/deployment-proof.json)、[配置布尔核验](evidence/nexus-qa-retest-c046abe-20261017/local-config-proof.json)、[旧实例核验清理](evidence/nexus-qa-retest-c046abe-20261017/legacy-cleanup.json)。

## 验证结果

### 1. 新版启停：通过

依次调用 `ops/dev/start_agent_service.sh`、`start_nexus_api.sh`、`start_astra.sh` 启动，再反向停止，再次启动。三个阶段所有脚本均返回 0。

- 首次启动后，受管进程记录均包含 `create_time`。
- 停止后，8000、8100、8110、8120、8201、8202、5174、4096 共 8 个受管端口全部释放；首次启动的受管进程没有存活残留，状态记录正确清除。
- 第二次启动后，8 个端口重新监听；最终 11 个受管进程均存活，创建时间与记录一致。
- Celery Beat、control Worker、agent Worker 各一组，没有重复实例。
- Agent Service、Task API、Gateway、Nexus API、Web 的最终健康请求均返回 200。

Redis 和独立员工 OpenCode Serve 不属于这 8 个受管端口，本轮没有把它们当成平台停止失败，也没有按宽泛进程名清理其他工程。

证据：[完整启停快照](evidence/nexus-qa-retest-c046abe-20261017/lifecycle-proof.json)、[阶段退出码](evidence/nexus-qa-retest-c046abe-20261017/lifecycle.log)、[复现脚本](evidence/nexus-qa-retest-c046abe-20261017/lifecycle.py)、[最终进程数量](evidence/nexus-qa-retest-c046abe-20261017/worker-process-counts.json)、[最终健康](evidence/nexus-qa-retest-c046abe-20261017/final-health.json)。各阶段原始日志随证据目录保存。

### 2. 真实任务生命周期：通过

验收任务：**`T-20261016-16AF37`**，员工为“验收工程师1016”，Worker 为 `astra-01`，执行器为 OpenCode 1.18.33，模型为 `deepseek/deepseek-flash`。任务 ID 保留服务实际生成值。

1. 在新私聊中要求员工实际创建且仅创建一个任务。真实模型加载 Skill，并调用 `nexus_task_create`、`nexus_task_start`、`nexus_task_get`；数据库和 API 核实任务归属、来源 Conversation/Session 正确。
2. Worker 通过真实 OpenCode Session 执行，在隔离测试工程根目录创建 `qa-c046abe.txt`。实际读取文件，字节内容为 `NEXUS_PHASE2_C046ABE_OK`，Git HEAD 未变，没有新增提交。
3. 执行时任务进度为“执行中”；原生 Session 最终为 `SUCCEEDED`。人工确认前业务任务保持 `RUNNING`，符合当前人工确认契约，不把该状态差异计为缺陷。
4. 实际调用人工确认 API 后，业务任务进入 `SUCCEEDED`。
5. 实际 `task_wake_outbox` 记录 event 2218 为 `delivered`，attempts=0，last_error 为空。员工在原会话中自动醒来，查询任务后报告“任务已人工确认完成”，没有自行确认、重试或创建第二个任务。

任务和交付文件保留，便于页面查看。该验收使用真实 AgentScope、Redis、Task MCP、模型和本机 Worker，没有用模拟回复代替执行。

证据：[任务与通知闭环](evidence/nexus-qa-retest-c046abe-20261017/task-lifecycle-proof.json)、[执行日志](evidence/nexus-qa-retest-c046abe-20261017/task-lifecycle.log)、[复现脚本](evidence/nexus-qa-retest-c046abe-20261017/task-lifecycle.py)、[真实 Workspace 截图](evidence/nexus-qa-retest-c046abe-20261017/screenshots/tasks-T-20261016-16AF37.png)。

### 3. 真实聊天和团队场景：4 项通过

启用 `NEXUS_LIVE_AGENTSCOPE=1`，实际运行 `services/nexus-api/tests/test_live_agentscope.py`，结果 **4 passed**，包含：

- AgentScope Session 隔离、私聊 SSE 和 `contact_employee` 同事唤醒。
- 员工创建、修改、停用及删除与 AgentScope 的一致性。
- 私聊消息流及匿名 owner 隔离。
- 团队讨论中的员工身份展示、同事联系和重复联系抑制。

证据：[真实集成测试日志](evidence/nexus-qa-retest-c046abe-20261017/nexus-live.log)。

### 4. 浏览器页面：主要路由通过

实际 Chrome 桌面窗口 1440×1000 走查 16 个页面：员工管理、私聊、团队、任务列表、Worker 列表/详情、图书馆、网关概览/节点/路由/密钥/日志、分类设置、待处理，以及已有正常 Workspace、缺少版本包的任务详情。路由检查没有捕获到页面脚本异常或 HTTP 4xx/5xx。聊天发送按钮中心没有被底栏遮挡，缺少 1.18.35 的界面明确提示未安装。

另补查本轮新任务详情：等待 iframe 完成加载后，确认真实 OpenCode 会话文本、Shell 工具执行、交付文件和 deepseek-flash 模型均可见，没有停留在空白壳。补查发现浏览器自动请求 `/favicon.ico` 返回 404，详见下方小问题。

图书馆页面只验证路由渲染；没有执行 llm_wiki 集成测试。该走查不等于所有按钮、表单、响应式尺寸及浏览器兼容性完整验收。

证据：[16 个页面检查](evidence/nexus-qa-retest-c046abe-20261017/ui-routes.json)、[新任务 iframe 内容](evidence/nexus-qa-retest-c046abe-20261017/workspace-ui-proof.json)、[网络补查](evidence/nexus-qa-retest-c046abe-20261017/workspace-ui-network-proof.json)、[聊天截图](evidence/nexus-qa-retest-c046abe-20261017/screenshots/chat.png)、[未安装版本截图](evidence/nexus-qa-retest-c046abe-20261017/screenshots/tasks-T-20261016-39F2BE.png)。

### 5. 自动检查

| 检查 | 本机结果 | 证据 |
| --- | --- | --- |
| task-platform | 531 passed，4 skipped，1 deselected | [日志](evidence/nexus-qa-retest-c046abe-20261017/task-tests.log) |
| Nexus API 基础测试 | 43 passed，4 skipped；这 4 个真实场景另行启用并全部通过 | [日志](evidence/nexus-qa-retest-c046abe-20261017/nexus-tests.log) |
| Agent Service 事件认领测试 | 1 passed | [日志](evidence/nexus-qa-retest-c046abe-20261017/agent-tests.log) |
| task-platform Ruff | 通过 | [日志](evidence/nexus-qa-retest-c046abe-20261017/task-lint.log) |
| Nexus API Ruff | 按 CI 显式配置命令通过 | [日志](evidence/nexus-qa-retest-c046abe-20261017/nexus-lint.log) |
| 前端类型检查与构建 | 通过；保留既有 chunk 体积警告 | [日志](evidence/nexus-qa-retest-c046abe-20261017/web-build.log) |

Task 平台只显式排除 `test_probe_network_performs_real_dns_and_tcp`：本机网络把 `.invalid` 解析到假 IP，前两轮已归类为本地网络环境差异。没有修改测试断言，没有将跳过或排除项计为通过，因此本机为 531 项通过，而非对方报告的 532 项。

主要检查命令：

```bash
cd services/task-platform
ASTRA_ASSISTANT_ENABLED=true .venv/bin/python -m pytest tests -q --disable-warnings -k 'not test_probe_network_performs_real_dns_and_tcp'
.venv/bin/python -m ruff check app tests

# 以下从仓库根目录运行
PYTHONPATH=services/nexus-api services/nexus-api/.venv/bin/python -m pytest services/nexus-api/tests -q --disable-warnings
NEXUS_LIVE_AGENTSCOPE=1 PYTHONPATH=services/nexus-api services/nexus-api/.venv/bin/python -m pytest services/nexus-api/tests/test_live_agentscope.py -q --disable-warnings
.venv/bin/python -m ruff check --config services/nexus-api/pyproject.toml services/nexus-api/nexus_api services/nexus-api/tests
PYTHONPATH=services/agent-service .venv/bin/python -m pytest services/agent-service/tests -q

cd web/nexus
npm run build
```

## 新发现的小问题

**UI-01 / P3：站点图标缺失。** 打开任务详情时，浏览器自动请求 `http://127.0.0.1:5174/favicon.ico` 返回 404，并产生一条资源加载错误。`web/nexus/index.html` 没有声明图标，`web/nexus/public/favicon.ico` 不存在。实际业务请求和 Workspace 加载正常；属于低优先级页面完善，不阻断试用。交给 Claude 时可补图标及显式链接，本轮没有修改。

复现和具体 URL 见 [网络证据](evidence/nexus-qa-retest-c046abe-20261017/workspace-ui-network-proof.json)。

## 剩余风险与未验证范围

- **跨重启/多实例持久防重仍未实现。** 当前接收端去重依赖内存；不能用任务状态机拒绝非法流转推导所有员工推理和工具副作用都不会重复。本轮启停是在任务执行验收前进行，没有在投递中途制造崩溃或跨实例竞争，不能据此证明该风险已消除。多实例或要求副作用严格一次时，应另行由 Claude 设计持久事件认领契约并实现。
- 未验证 MySQL、Linux 发布、远程 Worker、多节点故障演练，以及崩溃时的任务恢复。启停结论仅针对本机 macOS 已执行路径；PID 复用、无法停止等异常路径有代码测试覆盖，没有在用户机器制造真实 PID 复用故障。
- 按要求不测 llm_wiki；本机 `ASTRA_ASSISTANT_ENABLED=false`，没有验证真实自动总结/经验生成。
- 本轮聚焦最新启停修复与主要真实流程，没有重新逐条执行前三轮所有安全/故障专项复现；自动测试通过不能替代所有生产场景验收。

可将本报告及同目录证据交给 Claude。当前没有新增需要阻断 Mac 试用的事项；正式发布仍需补足上述未验证范围。
