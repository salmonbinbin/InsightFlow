"""Custom-project checks must not manufacture semantic scores or expand inputs."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import AnalysisRun, Cluster, ClusterMember, Feedback, FeedbackAnalysis, Project
from app.models.evaluation import EvaluationRun


@pytest.fixture
def fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'checks.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        project = Project(name="Custom", product_name="Files")
        other = Project(name="Other", product_name="Other")
        db.add_all([project, other])
        db.flush()
        feedbacks = [
            Feedback(project_id=project.id, external_id=f"SYN-{i}", content=f"模拟反馈{i}")
            for i in range(3)
        ]
        db.add_all(feedbacks)
        db.flush()
        source = AnalysisRun(
            project_id=project.id,
            run_type="classification",
            status="completed",
            prompt_version="test",
            input_count=2,
            processed_count=2,
            success_count=2,
            input_feedback_external_ids=["SYN-0", "SYN-1"],
        )
        db.add(source)
        db.flush()
        analyses = [
            FeedbackAnalysis(
                feedback_id=f.id,
                run_id=source.id,
                feedback_type="bug",
                module="upload",
                intent="修复",
                pain_point="失败",
                sentiment="negative",
                severity=3,
                confidence=0.8,
            )
            for f in feedbacks[:2]
        ]
        db.add_all(analyses)
        db.commit()
        ids = dict(project=project.id, other=other.id, source=source.id, analysis=analyses[0].id)

    def get_test_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = get_test_db
    try:
        with TestClient(app) as client:
            yield client, factory, ids
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def create(client, ids, **overrides):
    body = dict(
        project_id=str(ids["project"]),
        source_run_id=str(ids["source"]),
        evaluation_type="classification_metrics",
    )
    return client.post("/evaluations/run-check", json=body | overrides)


def test_custom_inputs_frozen_and_quality_unmeasured(fixture):
    client, factory, ids = fixture
    response = create(client, ids)
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["input_count"] == 2  # later-imported SYN-2 must not enter denominator
    assert run["evaluation_type"] == "pipeline_health"
    assert run["dataset_label_status"] == "no_reference_labels"
    metrics = client.get(f"/evaluations/{run['id']}/metrics").json()["items"]
    values = {m["metric_name"]: m["value"] for m in metrics}
    assert values["result_coverage_rate"] == 1
    assert values["feedback_type_accuracy"] is None
    cases = client.get(f"/evaluations/{run['id']}/cases").json()["items"]
    assert {c["case_id"] for c in cases} == {"SYN-0", "SYN-1"}
    assert all(c["expected_hash"] is None for c in cases)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Feedback)) == 3


@pytest.mark.parametrize(
    "change", ["foreign", "running", "wrong_type", "no_snapshot", "snapshot_mismatch"]
)
def test_rejects_invalid_sources_without_writes(fixture, change):
    client, factory, ids = fixture
    with factory() as db:
        source = db.get(AnalysisRun, ids["source"])
        if change == "running":
            source.status = "running"
        if change == "wrong_type":
            source.run_type = "clustering"
        if change == "no_snapshot":
            source.input_feedback_external_ids = None
        if change == "snapshot_mismatch":
            source.input_feedback_external_ids = ["SYN-0"]
        db.commit()
    response = create(
        client, ids, **({"project_id": str(ids["other"])} if change == "foreign" else {})
    )
    assert response.status_code == 409, response.text
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(EvaluationRun)) == 0


def test_missing_result_reported_as_failure(fixture):
    client, factory, ids = fixture
    with factory() as db:
        db.delete(db.get(FeedbackAnalysis, ids["analysis"]))
        db.commit()
    response = create(client, ids)
    assert response.status_code == 200, response.text
    assert response.json()["failed_count"] == 1
    assert response.json()["status"] == "partial_failed"


def test_cluster_checks_membership_not_semantic_purity(fixture):
    client, factory, ids = fixture
    with factory() as db:
        cluster_run = AnalysisRun(
            project_id=ids["project"],
            run_type="clustering",
            status="completed",
            prompt_version="test",
            model="local-hash-v1",
            input_count=2,
            processed_count=2,
            success_count=2,
            input_feedback_external_ids=["SYN-0", "SYN-1"],
            cluster_config={"classification_run_id": str(ids["source"])},
        )
        db.add(cluster_run)
        db.flush()
        cluster = Cluster(
            project_id=ids["project"],
            run_id=cluster_run.id,
            embedding_model="local-hash-v1",
            distance_threshold=0.45,
            linkage="average",
            feedback_count=1,
        )
        db.add(cluster)
        db.flush()
        db.add(
            ClusterMember(
                cluster_id=cluster.id, feedback_analysis_id=ids["analysis"], similarity_score=0.9
            )
        )
        db.commit()
        cluster_id = cluster_run.id
    response = create(client, ids, source_run_id=str(cluster_id), evaluation_type="cluster_metrics")
    assert response.status_code == 200, response.text
    assert response.json()["failed_count"] == 1
    metrics = client.get(f"/evaluations/{response.json()['id']}/metrics").json()["items"]
    assert next(m["value"] for m in metrics if m["metric_name"] == "cluster_purity_micro") is None


def test_rejects_foreign_result_and_preserves_input(fixture):
    client, factory, ids = fixture
    with factory() as db:
        foreign = Feedback(project_id=ids["other"], external_id="FOREIGN", content="private")
        db.add(foreign)
        db.flush()
        db.get(FeedbackAnalysis, ids["analysis"]).feedback_id = foreign.id
        db.commit()
    assert create(client, ids).status_code == 409
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(EvaluationRun)) == 0
        assert (
            db.scalar(select(Feedback.content).where(Feedback.external_id == "SYN-0"))
            == "模拟反馈0"
        )


def test_report_get_is_project_scoped(fixture):
    client, _, ids = fixture
    report = create(client, ids).json()
    response = client.get(f"/evaluations/{report['id']}", params={"project_id": str(ids["other"])})
    assert response.status_code == 404
