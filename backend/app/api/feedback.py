from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.projects import require_project
from app.db.session import get_db
from app.models import AnalysisRun, Feedback, FeedbackAnalysis
from app.schemas.analysis import FeedbackAnalysisRead
from app.schemas.feedback import (
    FeedbackAnalysisEnvelopeRead,
    FeedbackAnalysisSelectionRead,
    FeedbackPage,
    ImportResult,
)
from app.services.feedback_importer import ImportConflict, import_feedback
from app.services.file_parser import MAX_BYTES, CSVValidationError, parse_csv

router = APIRouter(prefix="/projects/{project_id}/feedback", tags=["feedback"])


@router.post("/import", response_model=ImportResult)
def upload_feedback(project_id: UUID, file: UploadFile = File(...), db: Session = Depends(get_db)):
    require_project(project_id, db)
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(415, "V0.1 仅支持 .csv 文件")
    data = file.file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "文件不能超过 10 MiB")
    try:
        rows = parse_csv(data)
        return import_feedback(db, project_id, rows)
    except CSVValidationError as exc:
        raise HTTPException(422, exc.detail) from exc
    except ImportConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("", response_model=FeedbackPage)
def list_feedback(
    project_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    require_project(project_id, db)
    where = Feedback.project_id == project_id
    total = db.scalar(select(func.count()).select_from(Feedback).where(where))
    items = db.scalars(
        select(Feedback)
        .where(where)
        .order_by(Feedback.created_at, Feedback.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return {"total": total, "items": items, "limit": limit, "offset": offset}


# Phase 7 Feedback Detail (E package) — read-only per-feedback
# AI classification selection.
#
# Contract:
#   GET /projects/{project_id}/feedback/{feedback_id}/analysis
#       (?run_id=... optional)
#
# 404 paths:
#   - unknown project
#   - feedback_id missing OR belongs to a different project
#     (we never leak a feedback imported under another project)
#   - explicit run_id provided but the run doesn't exist, the
#     run doesn't belong to ``project_id``, OR the run is not a
#     classification run (clustering / future types must 404 here
#     so the page never sees a clustering row masquerading as
#     classification)
#
# 200 envelope:
#   ``feedback_id`` echoes the path id, ``analysis`` is the
#   full ``FeedbackAnalysisRead`` or ``null`` when no row was
#   found, and ``selection`` reports whether the lookup was
#   ``explicit`` (caller passed ``run_id``) or ``latest``
#   (deterministic pick), along with the run id that sourced
#   the returned row (``null`` when no analysis exists).
#
# Selection rule (no ``run_id``): the latest classification run
# owned by this project that has a ``FeedbackAnalysis`` for
# this feedback, ordered by
# ``AnalysisRun.created_at DESC, id DESC`` (the id tiebreaker
# keeps the order stable when two runs share a timestamp),
# then ``FeedbackAnalysis.id DESC`` to break ties on the
# analysis row itself. Clustering runs and other projects' runs
# are NEVER consulted — no fallback, no guess. When no analysis
# row exists the response carries ``analysis: null`` AND
# ``selection.selected_run_id: null`` (the page renders "AI
# Analysis N/A" rather than invent a row from another run).
_SELECTION_RULE_EXPLICIT = "explicit run_id query parameter — only the given run is consulted."
_SELECTION_RULE_LATEST = (
    "latest classification run owned by this project that has a "
    "FeedbackAnalysis for this feedback, ordered by "
    "AnalysisRun.created_at DESC, id DESC, then FeedbackAnalysis.id DESC. "
    "Clustering runs and other projects' runs are ignored."
)


@router.get(
    "/{feedback_id}/analysis",
    response_model=FeedbackAnalysisEnvelopeRead,
)
def get_feedback_analysis(
    project_id: UUID,
    feedback_id: UUID,
    run_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
) -> FeedbackAnalysisEnvelopeRead:
    require_project(project_id, db)

    feedback = db.get(Feedback, feedback_id)
    # 404 even when the feedback exists under a DIFFERENT project:
    # leaking the existence of an unknown-but-real id would breach
    # the cross-project isolation the rest of the package enforces.
    if feedback is None or feedback.project_id != project_id:
        raise HTTPException(404, "反馈不存在")

    def _selection(
        mode: Literal["explicit", "latest"], picked_run: UUID | None
    ) -> FeedbackAnalysisSelectionRead:
        rule = _SELECTION_RULE_EXPLICIT if mode == "explicit" else _SELECTION_RULE_LATEST
        return FeedbackAnalysisSelectionRead(
            mode=mode,
            selected_run_id=picked_run,
            selection_rule=rule,
        )

    def _envelope(
        mode: Literal["explicit", "latest"],
        analysis: FeedbackAnalysis | None,
    ) -> FeedbackAnalysisEnvelopeRead:
        picked_run = analysis.run_id if analysis is not None else None
        return FeedbackAnalysisEnvelopeRead(
            feedback_id=feedback_id,
            analysis=(
                FeedbackAnalysisRead.model_validate(analysis) if analysis is not None else None
            ),
            selection=_selection(mode, picked_run),
        )

    if run_id is not None:
        run = db.get(AnalysisRun, run_id)
        # 404 on any of: missing run, run for a different project,
        # run that's not a classification. The brief specifies all
        # three as a single 404 so the page never gets a row that
        # would silently misrepresent provenance.
        if run is None or run.project_id != project_id or run.run_type != "classification":
            raise HTTPException(404, "分析运行不存在、不属于该项目或非分类运行")
        analysis = db.scalar(
            select(FeedbackAnalysis).where(
                FeedbackAnalysis.feedback_id == feedback_id,
                FeedbackAnalysis.run_id == run_id,
            )
        )
        return _envelope("explicit", analysis)

    # Latest: filter to (this feedback, this project, classification)
    # at the SQL layer — we never want a clustering row or a foreign
    # project's run to seed the ORDER BY even if data corruption
    # allowed it. The composite order matches the brief and keeps
    # pagination-like offsets (none here, but the schema is stable) honest.
    analysis = db.scalar(
        select(FeedbackAnalysis)
        .join(AnalysisRun, AnalysisRun.id == FeedbackAnalysis.run_id)
        .where(
            FeedbackAnalysis.feedback_id == feedback_id,
            AnalysisRun.project_id == project_id,
            AnalysisRun.run_type == "classification",
        )
        .order_by(
            AnalysisRun.created_at.desc(),
            AnalysisRun.id.desc(),
            FeedbackAnalysis.id.desc(),
        )
        .limit(1)
    )
    return _envelope("latest", analysis)
