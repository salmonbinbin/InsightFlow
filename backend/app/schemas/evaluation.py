"""Pydantic schemas for Phase 6 evaluation endpoints.

Endpoints exposed by :mod:`app.api.evaluations`:

* ``POST /evaluations`` — trigger an evaluation run that scores an
  existing Phase 1 ``classification`` or Phase 2 ``clustering`` run
  against the demo dataset snapshot. The server ALWAYS loads the
  dataset internally (``load_demo_dataset()``) so callers never
  inject data.
* ``GET /evaluations/{id}`` — read the run row.
* ``GET /evaluations/{id}/metrics`` — read the metric rows.
* ``GET /evaluations/{id}/cases`` — read the per-case rows (raw
  feedback text deliberately omitted by default — only hashes).

The dataset labels are always marked
``synthetic_reference_labels_unreviewed_no_human_review`` so the
response MUST surface that fact verbatim (``dataset_label_status``)
so the caller can never silently treat the demo dataset as
ground-truth.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import UTCDateTime

NOT_MEASURED_DISPLAY: str = "NOT MEASURED"


EvaluationTypeLiteral = Literal["classification_metrics", "cluster_metrics"]


class EvaluationCreateRequest(BaseModel):
    """Body for ``POST /evaluations``.

    The server loads the demo dataset internally — the caller does NOT
    supply a dataset identifier or any sensitive content. ``extra=forbid``
    rejects typos so a silently-ignored field cannot mask a client bug.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    source_run_id: UUID
    evaluation_type: EvaluationTypeLiteral = Field(
        description="'classification_metrics' 评估 Phase 1 分类运行；"
        "'cluster_metrics' 评估 Phase 2 聚类运行。"
    )


class EvaluationRunRead(BaseModel):
    """Read shape for one :class:`EvaluationRun` row.

    The raw ``notes`` field is preserved verbatim. Callers may parse
    ``source_run_id=…`` / ``source_run_type=…`` etc. out of it for
    convenience; the API itself does not pre-parse notes so the
    contract stays "notes is the source of truth".
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID

    evaluation_type: str
    stage: str
    status: str

    dataset_name: str
    dataset_version: str
    dataset_content_hash: str
    dataset_provenance: str
    dataset_split: str
    dataset_label_status: str

    model: str | None
    prompt_version: str | None
    pipeline_version: str | None
    calculation_version: str

    input_count: int
    processed_count: int
    success_count: int
    failed_count: int

    input_tokens: int | None
    output_tokens: int | None
    unknown_token_calls: int
    latency_ms: int | None
    total_cost_usd: float | None

    attempt_count: int
    retry_count: int

    started_at: UTCDateTime | None
    completed_at: UTCDateTime | None
    created_at: UTCDateTime

    error_message: str | None
    notes: str | None


class EvaluationRunCreateRead(BaseModel):
    """Response shape for ``POST /evaluations``.

    Mirrors :class:`EvaluationRunRead` plus an explicit
    ``dataset_source`` marker so the caller can tell, without
    parsing notes, that the dataset was loaded server-side and is
    the synthetic + unreviewed demo snapshot.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    evaluation_type: str
    stage: str
    status: str

    dataset_name: str
    dataset_version: str
    dataset_content_hash: str
    dataset_provenance: str
    dataset_split: str
    dataset_label_status: str

    input_count: int
    processed_count: int
    success_count: int
    failed_count: int

    calculation_version: str
    started_at: UTCDateTime | None
    completed_at: UTCDateTime | None
    created_at: UTCDateTime

    error_message: str | None
    notes: str | None
    dataset_source: Literal["demo_synthetic_unreviewed"] = "demo_synthetic_unreviewed"


class EvaluationMetricRead(BaseModel):
    """One :class:`EvaluationMetric` row.

    ``value_display`` is a stable, frontend-friendly rendering of
    ``value`` so a ``None`` (= NOT_MEASURED) is rendered as
    ``NOT MEASURED`` rather than as the silent ``null`` token. The
    numeric ``value`` is still present for analytics consumers that
    want to round / chart the number directly.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_id: UUID
    project_id: UUID

    stage: str
    metric_name: str
    metric_kind: str

    sample_count: int
    value: float | None
    value_display: str
    details: dict

    created_at: UTCDateTime


class EvaluationMetricPage(BaseModel):
    total: int
    items: list[EvaluationMetricRead]
    limit: int
    offset: int


class EvaluationCaseRead(BaseModel):
    """One :class:`EvaluationCase` row (raw text deliberately omitted).

    Only SHA-256 fingerprints of ``input`` / ``expected`` / ``actual``
    are returned so a caller reading the audit trail never sees raw
    feedback content. ``error_type`` and ``eval_source`` describe
    how the case was classified; ``failed_stage`` is derived from
    ``stage`` + ``passed`` for dashboard convenience.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_id: UUID
    project_id: UUID

    case_id: str
    stage: str

    input_hash: str | None
    expected_hash: str | None
    actual_hash: str | None

    passed: bool | None
    error_type: str | None
    eval_source: str
    notes: str | None
    created_at: UTCDateTime

    failed_stage: str | None


class EvaluationCasePage(BaseModel):
    total: int
    items: list[EvaluationCaseRead]
    limit: int
    offset: int


class ProjectEvaluationRunPage(BaseModel):
    """Paginated EvaluationRun list scoped to one project.

    Phase 7 work package D1 — the project-level EvaluationRun index
    that survives a page refresh on ``/evaluation``. The actual
    metric / case measurements are still served by
    ``/evaluations/{id}/metrics`` and ``/evaluations/{id}/cases``;
    this list is the lightweight project index used to render the
    run history sidebar.

    Sort is stable: ``created_at DESC, id DESC``. The ``id``
    tiebreaker matters when two runs share the same ``created_at``
    (possible when one transaction commits several runs together);
    without it, ``limit`` / ``offset`` slicing could miss or
    duplicate rows between pages.

    ``limit`` is validated ``1..200`` and ``offset`` ``>=0`` by the
    route handler — Pydantic does NOT re-validate these because
    the schema is shared between the route layer and any future
    internal callers that may legitimately pass a different
    pagination window.
    """

    total: int
    items: list[EvaluationRunRead]
    limit: int
    offset: int


__all__ = [
    "NOT_MEASURED_DISPLAY",
    "EvaluationCasePage",
    "EvaluationCaseRead",
    "EvaluationCreateRequest",
    "EvaluationMetricPage",
    "EvaluationMetricRead",
    "EvaluationRunCreateRead",
    "EvaluationRunRead",
    "EvaluationTypeLiteral",
    "ProjectEvaluationRunPage",
]
