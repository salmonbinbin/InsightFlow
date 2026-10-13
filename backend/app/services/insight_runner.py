"""Phase 3 — Insight run orchestration.

Mirrors :mod:`app.services.analysis_runner` and
:mod:`app.services.cluster_runner`:

* The request blocks until the insight run completes (synchronous 200).
* Each cluster is processed independently; per-cluster failures are
  captured on ``insight_run_items`` with a safe error code.
* A retry atomically claims the run via ``UPDATE … WHERE status IN (…)``
  so two concurrent retries cannot both rebuild the same insight rows.
* Retry preserves insights from already-successful clusters and only
  re-processes clusters whose item row is ``failed`` or
  ``needs_validation``.
* Cross-process locks use PostgreSQL ``pg_try_advisory_lock``; the unit
  tests fall back to a process-local ``threading.Lock``.

The Insights schema is bound to one ``clustering_run_id`` and the
classification run that fed it. The runner validates the cluster /
classification snapshot on every start and retry so a configuration
change between cluster and insight runs is detected and rejected.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock
from typing import Sequence
from uuid import UUID

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from app.agents.prompts.insight_generation_v2 import PROMPT_VERSION
from app.db.config import settings
from app.models import (
    AnalysisRun,
    Cluster,
    ClusterMember,
    ClusterSource,
    Feedback,
    FeedbackAnalysis,
    Insight,
    InsightEvidence,
    InsightItemStatus,
    InsightRun,
    InsightRunItem,
    InsightStatus,
    RunStatus,
)
from app.services.insight_client import (
    InsightClient,
    InsightLLMConfigError,
    InsightLLMProtocolError,
    InsightLLMTransportError,
)
from app.services.insight_service import (
    DRAFT_EVIDENCE_THRESHOLD,
    EVIDENCE_SELECTION_MAX,
    EvidenceCandidate,
    InsightValidationError,
    ValidatedInsight,
    select_evidence_candidates,
    validate_insight_payload,
)

logger = logging.getLogger(__name__)


_RUN_LOCKS: dict[UUID, Lock] = {}
_RUN_LOCKS_GUARD: Lock = Lock()
_INTERRUPTED_RUN_STATUSES: frozenset[str] = frozenset(
    {
        RunStatus.RUNNING.value,
        RunStatus.FAILED.value,
        RunStatus.PARTIAL_FAILED.value,
    }
)

# Cluster rows the LLM review left empty (sources that were drained into
# downstream merges / outliers) are not eligible for a new insight.
OUTLIER_SOURCE_VALUES: frozenset[str] = frozenset({ClusterSource.OUTLIER.value})

# Phase 3 retries are fixed at 2 — per the AGENT_HANDOFF and the
# supervisor's brief. We do not surface this as configuration so
# downstream metric stays stable.
INSIGHT_FIXED_MAX_RETRIES: int = 2

# Statuses that warrant a retry of the same cluster.
RETRY_ITEM_STATUSES: frozenset[str] = frozenset(
    {
        InsightItemStatus.FAILED.value,
        InsightItemStatus.NEEDS_VALIDATION.value,
    }
)


class InsightConfigError(RuntimeError):
    """Raised when the project has no usable clustering input."""


# ---- Execution lock (mirrors analysis_runner pattern) -----------------------


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
        with db.get_bind().connect() as lock_connection:
            acquired = lock_connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
            )
            if not acquired:
                raise InsightConfigError(f"insight run {run_id} is already being processed")
            try:
                yield
            finally:
                lock_connection.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
    else:
        lock = _acquire_run_lock(run_id)
        if not lock.acquire(blocking=False):
            raise InsightConfigError(f"insight run {run_id} is already being processed")
        try:
            yield
        finally:
            lock.release()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---- Public API ------------------------------------------------------------


def start_insight_run(db: Session, project_id: UUID, *, clustering_run_id: UUID) -> InsightRun:
    """Create + execute + finalise an insight run synchronously.

    The clustering run is the explicit input; ``InsightConfigError`` is
    raised when the cluster chain is missing or no eligible clusters
    remain. Returns the persisted run with committed counters.
    """
    if not settings.llm_api_key or not settings.llm_base_url or not settings.llm_model:
        raise InsightConfigError(
            "LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 未配置；无法启动真实洞察生成。"
        )

    classification_run, clustering_run, inputs = _load_inputs(db, project_id, clustering_run_id)

    run = InsightRun(
        project_id=project_id,
        clustering_run_id=clustering_run.id,
        classification_run_id=classification_run.id,
        status=RunStatus.RUNNING.value,
        model=settings.llm_model,
        prompt_version=PROMPT_VERSION,
        input_count=len(inputs["clusters"]),
        started_at=_now(),
        input_cluster_external_ids=[c["cluster_external_id"] for c in inputs["clusters"]],
        input_member_external_ids={
            c["cluster_external_id"]: c["member_external_ids"] for c in inputs["clusters"]
        },
        input_snapshot=_build_input_snapshot(inputs),
    )
    db.add(run)
    db.flush()
    run_id = run.id
    with _execution_lock(db, run_id):
        db.commit()
        try:
            _execute_pipeline(db, run, inputs)
        except BaseException as exc:
            db.rollback()
            run = db.get(InsightRun, run_id)
            if run is not None:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    run.error_message = f"interrupted: {type(exc).__name__}"
                    run.status = RunStatus.RUNNING.value
                else:
                    run.status = RunStatus.FAILED.value
                    run.error_message = "insight execution failed: " + type(exc).__name__
                _rebuild_run_counters_from_items(db, run)
                run.completed_at = _now()
                db.commit()
                db.refresh(run)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return run
        _finalise_run(db, run)
        return run


def _build_input_snapshot(inputs: dict) -> dict:
    """Hash the raw feedback content of every cluster member.

    Stored on ``insight_runs.input_snapshot`` so retries can detect any
    drift in membership or raw text without silently accepting it.
    """
    snapshot: dict[str, dict] = {}
    for cluster in inputs["clusters"]:
        members: dict[str, str] = {}
        for fid, _ in cluster["eligible_members"]:
            feedback = cluster["feedback_by_id"][fid]
            members[str(fid)] = hashlib.sha256((feedback.content or "").encode("utf-8")).hexdigest()
        snapshot[cluster["cluster_external_id"]] = {
            "member_feedback_ids": [str(fid) for fid, _ in cluster["eligible_members"]],
            "member_content_hashes": members,
        }
    return snapshot


def retry_insight_run(db: Session, run_id: UUID) -> InsightRun:
    """Retry only the clusters whose item status is failed / needs_validation.

    A completed run is a no-op; the API still returns the existing run so
    callers can observe the immutable audit history. Items whose status is
    ``success`` are preserved verbatim (the insight row stays intact).
    """
    with _execution_lock(db, run_id):
        return _retry_insight_run_locked(db, run_id)


def _retry_insight_run_locked(db: Session, run_id: UUID) -> InsightRun:
    run = db.get(InsightRun, run_id)
    if run is None:
        raise LookupError(f"insight run {run_id} not found")
    if run.status == RunStatus.COMPLETED.value:
        db.refresh(run)
        return run

    cluster_cfg_changed = settings.llm_model != run.model or PROMPT_VERSION != run.prompt_version
    if cluster_cfg_changed:
        db.rollback()
        raise InsightConfigError(
            "insight run model/prompt binding changed; "
            "restore original configuration or start a new insight run"
        )

    if run.prompt_version != PROMPT_VERSION:
        db.rollback()
        raise InsightConfigError(
            f"insight run was created with prompt_version {run.prompt_version!r}; "
            f"current PROMPT_VERSION={PROMPT_VERSION!r} does not match"
        )

    # Verify the source chain first, BEFORE mutating any state. A drift
    # must reject the retry without touching status / retry_count.
    try:
        _verify_source_against_snapshot(db, run)
    except InsightConfigError:
        db.rollback()
        raise

    # Load inputs and verify membership / raw-text against the snapshot
    # before any mutation. This catches drift without claiming the run.
    try:
        classification_run, clustering_run, inputs = _load_inputs(
            db, run.project_id, run.clustering_run_id
        )
        _validate_inputs_against_snapshot(run.input_snapshot, inputs)
    except InsightConfigError:
        db.rollback()
        raise

    # All preflight checks passed; now atomically claim the run.
    claim = db.execute(
        update(InsightRun)
        .where(
            InsightRun.id == run_id,
            InsightRun.status.in_(tuple(_INTERRUPTED_RUN_STATUSES)),
        )
        .values(
            status=RunStatus.RUNNING.value,
            started_at=_now(),
            error_message=None,
        )
        .returning(InsightRun.id)
    )
    if not claim.fetchall():
        db.refresh(run)
        return run

    # Pending = original snapshot cluster set MINUS terminal items.
    # Both ``success`` and ``needs_validation`` count as terminal — the
    # LLM produced a usable result; only ``failed`` items and clusters
    # without any item row (interrupted before recording) need retry.
    snapshot_clusters = set(run.input_cluster_external_ids or [])
    existing_items = list(
        db.scalars(select(InsightRunItem).where(InsightRunItem.run_id == run_id)).all()
    )
    terminal_statuses = {
        InsightItemStatus.SUCCESS.value,
        InsightItemStatus.NEEDS_VALIDATION.value,
    }
    covered_cluster_ids = {
        str(item.cluster_id) for item in existing_items if item.status in terminal_statuses
    }
    pending_cluster_ids = {UUID(cid) for cid in snapshot_clusters - covered_cluster_ids}
    db.refresh(run)

    if not pending_cluster_ids:
        run.retry_count += 1
        _finalise_run(db, run)
        return run

    db.execute(
        delete(InsightEvidence).where(
            InsightEvidence.insight_id.in_(
                select(Insight.id).where(
                    Insight.run_id == run_id,
                    Insight.cluster_id.in_(pending_cluster_ids),
                )
            )
        )
    )
    db.execute(
        delete(Insight).where(
            Insight.run_id == run_id,
            Insight.cluster_id.in_(pending_cluster_ids),
        )
    )

    inputs["clusters"] = [c for c in inputs["clusters"] if c["cluster_id"] in pending_cluster_ids]

    run.retry_count += 1
    _rebuild_run_counters_from_items(db, run)
    db.commit()

    if not inputs["clusters"]:
        _finalise_run(db, run)
        return run

    try:
        _execute_pipeline(db, run, inputs)
    except BaseException as exc:
        db.rollback()
        run = db.get(InsightRun, run_id)
        if run is not None:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                run.error_message = f"interrupted: {type(exc).__name__}"
                run.status = RunStatus.RUNNING.value
            else:
                run.status = RunStatus.FAILED.value
                run.error_message = "insight retry failed"
            _rebuild_run_counters_from_items(db, run)
            run.completed_at = _now()
            db.commit()
            db.refresh(run)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return run

    _finalise_run(db, run)
    return run


def _verify_source_against_snapshot(db: Session, run: InsightRun) -> None:
    """Reject retry when the clustering / classification chain has drifted.

    Checks performed before any mutation:

    * ``clustering_run.cluster_config.classification_run_id`` still
      matches ``run.classification_run_id``.
    * The classification run itself is unchanged (status / counters).
    * The *eligible* set of cluster UUIDs (excluding outliers and
      empty algorithmic parents — see :func:`_load_inputs`) still
      matches ``run.input_cluster_external_ids`` exactly.
    """
    clustering_run = db.get(AnalysisRun, run.clustering_run_id)
    if (
        clustering_run is None
        or clustering_run.project_id != run.project_id
        or clustering_run.run_type != "clustering"
        or clustering_run.status != RunStatus.COMPLETED.value
    ):
        raise InsightConfigError("clustering run 不可用；拒绝重试")

    cfg = clustering_run.cluster_config or {}
    cfg_classification_id = cfg.get("classification_run_id")
    if cfg_classification_id != str(run.classification_run_id):
        raise InsightConfigError("clustering run 的 classification_run_id 已变更；拒绝重试")

    classification_run = db.get(AnalysisRun, run.classification_run_id)
    if (
        classification_run is None
        or classification_run.project_id != run.project_id
        or classification_run.run_type != "classification"
        or classification_run.status != RunStatus.COMPLETED.value
        or classification_run.processed_count != classification_run.input_count
        or classification_run.success_count != classification_run.input_count
    ):
        raise InsightConfigError("classification run 不完整；拒绝重试")

    snapshot_cluster_ids = set(run.input_cluster_external_ids or [])

    # The snapshot stores the *eligible* cluster set (outliers and
    # empty parents are filtered out at run start). Re-apply the same
    # filter when comparing to the live classification run, otherwise a
    # legal excluded cluster would falsely read as drift.
    live_classification = db.get(AnalysisRun, UUID(cfg_classification_id))
    live_eligible_ids = {
        str(c.id)
        for c in db.scalars(select(Cluster).where(Cluster.run_id == clustering_run.id)).all()
        if c.project_id == run.project_id
        and c.source not in OUTLIER_SOURCE_VALUES
        and not (c.source == ClusterSource.ALGORITHMIC.value and c.feedback_count == 0)
        and _cluster_has_eligible_members(db, c, live_classification.id, run.project_id)
    }
    if live_eligible_ids != snapshot_cluster_ids:
        missing = snapshot_cluster_ids - live_eligible_ids
        added = live_eligible_ids - snapshot_cluster_ids
        raise InsightConfigError(
            "eligible cluster 集合已变化；拒绝重试"
            + (f"（缺失: {sorted(missing)}）" if missing else "")
            + (f"（新增: {sorted(added)}）" if added else "")
        )


def _cluster_has_eligible_members(
    db: Session, cluster: Cluster, classification_run_id: UUID, project_id: UUID
) -> bool:
    """Return True when the cluster has at least one eligible member row."""
    rows = db.execute(
        select(ClusterMember, FeedbackAnalysis, Feedback)
        .join(
            FeedbackAnalysis,
            FeedbackAnalysis.id == ClusterMember.feedback_analysis_id,
        )
        .join(Feedback, Feedback.id == FeedbackAnalysis.feedback_id)
        .where(ClusterMember.cluster_id == cluster.id)
    ).all()
    for _member, analysis, feedback in rows:
        if feedback.project_id == project_id and analysis.run_id == classification_run_id:
            return True
    return False


def _validate_inputs_against_snapshot(snapshot: dict | None, inputs: dict) -> None:
    """Reject the retry when membership or raw text drifted.

    Verifies each cluster's member feedback UUIDs and raw-content hashes
    still match the snapshot taken at run start. Any drift raises
    ``InsightConfigError``; the caller is expected to roll back.
    """
    if not snapshot:
        return
    live_by_id: dict[str, dict] = {c["cluster_external_id"]: c for c in inputs["clusters"]}
    for cluster_external_id, cluster_snapshot in snapshot.items():
        if cluster_external_id not in live_by_id:
            raise InsightConfigError(f"cluster {cluster_external_id} 已从数据库消失；拒绝重试")
        cluster = live_by_id[cluster_external_id]
        live_member_ids = {str(fid) for fid, _ in cluster["eligible_members"]}
        snapshot_member_ids = set(cluster_snapshot.get("member_feedback_ids") or [])
        if live_member_ids != snapshot_member_ids:
            missing = snapshot_member_ids - live_member_ids
            added = live_member_ids - snapshot_member_ids
            raise InsightConfigError(
                f"cluster {cluster_external_id} 成员已变化；拒绝重试"
                + (f"（缺失: {sorted(missing)}）" if missing else "")
                + (f"（新增: {sorted(added)}）" if added else "")
            )
        live_hashes: dict[str, str] = {}
        for fid, _ in cluster["eligible_members"]:
            feedback = cluster["feedback_by_id"][fid]
            live_hashes[str(fid)] = hashlib.sha256(
                (feedback.content or "").encode("utf-8")
            ).hexdigest()
        if live_hashes != cluster_snapshot.get("member_content_hashes") or {}:
            raise InsightConfigError(f"cluster {cluster_external_id} 原文已变化；拒绝重试")


# ---- Cluster loading + invariant enforcement -------------------------------


def _load_inputs(
    db: Session,
    project_id: UUID,
    clustering_run_id: UUID,
) -> tuple[AnalysisRun, AnalysisRun, dict]:
    """Validate and snapshot cluster inputs for an insight run.

    Returns ``(classification_run, clustering_run, {"clusters": [...]})``.
    Each cluster dict carries ``cluster_id``, ``cluster_external_id`` and
    ``member_external_ids`` (project-local feedback external_ids). The
    snapshot form is used by retry so the input set is preserved.
    """
    clustering_run = db.get(AnalysisRun, clustering_run_id)
    if clustering_run is None or clustering_run.project_id != project_id:
        raise InsightConfigError("指定的 clustering run 不存在或不属于该项目")
    if clustering_run.run_type != "clustering":
        raise InsightConfigError("指定的 analysis run 不是聚类运行")
    if clustering_run.status != RunStatus.COMPLETED.value:
        raise InsightConfigError("聚类运行尚未完成；无法生成洞察")
    cfg = clustering_run.cluster_config or {}
    classification_run_id_str = cfg.get("classification_run_id")
    if not classification_run_id_str:
        raise InsightConfigError("聚类运行缺少 classification snapshot")
    try:
        classification_run_uuid = UUID(classification_run_id_str)
    except ValueError as exc:
        raise InsightConfigError("classification_run_id 不可解析") from exc
    classification_run = db.get(AnalysisRun, classification_run_uuid)
    if (
        classification_run is None
        or classification_run.project_id != project_id
        or classification_run.run_type != "classification"
        or classification_run.status != RunStatus.COMPLETED.value
        or classification_run.processed_count != classification_run.input_count
        or classification_run.success_count != classification_run.input_count
    ):
        raise InsightConfigError("classification 运行不完整；无法生成洞察")

    clusters = db.scalars(
        select(Cluster)
        .where(Cluster.run_id == clustering_run.id)
        .order_by(Cluster.created_at, Cluster.id)
    ).all()
    if not clusters:
        raise InsightConfigError("聚类运行不包含任何簇；无法生成洞察")

    eligible_clusters: list[dict] = []
    for cluster in clusters:
        if cluster.project_id != project_id:
            raise InsightConfigError("簇不属于该项目；拒绝生成洞察")
        if cluster.source in OUTLIER_SOURCE_VALUES:
            continue
        # An "algorithmic parent whose feedback_count == 0" is a cluster
        # whose membership was drained into a sibling (LLM merge / split).
        # It is not eligible for a fresh insight because all its members
        # now live under the surviving child clusters.
        if cluster.source == ClusterSource.ALGORITHMIC.value and cluster.feedback_count == 0:
            continue

        rows = db.execute(
            select(ClusterMember, FeedbackAnalysis, Feedback)
            .join(
                FeedbackAnalysis,
                FeedbackAnalysis.id == ClusterMember.feedback_analysis_id,
            )
            .join(Feedback, Feedback.id == FeedbackAnalysis.feedback_id)
            .where(ClusterMember.cluster_id == cluster.id)
            .order_by(ClusterMember.created_at, ClusterMember.id)
        ).all()

        analysis_by_id: dict[UUID, FeedbackAnalysis] = {}
        feedback_by_id: dict[UUID, Feedback] = {}
        similarity_by_id: dict[UUID, float] = {}
        eligible_member_ids: list[tuple[UUID, float]] = []
        for member, analysis, feedback in rows:
            if feedback.project_id != project_id:
                raise InsightConfigError("簇内成员反馈不属于该项目；拒绝生成洞察")
            if analysis.run_id != classification_run.id:
                raise InsightConfigError(
                    "簇内成员 Analysis 来自不同的 classification run；拒绝生成洞察"
                )
            analysis_by_id[feedback.id] = analysis
            feedback_by_id[feedback.id] = feedback
            similarity_by_id[feedback.id] = float(member.similarity_score)
            eligible_member_ids.append((feedback.id, float(member.similarity_score)))

        if not eligible_member_ids:
            continue

        member_external_ids = [feedback_by_id[fid].external_id for fid, _ in eligible_member_ids]
        eligible_clusters.append(
            {
                "cluster_id": cluster.id,
                "cluster_external_id": str(cluster.id),
                "cluster_row": cluster,
                "eligible_members": eligible_member_ids,
                "member_external_ids": member_external_ids,
                "feedback_by_id": feedback_by_id,
                "analysis_by_id": analysis_by_id,
                "similarity_by_id": similarity_by_id,
            }
        )

    if not eligible_clusters:
        raise InsightConfigError("聚类运行无合格簇可生成洞察（已排除 outlier 与空父簇）")

    return classification_run, clustering_run, {"clusters": eligible_clusters}


# ---- Pipeline --------------------------------------------------------------


def _rebuild_run_counters_from_items(db: Session, run: InsightRun) -> None:
    """Re-derive counters from the items table and persist final state.

    Used by both ``start_insight_run`` and ``retry_insight_run`` so the
    pipeline produces the same terminal shape regardless of entry point.
    Counter fields not exposed by the items table (input/output tokens,
    latency, attempts, unknown_token_calls) are left untouched on
    ``run`` so the model accounting survives a downstream failure.
    """
    items = db.scalars(select(InsightRunItem).where(InsightRunItem.run_id == run.id)).all()
    run.processed_count = len(items)
    run.success_count = sum(1 for it in items if it.status == InsightItemStatus.SUCCESS.value)
    run.failed_count = sum(1 for it in items if it.status == InsightItemStatus.FAILED.value)
    run.needs_validation_count = sum(
        1 for it in items if it.status == InsightItemStatus.NEEDS_VALIDATION.value
    )
    if run.status != RunStatus.RUNNING.value:
        run.status = _derive_status(run)


def _execute_pipeline(
    db: Session,
    run: InsightRun,
    inputs: dict,
) -> None:
    clusters: list[dict] = list(inputs["clusters"])
    # Build initial counters from items — preserves any terminal items
    # already persisted (e.g. a successful cluster from a prior run) so
    # progress is visible during the model call.
    _rebuild_run_counters_from_items(db, run)
    db.commit()

    if not clusters:
        run.completed_at = _now()
        db.commit()
        return

    try:
        with InsightClient(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
        ) as client:
            for cluster in clusters:
                status, error_message = _process_one_cluster(db, run, cluster, client=client)
                _rebuild_run_counters_from_items(db, run)
                db.commit()
    except (
        InsightLLMConfigError,
        InsightLLMProtocolError,
        InsightLLMTransportError,
    ) as exc:
        for cluster in clusters:
            if cluster.get("_processed"):
                continue
            cluster["_processed"] = True
            _record_item(
                db,
                run,
                cluster,
                status=InsightItemStatus.FAILED.value,
                attempts=1,
                error_message=_safe_error_message(type(exc).__name__),
                feedback_count=len(cluster["eligible_members"]),
                evidence_count=0,
            )
            _rebuild_run_counters_from_items(db, run)
            db.commit()


def _finalise_run(db: Session, run: InsightRun) -> None:
    """Re-derive counters and stamp the terminal state on the run.

    Delegates the counter rebuild to
    :func:`_rebuild_run_counters_from_items` so the same logic is used
    by the success path, the early-failure path and the unexpected
    error path.
    """
    _rebuild_run_counters_from_items(db, run)
    run.status = _derive_status(run)
    run.completed_at = _now()
    db.commit()
    db.refresh(run)


def _process_one_cluster(
    db: Session,
    run: InsightRun,
    cluster: dict,
    *,
    client: InsightClient,
    max_retries: int = INSIGHT_FIXED_MAX_RETRIES,
) -> tuple[str, str | None]:
    """Process one cluster end-to-end and return ``(status, last_error)``.

    Returns ``("success", None)`` for clusters whose insight reaches
    ``DRAFT_EVIDENCE_THRESHOLD`` evidence rows, ``("needs_validation",
    None)`` for clusters whose evidence is below the threshold (the LLM
    confirmed the cluster but the surface is too thin to claim a full
    product insight), and ``("failed", code)`` when retries are
    exhausted.
    """
    eligible = cluster["eligible_members"]
    feedback_by_id = cluster["feedback_by_id"]
    analysis_by_id = cluster["analysis_by_id"]
    similarity_by_id = cluster["similarity_by_id"]
    candidates = _build_evidence_candidates(
        eligible, feedback_by_id, analysis_by_id, similarity_by_id
    )
    selected = select_evidence_candidates(candidates)
    cluster_payload = _build_cluster_payload(cluster, selected)

    feedback_count = len(eligible)
    if not selected:
        _record_item(
            db,
            run,
            cluster,
            status=InsightItemStatus.FAILED.value,
            attempts=1,
            error_message=_safe_error_message("no_evidence_candidates"),
            feedback_count=feedback_count,
            evidence_count=0,
        )
        return InsightItemStatus.FAILED.value, "no_evidence_candidates"

    min_evidence = min(DRAFT_EVIDENCE_THRESHOLD, len(selected))

    last_error: str | None = None
    outcome: ValidatedInsight | None = None
    cluster_attempts = 0
    for attempt_index in range(max_retries + 1):
        cluster_attempts += 1
        started = time.monotonic()
        try:
            result = client.generate_insight(cluster_payload)
            latency_ms = int((time.monotonic() - started) * 1000)
            run.latency_ms += max(latency_ms, 0)
            run.attempt_count += 1
            run.input_tokens += result.usage.input_tokens or 0
            run.output_tokens += result.usage.output_tokens or 0
            if result.usage.input_tokens is None or result.usage.output_tokens is None:
                run.unknown_token_calls += 1
            db.commit()
            try:
                outcome = validate_insight_payload(
                    result.payload,
                    allowed_feedback_ids=[c.feedback_id for c in selected],
                    min_evidence=min_evidence,
                )
                break
            except InsightValidationError as exc:
                last_error = exc.code
                db.commit()
        except (InsightLLMTransportError, InsightLLMProtocolError) as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            run.latency_ms += max(latency_ms, 0)
            run.attempt_count += 1
            run.unknown_token_calls += 1
            last_error = type(exc).__name__
            db.commit()
        if attempt_index >= max_retries:
            break
        time.sleep(0.05 * (2**attempt_index))

    if outcome is None:
        _record_item(
            db,
            run,
            cluster,
            status=InsightItemStatus.FAILED.value,
            attempts=cluster_attempts,
            error_message=_safe_error_message(last_error or "unknown error"),
            feedback_count=feedback_count,
            evidence_count=0,
        )
        return InsightItemStatus.FAILED.value, last_error or "unknown error"

    # Persist the insight + evidence + per-cluster item in a single
    # transaction. LLM accounting counters above were committed in
    # earlier passes and intentionally survive a DB writer failure here.
    try:
        evidence_count = _persist_insight(db, run, cluster, selected, outcome)
    except Exception as exc:  # pragma: no cover - DB failure handled below
        db.rollback()
        _record_item(
            db,
            run,
            cluster,
            status=InsightItemStatus.FAILED.value,
            attempts=cluster_attempts,
            error_message=_safe_error_message(type(exc).__name__),
            feedback_count=feedback_count,
            evidence_count=0,
        )
        return InsightItemStatus.FAILED.value, type(exc).__name__

    status = (
        InsightItemStatus.SUCCESS.value
        if evidence_count >= DRAFT_EVIDENCE_THRESHOLD
        else InsightItemStatus.NEEDS_VALIDATION.value
    )
    _record_item(
        db,
        run,
        cluster,
        status=status,
        attempts=cluster_attempts,
        error_message=None,
        feedback_count=feedback_count,
        evidence_count=evidence_count,
    )
    return status, None


def _build_evidence_candidates(
    eligible: Sequence[tuple[UUID, float]],
    feedback_by_id: dict[UUID, Feedback],
    analysis_by_id: dict[UUID, FeedbackAnalysis],
    similarity_by_id: dict[UUID, float],
) -> list[EvidenceCandidate]:
    candidates: list[EvidenceCandidate] = []
    for fid, similarity in eligible:
        feedback = feedback_by_id.get(fid)
        analysis = analysis_by_id.get(fid)
        if feedback is None or analysis is None:
            continue
        candidates.append(
            EvidenceCandidate(
                feedback_id=str(feedback.id),
                feedback_pk=str(feedback.id),
                external_id=feedback.external_id,
                content=feedback.content or "",
                similarity_score=similarity,
                severity=analysis.severity,
                sentiment=analysis.sentiment,
            )
        )
    return candidates


def _build_cluster_payload(cluster: dict, selected: list[EvidenceCandidate]) -> dict:
    # The LLM prompt is bounded to a 3-8 evidence range for the schema
    # contract, but small clusters only have ≤ 2 candidates. We tell the
    # LLM the actual minimum so it can return 1-2 evidence when no more
    # is available; the validator applies the same floor.
    min_evidence = min(DRAFT_EVIDENCE_THRESHOLD, max(len(selected), 1))
    return {
        "cluster_id": cluster["cluster_external_id"],
        "feedback_count": len(cluster["eligible_members"]),
        "evidence_candidates": [
            {
                "feedback_id": c.feedback_id,
                "feedback_external_id": c.external_id,
                "severity": c.severity,
                "sentiment": c.sentiment,
                "similarity_score": round(c.similarity_score, 6),
                "content": c.content,
            }
            for c in selected
        ],
        "instructions": {
            "min_evidence": min_evidence,
            "max_evidence": EVIDENCE_SELECTION_MAX,
            "evidence_types": [
                "representative",
                "high_severity",
                "diverse_expression",
            ],
            "affected_segment_unknown_placeholder": "unknown",
        },
    }


def _persist_insight(
    db: Session,
    run: InsightRun,
    cluster: dict,
    selected: list[EvidenceCandidate],
    validated: ValidatedInsight,
) -> int:
    """Persist one Insight row + its evidence rows + item in one transaction.

    Returns the number of evidence rows persisted. The caller commits
    the surrounding counters in the same transaction so a DB writer
    failure rolls back the whole cluster atomically.
    """
    candidates_by_id = {c.feedback_id: c for c in selected}
    cluster_id = cluster["cluster_id"]
    source_external = cluster["cluster_external_id"]
    member_external_ids = cluster["member_external_ids"]
    feedback_count = len(cluster["eligible_members"])

    insight = Insight(
        run_id=run.id,
        project_id=run.project_id,
        cluster_id=cluster_id,
        title=validated.title,
        pain_point=validated.pain_point,
        affected_segment=validated.affected_segment,
        insight=validated.insight,
        recommendation=validated.recommendation,
        confidence=validated.confidence,
        status=InsightStatus.DRAFT.value,
        feedback_count=feedback_count,
        evidence_count=0,
        source_cluster_external_id=source_external,
        source_member_external_ids=list(member_external_ids),
        model=settings.llm_model,
        prompt_version=PROMPT_VERSION,
    )
    db.add(insight)
    db.flush()

    evidence_count = 0
    for entry in validated.evidence:
        candidate = candidates_by_id.get(entry.feedback_id)
        if candidate is None:
            continue
        db.add(
            InsightEvidence(
                insight_id=insight.id,
                feedback_id=UUID(candidate.feedback_id),
                evidence_type=entry.evidence_type,
                relevance_score=entry.relevance_score,
                feedback_external_id=candidate.external_id,
                feedback_project_id=run.project_id,
                feedback_classification_run_id=run.classification_run_id,
            )
        )
        evidence_count += 1
    insight.evidence_count = evidence_count
    if evidence_count < DRAFT_EVIDENCE_THRESHOLD:
        insight.status = InsightStatus.NEEDS_VALIDATION.value
    db.flush()
    return evidence_count


def _record_item(
    db: Session,
    run: InsightRun,
    cluster: dict,
    *,
    status: str,
    attempts: int,
    error_message: str | None,
    feedback_count: int,
    evidence_count: int,
) -> None:
    existing = db.scalar(
        select(InsightRunItem).where(
            InsightRunItem.run_id == run.id,
            InsightRunItem.cluster_id == cluster["cluster_id"],
        )
    )
    safe_error = _safe_error_message(error_message)
    safe_attempts = max(attempts, 1)
    if existing is None:
        db.add(
            InsightRunItem(
                run_id=run.id,
                cluster_id=cluster["cluster_id"],
                source_cluster_external_id=cluster["cluster_external_id"],
                status=status,
                attempts=safe_attempts,
                last_error=safe_error,
                feedback_count=feedback_count,
                evidence_count=evidence_count,
            )
        )
    else:
        existing.status = status
        existing.attempts = existing.attempts + safe_attempts
        existing.last_error = safe_error
        existing.feedback_count = feedback_count
        existing.evidence_count = evidence_count
        existing.updated_at = _now()
    db.flush()


def _derive_status(run: InsightRun) -> str:
    """Translate cluster-level outcomes into the five-state run status.

    ``needs_validation`` clusters count as terminal success — the LLM
    produced a usable insight; only human review is pending. ``failed``
    items are the only ones that drag a run down to ``partial_failed``
    or ``failed``.
    """
    if run.input_count == 0:
        return RunStatus.COMPLETED.value
    if run.processed_count < run.input_count:
        return RunStatus.FAILED.value
    if run.failed_count == 0:
        return RunStatus.COMPLETED.value
    if run.success_count == 0 and run.needs_validation_count == 0:
        return RunStatus.FAILED.value
    return RunStatus.PARTIAL_FAILED.value


# ---- Safe error logging -----------------------------------------------------

_SAFE_ERROR_MAX_LEN = 4000

_SENSITIVE_TOKEN_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_\-]{6,}|"
    r"AKIA[0-9A-Z]{8,}|"
    r"x-api-key=[^\s,;]+|"
    r"api[_-]?key[=:][^\s,;]+|"
    r"bearer\s+[A-Za-z0-9._\-]{6,}|"
    r"FAKE_[A-Z0-9_]+_CREDENTIAL[A-Z0-9_]*|"
    r"password[=:][^\s,;]+|"
    r"secret[=:][^\s,;]+|"
    r"token[=:][A-Za-z0-9._\-]{6,})",
    re.IGNORECASE,
)


def _safe_error_message(message: str | None) -> str:
    text = "" if message is None else str(message)
    text = _SENSITIVE_TOKEN_RE.sub(" [REDACTED] ", text)
    text = text.replace("\x00", " ")
    text = " ".join(text.split())
    if len(text) > _SAFE_ERROR_MAX_LEN:
        text = text[:_SAFE_ERROR_MAX_LEN]
    return text


__all__ = [
    "InsightConfigError",
    "start_insight_run",
    "retry_insight_run",
    "InsightItemStatus",
]
