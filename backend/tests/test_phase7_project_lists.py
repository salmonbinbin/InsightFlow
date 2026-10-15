"""Phase 7 D1 — project-scoped Read API tests.

The frontend ``/prds`` and ``/evaluation`` pages need a stable,
server-authoritative project index so a page refresh does not fall
back to ``sessionStorage`` or mock data. The two endpoints added in
Phase 7 D1 are:

* ``GET /projects/{project_id}/prds`` — every PRD version in the
  project, paginated, sorted ``version DESC, id DESC``.
* ``GET /projects/{project_id}/evaluations`` — every EvaluationRun
  in the project, paginated, sorted ``created_at DESC, id DESC``.

The tests below pin the five properties the brief mandates and that
an incidental refactor MUST NOT regress:

1. **Empty list** — a project with no rows returns
   ``{"total": 0, "items": [], ...}``; never ``null`` items or a
   hard error. The frontend uses ``total`` to decide whether to
   render the empty-state copy.
2. **Pagination** — ``limit`` / ``offset`` slicing must not miss
   or duplicate rows across consecutive pages. The sort tiebreaker
   on ``id`` is what makes this stable when two rows share the
   same primary sort field.
3. **Cross-project isolation** — a project id never returns rows
   owned by another project.
4. **404 / 422** — unknown project id and an illegal UUID are
   indistinguishable from any other 4xx that the rest of the API
   surface returns.
5. **Response fields** — the schema is the wire contract; tests
   pin the field set so the frontend TypeScript types can rely on
   it without runtime fallback handling.

The seeding helpers are intentionally minimal — they write ORM
rows directly so a failure points at the project-list endpoint
itself, not at the upstream generation pipeline. The chain matches
what Phase 5 already inserts (see
:func:`backend.tests.test_phase5._seed`).
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import (
    AnalysisRun,
    Cluster,
    ClusterMember,
    Feedback,
    FeedbackAnalysis,
    Insight,
    InsightDecision,
    InsightEvidence,
    InsightRun,
    InsightStatus,
    Project,
)
from app.models.evaluation import (
    EvaluationRun,
    EvaluationRunStatus,
    EvaluationStage,
)
from app.models.requirement_prd import PRD, REQUIREMENT_DRAFT

# ---- Fixtures --------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path):
    """Per-test in-memory SQLite with shared StaticPool + FK enforcement.

    StaticPool is mandatory: without it, the TestClient opens a
    different connection per request, so the seed rows written by
    the fixture are invisible to the API code and every assertion
    sees an empty database.
    """
    url = os.environ.get("TEST_DATABASE_URL", f"sqlite:///{tmp_path}/p7.db")
    is_sqlite = url.startswith("sqlite")
    kwargs: dict = (
        {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
        if is_sqlite
        else {}
    )
    eng = create_engine(url, **kwargs)
    if is_sqlite:

        @event.listens_for(eng, "connect")
        def _fk_on(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def SessionLocal(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def client(SessionLocal):
    """TestClient with ``get_db`` overridden to the per-test engine."""

    def _override():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---- Seeding helpers -------------------------------------------------------


def _make_project(db: Session, *, name: str = "Phase7 D1") -> Project:
    project = Project(name=name, product_name="InsightFlow")
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _seed_decision_chain(
    db: Session, *, project: Project, evidence_external_id: str
) -> InsightDecision:
    """Append a minimal Phase 1—4 chain to ``project``; return the decision.

    The project-list endpoint only reads ``project_id``, but the
    PRD's FK chain is non-negotiable — without the full chain, the
    UNIQUE(requirement_id) constraint and the Insight FK would
    block the seed. This mirrors :func:`test_phase5._seed` so the
    schemas do not drift between test files.
    """
    classification = AnalysisRun(
        project_id=project.id,
        run_type="classification",
        model="p7-test",
        prompt_version="classification-v1",
        status="completed",
        input_count=1,
        processed_count=1,
        success_count=1,
    )
    db.add(classification)
    db.flush()

    feedback = Feedback(
        project_id=project.id,
        external_id=evidence_external_id,
        content="phase7 seed feedback",
    )
    db.add(feedback)
    db.flush()

    analysis = FeedbackAnalysis(
        feedback_id=feedback.id,
        run_id=classification.id,
        feedback_type="ux_issue",
        module="upload",
        intent="multi_select",
        pain_point="repeated taps",
        sentiment="negative",
        severity=4,
        confidence=0.9,
    )
    db.add(analysis)
    db.flush()

    clustering = AnalysisRun(
        project_id=project.id,
        run_type="clustering",
        model="p7-test",
        prompt_version="cluster-review-v4",
        status="completed",
        input_count=1,
        processed_count=1,
        success_count=1,
        cluster_config={"classification_run_id": str(classification.id)},
    )
    db.add(clustering)
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

    db.add(
        ClusterMember(
            cluster_id=cluster.id,
            feedback_analysis_id=analysis.id,
            similarity_score=0.9,
        )
    )
    db.flush()

    insight_run = InsightRun(
        project_id=project.id,
        clustering_run_id=clustering.id,
        classification_run_id=classification.id,
        status="completed",
        model="p7-test",
        prompt_version="insight-generation-v2",
        input_count=1,
        processed_count=1,
        success_count=1,
    )
    db.add(insight_run)
    db.flush()

    insight = Insight(
        run_id=insight_run.id,
        project_id=project.id,
        cluster_id=cluster.id,
        title="Phase7 D1 测试洞察",
        pain_point="种子数据；用于验证项目级列表。",
        affected_segment="测试用户",
        insight="仅用于验证项目级 Read API 的隔离与排序。",
        recommendation="无需产品行动。",
        confidence=0.8,
        status=InsightStatus.DRAFT.value,
        feedback_count=1,
        evidence_count=1,
        source_cluster_external_id=str(cluster.id),
        source_member_external_ids=[evidence_external_id],
        model="p7-test",
        prompt_version="insight-generation-v2",
    )
    db.add(insight)
    db.flush()

    db.add(
        InsightEvidence(
            insight_id=insight.id,
            feedback_id=feedback.id,
            evidence_type="representative",
            relevance_score=0.9,
            feedback_external_id=evidence_external_id,
            feedback_project_id=project.id,
            feedback_classification_run_id=classification.id,
        )
    )
    db.flush()

    decision = InsightDecision(
        insight_id=insight.id,
        project_id=project.id,
        current_score_id=None,
        ai_priority="P1",
        pm_priority="P1",
        status="accepted",
        override_reason=None,
        override_note=None,
        version=1,
    )
    db.add(decision)
    db.commit()
    db.refresh(decision)
    return decision


def _seed_requirement(db: Session, decision: InsightDecision):
    """Append a Requirement row suitable for Phase 5 PRD appending."""
    from app.models.requirement_prd import Requirement

    requirement = Requirement(
        project_id=decision.project_id,
        insight_id=decision.insight_id,
        decision_id=decision.id,
        score_id=None,
        insight_status_snapshot="draft",
        decision_status_snapshot=decision.status,
        ai_priority_snapshot=decision.ai_priority,
        pm_priority="P1",
        pm_override_reason_snapshot=None,
        source_snapshot_hash="p7-seed-snap",
        name="Phase7 D1 测试 Requirement",
        problem="种子数据；用于验证项目级 PRD 列表。",
        target_user="测试用户",
        user_story="验证项目级 PRD 列表的稳定性。",
        solution_hypothesis="不适用。",
        success_metrics=[],
        risks=[],
        evidence_references=[],
        status=REQUIREMENT_DRAFT,
        model="p7-test",
        prompt_version="requirement-generation-v1",
        input_tokens=0,
        output_tokens=0,
        unknown_token_calls=0,
        latency_ms=0,
        attempt_count=1,
        retry_count=0,
        next_version=1,
    )
    db.add(requirement)
    db.commit()
    db.refresh(requirement)
    return requirement


def _seed_prd_versions(
    db: Session,
    *,
    requirement,
    project_id: UUID,
    insight_id: UUID,
    count: int,
) -> list[PRD]:
    """Append ``count`` PRD rows with sequential versions and unique content hashes.

    ``content_hash`` MUST be unique per row because the read schema
    serialises it; the project-list endpoint surfaces it as a stable
    identifier. The payload matches the canonical Phase 5
    PRDContent shape — any extra key would 422 against ``extra=forbid``.
    """
    rows: list[PRD] = []
    for offset in range(count):
        version = offset + 1
        evidence_id = f"ext-p7-v{version}"
        row = PRD(
            requirement_id=requirement.id,
            project_id=project_id,
            insight_id=insight_id,
            version=version,
            status="draft",
            content={
                "background": {"text": "背景", "evidence_ids": [evidence_id]},
                "problem": {"text": "问题", "evidence_ids": [evidence_id]},
                "target_user": {"text": "人群", "evidence_ids": [evidence_id]},
                "user_story": {"text": "故事", "evidence_ids": [evidence_id]},
                "product_goal": {"text": "目标", "evidence_ids": [evidence_id]},
                "proposed_solution": {
                    "text": "方案",
                    "is_proposed": True,
                    "evidence_ids": [evidence_id],
                },
                "functional_requirements": [
                    {"text": "FR1", "evidence_ids": [evidence_id]}
                ],
                "user_flow": {"text": "关键路径", "evidence_ids": [evidence_id]},
                "edge_cases": [
                    {
                        "category": cat,
                        "description": cat,
                        "evidence_ids": [evidence_id],
                    }
                    for cat in (
                        "empty_state",
                        "failure",
                        "retry",
                        "partial_success",
                        "invalid_input",
                        "limit_exceeded",
                    )
                ],
                "acceptance_criteria": [
                    {
                        "text": "AC1",
                        "verifiable": True,
                        "note": "",
                        "evidence_ids": [evidence_id],
                    }
                ],
                "success_metrics": [
                    {
                        "name": "watch 渗透率",
                        "type": "metric_definition",
                        "note": "",
                        "sources": [evidence_id],
                    }
                ],
                "risks": [
                    {
                        "category": "known",
                        "description": "误触进入多选模式。",
                        "evidence_ids": [evidence_id],
                        "mitigation": "撤销提示。",
                    }
                ],
                "mvp_scope": {
                    "in_scope": "MVP 范围。",
                    "out_of_scope": "非 MVP。",
                },
                "evidence_sources": [f"feedback ext-id: {evidence_id}"],
                "evidence_ids": [evidence_id],
            },
            content_hash=f"p7-seed-{requirement.id}-v{version}",
            is_ai_generated_draft=True,
            grounding_warnings=[],
            model="p7-test",
            prompt_version="prd-generation-v1",
            input_tokens=0,
            output_tokens=0,
            unknown_token_calls=0,
            latency_ms=0,
            attempt_count=1,
            retry_count=0,
        )
        db.add(row)
        rows.append(row)
    db.commit()
    for row in rows:
        db.refresh(row)
    return rows


def _seed_eval_run(
    db: Session, *, project_id: UUID
) -> EvaluationRun:
    """Insert a minimal EvaluationRun satisfying every NOT-NULL check."""
    run = EvaluationRun(
        project_id=project_id,
        evaluation_type="classification_metrics",
        dataset_name="demo",
        dataset_version="v1",
        dataset_content_hash="p7-seed-dataset-hash",
        dataset_provenance="demo",
        dataset_split="full",
        dataset_label_status="synthetic_reference_labels_unreviewed_no_human_review",
        stage=EvaluationStage.CLASSIFICATION.value,
        model="p7-test",
        prompt_version="classification-v1",
        pipeline_version=None,
        calculation_version="phase6-test",
        status=EvaluationRunStatus.COMPLETED.value,
        input_count=1,
        processed_count=1,
        success_count=1,
        failed_count=0,
        input_tokens=None,
        output_tokens=None,
        unknown_token_calls=0,
        latency_ms=None,
        total_cost_usd=None,
        attempt_count=1,
        retry_count=0,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


# =============================================================================
# Common helpers
# =============================================================================


# Field-set pin: a contract that the Phase 5 / Phase 6 ``*Read`` schema
# declares. We assert ``>=`` so additive schema changes do not break
# the build. Both lists match what the read DTOs surface today.
_PRD_READ_KEYS = {
    "id",
    "requirement_id",
    "project_id",
    "insight_id",
    "version",
    "status",
    "content",
    "content_hash",
    "is_ai_generated_draft",
    "grounding_warnings",
    "model",
    "prompt_version",
    "input_tokens",
    "output_tokens",
    "unknown_token_calls",
    "latency_ms",
    "attempt_count",
    "retry_count",
    "accepted_at",
    "accepted_by",
    "created_at",
    "updated_at",
}

_EVAL_RUN_READ_KEYS = {
    "id",
    "project_id",
    "evaluation_type",
    "stage",
    "status",
    "dataset_name",
    "dataset_version",
    "dataset_content_hash",
    "dataset_provenance",
    "dataset_split",
    "dataset_label_status",
    "model",
    "prompt_version",
    "pipeline_version",
    "calculation_version",
    "input_count",
    "processed_count",
    "success_count",
    "failed_count",
    "input_tokens",
    "output_tokens",
    "unknown_token_calls",
    "latency_ms",
    "total_cost_usd",
    "attempt_count",
    "retry_count",
    "started_at",
    "completed_at",
    "created_at",
    "error_message",
    "notes",
}


# =============================================================================
# 1. Empty list
# =============================================================================


@pytest.mark.parametrize(
    "endpoint",
    ["/projects/{pid}/prds", "/projects/{pid}/evaluations"],
    ids=["prds", "evaluations"],
)
def test_empty_project_returns_zero_total_and_default_paging(
    client: TestClient, SessionLocal, endpoint: str
):
    """A project with no rows must return total=0 + the default paging envelope.

    The default ``limit=50 / offset=0`` envelope MUST be echoed in
    the response so the frontend can use ``limit`` / ``offset``
    without an extra round-trip; ``items=[]`` is the only shape
    that does not crash the React list renderer.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        url = endpoint.format(pid=str(project.id))
    finally:
        db.close()

    response = client.get(url)
    assert response.status_code == 200
    assert response.json() == {
        "total": 0,
        "items": [],
        "limit": 50,
        "offset": 0,
    }


# =============================================================================
# 2. 404 / 422
# =============================================================================


@pytest.mark.parametrize(
    "endpoint",
    ["/projects/{pid}/prds", "/projects/{pid}/evaluations"],
    ids=["prds", "evaluations"],
)
def test_unknown_project_returns_404_not_empty_list(
    client: TestClient, endpoint: str
):
    """An unknown project id must return 404 — never 200 with an empty list.

    Returning 200 + empty list for a non-existent project would let
    a malicious caller distinguish "deleted / empty" from "never
    existed"; 404 collapses both into the same response and matches
    the rest of the API surface.
    """
    response = client.get(endpoint.format(pid=str(uuid4())))
    assert response.status_code == 404


@pytest.mark.parametrize(
    "endpoint",
    ["/projects/{pid}/prds", "/projects/{pid}/evaluations"],
    ids=["prds", "evaluations"],
)
def test_malformed_uuid_returns_422(client: TestClient, endpoint: str):
    response = client.get(endpoint.format(pid="not-a-uuid"))
    assert response.status_code == 422


# =============================================================================
# 3. Pagination: stable, no missing, no duplicate
# =============================================================================


def _prd_seed(db: Session, *, project: Project) -> set[str]:
    """Seed 5 PRD rows on a single Requirement; return the set of ids."""
    decision = _seed_decision_chain(db, project=project, evidence_external_id="ext-p7-pg")
    requirement = _seed_requirement(db, decision)
    rows = _seed_prd_versions(
        db,
        requirement=requirement,
        project_id=project.id,
        insight_id=decision.insight_id,
        count=5,
    )
    return {str(r.id) for r in rows}


def _eval_seed(db: Session, *, project: Project) -> set[str]:
    """Seed 5 EvaluationRun rows; return the set of ids."""
    runs = [_seed_eval_run(db, project_id=project.id) for _ in range(5)]
    return {str(r.id) for r in runs}


@pytest.mark.parametrize(
    "endpoint_suffix,seeder",
    [
        ("prds", _prd_seed),
        ("evaluations", _eval_seed),
    ],
    ids=["prds", "evaluations"],
)
def test_pagination_no_missing_no_duplicate_with_stable_sort(
    client: TestClient,
    SessionLocal,
    endpoint_suffix: str,
    seeder,
):
    """Walk pages of ``limit=2`` over 5 rows; assert union == seeded set.

    The id tiebreaker is what keeps the page boundaries stable when
    two rows share the same primary sort field (e.g. several
    EvaluationRuns committed in the same transaction share
    ``created_at``; several PRD rows from a multi-version
    Requirement share ``project_id``).

    The ``seeder`` indirection lets the same test body cover both
    endpoints — each seeder encapsulates its own ORM chain.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seeded_ids = seeder(db, project=project)
        url = f"/projects/{project.id}/{endpoint_suffix}"
    finally:
        db.close()

    seen: list[str] = []
    for offset in (0, 2, 4):
        body = client.get(f"{url}?limit=2&offset={offset}").json()
        assert body["total"] == 5
        for item in body["items"]:
            assert item["id"] not in seen, "duplicate across pages"
            seen.append(item["id"])
    assert set(seen) == seeded_ids
    # The final page must be partial when total is not a multiple of limit.
    last = client.get(f"{url}?limit=2&offset=4").json()
    assert len(last["items"]) == 1


# =============================================================================
# 4. Cross-project isolation
# =============================================================================


# ``_isolation_seed`` returns the seeded ids for the project that the
# test will query. It is invoked twice (once per project) so the same
# test body covers both endpoints and the cross-project leakage
# assertion remains uniform.
def _prd_isolation_seed(db: Session, *, project: Project) -> set[str]:
    decision = _seed_decision_chain(db, project=project, evidence_external_id="ext-iso")
    requirement = _seed_requirement(db, decision)
    rows = _seed_prd_versions(
        db, requirement=requirement, project_id=project.id,
        insight_id=decision.insight_id, count=2,
    )
    return {str(r.id) for r in rows}


def _eval_isolation_seed(db: Session, *, project: Project) -> set[str]:
    runs = [_seed_eval_run(db, project_id=project.id) for _ in range(2)]
    return {str(r.id) for r in runs}


@pytest.mark.parametrize(
    "endpoint_suffix,seeder",
    [
        ("prds", _prd_isolation_seed),
        ("evaluations", _eval_isolation_seed),
    ],
    ids=["prds", "evaluations"],
)
def test_isolated_by_project(
    client: TestClient,
    SessionLocal,
    endpoint_suffix: str,
    seeder,
):
    """Rows from project A must NOT leak into a request scoped to project B."""
    db = SessionLocal()
    try:
        project_a = _make_project(db, name="A")
        project_b = _make_project(db, name="B")
        a_ids = seeder(db, project=project_a)
        b_ids = seeder(db, project=project_b)
        a_id, b_id = str(project_a.id), str(project_b.id)
    finally:
        db.close()

    body_a = client.get(f"/projects/{a_id}/{endpoint_suffix}").json()
    body_b = client.get(f"/projects/{b_id}/{endpoint_suffix}").json()
    a_returned = {item["id"] for item in body_a["items"]}
    b_returned = {item["id"] for item in body_b["items"]}
    assert a_returned == a_ids
    assert b_returned == b_ids
    assert a_returned.isdisjoint(b_returned)
    assert all(item["project_id"] == a_id for item in body_a["items"])
    assert all(item["project_id"] == b_id for item in body_b["items"])


# =============================================================================
# 5. Response field set
# =============================================================================


def _prd_field_set_body(client: TestClient, db: Session, project: Project) -> dict:
    decision = _seed_decision_chain(db, project=project, evidence_external_id="ext-fp")
    requirement = _seed_requirement(db, decision)
    _seed_prd_versions(
        db, requirement=requirement, project_id=project.id,
        insight_id=decision.insight_id, count=1,
    )
    return client.get(f"/projects/{project.id}/prds").json()


def _eval_field_set_body(client: TestClient, db: Session, project: Project) -> dict:
    _seed_eval_run(db, project_id=project.id)
    return client.get(f"/projects/{project.id}/evaluations").json()


@pytest.mark.parametrize(
    "endpoint,seed_fn,expected_keys",
    [
        (
            "/projects/{pid}/prds",
            _prd_field_set_body,
            _PRD_READ_KEYS,
        ),
        (
            "/projects/{pid}/evaluations",
            _eval_field_set_body,
            _EVAL_RUN_READ_KEYS,
        ),
    ],
    ids=["prds", "evaluations"],
)
def test_response_field_set_matches_read_schema(
    client: TestClient,
    SessionLocal,
    endpoint: str,
    seed_fn,
    expected_keys: set[str],
):
    """Wire field set must include every key declared by the read DTO.

    The frontend TypeScript types are derived from the Pydantic
    schemas; the test pins the field set so an accidental schema
    rename breaks here, not on the FE build. ``>=`` lets additive
    schema changes (audit columns, …) keep the build green.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        body = seed_fn(client, db, project)
    finally:
        db.close()

    assert body["total"] == 1
    item = body["items"][0]
    assert expected_keys.issubset(set(item.keys()))
    # ``project_id`` is the natural cross-endpoint join key for the FE;
    # pin it here too so a future schema change that drops the field
    # breaks the field-set test, not the FE build.
    assert item["project_id"] == str(project.id)


# =============================================================================
# 6. limit / offset bounds
# =============================================================================


@pytest.mark.parametrize(
    "endpoint_suffix",
    ["prds", "evaluations"],
    ids=["prds", "evaluations"],
)
@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=201", "limit=-1", "offset=-1"],
)
def test_pagination_bounds_return_422(
    client: TestClient, endpoint_suffix: str, query: str
):
    """Out-of-range ``limit`` (≤0, >200) and negative ``offset`` must 422.

    The brief pins ``limit ∈ [1, 200]`` and ``offset ≥ 0``; the
    route layer enforces this via FastAPI ``Query(ge=..., le=...)``
    so the contract is enforced before any DB work — the project
    does NOT need to exist for the 422 to fire.
    """
    url = f"/projects/{uuid4()}/{endpoint_suffix}"
    assert client.get(f"{url}?{query}").status_code == 422


# =============================================================================
# 7. Default paging envelope + sort direction (smoke)
# =============================================================================


def _prd_sort_seed(db: Session, *, project: Project) -> set[str]:
    decision = _seed_decision_chain(db, project=project, evidence_external_id="ext-d")
    requirement = _seed_requirement(db, decision)
    rows = _seed_prd_versions(
        db, requirement=requirement, project_id=project.id,
        insight_id=decision.insight_id, count=3,
    )
    return {str(r.id) for r in rows}


def _eval_sort_seed(db: Session, *, project: Project) -> set[str]:
    runs = [_seed_eval_run(db, project_id=project.id) for _ in range(5)]
    return {str(r.id) for r in runs}


@pytest.mark.parametrize(
    "endpoint_suffix,seeder,expected_total",
    [
        ("prds", _prd_sort_seed, 3),
        ("evaluations", _eval_sort_seed, 5),
    ],
    ids=["prds", "evaluations"],
)
def test_default_limit_and_sort_is_stable(
    client: TestClient,
    SessionLocal,
    endpoint_suffix: str,
    seeder,
    expected_total: int,
):
    """The default ``limit=50`` is echoed back; the secondary ``id`` sort keeps
    paging deterministic when the primary sort key ties.

    For PRDs, ``version`` is the primary sort; for EvaluationRuns,
    ``created_at`` is the primary sort and ties when several runs
    are committed in the same transaction. Either way, the
    ``id DESC`` tiebreaker is what keeps page boundaries stable.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seeder(db, project=project)
        url = f"/projects/{project.id}/{endpoint_suffix}"
    finally:
        db.close()

    body = client.get(url).json()
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["total"] == expected_total

    # Walk every page of ``limit=2``; the loop must reach the final
    # partial page so a ``total`` that is not a multiple of ``limit``
    # (e.g. 5 EvaluationRuns) is fully covered. Reading only
    # ``offset=0, 2`` would silently stop at 4 rows and the union
    # check would falsely report a missing row.
    page_ids: list[set[str]] = []
    for offset in range(0, expected_total, 2):
        page = client.get(f"{url}?limit=2&offset={offset}").json()
        page_ids.append({item["id"] for item in page["items"]})

    # No two pages share an id (no duplicates) and the union covers
    # every row the un-paged envelope reported (no missing).
    union: set[str] = set()
    for page in page_ids:
        assert union.isdisjoint(page)
        union |= page
    assert union == {item["id"] for item in body["items"]}
