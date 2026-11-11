"""Index action-filtered workflow lifecycle events."""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_workflow_lifecycle_events_workflow_action_created_id",
        "workflow_lifecycle_events",
        ["workflow_id", "action", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workflow_lifecycle_events_workflow_action_created_id",
        table_name="workflow_lifecycle_events",
    )
