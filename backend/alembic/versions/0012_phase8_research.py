"""Phase 8 — Research Agent external market intelligence.

Three new tables isolate external research from the internal evidence
surface (per ``docs/11`` §82 — Internal Evidence and External
Research live in different stores). The Phase 1—7 schema and the raw
``feedbacks.content`` are not modified.

* ``research_runs`` — one row per research execution for a single
  Insight. Stores the user-supplied question + scope (length-bounded),
  the assembled internal context snapshot, the provider + model +
  prompt binding, the canonical ``provider_query`` that was sent, the
  five-state status and the per-attempt token / latency / attempts /
  retry accounting. ``internal_context_hash`` lets a retry detect a
  drift in the internal context without re-reading the raw evidence.

* ``research_sources`` — one row per external search hit returned by
  the Research Tool Provider. The URL is bounded by length and
  constrained to be syntactically a valid http(s) URL via the
  application-layer validator; ``provider_source_id`` is the
  provider's opaque identifier so the same hit can be reconciled
  against a future retrieval.

* ``research_findings`` — one row per structured finding the LLM
  produced. ``category`` and ``status`` are constrained to closed
  enums; the status/provided invariant is enforced at the DB layer
  via ``ck_research_findings_status_provided_invariant`` so a
  confirmed finding must carry at least one ``source_refs`` entry and
  a not_verified finding must carry none.

The original feedback / analysis / cluster / insight / decision /
requirement / PRD / evaluation tables and their data are preserved;
``down_revision`` is ``0011`` so the migration chain stays linear.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


# Status / category enums mirrored from ``app.models.research`` so the
# CHECK constraints agree with the ORM string literals.

_RESEARCH_RUN_STATUSES = ("pending", "running", "completed", "partial_failed", "failed")
_RESEARCH_FINDING_CATEGORIES = (
    "summary",
    "competitor",
    "market_pattern",
    "opportunity",
    "risk",
)
_RESEARCH_FINDING_STATUSES = ("confirmed", "not_verified")


def upgrade() -> None:
    op.create_table(
        "research_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("insight_id", sa.Uuid(), nullable=False),
        sa.Column("classification_run_id", sa.Uuid(), nullable=False),
        sa.Column("clustering_run_id", sa.Uuid(), nullable=False),
        sa.Column("insight_decision_id", sa.Uuid(), nullable=True),
        sa.Column("research_question", sa.String(500), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("provider_query", sa.String(500), nullable=False),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("internal_context", sa.JSON(), nullable=False),
        sa.Column("internal_context_hash", sa.String(64), nullable=False),
        sa.Column("input_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("finding_count", sa.Integer(), nullable=False, server_default="0"),
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
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="fk_research_runs_project"
        ),
        sa.ForeignKeyConstraint(
            ["insight_id"], ["insights.id"], name="fk_research_runs_insight"
        ),
        sa.ForeignKeyConstraint(
            ["classification_run_id"],
            ["analysis_runs.id"],
            name="fk_research_runs_classification_run",
        ),
        sa.ForeignKeyConstraint(
            ["clustering_run_id"],
            ["analysis_runs.id"],
            name="fk_research_runs_clustering_run",
        ),
        sa.ForeignKeyConstraint(
            ["insight_decision_id"],
            ["insight_decisions.id"],
            name="fk_research_runs_decision",
        ),
        sa.CheckConstraint(
            "length(research_question) BETWEEN 1 AND 500",
            name="ck_research_runs_question_length",
        ),
        sa.CheckConstraint(
            "length(scope) <= 2000",
            name="ck_research_runs_scope_length",
        ),
        sa.CheckConstraint(
            "length(provider) BETWEEN 1 AND 64",
            name="ck_research_runs_provider_length",
        ),
        sa.CheckConstraint(
            "length(model) BETWEEN 1 AND 100 OR model IS NULL",
            name="ck_research_runs_model_length",
        ),
        sa.CheckConstraint(
            f"status IN ({','.join(repr(s) for s in _RESEARCH_RUN_STATUSES)})",
            name="ck_research_runs_status_allowed",
        ),
        sa.CheckConstraint(
            "input_count >= 0 AND processed_count >= 0 AND "
            "source_count >= 0 AND finding_count >= 0 AND "
            "input_tokens >= 0 AND output_tokens >= 0 AND "
            "latency_ms >= 0 AND attempt_count >= 0 AND "
            "retry_count >= 0 AND unknown_token_calls >= 0",
            name="ck_research_runs_non_negative_counts",
        ),
        sa.CheckConstraint(
            "processed_count <= input_count",
            name="ck_research_runs_processed_le_input",
        ),
    )
    op.create_index("ix_research_runs_project_id", "research_runs", ["project_id"])
    op.create_index("ix_research_runs_insight_id", "research_runs", ["insight_id"])
    op.create_index("ix_research_runs_status", "research_runs", ["status"])
    op.create_index("ix_research_runs_created_at", "research_runs", ["created_at"])

    op.create_table(
        "research_sources",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("provider_source_id", sa.String(200), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("domain", sa.String(253), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["research_runs.id"],
            name="fk_research_sources_run",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "run_id",
            "provider_source_id",
            name="uq_research_sources_run_provider_id",
        ),
        sa.CheckConstraint(
            "length(url) BETWEEN 1 AND 2048",
            name="ck_research_sources_url_length",
        ),
        sa.CheckConstraint(
            "length(title) <= 500",
            name="ck_research_sources_title_length",
        ),
        sa.CheckConstraint(
            "length(domain) BETWEEN 1 AND 253",
            name="ck_research_sources_domain_length",
        ),
        sa.CheckConstraint(
            "length(provider_source_id) BETWEEN 1 AND 200",
            name="ck_research_sources_provider_id_length",
        ),
        sa.CheckConstraint(
            "length(provider) BETWEEN 1 AND 64",
            name="ck_research_sources_provider_length",
        ),
    )
    op.create_index("ix_research_sources_run_id", "research_sources", ["run_id"])
    op.create_index("ix_research_sources_domain", "research_sources", ["domain"])

    op.create_table(
        "research_findings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="not_verified",
        ),
        sa.Column("source_refs", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["research_runs.id"],
            name="fk_research_findings_run",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            f"category IN ({','.join(repr(c) for c in _RESEARCH_FINDING_CATEGORIES)})",
            name="ck_research_findings_category_allowed",
        ),
        sa.CheckConstraint(
            f"status IN ({','.join(repr(s) for s in _RESEARCH_FINDING_STATUSES)})",
            name="ck_research_findings_status_allowed",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_research_findings_confidence_range",
        ),
        sa.CheckConstraint(
            "length(title) BETWEEN 1 AND 200",
            name="ck_research_findings_title_length",
        ),
        sa.CheckConstraint(
            "length(description) BETWEEN 1 AND 4000",
            name="ck_research_findings_description_length",
        ),
        # Status/provided invariant: a confirmed finding must carry at
        # least one source_refs entry; a not_verified finding must
        # carry none. The application layer enforces this BEFORE the
        # INSERT; the CHECK is defence-in-depth against direct DB
        # writes. ``json_array_length`` is dialect-neutral when the
        # column type is JSON. The invariant is split into a
        # ``confirmed`` branch and a ``not_verified`` branch so the
        # NULL test is explicit on the ``confirmed`` side (a
        # three-valued NULL would otherwise let a confirmed row with
        # source_refs=NULL slip through the OR).
        sa.CheckConstraint(
            "((status = 'confirmed') "
            "AND (source_refs IS NOT NULL AND json_array_length(source_refs) > 0)) "
            "OR "
            "((status = 'not_verified') "
            "AND (source_refs IS NULL OR json_array_length(source_refs) = 0))",
            name="ck_research_findings_status_provided_invariant",
        ),
    )
    op.create_index("ix_research_findings_run_id", "research_findings", ["run_id"])
    op.create_index("ix_research_findings_category", "research_findings", ["category"])
    op.create_index("ix_research_findings_status", "research_findings", ["status"])


def downgrade() -> None:
    op.drop_index("ix_research_findings_status", table_name="research_findings")
    op.drop_index("ix_research_findings_category", table_name="research_findings")
    op.drop_index("ix_research_findings_run_id", table_name="research_findings")
    op.drop_table("research_findings")

    op.drop_index("ix_research_sources_domain", table_name="research_sources")
    op.drop_index("ix_research_sources_run_id", table_name="research_sources")
    op.drop_table("research_sources")

    op.drop_index("ix_research_runs_created_at", table_name="research_runs")
    op.drop_index("ix_research_runs_status", table_name="research_runs")
    op.drop_index("ix_research_runs_insight_id", table_name="research_runs")
    op.drop_index("ix_research_runs_project_id", table_name="research_runs")
    op.drop_table("research_runs")