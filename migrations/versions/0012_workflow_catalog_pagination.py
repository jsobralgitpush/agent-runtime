"""Index workflow catalog keyset pagination."""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_workflows_created_id",
        "workflows",
        ["created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_workflows_created_id", table_name="workflows")
