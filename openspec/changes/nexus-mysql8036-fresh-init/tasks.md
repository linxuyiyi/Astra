## 1. Nexus 统一 SQL 存储

- [x] 1.1 将员工/聊天/IM/资源表纳入共享 schema，使用配置指定的 SQLAlchemy 连接和显式 SQL，验证 SQLite 业务回归与事务回滚。
- [x] 1.2 适配 upsert、去重、字段容量与精确比较，验证 MySQL 8.0.36 CRUD 和 IM 并发认领。

## 2. 全新初始化

- [x] 2.1 删除历史 Nexus revision 和旧库合并入口，固化单个 0001_init，验证空库、重复初始化、旧库拒绝和模型一致性。
- [x] 2.2 统一启动 schema 校验与测试初始化，验证无 SQLite 隐式旁路及重启保留数据。

## 3. AgentScope SQL

- [x] 3.1 接入官方 2.0.10 SQL 存储和显式初始化，保留 RedisMessageBus，验证缺配置/失败/生命周期及持久化恢复。
- [x] 3.2 更新依赖、安装器、环境示例和部署约定，明确两个数据库归属、全新部署和不兼容旧库。

## 4. 验证闭环

- [x] 4.1 运行真实 MySQL 8.0.36 的双服务初始化、关键 API 与重启/并发检查；无法验证项保留为未完成并记录原因。
- [x] 4.2 完成后端回归、Ruff、diff 检查和 OpenSpec Scenario 核验，记录验证证据与剩余风险。

## 验证记录（2026-10-21，本机 MySQL 8.0.36 + Redis）

| 检查 | 结果 |
| --- | --- |
| 空 MySQL 库 `alembic upgrade head` | 54 张表，全部 InnoDB、全部 `t_nexus_` 前缀，`alembic_version=0001_init` |
| 重复执行同一条命令 | 无新表、无数据变化 |
| 旧库拒绝（未标记表、`0058_drop_pool_route_weight`） | 失败并提示使用新的空数据库；原库数据与版本不变 |
| 空 SQLite 库升级 | 成功，重复执行幂等 |
| `tests/mysql/test_business_smoke.py`（真实 MySQL + 双服务进程） | 1 passed：员工长文本、私聊、团队、任务、重启恢复、16 路 SSE、IM 并发认领、peer 去重、数据目录无 `.db` |
| AgentScope `agentscope_sql.py init/check` | 通过；`messages.created_at` 精度为 6 |
| `agent-service/tests/test_sql_storage.py` | 9 passed：缺配置、不可用库不回显凭据、同秒消息顺序、游标分页、重启恢复 |
| `tests/platform/test_fresh_sql_storage.py` | 5 passed：初始化完整、旧库不动、连接退出回滚、坏 schema 拒绝 |
| SQLite 后端回归 `pytest tests/platform tests/nexus` | 1204 passed / 18 skipped / 4 failed / 4 deselected；失败均为 Linux-only 用例（`os.O_PATH`、`remote_install.sh` 仅支持 Linux），在 `7554ca0f1` 同样复现 |
| `ruff check app nexus_api tests` | 通过 |
| 前端 `npm run type-check`（web、chat-island） | 通过 |

Scenario 核验：空库初始化并重复执行、完整 API、拒绝旧库、IM 幂等、未提交写入回滚、AgentScope 初始化与重启、同秒消息顺序与分页、数据库异常不回退均有上述证据。

未验证（需公司环境或真实推理）：公司 MySQL 的 TLS、账号权限、`sql_mode`、容量与备份恢复；生产数据量下的并发与锁等待；`contact_employee` 的 inbox + MessageBus 唤醒端到端（需要模型推理）。这些项目不能由本机 SQLite/MySQL 冒烟替代。
