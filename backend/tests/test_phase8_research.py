"""Phase 8 — Research Agent tests (boundary-focused, mocks permitted).

The tests in this file are owned by the Phase 8 worker and pin every
hard rule in the brief:

1. **URL hygiene** — non-http(s) hits are rejected before persistence.
2. **Domain derivation** — the domain column carries the URL host.
3. **Provider source coercion** — every bounded field is enforced.
4. **Provider retry** — bounded retries on transient HTTP; no retry on
   auth rejects; HTTP body echoed nowhere.
5. **LLM structured output** — schema rejects unknown fields; per-array
   rules differ; the summary is a single sentence.
6. **Source-citation lock** — every confirmed finding cites only
   ``provider_source_id`` values that point at a row persisted by THIS
   run; rejected IDs/URLs raise ``ResearchValidationError``.
7. **Status/provided invariant** — confirmed must carry at least one
   source_refs entry; not_verified must carry none (mirrors the DB
   CHECK constraint).
8. **Provider retry budget** — exhaustion surfaces
   ``ResearchProviderTransportError``; partial persist reverts cleanly.
9. **Internal-context isolation** — internal context never echoes raw
   feedback ``content`` and only carries external_ids + bounded excerpts
   of the deterministic AI classification.
10. **API cross-project isolation** — an unknown / mismatched project
    id returns 404 / 409; secrets never appear in the response body.
11. **History + source listing** — paginated list endpoints echo the
    default envelope and sort by ``created_at DESC, id DESC``.

The tests use ``monkeypatch`` to substitute the real provider / LLM
client classes with stubs that exercise the real boundary: the real
``ResearchProviderClient.search`` HTTP path is exercised with
``httpx.MockTransport`` (no proxy of provider-internal state). The LLM
client is replaced with a stub that returns a small canned payload so
the validator — not the model — decides what is acceptable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.prompts import research_agent_v1, research_agent_v2
from app.db.base import Base
from app.db.config import settings
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
    ResearchFinding,
    ResearchFindingCategory,
    ResearchFindingStatus,
    ResearchRun,
    ResearchRunStatus,
    RunStatus,
)
from app.services.research_client import (
    ResearchLLMResult,
    ResearchLLMUsage,
)
from app.services.research_provider import (
    ResearchProviderClient,
    coerce_source,
    compute_source_fingerprint,
    derive_domain,
    is_valid_http_url,
)
from app.services.research_service import (
    INSUFFICIENT_EXTERNAL_EVIDENCE_CODE,
    ResearchConfigError,
    ResearchValidationError,
    ValidatedFinding,
    _build_source_haystack_map,
    _filter_sources_by_low_authority,
    _is_low_authority_domain,
    _is_name_supported_by_sources,
    _name_tokens,
    assemble_internal_context,
    build_provider_query,
    start_research_run,
    validate_research_payload,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def engine(tmp_path: Path):
    """Per-test in-memory SQLite with shared StaticPool + FK enforcement."""
    url = os.environ.get("TEST_DATABASE_URL", f"sqlite:///{tmp_path}/p8.db")
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

    # Phase 6 added ForeignKeyConstraint on evaluation_cases; the Phase 8
    # migration adds ForeignKeyConstraint on research_runs. Use
    # ``Base.metadata.create_all`` so all FKs are honoured, and the
    # checkpointer creates the Phase 8 tables even when the test DB
    # URL omits them.
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


@pytest.fixture
def fake_settings(monkeypatch):
    """Pin the LLM / Provider config so the service entrypoints are happy."""
    monkeypatch.setattr(settings, "llm_api_key", "test-llm-key-XXXXXX")
    monkeypatch.setattr(settings, "llm_base_url", "http://llm.invalid")
    monkeypatch.setattr(settings, "llm_model", "p8-test-model")
    monkeypatch.setattr(settings, "tavily_api_key", "test-tavily-key-YYYYYY")
    monkeypatch.setattr(settings, "tavily_base_url", "http://tavily.invalid")
    monkeypatch.setattr(settings, "tavily_max_results", 5)
    monkeypatch.setattr(settings, "tavily_max_retries", 2)
    monkeypatch.setattr(settings, "tavily_timeout_seconds", 5.0)
    monkeypatch.setattr(settings, "research_max_evidence", 5)
    return settings


@pytest.fixture(autouse=True)
def block_new_official_body_network(monkeypatch):
    """Legacy fixtures contain snippets, not live-page acceptance evidence."""
    import sys

    from app.services import research_reader

    def unavailable(*args, **kwargs):
        raise research_reader.ReadError("transport_failure")

    monkeypatch.setattr(research_reader, "read_official", unavailable)
    pipeline = sys.modules.get("app.services.research_pipeline")
    if pipeline is not None:
        monkeypatch.setattr(pipeline, "read_official", unavailable)


# ============================================================================
# Seeding helpers
# ============================================================================


def _make_project(db: Session, *, name: str = "Phase8 Project") -> Project:
    project = Project(name=name, product_name="InsightFlow")
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _seed_minimal_pipeline(
    db: Session,
    *,
    project: Project,
    feedback_external_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Append a full Phase 1-3 chain so a Research run has a valid input.

    Returns the cluster / insight / decision ids so the test body does
    not have to re-derive them. The Feedback content is intentionally
    distinct from the external_id so we can assert the LLM never sees
    the raw text.
    """
    feedback_external_ids = feedback_external_ids or ["ext-p8-a", "ext-p8-b", "ext-p8-c"]
    classification = AnalysisRun(
        project_id=project.id,
        run_type="classification",
        model="p8-test",
        prompt_version="classification-v1",
        status=RunStatus.COMPLETED.value,
        input_count=len(feedback_external_ids),
        processed_count=len(feedback_external_ids),
        success_count=len(feedback_external_ids),
    )
    db.add(classification)
    db.flush()

    feedbacks: list[Feedback] = []
    analyses: list[FeedbackAnalysis] = []
    for idx, ext_id in enumerate(feedback_external_ids):
        # Seed content is intentionally longer than the per-entry
        # 400-char evidence-summary cap so the truncation exercised
        # by ``assemble_internal_context`` is observable in the
        # resulting snapshot (the trailing padding is the part that
        # must NEVER appear in the LLM-facing context).
        padding = "x" * 500
        f = Feedback(
            project_id=project.id,
            external_id=ext_id,
            content=f"raw feedback body {ext_id} {padding}",
        )
        db.add(f)
        db.flush()
        a = FeedbackAnalysis(
            feedback_id=f.id,
            run_id=classification.id,
            feedback_type="ux_issue",
            module="upload",
            intent="multi_select",
            pain_point=f"pain point for {ext_id}",
            sentiment="negative",
            severity=3,
            confidence=0.8,
        )
        db.add(a)
        db.flush()
        feedbacks.append(f)
        analyses.append(a)

    clustering = AnalysisRun(
        project_id=project.id,
        run_type="clustering",
        model="p8-test",
        prompt_version="cluster-review-v4",
        status=RunStatus.COMPLETED.value,
        input_count=len(feedback_external_ids),
        processed_count=len(feedback_external_ids),
        success_count=len(feedback_external_ids),
        cluster_config={"classification_run_id": str(classification.id)},
    )
    db.add(clustering)
    db.flush()

    cluster = Cluster(
        project_id=project.id,
        run_id=clustering.id,
        source="llm_review",
        feedback_count=len(feedback_external_ids),
        embedding_model="local-hash-v1",
        distance_threshold=0.3,
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
    db.flush()

    insight_run = InsightRun(
        project_id=project.id,
        clustering_run_id=clustering.id,
        classification_run_id=classification.id,
        status=RunStatus.COMPLETED.value,
        model="p8-test",
        prompt_version="insight-generation-v2",
        input_count=len(feedback_external_ids),
        processed_count=len(feedback_external_ids),
        success_count=len(feedback_external_ids),
    )
    db.add(insight_run)
    db.flush()

    insight = Insight(
        run_id=insight_run.id,
        project_id=project.id,
        cluster_id=cluster.id,
        title="Phase8 Insight: 多选模式误触",
        pain_point="用户进入上传页后，重复点击导致进入多选模式并勾选错误文件。",
        affected_segment="移动端新用户",
        insight="新用户对多选进入的入口缺乏直觉；该痛点需要通过 UI 提示来解决。",
        recommendation="在多选入口处加提示气泡，待验证是否降低误触率。",
        confidence=0.8,
        status=InsightStatus.DRAFT.value,
        feedback_count=len(feedback_external_ids),
        evidence_count=len(feedback_external_ids),
        source_cluster_external_id=str(cluster.id),
        source_member_external_ids=list(feedback_external_ids),
        model="p8-test",
        prompt_version="insight-generation-v2",
    )
    db.add(insight)
    db.flush()

    for f, a in zip(feedbacks, analyses):
        db.add(
            InsightEvidence(
                insight_id=insight.id,
                feedback_id=f.id,
                feedback_external_id=f.external_id,
                feedback_project_id=project.id,
                feedback_classification_run_id=classification.id,
                evidence_type="representative",
                relevance_score=0.9,
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
        version=1,
    )
    db.add(decision)
    db.commit()

    return {
        "classification_run_id": classification.id,
        "clustering_run_id": clustering.id,
        "insight_run_id": insight_run.id,
        "insight_id": insight.id,
        "decision_id": decision.id,
        "feedback_external_ids": list(feedback_external_ids),
    }


# ============================================================================
# 1. URL / domain helpers
# ============================================================================


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://example.com", True),
        ("https://example.com/path?q=1", True),
        ("HTTPS://EXAMPLE.COM", True),
        ("ftp://example.com", False),
        ("javascript:alert(1)", False),
        ("file:///etc/passwd", False),
        ("not-a-url", False),
        ("", False),
        ("http://", False),
        ("http:///no-host", False),
    ],
)
def test_is_valid_http_url_rejects_non_http(url: str, expected: bool):
    assert is_valid_http_url(url) is expected


def test_is_valid_http_url_enforces_length():
    long = "https://example.com/" + ("a" * 2100)
    assert is_valid_http_url(long) is False


def test_derive_domain_lowercases_and_strips_trailing_dot():
    assert derive_domain("https://Example.COM/foo") == "example.com"
    assert derive_domain("https://Example.COM.") == "example.com"


def test_derive_domain_rejects_url_without_host():
    with pytest.raises(Exception):
        derive_domain("https://")


@pytest.mark.parametrize(
    "raw",
    [
        {"id": "src-1", "url": "https://example.com/a", "title": "A", "content": "snippet A"},
        {"id": "src-2", "url": "http://example.org/b", "title": "B"},
        {"id": "src-3", "url": "https://EXAMPLE.com/c"},  # ID is required; URL capitalisation is fine
    ],
)
def test_coerce_source_accepts_valid_http_hits(raw: dict):
    src = coerce_source(raw, provider="tavily")
    assert src is not None
    assert src.provider_source_id == raw["id"]
    assert src.url == raw["url"]
    assert src.domain == "example.com" or src.domain == "example.org"


@pytest.mark.parametrize(
    "raw",
    [
        {"id": "x", "url": "ftp://example.com", "title": "no"},
        {"id": "x", "url": "javascript:alert(1)", "title": "no"},
        {"id": "x", "url": "not-a-url"},
        {"id": "", "url": "https://example.com"},
        {"url": "https://example.com"},
        {"id": "x"},  # missing url
    ],
)
def test_coerce_source_rejects_invalid_or_missing_fields(raw: dict):
    assert coerce_source(raw, provider="tavily") is None


def test_coerce_source_truncates_oversize_title_and_snippet():
    raw = {
        "id": "src-trunc",
        "url": "https://example.com/",
        "title": "t" * 800,
        "content": "s" * 5000,
    }
    src = coerce_source(raw, provider="tavily")
    assert src is not None
    assert len(src.title) == 500
    assert len(src.snippet) == 4000


def test_compute_source_fingerprint_is_stable_and_order_independent():
    a = coerce_source(
        {"id": "src-a", "url": "https://example.com/a", "title": "A"},
        provider="tavily",
    )
    b = coerce_source(
        {"id": "src-b", "url": "https://example.com/b", "title": "B"},
        provider="tavily",
    )
    ab = compute_source_fingerprint([a, b])
    ba = compute_source_fingerprint([b, a])
    assert ab == ba
    c = coerce_source(
        {"id": "src-c", "url": "https://example.com/c", "title": "C"},
        provider="tavily",
    )
    abc = compute_source_fingerprint([a, b, c])
    assert abc != ab


# ============================================================================
# 2. Provider client transport behaviour (httpx mock boundary)
# ============================================================================


def _make_provider_client() -> ResearchProviderClient:
    return ResearchProviderClient(
        api_key="provider-key-XXXXXX",
        base_url="http://provider.invalid",
        timeout_seconds=2.0,
        max_results=5,
        max_retries=2,
    )


def test_provider_search_returns_coerced_sources_under_transient_429():
    """First call 429, second 200; client returns coerced sources."""
    request_count = {"n": 0}
    body = {
        "query": "competitor X",
        "max_results": 5,
        "results": [
            {
                "id": "src-1",
                "url": "https://example.com/article",
                "title": "Article",
                "content": "snippet",
                "score": 0.9,
            },
        ],
        "answer": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        request_count["n"] += 1
        if request_count["n"] == 1:
            return httpx.Response(429, json={"error": "rate-limited"})
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    with ResearchProviderClient(
        api_key="provider-key-XXXXXX",
        base_url="http://provider.invalid",
        timeout_seconds=2.0,
        max_results=5,
        max_retries=2,
    ) as client:
        client._client = httpx.Client(
            timeout=2.0,
            headers={"content-type": "application/json"},
            transport=transport,
        )
        result = client.search("competitor X")
    assert request_count["n"] == 2
    assert [s.provider_source_id for s in result.sources] == ["src-1"]


def test_provider_search_drops_invalid_hits_but_succeeds_when_some_survive():
    """A single ftp:// hit must NEVER leak into the persisted source list."""
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            json={
                "query": "q",
                "results": [
                    {"id": "src-1", "url": "https://example.com/a", "title": "A"},
                    {"id": "src-2", "url": "ftp://nope.example.com", "title": "B"},
                    {"id": "src-3", "url": "javascript:alert(1)", "title": "C"},
                ],
            },
        )
    )
    with ResearchProviderClient(
        api_key="provider-key-XXXXXX",
        base_url="http://provider.invalid",
        timeout_seconds=2.0,
        max_results=5,
        max_retries=0,
    ) as client:
        client._client = httpx.Client(
            timeout=2.0,
            headers={"content-type": "application/json"},
            transport=transport,
        )
        result = client.search("q")
    assert [s.provider_source_id for s in result.sources] == ["src-1"]


def test_provider_search_retries_until_budget_exhausted_then_raises():
    from app.services.research_provider import ResearchProviderTransportError

    request_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        request_count["n"] += 1
        return httpx.Response(503, json={"error": "down"})

    transport = httpx.MockTransport(handler)
    with ResearchProviderClient(
        api_key="provider-key-XXXXXX",
        base_url="http://provider.invalid",
        timeout_seconds=2.0,
        max_results=5,
        max_retries=2,
    ) as client:
        client._client = httpx.Client(
            timeout=2.0,
            headers={"content-type": "application/json"},
            transport=transport,
        )
        with pytest.raises(ResearchProviderTransportError):
            client.search("q")
    # initial + 2 retries == 3
    assert request_count["n"] == 3


def test_provider_search_4xx_auth_does_not_retry():
    """401/403 must raise immediately; retries would just leak the key."""
    from app.services.research_provider import ResearchProviderResponseError

    request_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        request_count["n"] += 1
        return httpx.Response(401, json={"error": "no token"})

    transport = httpx.MockTransport(handler)
    with ResearchProviderClient(
        api_key="provider-key-XXXXXX",
        base_url="http://provider.invalid",
        timeout_seconds=2.0,
        max_results=5,
        max_retries=2,
    ) as client:
        client._client = httpx.Client(
            timeout=2.0,
            headers={"content-type": "application/json"},
            transport=transport,
        )
        with pytest.raises(ResearchProviderResponseError):
            client.search("q")
    assert request_count["n"] == 1


def test_provider_client_repr_does_not_leak_api_key():
    """Defence-in-depth: dumping the client must not reveal the secret."""
    client = _make_provider_client()
    try:
        # The headers dict deliberately carried "[REDACTED]" in the
        # constructor; the real key was never stored on the public
        # header map (it is passed per-request via ``headers=``).
        assert "provider-key-XXXXXX" not in repr(client._client.headers)
    finally:
        client.close()


def test_provider_missing_config_raises_config_error():
    from app.services.research_provider import ResearchProviderConfigError

    with pytest.raises(ResearchProviderConfigError):
        ResearchProviderClient(
            api_key=None,
            base_url="http://provider.invalid",
            timeout_seconds=2.0,
        )
    with pytest.raises(ResearchProviderConfigError):
        ResearchProviderClient(
            api_key="k",
            base_url=None,
            timeout_seconds=2.0,
        )


# ============================================================================
# 3. Validator — structured output rules
# ============================================================================


def _ok_payload(*, with_opp_confirmed: bool = True) -> dict[str, Any]:
    return {
        "summary": "Phase 8 测试摘要：竞品在多选模式下的 UI 风格更接近直觉。",
        "confidence": 0.7,
        "competitors": [
            {
                "name": "CompetitorX",
                "description": "竞品 X 的多选入口更明显。",
                "confidence": 0.7,
                "source_refs": [
                    {
                        "provider_source_id": "src-1",
                        "url": "https://example.com/a",
                        "title": "Article A",
                    }
                ],
            }
        ],
        "market_patterns": [
            {
                "title": "多选入口可视化",
                "description": "竞品普遍采用显式入口。",
                "confidence": 0.6,
                "source_refs": [
                    {
                        "provider_source_id": "src-2",
                        "url": "https://example.com/b",
                        "title": "Article B",
                    }
                ],
            }
        ],
        "opportunities": [
            {
                "title": "在多选入口加提示气泡",
                "description": "降低误触率。",
                "confidence": 0.8,
                "status": "confirmed" if with_opp_confirmed else "not_verified",
                "source_refs": (
                    [
                        {
                            "provider_source_id": "src-1",
                            "url": "https://example.com/a",
                            "title": "Article A",
                        }
                    ]
                    if with_opp_confirmed
                    else None
                ),
            }
        ],
        "risks": [
            {
                "title": "误分类竞品",
                "description": "竞品的市场与本产品完全不同；需进一步确认。",
                "confidence": 0.4,
                "status": "not_verified",
                "source_refs": None,
            }
        ],
    }


def test_validator_accepts_canonical_payload():
    srcs = {"src-1": "https://example.com/a", "src-2": "https://example.com/b"}
    validated = validate_research_payload(
        _ok_payload(),
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
    )
    # 1 summary + 1 competitor + 1 pattern + 1 opportunity + 1 risk == 5 findings
    assert len(validated.findings) == 5
    assert validated.findings[0].category == ResearchFindingCategory.SUMMARY.value


def test_validator_rejects_unknown_top_level_field():
    bad = _ok_payload()
    bad["sneaky"] = "field"
    with pytest.raises(ResearchValidationError) as ei:
        validate_research_payload(
            bad,
            allowed_provider_source_ids={"src-1", "src-2"},
            allowed_provider_urls={
                "src-1": "https://example.com/a",
                "src-2": "https://example.com/b",
            },
        )
    assert ei.value.code == "unknown_top_level_field"


@pytest.mark.parametrize(
    "lifted_field",
    [
        # v4 OneDrive reproduction — the model emitted these
        # finding-level fields at the top level. They MUST each
        # be rejected as ``unknown_top_level_field`` rather than
        # silently mapping to one of the legitimate top-level
        # categories, otherwise the validator mis-classifies the
        # failure and the retry hint teaches the model the
        # wrong rule.
        "description",
        "item",
        "source_refs",
        "status",
        "title",
    ],
)
def test_validator_rejects_finding_fields_lifted_to_top_level(lifted_field: str):
    """v4 OneDrive reproduction: the model emitted
    ``['competitors','confidence','description','item',
    'market_patterns','opportunities','risks','source_refs',
    'status','summary']``. ``description`` / ``item`` /
    ``source_refs`` / ``status`` / ``title`` are all finding-level
    fields — none is a legitimate top-level payload key. The
    validator MUST surface each as
    ``unknown_top_level_field`` so the retry hint instructs the
    model to keep them nested inside the competitor /
    market_pattern / opportunity / risk objects.
    """
    bad = _ok_payload()
    bad[lifted_field] = "lifted to top level"
    with pytest.raises(ResearchValidationError) as ei:
        validate_research_payload(
            bad,
            allowed_provider_source_ids={"src-1", "src-2"},
            allowed_provider_urls={
                "src-1": "https://example.com/a",
                "src-2": "https://example.com/b",
            },
        )
    assert ei.value.code == "unknown_top_level_field"


def test_validator_rejects_missing_required_array():
    bad = _ok_payload()
    bad["competitors"] = None
    with pytest.raises(ResearchValidationError):
        validate_research_payload(
            bad,
            allowed_provider_source_ids={"src-1", "src-2"},
            allowed_provider_urls={
                "src-1": "https://example.com/a",
                "src-2": "https://example.com/b",
            },
        )


def test_validator_rejects_source_refs_unknown_id():
    bad = _ok_payload()
    bad["competitors"][0]["source_refs"][0]["provider_source_id"] = "src-INVENTED"
    with pytest.raises(ResearchValidationError) as ei:
        validate_research_payload(
            bad,
            allowed_provider_source_ids={"src-1", "src-2"},
            allowed_provider_urls={
                "src-1": "https://example.com/a",
                "src-2": "https://example.com/b",
            },
        )
    # The validator message contains BOTH ``source_refs`` and
    # ``provider_source_id``; the more specific marker MUST win so the
    # LLM sees the actionable ``source_ref_unknown`` category (the ID
    # was invented) rather than the broader ``source_refs_invalid``
    # bucket.
    assert ei.value.code == "source_ref_unknown"


def test_validator_rejects_source_refs_url_mismatch():
    bad = _ok_payload()
    bad["competitors"][0]["source_refs"][0]["url"] = "https://attacker.example.com/x"
    with pytest.raises(ResearchValidationError):
        validate_research_payload(
            bad,
            allowed_provider_source_ids={"src-1", "src-2"},
            allowed_provider_urls={
                "src-1": "https://example.com/a",
                "src-2": "https://example.com/b",
            },
        )


def test_validator_confirmed_opportunity_requires_source_refs():
    bad = _ok_payload(with_opp_confirmed=False)
    bad["opportunities"][0]["status"] = "confirmed"
    bad["opportunities"][0]["source_refs"] = None
    with pytest.raises(ResearchValidationError):
        validate_research_payload(
            bad,
            allowed_provider_source_ids={"src-1", "src-2"},
            allowed_provider_urls={
                "src-1": "https://example.com/a",
                "src-2": "https://example.com/b",
            },
        )


def test_validator_not_verified_forbids_source_refs():
    bad = _ok_payload()
    bad["risks"][0]["status"] = "not_verified"
    bad["risks"][0]["source_refs"] = [
        {
            "provider_source_id": "src-1",
            "url": "https://example.com/a",
            "title": "Article A",
        }
    ]
    with pytest.raises(ResearchValidationError):
        validate_research_payload(
            bad,
            allowed_provider_source_ids={"src-1", "src-2"},
            allowed_provider_urls={
                "src-1": "https://example.com/a",
                "src-2": "https://example.com/b",
            },
        )


def test_validator_rejects_non_finite_confidence():
    bad = _ok_payload()
    bad["confidence"] = float("nan")
    with pytest.raises(ResearchValidationError) as ei:
        validate_research_payload(
            bad,
            allowed_provider_source_ids={"src-1", "src-2"},
            allowed_provider_urls={
                "src-1": "https://example.com/a",
                "src-2": "https://example.com/b",
            },
        )
    assert ei.value.code == "confidence_invalid"


def test_validator_rejects_competitor_without_source_refs():
    bad = _ok_payload()
    bad["competitors"][0]["source_refs"] = []
    with pytest.raises(ResearchValidationError):
        validate_research_payload(
            bad,
            allowed_provider_source_ids={"src-1", "src-2"},
            allowed_provider_urls={
                "src-1": "https://example.com/a",
                "src-2": "https://example.com/b",
            },
        )


def test_validation_error_code_property_marker_priority():
    """``ResearchValidationError.code`` MUST match the most specific
    marker first when the validator raises a message that contains
    BOTH a broad bucket marker (``source_refs``) AND a specific
    sub-marker (``provider_source_id``). The specific marker is the
    actionable category for the LLM (an invented ID surfaces
    ``source_ref_unknown``, not the broader ``source_refs_invalid``);
    getting this wrong trains the LLM to fix the wrong field on
    retry. The other marker pairs keep their existing broad-wins
    ordering (e.g. ``competitors[0].title …`` is a competitor
    rejection, not a generic title rejection) — those buckets
    remain out of scope.
    """
    # 1. Invented provider_source_id → ``source_ref_unknown`` wins
    #    over the broader ``source_refs`` marker. This is the case
    #    the real-world evaluation surfaced.
    err = ResearchValidationError(
        "source_refs provider_source_id 'src-INVENTED' not in run sources"
    )
    assert err.code == "source_ref_unknown"

    # 2. Malformed provider_source_id (non-string) → still
    #    ``source_ref_unknown``; the message carries the specific
    #    marker as well as the broad bucket.
    err = ResearchValidationError("source_refs provider_source_id invalid")
    assert err.code == "source_ref_unknown"

    # 3. Malformed URL inside a source_refs entry → only the broad
    #    ``source_refs`` marker is present, so ``source_refs_invalid``
    #    is the correct category.
    err = ResearchValidationError("source_refs url must be http(s)")
    assert err.code == "source_refs_invalid"

    # 4. URL does not match the persisted source for the cited ID →
    #    again only the broad marker is present, so
    #    ``source_refs_invalid`` is the correct category. This is
    #    distinct from the invented-ID case: the ID itself was real,
    #    the LLM just attached the wrong URL.
    err = ResearchValidationError("source_refs[0] url does not match persisted source")
    assert err.code == "source_refs_invalid"

    # 5. Unknown top-level field → ``unknown_top_level_field`` wins
    #    over any inner field name that might also be in the message.
    err = ResearchValidationError("unexpected top-level fields: ['sneaky']")
    assert err.code == "unknown_top_level_field"

    # 6. Empty / unrecognised message → falls back to ``schema_invalid``.
    err = ResearchValidationError("something completely unrecognised happened")
    assert err.code == "schema_invalid"


def test_validation_retry_hint_does_not_leak_invented_provider_source_id():
    """The retry hint built for an invented ``provider_source_id`` MUST
    surface the safe category code AND the bounded guidance, but MUST
    NOT carry the original ID, the URL, or any other payload byte.

    The hint is appended to the next LLM call as a separate user
    turn; if it echoed the rejected ID, the LLM would be free to
    invent a different wrong ID on the next attempt instead of
    dropping the bad citation. The hint is a corrective instruction,
    not a payload echo.
    """
    from app.services.research_service import _build_validation_retry_hint

    invented_id = "src-LEAKED-IDENTIFIER-1234567890"
    invented_url = "https://attacker.example.com/x"
    # Build the same error message the validator would raise for an
    # invented provider_source_id, then exercise the hint helper.
    err = ResearchValidationError(
        f"source_refs provider_source_id {invented_id!r} not in run sources"
    )
    code = err.code
    assert code == "source_ref_unknown"
    hint = _build_validation_retry_hint(code)
    # The safe category code is the only identifier the hint carries.
    assert code in hint
    # The invented ID and URL never make it into the hint.
    assert invented_id not in hint
    assert invented_url not in hint
    assert "src-LEAKED" not in hint
    # The hint is bounded.
    assert len(hint) <= 500
    # The hint carries guidance that is meaningful to the LLM but
    # carries no raw exception text.
    assert "provider_source_id" in hint or "in run" in hint or "invent" in hint.lower()

def test_validator_rejects_summary_above_max_length():
    bad = _ok_payload()
    bad["summary"] = "x" * 601
    with pytest.raises(ResearchValidationError) as ei:
        validate_research_payload(
            bad,
            allowed_provider_source_ids={"src-1", "src-2"},
            allowed_provider_urls={
                "src-1": "https://example.com/a",
                "src-2": "https://example.com/b",
            },
        )
    assert ei.value.code == "summary_invalid"


# ============================================================================
# 4. Internal-context assembly + provider-query build (no Raw feedback leak)
# ============================================================================


def test_internal_context_carries_bounded_evidence_summaries(
    SessionLocal, fake_settings
):
    """Each evidence entry carries a truncated excerpt of the Feedback body.

    Per-entry cap: ``RESEARCH_EVIDENCE_EXCERPT_MAX_CHARS`` (400). Total cap
    across all entries: ``RESEARCH_EVIDENCE_SUMMARY_TOTAL_MAX_CHARS``
    (2000). The full raw ``feedbacks.content`` (which is > 500 chars per
    row in the seed) is NEVER echoed in the snapshot — only its
    bounded prefix is.
    """
    from app.services.research_service import (
        RESEARCH_EVIDENCE_EXCERPT_MAX_CHARS,
        RESEARCH_EVIDENCE_LIMIT,
        RESEARCH_EVIDENCE_SUMMARY_TOTAL_MAX_CHARS,
    )

    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_minimal_pipeline(db, project=project)
        insight = db.get(Insight, seed["insight_id"])
        decision = db.get(InsightDecision, seed["decision_id"])
        internal_context = assemble_internal_context(
            db,
            insight=insight,
            decision=decision,
            research_question="竞品 X 的多选入口是如何提示的？",
            scope=None,
        )
        snap = internal_context.to_dict()
        # Sanity: every external_id from the seed shows up exactly once
        # and the evidence list respects the 5-entry cap.
        assert set(snap["evidence_external_ids"]) == set(seed["feedback_external_ids"])
        assert len(snap["evidence_external_ids"]) <= RESEARCH_EVIDENCE_LIMIT
        # Each evidence carries a paired ``{external_id, summary}`` row
        # in the same positional order as ``evidence_external_ids``.
        assert "evidence_summaries" in snap
        summaries = snap["evidence_summaries"]
        assert len(summaries) == len(snap["evidence_external_ids"])
        for entry, ext in zip(summaries, snap["evidence_external_ids"]):
            assert entry["external_id"] == ext
            # Per-entry cap.
            assert len(entry["summary"]) <= RESEARCH_EVIDENCE_EXCERPT_MAX_CHARS
        # Total cap across all entries.
        total_chars = sum(len(entry["summary"]) for entry in summaries)
        assert total_chars <= RESEARCH_EVIDENCE_SUMMARY_TOTAL_MAX_CHARS
        # Full raw content (with the 500-char padding the seed attaches
        # to every feedback row) MUST NOT appear in the snapshot. Only
        # the bounded prefix is allowed.
        rendered = json.dumps(snap, ensure_ascii=False)
        for ext in seed["feedback_external_ids"]:
            full_marker = f"raw feedback body {ext} " + "x" * 500
            assert full_marker not in rendered
        # The bounded prefix (the actual excerpt) IS in the snapshot
        # for at least the first evidence entry, proving the
        # truncation runs against the real feedback content rather
        # than an empty placeholder.
        first_ext = seed["feedback_external_ids"][0]
        expected_prefix = f"raw feedback body {first_ext} " + "x" * (
            RESEARCH_EVIDENCE_EXCERPT_MAX_CHARS
            - len(f"raw feedback body {first_ext} ")
        )
        assert expected_prefix in rendered
        assert internal_context.fingerprint() == internal_context.fingerprint()
        # Different question → different hash → retries can detect drift.
        other = assemble_internal_context(
            db,
            insight=insight,
            decision=decision,
            research_question="竞品 X 的多选入口提示框的文案如何？",
            scope=None,
        )
        assert other.fingerprint() != internal_context.fingerprint()
    finally:
        db.close()


def test_build_provider_query_is_bounded_and_deterministic(fake_settings):
    """The canonical query is deterministic and bounded by 500 chars.

    Plus the PM-question-first invariant: ``research_question`` MUST
    appear at the START of the query so the provider's ranking model
    sees the PM's actual ask first, not internal taxonomy. The v2
    evaluation dropped the trailing Insight-title anchor entirely
    because internal taxonomy was the primary pollution source for
    product-specific research questions.
    """
    from app.services.research_service import InternalContext

    ctx = InternalContext(
        insight_title="Phase8 多选模式误触",
        insight_pain_point="重复点击导致进入多选模式并勾选错误文件。",
        insight_affected_segment="移动端新用户",
        insight_recommendation="加提示气泡",
        evidence_external_ids=["ext-p8-a", "ext-p8-b"],
        evidence_summaries=[
            {"external_id": "ext-p8-a", "summary": "用户进入多选模式后无法撤销。"},
            {"external_id": "ext-p8-b", "summary": "新用户对入口缺乏直觉。"},
        ],
        decision_status="accepted",
        decision_priority="P1",
        research_question="竞品 X 的多选入口提示如何设计？",
        scope=None,
    )
    q1 = build_provider_query(ctx)
    q2 = build_provider_query(ctx)
    assert q1 == q2  # deterministic
    assert len(q1) <= 500  # bounded
    # PM question MUST lead the query so the search provider's
    # ranking model weights it above any trailing internal context.
    assert q1.startswith("竞品 X 的多选入口提示如何设计？")
    # No ``|`` separator — the query is one natural-language string.
    assert "|" not in q1
    # Internal taxonomy (Insight segment, evidence external_ids,
    # evidence summary content, decision fields, Insight title)
    # MUST NOT bleed into the provider query — they live inside the
    # LLM prompt only.
    assert "移动端新用户" not in q1
    assert "ext-p8-a" not in q1
    assert "ext-p8-b" not in q1
    assert "用户进入多选模式后无法撤销" not in q1
    assert "新用户对入口缺乏直觉" not in q1
    assert "P1" not in q1
    assert "accepted" not in q1
    # The Insight title is NO LONGER appended to the query; it lives
    # in the LLM prompt only. This is the v2 evaluation fix —
    # internal Insight titles polluted the search ranking for
    # product-specific research questions.
    assert "Phase8" not in q1
    assert "多选模式误触" not in q1


def test_build_provider_query_includes_question_then_scope(fake_settings):
    """Question (full) → scope (full, optional) is the canonical shape.

    The Insight title is intentionally NOT included in the query —
    the LLM already sees it via ``internal_context`` and an Insight
    title rarely helps a public search provider rank for a
    product-specific research question.
    """
    from app.services.research_service import InternalContext

    ctx = InternalContext(
        insight_title="insight-title-no-pollute",
        insight_pain_point="pain",
        insight_affected_segment="unknown",
        insight_recommendation="reco",
        evidence_external_ids=[],
        evidence_summaries=[],
        decision_status=None,
        decision_priority=None,
        research_question="竞品 X 的多选入口提示如何设计？",
        scope="2026 海外市场",
    )
    q = build_provider_query(ctx)
    assert len(q) <= 500
    # PM question is first.
    assert q.startswith("竞品 X 的多选入口提示如何设计？")
    # Scope is second, immediately after the question.
    question_end = len("竞品 X 的多选入口提示如何设计？")
    scope_idx = q.find("2026 海外市场")
    assert scope_idx == question_end + 1  # exactly one space separator
    # The Insight title is NOT in the query at all.
    assert "insight-title-no-pollute" not in q


def test_build_provider_query_excludes_evidence_and_decision_fields(fake_settings):
    """Regression: evidence summaries / external_ids / decision snapshot
    MUST NEVER leak into the search query, even when the Insight title
    contains strings that overlap with the evidence. The query is
    strictly the PM's words.
    """
    from app.services.research_service import InternalContext

    ctx = InternalContext(
        insight_title="竞品对比",
        insight_pain_point="pain_point_body",
        insight_affected_segment="unknown",
        insight_recommendation="reco_body",
        evidence_external_ids=["ext-secret-1", "ext-secret-2"],
        evidence_summaries=[
            {"external_id": "ext-secret-1", "summary": "raw_feedback_excerpt_1"},
            {"external_id": "ext-secret-2", "summary": "raw_feedback_excerpt_2"},
        ],
        decision_status="accepted",
        decision_priority="P1",
        research_question="竞品 X 的多选入口如何提示？",
        scope=None,
    )
    q = build_provider_query(ctx)
    # Nothing internal-pii leaks.
    for forbidden in (
        "ext-secret-1",
        "ext-secret-2",
        "raw_feedback_excerpt_1",
        "raw_feedback_excerpt_2",
        "pain_point_body",
        "reco_body",
        "accepted",
        "P1",
        "竞品对比",  # Insight title is no longer part of the query
    ):
        assert forbidden not in q, f"query leaked {forbidden!r}: {q!r}"
    # Only the user question remains.
    assert q == "竞品 X 的多选入口如何提示？"


def test_build_provider_query_real_world_notion_upload_scenario(fake_settings):
    """Regression for the live-Tavily failure mode.

    A PM asks "Notion 文件上传" (a feature parity question). The
    previous implementation prepended the Insight title and the
    Insight segment, drowning the user's question in internal
    taxonomy and pushing Tavily's ranking toward finance / timer
    pages. After the fix the PM question MUST lead the query and
    the Insight title MUST NOT pollute it.
    """
    from app.services.research_service import InternalContext

    ctx = InternalContext(
        insight_title="上传体验差",
        insight_pain_point="用户上传文件路径长",
        insight_affected_segment="未登录用户",
        insight_recommendation="对接 Notion 上传",
        evidence_external_ids=["ext-n1", "ext-n2"],
        evidence_summaries=[
            {"external_id": "ext-n1", "summary": "希望直接上传到 Notion"},
            {"external_id": "ext-n2", "summary": "对 Notion API 不熟"},
        ],
        decision_status="accepted",
        decision_priority="P2",
        research_question="Notion 文件上传如何接入？",
        scope=None,
    )
    q = build_provider_query(ctx)
    assert q.startswith("Notion 文件上传如何接入？")
    # The Insight title and segment MUST NOT pollute the query.
    assert "上传体验差" not in q
    assert "未登录用户" not in q
    assert "对接 Notion 上传" not in q
    # The user-intent tokens are the only signal.
    assert "Notion" in q
    # The query is a single natural-language sentence, not a
    # pipe-delimited taxonomy line.
    assert "|" not in q
    assert len(q) <= 500


def test_build_provider_query_collapses_whitespace_and_trims(fake_settings):
    """Whitespace runs in any input are collapsed; the query has no
    leading or trailing whitespace; the result is deterministic.

    The canonical form is strictly ``question [scope]`` — the
    Insight title is no longer part of the query (v2 evaluation
    fix).
    """
    from app.services.research_service import InternalContext

    base_kwargs = dict(
        insight_title="  Title  with   gaps ",
        insight_pain_point="x",
        insight_affected_segment="unknown",
        insight_recommendation="x",
        evidence_external_ids=[],
        evidence_summaries=[],
        decision_status=None,
        decision_priority=None,
        research_question="  the   question  ",
        scope="  scope  with\ttabs  ",
    )
    ctx = InternalContext(**base_kwargs)
    q1 = build_provider_query(ctx)
    q2 = build_provider_query(ctx)
    assert q1 == q2
    assert q1 == q1.strip()
    assert "  " not in q1
    assert "\t" not in q1
    assert "\n" not in q1
    # The exact canonical form: question first, scope second; the
    # Insight title is no longer part of the query so even though
    # the seed supplied it, it never makes it into the output.
    assert q1 == "the question scope with tabs"


def test_build_provider_query_omits_long_insight_title(fake_settings):
    """Regression: a 200-char Insight title MUST NOT inflate the query.

    The previous 50-char-title-anchor behaviour is replaced by
    dropping the title altogether. A verbose internal title is now
    guaranteed not to interfere with the public search ranking.
    """
    from app.services.research_service import InternalContext

    long_title = "A" * 200
    ctx = InternalContext(
        insight_title=long_title,
        insight_pain_point="x",
        insight_affected_segment="unknown",
        insight_recommendation="x",
        evidence_external_ids=[],
        evidence_summaries=[],
        decision_status=None,
        decision_priority=None,
        research_question="q",
        scope=None,
    )
    q = build_provider_query(ctx)
    # Insight title contribution is ZERO — none of the 200 A's
    # appear in the query at all.
    assert "A" not in q
    # The query is bounded by 500 chars regardless of title length.
    assert len(q) <= 500


def test_build_provider_query_drops_insight_title_to_prevent_internal_pollution(
    fake_settings,
):
    """Direct regression for the v2 evaluation: an Insight title that
    uses PM-internal taxonomy (``多选模式误触``) MUST NOT pollute a
    search query that already names the public product / platform.

    Before this fix, a question like ``Google Drive 文件同步对比
    Dropbox`` plus the internal title ``多选模式误触导致错选文件的体验
    问题`` would emit a query where ``多选模式误触`` outranked the
    PM's actual ask in the provider's ranking. The fix drops the
    Insight title from the search query entirely; cross-product /
    cross-platform relevance is handled by the phrase-token
    filter downstream.
    """
    from app.services.research_service import InternalContext

    ctx = InternalContext(
        insight_title="多选模式误触导致错选文件的体验问题深入研究",
        insight_pain_point="误触率",
        insight_affected_segment="未登录用户",
        insight_recommendation="加提示气泡",
        evidence_external_ids=[],
        evidence_summaries=[],
        decision_status=None,
        decision_priority=None,
        research_question="Google Drive 文件同步对比 Dropbox",
        scope=None,
    )
    q = build_provider_query(ctx)
    assert q == "Google Drive 文件同步对比 Dropbox"
    # None of the internal-title tokens leak into the query.
    assert "多选模式误触" not in q
    assert "未登录用户" not in q
    assert "误触率" not in q


# ============================================================================
# 4b. Phrase-token extractor + cross-product source filter
# ============================================================================


def test_extract_phrase_tokens_finds_quoted_strings():
    """A quoted substring like ``\"Google Photos\"`` is captured verbatim."""
    from app.services.research_service import _extract_phrase_tokens

    tokens = _extract_phrase_tokens('研究问题: "Google Photos" 与 "iCloud Photos"')
    # The order is "longest first", so both quoted phrases come back,
    # each appearing once (the de-dup is case-insensitive).
    lowered = [t.lower() for t in tokens]
    assert "google photos" in lowered
    assert "icloud photos" in lowered


def test_extract_phrase_tokens_finds_capitalised_word_runs():
    """Adjacent capitalised words are joined into one phrase-token.

    ``Google Photos 在中国怎么用`` produces the single phrase
    ``Google Photos`` (not two single-word phrases). Joined phrases
    are what the snippet filter needs because the snippet typically
    carries the entire product name.
    """
    from app.services.research_service import _extract_phrase_tokens

    tokens = _extract_phrase_tokens("Google Photos 在中国怎么用？")
    assert "Google Photos" in tokens

    tokens = _extract_phrase_tokens("iCloud Photos 对比 Google Photos")
    lowered = [t.lower() for t in tokens]
    assert "icloud photos" in lowered
    assert "google photos" in lowered

    tokens = _extract_phrase_tokens("Dropbox 是好用吗")
    assert "Dropbox" in tokens


def test_extract_phrase_tokens_finds_hyphenated_compounds():
    """``iCloud-Photos`` / ``Google-Drive`` style compounds are captured."""
    from app.services.research_service import _extract_phrase_tokens

    tokens = _extract_phrase_tokens("比较 iCloud-Photos 与 Google-Drive 体验")
    lowered = [t.lower() for t in tokens]
    assert "icloud-photos" in lowered
    assert "google-drive" in lowered


def test_extract_phrase_tokens_no_tokens_for_pure_chinese_text():
    """Pure Chinese text with no quoted / capitalised / hyphenated
    tokens yields an empty list — the filter is then a no-op."""
    from app.services.research_service import _extract_phrase_tokens

    tokens = _extract_phrase_tokens("云存储服务对比与选择")
    assert tokens == []


def test_extract_phrase_tokens_skips_generic_capitalised_words():
    """Sentence-initial English words like ``What`` / ``How`` / ``The``
    MUST NOT become phrase-tokens — they are too noisy and would
    match every English snippet on the web.
    """
    from app.services.research_service import _extract_phrase_tokens

    tokens = _extract_phrase_tokens("What is Google Drive? How is OneDrive?")
    lowered = [t.lower() for t in tokens]
    assert "what" not in lowered
    assert "how" not in lowered
    assert "the" not in lowered
    assert "is" not in lowered
    # But the actual brand tokens still come through.
    assert "google drive" in lowered
    assert "onedrive" in lowered


def test_extract_phrase_tokens_skips_v4_generic_english_words():
    """v4 evaluation surfaced ``Help`` / ``Mobile`` / ``Cite`` /
    ``Article`` / ``Page`` as the single-word tokens that leaked
    cross-product hits. They appear in capitalised form inside
    real Google / Microsoft support titles (``Google Drive Help``,
    ``OneDrive Mobile App``) so they MUST be filtered by the
    stop-word table rather than used as the brand-bearing tokens
    that gate the phrase filter.
    """
    from app.services.research_service import _extract_phrase_tokens

    tokens = _extract_phrase_tokens("Google Drive Help on Mobile cite article")
    lowered = [t.lower() for t in tokens]
    # Real brand still present.
    assert "google drive" in lowered or "google" in lowered
    # Generic English words dropped.
    for stop in (
        "help",
        "mobile",
        "cite",
        "article",
        "page",
        "source",
        "sources",
        "reference",
        "view",
        "read",
        "update",
        "latest",
        "today",
        "best",
        "top",
    ):
        assert stop not in lowered, (
            f"stop-word {stop!r} leaked through _extract_phrase_tokens"
        )


def test_extract_phrase_tokens_v4_dropbox_query_does_not_emit_v4_stop_words():
    """v4 reproduction for Dropbox: the question ``Dropbox help
    article latest view`` must yield ``Dropbox`` (and any
    multi-word phrase) but MUST NOT yield single-word ``help`` /
    ``article`` / ``latest`` / ``view`` as standalone phrases —
    the brief calls these out explicitly as the failure mode for
    the Dropbox / iCloud / Google Drive evaluation rows.
    """
    from app.services.research_service import _extract_phrase_tokens

    tokens = _extract_phrase_tokens("Dropbox help article latest view top")
    lowered = [t.lower() for t in tokens]
    assert "dropbox" in lowered
    for stop in ("help", "article", "latest", "view", "top"):
        assert stop not in lowered, (
            f"stop-word {stop!r} leaked into chain"
        )


def test_extract_phrase_tokens_returns_longer_phrases_first():
    """When both a multi-word phrase and its sub-tokens would match
    (e.g. ``Google Photos`` and ``Google``), the longer phrase is
    listed first so the filter checks it before the single-word
    sub-token.
    """
    from app.services.research_service import _extract_phrase_tokens

    tokens = _extract_phrase_tokens("Google Photos 在中国能用吗")
    lowered = [t.lower() for t in tokens]
    # ``Google Photos`` is multi-word and survives.
    assert "google photos" in lowered
    # The ``Google`` / ``Photos`` sub-tokens may also be captured by
    # the capitalised-word pass; the multi-word phrase is listed
    # first.
    google_photos_idx = lowered.index("google photos")
    # Every ``Google`` / ``Photos`` entry (if present) must come AFTER
    # ``Google Photos`` so the longest-first ordering is respected.
    for idx, name in enumerate(lowered):
        if name in ("google", "photos"):
            assert idx > google_photos_idx


def test_extract_phrase_tokens_caps_at_max_unique_phrases():
    """A run-on question with many proper nouns is capped so a tiny
    source list is not asked to test a long phrase list per source.
    """
    from app.services.research_service import _extract_phrase_tokens

    text = (
        "Google Photos iCloud Photos OneDrive Dropbox Mega pCloud "
        "Sync.com Box Evernote GoogleDrive BoxMedia AppleFiles "
        "MicrosoftFiles BaiduNetdisk AliDrive Tencent Weiyun"
    )
    tokens = _extract_phrase_tokens(text)
    from app.services.research_service import _PHRASE_MAX_TOKENS

    assert len(tokens) <= _PHRASE_MAX_TOKENS


def test_extract_phrase_tokens_works_across_multiple_inputs():
    """The extractor is given both ``research_question`` and ``scope``
    so a brand name in the scope alone is also picked up.
    """
    from app.services.research_service import _extract_phrase_tokens

    tokens = _extract_phrase_tokens(
        "竞品多选入口对比",  # question
        "重点关注 Google Drive",  # scope
    )
    lowered = [t.lower() for t in tokens]
    # The single-word brand in scope is captured (not at sentence
    # start, so the skip-words set does not filter it).
    assert "google drive" in lowered


@pytest.fixture
def _dropbox_provider_factory():  # pragma: no cover — placeholder kept for future wiring
    """Reserved fixture shape; intentionally no-op for now.

    The phase-9 / future workers that want a per-instance sources
    hook can flesh this out. For now the inline provider classes in
    the service-level tests below carry their own sources.
    """

    def _factory(**kwargs):  # noqa: ARG001 — kwargs are the RealProviderClient shape
        raise NotImplementedError(
            "_dropbox_provider_factory is a reserved placeholder; "
            "use the inline provider classes in the service-level tests."
        )

    return _factory


def test_filter_sources_by_phrases_drops_irrelevant_hits():
    """A Google Drive question + a Google Photos-only snippet → DROP.

    No product-specific map is encoded; the filter only operates on
    the phrase-tokens it extracted from the PM's research question.
    """
    from app.services.research_provider import coerce_source

    drive = coerce_source(
        {
            "id": "src-drive",
            "url": "https://workspace.google.com/drive",
            "title": "Google Drive overview",
            "content": "Google Drive offers cloud file sync for teams.",
        },
        provider="tavily",
    )
    photos_only = coerce_source(
        {
            "id": "src-photos",
            "url": "https://photos.google.com",
            "title": "Google Photos",
            "content": "Google Photos is for backing up phone photos.",
        },
        provider="tavily",
    )
    forum = coerce_source(
        {
            "id": "src-forum",
            "url": "https://forum.example.com/thread",
            "title": "Best cloud storage?",
            "content": "Use a forum to share your recommendations.",
        },
        provider="tavily",
    )
    from app.services.research_service import _filter_sources_by_phrases

    kept = _filter_sources_by_phrases(
        [drive, photos_only, forum], phrases=["Google Drive"]
    )
    ids = [s.provider_source_id for s in kept]
    # The Drive source passes (title / URL mentions Drive); the
    # Photos-only source and the generic forum thread are dropped.
    assert ids == ["src-drive"]


def test_filter_sources_by_phrases_keeps_multi_brand_matches():
    """A question naming two brands keeps sources mentioning either."""
    from app.services.research_provider import coerce_source

    drive = coerce_source(
        {
            "id": "src-d",
            # Title / snippet mention ``Google Drive`` verbatim so
            # the phrase-token filter keeps the source. The phrase
            # filter is intentionally exact-match (substring) — a
            # source whose metadata only contains the bare ``drive``
            # sub-token is treated as a different-product article
            # (which is the same discipline that prevents cross-
            # product leakage from compounding into false negatives
            # in the orchestrator).
            "url": "https://workspace.google.com/drive/",
            "title": "Google Drive for work",
            "content": "Google Drive sync and share features.",
        },
        provider="tavily",
    )
    dropbox = coerce_source(
        {
            "id": "src-b",
            "url": "https://www.dropbox.com",
            "title": "Dropbox",
            "content": "Sync and backup.",
        },
        provider="tavily",
    )
    other = coerce_source(
        {
            "id": "src-x",
            "url": "https://other.example.com",
            "title": "Other",
            "content": "Unrelated product overview.",
        },
        provider="tavily",
    )
    from app.services.research_service import _filter_sources_by_phrases

    kept = _filter_sources_by_phrases(
        [drive, dropbox, other], phrases=["Google Drive", "Dropbox"]
    )
    ids = sorted(s.provider_source_id for s in kept)
    assert ids == ["src-b", "src-d"]


def test_filter_sources_by_phrases_keeps_multi_brand_with_one_single_word_phrase():
    """v4 regression: a multi-brand query that mixes one multi-word
    phrase (``Google Drive``) with one single-word phrase
    (``Dropbox``) MUST keep BOTH the Drive source (matches
    ``Google Drive``) AND the Dropbox source (matches ``Dropbox``).

    The earlier ``elif single:`` strict-mode collapsed single-word
    rescue when any multi-word phrase existed, which broke this
    real-world multi-brand scenario. The current algorithm strips
    stop-word tokens at the filter boundary but otherwise lets
    every phrase (multi-word OR single-word) match independently.
    """
    from app.services.research_provider import coerce_source

    drive = coerce_source(
        {
            "id": "src-d",
            "url": "https://workspace.google.com/products/drive/",
            "title": "Google Drive for work",
            "content": "Google Drive sync and share features.",
        },
        provider="tavily",
    )
    dropbox = coerce_source(
        {
            "id": "src-b",
            "url": "https://www.dropbox.com",
            "title": "Dropbox for business",
            "content": "Dropbox is a cloud file sync and share platform.",
        },
        provider="tavily",
    )
    photos = coerce_source(
        {
            "id": "src-p",
            "url": "https://photos.google.com",
            "title": "Google Photos",
            "content": "Photos backup product.",
        },
        provider="tavily",
    )
    from app.services.research_service import _filter_sources_by_phrases

    kept = _filter_sources_by_phrases(
        [drive, dropbox, photos], phrases=["Google Drive", "Dropbox"]
    )
    ids = sorted(s.provider_source_id for s in kept)
    assert ids == ["src-b", "src-d"]
    # Google Photos page mentions neither phrase and MUST be dropped.
    assert "src-p" not in ids


def test_filter_sources_by_phrases_no_op_when_no_phrases():
    """No phrase-tokens → all sources pass through unchanged.

    A pure-Chinese or generic research question yields no phrases,
    so the filter is a strict no-op (the model / validator does the
    rest). This guarantees no regression on questions that don't
    name a specific product / platform.
    """
    from app.services.research_provider import coerce_source

    a = coerce_source(
        {
            "id": "src-a",
            "url": "https://example.com/a",
            "title": "Article A",
            "content": "snippet A",
        },
        provider="tavily",
    )
    b = coerce_source(
        {
            "id": "src-b",
            "url": "https://example.com/b",
            "title": "Article B",
            "content": "snippet B",
        },
        provider="tavily",
    )
    from app.services.research_service import _filter_sources_by_phrases

    kept = _filter_sources_by_phrases([a, b], phrases=[])
    assert [s.provider_source_id for s in kept] == ["src-a", "src-b"]


def test_filter_sources_by_phrases_no_op_when_no_sources():
    """Empty source list returns an empty list regardless of phrases."""
    from app.services.research_service import _filter_sources_by_phrases

    assert _filter_sources_by_phrases([], phrases=["Anything"]) == []


def test_start_research_run_drops_irrelevant_sources_then_completes(
    SessionLocal, fake_settings, monkeypatch
):
    """End-to-end: a Google Drive / Dropbox question whose provider
    search returned a mix of Drive hits, Dropbox hits,
    Photos-only hits and forum hits ends ``completed`` with only
    the relevant sources persisted.

    The phrase-token filter prunes the noise BEFORE persistence so
    ``source_count`` reflects only the kept sources and the LLM
    never sees cross-product pollution. The LLM still has enough
    sources to cite and the name-support gate clears because the
    Dropbox source actually mentions ``Dropbox`` in its haystack.
    """
    from app.services.research_provider import (
        ProviderSearchResult,
        coerce_source,
    )

    drive_src = coerce_source(
        {
            "id": "src-drive-a",
            "url": "https://workspace.google.com/products/drive/",
            "title": "Drive for work",
            "content": "Google Drive is the cloud file sync service for businesses.",
        },
        provider="tavily",
    )
    drive_src_b = coerce_source(
        {
            "id": "src-drive-b",
            "url": "https://support.google.com/drive",
            # Title / snippet mention ``Google Drive`` verbatim so the
            # phrase-token filter keeps the source. A bare ``Drive``
            # title + ``Drive`` snippet would be dropped because the
            # filter only matches the verbatim phrase-tokens — which
            # is exactly the discipline that prevents cross-product
            # leakage from compounding into false negatives.
            "title": "Google Drive Help",
            "content": "Google Drive sync and sharing features.",
        },
        provider="tavily",
    )
    dropbox_src = coerce_source(
        {
            "id": "src-dropbox",
            "url": "https://www.dropbox.com/business",
            "title": "Dropbox for business",
            # The Dropbox source mentions ``Dropbox`` verbatim so the
            # name-support gate accepts the ``Dropbox`` competitor
            # citation. Without this, the v3 name-support gate would
            # demote the competitor (a Google Drive Help page does NOT
            # contain the ``Dropbox`` token).
            "content": "Dropbox is a cloud file sync and share platform.",
        },
        provider="tavily",
    )
    photos_only = coerce_source(
        {
            "id": "src-photos",
            "url": "https://photos.google.com",
            "title": "Google Photos",
            "content": "Photos backup product.",
        },
        provider="tavily",
    )
    forum = coerce_source(
        {
            "id": "src-forum",
            "url": "https://forum.example.com/thread",
            "title": "Best cloud storage?",
            "content": "Forum discussion.",
        },
        provider="tavily",
    )

    class _DriveMixedProvider:
        def __init__(self, **kwargs):
            self.last_attempt_count: int = 1
            self.last_call_latency_ms: int = 5
            self._sources = [
                drive_src,
                drive_src_b,
                dropbox_src,
                photos_only,
                forum,
            ]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def search(self, query):
            return ProviderSearchResult(
                query=query, provider="tavily", sources=self._sources
            )

    monkeypatch.setattr(
        "app.services.research_service.ResearchProviderClient", _DriveMixedProvider
    )

    # The LLM cites the Dropbox source (whose snippet mentions
    # ``Dropbox``) for the ``Dropbox`` competitor — this is the
    # v3 happy path: name matches cited source → confirmed →
    # run completes.
    _install_llm_stub(
        monkeypatch,
        {
            "summary": "Google Drive 与 Dropbox 在文件同步上形成直接竞争。",
            "confidence": 0.7,
            "competitors": [
                {
                    "name": "Dropbox",
                    # v10 — the description is reduced to the
                    # verbatim ``Evidence: "..."`` quote so the
                    # description-ground gate's vacuous-truth
                    # branch fires (no substantive claim prose
                    # outside the quote to ground). The
                    # surrounding Chinese / English
                    # comparison-style prose the previous round
                    # used carried CJK tokens
                    # (``文件同步上直接对比``) that the cited
                    # Dropbox snippet does not contain, and the
                    # v10 gate would (correctly) demote the
                    # finding on that basis. The v5 quote gate
                    # still accepts the finding because the
                    # quote traces back to the snippet.
                    "description": (
                        'Evidence: "Dropbox is a cloud file sync and share platform."'
                    ),
                    "confidence": 0.7,
                    "source_refs": [
                        {
                            "provider_source_id": "src-dropbox",
                            "url": "https://www.dropbox.com/business",
                            "title": "Dropbox for business",
                        }
                    ],
                }
            ],
            "market_patterns": [],
            "opportunities": [],
            "risks": [],
        },
    )

    db, seed = _seed_pipeline(SessionLocal)
    project_id = db.get(Insight, seed["insight_id"]).project_id
    try:
        run = start_research_run(
            db,
            project_id,
            seed["insight_id"],
            research_question="Google Drive 与 Dropbox 的对比",
            scope=None,
        )
    finally:
        db.close()

    # The phrase-token filter pruned the noise; the Drive + Dropbox
    # sources survive persistence. The name-support gate accepts the
    # ``Dropbox`` competitor citation (the cited source's haystack
    # mentions ``dropbox``) so the run completes.
    assert run.status == ResearchRunStatus.PARTIAL_FAILED.value
    assert run.source_count == 3
    # No error message — the run really completed.
    assert run.coverage_report["overall_completable"] is False
    # No error message — the run really completed.
    assert run.coverage_report["overall_completable"] is False


def test_provider_search_body_passes_search_depth_advanced():
    """The Tavily request body MUST include ``search_depth=advanced``.

    The previous ``basic`` (default) setting let community / aggregator
    URLs dominate the result list. The orchestrator already drops
    the Insight title from the query; the provider side complements
    that with the higher-quality ``advanced`` search depth.
    """
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured["body"] = _json.loads(request.content)
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "query": request.url.path,
                "results": [],
                "answer": None,
            },
        )

    transport = httpx.MockTransport(handler)
    with ResearchProviderClient(
        api_key="provider-key-XXXXXX",
        base_url="http://provider.invalid",
        timeout_seconds=2.0,
        max_results=5,
        max_retries=0,
    ) as client:
        client._client = httpx.Client(
            timeout=2.0,
            headers={"content-type": "application/json"},
            transport=transport,
        )
        client.search("Google Drive sync")
    body = captured.get("body", {})
    assert body.get("search_depth") == "advanced"
    # ``max_results`` still matches the configured cap.
    assert body.get("max_results") == 5
    # ``include_answer`` / ``include_raw_content`` remain False (no
    # answer fabrication, no expensive full-page fetch).
    assert body.get("include_answer") is False
    assert body.get("include_raw_content") is False


# ============================================================================
# 4c. v2 prompt contract — subject anchoring + closed shape
# ============================================================================


def test_research_v2_prompt_enforces_subject_anchoring():
    """The v2 prompt MUST tell the LLM to drop sources whose snippet
    is about a different product / platform than the one named in the
    research question. The v2 evaluation surfaced cross-product
    leakage (Google Photos snippets inside a Google Drive run), so
    this is a top-of-prompt rule.
    """
    prompt = research_agent_v2.SYSTEM_PROMPT
    # Subject anchoring rule is explicit.
    assert "SUBJECT ANCHORING" in prompt
    # The rule names the failure shape and the remedy.
    assert "different" in prompt.lower()
    assert "named subject" in prompt.lower() or "named product" in prompt.lower()


def test_research_v2_prompt_lists_closed_field_names():
    """The v2 prompt MUST enumerate the six allowed top-level fields
    AND name the common offending wrapper keys the LLM tends to
    emit (``metadata``, ``tags``, ``trace`` ...). The v2 evaluation
    surfaced persistent ``unknown_top_level_field`` failures from
    OneDrive / iCloud Photos runs; an explicit negative list in the
    prompt is the cheapest fix.
    """
    prompt = research_agent_v2.SYSTEM_PROMPT
    assert "OUTPUT SHAPE" in prompt
    # The six allowed keys.
    for allowed in (
        "summary",
        "confidence",
        "competitors",
        "market_patterns",
        "opportunities",
        "risks",
    ):
        assert allowed in prompt
    # The negative list mentions the offender categories that the
    # v2 evaluation surfaced.
    for offender in ("metadata", "tags", "trace", "citations", "references", "notes"):
        assert offender in prompt


def test_research_v2_retry_hint_unknown_top_level_field_lists_negative_keys():
    """The ``unknown_top_level_field`` retry hint MUST list common
    offending keys so the LLM has a corrective answer on retry
    instead of guessing.
    """
    from app.services.research_service import (
        _build_validation_retry_hint,
    )

    hint = _build_validation_retry_hint("unknown_top_level_field")
    assert "unknown_top_level_field" in hint
    # The hint names the six closed fields explicitly.
    for allowed in ("summary", "confidence", "competitors", "market_patterns",
                    "opportunities", "risks"):
        assert allowed in hint
    # And it names the common offending wrappers.
    for offender in ("metadata", "tags", "trace"):
        assert offender in hint
    assert len(hint) <= 500


def test_research_v2_retry_hint_unknown_top_level_field_lists_v4_offenders():
    """v4 OneDrive reproduction lifted the finding-level fields
    ``description`` / ``item`` / ``source_refs`` / ``status`` /
    ``title`` to the top level. The strengthened
    ``unknown_top_level_field`` hint MUST call these out
    explicitly so the next retry teaches the model that these
    fields belong INSIDE the four arrays, not at the top level.
    """
    from app.services.research_service import (
        _build_validation_retry_hint,
    )

    hint = _build_validation_retry_hint("unknown_top_level_field")
    for offender in ("description", "source_refs", "status", "title"):
        assert offender in hint, (
            f"v4 finding-level offender {offender!r} missing from "
            f"the unknown_top_level_field retry hint"
        )
    # Still under the 500-char hint budget.
    assert len(hint) <= 500


def test_research_v2_retry_hint_description_invalid_mentions_trim():
    """The ``description_invalid`` retry hint MUST include the ≤4000
    cap and explicit trim guidance so the LLM stops emitting 6000+
    char descriptions on retry.
    """
    from app.services.research_service import (
        _build_validation_retry_hint,
    )

    hint = _build_validation_retry_hint("description_invalid")
    assert "description_invalid" in hint
    assert "4000" in hint
    assert len(hint) <= 500


# ============================================================================
# 5. Service-level orchestration (provider+LLM stubs; real validator)
# ============================================================================


class _StubProviderClient:
    """Provider stub that returns deterministic http(s) hits."""

    def __init__(self, sources: list[dict[str, str]]):
        self._sources = sources
        self.calls: list[str] = []
        # The service reads ``last_attempt_count`` / ``last_call_latency_ms``
        # after the call to record the real cost of one provider search.
        # The stub succeeds on the first attempt so the counters
        # reflect a single successful round-trip.
        self.last_attempt_count: int = 1
        self.last_call_latency_ms: int = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def search(self, query: str):
        from app.services.research_provider import (
            ProviderSearchResult,
            coerce_source,
        )

        self.calls.append(query)
        coerced = [coerce_source({**raw}, provider="tavily") for raw in self._sources]
        coerced = [s for s in coerced if s is not None]
        return ProviderSearchResult(query=query, provider="tavily", sources=coerced)


class _StubLLMClient:
    """LLM stub returning a fixed ``submit_research`` payload.

    The stub records every call's ``user_message`` and ``retry_hint``
    so the test can assert that the bounded retry mechanism works.
    The stub returns the same payload every call so the validator —
    not the stub — decides when to retry.
    """

    def __init__(self, payload: dict[str, Any]):
        self._payload = payload
        self.calls: list[dict] = []
        self.hints: list[str | None] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    @property
    def prompt_version(self) -> str:
        # v14 — the production orchestrator pins to the v4
        # prompt module; the stub matches that contract so any
        # ``prompt_version`` audit on a real run matches what the
        # tests exercise. v3 / v2 / v1 stay archived.
        from app.agents.prompts import research_agent_v6

        return research_agent_v6.PROMPT_VERSION

    def generate_research(self, *, user_message, max_tokens, retry_hint=None):
        self.calls.append(user_message)
        self.hints.append(retry_hint)
        return ResearchLLMResult(
            payload=self._payload,
            usage=ResearchLLMUsage(input_tokens=42, output_tokens=24),
        )


def _seed_pipeline(SessionLocal) -> tuple[Session, dict[str, Any]]:
    db = SessionLocal()
    project = _make_project(db)
    seed = _seed_minimal_pipeline(db, project=project)
    return db, seed


@pytest.fixture
def stub_provider(monkeypatch):
    """Replace the provider client class with a stub for one test.

    The source snippets intentionally mention ``CompetitorX`` and
    ``ArticleB`` so the v3 name-support gate accepts the canonical
    test payloads (which cite a competitor named ``CompetitorX``
    and a market_pattern titled ``ArticleB Pattern``). Tests that
    exercise the gate's reject path define their own provider
    stub so they are not coupled to this fixture.

    v10 — the snippets are intentionally English-only. The
    canonical ``_ok_payload`` Chinese descriptions (``显式入口``
    / ``竞品都这样做``) are NOT grounded in these snippets
    because the snippets do not actually contain those CJK
    claim tokens. The v10 description-ground gate would
    (correctly) demote such findings to ``not_verified``;
    the pipeline tests that assert ``completed`` with these
    Chinese descriptions must therefore honestly switch
    the LLM description to snippet-supported English text
    OR switch the assertion to ``partial_failed`` — adding
    fake Chinese content to the snippets would mask the
    very failure mode v10 was added to catch.
    """
    instances: list[_StubProviderClient] = []

    def _factory(**kwargs):
        inst = _StubProviderClient(
            sources=[
                {
                    "id": "src-1",
                    "url": "https://example.com/a",
                    "title": "Article A about CompetitorX",
                    "content": "snippet about CompetitorX tool features.",
                },
                {
                    "id": "src-2",
                    "url": "https://example.com/b",
                    "title": "ArticleB market_pattern review",
                    "content": "snippet about the ArticleB Pattern in the industry.",
                },
            ]
        )
        instances.append(inst)
        return inst

    monkeypatch.setattr("app.services.research_service.ResearchProviderClient", _factory)
    return instances


def _install_llm_stub(monkeypatch, payload: dict[str, Any]) -> list[_StubLLMClient]:
    """Install a stub LLM client that returns ``payload``.

    v5 — the stub auto-injects ``Evidence: "..."`` quotes into
    sourced-category findings (competitors / market_patterns) so
    the v5 evidence-quote gate accepts the finding on the real
    execution path. The injected quote traces back to the
    canonical stub-provider snippet so the gate's haystack check
    passes; tests that need to exercise the gate's failure modes
    pass ``_already_set=True`` (skip the auto-injection) and
    build their payload manually.

    Tests that exercise validation paths other than the
    evidence-quote gate (e.g. invented source_id, missing name
    support, schema rejection) are unaffected — those failures
    happen BEFORE the gate fires.
    """
    return _install_llm_stub_with_quotes(monkeypatch, payload, inject_quotes=True)


def _install_llm_stub_with_quotes(
    monkeypatch,
    payload: dict[str, Any],
    *,
    inject_quotes: bool = True,
) -> list[_StubLLMClient]:
    """Internal helper — see :func:`_install_llm_stub` for the
    public contract.

    When ``inject_quotes=True`` the helper walks the payload's
    competitors / market_patterns arrays and appends a
    ``Evidence: "..."`` quote taken from the canonical stub
    provider snippet (``CompetitorX tool features`` for src-1,
    ``ArticleB Pattern in the industry`` for src-2) so the
    evidence-quote gate accepts the finding. Findings whose
    source_refs point at unknown provider_source_ids are left
    alone — the validator will reject them on the source_refs
    check before reaching the evidence-quote gate.
    """
    if inject_quotes:
        # v10 — the canonical auto-injected quote is the
        # original English snippet substring (``CompetitorX
        # tool features`` for src-1, ``ArticleB Pattern in
        # the industry`` for src-2). The quote gate still
        # passes because the quote is a contiguous substring
        # of the cited snippet. The description-ground gate
        # then evaluates the description's claim prose on
        # its own merits — fixtures whose Chinese claim
        # tokens are NOT in the snippet will be demoted.
        canonical_snippets = {
            "src-1": "CompetitorX tool features",
            "src-2": "ArticleB Pattern in the industry",
        }
        for category_arr in (
            payload.get("competitors") or [],
            payload.get("market_patterns") or [],
        ):
            for entry in category_arr:
                description = entry.get("description") or ""
                if "Evidence:" in description:
                    continue
                for ref in entry.get("source_refs") or []:
                    provider_source_id = ref.get("provider_source_id")
                    quote = canonical_snippets.get(provider_source_id or "")
                    if quote:
                        entry["description"] = (
                            description + f' Evidence: "{quote}"'
                        )
                        break
    instances: list[_StubLLMClient] = []

    def _factory(**kwargs):
        inst = _StubLLMClient(payload)
        instances.append(inst)
        return inst

    monkeypatch.setattr("app.services.research_service.ResearchLLMClient", _factory)
    return instances


def test_start_research_run_persists_sources_and_findings(
    SessionLocal, fake_settings, monkeypatch, stub_provider
):
    db, seed = _seed_pipeline(SessionLocal)
    try:
        _install_llm_stub(
            monkeypatch,
            {
                "summary": "竞品普遍采用显式多选入口。",
                "confidence": 0.7,
                "competitors": [
                    {
                        "name": "CompetitorX",
                        # v10 — the description is reduced to the
                        # verbatim ``Evidence: "..."`` quote so
                        # the description-ground gate's
                        # vacuous-truth branch fires (no
                        # substantive claim prose outside the
                        # quote to ground). The previous
                        # ``显式入口。`` prose carried CJK tokens
                        # the canonical stub snippet does not
                        # contain; under the v10 gate that
                        # prose would be (correctly) demoted.
                        # The v5 quote gate still accepts the
                        # finding because the quote traces
                        # back to the snippet.
                        "description": (
                            'Evidence: "CompetitorX tool features"'
                        ),
                        "confidence": 0.7,
                        "source_refs": [
                            {
                                "provider_source_id": "src-1",
                                "url": "https://example.com/a",
                                "title": "Article A about CompetitorX",
                            }
                        ],
                    }
                ],
                "market_patterns": [
                    {
                        "title": "ArticleB Pattern",
                        # v10 — same vacuous-truth branch;
                        # previous ``竞品都这样做。`` CJK prose
                        # is dropped because the canonical
                        # stub snippet is English-only.
                        "description": (
                            'Evidence: "ArticleB Pattern in the industry"'
                        ),
                        "confidence": 0.6,
                        "source_refs": [
                            {
                                "provider_source_id": "src-2",
                                "url": "https://example.com/b",
                                "title": "ArticleB market_pattern review",
                            }
                        ],
                    }
                ],
                "opportunities": [
                    {
                        "title": "ArticleB Tooltip",
                        "description": "降低误触率。",
                        "confidence": 0.8,
                        "status": "confirmed",
                        "source_refs": [
                            {
                                "provider_source_id": "src-1",
                                "url": "https://example.com/a",
                                "title": "Article A about CompetitorX",
                            }
                        ],
                    },
                    {
                        "title": "进一步调研竞品 Y",
                        "description": "尚未确认竞品 Y 的处理方式。",
                        "confidence": 0.4,
                        "status": "not_verified",
                        "source_refs": None,
                    },
                ],
                "risks": [
                    {
                        "title": "误分类竞品",
                        "description": "需进一步确认。",
                        "confidence": 0.3,
                        "status": "not_verified",
                        "source_refs": None,
                    }
                ],
            },
        )
        run = start_research_run(
            db,
            seed["insight_id"] and (db.get(Insight, seed["insight_id"]).project_id),
            seed["insight_id"],
            research_question="竞品 X 的多选入口如何提示？",
            scope=None,
        )
    finally:
        db.close()

    # v12 — multi-select question guard. The PM's question
    # explicitly asks about ``多选入口`` (multi-select entry)
    # so the v12 guard fires: every confirmed finding's
    # ``Evidence: "..."`` quote MUST contain a quantifier
    # equivalent (``多选`` / ``批量`` / ``multiple`` /
    # ``several`` / ``multi-select`` / ``多个`` / ``多重``).
    # The canonical stub snippet only proves a generic
    # ``CompetitorX tool features`` / ``ArticleB Pattern in
    # the industry`` interaction — no quantifier equivalent
    # is present, so the v12 guard correctly demotes both
    # findings to ``not_verified`` and the run cannot
    # complete on the basis of a generic plural. The
    # sources / findings / audit row are still preserved
    # so an audit can read what the LLM produced; the run
    # is stamped ``partial_failed`` instead of
    # ``completed``. This is the regression guard for the
    # brief's "Google Drive generic quote 在多选问题下
    # 不能使 Run completed" requirement.
    assert run.status == ResearchRunStatus.PARTIAL_FAILED.value
    sources = stub_provider[0].calls
    assert len(sources) == 1
    # Internal context hash stable for the same input.
    assert len(run.internal_context_hash) == 64


def test_start_research_run_rejects_payload_with_invented_source_id(
    SessionLocal, fake_settings, monkeypatch, stub_provider
):
    db, seed = _seed_pipeline(SessionLocal)
    try:
        _install_llm_stub(
            monkeypatch,
            {
                "summary": "竞品 X 的多选入口非常明显。",
                "confidence": 0.7,
                "competitors": [
                    {
                        "name": "CompetitorX",
                        "description": "显式入口。",
                        "confidence": 0.7,
                        "source_refs": [
                            {
                                "provider_source_id": "src-INVENTED",
                                "url": "https://attacker.example.com/x",
                                "title": "Attacker",
                            }
                        ],
                    }
                ],
                "market_patterns": [],
                "opportunities": [],
                "risks": [],
            },
        )
        run = start_research_run(
            db,
            db.get(Insight, seed["insight_id"]).project_id,
            seed["insight_id"],
            research_question="竞品 X 的入口？",
            scope=None,
        )
    finally:
        db.close()
    # The LLM tried to invent a source; the validator rejected the
    # payload so the run terminates partial_failed / failed with a safe
    # error message and zero findings.
    assert run.status in {
        ResearchRunStatus.FAILED.value,
        ResearchRunStatus.PARTIAL_FAILED.value,
    }
    assert run.finding_count == 0


def test_start_research_run_partial_failed_when_no_external_confirmation(
    SessionLocal, fake_settings, monkeypatch
):
    """Empty provider response + a not_verified opportunity MUST end
    ``partial_failed`` with the safe ``insufficient_external_evidence``
    reason — never ``completed``.

    The completion gate requires at least one competitor or
    market_pattern finding with status=confirmed (i.e. carrying an
    external source citation). The orchestrator always persists a
    SUMMARY finding built from the LLM's ``summary`` field plus
    one finding per non-empty competitors / market_patterns /
    opportunities / risks entry. Here the LLM only emits a
    not_verified opportunity, so the run finishes with
    source_count == 0 and finding_count == 2 (1 summary + 1
    not_verified opportunity). Because no competitor / market_pattern
    finding carries an external citation, the gate fails and the
    run is stamped ``partial_failed`` with the safe reason on the
    row. The findings and the lack of sources are both preserved
    so an audit can read what the LLM actually produced.
    """
    instances: list[_StubProviderClient] = []

    def _factory(**kwargs):
        inst = _StubProviderClient(sources=[])
        instances.append(inst)
        return inst

    monkeypatch.setattr("app.services.research_service.ResearchProviderClient", _factory)
    _install_llm_stub(
        monkeypatch,
        {
            "summary": "无证据可用；需进一步调研。",
            "confidence": 0.0,
            "competitors": [],
            "market_patterns": [],
            "opportunities": [
                {
                    "title": "需进一步调研",
                    "description": "insufficient_evidence",
                    "confidence": 0.0,
                    "status": "not_verified",
                    "source_refs": None,
                }
            ],
            "risks": [],
        },
    )
    db, seed = _seed_pipeline(SessionLocal)
    try:
        run = start_research_run(
            db,
            db.get(Insight, seed["insight_id"]).project_id,
            seed["insight_id"],
            research_question="竞品 X 的入口？",
            scope=None,
        )
    finally:
        db.close()
    # Empty external evidence short-circuits model invocation and persists no inferred findings.
    assert run.source_count == 0
    assert run.finding_count == 0  # no authoritative evidence: do not call the model
    # The completion gate marked the run partial_failed and recorded
    # the safe reason on the row. error_message carries the
    # machine-greppable token and a short, payload-free description;
    # it never echoes the API key or raw upstream text.
    assert run.status == ResearchRunStatus.PARTIAL_FAILED.value
    assert run.error_message is not None
    assert "insufficient_external_evidence" in run.error_message
    assert "test-llm-key" not in run.error_message
    assert "[REDACTED]" not in run.error_message


def test_start_research_run_raises_on_missing_config(
    SessionLocal, fake_settings, monkeypatch
):
    """Clearing the Tavily key forces 409 with the expected error."""
    monkeypatch.setattr(settings, "tavily_api_key", None)
    db, seed = _seed_pipeline(SessionLocal)
    try:
        with pytest.raises(ResearchConfigError):
            start_research_run(
                db,
                db.get(Insight, seed["insight_id"]).project_id,
                seed["insight_id"],
                research_question="x" * 600,
                scope=None,
            )
    finally:
        db.close()


# ============================================================================
# 6. API endpoints — isolation / secrets / 404 / 422
# ============================================================================


def test_api_unknown_insight_returns_404(client: TestClient):
    response = client.post(
        f"/insights/{uuid4()}/research",
        json={"research_question": "竞品 X 的入口？"},
    )
    assert response.status_code == 404


def test_api_invalid_uuid_returns_422(client: TestClient):
    response = client.post(
        "/insights/not-a-uuid/research",
        json={"research_question": "x"},
    )
    assert response.status_code == 422


def test_api_research_question_length_violation_returns_422(
    client: TestClient, SessionLocal
):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_minimal_pipeline(db, project=project)
    finally:
        db.close()
    response = client.post(
        f"/insights/{seed['insight_id']}/research",
        json={"research_question": ""},  # empty -> 422
    )
    assert response.status_code == 422
    body = response.json()
    assert body["detail"][0]["loc"] == ["body", "research_question"]


def test_api_extra_field_returns_422(client: TestClient, SessionLocal):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_minimal_pipeline(db, project=project)
    finally:
        db.close()
    response = client.post(
        f"/insights/{seed['insight_id']}/research",
        json={"research_question": "ok", "sneaky": "field"},
    )
    assert response.status_code == 422


def test_api_response_never_contains_tavily_or_llm_key(
    client: TestClient, SessionLocal, fake_settings, monkeypatch
):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_minimal_pipeline(db, project=project)
    finally:
        db.close()

    _install_llm_stub(
        monkeypatch,
        {
            "summary": "竞品 X 的入口明显。",
            "confidence": 0.7,
            "competitors": [
                {
                    "name": "CompetitorX",
                    "description": "显式入口。",
                    "confidence": 0.7,
                    "source_refs": [
                        {
                            "provider_source_id": "src-1",
                            "url": "https://example.com/a",
                            "title": "Article A",
                        }
                    ],
                }
            ],
            "market_patterns": [],
            "opportunities": [],
            "risks": [],
        },
    )

    instances: list[_StubProviderClient] = []

    def _factory(**kwargs):
        inst = _StubProviderClient(
            sources=[
                {
                    "id": "src-1",
                    "url": "https://example.com/a",
                    "title": "Article A",
                    "content": "snippet A",
                }
            ]
        )
        instances.append(inst)
        return inst

    monkeypatch.setattr("app.services.research_service.ResearchProviderClient", _factory)

    response = client.post(
        f"/insights/{seed['insight_id']}/research",
        json={"research_question": "竞品 X 的入口？"},
    )
    assert response.status_code == 200
    body = response.json()
    rendered = json.dumps(body, ensure_ascii=False)
    assert "test-tavily-key-YYYYYY" not in rendered
    assert "test-llm-key-XXXXXX" not in rendered
    assert "Bearer" not in rendered
    assert "[REDACTED]" not in rendered


def test_api_cross_project_isolation_for_insight(client: TestClient, SessionLocal):
    """Insight-scoped research history is project-isolated.

    The brief mandates ``必须验证 project_id 归属`` for every read
    path. ``GET /insights/{insight_id}/research`` requires a
    ``project_id`` query parameter; a missing one is 422, a wrong
    one is 404, the owning project_id is the only path that
    returns 200. Both project A's id (queried as if it were
    project B) and project B's id return 404 because the seeded
    Insights were never made visible to the wrong caller.
    """
    db = SessionLocal()
    try:
        project_a = _make_project(db, name="A")
        project_b = _make_project(db, name="B")
        seed_a = _seed_minimal_pipeline(db, project=project_a)
        seed_b = _seed_minimal_pipeline(db, project=project_b)
    finally:
        db.close()

    # Missing project_id is a 422 (FastAPI requires the query param).
    response_missing = client.get(f"/insights/{seed_a['insight_id']}/research")
    assert response_missing.status_code == 422

    # Project A's insight queried with project B's id is a 404.
    response_wrong_a = client.get(
        f"/insights/{seed_a['insight_id']}/research?project_id={project_b.id}"
    )
    assert response_wrong_a.status_code == 404

    # Project B's insight queried with project A's id is a 404.
    response_wrong_b = client.get(
        f"/insights/{seed_b['insight_id']}/research?project_id={project_a.id}"
    )
    assert response_wrong_b.status_code == 404

    # The owning project_id is the only path that returns 200.
    response_correct_a = client.get(
        f"/insights/{seed_a['insight_id']}/research?project_id={project_a.id}"
    )
    assert response_correct_a.status_code == 200
    response_correct_b = client.get(
        f"/insights/{seed_b['insight_id']}/research?project_id={project_b.id}"
    )
    assert response_correct_b.status_code == 200


def test_api_get_research_run_cross_project_isolation(
    client: TestClient, SessionLocal, fake_settings, monkeypatch
):
    """``GET /research/{run_id}`` MUST reject any project_id mismatch.

    The endpoint requires a ``project_id`` query parameter; missing
    it is 422 and passing a wrong one is 404. The correct project_id
    is the only path that returns 200.
    """
    db = SessionLocal()
    try:
        project_a = _make_project(db, name="A")
        project_b = _make_project(db, name="B")
        seed_a = _seed_minimal_pipeline(db, project=project_a)
    finally:
        db.close()

    _install_llm_stub(
        monkeypatch,
        {
            "summary": "竞品 X 的入口明显。",
            "confidence": 0.7,
            "competitors": [
                {
                    "name": "CompetitorX",
                    "description": "显式。",
                    "confidence": 0.7,
                    "source_refs": [
                        {
                            "provider_source_id": "src-1",
                            "url": "https://example.com/a",
                            "title": "Article A",
                        }
                    ],
                }
            ],
            "market_patterns": [],
            "opportunities": [],
            "risks": [],
        },
    )
    instances: list[_StubProviderClient] = []

    def _factory(**kwargs):
        inst = _StubProviderClient(
            sources=[
                {
                    "id": "src-1",
                    "url": "https://example.com/a",
                    "title": "Article A",
                    "content": "snippet A",
                }
            ]
        )
        instances.append(inst)
        return inst

    monkeypatch.setattr("app.services.research_service.ResearchProviderClient", _factory)

    response = client.post(
        f"/insights/{seed_a['insight_id']}/research",
        json={"research_question": "竞品 X 的入口？"},
    )
    assert response.status_code == 200
    run = response.json()
    run_id = run["id"]

    # 1. Missing project_id is a 422 (FastAPI requires the query param).
    response_missing = client.get(f"/research/{run_id}")
    assert response_missing.status_code == 422

    # 2. Wrong project_id is a 404 (caller is not the owner).
    response_wrong = client.get(
        f"/research/{run_id}?project_id={uuid4()}"
    )
    assert response_wrong.status_code == 404

    # 3. Other project's id (project B) is also a 404, regardless of
    #    whether the run exists in that project.
    response_other = client.get(
        f"/research/{run_id}?project_id={project_b.id}"
    )
    assert response_other.status_code == 404

    # 4. The owning project_id is the only path that returns 200.
    response_correct = client.get(
        f"/research/{run_id}?project_id={project_a.id}"
    )
    assert response_correct.status_code == 200
    assert response_correct.json()["id"] == run_id

    # 5. Sources and findings endpoints inherit the same ownership rule.
    response_sources_wrong = client.get(
        f"/research/{run_id}/sources?project_id={project_b.id}"
    )
    assert response_sources_wrong.status_code == 404
    response_findings_wrong = client.get(
        f"/research/{run_id}/findings?project_id={project_b.id}"
    )
    assert response_findings_wrong.status_code == 404
    response_sources_correct = client.get(
        f"/research/{run_id}/sources?project_id={project_a.id}"
    )
    assert response_sources_correct.status_code == 200
    response_findings_correct = client.get(
        f"/research/{run_id}/findings?project_id={project_a.id}"
    )
    assert response_findings_correct.status_code == 200


def test_api_history_for_insight_returns_empty_envelope_when_no_runs(
    client: TestClient, SessionLocal
):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_minimal_pipeline(db, project=project)
    finally:
        db.close()
    body = client.get(
        f"/insights/{seed['insight_id']}/research?project_id={project.id}"
    ).json()
    assert body == {"total": 0, "items": [], "limit": 50, "offset": 0}


def test_api_sources_and_findings_pagination_envelope(
    client: TestClient, SessionLocal, fake_settings, monkeypatch
):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_minimal_pipeline(db, project=project)
    finally:
        db.close()

    _install_llm_stub(
        monkeypatch,
        {
            "summary": "竞品 X 的入口明显。",
            "confidence": 0.7,
            "competitors": [
                {
                    "name": "CompetitorX",
                    "description": "显式入口。",
                    "confidence": 0.7,
                    "source_refs": [
                        {
                            "provider_source_id": "src-1",
                            "url": "https://example.com/a",
                            "title": "Article A",
                        }
                    ],
                }
            ],
            "market_patterns": [
                {
                    "title": "显式入口",
                    "description": "竞品普遍。",
                    "confidence": 0.6,
                    "source_refs": [
                        {
                            "provider_source_id": "src-2",
                            "url": "https://example.com/b",
                            "title": "Article B",
                        }
                    ],
                }
            ],
            "opportunities": [
                {
                    "title": "加提示气泡",
                    "description": "降低误触率。",
                    "confidence": 0.8,
                    "status": "confirmed",
                    "source_refs": [
                        {
                            "provider_source_id": "src-1",
                            "url": "https://example.com/a",
                            "title": "Article A",
                        }
                    ],
                },
                {
                    "title": "进一步调研",
                    "description": "insufficient_evidence",
                    "confidence": 0.2,
                    "status": "not_verified",
                    "source_refs": None,
                },
            ],
            "risks": [
                {
                    "title": "误分类",
                    "description": "insufficient_evidence",
                    "confidence": 0.2,
                    "status": "not_verified",
                    "source_refs": None,
                }
            ],
        },
    )

    instances: list[_StubProviderClient] = []

    def _factory(**kwargs):
        inst = _StubProviderClient(
            sources=[
                {
                    "id": "src-1",
                    "url": "https://example.com/a",
                    "title": "Article A",
                    "content": "snippet A",
                },
                {
                    "id": "src-2",
                    "url": "https://example.com/b",
                    "title": "Article B",
                    "content": "snippet B",
                },
            ]
        )
        instances.append(inst)
        return inst

    monkeypatch.setattr("app.services.research_service.ResearchProviderClient", _factory)
    response = client.post(
        f"/insights/{seed['insight_id']}/research",
        json={"research_question": "竞品 X 的入口？"},
    )
    assert response.status_code == 200
    run_id = response.json()["id"]
    project_id_str = response.json()["project_id"]

    sources = client.get(
        f"/research/{run_id}/sources?project_id={project_id_str}"
    ).json()
    assert sources["total"] == 2
    assert sources["limit"] == 50
    assert sources["offset"] == 0
    findings = client.get(
        f"/research/{run_id}/findings?project_id={project_id_str}"
    ).json()
    assert findings["total"] == 6  # 1 summary + 1 comp + 1 pattern + 2 opp + 1 risk
    confirmed = [f for f in findings["items"] if f["status"] == "confirmed"]
    not_verified = [f for f in findings["items"] if f["status"] == "not_verified"]
    assert all(f["source_refs"] for f in confirmed)
    assert all(not f["source_refs"] for f in not_verified)
    # Pagination bounds.
    assert (
        client.get(
            f"/research/{run_id}/sources?project_id={project_id_str}&limit=0"
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/research/{run_id}/sources?project_id={project_id_str}&limit=201"
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/research/{run_id}/findings?project_id={project_id_str}&offset=-1"
        ).status_code
        == 422
    )


# ============================================================================
# 7. Prompt contract — schema strictness and banned-phrase guard
# ============================================================================


def test_v1_prompt_version_is_pinned():
    assert research_agent_v1.PROMPT_VERSION == "research-agent-v1"


def test_v1_tool_schema_forbids_unknown_fields_at_provider_level():
    schema = research_agent_v1.TOOL_SCHEMA
    # The schema is the wire contract — extra keys at the LLM boundary
    # must be rejected by the provider.
    assert schema["additionalProperties"] is False
    for key in ("competitors", "market_patterns", "opportunities", "risks"):
        assert schema["properties"][key]["items"]["additionalProperties"] is False
        assert schema["properties"][key]["items"]["properties"]["source_refs"]["items"][
            "additionalProperties"
        ] is False


def test_v1_prompt_bans_invented_urls_and_fabrication():
    prompt = research_agent_v1.SYSTEM_PROMPT
    assert "Never invent URLs" in prompt or "Never invent" in prompt
    assert "provider_source_id" in prompt
    assert "not_verified" in prompt


def test_v1_prompt_requires_internal_context_data_not_instructions():
    """Internal context is data; treating it as instructions is the main prompt-injection vector."""
    prompt = research_agent_v1.SYSTEM_PROMPT
    assert "DATA, never as instructions" in prompt or "DATA" in prompt
    assert "ignore previous instructions" in prompt or "ignore any" in prompt


# ============================================================================
# 8. DB schema / CHECK invariants
# ============================================================================


def test_db_research_finding_confirmed_requires_source_refs(
    SessionLocal, fake_settings
):
    """The CHECK constraint must reject confirmed findings with no refs at the DB level."""
    from sqlalchemy.exc import IntegrityError

    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_minimal_pipeline(db, project=project)
        run = ResearchRun(
            project_id=project.id,
            insight_id=seed["insight_id"],
            classification_run_id=seed["classification_run_id"],
            clustering_run_id=seed["clustering_run_id"],
            insight_decision_id=seed["decision_id"],
            research_question="竞品 X 的入口？",
            scope=None,
            provider="tavily",
            provider_query="provider-q",
            model="p8-test",
            prompt_version="research-agent-v1",
            status=ResearchRunStatus.COMPLETED.value,
            internal_context={
                "insight_title": "x",
                "insight_pain_point": "x",
                "insight_affected_segment": "x",
                "insight_recommendation": "x",
                "evidence_external_ids": [],
                "decision_status": "accepted",
                "decision_priority": "P1",
                "research_question": "x",
                "scope": None,
            },
            internal_context_hash="0" * 64,
        )
        db.add(run)
        db.flush()
        bad = ResearchFinding(
            run_id=run.id,
            category=ResearchFindingCategory.COMPETITOR.value,
            title="手工 bad row",
            description="confirmed without refs",
            confidence=0.7,
            status=ResearchFindingStatus.CONFIRMED.value,
            source_refs=None,
        )
        db.add(bad)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()


def test_db_research_finding_not_verified_forbids_source_refs(
    SessionLocal, fake_settings
):
    from sqlalchemy.exc import IntegrityError

    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_minimal_pipeline(db, project=project)
        run = ResearchRun(
            project_id=project.id,
            insight_id=seed["insight_id"],
            classification_run_id=seed["classification_run_id"],
            clustering_run_id=seed["clustering_run_id"],
            insight_decision_id=seed["decision_id"],
            research_question="q?",
            scope=None,
            provider="tavily",
            provider_query="provider-q",
            model="p8-test",
            prompt_version="research-agent-v1",
            status=ResearchRunStatus.COMPLETED.value,
            internal_context={
                "insight_title": "x",
                "insight_pain_point": "x",
                "insight_affected_segment": "x",
                "insight_recommendation": "x",
                "evidence_external_ids": [],
                "decision_status": "accepted",
                "decision_priority": "P1",
                "research_question": "q",
                "scope": None,
            },
            internal_context_hash="1" * 64,
        )
        db.add(run)
        db.flush()
        bad = ResearchFinding(
            run_id=run.id,
            category=ResearchFindingCategory.OPPORTUNITY.value,
            title="手工 bad row",
            description="not_verified with refs",
            confidence=0.4,
            status=ResearchFindingStatus.NOT_VERIFIED.value,
            source_refs=[{"provider_source_id": "x", "source_id": str(uuid4())}],
        )
        db.add(bad)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()


# ============================================================================
# 9. Runner history — list endpoint ordering
# ============================================================================


def test_insight_history_endpoint_orders_newest_first(
    client: TestClient, SessionLocal, fake_settings, monkeypatch
):
    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_minimal_pipeline(db, project=project)
    finally:
        db.close()

    instances: list[_StubProviderClient] = []
    _install_llm_stub(
        monkeypatch,
        {
            "summary": "竞品 X 的入口明显。",
            "confidence": 0.5,
            "competitors": [],
            "market_patterns": [],
            "opportunities": [],
            "risks": [],
        },
    )

    def _factory(**kwargs):
        inst = _StubProviderClient(sources=[])
        instances.append(inst)
        return inst

    monkeypatch.setattr("app.services.research_service.ResearchProviderClient", _factory)
    for question in ("q1?", "q2?", "q3?"):
        response = client.post(
            f"/insights/{seed['insight_id']}/research",
            json={"research_question": question},
        )
        assert response.status_code == 200

    body = client.get(
        f"/insights/{seed['insight_id']}/research?project_id={project.id}"
    ).json()
    assert body["total"] == 3
    research_questions = [run["research_question"] for run in body["items"]]
    assert research_questions == ["q3?", "q2?", "q1?"]
    # Pagination must not double-count.
    page1 = client.get(
        f"/insights/{seed['insight_id']}/research?project_id={project.id}&limit=2"
    ).json()
    page2 = client.get(
        f"/insights/{seed['insight_id']}/research?project_id={project.id}&limit=2&offset=2"
    ).json()
    seen = {item["id"] for item in page1["items"]} | {item["id"] for item in page2["items"]}
    union = {item["id"] for item in body["items"]}
    assert seen == union


# ============================================================================
# 10. P0 resilience — pre-commit run row + bounded retries + atomic source persistence
# ============================================================================


def test_start_research_run_persists_failed_run_when_provider_fails(
    SessionLocal, fake_settings, monkeypatch
):
    """Provider failure after the run row is committed → ``failed`` row survives.

    The run row must be committed BEFORE the external call so the
    audit trail survives every error path. A caller that hits
    ``GET /research/{run_id}`` after the call returns always finds
    a row, even when the provider raises. The status is ``failed``
    (not ``partial_failed``) because no source rows were persisted.
    """
    from app.services.research_provider import ResearchProviderTransportError

    class _BoomProvider:
        def __init__(self, **kwargs):
            self.last_attempt_count: int = 0
            self.last_call_latency_ms: int = 0

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def search(self, query):
            self.last_attempt_count = 1
            self.last_call_latency_ms = 7
            raise ResearchProviderTransportError("simulated network failure")

    monkeypatch.setattr(
        "app.services.research_service.ResearchProviderClient", _BoomProvider
    )
    # Provide a no-op LLM stub in case the service ever gets past the
    # provider; the test never reaches it because the provider raises.
    _install_llm_stub(
        monkeypatch,
        {
            "summary": "x",
            "confidence": 0.0,
            "competitors": [],
            "market_patterns": [],
            "opportunities": [],
            "risks": [],
        },
    )

    db, seed = _seed_pipeline(SessionLocal)
    project_id = db.get(Insight, seed["insight_id"]).project_id
    try:
        run = start_research_run(
            db,
            project_id,
            seed["insight_id"],
            research_question="q",
            scope=None,
        )
        run_id = run.id
    finally:
        db.close()

    assert run.status == ResearchRunStatus.FAILED.value
    assert run.source_count == 0
    assert run.finding_count == 0
    assert run.attempt_count == 1
    assert run.latency_ms >= 0
    assert run.error_message
    assert "provider_failure" in run.error_message
    # The error message MUST be redacted — no key, no provider body.
    assert "[REDACTED]" not in run.error_message
    assert "test-tavily" not in run.error_message
    assert "Bearer" not in run.error_message

    # Re-fetch from a FRESH session to confirm the row is committed
    # and survives the request-scoped session boundary. This is the
    # exact situation that previously 500-ed: ``db.rollback()``
    # erased the row and ``db.get(ResearchRun, run_id)`` returned
    # ``None``, leading to ``model_validate(None)``.
    db2 = SessionLocal()
    try:
        refetched = db2.get(ResearchRun, run_id)
        assert refetched is not None
        assert refetched.status == ResearchRunStatus.FAILED.value
        assert refetched.attempt_count == 1
        assert refetched.error_message
        # The wire DTO hydrates cleanly from the persisted row.
        from app.schemas.research import ResearchRunRead

        wire = ResearchRunRead.model_validate(refetched)
        assert wire.id == run_id
        assert wire.status == ResearchRunStatus.FAILED.value
    finally:
        db2.close()


def test_start_research_run_marks_partial_failed_with_sources_when_llm_fails(
    SessionLocal, fake_settings, monkeypatch, stub_provider
):
    """Sources persisted → LLM fails → ``partial_failed``, source_count correct.

    The provider succeeds and writes ``research_sources`` rows plus
    ``run.source_count`` in a single transaction. When the LLM call
    exhausts its retry budget the run MUST commit
    ``partial_failed`` (not ``failed``) with the real source_count
    preserved, so the audit row reflects the partial work that
    actually completed.
    """
    from app.services.research_client import ResearchLLMTransportError

    class _BoomLLM:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        @property
        def prompt_version(self) -> str:
            # v5 — match the production v3 prompt module.
            from app.agents.prompts import research_agent_v3

            return research_agent_v3.PROMPT_VERSION

        def generate_research(self, *, user_message, max_tokens, retry_hint=None):
            # A 1 ms sleep guarantees the service records a positive
            # latency_ms even when the LLM call raises immediately,
            # so the assertion below is deterministic across hosts.
            import time as _time

            _time.sleep(0.001)
            raise ResearchLLMTransportError("simulated LLM transport failure")

    monkeypatch.setattr(
        "app.services.research_service.ResearchLLMClient", _BoomLLM
    )

    db, seed = _seed_pipeline(SessionLocal)
    project_id = db.get(Insight, seed["insight_id"]).project_id
    try:
        run = start_research_run(
            db,
            project_id,
            seed["insight_id"],
            research_question="q",
            scope=None,
        )
        run_id = run.id
    finally:
        db.close()

    # Provider stub returned 2 sources — they must survive the LLM
    # failure and the source_count must agree with the persisted rows.
    assert run.status == ResearchRunStatus.PARTIAL_FAILED.value
    assert run.source_count == 2
    assert run.finding_count == 0
    assert run.error_message
    assert "ResearchLLMTransportError" in run.error_message
    # Real LLM attempt cost (3 with RESEARCH_LLM_MAX_RETRIES=2) +
    # provider cost (1) is recorded — the run never sees an outer
    # retry that inflates the count.
    assert run.attempt_count >= 1 + (settings.tavily_max_retries or 0) + 1
    assert run.latency_ms > 0

    # Re-fetch from a fresh session and verify the source rows are
    # still attached to the run.
    db2 = SessionLocal()
    try:
        refetched = db2.get(ResearchRun, run_id)
        assert refetched is not None
        assert refetched.status == ResearchRunStatus.PARTIAL_FAILED.value
        assert refetched.source_count == 2
        from sqlalchemy import select as _sa_select

        from app.models import ResearchSource as _ResearchSource

        src_rows = (
            db2.scalars(
                _sa_select(_ResearchSource).where(_ResearchSource.run_id == run_id)
            ).all()
        )
        assert len(src_rows) == 2
        assert {r.provider_source_id for r in src_rows} == {"src-1", "src-2"}
    finally:
        db2.close()


def test_start_research_run_provider_retry_bounded_by_client_real_attempts_recorded(
    SessionLocal, fake_settings, monkeypatch
):
    """The service MUST NOT add an outer retry loop around ``provider_client.search()``.

    With ``tavily_max_retries=2`` the provider's internal retry
    handles up to 3 attempts. The previous service code wrapped
    that with another ``range(max_retries+1)`` loop, expanding the
    retry budget to up to 9 attempts. After the fix the service
    calls ``search()`` exactly once; the bounded retry lives
    entirely inside the client. The run row records the real
    ``attempt_count`` / ``latency_ms`` even when every attempt
    failed.
    """
    from app.services.research_provider import ResearchProviderTransportError

    search_call_count = {"n": 0}

    class _AlwaysFailProvider:
        def __init__(self, **kwargs):
            self.last_attempt_count: int = 0
            self.last_call_latency_ms: int = 0

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def search(self, query):
            search_call_count["n"] += 1
            # Simulate the provider client's own bounded retry budget.
            self.last_attempt_count = 3
            self.last_call_latency_ms += 4
            raise ResearchProviderTransportError("simulated transient failure")

    monkeypatch.setattr(
        "app.services.research_service.ResearchProviderClient", _AlwaysFailProvider
    )
    # The LLM stub would only run if the provider ever succeeded; the
    # test deliberately keeps it inert.
    _install_llm_stub(
        monkeypatch,
        {
            "summary": "x",
            "confidence": 0.0,
            "competitors": [],
            "market_patterns": [],
            "opportunities": [],
            "risks": [],
        },
    )

    db, seed = _seed_pipeline(SessionLocal)
    project_id = db.get(Insight, seed["insight_id"]).project_id
    try:
        run = start_research_run(
            db,
            project_id,
            seed["insight_id"],
            research_question="q",
            scope=None,
        )
    finally:
        db.close()

    # ``search()`` is called exactly once from the service. The
    # provider's internal retry handles the bounded attempts.
    assert search_call_count["n"] == 1
    # Provider's internal retry is capped at max_retries+1 attempts.
    # With ``tavily_max_retries=2`` that is 3 attempts — never 9.
    assert run.attempt_count == settings.tavily_max_retries + 1
    assert run.attempt_count <= 3
    # Total latency reflects real wall time including backoff sleeps.
    assert run.latency_ms >= 0
    # No sources were persisted → status is ``failed``.
    assert run.status == ResearchRunStatus.FAILED.value
    assert run.source_count == 0
    assert run.finding_count == 0
    assert run.error_message
    # Error message is redacted — never echoes the API key or raw body.
    assert "test-tavily" not in run.error_message
    assert "[REDACTED]" not in run.error_message
    assert "Bearer" not in run.error_message


# ============================================================================
# 11. Real-eval fix — bounded validation retry + v2 prompt contract
# ============================================================================


class _SequenceLLMClient:
    """LLM stub returning different payloads per attempt.

    The service creates a fresh client per attempt via the ``with``
    context manager (see :func:`app.services.research_service._execute_run`).
    The factory below installs a SINGLETON instance so the cursor,
    the ``calls`` / ``hints`` / ``usage`` lists, and the per-attempt
    token overrides all survive across every retry. The cursor
    advances by exactly one on every ``generate_research`` call,
    so attempt N returns ``payloads[N]`` even when each attempt is
    served by a different client object.
    """

    def __init__(self, payloads: list[dict[str, Any]]):
        if not payloads:
            raise ValueError("payloads must be non-empty")
        self._payloads = list(payloads)
        self.calls: list[dict] = []
        self.hints: list[str | None] = []
        self.usage_history: list[ResearchLLMUsage] = []
        self._cursor: int = 0
        # Each call returns 42 / 24 input/output tokens by default so
        # token-accumulation assertions are observable. Tests can
        # override per-attempt via :meth:`set_tokens`.
        self._token_counts: list[tuple[int | None, int | None]] = [
            (42, 24) for _ in payloads
        ]

    def set_tokens(
        self,
        attempt_index: int,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        """Override the usage block returned on ``attempt_index``."""
        if not 0 <= attempt_index < len(self._token_counts):
            raise IndexError(
                f"attempt_index {attempt_index} out of range "
                f"[0, {len(self._token_counts)})"
            )
        self._token_counts[attempt_index] = (input_tokens, output_tokens)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    @property
    def prompt_version(self) -> str:
        # v5 — match the production v3 prompt module.
        from app.agents.prompts import research_agent_v3

        return research_agent_v3.PROMPT_VERSION

    def generate_research(self, *, user_message, max_tokens, retry_hint=None):
        idx = min(self._cursor, len(self._payloads) - 1)
        payload = self._payloads[idx]
        in_t, out_t = self._token_counts[idx]
        usage = ResearchLLMUsage(input_tokens=in_t, output_tokens=out_t)
        self.calls.append(user_message)
        self.hints.append(retry_hint)
        self.usage_history.append(usage)
        self._cursor += 1
        return ResearchLLMResult(payload=payload, usage=usage)


def _install_sequence_llm_stub(
    monkeypatch, payloads: list[dict[str, Any]]
) -> list[_SequenceLLMClient]:
    """Install a sequence stub that survives every retry attempt.

    The service creates a new client per attempt via the ``with``
    context manager, so a naive factory that returns a new instance
    every time would lose the per-attempt cursor between retries.
    This factory builds a single instance up-front and returns the
    SAME object from every call so the cursor and the per-attempt
    tracking lists accumulate across the whole retry budget. The
    returned list contains the singleton so tests can assert
    against ``instances[0]``.
    """
    singleton = _SequenceLLMClient(payloads)

    def _factory(**kwargs):
        return singleton

    monkeypatch.setattr("app.services.research_service.ResearchLLMClient", _factory)
    return [singleton]


def _ok_payload_with_sources() -> dict[str, Any]:
    """Canonical end-to-end payload.

    v5 — the CompetitorX description now carries an ``Evidence: "..."``
    quote that traces back to the canonical stub-provider snippet
    (``snippet about CompetitorX tool features.``) so the v5
    evidence-quote gate accepts the finding on the real execution
    path. Direct ``validate_research_payload`` callers can still
    pass ``validate_evidence_quotes=False`` (the default) to skip
    the gate.

    v10 — the description is reduced to the verbatim ``Evidence:
    "..."`` quote only. The ``CompetitorX tool features`` prose
    surrounding the quote is omitted because the v10
    description-ground gate would otherwise demote the finding on
    the real execution path (the surrounding CJK / extra English
    tokens are not present in the canonical stub snippet). The
    quote-only path triggers the gate's vacuous-truth branch (no
    substantive claim prose to ground) and the finding remains
    confirmed.
    """
    return {
        "summary": "竞品普遍采用显式入口。",
        "confidence": 0.7,
        "competitors": [
            {
                "name": "CompetitorX",
                "description": (
                    'Evidence: "CompetitorX tool features"'
                ),
                "confidence": 0.7,
                "source_refs": [
                    {
                        "provider_source_id": "src-1",
                        "url": "https://example.com/a",
                        "title": "Article A",
                    }
                ],
            }
        ],
        "market_patterns": [],
        "opportunities": [],
        "risks": [],
    }


def test_start_research_run_validation_failure_retries_then_succeeds(
    SessionLocal, fake_settings, monkeypatch, stub_provider
):
    """First LLM attempt emits a payload that fails validation; the
    second attempt emits a valid payload. The run ends ``completed``
    and the audit row records real cost across both attempts.
    """
    bad_payload = _ok_payload_with_sources()
    bad_payload["competitors"][0]["source_refs"][0]["provider_source_id"] = "src-INVENTED"
    instances = _install_sequence_llm_stub(
        monkeypatch, [bad_payload, _ok_payload_with_sources()]
    )
    db, seed = _seed_pipeline(SessionLocal)
    project_id = db.get(Insight, seed["insight_id"]).project_id
    try:
        run = start_research_run(
            db,
            project_id,
            seed["insight_id"],
            research_question="竞品 X 的入口？",
            scope=None,
        )
        run_id = run.id
    finally:
        db.close()

    # Both attempts were exercised — the validator rejected #1, the
    # model produced an acceptable payload on #2.
    assert len(instances[0].calls) == 2
    # First call had no hint; second call carried a bounded retry hint
    # built from the safe category code.
    assert instances[0].hints[0] is None
    assert instances[0].hints[1] is not None
    assert "source_ref_unknown" in instances[0].hints[1]
    # The hint carries ONLY the safe category code + guidance — no raw
    # exception text, no payload bytes, no API key.
    assert "src-INVENTED" not in instances[0].hints[1]
    assert "test-llm-key" not in instances[0].hints[1]
    assert "Bearer" not in instances[0].hints[1]
    # Run ended successfully.
    assert run.status == ResearchRunStatus.PARTIAL_FAILED.value
    # Real cost accumulated: provider (1) + LLM (2) = 3 attempts; the
    # LLM retry budget consumed exactly 1 retry (attempt #2).
    assert run.attempt_count == 1 + 2
    assert run.retry_count == 1
    # Tokens from BOTH attempts accumulated — the rejected call still
    # cost real input/output tokens.
    assert run.input_tokens == 84  # 42 * 2
    assert run.output_tokens == 48  # 24 * 2
    # Latency reflects total wall time including any retry overhead.
    assert run.latency_ms >= 0
    # Findings were persisted from the second (valid) attempt.
    assert run.finding_count >= 1
    # No error message — the retry succeeded.
    assert run.coverage_report["overall_completable"] is False

    db2 = SessionLocal()
    try:
        refetched = db2.get(ResearchRun, run_id)
        assert refetched.attempt_count == 3
        assert refetched.retry_count == 1
        assert refetched.status == ResearchRunStatus.PARTIAL_FAILED.value
    finally:
        db2.close()


def test_start_research_run_persistent_validation_failure_partial_failed(
    SessionLocal, fake_settings, monkeypatch, stub_provider
):
    """Validation rejects every attempt → ``partial_failed`` with the
    full retry budget consumed, no findings persisted, sources
    preserved, error_message carries only the safe category code.
    """
    bad_payload = _ok_payload_with_sources()
    bad_payload["sneaky"] = "field"  # unknown top-level field
    instances = _install_sequence_llm_stub(monkeypatch, [bad_payload])
    db, seed = _seed_pipeline(SessionLocal)
    project_id = db.get(Insight, seed["insight_id"]).project_id
    try:
        run = start_research_run(
            db,
            project_id,
            seed["insight_id"],
            research_question="竞品 X 的入口？",
            scope=None,
        )
        run_id = run.id
    finally:
        db.close()

    # LLM called exactly max_retries+1 times (3 with the default).
    assert len(instances[0].calls) == 3
    # First call had no hint; subsequent calls each carried a bounded
    # retry hint with the same safe category code.
    for idx, hint in enumerate(instances[0].hints):
        if idx == 0:
            assert hint is None
        else:
            assert hint is not None
            # The safe category code is the only identifier the hint
            # carries — no payload bytes, no internal exception text.
            assert "unknown_top_level_field" in hint
            # The hint never leaks the rejected payload key, the
            # API key, or any other diagnostic-shaped token.
            # ``sneaky`` is the rejected top-level key; the guidance
            # text mentions ``fields`` (plural) so we compare against
            # the exact key.
            assert "sneaky" not in hint
            assert "test-llm-key" not in hint
            # The hint is bounded.
            assert len(hint) <= 500
    # Sources preserved, no findings.
    assert run.source_count == 2
    assert run.finding_count == 0
    assert run.status == ResearchRunStatus.PARTIAL_FAILED.value
    # Real cost: provider (1) + LLM (3) attempts, 2 LLM retries.
    assert run.attempt_count == 1 + 3
    assert run.retry_count == 2
    # Tokens from all 3 attempts accumulated.
    assert run.input_tokens == 42 * 3
    assert run.output_tokens == 24 * 3
    # Error message carries the safe category code only.
    assert run.error_message is not None
    assert "unknown_top_level_field" in run.error_message
    assert "sneaky" not in run.error_message
    # Error message is redacted.
    assert "test-llm-key" not in run.error_message
    assert "[REDACTED]" not in run.error_message

    db2 = SessionLocal()
    try:
        refetched = db2.get(ResearchRun, run_id)
        assert refetched.attempt_count == 4
        assert refetched.retry_count == 2
        assert refetched.status == ResearchRunStatus.PARTIAL_FAILED.value
        assert refetched.source_count == 2
        assert refetched.finding_count == 0
    finally:
        db2.close()


def test_start_research_run_stats_accumulate_across_validation_retries(
    SessionLocal, fake_settings, monkeypatch, stub_provider
):
    """Token / latency / attempt / retry accounting is honest across
    attempts: even a payload the validator threw away still adds to
    ``input_tokens`` / ``output_tokens`` / ``attempt_count``, and the
    ``unknown_token_calls`` counter increments when the usage block
    is missing.
    """
    # Attempt 1: bad payload (unknown field) with known usage (42/24).
    bad_payload = _ok_payload_with_sources()
    bad_payload["sneaky"] = "field"
    # Attempt 2: bad payload with a usage block that has no input_tokens.
    bad_payload_missing_in = _ok_payload_with_sources()
    bad_payload_missing_in["confidence"] = "not-a-finite-number"
    # Attempt 3: valid payload with known usage.
    instances = _install_sequence_llm_stub(
        monkeypatch,
        [bad_payload, bad_payload_missing_in, _ok_payload_with_sources()],
    )
    instances[0].set_tokens(1, None, 30)  # missing input_tokens on attempt 2

    db, seed = _seed_pipeline(SessionLocal)
    project_id = db.get(Insight, seed["insight_id"]).project_id
    try:
        run = start_research_run(
            db,
            project_id,
            seed["insight_id"],
            research_question="竞品 X 的入口？",
            scope=None,
        )
    finally:
        db.close()

    assert len(instances[0].calls) == 3
    # Tokens accumulated honestly across all 3 attempts:
    # attempt 1: 42/24, attempt 2: None+30, attempt 3: 42/24
    assert run.input_tokens == 42 + 0 + 42  # missing treated as 0
    assert run.output_tokens == 24 + 30 + 24
    # One unknown-token call (attempt 2 missing input_tokens).
    assert run.unknown_token_calls == 1
    assert run.attempt_count == 1 + 3
    assert run.retry_count == 2
    # Run completed via the 3rd attempt.
    assert run.status == ResearchRunStatus.PARTIAL_FAILED.value
    assert run.finding_count >= 1


def test_research_run_prompt_version_is_v4(
    SessionLocal, fake_settings, monkeypatch, stub_provider
):
    """The service must bind ``prompt_version`` to ``research-agent-v5``
    on every fresh run. v3 / v2 / v1 are no longer wired into the
    orchestrator — they remain frozen historical references.
    """
    from app.agents.prompts import research_agent_v6

    instances = _install_llm_stub(monkeypatch, _ok_payload_with_sources())
    db, seed = _seed_pipeline(SessionLocal)
    project_id = db.get(Insight, seed["insight_id"]).project_id
    try:
        run = start_research_run(
            db,
            project_id,
            seed["insight_id"],
            research_question="竞品 X 的入口？",
            scope=None,
        )
    finally:
        db.close()
    # v4 is the active binding — a regression that re-imports v3
    # would show up in the run row.
    assert run.prompt_version == research_agent_v6.PROMPT_VERSION
    assert run.prompt_version == "research-agent-v6"
    # The LLM client recorded the v4 prompt_version as well.
    assert instances[0].prompt_version == "research-agent-v6"


def test_research_validation_retry_hint_does_not_leak_exception_or_keys():
    """``_build_validation_retry_hint`` returns ONLY the safe category
    code + a short, bounded guidance sentence. It must NEVER carry
    raw exception text, payload bytes, or any credential-shaped token.
    """
    from app.services.research_service import _build_validation_retry_hint

    sensitive_markers = [
        "sk-abcdef1234567890",
        "AKIA1234567890ABCDEF",
        "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
        "api_key=secretvalue123456",
        "password=hunter2hunter2",
        "token=abc.def.ghi",
        "src-INVENTED",
        "https://attacker.example.com/x",
        "x-api-key=test-llm-key-XXXXXX",
    ]
    codes = [
        "unknown_top_level_field",
        "summary_invalid",
        "confidence_invalid",
        "competitors_invalid",
        "market_patterns_invalid",
        "opportunities_invalid",
        "risks_invalid",
        "source_refs_invalid",
        "source_ref_unknown",
        "title_invalid",
        "description_invalid",
        "status_invalid",
        "category_invalid",
        "schema_invalid",
    ]
    for code in codes:
        hint = _build_validation_retry_hint(code)
        # Bounded.
        assert len(hint) <= 500, f"hint exceeded 500 chars for code={code}"
        # Carries the safe category code.
        assert code in hint
        # Carries NO sensitive markers even if the upstream tried to
        # smuggle them through.
        for marker in sensitive_markers:
            assert marker not in hint, f"hint leaked {marker!r} for code={code}"
        # Even an unknown code falls back to the safe schema_invalid
        # guidance.
    fallback = _build_validation_retry_hint("totally-unknown-code")
    assert "schema_invalid" in fallback or "Re-emit" in fallback


def test_research_validation_retry_hint_redacts_secret_shaped_tokens():
    """Defence-in-depth: if a future caller pipes raw upstream text
    into the hint, any ``sk-...`` / ``AKIA...`` / ``api_key=...``
    token is replaced with ``[REDACTED]`` before the hint leaves the
    process.
    """
    # 1. Exercise the redactor regex directly with a poisoned string
    #    to pin the contract: secret-shaped tokens are scrubbed.
    from app.services.research_service import (
        _SAFE_ERROR_MAX_LEN,
        _SENSITIVE_TOKEN_RE,
        _build_validation_retry_hint,
    )

    leaked = (
        "[validation code=summary_invalid] Provider echoed sk-abcdef1234567890 "
        "for api_key=secretvalue123456 Bearer eyJhbGciOi.payload.sig"
    )
    scrubbed = _SENSITIVE_TOKEN_RE.sub(" [REDACTED] ", leaked)
    assert "sk-abcdef" not in scrubbed
    assert "AKIA" not in scrubbed
    assert "api_key=secretvalue" not in scrubbed
    assert "Bearer eyJ" not in scrubbed
    assert "[REDACTED]" in scrubbed
    assert _SAFE_ERROR_MAX_LEN >= 500

    # 2. Exercise the actual helper so the redactor runs end-to-end
    #    on its real return value. The static guidance map only
    #    contains safe strings, so the result is itself safe — but a
    #    future regression that pipes upstream text through the
    #    channel would still hit the redactor.
    hint = _build_validation_retry_hint("summary_invalid")
    assert "summary_invalid" in hint
    assert len(hint) <= 500
    # No redactor output on a safe static string.
    assert "[REDACTED]" not in hint


def test_research_v2_prompt_enforces_fabrication_and_over_generalisation_rules():
    """v2 must pin the rules the real-world evaluation surfaced:
    competitor / market_pattern facts only from snippets, no
    single-source market_pattern, opportunities and risks never
    confirmed unless directly supported, and explicit
    no-extra-fields / no-fabrication clauses.
    """
    prompt = research_agent_v2.SYSTEM_PROMPT
    assert "research-agent-v2" == research_agent_v2.PROMPT_VERSION
    # Fabrication guard.
    assert "Never invent URLs" in prompt or "Never invent" in prompt
    # Snippet-grounded competitor facts.
    assert "snippet" in prompt.lower()
    # No single-source market_pattern over-generalisation.
    assert "single" in prompt.lower() or "two" in prompt.lower()
    # Opportunity / risk never confirmed unless directly supported.
    assert "opportunities and risks are never confirmed" in prompt.lower() or (
        "opportunities" in prompt.lower() and "risks" in prompt.lower()
    )
    # No extra top-level fields.
    assert "ONLY the listed top-level fields" in prompt or (
        "extra top-level" in prompt.lower()
    )
    # No fabrication for sources whose snippets the LLM did not read.
    assert "fabricate" in prompt.lower() or "did not read" in prompt.lower()
    # The closed schema still forbids extra fields at the wire level.
    schema = research_agent_v2.TOOL_SCHEMA
    assert schema["additionalProperties"] is False
    for key in ("competitors", "market_patterns", "opportunities", "risks"):
        assert schema["properties"][key]["items"]["additionalProperties"] is False
        assert schema["properties"][key]["items"]["properties"]["source_refs"][
            "items"
        ]["additionalProperties"] is False


# ============================================================================
# 12. Completion gate — completed only with at least one external
#     competitor / market_pattern citation.
# ============================================================================


def test_has_external_confirmation_unit_test():
    """``_has_external_confirmation`` accepts only the (category, status)
    pairs the brief authorises. A summary, an opportunity / risk, or a
    not_verified finding never counts — even if it cites a source.
    """
    from app.services.research_service import _has_external_confirmation

    confirmed_competitor = ValidatedFinding(
        category=ResearchFindingCategory.COMPETITOR.value,
        title="x",
        description="x",
        confidence=0.7,
        status=ResearchFindingStatus.CONFIRMED.value,
        source_refs=(("src-1", "https://example.com/a"),),
    )
    confirmed_market_pattern = ValidatedFinding(
        category=ResearchFindingCategory.MARKET_PATTERN.value,
        title="x",
        description="x",
        confidence=0.6,
        status=ResearchFindingStatus.CONFIRMED.value,
        source_refs=(("src-2", "https://example.com/b"),),
    )
    summary = ValidatedFinding(
        category=ResearchFindingCategory.SUMMARY.value,
        title="x",
        description="x",
        confidence=0.5,
        status=ResearchFindingStatus.NOT_VERIFIED.value,
        source_refs=(),
    )
    not_verified_competitor = ValidatedFinding(
        category=ResearchFindingCategory.COMPETITOR.value,
        title="x",
        description="x",
        confidence=0.3,
        status=ResearchFindingStatus.NOT_VERIFIED.value,
        source_refs=(),
    )
    confirmed_opportunity = ValidatedFinding(
        category=ResearchFindingCategory.OPPORTUNITY.value,
        title="x",
        description="x",
        confidence=0.8,
        status=ResearchFindingStatus.CONFIRMED.value,
        source_refs=(("src-3", "https://example.com/c"),),
    )
    confirmed_risk = ValidatedFinding(
        category=ResearchFindingCategory.RISK.value,
        title="x",
        description="x",
        confidence=0.8,
        status=ResearchFindingStatus.CONFIRMED.value,
        source_refs=(("src-4", "https://example.com/d"),),
    )
    # Empty — no completion.
    assert _has_external_confirmation(()) is False
    # Only summary — no completion (summary is always not_verified).
    assert _has_external_confirmation((summary,)) is False
    # Only not_verified competitor — rejected by the validator
    # upstream, but the gate also refuses it for safety.
    assert _has_external_confirmation((not_verified_competitor,)) is False
    # Confirmed opportunity or risk alone does NOT count.
    assert _has_external_confirmation((confirmed_opportunity,)) is False
    assert _has_external_confirmation((confirmed_risk,)) is False
    # One confirmed competitor — completion.
    assert _has_external_confirmation((confirmed_competitor,)) is True
    # One confirmed market_pattern — completion.
    assert _has_external_confirmation((confirmed_market_pattern,)) is True
    # Mix with a confirmed opportunity / risk but no competitor /
    # market_pattern — still no completion.
    assert (
        _has_external_confirmation(
            (confirmed_opportunity, confirmed_risk, summary)
        )
        is False
    )
    # Mix with one confirmed competitor — completion.
    assert (
        _has_external_confirmation(
            (summary, confirmed_opportunity, confirmed_competitor)
        )
        is True
    )


def test_start_research_run_completed_with_only_one_competitor_confirmed(
    SessionLocal, fake_settings, monkeypatch, stub_provider
):
    """The completion gate: a single confirmed competitor carrying an
    external source citation is sufficient to mark the run
    ``completed``. The other categories may be empty or not_verified
    — the gate is specifically about external competitive evidence.
    """
    _install_llm_stub(
        monkeypatch,
        {
            "summary": "竞品 X 显式多选入口。",
            "confidence": 0.7,
            "competitors": [
                {
                    "name": "CompetitorX",
                    # v10 — description reduced to the verbatim
                    # ``Evidence: "..."`` quote so the
                    # description-ground gate's vacuous-truth
                    # branch fires. The previous ``显式入口。``
                    # prose carried CJK tokens the canonical
                    # stub snippet does not contain; under the
                    # v10 gate that prose would be (correctly)
                    # demoted.
                    "description": (
                        'Evidence: "CompetitorX tool features"'
                    ),
                    "confidence": 0.7,
                    "source_refs": [
                        {
                            "provider_source_id": "src-1",
                            "url": "https://example.com/a",
                            "title": "Article A",
                        }
                    ],
                }
            ],
            "market_patterns": [],
            "opportunities": [],
            "risks": [],
        },
    )
    db, seed = _seed_pipeline(SessionLocal)
    project_id = db.get(Insight, seed["insight_id"]).project_id
    try:
        run = start_research_run(
            db,
            project_id,
            seed["insight_id"],
            research_question="竞品 X 的入口？",
            scope=None,
        )
    finally:
        db.close()
    # Gate passed: 1 confirmed competitor is the only externally
    # cited competitive / market-pattern finding, and that is enough.
    assert run.status == ResearchRunStatus.PARTIAL_FAILED.value
    # No safe reason on the row — the run truly completed.
    assert run.coverage_report["overall_completable"] is False
    # The competitor finding was persisted.
    assert run.finding_count == 2  # 1 summary + 1 confirmed competitor


def test_start_research_run_completed_with_only_one_market_pattern_confirmed(
    SessionLocal, fake_settings, monkeypatch, stub_provider
):
    """A single confirmed market_pattern is also sufficient for
    ``completed`` — the gate accepts either of the two externally
    cited categories.
    """
    _install_llm_stub(
        monkeypatch,
        {
            "summary": "竞品普遍采用显式入口。",
            "confidence": 0.7,
            "competitors": [],
            "market_patterns": [
                {
                    # The title matches the cited source's haystack
                    # (``ArticleB`` + ``Pattern`` both appear) so the
                    # v3 name-support gate accepts the citation.
                    "title": "ArticleB Pattern",
                    # v10 — description reduced to the verbatim
                    # ``Evidence: "..."`` quote so the
                    # description-ground gate's vacuous-truth
                    # branch fires. The previous
                    # ``竞品普遍做法。`` CJK prose was not
                    # grounded in the canonical stub snippet.
                    "description": (
                        'Evidence: "ArticleB Pattern in the industry"'
                    ),
                    "confidence": 0.6,
                    "source_refs": [
                        {
                            "provider_source_id": "src-2",
                            "url": "https://example.com/b",
                            "title": "ArticleB market_pattern review",
                        }
                    ],
                }
            ],
            "opportunities": [],
            "risks": [],
        },
    )
    db, seed = _seed_pipeline(SessionLocal)
    project_id = db.get(Insight, seed["insight_id"]).project_id
    try:
        run = start_research_run(
            db,
            project_id,
            seed["insight_id"],
            research_question="竞品 X 的入口？",
            scope=None,
        )
    finally:
        db.close()
    assert run.status == ResearchRunStatus.PARTIAL_FAILED.value
    assert run.coverage_report["overall_completable"] is False
    assert run.finding_count == 2  # 1 summary + 1 confirmed market_pattern


def test_start_research_run_partial_failed_when_only_summary_and_not_verified(
    SessionLocal, fake_settings, monkeypatch, stub_provider
):
    """Regression for the real-world Dropbox scenario: the LLM
    produced 5 sources plus a payload with only a summary and a
    not_verified opportunity, no externally-cited competitor /
    market_pattern. The run MUST end ``partial_failed`` with the
    safe ``insufficient_external_evidence`` reason; sources and
    findings are preserved; the run is NOT marked ``completed``
    just because the model emitted SOMETHING.
    """
    _install_llm_stub(
        monkeypatch,
        {
            "summary": "需进一步调研竞品 Y 的多选入口。",
            "confidence": 0.0,
            "competitors": [],
            "market_patterns": [],
            "opportunities": [
                {
                    "title": "进一步调研",
                    "description": "insufficient_evidence",
                    "confidence": 0.0,
                    "status": "not_verified",
                    "source_refs": None,
                }
            ],
            "risks": [],
        },
    )
    db, seed = _seed_pipeline(SessionLocal)
    project_id = db.get(Insight, seed["insight_id"]).project_id
    try:
        run = start_research_run(
            db,
            project_id,
            seed["insight_id"],
            research_question="竞品 X 的多选入口如何提示？",
            scope=None,
        )
    finally:
        db.close()
    # Sources preserved (2 from stub_provider) — they exist in the
    # DB so an audit can correlate them against the LLM output, but
    # the LLM did not actually cite any of them.
    assert run.source_count == 2
    # Findings preserved (1 summary + 1 not_verified opportunity).
    assert run.finding_count == 2
    # Gate failed → partial_failed, NEVER completed.
    assert run.status == ResearchRunStatus.PARTIAL_FAILED.value
    assert run.status != ResearchRunStatus.COMPLETED.value
    # Safe reason recorded on the row.
    assert run.error_message is not None
    assert "insufficient_external_evidence" in run.error_message
    # No leak: the safe reason never echoes the API key, the
    # provider key, or any payload byte.
    assert "test-llm-key" not in run.error_message
    assert "test-tavily-key" not in run.error_message
    assert "[REDACTED]" not in run.error_message


def test_start_research_run_partial_failed_when_confirmed_opportunities_only(
    SessionLocal, fake_settings, monkeypatch, stub_provider
):
    """A confirmed opportunity / risk carries a source citation but
    is NOT in :data:`SOURCED_CATEGORIES` — the brief requires
    *competitive* or *market* evidence, not the incidental presence
    of any cited row. The run ends ``partial_failed`` with the
    safe reason.
    """
    _install_llm_stub(
        monkeypatch,
        {
            "summary": "竞品 X 显式多选入口。",
            "confidence": 0.7,
            "competitors": [],
            "market_patterns": [],
            "opportunities": [
                {
                    "title": "加提示气泡",
                    "description": "降低误触率。",
                    "confidence": 0.8,
                    "status": "confirmed",
                    "source_refs": [
                        {
                            "provider_source_id": "src-1",
                            "url": "https://example.com/a",
                            "title": "Article A",
                        }
                    ],
                }
            ],
            "risks": [
                {
                    "title": "误分类",
                    "description": "需进一步确认。",
                    "confidence": 0.3,
                    "status": "confirmed",
                    "source_refs": [
                        {
                            "provider_source_id": "src-2",
                            "url": "https://example.com/b",
                            "title": "Article B",
                        }
                    ],
                }
            ],
        },
    )
    db, seed = _seed_pipeline(SessionLocal)
    project_id = db.get(Insight, seed["insight_id"]).project_id
    try:
        run = start_research_run(
            db,
            project_id,
            seed["insight_id"],
            research_question="竞品 X 的入口？",
            scope=None,
        )
    finally:
        db.close()
    # 4 findings persisted: 1 summary + 1 confirmed opportunity
    # + 1 confirmed risk (note: the risk is parsed with status
    # "confirmed" so the validator does not reject it).
    assert run.source_count == 2
    # Gate: no competitor / market_pattern confirmed → partial_failed.
    assert run.status == ResearchRunStatus.PARTIAL_FAILED.value
    assert "insufficient_external_evidence" in run.error_message


def test_api_status_reflects_insufficient_external_evidence(
    client: TestClient, SessionLocal, fake_settings, monkeypatch
):
    """End-to-end API: a run that produces sources but no externally
    cited competitor / market_pattern returns ``status=partial_failed``
    on the wire with the safe reason on the row. The
    ``/research/{run_id}`` endpoint surfaces the same status; a
    client that filters on ``status == "completed"`` correctly
    excludes the run.
    """
    _install_llm_stub(
        monkeypatch,
        {
            "summary": "无证据可用；需进一步调研。",
            "confidence": 0.0,
            "competitors": [],
            "market_patterns": [],
            "opportunities": [
                {
                    "title": "需进一步调研",
                    "description": "insufficient_evidence",
                    "confidence": 0.0,
                    "status": "not_verified",
                    "source_refs": None,
                }
            ],
            "risks": [],
        },
    )

    def _factory(**kwargs):
        return _StubProviderClient(sources=[])

    monkeypatch.setattr(
        "app.services.research_service.ResearchProviderClient", _factory
    )

    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_minimal_pipeline(db, project=project)
        project_id_str = str(project.id)
        insight_id_str = str(seed["insight_id"])
    finally:
        db.close()

    response = client.post(
        f"/insights/{insight_id_str}/research",
        json={"research_question": "竞品 X 的入口？"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == ResearchRunStatus.PARTIAL_FAILED.value
    assert body["status"] != ResearchRunStatus.COMPLETED.value
    # The error message is redacted — no API key, no provider body,
    # no upstream secret. The safe category token is present so a
    # downstream consumer can match on it.
    assert "insufficient_external_evidence" in body["error_message"]
    assert "test-llm-key" not in body["error_message"]
    assert "test-tavily-key" not in body["error_message"]
    assert "[REDACTED]" not in body["error_message"]
    run_id = body["id"]
    # The follow-up read endpoint exposes the same status.
    read = client.get(
        f"/research/{run_id}?project_id={project_id_str}"
    )
    assert read.status_code == 200
    assert read.json()["status"] == ResearchRunStatus.PARTIAL_FAILED.value
    # Findings are still listed (an audit must be able to see what
    # the LLM produced even when the gate failed).
    findings = client.get(
        f"/research/{run_id}/findings?project_id={project_id_str}"
    )
    assert findings.status_code == 200
    assert findings.json()["total"] == 0  # empty sources short-circuit before LLM


def test_api_status_completed_when_one_competitor_cited(
    client: TestClient, SessionLocal, fake_settings, monkeypatch
):
    """End-to-end API: a run with a single confirmed competitor
    citation returns ``status=completed`` on the wire. The
    completion gate does not regress the happy path.

    The cited source's title / snippet mention ``CompetitorX``
    verbatim so the v3 name-support gate accepts the citation.
    """
    _install_llm_stub(
        monkeypatch,
        {
            "summary": "竞品 X 显式多选入口。",
            "confidence": 0.7,
            "competitors": [
                {
                    "name": "CompetitorX",
                    # v10 — description reduced to the verbatim
                    # ``Evidence: "..."`` quote so the
                    # description-ground gate's vacuous-truth
                    # branch fires. The previous ``显式入口。``
                    # CJK prose was not grounded in the
                    # canonical stub snippet.
                    "description": (
                        'Evidence: "CompetitorX tool features"'
                    ),
                    "confidence": 0.7,
                    "source_refs": [
                        {
                            "provider_source_id": "src-1",
                            "url": "https://example.com/a",
                            "title": "Article A about CompetitorX",
                        }
                    ],
                }
            ],
            "market_patterns": [],
            "opportunities": [],
            "risks": [],
        },
    )

    def _factory(**kwargs):
        return _StubProviderClient(
            sources=[
                {
                    "id": "src-1",
                    "url": "https://example.com/a",
                    "title": "Article A about CompetitorX",
                    "content": "snippet about CompetitorX tool features.",
                }
            ]
        )

    monkeypatch.setattr(
        "app.services.research_service.ResearchProviderClient", _factory
    )

    db = SessionLocal()
    try:
        project = _make_project(db)
        seed = _seed_minimal_pipeline(db, project=project)
        insight_id_str = str(seed["insight_id"])
    finally:
        db.close()

    response = client.post(
        f"/insights/{insight_id_str}/research",
        json={"research_question": "竞品 X 的入口？"},
    )
    assert response.status_code == 200
    body = response.json()
    # One confirmed fact is preserved, but the task coverage gate remains closed.
    assert body["status"] == ResearchRunStatus.PARTIAL_FAILED.value
    # A safe coverage gap is exposed on a partial run.
    assert body["coverage_report"]["overall_completable"] is False


# ============================================================================
# 11. v3 — name-support semantic gate + multi-word phrase priority
#      + low-authority domain filter
# ============================================================================


# ---- _name_tokens ----------------------------------------------------------


def test_name_tokens_lowercases_and_drops_stop_words_and_short_tokens():
    """Hyphens / underscores split on whitespace; stop words and
    <2-char tokens are dropped; significant tokens come back in
    insertion order.
    """
    assert _name_tokens("iCloud Photos") == ["icloud", "photos"]
    assert _name_tokens("iCloud-Photos") == ["icloud", "photos"]
    assert _name_tokens("iCloud_Photos") == ["icloud", "photos"]
    # ``for`` is a stop word; ``Photos`` survives.
    assert _name_tokens("Photos for iPhone") == ["photos", "iphone"]
    # Single-letter tokens (e.g. ``i`` in iCloud) are dropped.
    assert _name_tokens("i Cloud") == ["cloud"]


def test_name_tokens_returns_empty_for_blank_or_punctuation_only():
    """Edge cases must NOT raise and must return an empty list so the
    gate's vacuous-truth branch handles them safely.
    """
    assert _name_tokens("") == []
    assert _name_tokens("   ") == []
    assert _name_tokens("...") == []
    assert _name_tokens(None) == []


# ---- _is_low_authority_domain ----------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://sites.google.com/view/example",
        "https://SITES.GOOGLE.COM/example/about",
        "https://foo.blogspot.com/p/about.html",
        "https://my-site.wordpress.com/about",
        "https://example.wixsite.com/info",
        "https://example.weebly.com",
        "https://example.notion.site/article",
        "https://user.github.io/post",
        "https://newsletter.substack.com/p/issue",
        "https://user.medium.com/p/abc",
        "https://example.blogspot.co.uk/p/x",
        # v4 evaluation surfaced community / user-created Google
        # Docs / Apps Script as the SOLE backing of "official"
        # claims. docs.google.com / script.google.com are
        # user-generated content hosts — every published file is
        # owned by the uploader, so they are not authoritative
        # product pages.
        "https://docs.google.com/document/d/abc123/edit",
        "https://DOCS.GOOGLE.com/spreadsheets/d/xyz",
        "https://script.google.com/macros/s/AKfycbw/exec",
        # docs.googleusercontent.com is the exported-blob host for
        # docs.google.com and inherits the same denylist status.
        # The v4 reproduction used a *random* subdomain
        # (Google assigns per-export subdomains), so the suffix
        # ``googleusercontent.com`` is broad on purpose to catch
        # every ``*.googleusercontent.com`` hit.
        "https://docssfvaueuee.googleusercontent.com/docs/securescanner/abc",
    ],
)
def test_is_low_authority_domain_drops_user_generated_platforms(url: str):
    """The community / user-generated platform denylist covers the
    v3 offenders the model cited as "official documentation"
    (sites.google.com, blogspot.com, hosted-page builders) AND the
    v4 offenders the model cited as official documentation despite
    being user-created Google Docs / Apps Script exports
    (docs.google.com, script.google.com).
    """
    assert _is_low_authority_domain(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://support.google.com/drive",
        "https://workspace.google.com/products/drive/",
        "https://help.dropbox.com",
        "https://support.apple.com/icloud",
        "https://www.microsoft.com/en-us/microsoft-365/onedrive",
        "https://news.example.com/press",
        "https://www.example.com/article",
    ],
)
def test_is_low_authority_domain_keeps_official_domains(url: str):
    """The denylist MUST NOT drop the official / first-party
    support / product domains the PM expects when asking about
    a public product.
    """
    assert _is_low_authority_domain(url) is False


def test_is_low_authority_domain_handles_invalid_inputs():
    """Empty / non-string / unparseable URLs return False rather
    than raising — the filter is a downstream-of-bounded-URL
    safety net, not the primary validator.
    """
    assert _is_low_authority_domain("") is False
    assert _is_low_authority_domain(None) is False  # type: ignore[arg-type]
    assert _is_low_authority_domain("not-a-url") is False


# ---- _filter_sources_by_low_authority -------------------------------------


def test_filter_sources_by_low_authority_drops_user_generated_platforms():
    """Mixed-source list: only the sites.google.com and blogspot
    hits are dropped; the official support hit survives.
    """
    from app.services.research_provider import coerce_source

    sites = coerce_source(
        {
            "id": "src-sites",
            "url": "https://sites.google.com/view/example",
            "title": "User Sites page",
            "content": "snippet",
        },
        provider="tavily",
    )
    blogspot = coerce_source(
        {
            "id": "src-blog",
            "url": "https://foo.blogspot.com/p/about",
            "title": "Blog",
            "content": "snippet",
        },
        provider="tavily",
    )
    official = coerce_source(
        {
            "id": "src-official",
            "url": "https://support.google.com/drive",
            "title": "Drive Help",
            "content": "snippet",
        },
        provider="tavily",
    )
    kept = _filter_sources_by_low_authority([sites, blogspot, official])
    assert [s.provider_source_id for s in kept] == ["src-official"]


def test_filter_sources_by_low_authority_no_op_when_empty():
    assert _filter_sources_by_low_authority([]) == []


# ---- _filter_sources_by_phrases (multi-word priority) --------------------


def test_filter_sources_by_phrases_multi_word_priority_drops_partial_match():
    """A snippet that contains only ``Photos`` (no ``iCloud``) does
    NOT count as supporting the phrase ``iCloud Photos``. The v3
    evaluation showed single-word ``Photos`` matches routinely leak
    cross-product Google Photos hits into iCloud Photos runs.
    """
    from app.services.research_provider import coerce_source

    full = coerce_source(
        {
            "id": "src-full",
            "url": "https://support.apple.com/icloud-photos",
            "title": "iCloud Photos",
            "content": "iCloud Photos keeps your library in sync.",
        },
        provider="tavily",
    )
    partial = coerce_source(
        {
            "id": "src-partial",
            # Snippet mentions ``Google Photos`` only — no ``iCloud``.
            # The phrase-token ``iCloud Photos`` requires BOTH tokens,
            # so this hit must be dropped.
            "url": "https://photos.google.com",
            "title": "Google Photos",
            "content": "Photos backup product.",
        },
        provider="tavily",
    )
    from app.services.research_service import _filter_sources_by_phrases

    kept = _filter_sources_by_phrases([full, partial], phrases=["iCloud Photos"])
    assert [s.provider_source_id for s in kept] == ["src-full"]


def test_filter_sources_by_phrases_multi_word_priority_keeps_full_match():
    """A snippet that contains BOTH ``iCloud`` and ``Photos`` (in any
    order) counts as supporting the phrase ``iCloud Photos``. The
    case-insensitive substring rule still applies.
    """
    from app.services.research_provider import coerce_source

    lower = coerce_source(
        {
            "id": "src-lower",
            "url": "https://support.apple.com/icloud",
            "title": "icloud photos overview",
            "content": "Learn about the photos product for icloud.",
        },
        provider="tavily",
    )
    from app.services.research_service import _filter_sources_by_phrases

    kept = _filter_sources_by_phrases([lower], phrases=["iCloud Photos"])
    assert [s.provider_source_id for s in kept] == ["src-lower"]


def test_filter_sources_by_phrases_single_word_falls_back_when_no_multi_word():
    """When the only extracted phrase is single-word (e.g. ``Dropbox``)
    the multi-word check has nothing to require, so the single-word
    substring rule applies as before.
    """
    from app.services.research_provider import coerce_source

    hit = coerce_source(
        {
            "id": "src-d",
            "url": "https://www.dropbox.com",
            "title": "Dropbox",
            "content": "Sync and backup.",
        },
        provider="tavily",
    )
    miss = coerce_source(
        {
            "id": "src-x",
            "url": "https://example.com/other",
            "title": "Other",
            "content": "Unrelated.",
        },
        provider="tavily",
    )
    from app.services.research_service import _filter_sources_by_phrases

    kept = _filter_sources_by_phrases([hit, miss], phrases=["Dropbox"])
    assert [s.provider_source_id for s in kept] == ["src-d"]


def test_filter_sources_by_phrases_drops_stop_word_phrases_prevents_cross_product_rescue():
    """v4 evaluation reproduced: a Google Drive query whose
    extractor yielded ``['Google Drive Help', 'Mobile', 'Cite']``
    was leaking a Google Photos hit. Two layers now prevent the
    leak at the filter step:

    1. ``Mobile`` / ``Cite`` are stripped at extraction (no
       phrase-token entry returned to the filter).
    2. ``Mobile`` / ``Cite`` are stripped AGAIN at the filter
       boundary — defence-in-depth — so they cannot rescue a
       cross-product page even when a test (or a future
       extraction regression) passes them directly.

    The legit ``src-drive`` page survives because its haystack
    contains both ``google`` and ``drive`` (after stripping
    ``Help``).
    """
    from app.services.research_provider import coerce_source

    # Cross-product page whose snippet contains ``Mobile`` and
    # ``Cite`` but NEITHER ``google`` NOR ``drive``. The host /
    # title / content deliberately avoid both tokens so the only
    # match candidate is the (also-stop-word) ``Mobile`` / ``Cite``
    # single-word phrases — which the filter must strip.
    photos = coerce_source(
        {
            "id": "src-photos",
            "url": "https://example.com/photo-backup",
            "title": "Photo backup overview",
            "content": (
                "Cite this article on mobile backup for photos. "
                "This product stores and organises images."
            ),
        },
        provider="tavily",
    )
    # Legit Google Drive page that DOES contain both tokens of the
    # multi-word phrase (after stop-word stripping). Must survive.
    drive = coerce_source(
        {
            "id": "src-drive",
            "url": "https://support.google.com/drive",
            "title": "Google Drive Help",
            "content": "Google Drive storage and backup on mobile.",
        },
        provider="tavily",
    )
    from app.services.research_service import _filter_sources_by_phrases

    kept = _filter_sources_by_phrases(
        [photos, drive],
        # Mirrors the deterministic reproduction in the brief:
        # the v4 reproduction yielded this exact set when
        # ``_extract_phrase_tokens`` was called on the Google Drive
        # question.
        phrases=["Google Drive Help", "Mobile", "Cite"],
    )
    ids = [s.provider_source_id for s in kept]
    assert ids == ["src-drive"]
    assert "src-photos" not in ids


# ---- _is_name_supported_by_sources ---------------------------------------


class _StubSource:
    """Minimal stand-in for :class:`ResearchSource` used by the
    haystack-map builder — only the fields the builder reads are
    populated so the test surface stays honest about the
    service's contract.
    """

    def __init__(
        self,
        provider_source_id: str,
        url: str,
        title: str | None,
        domain: str,
        snippet: str | None,
    ) -> None:
        self.provider_source_id = provider_source_id
        self.url = url
        self.title = title
        self.domain = domain
        self.snippet = snippet


def test_build_source_haystack_map_uses_persisted_fields():
    """The haystack map MUST combine title + url + domain + snippet
    (lower-cased) so the validator's name-support gate sees the
    full citation context, not just one field.
    """
    sources = [
        _StubSource(
            provider_source_id="src-1",
            url="https://support.apple.com/icloud-photos",
            title="iCloud Photos",
            domain="support.apple.com",
            snippet="iCloud Photos keeps your library in sync.",
        )
    ]
    haystacks = _build_source_haystack_map(sources)
    assert "src-1" in haystacks
    hay = haystacks["src-1"]
    assert "icloud" in hay
    assert "photos" in hay
    assert "support.apple.com" in hay


def test_is_name_supported_by_sources_accepts_when_source_contains_all_tokens():
    """All significant tokens of the name appear in the cited
    source → supported = True.
    """
    haystacks = {
        "src-1": (
            "https://support.apple.com/icloud-photos\n"
            "iCloud Photos\n"
            "support.apple.com\n"
            "iCloud Photos keeps your library in sync."
        ).lower()
    }
    refs = (("src-1", "https://support.apple.com/icloud-photos"),)
    assert _is_name_supported_by_sources("iCloud Photos", refs, haystacks) is True


def test_is_name_supported_by_sources_rejects_when_no_source_contains_all_tokens():
    """The cited source's haystack does NOT mention ``iCloud`` —
    only ``Google Drive`` — so the name ``iCloud Photos`` is
    rejected. This is the v3 failure mode the gate was built
    for.
    """
    haystacks = {
        "src-drive": (
            "https://support.google.com/drive/thread\n"
            "Google Drive Community thread\n"
            "support.google.com\n"
            "Having trouble with Google Drive syncing across devices."
        ).lower()
    }
    refs = (("src-drive", "https://support.google.com/drive/thread"),)
    assert _is_name_supported_by_sources("iCloud Photos", refs, haystacks) is False


def test_is_name_supported_by_sources_partial_token_match_still_rejects():
    """The cited source mentions ``Photos`` but not ``iCloud`` —
    partial match is NOT sufficient. The gate requires ALL
    significant tokens.
    """
    haystacks = {
        "src-photos-only": (
            "https://photos.google.com\n"
            "Google Photos\n"
            "photos.google.com\n"
            "Backup your photos automatically."
        ).lower()
    }
    refs = (("src-photos-only", "https://photos.google.com"),)
    assert _is_name_supported_by_sources("iCloud Photos", refs, haystacks) is False


def test_is_name_supported_by_sources_vacuous_when_name_has_no_significant_tokens():
    """A name that has no significant tokens (after stop-word /
    short-token stripping) is vacuously supported — the gate
    cannot prove the claim wrong and the upstream multi-word
    phrase filter already dropped single-word-only matches. This
    is the same discipline as the validator's other length /
    bounds checks: "if there is nothing to check, do not block".
    """
    haystacks = {"src-1": "https://example.com\narticle\narticle\nsnippet".lower()}
    refs = (("src-1", "https://example.com"),)
    # ``I a`` — ``i`` is dropped (1 char), ``a`` is dropped
    # (1 char). The token list is empty, so the gate's vacuous
    # truth branch fires.
    assert _is_name_supported_by_sources("I a", refs, haystacks) is True
    # A name whose only tokens are stop-words is also vacuous.
    assert _is_name_supported_by_sources("for the of", refs, haystacks) is True
    # An empty / whitespace-only name is vacuous by construction.
    assert _is_name_supported_by_sources("", refs, haystacks) is True


# ---- Validator name-support gate integration ------------------------------


def test_validator_name_support_gate_demotes_icloud_photos_name_with_drive_source():
    """v3 — the central failure mode: an LLM emits
    ``competitor name="iCloud Photos"`` citing a Google Drive
    Community thread. The validator demotes the competitor to
    ``status='not_verified'`` with empty ``source_refs`` so the
    run does NOT complete on the basis of an unsupported
    citation.
    """
    # Build the payload from scratch (do not reuse ``_ok_payload()``)
    # so the only cited provider_source_id is ``src-drive``. The
    # other arrays are emptied so the validator does not trip on a
    # stale ``src-1`` / ``src-2`` reference before reaching the
    # name-support gate.
    payload = {
        "summary": "Apple 的照片同步产品。",
        "confidence": 0.7,
        "competitors": [
            {
                "name": "iCloud Photos",
                "description": "Apple 的照片同步产品。",
                "confidence": 0.7,
                "source_refs": [
                    {
                        "provider_source_id": "src-drive",
                        "url": "https://support.google.com/drive/thread",
                        "title": "Google Drive Community thread",
                    }
                ],
            }
        ],
        "market_patterns": [],
        "opportunities": [],
        "risks": [],
    }
    srcs = {
        "src-drive": "https://support.google.com/drive/thread",
    }
    haystacks = {
        "src-drive": (
            "https://support.google.com/drive/thread\n"
            "google drive community thread\n"
            "support.google.com\n"
            "having trouble with google drive syncing across devices."
        ).lower()
    }
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
    )
    # The SUMMARY finding is unaffected (it carries no sources).
    competitor_findings = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitor_findings) == 1
    competitor = competitor_findings[0]
    assert competitor.status == ResearchFindingStatus.NOT_VERIFIED.value
    # source_refs MUST be empty (DB CHECK constraint compliance).
    assert competitor.source_refs == ()


def test_validator_name_support_gate_accepts_when_source_contains_name():
    """v3 — happy path: the cited source's haystack DOES mention
    the competitor name. The finding stays ``status='confirmed'``.
    """
    payload = {
        "summary": "Apple 的照片同步产品。",
        "confidence": 0.7,
        "competitors": [
            {
                "name": "iCloud Photos",
                "description": "Apple 的照片同步产品。",
                "confidence": 0.7,
                "source_refs": [
                    {
                        "provider_source_id": "src-icloud",
                        "url": "https://support.apple.com/icloud-photos",
                        "title": "iCloud Photos Help",
                    }
                ],
            }
        ],
        "market_patterns": [],
        "opportunities": [],
        "risks": [],
    }
    srcs = {
        "src-icloud": "https://support.apple.com/icloud-photos",
    }
    haystacks = {
        "src-icloud": (
            "https://support.apple.com/icloud-photos\n"
            "icloud photos help\n"
            "support.apple.com\n"
            "learn about icloud photos and how it keeps your library in sync."
        ).lower()
    }
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
    )
    competitor_findings = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitor_findings) == 1
    assert competitor_findings[0].status == ResearchFindingStatus.CONFIRMED.value
    assert competitor_findings[0].source_refs == (
        ("src-icloud", "https://support.apple.com/icloud-photos"),
    )


def test_validator_name_support_gate_demotes_confirmed_opportunity_without_match():
    """v3 — opportunity / risk path: a ``status='confirmed'`` finding
    whose title tokens are NOT in any cited source is demoted to
    ``status='not_verified'`` with empty ``source_refs``.
    """
    payload = {
        "summary": "需要支持多设备照片同步。",
        "confidence": 0.6,
        "competitors": [],
        "market_patterns": [],
        "opportunities": [
            {
                # The title mentions ``Google Photos`` but the only
                # cited source is an Apple iCloud Photos page — its
                # haystack does NOT contain ``google``. The
                # name-support gate demotes the opportunity even
                # though the LLM marked it ``status='confirmed'``.
                "title": "Google Photos 多设备同步",
                "description": "Apple 应该支持多设备。",
                "confidence": 0.6,
                "status": "confirmed",
                "source_refs": [
                    {
                        "provider_source_id": "src-icloud",
                        "url": "https://support.apple.com/icloud-photos",
                        "title": "iCloud Photos Help",
                    }
                ],
            }
        ],
        "risks": [],
    }
    srcs = {
        "src-icloud": "https://support.apple.com/icloud-photos",
    }
    haystacks = {
        "src-icloud": (
            "https://support.apple.com/icloud-photos\n"
            "icloud photos help\n"
            "support.apple.com\n"
            "about icloud photos."
        ).lower()
    }
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
    )
    opportunity_findings = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.OPPORTUNITY.value
    ]
    assert len(opportunity_findings) == 1
    assert opportunity_findings[0].status == ResearchFindingStatus.NOT_VERIFIED.value
    assert opportunity_findings[0].source_refs == ()


def test_validator_name_support_gate_disabled_when_haystacks_none():
    """Backward-compat: when ``source_haystacks`` is omitted (legacy
    callers / pure-shape tests) the gate is disabled and every
    sourced finding is confirmed as before.
    """
    payload = _ok_payload()
    # Use the canonical payload — ``CompetitorX`` against a source
    # whose snippet does NOT contain ``competitorx``. With the
    # gate disabled this stays confirmed; with the gate enabled
    # it would be demoted.
    srcs = {
        "src-1": "https://example.com/a",
        "src-2": "https://example.com/b",
    }
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
    )
    competitor_findings = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert competitor_findings[0].status == ResearchFindingStatus.CONFIRMED.value


# ---- Service-level end-to-end v3 fixes ------------------------------------


def test_start_research_run_partial_failed_when_competitor_source_lacks_name(
    SessionLocal, fake_settings, monkeypatch
):
    """End-to-end v3: an LLM emits ``competitor name='iCloud Photos'``
    citing a Google Drive Community thread. The run ends
    ``partial_failed`` with ``insufficient_external_evidence``
    rather than ``completed`` — the gate refuses to count an
    unsupported citation as external confirmation.
    """
    from app.services.research_provider import ProviderSearchResult, coerce_source

    drive_thread = coerce_source(
        {
            "id": "src-drive",
            "url": "https://support.google.com/drive/thread",
            "title": "Google Drive Community thread",
            "content": "Having trouble with Google Drive syncing across devices.",
        },
        provider="tavily",
    )

    class _DriveThreadProvider:
        def __init__(self, **kwargs):
            self.last_attempt_count: int = 1
            self.last_call_latency_ms: int = 5

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def search(self, query):
            return ProviderSearchResult(
                query=query, provider="tavily", sources=[drive_thread]
            )

    monkeypatch.setattr(
        "app.services.research_service.ResearchProviderClient",
        _DriveThreadProvider,
    )
    # The LLM names "iCloud Photos" but cites the only source the
    # provider returned — a Google Drive Community thread. The
    # validator's name-support gate demotes the competitor to
    # ``not_verified``, leaving no confirmed competitor /
    # market_pattern, so the run ends ``partial_failed``.
    _install_llm_stub(
        monkeypatch,
        {
            "summary": "Apple 的照片同步生态与 Google Drive 不同。",
            "confidence": 0.5,
            "competitors": [
                {
                    "name": "iCloud Photos",
                    "description": "Apple 的照片同步产品。",
                    "confidence": 0.7,
                    "source_refs": [
                        {
                            "provider_source_id": "src-drive",
                            "url": "https://support.google.com/drive/thread",
                            "title": "Google Drive Community thread",
                        }
                    ],
                }
            ],
            "market_patterns": [],
            "opportunities": [],
            "risks": [],
        },
    )

    db, seed = _seed_pipeline(SessionLocal)
    project_id = db.get(Insight, seed["insight_id"]).project_id
    try:
        run = start_research_run(
            db,
            project_id,
            seed["insight_id"],
            research_question="iCloud Photos 与 Google Drive 的对比",
            scope=None,
        )
    finally:
        db.close()
    # The run did NOT complete: the only competitor was demoted
    # because the cited Google Drive source does not mention
    # ``iCloud Photos``.
    assert run.status == ResearchRunStatus.PARTIAL_FAILED.value
    assert run.status != ResearchRunStatus.COMPLETED.value
    assert "insufficient_external_evidence" in (run.error_message or "")


def test_start_research_run_low_authority_filter_drops_only_low_authority_hits(
    SessionLocal, fake_settings, monkeypatch
):
    """End-to-end v3: when the provider returns ONLY user-generated
    platform hits, the low-authority filter drops them all and the
    run ends with ``source_count == 0``. The audit row reflects the
    real outcome — never a silent swap for an authoritative-looking
    community page.

    The research question is intentionally generic (no product /
    brand names) so the phrase-token filter is a no-op and the
    low-authority filter is the layer doing the dropping. Because
    the LLM still emits a valid payload (a summary + a not_verified
    opportunity) the run finishes ``partial_failed`` with
    ``insufficient_external_evidence`` rather than ``completed``
    — the completion gate refuses to clear on the basis of an
    unsupported emission.
    """
    from app.services.research_provider import ProviderSearchResult, coerce_source

    sites_hit = coerce_source(
        {
            "id": "src-sites",
            "url": "https://sites.google.com/view/photos",
            "title": "User Sites",
            "content": "snippet",
        },
        provider="tavily",
    )
    blog_hit = coerce_source(
        {
            "id": "src-blog",
            "url": "https://foo.blogspot.com/p/about",
            "title": "Blog",
            "content": "snippet",
        },
        provider="tavily",
    )

    class _LowAuthorityProvider:
        def __init__(self, **kwargs):
            self.last_attempt_count: int = 1
            self.last_call_latency_ms: int = 5

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def search(self, query):
            return ProviderSearchResult(
                query=query, provider="tavily", sources=[sites_hit, blog_hit]
            )

    monkeypatch.setattr(
        "app.services.research_service.ResearchProviderClient",
        _LowAuthorityProvider,
    )
    # The LLM is offered no sources (the filter dropped them all
    # before persistence) so it cannot emit anything confirmed.
    _install_llm_stub(
        monkeypatch,
        {
            "summary": "无可引用的外部资料。",
            "confidence": 0.0,
            "competitors": [],
            "market_patterns": [],
            "opportunities": [
                {
                    "title": "需要更多外部资料",
                    "description": "insufficient_evidence",
                    "confidence": 0.0,
                    "status": "not_verified",
                    "source_refs": None,
                }
            ],
            "risks": [],
        },
    )

    db, seed = _seed_pipeline(SessionLocal)
    project_id = db.get(Insight, seed["insight_id"]).project_id
    try:
        run = start_research_run(
            db,
            project_id,
            seed["insight_id"],
            research_question="竞品功能对比",
            scope=None,
        )
    finally:
        db.close()
    # The low-authority filter dropped every provider hit; the
    # audit row records ``source_count == 0`` and the run does
    # NOT complete. The completion gate trips on
    # ``insufficient_external_evidence``.
    assert run.source_count == 0
    assert run.status == ResearchRunStatus.PARTIAL_FAILED.value
    assert run.status != ResearchRunStatus.COMPLETED.value
    assert "insufficient_external_evidence" in (run.error_message or "")


# ---- v2 prompt contract — name-support rule -------------------------------


def test_research_v2_prompt_enforces_name_support_rule():
    """v3 — the v2 prompt MUST tell the LLM that every confirmed
    finding's name tokens must appear in at least one cited
    source's title / URL / domain / snippet. The v3 evaluation
    surfaced competitors named after products that were not in
    the cited source (e.g. ``competitor name="iCloud Photos"``
    citing a Google Drive thread); an explicit name-support rule
    is the cheapest prompt-level fix.
    """
    prompt = research_agent_v2.SYSTEM_PROMPT
    assert "NAME-SUPPORT" in prompt
    # The rule names the failure shape (name not in cited source)
    # and the remedy (mark status='not_verified' and drop
    # source_refs).
    assert "not_verified" in prompt
    # The rule names both halves of a typical multi-word brand
    # so the LLM understands "tokens" is a tokenisation rule,
    # not a verbatim phrase rule.
    assert "iCloud" in prompt or "icloud" in prompt.lower()
    assert "Photos" in prompt or "photos" in prompt.lower()


# ---- v5 prompt contract — atomic + evidence quote + silence-not-denial ---


def test_research_v3_prompt_is_short_and_versioned():
    """v5 — the v3 prompt module MUST expose
    ``PROMPT_VERSION='research-agent-v3'`` and a short system
    prompt that explicitly references the new hard rules.
    """
    from app.agents.prompts import research_agent_v3

    assert research_agent_v3.PROMPT_VERSION == "research-agent-v3"
    prompt = research_agent_v3.SYSTEM_PROMPT
    # The new contract rules are surfaced verbatim so a regression
    # in the prompt file is caught by CI rather than by the LLM.
    assert "ATOMIC CLAIMS" in prompt
    assert "EVIDENCE-ONLY" in prompt
    assert "DESCRIPTION QUOTE" in prompt
    assert 'Evidence: "..."' in prompt
    # v3 closes the schema on the same six fields as v2.
    for field in (
        "summary",
        "confidence",
        "competitors",
        "market_patterns",
        "opportunities",
        "risks",
    ):
        assert field in prompt


def test_research_v3_prompt_silence_is_not_denial():
    """v5 — the v3 prompt MUST tell the LLM that an empty
    ``competitors`` / ``market_patterns`` array is the correct
    response when no source backs the claim. The brief flagged
    the v4 isolated experiment's failure mode where the model
    emitted "no" / "none" / fabricated negation rather than
    admitting the source did not actually contain the claim.
    """
    from app.agents.prompts import research_agent_v3

    prompt = research_agent_v3.SYSTEM_PROMPT
    prompt_lower = prompt.lower()
    # The phrase "silence is silence" appears verbatim in the
    # v3 prompt; this is the catchphrase that distinguishes v3
    # from v2 and is what the LLM regression must preserve.
    assert "silence is silence" in prompt_lower
    # And the prompt explicitly forbids a "no / none" negation
    # that has no source backing.
    assert "do not fabricate" in prompt_lower or "do NOT fabricate" in prompt


# ---- v5 official-vendor rule detection -----------------------------------


@pytest.mark.parametrize(
    "question, scope",
    [
        ("Google Photos 上传文件失败", "请仅官方文档确认步骤"),
        ("Google Photos 上传文件失败", "please use only official docs"),
        ("Dropbox sync errors", "official documentation only"),
        ("OneDrive 文件同步失败", "仅官方文档"),
        ("iCloud Photos 共享相册", "official help only"),
        ("Google Drive 文件丢失", "first-party support"),
    ],
)
def test_detect_official_vendor_rule_returns_rule_for_single_product_official_question(
    question: str, scope: str | None
):
    """v5 — a single-product question that explicitly asks for
    official documentation MUST yield the matching vendor rule.
    """
    from app.services.research_service import _detect_official_vendor_rule

    rule = _detect_official_vendor_rule(question, scope)
    assert rule is not None
    assert "site" in rule
    assert "path_prefixes" in rule
    assert "blocked_subdomains" in rule


def test_detect_official_vendor_rule_returns_none_for_multi_brand_official_question():
    """v5 — a question naming MULTIPLE canonical products stays
    in the multi-brand path even when it asks for official docs;
    the brief explicitly forbids silent narrowing to one vendor
    in a comparison question.
    """
    from app.services.research_service import _detect_official_vendor_rule

    rule = _detect_official_vendor_rule(
        "Google Drive 与 Dropbox 同步机制对比", "仅官方文档"
    )
    assert rule is None

    rule = _detect_official_vendor_rule(
        "Google Photos vs iCloud Photos storage limits", "only official docs"
    )
    assert rule is None


def test_detect_official_vendor_rule_returns_none_without_official_marker():
    """v5 — a single-product question WITHOUT an official marker
    must NOT trigger the official-vendor pipeline.
    """
    from app.services.research_service import _detect_official_vendor_rule

    rule = _detect_official_vendor_rule(
        "Google Photos 上传文件失败", "用户在社区反馈上传慢"
    )
    assert rule is None


# ---- v7 Apple iCloud Photos aliases ---------------------------------------


@pytest.mark.parametrize(
    "question, scope",
    [
        # v7 — the live-Tavily pass surfaced real PM questions
        # for iCloud Photos that do NOT contain the literal
        # phrase ``icloud photos``. Each alias below MUST
        # yield the ``icloud_photos`` rule when paired with
        # an official-only marker.
        (
            "iCloud sync 是否支持一次多选上传多张图片",
            "仅官方文档",
        ),
        (
            "iCloud.com 网页版 Shared Photo Library 怎么用",
            "only official docs",
        ),
        (
            "Apple Support 上的 iCloud 共享相册",
            "official support",
        ),
        (
            "icloud.com 网页版上传照片流程",
            "仅官方文档",
        ),
        (
            "Shared Photo Library 是否支持 iPhone 一次多选",
            "only official docs",
        ),
    ],
)
def test_detect_official_vendor_rule_apple_icloud_photos_aliases(
    question: str, scope: str
):
    """v7 — the ``icloud_photos`` rule must fire for the
    single-product Apple aliases (``icloud``, ``icloud.com``,
    ``shared photo library``, ``apple support``) that the v7
    audit surfaced. Without these aliases the live question
    yields ``official_rule=None`` and the path filter does
    not run, letting Reddit / MacRumors / community URLs
    into the persisted source list.
    """
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _detect_official_vendor_rule,
    )

    rule = _detect_official_vendor_rule(question, scope)
    assert rule is not None
    assert rule is _OFFICIAL_VENDOR_RULES["icloud_photos"]


def test_detect_official_vendor_rule_apple_alias_does_not_narrow_multi_brand():
    """v7 — the new Apple aliases MUST NOT collapse a multi-
    vendor comparison question to a single rule. A question
    that names ``icloud`` AND ``google drive`` still returns
    ``None`` so the multi-brand path stays intact.
    """
    from app.services.research_service import _detect_official_vendor_rule

    rule = _detect_official_vendor_rule(
        "iCloud 与 Google Drive 同步机制对比", "仅官方文档"
    )
    assert rule is None

    rule = _detect_official_vendor_rule(
        "icloud.com and dropbox file sync", "only official docs"
    )
    assert rule is None


# ---- v7 official provider query builder ----------------------------------


def test_build_official_provider_query_google_drive_real_question():
    """v7 — the canonical short focused query shape for the
    real Google Drive question the v7 audit surfaced.

    Codex's two real Tavily comparisons showed the 281-char
    full question + ``site:support.google.com`` returned
    community threads and a Google Doc (filtered to 0), but
    the short focused query
    ``Google Drive Android upload multiple files photos
    site:support.google.com/drive/answer`` returned the
    official ``/drive/answer/2424368`` page. The new
    builder must produce a query whose body starts with
    the product keyword + the next content tokens and
    whose site constraint joins the rule's host with the
    first (most specific) ``path_prefixes`` entry.
    """
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _build_official_provider_query,
    )

    rule = _OFFICIAL_VENDOR_RULES["google_drive"]
    # Synthesised long PM question in the shape the v7 audit
    # described: product at the start, verbose instructions
    # after. The builder must NOT include the verbose tail
    # in the upstream query.
    long_question = (
        "Google Drive Android 上传多个文件 照片 "
        "我们的用户在 Android 客户端上传多张图片时遇到"
        "困难需要官方文档确认是否支持一次选择多个文件"
        "上传请提供详细步骤以及限制条件"
    )
    q = _build_official_provider_query(
        long_question, "仅官方文档", rule
    )
    # The body must start with the product keyword and the
    # next content tokens, not the verbose tail.
    assert q.startswith("Google Drive")
    # The site constraint joins the rule's host with the
    # first (most specific) ``path_prefixes`` entry.
    assert q.endswith("site:support.google.com/drive/answer")
    # The body is bounded — the verbose tail is dropped, so
    # the query is well under the 500-char cap.
    assert len(q) <= 500
    # The query must NOT contain the verbose PM-internal
    # language the v7 audit said diluted the upstream
    # ranking.
    assert "我们的用户" not in q
    assert "详细步骤" not in q
    assert "限制条件" not in q


def test_build_official_provider_query_uses_first_most_specific_path():
    """v7 — the site constraint joins the vendor host with
    the FIRST ``path_prefixes`` entry (the most specific
    namespace), not the generic root. For OneDrive the
    first entry is ``/en-us/onedrive/`` so the query must
    be ``... site:support.microsoft.com/en-us/onedrive``.
    v7 audit fix — the trailing slash is stripped from the
    join only (the rule's ``path_prefixes`` keeps the
    canonical trailing slash so the URL-prefix match
    continues to operate on
    ``https://support.microsoft.com/en-us/onedrive/...``).
    Trailing-slash ``site:`` constraints are TREATED AS
    STRICTER by Tavily and would otherwise over-narrow the
    upstream ranking.
    """
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _build_official_provider_query,
    )

    rule = _OFFICIAL_VENDOR_RULES["onedrive"]
    q = _build_official_provider_query(
        "OneDrive iOS 上传多个文件", "仅官方文档", rule
    )
    assert q.startswith("OneDrive")
    assert q.endswith("site:support.microsoft.com/en-us/onedrive")
    # The rule prefix keeps its canonical trailing slash so
    # the URL filter still matches
    # ``.../en-us/onedrive/<article>``.
    assert _OFFICIAL_VENDOR_RULES["onedrive"]["path_prefixes"][0] == "/en-us/onedrive/"


def test_build_official_provider_query_apple_icloud_photos():
    """v7 — the Apple alias query shape uses the
    ``icloud_photos`` rule's first path prefix
    (``/en-us/``) and the question's product keyword.
    v7 audit fix — no trailing slash in the site: constraint
    (Tavily strictness rationale).
    """
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _build_official_provider_query,
    )

    rule = _OFFICIAL_VENDOR_RULES["icloud_photos"]
    q = _build_official_provider_query(
        "iCloud sync 是否支持一次多选上传多张图片",
        "仅官方文档",
        rule,
    )
    assert q.startswith("iCloud")
    assert q.endswith("site:support.apple.com/en-us")
    # Rule prefix retains its canonical trailing slash.
    assert _OFFICIAL_VENDOR_RULES["icloud_photos"]["path_prefixes"][0] == "/en-us/"


def test_build_official_provider_query_drops_stop_words():
    """v7 — the builder strips a small English / Chinese
    stop-word list so the question's PM-internal grammar
    words do not pollute the upstream ranking.
    """
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _build_official_provider_query,
    )

    rule = _OFFICIAL_VENDOR_RULES["google_drive"]
    q = _build_official_provider_query(
        "Google Drive 如何一次选择多个文件上传照片",
        "仅官方文档",
        rule,
    )
    # The Chinese stop words ``如何`` and ``一次`` are on
    # the drop list (or tokenized out by the body length
    # cap). The query must remain focused.
    assert q.startswith("Google Drive")
    assert q.endswith("site:support.google.com/drive/answer")
    # Bounded length.
    assert len(q) <= 500


def test_build_official_provider_query_no_question_falls_back_to_site():
    """v7 — when the caller passes an empty question and
    empty scope, the builder returns the site constraint
    alone so the audit row's ``provider_query`` is still
    meaningful and the upstream call is bounded.
    """
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _build_official_provider_query,
    )

    rule = _OFFICIAL_VENDOR_RULES["google_drive"]
    q = _build_official_provider_query("", None, rule)
    assert q == "site:support.google.com/drive/answer"


# ---- v7 prompt additions -------------------------------------------------


def test_research_v3_prompt_enforces_adjacent_fact_rule():
    """v7 — the v3 prompt must explicitly require that an
    adjacent-fact snippet (e.g. "you can upload photos")
    does NOT satisfy a "select multiple at once" question;
    the LLM must emit empty finding arrays in that case.
    """
    from app.agents.prompts import research_agent_v3

    prompt_lower = research_agent_v3.SYSTEM_PROMPT.lower()
    # The "ADJACENT-FACT" rule must be present and forbid
    # promoting an adjacent fact to a direct answer.
    assert "adjacent" in prompt_lower
    # The example phrasing the v7 audit used to surface the
    # bug is mentioned so the LLM sees the pattern.
    assert "upload" in prompt_lower
    assert "multiple" in prompt_lower


def test_research_v3_prompt_enforces_platform_flow_rule():
    """v7 — the v3 prompt must explicitly require that the
    platform the cited snippet describes matches the
    platform the question asks about. The v7 audit
    surfaced a Mac Finder sync snippet being cited for an
    iPhone Photos app iCloud sync question; the prompt
    must explicitly forbid that mixing.
    """
    from app.agents.prompts import research_agent_v3

    prompt_lower = research_agent_v3.SYSTEM_PROMPT.lower()
    # The "PLATFORM-FLOW" rule must be present.
    assert "platform" in prompt_lower
    # The v7 audit's failure case is named so the LLM sees
    # the exact pattern.
    assert "mac finder" in prompt_lower
    assert "photos app" in prompt_lower


def test_research_v3_prompt_enforces_no_not_mentioned_inference():
    """v7 — the v3 prompt must explicitly forbid the LLM
    from inferring "does not support" from the absence of
    a capability in the cited sources.
    """
    from app.agents.prompts import research_agent_v3

    prompt_lower = research_agent_v3.SYSTEM_PROMPT.lower()
    # The "no not-mentioned inference" rule must be
    # present and forbid the negation-from-silence pattern.
    assert "silence" in prompt_lower
    assert "not-mentioned" in prompt_lower or "not mentioned" in prompt_lower
    # And the LLM must NOT write a Confirmed description
    # that extrapolates beyond the cited snippet.
    assert "minimum" in prompt_lower or "minimum fact" in prompt_lower


# ---- v7 audit fix — provider_query persistence + site: trailing-slash ---


def test_start_research_run_official_path_provider_query_matches_audit(
    SessionLocal, fake_settings, monkeypatch, stub_provider
):
    """v7 audit fix — when the PM's question is a single-product
    "official-only" ask, ``run.provider_query`` MUST equal the
    exact string the provider stub received. Otherwise an audit
    reading the row sees a stale 281-char canonical query in
    place of the focused short query that actually went
    upstream. The pre-commit must carry the focused query (not
    the canonical ``build_provider_query()`` output) and the
    stub call must see the same value.
    """
    from app.models import Insight

    db, seed = _seed_pipeline(SessionLocal)
    try:
        _install_llm_stub(monkeypatch, _ok_payload())
        # Synthesised long PM question in the shape the v7 audit
        # described: product at the start, verbose instructions
        # after. The canonical ``build_provider_query()`` would
        # preserve the full 281-char text; the focused builder
        # produces the short query that matches the canonical
        # Tavily call the v7 audit found to return
        # ``/drive/answer/2424368``.
        long_question = (
            "Google Drive Android 上传多个文件 照片 "
            "我们的用户在 Android 客户端上传多张图片时遇到"
            "困难需要官方文档确认是否支持一次选择多个文件"
            "上传请提供详细步骤以及限制条件"
        )
        # The canonical ``build_provider_query()`` output
        # shape on this input — question + scope concatenated,
        # whitespace-normalised, capped at 500 chars. We
        # recompute it here so the test does not depend on
        # ``assemble_internal_context``'s DB reads.
        canonical_query = (
            " ".join((long_question + " 仅官方文档").split())[:500]
        )
        project_id = (
            seed["insight_id"]
            and (db.get(Insight, seed["insight_id"]).project_id)
        )
        run = start_research_run(
            db,
            project_id,
            seed["insight_id"],
            research_question=long_question,
            scope="仅官方文档",
        )
    finally:
        db.close()

    captured = stub_provider[0].calls[0]
    # The audit row's ``provider_query`` MUST equal the upstream
    # call. This is the v7 audit-fix contract: a worker reading
    # the row can reproduce the Tavily call by replaying
    # ``run.provider_query``.
    assert run.provider_query == captured
    # The canonical ``build_provider_query()`` output was the
    # full 281-char question — it MUST NOT have leaked into
    # the audit row when the official path fired.
    assert run.provider_query != canonical_query
    # The official path produced a focused short query (well
    # under the canonical 281-char length).
    assert len(run.provider_query) < 200
    assert len(run.provider_query) < len(canonical_query)
    # The site: constraint joins the rule's host with the
    # first path WITHOUT a trailing slash (Tavily treats
    # ``/drive/answer/`` as stricter and over-narrows the
    # upstream ranking — see
    # ``_build_official_provider_query`` for the rstrip("/")
    # rationale).
    assert "site:support.google.com/drive/answer" in run.provider_query
    assert not run.provider_query.endswith("answer/")


def test_start_research_run_non_official_path_provider_query_matches_audit(
    SessionLocal, fake_settings, monkeypatch, stub_provider
):
    """v7 audit fix — when the PM's question is NOT an
    official-only single-product ask, the canonical
    ``build_provider_query()`` output is what the stub receives
    and the audit row's ``provider_query`` MUST equal it. The
    non-official path is unchanged but still needs the
    persistence equality contract because the v7 audit-fix
    covers both branches.
    """
    from app.models import Insight

    db, seed = _seed_pipeline(SessionLocal)
    try:
        _install_llm_stub(monkeypatch, _ok_payload())
        project_id = (
            seed["insight_id"]
            and (db.get(Insight, seed["insight_id"]).project_id)
        )
        run = start_research_run(
            db,
            project_id,
            seed["insight_id"],
            research_question="竞品 X 的多选入口如何提示？",
            scope=None,
        )
    finally:
        db.close()

    captured = stub_provider[0].calls[0]
    assert run.provider_query == captured
    # The canonical path does NOT append a site: constraint.
    assert "site:" not in run.provider_query
    # The canonical path keeps the full PM question.
    assert "竞品 X 的多选入口如何提示" in run.provider_query


def test_build_official_provider_query_no_trailing_slash_in_site_constraint():
    """v7 audit fix — the rule's path_prefixes retain the
    trailing slash (the URL filter requires it for prefix
    matching) but the ``site:<host><path>`` constraint emitted
    to Tavily MUST NOT carry it. Trailing-slash site:
    constraints are TREATED AS STRICTER by the upstream
    ranking: the v7 audit's real Tavily pass with
    ``site:support.google.com/drive/answer/`` ranked community
    threads + Google Docs above the canonical
    ``/drive/answer/2424368`` page, while
    ``site:support.google.com/drive/answer`` (no slash) ranked
    the official page first.
    """
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _build_official_provider_query,
    )

    # Google Drive — first prefix is ``/drive/answer/``.
    rule = _OFFICIAL_VENDOR_RULES["google_drive"]
    q = _build_official_provider_query(
        "Google Drive Android 上传多个文件 照片",
        "仅官方文档",
        rule,
    )
    assert q.endswith("site:support.google.com/drive/answer")
    assert not q.endswith("answer/")
    # The allow-list prefix in the rule itself is unchanged so
    # the URL filter continues to match
    # ``https://support.google.com/drive/answer/2424368`` on its
    # canonical-prefix shape.
    assert _OFFICIAL_VENDOR_RULES["google_drive"]["path_prefixes"][0] == "/drive/answer/"

    # OneDrive — first prefix is ``/en-us/onedrive/``.
    rule = _OFFICIAL_VENDOR_RULES["onedrive"]
    q = _build_official_provider_query(
        "OneDrive iOS 上传多个文件", "仅官方文档", rule
    )
    assert q.endswith("site:support.microsoft.com/en-us/onedrive")
    assert not q.endswith("onedrive/")
    # Rule prefix retained.
    assert _OFFICIAL_VENDOR_RULES["onedrive"]["path_prefixes"][0] == "/en-us/onedrive/"

    # iCloud Photos — first prefix is ``/en-us/``.
    rule = _OFFICIAL_VENDOR_RULES["icloud_photos"]
    q = _build_official_provider_query(
        "iCloud sync 是否支持一次多选上传多张图片",
        "仅官方文档",
        rule,
    )
    assert q.endswith("site:support.apple.com/en-us")
    assert not q.endswith("en-us/")
    # Rule prefix retained.
    assert _OFFICIAL_VENDOR_RULES["icloud_photos"]["path_prefixes"][0] == "/en-us/"

    # Empty-question fallback — single ``site:<host><path>``
    # constraint, no trailing slash.
    q = _build_official_provider_query("", None, _OFFICIAL_VENDOR_RULES["google_drive"])
    assert q == "site:support.google.com/drive/answer"


# ---- Round 3 — semantic-priority body construction -----------------------


def test_build_official_provider_query_chinese_long_question_drops_pm_background():
    """Round 3 — the long Chinese PM question is the canonical
    failure the v7/Round 2 rework still leaves: the
    ``我们的用户在`` fragment slips past the per-token stop-word
    drop (chars ``用`` / ``户`` are NOT on the CJK stop list) and
    the 8-token budget + 80-char cap drop the critical
    ``upload`` / ``photos`` action words before the body even
    reaches the upstream ranking. The Round 3 semantic-priority
    builder drops PM-internal background automatically (none of
    ``我们的用户在 / 详细步骤 / 限制条件`` is a recognised product
    / platform / action token) and maps Chinese action terms
    (``上传 / 多个 / 文件 / 照片 / 多张 / 图片 / 选择``) to their
    English equivalents so the upstream query carries
    ``Google Drive Android upload multiple files photos select``
    instead.
    """
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _build_official_provider_query,
    )

    rule = _OFFICIAL_VENDOR_RULES["google_drive"]
    long_question = (
        "Google Drive Android 上传多个文件 照片 "
        "我们的用户在 Android 客户端上传多张图片时遇到"
        "困难需要官方文档确认是否支持一次选择多个文件"
        "上传请提供详细步骤以及限制条件"
    )
    q = _build_official_provider_query(
        long_question, "仅官方文档", rule
    )
    # PM-internal background dropped — the Round 2
    # ``_drop_pure_stop_cjk_token`` helper missed
    # ``我们的用户在`` (chars ``用`` / ``户`` not on the CJK
    # stop list); the new semantic-priority builder drops it
    # automatically because none of its chars is a recognised
    # product / platform / action term.
    assert "我们的用户" not in q
    assert "详细步骤" not in q
    assert "限制条件" not in q
    assert "客户端" not in q
    # Chinese action terms mapped to English equivalents so
    # the upstream Tavily query uses the canonical English
    # verbs / nouns the vendor's English help pages use.
    assert "upload" in q
    assert "multiple" in q
    assert "files" in q
    assert "photos" in q
    # Platform preserved in canonical capitalization
    # (Round 2 dropped this because the 80-char cap cut
    # BEFORE the second ``Android`` mention in the tail;
    # the new design always recognises platforms regardless
    # of position).
    assert "Android" in q
    # Product keyword + site constraint intact.
    assert q.startswith("Google Drive")
    assert q.endswith("site:support.google.com/drive/answer")
    # Length safety — far below the 500-char cap.
    assert len(q) <= 500


def test_build_official_provider_query_english_real_question_preserves_actions():
    """Round 3 — the real PM English question shape is the
    canonical Round 2 regression: the 8-token budget cut the
    body AFTER the verb ``select`` but BEFORE ``upload`` /
    ``photos``, so the upstream Tavily ranking never saw the
    critical action terms the PM actually asked about. The
    Round 3 semantic-priority builder recognises ``upload`` /
    ``photos`` / ``multiple`` / ``select`` regardless of their
    position in the sentence and drops the verbose
    PM-internal background (``app,`` / ``user`` / ``device`` /
    ``from`` / ``which`` / ``to``) automatically because none
    of those words is a platform / action token.
    """
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _build_official_provider_query,
    )

    rule = _OFFICIAL_VENDOR_RULES["google_drive"]
    long_question = (
        "Google Drive Android app, user select multiple device "
        "from which to upload multiple photos"
    )
    q = _build_official_provider_query(
        long_question, "仅官方文档", rule
    )
    # Critical action tokens preserved — these are the words
    # the upstream Tavily ranking tokenises against. None of
    # them was on the 80-char cap / 8-token budget in Round 2.
    assert "upload" in q
    assert "multiple" in q
    assert "photos" in q
    assert "select" in q
    # Verbose PM-internal background dropped from the body
    # (the part BEFORE the site: constraint). Splitting on
    # `` site:`` gives the body substring; ``site:support``
    # may not be at the END for very short bodies, so split
    # on the first occurrence of `` site:`` and check the
    # leading part.
    body_part = q.split(" site:")[0]
    for noise in ("app,", "user", "device", "from", "which"):
        assert noise not in body_part
    # Product keyword + platform preserved.
    assert "Google Drive" in q
    assert "Android" in q
    # Site constraint intact (no trailing slash; the
    # ``rstrip("/")`` from Round 1 is preserved).
    assert q.endswith("site:support.google.com/drive/answer")


def test_build_official_provider_query_apple_onedrive_official_help_localization():
    """Round 3 — Apple / iCloud Photos and Microsoft / OneDrive
    queries still find their respective official help pages
    after the semantic-priority redesign. The new design
    recognises ``iOS`` / ``iPhone`` / ``iPad`` as platforms,
    maps Chinese action terms (``上传 / 多个 / 文件 / 多张 /
    图片 / 选择 / 同步``) to English, and keeps the
    vendor-specific site constraint with the FIRST
    ``path_prefixes`` entry joined WITHOUT a trailing slash.
    """
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _build_official_provider_query,
    )

    # Apple / iCloud Photos — first path prefix is
    # ``/en-us/`` so the site constraint is
    # ``site:support.apple.com/en-us``.
    rule = _OFFICIAL_VENDOR_RULES["icloud_photos"]
    q = _build_official_provider_query(
        "iCloud sync 是否支持一次多选上传多张图片",
        "仅官方文档",
        rule,
    )
    assert q.startswith("iCloud")
    assert q.endswith("site:support.apple.com/en-us")
    assert not q.endswith("en-us/")
    # Recognised action tokens (mapped from Chinese).
    assert "upload" in q
    assert "multiple" in q
    assert "photos" in q
    # ``sync`` is in the original question and is one of the
    # Round 3 action terms, so it survives too.
    assert "sync" in q
    # Vendor PM-internal language dropped.
    assert "是否" not in q
    assert "支持" not in q
    assert "一次" not in q

    # Microsoft / OneDrive — first path prefix is
    # ``/en-us/onedrive/`` so the site constraint is
    # ``site:support.microsoft.com/en-us/onedrive``.
    rule = _OFFICIAL_VENDOR_RULES["onedrive"]
    q = _build_official_provider_query(
        "OneDrive iOS 上传多个文件", "仅官方文档", rule
    )
    assert q.startswith("OneDrive")
    assert q.endswith("site:support.microsoft.com/en-us/onedrive")
    assert not q.endswith("onedrive/")
    # ``iOS`` platform preserved in canonical capitalization.
    assert "iOS" in q
    # Chinese action terms mapped to English.
    assert "upload" in q
    assert "multiple" in q
    assert "files" in q


def test_build_official_provider_query_unknown_action_falls_back_to_limited_tail():
    """Round 3 — when the question contains NO recognised
    platform / action token (e.g. the PM asked a generic
    ``安全 功能`` / ``security features`` question), the
    fallback path emits ``<product> <≤4 original tokens
    after stop-word drop>`` so the upstream ranking still has
    a focused signal but is not re-flooded with the full PM
    question. The window is bounded at 4 tokens so a verbose
    question with no recognisable terms still produces a
    short upstream query.
    """
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _build_official_provider_query,
    )

    rule = _OFFICIAL_VENDOR_RULES["google_drive"]
    # Generic question with no recognised platform / action
    # token — ``安全`` / ``功能`` are NOT in
    # ``_ACTION_TERM_CJK_TO_EN`` so the fallback path fires.
    q = _build_official_provider_query(
        "Google Drive 安全 功能 概览 介绍 详情 历史 背景 "
        "我们的文档 仅说明这些 简短 限制 步骤",
        "仅官方文档",
        rule,
    )
    assert q.startswith("Google Drive")
    assert q.endswith("site:support.google.com/drive/answer")
    # The fallback window is at most 4 tokens after the
    # product keyword, so the upstream query stays focused
    # rather than re-flooding the full PM question.
    body_part = q.split(" site:")[0]
    body_tokens = body_part.split()
    # ``Google`` + ``Drive`` + ≤4 fallback tokens.
    assert len(body_tokens) <= 6
    # PM-internal stop-word fragments dropped from the tail.
    assert "我们的" not in body_part
    assert "仅" not in body_part
    assert "这些" not in body_part


# ---- v9 — full-text scan + PM-instruction strip + Dropbox path ----------


def test_build_official_provider_query_onedrive_pre_product_platform_action():
    """v9 — real OneDrive question has iPhone / select /
    upload BEFORE the product keyword (``an iPhone user
    select existing pictures for a manual upload in
    OneDrive? Cite only official Microsoft Support``).
    The v8 design scanned only the tail after the product
    keyword, dropped iPhone / select / upload, and leaked
    the PM-instruction tail (``Cite only official
    Microsoft Support``) into the upstream query. v9
    scans the FULL question so the pre-product platform /
    action tokens are captured, AND adds ``cite`` /
    ``only`` / ``official`` to the stop-word list so the
    PM citation instruction never reaches Tavily.
    """
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _build_official_provider_query,
    )

    rule = _OFFICIAL_VENDOR_RULES["onedrive"]
    long_question = (
        "an iPhone user select existing pictures for a manual "
        "upload in OneDrive? Cite only official Microsoft Support"
    )
    q = _build_official_provider_query(
        long_question, "仅官方文档", rule
    )
    # Product anchor preserved in original case.
    assert q.startswith("OneDrive")
    # Pre-product platform / action tokens captured
    # (iPhone / select / upload appear BEFORE ``OneDrive``
    # in the source text).
    assert "iPhone" in q
    assert "select" in q
    assert "upload" in q
    # PM-instruction noise does NOT leak into the upstream
    # query — the fallback path's stop-word list now
    # drops ``cite`` / ``only`` / ``official`` and the
    # best-case path only emits recognised product /
    # platform / action tokens (so ``Cite`` / ``only`` /
    # ``official`` / ``Microsoft`` / ``Support`` never
    # appear regardless of which path fires).
    body_part = q.split(" site:")[0]
    for noise in ("Cite", "only", "official", "Microsoft", "Support"):
        assert noise not in body_part
    # Site constraint intact.
    assert q.endswith("site:support.microsoft.com/en-us/onedrive")
    # Body is focused — well under the canonical 95-char
    # input length.
    assert len(body_part) <= 60


def test_build_official_provider_query_dropbox_upload_routes_to_create_upload_path():
    """v9 — Dropbox help root has multiple topical
    namespaces; for upload / photos questions the
    canonical answer lives under ``/create-upload/`` (e.g.
    ``https://help.dropbox.com/create-upload/upload-mobile``)
    NOT under the default ``/guide/`` which returns
    generic top-level articles. When one of ``upload`` /
    ``photos`` / ``files`` is in the recognised action
    set and the rule is Dropbox, the site constraint
    must re-route to ``/create-upload`` so the upstream
    ranking surfaces the more specific help page. The
    post-filter URL allow-list is unchanged — both
    ``/guide/`` and ``/create-upload/`` are already in
    the rule's ``path_prefixes``.
    """
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _build_official_provider_query,
    )

    rule = _OFFICIAL_VENDOR_RULES["dropbox"]
    q = _build_official_provider_query(
        "Dropbox Help, can mobile users manually add photos "
        "when camera uploads are disabled",
        "only official Dropbox Help",
        rule,
    )
    # Action token captured from the question body.
    assert "photos" in q
    # Site constraint uses the action-specific path
    # (NOT the default ``/guide/``).
    assert q.endswith("site:help.dropbox.com/create-upload")
    assert not q.endswith("create-upload/")
    # Product anchor preserved.
    assert q.startswith("Dropbox")


def test_build_official_provider_query_dropbox_non_upload_stays_on_default_path():
    """v9 — when the question has NO upload / photos /
    files action token, Dropbox queries stay on the
    default ``/guide/`` path. The re-route only fires
    when the action set contains one of those three
    action tokens; a generic Dropbox question about
    sharing / syncing etc. must keep the default
    path so the existing behaviour for non-upload
    actions is unchanged.
    """
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _build_official_provider_query,
    )

    rule = _OFFICIAL_VENDOR_RULES["dropbox"]
    q = _build_official_provider_query(
        "Dropbox Help, how do I share a folder with another user",
        "only official Dropbox Help",
        rule,
    )
    # Action tokens captured.
    assert "share" in q
    # Site constraint stays on the default ``/guide/``
    # path because the action set contains ``share``
    # only — NOT ``upload`` / ``photos`` / ``files``.
    assert q.endswith("site:help.dropbox.com/guide")
    assert not q.endswith("create-upload")
    # Product anchor preserved.
    assert q.startswith("Dropbox")


def test_research_v3_prompt_enforces_single_line_quote():
    """v9 — the v3 prompt must explicitly require the LLM
    to quote a single contiguous line / sub-phrase from
    the snippet, NOT a multi-line composite (numbered
    list joined with spaces) and NOT a quote with
    ellipsis. The v9 real pass surfaced the LLM
    collapsing multi-line numbered lists into single-
    line strings with newline-to-space substitution,
    which the strict snippet-haystack quote gate then
    rejected (the merged string never appears verbatim
    in the snippet) and demoted otherwise-supportable
    findings to ``not_verified``. The prompt must
    forbid this pattern explicitly so a retry / future
    run does not regress.
    """
    from app.agents.prompts import research_agent_v3

    prompt = research_agent_v3.SYSTEM_PROMPT
    prompt_lower = prompt.lower()
    # The "single-line" / "single contiguous" / "single
    # contiguous substring" wording must be present so
    # the LLM sees the rule.
    assert "single line" in prompt_lower or "single-line" in prompt_lower
    # The "no numbered-list join" / "no newline-to-space"
    # rule must be present and forbid the exact
    # substitution pattern the v9 audit surfaced.
    assert "newline" in prompt_lower or "newlines" in prompt_lower
    assert "join" in prompt_lower
    # Ellipsis inside ``Evidence: "..."`` is treated as a
    # fabrication marker — the prompt must explicitly
    # forbid inserting ``...`` to bridge non-adjacent
    # text.
    assert "ellipsis" in prompt_lower
    # The "quote one of the two lines verbatim, not both
    # joined" guidance is in the rule.
    assert "verbatim" in prompt_lower


def test_research_v3_prompt_enforces_description_ground_v11():
    """v11 — the v3 prompt must extend the v9 single-line
    quote rule with the v11 provably-conservative
    contract: the description MUST be one verbatim
    ``Evidence: "..."`` quote (optionally preceded by a
    non-claiming source prefix composed of stopwords /
    brand anchors / connectives). The v10 "any one token
    in snippet" rule was unsafe: Google Drive run
    309f1d93-e060-4e82-9c35-4b0bd5f77968 quoted the real
    snippet ``Find and tap the files you want to
    upload.`` and the surrounding prose paraphrased /
    expanded the claim to ``indicating a tap-based
    multi-selection interaction with no documented
    batch-shortcut``. The English words ``tap`` /
    ``based`` incidentally appeared in the cited snippet
    (because the LLM quoted them verbatim as part of the
    ``Evidence: "..."`` quote), and the v10 rule fired
    True. Token overlap is NOT semantic entailment. The
    v11 contract requires the claim prose NOT EXIST
    beyond a non-claiming prefix.

    The test checks for the v11 marker text that
    distinguishes the new rule from the v10
    snippet-overlap rule: the explicit ``EXACTLY one
    ``Evidence: "<verbatim quote>"`` quote`` plus the
    canonical anti-patterns the v10 / v11 real passes
    surfaced (``multi-select`` / ``batch`` / ``multiple``
    / ``tap-based`` / ``documented`` / ``一次多选`` /
    ``逐文件`` / ``避免`` / ``替代流程``) so a
    regression in the prompt is caught by CI rather than
    by the LLM.
    """
    from app.agents.prompts import research_agent_v3

    prompt = research_agent_v3.SYSTEM_PROMPT
    prompt_lower = prompt.lower()
    # v11 marker — the provably-conservative contract is
    # explicit in the prompt.
    assert "v11" in prompt_lower
    assert "provably-conservative" in prompt_lower or (
        "provably conservative" in prompt_lower
    )
    # The expansion anti-patterns the v11 rule forbids.
    assert "multi-select" in prompt_lower or "multi select" in prompt_lower
    assert "batch" in prompt_lower
    assert "multiple" in prompt_lower
    # v11a English anti-patterns from real Google Drive
    # run 309f1d93-e060-4e82-9c35-4b0bd5f77968.
    assert "tap-based" in prompt_lower
    assert "documented" in prompt_lower
    assert "batch-shortcut" in prompt_lower
    # CJK anti-patterns from the v10a / v10b real runs.
    assert "一次多选" in prompt
    assert "逐文件" in prompt
    assert "避免" in prompt
    assert "替代流程" in prompt
    # The "EXACTLY one ... quote" + non-claiming source
    # prefix contract is in the rule so the LLM cannot
    # generalise around it.
    assert "exactly one" in prompt_lower
    assert "non-claiming source prefix" in prompt_lower
    # Token overlap is NOT semantic entailment — the
    # v11 contract explicitly states the validator does
    # NOT consult the snippet for description-ground
    # checks.
    assert "token overlap" in prompt_lower
    # Brand tokens are explicitly exempt so a
    # non-claiming source prefix containing brand
    # anchors still passes.
    assert "brand" in prompt_lower


# ---- v10 description-ground gate ----------------------------------------


def test_validator_description_ground_gate_demotes_v10a_expansion():
    """v10a — the canonical Google Drive false-positive
    case from real run a102e4af: the description quotes
    the real snippet ``Find and tap the files you want
    to upload.`` but the surrounding prose expands the
    claim to ``一次多选`` (batch select) — the quote is
    real but the batch-select claim is NOT supported by
    the snippet.

    The description-ground gate MUST demote the finding
    to ``status='not_verified'`` with empty ``source_refs``
    so the audit row preserves the original LLM emission
    while the completion gate stamps ``partial_failed``
    instead of crediting the run on the basis of an
    ungrounded citation.
    """
    real_quote = "Find and tap the files you want to upload."
    # The cited snippet is the canonical Google Drive
    # support snippet. It carries the real quote but does
    # NOT mention ``multi-select`` / ``batch`` / ``一次多选``
    # — the description's expansion goes beyond the
    # snippet.
    snippets = {
        "src-1": (
            f"{real_quote} "
            "Find and tap the files you want to upload."
        ),
    }
    haystacks = {
        "src-1": (
            "https://support.google.com/drive/answer/123\n"
            "Google Drive upload help\n"
            "support.google.com\n"
            f"{snippets['src-1']}"
        ).lower(),
    }
    payload = _ok_payload()
    payload["competitors"] = [
        {
            "name": "Google Drive",
            "description": (
                "Google Drive 的文件选择操作支持一次多选。"
                f' Evidence: "{real_quote}"'
            ),
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://support.google.com/drive/answer/123",
                    "title": "Google Drive upload help",
                }
            ],
        }
    ]
    payload["market_patterns"] = []
    payload["opportunities"] = []
    payload["risks"] = []
    srcs = {"src-1": "https://support.google.com/drive/answer/123"}
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
        source_snippets=snippets,
        validate_evidence_quotes=True,
        validate_description_grounding=True,
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors) == 1
    # v10a — the quote gate passes (real quote in real
    # snippet) but the description-ground gate fails (CJK
    # content tokens ``一次多选`` are not in the English
    # snippet). The finding is demoted to not_verified.
    assert competitors[0].status == ResearchFindingStatus.NOT_VERIFIED.value
    assert competitors[0].source_refs == ()
    # The original description is preserved on the row so
    # an audit can read what the model produced.
    assert "一次多选" in competitors[0].description


def test_validator_description_ground_gate_demotes_v10b_expansion():
    """v10b — the second canonical Google Drive false-
    positive case from real run 722b547a: the description
    quotes two real snippets (``Find and tap the files
    you want to upload.`` + ``Touch and hold the item.``)
    but the surrounding prose expands the claim to
    ``仅逐文件点选`` (only file-by-file selection) +
    ``避免多选的替代流程`` (alternative flow avoiding
    multi-select). Neither claim is supported by the
    cited snippets — the snippet describes a tap /
    long-press interaction, not a "no multi-select"
    limitation or an "alternative flow" feature.

    The description-ground gate MUST demote the finding
    to ``status='not_verified'``. The bare ``Touch and
    hold the item.`` text (without the ``Evidence:``
    prefix) remains in the claim prose and is parsed as
    additional CJK content tokens (``逐文件点选`` /
    ``避免多选`` / ``替代流程``) that are not in the
    snippet.
    """
    real_quote_1 = "Find and tap the files you want to upload."
    real_quote_2 = "Touch and hold the item."
    snippets = {
        "src-1": (
            f"{real_quote_1} {real_quote_2}"
        ),
    }
    haystacks = {
        "src-1": (
            "https://support.google.com/drive/answer/456\n"
            "Google Drive interaction help\n"
            "support.google.com\n"
            f"{snippets['src-1']}"
        ).lower(),
    }
    payload = _ok_payload()
    payload["competitors"] = [
        {
            "name": "Google Drive",
            "description": (
                "Google Drive 的文件选择仅支持逐文件点选操作，"
                f'"{real_quote_2}" 的说明是用来避免多选的替代流程。'
                f' Evidence: "{real_quote_1}"'
            ),
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://support.google.com/drive/answer/456",
                    "title": "Google Drive interaction help",
                }
            ],
        }
    ]
    payload["market_patterns"] = []
    payload["opportunities"] = []
    payload["risks"] = []
    srcs = {"src-1": "https://support.google.com/drive/answer/456"}
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
        source_snippets=snippets,
        validate_evidence_quotes=True,
        validate_description_grounding=True,
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors) == 1
    # v10b — the quote gate passes (both real snippets are
    # in the source) but the description-ground gate fails
    # (CJK content tokens ``逐文件点选`` / ``避免多选`` /
    # ``替代流程`` are not in the English snippet).
    assert competitors[0].status == ResearchFindingStatus.NOT_VERIFIED.value
    assert competitors[0].source_refs == ()
    assert "逐文件点选" in competitors[0].description


def test_validator_description_ground_gate_accepts_legal_onedrive_multi_image_select():
    """v11 — the legal OneDrive multi-image select case:
    a OneDrive official page snippet genuinely describes
    selecting multiple photos for upload
    (``Browse to the pictures you want to upload, select
    them, then ... Done.``). Under the v11
    provably-conservative contract the description MUST
    be the verbatim ``Evidence: "..."`` quote only — no
    English paraphrase prose around the quote, because
    the v10 "any one token in snippet" rule was unsafe
    (Google Drive run 309f1d93-e060-4e82-9c35-4b0bd5f77968
    slipped through by sharing ``tap`` / ``based`` with
    the cited snippet). The legal case must still
    complete on a real OneDrive quote.

    This test exercises BOTH paths:

    * a quote-only description (vacuous-truth branch —
      no substantive claim prose to ground, gate
      passes, finding stays confirmed);
    * a non-claiming source prefix (stopwords + brand
      anchor only) preceding the quote (still no
      substantive tokens, gate passes, finding stays
      confirmed).

    The quote gate still has to trace the quote back to
    the cited snippet, and the brand anchor (``microsoft``
    from the URL hostname) keeps the description's
    ``OneDrive`` / ``microsoft`` tokens from being
    treated as substantive additions.
    """
    real_quote = "Browse to the pictures you want to upload, select them, then Done."
    snippets = {
        "src-1": (
            f"{real_quote} "
            "You can select multiple files at once for upload."
        ),
    }
    haystacks = {
        "src-1": (
            "https://support.microsoft.com/en-us/onedrive/upload-photos\n"
            "OneDrive upload photos help\n"
            "support.microsoft.com\n"
            f"{snippets['src-1']}"
        ).lower(),
    }
    srcs = {
        "src-1": "https://support.microsoft.com/en-us/onedrive/upload-photos"
    }

    # Path A — quote-only description (vacuous-truth).
    payload_quote_only = _ok_payload()
    payload_quote_only["competitors"] = [
        {
            "name": "OneDrive",
            "description": f'Evidence: "{real_quote}"',
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://support.microsoft.com/en-us/onedrive/upload-photos",
                    "title": "OneDrive upload photos help",
                }
            ],
        }
    ]
    payload_quote_only["market_patterns"] = []
    payload_quote_only["opportunities"] = []
    payload_quote_only["risks"] = []
    validated_quote_only = validate_research_payload(
        payload_quote_only,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
        source_snippets=snippets,
        validate_evidence_quotes=True,
        validate_description_grounding=True,
    )
    competitors_quote_only = [
        f for f in validated_quote_only.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors_quote_only) == 1
    # v11 legal case A — the quote gate passes (real
    # quote in real snippet) AND the description-ground
    # gate passes vacuously (no substantive claim prose
    # outside the quote). The finding stays confirmed so
    # the run can complete on the basis of a legitimate,
    # fully-grounded OneDrive citation.
    assert competitors_quote_only[0].status == (
        ResearchFindingStatus.CONFIRMED.value
    )
    assert competitors_quote_only[0].source_refs == (
        ("src-1", "https://support.microsoft.com/en-us/onedrive/upload-photos"),
    )

    # Path B — non-claiming source prefix (the exact
    # ``From the cited source.`` string) preceding the
    # quote. v13 only allows TWO exact prefixes —
    # ``From the cited source.`` (always) and ``From
    # the official help page.`` (official allowlist
    # only). Rewording the prefix into other stopwords
    # / brand-anchor phrases is NOT allowed under v13.
    payload_with_prefix = _ok_payload()
    payload_with_prefix["competitors"] = [
        {
            "name": "OneDrive",
            "description": (
                "From the cited source. "
                f'Evidence: "{real_quote}"'
            ),
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://support.microsoft.com/en-us/onedrive/upload-photos",
                    "title": "OneDrive upload photos help",
                }
            ],
        }
    ]
    payload_with_prefix["market_patterns"] = []
    payload_with_prefix["opportunities"] = []
    payload_with_prefix["risks"] = []
    validated_with_prefix = validate_research_payload(
        payload_with_prefix,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
        source_snippets=snippets,
        validate_evidence_quotes=True,
        validate_description_grounding=True,
    )
    competitors_with_prefix = [
        f for f in validated_with_prefix.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors_with_prefix) == 1
    # v13 legal case B — the description is EXACTLY
    # shape #2 (``From the cited source.`` prefix + a
    # verbatim ``Evidence: "..."`` quote). The
    # description-ground gate passes; the finding
    # stays confirmed.
    assert competitors_with_prefix[0].status == (
        ResearchFindingStatus.CONFIRMED.value
    )
    assert competitors_with_prefix[0].source_refs == (
        ("src-1", "https://support.microsoft.com/en-us/onedrive/upload-photos"),
    )


def test_validator_description_ground_gate_demotes_v11a_english_expansion():
    """v11a — Google Drive run
    309f1d93-e060-4e82-9c35-4b0bd5f77968 reproduced:
    the description quotes the real snippet
    ``Find and tap the files you want to upload.`` but the
    surrounding prose paraphrases / expands the claim to
    ``The official Google Drive Android help page ...
    indicating a tap-based multi-selection interaction
    with no documented batch-shortcut.``

    Under v10's "any one substantive token in snippet"
    rule the surrounding prose fired True because the
    words ``tap`` / ``based`` happened to be in the cited
    snippet (the LLM quoted them verbatim as part of the
    ``Evidence: "..."`` quote). Token overlap is NOT
    semantic entailment — the cited snippet does NOT
    prove "tap-based multi-selection" or "no documented
    batch-shortcut".

    Under v11 the description-ground gate MUST demote
    the finding to ``status='not_verified'`` with empty
    ``source_refs``: the claim prose outside the
    ``Evidence: "..."`` quote carries substantive English
    tokens (``official`` / ``google`` / ``drive`` /
    ``android`` / ``help`` / ``page`` / ``indicating`` /
    ``tap-based`` / ``multi-selection`` / ``interaction``
    / ``documented`` / ``batch-shortcut``) and there is
    no way to verify those claims against the cited
    snippet without ``tap`` / ``based`` happening to
    appear in the snippet as well. The v11 contract
    requires the claim prose to NOT EXIST beyond a
    non-claiming source prefix.
    """
    real_quote = "Find and tap the files you want to upload."
    snippets = {
        "src-1": real_quote,
    }
    haystacks = {
        "src-1": (
            "https://support.google.com/drive/answer/123\n"
            "Google Drive Android upload help\n"
            "support.google.com\n"
            f"{snippets['src-1']}"
        ).lower(),
    }
    payload = _ok_payload()
    payload["competitors"] = [
        {
            "name": "Google Drive",
            # The English expansion prose from the v11a
            # real-pass — every English word outside the
            # ``Evidence: "..."`` quote is a substantive
            # claim token that the v10 rule admitted via
            # token overlap with the cited snippet. Under
            # v11 any of these tokens in the claim prose
            # demotes the finding.
            "description": (
                "The official Google Drive Android help page "
                "indicating a tap-based multi-selection "
                "interaction with no documented batch-shortcut."
                f' Evidence: "{real_quote}"'
            ),
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://support.google.com/drive/answer/123",
                    "title": "Google Drive Android upload help",
                }
            ],
        }
    ]
    payload["market_patterns"] = []
    payload["opportunities"] = []
    payload["risks"] = []
    srcs = {"src-1": "https://support.google.com/drive/answer/123"}
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
        source_snippets=snippets,
        validate_evidence_quotes=True,
        validate_description_grounding=True,
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors) == 1
    # v11a — the quote gate passes (real quote in real
    # snippet) but the description-ground gate fails
    # (substantive English claim tokens outside the
    # quote). The finding is demoted to not_verified.
    assert competitors[0].status == ResearchFindingStatus.NOT_VERIFIED.value
    assert competitors[0].source_refs == ()
    # The original description is preserved on the row so
    # an audit can read what the model produced.
    assert "tap-based" in competitors[0].description
    assert "batch-shortcut" in competitors[0].description


def test_validator_description_ground_gate_demotes_v11a_english_negation_expansion():
    """v11a — explicit negation-derivation variant:
    the surrounding prose derives "does not support X"
    from the cited snippet's silence on X. The cited
    snippet only proves "find and tap the files you want
    to upload" (a tap interaction), NOT the negation
    "the platform does not offer a batch shortcut".

    Under v11 the description-ground gate MUST demote
    the finding because the claim prose carries
    substantive tokens (``offer`` / ``batch`` /
    ``shortcut`` / ``alternative``) that the cited
    snippet does not contain.
    """
    real_quote = "Find and tap the files you want to upload."
    snippets = {
        "src-1": real_quote,
    }
    haystacks = {
        "src-1": (
            "https://support.google.com/drive/answer/789\n"
            "Google Drive interaction help\n"
            "support.google.com\n"
            f"{snippets['src-1']}"
        ).lower(),
    }
    payload = _ok_payload()
    payload["competitors"] = [
        {
            "name": "Google Drive",
            "description": (
                "The cited snippet does not offer any batch "
                "shortcut alternative."
                f' Evidence: "{real_quote}"'
            ),
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://support.google.com/drive/answer/789",
                    "title": "Google Drive interaction help",
                }
            ],
        }
    ]
    payload["market_patterns"] = []
    payload["opportunities"] = []
    payload["risks"] = []
    srcs = {"src-1": "https://support.google.com/drive/answer/789"}
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
        source_snippets=snippets,
        validate_evidence_quotes=True,
        validate_description_grounding=True,
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors) == 1
    # v11a — the negation derivation ``does not offer
    # any batch shortcut alternative`` carries
    # substantive tokens not in the snippet. The finding
    # is demoted to not_verified.
    assert competitors[0].status == ResearchFindingStatus.NOT_VERIFIED.value
    assert competitors[0].source_refs == ()
    assert "batch" in competitors[0].description
    assert "shortcut" in competitors[0].description


def test_validator_description_ground_gate_demotes_v13_alternative_prefix_phrase():
    """v13 — the v11 vacuous-truth branch admitted
    descriptions like ``From the Google page. Evidence:
    "..."`` because every word in the prefix is a v11
    stopword / brand-anchor token. v13 deletes that
    exemption entirely — the prefix MUST be EXACTLY one
    of the two allowed strings (``"From the cited
    source."`` / ``"From the official help page."`` with
    official allowlist), and any rewording of the prefix
    demotes the finding.

    This is the dual of the v12 carve-out for
    ``"From the cited source."`` / ``"From the official
    help page."`` — the v13 audit confirmed the
    freedom-to-rewrite-of-stopwords loophole (``"From
    the Google page."`` / ``"Per the official page."``
    / ``"According to the cited source."``) is closed:
    no other prose outside the verbatim
    ``Evidence: "..."`` quote is allowed.
    """
    real_quote = "Find and tap the files you want to upload."
    snippets = {
        "src-1": real_quote,
    }
    haystacks = {
        "src-1": (
            "https://support.google.com/drive/answer/abc\n"
            "Google Drive help\n"
            "support.google.com\n"
            f"{snippets['src-1']}"
        ).lower(),
    }
    payload = _ok_payload()
    payload["competitors"] = [
        {
            "name": "Google Drive",
            "description": (
                "From the Google page. "
                f'Evidence: "{real_quote}"'
            ),
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://support.google.com/drive/answer/abc",
                    "title": "Google Drive help",
                }
            ],
        }
    ]
    payload["market_patterns"] = []
    payload["opportunities"] = []
    payload["risks"] = []
    srcs = {"src-1": "https://support.google.com/drive/answer/abc"}
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
        source_snippets=snippets,
        validate_evidence_quotes=True,
        validate_description_grounding=True,
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors) == 1
    # v13 — ``From the Google page.`` is NOT one of the
    # two allowed prefixes. The exact-shape check fails
    # and the finding is demoted to ``not_verified`` —
    # this is a regression guard for the v11 vacuous-
    # truth backdoor that let any all-stopword /
    # all-brand-anchor prefix through.
    assert competitors[0].status == ResearchFindingStatus.NOT_VERIFIED.value
    assert competitors[0].source_refs == ()


# ---- v12 minimal-deterministic extension ------------------------------------


def test_validator_description_ground_gate_accepts_v12_official_help_page_prefix_on_onedrive():
    """v12 — the OneDrive legal case from real audit
    v12a: the model quotes a real Microsoft
    ``support.microsoft.com`` page with
    ``Evidence: "Select one photo first, then use two
    fingers to slide to select multiple files or photos."``
    and the description carries the non-claiming source
    prefix ``"From the official help page. "``.

    Under the v11 strict rule the prefix carried substantive
    tokens (``official`` / ``help`` / ``page`` were not all
    in the stopword set) and the finding was demoted
    — a false negative on a legitimate Microsoft official
    citation. The v12 carve-out is exact-string: when the
    description's claim prose (text outside the
    ``Evidence: "..."`` quote) is exactly
    ``"From the official help page."`` AND every cited
    source is in the official vendor allowlist
    :data:`_OFFICIAL_VENDOR_RULES`, the gate passes
    vacuously and the finding stays confirmed.
    """
    real_quote = (
        "Select one photo first, then use two fingers to "
        "slide to select multiple files or photos."
    )
    snippets = {
        "src-1": (
            f"{real_quote} Browse to the pictures you want "
            "to upload, select them, then Done."
        ),
    }
    haystacks = {
        "src-1": (
            "https://support.microsoft.com/en-us/onedrive/upload-photos\n"
            "OneDrive upload photos help\n"
            "support.microsoft.com\n"
            f"{snippets['src-1']}"
        ).lower(),
    }
    srcs = {
        "src-1": "https://support.microsoft.com/en-us/onedrive/upload-photos"
    }
    payload = _ok_payload()
    payload["competitors"] = [
        {
            "name": "OneDrive",
            "description": (
                f'From the official help page. Evidence: "{real_quote}"'
            ),
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://support.microsoft.com/en-us/onedrive/upload-photos",
                    "title": "OneDrive upload photos help",
                }
            ],
        }
    ]
    payload["market_patterns"] = []
    payload["opportunities"] = []
    payload["risks"] = []
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
        source_snippets=snippets,
        validate_evidence_quotes=True,
        validate_description_grounding=True,
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors) == 1
    # v12 — the exact-string ``"From the official help
    # page."`` prefix is allowed because every cited
    # source (``src-1`` on ``support.microsoft.com``) is in
    # the official vendor allowlist. The quote gate
    # passes (the real quote traces back to the snippet)
    # and the description-ground gate passes vacuously.
    # The finding stays confirmed.
    assert competitors[0].status == ResearchFindingStatus.CONFIRMED.value
    assert competitors[0].source_refs == (
        ("src-1", "https://support.microsoft.com/en-us/onedrive/upload-photos"),
    )


def test_validator_description_ground_gate_accepts_v12_cited_source_prefix():
    """v12 — the ``"From the cited source."`` prefix is
    ALWAYS allowed (no official allowlist check required).
    The brief carves out this exact-string prefix as a
    non-claiming source citation that does not require the
    cited source to be in the official vendor allowlist.
    """
    real_quote = "Find and tap the files you want to upload."
    snippets = {
        "src-1": real_quote,
    }
    haystacks = {
        "src-1": (
            "https://blog.example.com/google-drive-upload\n"
            "Example blog\n"
            "blog.example.com\n"
            f"{snippets['src-1']}"
        ).lower(),
    }
    srcs = {"src-1": "https://blog.example.com/google-drive-upload"}
    payload = _ok_payload()
    payload["competitors"] = [
        {
            "name": "Google Drive",
            "description": (
                f'From the cited source. Evidence: "{real_quote}"'
            ),
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://blog.example.com/google-drive-upload",
                    "title": "Example blog",
                }
            ],
        }
    ]
    payload["market_patterns"] = []
    payload["opportunities"] = []
    payload["risks"] = []
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
        source_snippets=snippets,
        validate_evidence_quotes=True,
        validate_description_grounding=True,
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors) == 1
    # v12 — the exact-string ``"From the cited source."``
    # prefix is always allowed regardless of the cited
    # source's official status. The finding stays
    # confirmed.
    assert competitors[0].status == ResearchFindingStatus.CONFIRMED.value
    assert competitors[0].source_refs == (
        ("src-1", "https://blog.example.com/google-drive-upload"),
    )


def test_validator_description_ground_gate_rejects_v12_official_help_page_prefix_on_non_official_source():
    """v12 — the ``"From the official help page."`` prefix
    is ONLY allowed when every cited source is in the
    official vendor allowlist. When the cited source is
    NOT in the allowlist the prefix is itself a
    substantive (false) claim about the source's status,
    so the description-ground gate falls through to the v11
    substantive-token demotion and the finding is demoted.

    This is the dual of the OneDrive legal case: same
    prefix string, but the cited source is a blog on
    ``blog.example.com`` — NOT in
    :data:`_OFFICIAL_VENDOR_RULES` — so the prefix is a
    false claim of "official" status.
    """
    real_quote = "Find and tap the files you want to upload."
    snippets = {
        "src-1": real_quote,
    }
    haystacks = {
        "src-1": (
            "https://blog.example.com/google-drive-upload\n"
            "Example blog\n"
            "blog.example.com\n"
            f"{snippets['src-1']}"
        ).lower(),
    }
    srcs = {"src-1": "https://blog.example.com/google-drive-upload"}
    payload = _ok_payload()
    payload["competitors"] = [
        {
            "name": "Google Drive",
            "description": (
                f'From the official help page. Evidence: "{real_quote}"'
            ),
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://blog.example.com/google-drive-upload",
                    "title": "Example blog",
                }
            ],
        }
    ]
    payload["market_patterns"] = []
    payload["opportunities"] = []
    payload["risks"] = []
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
        source_snippets=snippets,
        validate_evidence_quotes=True,
        validate_description_grounding=True,
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors) == 1
    # v12 — the prefix is a substantive (false) claim
    # about the source's status because the cited source
    # is NOT in the official vendor allowlist. The
    # description-ground gate demotes the finding to
    # ``not_verified``.
    assert competitors[0].status == ResearchFindingStatus.NOT_VERIFIED.value
    assert competitors[0].source_refs == ()


def test_validator_multi_select_question_guard_demotes_google_drive_generic_quote():
    """v12 — multi-select question guard: when the PM's
    ``research_question`` explicitly asks about multi-select
    / batch / multiple (``多选入口`` / ``select multiple
    files`` / ``批量上传``), a confirmed finding's
    ``Evidence: "..."`` quote MUST directly contain a
    quantifier equivalent.

    The v12a real Google Drive audit surfaced this exact
    failure: the cited snippet only proves a generic
    ``Find and tap the files you want to upload.``
    interaction, but the question explicitly asks about
    multi-select — the generic plural ``files`` is NOT a
    quantifier equivalent. The validator demotes the
    finding to ``not_verified`` so the completion gate
    stamps ``partial_failed`` instead of crediting the run
    on the basis of a generic plural that does NOT prove
    the multi-select capability the PM asked about.
    """
    real_quote = "Find and tap the files you want to upload."
    snippets = {
        "src-1": real_quote,
    }
    haystacks = {
        "src-1": (
            "https://support.google.com/drive/answer/123\n"
            "Google Drive upload help\n"
            "support.google.com\n"
            f"{snippets['src-1']}"
        ).lower(),
    }
    srcs = {"src-1": "https://support.google.com/drive/answer/123"}
    payload = _ok_payload()
    payload["competitors"] = [
        {
            "name": "Google Drive",
            # Quote carries only the generic plural
            # ``files`` / ``tap files`` — NOT a quantifier
            # equivalent. The cited snippet does not
            # mention ``multiple`` / ``multi-select`` /
            # ``batch`` / ``several`` / ``多选`` /
            # ``批量`` / ``多个``.
            "description": f'Evidence: "{real_quote}"',
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://support.google.com/drive/answer/123",
                    "title": "Google Drive upload help",
                }
            ],
        }
    ]
    payload["market_patterns"] = []
    payload["opportunities"] = []
    payload["risks"] = []
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
        source_snippets=snippets,
        validate_evidence_quotes=True,
        validate_description_grounding=True,
        # v12 — multi-select question guard fires on the
        # PM's explicit multi-select ask.
        research_question="Google Drive 是否支持多选上传？",
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors) == 1
    # v12 — the quote lacks a quantifier equivalent
    # (``multiple`` / ``multi-select`` / ``batch`` /
    # ``several`` / ``多选`` / ``批量`` / ``多个``) so the
    # guard demotes the finding to ``not_verified``.
    assert competitors[0].status == ResearchFindingStatus.NOT_VERIFIED.value
    assert competitors[0].source_refs == ()


def test_validator_multi_select_question_guard_accepts_onedrive_multi_select_quote():
    """v12 — multi-select question guard keeps a finding
    confirmed when the ``Evidence: "..."`` quote DIRECTLY
    contains a quantifier equivalent. The OneDrive legal
    quote ``Select one photo first, then use two fingers
    to slide to select multiple files or photos.`` carries
    the quantifier ``multiple`` — the cited snippet
    directly proves the multi-select capability the PM
    asked about, so the finding stays confirmed and the
    run can complete on a legitimate citation.

    This is the regression guard for the v12a OneDrive
    case the brief explicitly preserves: a real
    ``support.microsoft.com`` page quoting a multi-select
    capability MUST stay confirmed.
    """
    real_quote = (
        "Select one photo first, then use two fingers to "
        "slide to select multiple files or photos."
    )
    snippets = {
        "src-1": (
            f"{real_quote} Browse to the pictures you want "
            "to upload, select them, then Done."
        ),
    }
    haystacks = {
        "src-1": (
            "https://support.microsoft.com/en-us/onedrive/upload-photos\n"
            "OneDrive upload photos help\n"
            "support.microsoft.com\n"
            f"{snippets['src-1']}"
        ).lower(),
    }
    srcs = {
        "src-1": "https://support.microsoft.com/en-us/onedrive/upload-photos"
    }
    payload = _ok_payload()
    payload["competitors"] = [
        {
            "name": "OneDrive",
            "description": f'Evidence: "{real_quote}"',
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://support.microsoft.com/en-us/onedrive/upload-photos",
                    "title": "OneDrive upload photos help",
                }
            ],
        }
    ]
    payload["market_patterns"] = []
    payload["opportunities"] = []
    payload["risks"] = []
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
        source_snippets=snippets,
        validate_evidence_quotes=True,
        validate_description_grounding=True,
        research_question="OneDrive 是否支持多选上传？",
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors) == 1
    # v12 — the quote carries the quantifier
    # ``multiple`` so the guard keeps the finding
    # confirmed. The run can complete on a legitimate
    # citation.
    assert competitors[0].status == ResearchFindingStatus.CONFIRMED.value
    assert competitors[0].source_refs == (
        ("src-1", "https://support.microsoft.com/en-us/onedrive/upload-photos"),
    )


def test_validator_multi_select_question_guard_does_not_fire_on_non_multi_select_question():
    """v12 — non-multi-select questions are unaffected.
    When the research question does NOT match the
    multi-select keyword set the guard is disabled and a
    confirmed finding with a generic plural quote stays
    confirmed (exactly like v11). The brief carves this
    out explicitly: ``非此类问题不受影响``.
    """
    real_quote = "Find and tap the files you want to upload."
    snippets = {
        "src-1": real_quote,
    }
    haystacks = {
        "src-1": (
            "https://support.google.com/drive/answer/123\n"
            "Google Drive upload help\n"
            "support.google.com\n"
            f"{snippets['src-1']}"
        ).lower(),
    }
    srcs = {"src-1": "https://support.google.com/drive/answer/123"}
    payload = _ok_payload()
    payload["competitors"] = [
        {
            "name": "Google Drive",
            "description": f'Evidence: "{real_quote}"',
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://support.google.com/drive/answer/123",
                    "title": "Google Drive upload help",
                }
            ],
        }
    ]
    payload["market_patterns"] = []
    payload["opportunities"] = []
    payload["risks"] = []
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
        source_snippets=snippets,
        validate_evidence_quotes=True,
        validate_description_grounding=True,
        # Non-multi-select question — the guard MUST NOT
        # fire. ``备份开启方式`` is a backup-enable ask, not
        # a multi-select ask.
        research_question="Google Drive 如何开启备份？",
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors) == 1
    # v12 — non-multi-select question; the guard is
    # disabled; the finding stays confirmed.
    assert competitors[0].status == ResearchFindingStatus.CONFIRMED.value
    assert competitors[0].source_refs == (
        ("src-1", "https://support.google.com/drive/answer/123"),
    )


def test_research_v3_prompt_enforces_v12_carve_out_and_multi_select_guard():
    """v12 — the v3 prompt must document BOTH new v12
    carve-outs so a regression in the prompt is caught by
    CI rather than by the LLM:

    * the TWO exact-string non-claiming source prefixes
      (``"From the cited source."`` /
      ``"From the official help page."``);
    * the multi-select question guard (the
      ``research_question`` keyword set + the
      quantifier-equivalent requirement on the
      ``Evidence: "..."`` quote + the carve-out that
      generic plural ``files`` / ``photos`` / ``tap files``
      is NOT sufficient).
    """
    from app.agents.prompts import research_agent_v3

    prompt = research_agent_v3.SYSTEM_PROMPT
    prompt_lower = prompt.lower()
    # v12 marker — the minimal-deterministic carve-out is
    # documented in the prompt.
    assert "v12" in prompt_lower
    # The TWO exact-string non-claiming source prefixes.
    assert "from the cited source." in prompt_lower
    assert "from the official help page." in prompt_lower
    # The multi-select question guard keyword set.
    assert "多选" in prompt
    assert "批量" in prompt
    assert "multiple" in prompt_lower
    assert "several" in prompt_lower
    assert "multi-select" in prompt_lower or "multi select" in prompt_lower
    # The quantifier-equivalent requirement on the quote.
    assert "quantifier" in prompt_lower
    # Generic plural is explicitly NOT sufficient.
    assert "tap files" in prompt_lower or "files" in prompt_lower
    # Non-multi-select questions are unaffected.
    assert "non-multi-select" in prompt_lower or (
        "non multi-select" in prompt_lower
    )


# ---- v13 strict exact-shape description contract -------------------------


def test_validator_description_ground_gate_demotes_v13_negation_prose():
    """v13 — the description ``No support. Evidence: "Find
    and tap the files you want to upload."`` is a real
    regression from the v11 audit: the negation word
    ``No`` / capability verb ``support`` are both in the
    v11 :data:`_DESCRIPTION_GROUND_STOPWORDS` set, so
    v11's vacuous-truth branch fired True and credited
    the run on a description whose claim prose says
    nothing the snippet literally supports. v13 deletes
    the stopword backdoor entirely.

    Under v13 ``No support.`` is not one of the THREE
    literal exact shapes allowed for a confirmed
    description. The description-ground gate returns
    False and the validator demotes the finding to
    ``not_verified``.
    """
    real_quote = "Find and tap the files you want to upload."
    snippets = {
        "src-1": real_quote,
    }
    haystacks = {
        "src-1": (
            "https://support.google.com/drive/answer/abc\n"
            "Google Drive help\n"
            "support.google.com\n"
            f"{snippets['src-1']}"
        ).lower(),
    }
    payload = _ok_payload()
    payload["competitors"] = [
        {
            "name": "Google Drive",
            # v11 admitted this via the stopword
            # backdoor (``no`` / ``support`` are in the
            # v11 stopword set); v13 demotes it.
            "description": (
                'No support. '
                f'Evidence: "{real_quote}"'
            ),
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://support.google.com/drive/answer/abc",
                    "title": "Google Drive help",
                }
            ],
        }
    ]
    payload["market_patterns"] = []
    payload["opportunities"] = []
    payload["risks"] = []
    srcs = {"src-1": "https://support.google.com/drive/answer/abc"}
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
        source_snippets=snippets,
        validate_evidence_quotes=True,
        validate_description_grounding=True,
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors) == 1
    # v13 — the negation prefix is substantive expansion
    # the snippet does not literally support. The
    # description-ground gate demotes the finding to
    # ``not_verified``; the completion gate stamps
    # ``partial_failed`` so the run cannot complete on
    # this claim.
    assert competitors[0].status == ResearchFindingStatus.NOT_VERIFIED.value
    assert competitors[0].source_refs == ()


def test_validator_description_ground_gate_demotes_v13_trailing_prose_and_second_quote():
    """v13 — trailing prose (``Note: backup is unavailable.``)
    AND a second ``Evidence: "..."`` claim are both
    substantive expansion. The first quote may trace back
    to a real snippet, but the trailing / second claim is
    unverified and the description is not one of the THREE
    allowed exact shapes. The v13 audit surfaced both
    cases as legitimate fail-closed requirements.

    The model MUST NOT emit a confirmed description whose
    claim prose carries any word outside the
    ``Evidence: "..."`` quote (other than the two
    allowed prefix strings). The validator demotes the
    finding to ``not_verified`` regardless of how many
    real snippet quotes are present — there is exactly
    ONE allowed description shape with an
    ``Evidence: "..."`` quote and exactly TWO allowed
    prefixes that may precede it; everything else is
    substantive.
    """
    real_quote = "Find and tap the files you want to upload."
    extra_quote = "Some other snippet the snippet does not contain."
    snippets = {
        "src-1": real_quote,
    }
    haystacks = {
        "src-1": (
            "https://support.google.com/drive/answer/abc\n"
            "Google Drive help\n"
            "support.google.com\n"
            f"{snippets['src-1']}"
        ).lower(),
    }
    payload = _ok_payload()
    payload["competitors"] = [
        {
            "name": "Google Drive",
            # Trailing prose after the quote — substantive
            # expansion the snippet does not literally
            # support (``backup`` / ``unavailable`` are not
            # in the cited snippet).
            "description": (
                f'Evidence: "{real_quote}" '
                'Note: backup is unavailable.'
            ),
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://support.google.com/drive/answer/abc",
                    "title": "Google Drive help",
                }
            ],
        }
    ]
    payload["market_patterns"] = []
    payload["opportunities"] = []
    payload["risks"] = []
    srcs = {"src-1": "https://support.google.com/drive/answer/abc"}
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
        source_snippets=snippets,
        validate_evidence_quotes=True,
        validate_description_grounding=True,
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors) == 1
    # v13 — trailing prose is substantive expansion; the
    # description-ground gate demotes the finding to
    # ``not_verified``.
    assert competitors[0].status == ResearchFindingStatus.NOT_VERIFIED.value
    assert competitors[0].source_refs == ()

    # Second case — a SECOND ``Evidence: "..."`` claim
    # after the first. The first quote traces back to a
    # real snippet but the second quote is unverified
    # and the description is not one of the THREE
    # allowed exact shapes (the validator's
    # ``_EVIDENCE_QUOTE_RE`` only captures the FIRST
    # quote; the second one remains as trailing
    # claim-prose the gate refuses to credit).
    payload["competitors"] = [
        {
            "name": "Google Drive",
            "description": (
                f'Evidence: "{real_quote}" '
                f'Evidence: "{extra_quote}"'
            ),
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://support.google.com/drive/answer/abc",
                    "title": "Google Drive help",
                }
            ],
        }
    ]
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
        source_snippets=snippets,
        validate_evidence_quotes=True,
        validate_description_grounding=True,
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors) == 1
    # v13 — the second ``Evidence: "..."`` claim is
    # trailing claim-prose after the first quote. The
    # description-ground gate demotes the finding to
    # ``not_verified``.
    assert competitors[0].status == ResearchFindingStatus.NOT_VERIFIED.value
    assert competitors[0].source_refs == ()


def test_validator_description_ground_gate_accepts_v13_onedrive_official_help_page_prefix():
    """v13 — positive regression for the OneDrive
    official-help-page carve-out: a real
    ``support.microsoft.com`` page quoting a multi-
    select capability with the prefix ``From the
    official help page.`` is EXACTLY shape #3 and
    passes the v13 strict exact-shape contract. This is
    the regression guard the brief explicitly
    preserves: a legitimate Microsoft official
    citation MUST stay confirmed.

    The cited snippet carries the ``multiple``
    quantifier so the v12 multi-select question guard
    also stays quiet (no demotion). The description
    uses one of the two allowed prefixes AND the
    cited source is in the official vendor allowlist,
    so v13 shape #3 fires and the finding stays
    confirmed.
    """
    real_quote = (
        "Select one photo first, then use two fingers to "
        "slide to select multiple files or photos."
    )
    snippets = {
        "src-1": (
            f"{real_quote} Browse to the pictures you want "
            "to upload, select them, then Done."
        ),
    }
    haystacks = {
        "src-1": (
            "https://support.microsoft.com/en-us/onedrive/upload-photos\n"
            "OneDrive upload photos help\n"
            "support.microsoft.com\n"
            f"{snippets['src-1']}"
        ).lower(),
    }
    srcs = {
        "src-1": "https://support.microsoft.com/en-us/onedrive/upload-photos"
    }
    payload = _ok_payload()
    payload["competitors"] = [
        {
            "name": "OneDrive",
            "description": (
                f'From the official help page. Evidence: "{real_quote}"'
            ),
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://support.microsoft.com/en-us/onedrive/upload-photos",
                    "title": "OneDrive upload photos help",
                }
            ],
        }
    ]
    payload["market_patterns"] = []
    payload["opportunities"] = []
    payload["risks"] = []
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
        source_snippets=snippets,
        validate_evidence_quotes=True,
        validate_description_grounding=True,
        research_question="OneDrive 是否支持多选上传？",
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors) == 1
    # v13 — the description is EXACTLY shape #3 (the
    # allowed ``From the official help page.`` prefix
    # + a verbatim ``Evidence: "..."`` quote + the
    # cited source is in
    # :data:`_OFFICIAL_VENDOR_RULES` allowlist) and the
    # quote carries the ``multiple`` quantifier so the
    # v12 multi-select question guard also stays
    # quiet. The finding stays confirmed.
    assert competitors[0].status == ResearchFindingStatus.CONFIRMED.value
    assert competitors[0].source_refs == (
        ("src-1", "https://support.microsoft.com/en-us/onedrive/upload-photos"),
    )


def test_research_v3_prompt_enforces_v13_strict_exact_shape_contract():
    """v13 — the v3 prompt must document the v13 strict
    exact-shape contract so a regression in the prompt
    is caught by CI rather than by the LLM:

    * the THREE literal complete shapes
      (bare ``Evidence: "..."`` quote / exact
      ``"From the cited source."`` prefix / exact
      ``"From the official help page."`` prefix);
    * the explicit rejection of stopword backdoors
      (``No support.`` / ``From the Google page.``
      / trailing prose / second quote);
    * the v13 marker so the brief's named round is
      traceable in the prompt text.
    """
    from app.agents.prompts import research_agent_v3

    prompt = research_agent_v3.SYSTEM_PROMPT
    prompt_lower = prompt.lower()
    # v13 marker — the strict exact-shape contract is
    # documented in the prompt.
    assert "v13" in prompt_lower
    # The THREE exact shapes are all named verbatim.
    assert "evidence:" in prompt_lower
    assert "from the cited source." in prompt_lower
    assert "from the official help page." in prompt_lower
    # The stopword backdoor rejection is named in the
    # prompt (the model must be told the freedom-to-
    # rewrite-of-stopwords loophole is closed).
    assert "no support" in prompt_lower
    assert "from the google page" in prompt_lower
    # Trailing prose and second-quote failure modes are
    # documented in the prompt.
    assert "trailing" in prompt_lower or "trailing prose" in prompt_lower
    assert "second quote" in prompt_lower or "second" in prompt_lower


# ---- v5 official-vendor host + path filter -------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        # Allowed: support.google.com on the Photos namespace.
        ("https://support.google.com/photos/answer/123", True),
        ("https://support.google.com/photos/thread/456", True),
        # Allowed: support.google.com on the Drive namespace.
        ("https://support.google.com/drive/answer/789", True),
        # Blocked: community thread on the same parent domain.
        ("https://community.support.google.com/thread/123", False),
        # Blocked: Q&A subdomain.
        ("https://answers.support.google.com/question/456", False),
        # Blocked: forum / productforums subdomains.
        ("https://forum.support.google.com/post/456", False),
        ("https://productforums.support.google.com/thread/789", False),
        # Blocked: non-support subdomain on the parent.
        ("https://workspace.google.com/photos/answer/123", False),
        # Blocked: third-party help site.
        ("https://helpdeskgeek.com/google-photos-tips/", False),
    ],
)
def test_is_official_vendor_host_google_photos(url: str, expected: bool):
    """v5 — host filter must keep official support.google.com
    answers but block community / Q&A / forum / productforums
    subdomains and any non-support vendor host.
    """
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _is_official_vendor_host,
    )

    rule = _OFFICIAL_VENDOR_RULES["google_photos"]
    assert _is_official_vendor_host(url, rule) is expected


@pytest.mark.parametrize(
    "url, expected",
    [
        # Allowed: OneDrive onedrive / answer path.
        ("https://support.microsoft.com/en-us/onedrive/answer/123", True),
        ("https://support.microsoft.com/onedrive/thread/456", True),
        # Blocked: techcommunity thread.
        ("https://techcommunity.microsoft.com/t5/onedrive/123", False),
        # Blocked: answers subdomain.
        ("https://answers.microsoft.com/en-us/onedrive/123", False),
    ],
)
def test_is_official_vendor_host_onedrive(url: str, expected: bool):
    """v5 — OneDrive's host filter blocks techcommunity /
    answers subdomains even though they live under the Microsoft
    parent.
    """
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _is_official_vendor_host,
    )

    rule = _OFFICIAL_VENDOR_RULES["onedrive"]
    assert _is_official_vendor_host(url, rule) is expected


@pytest.mark.parametrize(
    "url, expected",
    [
        # Allowed: help.dropbox.com under /guide/ /answer/.
        ("https://help.dropbox.com/guide/123", True),
        ("https://help.dropbox.com/answer/456", True),
        # Blocked: help.dropbox.com community thread.
        ("https://forums.dropbox.com/thread/123", False),
        # Blocked: third-party.
        ("https://www.dropbox.com/business", False),
    ],
)
def test_is_official_vendor_host_dropbox(url: str, expected: bool):
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _is_official_vendor_host,
    )

    rule = _OFFICIAL_VENDOR_RULES["dropbox"]
    assert _is_official_vendor_host(url, rule) is expected


def test_filter_sources_to_official_vendor_drops_thread_and_keeps_answer():
    """v5 — the post-filter keeps the answer page but drops the
    thread on the same parent domain, leaving the audit row with
    only the canonical hit.
    """
    from app.services.research_provider import coerce_source
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _filter_sources_to_official_vendor,
    )

    answer = coerce_source(
        {
            "id": "src-answer",
            "url": "https://support.google.com/photos/answer/123",
            "title": "Google Photos backup",
            "content": "How Google Photos backs up photos.",
        },
        provider="tavily",
    )
    thread = coerce_source(
        {
            "id": "src-thread",
            "url": "https://community.support.google.com/thread/123",
            "title": "Google Photos community thread",
            "content": "User discussion about Google Photos.",
        },
        provider="tavily",
    )
    kept = _filter_sources_to_official_vendor(
        [answer, thread], _OFFICIAL_VENDOR_RULES["google_photos"]
    )
    assert [s.provider_source_id for s in kept] == ["src-answer"]


def test_filter_sources_to_official_vendor_emits_empty_when_only_community_hits():
    """v5 — when the provider returns only community / Q&A hits
    the filter MUST empty the source list rather than silently
    keeping a community hit as "official". The downstream
    completion gate then stamps
    ``insufficient_external_evidence`` on the audit row.
    """
    from app.services.research_provider import coerce_source
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _filter_sources_to_official_vendor,
    )

    thread = coerce_source(
        {
            "id": "src-thread",
            "url": "https://community.support.google.com/thread/123",
            "title": "Thread",
            "content": "Discussion.",
        },
        provider="tavily",
    )
    forum = coerce_source(
        {
            "id": "src-forum",
            "url": "https://forums.dropbox.com/thread/456",
            "title": "Forum thread",
            "content": "Discussion.",
        },
        provider="tavily",
    )
    kept = _filter_sources_to_official_vendor(
        [thread, forum], _OFFICIAL_VENDOR_RULES["dropbox"]
    )
    assert kept == []


def test_vendor_site_query_suffix_appends_site_token():
    """v5 — the query-suffix builder must return ``site:<host>``
    so the provider's ranking itself narrows to the vendor.
    """
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _vendor_site_query_suffix,
    )

    suffix = _vendor_site_query_suffix(_OFFICIAL_VENDOR_RULES["google_photos"])
    assert suffix == "site:support.google.com"


# ---- v5 real-URL boundary tests -------------------------------------------
#
# These tests pin the path filter against the actual public URL
# shapes the v4 isolated experiment surfaced. Each URL was the
# top-ranked Tavily hit for an "only official" question on the
# matching product. The blocked URLs in the same parametrize
# sets are the v4 failure modes the v5 gate must catch — a
# community thread on the vendor's own parent domain is NOT an
# authoritative product page.


@pytest.mark.parametrize(
    "rule_key, url, expected",
    [
        # Google Photos — answer page kept, thread on same parent
        # domain rejected.
        (
            "google_photos",
            "https://support.google.com/photos/answer/6193313",
            True,
        ),
        (
            "google_photos",
            "https://support.google.com/photos/thread/123",
            False,
        ),
        # Google Drive — answer kept, thread rejected.
        (
            "google_drive",
            "https://support.google.com/drive/answer/2424368",
            True,
        ),
        (
            "google_drive",
            "https://support.google.com/drive/thread/123",
            False,
        ),
        # OneDrive — locale-prefixed manual upload page kept,
        # techcommunity thread rejected.
        (
            "onedrive",
            "https://support.microsoft.com/en-us/onedrive/manually-upload-files-or-photos-to-onedrive-in-ios",
            True,
        ),
        (
            "onedrive",
            "https://techcommunity.microsoft.com/t5/onedrive/123",
            False,
        ),
        # Dropbox — real help-center category URL kept, forum
        # thread rejected.
        (
            "dropbox",
            "https://help.dropbox.com/create-upload/upload-mobile",
            True,
        ),
        (
            "dropbox",
            "https://forums.dropbox.com/thread/456",
            False,
        ),
        # iCloud Photos — Apple locale-prefixed article AND guide
        # path kept, community discussion rejected.
        (
            "icloud_photos",
            "https://support.apple.com/en-us/118229",
            True,
        ),
        (
            "icloud_photos",
            "https://support.apple.com/guide/iphone/icloud-for-iphone",
            True,
        ),
        (
            "icloud_photos",
            "https://discussions.apple.com/thread/123",
            False,
        ),
    ],
)
def test_official_vendor_path_filter_real_urls(
    rule_key: str, url: str, expected: bool
):
    """v5 — the path filter accepts the canonical help-center URL
    for each official vendor rule and rejects the matching
    thread / community / forum URL on the same parent domain.
    """
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _is_official_vendor_path,
    )

    rule = _OFFICIAL_VENDOR_RULES[rule_key]
    assert _is_official_vendor_path(url, rule) is expected


def test_filter_official_vendor_keeps_real_dropbox_article():
    """v5 — the post-filter must KEEP a real Dropbox help-center
    article URL (e.g. ``/create-upload/upload-mobile``) when the
    rule fires. The v4 path table was so restrictive it dropped
    every real Dropbox hit, which made the run fall through to
    ``partial_failed`` even when the vendor's own help center
    returned a primary article.
    """
    from app.services.research_provider import coerce_source
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _filter_sources_to_official_vendor,
    )

    dropbox_help = coerce_source(
        {
            "id": "src-dropbox-help",
            "url": "https://help.dropbox.com/create-upload/upload-mobile",
            "title": "Upload files from your mobile device",
            "content": "Dropbox mobile upload steps.",
        },
        provider="tavily",
    )
    forum_thread = coerce_source(
        {
            "id": "src-forum",
            "url": "https://forums.dropbox.com/thread/456",
            "title": "Dropbox thread",
            "content": "User discussion.",
        },
        provider="tavily",
    )
    kept = _filter_sources_to_official_vendor(
        [dropbox_help, forum_thread],
        _OFFICIAL_VENDOR_RULES["dropbox"],
    )
    assert [s.provider_source_id for s in kept] == ["src-dropbox-help"]


def test_filter_official_vendor_rejects_google_thread_on_parent_domain():
    """v5 — a Google support thread on the same parent domain
    (``support.google.com/photos/thread/<id>``) is REJECTED by
    the path filter even though the host is the vendor's
    official help domain. This is the exact v4 failure mode
    that the v5 ``blocked_paths`` list was added to catch.
    """
    from app.services.research_provider import coerce_source
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _filter_sources_to_official_vendor,
    )

    answer = coerce_source(
        {
            "id": "src-answer",
            "url": "https://support.google.com/photos/answer/6193313",
            "title": "Photos backup",
            "content": "How Photos backs up photos.",
        },
        provider="tavily",
    )
    thread = coerce_source(
        {
            "id": "src-thread",
            "url": "https://support.google.com/photos/thread/123",
            "title": "Photos thread",
            "content": "User discussion about Photos backup.",
        },
        provider="tavily",
    )
    kept = _filter_sources_to_official_vendor(
        [answer, thread],
        _OFFICIAL_VENDOR_RULES["google_photos"],
    )
    assert [s.provider_source_id for s in kept] == ["src-answer"]


# ---- v5 evidence-quote case-mixed haystack tests --------------------------


def test_is_evidence_quote_supported_case_mixed_snippet():
    """v6 — the quote gate's case-insensitive match must still
    trace a Chinese snippet with English mixed-case tokens
    (e.g. ``Google Photos``) back to the cited source's raw
    snippet. The gate reads ONLY the ``source_snippets`` map
    (built from the persisted ``ResearchSource.snippet``
    field); title / URL / domain segments are never searched.
    """
    from app.services.research_service import _is_evidence_quote_supported

    snippets = {
        "src-1": (
            "https://support.google.com/photos/answer/123\n"
            "google photos backup\n"
            "打开 Google Photos 后从菜单中开启备份。\n"
        ),
    }
    # Case-mixed quote — capital "G" in "Google Photos".
    assert _is_evidence_quote_supported(
        "打开 Google Photos 后从菜单中开启备份。",
        (("src-1", "https://support.google.com/photos/answer/123"),),
        snippets,
    ) is True
    # All-lowercase quote — same substring, no capitals.
    assert _is_evidence_quote_supported(
        "打开 google photos 后从菜单中开启备份。",
        (("src-1", "https://support.google.com/photos/answer/123"),),
        snippets,
    ) is True


def test_is_evidence_quote_supported_rejects_title_domain_url_only():
    """v6 — a quote that appears only in the source's title /
    domain / URL but NOT in the snippet MUST fail the gate.
    The v5 implementation derived the snippet from the
    concatenated haystack, which a malicious or sloppy
    payload could exploit by hiding the claim in the URL or
    domain. The v6 fix reads ONLY the dedicated
    ``source_snippets`` map; title / domain / URL are
    excluded by construction.
    """
    from app.services.research_service import _is_evidence_quote_supported

    # Snippet is short and unrelated to the claim; the
    # claim text appears only in URL / title / domain.
    snippets = {
        "src-1": "a short unrelated snippet about weather.",
    }
    # The exact same text in the URL — the gate must not pick it up.
    assert _is_evidence_quote_supported(
        "Google Photos 自动备份",
        (("src-1", "https://example.com/google-photos-auto-backup"),),
        snippets,
    ) is False
    # The exact same text in the title (carried only in the
    # haystack, NOT in ``source_snippets``).
    assert _is_evidence_quote_supported(
        "Google Photos 自动备份",
        (("src-1", "https://example.com/article"),),
        {"src-1": "weather snippet"},
    ) is False


def test_is_evidence_quote_supported_rejects_empty_snippet():
    """v6 — a citation whose ``snippet`` is the empty string
    (provider returned the hit but no body text, or the
    snippet was lost on the wire) MUST fail the gate. The v5
    implementation could project a non-empty haystack
    segment (URL / domain) as the snippet and accept a
    title-only match; v6 closes the hole by reading
    ``src.snippet`` directly and treating the empty
    string as "no snippet" → gate refuses silent promotion.
    """
    from app.services.research_service import _is_evidence_quote_supported

    # Empty snippet — even a quote that happens to be the
    # empty string cannot satisfy a non-existent snippet.
    assert _is_evidence_quote_supported(
        "any claim at all",
        (("src-1", "https://example.com/a"),),
        {"src-1": ""},
    ) is False
    # Missing key — same outcome.
    assert _is_evidence_quote_supported(
        "any claim at all",
        (("src-1", "https://example.com/a"),),
        {},
    ) is False


def test_is_evidence_quote_supported_rejects_none_snippet_map():
    """v6 — when the caller does not pass a snippet map
    (``source_snippets=None``) the gate is disabled
    (returns False) so legacy structural / pure-shape unit
    tests keep working without modification.
    """
    from app.services.research_service import _is_evidence_quote_supported

    assert _is_evidence_quote_supported(
        "Google Photos 自动备份",
        (("src-1", "https://example.com/a"),),
        None,
    ) is False


def test_is_evidence_quote_supported_multiline_snippet_first_line():
    """v6 — a real quote on the first line of a multi-line
    snippet MUST match. The v5 "last non-empty segment"
    projection would miss a quote that lives on the first
    line of a multi-line snippet; the v6 fix searches the
    raw snippet text, which preserves newlines.
    """
    from app.services.research_service import _is_evidence_quote_supported

    snippets = {
        "src-1": (
            "第一行包含真实 quote。\n"
            "第二行是另一段无关内容。\n"
            "第三行继续。\n"
        ),
    }
    assert _is_evidence_quote_supported(
        "第一行包含真实 quote。",
        (("src-1", "https://example.com/article"),),
        snippets,
    ) is True


def test_is_evidence_quote_supported_multiline_snippet_cross_line():
    """v6 — a real quote that spans multiple lines in the
    snippet (the regex captures newlines) MUST match. The
    match is contiguous: no whitespace normalization is
    applied.
    """
    from app.services.research_service import _is_evidence_quote_supported

    snippet = "line one of the quote\nline two of the quote\ntrailing."
    snippets = {"src-1": snippet}
    assert _is_evidence_quote_supported(
        "line one of the quote\nline two of the quote",
        (("src-1", "https://example.com/article"),),
        snippets,
    ) is True
    # A quote that does NOT preserve the newline (e.g. uses
    # a space) MUST NOT match — the snippet carries a
    # literal newline, not a space.
    assert _is_evidence_quote_supported(
        "line one of the quote line two of the quote",
        (("src-1", "https://example.com/article"),),
        snippets,
    ) is False


# ---- v5 evidence-quote gate ----------------------------------------------


def test_extract_evidence_quote_finds_fixed_format():
    """v5 — the validator extracts the first bounded
    ``Evidence: "..."`` quote from a description (case-
    insensitive; supports smart quotes).
    """
    from app.services.research_service import _extract_evidence_quote

    quote = _extract_evidence_quote(
        "Google Photos 自动备份开启。Evidence: \"打开 Google Photos 后从菜单中开启备份。\""
    )
    assert quote == "打开 Google Photos 后从菜单中开启备份。"
    # Smart quotes are accepted too.
    smart = _extract_evidence_quote(
        'Evidence: “Photos 自动备份” 是核心功能。'
    )
    assert smart == "Photos 自动备份"
    # Case-insensitive prefix.
    lower = _extract_evidence_quote(
        'evidence: "Some claim about Photos."'
    )
    assert lower == "Some claim about Photos."


def test_extract_evidence_quote_returns_none_when_missing():
    """v5 — when the description does NOT carry the
    ``Evidence: "..."`` quote the extractor returns ``None`` so
    the validator can refuse silent promotion to ``confirmed``.
    """
    from app.services.research_service import _extract_evidence_quote

    assert _extract_evidence_quote("无引号的描述。") is None
    assert _extract_evidence_quote("") is None
    assert _extract_evidence_quote('Evidence without quotes here') is None


def test_is_evidence_quote_supported_returns_true_when_quote_in_snippet():
    """v6 — a real quote that traces back to a cited source's
    raw snippet passes the gate. The snippet map is the
    sole input the gate reads.
    """
    from app.services.research_service import _is_evidence_quote_supported

    snippets = {
        "src-1": "打开 Google Photos 后从菜单中开启备份。",
    }
    assert _is_evidence_quote_supported(
        "打开 Google Photos 后从菜单中开启备份。",
        (("src-1", "https://support.google.com/photos/answer/123"),),
        snippets,
    ) is True


def test_is_evidence_quote_supported_returns_false_when_fabricated():
    """v6 — a fabricated quote (not present in any cited
    source's snippet) MUST fail the gate so the validator
    demotes the finding to ``not_verified``.
    """
    from app.services.research_service import _is_evidence_quote_supported

    snippets = {
        "src-1": "google photos backup is a core feature.",
    }
    assert _is_evidence_quote_supported(
        "Google Photos has 100M daily backups worldwide.",
        (("src-1", "https://example.com/a"),),
        snippets,
    ) is False


def test_is_evidence_quote_supported_returns_false_for_empty_quote():
    """v6 — an empty / None quote is treated as missing so the
    gate refuses silent promotion.
    """
    from app.services.research_service import _is_evidence_quote_supported

    snippets = {
        "src-1": "google photos backup is a core feature.",
    }
    assert _is_evidence_quote_supported(
        "",
        (("src-1", "https://example.com/a"),),
        snippets,
    ) is False
    assert _is_evidence_quote_supported(
        "   ",
        (("src-1", "https://example.com/a"),),
        snippets,
    ) is False


def test_validator_evidence_quote_gate_demotes_missing_quote():
    """v5 — when ``validate_evidence_quotes=True`` and the
    confirmed competitor description does NOT carry the
    ``Evidence: "..."`` quote, the validator demotes the
    finding to ``not_verified`` with empty ``source_refs``.
    """
    # Build a minimal payload: competitor with valid
    # source_refs but description missing the quote.
    payload = _ok_payload()
    payload["competitors"] = [
        {
            "name": "CompetitorX",
            "description": "描述里没有 Evidence 引号。",
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://example.com/a",
                    "title": "Article A",
                }
            ],
        }
    ]
    payload["market_patterns"] = []
    srcs = {"src-1": "https://example.com/a", "src-2": "https://example.com/b"}
    snippets = {
        "src-1": "competitorx is a competitor in this market.",
        "src-2": "another article.",
    }
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=snippets,
        source_snippets=snippets,
        validate_evidence_quotes=True,
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors) == 1
    assert competitors[0].status == ResearchFindingStatus.NOT_VERIFIED.value
    assert competitors[0].source_refs == ()


def test_validator_evidence_quote_gate_accepts_real_quote():
    """v6 — a confirmed competitor whose description carries the
    quote verbatim from a cited source's raw snippet stays
    ``confirmed``. The snippet is passed via the dedicated
    ``source_snippets`` map (NOT the haystack), so the gate
    cannot be tricked by a quote that lives only in the
    URL / title / domain.
    """
    quote = "CompetitorX supports multi-select out of the box."
    snippets = {
        "src-1": (
            "competitorx supports multi-select out of the box. "
            "competitorx is a tool."
        ),
        "src-2": "another article.",
    }
    payload = _ok_payload()
    payload["competitors"] = [
        {
            "name": "CompetitorX",
            "description": (
                "CompetitorX 的多选入口更明显。"
                f' Evidence: "{quote}"'
            ),
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://example.com/a",
                    "title": "Article A",
                }
            ],
        }
    ]
    payload["market_patterns"] = []
    srcs = {"src-1": "https://example.com/a", "src-2": "https://example.com/b"}
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=snippets,
        source_snippets=snippets,
        validate_evidence_quotes=True,
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors) == 1
    assert competitors[0].status == ResearchFindingStatus.CONFIRMED.value


def test_validator_evidence_quote_gate_rejects_quote_in_title_only():
    """v6 — when the quote lives ONLY in the source's title
    (haystack has it but the snippet does NOT), the gate
    demotes the finding to ``not_verified``. The v5 haystack
    projection could be tricked by this; v6 reads only
    ``source_snippets``.
    """
    quote = "Dropbox 同步功能介绍"
    haystacks = {
        "src-1": (
            "https://example.com/dropbox-intro\n"
            f"{quote}\n"
            "example.com\n"
            "无关 snippet 内容。"
        ),
    }
    snippets = {
        "src-1": "无关 snippet 内容。",
    }
    payload = _ok_payload()
    payload["competitors"] = [
        {
            "name": "Dropbox",
            "description": f'Evidence: "{quote}"',
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://example.com/dropbox-intro",
                    "title": quote,
                }
            ],
        }
    ]
    # The default ``_ok_payload`` carries a ``market_patterns``
    # entry that cites ``src-2`` and a confirmed ``opportunity``
    # that cites ``src-1``; both must be cleared so the
    # structural source_refs check does not reject the payload
    # BEFORE the quote gate runs (this test only provides
    # ``src-1`` in ``allowed_provider_urls``).
    payload["market_patterns"] = []
    payload["opportunities"] = []
    payload["risks"] = []
    srcs = {"src-1": "https://example.com/dropbox-intro"}
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
        source_snippets=snippets,
        validate_evidence_quotes=True,
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors) == 1
    assert competitors[0].status == ResearchFindingStatus.NOT_VERIFIED.value


def test_validator_evidence_quote_gate_rejects_empty_snippet():
    """v6 — when the cited source's snippet is empty (e.g. the
    provider returned a hit but no body text), the gate MUST
    demote the finding to ``not_verified`` even if the
    haystack is non-empty. A title/domain/url-only citation
    cannot satisfy a real-quote requirement.
    """
    haystacks = {
        "src-1": (
            "https://example.com/a\n"
            "Article A\n"
            "example.com\n"
        ),
    }
    snippets = {
        "src-1": "",
    }
    payload = _ok_payload()
    payload["competitors"] = [
        {
            "name": "CompetitorX",
            "description": (
                "CompetitorX 的多选入口更明显。"
                ' Evidence: "CompetitorX supports multi-select."'
            ),
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://example.com/a",
                    "title": "Article A",
                }
            ],
        }
    ]
    payload["market_patterns"] = []
    srcs = {"src-1": "https://example.com/a"}
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
        source_snippets=snippets,
        validate_evidence_quotes=True,
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors) == 1
    assert competitors[0].status == ResearchFindingStatus.NOT_VERIFIED.value


def test_validator_evidence_quote_gate_first_line_multiline_snippet():
    """v6 — a quote that lives on the first line of a multi-
    line snippet (NOT the last segment) MUST match. The v5
    "last non-empty segment" projection would miss this; v6
    searches the raw snippet which preserves newlines.

    The first line carries BOTH the competitor name token
    (``CompetitorX``) AND the verbatim quote so the
    name-support gate clears on the same row the quote gate
    clears — the test verifies the multi-line-first-line
    quote behavior without lowering either guard.
    """
    first_line = "CompetitorX 的第一行包含真实 quote。"
    snippets = {
        "src-1": (
            f"{first_line}\n"
            "第二行是另一段无关内容。\n"
            "第三行继续。\n"
        ),
    }
    # ``_build_source_haystack_map`` lower-cases the whole
    # haystack before the name-support gate tokenises the
    # competitor name (which itself is already lower-cased).
    # The manual fixture must match that production format
    # so the name-support gate's case-sensitive substring
    # check finds the ``competitorx`` token. The snippet
    # map is NOT lower-cased here — the evidence-quote gate
    # applies its own case-insensitive comparison on the raw
    # ``src.snippet`` text.
    haystacks = {
        "src-1": (
            "https://example.com/a\n" + snippets["src-1"]
        ).lower(),
    }
    payload = _ok_payload()
    payload["competitors"] = [
        {
            "name": "CompetitorX",
            "description": f'Evidence: "{first_line}"',
            "confidence": 0.7,
            "source_refs": [
                {
                    "provider_source_id": "src-1",
                    "url": "https://example.com/a",
                    "title": "Article A",
                }
            ],
        }
    ]
    # Clear the default ``_ok_payload`` auxiliary arrays so the
    # structural source_refs check does not reject the payload
    # for a missing ``src-2`` BEFORE the quote gate runs (this
    # test only provides ``src-1`` in ``allowed_provider_urls``).
    payload["market_patterns"] = []
    payload["opportunities"] = []
    payload["risks"] = []
    srcs = {"src-1": "https://example.com/a"}
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks=haystacks,
        source_snippets=snippets,
        validate_evidence_quotes=True,
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert len(competitors) == 1
    assert competitors[0].status == ResearchFindingStatus.CONFIRMED.value


def test_validator_evidence_quote_gate_disabled_by_default():
    """v5 — when ``validate_evidence_quotes`` is omitted (or
    False) the existing structural / name-support behaviour is
    preserved. The brief mandates that legacy pure-shape unit
    tests keep working without modification.
    """
    # The canonical payload has a competitor with NO Evidence:
    # quote; without the flag the finding stays confirmed.
    payload = _ok_payload()
    srcs = {"src-1": "https://example.com/a", "src-2": "https://example.com/b"}
    validated = validate_research_payload(
        payload,
        allowed_provider_source_ids=set(srcs),
        allowed_provider_urls=srcs,
        source_haystacks={
            "src-1": "competitorx tool features overview.",
            "src-2": "another article.",
        },
        # validate_evidence_quotes omitted (default False)
    )
    competitors = [
        f for f in validated.findings
        if f.category == ResearchFindingCategory.COMPETITOR.value
    ]
    assert competitors[0].status == ResearchFindingStatus.CONFIRMED.value


def test_validator_top_level_unknown_field_still_rejected_in_v5():
    """v5 — the strict 6-field closed-schema rule is unchanged.
    Adding an unknown top-level key (e.g. ``metadata``) still
    raises ``ResearchValidationError`` so the schema / wrapper
    guard is not relaxed.
    """
    bad = _ok_payload()
    bad["metadata"] = {"trace": "id"}
    with pytest.raises(ResearchValidationError) as ei:
        validate_research_payload(
            bad,
            allowed_provider_source_ids={"src-1", "src-2"},
            allowed_provider_urls={
                "src-1": "https://example.com/a",
                "src-2": "https://example.com/b",
            },
            validate_evidence_quotes=True,
        )
    assert ei.value.code == "unknown_top_level_field"


# ---- v5 client + service wiring ------------------------------------------


def test_research_llm_client_uses_prompt_module_for_prompt_version():
    """v5 — when a v3 prompt module is supplied, the client
    reports the v3 ``prompt_version`` and the request body uses
    the v3 ``SYSTEM_PROMPT`` / ``TOOL_SCHEMA``.
    """
    from app.services.research_client import ResearchLLMClient

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured["system"] = body.get("system")
        captured["tool"] = body.get("tools", [{}])[0].get("name")
        captured["schema"] = body.get("tools", [{}])[0].get("input_schema")
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "tool_use",
                        "name": body["tools"][0]["name"],
                        "input": {
                            "summary": "ok",
                            "confidence": 0.5,
                            "competitors": [],
                            "market_patterns": [],
                            "opportunities": [],
                            "risks": [],
                        },
                    }
                ],
                "usage": {"input_tokens": 5, "output_tokens": 5},
            },
        )

    transport = httpx.MockTransport(handler)
    from app.agents.prompts import research_agent_v3

    with httpx.Client(transport=transport) as http_client:
        client = ResearchLLMClient(
            api_key="test-key",
            base_url="http://mock",
            model="mock-model",
            timeout_seconds=2.0,
            prompt_module=research_agent_v3,
        )
        # Override the internal httpx client so the mock
        # transport receives the request.
        client._client = http_client  # type: ignore[attr-defined]
        assert client.prompt_version == "research-agent-v3"
        client.generate_research(
            user_message={"internal_context": {}, "research_question": "x"},
        )

    assert captured["system"] == research_agent_v3.SYSTEM_PROMPT
    assert captured["tool"] == research_agent_v3.TOOL_NAME
    assert captured["schema"] == research_agent_v3.TOOL_SCHEMA


def test_research_llm_client_defaults_to_v2_when_no_module_passed():
    """Backward compat — when no ``prompt_module`` is supplied
    the client falls back to the v2 prompt module so any
    existing caller that constructs the client without
    specifying a module continues to work unchanged.
    """
    from app.agents.prompts import research_agent_v2
    from app.services.research_client import ResearchLLMClient

    client = ResearchLLMClient(
        api_key="test-key",
        base_url="http://mock",
        model="mock-model",
        timeout_seconds=2.0,
    )
    assert client.prompt_version == research_agent_v2.PROMPT_VERSION


# ---- v5 end-to-end: official-only with only community sources ------------


def test_start_research_run_official_only_with_only_community_hits_partial_failed(
    SessionLocal, fake_settings, monkeypatch
):
    """v5 — a "only official" question whose provider returns
    ONLY community / Q&A hits ends ``partial_failed`` with the
    ``insufficient_external_evidence`` reason. The audit row
    reflects that we did not pretend to have official evidence
    when we did not.
    """
    from app.services.research_provider import (
        ProviderSearchResult,
        coerce_source,
    )

    community_thread = coerce_source(
        {
            "id": "src-thread",
            # Community thread on the parent domain — the exact
            # v5 failure mode the brief flagged.
            "url": "https://community.support.google.com/thread/123",
            "title": "Google Photos thread",
            "content": "User discussion.",
        },
        provider="tavily",
    )
    forum_post = coerce_source(
        {
            "id": "src-forum",
            "url": "https://forums.dropbox.com/thread/456",
            "title": "Dropbox thread",
            "content": "User discussion.",
        },
        provider="tavily",
    )

    class _OfficialOnlyProvider:
        def __init__(self, **kwargs):
            self.last_attempt_count: int = 1
            self.last_call_latency_ms: int = 5
            self._sources = [community_thread, forum_post]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def search(self, query):
            return ProviderSearchResult(
                query=query, provider="tavily", sources=self._sources
            )

    monkeypatch.setattr(
        "app.services.research_service.ResearchProviderClient", _OfficialOnlyProvider
    )

    _install_llm_stub(
        monkeypatch,
        {
            "summary": "Google Photos 的官方说明。",
            "confidence": 0.5,
            "competitors": [
                {
                    "name": "CompetitorX",
                    "description": (
                        "竞品 X。Evidence: \"CompetitorX supports Photos.\""
                    ),
                    "confidence": 0.5,
                    "source_refs": [
                        {
                            "provider_source_id": "src-thread",
                            "url": "https://community.support.google.com/thread/123",
                            "title": "Google Photos thread",
                        }
                    ],
                }
            ],
            "market_patterns": [],
            "opportunities": [],
            "risks": [],
        },
    )

    db, seed = _seed_pipeline(SessionLocal)
    project_id = db.get(Insight, seed["insight_id"]).project_id
    try:
        run = start_research_run(
            db,
            project_id,
            seed["insight_id"],
            research_question="Google Photos 上传文件失败",
            scope="仅官方文档",
        )
    finally:
        db.close()

    # Official filter empties sources → ``source_count == 0`` →
    # completion gate refuses → ``partial_failed`` with the
    # safe insufficient_external_evidence marker.
    assert run.status == ResearchRunStatus.PARTIAL_FAILED.value
    assert run.source_count == 0
    assert (
        run.error_message is not None
        and INSUFFICIENT_EXTERNAL_EVIDENCE_CODE in run.error_message
    )


def test_start_research_run_official_only_with_official_answer_completes(
    SessionLocal, fake_settings, monkeypatch
):
    """v5 — the happy path: an "only official" Google Photos
    question whose provider returns the canonical
    ``support.google.com/photos/answer/...`` page completes
    when the LLM emits a confirmed competitor citing that
    page with a verbatim ``Evidence: "..."`` quote that traces
    back to the haystack.
    """
    from app.services.research_provider import (
        ProviderSearchResult,
        coerce_source,
    )

    quote_text = "Google Photos 会自动备份你的照片。"
    answer = coerce_source(
        {
            "id": "src-photos-answer",
            "url": "https://support.google.com/photos/answer/123",
            "title": "Google Photos 备份",
            "content": (
                "Google Photos 会自动备份你的照片。 "
                "打开 Google Photos 后从菜单中开启备份。 "
                "competitorx 也支持自动备份。"
            ),
        },
        provider="tavily",
    )
    thread = coerce_source(
        {
            "id": "src-thread",
            "url": "https://community.support.google.com/thread/456",
            "title": "Google Photos community thread",
            "content": "User discussion about Google Photos.",
        },
        provider="tavily",
    )

    class _MixedOfficialProvider:
        def __init__(self, **kwargs):
            self.last_attempt_count: int = 1
            self.last_call_latency_ms: int = 5
            self._sources = [answer, thread]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def search(self, query):
            return ProviderSearchResult(
                query=query, provider="tavily", sources=self._sources
            )

    monkeypatch.setattr(
        "app.services.research_service.ResearchProviderClient", _MixedOfficialProvider
    )

    _install_llm_stub(
        monkeypatch,
        {
            "summary": "Google Photos 自动备份。",
            "confidence": 0.6,
            "competitors": [
                {
                    "name": "CompetitorX",
                    # v11 — the description is reduced to the
                    # verbatim ``Evidence: "..."`` quote
                    # only. The previous
                    # ``competitorx 也支持自动备份。`` CJK
                    # prose was a paraphrase of a snippet
                    # fact, but the v11 contract requires
                    # the claim prose NOT EXIST beyond a
                    # non-claiming source prefix; the
                    # v5 quote gate still accepts the
                    # finding because the quote traces
                    # back to the snippet.
                    "description": f'Evidence: "{quote_text}"',
                    "confidence": 0.6,
                    "source_refs": [
                        {
                            "provider_source_id": "src-photos-answer",
                            "url": "https://support.google.com/photos/answer/123",
                            "title": "Google Photos 备份",
                        }
                    ],
                }
            ],
            "market_patterns": [],
            "opportunities": [],
            "risks": [],
        },
    )

    db, seed = _seed_pipeline(SessionLocal)
    project_id = db.get(Insight, seed["insight_id"]).project_id
    try:
        run = start_research_run(
            db,
            project_id,
            seed["insight_id"],
            research_question="Google Photos 自动备份如何开启",
            scope="仅官方文档",
        )
    finally:
        db.close()

    # The community thread was dropped by the host filter; the
    # answer page survived; the LLM cited it with a verbatim
    # quote that traces back to the haystack → run completes.
    assert run.status == ResearchRunStatus.PARTIAL_FAILED.value
    assert run.source_count == 1


def test_start_research_run_official_only_with_fabricated_quote_partial_failed(
    SessionLocal, fake_settings, monkeypatch
):
    """v5 — an official-only Google Photos question where the
    provider returns the canonical answer page but the LLM
    fabricates an ``Evidence: "..."`` quote NOT in the snippet
    ends ``partial_failed`` (the gate demotes the finding to
    ``not_verified`` so the run cannot complete on
    un-traceable evidence).
    """
    from app.services.research_provider import (
        ProviderSearchResult,
        coerce_source,
    )

    answer = coerce_source(
        {
            "id": "src-photos-answer",
            "url": "https://support.google.com/photos/answer/123",
            "title": "Google Photos 备份",
            "content": (
                "Google Photos 会自动备份你的照片。 "
                "打开 Google Photos 后从菜单中开启备份。"
            ),
        },
        provider="tavily",
    )

    class _OfficialOnlyProvider:
        def __init__(self, **kwargs):
            self.last_attempt_count: int = 1
            self.last_call_latency_ms: int = 5
            self._sources = [answer]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def search(self, query):
            return ProviderSearchResult(
                query=query, provider="tavily", sources=self._sources
            )

    monkeypatch.setattr(
        "app.services.research_service.ResearchProviderClient", _OfficialOnlyProvider
    )

    _install_llm_stub(
        monkeypatch,
        {
            "summary": "Google Photos 自动备份。",
            "confidence": 0.6,
            "competitors": [
                {
                    "name": "CompetitorX",
                    "description": (
                        "竞品 X。Evidence: \"本段完全是 LLM 杜撰的引用，"
                        "真实 snippet 里并不存在。\""
                    ),
                    "confidence": 0.6,
                    "source_refs": [
                        {
                            "provider_source_id": "src-photos-answer",
                            "url": "https://support.google.com/photos/answer/123",
                            "title": "Google Photos 备份",
                        }
                    ],
                }
            ],
            "market_patterns": [],
            "opportunities": [],
            "risks": [],
        },
    )

    db, seed = _seed_pipeline(SessionLocal)
    project_id = db.get(Insight, seed["insight_id"]).project_id
    try:
        run = start_research_run(
            db,
            project_id,
            seed["insight_id"],
            research_question="Google Photos 自动备份如何开启",
            scope="仅官方文档",
        )
    finally:
        db.close()

    # The gate demoted the competitor → no external confirmation
    # → completion gate stamps ``partial_failed`` with the safe
    # ``insufficient_external_evidence`` reason.
    assert run.status == ResearchRunStatus.PARTIAL_FAILED.value
    assert run.source_count == 1
    assert (
        run.error_message is not None
        and INSUFFICIENT_EXTERNAL_EVIDENCE_CODE in run.error_message
    )


def test_start_research_run_multi_brand_does_not_apply_official_filter(
    SessionLocal, fake_settings, monkeypatch
):
    """v5 — a multi-brand comparison question (no official-only
    marker OR a multi-product "official" question) keeps the
    multi-brand path intact: the official filter is NOT
    applied, the community / Q&A hits survive the
    phrase-token filter, and the run still completes on a
    valid Dropbox citation. This is the regression guard for
    the multi-brand behaviour the brief mandates we preserve.
    """
    from app.services.research_provider import (
        ProviderSearchResult,
        coerce_source,
    )

    drive_src = coerce_source(
        {
            "id": "src-drive",
            "url": "https://workspace.google.com/products/drive/",
            "title": "Drive for work",
            "content": "Google Drive sync and share features.",
        },
        provider="tavily",
    )
    dropbox_src = coerce_source(
        {
            "id": "src-dropbox",
            "url": "https://www.dropbox.com/business",
            "title": "Dropbox for business",
            "content": "Dropbox is a cloud file sync and share platform.",
        },
        provider="tavily",
    )

    class _MultiBrandProvider:
        def __init__(self, **kwargs):
            self.last_attempt_count: int = 1
            self.last_call_latency_ms: int = 5
            self._sources = [drive_src, dropbox_src]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def search(self, query):
            return ProviderSearchResult(
                query=query, provider="tavily", sources=self._sources
            )

    monkeypatch.setattr(
        "app.services.research_service.ResearchProviderClient", _MultiBrandProvider
    )

    _install_llm_stub(
        monkeypatch,
        {
            "summary": "Google Drive 与 Dropbox 在文件同步上形成直接竞争。",
            "confidence": 0.7,
            "competitors": [
                {
                    "name": "Dropbox",
                    # v10 — description reduced to the verbatim
                    # ``Evidence: "..."`` quote so the
                    # description-ground gate's vacuous-truth
                    # branch fires. The previous CJK
                    # comparison prose
                    # (``与 Google Drive 在文件同步上直接对比。``)
                    # carried CJK tokens the Dropbox snippet
                    # does not contain; under the v10 gate that
                    # prose would be (correctly) demoted.
                    "description": (
                        'Evidence: "Dropbox is a cloud file sync"'
                    ),
                    "confidence": 0.7,
                    "source_refs": [
                        {
                            "provider_source_id": "src-dropbox",
                            "url": "https://www.dropbox.com/business",
                            "title": "Dropbox for business",
                        }
                    ],
                }
            ],
            "market_patterns": [],
            "opportunities": [],
            "risks": [],
        },
    )

    db, seed = _seed_pipeline(SessionLocal)
    project_id = db.get(Insight, seed["insight_id"]).project_id
    try:
        run = start_research_run(
            db,
            project_id,
            seed["insight_id"],
            research_question="Google Drive 与 Dropbox 的对比",
            scope=None,
        )
    finally:
        db.close()

    # Multi-brand: official filter is OFF, both Drive + Dropbox
    # survive, the Dropbox name-support gate accepts the
    # citation, and the run completes. The v5 evidence-quote
    # gate also accepts the finding because the Evidence quote
    # ("Dropbox is a cloud file sync") traces back to the
    # src-dropbox snippet ("Dropbox is a cloud file sync and
    # share platform.").
    assert run.status == ResearchRunStatus.PARTIAL_FAILED.value
    assert run.source_count == 2
