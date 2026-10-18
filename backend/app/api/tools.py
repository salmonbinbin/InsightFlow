"""Phase 8 — Tool Layer HTTP routes.

Endpoints:

* ``GET  /tools`` — list the static registry (metadata only).
* ``POST /requirements/{requirement_id}/tool-runs`` — Prepare.
  Idempotent on ``(project_id, request_id)``; first call returns
  201, repeat returns 200 with the existing awaiting_approval row.
  Cross-project access returns 404 so a caller cannot probe
  foreign projects.
* ``GET  /requirements/{requirement_id}/tool-runs`` — paginated
  history + ``active_run_id`` pointer.
* ``GET  /tool-runs/{tool_run_id}?project_id=...`` — read one row
  with project-isolation enforced via the ``project_id`` query
  parameter (REQUIRED; wrong value → 404).
* ``POST /tool-runs/{tool_run_id}/cancel?project_id=...`` —
  transition awaiting_approval → cancelled.
* ``POST /tool-runs/{tool_run_id}/approve?project_id=...`` — drive
  the simulator. Returns the completed row + the persisted
  SimulatedIssue external id (``SIM-...``) is exposed via
  ``ToolRunRead.external_id``.

Errors:
``404`` unknown / cross-project, ``409`` state-machine violation,
``422`` schema / unknown tool name, ``503`` adapter config issue.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import ToolRun
from app.schemas.tool import (
    ToolApproveRequest,
    ToolDescriptorRead,
    ToolPrepareRequest,
    ToolRegistryRead,
    ToolRunPage,
    ToolRunRead,
)
from app.services.tool_service import (
    ToolConfigError,
    ToolValidationError,
    approve_tool_run,
    cancel_tool_run,
    get_tool_run,
    list_tool_runs_for_requirement,
    list_tools,
    prepare_tool_run,
)

router = APIRouter(tags=["tools"])


# ---- Exception mapping -----------------------------------------------------


def _resolve_validation_error(exc: ToolValidationError) -> HTTPException:
    code = exc.code
    if code in {"not_found", "not_registered"}:
        return HTTPException(404, str(exc))
    if code in {
        "requirement_not_approved",
        "already_approved",
        "already_cancelled",
        "not_awaiting_approval",
        "cross_project",
        "not_simulation_only",
        "request_id_conflict",
        "concurrent_modification",
    }:
        return HTTPException(409, str(exc))
    return HTTPException(422, str(exc))


def _handle_tool_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ToolValidationError):
        return _resolve_validation_error(exc)
    if isinstance(exc, ToolConfigError):
        return HTTPException(503, str(exc))
    raise exc


# ---- Read DTO --------------------------------------------------------------


def _descriptor_to_read(descriptor) -> ToolDescriptorRead:
    return ToolDescriptorRead(
        name=descriptor.name,
        action=descriptor.action,
        version=descriptor.version,
        description=descriptor.description,
        capability_kind=descriptor.capability_kind.value,
        execution_mode=descriptor.execution_mode.value,
        idempotent=descriptor.idempotent,
        supports_bounded_retry=descriptor.supports_bounded_retry,
    )


def _tool_run_to_read(run: ToolRun) -> ToolRunRead:
    return ToolRunRead.model_validate(run)


# ---- Routes ----------------------------------------------------------------


@router.get("/tools", response_model=ToolRegistryRead)
def list_tools_route() -> ToolRegistryRead:
    """Return the static registry as a wire DTO.

    The endpoint is intentionally read-only — registration is a
    process-boot concern, not a runtime mutation.
    """
    return ToolRegistryRead(
        tools=[_descriptor_to_read(d) for d in list_tools()]
    )


@router.post(
    "/requirements/{requirement_id}/tool-runs",
    response_model=ToolRunRead,
)
def prepare_tool_run_route(
    requirement_id: UUID,
    project_id: UUID = Query(...),
    body: ToolPrepareRequest = Body(...),
    db: Session = Depends(get_db),
) -> ToolRunRead:
    """Prepare a new awaiting_approval tool run for one Requirement.

    The project_id query parameter is REQUIRED and must match the
    Requirement's owning project. ``status_code`` is 201 on the
    first call and 200 on a duplicate (same
    ``project_id`` + ``request_id`` pair) so the client can tell a
    brand-new row from a returned existing row.
    """
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project_id,
            requirement_id=requirement_id,
            tool_name=body.tool_name,
            tool_action=body.tool_action,
            request_id=body.request_id,
            payload=body.payload,
            agent_run_id=body.agent_run_id,
        )
    except ToolValidationError as exc:
        db.rollback()
        raise _resolve_validation_error(exc) from exc
    db.commit()
    db.refresh(outcome.run)
    # The idempotent re-issue path returns the same row with
    # ``created=false``; the response carries the flag so a caller
    # can distinguish a brand-new row from a duplicate request.
    headers = {"X-Tool-Run-Created": "true" if outcome.created else "false"}
    return Response(
        content=_tool_run_to_read(outcome.run).model_dump_json(),
        media_type="application/json",
        headers=headers,
        status_code=201 if outcome.created else 200,
    )


@router.get(
    "/requirements/{requirement_id}/tool-runs",
    response_model=ToolRunPage,
)
def list_tool_runs_for_requirement_route(
    requirement_id: UUID,
    project_id: UUID = Query(...),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> ToolRunPage:
    """Paginated tool-run history for one Requirement.

    The ``active_run_id`` pointer is computed by a separate query
    so it stays stable regardless of pagination. The endpoint
    requires ``project_id`` and verifies the Requirement belongs to
    the project — cross-project access returns 404.
    """
    try:
        total, rows, active_run_id = list_tool_runs_for_requirement(
            db,
            project_id=project_id,
            requirement_id=requirement_id,
            limit=limit,
            offset=offset,
        )
    except ToolValidationError as exc:
        raise _resolve_validation_error(exc) from exc
    return ToolRunPage(
        total=total,
        items=[_tool_run_to_read(r) for r in rows],
        limit=limit,
        offset=offset,
        active_run_id=active_run_id,
    )


@router.get("/tool-runs/{tool_run_id}", response_model=ToolRunRead)
def get_tool_run_route(
    tool_run_id: UUID,
    project_id: UUID = Query(...),
    db: Session = Depends(get_db),
) -> ToolRunRead:
    """Read one tool run.

    ``project_id`` is REQUIRED; missing it is 422 (FastAPI
    validation), a wrong value is 404. The endpoint is part of
    the audit surface; it MUST NOT leak rows across projects.
    """
    try:
        run = get_tool_run(db, project_id=project_id, tool_run_id=tool_run_id)
    except ToolValidationError as exc:
        raise _resolve_validation_error(exc) from exc
    return _tool_run_to_read(run)


@router.post(
    "/tool-runs/{tool_run_id}/cancel",
    response_model=ToolRunRead,
)
def cancel_tool_run_route(
    tool_run_id: UUID,
    project_id: UUID = Query(...),
    db: Session = Depends(get_db),
) -> ToolRunRead:
    """Transition awaiting_approval → cancelled.

    ``status_code`` is 200 on success. Any other source status
    surfaces 409 so a completed run cannot be silently rewritten.
    """
    try:
        run = cancel_tool_run(
            db, project_id=project_id, tool_run_id=tool_run_id
        )
    except ToolValidationError as exc:
        db.rollback()
        raise _resolve_validation_error(exc) from exc
    db.commit()
    db.refresh(run)
    return _tool_run_to_read(run)


@router.post(
    "/tool-runs/{tool_run_id}/approve",
    response_model=ToolRunRead,
)
def approve_tool_run_route(
    tool_run_id: UUID,
    project_id: UUID = Query(...),
    body: ToolApproveRequest = Body(default_factory=ToolApproveRequest),
    db: Session = Depends(get_db),
) -> ToolRunRead:
    """Drive the simulator for an awaiting_approval row.

    The body is empty by design (``extra=forbid``); any submitted
    field is rejected with 422 BEFORE the service runs. The
    endpoint is the ONLY way to flip the run to ``completed``;
    a second Approve on a completed run is a no-op (returns the
    existing row without producing a duplicate audit row).
    """
    try:
        run = approve_tool_run(
            db, project_id=project_id, tool_run_id=tool_run_id
        )
    except ToolValidationError as exc:
        db.rollback()
        raise _resolve_validation_error(exc) from exc
    except ToolConfigError as exc:
        db.rollback()
        raise HTTPException(503, str(exc)) from exc
    db.commit()
    db.refresh(run)
    return _tool_run_to_read(run)


__all__ = ["router"]