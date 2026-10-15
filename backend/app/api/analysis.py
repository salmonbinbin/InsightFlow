"""Analysis API endpoints (Phase 1)."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import AnalysisRun, FeedbackAnalysis, Project, RunStatus
from app.schemas.analysis import (
    AnalysisRunPage,
    AnalysisRunRead,
    AnalysisRunTypeLiteral,
    FeedbackAnalysisPage,
)
from app.services.analysis_runner import retry_run, start_run
from app.services.classification import ClassificationConfigError

router = APIRouter(tags=["analysis"])


@router.post(
    "/projects/{project_id}/analysis",
    response_model=AnalysisRunRead,
    status_code=200,
)
def create_analysis(project_id: UUID, db: Session = Depends(get_db)) -> AnalysisRun:
    if db.get(Project, project_id) is None:
        raise HTTPException(404, "项目不存在")
    try:
        return start_run(db, project_id)
    except ClassificationConfigError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/analysis/{run_id}", response_model=AnalysisRunRead)
def get_run(run_id: UUID, db: Session = Depends(get_db)) -> AnalysisRun:
    run = db.get(AnalysisRun, run_id)
    if run is None:
        raise HTTPException(404, "分析运行不存在")
    return run


@router.post("/analysis/{run_id}/retry", response_model=AnalysisRunRead)
def retry_analysis(run_id: UUID, db: Session = Depends(get_db)) -> AnalysisRun:
    try:
        return retry_run(db, run_id)
    except LookupError:
        raise HTTPException(404, "分析运行不存在") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get(
    "/analysis/{run_id}/results",
    response_model=FeedbackAnalysisPage,
)
def list_results(
    run_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    if db.get(AnalysisRun, run_id) is None:
        raise HTTPException(404, "分析运行不存在")
    where = FeedbackAnalysis.run_id == run_id
    total = db.scalar(select(func.count()).select_from(FeedbackAnalysis).where(where))
    items = db.scalars(
        select(FeedbackAnalysis)
        .where(where)
        .order_by(FeedbackAnalysis.created_at, FeedbackAnalysis.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return {"total": total or 0, "items": items, "limit": limit, "offset": offset}


@router.get(
    "/projects/{project_id}/analysis-runs",
    response_model=AnalysisRunPage,
)
def list_project_analysis_runs(
    project_id: UUID,
    run_type: AnalysisRunTypeLiteral | None = Query(default=None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    """Project-scoped paginated history of AnalysisRun rows.

    The endpoint exists for the Phase 7 Analysis history tab and is
    intentionally read-only — it never creates / mutates rows. The
    ``run_type`` filter is restricted to the two values the runner
    actually emits (``classification`` / ``clustering``) at the
    request boundary so a stale or invented value cannot leak into
    the SQL ``WHERE`` clause. Sort is ``created_at DESC, id DESC``
    with a project_id index scan so pagination stays stable when
    two rows share a ``created_at`` (e.g. bulk-insert under a
    single transaction). An empty project returns
    ``{total: 0, items: [], limit, offset}`` — no 404.
    """
    if db.get(Project, project_id) is None:
        raise HTTPException(404, "项目不存在")
    where = AnalysisRun.project_id == project_id
    if run_type is not None:
        where = (AnalysisRun.project_id == project_id) & (AnalysisRun.run_type == run_type)
    total = db.scalar(select(func.count()).select_from(AnalysisRun).where(where))
    items = db.scalars(
        select(AnalysisRun)
        .where(where)
        .order_by(AnalysisRun.created_at.desc(), AnalysisRun.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return {"total": total or 0, "items": items, "limit": limit, "offset": offset}


# Reuse these status constants in OpenAPI examples without re-importing elsewhere.
__all__ = ["router", "RunStatus"]
