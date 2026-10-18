"""Pydantic schemas for the Phase 8 Research Agent endpoints.

All field names and length bounds mirror the server-side validators
in :mod:`app.services.research_service` and the DB CHECK constraints
on ``research_runs`` / ``research_sources`` / ``research_findings``.
The schemas use ``extra="forbid"`` everywhere a user can post a body,
so a request carrying unknown fields is rejected as 422 instead of
silently accepting the noise.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import UTCDateTime

# ---- Request body ----------------------------------------------------------


class ResearchRunRequest(BaseModel):
    """Body for ``POST /insights/{insight_id}/research``.

    The user supplies ONLY the research question and an optional scope
    bound. Every other input — Insight metadata, representative
    Evidence summaries, PM Decision snapshot — is assembled server-side
    from existing rows. ``prompt_version`` defaults to the active prompt
    version (``research-agent-v1``); an explicit value lets the future
    supervisor pin a historical prompt without code changes.
    """

    model_config = ConfigDict(extra="forbid")

    research_question: str = Field(
        min_length=1,
        max_length=500,
        description=(
            "User-supplied research question. Length-bounded to match the "
            "DB CHECK constraint and the LLM prompt contract."
        ),
    )
    scope: str | None = Field(
        default=None,
        max_length=2000,
        description=(
            "Optional scope hint (markets, time horizon, personas). "
            "Truncated to 2000 chars to match the DB CHECK constraint."
        ),
    )
    prompt_version: Literal["research-agent-v6", "research-agent-v7"] | None = Field(
        default=None,
        max_length=100,
        description=(
            "Optional pin to a specific prompt version. Defaults to the "
            "active prompt version recorded on the run."
        ),
    )


# ---- Read DTOs -------------------------------------------------------------


class ResearchEvidenceSummaryRead(BaseModel):
    """One evidence summary line attached to an ``InternalContext``.

    ``external_id`` matches the entry at the same position in
    ``ResearchInternalContextRead.evidence_external_ids``; ``summary``
    is a bounded excerpt of the underlying ``Feedback.content``
    (per-entry cap 400 chars, total cap 2000 chars across the
    snapshot). The raw feedback body is never echoed here.
    """

    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=400)


class ResearchInternalContextRead(BaseModel):
    """Read shape for ``research_runs.internal_context``.

    The snapshot is plain data: it carries no secrets and never echoes
    raw feedback content. The internal context is exposed verbatim so a
    reviewer can confirm what the LLM saw during the run.
    """

    model_config = ConfigDict(extra="forbid")

    insight_title: str = Field(min_length=1, max_length=200)
    insight_pain_point: str = Field(min_length=1, max_length=4000)
    insight_affected_segment: str = Field(min_length=1, max_length=4000)
    insight_recommendation: str = Field(min_length=1, max_length=4000)
    evidence_external_ids: list[str] = Field(default_factory=list, max_length=5)
    evidence_summaries: list[ResearchEvidenceSummaryRead] = Field(
        default_factory=list, max_length=5
    )
    decision_status: str | None = Field(default=None, max_length=32)
    decision_priority: str | None = Field(default=None, max_length=8)
    research_question: str = Field(min_length=1, max_length=500)
    scope: str | None = Field(default=None, max_length=2000)


class ResearchSourceRefRead(BaseModel):
    """A single ``source_refs`` entry that points at a persisted source.

    The provider_source_id is the opaque identifier returned by the
    search provider; the URL / title are echoed only so the frontend
    can render the citation without a follow-up fetch.
    """

    model_config = ConfigDict(extra="forbid")

    provider_source_id: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=500)


class ResearchFindingRead(BaseModel):
    """One structured research finding produced by the LLM.

    The ``status`` / ``source_refs`` invariant is enforced at the DB
    layer (``ck_research_findings_status_provided_invariant``): a
    confirmed finding carries one or more ``source_refs``; a
    not_verified finding carries none. The schema mirrors that
    contract so a Frontend TypeScript type matches the wire payload.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    run_id: UUID
    category: str = Field(
        description="Finding category enum: summary / competitor / market_pattern / opportunity / risk."
    )
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    status: str = Field(description="Finding provenance: confirmed or not_verified.")
    source_refs: list[ResearchSourceRefRead] | None = None
    claim_kind: str | None = None
    supporting_finding_ids: list[UUID] | None = None
    evidence_location: dict | None = None
    created_at: UTCDateTime


class ResearchSourceRead(BaseModel):
    """One persisted search hit returned by the Research Tool Provider."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_id: UUID
    provider: str = Field(min_length=1, max_length=64)
    provider_source_id: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=500)
    domain: str = Field(min_length=1, max_length=253)
    snippet: str | None = None
    retrieved_at: UTCDateTime
    created_at: UTCDateTime


class ResearchRunRead(BaseModel):
    """Read shape for a ResearchRun row.

    ``internal_context_hash`` is surfaced so a retry or audit can verify
    the supplied internal context matched the persisted snapshot at run
    time. ``error_message`` is a redacted summary — the persisted form
    never carries raw provider text, API keys or upstream bodies.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    insight_id: UUID
    classification_run_id: UUID
    clustering_run_id: UUID
    insight_decision_id: UUID | None
    research_question: str = Field(min_length=1, max_length=500)
    scope: str | None = Field(default=None, max_length=2000)
    provider: str = Field(min_length=1, max_length=64)
    provider_query: str = Field(min_length=1, max_length=500)
    model: str | None = Field(default=None, max_length=100)
    prompt_version: str = Field(min_length=1, max_length=100)
    status: str
    started_at: UTCDateTime | None
    completed_at: UTCDateTime | None
    created_at: UTCDateTime
    error_message: str | None
    internal_context: ResearchInternalContextRead
    internal_context_hash: str = Field(min_length=1, max_length=64)
    task_contract: dict | None = None
    coverage_report: dict | None = None
    execution_trace: dict | None = None
    input_count: int
    processed_count: int
    source_count: int
    finding_count: int
    input_tokens: int
    output_tokens: int
    unknown_token_calls: int
    latency_ms: int
    attempt_count: int
    retry_count: int


class ResearchRunListPage(BaseModel):
    total: int
    items: list[ResearchRunRead]
    limit: int
    offset: int


class ResearchSourceListPage(BaseModel):
    total: int
    items: list[ResearchSourceRead]
    limit: int
    offset: int


class ResearchFindingListPage(BaseModel):
    total: int
    items: list[ResearchFindingRead]
    limit: int
    offset: int


__all__ = [
    "ResearchEvidenceSummaryRead",
    "ResearchFindingListPage",
    "ResearchFindingRead",
    "ResearchInternalContextRead",
    "ResearchRunListPage",
    "ResearchRunRead",
    "ResearchRunRequest",
    "ResearchSourceListPage",
    "ResearchSourceRead",
    "ResearchSourceRefRead",
]
