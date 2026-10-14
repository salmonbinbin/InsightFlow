"""Phase 6 — regression comparison between two evaluation runs.

The brief: ``regression 比较同一 dataset/version + metric kind + 样本口径
的两次 run，missing metric 明示而非当作零；实现纯函数即可。``

What this module guarantees:

* Two runs are comparable ONLY when they share the same
  ``dataset_name`` + ``dataset_version`` + ``stage`` + ``metric_kind``
  + ``metric_name`` + ``sample_count``. A mismatch on any of those
  is a structural regression error and is raised.
* For each comparable metric the function returns the baseline
  value, the candidate value and the delta
  (``candidate - baseline``). ``None`` propagates from
  :class:`MetricResult` (= NOT_MEASURED); it is NEVER silently
  coerced to 0.
* When the baseline or the candidate is MISSING for a comparable
  metric, the function still emits a row and flags the regression
  status as ``missing_baseline`` / ``missing_candidate``. The
  supervisor's dashboard uses that flag to call out the drift
  rather than to hide it.
* The result is a frozen dataclass so the comparison can be cached
  or JSON-serialised without further sanitisation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Mapping, Sequence

from app.evals.metrics import MetricResult

# Backward-compatible string constants.
REGRESSION_MATCHED = "matched"
REGRESSION_MISSING_BASELINE = "missing_baseline"
REGRESSION_MISSING_CANDIDATE = "missing_candidate"
REGRESSION_BOTH_MISSING = "both_missing"
REGRESSION_SAMPLE_MISMATCH = "sample_mismatch"


class RegressionStatus(str, Enum):
    """How the baseline / candidate pair aligns on this metric."""

    MATCHED = "matched"
    MISSING_BASELINE = "missing_baseline"
    MISSING_CANDIDATE = "missing_candidate"
    BOTH_MISSING = "both_missing"
    SAMPLE_MISMATCH = "sample_mismatch"


# All regression status values as a tuple (handy for the test + dashboard).
REGRESSION_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in RegressionStatus)


@dataclass(frozen=True)
class MetricSnapshot:
    """One row's worth of context required for a like-for-like comparison.

    The :class:`MetricSnapshot` is the only input the comparison
    function needs. Persisting these snapshots next to the
    :class:`MetricResult` rows is the recommended pattern — that way
    the comparison can be replayed offline without re-reading the
    database.
    """

    dataset_name: str
    dataset_version: str
    stage: str
    metric_name: str
    metric_kind: str
    sample_count: int
    value: float | None
    details: Mapping[str, object] = field(default_factory=dict)
    run_id: str | None = None
    calculation_version: str | None = None

    @classmethod
    def from_metric_result(
        cls,
        result: MetricResult,
        *,
        dataset_name: str,
        dataset_version: str,
        run_id: str | None = None,
        calculation_version: str | None = None,
        extra_details: Mapping[str, object] | None = None,
    ) -> "MetricSnapshot":
        return cls(
            dataset_name=dataset_name,
            dataset_version=dataset_version,
            stage=result.stage,
            metric_name=result.name,
            metric_kind=result.kind,
            sample_count=result.sample_count,
            value=result.value,
            details=dict(extra_details) if extra_details else dict(result.details),
            run_id=run_id,
            calculation_version=calculation_version,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RegressionMetric:
    """One comparable metric in the regression output."""

    dataset_name: str
    dataset_version: str
    stage: str
    metric_name: str
    metric_kind: str
    sample_count: int
    baseline_value: float | None
    candidate_value: float | None
    delta: float | None
    status: str
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RegressionComparison:
    """Bundle of :class:`RegressionMetric` plus the context anchors.

    ``summary`` is a small dict for the dashboard's headline; it does
    NOT replace the per-metric detail (which the supervisor still
    reads row-by-row).
    """

    baseline_run_id: str | None
    candidate_run_id: str | None
    dataset_name: str
    dataset_version: str
    metrics: tuple[RegressionMetric, ...]
    summary: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# ---- Helpers -----------------------------------------------------------------


def _key(snapshot: MetricSnapshot) -> tuple[str, str, str, str]:
    return (snapshot.dataset_name, snapshot.dataset_version, snapshot.stage, snapshot.metric_name)


def _delta(baseline: float | None, candidate: float | None) -> float | None:
    if baseline is None or candidate is None:
        return None
    return float(candidate) - float(baseline)


# ---- Entry point -------------------------------------------------------------


def compare_metric_sets(
    baseline: Sequence[MetricSnapshot],
    candidate: Sequence[MetricSnapshot],
    *,
    baseline_run_id: str | None = None,
    candidate_run_id: str | None = None,
) -> RegressionComparison:
    """Compare two sets of :class:`MetricSnapshot`.

    Pre-flight: at least one snapshot must have a non-empty
    ``dataset_name`` + ``dataset_version``. All snapshots must
    reference the SAME dataset (or the function raises ``ValueError``)
    — that is how the brief "同一 dataset/version" rule is enforced.

    Per-metric rule:

    * ``(dataset_name, dataset_version, stage, metric_name)`` is the
      composite key. A baseline + candidate row with the same key is
      paired; rows without a counterpart are flagged
      ``missing_baseline`` / ``missing_candidate``.
    * Within a pair, the ``metric_kind`` MUST match (a deterministic
      metric cannot be compared to a human-reviewed one). Mismatches
      raise ``ValueError`` so a structural regression error cannot be
      silently swallowed.
    * Within a pair, the ``sample_count`` MUST match (different
      sample denominators break the comparison). The result row is
      still emitted but its status is ``sample_mismatch``; the
      ``delta`` is reported as ``None`` so a side-by-side diff
      cannot accidentally claim a number from incompatible sample
      sizes.
    * Missing values propagate as ``None`` (NOT_MEASURED). A
      baseline / candidate pair where BOTH values are ``None`` is
      status ``both_missing``.
    """
    if not baseline and not candidate:
        raise ValueError("both baseline and candidate metric sets are empty")

    if baseline and candidate:
        baseline_dataset = {(b.dataset_name, b.dataset_version) for b in baseline}
        candidate_dataset = {(c.dataset_name, c.dataset_version) for c in candidate}
        # The dataset axis is a regression-level invariant — different
        # datasets cannot be compared at all.
        if len(baseline_dataset) > 1 or len(candidate_dataset) > 1:
            raise ValueError(
                "each side must reference a single dataset; "
                f"baseline={baseline_dataset}, candidate={candidate_dataset}"
            )
        if baseline_dataset != candidate_dataset:
            raise ValueError(
                "baseline and candidate reference different datasets; "
                f"baseline={baseline_dataset}, candidate={candidate_dataset}"
            )
        dataset_name, dataset_version = next(iter(baseline_dataset))
    elif baseline:
        dataset_name, dataset_version = next(
            iter({(b.dataset_name, b.dataset_version) for b in baseline})
        )
    else:
        dataset_name, dataset_version = next(
            iter({(c.dataset_name, c.dataset_version) for c in candidate})
        )

    baseline_index: dict[tuple[str, str, str, str], MetricSnapshot] = {}
    for snap in baseline:
        key = _key(snap)
        if key in baseline_index:
            raise ValueError(f"duplicate baseline snapshot for key={key!r}")
        baseline_index[key] = snap

    candidate_index: dict[tuple[str, str, str, str], MetricSnapshot] = {}
    for snap in candidate:
        key = _key(snap)
        if key in candidate_index:
            raise ValueError(f"duplicate candidate snapshot for key={key!r}")
        candidate_index[key] = snap

    all_keys: list[tuple[str, str, str, str]] = sorted(set(baseline_index) | set(candidate_index))

    rows: list[RegressionMetric] = []
    matched = 0
    mismatched_kind = 0
    mismatched_sample = 0
    missing_baseline_count = 0
    missing_candidate_count = 0
    both_missing = 0

    for key in all_keys:
        b = baseline_index.get(key)
        c = candidate_index.get(key)
        _dname, _dver, stage, metric_name = key

        if b is None:
            assert c is not None  # key was in all_keys = union
            missing_baseline_count += 1
            rows.append(
                RegressionMetric(
                    dataset_name=dataset_name,
                    dataset_version=dataset_version,
                    stage=stage,
                    metric_name=metric_name,
                    metric_kind=c.metric_kind,
                    sample_count=c.sample_count,
                    baseline_value=None,
                    candidate_value=c.value,
                    delta=None,
                    status=RegressionStatus.MISSING_BASELINE.value,
                    notes=("baseline has no comparable metric",),
                )
            )
            continue
        if c is None:
            missing_candidate_count += 1
            rows.append(
                RegressionMetric(
                    dataset_name=dataset_name,
                    dataset_version=dataset_version,
                    stage=stage,
                    metric_name=metric_name,
                    metric_kind=b.metric_kind,
                    sample_count=b.sample_count,
                    baseline_value=b.value,
                    candidate_value=None,
                    delta=None,
                    status=RegressionStatus.MISSING_CANDIDATE.value,
                    notes=("candidate has no comparable metric",),
                )
            )
            continue

        if b.metric_kind != c.metric_kind:
            mismatched_kind += 1
            raise ValueError(
                "metric_kind mismatch for "
                f"key={key!r}: baseline={b.metric_kind!r}, candidate={c.metric_kind!r}"
            )

        # Value-availability takes priority over sample-count: when one
        # side has no measurement (value=None) the delta is undefined
        # regardless of how many samples were attempted.
        if b.value is None and c.value is None:
            both_missing += 1
            rows.append(
                RegressionMetric(
                    dataset_name=dataset_name,
                    dataset_version=dataset_version,
                    stage=stage,
                    metric_name=metric_name,
                    metric_kind=b.metric_kind,
                    sample_count=c.sample_count,
                    baseline_value=None,
                    candidate_value=None,
                    delta=None,
                    status=RegressionStatus.BOTH_MISSING.value,
                    notes=(),
                )
            )
            continue
        if b.value is None:
            missing_baseline_count += 1
            rows.append(
                RegressionMetric(
                    dataset_name=dataset_name,
                    dataset_version=dataset_version,
                    stage=stage,
                    metric_name=metric_name,
                    metric_kind=b.metric_kind,
                    sample_count=c.sample_count,
                    baseline_value=None,
                    candidate_value=c.value,
                    delta=None,
                    status=RegressionStatus.MISSING_BASELINE.value,
                    notes=("baseline value is NOT_MEASURED",),
                )
            )
            continue
        if c.value is None:
            missing_candidate_count += 1
            rows.append(
                RegressionMetric(
                    dataset_name=dataset_name,
                    dataset_version=dataset_version,
                    stage=stage,
                    metric_name=metric_name,
                    metric_kind=b.metric_kind,
                    sample_count=b.sample_count,
                    baseline_value=b.value,
                    candidate_value=None,
                    delta=None,
                    status=RegressionStatus.MISSING_CANDIDATE.value,
                    notes=("candidate value is NOT_MEASURED",),
                )
            )
            continue

        if b.sample_count != c.sample_count:
            mismatched_sample += 1
            rows.append(
                RegressionMetric(
                    dataset_name=dataset_name,
                    dataset_version=dataset_version,
                    stage=stage,
                    metric_name=metric_name,
                    metric_kind=b.metric_kind,
                    sample_count=c.sample_count,
                    baseline_value=b.value,
                    candidate_value=c.value,
                    delta=None,
                    status=RegressionStatus.SAMPLE_MISMATCH.value,
                    notes=(
                        f"baseline sample_count={b.sample_count}",
                        f"candidate sample_count={c.sample_count}",
                    ),
                )
            )
            continue

        matched += 1
        rows.append(
            RegressionMetric(
                dataset_name=dataset_name,
                dataset_version=dataset_version,
                stage=stage,
                metric_name=metric_name,
                metric_kind=b.metric_kind,
                sample_count=b.sample_count,
                baseline_value=b.value,
                candidate_value=c.value,
                delta=_delta(b.value, c.value),
                status=RegressionStatus.MATCHED.value,
                notes=(),
            )
        )

    summary: dict[str, object] = {
        "matched": matched,
        "missing_baseline": missing_baseline_count,
        "missing_candidate": missing_candidate_count,
        "both_missing": both_missing,
        "sample_mismatch": mismatched_sample,
        "metric_kind_mismatch_raised": mismatched_kind,
    }
    return RegressionComparison(
        baseline_run_id=baseline_run_id,
        candidate_run_id=candidate_run_id,
        dataset_name=dataset_name,
        dataset_version=dataset_version,
        metrics=tuple(rows),
        summary=summary,
    )


def aggregate_regression_by_status(
    comparison: RegressionComparison,
) -> Mapping[str, int]:
    """Group regression rows by status. Convenience helper for the dashboard."""
    out: dict[str, int] = {}
    for row in comparison.metrics:
        out[row.status] = out.get(row.status, 0) + 1
    return out
