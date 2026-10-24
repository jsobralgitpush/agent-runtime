"""Index status-filtered workflow run history."""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_workflow_runs_workflow_status_created_id",
        "workflow_runs",
        ["workflow_id", "status", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workflow_runs_workflow_status_created_id",
        table_name="workflow_runs",
    )
