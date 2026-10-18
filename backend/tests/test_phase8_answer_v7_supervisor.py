"""Analyses must reuse complete typed premises, not merely legal evidence IDs."""

import copy
import importlib

import pytest
from test_phase8_support_v7_supervisor import facts, inventory, spec

from app.services.research_spec_v7 import SpecError, validate
from app.services.research_support_v7 import evaluate_support


def module():
    try:
        return importlib.import_module("app.services.research_answer_v7")
    except ModuleNotFoundError:
        pytest.fail("typed v7 answer/analysis contract missing")


def case():
    from app.services import research_support_v7 as support

    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Upload photos\n1. Open OneDrive on iPhone.\n2. Choose photos and tap Upload.\n"
    )
    ids = list(entries)
    fs = facts(support, ids, entries, rid, pid)
    s = spec("Compare OneDrive iPhone upload photos in a travel scenario.", kind="procedure")
    report = evaluate_support(s, [{"requirement_id": "r1", "evidence_ids": ids}], fs, entries, cats)
    return s, report, fs, ids, entries


def candidate(ids, **changes):
    return (
        dict(
            requirement_id="a1",
            facet="opportunity",
            operator="proposed_test",
            premises=[{"requirement_id": "r1", "evidence_ids": ids}],
            scenario="travel scenario",
            proposal="Test the documented flow with travelers.",
        )
        | changes
    )


def analysis_spec(s, facets):
    s = copy.deepcopy(s)
    s["requirements"].append(
        s["requirements"][0] | {"id": "a1", "kind": "analysis", "analysis_facets": facets}
    )
    return s


def test_proposal_is_labelled_and_references_the_complete_same_run_flow():
    m = module()
    s, r, fs, ids, entries = case()
    s = analysis_spec(s, ["opportunity"])
    rows, issues = m.analyses(s, [candidate(ids)], r, fs, entries)
    assert not issues and len(rows) == 1
    assert rows[0].claim_kind == "recommendation" and rows[0].status == "not_verified"
    assert set(rows[0].supporting_finding_ids) == {f.id for f in fs}
    assert "建议" in rows[0].description and "待验证" in rows[0].description
    answer = m.build_answer(s, r, fs, rows)
    assert answer["version"] == "research-answer-v7"
    assert answer["items"][0]["quotes"][0]["text"] == fs[0].description
    assert answer["analyses"][0]["supporting_finding_ids"]


def test_legal_but_incomplete_premise_ids_and_uncovered_requirements_fail():
    m = module()
    s, r, fs, ids, entries = case()
    s = analysis_spec(s, ["opportunity"])
    rows, issues = m.analyses(s, [candidate(ids[:1])], r, fs, entries)
    assert not rows and issues[0]["code"] == "premise_group_incomplete"
    r["items"][0]["status"] = "partial"
    rows, issues = m.analyses(s, [candidate(ids)], r, fs, entries)
    assert not rows and issues[0]["code"] == "premise_not_covered"


def test_sample_pattern_requires_two_products_on_same_facet():
    m = module()
    s, r, fs, ids, entries = case()
    s = analysis_spec(s, ["market_pattern"])
    rows, issues = m.analyses(
        s,
        [candidate(ids, facet="market_pattern", operator="shared_pattern", proposal=None)],
        r,
        fs,
        entries,
    )
    assert not rows and issues[0]["code"] == "pattern_requires_two_products"


def test_scenario_wire_and_runtime_have_identical_200_character_boundary():
    m = module()
    for n in [199, 200]:
        c = candidate(["e"], scenario="x" * n)
        validate(m.SELECTION_SCHEMA, dict(evidence_ids=["e"], bindings=[], analyses=[c]))
    with pytest.raises(SpecError, match="schema_length"):
        validate(
            m.SELECTION_SCHEMA,
            dict(evidence_ids=["e"], bindings=[], analyses=[candidate(["e"], scenario="x" * 201)]),
        )
    with pytest.raises(SpecError, match="schema_extra"):
        validate(
            m.SELECTION_SCHEMA,
            dict(evidence_ids=[], bindings=[], analyses=[candidate([], status="confirmed")]),
        )


def test_model_cannot_invent_a_scenario_or_a_fact_title():
    m = module()
    s, r, fs, ids, entries = case()
    s = analysis_spec(s, ["opportunity"])
    rows, issues = m.analyses(
        s, [candidate(ids, scenario="unmentioned hospital emergency")], r, fs, entries
    )
    assert not rows and issues[0]["code"] == "scenario_not_in_input"
    bad = candidate(ids) | {"title": "All competitors support batch upload"}
    rows, issues = m.analyses(s, [bad], r, fs, entries)
    assert not rows and issues[0]["code"].startswith("schema_extra")


def test_two_real_complete_product_flows_support_all_five_requested_facets():
    from types import SimpleNamespace
    from uuid import uuid4

    from app.services import research_service as api
    from app.services import research_support_v7 as support
    from app.services.research_catalog_v7 import build_catalogs

    m = module()
    rid, pid = uuid4(), uuid4()
    sources = []
    for product, url in [
        (
            "OneDrive",
            "https://support.microsoft.com/en-us/onedrive/manually-upload-files-or-photos-to-onedrive-in-ios",
        ),
        ("Dropbox", "https://help.dropbox.com/create-upload/upload-mobile"),
    ]:
        sources.append(
            SimpleNamespace(
                run_id=rid,
                provider_source_id=product,
                url=url,
                title=product + " mobile",
                domain=url.split("/")[2],
                snippet=f"# {product} mobile\n## Upload photos\n1. Open {product} on a phone.\n2. Choose photos and tap Upload.\n"
                + ("3. Confirm the upload.\n" if product == "Dropbox" else ""),
            )
        )
    cats, entries = build_catalogs(sources)
    all_ids = list(entries)
    fs = support.exact_facts(
        all_ids,
        entries,
        run_id=rid,
        project_id=pid,
        api=api,
        full_source_ids={"OneDrive", "Dropbox"},
    )
    from test_phase8_spec_v7_supervisor import requirement

    from app.services.research_spec_v7 import compile_spec

    q = "Compare OneDrive and Dropbox mobile photo upload."
    r1 = requirement("q1") | dict(
        id="r1", products=["OneDrive"], platform="mobile", object="photos"
    )
    s = compile_spec(
        q, dict(requirements=[r1, r1 | dict(id="r2", products=["Dropbox"])], queries=[])
    )
    bindings = [
        dict(
            requirement_id=r["id"],
            evidence_ids=[i for i, e in entries.items() if e["unit"].source_id == r["products"][0]],
        )
        for r in s["requirements"]
    ]
    r = evaluate_support(s, bindings, fs, entries, cats)
    assert r["overall_completable"], r
    facets = ["market_pattern", "strength", "weakness", "opportunity", "risk"]
    s = analysis_spec(s, facets)
    s["requirements"][-1]["products"] = ["OneDrive", "Dropbox"]
    cs = [
        dict(
            requirement_id="a1",
            facet=f,
            operator="shared_pattern"
            if f == "market_pattern"
            else "proposed_test"
            if f in {"opportunity", "risk"}
            else "scenario_tradeoff",
            premises=bindings,
            scenario="mobile photo upload",
            proposal="Test the two documented entry points."
            if f in {"opportunity", "risk"}
            else None,
        )
        for f in facets
    ]
    rows, issues = m.analyses(s, cs, r, fs, entries)
    assert len(rows) == 5 and not issues
    by_facet = {row.evidence_location["facet"]: row for row in rows}
    assert by_facet["strength"].description != by_facet["weakness"].description
    import re

    for facet in ["strength", "weakness"]:
        text = by_facet[facet].description
        assert re.search(r"onedrive\s*[：:]\s*2", text, re.I), text
        assert re.search(r"dropbox\s*[：:]\s*3", text, re.I), text
        assert "差额" in text and "1" in text
    completed = m.complete_analysis_items(s, r, rows)
    assert completed["overall_completable"]
    # A missing actual premise invalidates the derivation even with all IDs legal.
    r["items"][1]["status"] = "partial"
    assert not m.analyses(s, cs, r, fs, entries)[0]


@pytest.mark.parametrize("facet", ["strength", "weakness", "risk"])
def test_facet_label_alone_cannot_turn_a_contrast_into_an_analysis(facet):
    m = module()
    s, r, fs, ids, entries = case()
    s = analysis_spec(s, [facet])
    second = s["requirements"][0] | dict(id="r2")
    s["requirements"].append(second)
    r["items"].append(r["items"][0] | dict(requirement_id="r2"))
    c = candidate(ids, facet=facet, operator="contrast", proposal=None)
    c["premises"].append(dict(requirement_id="r2", evidence_ids=ids))
    rows, issues = m.analyses(s, [c], r, fs, entries)
    assert not rows and issues
    assert not m.complete_analysis_items(s, r, rows)["overall_completable"]


def test_empty_premise_ids_reference_the_whole_server_verified_group():
    m = module()
    s, r, fs, ids, entries = case()
    s = analysis_spec(s, ["opportunity"])
    rows, issues = m.analyses(s, [candidate([])], r, fs, entries)
    assert not issues and len(rows) == 1
    assert set(rows[0].supporting_finding_ids) == {f.id for f in fs}
    assert rows[0].evidence_location["premises"][0]["evidence_ids"] == r["items"][0]["evidence_ids"]


def test_equal_documented_steps_do_not_prove_strength_or_weakness():
    m = module()
    s, r, fs, ids, entries = case()
    s = analysis_spec(s, ["strength", "weakness"])
    s["requirements"].append(s["requirements"][0] | dict(id="r2"))
    r["items"].append(r["items"][0] | dict(requirement_id="r2"))
    cs = []
    for facet in ["strength", "weakness"]:
        c = candidate(ids, facet=facet, operator="scenario_tradeoff", proposal=None)
        c["premises"].append(dict(requirement_id="r2", evidence_ids=ids))
        cs.append(c)
    rows, issues = m.analyses(s, cs, r, fs, entries)
    assert not rows and len(issues) == 2


def different_side_case(second_action="delete", second_object="files"):
    from types import SimpleNamespace
    from uuid import uuid4

    from test_phase8_spec_v7_supervisor import requirement

    from app.services import research_service as api
    from app.services import research_support_v7 as support
    from app.services.research_catalog_v7 import build_catalogs
    from app.services.research_spec_v7 import compile_spec

    rid, pid = uuid4(), uuid4()
    sources = []
    for product, url, action, obj in [
        (
            "OneDrive",
            "https://support.microsoft.com/en-us/onedrive/manually-upload-files-or-photos-to-onedrive-in-ios",
            "upload",
            "photos",
        ),
        (
            "Dropbox",
            "https://help.dropbox.com/create-upload/upload-mobile",
            second_action,
            second_object,
        ),
    ]:
        sources.append(
            SimpleNamespace(
                run_id=rid,
                provider_source_id=product,
                url=url,
                title=product,
                domain=url.split("/")[2],
                snippet=f"# {product} mobile\n## Manually {action} {obj}\n1. Open {product} on your phone.\n2. Select {obj} and tap {action}.\n",
            )
        )
    cats, entries = build_catalogs(sources)
    ids = list(entries)
    fs = support.exact_facts(
        ids, entries, run_id=rid, project_id=pid, api=api, full_source_ids=set(cats)
    )
    base = requirement("q1") | dict(products=["OneDrive"], platform="mobile", object="photos")
    s = compile_spec(
        f"Compare OneDrive mobile upload photos and Dropbox mobile {second_action} {second_object}.",
        dict(
            requirements=[
                base,
                base
                | dict(id="r2", products=["Dropbox"], action=second_action, object=second_object),
                base
                | dict(
                    id="c1",
                    kind="comparison",
                    products=["OneDrive", "Dropbox"],
                    action=None,
                    object=None,
                    mode="unspecified",
                ),
            ],
            queries=[],
        ),
    )
    bindings = [
        dict(
            requirement_id=r["id"],
            evidence_ids=[i for i, e in entries.items() if e["unit"].source_id == r["products"][0]],
        )
        for r in s["requirements"]
        if r["kind"] != "comparison"
    ]
    report = support.evaluate_support(s, bindings, fs, entries, cats)
    assert report["overall_completable"], report
    return s, report, fs, entries


def test_output_comparison_is_not_an_atomic_shared_pattern_premise():
    s, r, fs, entries = different_side_case()
    s = analysis_spec(s, ["market_pattern"])
    s["requirements"][-1]["products"] = ["OneDrive", "Dropbox"]
    group = next(i["evidence_ids"] for i in r["items"] if i["requirement_id"] == "c1")
    c = candidate(
        group, facet="market_pattern", operator="shared_pattern", proposal=None, scenario=None
    )
    c["premises"] = [dict(requirement_id="c1", evidence_ids=group)]
    rows, issues = module().analyses(s, [c], r, fs, entries)
    assert not rows and issues


def test_covered_other_product_cannot_be_borrowed_for_explicit_analysis_request():
    s, r, fs, entries = different_side_case()
    s = analysis_spec(s, ["opportunity"])
    group = next(i["evidence_ids"] for i in r["items"] if i["requirement_id"] == "r2")
    c = candidate(group, scenario="mobile upload photos")
    c["premises"] = [dict(requirement_id="r2", evidence_ids=group)]
    rows, issues = module().analyses(s, [c], r, fs, entries)
    assert not rows and issues


def test_complete_long_flow_premises_are_linked_without_copying_all_quotes_again():
    from types import SimpleNamespace
    from uuid import uuid4

    from test_phase8_spec_v7_supervisor import requirement

    from app.services import research_service as api
    from app.services import research_support_v7 as support
    from app.services.research_catalog_v7 import build_catalogs
    from app.services.research_spec_v7 import compile_spec

    rid, pid = uuid4(), uuid4()
    sources = []
    for product, url in [
        (
            "OneDrive",
            "https://support.microsoft.com/en-us/onedrive/manually-upload-files-or-photos-to-onedrive-in-ios",
        ),
        ("Dropbox", "https://help.dropbox.com/create-upload/upload-mobile"),
    ]:
        body = f"# {product} mobile\n## Manually upload photos\n" + "".join(
            f"{i}. Select photos and tap Upload on your phone. "
            + "Keep the selected items visible. " * 5
            + "\n"
            for i in range(1, 9)
        )
        sources.append(
            SimpleNamespace(
                run_id=rid,
                provider_source_id=product,
                url=url,
                title=product,
                domain=url.split("/")[2],
                snippet=body,
            )
        )
    cats, entries = build_catalogs(sources)
    ids = list(entries)
    assert len(ids) <= 40
    fs = support.exact_facts(
        ids, entries, run_id=rid, project_id=pid, api=api, full_source_ids=set(cats)
    )
    base = requirement("q1") | dict(products=["OneDrive"], platform="mobile", object="photos")
    s = compile_spec(
        "Compare OneDrive and Dropbox mobile manual upload photos.",
        dict(requirements=[base, base | dict(id="r2", products=["Dropbox"])], queries=[]),
    )
    bs = [
        dict(
            requirement_id=r["id"],
            evidence_ids=[i for i, e in entries.items() if e["unit"].source_id == r["products"][0]],
        )
        for r in s["requirements"]
    ]
    report = support.evaluate_support(s, bs, fs, entries, cats)
    assert report["overall_completable"], report
    s = analysis_spec(s, ["market_pattern"])
    s["requirements"][-1]["products"] = ["OneDrive", "Dropbox"]
    c = candidate(
        [], facet="market_pattern", operator="shared_pattern", proposal=None, scenario=None
    )
    c["premises"] = bs
    rows, issues = module().analyses(s, [c], report, fs, entries)
    assert len(rows) == 1 and not issues, issues
    assert len(rows[0].description) <= 3000
    assert set(rows[0].supporting_finding_ids) == {f.id for f in fs}


def test_analysis_does_not_mutate_original_model_candidate():
    m = module()
    s, r, fs, ids, entries = case()
    s = analysis_spec(s, ["opportunity"])
    c = candidate([])
    before = copy.deepcopy(c)
    rows, issues = m.analyses(s, [c], r, fs, entries)
    assert rows and not issues
    assert c == before


@pytest.mark.parametrize("facet", ["strength", "weakness"])
def test_two_distinct_products_with_equal_steps_do_not_generate_preference(facet):
    s, r, fs, entries = different_side_case("upload", "photos")
    s = analysis_spec(s, [facet])
    s["requirements"][-1]["products"] = ["OneDrive", "Dropbox"]
    bs = [
        dict(requirement_id=i["requirement_id"], evidence_ids=i["evidence_ids"])
        for i in r["items"]
        if i["requirement_id"] != "c1"
    ]
    c = candidate(
        [],
        facet=facet,
        operator="scenario_tradeoff",
        proposal=None,
        scenario="mobile upload photos",
    )
    c["premises"] = bs
    rows, issues = module().analyses(s, [c], r, fs, entries)
    assert not rows and issues


def test_same_product_wrong_operation_does_not_support_explicit_analysis_scope():
    from test_phase8_spec_v7_supervisor import requirement

    from app.services import research_spec_v7 as specs
    from app.services import research_support_v7 as support

    body = "# OneDrive iOS\n## Manually upload photos\n1. Open OneDrive on iPhone.\n2. Choose photos and tap Upload.\n## Manually delete files\n1. Open OneDrive on iPhone.\n2. Choose files and tap Delete.\n"
    rid, pid, cats, entries = inventory(body)
    base = requirement("q1") | dict(products=["OneDrive"], platform="iPhone", object="photos")
    s = specs.compile_spec(
        "Can OneDrive iPhone upload photos and delete files?",
        dict(
            requirements=[base, base | dict(id="r2", action="delete", object="files")], queries=[]
        ),
    )
    bs = [
        dict(
            requirement_id=r["id"],
            evidence_ids=[
                i
                for i, e in entries.items()
                if ("upload" if r["id"] == "r1" else "delete") in e["unit"].context.casefold()
            ],
        )
        for r in s["requirements"]
    ]
    fs = facts(support, list(entries), entries, rid, pid)
    report = support.evaluate_support(s, bs, fs, entries, cats)
    assert report["overall_completable"], report
    s = analysis_spec(s, ["opportunity"])
    c = candidate(bs[1]["evidence_ids"], scenario="upload photos")
    c["premises"] = [bs[1]]
    rows, issues = module().analyses(s, [c], report, fs, entries)
    assert not rows and issues
