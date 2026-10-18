"""Exact truth and requirement support are independent, bounded contracts.

No model verdict, arbitrary fact paraphrase or document-wide topic match can
certify support. Applicability inherits only an ancestor or the original line;
procedure/quantity proof is scoped to one flow. Open semantic quality still
requires the independent source review recorded by the caller.
"""

from __future__ import annotations

import copy
import re
from collections import defaultdict
from dataclasses import replace
from uuid import uuid4

from app.services import research_input_v7 as input_v7
from app.services.research_claims import (
    Claim,
    ClaimError,
    _build_haystack,
    _evidence_location,
    _product_key_for_source,
    _url_boundary_check,
)
from app.services.research_contract import _eval_procedure
from app.services.research_coverage import _condition_proof, normalize
from app.services.research_spec_v7 import SpecError


def exact_facts(ids, entries, *, run_id, project_id, api, full_source_ids):
    """Re-read exact saved spans, with no question-dependent truth demotion."""
    if not isinstance(ids, (list, tuple)) or len(ids) > 40:
        raise ClaimError("evidence_ids_invalid")
    if any(not isinstance(i, str) or i not in entries for i in ids):
        raise ClaimError("unknown_evidence_id")
    if len(set(ids)) != len(ids):
        raise ClaimError("evidence_ids_duplicate")
    out = []
    for eid in ids:
        u, s = entries[eid]["unit"], entries[eid]["source"]
        if getattr(s, "run_id", None) != run_id or u.source_id != s.provider_source_id:
            raise ClaimError("source_run_mismatch")
        if getattr(s, "project_id", project_id) != project_id:
            raise ClaimError("source_project_mismatch")
        body = s.snippet or ""
        if (
            type(u.offset) is not int
            or type(u.length) is not int
            or u.offset < 0
            or not 0 < u.length <= 200
            or u.length != len(u.text)
            or u.offset + u.length > len(body)
            or body[u.offset : u.offset + u.length] != u.text
        ):
            raise ClaimError("snapshot_drift")
        pid, url = s.provider_source_id, s.url
        failed = _url_boundary_check(url)
        if not failed and (not isinstance(pid, str) or not 0 < len(pid) <= 200):
            failed = "provider_id_invalid"
        # v7 has an explicit official-only source policy, using the unchanged registry.
        if not failed and not any(
            api._is_official_vendor_host(url, r) and api._is_official_vendor_path(url, r)
            for r in api._OFFICIAL_VENDOR_RULES.values()
        ):
            failed = "url_not_official"
        title = s.title or s.domain or ""
        if not failed and not api._is_name_supported_by_sources(
            title, ((pid, url),), {pid: _build_haystack(s)}
        ):
            failed = "name_not_supported"
        if not failed and pid not in full_source_ids:
            failed = "official_snapshot_unavailable"
        location = _evidence_location(source=s, unit=u, first_failed_guard=failed)
        location["version"] = "research-fact-v7"
        out.append(
            Claim(
                id=uuid4(),
                run_id=run_id,
                project_id=project_id,
                category="competitor",
                title=title[:200],
                description=u.text,
                status="not_verified" if failed else "confirmed",
                source_refs=() if failed else ((pid, url),),
                claim_kind="fact",
                supporting_finding_ids=(),
                evidence_location=location,
                evidence_id=eid,
                product_key=_product_key_for_source(s, api),
            )
        )
    return tuple(out)


# This is a finite support predicate vocabulary, independent of products/questions.
# It describes operations/platforms rather than containing product-specific answers.
_OPERATIONS = re.compile(
    r"\b(?:upload|download|backup|add|sync|move|delete|create|animate|animation)\b", re.I
)
_PLATFORMS = {
    "ios": r"\b(?:ios|iphone|ipados|ipad)\b",
    "android": r"\bandroid\b",
    "mobile": r"\b(?:mobile|phone|tablet|smartphone|ios|iphone|ipad|ipados|android)\b",
    "desktop": r"\b(?:windows|macos|linux|computer|desktop)\b",
    "web": r"\b(?:web|browser|online)\b",
}


def _tokens(text):
    return set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", normalize(text)))


def _line(u, s):
    from app.services.research_catalog_v7 import branch_span

    body = s.snippet or ""
    a, b = branch_span(body, u.offset)
    return body[a:b]


def _platform_class(value):
    value = (value or "").lower()
    if value in {"iphone", "ipad", "ios", "ipados"}:
        return "ios"
    if value in {"phone", "tablet", "mobile"}:
        return "mobile"
    if value in {"windows", "macos", "linux", "desktop"}:
        return "desktop"
    return value


def operation_family(action):
    """Finite action relation, retaining each source's verb in every quotation.

    Upload/add/back up are remote-ingest operations in this source policy;
    sync, move, delete and selection remain separate. Mode, device, object,
    entry, full flow, quantity and conditions are still independently required.
    This never equates local moves or automatic sync with manual upload.
    """
    word = normalize(action or "")
    transfer = re.fullmatch(
        r"(?:(?:manual|manually) )?(?:(?:select(?: and| then)?|start) )?(?:upload|add|backup)", word
    )
    return "ingest" if transfer else word


def _operation_families(text):
    # Reuse the input-obligation ledger's inflection-aware regex so a
    # unit text like ``OneDrive automatically uploads photos on
    # iPhone.`` recognises ``uploads`` -> ``upload`` (same verb family),
    # not just the bare ``upload`` form. Without this the support
    # layer rejects real capabilities whose source only quotes the
    # inflected surface, while the spec / input layers happily accept
    # the same form. Negative tests still distinguish upload/delete
    # because ``delete`` only matches when the verb literally appears.
    return {operation_family(canon) for _, _, _, canon in input_v7.action_atoms_in_text(text or "")}


_NEGATION = re.compile(r"\b(?:cannot|can.t|not support|does not support)\b|不能|不支持", re.I)


def _negative_scope(r, text):
    """Separate a target denial from a syntactically restricted object subset.

    This recognises relative/participial object modifiers, not product answers.
    Unrecognised denials stay negative. A recognised subset never establishes
    the general capability by itself; the caller must have independent positive
    support and retains the exact limitation among its delivered quotations.
    """
    states = set()
    target = input_v7.canonical_object(r.get("object"))
    question = normalize(r.get("question_text", ""))
    for sentence in re.split(r"(?<=[.!?。！？])\s+|\n", text):
        neg = _NEGATION.search(sentence)
        if not neg:
            continue
        objects = [
            a
            for a in input_v7.extract_input_atoms(sentence)
            if a["dimension"] == "object" and a["canonical"] == target and a["start"] >= neg.end()
        ]
        modifier = None
        if objects:
            tail = sentence[objects[0]["end"] :].strip().rstrip(".!?。！？")
            if re.match(
                r"^(?:(?:that|which|with|without|when|while|if|unless)\b|"
                r"\w+(?:ed|ing)\s+(?:for|in|on|by|with)\b)",
                tail,
                re.I,
            ):
                modifier = normalize(tail)
        states.add("restriction" if modifier and modifier not in question else "negative")
    return states


def applicability(r, u, s, peers=(), *, entries=None):
    """Return a closed rejection code. Never borrow a sibling's device/action."""
    local = _line(u, s)
    path = " / ".join(u.section_path)
    platform = _platform_class(r["platform"])
    # Inspect each local sentence/line before ancestors so a Windows note cannot
    # borrow the enclosing mobile article's heading. Parallel OS version lists
    # may carry several classes and are retained when the requested class exists.
    if platform in _PLATFORMS:
        if platform in {"ios", "android", "mobile"} and re.search(
            r"\b(?:on|from|using)\s+(?:a |your |the )?(?:computer|desktop|windows|macos|mac)\b",
            local,
            re.I,
        ):
            return "platform_direction_conflict"
        scoped = local
        if not any(re.search(pat, local, re.I) for pat in _PLATFORMS.values()):
            scoped = next(
                (
                    h
                    for h in reversed(u.section_path)
                    if any(re.search(pat, h, re.I) for pat in _PLATFORMS.values())
                ),
                local,
            )
        declared = (r.get("platform") or "").casefold()
        requested_devices = {
            d
            for d in ("iphone", "ipad")
            if re.search(r"\b" + d + r"\b", r.get("question_text", ""), re.I)
        }
        target_device = (
            declared
            if declared in {"iphone", "ipad"}
            else next(iter(requested_devices))
            if len(requested_devices) == 1
            else None
        )
        source_devices = {
            d for d in ("iphone", "ipad") if re.search(r"\b" + d + r"\b", scoped, re.I)
        }
        if target_device and source_devices and target_device not in source_devices:
            return "device_conflict"
        if (
            target_device == "iphone"
            and re.search(r"\bipados\b", scoped, re.I)
            and not re.search(r"\bios\b", scoped, re.I)
        ):
            return "device_conflict"
        detected = {p for p, pat in _PLATFORMS.items() if re.search(pat, scoped, re.I)}
        if detected and platform not in detected:
            if not (
                platform in {"ios", "android"}
                and "mobile" in detected
                and not ({"ios", "android", "desktop"} & detected)
            ):
                return "platform_conflict"
        inherited = " ".join(
            p.text for p in peers if p.source_id == u.source_id and p.section_path == u.section_path
        )
        # An explicit page-edition URL is a scope annotation; it cannot
        # override a conflicting local clause/heading checked above.
        from urllib.parse import parse_qs, urlsplit

        edition = " ".join(parse_qs(urlsplit(s.url).query).get("co", []))
        if not re.search(
            _PLATFORMS[platform], local + " " + path + " " + inherited + " " + edition, re.I
        ):
            return "platform_unproven"
    target = _operation_families(r.get("action") or "")
    # flow_member: nearest operation-bearing heading verifies branch ownership.
    # Numbered steps (step_index > 0) under a matching heading are genuine
    # flow members: a "Create an account" prerequisite inside an Upload
    # numbered list belongs to that flow. Real Move/Delete subsections
    # still fail the heading branch check below.
    flow_owner = None
    for _heading in reversed(u.section_path):
        _ops = _operation_families(_heading)
        if _ops:
            flow_owner = _ops
            break
    if flow_owner and target and target & flow_owner:
        # Same-flow: numbered members accept arbitrary action verbs as
        # legitimate prerequisites; non-numbered sentences still need
        # local-op match (with optional nav-marker truncation).
        if u.step_index:
            pass
        else:
            local_for_ops = _primary_clause(local)
            local_ops = _operation_families(local_for_ops)
            if target and local_ops and not local_ops & target:
                return "operation_local_conflict"
    else:
        local_for_ops = local
        local_ops = _operation_families(local_for_ops)
        if target and local_ops and not local_ops & target:
            return "operation_local_conflict"
    # The nearest operation-bearing heading owns its descendants. In particular,
    # Move/Create subsections do not inherit Upload from the page title.
    for heading in reversed(u.section_path):
        ops = _operation_families(heading)
        if ops:
            if target and not target & ops:
                return "operation_branch_conflict"
            # For automatic-mode requirements, an explicit "Manually X" heading
            # cannot donate its body to the automatic flow even when the verbs
            # intersect; the "Manually" branch owns a different condition.
            if (
                r.get("mode") == "automatic"
                and re.search(r"\bmanually?\b", heading, re.I)
                and not re.search(r"\bautomatic(?:ally)?\b", heading, re.I)
            ):
                return "mode_conflict"
            break
    if r["kind"] in {"procedure", "capability"} and target:
        # Instruction headings name their operation before its object. A
        # "Check backup status" section cannot borrow the noun backup as its verb.
        for heading in reversed(u.section_path):
            h = normalize(heading)
            if not _OPERATIONS.search(h):
                continue
            root = re.sub(r"^(?:how to |manually |automatically )+", "", h).split()[0]
            if root not in {"on", "in", "for", "from", "use", "using"} and operation_family(
                root
            ) != operation_family(r["action"]):
                return "operation_heading_conflict"
            break
    if r.get("entry") not in {None, "app"} and not _tokens(r["entry"]) <= _tokens(
        local + " " + path + " " + s.url
    ):
        # The app/entry can be introduced by step 1. Only an exact peer in
        # this structural list may supply that context; a sibling list or
        # another snapshot cannot. Evaluation supplies only bound peers,
        # so an unselected entry cannot silently count as delivery proof.
        flow_id = (entries or {}).get(u.evidence_id, {}).get("flow_id")
        entry_proven = bool(flow_id) and any(
            p.source_id == u.source_id
            and (entries or {}).get(p.evidence_id, {}).get("flow_id") == flow_id
            and _tokens(r["entry"]) <= _tokens(p.text + " " + path + " " + s.url)
            for p in peers
        )
        if not entry_proven:
            return "entry_conflict"
    requested_mode = r["mode"]
    if requested_mode == "manual" and re.search(
        r"\b(?:automatically|overnight)\b", local + " " + u.section, re.I
    ):
        return "mode_conflict"
    # Product identity comes from the exact source plus registered official URL,
    # not from a different fact supplied by the model.
    source_text = normalize(" ".join([s.url, s.title or "", s.domain or ""]))
    if not any(
        _tokens(p) <= _tokens(source_text.replace("/", " ").replace("-", " "))
        or normalize(p).replace(" ", "") in source_text.replace(" ", "")
        for p in r["products"]
    ):
        return "product_conflict"
    return None


def _flows(units, entries=None):
    """Y-flow: when ``entries`` carries per-list ``flow_id`` metadata
    (set by :func:`build_catalogs`), group by ``(source_id, flow_id)``
    so two parallel numbered lists under the same heading never
    collapse into one bucket. Falls back to the
    ``(source_id, section_path)`` grouping for direct callers /
    historical tests that don't carry flow metadata — that path is
    unchanged.
    """
    groups = defaultdict(list)
    for u in units:
        if entries is not None:
            e = entries.get(u.evidence_id) or {}
            fid = e.get("flow_id")
            if fid:
                groups[(u.source_id, fid)].append(u)
                continue
        groups[(u.source_id, tuple(u.section_path))].append(u)
    return groups


# flow_member: a numbered step confirmed under an operation-bearing heading
# may carry navigation / temporal-condition verbs (e.g. "until you move them").
# Only the PRIMARY action clause (before any such marker) counts toward the
# local operation family. The Move/Delete subsection stays a separate heading
# and is rejected by the branch check; this never equates move with upload.
_NAV_MARKER = re.compile(r"\b(?:until|before|after|while|when|once|as soon as|having|upon)\b", re.I)


def _primary_clause(text):
    m = _NAV_MARKER.search(text)
    return text[: m.start()] if m else text


def _per_product_evidence(entries, kept, product):
    """Filter evidence whose source identifies the given product.

    A source is considered to identify a product when the product tokens are
    all present in the normalized source URL/title/domain, or when the
    normalized product (no spaces) is a substring of the same haystack.
    This mirrors the source identity check used by applicability.
    """
    out = []
    for eid in kept:
        src = entries[eid]["source"]
        haystack = normalize(" ".join([src.url or "", src.title or "", src.domain or ""]))
        tokens = _tokens(haystack.replace("/", " ").replace("-", " "))
        flat = haystack.replace(" ", "")
        if _tokens(product) <= tokens or normalize(product).replace(" ", "") in flat:
            out.append(eid)
    return out


def _action_present(r, units):
    target = _tokens(r.get("action") or "")
    words = _tokens(" ".join(u.text for u in units))
    if operation_family(r.get("action")) == "ingest":
        return bool(_operation_families(" ".join(u.text for u in units)) & {"ingest"})
    return bool(target) and target <= words


def _procedure(units, catalogs, entries):
    if not units:
        return False
    from app.services.research_catalog_v7 import line_span

    # Y-flow: every unit must carry the same flow_id (one and only one
    # numbered list). Splicing Method A's step 1 with Method B's step 2
    # under the same heading is rejected here; if any unit lacks flow
    # metadata the section-path fallback below treats the selection as a
    # legacy section-level flow (unchanged for direct/historical callers).
    flow_groups = _flows(units, entries)
    all_have_flow_id = entries is not None and all(
        (entries.get(u.evidence_id) or {}).get("flow_id") for u in units
    )
    if all_have_flow_id:
        if len(flow_groups) != 1:
            return False
        fid, group = next(iter(flow_groups.items()))
        first_entry = entries[group[0].evidence_id]
        flow_step_total = first_entry.get("flow_step_total") or 0
        # Y-flow: every step ``1..flow_step_total`` MUST be present in
        # the selection. Method A's ``1, 2`` has ``flow_step_total=2``
        # (not the section's ``max=3`` inflated by Method B); Method B's
        # ``1, 2, 3`` independently needs all three. A mixed
        # ``{Method A 1, Method B 2}`` selection fails the 1..2 set
        # check for either list's total.
        numbered = [u for u in group if u.step_index]
        seen_indices = {u.step_index for u in numbered}
        if flow_step_total <= 0 or seen_indices != set(range(1, flow_step_total + 1)):
            return False
    else:
        if len(flow_groups) != 1:
            return False
        group = next(iter(flow_groups.values()))

    source = entries[group[0].evidence_id]["source"]
    body = source.snippet or ""
    # Each selected numbered line must carry every non-space source character.
    # Fragments sharing an index are legitimate only on the SAME original line.
    steps = defaultdict(list)
    for u in group:
        if u.step_index:
            steps[u.step_index].append(u)
    for step_group in steps.values():
        ranges = {
            (
                entries[u.evidence_id].get("statement_offset", line_span(body, u.offset)[0]),
                entries[u.evidence_id].get("statement_offset", line_span(body, u.offset)[0])
                + entries[u.evidence_id].get(
                    "statement_length", line_span(body, u.offset)[1] - line_span(body, u.offset)[0]
                ),
            )
            for u in step_group
        }
        if len(ranges) != 1:
            return False
        a, b = next(iter(ranges))
        if any(
            not body[pos].isspace()
            and not any(u.offset <= pos < u.offset + u.length for u in step_group)
            for pos in range(a, b)
        ):
            return False
    unique: list = [next(iter(step_group)) for step_group in steps.values()]
    if all_have_flow_id:
        # Replace step_total on each unit with the per-list total so
        # ``_eval_procedure`` checks ``1..flow_step_total`` instead of
        # the section's inflated max.
        unique = [replace(u, step_total=flow_step_total) for u in unique]
    unique += [u for u in group if not u.step_index]
    status, *_ = _eval_procedure(catalogs.get(group[0].source_id), tuple(unique))
    return status == "covered"


def _quantity(r, units, entries=None):
    if r["mandatory_quantity"] != "multiple":
        return True
    # Nearest same-branch operation relation. The execution verb may sit
    # BEFORE the quantifier+object ("You can manually upload multiple photos
    # on iPhone.") or AFTER it ("Select multiple photos, then tap Upload.").
    # Within one clause/branch, the operational verb closest to the
    # quantifier+object is the one that proves the count for the requested
    # action. "Select multiple photos to delete, or select one photo then
    # tap Upload." -> the clause holding "multiple photos" has nearest verb
    # "delete", so a delete count does NOT lend itself to upload.
    obj = normalize(r.get("object") or "")
    if not obj:
        return False
    _raw_action = re.sub(r"\bback\s+up\b", "backup", (r.get("action") or "").casefold(), flags=re.I)
    _m_act = re.search(
        r"\b(upload|add|backup|sync|move|delete|create|animate|ingest)\b", _raw_action
    )
    _primary = _m_act.group() if _m_act else ""
    if not _primary:
        return False
    _primary_family = operation_family(_primary)
    if not _primary_family:
        return False
    _quant_re = re.compile(
        r"\b(?:multiple|several|more than one|multi[- ]select|batch)\s+"
        r"(?:device\s+|existing\s+|some\s+)?" + re.escape(obj) + r"\b",
        re.I,
    )
    _OPS = re.compile(r"\b(upload|add|backup|sync|move|delete|create|animate|ingest)\b", re.I)
    # Split by terminal punctuation OR an action-branch "or" (an "or" that
    # introduces another verb/sentence). Noun alternatives ("photos or
    # videos") stay in the same clause: the verb after "or" must be IMMEDIATE,
    # with optional clear subject pronouns (you/i/we/they/a/an/the/to) before
    # it. A look-ahead-based pattern mis-splits "photos or videos, then tap
    # Upload" because "tap Upload" exists a few tokens later.
    _SPLIT = re.compile(
        r"[;。！!？?]|\bor\s+(?:you|i|we|they|he|she|it|a|an|the|to|"
        r"select|choose|pick|tap|click|press|touch|upload|add|backup|sync|move|delete|create|ingest|animate|open|start|begin)\b",
        re.I,
    )
    for group in _flows(units, entries).values():
        text = re.sub(
            r"\bback\s+up\b", "backup", normalize(" ".join(u.text for u in group)), flags=re.I
        )
        for clause in _SPLIT.split(text):
            qm = _quant_re.search(clause)
            if not qm:
                continue
            qstart, qend = qm.start(), qm.end()
            nearest = None
            nearest_dist = None
            for vm in _OPS.finditer(clause):
                vstart, vend = vm.start(), vm.end()
                if vend <= qstart:
                    dist = qstart - vend
                elif vstart >= qend:
                    dist = vstart - qend
                else:
                    dist = 0
                if nearest_dist is None or dist < nearest_dist:
                    nearest = vm.group()
                    nearest_dist = dist
            if nearest and operation_family(nearest) == _primary_family:
                return True
    return False


def _object_present(r, units):
    # Singular/plural inflection is grammatical, not fuzzy text matching.
    def nouns(text):
        return {
            t[:-1] if t.endswith("s") and len(t) > 3 else t for t in _tokens(text) - {"or", "and"}
        }

    obj = nouns(r.get("object") or "")
    return bool(obj) and obj <= nouns(" ".join(u.text + " " + u.section for u in units))


def _condition_state(text):
    """Finite state/negation algebra, separate from the feature's name."""
    raw = text.casefold().replace("’", "'")
    if re.search(r"\bnot\s+(?:currently\s+)?disabled\b|\bnot\s+off\b", raw):
        state = "on"
    elif re.search(
        r"\bdisabled\b|\boff\b|\bnot enabled\b|don.t have.{0,60}enabled|禁用|关闭|未启用|未开启",
        raw,
    ):
        state = "off"
    elif re.search(r"\benabled\b|\bon\b|已启用|已开启", raw):
        state = "on"
    else:
        state = None
    if re.search(r"\bnot offline\b", raw):
        return "online"
    if re.search(r"\boffline\b|离线", raw):
        return "offline"
    if state and re.search(r"\bunless\b", raw):
        state = "off" if state == "on" else "on"
    return state


def _conditions(r, units):
    for clause in r.get("condition_quotes", []):
        expected = _condition_state(clause)
        tokens = _tokens(re.sub(r"^(?:while|when|if)\s+", "", clause, flags=re.I))
        # A condition must be proved by a condition-bearing statement. Ordinary
        # steps from the same page cannot cancel an explicit opposite state.
        compatible = [u for u in units if expected is None or _condition_state(u.text) == expected]
        text = normalize(" ".join(u.text for u in compatible))
        state_facts = [
            type(
                "StateFact",
                (),
                {"status": "confirmed", "description": u.text, "evidence_id": u.evidence_id},
            )()
            for u in compatible
        ]
        if not tokens or (
            not tokens <= _tokens(text) and not _condition_proof(clause, state_facts)
        ):
            return False
    return True


def _version_constraints(units, entries, fact_map):
    """Only explicit comparison/requirement operators create version constraints."""
    from app.services.research_spec_v7 import VERSION_TOKEN

    statements = {}
    for u in units:
        e = entries[u.evidence_id]
        offset = e.get("statement_offset", u.offset)
        length = e.get("statement_length", u.length)
        key = (u.source_id, offset, length)
        statements.setdefault(
            key,
            dict(
                text=(e["source"].snippet or "")[offset : offset + length],
                evidence_ids=[],
                product_key=fact_map[u.evidence_id].product_key,
            ),
        )["evidence_ids"].append(u.evidence_id)
    result = []
    for statement in statements.values():
        text = statement["text"]
        for m in VERSION_TOKEN.finditer(text):
            before = text[: m.start()]
            after = text[m.end() :]
            relation = None
            if re.search(r"\b(?:or|and) later\b|及以上|或更高", after, re.I):
                relation = "at_least"
            elif re.search(r"\b(?:or|and) earlier\b|及以下|或更低", after, re.I):
                relation = "at_most"
            elif re.search(r"\b(?:at least|minimum(?: supported)?(?: version)?)\s*$", before, re.I):
                relation = "at_least"
            elif re.search(r"\b(?:at most|maximum(?: supported)?(?: version)?)\s*$", before, re.I):
                relation = "at_most"
            elif re.search(
                r"\b(?:requires?|needs?)\s+(?:(?:a |the )?device (?:with|running) )?$", before, re.I
            ) or re.match(r"\s+(?:is )?required\b", after, re.I):
                relation = "required"
            elif re.search(r"\b(?:before|earlier than)\s*$", before, re.I):
                relation = "earlier_than"
            elif re.search(r"\b(?:after|later than)\s*$", before, re.I):
                relation = "later_than"
            if relation:
                row = dict(
                    platform=m[1].casefold(),
                    relation=relation,
                    version=m[2],
                    evidence_ids=statement["evidence_ids"],
                    product_key=statement["product_key"],
                )
                if row not in result:
                    result.append(row)
    return result


def _version_conflict(constraints):
    # A conflict is one of:
    #   1. two DIFFERENT declared values of the SAME relation type for the
    #      SAME product and OS.
    #   2. an EMPTY intersection of bounds for the SAME product and OS,
    #      e.g. at_least 20 AND at_most 18 has no satisfying version. This
    #      fires even without a specific requested version; the range itself
    #      is impossible.
    #   3. a required value outside the lower/upper bounds.
    # "iOS 16 or later" + "iOS 18 or earlier" is a valid non-empty range.
    # Different products are kept independent; different relations on the
    # same product+OS are also independent (range vs minimum).
    def parts(raw):
        values = raw.split(".")
        return (
            tuple(int(v) for v in values) + tuple(0 for _ in range(8 - len(values)))
            if len(values) <= 8 and all(len(v) <= 8 for v in values)
            else None
        )

    by_product = defaultdict(lambda: defaultdict(set))
    for c in constraints:
        by_product[(c.get("product_key", "unspecified"), c["platform"])][c["relation"]].add(
            c["version"]
        )
    for relations in by_product.values():
        for versions in relations.values():
            if len(versions) > 1:
                return True
        lower = relations.get("at_least") or relations.get("later_than") or set()
        upper = relations.get("at_most") or relations.get("earlier_than") or set()
        required = relations.get("required") or set()
        if lower and upper:
            lowers = [parts(v) for v in lower if parts(v) is not None]
            uppers = [parts(v) for v in upper if parts(v) is not None]
            if lowers and uppers and max(lowers) > min(uppers):
                return True
        if lower and required:
            lowers = [parts(v) for v in lower if parts(v) is not None]
            for req in required:
                rp = parts(req)
                if rp is not None and lowers and rp > max(lowers):
                    return True
        if upper and required:
            uppers = [parts(v) for v in upper if parts(v) is not None]
            for req in required:
                rp = parts(req)
                if rp is not None and uppers and rp < min(uppers):
                    return True
    return False


def _version_membership(requested, constraints, products=()):
    def parts(raw):
        values = raw.split(".")
        return (
            tuple(int(v) for v in values) + tuple(0 for _ in range(8 - len(values)))
            if len(values) <= 8 and all(len(v) <= 8 for v in values)
            else None
        )

    def matches_product(pk):
        if not products:
            return True
        if not pk or pk == "unspecified":
            return True
        flat = pk.replace(" ", "")
        for p in products:
            if _tokens(p) <= _tokens(pk):
                return True
            if normalize(p).replace(" ", "") in flat:
                return True
        return False

    for r in requested:
        target = parts(r["version"])
        if target is None:
            return False
        matching = [
            c
            for c in constraints
            if c["platform"] == r["platform"] and matches_product(c.get("product_key"))
        ]
        if not matching:
            return False
        ok = True
        for c in matching:
            value = parts(c["version"])
            if value is None:
                continue
            ok = (
                ok
                and {
                    "at_least": target >= value,
                    "at_most": target <= value,
                    "required": target == value,
                    "earlier_than": target < value,
                    "later_than": target > value,
                }[c["relation"]]
            )
        if not ok:
            return False
    return True


def evaluate_support(spec, bindings, facts, entries, catalogs):
    requirements = {r["id"]: r for r in spec["requirements"]}
    if not isinstance(bindings, list) or len(bindings) > 16:
        raise SpecError("bindings_invalid")
    fact_map = {f.evidence_id: f for f in facts}
    if len({(f.run_id, f.project_id) for f in facts}) > 1:
        raise SpecError("fact_scope_mismatch")
    # Rejected requirement ids recorded by the spec layer as
    # unrequested local relationships. Their bindings are accepted
    # but DO NOT contribute to any item / coverage / analysis premise
    # and DO NOT invalidate evidence shared with other legitimate
    # requirements. Truly unknown ids (not in ``spec["requirements"]``
    # AND not in ``spec["rejected_requirements"]``) still raise
    # SpecError so the caller cannot fabricate requirements.
    rejected_req_ids: set[str] = {r["id"] for r in spec.get("rejected_requirements", []) or []}
    bound = {}
    rejected = []
    rejected_bindings: list[str] = []
    for b in bindings:
        if not isinstance(b, dict) or set(b) != {"requirement_id", "evidence_ids"}:
            raise SpecError("binding_schema_invalid")
        rid = b["requirement_id"]
        ids = b["evidence_ids"]
        # Bindings pointing at a rejected (locally unrequested)
        # requirement are recorded and skipped. Evidence ids within
        # such bindings must still be valid so a typo / mis-bound
        # fact stays caught — but the binding itself never
        # contributes to the rejected requirement (which is gone).
        if rid in rejected_req_ids:
            if (
                not isinstance(ids, list)
                or len(ids) > 40
                or any(not isinstance(i, str) or i not in entries or i not in fact_map for i in ids)
            ):
                raise SpecError("binding_evidence_invalid")
            if len(set(ids)) != len(ids):
                raise SpecError("binding_evidence_duplicate")
            rejected_bindings.append(rid)
            continue
        if not isinstance(rid, str) or rid not in requirements or rid in bound:
            raise SpecError("binding_requirement_invalid")
        if (
            not isinstance(ids, list)
            or len(ids) > 40
            or any(not isinstance(i, str) or i not in entries or i not in fact_map for i in ids)
        ):
            raise SpecError("binding_evidence_invalid")
        if len(set(ids)) != len(ids):
            raise SpecError("binding_evidence_duplicate")
        bound[rid] = ids
    if len({i for ids in bound.values() for i in ids}) > 40:
        raise SpecError("selection_budget_exhausted")
    items = []
    for rid, r in requirements.items():
        kept = []
        codes = []
        for eid in bound.get(rid, []):
            f = fact_map[eid]
            u, s = entries[eid]["unit"], entries[eid]["source"]
            if f.claim_kind != "fact" or (
                f.status == "confirmed" and f.source_refs != ((s.provider_source_id, s.url),)
            ):
                raise SpecError("fact_provenance_mismatch")
            if (
                f.run_id != s.run_id
                or f.description != u.text
                or f.evidence_location.get("sha256") != catalogs[u.source_id].snapshot_sha256
            ):
                raise SpecError("fact_snapshot_mismatch")
            reason = (
                "fact_not_verified"
                if f.status != "confirmed" or not f.source_refs
                else applicability(
                    r, u, s, [entries[i]["unit"] for i in bound.get(rid, [])], entries=entries
                )
            )
            if reason:
                codes.append(reason)
                rejected.append(eid)
            else:
                kept.append(eid)
        # Every delivered assertion must retain all its exact adjacent pieces.
        # Otherwise a final negation/condition could be dropped by choosing only
        # the first 200 characters of a long source sentence.
        complete = []
        for eid in kept:
            e = entries[eid]
            a = e.get("statement_offset")
            length = e.get("statement_length")
            if a is not None and any(
                not (e["source"].snippet or "")[pos].isspace()
                and not any(
                    entries[i]["unit"].offset
                    <= pos
                    < entries[i]["unit"].offset + entries[i]["unit"].length
                    for i in kept
                    if entries[i]["unit"].source_id == e["unit"].source_id
                )
                for pos in range(a, a + length)
            ):
                codes.append("statement_incomplete")
                rejected.append(eid)
            else:
                complete.append(eid)
        kept = complete
        units = [entries[i]["unit"] for i in kept]
        # Multi-product decomposition: a non-comparison, non-analysis r with
        # multiple products is decomposed into per-product sub-requirements.
        # Each sub-requirement is verified by the SAME evaluate_support, then
        # the results are combined. A side-effect from one product's URL cannot
        # satisfy another product's coverage; sources that do not identify any
        # requested product stay rejected and pollute the parent binding.
        # Run BEFORE kind-specific guards so a successful decomposition fully
        # delegates semantic checks to each single-product sub-evaluation;
        # otherwise the parent runs a two-flow `procedure` check on its own
        # unified unit list, which has nothing to do with per-product coverage.
        decomposition_succeeded = False
        if units and r["kind"] not in {"comparison", "analysis"} and len(r.get("products", [])) > 1:
            per_product_ok = True
            combined_kept = []
            for product in r["products"]:
                sub_kept = _per_product_evidence(entries, kept, product)
                if not sub_kept:
                    per_product_ok = False
                    break
                sub_r = copy.deepcopy(r)
                sub_r["products"] = [product]
                sub_spec = copy.deepcopy(spec)
                sub_spec["requirements"] = [sub_r]
                sub_spec["unresolved"] = []
                sub_bindings = [dict(requirement_id=r["id"], evidence_ids=list(sub_kept))]
                try:
                    sub_report = evaluate_support(sub_spec, sub_bindings, facts, entries, catalogs)
                except SpecError:
                    per_product_ok = False
                    break
                sub_item = sub_report["items"][0]
                if sub_item["status"] != "covered":
                    per_product_ok = False
                    break
                combined_kept.extend(sub_item.get("evidence_ids") or sub_kept)
            if per_product_ok:
                kept = list(dict.fromkeys(combined_kept))
                units = [entries[i]["unit"] for i in kept]
                decomposition_succeeded = True
            else:
                codes.append("product_coverage")
                kept = []
                units = []
        if not units:
            codes.append("evidence_missing")
        elif r["kind"] == "analysis":
            codes.append("analysis_pending")
        elif not decomposition_succeeded:
            context_action = any(
                operation_family(
                    re.sub(
                        r"^(?:how to |manually |automatically )+", "", normalize(u.section)
                    ).split()[0]
                )
                == operation_family(r.get("action"))
                for u in units
                if normalize(u.section)
            )
            action_ok = _action_present(r, units) or (
                r["kind"] in {"entry_listing", "destination", "version", "limit"} and context_action
            )
            if r["kind"] == "condition":
                action_ok = bool(r.get("condition_quotes"))
            if not action_ok:
                codes.append("action")
            if not _object_present(r, units) and not (
                r["kind"] == "condition" and r.get("condition_quotes")
            ):
                codes.append("object")
            if r["mode"] == "automatic" and not any(
                re.search(r"\bautomatic(?:ally)?\b", u.text, re.I)
                and not re.search(r"\b(?:disabled|off)\b", u.text, re.I)
                for u in units
            ):
                codes.append("mode")
            if r["mode"] == "automatic" and r.get("condition_quotes") and units:
                _exp = [
                    s for s in (_condition_state(c) for c in r["condition_quotes"]) if s is not None
                ]
                if _exp:
                    _auto_match = [
                        u
                        for u in units
                        if re.search(r"\bautomatic(?:ally)?\b", u.text, re.I)
                        and (_condition_state(u.text) is None or _condition_state(u.text) in _exp)
                    ]
                    if not _auto_match:
                        codes.append("mode")
            if not _quantity(r, units, entries):
                codes.append("quantity")
            if not _conditions(r, units):
                codes.append("condition")
            kind = r["kind"]
            if kind == "procedure" and not _procedure(units, catalogs, entries):
                codes.append("procedure")
            if kind in {"procedure", "capability"} and re.search(
                r"\bexisting\b|已有|现有", r["question_text"], re.I
            ):
                from app.services.research_catalog_v7 import line_span

                branches = [u for u in units if re.search(r"\bexisting\b", normalize(u.text))]
                if not branches:
                    codes.append("object_branch")
                for u in branches:
                    body = entries[u.evidence_id]["source"].snippet or ""
                    span = line_span(body, u.offset)
                    required = {
                        eid
                        for eid, e in entries.items()
                        if e["unit"].source_id == u.source_id
                        and line_span(body, e["unit"].offset) == span
                    }
                    if not required <= set(kept):
                        codes.append("object_branch")
            if kind == "capability" and r["mode"] != "automatic":
                # A capability backed by a published manual flow needs that
                # complete flow. A generic "can add" sentence is not a full
                # alternative workflow. Only an explicit select -> execute
                # inline branch can stand independently (e.g. the multi-item Tip).
                applicable_flow = False
                for u in units:
                    cat = catalogs.get(u.source_id)
                    section = (
                        next(
                            (
                                section
                                for section in cat.sections
                                if section.full_path == u.section_path
                            ),
                            None,
                        )
                        if cat
                        else None
                    )
                    if section and section.step_total:
                        applicable_flow = True
                inline = any(
                    not u.step_index
                    and _action_present(r, [u])
                    and _quantity(r, [u], entries)
                    and re.search(r"\b(?:select|choose|touch and hold)\b", normalize(u.text))
                    and re.search(
                        r"\bthen (?:tap|click|press)\b.*\b(?:upload|backup|add)\b",
                        normalize(u.text),
                    )
                    for u in units
                )
                if applicable_flow and not _procedure(units, catalogs, entries) and not inline:
                    codes.append("procedure")
            if kind == "limit" and not any(
                re.search(
                    r"\b(?:limit|maximum|cannot|can.t|not support|up to)\b|限制|上限|不能",
                    u.text,
                    re.I,
                )
                for u in units
            ):
                codes.append("limit")
            if kind == "entry_listing" and not any(
                re.search(r"\b(?:or|options?|choose from)\b|选项|或者", u.text, re.I) for u in units
            ):
                codes.append("entry_listing")
            if kind == "destination" and not any(
                re.search(r"\b(?:in|into|to)\s+\w+|到|位于", u.text, re.I) for u in units
            ):
                codes.append("destination")
        # Pollution check: a rejected candidate cannot vanish by coverage of
        # other candidates. Single-product non-comparison, non-analysis r with
        # any rejected evidence in the binding is not fully verifiable.
        if (
            r["kind"] not in {"comparison", "analysis"}
            and len(r.get("products", [])) == 1
            and any(e in rejected for e in bound.get(rid, []))
        ):
            codes.append("pollution")
        constraints = _version_constraints(units, entries, fact_map)
        if r["kind"] == "version" and not constraints:
            codes.append("version")
        if r.get("requested_versions") and not _version_membership(
            r["requested_versions"], constraints, r.get("products", [])
        ):
            codes.append("version")
        if _version_conflict(constraints):
            codes.append("version_conflict")
        negative_states = [_negative_scope(r, u.text) for u in units]
        negative = any("negative" in states for states in negative_states)
        restricted = any("restriction" in states for states in negative_states)
        positive = any(_action_present(r, [u]) and not _NEGATION.search(u.text) for u in units)
        if restricted and not positive and not negative:
            codes.append("negative_scope_unresolved")
        polarity = (
            "conflicting"
            if negative and positive
            else "negative"
            if negative
            else "positive"
            if positive or (units and not restricted)
            else "unknown"
        )
        if polarity == "conflicting":
            codes.append("conflicting_capability")
        if "version_conflict" in codes:
            polarity = "conflicting"
        codes = list(dict.fromkeys(codes))
        items.append(
            dict(
                requirement_id=rid,
                status="covered" if not codes else "partial" if kept else "not_verified",
                evidence_ids=kept,
                reason_codes=codes,
                gap="; ".join(codes),
                role="core",
                answer_polarity=polarity,
                version_constraints=constraints,
            )
        )
    item_by_id = {item["requirement_id"]: item for item in items}
    for r in requirements.values():
        if r["kind"] != "comparison":
            continue
        item = item_by_id[r["id"]]
        # Parent-scope obligation set: each input obligation whose
        # anchor matches the comparison's declared anchor_ids AND
        # whose owner product intersects the comparison's product
        # branches. These are the (action, object, mode, owner) tuples
        # the original question pinned for THIS comparison. The cell
        # check below uses them both as the required-cell source AND
        # the side-qualification ledger: a covered side is a real
        # applicable comparison side only when it fulfills at least
        # one such obligation. With no parent obligations anchored
        # here the legacy product-set / single-product variety gate
        # governs and null axes do NOT widen the comparison to any
        # already-covered sibling.
        spec_obligations = spec.get("input_obligations") or []
        r_anchor_ids = set(r.get("anchor_ids", []))
        r_product_set = {
            input_v7.canonical_product(p) or p.casefold() for p in r.get("products", [])
        }
        parent_obs: list[dict] = []
        for ob in spec_obligations:
            ob_anchor = ob.get("anchor_id")
            if ob_anchor and ob_anchor not in r_anchor_ids:
                continue
            owners = ob.get("owners") or []
            if r_product_set:
                if not owners or not (set(owners) & r_product_set):
                    continue
            parent_obs.append(ob)

        sides = []
        for side in requirements.values():
            proof = item_by_id[side["id"]]
            if side["kind"] in {"analysis", "comparison"} or proof["status"] != "covered":
                continue
            if not set(side["products"]) <= set(r["products"]):
                continue
            # Establish legal side qualification BEFORE any non-null
            # axis filter. When the question declared explicit
            # obligations in this comparison's anchor / owner scope,
            # the side has to fulfill at least one of them; a null
            # parent action or object is NOT a wildcard over already-
            # covered source evidence.
            if parent_obs:
                side_fulfills = set(side.get("fulfills_obligation_ids") or [])
                parent_obs_ids = {ob["id"] for ob in parent_obs}
                if not (side_fulfills & parent_obs_ids):
                    continue
            # Non-null parent axes add extra restrictions AFTER
            # qualification; r.action / r.object = None or "" skip the
            # matching filter cleanly instead of failing against empty.
            if r.get("action") and operation_family(side.get("action")) != operation_family(
                r.get("action")
            ):
                continue
            units = [entries[e]["unit"] for e in proof["evidence_ids"]]
            if r.get("object") and not _object_present(r, units):
                continue
            if not _quantity(r, units, entries):
                continue
            sides.append(side)

        # Owner × mode required cells from the parent obligations.
        # A symmetric mixed comparison yields the full (product ×
        # mode) cartesian product; an asymmetric question yields
        # only its declared cells. Empty matrix keeps the legacy
        # conservative comparison gate (no widening to null axes).
        required_cells: set[tuple[str, str]] = set()
        for ob in parent_obs:
            ob_mode = ob.get("mode_canon")
            owners = ob.get("owners") or []
            if not ob_mode:
                continue
            for owner in owners:
                if not r_product_set or owner in r_product_set:
                    required_cells.add((owner, ob_mode))

        # Covered cells: each (canonical product, side mode) pair is
        # credited only when a side covering that product+mode
        # fulfills a parent obligation whose owners and mode also
        # match the cell. The action / object axes come from the
        # obligation's own canonical because ``side.fulfills`` only
        # includes obligations whose action and object already
        # matched the side's dimensions.
        covered_cells: set[tuple[str, str]] = set()
        for side in sides:
            side_mode = side.get("mode")
            if not side_mode:
                continue
            side_fulfills = set(side.get("fulfills_obligation_ids") or [])
            for sp in side["products"]:
                canon_product = input_v7.canonical_product(sp) or sp
                if not required_cells:
                    covered_cells.add((canon_product, side_mode))
                    continue
                cell_ok = False
                for ob in parent_obs:
                    if canon_product not in (ob.get("owners") or []):
                        continue
                    if ob.get("mode_canon") != side_mode:
                        continue
                    if ob["id"] in side_fulfills:
                        cell_ok = True
                        break
                if cell_ok:
                    covered_cells.add((canon_product, side_mode))

        if required_cells:
            # Owner × mode matrix gate: every required cell must be
            # covered by a qualified side AND there must be at least
            # two truly applicable comparison sides. Subsumes both
            # the global product-set check and the (mode, entry,
            # action) variety check when the comparison has null
            # axes but the question pinned owner+mode+action+object.
            valid = required_cells <= covered_cells and len(sides) >= 2
        else:
            requested_products = set(r["products"])
            proven_products = {p for side in sides for p in side["products"]}
            valid = requested_products <= proven_products and len(sides) >= 2
            if len(requested_products) == 1:
                valid = (
                    valid
                    and len(
                        {
                            (
                                side["mode"],
                                side.get("entry"),
                                operation_family(side.get("action")),
                            )
                            for side in sides
                        }
                    )
                    >= 2
                )
        # Mixed-mode comparison must cover BOTH the manual and the automatic
        # flow in the same Run; two manual sides cannot satisfy "manual and
        # automatic" and a single manual product cannot either.
        if valid and r.get("mode") == "mixed":
            # Mixed-mode comparison must cover BOTH the manual and the
            # automatic flow in the same Run. Use the typed side.mode values
            # rather than re-parsing unit text; a normal numbered manual
            # procedure that omits the word "manually" still proves manual.
            side_modes = {side.get("mode") for side in sides}
            if not ({"manual", "automatic"} <= side_modes):
                valid = False
            else:
                # Parent platform/entry/conditions/version must not borrow
                # from the opposite side: each side's verified properties must
                # stay compatible with r's declared properties.
                def _side_compatible(side):
                    if (
                        r.get("platform")
                        and side.get("platform")
                        and _platform_class(side.get("platform"))
                        != _platform_class(r.get("platform"))
                        and not (
                            _platform_class(r.get("platform"))
                            in {"mobile", "phone", "tablet", "unspecified", ""}
                            and _platform_class(side.get("platform"))
                            in {"ios", "android", "ipados", "iphone", "ipad", "mobile"}
                        )
                    ):
                        return False
                    if (
                        r.get("entry") not in {None, "", "app"}
                        and side.get("entry") not in {None, "", "app"}
                        and r.get("entry") != side.get("entry")
                    ):
                        return False
                    return True

                if not all(_side_compatible(side) for side in sides):
                    valid = False
        # Invalid candidate spans remain a visible rejection. An empty parent
        # binding, however, is not a factual omission when both typed children
        # are already independently verified in this exact Run/project.
        polluted = any(e in rejected for e in bound.get(r["id"], []))
        if valid and not polluted:
            item.update(
                status="covered",
                evidence_ids=list(
                    dict.fromkeys(
                        e for side in sides for e in item_by_id[side["id"]]["evidence_ids"]
                    )
                ),
                reason_codes=[],
                gap="",
                side_requirement_ids=[side["id"] for side in sides],
            )
        else:
            item["reason_codes"] = list(dict.fromkeys(item["reason_codes"] + ["comparison_sides"]))
            item["gap"] = "; ".join(item["reason_codes"])
            item["status"] = "partial" if item["evidence_ids"] else "not_verified"
    return dict(
        version="research-support-v7",
        items=items,
        rejected_ids=list(dict.fromkeys(rejected)),
        overall_completable=bool(items)
        and not spec["unresolved"]
        and all(i["status"] == "covered" for i in items),
    )


class SupportLedger:
    """Adopt verified groups per frozen requirement, never by excerpt count."""

    def __init__(self, spec):
        self.spec = copy.deepcopy(spec)
        self.items = {
            r["id"]: dict(
                requirement_id=r["id"],
                status="not_verified",
                evidence_ids=[],
                reason_codes=["evidence_missing"],
                gap="evidence_missing",
                role="core",
                adopted_attempt=None,
            )
            for r in spec["requirements"]
        }
        self.rejected = []

    def update(self, report, *, attempt):
        incoming = report.get("items", [])
        if not isinstance(incoming, list) or any(
            i.get("requirement_id") not in self.items for i in incoming
        ):
            raise SpecError("ledger_requirement_unknown")
        if len({i["requirement_id"] for i in incoming}) != len(incoming):
            raise SpecError("ledger_requirement_duplicate")
        updated = copy.deepcopy(self.items)
        rank = {"not_verified": 0, "partial": 1, "covered": 2}
        for item in incoming:
            status = item.get("status")
            if status not in rank:
                raise SpecError("ledger_status_invalid")
            ids = item.get("evidence_ids")
            if (
                not isinstance(ids, list)
                or any(not isinstance(i, str) for i in ids)
                or len(set(ids)) != len(ids)
            ):
                raise SpecError("ledger_evidence_invalid")
            rid = item["requirement_id"]
            old = updated[rid]
            old_versions = old.get("version_constraints", [])
            new_versions = item.get("version_constraints", [])
            if (
                old["status"] == "covered"
                and status == "covered"
                and old_versions
                and new_versions
                and _version_conflict(old_versions + new_versions)
            ):
                updated[rid] = old | dict(
                    status="partial",
                    reason_codes=["version_conflict"],
                    gap="version_conflict",
                    answer_polarity="conflicting",
                    adopted_attempt=None,
                    evidence_ids=list(dict.fromkeys(old["evidence_ids"] + ids)),
                    version_constraints=old_versions + new_versions,
                    conflict_groups=[
                        dict(
                            attempt=old["adopted_attempt"],
                            evidence_ids=old["evidence_ids"],
                            version_constraints=old_versions,
                        ),
                        dict(attempt=attempt, evidence_ids=ids, version_constraints=new_versions),
                    ],
                )
                continue
            if old.get("conflict_groups"):
                continue
            old_p = old.get("answer_polarity")
            new_p = item.get("answer_polarity")
            if (
                old["status"] == "covered"
                and status == "covered"
                and {old_p, new_p} == {"positive", "negative"}
            ):
                updated[rid] = old | dict(
                    status="partial",
                    answer_polarity="conflicting",
                    reason_codes=["conflicting_capability"],
                    gap="conflicting_capability",
                    conflicting_attempts=[old["adopted_attempt"], attempt],
                    adopted_attempt=None,
                    evidence_ids=list(dict.fromkeys(old["evidence_ids"] + ids)),
                    conflict_groups=[
                        dict(attempt=old["adopted_attempt"], evidence_ids=old["evidence_ids"]),
                        dict(attempt=attempt, evidence_ids=ids),
                    ],
                )
                continue
            if old.get("answer_polarity") == "conflicting":
                continue
            improved = status == old["status"] == "partial" and set(
                item.get("reason_codes", [])
            ) < set(old.get("reason_codes", []))
            if rank[status] > rank[old["status"]] or improved:
                updated[rid] = copy.deepcopy(item) | {"adopted_attempt": attempt}

        def invalidate_comparisons(items):
            for item in items.values():
                sides = item.get("side_requirement_ids", [])
                if (
                    item["status"] == "covered"
                    and sides
                    and any(items[side]["status"] != "covered" for side in sides)
                ):
                    item.update(
                        status="partial",
                        evidence_ids=[],
                        adopted_attempt=None,
                        reason_codes=["comparison_side_not_covered"],
                        gap="comparison_side_not_covered",
                    )

        invalidate_comparisons(updated)
        if len({eid for i in updated.values() for eid in i["evidence_ids"]}) > 40:
            conflicts = {
                rid: item
                for rid, item in updated.items()
                if item.get("answer_polarity") == "conflicting"
                and self.items[rid].get("answer_polarity") != "conflicting"
            }
            if not conflicts:
                raise SpecError("ledger_selection_budget_exhausted")
            # A delivery budget must never restore a disproved premise.
            # Full opposing groups remain snapshot-addressed diagnostics.
            updated = copy.deepcopy(self.items)
            for rid, item in conflicts.items():
                codes = item["reason_codes"] + ["ledger_selection_budget_exhausted"]
                updated[rid] = item | dict(
                    evidence_ids=[], reason_codes=codes, gap="; ".join(codes)
                )
            invalidate_comparisons(updated)
        self.items = updated
        self.rejected = list(dict.fromkeys(self.rejected + report.get("rejected_ids", [])))

    def report(self):
        items = copy.deepcopy(list(self.items.values()))
        return dict(
            version="research-support-v7",
            items=items,
            rejected_ids=list(self.rejected),
            overall_completable=bool(items)
            and not self.spec["unresolved"]
            and all(i["status"] == "covered" for i in items),
        )
