"""Index expired-owner-filtered workflow run recovery events."""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_workflow_run_recovery_events_run_owner_created_id",
        "workflow_run_recovery_events",
        ["run_id", "expired_lease_owner", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workflow_run_recovery_events_run_owner_created_id",
        table_name="workflow_run_recovery_events",
    )
