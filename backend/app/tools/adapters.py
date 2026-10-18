"""Phase 8 — Tool Adapters.

The only adapter in scope for Phase 8 is
:class:`SimulatedIssueTrackerAdapter`. The brief mandates that real
Jira / Notion / Feishu integrations are intentionally NOT wired up
because there are no verified write credentials in this environment.
Any caller that wants to create an external issue MUST go through
this simulator so the UI / audit trail can clearly mark the action
as ``simulated=true`` and surface a ``SIM-...`` external id.

The simulator supports three explicit failure modes so the audit +
retry policy can be exercised in tests:

* ``transient_timeout`` — raises :class:`SimulatedIssueTrackerTransientError`
  with the safe category ``timeout``. Retriable when the descriptor's
  ``supports_bounded_retry`` flag is True.
* ``transient_5xx`` — same as above with category ``http_5xx``.
* ``provider_failure`` — raises :class:`SimulatedIssueTrackerError`
  with category ``provider_failure``. NOT retriable — the simulator
  itself cannot decide whether the simulated write was accepted or
  rejected, so the run is marked ``failed`` to preserve the audit
  trail rather than risk a silent duplicate.

The simulator never reaches the network. It returns a deterministic
``SIM-...`` external id built from a UUID4 prefix so two test runs do
not collide on persisted rows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

# ---- Errors -----------------------------------------------------------------


class SimulatedIssueTrackerError(RuntimeError):
    """Base class for simulator failures. Carries a safe category code."""

    def __init__(self, category: str, message: str) -> None:
        self.category = category
        super().__init__(message)


class SimulatedIssueTrackerTransientError(SimulatedIssueTrackerError):
    """Transient failure — retriable up to the descriptor's retry budget."""


# ---- Result dataclass -------------------------------------------------------


@dataclass(frozen=True)
class SimulatedIssueResult:
    """Outcome of one simulator invocation.

    ``external_id`` is the deterministic ``SIM-...`` token persisted
    on the run row + the :class:`SimulatedIssue` audit row. ``simulated``
    is always True so a UI consumer can never mistake the response
    for a real Jira / Notion / Feishu write. ``summary`` is a bounded
    human-readable line that the service layer stores in
    ``ToolRun.response_summary``; it never carries raw feedback text,
    credential-shaped tokens or any user-injected body.
    """

    external_id: str
    simulated: bool
    tool_name: str
    tool_action: str
    summary: str
    extra: dict[str, Any] = field(default_factory=dict)


# ---- Adapter ----------------------------------------------------------------


class SimulatedIssueTrackerAdapter:
    """Simulator for ``simulated_issue_tracker.create_issue``.

    The adapter never reaches the network; it only constructs a
    deterministic ``SIM-...`` external id and a bounded summary. The
    service layer decides whether the run succeeds, retries or fails.

    Failure injection is keyed off the instance attributes so a test
    can opt into a specific failure mode without monkeypatching
    methods:

    * ``fail_mode = "transient_timeout"`` → raises transient timeout.
    * ``fail_mode = "transient_5xx"`` → raises transient 5xx.
    * ``fail_mode = "provider_failure"`` → raises non-retriable error.
    * ``fail_mode = None`` (default) → succeeds.

    ``fail_on_attempt`` bounds the failure to the first N attempts so a
    retry-after-transient scenario can be exercised: e.g.
    ``fail_on_attempt=2`` fails the first two calls and lets the third
    succeed.
    """

    def __init__(
        self,
        *,
        fail_mode: str | None = None,
        fail_on_attempt: int = 1,
    ) -> None:
        if fail_mode not in (None, "transient_timeout", "transient_5xx", "provider_failure"):
            raise ValueError(f"unknown fail_mode: {fail_mode!r}")
        if fail_on_attempt < 1:
            raise ValueError("fail_on_attempt must be >= 1")
        self._fail_mode = fail_mode
        self._fail_on_attempt = fail_on_attempt
        self._attempt_count = 0
        self.calls: list[dict[str, Any]] = []

    @property
    def attempt_count(self) -> int:
        return self._attempt_count

    def execute(
        self,
        *,
        tool_name: str,
        tool_action: str,
        title: str,
        description: str,
        priority: str | None,
        source_insight_preview: str,
        requirement_id: Any,
        project_id: Any,
    ) -> SimulatedIssueResult:
        """Execute one simulator call.

        The function signature carries the server-derived issue fields
        only — the caller (the Approve route handler) is responsible
        for assembling those fields from the Requirement + source
        Insight so the simulator cannot accidentally echo raw feedback
        content or a credential-shaped token.
        """
        self._attempt_count += 1
        # Record the call AFTER incrementing the attempt counter so
        # ``len(self.calls) == self._attempt_count`` always holds.
        self.calls.append(
            {
                "tool_name": tool_name,
                "tool_action": tool_action,
                "title_length": len(title),
                "description_length": len(description),
                "priority": priority,
                "requirement_id": str(requirement_id),
                "project_id": str(project_id),
            }
        )

        # Failure injection — runs BEFORE the id generation so a
        # retriable failure does not leak a half-built external id
        # into the audit trail.
        if self._fail_mode is not None and self._attempt_count <= self._fail_on_attempt:
            if self._fail_mode == "transient_timeout":
                raise SimulatedIssueTrackerTransientError(
                    category="timeout",
                    message="simulated transient timeout",
                )
            if self._fail_mode == "transient_5xx":
                raise SimulatedIssueTrackerTransientError(
                    category="http_5xx",
                    message="simulated transient 5xx",
                )
            if self._fail_mode == "provider_failure":
                raise SimulatedIssueTrackerError(
                    category="provider_failure",
                    message="simulated provider failure",
                )

        # Deterministic SIM- prefix so a UI can grep for simulated
        # issues; the suffix is a fresh UUID4 per call so two
        # Approve calls do not collide on the audit table.
        external_id = f"SIM-{uuid.uuid4().hex[:24]}"
        summary = (
            f"Simulated issue created for requirement {requirement_id} "
            f"(title length {len(title)} chars, priority {priority or 'n/a'})"
        )
        return SimulatedIssueResult(
            external_id=external_id,
            simulated=True,
            tool_name=tool_name,
            tool_action=tool_action,
            summary=summary,
            extra={
                "source_insight_preview_length": len(source_insight_preview),
            },
        )

    def close(self) -> None:
        # Nothing to release today; reserved for future real adapters.
        return None


__all__ = [
    "SimulatedIssueResult",
    "SimulatedIssueTrackerAdapter",
    "SimulatedIssueTrackerError",
    "SimulatedIssueTrackerTransientError",
]