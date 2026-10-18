"""New v7 contracts exercise behavior; legacy regression assertions stay unchanged."""

import importlib
from copy import deepcopy

import pytest


def module():
    try:
        return importlib.import_module("app.services.research_spec_v7")
    except ModuleNotFoundError:
        pytest.fail("v7 stable question contract is not implemented")


def requirement(anchor, **changes):
    return dict(
        id="r1",
        anchor_ids=[anchor],
        kind="procedure",
        products=["Google Drive"],
        platform="Android",
        entry="app",
        action="upload",
        object="files",
        mode="manual",
        quantity="unspecified",
        condition_quotes=[],
        version=None,
        analysis_facets=[],
        **changes,
    )


def test_source_policy_is_not_an_unresolved_business_requirement():
    m = module()
    q = "Using only official Google Drive Help, explain how an Android user uploads files from the app."
    parts = m.question_parts(q)
    assert parts["policy_spans"]
    assert all(q[a["start"] : a["end"]] == a["text"] for a in parts["anchors"])
    assert not any("Using only" in a["text"] for a in parts["anchors"])
    spec = m.compile_spec(
        q, {"requirements": [requirement(parts["anchors"][0]["id"])], "queries": []}
    )
    assert spec["unresolved"] == []
    assert spec["requirements"][0]["id"] == "r1"
    assert spec["original_question"] == q
    assert spec["policy_spans"] == parts["policy_spans"]


def test_unaccounted_clause_survives_planning_and_offline_cannot_be_dropped():
    m = module()
    q = "In Google Drive Android, explain the upload steps; keep the offline condition."
    p = m.question_parts(q)
    spec = m.compile_spec(q, {"requirements": [requirement(p["anchors"][0]["id"])], "queries": []})
    assert spec["unresolved"]
    assert any("offline" in item["text"] for item in spec["unresolved"])


def test_kind_analysis_and_version_are_representable_without_question_numbers():
    m = module()
    q = "Compare Google Drive and Dropbox mobile upload entries. Give sample patterns, opportunities and risks. Which OS version is required?"
    parts = m.question_parts(q)
    assert len(parts["anchors"]) == 3, "list commas must not split one analysis requirement"
    tasks = []
    for n, a in enumerate(parts["anchors"]):
        t = requirement(a["id"])
        t.update(
            id=f"r{n}",
            kind=["comparison", "analysis", "version"][n],
            products=["Google Drive", "Dropbox"],
            platform="mobile",
            version=None,
        )
        if n == 1:
            t["analysis_facets"] = ["market_pattern", "opportunity", "risk"]
        tasks.append(t)
    spec = m.compile_spec(q, {"requirements": tasks, "queries": []})
    assert not spec["unresolved"]
    assert {r["kind"] for r in spec["requirements"]} == {"comparison", "analysis", "version"}
    assert spec["requirements"][-1]["version"] is None


def test_compiled_plan_does_not_mutate_model_candidate_and_keeps_stable_fingerprint():
    m = module()
    q = "Google Drive Android upload steps?"
    a = m.question_parts(q)["anchors"][0]["id"]
    candidate = {"requirements": [requirement(a)], "queries": []}
    original = deepcopy(candidate)
    one = m.compile_spec(q, candidate)
    two = m.compile_spec(q, candidate)
    assert candidate == original
    assert one["fingerprint"] == two["fingerprint"]
    assert one["requirements"] == two["requirements"]


@pytest.mark.parametrize(
    "resource,limit", [("llm_calls", 3), ("logical_searches", 3), ("official_reads", 5)]
)
def test_budget_is_reserved_before_dispatch_and_never_increases(resource, limit):
    m = module()
    b = m.ResearchBudget()
    for _ in range(limit):
        b.reserve(resource)
    with pytest.raises(m.SpecError):
        b.reserve(resource)
    assert b.to_dict()[resource] == limit


def test_offered_budget_is_cumulative_and_selection_union_is_capped():
    m = module()
    b = m.ResearchBudget()
    b.offer([f"e{i}" for i in range(60)])
    b.offer([f"e{i}" for i in range(40, 100)])
    assert b.to_dict()["offered_unique_evidence_units"] == 100
    with pytest.raises(m.SpecError):
        b.offer(["e101"])
    b.select([f"e{i}" for i in range(40)])
    with pytest.raises(m.SpecError):
        b.select(["e40"])
    assert b.to_dict()["selected_unique_evidence_units"] == 40


@pytest.mark.parametrize(
    "q,expected",
    [
        (
            "Google Photos Android backup? Cite only official Google Help; separate manual upload from automatic backup.",
            "separate manual",
        ),
        (
            "Dropbox iPhone upload? Cite only official Dropbox Help and state any limits actually documented.",
            "state any limits",
        ),
        (
            "Using only official Google Drive Help, explain upload steps. Cite only official Google Help.",
            "explain upload",
        ),
        ("仅引用 Apple 官方支持：手机有哪些上传选项？请保留版本条件。", "手机有哪些上传选项"),
    ],
)
def test_source_policy_never_swallows_following_business_and_offsets_are_exact(q, expected):
    m = module()
    parts = m.question_parts(q)
    assert parts["policy_spans"]
    assert any(expected in a["text"] for a in parts["anchors"])
    assert all(
        q[x["start"] : x["end"]] == x["text"] and x["end"] <= len(q)
        for x in parts["anchors"] + parts["policy_spans"]
    )
    assert all("Cite only" not in a["text"] and "仅引用" not in a["text"] for a in parts["anchors"])


def test_inline_offline_is_not_erased_by_covering_its_entire_anchor():
    m = module()
    q = "Google Drive Android upload steps while offline?"
    p = m.question_parts(q)
    spec = m.compile_spec(q, {"requirements": [requirement(p["anchors"][0]["id"])], "queries": []})
    assert any("offline" in u["text"] for u in spec["unresolved"])


@pytest.mark.parametrize("corrupt", ["extra", "duplicate", "invented_product", "missing"])
def test_contract_rejects_undeclared_fields_duplicate_ids_and_invented_subject(corrupt):
    m = module()
    q = "Google Drive Android upload steps?"
    t = requirement(m.question_parts(q)["anchors"][0]["id"])
    tasks = [t]
    if corrupt == "extra":
        t["is_complete"] = True
    if corrupt == "duplicate":
        tasks.append(deepcopy(t))
    if corrupt == "invented_product":
        t["products"] = ["Invented Product"]
    if corrupt == "missing":
        del t["mode"]
    with pytest.raises(m.SpecError):
        m.compile_spec(q, {"requirements": tasks, "queries": []})


def test_planning_schema_is_a_real_closed_json_schema_and_covers_business_kinds():
    m = module()
    schema = m.PLANNING_SCHEMA
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    props = schema["properties"]["requirements"]["items"]["properties"]
    assert {
        "capability",
        "procedure",
        "entry_listing",
        "destination",
        "condition",
        "limit",
        "version",
        "comparison",
        "analysis",
    } <= set(props["kind"]["enum"])


def test_scope_original_is_preserved_and_affects_fingerprint():
    m = module()
    q = "Google Drive Android upload steps?"
    c = {"requirements": [requirement(m.question_parts(q)["anchors"][0]["id"])], "queries": []}
    a = m.compile_spec(q, c, scope="existing device photos")
    b = m.compile_spec(q, c, scope="offline existing device photos")
    assert a["original_scope"] == "existing device photos"
    assert a["fingerprint"] != b["fingerprint"]


def test_budget_counts_uniques_within_one_offer_and_rejects_invalid_ids_atomically():
    m = module()
    b = m.ResearchBudget()
    b.offer(["e1"] * 101)
    assert b.to_dict()["offered_unique_evidence_units"] == 1
    with pytest.raises(m.SpecError):
        b.offer([None])
    assert b.to_dict()["offered_unique_evidence_units"] == 1


def test_source_policy_cannot_reappear_as_a_business_condition():
    q = "Using only official Atlas Help, explain Atlas mobile upload steps."
    # Build the closed wire object directly: policy text is data, not a premise.
    r = dict(
        id="r1",
        anchor_ids=["q1"],
        kind="condition",
        products=["Atlas"],
        platform="mobile",
        entry=None,
        action="upload",
        object="photos",
        mode="manual",
        quantity="unspecified",
        condition_quotes=["Using only official Atlas Help"],
        version=None,
        analysis_facets=[],
    )
    m = module()
    with pytest.raises(m.SpecError, match="condition_is_source_policy"):
        m.compile_spec(q, dict(requirements=[r], queries=[]))


def test_policy_candidate_cannot_destroy_a_real_factual_requirement():
    m = module()
    q = "Using only official Google Drive Help, explain Android upload steps while offline?"
    r = requirement("q1")
    r["condition_quotes"] = ["Using only official Google Drive Help", "while offline"]
    spec = m.compile_spec(q, dict(requirements=[r], queries=[]))
    actual = spec["requirements"][0]
    assert actual["condition_quotes"] == ["while offline"]
    assert actual["mandatory_conditions"] == ["while offline"]
    assert not spec["unresolved"]
    assert spec["normalizations"] == [
        dict(path="requirements[0].condition_quotes[0]", code="source_policy_removed")
    ]


def test_invented_condition_is_not_allowed_to_replace_original_offline_obligation():
    m = module()
    q = "Google Drive Android upload steps while offline?"
    r = requirement("q1")
    r["condition_quotes"] = ["while connected to Wi-Fi"]
    spec = m.compile_spec(q, dict(requirements=[r], queries=[]))
    assert spec["requirements"][0]["condition_quotes"] == []
    assert spec["requirements"][0]["mandatory_conditions"] == ["while offline"]
    assert any(u["reason"] == "condition_omitted" for u in spec["unresolved"])
    assert spec["normalizations"] == [
        dict(path="requirements[0].condition_quotes[0]", code="non_input_condition_removed")
    ]


def test_chinese_disabled_clause_stops_at_the_business_clause_boundary():
    from app.services.research_spec_v7 import condition_inventory

    assert condition_inventory("关闭相机上传（camera uploads）后，移动端还能手动添加照片吗") == [
        "关闭相机上传（camera uploads）后"
    ]
    assert condition_inventory("请保留完整操作步骤和禁用条件") == []


def test_output_label_instruction_is_not_an_extra_business_obligation():
    m = module()
    q = "Compare Atlas and Vega manual upload. Distinguish facts, inferences and recommendations."
    parts = m.question_parts(q)
    assert len(parts["anchors"]) == 1
    assert any("Distinguish facts" in p["text"] for p in parts["policy_spans"])


def test_model_cannot_add_multiple_quantity_to_an_ordinary_plural():
    m = module()
    q = "Atlas mobile upload photos steps?"
    r = requirement("q1") | dict(
        products=["Atlas"], platform="mobile", object="photos", quantity="multiple"
    )
    s = m.compile_spec(q, dict(requirements=[r], queries=[]))
    assert s["requirements"][0]["mandatory_quantity"] == "unspecified"


def test_unrequested_analysis_and_omitted_requested_facets_fail_planning():
    m = module()
    r = requirement("q1") | dict(products=["Atlas"], kind="analysis", analysis_facets=["strength"])
    with pytest.raises(m.SpecError, match="analysis_not_requested"):
        m.compile_spec("Atlas mobile upload steps?", dict(requirements=[r], queries=[]))
    q = "Analyze Atlas mobile upload: give sample patterns, strengths, weaknesses, opportunities and risks."
    with pytest.raises(m.SpecError, match="analysis_facets_omitted"):
        m.compile_spec(q, dict(requirements=[r], queries=[]))


@pytest.mark.parametrize(
    "q",
    [
        "Can OneDrive iPhone upload photos and Android delete files?",
        "Can OneDrive upload photos on iPhone and delete files on Android?",
    ],
)
def test_action_device_relation_handles_prefix_and_postposed_devices(q):
    m = module()
    anchor = m.question_parts(q)["anchors"][0]["id"]
    a = requirement(anchor) | dict(
        id="upload",
        kind="capability",
        products=["OneDrive"],
        platform="iPhone",
        action="upload",
        object="photos",
        mode="unspecified",
    )
    b = a | dict(id="delete", platform="Android", action="delete", object="files")
    candidate = dict(requirements=[a, b], queries=[])
    original = deepcopy(candidate)
    s = m.compile_spec(q, candidate)
    assert candidate == original and not s["unresolved"] and not s["rejected_requirements"]
    rows = {r["id"]: r for r in s["requirements"]}
    assert rows["upload"]["platform"] == "iPhone" and rows["delete"]["platform"] == "Android"
    assert not set(rows["upload"]["fulfills_obligation_ids"]) & set(
        rows["delete"]["fulfills_obligation_ids"]
    )
    with pytest.raises(m.SpecError, match="platform_inconsistent_with_question"):
        m.compile_spec(
            q,
            dict(
                requirements=[a | dict(platform="Android"), b | dict(platform="iPhone")], queries=[]
            ),
        )


def test_broad_mobile_scope_is_compatible_with_explicit_android():
    m = module()
    q = "Can OneDrive Android upload photos?"
    p = dict(
        requirements=[
            (
                requirement("q1")
                | dict(
                    kind="capability",
                    products=["OneDrive"],
                    object="photos",
                    platform="Android",
                    mode="unspecified",
                )
            )
        ],
        queries=[],
    )
    s = m.compile_spec(q, p, "Mobile photo upload; prefer official product support pages")
    assert not s["unresolved"], s["unresolved"]


def test_shared_action_retains_each_product_own_platform_without_cross_pairs():
    from app.services.research_input_v7 import extract_input_obligations

    q = "Compare manual photo upload entry points in Google Photos Android and Dropbox mobile."
    rows = extract_input_obligations(q)
    pairs = {
        (owner, p["canonical"])
        for row in rows
        for owner in row["owners"]
        for p in row["platform_scope"]
    }
    assert pairs == {("google_photos", "Android"), ("dropbox", "mobile")}, rows


def test_quantity_phrase_is_not_a_business_condition():
    m = module()
    q = "Can OneDrive iPhone upload multiple photos?"
    p = dict(
        requirements=[
            (
                requirement("q1")
                | dict(
                    kind="capability",
                    products=["OneDrive"],
                    object="photos",
                    platform="iPhone",
                    mode="unspecified",
                    quantity="multiple",
                    condition_quotes=["multiple photos"],
                )
            )
        ],
        queries=[],
    )
    s = m.compile_spec(q, p)
    assert s["requirements"][0]["condition_quotes"] == []


def test_elliptical_automatic_comparison_keeps_its_atomic_premise():
    m = module()
    q = "How to manually upload photos in OneDrive on Android, and distinguish automatic upload?"
    r = requirement("q1") | dict(
        products=["OneDrive"], object="photos", platform="Android", mode="manual"
    )
    p = dict(
        requirements=[
            r,
            r | dict(id="auto", kind="capability", mode="automatic"),
            r | dict(id="cmp", kind="comparison", mode="mixed"),
        ],
        queries=[],
    )
    s = m.compile_spec(q, p)
    rows = {r["id"]: r for r in s["requirements"]}
    assert "auto" in rows and rows["auto"]["kind"] == "capability"
    assert not s["unresolved"], s["unresolved"]
