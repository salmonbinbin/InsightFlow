"""Repeated lists within one heading retain distinct workflow identities."""

import pytest
from test_phase8_support_v7_supervisor import facts, inventory, module, spec

from app.services.research_catalog_v7 import build_catalogs
from app.services.research_pipeline_v7 import _selection_closure


def setup_case(extra_step=False):
    body = "# OneDrive iOS\n## Manually upload photos\nMethod A:\n1. Open OneDrive on your iPhone and choose Personal.\n2. Select photos, then tap Upload in Personal.\n\nMethod B:\n1. Open OneDrive on your iPhone and choose Work.\n2. Select photos, then tap Upload in Work.\n"
    if extra_step:
        body += "3. Confirm the upload in Work.\n"
    rid, pid, cats, entries = inventory(body)
    cats, entries = build_catalogs([next(iter(entries.values()))["source"]])
    groups = {
        name: [i for i, e in entries.items() if e["unit"].step_index and name in e["unit"].text]
        for name in ["Personal", "Work"]
    }
    s = spec("How does OneDrive iPhone manually upload photos?")
    return rid, pid, cats, entries, groups, s


@pytest.mark.parametrize("name", ["Personal", "Work"])
def test_each_restarted_list_is_a_complete_legal_flow(name):
    rid, pid, cats, entries, groups, s = setup_case(extra_step=True)
    ids = groups[name]
    report = module().evaluate_support(
        s,
        [dict(requirement_id="r1", evidence_ids=ids)],
        facts(module(), ids, entries, rid, pid),
        entries,
        cats,
    )
    assert report["overall_completable"], report


def test_steps_from_two_restarted_lists_cannot_form_one_complete_flow():
    rid, pid, cats, entries, groups, s = setup_case()
    ids = [groups["Personal"][0], groups["Work"][1]]
    report = module().evaluate_support(
        s,
        [dict(requirement_id="r1", evidence_ids=ids)],
        facts(module(), ids, entries, rid, pid),
        entries,
        cats,
    )
    assert not report["overall_completable"], report


@pytest.mark.parametrize("name", ["Personal", "Work"])
def test_closure_does_not_expand_correct_flow_into_sibling_list(name):
    rid, pid, cats, entries, groups, s = setup_case()
    payload = dict(
        evidence_ids=groups[name],
        bindings=[dict(requirement_id="r1", evidence_ids=groups[name])],
        analyses=[],
    )
    closed, meta = _selection_closure(s, payload, entries)
    assert set(closed["evidence_ids"]) == set(groups[name]), meta
    report = module().evaluate_support(
        s,
        closed["bindings"],
        facts(module(), closed["evidence_ids"], entries, rid, pid),
        entries,
        cats,
    )
    assert report["overall_completable"], report


def test_offered_partial_sibling_lists_cannot_be_closed_as_one_flow():
    rid, pid, cats, entries, groups, s = setup_case()
    ids = [groups["Personal"][0], groups["Work"][1]]
    offered = {i: entries[i] for i in ids}
    payload = dict(
        evidence_ids=ids, bindings=[dict(requirement_id="r1", evidence_ids=ids)], analyses=[]
    )
    closed, _ = _selection_closure(s, payload, offered)
    report = module().evaluate_support(
        s, closed["bindings"], facts(module(), ids, entries, rid, pid), entries, cats
    )
    assert not report["overall_completable"], report


def test_number_restart_without_method_label_still_identifies_distinct_flows():
    rid, pid, _, old, _, s = setup_case()
    source = next(iter(old.values()))["source"]
    source.snippet = source.snippet.replace("Method A:\n", "").replace("Method B:\n", "")
    cats, entries = build_catalogs([source])
    for name in ["Personal", "Work"]:
        ids = [i for i, e in entries.items() if e["unit"].step_index and name in e["unit"].text]
        report = module().evaluate_support(
            s,
            [dict(requirement_id="r1", evidence_ids=ids)],
            facts(module(), ids, entries, rid, pid),
            entries,
            cats,
        )
        assert report["overall_completable"], report


def test_intervening_note_does_not_break_a_continuing_numbered_flow():
    body = "# OneDrive iOS\n## Manually upload photos\n1. Open OneDrive on your iPhone.\nNote: Keep the app open.\n2. Select photos, then tap Upload.\n"
    rid, pid, _, old = inventory(body)
    cats, entries = build_catalogs([next(iter(old.values()))["source"]])
    ids = list(entries)
    s = spec("How does OneDrive iPhone manually upload photos?")
    report = module().evaluate_support(
        s,
        [dict(requirement_id="r1", evidence_ids=ids)],
        facts(module(), ids, entries, rid, pid),
        entries,
        cats,
    )
    assert report["overall_completable"], report


def test_flow_context_is_not_borrowed_from_a_later_section():
    body = "# OneDrive iOS\n## Manually upload photos\nKeep the app open before uploading.\n1. Open OneDrive on your iPhone.\n2. Select photos, then tap Upload.\n## Delete photos\n1. Open the recycle bin.\n2. Select a photo and delete it.\n"
    _, _, _, old = inventory(body)
    _, entries = build_catalogs([next(iter(old.values()))["source"]])
    pre = next(e for e in entries.values() if "Keep the app open" in e["unit"].text)
    upload = next(e for e in entries.values() if "Open OneDrive" in e["unit"].text)
    assert pre["flow_id"] == upload["flow_id"] is not None


def test_ambiguous_between_flow_note_does_not_inherit_previous_flow():
    body = "# OneDrive iOS\n## Manually upload photos\n1. Open OneDrive on your iPhone.\n2. Select photos, then tap Upload.\nNote: A work account may require authorization.\n1. Open the Work account.\n2. Select photos, then tap Upload in Work.\n"
    _, _, _, old = inventory(body)
    _, entries = build_catalogs([next(iter(old.values()))["source"]])
    note = next(e for e in entries.values() if "authorization" in e["unit"].text)
    assert note["flow_id"] is None
