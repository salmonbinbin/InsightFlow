"""Phase 8 — Tool Layer models.

Two tables back the Tool Registry + Tool Run audit + Human Approval
framework:

* :class:`ToolRun` — one row per attempted tool invocation. ``request_id``
  is the client-supplied (or server-generated) idempotency key;
  ``UNIQUE(project_id, request_id)`` makes the Prepare API idempotent
  within one project. ``status`` spans the six lifecycle states
  (pending / awaiting_approval / running / completed / failed /
  cancelled) so a row always reflects the most recent transition.
  ``execution_mode`` records whether the tool ran as a simulator or
  against a real provider; ``external_id`` is the bounded identifier
  the adapter returned (``SIM-...`` for the Phase 8 simulator).

* :class:`SimulatedIssue` — one row per approved simulator write.
  Persists the server-derived Title / Description / Priority /
  Source-Insight preview plus the ``SIM-...`` external id so an audit
  reader can confirm what the simulator actually saw. ``UNIQUE
  (external_id)`` rejects duplicate simulator writes even if the
  caller races against the Approve endpoint.

The brief mandates a "需要可持久化模拟目标表". The persisted simulator
target row is the evidence an audit can read to confirm zero
unauthorised writes were ever produced — every approved simulator
call leaves exactly one ``SimulatedIssue`` row behind, and the
caller can match the run id ↔ simulator row in one join.

The Phase 1—7 tables and the Raw feedback / analysis / cluster /
insight / decision / requirement / PRD / research tables are NEVER
modified here. ``down_revision`` is ``0012``.
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

# ---- Enumerations -----------------------------------------------------------


class ToolRunStatus(str, Enum):
    PENDING = "pending"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TOOL_RUN_STATUSES: tuple[str, ...] = tuple(s.value for s in ToolRunStatus)


class ToolExecutionMode(str, Enum):
    SIMULATION = "simulation"
    REAL = "real"


TOOL_EXECUTION_MODES: tuple[str, ...] = tuple(m.value for m in ToolExecutionMode)


class ToolFailureCategory(str, Enum):
    TRANSPORT_TIMEOUT = "transport_timeout"
    TRANSPORT_5XX = "transport_5xx"
    TRANSPORT_4XX = "transport_4xx"
    VALIDATION = "validation"
    NOT_REGISTERED = "not_registered"
    UNAUTHORISED = "unauthorised"
    PROVIDER_FAILURE = "provider_failure"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INELIGIBLE = "ineligible"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


TOOL_FAILURE_CATEGORIES: tuple[str, ...] = tuple(
    c.value for c in ToolFailureCategory
)


# ---- Tool Run --------------------------------------------------------------


class ToolRun(Base):
    """One attempted tool invocation.

    ``request_id`` is a UUID-shaped string and the row is unique on
    ``(project_id, request_id)`` so the Prepare endpoint is
    idempotent within one project. ``agent_run_id`` is nullable —
    Phase 7 has no agent runner that drives the tool layer, so most
    rows carry ``None`` until a real agent runner is wired in.

    ``request_payload`` is the SANITIZED input (the adapter can
    re-execute from it without re-reading the registry). The raw
    request bytes / any credential-shaped token / raw feedback body
    are NEVER echoed here. ``response_summary`` is a bounded summary
    line the adapter returned; it is NOT a free-form echo of the
    adapter output.

    ``attempt_count`` / ``retry_count`` follow the Phase 5 / 8
    convention: ``attempt_count`` is the total number of adapter
    invocations, ``retry_count`` is ``attempt_count - 1`` when the
    run completed (the success path never counted as a retry).
    """

    __tablename__ = "tool_runs"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "request_id",
            name="uq_tool_runs_project_request_id",
        ),
        CheckConstraint(
            f"status IN ({','.join(repr(s) for s in TOOL_RUN_STATUSES)})",
            name="ck_tool_runs_status_allowed",
        ),
        CheckConstraint(
            f"execution_mode IN ({','.join(repr(m) for m in TOOL_EXECUTION_MODES)})",
            name="ck_tool_runs_execution_mode_allowed",
        ),
        CheckConstraint(
            f"failure_category IS NULL OR failure_category IN "
            f"({','.join(repr(c) for c in TOOL_FAILURE_CATEGORIES)})",
            name="ck_tool_runs_failure_category_allowed",
        ),
        CheckConstraint(
            "length(tool_name) BETWEEN 1 AND 64",
            name="ck_tool_runs_tool_name_length",
        ),
        CheckConstraint(
            "length(tool_action) BETWEEN 1 AND 64",
            name="ck_tool_runs_tool_action_length",
        ),
        CheckConstraint(
            "length(tool_version) BETWEEN 1 AND 64",
            name="ck_tool_runs_tool_version_length",
        ),
        CheckConstraint(
            "length(request_id) BETWEEN 1 AND 64",
            name="ck_tool_runs_request_id_length",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND retry_count >= 0",
            name="ck_tool_runs_attempt_metrics_non_negative",
        ),
        CheckConstraint(
            "retry_count <= attempt_count",
            name="ck_tool_runs_retry_le_attempt",
        ),
        CheckConstraint(
            "(external_id IS NULL) OR (length(external_id) BETWEEN 1 AND 64)",
            name="ck_tool_runs_external_id_length",
        ),
        Index("ix_tool_runs_project_id", "project_id"),
        Index("ix_tool_runs_requirement_id", "requirement_id"),
        Index("ix_tool_runs_status", "status"),
        Index("ix_tool_runs_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    requirement_id: Mapped[UUID] = mapped_column(ForeignKey("requirements.id"))
    agent_run_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)

    tool_name: Mapped[str] = mapped_column(String(64))
    tool_action: Mapped[str] = mapped_column(String(64))
    tool_version: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(
        String(20), default=ToolRunStatus.PENDING.value
    )
    execution_mode: Mapped[str] = mapped_column(
        String(20), default=ToolExecutionMode.SIMULATION.value
    )
    external_id: Mapped[str | None] = mapped_column(String(64))

    request_payload: Mapped[dict] = mapped_column(JSON)
    response_summary: Mapped[str | None] = mapped_column(Text)
    failure_category: Mapped[str | None] = mapped_column(String(32), nullable=True)

    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# ---- Simulated Issue --------------------------------------------------------


class SimulatedIssue(Base):
    """One approved simulator write.

    Persists the server-derived Title / Description / Priority /
    Source-Insight preview so an audit can read exactly what the
    simulator saw. ``UNIQUE(external_id)`` rejects duplicate simulator
    writes even if two Approve calls race against the same row.
    ``tool_run_id`` is the FK back to the originating ToolRun.

    No raw feedback content, no credential-shaped token, and no
    user-injected issue body is allowed on the row — the Title /
    Description fields are server-derived from the Requirement +
    source Insight and bounded by CHECK constraints.
    """

    __tablename__ = "simulated_issues"
    __table_args__ = (
        UniqueConstraint(
            "external_id",
            name="uq_simulated_issues_external_id",
        ),
        UniqueConstraint(
            "tool_run_id",
            name="uq_simulated_issues_tool_run_id",
        ),
        CheckConstraint(
            "length(external_id) BETWEEN 1 AND 64",
            name="ck_simulated_issues_external_id_length",
        ),
        CheckConstraint(
            "length(title) BETWEEN 1 AND 200",
            name="ck_simulated_issues_title_length",
        ),
        CheckConstraint(
            "length(description) BETWEEN 1 AND 4000",
            name="ck_simulated_issues_description_length",
        ),
        CheckConstraint(
            "length(source_insight_preview) <= 400",
            name="ck_simulated_issues_source_preview_length",
        ),
        CheckConstraint(
            "priority IS NULL OR priority IN ('P0','P1','P2')",
            name="ck_simulated_issues_priority_allowed",
        ),
        Index("ix_simulated_issues_project_id", "project_id"),
        Index("ix_simulated_issues_requirement_id", "requirement_id"),
        Index("ix_simulated_issues_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tool_run_id: Mapped[UUID] = mapped_column(ForeignKey("tool_runs.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    requirement_id: Mapped[UUID] = mapped_column(ForeignKey("requirements.id"))

    tool_name: Mapped[str] = mapped_column(String(64))
    tool_action: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str] = mapped_column(String(64))
    simulated: Mapped[bool] = mapped_column(default=True)

    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    priority: Mapped[str | None] = mapped_column(String(4))
    source_insight_preview: Mapped[str] = mapped_column(String(400))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


__all__ = [
    "TOOL_EXECUTION_MODES",
    "TOOL_FAILURE_CATEGORIES",
    "TOOL_RUN_STATUSES",
    "SimulatedIssue",
    "ToolExecutionMode",
    "ToolFailureCategory",
    "ToolRun",
    "ToolRunStatus",
]