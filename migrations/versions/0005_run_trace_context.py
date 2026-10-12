"""Persist W3C trace context for deferred workflow runs."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("trace_context", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_runs", "trace_context")
