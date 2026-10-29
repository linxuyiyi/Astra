# Worker 状态与空闲可用性探测修复

开始日期：2026-10-29，完成本地验证：2026-10-30（北京时间）。工作分支：`v1.4.9`。按本次明确要求直接修复，不创建或更新 OpenSpec change。

## 问题原因

原审查确认的 7 项问题见 [审查记录](README.md)。本次继续完成已有未提交修复，并补齐 MySQL 当前读、事件外键锁和探测占用边界。

健康核查还将模型网关 `/models` 请求结果纳入整个 Worker 的健康判定。OpenCode 和 DSH 都存在该依赖；网关请求延迟或失败会拖慢核查并影响可用性。代码中的 `/models` 是路由目录查询，不发送推理请求，不能据此断言公司上游模型限流已经造成了这次故障。

## 修改内容

- **环境锁与 MySQL 当前读**：去掉 SQL 中整份 JSON 与字符串的相等比较，按环境 ID 取得写锁，再通过 locking refresh 读取当前配置。普通 refresh 在 REPEATABLE READ 下仍可读到旧快照，新增真实 MySQL 用例覆盖该边界。健康观测落库保留锁内配置与 epoch 核对，防止晚到结果覆盖新事实。
- **调度事务**：候选列表不加批量行锁；逐个锁定任务和最终候选 Worker，领取事务中不读取远程 Git HEAD。复用 start 已有的工作区准备与 HEAD 读取。维护中的 Worker 在候选查询中排除，避免它阻塞另一个可用 Worker。
- **锁顺序**：涉及相关写入时采用 Employee → Environment → MaintenanceOperation。维护结束写 Event 时，员工外键也会取锁，已提前取得该员工锁；健康观察不在取得环境锁后再获取员工写锁。等待锁后重新核对租约期限。
- **Session 结果**：读取历史后重新核实原生状态；缺少输出、缺少 anchor 或未完成 assistant 只形成 UNKNOWN，不推断执行失败或中断。移除失败/中断缓存阻止读取后续历史的快路径。明确停止事实在历史仍不完整时保留，真实完成结果可以纠正原投影。
- **超时停止**：abort 必须被接受，并在请求后重新枚举根 Session 及子 Session，确认 idle 或 Session 已不存在，才写入 timeout 中断。未知、断线或仍 busy 不算停止；在途提交和取消中的任务不触发该超时停止分支。
- **控制状态**：身份状态写入使用当前 locking read，保护已提交的 PAUSED/ERROR。旧探测时间不会把仍有任务或未结束 Session 的员工标成离线。
- **模型核查**：OpenCode、DSH 健康核查不再请求模型网关 `/models`。保留生效模型、provider、地址、调用凭据是否配置、已启用路由的核对；不再声称完成在线认证验证。模型网关运行健康继续由网关自身观测。
- **仅空闲探测**：复用现有任务占用和未结束 Session 谓词。有占用时，后台与手动核查均在远端 I/O 前跳过，保持原环境状态与检查时间；手动维护记录明确保存 skipped 原因，不把跳过当作健康成功。若后台探测发出后任务才启动，丢弃晚到结果。员工心跳同步只投影已有占用和健康事实，不在忙碌期间额外启动或探测 OpenCode。长任务不因暂停探测导致旧观测过期而被降为 UNKNOWN；回到空闲后仍要求重新核查。Session 对账与 Worker 连接心跳保持独立。

不新增 Run 或其他业务状态机，不修改数据库 schema。工作区另有并行修改的 MCP、前端等文件，本记录不将其列为本次修复内容。

## 验证结果

使用仓库 `.venv`。补装已在 `pyproject.toml` 声明的 `agent-client-protocol==0.8.1` 后执行 ACP 测试；未修改依赖声明。

- Session、Runtime 收敛、维护、装备核查、Worker 本地检查和 DSH driver 定向回归：**321 passed**。
- 最终 Runtime 收敛、占用、分配/工作区基准提交和 ACP 基础设施补充回归：**67 passed**；其中 Runtime 收敛单独复核 **19 passed**。这些批次有重叠，不相加为独立用例总数。
- 独立 MySQL **8.0.36 / REPEATABLE READ**：**16 passed**。每例创建并迁移专用随机库，测试后仅删除该用例自己的数据库；未使用本机 3306 的业务实例。覆盖 JSON guard、旧事务快照、晚到配置/epoch、其他 Worker 心跳与任务领取、同一 Worker 并发抢占、维护与健康观察锁顺序、Event 外键锁、暂停/异常状态保护，以及忙碌跳过、忙碌后的晚到结果和手动核查跳过。
- `ruff check app nexus_api tests`、`git diff --check`：通过。

本机 `ops/env/.env` 将 OpenCode 固定为 1.18.33，而仓库默认与多项测试桩使用 1.18.35。扩大回归使用仓库默认测试版本，未修改本机配置或正式版本要求：

```bash
ASTRA_OPENCODE_SERVER_VERSION=1.18.35 NEXUS_REMOTE_OPENCODE_VERSION=1.18.35 \
  .venv/bin/python -m pytest -q --disable-warnings tests/platform tests/nexus tests/dsh \
  --deselect tests/platform/test_deployment_gateway.py::test_existing_wiki_never_invents_credentials
```

最终扩大回归：**1502 passed、33 skipped、1 deselected，208.39 秒**，见 [输出](evidence/final-regression.txt)。最后补充的“忙碌期间观测不老化、空闲后恢复核查”投影边界及其维护、占用、Runtime 回归另跑 **108 passed**；最终 MySQL 再跑 **16 passed**，见 [输出](evidence/mysql-final-regression.txt)。

唯一排除项 `test_existing_wiki_never_invents_credentials` 已在此前扩大回归中实际失败：安装器测试读取真实 checkout 的 `ops/env/.env`，本机已有 wiki token/project_id，破坏了该用例“没有凭据应拒绝”的前置条件。该用例未修复、未计为通过；没有为本次 Runtime 修复改安装器。其余扩大回归暴露的旧断言与测试桩（分配阶段读取 HEAD、以缓存失败代替原生历史、忙碌 Worker 仍发健康探测）已按新的事实规则修正并定向复测通过。跳过项目也未计为通过。

## 合入 v1.4.9 前复验（2026-10-30）

基于最新远端 `origin/v1.4.9` 的 `202c678387297b4d37d19f0a4fff2d746baccb06` 建立隔离工作区，只纳入本次 Worker/Runtime 修复，保留远端已有的头像与原 Session 续答改动。主工作区的并行未提交内容不纳入本提交。

隔离工作区没有主工作区的 `ops/env/.env`，使用仓库默认测试设置。执行完整 `tests/platform tests/nexus tests/dsh`：**1512 passed、33 skipped、无失败、无排除，204.52 秒**，见 [合入前完整回归](evidence/publish-regression.txt)。此前因本地已有 wiki 凭据而失败的安装器测试也已通过，无需改动安装器或该测试。Ruff 与 `git diff --check` 均通过。

同一隔离工作区基于最新 migration 再跑真实 MySQL 8.0.36 回归：**16 passed，19.36 秒**，见 [合入前 MySQL 回归](evidence/publish-mysql-regression.txt)。专用测试实例已关闭，未操作本机 3306 业务实例。

## 剩余风险

本次发布代码，不执行部署。公司实际事故的准确归因、公司 MySQL/模型限流与真实 Worker 长任务未做现场验收。Worker 本地 inspection 与 DSH driver 的修改需要同步更新 Worker 包才会在已有执行机生效；只更新控制端不能声称现场已修复。

空闲观测与任务启动之间允许并发：已经发出的只读探测不能撤回，但任务开始后其结果不会回写环境。缺少停止或完成证据时保留未知与占用，需后续原生证据或人工处置，不能靠探测超时自动释放 Worker。
