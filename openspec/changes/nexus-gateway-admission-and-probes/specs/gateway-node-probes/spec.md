# Gateway Node Probes

## ADDED Requirements

### Requirement: 探测以轮次为事实

系统 SHALL 一次探测操作保存一条轮次记录，包含来源（手动/自动）、首模型、作用域、耗时、步骤与用量；旧步骤记录标记为 `LEGACY_STEP`，只读展示，不猜测归组、不双写。

#### Scenario: 一次手动探测

- WHEN 操作员对节点执行一次探测
- THEN 历史里新增一条轮次，步骤与用量在该轮次内
- AND 不额外生成步骤级历史行

#### Scenario: 旧记录

- WHEN 读取迁移前的步骤记录
- THEN 返回 `LEGACY_STEP`，不合并进任何轮次

### Requirement: 探测共享租约与幂等

系统 SHALL 让手动与自动探测共享跨实例租约与轮次幂等；重复触发不产生第二次上游探测。

#### Scenario: 重复点击

- WHEN 同一节点在租约有效期内被再次触发
- THEN 返回已有轮次或进行中状态，不新增上游请求

#### Scenario: 租约失效

- WHEN 持有租约的实例消失且租约过期
- THEN 新实例可重新领取并记录一条新轮次

#### Scenario: 提交成功投影失败

- WHEN 轮次事实已提交但健康投影失败
- THEN 重试只补投影，不重复探测

### Requirement: 健康只推进一份运行事实

系统 SHALL 只由轮次最终结论推进节点运行健康；投影按轮次序号拒绝晚到覆盖。

#### Scenario: 晚到轮次

- WHEN 较旧轮次在较新轮次之后到达
- THEN 健康结论保持较新轮次的结果

#### Scenario: Redis 清空

- WHEN 运行态被清空
- THEN 页面显示未知
- AND 必须产生新的实际探测才能重新授权，禁止用历史轮次重放授权

### Requirement: 验证策略可执行

系统 SHALL 以一次 SSE 作为常规验证、非流式作为专项验证；新节点可选择健康路径，旧继承路径按部署值显式回填；首模型、路径或策略变化使旧授权失效。

#### Scenario: 常规验证

- WHEN 校验通过
- THEN 记录首模型与其余未验证模型
- AND 两成功三失败按完整轮次计算

#### Scenario: 配置变化

- WHEN 首模型、健康路径或验证策略改变
- THEN 旧授权失效，节点回到待验证

### Requirement: 探测不依赖业务可调度

系统 SHALL 允许对 `DISABLED`、`DRAINING`、`UNKNOWN` 节点执行首次验证，只受共享并发与费用约束，不要求已启用或健康。

#### Scenario: 非启用节点验证

- WHEN 节点为 DISABLED 或 DRAINING
- THEN 仍可执行探测
- AND 满载等待不计为失败，遵守独立超时预算

#### Scenario: 诊断失败

- WHEN 探测失败
- THEN 不打开业务熔断，只记录诊断结论

### Requirement: 配置关系读模型与保存分离

系统 SHALL 提供池成员、模型、关联路由与实际候选/阻断原因；启用路由必须有静态候选；移动/删除最后成员或删除目标模型前必须先停用路由或补足候选。

#### Scenario: 无候选启用

- WHEN 路由没有静态候选
- THEN 启用被拒绝并说明原因

#### Scenario: 临时不健康

- WHEN 候选存在但当前不健康
- THEN 只告警，不阻止配置保存

#### Scenario: 节点保存

- WHEN 保存节点
- THEN 一次提交包含所选池，验证与启用是分开的后续操作
