"""Phase 6 — Insight evaluation runner.

This module is the bridge between Phase 3 (``InsightRun`` /
``Insight`` / ``InsightEvidence``) and the Phase 6 evaluation
tables. It is intentionally read-only against the source tables:

* No row in ``insight_runs`` / ``insights`` / ``insight_evidence`` /
  ``feedbacks`` is mutated.
* ``model`` / ``prompt_version`` / token / latency values are read
  from the source ``InsightRun`` snapshot — the runner does not
  call any LLM, does not derive new model names, and never asks the
  caller for them.

The evaluation is a **DB snapshot** evaluation (NOT a CSV
comparison). For every ``InsightEvidence`` row the runner persists
ONE :class:`EvaluationCase` with:

* ``case_id`` = ``InsightEvidence.id`` (the snapshot key).
* ``eval_source`` = ``db_record`` so a regression never confuses
  this row with a CSV-loaded case.
* ``passed`` = True iff the live ``Feedback`` row is in the same
  project AND its ``external_id`` matches the snapshot's
  ``feedback_external_id``. The FK may exist but the snapshot can
  still be wrong (cross-project, drift) — the runner refuses to
  assume consistency.
* Fingerprint hashes are SHA-256 of identifying tuples
  (``evidence_id``, ``feedback_id``, ``insight_id``); raw
  ``feedback.content`` is NEVER persisted.

The runner writes exactly these deterministic metrics:

* ``evidence_existence_rate`` — fraction of evidence rows whose
  ``feedback_id`` is a row in this project's ``feedbacks``.  When
  the source run has zero evidence the value is **NULL with
  ``sample_count = 0``** (NOT_MEASURED); the runner never fabricates
  a 100% rate.
* ``evidence_validity`` / ``groundedness`` / ``hallucination_rate``
  — three ``human_reviewed`` placeholder rows with ``value = NULL``,
  ``sample_count = 0`` and ``details`` explicitly stating that a
  human verdict is still required. The runner does NOT generate any
  semantic number for these.

The runner's traceability contract for the DB snapshot is:

* :attr:`EvaluationRun.dataset_provenance` defaults to
  ``"unverified"``. The runner does NOT certify whether the
  underlying DB snapshot is synthetic, anonymised, or production
  data; a separate provenance check must promote the value. The
  same disclaimer is also written into ``notes`` so an audit
  cannot mistake the row for a verified-provenance evaluation.
* :attr:`EvaluationRun.dataset_content_hash` is a SHA-256 over the
  canonical, sorted evidence payload — every evidence reference
  plus the SHA-256 of the referenced ``Feedback.content``. The raw
  feedback text NEVER enters the hash; only the content
  fingerprint does. Any change in an evidence reference, an
  identifier, or a referenced feedback content produces a
  different hash.
* :attr:`EvaluationRun.dataset_version` is a short prefix derived
  from ``dataset_content_hash``. The same snapshot yields the
  same version; any drift in evidence / feedback content produces
  a new version. The column is short (≤ 64 chars) so it fits the
  ``String(64)`` budget.

The runner reuses the Phase 1/2 runner's typed exceptions so
callers (``scripts/run_insight_evaluation.py``, supervisors,
regression code) only need to catch one set of error types across
all stages.

The entry point additionally calls ``db.expire_all()`` on entry
so the evidence / feedback reads always reflect the current DB
state, not the session's cached objects. This is essential when a
caller pre-loaded the source rows in the same session and then
applied raw ``Table.update()`` / ``Table.delete()`` patches
``expire_on_commit=False`` would otherwise mask.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evals.metrics import (
    PHASE6_CALCULATION_VERSION,
    MetricResult,
    compute_evidence_existence_rate,
)
from app.evals.runner import (
    EvaluationRunnerConfigError,
    EvaluationRunnerSourceMismatchError,
    EvaluationRunnerValidationError,
)
from app.models import Feedback, Insight, InsightEvidence, InsightRun
from app.models.evaluation import (
    EvaluationCase,
    EvaluationCaseErrorType,
    EvaluationCaseSource,
    EvaluationMetric,
    EvaluationRun,
    EvaluationRunStatus,
)

# ---- Public constants --------------------------------------------------------

EVALUATION_TYPE_INSIGHT: str = "insight_metrics"
"""The ``EvaluationRun.evaluation_type`` value this runner writes."""

_STAGE_INSIGHT: str = "insight"
"""The ``EvaluationRun.stage`` value the runner writes."""

# Five-state pipeline status shared with Phases 1/2. The Phase 3
# InsightRun uses the same vocabulary (pending / running / completed
# / partial_failed / failed), so the terminal set is identical to
# the Phase 1/2 runner. Re-declared here so the insight runner is
# self-contained even though the values are equal.
_TERMINAL_INSIGHT_SOURCE_STATUSES: frozenset[str] = frozenset(
    {"completed", "partial_failed", "failed"}
)

# Three human-reviewed insight metrics. The semantic numbers are
# intentionally NOT generated by the runner — a future sprint must
# land an inspector-verdict workflow before any of these rows can
# carry a value. The runner only persists skeleton rows that the
# dashboard / regression comparison can grep for.
HUMAN_REVIEWED_METRICS: tuple[str, ...] = (
    "evidence_validity",
    "groundedness",
    "hallucination_rate",
)

# Human-readable explanation embedded in the ``details`` of every
# human-reviewed placeholder row so an audit never mistakes the
# placeholder for a real measurement.
HUMAN_REVIEWED_NOTE: str = (
    "Awaiting human review: this insight-stage metric requires a "
    "human verdict per evidence row. The runner does NOT generate "
    "any semantic number; sample_count is 0 and value is NULL so "
    "the regression comparison cannot mistake this row for a real "
    "measurement."
)

# Stable dataset name for DB-snapshot insight evals. There is no
# CSV / dataset_provenance comparison; the name just records the
# contract in the row itself.
DATASET_NAME_INSIGHT_DB_SNAPSHOT: str = "phase6_insight_db_snapshot"


# ---- Per-evidence decision vocabulary ----------------------------------------

# These string tokens are written into ``EvaluationCase.notes`` so a
# downstream reader can see, at a glance, why a case did not pass.
# The values are part of the persisted contract — changing them is a
# schema migration for downstream tooling.
_DECISION_OK = "ok"
_DECISION_MISSING_FEEDBACK = "missing_feedback"
_DECISION_WRONG_PROJECT = "wrong_project"
_DECISION_EXTERNAL_ID_MISMATCH = "external_id_mismatch"


# ---- Internal helpers --------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_metric_for_db(metric: MetricResult) -> MetricResult:
    """Enforce the DB CHECK ``(sample_count > 0) OR (value IS NULL)``.

    Mirrors :func:`app.evals.runner._coerce_metric_for_db`. The
    pure metric package may return ``value=0.0`` with
    ``sample_count=0``; the ORM CHECK rejects that, so we coerce
    it to ``value=None`` with a flag in ``details``.
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


def _coerce_tokens_for_eval(
    source_run: InsightRun,
) -> tuple[int | None, int | None]:
    """Propagate input/output tokens with NOT_MEASURED semantics.

    Same rule as the Phase 1/2 runner: when the source flags
    unreliable calls via ``unknown_token_calls > 0`` the totals are
    not a real measurement, so the eval run records NULL on both
    sides and ``unknown_token_calls`` carries the signal.
    """
    if int(source_run.unknown_token_calls or 0) > 0:
        return None, None
    return _safe_int(source_run.input_tokens), _safe_int(source_run.output_tokens)


def _coerce_latency_for_eval(source_run: InsightRun) -> int | None:
    """Propagate latency with NOT_MEASURED semantics.

    Same rule as the Phase 1/2 runner: a real measurement requires
    ``attempt_count > 0`` AND a non-zero ``latency_ms``. The source
    schema records latency_ms with NOT NULL but does not encode
    "unknown" natively — the runner treats a zero
    ``attempt_count`` / ``latency_ms`` as the absence of a
    measurement and writes NULL.
    """
    attempt = int(source_run.attempt_count or 0)
    raw_latency = _safe_int(source_run.latency_ms)
    raw_latency_value = int(raw_latency or 0)
    if attempt == 0 or raw_latency_value == 0:
        return None
    return raw_latency_value


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


def _validate_insight_source_run(
    db: Session,
    *,
    project_id: UUID,
    source_run_id: UUID,
) -> InsightRun:
    """Load the source ``InsightRun`` and reject structural errors.

    Same defensive pattern as the Phase 1/2 runner:
    ``db.refresh(run)`` forces a fresh SELECT after the identity-map
    lookup so a raw ``Table.update()`` issued earlier (which bypasses
    the ORM) is still visible to the structural validations.

    The runner refuses to evaluate a source run that is:

    * missing (no row matches ``source_run_id``),
    * cross-project (``InsightRun.project_id`` ≠ ``project_id``),
    * non-terminal (status not in
      :data:`_TERMINAL_INSIGHT_SOURCE_STATUSES`).
    """
    run = db.get(InsightRun, source_run_id)
    if run is None:
        raise EvaluationRunnerSourceMismatchError(f"source insight run {source_run_id} not found")
    db.refresh(run)
    errors: list[str] = []
    if run.project_id != project_id:
        errors.append(
            f"source insight run {source_run_id} belongs to project "
            f"{run.project_id}, expected {project_id}; cross-project rejected"
        )
    if run.status not in _TERMINAL_INSIGHT_SOURCE_STATUSES:
        errors.append(
            f"source insight run {source_run_id} status {run.status!r} "
            f"is not terminal; terminal statuses="
            f"{sorted(_TERMINAL_INSIGHT_SOURCE_STATUSES)}"
        )
    if errors:
        raise EvaluationRunnerSourceMismatchError(
            f"source insight run {source_run_id} is not eligible for evaluation",
            errors=errors,
        )
    return run


def _load_evidence_for_run(
    db: Session,
    *,
    source_run_id: UUID,
) -> list[InsightEvidence]:
    """Load every ``InsightEvidence`` row attached to the source run.

    The FK on ``Insight.run_id`` is ``ON DELETE CASCADE``, so a
    missing run (caught earlier) trivially means zero rows. The
    runner does NOT synthesise evidence rows; an empty result is a
    legitimate state and produces NOT_MEASURED downstream.
    """
    insight_ids = list(db.scalars(select(Insight.id).where(Insight.run_id == source_run_id)).all())
    if not insight_ids:
        return []
    return list(
        db.scalars(select(InsightEvidence).where(InsightEvidence.insight_id.in_(insight_ids))).all()
    )


def _load_feedbacks_by_id(
    db: Session,
    *,
    feedback_ids: Sequence[UUID],
) -> dict[UUID, Feedback]:
    """Look up every referenced ``Feedback`` row by id.

    Intentionally NOT filtered by ``project_id`` so the runner can
    detect cross-project references (a feedback row exists but in a
    different project). The project consistency check happens later
    in :func:`_check_feedback_references`; the metric function
    :func:`compute_evidence_existence_rate` receives the subset of
    feedback ids that live in the eval project.
    """
    if not feedback_ids:
        return {}
    return {
        f.id: f for f in db.scalars(select(Feedback).where(Feedback.id.in_(feedback_ids))).all()
    }


def _classify_evidence(
    evidence: InsightEvidence,
    feedback: Feedback | None,
    *,
    project_id: UUID,
) -> tuple[str, str | None]:
    """Classify one evidence row against the live ``Feedback`` snapshot.

    Returns ``(decision, error_type)`` where ``decision`` is one of
    the four documented tokens (ok / missing_feedback /
    wrong_project / external_id_mismatch) and ``error_type`` is the
    matching :class:`EvaluationCaseErrorType` string or ``None`` for
    the OK case.

    The FK being satisfied is NOT sufficient — the runner
    independently verifies project + external_id so a stale
    snapshot or a cross-project leak is recorded as a failed case.
    """
    if feedback is None:
        return _DECISION_MISSING_FEEDBACK, EvaluationCaseErrorType.NOT_MEASURED.value
    if feedback.project_id != project_id:
        return _DECISION_WRONG_PROJECT, EvaluationCaseErrorType.VALIDATION_ERROR.value
    if feedback.external_id != evidence.feedback_external_id:
        return (
            _DECISION_EXTERNAL_ID_MISMATCH,
            EvaluationCaseErrorType.VALIDATION_ERROR.value,
        )
    return _DECISION_OK, None


def _fingerprint_payload(evidence: InsightEvidence) -> str:
    """Canonical JSON payload hashed into ``EvaluationCase.input_hash``.

    Raw feedback text is excluded on purpose — the runner
    fingerprints the identifying tuple only.
    """

    payload = {
        "evidence_id": str(evidence.id),
        "insight_id": str(evidence.insight_id),
        "feedback_id": str(evidence.feedback_id),
        "feedback_external_id": str(evidence.feedback_external_id),
        "feedback_project_id": str(evidence.feedback_project_id),
        "feedback_classification_run_id": str(evidence.feedback_classification_run_id),
        "evidence_type": str(evidence.evidence_type),
        "relevance_score": float(evidence.relevance_score),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _expected_payload(evidence: InsightEvidence) -> str:
    """Canonical JSON payload hashed into ``EvaluationCase.expected_hash``."""
    payload = {
        "feedback_exists_in_project": True,
        "feedback_external_id": str(evidence.feedback_external_id),
        "feedback_project_id": str(evidence.feedback_project_id),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _actual_payload(decision: str, feedback: Feedback | None) -> str:
    """Canonical JSON payload hashed into ``EvaluationCase.actual_hash``."""
    if decision == _DECISION_OK:
        return "feedback_matches_snapshot"
    if decision == _DECISION_MISSING_FEEDBACK:
        return "feedback_missing"
    if decision == _DECISION_WRONG_PROJECT:
        assert feedback is not None  # noqa: S101 - guarded by `_classify_evidence`
        return f"feedback_wrong_project: actual_project_id={feedback.project_id}"
    if decision == _DECISION_EXTERNAL_ID_MISMATCH:
        assert feedback is not None  # noqa: S101 - guarded by `_classify_evidence`
        return f"feedback_external_id_mismatch: actual_external_id={feedback.external_id}"
    return "other"


def _build_dataset_payload(
    *,
    source_run: InsightRun,
    evidence_rows: Sequence[InsightEvidence],
    feedback_by_id: dict[UUID, Feedback],
) -> dict:
    """Canonical dict whose SHA-256 is ``dataset_content_hash``.

    The payload is sorted by ``(insight_id, evidence_id)`` so the
    hash is reproducible regardless of the SQL fetch order. Each
    evidence row references the SHA-256 of the live
    ``Feedback.content``; raw feedback text NEVER enters the hash
    (it would also defeat the per-case raw-text default of NULL).
    A missing feedback (dangling FK, see the ``missing_feedback``
    contract) is recorded as ``feedback_content_sha256=None`` so
    the hash still reflects the live state of the source.
    """
    evidence_payload: list[dict] = []
    for ev in sorted(evidence_rows, key=lambda e: (str(e.insight_id), str(e.id))):
        fb = feedback_by_id.get(ev.feedback_id)
        content_sha: str | None = (
            hashlib.sha256(fb.content.encode("utf-8")).hexdigest() if fb is not None else None
        )
        evidence_payload.append(
            {
                "evidence_id": str(ev.id),
                "insight_id": str(ev.insight_id),
                "feedback_id": str(ev.feedback_id),
                "feedback_external_id": str(ev.feedback_external_id),
                "feedback_project_id": str(ev.feedback_project_id),
                "feedback_classification_run_id": str(ev.feedback_classification_run_id),
                "evidence_type": str(ev.evidence_type),
                "relevance_score": float(ev.relevance_score),
                "feedback_content_sha256": content_sha,
            }
        )
    return {
        "source_run_id": str(source_run.id),
        "evaluation_type": EVALUATION_TYPE_INSIGHT,
        "evidence": evidence_payload,
        "evidence_count": len(evidence_rows),
    }


def _build_human_reviewed_metric(
    *,
    metric_name: str,
    source_run: InsightRun,
) -> MetricResult:
    """Build one human-reviewed placeholder metric.

    The persisted row carries ``value=None`` / ``sample_count=0`` /
    ``kind="human_reviewed"`` so the ORM CHECK
    ``(sample_count > 0) OR (value IS NULL)`` is satisfied and so a
    downstream regression cannot mistake the placeholder for a real
    measurement.
    """
    return MetricResult(
        name=metric_name,
        kind="human_reviewed",
        stage=_STAGE_INSIGHT,
        sample_count=0,
        value=None,
        details={
            "source_run_id": str(source_run.id),
            "source_run_status": str(source_run.status),
            "requires_human_review": True,
            "no_semantic_value_generated": True,
            "reason": HUMAN_REVIEWED_NOTE,
        },
    )


# ---- Entry point -------------------------------------------------------------


def evaluate_insight_run(
    db: Session,
    *,
    project_id: UUID,
    insight_run_id: UUID,
    started_at: datetime | None = None,
) -> EvaluationRun:
    """Score an existing Phase 3 ``InsightRun`` as a DB-snapshot evaluation.

    The runner is read-only against Phase 1/2/3 source rows. It
    writes ONE :class:`EvaluationRun` + N
    :class:`EvaluationMetric` + N :class:`EvaluationCase` rows.

    Parameters
    ----------
    db:
        Open SQLAlchemy session. The function commits on success.
    project_id:
        Project the eval belongs to. The source ``InsightRun``'s
        ``project_id`` MUST match — cross-project refs are rejected
        BEFORE any DB write.
    insight_run_id:
        Phase 3 ``InsightRun`` UUID. Cross-project or non-terminal
        runs are rejected with
        :class:`EvaluationRunnerSourceMismatchError`.
    started_at:
        Override for the run's ``started_at`` (testing hook).
        Defaults to ``now()``.

    Returns
    -------
    EvaluationRun
        The persisted row. ``status`` is ``completed`` (every
        evidence passed) or ``partial_failed`` (at least one
        evidence failed). ``calculation_version`` is pinned to
        :data:`app.evals.metrics.PHASE6_CALCULATION_VERSION`.

    Raises
    ------
    EvaluationRunnerSourceMismatchError
        Cross-project / missing / non-terminal source run. Raised
        BEFORE any DB write.
    EvaluationRunnerValidationError
        Internal invariant violation (count / status
        reconciliation).
    EvaluationRunnerConfigError
        Misuse of the entry point (e.g. missing required kwargs).
    """
    if not isinstance(project_id, UUID) or not isinstance(insight_run_id, UUID):
        raise EvaluationRunnerConfigError(
            "evaluate_insight_run requires UUID project_id and insight_run_id"
        )

    # Defensive: clear the session's identity map so the reads below
    # always see the current DB state. A caller that pre-loaded the
    # ``InsightRun`` / ``Insight`` / ``InsightEvidence`` / ``Feedback``
    # rows in the same session — possibly with
    # ``expire_on_commit=False`` and a raw ``Table.update()`` /
    # ``Table.delete()`` issued after the load — would otherwise see
    # the cached attribute values. The brief: "保留缺失/错项目失败case
    # （不要因 FK 存在便假定一致）". Forcing the reads to come from the
    # live DB is the only way the per-evidence verdicts can trust
    # the source-of-truth state. This is a no-op for callers that
    # have not pre-loaded anything.
    db.expire_all()

    source_run = _validate_insight_source_run(
        db,
        project_id=project_id,
        source_run_id=insight_run_id,
    )

    effective_started_at = started_at or _now()

    try:
        evidence_rows = _load_evidence_for_run(db, source_run_id=insight_run_id)
        feedback_by_id = _load_feedbacks_by_id(
            db,
            feedback_ids=[ev.feedback_id for ev in evidence_rows],
        )

        case_records: list[EvaluationCase] = []
        success_cases = 0
        failed_cases = 0
        evidence_ids_for_metric: list[str] = []
        for ev in evidence_rows:
            feedback = feedback_by_id.get(ev.feedback_id)
            decision, error_type = _classify_evidence(ev, feedback, project_id=project_id)
            evidence_ids_for_metric.append(str(ev.feedback_id))

            passed = decision == _DECISION_OK
            if passed:
                success_cases += 1
            else:
                failed_cases += 1

            case_records.append(
                EvaluationCase(
                    run_id=None,
                    project_id=project_id,
                    case_id=str(ev.id),
                    stage=_STAGE_INSIGHT,
                    input_hash=_hash_text(_fingerprint_payload(ev)),
                    expected_hash=_hash_text(_expected_payload(ev)),
                    actual_hash=_hash_text(_actual_payload(decision, feedback)),
                    passed=passed,
                    error_type=error_type,
                    eval_source=EvaluationCaseSource.DB_RECORD.value,
                    notes=decision,
                )
            )

        # ``evidence_existence_rate`` is the deterministic metric the
        # pure-function package already implements. When the source
        # run has zero evidence the function returns
        # ``sample_count=0, value=None`` — NOT_MEASURED — which is
        # exactly what the brief mandates (no fake 100%).
        known_feedback_ids = sorted(
            str(fid) for fid, fb in feedback_by_id.items() if fb.project_id == project_id
        )
        existence_rate_metric = compute_evidence_existence_rate(
            evidence_ids_for_metric, known_feedback_ids
        )
        existence_rate_metric = MetricResult(
            name=existence_rate_metric.name,
            kind=existence_rate_metric.kind,
            stage=_STAGE_INSIGHT,
            sample_count=existence_rate_metric.sample_count,
            value=existence_rate_metric.value,
            details={
                "source_run_id": str(source_run.id),
                "source_run_status": str(source_run.status),
                "total_evidence_rows": len(evidence_rows),
                **dict(existence_rate_metric.details),
            },
        )

        human_reviewed_metrics: list[MetricResult] = [
            _build_human_reviewed_metric(metric_name=name, source_run=source_run)
            for name in HUMAN_REVIEWED_METRICS
        ]

        metric_results: list[MetricResult] = [existence_rate_metric, *human_reviewed_metrics]

        notes_text = _join_notes(
            f"evaluation_type={EVALUATION_TYPE_INSIGHT}",
            f"source_run_id={source_run.id}",
            "source_run_type=insight",
            f"source_run_status={source_run.status}",
            f"source_run_model={source_run.model}",
            f"source_run_prompt_version={source_run.prompt_version}",
            "dataset_provenance=unverified",
            "dataset_split=full",
            "dataset_label_status=db_snapshot",
            "dataset_source=db_snapshot",
            "db_source_unverified=True; the snapshot may contain real "
            "user feedback and is not certified as synthetic or "
            "anonymised; provenance must be promoted by a separate "
            "check",
            "store_raw=False; EvaluationCase.input stays NULL",
        )

        dataset_payload = _build_dataset_payload(
            source_run=source_run,
            evidence_rows=evidence_rows,
            feedback_by_id=feedback_by_id,
        )
        dataset_content_hash = _hash_text(
            json.dumps(
                dataset_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        # Short version derived from the content hash. 16 hex chars =
        # 64 bits of entropy — enough to differentiate snapshots
        # within one project's evaluation history while staying well
        # under the 64-char ``String(64)`` column budget. The full
        # 64-char SHA-256 stays in ``dataset_content_hash`` for
        # forensic use.
        dataset_version = f"unverified-{dataset_content_hash[:16]}"

        input_tokens_value, output_tokens_value = _coerce_tokens_for_eval(source_run)

        input_count = len(evidence_rows)
        processed_count = len(evidence_rows)
        final_status = (
            EvaluationRunStatus.COMPLETED.value
            if failed_cases == 0
            else EvaluationRunStatus.PARTIAL_FAILED.value
        )

        run = EvaluationRun(
            project_id=project_id,
            evaluation_type=EVALUATION_TYPE_INSIGHT,
            dataset_name=DATASET_NAME_INSIGHT_DB_SNAPSHOT,
            dataset_version=dataset_version,
            dataset_content_hash=dataset_content_hash,
            dataset_provenance="unverified",
            dataset_split="full",
            dataset_label_status="db_snapshot",
            stage=_STAGE_INSIGHT,
            model=source_run.model,
            prompt_version=source_run.prompt_version,
            pipeline_version=None,
            calculation_version=PHASE6_CALCULATION_VERSION,
            status=EvaluationRunStatus.RUNNING.value,
            input_count=input_count,
            processed_count=processed_count,
            success_count=success_cases,
            failed_count=failed_cases,
            input_tokens=input_tokens_value,
            output_tokens=output_tokens_value,
            unknown_token_calls=int(source_run.unknown_token_calls or 0),
            latency_ms=_coerce_latency_for_eval(source_run),
            total_cost_usd=None,
            attempt_count=int(source_run.attempt_count or 0),
            retry_count=int(source_run.retry_count or 0),
            started_at=effective_started_at,
            completed_at=None,
            notes=notes_text,
        )

        db.add(run)
        db.flush()
        run_id = run.id

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
        db.flush()

        for case in case_records:
            case.run_id = run_id
            db.add(case)
        db.flush()

        if run.processed_count != run.success_count + run.failed_count:
            raise EvaluationRunnerValidationError(
                f"processed_count ({run.processed_count}) != "
                f"success_count + failed_count "
                f"({run.success_count + run.failed_count})"
            )
        if run.success_count + run.failed_count > run.input_count:
            raise EvaluationRunnerValidationError(
                "success_count + failed_count exceeds input_count"
            )

        run.status = final_status
        run.completed_at = _now()
        db.commit()
        db.refresh(run)
        return run

    except (EvaluationRunnerSourceMismatchError, EvaluationRunnerValidationError):
        db.rollback()
        raise
    except EvaluationRunnerConfigError:
        db.rollback()
        raise


__all__ = [
    "DATASET_NAME_INSIGHT_DB_SNAPSHOT",
    "EVALUATION_TYPE_INSIGHT",
    "HUMAN_REVIEWED_METRICS",
    "HUMAN_REVIEWED_NOTE",
    "evaluate_insight_run",
]
