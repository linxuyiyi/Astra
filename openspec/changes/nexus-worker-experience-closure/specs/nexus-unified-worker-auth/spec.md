## ADDED Requirements

### Requirement: Worker 协议独立鉴权

统一服务 SHALL 使用既有 Worker token 和 epoch 校验机器数据请求，使用部署管理密钥校验登记及控制请求，不要求执行机提供浏览器会话。

#### Scenario: 无浏览器会话的 Worker
- **WHEN** Worker 使用有效 token 和 epoch 拉取属于自己的命令
- **THEN** 请求到达 Worker 协议并按命令归属返回结果
- **AND** 无效 token、过期 epoch 和跨 Worker 命令仍被拒绝

#### Scenario: 管理密钥不足
- **WHEN** 未登录请求登记 Worker，且没有有效管理密钥
- **THEN** 返回协议鉴权失败，不登记 Worker

### Requirement: 统一服务的维护操作人来自登录会话

Nexus 统一服务 SHALL 使用服务端已验证登录会话中的 UID 作为维护操作人，不接受客户端自报身份。普通聊天用户不得提交维护。

#### Scenario: 管理员正常维护
- **WHEN** 已登录管理员不携带旧 UID 头或 Cookie 提交维护
- **THEN** 操作成功登记并记录登录账号 UID

#### Scenario: 伪造身份
- **WHEN** 管理员携带另一个人的 X-Verified-UID、X-Owner-Id 或 nexus_uid
- **THEN** 审计仍记录真实登录账号 UID

#### Scenario: 未登录及无管理权限
- **WHEN** 用户没有登录或只有聊天权限
- **THEN** 维护请求分别返回 401 或 403，不产生维护记录

### Requirement: 经验审核和执行停止确认记录真实账号

统一服务 SHALL 从服务端登录会话取得知识审核人与执行停止确认人，不读取客户端自报身份。任务详情 SHALL 对取消但仍占用 Worker 的任务提供明确的人工确认停止入口，复用既有占用规则，不因显示取消就自动释放。

#### Scenario: 取消后确认释放
- **WHEN** 管理员核实执行停止并在取消任务详情明确确认
- **THEN** 记录该登录账号和确认原因，按既有 Session/attempt 匹配规则释放 Worker
- **AND** 尚未确认时页面说明该任务仍占用 Worker

#### Scenario: 审核身份不可伪造
- **WHEN** 审核发布或确认停止携带伪造身份头
- **THEN** 审计仍记录真实登录账号，且普通聊天账号不能越权
