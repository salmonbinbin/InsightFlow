"""Phase 4 — Opportunity Score + Human Decision endpoints.

Six HTTP routes:

* ``POST /insights/{insight_id}/opportunity-score``
* ``GET  /insights/{insight_id}/opportunity-score``
* ``GET  /insights/{insight_id}/opportunity-scores``
* ``GET  /insights/{insight_id}/decision``
* ``PATCH /insights/{insight_id}/decision``
* ``GET  /insights/{insight_id}/decision/history``

Routes only handle schema, session, pagination and exception mapping.
Business logic lives in
:mod:`app.services.opportunity_scoring`.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import Insight
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
from app.schemas.opportunity import (
    DecisionPatchRequest,
    DimensionRead,
    InsightDecisionEventPage,
    InsightDecisionEventRead,
    InsightDecisionRead,
    OpportunityScorePage,
    OpportunityScoreRead,
    ScoreBreakdownRead,
    ScoreCreateRequest,
    WeightsRead,
)
from app.services.opportunity_client import (
    OpportunityLLMConfigError,
    OpportunityLLMProtocolError,
    OpportunityLLMTransportError,
)
from app.services.opportunity_scoring import (
    OpportunityConcurrencyError,
    OpportunityConfigError,
    OpportunityValidationError,
    apply_decision_patch,
    create_opportunity_score,
    validate_weights,
)

router = APIRouter(tags=["opportunities"])

# Allowed PM Decision event types — guarded here so the route layer can
# emit a stable ``status`` value even if the service layer raises.
_DECISION_EVENT_TYPES = (
    "set_business_value",
    "accept",
    "override",
    "defer",
    "reject",
)

_DECISION_STATUS_VALUES = (
    DECISION_PENDING_REVIEW,
    DECISION_ACCEPTED,
    DECISION_OVERRIDDEN,
    DECISION_DEFERRED,
    DECISION_REJECTED,
)


# ---- Helpers -----------------------------------------------------------------


def _require_insight(db: Session, insight_id: UUID) -> Insight:
    insight = db.get(Insight, insight_id)
    if insight is None:
        raise HTTPException(404, "洞察不存在")
    return insight


def _decimal_or_none(value: float | int | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _score_to_read(row: OpportunityScore) -> OpportunityScoreRead:
    breakdown_raw = row.breakdown or {}
    weights_raw = {
        "configured": row.weights_configured or {},
        "effective": row.weights_effective or {},
    }
    breakdown = ScoreBreakdownRead(
        user_coverage=DimensionRead(**breakdown_raw.get("user_coverage", {})),
        severity=DimensionRead(**breakdown_raw.get("severity", {})),
        growth=DimensionRead(**breakdown_raw.get("growth", {})),
        user_value=DimensionRead(**breakdown_raw.get("user_value", {})),
        business_value=DimensionRead(**breakdown_raw.get("business_value", {})),
    )
    return OpportunityScoreRead(
        id=row.id,
        insight_id=row.insight_id,
        project_id=row.project_id,
        classification_run_id=row.classification_run_id,
        source_insight_status=row.source_insight_status,
        source_snapshot_hash=row.source_snapshot_hash,
        breakdown=breakdown,
        weights=WeightsRead(**weights_raw),
        thresholds=row.thresholds or {},
        business_value=_decimal_or_none(row.business_value),
        total_score=_decimal_or_none(row.total_score),
        ai_priority=row.ai_priority,
        ai_reasoning=row.ai_reasoning or "",
        calculation_version=row.calculation_version,
        model=row.model,
        prompt_version=row.prompt_version,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        unknown_token_calls=row.unknown_token_calls,
        latency_ms=row.latency_ms,
        attempt_count=row.attempt_count,
        retry_count=row.retry_count,
        created_at=row.created_at,
    )


def _decision_to_read(row: InsightDecision) -> InsightDecisionRead:
    return InsightDecisionRead(
        id=row.id,
        insight_id=row.insight_id,
        project_id=row.project_id,
        current_score_id=row.current_score_id,
        business_value=_decimal_or_none(row.business_value),
        ai_priority=row.ai_priority,
        pm_priority=row.pm_priority,
        status=row.status,
        override_reason=row.override_reason,
        override_note=row.override_note,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _event_to_read(row: InsightDecisionEvent) -> InsightDecisionEventRead:
    return InsightDecisionEventRead(
        id=row.id,
        insight_id=row.insight_id,
        decision_id=row.decision_id,
        score_id=row.score_id,
        request_id=row.request_id,
        event_type=row.event_type,
        before_state=row.before_state,
        after_state=row.after_state,
        business_value=_decimal_or_none(row.business_value),
        ai_priority=row.ai_priority,
        pm_priority=row.pm_priority,
        override_reason=row.override_reason,
        override_note=row.override_note,
        version_after=row.version_after,
        created_at=row.created_at,
    )


def _resolve_validation_error(exc: OpportunityValidationError) -> HTTPException:
    code = exc.code
    if code == "not_found":
        return HTTPException(404, "洞察不存在")
    if code == "expected_version_mismatch":
        return HTTPException(409, str(exc))
    if code == "source_invalid":
        return HTTPException(409, str(exc))
    if code == "score_not_found":
        return HTTPException(409, str(exc))
    return HTTPException(422, str(exc))


# ---- Endpoints ---------------------------------------------------------------


@router.post(
    "/insights/{insight_id}/opportunity-score",
    response_model=OpportunityScoreRead,
    status_code=201,
)
def create_score(
    insight_id: UUID,
    body: ScoreCreateRequest | None = Body(default=None),
    db: Session = Depends(get_db),
) -> OpportunityScoreRead:
    insight = _require_insight(db, insight_id)
    weights_payload = None
    if body is not None and body.weights is not None:
        weights_payload = validate_weights(body.weights.model_dump())
    existing_decision = db.scalar(
        select(InsightDecision).where(InsightDecision.insight_id == insight_id)
    )
    business_value = existing_decision.business_value if existing_decision else None
    try:
        outcome = create_opportunity_score(
            db,
            insight,
            weights=weights_payload,
            business_value=business_value,
        )
    except OpportunityValidationError as exc:
        db.rollback()
        raise _resolve_validation_error(exc) from exc
    except OpportunityConfigError as exc:
        db.rollback()
        raise HTTPException(503, str(exc)) from exc
    except (OpportunityLLMConfigError,) as exc:
        db.rollback()
        raise HTTPException(503, str(exc)) from exc
    except (OpportunityLLMProtocolError, OpportunityLLMTransportError) as exc:
        db.rollback()
        raise HTTPException(503, str(exc)) from exc
    db.commit()
    db.refresh(outcome.score)
    return _score_to_read(outcome.score)


@router.get(
    "/insights/{insight_id}/opportunity-score",
    response_model=OpportunityScoreRead,
)
def get_latest_score(insight_id: UUID, db: Session = Depends(get_db)) -> OpportunityScoreRead:
    _require_insight(db, insight_id)
    row = db.scalar(
        select(OpportunityScore)
        .where(OpportunityScore.insight_id == insight_id)
        .order_by(OpportunityScore.created_at.desc(), OpportunityScore.id.desc())
    )
    if row is None:
        raise HTTPException(404, "尚未生成 Opportunity Score")
    return _score_to_read(row)


@router.get(
    "/insights/{insight_id}/opportunity-scores",
    response_model=OpportunityScorePage,
)
def list_scores(
    insight_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> OpportunityScorePage:
    _require_insight(db, insight_id)
    where = OpportunityScore.insight_id == insight_id
    total = db.scalar(select(func.count()).select_from(OpportunityScore).where(where))
    rows = db.scalars(
        select(OpportunityScore)
        .where(where)
        .order_by(OpportunityScore.created_at.desc(), OpportunityScore.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return OpportunityScorePage(
        total=total or 0,
        items=[_score_to_read(r) for r in rows],
        limit=limit,
        offset=offset,
    )


@router.get(
    "/insights/{insight_id}/decision",
    response_model=InsightDecisionRead,
)
def get_decision(insight_id: UUID, db: Session = Depends(get_db)) -> InsightDecisionRead:
    _require_insight(db, insight_id)
    row = db.scalar(select(InsightDecision).where(InsightDecision.insight_id == insight_id))
    if row is None:
        raise HTTPException(404, "尚未建立决策")
    return _decision_to_read(row)


@router.patch(
    "/insights/{insight_id}/decision",
    response_model=InsightDecisionRead,
)
def patch_decision(
    insight_id: UUID,
    body: DecisionPatchRequest = Body(...),
    db: Session = Depends(get_db),
) -> InsightDecisionRead:
    insight = _require_insight(db, insight_id)
    try:
        decision = apply_decision_patch(
            db,
            insight,
            request_id=body.request_id,
            expected_version=body.expected_version,
            action=body.action,
            business_value=body.business_value,
            pm_priority=body.pm_priority,
            override_reason=body.override_reason,
            override_note=body.override_note,
        )
    except OpportunityConcurrencyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OpportunityValidationError as exc:
        raise _resolve_validation_error(exc) from exc
    except OpportunityConfigError as exc:
        raise HTTPException(503, str(exc)) from exc
    return _decision_to_read(decision)


@router.get(
    "/insights/{insight_id}/decision/history",
    response_model=InsightDecisionEventPage,
)
def decision_history(
    insight_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> InsightDecisionEventPage:
    _require_insight(db, insight_id)
    where = InsightDecisionEvent.insight_id == insight_id
    total = db.scalar(select(func.count()).select_from(InsightDecisionEvent).where(where))
    rows = db.scalars(
        select(InsightDecisionEvent)
        .where(where)
        .order_by(
            InsightDecisionEvent.created_at.desc(),
            InsightDecisionEvent.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    ).all()
    return InsightDecisionEventPage(
        total=total or 0,
        items=[_event_to_read(r) for r in rows],
        limit=limit,
        offset=offset,
    )


__all__ = ["router"]
