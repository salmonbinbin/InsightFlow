"""Phase 6 — metric computation tests.

Coverage:

* Feedback type accuracy + macro F1 with the controlled vocabulary
  enforced (illegal labels raise, never coerce).
* Severity exact match + MAE (range check + missing predictions).
* Intent exact match (controlled vocabulary required; otherwise
  NOT_MEASURED propagates with reason).
* Cluster purity micro / macro / coverage / fragmentation / over-merge
  / cluster count, including the missing-label + missing-prediction
  edge cases that the brief calls out explicitly.
* Evidence existence rate (deterministic only; semantic validity is
  out of scope here).
* Pipeline success rate + per-stage failure bucket.
* Latency / token aggregates honour ``None`` (= not measured); they
  never silently coerce missing measurements to zero.
* ``aggregate_metrics_by_kind`` returns the right grouping.
"""

from __future__ import annotations

import pytest

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

# ---- Helpers ----------------------------------------------------------------


FEEDBACK_TYPES = (
    "bug",
    "feature_request",
    "ux_issue",
    "performance_issue",
    "pricing_issue",
    "content_issue",
    "positive_feedback",
    "other",
)


def _pred(case_id: str, ftype: str, *, severity: int | None = None, intent: str | None = None):
    return FeedbackTypePrediction(
        case_id=case_id,
        feedback_type=ftype,
        severity=severity,
        intent=intent,
    )


# ---- Feedback type accuracy + macro F1 --------------------------------------


def test_feedback_type_metrics_match_documented_definitions():
    """Accuracy = correct / |GT|; macro F1 = mean over GT labels only.

    Designed to mirror the synthetic demo set so the supervisor can
    reproduce the number by hand:

      GT:   F001=bug  F002=feature_request  F003=other
      Pred: F001=bug  F002=ux_issue          F003=other

    All three cases have GT, so the accuracy denominator is 3
    (correct=2 → 2/3). The macro F1 is averaged over the GT labels
    only ({bug, feature_request, other}); ux_issue is a predicted-
    only label and is excluded from the F1 denominator entirely.
    """
    golden = {
        "F001": "bug",
        "F002": "feature_request",
        "F003": "other",
    }
    predictions = {
        "F001": _pred("F001", "bug"),
        "F002": _pred("F002", "ux_issue"),
        "F003": _pred("F003", "other"),
    }
    acc, f1 = compute_feedback_type_metrics(predictions, golden, allowed_labels=FEEDBACK_TYPES)
    assert acc.sample_count == 3
    assert acc.value == pytest.approx(2 / 3)
    assert acc.details["correct"] == 2
    assert acc.details["denominator"] == 3
    assert acc.details["missing_pred_count"] == 0

    # Per-label F1 over GT labels:
    #   bug: tp=1, fp=0, fn=0 -> 1.0
    #   feature_request: tp=0, fp=0, fn=1 -> 0.0
    #   other: tp=1, fp=0, fn=0 -> 1.0
    # Macro = (1.0 + 0.0 + 1.0) / 3 = 0.6666...
    assert f1.sample_count == 3
    assert f1.value == pytest.approx(2 / 3)


def test_feedback_type_metrics_accept_plain_string_predictions():
    golden = {"F001": "bug", "F002": "bug"}
    predictions = {"F001": "bug", "F002": "feature_request"}
    acc, f1 = compute_feedback_type_metrics(predictions, golden, allowed_labels=FEEDBACK_TYPES)
    assert acc.value == pytest.approx(0.5)
    assert 0.0 <= (f1.value or 0.0) <= 1.0


def test_feedback_type_metrics_missing_prediction_counts_as_incorrect():
    """Missing prediction counts as incorrect (accuracy) and FN for the
    true label (macro F1). Denominator is the full GT count, NOT the
    number of comparable pairs.
    """
    golden = {"F001": "bug", "F002": "bug", "F003": "bug"}
    predictions = {"F001": _pred("F001", "bug")}  # F002, F003 have no prediction
    acc, f1 = compute_feedback_type_metrics(predictions, golden, allowed_labels=FEEDBACK_TYPES)
    # accuracy = 1/3 — the 2 missing-pred cases count as wrong
    assert acc.sample_count == 3
    assert acc.value == pytest.approx(1 / 3)
    assert acc.details["correct"] == 1
    assert acc.details["denominator"] == 3
    assert acc.details["missing_pred_count"] == 2
    # macro F1 over {"bug"}: tp=1, fp=0, fn=2 → precision=1, recall=1/3, f1=0.5
    assert f1.sample_count == 3
    assert f1.value == pytest.approx(0.5)
    assert f1.details["missing_pred_count"] == 2


def test_feedback_type_metrics_extra_pred_no_gt_excluded_from_denominator():
    """Cases with a prediction but no GT are excluded from the accuracy
    denominator and from the F1 counts entirely.
    """
    golden = {"F001": "bug", "F002": "bug"}
    predictions = {
        "F001": _pred("F001", "bug"),
        "F002": _pred("F002", "bug"),
        "F003": _pred("F003", "ux_issue"),  # no GT — must not enter denominator
    }
    acc, f1 = compute_feedback_type_metrics(predictions, golden, allowed_labels=FEEDBACK_TYPES)
    assert acc.sample_count == 2
    assert acc.value == pytest.approx(1.0)
    assert acc.details["extra_pred_no_gt"] == ["F003"]
    assert acc.details["skipped"]["F003"] == "no_ground_truth"
    # F1 over {"bug"} only: tp=2, fp=0, fn=0 → 1.0
    assert f1.value == pytest.approx(1.0)
    assert f1.details["labels"] == ["bug"]


def test_feedback_type_metrics_reject_illegal_label():
    golden = {"F001": "bug"}
    predictions = {"F001": _pred("F001", "bug")}
    # Illegal GT label must raise — not silently coerce.
    with pytest.raises(ValueError, match="illegal feedback_type"):
        compute_feedback_type_metrics(predictions, golden, allowed_labels=("only_one",))


def test_feedback_type_metrics_reject_illegal_prediction_label():
    golden = {"F001": "bug"}
    predictions = {"F001": _pred("F001", "only_one")}
    with pytest.raises(ValueError, match="illegal feedback_type"):
        compute_feedback_type_metrics(predictions, golden, allowed_labels=FEEDBACK_TYPES)


def test_feedback_type_metrics_empty_inputs_return_not_measured():
    acc, f1 = compute_feedback_type_metrics({}, {}, allowed_labels=FEEDBACK_TYPES)
    assert acc.sample_count == 0
    assert acc.value is None
    assert f1.sample_count == 0
    assert f1.value is None


# ---- Severity ----------------------------------------------------------------


def test_severity_metrics_exact_and_mae():
    golden = {"F001": 3, "F002": 4, "F003": 5, "F004": 1}
    predictions = {
        "F001": _pred("F001", "bug", severity=3),
        "F002": _pred("F002", "bug", severity=5),  # off by 1
        "F003": _pred("F003", "bug", severity=5),  # exact
        "F004": _pred("F004", "bug", severity=2),  # off by 1
    }
    exact, mae = compute_severity_metrics(predictions, golden)
    assert exact.sample_count == 4
    assert exact.value == pytest.approx(0.5)
    assert mae.value == pytest.approx(0.5)


def test_severity_metrics_skip_when_severity_missing():
    """Missing severity counts as wrong for exact-match (full denominator)
    and is excluded from the MAE sample (conditional). The full GT
    count is exposed in details so the dashboard cannot confuse a
    conditional MAE with a full-cohort MAE.
    """
    golden = {"F001": 3, "F002": 3}
    predictions = {
        "F001": _pred("F001", "bug", severity=3),
        "F002": _pred("F002", "bug", severity=None),
    }
    exact, mae = compute_severity_metrics(predictions, golden)
    # exact: denominator = 2 GT cases, correct = 1 → 0.5
    assert exact.sample_count == 2
    assert exact.value == pytest.approx(0.5)
    assert exact.details["correct"] == 1
    assert exact.details["denominator"] == 2
    assert exact.details["missing_pred_count"] == 1
    assert exact.details["skipped"]["F002"] == "no_severity"
    # MAE: only over the 1 case with both values → 0.0; sample_count=1
    # is the conditional count, NOT the full denominator.
    assert mae.value == pytest.approx(0.0)
    assert mae.sample_count == 1
    assert mae.details["conditional_sample_count"] == 1
    assert mae.details["denominator"] == 2
    assert mae.details["missing_pred_count"] == 1


def test_severity_metrics_mae_is_conditional_not_full():
    """When half of the GT cases have no prediction, the MAE sample_count
    is the CONDITIONAL count (pairs with both values), not the full
    denominator. This is what stops a conditional MAE from being
    silently treated as a full-cohort MAE on the dashboard.
    """
    golden = {"F001": 3, "F002": 4, "F003": 5, "F004": 1}
    predictions = {
        "F001": _pred("F001", "bug", severity=3),  # exact
        "F002": _pred("F002", "bug", severity=5),  # off by 1
        # F003 has no prediction at all
        # F004 has no prediction either
    }
    exact, mae = compute_severity_metrics(predictions, golden)
    # exact denominator = 4, correct = 1 → 0.25
    assert exact.sample_count == 4
    assert exact.value == pytest.approx(0.25)
    assert exact.details["missing_pred_count"] == 2
    # MAE conditional = 2 (only F001 and F002 have both values)
    assert mae.sample_count == 2
    assert mae.value == pytest.approx(0.5)  # (0 + 1) / 2
    assert mae.details["conditional_sample_count"] == 2
    assert mae.details["denominator"] == 4
    assert mae.details["missing_pred_count"] == 2


def test_severity_metrics_reject_out_of_range_severity():
    golden = {"F001": 7}
    predictions = {"F001": _pred("F001", "bug", severity=3)}
    with pytest.raises(ValueError, match="illegal severity"):
        compute_severity_metrics(predictions, golden)


def test_severity_metrics_empty_returns_not_measured():
    exact, mae = compute_severity_metrics({}, {})
    assert exact.value is None and mae.value is None
    assert exact.sample_count == 0 and mae.sample_count == 0


# ---- Intent -----------------------------------------------------------------


def test_intent_exact_match_inside_controlled_vocabulary():
    golden = {"F001": "batch_image_upload", "F002": "batch_image_upload"}
    predictions = {
        "F001": _pred("F001", "feature_request", intent="batch_image_upload"),
        "F002": _pred("F002", "feature_request", intent="upload_pdf_reliably"),
    }
    result = compute_intent_exact_match(
        predictions, golden, controlled_vocabulary=("batch_image_upload", "upload_pdf_reliably")
    )
    assert result.sample_count == 2
    assert result.value == pytest.approx(0.5)


def test_intent_exact_match_uncontrolled_returns_not_measured():
    golden = {"F001": "batch_image_upload"}
    predictions = {"F001": _pred("F001", "feature_request", intent="anything goes")}
    result = compute_intent_exact_match(
        predictions, golden, controlled_vocabulary=("batch_image_upload",)
    )
    assert result.value is None
    assert result.details["reason"] == "out_of_vocabulary"
    assert result.details["out_of_vocabulary"] == [("F001", "anything goes")]


def test_intent_exact_match_empty_vocabulary():
    result = compute_intent_exact_match({}, {}, controlled_vocabulary=())
    assert result.value is None
    assert result.details["reason"] == "empty_vocabulary"


# ---- Cluster metrics ---------------------------------------------------------


def test_cluster_metrics_perfect_alignment():
    """When predicted clusters match GT labels 1-to-1, all metrics are ideal."""
    predictions = [
        ClusterPrediction("cA", ("F001", "F002", "F003")),
        ClusterPrediction("cB", ("F004", "F005")),
        ClusterPrediction("cC", ("F006",)),
    ]
    golden = {
        "F001": "bug_upload",
        "F002": "bug_upload",
        "F003": "bug_upload",
        "F004": "performance",
        "F005": "performance",
        "F006": "ux",
    }
    metrics = {m.name: m for m in compute_cluster_metrics(predictions, golden)}
    assert metrics["cluster_count"].value == 3.0
    assert metrics["cluster_purity_micro"].value == pytest.approx(1.0)
    assert metrics["cluster_purity_macro"].value == pytest.approx(1.0)
    assert metrics["cluster_coverage"].value == pytest.approx(1.0)
    assert metrics["cluster_fragmentation_mean"].value == pytest.approx(1.0)
    assert metrics["cluster_over_merge_ratio"].value == 0.0


def test_cluster_metrics_over_merge_and_fragmentation():
    predictions = [
        ClusterPrediction("cMixed", ("F001", "F002", "F003", "F004")),
    ]
    golden = {
        "F001": "A",
        "F002": "A",
        "F003": "B",
        "F004": "B",
    }
    metrics = {m.name: m for m in compute_cluster_metrics(predictions, golden)}
    assert metrics["cluster_purity_micro"].value == pytest.approx(0.5)
    # 2 GT labels, both appear in the single predicted cluster → fragmentation = 1.0
    assert metrics["cluster_fragmentation_mean"].value == pytest.approx(1.0)
    assert metrics["cluster_over_merge_ratio"].value == pytest.approx(1.0)


def test_cluster_metrics_missing_labels_skipped():
    """Members without a GT label must NOT inflate the denominator."""
    predictions = [
        ClusterPrediction("c1", ("F001", "F002", "F999")),
        ClusterPrediction("c2", ("F003", "F004")),
    ]
    golden = {
        "F001": "A",
        "F002": "A",
        "F003": "B",
        "F004": "B",
    }
    metrics = {m.name: m for m in compute_cluster_metrics(predictions, golden)}
    # Both clusters are pure over their labelled members → micro = 1.0
    assert metrics["cluster_purity_micro"].value == pytest.approx(1.0)
    # Both clusters have ≥1 labelled member → macro = 1.0
    assert metrics["cluster_purity_macro"].value == pytest.approx(1.0)
    # No over-merge (each cluster has 1 distinct GT label)
    assert metrics["cluster_over_merge_ratio"].value == 0.0
    # sample_count for purity_micro is the labelled count = 4
    assert metrics["cluster_purity_micro"].sample_count == 4


def test_cluster_metrics_uncovered_gt_label_counts_in_denominator():
    """A GT label with no predicted members contributes 0 fragmentation and 0 coverage."""
    predictions = [ClusterPrediction("c1", ("F001",))]
    golden = {"F001": "A", "F002": "B"}  # F002 has no predicted member
    metrics = {m.name: m for m in compute_cluster_metrics(predictions, golden)}
    # 2 GT labels in the denominator; 1 recovered (A) → coverage = 0.5
    assert metrics["cluster_coverage"].value == pytest.approx(0.5)
    # fragmentation = mean over [1, 0] = 0.5
    assert metrics["cluster_fragmentation_mean"].value == pytest.approx(0.5)
    assert metrics["cluster_fragmentation_mean"].sample_count == 2


def test_cluster_metrics_no_predictions_returns_not_measured():
    metrics = {m.name: m for m in compute_cluster_metrics([], {"F001": "A", "F002": "B"})}
    assert metrics["cluster_count"].value == 0.0
    assert metrics["cluster_purity_micro"].value is None
    assert metrics["cluster_purity_macro"].value is None
    assert metrics["cluster_coverage"].value == 0.0
    assert metrics["cluster_fragmentation_mean"].value == 0.0
    assert metrics["cluster_over_merge_ratio"].value is None


# ---- Evidence existence ------------------------------------------------------


def test_evidence_existence_rate_references_known_only():
    evidence_ids = ["F001", "F002", "F003", "F999"]
    result = compute_evidence_existence_rate(evidence_ids, ["F001", "F002", "F003"])
    assert result.sample_count == 4
    assert result.value == pytest.approx(0.75)
    assert result.details["missing"] == ["F999"]


def test_evidence_existence_rate_empty_returns_not_measured():
    result = compute_evidence_existence_rate([], ["F001"])
    assert result.value is None
    assert result.sample_count == 0


# ---- Pipeline ----------------------------------------------------------------


def test_pipeline_metrics_success_rate_and_buckets():
    cases = [
        {"status": True, "stage": "classification"},
        {"status": True, "stage": "classification"},
        {"status": False, "stage": "classification", "error_type": "transport_error"},
        {"status": False, "stage": "insight", "error_type": "validation_error"},
    ]
    success, failures = compute_pipeline_metrics(cases)
    assert success.sample_count == 4
    assert success.value == pytest.approx(0.5)
    assert failures.value == 2.0
    assert failures.details["failed_by_stage"]["classification"] == 1
    assert failures.details["failed_by_stage"]["insight"] == 1
    assert failures.details["by_error_type"]["transport_error"] == 1
    assert failures.details["by_error_type"]["validation_error"] == 1


def test_pipeline_metrics_empty_input_not_measured():
    success, failures = compute_pipeline_metrics([])
    assert success.value is None
    assert failures.value == 0.0


# ---- Latency / token ---------------------------------------------------------


def test_latency_metrics_quantiles_and_mean():
    metrics = {m.name: m for m in compute_latency_metrics([10, 20, 30, 40, 100, 200])}
    # Nearest-rank: p50 on 6 values → index 2 (value 30), p95 → index 5 (value 200).
    assert metrics["latency_p50_ms"].value == pytest.approx(30.0)
    assert metrics["latency_p95_ms"].value == pytest.approx(200.0)
    assert metrics["latency_mean_ms"].value == pytest.approx(66.6666666)
    assert all(m.sample_count == 6 for m in metrics.values())


def test_latency_metrics_distinguishes_unknown_from_zero():
    metrics = {m.name: m for m in compute_latency_metrics([None, None])}
    for m in metrics.values():
        assert m.value is None
        assert m.sample_count == 0
        assert m.details["unknown_count"] == 2


def test_latency_metrics_mixed_keeps_known_only():
    metrics = {m.name: m for m in compute_latency_metrics([None, 50, 100, None, 200])}
    # Measured sample: [50, 100, 200]; nearest-rank: p50 index 1 → 100, p95 index 2 → 200.
    assert metrics["latency_p50_ms"].value == pytest.approx(100.0)
    assert metrics["latency_p95_ms"].value == pytest.approx(200.0)
    assert metrics["latency_mean_ms"].value == pytest.approx((50 + 100 + 200) / 3)
    assert metrics["latency_mean_ms"].details["unknown_count"] == 2


def test_latency_metrics_rejects_negative():
    with pytest.raises(ValueError):
        compute_latency_metrics([10, -1])


def test_token_metrics_separates_unknown():
    result = compute_token_metrics(
        input_tokens=[100, None, 200],
        output_tokens=[50, 60, None],
        unknown_token_calls=[0, 1, 0],
    )
    details = result.details
    assert result.sample_count == 3
    assert details["input_tokens_unknown"] == 1
    assert details["output_tokens_unknown"] == 1
    # The aggregates are NOT_MEASURED when ANY side is unknown — the
    # brief forbids silently coercing a partial total to 0.
    assert details["input_tokens_total"] is None
    assert details["output_tokens_total"] is None
    assert details["unknown_token_calls_total"] == 1


def test_token_metrics_all_known_returns_totals():
    result = compute_token_metrics(
        input_tokens=[100, 200],
        output_tokens=[50, 60],
        unknown_token_calls=[0, 0],
    )
    assert result.sample_count == 2
    assert result.details["input_tokens_total"] == 300
    assert result.details["output_tokens_total"] == 110
    assert result.details["unknown_token_calls_total"] == 0


def test_token_metrics_rejects_negative():
    with pytest.raises(ValueError):
        compute_token_metrics(
            input_tokens=[100, -10],
            output_tokens=[50, 60],
            unknown_token_calls=[0, 0],
        )


def test_token_metrics_length_mismatch_raises():
    with pytest.raises(ValueError):
        compute_token_metrics(
            input_tokens=[100],
            output_tokens=[50, 60],
            unknown_token_calls=[0, 0],
        )


# ---- aggregation helper ------------------------------------------------------


def test_aggregate_metrics_by_kind_groups_correctly():
    metrics = [
        MetricResult(
            name="cluster_purity_micro",
            kind="deterministic",
            stage="clustering",
            sample_count=12,
            value=0.9,
        ),
        MetricResult(
            name="feedback_type_accuracy",
            kind="deterministic",
            stage="classification",
            sample_count=12,
            value=0.8,
        ),
        MetricResult(
            name="llm_judge_helpfulness",
            kind="llm_judge",
            stage="insight",
            sample_count=12,
            value=0.7,
        ),
    ]
    grouped = aggregate_metrics_by_kind(metrics)
    assert set(grouped.keys()) == {"deterministic", "llm_judge"}
    assert [m.name for m in grouped["deterministic"]] == [
        "cluster_purity_micro",
        "feedback_type_accuracy",
    ]
    assert [m.name for m in grouped["llm_judge"]] == ["llm_judge_helpfulness"]


def test_phase6_calculation_version_is_pinned():
    """The metric package version must be stable for regression comparison."""
    assert isinstance(PHASE6_CALCULATION_VERSION, str)
    assert PHASE6_CALCULATION_VERSION.startswith("phase6-")
