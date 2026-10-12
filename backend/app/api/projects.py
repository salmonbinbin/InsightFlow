from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import Project
from app.schemas.project import ProjectCreate, ProjectRead

router = APIRouter(prefix="/projects", tags=["projects"])


def require_project(project_id: UUID, db: Session):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


@router.post("", response_model=ProjectRead, status_code=201)
def create_project(body: ProjectCreate, db: Session = Depends(get_db)):
    project = Project(**body.model_dump())
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.get("", response_model=list[ProjectRead])
def list_projects(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return db.scalars(
        select(Project).order_by(Project.created_at, Project.id).limit(limit).offset(offset)
    ).all()


@router.get("/{project_id}", response_model=ProjectRead)
def get_project(project_id: UUID, db: Session = Depends(get_db)):
    return require_project(project_id, db)
