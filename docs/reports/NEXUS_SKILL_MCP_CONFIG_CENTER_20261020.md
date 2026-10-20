# Nexus 配置中心：Skill / MCP 管理与数字员工绑定

日期：2026-10-20
分支：`codex/nexus-v1.4.7`
AgentScope：`2.0.10`

## 1. 实现范围

- 配置中心新增「技能管理」「MCP 管理」两个页面，沿用 Element Plus 列表、搜索、状态、分页与 `el-drawer + el-form` 规范。
- Skill：在线编辑 `SKILL.md`、上传完整目录、编辑资源文件、查看详情、启用/停用、删除保护、版本列表与恢复。
- MCP：新增自定义 HTTP MCP、编辑 URL/传输方式/请求头/鉴权/超时、连接测试与 Tool 列表、启用/停用、删除保护。
- 员工：技能与 MCP 分别多选绑定；保存后同步邮箱、私聊、团队会话；页面展示实际同步状态。
- 资源来源：Nexus 自建（持久化、版本、加密凭据）与 AgentScope 已安装资源（按原生 id 只读引用，不复制正文）。
- AgentScope 同步：统一收敛入口，逐资源 + 逐会话记录 `applied/pending/error`；运行中会话不拆改，失败保留配置并支持重试。

## 2. 关键设计

### 资源

- `nexus_skills` / `nexus_skill_versions`：自建技能元数据与版本；文件在 `data/skills/<skill_id>/<version>/`。
- `nexus_mcps`：自建 MCP 配置；`token_ciphertext` 使用 Fernet 加密，密钥优先读 `NEXUS_MCP_MASTER_KEY`，未配置时在数据目录生成权限 `0600` 的 `.mcp_master_key`。
- `resource_sync`：`resource_type + resource_id + employee_id + session_id` 为唯一键，保存 revision、状态和异常。
- `employees.mcp_json`：员工 MCP 绑定，与 `skills_json`、`tools_json` 分开。

### 绑定值

- `task-management`：内置任务技能，仍由 `task_skill.py` 挂载。
- `nsk_*`：Nexus 自建技能，运行名取 `SKILL.md` frontmatter 的 `name`。
- `nmp_*`：Nexus 自建 MCP，运行名 `nexus_<slug>_<id后缀>`，满足 AgentScope `[a-zA-Z0-9_-]` 限制。
- 其他 id：AgentScope 技能库 / MCP 库原生 id。

### 同步

- `sync_employee_resources` 是唯一入口：员工保存、会话创建、发消息前修复、手动重试都调用它。
- 自建 Skill 按 revision 判断是否需要先移除再上传；AgentScope 技能 / MCP 通过 `from-library` 幂等装入。
- 解绑或停用会从所有相关会话移除对应资源；重新启用再装回。
- 同步前查询 `/sessions/{id}/status`，`running` 时只记录 pending，不中途拆改工作区。
- AgentScope 不可用时保留 Nexus 配置，逐会话写 pending，页面可重试。
- MCP Tool 在 `dont_ask` 下会被 AgentScope 转成 DENY；绑定 MCP 的会话使用官方 `bypass` 权限模式，否则员工看得见 Tool 但调不到。

## 3. 主要文件

后端：

- `services/nexus-api/nexus_api/resources.py`（新增：资源存储、校验、目录、同步、MCP 测试）
- `services/nexus-api/nexus_api/main.py`（配置中心 API、员工绑定、会话同步接入）
- `services/nexus-api/nexus_api/agentscope.py`（原生 Skill/MCP 库、workspace 上传、from-library、状态查询）
- `services/nexus-api/nexus_api/db.py`（表结构与 `mcp_json` 迁移）
- `services/nexus-api/nexus_api/capability.py`（自建 Skill/MCP 的实际装载核对）
- `services/nexus-api/requirements.txt`（`cryptography`、`PyYAML`）
- `services/nexus-api/tests/test_resources.py`（新增 10 个集成测试）

前端：

- `web/nexus/src/views/settings/SkillResourcesView.vue`（新增）
- `web/nexus/src/views/settings/McpResourcesView.vue`（新增）
- `web/nexus/src/views/SettingsView.vue`（导航）
- `web/nexus/src/router.ts`（路由）
- `web/nexus/src/components/EmployeeDrawer.vue`（Skill/MCP 绑定）
- `web/nexus/src/views/EmployeesView.vue`（MCP 列、同步状态）
- `web/nexus/src/views/DigitalEmployeeChatView.vue`（员工资料展示 MCP）
- `web/nexus/src/api.ts`、`web/nexus/src/types.ts`

## 4. 验证结果

- 后端：`PYTHONPATH=services/nexus-api services/nexus-api/.venv/bin/pytest services/nexus-api/tests` → `113 passed, 4 skipped`。
- 后端 lint：`ruff check nexus_api tests` → 通过。
- 前端：`npm run type-check` → 通过；`npm run build` → 通过；`npm test` → `17 passed`。
- 真实 E2E（证据：`docs/reports/evidence/nexus-skill-mcp-e2e-20261020/e2e.json`）：
  1. Nexus 创建技能 `NexusE2E技能` 与员工 `E2E验收员工`，邮箱工作区出现该技能。
  2. Nexus 创建 MCP `E2E任务MCP` 指向真实 Task MCP `http://127.0.0.1:8202/mcp`，连接测试返回 15 个 Tool。
  3. 员工绑定 Skill + MCP 后，邮箱、两条私聊、重启后新建私聊均完成装载。
  4. 真实模型会话调用 `mcp__nexus_e2e_mcp_baf3e2dc__nexus_task_list`，工具返回 `{"ok": true, "tasks": []}`，员工回复 `0`。
  5. 更新技能后三个会话的 `SKILL.md` 均变为新内容；停用 MCP 后工作区为空，重新启用后恢复；解绑后移除、重新绑定后装回。
  6. 删除被员工引用的技能 / MCP 均返回 409；服务重启后技能文件、版本和 MCP 密文仍在，连接测试仍成功。
  7. 配置列表和详情只返回 `has_token`，不返回 Token 明文。

## 5. 未解决 / 风险

- 前端已通过类型检查、构建和单元测试；受本机沙箱限制，Chrome headless 无法启动，未做浏览器点击/截图级验收。
- 绑定 MCP 的会话使用 `bypass` 权限模式，MCP Tool 可调用，但该模式同时放宽了该会话内其他工具的安全确认。当前没有 AgentScope 官方的 HTTP 规则接口，无法只对 MCP Tool 放行。
- 首次连接测试会在 AgentScope 创建并保留一个 `Nexus 资源校验` Agent 与 Session，仅用于 workspace 探测。
- AgentScope 已安装 Skill 的正文只能查看，不能在 Nexus 编辑（原生接口没有更新入口）；启用/停用/删除也由 AgentScope 管理。
- 生产部署建议显式配置 `NEXUS_MCP_MASTER_KEY`，不要依赖数据目录自动生成的密钥文件。
- 本次 E2E 在本地开发数据中保留了 `NexusE2E技能`、`E2E任务MCP` 和 `E2E验收员工`，便于复查；如需清理，先解绑再删除。
