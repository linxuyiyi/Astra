## Context

Employee.avatar 已持久化为字符串；配置/列表/聊天直接展示 Emoji，办公室按 ID/职责的 hash 选择 V5 机器人，导致身份分裂。BotPixel 0.10.2 提供 MIT 许可的稳定像素人物及渲染函数。

## Goals / Non-Goals

用一个 avatar 标识在所有 Nexus 员工入口保持同一角色，支持原有机器人和可选像素人物。不接入 BotPixel 的工具变形、Agent 内部动作或记忆；不新增 AgentScope 状态。

## Decisions

- 复用已有 avatar 字符串，不增加并行头像字段。唯一目录存为 nexus_api/employee_avatars.json，Python 验证及 Vue/聊天岛共享模块均读取该文件。资源仅来自本地受控路径。
- 保留六款机器人，新增六个 BotPixel 人物预设；固定版本 0.10.2，从官方渲染函数生成透明 SVG 并附许可证，不增加前端运行时依赖。人物只作视觉选择，不修改岗位；现有机器人继续默认。
- 新增 0006 migration，将旧值按升级前办公室算法一次性转换为相同机器人。冻结算法在 migration 内；长期业务代码仅接受目录中的 ID。新建省略使用默认机器人，更新省略保留已保存值。
- 当前形象在配置、员工列表/详情、办公室、聊天目录与群聊发言通过共享目录渲染。办公室使用同一角色坐姿，人物使用其透明 figure 自然放在椅子上；未知数据明确显示未配置，不能悄悄换成另一个人。
- 配置选择采用按钮预览网格，保持 Element Plus 表单/保存/脏数据退出规则。形象只是表单草稿，保存成功后才改变目录。

## Migration Plan

仅在隔离库执行迁移和 E2E；原运行服务不变。公司部署前备份数据库并执行 alembic upgrade head 后构建/重启统一服务。旧 Emoji 不再承担人物身份，回滚需恢复升级前备份，不能猜回原 Emoji。

## Risks / Trade-offs

- API 从任意字符串收敛为目录 ID，是显式契约收紧；使用旧 Emoji 的 API 客户端需更新。
- 人物素材是 BotPixel 原生 32×32 chibi figure，不是 VRM 或全身行走骨骼；保留坐姿呈现和平台状态提示，不虚构动作事实。
- 生产 MySQL 迁移与真实 AgentScope/Worker 执行独立列为未验证边界。
