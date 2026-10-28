"""Read-only checks of a saved input snapshot, without invented reference labels.

Writes evaluation tables only. A passed case means exactly one saved result or
cluster membership exists, NOT that its label or meaning has been verified.
"""

import json
from collections import Counter
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.evals.runner import (
    EvaluationRunnerValidationError,
    _build_pipeline_metrics,
    _coerce_latency_for_eval,
    _coerce_tokens_for_eval,
    _validate_source_run,
)
from app.models import AnalysisRun, Cluster, ClusterMember, Feedback, FeedbackAnalysis
from app.models.evaluation import EvaluationCase, EvaluationMetric, EvaluationRun


def evaluate_run_check(
    db: Session, *, project_id: UUID, source_run_id: UUID, evaluation_type: str
) -> EvaluationRun:
    stage = "classification" if evaluation_type == "classification_metrics" else "clustering"
    source = _validate_source_run(
        db, project_id=project_id, source_run_id=source_run_id, expected_run_type=stage
    )
    snapshot = source.input_feedback_external_ids
    if not snapshot or len(snapshot) != source.input_count or len(set(snapshot)) != len(snapshot):
        raise EvaluationRunnerValidationError(
            "该运行缺少完整输入快照，无法可靠检查；请重新运行分析。"
        )
    feedbacks = db.scalars(
        select(Feedback).where(
            Feedback.project_id == project_id, Feedback.external_id.in_(snapshot)
        )
    ).all()
    if len(feedbacks) != len(snapshot):
        raise EvaluationRunnerValidationError("运行输入中的反馈已缺失，无法生成一致的报告。")
    inputs = {f.id: f for f in feedbacks}
    counts: Counter = Counter()
    output_ids: dict = {}
    if stage == "classification":
        outputs = db.scalars(
            select(FeedbackAnalysis).where(FeedbackAnalysis.run_id == source.id)
        ).all()
        for row in outputs:
            if row.feedback_id not in inputs:
                raise EvaluationRunnerValidationError("分类结果与原运行输入不一致。")
            counts[row.feedback_id] += 1
            output_ids.setdefault(row.feedback_id, []).append(str(row.id))
    else:
        try:
            upstream_id = UUID((source.cluster_config or {})["classification_run_id"])
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise EvaluationRunnerValidationError("聚类运行缺少来源分类快照。") from exc
        upstream = db.get(AnalysisRun, upstream_id)
        if (
            not upstream
            or upstream.project_id != project_id
            or upstream.run_type != "classification"
        ):
            raise EvaluationRunnerValidationError("聚类来源分类记录不属于当前项目。")
        clusters = db.scalars(select(Cluster).where(Cluster.run_id == source.id)).all()
        if any(c.project_id != project_id for c in clusters):
            raise EvaluationRunnerValidationError("聚类结果项目归属不一致。")
        members = db.scalars(
            select(ClusterMember).where(ClusterMember.cluster_id.in_([c.id for c in clusters]))
        ).all()
        analyses = {
            a.id: a
            for a in db.scalars(
                select(FeedbackAnalysis).where(
                    FeedbackAnalysis.id.in_([m.feedback_analysis_id for m in members])
                )
            ).all()
        }
        for member in members:
            row = analyses.get(member.feedback_analysis_id)
            if not row or row.feedback_id not in inputs or row.run_id != upstream_id:
                raise EvaluationRunnerValidationError("聚类成员与来源分类或原运行输入不一致。")
            counts[row.feedback_id] += 1
            output_ids.setdefault(row.feedback_id, []).append(str(member.cluster_id))
    canonical = [
        (f.external_id, sha256(f.content.encode()).hexdigest())
        for f in sorted(feedbacks, key=lambda f: f.external_id)
    ]
    digest = sha256(json.dumps(canonical, ensure_ascii=False).encode()).hexdigest()
    good = sum(counts[f.id] == 1 for f in feedbacks)
    failed = len(feedbacks) - good
    in_tokens, out_tokens = _coerce_tokens_for_eval(source)
    now = utc_now()
    run = EvaluationRun(
        project_id=project_id,
        evaluation_type="pipeline_health",
        stage=stage,
        dataset_name="project_run_snapshot",
        dataset_version=f"snapshot-{digest[:16]}",
        dataset_content_hash=digest,
        dataset_provenance="unverified",
        dataset_split="full",
        dataset_label_status="no_reference_labels",
        model=source.model,
        prompt_version=source.prompt_version,
        calculation_version="run-check-v1",
        status="partial_failed" if failed else "completed",
        input_count=len(feedbacks),
        processed_count=len(feedbacks),
        success_count=good,
        failed_count=failed,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        unknown_token_calls=source.unknown_token_calls,
        latency_ms=_coerce_latency_for_eval(source),
        total_cost_usd=None,
        attempt_count=0,
        retry_count=0,
        started_at=now,
        completed_at=now,
        notes=f"source_run_id={source.id}; source_run_status={source.status}; "
        "result existence only; no semantic judgment; no model calls; "
        "input content hashed at evaluation time; historical content drift not verified",
    )
    try:
        db.add(run)
        db.flush()
        details = {
            "definition": "exactly_one_saved_result_or_membership_including_outliers",
            "source_run_id": str(source.id),
            "semantic_quality": "NOT MEASURED",
        }
        db.add(
            EvaluationMetric(
                run_id=run.id,
                project_id=project_id,
                stage=stage,
                metric_name="result_coverage_rate",
                metric_kind="deterministic",
                sample_count=len(feedbacks),
                value=good / len(feedbacks),
                details=details,
            )
        )
        for m in _build_pipeline_metrics(source_run=source, stage=stage):
            db.add(
                EvaluationMetric(
                    run_id=run.id,
                    project_id=project_id,
                    stage=m.stage,
                    metric_name=m.name,
                    metric_kind=m.kind,
                    sample_count=m.sample_count,
                    value=m.value,
                    details=dict(m.details),
                )
            )
        unmeasured = (
            ("feedback_type_accuracy", "intent_exact_match", "severity_mae")
            if stage == "classification"
            else ("cluster_purity_micro", "cluster_fragmentation_mean")
        )
        for name in unmeasured:
            db.add(
                EvaluationMetric(
                    run_id=run.id,
                    project_id=project_id,
                    stage=stage,
                    metric_name=name,
                    metric_kind="deterministic",
                    sample_count=0,
                    value=None,
                    details={"reason": "no_reference_labels"},
                )
            )
        for f in feedbacks:
            passed = counts[f.id] == 1
            db.add(
                EvaluationCase(
                    run_id=run.id,
                    project_id=project_id,
                    case_id=f.external_id,
                    stage=stage,
                    input_hash=sha256(f.content.encode()).hexdigest(),
                    expected_hash=None,
                    actual_hash=sha256(
                        json.dumps(sorted(output_ids.get(f.id, []))).encode()
                    ).hexdigest(),
                    passed=passed,
                    error_type=None
                    if passed
                    else ("no_prediction" if not counts[f.id] else "validation_error"),
                    eval_source="db_record",
                    notes="structure_only_not_semantic_quality",
                )
            )
        db.commit()
        db.refresh(run)
    except Exception:
        db.rollback()
        raise
    return run
