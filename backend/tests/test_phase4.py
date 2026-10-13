"""Phase 4 — Opportunity Score + Human Decision tests.

The tests are organised to mirror the supervisor's brief:

1. Pure-function formulas (Coverage / Severity / Growth / Priority /
   total / effective_weights).
2. Request / response contracts and validation paths.
3. DB-level constraints (CHECK, FK, UNIQUE, immutable history rows).
4. Migration 0009 round-trip (the supervisor runs an isolated
   Alembic upgrade → downgrade → upgrade cycle; we cover the model
   shape here).
5. Structured Output client (Anthropic-compatible Messages API) with
   safe error categorisation; no upstream body or sentinel leaks.
6. Service-level score creation, Business Value recompute, raw data
   preservation, AI Priority immutability, draft / needs_validation
   source status preserved.
7. Decision state machine: accept, override, defer, reject, idem­
   potent request_id, optimistic-concurrency 409, append-only events.
8. HTTP API: 404/409/422/503, OpenAPI schema, paging.

The LLM client is stubbed everywhere — the real TCP HTTP / model
calls live in Codex's independent supervisor test plan.
"""

from __future__ import annotations

import os
import threading
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agents.prompts import opportunity_recommendation_v1 as prompt_module
from app.db.base import Base
from app.db.config import settings
from app.models import (
    AnalysisRun,
    Cluster,
    ClusterMember,
    Feedback,
    FeedbackAnalysis,
    Insight,
    InsightEvidence,
    InsightRun,
    InsightStatus,
    Project,
)
from app.models.opportunity import (
    DECISION_ACCEPTED,
    DECISION_DEFERRED,
    DECISION_OVERRIDDEN,
    DECISION_PENDING_REVIEW,
    DECISION_REJECTED,
    InsightDecision,
    InsightDecisionEvent,
    OpportunityScore,
)
from app.schemas.opportunity import OpportunityWeights
from app.services import opportunity_scoring as scoring
from app.services.opportunity_client import (
    TOOL_NAME,
    OpportunityLLMProtocolError,
    OpportunityLLMTransportError,
    _parse_messages_response,
)
from app.services.opportunity_scoring import (
    CALCULATION_VERSION,
    PROMPT_VERSION,
    DimensionValue,
    OpportunityConcurrencyError,
    OpportunityValidationError,
    PriorityThresholds,
    apply_decision_patch,
    calculate_total,
    coverage_dimension,
    create_opportunity_score,
    default_thresholds,
    default_weights,
    growth_dimension,
    map_priority,
    severity_dimension,
    validate_weights,
)

# ---- Fixtures ----------------------------------------------------------------


@pytest.fixture
def db(monkeypatch):
    """SQLite engine seeded for one Phase 4 test."""
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
    monkeypatch.setattr(settings, "llm_api_key", "test", raising=False)
    monkeypatch.setattr(settings, "llm_base_url", "http://invalid", raising=False)
    monkeypatch.setattr(settings, "llm_model", "test-model", raising=False)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    Base.metadata.drop_all(engine)
    # Shared Postgres suite schema isolation: the verification script
    # ``scripts/verify_postgres.py`` runs the whole test file against a
    # single PostgreSQL instance. ``drop_all`` leaves the database empty,
    # which breaks the post-suite ``alembic downgrade base`` step (no
    # indexes/PK to drop). Re-create the ORM schema on Postgres so the
    # schema after teardown mirrors the state Alembic owns; on SQLite
    # the file is ``sqlite://`` (in-memory) and a fresh empty schema is
    # correct.
    if engine.dialect.name == "postgresql":
        Base.metadata.create_all(engine)
    engine.dispose()


def _make_analysis_run(project_id, run_type="classification", **overrides) -> AnalysisRun:
    defaults = dict(
        project_id=project_id,
        run_type=run_type,
        model="test-model",
        prompt_version="test",
        status="completed",
        input_count=0,
        processed_count=0,
        success_count=0,
        failed_count=0,
    )
    defaults.update(overrides)
    return AnalysisRun(**defaults)


def _seed(db, *, feedback_count=3, with_user_ids=True, with_timestamps=True, days_ago=0):
    """Seed a project / classification / clustering / insight chain.

    Returns ``(project, classification_run, clustering_run, insight, feedback_ids)``.
    """
    project = Project(name="Phase4 worker", product_name="test")
    db.add(project)
    db.flush()
    classification = _make_analysis_run(project.id)
    db.add(classification)
    db.flush()
    clustering = _make_analysis_run(
        project.id,
        run_type="clustering",
        cluster_config={"classification_run_id": str(classification.id)},
    )
    db.add(clustering)
    db.flush()
    classification.input_feedback_external_ids = [f"ext-{i}" for i in range(feedback_count)]
    db.add(classification)
    db.flush()
    insight_run = InsightRun(
        project_id=project.id,
        clustering_run_id=clustering.id,
        classification_run_id=classification.id,
        status="completed",
        model="test-model",
        prompt_version="insight-generation-v2",
        input_count=1,
        processed_count=1,
        success_count=1,
        started_at=None,
        completed_at=None,
    )
    db.add(insight_run)
    db.flush()
    cluster = Cluster(
        project_id=project.id,
        run_id=clustering.id,
        source="llm_review",
        feedback_count=feedback_count,
        embedding_model="local-hash-v1",
        distance_threshold=0.3,
        linkage="average",
    )
    db.add(cluster)
    db.flush()
    feedback_ids: list = []
    user_ids: set[str] = set()
    from datetime import datetime, timedelta, timezone

    for index in range(feedback_count):
        user_id = f"U{index}" if with_user_ids else None
        if user_id:
            user_ids.add(user_id)
        if with_timestamps:
            feedback_created_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
        else:
            feedback_created_at = None
        feedback = Feedback(
            project_id=project.id,
            external_id=f"ext-{index}",
            content=f"phase4 feedback #{index}",
            user_id=user_id,
            feedback_created_at=feedback_created_at,
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
        db.add(
            ClusterMember(
                cluster_id=cluster.id,
                feedback_analysis_id=analysis.id,
                similarity_score=0.9,
            )
        )
        feedback_ids.append(feedback.id)
    insight = Insight(
        run_id=insight_run.id,
        project_id=project.id,
        cluster_id=cluster.id,
        title="多图选择痛点",
        pain_point="重复选择图片",
        affected_segment="移动端用户",
        insight="这些反馈反映逐张选图带来的重复操作。",
        recommendation="验证多选方案。",
        confidence=0.8,
        status=InsightStatus.DRAFT.value,
        feedback_count=feedback_count,
        evidence_count=0,
        source_cluster_external_id=str(cluster.id),
        source_member_external_ids=[f"ext-{i}" for i in range(feedback_count)],
        model="test-model",
        prompt_version="insight-generation-v2",
    )
    db.add(insight)
    db.flush()
    for fid in feedback_ids:
        db.add(
            InsightEvidence(
                insight_id=insight.id,
                feedback_id=fid,
                evidence_type="representative",
                relevance_score=0.9,
                feedback_external_id=f"ext-{feedback_ids.index(fid)}",
                feedback_project_id=project.id,
                feedback_classification_run_id=classification.id,
            )
        )
    insight.evidence_count = len(feedback_ids)
    db.flush()
    db.commit()
    return project, classification, clustering, insight, feedback_ids


# ---- Pure formulas -----------------------------------------------------------


def test_priority_mapping_boundaries():
    """Each threshold boundary maps to the documented Priority bucket."""
    thresholds = PriorityThresholds(p1_below=Decimal("5.00"), p0_below=Decimal("7.50"))
    assert map_priority(Decimal("0.00"), thresholds) == "P2"
    assert map_priority(Decimal("4.90"), thresholds) == "P2"
    assert map_priority(Decimal("4.99"), thresholds) == "P2"
    assert map_priority(Decimal("5.00"), thresholds) == "P1"
    assert map_priority(Decimal("7.40"), thresholds) == "P1"
    assert map_priority(Decimal("7.49"), thresholds) == "P1"
    assert map_priority(Decimal("7.50"), thresholds) == "P0"
    assert map_priority(Decimal("10.00"), thresholds) == "P0"


def test_severity_dimension_with_sample_reliability():
    one = severity_dimension(average_severity=Decimal("5"), feedback_count=1)
    assert one.score == Decimal("3.33")
    assert one.raw_value == Decimal("10.00")
    three = severity_dimension(average_severity=Decimal("5"), feedback_count=3)
    assert three.score == Decimal("10.00")
    low = severity_dimension(average_severity=Decimal("1"), feedback_count=10)
    assert low.score == Decimal("0.00")
    missing = severity_dimension(average_severity=Decimal("3"), feedback_count=0)
    assert missing.available is False


def test_coverage_dimension_unavailable_without_user_ids():
    result = coverage_dimension(affected_unique_users=0, project_unique_users=0)
    assert result.available is False
    assert result.score is None
    filled = coverage_dimension(affected_unique_users=5, project_unique_users=10)
    assert filled.available is True
    assert filled.score == Decimal("5.00")


def test_growth_dimension_unavailable_insufficient_window():
    missing = growth_dimension(
        recent_count=0,
        previous_count=0,
        has_recent=False,
        has_previous=False,
    )
    assert missing.available is False
    zero_previous = growth_dimension(
        recent_count=3,
        previous_count=0,
        has_recent=True,
        has_previous=True,
    )
    assert zero_previous.available is False
    doubled = growth_dimension(
        recent_count=6,
        previous_count=3,
        has_recent=True,
        has_previous=True,
    )
    assert doubled.available is True
    assert doubled.score == Decimal("10.00")
    halved = growth_dimension(
        recent_count=2,
        previous_count=4,
        has_recent=True,
        has_previous=True,
    )
    assert halved.available is True
    assert halved.score == Decimal("2.50")


def test_validate_weights_rejects_non_sum_one():
    with pytest.raises(OpportunityValidationError):
        validate_weights(
            {
                "user_coverage": Decimal("0.4"),
                "severity": Decimal("0.4"),
                "growth": Decimal("0.1"),
                "user_value": Decimal("0.1"),
                "business_value": Decimal("0.1"),
            }
        )


def test_validate_weights_rejects_nan_and_inf():
    bad = {
        "user_coverage": Decimal("0.25"),
        "severity": Decimal("0.25"),
        "growth": Decimal("0.15"),
        "user_value": Decimal("0.20"),
        "business_value": Decimal("0.15"),
    }
    bad["growth"] = Decimal("NaN")
    with pytest.raises(OpportunityValidationError):
        validate_weights(bad)


def test_validate_weights_rejects_negative_or_over_one():
    with pytest.raises(OpportunityValidationError):
        validate_weights(
            {
                "user_coverage": Decimal("-0.1"),
                "severity": Decimal("0.5"),
                "growth": Decimal("0.2"),
                "user_value": Decimal("0.2"),
                "business_value": Decimal("0.2"),
            }
        )


def test_validate_weights_rejects_unknown_dimensions():
    with pytest.raises(OpportunityValidationError):
        validate_weights(
            {
                "user_coverage": Decimal("0.25"),
                "severity": Decimal("0.25"),
                "growth": Decimal("0.15"),
                "user_value": Decimal("0.20"),
                "business_value": Decimal("0.15"),
                "mystery": Decimal("0"),
            }
        )


def test_calculate_total_excludes_growth_when_unavailable():
    """When one or more non-Business dimensions are unavailable, the
    Business Value configured weight is preserved verbatim and the
    available non-Business dimensions share the remaining
    ``(1 - business_weight)`` budget in proportion to their configured
    weights. Effective weights must sum to ``1``.
    """
    breakdown = {
        "user_coverage": DimensionValue(
            raw_value=Decimal("5.00"), score=Decimal("5.00"), available=True
        ),
        "severity": DimensionValue(
            raw_value=Decimal("10.00"), score=Decimal("10.00"), available=True
        ),
        "growth": DimensionValue(raw_value=None, score=None, available=False),
        "user_value": DimensionValue(
            raw_value=Decimal("6.00"), score=Decimal("6.00"), available=True
        ),
        "business_value": DimensionValue(
            raw_value=Decimal("8.00"), score=Decimal("8.00"), available=True
        ),
    }
    weights = default_weights()
    calc = calculate_total(
        dimensions=breakdown,
        weights=weights,
        business_value=Decimal("8.00"),
        thresholds=default_thresholds(),
    )
    assert calc.total_score is not None
    business_weight = Decimal(str(settings.opportunity_weight_business_value))
    other_configured = (
        Decimal(str(settings.opportunity_weight_user_coverage))
        + Decimal(str(settings.opportunity_weight_severity))
        + Decimal(str(settings.opportunity_weight_user_value))
    )
    scale = (Decimal("1") - business_weight) / other_configured
    expected = (
        business_weight * Decimal("8.00")
        + Decimal(str(settings.opportunity_weight_user_coverage)) * scale * Decimal("5.00")
        + Decimal(str(settings.opportunity_weight_severity)) * scale * Decimal("10.00")
        + Decimal(str(settings.opportunity_weight_user_value)) * scale * Decimal("6.00")
    ).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
    assert calc.total_score == expected
    # Spec example: the same five-dimension numbers produce 7.21.
    assert calc.total_score == Decimal("7.21")
    assert calc.effective_weights["growth"] == Decimal("0")
    # BV configured weight is preserved verbatim.
    assert calc.effective_weights["business_value"] == business_weight
    # All effective weights sum to 1 (to a 1e-9 Decimal tolerance).
    weight_sum = sum(calc.effective_weights.values())
    assert abs(weight_sum - Decimal("1")) < Decimal("1e-9")


def test_calculate_total_renormalises_when_multiple_dimensions_unavailable():
    """Two unavailable non-Business dimensions must still leave the BV
    configured weight intact and redistribute the remaining budget to
    the still-available non-Business dimensions proportionally to their
    configured weights."""
    breakdown = {
        "user_coverage": DimensionValue(
            raw_value=Decimal("7.00"), score=Decimal("7.00"), available=True
        ),
        "severity": DimensionValue(raw_value=None, score=None, available=False),
        "growth": DimensionValue(raw_value=None, score=None, available=False),
        "user_value": DimensionValue(
            raw_value=Decimal("9.00"), score=Decimal("9.00"), available=True
        ),
        "business_value": DimensionValue(
            raw_value=Decimal("6.00"), score=Decimal("6.00"), available=True
        ),
    }
    weights = default_weights()
    calc = calculate_total(
        dimensions=breakdown,
        weights=weights,
        business_value=Decimal("6.00"),
        thresholds=default_thresholds(),
    )
    business_weight = Decimal(str(settings.opportunity_weight_business_value))
    coverage_weight = Decimal(str(settings.opportunity_weight_user_coverage))
    user_value_weight = Decimal(str(settings.opportunity_weight_user_value))
    scale = (Decimal("1") - business_weight) / (coverage_weight + user_value_weight)
    expected = (
        business_weight * Decimal("6.00")
        + coverage_weight * scale * Decimal("7.00")
        + user_value_weight * scale * Decimal("9.00")
    ).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
    assert calc.total_score == expected
    weight_sum = sum(calc.effective_weights.values())
    assert abs(weight_sum - Decimal("1")) < Decimal("1e-9")
    assert calc.effective_weights["business_value"] == business_weight
    assert calc.effective_weights["severity"] == Decimal("0")
    assert calc.effective_weights["growth"] == Decimal("0")


def test_calculate_total_collapses_to_bv_when_all_nonbusiness_unavailable():
    """When every non-Business dimension is unavailable, the BV
    effective weight collapses to 1 and the total equals the BV score."""
    breakdown = {
        "user_coverage": DimensionValue(raw_value=None, score=None, available=False),
        "severity": DimensionValue(raw_value=None, score=None, available=False),
        "growth": DimensionValue(raw_value=None, score=None, available=False),
        "user_value": DimensionValue(raw_value=None, score=None, available=False),
        "business_value": DimensionValue(
            raw_value=Decimal("9.00"), score=Decimal("9.00"), available=True
        ),
    }
    calc = calculate_total(
        dimensions=breakdown,
        weights=default_weights(),
        business_value=Decimal("9.00"),
        thresholds=default_thresholds(),
    )
    assert calc.total_score == Decimal("9.00")
    assert calc.effective_weights["business_value"] == Decimal("1")
    weight_sum = sum(calc.effective_weights.values())
    assert abs(weight_sum - Decimal("1")) < Decimal("1e-9")
    for dim in ("user_coverage", "severity", "growth", "user_value"):
        assert calc.effective_weights[dim] == Decimal("0")


def test_calculate_total_returns_none_when_business_missing():
    breakdown = {
        "user_coverage": DimensionValue(
            raw_value=Decimal("5.00"), score=Decimal("5.00"), available=True
        ),
        "severity": DimensionValue(
            raw_value=Decimal("10.00"), score=Decimal("10.00"), available=True
        ),
        "growth": DimensionValue(raw_value=None, score=None, available=False),
        "user_value": DimensionValue(
            raw_value=Decimal("6.00"), score=Decimal("6.00"), available=True
        ),
        "business_value": DimensionValue(raw_value=None, score=None, available=False),
    }
    calc = calculate_total(
        dimensions=breakdown,
        weights=default_weights(),
        business_value=None,
        thresholds=default_thresholds(),
    )
    assert calc.total_score is None
    assert calc.ai_priority is None


# ---- LLM protocol ------------------------------------------------------------


def _tool_block(name=TOOL_NAME, **overrides):
    block = {
        "type": "tool_use",
        "name": name,
        "input": {
            "user_value_score": 5.0,
            "reasoning": "基于证据的有限说明",
        },
    }
    block["input"].update(overrides)
    return block


def test_parser_accepts_valid_tool_block():
    data = {
        "content": [_tool_block()],
        "usage": {"input_tokens": 10, "output_tokens": 4},
    }
    result = _parse_messages_response(data)
    assert result.payload["user_value_score"] == 5.0
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 4


def test_parser_sums_cache_tokens():
    data = {
        "content": [_tool_block()],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 4,
            "cache_creation_input_tokens": 5,
            "cache_read_input_tokens": 3,
        },
    }
    result = _parse_messages_response(data)
    assert result.usage.input_tokens == 18
    assert result.usage.output_tokens == 4


def test_parser_rejects_missing_tool_block():
    with pytest.raises(OpportunityLLMProtocolError):
        _parse_messages_response({"content": [{"type": "text", "text": "no tool"}]})


def test_parser_rejects_wrong_tool_name():
    with pytest.raises(OpportunityLLMProtocolError):
        _parse_messages_response({"content": [_tool_block(name="other_tool")], "usage": {}})


def test_parser_rejects_multiple_tool_blocks():
    data = {"content": [_tool_block(), _tool_block()], "usage": {}}
    with pytest.raises(OpportunityLLMProtocolError):
        _parse_messages_response(data)


def test_parser_rejects_non_object_input():
    block = _tool_block()
    block["input"] = "not an object"
    with pytest.raises(OpportunityLLMProtocolError):
        _parse_messages_response({"content": [block], "usage": {}})


def test_parser_rejects_unknown_fields_and_invalid_numbers():
    block = _tool_block(quote="fabricated")
    with pytest.raises(OpportunityLLMProtocolError):
        _parse_messages_response({"content": [block], "usage": {}})
    block = _tool_block(user_value_score=float("nan"))
    with pytest.raises(OpportunityLLMProtocolError):
        _parse_messages_response({"content": [block], "usage": {}})
    block = _tool_block(user_value_score=11)
    with pytest.raises(OpportunityLLMProtocolError):
        _parse_messages_response({"content": [block], "usage": {}})


def test_parser_handles_malformed_usage():
    block = _tool_block()
    with pytest.raises(OpportunityLLMProtocolError):
        _parse_messages_response({"content": [block], "usage": "not-a-dict"})
    # Empty usage is allowed and counts as unknown.
    result = _parse_messages_response({"content": [_tool_block()], "usage": {}})
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None


def test_parser_keeps_error_message_safe():
    sentinel = "CUSTOM_PRIVATE_SENTINEL_KEY=abcd1234"
    with pytest.raises(OpportunityLLMProtocolError) as ei:
        _parse_messages_response(
            {
                "content": [_tool_block(name=sentinel)],
                "usage": {},
            }
        )
    assert sentinel not in str(ei.value)


def test_prompt_forbids_business_metrics_and_unobserved_populations():
    prompt = prompt_module.SYSTEM_PROMPT
    forbidden = [
        "收入损失",
        "流失比例",
        "留存变化",
        "市场规模",
        "NPS",
        "付费转化",
        "未给定人群",
        "根因",
    ]
    for phrase in forbidden:
        assert phrase in prompt, f"prompt must explicitly forbid {phrase!r}"
    assert '"unknown"' in prompt or "未在输入中出现" in prompt


def test_prompt_tool_schema_is_strict():
    schema = prompt_module.TOOL_SCHEMA
    assert schema["additionalProperties"] is False
    keys = set(schema["properties"].keys())
    assert keys == {"user_value_score", "reasoning"}


# ---- Service layer -----------------------------------------------------------


class _StubOpportunityClient:
    """Stub client that records calls and returns a deterministic payload."""

    calls: list = []
    fail_with: Exception | None = None
    next_payload: dict | None = None

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def generate_user_value(self, payload):
        self.calls.append(payload)
        if self.fail_with is not None:
            raise self.fail_with
        return SimpleNamespace(
            payload=self.next_payload or {"user_value_score": 7.0, "reasoning": "stub reasoning"},
            usage=SimpleNamespace(input_tokens=11, output_tokens=5),
        )


@pytest.fixture
def stub_client(monkeypatch):
    _StubOpportunityClient.calls = []
    _StubOpportunityClient.fail_with = None
    _StubOpportunityClient.next_payload = None
    monkeypatch.setattr(scoring, "OpportunityClient", _StubOpportunityClient)
    return _StubOpportunityClient


def _all_feedback_user_ids(db, project_id) -> set[str]:
    return {
        row[0]
        for row in db.execute(
            select(Feedback.user_id).where(Feedback.project_id == project_id)
        ).all()
        if row[0]
    }


def test_create_score_uses_user_ids_for_coverage(db, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    outcome = create_opportunity_score(db, insight, business_value=Decimal("8.0"))
    score = outcome.score
    coverage = score.breakdown["user_coverage"]
    assert coverage["available"] is True
    project_users = _all_feedback_user_ids(db, insight.project_id)
    expected_ratio = Decimal(len(project_users & set(["U0", "U1", "U2"]))) / Decimal(
        len(project_users)
    )
    expected_score = _round_two(expected_ratio * Decimal("10"))
    assert Decimal(coverage["score"]) == expected_score
    assert score.total_score is not None
    assert score.ai_priority in {"P0", "P1", "P2"}


def _round_two(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")


def test_create_score_marks_coverage_unavailable_when_no_user_ids(db, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3, with_user_ids=False)
    outcome = create_opportunity_score(db, insight, business_value=Decimal("8.0"))
    coverage = outcome.score.breakdown["user_coverage"]
    assert coverage["available"] is False


def test_create_score_preserves_insight_status(db, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    insight.status = InsightStatus.NEEDS_VALIDATION.value
    db.commit()
    outcome = create_opportunity_score(db, insight, business_value=Decimal("8.0"))
    assert outcome.score.source_insight_status == InsightStatus.NEEDS_VALIDATION.value
    assert outcome.decision.status == DECISION_PENDING_REVIEW


def test_create_score_without_business_value_keeps_total_null(db, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    outcome = create_opportunity_score(db, insight, business_value=None)
    assert outcome.score.total_score is None
    assert outcome.score.ai_priority is None
    assert outcome.score.business_value is None
    assert outcome.decision.status == DECISION_PENDING_REVIEW


def test_create_score_with_business_value_runs_full_formula(db, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    outcome = create_opportunity_score(db, insight, business_value=Decimal("9.0"))
    score = outcome.score
    assert score.total_score is not None
    assert score.business_value == 9.0
    assert score.ai_priority is not None


def test_create_score_recomputes_when_business_value_changes(db, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    first = create_opportunity_score(db, insight, business_value=Decimal("4.0"))
    second = create_opportunity_score(db, insight, business_value=Decimal("9.0"))
    assert first.score.id != second.score.id
    assert (
        first.score.ai_priority != second.score.ai_priority
        or first.score.business_value != second.score.business_value
    )
    history = db.scalars(select(OpportunityScore)).all()
    assert len(history) == 2
    # Decision points at the latest snapshot.
    decision = db.scalar(select(InsightDecision).where(InsightDecision.insight_id == insight.id))
    assert decision.current_score_id == second.score.id
    assert decision.version == 2


def test_raw_feedback_unchanged_after_score(db, stub_client):
    _, _, _, insight, feedback_ids = _seed(db, feedback_count=4)
    raw_snapshot = {
        str(f.id): (f.content, f.user_id, f.feedback_created_at)
        for f in db.scalars(select(Feedback)).all()
    }
    analysis_snapshot = {
        str(a.id): (a.severity, a.feedback_type, a.module)
        for a in db.scalars(select(FeedbackAnalysis)).all()
    }
    create_opportunity_score(db, insight, business_value=Decimal("7.5"))
    after_feedback = {
        str(f.id): (f.content, f.user_id, f.feedback_created_at)
        for f in db.scalars(select(Feedback)).all()
    }
    after_analysis = {
        str(a.id): (a.severity, a.feedback_type, a.module)
        for a in db.scalars(select(FeedbackAnalysis)).all()
    }
    assert raw_snapshot == after_feedback
    assert analysis_snapshot == after_analysis
    # Evidence rows untouched.
    assert db.scalars(select(InsightEvidence).where(InsightEvidence.insight_id == insight.id)).all()


def test_ai_priority_never_overwritten_by_pm(db, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    outcome = create_opportunity_score(db, insight, business_value=Decimal("9.0"))
    original_ai_priority = outcome.score.ai_priority
    # Bypass scoring to keep test deterministic.
    from app.models.opportunity import InsightDecision, InsightDecisionEvent

    decision = db.scalar(select(InsightDecision).where(InsightDecision.insight_id == insight.id))
    decision.pm_priority = "P0" if original_ai_priority == "P2" else "P2"
    decision.status = DECISION_OVERRIDDEN
    decision.override_reason = "strategic_priority"
    db.add(
        InsightDecisionEvent(
            insight_id=insight.id,
            decision_id=decision.id,
            score_id=decision.current_score_id,
            request_id=uuid4(),
            event_type="override",
            before_state={},
            after_state={},
            business_value=decision.business_value,
            ai_priority=original_ai_priority,
            pm_priority=decision.pm_priority,
            override_reason=decision.override_reason,
            version_after=decision.version,
        )
    )
    db.commit()
    score = db.get(OpportunityScore, outcome.score.id)
    assert score.ai_priority == original_ai_priority


def test_overwrite_request_rejected_by_expected_version(db, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    create_opportunity_score(db, insight, business_value=Decimal("7.0"))
    with pytest.raises(OpportunityConcurrencyError):
        apply_decision_patch(
            db,
            insight,
            request_id=uuid4(),
            expected_version=999,
            action="accept",
            business_value=None,
            pm_priority=None,
            override_reason=None,
            override_note=None,
        )


def test_decision_state_machine_accepts(db, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    create_opportunity_score(db, insight, business_value=Decimal("8.0"))
    decision = apply_decision_patch(
        db,
        insight,
        request_id=uuid4(),
        expected_version=1,
        action="accept",
        business_value=None,
        pm_priority=None,
        override_reason=None,
        override_note=None,
    )
    assert decision.status == DECISION_ACCEPTED
    assert decision.pm_priority == decision.ai_priority


def test_decision_state_machine_overrides_require_reason_and_note(db, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    outcome = create_opportunity_score(db, insight, business_value=Decimal("8.0"))
    # ``create_opportunity_score`` is flush-only; the four PATCHes
    # below exercise atomic rollback on validation failure, so the
    # initial score + decision must be persisted first or the first
    # rollback would wipe the baseline.
    db.commit()
    ai_priority = outcome.score.ai_priority
    assert ai_priority in {"P0", "P1", "P2"}
    same_priority = ai_priority

    def _different(ai: str) -> str:
        return next(p for p in ("P0", "P1", "P2") if p != ai)

    other_priority = _different(ai_priority)
    # Same priority — must be rejected.
    with pytest.raises(OpportunityValidationError):
        apply_decision_patch(
            db,
            insight,
            request_id=uuid4(),
            expected_version=1,
            action="override",
            business_value=None,
            pm_priority=same_priority,
            override_reason="strategic_priority",
            override_note=None,
        )
    # Missing reason.
    with pytest.raises(OpportunityValidationError):
        apply_decision_patch(
            db,
            insight,
            request_id=uuid4(),
            expected_version=1,
            action="override",
            business_value=None,
            pm_priority=other_priority,
            override_reason=None,
            override_note=None,
        )
    # 'other' without note.
    with pytest.raises(OpportunityValidationError):
        apply_decision_patch(
            db,
            insight,
            request_id=uuid4(),
            expected_version=1,
            action="override",
            business_value=None,
            pm_priority=other_priority,
            override_reason="other",
            override_note=None,
        )
    # Valid override path.
    decision = apply_decision_patch(
        db,
        insight,
        request_id=uuid4(),
        expected_version=1,
        action="override",
        business_value=None,
        pm_priority=other_priority,
        override_reason="other",
        override_note="PM 临时战略决定",
    )
    assert decision.status == DECISION_OVERRIDDEN
    assert decision.override_reason == "other"
    assert decision.override_note == "PM 临时战略决定"


def test_decision_state_machine_defer_and_reject_keep_ai_priority(db, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    create_opportunity_score(db, insight, business_value=Decimal("8.0"))
    deferred = apply_decision_patch(
        db,
        insight,
        request_id=uuid4(),
        expected_version=1,
        action="defer",
        business_value=None,
        pm_priority=None,
        override_reason=None,
        override_note=None,
    )
    assert deferred.status == DECISION_DEFERRED
    assert deferred.pm_priority is None
    rejected = apply_decision_patch(
        db,
        insight,
        request_id=uuid4(),
        expected_version=2,
        action="reject",
        business_value=None,
        pm_priority=None,
        override_reason=None,
        override_note=None,
    )
    assert rejected.status == DECISION_REJECTED


def test_decision_history_records_every_patch(db, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    create_opportunity_score(db, insight, business_value=Decimal("8.0"))
    apply_decision_patch(
        db,
        insight,
        request_id=uuid4(),
        expected_version=1,
        action="accept",
        business_value=None,
        pm_priority=None,
        override_reason=None,
        override_note=None,
    )
    events = db.scalars(select(InsightDecisionEvent)).all()
    assert len(events) == 1
    assert events[0].event_type == "accept"


def test_decision_request_id_is_idempotent(db, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    create_opportunity_score(db, insight, business_value=Decimal("8.0"))
    request_id = uuid4()
    apply_decision_patch(
        db,
        insight,
        request_id=request_id,
        expected_version=1,
        action="accept",
        business_value=None,
        pm_priority=None,
        override_reason=None,
        override_note=None,
    )
    apply_decision_patch(
        db,
        insight,
        request_id=request_id,
        expected_version=2,
        action="accept",
        business_value=None,
        pm_priority=None,
        override_reason=None,
        override_note=None,
    )
    events = db.scalars(select(InsightDecisionEvent)).all()
    assert len(events) == 1


def test_decision_set_business_value_creates_new_score(db, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    create_opportunity_score(db, insight, business_value=Decimal("4.0"))
    decision = apply_decision_patch(
        db,
        insight,
        request_id=uuid4(),
        expected_version=1,
        action="set_business_value",
        business_value=Decimal("9.0"),
        pm_priority=None,
        override_reason=None,
        override_note=None,
    )
    assert decision.status == DECISION_PENDING_REVIEW
    assert decision.pm_priority is None
    assert decision.business_value == 9.0
    scores = db.scalars(select(OpportunityScore)).all()
    assert len(scores) == 2


def test_set_business_value_bumps_version_once(db, stub_client):
    """set_business_value creates one new score row and bumps the
    decision version exactly once (no double increment from
    create_opportunity_score + apply_decision_patch)."""
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    create_opportunity_score(db, insight, business_value=Decimal("4.0"))
    before = db.scalar(select(InsightDecision).where(InsightDecision.insight_id == insight.id))
    before_version = before.version
    apply_decision_patch(
        db,
        insight,
        request_id=uuid4(),
        expected_version=before_version,
        action="set_business_value",
        business_value=Decimal("9.0"),
        pm_priority=None,
        override_reason=None,
        override_note=None,
    )
    after = db.scalar(select(InsightDecision).where(InsightDecision.insight_id == insight.id))
    assert after.version == before_version + 1
    rows = db.scalars(select(OpportunityScore)).all()
    assert len(rows) == 2


def test_set_business_value_preserves_current_score_weights(db, stub_client):
    """When a Decision already carries a score with non-default
    configured weights, a subsequent ``set_business_value`` PATCH must
    re-validate those exact weights via ``validate_weights`` and reuse
    them for the new score, NOT silently fall back to defaults. The
    historically persisted preview keeps its own weights verbatim.
    """
    custom_weights = OpportunityWeights(
        user_coverage=Decimal("0.30"),
        severity=Decimal("0.30"),
        growth=Decimal("0.10"),
        user_value=Decimal("0.20"),
        business_value=Decimal("0.10"),
    )
    validated_custom = validate_weights(custom_weights.model_dump())
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    preview = create_opportunity_score(
        db,
        insight,
        weights=validated_custom,
        business_value=None,
    )
    preview_hash = preview.score.source_snapshot_hash
    preview_weights = preview.score.weights_configured
    assert preview_weights == {
        "user_coverage": "0.30",
        "severity": "0.30",
        "growth": "0.10",
        "user_value": "0.20",
        "business_value": "0.10",
    }

    decision = apply_decision_patch(
        db,
        insight,
        request_id=uuid4(),
        expected_version=1,
        action="set_business_value",
        business_value=Decimal("7.5"),
        pm_priority=None,
        override_reason=None,
        override_note=None,
    )
    assert decision.business_value == 7.5
    assert decision.version == 2

    rows = db.scalars(
        select(OpportunityScore)
        .where(OpportunityScore.insight_id == insight.id)
        .order_by(OpportunityScore.created_at.asc(), OpportunityScore.id.asc())
    ).all()
    assert len(rows) == 2
    historical, new = rows
    # Historical preview is untouched.
    assert historical.id == preview.score.id
    assert historical.source_snapshot_hash == preview_hash
    assert historical.weights_configured == preview_weights
    # New score carries the exact same configured weights as the prior
    # snapshot; no silent fallback to defaults.
    assert new.weights_configured == preview_weights
    assert new.business_value == 7.5
    assert new.total_score is not None
    assert new.ai_priority is not None

    # And the effective weights are renormalised against the SAME
    # configured weights as the preview.
    assert set(new.weights_effective.keys()) == {
        "user_coverage",
        "severity",
        "growth",
        "user_value",
        "business_value",
    }
    assert Decimal(new.weights_effective["business_value"]) == Decimal("0.10")


# ---- HTTP layer --------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    """FastAPI test client wired to a private SQLite engine."""
    engine = create_engine(os.environ.get("TEST_DATABASE_URL", f"sqlite:///{tmp_path}/phase4.db"))
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def foreign_keys(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)

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
        yield c, engine
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)
    # Shared Postgres suite schema isolation: re-create the ORM schema
    # after teardown so the database still matches the Alembic-managed
    # state expected by ``scripts/verify_postgres.py`` (specifically the
    # post-suite ``alembic downgrade base``). On SQLite the engine uses
    # a private temp file that is discarded either way.
    if engine.dialect.name == "postgresql":
        Base.metadata.create_all(engine)
    engine.dispose()


def _http_seed(client, engine) -> UUID:
    """Seed a project / classification / clustering / insight chain via the
    shared engine so the HTTP client and direct DB writes see the same
    state. Returns the persisted Insight UUID so the HTTP routes can be
    exercised without re-running the LLM pipeline.
    """
    with Session(engine) as session:
        _, _, _, insight, _ = _seed(session)
        return insight.id


def test_post_opportunity_score_creates_preview_with_null_total(client, monkeypatch):
    """POST without body creates a preview score: business_value=null,
    total_score=null, ai_priority=null. The PM must use PATCH
    set_business_value to set BV and obtain a full score."""
    c, engine = client
    monkeypatch.setattr(scoring, "OpportunityClient", _StubOpportunityClient)
    _StubOpportunityClient.calls = []
    insight_id = _http_seed(c, engine)
    response = c.post(f"/insights/{insight_id}/opportunity-score")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["business_value"] is None
    assert body["total_score"] is None
    assert body["ai_priority"] is None
    assert body["source_insight_status"] in {"draft", "needs_validation"}
    assert body["calculation_version"] == CALCULATION_VERSION
    assert body["prompt_version"] == PROMPT_VERSION


def test_post_opportunity_score_unknown_insight_returns_404(client):
    c, _ = client
    response = c.post(f"/insights/{uuid4()}/opportunity-score")
    assert response.status_code == 404


def test_post_opportunity_score_rejects_business_value_in_body(client):
    """POST body must NOT carry Business Value — extra=forbid forces
    PATCH set_business_value to be the only BV writer. A POST body
    containing ``business_value`` must surface as 422 and must not
    create any event row or persist any BV."""
    c, engine = client
    insight_id = _http_seed(c, engine)
    response = c.post(
        f"/insights/{insight_id}/opportunity-score",
        json={"business_value": "11.0"},
    )
    assert response.status_code == 422
    with Session(engine) as verify:
        scores = verify.scalars(select(OpportunityScore)).all()
        decisions = verify.scalars(select(InsightDecision)).all()
        events = verify.scalars(select(InsightDecisionEvent)).all()
    assert scores == []
    assert decisions == []
    assert events == []


def test_post_opportunity_score_then_set_business_value_then_accept(client, monkeypatch):
    """Full lifecycle: POST preview (decision v1 pending) → PATCH
    set_business_value (v2, full score) → PATCH accept (v3, status
    accepted). All three steps must succeed and produce one event."""
    c, engine = client
    monkeypatch.setattr(scoring, "OpportunityClient", _StubOpportunityClient)
    _StubOpportunityClient.calls = []
    insight_id = _http_seed(c, engine)
    preview = c.post(f"/insights/{insight_id}/opportunity-score").json()
    assert preview["total_score"] is None
    decision = c.get(f"/insights/{insight_id}/decision").json()
    assert decision["status"] == "pending_review"
    assert decision["version"] == 1

    set_bv = c.patch(
        f"/insights/{insight_id}/decision",
        json={
            "request_id": str(uuid4()),
            "expected_version": 1,
            "action": "set_business_value",
            "business_value": "8.0",
        },
    )
    assert set_bv.status_code == 200, set_bv.text
    bv_decision = set_bv.json()
    assert bv_decision["version"] == 2
    assert bv_decision["business_value"] == "8.00"

    accept = c.patch(
        f"/insights/{insight_id}/decision",
        json={
            "request_id": str(uuid4()),
            "expected_version": 2,
            "action": "accept",
        },
    )
    assert accept.status_code == 200, accept.text
    accept_decision = accept.json()
    assert accept_decision["version"] == 3
    assert accept_decision["status"] == "accepted"

    history = c.get(f"/insights/{insight_id}/decision/history").json()
    assert history["total"] == 2
    events = {row["event_type"] for row in history["items"]}
    assert events == {"set_business_value", "accept"}


def test_get_latest_score_returns_404_when_missing(client):
    c, engine = client
    insight_id = _http_seed(c, engine)
    response = c.get(f"/insights/{insight_id}/opportunity-score")
    assert response.status_code == 404


def test_get_score_history_paging(client, monkeypatch):
    c, engine = client
    monkeypatch.setattr(scoring, "OpportunityClient", _StubOpportunityClient)
    _StubOpportunityClient.calls = []
    insight_id = _http_seed(c, engine)
    # POST preview (no body) once → decision v1 → drive three PATCH
    # set_business_value calls (v2, v3, v4) so the history has 4 scores.
    c.post(f"/insights/{insight_id}/opportunity-score")
    expected_version = 1
    for bv in ("4.0", "7.0", "9.0"):
        expected_version += 1
        resp = c.patch(
            f"/insights/{insight_id}/decision",
            json={
                "request_id": str(uuid4()),
                "expected_version": expected_version - 1,
                "action": "set_business_value",
                "business_value": bv,
            },
        )
        assert resp.status_code == 200, resp.text
    response = c.get(
        f"/insights/{insight_id}/opportunity-scores",
        params={"limit": 2, "offset": 0},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 4
    assert len(body["items"]) == 2


def test_decision_flow_accepts(client, monkeypatch):
    c, engine = client
    monkeypatch.setattr(scoring, "OpportunityClient", _StubOpportunityClient)
    _StubOpportunityClient.calls = []
    insight_id = _http_seed(c, engine)
    # First POST preview, then PATCH set_business_value, then PATCH accept.
    c.post(f"/insights/{insight_id}/opportunity-score")
    set_resp = c.patch(
        f"/insights/{insight_id}/decision",
        json={
            "request_id": str(uuid4()),
            "expected_version": 1,
            "action": "set_business_value",
            "business_value": "8.0",
        },
    ).json()
    request_id = str(uuid4())
    decision = c.patch(
        f"/insights/{insight_id}/decision",
        json={
            "request_id": request_id,
            "expected_version": 2,
            "action": "accept",
        },
    )
    assert decision.status_code == 200, decision.text
    body = decision.json()
    assert body["status"] == "accepted"
    assert body["pm_priority"] == body["ai_priority"] == set_resp["ai_priority"]
    history = c.get(f"/insights/{insight_id}/decision/history").json()
    assert history["total"] == 2


def test_decision_flow_override_requires_note(client, monkeypatch):
    c, engine = client
    monkeypatch.setattr(scoring, "OpportunityClient", _StubOpportunityClient)
    _StubOpportunityClient.calls = []
    insight_id = _http_seed(c, engine)
    c.post(f"/insights/{insight_id}/opportunity-score")
    c.patch(
        f"/insights/{insight_id}/decision",
        json={
            "request_id": str(uuid4()),
            "expected_version": 1,
            "action": "set_business_value",
            "business_value": "8.0",
        },
    )
    response = c.patch(
        f"/insights/{insight_id}/decision",
        json={
            "request_id": str(uuid4()),
            "expected_version": 2,
            "action": "override",
            "pm_priority": "P0",
            "override_reason": "other",
            "override_note": "    ",
        },
    )
    assert response.status_code == 422


def test_decision_flow_409_on_expected_version_mismatch(client, monkeypatch):
    c, engine = client
    monkeypatch.setattr(scoring, "OpportunityClient", _StubOpportunityClient)
    _StubOpportunityClient.calls = []
    insight_id = _http_seed(c, engine)
    c.post(f"/insights/{insight_id}/opportunity-score")
    response = c.patch(
        f"/insights/{insight_id}/decision",
        json={
            "request_id": str(uuid4()),
            "expected_version": 99,
            "action": "accept",
        },
    )
    assert response.status_code == 409


def test_decision_flow_idempotent_request_id(client, monkeypatch):
    c, engine = client
    monkeypatch.setattr(scoring, "OpportunityClient", _StubOpportunityClient)
    _StubOpportunityClient.calls = []
    insight_id = _http_seed(c, engine)
    c.post(f"/insights/{insight_id}/opportunity-score")
    c.patch(
        f"/insights/{insight_id}/decision",
        json={
            "request_id": str(uuid4()),
            "expected_version": 1,
            "action": "set_business_value",
            "business_value": "8.0",
        },
    )
    request_id = str(uuid4())
    body = {
        "request_id": request_id,
        "expected_version": 2,
        "action": "accept",
    }
    first = c.patch(f"/insights/{insight_id}/decision", json=body)
    second = c.patch(f"/insights/{insight_id}/decision", json=body)
    assert first.status_code == 200
    assert second.status_code == 200
    history = c.get(f"/insights/{insight_id}/decision/history").json()
    assert history["total"] == 2  # set_business_value + accept only


def test_openapi_contains_phase4_paths(client):
    c, _ = client
    schema = c.get("/openapi.json").json()
    paths = schema["paths"].keys()
    assert "/insights/{insight_id}/opportunity-score" in paths
    assert "/insights/{insight_id}/opportunity-scores" in paths
    assert "/insights/{insight_id}/decision" in paths
    assert "/insights/{insight_id}/decision/history" in paths


def test_score_breakdown_includes_dimension_payload(client, monkeypatch):
    c, engine = client
    monkeypatch.setattr(scoring, "OpportunityClient", _StubOpportunityClient)
    _StubOpportunityClient.calls = []
    insight_id = _http_seed(c, engine)
    response = c.post(f"/insights/{insight_id}/opportunity-score")
    body = response.json()
    assert set(body["breakdown"].keys()) == {
        "user_coverage",
        "severity",
        "growth",
        "user_value",
        "business_value",
    }
    weights = body["weights"]
    assert set(weights["configured"].keys()) == {
        "user_coverage",
        "severity",
        "growth",
        "user_value",
        "business_value",
    }
    assert set(weights["effective"].keys()) == {
        "user_coverage",
        "severity",
        "growth",
        "user_value",
        "business_value",
    }
    assert body["thresholds"]["p1_below"] == "5.00"
    assert body["thresholds"]["p0_below"] == "7.50"


def test_post_opportunity_score_409_when_no_evidence_for_classification_run(client, monkeypatch):
    """An Insight with no InsightEvidence rows bound to its source
    classification run must surface as 409 (source_invalid) and must
    not create any Phase 4 row."""
    from datetime import datetime, timezone

    from app.models import (
        Cluster,
        ClusterMember,
        Feedback,
        FeedbackAnalysis,
        Insight,
        InsightRun,
        Project,
    )

    c, engine = client
    monkeypatch.setattr(scoring, "OpportunityClient", _StubOpportunityClient)
    _StubOpportunityClient.calls = []
    with Session(engine) as session:
        project = Project(name="NoEvidence", product_name="test")
        session.add(project)
        session.flush()
        classification = _make_analysis_run(project.id)
        session.add(classification)
        session.flush()
        clustering = _make_analysis_run(
            project.id,
            run_type="clustering",
            cluster_config={"classification_run_id": str(classification.id)},
        )
        session.add(clustering)
        session.flush()
        classification.input_feedback_external_ids = ["ne-0", "ne-1", "ne-2"]
        session.add(classification)
        session.flush()
        insight_run = InsightRun(
            project_id=project.id,
            clustering_run_id=clustering.id,
            classification_run_id=classification.id,
            status="completed",
            model="m",
            prompt_version="p",
            input_count=1,
            processed_count=1,
            success_count=1,
            started_at=None,
            completed_at=None,
        )
        session.add(insight_run)
        session.flush()
        cluster = Cluster(
            project_id=project.id,
            run_id=clustering.id,
            source="llm_review",
            feedback_count=1,
            embedding_model="local-hash-v1",
            distance_threshold=0.3,
            linkage="average",
        )
        session.add(cluster)
        session.flush()
        for idx in range(3):
            feedback = Feedback(
                project_id=project.id,
                external_id=f"ne-{idx}",
                content=f"ne {idx}",
                user_id=f"u-{idx}",
                feedback_created_at=datetime.now(timezone.utc),
            )
            session.add(feedback)
            session.flush()
            analysis = FeedbackAnalysis(
                feedback_id=feedback.id,
                run_id=classification.id,
                feedback_type="ux_issue",
                module="x",
                intent="x",
                pain_point="x",
                sentiment="negative",
                severity=4,
                confidence=0.9,
            )
            session.add(analysis)
            session.flush()
            if idx == 0:
                session.add(
                    ClusterMember(
                        cluster_id=cluster.id,
                        feedback_analysis_id=analysis.id,
                        similarity_score=0.9,
                    )
                )
        insight = Insight(
            run_id=insight_run.id,
            project_id=project.id,
            cluster_id=cluster.id,
            title="no evidence",
            pain_point="x",
            affected_segment="x",
            insight="x",
            recommendation="x",
            confidence=0.8,
            status="draft",
            feedback_count=1,
            evidence_count=0,
            source_cluster_external_id=str(cluster.id),
            source_member_external_ids=["ne-0"],
            model="m",
            prompt_version="p",
        )
        session.add(insight)
        session.flush()
        insight_id = insight.id
        session.commit()
    response = c.post(f"/insights/{insight_id}/opportunity-score")
    assert response.status_code == 409, response.text
    with Session(engine) as verify:
        scores = verify.scalars(select(OpportunityScore)).all()
        decisions = verify.scalars(select(InsightDecision)).all()
        events = verify.scalars(select(InsightDecisionEvent)).all()
    assert scores == []
    assert decisions == []
    assert events == []
    # LLM must not have been called.
    assert _StubOpportunityClient.calls == []


def test_post_opportunity_score_409_when_evidence_bound_to_other_run(client, monkeypatch):
    """InsightEvidence rows bound to a *different* classification run do
    NOT count as evidence for this insight's source run — the request
    must surface as 409."""
    from datetime import datetime, timezone

    from app.models import (
        Cluster,
        ClusterMember,
        Feedback,
        FeedbackAnalysis,
        Insight,
        InsightEvidence,
        InsightRun,
        Project,
    )

    c, engine = client
    monkeypatch.setattr(scoring, "OpportunityClient", _StubOpportunityClient)
    _StubOpportunityClient.calls = []
    with Session(engine) as session:
        project = Project(name="ForeignEvidence", product_name="test")
        session.add(project)
        session.flush()
        classification = _make_analysis_run(project.id)
        session.add(classification)
        session.flush()
        other_run = _make_analysis_run(project.id)
        session.add(other_run)
        session.flush()
        clustering = _make_analysis_run(
            project.id,
            run_type="clustering",
            cluster_config={"classification_run_id": str(classification.id)},
        )
        session.add(clustering)
        session.flush()
        classification.input_feedback_external_ids = ["fe-0"]
        session.add(classification)
        session.flush()
        insight_run = InsightRun(
            project_id=project.id,
            clustering_run_id=clustering.id,
            classification_run_id=classification.id,
            status="completed",
            model="m",
            prompt_version="p",
            input_count=1,
            processed_count=1,
            success_count=1,
            started_at=None,
            completed_at=None,
        )
        session.add(insight_run)
        session.flush()
        cluster = Cluster(
            project_id=project.id,
            run_id=clustering.id,
            source="llm_review",
            feedback_count=1,
            embedding_model="local-hash-v1",
            distance_threshold=0.3,
            linkage="average",
        )
        session.add(cluster)
        session.flush()
        feedback = Feedback(
            project_id=project.id,
            external_id="fe-0",
            content="x",
            user_id="u",
            feedback_created_at=datetime.now(timezone.utc),
        )
        session.add(feedback)
        session.flush()
        analysis = FeedbackAnalysis(
            feedback_id=feedback.id,
            run_id=classification.id,
            feedback_type="ux_issue",
            module="x",
            intent="x",
            pain_point="x",
            sentiment="negative",
            severity=4,
            confidence=0.9,
        )
        session.add(analysis)
        session.flush()
        session.add(
            ClusterMember(
                cluster_id=cluster.id,
                feedback_analysis_id=analysis.id,
                similarity_score=0.9,
            )
        )
        insight = Insight(
            run_id=insight_run.id,
            project_id=project.id,
            cluster_id=cluster.id,
            title="foreign evidence",
            pain_point="x",
            affected_segment="x",
            insight="x",
            recommendation="x",
            confidence=0.8,
            status="draft",
            feedback_count=1,
            evidence_count=1,
            source_cluster_external_id=str(cluster.id),
            source_member_external_ids=["fe-0"],
            model="m",
            prompt_version="p",
        )
        session.add(insight)
        session.flush()
        # Evidence bound to the OTHER run, not the source run.
        session.add(
            InsightEvidence(
                insight_id=insight.id,
                feedback_id=feedback.id,
                evidence_type="representative",
                relevance_score=0.9,
                feedback_external_id="fe-0",
                feedback_project_id=project.id,
                feedback_classification_run_id=other_run.id,
            )
        )
        insight_id = insight.id
        session.commit()
    response = c.post(f"/insights/{insight_id}/opportunity-score")
    assert response.status_code == 409, response.text
    with Session(engine) as verify:
        scores = verify.scalars(select(OpportunityScore)).all()
    assert scores == []


def test_health_ready_checks_all_phase4_tables(client):
    """``/health/ready`` must reach ``opportunity_scores``,
    ``insight_decisions`` and ``insight_decision_events``. Even when no
    row exists yet, every table must be reachable (no migration gap)."""
    c, engine = client
    response = c.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"


def test_get_decision_404_when_missing(client):
    c, engine = client
    insight_id = _http_seed(c, engine)
    response = c.get(f"/insights/{insight_id}/decision")
    assert response.status_code == 404


# ---- P1 coverage: Coverage denominator + source hash ----------------------------


def test_coverage_denominator_is_snapshot_scoped(db, stub_client):
    """Coverage denominator must come from the snapshot's feedback, not the
    project-wide ``Feedback.user_id`` pool. The snapshot records three
    distinct users, but only two of them are source members of the
    insight. After the snapshot is captured, three "noise" users are
    imported; they must NOT enter the denominator.
    """
    from datetime import datetime, timezone

    project = Project(name="CoverageScope", product_name="test")
    db.add(project)
    db.flush()
    classification = _make_analysis_run(project.id)
    db.add(classification)
    db.flush()
    clustering = _make_analysis_run(
        project.id,
        run_type="clustering",
        cluster_config={"classification_run_id": str(classification.id)},
    )
    db.add(clustering)
    db.flush()
    # Snapshot three distinct snapshot IDs, not in DB yet at capture time.
    classification.input_feedback_external_ids = ["snap-0", "snap-1", "snap-2"]
    db.add(classification)
    db.flush()
    insight_run = InsightRun(
        project_id=project.id,
        clustering_run_id=clustering.id,
        classification_run_id=classification.id,
        status="completed",
        model="test-model",
        prompt_version="insight-generation-v2",
        input_count=1,
        processed_count=1,
        success_count=1,
        started_at=None,
        completed_at=None,
    )
    db.add(insight_run)
    db.flush()
    cluster = Cluster(
        project_id=project.id,
        run_id=clustering.id,
        source="llm_review",
        feedback_count=2,
        embedding_model="local-hash-v1",
        distance_threshold=0.3,
        linkage="average",
    )
    db.add(cluster)
    db.flush()

    # Three snapshot records — three distinct users. Only two of these
    # rows are joined to the cluster; the third is excluded by the
    # source_member_external_ids contract but still contributes to the
    # denominator (its user_id is in the snapshot).
    snapshot_specs = [
        ("snap-0", "snap-user-0"),
        ("snap-1", "snap-user-1"),
        ("snap-2", "snap-user-2"),
    ]
    for external_id, user_id in snapshot_specs:
        feedback = Feedback(
            project_id=project.id,
            external_id=external_id,
            content=f"snapshot {external_id}",
            user_id=user_id,
            feedback_created_at=datetime.now(timezone.utc),
        )
        db.add(feedback)
        db.flush()
        analysis = FeedbackAnalysis(
            feedback_id=feedback.id,
            run_id=classification.id,
            feedback_type="ux_issue",
            module="upload",
            intent="multi_select",
            pain_point="x",
            sentiment="negative",
            severity=4,
            confidence=0.9,
        )
        db.add(analysis)
        db.flush()
        if external_id in {"snap-0", "snap-1"}:
            db.add(
                ClusterMember(
                    cluster_id=cluster.id,
                    feedback_analysis_id=analysis.id,
                    similarity_score=0.9,
                )
            )
    insight = Insight(
        run_id=insight_run.id,
        project_id=project.id,
        cluster_id=cluster.id,
        title="Coverage scope test",
        pain_point="x",
        affected_segment="scope",
        insight="x",
        recommendation="x",
        confidence=0.8,
        status=InsightStatus.DRAFT.value,
        feedback_count=2,
        evidence_count=0,
        source_cluster_external_id=str(cluster.id),
        source_member_external_ids=["snap-0", "snap-1"],
        model="test-model",
        prompt_version="insight-generation-v2",
    )
    db.add(insight)
    db.flush()
    # Evidence rows bound to the source classification run are required
    # for the score to be created at all (no-evidence = 409). Bind the
    # first member feedback as the evidence row so the snapshot has
    # valid evidence.
    source_feedback_id = next(
        f.id for f in db.scalars(select(Feedback)).all() if f.external_id == "snap-0"
    )
    db.add(
        InsightEvidence(
            insight_id=insight.id,
            feedback_id=source_feedback_id,
            evidence_type="representative",
            relevance_score=0.9,
            feedback_external_id="snap-0",
            feedback_project_id=project.id,
            feedback_classification_run_id=classification.id,
        )
    )
    db.flush()
    # Noise feedback imported AFTER the snapshot — must not affect
    # anything (numerator or denominator).
    for index in range(3):
        noise = Feedback(
            project_id=project.id,
            external_id=f"noise-{index}",
            content="noise",
            user_id=f"noise-user-{index}",
            feedback_created_at=datetime.now(timezone.utc),
        )
        db.add(noise)
        db.flush()
        db.add(
            FeedbackAnalysis(
                feedback_id=noise.id,
                run_id=classification.id,
                feedback_type="ux_issue",
                module="noise",
                intent="noise",
                pain_point="x",
                sentiment="neutral",
                severity=1,
                confidence=0.4,
            )
        )
    db.commit()

    outcome = create_opportunity_score(db, insight, business_value=Decimal("8.0"))
    coverage = outcome.score.breakdown["user_coverage"]
    extras = coverage["extras"]
    # Numerator = 2 (only snap-0 and snap-1 are joined to the cluster).
    assert extras["affected_count"] == 2, coverage
    # Denominator = 3 (all snap IDs are in the snapshot, regardless of
    # whether they are joined to the cluster).
    assert extras["project_count"] == 3, extras
    assert Decimal(coverage["score"]) == Decimal("6.67"), coverage
    # Noise feedback IDs are not in the snapshot, so they cannot leak
    # into affected_user_ids either (they have no cluster membership).
    assert extras["affected_count"] == 2


def test_coverage_unavailable_when_snapshot_external_ids_empty(db, stub_client):
    """When ``classification_run.input_feedback_external_ids`` is
    missing or empty, the Coverage denominator must collapse to 0 and
    the dimension must be marked unavailable. The numerator is left as
    the true source-member user count (not faked to 0) so the audit
    trail still reflects the real affected users."""
    _, classification, _, insight, _ = _seed(db, feedback_count=3)
    classification.input_feedback_external_ids = None
    db.commit()
    outcome = create_opportunity_score(db, insight, business_value=Decimal("8.0"))
    coverage = outcome.score.breakdown["user_coverage"]
    assert coverage["available"] is False
    assert coverage["score"] is None
    # 3 source members with unique user_ids (U0, U1, U2) → affected_count
    # carries the real numerator.
    assert coverage["extras"]["affected_count"] == 3
    # Denominator is unavailable: snapshot external IDs are missing.
    assert coverage["extras"]["project_count"] == 0


def test_coverage_unavailable_when_snapshot_ext_ids_have_no_users(db, stub_client):
    """When the snapshot external IDs are present but no matching
    Feedback row carries a non-empty ``user_id``, Coverage must be
    unavailable and must not silently fall back to a project-wide
    count."""
    from datetime import datetime, timezone

    project = Project(name="CoverageEmptyUsers", product_name="test")
    db.add(project)
    db.flush()
    classification = _make_analysis_run(project.id)
    db.add(classification)
    db.flush()
    classification.input_feedback_external_ids = ["anon-0", "anon-1"]
    db.add(classification)
    db.flush()
    clustering = _make_analysis_run(
        project.id,
        run_type="clustering",
        cluster_config={"classification_run_id": str(classification.id)},
    )
    db.add(clustering)
    db.flush()
    insight_run = InsightRun(
        project_id=project.id,
        clustering_run_id=clustering.id,
        classification_run_id=classification.id,
        status="completed",
        model="m",
        prompt_version="p",
        input_count=1,
        processed_count=1,
        success_count=1,
        started_at=None,
        completed_at=None,
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
        external_id="anon-0",
        content="anon",
        user_id=None,
        feedback_created_at=datetime.now(timezone.utc),
    )
    db.add(feedback)
    db.flush()
    analysis = FeedbackAnalysis(
        feedback_id=feedback.id,
        run_id=classification.id,
        feedback_type="ux_issue",
        module="x",
        intent="x",
        pain_point="x",
        sentiment="negative",
        severity=4,
        confidence=0.9,
    )
    db.add(analysis)
    db.flush()
    db.add(
        ClusterMember(
            cluster_id=cluster.id,
            feedback_analysis_id=analysis.id,
            similarity_score=0.9,
        )
    )
    # Project-wide feedback with a user_id, but external_id NOT in the
    # snapshot — must not leak into the denominator.
    other = Feedback(
        project_id=project.id,
        external_id="project-only",
        content="outside snapshot",
        user_id="project-user",
        feedback_created_at=datetime.now(timezone.utc),
    )
    db.add(other)
    db.flush()
    db.add(
        FeedbackAnalysis(
            feedback_id=other.id,
            run_id=classification.id,
            feedback_type="ux_issue",
            module="x",
            intent="x",
            pain_point="x",
            sentiment="negative",
            severity=4,
            confidence=0.9,
        )
    )
    insight = Insight(
        run_id=insight_run.id,
        project_id=project.id,
        cluster_id=cluster.id,
        title="coverage empty users",
        pain_point="x",
        affected_segment="x",
        insight="x",
        recommendation="x",
        confidence=0.8,
        status=InsightStatus.DRAFT.value,
        feedback_count=1,
        evidence_count=0,
        source_cluster_external_id=str(cluster.id),
        source_member_external_ids=["anon-0"],
        model="m",
        prompt_version="p",
    )
    db.add(insight)
    db.flush()
    # InsightEvidence bound to the source classification run is required
    # by the no-evidence guard.
    anon_feedback_id = next(
        f.id for f in db.scalars(select(Feedback)).all() if f.external_id == "anon-0"
    )
    db.add(
        InsightEvidence(
            insight_id=insight.id,
            feedback_id=anon_feedback_id,
            evidence_type="representative",
            relevance_score=0.9,
            feedback_external_id="anon-0",
            feedback_project_id=project.id,
            feedback_classification_run_id=classification.id,
        )
    )
    db.flush()
    db.commit()
    outcome = create_opportunity_score(db, insight, business_value=Decimal("8.0"))
    coverage = outcome.score.breakdown["user_coverage"]
    assert coverage["available"] is False
    assert coverage["score"] is None
    assert coverage["extras"]["project_count"] == 0


def test_coverage_denominator_counts_unique_users_only(db, stub_client):
    """The Coverage denominator is built from the *unique* (stripped)
    ``user_id`` set across the classification snapshot. Two snapshot
    feedbacks that share the same user must NOT inflate the denominator
    by two. With snapshot ext ids ``dup-a``, ``dup-b``, ``dup-c`` →
    ``u1`` / ``u1`` / ``u2``, denominator must equal 2.
    """
    from datetime import datetime, timezone

    project = Project(name="CoverageUniqueUsers", product_name="test")
    db.add(project)
    db.flush()
    classification = _make_analysis_run(project.id)
    db.add(classification)
    db.flush()
    clustering = _make_analysis_run(
        project.id,
        run_type="clustering",
        cluster_config={"classification_run_id": str(classification.id)},
    )
    db.add(clustering)
    db.flush()
    classification.input_feedback_external_ids = ["dup-a", "dup-b", "dup-c"]
    db.add(classification)
    db.flush()
    insight_run = InsightRun(
        project_id=project.id,
        clustering_run_id=clustering.id,
        classification_run_id=classification.id,
        status="completed",
        model="m",
        prompt_version="p",
        input_count=1,
        processed_count=1,
        success_count=1,
        started_at=None,
        completed_at=None,
    )
    db.add(insight_run)
    db.flush()
    cluster = Cluster(
        project_id=project.id,
        run_id=clustering.id,
        source="llm_review",
        feedback_count=2,
        embedding_model="local-hash-v1",
        distance_threshold=0.3,
        linkage="average",
    )
    db.add(cluster)
    db.flush()

    snapshot_specs = [
        ("dup-a", "u1"),
        ("dup-b", "u1"),
        ("dup-c", "u2"),
    ]
    for external_id, user_id in snapshot_specs:
        feedback = Feedback(
            project_id=project.id,
            external_id=external_id,
            content=f"snapshot {external_id}",
            user_id=user_id,
            feedback_created_at=datetime.now(timezone.utc),
        )
        db.add(feedback)
        db.flush()
        analysis = FeedbackAnalysis(
            feedback_id=feedback.id,
            run_id=classification.id,
            feedback_type="ux_issue",
            module="x",
            intent="x",
            pain_point="x",
            sentiment="negative",
            severity=4,
            confidence=0.9,
        )
        db.add(analysis)
        db.flush()
        # Only dup-a and dup-c are joined to the cluster.
        if external_id in {"dup-a", "dup-c"}:
            db.add(
                ClusterMember(
                    cluster_id=cluster.id,
                    feedback_analysis_id=analysis.id,
                    similarity_score=0.9,
                )
            )
    insight = Insight(
        run_id=insight_run.id,
        project_id=project.id,
        cluster_id=cluster.id,
        title="coverage unique",
        pain_point="x",
        affected_segment="x",
        insight="x",
        recommendation="x",
        confidence=0.8,
        status=InsightStatus.DRAFT.value,
        feedback_count=2,
        evidence_count=0,
        source_cluster_external_id=str(cluster.id),
        source_member_external_ids=["dup-a", "dup-c"],
        model="m",
        prompt_version="p",
    )
    db.add(insight)
    db.flush()
    # Evidence row bound to the source classification run is required;
    # bind the dup-c feedback so the score can be created at all.
    source_feedback_id = next(
        f.id for f in db.scalars(select(Feedback)).all() if f.external_id == "dup-c"
    )
    db.add(
        InsightEvidence(
            insight_id=insight.id,
            feedback_id=source_feedback_id,
            evidence_type="representative",
            relevance_score=0.9,
            feedback_external_id="dup-c",
            feedback_project_id=project.id,
            feedback_classification_run_id=classification.id,
        )
    )
    db.flush()
    db.commit()

    outcome = create_opportunity_score(db, insight, business_value=Decimal("8.0"))
    coverage = outcome.score.breakdown["user_coverage"]
    extras = coverage["extras"]
    # Numerator = 2 unique source-member users (u1, u2).
    assert extras["affected_count"] == 2, extras
    # Denominator = 2 unique snapshot users (u1 collapses dup-a + dup-b).
    assert extras["project_count"] == 2, extras
    assert Decimal(coverage["score"]) == Decimal("10.00"), coverage


def test_source_snapshot_hash_changes_with_content_drift(db, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    first = create_opportunity_score(db, insight, business_value=Decimal("7.0"))
    first_hash = first.score.source_snapshot_hash

    feedback_row = db.scalars(select(Feedback)).first()
    feedback_row.content = "drifted content"
    db.commit()

    second = create_opportunity_score(db, insight, business_value=Decimal("7.0"))
    assert second.score.source_snapshot_hash != first_hash
    assert first.score.source_snapshot_hash == first_hash


def test_source_snapshot_hash_drifts_on_field_changes(db, stub_client):
    """With the cluster member set held constant (so the source stays
    valid), each individual source field — content, user_id,
    feedback_created_at, analysis.severity, feedback_type and an
    InsightEvidence mutation — must propagate into the snapshot hash.
    The previously persisted score is left untouched.
    """
    from datetime import timedelta

    _, _, _, insight, feedback_ids = _seed(db, feedback_count=3)
    first = create_opportunity_score(db, insight, business_value=Decimal("7.0"))
    baseline_hash = first.score.source_snapshot_hash

    feedback = db.get(Feedback, feedback_ids[0])
    feedback.content = "drift-content"
    db.commit()
    h_content = create_opportunity_score(
        db, insight, business_value=Decimal("7.0")
    ).score.source_snapshot_hash
    assert h_content != baseline_hash

    feedback.user_id = "drift-user"
    db.commit()
    h_user = create_opportunity_score(
        db, insight, business_value=Decimal("7.0")
    ).score.source_snapshot_hash
    assert h_user != h_content

    feedback.feedback_created_at = feedback.feedback_created_at + timedelta(seconds=1)
    db.commit()
    h_time = create_opportunity_score(
        db, insight, business_value=Decimal("7.0")
    ).score.source_snapshot_hash
    assert h_time != h_user

    analysis = db.scalar(
        select(FeedbackAnalysis).where(FeedbackAnalysis.feedback_id == feedback_ids[1])
    )
    analysis.severity = 5
    db.commit()
    h_severity = create_opportunity_score(
        db, insight, business_value=Decimal("7.0")
    ).score.source_snapshot_hash
    assert h_severity != h_time

    analysis.feedback_type = "bug"
    db.commit()
    h_type = create_opportunity_score(
        db, insight, business_value=Decimal("7.0")
    ).score.source_snapshot_hash
    assert h_type != h_severity

    evidence = db.scalar(select(InsightEvidence).where(InsightEvidence.insight_id == insight.id))
    evidence.relevance_score = 0.42
    db.commit()
    h_evidence = create_opportunity_score(
        db, insight, business_value=Decimal("7.0")
    ).score.source_snapshot_hash
    assert h_evidence != h_type

    # The previously persisted score is untouched.
    assert first.score.source_snapshot_hash == baseline_hash
    # All hashes differ pairwise.
    hashes = {
        baseline_hash,
        h_content,
        h_user,
        h_time,
        h_severity,
        h_type,
        h_evidence,
    }
    assert len(hashes) == 7


def test_source_invalid_when_live_member_set_diverges_from_insight(db, stub_client):
    """Adding a cluster member without updating
    ``insight.source_member_external_ids`` must surface as source_invalid
    (409), not as a silent hash drift.
    """
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    first = create_opportunity_score(db, insight, business_value=Decimal("7.0"))

    extra = Feedback(
        project_id=insight.project_id,
        external_id="extra-drift",
        content="drift",
        user_id="extra-drift",
        feedback_created_at=None,
    )
    db.add(extra)
    db.flush()
    analysis = FeedbackAnalysis(
        feedback_id=extra.id,
        run_id=first.score.classification_run_id,
        feedback_type="ux_issue",
        module="x",
        intent="x",
        pain_point="x",
        sentiment="negative",
        severity=4,
        confidence=0.9,
    )
    db.add(analysis)
    db.flush()
    db.add(
        ClusterMember(
            cluster_id=insight.cluster_id,
            feedback_analysis_id=analysis.id,
            similarity_score=0.9,
        )
    )
    db.commit()

    with pytest.raises(OpportunityValidationError) as ei:
        create_opportunity_score(db, insight, business_value=Decimal("7.0"))
    assert ei.value.code == "source_invalid" or "source" in str(ei.value).lower()


def test_cross_run_member_rejected(db, stub_client):
    """A cluster member whose FeedbackAnalysis belongs to a different
    classification run must surface as source_invalid (409)."""
    project = Project(name="CrossRun", product_name="test")
    db.add(project)
    db.flush()
    classification = _make_analysis_run(project.id)
    db.add(classification)
    db.flush()
    other_run = _make_analysis_run(project.id)
    db.add(other_run)
    db.flush()
    clustering = _make_analysis_run(
        project.id,
        run_type="clustering",
        cluster_config={"classification_run_id": str(other_run.id)},
    )
    db.add(clustering)
    db.flush()
    insight_run = InsightRun(
        project_id=project.id,
        clustering_run_id=clustering.id,
        classification_run_id=classification.id,
        status="completed",
        model="m",
        prompt_version="p",
        input_count=1,
        processed_count=1,
        success_count=1,
        started_at=None,
        completed_at=None,
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
        external_id="cross",
        content="x",
        user_id="x",
        feedback_created_at=None,
    )
    db.add(feedback)
    db.flush()
    foreign_analysis = FeedbackAnalysis(
        feedback_id=feedback.id,
        run_id=other_run.id,
        feedback_type="ux_issue",
        module="x",
        intent="x",
        pain_point="x",
        sentiment="negative",
        severity=4,
        confidence=0.9,
    )
    db.add(foreign_analysis)
    db.flush()
    db.add(
        ClusterMember(
            cluster_id=cluster.id,
            feedback_analysis_id=foreign_analysis.id,
            similarity_score=0.9,
        )
    )
    insight = Insight(
        run_id=insight_run.id,
        project_id=project.id,
        cluster_id=cluster.id,
        title="cross-run",
        pain_point="x",
        affected_segment="x",
        insight="x",
        recommendation="x",
        confidence=0.8,
        status=InsightStatus.DRAFT.value,
        feedback_count=1,
        evidence_count=0,
        source_cluster_external_id=str(cluster.id),
        source_member_external_ids=["cross"],
        model="m",
        prompt_version="p",
    )
    db.add(insight)
    db.flush()
    db.commit()
    with pytest.raises(OpportunityValidationError) as ei:
        create_opportunity_score(db, insight, business_value=Decimal("7.0"))
    assert ei.value.code == "source_invalid" or "source" in str(ei.value).lower()


# ---- P1 growth: anchored to source members' latest timestamp ------------------


def test_growth_anchored_to_source_members(db, stub_client):
    """Growth windows must be anchored to the source members' latest
    feedback_created_at — not to ``now``."""
    from datetime import datetime, timedelta, timezone

    project = Project(name="GrowthAnchor", product_name="test")
    db.add(project)
    db.flush()
    classification = _make_analysis_run(project.id)
    db.add(classification)
    db.flush()
    clustering = _make_analysis_run(
        project.id,
        run_type="clustering",
        cluster_config={"classification_run_id": str(classification.id)},
    )
    db.add(clustering)
    db.flush()
    insight_run = InsightRun(
        project_id=project.id,
        clustering_run_id=clustering.id,
        classification_run_id=classification.id,
        status="completed",
        model="m",
        prompt_version="p",
        input_count=1,
        processed_count=1,
        success_count=1,
        started_at=None,
        completed_at=None,
    )
    db.add(insight_run)
    db.flush()
    cluster = Cluster(
        project_id=project.id,
        run_id=clustering.id,
        source="llm_review",
        feedback_count=4,
        embedding_model="local-hash-v1",
        distance_threshold=0.3,
        linkage="average",
    )
    db.add(cluster)
    db.flush()
    anchor = datetime.now(timezone.utc) - timedelta(days=90)
    days_offsets = [89, 50, 20, 5]
    feedback_ids = []
    for idx, offset in enumerate(days_offsets):
        ts = anchor + timedelta(days=offset)
        feedback = Feedback(
            project_id=project.id,
            external_id=f"growth-{idx}",
            content=f"g{idx}",
            user_id=f"g{idx}",
            feedback_created_at=ts,
        )
        db.add(feedback)
        db.flush()
        analysis = FeedbackAnalysis(
            feedback_id=feedback.id,
            run_id=classification.id,
            feedback_type="ux_issue",
            module="x",
            intent="x",
            pain_point="x",
            sentiment="negative",
            severity=4,
            confidence=0.9,
        )
        db.add(analysis)
        db.flush()
        db.add(
            ClusterMember(
                cluster_id=cluster.id,
                feedback_analysis_id=analysis.id,
                similarity_score=0.9,
            )
        )
        feedback_ids.append(feedback.id)
    insight = Insight(
        run_id=insight_run.id,
        project_id=project.id,
        cluster_id=cluster.id,
        title="growth anchor",
        pain_point="x",
        affected_segment="x",
        insight="x",
        recommendation="x",
        confidence=0.8,
        status=InsightStatus.DRAFT.value,
        feedback_count=4,
        evidence_count=0,
        source_cluster_external_id=str(cluster.id),
        source_member_external_ids=[f"growth-{i}" for i in range(4)],
        model="m",
        prompt_version="p",
    )
    db.add(insight)
    db.flush()
    for fid in feedback_ids:
        db.add(
            InsightEvidence(
                insight_id=insight.id,
                feedback_id=fid,
                evidence_type="representative",
                relevance_score=0.9,
                feedback_external_id=f"growth-{feedback_ids.index(fid)}",
                feedback_project_id=project.id,
                feedback_classification_run_id=classification.id,
            )
        )
    insight.evidence_count = len(feedback_ids)
    db.flush()
    db.commit()
    outcome = create_opportunity_score(db, insight, business_value=Decimal("7.0"))
    growth = outcome.score.breakdown["growth"]
    # Anchor = anchor + 89d ⇒ relative ages: 0, 39, 69, 84d.
    # recent window (≤30d) = 1; previous window (30-60d) = 1; older = 2.
    assert growth["extras"]["recent_count"] == 1, growth
    assert growth["extras"]["previous_count"] == 1, growth


def test_growth_unavailable_for_old_only_member_data():
    """Pure-function behaviour: when previous_count is zero, growth is
    unavailable — the regulation guards against ``0/0`` ratios."""
    result = growth_dimension(
        recent_count=2,
        previous_count=0,
        has_recent=True,
        has_previous=True,
    )
    assert result.available is False


# ---- P1 LLM: retry / latency / attempt / retry counters ----------------------


class _CountingClient:
    """Records transport/protocol failures for retry tracking.

    The same instance is returned across retry attempts so its call
    counter accumulates; the real ``OpportunityClient`` is also created
    fresh per attempt (per-attempt lifecycle is preserved), but in the
    test we short-circuit through a factory that always yields this
    one object.
    """

    def __init__(self, mode: str, attempts_before_success: int = 0):
        self.mode = mode
        self.attempts_before_success = attempts_before_success
        self.calls: list = []
        self.lock = threading.Lock()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def generate_user_value(self, payload):
        with self.lock:
            self.calls.append(payload)
            attempt = len(self.calls)
        if self.mode == "transport":
            from app.services.opportunity_client import OpportunityLLMTransportError

            raise OpportunityLLMTransportError("synthetic transport")
        if self.mode == "protocol":
            from app.services.opportunity_client import OpportunityLLMProtocolError

            raise OpportunityLLMProtocolError("synthetic protocol")
        if self.mode == "flaky":
            if attempt <= self.attempts_before_success:
                from app.services.opportunity_client import OpportunityLLMTransportError

                raise OpportunityLLMTransportError("synthetic transport")
        return SimpleNamespace(
            payload={"user_value_score": 6.0, "reasoning": "retry reasoning"},
            usage=SimpleNamespace(input_tokens=11, output_tokens=5),
        )


def _patch_shared_client(monkeypatch, client: _CountingClient):
    """Bind ``scoring.OpportunityClient`` so every retry attempt reuses
    the same stub instance — this mirrors the real client's per-attempt
    creation semantics while keeping the call counter stable."""
    monkeypatch.setattr(scoring, "OpportunityClient", lambda *a, **kw: client)


def test_user_value_records_latency_and_attempt_counts(db, monkeypatch, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    stub = _CountingClient("flaky", attempts_before_success=1)
    _patch_shared_client(monkeypatch, stub)
    monkeypatch.setattr(settings, "opportunity_max_retries", 2, raising=False)
    outcome = create_opportunity_score(db, insight, business_value=Decimal("7.5"))
    assert outcome.score.attempt_count == 2
    assert outcome.score.retry_count == 1
    assert outcome.score.latency_ms >= 0
    assert len(stub.calls) == 2


def test_user_value_does_not_retry_protocol_failure(db, monkeypatch, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    stub = _CountingClient("protocol")
    _patch_shared_client(monkeypatch, stub)
    monkeypatch.setattr(settings, "opportunity_max_retries", 3, raising=False)
    with pytest.raises(OpportunityLLMProtocolError):
        create_opportunity_score(db, insight, business_value=Decimal("7.5"))
    assert len(stub.calls) == 1


def test_user_value_records_metrics_after_partial_success(db, monkeypatch, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    stub = _CountingClient("flaky", attempts_before_success=2)
    _patch_shared_client(monkeypatch, stub)
    monkeypatch.setattr(settings, "opportunity_max_retries", 3, raising=False)
    outcome = create_opportunity_score(db, insight, business_value=Decimal("7.5"))
    score = outcome.score
    assert score.attempt_count == 3
    assert score.retry_count == 2
    assert score.user_value_score is not None
    assert len(stub.calls) == 3


def test_user_value_exhausts_retries_then_raises(db, monkeypatch, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    stub = _CountingClient("transport")
    _patch_shared_client(monkeypatch, stub)
    monkeypatch.setattr(settings, "opportunity_max_retries", 1, raising=False)
    with pytest.raises(OpportunityLLMTransportError):
        create_opportunity_score(db, insight, business_value=Decimal("7.5"))
    assert len(stub.calls) == 2


# ---- P1 evidence: limit LLM evidence to source run -----------------------------


def test_user_value_evidence_excludes_other_run_analyses(db, monkeypatch, stub_client):
    """The AI call must only see FeedbackAnalysis rows that match
    snapshot.classification_run_id. One feedback carries two
    analyses (one bound to the source run, one bound to a foreign run);
    only the source-run analysis is bound to the ClusterMember and to
    the InsightEvidence row, so a correctly scoped call must surface
    the source-run values and never the foreign-run values."""
    from datetime import datetime, timezone

    project = Project(name="EvidenceScope", product_name="test")
    db.add(project)
    db.flush()
    classification = _make_analysis_run(project.id)
    db.add(classification)
    db.flush()
    other_run = _make_analysis_run(project.id)
    db.add(other_run)
    db.flush()
    clustering = _make_analysis_run(
        project.id,
        run_type="clustering",
        cluster_config={"classification_run_id": str(classification.id)},
    )
    db.add(clustering)
    db.flush()
    insight_run = InsightRun(
        project_id=project.id,
        clustering_run_id=clustering.id,
        classification_run_id=classification.id,
        status="completed",
        model="m",
        prompt_version="p",
        input_count=1,
        processed_count=1,
        success_count=1,
        started_at=None,
        completed_at=None,
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

    # Single feedback with two analyses: one bound to the source run,
    # one bound to a foreign run. ClusterMember only references the
    # source-run analysis.
    feedback = Feedback(
        project_id=project.id,
        external_id="dual",
        content="dual-analysis",
        user_id="u",
        feedback_created_at=datetime.now(timezone.utc),
    )
    db.add(feedback)
    db.flush()
    source_analysis = FeedbackAnalysis(
        feedback_id=feedback.id,
        run_id=classification.id,
        feedback_type="ux_issue",
        module="source-module",
        intent="source-intent",
        pain_point="source-pain",
        sentiment="positive",
        severity=4,
        confidence=0.9,
    )
    db.add(source_analysis)
    db.flush()
    foreign_analysis = FeedbackAnalysis(
        feedback_id=feedback.id,
        run_id=other_run.id,
        feedback_type="bug",
        module="foreign-module",
        intent="foreign-intent",
        pain_point="foreign-pain",
        sentiment="negative",
        severity=1,
        confidence=0.4,
    )
    db.add(foreign_analysis)
    db.flush()
    db.add(
        ClusterMember(
            cluster_id=cluster.id,
            feedback_analysis_id=source_analysis.id,
            similarity_score=0.9,
        )
    )

    insight = Insight(
        run_id=insight_run.id,
        project_id=project.id,
        cluster_id=cluster.id,
        title="evidence scope",
        pain_point="x",
        affected_segment="x",
        insight="x",
        recommendation="x",
        confidence=0.8,
        status=InsightStatus.DRAFT.value,
        feedback_count=1,
        evidence_count=0,
        source_cluster_external_id=str(cluster.id),
        source_member_external_ids=["dual"],
        model="m",
        prompt_version="p",
    )
    db.add(insight)
    db.flush()
    # InsightEvidence bound to the source run only.
    db.add(
        InsightEvidence(
            insight_id=insight.id,
            feedback_id=feedback.id,
            evidence_type="representative",
            relevance_score=0.9,
            feedback_external_id="dual",
            feedback_project_id=project.id,
            feedback_classification_run_id=classification.id,
        )
    )
    db.commit()

    # Capture the LLM call to assert the source-run analysis is what
    # reaches the model — not the foreign-run analysis.
    class _Capture:
        seen: object = None

        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def generate_user_value(self, payload):
            _Capture.seen = payload
            return SimpleNamespace(
                payload={"user_value_score": 5.0, "reasoning": "x"},
                usage=SimpleNamespace(input_tokens=10, output_tokens=4),
            )

    monkeypatch.setattr(scoring, "OpportunityClient", _Capture)
    create_opportunity_score(db, insight, business_value=Decimal("7.0"))
    evidence = _Capture.seen["evidence"]
    assert len(evidence) == 1, evidence
    entry = evidence[0]
    assert entry["feedback_external_id"] == "dual"
    # Source-run analysis values must reach the LLM.
    assert entry["severity"] == 4
    assert entry["sentiment"] == "positive"
    assert entry["feedback_type"] == "ux_issue"
    assert entry["module"] == "source-module"
    assert entry["intent"] == "source-intent"
    assert entry["pain_point"] == "source-pain"
    # Foreign-run analysis values must NEVER reach the LLM.
    assert "bug" != entry["feedback_type"]
    assert "foreign-module" != entry["module"]


def test_foreign_run_cluster_member_rejected_as_source_invalid(db, stub_client):
    """A ClusterMember whose FeedbackAnalysis belongs to a different
    classification run must surface as source_invalid (409)."""
    project = Project(name="CrossRun", product_name="test")
    db.add(project)
    db.flush()
    classification = _make_analysis_run(project.id)
    db.add(classification)
    db.flush()
    other_run = _make_analysis_run(project.id)
    db.add(other_run)
    db.flush()
    clustering = _make_analysis_run(
        project.id,
        run_type="clustering",
        cluster_config={"classification_run_id": str(other_run.id)},
    )
    db.add(clustering)
    db.flush()
    insight_run = InsightRun(
        project_id=project.id,
        clustering_run_id=clustering.id,
        classification_run_id=classification.id,
        status="completed",
        model="m",
        prompt_version="p",
        input_count=1,
        processed_count=1,
        success_count=1,
        started_at=None,
        completed_at=None,
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
        external_id="cross",
        content="x",
        user_id="x",
        feedback_created_at=None,
    )
    db.add(feedback)
    db.flush()
    foreign_analysis = FeedbackAnalysis(
        feedback_id=feedback.id,
        run_id=other_run.id,
        feedback_type="ux_issue",
        module="x",
        intent="x",
        pain_point="x",
        sentiment="negative",
        severity=4,
        confidence=0.9,
    )
    db.add(foreign_analysis)
    db.flush()
    db.add(
        ClusterMember(
            cluster_id=cluster.id,
            feedback_analysis_id=foreign_analysis.id,
            similarity_score=0.9,
        )
    )
    insight = Insight(
        run_id=insight_run.id,
        project_id=project.id,
        cluster_id=cluster.id,
        title="cross-run",
        pain_point="x",
        affected_segment="x",
        insight="x",
        recommendation="x",
        confidence=0.8,
        status=InsightStatus.DRAFT.value,
        feedback_count=1,
        evidence_count=0,
        source_cluster_external_id=str(cluster.id),
        source_member_external_ids=["cross"],
        model="m",
        prompt_version="p",
    )
    db.add(insight)
    db.flush()
    classification.input_feedback_external_ids = ["cross"]
    db.add(classification)
    db.flush()
    db.commit()
    with pytest.raises(OpportunityValidationError) as ei:
        create_opportunity_score(db, insight, business_value=Decimal("7.0"))
    assert "source" in str(ei.value).lower() or ei.value.code == "source_invalid"


def test_no_evidence_returns_source_invalid_with_no_score_or_decision(db, stub_client, monkeypatch):
    """When the insight has no InsightEvidence rows bound to its source
    classification run, scoring must surface as ``source_invalid`` (409)
    and must NOT create any ``OpportunityScore`` / ``InsightDecision``
    row. The LLM is never called.
    """
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    db.execute(InsightEvidence.__table__.delete().where(InsightEvidence.insight_id == insight.id))
    insight.evidence_count = 0
    db.commit()

    called = {"value": False}

    def _spy(self, payload):
        called["value"] = True
        return SimpleNamespace(
            payload={"user_value_score": 9.0, "reasoning": "x"},
            usage=SimpleNamespace(input_tokens=10, output_tokens=4),
        )

    monkeypatch.setattr(_StubOpportunityClient, "generate_user_value", _spy)
    with pytest.raises(OpportunityValidationError) as ei:
        create_opportunity_score(db, insight, business_value=Decimal("7.0"))
    assert ei.value.code == "source_invalid"
    assert called["value"] is False
    assert db.scalars(select(OpportunityScore)).all() == []
    assert db.scalars(select(InsightDecision)).all() == []


# ---- P1 atomicity + replay -------------------------------------------------------


def test_set_business_value_rolls_back_on_event_failure(db, monkeypatch, stub_client):
    """The set_business_value flow must commit score + decision + event
    atomically. Forcing an event-add error must rollback everything."""
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    from app.models.opportunity import InsightDecisionEvent, OpportunityScore

    original_add = Session.add

    def _broken_add(self, instance, *args, **kwargs):
        if isinstance(instance, InsightDecisionEvent):
            raise RuntimeError("synthetic event-write failure")
        return original_add(self, instance, *args, **kwargs)

    monkeypatch.setattr(Session, "add", _broken_add)

    with pytest.raises(RuntimeError):
        apply_decision_patch(
            db,
            insight,
            request_id=uuid4(),
            expected_version=0,
            action="set_business_value",
            business_value=Decimal("9.0"),
            pm_priority=None,
            override_reason=None,
            override_note=None,
        )
    assert db.scalars(select(OpportunityScore)).all() == []
    assert db.scalars(select(InsightDecisionEvent)).all() == []


def test_set_business_value_concurrent_only_one_succeeds(tmp_path, monkeypatch, stub_client):
    """Two concurrent set_business_value calls on the same insight must
    result in exactly one success (no double score rows). Each thread
    owns its own Session bound to the same engine — sharing a single
    Session is not thread-safe."""
    engine = create_engine(f"sqlite:///{tmp_path}/phase4_concurrent.db")
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def foreign_keys(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "llm_api_key", "test", raising=False)
    monkeypatch.setattr(settings, "llm_base_url", "http://invalid", raising=False)
    monkeypatch.setattr(settings, "llm_model", "test-model", raising=False)

    seed_session = Session(engine, expire_on_commit=False)
    try:
        _, _, _, insight, _ = _seed(seed_session)
        create_opportunity_score(seed_session, insight, business_value=Decimal("4.0"))
        # The score helper is flush-only; commit before close so the
        # baseline decision version is observable to the worker threads.
        seed_session.commit()
        insight_id = insight.id
    finally:
        seed_session.close()

    barrier = threading.Barrier(2)
    outcomes: list = []
    outcomes_lock = threading.Lock()

    def _attempt(value: str) -> None:
        with Session(engine, expire_on_commit=False) as thread_session:
            local_insight = thread_session.get(Insight, insight_id)
            barrier.wait()
            try:
                apply_decision_patch(
                    thread_session,
                    local_insight,
                    request_id=uuid4(),
                    expected_version=1,
                    action="set_business_value",
                    business_value=Decimal(value),
                    pm_priority=None,
                    override_reason=None,
                    override_note=None,
                )
                with outcomes_lock:
                    outcomes.append("ok")
            except Exception as exc:  # noqa: BLE001
                with outcomes_lock:
                    outcomes.append(("err", type(exc).__name__))

    t1 = threading.Thread(target=_attempt, args=("5.0",))
    t2 = threading.Thread(target=_attempt, args=("9.0",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    successes = [o for o in outcomes if o == "ok"]
    assert len(successes) == 1, outcomes
    with Session(engine) as verify:
        from app.models.opportunity import OpportunityScore

        rows = verify.scalars(select(OpportunityScore)).all()
        # The pre-existing score plus the one successful create.
        assert len(rows) == 2

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_request_id_replay_returns_after_state_from_event(db, stub_client):
    """A replay of an idempotent request_id must reconstruct the
    decision from the original event's after_state, not from the live row.
    Captures the original response values before the live mutation so the
    assertion is not polluted by SQLAlchemy identity-map aliasing."""
    from types import SimpleNamespace as _SN

    _, _, _, insight, _ = _seed(db, feedback_count=3)
    create_opportunity_score(db, insight, business_value=Decimal("8.0"))
    request_id = uuid4()
    first = apply_decision_patch(
        db,
        insight,
        request_id=request_id,
        expected_version=1,
        action="accept",
        business_value=None,
        pm_priority=None,
        override_reason=None,
        override_note=None,
    )
    # Capture original response values before any live mutation.
    first_snapshot = _SN(
        status=first.status,
        pm_priority=first.pm_priority,
        version=first.version,
        override_reason=first.override_reason,
        override_note=first.override_note,
        ai_priority=first.ai_priority,
        business_value=first.business_value,
    )

    # Mutate the live decision so the event's after_state diverges.
    from app.models.opportunity import InsightDecision

    decision = db.scalar(select(InsightDecision).where(InsightDecision.insight_id == insight.id))
    decision.pm_priority = "P0"
    decision.status = DECISION_OVERRIDDEN
    decision.override_reason = "other"
    decision.override_note = "live-only mutation"
    decision.version = decision.version + 1
    db.commit()

    # Replay must mirror the original event after_state, not the live
    # mutation. expected_version is intentionally mismatched; the replay
    # path must ignore it and not raise / mutate.
    replayed = apply_decision_patch(
        db,
        insight,
        request_id=request_id,
        expected_version=999,
        action="accept",
        business_value=None,
        pm_priority=None,
        override_reason=None,
        override_note=None,
    )
    assert replayed.status == first_snapshot.status
    assert replayed.pm_priority == first_snapshot.pm_priority
    assert replayed.version == first_snapshot.version
    assert replayed.override_reason == first_snapshot.override_reason
    assert replayed.override_note == first_snapshot.override_note
    assert replayed.ai_priority == first_snapshot.ai_priority
    assert float(replayed.business_value) == float(first_snapshot.business_value)
    # Live mutation persists in storage but is not surfaced.
    fresh = db.scalar(select(InsightDecision).where(InsightDecision.insight_id == insight.id))
    assert fresh.override_reason == "other"
    # Replay must not have created another event.
    from app.models.opportunity import InsightDecisionEvent

    assert len(db.scalars(select(InsightDecisionEvent)).all()) == 1


def test_ai_priority_preserved_via_real_service_override(db, stub_client):
    """PM override must change pm_priority but never ai_priority.
    Exercises the real apply_decision_patch service (no ORM bypass)."""
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    outcome = create_opportunity_score(db, insight, business_value=Decimal("9.0"))
    original_ai_priority = outcome.score.ai_priority
    new_pm = "P0" if original_ai_priority == "P2" else "P2"
    apply_decision_patch(
        db,
        insight,
        request_id=uuid4(),
        expected_version=1,
        action="override",
        business_value=None,
        pm_priority=new_pm,
        override_reason="other",
        override_note="战略覆盖",
    )
    score = db.get(OpportunityScore, outcome.score.id)
    assert score.ai_priority == original_ai_priority
    decision = db.scalar(select(InsightDecision).where(InsightDecision.insight_id == insight.id))
    assert decision.pm_priority == new_pm
    assert decision.ai_priority == original_ai_priority


def test_ai_priority_preserved_via_real_service_accept(db, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    outcome = create_opportunity_score(db, insight, business_value=Decimal("9.0"))
    ai = outcome.score.ai_priority
    apply_decision_patch(
        db,
        insight,
        request_id=uuid4(),
        expected_version=1,
        action="accept",
        business_value=None,
        pm_priority=None,
        override_reason=None,
        override_note=None,
    )
    score = db.get(OpportunityScore, outcome.score.id)
    assert score.ai_priority == ai


def test_ai_priority_preserved_via_real_service_defer_reject(db, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    outcome = create_opportunity_score(db, insight, business_value=Decimal("9.0"))
    ai = outcome.score.ai_priority
    apply_decision_patch(
        db,
        insight,
        request_id=uuid4(),
        expected_version=1,
        action="defer",
        business_value=None,
        pm_priority=None,
        override_reason=None,
        override_note=None,
    )
    score = db.get(OpportunityScore, outcome.score.id)
    assert score.ai_priority == ai
    apply_decision_patch(
        db,
        insight,
        request_id=uuid4(),
        expected_version=2,
        action="reject",
        business_value=None,
        pm_priority=None,
        override_reason=None,
        override_note=None,
    )
    score = db.get(OpportunityScore, outcome.score.id)
    assert score.ai_priority == ai


def test_old_time_window_drops_records_outside_both_windows(db, stub_client):
    """Records whose age to anchor is >60d fall outside both windows
    and must not be counted toward recent_count or previous_count.
    A pure previous_count=0 then yields an unavailable growth
    dimension per the formula.
    """
    from datetime import datetime, timedelta, timezone

    project = Project(name="OldWindow", product_name="test")
    db.add(project)
    db.flush()
    classification = _make_analysis_run(project.id)
    db.add(classification)
    db.flush()
    clustering = _make_analysis_run(
        project.id,
        run_type="clustering",
        cluster_config={"classification_run_id": str(classification.id)},
    )
    db.add(clustering)
    db.flush()
    insight_run = InsightRun(
        project_id=project.id,
        clustering_run_id=clustering.id,
        classification_run_id=classification.id,
        status="completed",
        model="m",
        prompt_version="p",
        input_count=1,
        processed_count=1,
        success_count=1,
        started_at=None,
        completed_at=None,
    )
    db.add(insight_run)
    db.flush()
    cluster = Cluster(
        project_id=project.id,
        run_id=clustering.id,
        source="llm_review",
        feedback_count=3,
        embedding_model="local-hash-v1",
        distance_threshold=0.3,
        linkage="average",
    )
    db.add(cluster)
    db.flush()
    # Anchor = today; place one record inside the recent window and one
    # record strictly outside both windows (anchor-65d).
    now = datetime.now(timezone.utc)
    offsets = [5, 65]
    feedback_ids = []
    for idx, offset in enumerate(offsets):
        ts = now - timedelta(days=offset)
        feedback = Feedback(
            project_id=project.id,
            external_id=f"old-{idx}",
            content=f"old {idx}",
            user_id=f"old-{idx}",
            feedback_created_at=ts,
        )
        db.add(feedback)
        db.flush()
        analysis = FeedbackAnalysis(
            feedback_id=feedback.id,
            run_id=classification.id,
            feedback_type="ux_issue",
            module="x",
            intent="x",
            pain_point="x",
            sentiment="negative",
            severity=4,
            confidence=0.9,
        )
        db.add(analysis)
        db.flush()
        db.add(
            ClusterMember(
                cluster_id=cluster.id,
                feedback_analysis_id=analysis.id,
                similarity_score=0.9,
            )
        )
        feedback_ids.append(feedback.id)
    # Anchor record at the latest feedback_created_at.
    anchor_ts = now
    anchor_feedback = Feedback(
        project_id=project.id,
        external_id="anchor",
        content="anchor",
        user_id="anchor",
        feedback_created_at=anchor_ts,
    )
    db.add(anchor_feedback)
    db.flush()
    anchor_analysis = FeedbackAnalysis(
        feedback_id=anchor_feedback.id,
        run_id=classification.id,
        feedback_type="ux_issue",
        module="x",
        intent="x",
        pain_point="x",
        sentiment="negative",
        severity=4,
        confidence=0.9,
    )
    db.add(anchor_analysis)
    db.flush()
    db.add(
        ClusterMember(
            cluster_id=cluster.id,
            feedback_analysis_id=anchor_analysis.id,
            similarity_score=0.9,
        )
    )
    feedback_ids.append(anchor_feedback.id)
    insight = Insight(
        run_id=insight_run.id,
        project_id=project.id,
        cluster_id=cluster.id,
        title="old window",
        pain_point="x",
        affected_segment="x",
        insight="x",
        recommendation="x",
        confidence=0.8,
        status=InsightStatus.DRAFT.value,
        feedback_count=3,
        evidence_count=0,
        source_cluster_external_id=str(cluster.id),
        source_member_external_ids=[f"old-{i}" for i in range(2)] + ["anchor"],
        model="m",
        prompt_version="p",
    )
    db.add(insight)
    db.flush()
    for fid in feedback_ids:
        db.add(
            InsightEvidence(
                insight_id=insight.id,
                feedback_id=fid,
                evidence_type="representative",
                relevance_score=0.9,
                feedback_external_id=f"old-{feedback_ids.index(fid)}",
                feedback_project_id=project.id,
                feedback_classification_run_id=classification.id,
            )
        )
    insight.evidence_count = len(feedback_ids)
    db.flush()
    db.commit()
    outcome = create_opportunity_score(db, insight, business_value=Decimal("7.0"))
    growth = outcome.score.breakdown["growth"]
    # Anchor (recent), 5d-old (recent); the 65d-old record is dropped.
    # previous_count stays 0 → growth dimension is unavailable.
    assert growth["available"] is False
    assert growth["extras"]["recent_count"] == 2, growth
    assert growth["extras"]["previous_count"] == 0, growth


def test_growth_dimension_unavailable_when_previous_count_zero():
    """Independent pure-formula guard: previous_count=0 ⇒ unavailable
    even if recent_count > 0 (the formula rejects 0/0 ratios)."""
    result = growth_dimension(
        recent_count=3,
        previous_count=0,
        has_recent=True,
        has_previous=True,
    )
    assert result.available is False


def test_snapshot_hash_includes_evidence_payload(db, stub_client):
    """Changing evidence relevance must change the snapshot hash."""
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    first = create_opportunity_score(db, insight, business_value=Decimal("7.0"))
    first_hash = first.score.source_snapshot_hash
    evidence = db.scalars(
        select(InsightEvidence).where(InsightEvidence.insight_id == insight.id)
    ).first()
    evidence.relevance_score = 0.42
    db.commit()
    second = create_opportunity_score(db, insight, business_value=Decimal("7.0"))
    assert second.score.source_snapshot_hash != first_hash
    assert first.score.source_snapshot_hash == first_hash


def test_score_create_persists_retry_metrics_columns(db, monkeypatch, stub_client):
    _, _, _, insight, _ = _seed(db, feedback_count=3)
    stub = _CountingClient("flaky", attempts_before_success=1)
    _patch_shared_client(monkeypatch, stub)
    monkeypatch.setattr(settings, "opportunity_max_retries", 2, raising=False)
    outcome = create_opportunity_score(db, insight, business_value=Decimal("7.0"))
    score = outcome.score
    assert hasattr(score, "latency_ms")
    assert hasattr(score, "attempt_count")
    assert hasattr(score, "retry_count")
    assert score.latency_ms >= 0
    assert score.attempt_count == 2
    assert score.retry_count == 1
    assert len(stub.calls) == 2


def test_persisted_effective_weights_sum_to_one_within_tolerance_default(db, stub_client):
    """Persistence must not drift the renormalised effective weights.

    With the default configured weights and Growth unavailable, the
    service-level renormalisation yields effective weights that sum to 1
    at full Decimal precision; the two-decimal score quantisation rule
    does NOT apply to the weights payload, so the API / JSON / DB layer
    must round-trip the values losslessly and the stored
    ``weights_effective`` Decimal sum must equal 1 within 1e-9.
    """
    _, _, _, insight, _ = _seed(db, feedback_count=3, days_ago=0)
    outcome = create_opportunity_score(db, insight, business_value=Decimal("8.0"))
    effective = outcome.score.weights_effective
    assert effective is not None
    total = sum(Decimal(str(v)) for v in effective.values())
    assert abs(total - Decimal("1")) <= Decimal("1e-9")
    # The score itself is still quantised to two decimals.
    assert outcome.score.total_score is not None
    assert outcome.score.total_score == outcome.score.total_score.quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def test_persisted_effective_weights_sum_to_one_within_tolerance_custom(db, stub_client):
    """Same guarantee for non-default configured weights.

    Custom configured weights 0.30 / 0.30 / 0.10 / 0.20 / 0.10 with Growth
    unavailable must still produce a ``weights_effective`` whose Decimal
    sum equals 1 within 1e-9, even though two-decimal quantisation of
    the individual weights would naively yield 0.99 or 1.01.
    """
    custom_weights = OpportunityWeights(
        user_coverage=Decimal("0.30"),
        severity=Decimal("0.30"),
        growth=Decimal("0.10"),
        user_value=Decimal("0.20"),
        business_value=Decimal("0.10"),
    )
    validated_custom = validate_weights(custom_weights.model_dump())
    _, _, _, insight, _ = _seed(db, feedback_count=3, days_ago=0)
    outcome = create_opportunity_score(
        db, insight, weights=validated_custom, business_value=Decimal("8.0")
    )
    effective = outcome.score.weights_effective
    configured = outcome.score.weights_configured
    assert effective is not None
    assert configured is not None
    eff_total = sum(Decimal(str(v)) for v in effective.values())
    cfg_total = sum(Decimal(str(v)) for v in configured.values())
    assert abs(eff_total - Decimal("1")) <= Decimal("1e-9")
    # Custom configured weights round-trip losslessly (no two-dp drift).
    assert cfg_total == Decimal("1")
    # total_score still obeys the two-decimal ROUND_HALF_UP rule.
    assert outcome.score.total_score is not None
    assert outcome.score.total_score == outcome.score.total_score.quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
