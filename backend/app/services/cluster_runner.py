"""Phase 2 — Cluster run orchestration.

Synchronous execution mirrors the Phase 1 ``analysis_runner`` pattern: the
request blocks until the run completes. The run object is committed at
start, refreshed as the embedding / clustering / review phases finish, and
finalised before the API returns.

Retries reuse original inputs and embeddings under a session advisory lock.
Completed runs are immutable; the LLM review is not deterministic.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.agents.prompts.cluster_review_v4 import PROMPT_VERSION
from app.db.config import settings
from app.models import (
    AnalysisRun,
    Cluster,
    ClusterMember,
    ClusterReview,
    ClusterSource,
    Feedback,
    FeedbackAnalysis,
    RunStatus,
)
from app.models import (
    Embedding as EmbeddingModel,
)
from app.services.analysis_runner import _execution_lock
from app.services.cluster_review import (
    ClusterReviewClient,
    ClusterReviewConfigError,
    ClusterReviewValidationError,
    LLMProtocolError,
    LLMTransportError,
    ReviewItem,
    ValidatedReview,
    is_cluster_review_configured,
    validate_review_payload,
)
from app.services.clustering import (
    ClusterCandidate,
    VectorPoint,
    agglomerative_cluster,
    cluster_statistics,
)
from app.services.embedding import (
    EmbeddingStats,
    build_embedder,
    ensure_embeddings,
)
from app.services.llm_client import LLMUsage

logger = logging.getLogger(__name__)


class ClusteringConfigError(RuntimeError):
    """Raised when the project has no usable classification input."""


_INTERRUPTED_RUN_STATUSES: frozenset[str] = frozenset(
    {
        RunStatus.RUNNING.value,
        RunStatus.FAILED.value,
        RunStatus.PARTIAL_FAILED.value,
    }
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _latest_successful_classification_run(db: Session, project_id: UUID) -> AnalysisRun | None:
    """Return the most recent successful classification run for the project.

    "Successful" means ``status == completed`` and ``processed_count ==
    input_count`` so partial runs never silently feed the clustering
    pipeline. A project that has never had a successful classification
    returns ``None`` and the caller raises a config error.
    """
    return db.scalar(
        select(AnalysisRun)
        .where(
            AnalysisRun.project_id == project_id,
            AnalysisRun.run_type == "classification",
            AnalysisRun.status == RunStatus.COMPLETED.value,
            AnalysisRun.processed_count == AnalysisRun.input_count,
            AnalysisRun.success_count == AnalysisRun.input_count,
        )
        .order_by(AnalysisRun.completed_at.desc(), AnalysisRun.created_at.desc())
    )


def _load_inputs(
    db: Session, classification_run: AnalysisRun
) -> tuple[list[FeedbackAnalysis], dict[UUID, Feedback]]:
    analyses = db.scalars(
        select(FeedbackAnalysis)
        .where(FeedbackAnalysis.run_id == classification_run.id)
        .order_by(FeedbackAnalysis.created_at, FeedbackAnalysis.id)
    ).all()
    if not analyses:
        return [], {}
    feedback_ids = {a.feedback_id for a in analyses}
    feedbacks = db.scalars(select(Feedback).where(Feedback.id.in_(feedback_ids))).all()
    feedback_by_id = {f.id: f for f in feedbacks}
    if len(analyses) != classification_run.input_count or any(
        a.feedback_id not in feedback_by_id
        or feedback_by_id[a.feedback_id].project_id != classification_run.project_id
        for a in analyses
    ):
        raise ClusteringConfigError("classification input coverage/project mismatch")
    return analyses, feedback_by_id


def start_cluster_run(db: Session, project_id: UUID) -> AnalysisRun:
    """Create + execute + finalise a clustering run synchronously."""
    classification_run = _latest_successful_classification_run(db, project_id)
    if classification_run is None:
        raise ClusteringConfigError(
            "项目还没有成功的 classification 运行；请先完成分类再启动聚类。"
        )

    analyses, feedback_by_id = _load_inputs(db, classification_run)
    if not analyses:
        raise ClusteringConfigError(
            "classification 运行不包含任何 FeedbackAnalysis；无法启动聚类。"
        )

    run = AnalysisRun(
        project_id=project_id,
        run_type="clustering",
        model=settings.embedding_model,
        prompt_version=PROMPT_VERSION,
        cluster_config={
            "classification_run_id": str(classification_run.id),
            "embedding_model": settings.embedding_model,
            "embedding_dimension": settings.embedding_dimension,
            "distance_threshold": settings.cluster_distance_threshold,
            "linkage": settings.cluster_linkage,
            "review_model": settings.llm_model,
            "review_enabled": settings.cluster_review_enabled,
        },
        status=RunStatus.RUNNING.value,
        input_count=len(analyses),
        started_at=_now(),
        # Snapshot the input set so a retry cannot expand it when new
        # classification runs finish while a cluster retry is in flight.
        input_feedback_external_ids=[
            feedback_by_id[a.feedback_id].external_id
            for a in analyses
            if a.feedback_id in feedback_by_id
        ],
    )
    db.add(run)
    db.flush()
    run_id = run.id

    with _execution_lock(db, run_id):
        db.commit()  # durable before any model call.
        try:
            _execute_pipeline(db, run, analyses, feedback_by_id)
        except BaseException as exc:
            db.rollback()
            run = db.get(AnalysisRun, run_id)
            if run is not None:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    run.error_message = f"interrupted: {type(exc).__name__}"
                    run.status = RunStatus.RUNNING.value
                else:
                    run.status = RunStatus.FAILED.value
                    run.processed_count = run.failed_count = run.input_count
                    run.success_count = 0
                    run.error_message = "cluster execution failed: " + type(exc).__name__
                run.completed_at = _now()
                db.commit()
                db.refresh(run)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return run

        run.completed_at = _now()
        run.status = _derive_status(run)
        db.commit()
        db.refresh(run)
        return run


def retry_cluster_run(db: Session, run_id: UUID) -> AnalysisRun:
    """Retry incomplete runs from the snapshot; completed runs are no-ops."""
    with _execution_lock(db, run_id):
        return _retry_cluster_run_locked(db, run_id)


def _retry_cluster_run_locked(db: Session, run_id: UUID) -> AnalysisRun:
    run = db.get(AnalysisRun, run_id)
    if run is None:
        raise LookupError(f"cluster run {run_id} not found")
    if run.run_type != "clustering":
        raise ValueError(f"run {run_id} is not a clustering run (run_type={run.run_type!r})")

    if run.status == RunStatus.COMPLETED.value:
        return run  # Completed audit history is immutable; start a new run to re-evaluate.
    config = run.cluster_config
    if not config or not config.get("classification_run_id"):
        raise ValueError("legacy run lacks classification snapshot; start a new clustering run")
    current = {
        "embedding_model": settings.embedding_model,
        "embedding_dimension": settings.embedding_dimension,
        "distance_threshold": settings.cluster_distance_threshold,
        "linkage": settings.cluster_linkage,
        "review_model": settings.llm_model,
        "review_enabled": settings.cluster_review_enabled,
    }
    if any(config.get(k) != v for k, v in current.items()):
        raise ValueError(
            "clustering configuration changed; restore original configuration or start a new run"
        )

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
        raise ValueError(f"run {run_id} status {run.status!r} is not eligible for retry")

    if run.prompt_version != PROMPT_VERSION:
        db.rollback()
        raise ValueError(
            f"run {run_id} was created with prompt_version {run.prompt_version!r}; "
            f"current PROMPT_VERSION={PROMPT_VERSION!r} does not match"
        )

    db.refresh(run)
    classification_run = db.get(AnalysisRun, UUID(config["classification_run_id"]))
    if classification_run is None or classification_run.project_id != run.project_id:
        db.rollback()
        raise ClusteringConfigError("项目已没有成功的 classification 运行；无法重试聚类。")
    analyses, feedback_by_id = _load_inputs(db, classification_run)

    # Wipe any partial output from the previous attempt so the retry
    # rebuilds the cluster graph from scratch. ClusterMember rows are
    # removed by ON DELETE CASCADE when we delete their parent clusters.
    db.execute(delete(ClusterReview).where(ClusterReview.run_id == run_id))
    db.execute(
        delete(ClusterMember).where(
            ClusterMember.cluster_id.in_(select(Cluster.id).where(Cluster.run_id == run_id))
        )
    )
    db.execute(update(Cluster).where(Cluster.run_id == run_id).values(parent_cluster_id=None))
    db.execute(delete(Cluster).where(Cluster.run_id == run_id))
    run.processed_count = run.success_count = run.failed_count = 0
    run.completed_at = None
    run.retry_count += 1
    db.commit()

    # If the original snapshot is still present, honour it so the retry
    # input is identical to the original run even if a fresh classification
    # run has produced more analyses since.
    snapshot = list(run.input_feedback_external_ids or [])
    if snapshot and feedback_by_id:
        snapshot_set = set(snapshot)
        analyses = [
            a
            for a in analyses
            if feedback_by_id.get(a.feedback_id) is not None
            and feedback_by_id[a.feedback_id].external_id in snapshot_set
        ]

    if not analyses:
        # Nothing to do — still need to finalise the run so the caller
        # observes a consistent state instead of an orphaned row.
        run.status = RunStatus.COMPLETED.value
        run.completed_at = _now()
        db.commit()
        db.refresh(run)
        return run

    run.input_count = len(analyses)
    try:
        _execute_pipeline(db, run, analyses, feedback_by_id)
    except BaseException as exc:
        db.rollback()
        run = db.get(AnalysisRun, run_id)
        if run is not None:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                run.error_message = f"interrupted: {type(exc).__name__}"
                run.status = RunStatus.RUNNING.value
            else:
                run.status = RunStatus.FAILED.value
                run.processed_count = run.failed_count = run.input_count
                run.success_count = 0
                run.error_message = "cluster retry failed"
            run.completed_at = _now()
            db.commit()
            db.refresh(run)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return run

    run.completed_at = _now()
    run.status = _derive_status(run)
    db.commit()
    db.refresh(run)
    return run


def _execute_pipeline(
    db: Session,
    run: AnalysisRun,
    analyses: list[FeedbackAnalysis],
    feedback_by_id: dict[UUID, Feedback],
) -> None:
    """Run embedding, clustering and (optionally) LLM review for one run."""
    # 1. Ensure embeddings (idempotent).
    items = [
        (analysis, feedback_by_id[analysis.feedback_id])
        for analysis in analyses
        if analysis.feedback_id in feedback_by_id
    ]
    embedder = build_embedder()
    embed_stats = EmbeddingStats()
    try:
        embeddings = ensure_embeddings(db, run, items, embedder=embedder, stats=embed_stats)
    finally:
        if hasattr(embedder, "close"):
            embedder.close()
        db.rollback()
        run.cluster_config = {**run.cluster_config, **embed_stats.as_dict()}
        db.commit()
    if len(embeddings) != len(analyses):
        raise ClusteringConfigError("incomplete embedding coverage")

    # 2. Run algorithmic clustering on the resulting vectors.
    emb_by_analysis: dict[UUID, EmbeddingModel] = {e.feedback_analysis_id: e for e in embeddings}
    points: list[VectorPoint] = []
    for analysis in analyses:
        emb = emb_by_analysis.get(analysis.id)
        if emb is None:
            continue
        points.append(
            VectorPoint(
                analysis_id=str(analysis.id),
                vector=tuple(float(x) for x in emb.embedding),
            )
        )
    candidates = agglomerative_cluster(
        points,
        distance_threshold=settings.cluster_distance_threshold,
        linkage=settings.cluster_linkage,
    )

    # 3. Persist algorithmic clusters. Each cluster gets its row + members +
    # aggregated statistics. We commit before the LLM review so a reviewer
    # failure cannot erase the algorithmic topology.
    alg_clusters: list[dict] = []
    for cand in candidates:
        cluster = _create_algorithmic_cluster(db, run, cand, analyses, feedback_by_id)
        alg_clusters.append(
            {
                "cluster_id": str(cluster.id),
                "cluster_row": cluster,
                "candidate": cand,
            }
        )
    db.commit()

    run.processed_count = len(analyses)
    run.success_count = len(analyses)
    run.cluster_config = {**run.cluster_config, "candidate_cluster_count": len(alg_clusters)}

    # 4. LLM semantic review (optional). Failure here is logged but does
    # not erase the algorithmic clusters; the run is marked
    # partial_failed so the API can surface the issue.
    if settings.cluster_review_enabled and is_cluster_review_configured() and alg_clusters:
        _apply_llm_review(db, run, alg_clusters, analyses, feedback_by_id)
    else:
        if alg_clusters and settings.cluster_review_enabled and not is_cluster_review_configured():
            _record_review_failure(db, run, "review configuration unavailable")
            logger.info(
                "cluster_review skipped: LLM_API_KEY / LLM_BASE_URL / LLM_MODEL "
                "未配置；algorithmic clusters persisted without review."
            )


def _create_algorithmic_cluster(
    db: Session,
    run: AnalysisRun,
    candidate: ClusterCandidate,
    analyses: list[FeedbackAnalysis],
    feedback_by_id: dict[UUID, Feedback],
) -> Cluster:
    cluster = Cluster(
        project_id=run.project_id,
        run_id=run.id,
        source=ClusterSource.ALGORITHMIC.value,
        embedding_model=settings.embedding_model,
        distance_threshold=settings.cluster_distance_threshold,
        linkage=settings.cluster_linkage,
    )
    db.add(cluster)
    db.flush()

    analysis_by_id: dict[UUID, FeedbackAnalysis] = {a.id: a for a in analyses}
    severities: dict[str, int] = {}
    sentiments: dict[str, str] = {}
    user_ids: dict[str, str | None] = {}
    for analysis in analyses:
        key = str(analysis.id)
        severities[key] = analysis.severity
        sentiments[key] = analysis.sentiment
        feedback = feedback_by_id.get(analysis.feedback_id)
        user_ids[key] = feedback.user_id if feedback is not None else None

    for member_id in candidate.members:
        try:
            member_uuid = UUID(member_id)
        except (TypeError, ValueError):
            continue
        similarity = float(candidate.similarities.get(member_id, 0.0))
        db.add(
            ClusterMember(
                cluster_id=cluster.id,
                feedback_analysis_id=member_uuid,
                similarity_score=similarity,
            )
        )
        analysis_by_id.pop(member_uuid, None)

    stats = cluster_statistics(candidate, severities, sentiments, user_ids)
    cluster.feedback_count = stats["feedback_count"]
    cluster.unique_user_count = stats["unique_user_count"]
    cluster.average_severity = stats["average_severity"]
    cluster.negative_ratio = stats["negative_ratio"]
    return cluster


def _apply_llm_review(
    db: Session,
    run: AnalysisRun,
    alg_clusters: list[dict],
    analyses: list[FeedbackAnalysis],
    feedback_by_id: dict[UUID, Feedback],
) -> None:
    """Call the LLM reviewer once, validate the payload, and apply actions.

    A reviewer failure leaves the algorithmic clusters untouched and only
    updates the run counters / status. A validation failure is treated the
    same way: a structured payload that does not match the candidate set is
    not persisted.
    """
    analysis_by_id: dict[UUID, FeedbackAnalysis] = {a.id: a for a in analyses}
    feedback_for_analysis: dict[UUID, Feedback | None] = {
        a.id: feedback_by_id.get(a.feedback_id) for a in analyses
    }
    items = [
        ReviewItem(
            cluster_id=row["cluster_id"],
            name_hint=None,
            members=[
                {
                    "feedback_id": member_id,
                    "content": (
                        feedback_for_analysis[UUID(member_id)].content
                        if UUID(member_id) in feedback_for_analysis
                        and feedback_for_analysis[UUID(member_id)] is not None
                        else ""
                    ),
                    "module": (
                        analysis_by_id[UUID(member_id)].module
                        if UUID(member_id) in analysis_by_id
                        else ""
                    ),
                    "intent": (
                        analysis_by_id[UUID(member_id)].intent
                        if UUID(member_id) in analysis_by_id
                        else ""
                    ),
                    "pain_point": (
                        analysis_by_id[UUID(member_id)].pain_point
                        if UUID(member_id) in analysis_by_id
                        else ""
                    ),
                }
                for member_id in row["candidate"].members
            ],
        )
        for row in alg_clusters
    ]

    review_attempts = 0
    last_error: str | None = None
    validated: list[ValidatedReview] | None = None

    try:
        client = ClusterReviewClient()
    except ClusterReviewConfigError as exc:
        logger.warning("cluster_review unavailable: %s", exc)
        _record_review_failure(db, run, str(exc))
        return

    try:
        with client:
            max_retries = settings.cluster_review_max_retries
            attempt_index = 0
            while True:
                started = time.monotonic()
                try:
                    result = client.review_clusters(items)
                    latency = int((time.monotonic() - started) * 1000)
                    run.latency_ms += max(latency, 0)
                    run.attempt_count += 1
                    review_attempts += 1
                    run.input_tokens += result.usage.input_tokens or 0
                    run.output_tokens += result.usage.output_tokens or 0
                    if result.usage.input_tokens is None or result.usage.output_tokens is None:
                        run.unknown_token_calls += 1
                    db.commit()
                    try:
                        validated = validate_review_payload(result.payload, items)
                    except ClusterReviewValidationError as exc:
                        last_error = "review payload invalid: " + exc.code
                        client.validation_feedback = exc.code
                        if attempt_index >= max_retries:
                            break
                        attempt_index += 1
                        run.retry_count += 1
                        time.sleep(0.2 * (2 ** (attempt_index - 1)))
                        continue
                    break
                except (LLMTransportError, LLMProtocolError) as exc:
                    latency = int((time.monotonic() - started) * 1000)
                    run.latency_ms += max(latency, 0)
                    run.attempt_count += 1
                    review_attempts += 1
                    run.unknown_token_calls += 1
                    db.commit()
                    last_error = type(exc).__name__
                    if attempt_index >= max_retries:
                        break
                    attempt_index += 1
                    run.retry_count += 1
                    time.sleep(0.2 * (2 ** (attempt_index - 1)))
                    continue
    except BaseException as exc:
        last_error = last_error or type(exc).__name__
        logger.warning("cluster_review crashed for run_id=%s: %s", run.id, last_error)
        _record_review_failure(db, run, last_error)
        return

    if validated is None:
        logger.warning(
            "cluster_review failed for run_id=%s after %d attempts: %s",
            run.id,
            review_attempts,
            last_error or "unknown error",
        )
        _record_review_failure(db, run, last_error or "cluster review failed")
        return

    db.commit()  # durability before we mutate the cluster graph.
    _apply_validated_reviews(
        db, run, alg_clusters, validated, analysis_by_id, feedback_for_analysis
    )


def _accumulate_usage(current: LLMUsage, last_known: bool, new: LLMUsage) -> tuple[LLMUsage, bool]:
    if new.input_tokens is None or new.output_tokens is None:
        return current, False
    next_in = (current.input_tokens or 0) + new.input_tokens
    next_out = (current.output_tokens or 0) + new.output_tokens
    return LLMUsage(input_tokens=next_in, output_tokens=next_out), last_known


def _accumulate_unknown(current: LLMUsage, last_known: bool) -> bool:
    return False


def _record_review_failure(db: Session, run: AnalysisRun, error_message: str) -> None:
    run.error_message = _truncate("cluster review failed: " + error_message)
    db.commit()


def _truncate(message: str, *, limit: int = 4000) -> str:
    text = " ".join((message or "").split())
    return text[:limit]


def _apply_validated_reviews(
    db: Session,
    run: AnalysisRun,
    alg_clusters: list[dict],
    validated: list[ValidatedReview],
    analysis_by_id: dict[UUID, FeedbackAnalysis],
    feedback_for_analysis: dict[UUID, Feedback | None],
) -> None:
    """Apply each validated review decision against the algorithmic clusters.

    Merge sources first: their members are transferred to the merge target's
    effective member set so the target's own action (keep/split/outlier)
    runs against the augmented input. Then every non-merge review is applied.
    """
    review_by_cluster: dict[str, ValidatedReview] = {r.cluster_id: r for r in validated}
    row_by_cluster: dict[str, dict] = {row["cluster_id"]: row for row in alg_clusters}

    # Track extra members arriving at a cluster via a merge action.
    extras: dict[str, list[str]] = {row["cluster_id"]: [] for row in alg_clusters}
    merge_sources: list[str] = []
    for r in validated:
        if r.action == "merge" and r.merge_with_cluster_id:
            merge_sources.append(r.cluster_id)
            extras.setdefault(r.merge_with_cluster_id, []).extend(
                row_by_cluster[r.cluster_id]["candidate"].members
            )

    # Apply merge actions: drain the source's ClusterMember rows into the
    # target's effective member set. We touch DB rows so subsequent keep /
    # split / outlier passes see the augmented membership.
    for source_id in merge_sources:
        source_row = row_by_cluster[source_id]
        target_id = review_by_cluster[source_id].merge_with_cluster_id
        target_row = row_by_cluster[target_id]
        target_candidate = target_row["candidate"]
        for member_id in source_row["candidate"].members:
            target_candidate.members.append(member_id)
            target_candidate.similarities[member_id] = source_row["candidate"].similarities.get(
                member_id, 0.0
            )
            db.add(
                ClusterMember(
                    cluster_id=target_row["cluster_row"].id,
                    feedback_analysis_id=UUID(member_id),
                    similarity_score=0.0,
                )
            )
        # Remove the source's ClusterMember rows so the source is empty.
        db.execute(
            delete(ClusterMember).where(ClusterMember.cluster_id == source_row["cluster_row"].id)
        )
        source_row["cluster_row"].feedback_count = 0
        source_row["cluster_row"].unique_user_count = 0
        source_row["cluster_row"].average_severity = None
        source_row["cluster_row"].negative_ratio = None

    # Apply non-merge actions in candidate order so output ordering is
    # reproducible.
    for r in validated:
        if r.action == "merge":
            continue
        row = row_by_cluster[r.cluster_id]
        if r.action == "keep":
            _apply_keep(db, run, row, r, analysis_by_id, feedback_for_analysis)
        elif r.action == "split":
            _apply_split(db, run, row, r, extras, analysis_by_id, feedback_for_analysis)
        elif r.action == "outlier":
            _apply_outlier(db, run, row, r, extras, analysis_by_id, feedback_for_analysis)
        else:
            # Unreachable: validator already rejected unknown actions.
            continue

    # Persist one ClusterReview audit row per algorithmic cluster so the
    # review chain is reproducible.
    for r in validated:
        row = row_by_cluster[r.cluster_id]
        db.add(
            ClusterReview(
                run_id=run.id,
                cluster_id=row["cluster_row"].id,
                action=r.action,
                reason=r.reason,
                prompt_version=PROMPT_VERSION,
                response_json=r.raw,
            )
        )

    db.flush()
    expected = set(analysis_by_id)
    actual = list(
        db.scalars(
            select(ClusterMember.feedback_analysis_id)
            .join(Cluster, Cluster.id == ClusterMember.cluster_id)
            .where(Cluster.run_id == run.id)
        )
    )
    if len(actual) != len(expected) or set(actual) != expected:
        raise ClusterReviewValidationError("final cluster membership is not a partition")
    db.commit()


def _apply_keep(
    db: Session,
    run: AnalysisRun,
    row: dict,
    review: ValidatedReview,
    analysis_by_id: dict[UUID, FeedbackAnalysis],
    feedback_for_analysis: dict[UUID, Feedback | None],
) -> None:
    cluster = row["cluster_row"]
    cluster.name = review.name
    cluster.summary = review.summary
    cluster.confidence = review.confidence
    cluster.source = ClusterSource.LLM_REVIEW.value
    _refresh_cluster_stats(
        cluster,
        row["candidate"],
        analysis_by_id,
        feedback_for_analysis,
    )


def _apply_split(
    db: Session,
    run: AnalysisRun,
    row: dict,
    review: ValidatedReview,
    extras: dict[str, list[str]],
    analysis_by_id: dict[UUID, FeedbackAnalysis],
    feedback_for_analysis: dict[UUID, Feedback | None],
) -> None:
    cluster = row["cluster_row"]
    member_set = set(row["candidate"].members)
    extra_for_target = list(extras.get(review.cluster_id, []))

    # Distribute members into the LLM-defined groups. The LLM only saw
    # the cluster's original members when it returned feedback_ids, so the
    # extras arriving via merge have to land in the first group. This is
    # deterministic and easy to reason about.
    group_members: list[list[str]] = []
    for group in review.groups:
        ids = [fid for fid in group["feedback_ids"] if fid in member_set]
        member_set.difference_update(ids)
        group_members.append(ids)
    if extra_for_target and group_members:
        group_members[0] = list(group_members[0]) + list(extra_for_target)
    elif extra_for_target:
        group_members.append(list(extra_for_target))

    db.execute(delete(ClusterMember).where(ClusterMember.cluster_id == cluster.id))
    cluster.feedback_count = 0
    cluster.unique_user_count = 0
    cluster.average_severity = None
    cluster.negative_ratio = None

    for group, members in zip(review.groups, group_members):
        stats = _stats_for_member_ids(members, analysis_by_id, feedback_for_analysis)
        new_cluster = Cluster(
            project_id=cluster.project_id,
            run_id=cluster.run_id,
            source=ClusterSource.LLM_REVIEW.value,
            parent_cluster_id=cluster.id,
            name=group["name"],
            confidence=review.confidence,
            feedback_count=stats["feedback_count"],
            unique_user_count=stats["unique_user_count"],
            average_severity=stats["average_severity"],
            negative_ratio=stats["negative_ratio"],
            embedding_model=cluster.embedding_model,
            distance_threshold=cluster.distance_threshold,
            linkage=cluster.linkage,
        )
        db.add(new_cluster)
        db.flush()
        for member_id in members:
            try:
                member_uuid = UUID(member_id)
            except (TypeError, ValueError):
                continue
            db.add(
                ClusterMember(
                    cluster_id=new_cluster.id,
                    feedback_analysis_id=member_uuid,
                    similarity_score=float(row["candidate"].similarities.get(member_id, 0.0)),
                )
            )

    cluster.summary = review.summary
    # Keep the original algorithmic name on the parent so the audit chain
    # is still legible even after the cluster has been split.
    cluster.confidence = review.confidence


def _apply_outlier(
    db: Session,
    run: AnalysisRun,
    row: dict,
    review: ValidatedReview,
    extras: dict[str, list[str]],
    analysis_by_id: dict[UUID, FeedbackAnalysis],
    feedback_for_analysis: dict[UUID, Feedback | None],
) -> None:
    cluster = row["cluster_row"]
    outlier_set = set(review.outlier_ids)
    remaining = [m for m in row["candidate"].members if m not in outlier_set]
    extra_for_target = list(extras.get(review.cluster_id, []))
    outlier_members = list(outlier_set) + [m for m in extra_for_target if m in outlier_set]
    # Any merge extras that are not in the explicit outlier_ids stay in
    # the main cluster. Extras that happen to coincide with outlier_ids
    # are absorbed into the new outlier cluster.
    remaining.extend(m for m in extra_for_target if m not in outlier_set)

    db.execute(delete(ClusterMember).where(ClusterMember.cluster_id == cluster.id))
    stats = _stats_for_member_ids(remaining, analysis_by_id, feedback_for_analysis)
    cluster.name = review.name
    cluster.summary = review.summary
    cluster.confidence = review.confidence
    cluster.feedback_count = stats["feedback_count"]
    cluster.unique_user_count = stats["unique_user_count"]
    cluster.average_severity = stats["average_severity"]
    cluster.negative_ratio = stats["negative_ratio"]
    cluster.source = ClusterSource.LLM_REVIEW.value
    for member_id in remaining:
        try:
            member_uuid = UUID(member_id)
        except (TypeError, ValueError):
            continue
        db.add(
            ClusterMember(
                cluster_id=cluster.id,
                feedback_analysis_id=member_uuid,
                similarity_score=float(row["candidate"].similarities.get(member_id, 0.0)),
            )
        )

    if outlier_members:
        stats_out = _stats_for_member_ids(outlier_members, analysis_by_id, feedback_for_analysis)
        outlier_cluster = Cluster(
            project_id=cluster.project_id,
            run_id=cluster.run_id,
            source=ClusterSource.OUTLIER.value,
            parent_cluster_id=cluster.id,
            name=(review.name + "__outlier") if review.name else None,
            summary=review.summary,
            confidence=review.confidence,
            feedback_count=stats_out["feedback_count"],
            unique_user_count=stats_out["unique_user_count"],
            average_severity=stats_out["average_severity"],
            negative_ratio=stats_out["negative_ratio"],
            embedding_model=cluster.embedding_model,
            distance_threshold=cluster.distance_threshold,
            linkage=cluster.linkage,
        )
        db.add(outlier_cluster)
        db.flush()
        for member_id in outlier_members:
            try:
                member_uuid = UUID(member_id)
            except (TypeError, ValueError):
                continue
            db.add(
                ClusterMember(
                    cluster_id=outlier_cluster.id,
                    feedback_analysis_id=member_uuid,
                    similarity_score=float(row["candidate"].similarities.get(member_id, 0.0)),
                )
            )


def _refresh_cluster_stats(
    cluster: Cluster,
    candidate: ClusterCandidate,
    analysis_by_id: dict[UUID, FeedbackAnalysis],
    feedback_for_analysis: dict[UUID, Feedback | None],
) -> None:
    stats = cluster_statistics(
        candidate,
        _severities_for(candidate.members, analysis_by_id),
        _sentiments_for(candidate.members, analysis_by_id),
        _user_ids_for(candidate.members, analysis_by_id, feedback_for_analysis),
    )
    cluster.feedback_count = stats["feedback_count"]
    cluster.unique_user_count = stats["unique_user_count"]
    cluster.average_severity = stats["average_severity"]
    cluster.negative_ratio = stats["negative_ratio"]


def _stats_for_member_ids(
    member_ids: list[str],
    analysis_by_id: dict[UUID, FeedbackAnalysis],
    feedback_for_analysis: dict[UUID, Feedback | None],
) -> dict:
    cand = ClusterCandidate(members=list(member_ids))
    return cluster_statistics(
        cand,
        _severities_for(member_ids, analysis_by_id),
        _sentiments_for(member_ids, analysis_by_id),
        _user_ids_for(member_ids, analysis_by_id, feedback_for_analysis),
    )


def _severities_for(
    member_ids: list[str],
    analysis_by_id: dict[UUID, FeedbackAnalysis],
) -> dict[str, int]:
    out: dict[str, int] = {}
    for member_id in member_ids:
        try:
            uuid = UUID(member_id)
        except (TypeError, ValueError):
            continue
        analysis = analysis_by_id.get(uuid)
        if analysis is not None:
            out[member_id] = analysis.severity
    return out


def _sentiments_for(
    member_ids: list[str],
    analysis_by_id: dict[UUID, FeedbackAnalysis],
) -> dict[str, str]:
    out: dict[str, str] = {}
    for member_id in member_ids:
        try:
            uuid = UUID(member_id)
        except (TypeError, ValueError):
            continue
        analysis = analysis_by_id.get(uuid)
        if analysis is not None:
            out[member_id] = analysis.sentiment
    return out


def _user_ids_for(
    member_ids: list[str],
    analysis_by_id: dict[UUID, FeedbackAnalysis],
    feedback_for_analysis: dict[UUID, Feedback | None],
) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for member_id in member_ids:
        try:
            uuid = UUID(member_id)
        except (TypeError, ValueError):
            continue
        feedback = feedback_for_analysis.get(uuid)
        out[member_id] = feedback.user_id if feedback is not None else None
    return out


def _derive_status(run: AnalysisRun) -> str:
    """Cluster runs are either ``completed`` or ``partial_failed``.

    ``failed`` would imply the algorithmic clusters were never persisted;
    that is reserved for the upstream pipeline crashing before any
    cluster rows were written. If the LLM review could not be applied we
    still surface the partial-failure so the API caller can decide.
    """
    if run.input_count == 0:
        return RunStatus.COMPLETED.value
    if run.processed_count < run.input_count:
        return RunStatus.PARTIAL_FAILED.value
    if run.error_message:
        return RunStatus.PARTIAL_FAILED.value
    return RunStatus.COMPLETED.value


def _truncate_reason(reason: str, *, limit: int = 4000) -> str:
    return " ".join((reason or "").split())[:limit]


__all__ = [
    "ClusteringConfigError",
    "start_cluster_run",
    "retry_cluster_run",
]
