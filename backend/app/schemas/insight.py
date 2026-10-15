"""Pydantic schemas for the Phase 3 insight endpoints."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import UTCDateTime

# ---- Request bodies ----------------------------------------------------------


class InsightGenerateRequest(BaseModel):
    """Body for ``POST /projects/{project_id}/insights/generate``.

    The clustering run is supplied explicitly so the runner never silently
    picks the most-recent cluster topology.
    """

    model_config = ConfigDict(extra="forbid")

    clustering_run_id: UUID


# ---- Run / item / evidence reads --------------------------------------------


class InsightRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    clustering_run_id: UUID
    classification_run_id: UUID
    status: str
    model: str | None
    prompt_version: str
    input_count: int
    processed_count: int
    success_count: int
    needs_validation_count: int
    failed_count: int
    input_tokens: int
    output_tokens: int
    unknown_token_calls: int
    retry_count: int
    latency_ms: int
    attempt_count: int
    started_at: UTCDateTime | None
    completed_at: UTCDateTime | None
    created_at: UTCDateTime
    error_message: str | None
    input_cluster_external_ids: list[str] | None
    input_member_external_ids: dict[str, list[str]] | None


class InsightRunItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_id: UUID
    cluster_id: UUID
    source_cluster_external_id: str
    status: str
    attempts: int
    last_error: str | None
    feedback_count: int
    evidence_count: int
    created_at: UTCDateTime
    updated_at: UTCDateTime


class InsightRead(BaseModel):
    """Insight row.

    Fields ``title``/``pain_point``/``affected_segment``/``insight``/
    ``recommendation``/``confidence`` may be ``null`` when
    ``status == 'needs_validation'`` because 1–2 evidence does not justify
    a full product claim.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_id: UUID
    project_id: UUID
    cluster_id: UUID
    clustering_run_id: UUID
    title: str | None
    pain_point: str | None
    affected_segment: str | None
    insight: str | None
    recommendation: str | None
    confidence: float | None
    status: str
    feedback_count: int
    evidence_count: int
    source_cluster_external_id: str
    source_member_external_ids: list[str]
    model: str | None
    prompt_version: str
    created_at: UTCDateTime


class FeedbackAnalysisRead(BaseModel):
    """Read shape for the Phase 1 FeedbackAnalysis row paired with one
    evidence entry. Surfaced through the evidence endpoint so the
    frontend can show the deterministic AI classification without a
    second round-trip and without a cross-run guess.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    feedback_id: UUID
    run_id: UUID
    feedback_type: str
    module: str
    intent: str
    pain_point: str
    sentiment: str
    severity: int
    confidence: float
    created_at: UTCDateTime


class InsightEvidenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    insight_id: UUID
    feedback_id: UUID
    feedback_external_id: str
    feedback_content: str
    evidence_type: str
    relevance_score: float
    created_at: UTCDateTime
    feedback_created_at: UTCDateTime | None
    feedback_user_id: str | None
    # Phase 7 work package B1: surface the raw ``feedbacks.source`` value so
    # the frontend can show the same provenance as the Feedback list without
    # a second round-trip. Optional because not every imported feedback
    # carries a source (older imports left the column null).
    feedback_source: str | None = None
    # Phase 7 work package B1: deterministic AI classification for this
    # evidence row, sourced from FeedbackAnalysis(feedback_id,
    # run_id=insight.run.classification_run_id). ``None`` when either
    # the InsightRun is missing or the FeedbackAnalysis was not produced
    # for that exact (feedback_id, classification_run_id) pair. We
    # deliberately do NOT fall back to analyses from other runs — the
    # cross-run pairing is unsupported by the data model and would
    # silently lie about provenance.
    feedback_analysis: FeedbackAnalysisRead | None = None


class InsightPage(BaseModel):
    total: int
    items: list[InsightRead]
    limit: int
    offset: int


class InsightRunPage(BaseModel):
    total: int
    items: list[InsightRunRead]
    limit: int
    offset: int


class InsightEvidencePage(BaseModel):
    total: int
    items: list[InsightEvidenceRead]
    limit: int
    offset: int


# ---- LLM-facing validation schemas (Pydantic for tests of validation logic) --


class EvidenceEntry(BaseModel):
    feedback_id: str = Field(min_length=1, max_length=200)
    evidence_type: str = Field(min_length=1, max_length=32)
    relevance_score: float = Field(ge=0.0, le=1.0)


class InsightPayload(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    pain_point: str = Field(min_length=1, max_length=4000)
    affected_segment: str = Field(min_length=1, max_length=4000)
    insight: str = Field(min_length=1, max_length=4000)
    recommendation: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[EvidenceEntry] = Field(min_length=1)


__all__ = [
    "EvidenceEntry",
    "FeedbackAnalysisRead",
    "InsightEvidencePage",
    "InsightEvidenceRead",
    "InsightGenerateRequest",
    "InsightPage",
    "InsightPayload",
    "InsightRead",
    "InsightRunItemRead",
    "InsightRunPage",
    "InsightRunRead",
]
