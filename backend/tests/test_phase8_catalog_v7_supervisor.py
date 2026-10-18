"""Long official lines remain exact and cannot lose a condition mid-span."""

import importlib

import pytest
from test_phase8_support_v7_supervisor import facts, inventory, spec


def module():
    try:
        return importlib.import_module("app.services.research_catalog_v7")
    except ModuleNotFoundError:
        pytest.fail("bounded v7 catalog is missing")


def test_long_step_fragments_preserve_every_original_character():
    m = module()
    long = (
        "2. Choose the existing photos on your iPhone "
        + ("using the picker " * 15)
        + "and tap Upload only after reviewing the selection."
    )
    body = "# OneDrive iOS\n## Upload photos\n1. Open OneDrive.\n" + long + "\n"
    rid, pid, _, entries = inventory(body)
    source = next(iter(entries.values()))["source"]
    cats, entries = m.build_catalogs([source])
    assert all(len(e["unit"].text) <= 200 for e in entries.values())
    spans = [e["unit"] for e in entries.values() if e["unit"].step_index == 2]
    assert len(spans) >= 2
    assert "".join(u.text for u in sorted(spans, key=lambda u: u.offset)) == long
    from app.services import research_support_v7 as support

    ids = list(entries)
    fs = facts(support, ids, entries, rid, pid)
    s = spec("OneDrive iPhone upload photos steps?")
    assert support.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )["overall_completable"]
    missing = [i for i in ids if i != spans[1].evidence_id]
    assert not support.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": missing}], fs, entries, cats
    )["overall_completable"]


def test_long_condition_is_not_proven_by_its_first_fragment():
    m = module()
    body = (
        "# OneDrive iOS\n## Upload photos\nNote: "
        + ("The picker is available on iPhone. " * 8)
        + "If camera uploads are disabled, you can still manually upload photos.\n"
    )
    rid, pid, _, old = inventory(body)
    source = next(iter(old.values()))["source"]
    _, entries = m.build_catalogs([source])
    assert any("disabled" in e["unit"].text for e in entries.values())
    assert all(
        source.snippet[e["unit"].offset : e["unit"].offset + e["unit"].length] == e["unit"].text
        for e in entries.values()
    )


def test_inline_computer_branch_cannot_borrow_following_mobile_tip_scope():
    m = module()
    body = "# iCloud\n## Upload photos\n- On a computer: Select photos and click Upload.Tip: On a phone or tablet, after upload tap More.\n"
    rid, pid, _, old = inventory(body, "iCloud")
    source = next(iter(old.values()))["source"]
    cats, entries = m.build_catalogs([source])
    from app.services import research_support_v7 as support

    ids = list(entries)
    fs = facts(support, ids, entries, rid, pid)
    s = spec(
        "iCloud phone photo upload choices?",
        kind="entry_listing",
        product="iCloud",
        platform="phone",
    )
    report = support.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats
    )
    assert not any(
        "On a computer" in entries[i]["unit"].text for i in report["items"][0]["evidence_ids"]
    )


def test_partial_long_assertion_cannot_drop_its_final_negation_or_condition():
    m = module()
    body = (
        "# OneDrive iOS\n## Upload photos\nYou can select multiple photos and upload "
        + ("from the picker " * 15)
        + "only if the administrator enabled this feature.\nSource note.\n"
    )
    rid, pid, _, old = inventory(body)
    source = next(iter(old.values()))["source"]
    cats, entries = m.build_catalogs([source])
    from app.services import research_support_v7 as support

    ids = list(entries)
    fs = facts(support, ids, entries, rid, pid)
    s = spec("Can OneDrive iPhone upload multiple photos?", kind="capability", quantity="multiple")
    report = support.evaluate_support(
        s, [{"requirement_id": "r1", "evidence_ids": ids[:1]}], fs, entries, cats
    )
    assert not report["overall_completable"] and not report["items"][0]["evidence_ids"]
