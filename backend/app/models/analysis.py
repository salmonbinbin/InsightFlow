"""Phase 1: AnalysisRun, FeedbackAnalysis and per-feedback failure tracking.

The ``feedbacks`` table remains the immutable raw evidence source; analysis rows
only reference ``feedbacks.id`` by foreign key and never overwrite content.
"""

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


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"
    __table_args__ = (
        CheckConstraint(
            "input_count >= 0 AND processed_count >= 0 AND success_count >= 0 "
            "AND failed_count >= 0 AND retry_count >= 0 AND latency_ms >= 0 "
            "AND attempt_count >= 0 AND unknown_token_calls >= 0 "
            "AND processed_count <= input_count "
            "AND success_count + failed_count <= processed_count",
            name="ck_analysis_runs_non_negative_counts",
        ),
        Index("ix_analysis_runs_project_id", "project_id"),
        Index("ix_analysis_runs_status", "status"),
        Index("ix_analysis_runs_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    run_type: Mapped[str] = mapped_column(String(50))
    # The model name captured at run creation; retries must reuse the same
    # model or be rejected as a configuration conflict.
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.PENDING.value)

    input_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)

    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Number of LLM attempts where usage tokens were missing or unparseable.
    # Distinguishes "not measured" from a true zero.
    unknown_token_calls: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    # Total latency measured at the LLM call level — sum across all attempts
    # including failures that still produced a measurable HTTP exchange.
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    # Number of LLM calls actually made (initial + retries).
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    error_message: Mapped[str | None] = mapped_column(Text)
    # Snapshot of the feedback external_ids captured at run creation so a
    # retry cannot expand the input set when new feedback rows are
    # imported after the run was started.
    input_feedback_external_ids: Mapped[list[str] | None] = mapped_column(JSON)
    cluster_config: Mapped[dict | None] = mapped_column(JSON)


class FeedbackAnalysis(Base):
    __tablename__ = "feedback_analyses"
    __table_args__ = (
        UniqueConstraint("feedback_id", "run_id", name="uq_feedback_analysis_feedback_run"),
        CheckConstraint("severity BETWEEN 1 AND 5", name="ck_feedback_analysis_severity"),
        CheckConstraint(
            "confidence BETWEEN 0 AND 1", name="ck_feedback_analysis_confidence"
        ),
        Index("ix_feedback_analyses_run_id", "run_id"),
        Index("ix_feedback_analyses_feedback_id", "feedback_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    feedback_id: Mapped[UUID] = mapped_column(ForeignKey("feedbacks.id"))
    run_id: Mapped[UUID] = mapped_column(ForeignKey("analysis_runs.id"))

    feedback_type: Mapped[str] = mapped_column(String(50))
    module: Mapped[str] = mapped_column(String(100))
    intent: Mapped[str] = mapped_column(String(200))
    pain_point: Mapped[str] = mapped_column(Text)
    sentiment: Mapped[str] = mapped_column(String(20))
    severity: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float)
    semantic_text: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AnalysisRunFailure(Base):
    """Per-feedback failure record so retries only re-process failed IDs.

    Successful analyses live in ``feedback_analyses``; ``AnalysisRunFailure`` is
    the source of truth for which IDs still need to be processed by retry.
    """

    __tablename__ = "analysis_run_failures"
    __table_args__ = (
        UniqueConstraint("run_id", "feedback_id", name="uq_run_failure_run_feedback"),
        CheckConstraint("attempts >= 1", name="ck_run_failure_attempts_positive"),
        Index("ix_run_failures_run_id", "run_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("analysis_runs.id"))
    feedback_id: Mapped[UUID] = mapped_column(ForeignKey("feedbacks.id"))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )