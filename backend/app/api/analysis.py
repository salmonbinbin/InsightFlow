"""Analysis API endpoints (Phase 1)."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import AnalysisRun, FeedbackAnalysis, Project, RunStatus
from app.schemas.analysis import (
    AnalysisRunRead,
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


# Reuse these status constants in OpenAPI examples without re-importing elsewhere.
__all__ = ["router", "RunStatus"]