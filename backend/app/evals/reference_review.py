"""Human-supplied reference snapshots. No model calls or writes to source data."""

import csv
import io
import json
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.evals.dataset import DatasetCase, DatasetProvenance
from app.evals.metrics import ClusterPrediction, MetricResult, compute_cluster_metrics
from app.evals.runner import (
    _build_classification_predictions,
    _build_cluster_predictions,
    _coerce_metric_for_db,
    _evaluate_classification,
    _evaluate_cluster,
)
from app.models import AnalysisRun, Cluster, ClusterMember, Feedback, FeedbackAnalysis
from app.models.evaluation import EvaluationMetric

LABEL_STATUS = "user_reviewed_reference_not_formal_acceptance"
MAX_BYTES = 2 * 1024 * 1024
FIELDS = (
    "project_id",
    "source_run_id",
    "feedback_id",
    "content_hash",
    "content",
    "review_status",
    "reviewer",
    "feedback_type",
    "severity",
    "cluster_label",
)
TYPES = {
    "bug",
    "feature_request",
    "ux_issue",
    "performance_issue",
    "pricing_issue",
    "content_issue",
    "positive_feedback",
    "other",
}


class ReviewError(ValueError):
    def __init__(self, message, status=409):
        self.status = status
        super().__init__(message)


def digest(value):
    return sha256(value.encode("utf-8")).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def csv_safe(value):
    """Reversible display escaping, including existing apostrophes and whitespace."""
    value = str(value)
    if (
        value.startswith("'")
        or value.lstrip().startswith(("=", "+", "-", "@"))
        or value.startswith(("\t", "\r", "\n"))
    ):
        return "'" + value
    return value


def load_source(db, project_id, source_run_id, evaluation_type):
    stage = {"classification_metrics": "classification", "cluster_metrics": "clustering"}.get(
        evaluation_type
    )
    if stage is None:
        raise ReviewError("不支持的评测阶段。", 422)
    source = db.get(AnalysisRun, source_run_id)
    if source is None or source.project_id != project_id:
        raise ReviewError("分析记录不存在。", 404)
    if source.run_type != stage or source.status not in {"completed", "partial_failed", "failed"}:
        raise ReviewError("请选择对应阶段已结束的运行。")
    snapshot = source.input_feedback_external_ids
    if not snapshot or len(snapshot) != source.input_count or len(set(snapshot)) != len(snapshot):
        raise ReviewError("运行缺少完整输入快照，无法绑定人工参考答案。")
    if len(snapshot) > 10000:
        raise ReviewError("本次运行超过10000条，请使用较小的输入批次。", 422)
    feedbacks = db.scalars(
        select(Feedback).where(
            Feedback.project_id == project_id, Feedback.external_id.in_(snapshot)
        )
    ).all()
    if len(feedbacks) != len(snapshot):
        raise ReviewError("运行输入已缺失，无法形成可靠的参考快照。")
    by_id = {f.id: f for f in feedbacks}
    # Validate output provenance before evaluating. Missing predictions remain measured failures.
    if stage == "classification":
        outputs = db.scalars(
            select(FeedbackAnalysis).where(FeedbackAnalysis.run_id == source.id)
        ).all()
        if any(a.feedback_id not in by_id for a in outputs):
            raise ReviewError("分类结果超出运行输入范围。")
    else:
        try:
            upstream_id = UUID((source.cluster_config or {})["classification_run_id"])
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise ReviewError("聚类缺少来源分类快照。") from exc
        upstream = db.get(AnalysisRun, upstream_id)
        if (
            not upstream
            or upstream.project_id != project_id
            or upstream.run_type != "classification"
        ):
            raise ReviewError("聚类来源分类归属不一致。")
        clusters = db.scalars(select(Cluster).where(Cluster.run_id == source.id)).all()
        if any(c.project_id != project_id for c in clusters):
            raise ReviewError("聚类项目归属不一致。")
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
        for m in members:
            a = analyses.get(m.feedback_analysis_id)
            if not a or a.run_id != upstream_id or a.feedback_id not in by_id:
                raise ReviewError("聚类成员超出来源分类或运行输入范围。")
    return source, {f.external_id: f for f in feedbacks}


def make_template(db, *, project_id, source_run_id, evaluation_type):
    source, feedbacks = load_source(db, project_id, source_run_id, evaluation_type)
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=FIELDS)
    writer.writeheader()
    for eid, f in sorted(feedbacks.items()):
        writer.writerow(
            {
                "project_id": str(project_id),
                "source_run_id": str(source.id),
                "feedback_id": csv_safe(eid),
                "content_hash": digest(f.content),
                "content": csv_safe(f.content),
                "review_status": "pending",
            }
        )
    text = out.getvalue()
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ReviewError("模板超过2MiB，请先使用较小的输入批次。", 413)
    return {
        "project_id": str(project_id),
        "source_run_id": str(source.id),
        "input_count": source.input_count,
        "csv_text": text,
        "filename": f"reference-{source.id}.csv",
    }


def parse_labels(text, *, project_id, source, feedbacks):
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ReviewError("CSV最大为2MiB。", 413)
    if "\x00" in text:
        raise ReviewError("CSV不能包含空字符。", 422)
    reader = csv.DictReader(io.StringIO(text.removeprefix("\ufeff"), newline=""), strict=True)
    rows = {}
    safe_ids = {csv_safe(eid): eid for eid in feedbacks}
    try:
        if (
            reader.fieldnames is None
            or len(reader.fieldnames) != len(FIELDS)
            or set(reader.fieldnames) != set(FIELDS)
        ):
            raise ReviewError("CSV列必须与下载模板一致。", 422)
        for row in reader:
            if len(rows) >= 10000 or None in row or any(v is None for v in row.values()):
                raise ReviewError("CSV行数或列数不合法。", 422)
            line = reader.line_num
            if row["project_id"] != str(project_id) or row["source_run_id"] != str(source.id):
                raise ReviewError(f"第{line}行不属于当前项目或所选运行。")
            eid = safe_ids.get(row["feedback_id"])
            if eid is None or eid in rows:
                raise ReviewError(f"第{line}行反馈ID未知、重复或不在原运行输入中。")
            f = feedbacks[eid]
            if row["content_hash"] != digest(f.content) or row["content"] != csv_safe(f.content):
                raise ReviewError(f"第{line}行原文或指纹已改变，请重新下载并复核。")
            reviewer = row["reviewer"].strip()
            if row["review_status"] != "reviewed" or not reviewer or len(reviewer) > 100:
                raise ReviewError(f"第{line}行需填写审核人并将review_status设为reviewed。", 422)
            if source.run_type == "classification":
                if (
                    row["feedback_type"] not in TYPES
                    or row["severity"] not in {"1", "2", "3", "4", "5"}
                    or row["cluster_label"]
                ):
                    raise ReviewError(
                        f"第{line}行分类标签或严重度不合法，cluster_label须留空。", 422
                    )
            elif (
                not row["cluster_label"].strip()
                or len(row["cluster_label"]) > 200
                or row["feedback_type"]
                or row["severity"]
            ):
                raise ReviewError(f"第{line}行需填写cluster_label，分类与严重度列须留空。", 422)
            rows[eid] = {
                "reviewer": reviewer,
                "feedback_type": row["feedback_type"] or None,
                "severity": int(row["severity"]) if row["severity"] else None,
                "cluster_label": row["cluster_label"].strip() or None,
                "content_hash": row["content_hash"],
            }
    except csv.Error as exc:
        raise ReviewError("CSV格式错误，请使用UTF-8 CSV模板。", 422) from exc
    if not rows:
        raise ReviewError("CSV没有待评测样本。", 422)
    return dict(sorted(rows.items()))


def _review_cluster_metrics(build, *, labelled_ids, golden):
    """按“仅已复核成员”重算聚类语义指标。

    未复核成员不参与纯度/过合并等语义指标的计算，因此未复核簇既不进入
    过合并率的分母，也不稀释比值；过滤后为空的簇直接丢弃。

    ``cluster_count`` 仍报告原 Run 的全量簇数，其口径在 details 中写明；
    其余指标的 ``sample_count`` 保持“已标注成员数 / 含标签簇数”，无可测量
    样本时为 0 且值为 NULL（不是假 0）。pipeline 指标沿用原 Run，不在此重算。
    """
    filtered: list[ClusterPrediction] = []
    for prediction in build.predictions:
        members = tuple(m for m in prediction.members if m in labelled_ids)
        if not members:
            continue
        filtered.append(ClusterPrediction(cluster_id=prediction.cluster_id, members=members))
    total_clusters = len(build.predictions)
    labelled_clusters = len(filtered)

    metrics: list[MetricResult] = []
    for metric in compute_cluster_metrics(filtered, golden):
        details = dict(metric.details)
        details["labelled_predicted_clusters"] = labelled_clusters
        details["total_predicted_clusters"] = total_clusters
        if metric.name == "cluster_count":
            details["predicted_clusters"] = total_clusters
            details["scope"] = "all_source_run_clusters"
            metrics.append(
                MetricResult(
                    name=metric.name,
                    kind=metric.kind,
                    stage=metric.stage,
                    sample_count=total_clusters,
                    value=float(total_clusters) if total_clusters else None,
                    details=details,
                )
            )
            continue
        metrics.append(
            _coerce_metric_for_db(
                MetricResult(
                    name=metric.name,
                    kind=metric.kind,
                    stage=metric.stage,
                    sample_count=metric.sample_count,
                    value=metric.value,
                    details=details,
                )
            )
        )
    return metrics


def evaluate_review(db: Session, *, project_id, source_run_id, evaluation_type, csv_text):
    source, feedbacks = load_source(db, project_id, source_run_id, evaluation_type)
    labels = parse_labels(csv_text, project_id=project_id, source=source, feedbacks=feedbacks)
    fingerprint = digest(
        canonical(
            {
                "project_id": str(project_id),
                "source_run_id": str(source.id),
                "stage": source.run_type,
                "labels": labels,
            }
        )
    )
    cases = tuple(
        DatasetCase(
            case_id=eid,
            feedback_text=feedbacks[eid].content,
            stage=source.run_type,
            feedback_type_label=r["feedback_type"],
            severity_label=r["severity"],
            cluster_label=r["cluster_label"],
        )
        for eid, r in labels.items()
    )
    dataset = DatasetProvenance(
        name="user_reference_review",
        version=f"review-{fingerprint[:16]}",
        content_hash=fingerprint,
        provenance="unverified",
        split="full" if len(cases) == source.input_count else "mixed",
        label_status=LABEL_STATUS,
        cases=cases,
        cases_by_id={c.case_id: c for c in cases},
        source_paths=(),
    )
    now = utc_now()
    try:
        args = dict(
            db=db, project_id=project_id, source_run=source, dataset=dataset, started_at=now
        )
        if source.run_type == "classification":
            run, metrics, records = _evaluate_classification(**args, intent_vocabulary=())
            predictions = _build_classification_predictions(
                db, project_id=project_id, source_run=source
            ).predictions
            actuals = {
                eid: {"feedback_type": p.feedback_type, "severity": p.severity}
                for eid, p in predictions.items()
            }
        else:
            run, metrics, records = _evaluate_cluster(**args)
            build = _build_cluster_predictions(db, project_id=project_id, source_run=source)
            actuals = {}
            for cluster in build.predictions:
                for eid in cluster.members:
                    actuals.setdefault(eid, {"cluster_ids": []})["cluster_ids"].append(
                        cluster.cluster_id
                    )
            golden = {eid: r["cluster_label"] for eid, r in labels.items() if r["cluster_label"]}
            metrics = _review_cluster_metrics(build, labelled_ids=set(golden), golden=golden) + [
                m for m in metrics if m.stage != "clustering"
            ]
        run.status = "partial_failed" if run.failed_count else "completed"
        run.completed_at = utc_now()
        run.calculation_version = "reference-review-v1"
        run.notes = (
            (run.notes or "")
            + f"; reviewed_count={len(cases)}; source_input_count={source.input_count}; user-declared review, not formal acceptance; raw input not copied"
        )
        db.add(run)
        db.flush()
        for m in metrics:
            db.add(
                EvaluationMetric(
                    run_id=run.id,
                    project_id=project_id,
                    stage=m.stage,
                    metric_name=m.name,
                    metric_kind=m.kind,
                    sample_count=m.sample_count,
                    value=m.value,
                    details=dict(m.details)
                    | {
                        "reviewed_count": len(cases),
                        "source_input_count": source.input_count,
                        "label_status": LABEL_STATUS,
                    },
                )
            )
        # Free-text intent has no controlled vocabulary in this input contract.
        if source.run_type == "classification":
            db.add(
                EvaluationMetric(
                    run_id=run.id,
                    project_id=project_id,
                    stage="classification",
                    metric_name="intent_exact_match",
                    metric_kind="deterministic",
                    sample_count=0,
                    value=None,
                    details={
                        "reason": "no_controlled_intent_reference",
                        "reviewed_count": len(cases),
                        "source_input_count": source.input_count,
                    },
                )
            )
        for record in records:
            eid = record.case_id
            record.run_id = run.id
            record.expected = canonical(labels[eid])
            record.actual = canonical(actuals.get(eid))
            record.expected_hash = digest(record.expected)
            record.actual_hash = digest(record.actual)
            record.input = None
            record.notes = (
                "classification type/severity match"
                if source.run_type == "classification"
                else "membership existence only; semantic quality uses aggregate cluster metrics"
            )
            db.add(record)
        db.commit()
        db.refresh(run)
        return run
    except Exception:
        db.rollback()
        raise
