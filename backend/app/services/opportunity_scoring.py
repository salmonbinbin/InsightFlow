"""Phase 4 — Opportunity Score calculation primitives + persistence orchestration.

Two responsibilities live here:

1. Pure-function formulas for the five dimensions (User Coverage, Severity,
   Growth Trend, User Value, Business Value), the weighted total, the
   centralised Priority mapping, and the effective-weight normalisation
   that excludes unavailable non-Business dimensions. These are stateless
   and unit-tested without a database.

2. The orchestration used by the API layer to:

   * load the immutable source snapshot for an Insight (cluster members
     belonging to the source classification run);
   * compute deterministic dimensions from that snapshot;
   * call the AI User Value client (when configured);
   * persist an immutable ``OpportunityScore`` row;
   * apply PM Decision PATCHes (accept / override / defer / reject /
     set_business_value) atomically with append-only event history and
     request-id idempotency.

All numeric inputs are coerced through ``Decimal`` to avoid the
floating-point drift the brief explicitly prohibits (``(avg-1)/4*10*min
(1, n/3)``, ROUND_HALF_UP, 2 dp).
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from threading import Lock
from typing import Iterator, Sequence
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

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
    Project,
)
from app.services.opportunity_client import (
    OpportunityClient,
    OpportunityLLMResult,
    OpportunityLLMTransportError,
)

CALCULATION_VERSION = "opportunity-score-v1"
PROMPT_VERSION = settings.opportunity_prompt_version
GROWTH_WINDOW_DAYS = 30

_WEIGHT_TOLERANCE = Decimal("1e-9")
_TWO_PLACES = Decimal("0.01")
_PRIORITY_HIGH = Decimal("7.50")
_PRIORITY_MID = Decimal("5.00")

_ALLOWED_DIMENSIONS: tuple[str, ...] = (
    "user_coverage",
    "severity",
    "growth",
    "user_value",
    "business_value",
)
_NON_BUSINESS_DIMENSIONS: tuple[str, ...] = (
    "user_coverage",
    "severity",
    "growth",
    "user_value",
)


# ---- Public configuration containers ------------------------------------------


@dataclass(frozen=True)
class PriorityThresholds:
    """Centralised Priority thresholds written on every snapshot."""

    p1_below: Decimal  # score < p1_below → P2
    p0_below: Decimal  # p1_below <= score < p0_below → P1, otherwise P0

    def to_dict(self) -> dict[str, str]:
        return {
            "p1_below": format(self.p1_below, "f"),
            "p0_below": format(self.p0_below, "f"),
        }


@dataclass(frozen=True)
class ScoreWeights:
    """Five-dimension weights with centralised defaults."""

    user_coverage: Decimal
    severity: Decimal
    growth: Decimal
    user_value: Decimal
    business_value: Decimal

    def as_dict(self) -> dict[str, Decimal]:
        return {
            "user_coverage": self.user_coverage,
            "severity": self.severity,
            "growth": self.growth,
            "user_value": self.user_value,
            "business_value": self.business_value,
        }


@dataclass(frozen=True)
class DimensionValue:
    """Outcome for one dimension."""

    raw_value: Decimal | None
    score: Decimal | None
    available: bool
    note: str | None = None
    extras: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoreCalculation:
    """Result of running the formula on a snapshot."""

    breakdown: dict[str, DimensionValue]
    effective_weights: dict[str, Decimal]
    total_score: Decimal | None
    ai_priority: str | None


# ---- Errors -------------------------------------------------------------------


class OpportunityConfigError(RuntimeError):
    """Configuration error (missing model binding, invalid weights)."""


class OpportunityValidationError(RuntimeError):
    """Validation error surfaced as 422 to the API."""

    @property
    def code(self) -> str:
        message = str(self)
        for marker, code in (
            ("weights", "weights_invalid"),
            ("business_value", "business_value_invalid"),
            ("override_reason", "override_reason_invalid"),
            ("override_note", "override_note_invalid"),
            ("pm_priority", "pm_priority_invalid"),
            ("expected_version", "expected_version_mismatch"),
            ("request_id", "request_id_invalid"),
            ("action", "action_invalid"),
            ("score not found", "score_not_found"),
            ("incomplete score", "score_incomplete"),
            ("source", "source_invalid"),
            ("not found", "not_found"),
        ):
            if marker in message:
                return code
        return "schema_invalid"


class OpportunityConcurrencyError(OpportunityValidationError):
    """Raised when expected_version does not match the live row."""


class OpportunityIdempotencyError(OpportunityValidationError):
    """Raised internally when a duplicate request_id is detected in the same window."""


# ---- Pure dimension formulas --------------------------------------------------


def _coerce_decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise OpportunityValidationError(f"{field_name} must not be a boolean")
    if not isinstance(value, (Decimal, int, float, str)):
        raise OpportunityValidationError(f"{field_name} must be numeric")
    text_value = str(value).strip()
    if not text_value:
        raise OpportunityValidationError(f"{field_name} must not be empty")
    try:
        decimal_value = Decimal(text_value)
    except Exception as exc:  # noqa: BLE001
        raise OpportunityValidationError(f"{field_name} is not a valid number") from exc
    if not decimal_value.is_finite():
        raise OpportunityValidationError(f"{field_name} must be finite")
    return decimal_value


def _round_two(value: Decimal) -> Decimal:
    return value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def validate_weights(weights: Mapping[str, object]) -> ScoreWeights:
    """Validate a 5-dimension weight mapping.

    Each value must be a finite non-negative Decimal ≤ 1; the sum must be
    ``1`` within a tolerance of ``1e-9``. Order is irrelevant — the
    function only accepts the canonical five dimension keys.
    """
    if not isinstance(weights, Mapping):
        raise OpportunityValidationError("weights must be a mapping")
    unknown = set(weights.keys()) - set(_ALLOWED_DIMENSIONS)
    if unknown:
        raise OpportunityValidationError(f"weights contains unknown dimensions: {sorted(unknown)}")
    coerced: dict[str, Decimal] = {}
    for dimension in _ALLOWED_DIMENSIONS:
        if dimension not in weights:
            raise OpportunityValidationError(f"weights missing dimension {dimension!r}")
        coerced[dimension] = _coerce_decimal(weights[dimension], f"weights.{dimension}")
        if coerced[dimension] < Decimal("0") or coerced[dimension] > Decimal("1"):
            raise OpportunityValidationError(f"weights.{dimension} must be within [0, 1]")
    total = sum(coerced.values())
    if total == Decimal("0"):
        raise OpportunityValidationError("weights must not all be zero")
    if abs(total - Decimal("1")) > _WEIGHT_TOLERANCE:
        raise OpportunityValidationError(
            f"weights must sum to 1 within {_WEIGHT_TOLERANCE}; got {total}"
        )
    return ScoreWeights(
        user_coverage=coerced["user_coverage"],
        severity=coerced["severity"],
        growth=coerced["growth"],
        user_value=coerced["user_value"],
        business_value=coerced["business_value"],
    )


def coverage_dimension(
    *,
    affected_unique_users: int,
    project_unique_users: int,
) -> DimensionValue:
    """Coverage = affected_unique_users / project_unique_users × 10.

    Marked unavailable when neither denominator has a usable
    ``user_id`` count — i.e. when the denominator is zero because no
    cluster member had a non-empty ``feedbacks.user_id``. The function
    never invents a coverage ratio from empty data.
    """
    if project_unique_users <= 0 or affected_unique_users <= 0:
        return DimensionValue(
            raw_value=None,
            score=None,
            available=False,
            note="missing_user_ids",
            extras={"affected_count": affected_unique_users, "project_count": project_unique_users},
        )
    ratio = Decimal(affected_unique_users) / Decimal(project_unique_users)
    score = ratio * Decimal("10")
    return DimensionValue(
        raw_value=_round_two(score),
        score=_round_two(score),
        available=True,
        extras={"affected_count": affected_unique_users, "project_count": project_unique_users},
    )


def severity_dimension(
    *,
    average_severity: Decimal | None,
    feedback_count: int,
) -> DimensionValue:
    """Severity = (avg-1)/4 × 10 × min(1, n/3).

    Small samples shrink the score via ``reliability = min(1, n/3)``.
    """
    if average_severity is None or feedback_count <= 0:
        return DimensionValue(
            raw_value=None,
            score=None,
            available=False,
            note="missing_severity",
            extras={"feedback_count": feedback_count},
        )
    raw = (average_severity - Decimal("1")) / Decimal("4") * Decimal("10")
    reliability = min(Decimal("1"), Decimal(feedback_count) / Decimal("3"))
    final = raw * reliability
    return DimensionValue(
        raw_value=_round_two(raw),
        score=_round_two(final),
        available=True,
        extras={
            "feedback_count": feedback_count,
            "average_severity": _round_two(average_severity),
        },
    )


def growth_dimension(
    *,
    recent_count: int,
    previous_count: int,
    has_recent: bool,
    has_previous: bool,
) -> DimensionValue:
    """Two 30-day windows. Unavailable when either side lacks timestamps."""
    if not has_recent or not has_previous or previous_count <= 0:
        return DimensionValue(
            raw_value=None,
            score=None,
            available=False,
            note="insufficient_window",
            extras={
                "recent_count": recent_count if has_recent else 0,
                "previous_count": previous_count if has_previous else 0,
            },
        )
    rate = (Decimal(recent_count) - Decimal(previous_count)) / Decimal(previous_count)
    rate_clamped = max(Decimal("-1"), min(Decimal("1"), rate))
    score = max(Decimal("0"), min(Decimal("10"), Decimal("5") + Decimal("5") * rate_clamped))
    return DimensionValue(
        raw_value=_round_two(rate),
        score=_round_two(score),
        available=True,
        extras={"recent_count": recent_count, "previous_count": previous_count},
    )


def calculate_total(
    *,
    dimensions: Mapping[str, DimensionValue],
    weights: ScoreWeights,
    business_value: Decimal | None,
    thresholds: PriorityThresholds,
) -> ScoreCalculation:
    """Apply weights, renormalise, and return the total + Priority.

    Business Value is always required for the total — when missing, the
    function returns ``total_score=None`` and ``ai_priority=None``. The
    Business Value configured weight is preserved verbatim; the available
    non-Business dimensions share the remaining
    ``(1 - business_weight)`` budget in proportion to their configured
    weights. Unavailable non-Business dimensions drop to ``0``. When no
    non-Business dimension is available, Business Value's effective
    weight collapses to ``1`` so the effective weights always sum to
    ``1`` (to a ``1e-9`` Decimal tolerance).
    """
    if "business_value" not in dimensions:
        raise OpportunityValidationError("business_value dimension is required")
    business_dim = dimensions["business_value"]
    if business_value is None or not business_dim.available:
        return ScoreCalculation(
            breakdown=dict(dimensions),
            effective_weights={dim: Decimal("0") for dim in _ALLOWED_DIMENSIONS},
            total_score=None,
            ai_priority=None,
        )

    weights_dict = weights.as_dict()
    business_weight = weights_dict["business_value"]
    available_nonbusiness = [dim for dim in _NON_BUSINESS_DIMENSIONS if dimensions[dim].available]
    available_nonbusiness_weight = sum(weights_dict[dim] for dim in available_nonbusiness)
    effective: dict[str, Decimal] = {
        "user_coverage": Decimal("0"),
        "severity": Decimal("0"),
        "growth": Decimal("0"),
        "user_value": Decimal("0"),
        "business_value": Decimal("1") if not available_nonbusiness else business_weight,
    }
    total = effective["business_value"] * (business_dim.score or Decimal("0"))
    if available_nonbusiness:
        scale = (Decimal("1") - business_weight) / available_nonbusiness_weight
        for dim in available_nonbusiness:
            new_weight = weights_dict[dim] * scale
            effective[dim] = new_weight
            total += new_weight * (dimensions[dim].score or Decimal("0"))
    total_score = _round_two(total)
    return ScoreCalculation(
        breakdown=dict(dimensions),
        effective_weights=effective,
        total_score=total_score,
        ai_priority=map_priority(total_score, thresholds),
    )


def map_priority(score: Decimal, thresholds: PriorityThresholds) -> str:
    """Centralised Priority mapping; LLM cannot override this."""
    if score < Decimal("0") or score > Decimal("10"):
        raise OpportunityValidationError("score must be within [0, 10]")
    if score < thresholds.p1_below:
        return "P2"
    if score < thresholds.p0_below:
        return "P1"
    return "P0"


def default_weights() -> ScoreWeights:
    return ScoreWeights(
        user_coverage=Decimal(settings.opportunity_weight_user_coverage),
        severity=Decimal(settings.opportunity_weight_severity),
        growth=Decimal(settings.opportunity_weight_growth),
        user_value=Decimal(settings.opportunity_weight_user_value),
        business_value=Decimal(settings.opportunity_weight_business_value),
    )


def default_thresholds() -> PriorityThresholds:
    return PriorityThresholds(
        p1_below=Decimal(settings.opportunity_priority_p1_below),
        p0_below=Decimal(settings.opportunity_priority_p0_below),
    )


# ---- Source snapshot + persistence orchestration -----------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


_DECISION_LOCKS: dict[UUID, Lock] = {}
_DECISION_LOCKS_GUARD: Lock = Lock()


def _acquire_decision_lock(insight_id: UUID) -> Lock:
    with _DECISION_LOCKS_GUARD:
        lock = _DECISION_LOCKS.get(insight_id)
        if lock is None:
            lock = Lock()
            _DECISION_LOCKS[insight_id] = lock
        return lock


@contextmanager
def _execution_lock(db: Session, insight_id: UUID) -> Iterator[None]:
    """Cross-process serialisation for one Insight's decision row."""
    if db.get_bind().dialect.name == "postgresql":
        key = insight_id.int & 0x7FFF_FFFF_FFFF_FFFF
        with db.get_bind().connect() as lock_connection:
            acquired = lock_connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
            )
            if not acquired:
                raise OpportunityValidationError(f"insight {insight_id} is already being processed")
            try:
                yield
            finally:
                lock_connection.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
    else:
        lock = _acquire_decision_lock(insight_id)
        if not lock.acquire(blocking=False):
            raise OpportunityValidationError(f"insight {insight_id} is already being processed")
        try:
            yield
        finally:
            lock.release()


@dataclass(frozen=True)
class SourceSnapshot:
    """Immutable provenance bundle for a score snapshot."""

    classification_run_id: UUID
    project_id: UUID
    cluster_id: UUID
    cluster_external_id: str
    member_feedback_ids: tuple[UUID, ...]
    average_severity: Decimal | None
    feedback_count: int
    affected_unique_users: int
    snapshot_unique_users: int
    has_recent_window: bool
    has_previous_window: bool
    recent_count: int
    previous_count: int
    snapshot_hash: str
    insight_status: str


@dataclass(frozen=True)
class SourceBundle:
    """Source snapshot plus the supporting rows the LLM call relies on."""

    snapshot: SourceSnapshot
    feedback_by_id: dict[UUID, Feedback]
    members_by_id: dict[UUID, FeedbackAnalysis]
    evidence_rows: tuple[InsightEvidence, ...]


def _load_source_bundle(db: Session, insight: Insight) -> SourceBundle:
    """Load the immutable source for the insight and its supporting rows.

    Joins ``insights`` → ``insight_runs`` → ``analysis_runs`` →
    ``clusters`` → ``cluster_members`` → ``feedback_analyses`` →
    ``feedbacks``. Cross-project or cross-run members raise
    :class:`OpportunityValidationError` (mapped to 409 by the API
    layer). The Coverage denominator is the snapshot's user base, not
    the project-wide ``Feedback.user_id`` pool. The Growth windows are
    anchored to the source members' latest ``feedback_created_at``.
    """
    run = db.get(InsightRun, insight.run_id)
    if run is None:
        raise OpportunityValidationError("insight run not found")
    if run.project_id != insight.project_id:
        raise OpportunityValidationError("insight run project mismatch")
    if run.classification_run_id is None:
        raise OpportunityValidationError("insight run missing classification_run_id")

    classification_run = db.get(AnalysisRun, run.classification_run_id)
    if (
        classification_run is None
        or classification_run.project_id != insight.project_id
        or classification_run.run_type != "classification"
    ):
        raise OpportunityValidationError("classification run unavailable for insight")

    project = db.get(Project, insight.project_id)
    if project is None:
        raise OpportunityValidationError("project not found for insight")

    cluster = db.get(Cluster, insight.cluster_id)
    if cluster is None or cluster.project_id != insight.project_id:
        raise OpportunityValidationError("cluster unavailable for insight")
    if cluster.run_id != run.clustering_run_id:
        raise OpportunityValidationError("cluster does not belong to insight's clustering run")

    rows = db.execute(
        select(ClusterMember, FeedbackAnalysis, Feedback)
        .join(
            FeedbackAnalysis,
            FeedbackAnalysis.id == ClusterMember.feedback_analysis_id,
        )
        .join(Feedback, Feedback.id == FeedbackAnalysis.feedback_id)
        .where(ClusterMember.cluster_id == insight.cluster_id)
    ).all()
    if not rows:
        raise OpportunityValidationError("insight has no source members")

    feedback_ids: list[UUID] = []
    severities: list[int] = []
    affected_user_ids: set[str] = set()
    member_external_ids: list[str] = []
    has_recent_window = True
    has_previous_window = True
    recent_count = 0
    previous_count = 0

    members_by_id: dict[UUID, FeedbackAnalysis] = {}
    feedback_by_id: dict[UUID, Feedback] = {}
    valid_timestamps: list[datetime] = []
    for member, analysis, feedback in rows:
        if feedback.project_id != insight.project_id:
            raise OpportunityValidationError("member feedback project mismatch; source_invalid")
        if analysis.run_id != classification_run.id:
            raise OpportunityValidationError(
                "member analysis belongs to a different classification run; source_invalid"
            )
        feedback_ids.append(feedback.id)
        members_by_id[feedback.id] = analysis
        feedback_by_id[feedback.id] = feedback
        if analysis.severity is not None:
            severities.append(analysis.severity)
        if feedback.user_id and feedback.user_id.strip():
            affected_user_ids.add(feedback.user_id.strip())
        member_external_ids.append(str(feedback.external_id or ""))
        ts = feedback.feedback_created_at
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            valid_timestamps.append(ts)

    if not feedback_ids:
        raise OpportunityValidationError("insight has no source members")

    average_severity: Decimal | None = None
    if severities:
        average_severity = (Decimal(sum(severities)) / Decimal(len(severities))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    if valid_timestamps:
        anchor = max(valid_timestamps)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        for fid in feedback_ids:
            ts = feedback_by_id[fid].feedback_created_at
            if ts is None:
                has_recent_window = False
                has_previous_window = False
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_seconds = (anchor - ts).total_seconds()
            if age_seconds < 0:
                has_recent_window = False
                continue
            if age_seconds <= GROWTH_WINDOW_DAYS * 86400:
                recent_count += 1
            elif age_seconds <= GROWTH_WINDOW_DAYS * 2 * 86400:
                previous_count += 1
    else:
        has_recent_window = False
        has_previous_window = False

    expected_member_ids = sorted(insight.source_member_external_ids or [])
    actual_member_ids = sorted(member_external_ids)
    if expected_member_ids != actual_member_ids:
        raise OpportunityValidationError(
            "cluster member external ids diverge from insight.source_member_external_ids; source_invalid"
        )

    snapshot_ext_ids = list(classification_run.input_feedback_external_ids or [])
    snapshot_unique_users = 0
    if snapshot_ext_ids:
        user_rows = db.execute(
            select(Feedback.user_id).where(
                Feedback.project_id == insight.project_id,
                Feedback.external_id.in_(snapshot_ext_ids),
            )
        ).all()
        stripped_users: set[str] = set()
        for (uid,) in user_rows:
            if uid is None:
                continue
            key = str(uid).strip()
            if key:
                stripped_users.add(key)
        snapshot_unique_users = len(stripped_users)

    evidence_rows = tuple(
        db.scalars(
            select(InsightEvidence)
            .where(
                InsightEvidence.insight_id == insight.id,
                InsightEvidence.feedback_classification_run_id == classification_run.id,
            )
            .order_by(InsightEvidence.id)
        ).all()
    )
    if not evidence_rows:
        raise OpportunityValidationError(
            "insight has no InsightEvidence rows bound to the source "
            "classification run; source_invalid"
        )

    canonical_lines: list[str] = []
    for fid in sorted(feedback_ids, key=str):
        feedback = feedback_by_id[fid]
        analysis = members_by_id[fid]
        ts_value = feedback.feedback_created_at
        ts_iso = ts_value.isoformat() if ts_value is not None else ""
        canonical_lines.append(
            "\t".join(
                [
                    str(fid),
                    str(feedback.external_id or ""),
                    str(feedback.content or ""),
                    str(feedback.user_id or ""),
                    ts_iso,
                    "" if analysis.severity is None else str(analysis.severity),
                    str(analysis.feedback_type or ""),
                ]
            )
        )
    for ev in sorted(evidence_rows, key=lambda e: str(e.id)):
        canonical_lines.append(
            "\t".join(
                [
                    str(ev.id),
                    str(ev.feedback_external_id or ""),
                    str(ev.feedback_classification_run_id or ""),
                    str(ev.evidence_type or ""),
                    str(ev.relevance_score),
                ]
            )
        )
    raw = "\n".join(canonical_lines).encode("utf-8")
    snapshot_hash = hashlib.sha256(raw).hexdigest()

    snapshot_obj = SourceSnapshot(
        classification_run_id=classification_run.id,
        project_id=insight.project_id,
        cluster_id=insight.cluster_id,
        cluster_external_id=insight.source_cluster_external_id,
        member_feedback_ids=tuple(feedback_ids),
        average_severity=average_severity,
        feedback_count=len(feedback_ids),
        affected_unique_users=len(affected_user_ids),
        snapshot_unique_users=snapshot_unique_users,
        has_recent_window=has_recent_window,
        has_previous_window=has_previous_window,
        recent_count=recent_count,
        previous_count=previous_count,
        snapshot_hash=snapshot_hash,
        insight_status=insight.status,
    )
    return SourceBundle(
        snapshot=snapshot_obj,
        feedback_by_id=feedback_by_id,
        members_by_id=members_by_id,
        evidence_rows=evidence_rows,
    )


def _select_user_value(
    db: Session,
    insight: Insight,
    snapshot: SourceSnapshot,
) -> Decimal | None:
    """Decide whether to call the LLM or reuse a recent snapshot.

    Reuses the most recent OpportunityScore that shares the same
    source snapshot hash and prompt version when the prompt version
    matches the current binding, so a Business Value recompute is
    deterministic. Returns the User Value Decimal or ``None`` when
    no prior snapshot is available.
    """
    from app.models.opportunity import OpportunityScore  # local import to avoid cycle

    recent = db.execute(
        select(OpportunityScore)
        .where(
            OpportunityScore.insight_id == insight.id,
            OpportunityScore.source_snapshot_hash == snapshot.snapshot_hash,
            OpportunityScore.prompt_version == PROMPT_VERSION,
        )
        .order_by(OpportunityScore.created_at.desc(), OpportunityScore.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if recent is not None:
        return (
            _round_two(Decimal(str(recent.user_value_score)))
            if recent.user_value_score is not None
            else None
        )
    return None


# ---- AI call wrapper ----------------------------------------------------------


def _call_user_value(
    *,
    insight: Insight,
    snapshot: SourceSnapshot,
    evidence_rows: Sequence[InsightEvidence],
    feedback_by_id: dict[UUID, Feedback],
    members_by_id: dict[UUID, FeedbackAnalysis],
) -> tuple[Decimal, str, int, int, int, int, int, int]:
    """Invoke the AI client for User Value with bounded transport retry.

    Returns ``(score, reasoning, input_tokens, output_tokens,
    unknown_token_calls, latency_ms, attempt_count, retry_count)``.

    Only :class:`OpportunityLLMTransportError` is retried up to
    ``opportunity_max_retries`` times — protocol / configuration
    failures surface immediately so a malformed payload cannot be
    replayed indefinitely.
    """
    if not (settings.llm_api_key and settings.llm_base_url and settings.llm_model):
        raise OpportunityConfigError(
            "LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 未配置；无法生成 User Value。"
        )
    evidence_payload = []
    for row in evidence_rows:
        if row.feedback_classification_run_id != snapshot.classification_run_id:
            continue
        feedback = feedback_by_id.get(row.feedback_id)
        analysis = members_by_id.get(row.feedback_id)
        if feedback is None or analysis is None:
            continue
        evidence_payload.append(
            {
                "feedback_external_id": feedback.external_id,
                "severity": analysis.severity,
                "sentiment": analysis.sentiment,
                "feedback_type": analysis.feedback_type,
                "module": analysis.module,
                "pain_point": analysis.pain_point,
                "intent": analysis.intent,
            }
        )
    payload = {
        "insight": {
            "title": insight.title,
            "pain_point": insight.pain_point,
            "affected_segment": insight.affected_segment,
            "insight": insight.insight,
            "recommendation": insight.recommendation,
            "status": insight.status,
            "feedback_count": insight.feedback_count,
            "evidence_count": insight.evidence_count,
        },
        "evidence_summary": {
            "feedback_count": snapshot.feedback_count,
            "average_severity": (
                float(snapshot.average_severity) if snapshot.average_severity is not None else None
            ),
            "affected_unique_users": snapshot.affected_unique_users,
            "snapshot_unique_users": snapshot.snapshot_unique_users,
        },
        "evidence": evidence_payload,
    }

    max_attempts = max(1, int(settings.opportunity_max_retries) + 1)
    attempt_count = 0
    last_transport_error: Exception | None = None
    started = time.monotonic()
    while attempt_count < max_attempts:
        attempt_count += 1
        try:
            with OpportunityClient(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                timeout_seconds=settings.llm_timeout_seconds,
            ) as client:
                result: OpportunityLLMResult = client.generate_user_value(payload)
        except OpportunityLLMTransportError as exc:
            last_transport_error = exc
            continue
        latency_ms = int((time.monotonic() - started) * 1000)
        retry_count = max(0, attempt_count - 1)
        score = Decimal(str(result.payload["user_value_score"]))
        reasoning = str(result.payload["reasoning"])
        unknown = (
            1 if result.usage.input_tokens is None or result.usage.output_tokens is None else 0
        )
        return (
            _round_two(score),
            reasoning,
            result.usage.input_tokens or 0,
            result.usage.output_tokens or 0,
            unknown,
            latency_ms,
            attempt_count,
            retry_count,
        )
    latency_ms = int((time.monotonic() - started) * 1000)
    if last_transport_error is not None:
        raise last_transport_error
    raise OpportunityLLMTransportError("opportunity LLM transport retry exhausted")


# ---- Public orchestration ----------------------------------------------------


@dataclass
class ScoreOutcome:
    """Result of one score snapshot creation."""

    score: object  # OpportunityScore row; typed in app.models.opportunity
    decision: object  # InsightDecision row or None


def create_opportunity_score(
    db: Session,
    insight: Insight,
    *,
    weights: ScoreWeights | None = None,
    business_value: Decimal | None = None,
    manage_decision: bool = True,
) -> ScoreOutcome:
    """Build and persist a new immutable score snapshot.

    Flush-only — callers own the transaction. The HTTP layer commits
    after this returns; ``apply_decision_patch`` wraps a
    ``set_business_value`` invocation so score + decision + event all
    commit atomically.

    When ``manage_decision`` is ``False`` the helper only persists the
    score row; the caller is responsible for updating / versioning the
    ``InsightDecision`` so a single PATCH increments the version
    exactly once.
    """
    from app.models.opportunity import (
        DECISION_PENDING_REVIEW,
        InsightDecision,
        OpportunityScore,
    )

    bundle = _load_source_bundle(db, insight)
    snapshot = bundle.snapshot
    used_weights = weights if weights is not None else default_weights()
    validate_weights(used_weights.as_dict())
    thresholds = default_thresholds()

    coverage = coverage_dimension(
        affected_unique_users=snapshot.affected_unique_users,
        project_unique_users=snapshot.snapshot_unique_users,
    )
    severity = severity_dimension(
        average_severity=snapshot.average_severity,
        feedback_count=snapshot.feedback_count,
    )
    growth = growth_dimension(
        recent_count=snapshot.recent_count,
        previous_count=snapshot.previous_count,
        has_recent=snapshot.has_recent_window,
        has_previous=snapshot.has_previous_window,
    )

    reused_user_value = _select_user_value(db, insight, snapshot)

    input_tokens = 0
    output_tokens = 0
    unknown_token_calls = 0
    latency_ms_total = 0
    attempt_count_total = 0
    retry_count_total = 0
    user_value_reasoning = "未调用模型；缺少配置或无可复用 User Value。"

    if reused_user_value is not None:
        user_value = reused_user_value
        user_value_reasoning = "复用最近一次同源 / 同 Prompt / 同模型的 User Value。"
    else:
        (
            user_value,
            user_value_reasoning,
            in_tok,
            out_tok,
            unknown,
            latency_ms_total,
            attempt_count_total,
            retry_count_total,
        ) = _call_user_value(
            insight=insight,
            snapshot=snapshot,
            evidence_rows=bundle.evidence_rows,
            feedback_by_id=bundle.feedback_by_id,
            members_by_id=bundle.members_by_id,
        )
        input_tokens += in_tok
        output_tokens += out_tok
        unknown_token_calls += unknown

    user_value_dimension = DimensionValue(
        raw_value=user_value,
        score=user_value,
        available=user_value is not None,
        note=None if user_value is not None else "user_value_unavailable",
    )
    business_value_dimension = DimensionValue(
        raw_value=business_value,
        score=business_value,
        available=business_value is not None,
    )

    calculation = calculate_total(
        dimensions={
            "user_coverage": coverage,
            "severity": severity,
            "growth": growth,
            "user_value": user_value_dimension,
            "business_value": business_value_dimension,
        },
        weights=used_weights,
        business_value=business_value,
        thresholds=thresholds,
    )

    breakdown_json = {
        "user_coverage": {
            "raw_value": _decimal_to_str(coverage.raw_value),
            "score": _decimal_to_str(coverage.score),
            "available": coverage.available,
            "note": coverage.note,
            "extras": _stringify_extras(coverage.extras),
        },
        "severity": {
            "raw_value": _decimal_to_str(severity.raw_value),
            "score": _decimal_to_str(severity.score),
            "available": severity.available,
            "note": severity.note,
            "extras": _stringify_extras(severity.extras),
        },
        "growth": {
            "raw_value": _decimal_to_str(growth.raw_value),
            "score": _decimal_to_str(growth.score),
            "available": growth.available,
            "note": growth.note,
            "extras": _stringify_extras(growth.extras),
        },
        "user_value": {
            "raw_value": _decimal_to_str(user_value_dimension.raw_value),
            "score": _decimal_to_str(user_value_dimension.score),
            "available": user_value_dimension.available,
            "note": user_value_dimension.note,
            "extras": {},
        },
        "business_value": {
            "raw_value": _decimal_to_str(business_value_dimension.raw_value),
            "score": _decimal_to_str(business_value_dimension.score),
            "available": business_value_dimension.available,
            "note": business_value_dimension.note,
            "extras": {},
        },
    }
    effective_serialised = {
        k: _decimal_to_str_full(v) for k, v in calculation.effective_weights.items()
    }
    weights_serialised = {k: _decimal_to_str_full(v) for k, v in used_weights.as_dict().items()}

    score_row = OpportunityScore(
        insight_id=insight.id,
        project_id=insight.project_id,
        classification_run_id=snapshot.classification_run_id,
        source_insight_status=snapshot.insight_status,
        source_snapshot_hash=snapshot.snapshot_hash,
        breakdown=breakdown_json,
        weights_configured=weights_serialised,
        weights_effective=effective_serialised,
        thresholds=thresholds.to_dict(),
        business_value=business_value,
        total_score=calculation.total_score,
        ai_priority=calculation.ai_priority,
        ai_reasoning=_build_ai_reasoning(
            snapshot=snapshot,
            breakdown=breakdown_json,
            effective=effective_serialised,
            total=calculation.total_score,
            ai_priority=calculation.ai_priority,
            user_value_reasoning=user_value_reasoning,
        ),
        user_value_score=user_value,
        user_value_reasoning=user_value_reasoning,
        calculation_version=CALCULATION_VERSION,
        model=settings.llm_model,
        prompt_version=PROMPT_VERSION,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        unknown_token_calls=unknown_token_calls,
        latency_ms=latency_ms_total,
        attempt_count=attempt_count_total,
        retry_count=retry_count_total,
    )
    db.add(score_row)
    db.flush()

    decision = None
    if manage_decision:
        decision = db.scalar(
            select(InsightDecision).where(InsightDecision.insight_id == insight.id)
        )
        if decision is None:
            decision = InsightDecision(
                insight_id=insight.id,
                project_id=insight.project_id,
                current_score_id=score_row.id,
                business_value=business_value,
                ai_priority=calculation.ai_priority,
                pm_priority=None,
                status=DECISION_PENDING_REVIEW,
                override_reason=None,
                override_note=None,
                version=1,
            )
            db.add(decision)
        else:
            decision.current_score_id = score_row.id
            decision.business_value = business_value
            decision.ai_priority = calculation.ai_priority
            decision.pm_priority = None
            decision.status = DECISION_PENDING_REVIEW
            decision.override_reason = None
            decision.override_note = None
            decision.version = decision.version + 1
            decision.updated_at = _now()
        db.flush()
    else:
        decision = db.scalar(
            select(InsightDecision).where(InsightDecision.insight_id == insight.id)
        )
    return ScoreOutcome(score=score_row, decision=decision)


def _decimal_to_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return format(value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP), "f")


def _decimal_to_str_full(value: object) -> str | None:
    """Serialise a Decimal preserving its full precision (no two-dp rounding).

    Used for ``weights_configured`` / ``weights_effective`` so the stored
    values still sum to 1 within ``1e-9`` after renormalisation; the
    public ``_decimal_to_str`` still quantises score / total_score to two
    decimal places. Decimal's default ``str`` representation round-trips
    losslessly through ``Decimal(str(...))``.
    """
    if value is None:
        return None
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return format(value, "f")


def _stringify_extras(extras: Mapping[str, object]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in extras.items():
        if isinstance(value, Decimal):
            out[key] = format(value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP), "f")
        else:
            out[key] = value
    return out


def _build_ai_reasoning(
    *,
    snapshot: SourceSnapshot,
    breakdown: dict,
    effective: dict,
    total: Decimal | None,
    ai_priority: str | None,
    user_value_reasoning: str,
) -> str:
    """Compose a deterministic reasoning string from formula breakdown."""
    parts = [
        f"calculation_version={CALCULATION_VERSION}",
        f"feedback_count={snapshot.feedback_count}",
        f"affected_unique_users={snapshot.affected_unique_users}",
        f"snapshot_unique_users={snapshot.snapshot_unique_users}",
        f"user_value_available={breakdown['user_value']['available']}",
        f"effective_weights={effective}",
    ]
    if total is not None and ai_priority is not None:
        parts.append(f"total_score={_decimal_to_str(total)}")
        parts.append(f"ai_priority={ai_priority}")
    else:
        parts.append("total_score=null")
        parts.append("ai_priority=null")
    parts.append(f"user_value_reasoning={user_value_reasoning}")
    return " | ".join(parts)


# ---- Decision patch ---------------------------------------------------------


def _validate_action(
    *,
    action: str,
    business_value: Decimal | None,
    pm_priority: str | None,
    override_reason: str | None,
    override_note: str | None,
    decision_has_full_score: bool,
) -> None:
    if action == "set_business_value":
        if business_value is None:
            raise OpportunityValidationError(
                "business_value is required for set_business_value action"
            )
        if not (Decimal("0") <= business_value <= Decimal("10")):
            raise OpportunityValidationError("business_value must be within [0, 10]")
        return
    if not decision_has_full_score:
        raise OpportunityValidationError("action requires a complete score snapshot")
    if action == "accept":
        # Accept aligns PM Priority with AI Priority; the caller does
        # not supply pm_priority.
        return
    if action == "override":
        if pm_priority is None:
            raise OpportunityValidationError("override requires pm_priority")
        if override_reason is None:
            raise OpportunityValidationError("override requires override_reason")
        if override_reason == "other":
            if not override_note or not override_note.strip():
                raise OpportunityValidationError("override_reason=other requires override_note")
        return
    if action in ("defer", "reject"):
        return
    raise OpportunityValidationError(f"unknown action: {action}")


def _capture_decision_state(decision: object) -> dict[str, object]:
    """Snapshot every replay-relevant column from ``decision``."""
    return {
        "id": str(decision.id),
        "insight_id": str(decision.insight_id),
        "project_id": str(decision.project_id),
        "current_score_id": (
            str(decision.current_score_id) if decision.current_score_id is not None else None
        ),
        "business_value": _decimal_to_str(decision.business_value),
        "ai_priority": decision.ai_priority,
        "pm_priority": decision.pm_priority,
        "status": decision.status,
        "override_reason": decision.override_reason,
        "override_note": decision.override_note,
        "version": decision.version,
        "created_at": decision.created_at.isoformat() if decision.created_at else None,
        "updated_at": decision.updated_at.isoformat() if decision.updated_at else None,
    }


def _decision_dto_from_after_state(after_state: Mapping[str, object]):
    """Build an independent read-only DTO mirroring ``InsightDecision``.

    Replay must not touch the live object — the caller observes the
    state at the time of the original event. The DTO exposes every
    column ``_decision_to_read`` projects, plus ``utctimestamp``
    fields ``_decision_to_read`` parses via ``UTCDateTime``.
    """
    from datetime import datetime as _dt
    from types import SimpleNamespace

    sid_raw = after_state.get("current_score_id")
    current_score_id = UUID(str(sid_raw)) if sid_raw else None
    bv_raw = after_state.get("business_value")
    business_value = (
        float(Decimal(str(bv_raw)).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP))
        if bv_raw not in (None, "")
        else None
    )
    created_raw = after_state.get("created_at")
    updated_raw = after_state.get("updated_at")
    created_at = _dt.fromisoformat(str(created_raw)) if created_raw else None
    updated_at = _dt.fromisoformat(str(updated_raw)) if updated_raw else None
    return SimpleNamespace(
        id=UUID(str(after_state["id"])),
        insight_id=UUID(str(after_state["insight_id"])),
        project_id=UUID(str(after_state["project_id"])),
        current_score_id=current_score_id,
        business_value=business_value,
        ai_priority=after_state.get("ai_priority"),
        pm_priority=after_state.get("pm_priority"),
        status=after_state.get("status"),
        override_reason=after_state.get("override_reason"),
        override_note=after_state.get("override_note"),
        version=int(after_state.get("version") or 0),
        created_at=created_at,
        updated_at=updated_at,
    )


def apply_decision_patch(
    db: Session,
    insight: Insight,
    *,
    request_id: UUID,
    expected_version: int,
    action: str,
    business_value: Decimal | None,
    pm_priority: str | None,
    override_reason: str | None,
    override_note: str | None,
) -> object:
    """Apply one Decision PATCH inside a single serialised transaction.

    Steps:

    1. Acquire the per-insight lock (cross-process on PostgreSQL).
    2. Load the current Decision (creates one if missing).
    3. Replay: a duplicate ``request_id`` returns an independent DTO
       built from the original event's ``after_state``. The live
       ``InsightDecision`` row is not mutated, not flushed, and not
       committed. The caller-supplied ``expected_version`` is
       deliberately ignored on the replay path.
    4. Validate ``expected_version``; reject mismatch (409).
    5. Apply state-machine rules. For ``set_business_value`` the
       flush-only score helper is invoked and the helper's decision
       update is suppressed (``manage_decision=False``) so this
       function is the sole owner of the PATCH lifecycle. Score +
       Decision + Event commit together at the end.
    6. Append one ``InsightDecisionEvent`` row.

    Any exception raised inside the lock rolls back the entire
    transaction so an early event-write failure never leaves a
    half-committed state.
    """
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

    try:
        with _execution_lock(db, insight.id):
            decision = db.scalar(
                select(InsightDecision).where(InsightDecision.insight_id == insight.id)
            )
            if decision is None:
                decision = InsightDecision(
                    insight_id=insight.id,
                    project_id=insight.project_id,
                    current_score_id=None,
                    business_value=None,
                    ai_priority=None,
                    pm_priority=None,
                    status=DECISION_PENDING_REVIEW,
                    override_reason=None,
                    override_note=None,
                    version=0,
                )
                db.add(decision)
                db.flush()

            # Idempotency: a duplicate request_id returns an independent
            # DTO built from the original event's after_state. The live
            # row is untouched so concurrent in-flight PATCHes and
            # follow-up replays cannot be poisoned.
            existing_event = db.scalar(
                select(InsightDecisionEvent).where(
                    InsightDecisionEvent.insight_id == insight.id,
                    InsightDecisionEvent.request_id == request_id,
                )
            )
            if existing_event is not None:
                if existing_event.after_state:
                    return _decision_dto_from_after_state(existing_event.after_state)
                return _decision_dto_from_after_state(_capture_decision_state(decision))

            if decision.version != expected_version:
                raise OpportunityConcurrencyError(
                    f"expected_version mismatch: got {expected_version} actual {decision.version}"
                )

            current_score: OpportunityScore | None = None
            if decision.current_score_id is not None:
                current_score = db.get(OpportunityScore, decision.current_score_id)
            decision_has_full_score = bool(
                current_score is not None
                and current_score.business_value is not None
                and current_score.total_score is not None
                and current_score.ai_priority is not None
            )
            _validate_action(
                action=action,
                business_value=business_value,
                pm_priority=pm_priority,
                override_reason=override_reason,
                override_note=override_note,
                decision_has_full_score=decision_has_full_score,
            )
            if action == "override" and current_score is not None:
                if pm_priority == current_score.ai_priority:
                    raise OpportunityValidationError(
                        "override requires pm_priority to differ from ai_priority"
                    )

            before_state = _capture_decision_state(decision)

            if action == "set_business_value":
                assert business_value is not None
                reused_weights: ScoreWeights | None = None
                if current_score is not None:
                    reused_weights = validate_weights(current_score.weights_configured or {})
                outcome = create_opportunity_score(
                    db,
                    insight,
                    weights=reused_weights,
                    business_value=business_value,
                    manage_decision=False,
                )
                score_row = outcome.score
                decision.current_score_id = score_row.id
                decision.business_value = business_value
                decision.ai_priority = score_row.ai_priority
                decision.pm_priority = None
                decision.status = DECISION_PENDING_REVIEW
                decision.override_reason = None
                decision.override_note = None
                decision.version = decision.version + 1
                decision.updated_at = _now()
            else:
                if action == "accept":
                    decision.status = DECISION_ACCEPTED
                    decision.pm_priority = decision.ai_priority
                elif action == "override":
                    decision.status = DECISION_OVERRIDDEN
                    decision.pm_priority = pm_priority
                    decision.override_reason = override_reason
                    decision.override_note = (override_note or "").strip() or None
                elif action == "defer":
                    decision.status = DECISION_DEFERRED
                elif action == "reject":
                    decision.status = DECISION_REJECTED
                else:  # pragma: no cover - validated above
                    raise OpportunityValidationError(f"unknown action: {action}")
                decision.version = decision.version + 1
                decision.updated_at = _now()
            db.flush()

            after_state = _capture_decision_state(decision)

            event = InsightDecisionEvent(
                insight_id=insight.id,
                decision_id=decision.id,
                score_id=decision.current_score_id,
                request_id=request_id,
                event_type=action,
                before_state=before_state,
                after_state=after_state,
                business_value=decision.business_value,
                ai_priority=decision.ai_priority,
                pm_priority=decision.pm_priority,
                override_reason=decision.override_reason,
                override_note=decision.override_note,
                version_after=decision.version,
            )
            db.add(event)
            db.flush()
            db.commit()
            db.refresh(decision)
            return decision
    except Exception:
        db.rollback()
        raise


__all__ = [
    "CALCULATION_VERSION",
    "DimensionValue",
    "GROWTH_WINDOW_DAYS",
    "OpportunityConcurrencyError",
    "OpportunityConfigError",
    "OpportunityValidationError",
    "PriorityThresholds",
    "PROMPT_VERSION",
    "ScoreCalculation",
    "ScoreOutcome",
    "ScoreWeights",
    "SourceBundle",
    "SourceSnapshot",
    "apply_decision_patch",
    "calculate_total",
    "coverage_dimension",
    "create_opportunity_score",
    "default_thresholds",
    "default_weights",
    "growth_dimension",
    "map_priority",
    "severity_dimension",
    "validate_weights",
]
