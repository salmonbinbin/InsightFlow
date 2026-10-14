"""Phase 6 — Evaluation harness.

Pure-function metric package. Every public function returns immutable
results (``MetricResult`` or lists of ``MetricResult``) so the same
maths can be replayed offline and the supervisor's independent
verification sees the same numbers.
"""

from app.evals.dataset import (
    DATASET_LABEL_STATUS_SYNTHETIC,
    DATASET_NAME_DEMO,
    DATASET_PROVENANCE_DEMO,
    DATASET_SPLIT_FULL,
    DatasetCase,
    DatasetProvenance,
    DatasetValidationError,
    build_dataset_version,
    hash_dataset_cases,
    load_demo_dataset,
    validate_dataset_cases,
)
from app.evals.metrics import (
    PHASE6_CALCULATION_VERSION,
    ClusterPrediction,
    FeedbackTypePrediction,
    MetricResult,
    aggregate_metrics_by_kind,
    compute_cluster_metrics,
    compute_evidence_existence_rate,
    compute_feedback_type_metrics,
    compute_intent_exact_match,
    compute_latency_metrics,
    compute_pipeline_metrics,
    compute_severity_metrics,
    compute_token_metrics,
)
from app.evals.regression import (
    MetricSnapshot,
    RegressionComparison,
    RegressionMetric,
    RegressionStatus,
    compare_metric_sets,
)
from app.evals.runner import (
    LOCAL_HASH_EMBEDDING_MODEL,
    SUPPORTED_EVALUATION_TYPES,
    EvaluationRunnerConfigError,
    EvaluationRunnerSourceMismatchError,
    EvaluationRunnerValidationError,
    evaluate_existing_run,
)

__all__ = [
    "DATASET_LABEL_STATUS_SYNTHETIC",
    "DATASET_NAME_DEMO",
    "DATASET_PROVENANCE_DEMO",
    "DATASET_SPLIT_FULL",
    "DatasetCase",
    "DatasetProvenance",
    "DatasetValidationError",
    "EvaluationRunnerConfigError",
    "EvaluationRunnerSourceMismatchError",
    "EvaluationRunnerValidationError",
    "LOCAL_HASH_EMBEDDING_MODEL",
    "MetricResult",
    "ClusterPrediction",
    "FeedbackTypePrediction",
    "PHASE6_CALCULATION_VERSION",
    "aggregate_metrics_by_kind",
    "build_dataset_version",
    "compute_cluster_metrics",
    "compute_evidence_existence_rate",
    "compute_feedback_type_metrics",
    "compute_intent_exact_match",
    "compute_latency_metrics",
    "compute_pipeline_metrics",
    "compute_severity_metrics",
    "compute_token_metrics",
    "hash_dataset_cases",
    "load_demo_dataset",
    "MetricSnapshot",
    "RegressionComparison",
    "RegressionMetric",
    "RegressionStatus",
    "SUPPORTED_EVALUATION_TYPES",
    "compare_metric_sets",
    "evaluate_existing_run",
    "validate_dataset_cases",
]
