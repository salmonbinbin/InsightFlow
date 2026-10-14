"""Phase 6 — dataset loader tests.

The tests cover the live CSVs in ``data/`` plus the synthetic
validation paths the brief calls out:

* Duplicate case IDs must be rejected.
* Illegal severity / feedback type / sentiment must be rejected.
* Content hash + dataset version are stable across loads and change
  when any label changes.
* Demo dataset is fully loaded with the documented 12 cases (no
  silent truncation, no silent train/val/test split).
* Demo labels are explicitly marked ``synthetic + unreviewed
  reference`` so the regression comparison never treats them as
  ground truth.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.evals.dataset import (
    DATASET_LABEL_STATUS_SYNTHETIC,
    DATASET_NAME_DEMO,
    DATASET_PROVENANCE_DEMO,
    DATASET_SPLIT_FULL,
    DatasetCase,
    DatasetValidationError,
    build_dataset_version,
    hash_dataset_cases,
    load_demo_dataset,
    validate_dataset_cases,
)

# ---- Validation paths --------------------------------------------------------


def _make_case(case_id: str = "F001", **overrides) -> DatasetCase:
    defaults = dict(
        case_id=case_id,
        feedback_text="hello world",
        stage="classification",
        feedback_type_label="bug",
        module_label="image_upload",
        intent_label="batch_image_upload",
        cluster_label="batch_image_upload",
        severity_label=3,
    )
    defaults.update(overrides)
    return DatasetCase(**defaults)


def test_validate_dataset_cases_accepts_well_formed_set():
    cases = [
        _make_case("F001"),
        _make_case("F002", feedback_type_label="feature_request", severity_label=4),
    ]
    validate_dataset_cases(cases)  # does not raise


def test_validate_dataset_cases_rejects_duplicate_case_id():
    cases = [
        _make_case("F001"),
        _make_case("F001", feedback_type_label="feature_request"),
    ]
    with pytest.raises(DatasetValidationError) as exc_info:
        validate_dataset_cases(cases)
    assert any("duplicate case_id" in e for e in exc_info.value.errors)


def test_validate_dataset_cases_rejects_empty_case_id():
    cases = [_make_case("")]
    with pytest.raises(DatasetValidationError) as exc_info:
        validate_dataset_cases(cases)
    assert any("case_id" in e for e in exc_info.value.errors)


def test_validate_dataset_cases_rejects_illegal_severity():
    cases = [_make_case("F001", severity_label=0)]
    with pytest.raises(DatasetValidationError) as exc_info:
        validate_dataset_cases(cases)
    assert any("illegal severity_label" in e for e in exc_info.value.errors)


def test_validate_dataset_cases_rejects_non_integer_severity():
    cases = [_make_case("F001", severity_label="3")]  # type: ignore[arg-type]
    with pytest.raises(DatasetValidationError) as exc_info:
        validate_dataset_cases(cases)
    assert any("illegal severity_label" in e for e in exc_info.value.errors)


def test_validate_dataset_cases_rejects_illegal_feedback_type():
    cases = [_make_case("F001", feedback_type_label="not_a_type")]
    with pytest.raises(DatasetValidationError) as exc_info:
        validate_dataset_cases(cases)
    assert any("illegal feedback_type_label" in e for e in exc_info.value.errors)


def test_validate_dataset_cases_rejects_illegal_sentiment():
    cases = [_make_case("F001", sentiment_label="ecstatic")]
    with pytest.raises(DatasetValidationError) as exc_info:
        validate_dataset_cases(cases)
    assert any("illegal sentiment_label" in e for e in exc_info.value.errors)


def test_validate_dataset_cases_aggregates_multiple_errors():
    cases = [
        _make_case("F001", severity_label=99),
        _make_case("F001", feedback_type_label="bogus"),
    ]
    with pytest.raises(DatasetValidationError) as exc_info:
        validate_dataset_cases(cases)
    # Both errors should be surfaced, not just the first one.
    assert len(exc_info.value.errors) >= 2


# ---- Hash + version ----------------------------------------------------------


def test_hash_and_version_are_stable_across_loads():
    a = [_make_case("F001"), _make_case("F002", feedback_type_label="feature_request")]
    b = [_make_case("F001"), _make_case("F002", feedback_type_label="feature_request")]
    assert hash_dataset_cases(a) == hash_dataset_cases(b)
    assert build_dataset_version(a) == build_dataset_version(b)


def test_hash_changes_when_label_changes():
    base = [_make_case("F001", feedback_type_label="bug")]
    changed = [_make_case("F001", feedback_type_label="feature_request")]
    assert hash_dataset_cases(base) != hash_dataset_cases(changed)


def test_hash_changes_when_feedback_text_changes():
    """The content hash must fingerprint the raw text, not just the labels.

    Two cases with the same labels but different raw text are NOT the
    same dataset — the supervisor's regression comparison must refuse
    to treat them as comparable. The plaintext is never serialised;
    only its SHA-256 hex digest enters the canonical form.
    """
    a = [_make_case("F001", feedback_text="first version of the text")]
    b = [_make_case("F001", feedback_text="second version of the text")]
    assert hash_dataset_cases(a) != hash_dataset_cases(b)
    assert build_dataset_version(a) != build_dataset_version(b)


def test_hash_uses_text_hash_not_plaintext():
    """The canonical form must not include the raw text bytes.

    Even a tiny difference (one character) flips the hash, and the
    plaintext is never echoed back through the JSON form.
    """
    from app.evals.dataset import _canonicalise_cases

    a = _canonicalise_cases([_make_case("F001", feedback_text="hello world")])
    b = _canonicalise_cases([_make_case("F001", feedback_text="hello world!")])
    assert a != b
    assert "hello world" not in a
    assert "hello world!" not in b


def test_version_format_is_prefixed_truncated_hash():
    cases = [_make_case("F001")]
    version = build_dataset_version(cases)
    assert version.startswith("demo-")
    hex_part = version.split("-", 1)[1]
    assert len(hex_part) == 16
    int(hex_part, 16)  # parses as hex


# ---- Live demo CSV load ------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"


def _require_demo_data() -> Path:
    if not (DATA_DIR / "demo_feedback.csv").is_file():
        pytest.skip("data/demo_feedback.csv not present in the repo")
    if not (DATA_DIR / "golden_dataset.csv").is_file():
        pytest.skip("data/golden_dataset.csv not present in the repo")
    return DATA_DIR


def test_load_demo_dataset_returns_full_12_rows():
    data_dir = _require_demo_data()
    dataset = load_demo_dataset(data_dir)
    assert dataset.name == DATASET_NAME_DEMO
    assert dataset.provenance == DATASET_PROVENANCE_DEMO
    assert dataset.split == DATASET_SPLIT_FULL
    assert dataset.label_status == DATASET_LABEL_STATUS_SYNTHETIC
    # The brief: 12 cases, NO train/validation/test split.
    assert len(dataset.cases) == 12
    assert dataset.case_count() == 12


def test_load_demo_dataset_preserves_expected_labels():
    data_dir = _require_demo_data()
    dataset = load_demo_dataset(data_dir)
    f001 = dataset.cases_by_id["F001"]
    assert f001.feedback_type_label == "feature_request"
    assert f001.severity_label == 3
    assert f001.cluster_label == "batch_image_upload"
    f012 = dataset.cases_by_id["F012"]
    assert f012.feedback_type_label == "other"
    assert f012.severity_label == 1


def test_load_demo_dataset_content_hash_matches_manual_hash():
    data_dir = _require_demo_data()
    dataset = load_demo_dataset(data_dir)
    # The hash must be reproducible from the case list alone.
    assert dataset.content_hash == hash_dataset_cases(dataset.cases)
    assert dataset.version == build_dataset_version(dataset.cases)


def test_load_demo_dataset_hash_changes_with_data():
    """Mutating the CSV must change the dataset hash + version."""
    data_dir = _require_demo_data()
    original = load_demo_dataset(data_dir)
    # Read the golden CSV, mutate one row's severity in memory, recompute.
    modified_cases = []
    for case in original.cases:
        if case.case_id == "F001":
            modified_cases.append(
                DatasetCase(
                    case_id=case.case_id,
                    feedback_text=case.feedback_text,
                    stage=case.stage,
                    feedback_type_label=case.feedback_type_label,
                    module_label=case.module_label,
                    intent_label=case.intent_label,
                    cluster_label=case.cluster_label,
                    severity_label=5,  # was 3
                    sentiment_label=case.sentiment_label,
                )
            )
        else:
            modified_cases.append(case)
    assert hash_dataset_cases(modified_cases) != original.content_hash
    assert build_dataset_version(modified_cases) != original.version


def test_load_demo_dataset_is_stable_across_repeated_loads():
    data_dir = _require_demo_data()
    a = load_demo_dataset(data_dir)
    b = load_demo_dataset(data_dir)
    assert a.content_hash == b.content_hash
    assert a.version == b.version
    assert a.case_count() == b.case_count()


def test_load_demo_dataset_never_claims_human_review():
    """The dataset MUST be flagged as unreviewed; that's the safety net."""
    data_dir = _require_demo_data()
    dataset = load_demo_dataset(data_dir)
    # The label status must be explicit about being synthetic + unreviewed.
    assert "unreviewed" in dataset.label_status
    assert "synthetic" in dataset.label_status or "reference" in dataset.label_status


def test_load_demo_dataset_split_is_full():
    """The brief: don't force a 12-case train/val/test split."""
    data_dir = _require_demo_data()
    dataset = load_demo_dataset(data_dir)
    assert dataset.split == DATASET_SPLIT_FULL


def test_load_demo_dataset_records_source_paths():
    data_dir = _require_demo_data()
    dataset = load_demo_dataset(data_dir)
    assert any("demo_feedback.csv" in p for p in dataset.source_paths)
    assert any("golden_dataset.csv" in p for p in dataset.source_paths)


def test_load_demo_dataset_missing_files_raises(tmp_path):
    with pytest.raises(DatasetValidationError):
        load_demo_dataset(tmp_path)


def test_load_demo_dataset_duplicate_feedback_id_raises(tmp_path):
    """Duplicate feedback_id in the feedback CSV must be rejected."""
    (tmp_path / "demo_feedback.csv").write_text(
        "feedback_id,content,source,user_id,user_type,created_at\n"
        "F001,hello,app,U001,free,2026-10-01\n"
        "F001,world,app,U002,pro,2026-10-02\n",
        encoding="utf-8",
    )
    (tmp_path / "golden_dataset.csv").write_text(
        "feedback_id,gt_feedback_type,gt_module,gt_intent,gt_cluster,gt_severity\n"
        "F001,bug,image_upload,batch_image_upload,batch_image_upload,3\n",
        encoding="utf-8",
    )
    with pytest.raises(DatasetValidationError):
        load_demo_dataset(tmp_path)


def test_load_demo_dataset_orphan_golden_id_raises(tmp_path):
    """A golden row that has no matching feedback row must be rejected."""
    (tmp_path / "demo_feedback.csv").write_text(
        "feedback_id,content,source,user_id,user_type,created_at\n"
        "F001,hello,app,U001,free,2026-10-01\n",
        encoding="utf-8",
    )
    (tmp_path / "golden_dataset.csv").write_text(
        "feedback_id,gt_feedback_type,gt_module,gt_intent,gt_cluster,gt_severity\n"
        "F001,bug,image_upload,batch_image_upload,batch_image_upload,3\n"
        "F999,bug,image_upload,batch_image_upload,batch_image_upload,3\n",
        encoding="utf-8",
    )
    with pytest.raises(DatasetValidationError) as exc_info:
        load_demo_dataset(tmp_path)
    assert any("F999" in e for e in exc_info.value.errors)
