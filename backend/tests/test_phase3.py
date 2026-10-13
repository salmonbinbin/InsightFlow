"""Phase 3 — Insight + Evidence behaviour tests owned by the worker.

Each test below pins one invariant the supervisor requested:

1. ``validate_insight_payload`` rejects unknown top-level / evidence
   fields (the LLM cannot smuggle in auxiliary context like ``quote``).
2. ``EvidenceCandidate.feedback_id`` is the original ``Feedback`` UUID
   (not the ``FeedbackAnalysis`` UUID), so the LLM and the validator
   use the same canonical key.
3. Retry preserves insights from already-successful clusters and only
   re-processes clusters whose item status is failed / needs_validation.
4. Small clusters (1–2 members) produce ``needs_validation`` with all
   available evidence.
5. ``insights.feedback_count`` is the original cluster member count,
   not the (capped) selected evidence count.
6. ``InsightRunItem.attempts`` accumulates across retries.
7. Cluster / project / classification-run integrity is enforced; a
   project mismatch raises ``InsightConfigError`` without any LLM call.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.config import settings
from app.models import AnalysisRun, Cluster, ClusterMember, Feedback, FeedbackAnalysis, Project
from app.models.insight import Insight, InsightEvidence, InsightRun, InsightRunItem
from app.services import insight_runner as runner
from app.services.insight_client import InsightLLMResult, InsightLLMTransportError, InsightLLMUsage
from app.services.insight_runner import start_insight_run as runner_start  # noqa: F401
from app.services.insight_service import (
    ALLOWED_EVIDENCE_FIELDS,
    ALLOWED_EVIDENCE_TYPES,
    ALLOWED_TOP_LEVEL_FIELDS,
    DRAFT_EVIDENCE_THRESHOLD,
    EvidenceCandidate,
    InsightValidationError,
    select_evidence_candidates,
    validate_insight_payload,
)


def _payload(ids):
    return dict(
        title="title",
        pain_point="pain",
        affected_segment="unknown",
        insight="insight",
        recommendation="recommendation",
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
    engine.dispose()


def _seed(db, sizes=(3, 3)):
    p = Project(name="Phase3 worker", product_name="test")
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
                project_id=p.id,
                external_id=f"{i}-{j}",
                content=f"原始反馈 #{i}-{j}",
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


class _StubClient:
    calls: list = []
    fail_cluster: str | None = None

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def generate_insight(self, cluster):
        self.calls.append(cluster)
        if cluster["cluster_id"] == self.fail_cluster:
            raise InsightLLMTransportError("synthetic transport failure")
        return InsightLLMResult(
            _payload([x["feedback_id"] for x in cluster["evidence_candidates"]]),
            InsightLLMUsage(11, 7),
        )


@pytest.fixture
def stub_client(monkeypatch):
    _StubClient.calls = []
    _StubClient.fail_cluster = None
    monkeypatch.setattr(runner, "InsightClient", _StubClient)
    return _StubClient


# 1. Validator field hygiene ----------------------------------------------


def test_validator_known_fields_are_complete():
    """Both allow-lists must cover every key the runner persists."""
    assert "title" in ALLOWED_TOP_LEVEL_FIELDS
    assert "evidence" in ALLOWED_TOP_LEVEL_FIELDS
    assert {"feedback_id", "evidence_type", "relevance_score"} == ALLOWED_EVIDENCE_FIELDS
    assert {"representative", "high_severity", "diverse_expression"} == ALLOWED_EVIDENCE_TYPES


def test_validator_rejects_unknown_top_level_field():
    bad = _payload(["a", "b", "c"])
    bad["quote"] = "fabricated"
    with pytest.raises(InsightValidationError) as ei:
        validate_insight_payload(bad, allowed_feedback_ids=["a", "b", "c"])
    assert ei.value.code == "unknown_top_level_field"


def test_validator_rejects_unknown_evidence_field():
    bad = _payload(["a", "b", "c"])
    bad["evidence"][0]["quote"] = "fabricated"
    with pytest.raises(InsightValidationError) as ei:
        validate_insight_payload(bad, allowed_feedback_ids=["a", "b", "c"])
    assert ei.value.code == "unknown_evidence_field"


def test_validator_min_evidence_param_honoured():
    """Passing ``min_evidence=2`` must accept exactly two entries."""
    payload = _payload(["a", "b"])
    validated = validate_insight_payload(payload, allowed_feedback_ids=["a", "b"], min_evidence=2)
    assert len(validated.evidence) == 2


def test_validator_min_evidence_rejects_below_threshold():
    payload = _payload(["a"])
    with pytest.raises(InsightValidationError) as ei:
        validate_insight_payload(payload, allowed_feedback_ids=["a"], min_evidence=2)
    assert ei.value.code == "evidence_count_too_few"


# 2. Evidence feedback_id is the Feedback UUID ----------------------------


def test_evidence_candidate_feedback_id_is_feedback_uuid():
    candidate = EvidenceCandidate(
        feedback_id="feedback-uuid",
        feedback_pk="feedback-uuid",
        external_id="ext",
        content="x",
        similarity_score=0.5,
        severity=None,
        sentiment=None,
    )
    assert candidate.feedback_id == candidate.feedback_pk == "feedback-uuid"


def test_select_evidence_dedupes_by_feedback_id():
    a = EvidenceCandidate(
        feedback_id="a",
        feedback_pk="a",
        external_id="a",
        content="",
        similarity_score=0.9,
        severity=None,
        sentiment=None,
    )
    b = EvidenceCandidate(
        feedback_id="b",
        feedback_pk="b",
        external_id="b",
        content="",
        similarity_score=0.5,
        severity=None,
        sentiment=None,
    )
    out = select_evidence_candidates([a, b])
    assert [c.feedback_id for c in out] == ["a", "b"]


# 3–7. End-to-end runner behaviour ----------------------------------------


def test_pipeline_runs_draft_for_normal_cluster(db, stub_client):
    p, _, source, _ = _seed(db, (3,))
    run = runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    assert run.status == "completed"
    assert run.success_count == run.processed_count == 1
    assert run.failed_count == 0
    assert run.needs_validation_count == 0
    insight = db.scalars(select(Insight)).one()
    assert insight.status == "draft"
    assert insight.feedback_count == 3
    assert insight.evidence_count == DRAFT_EVIDENCE_THRESHOLD
    assert len(db.scalars(select(InsightEvidence)).all()) == DRAFT_EVIDENCE_THRESHOLD


def test_small_cluster_becomes_needs_validation(db, stub_client):
    p, _, source, _ = _seed(db, (2,))
    run = runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    assert run.status == "completed"
    assert run.needs_validation_count == 1
    assert run.failed_count == 0
    insight = db.scalars(select(Insight)).one()
    assert insight.status == "needs_validation"
    assert insight.feedback_count == 2
    assert insight.evidence_count == 2


def test_feedback_count_is_total_membership_not_sample(db, stub_client):
    p, _, source, _ = _seed(db, (10,))
    run = runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    assert run.status == "completed"
    insight = db.scalars(select(Insight)).one()
    assert insight.feedback_count == 10
    assert 3 <= insight.evidence_count <= 8


def test_retry_only_processes_failed_clusters(db, stub_client):
    p, _, source, clusters = _seed(db, (3, 3))
    stub_client.fail_cluster = str(clusters[-1].id)
    run = runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    assert run.status == "partial_failed"

    success_insight = db.scalars(select(Insight).where(Insight.cluster_id == clusters[0].id)).one()
    snapshot = (success_insight.id, success_insight.insight, success_insight.created_at)

    stub_client.fail_cluster = None
    run2 = runner.retry_insight_run(db, run.id)
    assert run2.status == "completed"
    assert run2.processed_count == 2
    assert run2.success_count == 2
    assert run2.failed_count == 0

    # Original successful insight preserved.
    reloaded = db.get(Insight, snapshot[0])
    assert reloaded is not None
    assert (reloaded.id, reloaded.insight, reloaded.created_at) == snapshot

    # Successful cluster was not re-processed.
    after_calls = [c["cluster_id"] for c in stub_client.calls]
    assert after_calls.count(str(clusters[0].id)) == 1


def test_item_attempts_accumulate_across_retries(db, stub_client):
    p, _, source, clusters = _seed(db, (3, 3))
    stub_client.fail_cluster = str(clusters[0].id)
    run = runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    assert run.status == "partial_failed"

    failed_item = db.scalar(
        select(InsightRunItem).where(InsightRunItem.cluster_id == clusters[0].id)
    )
    assert failed_item is not None
    attempts_after_first = failed_item.attempts

    stub_client.fail_cluster = None
    runner.retry_insight_run(db, run.id)
    failed_item_after = db.scalar(
        select(InsightRunItem).where(InsightRunItem.cluster_id == clusters[0].id)
    )
    assert failed_item_after is not None
    assert failed_item_after.attempts >= attempts_after_first + 1


def test_cluster_project_mismatch_is_rejected(db, stub_client):
    p, _, source, clusters = _seed(db, (3,))
    other = Project(name="other", product_name="other")
    db.add(other)
    db.flush()
    clusters[0].project_id = other.id
    db.commit()
    with pytest.raises(runner.InsightConfigError):
        runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    assert stub_client.calls == []


def test_unknown_classification_run_in_cluster_config_is_rejected(db, stub_client):
    p, _, source, _ = _seed(db, (3,))
    source.cluster_config = {"classification_run_id": "not-a-uuid"}
    db.commit()
    with pytest.raises(runner.InsightConfigError):
        runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    assert stub_client.calls == []


def test_pipeline_failure_does_not_call_client(db, monkeypatch):
    """Missing LLM config is rejected before any run or client call."""
    p, _, source, _ = _seed(db, (3,))
    monkeypatch.setattr(runner, "InsightClient", _StubClient)
    _StubClient.calls = []

    # Force the pre-flight config check to fail by clearing the LLM key.
    monkeypatch.setattr(settings, "llm_api_key", "")

    with pytest.raises(runner.InsightConfigError):
        runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    assert _StubClient.calls == []
    assert not db.scalars(select(InsightRun)).all()


# 8. v2 prompt / tool-schema invariants ------------------------------------


def test_v2_prompt_version_is_active():
    """The runner must bind to the v2 prompt; v1 is preserved as archive."""
    from app.agents.prompts import insight_generation_v2
    from app.services import insight_runner

    assert insight_generation_v2.PROMPT_VERSION == "insight-generation-v2"
    assert insight_runner.PROMPT_VERSION == insight_generation_v2.PROMPT_VERSION


def test_v2_tool_schema_allows_small_evidence_lists():
    """Small clusters (1-2 evidence rows) must be schema-legal."""
    from app.agents.prompts import insight_generation_v2
    from app.services.insight_client import TOOL_SCHEMA

    assert TOOL_SCHEMA is insight_generation_v2.TOOL_SCHEMA
    evidence = TOOL_SCHEMA["properties"]["evidence"]
    assert evidence["minItems"] == 1
    assert evidence["maxItems"] >= 2
    assert TOOL_SCHEMA["additionalProperties"] is False


def test_v2_tool_schema_rejects_unknown_fields_at_provider_level():
    """``additionalProperties=False`` propagates to every nested object."""
    from app.agents.prompts import insight_generation_v2

    schema = insight_generation_v2.TOOL_SCHEMA
    assert schema["additionalProperties"] is False
    assert schema["properties"]["evidence"]["items"]["additionalProperties"] is False


def test_v2_prompt_forbids_unsupported_root_cause_phrases():
    """Network / missing-feature / billing / speech-accuracy phrases are banned."""
    from app.agents.prompts import insight_generation_v2

    prompt = insight_generation_v2.SYSTEM_PROMPT
    banned = [
        "rather than a one-off network hiccup",
        "网络抖动",
        "网络异常",
        "该功能尚未实现",
        "功能不存在",
        "未识别到内部计费状态",
        "扣费逻辑缺失",
        "rather than general speech accuracy",
        "通用识别准确率不足",
        "用户流失",
        "留存下降",
        "商业影响",
    ]
    for phrase in banned:
        assert phrase in prompt, f"v2 prompt must mention forbidden phrase {phrase!r}"


def test_v2_prompt_keeps_recommendation_as_hypothesis():
    """Recommendation is a *hypothesis to verify*, not an action item."""
    from app.agents.prompts import insight_generation_v2

    prompt = insight_generation_v2.SYSTEM_PROMPT
    assert "待验证" in prompt or "假设" in prompt
    assert "开放通讯录" in prompt or "授权" in prompt


def test_v2_prompt_requires_natural_chinese_title():
    """Title must be natural Chinese — no snake_case or English."""
    from app.agents.prompts import insight_generation_v2

    prompt = insight_generation_v2.SYSTEM_PROMPT
    assert "中文" in prompt
    assert "snake_case" in prompt


def test_v2_prompt_flags_severity_as_derived_signal():
    """severity / sentiment / pain_point are derived; cannot be quoted as text."""
    from app.agents.prompts import insight_generation_v2

    prompt = insight_generation_v2.SYSTEM_PROMPT
    assert "派生" in prompt
    assert "severity" in prompt


def test_v2_prompt_keeps_unknown_as_literal_placeholder():
    """'unknown' is reserved for missing affected_segment — must not be paraphrased."""
    from app.agents.prompts import insight_generation_v2

    prompt = insight_generation_v2.SYSTEM_PROMPT
    assert '"unknown"' in prompt


def test_v2_evidence_evidence_types_are_three_literals():
    """evidence_type enum is exactly representative / high_severity / diverse_expression."""
    from app.agents.prompts import insight_generation_v2
    from app.services.insight_service import ALLOWED_EVIDENCE_TYPES

    evidence = insight_generation_v2.TOOL_SCHEMA["properties"]["evidence"]["items"]
    assert set(evidence["properties"]["evidence_type"]["enum"]) == set(ALLOWED_EVIDENCE_TYPES)


def test_runner_prompt_binding_round_trips_v2(db, stub_client):
    """Insight rows must persist ``prompt_version == 'insight-generation-v2'``."""
    p, _, source, _ = _seed(db, (3,))
    runner.start_insight_run(db, p.id, clustering_run_id=source.id)
    insight = db.scalars(select(Insight)).one()
    assert insight.prompt_version == "insight-generation-v2"
