"""Custom-project checks must not manufacture semantic scores or expand inputs."""

import csv
import io
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import AnalysisRun, Cluster, ClusterMember, Feedback, FeedbackAnalysis, Project
from app.models.evaluation import EvaluationCase, EvaluationMetric, EvaluationRun


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


def template(client, ids, **kw):
    return client.get(
        "/evaluations/reference-template",
        params={
            "project_id": str(ids["project"]),
            "source_run_id": str(ids["source"]),
            "evaluation_type": "classification_metrics",
            **kw,
        },
    )


def rows_for(client, ids):
    response = template(client, ids)
    assert response.status_code == 200, response.text
    return list(csv.DictReader(io.StringIO(response.json()["csv_text"])))


def upload(client, ids, rows, **kw):
    out = io.StringIO(newline="")
    w = csv.DictWriter(out, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
    return client.post(
        "/evaluations/reference-review",
        json={
            "project_id": str(ids["project"]),
            "source_run_id": str(ids["source"]),
            "evaluation_type": "classification_metrics",
            "csv_text": out.getvalue(),
            **kw,
        },
    )


def reviewed(rows):
    for row in rows:
        row.update(
            review_status="reviewed", reviewer="人工审核者", feedback_type="bug", severity="3"
        )
    return rows


def test_template_is_blank_frozen_and_no_predictions(fixture):
    client, _, ids = fixture
    rows = rows_for(client, ids)
    assert {r["feedback_id"] for r in rows} == {"SYN-0", "SYN-1"}
    assert all(r["review_status"] == "pending" and not r["reviewer"] for r in rows)
    assert all(not r["feedback_type"] and not r["severity"] for r in rows)
    assert all(len(r["content_hash"]) == 64 for r in rows)


def test_labels_scores_snapshot_and_subset_denominators(fixture):
    client, factory, ids = fixture
    rows = reviewed(rows_for(client, ids))[:1]
    rows[0]["feedback_type"] = "feature_request"
    result = upload(client, ids, rows)
    assert result.status_code == 200, result.text
    run = result.json()
    assert run["input_count"] == 1
    assert run["failed_count"] == 1
    assert run["dataset_label_status"] == "user_reviewed_reference_not_formal_acceptance"
    metrics = client.get(f"/evaluations/{run['id']}/metrics").json()["items"]
    acc = next(m for m in metrics if m["metric_name"] == "feedback_type_accuracy")
    assert acc["value"] == 0 and acc["sample_count"] == 1
    assert acc["details"]["source_input_count"] == 2
    assert acc["details"]["reviewed_count"] == 1
    detail = client.get(
        f"/evaluations/{run['id']}/reference-review", params={"project_id": str(ids["project"])}
    ).json()
    assert detail["items"][0]["expected"]["feedback_type"] == "feature_request"
    assert detail["items"][0]["actual"]["feedback_type"] == "bug"
    assert detail["items"][0]["reviewer"] == "人工审核者"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Feedback)) == 3
        case = db.scalar(
            select(EvaluationCase).where(
                EvaluationCase.run_id == __import__("uuid").UUID(run["id"])
            )
        )
        assert case.input is None
        assert json.loads(case.expected)["feedback_type"] == "feature_request"
    rows[0]["feedback_type"] = "bug"
    second = upload(client, ids, rows).json()
    assert second["dataset_version"] != run["dataset_version"]
    assert client.get(f"/evaluations/{run['id']}").json()["failed_count"] == 1


@pytest.mark.parametrize(
    "change",
    [
        "pending",
        "reviewer",
        "invalid_type",
        "invalid_severity",
        "duplicate",
        "unknown",
        "later",
        "hash",
        "project",
        "run",
        "content",
    ],
)
def test_invalid_upload_no_writes(fixture, change):
    client, factory, ids = fixture
    rows = reviewed(rows_for(client, ids))
    if change == "pending":
        rows[0]["review_status"] = "pending"
    if change == "reviewer":
        rows[0]["reviewer"] = ""
    if change == "invalid_type":
        rows[0]["feedback_type"] = "invented"
    if change == "invalid_severity":
        rows[0]["severity"] = "3.5"
    if change == "duplicate":
        rows.append(rows[0])
    if change == "unknown":
        rows[0]["feedback_id"] = "missing"
    if change == "later":
        rows[0]["feedback_id"] = "SYN-2"
    if change == "hash":
        rows[0]["content_hash"] = "0" * 64
    if change == "project":
        rows[0]["project_id"] = str(ids["other"])
    if change == "run":
        rows[0]["source_run_id"] = str(ids["other"])
    if change == "content":
        rows[0]["content"] = "改过的原文"
    response = upload(client, ids, rows)
    assert response.status_code in (409, 422), response.text
    with factory() as db:
        for model in (EvaluationRun, EvaluationCase, EvaluationMetric):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_cross_project_and_running_template(fixture):
    client, factory, ids = fixture
    assert template(client, ids, project_id=str(ids["other"])).status_code == 404
    with factory() as db:
        db.get(AnalysisRun, ids["source"]).status = "running"
        db.commit()
    assert template(client, ids).status_code == 409


def test_formula_escape_and_input_drift(fixture):
    client, factory, ids = fixture
    with factory() as db:
        fb = db.scalar(select(Feedback).where(Feedback.external_id == "SYN-0"))
        fb.content = '=HYPERLINK("https://example.test")'
        db.commit()
    rows = reviewed(rows_for(client, ids))
    assert rows[0]["content"].startswith("'")
    response = upload(client, ids, rows)
    assert response.status_code == 200, response.text
    with factory() as db:
        fb = db.scalar(select(Feedback).where(Feedback.external_id == "SYN-0"))
        fb.content = "changed"
        db.commit()
    assert upload(client, ids, rows).status_code == 409


def test_report_read_project_isolation(fixture):
    client, _, ids = fixture
    report = upload(client, ids, reviewed(rows_for(client, ids))).json()
    assert (
        client.get(
            f"/evaluations/{report['id']}/reference-review",
            params={"project_id": str(ids["other"])},
        ).status_code
        == 404
    )


def cluster_source(factory, ids):
    with factory() as db:
        run = AnalysisRun(
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
        db.add(run)
        db.flush()
        c = Cluster(
            project_id=ids["project"],
            run_id=run.id,
            embedding_model="local-hash-v1",
            distance_threshold=0.45,
            linkage="average",
            feedback_count=2,
        )
        db.add(c)
        db.flush()
        for a in db.scalars(
            select(FeedbackAnalysis).where(FeedbackAnalysis.run_id == ids["source"])
        ).all():
            db.add(ClusterMember(cluster_id=c.id, feedback_analysis_id=a.id, similarity_score=0.8))
        db.commit()
        return run.id, c.id


def test_cluster_reference_math_and_source_protection(fixture):
    client, factory, ids = fixture
    run_id, cid = cluster_source(factory, ids)
    cid_args = {"source_run_id": str(run_id), "evaluation_type": "cluster_metrics"}
    response = template(client, ids, **cid_args)
    rows = list(csv.DictReader(io.StringIO(response.json()["csv_text"])))
    for i, r in enumerate(rows):
        r.update(reviewer="人工", review_status="reviewed", cluster_label=f"topic-{i}")
    response = upload(client, ids, rows, **cid_args)
    assert response.status_code == 200, response.text
    report = response.json()
    metrics = client.get(f"/evaluations/{report['id']}/metrics").json()["items"]
    assert next(m["value"] for m in metrics if m["metric_name"] == "cluster_purity_micro") == 0.5
    assert report["model"] == "local-hash-v1"
    assert report["success_count"] == 2  # membership only, not semantic quality
    with factory() as db:
        db.get(Cluster, cid).project_id = ids["other"]
        db.commit()
    assert upload(client, ids, rows, **cid_args).status_code == 409


def test_bad_snapshot_empty_missing_results_and_rollback(fixture, monkeypatch):
    client, factory, ids = fixture
    rows = reviewed(rows_for(client, ids))
    with factory() as db:
        db.delete(db.get(FeedbackAnalysis, ids["analysis"]))
        db.commit()
    response = upload(client, ids, rows)
    assert response.status_code == 200, response.text
    assert response.json()["failed_count"] == 1
    with factory() as db:
        db.get(AnalysisRun, ids["source"]).input_feedback_external_ids = None
        db.commit()
    assert template(client, ids).status_code == 409


def test_commit_failure_rolls_back_all_review_tables(fixture, monkeypatch):
    from sqlalchemy.orm import Session

    from app.evals.reference_review import evaluate_review

    client, factory, ids = fixture
    rows = reviewed(rows_for(client, ids))
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)

    def fail(self):
        raise RuntimeError("controlled commit failure")

    monkeypatch.setattr(Session, "commit", fail)
    with factory() as db:
        with pytest.raises(RuntimeError):
            evaluate_review(
                db,
                project_id=ids["project"],
                source_run_id=ids["source"],
                evaluation_type="classification_metrics",
                csv_text=output.getvalue(),
            )
    with factory() as db:
        for model in (EvaluationRun, EvaluationCase, EvaluationMetric):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def cluster_run_with_unreviewed_clusters(factory, ids, *, labelled_members=("SYN-0", "SYN-1")):
    """已结束的聚类Run：可选的混合簇 + 2个仅含未复核反馈的簇。

    模板会列出全部4条输入；人工只复核 ``labelled_members`` 里的反馈，其余簇
    没有已复核成员。``labelled_members`` 为空时，被复核的两条反馈没有任何
    簇归属（结果缺失）。
    """
    with factory() as db:
        db.add(Feedback(project_id=ids["project"], external_id="SYN-3", content="模拟反馈3"))
        db.flush()
        upstream = db.get(AnalysisRun, ids["source"])
        upstream.input_feedback_external_ids = ["SYN-0", "SYN-1", "SYN-2", "SYN-3"]
        upstream.input_count = 4
        upstream.processed_count = 4
        upstream.success_count = 4
        for fb in db.scalars(select(Feedback).where(Feedback.project_id == ids["project"])).all():
            if fb.external_id in {"SYN-2", "SYN-3"}:
                db.add(
                    FeedbackAnalysis(
                        feedback_id=fb.id,
                        run_id=upstream.id,
                        feedback_type="bug",
                        module="upload",
                        intent="修复",
                        pain_point="失败",
                        sentiment="negative",
                        severity=3,
                        confidence=0.8,
                    )
                )
        db.flush()
        analysis_by_external_id = {}
        for a in db.scalars(
            select(FeedbackAnalysis).where(FeedbackAnalysis.run_id == upstream.id)
        ).all():
            analysis_by_external_id[db.get(Feedback, a.feedback_id).external_id] = a
        run = AnalysisRun(
            project_id=ids["project"],
            run_type="clustering",
            status="completed",
            prompt_version="test",
            model="local-hash-v1",
            input_count=4,
            processed_count=4,
            success_count=4,
            input_feedback_external_ids=["SYN-0", "SYN-1", "SYN-2", "SYN-3"],
            cluster_config={"classification_run_id": str(upstream.id)},
        )
        db.add(run)
        db.flush()
        planned = []
        if labelled_members:
            planned.append(("mixed", list(labelled_members)))
        planned.append(("unreviewed-1", ["SYN-2"]))
        planned.append(("unreviewed-2", ["SYN-3"]))
        for name, members in planned:
            cluster = Cluster(
                project_id=ids["project"],
                run_id=run.id,
                name=name,
                embedding_model="local-hash-v1",
                distance_threshold=0.45,
                linkage="average",
                feedback_count=len(members),
            )
            db.add(cluster)
            db.flush()
            for external_id in members:
                db.add(
                    ClusterMember(
                        cluster_id=cluster.id,
                        feedback_analysis_id=analysis_by_external_id[external_id].id,
                        similarity_score=0.8,
                    )
                )
        db.commit()
        return run.id


def reviewed_cluster_rows(client, ids, run_id, labels):
    """只上传人工复核过的行，其余簇的反馈保持未复核状态。"""
    args = {"source_run_id": str(run_id), "evaluation_type": "cluster_metrics"}
    response = template(client, ids, **args)
    assert response.status_code == 200, response.text
    rows = list(csv.DictReader(io.StringIO(response.json()["csv_text"])))
    assert {r["feedback_id"] for r in rows} == {"SYN-0", "SYN-1", "SYN-2", "SYN-3"}
    picked = []
    for row in rows:
        if row["feedback_id"] not in labels:
            continue
        row.update(review_status="reviewed", reviewer="人工审核者")
        row["cluster_label"] = labels[row["feedback_id"]]
        picked.append(row)
    assert len(picked) == len(labels)
    return picked, args


def test_cluster_over_merge_ratio_ignores_clusters_without_reviewed_members(fixture):
    """过合并率的分母只能是含已复核成员的簇，未复核簇不得稀释比值。"""
    client, factory, ids = fixture
    run_id = cluster_run_with_unreviewed_clusters(factory, ids)
    rows, args = reviewed_cluster_rows(
        client, ids, run_id, {"SYN-0": "topic-A", "SYN-1": "topic-B"}
    )
    result = upload(client, ids, rows, **args)
    assert result.status_code == 200, result.text
    report = result.json()
    assert report["input_count"] == 2
    metrics = client.get(f"/evaluations/{report['id']}/metrics").json()["items"]
    over_merge = next(m for m in metrics if m["metric_name"] == "cluster_over_merge_ratio")
    assert over_merge["value"] == 1.0, (
        "过合并率分母混入了无已复核成员的簇（当前按全部3个簇计算，期望仅按含已复核成员的簇）"
    )
    purity = next(m for m in metrics if m["metric_name"] == "cluster_purity_micro")
    assert purity["value"] == 0.5
    assert purity["sample_count"] == 2
    assert over_merge["sample_count"] == 1
    assert over_merge["details"]["labelled_predicted_clusters"] == 1
    assert over_merge["details"]["total_predicted_clusters"] == 3
    assert over_merge["details"]["reviewed_count"] == 2
    assert over_merge["details"]["source_input_count"] == 4
    cluster_count = next(m for m in metrics if m["metric_name"] == "cluster_count")
    assert cluster_count["value"] == 3
    assert cluster_count["sample_count"] == 3
    assert cluster_count["details"]["total_predicted_clusters"] == 3
    assert cluster_count["details"]["labelled_predicted_clusters"] == 1


def test_cluster_over_merge_ratio_zero_when_reviewed_clusters_are_pure(fixture):
    """所有已标注簇都纯净时过合并率为0，避免用固定值或全拒绝蒙混过关。"""
    client, factory, ids = fixture
    run_id = cluster_run_with_unreviewed_clusters(factory, ids)
    rows, args = reviewed_cluster_rows(
        client, ids, run_id, {"SYN-0": "topic-A", "SYN-1": "topic-A"}
    )
    result = upload(client, ids, rows, **args)
    assert result.status_code == 200, result.text
    report = result.json()
    metrics = client.get(f"/evaluations/{report['id']}/metrics").json()["items"]
    over_merge = next(m for m in metrics if m["metric_name"] == "cluster_over_merge_ratio")
    assert over_merge["value"] == 0.0
    assert over_merge["sample_count"] == 1
    assert over_merge["details"]["labelled_predicted_clusters"] == 1
    assert over_merge["details"]["total_predicted_clusters"] == 3
    purity = next(m for m in metrics if m["metric_name"] == "cluster_purity_micro")
    assert purity["value"] == 1.0
    assert purity["sample_count"] == 2
    assert over_merge["details"]["reviewed_count"] == 2
    assert over_merge["details"]["source_input_count"] == 4


def test_cluster_over_merge_ratio_null_when_no_reviewed_prediction(fixture):
    """被复核的反馈没有预测结果时，过合并率必须是NULL而不是假0。"""
    client, factory, ids = fixture
    run_id = cluster_run_with_unreviewed_clusters(factory, ids, labelled_members=())
    rows, args = reviewed_cluster_rows(
        client, ids, run_id, {"SYN-0": "topic-A", "SYN-1": "topic-B"}
    )
    result = upload(client, ids, rows, **args)
    assert result.status_code == 200, result.text
    report = result.json()
    assert report["failed_count"] == 2
    assert report["status"] == "partial_failed"
    metrics = client.get(f"/evaluations/{report['id']}/metrics").json()["items"]
    over_merge = next(m for m in metrics if m["metric_name"] == "cluster_over_merge_ratio")
    assert over_merge["value"] is None
    assert over_merge["sample_count"] == 0
    assert over_merge["details"]["labelled_predicted_clusters"] == 0
    assert over_merge["details"]["total_predicted_clusters"] == 2
    assert over_merge["details"]["reviewed_count"] == 2
    assert over_merge["details"]["source_input_count"] == 4
    purity = next(m for m in metrics if m["metric_name"] == "cluster_purity_micro")
    assert purity["value"] is None
    assert purity["sample_count"] == 0
    cluster_count = next(m for m in metrics if m["metric_name"] == "cluster_count")
    assert cluster_count["value"] > 0
    assert cluster_count["value"] == 2
    assert cluster_count["details"]["total_predicted_clusters"] == 2
    assert cluster_count["details"]["labelled_predicted_clusters"] == 0


def test_reordered_rows_same_version_and_strict_columns(fixture):
    client, _, ids = fixture
    rows = reviewed(rows_for(client, ids))
    first = upload(client, ids, rows).json()
    second = upload(client, ids, list(reversed(rows))).json()
    assert first["dataset_version"] == second["dataset_version"]
    rows[0]["unknown"] = "extra"
    rows[1]["unknown"] = "extra"
    assert upload(client, ids, rows).status_code == 422
