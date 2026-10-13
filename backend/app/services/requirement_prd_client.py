"""Phase 5 — Requirement + PRD LLM clients.

Two thin synchronous wrappers around an Anthropic-compatible Messages
API. Each one targets a single ``submit_*`` tool call:

* :class:`RequirementClient` returns one Requirement draft payload.
* :class:`PRDClient` returns one PRD draft payload.

Both clients share the same parser discipline as the Phase 3/4 clients:

* No upstream body / token / sentinel leaks. Error categorisation
  distinguishes transport (retriable) from protocol / configuration
  (immediate failure).
* Strict tool-name + input-schema check; the schema comes from the
  matching prompt module and unknown keys raise immediately.
* Cache-token support (``cache_creation_input_tokens`` +
  ``cache_read_input_tokens``) sums into ``input_tokens``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

import httpx

from app.agents.prompts.prd_generation_v1 import SYSTEM_PROMPT as PRD_SYSTEM_PROMPT
from app.agents.prompts.prd_generation_v1 import TOOL_DESCRIPTION as PRD_TOOL_DESCRIPTION
from app.agents.prompts.prd_generation_v1 import TOOL_NAME as PRD_TOOL_NAME
from app.agents.prompts.prd_generation_v1 import TOOL_SCHEMA as PRD_TOOL_SCHEMA
from app.agents.prompts.requirement_generation_v1 import SYSTEM_PROMPT as REQ_SYSTEM_PROMPT
from app.agents.prompts.requirement_generation_v1 import TOOL_DESCRIPTION as REQ_TOOL_DESCRIPTION
from app.agents.prompts.requirement_generation_v1 import TOOL_NAME as REQ_TOOL_NAME
from app.agents.prompts.requirement_generation_v1 import TOOL_SCHEMA as REQ_TOOL_SCHEMA


class RequirementLLMConfigError(RuntimeError):
    """Missing LLM environment variables."""


class RequirementLLMTransportError(RuntimeError):
    """Network / transient HTTP failure (retriable)."""


class RequirementLLMProtocolError(RuntimeError):
    """Malformed JSON, missing tool block, or wrong tool name."""


class PRDLLMConfigError(RuntimeError):
    """Missing LLM environment variables."""


class PRDLLMTransportError(RuntimeError):
    """Network / transient HTTP failure (retriable)."""


class PRDLLMProtocolError(RuntimeError):
    """Malformed JSON, missing tool block, or wrong tool name."""


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True)
class RequirementLLMResult:
    payload: dict[str, Any]
    usage: LLMUsage


@dataclass(frozen=True)
class PRDLLMResult:
    payload: dict[str, Any]
    usage: LLMUsage


# ---- Shared HTTP plumbing ---------------------------------------------------


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


def _parse_usage(usage_raw: Any) -> LLMUsage:
    if usage_raw is None:
        usage_raw = {}
    if not isinstance(usage_raw, dict):
        raise RequirementLLMProtocolError("LLM usage is not an object")
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
    return LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens)


def _post_messages(
    *,
    client: httpx.Client,
    base_url: str,
    body: dict[str, Any],
    tool_name: str,
    protocol_error_cls: type[Exception],
) -> dict[str, Any]:
    url = f"{base_url}/v1/messages"
    try:
        response = client.post(url, json=body)
    except httpx.HTTPError as exc:
        raise RequirementLLMTransportError(f"{tool_name} request failed") from exc
    if response.status_code >= 500 or response.status_code == 429:
        raise RequirementLLMTransportError(f"{tool_name} transient HTTP {response.status_code}")
    if response.status_code in (401, 403):
        raise protocol_error_cls(
            f"{tool_name} authentication rejected (HTTP {response.status_code})"
        )
    if response.status_code >= 400:
        raise protocol_error_cls(f"{tool_name} client error (HTTP {response.status_code})")
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise protocol_error_cls(f"{tool_name} returned non-JSON body") from exc


def _extract_tool_payload(
    *,
    data: dict[str, Any],
    tool_name: str,
    allowed_keys: set[str],
    protocol_error_cls: type[Exception],
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise protocol_error_cls(f"{tool_name} response is not a JSON object")
    content = data.get("content")
    if not isinstance(content, list) or not content:
        raise protocol_error_cls(f"{tool_name} response missing content array")
    tool_blocks = [
        block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    if not tool_blocks:
        raise protocol_error_cls(f"{tool_name} response missing tool_use block")
    if len(tool_blocks) > 1:
        raise protocol_error_cls(
            f"{tool_name} response contains {len(tool_blocks)} tool_use blocks; expected exactly 1"
        )
    block = tool_blocks[0]
    if block.get("name") != tool_name:
        raise protocol_error_cls(f"{tool_name} response uses unexpected tool (not {tool_name})")
    inp = block.get("input")
    if not isinstance(inp, dict):
        raise protocol_error_cls(f"{tool_name} tool_use input is not an object")
    extras = set(inp.keys()) - allowed_keys
    if extras:
        # Strict Structured Output: the model is told it may only return
        # fields listed in the tool schema. Silently dropping unknown
        # keys would let the LLM smuggle in fields the persistence
        # layer would then ignore. Reject the tool_use block instead
        # so the caller can decide whether to retry or surface 503.
        raise protocol_error_cls(
            f"{tool_name} tool input contains unknown fields: {sorted(extras)}"
        )
    return inp


# ---- Requirement client ------------------------------------------------------


class RequirementClient:
    """Synchronous Phase 5 Requirement client."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        model: str | None,
        timeout_seconds: float,
    ) -> None:
        if not api_key:
            raise RequirementLLMConfigError("LLM_API_KEY is not configured")
        if not base_url:
            raise RequirementLLMConfigError("LLM_BASE_URL is not configured")
        if not model:
            raise RequirementLLMConfigError("LLM_MODEL is not configured")
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

    def __enter__(self) -> "RequirementClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def generate_requirement(self, payload: dict[str, Any]) -> RequirementLLMResult:
        if not payload:
            raise RequirementLLMProtocolError("generate_requirement called with empty payload")
        body = {
            "model": self._model,
            "max_tokens": 4096,
            "system": REQ_SYSTEM_PROMPT,
            "tools": [
                {
                    "name": REQ_TOOL_NAME,
                    "description": REQ_TOOL_DESCRIPTION,
                    "input_schema": REQ_TOOL_SCHEMA,
                }
            ],
            "tool_choice": {"type": "tool", "name": REQ_TOOL_NAME},
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(
                        {"requirement": payload}, ensure_ascii=False, separators=(",", ":")
                    ),
                }
            ],
        }
        data = _post_messages(
            client=self._client,
            base_url=self._base_url,
            body=body,
            tool_name=REQ_TOOL_NAME,
            protocol_error_cls=RequirementLLMProtocolError,
        )
        allowed = set(REQ_TOOL_SCHEMA["properties"].keys())
        inp = _extract_tool_payload(
            data=data,
            tool_name=REQ_TOOL_NAME,
            allowed_keys=allowed,
            protocol_error_cls=RequirementLLMProtocolError,
        )
        return RequirementLLMResult(payload=inp, usage=_parse_usage(data.get("usage")))


# ---- PRD client --------------------------------------------------------------


class PRDClient:
    """Synchronous Phase 5 PRD client."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        model: str | None,
        timeout_seconds: float,
    ) -> None:
        if not api_key:
            raise PRDLLMConfigError("LLM_API_KEY is not configured")
        if not base_url:
            raise PRDLLMConfigError("LLM_BASE_URL is not configured")
        if not model:
            raise PRDLLMConfigError("LLM_MODEL is not configured")
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

    def __enter__(self) -> "PRDClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def generate_prd(self, payload: dict[str, Any]) -> PRDLLMResult:
        if not payload:
            raise PRDLLMProtocolError("generate_prd called with empty payload")
        body = {
            "model": self._model,
            "max_tokens": 8192,
            "system": PRD_SYSTEM_PROMPT,
            "tools": [
                {
                    "name": PRD_TOOL_NAME,
                    "description": PRD_TOOL_DESCRIPTION,
                    "input_schema": PRD_TOOL_SCHEMA,
                }
            ],
            "tool_choice": {"type": "tool", "name": PRD_TOOL_NAME},
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(
                        {"prd": payload}, ensure_ascii=False, separators=(",", ":")
                    ),
                }
            ],
        }
        data = _post_messages(
            client=self._client,
            base_url=self._base_url,
            body=body,
            tool_name=PRD_TOOL_NAME,
            protocol_error_cls=PRDLLMProtocolError,
        )
        allowed = set(PRD_TOOL_SCHEMA["properties"].keys())
        inp = _extract_tool_payload(
            data=data,
            tool_name=PRD_TOOL_NAME,
            allowed_keys=allowed,
            protocol_error_cls=PRDLLMProtocolError,
        )
        return PRDLLMResult(payload=inp, usage=_parse_usage(data.get("usage")))


__all__ = [
    "LLMUsage",
    "PRDLLMConfigError",
    "PRDLLMProtocolError",
    "PRDLLMResult",
    "PRDLLMTransportError",
    "PRDClient",
    "RequirementLLMConfigError",
    "RequirementLLMProtocolError",
    "RequirementLLMResult",
    "RequirementLLMTransportError",
    "RequirementClient",
]
