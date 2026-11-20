"""Record expired workflow run recovery decisions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    outcome = sa.Enum(
        "requeued",
        "failed",
        name="runrecoveryoutcome",
        native_enum=False,
        create_constraint=True,
    )
    op.create_table(
        "workflow_run_recovery_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("outcome", outcome, nullable=False),
        sa.Column("expired_lease_owner", sa.String(255), nullable=True),
        sa.Column("lease_expired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_workflow_run_recovery_events_run_created_id",
        "workflow_run_recovery_events",
        ["run_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workflow_run_recovery_events_run_created_id",
        table_name="workflow_run_recovery_events",
    )
    op.drop_table("workflow_run_recovery_events")
