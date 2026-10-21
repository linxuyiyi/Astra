# Gateway Request Admission

## ADDED Requirements

### Requirement: 流式响应只有完整有效才算成功

系统 SHALL 以「媒体类型为 `text/event-stream`、至少一帧实际内容、出现 `[DONE]` 或非空 `finish_reason`」判定流式成功；只有结束标记、空内容、错误媒体类型、未到终止标记即 EOF 都记为失败。

#### Scenario: 只有结束标记

- WHEN 上游返回 200 与 `text/event-stream`，但只有 `data: [DONE]`
- THEN 调用日志记录 `STREAM_EMPTY`
- AND 该请求不计为成功，节点被动失败按现有熔断规则记录

#### Scenario: 错误媒体类型

- WHEN 客户端请求 `stream=true`，上游返回 2xx 但 `content-type` 不是 `text/event-stream`
- THEN 网关返回 502 `UPSTREAM_PROTOCOL_ERROR`，不把普通响应伪装成流
- AND 上游连接关闭、租约释放

#### Scenario: 意外断流

- WHEN 上游在出现终止标记前关闭连接
- THEN 调用日志记录 `STREAM_INCOMPLETE`
- AND `stream_interrupted` 为真，节点被动失败

#### Scenario: 正常短流

- WHEN 上游返回一段内容并以 `[DONE]`（或 `finish_reason`）结束
- THEN 请求成功，`stream_interrupted` 为假，usage 正常计量

### Requirement: Key 小时/日额度参与准入

系统 SHALL 在调度前读取 Key 的小时与日用量；任一达到上限即返回 429 `KEY_QUOTA_EXCEEDED`，不获取节点租约、不向上游发请求。

#### Scenario: 达到额度

- WHEN Key 小时或日用量已达到配置上限
- THEN 请求返回 429，调用日志记录 `KEY_QUOTA_EXCEEDED`
- AND 上游请求数为零

#### Scenario: 额度未达上限

- WHEN Key 用量低于上限
- THEN 请求照常调度；额度不改变节点选择

#### Scenario: 未知用量

- WHEN 分桶里存在未知用量请求
- THEN 准入只按已知可计费用量判断
- AND 页面同时展示未知用量数量，不把未知当作零成本

### Requirement: 节点额度策略语义统一

系统 SHALL 只让 `STOP` 在达到上限时阻断新请求；`ALERT` 与 `RECORD` 在 79%–120% 之间不改变调度权重，也不阻断。

#### Scenario: STOP 达限

- WHEN 节点策略为 STOP 且小时或日用量达到上限
- THEN 调度跳过该节点，错误原因 `QUOTA_EXCEEDED`

#### Scenario: ALERT/RECORD 区间

- WHEN 节点策略为 ALERT 或 RECORD，用量在 79%–120%
- THEN 节点权重与未达额度时一致
- AND 告警/记录只体现在运行事件与页面状态

#### Scenario: 在途请求

- WHEN 节点在请求执行中越过额度阈值
- THEN 该请求继续完成并计量
- AND 只影响之后的新请求

### Requirement: 客户端模型别名统一回写

系统 SHALL 在非流式响应与每个流式 `data:` 帧中把 `model` 回写为客户端请求的模型，不缓冲整流、不改变重试范围。

#### Scenario: 流式分块边界

- WHEN 上游 JSON 帧被任意切分在字节边界
- THEN 网关按行重组后仍正确替换 `model`，其余字段与顺序不变

#### Scenario: 非数据帧

- WHEN 帧是注释、空行或 `[DONE]`
- THEN 原样透传，不尝试解析

#### Scenario: 取消与异常

- WHEN 客户端断开或上游抛错
- THEN 已写出的字节不被追加，租约释放，日志只记录一次结局
