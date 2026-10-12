"""Phase 1 orchestration: create / start / retry an ``AnalysisRun``.

Synchronous execution — the request blocks until the run completes — keeps the
contract simple for the demo dataset and avoids introducing a queue. The run
object is committed at start, refreshed as batches finish, and finalised
before the API returns.

A retry call atomically claims the run via an ``UPDATE ... WHERE status IN
(...)`` so two concurrent retry requests cannot both delete the same failure
rows or both re-process the same feedbacks.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock
from uuid import UUID

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from app.agents.prompts.feedback_classification_v1 import PROMPT_VERSION
from app.db.config import settings
from app.models import (
    AnalysisRun,
    AnalysisRunFailure,
    Feedback,
    FeedbackAnalysis,
    RunStatus,
)
from app.services.classification import (
    ClassificationConfigError,
    ClassificationValidationError,
    derive_run_status,
    is_classification_configured,
    load_project_feedbacks,
    run_classification,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# SQLite tests have no advisory locks, so retain a process-local fallback.
# PostgreSQL uses a dedicated connection and a session-level advisory lock:
# batch commits cannot release it and a crashed worker releases it automatically.
_RUN_LOCKS: dict[UUID, Lock] = {}
_RUN_LOCKS_GUARD: Lock = Lock()


def _acquire_run_lock(run_id: UUID) -> Lock:
    with _RUN_LOCKS_GUARD:
        lock = _RUN_LOCKS.get(run_id)
        if lock is None:
            lock = Lock()
            _RUN_LOCKS[run_id] = lock
        return lock


@contextmanager
def _execution_lock(db: Session, run_id: UUID):
    if db.get_bind().dialect.name == "postgresql":
        key = run_id.int & 0x7FFF_FFFF_FFFF_FFFF
        # This connection stays checked out throughout the synchronous run.
        # The ORM session uses its own connection for each batch transaction.
        with db.get_bind().connect() as lock_connection:
            acquired = lock_connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
            )
            if not acquired:
                raise ValueError(f"run {run_id} is already being processed")
            try:
                yield
            finally:
                lock_connection.scalar(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                )
    else:
        lock = _acquire_run_lock(run_id)
        if not lock.acquire(blocking=False):
            raise ValueError(f"run {run_id} is already being processed")
        try:
            yield
        finally:
            lock.release()


_INTERRUPTED_RUN_STATUSES: frozenset[str] = frozenset(
    {
        RunStatus.RUNNING.value,
        RunStatus.FAILED.value,
        RunStatus.PARTIAL_FAILED.value,
    }
)


def start_run(db: Session, project_id: UUID, *, run_type: str = "classification") -> AnalysisRun:
    """Create + execute + finalise an analysis run synchronously."""
    if not is_classification_configured():
        raise ClassificationConfigError(
            "LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 未配置；无法启动真实分类。"
        )
    feedbacks = load_project_feedbacks(db, project_id)
    run = AnalysisRun(
        project_id=project_id,
        run_type=run_type,
        model=settings.llm_model,
        prompt_version=PROMPT_VERSION,
        status=RunStatus.RUNNING.value,
        input_count=len(feedbacks),
        started_at=_now(),
        # Snapshot the run's input set so a retry cannot be expanded by
        # later feedback imports in the same project.
        input_feedback_external_ids=[f.external_id for f in feedbacks],
    )
    db.add(run)
    db.flush()
    run_id = run.id
    with _execution_lock(db, run_id):
        db.commit()  # durable before any model call, lock held across batches.
        try:
            if feedbacks:
                run_classification(db, run, feedbacks)
            run.status = derive_run_status(run)
        except BaseException as exc:
            db.rollback()
            run = db.get(AnalysisRun, run_id)
            if run is not None:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    run.error_message = f"interrupted: {type(exc).__name__}"
                    run.status = RunStatus.RUNNING.value
                else:
                    run.status = RunStatus.FAILED.value
                    run.error_message = "classification execution failed"
                run.completed_at = _now()
                db.commit()
                db.refresh(run)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return run
        run.completed_at = _now()
        db.commit()
        db.refresh(run)
        return run


def retry_run(db: Session, run_id: UUID) -> AnalysisRun:
    """Retry only the failed / pending feedback IDs of an existing run.

    The retry first atomically claims the run via an ``UPDATE`` that only
    succeeds if the row is in an eligible state. A concurrent retry therefore
    returns ``409`` instead of corrupting the failure table or counters.
    Already-successful rows are left untouched and pending rows (no analysis
    and no failure record, e.g. after an interrupted process) are picked up
    too.

    PostgreSQL execution authority is held on a dedicated connection across
    every batch commit. SQLite tests use a process-local lock.
    """
    with _execution_lock(db, run_id):
        return _retry_run_locked(db, run_id)


def _retry_run_locked(db: Session, run_id: UUID) -> AnalysisRun:
    run = db.get(AnalysisRun, run_id)
    if run is None:
        raise LookupError(f"analysis run {run_id} not found")

    # Atomic claim. The UPDATE only matches if the run is still in an eligible
    # state — a concurrent retry will already have flipped the status, so its
    # UPDATE matches 0 rows and we raise a conflict instead of double-running.
    claim = db.execute(
        update(AnalysisRun)
        .where(
            AnalysisRun.id == run_id,
            AnalysisRun.status.in_(tuple(_INTERRUPTED_RUN_STATUSES)),
        )
        .values(
            status=RunStatus.RUNNING.value,
            started_at=_now(),
            error_message=None,
        )
        .returning(AnalysisRun.id)
    )
    if not claim.fetchall():
        raise ValueError(
            f"run {run_id} status {run.status!r} is not eligible for retry"
        )

    # A retry must use the same model and prompt the run was created with;
    # otherwise downstream metrics, evidence and reproducibility become
    # misleading. We capture the original prompt at run creation so a config
    # change between creation and retry is detected.
    if settings.llm_model != run.model:
        db.rollback()
        raise ValueError(
            f"run {run_id} was created with model {run.model!r}; "
            f"current LLM_MODEL={settings.llm_model!r} does not match"
        )
    if run.prompt_version != PROMPT_VERSION:
        db.rollback()
        raise ValueError(
            f"run {run_id} was created with prompt_version "
            f"{run.prompt_version!r}; current PROMPT_VERSION="
            f"{PROMPT_VERSION!r} does not match"
        )

    db.refresh(run)

    pending = _pending_feedbacks(db, run)
    if not pending:
        run.status = derive_run_status(run)
        run.completed_at = _now()
        db.commit()
        db.refresh(run)
        return run

    previously_failed_ids = {
        row[0]
        for row in db.execute(
            select(AnalysisRunFailure.feedback_id).where(
                AnalysisRunFailure.run_id == run.id,
                AnalysisRunFailure.feedback_id.in_([f.id for f in pending]),
            )
        ).all()
    }
    if previously_failed_ids:
        db.execute(
            delete(AnalysisRunFailure).where(
                AnalysisRunFailure.run_id == run.id,
                AnalysisRunFailure.feedback_id.in_(list(previously_failed_ids)),
            )
        )
        n = len(previously_failed_ids)
        run.failed_count = max(run.failed_count - n, 0)
        run.processed_count = max(run.processed_count - n, 0)
        db.flush()

    try:
        run_classification(db, run, pending)
        run.status = derive_run_status(run)
    except BaseException as exc:
        db.rollback()
        run = db.get(AnalysisRun, run_id)
        if run is not None:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                run.error_message = f"interrupted: {type(exc).__name__}"
                run.status = RunStatus.RUNNING.value
            else:
                run.status = RunStatus.FAILED.value
                run.error_message = "classification retry failed"
            run.completed_at = _now()
            db.commit()
            db.refresh(run)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return run

    run.completed_at = _now()
    db.commit()
    db.refresh(run)
    return run


def _pending_feedbacks(db: Session, run: AnalysisRun) -> list[Feedback]:
    """Feedbacks that still need processing for this run.

    "Pending" means "no FeedbackAnalysis row yet for (feedback_id, run_id)".
    Such a feedback may or may not already have a failure record (e.g. a
    process was interrupted before the failure row was committed) and is
    always re-run by ``retry_run``.

    The set of eligible feedbacks is the **snapshot** captured at
    ``start_run`` (``input_feedback_external_ids``) intersected with the
    feedback rows that still exist. New imports that happened after the
    run was created are never expanded into the retry input — only the
    originally captured external IDs are processed.
    """
    snapshot = list(run.input_feedback_external_ids or [])
    if not snapshot:
        return []
    project_feedbacks = load_project_feedbacks(db, run.project_id)
    by_external = {f.external_id: f for f in project_feedbacks}
    eligible = [by_external[eid] for eid in snapshot if eid in by_external]
    if not eligible:
        return []
    analyzed_ids = {
        row[0]
        for row in db.execute(
            select(FeedbackAnalysis.feedback_id).where(
                FeedbackAnalysis.run_id == run.id
            )
        ).all()
    }
    return [f for f in eligible if f.id not in analyzed_ids]


__all__ = ["start_run", "retry_run", "ClassificationValidationError"]
