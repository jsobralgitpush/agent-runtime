"""Add a waiting status for human approval gates."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_VALUES = ("pending", "running", "completed", "failed")
NEW_VALUES = ("pending", "running", "waiting", "completed", "failed")


def _status(values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(
        *values,
        name="runstatus",
        native_enum=False,
        create_constraint=True,
    )


def _replace_status(values: tuple[str, ...], previous: tuple[str, ...]) -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        for table_name in ("workflow_runs", "step_runs"):
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.alter_column(
                    "status",
                    existing_type=_status(previous),
                    type_=_status(values),
                    existing_nullable=False,
                )
        return

    allowed = ", ".join(f"'{value}'" for value in values)
    for table_name in ("workflow_runs", "step_runs"):
        op.drop_constraint("runstatus", table_name, type_="check")
        op.create_check_constraint(
            "runstatus",
            table_name,
            f"status IN ({allowed})",
        )


def upgrade() -> None:
    _replace_status(NEW_VALUES, OLD_VALUES)


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE step_runs SET status = 'failed', "
            "error = COALESCE(error, 'Approval interrupted by schema downgrade') "
            "WHERE status = 'waiting'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE workflow_runs SET status = 'failed', "
            "error = COALESCE(error, 'Approval interrupted by schema downgrade') "
            "WHERE status = 'waiting'"
        )
    )
    _replace_status(OLD_VALUES, NEW_VALUES)
