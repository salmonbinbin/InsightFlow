"""Pydantic schemas for cluster endpoints."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.common import UTCDateTime


class ClusterRunRead(BaseModel):
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
    cluster_config: dict | None = None


class ClusterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    project_id: UUID
    run_id: UUID
    source: str
    parent_cluster_id: UUID | None
    name: str | None
    summary: str | None
    feedback_count: int
    unique_user_count: int
    average_severity: float | None
    negative_ratio: float | None
    confidence: float | None
    embedding_model: str
    distance_threshold: float
    linkage: str
    created_at: UTCDateTime


class ClusterMemberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    cluster_id: UUID
    feedback_analysis_id: UUID
    similarity_score: float
    created_at: UTCDateTime


class ClusterReviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    run_id: UUID
    cluster_id: UUID
    action: str
    reason: str | None
    prompt_version: str
    created_at: UTCDateTime


class ClusterPage(BaseModel):
    total: int
    items: list[ClusterRead]
    limit: int
    offset: int


class ClusterMemberPage(BaseModel):
    total: int
    items: list[ClusterMemberRead]
    limit: int
    offset: int
