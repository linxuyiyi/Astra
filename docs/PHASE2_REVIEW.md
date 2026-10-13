# 第二阶段审查记录

本记录仅保存验证与限制，唯一实施清单为 OpenSpec change 的 tasks.md。

## 原因与改动

独立部署官方 AgentScope 2.0.10 Agent Service，使用原生 SQLite、单进程消息总线及 PER_SESSION workspace。Nexus FastAPI 后端以 HTTP adapter 集成，模型凭据和默认绑定仅由 AgentScope 服务管理。统一脚本提供两个进程的启停与实例归属检查，不复制官方执行核心。

Codex 独立审查发现的 PID 归属、配置范围、敏感错误、readiness、分页完整性、SSE 读取错误、重复启动健康等问题已由 OpenCode 分步修复；缺少模型配置明确未验证，不使用假模型。

## 验证

- Codex 执行 `uv run pytest -q`：100 passed。
- Codex 执行真实服务进程 CRUD/Session/重启 smoke：`evidence/phase2/smoke-20261013T164904Z.json`，passed。
- 后端真实连通及停止上游后 503：`evidence/phase2/smoke-runtime-20261013T170716Z.json`，passed。
- Codex 安全注入已有 DeepSeek 凭据执行完整真实模型 smoke：`evidence/phase2/smoke-runtime-20261013T171016Z.json`，passed。密钥未写入源码、命令文本或证据；测试数据库按隔离测试流程清理。
- 四轮 A1/A2/B1/A3 均 `finished_reason=completed`，含 REPLY_START、非空 TEXT_BLOCK_DELTA、REPLY_END。原生消息最终在 idle 后读取校准。
- A2 回忆随机口令成功，独立 Session B1 回答不知道；AgentScope 实际进程 stop/start 且无环境密钥重新注入后，A3 在原 Session 回忆成功。
- 两个 Session 的原生 workspace ID 不同（**仅证明 ID 不同，不证明物理文件隔离**，见文末更正）；原生顶层 Agent ID 绑定正确；测试 Agent 已真实删除。

## 风险与未完成范围

这里只验收官方服务与后端集成底座，员工业务、匿名 Conversation、附件、页面和 peer 通信尚未实现。单进程内存总线不承诺多进程恢复；进程控制依赖 macOS/Linux 的 fcntl 与 ps。匿名 UUID 最终仍只适用于受控验证环境。

后续源码审查更正：本阶段只验证了两个 Session 的 workspace ID 不同，尚不能证明物理文件隔离。官方 LocalWorkspaceManager 2.0.10 实际按 Agent ID 共用 workdir，因此此前 PER_SESSION 配置不作为文件隔离通过依据。

任务 3.2.1 已修复：`agent-service/nexus_agent_service/workspace.py` 新增公开扩展 `SessionWorkspaceManager`（`WorkspaceManagerBase` + `LocalWorkspace`），按原生持久 workspace ID 的 SHA-256 建独立物理目录，并装配进服务。新增真实证据：`tests/agent_service/smoke_workspace.py` 通过真实进程 start/stop/start 与官方 `/workspace/files` 验证同员工双 Session 物理目录不同、A 写文件 B 不可读、进程重启后同 ID 同文件仍在；`tests/agent_service/test_workspace_manager.py` 覆盖路径穿越防护、并发同 ID 只初始化一次与 `close`/`close_all`。证据写入 `evidence/phase3/smoke-workspace-*.json`。本阶段早期证据（`evidence/phase2/smoke-*.json`）的范围应理解为“原生 CRUD/Session/重启恢复与 workspace ID 不同”，不再作为物理隔离通过依据。
