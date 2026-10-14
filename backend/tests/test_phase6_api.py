"""Phase 6 — evaluation API tests.

Coverage:

* ``POST /evaluations`` happy path with ``classification_metrics``
  and ``cluster_metrics`` — returns the run row + dataset source
  marker.
* ``GET /evaluations/{id}`` returns the run with all scalar fields
  (``dataset_label_status`` includes the synthetic + unreviewed
  marker).
* ``GET /evaluations/{id}/metrics`` returns paginated metrics with
  ``metric_kind`` / ``sample_count`` / ``value_display`` carrying
  the explicit ``NOT MEASURED`` rendering when ``value`` is ``None``.
* ``GET /evaluations/{id}/cases`` returns paginated cases WITHOUT
  raw feedback text — only SHA-256 fingerprints. ``failed_stage``
  surfaces the failing stage for failing cases. ``error_type``
  distinguishes ``no_prediction`` / etc.
* Cross-project isolation: ``?project_id=`` mismatch returns 404
  (not 403) so a malicious caller cannot probe other projects.
* 404 — unknown evaluation run id, unknown project.
* 409 — source-run side validation (wrong ``run_type``,
  non-terminal status, cross-project source).
* 422 — invalid ``evaluation_type``, extra fields in body, illegal
  UUIDs, out-of-range pagination.
* Null-metric rendering: ``intent_exact_match`` with an empty
  controlled vocabulary returns ``value_display == "NOT MEASURED"``
  and the raw ``value`` is ``None``.
* Safe response: 422 / 409 bodies must NOT echo raw SQL fragments,
  parameter tuples, or credential-like tokens.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_db
from app.evals.dataset import load_demo_dataset
from app.main import app
from app.models import Base
from app.models.analysis import AnalysisRun, FeedbackAnalysis
from app.models.cluster import Cluster, ClusterMember
from app.models.feedback import Feedback
from app.models.project import Project

# ---- Fixture ----------------------------------------------------------------


@pytest.fixture
def db_engine(tmp_path: Path):
    engine = create_engine(os.environ.get("TEST_DATABASE_URL", f"sqlite:///{tmp_path}/test.db"))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def SessionLocal(db_engine):
    return sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
def client(SessionLocal):
    def override():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---- Helpers ----------------------------------------------------------------


def _create_project(db: Session, *, name: str = "Phase6 API") -> Project:
    project = Project(name=name, product_name="InsightFlow")
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _seed_classification_fixture(
    db: Session,
    *,
    dataset=None,
    run_status: str = "completed",
    include_prediction: bool = True,
) -> tuple[Project, list[Feedback], AnalysisRun]:
    """Create Project + Feedbacks + completed classification AnalysisRun.

    Feedback content MUST match the demo dataset byte-for-byte so the
    runner's content-hash validator accepts the case mapping. When
    ``include_prediction`` is False, NO ``FeedbackAnalysis`` row is
    created for any feedback, which lets the per-case path record
    ``no_prediction`` for every dataset case.
    """
    if dataset is None:
        dataset = load_demo_dataset()

    project = _create_project(db, name="Phase6 API")
    feedbacks: list[Feedback] = []
    for case in dataset.cases:
        fb = Feedback(
            project_id=project.id,
            external_id=case.case_id,
            content=case.feedback_text,
        )
        db.add(fb)
        feedbacks.append(fb)
    db.flush()

    run = AnalysisRun(
        project_id=project.id,
        run_type="classification",
        model="phase6-api-model",
        prompt_version="classification-v1",
        status=run_status,
        input_count=len(feedbacks),
        processed_count=len(feedbacks),
        success_count=len(feedbacks) if include_prediction else 0,
        failed_count=0,
        input_tokens=120,
        output_tokens=60,
        unknown_token_calls=0,
        latency_ms=1234,
        attempt_count=1,
        retry_count=0,
    )
    db.add(run)
    db.flush()

    if include_prediction:
        for case in dataset.cases:
            fb = next(f for f in feedbacks if f.external_id == case.case_id)
            predicted_type = case.feedback_type_label or "bug"
            predicted_severity = case.severity_label or 3
            db.add(
                FeedbackAnalysis(
                    feedback_id=fb.id,
                    run_id=run.id,
                    feedback_type=predicted_type,
                    module="m",
                    intent="i",
                    pain_point="p",
                    sentiment="negative",
                    severity=predicted_severity,
                    confidence=0.9,
                )
            )
    db.commit()
    db.refresh(project)
    return project, feedbacks, run


def _seed_cluster_fixture(
    db: Session,
    *,
    dataset=None,
    run_status: str = "completed",
) -> tuple[Project, list[Feedback], AnalysisRun, list[Cluster]]:
    """Create Project + Feedbacks + classification run + completed cluster run.

    All feedbacks end up under a single predicted cluster so the
    ``cluster_purity_micro`` metric hits ``1.0`` deterministically.
    """
    if dataset is None:
        dataset = load_demo_dataset()

    project = _create_project(db, name="Phase6 API cluster")
    feedbacks: list[Feedback] = []
    for case in dataset.cases:
        fb = Feedback(
            project_id=project.id,
            external_id=case.case_id,
            content=case.feedback_text,
        )
        db.add(fb)
        feedbacks.append(fb)
    db.flush()

    class_run = AnalysisRun(
        project_id=project.id,
        run_type="classification",
        model="phase6-api-model",
        prompt_version="classification-v1",
        status="completed",
        input_count=len(feedbacks),
        processed_count=len(feedbacks),
        success_count=len(feedbacks),
    )
    db.add(class_run)
    db.flush()

    analyses: list[FeedbackAnalysis] = []
    for fb in feedbacks:
        analysis = FeedbackAnalysis(
            feedback_id=fb.id,
            run_id=class_run.id,
            feedback_type="bug",
            module="m",
            intent="i",
            pain_point="p",
            sentiment="negative",
            severity=3,
            confidence=0.9,
        )
        db.add(analysis)
        analyses.append(analysis)
    db.flush()

    run = AnalysisRun(
        project_id=project.id,
        run_type="clustering",
        model="phase6-api-cluster",
        prompt_version="cluster-review-v4",
        status=run_status,
        input_count=len(feedbacks),
        processed_count=len(feedbacks),
        success_count=len(feedbacks),
        input_tokens=200,
        output_tokens=80,
        unknown_token_calls=0,
        latency_ms=5000,
        attempt_count=1,
        cluster_config={"embedding_model": "local-hash-v1"},
    )
    db.add(run)
    db.flush()

    cluster = Cluster(
        project_id=project.id,
        run_id=run.id,
        embedding_model="local-hash-v1",
        distance_threshold=0.5,
        linkage="average",
    )
    db.add(cluster)
    db.flush()

    for analysis in analyses:
        db.add(
            ClusterMember(
                cluster_id=cluster.id,
                feedback_analysis_id=analysis.id,
                similarity_score=0.9,
            )
        )
    db.commit()
    db.refresh(project)
    return project, feedbacks, run, [cluster]


# ---- Happy paths ------------------------------------------------------------


def test_post_classification_returns_run_row(client: TestClient, SessionLocal):
    db = SessionLocal()
    try:
        project, feedbacks, run = _seed_classification_fixture(db)
        project_id = str(project.id)
        run_id = str(run.id)
    finally:
        db.close()

    response = client.post(
        "/evaluations",
        json={
            "project_id": project_id,
            "source_run_id": run_id,
            "evaluation_type": "classification_metrics",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] in {"completed", "partial_failed", "failed"}
    assert body["evaluation_type"] == "classification_metrics"
    assert body["dataset_label_status"] == "synthetic_reference_labels_unreviewed_no_human_review"
    assert body["dataset_source"] == "demo_synthetic_unreviewed"
    assert body["dataset_provenance"] == "demo"
    assert body["dataset_split"] == "full"
    assert body["calculation_version"].startswith("phase6-")
    assert body["id"]
    assert body["project_id"] == project_id


def test_post_cluster_returns_run_row(client: TestClient, SessionLocal):
    db = SessionLocal()
    try:
        project, feedbacks, run, clusters = _seed_cluster_fixture(db)
        project_id = str(project.id)
        run_id = str(run.id)
    finally:
        db.close()

    response = client.post(
        "/evaluations",
        json={
            "project_id": project_id,
            "source_run_id": run_id,
            "evaluation_type": "cluster_metrics",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["evaluation_type"] == "cluster_metrics"
    assert body["stage"] == "clustering"
    assert body["dataset_label_status"] == "synthetic_reference_labels_unreviewed_no_human_review"


def test_get_evaluation_returns_full_row(client: TestClient, SessionLocal):
    db = SessionLocal()
    try:
        project, feedbacks, run = _seed_classification_fixture(db)
        project_id = str(project.id)
        run_id = str(run.id)
    finally:
        db.close()

    create = client.post(
        "/evaluations",
        json={
            "project_id": project_id,
            "source_run_id": run_id,
            "evaluation_type": "classification_metrics",
        },
    )
    assert create.status_code == 200, create.text
    eval_id = create.json()["id"]

    response = client.get(f"/evaluations/{eval_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == eval_id
    assert body["project_id"] == project_id
    assert body["evaluation_type"] == "classification_metrics"
    assert body["status"] in {"completed", "partial_failed"}
    assert body["input_count"] >= 1
    assert body["calculation_version"].startswith("phase6-")
    assert body["dataset_label_status"] == "synthetic_reference_labels_unreviewed_no_human_review"
    assert body["dataset_provenance"] == "demo"
    assert body["dataset_split"] == "full"
    # input_tokens / output_tokens may be int | None (NULL semantics);
    # the API must NOT have collapsed unknown to 0
    assert body["input_tokens"] in (None, 120)
    assert body["output_tokens"] in (None, 60)


# ---- Metric endpoint --------------------------------------------------------


def test_metrics_endpoint_returns_metric_kind_sample_count_value(client: TestClient, SessionLocal):
    db = SessionLocal()
    try:
        project, feedbacks, run = _seed_classification_fixture(db)
        project_id = str(project.id)
        run_id = str(run.id)
    finally:
        db.close()

    create = client.post(
        "/evaluations",
        json={
            "project_id": project_id,
            "source_run_id": run_id,
            "evaluation_type": "classification_metrics",
        },
    )
    assert create.status_code == 200, create.text
    eval_id = create.json()["id"]

    response = client.get(f"/evaluations/{eval_id}/metrics")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] >= 1
    assert 1 <= len(body["items"]) <= body["total"]
    metric_names = {item["metric_name"] for item in body["items"]}
    # The two classification-side accuracy metrics must survive the
    # pipeline layer addition; the two new pipeline metrics must
    # surface too — the brief mandates both classes coexist on the
    # run so the dashboard can distinguish "label quality" from
    # "source pipeline execution".
    assert {"feedback_type_accuracy", "severity_exact_match"}.issubset(metric_names)
    assert {"pipeline_success_rate", "pipeline_failure_stage_count"}.issubset(metric_names)
    classification_metric_names = {
        "feedback_type_accuracy",
        "feedback_type_macro_f1",
        "severity_exact_match",
        "severity_mae",
        "intent_exact_match",
    }
    pipeline_metric_names = {
        "pipeline_success_rate",
        "pipeline_failure_stage_count",
    }
    items_by_name = {item["metric_name"]: item for item in body["items"]}
    pipeline_success = items_by_name["pipeline_success_rate"]
    assert pipeline_success["stage"] == "pipeline"
    assert pipeline_success["metric_kind"] == "deterministic"
    assert pipeline_success["sample_count"] == len(load_demo_dataset().cases)
    assert pipeline_success["value"] == pytest.approx(1.0)
    # The details payload carries the provenance string mandated by
    # the brief so a downstream reader can confirm the metric is
    # sourced from the source-pipeline counters, NOT from per-label
    # accuracy. Api surface MUST keep details JSON-only — never
    # expose raw feedback text.
    success_details = dict(pipeline_success["details"])
    assert success_details["definition"] == "source_pipeline_execution_not_label_accuracy"
    assert success_details["source_run_id"] == run_id
    assert success_details["source_status"] == "completed"
    assert success_details["success_count"] == pipeline_success["sample_count"]
    assert success_details["failed_count"] == 0
    pipeline_failure = items_by_name["pipeline_failure_stage_count"]
    assert pipeline_failure["stage"] == "pipeline"
    assert pipeline_failure["metric_kind"] == "deterministic"
    assert pipeline_failure["sample_count"] == len(load_demo_dataset().cases)
    assert pipeline_failure["value"] == pytest.approx(0.0)
    failure_details = dict(pipeline_failure["details"])
    assert failure_details["failure_stage_label"] == "classification"
    assert failure_details["source_run_id"] == run_id
    for item in body["items"]:
        assert item["metric_kind"] == "deterministic"
        assert isinstance(item["sample_count"], int) and item["sample_count"] >= 0
        if item["value"] is None:
            assert item["value_display"] == "NOT MEASURED"
        else:
            assert item["value_display"] != "NOT MEASURED"
            assert isinstance(item["value"], (int, float))
        if item["metric_name"] in pipeline_metric_names:
            # New pipeline-level rows live under their own stage so
            # the dashboard does not confuse them with the original
            # per-label metrics. The code below guarantees every
            # other row stays on the originating analysis stage.
            assert item["stage"] == "pipeline"
        else:
            assert item["metric_name"] in classification_metric_names
            assert item["stage"] == "classification"
        assert item["run_id"] == eval_id


def test_metrics_pagination_total_limit_offset(client: TestClient, SessionLocal):
    db = SessionLocal()
    try:
        project, feedbacks, run = _seed_classification_fixture(db)
        project_id = str(project.id)
        run_id = str(run.id)
    finally:
        db.close()

    create = client.post(
        "/evaluations",
        json={
            "project_id": project_id,
            "source_run_id": run_id,
            "evaluation_type": "classification_metrics",
        },
    )
    eval_id = create.json()["id"]

    page1 = client.get(f"/evaluations/{eval_id}/metrics?limit=1&offset=0").json()
    assert page1["limit"] == 1
    assert page1["offset"] == 0
    assert len(page1["items"]) == 1
    assert page1["total"] >= 2
    page2 = client.get(f"/evaluations/{eval_id}/metrics?limit=1&offset=1").json()
    assert page2["offset"] == 1
    assert page2["items"][0]["metric_name"] != page1["items"][0]["metric_name"]


def test_metrics_limit_out_of_range_is_422(client: TestClient, SessionLocal):
    db = SessionLocal()
    try:
        project, feedbacks, run = _seed_classification_fixture(db)
        project_id = str(project.id)
        run_id = str(run.id)
    finally:
        db.close()

    create = client.post(
        "/evaluations",
        json={
            "project_id": project_id,
            "source_run_id": run_id,
            "evaluation_type": "classification_metrics",
        },
    )
    eval_id = create.json()["id"]
    bad = client.get(f"/evaluations/{eval_id}/metrics?limit=0")
    assert bad.status_code == 422


def test_intent_metric_value_is_null_with_not_measured_display(client: TestClient, SessionLocal):
    """The classification runner reports intent_exact_match as NOT_MEASURED.

    The demo dataset has no controlled intent vocabulary, so the
    intent metric must come back with ``value=None`` and
    ``value_display == "NOT MEASURED"``.
    """
    db = SessionLocal()
    try:
        project, feedbacks, run = _seed_classification_fixture(db)
        project_id = str(project.id)
        run_id = str(run.id)
    finally:
        db.close()

    create = client.post(
        "/evaluations",
        json={
            "project_id": project_id,
            "source_run_id": run_id,
            "evaluation_type": "classification_metrics",
        },
    )
    eval_id = create.json()["id"]
    metrics = client.get(f"/evaluations/{eval_id}/metrics").json()
    intent = next(
        (m for m in metrics["items"] if m["metric_name"] == "intent_exact_match"),
        None,
    )
    assert intent is not None, "intent_exact_match should be present"
    assert intent["value"] is None
    assert intent["value_display"] == "NOT MEASURED"


# ---- Cases endpoint ---------------------------------------------------------


def test_cases_endpoint_returns_hashes_only_no_raw_text(client: TestClient, SessionLocal):
    db = SessionLocal()
    try:
        project, feedbacks, run = _seed_classification_fixture(db)
        project_id = str(project.id)
        run_id = str(run.id)
    finally:
        db.close()

    create = client.post(
        "/evaluations",
        json={
            "project_id": project_id,
            "source_run_id": run_id,
            "evaluation_type": "classification_metrics",
        },
    )
    eval_id = create.json()["id"]

    response = client.get(f"/evaluations/{eval_id}/cases")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] >= 1
    payload_text = response.text
    item = body["items"][0]
    assert set(item.keys()) >= {
        "id",
        "run_id",
        "project_id",
        "case_id",
        "stage",
        "input_hash",
        "expected_hash",
        "actual_hash",
        "passed",
        "error_type",
        "eval_source",
        "notes",
        "created_at",
        "failed_stage",
    }
    assert "input" not in item
    assert "expected" not in item
    assert "actual" not in item
    assert item["input_hash"] and len(item["input_hash"]) == 64
    # Raw demo feedback text MUST NOT appear anywhere in the payload.
    for case in load_demo_dataset().cases:
        assert case.feedback_text not in payload_text


def test_cases_no_prediction_records_error_type(client: TestClient, SessionLocal):
    db = SessionLocal()
    try:
        project, feedbacks, run = _seed_classification_fixture(db, include_prediction=False)
        project_id = str(project.id)
        run_id = str(run.id)
    finally:
        db.close()

    create = client.post(
        "/evaluations",
        json={
            "project_id": project_id,
            "source_run_id": run_id,
            "evaluation_type": "classification_metrics",
        },
    )
    eval_id = create.json()["id"]
    cases = client.get(f"/evaluations/{eval_id}/cases?limit=200").json()
    assert cases["total"] >= 1
    for item in cases["items"]:
        assert item["error_type"] == "no_prediction"
        assert item["passed"] is False
        assert item["failed_stage"] == "classification"


def test_cases_pagination(client: TestClient, SessionLocal):
    db = SessionLocal()
    try:
        project, feedbacks, run = _seed_classification_fixture(db)
        project_id = str(project.id)
        run_id = str(run.id)
    finally:
        db.close()

    create = client.post(
        "/evaluations",
        json={
            "project_id": project_id,
            "source_run_id": run_id,
            "evaluation_type": "classification_metrics",
        },
    )
    eval_id = create.json()["id"]
    page1 = client.get(f"/evaluations/{eval_id}/cases?limit=2&offset=0").json()
    assert page1["limit"] == 2 and page1["offset"] == 0
    page2 = client.get(f"/evaluations/{eval_id}/cases?limit=2&offset=2").json()
    p1_ids = {i["case_id"] for i in page1["items"]}
    p2_ids = {i["case_id"] for i in page2["items"]}
    assert p1_ids.isdisjoint(p2_ids)


# ---- 404 paths --------------------------------------------------------------


def test_get_unknown_run_returns_404(client: TestClient):
    response = client.get(f"/evaluations/{uuid4()}")
    assert response.status_code == 404


def test_get_metrics_unknown_run_returns_404(client: TestClient):
    response = client.get(f"/evaluations/{uuid4()}/metrics")
    assert response.status_code == 404


def test_get_cases_unknown_run_returns_404(client: TestClient):
    response = client.get(f"/evaluations/{uuid4()}/cases")
    assert response.status_code == 404


def test_get_with_illegal_uuid_returns_422(client: TestClient):
    bad = client.get("/evaluations/not-a-uuid")
    assert bad.status_code == 422


# ---- 422 paths --------------------------------------------------------------


def test_post_extra_field_rejected(client: TestClient, SessionLocal):
    db = SessionLocal()
    try:
        project, feedbacks, run = _seed_classification_fixture(db)
        project_id = str(project.id)
        run_id = str(run.id)
    finally:
        db.close()

    response = client.post(
        "/evaluations",
        json={
            "project_id": project_id,
            "source_run_id": run_id,
            "evaluation_type": "classification_metrics",
            "store_raw": True,
        },
    )
    assert response.status_code == 422


def test_post_invalid_evaluation_type_rejected(client: TestClient, SessionLocal):
    db = SessionLocal()
    try:
        project, feedbacks, run = _seed_classification_fixture(db)
        project_id = str(project.id)
        run_id = str(run.id)
    finally:
        db.close()

    response = client.post(
        "/evaluations",
        json={
            "project_id": project_id,
            "source_run_id": run_id,
            "evaluation_type": "pipeline_health",
        },
    )
    assert response.status_code == 422


def test_post_missing_required_field_rejected(client: TestClient):
    response = client.post(
        "/evaluations",
        json={"project_id": str(uuid4()), "source_run_id": str(uuid4())},
    )
    assert response.status_code == 422


def test_post_unknown_project_returns_404(client: TestClient):
    response = client.post(
        "/evaluations",
        json={
            "project_id": str(uuid4()),
            "source_run_id": str(uuid4()),
            "evaluation_type": "classification_metrics",
        },
    )
    assert response.status_code == 404


# ---- 409 paths --------------------------------------------------------------


def test_post_wrong_run_type_returns_409(client: TestClient, SessionLocal):
    """Use the cluster run id with classification_metrics → wrong run_type."""
    db = SessionLocal()
    try:
        project, feedbacks, run, clusters = _seed_cluster_fixture(db)
        project_id = str(project.id)
        bad_run_id = str(run.id)
    finally:
        db.close()

    response = client.post(
        "/evaluations",
        json={
            "project_id": project_id,
            "source_run_id": bad_run_id,
            "evaluation_type": "classification_metrics",
        },
    )
    assert response.status_code == 409, response.text


def test_post_non_terminal_run_returns_409(client: TestClient, SessionLocal):
    db = SessionLocal()
    try:
        project, feedbacks, run = _seed_classification_fixture(db, run_status="running")
        project_id = str(project.id)
        run_id = str(run.id)
    finally:
        db.close()

    response = client.post(
        "/evaluations",
        json={
            "project_id": project_id,
            "source_run_id": run_id,
            "evaluation_type": "classification_metrics",
        },
    )
    assert response.status_code == 409, response.text


def test_post_cross_project_source_run_returns_409(client: TestClient, SessionLocal):
    """Defends against the ``跨项目恶意 source_run_id`` attack."""
    db = SessionLocal()
    try:
        project_a, _, run_a = _seed_classification_fixture(db)
        project_b = _create_project(db, name="Phase6 API B")
        project_b_id = str(project_b.id)
        run_a_id = str(run_a.id)
    finally:
        db.close()

    response = client.post(
        "/evaluations",
        json={
            "project_id": project_b_id,
            "source_run_id": run_a_id,
            "evaluation_type": "classification_metrics",
        },
    )
    assert response.status_code == 409, response.text


# ---- Cross-project GET isolation --------------------------------------------


def test_get_cross_project_returns_404(client: TestClient, SessionLocal):
    db = SessionLocal()
    try:
        project_a, _, run_a = _seed_classification_fixture(db)
        project_b = _create_project(db, name="Phase6 API B")
        a_id = str(project_a.id)
        b_id = str(project_b.id)
        run_a_id = str(run_a.id)
    finally:
        db.close()

    create = client.post(
        "/evaluations",
        json={
            "project_id": a_id,
            "source_run_id": run_a_id,
            "evaluation_type": "classification_metrics",
        },
    )
    eval_id = create.json()["id"]

    bad = client.get(f"/evaluations/{eval_id}?project_id={b_id}")
    assert bad.status_code == 404


def test_get_correct_project_returns_200(client: TestClient, SessionLocal):
    db = SessionLocal()
    try:
        project, _, run = _seed_classification_fixture(db)
        project_id = str(project.id)
        run_id = str(run.id)
    finally:
        db.close()

    create = client.post(
        "/evaluations",
        json={
            "project_id": project_id,
            "source_run_id": run_id,
            "evaluation_type": "classification_metrics",
        },
    )
    eval_id = create.json()["id"]
    ok = client.get(f"/evaluations/{eval_id}?project_id={project_id}")
    assert ok.status_code == 200


# ---- Safe response ----------------------------------------------------------


_SQL_FRAGMENTS = ["[SQL:", "[parameters", "INSERT INTO", "VALUES (", "SELECT ", "FROM projects"]
_CREDENTIAL_FRAGMENTS = ["sk-abc12345", "api_key=DEADBEEF", "Bearer eyJ"]


def test_post_409_response_does_not_echo_sql_or_credentials(
    client: TestClient, SessionLocal, monkeypatch
):
    """A source-mismatch exception carrying SQL fragments must be scrubbed.

    The API imports ``evaluate_existing_run`` into its own namespace
    via ``from app.evals.runner import evaluate_existing_run``; the
    patch must therefore target ``app.api.evaluations`` (the local
    binding), NOT ``app.evals.runner`` (which the API never reads
    from at call time).
    """
    from app.api import evaluations as api_module
    from app.evals.runner import EvaluationRunnerSourceMismatchError

    def _boom(*args, **kwargs):
        raise EvaluationRunnerSourceMismatchError(
            "(IntegrityError) CHECK constraint failed\n"
            "[SQL: INSERT INTO evaluation_runs (id) VALUES (?)]\n"
            "[parameters: ('SECRET-UUID-1',)]\n"
            "Bearer sk-abc12345xyz9876",
            errors=["the upstream blew up with [SQL: SELECT * FROM projects]"],
        )

    monkeypatch.setattr(api_module, "evaluate_existing_run", _boom)

    db = SessionLocal()
    try:
        project, _, run = _seed_classification_fixture(db)
        project_id = str(project.id)
        run_id = str(run.id)
    finally:
        db.close()

    response = client.post(
        "/evaluations",
        json={
            "project_id": project_id,
            "source_run_id": run_id,
            "evaluation_type": "classification_metrics",
        },
    )
    assert response.status_code == 409, response.text
    text = response.text
    for needle in _SQL_FRAGMENTS + _CREDENTIAL_FRAGMENTS + ["SECRET-UUID-1"]:
        assert needle not in text


def test_post_unexpected_exception_returns_500_without_echo(
    client: TestClient, SessionLocal, monkeypatch
):
    """An unexpected exception (e.g. transport failure) collapses to 500 with no detail.

    Targets the API's local binding of ``evaluate_existing_run`` (see
    :func:`test_post_409_response_does_not_echo_sql_or_credentials`).
    """
    from app.api import evaluations as api_module

    def _boom(*args, **kwargs):
        raise RuntimeError(
            "transport failed: [SQL: SELECT * FROM evaluation_runs]\n"
            "[parameters: ('SECRET-UUID-2',)]\n"
            "Bearer sk-deadbeef9999"
        )

    monkeypatch.setattr(api_module, "evaluate_existing_run", _boom)

    db = SessionLocal()
    try:
        project, _, run = _seed_classification_fixture(db)
        project_id = str(project.id)
        run_id = str(run.id)
    finally:
        db.close()

    response = client.post(
        "/evaluations",
        json={
            "project_id": project_id,
            "source_run_id": run_id,
            "evaluation_type": "classification_metrics",
        },
    )
    assert response.status_code == 500, response.text
    text = response.text
    for needle in _SQL_FRAGMENTS + _CREDENTIAL_FRAGMENTS + ["SECRET-UUID-2"]:
        assert needle not in text


def test_health_ready_includes_evaluation_tables(client: TestClient):
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
