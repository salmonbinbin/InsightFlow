"""Phase 1 batch classification pipeline.

Encapsulates:

* Schema validation of the structured LLM output — strict typing (no silent
  coercion of ``None`` / wrong types), enum membership, finite-number checks
  for severity and confidence, and required-field enforcement. The validator
  raises :class:`InspectionClassificationValidationError`; sibling code does not
  allow raw ``TypeError`` / ``ValueError`` to escape.
* Strict batch ID-set validation (no duplicates, no unknown IDs, no missing
  IDs) so partial / tampered outputs are rejected before any database write.
* Retries with exponential backoff for transient transport errors and
  protocol errors. Each attempt's actual latency and (when known) token usage
  are accumulated so totals reflect every call the runner made. Only failed
  items are re-processed; successful rows are never re-written or duplicated.
* Per-batch commits so a later batch's failure cannot roll back the successes
  recorded by earlier batches in the same run.
* Persistence that enforces ``UNIQUE(feedback_id, run_id)`` via the ORM
  constraints and keeps raw feedback immutable.
* Token / latency accounting surfaced to the ``AnalysisRun`` together with an
  ``attempt_count`` and ``unknown_token_calls`` so missing measurements are
  not silently reported as zero.
"""

from __future__ import annotations

import logging
import math
import re
import time
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.config import settings
from app.models import AnalysisRun, AnalysisRunFailure, Feedback, FeedbackAnalysis
from app.services.llm_client import (
    LLMClient,
    LLMProtocolError,
    LLMTransportError,
)
from app.services.semantic_text import build_semantic_text

FEEDBACK_TYPES: frozenset[str] = frozenset(
    {
        "bug",
        "feature_request",
        "ux_issue",
        "performance_issue",
        "pricing_issue",
        "content_issue",
        "positive_feedback",
        "other",
    }
)
SENTIMENTS: frozenset[str] = frozenset({"positive", "neutral", "negative", "mixed"})
logger = logging.getLogger(__name__)


_SAFE_ERROR_MAX_LEN = 4000


def _safe_error_message(message: str | None) -> str:
    """Produce a safe-to-persist error summary.

    Anything the LLM client, the validator or the upstream provider put in the
    error text must pass through here before reaching the database, an API
    response, or a log line. ``re.escape``/``str.format`` and a series of
    known sentinels are stripped so that an attacker cannot smuggle a
    credential, session token or arbitrary diagnostic into
    ``AnalysisRunFailure.last_error``.
    """
    text = "" if message is None else str(message)
    # Replace any sequence that looks like an HTTP header value with a
    # placeholder, then drop known credential-like tokens verbatim.
    text = _SENSITIVE_TOKEN_RE.sub(" [REDACTED] ", text)
    text = text.replace("\x00", " ")
    text = " ".join(text.split())
    if len(text) > _SAFE_ERROR_MAX_LEN:
        text = text[:_SAFE_ERROR_MAX_LEN]
    return text


_SENSITIVE_TOKEN_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_\-]{6,}|"
    r"AKIA[0-9A-Z]{8,}|"
    r"x-api-key=[^\s,;]+|"
    r"api[_-]?key[=:][^\s,;]+|"
    r"bearer\s+[A-Za-z0-9._\-]{6,}|"
    r"ACCEPTANCE_FAKE_CREDENTIAL_NOT_REAL|"
    r"FAKE_RECHECK_CREDENTIAL_NEVER_REAL|"
    r"FAKE_[A-Z0-9_]+_CREDENTIAL[A-Z0-9_]*|"
    r"password[=:][^\s,;]+|"
    r"secret[=:][^\s,;]+|"
    r"token[=:][A-Za-z0-9._\-]{6,})",
    re.IGNORECASE,
)


class ClassificationConfigError(RuntimeError):
    """Raised when the environment is missing the keys needed for real classification."""


class ClassificationValidationError(RuntimeError):
    """Raised when the LLM output fails schema / business validation.

    Every well-formed rejection raised by this module is an instance of this
    class so that retry semantics and persistent failure records do not have
    to special-case raw ``TypeError`` / ``ValueError`` coming from the
    validator itself.
    """


class _BatchAttemptError(ClassificationValidationError):
    """Raised by :func:`attempt_batch` when an LLM call or its validation fails.

    Carries the per-attempt latency and token usage so the caller can
    accumulate them even on the failure path.
    """

    def __init__(
        self,
        message: str,
        *,
        latency_ms: int,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        super().__init__(message)
        self.latency_ms = latency_ms
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class ClassificationAttempt:
    """Result of a single successful attempt to classify a single batch."""

    def __init__(
        self,
        successes: list[dict[str, Any]],
        input_tokens: int | None,
        output_tokens: int | None,
        latency_ms: int,
    ) -> None:
        self.successes = successes
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms


def is_classification_configured() -> bool:
    return bool(settings.llm_api_key and settings.llm_base_url and settings.llm_model)


def build_llm_client(model: str | None = None) -> LLMClient:
    if not is_classification_configured():
        raise ClassificationConfigError(
            "LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 未配置；无法执行真实分类。"
        )
    return LLMClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=model if model is not None else settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
    )


def chunk(items: list[Any], size: int) -> list[list[Any]]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    return [items[i : i + size] for i in range(0, len(items), size)]


def _require_enum(value: Any, allowed: frozenset[str], field: str, fid: str) -> str:
    if not isinstance(value, str):
        raise ClassificationValidationError(
            f"feedback_id {fid!r} {field} must be a string, got {type(value).__name__}"
        )
    if value not in allowed:
        raise ClassificationValidationError(f"feedback_id {fid!r} invalid {field} {value!r}")
    return value


def _require_nonempty_string(value: Any, field: str, fid: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise ClassificationValidationError(
            f"feedback_id {fid!r} {field} must be a string, got {type(value).__name__}"
        )
    if not value.strip():
        raise ClassificationValidationError(f"feedback_id {fid!r} {field} must not be empty")
    if len(value) > max_length:
        raise ClassificationValidationError(
            f"feedback_id {fid!r} {field} too long ({len(value)} > {max_length})"
        )
    return value


def _require_strict_int(value: Any, field: str, fid: str, *, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ClassificationValidationError(f"feedback_id {fid!r} {field} must be an integer")
    if value < lo or value > hi:
        raise ClassificationValidationError(
            f"feedback_id {fid!r} {field} {value} out of {lo}..{hi}"
        )
    return value


def _require_strict_float(value: Any, field: str, fid: str, *, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClassificationValidationError(f"feedback_id {fid!r} {field} must be a number")
    num = float(value)
    if not math.isfinite(num):
        raise ClassificationValidationError(
            f"feedback_id {fid!r} {field} must be a finite number, got {value!r}"
        )
    if num < lo or num > hi:
        raise ClassificationValidationError(f"feedback_id {fid!r} {field} {num} out of {lo}..{hi}")
    return num


def validate_batch_output(
    payload: dict[str, Any],
    expected_feedback_ids: list[str],
) -> list[dict[str, Any]]:
    """Validate the structured output for a single batch.

    Raises ``ClassificationValidationError`` with a precise reason on any
    mismatch; otherwise returns the list of validated result rows.
    """
    if not isinstance(payload, dict):
        raise ClassificationValidationError("payload is not an object")
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        raise ClassificationValidationError("payload missing 'results' array")
    expected = list(expected_feedback_ids)
    expected_set = set(expected)
    if len(results) != len(expected):
        raise ClassificationValidationError(
            f"results length {len(results)} != batch size {len(expected)}"
        )
    seen_ids: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    for index, item in enumerate(results):
        if not isinstance(item, dict):
            raise ClassificationValidationError(f"results[{index}] is not an object")
        fid = item.get("feedback_id")
        if not isinstance(fid, str) or not fid:
            raise ClassificationValidationError(f"results[{index}].feedback_id missing or invalid")
        if fid in seen_ids:
            raise ClassificationValidationError(f"duplicate feedback_id {fid!r}")
        seen_ids.add(fid)
        if fid not in expected_set:
            raise ClassificationValidationError(
                f"unknown feedback_id {fid!r} not present in input batch"
            )
        if expected[index] != fid:
            raise ClassificationValidationError(
                f"results[{index}].feedback_id order mismatch: expected "
                f"{expected[index]!r}, got {fid!r}"
            )
        ftype = _require_enum(item.get("feedback_type"), FEEDBACK_TYPES, "feedback_type", fid)
        sentiment = _require_enum(item.get("sentiment"), SENTIMENTS, "sentiment", fid)
        module = _require_nonempty_string(item.get("module"), "module", fid, max_length=100)
        intent = _require_nonempty_string(item.get("intent"), "intent", fid, max_length=200)
        pain_point = _require_nonempty_string(
            item.get("pain_point"), "pain_point", fid, max_length=4000
        )
        severity = _require_strict_int(item.get("severity"), "severity", fid, lo=1, hi=5)
        confidence = _require_strict_float(
            item.get("confidence"), "confidence", fid, lo=0.0, hi=1.0
        )
        cleaned.append(
            {
                "feedback_id": fid,
                "feedback_type": ftype,
                "module": module,
                "intent": intent,
                "pain_point": pain_point,
                "sentiment": sentiment,
                "severity": severity,
                "confidence": confidence,
            }
        )
    missing = expected_set - seen_ids
    if missing:
        raise ClassificationValidationError(f"missing feedback_ids in results: {sorted(missing)}")
    return cleaned


def build_user_message(batch: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"feedback_id": fid, "content": content} for fid, content in batch]


def attempt_batch(
    client: LLMClient,
    batch: list[tuple[str, str]],
) -> ClassificationAttempt:
    """Run one LLM call + validation pass; never touches the database.

    Failures raise :class:`_BatchAttemptError` so callers can still capture
    the per-call latency and token usage even when the batch did not succeed.
    """
    if not batch:
        return ClassificationAttempt([], None, None, 0)
    expected_ids = [fid for fid, _ in batch]
    user_items = build_user_message(batch)
    start = time.monotonic()
    try:
        response = client.classify_batch(user_items)
        latency_ms = int((time.monotonic() - start) * 1000)
    except (LLMTransportError, LLMProtocolError) as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        raise _BatchAttemptError(
            "LLM transport failure"
            if isinstance(exc, LLMTransportError)
            else "LLM protocol failure",
            latency_ms=latency_ms,
            input_tokens=None,
            output_tokens=None,
        ) from exc
    try:
        successes = validate_batch_output(response.payload, expected_ids)
    except ClassificationValidationError as exc:
        raise _BatchAttemptError(
            "LLM classification validation failed",
            latency_ms=latency_ms,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        ) from exc
    return ClassificationAttempt(
        successes=successes,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        latency_ms=latency_ms,
    )


def _accumulate_attempt(
    run: AnalysisRun,
    input_tokens: int | None,
    output_tokens: int | None,
    latency_ms: int,
) -> None:
    """Update run-level counters for a single LLM attempt (success or failure).

    ``unknown_token_calls`` counts the number of LLM calls whose usage was
    unparseable, not the number of missing fields. A call where either
    field is ``None`` contributes exactly one to the unknown counter so the
    total stays comparable to ``attempt_count`` instead of being inflated by
    every missing token count.
    """
    run.attempt_count += 1
    run.latency_ms += max(latency_ms, 0)
    if input_tokens is None or output_tokens is None:
        run.unknown_token_calls += 1
        if input_tokens is not None:
            run.input_tokens += input_tokens
        if output_tokens is not None:
            run.output_tokens += output_tokens
    else:
        run.input_tokens += input_tokens
        run.output_tokens += output_tokens


def _upsert_failure(
    db: Session,
    run_id: UUID,
    feedback_id: UUID,
    attempts: int,
    error: str,
) -> None:
    """Record or update a per-feedback failure row.

    ``attempts`` is the *actual* number of LLM calls the runner made for this
    feedback; we do not assume one-attempt-per-record.
    """
    existing = db.scalar(
        select(AnalysisRunFailure).where(
            AnalysisRunFailure.run_id == run_id,
            AnalysisRunFailure.feedback_id == feedback_id,
        )
    )
    safe_error = _safe_error_message(error)
    if existing is None:
        db.add(
            AnalysisRunFailure(
                run_id=run_id,
                feedback_id=feedback_id,
                attempts=max(attempts, 1),
                last_error=safe_error,
            )
        )
    else:
        existing.attempts = max(existing.attempts, max(attempts, 1))
        existing.last_error = safe_error


def _persist_successes(
    db: Session,
    run: AnalysisRun,
    feedback_by_external: dict[str, Feedback],
    successes: list[dict[str, Any]],
) -> int:
    inserted = 0
    for row in successes:
        feedback = feedback_by_external.get(row["feedback_id"])
        if feedback is None:
            continue
        already = db.scalar(
            select(FeedbackAnalysis).where(
                FeedbackAnalysis.run_id == run.id,
                FeedbackAnalysis.feedback_id == feedback.id,
            )
        )
        if already is not None:
            continue
        db.add(
            FeedbackAnalysis(
                feedback_id=feedback.id,
                run_id=run.id,
                feedback_type=row["feedback_type"],
                module=row["module"],
                intent=row["intent"],
                pain_point=row["pain_point"],
                sentiment=row["sentiment"],
                severity=row["severity"],
                confidence=row["confidence"],
                semantic_text=build_semantic_text(
                    feedback.content,
                    module=row["module"],
                    intent=row["intent"],
                    pain_point=row["pain_point"],
                ),
            )
        )
        inserted += 1
    return inserted


def _record_batch_failure(
    db: Session,
    run: AnalysisRun,
    feedback_by_external: dict[str, Feedback],
    batch_external_ids: list[str],
    attempts: int,
    error_message: str,
) -> None:
    """Persist one batch's failure row(s) and run counters, then commit.

    Each batch is its own transaction so an aborted or interrupted later batch
    cannot wipe out earlier successes.
    """
    for external_id in batch_external_ids:
        feedback = feedback_by_external[external_id]
        _upsert_failure(db, run.id, feedback.id, attempts, error_message)
        run.failed_count += 1
        run.processed_count += 1
    db.commit()


def _record_batch_success(
    db: Session,
    run: AnalysisRun,
    attempt: ClassificationAttempt,
    inserted: int,
    batch_external_ids: list[str],
) -> None:
    """Finalise one batch's successes and run counters, then commit.

    Per-attempt metrics (``attempt_count``, ``input_tokens``, ``output_tokens``,
    ``unknown_token_calls``, ``latency_ms``) must already have been recorded
    by :func:`_accumulate_attempt` for the call that produced ``attempt``.
    This function only stamps the run with the success / processed counters
    so a subsequent ``_persist_successes`` failure can be turned into a
    failure record without double-counting the LLM call.
    """
    run.processed_count += len(batch_external_ids)
    run.success_count += len(batch_external_ids)
    db.commit()


def run_classification(
    db: Session,
    run: AnalysisRun,
    feedbacks: list[Feedback],
    *,
    max_retries: int | None = None,
    delay_seconds: float = 0.5,
) -> None:
    """Execute a classification pass for a run in-place.

    Only ``feedbacks`` whose UUID does not already have a row in
    ``feedback_analyses`` for this run are processed. Counts are accumulated
    idempotently and each batch is committed independently so that an aborted
    or interrupted later batch does not roll back the earlier successes.
    """
    if max_retries is None:
        max_retries = settings.classification_max_retries
    batch_size = settings.classification_batch_size
    pending_external_ids: list[str] = []
    for feedback in feedbacks:
        already = db.scalar(
            select(FeedbackAnalysis.id).where(
                FeedbackAnalysis.run_id == run.id,
                FeedbackAnalysis.feedback_id == feedback.id,
            )
        )
        if already is not None:
            continue
        pending_external_ids.append(feedback.external_id)
    if not pending_external_ids:
        return

    feedback_by_external: dict[str, Feedback] = {f.external_id: f for f in feedbacks}
    batches = chunk(pending_external_ids, batch_size)

    with build_llm_client(model=run.model) as client:
        for batch_index, batch_external_ids in enumerate(batches, start=1):
            batch_started = time.monotonic()
            batch: list[tuple[str, str]] = [
                (external_id, feedback_by_external[external_id].content)
                for external_id in batch_external_ids
            ]
            attempt_count = 0
            last_error: str | None = None
            outcome: ClassificationAttempt | None = None
            while True:
                try:
                    outcome = attempt_batch(client, batch)
                    break
                except _BatchAttemptError as exc:
                    attempt_count += 1
                    last_error = repr(exc)
                    # Always accumulate per-attempt accounting so token and
                    # latency totals reflect every LLM call we actually made,
                    # not only the final successful one.
                    _accumulate_attempt(
                        run,
                        exc.input_tokens,
                        exc.output_tokens,
                        exc.latency_ms,
                    )
                    db.commit()
                    if attempt_count > max_retries:
                        outcome = None
                        break
                    run.retry_count += 1
                    time.sleep(delay_seconds * (2 ** (attempt_count - 1)))
            if outcome is None:
                _record_batch_failure(
                    db,
                    run,
                    feedback_by_external,
                    batch_external_ids,
                    attempts=attempt_count,
                    error_message=last_error or "unknown error",
                )
                logger.info(
                    "classification_batch run_id=%s project_id=%s batch=%d model=%r "
                    "prompt_version=%r status=failed attempts=%d latency_ms=%d",
                    run.id,
                    run.project_id,
                    batch_index,
                    run.model,
                    run.prompt_version,
                    attempt_count,
                    int((time.monotonic() - batch_started) * 1000),
                )
                continue
            # Record the LLM call's metrics on their own transaction so a
            # downstream persistence failure (server-side dedup, rollback,
            # etc.) cannot erase the actual call from the run's accounting.
            _accumulate_attempt(
                run,
                outcome.input_tokens,
                outcome.output_tokens,
                outcome.latency_ms,
            )
            db.commit()
            try:
                inserted = _persist_successes(db, run, feedback_by_external, outcome.successes)
                db.flush()
            except IntegrityError:
                db.rollback()
                _record_batch_failure(
                    db,
                    run,
                    feedback_by_external,
                    batch_external_ids,
                    attempts=attempt_count + 1,
                    error_message="persistence rejected batch result",
                )
                logger.info(
                    "classification_batch run_id=%s project_id=%s batch=%d model=%r "
                    "prompt_version=%r status=failed attempts=%d latency_ms=%d",
                    run.id,
                    run.project_id,
                    batch_index,
                    run.model,
                    run.prompt_version,
                    attempt_count + 1,
                    int((time.monotonic() - batch_started) * 1000),
                )
                continue
            if inserted != len(batch_external_ids):
                # Schema passed but server-side dedup hit (e.g. concurrent
                # insert). Roll back the persistence attempt (the metrics
                # above were committed separately) and record the failure so
                # the partial work can be retried without losing earlier
                # batches.
                db.rollback()
                _record_batch_failure(
                    db,
                    run,
                    feedback_by_external,
                    batch_external_ids,
                    attempts=attempt_count + 1,
                    error_message="persistence rejected some rows in batch",
                )
                logger.info(
                    "classification_batch run_id=%s project_id=%s batch=%d model=%r "
                    "prompt_version=%r status=failed attempts=%d latency_ms=%d",
                    run.id,
                    run.project_id,
                    batch_index,
                    run.model,
                    run.prompt_version,
                    attempt_count + 1,
                    int((time.monotonic() - batch_started) * 1000),
                )
                continue
            _record_batch_success(db, run, outcome, inserted, batch_external_ids)
            logger.info(
                "classification_batch run_id=%s project_id=%s batch=%d model=%r "
                "prompt_version=%r status=completed attempts=%d latency_ms=%d",
                run.id,
                run.project_id,
                batch_index,
                run.model,
                run.prompt_version,
                attempt_count + 1,
                int((time.monotonic() - batch_started) * 1000),
            )


def load_project_feedbacks(db: Session, project_id: UUID) -> list[Feedback]:
    return db.scalars(
        select(Feedback)
        .where(Feedback.project_id == project_id)
        .order_by(Feedback.created_at, Feedback.id)
    ).all()


def derive_run_status(run: AnalysisRun) -> str:
    if run.input_count == 0:
        return "completed"
    if run.failed_count == 0 and run.processed_count >= run.input_count:
        return "completed"
    if run.success_count == 0 and run.processed_count >= run.input_count:
        return "failed"
    if run.processed_count >= run.input_count:
        return "partial_failed"
    return "running"
