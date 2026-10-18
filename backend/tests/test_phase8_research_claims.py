"""Exact structured quotes and scoped fact-to-analysis relations."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import research_service as api
from app.services.research_selection import build_catalogs

try:
    from app.services import research_claims as claims
except ImportError:
    claims = None


def source(body, sid="s1", url="https://help.dropbox.com/create-upload/upload-mobile", run=None):
    return SimpleNamespace(
        provider_source_id=sid,
        title="Dropbox mobile upload",
        domain="help.dropbox.com",
        snippet=body,
        url=url,
        run_id=run,
    )


def setup(
    body='# Dropbox mobile\n## Upload photos\n3. Tap the "+" (plus sign) at the bottom of your screen.\n',
):
    run, project = uuid4(), uuid4()
    s = source(body, run=run)
    _, entries = build_catalogs([s])
    return run, project, s, entries


def facts_for(
    entries, run, project, question="Dropbox mobile upload; cite only official Dropbox Help."
):
    assert claims is not None, "structured quote/analysis module is missing"
    return claims.materialize_facts(
        list(entries), entries, research_question=question, run_id=run, project_id=project, api=api
    )


def analysis(ids, category="opportunity"):
    return {
        "category": category,
        "claim_kind": "recommendation" if category == "opportunity" else "inference",
        "title": "让手动上传入口更易发现",
        "description": "可以评估把手动上传入口放在更易发现的位置，并通过可用性测试验证。",
        "scenario": "手机用户主动选择照片上传",
        "supporting_evidence_ids": ids,
    }


def test_internal_quote_is_an_exact_structured_fact_without_weakening_legacy():
    run, project, s, entries = setup()
    facts = facts_for(entries, run, project)
    f = next(f for f in facts if '"+"' in f.description)
    assert f.status == "confirmed" and f.claim_kind == "fact"
    assert (
        f.description
        == s.snippet[
            f.evidence_location["offset"] : f.evidence_location["offset"]
            + f.evidence_location["length"]
        ]
    )
    assert f.evidence_location["text"] == f.description
    assert not api._is_description_grounded_in_snippet(
        'Evidence: "' + f.description + '"',
        ((s.provider_source_id, s.url),),
        {s.provider_source_id: s.snippet},
        {s.provider_source_id: s.url},
    )


def test_changed_snapshot_cannot_be_confirmed():
    run, project, s, entries = setup()
    s.snippet = s.snippet.replace("plus sign", "minus sign")
    with pytest.raises(ValueError):
        facts_for(entries, run, project)


def test_wrong_run_source_cannot_be_materialized():
    run, project, s, entries = setup()
    s.run_id = uuid4()
    with pytest.raises(ValueError):
        facts_for(entries, run, project)


def test_generic_plural_remains_unverified_for_explicit_multi_select():
    run, project, _, entries = setup(
        "# Dropbox mobile\n## Upload photos\n1. Tap the photos you would like to upload.\n"
    )
    facts = facts_for(
        entries,
        run,
        project,
        "In Dropbox mobile, select multiple photos; cite only official Dropbox Help.",
    )
    assert facts and all(f.status == "not_verified" for f in facts)
    assert all(not f.source_refs for f in facts)


def test_computer_fact_is_not_confirmed_for_phone_question():
    run, project, _, entries = setup(
        "# Dropbox\n## Upload\nOn a computer, upload the photos from your computer.\n"
    )
    facts = facts_for(
        entries,
        run,
        project,
        "In Dropbox iPhone app, how to upload photos? Cite only official Dropbox Help.",
    )
    assert facts and all(f.status != "confirmed" for f in facts)


def test_unknown_evidence_id_cannot_be_materialized():
    run, project, _, entries = setup()
    assert claims is not None
    with pytest.raises(ValueError):
        claims.materialize_facts(
            ["invented"],
            entries,
            research_question="Dropbox",
            run_id=run,
            project_id=project,
            api=api,
        )


def test_opportunity_and_risk_have_real_support_without_becoming_confirmed():
    run, project, _, entries = setup()
    facts = facts_for(entries, run, project)
    assert claims is not None
    result = claims.materialize_analyses(
        [analysis([facts[0].evidence_id]), analysis([facts[0].evidence_id], "risk")],
        facts,
        run_id=run,
        project_id=project,
    )
    assert {x.category for x in result} == {"opportunity", "risk"}
    assert all(x.status == "not_verified" and not x.source_refs for x in result)
    assert all(x.supporting_finding_ids == (facts[0].id,) for x in result)


@pytest.mark.parametrize("bad_scope", ["run", "project", "unverified"])
def test_foreign_or_unverified_facts_are_rejected(bad_scope):
    run, project, _, entries = setup()
    facts = facts_for(entries, run, project)
    f = facts[0]
    if bad_scope == "run":
        f = replace(f, run_id=uuid4())
    if bad_scope == "project":
        f = replace(f, project_id=uuid4())
    if bad_scope == "unverified":
        f = replace(f, status="not_verified", source_refs=())
    with pytest.raises(ValueError):
        claims.materialize_analyses(
            [analysis([f.evidence_id])], [f], run_id=run, project_id=project
        )


def test_unknown_support_and_analysis_self_support_are_rejected():
    run, project, _, entries = setup()
    facts = facts_for(entries, run, project)
    with pytest.raises(ValueError):
        claims.materialize_analyses(
            [analysis(["not-a-fact"])], facts, run_id=run, project_id=project
        )


def test_single_product_cannot_be_a_market_pattern():
    run, project, _, entries = setup()
    facts = facts_for(entries, run, project)
    with pytest.raises(ValueError):
        claims.materialize_analyses(
            [analysis([facts[0].evidence_id], "market_pattern")],
            facts,
            run_id=run,
            project_id=project,
        )


def test_two_distinct_products_can_support_a_labeled_sample_pattern():
    run, project, _, entries = setup()
    google = source(
        "# Google Photos Android\n## Manually back up\n1. Open the Google Photos app.\n",
        sid="g",
        url="https://support.google.com/photos/answer/6193313",
        run=run,
    )
    google.title = "Google Photos Android"
    google.domain = "support.google.com"
    _, more = build_catalogs([google])
    entries.update(more)
    facts = facts_for(
        entries,
        run,
        project,
        "Compare manual upload in Dropbox and Google Photos; cite only official help.",
    )
    a = analysis([f.evidence_id for f in facts], "market_pattern")
    a["description"] = "本次两款产品的已引用步骤均包含用户主动操作；可作为手动入口设计的样本观察。"
    result = claims.materialize_analyses([a], facts, run_id=run, project_id=project)
    assert result[0].claim_kind == "inference" and result[0].status == "not_verified"
    assert len(result[0].supporting_finding_ids) == 2


def test_analysis_cannot_inject_confirmed_or_decision_fields():
    run, project, _, entries = setup()
    facts = facts_for(entries, run, project)
    for field, value in [("status", "confirmed"), ("pm_priority", "P0"), ("claim_kind", "fact")]:
        a = analysis([facts[0].evidence_id])
        a[field] = value
        with pytest.raises(ValueError):
            claims.materialize_analyses([a], facts, run_id=run, project_id=project)


def test_unsupported_market_universal_is_rejected():
    run, project, _, entries = setup()
    facts = facts_for(entries, run, project)
    a = analysis([facts[0].evidence_id])
    a["description"] = "所有竞品都支持批量上传。"
    with pytest.raises(ValueError):
        claims.materialize_analyses([a], facts, run_id=run, project_id=project)
