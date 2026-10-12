"""Cluster API endpoints (Phase 2).

Mirrors the Phase 1 analysis endpoints: synchronous start/retry of a
cluster run, run status lookup, and read-only views over the resulting
clusters and their members.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import (
    AnalysisRun,
    Cluster,
    ClusterMember,
    ClusterReview,
    Project,
    RunStatus,
)
from app.schemas.cluster import (
    ClusterMemberPage,
    ClusterPage,
    ClusterRead,
    ClusterReviewRead,
    ClusterRunRead,
)
from app.services.cluster_runner import (
    ClusteringConfigError,
    retry_cluster_run,
    start_cluster_run,
)

router = APIRouter(tags=["clusters"])


def _require_project(db: Session, project_id: UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


def _require_cluster_run(db: Session, run_id: UUID) -> AnalysisRun:
    run = db.get(AnalysisRun, run_id)
    if run is None:
        raise HTTPException(404, "聚类运行不存在")
    if run.run_type != "clustering":
        raise HTTPException(404, "聚类运行不存在")
    return run


def _latest_cluster_run(db: Session, project_id: UUID) -> AnalysisRun | None:
    return db.scalar(
        select(AnalysisRun)
        .where(
            AnalysisRun.project_id == project_id,
            AnalysisRun.run_type == "clustering",
        )
        .order_by(AnalysisRun.created_at.desc(), AnalysisRun.id.desc())
    )


@router.post(
    "/projects/{project_id}/clustering",
    response_model=ClusterRunRead,
    status_code=200,
)
def start_clustering(
    project_id: UUID, db: Session = Depends(get_db)
) -> AnalysisRun:
    _require_project(db, project_id)
    try:
        return start_cluster_run(db, project_id)
    except ClusteringConfigError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/clustering/{run_id}", response_model=ClusterRunRead)
def get_cluster_run(
    run_id: UUID, db: Session = Depends(get_db)
) -> AnalysisRun:
    return _require_cluster_run(db, run_id)


@router.post("/clustering/{run_id}/retry", response_model=ClusterRunRead)
def retry_clustering(
    run_id: UUID, db: Session = Depends(get_db)
) -> AnalysisRun:
    try:
        return retry_cluster_run(db, run_id)
    except LookupError:
        raise HTTPException(404, "聚类运行不存在") from None
    except ClusteringConfigError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get(
    "/projects/{project_id}/clusters",
    response_model=ClusterPage,
)
def list_project_clusters(
    project_id: UUID,
    run_id: UUID | None = Query(default=None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    """List clusters for a project.

    Without ``run_id`` we return the clusters from the latest clustering run;
    with ``run_id`` we return the clusters for that specific run so callers
    can inspect historical topology.
    """
    _require_project(db, project_id)
    target_run_id = run_id
    if target_run_id is None:
        latest = _latest_cluster_run(db, project_id)
        if latest is None:
            return {"total": 0, "items": [], "limit": limit, "offset": offset}
        target_run_id = latest.id
    else:
        if _require_cluster_run(db, target_run_id).project_id != project_id:
            raise HTTPException(404, "聚类运行不属于该项目")

    where = Cluster.run_id == target_run_id
    total = db.scalar(select(func.count()).select_from(Cluster).where(where))
    items = db.scalars(
        select(Cluster)
        .where(where)
        .order_by(Cluster.created_at, Cluster.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return {"total": total or 0, "items": items, "limit": limit, "offset": offset}


@router.get("/clusters/{cluster_id}", response_model=ClusterRead)
def get_cluster(cluster_id: UUID, db: Session = Depends(get_db)) -> Cluster:
    cluster = db.get(Cluster, cluster_id)
    if cluster is None:
        raise HTTPException(404, "聚类不存在")
    return cluster


@router.get(
    "/clusters/{cluster_id}/members",
    response_model=ClusterMemberPage,
)
def list_cluster_members(
    cluster_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    if db.get(Cluster, cluster_id) is None:
        raise HTTPException(404, "聚类不存在")
    where = ClusterMember.cluster_id == cluster_id
    total = db.scalar(select(func.count()).select_from(ClusterMember).where(where))
    items = db.scalars(
        select(ClusterMember)
        .where(where)
        .order_by(ClusterMember.created_at, ClusterMember.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return {"total": total or 0, "items": items, "limit": limit, "offset": offset}


@router.get(
    "/clusters/{cluster_id}/review",
    response_model=ClusterReviewRead | None,
)
def get_cluster_review(
    cluster_id: UUID, db: Session = Depends(get_db)
) -> ClusterReview | None:
    """Return the most recent review audit row for a cluster (if any)."""
    if db.get(Cluster, cluster_id) is None:
        raise HTTPException(404, "聚类不存在")
    return db.scalar(
        select(ClusterReview)
        .where(ClusterReview.cluster_id == cluster_id)
        .order_by(ClusterReview.created_at.desc(), ClusterReview.id.desc())
    )


__all__ = ["router", "RunStatus"]
