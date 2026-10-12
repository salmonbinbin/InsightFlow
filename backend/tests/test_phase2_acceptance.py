"""Independent Phase 2 regression gates; all data is isolated test data."""

import math
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from test_phase2 import (
    StubReviewClient,
    _analysis_row,
    _create_project,
    _feedback_rows,
    _import_feedback,
    _install_review_stub,
    _run_classification,
)
from test_phase2 import (
    client as phase2_client,
)

from app.db.config import settings
from app.models import Cluster, ClusterMember, Embedding, Feedback, FeedbackAnalysis
from app.services import cluster_runner, embedding
from app.services.cluster_review import (
    ClusterReviewValidationError,
    ReviewItem,
    validate_review_payload,
)
from app.services.clustering import VectorPoint, agglomerative_cluster
from app.services.llm_client import LLMResult, LLMUsage

client = phase2_client


def prepare(client, monkeypatch, n=4):
    c, engine = client
    pid = _create_project(c)
    rows = _feedback_rows(n)
    _import_feedback(c, pid, rows)
    classified = _run_classification(c, monkeypatch, pid, {r[0]: _analysis_row(r[0]) for r in rows})
    _install_review_stub(monkeypatch, StubReviewClient())
    return c, engine, pid, classified


def test_all_embedding_batches_and_raw_immutable(client, monkeypatch):
    c, engine, pid, _ = prepare(client, monkeypatch, 35)
    with Session(engine) as db:
        before = [
            (f.id, f.content)
            for f in db.scalars(select(Feedback).where(Feedback.project_id == UUID(pid)))
        ]
    run = c.post(f"/projects/{pid}/clustering").json()
    with Session(engine) as db:
        count = db.scalar(
            select(func.count()).select_from(Embedding).where(Embedding.run_id == UUID(run["id"]))
        )
        after = [
            (f.id, f.content)
            for f in db.scalars(select(Feedback).where(Feedback.project_id == UUID(pid)))
        ]
    assert count == 35
    assert before == after
    assert run["success_count"] == run["processed_count"] == 35


def test_union_root_replacement_preserves_members():
    points = [
        VectorPoint(str(i), (math.cos(math.radians(a)), math.sin(math.radians(a))))
        for i, a in enumerate([28, 46, 43, 184, 86])
    ]
    clusters = agglomerative_cluster(points, distance_threshold=0.5)
    assert sorted(sorted(c.members) for c in clusters) == [["0", "1", "2", "4"], ["3"]]


@pytest.mark.parametrize("bad", [[float("nan"), 1.0], [True, 1.0], [0.0, 0.0]])
def test_invalid_vector_rejected(bad):
    with pytest.raises(ValueError):
        agglomerative_cluster([VectorPoint("a", tuple(bad))], distance_threshold=0.4)


def test_remote_index_mapping():
    with embedding.RemoteEmbedder(
        model="remote",
        dimension=2,
        api_key="fake",
        base_url="https://example.invalid",
        timeout_seconds=1,
    ) as e:
        e._client.close()
        e._client = httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    json={
                        "data": [
                            {"index": 1, "embedding": [0, 1]},
                            {"index": 0, "embedding": [1, 0]},
                        ]
                    },
                )
            )
        )
        assert e.embed(["first", "second"]) == [[1, 0], [0, 1]]


def test_remote_model_never_silently_hashes(monkeypatch):
    monkeypatch.setattr(settings, "embedding_model", "semantic-remote")
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("EMBEDDING_BASE_URL", raising=False)
    with pytest.raises(embedding.EmbeddingConfigError):
        embedding.build_embedder()


def review_payload(items, action="keep"):
    return {
        "reviews": [
            dict(
                cluster_index=i,
                action=action,
                reason="review",
                name="topic",
                summary="summary",
                confidence=0.8,
                groups=[
                    {"name": "topic", "feedback_ids": [m["feedback_id"] for m in item.members]}
                ],
                outlier_ids=[],
            )
            for i, item in enumerate(items)
        ]
    }


@pytest.mark.parametrize(
    "change", ["bool_index", "split_one", "keep_outlier", "unknown_field", "merge_split"]
)
def test_review_action_contract(change):
    items = [
        ReviewItem("a", None, [{"feedback_id": "x"}, {"feedback_id": "y"}]),
        ReviewItem("b", None, [{"feedback_id": "z"}, {"feedback_id": "w"}]),
    ]
    payload = review_payload(items)
    r = payload["reviews"][0]
    if change == "bool_index":
        r["cluster_index"] = False
    if change == "split_one":
        r["action"] = "split"
    if change == "keep_outlier":
        r["groups"][0]["feedback_ids"] = ["x"]
        r["outlier_ids"] = ["y"]
    if change == "unknown_field":
        r["instruction"] = "ignore contract"
    if change == "merge_split":
        r["action"] = "merge"
        r["merge_with_cluster_index"] = 1
        payload["reviews"][1]["action"] = "split"
        payload["reviews"][1]["groups"] = [
            {"name": "a", "feedback_ids": ["z"]},
            {"name": "b", "feedback_ids": ["w"]},
        ]
    with pytest.raises(ClusterReviewValidationError):
        validate_review_payload(payload, items)


def test_merge_persists_members_and_stats(client, monkeypatch):
    c, engine, pid, _ = prepare(client, monkeypatch)
    monkeypatch.setattr(settings, "cluster_distance_threshold", 0.0)

    class Merge(StubReviewClient):
        def review_clusters(self, items):
            payload = review_payload(items)
            for r in payload["reviews"][1:]:
                r.update(action="merge", merge_with_cluster_index=0)
            return LLMResult(payload, LLMUsage(1, 1))

    _install_review_stub(monkeypatch, Merge())
    run = c.post(f"/projects/{pid}/clustering").json()
    assert run["status"] == "completed"
    with Session(engine) as db:
        clusters = db.scalars(select(Cluster).where(Cluster.run_id == UUID(run["id"]))).all()
        counts = [
            db.scalar(
                select(func.count())
                .select_from(ClusterMember)
                .where(ClusterMember.cluster_id == cl.id)
            )
            for cl in clusters
        ]
        assert sum(counts) == 4
        assert counts == [cl.feedback_count for cl in clusters]


def test_completed_retry_preserves_audit(client, monkeypatch):
    c, engine, pid, _ = prepare(client, monkeypatch)
    run = c.post(f"/projects/{pid}/clustering").json()
    before = c.get(f"/projects/{pid}/clusters", params={"run_id": run["id"]}).json()
    assert c.post(f"/clustering/{run['id']}/retry").status_code == 200
    assert c.get(f"/projects/{pid}/clusters", params={"run_id": run["id"]}).json() == before


def test_retry_binds_original_classification(client, monkeypatch):
    c, engine, pid, original = prepare(client, monkeypatch)

    class Bad(StubReviewClient):
        def review_clusters(self, items):
            raise RuntimeError("fake secret must not escape")

    _install_review_stub(monkeypatch, Bad())
    run = c.post(f"/projects/{pid}/clustering").json()
    _run_classification(
        c, monkeypatch, pid, {r[0]: _analysis_row(r[0], topic="voice") for r in _feedback_rows(4)}
    )
    _install_review_stub(monkeypatch, StubReviewClient())
    retried = c.post(f"/clustering/{run['id']}/retry").json()
    assert retried["status"] == "completed"
    with Session(engine) as db:
        member_runs = set(
            db.scalars(
                select(FeedbackAnalysis.run_id)
                .join(ClusterMember, ClusterMember.feedback_analysis_id == FeedbackAnalysis.id)
                .join(Cluster, Cluster.id == ClusterMember.cluster_id)
                .where(Cluster.run_id == UUID(run["id"]))
            )
        )
        assert member_runs == {UUID(original["id"])}


def test_error_messages_do_not_persist_arbitrary_exception(client, monkeypatch):
    c, engine, pid, _ = prepare(client, monkeypatch)

    def bad():
        raise RuntimeError("ACCEPTANCE_FAKE_CREDENTIAL_NOT_REAL")

    monkeypatch.setattr(cluster_runner, "build_embedder", bad)
    run = c.post(f"/projects/{pid}/clustering").json()
    assert run["status"] == "failed"
    assert "ACCEPTANCE_FAKE_CREDENTIAL_NOT_REAL" not in str(run)


def test_pgvector_enabled_and_column(client):
    _, engine = client
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL only")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM pg_extension WHERE extname='vector'")) == 1
        assert (
            conn.scalar(
                text(
                    "SELECT udt_name FROM information_schema.columns WHERE table_name='feedback_embeddings' AND column_name='embedding'"
                )
            )
            == "vector"
        )
        assert conn.scalar(text("SELECT '[1,0]'::vector <=> '[0,1]'::vector")) == pytest.approx(1.0)


def test_partial_embedding_failure_retry_only_missing(client, monkeypatch):
    c, engine, pid, _ = prepare(client, monkeypatch, 5)
    monkeypatch.setattr(settings, "embedding_max_batch_size", 2)
    monkeypatch.setattr(settings, "embedding_max_retries", 0)

    class Partial(embedding.LocalHashEmbedder):
        calls = 0

        def embed(self, texts):
            self.calls += 1
            if self.calls == 2:
                raise embedding.EmbeddingTransportError("synthetic unavailable")
            return super().embed(texts)

    partial = Partial("local-hash-v1", 16)
    monkeypatch.setattr(cluster_runner, "build_embedder", lambda: partial)
    run = c.post(f"/projects/{pid}/clustering").json()
    assert run["status"] == "failed"
    assert run["failed_count"] == run["processed_count"] == run["input_count"] == 5
    assert run["success_count"] == 0
    with Session(engine) as db:
        before = set(db.scalars(select(Embedding.id).where(Embedding.run_id == UUID(run["id"]))))
        assert len(before) == 2
    _install_review_stub(monkeypatch, StubReviewClient())
    retried = c.post(f"/clustering/{run['id']}/retry").json()
    assert retried["status"] == "completed"
    with Session(engine) as db:
        after = set(db.scalars(select(Embedding.id).where(Embedding.run_id == UUID(run["id"]))))
        assert len(after) == 5 and before <= after
    assert retried["cluster_config"]["embedding_calls"] == 2


@pytest.mark.parametrize("n", [1, 2])
def test_tiny_samples_and_duplicate_run(client, monkeypatch, n):
    c, engine, pid, _ = prepare(client, monkeypatch, n)
    first = c.post(f"/projects/{pid}/clustering").json()
    second = c.post(f"/projects/{pid}/clustering").json()
    assert first["status"] == second["status"] == "completed"
    assert first["id"] != second["id"]
    assert second["cluster_config"]["embedding_calls"] == 0
    assert second["success_count"] == n


def test_failed_review_accounts_usage_and_hides_values(client, monkeypatch, caplog):
    c, engine, pid, _ = prepare(client, monkeypatch)

    class Bad(StubReviewClient):
        def review_clusters(self, items):
            payload = review_payload(items)
            payload["reviews"][0]["groups"][0]["feedback_ids"] = [
                "ACCEPTANCE_FAKE_CREDENTIAL_NOT_REAL"
            ]
            return LLMResult(payload, LLMUsage(13, 17))

    _install_review_stub(monkeypatch, Bad())
    run = c.post(f"/projects/{pid}/clustering").json()
    assert run["status"] == "partial_failed"
    assert run["input_tokens"] == 13 and run["output_tokens"] == 17
    assert "ACCEPTANCE_FAKE_CREDENTIAL_NOT_REAL" not in str(run) + caplog.text


def test_review_all_outliers(client, monkeypatch):
    c, engine, pid, _ = prepare(client, monkeypatch, 1)

    class Outlier(StubReviewClient):
        def review_clusters(self, items):
            payload = review_payload(items)
            for r in payload["reviews"]:
                r.update(action="outlier", outlier_ids=r["groups"][0]["feedback_ids"], groups=[])
            return LLMResult(payload, LLMUsage(1, 1))

    _install_review_stub(monkeypatch, Outlier())
    run = c.post(f"/projects/{pid}/clustering").json()
    assert run["status"] == "completed"
    listing = c.get(f"/projects/{pid}/clusters").json()["items"]
    assert sum(cl["feedback_count"] for cl in listing if cl["source"] == "outlier") == 1


def test_retry_configuration_drift_rejected(client, monkeypatch):
    c, engine, pid, _ = prepare(client, monkeypatch)

    class Bad(StubReviewClient):
        def review_clusters(self, items):
            raise RuntimeError("synthetic failure")

    _install_review_stub(monkeypatch, Bad())
    run = c.post(f"/projects/{pid}/clustering").json()
    monkeypatch.setattr(settings, "embedding_model", "another-model")
    assert c.post(f"/clustering/{run['id']}/retry").status_code == 409


def test_pg_dimension_constraint_and_readback(client, monkeypatch):
    c, engine, pid, _ = prepare(client, monkeypatch, 1)
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL only")
    run = c.post(f"/projects/{pid}/clustering").json()
    from sqlalchemy.exc import DBAPIError

    with engine.connect() as conn:
        stored = (
            conn.execute(
                text(
                    "SELECT id,embedding_dimension,vector_dims(embedding) AS actual,embedding::text FROM feedback_embeddings WHERE run_id=:rid"
                ),
                {"rid": run["id"]},
            )
            .mappings()
            .one()
        )
        assert stored["actual"] == stored["embedding_dimension"] == 16
        for sql in [
            "UPDATE feedback_embeddings SET embedding='[1,0]'::vector WHERE id=:id",
            "UPDATE feedback_embeddings SET embedding_dimension=17 WHERE id=:id",
        ]:
            with pytest.raises(DBAPIError):
                with conn.begin_nested():
                    conn.execute(text(sql), {"id": stored["id"]})


def test_missing_classification_and_empty_project(client):
    c, _ = client
    pid = _create_project(c)
    assert c.post(f"/projects/{pid}/clustering").status_code == 409
    _import_feedback(c, pid, _feedback_rows(1))
    assert c.post(f"/projects/{pid}/clustering").status_code == 409


def test_database_failure_preserves_raw_and_retry(client, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    c, engine, pid, _ = prepare(client, monkeypatch, 3)
    original = cluster_runner._create_algorithmic_cluster

    def fail(*args, **kwargs):
        raise IntegrityError("synthetic", {}, Exception("db unavailable"))

    monkeypatch.setattr(cluster_runner, "_create_algorithmic_cluster", fail)
    run = c.post(f"/projects/{pid}/clustering").json()
    assert run["status"] == "failed"
    assert c.get(f"/projects/{pid}/feedback").json()["total"] == 3
    monkeypatch.setattr(cluster_runner, "_create_algorithmic_cluster", original)
    assert c.post(f"/clustering/{run['id']}/retry").json()["status"] == "completed"


def test_negative_ratio_does_not_count_mixed():
    from app.services.clustering import ClusterCandidate, cluster_statistics

    stats = cluster_statistics(
        ClusterCandidate(members=["a", "b"]),
        {"a": 2, "b": 4},
        {"a": "negative", "b": "mixed"},
        {"a": "u", "b": "u"},
    )
    assert stats == dict(
        feedback_count=2, unique_user_count=1, average_severity=3.0, negative_ratio=0.5
    )


def test_classification_cross_project_member_rejected(client, monkeypatch):
    c, engine, pid, source = prepare(client, monkeypatch, 1)
    other = _create_project(c)
    _import_feedback(c, other, _feedback_rows(1))
    with Session(engine) as db:
        foreign = db.scalar(select(Feedback).where(Feedback.project_id == UUID(other)))
        analysis = db.scalar(
            select(FeedbackAnalysis).where(FeedbackAnalysis.run_id == UUID(source["id"]))
        )
        analysis.feedback_id = foreign.id
        db.commit()
    assert c.post(f"/projects/{pid}/clustering").status_code == 409


def test_retry_concurrent_claim_returns_conflict(client, monkeypatch):
    import threading

    c, engine, pid, _ = prepare(client, monkeypatch, 1)

    class Bad(StubReviewClient):
        def review_clusters(self, items):
            raise RuntimeError("synthetic failure")

    _install_review_stub(monkeypatch, Bad())
    run = c.post(f"/projects/{pid}/clustering").json()
    entered = threading.Event()
    release = threading.Event()

    class Hold(StubReviewClient):
        def review_clusters(self, items):
            entered.set()
            assert release.wait(10)
            return super().review_clusters(items)

    _install_review_stub(monkeypatch, Hold())
    results = []

    def retry():
        results.append(c.post(f"/clustering/{run['id']}/retry"))

    thread = threading.Thread(target=retry)
    thread.start()
    try:
        assert entered.wait(10)
        assert c.post(f"/clustering/{run['id']}/retry").status_code == 409
    finally:
        release.set()
        thread.join(10)
    assert len(results) == 1 and results[0].json()["status"] == "completed"


def test_semantic_text_deterministic():
    from app.services.semantic_text import build_semantic_text

    kwargs = dict(module="file_upload", intent="upload_pdf", pain_point="PDF fails")
    first = build_semantic_text(" PDF 失败\n原文 ", **kwargs)
    assert first == build_semantic_text(" PDF 失败\n原文 ", **kwargs)
    assert all(
        label in first for label in ("Original feedback:", "Module:", "Intent:", "Pain Point:")
    )


@pytest.mark.parametrize("mutation", ["action", "missing", "duplicate", "hallucinated"])
def test_review_rejects_invalid_outputs(mutation):
    items = [ReviewItem("c", None, [{"feedback_id": "a"}, {"feedback_id": "b"}])]
    payload = review_payload(items)
    row = payload["reviews"][0]
    if mutation == "action":
        row["action"] = "invent"
    if mutation == "missing":
        del row["summary"]
    if mutation == "duplicate":
        row["groups"][0]["feedback_ids"] = ["a", "a", "b"]
    if mutation == "hallucinated":
        row["groups"][0]["feedback_ids"] = ["a", "not-an-input"]
    with pytest.raises(ClusterReviewValidationError):
        validate_review_payload(payload, items)


def test_threshold_direction_boundary_and_nonunit_vectors():
    from app.services.clustering import cosine_distance

    a = VectorPoint("a", (2.0, 0.0))
    b = VectorPoint("b", (1.0, 1.0))
    boundary = cosine_distance(a.vector, b.vector)
    assert len(agglomerative_cluster([a, b], distance_threshold=boundary)) == 2
    assert len(agglomerative_cluster([a, b], distance_threshold=boundary + 1e-6)) == 1
    assert cosine_distance((1.0, 0.0), (-1.0, 0.0)) == pytest.approx(2.0)


@pytest.mark.parametrize(
    "items",
    [
        [{"index": 0, "embedding": [1, 0]}, {"index": 0, "embedding": [0, 1]}],
        [{"index": False, "embedding": [1, 0]}, {"index": 1, "embedding": [0, 1]}],
        [{"index": 0, "embedding": [True, 0]}, {"index": 1, "embedding": [0, 1]}],
        [{"index": 0, "embedding": [0, 0]}, {"index": 1, "embedding": [0, 1]}],
        [{"index": 0, "embedding": [1]}, {"index": 1, "embedding": [0, 1]}],
    ],
)
def test_remote_rejects_invalid_response(items):
    with embedding.RemoteEmbedder(
        model="remote",
        dimension=2,
        api_key="fake",
        base_url="https://example.invalid",
        timeout_seconds=1,
    ) as e:
        e._client.close()
        e._client = httpx.Client(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"data": items}))
        )
        with pytest.raises(
            (embedding.EmbeddingProtocolError, embedding.EmbeddingDimensionMismatch)
        ):
            e.embed(["one", "two"])


def test_embedding_transient_retry_has_no_duplicate_rows(client, monkeypatch):
    c, engine, pid, _ = prepare(client, monkeypatch, 2)

    class Transient(embedding.LocalHashEmbedder):
        calls = 0

        def embed(self, texts):
            self.calls += 1
            if self.calls == 1:
                raise embedding.EmbeddingTransportError("unavailable")
            return super().embed(texts)

    e = Transient("local-hash-v1", 16)
    monkeypatch.setattr(cluster_runner, "build_embedder", lambda: e)
    run = c.post(f"/projects/{pid}/clustering").json()
    assert run["status"] == "completed"
    assert run["cluster_config"]["embedding_calls"] == 2
    assert run["cluster_config"]["embedding_retries"] == 1
    with Session(engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Embedding)
                .where(Embedding.run_id == UUID(run["id"]))
            )
            == 2
        )


def test_review_usage_includes_cached_tokens():
    from app.services.cluster_review import TOOL_NAME, _parse_messages_response

    response = {
        "content": [{"type": "tool_use", "name": TOOL_NAME, "input": {"reviews": []}}],
        "usage": {
            "input_tokens": 15,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 20,
            "output_tokens": 4,
        },
    }
    assert _parse_messages_response(response).usage == LLMUsage(135, 4)


def test_review_invalid_usage_is_protocol_error():
    from app.services.cluster_review import TOOL_NAME, _parse_messages_response
    from app.services.llm_client import LLMProtocolError

    with pytest.raises(LLMProtocolError):
        _parse_messages_response(
            {"content": [{"type": "tool_use", "name": TOOL_NAME, "input": {}}], "usage": []}
        )
