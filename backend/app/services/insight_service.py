"""Phase 3 — Insight service: deterministic evidence selection and payload validation.

Two responsibilities live here:

1. :func:`select_evidence_candidates` — picks up to 8 evidence rows for a
   cluster with a deterministic preference (representative by similarity,
   highest-severity, diverse expression). Clusters with fewer candidates
   get every member passed through (the LLM may then return fewer than
   three and the runner marks the insight ``needs_validation``).

2. :func:`validate_insight_payload` — strict business validator for the
   LLM structured output. The validator rejects:

   * non-object payloads and unknown top-level fields (LLM is not allowed
     to invent fields like ``quote``);
   * unknown per-evidence fields (same contract);
   * missing / non-string fields;
   * strings shorter than ``minLength=1`` or longer than ``maxLength``
     (matches the LLM tool schema);
   * non-finite ``confidence`` (NaN / Inf) or values outside [0, 1];
   * ``evidence_type`` outside the allowed set;
   * non-finite ``relevance_score`` or values outside [0, 1];
   * ``feedback_id`` outside the allowed set, duplicates, or empty;
   * evidence arrays with fewer than ``min_evidence`` or more than 8
     entries; the runner passes ``min_evidence=3`` for normal clusters
     and ``min_evidence=1`` for clusters with one or two candidates;
   * ``bool`` smuggled in as a number.

   String fields are bounded in length (matches the DB CHECK). The
   validator returns a :class:`ValidatedInsight` instance the runner
   persists; failures raise :class:`InsightValidationError` with a
   stable ``code`` the runner maps to retry/no-retry.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Sequence

from app.models import EvidenceType
from app.schemas.insight import EvidenceEntry

ALLOWED_EVIDENCE_TYPES: frozenset[str] = frozenset(t.value for t in EvidenceType)

ALLOWED_TOP_LEVEL_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "pain_point",
        "affected_segment",
        "insight",
        "recommendation",
        "confidence",
        "evidence",
    }
)

ALLOWED_EVIDENCE_FIELDS: frozenset[str] = frozenset(
    {"feedback_id", "evidence_type", "relevance_score"}
)

EVIDENCE_SELECTION_MAX = 8
EVIDENCE_SELECTION_MIN = 1
DRAFT_EVIDENCE_THRESHOLD = 3


class InsightValidationError(RuntimeError):
    """Validation failure with a safe category code (no provider text)."""

    @property
    def code(self) -> str:
        message = str(self)
        for marker, code in (
            ("title", "title_invalid"),
            ("pain_point", "pain_point_invalid"),
            ("affected_segment", "affected_segment_invalid"),
            ("insight", "insight_invalid"),
            ("recommendation", "recommendation_invalid"),
            ("confidence", "confidence_invalid"),
            ("evidence_type", "evidence_type_invalid"),
            ("relevance_score", "relevance_score_invalid"),
            ("feedback_id", "feedback_id_invalid"),
            ("duplicate feedback", "duplicate_evidence_feedback"),
            ("unknown feedback", "unknown_evidence_feedback"),
            ("too many evidence", "evidence_count_too_many"),
            ("too few evidence", "evidence_count_too_few"),
            ("unknown top-level field", "unknown_top_level_field"),
            ("unknown evidence field", "unknown_evidence_field"),
            ("unknown top-level fields", "unknown_top_level_field"),
            ("unknown evidence fields", "unknown_evidence_field"),
            ("maximum length", "field_too_long"),
        ):
            if marker in message:
                return code
        return "schema_invalid"


@dataclass(frozen=True)
class EvidenceCandidate:
    feedback_id: str  # feedbacks.id (UUID str) — the canonical reference for LLM evidence
    feedback_pk: str  # feedbacks.id (UUID str) — same value; kept for clarity
    external_id: str  # external_id, displayed and persisted on evidence
    content: str  # raw text, read from DB and never tampered
    similarity_score: float
    severity: int | None
    sentiment: str | None


@dataclass(frozen=True)
class ValidatedInsight:
    title: str
    pain_point: str
    affected_segment: str
    insight: str
    recommendation: str
    confidence: float
    evidence: tuple[EvidenceEntry, ...]


def select_evidence_candidates(
    candidates: Sequence[EvidenceCandidate],
    *,
    max_evidence: int = EVIDENCE_SELECTION_MAX,
) -> list[EvidenceCandidate]:
    """Deterministic selection of up to ``max_evidence`` rows per cluster.

    Preference order:
      * The single most-similar representative (``similarity_score``).
      * Top-2 highest-severity rows (severity descending, similarity break
        tie); skipped if the cluster has fewer than 3 rows or there is no
        measurable severity.
      * Up to 5 diverse-expression rows chosen by ascending similarity so
        the cluster still covers the long-tail content.

    Returns the selected candidates in deterministic order. ``max_evidence``
    is the hard ceiling; clusters smaller than the cap get every member.
    """
    if not candidates:
        return []
    if len(candidates) <= max_evidence:
        return sorted(candidates, key=lambda c: (-c.similarity_score, c.external_id))
    by_similarity = sorted(candidates, key=lambda c: (-c.similarity_score, c.external_id))
    representative = by_similarity[0]
    severity_rows: list[EvidenceCandidate] = []
    if any(c.severity is not None for c in candidates):
        by_severity = sorted(
            candidates,
            key=lambda c: (
                -(c.severity if c.severity is not None else 0),
                -c.similarity_score,
                c.external_id,
            ),
        )
        severity_rows = [c for c in by_severity if c.severity is not None]
    diverse_pool = sorted(candidates, key=lambda c: (c.similarity_score, c.external_id))
    selected: list[EvidenceCandidate] = []
    seen: set[str] = set()

    def _add(candidate: EvidenceCandidate) -> None:
        if candidate.feedback_id in seen:
            return
        seen.add(candidate.feedback_id)
        selected.append(candidate)

    _add(representative)
    for row in severity_rows[:2]:
        _add(row)
    for row in diverse_pool:
        if len(selected) >= max_evidence:
            break
        _add(row)
    # Backfill in similarity order if any category was empty.
    for row in by_similarity:
        if len(selected) >= max_evidence:
            break
        _add(row)
    return selected


def validate_insight_payload(
    payload: dict,
    *,
    allowed_feedback_ids: Sequence[str],
    min_evidence: int = EVIDENCE_SELECTION_MIN,
    max_evidence: int = EVIDENCE_SELECTION_MAX,
) -> ValidatedInsight:
    """Strict business validator for one structured insight payload.

    ``allowed_feedback_ids`` must be the cluster member feedback UUIDs
    (the canonical references for evidence). The function rejects unknown
    top-level / evidence fields first so the LLM cannot smuggle in
    auxiliary context like ``quote``.
    """
    if not isinstance(payload, dict):
        raise InsightValidationError("payload is not an object")

    unknown_top = set(payload.keys()) - ALLOWED_TOP_LEVEL_FIELDS
    if unknown_top:
        raise InsightValidationError(f"unknown top-level field(s): {sorted(unknown_top)}")

    title = _require_string(payload.get("title"), "title", max_length=200)
    pain_point = _require_string(payload.get("pain_point"), "pain_point", max_length=4000)
    affected_segment = _require_string(
        payload.get("affected_segment"), "affected_segment", max_length=4000
    )
    insight_text = _require_string(payload.get("insight"), "insight", max_length=4000)
    recommendation = _require_string(
        payload.get("recommendation"), "recommendation", max_length=4000
    )
    confidence = _require_finite_score(payload.get("confidence"), "confidence", lo=0.0, hi=1.0)

    evidence_raw = payload.get("evidence")
    if not isinstance(evidence_raw, list):
        raise InsightValidationError("evidence is not an array")
    if len(evidence_raw) < min_evidence:
        raise InsightValidationError("too few evidence entries")
    if len(evidence_raw) > max_evidence:
        raise InsightValidationError("too many evidence entries")

    allowed = set(allowed_feedback_ids)
    cleaned: list[EvidenceEntry] = []
    seen: set[str] = set()
    counter: Counter[str] = Counter()
    for idx, entry in enumerate(evidence_raw):
        if not isinstance(entry, dict):
            raise InsightValidationError(f"evidence[{idx}] is not an object")
        unknown_ev = set(entry.keys()) - ALLOWED_EVIDENCE_FIELDS
        if unknown_ev:
            raise InsightValidationError(
                f"evidence[{idx}] unknown evidence field(s): {sorted(unknown_ev)}"
            )
        feedback_id = entry.get("feedback_id")
        if not isinstance(feedback_id, str) or not feedback_id:
            raise InsightValidationError(f"evidence[{idx}].feedback_id missing or invalid")
        if feedback_id in seen:
            raise InsightValidationError(f"duplicate feedback_id {feedback_id!r} in evidence")
        if feedback_id not in allowed:
            raise InsightValidationError(f"unknown feedback_id {feedback_id!r} not in cluster")
        evidence_type = entry.get("evidence_type")
        if not isinstance(evidence_type, str) or evidence_type not in ALLOWED_EVIDENCE_TYPES:
            raise InsightValidationError(f"evidence[{idx}].evidence_type {evidence_type!r} invalid")
        relevance = _require_finite_score(
            entry.get("relevance_score"),
            f"evidence[{idx}].relevance_score",
            lo=0.0,
            hi=1.0,
        )
        cleaned.append(
            EvidenceEntry(
                feedback_id=feedback_id,
                evidence_type=evidence_type,
                relevance_score=relevance,
            )
        )
        seen.add(feedback_id)
        counter[feedback_id] += 1
    duplicates = {fid for fid, n in counter.items() if n > 1}
    if duplicates:
        raise InsightValidationError(f"duplicate feedback in evidence: {sorted(duplicates)}")
    return ValidatedInsight(
        title=title,
        pain_point=pain_point,
        affected_segment=affected_segment,
        insight=insight_text,
        recommendation=recommendation,
        confidence=confidence,
        evidence=tuple(cleaned),
    )


def _require_string(value: object, field: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise InsightValidationError(f"{field} must be a string")
    if not value.strip():
        raise InsightValidationError(f"{field} must not be empty")
    if len(value) > max_length:
        raise InsightValidationError(f"{field} exceeds maximum length {max_length}")
    return value


def _require_finite_score(value: object, field: str, *, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InsightValidationError(f"{field} must be a finite number")
    num = float(value)
    if not math.isfinite(num):
        raise InsightValidationError(f"{field} must be finite")
    if num < lo or num > hi:
        raise InsightValidationError(f"{field} {num} out of [{lo}, {hi}]")
    return num


__all__ = [
    "ALLOWED_EVIDENCE_FIELDS",
    "ALLOWED_EVIDENCE_TYPES",
    "ALLOWED_TOP_LEVEL_FIELDS",
    "DRAFT_EVIDENCE_THRESHOLD",
    "EVIDENCE_SELECTION_MAX",
    "EVIDENCE_SELECTION_MIN",
    "EvidenceCandidate",
    "InsightValidationError",
    "ValidatedInsight",
    "select_evidence_candidates",
    "validate_insight_payload",
]
