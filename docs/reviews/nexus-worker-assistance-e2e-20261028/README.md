# 数字员工处理 Worker 求助：双引擎真实 E2E

日期：2026-10-28（北京时间）。结论：核心私聊闭环在 OpenCode、DSH 均已验证；群聊自动接手、原生审批与人工回传已验证，恢复过程和未通过样本保留。属于独立 Linux 测试栈验收，不是公司生产部署验收。

## 问题原因与此次补修

原实现缺少责任员工读取/回复 Worker 原生求助的完整工具通道，WAITING 文案又引导人进入工作台。本 change 已加入实时 assistance/reply/input、来源及执行轮绑定校验、私聊/群聊唤醒和员工先判断规则。

真实 E2E 又发现并修复三处缺口：

1. 原生问题等待文案仍要求人在 Workspace 处理，改为等待责任数字员工处理。
2. 群任务内部提示使用 user 消息内 HintBlock，官方 AgentScope 2.0.10 `/chat/` 返回 422。改为带 `source=nexus-task` 元数据的内部文本提示；公开群消息仍只展示通知，内部提示不公开。测试替身增加官方 user content 类型约束。
3. OpenCode assistance 原先只读请求，员工看不到执行结果并要求人核查工作区。复用现有分页消息读取、原生事件归一化和脱敏逻辑，返回当前根 Session 的回复、工具结果及退出码摘要；排除用户输入，按 40 个事件分页。员工技能明确摘要边界及在原对话汇报要求。真实任务复验中读到 7 个原生事件，确认 Python 输出与 exit_code=0 后直接在群内汇报。

## 实际环境

- 源码：`v1.4.8`，基线 `5ae0401b700e879a14f8e7ed9e84f80f681e603b` 加本次未提交修改；见 `evidence/source-manifest.json`。
- 两台真实 Worker 在 Lima `nexus-v145-qa` Linux ARM64 / Ubuntu 24.04 / kernel 6.8 / glibc 2.39 中执行，独立仓库与工作区。
- OpenCode 1.18.33（正式锁定版本）；DSH 0.2.1-rc.2、ACP 0.8.1；真实装备健康检查均通过。
- 独立 Nexus API 18841、AgentScope 2.0.10 18800、Task MCP 18802、Library MCP 18812、Redis 18879、Celery/beat。聊天记忆存于官方 AgentScope SQL storage；测试 Nexus 使用 SQLite，未验证公司 MySQL。
- 真实模型为 `deepseek-flash`，员工及两个 Worker 均经过现有 Nexus Gateway 18511；无厂商直连、无跨引擎 fallback。
- 本次是手工装配隔离环境，不算安装器部署验收。历史云机器 SSH host key 与留存记录不匹配，未绕过，改用已有 Linux VM。
- 人工答复、私聊验收及群内答复通过真实网页完成；部分初始测试委托及恢复说明通过正常聊天 API 发给测试员工。没有直接调用 Worker 的 answer/reply 代替员工。

## 场景与结果

| 场景 | 任务 | 真实证据与结论 |
| --- | --- | --- |
| OpenCode 已知答案直接答 | T-20261027-AD1948 | 答案只在员工私聊，不在任务说明；员工调用 MCP 回复原生 question，按次批准验证命令；交付值 `qa-opencode-east-17` 正确，人工在聊天确认，SUCCEEDED |
| DSH 已知答案直接答 | T-20261027-1907AD | Worker 正常回合提问，员工读取真实持久轨迹、MCP input 同一原生 Session 续答；交付值 `qa-dsh-east-17` 正确，聊天确认后 SUCCEEDED |
| OpenCode 未知答案问人 | T-20261027-CC81DD | 员工在原私聊提问；答复前无交付文件；API/MCP/调度进程计划重启后请求与 Session 未变化；网页回复 `qa-opencode-human-west-29` 后员工重查并回传原 question，交付正确，SUCCEEDED |
| DSH 未知答案问人 | T-20261027-316E5A | 原私聊提问、答复前无文件；网页答复 `qa-dsh-human-west-83` 后同一原生 UUID 续答。后遇真实网关 503，明确停止并在原对话授权只读恢复，同一 UUID 重新核验已有交付后确认 SUCCEEDED。计为恢复完成，不计首次无故障通过 |
| DSH 原生一次性审批 | T-20261027-0BD234 | 真实 ACP request_permission → 员工 MCP reply `allow-once` → 原生 approval/decided `allowed-once`，固定无副作用打印命令输出 `ACP_PERMISSION_E2E_OK`，job/settled 退出码 0。群内汇报遇模型错误，恢复后通过群聊确认 SUCCEEDED |
| OpenCode 群内求助与回传 | T-20261027-5456F9 | 初始创建结束后由任务通知自动发起新讨论，员工先按次拒绝未授权 `ls -la`。原轮中断后经明确恢复产生新 Session；随后自动通知引导员工在群内询问未知值，网页 @员工答复 `qa-group-north-61`，员工重查后回传同一 question/request，后续审批自动按次处理，JSON 与原生 Python 输出正确，群聊确认 SUCCEEDED |
| 修复前失败样本 | T-20261027-7E034B | 群提示 422 导致自动接手失败；通过群聊委托员工终止，CANCELED，Worker/DSH owner 已释放。保留失败，不计通过 |

OpenCode 群任务恢复前 Session 为 `ses_e81aca349ffez4HDi5UGxqFOkf`，既有 resume 行为生成新 Session `ses_e81ab9117ffeYOIdjWnsUH2EOt`。不能说整个任务恢复全程同一 Session。业务答复回传时，确实命中后者的原请求 `que_17e547423001uW4tuev6nEWc8M`，未换请求、未广播给其他任务。OpenCode 的 `nexus_task_input` 尝试被明确拒绝（仅 DSH 支持），没有被误报为续答成功。

## 证据与资源收尾

- 官方 AgentScope 数据库提取的员工真实 Task MCP 调用与结果：`evidence/official-agentscope-task-tools.json`。源头不是模型口头宣称。
- 独立 SSH 读取的文件内容与 SHA-256：`auto-independent-deliveries.json`、`human-independent-deliveries.json`、`group-independent-delivery.json`。
- 人回答前等待状态与无交付文件：`human-required-before-answer.json`；原生等待跨计划重启：`planned-restart-pending-questions.json`。
- DSH 原生 approval、tool/result、job/settled 及轨迹 SHA-256：`dsh-native-permission.json`。
- 群聊公开消息与讨论 ID：`group-visible-messages.json`；真实页面：`group-native-results-in-chat.png`、`group-confirmed-in-chat.png`。
- 最终业务状态：`final-business-state.json`。6 条成功、1 条取消、无未结束任务；两位 Worker 均 `current_task_count=0`、IDLE。`WAITING + Session SUCCEEDED` 的验收前快照与最终 Task SUCCEEDED 明确区分。
- Linux 原生 receipt ledger 的所有 DSH binding `owner=null`；两台 Worker 子进程均为 0，见 `linux-resources-before-cleanup.json`。VM 校时导致三个 psutil 创建时间同时偏移 1 秒，清理前额外核对完整命令、独立 cwd、pgid、Worker instance 和内核 start ticks，并在发信号前再次核对；没有按端口或模糊进程名清理。
- 本次两个 Linux Worker、OpenCode Serve 和本机全部独立测试栈均已停止。`linux-cleanup-result.json`、`local-cleanup-result.json` 记录结果；原有五个受保护服务的 PID/create_time/cwd 均未改变。

## 回归与逐场景核验

最终相关回归 **491 passed, 6 skipped**（70.68 秒），覆盖新增 Worker assistance、全部 Nexus/DSH、责任归属、唤醒、原生工作区、Runtime、Runtime context 和数字办公室。日志 `evidence/regression-final.log`。

Ruff、`git diff --check`、OpenSpec `validate nexus-employee-worker-assistance --strict` 通过。无前端源码修改；使用实际静态页面验证，未重复前端构建。

对 change 的全部 Scenario 核查：已知答案、已授权操作、未知业务信息、群任务新讨论、双任务隔离、过期/停止回复、DSH 正常回合续答、OpenCode 输出汇报、执行环境不可达均有真实场景或针对性测试。隔离/过期/停止竞态的穷尽边界仍以针对性测试为证，不能宣称所有对抗情形均做了真实模型 E2E。

## 剩余风险

- 不宣称首轮全部无故障通过：真实网关发生过 `NO_ELIGIBLE_NODE/CIRCUIT_OPEN` 503，DSH 私聊交付后回执变 unknown，群内模型回复也失败过；网关探针恢复 200 后通过原员工对话显式恢复。此次没有修改或绕过网关熔断策略，网关稳定性问题仍在。
- OpenCode 权限拒绝后原轮中断，需恢复；现有 resume 会创建新 Session。此行为及旧的 incomplete 等错误文案不在此次原生求助回复修复范围。原生等待中的问题不通过 pause/intervene 中断。
- 员工模型在长轨迹中的“权限次数/调用对应关系”汇总有不精确措辞；独立验收以原生记录、真实工具 ACK、文件与 Task 状态为准。
- 摘要不能替代完整文件审计；原生消息历史读取有既有分页上限，读取不完整应返回失败。模型判断能力也需持续观察，不能保证任意开放任务都无误判。
- 未验证公司 MySQL 8.0.36、CentOS/glibc 2.28、长时间断网/重启耐久、生产安装器或其他模型。6 项环境跳过和早前扩大回归的既有失败仍保留，未声称全量测试全绿。
- 本次没有提交、推送或替换既有运行栈。上线需部署 API、Task MCP 和后台进程，并向员工 Session 更新任务技能。OpenSpec change 保留供审阅，未改历史 specs、未 sync/archive。


## 发布补充

后续完成中文状态与输入框缺陷修复后，用户授权全部合入并推送 v1.4.8。发布前最终组合回归为 502 项通过、6 项跳过；主站 30 项、聊天岛 17 项通过，类型检查和构建通过。按仓库约定将新静态产物随源码提交，见 `evidence/publish-artifacts.json`。此前“未提交/未推送/构建产物恢复”的说明保留为当时的验证状态；发布不等于公司部署，不改变本报告真实 E2E 的源码快照及风险边界。
