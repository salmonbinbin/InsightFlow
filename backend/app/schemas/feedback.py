from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.analysis import FeedbackAnalysisRead
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


# Phase 7 Feedback Detail (E package) — read-only per-feedback
# analysis selection. ``selection.mode == "explicit"`` when the
# caller passed ``?run_id=`` and we returned (or didn't find) an
# analysis row for that exact run; ``"latest"`` when we picked
# the most recent classification run owned by the project that
# had a ``FeedbackAnalysis`` for this feedback. The rule string
# documents the deterministic ordering so a caller never has to
# guess whether we silently fell back across projects or to a
# clustering run.
class FeedbackAnalysisSelectionRead(BaseModel):
    mode: Literal["explicit", "latest"]
    selected_run_id: UUID | None
    selection_rule: str


class FeedbackAnalysisEnvelopeRead(BaseModel):
    feedback_id: UUID
    analysis: FeedbackAnalysisRead | None
    selection: FeedbackAnalysisSelectionRead
