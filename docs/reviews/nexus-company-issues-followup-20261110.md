# Nexus v1.5.0 公司反馈问题补充回归

日期：2026-11-10。基于同一 checkout 中的 SSH/OpenCode 架构替换继续修复。本文记录本地代码证据与验证，不据此认定公司历史故障的唯一原因，也不表示公司环境已更新。

## 原因与修改

### Session 已空闲，Task 仍显示执行中

原生状态对账已确认父子 Session 均不忙后，读取当前轮结果失败可能使 Task 留在 RUNNING。现在进入 WAITING / result_pending，保留原 Session 与 Worker 占用；后台继续对账，结果可读后进入人工确认或已确认的执行失败。结果待核对时禁止确认完成。

根 Session 空闲但子 Session 仍 busy/retry 时，Task 继续 RUNNING；人工确认完成后才进入 SUCCEEDED。继续以原生 API 为准，不从单条 SSE 推断完成。

### Worker 频繁离线，页面使用中断

- 健康请求原先只有固定 3 秒超时；改为复用现有 OpenCode 控制请求超时，不新增配置。
- 原生健康请求失败会错误关闭仍活跃的 SSH Transport 和转发。现在只将健康事实标记为不可用，SSH 确认失活时才释放；后续成功探测恢复在线事实。
- 普通 HTTP/SSE 连接使用已复用的转发，启动任务时与后台巡检进行健康校验；避免每条工作区请求前叠加健康探测。
- 页面不再随离线标记清空已有工作区绑定或卸载 iframe。实际请求和原生 SSE 自行恢复；Task/Worker/Session 绑定变更或确认 Session 缺失仍按原规则处理。
- 打开工作区时尝试读取原生服务，不用缓存离线标记拒绝可达的服务。SSH 暂不可用时返回明确连接提示，恢复后可重新读取。

这些修改不触发远端 OpenCode 重启。

### 员工 MCP 未在每次会话完整挂载

- 私聊、重建会话、群讨论、同事邮箱联系和 IM 每次运行前核对当前 Session 的员工绑定资源；IM 复用旧 Session 也会核对。
- 资源同步未完成或失败时阻止新的推理输入，返回具体错误；保存员工配置仍保留现有同步状态反馈。
- 读取本会话 MCP 列表与健康状态，补装缺失资源，重新挂载明确不健康的连接；安装后再次读取原生列表，确认绑定且启用的 MCP 均存在并健康。不会把平台所有 MCP 隐式挂到员工。
- 无法读取 Session 状态时不再猜测 idle 并修改工作区；权限 PATCH 失败不再被当成同步成功。
- 修正同事联系入口：先设定无人值守权限，再准备资源；不再在准备完成后把 MCP 所需的 bypass 改回 dont_ask。
- 无绑定资源的运行省去工作区同步，但仍处理已登记的资源收回待办。

原生挂载与健康语义已核对 [AgentScope v2.0.10 工作区路由源码](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_workspace.py)：按 Session 列出 MCP 及工具健康状态，from-library 按资源返回部分成功结果。

## 验证

| 检查 | 结果 |
| --- | --- |
| `pytest tests/platform tests/nexus` | 821 passed / 20 skipped |
| 最终 Runtime/SSH 定向回归 | 60 passed / 1 skipped，含新增 SSH 失败的工作区响应处理 |
| 独立真实 SSH / OpenCode 原生 API 集成 | 6 passed；真实 loopback SSH、HTTP/SSE、并发、断线重连及健康失败后保留同一 Transport |
| web 单元测试 | 72 passed |
| web 类型检查、生产构建 | 通过；构建产物已同步 |
| 桌面 Playwright `nexus_task_edit.mjs` | 通过；离线/恢复前后保留 iframe DOM 内容、无额外绑定读取，保留任务编辑与交互回归 |
| Ruff 与 `git diff --check` | 通过 |

全量回归覆盖两个独立私聊 Session 的全部 MCP、旧 Session 缺失/不健康 MCP 补装、部分安装/权限/原生状态/目录失败阻止运行、恢复后复用原 Session、同事邮箱最终权限、群讨论失败后恢复、IM 旧 Session 重新补挂。

隔离 SSH 测试服务也修正了目标端口尚未监听时的通道关闭，模拟真实 SSH 的连接失败行为；此后真实 OpenCode 测试无需等待健康请求读超时。完整后端回归通过后，最终 Runtime/SSH 回归也已通过。

## 尚未验证

没有连接公司服务器、更新公司数据库或部署公司服务；缺少当时部署版本、时间窗口及日志，尚不能将公司历史故障归因到上述具体路径。真实 AgentScope 进程与公司 MCP 服务、真实模型执行、公司 Linux Worker 长时运行与跨网络抖动仍待部署后验收。外部条件不足的测试保持 skip，不计为通过。

本次架构替换的删除组件、空库结构和早先隔离 MySQL 等证据见 [SSH Runtime 验收](nexus-ssh-runtime-20261109.md)。
