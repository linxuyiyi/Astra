"""IM 入站、出站和 ACK。公司 SDK 不在这里，调用方可以换一个模拟 Adapter。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text as sql_text

from .db import new_id, utcnow
from .sql import insert_record, upsert_record

LEASE_SECONDS = 30


class ImError(Exception):
    def __init__(self, detail: str, status_code: int = 409) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def bind_bot(connection, employee_id: str, provider: str, bot_id: str) -> dict[str, str]:
    provider = provider.strip()
    bot_id = bot_id.strip()
    if not provider or not bot_id:
        raise ImError("缺少机器人标识", 422)
    connection.execute(
        sql_text("""
            DELETE FROM t_nexus_im_bots WHERE employee_id = :p0 OR (provider = :p1 AND bot_id = :p2)
            """),
        {"p0": employee_id, "p1": provider, "p2": bot_id},
    )
    connection.execute(
        sql_text("INSERT INTO t_nexus_im_bots (employee_id, provider, bot_id) VALUES (:p0, :p1, :p2)"),
        {"p0": employee_id, "p1": provider, "p2": bot_id},
    )
    connection.commit()
    return {"employee_id": employee_id, "provider": provider, "bot_id": bot_id}


def open_external(connection, employee_id: str, external_key: str, session_id: str) -> dict[str, str]:
    external_key = external_key.strip()
    session_id = session_id.strip()
    if not external_key or not session_id:
        raise ImError("外部群需要独立的会话", 422)
    private = (
        connection.execute(
            sql_text(
                """
                SELECT agentscope_session_id FROM t_nexus_conversations
                WHERE employee_id = :p0 AND kind = 'private' AND agentscope_session_id = :p1
                """
            ),
            {"p0": employee_id, "p1": session_id},
        )
        .mappings()
        .fetchone()
    )
    if private is not None:
        raise ImError("外部群不能复用私聊会话", 409)
    upsert_record(
        connection,
        "t_nexus_im_bindings",
        {
            "employee_id": employee_id,
            "kind": "external",
            "external_key": external_key,
            "owner_uid": "",
            "session_id": session_id,
        },
        keys=["employee_id", "kind", "external_key", "owner_uid"],
        update=["session_id"],
    )
    connection.commit()
    return {"employee_id": employee_id, "external_key": external_key, "session_id": session_id}


async def accept_inbound(
    connection,
    *,
    provider: str,
    event_id: str,
    bot_id: str,
    sender_uid: str,
    text: str,
    accept: Callable[..., Any],
    external_key: str = "",
    conversation_key: str = "",
    ensure_private: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """先原子认领事件行。只有认领成功的那一份才调用 accept。"""

    provider = provider.strip()
    event_id = event_id.strip()
    if not provider or not event_id:
        raise ImError("缺少入站事件", 422)
    bot = (
        connection.execute(
            sql_text("SELECT employee_id FROM t_nexus_im_bots WHERE provider = :p0 AND bot_id = :p1"),
            {"p0": provider, "p1": bot_id.strip()},
        )
        .mappings()
        .fetchone()
    )
    if bot is None:
        raise ImError("这个员工没有绑定机器人，IM 不可用", 409)
    employee_id = bot["employee_id"]
    sender = sender_uid.strip()
    external = external_key.strip()
    thread = conversation_key.strip()
    session_id = _session_for(connection, employee_id, sender, external, thread)
    if not session_id and ensure_private is not None and sender and not external and not thread:
        session_id = await ensure_private(employee_id, sender)
    if not session_id:
        raise ImError("还没有可复用的会话", 409)
    token = new_id("clm")
    insert_record(
        connection,
        "t_nexus_im_events",
        {
            "provider": provider,
            "event_id": event_id,
            "employee_id": employee_id,
            "session_id": session_id,
            "accepted": 0,
            "claim_token": "",
        },
        ignore_keys=["provider", "event_id"],
    )
    now = datetime.now(UTC).isoformat()
    expiry = (datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)).isoformat()
    claimed = connection.execute(
        sql_text(
            """
            UPDATE t_nexus_im_events
            SET claim_token = :p0, session_id = :p1, employee_id = :p2, claim_until = :p3
            WHERE provider = :p4 AND event_id = :p5 AND accepted = 0
              AND (claim_token = '' OR claim_until = '' OR claim_until <= :p6)
            """
        ),
        {
            "p0": token,
            "p1": session_id,
            "p2": employee_id,
            "p3": expiry,
            "p4": provider,
            "p5": event_id,
            "p6": now,
        },
    )
    connection.commit()
    if claimed.rowcount != 1:
        existing = (
            connection.execute(
                sql_text("SELECT * FROM t_nexus_im_events WHERE provider = :p0 AND event_id = :p1"),
                {"p0": provider, "p1": event_id},
            )
            .mappings()
            .fetchone()
        )
        if existing is not None and existing["accepted"]:
            return {
                "accepted": True,
                "duplicate": True,
                "session_id": existing["session_id"],
                "employee_id": employee_id,
            }
        raise ImError("这个事件正在处理", 409)
    try:
        current = (
            connection.execute(
                sql_text("SELECT claim_token FROM t_nexus_im_events WHERE provider = :p0 AND event_id = :p1"),
                {"p0": provider, "p1": event_id},
            )
            .mappings()
            .fetchone()
        )
        if current is None or current["claim_token"] != token:
            return {
                "accepted": True,
                "duplicate": True,
                "session_id": session_id,
                "employee_id": employee_id,
            }
        await accept(employee_id, session_id, text)
    except Exception:
        connection.execute(
            sql_text(
                """
                UPDATE t_nexus_im_events SET claim_token = '', claim_until = ''
                WHERE provider = :p0 AND event_id = :p1 AND claim_token = :p2
                """
            ),
            {"p0": provider, "p1": event_id, "p2": token},
        )
        connection.commit()
        raise
    finished = connection.execute(
        sql_text(
            """
            UPDATE t_nexus_im_events SET accepted = 1, claim_token = '', claim_until = ''
            WHERE provider = :p0 AND event_id = :p1 AND claim_token = :p2
            """
        ),
        {"p0": provider, "p1": event_id, "p2": token},
    )
    connection.commit()
    if finished.rowcount != 1:
        raise ImError("这个事件正在处理", 409)
    return {"accepted": True, "duplicate": False, "session_id": session_id, "employee_id": employee_id}


def contact_human(
    connection,
    *,
    employee_id: str,
    conversation_key: str,
    session_id: str,
    body: str,
) -> dict[str, str]:
    conversation_key = conversation_key.strip()
    session_id = session_id.strip()
    if not conversation_key or not session_id or not body.strip():
        raise ImError("缺少主动联系的会话或正文", 422)
    upsert_record(
        connection,
        "t_nexus_im_threads",
        {"employee_id": employee_id, "conversation_key": conversation_key, "session_id": session_id},
        keys=["employee_id", "conversation_key"],
        update=["session_id"],
    )
    delivery_id = new_id("dlv")
    connection.execute(
        sql_text(
            """
            INSERT INTO t_nexus_im_outbox (
                delivery_id, employee_id, conversation_key, session_id, body, state, lease_until,
            created_at
            ) VALUES (:p0, :p1, :p2, :p3, :p4, 'queued', '', :p5)
            """
        ),
        {
            "p0": delivery_id,
            "p1": employee_id,
            "p2": conversation_key,
            "p3": session_id,
            "p4": body.strip(),
            "p5": utcnow(),
        },
    )
    connection.commit()
    return {"delivery_id": delivery_id, "state": "queued", "session_id": session_id}


def claim_outbox(connection, *, lease_seconds: int = LEASE_SECONDS) -> dict[str, str] | None:
    """认领一条出站。只重新认领租约已过期的在途行，仍用原来的 delivery_id。"""

    now = datetime.now(UTC).isoformat()
    expiry = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
    current = (
        connection.execute(
            sql_text(
                """
                SELECT delivery_id, lease_until FROM t_nexus_im_outbox
                WHERE state = 'inflight' AND (lease_until = '' OR lease_until <= :p0)
                ORDER BY created_at, delivery_id
                LIMIT 1
                """
            ),
            {"p0": now},
        )
        .mappings()
        .fetchone()
    )
    if current is not None:
        renewed = connection.execute(
            sql_text(
                """
                UPDATE t_nexus_im_outbox SET lease_until = :p0
                WHERE delivery_id = :p1 AND state = 'inflight' AND lease_until = :p2
                """
            ),
            {"p0": expiry, "p1": current["delivery_id"], "p2": current["lease_until"]},
        )
        connection.commit()
        if renewed.rowcount != 1:
            return claim_outbox(connection, lease_seconds=lease_seconds)
        return _outbox_row(connection, current["delivery_id"])
    candidate = (
        connection.execute(
            sql_text(
                """
                SELECT delivery_id FROM t_nexus_im_outbox
                WHERE state = 'queued'
                  AND conversation_key NOT IN (
                    SELECT conversation_key FROM t_nexus_im_outbox WHERE state = 'inflight'
                  )
                ORDER BY created_at, delivery_id
                LIMIT 1
                """
            )
        )
        .mappings()
        .fetchone()
    )
    if candidate is None:
        return None
    claimed = connection.execute(
        sql_text(
            """
            UPDATE t_nexus_im_outbox SET state = 'inflight', lease_until = :p0
            WHERE delivery_id = :p1 AND state = 'queued'
            """
        ),
        {"p0": expiry, "p1": candidate["delivery_id"]},
    )
    connection.commit()
    if claimed.rowcount != 1:
        return claim_outbox(connection, lease_seconds=lease_seconds)
    return _outbox_row(connection, candidate["delivery_id"])


def _outbox_row(connection, delivery_id: str) -> dict[str, str]:
    row = (
        connection.execute(
            sql_text("SELECT * FROM t_nexus_im_outbox WHERE delivery_id = :p0"), {"p0": delivery_id}
        )
        .mappings()
        .fetchone()
    )
    return {
        "delivery_id": row["delivery_id"],
        "employee_id": row["employee_id"],
        "conversation_key": row["conversation_key"],
        "session_id": row["session_id"],
        "body": row["body"],
        "state": row["state"],
        "lease_until": row["lease_until"],
    }


def acknowledge(connection, delivery_id: str) -> dict[str, str]:
    row = (
        connection.execute(
            sql_text("SELECT * FROM t_nexus_im_outbox WHERE delivery_id = :p0"), {"p0": delivery_id}
        )
        .mappings()
        .fetchone()
    )
    if row is None:
        raise ImError("没有这条投递", 404)
    if row["state"] != "acked":
        connection.execute(
            sql_text("UPDATE t_nexus_im_outbox SET state = 'acked' WHERE delivery_id = :p0"),
            {"p0": delivery_id},
        )
        nxt = (
            connection.execute(
                sql_text(
                    """
                    SELECT delivery_id FROM t_nexus_im_outbox
                    WHERE conversation_key = :p0 AND state = 'queued'
                    ORDER BY created_at, delivery_id
                    LIMIT 1
                    """
                ),
                {"p0": row["conversation_key"]},
            )
            .mappings()
            .fetchone()
        )
        if nxt is not None:
            connection.execute(
                sql_text("UPDATE t_nexus_im_outbox SET state = 'inflight' WHERE delivery_id = :p0"),
                {"p0": nxt["delivery_id"]},
            )
        connection.commit()
    return {"delivery_id": delivery_id, "state": "acked"}


def redeliver(connection, conversation_key: str) -> dict[str, str]:
    """ACK 丢失时仍用原来的 delivery_id。不新写一行，也不再次推理。"""

    row = (
        connection.execute(
            sql_text(
                """
                SELECT * FROM t_nexus_im_outbox
                WHERE conversation_key = :p0 AND state = 'inflight'
                ORDER BY created_at, delivery_id
                LIMIT 1
                """
            ),
            {"p0": conversation_key},
        )
        .mappings()
        .fetchone()
    )
    if row is None:
        raise ImError("没有待重投的消息", 404)
    return {
        "delivery_id": row["delivery_id"],
        "session_id": row["session_id"],
        "body": row["body"],
        "state": row["state"],
    }


def _session_for(
    connection,
    employee_id: str,
    sender_uid: str,
    external_key: str,
    conversation_key: str,
) -> str:
    if conversation_key:
        thread = (
            connection.execute(
                sql_text(
                    """
                    SELECT session_id FROM t_nexus_im_threads
                    WHERE employee_id = :p0 AND conversation_key = :p1
                    """
                ),
                {"p0": employee_id, "p1": conversation_key},
            )
            .mappings()
            .fetchone()
        )
        return thread["session_id"] if thread is not None else ""
    if external_key:
        binding = (
            connection.execute(
                sql_text(
                    """
                    SELECT session_id FROM t_nexus_im_bindings
                    WHERE employee_id = :p0 AND kind = 'external' AND external_key = :p1
                    """
                ),
                {"p0": employee_id, "p1": external_key},
            )
            .mappings()
            .fetchone()
        )
        return binding["session_id"] if binding is not None else ""
    private = (
        connection.execute(
            sql_text(
                """
                SELECT agentscope_session_id FROM t_nexus_conversations
                WHERE employee_id = :p0 AND owner_id = :p1 AND kind = 'private'
                """
            ),
            {"p0": employee_id, "p1": sender_uid},
        )
        .mappings()
        .fetchone()
    )
    if private is None:
        return ""
    return private["agentscope_session_id"] or ""
