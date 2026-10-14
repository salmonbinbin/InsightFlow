"""Phase 6 — reusable evaluation runner service.

The runner is the bridge between Phase 1—5 source artefacts and the
Phase 6 evaluation tables. It ONLY reads from Phase 1—5 (it never
mutates ``AnalysisRun`` / ``FeedbackAnalysis`` / ``Cluster`` /
``ClusterMember`` / ``Feedback`` rows) and writes a fresh snapshot
into ``evaluation_runs`` / ``evaluation_metrics`` / ``evaluation_cases``.

The runner is intentionally NOT an LLM judge: there is no "is the
summary accurate?" subjective metric, no LLM-as-judge call, no
hallucination metric. Everything is reproducible from the deterministic
metric package in :mod:`app.evals.metrics` plus the read-only
Phase 1—2 rows. The brief: "不要实现 LLM Judge 或主观 groundedness 数字".

Public surface:

* :class:`EvaluationRunnerConfigError`
* :class:`EvaluationRunnerValidationError`
* :class:`EvaluationRunnerSourceMismatchError`
* :func:`evaluate_existing_run` — the one entry point.

What the runner guarantees:

* Source validation happens BEFORE any DB write. Cross-project calls,
  wrong ``run_type``, non-terminal runs, dataset cases with no matching
  ``Feedback`` row, or ``feedback.content`` whose hash disagrees with
  the dataset's ``feedback_text_hash`` are all rejected with a typed
  exception (no half-written evaluation row left behind).
* The dataset ``case_id`` is mapped onto ``Feedback.external_id``.
  Both directions — case-dataset present → feedback missing AND
  feedback present → case-dataset missing — are reported through the
  validation error list so the supervisor can see every mismatch in
  one pass.
* Per-case ``EvaluationCase`` rows store SHA-256 of input/expected/
  actual by default. The full plaintext fields (``input`` /
  ``expected`` / ``actual``) stay NULL unless the caller explicitly
  passes ``store_raw=True``. The opt-in is recorded in the
  ``EvaluationRun.notes`` so an audit can read it back.
* Token / latency / cost columns are sourced from the source
  ``AnalysisRun``. ``input_tokens`` / ``output_tokens`` / ``latency_ms``
  stay NULL when the source reported them as unknown.
  ``unknown_token_calls`` is propagated as-is so a downstream regression
  can detect the NOT_MEASURED signal.
* The ``local-hash-v1`` embedding model is NOT a real semantic
  embedding; the runner records that fact in the run's ``notes`` so
  a regression can never confuse it with a remote embedding.
* Transaction model: a short write transaction at the end. On any
  internal exception the runner rolls back, then commits ONE
  ``EvaluationRun`` row with ``status='failed'`` carrying the error
  message — a partial / fake-``completed`` row is never left behind.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evals.dataset import DatasetProvenance
from app.evals.metrics import (
    PHASE6_CALCULATION_VERSION,
    FeedbackTypePrediction,
    MetricResult,
    compute_cluster_metrics,
    compute_feedback_type_metrics,
    compute_intent_exact_match,
    compute_severity_metrics,
)
from app.evals.metrics import (
    ClusterPrediction as ClusterPredictionValue,
)
from app.models import (
    AnalysisRun,
    Cluster,
    ClusterMember,
    FeedbackAnalysis,
)
from app.models.evaluation import (
    EvaluationCase,
    EvaluationCaseErrorType,
    EvaluationCaseSource,
    EvaluationMetric,
    EvaluationRun,
    EvaluationRunStatus,
)
from app.models.feedback import Feedback

# ---- Public constants --------------------------------------------------------

# Canonical mapping between evaluation_type and the AnalysisRun.run_type the
# runner is allowed to consume. Keep these in one place so a new evaluation
# type cannot accidentally read from a wrong source run.
_EVAL_TYPE_TO_SOURCE_RUN_TYPE: Mapping[str, str] = {
    "classification_metrics": "classification",
    "cluster_metrics": "clustering",
}

# The Phase 6 runner only implements two evaluation_type values today.
SUPPORTED_EVALUATION_TYPES: tuple[str, ...] = tuple(_EVAL_TYPE_TO_SOURCE_RUN_TYPE)

# Source-run status values that count as "terminal" for the runner. A
# classification run in ``running`` / ``pending`` is never a stable source
# for evaluation — its outputs can still change.
_TERMINAL_SOURCE_STATUSES: frozenset[str] = frozenset({"completed", "partial_failed", "failed"})

# Controlled vocabulary for classification ``feedback_type``. Mirrors the
# classifier's whitelist so a regression never sees a phantom label.
_CLASSIFICATION_FEEDBACK_TYPES: tuple[str, ...] = (
    "bug",
    "feature_request",
    "ux_issue",
    "performance_issue",
    "pricing_issue",
    "content_issue",
    "positive_feedback",
    "other",
)
_CLASSIFICATION_FEEDBACK_TYPES_SET: frozenset[str] = frozenset(_CLASSIFICATION_FEEDBACK_TYPES)

# Severity range mirrors the FeedbackAnalysis CHECK constraint and the
# metric package default (1..5).
_CLASSIFICATION_SEVERITY_RANGE: tuple[int, int] = (1, 5)

# Embedding model that the runner explicitly flags as non-semantic. Any
# source cluster run with this embedding_model gets a note in the
# EvaluationRun.
LOCAL_HASH_EMBEDDING_MODEL: str = "local-hash-v1"

# Stage strings used on EvaluationRun.stage / EvaluationMetric.stage /
# EvaluationCase.stage. Kept as plain strings so the runner does not
# import the ORM enum just to compare equality.
_STAGE_CLASSIFICATION = "classification"
_STAGE_CLUSTERING = "clustering"
# Pipeline-level metrics (success / failure counts sourced from the
# source AnalysisRun) live under a dedicated ``pipeline`` stage so the
# dashboard does not confuse them with per-label accuracy rows whose
# stage matches the originating analysis (``classification`` /
# ``clustering``).
_STAGE_PIPELINE = "pipeline"


# ---- Errors ------------------------------------------------------------------


class EvaluationRunnerConfigError(ValueError):
    """Bad configuration / unsupported evaluation type. Caller must fix."""


class EvaluationRunnerValidationError(ValueError):
    """The input bundle (source run + dataset) is malformed.

    ``errors`` carries the full list of mismatches so the supervisor can
    read every one of them in one pass instead of fixing them
    one-at-a-time. Always raised BEFORE any DB write.
    """

    def __init__(self, message: str, *, errors: Sequence[str] | None = None) -> None:
        super().__init__(message)
        self.errors: list[str] = list(errors or [])


class EvaluationRunnerSourceMismatchError(EvaluationRunnerValidationError):
    """Cross-project / wrong-run-type / non-terminal source run.

    Subclass of :class:`EvaluationRunnerValidationError` so callers
    that catch all runner-side input errors get this one too.
    """


# ---- Internal helpers ---------------------------------------------------------


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_optional(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return _hash_text(value)
    return _hash_text(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _check_supported_evaluation_type(evaluation_type: str) -> str:
    if evaluation_type not in _EVAL_TYPE_TO_SOURCE_RUN_TYPE:
        raise EvaluationRunnerConfigError(
            f"unsupported evaluation_type {evaluation_type!r}; "
            f"allowed={sorted(_EVAL_TYPE_TO_SOURCE_RUN_TYPE)}"
        )
    return _EVAL_TYPE_TO_SOURCE_RUN_TYPE[evaluation_type]


def _validate_source_run(
    db: Session,
    *,
    project_id: UUID,
    source_run_id: UUID,
    expected_run_type: str,
) -> AnalysisRun:
    """Load the source run and reject the obvious structural errors.

    ``db.refresh(run)`` forces a fresh SELECT after the identity-map
    lookup so a raw ``Table.update()`` issued earlier (which bypasses
    the ORM and leaves the in-session object stale) is still visible to
    the structural validations below. Without the refresh, a wrong
    ``run_type`` / non-terminal ``status`` set via ``db.execute(...)``
    would pass validation because the cached object still carries the
    original values.
    """
    run = db.get(AnalysisRun, source_run_id)
    if run is None:
        raise EvaluationRunnerSourceMismatchError(f"source run {source_run_id} not found")
    db.refresh(run)
    errors: list[str] = []
    if run.project_id != project_id:
        errors.append(
            f"source run {source_run_id} belongs to project "
            f"{run.project_id}, expected {project_id}; cross-project rejected"
        )
    if run.run_type != expected_run_type:
        errors.append(
            f"source run {source_run_id} has run_type {run.run_type!r}; "
            f"expected {expected_run_type!r} (evaluation_type requires this)"
        )
    if run.status not in _TERMINAL_SOURCE_STATUSES:
        errors.append(
            f"source run {source_run_id} status {run.status!r} is not terminal; "
            f"terminal statuses={sorted(_TERMINAL_SOURCE_STATUSES)}"
        )
    if errors:
        raise EvaluationRunnerSourceMismatchError(
            f"source run {source_run_id} is not eligible for evaluation",
            errors=errors,
        )
    return run


def _validate_dataset_against_feedbacks(
    *,
    dataset: DatasetProvenance,
    by_external_id: Mapping[str, Feedback],
) -> None:
    """Verify every dataset case's text matches Feedback.content (by SHA-256).

    Also flags duplicate ``case_id`` rows inside the dataset. The
    dataset's :func:`app.evals.dataset.load_demo_dataset` already
    rejects duplicates at load time, but the runner must defend against
    callers that hand-build a :class:`DatasetProvenance` and skip
    validation.
    """
    errors: list[str] = []
    seen_case_ids: set[str] = set()
    for case in dataset.cases:
        if case.case_id in seen_case_ids:
            errors.append(f"dataset has duplicate case_id {case.case_id!r}")
            continue
        seen_case_ids.add(case.case_id)
        feedback = by_external_id.get(case.case_id)
        if feedback is None:
            errors.append(
                f"dataset case {case.case_id!r}: no Feedback row with "
                f"external_id={case.case_id!r} in this project"
            )
            continue
        expected_hash = _hash_text(case.feedback_text)
        actual_hash = _hash_text(feedback.content)
        if expected_hash != actual_hash:
            errors.append(
                f"dataset case {case.case_id!r}: content hash mismatch; "
                f"dataset.feedback_text hash={expected_hash[:16]}..., "
                f"feedback.content hash={actual_hash[:16]}...; "
                f"refusing to score against drifted content"
            )
    if errors:
        raise EvaluationRunnerValidationError(
            f"dataset content does not match this project's feedbacks ({len(errors)} mismatch(es))",
            errors=errors,
        )


def _load_feedbacks_for_dataset(
    db: Session,
    *,
    project_id: UUID,
    dataset: DatasetProvenance,
) -> dict[str, Feedback]:
    """Return ``{external_id: Feedback}`` for every dataset case.

    Independent of the source run: a feedback row is fetched whenever
    the project's ``Feedback.external_id`` matches a dataset
    ``case_id``. This lets the validator detect content drift even for
    feedbacks the source run did not process.
    """
    if not dataset.cases:
        return {}
    case_ids = sorted(case.case_id for case in dataset.cases)
    rows = db.scalars(
        select(Feedback).where(
            Feedback.project_id == project_id,
            Feedback.external_id.in_(case_ids),
        )
    ).all()
    return {f.external_id: f for f in rows}


def _coerce_metric_for_db(metric: MetricResult) -> MetricResult:
    """Enforce the DB CHECK: ``(sample_count > 0) OR (value IS NULL)``.

    The pure metric package can legitimately return ``value=0.0`` with
    ``sample_count=0`` (e.g. ``cluster_count`` on an empty prediction
    set). The ORM CHECK rejects that combination, so we coerce it to
    ``value=None`` with a flag in ``details`` so the persisted row
    still reflects "we measured zero clusters".
    """
    if metric.sample_count == 0 and metric.value is not None:
        details = dict(metric.details)
        details["zero_with_no_sample"] = True
        return MetricResult(
            name=metric.name,
            kind=metric.kind,
            stage=metric.stage,
            sample_count=0,
            value=None,
            details=details,
        )
    return metric


def _safe_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_tokens_for_eval(
    source_run: AnalysisRun,
) -> tuple[int | None, int | None]:
    """Propagate input/output token counts onto the eval run with NULL semantics.

    The Phase 1/2 source flags ``unknown_token_calls`` whenever an LLM
    call returned unparseable / missing usage. The brief: "input/output
    tokens 如 unknown_token_calls>0 也应 NULL" — when ANY call is
    unreliable the totals are not a real measurement, so the eval run
    must record NULL and let the per-call counter carry the signal.
    When ``unknown_token_calls == 0`` we trust the source totals.
    """
    if int(source_run.unknown_token_calls or 0) > 0:
        return None, None
    return _safe_int(source_run.input_tokens), _safe_int(source_run.output_tokens)


def _coerce_latency_for_eval(source_run: AnalysisRun) -> int | None:
    """Propagate latency onto the eval run with NULL semantics.

    The source's latency is a real measurement only when an LLM call
    actually happened (``attempt_count > 0``) AND a non-zero duration
    was captured. The brief: "source run attempt_count=0/latency_ms=0
    时确为未测，不可当真实0；记录 NULL". A zero here is the absence of
    a measurement, not an observed zero. Persisting NULL on the eval
    row lets the dashboard distinguish "we did not measure" from "we
    measured zero".
    """
    attempt = int(source_run.attempt_count or 0)
    raw_latency = _safe_int(source_run.latency_ms)
    raw_latency_value = int(raw_latency or 0)
    if attempt == 0 or raw_latency_value == 0:
        return None
    return raw_latency_value


def _build_pipeline_metrics(
    *,
    source_run: AnalysisRun,
    stage: str,
) -> list[MetricResult]:
    """Build deterministic pipeline-level metrics from the source run counters.

    Two metrics are returned in a fixed order:

    * ``pipeline_success_rate`` — ``source_run.success_count /
      source_run.input_count``. When the source had zero inputs the
      metric reports NOT_MEASURED (``value=None``, ``sample_count=0``)
      so the row never claims a meaningless zero. The ``details``
      payload carries an explicit ``definition`` string
      ``source_pipeline_execution_not_label_accuracy`` so a downstream
      regression test / dashboard panel can confirm the metric is
      decoupled from per-label accuracy and from the eval run's
      per-case pass/fail outcome.
    * ``pipeline_failure_stage_count`` — ``source_run.failed_count``
      with ``sample_count = source_run.input_count`` so the metric
      stays attached to the same denominator. When the source had
      zero inputs the value is also NULL to satisfy the ORM CHECK
      ``(sample_count > 0) OR (value IS NULL)``.

    Both metrics are intentionally NOT derived from the eval run's
    per-case pass/fail outcome. The whole point of these rows is to
    surface source-pipeline execution health so a regression there
    is not masked by a strong label-accuracy number (and vice-versa).
    ``stage`` on the persisted row is always ``pipeline``; the
    originating ``classification`` / ``clustering`` label is recorded
    in ``details["stage"]`` so an audit can trace the row back to
    the analysis stage that produced the source counters.
    """
    input_count = int(source_run.input_count or 0)
    processed_count = int(source_run.processed_count or 0)
    success_count = int(source_run.success_count or 0)
    failed_count = int(source_run.failed_count or 0)
    source_run_id = str(source_run.id)
    source_status = str(source_run.status)

    pipeline_success: MetricResult
    if input_count > 0:
        pipeline_success = MetricResult(
            name="pipeline_success_rate",
            kind="deterministic",
            stage=_STAGE_PIPELINE,
            sample_count=input_count,
            value=float(success_count) / float(input_count),
            details={
                "source_run_id": source_run_id,
                "source_status": source_status,
                "success_count": success_count,
                "failed_count": failed_count,
                "processed_count": processed_count,
                "input_count": input_count,
                "stage": stage,
                "definition": "source_pipeline_execution_not_label_accuracy",
            },
        )
    else:
        pipeline_success = MetricResult(
            name="pipeline_success_rate",
            kind="deterministic",
            stage=_STAGE_PIPELINE,
            sample_count=0,
            value=None,
            details={
                "source_run_id": source_run_id,
                "source_status": source_status,
                "success_count": success_count,
                "failed_count": failed_count,
                "processed_count": processed_count,
                "input_count": input_count,
                "stage": stage,
                "definition": "source_pipeline_execution_not_label_accuracy",
                "not_measured_reason": "source_run_input_count_zero",
            },
        )

    pipeline_failure: MetricResult
    if input_count > 0:
        pipeline_failure = MetricResult(
            name="pipeline_failure_stage_count",
            kind="deterministic",
            stage=_STAGE_PIPELINE,
            sample_count=input_count,
            value=float(failed_count),
            details={
                "source_run_id": source_run_id,
                "source_status": source_status,
                "input_count": input_count,
                "processed_count": processed_count,
                "success_count": success_count,
                "failed_count": failed_count,
                "stage": stage,
                "failure_stage_label": (
                    "classification" if stage == _STAGE_CLASSIFICATION else "clustering"
                ),
            },
        )
    else:
        pipeline_failure = MetricResult(
            name="pipeline_failure_stage_count",
            kind="deterministic",
            stage=_STAGE_PIPELINE,
            sample_count=0,
            value=None,
            details={
                "source_run_id": source_run_id,
                "source_status": source_status,
                "input_count": input_count,
                "processed_count": processed_count,
                "success_count": success_count,
                "failed_count": failed_count,
                "stage": stage,
                "failure_stage_label": (
                    "classification" if stage == _STAGE_CLASSIFICATION else "clustering"
                ),
                "not_measured_reason": "source_run_input_count_zero",
            },
        )

    return [pipeline_success, pipeline_failure]


def _join_notes(*parts: object) -> str | None:
    cleaned: list[str] = []
    for part in parts:
        if part is None:
            continue
        text = str(part).strip()
        if text:
            cleaned.append(text)
    if not cleaned:
        return None
    return "\n".join(cleaned)


def _derive_final_status(*, failure_count: int, processed_count: int) -> str:
    """Map per-case outcomes to a final ``EvaluationRunStatus`` value.

    * No failures → ``completed``.
    * Some failures → ``partial_failed``.

    The runner already decided whether to mark the run ``failed``
    (catastrophic) BEFORE this is called; the entry point that routes
    exceptions to a ``failed`` row is :func:`evaluate_existing_run`.
    """
    if failure_count == 0:
        return EvaluationRunStatus.COMPLETED.value
    return EvaluationRunStatus.PARTIAL_FAILED.value


# ---- Exception sanitisation -------------------------------------------------


# Credential-like patterns: an audit log MUST never echo raw API keys,
# bearer tokens, password=… payloads, etc. Mirrors the redactor used by
# the Phase 1 classifier.
_SENSITIVE_TOKEN_RE = re.compile(
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

# SQL / parameters markers. The brief: "异常 error_message 当前包含
# IntegrityError SQL/parameters 原文，可能泄露用户输入；只存安全摘要，不含
# SQL、参数、密钥". A single match anywhere in the candidate string is
# enough to force the runner to drop the reason and only keep the
# exception type name.
_SQL_OR_PARAMS_MARKERS = re.compile(
    r"\[SQL:|\[parameters?[^\]]*\]|"
    r"\bSELECT\b|\bINSERT\s+INTO\b|\bUPDATE\s+\w+\b|\bDELETE\s+FROM\b|"
    r"\bFROM\s+\w+\b|\bWHERE\s+|\bJOIN\s+|\bVALUES\s*\(|"
    r"PRAGMA|BEGIN\s+TRANSACTION|COMMIT|ROLLBACK|"
    r"sqlite|postgresql|psycopg",
    re.IGNORECASE,
)

_SAFE_ERROR_MAX_LEN = 4000
_SAFE_ERROR_REASON_MAX_LEN = 200


def _safe_runner_error(exc: BaseException) -> str:
    """Return a safe, short summary of an exception for audit storage.

    Always returns the exception class name (safe). Optionally appends a
    sanitised single-line reason. The reason is dropped entirely when
    the exception text contains SQL / parameter markers (the brief
    forbids echoing those). Credential-like tokens are redacted before
    the reason is persisted.
    """
    name = type(exc).__name__
    raw = ""
    if exc.args:
        first = exc.args[0]
        if isinstance(first, str):
            raw = first
    if not raw:
        return name
    if _SQL_OR_PARAMS_MARKERS.search(raw):
        # SQL / parameters detected — keep only the type name. Do not
        # attempt partial sanitisation: a partial scrub of a SQL
        # fragment still leaks structure and column names.
        return name
    sanitized = _SENSITIVE_TOKEN_RE.sub(" [REDACTED] ", raw)
    sanitized = sanitized.replace("\x00", " ")
    sanitized = " ".join(sanitized.split())
    if len(sanitized) > _SAFE_ERROR_REASON_MAX_LEN:
        sanitized = sanitized[:_SAFE_ERROR_REASON_MAX_LEN]
    return f"{name}: {sanitized}"


def _truncate_safe(message: str | None) -> str | None:
    if message is None:
        return None
    text = " ".join(str(message).split())
    if len(text) > _SAFE_ERROR_MAX_LEN:
        text = text[:_SAFE_ERROR_MAX_LEN]
    return text


# ---- Classification runner --------------------------------------------------


@dataclass(frozen=True)
class _ClassificationBuild:
    predictions: Mapping[str, FeedbackTypePrediction]
    by_external_id: Mapping[str, Feedback]
    ran_set_external_ids: tuple[str, ...]


def _build_classification_predictions(
    db: Session,
    *,
    project_id: UUID,
    source_run: AnalysisRun,
) -> _ClassificationBuild:
    """Read the source run's predictions, mapped to ``Feedback.external_id``."""
    analyses = db.scalars(
        select(FeedbackAnalysis).where(FeedbackAnalysis.run_id == source_run.id)
    ).all()
    feedback_ids = {a.feedback_id for a in analyses}
    by_id: dict[UUID, Feedback] = {}
    if feedback_ids:
        rows = db.scalars(
            select(Feedback).where(
                Feedback.project_id == project_id,
                Feedback.id.in_(feedback_ids),
            )
        ).all()
        by_id = {f.id: f for f in rows}

    predictions: dict[str, FeedbackTypePrediction] = {}
    by_external_id: dict[str, Feedback] = {}
    for analysis in analyses:
        feedback = by_id.get(analysis.feedback_id)
        if feedback is None:
            # Project mismatch / dangling FK → skip. The runner refuses
            # to invent a prediction for an unreachable feedback row.
            continue
        case_id = feedback.external_id
        severity = _safe_int(analysis.severity)
        if severity is not None and not (
            _CLASSIFICATION_SEVERITY_RANGE[0] <= severity <= _CLASSIFICATION_SEVERITY_RANGE[1]
        ):
            severity = None
        ftype = str(analysis.feedback_type)
        if ftype not in _CLASSIFICATION_FEEDBACK_TYPES_SET:
            continue
        intent_value = analysis.intent if isinstance(analysis.intent, str) else None
        predictions[case_id] = FeedbackTypePrediction(
            case_id=case_id,
            feedback_type=ftype,
            severity=severity,
            intent=intent_value,
        )
        by_external_id[case_id] = feedback

    referenced_external_ids = sorted(by_external_id)
    return _ClassificationBuild(
        predictions=predictions,
        by_external_id=by_external_id,
        ran_set_external_ids=tuple(referenced_external_ids),
    )


def _evaluate_classification(
    db: Session,
    *,
    project_id: UUID,
    source_run: AnalysisRun,
    dataset: DatasetProvenance,
    intent_vocabulary: Sequence[str],
    started_at: datetime,
) -> tuple[EvaluationRun, list[MetricResult], list[EvaluationCase]]:
    """Run classification metrics + per-case rows from source run + dataset."""
    build = _build_classification_predictions(db, project_id=project_id, source_run=source_run)

    # Validate the dataset against ALL feedbacks whose external_id is in
    # the dataset, independent of the source run. This catches content
    # drift even for feedbacks the source run did not process.
    all_feedbacks = _load_feedbacks_for_dataset(db, project_id=project_id, dataset=dataset)
    _validate_dataset_against_feedbacks(dataset=dataset, by_external_id=all_feedbacks)

    golden_types: dict[str, str] = {}
    golden_severities: dict[str, int] = {}
    golden_intents: dict[str, str] = {}
    for case in dataset.cases:
        if case.feedback_type_label is not None:
            golden_types[case.case_id] = case.feedback_type_label
        if case.severity_label is not None:
            golden_severities[case.case_id] = case.severity_label
        if case.intent_label is not None:
            golden_intents[case.case_id] = case.intent_label

    metrics_results: list[MetricResult] = []
    if golden_types:
        acc, macro_f1 = compute_feedback_type_metrics(
            build.predictions,
            golden_types,
            allowed_labels=_CLASSIFICATION_FEEDBACK_TYPES,
        )
        metrics_results.extend([acc, macro_f1])
    if golden_severities:
        exact, mae = compute_severity_metrics(
            build.predictions,
            golden_severities,
            severity_range=_CLASSIFICATION_SEVERITY_RANGE,
        )
        metrics_results.extend([exact, mae])
    if golden_intents:
        # Intent is open-ended free text by default; the brief:
        # "Intent 因自由文本而 NOT MEASURED". The metric function already
        # returns NOT_MEASURED when the vocabulary is empty or when
        # observed intents fall outside it. Callers may opt into a
        # controlled vocabulary; otherwise the metric row documents
        # the reason explicitly.
        intent_metric = compute_intent_exact_match(
            build.predictions,
            golden_intents,
            controlled_vocabulary=intent_vocabulary,
        )
        metrics_results.append(intent_metric)

    # Pipeline-level metrics — sourced from the source AnalysisRun
    # counters, NOT from per-case pass/fail. Decoupling the two reading
    # channels avoids confusion between "the LLM answered 12/12 but
    # labelled them wrong" and "the LLM dropped 4/12 of the requests";
    # both signals matter and must surface independently.
    metrics_results.extend(
        _build_pipeline_metrics(source_run=source_run, stage=_STAGE_CLASSIFICATION)
    )

    case_records: list[EvaluationCase] = []
    success_cases = 0
    failed_cases = 0
    missing_pred_count = 0
    for case in dataset.cases:
        pred = build.predictions.get(case.case_id)
        if pred is None:
            missing_pred_count += 1
            failed_cases += 1
            case_records.append(
                EvaluationCase(
                    run_id=None,
                    project_id=project_id,
                    case_id=case.case_id,
                    stage=_STAGE_CLASSIFICATION,
                    input_hash=_hash_text(case.feedback_text),
                    expected_hash=_hash_optional(case.feedback_type_label),
                    actual_hash=None,
                    passed=False,
                    error_type=EvaluationCaseErrorType.NO_PREDICTION.value,
                    eval_source=EvaluationCaseSource.CSV_LOAD.value,
                    notes="no FeedbackAnalysis row for this case_id in the source run",
                )
            )
            continue
        passed = True
        expected_parts: list[str] = []
        actual_parts: list[str] = []
        if case.feedback_type_label is not None:
            expected_parts.append(f"feedback_type={case.feedback_type_label}")
            actual_parts.append(f"feedback_type={pred.feedback_type}")
            if pred.feedback_type != case.feedback_type_label:
                passed = False
        if case.severity_label is not None:
            expected_parts.append(f"severity={case.severity_label}")
            actual_parts.append(
                f"severity={pred.severity}" if pred.severity is not None else "severity=<missing>"
            )
            if pred.severity != case.severity_label:
                passed = False
        error_type = None if passed else EvaluationCaseErrorType.OTHER.value
        if passed:
            success_cases += 1
        else:
            failed_cases += 1
        case_records.append(
            EvaluationCase(
                run_id=None,
                project_id=project_id,
                case_id=case.case_id,
                stage=_STAGE_CLASSIFICATION,
                input_hash=_hash_text(case.feedback_text),
                expected_hash=_hash_text(",".join(expected_parts) or "<none>"),
                actual_hash=_hash_text(",".join(actual_parts) or "<none>"),
                passed=passed,
                error_type=error_type,
                eval_source=EvaluationCaseSource.CSV_LOAD.value,
            )
        )

    notes = _join_notes(
        "evaluation_type=classification_metrics",
        f"source_run_id={source_run.id}",
        f"source_run_type={source_run.run_type}",
        f"source_run_status={source_run.status}",
        f"source_run_model={source_run.model}",
        f"source_run_prompt_version={source_run.prompt_version}",
        f"missing_prediction_dataset_cases={missing_pred_count}",
    )

    input_tokens_value, output_tokens_value = _coerce_tokens_for_eval(source_run)
    run = EvaluationRun(
        project_id=project_id,
        evaluation_type="classification_metrics",
        dataset_name=dataset.name,
        dataset_version=dataset.version,
        dataset_content_hash=dataset.content_hash,
        dataset_provenance=dataset.provenance,
        dataset_split=dataset.split,
        dataset_label_status=dataset.label_status,
        stage=_STAGE_CLASSIFICATION,
        model=source_run.model,
        prompt_version=source_run.prompt_version,
        pipeline_version=None,
        calculation_version=PHASE6_CALCULATION_VERSION,
        status=EvaluationRunStatus.RUNNING.value,
        input_count=len(dataset.cases),
        processed_count=len(dataset.cases),
        success_count=success_cases,
        failed_count=failed_cases,
        input_tokens=input_tokens_value,
        output_tokens=output_tokens_value,
        unknown_token_calls=int(source_run.unknown_token_calls or 0),
        latency_ms=_coerce_latency_for_eval(source_run),
        total_cost_usd=None,
        attempt_count=int(source_run.attempt_count or 0),
        retry_count=int(source_run.retry_count or 0),
        started_at=started_at,
        completed_at=None,
        notes=notes,
    )
    return run, metrics_results, case_records


# ---- Cluster runner -------------------------------------------------------


@dataclass(frozen=True)
class _ClusterBuild:
    predictions: tuple[ClusterPredictionValue, ...]
    by_external_id: Mapping[str, Feedback]
    duplicate_external_ids: tuple[str, ...]
    embedding_model: str | None


def _build_cluster_predictions(
    db: Session,
    *,
    project_id: UUID,
    source_run: AnalysisRun,
) -> _ClusterBuild:
    """Read the source cluster run's topology, mapped to ``Feedback.external_id``."""
    clusters = db.scalars(select(Cluster).where(Cluster.run_id == source_run.id)).all()
    if not clusters:
        return _ClusterBuild(
            predictions=(),
            by_external_id={},
            duplicate_external_ids=(),
            embedding_model=None,
        )
    cluster_ids = [c.id for c in clusters]
    members = db.scalars(
        select(ClusterMember).where(ClusterMember.cluster_id.in_(cluster_ids))
    ).all()
    analysis_ids = {m.feedback_analysis_id for m in members}
    analyses = db.scalars(
        select(FeedbackAnalysis).where(FeedbackAnalysis.id.in_(analysis_ids))
    ).all()
    feedback_ids = {a.feedback_id for a in analyses}
    fb_rows = db.scalars(
        select(Feedback).where(
            Feedback.project_id == project_id,
            Feedback.id.in_(feedback_ids),
        )
    ).all()
    analysis_by_id: dict[UUID, FeedbackAnalysis] = {a.id: a for a in analyses}
    feedback_by_id: dict[UUID, Feedback] = {f.id: f for f in fb_rows}

    external_to_clusters: dict[str, set[str]] = {}
    cluster_to_members: dict[str, list[str]] = {}
    for member in members:
        analysis = analysis_by_id.get(member.feedback_analysis_id)
        if analysis is None:
            continue
        feedback = feedback_by_id.get(analysis.feedback_id)
        if feedback is None:
            continue
        eid = feedback.external_id
        cluster_to_members.setdefault(str(member.cluster_id), []).append(eid)
        external_to_clusters.setdefault(eid, set()).add(str(member.cluster_id))

    duplicates = sorted(
        eid for eid, cluster_ids in external_to_clusters.items() if len(cluster_ids) > 1
    )

    predictions: list[ClusterPredictionValue] = []
    for cluster in clusters:
        members_list = cluster_to_members.get(str(cluster.id), [])
        seen: set[str] = set()
        unique_members: list[str] = []
        for m in members_list:
            if m in seen:
                continue
            seen.add(m)
            unique_members.append(m)
        predictions.append(
            ClusterPredictionValue(
                cluster_id=str(cluster.id),
                members=tuple(unique_members),
            )
        )

    by_external_id: dict[str, Feedback] = {f.external_id: f for f in fb_rows}
    embedding_model = clusters[0].embedding_model if clusters else None
    return _ClusterBuild(
        predictions=tuple(predictions),
        by_external_id=by_external_id,
        duplicate_external_ids=tuple(duplicates),
        embedding_model=embedding_model,
    )


def _evaluate_cluster(
    db: Session,
    *,
    project_id: UUID,
    source_run: AnalysisRun,
    dataset: DatasetProvenance,
    started_at: datetime,
) -> tuple[EvaluationRun, list[MetricResult], list[EvaluationCase]]:
    """Run cluster metrics + per-case rows from source run + dataset."""
    build = _build_cluster_predictions(db, project_id=project_id, source_run=source_run)

    all_feedbacks = _load_feedbacks_for_dataset(db, project_id=project_id, dataset=dataset)
    _validate_dataset_against_feedbacks(dataset=dataset, by_external_id=all_feedbacks)

    golden_clusters: dict[str, str] = {
        case.case_id: case.cluster_label for case in dataset.cases if case.cluster_label is not None
    }
    if not golden_clusters:
        raise EvaluationRunnerValidationError(
            "dataset has no cluster_label rows; cluster_metrics requires "
            "gt_cluster labels on the dataset"
        )

    metrics_results: list[MetricResult] = list(
        compute_cluster_metrics(build.predictions, golden_clusters)
    )

    # Pipeline-level metrics mirror the classification path:
    # ``pipeline_success_rate`` reflects the source cluster run's
    # success_count / input_count and ``pipeline_failure_stage_count``
    # exposes the absolute failure count so a regression that drops
    # cluster members is surfaced next to purity / coverage scores.
    metrics_results.extend(_build_pipeline_metrics(source_run=source_run, stage=_STAGE_CLUSTERING))

    case_records: list[EvaluationCase] = []
    success_cases = 0
    failed_cases = 0
    predicted_lookup: dict[str, tuple[str, ...]] = {}
    for pred in build.predictions:
        for member in pred.members:
            predicted_lookup.setdefault(member, []).append(pred.cluster_id)
    duplicate_set = set(build.duplicate_external_ids)

    for case in dataset.cases:
        gt_label = case.cluster_label
        if gt_label is None:
            continue
        predicted = tuple(predicted_lookup.get(case.case_id, ()))
        is_duplicate = case.case_id in duplicate_set
        notes_parts: list[str] = []
        if not predicted:
            failed_cases += 1
            passed = False
            error_type = EvaluationCaseErrorType.NO_PREDICTION.value
        else:
            passed = len(predicted) == 1 and not is_duplicate
            if passed:
                success_cases += 1
                error_type = None
            else:
                failed_cases += 1
                error_type = EvaluationCaseErrorType.OTHER.value
            if is_duplicate:
                notes_parts.append(
                    f"external_id appears in multiple clusters: {','.join(predicted)}"
                )
        expected = f"gt_cluster={gt_label}"
        actual = "predicted_cluster=" + ",".join(predicted) if predicted else None
        case_records.append(
            EvaluationCase(
                run_id=None,
                project_id=project_id,
                case_id=case.case_id,
                stage=_STAGE_CLUSTERING,
                input_hash=_hash_text(case.feedback_text),
                expected_hash=_hash_text(expected),
                actual_hash=_hash_text(actual or "<none>"),
                passed=passed,
                error_type=error_type,
                eval_source=EvaluationCaseSource.CSV_LOAD.value,
                notes=", ".join(notes_parts) or None,
            )
        )

    cluster_config_snapshot = dict(source_run.cluster_config or {})
    local_hash_warning = (
        "embedding_model=local-hash-v1 is NOT a real semantic embedding; "
        "the cluster topology is deterministic and reproducible but does "
        "not reflect remote embedding similarity."
        if build.embedding_model == LOCAL_HASH_EMBEDDING_MODEL
        else None
    )

    notes = _join_notes(
        "evaluation_type=cluster_metrics",
        f"source_run_id={source_run.id}",
        f"source_run_type={source_run.run_type}",
        f"source_run_status={source_run.status}",
        f"source_run_model={source_run.model}",
        f"source_run_prompt_version={source_run.prompt_version}",
        f"source_embedding_model={build.embedding_model}",
        f"source_cluster_config={json.dumps(cluster_config_snapshot, ensure_ascii=False, sort_keys=True, default=str)}",
        f"duplicate_external_ids={','.join(d for d in build.duplicate_external_ids) or None}",
        local_hash_warning,
    )

    input_tokens_value, output_tokens_value = _coerce_tokens_for_eval(source_run)
    run = EvaluationRun(
        project_id=project_id,
        evaluation_type="cluster_metrics",
        dataset_name=dataset.name,
        dataset_version=dataset.version,
        dataset_content_hash=dataset.content_hash,
        dataset_provenance=dataset.provenance,
        dataset_split=dataset.split,
        dataset_label_status=dataset.label_status,
        stage=_STAGE_CLUSTERING,
        model=build.embedding_model,
        prompt_version=source_run.prompt_version,
        pipeline_version=source_run.model,
        calculation_version=PHASE6_CALCULATION_VERSION,
        status=EvaluationRunStatus.RUNNING.value,
        input_count=len(dataset.cases),
        processed_count=sum(1 for c in case_records),
        success_count=success_cases,
        failed_count=failed_cases,
        input_tokens=input_tokens_value,
        output_tokens=output_tokens_value,
        unknown_token_calls=int(source_run.unknown_token_calls or 0),
        latency_ms=_coerce_latency_for_eval(source_run),
        total_cost_usd=None,
        attempt_count=int(source_run.attempt_count or 0),
        retry_count=int(source_run.retry_count or 0),
        started_at=started_at,
        completed_at=None,
        notes=notes,
    )
    return run, metrics_results, case_records


# ---- Entry point -------------------------------------------------------------


def evaluate_existing_run(
    db: Session,
    project_id: UUID,
    source_run_id: UUID,
    evaluation_type: str,
    dataset: DatasetProvenance,
    *,
    store_raw: bool = False,
    intent_vocabulary: Sequence[str] = (),
    started_at: datetime | None = None,
) -> EvaluationRun:
    """Score an existing Phase 1/2 run against a dataset snapshot.

    The runner is READ-ONLY against Phase 1/2 tables. It writes ONE
    :class:`EvaluationRun` row plus N :class:`EvaluationMetric` rows
    and N :class:`EvaluationCase` rows.

    The commit happens once, at the very end, inside a try / except
    so an internal exception rolls the transaction back and the
    function commits a single ``failed`` row so the audit trail never
    contains a fake ``completed`` snapshot.

    Parameters
    ----------
    db:
        Open SQLAlchemy session. The function commits on success and on
        the ``failed`` recovery path.
    project_id:
        Project the evaluation belongs to. The source run MUST match.
    source_run_id:
        The Phase 1 ``classification`` or Phase 2 ``clustering`` run
        whose predictions will be scored.
    evaluation_type:
        ``classification_metrics`` or ``cluster_metrics``. Other values
        raise :class:`EvaluationRunnerConfigError`.
    dataset:
        Loaded :class:`DatasetProvenance` snapshot. Its ``case_id``
        values are mapped to ``Feedback.external_id`` rows in the
        project and its ``feedback_text`` SHA-256 must match the
        stored ``Feedback.content`` SHA-256 (drift is rejected).
    store_raw:
        Opt-in to persisting the raw feedback text into the
        ``EvaluationCase.input`` column. Default ``False``: the runner
        persists SHA-256 only so sensitive raw content never lands in
        the eval table. The choice is recorded in ``EvaluationRun.notes``.
    intent_vocabulary:
        Controlled vocabulary for the intent exact-match metric. With
        the default (no value), intent reports NOT_MEASURED per the
        brief ("Intent 因自由文本而 NOT MEASURED").
    started_at:
        Override for the run's ``started_at`` (testing hook). Defaults
        to ``now()``.

    Returns
    -------
    EvaluationRun
        The persisted row. ``status`` is ``completed`` /
        ``partial_failed`` / ``failed``. Counts (``input_count``,
        ``processed_count``, ``success_count``, ``failed_count``) are
        mutually consistent. ``calculation_version`` is pinned to
        :data:`PHASE6_CALCULATION_VERSION`.
    """
    expected_run_type = _check_supported_evaluation_type(evaluation_type)
    source_run = _validate_source_run(
        db,
        project_id=project_id,
        source_run_id=source_run_id,
        expected_run_type=expected_run_type,
    )
    if not isinstance(dataset, DatasetProvenance):
        raise EvaluationRunnerConfigError("dataset must be a DatasetProvenance instance")
    if dataset.case_count() == 0:
        raise EvaluationRunnerValidationError("dataset has zero cases")

    effective_started_at = started_at or _now()

    try:
        if expected_run_type == "classification":
            run, metric_results, case_records = _evaluate_classification(
                db,
                project_id=project_id,
                source_run=source_run,
                dataset=dataset,
                intent_vocabulary=intent_vocabulary,
                started_at=effective_started_at,
            )
        elif expected_run_type == "clustering":
            run, metric_results, case_records = _evaluate_cluster(
                db,
                project_id=project_id,
                source_run=source_run,
                dataset=dataset,
                started_at=effective_started_at,
            )
        else:  # pragma: no cover - guarded by _check_supported_evaluation_type
            raise EvaluationRunnerConfigError(
                f"evaluation_type {evaluation_type!r} has no evaluator wired up"
            )

        run.status = _derive_final_status(
            failure_count=run.failed_count, processed_count=run.processed_count
        )
        run.completed_at = _now()

        store_raw_note = (
            "store_raw=True; raw text persisted into EvaluationCase.input"
            if store_raw
            else "store_raw=False; EvaluationCase.input stays NULL"
        )
        run.notes = _join_notes(run.notes, store_raw_note)

        db.add(run)
        db.flush()
        run_id = run.id

        persisted_metrics: list[MetricResult] = []
        for metric in metric_results:
            coerced = _coerce_metric_for_db(metric)
            db.add(
                EvaluationMetric(
                    run_id=run_id,
                    project_id=project_id,
                    stage=coerced.stage,
                    metric_name=coerced.name,
                    metric_kind=coerced.kind,
                    sample_count=coerced.sample_count,
                    value=coerced.value,
                    details=dict(coerced.details),
                )
            )
            persisted_metrics.append(coerced)
        db.flush()

        for case in case_records:
            case.run_id = run_id
            case.stage = run.stage
            if store_raw:
                raw_text = dataset.cases_by_id.get(case.case_id)
                case.input = raw_text.feedback_text if raw_text else None
            db.add(case)
        db.flush()

        if run.processed_count != run.success_count + run.failed_count:
            raise EvaluationRunnerValidationError(
                f"processed_count ({run.processed_count}) != "
                f"success_count + failed_count ({run.success_count + run.failed_count})"
            )
        if run.success_count + run.failed_count > run.input_count:
            raise EvaluationRunnerValidationError(
                "success_count + failed_count exceeds input_count"
            )
        db.commit()
        db.refresh(run)
        return run

    except EvaluationRunnerValidationError:
        db.rollback()
        raise
    except BaseException as exc:
        db.rollback()
        stage_str = (
            _STAGE_CLASSIFICATION if expected_run_type == "classification" else _STAGE_CLUSTERING
        )
        # A crashed run never reached the per-case loop, so
        # ``processed_count`` stays 0. The CHECK constraint
        # ``success_count + failed_count <= processed_count`` therefore
        # requires ``success_count = failed_count = 0``. The plan size
        # is preserved in ``input_count`` so a downstream reader can
        # still tell "we knew about N cases when we crashed".
        failed_run = EvaluationRun(
            project_id=project_id,
            evaluation_type=evaluation_type,
            dataset_name=dataset.name,
            dataset_version=dataset.version,
            dataset_content_hash=dataset.content_hash,
            dataset_provenance=dataset.provenance,
            dataset_split=dataset.split,
            dataset_label_status=dataset.label_status,
            stage=stage_str,
            model=getattr(source_run, "model", None),
            prompt_version=getattr(source_run, "prompt_version", None),
            pipeline_version=None,
            calculation_version=PHASE6_CALCULATION_VERSION,
            status=EvaluationRunStatus.FAILED.value,
            input_count=dataset.case_count(),
            processed_count=0,
            success_count=0,
            failed_count=0,
            unknown_token_calls=0,
            attempt_count=0,
            retry_count=0,
            started_at=effective_started_at,
            completed_at=_now(),
            error_message=_truncate_safe(f"runner crashed: {_safe_runner_error(exc)}"),
            notes=_join_notes(
                f"evaluation_type={evaluation_type}",
                f"source_run_id={source_run_id}",
                "runner_crash=True",
            ),
        )
        try:
            db.add(failed_run)
            db.commit()
            db.refresh(failed_run)
        except BaseException:
            db.rollback()
            raise
        return failed_run


__all__ = [
    "EvaluationRunnerConfigError",
    "EvaluationRunnerSourceMismatchError",
    "EvaluationRunnerValidationError",
    "LOCAL_HASH_EMBEDDING_MODEL",
    "SUPPORTED_EVALUATION_TYPES",
    "evaluate_existing_run",
]
