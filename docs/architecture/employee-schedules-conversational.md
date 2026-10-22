# 数字员工对话创建日程：可行性与实现分析

日期：2026-10-23。分支 `codex/employee-schedules`，基线 `a3f429e7c`。状态：**分析完成，未实现**。本文回答“能否让数字员工在对话里自己分析并创建日程，是否应该做成 MCP 工具”，并给出最小可行路线与验证计划。

## 结论

1. **能力已经存在。** AgentScope 2.0.10 自带 `ScheduleCreate`、`ScheduleList`、`ScheduleView`、`ScheduleDelete` 四个 Agent 工具；`ScheduleCreate` 会写入 `source=AGENT`、`source_session_id` 和当前会话的模型快照。Nexus 当前是在 `agent_service.py` 里按名字主动移除它们，不是缺少能力。
2. **直接放开不可行。** Nexus 所有聊天 Session 都跑在平台身份 `nexus` 下，而日程页的命名空间是 `nexus-schedule-<sha256(人类 UID)>`。员工创建的日程会落到 `nexus` 名下：人类页面看不到，`ScheduleList` 还会把平台身份下所有人的日程列给任意员工，破坏现有“按人隔离”。
3. **推荐路线是重定向官方工具，而不是新建 MCP。** 只在“私聊 + 非日程执行会话”保留官方 `Schedule` 工具，组装工具时按当前会话所属人类的命名空间重新构造四个工具；其他会话继续移除。归属查询失败时 fail closed。如果首版只要“能创建”、不需要区分来源，B 方案（Nexus FunctionTool 调官方 REST）改动更小，但会丢掉 `source=AGENT` 和官方列表/删除工具。
4. **MCP 可行，但不是关键。** 模型的分析能力来自对话推理和工具描述，不来自 MCP；MCP 只是把创建动作暴露给模型的另一种传输层，仍要解决“会话属于谁、写进哪个命名空间、如何审计、如何限流”这些同一组问题，并额外引入进程、部署、密钥和按会话安装的生命周期。当前只有 AgentScope 一个运行时，优先 FunctionTool 或官方工具重定向；将来 OpenCode 等其他 runtime 也要创建日程时，再把同一 Nexus 能力包成 MCP 才有明显收益。
5. **还有前置缺口。** 官方触发新建的日程工作区不继承员工的 Skill/MCP/知识库；官方工具默认时区是 UTC；没有官方 `ScheduleUpdate` 工具；配额、递归创建和提示注入都未处理。这些必须在开放前定策略。

## 一、官方 2.0.10 已经提供什么

| 事实 | 依据 | 影响 |
| --- | --- | --- |
| 四个日程工具 | `agentscope/app/_manager/_scheduler/_tools/`：`ScheduleCreate`、`ScheduleList`、`ScheduleView`、`ScheduleDelete`；`SchedulerManager.list_tools()` 统一构造 | 不需要自己实现创建/查询/删除，也不需要改 AgentScope 包 |
| 工具只在该 Session 有模型配置时挂载 | `agentscope/app/_service/_toolkit.py:150-165`，条件是 `session_record.config.chat_model_config is not None` | Nexus 的聊天 Session 都带模型路由，正常情况下会挂载；探针 Session 不会 |
| 创建时归属取工具的 `_user_id` | `_schedule_create.py:147`、`:218`、`:237` | 工具实例的 `_user_id` 是唯一归属开关；可以按会话重定向 |
| Agent 与模型来自当前会话 | `_schedule_create.py:218-235`：`agent_id=self._agent_id`，`chat_model_config=self._chat_model_config` | 员工不能借工具给别的员工或别的模型建日程；模型是当前会话的快照 |
| 工具创建的来源可审计 | `_schedule_create.py:230-231`：`source=ScheduleSource.AGENT`、`source_session_id=_agent_state.session_id` | 页面/日志可以区分“员工创建”和“人类页面创建”，并能追到来源会话 |
| `description` 是触发时唯一上下文 | `ScheduleCreate` 的工具描述明确要求写清目标、产出、约束和路径；触发时只把 `description` 包进 `<scheduled-task>` | 描述为空或过短，到点后的运行就没有任务说明 |
| 默认时区是 UTC | `_schedule_create.py:39-43`，参数默认 `timezone="UTC"` | 员工不主动判断时区时会按 UTC 建日程；页面默认是 `Asia/Shanghai`，两者不一致 |
| 列表/查看/删除都按 `_user_id` 隔离 | `_schedule_list.py`、`_schedule_view.py`、`_schedule_delete.py` 都以 `self._user_id` 读写 storage | 重定向必须四个工具一起做，不能只改创建 |
| 删除会级联清理执行会话 | `_schedule_delete.py` 调用 `SessionService.delete_schedule()`，取消在跑的运行并清理 bus 状态 | 员工如果保留删除工具，就能删人类页面创建的日程；是否开放要单独决定 |
| 触发时按记录的 `user_id` 建新 Session | `_scheduler_manager.py:208-271`：`storage.get_session(record.user_id, ...)`、`SessionConfig(workspace_id, chat_model_config)` | 日程记录属于谁，执行就属于谁；执行工作区只带 workspace 和模型，不带知识库/Skill/MCP |
| 执行 Session 有 `origin.type="schedule"` | `agentscope/app/storage/_model/_session.py:35-41`、`:324` | 可以在工具组装时识别“这是日程执行会话”，防止日程再创建日程 |
| 官方 REST 创建的 `source` 固定为 `USER` | `agentscope/app/_router/_schedule.py:108-125` | 走 Nexus HTTP 适配创建时拿不到 `source=AGENT`，也无法带 `source_session_id` |

## 二、Nexus 当前的阻断点

1. **身份是平台用户，不是人类。** `services/nexus-api/nexus_api/main.py:995-998` 创建 `AgentScopeClient` 时使用 `NEXUS_AGENTSCOPE_USER_ID`（默认 `nexus`）；私聊、邮箱、团队 Session 都通过这个客户端创建和调用。人类只体现在 Nexus 自己的 `t_nexus_conversations.owner_id`，以及日程页专用的 `nexus-schedule-<sha256(uid)>`（`nexus_api/schedules.py:19-20`）。
2. **工具被显式移除。** `services/agent-service/agent_service.py:149-160` 的 `REMOVED_AGENT_TOOLS` 包含四个 `Schedule*`，`:207-220` 在每次组装后 `remove_tool`。这是有意为之，不是遗漏。
3. **页面只读人类命名空间。** `schedules.py:61-69` 把所有日程请求的 `X-User-ID` 换成 `schedule_user_id(uid)`；列表、创建、启停、删除、执行记录都走这个身份。
4. **官方资源访问策略只为人类命名空间共享平台资源。** `services/agent-service/schedule_access.py` 让 `nexus-schedule-*` 只读访问平台 Agent、credential、知识库；不共享 Schedule 和 Session。它解决的是“执行时能拿到平台 Agent/模型”，不解决“员工在聊天里把日程写到哪个用户”。
5. **MCP 是按会话安装的。** `services/nexus-api/nexus_api/task_skill.py:35-45` 已经给出 per-session header 模式（`X-Nexus-Employee-Id`、`X-Nexus-Session-Id`、`X-Nexus-Conversation-Id`、Bearer key），说明“让 MCP 知道当前会话”在工程上可行；但这也说明 MCP 需要额外的安装、鉴权和生命周期管理。
6. **日程执行工作区不继承绑定。** 官方触发只创建 `workspace_id + chat_model_config`，没有 `knowledge_config`，也没有把私聊工作区的 Skill/MCP 复制过去；这与现有交接材料中的已知缺口一致。

## 三、为什么不能只删白名单

把四个名字从 `REMOVED_AGENT_TOOLS` 删掉，会出现以下后果：

| 场景 | 直接放开的实际行为 |
| --- | --- |
| 员工在私聊里创建日程 | 记录 `user_id="nexus"`，不是当前人类；`nexus-schedule-<hash>` 的页面查不到 |
| 人类在页面查看 | 看不到员工创建的日程；无法暂停、编辑、删除，也无法查看执行记录 |
| 另一个人类问同一员工“列一下日程” | `ScheduleList` 读的是 `nexus` 名下全部记录，会把其他人类/其他员工创建的日程名称、描述、Agent ID 一起返回 |
| 员工删除日程 | `ScheduleDelete` 也按 `nexus` 删除；可能删到别人的记录 |
| 员工在日程执行会话里再创建日程 | 执行 Session 同样有模型配置，官方仍会挂载 `ScheduleCreate`；可以递归创建，形成链式/指数级日程 |
| 归属与审计 | 官方记录没有人类归属，`source_session_id` 是 `nexus` 下的会话；Nexus 页面无法把它映射回某个人的会话 |
| 时区 | 模型不主动判断时按 UTC 创建；页面默认 `Asia/Shanghai`，9 点会变成北京时间 17 点 |
| 一次性提醒 | 官方工具支持 `started_at`/`ended_at` 和一次性 cron；Nexus 页面只展示周期 cron，用户会看到一个“看起来每周执行”的一次性任务 |

归属验证脚本已经复现了最关键的两条：官方工具可以写入人类命名空间并保留 `source=AGENT`/`source_session_id`/模型快照；而沿用平台身份创建时，人类命名空间看不到，平台列表能看到。见 [conversational-schedule-spike.json](../reports/evidence/employee-schedules-20261023/conversational-schedule-spike.json)（21 项检查全部通过；边界见该文件的 `limitations`）。

## 四、方案对比

| 方案 | 归属 | 页面可见 | `source=AGENT` | 列表/查看/删除 | 策略与配额 | 递归保护 | 改动/运维 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **A. 重定向官方工具**（推荐） | 组装时按会话解析人类命名空间 | 是 | 是，且带 `source_session_id` | 官方四个工具可用（首版可先不开放删除） | 需要 Nexus 内部查询/策略门 | 组装时看 `session_record.origin.type`，天然可做 | 只改 `agent_service.py` + Nexus 一个内部接口；无新进程 |
| B. Nexus FunctionTool → 官方 REST | Nexus 解析会话后调用 | 是 | 否，REST 固定 `USER` | 需要另做列表/删除端点 | 集中在 Nexus，容易做 | 工具可拿到 session_id，但需额外判断会话来源 | 新工具 + 端点；无新进程 |
| C. Nexus FunctionTool → Agent Service 内部写接口 | Nexus 解析后调用内部写接口 | 是 | 是 | 需要另做 | 集中在 Nexus | 内部接口可校验会话来源 | 新工具 + Nexus 端点 + Agent Service 写接口；写接口敏感，重复一部分官方创建逻辑 |
| D. MCP Server | MCP 按会话 header 解析 | 是 | 否（除非再走 C 的内部接口） | 取决于 MCP 工具设计 | 可在 MCP/Nexus 做 | 需要额外查询会话来源 | 新进程、部署、密钥、按会话安装、健康检查；最重 |

**推荐 A**，原因：

- 直接复用官方 `ScheduleCreate` 的校验、`source=AGENT`、`source_session_id`、模型快照和删除级联等官方生命周期，不复制状态机。
- 执行仍由官方 `SchedulerManager` 负责，符合“Nexus 不新增日程表/计时器”的边界。
- 递归保护在工具组装阶段就能做：`session_record.origin.type == "schedule"` 时移除工具。
- 不需要新增 MCP 进程，也不需要给会话加 `bypass` 或额外的 MCP 权限前缀。
- 重定向有两种实现：用公开的 `SchedulerManager.list_tools()` 按人类命名空间重建工具（推荐），或改官方工具实例的 `_user_id`（最小改动，但碰 2.0.10 私有字段，升级必须重验）。仓库已有改私有字段的先例：`retarget_agent_invites()` 会改 `AgentInvite._storage`（`agent_service.py:195-204`）。
- 如果产品接受 `source=USER`，B 是更小的 fallback：用一个 FunctionTool 调 Nexus 内部端点，由 Nexus 复用现有 `create` 逻辑走官方 REST。代价是页面无法区分“员工创建/手动创建”，也没有官方 `ScheduleList`/`ScheduleDelete` 工具可复用。

## 五、推荐路线 A 的落地设计

### 5.1 会话归属查询

Nexus 增加一个只给 Agent Service 用的内部接口，例如：

```text
GET /api/v1/internal/schedule-context?session_id=...&agent_id=...
→ 200 {"schedule_user_id": "nexus-schedule-...", "employee_id": "...", "can_create": true, "reason": ""}
→ 404 非私聊会话（团队/邮箱/渠道）
→ 409 agent_id 与会话员工不一致
```

查询条件：

```sql
SELECT c.owner_id, c.employee_id, e.enabled, e.agentscope_agent_id
FROM t_nexus_conversations c
JOIN t_nexus_employees e ON e.id = c.employee_id
WHERE c.agentscope_session_id = :session_id
  AND c.kind = 'private'
```

- 只返回 `schedule_user_id`，不把原始 UID 或哈希规则交给模型。
- `agent_id` 必须等于会话员工的 `agentscope_agent_id`，防止一个会话替另一个员工创建。
- 员工禁用、没有模型路由、超配额时返回 `can_create=false` 和原因；Agent Service 据此移除工具或让创建失败。
- 接口需要内部鉴权，不能沿用当前 `/api/v1/internal/mailbox` 的无鉴权模式，也不能只靠“监听 127.0.0.1”；可加 `X-Nexus-Internal-Key`。

### 5.2 工具组装

在 `without_team_and_task_tools()` 里把“移除”改成“条件保留 + 重定向”。现有 `REMOVED_AGENT_TOOLS` 同时包含任务/团队和四个日程工具，需要拆成两个常量，避免“先删后加”。

优先用官方公开的 `SchedulerManager.list_tools()` 按人类命名空间重新构造四个工具，替换原来的 `schedule_tools` 组；这比修改工具实例的 `_user_id` 更稳：

```python
SCHEDULE_TOOL_NAMES = (
    "ScheduleCreate", "ScheduleList", "ScheduleView", "ScheduleDelete",
)
TASK_AND_TEAM_TOOLS = tuple(
    name for name in REMOVED_AGENT_TOOLS if name not in SCHEDULE_TOOL_NAMES
)

async def assembled(**kwargs):
    toolkit = await get_toolkit(**kwargs)
    await toolkit.remove_tool(list(TASK_AND_TEAM_TOOLS))
    session = kwargs.get("session_record")
    context = await schedule_context(session) if session else None
    usable = (
        context is not None
        and context["can_create"]
        and session is not None
        and session.origin.type != "schedule"
        and session.config.chat_model_config is not None
    )
    if not usable:
        await toolkit.remove_tool(list(SCHEDULE_TOOL_NAMES))
        return toolkit

    tools = await kwargs["scheduler_manager"].list_tools(
        user_id=context["schedule_user_id"],
        agent_id=kwargs["agent_record"].id,
        chat_model_config=session.config.chat_model_config,
    )
    for group in toolkit.tool_groups:
        if group.name == "schedule_tools":
            group.tools = tools
    return toolkit
```

要点：

- **推荐重建，而不是改 `_user_id`**：`list_tools()` 是公开方法，直接传人类命名空间；不碰私有字段，未来官方新增日程工具也会自动带上。若只想最小改动，也可以把四个实例的 `_user_id` 改成人类命名空间，但它依赖 2.0.10 私有字段，升级必须重验。
- **只对私聊会话**保留；团队、邮箱、渠道、日程执行会话全部移除。`session.origin.type == "schedule"` 是递归保护的硬条件。
- 查询失败、超时、返回 404/409 一律移除（fail closed），保持当前行为，不让员工在没有归属的情况下创建。`can_create=false` 但归属明确时（例如配额已满），可以只移除 `ScheduleCreate`，保留 `ScheduleList`/`ScheduleView`，让员工还能帮人类查已有日程。
- 四个工具一起处理；只改 `ScheduleCreate` 会出现“能建不能看/不能删”或反向的越权。
- 首次可以先只保留 `ScheduleCreate` + `ScheduleList` + `ScheduleView`，从重建结果里过滤掉 `ScheduleDelete`；删除仍走页面。等确认交互后再开放删除。
- 每次组装都查一次会增加一次本地 HTTP 调用；可以按 `session_id` 做短 TTL 缓存，但员工禁用/配额变化要能及时生效，TTL 不宜过长。
- 实现细节：`agent_service.py` 现在先构造 `peer_toolkit`、后读取 `nexus_api`；加 lookup 时要把 URL 读取提前，或用闭包延迟到首次调用，避免 `NameError`。

### 5.3 创建前的策略门

官方 `ScheduleCreate` 本身只校验 cron/timezone，不校验员工状态、配额、描述质量。建议在工具 `__call__` 外包一层，或在 Nexus 内部接口里做：

- `name`、`description` 非空；`description` 至少能独立说明到点后要做什么。
- 五字段 cron；拒绝 6 字段（带秒）和明显过密的表达式。
- 时区默认 `Asia/Shanghai`；模型没有显式给时区时不要落到 UTC；人类明确说其他时区时按人类说的。
- 每个人类/员工的数量上限与最小执行间隔（例如每人 20 条、最短 5 分钟一次），避免对话里连续创建消耗模型额度。
- 同一个人、同一员工、同名同 cron 的重复创建可以返回已有记录，避免模型重试造成重复日程。
- 员工禁用、路由被删、会话不属于当前员工时拒绝。

### 5.4 页面呈现

- 列表增加“来源”列或标签：`data.source == "AGENT"` 显示“员工创建”，`USER` 显示“手动创建”。
- 可选：用 `source_session_id` 反查 `t_nexus_conversations`，在详情里显示“来源对话”；只对当前 owner 可见。
- 员工创建的日程默认 `enabled=False` 时，页面显示“待启用”，不要只显示“已暂停”；人类确认内容后再启用。
- 如果支持一次性 `started_at`/`ended_at`，页面需要展示时间窗口，不能只显示 cron。
- 执行记录和执行内容沿用现有读取逻辑；归属已经是人类命名空间，不需要改。

## 六、MCP 专项回答

**技术上可以。** 仓库里已有 Task MCP / Library MCP 的服务端实现（`services/nexus-api/app/task_mcp.py`、`app/mcp_server.py`），但它们是独立基础设施进程，不在 `nexus_api` 进程内；Nexus 侧只有 MCP 客户端和 MCP 库管理（`resources.py`、`task_skill.py`）。做日程 MCP 可以复用这套 MCP 服务端技术，但仍要新增或扩展一个 MCP 服务进程、端口和部署，成本比一个 FunctionTool 高。具体做法是：

1. 写一个 MCP server（放 Nexus 或 Agent Service 都可以），暴露 `schedule_create`、`schedule_list`、`schedule_cancel` 等工具。
2. 按 `task_skill.py` 的模式，在私聊 Session 创建/同步时用 `add_workspace_mcp()` 安装，headers 带 `X-Nexus-Employee-Id`、`X-Nexus-Session-Id`、`X-Nexus-Conversation-Id` 和内部 Bearer key。
3. MCP server 用 session id 反查 Nexus 的 `t_nexus_conversations.owner_id`，再调用 Nexus 的创建逻辑。
4. 工具名会是 `mcp__nexus-schedule__schedule_create`；要么像 `allow_nexus_task_mcp()` 一样给这个前缀显式 ALLOW，要么让会话用 `bypass` 权限模式。

如果 MCP server 直接放在 Agent Service 进程内，它可以访问官方 storage 和 scheduler，从而保留 `source=AGENT`；但它仍要按会话安装、鉴权、判断 `origin.type`，复杂度不会低于方案 A。

**但它不解决关键问题：**

- **归属**：MCP server 仍然要解析“这个 session 属于哪个 人类”，和方案 A 的内部查询是同一件事。
- **source**：如果 MCP server 调官方 REST，记录仍是 `source=USER`、没有 `source_session_id`；要保留 `AGENT` 就得再开一个 Agent Service 内部写接口（方案 C），MCP 只是又包了一层。
- **递归**：MCP server 不在 Agent 进程内，拿不到 `session_record.origin`，要额外查 AgentScope 或让 Nexus 记录日程执行会话，才能阻止日程再建日程。
- **权限面**：MCP 需要按会话安装、更新、卸载；会话已有 MCP 时权限模式要切 `bypass`（`resources.py:1295-1301`），比 FunctionTool 的显式 ALLOW 更难收紧。
- **运维**：多一个进程、端口、密钥、健康检查、部署模板和故障模式；当前只有一个运行时，收益不足以抵消成本。

**什么时候值得做 MCP：** OpenCode Worker、外部 IDE、其他 Agent runtime 也需要创建/查询 Nexus 日程；或者日程能力需要独立扩缩容、独立审计；或者要把工具放进用户可配置的 MCP 库。到那时再把“会话归属 + 策略门”包成 MCP，内部仍复用同一套 Nexus 逻辑。

## 七、执行上下文继承（开放前必须决定）

官方触发路径在 `_scheduler_manager.py:208-271` 只创建 `workspace_id` 和 `chat_model_config`：

- 不复制私聊工作区的 Skill；
- 不安装员工的 MCP；
- 不设置 `knowledge_config`；
- 不复制工作区文件。

因此，员工在对话里创建的日程到点后，只有基础 Agent 指令、官方默认工作区工具和网关模型；如果任务依赖员工的 Skill、MCP 或知识库，会直接失败或给出不完整结果。这是“对话创建”从演示走向可用的最大前置项。

还有一条容易漏掉的执行上下文：日程触发创建的 Session 不在 `t_nexus_conversations`、`t_nexus_channel_members` 或员工邮箱映射里。`contact_employee` 通过发送者 `session_id` 调 `/api/v1/peers/resolve`，日程 Session 三条映射都查不到，工具会返回“没有找到可联系的员工”；`contact_human` 需要人类会话的 `conversation_key`，日程 Session 没有这个映射，模型只能猜，消息不保证能到达正确的人类会话。现有交接只验证了日程执行后的文本回复落盘，没有验证执行中的跨员工/人工联系。若员工自建日程要求到点后联系同事或人类，需要一并补映射或明确禁用这些工具。

可选处理：

1. **先限制范围**：只允许创建不依赖额外绑定的日程（基础对话、读写工作区、简单整理），页面/对话里明确说明。
2. **在 Agent Service 里做触发前装配**：识别 `origin.type=="schedule"`，在运行前把员工的 Skill/MCP/知识库装进新 workspace。官方 `extra_agent_tools` 和 `get_toolkit` 都在 workspace 解析之后，是否来得及影响当前运行的 toolkit 需要单独验证；不要假设“加个 factory 就能继承”。
3. **改官方调度路径**：在 AgentScope 上游增加“从来源会话复制绑定”的能力，或由 Nexus 自己维护日程→绑定的映射并在触发后补装。这会引入新的状态和时序，必须单独设计。

在第 1 或第 2 项完成前，不建议把“员工自建日程”作为通用能力对外承诺。

## 八、产品与安全护栏

| 决策点 | 建议 |
| --- | --- |
| 日程归属 | 归属于发起对话的人类，而不是员工或平台；这样页面、暂停、删除、执行记录都沿用现有隔离 |
| 可用会话 | 仅私聊；团队讨论、员工邮箱、外部渠道不提供。团队里没有唯一的人类归属 |
| 是否自动启用 | 员工创建默认 `enabled=False`，人类在页面确认内容后启用；或在对话里明确说“创建并启用”才允许直接启用 |
| 删除 | 首版不开放 `ScheduleDelete`；删除仍走页面，避免误删和丢失执行历史 |
| 编辑 | 官方没有 `ScheduleUpdate` 工具；需要编辑时引导人类到页面，或后续单独做更新工具 |
| 一次性提醒 | 明确产品语义：只支持周期 cron，或同时展示 `started_at`/`ended_at`。不要把一次性任务伪装成周期任务 |
| 提示注入 | 员工可能读到恶意文档后自主创建定时任务；默认不启用、人类确认、配额和最小间隔是主要缓解。不要只靠模型“自己判断” |
| 审计 | 依赖官方 `source=AGENT` + `source_session_id`；页面显示来源；Nexus 日志记录创建/拒绝原因 |
| 员工禁用/路由变更 | 创建前校验员工状态和路由；已有日程仍按创建时快照运行，页面继续提示“路由变化需重新创建” |

## 九、验证计划

### 已有证据

- 归属与重定向 spike：21 项检查全部通过，覆盖人类命名空间写入、`source=AGENT`、`source_session_id`、模型快照、时区、列表隔离、平台身份直接创建的泄漏、官方 `SchedulerManager.list_tools()` 按人类命名空间重建四个工具，以及私聊/团队/邮箱/日程执行/查询失败五种工具决策。spike 按四个工具验证机制，不代表首版一定开放删除。
- 现有交接材料的 133+13+30 测试、页面操作、官方触发至回复落盘仍有效；本次分析没有改动产品代码。

### 实现后要补的验证

1. **单元**：`without_team_and_task_tools` 在私聊/团队/邮箱/日程执行/查询失败五种情况下保留或移除四个工具，并断言重建调用收到的人类命名空间（或 `_user_id`）；任务/团队工具仍被移除，`contact_*` 仍保留。
2. **Nexus 内部接口**：私聊返回命名空间；团队/邮箱 404；`agent_id` 不匹配 409；员工禁用 `can_create=false`；未带内部 key 拒绝。
3. **集成**：官方 SQL（SQLite 开发库 + MySQL 8.0.36 各一次）创建后记录归属正确、`source=AGENT`、`source_session_id` 正确；另一个人列表看不到。
4. **页面/API**：员工创建后 `/api/v1/schedules` 对当前人类返回该条，`source` 为 `AGENT`；另一个人返回 0；暂停/启用/删除仍可用。
5. **端到端**：真实模型工具调用（本地 OpenAI 协议桩）在私聊里说“每周一 9 点…”，员工调用 `ScheduleCreate`，页面出现记录；用官方触发函数跑一次，回复落到执行会话；日程执行会话里再要求创建日程应被拒绝（无工具）。
6. **回归**：Nexus 133+、Agent Service 13+、前端 30+、type-check/build、Ruff、OpenSpec strict。
7. **未验证边界**：真实供应商、生产 MySQL 8.0.36、生产 Nginx、墙钟周期、多副本单 scheduler owner、提示注入演练。

## 十、建议的 OpenSpec 变更

这是行为变更，且与现有 change 的 Non-Goals（“本次不重新开放员工自主安排”）冲突，**不能直接改现有 change**。建议新建一个 change，例如 `nexus-employee-schedules-dialogue`：

- Requirement：员工在私聊中可通过工具创建/查看自己的日程，归属当前人类；非私聊和日程执行会话不提供。
- Requirement：创建保留官方 `source=AGENT`、`source_session_id` 和模型快照；员工禁用、配额、时区、内容校验失败时明确拒绝。
- Requirement：页面显示来源；不新增日程表或计时器；执行记录仍按人隔离。
- 非目标（首版）：MCP server、对话编辑、对话删除、跨人安排、一次性提醒、Skill/MCP/知识库自动继承。
- 任务：内部归属接口 → 工具条件保留/重定向 → 策略门 → 页面来源 → 测试/端到端/文档。

## 十一、开放问题

1. 员工创建的日程，人类是否必须在页面上再确认一次才启用？还是对话里明确说“创建并启用”就可以？
2. 首版是否开放 `ScheduleDelete`？如果不开放，员工误建后只能让人类去页面删。
3. 一次性提醒要不要支持？支持的话页面必须展示 `started_at`/`ended_at`，否则语义会误导。
4. 每个人类/员工的日程配额和最小间隔是多少？谁来配置？
5. 日程执行工作区的 Skill/MCP/知识库继承，放在本次 change 还是单独一个 change？
6. 如果员工在团队讨论里被要求“安排一下”，是否允许归属到发起人？这需要把消息发送者身份传进工具上下文，当前没有。

---

**一句话回答**：可以让数字员工在对话里自己创建日程，官方工具已经具备；但不应该直接放开，也不应该先做 MCP。先把“私聊会话 → 当前人类日程命名空间”的归属路由和“默认不启用、配额、递归保护”的护栏做出来，官方 `ScheduleCreate` 就能用；MCP 留到需要跨 runtime 复用时再做。
