"""Backfill input snapshots for classification runs created before 0004.

The original run did not record its input IDs. Feedback rows already present
when the run started are the best recoverable approximation; successful and
failed rows are also included even if timestamps are inconsistent. No raw or
analysis row is changed.
"""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE analysis_runs AS run
            SET input_feedback_external_ids = (
                SELECT COALESCE(json_agg(f.external_id ORDER BY f.created_at, f.id), '[]'::json)
                FROM feedbacks AS f
                WHERE f.project_id = run.project_id
                  AND (
                      f.created_at <= COALESCE(run.started_at, run.created_at)
                      OR EXISTS (
                          SELECT 1 FROM feedback_analyses a
                          WHERE a.run_id = run.id AND a.feedback_id = f.id
                      )
                      OR EXISTS (
                          SELECT 1 FROM analysis_run_failures x
                          WHERE x.run_id = run.id AND x.feedback_id = f.id
                      )
                  )
            )
            WHERE run.input_feedback_external_ids IS NULL
            """
        )
    )
    # Earlier versions could persist counters without matching analysis rows.
    # Rebuild the business counts from preserved rows and the recovered input
    # set; the API must never claim successes that are absent from the table.
    op.execute(
        sa.text(
            """
            WITH tally AS (
                SELECT run.id,
                       json_array_length(run.input_feedback_external_ids) AS input_n,
                       (SELECT COUNT(*) FROM feedback_analyses a
                        WHERE a.run_id = run.id) AS success_n,
                       (SELECT COUNT(*) FROM analysis_run_failures x
                        WHERE x.run_id = run.id
                          AND NOT EXISTS (
                              SELECT 1 FROM feedback_analyses a
                              WHERE a.run_id = x.run_id AND a.feedback_id = x.feedback_id
                          )) AS failed_n
                FROM analysis_runs run
                WHERE run.input_feedback_external_ids IS NOT NULL
            )
            UPDATE analysis_runs run
            SET input_count = GREATEST(tally.input_n, tally.success_n + tally.failed_n),
                processed_count = tally.success_n + tally.failed_n,
                success_count = tally.success_n,
                failed_count = tally.failed_n,
                completed_at = CASE
                    WHEN tally.success_n + tally.failed_n
                         < GREATEST(tally.input_n, tally.success_n + tally.failed_n)
                        THEN NULL
                    ELSE run.completed_at
                END,
                status = CASE
                    WHEN GREATEST(tally.input_n, tally.success_n + tally.failed_n) = 0
                        THEN 'completed'
                    WHEN tally.success_n + tally.failed_n
                         < GREATEST(tally.input_n, tally.success_n + tally.failed_n)
                        THEN 'running'
                    WHEN tally.failed_n = 0 THEN 'completed'
                    WHEN tally.success_n = 0 THEN 'failed'
                    ELSE 'partial_failed'
                END
            FROM tally
            WHERE run.id = tally.id
            """
        )
    )


def downgrade() -> None:
    # Keep the reconstructed snapshots: clearing them would make retries of
    # historical runs silently do no work again.
    pass
