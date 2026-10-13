"""Phase 4 — Opportunity Score + Human Decision.

Three tables back the Phase 4 contract:

* :class:`OpportunityScore` — immutable snapshot of one scoring run.
  Every dimension value, configured/effective weights, thresholds,
  source-snapshot hash and prompt/model binding is persisted so the
  audit chain can be replayed without depending on live data.
* :class:`InsightDecision` — current PM decision for one Insight.
  Holds the current Business Value, AI Priority, PM Priority, status,
  override reason / note and an optimistic-concurrency ``version``.
* :class:`InsightDecisionEvent` — append-only history of every PATCH
  the PM applies. The ``(insight_id, request_id)`` unique constraint
  provides idempotency; ``version_after`` captures the new version of
  the Decision row.

None of these tables modify the Phase 1—3 evidence surface.
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

# ---- Enumerations -------------------------------------------------------------


class Priority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


PRIORITY_VALUES: tuple[str, ...] = ("P0", "P1", "P2")


class DecisionStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    ACCEPTED = "accepted"
    OVERRIDDEN = "overridden"
    DEFERRED = "deferred"
    REJECTED = "rejected"


DECISION_PENDING_REVIEW = DecisionStatus.PENDING_REVIEW.value
DECISION_ACCEPTED = DecisionStatus.ACCEPTED.value
DECISION_OVERRIDDEN = DecisionStatus.OVERRIDDEN.value
DECISION_DEFERRED = DecisionStatus.DEFERRED.value
DECISION_REJECTED = DecisionStatus.REJECTED.value

DECISION_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in DecisionStatus)


class OverrideReason(str, Enum):
    STRATEGIC_PRIORITY = "strategic_priority"
    CORE_USER_EXPERIENCE = "core_user_experience"
    REVENUE_IMPACT = "revenue_impact"
    DEVELOPMENT_COST = "development_cost"
    MANAGEMENT_DECISION = "management_decision"
    DATA_QUALITY = "data_quality"
    OTHER = "other"


OVERRIDE_REASON_VALUES: tuple[str, ...] = tuple(r.value for r in OverrideReason)


class DecisionEventType(str, Enum):
    SET_BUSINESS_VALUE = "set_business_value"
    ACCEPT = "accept"
    OVERRIDE = "override"
    DEFER = "defer"
    REJECT = "reject"


DECISION_EVENTS: tuple[str, ...] = tuple(e.value for e in DecisionEventType)


# ---- Tables ------------------------------------------------------------------


class OpportunityScore(Base):
    """Immutable snapshot of one Opportunity Score computation."""

    __tablename__ = "opportunity_scores"
    __table_args__ = (
        CheckConstraint(
            "user_value_score IS NULL OR (user_value_score >= 0 AND user_value_score <= 10)",
            name="ck_opportunity_user_value_range",
        ),
        CheckConstraint(
            "business_value IS NULL OR (business_value >= 0 AND business_value <= 10)",
            name="ck_opportunity_business_value_range",
        ),
        CheckConstraint(
            "total_score IS NULL OR (total_score >= 0 AND total_score <= 10)",
            name="ck_opportunity_total_score_range",
        ),
        CheckConstraint(
            "ai_priority IS NULL OR ai_priority IN ('P0','P1','P2')",
            name="ck_opportunity_ai_priority_allowed",
        ),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND unknown_token_calls >= 0",
            name="ck_opportunity_token_counts_non_negative",
        ),
        CheckConstraint(
            "latency_ms >= 0 AND attempt_count >= 0 AND retry_count >= 0",
            name="ck_opportunity_attempt_metrics_non_negative",
        ),
        Index("ix_opportunity_scores_insight_id", "insight_id"),
        Index("ix_opportunity_scores_project_id", "project_id"),
        Index("ix_opportunity_scores_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    insight_id: Mapped[UUID] = mapped_column(ForeignKey("insights.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    classification_run_id: Mapped[UUID] = mapped_column(ForeignKey("analysis_runs.id"))

    # Source provenance — pinned at computation time so the audit chain
    # does not silently drift when the upstream insight is mutated.
    source_insight_status: Mapped[str] = mapped_column(String(32))
    source_snapshot_hash: Mapped[str] = mapped_column(String(64))

    # Per-dimension breakdown with raw values, scores, availability and
    # auxiliary counters (counts, average_severity, growth windows).
    breakdown: Mapped[dict] = mapped_column(JSON)
    weights_configured: Mapped[dict] = mapped_column(JSON)
    weights_effective: Mapped[dict] = mapped_column(JSON)
    thresholds: Mapped[dict] = mapped_column(JSON)

    business_value: Mapped[float | None] = mapped_column(Float)
    total_score: Mapped[float | None] = mapped_column(Float)
    ai_priority: Mapped[str | None] = mapped_column(String(4))

    # AI User Value (Strict Structured Output) and the deterministic
    # formula reasoning string.
    user_value_score: Mapped[float | None] = mapped_column(Float)
    user_value_reasoning: Mapped[str] = mapped_column(Text, default="")
    ai_reasoning: Mapped[str] = mapped_column(Text, default="")

    calculation_version: Mapped[str] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(100))

    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    unknown_token_calls: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class InsightDecision(Base):
    """Current PM decision for one Insight."""

    __tablename__ = "insight_decisions"
    __table_args__ = (
        UniqueConstraint("insight_id", name="uq_insight_decisions_insight"),
        CheckConstraint(
            "business_value IS NULL OR (business_value >= 0 AND business_value <= 10)",
            name="ck_insight_decisions_business_value_range",
        ),
        CheckConstraint(
            "ai_priority IS NULL OR ai_priority IN ('P0','P1','P2')",
            name="ck_insight_decisions_ai_priority_allowed",
        ),
        CheckConstraint(
            "pm_priority IS NULL OR pm_priority IN ('P0','P1','P2')",
            name="ck_insight_decisions_pm_priority_allowed",
        ),
        CheckConstraint(
            "status IN ('pending_review','accepted','overridden','deferred','rejected')",
            name="ck_insight_decisions_status_allowed",
        ),
        CheckConstraint(
            "override_reason IS NULL OR override_reason IN ("
            "'strategic_priority','core_user_experience','revenue_impact',"
            "'development_cost','management_decision','data_quality','other')",
            name="ck_insight_decisions_override_reason_allowed",
        ),
        CheckConstraint(
            "(override_reason <> 'other') OR (override_note IS NOT NULL AND length(override_note) > 0)",
            name="ck_insight_decisions_other_note_required",
        ),
        CheckConstraint("version >= 0", name="ck_insight_decisions_version_non_negative"),
        Index("ix_insight_decisions_project_id", "project_id"),
        Index("ix_insight_decisions_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    insight_id: Mapped[UUID] = mapped_column(ForeignKey("insights.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    current_score_id: Mapped[UUID | None] = mapped_column(ForeignKey("opportunity_scores.id"))
    business_value: Mapped[float | None] = mapped_column(Float)
    ai_priority: Mapped[str | None] = mapped_column(String(4))
    pm_priority: Mapped[str | None] = mapped_column(String(4))
    status: Mapped[str] = mapped_column(String(32), default=DECISION_PENDING_REVIEW)
    override_reason: Mapped[str | None] = mapped_column(String(64))
    override_note: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class InsightDecisionEvent(Base):
    """Append-only history of PM Decision PATCHes."""

    __tablename__ = "insight_decision_events"
    __table_args__ = (
        UniqueConstraint(
            "insight_id",
            "request_id",
            name="uq_insight_decision_events_insight_request",
        ),
        CheckConstraint(
            "event_type IN ('set_business_value','accept','override','defer','reject')",
            name="ck_insight_decision_events_event_type_allowed",
        ),
        CheckConstraint(
            "business_value IS NULL OR (business_value >= 0 AND business_value <= 10)",
            name="ck_insight_decision_events_business_value_range",
        ),
        CheckConstraint(
            "ai_priority IS NULL OR ai_priority IN ('P0','P1','P2')",
            name="ck_insight_decision_events_ai_priority_allowed",
        ),
        CheckConstraint(
            "pm_priority IS NULL OR pm_priority IN ('P0','P1','P2')",
            name="ck_insight_decision_events_pm_priority_allowed",
        ),
        CheckConstraint(
            "override_reason IS NULL OR override_reason IN ("
            "'strategic_priority','core_user_experience','revenue_impact',"
            "'development_cost','management_decision','data_quality','other')",
            name="ck_insight_decision_events_override_reason_allowed",
        ),
        CheckConstraint(
            "(override_reason <> 'other') OR (override_note IS NOT NULL AND length(override_note) > 0)",
            name="ck_insight_decision_events_other_note_required",
        ),
        CheckConstraint(
            "version_after >= 0",
            name="ck_insight_decision_events_version_after_non_negative",
        ),
        Index("ix_insight_decision_events_insight_id", "insight_id"),
        Index("ix_insight_decision_events_decision_id", "decision_id"),
        Index("ix_insight_decision_events_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    insight_id: Mapped[UUID] = mapped_column(ForeignKey("insights.id"))
    decision_id: Mapped[UUID] = mapped_column(ForeignKey("insight_decisions.id"))
    score_id: Mapped[UUID | None] = mapped_column(ForeignKey("opportunity_scores.id"))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    event_type: Mapped[str] = mapped_column(String(32))
    before_state: Mapped[dict | None] = mapped_column(JSON)
    after_state: Mapped[dict | None] = mapped_column(JSON)
    business_value: Mapped[float | None] = mapped_column(Float)
    ai_priority: Mapped[str | None] = mapped_column(String(4))
    pm_priority: Mapped[str | None] = mapped_column(String(4))
    override_reason: Mapped[str | None] = mapped_column(String(64))
    override_note: Mapped[str | None] = mapped_column(Text)
    version_after: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


__all__ = [
    "DECISION_ACCEPTED",
    "DECISION_DEFERRED",
    "DECISION_EVENTS",
    "DECISION_OVERRIDDEN",
    "DECISION_PENDING_REVIEW",
    "DECISION_REJECTED",
    "DECISION_STATUS_VALUES",
    "DecisionEventType",
    "DecisionStatus",
    "InsightDecision",
    "InsightDecisionEvent",
    "OpportunityScore",
    "OVERRIDE_REASON_VALUES",
    "OverrideReason",
    "PRIORITY_VALUES",
    "Priority",
]
