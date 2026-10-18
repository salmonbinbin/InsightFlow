"""Phase 8 — Research task / evidence / coverage contract tests (Batch A r2).

The previous revision failed five architectural cases; this
revision freezes the new contract:

  1. duplicate same EvidenceUnit twice cannot complete multi-step;
  2. two steps from different sources cannot complete;
  3. "Manually select photos and upload." + "There are 3 storage
     settings." is NOT multi-select;
  4. "while offline" condition is preserved as unresolved_clause
     and blocks completion;
  5. "state any limits" is CORE (not auxiliary); no evidence
     names the same limit -> NOT_VERIFIED -> not complete.

The positive case uses a real Markdown snapshot with complete
numbered steps parsed by :func:`build_evidence_catalog`. The
literal ``\\n`` case exercises :func:`build_evidence_unit` as
an isolated citation (no procedure eligibility).
"""

from __future__ import annotations

import pytest

from app.services.research_contract import (
    KIND_UNRESOLVED,
    MAX_CITATION_CHARS,
    ROLE_AUXILIARY,
    ROLE_CORE,
    Catalog,
    ContractError,
    CoverageStatus,
    ParseStatus,
    ProcedureStatus,
    build_evidence_catalog,
    build_evidence_unit,
    build_task_plan,
    evaluate_coverage,
    extract_citation,
)

# ============================================================================
# Synthetic Markdown snapshot for the positive case
# ============================================================================


ONEDRIVE_IOS_MD = (
    "# Microsoft OneDrive Support\n"
    "## Upload multiple photos on iOS\n"
    "1. Open the OneDrive app on your iPhone or iPad.\n"
    "2. Tap the plus button and then choose Upload.\n"
    "3. Select multiple photos from your photo library and tap Open.\n"
)

ONEDRIVE_LIMITS_MD = (
    "# Microsoft OneDrive Support\n"
    "## Upload limits on iOS\n"
    "1. Open the OneDrive app on your iPhone or iPad.\n"
    "2. The maximum file size is 100 GB per upload.\n"
)


# ============================================================================
# Frozen case 1 — duplicate same EvidenceUnit twice
# ============================================================================


def test_duplicate_same_unit_cannot_complete_procedure() -> None:
    """Selecting the same catalog unit twice does not satisfy a
    multi-step procedure. The selection is rejected as a
    duplicate step_index, NOT collapsed to a 1-step coverage.
    """
    question = (
        "According to official Microsoft Support, what are the "
        "steps to upload photos to OneDrive on iOS?"
    )
    plan = build_task_plan(question)
    assert plan.parse_status is ParseStatus.PARSED
    assert any(i.kind == "procedure" for i in plan.core_items)

    catalog = build_evidence_catalog("onedrive-ios", ONEDRIVE_IOS_MD)
    units = catalog.units
    assert len(units) == 3
    first_id = units[0].evidence_id
    selected = (first_id, first_id)

    report = evaluate_coverage(
        plan,
        units,
        catalog=catalog,
        selected_evidence_ids=selected,
    )

    proc = [c for c in report.items if c.kind == "procedure"][0]
    assert proc.status is CoverageStatus.NOT_VERIFIED
    assert "duplicate" in proc.gap
    assert report.procedure_status is ProcedureStatus.INSUFFICIENT
    assert report.overall_completable is False


# ============================================================================
# Frozen case 2 — two steps from different sources
# ============================================================================


def test_cross_source_steps_cannot_complete_procedure() -> None:
    """Steps from two different source_ids are rejected.

    The procedure requirement binds to a single source's
    section. A selection that mixes units from source A and
    source B is not a credible procedure.
    """
    question = (
        "According to official Microsoft Support, what are the "
        "steps to upload photos to OneDrive on iOS?"
    )
    plan = build_task_plan(question)

    catalog_a = build_evidence_catalog("source-a", ONEDRIVE_IOS_MD)
    catalog_b = build_evidence_catalog("source-b", ONEDRIVE_IOS_MD)
    units = catalog_a.units + catalog_b.units
    selected = (catalog_a.units[0].evidence_id, catalog_b.units[0].evidence_id)

    report = evaluate_coverage(
        plan,
        units,
        catalog=catalog_a,
        selected_evidence_ids=selected,
    )

    proc = [c for c in report.items if c.kind == "procedure"][0]
    assert proc.status is CoverageStatus.NOT_VERIFIED
    assert "different source" in proc.gap or "single source" in proc.gap
    assert report.overall_completable is False


# ============================================================================
# Frozen case 3 — storage number is NOT a multi-select quantifier
# ============================================================================


def test_storage_number_is_not_multi_select_quantifier() -> None:
    """A storage number (``3 storage settings``) and an
    action-only sentence (``Manually select photos and upload.``)
    do NOT satisfy a multi-select requirement. Only the v14
    quantifiers (multiple / several / multi-select / batch) count.
    """
    question = (
        "According to official OneDrive Help, what are the upload "
        "choices when uploading multiple photos to OneDrive on iOS?"
    )
    plan = build_task_plan(question)
    assert plan.parse_status is ParseStatus.PARSED
    assert any(i.kind == "multi_select" for i in plan.items)
    assert any(i.kind == "choices" for i in plan.items)

    snapshot = (
        "Manually select photos and upload to OneDrive.\n"
        "There are 3 storage settings to choose from.\n"
    )
    evidence = (
        build_evidence_unit(
            snapshot=snapshot,
            source_id="onedrive-help",
            offset=snapshot.find("Manually select"),
            length=len("Manually select photos and upload to OneDrive."),
            section="manual upload",
            context="OneDrive manual upload",
        ),
        build_evidence_unit(
            snapshot=snapshot,
            source_id="onedrive-help",
            offset=snapshot.find("There are 3"),
            length=len("There are 3 storage settings to choose from."),
            section="storage settings",
            context="OneDrive storage settings",
        ),
    )

    report = evaluate_coverage(plan, evidence)

    multi = [c for c in report.items if c.kind == "multi_select"][0]
    assert multi.status is CoverageStatus.NOT_VERIFIED
    # The question is non-procedure so the multi_select item is
    # routed through the review-required gate. String co-occurrence
    # alone can never auto-complete it.
    assert "review required" in multi.gap.lower()
    # And the choices requirement is not satisfied without an
    # enumeration.
    choices = [c for c in report.items if c.kind == "choices"]
    if choices:
        assert choices[0].status is not CoverageStatus.COVERED
    assert report.overall_completable is False


# ============================================================================
# Frozen case 4 — "while offline" condition is unresolved and blocks
# ============================================================================


def test_while_offline_condition_blocks_completion() -> None:
    """A conditional clause (``while offline``) is preserved as
    :data:`KIND_UNRESOLVED` CORE. The plan still parses, but
    ``evaluate_coverage`` reports the unresolved clause and
    overall_completable is False — the condition is NOT
    silently dropped.
    """
    question = (
        "According to official Microsoft Support, what are the "
        "steps to upload photos to OneDrive on iOS while offline?"
    )
    plan = build_task_plan(question)
    assert plan.parse_status is ParseStatus.PARSED
    unresolved = [i for i in plan.items if i.kind == KIND_UNRESOLVED]
    assert unresolved, "plan must keep the conditional clause as unresolved"
    assert any("while offline" in i.value for i in unresolved)
    assert plan.is_completable_target is False

    catalog = build_evidence_catalog("onedrive-ios", ONEDRIVE_IOS_MD)
    selected = tuple(u.evidence_id for u in catalog.units)

    report = evaluate_coverage(
        plan,
        catalog.units,
        catalog=catalog,
        selected_evidence_ids=selected,
    )

    assert report.overall_completable is False
    assert report.procedure_status is ProcedureStatus.INSUFFICIENT
    assert any("unresolved clause" in r for r in report.failure_reasons)
    # The unresolved item is reported as NOT_VERIFIED with the
    # clause value in the gap; it must NOT be silently COVERED.
    unresolved_items = [c for c in report.items if c.kind == KIND_UNRESOLVED]
    assert unresolved_items
    assert all(c.status is not CoverageStatus.COVERED for c in unresolved_items)


# ============================================================================
# Frozen case 5 — "state any limits" is CORE, not auxiliary
# ============================================================================


def test_state_any_limits_is_core_not_auxiliary() -> None:
    """The question asks for limits. The contract emits a
    non-auxiliary ``limit`` requirement (CORE for the narrow
    procedure grammar; REVIEW otherwise). Limits are NEVER
    silently demoted to auxiliary, and absence of matching
    evidence is reported as NOT_VERIFIED. The contract does
    NOT upgrade ``no evidence`` to ``no limit``.
    """
    question = (
        "According to official Microsoft Support, please state any "
        "limits on uploading to OneDrive on iOS."
    )
    plan = build_task_plan(question)
    assert plan.parse_status is ParseStatus.PARSED
    limit_items = [i for i in plan.items if i.kind == "limit"]
    assert limit_items
    assert all(i.role != ROLE_AUXILIARY for i in limit_items), (
        "limits question must emit a non-auxiliary role (CORE or REVIEW), never ROLE_AUXILIARY"
    )

    # Evidence that mentions the operation but NOT the limit.
    catalog = build_evidence_catalog("onedrive-ios", ONEDRIVE_IOS_MD)
    selected = tuple(u.evidence_id for u in catalog.units)
    report = evaluate_coverage(
        plan,
        catalog.units,
        catalog=catalog,
        selected_evidence_ids=selected,
    )

    limit = [c for c in report.items if c.kind == "limit"][0]
    assert limit.status is CoverageStatus.NOT_VERIFIED
    # Non-procedure (including limits) is now routed through the
    # review gate; gap names the review-required reason.
    assert "review required" in limit.gap.lower()
    assert report.overall_completable is False


# ============================================================================
# Frozen case 6 — literal backslash-n in snapshot is preserved
# ============================================================================


def test_literal_backslash_n_in_snapshot_is_preserved_not_unescaped() -> None:
    """Literal backslash-n must NOT be silently turned into a
    newline. The substring returned by ``build_evidence_unit``
    keeps both characters; ``len(text)`` counts both. A naive
    ``.replace('\\n', '\\n')`` would shorten the string and
    break the multi-select / manual quantifier match.
    """
    snapshot = "Hello\\nWorld"  # 12 chars: H e l l o \ n W o r l d
    assert len(snapshot) == 12

    ev = build_evidence_unit(
        snapshot=snapshot,
        source_id="escape-test",
        offset=0,
        length=12,
        section="escape test",
        context="literal backslash-n",
    )
    assert ev.length == 12
    assert len(ev.text) == 12
    assert ev.text == "Hello\\nWorld"
    assert extract_citation(ev) == "Hello\\nWorld"
    assert "\n" not in snapshot
    assert ev.text.count("\\n") == 1

    cap_snapshot = "x" * MAX_CITATION_CHARS
    cap_ev = build_evidence_unit(
        snapshot=cap_snapshot,
        source_id="cap",
        offset=0,
        length=MAX_CITATION_CHARS,
        section="cap",
        context="cap",
    )
    assert len(cap_ev.text) == MAX_CITATION_CHARS


# ============================================================================
# Frozen case 7 — isolated unit cannot satisfy a procedure
# ============================================================================


def test_isolated_unit_cannot_complete_procedure() -> None:
    """A unit built via :func:`build_evidence_unit` (no catalog)
    is isolated: it has no catalog_id. The procedure evaluator
    rejects isolated units even if their ``evidence_id`` is
    listed in ``selected_evidence_ids``.
    """
    question = (
        "According to official Microsoft Support, what are the "
        "steps to upload photos to OneDrive on iOS?"
    )
    plan = build_task_plan(question)

    catalog = build_evidence_catalog("onedrive-ios", ONEDRIVE_IOS_MD)
    snapshot = ONEDRIVE_IOS_MD
    isolated = build_evidence_unit(
        snapshot=snapshot,
        source_id="onedrive-ios",
        offset=snapshot.find("1. Open"),
        length=len("1. Open the OneDrive app on your iPhone or iPad."),
        section="OneDrive iOS upload",
        context="OneDrive iOS upload steps",
    )
    assert isolated.is_isolated
    assert not isolated.catalog_id

    report = evaluate_coverage(
        plan,
        (isolated,),
        catalog=catalog,
        selected_evidence_ids=(isolated.evidence_id,),
    )

    proc = [c for c in report.items if c.kind == "procedure"][0]
    assert proc.status is CoverageStatus.NOT_VERIFIED
    assert "isolated" in proc.gap
    assert report.overall_completable is False


# ============================================================================
# Frozen case 8 — legitimate real-Markdown procedure positive
# ============================================================================


def test_legitimate_real_markdown_procedure_is_completable() -> None:
    """Positive case. The snapshot is a synthetic Markdown page
    with an h1, an h2, and three numbered steps. The catalog
    derives 3 units with step_total=3 from the bytes. The
    question is a known single-product manual step question
    that parses as a procedure. Selecting all 3 steps
    yields a complete procedure.
    """
    question = (
        "According to official Microsoft Support, what are the "
        "steps to upload multiple photos to OneDrive on iOS?"
    )
    plan = build_task_plan(question)
    assert plan.parse_status is ParseStatus.PARSED
    kinds = {i.kind for i in plan.core_items}
    assert {"product", "platform", "operation", "multi_select", "procedure"} <= kinds
    assert plan.unresolved_clauses == ()
    assert plan.is_completable_target is True

    catalog = build_evidence_catalog("onedrive-ios", ONEDRIVE_IOS_MD)
    assert len(catalog.units) == 3
    assert all(u.step_index in (1, 2, 3) for u in catalog.units)
    assert all(u.step_total == 3 for u in catalog.units)
    assert all(
        u.section_path == ("Microsoft OneDrive Support", "Upload multiple photos on iOS")
        for u in catalog.units
    )
    assert len(catalog.snapshot_sha256) == 64

    selected = tuple(u.evidence_id for u in catalog.units)
    report = evaluate_coverage(
        plan,
        catalog.units,
        catalog=catalog,
        selected_evidence_ids=selected,
    )

    assert report.parse_status is ParseStatus.PARSED
    assert report.procedure_status is ProcedureStatus.COMPLETE
    assert report.overall_completable is True, (
        f"positive: legitimate flow must be completable; reasons={report.failure_reasons!r}"
    )
    for c in report.items:
        if c.role == ROLE_CORE:
            assert c.status is CoverageStatus.COVERED, (
                f"positive: core {c.kind}={c.requirement_id} not covered; gap={c.gap!r}"
            )
    # Procedure evidence must be all three catalog units.
    proc = [c for c in report.items if c.kind == "procedure"][0]
    assert len(proc.evidence_ids) == 3
    assert proc.evidence_source_ids == ("onedrive-ios",)


# ============================================================================
# Defensive — grammar / catalog / invalid input
# ============================================================================


def test_empty_question_is_unparseable_empty() -> None:
    plan = build_task_plan("   ")
    assert plan.parse_status is ParseStatus.UNPARSEABLE_EMPTY
    assert plan.items == ()
    assert plan.is_completable_target is False


def test_multi_product_question_is_unparseable_multi_intent() -> None:
    plan = build_task_plan("Compare Google Drive and Dropbox upload behaviour on iOS.")
    assert plan.parse_status is ParseStatus.UNPARSEABLE_MULTI_INTENT
    assert plan.items == ()


def test_unknown_question_is_unparseable_unknown() -> None:
    plan = build_task_plan("Tell me a joke.")
    assert plan.parse_status is ParseStatus.UNPARSEABLE_UNKNOWN
    assert plan.items == ()


def test_unparseable_plan_never_completes_even_with_evidence() -> None:
    plan = build_task_plan("")
    catalog = build_evidence_catalog("x", ONEDRIVE_IOS_MD)
    report = evaluate_coverage(
        plan,
        catalog.units,
        catalog=catalog,
        selected_evidence_ids=tuple(u.evidence_id for u in catalog.units),
    )
    assert report.parse_status is ParseStatus.UNPARSEABLE_EMPTY
    assert report.overall_completable is False
    assert report.items == ()
    assert any("plan not parsable" in r for r in report.failure_reasons)


def test_build_evidence_unit_rejects_invalid_inputs() -> None:
    with pytest.raises(ContractError):
        build_evidence_unit(snapshot="short", source_id="x", offset=0, length=10)
    with pytest.raises(ContractError):
        build_evidence_unit(snapshot="short", source_id="x", offset=-1, length=1)
    with pytest.raises(ContractError):
        build_evidence_unit(snapshot="short", source_id="x", offset=0, length=0)
    with pytest.raises(ContractError):
        build_evidence_unit(snapshot="short", source_id="", offset=0, length=1)
    with pytest.raises(ContractError):
        build_evidence_unit(
            snapshot="x" * (MAX_CITATION_CHARS + 1),
            source_id="x",
            offset=0,
            length=MAX_CITATION_CHARS + 1,
        )


def test_build_evidence_catalog_rejects_invalid_inputs() -> None:
    with pytest.raises(ContractError):
        build_evidence_catalog("", "# h\n1. a\n")
    with pytest.raises(ContractError):
        build_evidence_catalog("src", 123)  # type: ignore[arg-type]


def test_procedure_partial_when_fewer_steps_selected() -> None:
    """Selecting steps 1 and 2 of a 3-step procedure is PARTIAL,
    not COVERED. The contract requires 1..N of the section's
    step_total.
    """
    question = (
        "According to official Microsoft Support, what are the "
        "steps to upload photos to OneDrive on iOS?"
    )
    plan = build_task_plan(question)
    catalog = build_evidence_catalog("onedrive-ios", ONEDRIVE_IOS_MD)
    selected = (catalog.units[0].evidence_id, catalog.units[1].evidence_id)
    report = evaluate_coverage(
        plan,
        catalog.units,
        catalog=catalog,
        selected_evidence_ids=selected,
    )
    proc = [c for c in report.items if c.kind == "procedure"][0]
    assert proc.status is CoverageStatus.PARTIAL
    assert "missing step(s) [3] of 3" in proc.gap
    assert report.procedure_status is ProcedureStatus.PARTIAL
    assert report.overall_completable is False


def test_procedure_without_catalog_is_insufficient() -> None:
    question = (
        "According to official Microsoft Support, what are the "
        "steps to upload photos to OneDrive on iOS?"
    )
    plan = build_task_plan(question)
    catalog = build_evidence_catalog("onedrive-ios", ONEDRIVE_IOS_MD)
    report = evaluate_coverage(
        plan,
        catalog.units,
        selected_evidence_ids=tuple(u.evidence_id for u in catalog.units),
    )
    proc = [c for c in report.items if c.kind == "procedure"][0]
    assert proc.status is CoverageStatus.NOT_VERIFIED
    assert "catalog" in proc.gap
    assert report.overall_completable is False


def test_procedure_without_selection_is_insufficient() -> None:
    question = (
        "According to official Microsoft Support, what are the "
        "steps to upload photos to OneDrive on iOS?"
    )
    plan = build_task_plan(question)
    catalog = build_evidence_catalog("onedrive-ios", ONEDRIVE_IOS_MD)
    report = evaluate_coverage(plan, catalog.units, catalog=catalog)
    proc = [c for c in report.items if c.kind == "procedure"][0]
    assert proc.status is CoverageStatus.NOT_VERIFIED
    assert "selected_evidence_ids" in proc.gap
    assert report.overall_completable is False


def test_snapshot_with_no_numbered_steps_yields_empty_catalog() -> None:
    """A Markdown page with only h1 / paragraphs and no
    numbered list produces a Catalog with sections but no
    units. The catalog hash and section path are still
    populated; the procedure requirement cannot be completed.
    """
    md = "# Title\nJust some prose with no numbered list.\n"
    catalog = build_evidence_catalog("plain", md)
    assert isinstance(catalog, Catalog)
    assert catalog.sections
    assert catalog.units == ()
    assert len(catalog.snapshot_sha256) == 64
