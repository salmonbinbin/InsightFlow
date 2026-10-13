from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api import analysis, clusters, feedback, insights, projects
from app.db.session import get_db

app = FastAPI(
    title="InsightFlow",
    version="0.1.0",
    description="原始反馈导入与 AI 分类 + 聚类 + 洞察生成 API。",
)
app.include_router(projects.router)
app.include_router(feedback.router)
app.include_router(analysis.router)
app.include_router(clusters.router)
app.include_router(insights.router)


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/health/ready", tags=["health"])
def ready(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1 FROM projects LIMIT 1"))
        db.execute(text("SELECT 1 FROM feedbacks LIMIT 1"))
        db.execute(text("SELECT 1 FROM analysis_runs LIMIT 1"))
        db.execute(text("SELECT 1 FROM clusters LIMIT 1"))
        db.execute(text("SELECT 1 FROM insight_runs LIMIT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(503, "数据库不可用或尚未迁移") from exc
    return {"status": "ready"}
