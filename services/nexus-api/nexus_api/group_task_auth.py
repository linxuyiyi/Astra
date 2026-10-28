"""Task MCP 对群调用再次鉴权；调用主体来自服务端讨论记录。"""

import json

from sqlalchemy import text

from .db import connect


def authorize_group_task(session_id: str, employee_id: str, channel_id: str, discussion_id: str = "") -> str:
    with connect() as db:
        binding = (
            db.execute(
                text("""SELECT * FROM t_nexus_channel_members
            WHERE agentscope_session_id=:s AND member_type='employee'"""),
                {"s": session_id},
            )
            .mappings()
            .first()
        )
        if not binding:
            private = (
                db.execute(
                    text("SELECT * FROM t_nexus_conversations WHERE agentscope_session_id=:s"),
                    {"s": session_id},
                )
                .mappings()
                .first()
            )
            if channel_id or not private or private["employee_id"] != employee_id:
                raise PermissionError("调用来源不存在")
            employee = (
                db.execute(
                    text("SELECT enabled,skills_json FROM t_nexus_employees WHERE id=:e"), {"e": employee_id}
                )
                .mappings()
                .first()
            )
            if (
                not employee
                or not employee["enabled"]
                or "task-management" not in json.loads(employee["skills_json"])
            ):
                raise PermissionError("员工岗位没有任务能力")
            return private["owner_id"]
        if not binding["membership_active"]:
            raise PermissionError("员工已移出群组")
        if binding["employee_id"] != employee_id or binding["channel_id"] != channel_id:
            raise PermissionError("群会话归属不符")
        discussion = (
            db.execute(
                text("""SELECT d.*,c.lease_until FROM t_nexus_discussions d JOIN t_nexus_channels c
            ON c.id=d.channel_id AND c.generation=d.generation
            WHERE d.channel_id=:c AND d.state='running' AND d.active_employee_id=:e"""),
                {"c": channel_id, "e": employee_id},
            )
            .mappings()
            .first()
        )
        from .db import utcnow

        if not discussion or discussion["id"] != discussion_id or discussion["lease_until"] < utcnow():
            raise PermissionError("群讨论已结束")
        user = (
            db.execute(
                text("""SELECT m.member_id FROM t_nexus_channel_members m
            WHERE m.member_type='human' AND m.membership_active=1
            AND m.channel_id=:c AND m.member_id=:u"""),
                {"c": channel_id, "u": discussion["initiator_id"]},
            )
            .mappings()
            .first()
        )
        employee = (
            db.execute(
                text("SELECT enabled,skills_json FROM t_nexus_employees WHERE id=:e"), {"e": employee_id}
            )
            .mappings()
            .first()
        )
        if not user:
            raise PermissionError("发起者已不在群组")
        if (
            not employee
            or not employee["enabled"]
            or "task-management" not in json.loads(employee["skills_json"])
        ):
            raise PermissionError("员工岗位没有任务能力")
        return user["member_id"]


async def authorize_task_source(
    session_id: str,
    employee_id: str,
    channel_id: str,
    discussion_id: str = "",
    schedule_user_id: str = "",
    schedule_agent_id: str = "",
    conversation_id: str = "",
) -> str:
    """日程没有聊天绑定，向 Nexus 按官方来源重验，不能退回私聊身份。"""
    if not schedule_user_id and not schedule_agent_id:
        return authorize_group_task(session_id, employee_id, channel_id, discussion_id)
    if channel_id or discussion_id or conversation_id or not schedule_user_id or not schedule_agent_id:
        raise PermissionError("日程调用来源无效")
    import httpx

    from app.config.settings import get_settings

    from .auth import internal_headers

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"{get_settings().nexus_api_base.rstrip('/')}/api/v1/internal/schedule-sessions/policy",
                headers=internal_headers(),
                params={"user_id": schedule_user_id, "agent_id": schedule_agent_id, "session_id": session_id},
            )
        response.raise_for_status()
        policy = response.json()
        if (
            not isinstance(policy, dict)
            or policy.get("employee_id") != employee_id
            or not policy.get("task_allowed")
        ):
            raise PermissionError("日程员工没有任务权限")
        return schedule_user_id
    except (httpx.HTTPError, ValueError) as exc:
        raise PermissionError("日程调用来源验证失败") from exc
