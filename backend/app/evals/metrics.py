"""Phase 6 — deterministic metric functions.

Every metric in this module is a pure function with the following
invariants:

* Each function returns a :class:`MetricResult` whose ``value`` is
  either a finite ``float`` (computed over a non-empty sample) or
  ``None`` to signal "NOT_MEASURED". Mixing the two readings is what
  the rest of the Phase 6 stack relies on so a ``None`` value never
  silently becomes a true zero.
* ``sample_count`` is the denominator used. When the metric has no
  eligible cases the function still returns a ``MetricResult`` with
  ``sample_count=0`` and ``value=None`` so the caller can render a row
  in the dashboard marked "N=0".
* ``metric_kind`` is always one of the strings
  ``deterministic``/``human_reviewed``/``llm_judge``. The current
  package only implements deterministic metrics, but the table allows
  ``human_reviewed`` and ``llm_judge`` rows so a future sprint can
  attach reviewed / LLM-judged metrics without a migration.
* ``details`` is a JSON-friendly dict with auxiliary counters and
  diagnostics. It must NEVER carry raw feedback text (the brief:
  default behaviour avoids storing sensitive raw content).

Definitions:

* ``feedback_type_accuracy`` — number of GT cases whose prediction
  matches the GT feedback type, divided by the number of GT cases
  (``len(golden)``). A case that has a GT label but no prediction is
  counted as incorrect and recorded in
  ``details["missing_pred_count"]``. Cases with a prediction but no
  GT are excluded from the denominator and surfaced in
  ``details["extra_pred_no_gt"]``.
* ``feedback_type_macro_f1`` — unweighted macro F1 over the GT
  labels. A missing prediction counts as a FN for the true label
  (and never as a FP for any other label). Cases without GT are
  excluded from the F1 counts entirely. The macro average is
  computed over the GT labels so a predicted-only label never
  inflates the F1 denominator.
* ``severity_exact_match`` — fraction of GT cases whose predicted
  ``severity`` (int 1..5) equals the GT severity. A case with no
  prediction (or a missing severity) is counted as wrong.
* ``severity_mae`` — mean absolute error over the pairs where both
  prediction and GT are available (CONDITIONAL). ``sample_count`` on
  the result is the conditional count, NOT the full denominator, so
  a conditional MAE cannot be confused with a full-cohort MAE. The
  full denominator and the missing-pred count are exposed in
  ``details`` for transparency.
* ``intent_exact_match`` — only computed when EVERY observed intent
  value is inside the supplied controlled vocabulary. If the
  vocabulary is empty or any observed value is not in the vocabulary
  the metric is reported as NOT_MEASURED so the supervisor does not
  compare two runs whose intent label set is open-ended.
* ``cluster_purity_micro`` — sum of majority-count per cluster divided
  by total members (excluding unlabelled members). ``None`` when
  there are zero labelled members.
* ``cluster_purity_macro`` — mean of per-cluster purity. Clusters with
  no labelled members are skipped.
* ``cluster_coverage`` — fraction of GT labels whose majority (>50%
  of its members) lives inside a single predicted cluster.
* ``cluster_fragmentation_mean`` — mean across GT labels of the
  number of distinct predicted clusters that contain at least one of
  the GT label's members. Lower is better.
* ``cluster_over_merge_ratio`` — fraction of predicted clusters whose
  members cover at least two distinct GT labels. Lower is better.
* ``pipeline_success_rate`` — fraction of cases whose status was a
  success stage. Failures are bucketed by ``error_type`` and the
  bucket distribution is exposed in ``details["by_error_type"]``.
* ``evidence_existence_rate`` — fraction of insight evidence rows
  whose feedback id is found in the supplied reference set of known
  feedback ids. This is the deterministic "does the row cite a real
  feedback id" check. The semantic validity / groundedness /
  hallucination metrics are explicitly NOT computed here (different
  scope; that work belongs to a future sprint with a human-reviewed
  ground-truth set).
* ``latency_*`` / ``token_*`` — quantile and aggregate statistics.
  All token / latency columns can be ``None`` (= not measured); the
  metric package propagates ``NOT_MEASURED`` and never collapses
  ``None`` into 0.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Hashable, Iterable, Mapping, Sequence

PHASE6_CALCULATION_VERSION = "phase6-metrics-v1"


# Defaults exposed for callers that want a single bundle of vocabularies.
SEVERITY_MIN = 1
SEVERITY_MAX = 5


class MetricKind(str, Enum):
    """Origin / trust level of a metric value."""

    DETERMINISTIC = "deterministic"
    HUMAN_REVIEWED = "human_reviewed"
    LLM_JUDGE = "llm_judge"


# Backward-compatible string constants matching the SQL enum.
METRIC_KIND_DETERMINISTIC = MetricKind.DETERMINISTIC.value
METRIC_KIND_HUMAN_REVIEWED = MetricKind.HUMAN_REVIEWED.value
METRIC_KIND_LLM_JUDGE = MetricKind.LLM_JUDGE.value


# ---- Result container --------------------------------------------------------


@dataclass(frozen=True)
class MetricResult:
    """One metric measurement.

    ``value`` is either a finite float or ``None`` (= NOT_MEASURED).
    ``details`` carries auxiliary counters / breakdowns; it must remain
    JSON-serialisable.
    """

    name: str
    kind: str
    stage: str
    sample_count: int
    value: float | None
    details: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# ---- Inputs -----------------------------------------------------------------


@dataclass(frozen=True)
class FeedbackTypePrediction:
    """One labelled row for feedback-type evaluation.

    ``severity`` is optional — only filled when the prediction comes
    from a structured classifier. ``intent`` is a free-text string; the
    intent exact-match metric requires every observed intent to be in
    the supplied controlled vocabulary.
    """

    case_id: str
    feedback_type: str
    severity: int | None = None
    intent: str | None = None
    extra: Mapping[str, object] | None = None


@dataclass(frozen=True)
class ClusterPrediction:
    """One predicted cluster for clustering evaluation."""

    cluster_id: str
    members: tuple[str, ...]
    extra: Mapping[str, object] | None = None


# ---- Utilities --------------------------------------------------------------


def _safe_mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _quantile(values: Sequence[int], pct: float) -> int | None:
    """Nearest-rank percentile over a sorted view of the data.

    ``None`` for empty input so the caller can distinguish NOT_MEASURED
    from a true zero.
    """
    if not values:
        return None
    if not (0.0 <= pct <= 100.0):
        raise ValueError(f"pct must be in [0,100]; got {pct}")
    sorted_values = sorted(values)
    rank = max(0, min(len(sorted_values) - 1, int(round(pct / 100.0 * (len(sorted_values) - 1)))))
    return int(sorted_values[rank])


def _is_finite_float(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


# ---- Classification ----------------------------------------------------------


def compute_feedback_type_metrics(
    predictions: Mapping[str, str | FeedbackTypePrediction],
    golden: Mapping[str, str],
    *,
    allowed_labels: Iterable[str] | None = None,
) -> tuple[MetricResult, MetricResult]:
    """Compute deterministic feedback_type accuracy + macro F1.

    Definitions (per the Phase 6 brief — the supervisor can
    reproduce every number by hand):

    * The accuracy denominator is the number of GT cases
      (``len(golden)``), NOT the number of comparable pairs. A case
      that has a GT label but no prediction is counted as incorrect
      and recorded in ``details["missing_pred_count"]``. Cases that
      have a prediction but no GT are excluded from the denominator
      entirely and surfaced in ``details["extra_pred_no_gt"]``.
    * For the macro F1, a missing prediction counts as a FN for the
      true label. It does NOT count as a FP for any other label.
      The macro average is computed over the GT labels so a
      predicted-only label never inflates the F1 denominator.
    * Cases without GT are excluded from the F1 counts entirely
      (the brief: do not include no-GT samples in the denominator).
    * ``allowed_labels`` is the optional controlled vocabulary. A
      prediction / GT label that is not in the vocabulary raises
      :class:`ValueError` so the caller cannot silently compute on
      illegal labels.

    Returns ``(accuracy, macro_f1)`` as :class:`MetricResult`. Empty
    inputs return ``MetricResult(..., value=None, sample_count=0)``
    so the caller can still render the row.
    """
    allowed = None if allowed_labels is None else frozenset(allowed_labels)

    correct = 0
    missing_pred_count = 0
    extra_pred_no_gt: list[str] = []
    skipped: dict[str, str] = {}
    y_true: list[str] = []
    y_pred: list[str | None] = []

    for case_id, gt_label in sorted(golden.items()):
        if allowed is not None and gt_label not in allowed:
            raise ValueError(
                f"golden case {case_id!r}: illegal feedback_type {gt_label!r}; "
                f"allowed={sorted(allowed)}"
            )
        pred = predictions.get(case_id)
        if pred is None:
            missing_pred_count += 1
            y_true.append(gt_label)
            y_pred.append(None)
            continue
        pred_label = pred.feedback_type if isinstance(pred, FeedbackTypePrediction) else pred
        if not isinstance(pred_label, str) or not pred_label:
            raise ValueError(
                f"prediction for case {case_id!r}: illegal feedback_type {pred_label!r}"
            )
        if allowed is not None and pred_label not in allowed:
            raise ValueError(
                f"prediction for case {case_id!r}: illegal feedback_type {pred_label!r}; "
                f"allowed={sorted(allowed)}"
            )
        if pred_label == gt_label:
            correct += 1
        y_true.append(gt_label)
        y_pred.append(pred_label)

    for case_id in predictions:
        if case_id not in golden:
            extra_pred_no_gt.append(case_id)
            skipped[case_id] = "no_ground_truth"

    total_gt = len(golden)
    accuracy = correct / total_gt if total_gt else None

    labels = sorted(set(y_true))
    per_label_f1: list[float] = []
    label_breakdown: dict[str, dict[str, int]] = {}
    for label in labels:
        tp = 0
        fp = 0
        fn = 0
        for t, p in zip(y_true, y_pred):
            if t == label and p == label:
                tp += 1
            elif t != label and p == label:
                fp += 1
            elif t == label and p != label:
                fn += 1
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        per_label_f1.append(f1)
        label_breakdown[label] = {"tp": tp, "fp": fp, "fn": fn}
    macro_f1 = _safe_mean(per_label_f1)

    details_acc: dict[str, object] = {
        "correct": correct,
        "denominator": total_gt,
        "missing_pred_count": missing_pred_count,
        "extra_pred_no_gt": sorted(extra_pred_no_gt),
        "skipped": skipped,
        "label_breakdown": label_breakdown,
    }
    details_f1: dict[str, object] = {
        "labels": labels,
        "per_label_f1": per_label_f1,
        "missing_pred_count": missing_pred_count,
        "label_breakdown": label_breakdown,
    }

    return (
        MetricResult(
            name="feedback_type_accuracy",
            kind=METRIC_KIND_DETERMINISTIC,
            stage="classification",
            sample_count=total_gt,
            value=accuracy,
            details=details_acc,
        ),
        MetricResult(
            name="feedback_type_macro_f1",
            kind=METRIC_KIND_DETERMINISTIC,
            stage="classification",
            sample_count=total_gt,
            value=macro_f1,
            details=details_f1,
        ),
    )


def compute_severity_metrics(
    predictions: Mapping[str, str | FeedbackTypePrediction],
    golden: Mapping[str, int],
    *,
    severity_range: tuple[int, int] = (SEVERITY_MIN, SEVERITY_MAX),
) -> tuple[MetricResult, MetricResult]:
    """Compute deterministic severity exact-match + MAE.

    Definitions (per the Phase 6 brief — the supervisor can
    reproduce every number by hand):

    * The exact-match denominator is the number of GT cases
      (``len(golden)``). A case that has a GT severity but no
      prediction (or a prediction with a missing severity) is
      counted as wrong and recorded in
      ``details["missing_pred_count"]``.
    * The MAE is the mean absolute error over the pairs where both
      prediction and GT are available. ``sample_count`` on the
      ``severity_mae`` result is the CONDITIONAL count (not the full
      denominator) so a conditional MAE cannot be confused with a
      full-cohort MAE. ``details["denominator"]`` and
      ``details["missing_pred_count"]`` expose the wider context for
      the dashboard.
    """
    lo, hi = severity_range
    if not (1 <= lo < hi <= 10):
        raise ValueError(f"severity_range must satisfy 1 <= lo < hi <= 10; got {severity_range}")

    abs_errors: list[int] = []
    correct = 0
    missing_pred_count = 0
    skipped: dict[str, str] = {}

    for case_id, gt_sev in sorted(golden.items()):
        if not isinstance(gt_sev, int) or not (lo <= gt_sev <= hi):
            raise ValueError(
                f"golden case {case_id!r}: illegal severity {gt_sev!r}; expected int in [{lo}, {hi}]"
            )
        pred = predictions.get(case_id)
        if pred is None:
            missing_pred_count += 1
            skipped[case_id] = "no_prediction"
            continue
        pred_sev = pred.severity if isinstance(pred, FeedbackTypePrediction) else None
        if pred_sev is None:
            missing_pred_count += 1
            skipped[case_id] = "no_severity"
            continue
        if not isinstance(pred_sev, int) or not (lo <= pred_sev <= hi):
            raise ValueError(f"prediction for case {case_id!r}: illegal severity {pred_sev!r}")
        abs_errors.append(abs(pred_sev - gt_sev))
        if pred_sev == gt_sev:
            correct += 1

    total_gt = len(golden)
    exact = correct / total_gt if total_gt else None
    mae = _safe_mean([float(e) for e in abs_errors]) if abs_errors else None
    conditional_sample_count = len(abs_errors)

    return (
        MetricResult(
            name="severity_exact_match",
            kind=METRIC_KIND_DETERMINISTIC,
            stage="classification",
            sample_count=total_gt,
            value=exact,
            details={
                "correct": correct,
                "denominator": total_gt,
                "missing_pred_count": missing_pred_count,
                "skipped": skipped,
            },
        ),
        MetricResult(
            name="severity_mae",
            kind=METRIC_KIND_DETERMINISTIC,
            stage="classification",
            sample_count=conditional_sample_count,
            value=mae,
            details={
                "abs_errors": abs_errors,
                "conditional_sample_count": conditional_sample_count,
                "denominator": total_gt,
                "missing_pred_count": missing_pred_count,
            },
        ),
    )


def compute_intent_exact_match(
    predictions: Mapping[str, str | FeedbackTypePrediction],
    golden: Mapping[str, str],
    *,
    controlled_vocabulary: Iterable[str],
) -> MetricResult:
    """Compute deterministic intent exact match.

    The metric only fires when ``controlled_vocabulary`` is non-empty
    AND every observed intent (both prediction and GT) is inside it.
    Otherwise the metric reports ``NOT_MEASURED``. The caller can
    therefore choose how strict their language is — open-ended intents
    stay out of every regression comparison.
    """
    vocab = frozenset(controlled_vocabulary)
    if not vocab:
        return MetricResult(
            name="intent_exact_match",
            kind=METRIC_KIND_DETERMINISTIC,
            stage="classification",
            sample_count=0,
            value=None,
            details={"reason": "empty_vocabulary"},
        )

    correct = 0
    compared = 0
    skipped: dict[str, str] = {}
    observed_vocab_misses: list[tuple[str, str]] = []

    for case_id, gt_intent in sorted(golden.items()):
        pred = predictions.get(case_id)
        if pred is None:
            skipped[case_id] = "no_prediction"
            continue
        pred_intent = pred.intent if isinstance(pred, FeedbackTypePrediction) else pred
        if pred_intent is None or gt_intent is None:
            skipped[case_id] = "missing_intent"
            continue
        if pred_intent not in vocab:
            observed_vocab_misses.append((case_id, pred_intent))
            continue
        if gt_intent not in vocab:
            observed_vocab_misses.append((case_id, gt_intent))
            continue
        compared += 1
        if pred_intent == gt_intent:
            correct += 1

    if observed_vocab_misses or compared == 0:
        return MetricResult(
            name="intent_exact_match",
            kind=METRIC_KIND_DETERMINISTIC,
            stage="classification",
            sample_count=compared,
            value=None,
            details={
                "reason": "out_of_vocabulary"
                if observed_vocab_misses
                else "no_eligible_comparisons",
                "out_of_vocabulary": observed_vocab_misses,
                "skipped": skipped,
            },
        )

    value = correct / compared if compared else None
    return MetricResult(
        name="intent_exact_match",
        kind=METRIC_KIND_DETERMINISTIC,
        stage="classification",
        sample_count=compared,
        value=value,
        details={"correct": correct, "compared": compared, "skipped": skipped},
    )


# ---- Clustering --------------------------------------------------------------


def _normalise_cluster_predictions(
    predictions: Sequence[ClusterPrediction | tuple[str, Sequence[Hashable]]],
) -> list[tuple[str, tuple[str, ...]]]:
    out: list[tuple[str, tuple[str, ...]]] = []
    for p in predictions:
        if isinstance(p, ClusterPrediction):
            members = tuple(str(m) for m in p.members)
            out.append((str(p.cluster_id), members))
        else:
            cluster_id, members = p
            out.append((str(cluster_id), tuple(str(m) for m in members)))
    return out


def compute_cluster_metrics(
    predictions: Sequence[ClusterPrediction | tuple[str, Sequence[Hashable]]],
    golden: Mapping[str, str],
) -> list[MetricResult]:
    """Compute deterministic cluster metrics.

    Metrics computed:

    * ``cluster_count`` — number of predicted clusters (positive int).
    * ``cluster_purity_micro`` — sum of majority-count per cluster
      divided by total labelled members. ``None`` if zero labelled
      members.
    * ``cluster_purity_macro`` — mean per-cluster purity. Clusters
      without any labelled members are skipped (cannot compute).
    * ``cluster_coverage`` — fraction of GT labels whose majority
      (>50%) lives inside a single predicted cluster. 0.0 when zero
      GT labels.
    * ``cluster_fragmentation_mean`` — mean across GT labels of the
      number of distinct predicted clusters that contain ≥1 member of
      the GT label. Lower is better.
    * ``cluster_over_merge_ratio`` — fraction of predicted clusters
      whose members cover ≥2 distinct GT labels. Lower is better.
      Reported as ``None`` when no predicted cluster has any labelled
      member.

    Behaviour on missing data:

    * Predicted members without a GT label do NOT count in the
      denominator or numerator (their cluster purity is computed over
      its labelled members only).
    * GT labels with no predicted members still appear in the
      fragmentation mean (contribute 0 distinct predicted clusters)
      and the coverage denominator.
    """
    normalised = _normalise_cluster_predictions(predictions)
    gt_labels_in_play = sorted(set(golden.values()))
    n_gt = len(gt_labels_in_play)
    n_pred = len(normalised)

    # cluster_to_gt: list parallel to ``normalised`` of GT labels per member.
    cluster_to_gt: list[list[str]] = []
    n_labelled_items = 0
    for _, members in normalised:
        gts = [golden.get(m) for m in members]
        gts_labelled = [g for g in gts if g is not None]
        cluster_to_gt.append(gts_labelled)
        n_labelled_items += len(gts_labelled)

    # Cluster count — number of predicted clusters (no labelled
    # denominator involved).
    metrics: list[MetricResult] = [
        MetricResult(
            name="cluster_count",
            kind=METRIC_KIND_DETERMINISTIC,
            stage="clustering",
            sample_count=n_pred,
            value=float(n_pred),
            details={"predicted_clusters": n_pred},
        )
    ]

    # purity micro / macro / over_merge_ratio
    cluster_purities: list[float] = []
    majority_total = 0
    over_merge_count = 0
    for gts in cluster_to_gt:
        distinct_gts = set(gts)
        if len(distinct_gts) >= 2:
            over_merge_count += 1
        if gts:
            counts = Counter(gts)
            top_n = counts.most_common(1)[0][1]
            majority_total += top_n
            cluster_purities.append(top_n / len(gts))

    purity_micro = majority_total / n_labelled_items if n_labelled_items else None
    purity_macro = _safe_mean(cluster_purities)
    over_merge_ratio = over_merge_count / n_pred if n_pred else None

    # Coverage + fragmentation
    gt_total_counts = Counter(golden.values())
    gt_to_clusters: dict[str, set[int]] = defaultdict(set)
    for idx, gts in enumerate(cluster_to_gt):
        for g in set(gts):
            gt_to_clusters[g].add(idx)

    recovered = 0
    for g in gt_labels_in_play:
        total = gt_total_counts.get(g, 0)
        if total == 0:
            continue
        best = max(
            (Counter(gts).get(g, 0) for gts in cluster_to_gt),
            default=0,
        )
        if best / total >= 0.5:
            recovered += 1

    coverage = recovered / n_gt if n_gt else 0.0
    fragmentation_per_gt = [len(gt_to_clusters.get(g, set())) for g in gt_labels_in_play]
    fragmentation_mean = _safe_mean([float(v) for v in fragmentation_per_gt])

    # cluster_count already added; continue with the rest.
    metrics.extend(
        [
            MetricResult(
                name="cluster_purity_micro",
                kind=METRIC_KIND_DETERMINISTIC,
                stage="clustering",
                sample_count=n_labelled_items,
                value=purity_micro,
                details={
                    "majority_total": majority_total,
                    "labelled_items": n_labelled_items,
                },
            ),
            MetricResult(
                name="cluster_purity_macro",
                kind=METRIC_KIND_DETERMINISTIC,
                stage="clustering",
                sample_count=len(cluster_purities),
                value=purity_macro,
                details={"per_cluster_purity": cluster_purities},
            ),
            MetricResult(
                name="cluster_coverage",
                kind=METRIC_KIND_DETERMINISTIC,
                stage="clustering",
                sample_count=n_gt,
                value=coverage,
                details={"recovered": recovered, "total_gt": n_gt},
            ),
            MetricResult(
                name="cluster_fragmentation_mean",
                kind=METRIC_KIND_DETERMINISTIC,
                stage="clustering",
                sample_count=n_gt,
                value=fragmentation_mean,
                details={"per_gt": fragmentation_per_gt},
            ),
            MetricResult(
                name="cluster_over_merge_ratio",
                kind=METRIC_KIND_DETERMINISTIC,
                stage="clustering",
                sample_count=n_pred,
                value=over_merge_ratio,
                details={"over_merge_count": over_merge_count},
            ),
        ]
    )
    return metrics


# ---- Insight evidence --------------------------------------------------------


def compute_evidence_existence_rate(
    evidence_feedback_ids: Sequence[str],
    known_feedback_ids: Iterable[str],
) -> MetricResult:
    """Deterministic "evidence references a known feedback id" rate.

    This metric only verifies that the feedback UUID referenced from an
    ``InsightEvidence`` row EXISTS in the project's feedback table. It
    does NOT verify semantic validity, groundedness or hallucination —
    those are separate human-reviewed metrics that must not be
    conflated with a deterministic existence check.
    """
    known = frozenset(known_feedback_ids)
    total = 0
    referenced = 0
    missing: list[str] = []
    raw = [str(v) for v in evidence_feedback_ids]
    for v in raw:
        total += 1
        if v in known:
            referenced += 1
        else:
            missing.append(v)
    value = referenced / total if total else None
    return MetricResult(
        name="evidence_existence_rate",
        kind=METRIC_KIND_DETERMINISTIC,
        stage="insight",
        sample_count=total,
        value=value,
        details={"referenced": referenced, "total": total, "missing": missing},
    )


# ---- Pipeline ----------------------------------------------------------------


def compute_pipeline_metrics(
    cases: Sequence[Mapping[str, object]],
    *,
    success_value: object = True,
) -> list[MetricResult]:
    """Compute pipeline success rate + per-stage failure breakdown.

    Each entry in ``cases`` is a mapping that must at minimum contain
    ``status`` (the stage outcome: a success value or a failure code)
    and MAY contain ``stage`` and ``error_type`` for failure bucketing.
    ``success_value`` is the value (default True) that counts as a
    pipeline success in this dataset — other values count as failures.
    """
    success_count = 0
    failure_total = 0
    failed_by_stage: dict[str, int] = defaultdict(int)
    by_error_type: dict[str, int] = defaultdict(int)
    stage_total: dict[str, int] = defaultdict(int)

    for case in cases:
        status = case.get("status")
        stage_value = case.get("stage")
        if stage_value is None:
            failed_by_stage["<unspecified>"] += 1
        else:
            stage_total[str(stage_value)] += 1
        if status == success_value:
            success_count += 1
        else:
            failure_total += 1
            error_type = str(case.get("error_type") or "<unspecified>")
            by_error_type[error_type] += 1
            stage_key = str(stage_value) if stage_value is not None else "<unspecified>"
            failed_by_stage[stage_key] += 1

    total = success_count + failure_total
    rate = success_count / total if total else None

    return [
        MetricResult(
            name="pipeline_success_rate",
            kind=METRIC_KIND_DETERMINISTIC,
            stage="pipeline",
            sample_count=total,
            value=rate,
            details={
                "success": success_count,
                "failed": failure_total,
            },
        ),
        MetricResult(
            name="pipeline_failure_stage_count",
            kind=METRIC_KIND_DETERMINISTIC,
            stage="pipeline",
            sample_count=len(stage_total),
            value=float(sum(failed_by_stage.values())),
            details={
                "failed_by_stage": dict(failed_by_stage),
                "by_error_type": dict(by_error_type),
                "stage_total": dict(stage_total),
            },
        ),
    ]


# ---- Latency / tokens --------------------------------------------------------


def compute_latency_metrics(
    latencies_ms: Sequence[int | None],
) -> list[MetricResult]:
    """Compute deterministic latency aggregates.

    Each input may be ``None`` (= not measured). Missing entries are
    NOT coerced to 0; the metric reports ``unknown_count`` separately
    and returns ``NOT_MEASURED`` for any aggregate that would be
    ambiguous. ``sample_count`` is the count of measured (= non-null)
    latencies for aggregates that have a measurable value.
    """
    measured: list[int] = []
    unknown: list[int] = []
    for idx, value in enumerate(latencies_ms):
        if value is None:
            unknown.append(idx)
            continue
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"latency_ms[{idx}]={value!r}: expected non-negative int or None")
        measured.append(value)

    sample_count = len(measured)
    if not measured:
        return [
            MetricResult(
                name="latency_p50_ms",
                kind=METRIC_KIND_DETERMINISTIC,
                stage="latency",
                sample_count=0,
                value=None,
                details={"unknown_count": len(unknown)},
            ),
            MetricResult(
                name="latency_p95_ms",
                kind=METRIC_KIND_DETERMINISTIC,
                stage="latency",
                sample_count=0,
                value=None,
                details={"unknown_count": len(unknown)},
            ),
            MetricResult(
                name="latency_mean_ms",
                kind=METRIC_KIND_DETERMINISTIC,
                stage="latency",
                sample_count=0,
                value=None,
                details={"unknown_count": len(unknown)},
            ),
        ]

    return [
        MetricResult(
            name="latency_p50_ms",
            kind=METRIC_KIND_DETERMINISTIC,
            stage="latency",
            sample_count=sample_count,
            value=float(_quantile(measured, 50.0) or 0),
            details={"unknown_count": len(unknown)},
        ),
        MetricResult(
            name="latency_p95_ms",
            kind=METRIC_KIND_DETERMINISTIC,
            stage="latency",
            sample_count=sample_count,
            value=float(_quantile(measured, 95.0) or 0),
            details={"unknown_count": len(unknown)},
        ),
        MetricResult(
            name="latency_mean_ms",
            kind=METRIC_KIND_DETERMINISTIC,
            stage="latency",
            sample_count=sample_count,
            value=_safe_mean([float(v) for v in measured]),
            details={"unknown_count": len(unknown)},
        ),
    ]


def compute_token_metrics(
    input_tokens: Sequence[int | None],
    output_tokens: Sequence[int | None],
    unknown_token_calls: Sequence[int | None],
) -> MetricResult:
    """Aggregate token usage without ever coercing ``None`` to 0.

    Any entry that is ``None`` is recorded under ``details["unknown"]``
    and excluded from the totals. When ALL entries on a side are
    ``None`` the corresponding total is reported as ``None``
    (= NOT_MEASURED).
    """
    if len(input_tokens) != len(output_tokens) or len(input_tokens) != len(unknown_token_calls):
        raise ValueError(
            "input_tokens / output_tokens / unknown_token_calls must have equal length"
        )

    in_total: int | None = 0
    out_total: int | None = 0
    in_unknown = 0
    out_unknown = 0
    unk_calls_total = 0
    seen_in_null = False
    seen_out_null = False

    for idx, (tin, tout, unk) in enumerate(zip(input_tokens, output_tokens, unknown_token_calls)):
        if tin is None:
            in_unknown += 1
            seen_in_null = True
            in_total = None  # type: ignore[assignment]
        else:
            if isinstance(tin, bool) or not isinstance(tin, int) or tin < 0:
                raise ValueError(f"input_tokens[{idx}]={tin!r}: must be a non-negative int or None")
            if in_total is not None:
                in_total += int(tin)
        if tout is None:
            out_unknown += 1
            seen_out_null = True
            out_total = None  # type: ignore[assignment]
        else:
            if isinstance(tout, bool) or not isinstance(tout, int) or tout < 0:
                raise ValueError(
                    f"output_tokens[{idx}]={tout!r}: must be a non-negative int or None"
                )
            if out_total is not None:
                out_total += int(tout)
        if unk is None:
            raise ValueError(f"unknown_token_calls[{idx}] must be a non-negative int; got None")
        if not isinstance(unk, int) or unk < 0:
            raise ValueError(f"unknown_token_calls[{idx}]={unk!r}: must be a non-negative int")
        unk_calls_total += unk

    sample_count = len(input_tokens)
    return MetricResult(
        name="token_usage_total",
        kind=METRIC_KIND_DETERMINISTIC,
        stage="token",
        sample_count=sample_count,
        value=None,  # the numeric body lives in details; meta-metric is "unknown" awareness
        details={
            "input_tokens_total": in_total,
            "output_tokens_total": out_total,
            "input_tokens_unknown": in_unknown,
            "output_tokens_unknown": out_unknown,
            "input_tokens_total_unknown_flag": seen_in_null,
            "output_tokens_total_unknown_flag": seen_out_null,
            "unknown_token_calls_total": unk_calls_total,
        },
    )


def aggregate_metrics_by_kind(
    metrics: Sequence[MetricResult],
) -> dict[str, list[MetricResult]]:
    """Group metrics by their ``metric_kind`` for dashboard / Ops use."""
    out: dict[str, list[MetricResult]] = defaultdict(list)
    for metric in metrics:
        out[metric.kind].append(metric)
    return dict(out)
