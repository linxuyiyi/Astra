## ADDED Requirements

### Requirement: 维护请求人来自服务端解析的身份
系统 SHALL 在提交维护操作时由服务端解析请求人，来源依次为可信代理注入的 `X-Verified-UID`、手动模式下的 `X-Owner-Id`、以及 `nexus_uid` Cookie。请求体 MUST NOT 决定请求人；客户端提交的 `requested_by` 不参与持久化。无法解析身份时 MUST 返回 401，不得写入操作记录。

#### Scenario: 可信代理提供 UID
- **WHEN** 反向代理注入 `X-Verified-UID: alice`，客户端提交部署
- **THEN** 维护记录 `requested_by` 为 `alice`，与请求体无关

#### Scenario: 手动 UID 模式
- **WHEN** 未经过可信代理，请求携带 `X-Owner-Id` 或 `nexus_uid` Cookie
- **THEN** 维护记录保存该 UID，而不是默认文本

#### Scenario: 客户端伪造请求人
- **WHEN** 请求体包含 `requested_by: console-admin`，但请求头身份为 `bob`
- **THEN** 维护记录保存 `bob`，忽略请求体字段

#### Scenario: 浏览器编码的 UID
- **WHEN** 手动 UID 包含中文或特殊字符，浏览器将其编码后写入 `nexus_uid` Cookie
- **THEN** 维护记录保存还原后的原始 UID，与聊天身份一致；可信头与手动身份头仍按原值解析

#### Scenario: 缺少身份
- **WHEN** 请求没有可信头、`X-Owner-Id` 或 Cookie
- **THEN** 提交被拒绝并返回 401，不创建维护操作

### Requirement: 人工动作可追溯实际操作者
系统 SHALL 在取消、重试与只读核对时解析操作者，并把操作者写入对应的维护事件。重试与取消 MUST NOT 改写原始发起人；自动系统动作 MUST 保留实际系统来源（如 `worker-channel`、`api`）。

#### Scenario: 不同用户重试失败操作
- **WHEN** 用户 `bob` 重试由 `alice` 发起且已失败的操作
- **THEN** 操作记录仍显示发起人 `alice`，重试事件记录操作者 `bob`

#### Scenario: 请求取消
- **WHEN** 用户通过取消接口请求停止进行中的操作
- **THEN** 取消事件记录该用户，原始发起人不变

#### Scenario: 系统自动动作
- **WHEN** Worker 通道或执行器自动写入维护事件
- **THEN** 事件保留其系统来源，不被人工 UID 覆盖

### Requirement: 历史缺失 UID 的记录不追填
系统 SHALL 区分历史无 UID 记录：`requested_by` 为 `console-admin` 或 `api` 的旧记录 MUST 标注为未记录 UID，MUST NOT 用当前用户或其他猜测值替换。新记录 MUST 显示解析后的真实 UID。

#### Scenario: 查看历史记录
- **WHEN** 维护记录由旧版本以 `console-admin` 创建
- **THEN** 页面标注“历史记录，UID 未记录”，记录内容保持原样

#### Scenario: 查看新记录
- **WHEN** 维护记录由当前版本创建
- **THEN** 页面显示服务端解析并保存的请求人
