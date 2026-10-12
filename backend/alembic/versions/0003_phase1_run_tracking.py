"""Phase 1 follow-up: tighten analysis_runs counters and add per-attempt accounting.

Adds ``attempt_count`` and ``unknown_token_calls`` so the runner can record
real LLM call counts (success + retries) and the number of attempts whose token
usage was not reported by the upstream. Tightens the
``ck_analysis_runs_non_negative_counts`` check to also enforce the
``success + failed <= processed <= input`` invariant on every committed row.

A new ``ck_run_failure_attempts_positive`` constraint rejects malformed
``attempts = 0`` rows on the failure table; the runner now stamps at least 1
attempt when it creates a failure record.

To make the upgrade robust against legacy 0002 rows whose counters drift
outside the new invariant (e.g. ``processed_count = 0, success_count = 1``
left over from the original concurrency defect), we first clamp the
counters into a state the new CHECK accepts. Raw feedback and analysis
rows are never modified — only the counters on the run are repaired.
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    # Repair pre-existing inconsistent counter rows so the new CHECK
    # constraint below can be applied to legacy databases without dropping
    # feedback or analysis rows. Some old rows also undercount input_count.
    op.execute(
        sa.text(
            "UPDATE analysis_runs SET input_count = success_count + failed_count "
            "WHERE input_count < success_count + failed_count"
        )
    )
    op.execute(
        sa.text(
            "UPDATE analysis_runs "
            "SET processed_count = LEAST(GREATEST(processed_count, "
            "success_count + failed_count), input_count) "
            "WHERE processed_count < success_count + failed_count "
            "   OR processed_count > input_count"
        )
    )
    op.execute(
        sa.text(
            "UPDATE analysis_run_failures SET attempts = 1 WHERE attempts < 1"
        )
    )

    op.add_column(
        "analysis_runs",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "analysis_runs",
        sa.Column(
            "unknown_token_calls",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    # Replace the looser non-negative check with one that also enforces the
    # invariant success + failed <= processed <= input. Existing rows pass
    # because the runner never produced negative or overflowed counters.
    op.drop_constraint(
        "ck_analysis_runs_non_negative_counts", "analysis_runs", type_="check"
    )
    op.create_check_constraint(
        "ck_analysis_runs_non_negative_counts",
        "analysis_runs",
        (
            "input_count >= 0 AND processed_count >= 0 AND success_count >= 0 "
            "AND failed_count >= 0 AND retry_count >= 0 AND latency_ms >= 0 "
            "AND attempt_count >= 0 AND unknown_token_calls >= 0 "
            "AND processed_count <= input_count "
            "AND success_count + failed_count <= processed_count"
        ),
    )

    op.create_check_constraint(
        "ck_run_failure_attempts_positive",
        "analysis_run_failures",
        "attempts >= 1",
    )


def downgrade():
    op.drop_constraint(
        "ck_run_failure_attempts_positive", "analysis_run_failures", type_="check"
    )
    op.drop_constraint(
        "ck_analysis_runs_non_negative_counts", "analysis_runs", type_="check"
    )
    op.create_check_constraint(
        "ck_analysis_runs_non_negative_counts",
        "analysis_runs",
        (
            "input_count >= 0 AND processed_count >= 0 AND success_count >= 0 "
            "AND failed_count >= 0 AND retry_count >= 0 AND latency_ms >= 0"
        ),
    )
    op.drop_column("analysis_runs", "unknown_token_calls")
    op.drop_column("analysis_runs", "attempt_count")
