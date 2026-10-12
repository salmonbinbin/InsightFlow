"""Phase 1: analysis_runs, feedback_analyses and analysis_run_failures.

Raw feedback content is never overwritten; analysis rows reference
``feedbacks.id`` by foreign key and ``UNIQUE(feedback_id, run_id)`` prevents
duplicate writes for the same run.
"""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "analysis_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("run_type", sa.String(50), nullable=False),
        sa.Column("model", sa.String(100)),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("input_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_message", sa.Text()),
        sa.CheckConstraint(
            "input_count >= 0 AND processed_count >= 0 AND success_count >= 0 "
            "AND failed_count >= 0 AND retry_count >= 0 AND latency_ms >= 0",
            name="ck_analysis_runs_non_negative_counts",
        ),
    )
    op.create_index("ix_analysis_runs_project_id", "analysis_runs", ["project_id"])
    op.create_index("ix_analysis_runs_status", "analysis_runs", ["status"])
    op.create_index("ix_analysis_runs_created_at", "analysis_runs", ["created_at"])

    op.create_table(
        "feedback_analyses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("feedback_id", sa.Uuid(), sa.ForeignKey("feedbacks.id"), nullable=False),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("analysis_runs.id"), nullable=False),
        sa.Column("feedback_type", sa.String(50), nullable=False),
        sa.Column("module", sa.String(100), nullable=False),
        sa.Column("intent", sa.String(200), nullable=False),
        sa.Column("pain_point", sa.Text(), nullable=False),
        sa.Column("sentiment", sa.String(20), nullable=False),
        sa.Column("severity", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("semantic_text", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("feedback_id", "run_id", name="uq_feedback_analysis_feedback_run"),
        sa.CheckConstraint("severity BETWEEN 1 AND 5", name="ck_feedback_analysis_severity"),
        sa.CheckConstraint(
            "confidence BETWEEN 0 AND 1", name="ck_feedback_analysis_confidence"
        ),
    )
    op.create_index("ix_feedback_analyses_run_id", "feedback_analyses", ["run_id"])
    op.create_index("ix_feedback_analyses_feedback_id", "feedback_analyses", ["feedback_id"])

    op.create_table(
        "analysis_run_failures",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("analysis_runs.id"), nullable=False),
        sa.Column("feedback_id", sa.Uuid(), sa.ForeignKey("feedbacks.id"), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "feedback_id", name="uq_run_failure_run_feedback"),
    )
    op.create_index("ix_run_failures_run_id", "analysis_run_failures", ["run_id"])


def downgrade():
    op.drop_index("ix_run_failures_run_id", table_name="analysis_run_failures")
    op.drop_table("analysis_run_failures")
    op.drop_index("ix_feedback_analyses_feedback_id", table_name="feedback_analyses")
    op.drop_index("ix_feedback_analyses_run_id", table_name="feedback_analyses")
    op.drop_table("feedback_analyses")
    op.drop_index("ix_analysis_runs_created_at", table_name="analysis_runs")
    op.drop_index("ix_analysis_runs_status", table_name="analysis_runs")
    op.drop_index("ix_analysis_runs_project_id", table_name="analysis_runs")
    op.drop_table("analysis_runs")