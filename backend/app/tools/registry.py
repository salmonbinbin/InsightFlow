"""Phase 8 — Tool Registry (static metadata gate).

The registry holds one :class:`ToolDescriptor` per ``(name, action)``
pair. Every call to :func:`app.services.tool_service.prepare_tool_run`
or :func:`app.services.tool_service.approve_tool_run` goes through
:meth:`ToolRegistry.validate_inputs` so an unregistered tool name
cannot leak into persistence. Unknown fields on the payload are
rejected by the per-descriptor Pydantic schema (``extra="forbid"``).

Two descriptors ship by default:

* ``web_research.search`` — read-only capability metadata that
  documents the search call the Phase 8 Research API exercises. The
  descriptor's :attr:`ToolDescriptor.execution_mode` is ``real`` and
  its input schema matches the Phase 8 Research request body so the
  registry can validate a future caller that wants to invoke the
  search capability through a generic tool-call surface.
* ``simulated_issue_tracker.create_issue`` — a ``simulation_only``
  write capability. The descriptor's adapter is
  :class:`app.tools.adapters.SimulatedIssueTrackerAdapter`; the
  adapter is a clearly-marked simulator that returns a ``SIM-...``
  external id and a ``simulated=true`` flag so the UI can never
  mistake it for a real Jira / Notion / Feishu write.

The registry is intentionally static and process-wide: there is no
runtime registration endpoint and no auto-discovery from MCP / plugin
manifests. ``bootstrap_default_registry`` is called once at process
start by :func:`get_default_registry`.
"""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

# ---- Enumerations ----------------------------------------------------------


class CapabilityKind(str, Enum):
    """What kind of side effect the tool produces."""

    READ = "read"
    WRITE = "write"
    SIMULATION_ONLY = "simulation_only"


class ExecutionMode(str, Enum):
    """Where the tool actually executes.

    ``simulation`` is the only safe value for the Phase 8 write path —
    the real Jira / Notion / Feishu integration is intentionally NOT
    wired up because we have no verified write credentials in this
    environment. ``real`` is reserved for capabilities the registry
    only documents (e.g. ``web_research.search``).
    """

    SIMULATION = "simulation"
    REAL = "real"


# ---- Errors -----------------------------------------------------------------


class ToolNotRegisteredError(KeyError):
    """Raised when a caller asks for an unregistered tool name."""

    def __init__(self, name: str, action: str) -> None:
        self.name = name
        self.action = action
        super().__init__(f"tool '{name}/{action}' is not registered")


class ToolInputValidationError(ValueError):
    """Raised when a payload fails the per-tool Pydantic schema."""

    def __init__(self, name: str, action: str, message: str) -> None:
        self.name = name
        self.action = action
        super().__init__(f"tool '{name}/{action}' input invalid: {message}")


# ---- Descriptor -------------------------------------------------------------


class ToolDescriptor:
    """Metadata for one registered tool.

    ``input_schema`` is the per-tool Pydantic model; ``output_summary_schema``
    documents the bounded response_summary shape but is NOT enforced by
    the adapter (the adapter stores its own serialised summary so the
    registry stays free of ORM imports). ``idempotent`` +
    ``supports_bounded_retry`` together gate the simulator's retry
    policy: only tools that explicitly opt in to bounded retry may
    see a retry on transient timeout / 5xx.
    """

    __slots__ = (
        "name",
        "action",
        "version",
        "description",
        "capability_kind",
        "execution_mode",
        "input_schema",
        "output_summary_schema",
        "idempotent",
        "supports_bounded_retry",
        "adapter_factory",
    )

    def __init__(
        self,
        *,
        name: str,
        action: str,
        version: str,
        description: str,
        capability_kind: CapabilityKind,
        execution_mode: ExecutionMode,
        input_schema: type[BaseModel],
        output_summary_schema: type[BaseModel],
        idempotent: bool = False,
        supports_bounded_retry: bool = False,
        adapter_factory: "Any | None" = None,
    ) -> None:
        if not (1 <= len(name) <= 64):
            raise ValueError(f"tool name length must be within [1, 64]; got {name!r}")
        if not (1 <= len(action) <= 64):
            raise ValueError(f"tool action length must be within [1, 64]; got {action!r}")
        if not (1 <= len(version) <= 64):
            raise ValueError(f"tool version length must be within [1, 64]; got {version!r}")
        self.name = name
        self.action = action
        self.version = version
        self.description = description
        self.capability_kind = capability_kind
        self.execution_mode = execution_mode
        self.input_schema = input_schema
        self.output_summary_schema = output_summary_schema
        self.idempotent = idempotent
        self.supports_bounded_retry = supports_bounded_retry
        self.adapter_factory = adapter_factory

    @property
    def key(self) -> tuple[str, str]:
        return (self.name, self.action)

    def is_write(self) -> bool:
        return self.capability_kind in (
            CapabilityKind.WRITE,
            CapabilityKind.SIMULATION_ONLY,
        )


# ---- Default input schemas --------------------------------------------------


class WebResearchSearchInput(BaseModel):
    """Pydantic schema for ``web_research.search`` inputs.

    Mirrors :class:`app.schemas.research.ResearchRunRequest` so the
    registry rejects unknown / oversize fields consistently with the
    Research API surface.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    scope: str | None = Field(default=None, max_length=2000)
    max_results: int | None = Field(default=None, ge=1, le=10)


class WebResearchSearchOutputSummary(BaseModel):
    """Documented output_summary shape for ``web_research.search``.

    Not enforced — the existing Research API persists its own rows;
    this exists so a generic tool-call surface can render a uniform
    summary block.
    """

    model_config = ConfigDict(extra="forbid")

    source_count: int = Field(ge=0, le=10)
    finding_count: int = Field(ge=0, le=64)
    provider: str = Field(min_length=1, max_length=64)


class SimulatedIssueCreateInput(BaseModel):
    """Pydantic schema for ``simulated_issue_tracker.create_issue`` inputs.

    The simulator is server-driven: the route handler assembles the
    Title / Description / Priority / Source Insight preview from the
    Requirement + the source Insight's PM priority, so the wire
    payload carries no user-supplied issue body. ``extra=forbid`` is
    the gate: a payload carrying a user-injected ``title`` or
    ``description`` field is rejected with 422 before persistence.
    """

    model_config = ConfigDict(extra="forbid")


class SimulatedIssueCreateOutputSummary(BaseModel):
    """Documented output_summary shape for the simulator."""

    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=64)
    simulated: bool
    tool_name: str = Field(min_length=1, max_length=64)
    tool_action: str = Field(min_length=1, max_length=64)


class SimulatedIssueSnapshot(BaseModel):
    """Frozen Snapshot stored on ``ToolRun.request_payload`` at Prepare time.

    The simulator is server-driven: the user's wire payload is always
    ``{}`` (validated by :class:`SimulatedIssueCreateInput`), so the
    audit-able inputs the adapter will see are produced and sanitised
    here. The snapshot is built from the Requirement + the source
    Insight the moment the run is created; any subsequent edit to the
    Requirement CANNOT silently rewrite the snapshot, so what the user
    approves is exactly what the simulator writes.

    Bounds mirror the ``simulated_issues`` CHECK constraints + the
    DB CHECK on ``title`` (``<= 200``), ``description``
    (``<= 4000``) and ``source_insight_preview`` (``<= 400``).
    Unknown fields are rejected with ``extra=forbid``; ``priority``
    is restricted to the documented ``P0|P1|P2`` set so a malicious
    Requirement cannot inflate the audit row.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    priority: str | None = Field(default=None)
    source_insight_preview: str = Field(min_length=0, max_length=400)

    @field_validator("priority")
    @classmethod
    def _validate_priority(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value not in {"P0", "P1", "P2"}:
            raise ValueError("priority must be one of {'P0', 'P1', 'P2'}")
        return value


# ---- Registry ---------------------------------------------------------------


class ToolRegistry:
    """Process-wide static registry of :class:`ToolDescriptor` rows.

    The registry is intentionally NOT thread-safe — it is populated
    once at boot by :func:`bootstrap_default_registry` and read-only
    thereafter. A runtime ``register`` method exists for tests so a
    test can inject a custom descriptor into a freshly-built registry.
    """

    _instance: "ToolRegistry | None" = None

    def __init__(self) -> None:
        self._tools: dict[tuple[str, str], ToolDescriptor] = {}

    @classmethod
    def instance(cls) -> "ToolRegistry":
        """Return the process-wide registry, bootstrapping defaults on first access."""
        if cls._instance is None:
            bootstrap_default_registry()
            assert cls._instance is not None  # for type checkers
        return cls._instance

    @classmethod
    def reset_for_tests(cls) -> None:
        """Drop the cached instance. Tests call this before re-bootstrapping."""
        cls._instance = None

    def register(self, descriptor: ToolDescriptor) -> ToolDescriptor:
        key = descriptor.key
        if key in self._tools:
            existing = self._tools[key]
            if (
                existing.version == descriptor.version
                and existing.capability_kind == descriptor.capability_kind
                and existing.execution_mode == descriptor.execution_mode
            ):
                return existing
            raise ValueError(
                f"tool '{descriptor.name}/{descriptor.action}' already registered "
                f"with a different descriptor"
            )
        self._tools[key] = descriptor
        return descriptor

    def get(self, name: str, action: str) -> ToolDescriptor:
        try:
            return self._tools[(name, action)]
        except KeyError as exc:
            raise ToolNotRegisteredError(name, action) from exc

    def has(self, name: str, action: str) -> bool:
        return (name, action) in self._tools

    def list_tools(self) -> list[ToolDescriptor]:
        return sorted(
            self._tools.values(), key=lambda d: (d.name, d.action)
        )

    def validate_inputs(
        self, name: str, action: str, payload: Any
    ) -> BaseModel:
        """Validate ``payload`` against the registered descriptor's input schema.

        Raises :class:`ToolNotRegisteredError` when the name is unknown
        and :class:`ToolInputValidationError` when the payload fails
        the per-tool Pydantic schema (including the ``extra=forbid``
        guard).
        """
        descriptor = self.get(name, action)
        try:
            return descriptor.input_schema.model_validate(payload)
        except ValidationError as exc:
            raise ToolInputValidationError(name, action, str(exc)) from exc


# ---- Bootstrapping ---------------------------------------------------------


def _make_default_descriptors() -> list[ToolDescriptor]:
    """Build the static default descriptor set.

    Importing :mod:`app.tools.adapters` here keeps the registry module
    free of ORM / service-layer dependencies; the adapter factory is a
    callable the service layer invokes at Approve-time.
    """
    # Local import to avoid a circular dependency: adapters import
    # nothing from the registry, but the registry registers factories
    # that produce adapters.
    from app.tools.adapters import SimulatedIssueTrackerAdapter

    return [
        ToolDescriptor(
            name="web_research",
            action="search",
            version="research-agent-v2",
            description=(
                "Read-only web search capability used by the Phase 8 "
                "Research API to gather external market intelligence. "
                "The descriptor is metadata: the Research API calls the "
                "provider client directly. A generic tool-call surface "
                "that wants to invoke this capability MUST route through "
                "the registry so unknown input fields are rejected."
            ),
            capability_kind=CapabilityKind.READ,
            execution_mode=ExecutionMode.REAL,
            input_schema=WebResearchSearchInput,
            output_summary_schema=WebResearchSearchOutputSummary,
            idempotent=False,
            supports_bounded_retry=False,
            adapter_factory=None,
        ),
        ToolDescriptor(
            name="simulated_issue_tracker",
            action="create_issue",
            version="simulator-v1",
            description=(
                "Simulation-only write capability that produces a "
                "clearly-marked 'SIM-...' external id. There is no "
                "real Jira / Notion / Feishu integration; the simulator "
                "is the only registered write path while the integration "
                "is unverified. Invoke only via the explicit human "
                "approve flow."
            ),
            capability_kind=CapabilityKind.SIMULATION_ONLY,
            execution_mode=ExecutionMode.SIMULATION,
            input_schema=SimulatedIssueCreateInput,
            output_summary_schema=SimulatedIssueCreateOutputSummary,
            idempotent=True,
            supports_bounded_retry=True,
            adapter_factory=lambda: SimulatedIssueTrackerAdapter(),
        ),
    ]


def bootstrap_default_registry() -> ToolRegistry:
    """Populate the process-wide registry with the default descriptors.

    Idempotent: calling it twice returns the same instance and does
    not double-register. ``reset_for_tests`` drops the cache so a test
    can rebuild from scratch.
    """
    if ToolRegistry._instance is None:
        registry = ToolRegistry()
        for descriptor in _make_default_descriptors():
            registry.register(descriptor)
        ToolRegistry._instance = registry
    return ToolRegistry._instance


def get_default_registry() -> ToolRegistry:
    """Return the bootstrapped registry (bootstraps on first call)."""
    return ToolRegistry.instance()


# ---- Helpers ----------------------------------------------------------------


def make_request_id() -> str:
    """Return a UUID4 string used as the idempotency key for prepare calls.

    Exposed so tests can assert the request_id format on persisted
    rows; the service layer accepts a client-supplied UUID instead and
    only falls back to a generated value when the client omits it.
    """
    return str(uuid4())


__all__ = [
    "CapabilityKind",
    "ExecutionMode",
    "SimulatedIssueCreateInput",
    "SimulatedIssueCreateOutputSummary",
    "SimulatedIssueSnapshot",
    "ToolDescriptor",
    "ToolInputValidationError",
    "ToolNotRegisteredError",
    "ToolRegistry",
    "WebResearchSearchInput",
    "WebResearchSearchOutputSummary",
    "bootstrap_default_registry",
    "get_default_registry",
    "make_request_id",
]