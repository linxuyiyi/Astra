"""把通过证据校验的候选写入数字图书馆。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.library_schemas import ExperienceEvidenceWrite, LibrarySubmitExperienceInput
from app.library_service import KnowledgeService, LibraryConflict, experience_fingerprint
from app.models import ExperienceReviewJob, KnowledgeItem, RuntimeSession

from .schemas import Candidate, TraceResult


def _evidence(candidate: Candidate, trace: TraceResult) -> list[ExperienceEvidenceWrite]:
    index = trace.evidence
    result: list[ExperienceEvidenceWrite] = []
    for ref in candidate.evidence_refs:
        event = index[ref]
        result.append(
            ExperienceEvidenceWrite(
                evidence_type=event.kind,
                content=event.excerpt,
                message_id=event.message_id,
                part_id=event.part_id,
                result=event.status,
                source_system=event.source_system,
                native_session_id=event.session_id,
                source_digest=event.content_digest,
                validation_status="validated",
            )
        )
    return result


async def write_candidates(
    session: AsyncSession,
    *,
    job: ExperienceReviewJob,
    trace: TraceResult,
    candidates: list[Candidate],
) -> list[str]:
    binding = await session.get(RuntimeSession, job.root_session_id)
    if binding is None:
        raise LibraryConflict("复盘来源 RuntimeSession 已不存在")
    service = KnowledgeService(session)
    ids: list[str] = []
    for candidate in candidates:
        payload = LibrarySubmitExperienceInput(
            title=candidate.title,
            summary=candidate.summary,
            content_md=candidate.content_md,
            experience_type=candidate.experience_type,
            scope=candidate.scope,
            tags=candidate.tags,
            evidence=_evidence(candidate, trace),
            experience_data={
                "kind": candidate.kind,
                "target_component": candidate.target_component,
                "proposed_change": candidate.proposed_change,
                "evidence_verified": True,
            },
        )
        try:
            detail = await service.mcp_submit_experience(
                payload,
                review_session=binding,
                review_key=job.review_key,
                commit=False,
            )
            ids.append(detail.id)
        except LibraryConflict:
            existing = await session.scalar(
                select(KnowledgeItem).where(
                    KnowledgeItem.dedupe_key == "global-experience",
                    KnowledgeItem.fingerprint == experience_fingerprint(candidate.content_md),
                )
            )
            if existing is None or existing.status not in {"candidate", "published"}:
                raise
            if existing.id not in ids:
                ids.append(existing.id)
    return ids
