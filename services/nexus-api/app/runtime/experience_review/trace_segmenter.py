"""按因果边界和 Token 预算切分轨迹，片段之间只传结构化状态。"""

from __future__ import annotations

import hashlib
from json import dumps

from app.config.settings import get_settings

from .schemas import SegmentInput, TraceEvent

BOUNDARY_KINDS = {"error", "human_correction", "verification"}


def estimate_tokens(events: list[TraceEvent]) -> int:
    chars = sum(len(event.excerpt) for event in events)
    return max(1, chars // 4)


def segment_events(events: list[TraceEvent], *, budget_tokens: int | None = None) -> list[SegmentInput]:
    settings = get_settings()
    budget = budget_tokens or settings.experience_review_map_input_tokens
    segments: list[SegmentInput] = []
    current: list[TraceEvent] = []
    current_tokens = 0

    def flush() -> None:
        nonlocal current, current_tokens
        if not current:
            return
        index = len(segments)
        payload = dumps([event.model_dump() for event in current], ensure_ascii=False, sort_keys=True)
        segments.append(
            SegmentInput(
                index=index,
                events=current,
                token_estimate=max(1, current_tokens),
                digest=hashlib.sha256(payload.encode()).hexdigest(),
            )
        )
        current = []
        current_tokens = 0

    for event in events:
        event_tokens = max(1, len(event.excerpt) // 4)
        if current and current_tokens + event_tokens > budget:
            flush()
        current.append(event)
        current_tokens += event_tokens
        if event.kind in BOUNDARY_KINDS and current_tokens >= budget // 2:
            flush()
    flush()
    return segments
