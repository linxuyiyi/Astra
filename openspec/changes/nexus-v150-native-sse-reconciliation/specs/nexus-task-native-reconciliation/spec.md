## ADDED Requirements

### Requirement: 后台原生事件对账

系统 SHALL 在浏览器关闭时由当前 Task 的原生状态及求助事件触发对账。OpenCode 原生查询提供状态事实。

#### Scenario: 原生执行结束

- **WHEN** 当前根 Session 或其子 Session 产生原生状态或求助事件
- **THEN** 后台查询原生状态并投影到 Task，完成验收仍由显式确认触发

#### Scenario: 断流或事件缺口

- **WHEN** 原生流重连、Worker 重新连接或事件存在缺口
- **THEN** 系统重新核对当前绑定的原生状态，丢弃已替换绑定的事件，保持现有独占规则

#### Scenario: 无事件和未知提交

- **WHEN** 健康流没有业务事件，或任务有待确认输入
- **THEN** 健康且无待办的任务无需定期原生状态查询，未知提交仍按当前 Session 优先的规则恢复
