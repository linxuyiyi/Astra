# Nexus v1.5.0 公司环境排查

对照远端 `v1.5.0` 提交 `08b0be4bacccac3c76e4915b7fcfeddc485aaf76`。修改和验证在本地与隔离服务中完成。

| 项目 | 归因 | 整改和验证 |
| --- | --- | --- |
| 1. Worker 入口 | 公司侧决定端口放行、路由及允许的来源网段。仓库中的 18130 是 Nginx 默认 Worker 入口，Nexus 侧会默认选用该端口，覆盖旧派生 URL 时也缺少诊断。部分主机内部服务调用仍直连进程监听端口。 | 安装明确确认 Worker HTTP 端口，非交互安装显式传入。以一个端口派生 Nexus API、AgentScope、内置 MCP、模型网关与 Worker HTTP/WS 地址。更换端口时记录重新生成的地址。真实 Nginx 验证了 HTTPS、内部 API/MCP、模型鉴权、Worker 数据和 WS 路由。 |
| 2. 就绪窗口 | Nexus 缺陷。原有 30 秒在安装之后开始计时，首次安装总耗时超过五分钟本身无法证明就绪超时。原验收依赖数据库运行状态，PAUSED 也可能离线，失败摘要没有展开装备阻塞。 | 使用现有部署总时限的剩余预算，通过 Gateway 连接归属与 epoch 确认是本次部署的连接。输出守护、握手和装备详情，守护退出或确定失败时提前结束。回归覆盖超过原窗口、旧连接、重部署和失败诊断。 |
| 3. MCP 请求头 | Nexus 缺陷。Worker 已保存的 `mcp_refs[].config` 在生成 OpenCode 配置时被丢弃。 | 保留 URL、自定义请求头、OAuth 和超时，敏感值进入现有日志脱敏链路。真实 OpenCode 1.18.35 使用自定义头完成 `initialize` 和 `tools/list`。 |
| 4. 中文 Skill | Nexus 缺陷。Worker 仅允许 ASCII 名称，平台允许中文。 | 平台登记与 Worker 同步允许合法 Unicode，拒绝路径分隔符、遍历、控制字符及超长组件。真实 OpenCode 读取「任务管理」。 |
| 5. Skill 源 | Nexus 缺陷。Worker 部署与就绪计算仍读取 `worker-skills`，平台实际存储是 `NEXUS_DATA_DIR/skills/<id>/<version>`。 | 统一从平台数据库绑定及当前启用版本取主文件和附件。版本变化参与期望配置比较，缺失或停用时显示绑定名。回归覆盖首次下发、附件、版本切换、停用和重新部署。 |
| 6. MCP 握手 | 公司实际瞬时故障的来源待查，可能涉及网络或 MCP 服务。Nexus 确认缺少针对已知瞬时失败的连接恢复。 | 对超时、连接错误和可识别的临时 HTTP 失败最多重连两次，401/403 和待认证直接反馈。真实 OpenCode 验证 503 恢复后通过原生 MCP 连接接口重新连通。 |
| 7. 工作区异常 | 公司 `reading 'some'` 的触发响应及调用栈尚无现场证据。仓库确认原生应用渲染异常会被前端替换为 Worker 离线提示。 | 校验任务工作区 Session 列表契约，显示真实失败。离线提示依据连接事实。Chrome 通过真实 Worker 与 OpenCode 验证打开、发送、流式回复、刷新和异常注入。公司原报错在正常响应下未复现。 |

## 入口与重部署

- 浏览器 Web、页面 API 和 SSE 使用 Nginx HTTPS。
- 平台内部 API、AgentScope、Task 和 Schedule MCP 使用控制机 Nginx 回环 HTTP。Worker 使用内网 Nginx HTTP 的模型、Library MCP 和数据通道，WebSocket 使用同入口的 `ws://`。来源限制和 Worker token/epoch 仍生效。
- 公司现场应在部署配置明确填写已放行的 `NEXUS_DEPLOY_WORKER_PORT`，以 Worker 实际出口地址设置 `NEXUS_DEPLOY_WORKER_ALLOW`。端口变更后先部署平台，再重部署 Worker，核对其 `worker.env` 与 OpenCode 配置。外部自定义 MCP 使用登记的服务地址，该代理入口由相应服务的部署方提供。

## 修改与验证

- 入口生成、代理与发布验收涉及 `ops/deploy` 和 `app/deployment.py`。Worker 预检、配置、资源和就绪涉及 `app/runtime/remote_provisioning.py`、`remote_workspace.py`、`repository.py`、`worker_readiness.py`、`app/worker_agent/inspection.py` 与 `nexus_api/resources.py`。
- 工作区诊断及 MCP 请求头脱敏涉及 `web/workspace-ui/src/adapter.tsx`、对应生产制品及 `app/api/workspace_routes.py`。数据库 schema 无变更。
- 后端全量 886 通过、20 跳过。最后的入口变更另跑真实 Nginx 定向回归及配置测试。Web 72 项、原生工作区 3 项均通过，类型检查和生产构建通过。
- 现场待验收项为公司目标机及实际 Worker 之间的入口放行、远程 Linux SSH 全新安装和重部署，以及公司原生工作区报错。定位原报错需要现场浏览器首条调用栈、对应接口响应与发布提交。
