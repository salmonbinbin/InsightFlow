"""Strict, extractive known-risk support for explicitly versioned AI outputs.

PM edits and historical artifacts retain their original content. New known
risk text must be a complete cited Raw record, never a model-generated causal
explanation decorated with otherwise legitimate IDs. Mitigations are proposals.
"""


class RiskSupportError(ValueError):
    pass


def validate_known_risks(risks, evidence):
    raw = {e["feedback_external_id"]: e["feedback_content"] for e in evidence}
    for risk in risks:
        if risk["category"] != "known":
            continue
        ids = risk["evidence_ids"]
        if not ids or any(i not in raw for i in ids):
            raise RiskSupportError("known_risk_source_missing")
        if any(risk["description"] != raw[i] for i in ids):
            raise RiskSupportError("known_risk_text_unsupported")
