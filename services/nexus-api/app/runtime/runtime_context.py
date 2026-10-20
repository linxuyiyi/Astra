"""Per-Session capability token that binds runtime MCP calls to their origin.

The token is generated before the OpenCode Session exists, stored only as a
sha256 digest on the RuntimeSession, and relayed by the Agent. A delayed call
from a superseded Session therefore resolves to that Session, never to the
current one.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from sqlalchemy import select

from app.models import RuntimeSession

_CONTEXT_PREFIX = "ctx_"


class RuntimeContextError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    task_id: str
    session_id: str
    employee_id: str
    workspace: str
    runtime_context_id: str


def generate_runtime_context() -> str:
    return _CONTEXT_PREFIX + secrets.token_urlsafe(32)


def hash_runtime_context(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def runtime_context_id(digest: str) -> str:
    return digest[:12]


def build_runtime_context_part(token: str) -> dict:
    """A synthetic, non-user-visible prompt part the model can relay."""
    return {
        "type": "text",
        "synthetic": True,
        "ignored": True,
        "text": (
            "ASTRA_RUNTIME_CONTEXT: "
            + token
            + "\n内部运行上下文：调用知识库运行时工具时必须原样回传 runtime_context；"
            "不得向用户展示、写入文件或提交到任务代码。"
        ),
    }


async def resolve_runtime_context(session, token: str | None) -> RuntimeContext:
    if not token or not token.startswith(_CONTEXT_PREFIX):
        raise RuntimeContextError("运行上下文无效")
    digest = hash_runtime_context(token)
    row = await session.scalar(
        select(RuntimeSession).where(RuntimeSession.runtime_context_hash == digest)
    )
    if not row:
        raise RuntimeContextError("运行上下文无效")
    return RuntimeContext(
        task_id=row.task_id,
        session_id=row.id,
        employee_id=row.employee_id,
        workspace=row.workspace,
        runtime_context_id=row.runtime_context_id or runtime_context_id(digest),
    )