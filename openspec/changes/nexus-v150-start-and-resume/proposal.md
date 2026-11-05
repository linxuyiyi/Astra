## Why

启动请求可在任务取消并释放 Worker 后继续提交。已绑定原生会话的「继续」返回成功但只记事件。

## What Changes

- 在准备工作区和提交前重新核对启动轮次、任务状态和 Worker 占用。
- Web 与 MCP 的恢复动作只重试尚未创建 Session 的启动。已有会话使用原生输入、回答或授权。

## Capabilities

### New Capabilities

- `nexus-task-start-and-resume` 启动取消互斥和等待中的可执行动作。

### Modified Capabilities

无。

## Impact

涉及 Task Runtime、Task API、Task MCP 与任务页面。
