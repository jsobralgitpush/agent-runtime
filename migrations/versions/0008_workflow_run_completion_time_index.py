"""Index completion-time-filtered workflow run history."""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_workflow_runs_workflow_completed_at",
        "workflow_runs",
        ["workflow_id", "completed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workflow_runs_workflow_completed_at",
        table_name="workflow_runs",
    )
