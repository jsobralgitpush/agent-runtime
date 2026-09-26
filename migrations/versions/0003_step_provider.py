"""Record the provider used for each LLM step."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("step_runs", sa.Column("provider", sa.String(80), nullable=True))


def downgrade() -> None:
    op.drop_column("step_runs", "provider")
