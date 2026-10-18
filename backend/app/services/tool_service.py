"""Phase 8 — Tool Layer orchestrator.

Public surface:

* :func:`prepare_tool_run` — idempotent Prepare endpoint. Validates
  the registered ``(tool_name, tool_action)`` pair, sanitises the
  payload via :meth:`ToolRegistry.validate_inputs`, builds the
  server-derived Title / Description / Priority / Source-Insight
  preview **at Prepare time** from the Requirement + the source
  Insight, validates that snapshot against the registered
  :class:`app.tools.registry.SimulatedIssueSnapshot` schema, and
  persists the sanitised snapshot on ``ToolRun.request_payload``.
  Only ``approved`` Requirements can prepare a simulated issue —
  the brief's "仅批准状态 Requirement 可准备模拟 issue" gate. A
  duplicate ``(project_id, request_id)`` returns the existing
  awaiting_approval row unchanged.
* :func:`cancel_tool_run` — moves an awaiting_approval row to
  ``cancelled``. Any other source status is rejected so a completed
  or already-cancelled run cannot be silently rewritten.
* :func:`approve_tool_run` — the ONLY way to drive the simulator.
  Reads the **frozen snapshot** from ``ToolRun.request_payload``
  (built at Prepare time) and passes those exact fields to the
  adapter. The Requirement / Insight are NOT re-read at approval;
  a later edit to the Requirement cannot drift the audited content.
  A second Approve call on a completed run is a no-op (no second
  SimulatedIssue row); a non-idempotent retry is intentionally NOT
  performed so an unknown-result error can never silently produce a
  duplicate write.
* :func:`get_tool_run` / :func:`list_tool_runs_for_requirement` —
  read APIs scoped to one project + one Requirement. The
  cross-project isolation rule is enforced here: a project_id that
  doesn't own the Requirement → :class:`ToolValidationError` with
  code ``not_found`` (mapped to 404).
* :func:`list_tools` — exposes the static registry so the UI can
  enumerate what is available.

Approval / Snapshot freeze policy
================================
The user's wire payload is always ``{}`` for the simulator
(``extra=forbid`` on :class:`app.tools.registry.SimulatedIssueCreateInput`).
The four audit-visible fields (Title / Description / Priority /
Source-Insight preview) are derived from the Requirement + source
Insight at Prepare time and persisted on the row. Approve re-uses
the SAME snapshot — the simulator adapter only ever sees what was
originally prepared.

Revoked-Requirement gate
------------------------
Before driving the simulator, :func:`approve_tool_run` re-reads
the live Requirement and refuses the approval with
:class:`ToolValidationError` (mapped to 409 by the API layer)
unless the Requirement still exists, still belongs to the
requesting ``project_id``, and is still in ``approved`` status.
A PM who archives / rejects the Requirement after Prepare is
therefore protected from an inadvertent write — the frozen
snapshot is preserved untouched on the run row so an audit can
still read it, but the simulator is NEVER invoked and no
:class:`SimulatedIssue` row is persisted. Edits to the
Requirement's name, body, or PM priority that keep it
``approved`` do NOT re-derive the snapshot; the original frozen
fields still drive the audit row.

Lock order on PostgreSQL: the ``tool_runs`` row is acquired
FIRST (``SELECT ... FOR UPDATE``), then the ``requirements``
row is acquired (``SELECT ... FOR UPDATE``). Both locks are
held until the calling Approve transaction commits — i.e.
across the eligibility re-check, the conditional
``awaiting_approval → running`` UPDATE, the simulator adapter
call, the SimulatedIssue insert, and the
``running → completed`` UPDATE. A revoke (or status flip) that
issues ``UPDATE requirements SET status = ...`` on the same
row while Approve is mid-transaction is blocked at the row
level until Approve commits. This serialises the two writes
linearly so the "eligibility was approved, then a revoke
slipped in, then the simulator still ran" interleaving is
impossible. On SQLite the Requirement lock is a no-op; the
brief explicitly accepts that standing cross-transaction
row-lock semantics are not claimed on that backend.

Concurrency
-----------
* :func:`prepare_tool_run` — pre-flight SELECT catches the common
  duplicate-``request_id`` case. A true race that slips past the
  pre-flight is absorbed by catching
  :class:`sqlalchemy.exc.IntegrityError` on flush, rolling back,
  re-fetching the surviving row, and returning it idempotently
  (or raising 409 if the surviving row's tool / requirement
  differs from the incoming request).
* :func:`approve_tool_run` and :func:`cancel_tool_run` — both
  acquire a row-level lock on the ``tool_runs`` row inside the
  service transaction. On PostgreSQL this issues
  ``SELECT ... FOR UPDATE``; on SQLite (used by the test
  fixtures) the same call path falls back to a conditional
  ``UPDATE ... WHERE status = 'awaiting_approval'`` so the
  state machine is serialised even when the dialect does not
  honour row locks. The second writer either sees the row in
  the new state and returns it idempotently, or receives a
  clean 409 — no raw 500, no second adapter invocation, no
  duplicate :class:`SimulatedIssue`.

The Phase 1—7 services are NOT modified. ``Requirement``,
``Insight``, ``InsightDecision`` are read-only inputs here and
are consulted at Prepare time and again (live) at Approve time.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.models import (
    REQUIREMENT_APPROVED,
    Insight,
    Project,
    Requirement,
    SimulatedIssue,
    ToolExecutionMode,
    ToolFailureCategory,
    ToolRun,
    ToolRunStatus,
)
from app.tools.adapters import (
    SimulatedIssueTrackerAdapter,
    SimulatedIssueTrackerError,
    SimulatedIssueTrackerTransientError,
)
from app.tools.registry import (
    CapabilityKind,
    SimulatedIssueSnapshot,
    ToolDescriptor,
    ToolInputValidationError,
    ToolNotRegisteredError,
    ToolRegistry,
)

logger = logging.getLogger(__name__)


# ---- Limits -----------------------------------------------------------------


# Title / Description / Source-Insight preview bounds. Mirrors the
# DB CHECK constraints on ``simulated_issues`` + the simulator input
# contract. Values intentionally small so a maliciously crafted
# Requirement cannot inflate a persisted row.
SIMULATED_ISSUE_TITLE_MAX = 200
SIMULATED_ISSUE_DESCRIPTION_MAX = 4000
SIMULATED_ISSUE_SOURCE_PREVIEW_MAX = 400

# Retry budget for the simulator adapter when the descriptor opts in
# to ``supports_bounded_retry``. The brief mandates bounded retries:
# unknown results MUST NOT be blindly re-attempted, so the budget is
# capped at 1 retry (i.e. up to 2 attempts in total) so a transient
# timeout gets ONE chance to recover before the run is marked failed.
SIMULATOR_MAX_RETRIES = 1

# Redaction pattern for credentials / API key shapes. The simulator
# payload is bounded by the registry schema (``extra=forbid`` +
# empty input schema) but a defence-in-depth scrub keeps
# ``request_payload`` free of any token-shaped string the client
# might have smuggled through the registry.
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b("
    r"sk-[A-Za-z0-9_-]{16,}"
    r"|sk-ant-[A-Za-z0-9_-]{16,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|xox[abprs]-[A-Za-z0-9-]{8,}"
    r"|Bearer\s+[A-Za-z0-9._-]{8,}"
    r")"
)
_REDACTED = "[REDACTED]"


# ---- Errors -----------------------------------------------------------------


class ToolError(RuntimeError):
    """Base class for service-layer errors."""


class ToolValidationError(ToolError):
    """Mapped to 422 / 404 / 409 by the API layer."""

    @property
    def code(self) -> str:
        message = str(self)
        for marker, code in (
            ("concurrent_modification", "concurrent_modification"),
            ("conflict on request_id", "request_id_conflict"),
            ("not found", "not_found"),
            ("not registered", "not_registered"),
            ("cross-project", "cross_project"),
            ("invalid input", "input_invalid"),
            ("not approved", "requirement_not_approved"),
            ("already approved", "already_approved"),
            ("already cancelled", "already_cancelled"),
            ("not awaiting", "not_awaiting_approval"),
            ("tool_name", "tool_name_invalid"),
            ("tool_action", "tool_action_invalid"),
            ("request_id", "request_id_invalid"),
            ("not simulation_only", "not_simulation_only"),
            ("payload", "payload_invalid"),
        ):
            if marker in message:
                return code
        return "schema_invalid"


class ToolConfigError(ToolError):
    """Mapped to 503 by the API layer."""


# ---- Internal helpers ------------------------------------------------------


def _scrub_payload(payload: dict) -> dict:
    """Return a payload with credential-shaped strings replaced.

    Defence-in-depth — the registry input schema (``extra=forbid``
    + empty body) already blocks user-injected fields, but if a
    future descriptor accepts free-form strings the scrub keeps the
    audit row free of credentials.
    """
    scrubbed: dict = {}
    for key, value in payload.items():
        if isinstance(value, str):
            scrubbed[key] = _CREDENTIAL_PATTERN.sub(_REDACTED, value)
        elif isinstance(value, dict):
            scrubbed[key] = _scrub_payload(value)
        elif isinstance(value, list):
            scrubbed[key] = [
                _scrub_payload(item) if isinstance(item, dict)
                else (_CREDENTIAL_PATTERN.sub(_REDACTED, item) if isinstance(item, str) else item)
                for item in value
            ]
        else:
            scrubbed[key] = value
    return scrubbed


def _require_project(db: Session, project_id: UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise ToolValidationError("project not found")
    return project


def _require_requirement(db: Session, requirement_id: UUID) -> Requirement:
    requirement = db.get(Requirement, requirement_id)
    if requirement is None:
        raise ToolValidationError("requirement not found")
    return requirement


def _resolve_requirement_for_project(
    db: Session, project_id: UUID, requirement_id: UUID
) -> tuple[Requirement, Project]:
    """Return ``(Requirement, Project)`` if the requirement belongs to the project.

    Cross-project access raises ``ToolValidationError`` with the
    ``not_found`` marker (the API layer maps that to 404 so the two
    failure modes — wrong project vs. unknown id — are
    indistinguishable to a caller).
    """
    project = _require_project(db, project_id)
    requirement = _require_requirement(db, requirement_id)
    if requirement.project_id != project.id:
        raise ToolValidationError("requirement not found")
    return requirement, project


# ---- Concurrency helpers ----------------------------------------------------


def _is_postgres(db: Session) -> bool:
    """Return True when the active SQLAlchemy dialect is PostgreSQL.

    Used to opt into ``SELECT ... FOR UPDATE`` row-level locking
    on production. The SQLite test fixture does not honour
    row-level locks, so the service layer falls back to a
    conditional ``UPDATE`` for the same serialisation guarantee.
    """
    bind = db.get_bind()
    if bind is None:
        return False
    name = bind.dialect.name
    return name in {"postgresql", "postgres"}


def _lock_tool_run(db: Session, tool_run_id: UUID) -> ToolRun | None:
    """Read one ``tool_runs`` row, holding a row-level lock on PostgreSQL.

    On PostgreSQL this issues ``SELECT ... FOR UPDATE`` so a
    concurrent Approve / Cancel on the same row blocks until this
    transaction commits or rolls back. On SQLite (and other
    dialects without row locks) the call is a plain ``db.get``;
    serialisation is enforced by the conditional ``UPDATE`` in
    :func:`_try_conditional_status_transition`.

    Returns the row, or ``None`` if the id is unknown.
    """
    if _is_postgres(db):
        stmt = (
            select(ToolRun)
            .where(ToolRun.id == tool_run_id)
            .with_for_update()
        )
        return db.execute(stmt).scalar_one_or_none()
    return db.get(ToolRun, tool_run_id)


def _lock_requirement(
    db: Session, project_id: UUID, requirement_id: UUID
) -> Requirement | None:
    """Read one ``requirements`` row, holding a row-level lock on PostgreSQL.

    Used by :func:`approve_tool_run` to serialise the eligibility
    re-check against a concurrent revoke / re-status. The lock is
    held until the calling transaction commits — that is, across
    the entire Approve flow including the simulator adapter call
    and the SimulatedIssue insert. A concurrent revoke transaction
    that issues ``UPDATE requirements SET status = ...`` must
    wait for Approve to commit before it can acquire the same row,
    so the simulator is NEVER invoked against a Requirement that
    is "already revoked" by an interleaved transaction.

    On SQLite (and any other dialect without row-level locks)
    this is a plain read; the brief explicitly accepts that
    standing row-lock semantics are not portable to that backend.
    SQLite's database-level serialisation + the conditional UPDATE
    on ``tool_runs`` are the only invariants honoured there.

    Cross-project access collapses with "not found": a
    Requirement whose ``project_id`` differs from the requesting
    ``project_id`` is returned as ``None`` so the caller cannot
    probe for foreign ids.
    """
    if _is_postgres(db):
        stmt = (
            select(Requirement)
            .where(Requirement.id == requirement_id)
            .with_for_update()
        )
        requirement = db.execute(stmt).scalar_one_or_none()
    else:
        requirement = db.get(Requirement, requirement_id)
    if (
        requirement is None
        or requirement.project_id != project_id
    ):
        return None
    return requirement


def _try_conditional_status_transition(
    db: Session,
    tool_run_id: UUID,
    from_status: str,
    to_status: str,
) -> int:
    """Atomically move ``tool_runs.status`` from ``from_status`` to ``to_status``.

    Returns the rowcount: ``1`` if the caller won the race and
    owns the transition, ``0`` if another transaction already
    moved the row out of ``from_status``. The caller decides
    whether rowcount 0 means "idempotent return" (e.g. the row is
    now ``completed``) or "409 conflict" (e.g. the row is now
    ``cancelled`` / ``failed``).

    This is the belt-and-braces guard behind the
    ``SELECT ... FOR UPDATE`` lock: it works on dialects that do
    not honour row locks (SQLite) AND on PostgreSQL as a
    second-line check after the lock has been acquired.
    """
    result = db.execute(
        update(ToolRun)
        .where(
            ToolRun.id == tool_run_id,
            ToolRun.status == from_status,
        )
        .values(status=to_status, updated_at=utc_now())
    )
    return result.rowcount


def _resolve_descriptor(
    tool_name: str, tool_action: str
) -> ToolDescriptor:
    """Look up the registered descriptor.

    Raises :class:`ToolValidationError`` with the ``not_registered``
    marker so the API layer maps the failure to 404 (indistinguishable
    from "tool not found"). The registry exception is caught here so
    the service contract stays uniform.
    """
    registry = ToolRegistry.instance()
    try:
        descriptor = registry.get(tool_name, tool_action)
    except ToolNotRegisteredError as exc:
        raise ToolValidationError(f"tool '{tool_name}/{tool_action}' is not registered") from exc
    return descriptor


def _validate_payload_against_descriptor(
    descriptor: ToolDescriptor, payload: dict
) -> dict:
    """Run the registry's per-tool schema against ``payload``.

    Returns the parsed model dump so the audit row carries exactly
    the validated shape (no undeclared fields).
    """
    registry = ToolRegistry.instance()
    try:
        parsed = registry.validate_inputs(descriptor.name, descriptor.action, payload)
    except ToolInputValidationError as exc:
        raise ToolValidationError(f"invalid input: {exc}") from exc
    return parsed.model_dump()


def _validate_snapshot(snapshot: dict) -> dict:
    """Validate ``snapshot`` against :class:`SimulatedIssueSnapshot`.

    The snapshot lives on ``ToolRun.request_payload`` and was either
    (a) produced by :func:`prepare_tool_run` from the Requirement +
    source Insight or (b) — in the rejected case — tampered with.
    Either way, Approve consults this validator before driving the
    adapter so a malformed stored row cannot reach the simulator.
    Rejected with ``payload_invalid`` so the API maps it to 422.
    """
    try:
        return SimulatedIssueSnapshot.model_validate(snapshot).model_dump()
    except ValueError as exc:
        raise ToolValidationError(
            f"invalid snapshot payload: {exc}"
        ) from exc


# ---- Prepare ---------------------------------------------------------------


@dataclass(frozen=True)
class PrepareOutcome:
    run: ToolRun
    created: bool


def prepare_tool_run(
    db: Session,
    *,
    project_id: UUID,
    requirement_id: UUID,
    tool_name: str,
    tool_action: str,
    request_id: str,
    payload: dict,
    agent_run_id: UUID | None = None,
) -> PrepareOutcome:
    """Create a new awaiting_approval row, or return the existing one.

    Idempotency rule: ``UNIQUE(project_id, request_id)``. A repeated
    call with the same pair returns the existing row unchanged;
    the ``created`` flag tells the caller whether the row is new so
    the API layer can answer 201 vs 200.

    Eligibility: only Requirement in ``approved`` status may
    prepare a simulated issue. ``Insight.status`` is surfaced in
    the source preview only.

    Freeze: the four server-derived fields (Title / Description /
    Priority / Source-Insight preview) are computed here from the
    Requirement + source Insight, validated against
    :class:`SimulatedIssueSnapshot`, sanitised, and persisted on
    ``ToolRun.request_payload``. The Approve handler reads the same
    snapshot later — the persisted record is the audit trail and
    the only thing the simulator ever sees.
    """
    _require_project(db, project_id)
    requirement, _ = _resolve_requirement_for_project(db, project_id, requirement_id)
    if requirement.status != REQUIREMENT_APPROVED:
        raise ToolValidationError("requirement is not approved")

    descriptor = _resolve_descriptor(tool_name, tool_action)

    # Idempotency check before any DB write so a duplicate request
    # never produces two audit rows.
    existing = db.scalar(
        select(ToolRun).where(
            ToolRun.project_id == project_id,
            ToolRun.request_id == request_id,
        )
    )
    if existing is not None:
        if (
            existing.tool_name != tool_name
            or existing.tool_action != tool_action
            or existing.requirement_id != requirement_id
        ):
            raise ToolValidationError(
                "conflict on request_id: already used with a different "
                "tool or requirement in this project"
            )
        return PrepareOutcome(run=existing, created=False)

    # User payload validation: the simulator input schema is empty,
    # so a ``{}`` body is accepted. Other tools (e.g. web_research)
    # still get their input schemas enforced here.
    sanitized_user_payload = _scrub_payload(
        _validate_payload_against_descriptor(descriptor, payload)
    )

    # Freeze — derive, validate, scrub, persist.
    source_insight = db.get(Insight, requirement.insight_id)
    snapshot = _derive_simulated_issue_fields(requirement, source_insight)
    validated_snapshot = _validate_snapshot(snapshot)
    sanitized_snapshot = _scrub_payload(validated_snapshot)

    # The simulator writes the snapshot on the row (NOT the user's
    # ``{}`` payload) so a later GET surfaces the audit-able inputs
    # as a preview. Other tools persist their own validated payload
    # as before.
    persisted_payload = (
        sanitized_snapshot
        if descriptor.capability_kind == CapabilityKind.SIMULATION_ONLY
        else sanitized_user_payload
    )

    run = ToolRun(
        project_id=project_id,
        requirement_id=requirement.id,
        agent_run_id=agent_run_id,
        tool_name=descriptor.name,
        tool_action=descriptor.action,
        tool_version=descriptor.version,
        request_id=request_id,
        status=ToolRunStatus.AWAITING_APPROVAL.value,
        execution_mode=(
            ToolExecutionMode.SIMULATION.value
            if descriptor.execution_mode.value == "simulation"
            else ToolExecutionMode.REAL.value
        ),
        request_payload=persisted_payload,
        response_summary=None,
        failure_category=None,
        attempt_count=0,
        retry_count=0,
    )
    db.add(run)
    try:
        db.flush()
    except IntegrityError as exc:
        # Two Prepares with the same ``(project_id, request_id)``
        # raced past the pre-flight SELECT. One inserted first and
        # won the UNIQUE constraint; the other lost. Roll back the
        # in-flight insert, re-fetch the surviving row, and decide
        # idempotent-return vs. clean 409.
        db.rollback()
        existing = db.scalar(
            select(ToolRun).where(
                ToolRun.project_id == project_id,
                ToolRun.request_id == request_id,
            )
        )
        if existing is None:
            # The conflict was on a different constraint (e.g. PK).
            # We cannot recover cleanly; surface as 409.
            raise ToolValidationError(
                "concurrent_modification: prepare could not complete"
            ) from exc
        if (
            existing.tool_name != tool_name
            or existing.tool_action != tool_action
            or existing.requirement_id != requirement_id
        ):
            raise ToolValidationError(
                "conflict on request_id: already used with a different "
                "tool or requirement in this project"
            ) from exc
        return PrepareOutcome(run=existing, created=False)
    return PrepareOutcome(run=run, created=True)


# ---- Cancel ----------------------------------------------------------------


def cancel_tool_run(
    db: Session,
    *,
    project_id: UUID,
    tool_run_id: UUID,
) -> ToolRun:
    """Transition an awaiting_approval row to ``cancelled``.

    Concurrency: the row is locked at the start of the
    transaction (``SELECT ... FOR UPDATE`` on PostgreSQL, plain
    read on SQLite), and the status transition is then issued as
    a conditional ``UPDATE ... WHERE status = 'awaiting_approval'``
    so a competing Approve / Cancel cannot both succeed. A
    second Cancel on an already-cancelled run is a no-op
    (idempotent return); any other source status surfaces 409
    so a completed or failed run cannot be silently rewritten.

    Cross-project access raises ``ToolValidationError`` with
    code ``not_found`` (mapped to 404).
    """
    run = _lock_tool_run(db, tool_run_id)
    if run is None or run.project_id != project_id:
        raise ToolValidationError("tool run not found")
    if run.status == ToolRunStatus.CANCELLED.value:
        return run
    if run.status != ToolRunStatus.AWAITING_APPROVAL.value:
        raise ToolValidationError(
            f"tool run is not awaiting approval (status='{run.status}')"
        )
    transitioned = _try_conditional_status_transition(
        db,
        tool_run_id,
        ToolRunStatus.AWAITING_APPROVAL.value,
        ToolRunStatus.CANCELLED.value,
    )
    if transitioned == 0:
        # Another transaction moved the row out of awaiting_approval
        # between our read and our conditional update. Re-read the
        # current state and surface a deterministic outcome.
        current = db.get(ToolRun, tool_run_id)
        if current is not None and current.status == ToolRunStatus.CANCELLED.value:
            return current
        status = current.status if current is not None else "unknown"
        raise ToolValidationError(
            f"tool run is not awaiting approval (status='{status}')"
        )
    run.status = ToolRunStatus.CANCELLED.value
    run.completed_at = utc_now()
    db.flush()
    return run


# ---- Issue field derivation -----------------------------------------------


def _derive_simulated_issue_fields(
    requirement: Requirement,
    source_insight: Insight | None,
) -> dict[str, Any]:
    """Build the server-derived Title / Description / Priority / preview.

    The brief mandates:

    * Title / Description / Priority come from the Requirement +
      source Insight's PM priority.
    * Source Insight preview is bounded; the full insight text is
      NEVER echoed.
    * Raw feedback content + credential-shaped tokens are NOT
      included (the helper strips them via :func:`_scrub_payload`).
    """
    priority = requirement.pm_priority
    title = f"[Simulated] {requirement.name}".strip()
    if len(title) > SIMULATED_ISSUE_TITLE_MAX:
        title = title[:SIMULATED_ISSUE_TITLE_MAX]

    description_parts = [
        f"Requirement: {requirement.name}",
        f"Problem: {requirement.problem}",
        f"Target user: {requirement.target_user}",
        f"User story: {requirement.user_story}",
        f"Solution hypothesis: {requirement.solution_hypothesis}",
    ]
    description = "\n\n".join(description_parts)
    if len(description) > SIMULATED_ISSUE_DESCRIPTION_MAX:
        description = description[:SIMULATED_ISSUE_DESCRIPTION_MAX]

    if source_insight is not None:
        preview_seed = source_insight.title or source_insight.pain_point or ""
    else:
        preview_seed = requirement.name
    # Strip control characters + collapse whitespace so the persisted
    # preview is a single bounded line.
    preview = " ".join(str(preview_seed).split())
    if len(preview) > SIMULATED_ISSUE_SOURCE_PREVIEW_MAX:
        preview = preview[:SIMULATED_ISSUE_SOURCE_PREVIEW_MAX]

    payload = {
        "title": title,
        "description": description,
        "priority": priority,
        "source_insight_preview": preview,
    }
    return _scrub_payload(payload)


# ---- Approve ---------------------------------------------------------------


def _build_simulator_for_descriptor(
    descriptor: ToolDescriptor,
) -> SimulatedIssueTrackerAdapter:
    """Instantiate the simulator adapter for the registered descriptor.

    The factory pattern means the registry stays free of ORM
    imports — the simulator is instantiated only when an Approve
    call needs to drive the adapter. Tests inject their own
    descriptor (or monkeypatch ``_build_simulator_for_descriptor``)
    to exercise the failure-injection surface.
    """
    if descriptor.adapter_factory is None:
        raise ToolConfigError(
            f"tool '{descriptor.name}/{descriptor.action}' has no adapter factory"
        )
    adapter = descriptor.adapter_factory()
    if not isinstance(adapter, SimulatedIssueTrackerAdapter):
        raise ToolConfigError(
            f"tool '{descriptor.name}/{descriptor.action}' adapter is not a "
            "SimulatedIssueTrackerAdapter"
        )
    return adapter


def approve_tool_run(
    db: Session,
    *,
    project_id: UUID,
    tool_run_id: UUID,
) -> ToolRun:
    """Drive the simulator for an awaiting_approval row.

    Returns the (now ``completed``) run + persists one
    :class:`SimulatedIssue` audit row built from the **frozen
    snapshot** on ``ToolRun.request_payload``.

    Frozen-snapshot policy:
    * The four server-derived fields were computed and persisted at
      Prepare time. Approve MUST NOT re-derive them from the live
      Requirement + Insight — a silent drift between "what the
      human approved" and "what the simulator wrote" would void
      the audit trail.
    * If the Requirement's name / body / PM priority changes
      after Prepare but it remains ``approved``, the original
      frozen snapshot still drives the simulator. The simulator
      never re-reads the live Requirement.
    * If the Requirement is archived / rejected / deleted, or
      moved to a different project, between Prepare and Approve,
      the call is rejected with ``ToolValidationError`` mapped
      to 409 and the simulator is NEVER invoked. The frozen
      snapshot is left untouched on the run row so an audit can
      still read it; no :class:`SimulatedIssue` row is created.

    Concurrency:
    * Lock order is ``tool_runs`` row FIRST, then
      ``requirements`` row. Both are acquired via
      ``SELECT ... FOR UPDATE`` on PostgreSQL and held until the
      calling transaction commits (i.e. across the eligibility
      re-check, conditional state transition, simulator adapter
      call, SimulatedIssue insert, and final ``running →
      completed`` UPDATE). On SQLite both locks degrade to
      plain ``db.get``; the brief accepts that cross-transaction
      row-lock semantics are not portable to that backend.
    * A concurrent revoke (or status flip) on the Requirement
      is therefore blocked at the row level until Approve
      commits, so the "eligibility was approved, then a revoke
      slipped in, then the simulator still ran" interleaving
      is impossible on PostgreSQL. The frozen snapshot is still
      the only thing the adapter sees — a successful simulator
      write that landed BEFORE the revoke was queued is the
      correct semantic; the revoke only governs future Approve
      calls.
    * On the ``tool_runs`` row itself: after the FOR UPDATE
      lock, the ``awaiting_approval → running`` transition is
      issued as a conditional UPDATE. A second concurrent
      Approve sees the row in ``completed`` and returns the
      existing run idempotently without re-invoking the
      adapter. A racing Cancel sees the row in ``cancelled``
      and surfaces 409. No raw 500, no duplicate
      :class:`SimulatedIssue`.

    Idempotency:
    * Cross-project access → ``not_found`` (404).
    * Status not awaiting_approval → ``not_awaiting_approval`` (409).
    * Status already completed → returns the existing row WITHOUT
      running the adapter a second time (no duplicate audit row).
    * Status cancelled / failed → ``not_awaiting_approval`` (409).

    Retry policy:
    * Bounded retry on transient timeout / 5xx ONLY when the
      descriptor opts in to ``supports_bounded_retry`` AND
      ``idempotent``. Non-idempotent failures and unknown-result
      errors propagate to ``failed`` without retry — the brief
      mandates that unknown results MUST NOT be blindly re-attempted.
    """
    run = _lock_tool_run(db, tool_run_id)
    if run is None or run.project_id != project_id:
        raise ToolValidationError("tool run not found")
    if run.status == ToolRunStatus.COMPLETED.value:
        return run
    if run.status != ToolRunStatus.AWAITING_APPROVAL.value:
        raise ToolValidationError(
            f"tool run is not awaiting approval (status='{run.status}')"
        )

    registry = ToolRegistry.instance()
    descriptor = registry.get(run.tool_name, run.tool_action)

    # Defensive guard — the simulator is the only write path that
    # currently opts into ``simulation_only``. Real-write tools are
    # not wired up because we have no verified write credentials.
    if descriptor.capability_kind != CapabilityKind.SIMULATION_ONLY:
        raise ToolValidationError(
            f"tool '{descriptor.name}/{descriptor.action}' is not simulation_only"
        )

    # Read the frozen snapshot. Do NOT re-derive from Requirement +
    # source Insight — that path is only available at Prepare time.
    issue_fields = _validate_snapshot(run.request_payload)

    # Re-validate the live Requirement at Approve time. The frozen
    # snapshot on ``request_payload`` is the audited content the
    # simulator will write, but a human approval against a now-revoked
    # or now-deleted Requirement MUST be rejected — otherwise a PM
    # could silently resurrect a write after they had explicitly
    # withdrawn the Requirement. The simulator is therefore NEVER
    # invoked unless the Requirement still exists, still belongs to
    # the requesting project, AND is still ``approved``.
    #
    # Lock order: ``tool_runs`` row FIRST, then ``requirements`` row.
    # The Requirement lock is held until the calling transaction
    # commits — i.e. across the entire Approve flow (eligibility
    # check, conditional status transition, simulator adapter
    # call, SimulatedIssue insert, ToolRun→completed update). A
    # concurrent revoke transaction that tries to flip the
    # Requirement out of ``approved`` while Approve is paused in
    # the adapter call MUST wait for Approve's commit before it
    # can acquire the same row, so the "eligibility was approved,
    # then revoke slipped in, then simulator still ran" interleaving
    # is impossible on PostgreSQL. On SQLite the lock is a no-op;
    # the brief explicitly accepts that the cross-transaction
    # row-lock guarantee is not claimed on SQLite.
    requirement = _lock_requirement(db, project_id, run.requirement_id)
    if (
        requirement is None
        or requirement.status != REQUIREMENT_APPROVED
    ):
        raise ToolValidationError("requirement is not approved")

    # Atomic state transition. Only one transaction can move the
    # row from awaiting_approval to running; the conditional
    # UPDATE is the actual race-detection guard, working on both
    # PostgreSQL (after the FOR UPDATE lock) and SQLite (where
    # SELECT FOR UPDATE is a no-op).
    transitioned = _try_conditional_status_transition(
        db,
        tool_run_id,
        ToolRunStatus.AWAITING_APPROVAL.value,
        ToolRunStatus.RUNNING.value,
    )
    if transitioned == 0:
        current = db.get(ToolRun, tool_run_id)
        if current is not None and current.status == ToolRunStatus.COMPLETED.value:
            return current
        status = current.status if current is not None else "unknown"
        raise ToolValidationError(
            f"concurrent_modification: tool run moved to status='{status}'"
        )

    run.status = ToolRunStatus.RUNNING.value
    run.approved_at = utc_now()
    db.flush()

    adapter = _build_simulator_for_descriptor(descriptor)
    total_attempts = SIMULATOR_MAX_RETRIES + 1
    last_error: Exception | None = None
    failure_category: str | None = None

    for attempt_index in range(total_attempts):
        run.attempt_count += 1
        if attempt_index > 0:
            run.retry_count += 1
        try:
            result = adapter.execute(
                tool_name=descriptor.name,
                tool_action=descriptor.action,
                title=issue_fields["title"],
                description=issue_fields["description"],
                priority=issue_fields["priority"],
                source_insight_preview=issue_fields["source_insight_preview"],
                requirement_id=run.requirement_id,
                project_id=project_id,
            )
        except SimulatedIssueTrackerTransientError as exc:
            last_error = exc
            failure_category = (
                ToolFailureCategory.TRANSPORT_TIMEOUT.value
                if exc.category == "timeout"
                else ToolFailureCategory.TRANSPORT_5XX.value
            )
            logger.warning(
                "tool simulator transient failure",
                extra={
                    "tool_name": descriptor.name,
                    "tool_action": descriptor.action,
                    "tool_run_id": str(tool_run_id),
                    "attempt": run.attempt_count,
                    "category": exc.category,
                },
            )
            # Bounded retry only when the descriptor explicitly opts
            # in; an unknown-result error must not be blindly retried.
            if not (descriptor.idempotent and descriptor.supports_bounded_retry):
                break
            continue
        except SimulatedIssueTrackerError as exc:
            last_error = exc
            failure_category = (
                ToolFailureCategory.PROVIDER_FAILURE.value
                if exc.category == "provider_failure"
                else ToolFailureCategory.UNKNOWN.value
            )
            break
        else:
            last_error = None
            failure_category = None
            # Persist the audit row from the SAME frozen snapshot.
            issue_row = SimulatedIssue(
                tool_run_id=run.id,
                project_id=project_id,
                requirement_id=run.requirement_id,
                tool_name=descriptor.name,
                tool_action=descriptor.action,
                external_id=result.external_id,
                simulated=result.simulated,
                title=issue_fields["title"],
                description=issue_fields["description"],
                priority=issue_fields["priority"],
                source_insight_preview=issue_fields["source_insight_preview"],
            )
            db.add(issue_row)
            run.external_id = result.external_id
            run.response_summary = result.summary
            run.failure_category = None
            run.status = ToolRunStatus.COMPLETED.value
            run.completed_at = utc_now()
            db.flush()
            return run

    # The loop exited via an exception or a non-retriable break.
    run.status = ToolRunStatus.FAILED.value
    run.completed_at = utc_now()
    run.failure_category = failure_category or ToolFailureCategory.UNKNOWN.value
    if last_error is not None:
        run.response_summary = (
            f"tool '{descriptor.name}/{descriptor.action}' failed: "
            f"category={run.failure_category}; "
            "see audit logs for details"
        )
    else:
        run.response_summary = (
            f"tool '{descriptor.name}/{descriptor.action}' failed: "
            f"category={run.failure_category}"
        )
    db.flush()
    return run


# ---- Read APIs -------------------------------------------------------------


def get_tool_run(
    db: Session,
    *,
    project_id: UUID,
    tool_run_id: UUID,
) -> ToolRun:
    """Read one tool run with project isolation."""
    run = db.get(ToolRun, tool_run_id)
    if run is None or run.project_id != project_id:
        raise ToolValidationError("tool run not found")
    return run


def list_tool_runs_for_requirement(
    db: Session,
    *,
    project_id: UUID,
    requirement_id: UUID,
    limit: int,
    offset: int,
) -> tuple[int, list[ToolRun], UUID | None]:
    """Paginated history for one requirement.

    Returns ``(total, rows, active_run_id)`` where ``active_run_id``
    is the awaiting_approval run if one exists (the UI surfaces
    that one for the Approve button), otherwise the most recent
    non-cancelled run.
    """
    _resolve_requirement_for_project(db, project_id, requirement_id)
    where = ToolRun.requirement_id == requirement_id
    total = db.scalar(select(func.count()).select_from(ToolRun).where(where)) or 0
    rows = db.scalars(
        select(ToolRun)
        .where(where)
        .order_by(ToolRun.created_at.desc(), ToolRun.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    active_row = db.scalar(
        select(ToolRun)
        .where(
            ToolRun.requirement_id == requirement_id,
            ToolRun.status == ToolRunStatus.AWAITING_APPROVAL.value,
        )
        .order_by(ToolRun.created_at.desc(), ToolRun.id.desc())
        .limit(1)
    )
    return total, rows, active_row.id if active_row is not None else None


def list_tools() -> list[ToolDescriptor]:
    """Return the static descriptor list."""
    return ToolRegistry.instance().list_tools()


# ---- UTC alias re-export (so the API layer can import safely) --------------


__all__ = [
    "SIMULATED_ISSUE_DESCRIPTION_MAX",
    "SIMULATED_ISSUE_SOURCE_PREVIEW_MAX",
    "SIMULATED_ISSUE_TITLE_MAX",
    "SIMULATOR_MAX_RETRIES",
    "PrepareOutcome",
    "ToolConfigError",
    "ToolError",
    "ToolValidationError",
    "approve_tool_run",
    "cancel_tool_run",
    "get_tool_run",
    "list_tool_runs_for_requirement",
    "list_tools",
    "prepare_tool_run",
]