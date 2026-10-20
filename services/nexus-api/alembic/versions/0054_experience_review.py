"""Add durable experience review jobs, segments and native evidence metadata."""

import sqlalchemy as sa

from alembic import op

revision = "0054_experience_review"
down_revision = "0053_remove_platform_assistant"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "t_nexus_experience_review_jobs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("confirmation_event_id", sa.Integer(), nullable=False, unique=True),
        sa.Column("review_key", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "task_id",
            sa.String(64),
            sa.ForeignKey("t_nexus_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("task_id_snapshot", sa.String(64), nullable=False),
        sa.Column("root_session_id", sa.String(160), nullable=False),
        sa.Column("employee_id_snapshot", sa.String(64), nullable=True),
        sa.Column("turn_anchor_id", sa.String(160), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=False),
        sa.Column("source_snapshot", sa.JSON(), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(), nullable=False),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("source_message_count", sa.Integer(), nullable=False),
        sa.Column("segment_count", sa.Integer(), nullable=False),
        sa.Column("completed_segment_count", sa.Integer(), nullable=False),
        sa.Column("candidate_ids", sa.JSON(), nullable=False),
        sa.Column("zero_reason", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("model_id", sa.String(160), nullable=True),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_experience_review_status_retry",
        "t_nexus_experience_review_jobs",
        ["status", "next_retry_at"],
    )
    op.create_index(
        "ix_experience_review_status_lease",
        "t_nexus_experience_review_jobs",
        ["status", "lease_until"],
    )
    op.create_index(
        "ix_experience_review_task_confirmed",
        "t_nexus_experience_review_jobs",
        ["task_id_snapshot", "confirmed_at"],
    )

    op.create_table(
        "t_nexus_experience_review_segments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "job_id",
            sa.String(64),
            sa.ForeignKey("t_nexus_experience_review_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("segment_index", sa.Integer(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("token_estimate", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(), nullable=False),
        sa.Column("analysis_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("job_id", "segment_index", name="uq_experience_review_segment"),
    )
    op.create_index(
        "ix_experience_review_segment_job_status",
        "t_nexus_experience_review_segments",
        ["job_id", "status"],
    )

    op.add_column(
        "t_nexus_experience_evidence",
        sa.Column("source_system", sa.String(32), nullable=True),
    )
    op.add_column(
        "t_nexus_experience_evidence",
        sa.Column("native_session_id", sa.String(160), nullable=True),
    )
    op.add_column(
        "t_nexus_experience_evidence",
        sa.Column("source_digest", sa.String(64), nullable=True),
    )
    op.add_column(
        "t_nexus_experience_evidence",
        sa.Column("review_job_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "t_nexus_experience_evidence",
        sa.Column(
            "validation_status",
            sa.String(16),
            nullable=False,
            server_default="unverified",
        ),
    )


def downgrade():
    op.drop_column("t_nexus_experience_evidence", "validation_status")
    op.drop_column("t_nexus_experience_evidence", "review_job_id")
    op.drop_column("t_nexus_experience_evidence", "source_digest")
    op.drop_column("t_nexus_experience_evidence", "native_session_id")
    op.drop_column("t_nexus_experience_evidence", "source_system")
    op.drop_index(
        "ix_experience_review_segment_job_status",
        table_name="t_nexus_experience_review_segments",
    )
    op.drop_table("t_nexus_experience_review_segments")
    op.drop_index(
        "ix_experience_review_task_confirmed",
        table_name="t_nexus_experience_review_jobs",
    )
    op.drop_index(
        "ix_experience_review_status_lease",
        table_name="t_nexus_experience_review_jobs",
    )
    op.drop_index(
        "ix_experience_review_status_retry",
        table_name="t_nexus_experience_review_jobs",
    )
    op.drop_table("t_nexus_experience_review_jobs")
