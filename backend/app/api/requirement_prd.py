"""Phase 5 — Requirement + PRD HTTP routes.

Nine endpoints split across two sub-resources:

Requirement routes:

* ``POST /insights/{insight_id}/requirement`` — create one Requirement.
  Re-issue is rejected with 409 so PM must PATCH or archive.
* ``GET  /requirements/{requirement_id}`` — read.
* ``PATCH /requirements/{requirement_id}`` — edit content + status.
* ``GET  /projects/{project_id}/requirements`` — paginated list.

PRD routes:

* ``POST /requirements/{requirement_id}/prds`` — append a new version.
* ``GET  /prds/{prd_id}`` — read one PRD version.
* ``PATCH /prds/{prd_id}`` — transition status (only ``draft`` →
  ``reviewing`` / ``archived``).
* ``POST /prds/{prd_id}/accept`` — accept the PRD (idempotent).
* ``GET  /requirements/{requirement_id}/prds`` — paginated history.

Routes delegate business logic to
:mod:`app.services.requirement_prd` and translate exceptions into
HTTP status codes:

* :class:`RequirementNotFoundError` → 404
* :class:`RequirementValidationError` → 422 / 409 (duplicate)
* :class:`RequirementConfigError` → 503
* :class:`RequirementLLMConfigError` / :class:`RequirementLLMProtocolError`
  / :class:`RequirementLLMTransportError` → 503
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import Insight
from app.models.requirement_prd import (
    PRD,
    PRD_STATUS_VALUES,
    REQUIREMENT_STATUS_VALUES,
    Requirement,
)
from app.schemas.requirement_prd import (
    EvidenceReferenceRead,
    GroundingWarning,
    PRDAcceptRequest,
    PRDContentRead,
    PRDCreateRequest,
    PRDPage,
    PRDPatchRequest,
    PRDRead,
    RequirementCreateRequest,
    RequirementPage,
    RequirementPatchRequest,
    RequirementRead,
    RequirementStatusLiteral,
    RiskRead,
    SuccessMetricRead,
)
from app.services.requirement_prd import (
    PRDOutcome,
    RequirementConfigError,
    RequirementNotFoundError,
    RequirementOutcome,
    RequirementValidationError,
    accept_prd,
    create_prd_version,
    create_requirement,
    get_active_prd,
    get_prd,
    get_requirement,
    list_prds,
    list_requirements,
    patch_prd,
    patch_requirement,
)
from app.services.requirement_prd_client import (
    PRDLLMConfigError,
    PRDLLMProtocolError,
    PRDLLMTransportError,
    RequirementLLMConfigError,
    RequirementLLMProtocolError,
    RequirementLLMTransportError,
)

router = APIRouter(tags=["requirement_prd"])


_REQUIREMENT_STATUS_VALUES = tuple(REQUIREMENT_STATUS_VALUES)
_PRD_STATUS_VALUES = tuple(PRD_STATUS_VALUES)


# ---- Read DTOs --------------------------------------------------------------


def _requirement_to_read(row: Requirement) -> RequirementRead:
    return RequirementRead(
        id=row.id,
        project_id=row.project_id,
        insight_id=row.insight_id,
        decision_id=row.decision_id,
        score_id=row.score_id,
        insight_status_snapshot=row.insight_status_snapshot,
        decision_status_snapshot=row.decision_status_snapshot,
        ai_priority_snapshot=row.ai_priority_snapshot,
        pm_priority=row.pm_priority,
        pm_override_reason_snapshot=row.pm_override_reason_snapshot,
        source_snapshot_hash=row.source_snapshot_hash,
        name=row.name,
        problem=row.problem,
        target_user=row.target_user,
        user_story=row.user_story,
        solution_hypothesis=row.solution_hypothesis,
        success_metrics=[
            SuccessMetricRead(
                name=m["name"],
                type=m["type"],
                sources=m.get("sources", []),
                note=m.get("note", ""),
            )
            for m in (row.success_metrics or [])
        ],
        risks=[
            RiskRead(
                category=r["category"],
                description=r["description"],
                evidence_ids=r.get("evidence_ids", []),
                mitigation=r.get("mitigation", ""),
            )
            for r in (row.risks or [])
        ],
        evidence_references=[
            EvidenceReferenceRead(external_id=ref["external_id"], usage=ref["usage"])
            for ref in (row.evidence_references or [])
        ],
        status=row.status,
        model=row.model,
        prompt_version=row.prompt_version,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        unknown_token_calls=row.unknown_token_calls,
        latency_ms=row.latency_ms,
        attempt_count=row.attempt_count,
        retry_count=row.retry_count,
        next_version=row.next_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _prd_content_to_read(content: dict[str, Any]) -> PRDContentRead:
    return PRDContentRead.model_validate(content)


def _prd_to_read(row: PRD) -> PRDRead:
    return PRDRead(
        id=row.id,
        requirement_id=row.requirement_id,
        project_id=row.project_id,
        insight_id=row.insight_id,
        version=row.version,
        status=row.status,
        content=_prd_content_to_read(row.content),
        content_hash=row.content_hash,
        is_ai_generated_draft=row.is_ai_generated_draft,
        grounding_warnings=[
            GroundingWarning.model_validate(w) for w in (row.grounding_warnings or [])
        ],
        model=row.model,
        prompt_version=row.prompt_version,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        unknown_token_calls=row.unknown_token_calls,
        latency_ms=row.latency_ms,
        attempt_count=row.attempt_count,
        retry_count=row.retry_count,
        accepted_at=row.accepted_at,
        accepted_by=row.accepted_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ---- Exception mapping ------------------------------------------------------


def _resolve_validation_error(exc: RequirementValidationError) -> HTTPException:
    code = exc.code
    if code in {
        "insight_not_found",
        "requirement_not_found",
        "prd_not_found",
        "not_found",
    }:
        return HTTPException(404, str(exc))
    if code == "duplicate_requirement":
        return HTTPException(409, "已存在该 Insight 的 Requirement；请通过 PATCH 或归档后重试")
    if code in {
        "status_transition_invalid",
        "status_invalid",
        "prd_archived",
        "requirement_archived",
        "content_immutable",
        "decision_ineligible",
    }:
        return HTTPException(409, str(exc))
    return HTTPException(422, str(exc))


def _handle_requirement_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RequirementNotFoundError):
        return HTTPException(404, str(exc))
    if isinstance(exc, RequirementValidationError):
        return _resolve_validation_error(exc)
    if isinstance(exc, RequirementConfigError):
        return HTTPException(503, str(exc))
    if isinstance(exc, RequirementLLMConfigError):
        return HTTPException(503, str(exc))
    if isinstance(exc, RequirementLLMProtocolError):
        return HTTPException(503, str(exc))
    if isinstance(exc, RequirementLLMTransportError):
        return HTTPException(503, str(exc))
    if isinstance(exc, PRDLLMConfigError):
        return HTTPException(503, str(exc))
    if isinstance(exc, PRDLLMProtocolError):
        return HTTPException(503, str(exc))
    if isinstance(exc, PRDLLMTransportError):
        return HTTPException(503, str(exc))
    raise exc


# ---- Insight validation -----------------------------------------------------


def _require_insight(db: Session, insight_id: UUID) -> Insight:
    insight = db.get(Insight, insight_id)
    if insight is None:
        raise HTTPException(404, "洞察不存在")
    return insight


# ---- Routes -----------------------------------------------------------------


@router.post(
    "/insights/{insight_id}/requirement",
    response_model=RequirementRead,
    status_code=201,
)
def create_requirement_route(
    insight_id: UUID,
    body: RequirementCreateRequest = Body(default_factory=RequirementCreateRequest),
    db: Session = Depends(get_db),
) -> RequirementRead:
    _require_insight(db, insight_id)
    try:
        outcome: RequirementOutcome = create_requirement(db, insight_id, pm_content=body.content)
    except Exception as exc:
        db.rollback()
        raise _handle_requirement_error(exc) from exc
    db.commit()
    db.refresh(outcome.requirement)
    return _requirement_to_read(outcome.requirement)


@router.get(
    "/requirements/{requirement_id}",
    response_model=RequirementRead,
)
def get_requirement_route(
    requirement_id: UUID,
    db: Session = Depends(get_db),
) -> RequirementRead:
    try:
        row = get_requirement(db, requirement_id)
    except RequirementNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return _requirement_to_read(row)


@router.patch(
    "/requirements/{requirement_id}",
    response_model=RequirementRead,
)
def patch_requirement_route(
    requirement_id: UUID,
    body: RequirementPatchRequest = Body(...),
    db: Session = Depends(get_db),
) -> RequirementRead:
    if body.status is not None and body.status not in _REQUIREMENT_STATUS_VALUES:
        raise HTTPException(422, f"unknown status: {body.status}")
    try:
        outcome = patch_requirement(
            db,
            requirement_id,
            status=body.status,
            pm_content=body.content,
        )
    except RequirementNotFoundError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except RequirementValidationError as exc:
        db.rollback()
        raise _resolve_validation_error(exc) from exc
    db.commit()
    db.refresh(outcome.requirement)
    return _requirement_to_read(outcome.requirement)


@router.get(
    "/projects/{project_id}/requirements",
    response_model=RequirementPage,
)
def list_requirements_route(
    project_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    insight_id: UUID | None = Query(default=None),
    status: RequirementStatusLiteral | None = Query(default=None),
    db: Session = Depends(get_db),
) -> RequirementPage:
    total, rows = list_requirements(
        db,
        project_id,
        insight_id=insight_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return RequirementPage(
        total=total,
        items=[_requirement_to_read(r) for r in rows],
        limit=limit,
        offset=offset,
    )


@router.post(
    "/requirements/{requirement_id}/prds",
    response_model=PRDRead,
    status_code=201,
)
def create_prd_route(
    requirement_id: UUID,
    body: PRDCreateRequest = Body(default_factory=PRDCreateRequest),
    db: Session = Depends(get_db),
) -> PRDRead:
    try:
        outcome: PRDOutcome = create_prd_version(db, requirement_id, pm_content=body.content)
    except RequirementNotFoundError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except RequirementValidationError as exc:
        db.rollback()
        raise _resolve_validation_error(exc) from exc
    except RequirementConfigError as exc:
        db.rollback()
        raise HTTPException(503, str(exc)) from exc
    except (PRDLLMConfigError, PRDLLMProtocolError, PRDLLMTransportError) as exc:
        db.rollback()
        raise HTTPException(503, str(exc)) from exc
    except (
        RequirementLLMConfigError,
        RequirementLLMProtocolError,
        RequirementLLMTransportError,
    ) as exc:
        db.rollback()
        raise HTTPException(503, str(exc)) from exc
    db.commit()
    db.refresh(outcome.prd)
    return _prd_to_read(outcome.prd)


@router.get(
    "/prds/{prd_id}",
    response_model=PRDRead,
)
def get_prd_route(prd_id: UUID, db: Session = Depends(get_db)) -> PRDRead:
    try:
        row = get_prd(db, prd_id)
    except RequirementNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return _prd_to_read(row)


@router.patch(
    "/prds/{prd_id}",
    response_model=PRDRead,
)
def patch_prd_route(
    prd_id: UUID,
    body: PRDPatchRequest = Body(...),
    db: Session = Depends(get_db),
) -> PRDRead:
    if body.status is not None and body.status not in _PRD_STATUS_VALUES:
        raise HTTPException(422, f"unknown PRD status: {body.status}")
    try:
        row = patch_prd(db, prd_id, status=body.status, pm_content=body.content)
    except RequirementNotFoundError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except RequirementValidationError as exc:
        db.rollback()
        raise _resolve_validation_error(exc) from exc
    db.commit()
    db.refresh(row)
    return _prd_to_read(row)


@router.post(
    "/prds/{prd_id}/accept",
    response_model=PRDRead,
)
def accept_prd_route(
    prd_id: UUID,
    body: PRDAcceptRequest = Body(default_factory=PRDAcceptRequest),
    db: Session = Depends(get_db),
) -> PRDRead:
    # No user system → the audit row must NOT carry an identity
    # the client claimed. ``PRDAcceptRequest`` is empty by design
    # (extra=forbid) so any submitted key already returns 422 before
    # we get here. The service call therefore always passes
    # ``accepted_by=None``.
    try:
        row = accept_prd(db, prd_id)
    except RequirementNotFoundError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except RequirementValidationError as exc:
        db.rollback()
        raise _resolve_validation_error(exc) from exc
    db.commit()
    db.refresh(row)
    return _prd_to_read(row)


@router.get(
    "/requirements/{requirement_id}/prds",
    response_model=PRDPage,
)
def list_prds_route(
    requirement_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> PRDPage:
    try:
        get_requirement(db, requirement_id)
    except RequirementNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    total, rows = list_prds(db, requirement_id, limit=limit, offset=offset)
    items = [_prd_to_read(r) for r in rows]
    # Active pointer is a separate ``ORDER BY version DESC LIMIT 1``
    # lookup so it stays stable regardless of pagination. Computing it
    # from ``rows`` would silently fall back to whichever row happens
    # to be highest on the current page — when ``offset`` skips past
    # the newest PRD, the UI would think an older draft is active.
    active_row = get_active_prd(db, requirement_id)
    if active_row is not None:
        active_prd_id = active_row.id
        active_version = active_row.version
    else:
        active_prd_id = None
        active_version = None
    return PRDPage(
        total=total,
        items=items,
        limit=limit,
        offset=offset,
        active_prd_id=active_prd_id,
        active_version=active_version,
    )


__all__ = ["router"]
