"""Pydantic schemas for analysis endpoints."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.common import UTCDateTime

# Phase 7 Analysis history (D package) — only the two ``run_type``
# values the runner actually emits are accepted on the read-side
# filter. Adding a new value here is the deliberate choke-point
# the brief authorises; the open-string ``run_type`` field on
# ``AnalysisRunRead`` is preserved for forward-compat with rows
# the runner may grow into.
AnalysisRunTypeLiteral = Literal["classification", "clustering"]


class AnalysisRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    project_id: UUID
    run_type: str
    model: str | None
    prompt_version: str
    status: str
    input_count: int
    processed_count: int
    success_count: int
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
    input_feedback_external_ids: list[str] | None


class AnalysisRunPage(BaseModel):
    total: int
    items: list[AnalysisRunRead]
    limit: int
    offset: int


class FeedbackAnalysisRead(BaseModel):
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
    semantic_text: str | None
    created_at: UTCDateTime


class FeedbackAnalysisPage(BaseModel):
    total: int
    items: list[FeedbackAnalysisRead]
    limit: int
    offset: int


class RunFailureRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    run_id: UUID
    feedback_id: UUID
    attempts: int
    last_error: str | None
    created_at: UTCDateTime
    updated_at: UTCDateTime
