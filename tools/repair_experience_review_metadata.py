"""按明确 Job 修复已完成复盘的旧错误和缺失证据关联；默认只预览。

必须通过环境变量 ASTRA_DATABASE_URL 指定目标库，显式 --apply 才提交。
不推断未知来源，不改候选正文、任务状态、原生轨迹或 AgentScope 数据。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/nexus-api"))


async def repair(job_id: str, apply: bool) -> dict:
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import ExperienceEvidence, ExperienceReviewJob

    async with SessionLocal() as db:
        job = await db.get(ExperienceReviewJob, job_id)
        if job is None or job.status not in {"SUCCEEDED", "NO_FINDINGS"}:
            raise ValueError("只允许修复已完成的明确复盘 Job")
        sources = set(job.source_snapshot.get("source_session_ids") or [])
        sources.add(job.root_session_id)
        rows = list(
            (
                await db.scalars(
                    select(ExperienceEvidence).where(
                        ExperienceEvidence.knowledge_id.in_(job.candidate_ids or []),
                        ExperienceEvidence.task_id == job.task_id_snapshot,
                        ExperienceEvidence.source_session_id == job.root_session_id,
                        ExperienceEvidence.native_session_id.in_(sources),
                        ExperienceEvidence.validation_status == "validated",
                        ExperienceEvidence.review_job_id.is_(None),
                    )
                )
            ).all()
        )
        clear_error = bool(job.error_code or job.error_message)
        result = {
            "job_id": job.id,
            "status": job.status,
            "clear_old_error": clear_error,
            "evidence_ids": [row.id for row in rows],
            "applied": apply,
        }
        if apply:
            job.error_code = None
            job.error_message = None
            for row in rows:
                row.review_job_id = job.id
            await db.commit()
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not os.environ.get("ASTRA_DATABASE_URL"):
        parser.error("必须显式配置 ASTRA_DATABASE_URL；拒绝使用默认库")
    print(json.dumps(asyncio.run(repair(args.job_id, args.apply)), ensure_ascii=False, indent=2))
