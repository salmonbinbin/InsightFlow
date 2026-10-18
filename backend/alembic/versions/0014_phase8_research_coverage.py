"""Persist v5 task, coverage and safe execution trace; historical rows stay NULL."""

import sqlalchemy as sa

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("task_contract", "coverage_report", "execution_trace"):
        op.add_column("research_runs", sa.Column(name, sa.JSON(), nullable=True))


def downgrade() -> None:
    for name in ("execution_trace", "coverage_report", "task_contract"):
        op.drop_column("research_runs", name)
