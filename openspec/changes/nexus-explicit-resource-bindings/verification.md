# 实现核对

当前实现遵循用户最终确认的简单接口和全新空库约定。所有代码与测试任务完成，远端发布步骤另行记录。

| 契约 | 实现 | 验证 |
| --- | --- | --- |
| 资源独立登记与绑定 | `nexus_api/resources.py`、`main.py`、`capability.py`；删除特殊技能目录和自动 MCP 挂载 | `test_explicit_bindings.py` 覆盖空目录、独立解绑与连续保存；官方 AgentScope 真实 MCP 测试通过 |
| Task MCP 简单员工参数 | `app/task_mcp.py`、`task_tools.py`；删除来源权限链 | `test_task_mcp_auth.py` 覆盖无用户/会话/Skill、员工存在/不存在及参数 schema |
| 最终同步状态一致 | 全部会话同步后保存 capability，失败/待处理保留上次生效版本 | `test_resources.py` 覆盖私聊失败和运行中等待时的响应/列表一致性 |
| 分类派工、任务无仓库 | `Dispatcher`、Task 模型与 schema；删除仓库参数和仓库工具 | `test_task_routing.py`、`test_task_responsibility.py`；任务使用支持其分类的 Worker |
| Worker 直接 Git 配置 | 环境字段、Worker API、部署读取与配置页面 | `test_repositories.py`、Worker 维护/Git HTTP 测试，覆盖直接保存、凭据加密、清空、公开仓库与 Worker 复制 |
| 全新空库初始化 | 单个 `0001_init`；无旧版修订链、独立仓库表和任务仓库字段 | SQLite schema 一致性与 MySQL 8.0.36 临时空库实测通过 |

代码与契约未发现未实现项；公司部署未验收，未切换现有本地环境。详细测试结果和限制见 `evidence.md`。
