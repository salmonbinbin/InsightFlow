"""Phase 4 — Opportunity Score + Human Decision.

Adds three new tables backing the Phase 4 contract:

* ``opportunity_scores`` — immutable per-snapshot scoring record.
* ``insight_decisions`` — current PM decision (Business Value, AI / PM
  Priority, status, override reason / note, optimistic version).
* ``insight_decision_events`` — append-only history of every PATCH the
  PM applies; ``UNIQUE(insight_id, request_id)`` provides idempotency.

The original Phase 1—3 tables and their data are not modified; raw
``feedbacks.content`` and the analysis / cluster / insight history
remain unchanged.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "opportunity_scores",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("insight_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("classification_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_insight_status", sa.String(32), nullable=False),
        sa.Column("source_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("breakdown", sa.JSON(), nullable=False),
        sa.Column("weights_configured", sa.JSON(), nullable=False),
        sa.Column("weights_effective", sa.JSON(), nullable=False),
        sa.Column("thresholds", sa.JSON(), nullable=False),
        sa.Column("business_value", sa.Float(), nullable=True),
        sa.Column("total_score", sa.Float(), nullable=True),
        sa.Column("ai_priority", sa.String(4), nullable=True),
        sa.Column("user_value_score", sa.Float(), nullable=True),
        sa.Column("user_value_reasoning", sa.Text(), nullable=False, server_default=""),
        sa.Column("ai_reasoning", sa.Text(), nullable=False, server_default=""),
        sa.Column("calculation_version", sa.String(64), nullable=False),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("prompt_version", sa.String(100), nullable=False),
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
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["insight_id"], ["insights.id"], name="fk_opportunity_scores_insight"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="fk_opportunity_scores_project"
        ),
        sa.ForeignKeyConstraint(
            ["classification_run_id"],
            ["analysis_runs.id"],
            name="fk_opportunity_scores_classification_run",
        ),
        sa.CheckConstraint(
            "user_value_score IS NULL OR (user_value_score >= 0 AND user_value_score <= 10)",
            name="ck_opportunity_user_value_range",
        ),
        sa.CheckConstraint(
            "business_value IS NULL OR (business_value >= 0 AND business_value <= 10)",
            name="ck_opportunity_business_value_range",
        ),
        sa.CheckConstraint(
            "total_score IS NULL OR (total_score >= 0 AND total_score <= 10)",
            name="ck_opportunity_total_score_range",
        ),
        sa.CheckConstraint(
            "ai_priority IS NULL OR ai_priority IN ('P0','P1','P2')",
            name="ck_opportunity_ai_priority_allowed",
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND unknown_token_calls >= 0",
            name="ck_opportunity_token_counts_non_negative",
        ),
        sa.CheckConstraint(
            "latency_ms >= 0 AND attempt_count >= 0 AND retry_count >= 0",
            name="ck_opportunity_attempt_metrics_non_negative",
        ),
    )
    op.create_index("ix_opportunity_scores_insight_id", "opportunity_scores", ["insight_id"])
    op.create_index("ix_opportunity_scores_project_id", "opportunity_scores", ["project_id"])
    op.create_index("ix_opportunity_scores_created_at", "opportunity_scores", ["created_at"])

    op.create_table(
        "insight_decisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("insight_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("current_score_id", sa.Uuid(), nullable=True),
        sa.Column("business_value", sa.Float(), nullable=True),
        sa.Column("ai_priority", sa.String(4), nullable=True),
        sa.Column("pm_priority", sa.String(4), nullable=True),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="pending_review",
        ),
        sa.Column("override_reason", sa.String(64), nullable=True),
        sa.Column("override_note", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["insight_id"], ["insights.id"], name="fk_insight_decisions_insight"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="fk_insight_decisions_project"
        ),
        sa.ForeignKeyConstraint(
            ["current_score_id"],
            ["opportunity_scores.id"],
            name="fk_insight_decisions_score",
        ),
        sa.UniqueConstraint("insight_id", name="uq_insight_decisions_insight"),
        sa.CheckConstraint(
            "business_value IS NULL OR (business_value >= 0 AND business_value <= 10)",
            name="ck_insight_decisions_business_value_range",
        ),
        sa.CheckConstraint(
            "ai_priority IS NULL OR ai_priority IN ('P0','P1','P2')",
            name="ck_insight_decisions_ai_priority_allowed",
        ),
        sa.CheckConstraint(
            "pm_priority IS NULL OR pm_priority IN ('P0','P1','P2')",
            name="ck_insight_decisions_pm_priority_allowed",
        ),
        sa.CheckConstraint(
            "status IN ('pending_review','accepted','overridden','deferred','rejected')",
            name="ck_insight_decisions_status_allowed",
        ),
        sa.CheckConstraint(
            "override_reason IS NULL OR override_reason IN ("
            "'strategic_priority','core_user_experience','revenue_impact',"
            "'development_cost','management_decision','data_quality','other')",
            name="ck_insight_decisions_override_reason_allowed",
        ),
        sa.CheckConstraint(
            "(override_reason <> 'other') OR (override_note IS NOT NULL AND length(override_note) > 0)",
            name="ck_insight_decisions_other_note_required",
        ),
        sa.CheckConstraint("version >= 0", name="ck_insight_decisions_version_non_negative"),
    )
    op.create_index("ix_insight_decisions_project_id", "insight_decisions", ["project_id"])
    op.create_index("ix_insight_decisions_status", "insight_decisions", ["status"])

    op.create_table(
        "insight_decision_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("insight_id", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("score_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("before_state", sa.JSON(), nullable=True),
        sa.Column("after_state", sa.JSON(), nullable=True),
        sa.Column("business_value", sa.Float(), nullable=True),
        sa.Column("ai_priority", sa.String(4), nullable=True),
        sa.Column("pm_priority", sa.String(4), nullable=True),
        sa.Column("override_reason", sa.String(64), nullable=True),
        sa.Column("override_note", sa.Text(), nullable=True),
        sa.Column("version_after", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["insight_id"], ["insights.id"], name="fk_insight_decision_events_insight"
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["insight_decisions.id"],
            name="fk_insight_decision_events_decision",
        ),
        sa.ForeignKeyConstraint(
            ["score_id"],
            ["opportunity_scores.id"],
            name="fk_insight_decision_events_score",
        ),
        sa.UniqueConstraint(
            "insight_id",
            "request_id",
            name="uq_insight_decision_events_insight_request",
        ),
        sa.CheckConstraint(
            "event_type IN ('set_business_value','accept','override','defer','reject')",
            name="ck_insight_decision_events_event_type_allowed",
        ),
        sa.CheckConstraint(
            "business_value IS NULL OR (business_value >= 0 AND business_value <= 10)",
            name="ck_insight_decision_events_business_value_range",
        ),
        sa.CheckConstraint(
            "ai_priority IS NULL OR ai_priority IN ('P0','P1','P2')",
            name="ck_insight_decision_events_ai_priority_allowed",
        ),
        sa.CheckConstraint(
            "pm_priority IS NULL OR pm_priority IN ('P0','P1','P2')",
            name="ck_insight_decision_events_pm_priority_allowed",
        ),
        sa.CheckConstraint(
            "override_reason IS NULL OR override_reason IN ("
            "'strategic_priority','core_user_experience','revenue_impact',"
            "'development_cost','management_decision','data_quality','other')",
            name="ck_insight_decision_events_override_reason_allowed",
        ),
        sa.CheckConstraint(
            "(override_reason <> 'other') OR (override_note IS NOT NULL AND length(override_note) > 0)",
            name="ck_insight_decision_events_other_note_required",
        ),
        sa.CheckConstraint(
            "version_after >= 0",
            name="ck_insight_decision_events_version_after_non_negative",
        ),
    )
    op.create_index(
        "ix_insight_decision_events_insight_id",
        "insight_decision_events",
        ["insight_id"],
    )
    op.create_index(
        "ix_insight_decision_events_decision_id",
        "insight_decision_events",
        ["decision_id"],
    )
    op.create_index(
        "ix_insight_decision_events_created_at",
        "insight_decision_events",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_insight_decision_events_created_at", table_name="insight_decision_events")
    op.drop_index("ix_insight_decision_events_decision_id", table_name="insight_decision_events")
    op.drop_index("ix_insight_decision_events_insight_id", table_name="insight_decision_events")
    op.drop_table("insight_decision_events")

    op.drop_index("ix_insight_decisions_status", table_name="insight_decisions")
    op.drop_index("ix_insight_decisions_project_id", table_name="insight_decisions")
    op.drop_table("insight_decisions")

    op.drop_index("ix_opportunity_scores_created_at", table_name="opportunity_scores")
    op.drop_index("ix_opportunity_scores_project_id", table_name="opportunity_scores")
    op.drop_index("ix_opportunity_scores_insight_id", table_name="opportunity_scores")
    op.drop_table("opportunity_scores")
