"""Independent task preservation checks, with no product answer templates."""

from copy import deepcopy

import pytest

from app.services.research_tasks import TaskContractError, build_task_plan


def candidate(question, anchor, *, role="core", constraints=None):
    start = question.index(anchor)
    return {
        "id": "t1",
        "kind": "procedure",
        "role": role,
        "question_span": {"start": start, "end": start + len(anchor)},
        "subject": {"product": "Acme Vault", "platform": "Android", "entry": "app"},
        "action": "manual_upload",
        "constraints": constraints or [],
        "answer_mode": "explicit_official_evidence",
    }


def test_arbitrary_product_uses_anchored_candidates():
    question = "In Acme Vault Android app, how to manually upload a file?"
    plan = build_task_plan(question, [candidate(question, "how to manually upload a file")])
    assert plan.items[0].subject.product == "Acme Vault"
    assert plan.original_question == question


def test_unknown_condition_inside_model_span_is_not_hidden():
    question = "In Acme Vault Android app, how to manually upload a file without a subscription?"
    plan = build_task_plan(
        question, [candidate(question, "how to manually upload a file without a subscription")]
    )
    assert not plan.is_completable
    assert any(
        "without a subscription" in question[slice(*i.question_span)] for i in plan.unresolved
    )


def test_auxiliary_only_model_plan_never_gets_completion_eligibility():
    question = "In Acme Vault Android app, upload"
    plan = build_task_plan(question, [candidate(question, "upload", role="auxiliary")])
    assert not plan.is_completable


def test_unanchored_quantity_constraint_is_rejected():
    question = "In Acme Vault Android app, how to manually upload a file?"
    item = candidate(
        question,
        "how to manually upload a file",
        constraints=[{"kind": "quantity", "value": "unlimited"}],
    )
    with pytest.raises(TaskContractError):
        build_task_plan(question, [item])


def test_boolean_offsets_are_rejected():
    question = "In Acme Vault Android app, how to manually upload a file?"
    item = deepcopy(candidate(question, "how to manually upload a file"))
    item["question_span"]["start"] = True
    with pytest.raises(TaskContractError):
        build_task_plan(question, [item])


def test_chinese_subject_syntax_does_not_destroy_legal_plan():
    from test_phase8_research_tasks import _CHINESE_GOOGLE_PHOTOS, _legal_candidate

    question = _CHINESE_GOOGLE_PHOTOS
    plan = build_task_plan(question, [_legal_candidate(question)])
    assert plan.is_completable, [question[slice(*i.question_span)] for i in plan.unresolved]
