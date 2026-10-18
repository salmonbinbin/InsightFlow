"""Exact renderer proof masks only Raw spans, never neighboring proposal claims."""

from copy import deepcopy

import pytest
from test_phase5 import _make_valid_requirement_payload

from app.services.artifact_grounding_v2 import (
    ArtifactSupportError,
    mask_verified_raw,
    materialize_with_proof,
)


def render():
    payload = _make_valid_requirement_payload(("F001",))
    payload["risks"] = []
    return materialize_with_proof(
        "requirement",
        payload,
        [dict(feedback_external_id="F001", feedback_content="希望留存提高30%。", user_id="U1")],
    )


def test_proof_rejects_changed_factual_chapter():
    payload, proof = render()
    payload["problem"] += "收入已经提高30%。"
    with pytest.raises(ArtifactSupportError, match="field_mismatch"):
        mask_verified_raw(payload, proof)


def test_proof_rejects_shifted_raw_span():
    payload, proof = render()
    proof["problem"]["spans"][0]["start"] += 1
    with pytest.raises(ArtifactSupportError, match="span_mismatch"):
        mask_verified_raw(payload, proof)


def test_raw_wish_cannot_exempt_same_promise_in_other_field():
    payload, proof = render()
    payload["solution_hypothesis"] = "用户报告：上线后留存提高30%。"
    before = deepcopy(payload)
    scan = mask_verified_raw(payload, proof)
    assert "希望留存提高30%" not in scan["problem"]
    assert scan["solution_hypothesis"] == payload["solution_hypothesis"]
    assert payload == before
