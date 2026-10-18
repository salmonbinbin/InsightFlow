"""Supervisor adversarial coverage cases: no network or database."""

import pytest

from app.services.research_contract import (
    build_evidence_catalog,
    build_evidence_unit,
    build_task_plan,
    evaluate_coverage,
)


def _unit(text, source="official-page"):
    return build_evidence_unit(
        snapshot=text,
        source_id=source,
        offset=0,
        length=len(text),
        section="Android manual upload",
        context="Google Photos Android",
    )


@pytest.mark.parametrize(
    "units",
    [
        [
            _unit("Manually upload with Google Photos Android."),
            _unit("Manually upload with Google Photos Android."),
        ],
        [
            _unit("Manually upload with Google Photos Android.", "a"),
            _unit("Manually upload with Google Photos Android.", "b"),
        ],
    ],
)
def test_duplicate_or_unrelated_sources_cannot_make_a_procedure_complete(units):
    plan = build_task_plan("What are the steps to manually upload photos in Google Photos Android?")
    assert not evaluate_coverage(plan, units).overall_completable


def test_unrelated_number_cannot_prove_multiselect():
    plan = build_task_plan("Can Google Photos Android manually select multiple photos for upload?")
    units = [_unit("Manually select photos and upload."), _unit("There are 3 storage settings.")]
    assert not evaluate_coverage(plan, units).overall_completable


def test_unparsed_offline_condition_must_block_completion():
    plan = build_task_plan(
        "What are the steps to manually upload photos in Google Photos Android while offline?"
    )
    units = [_unit("Manually upload with Google Photos Android."), _unit("Tap upload.")]
    assert not evaluate_coverage(plan, units).overall_completable


def test_requested_limits_are_core_requirements():
    plan = build_task_plan(
        "In the Dropbox iPhone app, can a user select and upload multiple photos at once? Cite only official Dropbox Help and state any limits actually documented."
    )
    requirements = [r for r in plan.items if r.kind == "limit"]
    assert requirements and all(r.role == "core" for r in requirements)


@pytest.mark.parametrize(
    "modifier", ["via satellite", "without internet", "using an administrator account"]
)
def test_unknown_modifier_is_not_dropped(modifier):
    plan = build_task_plan(f"What are the steps to upload photos to OneDrive on iOS {modifier}?")
    assert not plan.is_completable_target


def test_missing_middle_step_in_snapshot_stays_incomplete():
    catalog = build_evidence_catalog(
        "a", "# OneDrive iOS\n## Upload photos on iOS\n1. Open OneDrive.\n3. Upload photos.\n"
    )
    plan = build_task_plan("What are the steps to upload photos to OneDrive on iOS?")
    report = evaluate_coverage(
        plan,
        catalog.units,
        catalog=catalog,
        selected_evidence_ids=[u.evidence_id for u in catalog.units],
    )
    assert not report.overall_completable


def test_sibling_section_does_not_inherit_another_platform():
    catalog = build_evidence_catalog(
        "a", "# OneDrive\n## iOS\n1. Upload photos.\n## Android\n1. Upload photos.\n"
    )
    assert "iOS" not in catalog.units[-1].context


def test_blank_line_before_a_numbered_step_is_not_part_of_the_quote():
    body = "# OneDrive iOS\n## Upload photos on iOS\n\n1. Open OneDrive.\n2. Upload photos.\n"
    catalog = build_evidence_catalog("a", body)
    assert catalog.units[0].text == "1. Open OneDrive."
    assert (
        body[catalog.units[0].offset : catalog.units[0].offset + catalog.units[0].length]
        == catalog.units[0].text
    )
