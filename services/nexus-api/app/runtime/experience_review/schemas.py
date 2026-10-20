"""内部协议：轨迹事件、局部观察、经验候选。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TraceEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ref: str
    source_system: str = "opencode"
    session_id: str
    message_id: str | None = None
    part_id: str | None = None
    timestamp: str | None = None
    role: str = ""
    kind: str
    tool: str | None = None
    status: str | None = None
    exit_code: int | None = None
    content_digest: str = ""
    excerpt: str = ""


class TraceResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    events: list[TraceEvent]
    sessions: list[str]
    source_digest: str
    message_count: int
    watermarks: dict[str, str] = Field(default_factory=dict)

    @property
    def evidence(self) -> dict[str, TraceEvent]:
        return {event.ref: event for event in self.events}


class Observation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    problem: str = ""
    attempt: str = ""
    outcome: str = ""
    correction: str = ""
    verification: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class MapResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    observations: list[Observation] = Field(default_factory=list)


class Candidate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: Literal["domain", "harness_improvement"] = "domain"
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(default="", max_length=4000)
    content_md: str = Field(min_length=1)
    experience_type: str = "domain"
    scope: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    target_component: str | None = None
    proposed_change: str | None = None


class CandidateList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    candidates: list[Candidate] = Field(default_factory=list)
    no_findings_reason: str | None = None


class SegmentInput(BaseModel):
    index: int
    events: list[TraceEvent]
    token_estimate: int
    digest: str
