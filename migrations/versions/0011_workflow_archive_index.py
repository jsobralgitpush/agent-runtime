"""Index workflow archive-state filtering."""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_workflows_archived_at",
        "workflows",
        ["archived_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_workflows_archived_at", table_name="workflows")
