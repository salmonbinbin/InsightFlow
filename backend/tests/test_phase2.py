"""Phase 2 — Embedding + Semantic Clustering tests.

The LLM classification and review clients are replaced with stubs so the
pipeline can be exercised without external services. The same SQLite
fixture from :mod:`test_analysis` is reused to drive the cluster runner
end-to-end.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import config as db_config
from app.main import app
from app.models import (
    ClusterSource,
    RunStatus,
)
from app.models import (
    Embedding as EmbeddingModel,
)
from app.models import (
    Feedback as FeedbackModel,
)
from app.models import (
    FeedbackAnalysis as FeedbackAnalysisModel,
)
from app.services import cluster_review, cluster_runner, clustering, embedding
from app.services.cluster_review import ReviewItem
from app.services.clustering import VectorPoint
from app.services.embedding import EmbeddingStats, LocalHashEmbedder
from app.services.llm_client import LLMResult, LLMUsage

# ----- Test data ----------------------------------------------------------------


def _feedback_rows(n: int) -> list[list[str]]:
    """Generate feedback rows with overlapping topics so clustering is meaningful."""
    pools = [
        ("upload", "上传"),
        ("upload", "压缩"),
        ("voice", "语音"),
        ("search", "搜索"),
        ("ui", "界面"),
    ]
    rows: list[list[str]] = []
    for i in range(n):
        topic, hint = pools[i % len(pools)]
        rows.append([f"F{i:03d}", f"{topic} {hint} {i}", "support", f"U{i % 5}", "free", ""])
    return rows


def _analysis_row(external_id: str, *, topic: str = "image_upload") -> dict[str, Any]:
    return {
        "feedback_id": external_id,
        "feedback_type": "feature_request",
        "module": topic,
        "intent": f"{topic}_intent",
        "pain_point": f"Pain point for {external_id}",
        "sentiment": "negative",
        "severity": 3,
        "confidence": 0.9,
    }


# ----- Configuration helpers ----------------------------------------------------


def _configure_env(monkeypatch) -> None:
    """Patch the Settings singleton so Phase 1 stubs see configured LLM env."""
    monkeypatch.setattr(db_config.settings, "llm_api_key", "test-key", raising=False)
    monkeypatch.setattr(
        db_config.settings, "llm_base_url", "https://example.invalid/anthropic", raising=False
    )
    monkeypatch.setattr(db_config.settings, "llm_model", "stub-model", raising=False)
    monkeypatch.setattr(db_config.settings, "llm_timeout_seconds", 10.0, raising=False)
    monkeypatch.setattr(db_config.settings, "classification_batch_size", 20, raising=False)
    monkeypatch.setattr(db_config.settings, "classification_max_retries", 1, raising=False)
    # Phase 2 — review settings.
    monkeypatch.setattr(db_config.settings, "cluster_review_max_retries", 0, raising=False)
    monkeypatch.setattr(db_config.settings, "embedding_max_retries", 1, raising=False)
    monkeypatch.setattr(db_config.settings, "embedding_max_batch_size", 16, raising=False)
    monkeypatch.setattr(db_config.settings, "cluster_distance_threshold", 0.6, raising=False)
    monkeypatch.setattr(db_config.settings, "cluster_linkage", "average", raising=False)
    monkeypatch.setattr(db_config.settings, "cluster_review_enabled", True, raising=False)


def _configure_review_disabled(monkeypatch) -> None:
    _configure_env(monkeypatch)
    monkeypatch.setattr(db_config.settings, "cluster_review_enabled", False, raising=False)


def _configure_review_unset(monkeypatch) -> None:
    """Configure env without LLM keys so the review path is *unconfigured*."""
    _configure_env(monkeypatch)
    monkeypatch.setattr(db_config.settings, "llm_api_key", None, raising=False)
    monkeypatch.setattr(db_config.settings, "llm_base_url", None, raising=False)
    monkeypatch.setattr(db_config.settings, "llm_model", None, raising=False)


# ----- Stubs --------------------------------------------------------------------


@dataclass
class StubReviewClient:
    """In-process stub for the LLM cluster reviewer.

    ``plan`` is keyed by ``cluster_id`` when the test knows the runner's
    output IDs in advance. When a cluster_id is missing from the plan the
    stub falls back to ``default_action`` and synthesises a deterministic
    payload from the input members — this lets retry-based tests use the
    stub without re-deriving UUIDs that the runner has just produced.
    """

    plan: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: int = 0
    fail_first: int = 0
    input_tokens_per_call: int = 1
    output_tokens_per_call: int = 1
    default_action: str = "keep"

    def review_clusters(self, items: list[ReviewItem]) -> LLMResult:
        self.calls += 1
        if self.fail_first > 0:
            self.fail_first -= 1
            from app.services.llm_client import LLMTransportError

            raise LLMTransportError("simulated transient failure")
        reviews = []
        for index, item in enumerate(items):
            entry = self.plan.get(item.cluster_id)
            if entry is None:
                member_ids = [m["feedback_id"] for m in item.members]
                if self.default_action == "split":
                    half = max(1, len(member_ids) // 2)
                    entry = {
                        "action": "split",
                        "name": f"topic_{index}",
                        "summary": f"split summary {index}",
                        "groups": [
                            {"name": f"topic_{index}_a", "feedback_ids": member_ids[:half]},
                            {"name": f"topic_{index}_b", "feedback_ids": member_ids[half:]},
                        ],
                        "outlier_ids": [],
                    }
                elif self.default_action == "outlier" and member_ids:
                    last = member_ids[-1:]
                    entry = {
                        "action": "outlier",
                        "name": f"topic_{index}",
                        "summary": f"outlier summary {index}",
                        "groups": [
                            {
                                "name": f"topic_{index}",
                                "feedback_ids": member_ids[:-1] or member_ids,
                            }
                        ],
                        "outlier_ids": last if len(member_ids) > 1 else [],
                    }
                else:
                    entry = {
                        "action": self.default_action,
                        "name": f"topic_{index}",
                        "summary": f"auto summary for {index}",
                        "groups": [{"name": f"topic_{index}", "feedback_ids": member_ids}],
                        "outlier_ids": [],
                    }
            action = entry["action"]
            groups = entry["groups"]
            outlier_ids = entry.get("outlier_ids", [])
            merge_with_cluster_index = entry.get("merge_with_cluster_index")
            reviews.append(
                {
                    "cluster_index": int(entry.get("cluster_index", index)),
                    "action": action,
                    "reason": entry.get("reason", f"stub reason for {item.cluster_id}"),
                    "name": entry["name"],
                    "summary": entry["summary"],
                    "confidence": entry.get("confidence", 0.8),
                    "merge_with_cluster_index": merge_with_cluster_index,
                    "groups": groups,
                    "outlier_ids": outlier_ids,
                }
            )
        return LLMResult(
            payload={"reviews": reviews},
            usage=LLMUsage(self.input_tokens_per_call, self.output_tokens_per_call),
        )

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _install_review_stub(monkeypatch, stub: StubReviewClient) -> None:
    monkeypatch.setattr(
        cluster_runner, "build_embedder", lambda: LocalHashEmbedder("local-hash-v1", 16)
    )
    monkeypatch.setattr(
        cluster_runner,
        "ClusterReviewClient",
        lambda: stub,
    )


def _install_classification_stub(monkeypatch, plan: dict[str, dict[str, Any]]) -> None:
    """Stub the LLM classification client used by start_run."""
    from app.services import classification

    class _Stub:
        calls = 0

        def classify_batch(self, items, max_tokens: int = 4096):
            self.calls += 1
            results = []
            for item in items:
                payload = plan[item["feedback_id"]]
                row = dict(payload)
                row["feedback_id"] = item["feedback_id"]
                results.append(row)
            return LLMResult(payload={"results": results}, usage=LLMUsage(1, 1))

        def close(self) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.close()

    monkeypatch.setattr(classification, "build_llm_client", lambda model=None: _Stub())


# ----- Client + helpers ---------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    engine = create_engine(os.environ.get("TEST_DATABASE_URL", f"sqlite:///{tmp_path}/test.db"))
    from app.db.base import Base

    Base.metadata.create_all(engine)

    def override():
        with Session(engine) as db:
            yield db

    from app.db.session import get_db

    app.dependency_overrides[get_db] = override
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c, engine
    app.dependency_overrides.clear()
    engine.dispose()


def _create_project(c) -> str:
    response = c.post("/projects", json={"name": "Phase2", "product_name": "AI Chat"})
    assert response.status_code == 201
    return response.json()["id"]


def _import_feedback(c, pid: str, rows: list[list[str]]) -> None:
    import csv
    import io

    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(["feedback_id", "content", "source", "user_id", "user_type", "created_at"])
    writer.writerows(rows)
    data = out.getvalue().encode("utf-8-sig")
    resp = c.post(
        f"/projects/{pid}/feedback/import",
        files={"file": ("test.csv", data, "text/csv")},
    )
    assert resp.status_code == 200, resp.text


def _run_classification(c, monkeypatch, pid: str, plan: dict[str, dict[str, Any]]):
    _configure_env(monkeypatch)
    _install_classification_stub(monkeypatch, plan)
    resp = c.post(f"/projects/{pid}/analysis")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ----- Embedding tests ----------------------------------------------------------


def test_local_hash_embedder_is_deterministic():
    embedder = LocalHashEmbedder("local-hash-v1", 16)
    v1 = embedder.embed(["upload voice text"])[0]
    v2 = embedder.embed(["upload voice text"])[0]
    assert v1 == v2
    assert len(v1) == 16
    norm = math.sqrt(sum(x * x for x in v1))
    assert abs(norm - 1.0) < 1e-6


def test_local_hash_embedder_empty_string_is_zero_vector():
    embedder = LocalHashEmbedder("local-hash-v1", 8)
    v = embedder.embed([""])[0]
    assert all(x == 0.0 for x in v)


def test_cosine_distance_unit_vectors_returns_zero_when_identical():
    v = [1.0, 0.0, 0.0]
    assert clustering.cosine_distance(v, v) == pytest.approx(0.0)


def test_cosine_distance_orthogonal_returns_one():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert clustering.cosine_distance(a, b) == pytest.approx(1.0)


def test_agglomerative_cluster_keeps_singletons_when_threshold_zero():
    pts = [
        VectorPoint("a", (1.0, 0.0)),
        VectorPoint("b", (0.0, 1.0)),
        VectorPoint("c", (0.9, 0.1)),
    ]
    out = clustering.agglomerative_cluster(pts, distance_threshold=0.0)
    # At threshold=0 no pair satisfies distance < threshold, so all are singletons.
    member_sets = sorted(tuple(sorted(c.members)) for c in out)
    assert member_sets == [("a",), ("b",), ("c",)]


def test_agglomerative_cluster_merges_close_points():
    pts = [
        VectorPoint("a", (1.0, 0.0)),
        VectorPoint("b", (0.95, 0.05)),
        VectorPoint("c", (0.0, 1.0)),
    ]
    out = clustering.agglomerative_cluster(pts, distance_threshold=0.2)
    member_sets = sorted(tuple(sorted(c.members)) for c in out)
    assert member_sets == [("a", "b"), ("c",)]


def test_build_embedder_picks_remote_when_settings_keys_set(monkeypatch):
    """``build_embedder`` must consult the Settings object so values from
    ``backend/.env`` activate the remote embedder, not just shell env vars.

    This is the regression guard for the "real Embedding config in .env
    never triggered the remote path" bug.
    """
    monkeypatch.setattr(db_config.settings, "embedding_api_key", "test-key", raising=False)
    monkeypatch.setattr(
        db_config.settings,
        "embedding_base_url",
        "https://example.invalid/openai",
        raising=False,
    )
    monkeypatch.setattr(db_config.settings, "embedding_model", "remote-test-model")
    monkeypatch.setattr(db_config.settings, "embedding_dimension", 256)
    assert embedding.is_embedding_remote() is True
    e = embedding.build_embedder()
    assert isinstance(e, embedding.RemoteEmbedder)
    assert e.model == "remote-test-model"
    assert e.dimension == 256


def test_build_embedder_falls_back_to_local_when_keys_unset(monkeypatch):
    """Without ``EMBEDDING_API_KEY`` + ``EMBEDDING_BASE_URL`` the local
    embedder is the only thing the runner can use, so the pipeline keeps
    working end-to-end."""
    monkeypatch.setattr(db_config.settings, "embedding_api_key", None, raising=False)
    monkeypatch.setattr(db_config.settings, "embedding_base_url", None, raising=False)
    assert embedding.is_embedding_remote() is False
    e = embedding.build_embedder()
    assert isinstance(e, embedding.LocalHashEmbedder)
    assert e.model == db_config.settings.embedding_model
    assert e.dimension == db_config.settings.embedding_dimension


def test_ensure_embeddings_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(db_config.settings, "embedding_model", "local-hash-v1")
    monkeypatch.setattr(db_config.settings, "embedding_dimension", 16)
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    from app.db.base import Base

    Base.metadata.create_all(engine)
    from app.models import Project as ProjectModel

    with Session(engine) as db:
        project = ProjectModel(name="t", product_name="t")
        db.add(project)
        db.commit()
        db.refresh(project)
        feedback = FeedbackModel(
            project_id=project.id,
            external_id="F1",
            content="upload image content",
        )
        db.add(feedback)
        db.commit()
        db.refresh(feedback)
        analysis = FeedbackAnalysisModel(
            feedback_id=feedback.id,
            run_id=UUID(int=0),  # placeholder; run_id is required by FK
            feedback_type="feature_request",
            module="upload",
            intent="image",
            pain_point="p",
            sentiment="negative",
            severity=3,
            confidence=0.9,
            semantic_text="x",
        )
        # analysis requires a real run row; create a minimal one:
        from app.models import AnalysisRun as RunModel

        run = RunModel(
            project_id=project.id,
            run_type="classification",
            model="m",
            prompt_version="p",
            status=RunStatus.COMPLETED.value,
            input_count=1,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        analysis.run_id = run.id
        db.add(analysis)
        db.commit()
        db.refresh(analysis)

        embedder = LocalHashEmbedder("local-hash-v1", 16)
        stats = EmbeddingStats()
        rows1 = embedding.ensure_embeddings(
            db, run, [(analysis, feedback)], embedder=embedder, stats=stats
        )
        rows2 = embedding.ensure_embeddings(
            db, run, [(analysis, feedback)], embedder=embedder, stats=stats
        )
        assert len(rows1) == 1
        assert len(rows2) == 1
        assert rows1[0].id == rows2[0].id
        assert stats.calls == 1  # Only one remote call thanks to idempotency.
    engine.dispose()


# ----- Cluster review validation tests ------------------------------------------


def _candidate(member_ids: list[str]) -> ReviewItem:
    return ReviewItem(
        cluster_id="c1",
        name_hint=None,
        members=[
            {"feedback_id": fid, "content": "", "module": "x", "intent": "y", "pain_point": "z"}
            for fid in member_ids
        ],
    )


def test_validate_keep_payload():
    cand = _candidate(["F1", "F2"])
    payload = {
        "reviews": [
            {
                "cluster_index": 0,
                "action": "keep",
                "reason": "single issue",
                "name": "upload",
                "summary": "users want upload",
                "confidence": 0.7,
                "groups": [{"name": "upload", "feedback_ids": ["F1", "F2"]}],
                "outlier_ids": [],
            }
        ]
    }
    out = cluster_review.validate_review_payload(payload, [cand])
    assert out[0].action == "keep"
    assert out[0].name == "upload"


def test_validate_split_payload():
    cand = _candidate(["F1", "F2", "F3"])
    payload = {
        "reviews": [
            {
                "cluster_index": 0,
                "action": "split",
                "reason": "two topics",
                "name": "split_root",
                "summary": "mixed",
                "confidence": 0.6,
                "groups": [
                    {"name": "topic_a", "feedback_ids": ["F1", "F2"]},
                    {"name": "topic_b", "feedback_ids": ["F3"]},
                ],
                "outlier_ids": [],
            }
        ]
    }
    out = cluster_review.validate_review_payload(payload, [cand])
    assert out[0].action == "split"
    assert len(out[0].groups) == 2


def test_validate_merge_requires_target():
    a = ReviewItem(
        cluster_id="ca",
        name_hint=None,
        members=[
            {"feedback_id": fid, "content": "", "module": "", "intent": "", "pain_point": ""}
            for fid in ["A1", "A2"]
        ],
    )
    b = ReviewItem(
        cluster_id="cb",
        name_hint=None,
        members=[
            {"feedback_id": fid, "content": "", "module": "", "intent": "", "pain_point": ""}
            for fid in ["B1", "B2"]
        ],
    )
    payload = {
        "reviews": [
            {
                "cluster_index": 0,
                "action": "merge",
                "reason": "same",
                "name": "merged",
                "summary": "merge",
                "confidence": 0.5,
                "merge_with_cluster_index": 1,
                "groups": [{"name": "merged", "feedback_ids": ["A1", "A2"]}],
                "outlier_ids": [],
            },
            {
                "cluster_index": 1,
                "action": "keep",
                "reason": "kept",
                "name": "kept",
                "summary": "kept",
                "confidence": 0.5,
                "groups": [{"name": "kept", "feedback_ids": ["B1", "B2"]}],
                "outlier_ids": [],
            },
        ]
    }
    out = cluster_review.validate_review_payload(payload, [a, b])
    assert out[0].action == "merge"
    assert out[0].merge_with_cluster_id == "cb"
    assert out[1].action == "keep"


def test_validate_rejects_merge_into_merge():
    a = ReviewItem(
        cluster_id="ca",
        name_hint=None,
        members=[
            {"feedback_id": "A1", "content": "", "module": "", "intent": "", "pain_point": ""}
        ],
    )
    b = ReviewItem(
        cluster_id="cb",
        name_hint=None,
        members=[
            {"feedback_id": "B1", "content": "", "module": "", "intent": "", "pain_point": ""}
        ],
    )
    payload = {
        "reviews": [
            {
                "cluster_index": 0,
                "action": "merge",
                "reason": "x",
                "name": "n",
                "summary": "s",
                "confidence": 0.5,
                "merge_with_cluster_index": 1,
                "groups": [{"name": "n", "feedback_ids": ["A1"]}],
                "outlier_ids": [],
            },
            {
                "cluster_index": 1,
                "action": "merge",
                "reason": "x",
                "name": "n",
                "summary": "s",
                "confidence": 0.5,
                "merge_with_cluster_index": 0,
                "groups": [{"name": "n", "feedback_ids": ["B1"]}],
                "outlier_ids": [],
            },
        ]
    }
    with pytest.raises(cluster_review.ClusterReviewValidationError):
        cluster_review.validate_review_payload(payload, [a, b])


def test_validate_outlier_payload():
    cand = _candidate(["F1", "F2", "F3"])
    payload = {
        "reviews": [
            {
                "cluster_index": 0,
                "action": "outlier",
                "reason": "noisy",
                "name": "main",
                "summary": "filtered",
                "confidence": 0.7,
                "groups": [{"name": "main", "feedback_ids": ["F1", "F2"]}],
                "outlier_ids": ["F3"],
            }
        ]
    }
    out = cluster_review.validate_review_payload(payload, [cand])
    assert out[0].outlier_ids == ["F3"]


def test_validate_rejects_unknown_member():
    cand = _candidate(["F1", "F2"])
    payload = {
        "reviews": [
            {
                "cluster_index": 0,
                "action": "keep",
                "reason": "x",
                "name": "n",
                "summary": "s",
                "confidence": 0.5,
                "groups": [{"name": "n", "feedback_ids": ["F1", "ZZ"]}],
                "outlier_ids": [],
            }
        ]
    }
    with pytest.raises(cluster_review.ClusterReviewValidationError):
        cluster_review.validate_review_payload(payload, [cand])


def test_validate_rejects_missing_members():
    cand = _candidate(["F1", "F2", "F3"])
    payload = {
        "reviews": [
            {
                "cluster_index": 0,
                "action": "keep",
                "reason": "x",
                "name": "n",
                "summary": "s",
                "confidence": 0.5,
                "groups": [{"name": "n", "feedback_ids": ["F1"]}],
                "outlier_ids": [],
            }
        ]
    }
    with pytest.raises(cluster_review.ClusterReviewValidationError):
        cluster_review.validate_review_payload(payload, [cand])


# ----- Cluster runner E2E -------------------------------------------------------


def _seed_classification(c, engine, pid: str, n: int = 6):
    rows = _feedback_rows(n)
    _import_feedback(c, pid, rows)
    {row[0]: _analysis_row(row[0], topic="upload") for row in rows}
    # We can't reuse the API because of stubbing; do it manually:
    with Session(engine) as db:
        from app.models import AnalysisRun as RunModel
        from app.models import Project as ProjectModel

        project = db.get(ProjectModel, UUID(pid))
        run = RunModel(
            project_id=project.id,
            run_type="classification",
            model="stub",
            prompt_version="stub-v1",
            status=RunStatus.COMPLETED.value,
            input_count=n,
            processed_count=n,
            success_count=n,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        feedbacks = db.scalars(
            select(FeedbackModel).where(FeedbackModel.project_id == project.id)
        ).all()
        for f in feedbacks:
            db.add(
                FeedbackAnalysisModel(
                    feedback_id=f.id,
                    run_id=run.id,
                    feedback_type="feature_request",
                    module="upload",
                    intent="upload_intent",
                    pain_point=f"pain for {f.external_id}",
                    sentiment="negative",
                    severity=3,
                    confidence=0.9,
                    semantic_text=f"Original: {f.content}",
                )
            )
        db.commit()
        return run.id


def test_start_clustering_without_classification_fails(client, monkeypatch):
    c, _ = client
    _configure_review_unset(monkeypatch)
    pid = _create_project(c)
    resp = c.post(f"/projects/{pid}/clustering")
    assert resp.status_code == 409


def test_clustering_without_review_persists_algorithmic_clusters(client, monkeypatch):
    c, engine = client
    _configure_review_disabled(monkeypatch)
    pid = _create_project(c)
    _seed_classification(c, engine, pid, n=6)

    resp = c.post(f"/projects/{pid}/clustering")
    assert resp.status_code == 200, resp.text
    cluster_run = resp.json()
    if cluster_run["status"] != "completed":
        print("Clustering failed:", cluster_run.get("error_message"))
    assert cluster_run["status"] == "completed", cluster_run.get("error_message")
    assert cluster_run["input_count"] == 6
    assert cluster_run["success_count"] >= 1
    assert cluster_run["error_message"] is None

    # List clusters for the project.
    listing = c.get(f"/projects/{pid}/clusters").json()
    assert sum(cl["feedback_count"] for cl in listing["items"]) == cluster_run["success_count"]
    for cl in listing["items"]:
        # Algorithmic-only path means no LLM metadata yet.
        assert cl["name"] is None
        assert cl["source"] == ClusterSource.ALGORITHMIC.value


def test_clustering_with_review_keep_action(client, monkeypatch):
    c, engine = client
    _configure_env(monkeypatch)
    pid = _create_project(c)
    _seed_classification(c, engine, pid, n=6)

    # Get the actual algorithmic cluster IDs from a dry run first so the
    # stub plan can target them.
    monkeypatch.setattr(
        cluster_runner, "build_embedder", lambda: LocalHashEmbedder("local-hash-v1", 16)
    )
    # Start cluster run with no review plan to learn the cluster_ids.
    monkeypatch.setattr(db_config.settings, "cluster_review_enabled", False, raising=False)
    pre = c.post(f"/projects/{pid}/clustering").json()
    assert pre["status"] == "completed"
    clusters = c.get(f"/projects/{pid}/clusters", params={"run_id": pre["id"]}).json()
    cluster_ids = [cl["id"] for cl in clusters["items"]]
    assert len(cluster_ids) >= 1

    # Now retry with review enabled and a "keep" plan.
    monkeypatch.setattr(db_config.settings, "cluster_review_enabled", True, raising=False)
    keep_plan = {
        cid: {
            "cluster_index": idx,
            "action": "keep",
            "name": f"topic_{idx}",
            "summary": f"summary for {cid}",
            "groups": [
                {
                    "name": f"topic_{idx}",
                    "feedback_ids": [
                        str(m["feedback_analysis_id"])
                        for m in c.get(f"/clusters/{cid}/members").json()["items"]
                    ],
                }
            ],
            "outlier_ids": [],
        }
        for idx, cid in enumerate(cluster_ids)
    }
    stub = StubReviewClient(plan=keep_plan)
    _install_review_stub(monkeypatch, stub)

    retry = c.post(f"/projects/{pid}/clustering")
    assert retry.status_code == 200, retry.text
    review_run = retry.json()
    assert review_run["status"] == "completed"
    assert review_run["attempt_count"] >= 1
    assert review_run["input_tokens"] >= 1

    clusters = c.get(f"/projects/{pid}/clusters", params={"run_id": review_run["id"]}).json()
    {cl["id"]: cl for cl in clusters["items"]}
    # Algorithmic parents stay (with empty members) and the new LLM
    # cluster adopts the topic_0 name.
    named = [cl for cl in clusters["items"] if cl["name"] is not None]
    assert len(named) == len(cluster_ids)
    for cl in named:
        assert cl["source"] == ClusterSource.LLM_REVIEW.value
        assert cl["confidence"] == pytest.approx(0.8)
        assert cl["summary"]


def test_clustering_with_review_split_action(client, monkeypatch):
    c, engine = client
    _configure_env(monkeypatch)
    pid = _create_project(c)
    _seed_classification(c, engine, pid, n=6)

    monkeypatch.setattr(
        cluster_runner, "build_embedder", lambda: LocalHashEmbedder("local-hash-v1", 16)
    )
    stub = StubReviewClient(plan={}, default_action="split")
    _install_review_stub(monkeypatch, stub)
    resp = c.post(f"/projects/{pid}/clustering")
    assert resp.status_code == 200, resp.text
    cluster_run = resp.json()
    assert cluster_run["status"] == "completed"
    new_clusters = c.get(f"/projects/{pid}/clusters", params={"run_id": cluster_run["id"]}).json()
    children = [
        cl for cl in new_clusters["items"] if cl["source"] == ClusterSource.LLM_REVIEW.value
    ]
    assert len(children) >= 2
    # Every child cluster has a parent_cluster_id pointing to its split root.
    parents = {cl["parent_cluster_id"] for cl in children}
    assert None not in parents
    for parent_id in parents:
        parent = next(cl for cl in new_clusters["items"] if cl["id"] == parent_id)
        assert parent["source"] == ClusterSource.ALGORITHMIC.value
        assert parent["feedback_count"] == 0
        assert sum(cl["parent_cluster_id"] == parent_id for cl in children) == 2
    assert sum(cl["feedback_count"] for cl in children) == 6


def test_clustering_with_review_outlier_action_creates_outlier_cluster(client, monkeypatch):
    c, engine = client
    _configure_env(monkeypatch)
    pid = _create_project(c)
    _seed_classification(c, engine, pid, n=6)

    monkeypatch.setattr(
        cluster_runner, "build_embedder", lambda: LocalHashEmbedder("local-hash-v1", 16)
    )
    stub = StubReviewClient(plan={}, default_action="outlier")
    _install_review_stub(monkeypatch, stub)
    resp = c.post(f"/projects/{pid}/clustering")
    assert resp.status_code == 200, resp.text
    cluster_run = resp.json()
    assert cluster_run["status"] == "completed"
    new_clusters = c.get(f"/projects/{pid}/clusters", params={"run_id": cluster_run["id"]}).json()
    outlier_clusters = [
        cl for cl in new_clusters["items"] if cl["source"] == ClusterSource.OUTLIER.value
    ]
    assert len(outlier_clusters) >= 1
    for cl in outlier_clusters:
        assert cl["parent_cluster_id"] is not None


def test_clustering_retry_reruns_completed(client, monkeypatch):
    """Phase 2 retry from ``completed`` is a full re-run (different from
    Phase 1 which rejects retry of completed)."""
    c, engine = client
    _configure_review_disabled(monkeypatch)
    pid = _create_project(c)
    _seed_classification(c, engine, pid, n=4)
    monkeypatch.setattr(
        cluster_runner, "build_embedder", lambda: LocalHashEmbedder("local-hash-v1", 16)
    )
    first = c.post(f"/projects/{pid}/clustering").json()
    assert first["status"] == "completed"
    resp = c.post(f"/clustering/{first['id']}/retry")
    assert resp.status_code == 200
    second = resp.json()
    assert second["id"] == first["id"]
    assert second["status"] == "completed"


def test_cluster_run_status_endpoint_returns_run(client, monkeypatch):
    c, engine = client
    _configure_review_disabled(monkeypatch)
    pid = _create_project(c)
    _seed_classification(c, engine, pid, n=4)
    monkeypatch.setattr(
        cluster_runner, "build_embedder", lambda: LocalHashEmbedder("local-hash-v1", 16)
    )
    first = c.post(f"/projects/{pid}/clustering").json()
    resp = c.get(f"/clustering/{first['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == first["id"]


def test_cluster_endpoint_returns_404_for_unknown(client):
    c, _ = client
    assert c.get("/clusters/00000000-0000-0000-0000-000000000000").status_code == 404
    assert c.get("/clustering/00000000-0000-0000-0000-000000000000").status_code == 404


def test_list_clusters_filters_by_run_id(client, monkeypatch):
    c, engine = client
    _configure_review_disabled(monkeypatch)
    pid = _create_project(c)
    _seed_classification(c, engine, pid, n=4)
    monkeypatch.setattr(
        cluster_runner, "build_embedder", lambda: LocalHashEmbedder("local-hash-v1", 16)
    )
    first = c.post(f"/projects/{pid}/clustering").json()
    second = c.post(f"/projects/{pid}/clustering").json()

    by_first = c.get(f"/projects/{pid}/clusters", params={"run_id": first["id"]}).json()
    by_second = c.get(f"/projects/{pid}/clusters", params={"run_id": second["id"]}).json()
    latest = c.get(f"/projects/{pid}/clusters").json()

    assert by_first["total"] >= 1
    assert by_second["total"] >= 1
    # The latest call should match second (or whatever is newest).
    assert latest["total"] == by_second["total"] or latest["total"] == by_first["total"]


def test_clustering_run_idempotent_embeddings(tmp_path, monkeypatch):
    """Re-running clustering does not duplicate embeddings or clusters."""
    _configure_env(monkeypatch)
    monkeypatch.setattr(db_config.settings, "cluster_review_enabled", False, raising=False)
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    from app.db.base import Base

    Base.metadata.create_all(engine)
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import app as app_under_test

    def override():
        with Session(engine) as db:
            yield db

    app_under_test.dependency_overrides[get_db] = override
    with TestClient(app_under_test) as c:
        pid = c.post("/projects", json={"name": "idem", "product_name": "x"}).json()["id"]
        _seed_classification(c, engine, pid, n=4)
        monkeypatch.setattr(
            cluster_runner, "build_embedder", lambda: LocalHashEmbedder("local-hash-v1", 16)
        )
        c.post(f"/projects/{pid}/clustering")
        # Count embeddings after first run.
        with Session(engine) as db:
            first_count = db.scalar(
                select(__import__("sqlalchemy").func.count()).select_from(EmbeddingModel)
            )
        c.post(f"/projects/{pid}/clustering")
        with Session(engine) as db:
            second_count = db.scalar(
                select(__import__("sqlalchemy").func.count()).select_from(EmbeddingModel)
            )
        assert first_count == second_count
    app_under_test.dependency_overrides.clear()
    engine.dispose()


def test_cluster_review_unconfigured_skips_review_but_persists_clusters(client, monkeypatch):
    c, engine = client
    _configure_review_unset(monkeypatch)
    pid = _create_project(c)
    _seed_classification(c, engine, pid, n=4)
    monkeypatch.setattr(
        cluster_runner, "build_embedder", lambda: LocalHashEmbedder("local-hash-v1", 16)
    )
    resp = c.post(f"/projects/{pid}/clustering")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "partial_failed"
    assert body["attempt_count"] == 0
    listing = c.get(f"/projects/{pid}/clusters").json()
    assert listing["total"] >= 1
    # All clusters are algorithmic when review is skipped.
    for cl in listing["items"]:
        assert cl["source"] == ClusterSource.ALGORITHMIC.value


def test_cluster_review_failure_marks_partial_failed(client, monkeypatch):
    c, engine = client
    _configure_env(monkeypatch)
    pid = _create_project(c)
    _seed_classification(c, engine, pid, n=4)
    monkeypatch.setattr(
        cluster_runner, "build_embedder", lambda: LocalHashEmbedder("local-hash-v1", 16)
    )
    pre = c.post(f"/projects/{pid}/clustering").json()
    target = c.get(f"/projects/{pid}/clusters", params={"run_id": pre["id"]}).json()["items"][0]
    c.get(f"/clusters/{target['id']}/members").json()["items"]

    # Stub raises on every call; with cluster_review_max_retries=0 the
    # runner should record partial_failed without rolling back clusters.
    class AlwaysFail:
        def review_clusters(self, items):
            from app.services.llm_client import LLMTransportError

            raise LLMTransportError("permanent failure")

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

    monkeypatch.setattr(cluster_runner, "ClusterReviewClient", lambda: AlwaysFail())
    monkeypatch.setattr(db_config.settings, "cluster_review_max_retries", 0, raising=False)

    resp = c.post(f"/clustering/{pre['id']}/retry")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "partial_failed"
    # Algorithmic clusters must still be visible.
    listing = c.get(f"/projects/{pid}/clusters", params={"run_id": body["id"]}).json()
    assert listing["total"] >= 1
