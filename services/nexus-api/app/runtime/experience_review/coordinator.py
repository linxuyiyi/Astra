"""复盘作业：创建、认领、租约、分段执行、归纳与写入。"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.database import SessionLocal, worker_session
from app.models import (
    Event,
    ExperienceReviewJob,
    ExperienceReviewSegment,
    KnowledgeItem,
    KnowledgeUsage,
    Task,
)
from app.runtime.experience_review.candidate_writer import write_candidates
from app.runtime.experience_review.evidence import validate_candidates
from app.runtime.experience_review.llm_client import ReviewLLMClient, ReviewLLMError
from app.runtime.experience_review.map_analyzer import analyze_segment
from app.runtime.experience_review.reduce_analyzer import reduce_observations
from app.runtime.experience_review.schemas import Observation, SegmentInput
from app.runtime.experience_review.trace_reader import TraceTooLarge, TraceUnavailable, read_trace
from app.runtime.experience_review.trace_segmenter import segment_events
from app.runtime.redaction import redact_text

logger = logging.getLogger(__name__)
PROMPT_VERSION = "2"

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
        prompt_version=PROMPT_VERSION,
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
            prompt_version=PROMPT_VERSION,
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


async def _read_experiences(session: AsyncSession, job: ExperienceReviewJob) -> list[dict[str, str]]:
    source_ids = set(job.source_snapshot.get("source_session_ids") or [])
    source_ids.add(job.root_session_id)
    items = await session.scalars(
        select(KnowledgeItem)
        .join(KnowledgeUsage, KnowledgeUsage.knowledge_id == KnowledgeItem.id)
        .where(
            KnowledgeUsage.task_id == job.task_id_snapshot,
            KnowledgeUsage.session_id.in_(source_ids),
            KnowledgeUsage.action == "read",
            KnowledgeItem.type == "experience",
            KnowledgeItem.status.in_(("candidate", "published")),
        )
        .distinct()
        .order_by(KnowledgeItem.updated_at.desc(), KnowledgeItem.id)
        .limit(8)
    )
    result: list[dict[str, str]] = []
    for item in items:
        entry = {
            "id": item.id,
            "title": redact_text(item.title),
            "summary": redact_text(item.summary)[:500],
            "content_md": redact_text(item.content_md)[:3000],
        }
        if len(json.dumps([*result, entry], ensure_ascii=False)) > 8000:
            break
        result.append(entry)
    return result


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


async def _advance(
    session: AsyncSession, job_id: str, token: str | None, **values: Any
) -> bool:
    """按作业 ID + 本次 claim token + 活动状态做条件更新。

    起始阶段推进（COLLECTING/ANALYZING）和终态写入都必须先确认仍有权写：
    任务新轮次可能刚好在 claim 与这次写入之间写入 SUPERSEDED 并清掉租约。
    这个条件更新同时开启真实写事务：SQLite 下库内的 ``begin_nested()`` 只有落在
    外层事务里才是 SAVEPOINT，否则释放即提交。
    """

    if not token:
        return False
    result = await session.execute(
        update(ExperienceReviewJob)
        .where(
            ExperienceReviewJob.id == job_id,
            ExperienceReviewJob.lease_token == token,
            ExperienceReviewJob.status.in_(ACTIVE_STATUSES),
        )
        .values(**values)
    )
    return result.rowcount == 1


async def _lease_holds(session: AsyncSession, job_id: str, token: str | None) -> bool:
    """确认自己仍拥有这次复盘，并顺带续租。"""

    return await _advance(
        session,
        job_id,
        token,
        lease_until=_now()
        + timedelta(seconds=get_settings().experience_review_lease_seconds),
    )


async def _current_job(session: AsyncSession, job_id: str) -> ExperienceReviewJob | None:
    """读回当前行并脱离 Session，避免 close/rollback 把返回对象变成过期状态。"""

    job = await session.get(ExperienceReviewJob, job_id, populate_existing=True)
    if job is not None:
        session.expunge(job)
    return job


async def _abandoned(session: AsyncSession, job: ExperienceReviewJob) -> ExperienceReviewJob:
    """失去所有权：放弃本次结果，返回数据库里的当前状态。"""

    job_id = job.id  # rollback 会让 ORM 实例过期，先取出主键。
    await session.rollback()
    current = await _current_job(session, job_id)
    return current or job


async def _finish(
    session: AsyncSession,
    job_id: str,
    token: str | None,
    status: str,
    *,
    zero_reason: str | None = None,
    candidate_ids: list[str] | None = None,
) -> ExperienceReviewJob | None:
    """按本次 claim 的 token 提交终态；失去所有权时不覆盖别人的结果。"""

    result = await session.execute(
        update(ExperienceReviewJob)
        .where(
            ExperienceReviewJob.id == job_id,
            ExperienceReviewJob.lease_token == token,
            ExperienceReviewJob.status.in_(ACTIVE_STATUSES),
        )
        .values(
            status=status,
            zero_reason=zero_reason,
            candidate_ids=candidate_ids or [],
            error_code=None,
            error_message=None,
            finished_at=_now(),
            lease_owner=None,
            lease_token=None,
            lease_until=None,
        )
    )
    if result.rowcount != 1:
        # 作业已被新轮次作废，或被接管的 Worker 写成了终态。
        await session.rollback()
        return await _current_job(session, job_id)
    await session.commit()
    return await _current_job(session, job_id)


async def _fail_or_retry(
    session: AsyncSession, job_id: str, token: str | None, code: str, message: str
) -> ExperienceReviewJob | None:
    """失败/重试同样按本次 claim 的 token 做条件更新。

    异常分支会在 rollback 后重读 Job；那里可能已经带着别人的 owner token，
    不能拿它代替自己 claim 的 token，否则会覆盖已作废或已完成的作业。
    """

    settings = get_settings()
    row = (
        await session.execute(
            select(ExperienceReviewJob.attempts, ExperienceReviewJob.phase).where(
                ExperienceReviewJob.id == job_id
            )
        )
    ).first()
    attempts = int(row.attempts or 0) + 1 if row is not None else 1
    phase = row.phase if row is not None else None
    if phase in {WRITING, VALIDATING}:
        phase = "REDUCE"
    values: dict[str, Any] = {
        "attempts": attempts,
        "error_code": code,
        "error_message": message[:2000],
        "phase": phase,
        "lease_owner": None,
        "lease_token": None,
        "lease_until": None,
    }
    if attempts >= settings.experience_review_max_attempts:
        values["status"] = FAILED
        values["finished_at"] = _now()
    else:
        delay = RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)]
        values["status"] = RETRY_WAIT
        values["next_retry_at"] = _now() + timedelta(seconds=delay)
    result = await session.execute(
        update(ExperienceReviewJob)
        .where(
            ExperienceReviewJob.id == job_id,
            ExperienceReviewJob.lease_token == token,
            ExperienceReviewJob.status.in_(ACTIVE_STATUSES),
        )
        .values(**values)
    )
    if result.rowcount != 1:
        await session.rollback()
        return await _current_job(session, job_id)
    await session.execute(
        update(ExperienceReviewSegment)
        .where(
            ExperienceReviewSegment.job_id == job_id,
            ExperienceReviewSegment.status.in_(("PENDING", "RETRY_WAIT", "ANALYZING")),
        )
        .values(lease_token=None, lease_until=None)
    )
    await session.commit()
    return await _current_job(session, job_id)


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
            # 本次 claim 的租约 token：所有终态/失败更新都必须带着它做条件更新，
            # 不能用 rollback 后重读到的别人 token。
            token = job.lease_token
            try:
                if job.phase in {PENDING, COLLECTING}:
                    # claim 与这次推进之间可能已被新轮次作废：先 CAS 再提交。
                    if not await _advance(
                        session, job_id, token, status=COLLECTING, phase=COLLECTING
                    ):
                        return await _abandoned(session, job)
                    await session.commit()
                    trace = await read_trace(job.source_snapshot)
                    if not await _lease_holds(session, job.id, token):
                        return await _abandoned(session, job)
                    if not trace.events:
                        return await _finish(
                            session, job.id, token, NO_FINDINGS, zero_reason="empty_trace"
                        )
                    await _create_segments(session, job, trace)
                    job.phase = "MAP"
                    job.status = ANALYZING
                    job.lease_owner = None
                    job.lease_token = None
                    job.lease_until = None
                    await session.commit()
                    return await _current_job(session, job_id)

                if job.phase == "MAP":
                    if not await _advance(session, job_id, token, status=ANALYZING):
                        return await _abandoned(session, job)
                    segment = await _next_segment(session, job.id)
                    if segment is None:
                        job.phase = "REDUCE"
                        job.lease_owner = None
                        job.lease_token = None
                        job.lease_until = None
                        await session.commit()
                        return await _current_job(session, job_id)
                    segment.lease_token = uuid4().hex
                    segment.lease_until = _now() + timedelta(
                        seconds=settings.experience_review_lease_seconds
                    )
                    await session.commit()
                    trace = await read_trace(job.source_snapshot)
                    mapped = await analyze_segment(
                        client, task_meta=_task_meta(job), segment=_segment_input(segment, trace)
                    )
                    if not await _lease_holds(session, job.id, token):
                        return await _abandoned(session, job)
                    segment.analysis_json = mapped.model_dump()
                    segment.status = "DONE"
                    segment.lease_token = None
                    segment.lease_until = None
                    job.completed_segment_count += 1
                    job.lease_owner = None
                    job.lease_token = None
                    job.lease_until = None
                    await session.commit()
                    return await _current_job(session, job_id)

                # WRITING/VALIDATING 是写入阶段留下的产物：原子提交后重算即可，
                # 不能因为没有分支直接判 unknown_phase。
                if job.phase in {"REDUCE", WRITING, VALIDATING}:
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
                        client,
                        task_meta=_task_meta(job),
                        observations=observations,
                        existing_experiences=await _read_experiences(session, job),
                    )
                    if not await _lease_holds(session, job.id, token):
                        return await _abandoned(session, job)
                    accepted, rejected = validate_candidates(
                        candidates, trace, limit=settings.experience_review_max_candidates
                    )
                    if rejected:
                        logger.info("Experience review %s rejected evidence: %s", job.id, rejected)
                    if not accepted:
                        return await _finish(
                            session,
                            job.id,
                            token,
                            NO_FINDINGS,
                            zero_reason="no_evidence_backed_candidate" if candidates else "no_findings",
                        )
                    if settings.experience_review_shadow_mode:
                        return await _finish(session, job.id, token, SUCCEEDED, zero_reason="shadow_mode")
                    # 候选、证据与作业结果必须在同一个外层事务里提交：先提交
                    # WRITING 会把“没有证据的候选”留在库里，崩溃后无法回滚。
                    if not await _lease_holds(session, job.id, token):
                        return await _abandoned(session, job)
                    job.status = WRITING
                    job.phase = WRITING
                    ids = await write_candidates(
                        session, job=job, trace=trace, candidates=accepted
                    )
                    return await _finish(session, job.id, token, SUCCEEDED, candidate_ids=ids)

                return await _finish(session, job.id, token, FAILED, zero_reason="unknown_phase")
            except TraceTooLarge as exc:
                await session.rollback()
                return await _finish(
                    session, job_id, token, SKIPPED_TOO_LARGE, zero_reason=str(exc)[:64]
                )
            except TraceUnavailable as exc:
                await session.rollback()
                return await _fail_or_retry(session, job_id, token, "source_unavailable", str(exc))
            except ReviewLLMError as exc:
                await session.rollback()
                return await _fail_or_retry(session, job_id, token, exc.code, str(exc))
            except Exception as exc:  # noqa: BLE001 - worker boundary
                logger.exception("Experience review job %s failed", job_id)
                await session.rollback()
                return await _fail_or_retry(session, job_id, token, "unexpected_error", str(exc))
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
