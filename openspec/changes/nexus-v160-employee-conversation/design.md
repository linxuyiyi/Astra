## Context

入口为 Vue 的 `/chat`。v1.5.0 的 ChatPanel、自研 Runtime 和两个 Adapter 负责实际消息，另有打包发布但未被该页面挂载的 AgentScope 示例聊天岛。旧页已支持群聊提及、图片和文件、语音、停止及工具折叠。

## Decisions

- Vue 管联系人、群管理、会话选择及关联任务侧栏。私聊通过员工 API 取回所有者的会话绑定，群聊传群 id。Vue 只在会话选择变化时挂载 React，在离开时释放订阅。
- 私聊链接使用 `/chat?employee={id}`，以 `POST /api/v1/employees/{id}/conversations` 打开当前用户的绑定，然后以 `GET /api/v1/conversations/{id}/messages` 读取受授权的 Session、历史和游标。群聊链接使用 `/chat?group={id}`，以 `GET /api/v1/channels/{id}/messages` 读取历史和群成员快照，群组在创建时绑定群主。
- 私聊和群聊共用发送请求结构，包含 `content` 文本块列表、`client_id` 及可选的 `attachment_ids`。纯附件消息的 `content` 为 `[]`。服务从文本块派生正文，并按现行请求结构验证。
- 以现有两阶段构建将 React 导出为 ES 模块。宿主页使用同一份构建的 manifest 按需加载带哈希的脚本和样式。发布静态资源仍随 Nexus 服务提供。
- 私聊和群聊分别读授权范围内的消息和事件，在传输层归并事实后映射为 assistant-ui 消息。React 的 ExternalStoreRuntime、Thread、Message、Composer 与附件适配器负责交互。数据块用于同事、文件及任务结果；工具结果有消息内的折叠详情。
- 历史每次读取 50 条，由用户向前加载。连续流式增量约每 32 毫秒批量推送给 assistant-ui，首次回复和状态事件在下一渲染帧更新。私聊 `REPLY_END` 后读取持久消息与运行锁。两者尚未收敛时展示同步状态，以新快照为准继续对账。群聊分页外的增量推进事件游标，历史正文由分页快照提供。
- 用户主动提交消息时跟随至本轮。收到异步内容时保留历史阅读位置。草稿和未确认投递按 UID 和会话隔离。确认未送达时使用 assistant-ui 的 `MessageNotSentError` 回填草稿。结果未知时保留完整请求和 `client_id`，用户可原样重试或明确取回编辑。
- 员工聊天没有授权确认写接口；真实的确认请求可见，任务人工处理沿用既有任务入口。私聊进行中可编辑草稿，群消息按其投递契约处理。
- 会话目录与正文各有独立滚动容器。私聊与群聊使用同一正文宽度、排版和输入区。连续发言合并身份排版，发送者与原始时间仍留在消息中。新会话将输入区放在身份提示下，开始对话后输入区回到底部。
- assistant-ui `GroupedParts` 将相邻且已有成功结果的普通工具调用折叠成一行，展开第一层即展示已有的简短结果。任务、同事联系、审批与失败单独可见。同事和任务工具的业务失败依据各自的返回值展示，群聊工作者依据群快照和分配事件展示。

## 参考代码及许可

- assistant-ui 使用 MIT 许可。使用 ExternalStoreRuntime 的外部消息和回调接口，UI 通过 primitives 组合工具和附件。官方 Thread registry 把新会话时的输入区与欢迎内容相邻排布，用 `GroupedParts` 管理连续内容块，输入动作集中在同一容器。[Thread registry 源码](https://r.assistant-ui.com/thread.json) · [Runtime 文档](https://www.assistant-ui.com/docs/runtimes/custom/external-store) · [许可](https://github.com/assistant-ui/assistant-ui/blob/main/LICENSE)
- 本机 Grok Bot 0.78.1 的 Electron 发布包标为 private，包清单没有应用级开源许可。检查 renderer 的消息分支、工具调用 id、发送态及流式标记后，仅采用员工身份随消息、工具操作融入时间线的交互原则。产品代码和视觉资产均由 Nexus 自行实现。[产品说明](https://x.ai/bot)
- OpenBot 存在多个同名仓库，本次参考 ashhart/OpenBot 的 MIT 实现。其消息样式将用户输入右对齐，员工回复采用正文排版。`ToolCallView` 收起时呈现状态、名称和摘要，展开后再渲染参数与结果。`ApprovalDialog` 绑定具体审批请求。[Message.css](https://github.com/ashhart/OpenBot/blob/b544cb743986193fdc3d234ae66c8e44ef68fc00/src/renderer/src/components/Message.css) · [ToolCallView](https://github.com/ashhart/OpenBot/blob/b544cb743986193fdc3d234ae66c8e44ef68fc00/src/renderer/src/components/ToolCallView.tsx) · [许可](https://github.com/ashhart/OpenBot/blob/b544cb743986193fdc3d234ae66c8e44ef68fc00/LICENSE)
- CopilotKit 的 Travel 示例使用 `useRenderTool` 根据真实搜索状态显示执行进度，通过 `useHumanInTheLoop` 把 `respond` 与具体审批操作绑定。Nexus 按自身接口权限显示任务入口和待确认状态。[Travel 示例源码](https://github.com/CopilotKit/CopilotKit/blob/d85b612a3132b5d1113da92dc0c4beb06349b923/examples/v1/travel/lib/hooks/use-trips.tsx) · [许可](https://github.com/CopilotKit/CopilotKit/blob/d85b612a3132b5d1113da92dc0c4beb06349b923/LICENSE)
