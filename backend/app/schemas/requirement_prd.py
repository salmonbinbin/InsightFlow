"""Phase 5 — Requirement + PRD Pydantic schemas.

The schemas mirror the 13-section PRD contract from the Phase 5 brief
and the requirement contract documented in :mod:`app.agents.prompts.
requirement_generation_v1`. They are deliberately strict
(``extra=forbid`` + length / type bounds) so a malformed payload from
the LLM client or the PM UI cannot reach persistence.

Grounding metadata (``evidence_ids`` / ``sources``) is exposed on
every claim-shaped field. The validator layer in
:mod:`app.services.requirement_prd` enforces that PM cannot silently
strip evidence references off a structured claim.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import UTCDateTime

# ---- Literal types ----------------------------------------------------------

RequirementStatusLiteral = Literal[
    "draft",
    "reviewing",
    "approved",
    "rejected",
    "archived",
]

PRDStatusLiteral = Literal[
    "draft",
    "reviewing",
    "accepted",
    "archived",
]

RiskCategoryLiteral = Literal["known", "speculative"]

MetricTypeLiteral = Literal["metric_definition", "baseline_only"]

EdgeCaseCategoryLiteral = Literal[
    "empty_state",
    "failure",
    "retry",
    "partial_success",
    "invalid_input",
    "limit_exceeded",
]

Phase4DecisionStatusLiteral = Literal[
    "pending_review",
    "accepted",
    "overridden",
    "deferred",
    "rejected",
]

InsightStatusLiteral = Literal["draft", "needs_validation"]

PriorityLiteral = Literal["P0", "P1", "P2"]


# ---- Inner structured items -------------------------------------------------


class EvidenceReference(BaseModel):
    """One row inside ``requirements.evidence_references``.

    ``external_id`` must come from the upstream Evidence list supplied
    by the LLM prompt; the service layer rejects IDs that weren't in
    the input payload so PM cannot retain hallucinated citations.
    """

    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=200)
    usage: str = Field(min_length=1, max_length=500)


class SuccessMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    type: MetricTypeLiteral
    sources: list[str] = Field(default_factory=list, max_length=16)
    note: str = Field(default="", max_length=500)


class Risk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: RiskCategoryLiteral
    description: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=16)
    mitigation: str = Field(default="", max_length=2000)


class FunctionalRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=32)


class EdgeCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: EdgeCaseCategoryLiteral
    description: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=32)


class AcceptanceCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    verifiable: bool = Field(strict=True)
    note: str = Field(default="", max_length=500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=32)


class EvidenceBackedSection(BaseModel):
    """A PRD section whose ``text`` must be backed by evidence IDs.

    ``evidence_ids`` is required (not defaulted) so the schema rejects
    any LLM or PM payload that silently drops the provenance list.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(min_length=1, max_length=32)


class ProposedSolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)
    is_proposed: bool
    evidence_ids: list[str] = Field(min_length=1, max_length=32)


class SuccessMetricBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    type: MetricTypeLiteral
    note: str = Field(default="", max_length=500)
    sources: list[str] = Field(default_factory=list, max_length=16)


class PRDRisk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: RiskCategoryLiteral
    description: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=16)
    mitigation: str = Field(default="", max_length=2000)


class MVPScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    in_scope: str = Field(min_length=1, max_length=4000)
    out_of_scope: str = Field(min_length=1, max_length=4000)


class PRDContent(BaseModel):
    """Full 13-section PRD payload persisted as JSON."""

    model_config = ConfigDict(extra="forbid")

    background: EvidenceBackedSection
    problem: EvidenceBackedSection
    target_user: EvidenceBackedSection
    user_story: EvidenceBackedSection
    product_goal: EvidenceBackedSection
    proposed_solution: ProposedSolution
    functional_requirements: list[FunctionalRequirement] = Field(min_length=1, max_length=64)
    user_flow: EvidenceBackedSection
    edge_cases: list[EdgeCase] = Field(min_length=1, max_length=64)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1, max_length=64)
    success_metrics: list[SuccessMetricBlock] = Field(default_factory=list, max_length=32)
    risks: list[PRDRisk] = Field(default_factory=list, max_length=32)
    mvp_scope: MVPScope
    evidence_sources: list[str] = Field(min_length=1, max_length=64)
    evidence_ids: list[str] = Field(min_length=1, max_length=32)


# ---- Request bodies ---------------------------------------------------------


class RequirementCreateRequest(BaseModel):
    """Body for ``POST /insights/{insight_id}/requirement``.

    Empty body is allowed — the service layer triggers the AI client
    when the payload cannot be sourced from existing draft state. When
    ``content`` is supplied, the AI client is bypassed and the row is
    marked as PM-authored.
    """

    model_config = ConfigDict(extra="forbid")

    content: "PMRequirementContent | None" = None


class PMRequirementContent(BaseModel):
    """Editable content produced directly by the PM (no AI call)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    problem: str = Field(min_length=1, max_length=4000)
    target_user: str = Field(min_length=1, max_length=4000)
    user_story: str = Field(min_length=1, max_length=4000)
    solution_hypothesis: str = Field(min_length=1, max_length=4000)
    success_metrics: list[SuccessMetric] = Field(default_factory=list, max_length=32)
    risks: list[Risk] = Field(default_factory=list, max_length=32)
    evidence_references: list[EvidenceReference] = Field(min_length=1, max_length=32)


class RequirementPatchRequest(BaseModel):
    """Body for ``PATCH /requirements/{requirement_id}``.

    Allows PM to edit content fields and transition status. PM is the
    sole owner of status transitions to ``reviewing`` / ``approved`` /
    ``rejected`` / ``archived`` — the brief forbids the AI from mutating
    the Requirement state machine.
    """

    model_config = ConfigDict(extra="forbid")

    status: RequirementStatusLiteral | None = None
    content: PMRequirementContent | None = None


class PRDCreateRequest(BaseModel):
    """Body for ``POST /requirements/{requirement_id}/prd``.

    The body is empty by default — the AI generates the 13-section
    PRD. ``content`` is the escape hatch for PM-authored PRDs (e.g.
    drafts that were re-written by hand). The brief forbids callers
    from injecting metrics or revenue targets that contradict
    grounding — the service layer validates ``success_metrics`` items
    with the same rule the AI client follows.
    """

    model_config = ConfigDict(extra="forbid")

    content: PRDContent | None = None


class PRDPatchRequest(BaseModel):
    """Body for ``PATCH /prds/{prd_id}``.

    PM may:

    * Transition ``draft`` → ``reviewing`` → ``accepted`` (only via the
      dedicated ``POST /prds/{prd_id}/accept`` endpoint) → ``archived``.
    * Edit the 13-section content of a still-active PRD
      (``status`` in ``{draft, reviewing}``). The accepted version is
      immutable — a follow-up edit must create a new version via the
      ``POST`` endpoint, which keeps history auditable.
    """

    model_config = ConfigDict(extra="forbid")

    status: PRDStatusLiteral | None = None
    content: "PRDContent | None" = None


class PRDAcceptRequest(BaseModel):
    """Body for ``POST /prds/{prd_id}/accept``.

    Phase 5 has no user system — the audit trail simply cannot be
    trusted if the client is allowed to claim an arbitrary identity.
    The request body is therefore empty by design; any field
    submitted (``extra=forbid``) is rejected with 422. ``accepted_by``
    on the persisted row stays ``NULL`` until a real auth layer is
    added.
    """

    model_config = ConfigDict(extra="forbid")


# ---- Read shapes ------------------------------------------------------------


class EvidenceReferenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    external_id: str
    usage: str


class SuccessMetricRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    type: str
    sources: list[str] = Field(default_factory=list)
    note: str = ""


class RiskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    category: str
    description: str
    evidence_ids: list[str] = Field(default_factory=list)
    mitigation: str = ""


class RequirementRead(BaseModel):
    """Read shape for one Requirement row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    insight_id: UUID
    decision_id: UUID
    score_id: UUID | None

    insight_status_snapshot: str
    decision_status_snapshot: str
    ai_priority_snapshot: str | None
    pm_priority: str
    pm_override_reason_snapshot: str | None
    source_snapshot_hash: str

    name: str
    problem: str
    target_user: str
    user_story: str
    solution_hypothesis: str
    success_metrics: list[SuccessMetricRead]
    risks: list[RiskRead]
    evidence_references: list[EvidenceReferenceRead]

    status: str
    model: str | None
    prompt_version: str
    input_tokens: int
    output_tokens: int
    unknown_token_calls: int
    latency_ms: int
    attempt_count: int
    retry_count: int
    next_version: int

    created_at: UTCDateTime
    updated_at: UTCDateTime


class RequirementPage(BaseModel):
    total: int
    items: list[RequirementRead]
    limit: int
    offset: int


class PRDContentRead(BaseModel):
    """Read shape for ``prds.content`` JSON."""

    model_config = ConfigDict(from_attributes=True)

    background: EvidenceBackedSection
    problem: EvidenceBackedSection
    target_user: EvidenceBackedSection
    user_story: EvidenceBackedSection
    product_goal: EvidenceBackedSection
    proposed_solution: ProposedSolution
    functional_requirements: list[FunctionalRequirement]
    user_flow: EvidenceBackedSection
    edge_cases: list[EdgeCase]
    acceptance_criteria: list[AcceptanceCriterion]
    success_metrics: list[SuccessMetricBlock]
    risks: list[PRDRisk]
    mvp_scope: MVPScope
    evidence_sources: list[str]
    evidence_ids: list[str]


class GroundingWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=64)
    section: str | None = Field(default=None, max_length=64)
    message: str = Field(min_length=1, max_length=500)
    evidence_id: str | None = Field(default=None, max_length=200)


class PRDRead(BaseModel):
    """Read shape for one PRD version."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    requirement_id: UUID
    project_id: UUID
    insight_id: UUID

    version: int
    status: str
    content: PRDContentRead
    content_hash: str

    is_ai_generated_draft: bool
    grounding_warnings: list[GroundingWarning]

    model: str | None
    prompt_version: str
    input_tokens: int
    output_tokens: int
    unknown_token_calls: int
    latency_ms: int
    attempt_count: int
    retry_count: int

    accepted_at: UTCDateTime | None
    accepted_by: str | None

    created_at: UTCDateTime
    updated_at: UTCDateTime


class PRDPage(BaseModel):
    """Paginated PRD history plus a pointer to the active version.

    ``active_prd_id`` / ``active_version`` always reflect the
    highest-versioned row in ``items`` (or ``None`` when the
    requirement has not yet generated a PRD). Consumers do not have
    to infer the active PRD from sort order — the field is stable
    regardless of pagination.
    """

    total: int
    items: list[PRDRead]
    limit: int
    offset: int
    active_prd_id: UUID | None = None
    active_version: int | None = None


class ProjectPRDPage(BaseModel):
    """Paginated PRD list scoped to one project.

    Phase 7 work package D1 — the project-level PRD index that
    survives a page refresh on ``/prds``. Every PRD version from
    every requirement inside ``project_id`` is returned (no
    latest-per-requirement projection), ordered ``version DESC,
    id DESC``. The frontend groups by ``requirement_id`` to render
    the version timeline; collapsing to one row per requirement
    would silently drop earlier drafts and accepted versions,
    breaking the audit trail.

    ``limit`` is validated ``1..200`` and ``offset`` ``>=0`` by the
    route handler — Pydantic does NOT re-validate these because
    the schema is shared between the route layer and any future
    internal callers that may legitimately pass a different
    pagination window.
    """

    total: int
    items: list[PRDRead]
    limit: int
    offset: int


__all__ = [
    "AcceptanceCriterion",
    "EdgeCase",
    "EdgeCaseCategoryLiteral",
    "EvidenceBackedSection",
    "EvidenceReference",
    "EvidenceReferenceRead",
    "FunctionalRequirement",
    "GroundingWarning",
    "MVPScope",
    "MetricTypeLiteral",
    "PMRequirementContent",
    "PRDContent",
    "PRDContentRead",
    "PRDCreateRequest",
    "PRDPatchRequest",
    "PRDAcceptRequest",
    "PRDPage",
    "PRDRead",
    "ProjectPRDPage",
    "PRDStatusLiteral",
    "PRDRisk",
    "PriorityLiteral",
    "ProposedSolution",
    "RequirementCreateRequest",
    "RequirementPage",
    "RequirementPatchRequest",
    "RequirementRead",
    "RequirementStatusLiteral",
    "Risk",
    "RiskCategoryLiteral",
    "RiskRead",
    "SuccessMetric",
    "SuccessMetricBlock",
    "SuccessMetricRead",
    "Phase4DecisionStatusLiteral",
    "InsightStatusLiteral",
]
