import csv
import importlib.util
from pathlib import Path

import pytest

from app.services.file_parser import parse_csv


def test_dataset_is_reproducible_importable_and_never_claims_review(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "builder", Path(__file__).parents[1] / "scripts/build_review_dataset.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    a, b = tmp_path / "a", tmp_path / "b"
    manifest = module.build(a)
    assert module.build(b) == manifest
    rows = parse_csv((a / "feedback.csv").read_bytes())
    assert len(rows) == 300 and len({r["content"] for r in rows}) == 300
    with (a / "review-100.csv").open(encoding="utf-8-sig") as f:
        review = list(csv.DictReader(f))
    assert len(review) == 100
    assert all(
        r["review_status"] == "pending"
        and r["reviewer"] == r["feedback_type"] == r["severity"] == r["cluster_label"] == ""
        for r in review
    )
    with (a / "groups.csv").open(encoding="utf-8-sig") as f:
        groups = list(csv.DictReader(f))
    splits = {}
    for r in groups:
        splits.setdefault(r["design_group"], set()).add(r["split"])
    assert all(len(s) == 1 for s in splits.values())
    assert manifest["provenance"] == "synthetic" and manifest["label_status"] == "unreviewed"
    with pytest.raises(FileExistsError):
        module.build(a)
