## 1. 实现
- [x] 1.1 删除本地认证与权限 API，恢复临时 UID 记录方式。
- [x] 1.2 清理数据库与群聊/Task MCP 耦合，新增 migration。
- [x] 1.3 删除前端登录与本地权限控制。
## 2. 验证
- [x] 2.1 更新测试身份注入与用例，验证入口删除、临时 UID、群归属和服务认证。
- [x] 2.2 运行后端测试、Ruff、前端检查构建和 migration 检查。
- [x] 2.3 按 Scenario 执行 OpenSpec verify，检查 diff 并记录边界。
