"""Nexus 只保存聊天附件的 Workspace 引用。"""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from typing import Literal
from urllib.parse import quote

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import inspect, text

from .db import connect

ATTACHMENT_PREFIX = "/nexus/workspace-attachments"
ATTACHMENT_ID = r"^att_[0-9a-f]{32}$"
PREVIEW_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def file_name(value):
    name = PurePath(value.replace("\\", "/")).name
    name = "".join(c for c in name if c.isprintable()).strip()
    return name[:255] if name and name not in {".", ".."} else "未命名文件"


def disposition(name, mime):
    kind = "inline" if mime in PREVIEW_TYPES else "attachment"
    return f"{kind}; filename*=UTF-8''{quote(name, safe='')}"


def assert_attachment_cutover():
    with connect() as db:
        if not inspect(db).has_table("t_nexus_attachments"):
            return
        migrated = db.execute(
            text("SELECT value FROM t_nexus_settings WHERE `key`=:key"),
            {"key": "workspace_attachments_migrated"},
        ).scalar()
        if migrated != "1":
            raise RuntimeError("旧附件尚未迁移，请先执行 scripts.migrate_workspace_attachments")


def reference(db, key):
    return db.execute(
        text("SELECT * FROM t_nexus_attachment_refs WHERE id=:id"), {"id": key},
    ).mappings().first()


def visible(row):
    return {"id": row["id"], "name": row["name"], "type": row["media_type"], "size": row["size"]}


def location(row):
    return {"agent_id": row["agent_id"], "session_id": row["session_id"]}


def load_attachments(db, ids, *, scope_kind, scope_id, owner_id=None):
    if len(ids) > 6 or len(set(ids)) != len(ids):
        raise HTTPException(400, "每条消息最多添加 6 个不同附件")
    result = []
    for key in ids:
        row = reference(db, key)
        if (
            not row or not row["sha256"] or row["linked"] < 0
            or row["scope_kind"] != scope_kind or row["scope_id"] != scope_id
            or (owner_id is not None and row["owner_uid"] != owner_id)
        ):
            raise HTTPException(404, "附件不存在")
        result.append(row)
    return result


def link_attachments(db, rows):
    for row in rows:
        result = db.execute(
            text("UPDATE t_nexus_attachment_refs SET linked=1 WHERE id=:id AND sha256=:digest AND linked>=0"),
            {"id": row["id"], "digest": row["sha256"]},
        )
        if not result.rowcount:
            raise HTTPException(409, "附件已过期，请重新上传")


def attachment_content(blocks, attachments, message_id):
    if not attachments:
        return blocks
    names = "\n".join(
        f"attachments/{item['id']} {json.dumps(item['name'], ensure_ascii=False)} ({item['type']})"
        for item in attachments
    )
    label = f"消息 {message_id} 的会话附件（按需调用 read_chat_attachment）"
    return [*blocks, {
        "type": "text", "id": f"nexus-attachments-{message_id}", "text": f"{label}：\n{names}",
    }]


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str = Field(max_length=64_000)
    id: str | None = Field(default=None, max_length=128)


class MessageContent(BaseModel):
    content: list[TextBlock] = Field(default_factory=list, max_length=128)
    text: str = Field(default="", max_length=64_000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def derive_display_text(self):
        if self.content:
            joined = "".join(block.text for block in self.content)
            if len(joined) > 64_000 or (self.text and self.text != joined):
                raise ValueError("消息正文过长或与内容块不一致")
            self.text = joined
        elif self.text:
            self.content = [TextBlock(text=self.text)]
        return self

    def blocks(self):
        return [block.model_dump(exclude_none=True) for block in self.content]


async def expire_attachments(client):
    from .agentscope import AgentScopeError

    logger = logging.getLogger(__name__)
    while True:
        await asyncio.sleep(60)
        if not client.configured:
            continue
        cutoff = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        try:
            with connect() as db:
                rows = db.execute(
                    text("SELECT * FROM t_nexus_attachment_refs "
                         "WHERE linked<=0 AND created_at<:cutoff LIMIT 100"),
                    {"cutoff": cutoff},
                ).mappings().all()
            for row in rows:
                with connect() as db:
                    claimed = db.execute(
                        text("UPDATE t_nexus_attachment_refs SET linked=-1 "
                             "WHERE id=:id AND linked<=0 AND created_at<:cutoff"),
                        {"id": row["id"], "cutoff": cutoff},
                    ).rowcount
                    db.commit()
                if not claimed:
                    continue
                try:
                    await client.request("DELETE", f"{ATTACHMENT_PREFIX}/{row['id']}", params=location(row))
                except AgentScopeError as exc:
                    if exc.status_code != 404:
                        logger.warning("Workspace 附件回收失败，稍后重试")
                        continue
                with connect() as db:
                    db.execute(
                        text("DELETE FROM t_nexus_attachment_refs WHERE id=:id AND linked=-1"),
                        {"id": row["id"]},
                    )
                    db.commit()
        except Exception:
            logger.exception("Workspace 附件回收失败")
