"""Server-rendered sample facts and exact Raw-span proofs for new AI drafts."""

from copy import deepcopy

from app.agents.prompts import prd_generation_v3, requirement_generation_v1


class ArtifactSupportError(ValueError):
    pass


def index_evidence(evidence):
    records = {}
    for row in evidence:
        key, text = row.get("feedback_external_id"), row.get("feedback_content")
        uid = row.get("user_id")
        if not isinstance(key, str) or not key or not isinstance(text, str):
            raise ArtifactSupportError("artifact_source_invalid")
        identity = uid.strip() if isinstance(uid, str) else None
        identity = identity or None
        value = (text, identity)
        if key in records and records[key] != value:
            raise ArtifactSupportError("artifact_source_conflict")
        records[key] = value
    return records


def _ids(values, records):
    ids = list(dict.fromkeys(values))
    if not ids or any(i not in records for i in ids):
        raise ArtifactSupportError("artifact_source_missing")
    return ids


def _facts(ids, records):
    users = {records[i][1] for i in ids if records[i][1] is not None}
    unknown = sum(records[i][1] is None for i in ids)
    text = (
        f"本章本次引用样本：{len(ids)} 条反馈，{len(users)} 个用户 ID（非空去重），"
        f"{unknown} 条反馈缺少用户 ID（未知身份）。用户报告不证明系统原因、总体受影响人数或发生率。\n"
    )
    spans = []
    for key in ids:
        text += f"[{key}] 用户报告："
        start = len(text)
        raw = records[key][0]
        text += raw
        spans.append(dict(external_id=key, start=start, end=len(text), raw=raw))
        text += "\n"
    return text, dict(rendered=text, spans=spans)


def _budget(schema, value):
    if isinstance(value, str) and len(value) > schema.get("maxLength", 100000):
        raise ArtifactSupportError("artifact_render_budget_exhausted")
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", 100000):
            raise ArtifactSupportError("artifact_render_budget_exhausted")
        for item in value:
            _budget(schema.get("items", {}), item)
    if isinstance(value, dict):
        for key, item in value.items():
            _budget(schema.get("properties", {}).get(key, {}), item)


def _source(key):
    return f"{key}：本次引用的用户报告索引；仅支持原话，不证明原因、总体人数或方案成效。"


def _proposal(text):
    return "提案（待验证）：" + text


def materialize_with_proof(kind, payload, evidence):
    records = index_evidence(evidence)
    out = deepcopy(payload)
    proof = {}
    if kind == "requirement":
        chapter_ids = _ids([r["external_id"] for r in payload["evidence_references"]], records)
        out["problem"], proof["problem"] = _facts(chapter_ids, records)
        for key in ["target_user", "user_story", "solution_hypothesis"]:
            out[key] = _proposal(out[key])
        used = list(chapter_ids)
        for risk in out["risks"]:
            used.extend(risk["evidence_ids"])
        for metric in out["success_metrics"]:
            used.extend(i for i in metric["sources"] if i in records)
            metric["note"] = "指标定义或样本基线，尚未执行成效验收。" + metric.get("note", "")
        used = _ids(used, records)
        out["evidence_references"] = [dict(external_id=i, usage=_source(i)) for i in used]
        schema = requirement_generation_v1.TOOL_SCHEMA
    elif kind == "prd":
        for key in ["background", "problem"]:
            ids = _ids(payload[key]["evidence_ids"], records)
            text, proof[key] = _facts(ids, records)
            out[key] = dict(text=text, evidence_ids=ids)
        for key in ["target_user", "user_story", "product_goal", "proposed_solution", "user_flow"]:
            out[key]["text"] = _proposal(out[key]["text"])
        out["proposed_solution"]["is_proposed"] = True
        for fr in out["functional_requirements"]:
            fr["text"] = _proposal(fr["text"])
        for ac in out["acceptance_criteria"]:
            ac["note"] = "尚未执行；verifiable 仅表示标准可验证。" + ac.get("note", "")
        for metric in out["success_metrics"]:
            metric["note"] = "指标定义或样本基线，尚未执行成效验收。" + metric.get("note", "")
        used = []

        def collect(node):
            if isinstance(node, dict):
                used.extend(node.get("evidence_ids", []))
                for key, value in node.items():
                    if key not in {"evidence_ids", "evidence_sources"}:
                        collect(value)
            elif isinstance(node, list):
                for child in node:
                    collect(child)

        for key, value in out.items():
            if key not in {"evidence_ids", "evidence_sources"}:
                collect(value)
        for metric in out["success_metrics"]:
            used.extend(i for i in metric["sources"] if i in records)
        used = _ids(used, records)
        out["evidence_ids"] = used
        out["evidence_sources"] = [_source(i) for i in used]
        schema = prd_generation_v3.TOOL_SCHEMA
    else:
        raise ArtifactSupportError("artifact_kind_invalid")
    for risk in out["risks"]:
        risk["mitigation"] = "建议（待验证）：" + risk.get("mitigation", "")
    _budget(schema, out)
    return out, proof


def mask_verified_raw(payload, proof):
    """Mask only renderer-proved Raw spans in a private scan copy."""
    scan = deepcopy(payload)
    for field, chapter in proof.items():
        value = payload[field]
        text = value["text"] if isinstance(value, dict) else value
        if text != chapter["rendered"]:
            raise ArtifactSupportError("artifact_proof_field_mismatch")
        previous = 0
        chars = list(text)
        for span in chapter["spans"]:
            a, b = span["start"], span["end"]
            if a < previous or not 0 <= a <= b <= len(text) or text[a:b] != span["raw"]:
                raise ArtifactSupportError("artifact_proof_span_mismatch")
            chars[a:b] = ["□"] * (b - a)
            previous = b
        masked = "".join(chars)
        if isinstance(scan[field], dict):
            scan[field]["text"] = masked
        else:
            scan[field] = masked
    return scan


def materialize_requirement(payload, evidence):
    return materialize_with_proof("requirement", payload, evidence)[0]


def materialize_prd(payload, evidence):
    return materialize_with_proof("prd", payload, evidence)[0]
