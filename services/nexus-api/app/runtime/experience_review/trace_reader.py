"""按确认时冻结的 Session 列表分页读取原生执行轨迹。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import RuntimeSession
from app.runtime.session_connection import connect, resolve_runtime_session_environment

from .schemas import TraceResult
from .trace_normalizer import normalize_messages


class TraceUnavailable(RuntimeError):
    """原生来源在当前时刻不可读取；可重试。"""


class TraceTooLarge(RuntimeError):
    """超过消息数或字节硬上限；以受控终态结束。"""


def _message_bytes(message: dict[str, Any]) -> int:
    import json

    try:
        return len(json.dumps(message, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(message).encode("utf-8", "replace"))


async def read_trace(snapshot: dict[str, Any]) -> TraceResult:
    """读取 snapshot 指定的 Session；不读取 snapshot 之外的 Session。"""

    settings = get_settings()
    root = str(snapshot.get("root_session_id") or "").strip()
    sessions = [str(item) for item in snapshot.get("source_session_ids") or [] if item]
    if root and root not in sessions:
        sessions.insert(0, root)
    if not sessions:
        raise TraceUnavailable("复盘来源没有 Session")

    async with SessionLocal() as db:
        binding = await db.get(RuntimeSession, root)
        if binding is None:
            raise TraceUnavailable("根 RuntimeSession 不存在")
        try:
            environment = await resolve_runtime_session_environment(db, binding)
        except Exception as exc:  # noqa: BLE001 - source resolution is an external boundary
            raise TraceUnavailable(f"无法解析复盘执行环境: {exc}") from exc
        workspace = str(snapshot.get("workspace") or binding.workspace or "")

    if binding.runtime_engine == "dsh":
        from .dsh_trace import read_dsh_trace
        return await read_dsh_trace(binding, environment, snapshot)
    directory = Path(workspace)
    events = []
    total_messages = 0
    total_bytes = 0
    try:
        async with connect(environment, start=False) as client:
            if not workspace:
                raise TraceUnavailable("复盘来源没有 Workspace")
            for session_id in sessions:
                before: str | None = None
                session_messages: list[dict[str, Any]] = []
                seen: set[str] = set()
                while True:
                    reader = getattr(client, "read_review_trace", None)
                    if callable(reader):
                        page = await reader(
                            directory,
                            session_id,
                            limit=settings.experience_review_page_size,
                            before=before,
                        )
                    else:
                        page = await client.messages_page(
                            directory,
                            session_id,
                            limit=settings.experience_review_page_size,
                            before=before,
                        )
                    if isinstance(page, dict) and page.get("too_large"):
                        # Worker 明确报告“单条消息放不进一帧”；以受控终态结束，
                        # 不把缺口当成“读取完成”。
                        raise TraceTooLarge(str(page["too_large"]))
                    if isinstance(page, dict) and page.get("truncated"):
                        # 仍然出现被裁剪的页说明游标不再覆盖全部消息；不能静默跳过。
                        raise TraceTooLarge("原生轨迹分页被裁剪，无法保证完整读取")
                    batch = page.get("messages") if isinstance(page, dict) else []
                    if not isinstance(batch, list) or not batch:
                        break
                    for message in batch:
                        if not isinstance(message, dict):
                            continue
                        info = message.get("info") if isinstance(message.get("info"), dict) else {}
                        message_id = str(info.get("id") or "")
                        if message_id and message_id in seen:
                            continue
                        if message_id:
                            seen.add(message_id)
                        session_messages.append(message)
                        total_messages += 1
                        total_bytes += _message_bytes(message)
                        if total_messages > settings.experience_review_max_messages:
                            raise TraceTooLarge("原生消息数超过复盘硬上限")
                        if total_bytes > settings.experience_review_max_source_bytes:
                            raise TraceTooLarge("原生轨迹字节数超过复盘硬上限")
                    before = page.get("next_before") if isinstance(page, dict) else None
                    if not before:
                        break
                # OpenCode 分页按新到旧返回；统一恢复为旧到新。
                events.extend(normalize_messages(list(reversed(session_messages)), session_id))
    except (TraceTooLarge, TraceUnavailable):
        raise
    except Exception as exc:  # noqa: BLE001 - OpenCode/Worker transport boundary
        raise TraceUnavailable(f"读取原生轨迹失败: {exc}") from exc

    digest = hashlib.sha256()
    for index, event in enumerate(events, 1):
        event.ref = f"trace-{index:06d}"
        digest.update(
            f"{event.ref}|{event.session_id}|{event.message_id}|{event.part_id}|{event.content_digest}\n".encode()
        )
    watermarks = snapshot.get("native_watermarks")
    return TraceResult(
        events=events,
        sessions=sessions,
        source_digest=digest.hexdigest(),
        message_count=total_messages,
        watermarks=watermarks if isinstance(watermarks, dict) else {},
    )
