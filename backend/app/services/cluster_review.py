"""Phase 2 — LLM semantic review for algorithmic cluster candidates.

The review is a *secondary* refinement layer. It receives the algorithmic
cluster candidates produced by :mod:`app.services.clustering` and asks the
LLM to confirm / split / merge / reject them. The runner is the source of
truth: the review output is validated against the input candidate set and
only validated decisions are written to ``cluster_reviews`` and applied to
``clusters`` / ``cluster_members``.

This module deliberately never logs the API key. Errors are typed and safe
to persist: the review row only stores a summary.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Sequence

import httpx

from app.agents.prompts.cluster_review_v4 import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    TOOL_DESCRIPTION,
    TOOL_NAME,
    TOOL_SCHEMA,
)
from app.db.config import settings
from app.models import (
    ClusterReviewAction,
)
from app.services.llm_client import (
    LLMProtocolError,
    LLMResult,
    LLMTransportError,
    LLMUsage,
)

logger = logging.getLogger(__name__)


_ACTIONS = frozenset({a.value for a in ClusterReviewAction})


class ClusterReviewConfigError(RuntimeError):
    """Raised when the LLM environment is not configured."""


class ClusterReviewValidationError(RuntimeError):
    """Validation failure with a safe diagnostic category, never provider text."""

    @property
    def code(self):
        message = str(self)
        for marker, code in (
            ("cluster_index", "cluster_index"),
            ("unknown feedback_id", "unknown_member"),
            ("duplicate", "duplicate_member"),
            ("missing feedback_ids", "missing_member"),
            ("merge target", "merge_target_action"),
            ("keep/merge", "keep_merge_partition"),
            ("split requires", "split_partition"),
            ("groups", "groups_shape"),
            ("reviews length", "review_count"),
            ("unexpected", "unknown_field"),
        ):
            if marker in message:
                return code
        return "invalid_schema"


@dataclass(frozen=True)
class ReviewItem:
    """A single algorithmic cluster candidate passed to the LLM."""

    cluster_id: str
    name_hint: str | None
    members: list[dict]  # list of {feedback_id, content, module, intent, pain_point}


@dataclass(frozen=True)
class ValidatedReview:
    cluster_id: str
    action: str
    reason: str
    name: str
    summary: str
    confidence: float
    groups: list[dict]
    outlier_ids: list[str]
    merge_with_cluster_id: str | None
    raw: dict


def _redact(value: str | None) -> str:
    if value is None:
        return ""
    return " ".join(value.split())


def _build_user_message(items: Sequence[ReviewItem]) -> str:
    payload = {
        "clusters": [
            {
                "cluster_index": index,
                "cluster_id": it.cluster_id,
                "name_hint": it.name_hint,
                "members": [
                    {
                        "feedback_id": m["feedback_id"],
                        "content": m["content"],
                        "module": m["module"],
                        "intent": m["intent"],
                        "pain_point": m["pain_point"],
                    }
                    for m in it.members
                ],
            }
            for index, it in enumerate(items)
        ]
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _is_finite_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def validate_review_payload(
    payload: dict,
    candidates: Sequence[ReviewItem],
) -> list[ValidatedReview]:
    """Validate a structured review payload against the candidate set.

    Strict rules:

    * exactly one review per candidate, in the same order;
    * ``action`` ∈ {keep, split, merge, outlier};
    * every input member appears exactly once across the review's
      ``groups`` + ``outlier_ids``;
    * ``name`` / ``summary`` are non-empty strings of bounded length;
    * ``confidence`` is a finite number in [0, 1].
    """
    if not isinstance(payload, dict):
        raise ClusterReviewValidationError("payload is not an object")
    if set(payload) != {"reviews"}:
        raise ClusterReviewValidationError("unexpected payload fields")
    reviews = payload.get("reviews")
    if not isinstance(reviews, list) or not reviews:
        raise ClusterReviewValidationError("payload missing 'reviews' array")
    if len(reviews) != len(candidates):
        raise ClusterReviewValidationError(
            f"reviews length {len(reviews)} != candidates {len(candidates)}"
        )

    out: list[ValidatedReview] = []
    for idx, review in enumerate(reviews):
        candidate = candidates[idx]
        if not isinstance(review, dict):
            raise ClusterReviewValidationError(f"reviews[{idx}] not object")
        allowed = {
            "cluster_index",
            "action",
            "reason",
            "name",
            "summary",
            "confidence",
            "groups",
            "outlier_ids",
            "merge_with_cluster_index",
        }
        if set(review) - allowed:
            raise ClusterReviewValidationError("unexpected review fields")
        ci = review.get("cluster_index")
        if type(ci) is not int or ci != idx:
            raise ClusterReviewValidationError(f"reviews[{idx}].cluster_index {ci!r} != {idx}")
        action = review.get("action")
        if not isinstance(action, str) or action not in _ACTIONS:
            raise ClusterReviewValidationError(f"reviews[{idx}].action {action!r} invalid")
        reason = review.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ClusterReviewValidationError(f"reviews[{idx}].reason must be non-empty string")
        if len(reason) > 4000:
            raise ClusterReviewValidationError(f"reviews[{idx}].reason too long")
        name = review.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ClusterReviewValidationError(f"reviews[{idx}].name must be non-empty string")
        if len(name) > 200:
            raise ClusterReviewValidationError(f"reviews[{idx}].name too long")
        summary = review.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ClusterReviewValidationError(f"reviews[{idx}].summary must be non-empty string")
        if len(summary) > 4000:
            raise ClusterReviewValidationError(f"reviews[{idx}].summary too long")
        confidence = review.get("confidence")
        if not _is_finite_number(confidence):
            raise ClusterReviewValidationError(f"reviews[{idx}].confidence must be finite number")
        cf = float(confidence)
        if cf < 0.0 or cf > 1.0:
            raise ClusterReviewValidationError(f"reviews[{idx}].confidence {cf} out of [0,1]")
        groups = review.get("groups")
        if not isinstance(groups, list):
            raise ClusterReviewValidationError(f"reviews[{idx}].groups must be a non-empty array")
        outlier_ids = review.get("outlier_ids", [])
        if not isinstance(outlier_ids, list):
            raise ClusterReviewValidationError(f"reviews[{idx}].outlier_ids must be an array")
        merge_with_cluster_id: str | None = None
        if action == "merge":
            merge_with = review.get("merge_with_cluster_index")
            if merge_with is None:
                raise ClusterReviewValidationError(
                    f"reviews[{idx}] merge requires merge_with_cluster_index"
                )
            if type(merge_with) is not int or merge_with < 0 or merge_with >= len(candidates):
                raise ClusterReviewValidationError(
                    f"reviews[{idx}].merge_with_cluster_index out of range"
                )
            merge_with_cluster_id = candidates[merge_with].cluster_id

        if action in {"keep", "merge"} and (len(groups) != 1 or outlier_ids):
            raise ClusterReviewValidationError("keep/merge requires one group and no outliers")
        if action == "split" and (len(groups) < 2 or outlier_ids):
            raise ClusterReviewValidationError("split requires at least two groups and no outliers")
        if action == "outlier" and len(groups) > 1:
            raise ClusterReviewValidationError("outlier allows at most one retained group")
        if action != "merge" and review.get("merge_with_cluster_index") is not None:
            raise ClusterReviewValidationError("merge target only allowed for merge")
        seen: set[str] = set()
        member_ids = {m["feedback_id"] for m in candidate.members}
        cleaned_groups: list[dict] = []
        for g_idx, group in enumerate(groups):
            if not isinstance(group, dict):
                raise ClusterReviewValidationError(f"reviews[{idx}].groups[{g_idx}] not object")
            g_name = group.get("name")
            if not isinstance(g_name, str) or not g_name.strip():
                raise ClusterReviewValidationError(f"reviews[{idx}].groups[{g_idx}].name invalid")
            if len(g_name) > 200:
                raise ClusterReviewValidationError(f"reviews[{idx}].groups[{g_idx}].name too long")
            ids = group.get("feedback_ids")
            if not isinstance(ids, list) or not ids:
                raise ClusterReviewValidationError(
                    f"reviews[{idx}].groups[{g_idx}].feedback_ids invalid"
                )
            for fid in ids:
                if not isinstance(fid, str) or not fid:
                    raise ClusterReviewValidationError(
                        f"reviews[{idx}].groups[{g_idx}].feedback_ids entry invalid"
                    )
                if fid not in member_ids:
                    raise ClusterReviewValidationError(
                        f"reviews[{idx}] references unknown feedback_id {fid!r}"
                    )
                if fid in seen:
                    raise ClusterReviewValidationError(
                        f"reviews[{idx}] duplicate feedback_id {fid!r}"
                    )
                seen.add(fid)
            cleaned_groups.append({"name": g_name, "feedback_ids": list(ids)})
        cleaned_outliers: list[str] = []
        for fid in outlier_ids:
            if not isinstance(fid, str) or not fid:
                raise ClusterReviewValidationError(f"reviews[{idx}].outlier_ids entry invalid")
            if fid not in member_ids:
                raise ClusterReviewValidationError(
                    f"reviews[{idx}] outlier references unknown feedback_id {fid!r}"
                )
            if fid in seen:
                raise ClusterReviewValidationError(f"reviews[{idx}] outlier duplicate {fid!r}")
            seen.add(fid)
            cleaned_outliers.append(fid)
        missing = member_ids - seen
        if missing:
            raise ClusterReviewValidationError(
                f"reviews[{idx}] missing feedback_ids {sorted(missing)}"
            )

        out.append(
            ValidatedReview(
                cluster_id=candidate.cluster_id,
                action=action,
                reason=_redact(reason),
                name=name.strip(),
                summary=_redact(summary),
                confidence=cf,
                groups=cleaned_groups,
                outlier_ids=cleaned_outliers,
                merge_with_cluster_id=merge_with_cluster_id,
                raw=review,
            )
        )
    # Detect merge-with clusters that are themselves being merged: a cluster
    # cannot merge with a cluster that is itself marked merge.
    for r in out:
        if r.action != "merge":
            continue
        target = next((x for x in out if x.cluster_id == r.merge_with_cluster_id), None)
        if target is None or target.action != "keep":
            raise ClusterReviewValidationError(
                f"merge target {r.merge_with_cluster_id!r} is also a merge"
            )
    return out


class ClusterReviewClient:
    """Thin synchronous client around an OpenAI-compatible chat endpoint."""

    def __init__(self) -> None:
        if not (settings.llm_api_key and settings.llm_base_url and settings.llm_model):
            raise ClusterReviewConfigError(
                "LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 未配置；无法执行 cluster review。"
            )
        self.validation_feedback = None
        self._base_url = settings.llm_base_url.rstrip("/")
        self._model = settings.llm_model
        self._timeout = settings.llm_timeout_seconds
        self._client = httpx.Client(
            timeout=self._timeout,
            headers={
                "x-api-key": settings.llm_api_key or "",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ClusterReviewClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def review_clusters(self, items: Sequence[ReviewItem]) -> LLMResult:
        if not items:
            raise ClusterReviewValidationError("review_clusters called with empty input")
        user_message = _build_user_message(items)
        if self.validation_feedback:
            user_message += (
                "\nPrevious output failed backend validation: "
                + self.validation_feedback
                + ". Re-check all action shapes and return the complete corrected reviews array."
            )
        body = {
            "model": self._model,
            "max_tokens": min(16000, max(4096, len(items) * 450)),
            "system": SYSTEM_PROMPT,
            "tools": [
                {
                    "name": TOOL_NAME,
                    "description": TOOL_DESCRIPTION,
                    "input_schema": TOOL_SCHEMA,
                }
            ],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
            "messages": [{"role": "user", "content": user_message}],
        }
        url = f"{self._base_url}/v1/messages"
        try:
            response = self._client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise LLMTransportError("cluster review request failed") from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise LLMTransportError(f"cluster review transient HTTP {response.status_code}")
        if response.status_code in (401, 403):
            raise LLMProtocolError(
                f"cluster review authentication rejected (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            raise LLMProtocolError(f"cluster review client error (HTTP {response.status_code})")
        try:
            data = response.json()
        except Exception as exc:  # pragma: no cover
            raise LLMProtocolError("cluster review returned non-JSON body") from exc
        return _parse_messages_response(data)


def _parse_messages_response(data: dict) -> LLMResult:
    """Strict Anthropic-compatible parser for cluster review responses."""
    if not isinstance(data, dict):
        raise LLMProtocolError("LLM response is not a JSON object")
    content = data.get("content")
    if not isinstance(content, list) or not content:
        raise LLMProtocolError("LLM response missing content array")
    tool_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
    if not tool_blocks:
        raise LLMProtocolError("LLM response missing tool_use block")
    if len(tool_blocks) > 1:
        raise LLMProtocolError(
            f"LLM response contains {len(tool_blocks)} tool_use blocks; expected exactly 1"
        )
    block = tool_blocks[0]
    if block.get("name") != TOOL_NAME:
        raise LLMProtocolError("LLM response uses unexpected tool (not " + TOOL_NAME + ")")
    inp = block.get("input")
    if not isinstance(inp, dict):
        raise LLMProtocolError("LLM tool_use input is not an object")
    usage_raw = data.get("usage")
    if usage_raw is None:
        usage_raw = {}
    if not isinstance(usage_raw, dict):
        raise LLMProtocolError("LLM response usage is not an object")
    input_tokens = _parse_token_count(usage_raw.get("input_tokens"))
    if input_tokens is not None:
        for field in ("cache_creation_input_tokens", "cache_read_input_tokens"):
            if field in usage_raw:
                component = _parse_token_count(usage_raw[field])
                if component is None:
                    input_tokens = None
                    break
                input_tokens += component
    usage = LLMUsage(
        input_tokens=input_tokens,
        output_tokens=_parse_token_count(usage_raw.get("output_tokens")),
    )
    return LLMResult(payload=inp, usage=usage)


def _parse_token_count(value) -> int | None:
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


def is_cluster_review_configured() -> bool:
    return bool(settings.llm_api_key and settings.llm_base_url and settings.llm_model)


__all__ = [
    "ClusterReviewClient",
    "ClusterReviewConfigError",
    "ClusterReviewValidationError",
    "PROMPT_VERSION",
    "ReviewItem",
    "ValidatedReview",
    "is_cluster_review_configured",
    "validate_review_payload",
]
