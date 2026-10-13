"""Phase 5 — Requirement + PRD.

Adds two new tables:

* ``requirements`` — one row per Insight (UNIQUE constraint enforces
  "one current requirement per insight"). The row carries a snapshot of
  the Phase 4 decision so a later PM PATCH cannot rewrite the requirement
  priority provenance.
* ``prds`` — versioned PRD draft attached to a requirement. The
  ``UNIQUE(requirement_id, version)`` constraint guards against duplicate
  versions; ``requirements.next_version`` is incremented atomically
  inside the same transaction as the insert to satisfy the brief's
  "并发版本原子递增" rule.

No existing Phase 1—4 table or constraint is modified.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "requirements",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("insight_id", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("score_id", sa.Uuid(), nullable=True),
        sa.Column("insight_status_snapshot", sa.String(20), nullable=False),
        sa.Column("decision_status_snapshot", sa.String(32), nullable=False),
        sa.Column("ai_priority_snapshot", sa.String(4), nullable=True),
        sa.Column("pm_priority", sa.String(4), nullable=False),
        sa.Column("pm_override_reason_snapshot", sa.String(64), nullable=True),
        sa.Column("source_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("problem", sa.Text(), nullable=False),
        sa.Column("target_user", sa.Text(), nullable=False),
        sa.Column("user_story", sa.Text(), nullable=False),
        sa.Column("solution_hypothesis", sa.Text(), nullable=False),
        sa.Column("success_metrics", sa.JSON(), nullable=False),
        sa.Column("risks", sa.JSON(), nullable=False),
        sa.Column("evidence_references", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column(
            "input_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "output_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "unknown_token_calls",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_requirements_project"),
        sa.ForeignKeyConstraint(["insight_id"], ["insights.id"], name="fk_requirements_insight"),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["insight_decisions.id"],
            name="fk_requirements_decision",
        ),
        sa.ForeignKeyConstraint(
            ["score_id"],
            ["opportunity_scores.id"],
            name="fk_requirements_score",
        ),
        sa.UniqueConstraint("insight_id", name="uq_requirements_insight"),
        sa.CheckConstraint(
            "status IN ('draft','reviewing','approved','rejected','archived')",
            name="ck_requirements_status_allowed",
        ),
        sa.CheckConstraint(
            "pm_priority IS NULL OR pm_priority IN ('P0','P1','P2')",
            name="ck_requirements_pm_priority_allowed",
        ),
        sa.CheckConstraint(
            "ai_priority_snapshot IS NULL OR ai_priority_snapshot IN ('P0','P1','P2')",
            name="ck_requirements_ai_priority_snapshot_allowed",
        ),
        sa.CheckConstraint(
            "decision_status_snapshot IS NULL OR decision_status_snapshot IN ("
            "'pending_review','accepted','overridden','deferred','rejected')",
            name="ck_requirements_decision_status_allowed",
        ),
        sa.CheckConstraint(
            "insight_status_snapshot IS NULL OR insight_status_snapshot IN ("
            "'draft','needs_validation')",
            name="ck_requirements_insight_status_allowed",
        ),
        sa.CheckConstraint(
            "next_version >= 1",
            name="ck_requirements_next_version_positive",
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND unknown_token_calls >= 0",
            name="ck_requirements_token_counts_non_negative",
        ),
        sa.CheckConstraint(
            "latency_ms >= 0 AND attempt_count >= 0 AND retry_count >= 0",
            name="ck_requirements_attempt_metrics_non_negative",
        ),
    )
    op.create_index("ix_requirements_project_id", "requirements", ["project_id"])
    op.create_index("ix_requirements_insight_id", "requirements", ["insight_id"])
    op.create_index("ix_requirements_status", "requirements", ["status"])

    op.create_table(
        "prds",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("requirement_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("insight_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column(
            "is_ai_generated_draft",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "grounding_warnings",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column(
            "input_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "output_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "unknown_token_calls",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["requirement_id"],
            ["requirements.id"],
            name="fk_prds_requirement",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_prds_project"),
        sa.ForeignKeyConstraint(["insight_id"], ["insights.id"], name="fk_prds_insight"),
        sa.UniqueConstraint(
            "requirement_id",
            "version",
            name="uq_prds_requirement_version",
        ),
        sa.CheckConstraint("version >= 1", name="ck_prds_version_positive"),
        sa.CheckConstraint(
            "status IN ('draft','reviewing','accepted','archived')",
            name="ck_prds_status_allowed",
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND unknown_token_calls >= 0",
            name="ck_prds_token_counts_non_negative",
        ),
        sa.CheckConstraint(
            "latency_ms >= 0 AND attempt_count >= 0 AND retry_count >= 0",
            name="ck_prds_attempt_metrics_non_negative",
        ),
    )
    op.create_index("ix_prds_requirement_id", "prds", ["requirement_id"])
    op.create_index("ix_prds_status", "prds", ["status"])
    op.create_index("ix_prds_created_at", "prds", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_prds_created_at", table_name="prds")
    op.drop_index("ix_prds_status", table_name="prds")
    op.drop_index("ix_prds_requirement_id", table_name="prds")
    op.drop_table("prds")

    op.drop_index("ix_requirements_status", table_name="requirements")
    op.drop_index("ix_requirements_insight_id", table_name="requirements")
    op.drop_index("ix_requirements_project_id", table_name="requirements")
    op.drop_table("requirements")
