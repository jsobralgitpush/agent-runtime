"""Index workflow run history pagination."""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_workflow_runs_workflow_created_id",
        "workflow_runs",
        ["workflow_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_runs_workflow_created_id", table_name="workflow_runs")
