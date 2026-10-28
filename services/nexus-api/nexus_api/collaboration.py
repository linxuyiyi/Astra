"""群组公开日志、成员授权与有限发言协调。Agent 推理与记忆仍由 AgentScope 处理。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from .auth import digest, require_internal, require_user
from .db import connect, employee_from_row, new_id, utcnow
from .db_models import group_events
from .sql import upsert_record
from .tool_policy import employee_tool_policy, require_employee_tool

logger = logging.getLogger("nexus.groups")
MAX_TURNS = 6
MAX_SECONDS = 120
MAX_OUTPUT_TOKENS = 768
MAX_CONTEXT_CHARS = 24000
MAX_TOKEN_BUDGET = 16000
LEASE_SECONDS = 30


class UnconfirmedStop(RuntimeError):
    """保留运行身份，等待租约恢复时再次确认上游停止。"""


class TaskEventBody(BaseModel):
    event_id: int = Field(gt=0)


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


def one(db, sql, **params):
    row = db.execute(text(sql), params).mappings().first()
    return dict(row) if row is not None else None


def member(db, channel_id, uid, manage=False):
    row = one(
        db,
        """SELECT * FROM t_nexus_channel_members
        WHERE channel_id=:c AND member_type='human' AND membership_active=1 AND member_id=:u""",
        c=channel_id,
        u=uid,
    )
    if not row:
        raise HTTPException(403, "你不是该群成员")
    if manage and row["role"] not in {"owner", "admin"}:
        raise HTTPException(403, "需要群主或管理员权限")
    return row


def lock_channel(db, channel_id):
    result = db.execute(text("UPDATE t_nexus_channels SET next_seq=next_seq WHERE id=:c"), {"c": channel_id})
    if result.rowcount != 1:
        raise HTTPException(404, "群组不存在")


async def acquire_channel_lock(db, channel_id):
    """同步数据库等待不能阻塞 Task MCP 的异步提交；取消时先等连接操作结束。"""
    pending = asyncio.create_task(asyncio.to_thread(lock_channel, db, channel_id))
    try:
        await asyncio.shield(pending)
    except asyncio.CancelledError:
        await pending
        raise


def emit(db, channel_id, payload):
    result = db.execute(
        group_events.insert().values(channel_id=channel_id, payload_json=dumps(payload), created_at=utcnow())
    )
    return result.inserted_primary_key[0]


def message_payload(row):
    native = json.loads(row["native_json"])
    return {
        "id": row["id"],
        "seq": row["seq"],
        "sender_type": row["sender_type"],
        "sender_id": row["speaker_uid"],
        "sender_name": row["sender_name"],
        "text": row["text"],
        "created_at": row["created_at"],
        "discussion_id": row["discussion_id"],
        "message": native,
    }


def append_message(
    db,
    channel_id,
    sender_type,
    sender_id,
    sender_name,
    body,
    *,
    native=None,
    message_id=None,
    client_id=None,
    discussion_id="",
    attachments=None,
):
    # UPDATE 持有行锁直至消息和事件共同提交；相同时间戳也有唯一稳定顺序。
    db.execute(text("UPDATE t_nexus_channels SET next_seq=next_seq+1 WHERE id=:c"), {"c": channel_id})
    seq = one(db, "SELECT next_seq FROM t_nexus_channels WHERE id=:c", c=channel_id)["next_seq"]
    mid, now = message_id or new_id("msg"), utcnow()
    native = dict(
        native
        or {
            "role": "user" if sender_type == "human" else "assistant",
            "content": [{"type": "text", "text": body}],
        }
    )
    native.update(id=mid, name=sender_name, created_at=now)
    native["metadata"] = {
        **(native.get("metadata") or {}),
        "sender_type": sender_type,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "sequence": seq,
        "discussion_id": discussion_id,
    }
    if sender_type != "employee":
        native["finished_at"] = now
    db.execute(
        text("""INSERT INTO t_nexus_messages
        (id,channel_id,sender,speaker_uid,sender_type,sender_name,text,attachments_json,accepted,
         created_at,seq,native_json,discussion_id,client_id)
        VALUES (:i,:c,:s,:u,:k,:n,:b,:a,1,:t,:q,:j,:d,:x)"""),
        {
            "i": mid,
            "c": channel_id,
            "s": "user" if sender_type == "human" else sender_id or "system",
            "u": sender_id,
            "k": sender_type,
            "n": sender_name,
            "b": body,
            "a": dumps(attachments or []),
            "t": now,
            "q": seq,
            "j": dumps(native),
            "d": discussion_id,
            "x": client_id,
        },
    )
    item = message_payload(one(db, "SELECT * FROM t_nexus_messages WHERE id=:i", i=mid))
    emit(db, channel_id, {"type": "message", "item": item})
    return item


def audit(db, channel_id, actor, action, target="", label=""):
    db.execute(
        text("INSERT INTO t_nexus_group_audit VALUES (:i,:c,:u,:a,:x,:t)"),
        {"i": new_id("audit"), "c": channel_id, "u": actor, "a": action, "x": target, "t": utcnow()},
    )
    if label:
        append_message(db, channel_id, "system", actor, "系统", label)
    emit(db, channel_id, {"type": "members"})


def employees(db, channel_id):
    return [
        employee_from_row(row)
        for row in db.execute(
            text("""SELECT e.* FROM t_nexus_employees e JOIN t_nexus_channel_members m ON
                 m.employee_id=e.id WHERE m.channel_id=:c AND m.member_type='employee'
                 AND membership_active=1 ORDER BY m.member_id"""),
            {"c": channel_id},
        ).mappings()
    ]


def channel_payload(db, channel_id, uid):
    own = member(db, channel_id, uid)
    channel = one(db, "SELECT * FROM t_nexus_channels WHERE id=:c", c=channel_id)
    rows = []
    cutoff = (datetime.now(UTC) - timedelta(seconds=45)).isoformat()
    for row in db.execute(
        text(
            """SELECT * FROM t_nexus_channel_members WHERE channel_id=:c AND membership_active=1
            ORDER BY role,member_id"""
        ),
        {"c": channel_id},
    ).mappings():
        if row["member_type"] == "human":
            source = one(
                db, "SELECT display_name FROM t_nexus_identity_profiles WHERE id=:i", i=row["member_id"]
            )
            name, avatar, online = (
                (source or {}).get("display_name", "未知用户"),
                "👤",
                bool(source and row["last_seen"] > cutoff),
            )
        else:
            source = one(
                db, "SELECT name,avatar,enabled FROM t_nexus_employees WHERE id=:i", i=row["member_id"]
            )
            name, avatar, online = (
                (source or {}).get("name", "未知员工"),
                (source or {}).get("avatar", "🤖"),
                False,
            )
        rows.append(
            {
                "member_id": row["member_id"],
                "member_type": row["member_type"],
                "employee_id": row["employee_id"],
                "role": row["role"],
                "name": name,
                "avatar": avatar,
                "online": online,
                "read_seq": row["read_seq"],
                "agentscope_session_id": row["agentscope_session_id"],
            }
        )
    active = one(
        db,
        """SELECT id,state,turns,detail,active_employee_id FROM t_nexus_discussions WHERE
        channel_id=:c ORDER BY generation DESC LIMIT 1""",
        c=channel_id,
    )
    return {
        "id": channel_id,
        "name": channel["name"],
        "description": channel["description"],
        "members": rows,
        "created_at": channel["created_at"],
        "my_role": own["role"],
        "viewer_id": uid,
        "discussion": active,
    }


def routing_text(body):
    """引用和代码是待分析的数据，不参与选人；保留位置供提及排序。"""
    pattern = (
        r"```[\s\S]*?(?:```|\Z)|~~~[\s\S]*?(?:~~~|\Z)"
        r'|`[^`\n]*(?:`|$)|"(?:\\.|[^"\\\n])*(?:"|$)'
        r"|'(?:\\.|[^'\\\n])*(?:'|$)|“[^”\n]*(?:”|$)|‘[^’\n]*(?:’|$)"
        r"|(?m:^\s*>[^\n]*$)"
    )
    return re.sub(pattern, lambda m: re.sub(r"[^\n]", " ", m.group()), body)


def named_employees(body, staff):
    body = routing_text(body)
    found = []
    for employee in staff:
        match = re.search(r"@" + re.escape(employee["name"]) + r"(?=$|[\s@，,。！？!?：:、])", body)
        if match:
            found.append((match.start(), employee["id"]))
    return [eid for _, eid in sorted(found)]


def select_participants(body, staff, *, human_names=(), human_count=1, previous_employee=""):
    body = routing_text(body)
    enabled = [e for e in staff if e["enabled"] and e["agentscope_agent_id"]]
    explicit = named_employees(body, enabled)
    if explicit:
        return explicit[:MAX_TURNS]
    if "@所有人" in body or "@全体" in body:
        return [e["id"] for e in enabled][:MAX_TURNS]
    if "@" in body or any(
        body.startswith(name + "，") or body.startswith(name + ",") for name in human_names
    ):
        return []
    if re.fullmatch(r"\s*(大家好|你们好|各位好|hello|hi)[！!。~～\s]*", body, re.I):
        return [enabled[0]["id"]] if enabled else []
    # 多人陈述不逐条推理；问题和明确请求才参与岗位匹配。
    request = bool(
        re.search(r"[?？]|如何|进展|怎样|怎么|谁|请|帮忙|帮我|能否|介绍|建议|看看|测试.*(情况|结果)", body)
    )
    if human_count > 1 and not request:
        return []
    scored = []
    stop = {"工程", "员工", "负责", "工作", "问题", "如何", "进展", "一下"}
    tokens = set(re.findall(r"[a-zA-Z][a-zA-Z0-9_-]+", body.lower()))
    chinese = re.findall(r"[\u4e00-\u9fff]+", body)
    tokens.update(x[i : i + 2] for x in chinese for i in range(len(x) - 1) if x[i : i + 2] not in stop)
    for employee in enabled:
        scope = (employee["name"] + employee["domain"] + employee["description"]).lower()
        score = sum(1 for token in tokens if token in scope)
        if employee["name"] in body:
            score += 10
        if score:
            scored.append((score, employee["id"]))
    if scored and (request or human_count == 1):
        return [sorted(scored, key=lambda x: (-x[0], x[1]))[0][1]]
    if request and previous_employee and human_count == 1:
        return [previous_employee] if previous_employee in {e["id"] for e in enabled} else []
    return []


def followup(body, staff, speaker_id, counts):
    body = routing_text(body)
    # 只让被点名并被提问的同事继续；泛化问候/致谢不产生轮次。
    if not re.search(r"[?？]|请.*(回答|介绍|说明|补充)|怎么看|呢[。\s]*$", body):
        return []
    result = []
    for e in staff:
        if e["id"] != speaker_id and e["enabled"] and e["name"] in body and counts[e["id"]] < 2:
            result.append(e["id"])
    return result[:1]


class GroupBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=32)
    description: str = Field(default="", max_length=2000)
    member_ids: list[str] = Field(default_factory=list, max_length=100)


class SendBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(default="", max_length=64000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=6)
    client_id: str = Field(min_length=8, max_length=64, pattern=r"^[\w-]+$")


class JoinBody(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class RoleBody(BaseModel):
    role: str = Field(pattern="^(admin|member)$")


class ReadBody(BaseModel):
    seq: int = Field(ge=0)


class Collaboration:
    def __init__(
        self,
        app,
        *,
        load_employee,
        claim_session,
        sync_resources,
        model_for,
        load_attachments,
        model_input_blocks,
    ):
        self.app = app
        self.load_employee = load_employee
        self.claim_session = claim_session
        self.sync_resources = sync_resources
        self.model_for = model_for
        self.load_attachments = load_attachments
        self.model_input_blocks = model_input_blocks
        self.running = {}

    def install(self):
        app = self.app

        @app.get("/api/v1/channels")
        def list_channels(request: Request):
            uid = require_user(request)["uid"]
            with connect() as db:
                ids = (
                    db.execute(
                        text(
                            """SELECT channel_id FROM t_nexus_channel_members WHERE member_type='human' AND
                            membership_active=1 AND member_id=:u"""
                        ),
                        {"u": uid},
                    )
                    .scalars()
                    .all()
                )
                return {"items": [channel_payload(db, c, uid) for c in ids]}

        @app.post("/api/v1/channels", status_code=201)
        def create_channel(body: GroupBody, request: Request):
            user = require_user(request)
            with connect() as db:
                staff = [self.load_employee(db, e) for e in dict.fromkeys(body.member_ids)]
                cid = new_id("ch")
                db.execute(
                    text(
                        """INSERT INTO t_nexus_channels (id,name,description,created_at,updated_at) VALUES
                        (:i,:n,:d,:t,:t)"""
                    ),
                    {"i": cid, "n": body.name.strip(), "d": body.description.strip(), "t": utcnow()},
                )
                db.execute(
                    text(
                        """INSERT INTO t_nexus_channel_members (channel_id,member_type,member_id,role) VALUES
                        (:c,'human',:u,'owner')"""
                    ),
                    {"c": cid, "u": user["uid"]},
                )
                for e in staff:
                    db.execute(
                        text(
                            """INSERT INTO t_nexus_channel_members
                            (channel_id,member_type,member_id,employee_id) VALUES (:c,'employee',:i,:i)"""
                        ),
                        {"c": cid, "i": e["id"]},
                    )
                audit(db, cid, user["uid"], "create", label=f"{user['display_name']} 创建了群组")
                db.commit()
                return channel_payload(db, cid, user["uid"])

        @app.get("/api/v1/channels/unclaimed")
        def unclaimed(request: Request):
            require_user(request)
            with connect() as db:
                return {
                    "items": [
                        dict(r)
                        for r in db.execute(
                            text("""SELECT id,name FROM t_nexus_channels c WHERE NOT EXISTS (SELECT 1 FROM
                            t_nexus_channel_members m WHERE m.channel_id=c.id AND
                            m.member_type='human' AND membership_active=1)""")
                        ).mappings()
                    ]
                }

        @app.post("/api/v1/channels/{channel_id}/claim")
        def claim(channel_id: str, request: Request):
            user = require_user(request)
            with connect() as db:
                lock_channel(db, channel_id)
                if one(
                    db,
                    """SELECT 1 FROM t_nexus_channel_members WHERE channel_id=:c AND member_type='human' AND
                    membership_active=1""",
                    c=channel_id,
                ):
                    raise HTTPException(409, "群组已有可信成员")
                db.execute(
                    text(
                        """INSERT INTO t_nexus_channel_members (channel_id,member_type,member_id,role) VALUES
                        (:c,'human',:u,'owner')"""
                    ),
                    {"c": channel_id, "u": user["uid"]},
                )
                audit(db, channel_id, user["uid"], "claim", label=f"{user['display_name']} 认领了未分配群组")
                db.commit()
                return channel_payload(db, channel_id, user["uid"])

        @app.patch("/api/v1/channels/{channel_id}")
        def update_channel(channel_id: str, body: GroupBody, request: Request):
            user = require_user(request)
            with connect() as db:
                lock_channel(db, channel_id)
                member(db, channel_id, user["uid"], True)
                staff = {e: self.load_employee(db, e) for e in dict.fromkeys(body.member_ids)}
                current = {e["id"] for e in employees(db, channel_id)}
                for eid in current - staff.keys():
                    db.execute(
                        text(
                            """UPDATE t_nexus_channel_members SET membership_active=0 WHERE channel_id=:c AND
                            member_type='employee' AND membership_active=1 AND member_id=:e"""
                        ),
                        {"c": channel_id, "e": eid},
                    )
                for eid in staff.keys() - current:
                    upsert_record(
                        db,
                        "t_nexus_channel_members",
                        {
                            "channel_id": channel_id,
                            "member_type": "employee",
                            "member_id": eid,
                            "employee_id": eid,
                            "membership_active": 1,
                        },
                        keys=["channel_id", "member_type", "member_id"],
                        update=["membership_active"],
                    )

                db.execute(
                    text(
                        """UPDATE t_nexus_channels SET
                        name=:n,description=:d,updated_at=:t,generation=generation+1 WHERE id=:c"""
                    ),
                    {"c": channel_id, "n": body.name.strip(), "d": body.description.strip(), "t": utcnow()},
                )
                audit(
                    db,
                    channel_id,
                    user["uid"],
                    "update",
                    label=f"{user['display_name']} 更新了群组与员工成员",
                )
                db.commit()
                return channel_payload(db, channel_id, user["uid"])

        @app.get("/api/v1/channels/{channel_id}/members")
        def list_members(channel_id: str, request: Request):
            with connect() as db:
                return channel_payload(db, channel_id, require_user(request)["uid"])

        @app.post("/api/v1/channels/{channel_id}/invitations")
        def invite(channel_id: str, request: Request):
            import secrets

            user = require_user(request)
            with connect() as db:
                lock_channel(db, channel_id)
                member(db, channel_id, user["uid"], True)
                token = secrets.token_urlsafe(32)
                db.execute(
                    text(
                        """INSERT INTO t_nexus_group_invites (token_hash,channel_id,created_by,expires_at)
                        VALUES (:t,:c,:u,:e)"""
                    ),
                    {
                        "t": digest(token),
                        "c": channel_id,
                        "u": user["uid"],
                        "e": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                    },
                )
                audit(db, channel_id, user["uid"], "invite")
                db.commit()
                return {"token": token, "expires_in": 86400}

        @app.post("/api/v1/channels/join")
        def join(body: JoinBody, request: Request):
            user = require_user(request)
            with connect() as db:
                invitation = one(
                    db, "SELECT * FROM t_nexus_group_invites WHERE token_hash=:t", t=digest(body.token)
                )
                if not invitation:
                    raise HTTPException(404, "邀请不存在")
                cid = invitation["channel_id"]
                db.rollback()
                lock_channel(db, cid)
                invitation = one(
                    db, "SELECT * FROM t_nexus_group_invites WHERE token_hash=:t", t=digest(body.token)
                )
                if invitation["expires_at"] < utcnow() or invitation["used_by"]:
                    raise HTTPException(410, "邀请已过期或已使用")
                # 邀请发起人被撤权后，未使用链接也不再授予访问。
                member(db, cid, invitation["created_by"], True)
                if not one(
                    db,
                    """SELECT 1 FROM t_nexus_channel_members WHERE channel_id=:c AND member_type='human' AND
                    membership_active=1 AND member_id=:u""",
                    c=cid,
                    u=user["uid"],
                ):
                    upsert_record(
                        db,
                        "t_nexus_channel_members",
                        {
                            "channel_id": cid,
                            "member_type": "human",
                            "member_id": user["uid"],
                            "membership_active": 1,
                            "role": "member",
                            "last_seen": "",
                            "read_seq": 0,
                        },
                        keys=["channel_id", "member_type", "member_id"],
                        update=["membership_active", "role", "last_seen", "read_seq"],
                    )

                    audit(db, cid, user["uid"], "join", label=f"{user['display_name']} 加入了群组")
                db.execute(
                    text("UPDATE t_nexus_group_invites SET used_by=:u WHERE token_hash=:t"),
                    {"u": user["uid"], "t": digest(body.token)},
                )
                db.commit()
                return channel_payload(db, cid, user["uid"])

        @app.post("/api/v1/channels/{channel_id}/leave")
        def leave(channel_id: str, request: Request):
            return remove_human(channel_id, require_user(request)["uid"], request, True)

        def remove_human(cid, uid, request, leaving=False):
            user = require_user(request)
            with connect() as db:
                lock_channel(db, cid)
                own = member(db, cid, user["uid"], not leaving)
                target = member(db, cid, uid)
                if target["role"] == "owner":
                    raise HTTPException(409, "群主须先转让群组")
                if not leaving and target["role"] == "admin" and own["role"] != "owner":
                    raise HTTPException(403, "只有群主可移除管理员")
                name = one(db, "SELECT display_name FROM t_nexus_identity_profiles WHERE id=:u", u=uid)[
                    "display_name"
                ]
                db.execute(
                    text(
                        """UPDATE t_nexus_channel_members SET membership_active=0 WHERE channel_id=:c AND
                        member_type='human' AND membership_active=1 AND member_id=:u"""
                    ),
                    {"c": cid, "u": uid},
                )
                db.execute(
                    text("UPDATE t_nexus_channels SET generation=generation+1 WHERE id=:c"), {"c": cid}
                )
                audit(
                    db,
                    cid,
                    user["uid"],
                    "leave" if leaving else "remove",
                    uid,
                    f"{name} {'退出了' if leaving else '被移出了'}群组",
                )
                db.commit()
                return {"ok": True}

        @app.delete("/api/v1/channels/{channel_id}/humans/{user_id}")
        def remove(channel_id: str, user_id: str, request: Request):
            return remove_human(channel_id, user_id, request)

        @app.patch("/api/v1/channels/{channel_id}/humans/{user_id}")
        def role(channel_id: str, user_id: str, body: RoleBody, request: Request):
            user = require_user(request)
            with connect() as db:
                lock_channel(db, channel_id)
                own = member(db, channel_id, user["uid"], True)
                target = member(db, channel_id, user_id)
                if own["role"] != "owner" or target["role"] == "owner":
                    raise HTTPException(403, "只有群主可调整其他成员角色")
                db.execute(
                    text(
                        """UPDATE t_nexus_channel_members SET role=:r WHERE channel_id=:c AND
                        member_type='human' AND membership_active=1 AND member_id=:u"""
                    ),
                    {"r": body.role, "c": channel_id, "u": user_id},
                )
                audit(db, channel_id, user["uid"], "role", user_id, "群成员角色已更新")
                db.commit()
                return channel_payload(db, channel_id, user["uid"])

        @app.post("/api/v1/channels/{channel_id}/transfer/{user_id}")
        def transfer(channel_id: str, user_id: str, request: Request):
            uid = require_user(request)["uid"]
            with connect() as db:
                lock_channel(db, channel_id)
                if member(db, channel_id, uid)["role"] != "owner" or uid == user_id:
                    raise HTTPException(403, "只有群主可转让给其他人类成员")
                member(db, channel_id, user_id)
                db.execute(
                    text(
                        """UPDATE t_nexus_channel_members SET role='member' WHERE channel_id=:c AND
                        member_type='human' AND membership_active=1 AND member_id=:u"""
                    ),
                    {"c": channel_id, "u": uid},
                )
                db.execute(
                    text(
                        """UPDATE t_nexus_channel_members SET role='owner' WHERE channel_id=:c AND
                        member_type='human' AND membership_active=1 AND member_id=:u"""
                    ),
                    {"c": channel_id, "u": user_id},
                )
                audit(db, channel_id, uid, "transfer", user_id, "群主已转让")
                db.commit()
                return {"ok": True}

        @app.post("/api/v1/channels/{channel_id}/read")
        def read(channel_id: str, body: ReadBody, request: Request):
            uid = require_user(request)["uid"]
            with connect() as db:
                member(db, channel_id, uid)
                bound = one(db, "SELECT next_seq FROM t_nexus_channels WHERE id=:c", c=channel_id)["next_seq"]
                db.execute(
                    text("""UPDATE t_nexus_channel_members SET read_seq=CASE WHEN read_seq<:q THEN
                         :q ELSE read_seq END,last_seen=:t WHERE channel_id=:c AND
                         member_type='human' AND membership_active=1 AND member_id=:u"""),
                    {"q": min(body.seq, bound), "t": utcnow(), "c": channel_id, "u": uid},
                )
                db.commit()
                return {"ok": True}

        @app.get("/api/v1/channels/{channel_id}/messages")
        @app.get("/api/v1/channels/{channel_id}/timeline/native")
        @app.get("/api/v1/channels/{channel_id}/timeline")
        def history(channel_id: str, request: Request, before: int = 0, limit: int = 50):
            if not 1 <= limit <= 100 or before < 0:
                raise HTTPException(422, "分页参数无效")
            uid = require_user(request)["uid"]
            with connect() as db:
                cursor = (
                    one(
                        db,
                        "SELECT MAX(id) AS cursor FROM t_nexus_group_events WHERE channel_id=:c",
                        c=channel_id,
                    )["cursor"]
                    or 0
                )
                channel = channel_payload(db, channel_id, uid)
                rows = (
                    db.execute(
                        text("""SELECT * FROM t_nexus_messages WHERE channel_id=:c
                    AND (:b=0 OR seq<:b) ORDER BY seq DESC LIMIT :l"""),
                        {"c": channel_id, "b": before, "l": limit + 1},
                    )
                    .mappings()
                    .all()
                )
                more = len(rows) > limit
                items = [message_payload(row) for row in reversed(rows[:limit])]

                return {
                    "channel": channel,
                    "items": items,
                    "before": items[0]["seq"] if more else 0,
                    "event_cursor": cursor,
                }

        @app.post("/api/v1/channels/{channel_id}/messages")
        def send(channel_id: str, body: SendBody, request: Request):
            user = require_user(request)
            with connect() as db:
                lock_channel(db, channel_id)
                member(db, channel_id, user["uid"])
                duplicate = one(
                    db,
                    "SELECT * FROM t_nexus_messages WHERE channel_id=:c AND speaker_uid=:u AND client_id=:x",
                    c=channel_id,
                    u=user["uid"],
                    x=body.client_id,
                )
                if duplicate:
                    original = message_payload(duplicate)
                    if (
                        original["text"] != body.text
                        or json.loads(duplicate["attachments_json"]) != body.attachment_ids
                    ):
                        raise HTTPException(409, "同一幂等键不能用于不同内容")
                    return {"message": original, "duplicate": True}
                if not body.text.strip() and not body.attachment_ids:
                    raise HTTPException(422, "消息不能为空")
                attachments = self.load_attachments(db, body.attachment_ids, user["uid"])
                staff = employees(db, channel_id)
                humans = (
                    db.execute(
                        text("""SELECT u.display_name FROM t_nexus_channel_members m JOIN
                            t_nexus_identity_profiles u ON u.id=m.member_id WHERE m.channel_id=:c AND
                            m.member_type='human' AND membership_active=1"""),
                        {"c": channel_id},
                    )
                    .scalars()
                    .all()
                )
                last = one(
                    db,
                    """SELECT speaker_uid FROM t_nexus_messages WHERE channel_id=:c AND
                    sender_type='employee' ORDER BY seq DESC LIMIT 1""",
                    c=channel_id,
                )
                participants = select_participants(
                    body.text,
                    staff,
                    human_names=humans,
                    human_count=len(humans),
                    previous_employee=(last or {}).get("speaker_uid", ""),
                )
                generation = (
                    one(db, "SELECT next_seq FROM t_nexus_channels WHERE id=:c", c=channel_id)["next_seq"] + 1
                )
                active = one(
                    db,
                    "SELECT initiator_id FROM t_nexus_discussions WHERE channel_id=:c AND state='running'",
                    c=channel_id,
                )
                adjust_all = bool(re.search(r"停止讨论|先别|不用.*(讨论|回答)|调整讨论|改成", body.text))
                if adjust_all or (active and active["initiator_id"] == user["uid"]):
                    db.execute(
                        text("UPDATE t_nexus_channels SET generation=:g WHERE id=:c"),
                        {"g": generation, "c": channel_id},
                    )
                db.execute(
                    text("""UPDATE t_nexus_discussions SET state='superseded'
                    WHERE channel_id=:c AND state IN ('queued','running') AND (initiator_id=:u OR :all=1)"""),
                    {"c": channel_id, "u": user["uid"], "all": int(adjust_all)},
                )
                did = new_id("dis") if participants else ""
                content = [{"type": "text", "text": body.text}]
                for a in attachments:
                    content.append(
                        {
                            "type": "data",
                            "name": a["name"],
                            "source": {
                                "type": "url",
                                "media_type": a["type"],
                                "url": f"/api/v1/attachments/{a['id']}",
                            },
                        }
                    )
                message = append_message(
                    db,
                    channel_id,
                    "human",
                    user["uid"],
                    user["display_name"],
                    body.text,
                    native={"role": "user", "content": content},
                    client_id=body.client_id,
                    discussion_id=did,
                    attachments=body.attachment_ids,
                )
                if participants:
                    db.execute(
                        text("""INSERT INTO t_nexus_discussions
                            (id,channel_id,message_id,initiator_id,generation,state,participants_json,created_at)
                            VALUES (:i,:c,:m,:u,:g,'queued',:p,:t)"""),
                        {
                            "i": did,
                            "c": channel_id,
                            "m": message["id"],
                            "u": user["uid"],
                            "g": generation,
                            "p": dumps(participants),
                            "t": utcnow(),
                        },
                    )
                    emit(db, channel_id, {"type": "discussion", "id": did, "state": "queued"})
                db.commit()
            return {
                "message": message,
                "discussion_id": did,
                "participants": participants,
                "duplicate": False,
            }

        @app.post("/api/v1/channels/{channel_id}/interrupt")
        def interrupt(channel_id: str, request: Request):
            uid = require_user(request)["uid"]
            with connect() as db:
                lock_channel(db, channel_id)
                member(db, channel_id, uid)
                db.execute(
                    text("UPDATE t_nexus_channels SET generation=generation+1 WHERE id=:c"), {"c": channel_id}
                )
                db.execute(
                    text(
                        """UPDATE t_nexus_discussions SET state='superseded' WHERE channel_id=:c AND state IN
                        ('queued','running')"""
                    ),
                    {"c": channel_id},
                )
                audit(db, channel_id, uid, "interrupt")
                db.commit()
                return {"accepted": True}

        @app.get("/api/v1/channels/{channel_id}/events")
        async def events(channel_id: str, request: Request, after: int = 0):
            uid = require_user(request)["uid"]
            with connect() as db:
                member(db, channel_id, uid)

            async def stream():
                cursor = after
                heartbeat = 0
                while not await request.is_disconnected():
                    try:
                        # 成员退出同时作用于既有 SSE。
                        with connect() as db:
                            member(db, channel_id, uid)
                            rows = (
                                db.execute(
                                    text(
                                        """SELECT * FROM t_nexus_group_events
                                        WHERE channel_id=:c AND id>:q ORDER BY id LIMIT 100"""
                                    ),
                                    {"c": channel_id, "q": cursor},
                                )
                                .mappings()
                                .all()
                            )
                            if heartbeat % 50 == 0:
                                await acquire_channel_lock(db, channel_id)
                                member(db, channel_id, uid)
                                db.execute(
                                    text(
                                        """UPDATE t_nexus_channel_members SET last_seen=:t
                                        WHERE channel_id=:c AND member_type='human'
                                        AND membership_active=1 AND member_id=:u"""
                                    ),
                                    {"t": utcnow(), "c": channel_id, "u": uid},
                                )
                                db.commit()
                        for row in rows:
                            cursor = row["id"]
                            yield f"id: {cursor}\ndata: {row['payload_json']}\n\n"
                        if heartbeat % 50 == 0:
                            yield ": heartbeat\n\n"
                        heartbeat += 1
                        await asyncio.sleep(0.2)
                    except HTTPException:
                        yield 'event: revoked\ndata: {"type":"revoked"}\n\n'
                        return

            return StreamingResponse(
                stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        @app.get("/api/v1/internal/group-policy")
        def policy(
            request: Request, session_id: str, tool_name: str = "", execution_id: str = "", mcp_ref: str = ""
        ):
            require_internal(request)
            return self.policy(session_id, tool_name, execution_id, mcp_ref)

        @app.post("/api/v1/internal/group-task-event")
        async def task_event(body: TaskEventBody, request: Request):
            require_internal(request)
            return await asyncio.to_thread(self.publish_task_event, body.event_id)

    def publish_task_event(self, event_id: int):
        """投影可信 Task 事件；求助在原发起者仍有权限时排入新的责任员工讨论。"""
        with connect() as db:
            source = one(
                db,
                """SELECT e.message,e.created_at,t.id,t.title,t.source_channel_id,
                t.source_session_id,t.responsible_employee_id,t.status,t.waiting_reason
                FROM t_nexus_events e
                JOIN t_nexus_tasks t ON t.id=e.task_id
                JOIN t_nexus_task_wake_outbox o ON o.event_id=e.id AND o.task_id=t.id WHERE e.id=:i""",
                i=event_id,
            )
            if not source or not source["source_channel_id"]:
                raise HTTPException(404, "事件没有来源群")
            cid = source["source_channel_id"]
            lock_channel(db, cid)
            binding = one(
                db,
                """SELECT 1 FROM t_nexus_channel_members WHERE channel_id=:c
                AND employee_id=:e AND agentscope_session_id=:s AND member_type='employee'
                AND membership_active=1""",
                c=cid,
                e=source["responsible_employee_id"],
                s=source["source_session_id"],
            )
            if not binding:
                raise HTTPException(403, "任务来源与员工群会话不一致")
            mid = f"task_event_{event_id}"
            if one(db, "SELECT id FROM t_nexus_messages WHERE id=:i", i=mid):
                return {"ok": True, "duplicate": True}
            body = f"任务《{source['title'] or source['id']}》：{source['message']}"
            needs_assistance = source["status"] == "WAITING" and source["waiting_reason"] in {
                "HITL_QUESTION", "HITL_PERMISSION", "EXECUTION_RESULT_REVIEW",
            }
            did, initiator = "", ""
            if needs_assistance:
                origin = one(
                    db, """SELECT detail FROM t_nexus_events WHERE task_id=:t
                    AND event_type='group_task_requested' ORDER BY id LIMIT 1""", t=source["id"],
                )
                detail = json.loads(origin["detail"]) if origin and isinstance(origin["detail"], str) else (
                    origin["detail"] if origin else {}
                )
                initiator = (detail or {}).get("initiator_id", "")
                authorized = one(
                    db, """SELECT 1 FROM t_nexus_channel_members WHERE channel_id=:c
                    AND member_type='human' AND membership_active=1 AND member_id=:u""",
                    c=cid, u=initiator,
                )
                employee = self.load_employee(db, source["responsible_employee_id"])
                if authorized and employee["enabled"] and "task-management" in employee["skills"]:
                    did = new_id("dis")
                    body += "\n责任员工将先核实并处理，需要补充信息时会在群内提问。"
            message = append_message(
                db,
                cid,
                "system",
                "",
                "任务通知",
                body,
                message_id=mid,
                discussion_id=did,
                native={
                    "role": "user",
                    "content": [{"type": "text", "text": body}],
                    "metadata": {"task_id": source["id"], "task_event_id": event_id},
                },
            )
            if did:
                generation = one(db, "SELECT next_seq FROM t_nexus_channels WHERE id=:c", c=cid)["next_seq"]
                db.execute(
                    text("""INSERT INTO t_nexus_discussions
                    (id,channel_id,message_id,initiator_id,generation,state,participants_json,created_at)
                    VALUES (:i,:c,:m,:u,:g,'queued',:p,:t)"""),
                    {"i": did, "c": cid, "m": message["id"], "u": initiator, "g": generation,
                     "p": dumps([source["responsible_employee_id"]]), "t": utcnow()},
                )
                emit(db, cid, {"type": "discussion", "id": did, "state": "queued"})
            db.commit()
            return {"ok": True, "duplicate": False, "assistance_scheduled": bool(did)}

    def policy(self, session_id, tool_name="", execution_id="", mcp_ref=""):
        with connect() as db:
            binding = one(
                db,
                """SELECT * FROM t_nexus_channel_members WHERE agentscope_session_id=:s AND
                member_type='employee'""",
                s=session_id,
            )
            if binding:
                if not binding["membership_active"]:
                    raise HTTPException(403, "员工已移出群组")
                discussion = one(
                    db,
                    """SELECT d.*,c.lease_until FROM t_nexus_discussions d JOIN t_nexus_channels c
                    ON c.id=d.channel_id AND c.generation=d.generation
                    WHERE d.channel_id=:c AND d.state='running' AND d.active_employee_id=:e""",
                    c=binding["channel_id"],
                    e=binding["employee_id"],
                )
                if not discussion or discussion["lease_until"] < utcnow():
                    raise HTTPException(403, "群讨论已结束、失去租约或被中断")
                if (tool_name or execution_id) and execution_id != discussion["id"]:
                    raise HTTPException(403, "旧讨论不能借用新讨论的权限")
                member(db, binding["channel_id"], discussion["initiator_id"])
                initiator_id = discussion["initiator_id"]
            else:
                private = one(
                    db, "SELECT * FROM t_nexus_conversations WHERE agentscope_session_id=:s", s=session_id
                )
                if not private:
                    return {"managed": False}
                binding = {"employee_id": private["employee_id"], "channel_id": ""}
                initiator_id = private["owner_id"]
                discussion = {"id": "private:" + private["id"], "generation": 0}
            employee = self.load_employee(db, binding["employee_id"])
            tools_policy = employee_tool_policy(db, employee)
            require_employee_tool(tools_policy, tool_name, mcp_ref)
            if tool_name:
                db.execute(
                    text("INSERT INTO t_nexus_group_audit VALUES (:i,:c,:u,:a,:x,:t)"),
                    {
                        "i": new_id("audit"),
                        "c": binding["channel_id"],
                        "u": initiator_id,
                        "a": f"tool:{tool_name}"[:64],
                        "x": employee["id"],
                        "t": utcnow(),
                    },
                )
                db.commit()
            return {
                "managed": True,
                **tools_policy,
                "initiator_id": initiator_id,
                "discussion_id": discussion["id"],
                "generation": discussion["generation"],
            }

    def request_peer(self, session_id, name, body, execution_id=""):
        policy = self.policy(session_id, "contact_employee", execution_id)
        if not policy.get("managed"):
            raise HTTPException(404, "未找到群会话")
        with connect() as db:
            binding = one(
                db,
                """SELECT * FROM t_nexus_channel_members WHERE agentscope_session_id=:s AND
                member_type='employee' AND membership_active=1""",
                s=session_id,
            )
            db.rollback()
            lock_channel(db, binding["channel_id"])
            # 行锁内重验 generation，避免新消息到来后排入旧讨论。
            current = one(db, "SELECT generation FROM t_nexus_channels WHERE id=:c", c=binding["channel_id"])
            if current["generation"] != policy["generation"]:
                raise HTTPException(409, "讨论已被新消息中断")
            staff = employees(db, binding["channel_id"])
            target = next(
                (
                    e
                    for e in staff
                    if e["name"] == name and e["id"] != binding["employee_id"] and e["enabled"]
                ),
                None,
            )
            if not target:
                raise HTTPException(404, "对方不是可用群员工")
            d = one(db, "SELECT * FROM t_nexus_discussions WHERE id=:i", i=policy["discussion_id"])
            scheduled = json.loads(d["participants_json"])
            if len(scheduled) >= MAX_TURNS or scheduled.count(target["id"]) >= 2:
                raise HTTPException(409, "同事交流已达到轮次上限")
            scheduled.append(target["id"])
            sender = self.load_employee(db, binding["employee_id"])
            append_message(
                db,
                binding["channel_id"],
                "employee",
                sender["id"],
                sender["name"],
                body[:8000],
                native={
                    "role": "assistant",
                    "content": [{"type": "text", "text": body[:8000]}],
                    "finished_at": utcnow(),
                },
                discussion_id=d["id"],
            )
            db.execute(
                text("UPDATE t_nexus_discussions SET participants_json=:p WHERE id=:i"),
                {"p": dumps(scheduled), "i": d["id"]},
            )
            db.commit()
            return {"scheduled": True, "sender_name": sender["name"]}

    async def supervisor(self):
        while True:
            try:
                with connect() as db:
                    ids = (
                        db.execute(
                            text(
                                """SELECT DISTINCT channel_id FROM t_nexus_discussions WHERE state IN
                                ('queued','running','superseded')"""
                            )
                        )
                        .scalars()
                        .all()
                    )
                for cid in ids:
                    if cid not in self.running or self.running[cid].done():
                        self.running[cid] = asyncio.create_task(self.run_channel(cid))
            except Exception:
                logger.exception("群讨论调度失败")
            await asyncio.sleep(0.3)

    async def close(self):
        for task in self.running.values():
            task.cancel()
        await asyncio.gather(*self.running.values(), return_exceptions=True)

    def renew(self, cid, token):
        with connect() as db:
            result = db.execute(
                text("UPDATE t_nexus_channels SET lease_until=:e WHERE id=:c AND lease_token=:t"),
                {
                    "e": (datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)).isoformat(),
                    "c": cid,
                    "t": token,
                },
            )
            db.commit()
            return result.rowcount == 1

    def current(self, did):
        with connect() as db:
            row = one(
                db,
                """SELECT d.*,c.generation AS current_generation FROM t_nexus_discussions d
                JOIN t_nexus_channels c ON c.id=d.channel_id WHERE d.id=:i""",
                i=did,
            )
            if row:
                row["valid"] = row["generation"] == row["current_generation"] and row["state"] == "running"
            return row

    def finish(self, did, state, detail=""):
        with connect() as lookup:
            cid = one(lookup, "SELECT channel_id FROM t_nexus_discussions WHERE id=:i", i=did)["channel_id"]
        with connect() as db:
            lock_channel(db, cid)
            d = one(db, "SELECT * FROM t_nexus_discussions WHERE id=:i", i=did)
            db.execute(
                text("UPDATE t_nexus_discussions SET state=:s,detail=:x,active_employee_id='' WHERE id=:i"),
                {"i": did, "s": state, "x": detail},
            )
            emit(db, d["channel_id"], {"type": "discussion", "id": did, "state": state, "detail": detail})
            db.commit()

    async def stop_session(self, employee, session_id):
        client = self.app.state.agentscope
        await client.interrupt_session(employee["agentscope_agent_id"], session_id)
        # 确认上游停止再交给下一讨论，禁止仅凭本地 cancel 释放并发隔离。
        for _ in range(50):
            _, _, running = await client.read_session_detail(employee["agentscope_agent_id"], session_id)
            if not running:
                return
            await asyncio.sleep(0.2)
        raise TimeoutError("AgentScope 未确认中断")

    async def run_channel(self, cid):
        token = new_id("lease")
        with connect() as db:
            await acquire_channel_lock(db, cid)
            result = db.execute(
                text(
                    """UPDATE t_nexus_channels SET lease_token=:t,lease_until=:e WHERE id=:c AND
                    lease_until<:now"""
                ),
                {
                    "t": token,
                    "e": (datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)).isoformat(),
                    "c": cid,
                    "now": utcnow(),
                },
            )
            if result.rowcount:
                db.execute(
                    text(
                        "UPDATE t_nexus_discussions SET state='superseded' "
                        "WHERE channel_id=:c AND state='running'"
                    ),
                    {"c": cid},
                )
            db.commit()
            if not result.rowcount:
                return
        release = True
        main_task = asyncio.current_task()

        async def heartbeat():
            while True:
                await asyncio.sleep(5)
                if not await asyncio.to_thread(self.renew, cid, token):
                    main_task.cancel()
                    return

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            # 过期租约下的旧运行不重放：先中断关联 Session，再将状态落定。
            with connect() as db:
                stale = [
                    dict(r)
                    for r in db.execute(
                        text(
                            """SELECT * FROM t_nexus_discussions WHERE channel_id=:c AND state IN
                            ('running','superseded')"""
                        ),
                        {"c": cid},
                    ).mappings()
                ]
            for old in stale:
                if old["active_employee_id"]:
                    with connect() as db:
                        e = self.load_employee(db, old["active_employee_id"])
                        m = one(
                            db,
                            """SELECT agentscope_session_id FROM t_nexus_channel_members
                            WHERE channel_id=:c AND employee_id=:e""",
                            c=cid,
                            e=e["id"],
                        )
                    if m and m["agentscope_session_id"]:
                        try:
                            await self.stop_session(e, m["agentscope_session_id"])
                        except TimeoutError:
                            # 未确认旧运行停止时保留租约，等过期后再恢复，避免双执行。
                            logger.warning("旧群讨论停止未确认，保留租约等待恢复")
                            release = False
                            return
                await asyncio.to_thread(self.finish, old["id"], "interrupted", "旧讨论已中断")
            while True:
                with connect() as db:
                    await acquire_channel_lock(db, cid)
                    d = one(
                        db,
                        """SELECT d.* FROM t_nexus_discussions d JOIN t_nexus_channels c ON
                        c.id=d.channel_id WHERE d.channel_id=:c
                        AND d.state='queued' ORDER BY generation LIMIT 1""",
                        c=cid,
                    )
                    if not d:
                        break
                    db.execute(
                        text("UPDATE t_nexus_channels SET generation=:g WHERE id=:c"),
                        {"g": d["generation"], "c": cid},
                    )
                    db.execute(
                        text("UPDATE t_nexus_discussions SET state='running' WHERE id=:i"), {"i": d["id"]}
                    )
                    emit(db, cid, {"type": "discussion", "id": d["id"], "state": "running"})
                    db.commit()
                try:
                    await self.discuss(d, token)
                except UnconfirmedStop:
                    release = False
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("群讨论失败: %s", type(exc).__name__)
                    await asyncio.to_thread(self.finish, d["id"], "failed", str(exc)[:300])
                    # 未确认上游停止时保留租约，等待过期恢复，避免双执行。
                    return
        except BaseException:
            release = False
            raise
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            if release:
                with connect() as db:
                    await acquire_channel_lock(db, cid)
                    db.execute(
                        text(
                            """UPDATE t_nexus_channels SET lease_token='',lease_until='' WHERE id=:c AND
                            lease_token=:t"""
                        ),
                        {"c": cid, "t": token},
                    )
                    db.commit()

    async def discuss(self, d, token):
        counts = Counter()
        queue = json.loads(d["participants_json"])
        scheduled_count = len(queue)
        started = asyncio.get_running_loop().time()
        total_tokens = 0
        generated_tokens = 0
        for turn in range(MAX_TURNS):
            if not queue:
                await asyncio.to_thread(self.finish, d["id"], "completed")
                return
            if not self.current(d["id"])["valid"]:
                await asyncio.to_thread(self.finish, d["id"], "interrupted")
                return
            if (
                asyncio.get_running_loop().time() - started > MAX_SECONDS
                or generated_tokens >= MAX_TOKEN_BUDGET
            ):
                await asyncio.to_thread(self.finish, d["id"], "limited", "讨论已达到时间或 Token 上限")
                return
            eid = queue.pop(0)
            if counts[eid] >= 2:
                continue
            with connect() as db:
                await acquire_channel_lock(db, d["channel_id"])
                member(db, d["channel_id"], d["initiator_id"])
                staff = employees(db, d["channel_id"])
                e = next((e for e in staff if e["id"] == eid and e["enabled"]), None)
                if not e:
                    continue
                db.execute(
                    text("UPDATE t_nexus_discussions SET active_employee_id=:e,turns=:t WHERE id=:i"),
                    {"e": eid, "t": turn + 1, "i": d["id"]},
                )
                db.commit()
                channel = one(db, "SELECT * FROM t_nexus_channels WHERE id=:c", c=d["channel_id"])
                session_id = await self.claim_session(
                    db,
                    self.app.state.agentscope,
                    e,
                    d["channel_id"],
                    channel["name"],
                    self.app.state.resources,
                )
                config = await self.model_for(self.app.state.agentscope, e)
                config = {
                    **config,
                    "parameters": {**config.get("parameters", {}), "max_tokens": MAX_OUTPUT_TOKENS},
                }
                await self.app.state.agentscope.replace_session_model(
                    e["agentscope_agent_id"], session_id, config
                )
                await self.sync_resources(
                    self.app.state.agentscope, e, [session_id], db, self.app.state.resources
                )
                from .task_skill import attach_task_capability

                await attach_task_capability(
                    self.app.state.agentscope,
                    e,
                    session_id,
                    channel_id=d["channel_id"],
                    discussion_id=d["id"],
                )
                history = [
                    dict(r)
                    for r in db.execute(
                        text(
                            "SELECT * FROM t_nexus_messages WHERE channel_id=:c ORDER BY seq DESC LIMIT 100"
                        ),
                        {"c": d["channel_id"]},
                    ).mappings()
                ][::-1]
                binding = one(
                    db,
                    """SELECT last_delivered_message_id FROM t_nexus_channel_members WHERE channel_id=:c AND
                    employee_id=:e""",
                    c=d["channel_id"],
                    e=eid,
                )
                cursor = binding["last_delivered_message_id"]
                pending = history
                if cursor:
                    for index, row in enumerate(history):
                        if row["id"] == cursor:
                            pending = history[index + 1 :]
                            break
                # 每轮提供受限公开上下文；同事原话保留独立 name 与 sender_type。
                pending = [r for r in pending if r["sender_type"] != "system" and r["speaker_uid"] != eid]
                size = 0
                kept = []
                for row in reversed(pending):
                    size += len(row["text"])
                    if size > MAX_CONTEXT_CHARS and kept:
                        break
                    kept.append(row)
                inputs = []
                for row in reversed(kept):
                    content = [{"type": "text", "text": row["text"][:MAX_CONTEXT_CHARS]}]
                    ids = json.loads(row["attachments_json"])
                    if ids:
                        import pathlib

                        files = self.load_attachments(db, ids, None)
                        content = self.model_input_blocks(
                            row["text"],
                            [(a["name"], a["type"], pathlib.Path(a["path"]).read_bytes()) for a in files],
                        )
                    inputs.append(
                        {
                            "id": row["id"],
                            "name": row["sender_name"],
                            "role": "user",
                            "content": content,
                            "metadata": {
                                "message_id": row["id"],
                                "sender_id": row["speaker_uid"],
                                "sender_type": row["sender_type"],
                                "display_text": row["text"],
                            },
                        }
                    )
                source = next((row for row in history if row["id"] == d["message_id"]), None)
                task_event_id = (
                    json.loads(source["native_json"]).get("metadata", {}).get("task_event_id")
                    if source and source["sender_type"] == "system" else None
                )
                if task_event_id:
                    notice = one(
                        db, """SELECT o.hint FROM t_nexus_task_wake_outbox o JOIN t_nexus_tasks t
                        ON t.id=o.task_id WHERE o.event_id=:i AND t.source_channel_id=:c
                        AND t.source_session_id=:s AND t.responsible_employee_id=:e""",
                        i=task_event_id, c=d["channel_id"], s=session_id, e=eid,
                    )
                    if notice:
                        inputs.append({
                            "id": f"task_hint_{task_event_id}", "name": "任务协助提示", "role": "user",
                            # AgentScope 2.0.10 的 user Msg 只接受 text/data；
                            # inbox 可用的 HintBlock 不能直接放进 /chat/ 的 user 消息。
                            "content": [{"type": "text", "text": notice["hint"]}],
                            "metadata": {"source": "nexus-task"},
                        })
                names = "、".join(x["name"] for x in staff)
                inputs.append(
                    {
                        "id": new_id("turn"),
                        "name": "群讨论规则",
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"当前群成员：{names}。你是{e['name']}。"
                                "请根据以上真实群发言回应当前讨论目标；前面同事已回答的部分不重复。"
                                f"这是第{turn + 1}/{MAX_TURNS}次发言。"
                                "需要同事继续行动或把结果交还请求者时，使用contact_employee向该同事投递，"
                                "并在原话中说清回传对象和下一步；仅公开陈述结果不会自动唤醒请求者。不要代写同事回答。"
                                "不需要继续时直接结束，不为客套提出新问题。普通交流不创建工程任务或职责请求。权限由服务端判定。",
                            }
                        ],
                        "metadata": {"discussion_id": d["id"], "verified_uid": d["initiator_id"]},
                    }
                )
                db.commit()
            if not self.current(d["id"])["valid"]:
                await asyncio.to_thread(self.finish, d["id"], "interrupted")
                return
            if not await asyncio.to_thread(self.renew, d["channel_id"], token):
                raise UnconfirmedStop("发言前租约已失效")
            reply = await self.reply(d, e, session_id, inputs, token, started)
            if reply is None:
                await asyncio.to_thread(self.finish, d["id"], "interrupted")
                return
            counts[eid] += 1
            usage = reply.get("usage") or {}
            generated_tokens += int(usage.get("output_tokens", 0) or 0)
            total_tokens += int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)
            with connect() as db:
                await acquire_channel_lock(db, d["channel_id"])
                db.execute(
                    text("UPDATE t_nexus_discussions SET tokens=:t WHERE id=:i"),
                    {"t": total_tokens, "i": d["id"]},
                )
                db.execute(
                    text(
                        """UPDATE t_nexus_channel_members SET last_delivered_message_id=:m WHERE channel_id=:c
                        AND employee_id=:e"""
                    ),
                    {"m": history[-1]["id"], "c": d["channel_id"], "e": eid},
                )
                db.commit()
            with connect() as db:
                updated = one(db, "SELECT participants_json FROM t_nexus_discussions WHERE id=:i", i=d["id"])
                scheduled = json.loads(updated["participants_json"])
                for target in scheduled[scheduled_count:]:
                    if target not in queue and counts[target] < 2:
                        queue.append(target)
                scheduled_count = len(scheduled)
            body = "".join(b.get("text", "") for b in reply.get("content", []) if b.get("type") == "text")
            source = next((row for row in history if row["id"] == d["message_id"]), None)
            greeting = bool(
                source
                and re.fullmatch(r"\s*(大家好|你们好|各位好|hello|hi)[！!。~～\s]*", source["text"], re.I)
            )
            explicit = named_employees(source["text"], staff) if source else []
            strict_single = len(explicit) == 1 and not any(
                other["name"] in routing_text(source["text"]) for other in staff if other["id"] != explicit[0]
            )
            task_notice = bool(
                source and source["sender_type"] == "system"
                and json.loads(source["native_json"]).get("metadata", {}).get("task_event_id")
            )
            targets = [] if greeting or strict_single or task_notice else followup(body, staff, eid, counts)
            for target in targets:
                if target not in queue:
                    queue.append(target)
                    with connect() as db:
                        await acquire_channel_lock(db, d["channel_id"])
                        latest = one(
                            db, "SELECT participants_json FROM t_nexus_discussions WHERE id=:i", i=d["id"]
                        )
                        scheduled = json.loads(latest["participants_json"])
                        if target not in scheduled:
                            scheduled.append(target)
                            db.execute(
                                text("UPDATE t_nexus_discussions SET participants_json=:p WHERE id=:i"),
                                {"p": dumps(scheduled), "i": d["id"]},
                            )
                        db.commit()
                    scheduled_count = len(scheduled)
        await asyncio.to_thread(self.finish, d["id"], "limited", "讨论已达到最大轮次")

    async def reply(self, d, e, session_id, inputs, token, started):
        client = self.app.state.agentscope
        before = await client.latest_event_cursor(e["agentscope_agent_id"], session_id)
        await client.trigger_inputs(e["agentscope_agent_id"], session_id, inputs)
        events = asyncio.Queue(maxsize=1000)

        async def pump():
            try:
                async with client.stream_session(e["agentscope_agent_id"], session_id, before) as response:
                    if response.status_code >= 400:
                        raise RuntimeError("AgentScope 事件流连接失败")
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            await events.put(json.loads(line[5:].strip()))
            except Exception as exc:
                await events.put(exc)

        task = asyncio.create_task(pump())
        reply_id = ""
        last_renew = 0
        try:
            while True:
                now = asyncio.get_running_loop().time()
                if now - last_renew > 5:
                    if not await asyncio.to_thread(self.renew, d["channel_id"], token):
                        raise RuntimeError("群讨论租约已失效")
                    last_renew = now
                current = self.current(d["id"])
                if not current["valid"] or now - started > MAX_SECONDS:
                    await self.stop_session(e, session_id)
                    if reply_id:
                        await self.persist_reply(d, e, session_id, reply_id, interrupted=True)
                    return None
                try:
                    event = await asyncio.wait_for(events.get(), 0.3)
                except TimeoutError:
                    if task.done():
                        raise RuntimeError("员工回复结束事件缺失") from None
                    continue
                if isinstance(event, Exception):
                    raise event
                kind = event.get("type", "")
                if kind == "REPLY_START":
                    reply_id = event["reply_id"]
                    with connect() as db:
                        await acquire_channel_lock(db, d["channel_id"])
                        append_message(
                            db,
                            d["channel_id"],
                            "employee",
                            e["id"],
                            e["name"],
                            "",
                            message_id=reply_id,
                            discussion_id=d["id"],
                        )
                        db.commit()
                if reply_id:
                    with connect() as db:
                        await acquire_channel_lock(db, d["channel_id"])
                        row = one(db, "SELECT native_json,seq FROM t_nexus_messages WHERE id=:i", i=reply_id)
                        native = json.loads(row["native_json"])
                        native["metadata"].setdefault("stream_events", []).append(event)
                        event_id = emit(
                            db,
                            d["channel_id"],
                            {
                                "type": "delta",
                                "message_id": reply_id,
                                "event": event,
                                "discussion_id": d["id"],
                                "employee_id": e["id"],
                                "seq": row["seq"],
                            },
                        )
                        native["metadata"]["stream_cursor"] = event_id
                        db.execute(
                            text("UPDATE t_nexus_messages SET native_json=:j WHERE id=:i"),
                            {"j": dumps(native), "i": reply_id},
                        )
                        db.commit()
                if kind == "REPLY_END":
                    result = await self.persist_reply(d, e, session_id, reply_id)
                    if event.get("finished_reason") == "error" or event.get("error"):
                        raise RuntimeError("员工推理失败，请检查模型路由与 Agent Service")
                    return result

        except BaseException:
            try:
                await self.stop_session(e, session_id)
                if reply_id:
                    await self.persist_reply(d, e, session_id, reply_id, interrupted=True)
            except Exception as stop_error:
                logger.exception("群回复中断尚未确认")
                raise UnconfirmedStop("员工停止状态尚未确认") from stop_error
            raise
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def persist_reply(self, d, e, session_id, reply_id, interrupted=False):
        for _ in range(30):
            messages, _, running = await self.app.state.agentscope.read_session_detail(
                e["agentscope_agent_id"], session_id
            )
            native = next((m for m in messages if m["id"] == reply_id), None)
            if native and not running:
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("AgentScope 未完成消息持久化")
        with connect() as db:
            await acquire_channel_lock(db, d["channel_id"])
            row = one(
                db,
                "SELECT * FROM t_nexus_messages WHERE id=:i AND channel_id=:c",
                i=reply_id,
                c=d["channel_id"],
            )
            if not row:
                raise RuntimeError("群回复记录不存在")
            local = json.loads(row["native_json"])
            local["metadata"].pop("stream_events", None)
            native = {
                **native,
                "id": row["id"],
                "name": row["sender_name"],
                "created_at": row["created_at"],
                "metadata": local["metadata"],
            }
            if interrupted:
                native["finished_reason"] = "interrupted"
            body = "".join(b.get("text", "") for b in native.get("content", []) if b.get("type") == "text")
            db.execute(
                text("UPDATE t_nexus_messages SET native_json=:j,text=:b WHERE id=:i"),
                {"j": dumps(native), "b": body, "i": reply_id},
            )
            row = one(db, "SELECT * FROM t_nexus_messages WHERE id=:i", i=reply_id)
            emit(db, d["channel_id"], {"type": "message", "item": message_payload(row)})
            db.commit()
        return native
