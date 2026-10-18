"""Phase 8 — Tool Layer.

Three sub-modules collaborate:

* :mod:`app.tools.registry` — the static :class:`ToolRegistry` plus
  :class:`ToolDescriptor` metadata + per-tool input / output schemas.
  Every caller that wants to invoke a tool MUST go through
  :meth:`ToolRegistry.validate_inputs` so an unregistered name or a
  payload carrying unknown fields is rejected BEFORE persistence.
* :mod:`app.tools.adapters` — the simulator adapter for
  ``simulated_issue_tracker.create_issue``. The adapter is a clearly-
  marked simulation that NEVER makes a real network call; it returns
  a ``SIM-...`` external id and a ``simulated=true`` flag so a UI can
  never mistake it for a real Jira / Notion / Feishu write.
* :mod:`app.tools` (this module) — re-exports the public surface for
  ergonomic ``from app.tools import ...`` imports.

The registry is populated once at process start by
:func:`app.tools.registry.bootstrap_default_registry`. The brief
mandates that ``web_research.search`` is registered as the read-only
metadata for the capability the Research API already exercises; the
Research API itself continues to call
:mod:`app.services.research_provider` directly — the registry is the
contractual gate, not a redirect.
"""

from __future__ import annotations

from app.tools.adapters import (
    SimulatedIssueTrackerAdapter,
    SimulatedIssueTrackerError,
    SimulatedIssueTrackerTransientError,
)
from app.tools.registry import (
    CapabilityKind,
    ExecutionMode,
    ToolDescriptor,
    ToolInputValidationError,
    ToolNotRegisteredError,
    ToolRegistry,
    bootstrap_default_registry,
    get_default_registry,
)

__all__ = [
    "CapabilityKind",
    "ExecutionMode",
    "SimulatedIssueTrackerAdapter",
    "SimulatedIssueTrackerError",
    "SimulatedIssueTrackerTransientError",
    "ToolDescriptor",
    "ToolInputValidationError",
    "ToolNotRegisteredError",
    "ToolRegistry",
    "bootstrap_default_registry",
    "get_default_registry",
]