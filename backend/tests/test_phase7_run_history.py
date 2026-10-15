"""Project-scoped persisted run history used by the Phase 7 Analysis page."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import AnalysisRun, InsightRun, Project


def test_project_run_history_isolated_paginated_and_stable(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'runs.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    when = datetime(2026, 10, 15, tzinfo=timezone.utc)
    with session_factory() as db:
        first_project = Project(name="First", product_name="A")
        other_project = Project(name="Other", product_name="B")
        db.add_all([first_project, other_project])
        db.flush()
        first_id, other_id = first_project.id, other_project.id
        run_ids = [UUID(int=n) for n in (1, 2, 3)]
        for run_id, project_id, kind in (
            (run_ids[0], first_id, "classification"),
            (run_ids[1], first_id, "clustering"),
            (run_ids[2], other_id, "classification"),
        ):
            db.add(
                AnalysisRun(
                    id=run_id,
                    project_id=project_id,
                    run_type=kind,
                    prompt_version="test",
                    status="completed",
                    created_at=when,
                )
            )
        db.flush()
        insight_ids = [UUID(int=n) for n in (11, 12)]
        for insight_id, project_id, upstream in (
            (insight_ids[0], first_id, run_ids[1]),
            (insight_ids[1], other_id, run_ids[2]),
        ):
            db.add(
                InsightRun(
                    id=insight_id,
                    project_id=project_id,
                    clustering_run_id=upstream,
                    classification_run_id=upstream,
                    prompt_version="test",
                    status="completed",
                    created_at=when,
                )
            )
        db.commit()

    def override_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            base = f"/projects/{first_id}"
            page1 = client.get(f"{base}/analysis-runs", params={"limit": 1, "offset": 0})
            page2 = client.get(f"{base}/analysis-runs", params={"limit": 1, "offset": 1})
            assert page1.status_code == page2.status_code == 200
            assert page1.json()["total"] == page2.json()["total"] == 2
            assert [page1.json()["items"][0]["id"], page2.json()["items"][0]["id"]] == [
                str(run_ids[1]),
                str(run_ids[0]),
            ]
            assert (
                client.get(f"{base}/analysis-runs", params={"run_type": "classification"}).json()[
                    "total"
                ]
                == 1
            )
            assert (
                client.get(f"{base}/analysis-runs", params={"run_type": "clustering"}).json()[
                    "total"
                ]
                == 1
            )
            assert (
                client.get(f"{base}/analysis-runs", params={"run_type": "invalid"}).status_code
                == 422
            )
            assert client.get(f"{base}/analysis-runs", params={"limit": 201}).status_code == 422
            insights = client.get(f"{base}/insight-runs").json()
            assert insights["total"] == 1
            assert [row["id"] for row in insights["items"]] == [str(insight_ids[0])]
            assert client.get(f"{base}/insight-runs", params={"offset": 1}).json()["items"] == []
            missing = uuid4()
            assert client.get(f"/projects/{missing}/analysis-runs").status_code == 404
            assert client.get(f"/projects/{missing}/insight-runs").status_code == 404
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
