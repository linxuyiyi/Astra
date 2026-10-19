# Nexus v1.4.4 生产部署实施验证（2026-10-19）

范围：`nexus-v144-production-deploy` 的部署入口、配置隔离、systemd 服务定义、迁移与恢复、健康检查、可选模块和文档。基线 `ae6bc2e3e`，实施分支 `codex/nexus-v1.4.4-deploy-p1-entry`。

结论：本机可运行的单元/契约检查通过；真实 Linux systemd、Nginx、MySQL 恢复、Redis/Qdrant/blob/workspaces 恢复演练和模型计费调用未执行，不能据此声明生产验收通过，change 不归档。

## 验证结果

| 检查 | 命令 | 结果 |
|---|---|---|
| 部署相关组合 | `pytest test_production_deploy.py test_runtime_context.py test_employee_serve.py test_astra_service_stop.py test_service_gateway.py test_dev_daemon.py test_preflight_database_isolation.py test_phase4_channel.py::test_remote_port_default_is_14096_and_llm_wiki_tunnel_stays` | **109 passed**，exit 0 |
| ruff | `ruff check app tests ../../ops/deploy/nexus_deploy` | exit 0，All checks passed |
| Shell 语法 | `bash -n install_nexus.sh optional/llm_wiki.sh tests/linux_smoke.sh` | 3 个脚本 exit 0 |
| OpenSpec | `node .opencode/tools/openspec.mjs validate nexus-v144-production-deploy --strict` | exit 0，valid |
| diff 格式 | `git diff --check` | exit 0 |

证据目录：`docs/reports/evidence/nexus-v144-deploy/`。组合测试使用随机空闲端口启动本次独立 Redis，按 PID、create_time 和 cwd 核对后清理；未连接现有 Redis，未读取真实密钥，未调用计费模型。

## R01-R10

| 需求 | 状态 | 证据与边界 |
|---|---|---|
| R01 版本锁定 | 已验证（单元） | 完整 commit、仓库/HEAD/脏工作树校验与失败停止由 `test_production_deploy.py` 覆盖；真实远端网络中断未在 Linux 演练 |
| R02 配置保护 | 已验证（单元） | 用户配置/密钥保留、0600、全局 OpenCode 哈希不变、专属配置目录由测试覆盖 |
| R03 统一托管 | 源码核对 | 每进程 unit、无 oneshot 常驻托管、启停只操作 Nexus unit 由渲染/调用测试覆盖；真实 systemd 查询/重启/日志未验证 |
| R04 数据迁移 | 已验证（单元） | 单次迁移、并发锁、失败不写成功记录由测试覆盖；真实 MySQL/迁移失败未验证 |
| R05 健康验证 | 已验证（单元） | 端口归属、无效 HTTP 结构、无 worker、MCP 未认证、Redis/SQL 探针失败路径由测试覆盖；真实依赖不可达未在 Linux 演练 |
| R06 发布恢复 | 已验证（单元） | 构建/启动/readiness 失败回退、缺快照阻断、revision 不兼容阻断由测试覆盖；真实 systemd 回切未验证 |
| R07 成功记录 | 已验证（单元） | commit、配置摘要、revision、检查结果与失败无成功标记由测试覆盖 |
| R08 可重复执行 | 已验证（单元） | 重复安装复用 release、复用探针 key、不覆盖密钥由测试覆盖 |
| R09 可选模块隔离 | 源码核对 | 未启用模块不进入核心检查/安装；启用后的真实依赖安装未验证 |
| R10 文档统一 | 已验证（本机） | README、模板、Nginx、systemd 说明与代码默认端口/命名一致；真实入口路由未验证 |

## 全量测试边界

全量 `pytest tests -x -q --tb=short` 在 macOS 上不能完整通过，属平台/基线限制，不是本次变更失败：

- `test_phase4_channel.py::test_install_marks_inside_the_operation_and_serves_files` 因 `app/worker_agent/files.py` 的 Linux `openat2` 实现使用 `os.O_PATH` 失败；同一用例在基线 worktree `ae6bc2e3e` 复现 exit 1。
- `test_remote_provisioning.py` 的直接安装脚本用例依赖 `flock`，本机无该命令；`remote_install.sh` 未在本次修改。
- 去掉上述已知平台用例后，全量进程在 689 个用例后以 exit 140 无 summary 退出，最后用例位于 `test_session_workspace.py`；该文件单独运行 145 passed，确认是累积进程边界而非本次业务改动。未将全量标为通过。

## 未验证项

- 真实 Linux x86_64/aarch64 安装、重复安装、升级、错误 commit、Git 中断、磁盘/构建/端口故障注入。
- systemd 托管、重启、崩溃自恢复、停止超时与 MainPID/cgroup 归属。
- Nginx `-t` 与首页、`/api/`、`/api/v1/`、`/v1/`、`/agentscope-api/` 入口探测。
- MySQL `mysqldump`/恢复、Redis/Qdrant/blob/workspaces 恢复演练。
- 模型计费调用；报告中始终标记 skipped，未执行。
- 目标主机上的回滚与备份可恢复性。真实 Linux 验收任务保持 unchecked。
