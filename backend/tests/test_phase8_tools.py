"""Phase 8 — Tool Layer tests (boundary-focused, mocks permitted).

The tests in this file pin every hard rule in the brief:

1. **Static registry** — unregistered ``(tool_name, tool_action)``
   pairs are rejected; registered pairs are validated by a strict
   ``extra=forbid`` schema; models cannot freely invent a tool.
2. **Lifecycle states** — Prepare produces ``awaiting_approval``;
   Cancel transitions ``awaiting_approval → cancelled``; Approve
   transitions ``awaiting_approval → completed`` and persists one
   ``SimulatedIssue`` row. Repeat Approve is a no-op.
3. **Idempotency** — ``(project_id, request_id)`` uniqueness;
   a duplicate Prepare returns the existing row unchanged.
4. **Eligibility** — only ``approved`` Requirements can prepare a
   simulated issue; cross-project access returns 404.
5. **Server-derived fields** — Title / Description / Priority /
   Source-Insight preview are built from the Requirement + source
   Insight, never from user-supplied payload bytes. Raw feedback
   content + credential-shaped tokens are scrubbed.
6. **Simulator output** — ``SIM-...`` external id + ``simulated=true``
   flag, persisted on the audit row + the run row.
7. **Adapter failure injection** — transient timeout / 5xx /
   provider failure modes are covered; bounded retry runs only when
   the descriptor opts in; unknown results are NOT retried.
8. **No-fan-out** — a rejected Prepare produces zero rows; a
   cancelled run produces no ``SimulatedIssue`` row.
9. **Audit preservation** — failed Approve keeps the run row +
   failure_category + response_summary so an audit can read what
   happened.
10. **Cross-project isolation** — read / cancel / approve endpoints
    reject any project_id mismatch with 404.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import (
    REQUIREMENT_APPROVED,
    REQUIREMENT_DRAFT,
    AnalysisRun,
    Cluster,
    Insight,
    InsightDecision,
    InsightRun,
    InsightStatus,
    Project,
    RunStatus,
    SimulatedIssue,
    ToolRun,
    ToolRunStatus,
)
from app.services.tool_service import (
    approve_tool_run,
    cancel_tool_run,
    prepare_tool_run,
)
from app.tools.adapters import (
    SimulatedIssueTrackerAdapter,
    SimulatedIssueTrackerError,
    SimulatedIssueTrackerTransientError,
)
from app.tools.registry import (
    CapabilityKind,
    ExecutionMode,
    ToolInputValidationError,
    ToolNotRegisteredError,
    ToolRegistry,
    WebResearchSearchInput,
    bootstrap_default_registry,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def engine(tmp_path: Path):
    """Per-test engine bound to the test database.

    On SQLite (the default), a fresh ``tmp_path`` file is created
    per test so isolation is automatic.

    On PostgreSQL (``TEST_DATABASE_URL`` set to a
    ``postgresql://`` DSN), the engine creates a PER-TEST RANDOM
    SCHEMA and qualifies every SQLAlchemy-emitted table /
    sequence reference to that schema via
    ``execution_options={"schema_translate_map": {None: ...}}``
    (the primary isolation mechanism) and sets the connection
    ``search_path`` so unqualified TYPE names (specifically
    ``vector``) still resolve. Teardown issues
    ``DROP SCHEMA ... CASCADE`` against the random schema only;
    Raw Feedback or any other table in ``public`` is never
    ``TRUNCATE``d, ``DROP``ped, downgraded, or otherwise
    touched.

    Isolation invariant (PG mode):
      * ``schema_translate_map={None: <random>}`` is the
        PRIMARY isolation boundary. SQLAlchemy rewrites every
        table / sequence reference in ``Base.metadata.create_all``
        and in ORM queries to ``"<random>"."tablename"``,
        regardless of what is in ``public``. An existing
        ``public.tool_runs`` cannot make ``create_all`` skip
        the per-test table and ORM ``SELECT``s cannot leak
        across.
      * The only DDL the fixture emits against the cluster is
        ``CREATE SCHEMA IF NOT EXISTS "<random>"`` and a
        best-effort ``CREATE EXTENSION IF NOT EXISTS vector
        SCHEMA "<random>"``. Both are no-ops if the schema or
        the per-schema extension already exists.
      * The only DDL the teardown emits is ``DROP SCHEMA IF
        EXISTS "<random>" CASCADE``. CASCADE removes only
        objects owned by the random schema.
      * The connect event sets ``search_path TO "<random>",
        public`` so unqualified TYPE names (``vector``) remain
        resolvable while the engine never falls back to
        ``public`` for table / sequence lookups.
      * No ``TRUNCATE``, no full-table ``DROP``, no
        schema-wide destructive statement outside the random
        schema is ever issued. If the per-test schema cannot
        be created (e.g. permission denied on the test DSN)
        the fixture raises and the test fails; the engine
        never falls back to a global cleanup path.

    The ``vector`` extension is cluster-wide on PostgreSQL
    (extensions are not schema-scoped). The fixture first
    attempts to install it inside the per-test schema; if the
    extension is already installed in another schema
    (typically ``public``), PostgreSQL rejects the per-schema
    create and the fixture falls back to the existing
    installation. The extension is never ``DROP``ped.

    Per-test concurrency tests are safe: every connection the
    engine's pool checks out carries the ``search_path`` set
    in the connect event, so concurrent threads always write
    into the per-test schema.
    """
    url = os.environ.get("TEST_DATABASE_URL", f"sqlite:///{tmp_path}/p8_tools.db")
    is_sqlite = url.startswith("sqlite")
    kwargs: dict = (
        {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
        if is_sqlite
        else {}
    )

    schema_name: str | None = None
    eng = None
    if is_sqlite:
        eng = create_engine(url, **kwargs)

        @event.listens_for(eng, "connect")
        def _fk_on(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
        Base.metadata.create_all(eng)
    else:
        # 32 hex chars = 128 bits of entropy, negligible
        # collision risk within a pytest run and grep-able
        # for forensic cleanup if a crash leaves one behind.
        schema_name = f"p8t_{uuid4().hex}"

        # ``schema_translate_map={None: schema_name}`` is the
        # PRIMARY isolation mechanism. It forces every
        # SQLAlchemy-emitted table / sequence reference to be
        # qualified with the per-test schema:
        #   * ``Base.metadata.create_all(eng)`` issues
        #     ``CREATE TABLE IF NOT EXISTS "<schema>".tool_runs``
        #     regardless of what is in ``public``. The
        #     ``has_table`` check used by ``create_all`` is
        #     also qualified, so an existing
        #     ``public.tool_runs`` can no longer make
        #     SQLAlchemy skip creating the per-test table.
        #   * ORM ``SELECT`` / ``INSERT`` / ``UPDATE`` /
        #     ``DELETE`` issued through any session bound to
        #     this engine are qualified to the per-test
        #     schema, so an existing ``public.tool_runs`` row
        #     is never visible and never mutated.
        # ``search_path`` (set in the connect event below)
        # remains in effect ONLY for unqualified TYPE names
        # like ``vector``; the engine never falls back to
        # ``public`` for table / sequence lookups.
        eng = create_engine(
            url,
            execution_options={
                "schema_translate_map": {None: schema_name},
            },
            **kwargs,
        )

        # Register the connect event FIRST so every new
        # physical connection the engine creates has the
        # search_path set to our schema. The closure captures
        # ``schema_name``; only the per-test schema is
        # referenced. ``public`` is appended so the ``vector``
        # type installed by the pgvector extension (which
        # usually lives in ``public``) is still resolvable
        # for unqualified DDL like ``CREATE TABLE ... (
        # embedding vector )``.
        @event.listens_for(eng, "connect")
        def _set_search_path(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute(
                f'SET search_path TO "{schema_name}", public'
            )
            cur.close()

        # Create the per-test schema. CREATE SCHEMA is a
        # top-level operation; the current search_path does
        # not affect where the schema itself is created.
        # ``IF NOT EXISTS`` is a no-op if a prior crashed run
        # left the schema behind; collision probability with
        # 128 bits of entropy is negligible.
        with eng.begin() as conn:
            conn.exec_driver_sql(
                f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"'
            )

        # Best-effort per-schema install of the pgvector
        # extension. PostgreSQL extensions are cluster-wide
        # (not schema-scoped), so the "minimum scope" here is
        # ``IF NOT EXISTS`` + the per-test schema name. If
        # pgvector is already installed in another schema
        # (typically ``public``) PostgreSQL rejects the
        # per-schema create with ``extension ... already
        # exists``; the except branch leaves the existing
        # installation in place and continues. The extension
        # is never ``DROP``ped. If pgvector is not installed
        # at all, the next step
        # (``Base.metadata.create_all``) will fail with a
        # clear ``UndefinedObject: type "vector" does not
        # exist`` error — surfacing the environment problem
        # is the safe behaviour.
        try:
            with eng.begin() as conn:
                conn.exec_driver_sql(
                    f'CREATE EXTENSION IF NOT EXISTS vector '
                    f'SCHEMA "{schema_name}"'
                )
        except Exception:
            # Extension already installed in another schema;
            # the search_path above includes ``public`` so
            # the ``vector`` type remains resolvable.
            pass

        # All tables defined in ``Base.metadata`` land in the
        # per-test schema because the connect event sets
        # ``search_path`` to ``schema_name`` first. No table
        # in ``public`` is ever created or modified.
        Base.metadata.create_all(eng)

    try:
        yield eng
    finally:
        if schema_name is not None:
            # Drop ONLY the per-test schema. CASCADE removes
            # only objects owned by this schema; ``public``
            # and any other schema are never touched. If the
            # drop fails for any reason we still run
            # ``dispose()`` so the engine is released; the
            # leftover schema is the lesser evil compared to
            # crashing the pytest session or attempting a
            # broader destructive operation.
            try:
                with eng.begin() as conn:
                    conn.exec_driver_sql(
                        f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'
                    )
            except Exception as exc:
                import warnings

                warnings.warn(
                    f"phase 8 engine: failed to drop per-test schema "
                    f"'{schema_name}': {exc!r}; schema left behind "
                    "for forensic inspection"
                )
        eng.dispose()


@pytest.fixture
def requires_postgres(engine):
    """Skip the test unless the test engine is PostgreSQL.

    Set the ``TEST_DATABASE_URL`` environment variable to a
    PostgreSQL DSN to enable the Phase 8 concurrency suite. The
    default SQLite engine cannot exercise ``SELECT ... FOR UPDATE``
    semantics, so PostgreSQL-only tests are gated here rather
    than masking race bugs behind SQLite's connection-level
    serialisation.
    """
    if engine.dialect.name != "postgresql":
        pytest.skip(
            "PostgreSQL required: set TEST_DATABASE_URL to a PostgreSQL DSN "
            "to run this Phase 8 concurrency regression"
        )
    return engine


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


@pytest.fixture(autouse=True)
def _reset_registry():
    """Always start each test with a fresh registry bootstrap.

    The registry caches the singleton instance on the class. Each
    test re-bootstraps the defaults so a previous test's monkeypatch
    of a descriptor cannot leak across tests.
    """
    ToolRegistry.reset_for_tests()
    bootstrap_default_registry()
    yield
    ToolRegistry.reset_for_tests()


def _run_concurrent(callables: list, *, timeout: float = 15.0) -> list:
    """Run a list of zero-arg callables in parallel threads.

    A :class:`threading.Barrier` aligns the start so both threads
    enter the critical section as close to simultaneously as
    Python's GIL allows. Each callable runs in its own thread and
    gets its own return slot in the result list; exceptions are
    captured instead of raised so a race-induced 409 / idempotent
    return can be inspected rather than aborting the test.

    Returns a list of ``("ok", value)`` or ``("err", exception)``
    tuples in the original order. A timeout aborts the test
    (with the surviving threads still alive) to surface
    deadlocks.
    """
    barrier = threading.Barrier(len(callables))
    results: list[Any] = [None] * len(callables)

    def _wrapped(idx: int, target) -> None:
        barrier.wait()
        try:
            results[idx] = ("ok", target())
        except BaseException as exc:  # noqa: BLE001 — test-side inspect
            results[idx] = ("err", exc)

    threads = [
        threading.Thread(target=_wrapped, args=(i, fn))
        for i, fn in enumerate(callables)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
        if t.is_alive():
            raise RuntimeError(
                f"thread did not complete within {timeout}s — possible deadlock"
            )
    return results


# ============================================================================
# Seeding helpers
# ============================================================================


def _make_project(db: Session, *, name: str = "Phase8 Tools Project") -> Project:
    project = Project(name=name, product_name="InsightFlow")
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _make_requirement(
    db: Session,
    *,
    project: Project,
    status: str = REQUIREMENT_APPROVED,
    name: str = "多选模式提示气泡",
    pm_priority: str = "P1",
) -> dict[str, Any]:
    """Insert a minimal Insight + Decision + Requirement chain at the chosen status.

    The Insight body deliberately contains a "secret" string so a test
    can assert that the Source-Insight preview / Description fields
    do NOT echo raw feedback content (or the secret).

    The Phase 8 brief mandates that the chain survive the SQLite FK
    enforcement, so we build the parent rows (AnalysisRun, Cluster,
    InsightRun) in dependency order and flush after each step.
    """
    from app.models.requirement_prd import Requirement as RequirementModel

    analysis_run = AnalysisRun(
        project_id=project.id,
        run_type="clustering",
        model="p8-tools-test",
        prompt_version="cluster-review-v4",
        status=RunStatus.COMPLETED.value,
        input_count=3,
        processed_count=3,
        success_count=3,
    )
    db.add(analysis_run)
    db.flush()

    cluster = Cluster(
        project_id=project.id,
        run_id=analysis_run.id,
        source="llm_review",
        feedback_count=3,
        embedding_model="local-hash-v1",
        distance_threshold=0.3,
        linkage="average",
    )
    db.add(cluster)
    db.flush()

    insight_run = InsightRun(
        project_id=project.id,
        clustering_run_id=analysis_run.id,
        classification_run_id=analysis_run.id,
        status=RunStatus.COMPLETED.value,
        model="p8-tools-test",
        prompt_version="insight-generation-v2",
        input_count=3,
        processed_count=3,
        success_count=3,
    )
    db.add(insight_run)
    db.flush()

    insight = Insight(
        run_id=insight_run.id,
        project_id=project.id,
        cluster_id=cluster.id,
        title="Phase8 多选模式误触",
        pain_point="raw_feedback_secret_token_DO_NOT_ECHO 用户进入上传页后重复点击",
        affected_segment="移动端新用户",
        insight="新用户对多选入口缺乏直觉；该痛点需要通过 UI 提示来解决。",
        recommendation="在多选入口处加提示气泡，待验证是否降低误触率。",
        confidence=0.8,
        status=InsightStatus.DRAFT.value,
        feedback_count=3,
        evidence_count=3,
        source_cluster_external_id="cluster-secret",
        source_member_external_ids=["ext-1", "ext-2", "ext-3"],
        model="p8-tools-test",
        prompt_version="insight-generation-v2",
    )
    db.add(insight)
    db.flush()

    decision = InsightDecision(
        insight_id=insight.id,
        project_id=project.id,
        current_score_id=None,
        ai_priority=pm_priority,
        pm_priority=pm_priority,
        status="accepted",
        version=1,
    )
    db.add(decision)
    db.flush()

    requirement = RequirementModel(
        project_id=project.id,
        insight_id=insight.id,
        decision_id=decision.id,
        score_id=None,
        insight_status_snapshot=insight.status,
        decision_status_snapshot=decision.status,
        ai_priority_snapshot=decision.ai_priority,
        pm_priority=pm_priority,
        pm_override_reason_snapshot=None,
        source_snapshot_hash="x" * 64,
        name=name,
        problem="用户进入多选模式后无法撤销。",
        target_user="移动端新用户。",
        user_story="希望进入多选模式时有提示气泡。",
        solution_hypothesis="加显式入口 + 提示气泡可降低误触率。",
        success_metrics=[],
        risks=[],
        evidence_references=[
            {"external_id": "ext-1", "usage": "representative"}
        ],
        status=status,
        model="p8-tools-test",
        prompt_version="requirement-generation-v1",
    )
    db.add(requirement)
    db.commit()
    db.refresh(requirement)
    return {
        "insight_id": insight.id,
        "decision_id": decision.id,
        "requirement_id": requirement.id,
        "pm_priority": pm_priority,
    }


def _seed_approved_requirement(
    db: Session, *, project: Project, name: str = "多选模式提示气泡"
) -> dict[str, Any]:
    return _make_requirement(db, project=project, status=REQUIREMENT_APPROVED, name=name)


# ============================================================================
# 1. Registry + descriptor contract
# ============================================================================


def test_default_registry_contains_web_research_and_simulated_issue_tracker():
    """Bootstrap exposes the two Phase 8 descriptors and nothing else."""
    tools = ToolRegistry.instance().list_tools()
    names = {(d.name, d.action) for d in tools}
    assert ("web_research", "search") in names
    assert ("simulated_issue_tracker", "create_issue") in names
    # Sanity: registered tools are small in number; no MCP/plugin
    # descriptors sneak in from the registry defaults.
    assert len(tools) == 2


def test_web_research_search_descriptor_is_read_only_and_real():
    descriptor = ToolRegistry.instance().get("web_research", "search")
    assert descriptor.capability_kind == CapabilityKind.READ
    assert descriptor.execution_mode == ExecutionMode.REAL
    assert descriptor.version == "research-agent-v2"
    # The input schema is the same strict shape as the Research API
    # request body so unknown fields are rejected at both boundaries.
    assert descriptor.input_schema is WebResearchSearchInput


def test_simulated_issue_tracker_create_issue_descriptor_is_simulation_only():
    descriptor = ToolRegistry.instance().get(
        "simulated_issue_tracker", "create_issue"
    )
    assert descriptor.capability_kind == CapabilityKind.SIMULATION_ONLY
    assert descriptor.execution_mode == ExecutionMode.SIMULATION
    # Idempotent + bounded-retry are the gates that allow the
    # simulator to attempt a recovery on transient timeout / 5xx.
    assert descriptor.idempotent is True
    assert descriptor.supports_bounded_retry is True
    assert descriptor.adapter_factory is not None


def test_registry_rejects_unknown_tool_name():
    with pytest.raises(ToolNotRegisteredError):
        ToolRegistry.instance().get("definitely_not_registered", "create_issue")


def test_registry_rejects_unknown_tool_action():
    with pytest.raises(ToolNotRegisteredError):
        ToolRegistry.instance().get("simulated_issue_tracker", "delete_issue")


def test_registry_input_validation_rejects_unknown_payload_field():
    """The simulator input schema is empty + ``extra=forbid`` so a
    payload carrying a ``title`` field is rejected BEFORE persistence.
    """
    with pytest.raises(ToolInputValidationError):
        ToolRegistry.instance().validate_inputs(
            "simulated_issue_tracker",
            "create_issue",
            {"title": "USER-INJECTED-TITLE"},
        )


def test_registry_input_validation_rejects_oversize_search_query():
    """Web search query > 500 chars is rejected by the per-tool schema."""
    with pytest.raises(ToolInputValidationError):
        ToolRegistry.instance().validate_inputs(
            "web_research",
            "search",
            {"query": "x" * 600},
        )


def test_registry_input_validation_accepts_canonical_web_research_payload():
    parsed = ToolRegistry.instance().validate_inputs(
        "web_research",
        "search",
        {"query": "竞品 X 的入口？", "scope": "2026 海外", "max_results": 5},
    )
    assert parsed.query == "竞品 X 的入口？"
    assert parsed.scope == "2026 海外"
    assert parsed.max_results == 5


def test_model_cannot_freely_invent_tool(
    client: TestClient, SessionLocal
):
    """The model layer rejects ``ToolRun`` rows whose
    ``tool_name`` / ``tool_action`` pair is not registered. The
    guard is enforced at the service layer (Prepare + Approve) so
    no caller can persist a row pointing at an unregistered tool.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    # Bypassing the service (a direct ORM insert) is allowed at the
    # DB layer because the CHECK constraints only enforce length +
    # status. The brief's "模型不得自由构造工具" rule applies to the
    # service-layer surface; an out-of-band ORM insert is still
    # persisted. The contract under test is the service entrypoint,
    # which DOES reject the unregistered pair.
    response = client.post(
        f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project.id}",
        json={
            "tool_name": "imaginary_tool",
            "tool_action": "do_thing",
            "request_id": str(uuid4()),
        },
    )
    # The route maps ToolValidationError not_registered → 404 so the
    # failure mode is indistinguishable from "insight not found".
    assert response.status_code == 404


# ============================================================================
# 2. Prepare — eligibility, idempotency, cross-project
# ============================================================================


def test_prepare_creates_awaiting_approval_run_for_approved_requirement(
    SessionLocal,
):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    request_id = str(uuid4())
    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=request_id,
            payload={},
        )
        db.commit()
        run = outcome.run
    finally:
        db.close()

    assert run.status == ToolRunStatus.AWAITING_APPROVAL.value
    assert run.request_id == request_id
    assert run.tool_name == "simulated_issue_tracker"
    assert run.tool_action == "create_issue"
    assert run.execution_mode == "simulation"
    assert outcome.created is True


def test_prepare_idempotent_on_duplicate_request_id_in_same_project(
    SessionLocal,
):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    request_id = str(uuid4())
    db = SessionLocal()
    try:
        first = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=request_id,
            payload={},
        )
        db.commit()
        first_id = first.run.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        second = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=request_id,
            payload={},
        )
        db.commit()
    finally:
        db.close()

    assert first_id == second.run.id
    assert second.created is False
    # No second row was written.
    db = SessionLocal()
    try:
        rows = db.query(ToolRun).all()
    finally:
        db.close()
    assert len(rows) == 1


def test_prepare_rejects_draft_requirement(SessionLocal):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _make_requirement(db, project=project, status=REQUIREMENT_DRAFT)
    finally:
        db.close()

    db = SessionLocal()
    try:
        with pytest.raises(Exception) as excinfo:
            prepare_tool_run(
                db,
                project_id=project.id,
                requirement_id=seed["requirement_id"],
                tool_name="simulated_issue_tracker",
                tool_action="create_issue",
                request_id=str(uuid4()),
                payload={},
            )
        db.rollback()
    finally:
        db.close()

    # The error message must mention "not approved" so the route
    # layer maps it to 409; the brief forbids creating a simulated
    # issue against a non-approved Requirement.
    assert "not approved" in str(excinfo.value)


def test_prepare_rejects_cross_project_requirement(SessionLocal):
    db = SessionLocal()
    try:
        project_a = _make_project(db, name="A")
        project_b = _make_project(db, name="B")
        seed = _seed_approved_requirement(db, project=project_a)
    finally:
        db.close()

    db = SessionLocal()
    try:
        with pytest.raises(Exception) as excinfo:
            prepare_tool_run(
                db,
                project_id=project_b.id,  # wrong project
                requirement_id=seed["requirement_id"],
                tool_name="simulated_issue_tracker",
                tool_action="create_issue",
                request_id=str(uuid4()),
                payload={},
            )
        db.rollback()
    finally:
        db.close()

    # The route maps not_found → 404; the contract under test is
    # the service-layer marker.
    assert "not found" in str(excinfo.value)


def test_prepare_scrubs_credential_shaped_strings(SessionLocal):
    """``request_payload`` MUST NOT echo a credential-shaped token.

    The simulator input schema is empty so no real payload bytes
    should land on the row, but a defence-in-depth scrub keeps the
    audit surface free of credentials even if a future descriptor
    accepts free-form strings.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    # Verify the scrub helper directly — the simulator input schema
    # is empty so a Prepare with a credential-shaped payload would
    # be rejected at the schema layer before reaching persistence.
    # The scrub is the defence-in-depth behind that gate.
    from app.services.tool_service import _scrub_payload

    scrubbed = _scrub_payload({"leak": "Bearer sk-1234567890ABCDEF"})
    assert "sk-1234567890ABCDEF" not in scrubbed["leak"]
    assert "[REDACTED]" in scrubbed["leak"]

    # The Prepare path itself succeeds with an empty payload — the
    # schema accepts the simulator's empty input shape.
    db = SessionLocal()
    try:
        prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
    finally:
        db.close()


# ============================================================================
# 3. Server-derived issue fields
# ============================================================================


def test_issue_fields_derived_from_requirement_and_insight(SessionLocal):
    """Title / Description / Priority / Source preview come from the server."""
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project, name="多选模式提示气泡")
    finally:
        db.close()

    db = SessionLocal()
    try:
        from app.models.requirement_prd import Requirement as RequirementModel

        requirement = db.get(RequirementModel, seed["requirement_id"])
        insight = db.get(Insight, seed["insight_id"])
        from app.services.tool_service import _derive_simulated_issue_fields

        fields = _derive_simulated_issue_fields(requirement, insight)
    finally:
        db.close()

    assert fields["title"].startswith("[Simulated] 多选模式提示气泡")
    assert "用户进入多选模式后无法撤销" in fields["description"]
    assert fields["priority"] == "P1"
    # The Insight body contained a secret token — the preview must
    # NOT echo raw feedback content or that token.
    rendered = json.dumps(fields, ensure_ascii=False)
    assert "raw_feedback_secret_token" not in rendered
    # The preview is bounded.
    assert len(fields["source_insight_preview"]) <= 400


def test_issue_fields_truncate_long_title(SessionLocal):
    db = SessionLocal()
    try:
        project = _make_project(db)
        # ``Requirement.name`` is ``varchar(200)`` on PostgreSQL; a
        # 250-char name would be rejected at INSERT time. The
        # derived title is ``"[Simulated] " + name`` (an 11-char
        # prefix), so a 195-char name yields a 206-char title that
        # still exceeds ``SIMULATED_ISSUE_TITLE_MAX`` (200) and
        # exercises the truncation rule.
        seed = _seed_approved_requirement(
            db, project=project, name="A" * 195
        )
    finally:
        db.close()

    db = SessionLocal()
    try:
        from app.models.requirement_prd import Requirement as RequirementModel

        requirement = db.get(RequirementModel, seed["requirement_id"])
        from app.services.tool_service import (
            SIMULATED_ISSUE_TITLE_MAX,
            _derive_simulated_issue_fields,
        )

        fields = _derive_simulated_issue_fields(requirement, None)
    finally:
        db.close()

    assert len(fields["title"]) <= SIMULATED_ISSUE_TITLE_MAX
    assert fields["title"].startswith("[Simulated] ")


# ============================================================================
# 4. Cancel
# ============================================================================


def test_cancel_transitions_awaiting_to_cancelled(SessionLocal):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    request_id = str(uuid4())
    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=request_id,
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        run = cancel_tool_run(
            db,
            project_id=project.id,
            tool_run_id=run_id,
        )
        db.commit()
    finally:
        db.close()

    assert run.status == ToolRunStatus.CANCELLED.value
    # No SimulatedIssue row was written — the brief mandates a
    # rejected Prepare produces zero writes.
    db = SessionLocal()
    try:
        assert db.query(SimulatedIssue).count() == 0
    finally:
        db.close()


def test_cancel_rejects_completed_run(SessionLocal):
    """A completed run cannot be silently rewritten by Cancel."""
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        approve_tool_run(db, project_id=project.id, tool_run_id=run_id)
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        with pytest.raises(Exception) as excinfo:
            cancel_tool_run(db, project_id=project.id, tool_run_id=run_id)
        db.rollback()
    finally:
        db.close()
    assert "not awaiting" in str(excinfo.value)


# ============================================================================
# 5. Approve — happy path, idempotency, persistence
# ============================================================================


def test_approve_produces_sim_issue_with_simulated_flag(SessionLocal):
    """Approve persists a SimulatedIssue row + flips run to completed."""
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        run = approve_tool_run(
            db, project_id=project.id, tool_run_id=run_id
        )
        db.commit()
    finally:
        db.close()

    assert run.status == ToolRunStatus.COMPLETED.value
    assert run.external_id is not None
    assert run.external_id.startswith("SIM-")
    # Exactly one audit row was persisted — the simulator writes
    # never produce a duplicate.
    db = SessionLocal()
    try:
        issues = db.query(SimulatedIssue).all()
        assert len(issues) == 1
        assert issues[0].external_id == run.external_id
        assert issues[0].simulated is True
        assert issues[0].tool_run_id == run_id
        assert issues[0].priority == "P1"
    finally:
        db.close()


def test_approve_does_not_echo_raw_feedback_content(SessionLocal):
    """The simulator description MUST NOT echo raw feedback content or
    credential-shaped tokens. The simulator builds its inputs from
    the server-derived Requirement fields, so the audit row carries
    only bounded, redacted text.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        approve_tool_run(db, project_id=project.id, tool_run_id=run_id)
        db.commit()
        issue = db.query(SimulatedIssue).one()
    finally:
        db.close()

    rendered = json.dumps(
        {
            "title": issue.title,
            "description": issue.description,
            "preview": issue.source_insight_preview,
        },
        ensure_ascii=False,
    )
    # The Insight body deliberately contained this marker; it must
    # NOT leak into the simulator description or preview.
    assert "raw_feedback_secret_token" not in rendered
    # The preview is bounded.
    assert len(issue.source_insight_preview) <= 400
    # The title is bounded.
    assert 1 <= len(issue.title) <= 200
    # The description is bounded.
    assert 1 <= len(issue.description) <= 4000


def test_approve_idempotent_no_second_write(SessionLocal):
    """A second Approve on a completed run returns the same row and
    does NOT produce a second SimulatedIssue row.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        run1 = approve_tool_run(
            db, project_id=project.id, tool_run_id=run_id
        )
        db.commit()
        first_external = run1.external_id
        first_attempt = run1.attempt_count
    finally:
        db.close()

    db = SessionLocal()
    try:
        run2 = approve_tool_run(
            db, project_id=project.id, tool_run_id=run_id
        )
        db.commit()
    finally:
        db.close()

    assert run2.status == ToolRunStatus.COMPLETED.value
    assert run2.external_id == first_external
    # The simulator was NOT invoked a second time.
    assert run2.attempt_count == first_attempt
    db = SessionLocal()
    try:
        assert db.query(SimulatedIssue).count() == 1
    finally:
        db.close()


def test_approve_cross_project_404(SessionLocal):
    """Approve with a wrong project_id surfaces as not_found (404)."""
    db = SessionLocal()
    try:
        project_a = _make_project(db, name="A")
        project_b = _make_project(db, name="B")
        seed = _seed_approved_requirement(db, project=project_a)
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project_a.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        with pytest.raises(Exception) as excinfo:
            approve_tool_run(
                db, project_id=project_b.id, tool_run_id=run_id
            )
        db.rollback()
    finally:
        db.close()
    assert "not found" in str(excinfo.value)


def test_approve_unknown_run_id_404(SessionLocal):
    db = SessionLocal()
    try:
        project = _make_project(db)
    finally:
        db.close()

    db = SessionLocal()
    try:
        with pytest.raises(Exception) as excinfo:
            approve_tool_run(
                db, project_id=project.id, tool_run_id=uuid4()
            )
        db.rollback()
    finally:
        db.close()
    assert "not found" in str(excinfo.value)


# ============================================================================
# 6. Adapter failure injection — bounded retry, unknown result
# ============================================================================


def test_adapter_transient_timeout_retries_then_succeeds(
    SessionLocal, monkeypatch
):
    """A transient timeout on the first attempt is retried (bounded)
    and the second attempt succeeds. The run row records the real
    attempt / retry count and the persisted SimulatedIssue carries
    the SIM-... id from the successful attempt.
    """
    from app.tools.registry import bootstrap_default_registry

    # Rebuild the registry so the descriptor's adapter_factory uses
    # the failure-injecting instance below.
    ToolRegistry.reset_for_tests()

    captured: dict[str, Any] = {}

    def _failing_factory():
        adapter = SimulatedIssueTrackerAdapter(
            fail_mode="transient_timeout", fail_on_attempt=1
        )
        captured["adapter"] = adapter
        return adapter

    bootstrap_default_registry()
    # Replace the simulator adapter factory with the failing one.
    descriptor = ToolRegistry.instance().get(
        "simulated_issue_tracker", "create_issue"
    )
    descriptor.adapter_factory = _failing_factory  # type: ignore[assignment]

    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        run = approve_tool_run(
            db, project_id=project.id, tool_run_id=run_id
        )
        db.commit()
    finally:
        db.close()

    assert run.status == ToolRunStatus.COMPLETED.value
    # Bounded retry: one retry (2 attempts).
    assert run.attempt_count == 2
    assert run.retry_count == 1
    # Exactly one audit row.
    db = SessionLocal()
    try:
        assert db.query(SimulatedIssue).count() == 1
        # The adapter was invoked twice (1 failure + 1 success).
        assert captured["adapter"].attempt_count == 2
    finally:
        db.close()


def test_adapter_transient_5xx_retries_then_fails(SessionLocal):
    """A persistent transient 5xx exhausts the bounded retry budget
    and the run is marked failed. No SimulatedIssue row is written.
    """
    ToolRegistry.reset_for_tests()

    def _always_fail_factory():
        return SimulatedIssueTrackerAdapter(
            fail_mode="transient_5xx", fail_on_attempt=10
        )

    bootstrap_default_registry()
    descriptor = ToolRegistry.instance().get(
        "simulated_issue_tracker", "create_issue"
    )
    descriptor.adapter_factory = _always_fail_factory  # type: ignore[assignment]

    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        run = approve_tool_run(
            db, project_id=project.id, tool_run_id=run_id
        )
        db.commit()
    finally:
        db.close()

    assert run.status == ToolRunStatus.FAILED.value
    # The bounded retry budget is exhausted (initial + 1 retry = 2).
    assert run.attempt_count == 2
    assert run.retry_count == 1
    assert run.failure_category == "transport_5xx"
    # No SimulatedIssue row was persisted — failed runs produce
    # zero writes.
    db = SessionLocal()
    try:
        assert db.query(SimulatedIssue).count() == 0
    finally:
        db.close()


def test_adapter_provider_failure_not_retried(SessionLocal):
    """A non-retriable provider_failure error terminates the run on
    the first attempt. Unknown results MUST NOT be blindly retried.
    """
    ToolRegistry.reset_for_tests()

    def _fail_factory():
        return SimulatedIssueTrackerAdapter(
            fail_mode="provider_failure", fail_on_attempt=10
        )

    bootstrap_default_registry()
    descriptor = ToolRegistry.instance().get(
        "simulated_issue_tracker", "create_issue"
    )
    descriptor.adapter_factory = _fail_factory  # type: ignore[assignment]

    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        run = approve_tool_run(
            db, project_id=project.id, tool_run_id=run_id
        )
        db.commit()
    finally:
        db.close()

    assert run.status == ToolRunStatus.FAILED.value
    # No retry was attempted — the failure was non-retriable.
    assert run.attempt_count == 1
    assert run.retry_count == 0
    assert run.failure_category == "provider_failure"
    # Audit + run row preserved for inspection.
    assert run.response_summary is not None
    db = SessionLocal()
    try:
        assert db.query(SimulatedIssue).count() == 0
    finally:
        db.close()


def test_approve_rejects_non_simulation_only_tool(SessionLocal):
    """Approve is the simulator gate — calling it on a read-only tool
    is rejected with ``not_simulation_only`` (409) so a read-only
    descriptor cannot accidentally become a write path.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="web_research",  # read-only
            tool_action="search",
            request_id=str(uuid4()),
            payload={"query": "q"},
        )
        db.commit()
        run_id = outcome.run.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        with pytest.raises(Exception) as excinfo:
            approve_tool_run(
                db, project_id=project.id, tool_run_id=run_id
            )
        db.rollback()
    finally:
        db.close()
    assert "simulation_only" in str(excinfo.value)


# ============================================================================
# 7. Rejected Prepare produces zero writes
# ============================================================================


def test_rejected_prepare_writes_no_simulated_issue_or_run_row(
    SessionLocal,
):
    """A rejected Prepare (draft Requirement) writes no SimulatedIssue
    AND no ToolRun row. ``db.rollback()`` reverts everything.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _make_requirement(
            db, project=project, status=REQUIREMENT_DRAFT
        )
    finally:
        db.close()

    db = SessionLocal()
    try:
        with pytest.raises(Exception):
            prepare_tool_run(
                db,
                project_id=project.id,
                requirement_id=seed["requirement_id"],
                tool_name="simulated_issue_tracker",
                tool_action="create_issue",
                request_id=str(uuid4()),
                payload={},
            )
        db.rollback()
    finally:
        db.close()

    db = SessionLocal()
    try:
        assert db.query(ToolRun).count() == 0
        assert db.query(SimulatedIssue).count() == 0
    finally:
        db.close()


def test_cancelled_run_writes_no_simulated_issue(SessionLocal):
    """Cancellation produces zero audit rows."""
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        cancel_tool_run(
            db, project_id=project.id, tool_run_id=run_id
        )
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        assert db.query(SimulatedIssue).count() == 0
    finally:
        db.close()


# ============================================================================
# 8. API endpoints — auth-free; explicit approval; project isolation
# ============================================================================


def test_api_get_tools_returns_static_registry(client: TestClient):
    response = client.get("/tools")
    assert response.status_code == 200
    body = response.json()
    names = {(d["name"], d["action"]) for d in body["tools"]}
    assert ("web_research", "search") in names
    assert ("simulated_issue_tracker", "create_issue") in names


def test_api_prepare_unknown_tool_returns_404(client: TestClient, SessionLocal):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    response = client.post(
        f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project.id}",
        json={
            "tool_name": "imaginary_tool",
            "tool_action": "create_issue",
            "request_id": str(uuid4()),
        },
    )
    assert response.status_code == 404


def test_api_prepare_creates_run_with_201_then_idempotent_200(
    client: TestClient, SessionLocal
):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    request_id = str(uuid4())
    body = {
        "tool_name": "simulated_issue_tracker",
        "tool_action": "create_issue",
        "request_id": request_id,
    }
    first = client.post(
        f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project.id}",
        json=body,
    )
    assert first.status_code == 201
    assert first.headers.get("X-Tool-Run-Created") == "true"
    first_id = first.json()["id"]

    second = client.post(
        f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project.id}",
        json=body,
    )
    assert second.status_code == 200
    assert second.headers.get("X-Tool-Run-Created") == "false"
    assert second.json()["id"] == first_id


def test_api_prepare_rejects_draft_requirement_409(
    client: TestClient, SessionLocal
):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _make_requirement(
            db, project=project, status=REQUIREMENT_DRAFT
        )
    finally:
        db.close()

    response = client.post(
        f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project.id}",
        json={
            "tool_name": "simulated_issue_tracker",
            "tool_action": "create_issue",
            "request_id": str(uuid4()),
        },
    )
    assert response.status_code == 409


def test_api_prepare_rejects_cross_project_404(
    client: TestClient, SessionLocal
):
    db = SessionLocal()
    try:
        project_a = _make_project(db, name="A")
        project_b = _make_project(db, name="B")
        seed = _seed_approved_requirement(db, project=project_a)
    finally:
        db.close()

    response = client.post(
        f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project_b.id}",
        json={
            "tool_name": "simulated_issue_tracker",
            "tool_action": "create_issue",
            "request_id": str(uuid4()),
        },
    )
    assert response.status_code == 404


def test_api_prepare_rejects_oversize_request_id_422(
    client: TestClient, SessionLocal
):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    response = client.post(
        f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project.id}",
        json={
            "tool_name": "simulated_issue_tracker",
            "tool_action": "create_issue",
            "request_id": "not-a-uuid",
        },
    )
    assert response.status_code == 422


def test_api_prepare_rejects_extra_field_422(
    client: TestClient, SessionLocal
):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    response = client.post(
        f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project.id}",
        json={
            "tool_name": "simulated_issue_tracker",
            "tool_action": "create_issue",
            "request_id": str(uuid4()),
            "sneaky": "field",
        },
    )
    assert response.status_code == 422


def test_api_cancel_and_approve_lifecycle(
    client: TestClient, SessionLocal
):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    prepared = client.post(
        f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project.id}",
        json={
            "tool_name": "simulated_issue_tracker",
            "tool_action": "create_issue",
            "request_id": str(uuid4()),
        },
    )
    assert prepared.status_code == 201
    run_id = prepared.json()["id"]
    assert prepared.json()["status"] == "awaiting_approval"

    approved = client.post(
        f"/tool-runs/{run_id}/approve?project_id={project.id}"
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "completed"
    assert approved.json()["external_id"].startswith("SIM-")
    assert approved.json()["response_summary"] is not None

    # Cancel after complete → 409.
    cancel_after = client.post(
        f"/tool-runs/{run_id}/cancel?project_id={project.id}"
    )
    assert cancel_after.status_code == 409


def test_api_cancel_awaiting_run_returns_cancelled(
    client: TestClient, SessionLocal
):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    prepared = client.post(
        f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project.id}",
        json={
            "tool_name": "simulated_issue_tracker",
            "tool_action": "create_issue",
            "request_id": str(uuid4()),
        },
    )
    assert prepared.status_code == 201
    run_id = prepared.json()["id"]

    cancelled = client.post(
        f"/tool-runs/{run_id}/cancel?project_id={project.id}"
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    # Approve a cancelled run → 409.
    approve_cancelled = client.post(
        f"/tool-runs/{run_id}/approve?project_id={project.id}"
    )
    assert approve_cancelled.status_code == 409


def test_api_get_run_requires_project_id_422(
    client: TestClient, SessionLocal
):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    prepared = client.post(
        f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project.id}",
        json={
            "tool_name": "simulated_issue_tracker",
            "tool_action": "create_issue",
            "request_id": str(uuid4()),
        },
    )
    run_id = prepared.json()["id"]
    response = client.get(f"/tool-runs/{run_id}")
    assert response.status_code == 422


def test_api_get_run_cross_project_404(
    client: TestClient, SessionLocal
):
    db = SessionLocal()
    try:
        project_a = _make_project(db, name="A")
        project_b = _make_project(db, name="B")
        seed = _seed_approved_requirement(db, project=project_a)
    finally:
        db.close()

    prepared = client.post(
        f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project_a.id}",
        json={
            "tool_name": "simulated_issue_tracker",
            "tool_action": "create_issue",
            "request_id": str(uuid4()),
        },
    )
    run_id = prepared.json()["id"]

    response = client.get(f"/tool-runs/{run_id}?project_id={project_b.id}")
    assert response.status_code == 404


def test_api_list_tool_runs_for_requirement_includes_active_pointer(
    client: TestClient, SessionLocal
):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    prepared = client.post(
        f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project.id}",
        json={
            "tool_name": "simulated_issue_tracker",
            "tool_action": "create_issue",
            "request_id": str(uuid4()),
        },
    )
    active_id = prepared.json()["id"]

    history = client.get(
        f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project.id}"
    )
    assert history.status_code == 200
    body = history.json()
    assert body["total"] == 1
    assert body["active_run_id"] == active_id


def test_api_response_payload_is_bounded(
    client: TestClient, SessionLocal
):
    """Sanity: the wire DTO carries the bounded fields only."""
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    prepared = client.post(
        f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project.id}",
        json={
            "tool_name": "simulated_issue_tracker",
            "tool_action": "create_issue",
            "request_id": str(uuid4()),
        },
    )
    run_id = prepared.json()["id"]

    approved = client.post(
        f"/tool-runs/{run_id}/approve?project_id={project.id}"
    )
    payload = approved.json()
    assert payload["status"] == "completed"
    assert payload["execution_mode"] == "simulation"
    assert payload["external_id"].startswith("SIM-")
    # No raw feedback content leaks into the wire payload.
    rendered = json.dumps(payload, ensure_ascii=False)
    assert "raw_feedback_secret_token" not in rendered


# ============================================================================
# 9. DB-level invariants
# ============================================================================


def test_db_unique_request_id_per_project(SessionLocal):
    """``UNIQUE(project_id, request_id)`` rejects duplicate rows."""
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        # Manually try to insert a second row with the same
        # (project_id, request_id) — the DB-level UNIQUE must
        # reject it.
        from sqlalchemy.exc import IntegrityError

        dup = ToolRun(
            project_id=outcome.run.project_id,
            requirement_id=outcome.run.requirement_id,
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            tool_version="simulator-v1",
            request_id=outcome.run.request_id,
            status="awaiting_approval",
            execution_mode="simulation",
            request_payload={},
        )
        db.add(dup)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()


def test_db_unique_simulated_issue_external_id(SessionLocal):
    """``UNIQUE(external_id)`` on ``simulated_issues`` rejects duplicate writes."""
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        approve_tool_run(db, project_id=project.id, tool_run_id=run_id)
        db.commit()
        issue = db.query(SimulatedIssue).one()
    finally:
        db.close()

    db = SessionLocal()
    try:
        from sqlalchemy.exc import IntegrityError

        dup = SimulatedIssue(
            tool_run_id=issue.tool_run_id,
            project_id=issue.project_id,
            requirement_id=issue.requirement_id,
            tool_name=issue.tool_name,
            tool_action=issue.tool_action,
            external_id=issue.external_id,
            simulated=True,
            title="dup",
            description="dup body",
            priority="P1",
            source_insight_preview="dup",
        )
        db.add(dup)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()


# ============================================================================
# 10. Adapter unit tests (no DB)
# ============================================================================


def test_adapter_returns_sim_prefix_and_simulated_true():
    adapter = SimulatedIssueTrackerAdapter()
    result = adapter.execute(
        tool_name="simulated_issue_tracker",
        tool_action="create_issue",
        title="T",
        description="D",
        priority="P1",
        source_insight_preview="preview",
        requirement_id=uuid4(),
        project_id=uuid4(),
    )
    assert result.external_id.startswith("SIM-")
    assert result.simulated is True
    assert result.tool_name == "simulated_issue_tracker"
    assert result.tool_action == "create_issue"
    # The summary is bounded and never carries raw bytes.
    assert len(result.summary) <= 500


def test_adapter_transient_timeout_raises_transient_error():
    adapter = SimulatedIssueTrackerAdapter(
        fail_mode="transient_timeout", fail_on_attempt=1
    )
    with pytest.raises(SimulatedIssueTrackerTransientError) as excinfo:
        adapter.execute(
            tool_name="x",
            tool_action="y",
            title="T",
            description="D",
            priority="P1",
            source_insight_preview="p",
            requirement_id=uuid4(),
            project_id=uuid4(),
        )
    assert excinfo.value.category == "timeout"


def test_adapter_provider_failure_raises_non_retriable_error():
    adapter = SimulatedIssueTrackerAdapter(
        fail_mode="provider_failure", fail_on_attempt=1
    )
    with pytest.raises(SimulatedIssueTrackerError) as excinfo:
        adapter.execute(
            tool_name="x",
            tool_action="y",
            title="T",
            description="D",
            priority="P1",
            source_insight_preview="p",
            requirement_id=uuid4(),
            project_id=uuid4(),
        )
    assert excinfo.value.category == "provider_failure"


def test_adapter_records_calls_with_bounded_lengths():
    adapter = SimulatedIssueTrackerAdapter()
    adapter.execute(
        tool_name="simulated_issue_tracker",
        tool_action="create_issue",
        title="T",
        description="D",
        priority="P1",
        source_insight_preview="preview",
        requirement_id=uuid4(),
        project_id=uuid4(),
    )
    assert len(adapter.calls) == 1
    call = adapter.calls[0]
    assert call["tool_name"] == "simulated_issue_tracker"
    assert call["title_length"] == 1
    assert call["description_length"] == 1


def test_rejected_request_id_shape_via_api(
    client: TestClient, SessionLocal
):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    response = client.post(
        f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project.id}",
        json={
            "tool_name": "simulated_issue_tracker",
            "tool_action": "create_issue",
            "request_id": "x" * 36,  # wrong shape
        },
    )
    assert response.status_code == 422


def test_history_pagination_envelope(
    client: TestClient, SessionLocal
):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    # Three prepare calls with three different request_ids.
    for _ in range(3):
        client.post(
            f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project.id}",
            json={
                "tool_name": "simulated_issue_tracker",
                "tool_action": "create_issue",
                "request_id": str(uuid4()),
            },
        )
    response = client.get(
        f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project.id}"
    )
    body = response.json()
    assert body["total"] == 3
    assert body["limit"] == 50
    assert body["offset"] == 0
    # Newest first.
    assert body["items"][0]["created_at"] >= body["items"][-1]["created_at"]


def test_approve_then_re_approve_keeps_one_audit_row(
    client: TestClient, SessionLocal
):
    """End-to-end: prepare → approve → re-approve → audit row count is 1."""
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    prepared = client.post(
        f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project.id}",
        json={
            "tool_name": "simulated_issue_tracker",
            "tool_action": "create_issue",
            "request_id": str(uuid4()),
        },
    )
    run_id = prepared.json()["id"]

    first = client.post(
        f"/tool-runs/{run_id}/approve?project_id={project.id}"
    )
    second = client.post(
        f"/tool-runs/{run_id}/approve?project_id={project.id}"
    )
    assert first.json()["external_id"] == second.json()["external_id"]

    # The simulator should have been invoked exactly once.
    db = SessionLocal()
    try:
        assert db.query(SimulatedIssue).count() == 1
    finally:
        db.close()


def test_approve_in_pending_status_is_required(
    client: TestClient, SessionLocal
):
    """Approve cannot be called without an explicit Prepare first."""
    db = SessionLocal()
    try:
        project = _make_project(db)
    finally:
        db.close()

    response = client.post(
        f"/tool-runs/{uuid4()}/approve?project_id={project.id}"
    )
    assert response.status_code == 404


# ============================================================================
# 11. Frozen snapshot regressions — Prepare-time freeze + Approve reuse
# ============================================================================


def test_snapshot_persisted_at_prepare_time_matches_get_preview(
    SessionLocal,
):
    """The 4-field snapshot is persisted on ToolRun.request_payload at
    Prepare time. A subsequent GET surfaces the same content the
    Approve handler will drive the simulator with.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project, name="多选模式提示气泡")
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
    finally:
        db.close()

    # GET the row back via the service.
    db = SessionLocal()
    try:
        run = db.get(ToolRun, run_id)
        payload = dict(run.request_payload)
    finally:
        db.close()

    # The snapshot MUST contain all four fields, exactly as a
    # SimulatedIssueSnapshot would validate.
    from app.tools.registry import SimulatedIssueSnapshot

    snapshot = SimulatedIssueSnapshot.model_validate(payload)
    assert snapshot.title.startswith("[Simulated] 多选模式提示气泡")
    assert "用户进入多选模式后无法撤销" in snapshot.description
    assert snapshot.priority == "P1"
    assert len(snapshot.source_insight_preview) <= 400


def test_approve_uses_frozen_snapshot_after_requirement_modified(
    SessionLocal,
):
    """After Prepare, the Requirement's text + PM priority change.
    Approve MUST still produce a SimulatedIssue that matches the
    frozen snapshot (NOT the live Requirement), proving the
    user-visible approval cannot drift.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project, name="OLD-TITLE")
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
        frozen_payload = dict(outcome.run.request_payload)
    finally:
        db.close()

    # Tamper with the live Requirement — change the title and the
    # PM priority. The Approve handler must NOT see these.
    db = SessionLocal()
    try:
        from app.models.requirement_prd import Requirement as RequirementModel

        requirement = db.get(RequirementModel, seed["requirement_id"])
        requirement.name = "NEW-TITLE-AFTER-PREPARE"
        requirement.pm_priority = "P0"
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        approve_tool_run(
            db, project_id=project.id, tool_run_id=run_id
        )
        db.commit()
        issue = db.query(SimulatedIssue).one()
    finally:
        db.close()

    # The SimulatedIssue row reflects the FROZEN snapshot, not the
    # mutated Requirement.
    assert issue.title == frozen_payload["title"]
    assert issue.title.startswith("[Simulated] OLD-TITLE")
    assert "NEW-TITLE-AFTER-PREPARE" not in issue.title
    assert issue.priority == "P1"  # NOT "P0" from the live edit
    assert issue.description == frozen_payload["description"]


def test_approve_rejects_when_requirement_archived_after_prepare(
    SessionLocal,
):
    """If a PM revokes / archives the Requirement between Prepare and
    Approve, the Approve handler MUST refuse (mapped to 409 by the
    API layer) and MUST NOT drive the simulator. The frozen snapshot
    on the run is preserved untouched so the audit row can still be
    read, but no SimulatedIssue is persisted.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
        frozen_payload = dict(outcome.run.request_payload)
    finally:
        db.close()

    db = SessionLocal()
    try:
        from app.models.requirement_prd import Requirement as RequirementModel

        requirement = db.get(RequirementModel, seed["requirement_id"])
        requirement.status = REQUIREMENT_DRAFT  # revoked after Prepare
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        with pytest.raises(Exception) as excinfo:
            approve_tool_run(
                db, project_id=project.id, tool_run_id=run_id
            )
        db.rollback()
    finally:
        db.close()

    # The service-layer marker is "not approved" → mapped to 409.
    assert "not approved" in str(excinfo.value)
    # The frozen snapshot is preserved untouched.
    db = SessionLocal()
    try:
        run = db.get(ToolRun, run_id)
        assert run.status == ToolRunStatus.AWAITING_APPROVAL.value
        assert run.request_payload == frozen_payload
        # No SimulatedIssue row was written.
        assert db.query(SimulatedIssue).count() == 0
    finally:
        db.close()


def test_approve_via_api_matches_get_preview_after_requirement_modified(
    client: TestClient, SessionLocal
):
    """End-to-end via HTTP: Prepare, mutate Requirement, GET the row,
    Approve. The SimulatedIssue audit row matches the GET preview
    exactly.
    """
    from app.models.requirement_prd import Requirement as RequirementModel

    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project, name="Frozen")
    finally:
        db.close()

    prepared = client.post(
        f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project.id}",
        json={
            "tool_name": "simulated_issue_tracker",
            "tool_action": "create_issue",
            "request_id": str(uuid4()),
        },
    )
    assert prepared.status_code == 201
    run_id = prepared.json()["id"]
    preview = prepared.json()["request_payload"]

    # Mutate the live Requirement.
    db = SessionLocal()
    try:
        requirement = db.get(RequirementModel, seed["requirement_id"])
        requirement.name = "MutatedAfterPrepare"
        requirement.pm_priority = "P0"
        db.commit()
    finally:
        db.close()

    # GET the audit preview — the snapshot must still reflect the
    # original title "Frozen", not "MutatedAfterPrepare".
    preview_get = client.get(
        f"/tool-runs/{run_id}?project_id={project.id}"
    )
    assert preview_get.status_code == 200
    get_payload = preview_get.json()["request_payload"]
    assert get_payload["title"] == preview["title"]
    assert get_payload["title"].startswith("[Simulated] Frozen")
    assert "MutatedAfterPrepare" not in get_payload["title"]
    assert get_payload["priority"] == "P1"  # NOT the mutated "P0"

    # Approve — succeeds against the frozen snapshot.
    approved = client.post(
        f"/tool-runs/{run_id}/approve?project_id={project.id}"
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "completed"

    # The audit row matches the GET preview (not the mutated live
    # Requirement).
    db = SessionLocal()
    try:
        issue = db.query(SimulatedIssue).one()
    finally:
        db.close()
    assert issue.title == get_payload["title"]
    assert issue.priority == get_payload["priority"]
    assert issue.description == get_payload["description"]


def test_api_approve_after_requirement_archived_returns_409(
    client: TestClient, SessionLocal
):
    """HTTP regression for the archived-after-Prepare case.

    Prepare succeeds (Requirement was approved at Prepare time and the
    snapshot is frozen). Between Prepare and Approve, the Requirement
    is moved out of ``approved``. The Approve endpoint MUST return
    409, MUST leave the run in ``awaiting_approval``, and MUST NOT
    persist a SimulatedIssue row.
    """
    from app.models.requirement_prd import Requirement as RequirementModel

    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    prepared = client.post(
        f"/requirements/{seed['requirement_id']}/tool-runs?project_id={project.id}",
        json={
            "tool_name": "simulated_issue_tracker",
            "tool_action": "create_issue",
            "request_id": str(uuid4()),
        },
    )
    assert prepared.status_code == 201
    run_id = prepared.json()["id"]
    frozen_title = prepared.json()["request_payload"]["title"]

    # Revoke the Requirement after Prepare.
    db = SessionLocal()
    try:
        requirement = db.get(RequirementModel, seed["requirement_id"])
        requirement.status = REQUIREMENT_DRAFT
        db.commit()
    finally:
        db.close()

    response = client.post(
        f"/tool-runs/{run_id}/approve?project_id={project.id}"
    )
    assert response.status_code == 409

    # The run row stays in awaiting_approval with the original snapshot.
    after_get = client.get(f"/tool-runs/{run_id}?project_id={project.id}")
    assert after_get.status_code == 200
    assert after_get.json()["status"] == "awaiting_approval"
    assert after_get.json()["request_payload"]["title"] == frozen_title

    db = SessionLocal()
    try:
        assert db.query(SimulatedIssue).count() == 0
    finally:
        db.close()


def test_cancel_after_prepare_writes_no_simulated_issue(
    SessionLocal,
):
    """Cancel writes no SimulatedIssue row — even though Prepare
    already froze the snapshot on the run.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        run = cancel_tool_run(
            db, project_id=project.id, tool_run_id=run_id
        )
        db.commit()
    finally:
        db.close()

    assert run.status == ToolRunStatus.CANCELLED.value
    db = SessionLocal()
    try:
        assert db.query(SimulatedIssue).count() == 0
    finally:
        db.close()


def test_repeat_approve_writes_no_second_simulated_issue(
    SessionLocal,
):
    """A second Approve on a completed run returns the same row and
    persists NO additional SimulatedIssue row, even though the
    snapshot is intact. Idempotency is independent of snapshot reuse.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        first = approve_tool_run(
            db, project_id=project.id, tool_run_id=run_id
        )
        db.commit()
        first_external = first.external_id
    finally:
        db.close()

    db = SessionLocal()
    try:
        second = approve_tool_run(
            db, project_id=project.id, tool_run_id=run_id
        )
        db.commit()
    finally:
        db.close()

    assert second.status == ToolRunStatus.COMPLETED.value
    assert second.external_id == first_external
    db = SessionLocal()
    try:
        assert db.query(SimulatedIssue).count() == 1
    finally:
        db.close()


def test_approve_rejects_tampered_snapshot(SessionLocal):
    """A malicious edit to ToolRun.request_payload between Prepare
    and Approve must NOT reach the simulator. Approve validates the
    persisted snapshot against SimulatedIssueSnapshot and rejects
    nonsense (wrong type, missing field, oversize, illegal priority).
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
    finally:
        db.close()

    # Tamper directly at the ORM layer.
    db = SessionLocal()
    try:
        run = db.get(ToolRun, run_id)
        run.request_payload = {
            "title": "hi",  # too short? actually OK; oversize below
            "description": "X",  # below the min_length=1
            "priority": "P9",  # not in {P0,P1,P2}
            "source_insight_preview": "preview",
        }
        # Force a string that violates the bounded-length rule by
        # writing a giant priority.
        run.request_payload = {
            "title": "Tampered title — USER-INJECTED",
            "description": "Injected by a malicious reviewer.",
            "priority": "P9",
            "source_insight_preview": "tampered preview",
        }
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        with pytest.raises(Exception) as excinfo:
            approve_tool_run(
                db, project_id=project.id, tool_run_id=run_id
            )
        db.rollback()
    finally:
        db.close()

    assert "snapshot" in str(excinfo.value).lower()

    # The run row is still awaiting_approval and NO SimulatedIssue
    # was written.
    db = SessionLocal()
    try:
        run = db.get(ToolRun, run_id)
        assert run.status == ToolRunStatus.AWAITING_APPROVAL.value
        assert db.query(SimulatedIssue).count() == 0
    finally:
        db.close()


def test_approve_rejects_missing_required_fields_in_snapshot(
    SessionLocal,
):
    """A snapshot missing one of the required 4 fields must be
    rejected before the simulator is invoked.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        run = db.get(ToolRun, run_id)
        # Drop one required field.
        run.request_payload = {
            "title": "only title",
            "description": "only desc",
            "priority": "P1",
        }
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        with pytest.raises(Exception) as excinfo:
            approve_tool_run(
                db, project_id=project.id, tool_run_id=run_id
            )
        db.rollback()
    finally:
        db.close()
    assert "snapshot" in str(excinfo.value).lower()


def test_snapshot_priority_validator_rejects_arbitrary_string():
    """The snapshot schema enforces ``priority in {P0, P1, P2}``.
    Direct construction catches the violation BEFORE persistence.
    """
    from app.tools.registry import SimulatedIssueSnapshot

    with pytest.raises(Exception):
        SimulatedIssueSnapshot(
            title="t",
            description="d",
            priority="URGENT",  # not a permitted value
            source_insight_preview="p",
        )


# ============================================================================
# 12. Concurrency + cross-Requirement Prepare regression
# ============================================================================
#
# These tests pin the Phase 8 cross-Requirement Prepare conflict
# rule and the Approve / Cancel race-serialisation. The cross-
# Requirement + 409 cases run on any DB. The two-thread race cases
# require PostgreSQL so the ``SELECT ... FOR UPDATE`` lock is
# actually exercised; on SQLite the test would mask race bugs
# behind connection-level serialisation, so it is gated behind
# ``requires_postgres``.


def test_prepare_rejects_same_request_id_for_different_requirement(
    SessionLocal,
):
    """The ``(project_id, request_id)`` UNIQUE key is project-scoped
    but Requirement-scoped: a SECOND Prepare that reuses the same
    ``request_id`` but points at a DIFFERENT Requirement is a
    CONFLICT, not an idempotent re-issue. Mapped to 409 by the API
    layer.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed_a = _seed_approved_requirement(db, project=project, name="A")
        seed_b = _seed_approved_requirement(db, project=project, name="B")
    finally:
        db.close()

    shared_request_id = str(uuid4())

    db = SessionLocal()
    try:
        first = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed_a["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=shared_request_id,
            payload={},
        )
        db.commit()
    finally:
        db.close()

    assert first.created is True

    db = SessionLocal()
    try:
        with pytest.raises(Exception) as excinfo:
            prepare_tool_run(
                db,
                project_id=project.id,
                requirement_id=seed_b["requirement_id"],
                tool_name="simulated_issue_tracker",
                tool_action="create_issue",
                request_id=shared_request_id,
                payload={},
            )
        db.rollback()
    finally:
        db.close()

    assert "conflict on request_id" in str(excinfo.value).lower()
    # The first row is untouched and no second row was inserted.
    db = SessionLocal()
    try:
        assert db.query(ToolRun).count() == 1
        assert db.query(SimulatedIssue).count() == 0
    finally:
        db.close()


def test_prepare_rejects_same_request_id_for_different_tool_action(
    SessionLocal,
):
    """The UNIQUE key covers ``(project_id, request_id)``; the
    ``(tool_name, tool_action)`` pair is NOT free to drift. A second
    Prepare with the same request_id but a different tool surface is
    a 409.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    shared_request_id = str(uuid4())

    db = SessionLocal()
    try:
        prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=shared_request_id,
            payload={},
        )
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        with pytest.raises(Exception) as excinfo:
            prepare_tool_run(
                db,
                project_id=project.id,
                requirement_id=seed["requirement_id"],
                tool_name="web_research",
                tool_action="search",
                request_id=shared_request_id,
                payload={"query": "x"},
            )
        db.rollback()
    finally:
        db.close()

    assert "conflict on request_id" in str(excinfo.value).lower()


def test_api_prepare_conflict_returns_409(client: TestClient, SessionLocal):
    """HTTP regression: the same (project_id, request_id) reused
    across Requirements must surface as 409 with no second row
    inserted.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed_a = _seed_approved_requirement(db, project=project, name="A")
        seed_b = _seed_approved_requirement(db, project=project, name="B")
    finally:
        db.close()

    shared_request_id = str(uuid4())
    body = {
        "tool_name": "simulated_issue_tracker",
        "tool_action": "create_issue",
        "request_id": shared_request_id,
    }

    first = client.post(
        f"/requirements/{seed_a['requirement_id']}/tool-runs?project_id={project.id}",
        json=body,
    )
    assert first.status_code == 201

    second = client.post(
        f"/requirements/{seed_b['requirement_id']}/tool-runs?project_id={project.id}",
        json=body,
    )
    assert second.status_code == 409


def test_cancel_after_approve_returns_409(SessionLocal):
    """Sequential: Approve completes, then a Cancel arrives. The
    state machine must reject the Cancel (run is already completed)
    with 409 — no silent status rewrite, no second SimulatedIssue.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        approve_tool_run(db, project_id=project.id, tool_run_id=run_id)
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        with pytest.raises(Exception) as excinfo:
            cancel_tool_run(
                db, project_id=project.id, tool_run_id=run_id
            )
        db.rollback()
    finally:
        db.close()

    assert "not awaiting" in str(excinfo.value).lower()

    db = SessionLocal()
    try:
        run = db.get(ToolRun, run_id)
        assert run.status == ToolRunStatus.COMPLETED.value
        assert db.query(SimulatedIssue).count() == 1
    finally:
        db.close()


# ---- PostgreSQL-only concurrency regressions ------------------------------


def test_postgres_concurrent_double_approve_runs_adapter_once(
    SessionLocal, requires_postgres
):
    """Two threads call ``approve_tool_run`` on the same awaiting
    run. PostgreSQL's ``SELECT ... FOR UPDATE`` serialises them;
    the second one sees the row in ``completed`` and returns the
    existing run without re-invoking the adapter. Exactly one
    :class:`SimulatedIssue` row is produced and no 500 leaks.
    """
    captured: dict[str, int] = {"adapter_calls": 0}
    original_execute = SimulatedIssueTrackerAdapter.execute

    def _counting_execute(self, **kwargs):  # type: ignore[no-redef]
        captured["adapter_calls"] += 1
        return original_execute(self, **kwargs)

    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
    finally:
        db.close()

    SimulatedIssueTrackerAdapter.execute = _counting_execute  # type: ignore[assignment]
    try:
        results = _run_concurrent([
            lambda: _approve_in_thread(SessionLocal, project.id, run_id),
            lambda: _approve_in_thread(SessionLocal, project.id, run_id),
        ])
    finally:
        SimulatedIssueTrackerAdapter.execute = original_execute  # type: ignore[assignment]

    for status, value in results:
        if status == "err":
            raise value  # type: ignore[misc]
        assert value.status == ToolRunStatus.COMPLETED.value
        assert value.external_id is not None

    # Exactly one adapter invocation → exactly one SimulatedIssue row.
    assert captured["adapter_calls"] == 1
    db = SessionLocal()
    try:
        assert db.query(SimulatedIssue).count() == 1
    finally:
        db.close()


def test_postgres_concurrent_approve_and_cancel_resolves_deterministically(
    SessionLocal, requires_postgres
):
    """Approve and Cancel race on the same run. Exactly one wins,
    the other receives a clean 409. No 500, no duplicate audit
    rows, and the run's terminal state matches the winner.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    db = SessionLocal()
    try:
        outcome = prepare_tool_run(
            db,
            project_id=project.id,
            requirement_id=seed["requirement_id"],
            tool_name="simulated_issue_tracker",
            tool_action="create_issue",
            request_id=str(uuid4()),
            payload={},
        )
        db.commit()
        run_id = outcome.run.id
    finally:
        db.close()

    results = _run_concurrent([
        lambda: _approve_in_thread(SessionLocal, project.id, run_id),
        lambda: _cancel_in_thread(SessionLocal, project.id, run_id),
    ])

    approve_status, approve_value = results[0]
    cancel_status, cancel_value = results[1]

    # The simulator ran at most once → SimulatedIssue count is 0 or 1.
    if approve_status == "ok" and cancel_status == "ok":
        # Both succeeded only if the second was a no-op (e.g. a
        # repeated Cancel on an already-cancelled run). On a true
        # race this case is impossible.
        assert False, (
            "Approve and Cancel both succeeded — race-serialisation broken"
        )
    if approve_status == "ok" and cancel_status == "err":
        # Approve won. Cancel must surface 409 with a clear marker.
        assert approve_value.status == ToolRunStatus.COMPLETED.value
        assert "not awaiting" in str(cancel_value).lower()
    elif approve_status == "err" and cancel_status == "ok":
        # Cancel won. Approve must surface a 409-style error and
        # no SimulatedIssue must exist.
        assert cancel_value.status == ToolRunStatus.CANCELLED.value
        msg = str(approve_value).lower()
        assert (
            "concurrent_modification" in msg
            or "not awaiting" in msg
        )
    else:
        assert False, f"unexpected outcome: approve={approve_status}, cancel={cancel_status}"

    db = SessionLocal()
    try:
        # Exactly one SimulatedIssue iff Approve won.
        if approve_status == "ok":
            assert db.query(SimulatedIssue).count() == 1
        else:
            assert db.query(SimulatedIssue).count() == 0
    finally:
        db.close()


def test_postgres_concurrent_prepare_same_request_id_is_idempotent(
    SessionLocal, requires_postgres
):
    """Two threads call ``prepare_tool_run`` with the same
    ``request_id`` for the SAME Requirement. Exactly one
    :class:`ToolRun` row is persisted, both calls return the same
    id, and exactly one adapter does NOT need to be invoked (this
    is a Prepare, not an Approve — there is no adapter here). The
    second thread's call must NOT raise an unhandled
    ``IntegrityError``; the service layer must absorb the race and
    return the surviving row idempotently.
    """
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_approved_requirement(db, project=project)
    finally:
        db.close()

    shared_request_id = str(uuid4())

    def do_prepare() -> Any:
        db_local = SessionLocal()
        try:
            outcome = prepare_tool_run(
                db_local,
                project_id=project.id,
                requirement_id=seed["requirement_id"],
                tool_name="simulated_issue_tracker",
                tool_action="create_issue",
                request_id=shared_request_id,
                payload={},
            )
            db_local.commit()
            return outcome
        finally:
            db_local.close()

    results = _run_concurrent([do_prepare, do_prepare])

    for status, value in results:
        if status == "err":
            raise value  # type: ignore[misc]
        assert status == "ok"

    first_id = results[0][1].run.id
    second_id = results[1][1].run.id
    assert first_id == second_id

    # Exactly one created row regardless of who won the pre-flight
    # SELECT.
    created_flags = {r[1].created for r in results}
    assert created_flags == {True, False}, (
        f"expected one True + one False, got {created_flags}"
    )

    db = SessionLocal()
    try:
        assert db.query(ToolRun).count() == 1
    finally:
        db.close()


def test_postgres_revocation_before_approve_returns_409(
    SessionLocal, requires_postgres
):
    """If the Requirement has been revoked (status flipped off
    ``approved``) by the time Approve's eligibility re-check runs,
    Approve MUST raise ``ToolValidationError`` (mapped to 409) and
    the simulator is NEVER invoked. The frozen snapshot is left
    untouched on the run row so an audit can still read what was
    about to be approved; no ``SimulatedIssue`` row is persisted.

    This is one half of the requirement-vs-Approve race; the
    other half ("Approve holds the lock first, revoke blocks")
    is covered by
    :func:`test_postgres_approve_holds_requirement_lock_blocks_concurrent_revoke`.

    No external / public / shared schema is touched; the test runs
    inside the per-test fixture schema only.
    """
    from app.models.requirement_prd import Requirement as RequirementModel
    from app.services.tool_service import ToolValidationError

    adapter_called = threading.Event()

    class GuardedAdapter(SimulatedIssueTrackerAdapter):
        def execute(self, **kwargs):
            adapter_called.set()
            # Delegate to the real simulator so we don't handcraft a
            # result dataclass — Approve's eligibility gate must
            # short-circuit BEFORE this method runs anyway, so any
            # returned value is unreachable in this test.
            return super().execute(**kwargs)

    registry = ToolRegistry.instance()
    descriptor = registry.get(
        "simulated_issue_tracker", "create_issue"
    )
    original_factory = descriptor.adapter_factory
    descriptor.adapter_factory = lambda: GuardedAdapter()

    try:
        db = SessionLocal()
        try:
            project = _make_project(db)
            seed = _seed_approved_requirement(db, project=project)
            db.commit()
        finally:
            db.close()

        db = SessionLocal()
        try:
            outcome = prepare_tool_run(
                db,
                project_id=project.id,
                requirement_id=seed["requirement_id"],
                tool_name="simulated_issue_tracker",
                tool_action="create_issue",
                request_id=str(uuid4()),
                payload={},
            )
            db.commit()
            run_id = outcome.run.id
        finally:
            db.close()

        # Revoke the Requirement FIRST and commit BEFORE Approve
        # is started, so Approve's eligibility re-check must see
        # the post-revoke status.
        db = SessionLocal()
        try:
            requirement = db.get(RequirementModel, seed["requirement_id"])
            requirement.status = REQUIREMENT_DRAFT
            db.commit()
        finally:
            db.close()

        with pytest.raises(ToolValidationError) as excinfo:
            db = SessionLocal()
            try:
                approve_tool_run(
                    db,
                    project_id=project.id,
                    tool_run_id=run_id,
                )
                db.commit()
            finally:
                db.rollback()
                db.close()

        assert "not approved" in str(excinfo.value).lower(), (
            f"unexpected ToolValidationError wording: {excinfo.value!r}"
        )
        assert not adapter_called.is_set(), (
            "simulator adapter was invoked despite the Requirement "
            "having been revoked; the eligibility gate is broken"
        )

        # Final state: ToolRun is still awaiting_approval (the
        # conditional UPDATE was never issued), and no
        # SimulatedIssue row exists.
        db = SessionLocal()
        try:
            run = db.get(ToolRun, run_id)
            assert run.status == ToolRunStatus.AWAITING_APPROVAL.value
            assert db.query(SimulatedIssue).count() == 0
        finally:
            db.close()
    finally:
        descriptor.adapter_factory = original_factory


def test_postgres_approve_holds_requirement_lock_blocks_concurrent_revoke(
    SessionLocal, requires_postgres
):
    """If Approve acquires the requirement row lock first, a
    concurrent revoke transaction MUST block at the row level
    until Approve's local transaction commits.

    Lock order: ``tool_runs`` row first, ``requirements`` row
    second. Both are held until the calling transaction commits
    — i.e. across the eligibility re-check, the conditional
    ``awaiting_approval → running`` UPDATE, the simulator
    adapter call, the ``SimulatedIssue`` insert, and the
    ``running → completed`` UPDATE.

    The simulator adapter is replaced with a pausing
    implementation that signals on entry and blocks until
    released. By the time the test main thread releases the
    adapter, Approve has been holding the requirement row lock
    for the entire pause window. A revoke thread that started
    its UPDATE during that window MUST still be blocked — if
    it has committed within a short observation window, the row
    lock is missing.

    Final state after both threads finish:

    * Approve completed (ToolRun=completed, one SimulatedIssue
      row created while the Requirement was still ``approved``).
    * Revoke then ran and set Requirement=draft — correct: the
      simulation was already complete, so the revocation only
      governs future Approve calls.

    No external / public / shared schema is touched; the test
    runs inside the per-test fixture schema only.
    """
    import time

    from app.models.requirement_prd import Requirement as RequirementModel

    adapter_entered = threading.Event()
    release_adapter = threading.Event()

    class PausingAdapter(SimulatedIssueTrackerAdapter):
        def execute(self, **kwargs):
            adapter_entered.set()
            assert release_adapter.wait(timeout=15), (
                "test main thread failed to release the pausing adapter"
            )
            # Delegate to the real simulator so we don't handcraft a
            # result dataclass. The returned external_id is a fresh
            # SIM-<uuid> token; the assertion below checks the
            # prefix rather than the exact value.
            return super().execute(**kwargs)

    registry = ToolRegistry.instance()
    descriptor = registry.get(
        "simulated_issue_tracker", "create_issue"
    )
    original_factory = descriptor.adapter_factory
    descriptor.adapter_factory = lambda: PausingAdapter()

    try:
        db = SessionLocal()
        try:
            project = _make_project(db)
            seed = _seed_approved_requirement(db, project=project)
            db.commit()
        finally:
            db.close()

        db = SessionLocal()
        try:
            outcome = prepare_tool_run(
                db,
                project_id=project.id,
                requirement_id=seed["requirement_id"],
                tool_name="simulated_issue_tracker",
                tool_action="create_issue",
                request_id=str(uuid4()),
                payload={},
            )
            db.commit()
            run_id = outcome.run.id
        finally:
            db.close()

        # Start Approve. It will hold the requirement row lock
        # while paused in the adapter.
        approve_result: dict[str, Any] = {}

        def do_approve() -> None:
            db = SessionLocal()
            try:
                run = approve_tool_run(
                    db,
                    project_id=project.id,
                    tool_run_id=run_id,
                )
                db.commit()
                approve_result["status"] = "ok"
                approve_result["run"] = run
            except Exception as exc:
                approve_result["status"] = "err"
                approve_result["exc"] = exc
            finally:
                db.close()

        approve_thread = threading.Thread(target=do_approve)
        approve_thread.start()

        # Wait until Approve reaches the adapter — by that point
        # Approve has acquired the tool_runs row lock, the
        # requirements row lock, validated status=approved, and
        # issued the conditional ``awaiting_approval → running``
        # UPDATE.
        assert adapter_entered.wait(timeout=10), (
            "Approve never reached the adapter; lock-holding scenario "
            "is not set up"
        )

        # Start revoke. Its ``UPDATE requirements SET status =
        # draft`` MUST block at the row level because Approve
        # holds ``SELECT ... FOR UPDATE`` on the same row.
        revoke_before_commit = threading.Event()
        revoke_committed = threading.Event()

        def do_revoke() -> None:
            db = SessionLocal()
            try:
                requirement = db.get(
                    RequirementModel, seed["requirement_id"]
                )
                requirement.status = REQUIREMENT_DRAFT
                # Signal BEFORE ``db.commit()`` so the test main
                # thread knows revoke has started its UPDATE. If
                # the row lock works, ``db.commit()`` blocks here
                # until Approve releases it; the post-commit
                # ``revoke_committed.set()`` happens only AFTER
                # the lock has been released and the UPDATE has
                # landed.
                revoke_before_commit.set()
                db.commit()
            finally:
                db.close()
                revoke_committed.set()

        revoke_thread = threading.Thread(target=do_revoke)
        revoke_thread.start()

        # Wait for revoke to enter its tx (the moment the UPDATE
        # is about to be flushed).
        assert revoke_before_commit.wait(timeout=5), (
            "revoke thread never started its UPDATE"
        )

        # Observe a short window. PostgreSQL row lock acquisition
        # + UPDATE + COMMIT completes in well under 100 ms on a
        # local cluster; if revoke has not committed within 500
        # ms it is definitively blocked at the row level.
        time.sleep(0.5)
        assert not revoke_committed.is_set(), (
            "Revoke committed while Approve held the requirement "
            "row lock — FOR UPDATE on requirements is missing or "
            "revoke is bypassing it. Adapter "
            f"entered={adapter_entered.is_set()}; "
            f"revoke_committed={revoke_committed.is_set()}"
        )

        # Release Approve. It will run the (stubbed) adapter,
        # insert the SimulatedIssue, mark ToolRun=completed, and
        # commit — releasing the requirement row lock.
        release_adapter.set()

        approve_thread.join(timeout=15)
        revoke_thread.join(timeout=15)

        assert approve_result.get("status") == "ok", (
            f"Approve failed unexpectedly: {approve_result!r}"
        )
        assert revoke_committed.is_set(), (
            "Revoke did not commit after Approve released the "
            "requirement row lock"
        )

        # Final state assertions.
        db = SessionLocal()
        try:
            run = db.get(ToolRun, run_id)
            assert run.status == ToolRunStatus.COMPLETED.value
            # ``PausingAdapter.execute`` delegates to the real
            # simulator which mints a fresh ``SIM-<uuid>`` per call,
            # so we assert the deterministic prefix instead of the
            # exact external_id value.
            assert run.external_id.startswith("SIM-"), (
                f"expected simulator-shaped external_id, got "
                f"{run.external_id!r}"
            )

            sim_count = db.query(SimulatedIssue).count()
            assert sim_count == 1, (
                f"expected exactly 1 SimulatedIssue row, got "
                f"{sim_count}"
            )

            requirement = db.get(
                RequirementModel, seed["requirement_id"]
            )
            assert requirement.status == REQUIREMENT_DRAFT, (
                f"expected Requirement=draft after Approve "
                f"completed, got {requirement.status!r}"
            )
        finally:
            db.close()
    finally:
        descriptor.adapter_factory = original_factory


def test_pg_engine_isolated_from_peer_schema_lookalike_table(
    requires_postgres
):
    """Per-test fixture is isolated from any peer schema's lookalikes.

    Threat model: a peer (or a previous crashed test run) has
    already left a ``tool_runs`` table in some OTHER random
    schema on the same cluster BEFORE this test's own
    ``Base.metadata.create_all`` runs.

    The target engine's
    ``schema_translate_map={None: <target>}`` MUST qualify every
    CREATE TABLE and ORM statement to its own target schema, so:

    * ``Base.metadata.create_all`` still creates its OWN
      ``tool_runs`` in the target schema even though the peer
      schema already holds a same-named table — i.e. the
      ``has_table`` precheck must be qualified and not find the
      peer's table.
    * ORM ``SELECT`` does not surface the peer schema's
      ``tool_runs`` rows.
    * ORM ``INSERT`` writes only to the target schema.

    The test never writes to ``public`` or any pre-existing
    schema. Both ``<target>`` (``p8tt_<hex>``) and ``<peer>``
    (``p8tp_<hex>``) are per-test random schemas created by this
    test. Cleanup drops ONLY those two via
    ``DROP SCHEMA ... CASCADE``, which removes only objects owned
    by the named schema. A read-only before/after snapshot of the
    ``public`` schema's tables is taken to prove the test wrote
    nothing there.

    Coverage scope note: the ORM ``INSERT`` here inserts one row
    into ``tool_runs`` (which has FKs to ``projects`` and
    ``requirements``). The test seeds the parent rows via the
    same helpers the Phase 8 tests use (``_make_project`` +
    ``_seed_approved_requirement``) bound to the target session,
    so the FK targets live in the target schema. The probe
    therefore exercises the full ``tool_runs`` insert path —
    NOT just a stand-alone table.
    """
    from sqlalchemy import text as sa_text
    from sqlalchemy.orm import sessionmaker

    # 1. Read-only snapshot of ``public`` BEFORE the test runs.
    #    The test itself never writes to ``public``; we re-read
    #    the snapshot after teardown to prove it is unchanged.
    #    Using ``pg_tables`` (catalog) so we tolerate whatever
    #    pre-existing public state the cluster has — we do not
    #    assume public.tool_runs does or does not exist.
    def _public_tables_snapshot() -> frozenset:
        eng = create_engine(os.environ["TEST_DATABASE_URL"])
        try:
            with eng.connect() as conn:
                rows = list(
                    conn.execute(
                        sa_text(
                            "SELECT tablename FROM pg_tables "
                            "WHERE schemaname = 'public'"
                        )
                    ).scalars()
                )
                return frozenset(rows)
        finally:
            eng.dispose()

    pre_public_tables = _public_tables_snapshot()

    # 2. Spawn a peer random schema with a same-named lookalike
    #    table BEFORE the target's ``create_all`` runs. The
    #    peer's prefix is ``p8tp_<hex>``; the target's is
    #    ``p8tt_<hex>``; both are uuid4-shaped so collision is
    #    negligible. The peer engine pins its ``search_path`` to
    #    the peer schema via a connect event so unqualified
    #    CREATE / INSERT land in the peer schema and ONLY there.
    peer_schema = f"p8tp_{uuid4().hex}"
    target_schema = f"p8tt_{uuid4().hex}"
    peer_eng = create_engine(os.environ["TEST_DATABASE_URL"])
    try:
        @event.listens_for(peer_eng, "connect")
        def _set_peer_search_path(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute(
                f'SET search_path TO "{peer_schema}", public'
            )
            cur.close()

        with peer_eng.begin() as conn:
            # Create the peer random schema. CREATE SCHEMA is
            # top-level; search_path does not affect where the
            # schema itself is created.
            conn.exec_driver_sql(
                f'CREATE SCHEMA "{peer_schema}"'
            )
            # Mirror the ORM-visible columns closely enough that
            # a SELECT against the peer's ``tool_runs`` would
            # produce a row if SQLAlchemy ever fell back to the
            # peer. ``search_path`` routes it to ``peer_schema``.
            conn.exec_driver_sql(
                "CREATE TABLE tool_runs ("
                "  id uuid PRIMARY KEY,"
                "  project_id uuid NOT NULL,"
                "  requirement_id uuid NOT NULL,"
                "  tool_name text NOT NULL,"
                "  tool_action text NOT NULL,"
                "  request_id text NOT NULL,"
                "  payload jsonb NOT NULL,"
                "  status text NOT NULL,"
                "  external_id text,"
                "  failure_category text,"
                "  response_summary text,"
                "  created_at timestamptz NOT NULL DEFAULT now(),"
                "  updated_at timestamptz NOT NULL DEFAULT now(),"
                "  agent_run_id uuid"
                ")"
            )
            conn.exec_driver_sql(
                "INSERT INTO tool_runs ("
                "  id, project_id, requirement_id, tool_name, "
                "  tool_action, request_id, payload, status"
                ") VALUES ("
                f"'{uuid4()}', '{uuid4()}', '{uuid4()}', "
                f"'peer_sentinel_only', 'peer_sentinel_only', "
                f"'peer_sentinel_only', '{{}}'::jsonb, "
                f"'peer_sentinel_only'"
                ")"
            )
            # Pin the sentinel is in place inside the peer
            # schema. ``current_schema()`` should resolve to
            # ``peer_schema`` because the connect event set it.
            actual_schema = conn.exec_driver_sql(
                "SELECT current_schema()"
            ).scalar()
            assert actual_schema == peer_schema, (
                "raw peer engine search_path did not route to "
                "peer schema; "
                f"current_schema()={actual_schema!r}"
            )
            assert (
                conn.exec_driver_sql(
                    "SELECT count(*) FROM tool_runs"
                ).scalar()
                == 1
            ), "sentinel setup failed to seed peer.tool_runs"

        # 3. Build a target engine with
        #    ``schema_translate_map={None: target_schema}``.
        #    This is the engine under test. ``target_paths`` is
        #    a mutable list captured by the connect event so we
        #    can extend the per-connection search_path AFTER
        #    discovering where the cluster's ``vector`` TYPE
        #    actually lives (see step 4b below).
        target_paths: list[str] = ["public"]
        target_eng = create_engine(
            os.environ["TEST_DATABASE_URL"],
            execution_options={
                "schema_translate_map": {None: target_schema},
            },
        )
        try:
            @event.listens_for(target_eng, "connect")
            def _set_target_search_path(dbapi_conn, _):
                cur = dbapi_conn.cursor()
                # Build search_path: target_schema first, then
                # any discovered vector schema, then ``public``
                # fallback. Deduplicate while preserving order
                # so the unqualified ``vector`` TYPE reference
                # resolves through whichever schema the cluster
                # extension was installed in. We do NOT modify
                # any existing schema or extension — we only
                # SET search_path on new connections.
                seen = {target_schema}
                ordered = [target_schema]
                for path in target_paths:
                    if path not in seen:
                        seen.add(path)
                        ordered.append(path)
                quoted = ", ".join(f'"{p}"' for p in ordered)
                cur.execute(f'SET search_path TO {quoted}')
                cur.close()

            with target_eng.begin() as conn:
                conn.exec_driver_sql(
                    f'CREATE SCHEMA IF NOT EXISTS "{target_schema}"'
                )
                # Best-effort per-schema pgvector install. The
                # extension is cluster-wide, so if pgvector is
                # already installed in another schema (e.g. the
                # fixture's p8t_<hex>), PostgreSQL rejects this
                # with ``extension ... already exists``; the
                # except branch leaves the existing
                # installation untouched and continues. The
                # extension is never ``DROP``ped.
                try:
                    conn.exec_driver_sql(
                        f'CREATE EXTENSION IF NOT EXISTS vector '
                        f'SCHEMA "{target_schema}"'
                    )
                except Exception:
                    pass

            # 4b. Discover which schema the installed ``vector``
            #     extension actually lives in. We do this with
            #     a read-only catalog query — no DDL, no DML.
            #     If our target CREATE EXTENSION succeeded the
            #     extension is in ``target_schema``; if it
            #     failed because the cluster already had it
            #     elsewhere, the extension is in that other
            #     schema (typically public, or the fixture's
            #     p8t_<hex>). We extend ``target_paths`` so the
            #     unqualified ``vector`` TYPE reference
            #     resolves on the next connection.
            with target_eng.begin() as conn:
                row = conn.exec_driver_sql(
                    "SELECT n.nspname FROM pg_extension e "
                    "JOIN pg_namespace n "
                    "  ON e.extnamespace = n.oid "
                    "WHERE e.extname = 'vector'"
                ).fetchone()
            vector_ext_schema = row[0] if row is not None else None
            if (
                vector_ext_schema is not None
                and vector_ext_schema != target_schema
                and vector_ext_schema not in target_paths
            ):
                target_paths.append(vector_ext_schema)

            # 4b-prime. Force every pool connection opened
            #     before ``target_paths`` was extended to be
            #     closed. SQLAlchemy's ``connect`` event only
            #     fires on NEW physical DBAPI connections; a
            #     pooled connection keeps the ``search_path``
            #     it was opened with. If ``Base.metadata
            #     .create_all`` below checks out a connection
            #     that was opened when ``target_paths`` was
            #     just ``["public"]``, that connection will
            #     have ``search_path = "<target>", public``
            #     and the unqualified ``vector`` TYPE
            #     reference in
            #     ``CREATE TABLE ... (embedding vector)`` will
            #     fail with ``UndefinedObject``. ``dispose()``
            #     drops every pooled connection so the next
            #     checkout opens a fresh physical connection
            #     and re-runs the connect event listener with
            #     the updated ``target_paths``. The listener
            #     registration itself survives ``dispose()``
            #     — only the cached connections are released.
            target_eng.dispose()

            # 4c. The ``create_all`` under test. Even though
            #     ``peer.tool_runs`` already exists, this MUST
            #     create ``target.tool_runs`` and only
            #     ``target.tool_runs``. If ``schema_translate_map``
            #     were missing, ``has_table("tool_runs")`` would
            #     resolve against the connection search_path and
            #     could either find the peer's table (causing a
            #     skip) or fail outright. If the cluster does
            #     not have the pgvector extension installed at
            #     all this step fails with a clear
            #     ``UndefinedObject: type "vector" does not
            #     exist`` — that is fail-closed behaviour.
            Base.metadata.create_all(target_eng)

            # 5. Build a session bound to the target engine for
            #    ORM SELECT / INSERT assertions.
            TargetSession = sessionmaker(
                bind=target_eng, expire_on_commit=False
            )

            # 6. ORM SELECT must NOT see the peer sentinel.
            db = TargetSession()
            try:
                assert db.query(ToolRun).count() == 0, (
                    "ORM session read peer-schema tool_runs — "
                    "schema_translate_map is not being applied"
                )
                assert (
                    db.query(ToolRun)
                    .filter(ToolRun.tool_name == "peer_sentinel_only")
                    .count()
                    == 0
                ), "ORM session surfaced a row from peer schema"
            finally:
                db.close()

            # 7. ORM INSERT must write only to the target schema.
            #    ``tool_runs`` has FKs to ``projects`` and
            #    ``requirements``. We seed the parent rows via
            #    the existing helpers bound to the target
            #    session so the FK targets live in the target
            #    schema. This exercises the full tool_runs
            #    insert path — not a stand-alone table.
            db = TargetSession()
            try:
                project = _make_project(db)
                seed = _seed_approved_requirement(
                    db, project=project
                )
                db.commit()
            finally:
                db.close()

            db = TargetSession()
            try:
                outcome = prepare_tool_run(
                    db,
                    project_id=project.id,
                    requirement_id=seed["requirement_id"],
                    tool_name="simulated_issue_tracker",
                    tool_action="create_issue",
                    request_id=str(uuid4()),
                    payload={},
                )
                db.commit()
                inserted_id = outcome.run.id
            finally:
                db.close()

            with peer_eng.begin() as conn:
                # Peer sentinel is still exactly 1 row, untouched.
                n_peer = conn.exec_driver_sql(
                    "SELECT count(*) FROM tool_runs"
                ).scalar()
                assert n_peer == 1, (
                    f"peer-schema tool_runs row count changed to "
                    f"{n_peer}; insert leaked to peer"
                )
                # Target schema has its own tool_runs.
                n_target = conn.exec_driver_sql(
                    f'SELECT count(*) FROM "{target_schema}".tool_runs'
                ).scalar()
                assert n_target == 1, (
                    f"target-schema tool_runs expected 1 row, "
                    f"got {n_target}"
                )
                # Peer schema must not contain the inserted id.
                assert (
                    conn.exec_driver_sql(
                        "SELECT count(*) FROM tool_runs WHERE "
                        f"id = '{inserted_id}'"
                    ).scalar()
                    == 0
                ), "inserted ToolRun leaked into peer schema"
        finally:
            target_eng.dispose()
    finally:
        # 8. Drop ONLY the two random schemas this test created.
        #    CASCADE removes only objects owned by the named
        #    schema; ``public`` and any other schema are never
        #    touched. Use a fresh raw engine with no
        #    ``search_path`` override so the DROP names are
        #    unambiguous. If the DROP itself fails the test
        #    surfaces a warning and continues so the engines
        #    below are still released.
        try:
            cleanup_eng = create_engine(os.environ["TEST_DATABASE_URL"])
            try:
                with cleanup_eng.begin() as conn:
                    conn.exec_driver_sql(
                        f'DROP SCHEMA IF EXISTS '
                        f'"{target_schema}" CASCADE'
                    )
                    conn.exec_driver_sql(
                        f'DROP SCHEMA IF EXISTS '
                        f'"{peer_schema}" CASCADE'
                    )
            finally:
                cleanup_eng.dispose()
        except Exception as exc:
            import warnings

            warnings.warn(
                f"phase 8 isolation test: failed to drop test "
                f"schemas target={target_schema!r} "
                f"peer={peer_schema!r}: {exc!r}"
            )
        peer_eng.dispose()

    # 9. Read-only after-snapshot of ``public`` to prove the
    #    test wrote nothing there. Tolerates any pre-existing
    #    public state — the assertion is set equality, not
    #    non-existence.
    post_public_tables = _public_tables_snapshot()
    assert post_public_tables == pre_public_tables, (
        "public schema tables changed during the test: "
        f"pre={sorted(pre_public_tables)!r}, "
        f"post={sorted(post_public_tables)!r}"
    )


# ---- Threaded helpers -----------------------------------------------------


def _approve_in_thread(SessionLocal, project_id, tool_run_id):
    """Open a fresh session, approve, commit, close. Used by the
    PostgreSQL concurrency tests so each thread owns its own
    transaction on a separate pooled connection.
    """
    db = SessionLocal()
    try:
        run = approve_tool_run(
            db, project_id=project_id, tool_run_id=tool_run_id
        )
        db.commit()
        return run
    finally:
        db.close()


def _cancel_in_thread(SessionLocal, project_id, tool_run_id):
    """Open a fresh session, cancel, commit, close."""
    db = SessionLocal()
    try:
        run = cancel_tool_run(
            db, project_id=project_id, tool_run_id=tool_run_id
        )
        db.commit()
        return run
    finally:
        db.close()
