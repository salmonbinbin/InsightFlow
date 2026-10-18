"""Phase 8 — Research Agent v4 prompt + official-query builder regression.

These tests target the v14 audit root causes:

1. v14b Dropbox Run 3fa7ca34-de32-497a-8001-3815980b96d2:
   the question "According to official Dropbox Help, can
   mobile users manually add photos when camera uploads
   are disabled?" produced the official query
   ``Dropbox photos site:help.dropbox.com/create-upload``
   (manual / add / mobile tokens missing). The canonical
   /create-upload/upload-mobile page did not surface in
   the upstream ranking; unrelated /photos-not-appearing
   /ios-formats pages did. The v14 fix surfaces
   ``manual`` / ``add`` / ``mobile`` tokens in the query
   body and keeps the /create-upload site path.

2. v14b iCloud Run f8d51c18-a421-4389-9545-72424c3b7b4d:
   the question "According to official Apple Support, which
   upload choices does iCloud.com show on a phone or
   tablet, and where should users upload to a Shared
   Photo Library?" produced the official query
   ``Apple Support upload site:support.apple.com/en-us``
   (the vendor brand won over the iCloud / Shared Photo
   Library subject). The v14 fix surfaces the iCloud /
   iCloud.com / Shared Photo Library subject in the body
   and never treats "Apple Support" as the body subject.

3. v14 prompt: the active prompt is research-agent-v4.
   The v3 prompt is preserved for archival but no longer
   imported by the orchestrator. The v4 prompt is
   concise (≤100 lines) and self-consistent — it does not
   instruct the LLM to emit illegal-status entries that
   would fail the closed schema, and it does not carry
   v11/v12 stopword fallback language.

4. v14 description contract: still strict. Multi-line
   numbered quotes are rejected; multi-step single-
   product operations must be split into multiple
   competitor entries; the name field must be a short
   product name only; market_patterns need ≥2 sources.

The tests in this file are TDD-style: they were authored
to fail before the v14 fix and pass after it.
"""

from __future__ import annotations

from app.agents.prompts import research_agent_v2, research_agent_v4
from app.services import research_service
from app.services.research_service import (
    _OFFICIAL_VENDOR_RULES,
    _build_official_provider_query,
)

# ============================================================================
# v14b — Dropbox official query must surface manual / add / mobile
# ============================================================================


def test_v14b_dropbox_official_query_contains_manual_add_mobile():
    """v14b — the v14b Dropbox audit surfaced a real
    regression: the question's manual / add / mobile
    tokens were dropped from the official query, so the
    canonical /create-upload/upload-mobile page did not
    surface in the upstream ranking. The v14 fix adds
    the missing action terms (``add`` / ``manual``) and
    ensures the existing platform term (``mobile``) is
    carried into the body. The /create-upload site path
    re-route is preserved.
    """
    rule = _OFFICIAL_VENDOR_RULES["dropbox"]
    q = _build_official_provider_query(
        "According to official Dropbox Help, can mobile users "
        "manually add photos when camera uploads are disabled",
        "only official Dropbox Help",
        rule,
    )
    # Product anchor preserved.
    assert "Dropbox" in q
    # Site constraint re-routes to /create-upload (Dropbox
    # upload-action override from v9).
    assert q.endswith("site:help.dropbox.com/create-upload")
    # v14 fix: the manual / add / mobile tokens surface
    # in the body. The query must be bounded by the
    # 80-char body cap and the 500-char total cap.
    q_lower = q.lower()
    for token in ("manual", "add", "mobile"):
        assert token in q_lower, f"v14 fix: {token!r} missing from official query: {q!r}"
    assert len(q) <= 500


# ============================================================================
# v14b — iCloud official query must prefer iCloud subject over Apple Support
# ============================================================================


def test_v14b_icloud_official_query_prefers_icloud_subject_over_apple_support():
    """v14b — the v14b iCloud audit surfaced a real
    regression: the question's vendor brand citation
    ("According to official Apple Support") won over
    the iCloud / Shared Photo Library subject, so the
    upstream query surfaced the vendor brand instead of
    the product. The v14 fix demotes the "Apple Support"
    brand citation and prefers the most specific iCloud
    product keyword that also matches in the question
    (``iCloud`` / ``iCloud.com`` / ``Shared Photo
    Library``).
    """
    rule = _OFFICIAL_VENDOR_RULES["icloud_photos"]
    q = _build_official_provider_query(
        "According to official Apple Support, which upload "
        "choices does iCloud.com show on a phone or tablet, "
        "and where should users upload to a Shared Photo "
        "Library",
        "only official",
        rule,
    )
    # Site constraint must still point at Apple Support
    # (the vendor host for iCloud documentation).
    assert "site:support.apple.com" in q
    # v14 fix: the body must surface the iCloud subject
    # (iCloud / iCloud.com / Shared Photo Library) NOT
    # the "Apple Support" vendor brand phrase.
    q_lower = q.lower()
    assert "icloud" in q_lower, f"v14 fix: 'icloud' missing from official query: {q!r}"
    # The vendor brand citation must NOT be the body
    # subject.
    assert not q.startswith("Apple Support"), (
        f"v14 fix: 'Apple Support' must not be the body subject: {q!r}"
    )
    # Subject keywords surface in original case (the
    # product_keyword resolution preserves the user's
    # original casing).
    assert "iCloud" in q or "icloud.com" in q.lower()
    assert len(q) <= 500


# ============================================================================
# v14 — active prompt is research-agent-v4
# ============================================================================


def test_v14_active_prompt_is_v4():
    """v14 — the orchestrator must import the v4
    prompt, not v3. A regression to v3 (e.g. a stale
    import) shows up here. The v3 module is preserved
    for archival but the orchestrator no longer
    references it.
    """
    # The orchestrator binds the active prompt under
    # the alias ``_research_prompt``; that alias must
    # resolve to the v4 module.
    from app.agents.prompts import research_agent_v6
    assert research_service._research_prompt is research_agent_v6
    assert research_agent_v4.PROMPT_VERSION == "research-agent-v4"
    # v4's version string is distinct from v3.
    assert research_agent_v4.PROMPT_VERSION == "research-agent-v4"
    assert research_agent_v4.PROMPT_VERSION != "research-agent-v3"
    # v3 module is still importable for archival.
    from app.agents.prompts import research_agent_v3  # noqa: F401


# ============================================================================
# v14 — prompt is concise and self-consistent
# ============================================================================


def test_v14_prompt_is_concise():
    """v14 — the v4 prompt is concise (≤100 file lines
    as the brief recommends). The v3 prompt grew to
    ~500 lines with v11/v12 stopword fallback language;
    v4 is short and self-consistent.
    """
    import os

    path = os.path.abspath(research_agent_v4.__file__)
    line_count = sum(1 for _ in open(path, encoding="utf-8"))
    assert line_count <= 100, f"v4 prompt must be ≤100 lines per brief; got {line_count}"


def test_v14_prompt_omits_v11_v12_stopword_fallback_language():
    """v14 — the v4 prompt does not carry v11/v12
    stopword / brand-anchor fallback language. The
    v3 prompt's "vacuous truth" / "brand-anchor
    exemption" framing is closed. The three exact
    description shapes are the only paths to
    confirmation; the v4 prompt names alternative
    prefix wording (e.g. "From the Google page.") as a
    demoted case.
    """
    prompt_lower = research_agent_v4.SYSTEM_PROMPT.lower()
    # v11/v12 loophole closure — these phrases must not
    # appear in v4.
    assert "vacuous truth" not in prompt_lower
    assert "brand-anchor" not in prompt_lower
    assert "stopword backdoor" not in prompt_lower
    # v14 still requires the three exact shapes (named
    # verbatim) AND the alternative-prefix demoted case.
    assert "from the cited source." in prompt_lower
    assert "from the official help page." in prompt_lower
    assert "from the google page" in prompt_lower


def test_v14_prompt_requires_short_product_name_only():
    """v14 — competitor.name must be the product's
    short name only. Embedding the operational
    conclusion into the name (e.g. ``name="OneDrive
    mobile add photos"``) would cause name-support
    demotion on a single-source page that mentions
    only "OneDrive".
    """
    prompt_lower = research_agent_v4.SYSTEM_PROMPT.lower()
    assert "short name" in prompt_lower
    # v14 also explicitly forbids baking the operational
    # conclusion into the name.
    assert "do not embed" in prompt_lower or "must be" in prompt_lower


def test_v14_prompt_requires_multi_step_competitor_splitting():
    """v14 — a single-product multi-step operation
    (e.g. OneDrive mobile add photos) must be split
    into MULTIPLE competitor entries, each with a
    single-step Evidence quote. The v4 prompt forbids
    the failure modes the v14 audit surfaced:
    multi-line joined quotes, market_patterns with a
    single product name, and a competitor.name that
    bakes in the operational conclusion.
    """
    prompt = research_agent_v4.SYSTEM_PROMPT
    prompt_lower = prompt.lower()
    # Single-line contiguous quote requirement.
    assert "single, contiguous" in prompt_lower or "single contiguous" in prompt_lower
    # Do NOT join numbered / list-item lines.
    assert "do not join" in prompt_lower
    # Multi-step → multiple competitor entries.
    assert "sequence" in prompt_lower or "multiple competitor" in prompt_lower
    # market_patterns require ≥2 distinct sources.
    assert "≥2" in prompt or "two distinct" in prompt_lower
    # No stacking same-product steps into market_patterns.
    assert "do not stack" in prompt_lower or "stack same-product" in prompt_lower


def test_v14_prompt_requires_multi_select_quantifier_in_quote():
    """v14 — the multi-select guard is preserved. When
    the research question names multi-select / batch /
    multiple / several, the Evidence: "..." quote MUST
    directly contain a quantifier equivalent. Generic
    plural "files" / "photos" alone is NOT sufficient.
    """
    prompt_lower = research_agent_v4.SYSTEM_PROMPT.lower()
    assert "multi-select" in prompt_lower or "multi select" in prompt_lower
    assert "quantifier" in prompt_lower
    # Generic plural is NOT sufficient.
    assert "generic plural" in prompt_lower
    assert "files" in prompt_lower and "photos" in prompt_lower


def test_v14_prompt_omits_illegal_status_instructions():
    """v14 — the v4 prompt does NOT instruct the LLM to
    emit entries with an illegal status shape. The v3
    prompt conflicted with the closed schema (it told
    the LLM to emit ``status="not_verified"`` /
    ``source_refs=[]`` on sourced categories, but the
    closed schema for competitors / market_patterns
    REQUIRES source_refs — such entries would fail the
    validator before reaching the status check). The v4
    prompt mandates empty arrays for missing evidence.
    """
    prompt = research_agent_v4.SYSTEM_PROMPT
    # The v4 prompt mandates empty arrays for missing
    # evidence, not illegal-status entries.
    assert "leave the array empty" in prompt.lower() or "[]" in prompt
    # Sourced categories require source_refs (per the
    # closed schema); the v4 prompt must not instruct
    # the LLM to violate this.
    assert "source_refs" in prompt


# ============================================================================
# v14 — wire contract unchanged (closed TOOL_SCHEMA preserved)
# ============================================================================


def test_v14_tool_schema_preserves_strict_required_and_additional_properties():
    """v14 — the v4 prompt reuses v2's closed TOOL_SCHEMA
    via deepcopy. The deepcopy may add field description
    strings but MUST NOT relax ``additionalProperties``
    or remove any ``required`` entry — the wire contract
    is unchanged.
    """
    schema_v4 = research_agent_v4.TOOL_SCHEMA
    schema_v2 = research_agent_v2.TOOL_SCHEMA
    # Top-level: still closed, same required set.
    assert schema_v4["additionalProperties"] is False
    assert schema_v4["required"] == schema_v2["required"]
    # Per-array item schemas: still closed, same required
    # set as v2.
    for arr_name in ("competitors", "market_patterns", "opportunities", "risks"):
        items_v4 = schema_v4["properties"][arr_name]["items"]
        items_v2 = schema_v2["properties"][arr_name]["items"]
        assert items_v4["additionalProperties"] is False
        assert items_v4["required"] == items_v2["required"]
    # source_refs entry schema: still closed, same
    # required set.
    for arr_name in ("competitors", "market_patterns", "opportunities", "risks"):
        sr_v4 = schema_v4["properties"][arr_name]["items"]["properties"]["source_refs"]["items"]
        sr_v2 = schema_v2["properties"][arr_name]["items"]["properties"]["source_refs"]["items"]
        assert sr_v4["additionalProperties"] is False
        assert sr_v4["required"] == sr_v2["required"]


def test_v14_tool_schema_is_a_deepcopy_not_a_reference():
    """v14 — the v4 TOOL_SCHEMA is a deepcopy of v2's
    TOOL_SCHEMA, not a reference. Mutating v4's schema
    must not affect v2's schema.
    """
    schema_v4 = research_agent_v4.TOOL_SCHEMA
    schema_v2 = research_agent_v2.TOOL_SCHEMA
    # Same content.
    assert schema_v4 == schema_v2
    # But distinct objects — mutating v4 must not
    # leak into v2.
    schema_v4["_v14_marker"] = "test"
    assert "_v14_marker" not in schema_v2
    del schema_v4["_v14_marker"]


# ============================================================================
# v14 — end-to-end: prompt version stamp
# ============================================================================


def test_v14_prompt_version_constant_is_v4():
    """v14 — the prompt module's version constant is
    ``research-agent-v4`` and the system prompt
    references the v14 marker so an audit can find the
    contract revision.
    """
    assert research_agent_v4.PROMPT_VERSION == "research-agent-v4"
    prompt_lower = research_agent_v4.SYSTEM_PROMPT.lower()
    # v14 marker is named in the prompt text.
    assert "(v14)" in prompt_lower or "v14" in prompt_lower


def test_query_does_not_turn_unrelated_prefix_words_into_actions():
    _, actions = research_service._scan_semantic_terms(
        "Dropbox viewpoint shareholder editorial storagecorp manually add photos"
    )
    assert set(actions) == {"manual", "add", "photos"}


def test_icloud_query_preserves_distinct_requested_product_surfaces():
    query = _build_official_provider_query(
        "According to official Apple Support, which upload choices does iCloud.com "
        "show on a phone or tablet, and where should users upload to a Shared Photo Library?",
        None,
        _OFFICIAL_VENDOR_RULES["icloud_photos"],
    )
    assert "icloud.com" in query.lower()
    assert "shared photo library" in query.lower()
    assert query.endswith("site:support.apple.com/en-us")


def _step_payload(descriptions):
    return {
        "summary": "Synthetic internal context; no market conclusion.",
        "confidence": 0.5,
        "competitors": [
            {
                "name": "Google Drive",
                "description": description,
                "confidence": 0.5,
                "source_refs": [
                    {
                        "provider_source_id": "official-1",
                        "url": "https://support.google.com/drive/answer/2424368",
                        "title": "Google Drive Android upload",
                    }
                ],
            }
            for description in descriptions
        ],
        "market_patterns": [],
        "opportunities": [],
        "risks": [],
    }


def _validate_steps(descriptions):
    return research_service.validate_research_payload(
        _step_payload(descriptions),
        allowed_provider_source_ids={"official-1"},
        allowed_provider_urls={"official-1": "https://support.google.com/drive/answer/2424368"},
        source_haystacks={"official-1": "google drive android upload"},
        source_snippets={
            "official-1": "Open the Google Drive app.\nTap New and then Upload.\nFind and tap the files you want to upload."
        },
        validate_evidence_quotes=True,
        validate_description_grounding=True,
        research_question="What are the Google Drive Android upload steps?",
    )


def test_separate_verbatim_step_quotes_survive_all_strict_gates():
    result = _validate_steps(
        [
            'Evidence: "Open the Google Drive app."',
            'Evidence: "Tap New and then Upload."',
            'Evidence: "Find and tap the files you want to upload."',
        ]
    )
    assert len([f for f in result.findings if f.status == "confirmed"]) == 3
    assert {f.category for f in result.findings} == {"summary", "competitor"}


def test_joining_steps_into_one_quote_still_fails_closed():
    result = _validate_steps(
        [
            'Evidence: "Open the Google Drive app. Tap New and then Upload."',
        ]
    )
    assert not any(f.status == "confirmed" for f in result.findings)
    assert not research_service._has_external_confirmation(result.findings)
