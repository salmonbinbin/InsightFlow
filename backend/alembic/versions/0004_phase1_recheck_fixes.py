"""Phase 1 recheck: snapshot retry input set.

The 0003 migration already repairs any pre-existing counter inconsistency
(see :mod:`0003_phase1_run_tracking`). This migration only adds the JSON
column that snapshots the feedback external IDs captured at run
creation so a retry cannot expand its input set when new feedback rows
are imported into the same project.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "analysis_runs",
        sa.Column(
            "input_feedback_external_ids",
            sa.JSON(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("analysis_runs", "input_feedback_external_ids")
