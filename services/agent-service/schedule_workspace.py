"""固定 2.0.10 本地工作区的物理隔离与精简提示词。"""

import hashlib

from agentscope.app.workspace_manager import LocalWorkspaceManager
from schedule_resources import is_schedule_user

# AgentScope 默认的 LocalWorkspace.instructions 包含大量 git worktree / uv venv / README 等
# Coding Agent 教程，会把数字员工（设计师、架构师、运营等）强行带偏为代码执行 Worker。
# Nexus 中真实工程开发由远端 Agent Worker / OpenCode 承担，数字员工会话只需极简工作区说明。
NEXUS_WORKSPACE_INSTRUCTIONS = (
    "<workspace>你的会话工作目录位于 {workdir}（系统自动管理附件、技能与长期记忆目录）。"
    "仅在确实需要读写个人工作文件且已获文件工具授权时在该目录内操作；"
    "读取会话附件请使用 read_chat_attachment。</workspace>"
)


class ScheduleWorkspaceManager(LocalWorkspaceManager):
    async def get_workspace(self, user_id, agent_id, session_id, workspace_id=None):
        if not workspace_id:
            workspace_id = await self.assign_workspace_id(
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
            )
        # 官方本地实现用 basedir/agent_id 构造目录；PER_SESSION 只隔离缓存 ID。
        # 同时限定物理目录和缓存键，不能借用另一安排者传入的 workspace_id。
        scope = hashlib.sha256(f"{user_id}\n{agent_id}\n{workspace_id}".encode()).hexdigest()
        if is_schedule_user(user_id):
            ws = await super().get_workspace(
                user_id, f"schedules/{scope}", session_id, f"nexus-schedule-{scope}",
            )
        else:
            ws = await super().get_workspace(
                user_id, f"sessions/{scope}", session_id, f"nexus-session-{scope}",
            )
        ws.instructions = NEXUS_WORKSPACE_INSTRUCTIONS.format(workdir=ws.workdir)
        return ws
