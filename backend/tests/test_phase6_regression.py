"""Phase 6 — regression comparison tests.

Coverage:

* Matched metrics surface baseline / candidate values + delta.
* Missing baseline / missing candidate / both missing cases are
  reported (NEVER coerced to 0).
* Sample-count mismatch is surfaced and the delta is suppressed.
* metric_kind mismatch is rejected (different trust levels cannot be
  compared in silence).
* Different datasets are rejected at the entry point.
* Empty inputs raise (you cannot "regress against nothing").
* ``MetricSnapshot.from_metric_result`` ties the snapshot to a real
  :class:`MetricResult` with the right dataset anchors.
"""

from __future__ import annotations

import pytest

from app.evals.metrics import MetricResult
from app.evals.regression import (
    REGRESSION_BOTH_MISSING,
    REGRESSION_MATCHED,
    REGRESSION_MISSING_BASELINE,
    REGRESSION_MISSING_CANDIDATE,
    REGRESSION_SAMPLE_MISMATCH,
    REGRESSION_STATUS_VALUES,
    MetricSnapshot,
    RegressionStatus,
    compare_metric_sets,
)

# ---- Helpers -----------------------------------------------------------------


def _snap(
    metric_name: str,
    value: float | None,
    *,
    sample_count: int = 12,
    kind: str = "deterministic",
    stage: str = "classification",
    dataset_name: str = "demo_feedback_plus_golden",
    dataset_version: str = "demo-abc123",
) -> MetricSnapshot:
    return MetricSnapshot(
        dataset_name=dataset_name,
        dataset_version=dataset_version,
        stage=stage,
        metric_name=metric_name,
        metric_kind=kind,
        sample_count=sample_count,
        value=value,
    )


# ---- Matched -----------------------------------------------------------------


def test_matched_metrics_report_delta():
    baseline = [_snap("feedback_type_accuracy", 0.7)]
    candidate = [_snap("feedback_type_accuracy", 0.8)]
    cmp = compare_metric_sets(
        baseline, candidate, baseline_run_id="run-A", candidate_run_id="run-B"
    )
    assert cmp.dataset_name == "demo_feedback_plus_golden"
    assert len(cmp.metrics) == 1
    row = cmp.metrics[0]
    assert row.status == REGRESSION_MATCHED
    assert row.baseline_value == pytest.approx(0.7)
    assert row.candidate_value == pytest.approx(0.8)
    assert row.delta == pytest.approx(0.1)
    assert cmp.summary["matched"] == 1


def test_matched_missing_values_become_both_missing():
    """NOT_MEASURED propagates: both values None → both_missing, delta None."""
    baseline = [_snap("feedback_type_accuracy", None, sample_count=0)]
    candidate = [_snap("feedback_type_accuracy", None, sample_count=0)]
    cmp = compare_metric_sets(baseline, candidate)
    row = cmp.metrics[0]
    assert row.status == REGRESSION_BOTH_MISSING
    assert row.baseline_value is None
    assert row.candidate_value is None
    assert row.delta is None


def test_matched_baseline_present_candidate_missing_returns_not_zero():
    """A None on the candidate side is reported as None, NOT zero."""
    baseline = [_snap("feedback_type_accuracy", 0.7)]
    candidate = [_snap("feedback_type_accuracy", None, sample_count=0)]
    cmp = compare_metric_sets(baseline, candidate)
    row = cmp.metrics[0]
    assert row.status == REGRESSION_MISSING_CANDIDATE
    assert row.candidate_value is None
    assert row.delta is None
    assert row.baseline_value == pytest.approx(0.7)
    assert cmp.summary["missing_candidate"] == 1


def test_matched_candidate_present_baseline_missing_returns_not_zero():
    baseline = [_snap("feedback_type_accuracy", None, sample_count=0)]
    candidate = [_snap("feedback_type_accuracy", 0.7)]
    cmp = compare_metric_sets(baseline, candidate)
    row = cmp.metrics[0]
    assert row.status == REGRESSION_MISSING_BASELINE
    assert row.baseline_value is None
    assert row.candidate_value == pytest.approx(0.7)
    assert row.delta is None


def test_extra_metric_only_in_candidate_is_reported():
    """A new metric that only appears in the candidate must be flagged, not dropped."""
    baseline = [_snap("feedback_type_accuracy", 0.7)]
    candidate = [
        _snap("feedback_type_accuracy", 0.75),
        _snap("cluster_purity_micro", 0.9, stage="clustering"),
    ]
    cmp = compare_metric_sets(baseline, candidate)
    by_status = {row.metric_name: row for row in cmp.metrics}
    assert by_status["cluster_purity_micro"].status == REGRESSION_MISSING_BASELINE
    assert by_status["feedback_type_accuracy"].status == REGRESSION_MATCHED
    assert cmp.summary["missing_baseline"] == 1
    assert cmp.summary["matched"] == 1


def test_extra_metric_only_in_baseline_is_reported():
    baseline = [
        _snap("feedback_type_accuracy", 0.7),
        _snap("severity_mae", 0.3),
    ]
    candidate = [_snap("feedback_type_accuracy", 0.75)]
    cmp = compare_metric_sets(baseline, candidate)
    by_name = {row.metric_name: row for row in cmp.metrics}
    assert by_name["severity_mae"].status == REGRESSION_MISSING_CANDIDATE
    assert by_name["feedback_type_accuracy"].status == REGRESSION_MATCHED


# ---- Sample mismatch ---------------------------------------------------------


def test_sample_count_mismatch_suppresses_delta():
    baseline = [_snap("feedback_type_accuracy", 0.7, sample_count=12)]
    candidate = [_snap("feedback_type_accuracy", 0.9, sample_count=8)]
    cmp = compare_metric_sets(baseline, candidate)
    row = cmp.metrics[0]
    assert row.status == REGRESSION_SAMPLE_MISMATCH
    assert row.delta is None
    assert "baseline sample_count=12" in row.notes
    assert "candidate sample_count=8" in row.notes


# ---- Structural errors -------------------------------------------------------


def test_different_datasets_are_rejected():
    baseline = [_snap("feedback_type_accuracy", 0.7, dataset_version="demo-aaa")]
    candidate = [_snap("feedback_type_accuracy", 0.8, dataset_version="demo-bbb")]
    with pytest.raises(ValueError, match="different datasets"):
        compare_metric_sets(baseline, candidate)


def test_metric_kind_mismatch_raises():
    """A deterministic metric cannot be compared to a human-reviewed one."""
    baseline = [_snap("feedback_type_accuracy", 0.7, kind="deterministic")]
    candidate = [_snap("feedback_type_accuracy", 0.7, kind="human_reviewed")]
    with pytest.raises(ValueError, match="metric_kind mismatch"):
        compare_metric_sets(baseline, candidate)


def test_duplicate_baseline_key_raises():
    baseline = [
        _snap("feedback_type_accuracy", 0.7),
        _snap("feedback_type_accuracy", 0.8),
    ]
    candidate = [_snap("feedback_type_accuracy", 0.9)]
    with pytest.raises(ValueError, match="duplicate baseline snapshot"):
        compare_metric_sets(baseline, candidate)


def test_empty_both_sides_raises():
    with pytest.raises(ValueError, match="empty"):
        compare_metric_sets([], [])


def test_one_sided_dataset_is_inferred_from_present():
    """When only one side has data, the dataset is inferred from that side."""
    baseline = [_snap("feedback_type_accuracy", 0.7, dataset_version="demo-aaa")]
    candidate: list[MetricSnapshot] = []
    cmp = compare_metric_sets(baseline, candidate)
    assert cmp.dataset_name == "demo_feedback_plus_golden"
    assert cmp.dataset_version == "demo-aaa"
    assert cmp.metrics[0].status == REGRESSION_MISSING_CANDIDATE


# ---- MetricSnapshot.from_metric_result ---------------------------------------


def test_metric_snapshot_from_metric_result_captures_anchors():
    result = MetricResult(
        name="feedback_type_accuracy",
        kind="deterministic",
        stage="classification",
        sample_count=12,
        value=0.8,
        details={"correct": 10, "compared": 12, "skipped": {}},
    )
    snap = MetricSnapshot.from_metric_result(
        result,
        dataset_name="demo_feedback_plus_golden",
        dataset_version="demo-abc123",
        run_id="run-1",
        calculation_version="phase6-metrics-v1",
    )
    assert snap.dataset_name == "demo_feedback_plus_golden"
    assert snap.dataset_version == "demo-abc123"
    assert snap.metric_name == "feedback_type_accuracy"
    assert snap.metric_kind == "deterministic"
    assert snap.stage == "classification"
    assert snap.sample_count == 12
    assert snap.value == pytest.approx(0.8)
    assert snap.run_id == "run-1"
    assert snap.calculation_version == "phase6-metrics-v1"
    assert snap.details["correct"] == 10


# ---- End-to-end: metrics → snapshot → regression -----------------------------


def test_end_to_end_metric_to_regression_uses_real_metric_results():
    """The same metric package can be plumbed into the regression comparison.

    The supervisor should be able to do:

        metrics = compute_feedback_type_metrics(...)
        snapshots = [MetricSnapshot.from_metric_result(m, ...) for m in metrics]
        compare_metric_sets(baseline_snapshots, candidate_snapshots)
    """
    from app.evals.metrics import compute_feedback_type_metrics

    golden = {"F001": "bug", "F002": "feature_request"}
    baseline_pred = {"F001": "bug", "F002": "feature_request"}
    candidate_pred = {"F001": "bug", "F002": "bug"}

    def _snapshots(preds):
        accuracy, _macro = compute_feedback_type_metrics(
            preds,
            golden,
            allowed_labels=(
                "bug",
                "feature_request",
                "ux_issue",
                "performance_issue",
                "pricing_issue",
                "content_issue",
                "positive_feedback",
                "other",
            ),
        )
        return [
            MetricSnapshot.from_metric_result(
                accuracy,
                dataset_name="demo_feedback_plus_golden",
                dataset_version="demo-abc123",
                run_id="r",
            )
        ]

    baseline_snaps = _snapshots(baseline_pred)
    candidate_snaps = _snapshots(candidate_pred)
    cmp = compare_metric_sets(baseline_snaps, candidate_snaps)
    assert len(cmp.metrics) == 1
    row = cmp.metrics[0]
    assert row.status == REGRESSION_MATCHED
    assert row.baseline_value == pytest.approx(1.0)
    assert row.candidate_value == pytest.approx(0.5)
    assert row.delta == pytest.approx(-0.5)


# ---- Constants ---------------------------------------------------------------


def test_regression_status_values_cover_every_status():
    """The status string set is closed and stable."""
    assert set(REGRESSION_STATUS_VALUES) == {
        REGRESSION_MATCHED,
        REGRESSION_MISSING_BASELINE,
        REGRESSION_MISSING_CANDIDATE,
        REGRESSION_BOTH_MISSING,
        REGRESSION_SAMPLE_MISMATCH,
    }


def test_regression_status_enum_matches_string_constants():
    assert RegressionStatus.MATCHED.value == REGRESSION_MATCHED
    assert RegressionStatus.MISSING_BASELINE.value == REGRESSION_MISSING_BASELINE
    assert RegressionStatus.MISSING_CANDIDATE.value == REGRESSION_MISSING_CANDIDATE
    assert RegressionStatus.BOTH_MISSING.value == REGRESSION_BOTH_MISSING
    assert RegressionStatus.SAMPLE_MISMATCH.value == REGRESSION_SAMPLE_MISMATCH
