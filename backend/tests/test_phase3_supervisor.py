"""Independent Phase 3 acceptance regressions owned by Codex."""

import os

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.config import settings
from app.models import AnalysisRun, Cluster, ClusterMember, Feedback, FeedbackAnalysis, Project
from app.models.insight import Insight, InsightEvidence
from app.services import insight_runner as runner
from app.services.insight_client import InsightLLMResult, InsightLLMTransportError, InsightLLMUsage
from app.services.insight_service import InsightValidationError, validate_insight_payload


def payload(ids):
    return dict(
        title="多图选择",
        pain_point="反复选择图片",
        affected_segment="未知",
        insight="这些反馈反映逐张选图带来的重复操作。",
        recommendation="验证多选方案。",
        confidence=0.8,
        evidence=[
            dict(feedback_id=x, relevance_score=0.9, evidence_type="representative") for x in ids
        ],
    )


@pytest.fixture
def db(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL", "sqlite://")
    engine = create_engine(
        url,
        **(
            {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
            if url.startswith("sqlite")
            else {}
        ),
    )
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def foreign_keys(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    for key, value in [
        ("llm_model", "test"),
        ("llm_api_key", "test"),
        ("llm_base_url", "http://invalid"),
    ]:
        monkeypatch.setattr(settings, key, value)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    engine.dispose()


def seed(db, sizes=(3, 3)):
    p = Project(name="Phase3 supervisor", product_name="test")
    db.add(p)
    db.flush()
    a = AnalysisRun(
        project_id=p.id,
        run_type="classification",
        model="test",
        prompt_version="test",
        status="completed",
        input_count=sum(sizes),
        processed_count=sum(sizes),
        success_count=sum(sizes),
    )
    db.add(a)
    db.flush()
    r = AnalysisRun(
        project_id=p.id,
        run_type="clustering",
        model="test",
        prompt_version="test",
        status="completed",
        input_count=sum(sizes),
        processed_count=sum(sizes),
        success_count=sum(sizes),
        cluster_config={"classification_run_id": str(a.id)},
    )
    db.add(r)
    db.flush()
    clusters = []
    for i, size in enumerate(sizes):
        c = Cluster(
            project_id=p.id,
            run_id=r.id,
            source="llm_review",
            feedback_count=size,
            embedding_model="synthetic-test",
            distance_threshold=0.3,
            linkage="average",
        )
        db.add(c)
        db.flush()
        clusters.append(c)
        for j in range(size):
            f = Feedback(
                project_id=p.id, external_id=f"{i}-{j}", content=f"选择第{j}张图片需要重复操作。"
            )
            db.add(f)
            db.flush()
            arow = FeedbackAnalysis(
                feedback_id=f.id,
                run_id=a.id,
                feedback_type="ux_issue",
                module="upload",
                intent="multiple_images",
                pain_point="repeated selection",
                sentiment="negative",
                severity=3,
                confidence=0.8,
            )
            db.add(arow)
            db.flush()
            db.add(
                ClusterMember(cluster_id=c.id, feedback_analysis_id=arow.id, similarity_score=0.9)
            )
    db.commit()
    return p, a, r, clusters


class Client:
    calls = []
    fail_cluster = None

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def generate_insight(self, cluster):
        self.calls.append(cluster)
        if cluster["cluster_id"] == self.fail_cluster:
            raise InsightLLMTransportError("CUSTOM_PRIVATE_SENTINEL")
        return InsightLLMResult(
            payload([x["feedback_id"] for x in cluster["evidence_candidates"]]),
            InsightLLMUsage(11, 7),
        )


def client(monkeypatch):
    Client.calls = []
    Client.fail_cluster = None
    monkeypatch.setattr(runner, "InsightClient", Client)
    return Client


@pytest.mark.parametrize("where", ["top", "evidence"])
def test_unknown_fields_rejected(where):
    data = payload(["a", "b", "c"])
    (data if where == "top" else data["evidence"][0])["quote"] = "fabricated quotation"
    with pytest.raises(InsightValidationError):
        validate_insight_payload(data, allowed_feedback_ids=["a", "b", "c"])


def test_happy_path_and_raw_feedback_ids(db, monkeypatch):
    p, _, source, _ = seed(db, (3,))
    stub = client(monkeypatch)
    raw = {str(f.id): f.content for f in db.scalars(select(Feedback))}
    run = runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    assert run.status == "completed"
    assert run.success_count == run.processed_count == 1
    assert len(stub.calls) == 1
    assert {e["feedback_id"] for e in stub.calls[0]["evidence_candidates"]} == set(raw)
    assert {str(f.id): f.content for f in db.scalars(select(Feedback))} == raw
    assert len(db.scalars(select(InsightEvidence)).all()) == 3


def test_retry_preserves_success_and_only_retries_failed(db, monkeypatch):
    p, _, source, clusters = seed(db)
    stub = client(monkeypatch)
    stub.fail_cluster = str(clusters[-1].id)
    run = runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    assert run.status == "partial_failed"
    original = db.scalars(select(Insight)).one()
    snapshot = (original.id, original.insight, original.created_at)
    successful_cluster = str(original.cluster_id)
    before = len([c for c in stub.calls if c["cluster_id"] == successful_cluster])
    stub.fail_cluster = None
    run = runner.retry_insight_run(db, run.id)
    assert run.status == "completed"
    assert (original.id, original.insight, original.created_at) == snapshot
    assert db.get(Insight, snapshot[0]) is not None
    assert len([c for c in stub.calls if c["cluster_id"] == successful_cluster]) == before
    assert run.success_count == run.processed_count == 2


def test_small_cluster_needs_validation(db, monkeypatch):
    p, _, source, _ = seed(db, (2,))
    client(monkeypatch)
    run = runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    assert run.needs_validation_count == 1
    assert run.failed_count == 0
    row = db.scalars(select(Insight)).one()
    assert row.status == "needs_validation"
    assert row.evidence_count == 2


def test_total_feedback_count_not_sample_size(db, monkeypatch):
    p, _, source, _ = seed(db, (10,))
    client(monkeypatch)
    runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    row = db.scalars(select(Insight)).one()
    assert row.feedback_count == 10
    assert 3 <= row.evidence_count <= 8


def test_cluster_project_mismatch_rejected(db, monkeypatch):
    p, _, source, clusters = seed(db, (3,))
    other = Project(name="other", product_name="other")
    db.add(other)
    db.flush()
    clusters[0].project_id = other.id
    db.commit()
    stub = client(monkeypatch)
    with pytest.raises(runner.InsightConfigError):
        runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    assert not stub.calls


def test_interrupted_cluster_without_item_is_recovered(db, monkeypatch):
    from app.models.insight import InsightRun

    p, _, source, _ = seed(db, (3, 3))
    client(monkeypatch)
    original_method = Client.generate_insight
    counter = []

    def interrupt(self, cluster):
        counter.append(cluster)
        if len(counter) == 2:
            raise KeyboardInterrupt()
        return original_method(self, cluster)

    monkeypatch.setattr(Client, "generate_insight", interrupt)
    with pytest.raises(KeyboardInterrupt):
        runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    run = db.scalars(select(InsightRun)).one()
    assert len(db.scalars(select(Insight)).all()) == 1
    monkeypatch.setattr(Client, "generate_insight", original_method)
    run = runner.retry_insight_run(db, run.id)
    assert run.status == "completed"
    assert run.success_count == run.processed_count == 2
    assert len(db.scalars(select(Insight)).all()) == 2


@pytest.mark.parametrize("change", ["remove_member", "classification", "raw_text"])
def test_retry_rejects_source_drift_without_mutation(db, monkeypatch, change):
    p, classification, source, clusters = seed(db, (3,))
    stub = client(monkeypatch)
    stub.fail_cluster = str(clusters[0].id)
    run = runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    assert run.status == "failed"
    if change == "remove_member":
        db.delete(db.scalars(select(ClusterMember)).first())
    elif change == "raw_text":
        db.scalars(select(Feedback)).first().content = "changed raw content"
    else:
        replacement = AnalysisRun(
            project_id=p.id,
            run_type="classification",
            model="test",
            prompt_version="test",
            status="completed",
            input_count=3,
            processed_count=3,
            success_count=3,
        )
        db.add(replacement)
        db.flush()
        source.cluster_config = {"classification_run_id": str(replacement.id)}
        for a in db.scalars(select(FeedbackAnalysis)):
            a.run_id = replacement.id
    db.commit()
    before = (run.status, run.retry_count, run.attempt_count)
    stub.fail_cluster = None
    with pytest.raises(runner.InsightConfigError):
        runner.retry_insight_run(db, run.id)
    db.expire_all()
    assert (run.status, run.retry_count, run.attempt_count) == before


def test_completed_retry_does_not_call_or_change_output(db, monkeypatch):
    p, _, source, _ = seed(db, (3,))
    stub = client(monkeypatch)
    run = runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    calls = len(stub.calls)
    before = (run.attempt_count, run.input_tokens, run.retry_count)
    runner.retry_insight_run(db, run.id)
    assert len(stub.calls) == calls
    assert before == (run.attempt_count, run.input_tokens, run.retry_count)


def test_database_failure_rolls_back_output_but_keeps_usage(db, monkeypatch):
    p, _, source, _ = seed(db, (3,))
    client(monkeypatch)
    from sqlalchemy.exc import IntegrityError

    def reject_evidence(mapper, conn, target):
        raise IntegrityError("forced", {}, Exception("CUSTOM_PRIVATE_SENTINEL"))

    event.listen(InsightEvidence, "before_insert", reject_evidence)
    try:
        run = runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    finally:
        event.remove(InsightEvidence, "before_insert", reject_evidence)
    assert run.status == "failed"
    assert run.attempt_count == 1
    assert run.input_tokens == 11 and run.output_tokens == 7
    assert not db.scalars(select(Insight)).all()
    assert not db.scalars(select(InsightEvidence)).all()


def test_concurrent_lock_rejects_retry(db, monkeypatch):
    p, _, source, _ = seed(db, (3,))
    client(monkeypatch)
    run = runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    with runner._execution_lock(db, run.id):
        with pytest.raises(runner.InsightConfigError):
            runner.retry_insight_run(db, run.id)


def test_retry_preserves_small_completed_result(db, monkeypatch):
    p, _, source, clusters = seed(db, (2, 3))
    stub = client(monkeypatch)
    stub.fail_cluster = str(clusters[1].id)
    run = runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    original = db.scalars(select(Insight)).one()
    saved_id = original.id
    original_calls = sum(c["cluster_id"] == str(clusters[0].id) for c in stub.calls)
    stub.fail_cluster = None
    runner.retry_insight_run(db, run.id)
    assert db.get(Insight, saved_id) is not None
    assert sum(c["cluster_id"] == str(clusters[0].id) for c in stub.calls) == original_calls


def test_retry_with_excluded_outlier_and_empty_parent(db, monkeypatch):
    p, _, source, clusters = seed(db, (3, 1, 0))
    clusters[1].source = "outlier"
    clusters[2].source = "algorithmic"
    db.commit()
    stub = client(monkeypatch)
    stub.fail_cluster = str(clusters[0].id)
    run = runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    stub.fail_cluster = None
    run = runner.retry_insight_run(db, run.id)
    assert run.status == "completed"
    assert run.input_count == 1


def test_unexpected_later_error_preserves_success_counts(db, monkeypatch):
    p, _, source, _ = seed(db, (3, 3))
    client(monkeypatch)
    original = Client.generate_insight
    calls = []

    def crash(self, cluster):
        calls.append(cluster)
        if len(calls) == 2:
            raise RuntimeError("CUSTOM_PRIVATE_SENTINEL")
        return original(self, cluster)

    monkeypatch.setattr(Client, "generate_insight", crash)
    run = runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    assert len(db.scalars(select(Insight)).all()) == 1
    assert run.success_count == 1
    assert "CUSTOM_PRIVATE_SENTINEL" not in (run.error_message or "")


def test_small_cluster_provider_schema_allows_two_items():
    from app.services.insight_client import TOOL_SCHEMA

    assert TOOL_SCHEMA["properties"]["evidence"]["minItems"] <= 2


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), -0.1, 1.1, "0.8"])
@pytest.mark.parametrize("field", ["confidence", "relevance_score"])
def test_invalid_numbers_are_rejected(bad, field):
    data = payload(["a", "b", "c"])
    if field == "confidence":
        data[field] = bad
    else:
        data["evidence"][0][field] = bad
    with pytest.raises(InsightValidationError):
        validate_insight_payload(data, allowed_feedback_ids=["a", "b", "c"])


@pytest.mark.parametrize("ids", [[], ["a", "b", "x"], ["a", "a", "b"]])
def test_invalid_evidence_sets_rejected(ids):
    with pytest.raises(InsightValidationError):
        validate_insight_payload(payload(ids), allowed_feedback_ids=["a", "b", "c"])


def test_live_retry_retains_success_progress(db, monkeypatch):
    p, _, source, clusters = seed(db, (3, 3))
    stub = client(monkeypatch)
    stub.fail_cluster = str(clusters[1].id)
    run = runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    assert run.success_count == 1
    original = Client.generate_insight
    seen = []

    def observe(self, cluster):
        seen.append(run.success_count)
        return original(self, cluster)

    monkeypatch.setattr(Client, "generate_insight", observe)
    stub.fail_cluster = None
    runner.retry_insight_run(db, run.id)
    assert seen and all(n == 1 for n in seen)


def test_missing_configuration_rejected_before_creating_run(db, monkeypatch):
    from app.models.insight import InsightRun

    p, _, source, _ = seed(db, (3,))
    monkeypatch.setattr(settings, "llm_api_key", None)
    with pytest.raises(runner.InsightConfigError):
        runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    assert not db.scalars(select(InsightRun)).all()


@pytest.mark.parametrize("usage", [[], "", False, 0])
def test_malformed_provider_usage_rejected(usage):
    from app.services.insight_client import (
        TOOL_NAME,
        InsightLLMProtocolError,
        _parse_messages_response,
    )

    with pytest.raises(InsightLLMProtocolError):
        _parse_messages_response(
            {"content": [{"type": "tool_use", "name": TOOL_NAME, "input": {}}], "usage": usage}
        )
