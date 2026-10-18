"""Independent guard parity checks for the structured quote channel."""

from dataclasses import replace

import pytest
from test_phase8_research_claims import analysis, facts_for, setup

from app.services import research_claims as claims


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/upload",
        "https://support.google.com/photos/answer/6193313",
        "http://help.dropbox.com/create-upload/upload-mobile",
        "https://user@help.dropbox.com/create-upload/upload-mobile",
        "https://help.dropbox.com:444/create-upload/upload-mobile",
    ],
)
def test_official_source_and_reader_url_boundaries_remain_strict(url):
    run, project, source, entries = setup()
    source.url = url
    facts = facts_for(entries, run, project)
    assert all(f.status == "not_verified" and not f.source_refs for f in facts)


def test_range_cannot_extend_past_snapshot_even_when_slice_text_matches():
    run, project, source, entries = setup()
    key = next(iter(entries))
    entries[key]["unit"] = replace(entries[key]["unit"], offset=0, length=200, text=source.snippet)
    with pytest.raises(claims.ClaimError):
        facts_for(entries, run, project)


def test_app_word_does_not_make_a_computer_directive_relevant_to_iphone():
    run, project, _, entries = setup(
        "# Dropbox\n## Upload\nOn a computer, use the Dropbox app to upload photos.\n"
    )
    facts = facts_for(
        entries,
        run,
        project,
        "In the Dropbox iPhone app, how to upload photos? Cite only official Dropbox Help.",
    )
    assert facts and all(f.status == "not_verified" for f in facts)


def test_inference_cannot_be_reused_as_a_confirmed_support_fact():
    run, project, _, entries = setup()
    fact = replace(facts_for(entries, run, project)[0], claim_kind="inference")
    with pytest.raises(claims.ClaimError):
        claims.materialize_analyses(
            [analysis([fact.evidence_id])], [fact], run_id=run, project_id=project
        )


def test_analysis_materialization_preserves_scenario_and_explicit_label():
    run, project, _, entries = setup()
    facts = facts_for(entries, run, project)
    candidate = analysis([facts[0].evidence_id])
    result = claims.materialize_analyses([candidate], facts, run_id=run, project_id=project)
    assert candidate["scenario"] in result[0].description
    assert "建议" in result[0].description


def test_mobile_scope_includes_an_explicit_android_and_ios_branch():
    run, project, _, entries = setup(
        "# Dropbox mobile\n## Upload\n"
        "6. Tap the folder icon (Android) or Next (iOS), and select where to add files.\n"
    )
    facts = facts_for(entries, run, project)
    assert facts and all(f.status == "confirmed" for f in facts)


def test_ios_and_macos_note_retains_its_phone_applicability():
    run, project, source, entries = setup(
        "# iCloud Photos\n## Upload photos\n"
        "Note: To upload to iCloud Shared Photo Library, use Photos on iOS 16.1, iPadOS 16, macOS 13, or later.\n"
    )
    source.url = (
        "https://support.apple.com/guide/icloud/upload-and-download-photos-mmb5dbaf376f/icloud"
    )
    source.title = "Upload photos on iCloud.com"
    source.domain = "support.apple.com"
    facts = facts_for(
        entries,
        run,
        project,
        "On a phone or tablet, where to upload to a Shared Photo Library? Cite only official Apple Support.",
    )
    assert facts and all(f.status == "confirmed" for f in facts)
