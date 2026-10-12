from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.common import UTCDateTime


class FeedbackRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    project_id: UUID
    external_id: str
    content: str
    source: str | None
    user_id: str | None
    user_type: str | None
    feedback_created_at: UTCDateTime | None
    created_at: UTCDateTime


class FeedbackPage(BaseModel):
    total: int
    items: list[FeedbackRead]
    limit: int
    offset: int


class ImportResult(BaseModel):
    total: int
    imported: int
    skipped: int
