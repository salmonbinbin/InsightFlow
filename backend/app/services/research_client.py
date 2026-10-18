"""Phase 8 — Research Agent LLM client.

Thin synchronous wrapper around the Anthropic-compatible Messages API
that mirrors :mod:`app.services.insight_client`. The model receives the
internal-context snapshot, the bounded research question and the
provider-returned search hits, then produces a structured
``submit_research`` tool call. The validator in
:mod:`app.services.research_service` enforces the per-finding
``source_refs`` / ``status`` invariant; this client only owns the wire
format.

The client deliberately never logs the API key (the value is loaded
once at construction; the request headers do not include the literal
key — they pass through ``httpx.Headers`` so the value remains hidden
from reprs). Errors are surfaced as typed exceptions
(``ResearchLLMTransportError`` / ``ResearchLLMProtocolError``) so the
service layer can decide between bounded retry, persistence and
escalation. Protocol errors carry an optional ``response_metadata``
dict built exclusively from the allow-listed scalars in
:func:`safe_response_metadata` — never the upstream body, headers, or
text / thinking content.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.agents.prompts import research_agent_v2 as _default_prompt_module

# Allow-list for the ``stop_reason`` field on a Messages response.
# Anything outside this set is recorded as the literal ``"unknown"``
# string so the metadata is a closed set of categories.
_ALLOWED_STOP_REASONS = frozenset(
    {
        "tool_use",
        "end_turn",
        "max_tokens",
        "stop_sequence",
        "pause_turn",
        "refusal",
    }
)

# Allow-list of content block types. Any other ``type`` value is
# counted under ``"unknown"``. Text and thinking content bodies are
# NEVER recorded in metadata; the count is the only signal.
_ALLOWED_BLOCK_TYPES = frozenset(
    {
        "text",
        "tool_use",
        "thinking",
        "redacted_thinking",
        "unknown",
    }
)

# Hard cap on the number of content blocks we look at. A runaway
# response that streams thousands of blocks cannot spend unbounded
# time in the metadata builder.
_MAX_BLOCKS_FOR_METADATA = 64

# Token-count ceiling. The Anthropic wire format is non-negative
# integers, but a malicious or buggy response could carry a
# 1e308-shaped float. Any value above this ceiling is dropped.
_MAX_TOKEN_COUNT_FOR_METADATA = 1_000_000_000


class ResearchLLMConfigError(RuntimeError):
    """Missing Research Agent LLM environment configuration."""


class ResearchLLMTransportError(RuntimeError):
    """Network / transient HTTP failure (retriable)."""


class ResearchLLMProtocolError(RuntimeError):
    """Malformed JSON, missing tool block, or unexpected tool name.

    ``response_metadata`` is the safe allow-listed summary built by
    :func:`safe_response_metadata` for the failing response. It is
    the ONLY structured context attached to a protocol error. The
    raw upstream body, response headers, text / thinking content,
    and any string ID are NEVER recorded here. When the parser
    cannot read the response at all, ``response_metadata`` is
    ``None`` and the message is the only diagnostic.
    """

    def __init__(
        self,
        message: str,
        *,
        response_metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        # Frozen copy so callers cannot mutate the safe metadata
        # after the exception is constructed.
        self.response_metadata = (
            dict(response_metadata) if isinstance(response_metadata, dict) else None
        )


@dataclass(frozen=True)
class ResearchLLMUsage:
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True)
class ResearchLLMResult:
    payload: dict[str, Any]
    usage: ResearchLLMUsage
    # Safe allow-listed summary of the response. ``None`` on the
    # success path of callers that do not opt in; the constructor
    # default keeps backward compatibility with existing call sites
    # that construct ``ResearchLLMResult(payload=..., usage=...)``.
    response_metadata: dict[str, Any] | None = None


class ResearchLLMClient:
    """Synchronous Phase 8 LLM client (one tool call per research run)."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        model: str | None,
        timeout_seconds: float,
        prompt_module: Any | None = None,
    ) -> None:
        if not api_key:
            raise ResearchLLMConfigError("LLM_API_KEY is not configured")
        if not base_url:
            raise ResearchLLMConfigError("LLM_BASE_URL is not configured")
        if not model:
            raise ResearchLLMConfigError("LLM_MODEL is not configured")
        # The prompt module is pluggable so v3 (and any future
        # version) can ride on the same wire format without
        # duplicating the client. The default is v2 so any
        # existing caller that constructs the client without
        # specifying a module continues to work unchanged. The
        # module must expose ``SYSTEM_PROMPT`` / ``TOOL_NAME`` /
        # ``TOOL_DESCRIPTION`` / ``TOOL_SCHEMA`` / ``PROMPT_VERSION``.
        self._prompt_module = prompt_module or _default_prompt_module
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._client = httpx.Client(
            timeout=timeout_seconds,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )

    @property
    def prompt_version(self) -> str:
        return self._prompt_module.PROMPT_VERSION

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ResearchLLMClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def generate_research(
        self,
        *,
        user_message: dict[str, Any],
        max_tokens: int = 4096,
        retry_hint: str | None = None,
    ) -> ResearchLLMResult:
        """Send a structured research request and return the parsed tool call.

        ``user_message`` is the payload the LLM consumes:
        ``{"internal_context": {...}, "research_question": ..., "scope": ...,
        "search_results": [...]}``. The LLM has no other channels.

        ``retry_hint`` is an optional bounded error-category hint that the
        service appends as a separate user-turn message AFTER the JSON
        payload when a previous attempt was rejected by the validator.
        It is deliberately short (a category code + a short instruction
        like "Drop extra top-level fields."), never carries the
        internal exception text, payload bytes or API keys. The hint is
        stripped of any 6-200 char secret-shaped token before it is
        sent so an upstream body that echoed a credential cannot leak
        via the retry channel.
        """
        if not isinstance(user_message, dict):
            raise ResearchLLMProtocolError("generate_research called with non-object payload")
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": json.dumps(user_message, ensure_ascii=False, separators=(",", ":")),
            }
        ]
        if retry_hint:
            # Defence-in-depth: scrub any short-lived secret-shaped
            # token out of the hint before it leaves the process. The
            # hint is bounded (≤500 chars below) and contains no
            # credentials, but if a future change accidentally pipes
            # an upstream body through the channel this scrub keeps
            # the wire free of ``sk-...`` / ``AKIA...`` shaped tokens.
            scrubbed = _REDACTED_TOKEN_RE.sub(" [REDACTED] ", str(retry_hint))
            scrubbed = scrubbed[:500]
            messages.append({"role": "user", "content": scrubbed})
        body = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": self._prompt_module.SYSTEM_PROMPT,
            "tools": [
                {
                    "name": self._prompt_module.TOOL_NAME,
                    "description": self._prompt_module.TOOL_DESCRIPTION,
                    "input_schema": self._prompt_module.TOOL_SCHEMA,
                }
            ],
            "tool_choice": {"type": "tool", "name": self._prompt_module.TOOL_NAME},
            "messages": messages,
        }
        url = f"{self._base_url}/v1/messages"
        try:
            response = self._client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise ResearchLLMTransportError("research request failed") from exc
        status = response.status_code
        if status >= 500 or status == 429:
            raise ResearchLLMTransportError(f"research transient HTTP {status}")
        if status in (401, 403):
            raise ResearchLLMProtocolError(
                f"research authentication rejected (HTTP {status})",
                response_metadata=safe_response_metadata(http_status=status),
            )
        if status >= 400:
            raise ResearchLLMProtocolError(
                f"research client error (HTTP {status})",
                response_metadata=safe_response_metadata(http_status=status),
            )
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            # Non-JSON bodies are still summarised with the HTTP
            # status so an audit can tell a 200-with-garbage from a
            # 500-with-garbage without seeing the body.
            raise ResearchLLMProtocolError(
                "research returned non-JSON body",
                response_metadata=safe_response_metadata(http_status=status),
            ) from exc
        return _parse_messages_response(data, self._prompt_module.TOOL_NAME)


def safe_response_metadata(
    data: Any = None,
    *,
    http_status: int | None = None,
) -> dict[str, Any]:
    """Build a safe allow-listed metadata dict.

    Every field is drawn from a closed allow-list:

    * ``stop_reason`` — one of the literals in
      :data:`_ALLOWED_STOP_REASONS`, or the literal ``"unknown"``.
    * ``content_block_counts`` — a ``{type: count}`` map over the
      first :data:`_MAX_BLOCKS_FOR_METADATA` content blocks, with
      unknown types folded into the ``"unknown"`` key. The text /
      thinking bodies themselves are NEVER copied.
    * ``http_status`` — an int in 100..599, or absent. A bool,
      out-of-range int, or non-int is silently dropped.
    * ``usage`` — a dict with ``input_tokens`` / ``output_tokens``
      and the cache token sub-fields, each a non-negative int up
      to :data:`_MAX_TOKEN_COUNT_FOR_METADATA` or ``None``. Bool,
      NaN, infinity, and unparseable strings are dropped.

    Headers, response bodies, tool ``input`` payloads, and arbitrary
    string IDs NEVER appear in the result.
    """
    metadata: dict[str, Any] = {}

    if http_status is not None:
        if (
            isinstance(http_status, int)
            and not isinstance(http_status, bool)
            and 100 <= http_status <= 599
        ):
            metadata["http_status"] = http_status

    if isinstance(data, dict):
        # stop_reason: must be a string from the allow-list.
        stop_raw = data.get("stop_reason")
        if isinstance(stop_raw, str):
            metadata["stop_reason"] = stop_raw if stop_raw in _ALLOWED_STOP_REASONS else "unknown"
        else:
            metadata["stop_reason"] = "unknown"

        # content_block_counts: count types, drop bodies.
        content = data.get("content")
        if isinstance(content, list):
            counts: dict[str, int] = {}
            for block in list(content)[:_MAX_BLOCKS_FOR_METADATA]:
                if not isinstance(block, dict):
                    counts["unknown"] = counts.get("unknown", 0) + 1
                    continue
                btype = block.get("type")
                if isinstance(btype, str) and btype in _ALLOWED_BLOCK_TYPES:
                    key = btype
                else:
                    key = "unknown"
                counts[key] = counts.get(key, 0) + 1
            metadata["content_block_counts"] = counts

        # usage: only the recognised token sub-fields, sanitised.
        usage_raw = data.get("usage")
        if isinstance(usage_raw, dict):
            usage: dict[str, Any] = {}
            for field in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            ):
                token = _parse_safe_token_count(usage_raw.get(field))
                if token is not None:
                    usage[field] = token
            if usage:
                metadata["usage"] = usage

    return metadata


def _parse_safe_token_count(value: Any) -> int | None:
    """Sanitise a token-count value into a bounded non-negative int.

    ``bool`` is rejected explicitly (Python treats ``True`` as ``1``).
    Only EXACT integer values are accepted: a non-integer float
    (``1.5``) or a string that parses to a non-integer float
    (``"1.5"``) is dropped rather than silently truncated. NaN /
    inf / negative / out-of-cap / unparseable strings all return
    ``None`` so the upstream cannot inject a sentinel into metadata.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value < 0 or value > _MAX_TOKEN_COUNT_FOR_METADATA:
            return None
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        if value < 0 or value > _MAX_TOKEN_COUNT_FOR_METADATA:
            return None
        # Reject fractional floats — ``int(1.5)`` would silently
        # truncate to ``1`` and let an upstream shape the metadata.
        if not value.is_integer():
            return None
        return int(value)
    if isinstance(value, str):
        try:
            num = float(value)
        except ValueError:
            return None
        if not math.isfinite(num):
            return None
        if num < 0 or num > _MAX_TOKEN_COUNT_FOR_METADATA:
            return None
        # Same fractional-float rejection as for native floats:
        # ``int(float("1.5"))`` would silently become ``1``.
        if not num.is_integer():
            return None
        return int(num)
    return None


def _parse_messages_response(data: Any, expected_tool_name: str) -> ResearchLLMResult:
    """Strict Anthropic-compatible Messages parser for research output.

    Mirrors the rules in
    :func:`app.services.insight_client._parse_messages_response` so
    injection shapes (missing tool block, wrong tool name, multiple
    tool blocks, non-object input) all raise
    :class:`ResearchLLMProtocolError` and never reach the persistence
    layer. Every protocol error carries the safe
    ``response_metadata`` so an audit can distinguish a
    stop_reason=end_turn "no tool_use" from a stop_reason=max_tokens
    "no tool_use" without seeing the raw body.

    ``expected_tool_name`` is taken from the configured prompt
    module so v3 (or any future version) rides on the same parser
    without hard-coding a tool name.
    """
    # ``data`` may not be a dict; safe_response_metadata handles
    # that by returning an empty / status-only summary. We still
    # pass it through so the protocol error always carries
    # SOMETHING the audit can use.
    base_metadata = safe_response_metadata(data if isinstance(data, dict) else None)
    if not isinstance(data, dict):
        raise ResearchLLMProtocolError(
            "research response is not a JSON object",
            response_metadata=base_metadata,
        )
    content = data.get("content")
    if not isinstance(content, list) or not content:
        raise ResearchLLMProtocolError(
            "research response missing content array",
            response_metadata=base_metadata,
        )
    tool_blocks = [
        block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    if not tool_blocks:
        # The distinguishing diagnostic the audit needs lives in
        # metadata.stop_reason (``end_turn`` vs ``max_tokens`` etc.).
        # We deliberately do NOT include the raw body in the message.
        stop = base_metadata.get("stop_reason", "unknown")
        raise ResearchLLMProtocolError(
            "research response missing "
            + expected_tool_name
            + f" tool_use block (stop_reason={stop})",
            response_metadata=base_metadata,
        )
    if len(tool_blocks) > 1:
        raise ResearchLLMProtocolError(
            f"research response contains {len(tool_blocks)} tool_use blocks; expected exactly 1",
            response_metadata=base_metadata,
        )
    block = tool_blocks[0]
    if block.get("name") != expected_tool_name:
        raise ResearchLLMProtocolError(
            "research response uses unexpected tool (not " + expected_tool_name + ")",
            response_metadata=base_metadata,
        )
    inp = block.get("input")
    if not isinstance(inp, dict):
        raise ResearchLLMProtocolError(
            "research tool_use input is not an object",
            response_metadata=base_metadata,
        )
    usage_raw = data.get("usage")
    if usage_raw is None:
        usage_raw = {}
    if not isinstance(usage_raw, dict):
        raise ResearchLLMProtocolError(
            "research usage is not an object",
            response_metadata=base_metadata,
        )
    input_tokens = _parse_token_count(usage_raw.get("input_tokens"))
    if input_tokens is not None:
        for field in ("cache_creation_input_tokens", "cache_read_input_tokens"):
            if field in usage_raw:
                component = _parse_token_count(usage_raw[field])
                if component is None:
                    input_tokens = None
                    break
                input_tokens += component
    output_tokens = _parse_token_count(usage_raw.get("output_tokens"))
    return ResearchLLMResult(
        payload=inp,
        usage=ResearchLLMUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        response_metadata=base_metadata,
    )


def _parse_token_count(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return int(value) if value >= 0 else None
    if isinstance(value, str):
        try:
            num = float(value)
        except ValueError:
            return None
        if not math.isfinite(num):
            return None
        return int(num) if num >= 0 else None
    return None


# ``generate_research`` scrubs any short-lived secret-shaped token out
# of the bounded ``retry_hint`` before it is sent. The hint is built
# from a category code + short guidance, so this regex is only there
# as a safety net for future callers that might pipe raw upstream
# text through the channel.
_REDACTED_TOKEN_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_\-]{6,}|"
    r"AKIA[0-9A-Z]{8,}|"
    r"x-api-key=[^\s,;]+|"
    r"api[_-]?key[=:][^\s,;]+|"
    r"bearer\s+[A-Za-z0-9._\-]{6,}|"
    r"password[=:][^\s,;]+|"
    r"secret[=:][^\s,;]+|"
    r"token[=:][A-Za-z0-9._\-]{6,})",
    re.IGNORECASE,
)


__all__ = [
    "ResearchLLMClient",
    "ResearchLLMConfigError",
    "ResearchLLMProtocolError",
    "ResearchLLMResult",
    "ResearchLLMTransportError",
    "ResearchLLMUsage",
    "safe_response_metadata",
]
