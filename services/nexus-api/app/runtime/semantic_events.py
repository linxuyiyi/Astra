"""Translate raw Agent CLI events into stable NEXUS runtime semantics.

One OpenCode stream item may carry more than one business meaning. The
important example is the structured ``task`` tool: it is both a tool call and
the lifecycle event of the subagent launched by that tool. The plural adapter
keeps those meanings separate so downstream projections never need to infer
them from assistant prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SemanticEvent:
    event_type: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    agent_name: str | None = None


def _normalized(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", ".").replace("-", ".")


def _part(event: dict[str, Any]) -> dict[str, Any]:
    properties = event.get("properties")
    if isinstance(properties, dict):
        nested = properties.get("part")
        return nested if isinstance(nested, dict) else properties
    return event.get("part") if isinstance(event.get("part"), dict) else event


def _raw_type(event: dict[str, Any], part: dict[str, Any]) -> str:
    return _normalized(event.get("type") or part.get("type"))


def _text_value(event: dict[str, Any], part: dict[str, Any]) -> str:
    candidates = [part.get("text"), event.get("text"), part.get("content"), event.get("content")]
    message = event.get("message")
    if isinstance(message, dict):
        candidates.extend([message.get("text"), message.get("content")])
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _status(event: dict[str, Any], part: dict[str, Any]) -> str:
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    return _normalized(state.get("status") or part.get("status") or event.get("status") or "started")


def _lifecycle(prefix: str, status: str) -> str:
    if any(value in status for value in ("fail", "error", "reject", "denied", "cancel")):
        return f"{prefix}.failed"
    if any(value in status for value in ("complete", "success", "done", "finished")):
        return f"{prefix}.completed"
    if any(value in status for value in ("progress", "running", "working", "update")):
        return f"{prefix}.progress"
    return f"{prefix}.started"


def _question(event: dict[str, Any], part: dict[str, Any]) -> str:
    questions = event.get("questions") or part.get("questions")
    if isinstance(questions, list):
        values = [
            str(item.get("question") or "").strip()
            for item in questions
            if isinstance(item, dict) and item.get("question")
        ]
        if values:
            return "\n".join(values)
    candidates = [
        part.get("question"),
        event.get("question"),
        part.get("prompt"),
        event.get("prompt"),
        part.get("message"),
        event.get("message"),
        _text_value(event, part),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if isinstance(candidate, dict):
            nested = candidate.get("text") or candidate.get("content")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def _is_inquiry_type(value: str) -> bool:
    return (
        value == "ask"
        or value.startswith(("ask.", "question.", "permission.", "inquiry."))
        or value
        in {
            "question",
            "permission",
            "inquiry",
        }
    )


def _source_payload(event: dict[str, Any], part: dict[str, Any], event_type: str) -> dict[str, Any]:
    return {
        "source_event": event_type,
        "source_part_type": _normalized(part.get("type")),
        "source_part_id": part.get("id"),
        "source_message_id": part.get("messageID") or part.get("message_id"),
        "source_session_id": (
            part.get("sessionID")
            or part.get("session_id")
            or event.get("sessionID")
            or event.get("session_id")
        ),
        "source_timestamp": event.get("timestamp"),
    }


def _task_subagent_event(
    event: dict[str, Any],
    part: dict[str, Any],
    event_type: str,
    status: str,
) -> SemanticEvent | None:
    """Project OpenCode's structured Task tool into a subagent lifecycle.

    OpenCode 1.18 emits a completed/error ``tool_use`` item for the Task tool.
    ``subagent_type`` and child-session metadata are the proof that a subagent
    ran. Assistant text is deliberately not consulted here.
    """

    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    tool = _normalized(part.get("tool") or part.get("toolName") or part.get("tool_name"))
    tool_input = state.get("input") or part.get("input") or {}
    metadata = state.get("metadata") or part.get("metadata") or {}
    if tool != "task" or not isinstance(tool_input, dict):
        return None
    agent_name = str(
        tool_input.get("subagent_type") or tool_input.get("subagentType") or tool_input.get("agent") or ""
    ).strip()
    if not agent_name:
        return None
    if not isinstance(metadata, dict):
        metadata = {}
    title = str(state.get("title") or tool_input.get("description") or "子任务").strip()
    lifecycle = _lifecycle("subagent", status)
    verb = (
        "已完成"
        if lifecycle.endswith("completed")
        else "执行失败"
        if lifecycle.endswith("failed")
        else "正在执行"
    )
    model = metadata.get("model") if isinstance(metadata.get("model"), dict) else {}
    state_time = state.get("time") if isinstance(state.get("time"), dict) else {}
    return SemanticEvent(
        lifecycle,
        f"{agent_name} {verb}子任务：{title}",
        {
            **_source_payload(event, part, event_type),
            "source_tool": "task",
            "source_call_id": part.get("callID") or part.get("call_id"),
            "status": status,
            "role": agent_name,
            "description": tool_input.get("description"),
            "prompt": tool_input.get("prompt"),
            "input": tool_input,
            "output": state.get("output") or part.get("output"),
            "error": state.get("error") or part.get("error"),
            "parent_session_id": metadata.get("parentSessionId") or metadata.get("parent_session_id"),
            "child_session_id": metadata.get("sessionId") or metadata.get("session_id"),
            "model": model or None,
            "started_at": state_time.get("start"),
            "finished_at": state_time.get("end"),
        },
        agent_name,
    )


def adapt_open_code_events(event: dict[str, Any]) -> list[SemanticEvent]:
    """Return all stable meanings carried by one OpenCode runtime item."""
    if not isinstance(event, dict):
        return []
    part = _part(event)
    event_type = _raw_type(event, part)
    part_type = _normalized(part.get("type"))
    combined_type = f"{event_type} {part_type}"
    status = _status(event, part)
    source = _source_payload(event, part, event_type)

    info = event.get("info") if isinstance(event.get("info"), dict) else {}
    message_error = info.get("error")
    if isinstance(message_error, dict):
        data = message_error.get("data") if isinstance(message_error.get("data"), dict) else {}
        message = str(
            data.get("message")
            or message_error.get("message")
            or message_error.get("name")
            or "Agent 运行异常"
        )
        return [SemanticEvent("runtime.error", message, {**source, "status": "error"})]

    if event_type == "message.updated" and info:
        return [SemanticEvent("runtime.metadata", "Agent 消息元数据已更新", source)]

    if _is_inquiry_type(event_type) or _is_inquiry_type(part_type):
        question = _question(event, part)
        if question and not any(value in status for value in ("answered", "completed", "cancelled")):
            questions = event.get("questions") or part.get("questions") or []
            options = part.get("options") or event.get("options") or []
            if not options and isinstance(questions, list) and questions:
                first_question = questions[0]
                if isinstance(first_question, dict):
                    options = first_question.get("options") or []
            return [
                SemanticEvent(
                    "inquiry.requested",
                    question,
                    {
                        **source,
                        "description": str(part.get("description") or event.get("description") or ""),
                        "options": options if isinstance(options, list) else [],
                        "runtime_request_id": event.get("id") or part.get("id"),
                        "questions": questions if isinstance(questions, list) else [],
                    },
                )
            ]

    if "reasoning" in combined_type:
        return [
            SemanticEvent(
                "runtime.reasoning",
                "Agent 推理过程已省略",
                {**source, "visibility": "diagnostic", "content_stored": False},
            )
        ]

    text = _text_value(event, part)
    role = _normalized(part.get("role") or event.get("role"))
    is_assistant_message = (
        event_type == "text"
        or part_type == "text"
        or "assistant" in combined_type
        or ("message" in combined_type and role in {"assistant", "agent"})
    )
    if text and is_assistant_message:
        return [SemanticEvent("message.agent", text, source)]

    if "tool" in combined_type:
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        tool = str(
            part.get("tool")
            or part.get("toolName")
            or part.get("tool_name")
            or part.get("name")
            or "Agent Tool"
        )
        state_time = state.get("time") if isinstance(state.get("time"), dict) else {}
        metadata = state.get("metadata") or part.get("metadata")
        tool_event = SemanticEvent(
            _lifecycle("tool", status),
            str(state.get("title") or part.get("title") or event.get("title") or f"调用 {tool}"),
            {
                **source,
                "tool": tool,
                "status": status,
                "title": state.get("title") or part.get("title") or event.get("title"),
                "call_id": part.get("callID") or part.get("call_id"),
                "input": state.get("input") or part.get("input"),
                "output": state.get("output") or part.get("output"),
                "error": state.get("error") or part.get("error"),
                "metadata": metadata if isinstance(metadata, dict) else None,
                "started_at": state_time.get("start"),
                "finished_at": state_time.get("end"),
            },
            str(part.get("agent") or event.get("agent") or "") or None,
        )
        events = [tool_event]
        subagent = _task_subagent_event(event, part, event_type, status)
        if subagent:
            events.append(subagent)
        return events

    if "subtask" in combined_type or "subagent" in combined_type:
        agent_name = str(
            part.get("agent")
            or part.get("agent_name")
            or part.get("name")
            or event.get("agent")
            or "Subagent"
        )
        return [
            SemanticEvent(
                _lifecycle("subagent", status),
                str(
                    part.get("title")
                    or part.get("description")
                    or event.get("title")
                    or f"{agent_name} 正在处理"
                ),
                {
                    **source,
                    "status": status,
                    "role": part.get("role") or event.get("role") or agent_name,
                    "description": part.get("description") or event.get("description"),
                    "prompt": part.get("prompt") or event.get("prompt"),
                    "input": part.get("input") or event.get("input"),
                    "output": part.get("output") or event.get("output"),
                    "model": part.get("model") or event.get("model"),
                },
                agent_name,
            )
        ]

    if event_type.startswith("agent") or part_type.startswith("agent"):
        agent_name = str(
            part.get("agent") or part.get("agent_name") or part.get("name") or event.get("agent") or "Agent"
        )
        return [
            SemanticEvent(
                _lifecycle("agent", status),
                str(part.get("title") or event.get("title") or f"{agent_name} 正在处理"),
                {**source, "status": status},
                agent_name,
            )
        ]

    if event_type == "patch" or part_type == "patch":
        files = part.get("files") if isinstance(part.get("files"), list) else []
        return [
            SemanticEvent(
                "artifact.updated",
                f"Agent 更新了 {len(files)} 个文件" if files else "Agent 更新了工作文件",
                {**source, "hash": part.get("hash"), "files": files},
            )
        ]

    if event_type == "file" or part_type == "file":
        filename = str(part.get("filename") or part.get("url") or "Agent 产物")
        return [
            SemanticEvent(
                "artifact.created",
                f"Agent 生成了产物：{filename}",
                {
                    **source,
                    "name": part.get("filename"),
                    "url": part.get("url"),
                    "mime": part.get("mime"),
                    "source": part.get("source"),
                },
            )
        ]

    if event_type == "retry" or part_type == "retry":
        return [
            SemanticEvent(
                "runtime.retry",
                f"Agent 正在进行第 {part.get('attempt', '?')} 次重试",
                {**source, "attempt": part.get("attempt"), "error": part.get("error")},
            )
        ]

    if event_type == "error" or part_type == "error" or "session.error" in combined_type:
        error = event.get("error") or part.get("error") or {}
        if isinstance(error, dict):
            data = error.get("data") if isinstance(error.get("data"), dict) else {}
            message = str(
                data.get("message") or error.get("message") or error.get("name") or "Agent 运行异常"
            )
        else:
            message = str(error or "Agent 运行异常")
        return [SemanticEvent("runtime.error", message, {**source, "status": "error"})]

    if any(
        value in combined_type
        for value in (
            "step",
            "usage",
            "token",
            "session",
            "result",
            "snapshot",
            "compaction",
        )
    ):
        return [SemanticEvent("runtime.metadata", "运行元数据已更新", source)]

    return [
        SemanticEvent(
            "runtime.unknown",
            "收到尚未映射的 Agent 运行事件",
            {**source, "status": status, "source_keys": sorted(str(key) for key in event)},
        )
    ]


def adapt_open_code_event(event: dict[str, Any]) -> SemanticEvent | None:
    """Backward-compatible primary semantic event accessor."""
    events = adapt_open_code_events(event)
    return events[0] if events else None


def extract_inquiry(event: dict[str, Any]) -> dict[str, Any] | None:
    semantic = next(
        (item for item in adapt_open_code_events(event) if item.event_type == "inquiry.requested"),
        None,
    )
    if not semantic:
        return None
    return {
        "blocking_reason": semantic.payload.get("description") or "Agent 需要用户输入",
        "completed_work": "已保留当前执行上下文",
        "current_state": "等待用户回答",
        "requested_input": semantic.summary,
        "options": semantic.payload.get("options") or [],
    }


def extract_agent_text(event: dict[str, Any]) -> str:
    semantic = next(
        (item for item in adapt_open_code_events(event) if item.event_type == "message.agent"),
        None,
    )
    return semantic.summary if semantic else ""
