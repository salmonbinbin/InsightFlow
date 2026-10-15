"""Phase 3 — Insight endpoints.

Endpoints:

* ``POST /projects/{project_id}/insights/generate`` — start a synchronous
  insight run for an explicit clustering run id (body).
* ``GET /insight-runs/{run_id}`` — run status with snapshot provenance.
* ``POST /insight-runs/{run_id}/retry`` — retry only the clusters whose
  status is ``failed`` or ``needs_validation``; completed runs are no-ops.
* ``GET /projects/{project_id}/insight-runs`` — paginated project-scoped
  history of insight runs (Phase 7 Analysis history tab).
* ``GET /projects/{project_id}/insights`` — paginated list of insights
  (optionally filter by ``run_id``).
* ``GET /insights/{insight_id}`` — single insight with snapshot metadata.
* ``GET /insights/{insight_id}/evidence`` — paginated evidence with
  feedback content read directly from ``feedbacks.content``.

The endpoints are synchronous (200) so the caller observes the final
state; we never return ``202`` for these routes. Error mapping:
``404`` unknown ids, ``409`` invalid input chains or already-complete
runs being retried after a configuration drift, ``422`` invalid path
UUIDs.
"""

from __future__ import annotations

from typing import Sequence
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import (
    Feedback,
    FeedbackAnalysis,
    Insight,
    InsightEvidence,
    InsightRun,
    InsightRunItem,
    Project,
)
from app.schemas.insight import (
    FeedbackAnalysisRead,
    InsightEvidencePage,
    InsightEvidenceRead,
    InsightGenerateRequest,
    InsightPage,
    InsightRead,
    InsightRunItemRead,
    InsightRunPage,
    InsightRunRead,
)
from app.services.insight_runner import (
    InsightConfigError,
    retry_insight_run,
    start_insight_run,
)

router = APIRouter(tags=["insights"])


# ----- helpers --------------------------------------------------------------


def _require_project(db: Session, project_id: UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


def _require_insight_run(db: Session, run_id: UUID) -> InsightRun:
    run = db.get(InsightRun, run_id)
    if run is None:
        raise HTTPException(404, "洞察运行不存在")
    return run


def _require_insight(db: Session, insight_id: UUID) -> Insight:
    insight = db.get(Insight, insight_id)
    if insight is None:
        raise HTTPException(404, "洞察不存在")
    return insight


def _latest_insight_run(db: Session, project_id: UUID) -> InsightRun | None:
    return db.scalar(
        select(InsightRun)
        .where(InsightRun.project_id == project_id)
        .order_by(InsightRun.created_at.desc(), InsightRun.id.desc())
    )


def _clustering_run_for(db: Session, insight: Insight) -> UUID:
    run = db.get(InsightRun, insight.run_id)
    if run is None:
        raise HTTPException(404, "洞察运行不存在")
    return run.clustering_run_id


def _read_insight(db: Session, insight: Insight) -> InsightRead:
    return InsightRead(
        id=insight.id,
        run_id=insight.run_id,
        project_id=insight.project_id,
        cluster_id=insight.cluster_id,
        clustering_run_id=_clustering_run_for(db, insight),
        title=insight.title,
        pain_point=insight.pain_point,
        affected_segment=insight.affected_segment,
        insight=insight.insight,
        recommendation=insight.recommendation,
        confidence=insight.confidence,
        status=insight.status,
        feedback_count=insight.feedback_count,
        evidence_count=insight.evidence_count,
        source_cluster_external_id=insight.source_cluster_external_id,
        source_member_external_ids=list(insight.source_member_external_ids or []),
        model=insight.model,
        prompt_version=insight.prompt_version,
        created_at=insight.created_at,
    )


# ----- endpoints ------------------------------------------------------------


@router.post(
    "/projects/{project_id}/insights/generate",
    response_model=InsightRunRead,
    status_code=200,
)
def generate_insights(
    project_id: UUID,
    body: InsightGenerateRequest = Body(...),
    db: Session = Depends(get_db),
) -> InsightRun:
    _require_project(db, project_id)
    try:
        return start_insight_run(db, project_id, clustering_run_id=body.clustering_run_id)
    except InsightConfigError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/insight-runs/{run_id}", response_model=InsightRunRead)
def get_insight_run(run_id: UUID, db: Session = Depends(get_db)) -> InsightRun:
    return _require_insight_run(db, run_id)


@router.get(
    "/insight-runs/{run_id}/items",
    response_model=list[InsightRunItemRead],
)
def list_insight_run_items(run_id: UUID, db: Session = Depends(get_db)) -> list[InsightRunItem]:
    _require_insight_run(db, run_id)
    items = db.scalars(
        select(InsightRunItem)
        .where(InsightRunItem.run_id == run_id)
        .order_by(InsightRunItem.created_at, InsightRunItem.cluster_id)
    ).all()
    return list(items)


@router.post(
    "/insight-runs/{run_id}/retry",
    response_model=InsightRunRead,
    status_code=200,
)
def retry_insights(run_id: UUID, db: Session = Depends(get_db)) -> InsightRun:
    try:
        return retry_insight_run(db, run_id)
    except LookupError:
        raise HTTPException(404, "洞察运行不存在") from None
    except InsightConfigError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get(
    "/projects/{project_id}/insight-runs",
    response_model=InsightRunPage,
)
def list_project_insight_runs(
    project_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    """Project-scoped paginated history of InsightRun rows.

    Read-only sibling of ``GET /insight-runs/{run_id}`` for the
    Phase 7 Analysis history tab. The 404 only fires for an
    unknown project; an existing project with no insight runs
    yet returns ``{total: 0, items: [], limit, offset}`` so the
    frontend can render the empty state without a follow-up
    round-trip. Sort is ``created_at DESC, id DESC`` (the
    ``id`` tiebreaker matches the ``created_at`` index scan so
    pagination stays stable when two runs share a timestamp).
    """
    _require_project(db, project_id)
    where = InsightRun.project_id == project_id
    total = db.scalar(select(func.count()).select_from(InsightRun).where(where))
    items = db.scalars(
        select(InsightRun)
        .where(where)
        .order_by(InsightRun.created_at.desc(), InsightRun.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return {"total": total or 0, "items": items, "limit": limit, "offset": offset}


@router.get(
    "/projects/{project_id}/insights",
    response_model=InsightPage,
)
def list_project_insights(
    project_id: UUID,
    run_id: UUID | None = Query(default=None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    """Paginated insight list (optionally filter by ``run_id``)."""
    _require_project(db, project_id)
    if run_id is not None:
        target_run = _require_insight_run(db, run_id)
        if target_run.project_id != project_id:
            raise HTTPException(404, "洞察运行不属于该项目")
        target_run_id = target_run.id
    else:
        latest = _latest_insight_run(db, project_id)
        if latest is None:
            return {"total": 0, "items": [], "limit": limit, "offset": offset}
        target_run_id = latest.id

    where = Insight.run_id == target_run_id
    total = db.scalar(select(func.count()).select_from(Insight).where(where))
    items: Sequence[Insight] = db.scalars(
        select(Insight)
        .where(where)
        .order_by(Insight.created_at, Insight.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return {
        "total": total or 0,
        "items": [_read_insight(db, item) for item in items],
        "limit": limit,
        "offset": offset,
    }


@router.get("/insights/{insight_id}", response_model=InsightRead)
def get_insight(insight_id: UUID, db: Session = Depends(get_db)) -> InsightRead:
    insight = _require_insight(db, insight_id)
    return _read_insight(db, insight)


@router.get(
    "/insights/{insight_id}/evidence",
    response_model=InsightEvidencePage,
)
def list_insight_evidence(
    insight_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    """List evidence for one insight.

    The endpoint hydrates each evidence row with the original feedback
    content (``feedbacks.content``) and metadata so the caller's audit
    trail never depends on a model echo. Phase 7 work package B1 also
    attaches the deterministic AI classification for each piece of
    evidence, sourced exclusively from
    ``FeedbackAnalysis(feedback_id, run_id=insight.run.classification_run_id)``
    — we never cross-reference a different run because that would
    silently lie about provenance.
    """
    insight = _require_insight(db, insight_id)
    run = db.get(InsightRun, insight.run_id)
    classification_run_id = run.classification_run_id if run is not None else None

    where = InsightEvidence.insight_id == insight_id
    total = db.scalar(select(func.count()).select_from(InsightEvidence).where(where))
    evidence_rows = db.scalars(
        select(InsightEvidence)
        .where(where)
        .order_by(InsightEvidence.created_at, InsightEvidence.id)
        .limit(limit)
        .offset(offset)
    ).all()
    feedback_ids = [row.feedback_id for row in evidence_rows]
    feedbacks_by_id = (
        {f.id: f for f in db.scalars(select(Feedback).where(Feedback.id.in_(feedback_ids))).all()}
        if feedback_ids
        else {}
    )
    analyses_by_feedback_id: dict[UUID, FeedbackAnalysis] = {}
    if feedback_ids and classification_run_id is not None:
        analyses_by_feedback_id = {
            a.feedback_id: a
            for a in db.scalars(
                select(FeedbackAnalysis).where(
                    FeedbackAnalysis.feedback_id.in_(feedback_ids),
                    FeedbackAnalysis.run_id == classification_run_id,
                )
            ).all()
        }
    items = [
        InsightEvidenceRead(
            id=row.id,
            insight_id=row.insight_id,
            feedback_id=row.feedback_id,
            feedback_external_id=row.feedback_external_id,
            feedback_content=(
                feedbacks_by_id.get(row.feedback_id).content
                if feedbacks_by_id.get(row.feedback_id)
                else ""
            ),
            evidence_type=row.evidence_type,
            relevance_score=row.relevance_score,
            created_at=row.created_at,
            feedback_created_at=feedbacks_by_id.get(row.feedback_id).feedback_created_at
            if feedbacks_by_id.get(row.feedback_id)
            else None,
            feedback_user_id=feedbacks_by_id.get(row.feedback_id).user_id
            if feedbacks_by_id.get(row.feedback_id)
            else None,
            feedback_source=feedbacks_by_id.get(row.feedback_id).source
            if feedbacks_by_id.get(row.feedback_id)
            else None,
            feedback_analysis=(
                FeedbackAnalysisRead.model_validate(analyses_by_feedback_id[row.feedback_id])
                if row.feedback_id in analyses_by_feedback_id
                else None
            ),
        )
        for row in evidence_rows
    ]
    return {"total": total or 0, "items": items, "limit": limit, "offset": offset}


__all__ = ["router"]
