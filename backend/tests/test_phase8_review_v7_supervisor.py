"""Independent review counterexamples; never used as production answer tables."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_phase8_spec_v7_supervisor import requirement
from test_phase8_support_v7_supervisor import facts, inventory, module, spec

from app.services.research_catalog_v7 import build_catalogs
from app.services.research_spec_v7 import SpecError, compile_spec


@pytest.mark.parametrize("platform_word", ["Android", "安卓"])
def test_planner_cannot_replace_original_device(platform_word):
    q = f"Google Drive {platform_word} manual upload files steps?"
    r = requirement("q1") | dict(platform="iPhone")
    with pytest.raises(SpecError, match="platform"):
        compile_spec(q, dict(requirements=[r], queries=[]))


def test_multiple_for_delete_does_not_prove_multiple_for_upload():
    m = module()
    body = "# OneDrive iOS\n## Manually upload photos\n1. Open OneDrive on iPhone.\n2. Select multiple photos to delete, or select one photo then tap Upload.\n"
    rid, pid, cats, entries = inventory(body)
    ids = list(entries)
    s = spec("Can OneDrive iPhone manually upload multiple photos?", kind="capability")
    report = m.evaluate_support(
        s,
        [dict(requirement_id="r1", evidence_ids=ids)],
        facts(m, ids, entries, rid, pid),
        entries,
        cats,
    )
    assert not report["overall_completable"]
    assert "quantity" in report["items"][0]["reason_codes"]


def test_automatic_when_disabled_cannot_borrow_manual_disabled_sibling():
    m = module()
    body = "# OneDrive iOS\n## Automatically upload photos\nWhen camera uploads are enabled, OneDrive automatically uploads photos on iPhone.\n## Manually upload photos\nWhen camera uploads are disabled, select photos and then tap Upload on iPhone.\n"
    rid, pid, cats, entries = inventory(body)
    ids = list(entries)
    q = "Can OneDrive iPhone automatically upload photos when camera uploads are disabled?"
    s = spec(
        q,
        kind="capability",
        mode="automatic",
        condition_quotes=["when camera uploads are disabled"],
    )
    report = m.evaluate_support(
        s,
        [dict(requirement_id="r1", evidence_ids=ids)],
        facts(m, ids, entries, rid, pid),
        entries,
        cats,
    )
    assert not report["overall_completable"]


def test_manual_only_sides_cannot_complete_manual_automatic_comparison():
    m = module()
    rid, pid = uuid4(), uuid4()
    sources = [
        SimpleNamespace(
            provider_source_id=p,
            run_id=rid,
            title=p,
            domain=url.split("/")[2],
            url=url,
            snippet=f"# {p} mobile\n## Manually upload photos\n1. Open {p} on a phone.\n2. Choose photos and tap Upload.\n",
        )
        for p, url in [
            (
                "OneDrive",
                "https://support.microsoft.com/en-us/onedrive/manually-upload-files-or-photos-to-onedrive-in-ios",
            ),
            ("Dropbox", "https://help.dropbox.com/create-upload/upload-mobile"),
        ]
    ]
    cats, entries = build_catalogs(sources)
    ids = list(entries)
    fs = m.exact_facts(
        ids,
        entries,
        run_id=rid,
        project_id=pid,
        api=__import__("app.services.research_service", fromlist=["x"]),
        full_source_ids=set(cats),
    )
    q = "Compare OneDrive and Dropbox mobile manual and automatic upload photos."
    base = requirement("q1") | dict(products=["OneDrive"], platform="mobile", object="photos")
    tasks = [
        base | dict(id="one"),
        base | dict(id="two", products=["Dropbox"]),
        base
        | dict(id="compare", products=["OneDrive", "Dropbox"], kind="comparison", mode="mixed"),
    ]
    s = compile_spec(q, dict(requirements=tasks, queries=[]))
    bindings = [
        dict(
            requirement_id=r["id"],
            evidence_ids=[e for e in ids if entries[e]["unit"].source_id == r["products"][0]],
        )
        for r in tasks[:2]
    ]
    report = m.evaluate_support(s, bindings, fs, entries, cats)
    assert not report["overall_completable"]
    assert report["items"][-1]["status"] != "covered"


def test_multi_product_atomic_requirement_needs_every_product():
    m = module()
    body = "# OneDrive iOS\n## Upload photos\n1. Open OneDrive on iPhone.\n2. Choose photos and tap Upload.\n"
    rid, pid, cats, entries = inventory(body)
    ids = list(entries)
    s = spec(
        "Can OneDrive and Dropbox iPhone manually upload photos?",
        kind="capability",
        products=["OneDrive", "Dropbox"],
    )
    report = m.evaluate_support(
        s,
        [dict(requirement_id="r1", evidence_ids=ids)],
        facts(m, ids, entries, rid, pid),
        entries,
        cats,
    )
    assert not report["overall_completable"]


def test_version_interval_intersects_bounds_and_rejects_outside_value():
    m = module()
    body = "# OneDrive iOS\n## Upload photos\nUpload photos on iPhone requires iOS 16 or later and iOS 18 or earlier.\n"
    rid, pid, cats, entries = inventory(body)
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    for number, expected in [(17, True), (19, False)]:
        s = spec(
            f"Can OneDrive iPhone iOS {number} upload photos?",
            kind="capability",
            version=f"iOS {number}",
        )
        report = m.evaluate_support(
            s, [dict(requirement_id="r1", evidence_ids=ids)], fs, entries, cats
        )
        assert report["overall_completable"] == expected, report


def test_quantity_on_next_execution_step_and_noun_alternatives_stay_valid():
    m = module()
    for text in [
        "Select multiple photos, then tap Upload.",
        "Select multiple photos or videos, then tap Upload.",
        "Select multiple photos to delete, then upload one photo.",
    ]:
        rid, pid, cats, entries = inventory(
            "# OneDrive iOS\n## Upload photos\n1. Open OneDrive on iPhone.\n2. " + text + "\n"
        )
        ids = list(entries)
        s = spec("Can OneDrive iPhone upload multiple photos?", kind="capability")
        report = m.evaluate_support(
            s,
            [dict(requirement_id="r1", evidence_ids=ids)],
            facts(m, ids, entries, rid, pid),
            entries,
            cats,
        )
        assert report["overall_completable"] == ("delete" not in text), report


def test_quantity_execution_may_precede_its_object():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Upload photos\nYou can manually upload multiple photos on iPhone.\n"
    )
    ids = list(entries)
    s = spec("Can OneDrive iPhone manually upload multiple photos?", kind="capability")
    assert m.evaluate_support(
        s,
        [dict(requirement_id="r1", evidence_ids=ids)],
        facts(m, ids, entries, rid, pid),
        entries,
        cats,
    )["overall_completable"]


def test_version_empty_interval_is_a_conflict_even_without_requested_number():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Upload photos\nUpload photos on iPhone requires iOS 20 or later and iOS 18 or earlier.\n"
    )
    ids = list(entries)
    s = spec("Which iOS version does OneDrive iPhone upload photos require?", kind="version")
    report = m.evaluate_support(
        s,
        [dict(requirement_id="r1", evidence_ids=ids)],
        facts(m, ids, entries, rid, pid),
        entries,
        cats,
    )
    assert not report["overall_completable"]
    assert "version_conflict" in report["items"][0]["reason_codes"]


@pytest.mark.parametrize("complete_second", [False, True])
def test_every_product_has_its_own_complete_flow(complete_second):
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
        second = (
            "2. Choose photos and tap Upload.\n" if product == "OneDrive" or complete_second else ""
        )
        body = f"# {product} mobile\n## Upload photos\n1. Open {product} on a phone.\n" + second
        # The saved source still publishes the second step: a candidate cannot
        # prove Dropbox merely by citing its first, true app-opening statement.
        if product == "Dropbox" and not complete_second:
            body += "2. Choose photos and tap Upload.\n"
        sources.append(
            SimpleNamespace(
                provider_source_id=product,
                run_id=rid,
                title=product,
                domain=url.split("/")[2],
                url=url,
                snippet=body,
            )
        )
    cats, entries = build_catalogs(sources)
    ids = [
        i
        for i, e in entries.items()
        if complete_second or e["unit"].source_id != "Dropbox" or e["unit"].step_index == 1
    ]
    fs = m.exact_facts(
        ids,
        entries,
        run_id=rid,
        project_id=pid,
        api=__import__("app.services.research_service", fromlist=["x"]),
        full_source_ids=set(cats),
    )
    s = spec(
        "Can OneDrive and Dropbox mobile manually upload photos?",
        kind="capability",
        platform="mobile",
        products=["OneDrive", "Dropbox"],
    )
    report = m.evaluate_support(s, [dict(requirement_id="r1", evidence_ids=ids)], fs, entries, cats)
    assert report["overall_completable"] == complete_second, report


@pytest.mark.parametrize(
    "changes",
    [
        dict(action="delete"),
        dict(object="files"),
        dict(mode="automatic"),
    ],
)
def test_original_action_object_and_mode_cannot_be_replaced(changes):
    r = requirement("q1") | dict(object="photos") | changes
    with pytest.raises(SpecError):
        compile_spec(
            "Can Google Drive Android users manually upload photos?",
            dict(requirements=[r], queries=[]),
        )


def test_how_question_cannot_be_downgraded_to_a_bare_capability():
    r = requirement("q1") | dict(kind="capability", object="photos")
    s = compile_spec(
        "How do Google Drive Android users upload photos with complete steps?",
        dict(requirements=[r], queries=[]),
    )
    assert s["unresolved"] or s["requirements"][0]["kind"] == "procedure"


def test_named_original_entry_cannot_disappear():
    r = requirement("q1") | dict(entry=None, object="photos")
    s = compile_spec(
        "How do Google Drive Android users upload photos from the Shared tab?",
        dict(requirements=[r], queries=[]),
    )
    assert s["unresolved"] or s["requirements"][0]["entry"] == "Shared tab"


def test_each_requested_product_needs_a_frozen_obligation():
    r = requirement("q1") | dict(products=["OneDrive"], platform="iPhone", object="photos")
    s = compile_spec(
        "Can OneDrive and Dropbox iPhone users manually upload photos?",
        dict(requirements=[r], queries=[]),
    )
    assert s["unresolved"] or any("Dropbox" in x["products"] for x in s["requirements"])


def test_move_condition_in_final_upload_step_is_not_a_move_workflow():
    m = module()
    body = "# OneDrive Android\n## Upload files\n1. Open OneDrive on Android.\n2. Tap Add and then Upload.\n3. Find and tap the files you want to upload.\n4. Find uploaded files in My Drive until you move them.\n## Move files\n1. Select files.\n2. Tap Move.\n"
    rid, pid, cats, entries = inventory(body)
    ids = [i for i, e in entries.items() if e["unit"].section == "Upload files"]
    s = spec("How do OneDrive Android users upload files?", platform="Android", object="files")
    report = m.evaluate_support(
        s,
        [dict(requirement_id="r1", evidence_ids=ids)],
        facts(m, ids, entries, rid, pid),
        entries,
        cats,
    )
    assert report["overall_completable"], report
    bad = list(entries)
    report = m.evaluate_support(
        s,
        [dict(requirement_id="r1", evidence_ids=bad)],
        facts(m, bad, entries, rid, pid),
        entries,
        cats,
    )
    assert not report["overall_completable"]


def test_chinese_dimensions_and_separate_product_branches_are_preserved():
    r = requirement("q1") | dict(object="photos")
    s = compile_spec("Google Drive 安卓用户如何手动上传照片？", dict(requirements=[r], queries=[]))
    assert not s["unresolved"]
    for change in [dict(action="delete"), dict(object="files"), dict(mode="automatic")]:
        with pytest.raises(SpecError):
            compile_spec(
                "Google Drive 安卓用户如何手动上传照片？",
                dict(requirements=[r | change], queries=[]),
            )
    q = "Compare Google Drive Android uploading photos and OneDrive iPhone moving files."
    one = r | dict(id="one")
    two = r | dict(
        id="two", products=["OneDrive"], platform="iPhone", action="move", object="files"
    )
    comparison = r | dict(
        id="comparison",
        kind="comparison",
        products=["Google Drive", "OneDrive"],
        platform="mobile",
        action=None,
        object=None,
        mode="unspecified",
        entry=None,
    )
    s = compile_spec(q, dict(requirements=[one, two, comparison], queries=[]))
    assert not s["unresolved"], s


def test_input_atoms_retain_exact_spans_independent_of_candidate_fields():
    q = "Google Drive 安卓用户如何手动上传照片？"
    r = requirement("q1") | dict(object="photos")
    s = compile_spec(q, dict(requirements=[r], queries=[]))
    atoms = s["input_atoms"]
    assert {"platform", "action", "object", "mode", "product", "output"} <= {
        a["dimension"] for a in atoms
    }
    assert all(q[a["start"] : a["end"]] == a["text"] for a in atoms)


@pytest.mark.parametrize(
    "change", [dict(action="delete"), dict(object="files"), dict(mode="automatic")]
)
def test_case_and_shared_product_modifier_cannot_erase_original_dimensions(change):
    r = requirement("q1") | dict(object="photos")
    with pytest.raises(SpecError):
        compile_spec(
            "Can Google Drive Android users MANUALLY UPLOAD PHOTOS?",
            dict(requirements=[r | change], queries=[]),
        )
    shared = "Can Google Drive and OneDrive Android users manually upload photos?"
    two = r | dict(id="two", products=["OneDrive"])
    with pytest.raises(SpecError):
        compile_spec(shared, dict(requirements=[r | change, two], queries=[]))


@pytest.mark.parametrize(
    "change", [dict(action=None), dict(object=None), dict(mode="unspecified"), dict(platform=None)]
)
def test_null_candidate_cannot_erase_explicit_original_requirement(change):
    r = requirement("q1") | dict(kind="capability", object="photos") | change
    s = compile_spec(
        "Can Google Drive Android users manually upload photos?", dict(requirements=[r], queries=[])
    )
    actual = s["requirements"][0]
    assert s["unresolved"] or (
        actual["action"]
        and actual["object"]
        and actual["mode"] == "manual"
        and actual["platform"] == "Android"
    )


@pytest.mark.parametrize("prefix", ["In", "in", "in the"])
def test_product_preposition_is_not_an_invented_named_ui_entry(prefix):
    r = requirement("q1") | dict(
        products=["Google Photos"], kind="capability", object="photos", entry=None
    )
    s = compile_spec(
        prefix + " Google Photos Android, can users upload photos?",
        dict(requirements=[r], queries=[]),
    )
    assert s["requirements"][0]["entry"] in (None, "app")


def test_numbered_upload_flow_member_can_contain_an_account_prerequisite():
    m = module()
    body = "# OneDrive iOS\n## Upload photos\n1. Create an account on iPhone.\n2. Choose photos and tap Upload.\n"
    rid, pid, cats, entries = inventory(body)
    ids = list(entries)
    s = spec("How do OneDrive iPhone users upload photos?")
    report = m.evaluate_support(
        s,
        [dict(requirement_id="r1", evidence_ids=ids)],
        facts(m, ids, entries, rid, pid),
        entries,
        cats,
    )
    assert report["overall_completable"], report


@pytest.mark.parametrize("change", [dict(action="remove"), dict(object="documents")])
def test_unknown_candidate_mapping_cannot_bypass_explicit_original_dimension(change):
    r = requirement("q1") | dict(object="photos") | change
    try:
        s = compile_spec(
            "Can Google Drive Android users upload photos?", dict(requirements=[r], queries=[])
        )
    except SpecError:
        return
    assert s["unresolved"]


def test_generic_app_cannot_erase_explicit_shared_tab():
    r = requirement("q1") | dict(object="photos", entry="app")
    s = compile_spec(
        "How do Google Drive Android users upload photos from the Shared tab?",
        dict(requirements=[r], queries=[]),
    )
    assert s["unresolved"] or s["requirements"][0]["entry"] == "Shared tab"


def test_each_action_object_pair_in_one_anchor_needs_an_obligation():
    q = "Can Google Drive Android users upload photos and delete files?"
    r = requirement("q1") | dict(kind="capability", object="photos")
    partial = compile_spec(q, dict(requirements=[r], queries=[]))
    assert partial["unresolved"]
    full = compile_spec(
        q, dict(requirements=[r, r | dict(id="r2", action="delete", object="files")], queries=[])
    )
    assert not full["unresolved"], full
    swapped = compile_spec(
        q,
        dict(
            requirements=[r | dict(object="files"), r | dict(id="r2", action="delete")], queries=[]
        ),
    )
    assert swapped["unresolved"]


def test_how_only_applies_to_its_own_anchor():
    q = "How do Google Drive Android users manually upload photos? Can Google Drive Android automatically upload photos?"
    one = requirement("q1") | dict(object="photos")
    two = requirement("q2") | dict(id="two", kind="capability", mode="automatic", object="photos")
    s = compile_spec(q, dict(requirements=[one, two], queries=[]))
    assert s["requirements"][1]["kind"] == "capability"


@pytest.mark.parametrize(
    "q,changes",
    [
        ("Can Google Drive Android users upload files and photos?", [dict(object="files")]),
        (
            "Can Google Drive Android users upload and delete photos?",
            [dict(action="upload", object="photos")],
        ),
        (
            "Can Google Drive Android users manually upload files and automatically delete photos?",
            [
                dict(action="upload", object="files", mode="automatic"),
                dict(action="delete", object="photos", mode="manual"),
            ],
        ),
    ],
)
def test_shared_and_paired_operations_cannot_disappear(q, changes):
    rs = [
        requirement("q1") | dict(id=f"r{i}", kind="capability") | c for i, c in enumerate(changes)
    ]
    try:
        s = compile_spec(q, dict(requirements=rs, queries=[]))
    except SpecError:
        return
    assert s["unresolved"]


def test_coordinated_actions_and_objects_have_positive_full_coverage():
    for q, pairs in [
        (
            "Can Google Drive Android users upload files and photos?",
            [("upload", "files"), ("upload", "photos")],
        ),
        (
            "Can Google Drive Android users upload and delete photos?",
            [("upload", "photos"), ("delete", "photos")],
        ),
    ]:
        rs = [
            requirement("q1") | dict(id=f"r{i}", kind="capability", action=a, object=o)
            for i, (a, o) in enumerate(pairs)
        ]
        s = compile_spec(q, dict(requirements=rs, queries=[]))
        assert not s["unresolved"], s


@pytest.mark.parametrize(
    "q",
    [
        "Can Google Drive Android users upload files, then delete photos?",
        "Google Drive 安卓用户能上传文件并删除照片吗？",
    ],
)
def test_every_action_position_counts_even_without_english_and(q):
    r = requirement("q1") | dict(kind="capability", object="files")
    s = compile_spec(q, dict(requirements=[r], queries=[]))
    assert s["unresolved"]
    full = compile_spec(
        q,
        dict(requirements=[r, r | dict(id="second", action="delete", object="photos")], queries=[]),
    )
    assert not full["unresolved"], full


def test_product_name_is_not_a_business_object():
    q = "Can Google Photos Android users upload videos?"
    r = requirement("q1") | dict(products=["Google Photos"], kind="capability", object="videos")
    s = compile_spec(q, dict(requirements=[r], queries=[]))
    assert not s["unresolved"], s
    product = next(a for a in s["input_atoms"] if a["dimension"] == "product")
    assert all(
        not (product["start"] < a["end"] and a["start"] < product["end"])
        for a in s["input_atoms"]
        if a["dimension"] == "object"
    )


def test_each_shared_product_needs_each_operation_not_diagonal_subset():
    q = "Can OneDrive and Dropbox mobile upload photos and delete files?"
    r = requirement("q1") | dict(
        kind="capability", products=["OneDrive"], platform="mobile", object="photos"
    )
    diagonal = [r, r | dict(id="b-delete", products=["Dropbox"], action="delete", object="files")]
    s = compile_spec(q, dict(requirements=diagonal, queries=[]))
    assert s["unresolved"]
    full = diagonal + [
        r | dict(id="a-delete", action="delete", object="files"),
        r | dict(id="b-upload", products=["Dropbox"]),
    ]
    assert not compile_spec(q, dict(requirements=full, queries=[]))["unresolved"]


def test_multi_product_atomic_requirement_can_match_all_owners_with_real_support_required():
    q = "Can OneDrive and Dropbox mobile manually upload photos?"
    r = requirement("q1") | dict(
        kind="capability", products=["OneDrive", "Dropbox"], platform="mobile", object="photos"
    )
    s = compile_spec(q, dict(requirements=[r], queries=[]))
    assert not s["unresolved"], s
    assert len(s["requirements"][0]["fulfills_obligation_ids"]) == 2


def test_action_positions_are_not_limited_to_a_separator_dictionary():
    q = "Google Drive 安卓用户能上传文件同时删除照片吗？"
    r = requirement("q1") | dict(kind="capability", object="files")
    s = compile_spec(q, dict(requirements=[r], queries=[]))
    assert s["unresolved"]
    s = compile_spec(
        q,
        dict(requirements=[r, r | dict(id="second", action="delete", object="photos")], queries=[]),
    )
    assert not s["unresolved"], s


def test_unspecified_cannot_erase_action_bound_leaf_modes():
    q = "Can Google Drive Android users manually upload files and automatically delete photos?"
    one = requirement("q1") | dict(kind="capability", mode="unspecified", object="files")
    two = one | dict(id="delete", action="delete", object="photos")
    s = compile_spec(q, dict(requirements=[one, two], queries=[]))
    assert s["unresolved"] or [r["mode"] for r in s["requirements"]] == ["manual", "automatic"]


def test_source_policy_action_words_are_not_business_obligations():
    q = "Using only official Google Drive upload files Help, can Google Drive Android delete photos?"
    r = requirement("q1") | dict(kind="capability", action="delete", object="photos")
    s = compile_spec(q, dict(requirements=[r], queries=[]))
    assert not s["unresolved"], s
    assert all(ob["anchor_id"] is not None for ob in s["input_obligations"])


def test_chinese_disabled_feature_clause_is_condition_not_a_requested_action():
    q = "Dropbox 移动端关闭相机上传（camera uploads）后，如何手动添加照片？"
    r = requirement("q1") | dict(
        products=["Dropbox"],
        platform="mobile",
        action="add",
        object="photos",
        condition_quotes=["关闭相机上传（camera uploads）后"],
    )
    s = compile_spec(q, dict(requirements=[r], queries=[]))
    assert not s["unresolved"], s
    assert {o["action_canon"] for o in s["input_obligations"]} == {"add"}


@pytest.mark.parametrize("complete", [False, True])
def test_each_product_has_each_requested_comparison_mode(complete):
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
                provider_source_id=product,
                run_id=rid,
                title=product,
                domain=url.split("/")[2],
                url=url,
                snippet=f"# {product} mobile\n## Manually upload photos\n1. Open {product} on a phone.\n2. Choose photos and tap Upload.\n## Automatically upload photos\n{product} can automatically upload photos on a phone.\n",
            )
        )
    cats, entries = build_catalogs(sources)
    ids = list(entries)
    fs = m.exact_facts(
        ids,
        entries,
        run_id=rid,
        project_id=pid,
        api=__import__("app.services.research_service", fromlist=["x"]),
        full_source_ids=set(cats),
    )
    base = requirement("q1") | dict(kind="capability", platform="mobile", object="photos")
    tasks = [
        base | dict(id=f"{p}-{mode}", products=[p], mode=mode)
        for p in ["OneDrive", "Dropbox"]
        for mode in ["manual", "automatic"]
    ]
    tasks.append(
        base | dict(id="compare", products=["OneDrive", "Dropbox"], kind="comparison", mode="mixed")
    )
    s = compile_spec(
        "Compare OneDrive and Dropbox mobile manual and automatic upload photos.",
        dict(requirements=tasks, queries=[]),
    )
    bindings = []
    for r in tasks[:-1]:
        if r["id"] == "OneDrive-automatic" and not complete:
            continue
        selected = [
            i
            for i, e in entries.items()
            if e["unit"].source_id == r["products"][0]
            and (
                (r["mode"] == "manual" and e["unit"].step_index)
                or (r["mode"] == "automatic" and "automatically" in e["unit"].text)
            )
        ]
        bindings.append(dict(requirement_id=r["id"], evidence_ids=selected))
    report = m.evaluate_support(s, bindings, fs, entries, cats)
    assert (report["items"][-1]["status"] == "covered") == complete, report


def test_source_operation_inflection_preserves_a_true_capability():
    m = module()
    rid, pid, cats, entries = inventory(
        "# OneDrive iOS\n## Automatically upload photos\nOneDrive automatically uploads photos on iPhone.\n"
    )
    ids = list(entries)
    s = spec(
        "Can OneDrive iPhone automatically upload photos?", kind="capability", mode="automatic"
    )
    report = m.evaluate_support(
        s,
        [dict(requirement_id="r1", evidence_ids=ids)],
        facts(m, ids, entries, rid, pid),
        entries,
        cats,
    )
    assert report["overall_completable"], report


def test_unrequested_cross_pair_is_locally_rejected_without_erasing_real_answers():
    m = module()
    body = "# OneDrive iOS\n## Upload files\n1. Open OneDrive on iPhone.\n2. Select files and tap Upload.\n## Delete photos\n1. Open OneDrive on iPhone.\n2. Select photos and tap Delete.\n## Upload photos\n1. Open OneDrive on iPhone.\n2. Select photos and tap Upload.\n"
    rid, pid, cats, entries = inventory(body)
    ids = list(entries)
    fs = facts(m, ids, entries, rid, pid)
    q = "Can OneDrive iPhone upload files and delete photos?"
    one = requirement("q1") | dict(
        id="upload-files",
        products=["OneDrive"],
        platform="iPhone",
        kind="capability",
        object="files",
    )
    two = one | dict(id="delete-photos", action="delete", object="photos")
    extra = one | dict(id="unrequested-cross-pair", object="photos")
    s = compile_spec(q, dict(requirements=[one, two, extra], queries=[]))
    bindings = [
        dict(
            requirement_id=r["id"],
            evidence_ids=[
                i
                for i, e in entries.items()
                if e["unit"].section == f"{r['action'].capitalize()} {r['object']}"
            ],
        )
        for r in [one, two, extra]
    ]
    report = m.evaluate_support(s, bindings, fs, entries, cats)
    delivered = {i for item in report["items"] for i in item["evidence_ids"]}
    unwanted = {i for i, e in entries.items() if e["unit"].section == "Upload photos"}
    assert not delivered & unwanted, report
    assert report["overall_completable"], report
    assert "unrequested-cross-pair" not in {r["id"] for r in s["requirements"]}
    assert any(r["id"] == "unrequested-cross-pair" for r in s["rejected_requirements"])


def test_general_comparison_null_axes_use_anchored_real_side_relations():
    m = module()
    rid, pid = uuid4(), uuid4()
    sources = []
    for p, url in [
        (
            "OneDrive",
            "https://support.microsoft.com/en-us/onedrive/manually-upload-files-or-photos-to-onedrive-in-ios",
        ),
        ("Dropbox", "https://help.dropbox.com/create-upload/upload-mobile"),
    ]:
        sources.append(
            SimpleNamespace(
                provider_source_id=p,
                run_id=rid,
                title=p,
                domain=url.split("/")[2],
                url=url,
                snippet=f"# {p} mobile\n## Upload photos\n1. Open {p} on a phone.\n2. Select photos and tap Upload.\n",
            )
        )
    cats, entries = build_catalogs(sources)
    ids = list(entries)
    fs = m.exact_facts(
        ids,
        entries,
        run_id=rid,
        project_id=pid,
        api=__import__("app.services.research_service", fromlist=["x"]),
        full_source_ids=set(cats),
    )
    base = requirement("q1") | dict(
        products=["OneDrive"], platform="mobile", kind="capability", object="photos"
    )
    rs = [
        base | dict(id="a"),
        base | dict(id="b", products=["Dropbox"]),
        base
        | dict(
            id="comparison",
            products=["OneDrive", "Dropbox"],
            kind="comparison",
            action=None,
            object=None,
        ),
    ]
    s = compile_spec(
        "Compare OneDrive and Dropbox mobile manual photo upload.",
        dict(requirements=rs, queries=[]),
    )
    bindings = [
        dict(
            requirement_id=r["id"],
            evidence_ids=[i for i, e in entries.items() if e["unit"].source_id == r["products"][0]],
        )
        for r in rs[:2]
    ]
    report = m.evaluate_support(s, bindings, fs, entries, cats)
    assert report["overall_completable"], report
    report = m.evaluate_support(s, bindings[:1], fs, entries, cats)
    assert report["items"][-1]["status"] != "covered", report


@pytest.mark.parametrize("field,value", [("platform", "mobile"), ("entry", "Other tab")])
def test_explicit_input_scope_cannot_be_weakened_by_nonempty_candidate(field, value):
    q = "Can OneDrive Android manually upload photos from the Shared tab?"
    r = requirement("q1") | dict(
        products=["OneDrive"],
        platform="Android",
        entry="Shared tab",
        object="photos",
        kind="capability",
    )
    r[field] = value
    try:
        s = compile_spec(q, dict(requirements=[r], queries=[]))
    except SpecError:
        return
    assert (
        s["unresolved"]
        or not s["requirements"]
        or (
            s["requirements"][0]["platform"] == "Android"
            and "shared tab" in s["requirements"][0]["entry"].casefold()
        )
    ), s


def test_correct_per_action_mode_pair_does_not_invent_cross_mode_obligation():
    q = "Can OneDrive Android manually upload files and automatically delete photos?"
    base = requirement("q1") | dict(products=["OneDrive"], kind="capability")
    tasks = [
        base | dict(id="u", object="files", mode="manual"),
        base | dict(id="d", action="delete", object="photos", mode="automatic"),
    ]
    s = compile_spec(q, dict(requirements=tasks, queries=[]))
    assert not s["unresolved"], s
    assert {
        (o["action_canon"], o["object_canon"], o["mode_canon"]) for o in s["input_obligations"]
    } == {("upload", "files", "manual"), ("delete", "photos", "automatic")}


def test_quantity_belongs_to_object_relation_instead_of_entire_anchor():
    q = "Can OneDrive Android upload one file and delete multiple photos?"
    base = requirement("q1") | dict(products=["OneDrive"], kind="capability", mode="unspecified")
    s = compile_spec(
        q,
        dict(
            requirements=[
                base | dict(id="u", object="files"),
                base | dict(id="d", action="delete", object="photos"),
            ],
            queries=[],
        ),
    )
    assert not s["unresolved"], s
    assert {r["id"]: r["mandatory_quantity"] for r in s["requirements"]} == {
        "u": "single",
        "d": "multiple",
    }


def test_explicit_platform_in_scope_remains_effective():
    q = "Can OneDrive manually upload photos?"
    r = requirement("q1") | dict(
        products=["OneDrive"], platform="mobile", object="photos", kind="capability"
    )
    try:
        s = compile_spec(q, dict(requirements=[r], queries=[]), scope="Android only")
    except SpecError:
        return
    assert s["unresolved"] or s["requirements"][0]["platform"] == "Android", s


def test_two_platforms_in_same_anchor_stay_attached_to_their_operation():
    q = "Can OneDrive Android upload photos and OneDrive iPhone delete files?"
    base = requirement("q1") | dict(products=["OneDrive"], kind="capability", mode="unspecified")
    correct = [
        base | dict(id="u", platform="Android", object="photos"),
        base | dict(id="d", platform="iPhone", action="delete", object="files"),
    ]
    s = compile_spec(q, dict(requirements=correct, queries=[]))
    assert not s["unresolved"], s
    wrong = [correct[0] | dict(platform="iPhone"), correct[1] | dict(platform="Android")]
    try:
        s = compile_spec(q, dict(requirements=wrong, queries=[]))
    except SpecError:
        return
    assert s["unresolved"] or not s["requirements"], s


def test_quantity_does_not_broadcast_between_objects_of_the_same_action():
    q = "Can OneDrive Android upload one file and multiple photos?"
    base = requirement("q1") | dict(products=["OneDrive"], kind="capability", mode="unspecified")
    s = compile_spec(
        q,
        dict(
            requirements=[
                base | dict(id="f", object="files"),
                base | dict(id="p", object="photos"),
            ],
            queries=[],
        ),
    )
    assert not s["unresolved"], s
    assert {r["id"]: r["mandatory_quantity"] for r in s["requirements"]} == {
        "f": "single",
        "p": "multiple",
    }


def test_two_entries_in_same_anchor_remain_legal_for_their_respective_actions():
    q = "Can OneDrive Android upload photos from the Shared tab and delete files from the Other tab?"
    base = requirement("q1") | dict(products=["OneDrive"], kind="capability", mode="unspecified")
    correct = [
        base | dict(id="u", entry="Shared tab", object="photos"),
        base | dict(id="d", entry="Other tab", action="delete", object="files"),
    ]
    s = compile_spec(q, dict(requirements=correct, queries=[]))
    assert not s["unresolved"], s
    try:
        s = compile_spec(
            q,
            dict(
                requirements=[
                    correct[0] | dict(entry="Other tab"),
                    correct[1] | dict(entry="Shared tab"),
                ],
                queries=[],
            ),
        )
    except SpecError:
        return
    assert s["unresolved"] or not s["requirements"], s


@pytest.mark.parametrize(
    "scope,field,expected",
    [
        ("Manual uploads only", "mode", "manual"),
        ("Multiple photos in one upload", "mandatory_quantity", "multiple"),
    ],
)
def test_scope_mode_and_quantity_are_effective_original_constraints(scope, field, expected):
    q = "Can OneDrive Android upload photos?"
    r = requirement("q1") | dict(
        products=["OneDrive"], object="photos", kind="capability", mode="unspecified"
    )
    s = compile_spec(q, dict(requirements=[r], queries=[]), scope=scope)
    assert s["unresolved"] or s["requirements"][0][field] == expected, s


def test_null_platform_and_generic_entry_fill_from_each_matched_relation():
    q = "Can OneDrive Android upload photos from the Shared tab and OneDrive iPhone delete files from the Other tab?"
    base = requirement("q1") | dict(
        products=["OneDrive"], kind="capability", mode="unspecified", platform=None, entry="app"
    )
    s = compile_spec(
        q,
        dict(
            requirements=[
                base | dict(id="u", object="photos"),
                base | dict(id="d", action="delete", object="files"),
            ],
            queries=[],
        ),
    )
    assert not s["unresolved"], s
    by_id = {r["id"]: r for r in s["requirements"]}
    assert by_id["u"]["platform"] == "Android"
    assert by_id["d"]["platform"] in {"iPhone", "iOS"}
    assert "shared tab" in by_id["u"]["entry"].casefold()
    assert "other tab" in by_id["d"]["entry"].casefold()


@pytest.mark.parametrize(
    "scope,updates",
    [("Manual uploads only", dict(mode="automatic")), ("Android only", dict(platform="iPhone"))],
)
def test_filled_candidate_cannot_override_scope_constraint(scope, updates):
    q = "Can OneDrive upload photos?"
    r = (
        requirement("q1")
        | dict(
            products=["OneDrive"],
            platform=None,
            object="photos",
            kind="capability",
            mode="unspecified",
        )
        | updates
    )
    try:
        s = compile_spec(q, dict(requirements=[r], queries=[]), scope=scope)
    except SpecError:
        return
    assert s["unresolved"] or not s["requirements"], s


@pytest.mark.parametrize(
    "scope", ["Automatic uploads only", "iPhone only", "Multiple photos in one upload"]
)
def test_question_scope_conflict_is_visible_instead_of_ignored(scope):
    q = "Can OneDrive Android manually upload one photo?"
    r = requirement("q1") | dict(products=["OneDrive"], object="photos", kind="capability")
    try:
        s = compile_spec(q, dict(requirements=[r], queries=[]), scope=scope)
    except SpecError:
        return
    assert s["unresolved"], s


def test_one_wide_candidate_cannot_consume_two_different_device_relations():
    q = "Can OneDrive Android upload photos and OneDrive iPhone upload photos?"
    r = requirement("q1") | dict(
        products=["OneDrive"], platform=None, object="photos", kind="capability", mode="unspecified"
    )
    try:
        s = compile_spec(q, dict(requirements=[r], queries=[]))
    except SpecError:
        return
    assert s["unresolved"], s


def test_named_entry_in_scope_is_not_ignored():
    q = "Can OneDrive Android upload photos?"
    r = requirement("q1") | dict(
        products=["OneDrive"], object="photos", kind="capability", mode="unspecified"
    )
    s = compile_spec(q, dict(requirements=[r], queries=[]), scope="from the Shared tab only")
    assert s["unresolved"] or "shared tab" in s["requirements"][0]["entry"].casefold(), s


@pytest.mark.parametrize("dimension", ["platform", "entry"])
def test_same_operation_distinct_scope_relations_can_be_correctly_split(dimension):
    q = (
        "Can OneDrive Android upload photos and OneDrive iPhone upload photos?"
        if dimension == "platform"
        else "Can OneDrive Android upload photos from the Shared tab and upload photos from the Other tab?"
    )
    base = requirement("q1") | dict(
        products=["OneDrive"], kind="capability", object="photos", mode="unspecified"
    )
    tasks = (
        [base | dict(id="a", platform="Android"), base | dict(id="b", platform="iPhone")]
        if dimension == "platform"
        else [base | dict(id="a", entry="Shared tab"), base | dict(id="b", entry="Other tab")]
    )
    s = compile_spec(q, dict(requirements=tasks, queries=[]))
    assert not s["unresolved"], s
    assert {r["id"] for r in s["requirements"]} == {"a", "b"}


@pytest.mark.parametrize(
    "quantities", [["unspecified"], ["single"], ["multiple"], ["single", "multiple"]]
)
def test_repeated_same_object_quantity_relations_keep_distinct_obligations(quantities):
    q = "Can OneDrive Android upload one photo and upload multiple photos?"
    base = requirement("q1") | dict(
        products=["OneDrive"], kind="capability", object="photos", mode="unspecified"
    )
    tasks = [base | dict(id=f"r{i}", quantity=value) for i, value in enumerate(quantities)]
    s = compile_spec(q, dict(requirements=tasks, queries=[]))
    if len(quantities) == 2:
        assert not s["unresolved"], s
        assert {r["id"]: r["mandatory_quantity"] for r in s["requirements"]} == {
            "r0": "single",
            "r1": "multiple",
        }
        assert all(len(r["fulfills_obligation_ids"]) == 1 for r in s["requirements"])
    else:
        assert s["unresolved"], s


@pytest.mark.parametrize("platforms", [["Android"], ["Android", "iPhone"]])
def test_shared_action_still_requires_every_explicit_device(platforms):
    q = "Can OneDrive Android and iPhone upload photos?"
    base = requirement("q1") | dict(
        products=["OneDrive"], kind="capability", object="photos", mode="unspecified"
    )
    tasks = [base | dict(id=f"r{i}", platform=value) for i, value in enumerate(platforms)]
    s = compile_spec(q, dict(requirements=tasks, queries=[]))
    if len(platforms) == 2:
        assert not s["unresolved"], s
        assert all(len(r["fulfills_obligation_ids"]) == 1 for r in s["requirements"])
    else:
        assert s["unresolved"], s


def test_quantity_relation_provenance_slices_original_text_exactly():
    q = "Can OneDrive Android select multiple photos and start backup?"
    r = requirement("q1") | dict(
        products=["OneDrive"],
        kind="capability",
        action="select and backup",
        object="photos",
        quantity="multiple",
    )
    s = compile_spec(q, dict(requirements=[r], queries=[]))
    assert not s["unresolved"], s
    qs = [a for o in s["input_obligations"] for a in o.get("quantity_scope", [])]
    assert qs
    for atom in qs:
        assert atom["source"] == "question"
        assert q[atom["start"] : atom["end"]] == atom["text"]
        assert atom["canonical"] == "multiple"
