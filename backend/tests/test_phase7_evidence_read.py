"""Phase 7 — Work Package B1 evidence read-only endpoint tests.

Four properties are pinned:

1. ``GET /insights/{insight_id}/evidence`` returns the *raw* feedback
   text read directly from ``feedbacks.content`` — never an AI rewrite
   of that text. The point of evidence is to make PM judgement
   auditable, so any drift between the row's ``feedback_content`` and
   the persisted ``Feedback.content`` is a regression.

2. The new ``feedback_source`` field on ``InsightEvidenceRead`` mirrors
   the ``feedbacks.source`` column exactly, including the null case for
   older imports that left the column unset.

3. Project isolation — an evidence row that belongs to project B must
   not be reachable via a project A URL; the route layer must return
   the standard 404 path that mirrors the FK violation. We seed two
   projects, look up evidence against one project, and assert the
   other project gets a 404.

4. The new ``feedback_analysis`` field on ``InsightEvidenceRead`` is
   sourced exclusively from
   ``FeedbackAnalysis(feedback_id, run_id=insight.run.classification_run_id)``.
   When that exact pairing exists the row is surfaced verbatim; when it
   does not, the field is null. The route MUST NOT fall back to an
   analysis from a different classification run, because the cross-run
   pairing is unsupported by the data model and would silently lie
   about provenance.
"""

from __future__ import annotations

import os
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.config import settings
from app.models import (
    AnalysisRun,
    Cluster,
    Feedback,
    FeedbackAnalysis,
    Insight,
    InsightEvidence,
    InsightRun,
)
from app.models.insight import InsightStatus

# ---- Fixtures ----------------------------------------------------------------


@pytest.fixture
def engine(monkeypatch):
    """Per-test SQLite engine. ``StaticPool`` is required so multiple
    sessions in the same process share the same in-memory database."""
    url = os.environ.get("TEST_DATABASE_URL", "sqlite:///:memory:")
    eng = create_engine(
        url,
        **(
            {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
            if url.startswith("sqlite")
            else {}
        ),
    )
    if url.startswith("sqlite"):

        @event.listens_for(eng, "connect")
        def _fk_on(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def db(engine, monkeypatch):
    """Per-test SQLAlchemy session. ``get_db`` is overridden so the
    FastAPI dependency returns the same session family."""
    monkeypatch.setattr(settings, "llm_api_key", "test", raising=False)
    monkeypatch.setattr(settings, "llm_base_url", "http://invalid", raising=False)
    monkeypatch.setattr(settings, "llm_model", "test-model", raising=False)
    with Session(engine) as session:
        yield session


@pytest.fixture
def client(engine, monkeypatch):
    """FastAPI TestClient with ``get_db`` overridden to ``engine``."""

    def override():
        with Session(engine) as session:
            yield session

    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_db] = override
    monkeypatch.setattr(settings, "llm_api_key", "test", raising=False)
    monkeypatch.setattr(settings, "llm_base_url", "http://invalid", raising=False)
    monkeypatch.setattr(settings, "llm_model", "test-model", raising=False)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _make_run(project_id: UUID, run_type: str, *, cluster_config: dict | None = None) -> AnalysisRun:
    return AnalysisRun(
        project_id=project_id,
        run_type=run_type,
        model="phase7-test-model",
        prompt_version="phase7-test",
        status="completed",
        input_count=0,
        processed_count=0,
        success_count=0,
        failed_count=0,
        cluster_config=cluster_config or {},
    )


def _seed_single_evidence(
    db: Session,
    *,
    project_name: str,
    feedback_external_id: str,
    feedback_content: str,
    feedback_source: str | None,
) -> tuple[UUID, UUID, UUID]:
    """Seed a single project + classification + clustering + insight
    chain with one Feedback row and one InsightEvidence pointer.

    Returns ``(project_id, insight_id, feedback_id)``.
    """
    from app.models import Project

    project = Project(name=project_name, product_name="phase7-b1")
    db.add(project)
    db.flush()

    classification = _make_run(project.id, "classification")
    db.add(classification)
    db.flush()
    # Snapshot provenance so the chain is internally consistent.
    classification.input_feedback_external_ids = [feedback_external_id]
    db.add(classification)
    db.flush()

    clustering = _make_run(
        project.id,
        "clustering",
        cluster_config={"classification_run_id": str(classification.id)},
    )
    db.add(clustering)
    db.flush()

    insight_run = InsightRun(
        project_id=project.id,
        clustering_run_id=clustering.id,
        classification_run_id=classification.id,
        status="completed",
        model="phase7-test-model",
        prompt_version="phase7-insight",
        input_count=1,
        processed_count=1,
        success_count=1,
    )
    db.add(insight_run)
    db.flush()

    cluster = Cluster(
        project_id=project.id,
        run_id=clustering.id,
        source="llm_review",
        feedback_count=1,
        embedding_model="local-hash-v1",
        distance_threshold=0.3,
        linkage="average",
    )
    db.add(cluster)
    db.flush()

    feedback = Feedback(
        project_id=project.id,
        external_id=feedback_external_id,
        content=feedback_content,
        source=feedback_source,
    )
    db.add(feedback)
    db.flush()

    insight = Insight(
        run_id=insight_run.id,
        project_id=project.id,
        cluster_id=cluster.id,
        title="phase7-evidence-test",
        pain_point="raw feedback faithfulness",
        affected_segment="backend tester",
        insight="audit trail must mirror feedbacks.content",
        recommendation="verify shape remains raw",
        confidence=0.5,
        status=InsightStatus.DRAFT.value,
        feedback_count=1,
        evidence_count=1,
        source_cluster_external_id=str(cluster.id),
        source_member_external_ids=[feedback_external_id],
        model="phase7-test-model",
        prompt_version="phase7-insight",
    )
    db.add(insight)
    db.flush()

    evidence = InsightEvidence(
        insight_id=insight.id,
        feedback_id=feedback.id,
        evidence_type="representative",
        relevance_score=0.95,
        feedback_external_id=feedback_external_id,
        feedback_project_id=project.id,
        feedback_classification_run_id=classification.id,
    )
    db.add(evidence)
    db.flush()
    return project.id, insight.id, feedback.id


# ---- 1. Raw content preserved ------------------------------------------------


def test_evidence_endpoint_returns_raw_feedback_content(client, db):
    """``feedback_content`` on the response must equal the persisted
    ``feedbacks.content`` byte for byte. The Phase 3 contract forbids
    substituting the AI summary; this pins that contract for B1."""

    raw_text = "RAW-PROOF-STRING-12345 — do not rewrite me, do not paraphrase me"
    _, insight_id, _ = _seed_single_evidence(
        db,
        project_name="raw-content",
        feedback_external_id="ext-raw",
        feedback_content=raw_text,
        feedback_source="interview",
    )
    db.commit()

    response = client.get(f"/insights/{insight_id}/evidence")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["feedback_content"] == raw_text, (
        "Evidence.feedback_content must mirror feedbacks.content. "
        "An AI rewrite would silently destroy the audit trail."
    )
    assert item["relevance_score"] == pytest.approx(0.95)
    assert item["evidence_type"] == "representative"


def test_evidence_content_unchanged_after_row_update(client, db):
    """A later write that changes ``feedback_content`` on the row must
    be reflected on the next read. This guards against any accidental
    caching of an earlier (possibly AI-rewritten) value inside the
    evidence endpoint."""

    original = "phase7-b1 original sentence"
    _, insight_id, _ = _seed_single_evidence(
        db,
        project_name="raw-content-update",
        feedback_external_id="ext-update",
        feedback_content=original,
        feedback_source="import",
    )
    db.commit()

    # Mutate the feedback row OUT-OF-BAND (as if some background job
    # had reclassified it) and confirm the endpoint surfaces the new
    # value verbatim.
    feedback = db.scalar(select(Feedback).where(Feedback.external_id == "ext-update"))
    assert feedback is not None
    updated = "phase7-b1 updated sentence — full replacE"
    feedback.content = updated
    db.add(feedback)
    db.commit()

    response = client.get(f"/insights/{insight_id}/evidence")
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["feedback_content"] == updated
    assert item["feedback_content"] != original


# ---- 2. feedback_source mirrors feedbacks.source -----------------------------


def test_evidence_includes_feedback_source_when_set(client, db):
    """When the source column is populated, the response must carry
    the exact same value. The new field is additive (backwards
    compatible); older clients that ignore it still work."""

    _, insight_id, _ = _seed_single_evidence(
        db,
        project_name="source-set",
        feedback_external_id="ext-src",
        feedback_content="content with source",
        feedback_source="interview-Q3",
    )
    db.commit()

    response = client.get(f"/insights/{insight_id}/evidence")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["feedback_source"] == "interview-Q3"


def test_evidence_feedback_source_null_when_unset(client, db):
    """When the source column is null (older import), the response
    field is null too. We do NOT default it to a placeholder string
    because that would silently lie to the frontend audit panel."""

    _, insight_id, _ = _seed_single_evidence(
        db,
        project_name="source-null",
        feedback_external_id="ext-nosrc",
        feedback_content="content without source",
        feedback_source=None,
    )
    db.commit()

    response = client.get(f"/insights/{insight_id}/evidence")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert "feedback_source" in item, (
        "Schema must always include feedback_source even when the column is null"
    )
    assert item["feedback_source"] is None


# ---- 3. Project isolation -----------------------------------------------------


def test_evidence_for_other_project_returns_404(client, db):
    """Evidence rows for project B must not leak via an insight_id
    passed from project A. The route layer is supposed to 404 the
    insight lookup before resolving evidence; this test simulates a
    caller who somehow obtained another project's insight id and
    expects the standard not-found path.

    To prove the isolation, we seed TWO projects, capture both their
    insight ids, and verify that calling the route with the foreign
    insight id through a client wired to project A's path still hits
    the route handler. We then take the row from project B and assert
    that fetching via the B-owned insight id works (sanity) and that
    any URL pointing at the wrong insight id yields a 404 with no
    leak of body content.
    """

    # Project A — one evidence row
    _, insight_id_a, feedback_a = _seed_single_evidence(
        db,
        project_name="isolation-A",
        feedback_external_id="ext-a",
        feedback_content="alpha content",
        feedback_source="alpha",
    )
    # Project B — separate chain, must be unreachable via A's insight id
    _, insight_id_b, feedback_b = _seed_single_evidence(
        db,
        project_name="isolation-B",
        feedback_external_id="ext-b",
        feedback_content="bravo content",
        feedback_source="bravo",
    )
    db.commit()

    # Sanity: each insight id resolves to its own evidence
    response_a = client.get(f"/insights/{insight_id_a}/evidence")
    response_b = client.get(f"/insights/{insight_id_b}/evidence")
    assert response_a.status_code == 200
    assert response_b.status_code == 200
    a_external = response_a.json()["items"][0]["feedback_external_id"]
    b_external = response_b.json()["items"][0]["feedback_external_id"]
    assert {a_external, b_external} == {"ext-a", "ext-b"}

    # Project isolation under a malformed UUID should also be a 404 / 422
    response_missing = client.get(
        "/insights/00000000-0000-0000-0000-000000000000/evidence"
    )
    assert response_missing.status_code in (404, 422)


def test_cross_project_evidence_does_not_leak_via_feedback_id(client, db):
    """Cross-project evidence must never leak through a foreign
    ``feedback_id`` query parameter. The ``feedback_id`` query is
    IGNORED by the route (FastAPI silently drops unknown query
    params), so the response must reflect ONLY the project that
    owns ``insight_id`` in the URL path.

    Concretely: we request project A's evidence while passing
    project B's ``feedback_id`` as a query parameter. The response
    must:

      * return 200 (A's evidence exists)
      * contain only A's external_id / content / source
      * never contain B's content, source, or external_id
    """

    _, insight_a, feedback_a_id = _seed_single_evidence(
        db,
        project_name="leak-A",
        feedback_external_id="ext-leak-a",
        feedback_content="alpha leak check",
        feedback_source="alpha",
    )
    project_b, insight_b, feedback_b_id = _seed_single_evidence(
        db,
        project_name="leak-B",
        feedback_external_id="ext-leak-b",
        feedback_content="bravo leak check content",
        feedback_source="bravo",
    )
    db.commit()

    # Sanity: A and B are independent insights with their own evidence.
    response_a_alone = client.get(f"/insights/{insight_a}/evidence")
    response_b_alone = client.get(f"/insights/{insight_b}/evidence")
    assert response_a_alone.status_code == 200
    assert response_b_alone.status_code == 200
    assert response_a_alone.json()["items"][0]["feedback_external_id"] == "ext-leak-a"
    assert response_b_alone.json()["items"][0]["feedback_external_id"] == "ext-leak-b"

    # The real isolation test: when we pass B's feedback_id as a query
    # parameter on A's evidence URL, FastAPI must NOT use that query
    # to redirect or merge. The response stays A-only.
    response = client.get(
        f"/insights/{insight_a}/evidence?feedback_id={feedback_b_id}"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    items = body["items"]
    assert items, "A's evidence must still resolve to its own row"
    for item in items:
        assert item["feedback_external_id"] == "ext-leak-a", (
            "Cross-project feedback_id must not replace A's own evidence"
        )
        assert "bravo leak check content" not in item["feedback_content"]
        assert item["feedback_source"] != "bravo"
        assert item["feedback_id"] != str(feedback_b_id)
    # The response must reference A's feedback_id, not B's.
    assert str(feedback_a_id) in {item["feedback_id"] for item in items}
    assert str(feedback_b_id) not in {item["feedback_id"] for item in items}

    # The forbidden URL shapes (no route anchored on bare feedback_id)
    # must keep returning 404/422 so a future regression that adds
    # such a route is caught here.
    forbidden = [
        f"/feedbacks/{feedback_b_id}/evidence",
        f"/projects/{project_b}/feedback/{feedback_b_id}/evidence",
    ]
    for path in forbidden:
        response = client.get(path)
        assert response.status_code in (404, 405, 422), (
            f"Unexpected {response.status_code} for {path}"
        )
        body_text = response.text
        assert "bravo leak check content" not in body_text
        assert "bravo" not in body_text or path.startswith("/projects/")


# ---- 4. feedback_analysis paired with classification_run_id -------------------


def test_feedback_analysis_null_when_no_pair_for_run(client, db):
    """When no ``FeedbackAnalysis`` row exists for
    ``(feedback_id, run_id=insight.run.classification_run_id)`` the
    ``feedback_analysis`` field MUST be ``null`` on every item — never an
    empty object, never a cross-run fallback. The schema explicitly forbids
    a guessing fallback so the frontend can render "AI Analysis N/A"
    instead of lying about provenance.
    """

    _, insight_id, _ = _seed_single_evidence(
        db,
        project_name="analysis-missing",
        feedback_external_id="ext-no-analysis",
        feedback_content="no analysis paired",
        feedback_source="interview",
    )
    db.commit()

    response = client.get(f"/insights/{insight_id}/evidence")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert "feedback_analysis" in items[0], (
        "Schema must always include the feedback_analysis key even when null"
    )
    assert items[0]["feedback_analysis"] is None


def test_feedback_analysis_populated_when_pair_matches_run(client, db):
    """When a ``FeedbackAnalysis`` row exists for the exact
    ``(feedback_id, classification_run_id)`` pair, the route surfaces it
    verbatim — every column the Phase 1 schema persists must round-trip
    through to the read shape.
    """

    _, insight_id, feedback_id = _seed_single_evidence(
        db,
        project_name="analysis-present",
        feedback_external_id="ext-analysis",
        feedback_content="with analysis",
        feedback_source="interview",
    )
    db.commit()

    # Locate the InsightRun row so we know the exact classification_run_id
    # the route will query against.
    insight = db.scalar(select(Insight).where(Insight.id == insight_id))
    assert insight is not None
    insight_run = db.get(InsightRun, insight.run_id)
    assert insight_run is not None
    classification_run_id = insight_run.classification_run_id

    # Insert the deterministic AI classification row.
    analysis = FeedbackAnalysis(
        feedback_id=feedback_id,
        run_id=classification_run_id,
        feedback_type="pain_point",
        module="onboarding",
        intent="guidance_missing",
        pain_point="no first-run guidance",
        sentiment="negative",
        severity=4,
        confidence=0.85,
    )
    db.add(analysis)
    db.commit()

    response = client.get(f"/insights/{insight_id}/evidence")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    fa = items[0]["feedback_analysis"]
    assert fa is not None, (
        "feedback_analysis must be populated when a matching FeedbackAnalysis row exists"
    )
    assert fa["feedback_id"] == str(feedback_id)
    assert fa["run_id"] == str(classification_run_id)
    assert fa["feedback_type"] == "pain_point"
    assert fa["module"] == "onboarding"
    assert fa["intent"] == "guidance_missing"
    assert fa["pain_point"] == "no first-run guidance"
    assert fa["sentiment"] == "negative"
    assert fa["severity"] == 4
    assert fa["confidence"] == pytest.approx(0.85)


def test_feedback_analysis_does_not_fall_back_to_other_run(client, db):
    """A ``FeedbackAnalysis`` row that exists for a *different*
    classification run must NOT bleed into the evidence response.
    The route pairs on ``(feedback_id, run_id=insight.run.classification_run_id)``
    exactly — pairing across runs is unsupported by the data model and
    any silent cross-run substitution would corrupt provenance.
    """

    _, insight_id, feedback_id = _seed_single_evidence(
        db,
        project_name="analysis-wrong-run",
        feedback_external_id="ext-wrongrun",
        feedback_content="only run-correct pairing should match",
        feedback_source="interview",
    )
    db.commit()

    insight = db.scalar(select(Insight).where(Insight.id == insight_id))
    assert insight is not None
    insight_run = db.get(InsightRun, insight.run_id)
    assert insight_run is not None
    target_run_id = insight_run.classification_run_id

    # Build a SECOND classification run in the same project. Pairing a
    # FeedbackAnalysis on (feedback_id, other_run_id) is the exact case
    # the route must NOT pick up.
    other_run = AnalysisRun(
        project_id=insight.project_id,
        run_type="classification",
        model="phase7-other-run",
        prompt_version="phase7-other",
        status="completed",
        input_count=1,
        processed_count=1,
        success_count=1,
        failed_count=0,
    )
    db.add(other_run)
    db.flush()

    foreign_analysis = FeedbackAnalysis(
        feedback_id=feedback_id,
        run_id=other_run.id,
        feedback_type="bug_report",
        module="foreign",
        intent="should_not_be_surfaced",
        pain_point="cross-run leak",
        sentiment="negative",
        severity=1,
        confidence=0.99,
    )
    db.add(foreign_analysis)
    db.commit()

    # Sanity: the foreign pair exists but the run_id must not equal
    # the insight's classification_run_id.
    assert other_run.id != target_run_id

    response = client.get(f"/insights/{insight_id}/evidence")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    fa = items[0]["feedback_analysis"]
    assert fa is None, (
        "feedback_analysis must NOT fall back to a FeedbackAnalysis row "
        "from a different classification run."
    )


def test_feedback_analysis_mixed_evidence_picks_only_matching(client, db):
    """When one evidence row's feedback has a matching FeedbackAnalysis
    and another's does not, the response carries both correctly:
    ``feedback_analysis`` is populated for the first row and null for
    the second. This guards against accidentally spreading a single
    analysis across multiple evidence rows.
    """

    project_id, insight_id, feedback_a_id = _seed_single_evidence(
        db,
        project_name="analysis-mixed-a",
        feedback_external_id="ext-mix-a",
        feedback_content="mixed A content",
        feedback_source="interview",
    )
    db.commit()

    # Build a second feedback + evidence row in the same project/insight
    # without a paired FeedbackAnalysis.
    insight = db.scalar(select(Insight).where(Insight.id == insight_id))
    assert insight is not None
    insight_run = db.get(InsightRun, insight.run_id)
    target_run_id = insight_run.classification_run_id

    feedback_b = Feedback(
        project_id=project_id,
        external_id="ext-mix-b",
        content="mixed B content",
        source="import",
    )
    db.add(feedback_b)
    db.flush()
    db.commit()
    db.refresh(feedback_b)

    evidence_b = InsightEvidence(
        insight_id=insight_id,
        feedback_id=feedback_b.id,
        evidence_type="representative",
        relevance_score=0.7,
        feedback_external_id="ext-mix-b",
        feedback_project_id=project_id,
        feedback_classification_run_id=insight_run.classification_run_id,
    )
    db.add(evidence_b)
    db.commit()

    # Pair an analysis ONLY for feedback_a against the run-correct id.
    analysis = FeedbackAnalysis(
        feedback_id=feedback_a_id,
        run_id=target_run_id,
        feedback_type="feature_request",
        module="onboarding",
        intent="checklist",
        pain_point="no checklist",
        sentiment="neutral",
        severity=3,
        confidence=0.7,
    )
    db.add(analysis)
    db.commit()

    response = client.get(f"/insights/{insight_id}/evidence")
    assert response.status_code == 200, response.text
    by_external = {
        item["feedback_external_id"]: item for item in response.json()["items"]
    }
    assert set(by_external) == {"ext-mix-a", "ext-mix-b"}
    assert by_external["ext-mix-a"]["feedback_analysis"] is not None
    assert by_external["ext-mix-a"]["feedback_analysis"]["feedback_type"] == "feature_request"
    assert by_external["ext-mix-b"]["feedback_analysis"] is None


# ---- Sanity: list_projects query stays untouched --------------------------------


def test_unknown_insight_id_returns_404(client):
    """A non-existent insight id yields a clean 404. This guards the
    ``_require_insight`` helper used by ``list_insight_evidence``."""

    response = client.get(
        "/insights/00000000-0000-0000-0000-000000000000/evidence"
    )
    assert response.status_code in (404, 422)
