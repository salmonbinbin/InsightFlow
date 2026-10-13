"""Phase 5 — Requirement + PRD orchestration.

Two responsibilities live here:

1. :func:`create_requirement` — call the AI Requirement client (or
   accept PM-authored content), validate grounding, persist one
   ``Requirement`` row per Insight. A duplicate POST for the same
   insight returns 409 so PM must explicitly PATCH or archive the
   existing draft.

2. :func:`create_prd_version` — call the AI PRD client (or accept
   PM-authored content), validate the 13-section contract, atomically
   increment ``requirements.next_version`` inside the same transaction
   as the insert so concurrent requests cannot collide on the unique
   ``(requirement_id, version)`` constraint.

Both helpers share the same grounding discipline:

* ``success_metrics`` items must use the ``metric_definition`` or
  ``baseline_only`` type; fabricated percentage / revenue targets are
  rejected at the schema layer.
* ``risks[].evidence_ids`` must reference IDs present in the upstream
  Evidence input when ``category == "known"``; ``speculative`` risks
  are recorded into ``grounding_warnings`` rather than blocking the
  draft.
* ``evidence_references`` / ``evidence_ids`` only accept IDs that
  appear in the input payload, so the AI cannot cite a hallucinated
  feedback ID.

AI errors are categorised the same way as Phase 3/4:

* :class:`RequirementLLMConfigError` / :class:`PRDLLMConfigError`
  (missing env) → caller maps to 503.
* :class:`RequirementLLMTransportError` / :class:`PRDLLMTransportError`
  (5xx / 429 / network) → bounded retry, then 503.
* :class:`RequirementLLMProtocolError` / :class:`PRDLLMProtocolError`
  (malformed JSON / wrong tool / schema violation) → fail closed
  immediately, 503.
* Grounding / unsupported-claim errors → :class:`RequirementValidationError`
  mapped to 422 by the API layer.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.config import settings
from app.models import Insight, InsightDecision, InsightStatus
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
    PRD_ARCHIVED,
    PRD_DRAFT,
    PRD_REVIEWING,
    PRD_STATUS_VALUES,
    REQUIREMENT_APPROVED,
    REQUIREMENT_ARCHIVED,
    REQUIREMENT_DRAFT,
    REQUIREMENT_REJECTED,
    REQUIREMENT_REVIEWING,
    REQUIREMENT_STATUS_VALUES,
    RISK_SPECULATIVE,
    Requirement,
)
from app.schemas.requirement_prd import (
    EvidenceReferenceRead,
    GroundingWarning,
    PMRequirementContent,
    PRDContent,
    PRDStatusLiteral,
    RequirementStatusLiteral,
    RiskRead,
    SuccessMetricRead,
)
from app.services.opportunity_scoring import SourceBundle, _load_source_bundle
from app.services.requirement_prd_client import (
    PRDClient,
    PRDLLMConfigError,
    PRDLLMProtocolError,
    PRDLLMResult,
    PRDLLMTransportError,
    RequirementClient,
    RequirementLLMConfigError,
    RequirementLLMProtocolError,
    RequirementLLMResult,
    RequirementLLMTransportError,
)

# ---- Error taxonomy ----------------------------------------------------------


class RequirementError(RuntimeError):
    """Base class for Phase 5 service errors."""


class RequirementValidationError(RequirementError):
    """Input that violates the Phase 5 contract; mapped to 422."""

    @property
    def code(self) -> str:
        message = str(self)
        for marker, code in (
            ("not found", "not_found"),
            ("insight not found", "insight_not_found"),
            ("decision not found", "decision_not_found"),
            ("not eligible for Requirement", "decision_ineligible"),
            ("not ready", "score_incomplete"),
            ("pending_review only", "decision_not_eligible"),
            ("duplicate", "duplicate_requirement"),
            ("unknown status", "status_invalid"),
            ("PM Priority is required", "pm_priority_missing"),
            ("evidence_id", "evidence_id_unknown"),
            ("evidence_references", "evidence_references_invalid"),
            ("evidence_ids is required", "evidence_ids_missing"),
            ("content immutable", "content_immutable"),
            ("success_metrics", "success_metrics_invalid"),
            ("risks", "risks_invalid"),
            ("acceptance_criteria", "acceptance_criteria_invalid"),
            ("edge_cases", "edge_cases_invalid"),
            ("functional_requirements", "functional_requirements_invalid"),
            ("evidence_sources", "evidence_sources_invalid"),
            ("proposed_solution", "proposed_solution_invalid"),
            ("grounding_warnings", "grounding_warnings_invalid"),
            ("requirement not found", "requirement_not_found"),
            ("prd not found", "prd_not_found"),
            ("prd archived", "prd_archived"),
            ("requirement archived", "requirement_archived"),
            ("content is required", "content_missing"),
            ("name is required", "name_missing"),
            ("problem is required", "problem_missing"),
            ("target_user is required", "target_user_missing"),
            ("user_story is required", "user_story_missing"),
            ("solution_hypothesis is required", "solution_hypothesis_missing"),
            ("pm_priority must", "pm_priority_invalid"),
            ("status transition", "status_transition_invalid"),
            ("section", "section_invalid"),
            ("text is required", "text_missing"),
            ("description is required", "description_missing"),
            ("usage is required", "usage_missing"),
            ("note is required", "note_invalid"),
            ("maxLength", "length_exceeded"),
            ("minLength", "length_too_small"),
            ("field", "field_invalid"),
            ("required", "field_missing"),
            ("type", "type_invalid"),
            ("category", "category_invalid"),
            ("verifiable", "verifiable_invalid"),
            ("is_proposed", "is_proposed_invalid"),
            ("out_of_scope", "mvp_scope_invalid"),
            ("in_scope", "mvp_scope_invalid"),
            ("idempotent", "idempotency_violation"),
        ):
            if marker in message:
                return code
        return "schema_invalid"


class RequirementConfigError(RequirementError):
    """Missing LLM configuration — surfaced as 503."""


class RequirementNotFoundError(RequirementValidationError):
    """A specific row is required but missing — surfaced as 404."""


# ---- Status transition rules -------------------------------------------------

_REQUIREMENT_TRANSITIONS: dict[str, set[str]] = {
    REQUIREMENT_DRAFT: {REQUIREMENT_REVIEWING, REQUIREMENT_ARCHIVED},
    REQUIREMENT_REVIEWING: {REQUIREMENT_APPROVED, REQUIREMENT_REJECTED, REQUIREMENT_DRAFT},
    REQUIREMENT_APPROVED: {REQUIREMENT_ARCHIVED},
    REQUIREMENT_REJECTED: {REQUIREMENT_DRAFT, REQUIREMENT_ARCHIVED},
    REQUIREMENT_ARCHIVED: set(),
}

_PRD_TRANSITIONS: dict[str, set[str]] = {
    PRD_DRAFT: {PRD_REVIEWING, PRD_ARCHIVED},
    PRD_REVIEWING: {PRD_DRAFT, PRD_ARCHIVED, PRD_ACCEPTED},
    PRD_ACCEPTED: {PRD_ARCHIVED},
    PRD_ARCHIVED: set(),
}


def _validate_requirement_status_transition(current: str, target: str) -> None:
    allowed = _REQUIREMENT_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise RequirementValidationError(f"status transition {current} -> {target} not allowed")


def _validate_prd_status_transition(current: str, target: str) -> None:
    allowed = _PRD_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise RequirementValidationError(f"status transition {current} -> {target} not allowed")


# ---- Constants --------------------------------------------------------------


_MAX_REQUIREMENT_TEXT = 4000
_MAX_REQUIREMENT_NAME = 200
_MAX_RISK_DESCRIPTION = 2000
_MAX_METRIC_NAME = 200
_MAX_METRIC_NOTE = 500
_MAX_USAGE = 500
_MAX_EVIDENCE_ID = 200
_MAX_FR_TEXT = 2000
_MAX_PRD_TEXT = 4000
_MAX_AC_NOTE = 500


@dataclass(frozen=True)
class Phase4Snapshot:
    """Snapshot of the Phase 4 decision bound to a Requirement."""

    insight_id: UUID
    project_id: UUID
    decision_id: UUID
    score_id: UUID | None
    insight_status: str
    decision_status: str
    ai_priority: str | None
    pm_priority: str | None
    pm_override_reason: str | None
    source_snapshot_hash: str
    evidence_external_ids: tuple[str, ...]


# ---- Source bundle loading --------------------------------------------------


def load_phase4_snapshot(db: Session, insight_id: UUID) -> Phase4Snapshot:
    """Load the Phase 4 decision + Evidence rows required by Phase 5.

    Reuses :func:`app.services.opportunity_scoring._load_source_bundle`
    so the provenance chain matches Phase 4 byte-for-byte. Caller
    owns the transaction.
    """
    insight = db.get(Insight, insight_id)
    if insight is None:
        raise RequirementNotFoundError("insight not found")

    decision = db.scalar(select(InsightDecision).where(InsightDecision.insight_id == insight_id))
    if decision is None:
        raise RequirementValidationError("decision not found")

    if decision.pm_priority is None:
        raise RequirementValidationError("PM Priority is required")
    if decision.pm_priority not in PRIORITY_VALUES:
        raise RequirementValidationError("pm_priority must be one of P0/P1/P2")
    # PM-driven entry gate: a Requirement can only be created once the
    # PM has actively committed to the opportunity (``accepted`` or
    # ``overridden``). The other three Phase 4 outcomes — pending
    # review, rejected, deferred — are explicit stops and surface as
    # 409 at the API layer.
    if decision.status not in (DECISION_ACCEPTED, DECISION_OVERRIDDEN):
        raise RequirementValidationError(
            f"decision status '{decision.status}' not eligible for Requirement"
        )

    bundle = _load_source_bundle(db, insight)
    evidence_ids = sorted({str(ev.feedback_external_id or "") for ev in bundle.evidence_rows})
    evidence_ids = [eid for eid in evidence_ids if eid]

    return Phase4Snapshot(
        insight_id=insight.id,
        project_id=insight.project_id,
        decision_id=decision.id,
        score_id=decision.current_score_id,
        insight_status=insight.status or InsightStatus.DRAFT.value,
        decision_status=decision.status,
        ai_priority=decision.ai_priority,
        pm_priority=decision.pm_priority,
        pm_override_reason=decision.override_reason,
        source_snapshot_hash=bundle.snapshot.snapshot_hash,
        evidence_external_ids=tuple(evidence_ids),
    )


PRIORITY_VALUES: tuple[str, ...] = ("P0", "P1", "P2")

REQUIREMENT_ELIGIBLE_DECISION_STATUSES: tuple[str, ...] = (
    DECISION_ACCEPTED,
    DECISION_OVERRIDDEN,
)

# Tuple of ineligible decision statuses (rejected / deferred /
# pending_review) — exported for tests + HTTP-doc consumers.
REQUIREMENT_INELIGIBLE_DECISION_STATUSES: tuple[str, ...] = (
    DECISION_PENDING_REVIEW,
    DECISION_DEFERRED,
    DECISION_REJECTED,
)


def _build_requirement_input(
    *,
    insight: Insight,
    decision: InsightDecision,
    snapshot: Phase4Snapshot,
    bundle: SourceBundle,
) -> dict[str, Any]:
    """Compose the LLM input payload for the Requirement prompt."""
    insight_block = {
        "title": insight.title,
        "pain_point": insight.pain_point,
        "affected_segment": insight.affected_segment,
        "insight": insight.insight,
        "recommendation": insight.recommendation,
        "status": insight.status,
        "feedback_count": insight.feedback_count,
        "evidence_count": insight.evidence_count,
    }
    decision_block = {
        "decision_id": str(snapshot.decision_id),
        "decision_status": snapshot.decision_status,
        "ai_priority": snapshot.ai_priority,
        "pm_priority": snapshot.pm_priority,
        "pm_override_reason": snapshot.pm_override_reason,
        "business_value": (
            float(decision.business_value) if decision.business_value is not None else None
        ),
        "version": decision.version,
    }
    evidence_payload = []
    for row in bundle.evidence_rows:
        feedback = bundle.feedback_by_id.get(row.feedback_id)
        analysis = bundle.members_by_id.get(row.feedback_id)
        if feedback is None or analysis is None:
            continue
        evidence_payload.append(
            {
                "feedback_external_id": feedback.external_id,
                "feedback_content": feedback.content,
                "user_id": feedback.user_id,
                "severity": analysis.severity,
                "sentiment": analysis.sentiment,
                "feedback_type": analysis.feedback_type,
                "module": analysis.module,
                "pain_point": analysis.pain_point,
                "intent": analysis.intent,
                "feedback_created_at": (
                    feedback.feedback_created_at.isoformat()
                    if feedback.feedback_created_at
                    else None
                ),
            }
        )
    return {
        "insight": insight_block,
        "decision": decision_block,
        "evidence_external_ids": list(snapshot.evidence_external_ids),
        "evidence": evidence_payload,
    }


def _build_prd_input(
    *,
    requirement: Requirement,
    insight: Insight,
    snapshot: Phase4Snapshot,
    bundle: SourceBundle,
) -> dict[str, Any]:
    requirement_block = {
        "id": str(requirement.id),
        "name": requirement.name,
        "problem": requirement.problem,
        "target_user": requirement.target_user,
        "user_story": requirement.user_story,
        "solution_hypothesis": requirement.solution_hypothesis,
        "success_metrics": requirement.success_metrics,
        "risks": requirement.risks,
        "evidence_references": requirement.evidence_references,
    }
    decision_block = {
        "decision_id": str(snapshot.decision_id),
        "decision_status": snapshot.decision_status,
        "ai_priority_snapshot": requirement.ai_priority_snapshot,
        "pm_priority": requirement.pm_priority,
        "pm_override_reason": requirement.pm_override_reason_snapshot,
        "accepted": True,
    }
    evidence_payload = []
    for row in bundle.evidence_rows:
        feedback = bundle.feedback_by_id.get(row.feedback_id)
        analysis = bundle.members_by_id.get(row.feedback_id)
        if feedback is None or analysis is None:
            continue
        evidence_payload.append(
            {
                "feedback_external_id": feedback.external_id,
                "feedback_content": feedback.content,
                "user_id": feedback.user_id,
                "severity": analysis.severity,
                "sentiment": analysis.sentiment,
                "feedback_type": analysis.feedback_type,
                "module": analysis.module,
                "pain_point": analysis.pain_point,
                "intent": analysis.intent,
            }
        )
    return {
        "requirement": requirement_block,
        "insight": {
            "title": insight.title,
            "pain_point": insight.pain_point,
            "affected_segment": insight.affected_segment,
            "insight": insight.insight,
            "recommendation": insight.recommendation,
            "status": insight.status,
        },
        "phase4_decision": decision_block,
        "evidence_external_ids": list(snapshot.evidence_external_ids),
        "evidence": evidence_payload,
    }


# ---- Grounding validators ---------------------------------------------------


def _check_string(value: Any, *, field_name: str, max_length: int, min_length: int = 1) -> str:
    if not isinstance(value, str):
        raise RequirementValidationError(f"{field_name} field must be a string")
    if min_length and len(value.strip()) < min_length:
        raise RequirementValidationError(f"{field_name} must be a non-empty string")
    if max_length and len(value) > max_length:
        raise RequirementValidationError(f"{field_name} maxLength exceeded ({max_length})")
    return value


def _check_evidence_ids(values: Any, *, allowed: set[str], field_name: str) -> list[str]:
    if not isinstance(values, list):
        raise RequirementValidationError(f"{field_name} must be a list")
    cleaned: list[str] = []
    for index, item in enumerate(values):
        if not isinstance(item, str):
            raise RequirementValidationError(f"{field_name}[{index}] must be a string")
        cleaned_id = item.strip()
        if not cleaned_id:
            raise RequirementValidationError(f"{field_name}[{index}] must be non-empty")
        if len(cleaned_id) > _MAX_EVIDENCE_ID:
            raise RequirementValidationError(
                f"{field_name}[{index}] exceeds maxLength {_MAX_EVIDENCE_ID}"
            )
        if cleaned_id not in allowed:
            raise RequirementValidationError(
                f"{field_name}[{index}] evidence_id '{cleaned_id}' not in input evidence"
            )
        cleaned.append(cleaned_id)
    return cleaned


# ---- Business commitment ground rules ---------------------------------------


# Patterns that describe an unsupported business-impact promise — a
# claim about revenue, retention, market share, user-base growth or
# development-cost reduction. The PRD contract forbids these unless
# they are sourced from structured input the LLM has not been given,
# so the conservative default is to reject any phrasing that looks
# like one of these patterns. The list is intentionally narrow so it
# does not over-fire on neutral metric descriptions ("watch
# penetration"), risk descriptions ("risk: 留存下降") or metric
# definitions. ``metric_definition`` is the *type* the prompt expects
# the AI to emit, not a free pass on text content.
_BUSINESS_COMMITMENT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"提升.{0,8}收入", "revenue_increase"),
    (r"增加.{0,8}收入", "revenue_increase"),
    (r"带来.{0,8}收入", "revenue_increase"),
    (r"拉动.{0,8}收入", "revenue_increase"),
    (r"营收.{0,6}提升", "revenue_increase"),
    (r"营收.{0,6}增长", "revenue_increase"),
    (r"increase revenue", "revenue_increase"),
    (r"boost revenue", "revenue_increase"),
    (r"drive revenue", "revenue_increase"),
    (r"revenue increase", "revenue_increase"),
    (r"revenue growth", "revenue_increase"),
    (r"\bincrease\b.{0,20}\brevenue\b", "revenue_increase"),
    (r"This feature will increase revenue", "revenue_increase"),
    (r"This will.{0,40}revenue", "revenue_increase"),
    (r"提升.{0,8}留存", "retention_increase"),
    (r"提高.{0,8}留存", "retention_increase"),
    (r"留存率.{0,6}提升", "retention_increase"),
    (r"留存率.{0,6}增长", "retention_increase"),
    (r"\bretention\b.{0,30}\bincrease\b", "retention_increase"),
    (r"\bincrease\b.{0,30}\bretention\b", "retention_increase"),
    (r"improve retention", "retention_increase"),
    (r"扩大.{0,8}用户.{0,6}规模", "user_growth"),
    (r"扩大.{0,8}用户量", "user_growth"),
    (r"用户.{0,6}增长", "user_growth"),
    (r"用户量.{0,6}增长", "user_growth"),
    (r"MAU.{0,6}增长", "user_growth"),
    (r"DAU.{0,6}增长", "user_growth"),
    (r"提升.{0,8}DAU", "user_growth"),
    (r"提升.{0,8}MAU", "user_growth"),
    (r"提高.{0,8}DAU", "user_growth"),
    (r"提高.{0,8}MAU", "user_growth"),
    (r"用户增长", "user_growth"),
    (r"用户规模.{0,6}增长", "user_growth"),
    (r"user base.{0,30}expand", "user_growth"),
    (r"user growth", "user_growth"),
    (r"MAU growth", "user_growth"),
    (r"DAU growth", "user_growth"),
    (r"user-base expansion", "user_growth"),
    (r"grow.{0,20}user base", "user_growth"),
    (r"grow DAU", "user_growth"),
    (r"grow MAU", "user_growth"),
    (r"扩大.{0,8}用户.{0,6}增长", "user_growth"),
    (r"增加.{0,8}市场份额", "market_share"),
    (r"提升.{0,8}市场份额", "market_share"),
    (r"扩大.{0,8}市场份额", "market_share"),
    (r"市场份额.{0,6}提升", "market_share"),
    (r"市场份额.{0,6}增长", "market_share"),
    (r"市场占有率.{0,6}提升", "market_share"),
    (r"market share.{0,30}grow", "market_share"),
    (r"market share.{0,30}increase", "market_share"),
    (r"market share gain", "market_share"),
    (r"market expansion", "market_share"),
    (r"grow market share", "market_share"),
    (r"降低.{0,8}开发成本", "cost_reduction"),
    (r"减少.{0,8}开发成本", "cost_reduction"),
    (r"节省.{0,8}开发成本", "cost_reduction"),
    (r"reduce.{0,20}cost", "cost_reduction"),
    (r"cost reduction", "cost_reduction"),
    (r"cost saving", "cost_reduction"),
    (r"reduce development cost", "cost_reduction"),
)


def _check_business_commitment(text: str, *, field_path: str) -> None:
    """Reject ``text`` if it carries an unsupported business commitment.

    Called on every claim-shaped PRD field (problem statement,
    proposed solution, metric name/note, acceptance criterion text/note,
    functional requirement text, MVP scope statements). The scan is
    deliberately conservative: any phrasing that matches one of the
    pre-registered patterns raises a hard
    :class:`RequirementValidationError`, which the AI pipeline
    converts to :class:`PRDLLMProtocolError` and the PM pipeline
    surfaces to the API layer as 422.
    """
    if not isinstance(text, str):
        return
    for pattern, code in _BUSINESS_COMMITMENT_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            raise RequirementValidationError(
                f"{field_path} carries unsupported business commitment "
                f"({code}); claims about revenue / retention / market share / "
                "user growth / cost reduction must be sourced from structured input."
            )


def _scan_business_commitments(payload: Mapping[str, Any]) -> None:
    """Walk every claim-shaped PRD field and run the commitment scan.

    Fields like ``risks[].description`` / ``risks[].mitigation`` and
    ``edge_cases[].description`` describe negative scenarios or
    mitigations, not promises, so they are deliberately excluded —
    scanning them would generate false positives on text like
    "若不拦截将导致留存下降".
    """
    for section_name in (
        "background",
        "problem",
        "target_user",
        "user_story",
        "product_goal",
        "user_flow",
    ):
        section = payload.get(section_name)
        if isinstance(section, Mapping):
            text = section.get("text")
            if isinstance(text, str):
                _check_business_commitment(text, field_path=f"{section_name}.text")

    proposed = payload.get("proposed_solution")
    if isinstance(proposed, Mapping):
        text = proposed.get("text")
        if isinstance(text, str):
            _check_business_commitment(text, field_path="proposed_solution.text")

    for index, fr in enumerate(payload.get("functional_requirements") or []):
        if isinstance(fr, Mapping):
            text = fr.get("text")
            if isinstance(text, str):
                _check_business_commitment(
                    text, field_path=f"functional_requirements[{index}].text"
                )

    for index, ac in enumerate(payload.get("acceptance_criteria") or []):
        if isinstance(ac, Mapping):
            text = ac.get("text")
            if isinstance(text, str):
                _check_business_commitment(text, field_path=f"acceptance_criteria[{index}].text")
            note = ac.get("note")
            if isinstance(note, str) and note:
                _check_business_commitment(note, field_path=f"acceptance_criteria[{index}].note")

    for index, m in enumerate(payload.get("success_metrics") or []):
        if isinstance(m, Mapping):
            name = m.get("name")
            if isinstance(name, str):
                _check_business_commitment(name, field_path=f"success_metrics[{index}].name")
            note = m.get("note")
            if isinstance(note, str) and note:
                _check_business_commitment(note, field_path=f"success_metrics[{index}].note")

    mvp = payload.get("mvp_scope")
    if isinstance(mvp, Mapping):
        for key in ("in_scope", "out_of_scope"):
            value = mvp.get(key)
            if isinstance(value, str):
                _check_business_commitment(value, field_path=f"mvp_scope.{key}")


def _parse_requirement_payload(
    payload: Mapping[str, Any],
    *,
    allowed_evidence_ids: set[str],
) -> tuple[
    str,
    str,
    str,
    str,
    str,
    list[SuccessMetricRead],
    list[RiskRead],
    list[EvidenceReferenceRead],
    list[GroundingWarning],
]:
    warnings: list[GroundingWarning] = []
    if not isinstance(payload, Mapping):
        raise RequirementLLMProtocolError("requirement tool input is not an object")

    expected = {
        "name",
        "problem",
        "target_user",
        "user_story",
        "solution_hypothesis",
        "success_metrics",
        "risks",
        "evidence_references",
    }
    extras = set(payload.keys()) - expected
    if extras:
        raise RequirementLLMProtocolError(
            f"requirement tool input contains unknown fields: {sorted(extras)}"
        )
    missing = expected - set(payload.keys())
    if missing:
        raise RequirementLLMProtocolError(
            f"requirement tool input missing required fields: {sorted(missing)}"
        )

    name = _check_string(payload["name"], field_name="name", max_length=_MAX_REQUIREMENT_NAME)
    problem = _check_string(
        payload["problem"], field_name="problem", max_length=_MAX_REQUIREMENT_TEXT
    )
    target_user = _check_string(
        payload["target_user"], field_name="target_user", max_length=_MAX_REQUIREMENT_TEXT
    )
    user_story = _check_string(
        payload["user_story"], field_name="user_story", max_length=_MAX_REQUIREMENT_TEXT
    )
    solution_hypothesis = _check_string(
        payload["solution_hypothesis"],
        field_name="solution_hypothesis",
        max_length=_MAX_REQUIREMENT_TEXT,
    )

    metrics_raw = payload["success_metrics"]
    if not isinstance(metrics_raw, list):
        raise RequirementLLMProtocolError("success_metrics must be a list")
    metrics: list[SuccessMetricRead] = []
    for index, item in enumerate(metrics_raw):
        if not isinstance(item, Mapping):
            raise RequirementLLMProtocolError(f"success_metrics[{index}] must be an object")
        metric_type = item.get("type")
        if metric_type not in ("metric_definition", "baseline_only"):
            raise RequirementLLMProtocolError(
                f"success_metrics[{index}].type must be metric_definition or baseline_only"
            )
        name_m = _check_string(
            item.get("name"),
            field_name=f"success_metrics[{index}].name",
            max_length=_MAX_METRIC_NAME,
        )
        sources_raw = item.get("sources")
        if not isinstance(sources_raw, list):
            raise RequirementLLMProtocolError(f"success_metrics[{index}].sources must be a list")
        sources: list[str] = []
        for s_index, source in enumerate(sources_raw):
            if not isinstance(source, str) or not source.strip():
                raise RequirementLLMProtocolError(
                    f"success_metrics[{index}].sources[{s_index}] must be non-empty string"
                )
            if len(source) > _MAX_EVIDENCE_ID:
                raise RequirementLLMProtocolError(
                    f"success_metrics[{index}].sources[{s_index}] exceeds maxLength"
                )
            sources.append(source)
        note_raw = item.get("note", "")
        if note_raw is None:
            note_raw = ""
        if not isinstance(note_raw, str):
            raise RequirementLLMProtocolError(f"success_metrics[{index}].note must be string")
        if len(note_raw) > _MAX_METRIC_NOTE:
            raise RequirementLLMProtocolError(f"success_metrics[{index}].note exceeds maxLength")
        if metric_type == "baseline_only" and not sources:
            warnings.append(
                GroundingWarning(
                    code="metric_baseline_sources_missing",
                    section=f"success_metrics[{index}]",
                    message="baseline_only metric must list at least one evidence source",
                ).model_dump()
            )
        metrics.append(
            SuccessMetricRead(name=name_m, type=metric_type, sources=sources, note=note_raw)
        )

    risks_raw = payload["risks"]
    if not isinstance(risks_raw, list):
        raise RequirementLLMProtocolError("risks must be a list")
    risks: list[RiskRead] = []
    for index, item in enumerate(risks_raw):
        if not isinstance(item, Mapping):
            raise RequirementLLMProtocolError(f"risks[{index}] must be an object")
        category = item.get("category")
        if category not in ("known", RISK_SPECULATIVE):
            raise RequirementLLMProtocolError(
                f"risks[{index}].category must be known or speculative"
            )
        description = _check_string(
            item.get("description"),
            field_name=f"risks[{index}].description",
            max_length=_MAX_RISK_DESCRIPTION,
        )
        evidence_ids = _check_evidence_ids(
            item.get("evidence_ids", []),
            allowed=allowed_evidence_ids,
            field_name=f"risks[{index}].evidence_ids",
        )
        mitigation_raw = item.get("mitigation", "")
        if mitigation_raw is None:
            mitigation_raw = ""
        if not isinstance(mitigation_raw, str):
            raise RequirementLLMProtocolError(f"risks[{index}].mitigation must be a string")
        if len(mitigation_raw) > _MAX_RISK_DESCRIPTION:
            raise RequirementLLMProtocolError(f"risks[{index}].mitigation exceeds maxLength")
        if category == "known" and not evidence_ids:
            warnings.append(
                GroundingWarning(
                    code="known_risk_evidence_missing",
                    section=f"risks[{index}]",
                    message="known risks must cite at least one evidence id",
                ).model_dump()
            )
        elif category == RISK_SPECULATIVE:
            warnings.append(
                GroundingWarning(
                    code="speculative_risk",
                    section=f"risks[{index}]",
                    message="risk marked as speculative — PM must review before approval",
                ).model_dump()
            )
        risks.append(
            RiskRead(
                category=category,
                description=description,
                evidence_ids=evidence_ids,
                mitigation=mitigation_raw,
            )
        )

    refs_raw = payload["evidence_references"]
    if not isinstance(refs_raw, list) or not refs_raw:
        raise RequirementLLMProtocolError("evidence_references must be a non-empty list")
    refs: list[EvidenceReferenceRead] = []
    for index, item in enumerate(refs_raw):
        if not isinstance(item, Mapping):
            raise RequirementLLMProtocolError(f"evidence_references[{index}] must be an object")
        external_id_raw = item.get("external_id")
        if not isinstance(external_id_raw, str) or not external_id_raw.strip():
            raise RequirementLLMProtocolError(
                f"evidence_references[{index}].external_id must be non-empty"
            )
        external_id = external_id_raw.strip()
        if len(external_id) > _MAX_EVIDENCE_ID:
            raise RequirementLLMProtocolError(
                f"evidence_references[{index}].external_id exceeds maxLength"
            )
        if external_id not in allowed_evidence_ids:
            raise RequirementLLMProtocolError(
                f"evidence_references[{index}].external_id '{external_id}' not in input evidence"
            )
        usage = _check_string(
            item.get("usage"),
            field_name=f"evidence_references[{index}].usage",
            max_length=_MAX_USAGE,
        )
        refs.append(EvidenceReferenceRead(external_id=external_id, usage=usage))
    return (
        name,
        problem,
        target_user,
        user_story,
        solution_hypothesis,
        metrics,
        risks,
        refs,
        warnings,
    )


def _parse_prd_payload(
    payload: Mapping[str, Any],
    *,
    allowed_evidence_ids: set[str],
) -> tuple[dict[str, Any], list[GroundingWarning]]:
    """Validate a 13-section PRD payload.

    Returns ``(content_dict, grounding_warnings)``. Raises
    :class:`PRDLLMProtocolError` for hard schema violations
    (missing / wrong-type / over-length sections, fabricated metrics)
    and appends :class:`GroundingWarning` rows for soft issues
    (speculative risks, unverifiable acceptance criteria, baseline
    metrics with no sources).
    """
    try:
        return _parse_prd_payload_inner(payload, allowed_evidence_ids=allowed_evidence_ids)
    except RequirementValidationError as exc:
        # Shared grounding helpers raise RequirementValidationError;
        # the PRD LLM contract classifies those as a malformed tool
        # payload, so the API layer must surface them as 503.
        raise PRDLLMProtocolError(str(exc)) from exc


def _parse_prd_payload_inner(
    payload: Mapping[str, Any],
    *,
    allowed_evidence_ids: set[str],
) -> tuple[dict[str, Any], list[GroundingWarning]]:
    if not isinstance(payload, Mapping):
        raise PRDLLMProtocolError("PRD tool input is not an object")
    expected_sections = {
        "background",
        "problem",
        "target_user",
        "user_story",
        "product_goal",
        "proposed_solution",
        "functional_requirements",
        "user_flow",
        "edge_cases",
        "acceptance_criteria",
        "success_metrics",
        "risks",
        "mvp_scope",
        "evidence_sources",
        "evidence_ids",
    }
    extras = set(payload.keys()) - expected_sections
    if extras:
        raise PRDLLMProtocolError(f"PRD tool input contains unknown fields: {sorted(extras)}")
    missing = expected_sections - set(payload.keys())
    if missing:
        raise PRDLLMProtocolError(f"PRD tool input missing required fields: {sorted(missing)}")

    # Business-impact promise scan runs before any per-section
    # coercion so a fabricated commitment in ``problem`` or
    # ``proposed_solution`` short-circuits with a clear error code
    # instead of being persisted as a "neutral" field. The scan
    # re-uses ``RequirementValidationError`` — the public wrapper
    # below translates it into ``PRDLLMProtocolError`` for the AI
    # call path.
    _scan_business_commitments(payload)

    warnings: list[GroundingWarning] = []
    content: dict[str, Any] = {}

    def _evidence_section(field_name: str) -> dict[str, Any]:
        section = payload[field_name]
        if not isinstance(section, Mapping):
            raise PRDLLMProtocolError(f"{field_name} must be an object")
        text = _check_string(
            section.get("text"), field_name=f"{field_name}.text", max_length=_MAX_PRD_TEXT
        )
        raw_ids = section.get("evidence_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise PRDLLMProtocolError(f"{field_name}.evidence_ids must be a non-empty list")
        ids = _check_evidence_ids(
            raw_ids,
            allowed=allowed_evidence_ids,
            field_name=f"{field_name}.evidence_ids",
        )
        return {"text": text, "evidence_ids": ids}

    content["background"] = _evidence_section("background")
    content["problem"] = _evidence_section("problem")
    content["target_user"] = _evidence_section("target_user")
    content["user_story"] = _evidence_section("user_story")
    content["product_goal"] = _evidence_section("product_goal")

    proposed = payload["proposed_solution"]
    if not isinstance(proposed, Mapping):
        raise PRDLLMProtocolError("proposed_solution must be an object")
    proposed_text = _check_string(
        proposed.get("text"), field_name="proposed_solution.text", max_length=_MAX_PRD_TEXT
    )
    proposed_is_proposed = proposed.get("is_proposed")
    if not isinstance(proposed_is_proposed, bool):
        raise PRDLLMProtocolError("proposed_solution.is_proposed must be boolean")
    if proposed_is_proposed is False:
        warnings.append(
            GroundingWarning(
                code="proposed_solution_not_proposed",
                section="proposed_solution",
                message="AI marked proposed_solution.is_proposed=false; PM should verify",
            ).model_dump()
        )
    raw_proposed_ids = proposed.get("evidence_ids")
    if not isinstance(raw_proposed_ids, list) or not raw_proposed_ids:
        raise PRDLLMProtocolError("proposed_solution.evidence_ids must be a non-empty list")
    proposed_ids = _check_evidence_ids(
        raw_proposed_ids,
        allowed=allowed_evidence_ids,
        field_name="proposed_solution.evidence_ids",
    )
    content["proposed_solution"] = {
        "text": proposed_text,
        "is_proposed": proposed_is_proposed,
        "evidence_ids": proposed_ids,
    }

    fr_raw = payload["functional_requirements"]
    if not isinstance(fr_raw, list) or not fr_raw:
        raise PRDLLMProtocolError("functional_requirements must be a non-empty list")
    frs: list[dict[str, Any]] = []
    for index, item in enumerate(fr_raw):
        if not isinstance(item, Mapping):
            raise PRDLLMProtocolError(f"functional_requirements[{index}] must be an object")
        text_v = _check_string(
            item.get("text"),
            field_name=f"functional_requirements[{index}].text",
            max_length=_MAX_FR_TEXT,
        )
        ids = _check_evidence_ids(
            item.get("evidence_ids", []),
            allowed=allowed_evidence_ids,
            field_name=f"functional_requirements[{index}].evidence_ids",
        )
        frs.append({"text": text_v, "evidence_ids": ids})
    content["functional_requirements"] = frs

    content["user_flow"] = _evidence_section("user_flow")

    edge_raw = payload["edge_cases"]
    if not isinstance(edge_raw, list) or not edge_raw:
        raise PRDLLMProtocolError("edge_cases must be a non-empty list")
    allowed_categories = {
        "empty_state",
        "failure",
        "retry",
        "partial_success",
        "invalid_input",
        "limit_exceeded",
    }
    edges: list[dict[str, Any]] = []
    seen_categories: set[str] = set()
    for index, item in enumerate(edge_raw):
        if not isinstance(item, Mapping):
            raise PRDLLMProtocolError(f"edge_cases[{index}] must be an object")
        category = item.get("category")
        if category not in allowed_categories:
            raise PRDLLMProtocolError(
                f"edge_cases[{index}].category must be one of {sorted(allowed_categories)}"
            )
        description = _check_string(
            item.get("description"),
            field_name=f"edge_cases[{index}].description",
            max_length=_MAX_FR_TEXT,
        )
        ids = _check_evidence_ids(
            item.get("evidence_ids", []),
            allowed=allowed_evidence_ids,
            field_name=f"edge_cases[{index}].evidence_ids",
        )
        edges.append({"category": category, "description": description, "evidence_ids": ids})
        seen_categories.add(category)
    # Strict coverage: every PRD must enumerate all six pre-defined
    # categories. Skipping a class means the AI (or PM hand-edit)
    # silently dropped a known failure mode. Reject with a dedicated
    # message so the diagnostic names which categories are missing.
    missing_categories = allowed_categories - seen_categories
    if missing_categories:
        raise PRDLLMProtocolError(
            "edge_cases must cover all six categories "
            f"{sorted(allowed_categories)}; missing {sorted(missing_categories)}"
        )
    content["edge_cases"] = edges

    ac_raw = payload["acceptance_criteria"]
    if not isinstance(ac_raw, list) or not ac_raw:
        raise PRDLLMProtocolError("acceptance_criteria must be a non-empty list")
    acs: list[dict[str, Any]] = []
    for index, item in enumerate(ac_raw):
        if not isinstance(item, Mapping):
            raise PRDLLMProtocolError(f"acceptance_criteria[{index}] must be an object")
        text_v = _check_string(
            item.get("text"),
            field_name=f"acceptance_criteria[{index}].text",
            max_length=_MAX_FR_TEXT,
        )
        verifiable = item.get("verifiable")
        if not isinstance(verifiable, bool):
            raise PRDLLMProtocolError(f"acceptance_criteria[{index}].verifiable must be boolean")
        # Strict: unverifiable criteria are no longer accepted as an
        # AI draft. A "verifiable=false" criterion cannot be QA-ed and
        # cannot gate ship, so the conservative default is to fail the
        # parse. PM hand-edits go through the same gate — they cannot
        # smuggle in unverifiable criteria via PATCH either.
        if verifiable is False:
            raise PRDLLMProtocolError(
                f"acceptance_criteria[{index}].verifiable must be true; "
                "unverifiable acceptance criteria are rejected at parse time."
            )
        note_raw = item.get("note", "")
        if note_raw is None:
            note_raw = ""
        if not isinstance(note_raw, str):
            raise PRDLLMProtocolError(f"acceptance_criteria[{index}].note must be string")
        if len(note_raw) > _MAX_AC_NOTE:
            raise PRDLLMProtocolError(f"acceptance_criteria[{index}].note exceeds maxLength")
        ids = _check_evidence_ids(
            item.get("evidence_ids", []),
            allowed=allowed_evidence_ids,
            field_name=f"acceptance_criteria[{index}].evidence_ids",
        )
        acs.append(
            {
                "text": text_v,
                "verifiable": verifiable,
                "note": note_raw,
                "evidence_ids": ids,
            }
        )
    content["acceptance_criteria"] = acs

    sm_raw = payload["success_metrics"]
    if not isinstance(sm_raw, list):
        raise PRDLLMProtocolError("success_metrics must be a list")
    metrics: list[dict[str, Any]] = []
    for index, item in enumerate(sm_raw):
        if not isinstance(item, Mapping):
            raise PRDLLMProtocolError(f"success_metrics[{index}] must be an object")
        metric_type = item.get("type")
        if metric_type not in ("metric_definition", "baseline_only"):
            raise PRDLLMProtocolError(
                f"success_metrics[{index}].type must be metric_definition or baseline_only"
            )
        name_v = _check_string(
            item.get("name"),
            field_name=f"success_metrics[{index}].name",
            max_length=_MAX_METRIC_NAME,
        )
        note_raw = item.get("note", "")
        if note_raw is None:
            note_raw = ""
        if not isinstance(note_raw, str):
            raise PRDLLMProtocolError(f"success_metrics[{index}].note must be string")
        if len(note_raw) > _MAX_METRIC_NOTE:
            raise PRDLLMProtocolError(f"success_metrics[{index}].note exceeds maxLength")
        sources_raw = item.get("sources", [])
        if not isinstance(sources_raw, list):
            raise PRDLLMProtocolError(f"success_metrics[{index}].sources must be a list")
        sources: list[str] = []
        for s_index, source in enumerate(sources_raw):
            if not isinstance(source, str) or not source.strip():
                raise PRDLLMProtocolError(
                    f"success_metrics[{index}].sources[{s_index}] must be non-empty string"
                )
            sources.append(source)
        if metric_type == "baseline_only" and not sources:
            warnings.append(
                GroundingWarning(
                    code="metric_baseline_sources_missing",
                    section=f"success_metrics[{index}]",
                    message="baseline_only metric must list at least one evidence source",
                ).model_dump()
            )
        metrics.append({"name": name_v, "type": metric_type, "note": note_raw, "sources": sources})
    content["success_metrics"] = metrics

    risks_raw = payload["risks"]
    if not isinstance(risks_raw, list):
        raise PRDLLMProtocolError("risks must be a list")
    risks: list[dict[str, Any]] = []
    for index, item in enumerate(risks_raw):
        if not isinstance(item, Mapping):
            raise PRDLLMProtocolError(f"risks[{index}] must be an object")
        category = item.get("category")
        if category not in ("known", RISK_SPECULATIVE):
            raise PRDLLMProtocolError(f"risks[{index}].category must be known or speculative")
        description = _check_string(
            item.get("description"),
            field_name=f"risks[{index}].description",
            max_length=_MAX_RISK_DESCRIPTION,
        )
        ids = _check_evidence_ids(
            item.get("evidence_ids", []),
            allowed=allowed_evidence_ids,
            field_name=f"risks[{index}].evidence_ids",
        )
        mitigation_raw = item.get("mitigation", "")
        if mitigation_raw is None:
            mitigation_raw = ""
        if not isinstance(mitigation_raw, str):
            raise PRDLLMProtocolError(f"risks[{index}].mitigation must be a string")
        if len(mitigation_raw) > _MAX_RISK_DESCRIPTION:
            raise PRDLLMProtocolError(f"risks[{index}].mitigation exceeds maxLength")
        if category == "known" and not ids:
            warnings.append(
                GroundingWarning(
                    code="known_risk_evidence_missing",
                    section=f"risks[{index}]",
                    message="known risks must cite at least one evidence id",
                ).model_dump()
            )
        elif category == RISK_SPECULATIVE:
            warnings.append(
                GroundingWarning(
                    code="speculative_risk",
                    section=f"risks[{index}]",
                    message="risk marked as speculative — PM must review before approval",
                ).model_dump()
            )
        risks.append(
            {
                "category": category,
                "description": description,
                "evidence_ids": ids,
                "mitigation": mitigation_raw,
            }
        )
    content["risks"] = risks

    mvp_raw = payload["mvp_scope"]
    if not isinstance(mvp_raw, Mapping):
        raise PRDLLMProtocolError("mvp_scope must be an object")
    in_scope = _check_string(
        mvp_raw.get("in_scope"), field_name="mvp_scope.in_scope", max_length=_MAX_PRD_TEXT
    )
    out_of_scope = _check_string(
        mvp_raw.get("out_of_scope"),
        field_name="mvp_scope.out_of_scope",
        max_length=_MAX_PRD_TEXT,
    )
    content["mvp_scope"] = {"in_scope": in_scope, "out_of_scope": out_of_scope}

    evidence_sources_raw = payload["evidence_sources"]
    if not isinstance(evidence_sources_raw, list) or not evidence_sources_raw:
        raise PRDLLMProtocolError("evidence_sources must be a non-empty list")
    evidence_sources: list[str] = []
    for index, item in enumerate(evidence_sources_raw):
        if not isinstance(item, str) or not item.strip():
            raise PRDLLMProtocolError(f"evidence_sources[{index}] must be non-empty string")
        if len(item) > 500:
            raise PRDLLMProtocolError(f"evidence_sources[{index}] exceeds maxLength")
        evidence_sources.append(item)
    content["evidence_sources"] = evidence_sources

    final_evidence_ids = _check_evidence_ids(
        payload["evidence_ids"],
        allowed=allowed_evidence_ids,
        field_name="evidence_ids",
    )
    content["evidence_ids"] = final_evidence_ids
    return content, warnings


def _content_hash(content: Mapping[str, Any]) -> str:
    """Stable hash of a PRD content payload for audit."""
    canonical = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---- AI call wrappers -------------------------------------------------------


def _call_requirement(
    *,
    payload: Mapping[str, Any],
    allowed_evidence_ids: set[str],
) -> tuple[
    str,
    str,
    str,
    str,
    str,
    list[SuccessMetricRead],
    list[RiskRead],
    list[EvidenceReferenceRead],
    list[GroundingWarning],
    int,
    int,
    int,
    int,
    int,
    int,
]:
    """Invoke the Requirement LLM client with bounded transport retry.

    Returns ``(name, problem, target_user, user_story, solution_hypothesis,
    success_metrics, risks, evidence_references, grounding_warnings,
    input_tokens, output_tokens, unknown_token_calls, latency_ms,
    attempt_count, retry_count)``.
    """
    if not (settings.llm_api_key and settings.llm_base_url and settings.llm_model):
        raise RequirementConfigError(
            "LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 未配置；无法生成 Requirement。"
        )
    max_attempts = max(1, int(settings.requirement_max_retries) + 1)
    attempt_count = 0
    started = time.monotonic()
    last_transport_error: Exception | None = None
    while attempt_count < max_attempts:
        attempt_count += 1
        try:
            with RequirementClient(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                timeout_seconds=settings.llm_timeout_seconds,
            ) as client:
                result: RequirementLLMResult = client.generate_requirement(dict(payload))
        except RequirementLLMTransportError as exc:
            last_transport_error = exc
            continue
        latency_ms = int((time.monotonic() - started) * 1000)
        retry_count = max(0, attempt_count - 1)
        parsed = _parse_requirement_payload(
            result.payload, allowed_evidence_ids=allowed_evidence_ids
        )
        unknown = (
            1 if result.usage.input_tokens is None or result.usage.output_tokens is None else 0
        )
        return (
            parsed[0],
            parsed[1],
            parsed[2],
            parsed[3],
            parsed[4],
            parsed[5],
            parsed[6],
            parsed[7],
            parsed[8],
            result.usage.input_tokens or 0,
            result.usage.output_tokens or 0,
            unknown,
            latency_ms,
            attempt_count,
            retry_count,
        )
    if last_transport_error is not None:
        raise last_transport_error
    raise RequirementLLMTransportError("requirement LLM transport retry exhausted")


def _call_prd(
    *,
    payload: Mapping[str, Any],
    allowed_evidence_ids: set[str],
) -> tuple[dict[str, Any], list[GroundingWarning], int, int, int, int, int, int]:
    """Invoke the PRD LLM client with bounded transport retry."""
    if not (settings.llm_api_key and settings.llm_base_url and settings.llm_model):
        raise RequirementConfigError(
            "LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 未配置；无法生成 PRD。"
        )
    max_attempts = max(1, int(settings.prd_max_retries) + 1)
    attempt_count = 0
    started = time.monotonic()
    last_transport_error: Exception | None = None
    while attempt_count < max_attempts:
        attempt_count += 1
        try:
            with PRDClient(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                timeout_seconds=settings.llm_timeout_seconds,
            ) as client:
                result: PRDLLMResult = client.generate_prd(dict(payload))
        except PRDLLMTransportError as exc:
            last_transport_error = exc
            continue
        latency_ms = int((time.monotonic() - started) * 1000)
        retry_count = max(0, attempt_count - 1)
        content, warnings = _parse_prd_payload(
            result.payload, allowed_evidence_ids=allowed_evidence_ids
        )
        unknown = (
            1 if result.usage.input_tokens is None or result.usage.output_tokens is None else 0
        )
        return (
            content,
            warnings,
            result.usage.input_tokens or 0,
            result.usage.output_tokens or 0,
            unknown,
            latency_ms,
            attempt_count,
            retry_count,
        )
    if last_transport_error is not None:
        raise last_transport_error
    raise PRDLLMTransportError("PRD LLM transport retry exhausted")


# ---- PM-authored content validators -----------------------------------------


def _validate_pm_requirement_content(
    payload: PMRequirementContent,
    *,
    allowed_evidence_ids: set[str],
    allowed_external_ids: set[str] | None = None,
) -> tuple[
    str,
    str,
    str,
    str,
    str,
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Validate PM-authored Requirement content. Returns serialisable dicts."""
    refs: list[dict[str, Any]] = []
    references_external_ids: set[str] = set()
    for index, ref in enumerate(payload.evidence_references):
        external = ref.external_id.strip()
        if allowed_external_ids is not None and external in allowed_external_ids:
            pass
        elif external not in allowed_evidence_ids:
            raise RequirementValidationError(
                f"evidence_references[{index}].external_id '{external}' not in input evidence"
            )
        usage = ref.usage.strip()
        refs.append({"external_id": external, "usage": usage})
        references_external_ids.add(external)

    metrics: list[dict[str, Any]] = []
    for index, metric in enumerate(payload.success_metrics):
        sources: list[str] = []
        for s in metric.sources:
            cleaned = s.strip()
            if cleaned not in allowed_evidence_ids:
                raise RequirementValidationError(
                    f"success_metrics[{index}].sources includes unknown evidence '{cleaned}'"
                )
            sources.append(cleaned)
        metrics.append(
            {
                "name": metric.name.strip(),
                "type": metric.type,
                "sources": sources,
                "note": metric.note.strip(),
            }
        )

    risks: list[dict[str, Any]] = []
    for index, risk in enumerate(payload.risks):
        ids: list[str] = []
        for rid in risk.evidence_ids:
            cleaned = rid.strip()
            if cleaned not in allowed_evidence_ids:
                raise RequirementValidationError(
                    f"risks[{index}].evidence_ids includes unknown evidence '{cleaned}'"
                )
            ids.append(cleaned)
        if risk.category == "known" and not ids:
            raise RequirementValidationError(
                f"risks[{index}] (category=known) must cite at least one evidence id"
            )
        risks.append(
            {
                "category": risk.category,
                "description": risk.description.strip(),
                "evidence_ids": ids,
                "mitigation": risk.mitigation.strip(),
            }
        )

    return (
        payload.name.strip(),
        payload.problem.strip(),
        payload.target_user.strip(),
        payload.user_story.strip(),
        payload.solution_hypothesis.strip(),
        metrics,
        risks,
        refs,
    )


def _validate_pm_prd_content(content: PRDContent) -> tuple[dict[str, Any], list[GroundingWarning]]:
    """Validate a PM-authored PRDContent payload.

    Returns ``(serialised_content_dict, grounding_warnings)``. The
    strict AI parser is reused with ``allowed_evidence_ids`` populated
    from the requirement's evidence references; warnings re-use the
    same codes so PM cannot smuggle in unverifiable acceptance
    criteria without explicit acknowledgement in the audit trail.
    """
    payload: dict[str, Any] = {
        "background": {
            "text": content.background.text,
            "evidence_ids": content.background.evidence_ids,
        },
        "problem": {
            "text": content.problem.text,
            "evidence_ids": content.problem.evidence_ids,
        },
        "target_user": {
            "text": content.target_user.text,
            "evidence_ids": content.target_user.evidence_ids,
        },
        "user_story": {
            "text": content.user_story.text,
            "evidence_ids": content.user_story.evidence_ids,
        },
        "product_goal": {
            "text": content.product_goal.text,
            "evidence_ids": content.product_goal.evidence_ids,
        },
        "proposed_solution": {
            "text": content.proposed_solution.text,
            "is_proposed": content.proposed_solution.is_proposed,
            "evidence_ids": content.proposed_solution.evidence_ids,
        },
        "functional_requirements": [
            {"text": fr.text, "evidence_ids": fr.evidence_ids}
            for fr in content.functional_requirements
        ],
        "user_flow": {
            "text": content.user_flow.text,
            "evidence_ids": content.user_flow.evidence_ids,
        },
        "edge_cases": [
            {
                "category": ec.category,
                "description": ec.description,
                "evidence_ids": ec.evidence_ids,
            }
            for ec in content.edge_cases
        ],
        "acceptance_criteria": [
            {
                "text": ac.text,
                "verifiable": ac.verifiable,
                "note": ac.note,
                "evidence_ids": ac.evidence_ids,
            }
            for ac in content.acceptance_criteria
        ],
        "success_metrics": [
            {"name": m.name, "type": m.type, "note": m.note, "sources": m.sources}
            for m in content.success_metrics
        ],
        "risks": [
            {
                "category": r.category,
                "description": r.description,
                "evidence_ids": r.evidence_ids,
                "mitigation": r.mitigation,
            }
            for r in content.risks
        ],
        "mvp_scope": {
            "in_scope": content.mvp_scope.in_scope,
            "out_of_scope": content.mvp_scope.out_of_scope,
        },
        "evidence_sources": content.evidence_sources,
        "evidence_ids": content.evidence_ids,
    }
    return payload, []  # parsing is delegated to the AI parser caller.


# ---- Content → JSON serialisation helpers -----------------------------------


def _serialise_metrics(metrics: Sequence[SuccessMetricRead]) -> list[dict[str, Any]]:
    return [{"name": m.name, "type": m.type, "sources": m.sources, "note": m.note} for m in metrics]


def _serialise_risks(risks: Sequence[RiskRead]) -> list[dict[str, Any]]:
    return [
        {
            "category": r.category,
            "description": r.description,
            "evidence_ids": r.evidence_ids,
            "mitigation": r.mitigation,
        }
        for r in risks
    ]


def _serialise_evidence_refs(refs: Sequence[EvidenceReferenceRead]) -> list[dict[str, Any]]:
    return [{"external_id": r.external_id, "usage": r.usage} for r in refs]


# ---- Public orchestration ---------------------------------------------------


@dataclass
class RequirementOutcome:
    requirement: Requirement
    is_ai_generated: bool
    grounding_warnings: list[GroundingWarning] = field(default_factory=list)


@dataclass
class PRDOutcome:
    prd: PRD
    is_ai_generated: bool
    grounding_warnings: list[GroundingWarning] = field(default_factory=list)


def create_requirement(
    db: Session,
    insight_id: UUID,
    *,
    pm_content: PMRequirementContent | None = None,
) -> RequirementOutcome:
    """Create one Requirement row for the Insight.

    Flush-only — caller owns the transaction. When ``pm_content`` is
    supplied the AI client is bypassed (PM-authored) and the row is
    marked ``status=draft``. Otherwise the AI client is invoked under
    a fresh bundle load so the PM Priority snapshot is current.
    """
    snapshot = load_phase4_snapshot(db, insight_id)
    insight = db.get(Insight, insight_id)
    if insight is None:
        raise RequirementNotFoundError("insight not found")

    existing = db.scalar(select(Requirement).where(Requirement.insight_id == insight_id))
    if existing is not None:
        raise RequirementValidationError("duplicate Requirement for insight")

    allowed_evidence_ids = set(snapshot.evidence_external_ids)
    if pm_content is not None:
        _validate_pm_requirement_content(pm_content, allowed_evidence_ids=allowed_evidence_ids)
        name, problem, target_user, user_story, solution_hypothesis, metrics, risks, refs = (
            _coerce_pm_requirement_fields(pm_content)
        )
        warnings: list[GroundingWarning] = []
        input_tokens = 0
        output_tokens = 0
        unknown = 0
        latency_ms = 0
        attempt_count = 0
        retry_count = 0
        model: str | None = None
        prompt_version = settings.requirement_prompt_version
    else:
        bundle = _load_source_bundle(db, insight)
        decision_row = db.get(InsightDecision, snapshot.decision_id)
        if decision_row is None:
            raise RequirementValidationError("decision not found")
        input_payload = _build_requirement_input(
            insight=insight,
            decision=decision_row,
            snapshot=snapshot,
            bundle=bundle,
        )
        try:
            (
                name,
                problem,
                target_user,
                user_story,
                solution_hypothesis,
                metrics_read,
                risks_read,
                refs_read,
                warnings_raw,
                input_tokens,
                output_tokens,
                unknown,
                latency_ms,
                attempt_count,
                retry_count,
            ) = _call_requirement(
                payload=input_payload,
                allowed_evidence_ids=allowed_evidence_ids,
            )
        except RequirementConfigError:
            db.rollback()
            raise
        except RequirementLLMConfigError:
            db.rollback()
            raise RequirementConfigError("requirement LLM configuration missing")
        except RequirementLLMProtocolError:
            db.rollback()
            raise
        except RequirementLLMTransportError:
            db.rollback()
            raise

        warnings = [
            GroundingWarning(
                code=w["code"],
                section=w.get("section"),
                message=w["message"],
                evidence_id=w.get("evidence_id"),
            )
            for w in warnings_raw
        ]
        metrics = [
            SuccessMetricRead(name=m.name, type=m.type, sources=m.sources, note=m.note)
            for m in metrics_read
        ]
        risks = [
            RiskRead(
                category=r.category,
                description=r.description,
                evidence_ids=r.evidence_ids,
                mitigation=r.mitigation,
            )
            for r in risks_read
        ]
        refs = [EvidenceReferenceRead(external_id=r.external_id, usage=r.usage) for r in refs_read]
        model = settings.llm_model
        prompt_version = settings.requirement_prompt_version

    requirement = Requirement(
        project_id=snapshot.project_id,
        insight_id=snapshot.insight_id,
        decision_id=snapshot.decision_id,
        score_id=snapshot.score_id,
        insight_status_snapshot=snapshot.insight_status,
        decision_status_snapshot=snapshot.decision_status,
        ai_priority_snapshot=snapshot.ai_priority,
        pm_priority=snapshot.pm_priority or "P2",
        pm_override_reason_snapshot=snapshot.pm_override_reason,
        source_snapshot_hash=snapshot.source_snapshot_hash,
        name=name,
        problem=problem,
        target_user=target_user,
        user_story=user_story,
        solution_hypothesis=solution_hypothesis,
        success_metrics=_serialise_metrics(metrics),
        risks=_serialise_risks(risks),
        evidence_references=_serialise_evidence_refs(refs),
        status=REQUIREMENT_DRAFT,
        model=model,
        prompt_version=prompt_version,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        unknown_token_calls=unknown,
        latency_ms=latency_ms,
        attempt_count=attempt_count,
        retry_count=retry_count,
        next_version=1,
    )
    db.add(requirement)
    db.flush()
    return RequirementOutcome(
        requirement=requirement,
        is_ai_generated=pm_content is None,
        grounding_warnings=warnings,
    )


def _coerce_pm_requirement_fields(
    payload: PMRequirementContent,
) -> tuple[
    str,
    str,
    str,
    str,
    str,
    list[SuccessMetricRead],
    list[RiskRead],
    list[EvidenceReferenceRead],
]:
    metrics = [
        SuccessMetricRead(
            name=m.name.strip(),
            type=m.type,
            sources=[s.strip() for s in m.sources],
            note=m.note.strip(),
        )
        for m in payload.success_metrics
    ]
    risks = [
        RiskRead(
            category=r.category,
            description=r.description.strip(),
            evidence_ids=[rid.strip() for rid in r.evidence_ids],
            mitigation=r.mitigation.strip(),
        )
        for r in payload.risks
    ]
    refs = [
        EvidenceReferenceRead(
            external_id=r.external_id.strip(),
            usage=r.usage.strip(),
        )
        for r in payload.evidence_references
    ]
    return (
        payload.name.strip(),
        payload.problem.strip(),
        payload.target_user.strip(),
        payload.user_story.strip(),
        payload.solution_hypothesis.strip(),
        metrics,
        risks,
        refs,
    )


def patch_requirement(
    db: Session,
    requirement_id: UUID,
    *,
    status: RequirementStatusLiteral | None = None,
    pm_content: PMRequirementContent | None = None,
) -> RequirementOutcome:
    """Apply a PATCH to a Requirement row.

    Status transitions follow the deterministic table defined in
    :data:`_REQUIREMENT_TRANSITIONS`; content edits replace all
    editable fields in one shot (the AI path is the only way to
    regenerate a draft).
    """
    row = db.get(Requirement, requirement_id)
    if row is None:
        raise RequirementNotFoundError("requirement not found")
    if row.status == REQUIREMENT_ARCHIVED:
        raise RequirementValidationError("requirement archived")
    if status is not None and status != row.status:
        _validate_requirement_status_transition(row.status, status)
        row.status = status
        row.updated_at = _now()
        db.flush()

    if pm_content is not None:
        snapshot = load_phase4_snapshot(db, row.insight_id)
        allowed_evidence_ids = set(snapshot.evidence_external_ids)
        _validate_pm_requirement_content(pm_content, allowed_evidence_ids=allowed_evidence_ids)
        name, problem, target_user, user_story, solution_hypothesis, metrics, risks, refs = (
            _coerce_pm_requirement_fields(pm_content)
        )
        row.name = name
        row.problem = problem
        row.target_user = target_user
        row.user_story = user_story
        row.solution_hypothesis = solution_hypothesis
        row.success_metrics = _serialise_metrics(metrics)
        row.risks = _serialise_risks(risks)
        row.evidence_references = _serialise_evidence_refs(refs)
        row.updated_at = _now()
        db.flush()
    return RequirementOutcome(requirement=row, is_ai_generated=False)


def list_requirements(
    db: Session,
    project_id: UUID,
    *,
    insight_id: UUID | None = None,
    status: RequirementStatusLiteral | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[int, list[Requirement]]:
    """Paginated listing scoped to a project, optionally narrowed by
    ``insight_id`` and / or ``status`` (draft / reviewing / approved /
    rejected / archived). The (status, project) pair is the basic
    slice UI: "show me everything still being shaped" vs
    "already-archived backlog"."""
    where_clauses = [Requirement.project_id == project_id]
    if insight_id is not None:
        where_clauses.append(Requirement.insight_id == insight_id)
    if status is not None:
        where_clauses.append(Requirement.status == status)
    from sqlalchemy import func

    total = db.scalar(select(func.count()).select_from(Requirement).where(*where_clauses))
    rows = db.scalars(
        select(Requirement)
        .where(*where_clauses)
        .order_by(Requirement.created_at.desc(), Requirement.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return total or 0, rows


# ---- PRD orchestration ------------------------------------------------------


def create_prd_version(
    db: Session,
    requirement_id: UUID,
    *,
    pm_content: PRDContent | None = None,
) -> PRDOutcome:
    """Append a new PRD version to a Requirement.

    The version is taken from ``requirements.next_version`` via an
    atomic UPDATE-and-RETURNING on both PostgreSQL and SQLite, so
    concurrent requests cannot collide on the unique
    ``(requirement_id, version)`` constraint.
    """
    requirement = db.get(Requirement, requirement_id)
    if requirement is None:
        raise RequirementNotFoundError("requirement not found")
    if requirement.status == REQUIREMENT_ARCHIVED:
        raise RequirementValidationError("requirement archived")

    insight = db.get(Insight, requirement.insight_id)
    if insight is None:
        raise RequirementNotFoundError("insight not found for requirement")

    snapshot = load_phase4_snapshot(db, requirement.insight_id)
    allowed_evidence_ids = set(snapshot.evidence_external_ids)

    if pm_content is not None:
        serialised_payload, _ = _validate_pm_prd_content(pm_content)
        # Re-run the AI parser with the allowed ids so PM-authored
        # drafts share the same grounding checks; the AI parser is
        # strict schema-wise but tolerates empty lists per the prompt.
        try:
            content_dict, warnings_raw = _parse_prd_payload(
                serialised_payload, allowed_evidence_ids=allowed_evidence_ids
            )
        except (RequirementLLMProtocolError, PRDLLMProtocolError) as exc:
            raise RequirementValidationError(str(exc)) from exc
        input_tokens = 0
        output_tokens = 0
        unknown = 0
        latency_ms = 0
        attempt_count = 0
        retry_count = 0
        model: str | None = None
        prompt_version = settings.prd_prompt_version
        warnings = warnings_raw
    else:
        bundle = _load_source_bundle(db, insight)
        input_payload = _build_prd_input(
            requirement=requirement,
            insight=insight,
            snapshot=snapshot,
            bundle=bundle,
        )
        try:
            (
                content_dict,
                warnings_raw,
                input_tokens,
                output_tokens,
                unknown,
                latency_ms,
                attempt_count,
                retry_count,
            ) = _call_prd(
                payload=input_payload,
                allowed_evidence_ids=allowed_evidence_ids,
            )
        except RequirementConfigError:
            db.rollback()
            raise
        except PRDLLMConfigError:
            db.rollback()
            raise RequirementConfigError("PRD LLM configuration missing")
        except PRDLLMProtocolError:
            db.rollback()
            raise
        except PRDLLMTransportError:
            db.rollback()
            raise
        warnings = [
            GroundingWarning(
                code=w["code"],
                section=w.get("section"),
                message=w["message"],
                evidence_id=w.get("evidence_id"),
            )
            for w in warnings_raw
        ]
        model = settings.llm_model
        prompt_version = settings.prd_prompt_version

    # Atomic version increment: SELECT-FOR-UPDATE on SQLite returns
    # immediately, but the UPDATE inside the transaction guarantees the
    # counter is monotonic. The unique (requirement_id, version)
    # constraint catches the only race we cannot natively serialise.
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        # PostgreSQL ``RETURNING next_version`` yields the new (already
        # incremented) value; the version we are inserting is the
        # *old* counter, so subtract one. Both branches must agree with
        # the migration-defined ``requirements.next_version`` default
        # of 1 — first write lands on version 1.
        new_next = db.execute(
            text(
                "UPDATE requirements SET next_version = next_version + 1, "
                "updated_at = now() WHERE id = :req RETURNING next_version"
            ),
            {"req": str(requirement.id)},
        ).scalar()
        if new_next is None:
            raise RequirementValidationError("requirement_not_found")
        version = int(new_next) - 1
        if version < 1:
            raise RequirementValidationError("version_below_one")
    else:
        # SQLite (and other backends) — read-modify-write inside the
        # transaction. UNIQUE(requirement_id, version) catches any
        # race the engine-internal lock does not catch.
        current = db.scalar(
            select(Requirement.next_version).where(Requirement.id == requirement.id)
        )
        if current is None:
            raise RequirementValidationError("requirement_not_found")
        version = int(current)
        version = int(current)
        requirement.next_version = version + 1
        requirement.updated_at = _now()
        db.flush()

    content_hash = _content_hash(content_dict)
    prd = PRD(
        requirement_id=requirement.id,
        project_id=requirement.project_id,
        insight_id=requirement.insight_id,
        version=version,
        status=PRD_DRAFT,
        content=content_dict,
        content_hash=content_hash,
        is_ai_generated_draft=pm_content is None,
        grounding_warnings=[w.model_dump() for w in warnings],
        model=model,
        prompt_version=prompt_version,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        unknown_token_calls=unknown,
        latency_ms=latency_ms,
        attempt_count=attempt_count,
        retry_count=retry_count,
    )
    db.add(prd)
    db.flush()
    return PRDOutcome(
        prd=prd,
        is_ai_generated=pm_content is None,
        grounding_warnings=warnings,
    )


def patch_prd(
    db: Session,
    prd_id: UUID,
    *,
    status: PRDStatusLiteral | None = None,
    pm_content: PRDContent | None = None,
) -> PRD:
    """Apply a PATCH to a PRD row.

    Two edit dimensions are supported; either, both, or neither may be
    supplied in a single call:

    * ``status`` — transition according to :data:`_PRD_TRANSITIONS`.
    * ``pm_content`` — replace the 13-section content. Only allowed on
      a still-active PRD (``status`` in ``{draft, reviewing}``);
      accepted or archived rows are immutable. The persisted payload
      goes through the same grounding checks as AI- or PM-authored
      drafts: ``evidence_ids`` must reference allowed evidence,
      fabricated metric types are rejected, content hash and
      ``updated_at`` are refreshed.
    """
    prd = db.get(PRD, prd_id)
    if prd is None:
        raise RequirementNotFoundError("prd not found")
    if prd.status == PRD_ARCHIVED:
        raise RequirementValidationError("prd archived")
    if status is not None and status != prd.status:
        _validate_prd_status_transition(prd.status, status)
        prd.status = status
        prd.updated_at = _now()
        db.flush()
    if pm_content is not None:
        if prd.status not in (PRD_DRAFT, PRD_REVIEWING):
            # Accepted / archived versions are immutable; the PM must
            # create a new PRD version via POST. ``content_immutable``
            # is the dedicated marker so the API layer can map this to
            # 409 instead of the catch-all 422.
            raise RequirementValidationError(
                "prd content immutable once accepted; create a new version via POST"
            )
        requirement = db.get(Requirement, prd.requirement_id)
        if requirement is None:
            raise RequirementNotFoundError("requirement not found")
        snapshot = load_phase4_snapshot(db, requirement.insight_id)
        allowed_evidence_ids = set(snapshot.evidence_external_ids)
        serialised_payload, _ = _validate_pm_prd_content(pm_content)
        try:
            content_dict, _warnings = _parse_prd_payload(
                serialised_payload, allowed_evidence_ids=allowed_evidence_ids
            )
        except (RequirementLLMProtocolError, PRDLLMProtocolError) as exc:
            raise RequirementValidationError(str(exc)) from exc
        prd.content = content_dict
        prd.content_hash = _content_hash(content_dict)
        prd.grounding_warnings = [
            w.model_dump() if hasattr(w, "model_dump") else w for w in _warnings
        ]
        prd.is_ai_generated_draft = False
        prd.model = None
        prd.updated_at = _now()
        db.flush()
    return prd


def accept_prd(db: Session, prd_id: UUID) -> PRD:
    """Mark a PRD as accepted.

    Idempotent: a second ``accept`` on an already-accepted row returns
    the same row without re-stamping ``accepted_at``. Any other status
    transition is rejected.

    Phase 5 owns no user system, so the audit row's ``accepted_by``
    column is unconditionally ``NULL`` here. The API layer is
    responsible for rejecting any client-supplied identity so the
    caller cannot forge an attribution through this endpoint.
    """
    prd = db.get(PRD, prd_id)
    if prd is None:
        raise RequirementNotFoundError("prd not found")
    if prd.status == PRD_ARCHIVED:
        raise RequirementValidationError("prd archived")
    if prd.status == PRD_ACCEPTED:
        return prd  # idempotent
    if prd.status not in (PRD_DRAFT, PRD_REVIEWING):
        raise RequirementValidationError(
            f"status transition {prd.status} -> {PRD_ACCEPTED} not allowed"
        )
    prd.status = PRD_ACCEPTED
    now = _now()
    prd.accepted_at = now
    prd.accepted_by = None
    prd.updated_at = now
    db.flush()
    return prd


def list_prds(
    db: Session,
    requirement_id: UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[int, list[PRD]]:
    from sqlalchemy import func

    where_clauses = [PRD.requirement_id == requirement_id]
    total = db.scalar(select(func.count()).select_from(PRD).where(*where_clauses))
    rows = db.scalars(
        select(PRD)
        .where(*where_clauses)
        .order_by(PRD.version.desc(), PRD.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return total or 0, rows


def get_active_prd(db: Session, requirement_id: UUID) -> PRD | None:
    """Return the PRD with the highest ``version`` for a Requirement.

    Independent of :func:`list_prds` so the active pointer stays
    accurate regardless of pagination: a request with
    ``offset=N>0`` still surfaces the same active row as
    ``offset=0``. Returns ``None`` when the requirement has not yet
    generated a PRD. Implemented as a single ``ORDER BY version DESC
    LIMIT 1`` query — the ``ix_prds_requirement_id`` index narrows
    the scan to one requirement's PRD set.
    """
    return db.scalar(
        select(PRD)
        .where(PRD.requirement_id == requirement_id)
        .order_by(PRD.version.desc(), PRD.id.desc())
        .limit(1)
    )


# ---- Shared helpers ---------------------------------------------------------


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def get_requirement(db: Session, requirement_id: UUID) -> Requirement:
    row = db.get(Requirement, requirement_id)
    if row is None:
        raise RequirementNotFoundError("requirement not found")
    return row


def get_prd(db: Session, prd_id: UUID) -> PRD:
    row = db.get(PRD, prd_id)
    if row is None:
        raise RequirementNotFoundError("prd not found")
    return row


__all__ = [
    "Phase4Snapshot",
    "PRDOutcome",
    "PRIORITY_VALUES",
    "PRD_DRAFT",
    "PRD_REVIEWING",
    "PRD_ACCEPTED",
    "PRD_ARCHIVED",
    "PRD_STATUS_VALUES",
    "REQUIREMENT_ARCHIVED",
    "REQUIREMENT_APPROVED",
    "REQUIREMENT_DRAFT",
    "REQUIREMENT_ELIGIBLE_DECISION_STATUSES",
    "REQUIREMENT_INELIGIBLE_DECISION_STATUSES",
    "REQUIREMENT_REJECTED",
    "REQUIREMENT_REVIEWING",
    "REQUIREMENT_STATUS_VALUES",
    "RequirementConfigError",
    "RequirementError",
    "RequirementNotFoundError",
    "RequirementOutcome",
    "RequirementValidationError",
    "RISK_SPECULATIVE",
    "accept_prd",
    "create_prd_version",
    "create_requirement",
    "get_active_prd",
    "get_prd",
    "get_requirement",
    "list_prds",
    "list_requirements",
    "load_phase4_snapshot",
    "patch_prd",
    "patch_requirement",
]
