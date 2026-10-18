"""Phase 8 — Tool Layer (Tool Registry + Tool Run audit + Human Approval).

Two tables back the Tool Layer without touching any Phase 1—8 table:

* ``tool_runs`` — one row per attempted tool invocation. Carries
  ``request_id`` (UUID idempotency key), the sanitized
  ``request_payload`` JSON, the bounded ``response_summary``, the
  ``execution_mode`` (``simulation`` / ``real``), the optional
  ``external_id`` returned by the adapter (``SIM-...`` for the
  Phase 8 simulator), ``status`` spanning the six lifecycle states,
  and the bounded attempt / retry counters. ``UNIQUE
  (project_id, request_id)`` enforces the Prepare idempotency
  contract at the DB layer; an attacker cannot bypass the service
  code to land two rows with the same key.

* ``simulated_issues`` — one row per approved simulator write.
  Stores the server-built Title / Description / Priority / Source-
  Insight preview so an audit can confirm what the simulator saw.
  ``UNIQUE(external_id)`` rejects duplicate simulator writes even
  when two Approve calls race against the same row;
  ``UNIQUE(tool_run_id)`` ensures exactly one audit row per
  approved tool run.

CHECK / FK / indexes are written dialect-neutral (no ``JSONB``, no
``UUID::text`` casts) so the same migration runs on SQLite and
PostgreSQL. The Phase 1—8 schema is preserved; ``down_revision`` is
``0012``.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


# Mirrors the enums in ``app.models.tool`` so the CHECK constraints
# agree with the ORM string literals.

_TOOL_RUN_STATUSES = (
    "pending",
    "awaiting_approval",
    "running",
    "completed",
    "failed",
    "cancelled",
)
_TOOL_EXECUTION_MODES = ("simulation", "real")
_TOOL_FAILURE_CATEGORIES = (
    "transport_timeout",
    "transport_5xx",
    "transport_4xx",
    "validation",
    "not_registered",
    "unauthorised",
    "provider_failure",
    "not_found",
    "conflict",
    "ineligible",
    "cancelled",
    "unknown",
)


def upgrade() -> None:
    op.create_table(
        "tool_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("requirement_id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("tool_action", sa.String(64), nullable=False),
        sa.Column("tool_version", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="awaiting_approval",
        ),
        sa.Column(
            "execution_mode",
            sa.String(20),
            nullable=False,
            server_default="simulation",
        ),
        sa.Column("external_id", sa.String(64), nullable=True),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("response_summary", sa.Text(), nullable=True),
        sa.Column("failure_category", sa.String(32), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="fk_tool_runs_project"
        ),
        sa.ForeignKeyConstraint(
            ["requirement_id"],
            ["requirements.id"],
            name="fk_tool_runs_requirement",
        ),
        sa.UniqueConstraint(
            "project_id",
            "request_id",
            name="uq_tool_runs_project_request_id",
        ),
        sa.CheckConstraint(
            f"status IN ({','.join(repr(s) for s in _TOOL_RUN_STATUSES)})",
            name="ck_tool_runs_status_allowed",
        ),
        sa.CheckConstraint(
            f"execution_mode IN ({','.join(repr(m) for m in _TOOL_EXECUTION_MODES)})",
            name="ck_tool_runs_execution_mode_allowed",
        ),
        sa.CheckConstraint(
            f"failure_category IS NULL OR failure_category IN "
            f"({','.join(repr(c) for c in _TOOL_FAILURE_CATEGORIES)})",
            name="ck_tool_runs_failure_category_allowed",
        ),
        sa.CheckConstraint(
            "length(tool_name) BETWEEN 1 AND 64",
            name="ck_tool_runs_tool_name_length",
        ),
        sa.CheckConstraint(
            "length(tool_action) BETWEEN 1 AND 64",
            name="ck_tool_runs_tool_action_length",
        ),
        sa.CheckConstraint(
            "length(tool_version) BETWEEN 1 AND 64",
            name="ck_tool_runs_tool_version_length",
        ),
        sa.CheckConstraint(
            "length(request_id) BETWEEN 1 AND 64",
            name="ck_tool_runs_request_id_length",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND retry_count >= 0",
            name="ck_tool_runs_attempt_metrics_non_negative",
        ),
        sa.CheckConstraint(
            "retry_count <= attempt_count",
            name="ck_tool_runs_retry_le_attempt",
        ),
        sa.CheckConstraint(
            "(external_id IS NULL) OR (length(external_id) BETWEEN 1 AND 64)",
            name="ck_tool_runs_external_id_length",
        ),
    )
    op.create_index("ix_tool_runs_project_id", "tool_runs", ["project_id"])
    op.create_index("ix_tool_runs_requirement_id", "tool_runs", ["requirement_id"])
    op.create_index("ix_tool_runs_status", "tool_runs", ["status"])
    op.create_index("ix_tool_runs_created_at", "tool_runs", ["created_at"])

    op.create_table(
        "simulated_issues",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tool_run_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("requirement_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("tool_action", sa.String(64), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("priority", sa.String(4), nullable=True),
        sa.Column(
            "source_insight_preview",
            sa.String(400),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tool_run_id"], ["tool_runs.id"], name="fk_simulated_issues_tool_run"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="fk_simulated_issues_project"
        ),
        sa.ForeignKeyConstraint(
            ["requirement_id"],
            ["requirements.id"],
            name="fk_simulated_issues_requirement",
        ),
        sa.UniqueConstraint(
            "external_id",
            name="uq_simulated_issues_external_id",
        ),
        sa.UniqueConstraint(
            "tool_run_id",
            name="uq_simulated_issues_tool_run_id",
        ),
        sa.CheckConstraint(
            "length(external_id) BETWEEN 1 AND 64",
            name="ck_simulated_issues_external_id_length",
        ),
        sa.CheckConstraint(
            "length(title) BETWEEN 1 AND 200",
            name="ck_simulated_issues_title_length",
        ),
        sa.CheckConstraint(
            "length(description) BETWEEN 1 AND 4000",
            name="ck_simulated_issues_description_length",
        ),
        sa.CheckConstraint(
            "length(source_insight_preview) <= 400",
            name="ck_simulated_issues_source_preview_length",
        ),
        sa.CheckConstraint(
            "priority IS NULL OR priority IN ('P0','P1','P2')",
            name="ck_simulated_issues_priority_allowed",
        ),
    )
    op.create_index(
        "ix_simulated_issues_project_id", "simulated_issues", ["project_id"]
    )
    op.create_index(
        "ix_simulated_issues_requirement_id",
        "simulated_issues",
        ["requirement_id"],
    )
    op.create_index(
        "ix_simulated_issues_created_at", "simulated_issues", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_simulated_issues_created_at", table_name="simulated_issues")
    op.drop_index(
        "ix_simulated_issues_requirement_id", table_name="simulated_issues"
    )
    op.drop_index("ix_simulated_issues_project_id", table_name="simulated_issues")
    op.drop_table("simulated_issues")

    op.drop_index("ix_tool_runs_created_at", table_name="tool_runs")
    op.drop_index("ix_tool_runs_status", table_name="tool_runs")
    op.drop_index("ix_tool_runs_requirement_id", table_name="tool_runs")
    op.drop_index("ix_tool_runs_project_id", table_name="tool_runs")
    op.drop_table("tool_runs")