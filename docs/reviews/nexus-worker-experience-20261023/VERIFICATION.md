# Nexus v1.4.8 真实 Worker 与数字经验闭环验证

日期：2026-10-23。基线为 `174bcbd9949797995751dda596010902f64e2f1f`，在 `codex/nexus-interaction-fixes` 隔离 worktree 上继续修复。代码文件 SHA-256 和进程身份见 [environment.json](evidence/environment.json)。原开发工作区、主数据库、原有 12 名员工及既有 Worker 未改动。

## 结果

**真实 Linux Worker 执行 → 人工确认 → 后台复盘 → 候选审核发布 → Wiki 同步 → 新任务实际检索读取并复用**已跑通。真实执行和服务交互没有 Mock；自动化测试使用仓库既有的外部接口替身，单独计数。

| 任务 | 实际结果 | 用途 |
| --- | --- | --- |
| `T-20261023-21DBC7` | PENDING，repository_id=null，未执行 | 修复前的 Task MCP 空仓库反例，保留供复核；没有伪造执行结果或批量回填 |
| `T-20261023-2863C9` | SUCCEEDED，Worker 已释放，复盘成功 | 第一次真实修复；4 项测试通过，产生 3 条经验候选 |
| `T-20261023-9319D1` | CANCELED，人工核实停止后释放 | 修复前内部工具上下文失败；越界读取配置被原生权限门拦截并拒绝 |
| `T-20261023-CD86DB` | SUCCEEDED，Worker 已释放，复盘成功 | 修复上下文后自动领取执行；期间修复读取归因，再做真实只读重读确认 |
| `T-20261023-1B83BA` | CANCELED，从页面人工确认释放 | 真实运行只读 QA 命令，核实 native idle；取消不自动释放，页面入口及身份正确 |
| `T-20261023-A0C02E` | SUCCEEDED，Worker 已释放，复盘成功 | 最终新任务复验：首次读取即正确关联经验；独立 4 PASS；页面确认后产生 3 条候选 |

证据：[最终任务快照](evidence/final-tasks.json)、[来源/读取/通知/释放审计](evidence/final-audit.json)、[Worker 最终健康](evidence/worker-final-health.json)。最终 Worker `qa-worker-experience-20261023` 为 IDLE，current_task_count=0。

## 问题原因与修改

| 问题 | 根因与修改 | 验证 |
| --- | --- | --- |
| Worker 机器请求被要求浏览器登录 | 统一认证先拦截 `/api/worker-gateway/`；交回既有协议 token、epoch 和管理密钥依赖校验 | 有效管理密钥/Worker token 到达协议；无效 token 拒绝；原有 epoch、命令归属测试；真实正式部署及执行 |
| 维护、审核、停止确认身份错误 | 旧处理器读自报 UID 或默认 admin；统一服务组合根适配到已验证登录会话 | 无旧身份头成功；伪造头无效；普通聊天权限受限；真实发布和释放均记录 `usr_18b6ad196291` |
| 员工建任务成功但永远不调度 | Task MCP 没有目标仓库，指定 Worker 也不继承环境绑定 | 新增只读仓库发现与 repository_id；创建/改派校验一致性；缺少目标拒绝；内容幂等键含仓库。真实员工创建及 MySQL 新库验证 |
| 任务通知查来源失败 | 内部 wake-target GET 缺服务间鉴权头 | 加既有 internal_headers；协议回归；最终新任务相关通知 delivered。首次测试还同时存在 ASTRA/NEXUS 地址配置不一致，已纠正测试环境，不归咎为第二个代码缺陷 |
| 复盘拒绝合法任务目录 | 正式任务 worktree 位于根目录旁 `.astra-tasks`，旧代码只允许根目录内 | 复用现有路径边界，并验证同一 Git 仓库和 worktree 注册归属；5 类路径回归；官方更新 Worker 后真实重试成功 |
| 模型拿不到工具上下文 | synthetic 文本同时带 ignored=true，OpenCode 1.18.35 确实不向模型提供 | 移除 ignored；同一真实模型 canary 对照：旧标记看不到、移除后能看到；最终新任务真实 MCP 调用通过 |
| 复盘输入可能带内部能力凭证 | 原生 user part 含 runtime_context，归一化未脱敏 | 摘要在截断前脱敏，候选再拒绝凭证；真实读取对照和自动化回归；交付证据再次扫描脱敏 |
| 成功复盘仍显示旧失败，证据缺作业 ID | 成功终态未清旧错误，候选证据未写 review_job_id | 修复终态和证据写入；旧 QA Job 用显式目标一次性脚本预览/修复/幂等复跑；最终 29 条证据均关联 Job |
| 经验被读过但详情没有引用任务 | Wiki 编译来源页使用 YAML sources，旧 ID 正则未识别真实来源 | 仅按结构化元数据和已发布 source_path 唯一关联；正文 ID、多来源不猜测；正确读记录与引用计数落库 |
| 取消后没有正常释放入口，页面仍催确认完成 | 前端没有复用既有停止确认接口，并优先显示旧 waiting 文案 | TaskRead 由唯一占用谓词导出 execution_released；增加明确确认入口，真实登录审计；取消文案以当前任务为准 |
| 图书馆把执行资源显示为数字员工 | 来源及使用记录实际外键为 AgentWorker | 详情、引用表和复制信息改为“来源 Worker / 执行 Worker”；保留真实来源关系，责任数字员工可从来源 Task 追溯 |

没有新增 Agent 编排、Agent Memory、任务状态机或数据库字段。复盘仍只产生候选，正式发布需人工审核；Task 成功与原生 Session 执行结束保持区分。

旧复盘元数据修复脚本为 `tools/repair_experience_review_metadata.py`：必须显式提供 ASTRA_DATABASE_URL 和 Job ID，默认只预览，`--apply` 才提交；仅处理已完成 Job、确定候选/Task/Session 且 validated 的缺失关联，不推断未知历史来源。当前只应用于本轮隔离 QA Job。

## 实际执行与经验复用

第一次任务在正式 Linux OpenCode Session `ses_e99b48b62ffeI6v9TH4NaefiDF` 中改动 `result_summary.py` 和 `test_result_summary.py`，按 `(run_id, case_id)` 保留最后一条后汇总。独立 SSH 验证 4 项测试通过。确认完成后，复盘 Job `xrj_e64423b9bd2f4cc3a9ac72fd27d25fc3` 生成 3 条候选。

人工从真实页面审核发布 `KNOW_53022A1009074C6E95B4`（按业务键保序去重并保留最后一条记录）到 QA 目录。最初 Wiki 未运行，发布状态与同步失败分别保留；启动已有 LLM Wiki.app 后，使用原配置的真实 token/project 权限重同步成功。Wiki 实际编译来源页为：

`wiki/sources/5-astra--9-knowledge--24-know53022a1009074c6e95b4--fzfk91.md`

最终新任务 `T-20261023-A0C02E` 通过数字员工 A 的真实 Task MCP 创建，自动领取并启动 Session `ses_e98d3291effeMWL171XBlwwp6q`。原生轨迹有 library_search、library_get、文件读取/写入及测试输出。**首次 library_get 已关联上述知识与该 Task/Session**；无手工注入工具上下文或代替 Worker 修改文件。独立复跑后从页面确认完成，Job `xrj_1425c93e8c744faa9e07db44872079c8` 成功生成 3 条候选，重复确认保持同一 Job。三次成功复盘合计 9 条候选，当前只人工发布 1 条，其余保持待审。

证据：[员工真实建任务](evidence/reuse-v3-task-create-chat.json)、[原生工具和答复轨迹](evidence/final-run-execution-trace.json)、[独立修复前后验证](evidence/final-run-red-green-independent.json)、[最终复盘 Job](evidence/final-run-review-final.json)、[已发布并被复用的经验](evidence/published-reused-final.json)、[引用页面](evidence/experience-reused-final.png)。知识引用次数为 2，分别来自第二次任务修复后重读与最终新任务首次读取；早期未关联读记录保留为 null，未伪造回填。

## 流程审视与验证边界

1. **命令退出码可能被 shell 包装掩盖。** 最终任务曾运行 `unittest | tail; echo "$?"`：输出明确 2 FAIL，shell 元数据却为 0。已独立用 subprocess 分别运行 seeded HEAD 与修复代码，真实退出码为 **1 / 0**，修复后 4 PASS。模型“已完成”和工具 completed 都不足以证明测试通过；本轮人工核验通过，平台没有新增通用 shell 语义判定器。
2. **原生权限门起作用。** 上下文失败任务尝试读取 `/etc/nexus/opencode/5/opencode.jsonc`，external_directory gate 拦截；已拒绝该请求并取消任务。修复上下文后新任务无需读取部署配置。[权限证据](evidence/runtime-context-permission-block.json)
3. **经验不是个人记忆自动更新。** 本轮沉淀到受审核的正式图书馆，经真实 Wiki 编译后由新任务调用工具检索；没有宣称写入 AgentScope Memory，亦未验证员工在未调用知识工具时会自动使用该经验。
4. **候选仍需内容审核。** 多次类似任务会产生相近主题候选；现有精确内容去重不等于自动语义合并。只有本轮经人工对照测试证据的 1 条发布，其余候选没有自动变为正式知识。
5. **重启与异常范围。** 实际经历 API 重启、Worker 自动重连、合法路径修复后的复盘重试、上下文失败后的取消及人工释放；已有完整生命周期回归另覆盖并发/失败/取消窗口。未进行正在写代码时 Worker 断电、生产网络分区或主机崩溃的真实故障注入。
6. **历史通知失败如实保留。** 首个任务旧 wake 记录因修复前鉴权与测试地址配置失败，未改写为 delivered；最终新任务相关记录均已 delivered。不能把新通知成功当成历史失败已经自动补投。

## 自动化、MySQL 与页面

- `pytest tests/platform tests/nexus`：**1306 passed / 32 skipped / 0 failed**。跳过项不计通过；保留既有弃用、连接清理等 warnings。[完整输出](evidence/backend-tests.txt)
- 补强边界内另一 Git 仓库与候选凭证防护后，经验/知识/上下文/统一鉴权针对性集合：**71 PASS**。
- `ruff check app nexus_api tests ../../tools/repair_experience_review_metadata.py`：通过。
- web：Node 测试 **30 PASS**；type-check/build 通过，保留既有大 chunk 警告。[测试](evidence/web-tests.txt)、[构建](evidence/web-build.txt)
- chat-island 本轮没有源码变更；前一修复提交的产物与验证保留，不冒充本轮重新执行。
- MySQL **8.0.36**：独立私有进程、仅 Unix socket、全新库 Alembic head；仓库绑定、重复创建、仓库冲突、取消保留占用及确认释放通过。临时库已删除、该私有实例已正常停止。[结果](evidence/mysql-targeted.json)
- 真实浏览器：完成确认、经验审核发布、引用任务可见、取消停止确认和最终取消文案；最终检查没有 pageerror。[结果](evidence/final-ui.json)、[取消页面](evidence/cancel-final.png)
- 原生工作区内容实际加载并显示执行交付；探针另记录资源 404 与字体请求中止，未定位原因，不计作零网络错误。[原生工作区探针](evidence/workspace-ui.json)
- OpenSpec：见该 change 的 `verification.md`，逐 Scenario 映射；不修改历史 Astra specs。

原生模型上下文的反例直接使用真实 OpenCode/网关完成，不是仅断言一个 Python 字段：[canary 对照](evidence/context-visibility-probe.json)。针对已确认缺陷的旧行为失败输出另存 [counterexamples.txt](evidence/counterexamples.txt)。

## 环境与剩余风险

- 隔离栈：API 18520、Gateway 18511、AgentScope **2.0.10 / 18500**、Task MCP 18502、Library MCP 18512、Redis 18579，独立 SQLite/AgentScope 数据和 QA 工作区。
- Worker 是 Lima Ubuntu arm64 内独立 environment 5，由正式部署/更新服务安装，OpenCode **1.18.35**，`ASTRA_EXECUTOR_TYPE=worker`。控制机未用本机 OpenCode 代替执行，也未绕过版本门禁。测试部署使用临时 QA SSH 用户转受控 root shell，不代表公司非 root 权限模型已验收。
- 真实网关与上游模型参与了员工、Worker 和复盘调用。初期 Gateway 健康 unknown、模型版本不匹配通过正式配置/探测/更新纠正；正式安装不会凭空获得厂商上游配置。
- Git 使用 VM 内隔离 `file://` bare origin；没有验证外部 Git 凭据、公司仓库网络、push、PR 或 CI。
- Wiki 是原已配置的**真实现有项目**，并非完全隔离 Wiki；本轮仅发布明确 QA 来源，并触发其既有自动编译。没有修改 Wiki 业务代码或其他历史知识内容。
- 正式运行全链仍使用 SQLite；MySQL 新库针对性通过不等于公司 MySQL/Linux 全链部署验收。真实公司第三方 MCP、DSML 上游根因仍未闭环，沿用原报告限制。
- 测试栈和 QA Worker 保留运行便于复核，Worker 已空闲。临时凭据、部署配置、raw runtime_context 没有写入报告；证据已脱敏扫描。
