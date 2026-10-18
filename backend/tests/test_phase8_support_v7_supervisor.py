"""Independent provenance/scope/coverage and monotonic support tests."""

import importlib
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import research_service as api
from app.services.research_selection import build_catalogs
from app.services.research_spec_v7 import SpecError, compile_spec, question_parts


def module():
    try:
        return importlib.import_module("app.services.research_support_v7")
    except ModuleNotFoundError:
        pytest.fail("v7 evidence support contract is not implemented")


def inventory(body, product="OneDrive"):
    rid, pid = uuid4(), uuid4()
    urls = {
        "OneDrive": "https://support.microsoft.com/en-us/onedrive/manually-upload-files-or-photos-to-onedrive-in-ios",
        "Dropbox": "https://help.dropbox.com/create-upload/upload-mobile",
        "iCloud": "https://support.apple.com/guide/icloud/upload-and-download-photos-mmb5dbaf376f/icloud",
    }
    s = SimpleNamespace(
        provider_source_id="s",
        url=urls[product],
        title=product,
        domain=urls[product].split("/")[2],
        snippet=body,
        run_id=rid,
    )
    cats, entries = build_catalogs([s])
    return rid, pid, cats, entries


def spec(q, kind="procedure", product="OneDrive", platform="iOS", **updates):
    r = dict(
        id="r1",
        anchor_ids=[a["id"] for a in question_parts(q)["anchors"]],
        kind=kind,
        products=[product],
        platform=platform,
        entry="app",
        action="upload",
        object="photos",
        mode="manual",
        quantity="unspecified",
        condition_quotes=[],
        version=None,
        analysis_facets=[],
    )
    r.update(updates)
    return compile_spec(q, {"requirements": [r], "queries": []})


def facts(m, ids, entries, rid, pid, full=True):
    return m.exact_facts(
        ids, entries, run_id=rid, project_id=pid, api=api, full_source_ids={"s"} if full else set()
    )


def test_truth_of_real_steps_is_independent_of_whole_question_quantity():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Manually upload photos\n1. Open OneDrive on your iPhone.\n2. Choose photos and tap Upload.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    assert all(f.status == "confirmed" for f in fs)
    s = spec(
        "Can OneDrive iPhone users manually upload multiple photos?",
        kind="capability",
        quantity="multiple",
    )
    report = m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )
    assert not report["overall_completable"]
    assert report["items"][0]["evidence_ids"]
    assert "quantity" in report["items"][0]["reason_codes"]


def test_explicit_quantity_in_same_flow_completes_without_demoting_ordinary_steps():
    m = module()
    body = "# OneDrive iOS\n## Manually upload photos\n1. Open OneDrive on your iPhone.\n2. Select multiple photos, then tap Upload.\n"
    rid, pid, cats, entries = inventory(body)
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec("How do OneDrive iPhone users manually upload multiple photos?", quantity="multiple")
    report = m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )
    assert report["overall_completable"], report


def test_wrong_function_and_explicit_windows_scope_never_become_mobile_support():
    m = module()
    body = "# Dropbox mobile\n## Upload photos\n1. Open the Dropbox mobile app.\n2. Choose photos and tap Upload.\nNote: The Dropbox app for Windows devices does not currently support video uploads. This is due to operating system limitations.\n## Create an animation\n1. Choose photos.\n2. Tap Create animation.\n"
    rid, pid, cats, entries = inventory(body, "Dropbox")
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec("Dropbox mobile manual photo upload steps?", product="Dropbox", platform="mobile")
    report = m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )
    kept = set(report["items"][0]["evidence_ids"])
    texts = [entries[i]["unit"].text for i in kept]
    assert not any("Windows" in t or "animation" in t or "operating system" in t for t in texts)
    assert report["rejected_ids"]
    assert not report["overall_completable"], (
        "a contaminated binding cannot pass by silently filtering it"
    )


def test_snapshot_and_run_guards_cannot_be_bypassed_by_valid_ids():
    m = module()
    rid, pid, cats, entries = inventory("# OneDrive iOS\n## Upload photos\n1. Open OneDrive.\n")
    ids = list(entries)
    assert all(f.status == "not_verified" for f in facts(m, ids, entries, rid, pid, full=False))
    with pytest.raises(ValueError):
        facts(m, ids, entries, uuid4(), pid)
    eid = ids[0]
    broken = dict(entries)
    broken[eid] = dict(entries[eid], unit=replace(entries[eid]["unit"], text="fabricated"))
    with pytest.raises(ValueError):
        facts(m, [eid], broken, rid, pid)


def test_version_is_proven_from_actual_note_not_from_digits_in_question():
    m = module()
    body = "# iCloud phone upload\n## Upload photos\nNote: To upload to a Shared Photo Library, use the Photos app with iOS 16.1 or later.\n"
    rid, pid, cats, entries = inventory(body, "iCloud")
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec(
        "Which version is required to upload photos to an iCloud Shared Photo Library?",
        kind="version",
        product="iCloud",
        platform="mobile",
        mode="unspecified",
    )
    report = m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )
    assert report["overall_completable"], report
    empty = m.evaluate_support(s, [{"requirement_id": "r1", "evidence_ids": []}], fs, entries, cats)
    assert not empty["overall_completable"]


def test_partial_retry_cannot_displace_later_answer_with_more_irrelevant_excerpts():
    m = module()
    s = spec("OneDrive iPhone upload choices?", kind="entry_listing")
    ledger = m.SupportLedger(s)

    def report(status, ids):
        return {
            "items": [
                {
                    "requirement_id": "r1",
                    "status": status,
                    "evidence_ids": ids,
                    "reason_codes": [],
                    "gap": "",
                }
            ],
            "overall_completable": status == "covered",
            "rejected_ids": [],
        }

    ledger.update(report("partial", [f"old{i}" for i in range(7)]), attempt=2)
    ledger.update(report("covered", ["phone-options", "shared-entry"]), attempt=3)
    ledger.update(report("partial", [f"noise{i}" for i in range(30)]), attempt=4)
    current = ledger.report()
    assert current["items"][0]["evidence_ids"] == ["phone-options", "shared-entry"]
    assert current["items"][0]["adopted_attempt"] == 3


def test_unknown_requirement_and_more_than_forty_final_ids_are_rejected():
    m = module()
    s = spec("OneDrive iPhone upload steps?")
    ledger = m.SupportLedger(s)
    with pytest.raises(SpecError):
        ledger.update(
            {"items": [{"requirement_id": "foreign", "status": "covered", "evidence_ids": []}]},
            attempt=1,
        )
    with pytest.raises(SpecError):
        ledger.update(
            {
                "items": [
                    {
                        "requirement_id": "r1",
                        "status": "covered",
                        "evidence_ids": [str(i) for i in range(41)],
                    }
                ]
            },
            attempt=1,
        )


def test_one_step_and_cross_section_splicing_cannot_complete_a_procedure():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Upload photos\n1. Open OneDrive on iPhone.\n2. Choose photos and tap Upload.\n## Move photos\n1. Select a photo.\n2. Tap Move.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec("OneDrive iPhone upload photos steps?")
    one = [next(i for i in ids if entries[i]["unit"].step_index == 1)]
    assert not m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": one}], fs, entries, cats
    )["overall_completable"]
    assert not m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )["overall_completable"]


def test_multiple_formats_is_not_a_multiple_photo_action():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Upload photos\n1. Open OneDrive on iPhone.\n2. Choose a photo and tap Upload.\nNote: You can upload multiple file formats including JPEG and PNG.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec(
        "Can OneDrive iPhone users upload multiple photos?", kind="capability", quantity="multiple"
    )
    report = m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )
    assert not report["overall_completable"]
    assert "quantity" in report["items"][0]["reason_codes"]


def test_unrelated_action_in_same_section_is_not_supported_by_its_heading():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Upload photos\nNote: To create an album on your iPhone, select multiple photos and tap Create.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec(
        "Can OneDrive iPhone users upload multiple photos?", kind="capability", quantity="multiple"
    )
    report = m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )
    assert not report["overall_completable"]
    assert not report["items"][0]["evidence_ids"]


def test_known_negative_is_truthful_but_never_a_positive_capability_answer():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Upload photos\nYou cannot select multiple photos for one upload on iPhone.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec(
        "Can OneDrive iPhone users upload multiple photos?", kind="capability", quantity="multiple"
    )
    report = m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )
    assert report["items"][0]["answer_polarity"] == "negative"
    assert all(f.status == "confirmed" for f in fs)


def test_platform_qualification_on_sibling_heading_overrides_parent():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive mobile\n## Upload photos from Windows\n1. Open OneDrive.\n2. Choose photos and tap Upload.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec("OneDrive mobile upload photos steps?", platform="mobile")
    report = m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )
    assert not report["items"][0]["evidence_ids"]


def test_fact_source_refs_and_type_are_checked_again_at_support_boundary():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Upload photos\n1. Open OneDrive on iPhone.\n2. Choose photos and tap Upload.\n"
    )
    ids = list(entries)
    fs = list(facts(m, ids, entries, rid, pid))
    s = spec("OneDrive iPhone upload photos steps?")
    fs[0] = replace(fs[0], source_refs=(("foreign", "https://example.com"),))
    with pytest.raises(SpecError):
        m.evaluate_support(s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats)


def test_neutral_steps_inherit_only_selected_same_section_platform():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive\n## Upload photos\n1. On iPhone open OneDrive.\n2. Choose photos and tap Upload.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec("OneDrive iPhone upload photos steps?")
    assert m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )["overall_completable"]


def test_manual_flow_cannot_be_used_as_automatic_backup_proof():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Upload photos\n1. Open OneDrive on iPhone.\n2. Choose photos and tap Upload.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec(
        "Does OneDrive iPhone automatically upload photos?", kind="capability", mode="automatic"
    )
    report = m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )
    assert not report["overall_completable"] and "mode" in report["items"][0]["reason_codes"]


def test_upload_audio_flow_cannot_answer_photo_upload_question():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Upload audio\n1. Open OneDrive on iPhone.\n2. Select audio and tap Upload.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec("OneDrive iPhone upload photos steps?")
    assert not m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )["overall_completable"]


def test_backup_status_flow_cannot_be_published_as_manual_backup_steps():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive Android\n## Check photo backup status\n1. Open OneDrive on Android.\n2. Open a photo and check its backup status.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec("OneDrive Android manual backup photos steps?", platform="Android", action="backup")
    report = m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )
    assert not report["items"][0]["evidence_ids"]


def test_disabled_feature_condition_is_not_a_request_to_disable_it():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Upload photos\nIf you don't have camera uploads enabled, you can manually upload photos.\n1. Open OneDrive on iPhone.\n2. Choose photos and tap Upload.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec(
        "Can OneDrive iPhone users upload photos when camera uploads are disabled?",
        kind="capability",
        condition_quotes=["when camera uploads are disabled"],
    )
    report = m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )
    assert report["overall_completable"], report


def test_explicit_requested_entry_must_exist_in_bound_source_branch():
    m = module()
    rid, pid, cats, entries = inventory(
        "# iCloud\n## Upload to iCloud Drive\nOn a phone or tablet, upload photos using Choose Files.\n",
        "iCloud",
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec(
        "Where to upload photos in the iCloud Photos app on a phone?",
        kind="destination",
        product="iCloud",
        platform="phone",
        entry="Photos app",
    )
    report = m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )
    assert not report["overall_completable"] and not report["items"][0]["evidence_ids"]


def test_later_opposite_verified_claim_is_a_conflict_not_silently_ignored():
    m = module()
    s = spec("Can OneDrive iPhone upload photos?", kind="capability")
    ledger = m.SupportLedger(s)

    def report(polarity, ids):
        return dict(
            items=[
                dict(
                    requirement_id="r1",
                    status="covered",
                    evidence_ids=ids,
                    reason_codes=[],
                    gap="",
                    answer_polarity=polarity,
                )
            ],
            rejected_ids=[],
            overall_completable=True,
        )

    ledger.update(report("positive", ["yes"]), attempt=2)
    ledger.update(report("negative", ["no"]), attempt=3)
    current = ledger.report()
    assert not current["overall_completable"]
    assert current["items"][0]["answer_polarity"] == "conflicting"
    assert current["items"][0]["conflicting_attempts"] == [2, 3]


def test_add_action_uses_its_exact_conditional_assertion_and_full_upload_flow():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Upload photos\nIf you don't have camera uploads enabled, you can manually add photos.\n1. Open OneDrive on iPhone.\n2. Choose photos and tap Upload.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec(
        "Can OneDrive iPhone manually add photos when camera uploads are disabled?",
        kind="capability",
        action="add",
        condition_quotes=["when camera uploads are disabled"],
    )
    assert m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )["overall_completable"]


def test_photo_or_video_object_does_not_require_the_word_or_in_official_note():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Backup photos and videos\nOneDrive automatically saves photos and videos when backup is on.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec(
        "Can OneDrive iPhone automatically backup one photo or video?",
        kind="capability",
        action="backup",
        object="photo or video",
        mode="automatic",
    )
    assert m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )["overall_completable"]


def test_upload_question_can_use_a_documented_manual_backup_flow_without_rewriting_it():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive Android\n## Manually back up photos\n1. Open OneDrive on Android.\n2. Choose photos and tap Back up now.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec("OneDrive Android manually upload photos steps?", platform="Android", action="upload")
    report = m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )
    assert report["overall_completable"], report
    assert any("Back up now" in f.description for f in fs)


def test_comparison_needs_complete_both_sides_not_two_source_names():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Upload photos\n1. Open OneDrive on iPhone.\n2. Choose photos and tap Upload.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec(
        "Compare OneDrive iPhone manual upload with automatic upload.",
        kind="comparison",
        mode="mixed",
    )
    report = m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )
    assert not report["overall_completable"]
    assert "comparison_sides" in report["items"][0]["reason_codes"]


def test_partial_group_can_improve_proved_predicates_without_quote_count_ranking():
    m = module()
    s = spec("OneDrive iPhone upload multiple photos steps?", quantity="multiple")
    ledger = m.SupportLedger(s)

    def report(ids, codes):
        return dict(
            items=[
                dict(
                    requirement_id="r1",
                    status="partial",
                    evidence_ids=ids,
                    reason_codes=codes,
                    gap="; ".join(codes),
                    answer_polarity="positive",
                )
            ],
            rejected_ids=[],
            overall_completable=False,
        )

    ledger.update(report(["old1", "old2", "old3"], ["quantity", "procedure"]), attempt=2)
    ledger.update(report(["complete-flow"], ["quantity"]), attempt=3)
    assert ledger.report()["items"][0]["evidence_ids"] == ["complete-flow"]
    assert ledger.report()["items"][0]["adopted_attempt"] == 3


def test_select_then_backup_is_one_transfer_flow_with_quantity_still_independent():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive Android\n## Manually back up photos\n1. Open OneDrive on Android.\n2. Select multiple photos, then tap Back up now.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec(
        "Can OneDrive Android select multiple photos and start backup?",
        kind="capability",
        platform="Android",
        action="select and backup",
        quantity="multiple",
    )
    assert m.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )["overall_completable"]


def test_comparison_uses_verified_side_relations_without_requiring_duplicate_id_bags():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\nOneDrive can automatically upload photos when backup is on.\n## Manually upload photos\n1. Open OneDrive on iPhone.\n2. Choose photos and tap Upload.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec("Compare OneDrive iPhone manual upload and automatic upload.", kind="procedure")
    s["requirements"] += [
        s["requirements"][0] | dict(id="auto", kind="capability", mode="automatic"),
        s["requirements"][0] | dict(id="cmp", kind="comparison", mode="mixed"),
    ]
    manual = [i for i in ids if entries[i]["unit"].step_index]
    auto = [i for i in ids if "automatically" in entries[i]["unit"].text]
    report = m.evaluate_support(
        s,
        [
            dict(requirement_id="r1", evidence_ids=manual),
            dict(requirement_id="auto", evidence_ids=auto),
        ],
        fs,
        entries,
        cats,
    )
    assert report["overall_completable"], report
    assert report["items"][2]["side_requirement_ids"] == ["r1", "auto"]
    assert set(report["items"][2]["evidence_ids"]) == set(ids)


@pytest.mark.parametrize(
    "condition,note",
    [
        (
            "when camera uploads are disabled",
            "When camera uploads are not disabled, you can manually upload photos.",
        ),
        ("while offline", "You can upload photos when you are not offline."),
    ],
)
def test_opposite_condition_cannot_pass_by_containing_the_same_words(condition, note):
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Upload photos\n"
        + note
        + "\n1. Open OneDrive on iPhone.\n2. Choose photos and tap Upload.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec(
        "Can OneDrive iPhone manually upload photos " + condition + "?",
        kind="capability",
        condition_quotes=[condition],
    )
    report = m.evaluate_support(s, [dict(requirement_id="r1", evidence_ids=ids)], fs, entries, cats)
    assert not report["overall_completable"]
    assert "condition" in report["items"][0]["reason_codes"]


def test_version_mention_alone_is_not_a_required_version_constraint():
    m = module()
    rid, pid, cats, entries = inventory(
        "# iCloud phone\n## Upload photos\nPhotos uploaded on iOS 18 are shown in the library.\n",
        "iCloud",
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec(
        "Which version is required to upload photos to iCloud on a phone?",
        kind="version",
        product="iCloud",
        platform="phone",
        mode="unspecified",
    )
    assert not m.evaluate_support(
        s, [dict(requirement_id="r1", evidence_ids=ids)], fs, entries, cats
    )["overall_completable"]


def test_conflicting_minimum_versions_remain_partial():
    m = module()
    rid, pid, cats, entries = inventory(
        "# iCloud phone\n## Upload photos\nTo upload photos use iOS 16.1 or later.\nTo upload photos use iOS 17 or later.\n",
        "iCloud",
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec(
        "Which version is required to upload photos to iCloud on a phone?",
        kind="version",
        product="iCloud",
        platform="phone",
        mode="unspecified",
    )
    report = m.evaluate_support(s, [dict(requirement_id="r1", evidence_ids=ids)], fs, entries, cats)
    assert (
        not report["overall_completable"]
        and "version_conflict" in report["items"][0]["reason_codes"]
    )


def test_explicit_input_version_cannot_be_ignored_or_proven_by_device_name():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Upload photos\n1. Open OneDrive on iPhone.\n2. Choose photos and tap Upload.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec(
        "Can OneDrive iPhone users upload photos on iOS 18?", kind="capability", version="iOS 18"
    )
    report = m.evaluate_support(s, [dict(requirement_id="r1", evidence_ids=ids)], fs, entries, cats)
    assert not report["overall_completable"] and "version" in report["items"][0]["reason_codes"]


def test_version_constraint_can_support_a_requested_later_version():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Upload photos\nTo upload photos, use iOS 16.1 or later.\n1. Open OneDrive on iPhone.\n2. Choose photos and tap Upload.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec(
        "Can OneDrive iPhone users upload photos on iOS 18?", kind="capability", version="iOS 18"
    )
    assert m.evaluate_support(s, [dict(requirement_id="r1", evidence_ids=ids)], fs, entries, cats)[
        "overall_completable"
    ]


def test_ledger_does_not_silently_keep_one_of_two_incompatible_version_groups():
    m = module()
    s = spec("Which OneDrive iPhone version supports upload photos?", kind="version")
    ledger = m.SupportLedger(s)

    def report(v, e):
        return dict(
            items=[
                dict(
                    requirement_id="r1",
                    status="covered",
                    evidence_ids=[e],
                    reason_codes=[],
                    gap="",
                    answer_polarity="positive",
                    version_constraints=[
                        dict(platform="ios", relation="at_least", version=v, evidence_ids=[e])
                    ],
                )
            ],
            rejected_ids=[],
            overall_completable=True,
        )

    ledger.update(report("16.1", "old"), attempt=2)
    ledger.update(report("17", "new"), attempt=3)
    assert not ledger.report()["overall_completable"]
    assert "version_conflict" in ledger.report()["items"][0]["reason_codes"]


def test_explicit_ipad_only_flow_cannot_borrow_generic_ios_for_iphone():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Upload photos on iPad\n1. Open OneDrive.\n2. Choose photos and tap Upload.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec("OneDrive iPhone upload photos steps?", platform="iOS")
    assert not m.evaluate_support(
        s, [dict(requirement_id="r1", evidence_ids=ids)], fs, entries, cats
    )["overall_completable"]


@pytest.mark.parametrize("opposite", [False, True])
def test_ledger_comparison_tracks_validity_of_its_verified_sides(opposite):
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\nOneDrive can automatically upload photos when backup is on.\nOneDrive cannot automatically upload photos on iPhone.\n## Manually upload photos\n1. Open OneDrive on iPhone.\n2. Choose photos and tap Upload.\n"
    )
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    s = spec("Compare OneDrive iPhone manual upload and automatic upload.", kind="procedure")
    s["requirements"] += [
        s["requirements"][0] | dict(id="auto", kind="capability", mode="automatic"),
        s["requirements"][0] | dict(id="cmp", kind="comparison", mode="mixed"),
    ]
    manual = [i for i in ids if entries[i]["unit"].step_index]
    pos = [i for i in ids if "can automatically upload" in entries[i]["unit"].text]
    neg = [i for i in ids if "cannot automatically upload" in entries[i]["unit"].text]
    initial = m.evaluate_support(
        s,
        [
            dict(requirement_id="r1", evidence_ids=manual),
            dict(requirement_id="auto", evidence_ids=pos),
        ],
        fs,
        entries,
        cats,
    )
    assert initial["overall_completable"], initial
    ledger = m.SupportLedger(s)
    ledger.update(initial, attempt=2)
    later = m.evaluate_support(
        s, [dict(requirement_id="auto", evidence_ids=neg if opposite else pos)], fs, entries, cats
    )
    ledger.update(later, attempt=3)
    current = ledger.report()
    rows = {i["requirement_id"]: i for i in current["items"]}
    assert rows["r1"]["status"] == "covered" and rows["r1"]["adopted_attempt"] == 2
    if opposite:
        assert rows["auto"]["answer_polarity"] == "conflicting"
        assert rows["cmp"]["status"] != "covered" and rows["cmp"]["adopted_attempt"] is None
        assert not current["overall_completable"]
    else:
        assert rows["cmp"]["status"] == "covered" and rows["cmp"]["adopted_attempt"] == 2
        assert current["overall_completable"]


def test_qualified_negative_note_does_not_deny_the_complete_general_upload_flow():
    m = module()
    body = "# OneDrive iOS\n## Upload photos\n1. Open OneDrive on iPhone.\n2. Select photos and tap Upload.\nNote: OneDrive cannot upload photos optimized for iCloud.\n"
    rid, pid, cats, entries = inventory(body)
    ids = list(entries)
    report = m.evaluate_support(
        spec("How does OneDrive iPhone upload photos?", kind="procedure"),
        [dict(requirement_id="r1", evidence_ids=ids)],
        facts(m, ids, entries, rid, pid),
        entries,
        cats,
    )
    assert report["overall_completable"], report
    assert report["items"][0]["answer_polarity"] == "positive"
    assert any("cannot" in entries[i]["unit"].text for i in report["items"][0]["evidence_ids"])


@pytest.mark.parametrize("kind", ["capability", "procedure"])
def test_only_subset_restriction_does_not_prove_general_upload(kind):
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Upload photos\nOneDrive cannot upload photos optimized for iCloud.\n"
    )
    ids = list(entries)
    report = m.evaluate_support(
        spec("Can OneDrive iPhone upload photos?", kind=kind),
        [dict(requirement_id="r1", evidence_ids=ids)],
        facts(m, ids, entries, rid, pid),
        entries,
        cats,
    )
    assert not report["overall_completable"], report
    assert report["items"][0]["answer_polarity"] == "unknown"


@pytest.mark.parametrize(
    "note",
    [
        "OneDrive cannot upload photos.",
        "OneDrive does not support uploading photos.",
    ],
)
def test_general_negative_still_conflicts_with_positive_flow(note):
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Upload photos\n1. Open OneDrive on iPhone.\n"
        "2. Select photos and tap Upload.\n" + note + "\n"
    )
    ids = list(entries)
    report = m.evaluate_support(
        spec("Can OneDrive iPhone upload photos?", kind="capability"),
        [dict(requirement_id="r1", evidence_ids=ids)],
        facts(m, ids, entries, rid, pid),
        entries,
        cats,
    )
    assert not report["overall_completable"], report
    assert report["items"][0]["answer_polarity"] == "conflicting"


def test_requested_subset_keeps_its_negative_answer():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Upload photos\nOneDrive cannot upload photos optimized for iCloud.\n"
    )
    ids = list(entries)
    report = m.evaluate_support(
        spec("Can OneDrive iPhone upload photos optimized for iCloud?", kind="capability"),
        [dict(requirement_id="r1", evidence_ids=ids)],
        facts(m, ids, entries, rid, pid),
        entries,
        cats,
    )
    assert report["items"][0]["answer_polarity"] == "negative", report
