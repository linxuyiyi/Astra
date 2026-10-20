"""复盘作业：创建、认领、租约、分段执行、归纳与写入。"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.database import SessionLocal, worker_session
from app.models import Event, ExperienceReviewJob, ExperienceReviewSegment, Task
from app.runtime.experience_review.candidate_writer import write_candidates
from app.runtime.experience_review.evidence import validate_candidates
from app.runtime.experience_review.llm_client import ReviewLLMClient, ReviewLLMError
from app.runtime.experience_review.map_analyzer import analyze_segment
from app.runtime.experience_review.reduce_analyzer import reduce_observations
from app.runtime.experience_review.schemas import Observation, SegmentInput
from app.runtime.experience_review.trace_reader import TraceTooLarge, TraceUnavailable, read_trace
from app.runtime.experience_review.trace_segmenter import segment_events

logger = logging.getLogger(__name__)

PENDING = "PENDING"
COLLECTING = "COLLECTING"
ANALYZING = "ANALYZING"
VALIDATING = "VALIDATING"
WRITING = "WRITING"
SUCCEEDED = "SUCCEEDED"
NO_FINDINGS = "NO_FINDINGS"
RETRY_WAIT = "RETRY_WAIT"
FAILED = "FAILED"
SUPERSEDED = "SUPERSEDED"
SKIPPED_SOURCE_MISSING = "SKIPPED_SOURCE_MISSING"
SKIPPED_TOO_LARGE = "SKIPPED_TOO_LARGE"

ACTIVE_STATUSES = (PENDING, COLLECTING, ANALYZING, VALIDATING, WRITING, RETRY_WAIT)
MAX_ATTEMPTS = 3
RETRY_DELAYS = (30, 120, 300)


def review_key(event_id: int) -> str:
    return hashlib.sha256(f"experience-review:v1:{event_id}".encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


async def enqueue_in_transaction(
    session: AsyncSession,
    *,
    task: Task,
    event: Event,
    snapshot: dict[str, Any],
) -> ExperienceReviewJob:
    """在确认 Task 的同一事务里创建 Job；重复确认返回已有 Job。"""

    existing = await session.scalar(
        select(ExperienceReviewJob).where(ExperienceReviewJob.confirmation_event_id == event.id)
    )
    if existing is not None:
        return existing
    job = ExperienceReviewJob(
        id="xrj_" + uuid4().hex,
        confirmation_event_id=event.id,
        review_key=review_key(event.id),
        task_id=task.id,
        task_id_snapshot=task.id,
        root_session_id=str(snapshot["root_session_id"]),
        employee_id_snapshot=task.employee_id,
        turn_anchor_id=snapshot.get("turn_anchor_id"),
        confirmed_at=datetime.fromisoformat(str(snapshot["confirmed_at"])),
        source_snapshot=snapshot,
        status=PENDING,
        phase=PENDING,
        attempts=0,
        next_retry_at=_now(),
        source_message_count=0,
        segment_count=0,
        completed_segment_count=0,
        candidate_ids=[],
        prompt_version="1",
    )
    session.add(job)
    await session.flush()
    return job


async def claim_job(session: AsyncSession, job_id: str, owner: str) -> ExperienceReviewJob | None:
    now = _now()
    token = uuid4().hex
    result = await session.execute(
        update(ExperienceReviewJob)
        .where(
            ExperienceReviewJob.id == job_id,
            ExperienceReviewJob.status.in_(ACTIVE_STATUSES),
            or_(ExperienceReviewJob.lease_until.is_(None), ExperienceReviewJob.lease_until < now),
            or_(ExperienceReviewJob.status != RETRY_WAIT, ExperienceReviewJob.next_retry_at <= now),
        )
        .values(
            lease_owner=owner,
            lease_token=token,
            lease_until=now + timedelta(seconds=get_settings().experience_review_lease_seconds),
        )
    )
    if result.rowcount != 1:
        await session.rollback()
        return None
    await session.commit()
    return await session.get(ExperienceReviewJob, job_id)


async def retry_job(session: AsyncSession, job_id: str) -> ExperienceReviewJob | None:
    job = await session.get(ExperienceReviewJob, job_id)
    if job is None or job.status not in {FAILED, NO_FINDINGS, SKIPPED_SOURCE_MISSING}:
        return None
    job.status = PENDING
    job.phase = PENDING
    job.attempts = 0
    job.next_retry_at = _now()
    job.error_code = None
    job.error_message = None
    job.zero_reason = None
    job.finished_at = None
    job.lease_owner = None
    job.lease_token = None
    job.lease_until = None
    await session.commit()
    return job


def _task_meta(job: ExperienceReviewJob) -> dict[str, Any]:
    task = job.source_snapshot.get("task") if isinstance(job.source_snapshot, dict) else {}
    return task if isinstance(task, dict) else {}


async def _fail_or_retry(
    session: AsyncSession, job: ExperienceReviewJob, code: str, message: str
) -> None:
    settings = get_settings()
    job.attempts += 1
    job.error_code = code
    job.error_message = message[:2000]
    if job.phase in {WRITING, VALIDATING}:
        job.phase = "REDUCE"
    job.lease_owner = None
    job.lease_token = None
    job.lease_until = None
    if job.attempts >= settings.experience_review_max_attempts:
        job.status = FAILED
        job.finished_at = _now()
    else:
        job.status = RETRY_WAIT
        delay = RETRY_DELAYS[min(job.attempts - 1, len(RETRY_DELAYS) - 1)]
        job.next_retry_at = _now() + timedelta(seconds=delay)
    await session.execute(
        update(ExperienceReviewSegment)
        .where(
            ExperienceReviewSegment.job_id == job.id,
            ExperienceReviewSegment.status.in_(("PENDING", "RETRY_WAIT", "ANALYZING")),
        )
        .values(lease_token=None, lease_until=None)
    )
    await session.commit()


async def _create_segments(session: AsyncSession, job: ExperienceReviewJob, trace) -> None:
    existing = await session.scalar(
        select(ExperienceReviewSegment.id).where(ExperienceReviewSegment.job_id == job.id).limit(1)
    )
    if existing is not None:
        return
    segments = segment_events(trace.events)
    now = _now()
    for segment in segments:
        session.add(
            ExperienceReviewSegment(
                job_id=job.id,
                segment_index=segment.index,
                source_refs=[event.ref for event in segment.events],
                input_digest=segment.digest,
                token_estimate=segment.token_estimate,
                status=PENDING,
                attempts=0,
                next_retry_at=now,
            )
        )
    job.segment_count = len(segments)
    job.source_message_count = trace.message_count
    job.source_digest = trace.source_digest
    await session.flush()


def _segment_input(segment: ExperienceReviewSegment, trace) -> SegmentInput:
    index = trace.evidence
    events = [index[ref] for ref in segment.source_refs if ref in index]
    return SegmentInput(
        index=segment.segment_index,
        events=events,
        token_estimate=segment.token_estimate,
        digest=segment.input_digest,
    )


async def _next_segment(session: AsyncSession, job_id: str) -> ExperienceReviewSegment | None:
    now = _now()
    return await session.scalar(
        select(ExperienceReviewSegment)
        .where(
            ExperienceReviewSegment.job_id == job_id,
            ExperienceReviewSegment.status.in_((PENDING, RETRY_WAIT)),
            ExperienceReviewSegment.next_retry_at <= now,
            or_(
                ExperienceReviewSegment.lease_until.is_(None),
                ExperienceReviewSegment.lease_until < now,
            ),
        )
        .order_by(ExperienceReviewSegment.segment_index)
        .limit(1)
    )


async def _finish(
    session: AsyncSession,
    job: ExperienceReviewJob,
    status: str,
    *,
    zero_reason: str | None = None,
    candidate_ids: list[str] | None = None,
) -> None:
    job.status = status
    job.zero_reason = zero_reason
    job.candidate_ids = candidate_ids or []
    job.finished_at = _now()
    job.lease_owner = None
    job.lease_token = None
    job.lease_until = None
    await session.commit()


async def process_job(job_id: str, *, llm: ReviewLLMClient | None = None) -> ExperienceReviewJob | None:
    settings = get_settings()
    if not settings.experience_review_enabled:
        return None
    owner = f"exp-review-{uuid4().hex[:12]}"
    client = llm or ReviewLLMClient()
    try:
        async with worker_session() as session:
            job = await claim_job(session, job_id, owner)
            if job is None:
                return None
            try:
                if job.phase in {PENDING, COLLECTING}:
                    job.status = COLLECTING
                    job.phase = COLLECTING
                    await session.commit()
                    trace = await read_trace(job.source_snapshot)
                    if not trace.events:
                        await _finish(session, job, NO_FINDINGS, zero_reason="empty_trace")
                        return job
                    await _create_segments(session, job, trace)
                    job.phase = "MAP"
                    job.status = ANALYZING
                    job.lease_owner = None
                    job.lease_token = None
                    job.lease_until = None
                    await session.commit()
                    return job

                if job.phase == "MAP":
                    job.status = ANALYZING
                    segment = await _next_segment(session, job.id)
                    if segment is None:
                        job.phase = "REDUCE"
                        job.lease_owner = None
                        job.lease_token = None
                        job.lease_until = None
                        await session.commit()
                        return job
                    segment.lease_token = uuid4().hex
                    segment.lease_until = _now() + timedelta(
                        seconds=settings.experience_review_lease_seconds
                    )
                    await session.commit()
                    trace = await read_trace(job.source_snapshot)
                    mapped = await analyze_segment(
                        client, task_meta=_task_meta(job), segment=_segment_input(segment, trace)
                    )
                    segment.analysis_json = mapped.model_dump()
                    segment.status = "DONE"
                    segment.lease_token = None
                    segment.lease_until = None
                    job.completed_segment_count += 1
                    job.lease_owner = None
                    job.lease_token = None
                    job.lease_until = None
                    await session.commit()
                    return job

                if job.phase == "REDUCE":
                    trace = await read_trace(job.source_snapshot)
                    rows = (
                        await session.scalars(
                            select(ExperienceReviewSegment)
                            .where(
                                ExperienceReviewSegment.job_id == job.id,
                                ExperienceReviewSegment.status == "DONE",
                            )
                            .order_by(ExperienceReviewSegment.segment_index)
                        )
                    ).all()
                    observations: list[Observation] = []
                    for row in rows:
                        payload = row.analysis_json or {}
                        for item in payload.get("observations", []):
                            try:
                                observations.append(Observation.model_validate(item))
                            except Exception:
                                continue
                    candidates = await reduce_observations(
                        client, task_meta=_task_meta(job), observations=observations
                    )
                    accepted, rejected = validate_candidates(
                        candidates, trace, limit=settings.experience_review_max_candidates
                    )
                    if rejected:
                        logger.info("Experience review %s rejected evidence: %s", job.id, rejected)
                    if not accepted:
                        await _finish(
                            session,
                            job,
                            NO_FINDINGS,
                            zero_reason="no_evidence_backed_candidate" if candidates else "no_findings",
                        )
                        return job
                    if settings.experience_review_shadow_mode:
                        await _finish(session, job, SUCCEEDED, zero_reason="shadow_mode")
                        return job
                    job.status = WRITING
                    job.phase = WRITING
                    await session.commit()
                    ids = await write_candidates(
                        session, job=job, trace=trace, candidates=accepted
                    )
                    await _finish(session, job, SUCCEEDED, candidate_ids=ids)
                    return job

                await _finish(session, job, FAILED, zero_reason="unknown_phase")
                return job
            except TraceTooLarge as exc:
                await session.rollback()
                job = await session.get(ExperienceReviewJob, job_id) or job
                await _finish(session, job, SKIPPED_TOO_LARGE, zero_reason=str(exc)[:64])
                return job
            except TraceUnavailable as exc:
                await session.rollback()
                job = await session.get(ExperienceReviewJob, job_id) or job
                await _fail_or_retry(session, job, "source_unavailable", str(exc))
                return job
            except ReviewLLMError as exc:
                await session.rollback()
                job = await session.get(ExperienceReviewJob, job_id) or job
                await _fail_or_retry(session, job, exc.code, str(exc))
                return job
            except Exception as exc:  # noqa: BLE001 - worker boundary
                logger.exception("Experience review job %s failed", job_id)
                await session.rollback()
                job = await session.get(ExperienceReviewJob, job_id) or job
                await _fail_or_retry(session, job, "unexpected_error", str(exc))
                return job
    finally:
        await client.aclose()


async def scan_jobs(limit: int = 20) -> list[str]:
    settings = get_settings()
    if not settings.experience_review_enabled:
        return []
    now = _now()
    async with SessionLocal() as session:
        jobs = (
            await session.scalars(
                select(ExperienceReviewJob)
                .where(
                    ExperienceReviewJob.status.in_(ACTIVE_STATUSES),
                    or_(
                        and_(
                            ExperienceReviewJob.status == RETRY_WAIT,
                            ExperienceReviewJob.next_retry_at <= now,
                        ),
                        and_(
                            ExperienceReviewJob.status != RETRY_WAIT,
                            or_(
                                ExperienceReviewJob.lease_until.is_(None),
                                ExperienceReviewJob.lease_until < now,
                            ),
                        ),
                    ),
                )
                .order_by(ExperienceReviewJob.next_retry_at)
                .limit(limit)
            )
        ).all()
    from app.runtime.celery_app import celery

    ids = [job.id for job in jobs]
    for job_id in ids:
        celery.send_task("nexus.experience-review.process", args=[job_id], queue="nexus-review")
    return ids
