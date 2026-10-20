from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.executors.base import TokenUsage, ToolUsageResult

_TOKEN_COMPONENTS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
_BREAKDOWN_FIELDS = (*_TOKEN_COMPONENTS, "total_tokens", "llm_call_count", "tool_call_count")


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return None


def _token_values(payload: dict[str, Any]) -> tuple[int | None, int | None, int | None, int | None]:
    cache = payload.get("cache") if isinstance(payload.get("cache"), dict) else {}
    input_tokens = _integer(payload.get("input_tokens", payload.get("input")))
    output_tokens = _integer(payload.get("output_tokens", payload.get("output")))
    cache_read = _integer(payload.get("cache_read_tokens", payload.get("cacheRead", cache.get("read"))))
    cache_write = _integer(payload.get("cache_write_tokens", payload.get("cacheWrite", cache.get("write"))))
    return input_tokens, output_tokens, cache_read, cache_write


def _find_tokens(event: dict[str, Any]) -> tuple[int | None, int | None, int | None, int | None]:
    candidates: list[dict[str, Any]] = []
    info = event.get("info")
    if isinstance(info, dict):
        for key in ("tokens", "usage"):
            if isinstance(info.get(key), dict):
                candidates.append(info[key])
    part = event.get("part")
    if isinstance(part, dict):
        for key in ("tokens", "usage"):
            if isinstance(part.get(key), dict):
                candidates.append(part[key])
        candidates.append(part)
    for key in ("tokens", "usage"):
        if isinstance(event.get(key), dict):
            candidates.append(event[key])
    candidates.append(event)
    for candidate in candidates:
        values = _token_values(candidate)
        if any(value is not None for value in values):
            return values
    return None, None, None, None


def _deep_value(value: Any, keys: tuple[str, ...]) -> str | None:
    if isinstance(value, dict):
        for key in keys:
            if value.get(key) not in (None, ""):
                return str(value[key])
        for child in value.values():
            found = _deep_value(child, keys)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = _deep_value(child, keys)
            if found:
                return found
    return None


def _find_value(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if candidate not in (None, ""):
                return candidate
        for child in value.values():
            found = _find_value(child, keys)
            if found is not None:
                return found
    if isinstance(value, list):
        for child in value:
            found = _find_value(child, keys)
            if found is not None:
                return found
    return None


def _normalize_model(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        model_id = (
            value.get("modelID")
            or value.get("model_id")
            or value.get("id")
            or value.get("name")
        )
        provider_id = value.get("providerID") or value.get("provider_id")
        if isinstance(model_id, str) and model_id:
            if isinstance(provider_id, str) and provider_id:
                return f"{provider_id}/{model_id}"
            return model_id
    return None


def _model_name(event: dict[str, Any]) -> str | None:
    # A structured `model` object can carry provider + id; prefer it so
    # "provider/model" survives instead of a bare modelID.
    structured = _normalize_model(_find_value(event, ("model",)))
    if structured:
        return structured
    # Flat provider/model ids (native assistant message info) must be joined.
    provider = _find_value(event, ("providerID", "provider_id"))
    model_id = _find_value(event, ("modelID", "model_id"))
    if isinstance(provider, str) and provider and isinstance(model_id, str) and model_id:
        return f"{provider}/{model_id}"
    direct = _find_value(event, ("modelID", "model_id", "modelName", "model_name"))
    if isinstance(direct, str) and direct:
        return direct
    return None


def _timestamp_ms(value: Any) -> int | None:
    number = _integer(value)
    if number is None or number <= 0:
        return None
    # Native OpenCode timestamps are epoch milliseconds; tolerate epoch seconds.
    return number if number > 100_000_000_000 else number * 1000


def parse_opencode_usage(
    events: list[dict[str, Any]],
) -> tuple[TokenUsage, list[ToolUsageResult], str | None]:
    token_totals: list[int | None] = [None, None, None, None]
    llm_calls = 0
    model_name: str | None = None
    tool_calls: dict[str, dict[str, Any]] = {}

    for index, event in enumerate(events):
        values = _find_tokens(event)
        if any(value is not None for value in values):
            llm_calls += 1
            for position, value in enumerate(values):
                if value is not None:
                    token_totals[position] = (token_totals[position] or 0) + value
        model_name = model_name or _model_name(event)

        part = event.get("part") if isinstance(event.get("part"), dict) else event
        event_type = str(event.get("type", "")).lower()
        part_type = str(part.get("type", "")).lower() if isinstance(part, dict) else ""
        if "tool" not in event_type and part_type not in {"tool", "tool_use", "tool-call"}:
            continue
        name = _deep_value(part, ("tool", "toolName", "tool_name", "name")) or "其他"
        call_id = _deep_value(part, ("callID", "call_id", "toolCallID", "tool_call_id", "id"))
        key = call_id or f"event-{index}"
        state = part.get("state") if isinstance(part, dict) and isinstance(part.get("state"), dict) else {}
        status = str(state.get("status", part.get("status", event.get("status", "unknown")))).lower()
        timing = state.get("time") if isinstance(state.get("time"), dict) else {}
        start = _integer(timing.get("start"))
        end = _integer(timing.get("end"))
        duration = max(0, end - start) if start is not None and end is not None else 0
        tool_calls[key] = {"name": name, "status": status, "duration_ms": duration}

    grouped: dict[str, dict[str, int]] = defaultdict(
        lambda: {"call_count": 0, "success_count": 0, "failure_count": 0, "duration_ms": 0}
    )
    for call in tool_calls.values():
        item = grouped[call["name"]]
        item["call_count"] += 1
        item["duration_ms"] += call["duration_ms"]
        if call["status"] in {"completed", "success", "succeeded", "done"}:
            item["success_count"] += 1
        elif call["status"] in {"error", "failed", "failure"}:
            item["failure_count"] += 1

    total_tokens = (
        sum(value or 0 for value in token_totals)
        if any(value is not None for value in token_totals)
        else None
    )
    token_usage = TokenUsage(
        input_tokens=token_totals[0],
        output_tokens=token_totals[1],
        cache_read_tokens=token_totals[2],
        cache_write_tokens=token_totals[3],
        total_tokens=total_tokens,
        llm_call_count=llm_calls if llm_calls else None,
    )
    tools = [ToolUsageResult(name=name, **values) for name, values in sorted(grouped.items())]
    return token_usage, tools, model_name


def _message_local_date(created_ms: int | None, timezone: ZoneInfo) -> str | None:
    if created_ms is None:
        return None
    return datetime.fromtimestamp(created_ms / 1000, tz=UTC).astimezone(timezone).date().isoformat()


def _breakdown_entry(key_label: str, key: str) -> dict[str, Any]:
    return {key_label: key, **{field: 0 for field in _BREAKDOWN_FIELDS}}


def parse_usage_breakdowns(
    messages: list[dict[str, Any]],
    *,
    timezone: ZoneInfo,
    fallback_date: str,
    fallback_model: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Bucket native token/tool usage by the real message day and real model.

    Tokens live on assistant message ``info``; the message timestamp and model are
    read from the same record, so no aggregate is attributed to the Session day.
    """

    days: dict[str, dict[str, Any]] = {}
    models: dict[str, dict[str, Any]] = {}

    for message in messages:
        if not isinstance(message, dict):
            continue
        info = message.get("info") if isinstance(message.get("info"), dict) else {}
        if str(info.get("role", "")).lower() != "assistant":
            continue
        values = _find_tokens(message)
        has_tokens = any(value is not None for value in values)
        parts = message.get("parts") if isinstance(message.get("parts"), list) else []
        tool_count = sum(
            1
            for part in parts
            if isinstance(part, dict) and "tool" in str(part.get("type", "")).lower()
        )
        if not has_tokens and not tool_count:
            continue
        timing = info.get("time") if isinstance(info.get("time"), dict) else {}
        day = _message_local_date(_timestamp_ms(timing.get("created")), timezone) or fallback_date
        model = _model_name(message) or fallback_model or "未知"

        for target, key, label in ((days, day, "date"), (models, model, "model_name")):
            entry = target.get(key)
            if entry is None:
                entry = _breakdown_entry(label, key)
                target[key] = entry
            for position, field in enumerate(_TOKEN_COMPONENTS):
                value = values[position]
                if value is not None:
                    entry[field] += value
            if has_tokens:
                entry["llm_call_count"] += 1
                entry["total_tokens"] += sum(value for value in values if value is not None)
            entry["tool_call_count"] += tool_count

    return (
        [days[key] for key in sorted(days)],
        sorted(models.values(), key=lambda item: item["total_tokens"], reverse=True),
    )


def parse_execution_seconds(messages: list[dict[str, Any]]) -> int:
    total_ms = 0
    for message in messages:
        info = message.get("info") if isinstance(message, dict) else None
        if not isinstance(info, dict) or str(info.get("role", "")).lower() != "assistant":
            continue
        timing = info.get("time") if isinstance(info.get("time"), dict) else {}
        start = _timestamp_ms(timing.get("created"))
        end = _timestamp_ms(timing.get("completed"))
        if start is not None and end is not None:
            total_ms += max(0, end - start)
    return max(0, int(total_ms / 1000))
