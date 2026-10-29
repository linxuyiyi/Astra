# Agent Worker 状态管理与 MySQL 并发审查

后续修复与本地验证见 [修复记录](FIXES.md)；本文保留最初审查时的事实和验证边界。

日期：2026-10-29。审查基线：本地 `v1.4.9`，`46bd6a629`。开始时没有受 Git 管理的未提交变更，存在多项无关 untracked 文件，均未修改。本地落后 `origin/v1.4.9` 引用 2 个提交；这两个提交在本次审查的 `app/runtime`、`app/worker_gateway`、`app/models.py` 范围没有差异。本轮未拉取、提交、推送或部署。

## 结论与故障对应

确认 **7 项缺陷**：3 项使用独立 MySQL 8.0.36 实例复现，4 项使用隔离数据库和受控原生客户端复现。

- “时间字段更新报错、一段时间都不动”：已复现其他候选 Worker 的 `last_heartbeat` 更新遇到 **1205 锁等待超时**；另复现维护与健康同步之间的 **1213 死锁**。两者都是应用事务问题，不能因为 SQLite 测试通过而排除。
- “实际上还在执行，却显示已中断”：已复现旧 `idle` 观测与新消息混用，以及停止未确认就标记超时中断；一次误判还可能被终态快路径固化。
- 另有 MySQL JSON 比较缺陷：无并发修改也会误报环境状态变化，影响启动、调度、维护与健康观测落库。

这些是当前代码的实证缺陷，**尚不能确定公司历史故障命中了哪一项**：没有当时的 SQL、错误号、堆栈、部署提交、任务 ID 和 Session 证据。未连接公司环境。

## 状态事实源

当前代码没有独立的 Run 模型；执行绑定与结果索引由 `RuntimeSession` 承担，Task 通过 `current_session_id` 指向当前绑定。不能为这次修复另建 Run 状态机。

| 对象 | 当前事实与职责 | 不应混用的含义 |
| --- | --- | --- |
| Task | `PENDING/RUNNING/WAITING/SUCCEEDED/FAILED/CANCELED`，业务分配与交付 | Session 本轮结束不等于业务任务已经人工验收 |
| RuntimeSession | 当前原生执行的 `RUNNING/WAITING/SUCCEEDED/FAILED/INTERRUPTED/UNKNOWN` | 传输未知不等于执行已停止 |
| AgentWorker | 执行身份，`OFFLINE/IDLE/WORKING/WAIT_HUMAN/PAUSED/ERROR` | 这是 Worker 执行身份，不是 AgentScope 数字员工的推理状态 |
| WorkerInstance | WebSocket 在线状态、epoch、心跳 | 在线不等于模型、仓库和装备健康，也不等于任务正在执行 |
| ExecutionEnvironment | 健康观测与装备期望/应用情况 | 健康检查失败不证明远端进程停止 |
| MaintenanceOperation | 部署、更新、检查、重启等维护执行与租约 | 维护 `INTERRUPTED` 是待核对状态，不是业务 Session 的停止证据 |
| AgentScope Agent/Session | 数字员工对话的官方事实源 | Nexus 不应复制出第二套 Agent 或 Memory 状态 |

Task/AgentWorker 的展示入口是 `app/runtime/presentation.py`。该模块主要投影已存储事实，下面的误判发生在写入事实的逻辑中，仅修改标签无法解决。

## F1 · P1 · MySQL 环境锁中的 JSON 比较恒不匹配

位置：`services/nexus-api/app/runtime/maintenance.py:172-188`；同类条件在 `:1830-1834`。

`lock_environment_row()` 使用 `status_detail == environment.status_detail` 作为条件，然后执行 `updated_at = updated_at` 取得行锁。MySQL 原生 JSON 对象与驱动绑定的序列化字符串不是同一种比较值。

在已由 Alembic 创建、没有并发写入的 MySQL 8.0.36 空库中，实测：

```text
status_detail = '{}'                 -> 0
status_detail = CAST('{}' AS JSON)   -> 1
仅按 id 执行 updated_at=updated_at   -> matched rows 1
实际 guard_environment_write()     -> HTTP 409 环境状态已变化
```

因此，当前这条误拒绝的根因是 JSON 比较，不能归咎于 `updated_at` 类型或 MySQL 不允许更新时间。健康观测落库使用同类条件，也可能把实际没有变化的环境当成 stale，长期不刷新健康事实。

这是 MySQL 与 SQLite 实现差异暴露出的确定性 Bug。[MySQL JSON 比较与转换规则](https://dev.mysql.com/doc/refman/8.0/en/json.html)与本次实测一致。

修复方向：复用同一环境行锁，在锁内核实当前事实；网络观测落库时保留真实的并发版本核对。避免直接把整个 JSON 对象与 SQL 字符串作相等比较，不增加另一套状态或兼容 fallback。

## F2 · P1 · 调度器批量持锁期间等待远端，阻塞无关 Worker 心跳

位置：`services/nexus-api/app/runtime/dispatcher.py:87-154`。

调度器没有只锁最终要领取的一个任务和一个 Worker，而是先将所有候选 PENDING 任务 `FOR UPDATE SKIP LOCKED` 读入列表，再将所有候选空闲 Worker 同样加锁。之后才选第一位，并在同一事务内等待远端 `workspace_head_commit()`。

MySQL 探针使用两项任务、两个候选 Worker，实际调用 `Dispatcher.claim_once()`，仅把远端 I/O 替换为可控等待。第一个 Worker 的网络等待尚未结束时：

- 第二项独立 PENDING 任务也已被锁，其他事务的 `SKIP LOCKED` 无法取到它。
- 第二位 Worker 的 `last_heartbeat` 更新实际报 **1205**。探针把锁等待超时缩短到 1 秒，仅用于缩短取证时间，不代表公司配置。

这可以形成“一个远端响应慢 → 一批候选任务和 Worker 状态都停滞”的放大效应。代码注释只避免了在环境行锁内做远端调用，遗漏此前已经获得的 Task 和 AgentWorker 行锁。

修复方向：缩小领取事务及锁定范围，完成最小原子 reservation 后提交，远端调用移到事务外；回来后依据同一 reservation 核对。不能通过增大锁等待超时掩盖。

## F3 · P1 · 维护完成与后台健康同步锁顺序相反

位置：`services/nexus-api/app/runtime/maintenance.py:510-542`、`:1798-1813`。

```text
维护完成 _finish_in_session：MaintenanceOperation → ExecutionEnvironment
健康同步 observe_environment：ExecutionEnvironment → MaintenanceOperation
```

实际调用上述两个函数，在 SQL 之间设置屏障，构造合法的并发交错，MySQL 8.0.36 返回 **1213**。InnoDB 死锁记录明确显示：健康同步持有环境行等待维护行，维护结束持有维护行等待环境行。

后台观察器虽然会预筛选没有维护的环境，但列表读取和后续网络探测之间允许新的维护开始；预筛选不能替代最终事务内一致的锁顺序。本次健康探测使用受控返回值，死锁发生在真实数据库事务中。

修复方向：统一环境、维护记录及必要 Worker/员工记录的全局加锁顺序。只允许对可安全重试的短数据库事务做有界重试，不因死锁重发远端副作用。[MySQL 官方建议](https://docs.oracle.com/cd/E17952_01/mysql-8.0-en/innodb-deadlocks-handling.html)也是缩短事务并保持一致的锁顺序。

维护续租循环在数据库异常时会取消本地执行协程（`:2324-2338`）；远端步骤可能已经发出。因此锁异常还可能传播为维护“已中断待核对”，不能把这个维护标签作为远端已停止的证明。

## F4 · P1 · 旧 idle 观测与较新消息混用，执行仍忙却判中断

位置：`services/nexus-api/app/runtime/lifecycle.py:1727-1730`、`:1850`、`:1901-1912`；`app/runtime/session_state.py:192-198`。

对账先读取原生状态，再读取子 Session、问题、权限和消息，最后仍使用最开始的状态判定。期间原生 Session 可以已经进入 busy。当前 Session/anchor 检查只能识别平台绑定变化，不能证明多次原生读属于同一快照。

受控交错结果：状态接口先返回 idle；消息读取时客户端已经 busy，返回当前回合未完成的 assistant；平台写入 `INTERRUPTED/incomplete`，Task 进入 WAITING。

15 秒 grace 实际从 `turn_started_at` 计算，是本轮启动时间，不是“连续观察到 idle”的时长。执行数分钟后偶遇一次不一致观测，就没有新的确认窗口。

修复方向：在写入失败/中断前核实原生观测的一致性，不能只凭旧 idle 加本轮经过时间判终态。复用原生状态与完成证据，不根据事件数、日志或最后活动时间推断。

## F5 · P1 · 推断出来的失败/中断被当作不可纠正的终态

位置：`services/nexus-api/app/runtime/lifecycle.py:1795-1817`。

只要不是 `UNCONFIRMED`，WAITING 任务绑定的 FAILED/INTERRUPTED 就被视作“confirmed terminal”。当原生 idle 时直接回写旧终态，不再读历史。

探针先产生 `INTERRUPTED/incomplete`，再让同一原生回合出现带 `completed + finish=stop` 的真实完成消息。下一次对账未读取消息，仍保留中断。若其间观察到 busy，现有代码可以恢复；缺陷出现在 busy 窗口未被平台采样、下一次读取时原生已经完成的情况。

修复方向：区分原生明确终态与平台暂时推断，不让后者阻止同一回合更强完成证据的收敛。F4 与 F5 组合会将短暂误判扩大为持续错报。

## F6 · P1 · 超时仅调用 abort，未确认停止就写已中断

位置：`services/nexus-api/app/runtime/lifecycle.py:1822-1832`。

达到 `opencode_timeout_seconds` 后调用 `client.abort()`，忽略布尔回执，直接令 `timed_out=True`。后续推导中 timeout 优先于原生 busy。

探针让 abort 返回 false 且原生继续 busy，平台仍写 `INTERRUPTED/timeout`。即使回执为 true，它本身也不等于已观察到进程停止。现有手动停止/重启路径已有停止核实逻辑，超时路径没有保持同一约束。

修复方向：超时触发停止请求与停止得到确认应使用一致的事实规则；未确认时保留真实运行/未知信息及占用，不宣告已中断。探针未调用真实远端 abort。

## F7 · P2 · 心跳使用缓存身份对象，覆盖并发暂停

位置：`services/nexus-api/app/runtime/lifecycle.py:2132-2205`；`app/database.py` 的 `expire_on_commit=False`。

心跳开始时加载员工对象，网络探测后 `session.get()` 仍可能返回 identity map 中的旧对象，变量名 fresh 不保证新读。对 PAUSED/ERROR 的保护也只检查旧值。

探针先将有占用任务的员工置为 OFFLINE；健康探测期间另一事务提交 PAUSED；心跳用缓存 OFFLINE 对象恢复为 WORKING，覆盖了 PAUSED。这个复现依赖状态确实发生变化；不能声称任何 WORKING 心跳都会写掉暂停，因为 ORM 会省略没有净变化的字段更新。

修复方向：短事务内重读并保护控制状态，或使用带当前状态条件的原子更新；保留现有占用规则作为唯一事实源。

## 验证记录

| 范围 | 结果 | 边界 |
| --- | --- | --- |
| 独立 MySQL 8.0.36、REPEATABLE-READ，全新空库 `alembic upgrade head` | 成功，执行至 `0005_dsh_runtime` | 独立本机实例，未访问公司库或本机已有业务实例 |
| MySQL 取证探针 | JSON 误拒绝、1205、1213 均复现 | 替换远端 I/O并控制交错；数据库与应用函数为实际实现 |
| 原生状态取证探针 | 4 passed | 验证缺陷存在，断言的是当前错误表现，不是修复后的期望行为 |
| 第一组现有针对性测试 | 124 passed、6 skipped、1 failed | 覆盖状态推导、Runtime、占用、维护身份、Gateway 故障、schema |
| 第二组现有针对性测试 | 239 passed、1 skipped | 覆盖 Session Workspace、恢复 Supervisor、维护、故障路径 |

现有测试合计 **363 passed、7 skipped、1 failed**。失败为 `test_connect_start_health_uses_the_worker_channel`：测试桩上报 OpenCode 1.18.35，而当前配置仅接受 1.18.33，进入版本校验后失败；本轮未改该配置或测试。跳过项目未计为通过。这些现有测试主要使用 SQLite/受控客户端，不能替代本次 MySQL 取证。

可复现材料：

- [原生状态探针](reproduce_state_defects.py)与[输出](evidence/state-probes.txt)。从 `services/nexus-api` 用 `.venv/bin/pytest -c pytest.ini -q -s <脚本绝对路径>` 运行。
- [MySQL 探针](reproduce_mysql_locks.py)、[输出](evidence/mysql-probes.txt)、[迁移记录](evidence/mysql-migration.txt)及[真实 InnoDB 死锁记录](evidence/mysql-deadlock.txt)。须指定以 `nexus_state_audit` 开头的专用空测试库，先运行 Alembic；脚本不会清空已有库，重复执行请使用新的空测试库。
- [现有测试命令与结果摘要](evidence/existing-tests.txt)。

## 处理顺序与剩余风险

建议先修 F1–F3 的 MySQL 比较、事务范围和锁顺序，再修 F4–F6 的中断判定与结果收敛，同时补 F7 的并发控制状态保护。每项修复应将取证案例转换为断言正确行为的回归测试；不能把当前探针直接作为产品行为契约。涉及业务状态定义或跨模块契约调整时，先按仓库 OpenSpec 流程定契约，复用既有事实源。

本轮只新增审查文档、取证脚本和证据，**没有修复生产代码，也没有执行生产验收**。未验证公司实际部署版本、MySQL sql_mode/隔离级别/DDL、现场长任务、真实模型、网络抖动和多控制节点压力；DSH 特有 ACP receipt 与进程恢复不在四项 OpenCode 原生状态探针的验证范围。公司历史事件的精确归因仍需当时证据。
