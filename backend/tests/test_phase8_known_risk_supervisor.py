"""New AI contract requires original Raw text, rather than merely legal IDs."""

import importlib

import pytest


def validate(risks, evidence):
    try:
        m = importlib.import_module("app.services.risk_support_v2")
    except ModuleNotFoundError:
        pytest.fail("exact Raw risk support validator missing")
    return m.validate_known_risks(risks, evidence)


def test_legal_single_select_feedback_ids_cannot_prove_backend_or_review_cause():
    evidence = [
        dict(feedback_external_id="F001", feedback_content="目前一次只能选择一张照片上传。")
    ]
    risk = dict(
        category="known",
        description="后端审核策略限制了批量上传。",
        evidence_ids=["F001"],
        mitigation="建议测试",
    )
    with pytest.raises(ValueError, match="known_risk_text_unsupported"):
        validate([risk], evidence)


def test_real_documented_limit_remains_known_without_relabelling_everything():
    text = "上传失败，提示文件已超过20MB大小限制。"
    risk = dict(
        category="known", description=text, evidence_ids=["F002"], mitigation="建议展示限制"
    )
    validate([risk], [dict(feedback_external_id="F002", feedback_content=text)])
    assert risk["category"] == "known"


def test_partial_negated_quote_and_an_extra_unrelated_id_are_rejected():
    text = "不是审核问题，是界面一次只能选择一张。"
    with pytest.raises(ValueError):
        validate(
            [dict(category="known", description="审核问题", evidence_ids=["F1"])],
            [dict(feedback_external_id="F1", feedback_content=text)],
        )
    evidence = [
        dict(feedback_external_id="F1", feedback_content=text),
        dict(feedback_external_id="F2", feedback_content="别的功能"),
    ]
    with pytest.raises(ValueError):
        validate([dict(category="known", description=text, evidence_ids=["F1", "F2"])], evidence)


def test_speculative_proposal_stays_explicitly_unproven():
    risk = dict(
        category="speculative",
        description="假设网络可能影响上传，待测试。",
        evidence_ids=[],
        mitigation="测试",
    )
    validate([risk], [])
    assert risk["category"] == "speculative"


def test_new_client_prompt_binding_and_version_registry_are_explicit():
    from app.agents.prompts import prd_generation_v4, requirement_generation_v2
    from app.services.requirement_prd_client import PRDClient, RequirementClient

    opts = dict(
        api_key="not-real", base_url="https://example.invalid", model="test", timeout_seconds=1
    )
    with RequirementClient(**opts, prompt_module=requirement_generation_v2) as c:
        assert c.prompt_version == "requirement-generation-v2"
    with PRDClient(**opts, prompt_module=prd_generation_v4) as c:
        assert c.prompt_version == "prd-generation-v4"


@pytest.mark.parametrize("kind", ["requirement", "prd"])
def test_new_generation_path_enforces_support_and_pins_actual_prompt(monkeypatch, kind):
    from types import SimpleNamespace

    from test_phase5 import _make_valid_prd_payload, _make_valid_requirement_payload

    from app.services import requirement_prd as service

    for key, value in [
        ("llm_api_key", "test"),
        ("llm_base_url", "https://example.invalid"),
        ("llm_model", "test"),
    ]:
        monkeypatch.setattr(service.settings, key, value)
    make = _make_valid_requirement_payload if kind == "requirement" else _make_valid_prd_payload
    payload = make(("F001",))
    payload["risks"] = [
        dict(
            category="known",
            description="审核造成上传限制",
            evidence_ids=["F001"],
            mitigation="建议调查",
        )
    ]
    observed = []

    class Client:
        def __init__(self, **kw):
            observed.append(kw["prompt_module"].PROMPT_VERSION)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def generate_requirement(self, p):
            return SimpleNamespace(
                payload=payload, usage=SimpleNamespace(input_tokens=1, output_tokens=1)
            )

        generate_prd = generate_requirement

    monkeypatch.setattr(
        service, "RequirementClient" if kind == "requirement" else "PRDClient", Client
    )
    call = service._call_requirement if kind == "requirement" else service._call_prd
    version = "requirement-generation-v2" if kind == "requirement" else "prd-generation-v4"
    error = (
        service.RequirementLLMProtocolError
        if kind == "requirement"
        else service.PRDLLMProtocolError
    )
    with pytest.raises(error, match="known_risk_text_unsupported"):
        call(
            payload={
                "evidence": [
                    dict(feedback_external_id="F001", feedback_content="目前只支持单张选择。")
                ]
            },
            allowed_evidence_ids={"F001"},
            prompt_version=version,
        )
    assert observed == [version]


def test_unknown_generation_versions_are_rejected():
    from pydantic import ValidationError

    from app.schemas.requirement_prd import PRDCreateRequest, RequirementCreateRequest

    for cls in [RequirementCreateRequest, PRDCreateRequest]:
        with pytest.raises(ValidationError):
            cls(prompt_version="invented")
