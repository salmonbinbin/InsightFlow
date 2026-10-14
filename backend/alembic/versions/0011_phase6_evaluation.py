"""Phase 6 — Evaluation + Regression + Observability.

Three new tables back the Phase 6 evaluation harness:

* ``evaluation_runs`` — one row per (project, evaluation_type, dataset,
  model) snapshot. The row carries the dataset provenance
  (name/version/content_hash/split/label_status), the model/prompt/
  pipeline version binding, the run status, per-attempt accounting and
  NULLABLE token / latency / cost. NULLABLE is enforced so missing
  measurements are reported as ``NULL`` (= not measured) instead of
  being silently coerced to 0. The supervisor and the dashboard rely on
  that distinction (per the Phase 6 brief).

* ``evaluation_metrics`` — per-metric measurement. Each row stores the
  metric ``name``, the ``metric_kind`` (``deterministic`` /
  ``human_reviewed`` / ``llm_judge``), the ``sample_count`` the metric
  was computed over, the ``value`` (NULL = NOT_MEASURED) and ``details``
  JSON for auxiliary counters / breakdowns. ``sample_count`` may be 0
  in which case ``value`` MUST also be NULL (the metric was
  computable but had no eligible samples).

* ``evaluation_cases`` — per-case record: ``case_id`` (e.g. ``F001``),
  ``stage``, hashes of input/expected/actual and OPTIONAL raw fields.
  By default the framework AVOIDS persisting sensitive raw feedback
  text — only ``input_hash`` / ``expected_hash`` / ``actual_hash`` are
  populated. The full text fields (``input`` / ``expected`` / ``actual``)
  stay NULL unless the caller explicitly opts in via the case-level
  ``store_raw`` policy at write time. The OPT-IN semantic is documented
  in the model docstring so the supervisor can audit the default.
  ``UNIQUE(run_id, stage, case_id)`` rejects duplicate case records
  inside one run.

CHECK / FK / indexes are written dialect-neutral (no ``JSONB``, no
``UUID::text`` casts) so the same migration runs on SQLite and
PostgreSQL.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


EVALUATION_TYPES = (
    "classification_metrics",
    "cluster_metrics",
    "insight_metrics",
    "pipeline_health",
    "token_efficiency",
    "latency_metrics",
    # Phase 4 — Opportunity Score metrics.
    "opportunity_metrics",
    # Phase 5 — Requirement + PRD metrics.
    "requirement_metrics",
    "prd_metrics",
)

EVALUATION_PROVENANCE = (
    "demo",
    "synthetic",
    "production",
    "mixed",
    # Default for DB-snapshot evaluations (Phase 6 insight runner).
    # The runner does NOT verify whether the underlying snapshot is
    # synthetic / anonymised / production; the value stays
    # ``unverified`` until a separate provenance check promotes it.
    "unverified",
)

EVALUATION_SPLIT = ("full", "train", "validation", "test", "mixed")

EVALUATION_STAGES = (
    "classification",
    "clustering",
    "insight",
    "pipeline",
    "token",
    "latency",
    # Phase 4 — Opportunity Score + Human Decision.
    "opportunity",
    # Phase 5 — Requirement + PRD.
    "requirement",
    "prd",
)

METRIC_KINDS = ("deterministic", "human_reviewed", "llm_judge")

EVAL_CASE_SOURCES = ("csv_load", "db_record", "synthetic", "not_applicable")

EVAL_CASE_ERROR_TYPES = (
    "no_prediction",
    "protocol_error",
    "transport_error",
    "validation_error",
    "config_error",
    "not_measured",
    "other",
)

EVALUATION_RUN_STATUSES = (
    "pending",
    "running",
    "completed",
    "partial_failed",
    "failed",
)


def upgrade() -> None:
    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_type", sa.String(64), nullable=False),
        sa.Column("dataset_name", sa.String(64), nullable=False),
        sa.Column("dataset_version", sa.String(64), nullable=False),
        sa.Column("dataset_content_hash", sa.String(64), nullable=False),
        sa.Column("dataset_provenance", sa.String(64), nullable=False),
        sa.Column("dataset_split", sa.String(32), nullable=False),
        sa.Column("dataset_label_status", sa.String(200), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("prompt_version", sa.String(100), nullable=True),
        sa.Column("pipeline_version", sa.String(64), nullable=True),
        sa.Column("calculation_version", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("input_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        # NULLABLE on purpose — missing measurements must not become 0.
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "unknown_token_calls",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("total_cost_usd", sa.Float(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_evaluation_runs_project"),
        sa.CheckConstraint(
            "input_count >= 0 AND processed_count >= 0 "
            "AND success_count >= 0 AND failed_count >= 0 "
            "AND processed_count <= input_count "
            "AND success_count + failed_count <= processed_count",
            name="ck_evaluation_runs_non_negative_counts",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND retry_count >= 0 AND unknown_token_calls >= 0",
            name="ck_evaluation_runs_attempt_non_negative",
        ),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name="ck_evaluation_runs_input_tokens_non_negative",
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="ck_evaluation_runs_output_tokens_non_negative",
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_evaluation_runs_latency_non_negative",
        ),
        sa.CheckConstraint(
            "total_cost_usd IS NULL OR total_cost_usd >= 0",
            name="ck_evaluation_runs_cost_non_negative",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','partial_failed','failed')",
            name="ck_evaluation_runs_status_allowed",
        ),
        sa.CheckConstraint(
            "stage IN ('classification','clustering','insight',"
            "'pipeline','token','latency',"
            "'opportunity','requirement','prd')",
            name="ck_evaluation_runs_stage_allowed",
        ),
        sa.CheckConstraint(
            f"evaluation_type IN ({','.join(repr(t) for t in EVALUATION_TYPES)})",
            name="ck_evaluation_runs_type_allowed",
        ),
        sa.CheckConstraint(
            f"dataset_provenance IN ({','.join(repr(p) for p in EVALUATION_PROVENANCE)})",
            name="ck_evaluation_runs_provenance_allowed",
        ),
        sa.CheckConstraint(
            f"dataset_split IN ({','.join(repr(s) for s in EVALUATION_SPLIT)})",
            name="ck_evaluation_runs_split_allowed",
        ),
    )
    op.create_index("ix_evaluation_runs_project_id", "evaluation_runs", ["project_id"])
    op.create_index("ix_evaluation_runs_stage", "evaluation_runs", ["stage"])
    op.create_index(
        "ix_evaluation_runs_dataset_version",
        "evaluation_runs",
        ["dataset_name", "dataset_version"],
    )
    op.create_index("ix_evaluation_runs_status", "evaluation_runs", ["status"])
    op.create_index("ix_evaluation_runs_created_at", "evaluation_runs", ["created_at"])

    op.create_table(
        "evaluation_metrics",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("metric_name", sa.String(64), nullable=False),
        sa.Column("metric_kind", sa.String(32), nullable=False),
        sa.Column(
            "sample_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["evaluation_runs.id"], name="fk_evaluation_metrics_run"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_evaluation_metrics_project",
        ),
        sa.CheckConstraint(
            f"metric_kind IN ({','.join(repr(k) for k in METRIC_KINDS)})",
            name="ck_evaluation_metrics_metric_kind_allowed",
        ),
        sa.CheckConstraint(
            "stage IN ('classification','clustering','insight',"
            "'pipeline','token','latency',"
            "'opportunity','requirement','prd')",
            name="ck_evaluation_metrics_stage_allowed",
        ),
        sa.CheckConstraint(
            "sample_count >= 0",
            name="ck_evaluation_metrics_sample_count_non_negative",
        ),
        # value=NULL⇔NOT_MEASURED; sample_count=0 must yield value=NULL.
        sa.CheckConstraint(
            "(sample_count > 0) OR (value IS NULL)",
            name="ck_evaluation_metrics_value_requires_sample",
        ),
        sa.UniqueConstraint(
            "run_id",
            "stage",
            "metric_name",
            name="uq_evaluation_metrics_run_stage_metric",
        ),
    )
    op.create_index("ix_evaluation_metrics_run_id", "evaluation_metrics", ["run_id"])
    op.create_index(
        "ix_evaluation_metrics_project_id",
        "evaluation_metrics",
        ["project_id"],
    )
    op.create_index(
        "ix_evaluation_metrics_stage_metric",
        "evaluation_metrics",
        ["stage", "metric_name"],
    )

    op.create_table(
        "evaluation_cases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.String(200), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=True),
        sa.Column("expected_hash", sa.String(64), nullable=True),
        sa.Column("actual_hash", sa.String(64), nullable=True),
        # Sensitive-free raw fields: NULL by default, only populated when
        # the caller explicitly opts in (the brief: default behaviour
        # avoids storing sensitive raw content).
        sa.Column("input", sa.Text(), nullable=True),
        sa.Column("expected", sa.Text(), nullable=True),
        sa.Column("actual", sa.Text(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("error_type", sa.String(64), nullable=True),
        sa.Column(
            "eval_source",
            sa.String(64),
            nullable=False,
            server_default="csv_load",
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["evaluation_runs.id"], name="fk_evaluation_cases_run"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="fk_evaluation_cases_project"
        ),
        sa.CheckConstraint(
            "stage IN ('classification','clustering','insight',"
            "'pipeline','token','latency',"
            "'opportunity','requirement','prd')",
            name="ck_evaluation_cases_stage_allowed",
        ),
        sa.CheckConstraint(
            f"eval_source IN ({','.join(repr(s) for s in EVAL_CASE_SOURCES)})",
            name="ck_evaluation_cases_source_allowed",
        ),
        sa.CheckConstraint(
            f"error_type IS NULL OR error_type IN ({','.join(repr(e) for e in EVAL_CASE_ERROR_TYPES)})",
            name="ck_evaluation_cases_error_type_allowed",
        ),
        sa.UniqueConstraint(
            "run_id",
            "stage",
            "case_id",
            name="uq_evaluation_cases_run_stage_case",
        ),
    )
    op.create_index("ix_evaluation_cases_run_id", "evaluation_cases", ["run_id"])
    op.create_index(
        "ix_evaluation_cases_project_id",
        "evaluation_cases",
        ["project_id"],
    )
    op.create_index(
        "ix_evaluation_cases_stage_case_id",
        "evaluation_cases",
        ["stage", "case_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_evaluation_cases_stage_case_id", table_name="evaluation_cases")
    op.drop_index("ix_evaluation_cases_project_id", table_name="evaluation_cases")
    op.drop_index("ix_evaluation_cases_run_id", table_name="evaluation_cases")
    op.drop_table("evaluation_cases")

    op.drop_index("ix_evaluation_metrics_stage_metric", table_name="evaluation_metrics")
    op.drop_index("ix_evaluation_metrics_project_id", table_name="evaluation_metrics")
    op.drop_index("ix_evaluation_metrics_run_id", table_name="evaluation_metrics")
    op.drop_table("evaluation_metrics")

    op.drop_index("ix_evaluation_runs_created_at", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_status", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_dataset_version", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_stage", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_project_id", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
