"""Phase 3 — Insight + Evidence.

Adds four new tables for the insight generation pipeline:

* ``insight_runs`` — dedicated run object bounded to a specific clustering
  run; captures model binding, prompt version, five-state status, token /
  latency accounting, and a snapshot of input cluster IDs and per-cluster
  feedback external IDs so a retry cannot silently expand.
* ``insight_run_items`` — per-cluster execution row; the source of truth
  for which clusters still need to be re-processed on retry.
* ``insights`` — one row per cluster. ``status`` is ``draft`` (≥3 evidence)
  or ``needs_validation`` (1–2 evidence). All actionable fields are NOT
  NULL for drafts and may be NULL for needs_validation.
* ``insight_evidence`` — one row per (insight, feedback) pair with the
  evidence role and relevance score; ``UNIQUE(insight_id, feedback_id)``
  prevents double-spending a feedback.

The original Phase 1/2 tables and their data are not modified; raw
``feedbacks.content`` and the entire analysis / cluster / cluster_review
history remain unchanged.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "insight_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("clustering_run_id", sa.Uuid(), nullable=False),
        sa.Column("classification_run_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("input_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "needs_validation_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "unknown_token_calls",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("input_cluster_external_ids", sa.JSON(), nullable=True),
        sa.Column("input_member_external_ids", sa.JSON(), nullable=True),
        sa.Column("input_snapshot", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_insight_runs_project"),
        sa.ForeignKeyConstraint(
            ["clustering_run_id"],
            ["analysis_runs.id"],
            name="fk_insight_runs_clustering_run",
        ),
        sa.ForeignKeyConstraint(
            ["classification_run_id"],
            ["analysis_runs.id"],
            name="fk_insight_runs_classification_run",
        ),
        sa.CheckConstraint(
            "input_count >= 0 AND processed_count >= 0 AND success_count >= 0 "
            "AND failed_count >= 0 AND needs_validation_count >= 0 "
            "AND retry_count >= 0 AND latency_ms >= 0 AND attempt_count >= 0 "
            "AND unknown_token_calls >= 0 AND input_tokens >= 0 "
            "AND output_tokens >= 0 "
            "AND processed_count <= input_count "
            "AND success_count + failed_count + needs_validation_count <= processed_count",
            name="ck_insight_runs_non_negative_counts",
        ),
    )
    op.create_index("ix_insight_runs_project_id", "insight_runs", ["project_id"])
    op.create_index("ix_insight_runs_clustering_run_id", "insight_runs", ["clustering_run_id"])
    op.create_index("ix_insight_runs_status", "insight_runs", ["status"])
    op.create_index("ix_insight_runs_created_at", "insight_runs", ["created_at"])

    op.create_table(
        "insight_run_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("cluster_id", sa.Uuid(), nullable=False),
        sa.Column("source_cluster_external_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("feedback_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["insight_runs.id"],
            name="fk_insight_run_items_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["cluster_id"], ["clusters.id"], name="fk_insight_run_items_cluster"
        ),
        sa.UniqueConstraint("run_id", "cluster_id", name="uq_insight_run_item_run_cluster"),
        sa.CheckConstraint(
            "status IN ('success','needs_validation','failed')",
            name="ck_insight_run_item_status_allowed",
        ),
        sa.CheckConstraint("attempts >= 1", name="ck_insight_run_item_attempts_positive"),
        sa.CheckConstraint(
            "feedback_count >= 0 AND evidence_count >= 0",
            name="ck_insight_run_item_non_negative",
        ),
    )
    op.create_index("ix_insight_run_items_run_id", "insight_run_items", ["run_id"])
    op.create_index("ix_insight_run_items_cluster_id", "insight_run_items", ["cluster_id"])

    op.create_table(
        "insights",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("cluster_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("pain_point", sa.Text(), nullable=True),
        sa.Column("affected_segment", sa.Text(), nullable=True),
        sa.Column("insight", sa.Text(), nullable=True),
        sa.Column("recommendation", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("feedback_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_cluster_external_id", sa.String(64), nullable=False),
        sa.Column("source_member_external_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["insight_runs.id"], name="fk_insights_run", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_insights_project"),
        sa.ForeignKeyConstraint(["cluster_id"], ["clusters.id"], name="fk_insights_cluster"),
        sa.UniqueConstraint("run_id", "cluster_id", name="uq_insight_run_cluster"),
        sa.CheckConstraint(
            "status IN ('draft','needs_validation')",
            name="ck_insight_status_allowed",
        ),
        sa.CheckConstraint(
            "status <> 'draft' OR (insight IS NOT NULL AND pain_point IS NOT NULL "
            "AND affected_segment IS NOT NULL AND recommendation IS NOT NULL "
            "AND title IS NOT NULL AND confidence IS NOT NULL)",
            name="ck_insight_draft_completeness",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_insight_confidence_range",
        ),
        sa.CheckConstraint("feedback_count >= 0", name="ck_insight_feedback_count_non_negative"),
        sa.CheckConstraint("evidence_count >= 0", name="ck_insight_evidence_count_non_negative"),
    )
    op.create_index("ix_insights_project_id", "insights", ["project_id"])
    op.create_index("ix_insights_run_id", "insights", ["run_id"])
    op.create_index("ix_insights_cluster_id", "insights", ["cluster_id"])
    op.create_index("ix_insights_status", "insights", ["status"])

    op.create_table(
        "insight_evidence",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("insight_id", sa.Uuid(), nullable=False),
        sa.Column("feedback_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_type", sa.String(32), nullable=False),
        sa.Column("relevance_score", sa.Float(), nullable=False),
        sa.Column("feedback_external_id", sa.String(200), nullable=False),
        sa.Column("feedback_project_id", sa.Uuid(), nullable=False),
        sa.Column("feedback_classification_run_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["insight_id"],
            ["insights.id"],
            name="fk_insight_evidence_insight",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["feedback_id"], ["feedbacks.id"], name="fk_insight_evidence_feedback"
        ),
        sa.ForeignKeyConstraint(
            ["feedback_project_id"],
            ["projects.id"],
            name="fk_insight_evidence_project",
        ),
        sa.ForeignKeyConstraint(
            ["feedback_classification_run_id"],
            ["analysis_runs.id"],
            name="fk_insight_evidence_classification_run",
        ),
        sa.UniqueConstraint(
            "insight_id",
            "feedback_id",
            name="uq_insight_evidence_insight_feedback",
        ),
        sa.CheckConstraint(
            "relevance_score >= 0 AND relevance_score <= 1",
            name="ck_insight_evidence_relevance_range",
        ),
        sa.CheckConstraint(
            "evidence_type IN ('representative','high_severity','diverse_expression')",
            name="ck_insight_evidence_type_allowed",
        ),
    )
    op.create_index("ix_insight_evidence_insight_id", "insight_evidence", ["insight_id"])
    op.create_index("ix_insight_evidence_feedback_id", "insight_evidence", ["feedback_id"])


def downgrade() -> None:
    op.drop_index("ix_insight_evidence_feedback_id", table_name="insight_evidence")
    op.drop_index("ix_insight_evidence_insight_id", table_name="insight_evidence")
    op.drop_table("insight_evidence")

    op.drop_index("ix_insights_status", table_name="insights")
    op.drop_index("ix_insights_cluster_id", table_name="insights")
    op.drop_index("ix_insights_run_id", table_name="insights")
    op.drop_index("ix_insights_project_id", table_name="insights")
    op.drop_table("insights")

    op.drop_index("ix_insight_run_items_cluster_id", table_name="insight_run_items")
    op.drop_index("ix_insight_run_items_run_id", table_name="insight_run_items")
    op.drop_table("insight_run_items")

    op.drop_index("ix_insight_runs_created_at", table_name="insight_runs")
    op.drop_index("ix_insight_runs_status", table_name="insight_runs")
    op.drop_index("ix_insight_runs_clustering_run_id", table_name="insight_runs")
    op.drop_index("ix_insight_runs_project_id", table_name="insight_runs")
    op.drop_table("insight_runs")
