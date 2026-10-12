from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utc_now


class Feedback(Base):
    __tablename__ = "feedbacks"
    __table_args__ = (
        UniqueConstraint("project_id", "external_id", name="uq_feedback_project_external"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(100))
    user_id: Mapped[str | None] = mapped_column(String(200))
    user_type: Mapped[str | None] = mapped_column(String(100))
    feedback_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
