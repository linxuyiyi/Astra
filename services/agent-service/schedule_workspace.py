"""固定 2.0.10 本地工作区的日程物理隔离，不改变普通聊天目录。"""

import hashlib

from agentscope.app.workspace_manager import LocalWorkspaceManager
from schedule_resources import is_schedule_user


class ScheduleWorkspaceManager(LocalWorkspaceManager):
    async def get_workspace(self, user_id, agent_id, session_id, workspace_id=None):
        if not is_schedule_user(user_id):
            return await super().get_workspace(user_id, agent_id, session_id, workspace_id)
        if not workspace_id:
            workspace_id = await self.assign_workspace_id(
                user_id=user_id, agent_id=agent_id, session_id=session_id,
            )
        # 官方本地实现用 basedir/agent_id 构造目录；PER_SESSION 只隔离缓存 ID。
        # 同时限定物理目录和缓存键，不能借用另一安排者传入的 workspace_id。
        scope = hashlib.sha256(f"{user_id}\n{agent_id}\n{workspace_id}".encode()).hexdigest()
        return await super().get_workspace(
            user_id, f"schedules/{scope}", session_id, f"nexus-schedule-{scope}",
        )
