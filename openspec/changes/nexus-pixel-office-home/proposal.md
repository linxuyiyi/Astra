## Why

现有星系首页不符合数字团队在办公室工作的直观体验；Worker 详情按目录绑定展示，无法完整覆盖共享 Worker 执行该员工任务的情况。V1.4.9 需要以 V5 可爱像素机器人办公室为入口，帮助用户看见工作并继续处理人工事项。

## What Changes

- 全屏像素办公室替换首页星系，默认全景，选择员工仅选中并打开右侧工作台，不放大员工，空白/Escape 关闭工作台。
- 复用 V5 素材，提供三类岗位各两款机器人；按真实目录展示，单房间最多十二位，更多员工分页访问。
- 新增只读员工办公室投影，按当前任务和现有调度配置关联共享 Worker，单列按人工/异常、执行、空闲排序，并提供真实任务与会话入口。
- 保留原有模块，通过悬浮入口访问；处理加载、空态、失败、快照过期与减少动态效果。

## Capabilities

### New Capabilities

- `nexus-pixel-office`: Nexus 沉浸办公室、真实员工工作台和共享执行资源只读投影。

### Modified Capabilities

无；`digital-office-home` 是 Astra 历史契约，不修改。

## Impact

涉及 `services/nexus-api/web` 首页、像素素材、办公室组件及 `nexus_api/nebula.py` 只读接口和测试。不修改 Task/Run/Session 生命周期、Worker 调度、AgentScope、业务库 schema 或其他模块业务。不自动创建员工或演示任务。
