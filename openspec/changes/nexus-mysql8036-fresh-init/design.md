## Context

当前部署以 MySQL 8.0.36 空库为起点。Nexus 已有 SQLAlchemy 异步任务层和同步 SQLite 聊天层；AgentScope 2.0.10 官方提供异步 SQL 存储。用户明确不保留历史升级。

## Goals / Non-Goals

**Goals:** 全部 Nexus 表位于同一个配置数据库；一个 `0001_init` 初始化当前完整 schema；AgentScope 官方 SQL 存储独立初始化；重启后业务与对话绑定可恢复。

**Non-Goals:** 不导入旧库、不双写、不自动清空开发库、不 fork AgentScope、不改变 Runtime 状态机和 UI 行为。

## Decisions

1. 员工/聊天表加入 Nexus 共享 metadata。保留聊天层现有同步调用方式，使用 SQLAlchemy 同步连接（MySQL 使用 PyMySQL），任务层继续使用 asyncmy。两者派生自同一数据库 URL，连接池独立，数据事实源唯一。相比全量 async 改写，这保持业务事务与接口边界，减少无关改动；不保留 SQL 字符串翻译兼容器。
2. 查询显式使用 SQLAlchemy statement 和命名绑定。upsert/插入去重通过 SQLite/MySQL 方言构造器表达，IM claim/fencing 继续依赖条件更新与明确提交。
3. 聊天 schema 使用有界字符串主键、标识列和索引，JSON/大文本字段按现有序列化契约保存。MySQL 大文本避免 64KB 截断；时间与大小写比较保持现有行为。所有历史增列与重命名代码删除。
4. `0001_init` 固化当前 schema，之后变更另加迁移。初始化拒绝有历史版本或未标记业务表的库；服务启动只接受完整且当前版本的 schema，测试初始化走明确工具。
5. AgentScope 使用官方 `AsyncSQLAlchemyStorage`、官方迁移及独立数据库，不改其包内历史。初始化直接调用官方 migrations 并对 Alembic Config 的 URL 百分号转义；官方 2.0.10 的 MySQL `messages.created_at` 默认丢失微秒，部署阶段额外固定为 `DATETIME(6)`，启动检查精度，保持官方写入顺序及游标分页，不改官方 ORM 或存储实现。Nexus 的“一个 init”只适用于本仓库拥有的业务表；AgentScope 内部版本由官方 2.0.10 管理。RedisMessageBus、Qdrant、文件和 workspace 保持独立。
6. 生产部署在启动服务前分别初始化 Nexus 与 AgentScope 数据库。SQL URL 不写日志，数据库失败直接报告，不回退 Redis/SQLite。

## Risks / Trade-offs

- 同步驱动会阻塞当前调用线程，与原 SQLite 调用方式相同；连接预检与回收限制故障窗口，后续性能优化不混入本变更。
- 删除旧 revision 是破坏性版本边界；旧库必须另建空库，工具拒绝旧版本，不自动 stamp。
- 本机预装 MySQL 为 8.4.10；需取得独立 8.0.36 运行时，不能把 8.4 通过当作公司版本通过。
- SQLite 测试不验证 MySQL 锁；独立 MySQL 8.0.36 用例覆盖初始化、两类 API、IM 去重与重启恢复。
