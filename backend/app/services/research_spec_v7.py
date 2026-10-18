"""One versioned question contract and cumulative budgets; no I/O or model ratings."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

KIND_ENUM = (
    "capability",
    "procedure",
    "entry_listing",
    "destination",
    "condition",
    "limit",
    "version",
    "comparison",
    "analysis",
)
PLATFORM_ENUM = (
    "Android",
    "iOS",
    "iPadOS",
    "iPhone",
    "iPad",
    "mobile",
    "phone",
    "tablet",
    "desktop",
    "web",
    "Windows",
    "macOS",
    "Linux",
    "unspecified",
)
MODE_ENUM = ("manual", "automatic", "mixed", "unspecified")
QUANTITY_ENUM = ("single", "multiple", "unspecified")
ANALYSIS_FACETS = (
    "competitor",
    "market_pattern",
    "strength",
    "weakness",
    "opportunity",
    "risk",
    "provenance",
)
MAX_SCENARIO = 200


class SpecError(ValueError):
    """Only fixed codes and schema-owned field paths may leave this module."""


def closed(properties: dict) -> dict:
    return dict(
        type="object", properties=properties, required=list(properties), additionalProperties=False
    )


def string(maximum=200, nullable=False):
    return dict(type=["string", "null"] if nullable else "string", minLength=1, maxLength=maximum)


def array(items, maximum, minimum=0):
    return dict(type="array", items=items, minItems=minimum, maxItems=maximum, uniqueItems=True)


REQUIREMENT_SCHEMA = closed(
    dict(
        id=string(80),
        anchor_ids=array(string(80), 16, 1),
        kind=dict(type="string", enum=list(KIND_ENUM)),
        products=array(string(100), 4, 1),
        platform=dict(type=["string", "null"], enum=[None, *PLATFORM_ENUM]),
        entry=string(200, True),
        action=string(100, True),
        object=string(100, True),
        mode=dict(type="string", enum=list(MODE_ENUM)),
        quantity=dict(type="string", enum=list(QUANTITY_ENUM)),
        condition_quotes=array(string(500), 8),
        version=string(100, True),
        analysis_facets=array(dict(type="string", enum=list(ANALYSIS_FACETS)), 8),
    )
)
REQUIREMENT_SCHEMA["properties"]["mode"]["description"] = (
    "Operation mode ONLY: manual, automatic, mixed, unspecified. NEVER multiple/single here; those are quantity."
)
REQUIREMENT_SCHEMA["properties"]["platform"]["description"] = (
    "One declared platform enum or null; never a slash-combined value like iPhone/iOS."
)
PLANNING_SCHEMA = closed(
    dict(
        requirements=array(REQUIREMENT_SCHEMA, 16),
        queries=array(closed(dict(id=string(80), text=string(200))), 3),
    )
)


def validate(schema: dict, value: Any, path: str = "candidate") -> None:
    """Validate the small JSON-Schema subset used by both versioned wire contracts."""
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    match = (
        value is None
        and "null" in types
        or isinstance(value, str)
        and "string" in types
        or isinstance(value, dict)
        and "object" in types
        or isinstance(value, list)
        and "array" in types
        or type(value) is int
        and "integer" in types
    )
    if not match:
        raise SpecError("schema_type:" + path)
    if "enum" in schema and value not in schema["enum"]:
        raise SpecError("schema_enum:" + path)
    if value is None:
        return
    if isinstance(value, str):
        if len(value.strip()) < schema.get("minLength", 0) or len(value) > schema.get(
            "maxLength", 10000
        ):
            raise SpecError("schema_length:" + path)
    elif isinstance(value, dict):
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise SpecError("schema_extra:" + path)
        if set(schema.get("required", [])) - set(value):
            raise SpecError("schema_missing:" + path)
        for key in props:
            if key in value:
                validate(props[key], value[key], path + "." + key)
    elif isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100):
            raise SpecError("schema_count:" + path)
        if schema.get("uniqueItems") and len(
            {json.dumps(v, sort_keys=True, ensure_ascii=False) for v in value}
        ) != len(value):
            raise SpecError("schema_duplicate:" + path)
        for n, item in enumerate(value):
            validate(schema["items"], item, f"{path}[{n}]")


# Policies are removed once by their source-language syntax, never by a product answer table.
_POLICY = re.compile(
    r"\b(?:according to|per|using only|based on|cite only)\b[^;?！!。？\n,:：，]*?"
    r"(?:help(?: pages)?|support(?: pages)?|documentation|docs)\b|"
    r"请?(?:仅|只)(?:引用|依据|根据)[^:：，,。？！;；\n]*?(?:官方(?:帮助|支持|文档)|帮助|支持|文档)|"
    r"\bif official documentation is absent,\s*mark not verified\b|"
    r"\bdo not infer unsupported capability\b|"
    r"\bdistinguish facts,?\s*inferences (?:and|&) recommendations\b|"
    r"(?:清楚)?区分事实[、，,]?(?:推断|推论)(?:与|和|、)建议",
    re.I,
)
_BOUNDARY = re.compile(r"[;；?？!！。\n]|\.(?=\s|$)")
_CONDITIONS = re.compile(
    r"\b(?:while|when|unless|without|if|before|after)\b[^;?！!。？\n.]*|\boffline\b|(?:离线|(?:禁用|关闭)(?!条件))[^，,；。？！\n]*",
    re.I,
)
_MULTIPLE = re.compile(r"\b(?:multiple|several|multi[- ]select|batch)\b|多张|多选|批量", re.I)


def question_parts(question: str) -> dict:
    if not isinstance(question, str) or not question.strip() or len(question) > 500:
        raise SpecError("question_invalid")
    policies = [
        dict(start=m.start(), end=m.end(), text=m.group()) for m in _POLICY.finditer(question)
    ]
    ranges = []
    start = 0
    for p in policies:
        if start < p["start"]:
            ranges.append((start, p["start"]))
        start = p["end"]
    if start < len(question):
        ranges.append((start, len(question)))
    anchors = []
    for left, right in ranges:
        cursor = left
        for end in [*(left + m.end() for m in _BOUNDARY.finditer(question[left:right])), right]:
            a, b = cursor, end
            cursor = end
            while a < b and (question[a].isspace() or question[a] in ",，:：;；.。?？!！"):
                a += 1
            while b > a and (question[b - 1].isspace() or question[b - 1] in ",，:：;；.。?？!！"):
                b -= 1
            if a < b:
                anchors.append(dict(id=f"q{len(anchors) + 1}", start=a, end=b, text=question[a:b]))
    return dict(policy_spans=policies, anchors=anchors)


def condition_inventory(text: str) -> list[str]:
    return list(
        dict.fromkeys(m.group().strip(" ,，:：?？.!！") for m in _CONDITIONS.finditer(text))
    )


def condition_spans(text: str) -> list[tuple[int, int]]:
    """Return absolute ``(start, end)`` spans of condition clauses.

    Same grammar as ``_CONDITIONS`` so the input-obligation ledger
    filters the SAME ranges the spec layer's ``mandatory_conditions``
    guard tracks. A clause named in mandatory_conditions (e.g. ``关闭
    相机上传后`` or ``when camera uploads are disabled``) never counts
    as a new requested action — copying only the trigger word would
    leak the inner verb back into the obligation ledger.
    """
    return [(m.start(), m.end()) for m in _CONDITIONS.finditer(text)]


VERSION_TOKEN = re.compile(
    r"\b(iPadOS|iOS|macOS|Android|Windows|Linux|version)\s*(\d+(?:\.\d+)*)\b", re.I
)


def version_mentions(text):
    return [
        dict(platform=m[1].casefold(), version=m[2], start=m.start(), end=m.end(), text=m.group())
        for m in VERSION_TOKEN.finditer(text)
    ]


def analysis_inventory(text: str) -> set[str]:
    """Output obligations from the request grammar, never from source answers."""
    grammar = {
        "market_pattern": r"\b(?:patterns?|trends?)\b|样本.*模式|产品.*模式|给出.*模式",
        "strength": r"\bstrengths?\b|优点|优势|优缺点",
        "weakness": r"\bweaknesses?\b|缺点|劣势|优缺点",
        "opportunity": r"\bopportunit(?:y|ies)\b|机会",
        "risk": r"\brisks?\b|风险",
    }
    found = {facet for facet, pattern in grammar.items() if re.search(pattern, text, re.I)}
    if not found and re.search(r"\banaly[sz](?:e|is)\b|分析", text, re.I):
        found.add("competitor")
    return found


def compile_spec(question: str, candidate: dict, scope: str | None = None) -> dict:
    validate(PLANNING_SCHEMA, candidate)
    if scope is not None and (not isinstance(scope, str) or len(scope) > 2000):
        raise SpecError("scope_invalid")
    parts = question_parts(question)
    anchors = {a["id"]: a for a in parts["anchors"]}
    # Lazy import: keeps spec module loadable even if the registry changes
    # shape, and avoids pulling research_service into the spec import path.
    from app.services import research_input_v7 as input_v7

    input_atoms = input_v7.extract_input_atoms(
        question, anchors=anchors, anchor_list=parts["anchors"]
    )
    atoms_by_anchor: dict[str, list[dict]] = {}
    for atom in input_atoms:
        aid = atom.get("anchor_id")
        if aid is not None:
            atoms_by_anchor.setdefault(aid, []).append(atom)
    question_product_canonicals = {
        a["canonical"] for a in input_atoms if a["dimension"] == "product"
    }
    requirements = copy.deepcopy(candidate["requirements"])
    ids = set()
    covered = set()
    unresolved = []
    normalizations: list[dict] = []
    context = question + " " + (scope or "")
    requested_facets = analysis_inventory(context)
    # Input obligations: ordered (action, object, mode) tuples per anchor.
    # Each factual requirement must fulfill one of these; missing tuples
    # become explicit ``unresolved`` gaps. ``analysis`` and ``comparison``
    # never satisfy a single-action obligation.
    input_obligations = input_v7.extract_input_obligations(question, anchor_list=parts["anchors"])

    # Scope-as-global-constraint: when the ORIGINAL question is silent
    # on a dimension (platform / mode / quantity / entry), every
    # obligation inherits the scope's value on the SAME per-action
    # binding the question already uses. When the question is
    # EXPLICIT on a dimension AND the scope contradicts it, the
    # conflict is recorded as an ``unresolved`` entry with a fixed
    # ``scope_*_conflicts_with_question`` code — neither side is
    # silently erased, and the downstream per-relation check still
    # uses the question-owned scope for matching. Scope-source atoms
    # keep the exact ``id`` / ``start`` / ``end`` / ``text`` /
    # ``canonical`` / ``source`` shape input_v7 emits for
    # question-source atoms so downstream consumers parse one
    # shape, not two.
    # iOS family collapse — kept at the compile_spec top-level so the
    # multi-relation fill guard and the lookalike post-pass can call it
    # even when the original ``scope`` argument is empty (W5 fix ③).
    # Treats ``iPhone`` / ``iPad`` / ``iOS`` / ``iPadOS`` as one class
    # on both sides of the comparison.
    def _platform_class(c: str) -> str:
        cu = c.casefold()
        if cu in {"ios", "ipados", "iphone", "ipad"}:
            return "ios"
        return cu

    _scope_atoms_list: list[dict] = []
    if scope:
        _scope_atoms_list = input_v7.extract_scope_atoms(scope or "")

    _SCOPE_CONFLICT_CODE = {
        "platform": "scope_platform_conflicts_with_question",
        "mode": "scope_mode_conflicts_with_question",
        "quantity": "scope_quantity_conflicts_with_question",
        "entry": "scope_entry_conflicts_with_question",
    }

    if _scope_atoms_list:
        # Group scope atoms by dimension once, outside the per-obligation
        # loop, so each obligation does not repeat the bucketing work.
        _scope_by_dim: dict[str, list[dict]] = {}
        for _sa in _scope_atoms_list:
            _scope_by_dim.setdefault(_sa["dimension"], []).append(_sa)

        for ob in input_obligations:
            for dim, scope_atoms_for_dim in _scope_by_dim.items():
                if dim not in {"platform", "mode", "quantity", "entry"}:
                    continue
                # Question-owned scope atoms (source="question") on this
                # obligation for this dimension. Scope-source atoms are
                # NEVER compared against themselves here — they only
                # conflict with question atoms.
                field = f"{dim}_scope"
                q_source_atoms = [a for a in (ob.get(field) or []) if a.get("source") == "question"]
                if not q_source_atoms:
                    # Question silent: scope binds to this obligation
                    # unchanged. The augmentation keeps the same shape
                    # input_v7 emits for question-source atoms; the
                    # ``source`` field tracks the provenance so the
                    # per-relation check below can still distinguish.
                    for sa in scope_atoms_for_dim:
                        # Skip duplicates: if scope declares the same
                        # canonical as a question-source atom, do not
                        # append twice. (Question silent here by
                        # construction.)
                        ob.setdefault(field, []).append(
                            dict(
                                id=sa["id"],
                                canonical=sa["canonical"],
                                text=sa["text"],
                                start=sa["start"],
                                end=sa["end"],
                                source=sa["source"],
                            )
                        )
                    continue
                # Question has explicit atoms: check for conflict.
                q_classes: set[str] = set()
                if dim == "platform":
                    q_classes = {
                        _platform_class(p["canonical"])
                        for p in q_source_atoms
                        if p.get("canonical") and p["canonical"] != "unspecified"
                    }
                elif dim == "entry":
                    # Entry "canonical" stays the surface text (entry
                    # forms a closed stem set the model treats as an
                    # opaque label, so a lower-cased identity is the
                    # comparison key).
                    q_classes = {p["text"] for p in q_source_atoms}
                else:
                    q_classes = {
                        p["canonical"]
                        for p in q_source_atoms
                        if p.get("canonical") in {"manual", "automatic", "single", "multiple"}
                    }
                for sa in scope_atoms_for_dim:
                    if dim == "platform":
                        sa_class = _platform_class(sa["canonical"])
                    elif dim == "entry":
                        sa_class = sa["text"]
                    else:
                        sa_class = sa["canonical"]
                    domains = {
                        "mobile": {"ios", "android"},
                        "phone": {"ios", "android"},
                        "tablet": {"ios", "android"},
                    }
                    compatible = sa_class in q_classes or (
                        dim == "platform"
                        and any(
                            domains.get(sa_class, {sa_class}) & domains.get(qc, {qc})
                            for qc in q_classes
                        )
                    )
                    if not compatible:
                        unresolved.append(
                            dict(
                                id=f"q-scope-conflict-{dim}-{ob['id']}-{sa['id']}",
                                text=ob.get("text") or ob["id"],
                                reason=_SCOPE_CONFLICT_CODE[dim],
                                q_canonicals=sorted(q_classes),
                                scope_canonical=sa_class,
                            )
                        )
                        # DO NOT add the conflicting scope atom to ob.

    def _scope_atoms(r):
        """Atoms in scope for requirement r.

        Anchors first. When the question names more than one product, also
        keep atoms whose owner overlaps this requirement's product list.
        Output / entry atoms default to all products because they describe
        the whole request.
        """
        r_products = {input_v7.canonical_product(p) or p.casefold() for p in r.get("products", [])}
        multi_product = len(question_product_canonicals) > 1 and bool(r_products)
        out: list[dict] = []
        for aid in r["anchor_ids"]:
            for atom in atoms_by_anchor.get(aid, []):
                if (
                    multi_product
                    and atom["dimension"] not in {"output", "entry"}
                    and atom["owners"]
                ):
                    if not (set(atom["owners"]) & r_products):
                        continue
                out.append(atom)
        return out

    for n, r in enumerate(requirements):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", r["id"]) or r["id"] in ids:
            raise SpecError("requirement_id_invalid")
        ids.add(r["id"])
        if not set(r["anchor_ids"]) <= set(anchors):
            raise SpecError("anchor_unknown")
        if any(p.casefold() not in context.casefold() for p in r["products"]):
            raise SpecError("product_not_in_input")

        # Analysis kind must be checked BEFORE any per-dimension replacement
        # check: an ``analysis`` requirement answers a question-wide synthesis,
        # not a single (action, object, mode) leaf. If the original question
        # named no analysis facets the candidate may not silently elevate to
        # ``analysis`` and ride a single-action obligation through.
        if r["kind"] == "analysis" and not requested_facets:
            raise SpecError("analysis_not_requested")

        # === Input obligation ledger checks =================================
        # A requirement may not silently replace any original dimension. We
        # compare each typed r field with the canonical form of the
        # question atoms IN SCOPE; a single mismatch raises a fixed
        # SpecError so the model cannot keep an anchor while swapping its
        # action / object / mode / platform / entry / product coverage.
        #
        # ``analysis`` and ``comparison`` are OUTPUT-level kinds: they
        # may not be silently demoted to single-leaf fact answers. The
        # leaf-dimension fill / replace checks below would let a
        # generic mobile-only sentence accidentally score a fact's
        # mode / object. The user instruction "叶子维度强检查不得作用
        # 于 analysis / comparison" gates those checks behind a
        # factual-kind predicate.
        leaf_dim_check = r["kind"] not in {"analysis", "comparison"}
        scope_atoms = _scope_atoms(r)
        scope_seen_action_canon = {
            a["canonical"] for a in scope_atoms if a["dimension"] == "action"
        }
        scope_seen_object_canon = {
            a["canonical"] for a in scope_atoms if a["dimension"] == "object"
        }
        scope_seen_mode_canon = {a["canonical"] for a in scope_atoms if a["dimension"] == "mode"}
        scope_seen_platform_canon = {
            a["canonical"] for a in scope_atoms if a["dimension"] == "platform"
        }

        # Action: missing / "" → fill from scope's unique action atom; a
        # filled value that doesn't canonicalise OR that doesn't appear in
        # the scope raises a fixed SpecError so the model can't quietly
        # swap "upload" for "remove". Skipped for analysis / comparison.
        if leaf_dim_check:
            if r["action"] in (None, ""):
                if len(scope_seen_action_canon) == 1:
                    scope_action_atom = next(a for a in scope_atoms if a["dimension"] == "action")
                    r["action"] = scope_action_atom["text"]
            elif r["action"]:
                target_action = input_v7.canonical_action(r["action"])
                if not target_action:
                    if "+" in (r["action"] or ""):
                        # Compound-but-unrecognised family. Keep the
                        # surface (casefold) so action_family_parts can
                        # still split it into leaves; an unknown verb
                        # inside the leaves will trip ``extra`` below.
                        target_action = (r["action"] or "").strip().casefold()
                    else:
                        raise SpecError("action_unknown")
                if scope_seen_action_canon:
                    # Decompose ``select and backup`` into its verb
                    # parts and check each against the question's
                    # declared action atoms individually. An
                    # "auxiliary" half (select / choose / tap /
                    # press / start / …) is allowed when the same
                    # word literally appears in the original
                    # question — ``_ACTION_ATOMS`` does not register
                    # it as a real action atom, so
                    # ``scope_seen_action_canon`` alone would reject
                    # ``select and backup`` even when the user wrote
                    # it that way. Real operation verbs still have
                    # to belong to the question's declared set so
                    # ``select and delete`` cannot smuggle a new
                    # verb past the gate.
                    target_parts = set(input_v7.action_family_parts(target_action))
                    aux_in_question = set(input_v7.auxiliary_verbs_in_text(question))
                    extended_scope = scope_seen_action_canon | aux_in_question
                    extra = target_parts - extended_scope
                    if extra:
                        raise SpecError("action_replaced")
            # Object: same fill / replace contract as action.
            if r["object"] in (None, ""):
                if len(scope_seen_object_canon) == 1:
                    scope_object_atom = next(a for a in scope_atoms if a["dimension"] == "object")
                    r["object"] = scope_object_atom["text"]
            elif r["object"]:
                target_object = input_v7.canonical_object(r["object"])
                if not target_object:
                    raise SpecError("object_unknown")
                if scope_seen_object_canon and target_object not in scope_seen_object_canon:
                    raise SpecError("object_replaced")
            # Mode: ``None`` / ``""`` / ``"unspecified"`` all mean "no
            # declared mode"; fill from the scope's unique mode atom
            # when available.
            if r["mode"] in (None, "", "unspecified"):
                if len(scope_seen_mode_canon) == 1:
                    scope_mode_atom = next(a for a in scope_atoms if a["dimension"] == "mode")
                    r["mode"] = scope_mode_atom["canonical"]
            else:
                target_mode = input_v7.canonical_mode(r["mode"])
                if not target_mode:
                    raise SpecError("mode_unknown")
                # ``mixed`` is ONLY legal for ``comparison`` requirements
                # — the leaf-action ``capability`` / ``procedure``
                # branches must pick one of the question's declared leaf
                # modes (manual / automatic).
                if (
                    target_mode not in {"mixed", "unspecified"}
                    and scope_seen_mode_canon
                    and target_mode not in scope_seen_mode_canon
                ):
                    raise SpecError("mode_replaced")
                if target_mode == "mixed" and r["kind"] != "comparison":
                    raise SpecError("mode_mixed_requires_comparison")
            # Platform: fill from scope's unique platform atom.
            # The question-wide platform compatibility check that
            # used to live here has been removed; platform /
            # entry constraints now travel with each input
            # obligation (``obligation.platform_scope`` /
            # ``obligation.entry_scope``) so per-action binding is
            # enforced through ``_fulfills`` instead of through an
            # anchor-wide scan that would silently accept a swapped
            # Android / iPhone pair or two different named entries.
            if r["platform"] in (None, "", "unspecified"):
                if len(scope_seen_platform_canon) == 1:
                    scope_platform_atom = next(
                        a for a in scope_atoms if a["dimension"] == "platform"
                    )
                    # Atom canonical is now a display enum (e.g.
                    # ``Android``) so the back-fill stays schema-legal
                    # for the downstream PLATFORM_ENUM check.
                    r["platform"] = scope_platform_atom["canonical"]

        # Entry fill is intentionally deferred to the per-obligation
        # fill below (after ``_fulfills``). Two named entries
        # (``Shared tab`` + ``Other tab``) can live in the same anchor;
        # first-pick-from-anchor would collapse them onto the first
        # leaf and silently swap the second leaf's tab. Per-obligation
        # fill uses the matched obligation's ``entry_scope`` so each
        # leaf gets exactly the entry its own (action, object) pair
        # binds to.

        # HOW or "complete steps" implies the question wants a procedure
        # rather than a bare capability. Auto-upgrade the kind before any
        # unsupported capability answer is built. Scoped to this
        # requirement's anchor text so a HOW in one question does NOT
        # upgrade an unrelated ``capability`` in a sibling anchor.
        r_anchor_text = "; ".join(anchors[a]["text"] for a in r["anchor_ids"])
        r_how_trigger = bool(re.search(r"\bhow\b|\b如何\b|\b怎么\b", r_anchor_text, re.I))
        r_complete_steps_trigger = bool(
            re.search(r"\bcomplete steps?\b|\b完整.*步骤\b|\b完整.*流程\b", r_anchor_text, re.I)
        )
        how_clause = re.split(
            r"\b(?:distinguish|separate|compare)\b|区分|对比|比较",
            r_anchor_text,
            maxsplit=1,
            flags=re.I,
        )[0]
        how_modes = {
            a["canonical"]
            for a in input_v7.extract_input_atoms(how_clause)
            if a["dimension"] == "mode"
        }
        if how_modes and r["mode"] in {"manual", "automatic"} and r["mode"] not in how_modes:
            r_how_trigger = r_complete_steps_trigger = False
        if r["kind"] == "capability" and (r_how_trigger or r_complete_steps_trigger):
            r["kind"] = "procedure"
        pure_condition_kind = r["kind"] == "condition"
        kept_conditions: list[str] = []
        for m_idx, q in enumerate(r["condition_quotes"]):
            is_policy = any(
                q.casefold() in p["text"].casefold() or p["text"].casefold() in q.casefold()
                for p in parts["policy_spans"]
            )
            not_in_input = q not in context
            if is_policy:
                if pure_condition_kind:
                    raise SpecError("condition_is_source_policy")
                normalizations.append(
                    dict(
                        path=f"requirements[{n}].condition_quotes[{m_idx}]",
                        code="source_policy_removed",
                    )
                )
                continue
            if not_in_input:
                if pure_condition_kind:
                    raise SpecError("condition_not_in_input")
                normalizations.append(
                    dict(
                        path=f"requirements[{n}].condition_quotes[{m_idx}]",
                        code="non_input_condition_removed",
                    )
                )
                continue
            if q not in condition_inventory(context):
                if pure_condition_kind:
                    raise SpecError("condition_not_business_clause")
                normalizations.append(
                    dict(
                        path=f"requirements[{n}].condition_quotes[{m_idx}]",
                        code="non_business_condition_removed",
                    )
                )
                continue
            kept_conditions.append(q)
        r["condition_quotes"] = kept_conditions
        covered.update(r["anchor_ids"])
        r["question_text"] = "; ".join(anchors[a]["text"] for a in r["anchor_ids"])
        # X-axes fix ①(a): do NOT collapse the requirement's
        # ``quantity`` from the whole-anchor regex scan. That
        # overwrite destroyed the model's candidate-level
        # ``proposed_quantity`` before ``_fulfills`` ever saw it,
        # so a candidate declaring ``single`` for the upload-one-photo
        # leaf could still satisfy the upload-multiple-photos
        # obligation (and vice versa). The per-obligation quantity is
        # now checked in ``_fulfills`` against the model's
        # ``proposed_quantity``; ``mandatory_quantity`` is kept as the
        # anchor-derived hint for the binding pass below so a silent
        # question that ends up with a single specific matched
        # obligation can still back-fill cleanly.
        r["proposed_quantity"] = r["quantity"]
        r["mandatory_quantity"] = (
            "multiple"
            if _MULTIPLE.search(r["question_text"])
            else "single"
            if re.search(r"\bone (?:photo|video|file)\b|一张|一个文件", r["question_text"], re.I)
            else "unspecified"
        )
        r["requested_versions"] = version_mentions(r["question_text"]) + version_mentions(
            scope or ""
        )
        platform_versions = {
            "iphone": {"ios"},
            "ios": {"ios", "ipados"},
            "ipad": {"ios", "ipados"},
            "ipados": {"ipados"},
            "android": {"android"},
            "phone": {"ios", "android"},
            "mobile": {"ios", "ipados", "android"},
            "tablet": {"ios", "ipados", "android"},
            "macos": {"macos"},
            "windows": {"windows"},
            "linux": {"linux"},
            "desktop": {"macos", "windows", "linux"},
        }
        permitted = platform_versions.get((r["platform"] or "").casefold())
        if permitted:
            r["requested_versions"] = [
                v
                for v in r["requested_versions"]
                if v["platform"] in permitted or v["platform"] == "version"
            ]
        if r["version"] is not None and r["version"].casefold() not in context.casefold():
            proposed = {(v["platform"], v["version"]) for v in version_mentions(r["version"])}
            available = {(v["platform"], v["version"]) for v in version_mentions(context)}
            if not proposed or not proposed <= available:
                raise SpecError("version_not_in_input")
        if (
            r["kind"] == "comparison"
            and re.search(r"\bmanual(?:ly)?\b|手动", r["question_text"], re.I)
            and re.search(r"\bautomatic(?:ally)?\b|自动", r["question_text"], re.I)
        ):
            r["proposed_mode"] = r["mode"]
            r["mode"] = "mixed"
        r["mandatory_conditions"] = condition_inventory(r["question_text"]) + condition_inventory(
            scope or ""
        )
        for c in r["mandatory_conditions"]:
            core = re.sub(r"^(?:while|when|if|unless|before|after)\s+", "", c, flags=re.I)
            if not any(core.casefold() in q.casefold() for q in r["condition_quotes"]):
                unresolved.append(
                    dict(
                        id=f"{r['id']}-condition-{len(unresolved)}",
                        text=c,
                        reason="condition_omitted",
                    )
                )
        r["fulfills_obligation_ids"] = []

    # Input-obligation ledger: each factual (capability / procedure /
    # version / entry_listing / destination / condition / limit) requirement
    # must fulfil exactly one input obligation whose anchor / product
    # branch / action-object-mode triple matches. ``analysis`` and
    # ``comparison`` never prove a single-action contract and never
    # consume an obligation.
    def _fulfills(r: dict, ob: dict) -> bool:
        if r["kind"] in {"analysis", "comparison"}:
            return False
        # Anchor scope: a requirement may only fulfil an obligation
        # whose anchor it is bound to.
        if ob.get("anchor_id") and ob["anchor_id"] not in r.get("anchor_ids", []):
            return False
        # Owner scope: each obligation is now fanned out per owner. A
        # requirement fulfils the obligation when the obligation's
        # UNIQUE owner is part of the requirement's product list — the
        # per-product "every product has its own complete flow" guard
        # already lives in the support layer, so the spec layer does
        # NOT require r_owner_canon to be a subset of ob owners. This
        # keeps a OneDrive-only requirement matching the OneDrive
        # obligation without forcing the support-test answer table
        # to construct a fake two-product requirement first.
        r_owner_canon = {
            input_v7.canonical_product(p) or p.casefold() for p in r.get("products", [])
        }
        ob_owners = ob.get("owners") or []
        if ob_owners:
            owners_set = set(ob_owners)
            if not (owners_set & r_owner_canon):
                return False
        # Action: simple canonical equality, plus operation-family
        # aliases (e.g. ``select and backup`` -> ``select+backup``).
        r_action_raw = input_v7.canonical_action(r.get("action") or "") or ""
        if r.get("action"):
            if not r_action_raw and "+" in (r.get("action") or ""):
                r_action_raw = (r.get("action") or "").strip().casefold()
        ob_action = ob.get("action_canon") or ""
        if ob_action:
            r_action_parts = set(input_v7.action_family_parts(r_action_raw))
            if r_action_raw and r_action_raw not in r_action_parts:
                r_action_parts.add(r_action_raw)
            if ob_action not in r_action_parts:
                return False
        # Object: simple canonical OR compound match (a candidate
        # answering ``photos|videos`` may satisfy either member).
        r_object_raw = input_v7.canonical_object(r.get("object") or "") or ""
        ob_object = ob.get("object_canon") or ""
        if ob_object:
            r_object_members = set(input_v7.object_compound_members(r_object_raw))
            if r_object_raw and r_object_raw not in r_object_members:
                r_object_members.add(r_object_raw)
            if ob_object not in r_object_members:
                return False
        # Mode: the obligation carries both ``mode_canon`` (question-
        # derived singular mode) and ``mode_scope`` (per-relation
        # mode list, mixing question-source atoms and scope-source
        # atoms). BOTH must participate in the candidate match —
        # relying on ``mode_canon`` alone lets an empty r.mode
        # silently bypass a Manual scope declaration. The unique
        # single-canonical case ALLOWS an empty r.mode to match so
        # the per-obligation fill below can back-fill the field;
        # a non-empty r.mode that disagrees with the scope canonical
        # fails the obligation match outright.
        r_mode_raw = input_v7.canonical_mode(r.get("mode") or "") or ""
        r_mode_lit = (r.get("mode") or "").strip().casefold()
        if r_mode_lit in {"", "unspecified", "mixed"}:
            r_mode_eff = ""
        else:
            r_mode_eff = r_mode_raw or r_mode_lit
        ob_mode_scope = [
            m["canonical"]
            for m in (ob.get("mode_scope") or [])
            if m.get("canonical") in {"manual", "automatic"}
        ]
        if ob_mode_scope:
            _unique_mode = set(ob_mode_scope)
            if len(_unique_mode) == 1:
                _scope_mode = next(iter(_unique_mode))
                if r_mode_eff and r_mode_eff != _scope_mode:
                    return False
                # r_mode_eff == "" here is fine: it can be filled
                # from the unique scope canonical after matching.
            else:
                if r_mode_eff not in _unique_mode:
                    return False
        # Defence-in-depth: ``mode_canon`` was the original strict
        # check. When ``mode_scope`` carries canonicals, the unique
        # case already matched the answer (and allowed empty for
        # fill); when ``mode_scope`` is empty, ``mode_canon`` may
        # still pin a question-derived mode. The strict comparison
        # is preserved here so existing tests expecting
        # "unspecified cannot erase action-bound leaf modes"
        # still surface the unresolved gap rather than have the
        # answer quietly absorb the leaf.
        ob_mode = ob.get("mode_canon") or ""
        if ob_mode and r_mode_eff != ob_mode:
            return False
        # Quantity: the obligation carries the question-derived
        # quantity atom (its own ``quantity``) plus the
        # ``quantity_scope`` canonicals (typically one per
        # segment-anchored object). X-axes fix ①(b) used to skip
        # this check entirely, letting a ``single`` candidate
        # consume a ``multiple`` obligation (and vice versa). An
        # empty / unspecified r.quantity is non-restrictive so the
        # binding pass can still back-fill the field from a unique
        # matched obligation; a specific r.quantity MUST appear in
        # the obligation's quantity set — a question that names
        # exactly one quantity cannot be narrowed to the other.
        #
        # X-axes2 fix ①: a silent obligation (empty
        # ``ob_quantity_set``) is NOT a mismatch. The original
        # question never asked for a quantity, so a candidate
        # adding one is a model over-specification that the post-
        # match reset pass demotes to ``unspecified``. Rejecting
        # the whole match here would drop every honest fact on the
        # leaf. Only an explicit specific obligation canonical
        # that disagrees with the candidate is rejected.
        r_quantity_raw = (r.get("quantity") or "").strip().casefold()
        r_quantity_eff = r_quantity_raw if r_quantity_raw in {"single", "multiple"} else ""
        ob_quantity = (ob.get("quantity") or "").strip().casefold()
        ob_quantity_scope_canonicals = {
            qs.get("canonical")
            for qs in (ob.get("quantity_scope") or [])
            if qs.get("canonical") in {"single", "multiple"}
        }
        ob_quantity_set = (
            {ob_quantity} if ob_quantity in {"single", "multiple"} else set()
        ) | ob_quantity_scope_canonicals
        if r_quantity_eff and ob_quantity_set and r_quantity_eff not in ob_quantity_set:
            return False
        # Platform: the obligation carries the per-action
        # platform_scope derived from the question atoms (one
        # segment per (action, object) leaf). When the requirement
        # names a specific platform it must appear in the SAME
        # leaf's scope; otherwise the two-platform anchor check has
        # no per-action binding and a swapped ``Android`` /
        # ``iPhone`` pair would sail through. An empty
        # ``platform_scope`` means the leaf has no platform
        # constraint, so any r.platform is admissible — including a
        # question-wide ``mobile`` / ``phone`` / ``tablet``
        # candidate.
        r_platform_raw = (r.get("platform") or "").strip()
        ob_platform_scope = ob.get("platform_scope") or []
        if r_platform_raw and r_platform_raw not in {"", "unspecified"} and ob_platform_scope:
            r_platform_canon = input_v7.canonical_platform(r_platform_raw) or r_platform_raw
            ob_platforms_canon = {p["canonical"] for p in ob_platform_scope}
            # iOS family collapse is handled by the compile_spec
            # top-level ``_platform_class`` so the per-obligation
            # platform guard and the multi-relation fill guard share
            # one canonical mapping.
            if _platform_class(r_platform_canon) not in {
                _platform_class(p) for p in ob_platforms_canon
            }:
                # W5 fix ①: per-relation platform mismatch returns
                # False instead of raising SpecError. A sibling
                # obligation whose (action, object, mode) matches
                # but whose per-action platform differs (e.g. a
                # candidate split into Android + iPhone leaves
                # matching their respective obligations) must NOT
                # kill the whole match. The lookalike post-pass
                # below raises the explicit SpecError code ONLY
                # when this candidate has no other legal match in
                # its anchor/owner scope, preserving the
                # ``test_planner_cannot_replace_original_device``
                # contract.
                return False
        # Entry: same per-action binding as platform. ``Shared tab``
        # is an entry to the upload leaf; ``Other tab`` is an entry
        # to the delete leaf. A candidate that swaps the two
        # silently is rejected here so the whole-anchor entry check
        # no longer has to first-pick the wrong slot.
        r_entry_raw = r.get("entry") or ""
        ob_entry_scope = ob.get("entry_scope") or []
        if r_entry_raw and r_entry_raw != "app" and ob_entry_scope:
            r_entry_cf = r_entry_raw.casefold()
            ob_entries_cf = [e["text"].casefold() for e in ob_entry_scope]
            if not any(e_cf in r_entry_cf for e_cf in ob_entries_cf):
                # W5 fix ①: per-relation entry mismatch returns False
                # instead of raising; the lookalike post-pass
                # re-introduces ``entry_replaced`` when no other
                # obligation in scope can legally satisfy this
                # requirement.
                return False
        return True

    def _lookalike_dim(r: dict):
        """W5 fix ① post-pass helper.

        Return ``("platform", ob)`` if ``r`` matches ``ob`` on every
        obligation dimension EXCEPT platform, or ``("entry", ob)`` if
        it matches on every dimension except entry. Returns ``None``
        when ``r`` has no anchor / owner / action / object / mode
        relation to any obligation, or when ``r`` already matched at
        least one obligation (``fulfills_obligation_ids`` non-empty).
        The post-pass in ``compile_spec`` raises the original
        ``SpecError`` code (``platform_inconsistent_with_question`` or
        ``entry_replaced``) only when this returns non-None, so
        sibling-obligation splits — a candidate whose Android leaf
        matches the Android obligation while its iPhone leaf matches
        the iPhone obligation — still go through ``_fulfills`` cleanly
        without each leaf being rejected by the other leaf's per-action
        platform.
        """
        if r["kind"] in {"analysis", "comparison"}:
            return None
        if r.get("fulfills_obligation_ids"):
            return None
        for ob in input_obligations:
            if ob.get("anchor_id") and ob["anchor_id"] not in r.get("anchor_ids", []):
                continue
            ob_owners = ob.get("owners") or []
            if ob_owners:
                _r_owner_canon = {
                    input_v7.canonical_product(p) or p.casefold() for p in r.get("products", [])
                }
                if not (set(ob_owners) & _r_owner_canon):
                    continue
            r_action_raw = input_v7.canonical_action(r.get("action") or "") or ""
            if r.get("action"):
                if not r_action_raw and "+" in (r.get("action") or ""):
                    r_action_raw = (r.get("action") or "").strip().casefold()
            ob_action = ob.get("action_canon") or ""
            if ob_action:
                _r_action_parts = set(input_v7.action_family_parts(r_action_raw))
                if r_action_raw and r_action_raw not in _r_action_parts:
                    _r_action_parts.add(r_action_raw)
                if ob_action not in _r_action_parts:
                    continue
            r_object_raw = input_v7.canonical_object(r.get("object") or "") or ""
            ob_object = ob.get("object_canon") or ""
            if ob_object:
                _r_object_members = set(input_v7.object_compound_members(r_object_raw))
                if r_object_raw and r_object_raw not in _r_object_members:
                    _r_object_members.add(r_object_raw)
                if ob_object not in _r_object_members:
                    continue
            r_mode_lit = (r.get("mode") or "").strip().casefold()
            r_mode_raw = input_v7.canonical_mode(r.get("mode") or "") or ""
            if r_mode_lit in {"", "unspecified", "mixed"}:
                r_mode_eff = ""
            else:
                r_mode_eff = r_mode_raw or r_mode_lit
            ob_mode_scope = [
                m["canonical"]
                for m in (ob.get("mode_scope") or [])
                if m.get("canonical") in {"manual", "automatic"}
            ]
            if ob_mode_scope:
                _unique_mode = set(ob_mode_scope)
                if len(_unique_mode) == 1:
                    _scope_mode = next(iter(_unique_mode))
                    if r_mode_eff and r_mode_eff != _scope_mode:
                        continue
                else:
                    if r_mode_eff not in _unique_mode:
                        continue
            ob_mode = ob.get("mode_canon") or ""
            if ob_mode and r_mode_eff != ob_mode:
                continue
            # All non-platform/non-entry dims matched. Probe platform
            # first (mirrors the original ``_fulfills`` raise order),
            # then entry.
            r_platform_raw = (r.get("platform") or "").strip()
            ob_platform_scope = ob.get("platform_scope") or []
            if r_platform_raw and r_platform_raw not in {"", "unspecified"} and ob_platform_scope:
                r_platform_canon = input_v7.canonical_platform(r_platform_raw) or r_platform_raw
                ob_platforms_canon = {p["canonical"] for p in ob_platform_scope}
                if _platform_class(r_platform_canon) not in {
                    _platform_class(p) for p in ob_platforms_canon
                }:
                    return ("platform", ob)
            r_entry_raw = r.get("entry") or ""
            ob_entry_scope = ob.get("entry_scope") or []
            if r_entry_raw and r_entry_raw != "app" and ob_entry_scope:
                r_entry_cf = r_entry_raw.casefold()
                ob_entries_cf = [e["text"].casefold() for e in ob_entry_scope]
                if not any(e_cf in r_entry_cf for e_cf in ob_entries_cf):
                    return ("entry", ob)
        return None

    fulfilled_ids: set[str] = set()
    for rr in requirements:
        matched: list[str] = []
        for ob in input_obligations:
            if _fulfills(rr, ob):
                matched.append(ob["id"])
        rr["fulfills_obligation_ids"] = matched
        fulfilled_ids.update(matched)

    # X-axes2 fix ①: silent-question quantity normalisation. When
    # the original question carries no quantity marker on a leaf
    # (``upload photos``, ``upload files``) the obligation's
    # ``quantity`` AND ``quantity_scope`` are both empty — that's
    # a silent question. A candidate that declares ``multiple`` /
    # ``single`` for such a leaf added the quantity itself; the
    # ``_fulfills`` quantity check correctly returns True (empty
    # obligation set is non-restrictive on the candidate side) but
    # the user-visible answer surface MUST NOT echo a fabricated
    # quantity that the question never asked for. Previously the
    # whole requirement was rejected, dropping otherwise-legal
    # facts (the multi-relation guard then surfaced an empty
    # coverage gap for every honest fact). Now we keep the legal
    # (action, object, mode, platform, entry) match and demote
    # ``rr.quantity`` / ``rr.mandatory_quantity`` to
    # ``unspecified`` so the analysis-side text only reports what
    # the question actually requested. The reset fires only when
    # EVERY matched obligation has zero specific quantity
    # canonicals — partial coverage (some ob says specific, others
    # silent) still flows through the binding block below, which
    # only fills on a unique specific canonical. Original-question
    # specific-vs-candidate-specific mismatches still return False
    # from ``_fulfills`` and are not affected.
    for rr in requirements:
        if rr["kind"] in {"analysis", "comparison"}:
            continue
        r_q_raw = (rr.get("quantity") or "").strip().casefold()
        if r_q_raw not in {"single", "multiple"}:
            continue
        matched_obs = [
            ob for ob in input_obligations if ob["id"] in (rr.get("fulfills_obligation_ids") or [])
        ]
        if not matched_obs:
            continue
        _ob_q_set: set[str] = set()
        for ob in matched_obs:
            ob_q = (ob.get("quantity") or "").strip().casefold()
            if ob_q in {"single", "multiple"}:
                _ob_q_set.add(ob_q)
            for qs in ob.get("quantity_scope", []) or []:
                qsc = qs.get("canonical")
                if qsc in {"single", "multiple"}:
                    _ob_q_set.add(qsc)
        if not _ob_q_set:
            rr["quantity"] = "unspecified"
            rr["mandatory_quantity"] = "unspecified"

    # Per-obligation fill: AFTER the anchor / owner / action /
    # object / mode matching has produced a set of matched
    # obligations, back-fill any empty ``platform`` / ``entry`` /
    # ``mode`` / ``quantity`` field on the candidate requirement
    # from the matched obligation's scope. The fill is unique-by-
    # canonical across the matched obligations' per-action lists:
    # a single unique canonical fills the requirement, multiple
    # distinct canonicals (or an empty scope) leave the field empty
    # so the unresolved-gap code below can surface the missing
    # dimension honestly. The whole-anchor first-pick that used
    # to fill ``entry`` is gone — two leaves in one anchor now get
    # their own entry each, not the first leaf's slot both ways.
    for rr in requirements:
        if rr["kind"] in {"analysis", "comparison"}:
            continue
        _r_obs = [
            ob for ob in input_obligations if ob["id"] in rr.get("fulfills_obligation_ids", [])
        ]
        if not _r_obs:
            continue
        # Platform fill.
        if rr["platform"] in (None, "", "unspecified"):
            _canonicals: list[str] = []
            for ob in _r_obs:
                for p in ob.get("platform_scope", []) or []:
                    pc = p.get("canonical")
                    if pc and pc != "unspecified":
                        _canonicals.append(pc)
            _unique = set(_canonicals)
            if len(_unique) == 1:
                rr["platform"] = next(iter(_unique))
        # Entry fill: r.entry "" / "app" / None all mean "no
        # declared entry"; fill from the matched obligation's
        # per-action entry scope (the per-action binding has
        # already resolved which tab belongs to which leaf).
        if not rr["entry"] or rr["entry"] == "app":
            _entries: list[str] = []
            for ob in _r_obs:
                for e in ob.get("entry_scope", []) or []:
                    _entries.append(e["text"])
            _unique = set(_entries)
            if len(_unique) == 1:
                rr["entry"] = next(iter(_unique))
        # Mode fill: r.mode "" / "unspecified" / None all mean
        # "no declared mode"; fill from matched obligation's
        # mode_scope (which carries both question-source atoms
        # AND scope-source atoms appended above) when exactly one
        # canonical value exists.
        if rr["mode"] in (None, "", "unspecified"):
            _modes: list[str] = []
            for ob in _r_obs:
                for m in ob.get("mode_scope", []) or []:
                    mc = m.get("canonical")
                    if mc in {"manual", "automatic"}:
                        _modes.append(mc)
            _unique = set(_modes)
            if len(_unique) == 1:
                rr["mode"] = next(iter(_unique))

    # Multi-relation fill guard: a single candidate matching
    # multiple obligations with DISTINCT specific platforms /
    # entries / modes / quantities — OR a single obligation whose
    # own per-action scope carries multiple distinct canonicals —
    # cannot express every relation's value in one field. The
    # previous iteration's per-obligation fill correctly refused
    # to set r.platform / r.entry / r.mode / r.quantity, but the
    # obligation ledger still listed the match in
    # ``fulfills_obligation_ids``, leaving the spec claiming
    # completeness on an empty field. Drop those conflicting
    # obligation matches here so the unresolved-gap path below
    # surfaces them as honest gaps instead of silently fabricating
    # coverage. Unique canonicals (single value across both Q and
    # scope) keep the matches — only distinct, irreconcilable
    # values force a drop.
    for rr in requirements:
        if rr["kind"] in {"analysis", "comparison"}:
            continue
        matched_ids = rr.get("fulfills_obligation_ids") or []
        if not matched_ids:
            continue
        matched_obs = [ob for ob in input_obligations if ob["id"] in matched_ids]
        _conflict_dims: list[str] = []
        # Platform: collect per-relation class canonicals across
        # ALL matched obligations' platform_scopes. The conflict
        # test fires whenever the union has >1 class AND rr never
        # committed to a specific platform — a candidate must
        # either pick one or leave the gaps unresolved.
        if rr["platform"] in (None, "", "unspecified"):
            _pcanon_classes: set[str] = set()
            for ob in matched_obs:
                for p in ob.get("platform_scope") or []:
                    pc = p.get("canonical")
                    if pc and pc != "unspecified":
                        _pcanon_classes.add(_platform_class(pc))
            if len(_pcanon_classes) > 1:
                _conflict_dims.append("platform")
        # Entry.
        if not rr["entry"] or rr["entry"] == "app":
            _ecanons: set[str] = set()
            for ob in matched_obs:
                for e in ob.get("entry_scope") or []:
                    _ecanons.add(e["text"])
            if len(_ecanons) > 1:
                _conflict_dims.append("entry")
        # Mode.
        if rr["mode"] in (None, "", "unspecified"):
            _mcanons: set[str] = set()
            for ob in matched_obs:
                if ob.get("mode_canon") in {"manual", "automatic"}:
                    _mcanons.add(ob["mode_canon"])
                for m in ob.get("mode_scope") or []:
                    mc = m.get("canonical")
                    if mc in {"manual", "automatic"}:
                        _mcanons.add(mc)
            if len(_mcanons) > 1:
                _conflict_dims.append("mode")
        # Quantity.
        if rr["quantity"] in (None, "", "unspecified"):
            _qcanons: set[str] = set()
            for ob in matched_obs:
                qv = ob.get("quantity")
                if qv in {"single", "multiple"}:
                    _qcanons.add(qv)
                for qs in ob.get("quantity_scope") or []:
                    qsc = qs.get("canonical")
                    if qsc in {"single", "multiple"}:
                        _qcanons.add(qsc)
            if len(_qcanons) > 1:
                _conflict_dims.append("quantity")
        if _conflict_dims:
            # Drop the conflicting obligation matches — let the
            # obligation-failure path below surface them as
            # unresolved gaps with the ``obligation_unfulfilled``
            # reason. Mark the conflict reason on the requirement
            # so downstream diagnostics surface which dimensions
            # blocked the fulfillment.
            rr["fulfills_obligation_ids"] = []
            rr["scope_conflict_dims"] = sorted(_conflict_dims)

    # W5 fix ②: recompute the fulfilled_ids union AFTER the
    # multi-relation fill guard has had a chance to clear
    # ``rr["fulfills_obligation_ids"]``. The original union was
    # built before the guard ran, so a wide-null candidate whose
    # only matches had conflicting scope dimensions would still
    # appear in ``fulfilled_ids`` — the obligation_unfulfilled
    # loop below would then trust the stale union and skip an
    # honest ``obligation_unfulfilled`` entry, even though the
    # candidate no longer fulfills anything. Rebuilding the union
    # here keeps the ``obligation_unfulfilled`` denominator honest.
    fulfilled_ids = set()
    for rr in requirements:
        fulfilled_ids.update(rr.get("fulfills_obligation_ids") or [])

    # W5 fix ① post-pass: detect candidates whose ``_fulfills`` ran
    # to completion (anchor / owner / action / object / mode all
    # aligned) but were rejected on platform or entry alone. After
    # the relaxation in ``_fulfills`` these candidates leave
    # ``fulfills_obligation_ids`` empty, so a silent
    # ``obligation_unfulfilled`` would otherwise hide the explicit
    # device / entry replacement. The post-pass re-introduces the
    # fixed SpecError code only when the candidate has NO other
    # legal match against ANY obligation in its anchor / owner
    # scope, preserving ``test_planner_cannot_replace_original_
    # device``'s contract while letting sibling-obligation splits
    # (Android leaf + iPhone leaf for the same (action, object))
    # pass cleanly.
    for rr in requirements:
        if rr["kind"] in {"analysis", "comparison"}:
            continue
        if rr.get("fulfills_obligation_ids"):
            continue
        _lookalike = _lookalike_dim(rr)
        if _lookalike is None:
            continue
        _dim, _ob = _lookalike
        if _dim == "platform":
            raise SpecError("platform_inconsistent_with_question")
        if _dim == "entry":
            raise SpecError("entry_replaced")

    # Quantity binding belongs to each matched (action, object)
    # obligation's segment, NOT the anchor-wide question text.
    # ``upload one file AND delete multiple photos`` must NOT
    # broadcast the delete leaf's ``multiple`` onto the upload
    # leaf; every obligation now carries its own segment-derived
    # quantity (``one file`` vs ``multiple photos``). Kinds without
    # an (action, object) leaf (version / destination / limit /
    # condition / analysis / comparison) keep the anchor-wide
    # fallback computed earlier so a quantity-bearing leaf can
    # still anchor on those kinds when no obligation matches.
    # Scope-source quantity atoms (``Multiple photos in one
    # upload``) ride on the same obligation's ``quantity_scope``
    # list and contribute here so a silent question can still pin
    # the leaf's quantity through the scope supplement.
    for rr in requirements:
        if rr["kind"] in {"analysis", "comparison", "version", "destination", "limit", "condition"}:
            continue
        _r_obs = [
            ob for ob in input_obligations if ob["id"] in rr.get("fulfills_obligation_ids", [])
        ]
        specific_quantities: set[str] = set()
        for ob in _r_obs:
            q = ob.get("quantity")
            if q in {"single", "multiple"}:
                specific_quantities.add(q)
            for qs in ob.get("quantity_scope", []) or []:
                qsc = qs.get("canonical")
                if qsc in {"single", "multiple"}:
                    specific_quantities.add(qsc)
        if len(specific_quantities) == 1:
            rr["quantity"] = next(iter(specific_quantities))
            rr["mandatory_quantity"] = rr["quantity"]

    for ob in input_obligations:
        if ob["id"] not in fulfilled_ids:
            unresolved.append(
                dict(
                    id=f"obligation-{ob['id']}",
                    text=ob["text"],
                    reason="obligation_unfulfilled",
                )
            )

    # Multi-product coverage: each product mentioned in the question must
    # appear in at least one requirement's product list, otherwise the
    # missing branch surfaces as an explicit unresolved gap. This is a
    # GLOBAL check across all requirements so a multi-product comparison
    # can split responsibility between two requirements without falsely
    # marking each other's product as missing.
    if len(question_product_canonicals) > 1:
        all_r_products = set()
        for rr in requirements:
            for p in rr.get("products", []):
                all_r_products.add(input_v7.canonical_product(p) or p.casefold())
        for m in sorted(question_product_canonicals - all_r_products):
            unresolved.append(
                dict(
                    id=f"product-{m}",
                    text=m,
                    reason="product_omitted",
                )
            )
    offered_facets = {
        f for r in requirements if r["kind"] == "analysis" for f in r["analysis_facets"]
    }
    if requested_facets - offered_facets:
        raise SpecError("analysis_facets_omitted")
    unresolved.extend(
        a | {"reason": "anchor_omitted"} for a in anchors.values() if a["id"] not in covered
    )
    # Local relationship rejection: a candidate requirement of an
    # action/object-anchored kind (capability / procedure) whose
    # anchor + owner scope carries at least one input_obligation,
    # but r itself fulfills NONE of those obligations, is a
    # fabrication — the user did not request that (action, object,
    # mode) triple in this anchor or branch. Reject it locally:
    # record a diagnostic ``rejected_requirements`` entry, drop
    # the requirement from the visible list, and remove its
    # dependent queries. The original ``input_obligations`` ledger
    # stays untouched; legitimate obligation-free kinds (version /
    # destination / limit / condition / analysis / comparison)
    # keep their existing obligation-free path. Truly unknown
    # query ids stay strict (no fabrication).
    REJECTABLE_KINDS = {"capability", "procedure"}
    rejected_requirements: list[dict] = []
    for rr in requirements:
        if rr["kind"] not in REJECTABLE_KINDS:
            continue
        if rr.get("fulfills_obligation_ids"):
            continue
        r_anchor_ids = set(rr.get("anchor_ids", []))
        r_owner_set = {
            input_v7.canonical_product(p) or p.casefold() for p in rr.get("products", [])
        }
        scope_obs = [
            ob
            for ob in input_obligations
            if (not ob.get("anchor_id")) or ob["anchor_id"] in r_anchor_ids
        ]
        if r_owner_set:
            scope_obs = [
                ob
                for ob in scope_obs
                if (not ob.get("owners")) or (set(ob["owners"]) & r_owner_set)
            ]
        if scope_obs:
            rejected_requirements.append(
                dict(
                    id=rr["id"],
                    reason="fulfills_no_input_obligation",
                    anchor_ids=sorted(r_anchor_ids),
                )
            )
    rejected_ids = {x["id"] for x in rejected_requirements}
    visible_requirements = [r for r in requirements if r["id"] not in rejected_ids]
    visible_queries = [q for q in candidate["queries"] if q["id"] not in rejected_ids]
    body = dict(
        version="research-spec-v7",
        original_question=question,
        original_scope=scope,
        policy_spans=parts["policy_spans"],
        anchors=parts["anchors"],
        requirements=visible_requirements,
        rejected_requirements=rejected_requirements,
        unresolved=unresolved,
        queries=copy.deepcopy(visible_queries),
        normalizations=normalizations,
        input_atoms=input_atoms,
        input_obligations=input_obligations,
    )
    body["fingerprint"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return body


class ResearchBudget:
    LIMITS = {"llm_calls": 3, "logical_searches": 3, "official_reads": 5}

    def __init__(self):
        self._counts = dict.fromkeys(self.LIMITS, 0)
        self._offered = set()
        self._selected = set()

    def reserve(self, resource):
        if not isinstance(resource, str) or resource not in self.LIMITS:
            raise SpecError("budget_resource_unknown")
        if self._counts[resource] >= self.LIMITS[resource]:
            raise SpecError("budget_exhausted:" + resource)
        self._counts[resource] += 1

    @staticmethod
    def _ids(ids):
        if not isinstance(ids, list) or any(
            not isinstance(i, str) or not i or len(i) > 200 for i in ids
        ):
            raise SpecError("budget_ids_invalid")
        return set(ids)

    def offer(self, ids):
        combined = self._offered | self._ids(ids)
        if len(combined) > 100:
            raise SpecError("budget_offer_exhausted")
        self._offered = combined

    def select(self, ids):
        combined = self._selected | self._ids(ids)
        if len(combined) > 40:
            raise SpecError("budget_selection_exhausted")
        self._selected = combined

    def to_dict(self):
        return self._counts | dict(
            offered_unique_evidence_units=len(self._offered),
            selected_unique_evidence_units=len(self._selected),
        )
