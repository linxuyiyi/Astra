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
