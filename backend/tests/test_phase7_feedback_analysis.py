"""Feedback detail analysis read selection and project isolation."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import AnalysisRun, Feedback, FeedbackAnalysis, Project


def test_feedback_analysis_latest_explicit_empty_and_isolation(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'feedback-analysis.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    with factory() as db:
        a = Project(name="A", product_name="A")
        b = Project(name="B", product_name="B")
        db.add_all([a, b])
        db.flush()
        feedback = Feedback(project_id=a.id, external_id="F-1", content="Raw unchanged")
        no_analysis = Feedback(project_id=a.id, external_id="F-2", content="No analysis")
        foreign = Feedback(project_id=b.id, external_id="F-1", content="Other project")
        db.add_all([feedback, no_analysis, foreign])
        db.flush()
        old = AnalysisRun(
            project_id=a.id,
            run_type="classification",
            prompt_version="v1",
            status="completed",
            created_at=now,
        )
        latest = AnalysisRun(
            project_id=a.id,
            run_type="classification",
            prompt_version="v1",
            status="completed",
            created_at=now + timedelta(minutes=1),
        )
        cluster = AnalysisRun(
            project_id=a.id,
            run_type="clustering",
            prompt_version="v1",
            status="completed",
            created_at=now + timedelta(minutes=2),
        )
        other = AnalysisRun(
            project_id=b.id,
            run_type="classification",
            prompt_version="v1",
            status="completed",
            created_at=now + timedelta(minutes=3),
        )
        db.add_all([old, latest, cluster, other])
        db.flush()
        for run, label in ((old, "old"), (latest, "latest"), (cluster, "cluster")):
            db.add(
                FeedbackAnalysis(
                    feedback_id=feedback.id,
                    run_id=run.id,
                    feedback_type="pain_point",
                    module="upload",
                    intent=label,
                    pain_point=label,
                    sentiment="negative",
                    severity=3,
                    confidence=0.8,
                )
            )
        db.commit()
        project_id, feedback_id = a.id, feedback.id
        no_analysis_id, foreign_id = no_analysis.id, foreign.id
        old_id, latest_id, cluster_id, other_id = old.id, latest.id, cluster.id, other.id

    def override():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app) as client:
            base = f"/projects/{project_id}/feedback"
            latest_response = client.get(f"{base}/{feedback_id}/analysis")
            assert latest_response.status_code == 200
            assert latest_response.json()["analysis"]["intent"] == "latest"
            assert latest_response.json()["selection"]["selected_run_id"] == str(latest_id)
            explicit = client.get(f"{base}/{feedback_id}/analysis", params={"run_id": str(old_id)})
            assert explicit.status_code == 200
            assert explicit.json()["analysis"]["intent"] == "old"
            assert client.get(f"{base}/{no_analysis_id}/analysis").json()["analysis"] is None
            assert (
                client.get(
                    f"{base}/{feedback_id}/analysis", params={"run_id": str(cluster_id)}
                ).status_code
                == 404
            )
            assert (
                client.get(
                    f"{base}/{feedback_id}/analysis", params={"run_id": str(other_id)}
                ).status_code
                == 404
            )
            assert client.get(f"{base}/{foreign_id}/analysis").status_code == 404
            assert (
                client.get(f"/projects/{uuid4()}/feedback/{feedback_id}/analysis").status_code
                == 404
            )
        with factory() as db:
            assert db.get(Feedback, feedback_id).content == "Raw unchanged"
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
