"""Phase 3 — Insight + Evidence.

Three new tables own the insight-generation pipeline:

* :class:`InsightRun` — a dedicated run object for a clustering
  ``analysis_runs`` row. The run captures the model binding, the prompt
  version, five-state status, per-attempt token / latency accounting
  (including failed calls), the snapshot of input cluster IDs and the
  per-cluster member external IDs so a retry cannot expand the input set.
* :class:`Insight` — one row per cluster. ``status`` is
  ``draft`` (≥3 evidence) or ``needs_validation`` (1–2 evidence); the
  actionable fields may be ``NULL`` for ``needs_validation`` because the
  contract requires human review before any business-level claim.
* :class:`InsightEvidence` — one row per (insight, feedback) pair. The
  unique constraint guarantees a feedback cannot appear twice for the same
  insight. ``evidence_type`` distinguishes representative / high_severity /
  diverse_expression picks.

Per-cluster status and per-attempt failures live in
:class:`InsightRunItem`. The original ``feedbacks.content`` and the
history tables created by Phases 1 and 2 are never modified.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
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


class InsightStatus(str, Enum):
    DRAFT = "draft"
    NEEDS_VALIDATION = "needs_validation"


class InsightItemStatus(str, Enum):
    SUCCESS = "success"
    NEEDS_VALIDATION = "needs_validation"
    FAILED = "failed"


class EvidenceType(str, Enum):
    REPRESENTATIVE = "representative"
    HIGH_SEVERITY = "high_severity"
    DIVERSE_EXPRESSION = "diverse_expression"


class InsightRun(Base):
    """One insight generation run against a completed clustering run."""

    __tablename__ = "insight_runs"
    __table_args__ = (
        CheckConstraint(
            "input_count >= 0 AND processed_count >= 0 AND success_count >= 0 "
            "AND failed_count >= 0 AND needs_validation_count >= 0 "
            "AND retry_count >= 0 AND latency_ms >= 0 AND attempt_count >= 0 "
            "AND unknown_token_calls >= 0 AND input_tokens >= 0 "
            "AND output_tokens >= 0 "
            "AND processed_count <= input_count "
            "AND success_count + failed_count + needs_validation_count <= processed_count",
            name="ck_insight_runs_non_negative_counts",
        ),
        Index("ix_insight_runs_project_id", "project_id"),
        Index("ix_insight_runs_clustering_run_id", "clustering_run_id"),
        Index("ix_insight_runs_status", "status"),
        Index("ix_insight_runs_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    clustering_run_id: Mapped[UUID] = mapped_column(ForeignKey("analysis_runs.id"))
    classification_run_id: Mapped[UUID] = mapped_column(ForeignKey("analysis_runs.id"))

    # Five-state pipeline status shared with Phases 1/2.
    status: Mapped[str] = mapped_column(String(20), default="pending")
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(100))

    input_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    needs_validation_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)

    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    unknown_token_calls: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    error_message: Mapped[str | None] = mapped_column(Text)
    # Snapshot of the input cluster UUIDs and per-cluster feedback external IDs
    # captured at run start. A retry must use this snapshot so it cannot
    # silently expand when a fresh clustering run completes.
    input_cluster_external_ids: Mapped[list[str] | None] = mapped_column(JSON)
    input_member_external_ids: Mapped[dict[str, list[str]] | None] = mapped_column(JSON)
    # Full per-cluster snapshot: cluster UUID, member feedback UUIDs, and
    # a SHA-256 hash of each member's raw content. Retries verify the live
    # data still matches the snapshot before doing any work.
    input_snapshot: Mapped[dict | None] = mapped_column(JSON)


class InsightRunItem(Base):
    """Per-cluster execution record. The source of truth for retries."""

    __tablename__ = "insight_run_items"
    __table_args__ = (
        UniqueConstraint("run_id", "cluster_id", name="uq_insight_run_item_run_cluster"),
        CheckConstraint("attempts >= 1", name="ck_insight_run_item_attempts_positive"),
        CheckConstraint(
            "status IN ('success','needs_validation','failed')",
            name="ck_insight_run_item_status_allowed",
        ),
        CheckConstraint(
            "feedback_count >= 0 AND evidence_count >= 0",
            name="ck_insight_run_item_non_negative",
        ),
        Index("ix_insight_run_items_run_id", "run_id"),
        Index("ix_insight_run_items_cluster_id", "cluster_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("insight_runs.id", ondelete="CASCADE"))
    cluster_id: Mapped[UUID] = mapped_column(ForeignKey("clusters.id"))
    source_cluster_external_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20))
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    last_error: Mapped[str | None] = mapped_column(Text)
    feedback_count: Mapped[int] = mapped_column(Integer, default=0)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Insight(Base):
    __tablename__ = "insights"
    __table_args__ = (
        UniqueConstraint("run_id", "cluster_id", name="uq_insight_run_cluster"),
        CheckConstraint(
            "status IN ('draft','needs_validation')",
            name="ck_insight_status_allowed",
        ),
        CheckConstraint(
            "status <> 'draft' OR (insight IS NOT NULL AND pain_point IS NOT NULL "
            "AND affected_segment IS NOT NULL AND recommendation IS NOT NULL "
            "AND title IS NOT NULL AND confidence IS NOT NULL)",
            name="ck_insight_draft_completeness",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_insight_confidence_range",
        ),
        CheckConstraint("feedback_count >= 0", name="ck_insight_feedback_count_non_negative"),
        CheckConstraint("evidence_count >= 0", name="ck_insight_evidence_count_non_negative"),
        Index("ix_insights_project_id", "project_id"),
        Index("ix_insights_run_id", "run_id"),
        Index("ix_insights_cluster_id", "cluster_id"),
        Index("ix_insights_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("insight_runs.id", ondelete="CASCADE"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    cluster_id: Mapped[UUID] = mapped_column(ForeignKey("clusters.id"))

    title: Mapped[str | None] = mapped_column(String(200))
    pain_point: Mapped[str | None] = mapped_column(Text)
    affected_segment: Mapped[str | None] = mapped_column(Text)
    insight: Mapped[str | None] = mapped_column(Text)
    recommendation: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)

    status: Mapped[str] = mapped_column(String(20), default=InsightStatus.DRAFT.value)
    feedback_count: Mapped[int] = mapped_column(Integer, default=0)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)

    # Snapshot the cluster external id so the read APIs can trace provenance
    # even if the underlying cluster row changes after a re-clustering.
    source_cluster_external_id: Mapped[str] = mapped_column(String(64))
    source_member_external_ids: Mapped[list[str]] = mapped_column(JSON, default=list)

    # Model / prompt binding persisted on the row so a later retry cannot
    # silently swap the contract.
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(100))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class InsightEvidence(Base):
    """Hard guard: a feedback may appear at most once per insight row."""

    __tablename__ = "insight_evidence"
    __table_args__ = (
        UniqueConstraint("insight_id", "feedback_id", name="uq_insight_evidence_insight_feedback"),
        CheckConstraint(
            "relevance_score >= 0 AND relevance_score <= 1",
            name="ck_insight_evidence_relevance_range",
        ),
        CheckConstraint(
            "evidence_type IN ('representative','high_severity','diverse_expression')",
            name="ck_insight_evidence_type_allowed",
        ),
        Index("ix_insight_evidence_insight_id", "insight_id"),
        Index("ix_insight_evidence_feedback_id", "feedback_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    insight_id: Mapped[UUID] = mapped_column(ForeignKey("insights.id", ondelete="CASCADE"))
    feedback_id: Mapped[UUID] = mapped_column(ForeignKey("feedbacks.id"))
    evidence_type: Mapped[str] = mapped_column(String(32))
    relevance_score: Mapped[float] = mapped_column(Float)
    # Stored on the row so a future re-classification of feedback cannot
    # silently change the evidence surface; raw text remains in ``feedbacks``
    # as the single source of truth.
    feedback_external_id: Mapped[str] = mapped_column(String(200))
    feedback_project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    feedback_classification_run_id: Mapped[UUID] = mapped_column(ForeignKey("analysis_runs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


__all__ = [
    "EvidenceType",
    "Insight",
    "InsightEvidence",
    "InsightRun",
    "InsightRunItem",
    "InsightStatus",
    "InsightItemStatus",
]
