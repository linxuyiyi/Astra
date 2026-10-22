## ADDED Requirements

### Requirement: 查询可用分类
Task MCP SHALL 为认证员工提供已启用分类的 ID、名称、说明、task_prompt 与 question_prompt，按配置排序；MUST 不硬编码公司分类或修改任务仓库绑定。

#### Scenario: 员工创建任务前读取分类
- **WHEN** 认证员工查询分类
- **THEN** 返回数据库当前已启用分类，不返回禁用分类；无分类时返回空列表

#### Scenario: 未认证调用
- **WHEN** 缺少有效 MCP 凭据或员工身份
- **THEN** 请求被拒绝
