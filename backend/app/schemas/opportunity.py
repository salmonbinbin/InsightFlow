"""Pydantic schemas for Phase 4 Opportunity Score + Human Decision endpoints."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import UTCDateTime

# ---- Request bodies ----------------------------------------------------------


class OpportunityWeights(BaseModel):
    """Five-dimension weight override; must sum to 1 within 1e-9 tolerance."""

    model_config = ConfigDict(extra="forbid")

    user_coverage: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    severity: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    growth: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    user_value: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    business_value: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))


class ScoreCreateRequest(BaseModel):
    """Body for ``POST /insights/{insight_id}/opportunity-score``.

    ``weights`` is optional; when omitted the Phase 4 default weights
    from :class:`app.db.config.Settings` are used. The endpoint must
    not carry Business Value — that field is intentionally absent and
    ``extra=forbid`` rejects it so PATCH ``set_business_value`` is the
    sole owner of BV writes (with idempotency + optimistic concurrency
    + append-only event).
    """

    model_config = ConfigDict(extra="forbid")

    weights: OpportunityWeights | None = None


OverrideReasonLiteral = Literal[
    "strategic_priority",
    "core_user_experience",
    "revenue_impact",
    "development_cost",
    "management_decision",
    "data_quality",
    "other",
]

DecisionActionLiteral = Literal[
    "set_business_value",
    "accept",
    "override",
    "defer",
    "reject",
]

PriorityLiteral = Literal["P0", "P1", "P2"]


class DecisionPatchRequest(BaseModel):
    """Body for ``PATCH /insights/{insight_id}/decision``.

    ``request_id`` is the client-generated idempotency key; the same
    combination of insight + request_id never produces two events. The
    ``expected_version`` enables optimistic concurrency on the decision
    row; a mismatch returns 409.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    expected_version: int = Field(ge=0)
    action: DecisionActionLiteral
    business_value: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("10"))
    pm_priority: PriorityLiteral | None = None
    override_reason: OverrideReasonLiteral | None = None
    override_note: str | None = Field(default=None, max_length=2000)


# ---- Dimension breakdowns -----------------------------------------------------


class DimensionRead(BaseModel):
    """One deterministic dimension row inside a score snapshot."""

    model_config = ConfigDict(from_attributes=True)

    raw_value: Decimal | None
    score: Decimal | None
    available: bool
    affected_count: int | None = None
    project_count: int | None = None
    feedback_count: int | None = None
    average_severity: Decimal | None = None
    recent_count: int | None = None
    previous_count: int | None = None
    note: str | None = None


class WeightsRead(BaseModel):
    """Configured defaults and the actual weights applied to one snapshot."""

    model_config = ConfigDict(from_attributes=True)

    configured: dict[str, str]
    effective: dict[str, str]


class ScoreBreakdownRead(BaseModel):
    """Per-dimension breakdown inside one score snapshot."""

    user_coverage: DimensionRead
    severity: DimensionRead
    growth: DimensionRead
    user_value: DimensionRead
    business_value: DimensionRead


# ---- Score / decision reads ---------------------------------------------------


class OpportunityScoreRead(BaseModel):
    """Read shape for one immutable opportunity score snapshot."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    insight_id: UUID
    project_id: UUID
    classification_run_id: UUID
    source_insight_status: str
    source_snapshot_hash: str
    breakdown: ScoreBreakdownRead
    weights: WeightsRead
    thresholds: dict[str, str]
    business_value: Decimal | None
    total_score: Decimal | None
    ai_priority: str | None
    ai_reasoning: str
    calculation_version: str
    model: str | None
    prompt_version: str
    input_tokens: int
    output_tokens: int
    unknown_token_calls: int
    latency_ms: int
    attempt_count: int
    retry_count: int
    created_at: UTCDateTime


class OpportunityScorePage(BaseModel):
    total: int
    items: list[OpportunityScoreRead]
    limit: int
    offset: int


class InsightDecisionRead(BaseModel):
    """Current Decision state for an Insight."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    insight_id: UUID
    project_id: UUID
    current_score_id: UUID | None
    business_value: Decimal | None
    ai_priority: str | None
    pm_priority: str | None
    status: str
    override_reason: str | None
    override_note: str | None
    version: int
    created_at: UTCDateTime
    updated_at: UTCDateTime


class InsightDecisionEventRead(BaseModel):
    """Append-only event row inside ``insight_decision_events``."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    insight_id: UUID
    decision_id: UUID
    score_id: UUID | None
    request_id: UUID
    event_type: str
    before_state: dict | None
    after_state: dict | None
    business_value: Decimal | None
    ai_priority: str | None
    pm_priority: str | None
    override_reason: str | None
    override_note: str | None
    version_after: int
    created_at: UTCDateTime


class InsightDecisionEventPage(BaseModel):
    total: int
    items: list[InsightDecisionEventRead]
    limit: int
    offset: int


__all__ = [
    "DecisionActionLiteral",
    "DecisionPatchRequest",
    "DimensionRead",
    "InsightDecisionEventPage",
    "InsightDecisionEventRead",
    "InsightDecisionRead",
    "OpportunityScorePage",
    "OpportunityScoreRead",
    "OpportunityWeights",
    "OverrideReasonLiteral",
    "PriorityLiteral",
    "ScoreBreakdownRead",
    "ScoreCreateRequest",
    "WeightsRead",
]
