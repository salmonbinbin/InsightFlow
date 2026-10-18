"""Phase 8 — Research LLM client safe response metadata tests.

These tests pin the v14-b Batch C safety contract on
:func:`app.services.research_client.safe_response_metadata` and on
the ``response_metadata`` field that the LLM parser attaches to
every :class:`ResearchLLMProtocolError`.

The contract is the allow-list: a protocol error carries ONLY the
following fields, derived from the upstream response:

* ``stop_reason`` — one of ``tool_use`` / ``end_turn`` / ``max_tokens``
  / ``stop_sequence`` / ``pause_turn`` / ``refusal`` or the literal
  ``"unknown"``.
* ``content_block_counts`` — ``{type: count}`` for
  ``text`` / ``tool_use`` / ``thinking`` / ``redacted_thinking`` /
  ``unknown``; up to 64 blocks counted.
* ``http_status`` — int in 100..599.
* ``usage`` — ``input_tokens`` / ``output_tokens`` /
  ``cache_creation_input_tokens`` / ``cache_read_input_tokens`` as
  non-negative int up to 1e9.

NEVER the upstream body, response headers, text / thinking
content, or arbitrary string IDs.

These tests are pure — they construct synthetic
``data`` payloads and assert on the metadata dict. No HTTP, no LLM.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from app.services.research_client import (
    ResearchLLMProtocolError,
    _parse_messages_response,
    safe_response_metadata,
)

# ============================================================================
# Sentry — body / header / text / thinking must never enter metadata
# ============================================================================


_SECRET_BODY = "TOPSECRET-bearer-9999-abc"
_SECRET_THINKING = "internal-cot-leak-deadbeef"
_SECRET_USAGE_FIELD = "secret_extra_field"
_SECRET_ID = "msg_unique_id_aaa-12345"


def test_sentry_metadata_never_contains_text_or_thinking_bodies() -> None:
    """A response with rich text and thinking content blocks must
    produce a metadata dict that records ONLY counts — the bodies
    themselves, the block ``id``s, the ``name`` fields, and any
    other string IDs are NEVER copied into metadata.
    """
    data = {
        "id": _SECRET_ID,
        "model": "secret-model-name-7777",
        "stop_reason": "end_turn",
        "content": [
            {
                "type": "text",
                "text": _SECRET_BODY,
                "id": "block-id-1",
            },
            {
                "type": "thinking",
                "thinking": _SECRET_THINKING,
                "signature": "sig-xxx",
                "id": "block-id-2",
            },
            {
                "type": "tool_use",
                "id": "tool-id-3",
                "name": "submit_research",
                "input": {"competitors": [{"name": _SECRET_BODY}]},
            },
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "extra_secret_metric": _SECRET_BODY,
        },
    }
    metadata = safe_response_metadata(data)
    # The allowed scalars survived.
    assert metadata["stop_reason"] == "end_turn"
    assert metadata["content_block_counts"] == {
        "text": 1,
        "thinking": 1,
        "tool_use": 1,
    }
    assert metadata["usage"] == {"input_tokens": 100, "output_tokens": 50}
    # Every sentinel string must be absent.
    flat = repr(metadata)
    for sentinel in (
        _SECRET_BODY,
        _SECRET_THINKING,
        _SECRET_ID,
        "secret-model-name",
        "block-id-1",
        "block-id-2",
        "tool-id-3",
        "submit_research",
        "extra_secret_metric",
    ):
        assert sentinel not in flat, f"metadata leaked sentinel {sentinel!r}: {flat!r}"


def test_sentry_metadata_never_contains_response_headers() -> None:
    """Header values are never an input to ``safe_response_metadata``;
    confirm that even if a caller accidentally stuffs a header dict
    into ``data``, the function ignores unknown top-level keys.
    """
    data = {
        "x-request-id": "req-9999",
        "anthropic-organization": "org-LEAKED-abc",
        "retry-after": "5",
        "stop_reason": "tool_use",
        "content": [],
    }
    metadata = safe_response_metadata(data)
    flat = repr(metadata)
    for sentinel in ("req-9999", "org-LEAKED", "retry-after", "anthropic"):
        assert sentinel not in flat


# ============================================================================
# stop_reason allow-list
# ============================================================================


@pytest.mark.parametrize(
    "stop_reason",
    [
        "tool_use",
        "end_turn",
        "max_tokens",
        "stop_sequence",
        "pause_turn",
        "refusal",
    ],
)
def test_stop_reason_allow_list_passes_through(stop_reason: str) -> None:
    metadata = safe_response_metadata({"stop_reason": stop_reason, "content": []})
    assert metadata["stop_reason"] == stop_reason


@pytest.mark.parametrize(
    "stop_reason",
    [
        "secret-unknown-reason",
        "",
        "tool_USE",  # case-sensitive
        "end_turns",
        "user_stop",
        "function_call",
    ],
)
def test_unknown_stop_reason_is_normalised_to_unknown(stop_reason: str) -> None:
    metadata = safe_response_metadata({"stop_reason": stop_reason, "content": []})
    assert metadata["stop_reason"] == "unknown"


def test_missing_stop_reason_is_unknown() -> None:
    metadata = safe_response_metadata({"content": []})
    assert metadata["stop_reason"] == "unknown"


def test_non_string_stop_reason_is_unknown() -> None:
    metadata = safe_response_metadata({"stop_reason": 42, "content": []})
    assert metadata["stop_reason"] == "unknown"


# ============================================================================
# content_block_counts allow-list + cap
# ============================================================================


def test_content_block_counts_only_lists_allow_list_types() -> None:
    data = {
        "stop_reason": "end_turn",
        "content": [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "id": "x", "name": "t", "input": {}},
            {"type": "thinking", "thinking": "x"},
            {"type": "redacted_thinking", "data": "x"},
            {"type": "weird_future_type", "blob": "x"},
            {"type": 123, "blob": "x"},
            "not a block",
            None,
            {"type": "text", "text": "again"},
        ],
    }
    metadata = safe_response_metadata(data)
    assert metadata["content_block_counts"] == {
        "text": 2,
        "tool_use": 1,
        "thinking": 1,
        "redacted_thinking": 1,
        # weird_future_type, type=123, "not a block", None all fold
        # into the ``unknown`` bucket — 4 distinct non-allow-listed
        # entries in the fixture.
        "unknown": 4,
    }


def test_content_block_counts_capped_at_64_blocks() -> None:
    """A response with thousands of blocks must not waste cycles; the
    counter caps at 64.
    """
    blocks = [{"type": "text", "text": "x"} for _ in range(200)]
    metadata = safe_response_metadata({"stop_reason": "end_turn", "content": blocks})
    # 64 blocks counted, all folded into the ``text`` bucket.
    assert metadata["content_block_counts"] == {"text": 64}


def test_content_block_counts_missing_content_omits_field() -> None:
    metadata = safe_response_metadata({"stop_reason": "end_turn"})
    assert "content_block_counts" not in metadata


def test_content_block_counts_non_list_omits_field() -> None:
    metadata = safe_response_metadata({"stop_reason": "end_turn", "content": "x"})
    assert "content_block_counts" not in metadata


# ============================================================================
# usage sanitisation — non-negative int <= 1e9, bool/NaN/inf/strings -> None
# ============================================================================


def test_usage_normal_ints_pass_through() -> None:
    data = {
        "stop_reason": "end_turn",
        "content": [],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 25,
            "cache_read_input_tokens": 5,
        },
    }
    metadata = safe_response_metadata(data)
    assert metadata["usage"] == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_input_tokens": 25,
        "cache_read_input_tokens": 5,
    }


def test_usage_extra_fields_are_dropped() -> None:
    """Fields outside the allow-list (and therefore outside the
    usage allow-list) are silently dropped.
    """
    data = {
        "stop_reason": "end_turn",
        "content": [],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "secret_extra_field": "leak-9999",
            "service_tier": "free",
        },
    }
    metadata = safe_response_metadata(data)
    assert metadata["usage"] == {"input_tokens": 100, "output_tokens": 50}
    assert "leak-9999" not in repr(metadata)


@pytest.mark.parametrize(
    "value, expected_input_tokens",
    [
        # bool is rejected (True/False are int subclass in Python).
        (True, None),
        (False, None),
        # Negative ints dropped.
        (-1, None),
        # Over-cap ints dropped.
        (1_000_000_001, None),
        # Unparseable strings dropped.
        ("abc", None),
        # NaN / inf dropped.
        (math.nan, None),
        (math.inf, None),
        (-math.inf, None),
        # A non-integer float is REJECTED, not silently truncated.
        (1.5, None),
        # A string parsing to a non-integer float is REJECTED, not
        # silently truncated.
        ("1.5", None),
        # A well-formed integer-shaped string in range is preserved.
        ("100", 100),
        # A negative integer-shaped string is dropped.
        ("-5", None),
        # A string above the cap is dropped.
        ("1e10", None),
    ],
)
def test_malicious_usage_values_are_dropped_or_preserved(
    value: Any,
    expected_input_tokens: int | None,
) -> None:
    """bool / negative / over-cap / NaN / inf / non-integer-float /
    unparseable strings all collapse to ``None`` in the usage map.
    Only exact integers (or integer-shaped strings) in
    ``[0, 1_000_000_000]`` are preserved verbatim.
    """
    data = {
        "stop_reason": "end_turn",
        "content": [],
        "usage": {"input_tokens": value, "output_tokens": 7},
    }
    metadata = safe_response_metadata(data)
    if expected_input_tokens is None:
        # ``input_tokens`` is dropped; only the safe ``output_tokens``
        # survives on the metadata ``usage`` map.
        assert metadata["usage"] == {"output_tokens": 7}
    else:
        assert metadata["usage"] == {
            "input_tokens": expected_input_tokens,
            "output_tokens": 7,
        }


def test_fractional_float_usage_is_rejected_not_truncated() -> None:
    """Regression: a non-integer float (``1.5``) MUST be rejected.
    The earlier implementation silently truncated to ``1``.
    """
    for value in (1.5, 0.5, 99.9, 1e9 + 0.5, 1.0001):
        metadata = safe_response_metadata(
            {
                "stop_reason": "end_turn",
                "content": [],
                "usage": {"input_tokens": value, "output_tokens": 7},
            }
        )
        assert "input_tokens" not in metadata["usage"], (
            f"fractional float {value!r} was truncated instead of rejected: metadata={metadata!r}"
        )
        assert metadata["usage"] == {"output_tokens": 7}


def test_fractional_string_usage_is_rejected_not_truncated() -> None:
    """Regression: a string that parses to a non-integer float
    (``"1.5"``) MUST be rejected. The earlier implementation
    silently truncated to ``1``.
    """
    for value in ("1.5", "0.5", "99.9", "1e9+0.5", "1.0001"):
        metadata = safe_response_metadata(
            {
                "stop_reason": "end_turn",
                "content": [],
                "usage": {"input_tokens": value, "output_tokens": 7},
            }
        )
        assert "input_tokens" not in metadata["usage"], (
            f"fractional string {value!r} was truncated instead of rejected: metadata={metadata!r}"
        )
        assert metadata["usage"] == {"output_tokens": 7}


def test_usage_dict_with_only_invalid_fields_is_omitted() -> None:
    """If every recognised usage field collapses to ``None``, the
    metadata MUST NOT include an empty ``usage`` dict.
    """
    data = {
        "stop_reason": "end_turn",
        "content": [],
        "usage": {"input_tokens": -1, "output_tokens": True},
    }
    metadata = safe_response_metadata(data)
    assert "usage" not in metadata


def test_usage_string_at_cap_passes() -> None:
    """A stringified integer inside the cap survives sanitisation."""
    data = {
        "stop_reason": "end_turn",
        "content": [],
        "usage": {"input_tokens": "999999999"},
    }
    metadata = safe_response_metadata(data)
    assert metadata["usage"] == {"input_tokens": 999_999_999}


def test_usage_string_above_cap_is_dropped() -> None:
    data = {
        "stop_reason": "end_turn",
        "content": [],
        "usage": {"input_tokens": "1000000001"},
    }
    metadata = safe_response_metadata(data)
    assert "usage" not in metadata


# ============================================================================
# http_status allow-list
# ============================================================================


@pytest.mark.parametrize("status", [100, 200, 404, 429, 500, 599])
def test_http_status_in_range_passes(status: int) -> None:
    metadata = safe_response_metadata(http_status=status)
    assert metadata["http_status"] == status


@pytest.mark.parametrize("status", [0, 99, 600, 1000, -1])
def test_http_status_out_of_range_dropped(status: int) -> None:
    metadata = safe_response_metadata(http_status=status)
    assert "http_status" not in metadata


@pytest.mark.parametrize("status", [True, False, "200", 200.0, None])
def test_http_status_non_int_dropped(status: Any) -> None:
    metadata = safe_response_metadata(http_status=status)
    assert "http_status" not in metadata


# ============================================================================
# Protocol errors carry metadata — end_turn vs max_tokens distinction
# ============================================================================


def test_no_tool_use_end_turn_metadata_distinguishes_from_max_tokens() -> None:
    """Without a tool_use block, the protocol error's metadata must
    surface ``stop_reason`` so the audit can tell
    ``stop_reason=end_turn`` (model decided not to call tool) from
    ``stop_reason=max_tokens`` (truncated output) from
    ``stop_reason=tool_use`` (rare upstream bug — declared a tool
    stop_reason but no tool_use block).
    """
    data_end = {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "I will not call the tool."}],
    }
    with pytest.raises(ResearchLLMProtocolError) as exc_end:
        _parse_messages_response(data_end, "submit_research")
    assert exc_end.value.response_metadata is not None
    assert exc_end.value.response_metadata["stop_reason"] == "end_turn"
    assert exc_end.value.response_metadata["content_block_counts"] == {"text": 1}
    # No tool_use block, so the count is text-only.
    assert "tool_use" not in exc_end.value.response_metadata["content_block_counts"]

    data_max = {
        "stop_reason": "max_tokens",
        "content": [{"type": "text", "text": "Started typing..."}],
    }
    with pytest.raises(ResearchLLMProtocolError) as exc_max:
        _parse_messages_response(data_max, "submit_research")
    assert exc_max.value.response_metadata["stop_reason"] == "max_tokens"

    data_tool = {
        "stop_reason": "tool_use",
        "content": [{"type": "text", "text": "Bizarre upstream."}],
    }
    with pytest.raises(ResearchLLMProtocolError) as exc_tool:
        _parse_messages_response(data_tool, "submit_research")
    assert exc_tool.value.response_metadata["stop_reason"] == "tool_use"


def test_non_object_response_carries_empty_metadata() -> None:
    with pytest.raises(ResearchLLMProtocolError) as exc:
        _parse_messages_response(["not a dict"], "submit_research")
    assert exc.value.response_metadata == {}


def test_wrong_tool_name_carries_metadata() -> None:
    data = {
        "stop_reason": "tool_use",
        "content": [
            {
                "type": "tool_use",
                "id": "x",
                "name": "wrong_tool_name",
                "input": {},
            }
        ],
    }
    with pytest.raises(ResearchLLMProtocolError) as exc:
        _parse_messages_response(data, "submit_research")
    assert exc.value.response_metadata["stop_reason"] == "tool_use"
    assert exc.value.response_metadata["content_block_counts"] == {"tool_use": 1}


def test_multiple_tool_blocks_still_rejected_with_metadata() -> None:
    data = {
        "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "id": "secret-msg-id-aaa", "name": "submit_research", "input": {}},
            {"type": "tool_use", "id": "secret-msg-id-bbb", "name": "submit_research", "input": {}},
        ],
    }
    with pytest.raises(ResearchLLMProtocolError) as exc:
        _parse_messages_response(data, "submit_research")
    assert exc.value.response_metadata["content_block_counts"] == {"tool_use": 2}
    # The error message uses the safe fixed category name
    # ``tool_use``, not the user-supplied tool name, so a hostile
    # payload that echoed a credential through the tool ``name``
    # cannot leak via the error text.
    msg = str(exc.value)
    assert "tool_use" in msg
    assert "expected exactly 1" in msg
    assert "submit_research" not in msg
    # The block ``id``s never enter the exception text.
    assert "secret-msg-id-aaa" not in msg
    assert "secret-msg-id-bbb" not in msg


def test_successful_parse_returns_response_metadata() -> None:
    """The success path also populates ``response_metadata`` so a
    caller that wants to log the safe summary does not need a
    second call. The metadata is identical in shape to the
    error-path metadata.
    """
    data = {
        "stop_reason": "tool_use",
        "content": [
            {
                "type": "tool_use",
                "id": "t",
                "name": "submit_research",
                "input": {"competitors": []},
            }
        ],
        "usage": {"input_tokens": 12, "output_tokens": 8},
    }
    result = _parse_messages_response(data, "submit_research")
    assert result.response_metadata == {
        "stop_reason": "tool_use",
        "content_block_counts": {"tool_use": 1},
        "usage": {"input_tokens": 12, "output_tokens": 8},
    }
    # The raw input / block bodies are not on the metadata.
    flat = repr(result.response_metadata)
    assert "competitors" not in flat
    assert "t" != flat  # the block id 't' is not the entire repr
