"""Project-bound CSV review workflow; no credential or model access."""

import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.evals.reference_review import LABEL_STATUS, ReviewError, evaluate_review, make_template
from app.evals.runner import EvaluationRunnerValidationError
from app.models.evaluation import EvaluationCase, EvaluationMetric, EvaluationRun
from app.schemas.evaluation import EvaluationRunRead

router = APIRouter(tags=["evaluations"])
Kind = Literal["classification_metrics", "cluster_metrics"]


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    source_run_id: UUID
    evaluation_type: Kind
    csv_text: str = Field(min_length=1, max_length=2 * 1024 * 1024)


@router.get("/evaluations/reference-template")
def download_template(
    project_id: UUID, source_run_id: UUID, evaluation_type: Kind, db: Session = Depends(get_db)
):
    try:
        return make_template(
            db, project_id=project_id, source_run_id=source_run_id, evaluation_type=evaluation_type
        )
    except ReviewError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.post("/evaluations/reference-review", response_model=EvaluationRunRead)
def create_review(body: ReviewRequest, db: Session = Depends(get_db)):
    try:
        return evaluate_review(db, **body.model_dump())
    except ReviewError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except EvaluationRunnerValidationError as exc:
        raise HTTPException(409, "原运行的结果或输入不一致，无法评测。") from exc


@router.get("/evaluations/{run_id}/reference-review")
def read_review(
    run_id: UUID,
    project_id: UUID,
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    run = db.get(EvaluationRun, run_id)
    if not run or run.project_id != project_id or run.dataset_label_status != LABEL_STATUS:
        raise HTTPException(404, "人工参考报告不存在。")
    where = (EvaluationCase.run_id == run_id, EvaluationCase.project_id == project_id)
    total = db.scalar(select(func.count()).select_from(EvaluationCase).where(*where)) or 0
    rows = db.scalars(
        select(EvaluationCase)
        .where(*where)
        .order_by(EvaluationCase.case_id)
        .limit(limit)
        .offset(offset)
    ).all()
    metric = db.scalar(select(EvaluationMetric).where(EvaluationMetric.run_id == run_id).limit(1))
    source_count = (metric.details or {}).get("source_input_count") if metric else None
    items = []
    for r in rows:
        expected = json.loads(r.expected)
        items.append(
            {
                "feedback_id": r.case_id,
                "reviewer": expected["reviewer"],
                "expected": expected,
                "actual": json.loads(r.actual),
                "passed": r.passed,
            }
        )
    return {
        "project_id": str(project_id),
        "run_id": str(run_id),
        "stage": run.stage,
        "source_input_count": source_count,
        "reviewed_count": run.input_count,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": items,
    }
