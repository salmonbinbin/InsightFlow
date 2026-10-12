from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.projects import require_project
from app.db.session import get_db
from app.models import Feedback
from app.schemas.feedback import FeedbackPage, ImportResult
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
