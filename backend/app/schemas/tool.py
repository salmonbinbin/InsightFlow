"""Phase 8 — Tool Layer Pydantic schemas.

All request bodies use ``extra=forbid`` so a payload carrying
unknown fields is rejected with 422 instead of silently landing on
the row. ``request_id`` is REQUIRED and must be a UUID-shaped
string; the service layer uses ``(project_id, request_id)`` as the
idempotency key for the Prepare call.

The wire DTOs are deliberately small: only fields the UI / audit
surface needs. The persisted ``request_payload`` JSON is exposed
verbatim via :class:`ToolRunRead` so an audit can read exactly
what the adapter saw, but no raw feedback content or credential-
shaped token can land there because the registry's input schema
already rejects them.

The schemas mirror :mod:`app.models.tool` and the DB CHECK
constraints on ``tool_runs`` / ``simulated_issues``.
"""

from __future__ import annotations

import re
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from app.schemas.common import UTCDateTime

ToolRunStatusLiteral = Literal[
    "pending",
    "awaiting_approval",
    "running",
    "completed",
    "failed",
    "cancelled",
]

ToolExecutionModeLiteral = Literal["simulation", "real"]

ToolFailureCategoryLiteral = Literal[
    "transport_timeout",
    "transport_5xx",
    "transport_4xx",
    "validation",
    "not_registered",
    "unauthorised",
    "provider_failure",
    "not_found",
    "conflict",
    "ineligible",
    "cancelled",
    "unknown",
]


# ---- Request bodies --------------------------------------------------------


# ``request_id`` is a UUID-shaped string so the wire format is
# stable across clients; the service layer validates the UUID shape
# before using it as an idempotency key.
_REQUEST_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class ToolPrepareRequest(BaseModel):
    """Body for ``POST /requirements/{requirement_id}/tool-runs``.

    ``tool_name`` + ``tool_action`` MUST resolve to a registered
    descriptor; an unregistered name is rejected with 422 by the
    service layer (mapped from
    :class:`app.tools.registry.ToolNotRegisteredError`). The
    payload is empty for the simulator because the simulator is
    server-driven; ``extra=forbid`` is the gate that stops a
    caller from injecting a ``title`` or ``description`` field.
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, max_length=64)
    tool_action: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=36, max_length=64)
    payload: dict = Field(default_factory=dict)
    agent_run_id: UUID | None = Field(default=None)

    @field_validator("request_id")
    @classmethod
    def _validate_request_id_shape(cls, value: str) -> str:
        if not _REQUEST_ID_RE.match(value):
            raise ValueError(
                "request_id must be a UUID-shaped string "
                "(8-4-4-4-12 hex digits separated by '-')"
            )
        return value


class ToolCancelRequest(BaseModel):
    """Body for ``POST /tool-runs/{tool_run_id}/cancel``.

    Empty by design (``extra=forbid``): cancelling is a single
    unambiguous state transition. Any field the client tries to
    submit is rejected with 422.
    """

    model_config = ConfigDict(extra="forbid")


class ToolApproveRequest(BaseModel):
    """Body for ``POST /tool-runs/{tool_run_id}/approve``.

    Empty by design (``extra=forbid``): the Approve endpoint is a
    single explicit acknowledgement. The brief mandates that the
    human approval is the ONLY way to drive the simulator; any
    extra field the client tries to submit is rejected with 422.
    """

    model_config = ConfigDict(extra="forbid")


# ---- Read DTOs --------------------------------------------------------------


class ToolRunRead(BaseModel):
    """Wire DTO for one :class:`app.models.tool.ToolRun` row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    requirement_id: UUID
    agent_run_id: UUID | None

    tool_name: str
    tool_action: str
    tool_version: str
    request_id: str

    status: str
    execution_mode: str
    external_id: str | None

    request_payload: dict
    response_summary: str | None
    failure_category: str | None

    attempt_count: int
    retry_count: int

    created_at: UTCDateTime
    updated_at: UTCDateTime
    approved_at: UTCDateTime | None
    completed_at: UTCDateTime | None


class ToolRunPage(BaseModel):
    """Paginated tool-run history scoped to one Requirement.

    ``active_run_id`` is the run the UI is most likely to want to
    surface (the awaiting_approval run if one exists, otherwise the
    most recent non-cancelled run). The pointer is computed by a
    separate query so pagination does not silently fall back to
    whichever row happens to be on the current page.
    """

    total: int
    items: list[ToolRunRead]
    limit: int
    offset: int
    active_run_id: UUID | None = None


class SimulatedIssueRead(BaseModel):
    """Wire DTO for one :class:`app.models.tool.SimulatedIssue` row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tool_run_id: UUID
    project_id: UUID
    requirement_id: UUID

    tool_name: str
    tool_action: str
    external_id: str
    simulated: bool

    title: str
    description: str
    priority: str | None
    source_insight_preview: str

    created_at: UTCDateTime


# ---- Tool registry read ----------------------------------------------------


class ToolDescriptorRead(BaseModel):
    """Wire DTO for one :class:`app.tools.registry.ToolDescriptor`.

    The descriptor is rendered for the UI so a PM can confirm
    which tools exist before triggering a prepare call. ``adapter``
    is intentionally NOT exposed — only metadata is shipped to
    the client.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=2000)
    capability_kind: Literal["read", "write", "simulation_only"]
    execution_mode: ToolExecutionModeLiteral
    idempotent: bool
    supports_bounded_retry: bool


class ToolRegistryRead(BaseModel):
    """Wire DTO for the full static registry."""

    model_config = ConfigDict(extra="forbid")

    tools: list[ToolDescriptorRead]


__all__ = [
    "SimulatedIssueRead",
    "ToolApproveRequest",
    "ToolCancelRequest",
    "ToolDescriptorRead",
    "ToolExecutionModeLiteral",
    "ToolFailureCategoryLiteral",
    "ToolPrepareRequest",
    "ToolRegistryRead",
    "ToolRunPage",
    "ToolRunRead",
    "ToolRunStatusLiteral",
]