"""Phase 6 — dataset loader for the demo feedback + golden labels.

The loader is the ONLY consumer of the two CSVs in ``data/`` so we
keep that surface narrow. The principles:

* The dataset NEVER claims to be human-reviewed. The golden labels
  are explicitly marked as ``synthetic + unreviewed reference`` so a
  Phase 6 metric computation is never silently misread as
  ``ground truth``.
* The dataset is loaded ONCE and validated in full. Duplicate case
  IDs, missing case IDs, illegal severity values, etc. raise
  :class:`DatasetValidationError` so the loader cannot quietly drop
  or reorder rows.
* The content hash is a deterministic SHA-256 over the canonical
  JSON form of the case list (sorted by ``case_id``). Two loads of
  the same CSV produce the same hash; two different CSVs produce
  different hashes — this is what the regression comparison uses to
  reject silent dataset drift.
* The dataset version is derived from the content hash (truncated to
  16 chars + a ``demo-`` prefix) so the regression comparison never
  has to invent a meaningless version string.
* The dataset split is always ``full`` for this batch: with only 12
  feedback rows a train/validation/test split is not statistically
  meaningful (the brief: ``不强拆12条 train/validation/test``).
* The loader returns a :class:`DatasetProvenance` snapshot — the
  caller passes that snapshot into the persistence layer so a run row
  captures the dataset name / version / content hash at the moment
  the run started, not the moment the user asks the dashboard.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

DATASET_NAME_DEMO = "demo_feedback_plus_golden"
DATASET_PROVENANCE_DEMO = "demo"
DATASET_SPLIT_FULL = "full"
DATASET_LABEL_STATUS_SYNTHETIC = "synthetic_reference_labels_unreviewed_no_human_review"

# Hard-coded demo inputs — these resolve relative to the repo root
# (NOT relative to the file location) so the loader doesn't break when
# the backend folder is moved.
DEFAULT_DEMO_FEEDBACK_CSV = "demo_feedback.csv"
DEFAULT_DEMO_GOLDEN_CSV = "golden_dataset.csv"


# Validators mirror the Phase 1 vocabulary.  These are the canonical
# sets we ACCEPT — we do not invent new types here.
_FEEDBACK_TYPES = frozenset(
    {
        "bug",
        "feature_request",
        "ux_issue",
        "performance_issue",
        "pricing_issue",
        "content_issue",
        "positive_feedback",
        "other",
    }
)

_SENTIMENTS = frozenset({"positive", "neutral", "negative", "mixed"})

_SEVERITY_RANGE = (1, 5)


class DatasetValidationError(ValueError):
    """Raised when the loader detects a malformed / duplicit / illegal row."""

    def __init__(self, message: str, *, errors: Sequence[str] | None = None) -> None:
        super().__init__(message)
        self.errors: list[str] = list(errors or [])


# ---- Value objects -----------------------------------------------------------


@dataclass(frozen=True)
class DatasetCase:
    """One feedback case with its (optional) GT labels.

    ``feedback_text`` is the user's raw message. The framework never
    persists ``feedback_text`` into the cases table by default; the
    hash of ``feedback_text`` is the only thing that lands in
    ``EvaluationCase.input_hash``. ``feedback_text`` MAY be persisted
    when the caller explicitly opts in (in which case the policy is
    recorded in the row's ``notes`` field).
    """

    case_id: str
    feedback_text: str
    stage: str = "classification"
    feedback_type_label: str | None = None
    module_label: str | None = None
    intent_label: str | None = None
    cluster_label: str | None = None
    severity_label: int | None = None
    sentiment_label: str | None = None
    extra: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class DatasetProvenance:
    """Snapshot of the loaded dataset used to anchor an evaluation run."""

    name: str
    version: str
    content_hash: str
    provenance: str
    split: str
    label_status: str
    cases: tuple[DatasetCase, ...]
    cases_by_id: Mapping[str, DatasetCase]
    source_paths: tuple[str, ...]

    def case_count(self) -> int:
        return len(self.cases)


# ---- Validation --------------------------------------------------------------


def validate_dataset_cases(cases: Sequence[DatasetCase]) -> None:
    """Validate a list of :class:`DatasetCase`.

    Rules:

    * ``case_id`` MUST be non-empty. A duplicate ``case_id`` raises
      (the brief: duplicate case IDs must not be silently overwritten).
    * ``feedback_type_label``, when present, MUST be in the controlled
      ``_FEEDBACK_TYPES`` vocabulary.
    * ``severity_label``, when present, MUST be an integer in 1..5.
    * ``sentiment_label``, when present, MUST be in the controlled
      ``_SENTIMENTS`` vocabulary.

    Failing cases are aggregated into a single ``DatasetValidationError``
    so the caller can fix all of them in one pass.
    """
    errors: list[str] = []
    seen: set[str] = set()
    for idx, case in enumerate(cases):
        if not isinstance(case.case_id, str) or not case.case_id.strip():
            errors.append(f"cases[{idx}].case_id must be a non-empty string; got {case.case_id!r}")
            continue
        if case.case_id in seen:
            errors.append(f"duplicate case_id {case.case_id!r}")
            continue
        seen.add(case.case_id)
        if case.feedback_type_label is not None:
            if case.feedback_type_label not in _FEEDBACK_TYPES:
                errors.append(
                    f"case {case.case_id!r}: illegal feedback_type_label "
                    f"{case.feedback_type_label!r}; allowed={sorted(_FEEDBACK_TYPES)}"
                )
        if case.severity_label is not None:
            lo, hi = _SEVERITY_RANGE
            if not isinstance(case.severity_label, int) or not (lo <= case.severity_label <= hi):
                errors.append(
                    f"case {case.case_id!r}: illegal severity_label "
                    f"{case.severity_label!r}; expected int in [{lo}, {hi}]"
                )
        if case.sentiment_label is not None:
            if case.sentiment_label not in _SENTIMENTS:
                errors.append(
                    f"case {case.case_id!r}: illegal sentiment_label "
                    f"{case.sentiment_label!r}; allowed={sorted(_SENTIMENTS)}"
                )
    if errors:
        raise DatasetValidationError(
            f"dataset validation failed with {len(errors)} error(s)",
            errors=errors,
        )


# ---- Hashing & versioning ----------------------------------------------------


def _hash_text(text: str) -> str:
    """Return the SHA-256 hex digest of a feedback text.

    Used as a stable fingerprint of the raw content for the dataset
    content hash so two loads with identical labels but different raw
    text are recognised as different dataset versions. The plaintext
    NEVER appears in the canonical form so the hash does not leak raw
    user content.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonicalise_cases(cases: Sequence[DatasetCase]) -> str:
    """Stable canonical JSON form: sorted by case_id.

    The fingerprint ``feedback_text_hash`` is included so a raw-text
    change alone (with labels unchanged) bumps the dataset version
    and the regression comparison refuses to treat the two loads as
    the same dataset. The raw text itself is never serialised.
    """

    serialisable = []
    for case in sorted(cases, key=lambda c: c.case_id):
        serialisable.append(
            {
                "case_id": case.case_id,
                "feedback_text_hash": _hash_text(case.feedback_text),
                "feedback_type_label": case.feedback_type_label,
                "module_label": case.module_label,
                "intent_label": case.intent_label,
                "cluster_label": case.cluster_label,
                "severity_label": case.severity_label,
                "sentiment_label": case.sentiment_label,
                "stage": case.stage,
                "extra": dict(case.extra),
            }
        )
    return json.dumps(serialisable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_dataset_cases(cases: Sequence[DatasetCase]) -> str:
    """Return the SHA-256 hex digest of the canonicalised case list."""
    canonical = _canonicalise_cases(cases).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_dataset_version(cases: Sequence[DatasetCase], *, prefix: str = "demo") -> str:
    """Build a deterministic ``<prefix>-<16-hex>`` version tag."""
    digest = hash_dataset_cases(cases)
    return f"{prefix}-{digest[:16]}"


# ---- Demo CSV loader ---------------------------------------------------------


def load_demo_dataset(
    data_dir: Path | str | None = None,
    *,
    filename_feedback: str = DEFAULT_DEMO_FEEDBACK_CSV,
    filename_golden: str = DEFAULT_DEMO_GOLDEN_CSV,
) -> DatasetProvenance:
    """Load the demo feedback CSV + golden labels CSV into a :class:`DatasetProvenance`.

    Parameters
    ----------
    data_dir : Path | str | None
        Folder containing ``filename_feedback`` and ``filename_golden``.
        When ``None`` the loader walks the parents of this file to find
        ``<repo>/data/`` (the historical layout used by
        ``scripts/run_classification_eval.py``).
    """
    data_path = Path(data_dir) if data_dir else _find_data_dir()
    feedback_csv = data_path / filename_feedback
    golden_csv = data_path / filename_golden
    errors: list[str] = []
    if not feedback_csv.is_file():
        errors.append(f"feedback CSV not found at {feedback_csv}")
    if not golden_csv.is_file():
        errors.append(f"golden CSV not found at {golden_csv}")
    if errors:
        raise DatasetValidationError("demo data missing", errors=errors)

    feedback_rows = _read_csv_dict(feedback_csv)
    golden_rows = _read_csv_dict(golden_csv)

    golden_by_fid: dict[str, dict[str, str]] = {}
    for row in golden_rows:
        case_id = (row.get("feedback_id") or "").strip()
        if not case_id:
            errors.append(f"golden CSV row missing feedback_id: {row!r}")
            continue
        if case_id in golden_by_fid:
            errors.append(f"golden CSV has duplicate feedback_id {case_id!r}")
            continue
        golden_by_fid[case_id] = row

    cases: list[DatasetCase] = []
    seen: set[str] = set()
    for row in feedback_rows:
        case_id = (row.get("feedback_id") or "").strip()
        if not case_id:
            errors.append(f"feedback CSV row missing feedback_id: {row!r}")
            continue
        if case_id in seen:
            errors.append(f"feedback CSV has duplicate feedback_id {case_id!r}")
            continue
        seen.add(case_id)
        text = (row.get("content") or "").strip()
        golden = golden_by_fid.get(case_id)
        cases.append(
            DatasetCase(
                case_id=case_id,
                feedback_text=text,
                feedback_type_label=_opt_str(golden, "gt_feedback_type") if golden else None,
                module_label=_opt_str(golden, "gt_module") if golden else None,
                intent_label=_opt_str(golden, "gt_intent") if golden else None,
                cluster_label=_opt_str(golden, "gt_cluster") if golden else None,
                severity_label=_opt_int(golden, "gt_severity") if golden else None,
                sentiment_label=None,
            )
        )

    # Cases that appear in the golden CSV but not in the feedback CSV
    # would be a different class of drift — surface them as warnings
    # without silently dropping. The dataset is incomplete in that case
    # and the caller may want to refuse to compute against it.
    for case_id in sorted(golden_by_fid.keys() - seen):
        errors.append(f"golden CSV has feedback_id {case_id!r} with no matching feedback row")

    validate_dataset_cases(cases)

    if errors:
        raise DatasetValidationError("demo dataset malformed", errors=errors)

    cases_tuple = tuple(cases)
    content_hash = hash_dataset_cases(cases_tuple)
    return DatasetProvenance(
        name=DATASET_NAME_DEMO,
        version=build_dataset_version(cases_tuple),
        content_hash=content_hash,
        provenance=DATASET_PROVENANCE_DEMO,
        split=DATASET_SPLIT_FULL,
        label_status=DATASET_LABEL_STATUS_SYNTHETIC,
        cases=cases_tuple,
        cases_by_id={c.case_id: c for c in cases_tuple},
        source_paths=(str(feedback_csv), str(golden_csv)),
    )


# ---- helpers ---------------------------------------------------------------


def _find_data_dir() -> Path:
    """Walk upward from this file to find the repo's ``data/`` folder."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "data"
        if candidate.is_dir():
            return candidate
    raise DatasetValidationError(f"could not locate the repo's data/ folder starting from {here}")


def _read_csv_dict(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fp:
        return list(csv.DictReader(fp))


def _opt_str(row: Mapping[str, str], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _opt_int(row: Mapping[str, str], key: str) -> int | None:
    value = row.get(key)
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise DatasetValidationError(f"golden row has non-integer {key}={value!r}") from exc
