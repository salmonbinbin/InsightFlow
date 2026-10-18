"""Phase 8 — Research Agent endpoints.

Routes:

* ``POST /insights/{insight_id}/research`` — start a synchronous
  research run for the Insight (200, returns the persisted run).
* ``GET /research/{run_id}?project_id=...`` — full run row with
  snapshot provenance; project_id is REQUIRED and must match the
  run's owning project (cross-project reads return 404).
* ``GET /research/{run_id}/sources?project_id=...`` — paginated
  research sources (the actual provider-returned hits).
* ``GET /research/{run_id}/findings?project_id=...`` — paginated
  structured findings.
* ``GET /insights/{insight_id}/research`` — paginated research run
  history for one Insight (already project-scoped through the
  insight's project_id).

Errors:
``404`` unknown ids or cross-project access, ``409`` invalid input
chain or missing config, ``422`` malformed UUIDs / length violations.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import Insight, ResearchFinding, ResearchRun, ResearchSource
from app.schemas.research import (
    ResearchFindingListPage,
    ResearchFindingRead,
    ResearchRunListPage,
    ResearchRunRead,
    ResearchRunRequest,
    ResearchSourceListPage,
    ResearchSourceRead,
    ResearchSourceRefRead,
)
from app.services.research_service import (
    ResearchConfigError,
    list_findings_for_run,
    list_sources_for_run,
    start_research_run,
)

router = APIRouter(tags=["research"])


# ---- helpers --------------------------------------------------------------


def _require_research_run(db: Session, run_id: UUID, *, project_id: UUID) -> ResearchRun:
    """Load a run and verify the caller passes the owning project_id.

    A 404 is returned both for an unknown run id and for a run that
    belongs to a different project — the two cases are collapsed so a
    malicious caller cannot distinguish "not found" from "belongs to
    someone else".
    """
    run = db.get(ResearchRun, run_id)
    if run is None or run.project_id != project_id:
        raise HTTPException(404, "研究运行不存在")
    return run


def _require_insight(db: Session, insight_id: UUID) -> Insight:
    insight = db.get(Insight, insight_id)
    if insight is None:
        raise HTTPException(404, "洞察不存在")
    return insight


def _read_run(db: Session, run: ResearchRun) -> ResearchRunRead:
    return ResearchRunRead.model_validate(run)


def _read_finding(db: Session, finding: ResearchFinding) -> ResearchFindingRead:
    """Hydrate a Finding row into the wire DTO with ``source_refs`` resolved."""
    raw_refs = finding.source_refs or []
    resolved: list[ResearchSourceRefRead] = []
    needed_ids: set[UUID] = set()
    for ref in raw_refs:
        if not isinstance(ref, dict):
            continue
        sid = ref.get("source_id")
        if not sid:
            continue
        try:
            needed_ids.add(UUID(str(sid)))
        except (TypeError, ValueError):
            continue
    sources_by_id = (
        {
            s.id: s
            for s in db.scalars(
                select(ResearchSource).where(ResearchSource.id.in_(needed_ids))
            ).all()
        }
        if needed_ids
        else {}
    )
    for ref in raw_refs:
        if not isinstance(ref, dict):
            continue
        provider_source_id = ref.get("provider_source_id")
        sid = ref.get("source_id")
        try:
            sid_uuid = UUID(str(sid)) if sid else None
        except (TypeError, ValueError):
            continue
        source = sources_by_id.get(sid_uuid) if sid_uuid else None
        if not (
            isinstance(provider_source_id, str)
            and source is not None
            and source.run_id == finding.run_id
            and source.provider_source_id == provider_source_id
            and source.url
        ):
            continue
        resolved.append(
            ResearchSourceRefRead(
                provider_source_id=provider_source_id,
                url=source.url,
                title=source.title or source.domain,
            )
        )
    support_ids: list[UUID] = []
    for value in finding.supporting_finding_ids or []:
        try:
            support_ids.append(UUID(str(value)))
        except (ValueError, TypeError):
            continue
    scoped_supports = (
        list(
            db.scalars(
                select(ResearchFinding.id).where(
                    ResearchFinding.id.in_(support_ids),
                    ResearchFinding.run_id == finding.run_id,
                    ResearchFinding.claim_kind == "fact",
                    ResearchFinding.status == "confirmed",
                )
            )
        )
        if support_ids
        else []
    )
    return ResearchFindingRead(
        id=finding.id,
        run_id=finding.run_id,
        category=finding.category,
        title=finding.title,
        description=finding.description,
        confidence=finding.confidence,
        status=finding.status,
        source_refs=resolved or None,
        claim_kind=finding.claim_kind,
        supporting_finding_ids=scoped_supports or None,
        evidence_location=finding.evidence_location,
        created_at=finding.created_at,
    )


# ---- endpoints ------------------------------------------------------------


@router.post(
    "/insights/{insight_id}/research",
    response_model=ResearchRunRead,
    status_code=200,
)
def start_research(
    insight_id: UUID,
    body: ResearchRunRequest = Body(...),
    db: Session = Depends(get_db),
) -> ResearchRunRead:
    """Start one synchronous research run for an Insight.

    The user payload is a 1-500 char research question plus an optional
    0-2000 char scope; everything else (Insight metadata, representative
    Evidence summaries, PM Decision snapshot, provider query) is
    assembled server-side. The project_id is taken from the Insight row
    and the service layer cross-checks it before any external call.
    """
    insight = _require_insight(db, insight_id)
    project_id = insight.project_id
    try:
        run = start_research_run(
            db,
            project_id,
            insight_id,
            research_question=body.research_question,
            scope=body.scope,
            prompt_version=body.prompt_version,
        )
    except ResearchConfigError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _read_run(db, run)


@router.get("/research/{run_id}", response_model=ResearchRunRead)
def get_research_run(
    run_id: UUID,
    project_id: UUID = Query(...),
    db: Session = Depends(get_db),
) -> ResearchRunRead:
    """Read one research run.

    ``project_id`` is a REQUIRED query parameter and must match the
    run's owning project. A missing or mismatched project_id yields
    404 (not 403) so the endpoint does not leak the existence of a
    run that belongs to another project.
    """
    run = _require_research_run(db, run_id, project_id=project_id)
    return _read_run(db, run)


@router.get(
    "/research/{run_id}/sources",
    response_model=ResearchSourceListPage,
)
def list_research_sources(
    run_id: UUID,
    project_id: UUID = Query(...),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    """List the persisted research sources for one run.

    The ``project_id`` query parameter is REQUIRED and must match the
    run's owning project. The endpoint is part of the audit surface;
    it MUST NOT leak sources across projects.
    """
    _require_research_run(db, run_id, project_id=project_id)
    total, rows = list_sources_for_run(db, run_id=run_id, limit=limit, offset=offset)
    return {
        "total": total,
        "items": [ResearchSourceRead.model_validate(row) for row in rows],
        "limit": limit,
        "offset": offset,
    }


@router.get(
    "/research/{run_id}/findings",
    response_model=ResearchFindingListPage,
)
def list_research_findings(
    run_id: UUID,
    project_id: UUID = Query(...),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    """List the structured findings for one run.

    The ``project_id`` query parameter is REQUIRED and must match the
    run's owning project. The endpoint is part of the audit surface;
    it MUST NOT leak findings across projects.
    """
    _require_research_run(db, run_id, project_id=project_id)
    total, rows = list_findings_for_run(db, run_id=run_id, limit=limit, offset=offset)
    return {
        "total": total,
        "items": [_read_finding(db, row) for row in rows],
        "limit": limit,
        "offset": offset,
    }


@router.get(
    "/insights/{insight_id}/research",
    response_model=ResearchRunListPage,
)
def list_insight_research_runs(
    insight_id: UUID,
    project_id: UUID = Query(...),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    """Paginated research history for one Insight.

    The endpoint is project-scoped through a REQUIRED ``project_id``
    query parameter: the project must own the Insight. Both unknown
    insight ids and project / insight mismatches return 404 so a
    malicious caller cannot distinguish "not found" from "belongs
    to a different project". An existing insight owned by the
    caller with no research runs yet returns
    ``{total: 0, items: [], limit, offset}``.
    """
    insight = _require_insight(db, insight_id)
    if insight.project_id != project_id:
        raise HTTPException(404, "洞察不存在")
    where = ResearchRun.insight_id == insight.id
    total = db.scalar(select(func.count()).select_from(ResearchRun).where(where))
    rows = db.scalars(
        select(ResearchRun)
        .where(where)
        .order_by(ResearchRun.created_at.desc(), ResearchRun.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return {
        "total": total or 0,
        "items": [_read_run(db, row) for row in rows],
        "limit": limit,
        "offset": offset,
    }


__all__ = ["router"]
