"""Record workflow archive and restore transitions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    action = sa.Enum(
        "archived",
        "restored",
        name="workflowlifecycleaction",
        native_enum=False,
        create_constraint=True,
    )
    op.create_table(
        "workflow_lifecycle_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workflow_id",
            sa.String(36),
            sa.ForeignKey("workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", action, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_workflow_lifecycle_events_workflow_created_id",
        "workflow_lifecycle_events",
        ["workflow_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workflow_lifecycle_events_workflow_created_id",
        table_name="workflow_lifecycle_events",
    )
    op.drop_table("workflow_lifecycle_events")
