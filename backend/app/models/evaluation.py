"""Phase 6 — Evaluation + Regression + Observability.

Three ORM classes back the Phase 6 evaluation harness:

* :class:`EvaluationRun` — one row per (project, evaluation_type,
  dataset, model) snapshot. Token, latency and cost columns are
  NULLABLE because a missing measurement MUST NOT be silently coerced
  to a true zero (the brief: "缺测绝不能假零"). ``status`` follows the
  same five-state pipeline as Phases 1—5 so the dashboard can be
  consistent across phases. ``calculation_version`` is the metric
  package version (``metrics.PHASE6_CALCULATION_VERSION``) — it pins
  the metric math so a side-by-side comparison across Prometheus
  rows doesn't silently mix two metric package versions.

* :class:`EvaluationMetric` — one row per (run, stage, metric_name).
  ``metric_kind`` distinguishes ``deterministic`` /
  ``human_reviewed`` / ``llm_judge``. ``value`` is NULL when the
  metric was not measurable; ``sample_count`` carries the denominator.
  A CHECK enforces ``(sample_count > 0) OR (value IS NULL)`` so an
  empty sample cannot accidentally claim a numeric value.

* :class:`EvaluationCase` — per-case record. By default ``input`` /
  ``expected`` / ``actual`` remain NULL so the database does not
  silently persist sensitive raw feedback content. The fingerprint
  ``*_hash`` columns store the SHA-256 of the corresponding value so a
  regression comparison can detect content drift without storing the
  raw text. Callers may opt-in to persisting one or more raw fields
  via an explicit store-raw policy at write time — there is no hidden
  ``True`` default. ``eval_source`` distinguishes ``csv_load`` /
  ``db_record`` / ``synthetic`` / ``not_applicable``. ``passed`` is a
  tri-state boolean so a non-applicable case can sit between pass and
  fail without faking either side.

No Phase 1—5 table is modified here. Raw feedback, classifications,
clusters, insights, decisions and PRDs remain immutable.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utc_now

# ---- Enumerations (single source of truth shared by migration + model) -------


EVALUATION_TYPES: tuple[str, ...] = (
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


EVALUATION_PROVENANCE: tuple[str, ...] = (
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


EVALUATION_SPLIT: tuple[str, ...] = (
    "full",
    "train",
    "validation",
    "test",
    "mixed",
)


EVALUATION_STAGES: tuple[str, ...] = (
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


EVALUATION_RUN_STATUSES: tuple[str, ...] = (
    "pending",
    "running",
    "completed",
    "partial_failed",
    "failed",
)


METRIC_KINDS: tuple[str, ...] = (
    "deterministic",
    "human_reviewed",
    "llm_judge",
)


EVAL_CASE_SOURCES: tuple[str, ...] = (
    "csv_load",
    "db_record",
    "synthetic",
    "not_applicable",
)


EVAL_CASE_ERROR_TYPES: tuple[str, ...] = (
    "no_prediction",
    "protocol_error",
    "transport_error",
    "validation_error",
    "config_error",
    "not_measured",
    "other",
)


class EvaluationRunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"


class EvaluationStage(str, Enum):
    CLASSIFICATION = "classification"
    CLUSTERING = "clustering"
    INSIGHT = "insight"
    PIPELINE = "pipeline"
    TOKEN = "token"
    LATENCY = "latency"


class MetricKind(str, Enum):
    DETERMINISTIC = "deterministic"
    HUMAN_REVIEWED = "human_reviewed"
    LLM_JUDGE = "llm_judge"


class EvaluationCaseSource(str, Enum):
    CSV_LOAD = "csv_load"
    DB_RECORD = "db_record"
    SYNTHETIC = "synthetic"
    NOT_APPLICABLE = "not_applicable"


class EvaluationCaseErrorType(str, Enum):
    NO_PREDICTION = "no_prediction"
    PROTOCOL_ERROR = "protocol_error"
    TRANSPORT_ERROR = "transport_error"
    VALIDATION_ERROR = "validation_error"
    CONFIG_ERROR = "config_error"
    NOT_MEASURED = "not_measured"
    OTHER = "other"


# ---- Tables ------------------------------------------------------------------


class EvaluationRun(Base):
    """One evaluation run snapshot."""

    __tablename__ = "evaluation_runs"
    __table_args__ = (
        CheckConstraint(
            "input_count >= 0 AND processed_count >= 0 "
            "AND success_count >= 0 AND failed_count >= 0 "
            "AND processed_count <= input_count "
            "AND success_count + failed_count <= processed_count",
            name="ck_evaluation_runs_non_negative_counts",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND retry_count >= 0 AND unknown_token_calls >= 0",
            name="ck_evaluation_runs_attempt_non_negative",
        ),
        CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name="ck_evaluation_runs_input_tokens_non_negative",
        ),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="ck_evaluation_runs_output_tokens_non_negative",
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_evaluation_runs_latency_non_negative",
        ),
        CheckConstraint(
            "total_cost_usd IS NULL OR total_cost_usd >= 0",
            name="ck_evaluation_runs_cost_non_negative",
        ),
        CheckConstraint(
            f"status IN ({','.join(repr(s.value) if isinstance(s, Enum) else repr(s) for s in EVALUATION_RUN_STATUSES)})",
            name="ck_evaluation_runs_status_allowed",
        ),
        CheckConstraint(
            f"stage IN ({','.join(repr(s.value) if isinstance(s, Enum) else repr(s) for s in EVALUATION_STAGES)})",
            name="ck_evaluation_runs_stage_allowed",
        ),
        CheckConstraint(
            f"evaluation_type IN ({','.join(repr(t) for t in EVALUATION_TYPES)})",
            name="ck_evaluation_runs_type_allowed",
        ),
        CheckConstraint(
            f"dataset_provenance IN ({','.join(repr(p) for p in EVALUATION_PROVENANCE)})",
            name="ck_evaluation_runs_provenance_allowed",
        ),
        CheckConstraint(
            f"dataset_split IN ({','.join(repr(s) for s in EVALUATION_SPLIT)})",
            name="ck_evaluation_runs_split_allowed",
        ),
        Index("ix_evaluation_runs_project_id", "project_id"),
        Index("ix_evaluation_runs_stage", "stage"),
        Index("ix_evaluation_runs_dataset_version", "dataset_name", "dataset_version"),
        Index("ix_evaluation_runs_status", "status"),
        Index("ix_evaluation_runs_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))

    evaluation_type: Mapped[str] = mapped_column(String(64))
    dataset_name: Mapped[str] = mapped_column(String(64))
    dataset_version: Mapped[str] = mapped_column(String(64))
    dataset_content_hash: Mapped[str] = mapped_column(String(64))
    dataset_provenance: Mapped[str] = mapped_column(String(64))
    dataset_split: Mapped[str] = mapped_column(String(32))
    dataset_label_status: Mapped[str] = mapped_column(String(200))
    stage: Mapped[str] = mapped_column(String(32))

    model: Mapped[Optional[str]] = mapped_column(String(100))
    prompt_version: Mapped[Optional[str]] = mapped_column(String(100))
    pipeline_version: Mapped[Optional[str]] = mapped_column(String(64))
    calculation_version: Mapped[str] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(String(20), default=EvaluationRunStatus.PENDING.value)

    input_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)

    # NULLABLE on purpose — missing measurements must not become 0.
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    unknown_token_calls: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    total_cost_usd: Mapped[Optional[float]] = mapped_column(Float)

    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    started_at: Mapped[Optional[object]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[object]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utc_now)

    error_message: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)


class EvaluationMetric(Base):
    """One metric measurement on one evaluation run."""

    __tablename__ = "evaluation_metrics"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "stage",
            "metric_name",
            name="uq_evaluation_metrics_run_stage_metric",
        ),
        CheckConstraint(
            f"metric_kind IN ({','.join(repr(k) for k in METRIC_KINDS)})",
            name="ck_evaluation_metrics_metric_kind_allowed",
        ),
        CheckConstraint(
            "stage IN ('classification','clustering','insight',"
            "'pipeline','token','latency',"
            "'opportunity','requirement','prd')",
            name="ck_evaluation_metrics_stage_allowed",
        ),
        CheckConstraint(
            "sample_count >= 0",
            name="ck_evaluation_metrics_sample_count_non_negative",
        ),
        CheckConstraint(
            "(sample_count > 0) OR (value IS NULL)",
            name="ck_evaluation_metrics_value_requires_sample",
        ),
        Index("ix_evaluation_metrics_run_id", "run_id"),
        Index("ix_evaluation_metrics_project_id", "project_id"),
        Index("ix_evaluation_metrics_stage_metric", "stage", "metric_name"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("evaluation_runs.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    stage: Mapped[str] = mapped_column(String(32))
    metric_name: Mapped[str] = mapped_column(String(64))
    metric_kind: Mapped[str] = mapped_column(String(32))

    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    value: Mapped[Optional[float]] = mapped_column(Float)
    details: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utc_now)


class EvaluationCase(Base):
    """Per-case record on one evaluation run.

    ``input`` / ``expected`` / ``actual`` default to NULL — the framework
    does NOT silently persist raw feedback content. The fingerprint
    ``*_hash`` columns store SHA-256 of the corresponding value so a
    regression comparison can detect drift without leaking the raw
    text. Callers MAY opt in to writing raw fields (the policy is
    explicit at write time).
    """

    __tablename__ = "evaluation_cases"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "stage",
            "case_id",
            name="uq_evaluation_cases_run_stage_case",
        ),
        CheckConstraint(
            "stage IN ('classification','clustering','insight',"
            "'pipeline','token','latency',"
            "'opportunity','requirement','prd')",
            name="ck_evaluation_cases_stage_allowed",
        ),
        CheckConstraint(
            f"eval_source IN ({','.join(repr(s) for s in EVAL_CASE_SOURCES)})",
            name="ck_evaluation_cases_source_allowed",
        ),
        CheckConstraint(
            f"error_type IS NULL OR error_type IN ({','.join(repr(e) for e in EVAL_CASE_ERROR_TYPES)})",
            name="ck_evaluation_cases_error_type_allowed",
        ),
        Index("ix_evaluation_cases_run_id", "run_id"),
        Index("ix_evaluation_cases_project_id", "project_id"),
        Index("ix_evaluation_cases_stage_case_id", "stage", "case_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("evaluation_runs.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    case_id: Mapped[str] = mapped_column(String(200))
    stage: Mapped[str] = mapped_column(String(32))

    input_hash: Mapped[Optional[str]] = mapped_column(String(64))
    expected_hash: Mapped[Optional[str]] = mapped_column(String(64))
    actual_hash: Mapped[Optional[str]] = mapped_column(String(64))

    # Sensitive-free raw fields. NULL by default; only populated if the
    # caller explicitly opts in.
    input: Mapped[Optional[str]] = mapped_column(Text)
    expected: Mapped[Optional[str]] = mapped_column(Text)
    actual: Mapped[Optional[str]] = mapped_column(Text)

    # Tri-state: True / False / None (None = not measurable).
    passed: Mapped[Optional[bool]] = mapped_column(Boolean)
    error_type: Mapped[Optional[str]] = mapped_column(String(64))
    eval_source: Mapped[str] = mapped_column(
        String(64), default=EvaluationCaseSource.CSV_LOAD.value
    )
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utc_now)


__all__ = [
    "EVALUATION_TYPES",
    "EVALUATION_PROVENANCE",
    "EVALUATION_SPLIT",
    "EVALUATION_STAGES",
    "EVALUATION_RUN_STATUSES",
    "METRIC_KINDS",
    "EVAL_CASE_SOURCES",
    "EVAL_CASE_ERROR_TYPES",
    "EvaluationRun",
    "EvaluationRunStatus",
    "EvaluationStage",
    "MetricKind",
    "EvaluationCase",
    "EvaluationCaseSource",
    "EvaluationCaseErrorType",
    "EvaluationMetric",
]
