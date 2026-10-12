from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import UTCDateTime


class ProjectCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    product_name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10000)


class ProjectRead(ProjectCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    created_at: UTCDateTime
