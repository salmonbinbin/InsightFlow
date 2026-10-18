"""Actual frozen official paragraphs, independently selected to test support rules.

These fixtures are test evidence, never production question/answer tables.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.services import research_service as api
from app.services import research_support_v7 as support
from app.services.research_catalog_v7 import build_catalogs
from app.services.research_spec_v7 import compile_spec, question_parts

ROOT = Path(__file__).parent / "fixtures/phase8_research_v5"


def load(number, index=0):
    d = json.loads((ROOT / f"case-{number}.json").read_text())
    row = d["sources"][index]
    rid, pid = uuid4(), uuid4()
    source = SimpleNamespace(
        **{k: row[k] for k in ["provider_source_id", "url", "title", "domain", "snippet"]},
        run_id=rid,
    )
    cats, entries = build_catalogs([source])
    return rid, pid, cats, entries


def requirement(q, product, platform, **changes):
    return (
        dict(
            id="r1",
            anchor_ids=[a["id"] for a in question_parts(q)["anchors"]],
            kind="procedure",
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
        | changes
    )


def evaluate(q, r, case, index, pick):
    rid, pid, cats, entries = load(case, index)
    ids = [i for i, e in entries.items() if pick(e["unit"])]
    fs = support.exact_facts(
        ids, entries, run_id=rid, project_id=pid, api=api, full_source_ids=set(cats)
    )
    s = compile_spec(q, dict(requirements=[r], queries=[]))
    return support.evaluate_support(
        s, [dict(requirement_id="r1", evidence_ids=ids)], fs, entries, cats
    )


def test_frozen_google_manual_flow_is_valid_for_an_upload_wording():
    q = "How do Google Photos Android users manually upload one photo or video?"
    r = requirement(q, "Google Photos", "Android", object="photo or video")
    result = evaluate(
        q, r, 6, 0, lambda u: u.section == "Manually back up photos & videos" and bool(u.step_index)
    )
    assert result["overall_completable"], result


def test_frozen_dropbox_disabled_manual_flow_keeps_all_eight_steps():
    q = "Can Dropbox mobile users manually add photos when camera uploads are disabled?"
    r = requirement(
        q,
        "Dropbox",
        "mobile",
        kind="capability",
        action="add",
        condition_quotes=["when camera uploads are disabled"],
    )
    result = evaluate(
        q,
        r,
        7,
        0,
        lambda u: (
            u.section == "How to upload photos and videos"
            and (bool(u.step_index) or "don't have camera uploads enabled" in u.text)
        ),
    )
    assert result["overall_completable"], result
    assert len(result["items"][0]["evidence_ids"]) == 9


def test_frozen_onedrive_existing_branch_cannot_be_omitted():
    q = "How does a OneDrive iPhone user select existing pictures for manual upload?"
    r = requirement(q, "OneDrive", "iPhone", object="pictures")

    def take(u):
        return u.section == "Upload files and folders using the OneDrive for iOS app" and (
            bool(u.step_index) or "Choose Existing" in u.text or "Browse to the pictures" in u.text
        )

    result = evaluate(q, r, 3, 0, take)
    assert result["overall_completable"], result
    bad = evaluate(
        q,
        r,
        3,
        0,
        lambda u: (
            u.section == "Upload files and folders using the OneDrive for iOS app"
            and bool(u.step_index)
        ),
    )
    assert not bad["overall_completable"]


def test_frozen_apple_phone_choices_need_no_rewritten_upload_sentence():
    q = "Which upload choices does iCloud.com show on a phone or tablet?"
    r = requirement(q, "iCloud.com", "phone", kind="entry_listing", mode="unspecified")
    result = evaluate(
        q,
        r,
        9,
        2,
        lambda u: (
            u.section == "Upload photos" and u.text.startswith("- On a phone or tablet: Choose")
        ),
    )
    assert result["overall_completable"], result


def test_frozen_dropbox_general_statement_cannot_replace_the_published_eight_step_flow():
    q = "Can Dropbox mobile users manually add photos when camera uploads are disabled?"
    r = requirement(
        q,
        "Dropbox",
        "mobile",
        kind="capability",
        action="add",
        condition_quotes=["when camera uploads are disabled"],
    )
    result = evaluate(
        q,
        r,
        7,
        0,
        lambda u: (
            u.section == "How to upload photos and videos"
            and (
                (u.step_index and u.step_index != 3)
                or "don't have camera uploads enabled" in u.text
            )
        ),
    )
    assert not result["overall_completable"]
    assert "procedure" in result["items"][0]["reason_codes"]
