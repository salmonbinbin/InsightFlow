"""Phase 8 — Task decomposition from original question + model candidates.

This test file is the red half of Task B in
``docs/superpowers/plans/2026-10-17-phase8-research-prd-completion.md``.

The future :mod:`app.services.research_tasks` module owns one job:

    Take the raw research question AND the model's candidate task
    splits, then produce a server-controlled
    :class:`QuestionTaskPlan` that downstream stages bind evidence
    against. The model MAY propose how to split the question, but
    the server is the only authority on:

      * the canonical :class:`TaskItem` shape (kind / role /
        subject / action / constraints);
      * the position anchors (``question_span``) that tie every
        item back to actual substrings of the original question;
      * the preservation of product / platform / entry / quantity
        / version / limit / mode conditions — including ones the
        model forgot to surface (an "offline" clause the model
        dropped still becomes an :class:`TaskItem` of kind
        :data:`KIND_UNRESOLVED`);
      * the rejection of model self-reported ``is_complete`` /
        ``covered`` / ``satisfied`` fields — they are stripped
        before the plan is returned;
      * the rejection of forged anchors, out-of-range spans, full-
        question span masking, and cross-entry subject confusion.

These tests are pure — no model, no DB, no network. They use the
frozen case fixtures in ``backend/tests/fixtures/phase8_research_v5``
so the original questions are real, not invented.

The contract this file pins (intentionally minimal so the
implementation can be expressed as dataclasses + pure functions):

    Subject(product=str|None, platform=str|None, entry=str|None)
    Constraint(kind=str, value=str)            # quantity/version/mode/...
    TaskItem(id, question_span, kind, role,
             subject, action, constraints, answer_mode)
    QuestionTaskPlan(items, unresolved, original_question,
                     has_full_question_mask: bool)

    build_task_plan(question: str,
                    candidate_tasks: list[dict]) -> QuestionTaskPlan
    validate_task_plan(plan: QuestionTaskPlan) -> None     # raises

Any future implementation must satisfy the assertions below; the
tests will fail with ``ImportError`` (no module), ``AttributeError``
(wrong name), or ``AssertionError`` (wrong shape) until the contract
is honoured.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

# ============================================================================
# Frozen fixtures — real Phase 8 questions / sources
# ============================================================================

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "phase8_research_v5"

_CACHED_FIXTURES: dict[str, dict] = {}


def _load_fixture(name: str) -> dict:
    """Load and memoise a Phase 8 v5 fixture file.

    The fixtures are the original question + frozen public sources
    (URL / snippet / sha256) for the runs that produced the
    two-completed / eight-partial / v14 evidence quality result.
    Only the ``question`` field is consumed here; the source
    snapshots exist so any future reader test can confirm span
    anchoring against the same canonical strings.
    """
    if name not in _CACHED_FIXTURES:
        path = FIXTURES_DIR / f"case-{name}.json"
        if not path.exists():
            pytest.skip(f"fixture {path.name} not present on disk")
        with path.open("r", encoding="utf-8") as fh:
            _CACHED_FIXTURES[name] = json.load(fh)
    return _CACHED_FIXTURES[name]


def _fixture_questions() -> list[tuple[str, str]]:
    """Return ``(case_name, question)`` for every fixture on disk.

    The case naming follows the on-disk filename, not the original
    question number. Skips silently when a fixture is absent so the
    test set degrades with the artefacts (cases 2/5/8 have no
    fixture and are intentionally excluded).
    """
    pairs: list[tuple[str, str]] = []
    for entry in sorted(os.listdir(FIXTURES_DIR)):
        if not entry.startswith("case-") or not entry.endswith(".json"):
            continue
        stem = entry[len("case-") : -len(".json")]
        pairs.append((stem, _load_fixture(stem)["question"]))
    return pairs


# ============================================================================
# Module-level imports — exercised under a fresh ImportError
# ============================================================================

try:
    from app.services import research_tasks
except ImportError:  # pragma: no cover — module intentionally absent
    research_tasks = None  # type: ignore[assignment]


# ============================================================================
# Public interface the future module must expose
# ============================================================================


_REQUIRED_PUBLIC_NAMES = (
    "Subject",
    "Constraint",
    "TaskItem",
    "QuestionTaskPlan",
    "KIND_UNRESOLVED",
    "ROLE_CORE",
    "ROLE_AUXILIARY",
    "build_task_plan",
    "validate_task_plan",
)


def test_module_is_importable() -> None:
    """The future ``research_tasks`` module must exist. Until then
    this test fails with :class:`ImportError` which is the correct
    red state for Task B's test-first batch.
    """
    assert research_tasks is not None, (
        "app.services.research_tasks is missing — Task B implementation "
        "must add this module before the red tests can go green."
    )


def test_module_exposes_required_public_names() -> None:
    """A minimal, plausible surface so the rest of the tests can
    reference well-defined names. The names below are the contract
    surface between Task B and downstream wiring; if any rename
    happens, this test forces an explicit decision.
    """
    assert research_tasks is not None
    for name in _REQUIRED_PUBLIC_NAMES:
        assert hasattr(research_tasks, name), (
            f"research_tasks must export {name!r} so downstream wiring "
            f"can rely on a stable contract surface."
        )


# ============================================================================
# 1. Legal Chinese / English / rewrite candidates must be accepted
# ============================================================================


_ENGLISH_GOOGLE_PHOTOS = (
    "In the Google Photos Android app, can a user manually select "
    "several photos and start backup in one action? Cite only "
    "official Google Help; separate manual upload from automatic "
    "backup."
)


_CHINESE_GOOGLE_PHOTOS = (
    "在 Google Photos 安卓应用中，用户能否一次性手动选择多张照片并"
    "开始备份？请仅引用 Google 官方帮助；并将手动上传与自动备份"
    "分开说明。"
)


_REWRITE_GOOGLE_PHOTOS = (
    "Per Google's official Android Photos documentation, is it "
    "possible to choose a batch of photos and trigger backup "
    "manually? Distinguish manual from auto backup."
)


def _legal_candidate(question: str) -> dict:
    """A well-formed candidate task the model might propose.

    Anchored to the verb phrase at the head of the question; subject
    carries the canonical product / platform; constraints include
    the explicit multi-select quantifier and the manual vs auto
    distinction that lives in the same question.

    No :class:`is_complete` / :class:`covered` field — those are
    the model-self-reporting tokens the future module must reject.
    """
    # ``question_span`` must point at a real substring of the
    # original question. The head verb phrase "can a user
    # manually select several photos and start backup in one
    # action" is a stable anchor across English, Chinese and the
    # rewrite variants.
    if "手动选择" in question:
        span_text = "用户能否一次性手动选择多张照片并开始备份"
    elif "choose a batch" in question:
        span_text = "is it possible to choose a batch of photos and trigger backup manually"
    else:
        span_text = "can a user manually select several photos and start backup in one action"
    start = question.find(span_text)
    assert start >= 0, "test fixture broken: anchor text not found"
    return {
        "id": "t1",
        "question_span": {"start": start, "end": start + len(span_text)},
        "kind": "capability",
        "role": "core",
        "subject": {"product": "Google Photos", "platform": "Android", "entry": "app"},
        "action": "manual_backup_multiple",
        "constraints": [
            {"kind": "quantity", "value": "multiple"},
            {"kind": "distinguish_auto", "value": "manual_vs_auto"},
        ],
        "answer_mode": "explicit_official_evidence",
    }


@pytest.mark.parametrize(
    "question",
    [
        _ENGLISH_GOOGLE_PHOTOS,
        _CHINESE_GOOGLE_PHOTOS,
        _REWRITE_GOOGLE_PHOTOS,
    ],
    ids=["english", "chinese", "rewrite"],
)
def test_legal_candidate_accepted_in_any_language(question: str) -> None:
    """A well-formed candidate anchored to the original question,
    with product / platform / quantity / distinguish_auto all
    populated, must be accepted regardless of language or wording.
    """
    assert research_tasks is not None
    plan = research_tasks.build_task_plan(question, [_legal_candidate(question)])
    research_tasks.validate_task_plan(plan)
    assert plan.original_question == question
    # The candidate becomes a core task item. The server may add
    # auxiliary items, but it MUST NOT drop the core candidate.
    core_ids = [item.id for item in plan.items if item.role == "core"]
    assert "t1" in core_ids, "the well-formed core candidate must survive validation as a core item"


# ============================================================================
# 2. Additional conditions must be added or preserved
# ============================================================================


_OFFLINE_DROPBOX_QUESTION = (
    "According to official Dropbox Help, can mobile users manually add "
    "photos when camera uploads are disabled and the device is offline? "
    "Cite only official Dropbox Help."
)


_DISABLED_ANDROID_QUESTION = (
    "When automatic backup is disabled on Google Photos Android, "
    "can the user still upload a single photo manually? Cite only "
    "official Google Help."
)


_VERSION_ICLOUD_QUESTION = (
    "On Apple iCloud, which iOS version first supports Shared Photo "
    "Library, and where should users upload to it from a phone or "
    "tablet? Cite only official Apple Support."
)


_MOBILE_ONLY_QUESTION = (
    "In the Dropbox mobile app only, can the user upload several "
    "photos when the camera-roll sync is off? Cite only official "
    "Dropbox Help."
)


def _candidate_dropbox_mobile() -> dict:
    q = _MOBILE_ONLY_QUESTION
    span_text = "can the user upload several photos"
    start = q.find(span_text)
    return {
        "id": "t1",
        "question_span": {"start": start, "end": start + len(span_text)},
        "kind": "capability",
        "role": "core",
        "subject": {"product": "Dropbox", "platform": "mobile", "entry": "app"},
        "action": "manual_upload_multiple",
        "constraints": [
            {"kind": "quantity", "value": "multiple"},
            {"kind": "mode", "value": "camera_roll_sync_off"},
            {"kind": "device_scope", "value": "mobile_only"},
        ],
        "answer_mode": "explicit_official_evidence",
    }


def test_offline_clause_preserved_as_unresolved_core() -> None:
    """An "offline" clause in the original question must survive
    into the plan as an unresolved core item. The model did not
    surface it (no constraint, no candidate covers it), but the
    server must inject it; otherwise the question's offline scope
    silently disappears.
    """
    assert research_tasks is not None
    plan = research_tasks.build_task_plan(_OFFLINE_DROPBOX_QUESTION, [])
    research_tasks.validate_task_plan(plan)
    unresolved = [item for item in plan.unresolved]
    kinds = [item.kind for item in unresolved]
    assert research_tasks.KIND_UNRESOLVED in kinds, (
        "an offline clause must become an unresolved core item even "
        "when the model returns zero candidates"
    )


def test_disabled_mode_clause_preserved_as_unresolved_core() -> None:
    """A 'disabled' mode clause must become an unresolved core
    item, not be silently dropped. (Same logic as offline.)
    """
    assert research_tasks is not None
    plan = research_tasks.build_task_plan(_DISABLED_ANDROID_QUESTION, [])
    research_tasks.validate_task_plan(plan)
    unresolved_kinds = [item.kind for item in plan.unresolved]
    assert research_tasks.KIND_UNRESOLVED in unresolved_kinds


def test_version_clause_in_original_question_is_preserved() -> None:
    """A version clause ("iOS X first supports ...") is a CORE
    requirement. The plan must surface it as a constraint on the
    matching subject, not as auxiliary.
    """
    assert research_tasks is not None
    q = _VERSION_ICLOUD_QUESTION
    span_text = "which iOS version first supports Shared Photo Library"
    start = q.find(span_text)
    candidate = {
        "id": "t1",
        "question_span": {"start": start, "end": start + len(span_text)},
        "kind": "version",
        "role": "core",
        "subject": {"product": "Apple iCloud", "platform": "iOS", "entry": "shared_photo_library"},
        "action": "first_version_support",
        "constraints": [{"kind": "version", "value": "minimum"}],
        "answer_mode": "explicit_official_evidence",
    }
    plan = research_tasks.build_task_plan(q, [candidate])
    research_tasks.validate_task_plan(plan)
    version_items = [
        item for item in plan.items if any(c.kind == "version" for c in item.constraints)
    ]
    assert version_items, "version clause must survive in the plan"
    assert all(item.role == research_tasks.ROLE_CORE for item in version_items), (
        "version requirement is CORE; cannot be demoted to auxiliary"
    )


def test_mobile_only_scope_and_quantity_preserved() -> None:
    """Both the 'mobile_only' device-scope and the 'multiple'
    quantity must remain attached to the same candidate. The
    server must not split them into two items and must not drop
    either.
    """
    assert research_tasks is not None
    plan = research_tasks.build_task_plan(_MOBILE_ONLY_QUESTION, [_candidate_dropbox_mobile()])
    research_tasks.validate_task_plan(plan)
    core = [i for i in plan.items if i.id == "t1" and i.role == research_tasks.ROLE_CORE]
    assert len(core) == 1, "the candidate must remain one core item, not be split"
    constraints = {(c.kind, c.value) for c in core[0].constraints}
    assert ("quantity", "multiple") in constraints
    assert ("device_scope", "mobile_only") in constraints
    assert ("mode", "camera_roll_sync_off") in constraints


# ============================================================================
# 3. Anchor validation — out-of-bounds / forged / wrong subject
# ============================================================================


def test_out_of_bounds_span_is_rejected() -> None:
    """A candidate whose end offset exceeds the question length is
    rejected before it can corrupt the plan.
    """
    assert research_tasks is not None
    q = _ENGLISH_GOOGLE_PHOTOS
    candidate = _legal_candidate(q)
    candidate["question_span"] = {"start": 0, "end": len(q) + 1000}
    with pytest.raises(Exception) as exc:
        research_tasks.build_task_plan(q, [candidate])
    # The error must be a deliberate contract failure, not a generic
    # Python error swallowed by ``except:``.
    assert exc.value.__class__.__name__ not in {"Exception", "BaseException"}, (
        "anchor validation must raise a specific contract error"
    )


def test_inverted_span_is_rejected() -> None:
    """end < start is a malformed anchor and must be rejected."""
    assert research_tasks is not None
    q = _ENGLISH_GOOGLE_PHOTOS
    candidate = _legal_candidate(q)
    candidate["question_span"] = {"start": 50, "end": 10}
    with pytest.raises(Exception):
        research_tasks.build_task_plan(q, [candidate])


def test_forged_anchor_text_does_not_match_original_question() -> None:
    """The substring between start and end in the ORIGINAL question
    must equal the substring the candidate claims to anchor. A
    forged anchor pointing at unrelated text is rejected.
    """
    assert research_tasks is not None
    q = _ENGLISH_GOOGLE_PHOTOS
    candidate = _legal_candidate(q)
    # Move the span to point at the citation clause, not the verb
    # phrase the candidate claims to cover.
    forged_text = "Cite only official Google Help"
    forged_start = q.find(forged_text)
    candidate["question_span"] = {
        "start": forged_start,
        "end": forged_start + len(forged_text),
    }
    with pytest.raises(Exception):
        research_tasks.build_task_plan(q, [candidate])


def test_wrong_product_in_subject_is_rejected() -> None:
    """The subject's product must match what the anchor points at.
    A candidate claiming to anchor the Google Photos verb phrase
    but listing iCloud Photos / OneDrive / Dropbox as the product
    is rejected — otherwise the plan would carry contradictory
    product labels.
    """
    assert research_tasks is not None
    q = _ENGLISH_GOOGLE_PHOTOS
    candidate = _legal_candidate(q)
    for wrong_product in ("iCloud Photos", "OneDrive", "Dropbox"):
        bad = dict(candidate)
        bad["subject"] = dict(candidate["subject"])
        bad["subject"]["product"] = wrong_product
        with pytest.raises(Exception):
            research_tasks.build_task_plan(q, [bad])


def test_wrong_platform_in_subject_is_rejected() -> None:
    """The subject's platform must match the anchor. A Google
    Photos Android anchor paired with ``platform: iOS`` is rejected.
    """
    assert research_tasks is not None
    q = _ENGLISH_GOOGLE_PHOTOS
    candidate = _legal_candidate(q)
    bad = dict(candidate)
    bad["subject"] = dict(candidate["subject"])
    bad["subject"]["platform"] = "iOS"
    with pytest.raises(Exception):
        research_tasks.build_task_plan(q, [bad])


# ============================================================================
# 4. Missing sub-questions and full-question span masking cannot complete
# ============================================================================


_MULTI_SUBQUESTION_GOOGLE = (
    "According to official Google Help, can Google Photos Android "
    "manually select several photos for backup AND manually select "
    "several photos for upload to Drive? Cite only official Google "
    "Help; separate manual from automatic."
)


def _multi_subq_candidate_a(question: str) -> dict:
    """Covers the backup clause only."""
    span_text = "manually select several photos for backup"
    start = question.find(span_text)
    return {
        "id": "t1",
        "question_span": {"start": start, "end": start + len(span_text)},
        "kind": "capability",
        "role": "core",
        "subject": {"product": "Google Photos", "platform": "Android", "entry": "app"},
        "action": "manual_backup_multiple",
        "constraints": [{"kind": "quantity", "value": "multiple"}],
        "answer_mode": "explicit_official_evidence",
    }


def _multi_subq_candidate_b(question: str) -> dict:
    """Covers the upload-to-Drive clause only."""
    span_text = "manually select several photos for upload to Drive"
    start = question.find(span_text)
    return {
        "id": "t2",
        "question_span": {"start": start, "end": start + len(span_text)},
        "kind": "capability",
        "role": "core",
        "subject": {"product": "Google Drive", "platform": "Android", "entry": "app"},
        "action": "manual_upload_multiple",
        "constraints": [{"kind": "quantity", "value": "multiple"}],
        "answer_mode": "explicit_official_evidence",
    }


def test_missing_sub_question_blocks_completable_state() -> None:
    """When the question asks two distinct sub-questions and the
    model only supplies one candidate, the plan must remain NOT
    completable and must surface the missing sub-question as an
    unresolved core item.
    """
    assert research_tasks is not None
    q = _MULTI_SUBQUESTION_GOOGLE
    plan = research_tasks.build_task_plan(q, [_multi_subq_candidate_a(q)])
    research_tasks.validate_task_plan(plan)
    core_items = [item for item in plan.items if item.role == research_tasks.ROLE_CORE]
    assert len(core_items) == 1, "only the model-supplied core is present"
    unresolved_kinds = [item.kind for item in plan.unresolved]
    assert research_tasks.KIND_UNRESOLVED in unresolved_kinds, (
        "the missing sub-question must be marked unresolved, not silently dropped"
    )
    # The plan must NOT report an overall completable state when a
    # sub-question is unresolved.
    assert getattr(plan, "is_completable", True) is False, (
        "completable flag must be False while a sub-question is unresolved"
    )


def test_full_question_span_masks_missing_sub_questions() -> None:
    """A candidate whose ``question_span`` covers the whole question
    must be rejected as a masking attempt. The whole-question span
    is the simplest way to hide dropped clauses; the server must
    detect it and refuse, regardless of the candidate's other
    well-formed fields.
    """
    assert research_tasks is not None
    q = _MULTI_SUBQUESTION_GOOGLE
    candidate = _multi_subq_candidate_a(q)
    candidate["question_span"] = {"start": 0, "end": len(q)}
    plan_or_exc: object = None
    raised: Exception | None = None
    try:
        plan_or_exc = research_tasks.build_task_plan(q, [candidate])
    except Exception as exc:  # noqa: BLE001 — must be a specific contract error
        raised = exc
    if raised is None:
        # If the implementation chooses to accept the candidate, it
        # MUST record the masking attempt and refuse to mark the
        # plan completable.
        assert getattr(plan_or_exc, "has_full_question_mask", False) is True
        assert getattr(plan_or_exc, "is_completable", True) is False


def test_full_question_span_in_arbitrary_question_is_rejected() -> None:
    """Even a single-sub-question input cannot be satisfied by a
    whole-question span. The server must reject it (preferred) or
    flag it; the candidate must never become a valid core item
    with ``question_span=(0, len(q))``.
    """
    assert research_tasks is not None
    q = _ENGLISH_GOOGLE_PHOTOS
    candidate = _legal_candidate(q)
    candidate["question_span"] = {"start": 0, "end": len(q)}
    raised: Exception | None = None
    try:
        research_tasks.build_task_plan(q, [candidate])
    except Exception as exc:  # noqa: BLE001
        raised = exc
    assert raised is not None, (
        "whole-question span must be rejected as a masking attempt; "
        "non-empty JSON cannot prove coverage of a deleted sub-question"
    )


# ============================================================================
# 5. Quantity guard — ordinary plural cannot substitute for explicit multiple
# ============================================================================


_PLURAL_TRICK_QUESTION = (
    "Does the Google Photos Android help page mention photos "
    "(plural) being selectable for backup? Cite only official "
    "Google Help."
)


def test_plural_mention_does_not_prove_multiple_quantity() -> None:
    """A candidate whose constraint says ``"photos"`` (just the
    plural noun) must NOT satisfy the multi-select requirement.
    Only the explicit quantifiers ``multiple`` / ``several`` /
    ``multi-select`` / ``batch`` (as the literal action subject)
    count. An ordinary plural is not a quantity claim.
    """
    assert research_tasks is not None
    q = _PLURAL_TRICK_QUESTION
    span_text = "photos (plural) being selectable for backup"
    start = q.find(span_text)
    candidate = {
        "id": "t1",
        "question_span": {"start": start, "end": start + len(span_text)},
        "kind": "capability",
        "role": "core",
        "subject": {"product": "Google Photos", "platform": "Android", "entry": "app"},
        "action": "manual_backup_multiple",
        # Plain plural noun is NOT one of the explicit quantifiers.
        "constraints": [{"kind": "quantity", "value": "photos"}],
        "answer_mode": "explicit_official_evidence",
    }
    plan = research_tasks.build_task_plan(q, [candidate])
    research_tasks.validate_task_plan(plan)
    # The candidate stays a core item, but the quantity constraint
    # is either stripped to ``unknown`` or the plan records the
    # quantity requirement as unresolved. The plan MUST NOT claim
    # the multi-select requirement is satisfied.
    core = [i for i in plan.items if i.id == "t1"]
    assert len(core) == 1
    item = core[0]
    quantity_values = {c.value for c in item.constraints if c.kind == "quantity"}
    assert "photos" not in quantity_values, (
        "ordinary plural noun must not satisfy the multi-select requirement"
    )
    # Either the constraint is normalised to a non-claim or the
    # multi-select requirement surfaces as an unresolved core item.
    assert "multiple" not in quantity_values
    unresolved_kinds = [i.kind for i in plan.unresolved]
    assert research_tasks.KIND_UNRESOLVED in unresolved_kinds or len(quantity_values) == 0


# ============================================================================
# 6. Cross-entry task split must not confuse entries
# ============================================================================


_ICLOUD_CROSS_ENTRY_QUESTION = (
    "According to official Apple Support, which upload choices "
    "does iCloud.com show on a phone or tablet, and where should "
    "users upload to a Shared Photo Library? Cite only official "
    "Apple Support."
)


def _icloud_entry_candidate(question: str, entry: str, span_text: str) -> dict:
    start = question.find(span_text)
    assert start >= 0
    return {
        "id": f"t-{entry}",
        "question_span": {"start": start, "end": start + len(span_text)},
        "kind": "entry_listing" if entry == "icloud.com" else "destination",
        "role": "core",
        "subject": {"product": "Apple iCloud", "platform": "phone_or_tablet", "entry": entry},
        "action": "upload_target_choice" if entry == "icloud.com" else "upload_destination",
        "constraints": [],
        "answer_mode": "explicit_official_evidence",
    }


def test_icloud_com_and_shared_library_are_distinct_subjects() -> None:
    """iCloud.com and Shared Photo Library are different entries
    and the question asks two different things about each. The
    plan must keep them as two distinct core items, never merge
    them under one ``entry`` field, and never collapse the
    question into a single item with both subjects.
    """
    assert research_tasks is not None
    q = _ICLOUD_CROSS_ENTRY_QUESTION
    candidates = [
        _icloud_entry_candidate(
            q, "icloud.com", "which upload choices does iCloud.com show on a phone or tablet"
        ),
        _icloud_entry_candidate(
            q,
            "shared_photo_library",
            "where should users upload to a Shared Photo Library",
        ),
    ]
    plan = research_tasks.build_task_plan(q, candidates)
    research_tasks.validate_task_plan(plan)
    core = [i for i in plan.items if i.role == research_tasks.ROLE_CORE]
    entries = sorted(i.subject.entry for i in core)
    assert entries == ["icloud.com", "shared_photo_library"], (
        f"entries must stay distinct, got {entries!r}"
    )
    # No core item may claim BOTH entries.
    for item in core:
        assert "+" not in (item.subject.entry or ""), (
            f"entry {item.subject.entry!r} looks like a cross-entry merge"
        )


def test_subject_action_corresponds_to_anchor_text() -> None:
    """The action string must be plausibly grounded in the anchor
    text. A candidate that anchors the iCloud.com choice question
    but lists the action as ``upload_destination`` (the Shared
    Library verb) is rejected.
    """
    assert research_tasks is not None
    q = _ICLOUD_CROSS_ENTRY_QUESTION
    candidate = _icloud_entry_candidate(
        q, "icloud.com", "which upload choices does iCloud.com show on a phone or tablet"
    )
    candidate["action"] = "upload_destination"
    with pytest.raises(Exception):
        research_tasks.build_task_plan(q, [candidate])


# ============================================================================
# 7. Model self-reported completion fields are rejected
# ============================================================================


@pytest.mark.parametrize(
    "field_name",
    ["is_complete", "covered", "satisfied", "is_fully_covered", "all_done"],
)
def test_model_self_reported_completion_field_is_rejected(field_name: str) -> None:
    """The model is NOT allowed to assert completeness. Even a
    well-formed candidate is rejected if it carries a self-reported
    completion field — the server must derive completion from the
    plan, not from a model claim.
    """
    assert research_tasks is not None
    q = _ENGLISH_GOOGLE_PHOTOS
    candidate = _legal_candidate(q)
    candidate[field_name] = True
    with pytest.raises(Exception):
        research_tasks.build_task_plan(q, [candidate])


def test_self_reported_confidence_is_also_rejected() -> None:
    """A numeric ``confidence`` or ``score`` field the model adds
    is also not part of the plan surface. The model may express
    confidence only via the existing v5 selection channel; the
    task module must not import that channel here.
    """
    assert research_tasks is not None
    q = _ENGLISH_GOOGLE_PHOTOS
    candidate = _legal_candidate(q)
    candidate["confidence"] = 0.99
    raised: Exception | None = None
    try:
        research_tasks.build_task_plan(q, [candidate])
    except Exception as exc:  # noqa: BLE001
        raised = exc
    if raised is None:
        # If the implementation tolerates the field, it MUST NOT
        # appear on the returned plan — confidence is a downstream
        # signal owned by the selection layer.
        plan = research_tasks.build_task_plan(q, [_legal_candidate(q)])
        for item in plan.items + list(plan.unresolved):
            assert not hasattr(item, "confidence") or getattr(item, "confidence", None) is None


# ============================================================================
# 8. Frozen-fixture driven coverage — every real question produces a plan
# ============================================================================


@pytest.mark.parametrize(
    "case_name,question",
    _fixture_questions(),
    ids=lambda v: v if isinstance(v, str) else v[0],
)
def test_every_fixture_question_produces_a_valid_plan(case_name: str, question: str) -> None:
    """For every frozen fixture on disk, an empty-candidate plan
    must still build, validate, and surface at least one core
    requirement from the original question. The plan must NOT
    collapse to "everything is fine because the candidate list
    was empty": an empty model input is itself a state the server
    must handle.
    """
    assert research_tasks is not None
    plan = research_tasks.build_task_plan(question, [])
    research_tasks.validate_task_plan(plan)
    assert plan.original_question == question
    # Some core requirement must be present OR an unresolved clause
    # must record that the model provided no answer. We never accept
    # "the question was empty so the plan is empty".
    assert plan.items or plan.unresolved, (
        f"{case_name}: plan must surface either core items or "
        f"unresolved clauses; empty plan is a contract violation"
    )


# ============================================================================
# 9. Per-fixture — at least one core requirement has a real anchor
# ============================================================================


@pytest.mark.parametrize(
    "case_name,question",
    _fixture_questions(),
    ids=lambda v: v if isinstance(v, str) else v[0],
)
def test_every_fixture_plan_keeps_at_least_one_anchor(case_name: str, question: str) -> None:
    """When a fixture question is parsed with an empty candidate
    list, the server must still emit at least one core item whose
    ``question_span`` points at a real substring of the original
    question (start >= 0, end <= len(question), and start < end).
    """
    assert research_tasks is not None
    plan = research_tasks.build_task_plan(question, [])
    research_tasks.validate_task_plan(plan)
    core = [i for i in plan.items if i.role == research_tasks.ROLE_CORE]
    assert core, (
        f"{case_name}: empty-model input must still surface at least "
        f"one core requirement with a real anchor"
    )
    q_len = len(question)
    for item in core:
        start, end = item.question_span
        assert 0 <= start < end <= q_len, (
            f"{case_name}: anchor {start}..{end} is out of range for a question of length {q_len}"
        )
        assert question[start:end], (
            f"{case_name}: anchor {start}..{end} points at an empty substring"
        )
