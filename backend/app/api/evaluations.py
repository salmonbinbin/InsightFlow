"""Phase 6 — Evaluation endpoints.

Four HTTP routes:

* ``POST /evaluations`` — synchronously score an existing Phase 1
  ``classification`` or Phase 2 ``clustering`` run against the demo
  dataset snapshot. The dataset is loaded server-side via
  :func:`app.evals.dataset.load_demo_dataset`; the body MUST carry
  ``project_id``, ``source_run_id`` and ``evaluation_type``.
* ``GET /evaluations/{id}`` — return the run row. ``project_id`` is
  an optional query parameter — when present the endpoint enforces
  cross-project isolation (mismatch → 404). When absent the
  endpoint returns the row without isolation (single-user mode).
* ``GET /evaluations/{id}/metrics`` — paginated metric rows.
  ``value_display`` is the stable, frontend-friendly rendering of
  the numeric ``value`` (``"NOT MEASURED"`` for ``None``).
* ``GET /evaluations/{id}/cases`` — paginated case rows. Raw
  feedback text is deliberately NOT returned; only SHA-256
  fingerprints. ``error_type`` describes how each case failed.

Error mapping:

* ``404`` — project, source run, or evaluation run does not exist.
  Cross-project access also returns ``404`` so a malicious caller
  cannot probe other projects' run ids.
* ``409`` — source-run side validation fails: cross-project,
  wrong ``run_type``, non-terminal ``status``, dataset content
  drift, dataset has no eligible cluster labels, etc. These are
  configuration / state problems the caller must fix before
  retrying.
* ``422`` — request body validation (Pydantic), extra fields, or
  illegal evaluation type.
* ``500`` — any unanticipated exception (transport error, DB
  failure, …) collapses to a generic 500 with NO echo of the
  original text. SQL fragments, parameter tuples, and
  credential-like tokens MUST NOT escape through the API surface.
* ``503`` — server-side configuration problem: demo CSV missing,
  unsupported ``evaluation_type`` (defensive — Pydantic already
  filters most of these), or any other unexpected runner crash.
  No raw SQL / parameter text / credential text is ever echoed in
  the response — the underlying runner already scrubs the audit
  trail; the API layer just keeps status codes stable.
"""

from __future__ import annotations

import re
from typing import Iterable
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.evals.dataset import DatasetValidationError, load_demo_dataset
from app.evals.runner import (
    EvaluationRunnerConfigError,
    EvaluationRunnerSourceMismatchError,
    EvaluationRunnerValidationError,
    evaluate_existing_run,
)
from app.models import Project
from app.models.evaluation import (
    EvaluationCase,
    EvaluationMetric,
    EvaluationRun,
)
from app.schemas.evaluation import (
    NOT_MEASURED_DISPLAY,
    EvaluationCasePage,
    EvaluationCaseRead,
    EvaluationCreateRequest,
    EvaluationMetricPage,
    EvaluationMetricRead,
    EvaluationRunCreateRead,
    EvaluationRunRead,
)

router = APIRouter(tags=["evaluations"])


# ---- Safe error sanitisation (defence in depth on top of the runner) ----------


_SQL_OR_PARAMS_MARKERS = re.compile(
    r"\[SQL:|\[parameters?[^\]]*\]|"
    r"\bSELECT\b|\bINSERT\s+INTO\b|\bUPDATE\s+\w+\b|\bDELETE\s+FROM\b|"
    r"\bFROM\s+\w+\b|\bWHERE\s+|\bJOIN\s+|\bVALUES\s*\(|"
    r"PRAGMA|BEGIN\s+TRANSACTION|COMMIT|ROLLBACK|"
    r"sqlite|postgresql|psycopg",
    re.IGNORECASE,
)
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


def _safe_error_message(message: str | None) -> str | None:
    """Strip SQL / parameter / credential substrings from a user-facing string.

    Defence in depth: the runner already scrubs
    ``EvaluationRun.error_message``; the API layer scrubs anything
    raised by the demo-data loader or any future upstream
    integration that might leak raw text into a 422 / 503 response.
    """
    if message is None:
        return None
    text = str(message)
    if _SQL_OR_PARAMS_MARKERS.search(text):
        return "数据源返回了不可安全回显的错误摘要"
    text = _SENSITIVE_TOKEN_RE.sub(" [REDACTED] ", text)
    text = text.replace("\x00", " ")
    text = " ".join(text.split())
    if len(text) > 4000:
        text = text[:4000]
    return text


# ---- Helpers ----------------------------------------------------------------


def _require_project(db: Session, project_id: UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


def _require_run(db: Session, run_id: UUID) -> EvaluationRun:
    run = db.get(EvaluationRun, run_id)
    if run is None:
        raise HTTPException(404, "评估运行不存在")
    return run


def _enforce_project_match(run: EvaluationRun, project_id: UUID | None) -> None:
    """Reject cross-project lookups with ``404`` (not ``403``).

    The brief: "GET 需项目内隔离：可用 project_id query 参数确认归属
    或依赖现有简化单用户机制；至少防跨项目恶意 source_run_id".
    Returning ``404`` rather than ``403`` is intentional — it matches
    the rest of the API surface (clusters, insights) and denies a
    malicious caller the ability to probe whether the id exists in
    another project.
    """
    if project_id is not None and run.project_id != project_id:
        raise HTTPException(404, "评估运行不属于该项目")


def _format_metric_value(value: float | None) -> str:
    if value is None:
        return NOT_MEASURED_DISPLAY
    return f"{value:.6f}"


def _metric_to_read(row: EvaluationMetric) -> EvaluationMetricRead:
    return EvaluationMetricRead(
        id=row.id,
        run_id=row.run_id,
        project_id=row.project_id,
        stage=row.stage,
        metric_name=row.metric_name,
        metric_kind=row.metric_kind,
        sample_count=row.sample_count,
        value=row.value,
        value_display=_format_metric_value(row.value),
        details=dict(row.details or {}),
        created_at=row.created_at,
    )


def _case_to_read(row: EvaluationCase) -> EvaluationCaseRead:
    failed_stage: str | None = None
    if row.passed is False:
        failed_stage = row.stage
    return EvaluationCaseRead(
        id=row.id,
        run_id=row.run_id,
        project_id=row.project_id,
        case_id=row.case_id,
        stage=row.stage,
        input_hash=row.input_hash,
        expected_hash=row.expected_hash,
        actual_hash=row.actual_hash,
        passed=row.passed,
        error_type=row.error_type,
        eval_source=row.eval_source,
        notes=row.notes,
        created_at=row.created_at,
        failed_stage=failed_stage,
    )


def _run_to_create_read(run: EvaluationRun) -> EvaluationRunCreateRead:
    """Adapt a fresh :class:`EvaluationRun` for the ``POST`` response.

    Includes the ``dataset_source`` marker so the caller can tell,
    without parsing ``notes``, that the dataset was the synthetic +
    unreviewed demo snapshot.
    """
    return EvaluationRunCreateRead(
        id=run.id,
        project_id=run.project_id,
        evaluation_type=run.evaluation_type,
        stage=run.stage,
        status=run.status,
        dataset_name=run.dataset_name,
        dataset_version=run.dataset_version,
        dataset_content_hash=run.dataset_content_hash,
        dataset_provenance=run.dataset_provenance,
        dataset_split=run.dataset_split,
        dataset_label_status=run.dataset_label_status,
        input_count=run.input_count,
        processed_count=run.processed_count,
        success_count=run.success_count,
        failed_count=run.failed_count,
        calculation_version=run.calculation_version,
        started_at=run.started_at,
        completed_at=run.completed_at,
        created_at=run.created_at,
        error_message=_safe_error_message(run.error_message),
        notes=run.notes,
    )


def _run_to_read(run: EvaluationRun) -> EvaluationRunRead:
    return EvaluationRunRead(
        id=run.id,
        project_id=run.project_id,
        evaluation_type=run.evaluation_type,
        stage=run.stage,
        status=run.status,
        dataset_name=run.dataset_name,
        dataset_version=run.dataset_version,
        dataset_content_hash=run.dataset_content_hash,
        dataset_provenance=run.dataset_provenance,
        dataset_split=run.dataset_split,
        dataset_label_status=run.dataset_label_status,
        model=run.model,
        prompt_version=run.prompt_version,
        pipeline_version=run.pipeline_version,
        calculation_version=run.calculation_version,
        input_count=run.input_count,
        processed_count=run.processed_count,
        success_count=run.success_count,
        failed_count=run.failed_count,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        unknown_token_calls=run.unknown_token_calls,
        latency_ms=run.latency_ms,
        total_cost_usd=run.total_cost_usd,
        attempt_count=run.attempt_count,
        retry_count=run.retry_count,
        started_at=run.started_at,
        completed_at=run.completed_at,
        created_at=run.created_at,
        error_message=_safe_error_message(run.error_message),
        notes=run.notes,
    )


def _stable_order_metric_rows(rows: Iterable[EvaluationMetric]) -> list[EvaluationMetric]:
    return sorted(rows, key=lambda r: (r.stage, r.metric_name, str(r.id)))


def _stable_order_case_rows(rows: Iterable[EvaluationCase]) -> list[EvaluationCase]:
    return sorted(rows, key=lambda r: (r.stage, r.case_id, str(r.id)))


# ---- Endpoints --------------------------------------------------------------


@router.post(
    "/evaluations",
    response_model=EvaluationRunCreateRead,
    status_code=200,
)
def create_evaluation(
    body: EvaluationCreateRequest = Body(...),
    db: Session = Depends(get_db),
) -> EvaluationRunCreateRead:
    """Score an existing Phase 1 / Phase 2 run against the demo dataset.

    The dataset is loaded internally — no client-supplied content
    ever enters the runner. Source-run validation (cross-project,
    wrong ``run_type``, non-terminal ``status``, dataset drift) is
    enforced by the runner BEFORE any DB write.
    """
    _require_project(db, body.project_id)
    try:
        dataset = load_demo_dataset()
    except DatasetValidationError as exc:
        raise HTTPException(503, _safe_error_message(str(exc)) or "演示数据集加载失败") from exc

    try:
        run = evaluate_existing_run(
            db,
            project_id=body.project_id,
            source_run_id=body.source_run_id,
            evaluation_type=body.evaluation_type,
            dataset=dataset,
        )
    except EvaluationRunnerSourceMismatchError as exc:
        # Includes: source-run not found, cross-project, wrong run_type,
        # non-terminal status. Treated as 409 because the caller can fix
        # the request (wrong source_run_id) or wait (non-terminal run).
        raise HTTPException(
            409, _safe_error_message(str(exc)) or "源运行不符合评估前置条件"
        ) from exc
    except EvaluationRunnerValidationError as exc:
        # Dataset content drift / cluster-label missing / consistency check.
        raise HTTPException(
            409, _safe_error_message(str(exc)) or "数据集与项目当前反馈不一致"
        ) from exc
    except EvaluationRunnerConfigError as exc:
        raise HTTPException(503, _safe_error_message(str(exc)) or "评估类型暂不支持") from exc
    except Exception as exc:  # pragma: no cover - defensive: never echo raw text
        # Any unanticipated exception (transport error, DB failure, …)
        # is collapsed to a generic 500 with no detail — SQL fragments,
        # parameter tuples, credential-like tokens MUST NOT escape
        # through the API surface.
        raise HTTPException(500, "评估运行失败，详情请查看运行 error_message 字段") from exc

    db.refresh(run)
    return _run_to_create_read(run)


@router.get("/evaluations/{run_id}", response_model=EvaluationRunRead)
def get_evaluation(
    run_id: UUID,
    project_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
) -> EvaluationRunRead:
    run = _require_run(db, run_id)
    _enforce_project_match(run, project_id)
    return _run_to_read(run)


@router.get(
    "/evaluations/{run_id}/metrics",
    response_model=EvaluationMetricPage,
)
def list_evaluation_metrics(
    run_id: UUID,
    project_id: UUID | None = Query(default=None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> EvaluationMetricPage:
    run = _require_run(db, run_id)
    _enforce_project_match(run, project_id)

    where = EvaluationMetric.run_id == run_id
    total = db.scalar(select(func.count()).select_from(EvaluationMetric).where(where))
    # Stable sort at the Python layer so ``limit``/``offset`` slicing
    # is consistent across pages. The DB ``ORDER BY`` would otherwise
    # collide on ``created_at`` for rows committed in the same
    # transaction and could miss / duplicate rows between pages.
    rows = db.scalars(select(EvaluationMetric).where(where)).all()
    ordered = _stable_order_metric_rows(rows)
    page = ordered[offset : offset + limit]
    items = [_metric_to_read(r) for r in page]
    return EvaluationMetricPage(
        total=total or 0,
        items=items,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/evaluations/{run_id}/cases",
    response_model=EvaluationCasePage,
)
def list_evaluation_cases(
    run_id: UUID,
    project_id: UUID | None = Query(default=None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> EvaluationCasePage:
    run = _require_run(db, run_id)
    _enforce_project_match(run, project_id)

    where = EvaluationCase.run_id == run_id
    total = db.scalar(select(func.count()).select_from(EvaluationCase).where(where))
    rows = db.scalars(select(EvaluationCase).where(where)).all()
    ordered = _stable_order_case_rows(rows)
    page = ordered[offset : offset + limit]
    items = [_case_to_read(r) for r in page]
    return EvaluationCasePage(
        total=total or 0,
        items=items,
        limit=limit,
        offset=offset,
    )


__all__ = ["router"]
