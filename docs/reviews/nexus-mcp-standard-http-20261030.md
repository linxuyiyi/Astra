# Nexus 内置 MCP 标准 HTTP 整改与验证

日期：2026-10-30（Asia/Shanghai）。分支 `v1.4.9`，工作区含未提交改动，本轮未提交、未推送。

## 原因

- Task MCP 的中间件在 `initialize`/`tools/list` 阶段调用 `authorize_task_source`；群会话没有运行中的讨论或租约过期时，AgentScope `GET /workspace/mcp` 的工具发现被 403，员工配置页随同步失败。
- Task/Schedule/Library MCP 依赖安装器生成的共享 Bearer key；Task 技能上传失败还会挡住同一函数里的 MCP 注册，而失败只记 warning。
- 部署验收只检查 `/health`（Library 只做 initialize），没有实际执行三个 MCP 的工具发现，也没有通过 AgentScope 工作区验证注册和连接。

## 修改

- Task MCP 中间件只把员工、会话、讨论和日程来源写入上下文；`initialize`/`tools/list` 不查业务状态。工具调用继续执行原 `authorize_task_source`，缺少上下文返回 `ok=false` 和明确原因。
- Schedule MCP 移除共享 key；缺少会话/员工时工具调用返回业务错误，连接和发现不拒绝。
- Library MCP 移除静态 Bearer verifier；保留 DNS rebinding 保护、Worker 网段入口和工具内 `runtime_context` 校验。
- 删除三个 MCP 共享 key 的设置、注入、安装器生成、Worker OpenCode 配置注入和文档说明；升级时静默清理旧环境项。
- Task 技能上传与 MCP 注册分开；同步通过 AgentScope `/workspace/mcp` 检查 `is_healthy` 和 `tools`，按技能上传、MCP 注册、连接/发现、收回阶段写入员工 capability。
- Schedule MCP 挂接去掉本地缓存，地址或配置变化后由 AgentScope 官方 `add_workspace_mcp` 替换。
- 部署器由监听地址/端口生成 Task/Schedule URL；生命周期验收执行 MCP `initialize`/`tools/list`；部署后通过真实 AgentScope 工作区注册 Task/Schedule，验收后删除临时 Agent/Session。

## 验证

### 真实 MCP 与 AgentScope

隔离环境：三个 MCP 分别监听 `127.0.0.1:19501`（Library）、`19502`（Task）、`19503`（Schedule）；临时 AgentScope 2.0.10 监听 `19500`，独立 SQLite 与 Redis DB 2。

| 项目 | 结果 |
|---|---|
| 无 key/无会话头 MCP `initialize` + `tools/list` | Library 4、Task 20、Schedule 3 个工具 |
| AgentScope 工作区注册 Task/Schedule | `python -m app.deployment mcp` 退出 0，两个 MCP 均 `is_healthy=true` 且工具非空 |
| 临时 Agent/Session 清理 | 验收后 `GET /agent/` 返回 `{"agents":[],"total":0}`；未创建员工、任务或业务会话 |
| 缺少业务上下文的工具调用 | Task `nexus_task_get` 返回 `{"ok":false,"error":"当前会话没有可执行该任务操作的业务上下文：调用来源不存在"}` |
| 不可达服务验收 | Task MCP 指向未监听端口时退出 1，输出 `[失败] MCP 验收：Task MCP 初始化或工具发现失败…` |

说明：AgentScope 删除 Session 后其工作区目录按官方设计可能保留；没有员工目录或任务记录，也没有永久校验 Agent/Session。

### 自动化测试

| 范围 | 结果 |
|---|---|
| `tests/nexus` | 173 passed，7 skipped |
| Agent Service `tests` | 19 passed，1 skipped |
| 部署/配置相关平台测试 | 70 passed |
| 全量 `tests/platform` | 1263 passed，25 skipped |

全量平台测试通过；在本地 `nexus.env` 和清空 `NEXUS_RUNTIME_ENV_FILE` 两种条件下均为 1263 passed、25 skipped。此前本机 OpenCode 版本配置导致的 10 个失败，已按本机实际版本 1.18.33 统一测试期望，并修复工作区 UI 用例的临时资源准备；没有放宽生产代码的版本校验。

Ruff：`services/nexus-api` 的 `app`、`nexus_api`、`tests` 全部通过。Agent Service 测试文件原有 import 顺序告警未在本轮处理。

### 未验证

- `sudo bash ops/deploy/install_nexus.sh` 的 Linux root 首次安装、升级、重复部署和重启：当前环境为 macOS，未执行。
- 公司 MySQL 8.0.36、Nginx/TLS 入口、真实 Worker 回连：未在本次环境执行。
- 生产环境已有 AgentScope/员工数据下的实际同步页面：仅用隔离 AgentScope 和 API 测试验证。
