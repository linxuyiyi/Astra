## Decisions

- 使用 AgentScope SQL Session 和 WorkspaceService 确定附件落点。本地工作区按用户、Agent 和 workspace ID 计算物理目录。
- 私聊和群组上传使用同一流式入口，作用域、原件 Session 和展示信息由 Nexus 引用表记录。群组使用专用资源 Session，推理使用员工独立 Session。
- Agent Service 使用目录描述符和原子发布阻止路径逃逸，Nexus 通过内部认证访问原件。群权限在 Nexus 校验。
- 存量附件先核对作用域和字节完整性，再更新历史引用。旧表保留至迁移核验通过。
