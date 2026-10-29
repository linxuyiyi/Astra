## Why

员工配置保存 Emoji，而办公室按岗位与 ID 推导另一款机器人，导致同一员工在不同页面形象不一致。用户希望借鉴 DeepSeekBot / Tobs 的 BotPixel 像素人物，并让已保存的形象在各入口一致。

## What Changes

- 建立唯一的可选形象目录：保留 V5 六款机器人，加入 BotPixel 像素人物预设。
- 配置页可视化选择/预览，首页工位、右侧工作台、员工列表/详情及聊天均使用保存的形象。
- **BREAKING** 员工 avatar 从任意 Emoji 文本收敛为受支持的稳定形象 ID；已有数据通过新 migration 一次性保留当前首页机器人，不在运行代码中猜测旧值。
- 禁止因改名、岗位、列表顺序或状态变更重新选脸。取消或保存失败不改变已保存形象。

## Capabilities

### New Capabilities

- `nexus-employee-avatar-identity`: 员工形象持久化、预览、跨入口一致及一次性迁移。

### Modified Capabilities

无；不修改历史 Astra capability。本次为 Nexus 独立契约。

## Impact

影响 Nexus 员工 API 的 avatar 校验、新 Alembic 数据回填、前端形象组件与配置页、办公室、聊天岛的员工头像。只使用 BotPixel 渲染/素材，保留 MIT 声明，不引入其 Agent、工具状态或多 Agent 编排能力。不更改员工工作/Runtime 状态、AgentScope 存储或 Worker 调度。
