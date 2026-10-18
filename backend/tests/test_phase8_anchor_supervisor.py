"""Server-owned question anchors isolate question text from context/source text."""

import pytest
from test_phase8_v6_supervisor import task

from app.services.research_coverage import prepare_plan, question_anchors
from app.services.research_tasks import TaskContractError


def test_chinese_question_anchors_are_original_offsets_and_preserve_condition():
    q = "仅引用 Atlas 官方帮助：关闭自动上传后，手机用户如何手动添加图片？请保留离线条件。"
    anchors = question_anchors(q)
    assert anchors and all(q[a["start"] : a["end"]] == a["text"] for a in anchors)
    assert any("关闭自动上传" in a["text"] for a in anchors)
    assert any("离线" in a["text"] for a in anchors)
    assert all("仅引用" not in a["text"] for a in anchors)


def test_nested_subquestions_have_separate_exact_anchors():
    q = "According to official Atlas Help, which upload options appear on a phone, and where should shared items go? Cite only official Atlas Help."
    anchors = question_anchors(q)
    assert len(anchors) == 2
    assert "which upload options" in anchors[0]["text"]
    assert anchors[1]["text"].startswith("where")


def test_id_based_task_cannot_quote_an_internal_suggestion_or_skip_a_subquestion():
    q = "In Atlas mobile, what are the steps to upload files? Compare manual and automatic upload."
    anchors = question_anchors(q)
    t = task(q, anchors[0]["text"], "procedure", "Atlas", "mobile", "app", "upload")
    t["question_anchor_id"] = anchors[0]["id"]
    t.pop("question_quote")
    plan = prepare_plan(q, [t])
    assert plan.unresolved and not plan.is_completable
    t["question_anchor_id"] = "internal-suggestion"
    with pytest.raises(TaskContractError):
        prepare_plan(q, [t])


def test_citation_instruction_does_not_swallow_a_limit_subquestion():
    q = "Can Atlas upload photos? Cite only official Atlas Help and state any documented limits."
    anchors = question_anchors(q)
    assert len(anchors) == 2
    assert "state any documented limits" in anchors[1]["text"]


def test_capability_anchor_carries_eligible_kind_and_precise_repair_pointer():
    q = "In Atlas mobile, can users upload photos?"
    anchor = question_anchors(q)[0]
    assert "capability" in anchor["allowed_kinds"]
    assert "procedure" not in anchor["allowed_kinds"]
    t = task(q, anchor["text"], "procedure", "Atlas", "mobile", "app", "upload")
    with pytest.raises(TaskContractError) as exc:
        prepare_plan(q, [t])
    assert "tasks[0]" in exc.value.detail
    assert "procedure" in exc.value.detail
