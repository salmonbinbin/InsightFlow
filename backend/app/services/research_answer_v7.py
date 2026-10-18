"""Extractive answers and finite derivations over complete, verified premises.

The model supplies no factual titles, causal explanations or completeness flags.
Proposals are user-visible hypotheses; neither their quality nor general semantic
entailment is represented as mechanically verified.
"""

from __future__ import annotations

import copy
from uuid import uuid4

from app.services.research_claims import Claim
from app.services.research_coverage import normalize
from app.services.research_spec_v7 import (
    ANALYSIS_FACETS,
    MAX_SCENARIO,
    SpecError,
    array,
    closed,
    string,
    validate,
)

# X-answer fix 1: empty evidence_ids is now legal. The server expands
# it to the requirement's full covered support group. Non-empty lists
# must STILL exactly equal the support group — no partial subsets —
# and the post-expansion union is capped at 40. confirmed / same Run /
# same project invariants live in the per-ID loop below.
PREMISE_SCHEMA = closed(dict(requirement_id=string(80), evidence_ids=array(string(200), 40, 0)))
ANALYSIS_SCHEMA = closed(
    dict(
        requirement_id=string(80),
        facet=dict(type="string", enum=list(ANALYSIS_FACETS)),
        operator=dict(
            type="string", enum=["contrast", "shared_pattern", "scenario_tradeoff", "proposed_test"]
        ),
        premises=array(PREMISE_SCHEMA, 8, 1),
        scenario=string(MAX_SCENARIO, True),
        proposal=string(200, True),
    )
)
SELECTION_SCHEMA = closed(
    dict(
        evidence_ids=array(string(200), 40),
        bindings=array(
            closed(dict(requirement_id=string(80), evidence_ids=array(string(200), 40))), 16
        ),
        analyses=array(ANALYSIS_SCHEMA, 8),
    )
)
_CATEGORY = {"strength": "competitor", "weakness": "competitor", "provenance": "competitor"}
_LABEL = {
    "competitor": "样本差异",
    "market_pattern": "本次样本模式",
    "strength": "场景优势假设",
    "weakness": "场景劣势假设",
    "opportunity": "待验证机会",
    "risk": "待验证风险",
    "provenance": "来源边界",
}


def _product_canonicals(r):
    """Canonical product set for a requirement (mirror of spec_v7)."""
    from app.services import research_input_v7 as input_v7

    return {input_v7.canonical_product(p) or p.casefold() for p in r.get("products", []) or []}


def _one(spec, c, report, facts, entries):
    # X2-answer fix 1: deep-copy the candidate so any in-place
    # expansion of ``evidence_ids`` never pollutes the original model
    # payload / audit trail. The structured ``evidence_location`` is
    # built from the COPY (resolved support groups) below; the
    # caller's ``c`` is left untouched.
    c_local = copy.deepcopy(c)
    validate(ANALYSIS_SCHEMA, c_local, "analysis")
    from app.services.research_support_v7 import operation_family

    requirements = {r["id"]: r for r in spec["requirements"]}
    request = requirements.get(c_local["requirement_id"])
    if (
        not request
        or request["kind"] != "analysis"
        or c_local["facet"] not in request["analysis_facets"]
    ):
        raise SpecError("analysis_requirement_invalid")
    scenario = c_local["scenario"]
    if (
        scenario is not None
        and scenario not in spec["original_question"]
        and scenario not in (spec["original_scope"] or "")
    ):
        raise SpecError("scenario_not_in_input")
    if c_local["facet"] in {"strength", "weakness", "opportunity", "risk"} and not scenario:
        raise SpecError("scenario_missing")
    covered = {i["requirement_id"]: i for i in report["items"]}
    fact_map = {f.evidence_id: f for f in facts}
    supports = {}
    premises = []
    if len({p["requirement_id"] for p in c_local["premises"]}) != len(c_local["premises"]):
        raise SpecError("premise_duplicate")
    request_product_canon = _product_canonicals(request)
    # X2-answer fix 2: explicit action / object / mode scope from the
    # analysis request. ``unspecified`` / ``mixed`` / empty values are
    # non-restrictive — the matching is only enforced when the request
    # has a binding value on that dimension. canonical (action via
    # ``operation_family``) and ``normalize`` (object) keep the same
    # alias semantics as the existing shared_pattern dimension check.
    request_action = (request.get("action") or "").strip()
    request_object = (request.get("object") or "").strip()
    request_mode = (request.get("mode") or "").strip()
    has_action = bool(request_action) and request_action not in {"unspecified", "mixed"}
    has_object = bool(request_object) and request_object not in {"unspecified", "mixed"}
    has_mode = bool(request_mode) and request_mode not in {"unspecified", "mixed"}
    target_action = operation_family(request_action) if has_action else None
    target_object = normalize(request_object) if has_object else None
    target_mode = request_mode if has_mode else None
    for premise in c_local["premises"]:
        rid = premise["requirement_id"]
        r = requirements.get(rid)
        item = covered.get(rid)
        # X-answer fix 2: atomic premise forbids analysis AND
        # comparison (output nodes aren't fact-side).
        if (
            not r
            or r["kind"] in {"analysis", "comparison"}
            or not item
            or item["status"] != "covered"
        ):
            raise SpecError("premise_not_covered")
        # X2-answer fix 2: premise products must sit INSIDE the
        # request's explicit product scope — a SUBSET check, not
        # intersection. ``intersection`` was too weak: a same-product
        # ``delete / files`` true-complete group could ride an
        # ``upload / photos`` analysis on the shared product key.
        # Different anchors are still allowed — q1 fact / q2
        # analysis is legal as long as the products nest cleanly.
        premise_products = _product_canonicals(r)
        if request_product_canon and not (premise_products <= request_product_canon):
            raise SpecError("premise_product_out_of_scope")
        # X2-answer fix 2: explicit action / object / mode binding.
        # Same product / same mode / same action / same object must
        # still pass when anchors differ; the rule constrains the
        # declared dimension value, not the anchor id.
        if has_action and operation_family(r.get("action") or "") != target_action:
            raise SpecError("premise_action_out_of_scope")
        if has_object and normalize(r.get("object") or "") != target_object:
            raise SpecError("premise_object_out_of_scope")
        if has_mode and (r.get("mode") or "").strip() != target_mode:
            raise SpecError("premise_mode_out_of_scope")
        # X2-answer fix 1: empty evidence_ids expands to the current
        # server's full covered support group on the COPY (the
        # original payload is never mutated). Non-empty lists must
        # STILL exactly equal the support group — no partial
        # subsets. Per-premise length is capped at 40; the global
        # unique-fact union is checked after the loop (fix 5).
        eids = premise["evidence_ids"]
        if not eids:
            eids = list(item["evidence_ids"])
        elif set(eids) != set(item["evidence_ids"]):
            raise SpecError("premise_group_incomplete")
        if len(eids) > 40:
            raise SpecError("premise_group_incomplete")
        for eid in eids:
            f = fact_map.get(eid)
            if not f or f.claim_kind != "fact" or f.status != "confirmed" or not f.source_refs:
                raise SpecError("premise_fact_unverified")
            supports[eid] = f
        premise["evidence_ids"] = eids  # mutates c_local, never c
        premises.append(r)
    # X2-answer fix 5: the post-expansion unique-fact union is the
    # real cap on premise evidence — a 41st unique fact across all
    # premises breaks the budget regardless of per-premise length.
    if len(supports) > 40:
        raise SpecError("supports_union_too_large")
    if len({(f.run_id, f.project_id) for f in supports.values()}) != 1:
        raise SpecError("premise_scope_mismatch")
    products = {f.product_key for f in supports.values()}
    op = c_local["operator"]
    facet = c_local["facet"]
    # X-answer fix 3: strength / weakness MUST be scenario_tradeoff
    # — relabelling ``contrast`` to fill the budget is rejected.
    if facet in {"strength", "weakness"} and op != "scenario_tradeoff":
        raise SpecError("strength_weakness_requires_tradeoff")
    # X-answer fix 3: opportunity / risk MUST be proposed_test with a
    # non-empty proposal (the existing code only enforced the
    # opportunity side; risk now follows the same rule).
    if facet == "opportunity" and op != "proposed_test":
        raise SpecError("opportunity_requires_proposal")
    if facet == "risk" and op != "proposed_test":
        raise SpecError("risk_requires_proposal")
    if facet == "market_pattern" and (op != "shared_pattern" or len(products) < 2):
        raise SpecError("pattern_requires_two_products")
    # A shared pattern compares one frozen action/mode/object facet. No claim of
    # automatic/manual coexistence can borrow a missing side from another page.
    if op == "shared_pattern":
        if len(products) < 2:
            raise SpecError("pattern_requires_two_products")
        dimensions = {
            (operation_family(r["action"] or ""), normalize(r["object"] or ""), r["mode"])
            for r in premises
        }
        if len(dimensions) != 1:
            raise SpecError("pattern_facet_mismatch")
    if op == "contrast" and len(premises) < 2:
        raise SpecError("contrast_requires_two_sides")
    if op == "proposed_test" and (facet not in {"opportunity", "risk"} or not c_local["proposal"]):
        raise SpecError("proposal_invalid")
    # Per-premise product / numbered-step mapping for scenario_tradeoff.
    # Used by the derivation (X2-answer fix 4) to pair each product
    # with its OWN complete numbered flow count, and by the
    # strength / weakness step-delta guard (fix 3) so the gap is
    # preserved when all counts are equal instead of manufactured.
    premise_to_count: dict[str, int] = {}
    premise_to_product: dict[str, str] = {}
    if op == "scenario_tradeoff":
        if not scenario or len(premises) < 2:
            raise SpecError("tradeoff_requires_two_sides")
        # X-answer fix 3: scenario_tradeoff needs ≥2 different
        # products and a complete numbered flow per side. Each
        # product's flow must be independent — own numbered step
        # set — so step-count deltas compare apples to apples.
        if len(products) < 2:
            raise SpecError("tradeoff_requires_two_products")
        dimensions = {
            (operation_family(r["action"] or ""), normalize(r["object"] or ""), r["mode"])
            for r in premises
        }
        if len(dimensions) != 1:
            raise SpecError("premise_dimension_mismatch")
        for p in c_local["premises"]:
            p_underlying = requirements.get(p["requirement_id"])
            p_products = _product_canonicals(p_underlying)
            # X2-answer fix 4: each scenario_tradeoff premise must
            # resolve to exactly one product so the count → product
            # mapping in the derivation is unambiguous. A multi-
            # product premise would interleave two products' step
            # sets and the ``10 / 2`` reading would lie.
            if len(p_products) != 1:
                raise SpecError("premise_multi_product")
            product = next(iter(p_products))
            eids = p.get("evidence_ids") or []
            numbered_steps = {
                (entries[e]["unit"].source_id, entries[e]["unit"].step_index)
                for e in eids
                if e in entries and entries[e]["unit"].step_index
            }
            if not numbered_steps:
                raise SpecError("tradeoff_step_missing")
            premise_to_count[p["requirement_id"]] = len(numbered_steps)
            premise_to_product[p["requirement_id"]] = product
        # X2-answer fix 3: equal step counts across the products is
        # "label padding" — the model would otherwise fabricate a
        # strength / weakness verdict on a flow that has no
        # measurable step-axis difference. Reject so the gap is
        # preserved by complete_analysis_items.
        if facet in {"strength", "weakness"}:
            counts = list(premise_to_count.values())
            if len(counts) >= 2 and min(counts) == max(counts):
                raise SpecError("strength_weakness_requires_step_delta")
    # X-answer fix 4: the description is a BOUNDED derivation — full
    # exact source text is tracked through structured premises
    # (in evidence_location) + supporting_finding_ids, NOT pasted
    # into the user-visible description. Pasting every premise's
    # source quote would push the description past 3000 chars on a
    # normal 8-premise candidate (8 × 4 = 32 segments, easily ~3k
    # chars) and trip the false-negative analysis_description_budget.
    if op == "shared_pattern":
        derivation = "推断：仅本次产品样本均有上述同维度原文支持；不外推整个市场。"
    elif op == "contrast":
        derivation = "推断：以上是同一研究问题下不同要求的原文对照；不能据此断言效果或优劣。"
    elif op == "scenario_tradeoff":
        # X2-answer fix 4: pair each product with its OWN numbered
        # step count so the user can tell which product owns which
        # count. Sorted ascending by count, then by product name for
        # stable display. Strength frames the shorter flow as a
        # 待验证场景优势假设; weakness frames the longer flow as a
        # 待验证场景劣势假设. Neither may claim time / experience /
        # quality is already better.
        product_count_pairs = sorted(
            [(premise_to_product[k], premise_to_count[k]) for k in premise_to_count],
            key=lambda x: (x[1], x[0]),
        )
        counts_str = ", ".join(f"{p}: {c} 步" for p, c in product_count_pairs)
        delta = max(premise_to_count.values()) - min(premise_to_count.values())
        if facet == "strength":
            adv_product = min(product_count_pairs, key=lambda x: x[1])[0]
            derivation = (
                f"推断：可比较的显式编号步骤数 {counts_str}；差额 {delta}。"
                f"较少的显式编号步骤数作为待验证场景优势假设（产品：{adv_product}）；"
                f"不可断言耗时、体验或质量已更优。"
            )
        elif facet == "weakness":
            dis_product = max(product_count_pairs, key=lambda x: x[1])[0]
            derivation = (
                f"推断：可比较的显式编号步骤数 {counts_str}；差额 {delta}。"
                f"较多的显式编号步骤数作为待验证场景劣势假设（产品：{dis_product}）；"
                f"不可断言耗时、体验或质量已更差。"
            )
        else:
            derivation = (
                f"推断：可比较的显式编号步骤数 {counts_str}；差额 {delta}。"
                f"步骤数差异不能直接证明耗时、体验或质量，优劣待用户验证。"
            )
    else:
        derivation = (
            "建议（待验证）：" + c_local["proposal"] + "；这是测试提案，不是已证实的产品事实。"
        )
    description = (f"场景（输入）：{scenario}\n" if scenario else "") + derivation
    # Defensive budget guard. The bounded derivation + MAX_SCENARIO=200
    # scenario can never reach this on a normal candidate, but unknown
    # future extensions (longer scenario strings, custom derivations)
    # should still fail loudly instead of silently bloating.
    if len(description) > 3000:
        raise SpecError("analysis_description_budget")
    first = next(iter(supports.values()))
    label = _LABEL[facet]
    kind = "recommendation" if op == "proposed_test" else "inference"
    return Claim(
        id=uuid4(),
        run_id=first.run_id,
        project_id=first.project_id,
        category=_CATEGORY.get(facet, facet),
        title=label,
        description=description,
        status="not_verified",
        source_refs=(),
        claim_kind=kind,
        supporting_finding_ids=tuple(f.id for f in supports.values()),
        evidence_location=dict(
            version="research-analysis-v7",
            requirement_id=request["id"],
            facet=facet,
            operator=op,
            # Resolved support groups ride on the COPY; the caller's
            # payload is untouched (X2-answer fix 1).
            premises=c_local["premises"],
            scenario=scenario,
            proposal=c_local["proposal"],
            semantic_review="NOT_MEASURED",
        ),
        evidence_id="",
        product_key="",
    )


def analyses(spec, candidates, report, facts, entries):
    if not isinstance(candidates, list) or len(candidates) > 8:
        raise SpecError("analysis_budget_invalid")
    rows = []
    issues = []
    for n, c in enumerate(candidates):
        try:
            rows.append(_one(spec, c, report, facts, entries))
        except SpecError as error:
            issues.append(dict(candidate_index=n, code=str(error)))
    return tuple(rows), issues


def complete_analysis_items(spec, report, rows):
    """All frozen facets need valid products; the model cannot grade completion."""
    present = {}
    for row in rows:
        meta = row.evidence_location
        present.setdefault(meta["requirement_id"], set()).add(meta["facet"])
    items = []
    by_id = {i["requirement_id"]: i for i in report["items"]}
    for r in spec["requirements"]:
        item = by_id.get(
            r["id"], dict(status="not_verified", evidence_ids=[], gap="evidence_missing")
        )
        if r["kind"] == "analysis":
            missing = set(r["analysis_facets"]) - present.get(r["id"], set())
            valid = bool(r["analysis_facets"]) and not missing
            item = item | dict(
                status="covered"
                if valid
                else "partial"
                if present.get(r["id"])
                else "not_verified",
                reason_codes=[] if valid else ["analysis_facets_missing"],
                gap=", ".join(sorted(missing)),
                analysis_facets=sorted(present.get(r["id"], set())),
            )
        items.append(item)
    return report | dict(
        items=items,
        overall_completable=bool(items)
        and not spec["unresolved"]
        and all(i["status"] == "covered" for i in items),
    )


def build_answer(spec, report, facts, rows):
    fact_map = {f.evidence_id: f for f in facts}
    items = []
    by_id = {i["requirement_id"]: i for i in report["items"]}
    for r in spec["requirements"]:
        item = by_id.get(
            r["id"], dict(status="not_verified", evidence_ids=[], gap="evidence_missing")
        )
        quotes = []
        for eid in item["evidence_ids"]:
            f = fact_map.get(eid)
            if not f or f.status != "confirmed":
                raise SpecError("answer_fact_missing")
            quotes.append(
                dict(
                    finding_id=str(f.id),
                    evidence_id=eid,
                    text=f.description,
                    provider_source_id=f.source_refs[0][0],
                    url=f.source_refs[0][1],
                )
            )
        items.append(
            dict(
                requirement_id=r["id"],
                question=r["question_text"],
                kind=r["kind"],
                status=item["status"],
                answer_polarity=item.get("answer_polarity", "unknown"),
                quotes=quotes,
                gap=item["gap"],
                adopted_attempt=item.get("adopted_attempt"),
                version_constraints=item.get("version_constraints", []),
                conflict_groups=item.get("conflict_groups", []),
            )
        )
    return dict(
        version="research-answer-v7",
        items=items,
        unresolved=spec["unresolved"],
        analyses=[
            dict(
                finding_id=str(row.id),
                title=row.title,
                description=row.description,
                claim_kind=row.claim_kind,
                supporting_finding_ids=[str(i) for i in row.supporting_finding_ids],
                **row.evidence_location,
            )
            for row in rows
        ],
        semantic_review="NOT_MEASURED",
    )
