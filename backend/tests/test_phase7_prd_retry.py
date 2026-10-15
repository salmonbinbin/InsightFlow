"""Phase 7 P0 — focused unit tests for ``_call_prd`` retry behaviour.

These tests directly drive :func:`app.services.requirement_prd._call_prd`
with a stub ``PRDClient`` and a stubbed ``_parse_prd_payload``. They
do NOT touch the database, the LLM HTTP client, or any other Phase's
fixture — every test is fully offline.

Contract under test
-------------------
1. Total attempts bounded at ``max(1, prd_max_retries + 1)``.
2. ``PRDLLMProtocolError`` from ``PRDClient.generate_prd`` is retried
   on the same payload / model / prompt / timeout.
3. The strict ``_parse_prd_payload`` call sits OUTSIDE the inner
   ``try`` block — parser failures propagate on the first attempt
   and are NEVER retried.
4. On exhaustion the LAST error is re-raised verbatim. Both
   ``PRDLLMProtocolError`` and ``PRDLLMTransportError`` already map
   to 503 at the API layer (``backend/app/api/requirement_prd.py``);
   the HTTP contract is unchanged. The transport path is already
   covered by ``test_phase5.py`` — this file focuses on the
   protocol-error branch added in the Phase 7 P0 fix.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.db.config import settings
from app.services import requirement_prd as service_module
from app.services.requirement_prd import _call_prd
from app.services.requirement_prd_client import PRDLLMProtocolError


@pytest.fixture(autouse=True)
def _stub_settings(monkeypatch):
    """Provide a valid LLM config so the env-var check in ``_call_prd``
    passes. The actual ``PRDClient`` is monkeypatched separately, so
    these URLs are never contacted."""
    monkeypatch.setattr(settings, "llm_api_key", "test", raising=False)
    monkeypatch.setattr(settings, "llm_base_url", "http://invalid", raising=False)
    monkeypatch.setattr(settings, "llm_model", "test-model", raising=False)


def _stub_client(behaviors):
    """Return ``(StubClass, state)`` for a fake ``PRDClient``.

    Each item in ``behaviors`` is consumed in order on consecutive
    ``generate_prd`` calls: an ``Exception`` is raised, anything else
    is returned as the ``PRDLLMResult``-shaped ``SimpleNamespace``.
    Calling past the end raises ``AssertionError`` so the test fails
    loudly instead of silently masking a regression.
    """
    state = {"calls": 0}

    class _Stub:
        def __init__(self, **kwargs):
            # Accept and ignore the kwargs ``_call_prd`` passes when
            # instantiating ``PRDClient`` (api_key, base_url, model,
            # timeout_seconds) — none are needed because the stub does
            # not contact the network.
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def generate_prd(self, payload):
            state["calls"] += 1
            if state["calls"] > len(behaviors):
                raise AssertionError(
                    f"PRDClient.generate_prd called {state['calls']} times; "
                    f"only {len(behaviors)} behaviors configured"
                )
            behavior = behaviors[state["calls"] - 1]
            if isinstance(behavior, BaseException):
                raise behavior
            return behavior

    return _Stub, state


_VALID_PAYLOAD = SimpleNamespace(
    payload={"background": "x"},
    usage=SimpleNamespace(input_tokens=10, output_tokens=5),
)


def test_call_prd_recovers_after_one_tool_block_protocol_error(monkeypatch):
    """Happy path: one transient tool-block protocol failure is
    recovered transparently on the next attempt. ``_call_prd``
    returns the parsed content with ``attempt_count=2`` and
    ``retry_count=1``."""
    monkeypatch.setattr(settings, "prd_max_retries", 2)
    stub, state = _stub_client(
        [
            PRDLLMProtocolError("submit_prd response missing tool_use block (call 1)"),
            _VALID_PAYLOAD,
        ]
    )
    monkeypatch.setattr(service_module, "PRDClient", stub)
    monkeypatch.setattr(
        service_module,
        "_parse_prd_payload",
        lambda payload, *, allowed_evidence_ids: ({"background": "ok"}, []),
    )

    content, warnings, _, _, _, _, attempt_count, retry_count = _call_prd(
        payload={},
        allowed_evidence_ids=set(),
    )

    assert state["calls"] == 2
    assert attempt_count == 2
    assert retry_count == 1
    assert content == {"background": "ok"}
    assert warnings == []


@pytest.mark.parametrize(
    ("prd_max_retries", "expected_calls"),
    [(0, 1), (1, 2), (2, 3), (3, 4)],
)
def test_call_prd_exhausts_protocol_raises_last_error_verbatim(
    monkeypatch, prd_max_retries, expected_calls
):
    """When every attempt is a tool-block protocol failure, the loop
    raises ``PRDLLMProtocolError`` after exactly
    ``max(1, prd_max_retries + 1)`` calls and re-raises the LAST
    error verbatim. The API layer maps ``PRDLLMProtocolError`` to 503
    (no caller-visible shift). Parametrised to lock both the
    ``max(1, ...)`` floor (retries=0 → 1 attempt) and the
    default-of-2 cap (retries=2 → 3 attempts) in a single test."""
    monkeypatch.setattr(settings, "prd_max_retries", prd_max_retries)
    stub, state = _stub_client(
        [
            PRDLLMProtocolError(f"submit_prd response missing tool_use block (call {i})")
            for i in range(1, expected_calls + 1)
        ]
    )
    monkeypatch.setattr(service_module, "PRDClient", stub)

    def _fail_parse(payload, *, allowed_evidence_ids):
        raise AssertionError("_parse_prd_payload must not run on exhaustion")

    monkeypatch.setattr(service_module, "_parse_prd_payload", _fail_parse)

    with pytest.raises(PRDLLMProtocolError) as exc:
        _call_prd(payload={}, allowed_evidence_ids=set())

    assert state["calls"] == expected_calls
    # Verbatim last-attempt message — operator dashboards correlate on
    # this string.
    assert f"call {expected_calls}" in str(exc.value)


def test_call_prd_parser_errors_are_not_retried(monkeypatch):
    """Parser / grounding errors are NOT retried — they propagate on
    the first occurrence. ``_parse_prd_payload`` sits OUTSIDE the
    retry ``try`` block by design. A single LLM call followed by a
    parser error must surface as ``PRDLLMProtocolError`` after
    exactly one LLM call. No second attempt, no parser relaxation —
    the strict gate cannot be bypassed by retrying a bad payload."""
    monkeypatch.setattr(settings, "prd_max_retries", 3)
    stub, state = _stub_client([_VALID_PAYLOAD])
    monkeypatch.setattr(service_module, "PRDClient", stub)

    def _raise_parser_error(payload, *, allowed_evidence_ids):
        raise PRDLLMProtocolError(
            "acceptance_criteria[0].verifiable must be true; "
            "unverifiable acceptance criteria are rejected at parse time."
        )

    monkeypatch.setattr(service_module, "_parse_prd_payload", _raise_parser_error)

    with pytest.raises(PRDLLMProtocolError) as exc:
        _call_prd(payload={}, allowed_evidence_ids=set())

    assert state["calls"] == 1
    assert "verifiable" in str(exc.value)
