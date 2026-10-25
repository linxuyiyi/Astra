# DSH 数字员工协作闭环验收（2026-10-25）

## 原因与改动

在主线 `efc85a6ea` 与 DSH 适配 `b7f4d70f1` 的隔离组合上复验。DSH 未发布迁移调整为 `0005_dsh_runtime`，衔接 `0004_remove_local_auth`；保留主线临时 UID、语音与工作区恢复，补齐 DSH 配置和任务仓库选择。未修改已发布 init。

真实服务暴露并修复三个问题：

| 问题原因 | 修改 | 验证 |
| --- | --- | --- |
| async peer 接口直接调用同步投递，SQLite 写锁竞争时阻塞 Runtime writer 所在事件循环 | 同步目录调用交给 `asyncio.to_thread`，保留原投递事务/结果 | 旧行为反例失败，修后并发回归通过；真实同事投递恢复 |
| Celery review wrapper 返回 ORM Job，JSON result backend 报 EncodeError | 只返回 job_id/status 或 null，不序列化 lease | Kombu 旧行为反例失败，修后可序列化；独立 Redis result 实测 SUCCESS |
| 任务页只在载入时获取复盘，后台已完成时仍显示“等待处理” | 活跃复盘每 2 秒读真实状态，终态、换路由、卸载清理定时器 | 实页由“分析中”自动变为“已完成，2 条候选经验”，无需刷新 |

见 evidence/peer-loop-before.txt、review-result-before.txt、targeted-loop-final.txt、targeted-review-final.txt、celery-result-after.json 和 review-poll-*.png。

## 场景与环境

真实 AgentScope 2.0.10 / Redis → contact_employee / Task MCP → Nexus Worker Gateway → CentOS agentwork 的 DSH 0.2.1-rc.2 / ACP 0.8.1，经已有 Nexus Gateway 调用模型。API 18741、Agent Service 18700、Task MCP 18702、Library MCP 18712；Nexus/AgentScope 使用独立 SQLite 快照，Redis DB 9/8。原有 QA/部署栈未替换。

远端 x86_64 / glibc 2.28 / kernel 5.15.223，独立 QA 根目录与两 Worker。固定运行时来自此前已验证安装，手工装配 QA 配置并显式选择 worker 通道；健康检查真实验证 Gateway/Git/ACP/openat2/MCP 声明，未放宽健康规则。**这不构成公司安装器、MySQL 或 OS 身份/网络隔离验收。**

群 `ch_01de419c01fa` 中，A 联系 B 交换验收样例，A 通过真实 Task MCP 建第一单；B 在另一 Worker 建复用单；人工审核本轮候选后，B 再建第三单读取本轮新经验。每单使用新的任务工作区，代码不 push，不自确认。对话由员工自己推理，Nexus 没有增加脚本式多 Agent 编排。

## 真实任务结果

| Task | 执行与独立验收 | 最终状态 / 真实复盘 | 原生事件 / 消息 |
| --- | --- | --- | --- |
| `T-20261025-96E997`，A / Worker A | 旧代码 12 failed + 1 passed → 13 passed；SSH 13 passed + 7 边界 + 6 排列 | SUCCEEDED；首次 NO_FINDINGS，显式重试后 SUCCEEDED / 2 候选 | 165 / 33 |
| `T-20261025-F20055`，B / Worker B | 旧经验实际 search/get；新数据 8 passed；SSH 8 passed + 7 边界 + 6 排列 | SUCCEEDED；NO_FINDINGS / no_findings | 132 / 23 |
| `T-20261025-AF090B`，B / Worker B | 本轮新经验实际 search/get；旧 12 failed + 1 passed → 13 passed；SSH 13 passed + 7 边界 + 6 排列 | SUCCEEDED；NO_FINDINGS / no_findings，页面自动到终态 | 168 / 32 |

三个 binding 的原生 UUID 与平台 binding ID 独立，engine 均为 dsh。逐个从远端只读取得原生 JSONL 摘要、连续 seq、消息集合、receipt、owner，再与 API 分页 trace SHA 和复盘冻结 SHA/消息数核对，三者一致。在线快照 `event_gap=true` 是有限在线缓存的提示；完整性结论来自持久文件与全量分页，不把快照 cursor 当完整轨迹。

真实 Task MCP 创建三单；群内六条 WAITING/SUCCEEDED 状态通知自动送达原群。确认前第三单 WAITING、Worker B WAIT_HUMAN / count=1；确认后三单 SUCCEEDED、两 Worker IDLE / count=0，三个 binding owner 均释放，两个 Worker 无存活子进程。A 调用总结一次、B 两次，返回真实审核状态（published/verified、rejected/unverified）及零产出原因，已从 AgentScope 官方存储的 tool_call/tool_result 独立提取核对。

证据：`evidence/final-runtime.json`、`three-native-final.json`、`first-independent.json`、`second-independent.json`、`third-independent.json`、`third-task-artifacts.json`、`real-agent-tool-results.json`。修复后 Redis 中 44 次 process-task SUCCESS 结果可 JSON 解码且无 EncodeError；这是调度执行次数，不是 44 个复盘业务作业。

## 经验积累与复用

首任务第一次复盘为 NO_FINDINGS/no_evidence_backed_candidate，保留该事实。为验证复盘页面刷新修复，人工显式重试首任务复盘，得到 2 条候选，逐条对照独立 SSH 与原生证据：

- `KNOW_4A95036E5C0E4870B83D`：核对 seeded HEAD，分开记录真实仓库红测与合成对照。证据支持；仅在 QA 目录人工审核发布、标为 verified。
- `KNOW_BDC326F6F9954A88847A`：裸源 ID 不能当 wiki 路径这一参数约束成立，但“先失败再改用路径”的叙述与原生记录相反。已驳回保留，未发布。

第三单必须通过真实 search/get 读取第一条新经验，不能只引用用户提示中的知识 ID。evidence/new-knowledge-published-and-used.png 和 candidate-review-result.json 可核对来源任务、发布状态、目录及新任务 read 引用；最终 native crosscheck 独立确认源内容确实返回。

## 检查

- 后端全量 `tests/platform tests/nexus tests/dsh`：**1406 passed, 33 skipped**（190 秒）；跳过项不计通过。日志含既有弃用/连接回收告警，未将告警隐藏或声称零告警。
- 关键定向回归：115 passed；增加 Celery wrapper 序列化反例后相关组合 119 passed。
- Ruff `app nexus_api tests` 通过。
- Web：30 tests passed，type-check/build 通过；构建仍有大 chunk 提示。
- chat-island 未改代码，复用主线构建，并通过实际群聊/联系/建单/通知操作验证；未声称本轮独立重建。

日志均为去行尾空白的证据副本，原始日志留在本机私有临时目录。没有提交完整对话/native 轨迹、私有配置或服务原始日志；原生文件仅留摘要/计数/工具归属。

## 流程效率与剩余风险

功能链路需要“提交任务 → 独立验收 → 人工确认 → 审核候选”几个明确动作；Worker 分派、执行通知、复盘入队、状态刷新和知识索引自动完成。第三单建单到启动约 12 秒、建单到人工确认约 4 分 43 秒（包含人工检查），确认到复盘终态约 2 分 12 秒；完成通知在确认约 30 秒后投递。这些是本轮功能耗时，不是吞吐或 SLA，见 `task-review-timings.json` 和消息 created_at。第二单耗时也包含排查、页面验证和人工等待，不能作为纯引擎速度对照。

首轮同事交流有重复联系并触达讨论轮次上限，且 A 使用了已可见的历史 B 样例后才收到新回复；不能称为一次提示即完美自治。后两轮直接建单、结束对话，避免无用追问，实际复用路径更短。首单也保留一次裸知识 ID 读取失败，未改写为全工具成功。

本轮修复验证的是流程闭环与真实执行。100 任务/72 小时、长 Session、其他模型与平台、公司安装器/离线包、独立系统身份及 OS 网络策略等原有四项门禁仍开放。默认引擎不切换，OpenSpec 不归档；短测不等于生产验收。首次配置读取曾输出范围过大，后续采用脱敏字段与实际凭据扫描，报告不收录私有值。


## 资源收尾与交付边界

通过 PID/create_time/cwd/pgid 在动作前重新核对归属，确认全部任务与复盘终态、占用0、无DSH子进程后，停止本轮专用7个本地进程（含SSH转发）和2个远端Worker。原18520/18641监听进程及旧15164/15165 Worker的身份在前后保持一致。数据库、原生轨迹和QA源目录保留，无凭据清空、无原服务替换；18741等本轮验收端口已停止，截图与结构化证据可离线审阅。见 `resource-before-cleanup.json` 与 `resource-cleanup.json`。

代码保存在 `codex/dsh-collaboration-e2e` 的本地集成提交；本轮未合入或推送 v1.4.8，主线先前的协作修复推送与本轮DSH集成分开。原DSH capability仍有4项门禁未完成。
