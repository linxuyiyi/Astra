## Why

Nexus v1.0.1 需要从空业务仓库构建真实可用的数字员工管理、单员工对话和团队交流。复用 AgentScope 2.0 的执行、会话、持久化和资源机制，保留已确认 HTML 原型，避免迁移 Astra 的业务架构或引入模拟回复。

## What Changes

- 从 Astra 复制并最小适配 AGENTS、OpenSpec、Superpowers，不复制旧业务源码、历史规范或数据。
- 独立部署固定版本的官方 AgentScope Agent Service；Nexus 业务后端通过 HTTP 接口集成。
- 员工与长期 Agent 一一关联，提供真实增删改、启停与原生能力资源关联。
- 匿名浏览器身份下提供持久化、多轮、流式私聊及真实文件和图片附件。
- 通过公开扩展实现平等长期员工的真实消息交流，复用原生 Session 与模型推理，增加必要去重和终止限制。
- Vue 3 + Element Plus 实现原型三个页面及加载、失败、空状态；提供部署说明和真实验收证据。

不开发首页、3D 空间、Task 生命周期、OpenCode Worker Pool、外部通信、审批、定时主动工作或旧数据兼容。

## Capabilities

### New Capabilities

- `agent-service-integration`：官方服务部署、版本与接口契约、持久化及运行边界。
- `employee-management`：员工身份、职责、指令、启停、资源关联和删除历史。
- `employee-conversation`：匿名隔离、多会话、真实推理、历史恢复和流式事件。
- `conversation-attachments`：上传、下载、预览、内容读取与多模态能力边界。
- `peer-discussion`：讨论管理、人类 mentions、独立员工互发消息、事件展示与循环终止。
- `nexus-interface`：三个页面的原型一致性及可用状态。

### Modified Capabilities

无。新仓库 `openspec/specs/` 为空，不迁移 Astra capability。

## Impact

新建 `agent-service/`、`backend/`、`frontend/` 与部署、测试目录。使用官方 `agentscope==2.0.10`；不复制或修改其核心源码。业务 SQLite 仅保存员工扩展、匿名所有者、原生 Session 关联、讨论投递与附件；原生执行状态和活动会话消息以 AgentScope 为事实源。匿名 UUID 仅适用受控验证环境。
