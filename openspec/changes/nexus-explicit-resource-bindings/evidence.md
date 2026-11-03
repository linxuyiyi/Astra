# 验证记录（2026-11-03）

## 最终范围

Skill/MCP 独立登记和绑定；内网 Task MCP 显式传员工编号、只检查员工存在；任务按分类和类型派发，Worker 直接管理 Git 地址和分支。使用当前空库 init，不提供旧库迁移、回填、兼容层或双写。

## 结果

- 统一后端：在 `services/nexus-api` 下运行 `pytest tests/platform tests/nexus -q --tb=short --disable-warnings --maxfail=5`，1365 passed、32 skipped。日志：本机 `/tmp/nexus-category-full-tests-final.log`。
- 全量回归后清理已无调用的来源权限参数，Worker 求助、续答与 Session Workspace 相关 183 项再次通过。
- Agent Service：官方 AgentScope 2.0.10 环境运行全部 `services/agent-service/tests`，19 passed、1 skipped。包含 SQL、真实 HTTP MCP、日程执行与锁释放；模型为确定性测试实现。日志：本机 `/tmp/nexus-agent-service-regression.log`。
- Web：79 项测试通过；type-check 和 build 通过。构建仍有既有的大 bundle 提示。
- Ruff：API `app nexus_api tests` 与 Alembic 目录通过；`git diff --check` 通过。
- 全新 MySQL 8.0.36：单独启动临时 mysqld，仅使用 Unix socket，在空库执行 init。版本为 `0001_init`，57 张表；无独立仓库/凭据表，无任务仓库 ID；Worker 直接包含 Git 地址、分支和加密凭据字段。测试结束后进程停止、临时库目录删除。
- 测试制品 fixture 固定 gzip 时间戳，避免两次 HTTP 请求生成的压缩内容摘要不同；未修改生产制品校验逻辑。
- 全新 SQLite：固化 init 与当前模型结构一致，重复初始化不丢已有配置；不提供旧版升级测试或旧数据回填。

## 边界

未切换现有本地实例，未修改其数据库、员工、会话和运行配置。MySQL 测试为独立临时实例；没有把本地验证视作公司 Linux Worker 完整部署验收。跳过项未计入通过数。
