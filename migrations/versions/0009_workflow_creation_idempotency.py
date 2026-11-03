"""Add database-enforced workflow creation idempotency."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workflows",
        sa.Column("idempotency_key", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_workflows_idempotency_key",
        "workflows",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_workflows_idempotency_key", table_name="workflows")
    op.drop_column("workflows", "idempotency_key")
