"""New-version factual chapters derive from Raw; proposals stay proposals."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_phase5 import _make_valid_prd_payload, _make_valid_requirement_payload

from app.services import requirement_prd as service

EVIDENCE = [
    dict(
        feedback_external_id="F001", feedback_content="为什么每次只能上传一张图片？", user_id="U1"
    ),
    dict(
        feedback_external_id="F002",
        feedback_content="我传旅游照片要选几十次，能不能支持多选？",
        user_id="U1",
    ),
    dict(
        feedback_external_id="F003",
        feedback_content="上传图片其实挺快的，就是每次只能选一张。",
        user_id="U2",
    ),
]


@pytest.mark.parametrize("kind", ["requirement", "prd"])
def test_new_ai_factual_chapters_cannot_invent_ui_or_performance_causes(monkeypatch, kind):
    for k, v in [
        ("llm_api_key", "test"),
        ("llm_base_url", "https://example.invalid"),
        ("llm_model", "test"),
    ]:
        monkeypatch.setattr(service.settings, k, v)
    payload = (
        _make_valid_requirement_payload if kind == "requirement" else _make_valid_prd_payload
    )(("F001", "F002", "F003"))
    fabricated = "当前用户必须打开选择器、选图、确认、添加；上传性能不是瓶颈，原因在选择器。"
    if kind == "requirement":
        payload["problem"] = fabricated
        payload["evidence_references"] = [
            dict(external_id=e["feedback_external_id"], usage="选择器原因已证实") for e in EVIDENCE
        ]
    else:
        for key in ["background", "problem"]:
            payload[key] = dict(text=fabricated, evidence_ids=["F001", "F002", "F003"])
        payload["evidence_ids"] = ["F001", "F002", "F003"]
        payload["evidence_sources"] = ["选择器原因已证实"]
    payload["risks"] = [
        dict(
            category="known",
            description=EVIDENCE[0]["feedback_content"],
            evidence_ids=["F001"],
            mitigation="建议核对",
        )
    ]
    original = deepcopy(payload)

    class Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def generate_requirement(self, *a, **kw):
            return SimpleNamespace(
                payload=payload, usage=SimpleNamespace(input_tokens=2, output_tokens=3)
            )

        generate_prd = generate_requirement

    monkeypatch.setattr(
        service, "RequirementClient" if kind == "requirement" else "PRDClient", Client
    )
    result = (service._call_requirement if kind == "requirement" else service._call_prd)(
        payload={"evidence": EVIDENCE},
        allowed_evidence_ids={"F001", "F002", "F003"},
        prompt_version="requirement-generation-v2"
        if kind == "requirement"
        else "prd-generation-v4",
    )
    if kind == "requirement":
        facts = result[1]
        usages = [x.usage for x in result[7]]
    else:
        facts = result[0]["background"]["text"] + "\n" + result[0]["problem"]["text"]
        usages = result[0]["evidence_sources"]
        assert set(result[0]["background"]["evidence_ids"]) == {"F001", "F002", "F003"}
        assert "提案" in result[0]["functional_requirements"][0]["text"]
        assert "尚未执行" in result[0]["acceptance_criteria"][0]["note"]
    assert fabricated not in facts
    assert "3 条反馈" in facts and "2 个用户 ID" in facts
    assert all(e["feedback_content"] in facts for e in EVIDENCE)
    assert all("原因已证实" not in s for s in usages)
    assert payload == original


@pytest.mark.parametrize("kind", ["requirement", "prd"])
def test_fact_materialization_rejects_over_budget_instead_of_truncating_raw(monkeypatch, kind):
    from app.services.artifact_grounding_v2 import (
        ArtifactSupportError,
        materialize_prd,
        materialize_requirement,
    )

    record = dict(
        feedback_external_id="F001", feedback_content="原始报告" + ("字" * 4000), user_id="U1"
    )
    p = (_make_valid_requirement_payload if kind == "requirement" else _make_valid_prd_payload)(
        ("F001",)
    )
    with pytest.raises(ArtifactSupportError, match="budget"):
        (materialize_requirement if kind == "requirement" else materialize_prd)(p, [record])


@pytest.mark.parametrize("kind", ["requirement", "prd"])
def test_actual_cited_subset_is_materialized_without_bundle_leak(kind):
    from app.services.artifact_grounding_v2 import materialize_prd, materialize_requirement

    p = (_make_valid_requirement_payload if kind == "requirement" else _make_valid_prd_payload)(
        ("F001",)
    )
    p["risks"] = []
    result = (materialize_requirement if kind == "requirement" else materialize_prd)(p, EVIDENCE)
    text = result["problem"] if kind == "requirement" else result["problem"]["text"]
    assert EVIDENCE[0]["feedback_content"] in text
    assert all(e["feedback_content"] not in text for e in EVIDENCE[1:])
    assert "1 条反馈" in text and "1 个用户 ID" in text


def test_prd_each_factual_chapter_keeps_its_own_citation_set():
    from app.services.artifact_grounding_v2 import materialize_prd

    p = _make_valid_prd_payload(("F001", "F002"))
    p["risks"] = []
    p["background"]["evidence_ids"] = ["F001"]
    p["problem"]["evidence_ids"] = ["F002"]
    result = materialize_prd(p, EVIDENCE)
    for field, index in [("background", 0), ("problem", 1)]:
        assert EVIDENCE[index]["feedback_content"] in result[field]["text"]
        assert EVIDENCE[1 - index]["feedback_content"] not in result[field]["text"]
        assert result[field]["evidence_ids"] == [EVIDENCE[index]["feedback_external_id"]]


def test_feedback_and_user_counts_are_distinct_and_conflicts_rejected():
    from app.services.artifact_grounding_v2 import ArtifactSupportError, materialize_requirement

    p = _make_valid_requirement_payload(("F001", "F002", "F004"))
    p["risks"] = []
    p["evidence_references"] = [
        dict(external_id=i, usage="index") for i in ["F001", "F002", "F004"]
    ]
    records = [
        EVIDENCE[0],
        EVIDENCE[0],
        EVIDENCE[1],
        dict(
            feedback_external_id="F004",
            feedback_content=EVIDENCE[0]["feedback_content"],
            user_id="",
        ),
    ]
    result = materialize_requirement(p, records)
    assert "3 条反馈" in result["problem"] and "1 个用户 ID" in result["problem"]
    assert "1" in result["problem"] and "未知" in result["problem"]
    with pytest.raises(ArtifactSupportError):
        materialize_requirement(p, records + [EVIDENCE[0] | dict(feedback_content="冲突原文")])


def call_output(monkeypatch, kind, output, evidence, version=None):
    for k, v in [
        ("llm_api_key", "test"),
        ("llm_base_url", "https://example.invalid"),
        ("llm_model", "test"),
    ]:
        monkeypatch.setattr(service.settings, k, v)

    class Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def generate_requirement(self, *a, **kw):
            return SimpleNamespace(
                payload=output, usage=SimpleNamespace(input_tokens=2, output_tokens=3)
            )

        generate_prd = generate_requirement

    monkeypatch.setattr(
        service, "RequirementClient" if kind == "requirement" else "PRDClient", Client
    )
    return (service._call_requirement if kind == "requirement" else service._call_prd)(
        payload={"evidence": evidence},
        allowed_evidence_ids={e["feedback_external_id"] for e in evidence},
        prompt_version=version
        or ("requirement-generation-v2" if kind == "requirement" else "prd-generation-v4"),
    )


@pytest.mark.parametrize("field", ["problem", "solution_hypothesis"])
def test_new_requirement_rejects_model_business_commitment_before_materialization(
    monkeypatch, field
):
    p = _make_valid_requirement_payload(("F001",))
    p["risks"] = []
    p[field] = "本方案必将提升收入。"
    with pytest.raises(service.RequirementLLMProtocolError):
        call_output(monkeypatch, "requirement", p, EVIDENCE)


@pytest.mark.parametrize("kind", ["requirement", "prd"])
def test_true_user_wish_is_preserved_as_raw_report_without_becoming_product_promise(
    monkeypatch, kind
):
    raw = dict(
        feedback_external_id="F001", feedback_content="希望这个功能能提升收入。", user_id="U1"
    )
    p = (_make_valid_requirement_payload if kind == "requirement" else _make_valid_prd_payload)(
        ("F001",)
    )
    p["risks"] = []
    result = call_output(monkeypatch, kind, p, [raw])
    text = result[1] if kind == "requirement" else result[0]["problem"]["text"]
    assert raw["feedback_content"] in text and "用户报告" in text


@pytest.mark.parametrize("kind", ["requirement", "prd"])
def test_call_boundary_maps_materialization_budget_without_exposing_raw(monkeypatch, kind):
    raw = dict(
        feedback_external_id="F001", feedback_content="隐私标识" + ("字" * 4000), user_id="U1"
    )
    p = (_make_valid_requirement_payload if kind == "requirement" else _make_valid_prd_payload)(
        ("F001",)
    )
    p["risks"] = []
    error = (
        service.RequirementLLMProtocolError
        if kind == "requirement"
        else service.PRDLLMProtocolError
    )
    with pytest.raises(error) as caught:
        call_output(monkeypatch, kind, p, [raw])
    assert "隐私标识" not in str(caught.value)
    assert len(str(caught.value)) < 500


@pytest.mark.parametrize("kind", ["requirement", "prd"])
def test_legacy_ai_output_is_not_materialized(monkeypatch, kind):
    p = (_make_valid_requirement_payload if kind == "requirement" else _make_valid_prd_payload)(
        ("F001",)
    )
    p["risks"] = []
    original = deepcopy(p)
    result = call_output(
        monkeypatch,
        kind,
        p,
        EVIDENCE,
        version="requirement-generation-v1" if kind == "requirement" else "prd-generation-v3",
    )
    assert (result[1] if kind == "requirement" else result[0]["problem"]["text"]) == (
        original["problem"] if kind == "requirement" else original["problem"]["text"]
    )
    assert p == original


def test_prd_global_source_index_matches_actual_chapter_and_risk_references():
    from app.services.artifact_grounding_v2 import materialize_prd

    p = _make_valid_prd_payload(("F001",))
    p["background"]["evidence_ids"] = ["F001"]
    p["problem"]["evidence_ids"] = ["F002"]
    p["risks"] = [
        dict(
            category="known",
            description=EVIDENCE[2]["feedback_content"],
            evidence_ids=["F003"],
            mitigation="建议核对",
        )
    ]
    out = materialize_prd(
        p, EVIDENCE + [dict(feedback_external_id="F004", feedback_content="不得偷带", user_id="U9")]
    )
    assert set(out["evidence_ids"]) == {"F001", "F002", "F003"}
    assert all(any(i in text for text in out["evidence_sources"]) for i in ["F001", "F002", "F003"])
    assert "F004" not in str(out)


def test_same_feedback_with_conflicting_user_identity_is_rejected():
    from app.services.artifact_grounding_v2 import ArtifactSupportError, materialize_requirement

    p = _make_valid_requirement_payload(("F001",))
    p["risks"] = []
    with pytest.raises(ArtifactSupportError):
        materialize_requirement(p, [EVIDENCE[0], EVIDENCE[0] | dict(user_id="different")])


@pytest.mark.parametrize(
    "kind,field",
    [
        ("requirement", "solution_hypothesis"),
        ("requirement", "target_user"),
        ("prd", "proposed_solution"),
        ("prd", "functional_requirements"),
        ("prd", "acceptance_criteria"),
    ],
)
def test_raw_revenue_wish_does_not_exempt_model_promises_in_other_fields(monkeypatch, kind, field):
    raw = dict(
        feedback_external_id="F001", feedback_content="希望这个功能能提升收入。", user_id="U1"
    )
    p = (_make_valid_requirement_payload if kind == "requirement" else _make_valid_prd_payload)(
        ("F001",)
    )
    p["risks"] = []
    text = "用户报告：希望这个功能能提升收入。本方案必将提升收入。"
    if kind == "requirement":
        p[field] = text
    elif isinstance(p[field], list):
        p[field][0]["text"] = text
    else:
        p[field]["text"] = text
    error = (
        service.RequirementLLMProtocolError
        if kind == "requirement"
        else service.PRDLLMProtocolError
    )
    with pytest.raises(error):
        call_output(monkeypatch, kind, p, [raw])


def test_unknown_user_id_is_counted_per_feedback_and_never_as_a_distinct_user():
    from app.services.artifact_grounding_v2 import materialize_requirement

    rows = [
        dict(feedback_external_id=f"F{i:03}", feedback_content="同一段用户报告", user_id=uid)
        for i, uid in enumerate(["U1", None, "", "  "], 1)
    ]
    p = _make_valid_requirement_payload(tuple(r["feedback_external_id"] for r in rows))
    p["risks"] = []
    p["evidence_references"] = [
        dict(external_id=r["feedback_external_id"], usage="sample") for r in rows
    ]
    out = materialize_requirement(p, rows)
    assert "4 条反馈" in out["problem"] and "1 个用户 ID" in out["problem"]
    assert "3" in out["problem"] and ("缺少用户 ID" in out["problem"] or "未知" in out["problem"])
    assert out["problem"].count("同一段用户报告") == 4


@pytest.mark.parametrize(
    "field", ["functional_requirements", "edge_cases", "acceptance_criteria", "success_metrics"]
)
def test_prd_nonfactual_only_reference_is_indexed_without_leaking_into_factual_chapters(field):
    from app.services.artifact_grounding_v2 import materialize_prd

    p = _make_valid_prd_payload(("F001",))
    p["risks"] = []
    p[field][0]["sources" if field == "success_metrics" else "evidence_ids"] = ["F002"]
    out = materialize_prd(p, EVIDENCE)
    assert set(out["evidence_ids"]) == {"F001", "F002"}
    assert any("F002" in x for x in out["evidence_sources"])
    assert EVIDENCE[1]["feedback_content"] not in out["background"]["text"]
    assert EVIDENCE[1]["feedback_content"] not in out["problem"]["text"]


def test_actual_global_citation_union_over_schema_budget_is_rejected():
    from app.services.artifact_grounding_v2 import ArtifactSupportError, materialize_prd

    rows = [
        dict(feedback_external_id=f"F{i:03}", feedback_content="用户报告", user_id="U1")
        for i in range(1, 34)
    ]
    p = _make_valid_prd_payload(("F001",))
    p["risks"] = []
    p["functional_requirements"] = [
        dict(text="功能提案", evidence_ids=[r["feedback_external_id"]]) for r in rows
    ]
    with pytest.raises(ArtifactSupportError, match="budget"):
        materialize_prd(p, rows)
