"""服务端证据校验：模型只能使用已分配且真实存在的 trace 引用。"""

from __future__ import annotations

import re

from .schemas import Candidate, TraceResult

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:api[_-]?key|secret|password)\s*[:=]\s*[^\s,;]{8,}", re.IGNORECASE),
)


class EvidenceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def validate_candidate(candidate: Candidate, trace: TraceResult) -> None:
    if not candidate.evidence_refs:
        raise EvidenceError("missing_evidence", "候选没有任何证据引用")
    index = trace.evidence
    for ref in candidate.evidence_refs:
        event = index.get(ref)
        if event is None:
            raise EvidenceError("unknown_trace_ref", f"证据引用不存在: {ref}")
        if event.session_id not in trace.sessions:
            raise EvidenceError("cross_session_ref", f"证据不属于来源 Session: {ref}")
        if not event.message_id:
            raise EvidenceError("missing_message_id", f"证据没有原生 message_id: {ref}")
        if event.part_id is None and event.kind in {"tool_call", "tool_result", "code_change"}:
            raise EvidenceError("missing_part_id", f"证据没有原生 part_id: {ref}")
    text = f"{candidate.title}\n{candidate.summary}\n{candidate.content_md}"
    if _contains_secret(text):
        raise EvidenceError("secret_detected", "候选包含疑似密钥或凭据")
    if candidate.kind == "harness_improvement" and not candidate.target_component:
        raise EvidenceError("missing_harness_target", "工作方法改进候选缺少 target_component")


def validate_candidates(
    candidates: list[Candidate], trace: TraceResult, *, limit: int
) -> tuple[list[Candidate], list[str]]:
    accepted: list[Candidate] = []
    rejected: list[str] = []
    seen_titles: set[str] = set()
    for candidate in candidates:
        if len(accepted) >= limit:
            break
        key = candidate.title.strip().casefold()
        if key in seen_titles:
            continue
        try:
            validate_candidate(candidate, trace)
        except EvidenceError as exc:
            rejected.append(f"{candidate.title}: {exc.code}")
            continue
        seen_titles.add(key)
        accepted.append(candidate)
    return accepted, rejected
