"""Phase 5 — Requirement + PRD.

Two new tables own the requirement / PRD pipeline on top of Phase 4
decisions:

* :class:`Requirement` — one row per Insight. The
  ``UNIQUE(insight_id)`` constraint guarantees a single current
  requirement per insight; a repeated POST is rejected with 409 so the
  PM must explicitly PATCH or archive the existing row to replace it
  (the rule against silent duplicates in the Phase 5 brief). Priority
  is inherited verbatim from the Phase 4 PM Priority at creation time
  and is captured as a snapshot — the AI never recomputes it.
* :class:`PRD` — versioned product document for one requirement. Each
  ``POST /requirements/{requirement_id}/prds`` appends a new version
  by atomically incrementing ``requirements.next_version``; the unique
  ``(requirement_id, version)`` constraint guarantees version monotonicity
  even under concurrent requests. Every version is preserved so PM
  edits and historical drafts are never overwritten by a regen.

No Phase 1—4 table or business rule is modified here. The Raw feedback
and analysis / cluster / insight / decision history remain untouched.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
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

# ---- Enumerations ------------------------------------------------------------


class RequirementStatus(str, Enum):
    DRAFT = "draft"
    REVIEWING = "reviewing"
    APPROVED = "approved"
    REJECTED = "rejected"
    ARCHIVED = "archived"


REQUIREMENT_DRAFT = RequirementStatus.DRAFT.value
REQUIREMENT_REVIEWING = RequirementStatus.REVIEWING.value
REQUIREMENT_APPROVED = RequirementStatus.APPROVED.value
REQUIREMENT_REJECTED = RequirementStatus.REJECTED.value
REQUIREMENT_ARCHIVED = RequirementStatus.ARCHIVED.value

REQUIREMENT_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in RequirementStatus)


class PRDStatus(str, Enum):
    DRAFT = "draft"
    REVIEWING = "reviewing"
    ACCEPTED = "accepted"
    ARCHIVED = "archived"


PRD_DRAFT = PRDStatus.DRAFT.value
PRD_REVIEWING = PRDStatus.REVIEWING.value
PRD_ACCEPTED = PRDStatus.ACCEPTED.value
PRD_ARCHIVED = PRDStatus.ARCHIVED.value

PRD_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in PRDStatus)


class RiskCategory(str, Enum):
    KNOWN = "known"
    SPECULATIVE = "speculative"


RISK_KNOWN = RiskCategory.KNOWN.value
RISK_SPECULATIVE = RiskCategory.SPECULATIVE.value

RISK_CATEGORY_VALUES: tuple[str, ...] = tuple(r.value for r in RiskCategory)


# ---- Requirement -------------------------------------------------------------


class Requirement(Base):
    """Structured Requirement derived from a Phase 4 PM Decision."""

    __tablename__ = "requirements"
    __table_args__ = (
        UniqueConstraint("insight_id", name="uq_requirements_insight"),
        CheckConstraint(
            "status IN ('draft','reviewing','approved','rejected','archived')",
            name="ck_requirements_status_allowed",
        ),
        CheckConstraint(
            "pm_priority IS NULL OR pm_priority IN ('P0','P1','P2')",
            name="ck_requirements_pm_priority_allowed",
        ),
        CheckConstraint(
            "ai_priority_snapshot IS NULL OR ai_priority_snapshot IN ('P0','P1','P2')",
            name="ck_requirements_ai_priority_snapshot_allowed",
        ),
        CheckConstraint(
            "decision_status_snapshot IS NULL OR decision_status_snapshot IN ("
            "'pending_review','accepted','overridden','deferred','rejected')",
            name="ck_requirements_decision_status_allowed",
        ),
        CheckConstraint(
            "insight_status_snapshot IS NULL OR insight_status_snapshot IN ("
            "'draft','needs_validation')",
            name="ck_requirements_insight_status_allowed",
        ),
        CheckConstraint(
            "next_version >= 1",
            name="ck_requirements_next_version_positive",
        ),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND unknown_token_calls >= 0",
            name="ck_requirements_token_counts_non_negative",
        ),
        CheckConstraint(
            "latency_ms >= 0 AND attempt_count >= 0 AND retry_count >= 0",
            name="ck_requirements_attempt_metrics_non_negative",
        ),
        Index("ix_requirements_project_id", "project_id"),
        Index("ix_requirements_insight_id", "insight_id"),
        Index("ix_requirements_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    insight_id: Mapped[UUID] = mapped_column(ForeignKey("insights.id"))
    decision_id: Mapped[UUID] = mapped_column(ForeignKey("insight_decisions.id"))
    score_id: Mapped[UUID | None] = mapped_column(ForeignKey("opportunity_scores.id"))

    # Phase 4 decision snapshot — captured at requirement creation so a
    # subsequent Phase 4 PATCH cannot silently rewrite the requirement's
    # priority provenance.
    insight_status_snapshot: Mapped[str] = mapped_column(String(20))
    decision_status_snapshot: Mapped[str] = mapped_column(String(32))
    ai_priority_snapshot: Mapped[str | None] = mapped_column(String(4))
    pm_priority: Mapped[str] = mapped_column(String(4))
    pm_override_reason_snapshot: Mapped[str | None] = mapped_column(String(64))
    source_snapshot_hash: Mapped[str] = mapped_column(String(64))

    # Editable content.
    name: Mapped[str] = mapped_column(String(200))
    problem: Mapped[str] = mapped_column(Text)
    target_user: Mapped[str] = mapped_column(Text)
    user_story: Mapped[str] = mapped_column(Text)
    solution_hypothesis: Mapped[str] = mapped_column(Text)

    # Structured JSON lists with per-item grounding metadata.
    success_metrics: Mapped[list] = mapped_column(JSON)
    risks: Mapped[list] = mapped_column(JSON)
    evidence_references: Mapped[list] = mapped_column(JSON)

    status: Mapped[str] = mapped_column(String(20), default=REQUIREMENT_DRAFT)

    # AI usage + model binding.
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    unknown_token_calls: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    # Atomic counter for PRD version assignment — see ``PRD`` docstring.
    next_version: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


# ---- PRD ---------------------------------------------------------------------


class PRD(Base):
    """Versioned PRD draft derived from one Requirement.

    Each row carries an immutable structured ``content`` JSON matching
    the 13-section contract from the Phase 5 brief. ``is_ai_generated_draft``
    is true for rows produced by the AI client and false for rows created
    directly from PM input (used by ``POST`` accept to detect a draft that
    was never AI-generated). ``grounding_warnings`` stores the
    deterministic validation issues that survived PM review (the AI must
    block obvious fabrications but PM may still accept a warning-laden
    draft after acknowledging them).
    """

    __tablename__ = "prds"
    __table_args__ = (
        UniqueConstraint("requirement_id", "version", name="uq_prds_requirement_version"),
        CheckConstraint("version >= 1", name="ck_prds_version_positive"),
        CheckConstraint(
            "status IN ('draft','reviewing','accepted','archived')",
            name="ck_prds_status_allowed",
        ),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND unknown_token_calls >= 0",
            name="ck_prds_token_counts_non_negative",
        ),
        CheckConstraint(
            "latency_ms >= 0 AND attempt_count >= 0 AND retry_count >= 0",
            name="ck_prds_attempt_metrics_non_negative",
        ),
        Index("ix_prds_requirement_id", "requirement_id"),
        Index("ix_prds_status", "status"),
        Index("ix_prds_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    requirement_id: Mapped[UUID] = mapped_column(ForeignKey("requirements.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    insight_id: Mapped[UUID] = mapped_column(ForeignKey("insights.id"))

    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default=PRD_DRAFT)

    content: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))

    is_ai_generated_draft: Mapped[bool] = mapped_column(default=True)
    grounding_warnings: Mapped[list] = mapped_column(JSON, default=list)

    model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    unknown_token_calls: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by: Mapped[str | None] = mapped_column(String(100))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


__all__ = [
    "PRD",
    "PRD_DRAFT",
    "PRD_REVIEWING",
    "PRD_ACCEPTED",
    "PRD_ARCHIVED",
    "PRD_STATUS_VALUES",
    "PRDStatus",
    "REQUIREMENT_ARCHIVED",
    "REQUIREMENT_APPROVED",
    "REQUIREMENT_DRAFT",
    "REQUIREMENT_REJECTED",
    "REQUIREMENT_REVIEWING",
    "REQUIREMENT_STATUS_VALUES",
    "RISK_CATEGORY_VALUES",
    "RISK_KNOWN",
    "RISK_SPECULATIVE",
    "Requirement",
    "RequirementStatus",
    "RiskCategory",
]
