"""LLM client for the Phase 1 classification pipeline.

Only the ``Anthropic-compatible Messages API`` is wired up because the local
environment exposes one through ``ANTHROPIC_BASE_URL``. Structured output is
forced via a single ``submit_classifications`` tool call so the payload can be
parsed deterministically and validated server-side before any database write.

The client deliberately never logs ``api_key``; errors are surfaced as typed
exceptions the classification service can map to retry semantics. HTTP error
messages never embed the upstream response body so that no credential,
session token or upstream diagnostic leaks into logs, exception chains, or
persisted failure records.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

import httpx

from app.agents.prompts.feedback_classification_v1 import SYSTEM_PROMPT

CLASSIFICATION_TOOL_NAME = "submit_classifications"
CLASSIFICATION_TOOL_DESCRIPTION = (
    "Submit structured classification results for a batch of user feedback. "
    "The results array must contain exactly one entry per input feedback_id, "
    "in the same order, with no duplicates."
)

CLASSIFICATION_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "feedback_id": {"type": "string", "minLength": 1},
                    "feedback_type": {
                        "type": "string",
                        "enum": [
                            "bug",
                            "feature_request",
                            "ux_issue",
                            "performance_issue",
                            "pricing_issue",
                            "content_issue",
                            "positive_feedback",
                            "other",
                        ],
                    },
                    "module": {"type": "string", "minLength": 1, "maxLength": 100},
                    "intent": {"type": "string", "minLength": 1, "maxLength": 200},
                    "pain_point": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "sentiment": {
                        "type": "string",
                        "enum": ["positive", "neutral", "negative", "mixed"],
                    },
                    "severity": {"type": "integer", "minimum": 1, "maximum": 5},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": [
                    "feedback_id",
                    "feedback_type",
                    "module",
                    "intent",
                    "pain_point",
                    "sentiment",
                    "severity",
                    "confidence",
                ],
            },
        }
    },
    "required": ["results"],
}


class LLMConfigError(RuntimeError):
    """Raised when the LLM environment configuration is missing or invalid."""


class LLMTransportError(RuntimeError):
    """Raised on network or HTTP transport errors; treated as retriable."""


class LLMProtocolError(RuntimeError):
    """Raised when the response shape is unexpected or the structured payload is missing."""


@dataclass(frozen=True)
class LLMUsage:
    """Provider-reported usage.

    Either field may be ``None`` when the upstream omitted the usage block or
    returned a malformed number; callers must distinguish "unknown" from "zero"
    so totals do not silently misrepresent missing measurements.
    """

    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True)
class LLMResult:
    payload: dict[str, Any]
    usage: LLMUsage


class LLMClient:
    """Thin synchronous client around an Anthropic-compatible Messages API."""

    def __init__(
        self,
        api_key: str | None,
        base_url: str | None,
        model: str | None,
        timeout_seconds: float,
    ) -> None:
        if not api_key:
            raise LLMConfigError("LLM_API_KEY is not configured")
        if not base_url:
            raise LLMConfigError("LLM_BASE_URL is not configured")
        if not model:
            raise LLMConfigError("LLM_MODEL is not configured")
        # Strip trailing slash so we can append the canonical path.
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

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def classify_batch(self, items: list[dict[str, str]], max_tokens: int = 4096) -> LLMResult:
        """Classify a batch of feedback items via the structured tool call.

        ``items`` is a list of ``{"feedback_id": str, "content": str}``.
        """
        if not items:
            raise LLMProtocolError("classify_batch called with empty batch")
        user_message = json.dumps(
            {"feedback_items": items}, ensure_ascii=False, separators=(",", ":")
        )
        body = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": SYSTEM_PROMPT,
            "tools": [
                {
                    "name": CLASSIFICATION_TOOL_NAME,
                    "description": CLASSIFICATION_TOOL_DESCRIPTION,
                    "input_schema": CLASSIFICATION_TOOL_SCHEMA,
                }
            ],
            "tool_choice": {"type": "tool", "name": CLASSIFICATION_TOOL_NAME},
            "messages": [{"role": "user", "content": user_message}],
        }
        url = f"{self._base_url}/v1/messages"
        try:
            response = self._client.post(url, json=body)
        except httpx.HTTPError as exc:
            # Do not include the URL or any header in the exception text so an
            # injected credential cannot leak via repr or exception chain.
            raise LLMTransportError("LLM request failed") from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise LLMTransportError(f"LLM transient HTTP {response.status_code}")
        if response.status_code == 401 or response.status_code == 403:
            raise LLMProtocolError(
                f"LLM authentication rejected (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            # Categorise without echoing the response body; the body may
            # contain credentials or session tokens that the caller never
            # asked us to persist.
            raise LLMProtocolError(
                f"LLM client error (HTTP {response.status_code})"
            )
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise LLMProtocolError("LLM returned non-JSON body") from exc
        return _parse_messages_response(data)


def _parse_messages_response(data: dict[str, Any]) -> LLMResult:
    """Parse an Anthropic-compatible Messages response.

    Strict contract:

    * top-level ``content`` must be a non-empty array;
    * exactly one ``tool_use`` block is allowed and its ``name`` must equal
      ``CLASSIFICATION_TOOL_NAME`` — this rejects injections that try to
      return a different tool or a side-channel payload;
    * the ``input`` of the tool block must be an object containing
      ``results``; this is then handed to the higher-level schema validator
      for type/range/ID checks;
    * ``usage`` is optional; ``input_tokens`` / ``output_tokens`` may be
      missing or non-numeric and are surfaced as ``None`` so the caller can
      record them as unknown instead of treating them as zero.
    """
    if not isinstance(data, dict):
        raise LLMProtocolError("LLM response is not a JSON object")
    content = data.get("content")
    if not isinstance(content, list) or not content:
        raise LLMProtocolError("LLM response missing content array")
    tool_blocks = [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    if not tool_blocks:
        raise LLMProtocolError(
            f"LLM response missing {CLASSIFICATION_TOOL_NAME} tool_use block"
        )
    if len(tool_blocks) > 1:
        raise LLMProtocolError(
            f"LLM response contains {len(tool_blocks)} tool_use blocks; expected exactly 1"
        )
    tool_block = tool_blocks[0]
    if tool_block.get("name") != CLASSIFICATION_TOOL_NAME:
        # Do not echo the offending name verbatim — a misbehaving tool block
        # is exactly the surface an attacker can use to smuggle tokens,
        # markers or diagnostic text into persisted failure records.
        raise LLMProtocolError(
            "LLM response uses unexpected tool (not "
            + CLASSIFICATION_TOOL_NAME
            + ")"
        )
    inp = tool_block.get("input")
    if not isinstance(inp, dict):
        raise LLMProtocolError("LLM tool_use input is not an object")
    usage_raw = data.get("usage")
    if usage_raw is None:
        usage_raw = {}
    if not isinstance(usage_raw, dict):
        raise LLMProtocolError("LLM response usage is not an object")
    input_tokens = _parse_token_count(usage_raw.get("input_tokens"))
    # Anthropic-compatible providers report cache creation/read separately
    # from input_tokens. Sum all three components for the run's input usage.
    # Any malformed reported component makes the total unknown.
    if input_tokens is not None:
        for field in ("cache_creation_input_tokens", "cache_read_input_tokens"):
            if field in usage_raw:
                component = _parse_token_count(usage_raw[field])
                if component is None:
                    input_tokens = None
                    break
                input_tokens += component
    output_tokens = _parse_token_count(usage_raw.get("output_tokens"))
    return LLMResult(
        payload=inp,
        usage=LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _parse_token_count(value: Any) -> int | None:
    """Coerce a usage value to int, or return None if missing/invalid/NaN."""
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
