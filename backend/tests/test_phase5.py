"""Phase 5 — Requirement + PRD tests.

The tests mirror the supervisor's brief:

1. Pydantic schema validation (request, response, inner structured
   items, status transitions).
2. Migration 0010 round-trip — every column / check / unique /
   foreign-key constraint from the Alembic script is mirrored in the
   ORM metadata.
3. LLM Structured-Output client: Anthropic-compatible Messages API
   parser, strict tool-name + schema check, error categorisation
   (config / transport / protocol), no upstream body or token leak.
4. Service layer:
   * :func:`create_requirement` snapshot of the Phase 4 decision so a
     later Phase 4 PATCH cannot silently rewrite the requirement
     priority provenance.
   * UNIQUE(insight_id) — duplicate POST rejected with 409.
   * PM-authored vs AI-generated requirements; grounding validators
     reject evidence IDs not in the upstream insight.
   * :func:`create_prd_version` with atomic version increment via
     ``requirements.next_version``. Concurrent creates serialise on
     the unique ``(requirement_id, version)`` constraint.
   * PRD grounding: ``success_metrics`` may not include revenue /
     percentage targets; ``risks[].evidence_ids`` for ``known``
     risks must cite at least one evidence id; ``speculative`` risks
     surface as ``grounding_warnings``.
   * PRD ``is_ai_generated_draft`` flag mirrors whether the AI client
     produced the payload.
   * :func:`accept_prd` is idempotent — repeated calls return the
     same row without re-stamping ``accepted_at``.
5. HTTP layer — 404 / 409 / 422 / 503, OpenAPI schema, pagination.

The LLM client is stubbed everywhere — the real TCP HTTP / model
calls live in Codex's independent supervisor test plan.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agents.prompts import requirement_generation_v1 as req_prompt
from app.db.base import Base
from app.db.config import settings
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
from app.models.opportunity import (
    DECISION_ACCEPTED,
    DECISION_DEFERRED,
    DECISION_OVERRIDDEN,
    DECISION_PENDING_REVIEW,
    DECISION_REJECTED,
)
from app.models.requirement_prd import (
    PRD,
    PRD_ACCEPTED,
    PRD_DRAFT,
    PRD_REVIEWING,
    PRD_STATUS_VALUES,
    REQUIREMENT_DRAFT,
    REQUIREMENT_REVIEWING,
    REQUIREMENT_STATUS_VALUES,
    RISK_SPECULATIVE,
)
from app.schemas.requirement_prd import (
    EvidenceReference,
    PMRequirementContent,
    PRDAcceptRequest,
    PRDContent,
    PRDCreateRequest,
    PRDPatchRequest,
    PRDStatusLiteral,
    RequirementCreateRequest,
    RequirementPatchRequest,
    RequirementStatusLiteral,
    SuccessMetric,
)
from app.services import requirement_prd as service_module
from app.services.requirement_prd import PRD_ARCHIVED as SVC_PRD_ARCHIVED
from app.services.requirement_prd import REQUIREMENT_APPROVED as SVC_REQUIREMENT_APPROVED
from app.services.requirement_prd import (
    RequirementValidationError,
    accept_prd,
    create_prd_version,
    create_requirement,
    list_prds,
    list_requirements,
    load_phase4_snapshot,
    patch_prd,
    patch_requirement,
)
from app.services.requirement_prd_client import (
    PRDLLMProtocolError,
    PRDLLMTransportError,
    RequirementLLMProtocolError,
    RequirementLLMTransportError,
    _extract_tool_payload,
    _parse_usage,
)

# ---- Fixtures ---------------------------------------------------------------


def _make_valid_requirement_payload(allowed: tuple[str, ...]) -> dict[str, object]:
    return {
        "name": "图片选择效率",
        "problem": "用户在上传时需要重复点击每一张图片，影响效率。",
        "target_user": "移动端上传用户。",
        "user_story": "As a 移动端用户 I want to 一次选择多张图片 So that 上传更高效。",
        "solution_hypothesis": "通过长按进入多选模式，减少重复点击。",
        "success_metrics": [
            {
                "name": "watch 多选渗透率",
                "type": "metric_definition",
                "sources": [allowed[0]],
                "note": "监测项；不承诺数字提升。",
            }
        ],
        "risks": [
            {
                "category": "known",
                "description": "误触进入多选模式。",
                "evidence_ids": [allowed[0]],
                "mitigation": "提供 3 秒撤销提示。",
            }
        ],
        "evidence_references": [
            {"external_id": allowed[0], "usage": "用于支撑用户故事与人群描述。"}
        ],
    }


def _make_valid_prd_payload(allowed: tuple[str, ...]) -> dict[str, object]:
    return {
        "background": {"text": "背景", "evidence_ids": [allowed[0]]},
        "problem": {"text": "问题", "evidence_ids": [allowed[0]]},
        "target_user": {"text": "人群", "evidence_ids": [allowed[0]]},
        "user_story": {"text": "故事", "evidence_ids": [allowed[0]]},
        "product_goal": {"text": "目标", "evidence_ids": [allowed[0]]},
        "proposed_solution": {
            "text": "方案",
            "is_proposed": True,
            "evidence_ids": [allowed[0]],
        },
        "functional_requirements": [{"text": "FR1", "evidence_ids": [allowed[0]]}],
        "user_flow": {"text": "关键路径", "evidence_ids": [allowed[0]]},
        "edge_cases": [
            {
                "category": "empty_state",
                "description": "未选图片状态。",
                "evidence_ids": [allowed[0]],
            },
            {
                "category": "failure",
                "description": "上传失败。",
                "evidence_ids": [allowed[0]],
            },
            {
                "category": "retry",
                "description": "重试机制。",
                "evidence_ids": [allowed[0]],
            },
            {
                "category": "partial_success",
                "description": "部分成功。",
                "evidence_ids": [allowed[0]],
            },
            {
                "category": "invalid_input",
                "description": "非法输入。",
                "evidence_ids": [allowed[0]],
            },
            {
                "category": "limit_exceeded",
                "description": "超过上限。",
                "evidence_ids": [allowed[0]],
            },
        ],
        "acceptance_criteria": [
            {
                "text": "AC1",
                "verifiable": True,
                "note": "",
                "evidence_ids": [allowed[0]],
            }
        ],
        "success_metrics": [
            {
                "name": "watch 渗透率",
                "type": "metric_definition",
                "note": "",
                "sources": [allowed[0]],
            }
        ],
        "risks": [
            {
                "category": "known",
                "description": "误触进入多选模式。",
                "evidence_ids": [allowed[0]],
                "mitigation": "撤销提示。",
            }
        ],
        "mvp_scope": {
            "in_scope": "MVP 范围。",
            "out_of_scope": "非 MVP。",
        },
        "evidence_sources": ["feedback ext-id: " + allowed[0]],
        "evidence_ids": [allowed[0]],
    }


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
    monkeypatch.setattr(settings, "llm_api_key", "test", raising=False)
    monkeypatch.setattr(settings, "llm_base_url", "http://invalid", raising=False)
    monkeypatch.setattr(settings, "llm_model", "test-model", raising=False)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    Base.metadata.drop_all(engine)
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


def _seed(
    db,
    *,
    feedback_count=3,
    with_user_ids=True,
    with_timestamps=True,
    days_ago=0,
    decision_status=DECISION_ACCEPTED,
):
    """Seed a Phase 1—4 chain; returns ``(project, classification, clustering, insight, feedback_ids)``.

    Includes an InsightDecision row with PM Priority P1 so
    :func:`load_phase4_snapshot` accepts it. The seed sets an
    ``opportunity_scores`` row only if the caller supplies one via
    ``score_overrides``; otherwise ``decision.current_score_id`` is
    left null and the ``score_id`` field on the Requirement stays
    null (matching migration 0010). ``decision_status`` lets the
    caller force any of the five Phase 4 outcomes for the new
    requirement-eligibility tests.
    """
    from datetime import datetime, timedelta, timezone

    project = Project(name="Phase5 worker", product_name="test")
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
            content=f"phase5 feedback #{index}",
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
    # Decision row — required by load_phase4_snapshot. PM Priority is
    # mandatory; ai_priority is allowed to remain null at this stage.
    decision = InsightDecision(
        insight_id=insight.id,
        project_id=project.id,
        current_score_id=None,
        business_value=None,
        ai_priority=None,
        pm_priority="P1",
        status=decision_status,
        override_reason=None,
        override_note=None,
        version=1,
    )
    db.add(decision)
    db.flush()
    db.commit()
    return project, classification, clustering, insight, feedback_ids


# ---- Schema validation ------------------------------------------------------


def test_status_enums_match_db_allowed_values():
    """The pydantic Literal must agree with the SQL CHECK constraints."""
    assert set(RequirementStatusLiteral.__args__) == set(REQUIREMENT_STATUS_VALUES)
    assert set(PRDStatusLiteral.__args__) == set(PRD_STATUS_VALUES)


def test_pydantic_requirement_content_strips_unknown_field():
    with pytest.raises(Exception):
        PMRequirementContent(
            name="x",
            problem="y",
            target_user="z",
            user_story="a",
            solution_hypothesis="b",
            success_metrics=[],
            risks=[],
            evidence_references=[EvidenceReference(external_id="ext-0", usage="u")],
            unknown_field="ignore-me",
        )


def test_pydantic_prd_content_strips_unknown_section():
    with pytest.raises(Exception):
        PRDContent.model_validate(
            {
                **_make_valid_prd_payload(("ext-0",)),
                "mystery_section": {"text": "x", "evidence_ids": ["ext-0"]},
            }
        )


def test_pydantic_prd_section_requires_evidence_ids_list():
    with pytest.raises(Exception):
        PRDContent.model_validate(
            {
                **_make_valid_prd_payload(("ext-0",)),
                "background": {"text": "x"},
            }
        )


def test_pydantic_prd_acceptance_criteria_requires_verifiable_bool():
    bad = _make_valid_prd_payload(("ext-0",))
    bad["acceptance_criteria"][0]["verifiable"] = "yes"
    with pytest.raises(Exception):
        PRDContent.model_validate(bad)


def test_pydantic_prd_proposed_solution_requires_is_proposed():
    bad = _make_valid_prd_payload(("ext-0",))
    bad["proposed_solution"] = {
        "text": "方案",
        "evidence_ids": ["ext-0"],
    }
    with pytest.raises(Exception):
        PRDContent.model_validate(bad)


def test_pydantic_prd_edge_case_category_must_be_in_enum():
    bad = _make_valid_prd_payload(("ext-0",))
    bad["edge_cases"][0]["category"] = "unknown_category"
    with pytest.raises(Exception):
        PRDContent.model_validate(bad)


def test_pydantic_request_models_have_extra_forbid():
    req = RequirementCreateRequest.model_json_schema()
    assert req.get("additionalProperties") is False
    patch_body = RequirementPatchRequest.model_json_schema()
    assert patch_body.get("additionalProperties") is False
    prd_patch = PRDPatchRequest.model_json_schema()
    assert prd_patch.get("additionalProperties") is False
    prd_accept = PRDAcceptRequest.model_json_schema()
    assert prd_accept.get("additionalProperties") is False
    prd_create = PRDCreateRequest.model_json_schema()
    assert prd_create.get("additionalProperties") is False


# ---- LLM Structured Output client ------------------------------------------


def _wrap_tool_use(payload: dict, tool_name: str) -> dict:
    return {
        "id": "msg_stub",
        "type": "message",
        "content": [
            {"type": "text", "text": "stub"},
            {"type": "tool_use", "id": "tool_1", "name": tool_name, "input": payload},
        ],
    }


def test_parse_usage_sums_cache_tokens():
    """Cache tokens aggregate into ``input_tokens`` like Phase 4."""
    usage = _parse_usage(
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_input_tokens": 7,
            "cache_read_input_tokens": 3,
        }
    )
    assert usage.input_tokens == 20
    assert usage.output_tokens == 5


def test_parse_usage_handles_unknown_token_count():
    usage = _parse_usage({"input_tokens": None, "output_tokens": 4})
    assert usage.input_tokens is None
    assert usage.output_tokens == 4


def test_parse_usage_rejects_unexpected_shape():
    with pytest.raises(RequirementLLMProtocolError):
        _parse_usage("not-an-object")


def test_extract_tool_payload_strict_about_field_extras():
    with pytest.raises(RequirementLLMProtocolError):
        _extract_tool_payload(
            data=_wrap_tool_use({"x": 1, "y": 2}, req_prompt.TOOL_NAME),
            tool_name=req_prompt.TOOL_NAME,
            allowed_keys={"x"},
            protocol_error_cls=RequirementLLMProtocolError,
        )


def test_extract_tool_payload_rejects_wrong_tool_name():
    bad = {
        "id": "msg",
        "content": [{"type": "tool_use", "id": "tool_1", "name": "wrong_tool", "input": {"a": 1}}],
    }
    with pytest.raises(RequirementLLMProtocolError):
        _extract_tool_payload(
            data=bad,
            tool_name=req_prompt.TOOL_NAME,
            allowed_keys={"a"},
            protocol_error_cls=RequirementLLMProtocolError,
        )


def test_extract_tool_payload_rejects_extra_tools():
    bad = {
        "id": "msg",
        "content": [
            {"type": "tool_use", "id": "tool_1", "name": req_prompt.TOOL_NAME, "input": {"x": 1}},
            {"type": "tool_use", "id": "tool_2", "name": req_prompt.TOOL_NAME, "input": {"x": 2}},
        ],
    }
    with pytest.raises(RequirementLLMProtocolError):
        _extract_tool_payload(
            data=bad,
            tool_name=req_prompt.TOOL_NAME,
            allowed_keys={"x"},
            protocol_error_cls=RequirementLLMProtocolError,
        )


def test_extract_tool_payload_rejects_missing_content():
    with pytest.raises(RequirementLLMProtocolError):
        _extract_tool_payload(
            data={"id": "x"},
            tool_name=req_prompt.TOOL_NAME,
            allowed_keys={"x"},
            protocol_error_cls=RequirementLLMProtocolError,
        )


# ---- Service layer: requirement creation -----------------------------------


class _StubRequirementClient:
    """Stub Requirement LLM client returning a deterministic payload."""

    calls: list = []
    fail_with: Exception | None = None
    next_payload: dict | None = None

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def generate_requirement(self, payload):
        self.calls.append(payload)
        if self.fail_with is not None:
            raise self.fail_with
        return SimpleNamespace(
            payload=self.next_payload or _make_valid_requirement_payload(("ext-0",)),
            usage=SimpleNamespace(input_tokens=12, output_tokens=8),
        )


class _StubPRDClient:
    """Stub PRD LLM client returning a deterministic payload."""

    calls: list = []
    fail_with: Exception | None = None
    next_payload: dict | None = None

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def generate_prd(self, payload):
        self.calls.append(payload)
        if self.fail_with is not None:
            raise self.fail_with
        return SimpleNamespace(
            payload=self.next_payload or _make_valid_prd_payload(("ext-0",)),
            usage=SimpleNamespace(input_tokens=20, output_tokens=15),
        )


@pytest.fixture(autouse=False)
def stub_llm(monkeypatch):
    _StubRequirementClient.calls = []
    _StubRequirementClient.fail_with = None
    _StubRequirementClient.next_payload = None
    _StubPRDClient.calls = []
    _StubPRDClient.fail_with = None
    _StubPRDClient.next_payload = None
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    return SimpleNamespace(requirement=_StubRequirementClient, prd=_StubPRDClient)


def test_create_requirement_captures_phase4_snapshot(db, stub_llm):
    _, _, _, insight, _ = _seed(db)
    outcome = create_requirement(db, insight.id)
    db.commit()
    requirement = outcome.requirement
    db.refresh(requirement)
    snapshot = load_phase4_snapshot(db, insight.id)
    assert requirement.pm_priority == snapshot.pm_priority
    assert requirement.ai_priority_snapshot == snapshot.ai_priority
    assert requirement.decision_status_snapshot == snapshot.decision_status
    assert requirement.insight_status_snapshot == snapshot.insight_status
    assert requirement.source_snapshot_hash == snapshot.source_snapshot_hash
    assert requirement.prompt_version == req_prompt.PROMPT_VERSION
    # Service layer marker — the API surfaces this via grounding_warnings.
    assert outcome.is_ai_generated is True
    assert requirement.next_version == 1
    assert requirement.status == REQUIREMENT_DRAFT


def test_create_requirement_rejects_duplicate_for_same_insight(db, stub_llm):
    _, _, _, insight, _ = _seed(db)
    create_requirement(db, insight.id)
    db.commit()
    with pytest.raises(RequirementValidationError) as exc:
        create_requirement(db, insight.id)
    assert exc.value.code == "duplicate_requirement"
    assert db.rollback is not None  # SQLAlchemy session usable
    db.rollback()


@pytest.mark.parametrize(
    "ineligible_status",
    [
        DECISION_PENDING_REVIEW,
        DECISION_REJECTED,
        DECISION_DEFERRED,
    ],
)
def test_create_requirement_blocks_ineligible_decision_status(db, stub_llm, ineligible_status):
    """Entry gate: pending_review / rejected / deferred decisions
    block Requirement creation. ``accepted`` and ``overridden`` are
    the only two Phase 4 outcomes that authorise PM work."""
    _, _, _, insight, _ = _seed(db, decision_status=ineligible_status)
    with pytest.raises(RequirementValidationError) as exc:
        create_requirement(db, insight.id)
    assert exc.value.code in {"decision_ineligible", "schema_invalid"}
    assert ineligible_status in str(exc.value)
    db.rollback()


@pytest.mark.parametrize(
    "eligible_status",
    [DECISION_ACCEPTED, DECISION_OVERRIDDEN],
)
def test_create_requirement_allows_eligible_decision_status(db, stub_llm, eligible_status):
    _, _, _, insight, _ = _seed(db, decision_status=eligible_status)
    outcome = create_requirement(db, insight.id)
    db.commit()
    assert outcome.requirement.pm_priority == "P1"
    assert outcome.requirement.decision_status_snapshot == eligible_status


def test_create_requirement_rejects_unknown_evidence_reference(db, stub_llm):
    _, _, _, insight, _ = _seed(db)
    bad = _make_valid_requirement_payload(("ext-0",))
    bad["evidence_references"][0]["external_id"] = "ghost-evidence"
    _StubRequirementClient.next_payload = bad
    with pytest.raises(RequirementLLMProtocolError):
        create_requirement(db, insight.id)
    db.rollback()


def test_create_requirement_blocks_fabricated_metric_type(db, stub_llm):
    _, _, _, insight, _ = _seed(db)
    bad = _make_valid_requirement_payload(("ext-0",))
    bad["success_metrics"][0]["type"] = "fake_percentage"
    _StubRequirementClient.next_payload = bad
    with pytest.raises(RequirementLLMProtocolError):
        create_requirement(db, insight.id)
    db.rollback()


def test_create_requirement_marks_known_risk_without_evidence(db, stub_llm):
    _, _, _, insight, _ = _seed(db)
    bad = _make_valid_requirement_payload(("ext-0",))
    bad["risks"][0]["evidence_ids"] = []
    bad["risks"][0]["category"] = "known"
    _StubRequirementClient.next_payload = bad
    outcome = create_requirement(db, insight.id)
    db.commit()
    # Soft warning — not blocking — surfaces in grounding_warnings list.
    codes = [w.code for w in outcome.grounding_warnings]
    assert "known_risk_evidence_missing" in codes


def test_create_requirement_marks_speculative_risk_warning(db, stub_llm):
    _, _, _, insight, _ = _seed(db)
    payload = _make_valid_requirement_payload(("ext-0",))
    payload["risks"][0]["category"] = RISK_SPECULATIVE
    payload["risks"][0]["evidence_ids"] = []
    _StubRequirementClient.next_payload = payload
    outcome = create_requirement(db, insight.id)
    db.commit()
    codes = [w.code for w in outcome.grounding_warnings]
    assert "speculative_risk" in codes


def test_create_requirement_503_when_llm_transport_exhausted(db, monkeypatch):
    _, _, _, insight, _ = _seed(db)

    class _AlwaysTransport:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def generate_requirement(self, payload):
            raise RequirementLLMTransportError("transient 503")

    monkeypatch.setattr(service_module, "RequirementClient", _AlwaysTransport)
    monkeypatch.setattr(settings, "requirement_max_retries", 1)
    with pytest.raises(RequirementLLMTransportError):
        create_requirement(db, insight.id)
    db.rollback()


def test_create_requirement_protocol_error_fails_immediately(db, monkeypatch):
    _, _, _, insight, _ = _seed(db)

    class _AlwaysProtocol:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def generate_requirement(self, payload):
            raise RequirementLLMProtocolError("malformed payload")

    monkeypatch.setattr(service_module, "RequirementClient", _AlwaysProtocol)
    with pytest.raises(RequirementLLMProtocolError):
        create_requirement(db, insight.id)
    db.rollback()


def test_phase4_patch_does_not_rewrite_requirement_snapshot(db, stub_llm):
    """A subsequent PATCH to the Phase 4 Decision must NOT update the
    Requirement's ``pm_priority`` / ``decision_status_snapshot`` /
    ``source_snapshot_hash`` columns; the snapshot is permanent."""
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()

    decision = db.get(InsightDecision, requirement.decision_id)
    decision.pm_priority = "P0"
    decision.override_reason = "strategic_priority"
    decision.override_note = "PM raised priority later"
    decision.version = 99
    db.commit()

    db.refresh(requirement)
    assert requirement.pm_priority == "P1"
    assert requirement.pm_override_reason_snapshot is None
    assert requirement.decision_status_snapshot == DECISION_ACCEPTED
    assert requirement.source_snapshot_hash != ""


def test_pm_authored_requirement_skips_ai_call(db, monkeypatch, stub_llm):
    """PM-provided content bypasses the AI client."""
    _, _, _, insight, _ = _seed(db)
    content = PMRequirementContent(
        name="PM-owned requirement",
        problem="problem",
        target_user="移动用户。",
        user_story="As a 用户 I want 多选 So that 提升效率。",
        solution_hypothesis="长按进入多选。",
        success_metrics=[
            SuccessMetric(
                name="watch",
                type="metric_definition",
                sources=["ext-0"],
                note="",
            )
        ],
        risks=[],
        evidence_references=[EvidenceReference(external_id="ext-0", usage="user pain point")],
    )
    _StubRequirementClient.calls = []
    outcome = create_requirement(db, insight.id, pm_content=content)
    db.commit()
    assert _StubRequirementClient.calls == []
    assert outcome.is_ai_generated is False
    row = outcome.requirement
    assert row.name == "PM-owned requirement"
    assert row.model is None
    assert row.prompt_version == settings.requirement_prompt_version


def test_pm_authored_requirement_rejects_unknown_evidence_id(db, stub_llm):
    _, _, _, insight, _ = _seed(db)
    content = PMRequirementContent(
        name="PM-owned requirement",
        problem="problem",
        target_user="移动用户。",
        user_story="As a 用户 I want 多选 So that 提升效率。",
        solution_hypothesis="长按进入多选。",
        success_metrics=[],
        risks=[],
        evidence_references=[EvidenceReference(external_id="ghost", usage="user pain point")],
    )
    with pytest.raises(RequirementValidationError):
        create_requirement(db, insight.id, pm_content=content)
    db.rollback()


def test_patch_requirement_blocks_invalid_status_transition(db, stub_llm):
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    with pytest.raises(RequirementValidationError):
        patch_requirement(db, requirement.id, status="approved")
    db.rollback()


def test_patch_requirement_status_transition_draft_to_reviewing_to_approved(db, stub_llm):
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    patch_requirement(db, requirement.id, status=REQUIREMENT_REVIEWING)
    db.commit()
    patch_requirement(db, requirement.id, status=SVC_REQUIREMENT_APPROVED)
    db.commit()
    db.refresh(requirement)
    assert requirement.status == SVC_REQUIREMENT_APPROVED


# ---- Service layer: PRD versioning -----------------------------------------


def test_create_prd_version_atomic_increment(db, stub_llm):
    """Each successive :func:`create_prd_version` call must bump the version
    counter by exactly one (no collision on the unique ``(requirement_id,
    version)`` constraint). Atomicity under contention is enforced by the
    SELECT-modify-write pattern over ``requirements.next_version`` plus the
    unique index — exercised here sequentially because SQLite's in-process
    engine segfaults when the same ORM session is shared across OS
    threads. Three concurrent PRDs created back-to-back must therefore
    land on versions 1, 2, 3 with ``next_version`` advanced to 4.
    """
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()

    produced_versions: list[int] = []
    for _ in range(3):
        outcome = create_prd_version(db, requirement.id)
        db.commit()
        produced_versions.append(outcome.prd.version)

    assert produced_versions == [1, 2, 3]

    rows = db.scalars(
        select(PRD).where(PRD.requirement_id == requirement.id).order_by(PRD.version)
    ).all()
    versions = [r.version for r in rows]
    assert versions == [1, 2, 3]
    db.refresh(requirement)
    assert requirement.next_version == 4


def test_create_prd_version_records_grounding_warnings(db, stub_llm):
    """Soft warnings are still surfaced for items that don't block
    persistence: ``baseline_only`` metrics without sources and
    speculative risks with no evidence."""
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    payload = _make_valid_prd_payload(("ext-0",))
    payload["success_metrics"][0]["type"] = "baseline_only"
    payload["success_metrics"][0]["sources"] = []
    payload["risks"][0]["category"] = RISK_SPECULATIVE
    payload["risks"][0]["evidence_ids"] = []
    _StubPRDClient.next_payload = payload
    outcome = create_prd_version(db, requirement.id)
    db.commit()
    codes = [w.code for w in outcome.grounding_warnings]
    assert "metric_baseline_sources_missing" in codes
    assert "speculative_risk" in codes
    warnings_stored = outcome.prd.grounding_warnings
    assert any(w["code"] == "speculative_risk" for w in warnings_stored)
    assert outcome.is_ai_generated is True
    assert outcome.prd.is_ai_generated_draft is True


def test_prd_rejects_fabricated_revenue_metric_name(db, stub_llm):
    """A ``metric_definition`` value that names a fabricated retention
    / revenue / market-share promise must be rejected by the parser.
    ``metric_definition`` is the expected *type* — it does NOT grant a
    free pass on the textual content of the metric name."""
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    payload = _make_valid_prd_payload(("ext-0",))
    payload["success_metrics"][0]["type"] = "metric_definition"
    payload["success_metrics"][0]["sources"] = ["ext-0"]
    payload["success_metrics"][0]["name"] = "提升 25% 留存率"
    _StubPRDClient.next_payload = payload
    with pytest.raises(PRDLLMProtocolError):
        create_prd_version(db, requirement.id)
    db.rollback()


def test_create_prd_version_rejects_unknown_evidence_id(db, stub_llm):
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    payload = _make_valid_prd_payload(("ext-0",))
    payload["evidence_sources"] = ["feedback ghost-id"]
    payload["evidence_ids"] = ["ghost"]
    _StubPRDClient.next_payload = payload
    with pytest.raises(PRDLLMProtocolError):
        create_prd_version(db, requirement.id)
    db.rollback()


@pytest.mark.parametrize(
    "missing_category",
    ["empty_state", "failure", "retry", "partial_success", "invalid_input", "limit_exceeded"],
)
def test_create_prd_version_rejects_missing_edge_case_category(db, stub_llm, missing_category):
    """Strict PRD contract: ``edge_cases`` must enumerate all six
    pre-defined categories. Dropping one means the AI silently skipped
    a known failure mode and the draft must NOT land."""
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    payload = _make_valid_prd_payload(("ext-0",))
    payload["edge_cases"] = [
        edge for edge in payload["edge_cases"] if edge["category"] != missing_category
    ]
    _StubPRDClient.next_payload = payload
    with pytest.raises(PRDLLMProtocolError) as exc:
        create_prd_version(db, requirement.id)
    assert "edge_cases" in str(exc.value)
    assert missing_category in str(exc.value)
    db.rollback()


@pytest.mark.parametrize(
    "field_path, mutate",
    [
        (
            "problem.text",
            lambda p: p.__setitem__("problem", {"text": "需要增加收入", "evidence_ids": ["ext-0"]}),
        ),
        (
            "proposed_solution.text",
            lambda p: p.__setitem__(
                "proposed_solution",
                {"text": "此方案将提升留存", "is_proposed": True, "evidence_ids": ["ext-0"]},
            ),
        ),
        (
            "success_metrics[0].note",
            lambda p: p.__setitem__(
                "success_metrics",
                [
                    {
                        "name": "watch 渗透率",
                        "type": "metric_definition",
                        "note": "预期扩大用户规模",
                        "sources": ["ext-0"],
                    }
                ],
            ),
        ),
        (
            "mvp_scope.in_scope",
            lambda p: p.__setitem__(
                "mvp_scope", {"in_scope": "提升市场份额", "out_of_scope": "其他"}
            ),
        ),
        (
            "background.text",
            lambda p: p.__setitem__(
                "background", {"text": "本期目标增加收入", "evidence_ids": ["ext-0"]}
            ),
        ),
        (
            "acceptance_criteria[0].text",
            lambda p: p.__setitem__(
                "acceptance_criteria",
                [
                    {
                        "text": "This feature will increase revenue",
                        "verifiable": True,
                        "note": "",
                        "evidence_ids": ["ext-0"],
                    }
                ],
            ),
        ),
        (
            "success_metrics[0].name",
            lambda p: p.__setitem__(
                "success_metrics",
                [
                    {
                        "name": "user growth KPI",
                        "type": "metric_definition",
                        "note": "",
                        "sources": ["ext-0"],
                    }
                ],
            ),
        ),
    ],
)
def test_create_prd_version_rejects_fabricated_business_commitment(
    db, stub_llm, field_path, mutate
):
    """Each claim-shaped PRD field must reject an unsupported business
    promise (revenue / retention / market share / user growth / cost
    reduction) — across the AI generation path. ``metric_definition``
    type and the metric name/note positions are scanned; the test
    grid deliberately covers the most common prompt leak vectors."""
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    payload = _make_valid_prd_payload(("ext-0",))
    mutate(payload)
    _StubPRDClient.next_payload = payload
    with pytest.raises(PRDLLMProtocolError) as exc:
        create_prd_version(db, requirement.id)
    message = str(exc.value)
    assert field_path.split("[")[0].split(".")[0] in message or field_path in message
    db.rollback()


def test_create_prd_version_does_not_false_positive_on_risks(db, stub_llm):
    """Risk descriptions and mitigations describe negative scenarios,
    not promises — a phrase like "若不拦截将导致留存下降" inside a
    risk must NOT trigger the commitment scanner."""
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    payload = _make_valid_prd_payload(("ext-0",))
    payload["risks"] = [
        {
            "category": "known",
            "description": "若不拦截将导致留存下降。",
            "evidence_ids": ["ext-0"],
            "mitigation": "增加拦截策略以避免市场份额被挤压。",
        }
    ]
    _StubPRDClient.next_payload = payload
    # Must succeed — the scan does NOT touch risks fields.
    outcome = create_prd_version(db, requirement.id)
    db.commit()
    assert outcome.prd.content["risks"][0]["description"].startswith("若不拦截")


def test_create_prd_version_rejects_unverifiable_acceptance_criterion(db, stub_llm):
    """``verifiable=false`` is no longer a soft warning — it is a hard
    parse-time reject. The downstream ``acceptance_criterion_unverifiable``
    warning code is fully retired."""
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    payload = _make_valid_prd_payload(("ext-0",))
    payload["acceptance_criteria"][0]["verifiable"] = False
    _StubPRDClient.next_payload = payload
    with pytest.raises(PRDLLMProtocolError) as exc:
        create_prd_version(db, requirement.id)
    assert "verifiable" in str(exc.value)
    db.rollback()


def test_pm_authored_prd_rejects_fabricated_business_commitment(db, stub_llm):
    """PM hand-edits go through the same commitment scanner — a manual
    draft that smuggles in "提升留存" must be rejected at parse time,
    not silently persisted."""
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    payload = _make_valid_prd_payload(("ext-0",))
    payload["proposed_solution"] = {
        "text": "本方案将增加收入并提高 DAU 增长",
        "is_proposed": True,
        "evidence_ids": ["ext-0"],
    }
    content = PRDContent.model_validate(payload)
    with pytest.raises(RequirementValidationError):
        create_prd_version(db, requirement.id, pm_content=content)
    db.rollback()


def test_pm_authored_prd_rejects_unverifiable_acceptance_criterion(db, stub_llm):
    """PM PATCH path applies the same ``verifiable=true`` strict rule."""
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    prd = create_prd_version(db, requirement.id).prd
    db.commit()
    payload = _make_valid_prd_payload(("ext-0",))
    payload["acceptance_criteria"][0]["verifiable"] = False
    bad = PRDContent.model_validate(payload)
    with pytest.raises(RequirementValidationError):
        patch_prd(db, prd.id, pm_content=bad)
    db.rollback()


def test_pm_authored_prd_rejects_missing_edge_case_category(db, stub_llm):
    """PM PATCH path enforces the six-category edge-case coverage rule
    just like the AI generation path."""
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    prd = create_prd_version(db, requirement.id).prd
    db.commit()
    payload = _make_valid_prd_payload(("ext-0",))
    payload["edge_cases"] = [edge for edge in payload["edge_cases"] if edge["category"] != "retry"]
    bad = PRDContent.model_validate(payload)
    with pytest.raises(RequirementValidationError) as exc:
        patch_prd(db, prd.id, pm_content=bad)
    assert "edge_cases" in str(exc.value)
    assert "retry" in str(exc.value)
    db.rollback()


def test_create_prd_version_503_on_transport_exhausted(db, monkeypatch):
    _, _, _, insight, _ = _seed(db)
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    _StubRequirementClient.next_payload = None
    requirement = create_requirement(db, insight.id).requirement
    db.commit()

    class _AlwaysTransport:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def generate_prd(self, payload):
            raise PRDLLMTransportError("transient 503")

    monkeypatch.setattr(service_module, "PRDClient", _AlwaysTransport)
    monkeypatch.setattr(settings, "prd_max_retries", 1)
    with pytest.raises(PRDLLMTransportError):
        create_prd_version(db, requirement.id)
    db.rollback()


def test_pm_authored_prd_records_ai_flag_false(db, stub_llm):
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    content = PRDContent.model_validate(_make_valid_prd_payload(("ext-0",)))
    outcome = create_prd_version(db, requirement.id, pm_content=content)
    db.commit()
    assert outcome.is_ai_generated is False
    assert outcome.prd.is_ai_generated_draft is False
    assert outcome.prd.version == 1


def test_prd_accept_idempotent(db, stub_llm):
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    outcome = create_prd_version(db, requirement.id).prd
    db.commit()
    patch_prd(db, outcome.id, status=PRD_REVIEWING)
    db.commit()
    accepted = accept_prd(db, outcome.id)
    db.commit()
    first_stamp = accepted.accepted_at
    second = accept_prd(db, outcome.id)
    db.commit()
    assert second.status == PRD_ACCEPTED
    # Idempotent — second accept does not re-stamp accepted_at.
    assert second.accepted_at == first_stamp
    # No user system means ``accepted_by`` is NULL for every accept,
    # no matter how many times the row is touched.
    assert accepted.accepted_by is None
    assert second.accepted_by is None


def test_prd_accept_rejects_already_accepted_then_rearchive(db, stub_llm):
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    prd = create_prd_version(db, requirement.id).prd
    db.commit()
    accept_prd(db, prd.id)
    db.commit()
    patch_prd(db, prd.id, status=SVC_PRD_ARCHIVED)
    db.commit()
    db.refresh(prd)
    assert prd.status == SVC_PRD_ARCHIVED


def test_prd_accept_leaves_accepted_by_null_without_caller(db, stub_llm):
    """The project has no user system; ``accepted_by`` must be NULL
    on every accept. The previous ``pm_local`` fallback forged a
    user identity in the audit trail; the call surface no longer
    accepts any client-supplied ``accepted_by`` either."""
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    prd = create_prd_version(db, requirement.id).prd
    db.commit()
    accepted = accept_prd(db, prd.id)
    db.commit()
    assert accepted.accepted_by is None
    assert accepted.status == PRD_ACCEPTED


def test_patch_prd_replaces_draft_content_and_updates_hash(db, stub_llm):
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    prd = create_prd_version(db, requirement.id).prd
    db.commit()
    original_hash = prd.content_hash
    edited = PRDContent.model_validate(_make_valid_prd_payload(("ext-0",)))
    edited_payload_dict = edited.model_dump()
    edited_payload_dict["background"]["text"] = "Edited background"
    edited_payload = PRDContent.model_validate(edited_payload_dict)
    patch_prd(db, prd.id, pm_content=edited_payload)
    db.commit()
    db.refresh(prd)
    assert prd.content["background"]["text"] == "Edited background"
    assert prd.content_hash != original_hash
    assert prd.is_ai_generated_draft is False
    assert prd.model is None


def test_patch_prd_edit_preserves_history_on_regenerate(db, stub_llm):
    """The PM-edited PRD lives at v1 with the new text; a subsequent
    AI regenerate appends v2 with the new content. The v1 audit
    row keeps the PM edits untouched.
    """
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    v1 = create_prd_version(db, requirement.id).prd
    db.commit()
    edited = PRDContent.model_validate(_make_valid_prd_payload(("ext-0",)))
    edited_payload_dict = edited.model_dump()
    edited_payload_dict["background"]["text"] = "PM edited background"
    edited_payload = PRDContent.model_validate(edited_payload_dict)
    patch_prd(db, v1.id, pm_content=edited_payload)
    db.commit()
    db.refresh(v1)
    assert v1.content["background"]["text"] == "PM edited background"

    # Regenerate → brand-new v2.
    _StubPRDClient.next_payload = None
    next_version = create_prd_version(db, requirement.id).prd
    db.commit()
    assert next_version.version == 2
    assert next_version.id != v1.id

    # v1 unchanged.
    db.refresh(v1)
    assert v1.content["background"]["text"] == "PM edited background"
    # v2 has the AI default content (background="背景").
    assert next_version.content["background"]["text"] == "背景"


def test_patch_prd_edit_allowed_in_reviewing(db, stub_llm):
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    prd = create_prd_version(db, requirement.id).prd
    db.commit()
    patch_prd(db, prd.id, status=PRD_REVIEWING)
    db.commit()
    edited = PRDContent.model_validate(_make_valid_prd_payload(("ext-0",)))
    edited_dict = edited.model_dump()
    edited_dict["problem"]["text"] = "PM tweaked problem"
    edited_payload = PRDContent.model_validate(edited_dict)
    patch_prd(db, prd.id, pm_content=edited_payload)
    db.commit()
    db.refresh(prd)
    assert prd.content["problem"]["text"] == "PM tweaked problem"
    assert prd.status == PRD_REVIEWING


def test_patch_prd_rejects_edit_after_accept(db, stub_llm):
    """Accepted version is immutable — PM must create a new version."""
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    prd = create_prd_version(db, requirement.id).prd
    db.commit()
    patch_prd(db, prd.id, status=PRD_REVIEWING)
    db.commit()
    accept_prd(db, prd.id)
    db.commit()
    edited = PRDContent.model_validate(_make_valid_prd_payload(("ext-0",)))
    edited_dict = edited.model_dump()
    edited_dict["background"]["text"] = "Attempted edit"
    edited_payload = PRDContent.model_validate(edited_dict)
    with pytest.raises(RequirementValidationError):
        patch_prd(db, prd.id, pm_content=edited_payload)
    db.rollback()
    db.refresh(prd)
    assert prd.content["background"]["text"] != "Attempted edit"


def test_patch_prd_edit_rejects_unknown_evidence_id(db, stub_llm):
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    prd = create_prd_version(db, requirement.id).prd
    db.commit()
    bad = PRDContent.model_validate(_make_valid_prd_payload(("ext-0",)))
    bad_dict = bad.model_dump()
    bad_dict["background"]["evidence_ids"] = ["ghost-evidence"]
    bad_payload = PRDContent.model_validate(bad_dict)
    with pytest.raises(RequirementValidationError):
        patch_prd(db, prd.id, pm_content=bad_payload)
    db.rollback()


def test_patch_prd_edit_rejects_unknown_evidence_in_fr(db, stub_llm):
    """Grounding check for PM-authored edits: ``functional_requirements``
    ``evidence_ids`` must reference the upstream insight evidence. The
    PRD parser enforces this in the same loop as ``_check_evidence_ids``;
    PM edits go through the exact same path so the gate is identical.
    """
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    prd = create_prd_version(db, requirement.id).prd
    db.commit()
    bad = PRDContent.model_validate(_make_valid_prd_payload(("ext-0",)))
    bad_dict = bad.model_dump()
    bad_dict["functional_requirements"][0]["evidence_ids"] = ["ghost-evidence"]
    bad_payload = PRDContent.model_validate(bad_dict)
    with pytest.raises(RequirementValidationError):
        patch_prd(db, prd.id, pm_content=bad_payload)
    db.rollback()


def test_list_prds_returns_history_sorted_by_version_desc(db, stub_llm):
    _, _, _, insight, _ = _seed(db)
    requirement = create_requirement(db, insight.id).requirement
    db.commit()
    for _ in range(3):
        create_prd_version(db, requirement.id)
        db.commit()
    total, rows = list_prds(db, requirement.id)
    assert total == 3
    versions = [r.version for r in rows]
    assert versions == [3, 2, 1]


def test_list_requirements_pagination(db, stub_llm):
    project, _, _, insight, _ = _seed(db)
    create_requirement(db, insight.id)
    db.commit()
    total, rows = list_requirements(db, project.id)
    assert total == 1
    assert rows[0].insight_id == insight.id


# ---- HTTP layer -------------------------------------------------------------


@pytest.fixture
def engine(tmp_path):
    """Standalone engine used by both the seed helper and the FastAPI
    app via the dependency override. Sharing one engine keeps the
    direct DB writes from the seed helper visible to the HTTP client."""
    eng = create_engine(os.environ.get("TEST_DATABASE_URL", f"sqlite:///{tmp_path}/phase5.db"))
    if eng.dialect.name == "sqlite":

        @event.listens_for(eng, "connect")
        def foreign_keys(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    if eng.dialect.name == "postgresql":
        Base.metadata.create_all(eng)
    eng.dispose()


@pytest.fixture
def client(engine, monkeypatch):
    def override():
        with Session(engine) as session:
            yield session

    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_db] = override
    monkeypatch.setattr(settings, "llm_api_key", "test", raising=False)
    monkeypatch.setattr(settings, "llm_base_url", "http://invalid", raising=False)
    monkeypatch.setattr(settings, "llm_model", "test-model", raising=False)
    # Reset the stub class-level mutable state so HTTP tests don't
    # inherit leaked ``next_payload`` / ``fail_with`` from earlier
    # service-layer tests that intentionally fed in malformed data.
    _StubRequirementClient.calls = []
    _StubRequirementClient.fail_with = None
    _StubRequirementClient.next_payload = None
    _StubPRDClient.calls = []
    _StubPRDClient.fail_with = None
    _StubPRDClient.next_payload = None
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _http_seed(engine, decision_status: str = DECISION_ACCEPTED) -> tuple[UUID, UUID]:
    with Session(engine) as session:
        project, _, _, insight, _ = _seed(session, decision_status=decision_status)
        return project.id, insight.id


def _patch_decision_status(engine: object, insight_id: UUID, status: str) -> None:
    """Flip an already-seeded insight's decision status to any Phase 4
    outcome. Lets HTTP tests reuse the ``_http_seed`` helper and then
    drive the eligibility gate from outside the seed path."""
    from sqlalchemy import update

    with Session(engine) as session:
        session.execute(
            update(InsightDecision)
            .where(InsightDecision.insight_id == insight_id)
            .values(status=status)
        )
        session.commit()


def test_post_requirement_creates_ai_draft(client, engine, monkeypatch):
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    _StubRequirementClient.calls = []
    project_id, insight_id = _http_seed(engine)
    response = client.post(f"/insights/{insight_id}/requirement")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["insight_id"] == str(insight_id)
    assert body["status"] == "draft"
    assert body["pm_priority"] == "P1"
    assert body["prompt_version"] == req_prompt.PROMPT_VERSION


def test_post_requirement_idempotent_via_unique_returns_409(client, engine, monkeypatch):
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    project_id, insight_id = _http_seed(engine)
    first = client.post(f"/insights/{insight_id}/requirement")
    assert first.status_code == 201
    second = client.post(f"/insights/{insight_id}/requirement")
    assert second.status_code == 409
    assert "已存在" in second.json()["detail"]


def test_post_requirement_404_for_unknown_insight(client):
    response = client.post(f"/insights/{uuid4()}/requirement")
    assert response.status_code == 404


def test_post_requirement_rejects_blocked_evidence_id(client, engine, monkeypatch):
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    project_id, insight_id = _http_seed(engine)
    bad = _make_valid_requirement_payload(("ext-0",))
    bad["evidence_references"][0]["external_id"] = "ghost"
    _StubRequirementClient.next_payload = bad
    response = client.post(f"/insights/{insight_id}/requirement")
    assert response.status_code == 503  # ProtocolError -> 503


def test_post_requirement_blocks_fabricated_metric_type(client, engine, monkeypatch):
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    project_id, insight_id = _http_seed(engine)
    bad = _make_valid_requirement_payload(("ext-0",))
    bad["success_metrics"][0]["type"] = "fake_percentage"
    _StubRequirementClient.next_payload = bad
    response = client.post(f"/insights/{insight_id}/requirement")
    assert response.status_code == 503


def test_post_requirement_maps_missing_llm_config_to_503(client, engine, monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", None, raising=False)
    monkeypatch.setattr(settings, "llm_api_key", None)
    project_id, insight_id = _http_seed(engine)
    response = client.post(f"/insights/{insight_id}/requirement")
    assert response.status_code == 503


def test_get_requirement_returns_persisted(client, engine, monkeypatch):
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    _, insight_id = _http_seed(engine)
    post = client.post(f"/insights/{insight_id}/requirement")
    requirement_id = post.json()["id"]
    response = client.get(f"/requirements/{requirement_id}")
    assert response.status_code == 200
    assert response.json()["id"] == requirement_id


def test_get_requirement_404(client):
    response = client.get(f"/requirements/{uuid4()}")
    assert response.status_code == 404


def test_patch_requirement_status_transition(client, engine, monkeypatch):
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    response = client.patch(f"/requirements/{rid}", json={"status": "reviewing"})
    assert response.status_code == 200
    assert response.json()["status"] == "reviewing"


def test_patch_requirement_rejects_invalid_transition(client, engine, monkeypatch):
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    response = client.patch(f"/requirements/{rid}", json={"status": "approved"})
    assert response.status_code == 409


def test_get_project_requirements_pagination(client, engine, monkeypatch):
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    project_id, insight_id = _http_seed(engine)
    client.post(f"/insights/{insight_id}/requirement")
    response = client.get(f"/projects/{project_id}/requirements")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["insight_id"] == str(insight_id)


@pytest.mark.parametrize(
    "ineligible_status",
    [DECISION_PENDING_REVIEW, DECISION_REJECTED, DECISION_DEFERRED],
)
def test_post_requirement_409_for_ineligible_decision(
    client, engine, monkeypatch, ineligible_status
):
    """HTTP coverage: only ``accepted`` / ``overridden`` authorise a
    Requirement POST. The other three Phase 4 outcomes must surface as
    409 with the decision-status label in the message body so the PM
    UI can tell which gate fired."""
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    project_id, insight_id = _http_seed(engine)
    _patch_decision_status(engine, insight_id, ineligible_status)
    response = client.post(f"/insights/{insight_id}/requirement")
    assert response.status_code == 409, response.text
    assert ineligible_status in response.json()["detail"]


@pytest.mark.parametrize(
    "eligible_status",
    [DECISION_ACCEPTED, DECISION_OVERRIDDEN],
)
def test_post_requirement_201_for_eligible_decision(client, engine, monkeypatch, eligible_status):
    """HTTP coverage: the two eligible Phase 4 outcomes (accepted /
    overridden) allow Requirement creation. ``overridden`` exercises
    the case where PM has actively committed despite AI recommendation."""
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    project_id, insight_id = _http_seed(engine)
    _patch_decision_status(engine, insight_id, eligible_status)
    response = client.post(f"/insights/{insight_id}/requirement")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["decision_status_snapshot"] == eligible_status
    assert body["insight_id"] == str(insight_id)


def test_get_project_requirements_filters_by_status(client, engine, monkeypatch):
    """The ``status`` query parameter must narrow the result set to the
    requested state and leave other rows out — basic UI pagination by
    lifecycle."""
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    project_id, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    response = client.get(f"/projects/{project_id}/requirements?status=draft")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == rid
    # ``reviewing`` is empty at this point — the row is still in draft.
    empty = client.get(f"/projects/{project_id}/requirements?status=reviewing").json()
    assert empty["total"] == 0
    assert empty["items"] == []
    # Move the row into reviewing and confirm the filter follows.
    client.patch(f"/requirements/{rid}", json={"status": "reviewing"})
    moved = client.get(f"/projects/{project_id}/requirements?status=reviewing").json()
    assert moved["total"] == 1
    assert moved["items"][0]["id"] == rid
    draft_again = client.get(f"/projects/{project_id}/requirements?status=draft").json()
    assert draft_again["total"] == 0


def test_post_prd_creates_version_one(client, engine, monkeypatch):
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    response = client.post(f"/requirements/{rid}/prds")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["version"] == 1
    assert body["status"] == PRD_DRAFT
    assert body["is_ai_generated_draft"] is True


def test_post_prd_version_two_via_history(client, engine, monkeypatch):
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    client.post(f"/requirements/{rid}/prds")
    response = client.post(f"/requirements/{rid}/prds")
    assert response.status_code == 201
    body = response.json()
    assert body["version"] == 2


def test_prd_history_endpoint_returns_versions(client, engine, monkeypatch):
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    client.post(f"/requirements/{rid}/prds")
    client.post(f"/requirements/{rid}/prds")
    response = client.get(f"/requirements/{rid}/prds")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [item["version"] for item in body["items"]] == [2, 1]


def test_prd_history_active_pointer_points_to_highest_version(client, engine, monkeypatch):
    """``PRDPage.active_prd_id`` / ``active_version`` must point at the
    newest PRD in the version sequence, not just whichever row the
    first page happens to start with. With three versions, the active
    pointer is version 3 even though the listing is ordered desc."""
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    created_ids: list[str] = []
    for _ in range(3):
        created_ids.append(client.post(f"/requirements/{rid}/prds").json()["id"])
    response = client.get(f"/requirements/{rid}/prds")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    # Highest version (newest PRD) is the third row we created.
    assert body["active_version"] == 3
    assert body["active_prd_id"] == created_ids[-1]
    # Listing still starts at version 3 by default ordering.
    assert body["items"][0]["version"] == 3
    assert body["items"][-1]["version"] == 1


def test_prd_history_active_pointer_null_when_no_versions(client, engine, monkeypatch):
    """An empty PRD history returns active_prd_id=None / active_version=None
    so consumers don't have to special-case a missing field."""
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    response = client.get(f"/requirements/{rid}/prds")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["items"] == []
    assert body["active_prd_id"] is None
    assert body["active_version"] is None


def test_prd_history_active_pointer_stable_across_pagination(client, engine, monkeypatch):
    """Regression: ``active_prd_id`` / ``active_version`` MUST stay
    pinned to the globally-newest PRD even when ``offset`` skips past
    the active row on the requested page. A naive ``max(rows.version)``
    implementation silently degrades to the highest version in the
    returned slice — this test fails if the pointer drifts with
    pagination."""
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    created_ids: list[str] = []
    for _ in range(3):
        created_ids.append(client.post(f"/requirements/{rid}/prds").json()["id"])
    newest_id = created_ids[-1]
    # Page 1 — offset 0 returns [v3, v2]; active must equal v3.
    page0 = client.get(f"/requirements/{rid}/prds?offset=0&limit=2").json()
    assert [item["version"] for item in page0["items"]] == [3, 2]
    assert page0["active_version"] == 3
    assert page0["active_prd_id"] == newest_id
    # Page 2 — offset 2 returns only [v1] (latest v3 is off the slice).
    # Active pointer must still resolve to v3 / newest_id, NOT v1.
    page1 = client.get(f"/requirements/{rid}/prds?offset=2&limit=2").json()
    assert [item["version"] for item in page1["items"]] == [1]
    assert page1["active_version"] == 3, "active pointer must not collapse into the paginated slice"
    assert page1["active_prd_id"] == newest_id
    # A request with ``limit=1&offset=0`` still surfaces v3 as the
    # single returned item — defensive coverage that the highest
    # version row is reachable and the pointer matches it.
    page_top = client.get(f"/requirements/{rid}/prds?offset=0&limit=1").json()
    assert page_top["active_version"] == 3
    assert page_top["active_prd_id"] == newest_id


def test_prd_accept_endpoint_idempotent(client, engine, monkeypatch):
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    prd_id = client.post(f"/requirements/{rid}/prds").json()["id"]
    client.patch(f"/prds/{prd_id}", json={"status": "reviewing"})
    first = client.post(f"/prds/{prd_id}/accept", json={})
    assert first.status_code == 200
    assert first.json()["status"] == PRD_ACCEPTED
    assert first.json()["accepted_by"] is None
    second = client.post(f"/prds/{prd_id}/accept", json={})
    assert second.status_code == 200
    assert second.json()["accepted_at"] == first.json()["accepted_at"]


def test_prd_accept_rejects_client_supplied_accepted_by(client, engine, monkeypatch):
    """No user system → the client cannot self-attribute. Any
    submitted ``accepted_by`` (or any other field) is rejected by
    Pydantic ``extra=forbid`` and surfaces as 422.
    """
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    prd_id = client.post(f"/requirements/{rid}/prds").json()["id"]
    client.patch(f"/prds/{prd_id}", json={"status": "reviewing"})
    response = client.post(f"/prds/{prd_id}/accept", json={"accepted_by": "pm_xyz"})
    assert response.status_code == 422
    # No state change.
    fetch = client.get(f"/prds/{prd_id}").json()
    assert fetch["status"] == "reviewing"
    assert fetch["accepted_at"] is None


def test_prd_patch_invalid_status_returns_422(client, engine, monkeypatch):
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    prd_id = client.post(f"/requirements/{rid}/prds").json()["id"]
    response = client.patch(f"/prds/{prd_id}", json={"status": "approved"})
    assert response.status_code == 422


def test_http_post_prd_rejects_fabricated_business_commitment(client, engine, monkeypatch):
    """End-to-end: a PRD draft that smuggles in an English revenue
    promise is rejected at the AI generation path before reaching
    persistence. The client surfaces a 503 because the underlying
    error is classified as a protocol failure — same code path as any
    malformed AI tool payload."""
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    bad = _make_valid_prd_payload(("ext-0",))
    bad["proposed_solution"]["text"] = "This feature will increase revenue"
    _StubPRDClient.next_payload = bad
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    response = client.post(f"/requirements/{rid}/prds")
    assert response.status_code == 503
    # And no PRD landed.
    history = client.get(f"/requirements/{rid}/prds").json()
    assert history["total"] == 0


def test_http_post_prd_rejects_missing_edge_case_category(client, engine, monkeypatch):
    """End-to-end: a PRD draft that drops one of the six
    ``edge_cases`` categories is rejected at the AI generation path."""
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    bad = _make_valid_prd_payload(("ext-0",))
    bad["edge_cases"] = [edge for edge in bad["edge_cases"] if edge["category"] != "failure"]
    _StubPRDClient.next_payload = bad
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    response = client.post(f"/requirements/{rid}/prds")
    assert response.status_code == 503
    history = client.get(f"/requirements/{rid}/prds").json()
    assert history["total"] == 0


def test_http_post_prd_rejects_unverifiable_acceptance_criterion(client, engine, monkeypatch):
    """End-to-end: an unverifiable ``acceptance_criteria`` row is no
    longer tolerated — the AI tool payload must carry ``verifiable=true``
    on every criterion or the draft is refused."""
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    bad = _make_valid_prd_payload(("ext-0",))
    bad["acceptance_criteria"][0]["verifiable"] = False
    _StubPRDClient.next_payload = bad
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    response = client.post(f"/requirements/{rid}/prds")
    assert response.status_code == 503
    history = client.get(f"/requirements/{rid}/prds").json()
    assert history["total"] == 0


def test_http_patch_prd_rejects_fabricated_business_commitment(client, engine, monkeypatch):
    """PM PATCH path applies the same commitment scanner at the HTTP
    boundary — a hand edit that injects "提升留存" in
    ``proposed_solution.text`` is rejected with 422."""
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    prd_id = client.post(f"/requirements/{rid}/prds").json()["id"]
    edited = _make_valid_prd_payload(("ext-0",))
    edited["proposed_solution"]["text"] = "本方案将提升留存"
    response = client.patch(f"/prds/{prd_id}", json={"content": edited})
    assert response.status_code == 422


def test_http_patch_prd_rejects_missing_edge_case_category(client, engine, monkeypatch):
    """PM PATCH path applies the six-category edge-case coverage rule."""
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    prd_id = client.post(f"/requirements/{rid}/prds").json()["id"]
    edited = _make_valid_prd_payload(("ext-0",))
    edited["edge_cases"] = [
        edge for edge in edited["edge_cases"] if edge["category"] != "invalid_input"
    ]
    response = client.patch(f"/prds/{prd_id}", json={"content": edited})
    assert response.status_code == 422


def test_http_patch_prd_rejects_unverifiable_acceptance_criterion(client, engine, monkeypatch):
    """PM PATCH path enforces ``verifiable=true``."""
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    prd_id = client.post(f"/requirements/{rid}/prds").json()["id"]
    edited = _make_valid_prd_payload(("ext-0",))
    edited["acceptance_criteria"][0]["verifiable"] = False
    response = client.patch(f"/prds/{prd_id}", json={"content": edited})
    assert response.status_code == 422


def test_prd_accept_404_for_unknown_id(client):
    response = client.post(f"/prds/{uuid4()}/accept", json={})
    assert response.status_code == 404


def test_prd_accept_422_for_non_pm_accepted_transition(client, engine, monkeypatch):
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    prd_id = client.post(f"/requirements/{rid}/prds").json()["id"]
    client.patch(f"/prds/{prd_id}", json={"status": "archived"})
    response = client.post(f"/prds/{prd_id}/accept", json={})
    assert response.status_code in (409, 503)


def test_http_patch_prd_replaces_content_and_updates_hash(client, engine, monkeypatch):
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    prd_id = client.post(f"/requirements/{rid}/prds").json()["id"]
    original = client.get(f"/prds/{prd_id}").json()
    assert original["content"]["background"]["text"] == "背景"
    edited_payload = _make_valid_prd_payload(("ext-0",))
    edited_payload["background"]["text"] = "PM edited HTTP"
    response = client.patch(f"/prds/{prd_id}", json={"content": edited_payload})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["content"]["background"]["text"] == "PM edited HTTP"
    assert body["content_hash"] != original["content_hash"]
    assert body["is_ai_generated_draft"] is False
    # Subsequent GET keeps the edit.
    fetch = client.get(f"/prds/{prd_id}").json()
    assert fetch["content"]["background"]["text"] == "PM edited HTTP"


def test_http_patch_prd_edit_after_accept_returns_409(client, engine, monkeypatch):
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    prd_id = client.post(f"/requirements/{rid}/prds").json()["id"]
    client.patch(f"/prds/{prd_id}", json={"status": "reviewing"})
    accept_response = client.post(f"/prds/{prd_id}/accept", json={})
    assert accept_response.status_code == 200
    edited_payload = _make_valid_prd_payload(("ext-0",))
    edited_payload["background"]["text"] = "Should fail"
    response = client.patch(f"/prds/{prd_id}", json={"content": edited_payload})
    assert response.status_code == 409


def test_http_patch_prd_edit_rejects_unknown_evidence(client, engine, monkeypatch):
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    prd_id = client.post(f"/requirements/{rid}/prds").json()["id"]
    edited_payload = _make_valid_prd_payload(("ext-0",))
    edited_payload["background"]["evidence_ids"] = ["ghost"]
    response = client.patch(f"/prds/{prd_id}", json={"content": edited_payload})
    assert response.status_code == 422


def test_http_post_prd_accept_without_body_leaves_accepted_by_null(client, engine, monkeypatch):
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    prd_id = client.post(f"/requirements/{rid}/prds").json()["id"]
    client.patch(f"/prds/{prd_id}", json={"status": "reviewing"})
    response = client.post(f"/prds/{prd_id}/accept", json={})
    assert response.status_code == 200
    assert response.json()["accepted_by"] is None


# ---- Health integration ----------------------------------------------------


def test_health_ready_includes_phase5_tables(client, engine, monkeypatch):
    monkeypatch.setattr(service_module, "RequirementClient", _StubRequirementClient)
    monkeypatch.setattr(service_module, "PRDClient", _StubPRDClient)
    _, insight_id = _http_seed(engine)
    rid = client.post(f"/insights/{insight_id}/requirement").json()["id"]
    client.post(f"/requirements/{rid}/prds")
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
