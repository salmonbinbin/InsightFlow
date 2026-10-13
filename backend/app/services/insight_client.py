"""Phase 3 — Insight Generation LLM client.

A thin synchronous wrapper around an Anthropic-compatible Messages API
that returns exactly one ``submit_cluster_insight`` tool call for the
supplied cluster payload. The structured payload is parsed
deterministically; downstream code performs the per-field / per-ID
business validation against the cluster's evidence candidates.

The client deliberately never logs or echoes ``api_key``. Errors are
surfaced as typed exceptions (``InsightLLMTransportError`` /
``InsightLLMProtocolError``) so the runner can decide between retry,
budget consumption, and persistence.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

import httpx

from app.agents.prompts.insight_generation_v2 import (
    SYSTEM_PROMPT,
    TOOL_DESCRIPTION,
    TOOL_NAME,
    TOOL_SCHEMA,
)


class InsightLLMConfigError(RuntimeError):
    """Missing LLM environment variables."""


class InsightLLMTransportError(RuntimeError):
    """Network or transient HTTP failure (retriable)."""


class InsightLLMProtocolError(RuntimeError):
    """Malformed JSON, missing tool block, or wrong tool name."""


@dataclass(frozen=True)
class InsightLLMUsage:
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True)
class InsightLLMResult:
    payload: dict[str, Any]
    usage: InsightLLMUsage


class InsightClient:
    """Synchronous Phase 3 insight client (one tool call per cluster)."""

    def __init__(
        self,
        api_key: str | None,
        base_url: str | None,
        model: str | None,
        timeout_seconds: float,
    ) -> None:
        if not api_key:
            raise InsightLLMConfigError("LLM_API_KEY is not configured")
        if not base_url:
            raise InsightLLMConfigError("LLM_BASE_URL is not configured")
        if not model:
            raise InsightLLMConfigError("LLM_MODEL is not configured")
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

    def __enter__(self) -> "InsightClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def generate_insight(self, cluster_payload: dict[str, Any]) -> InsightLLMResult:
        if not cluster_payload:
            raise InsightLLMProtocolError("generate_insight called with empty cluster payload")
        body = {
            "model": self._model,
            "max_tokens": 4096,
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
                        {"cluster": cluster_payload}, ensure_ascii=False, separators=(",", ":")
                    ),
                }
            ],
        }
        url = f"{self._base_url}/v1/messages"
        try:
            response = self._client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise InsightLLMTransportError("insight request failed") from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise InsightLLMTransportError(f"insight transient HTTP {response.status_code}")
        if response.status_code in (401, 403):
            raise InsightLLMProtocolError(
                f"insight authentication rejected (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            raise InsightLLMProtocolError(f"insight client error (HTTP {response.status_code})")
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise InsightLLMProtocolError("insight returned non-JSON body") from exc
        return _parse_messages_response(data)


def _parse_messages_response(data: dict[str, Any]) -> InsightLLMResult:
    """Strict Anthropic-compatible Messages parser.

    Mirrors :func:`app.services.llm_client._parse_messages_response` so
    Phase 1 / Phase 2 / Phase 3 all reject the same injection shapes
    (missing tool block, wrong tool name, more than one tool block,
    non-object tool ``input``).
    """
    if not isinstance(data, dict):
        raise InsightLLMProtocolError("insight response is not a JSON object")
    content = data.get("content")
    if not isinstance(content, list) or not content:
        raise InsightLLMProtocolError("insight response missing content array")
    tool_blocks = [
        block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    if not tool_blocks:
        raise InsightLLMProtocolError("insight response missing " + TOOL_NAME + " tool_use block")
    if len(tool_blocks) > 1:
        raise InsightLLMProtocolError(
            f"insight response contains {len(tool_blocks)} tool_use blocks; expected exactly 1"
        )
    block = tool_blocks[0]
    if block.get("name") != TOOL_NAME:
        raise InsightLLMProtocolError(
            "insight response uses unexpected tool (not " + TOOL_NAME + ")"
        )
    inp = block.get("input")
    if not isinstance(inp, dict):
        raise InsightLLMProtocolError("insight tool_use input is not an object")
    usage_raw = data.get("usage")
    if usage_raw is None:
        usage_raw = {}
    if not isinstance(usage_raw, dict):
        raise InsightLLMProtocolError("insight usage is not an object")
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
    return InsightLLMResult(
        payload=inp,
        usage=InsightLLMUsage(input_tokens=input_tokens, output_tokens=output_tokens),
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


__all__ = [
    "InsightClient",
    "InsightLLMConfigError",
    "InsightLLMProtocolError",
    "InsightLLMResult",
    "InsightLLMTransportError",
    "InsightLLMUsage",
]
