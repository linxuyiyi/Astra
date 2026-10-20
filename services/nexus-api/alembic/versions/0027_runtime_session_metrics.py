"""Add aggregate native Session count and execution seconds to RuntimeSession."""

from alembic import op
import sqlalchemy as sa

revision = "0027_runtime_session_metrics"
down_revision = "0026_runtime_recovery_facts"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("runtime_sessions") as batch:
        batch.add_column(
            sa.Column(
                "native_session_count",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )
        batch.add_column(sa.Column("execution_seconds", sa.Integer(), nullable=True))

    # Existing rows predate native child Session aggregation and always had one root.
    op.get_bind().execute(
        sa.text("UPDATE runtime_sessions SET native_session_count = 1")
    )


def downgrade():
    with op.batch_alter_table("runtime_sessions") as batch:
        batch.drop_column("execution_seconds")
        batch.drop_column("native_session_count")