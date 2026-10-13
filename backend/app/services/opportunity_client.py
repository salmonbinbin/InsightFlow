"""Phase 4 — Opportunity User Value LLM client.

A thin synchronous wrapper around an Anthropic-compatible Messages
API that returns exactly one ``submit_opportunity_user_value`` tool
call for the supplied insight payload. The structured payload is
parsed deterministically; downstream code performs the per-field /
business validation.

Mirrors :mod:`app.services.insight_client` for the security
guarantees (no echo of credentials, error categorisation, cache-token
support).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

import httpx

from app.agents.prompts.opportunity_recommendation_v1 import (
    SYSTEM_PROMPT,
    TOOL_DESCRIPTION,
    TOOL_NAME,
    TOOL_SCHEMA,
)


class OpportunityLLMConfigError(RuntimeError):
    """Missing LLM environment variables."""


class OpportunityLLMTransportError(RuntimeError):
    """Network or transient HTTP failure (retriable)."""


class OpportunityLLMProtocolError(RuntimeError):
    """Malformed JSON, missing tool block, or wrong tool name."""


@dataclass(frozen=True)
class OpportunityLLMUsage:
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True)
class OpportunityLLMResult:
    payload: dict[str, Any]
    usage: OpportunityLLMUsage


class OpportunityClient:
    """Synchronous Phase 4 opportunity client (one tool call per insight)."""

    def __init__(
        self,
        api_key: str | None,
        base_url: str | None,
        model: str | None,
        timeout_seconds: float,
    ) -> None:
        if not api_key:
            raise OpportunityLLMConfigError("LLM_API_KEY is not configured")
        if not base_url:
            raise OpportunityLLMConfigError("LLM_BASE_URL is not configured")
        if not model:
            raise OpportunityLLMConfigError("LLM_MODEL is not configured")
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

    def __enter__(self) -> "OpportunityClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def generate_user_value(self, payload: dict[str, Any]) -> OpportunityLLMResult:
        if not payload:
            raise OpportunityLLMProtocolError("generate_user_value called with empty payload")
        body = {
            "model": self._model,
            "max_tokens": 1024,
            "system": SYSTEM_PROMPT,
            "tools": [
                {
                    "name": TOOL_NAME,
                    "description": TOOL_DESCRIPTION,
                    "input_schema": TOOL_SCHEMA,
                }
            ],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(
                        {"opportunity": payload}, ensure_ascii=False, separators=(",", ":")
                    ),
                }
            ],
        }
        url = f"{self._base_url}/v1/messages"
        try:
            response = self._client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise OpportunityLLMTransportError("opportunity request failed") from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise OpportunityLLMTransportError(f"opportunity transient HTTP {response.status_code}")
        if response.status_code in (401, 403):
            raise OpportunityLLMProtocolError(
                f"opportunity authentication rejected (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            raise OpportunityLLMProtocolError(
                f"opportunity client error (HTTP {response.status_code})"
            )
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise OpportunityLLMProtocolError("opportunity returned non-JSON body") from exc
        return _parse_messages_response(data)


def _parse_messages_response(data: dict[str, Any]) -> OpportunityLLMResult:
    """Strict Anthropic-compatible Messages parser for opportunity output."""
    if not isinstance(data, dict):
        raise OpportunityLLMProtocolError("opportunity response is not a JSON object")
    content = data.get("content")
    if not isinstance(content, list) or not content:
        raise OpportunityLLMProtocolError("opportunity response missing content array")
    tool_blocks = [
        block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    if not tool_blocks:
        raise OpportunityLLMProtocolError(
            "opportunity response missing " + TOOL_NAME + " tool_use block"
        )
    if len(tool_blocks) > 1:
        raise OpportunityLLMProtocolError(
            f"opportunity response contains {len(tool_blocks)} tool_use blocks; expected exactly 1"
        )
    block = tool_blocks[0]
    if block.get("name") != TOOL_NAME:
        raise OpportunityLLMProtocolError(
            "opportunity response uses unexpected tool (not " + TOOL_NAME + ")"
        )
    inp = block.get("input")
    if not isinstance(inp, dict):
        raise OpportunityLLMProtocolError("opportunity tool_use input is not an object")
    _validate_tool_input(inp)
    usage_raw = data.get("usage")
    if usage_raw is None:
        usage_raw = {}
    if not isinstance(usage_raw, dict):
        raise OpportunityLLMProtocolError("opportunity usage is not an object")
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
    return OpportunityLLMResult(
        payload=inp,
        usage=OpportunityLLMUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _validate_tool_input(payload: dict[str, Any]) -> None:
    """Strict Pydantic-free validation matching the prompt's tool schema.

    The parser cannot use Pydantic directly (the schema is a dict); it
    replicates the contract by hand so we keep the same fail-closed
    behaviour as the Phase 3 insight client.
    """
    allowed = {"user_value_score", "reasoning"}
    extra = set(payload.keys()) - allowed
    if extra:
        raise OpportunityLLMProtocolError(
            f"opportunity tool input contains unknown fields: {sorted(extra)}"
        )
    if "user_value_score" not in payload or "reasoning" not in payload:
        raise OpportunityLLMProtocolError("opportunity tool input missing required fields")
    score = payload.get("user_value_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise OpportunityLLMProtocolError("opportunity user_value_score must be a finite number")
    score_f = float(score)
    if not math.isfinite(score_f):
        raise OpportunityLLMProtocolError("opportunity user_value_score must be finite")
    if score_f < 0 or score_f > 10:
        raise OpportunityLLMProtocolError("opportunity user_value_score out of [0, 10]")
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise OpportunityLLMProtocolError("opportunity reasoning must be a non-empty string")
    if len(reasoning) > 500:
        raise OpportunityLLMProtocolError("opportunity reasoning exceeds 500 chars")


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


__all__ = [
    "OpportunityClient",
    "OpportunityLLMConfigError",
    "OpportunityLLMProtocolError",
    "OpportunityLLMResult",
    "OpportunityLLMTransportError",
    "OpportunityLLMUsage",
]
