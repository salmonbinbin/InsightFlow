"""Bounded plan -> retrieve -> select/repair with server-owned support retention."""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import replace
from urllib.parse import urlsplit

from app.agents.prompts import research_agent_v7, research_joint_v7, research_plan_v7
from app.services import research_input_v7 as input_v7
from app.services import research_reader
from app.services import research_search_adapters as search_adapters
from app.services.research_answer_v7 import (
    ANALYSIS_SCHEMA,
    SELECTION_SCHEMA,
    analyses,
    build_answer,
    complete_analysis_items,
)
from app.services.research_catalog_v7 import branch_span, build_catalogs
from app.services.research_claims import ClaimError
from app.services.research_pipeline_v6 import _persist_claims
from app.services.research_spec_v7 import (
    ResearchBudget,
    SpecError,
    analysis_inventory,
    compile_spec,
    question_parts,
    validate,
)
from app.services.research_support_v7 import (
    SupportLedger,
    _tokens,
    applicability,
    evaluate_support,
    exact_facts,
)


def _rules(api, context):
    # Freeze source namespaces once. A requirement cannot add an arbitrary host.
    text = (context.research_question + " " + (context.scope or "")).lower()
    requested = [
        rule
        for rule in api._OFFICIAL_VENDOR_RULES.values()
        if any(k in text for k in rule["product_keywords"])
    ]
    return requested or list(api._OFFICIAL_VENDOR_RULES.values())


def queries(spec, rules):
    """Preserve the original anchor in every query, plus planned translations."""
    requirements = {r["id"]: r for r in spec["requirements"]}
    result = []
    candidates = spec["queries"] or [
        dict(id=r["id"], text=" ".join(r["products"]))
        for r in requirements.values()
        if r["kind"] != "analysis"
    ]
    for candidate in candidates:
        r = requirements.get(candidate["id"])
        if r is None:
            raise SpecError("query_requirement_unknown")
        matching = [
            rule
            for rule in rules
            if any(k in " ".join(r["products"]).lower() for k in rule["product_keywords"])
        ] or rules
        rule = matching[0]
        # Remove model-controlled source operators; policy belongs to the server.
        translation = re.sub(r"\bsite:\S+|https?://\S+", "", candidate["text"], flags=re.I)
        original = r["question_text"]
        # Keep the ORIGINAL action even for Chinese queries; translation augments it.
        core = (original + " " + translation).strip()
        suffix = " site:" + rule["site"]
        query = core[: 500 - len(suffix)] + suffix
        if query not in [q["text"] for q in result]:
            result.append(dict(text=query, requirement_id=r["id"]))
    # Two-product queries must each have a slot, even if the planner repeated one.
    for r in requirements.values():
        if r["kind"] == "analysis":
            continue
        for rule in rules:
            if not any(k in " ".join(r["products"]).lower() for k in rule["product_keywords"]):
                continue
            if any(
                "site:" + rule["site"] in q["text"] and q["requirement_id"] == r["id"]
                for q in result
            ):
                continue
            suffix = " site:" + rule["site"]
            result.append(
                dict(text=r["question_text"][: 500 - len(suffix)] + suffix, requirement_id=r["id"])
            )
    return result[:3]


def _offer(spec, entries, previous, limit):
    """Round-robin complete source sections, ranked by the frozen obligations."""
    streams = []
    for r in spec["requirements"]:
        if r["kind"] == "analysis":
            continue
        grouped = {}
        all_units = [e["unit"] for e in entries.values()]
        for eid, e in entries.items():
            u = e["unit"]
            key = (u.source_id, u.section_path)
            if applicability(r, u, e["source"], all_units, entries=entries) is None:
                grouped.setdefault(key, []).append((eid, e))
        target = _tokens(" ".join([r["action"] or "", r["object"] or "", r["question_text"]]))

        def score(group):
            return len(
                target
                & _tokens(" ".join(e["unit"].text + " " + e["unit"].context for _, e in group))
            )

        groups = list(grouped.values())
        if r["kind"] == "capability" and r["mode"] == "automatic":
            groups = [
                g
                for g in groups
                if any(re.search(r"\bautomatic(?:ally)?\b", e["unit"].text, re.I) for _, e in g)
            ]
        ordered = sorted(groups, key=score, reverse=True)
        streams.append(
            [pair for group in ordered for pair in sorted(group, key=lambda p: p[1]["unit"].offset)]
        )
    offered = dict(previous)
    while any(streams) and len(offered) < limit:
        for stream in streams:
            while stream and stream[0][0] in offered:
                stream.pop(0)
            if stream and len(offered) < limit:
                eid, e = stream.pop(0)
                offered[eid] = e
    return offered


def _unplanned_queries(context, rules):
    original = "; ".join(a["text"] for a in question_parts(context.research_question)["anchors"])
    result = []
    for rule in rules:
        suffix = " site:" + rule["site"]
        text = original[: 500 - len(suffix)] + suffix
        if text not in [q["text"] for q in result]:
            result.append(dict(text=text, requirement_id=None))
    return result[:2]


def _unplanned_offer(question, entries, previous, limit):
    groups = {}
    for eid, e in entries.items():
        groups.setdefault((e["unit"].source_id, e["unit"].section_path), []).append((eid, e))
    words = _tokens(question)
    by_source = {}
    for (source, _), group in groups.items():
        by_source.setdefault(source, []).append(group)
    streams = []
    for source_groups in by_source.values():
        ranked = sorted(
            source_groups,
            key=lambda group: len(
                words
                & _tokens(" ".join(e["unit"].text + " " + e["unit"].context for _, e in group))
            ),
            reverse=True,
        )
        streams.append(
            [p for group in ranked for p in sorted(group, key=lambda p: p[1]["unit"].offset)]
        )
    offered = dict(previous)
    while any(streams) and len(offered) < limit:
        for stream in streams:
            while stream and stream[0][0] in offered:
                stream.pop(0)
            if stream and len(offered) < limit:
                eid, e = stream.pop(0)
                offered[eid] = e
    return offered


def _pre_validate_planning(question, candidate, scope=None):
    """Reclassify pure source-policy requirements BEFORE compile_spec.

    Order of checks (each failure preserves the original candidate so
    compile_spec still surfaces the same error):

      1. ``validate(PLANNING_SCHEMA, candidate)`` — catches bad extra
         fields, missing required, and schema-level duplicate IDs.
      2. Original requirement IDs are pairwise unique (uses the
         ORIGINAL pre-filter list, not post-filter, so a duplicate
         detected here is still compile_spec's responsibility).
      3. Every original ``queries[].id`` references an ORIGINAL
         requirement id. Unknown refs are STRICT — they are NOT
         silently dropped or patched.
      4. Pure source-policy is detected with ``kind == "condition"``,
         non-empty ``condition_quotes`` and ALL quotes byte-for-byte
         equal to one of the combined question+scope policy_spans
         extracted via ``_POLICY`` (scope is unconstrained up to its
         own 2000-char limit; the 500-char ``question_parts`` guard
         is NOT applied to scope).
      5. Only ORIGINAL queries referencing a removed policy id are
         dropped. Negative cases (mixed business conditions,
         partial quote, duplicate IDs, unknown query refs) preserve
         the original candidate so compile_spec still rejects them.

    The original candidate dict is NEVER mutated; ``revised`` is a
    new dict whose ``requirements`` and ``queries`` are deep copies
    of the kept subsets.
    """
    from app.services.research_spec_v7 import (
        _POLICY,
        PLANNING_SCHEMA,
        validate,
    )

    if not isinstance(candidate, dict):
        return candidate, None

    # Phase 1: schema-level guard. ``validate`` itself rejects bad
    # extra fields (closed), missing required, and ``uniqueItems`` on
    # requirements (schema_duplicate). An unknown extra field on a
    # policy candidate must NOT be silently repaired by stripping it.
    try:
        validate(PLANNING_SCHEMA, candidate)
    except SpecError:
        return candidate, None

    original_reqs = list(candidate.get("requirements", []) or [])
    original_queries = list(candidate.get("queries", []) or [])

    # Phase 2: pairwise ID uniqueness on the ORIGINAL list.
    seen_ids: set = set()
    for r in original_reqs:
        rid = r.get("id")
        if rid in seen_ids:
            return candidate, None  # preserve schema_duplicate
        seen_ids.add(rid)
    original_ids = set(seen_ids)

    # Phase 3: every original query id resolves to an ORIGINAL
    # requirement id. Unknown refs are STRICT (no silent patch).
    for q in original_queries:
        if q.get("id") not in original_ids:
            return candidate, None  # preserve query_requirement_unknown

    # Phase 4: combined question + scope policy_spans. Question uses
    # the existing question_parts grammar; scope bypasses the 500-char
    # ``question_parts`` ceiling and runs ``_POLICY`` directly on the
    # raw text so a long legal scope does not falsely miss its own
    # citation policy.
    policy_texts: list[str] = []
    try:
        parts = question_parts(question)
        for p in parts.get("policy_spans", []) or []:
            text = p["text"]
            if text not in policy_texts:
                policy_texts.append(text)
    except SpecError:
        # If question_parts fails (e.g. question too long), let
        # compile_spec surface the same failure. Policy归位 is moot
        # here; no span to match anyway.
        return candidate, None
    if scope:
        for m in _POLICY.finditer(scope or ""):
            text = m.group()
            if text not in policy_texts:
                policy_texts.append(text)

    # Phase 5: identify pure source-policy requirements on the
    # ORIGINAL list. Mixed business conditions / partial quotes /
    # wrong kind all stay — they are NOT pure policy.
    removed_ids: list[str] = []
    kept_reqs: list[dict] = []
    removal_reasons = {}
    for r in original_reqs:
        if (
            r["kind"] in {"capability", "procedure"}
            and not input_v7.canonical_action(r.get("action") or "")
            and (r.get("action") or "").casefold() in input_v7.auxiliary_verbs_in_text(question)
        ):
            removed_ids.append(r["id"])
            removal_reasons[r["id"]] = "auxiliary_not_independent_requirement"
            continue
        if (
            r.get("kind") == "condition"
            and r.get("condition_quotes")
            and all(q in policy_texts for q in r["condition_quotes"])
        ):
            removed_ids.append(r["id"])
            removal_reasons[r["id"]] = "source_policy"
            continue
        kept_reqs.append(r)
    if not removed_ids:
        return candidate, None

    # Phase 6: drop only ORIGINAL queries whose target id is being
    # removed. Truly unknown refs were already rejected in Phase 3.
    removed_id_set = set(removed_ids)
    kept_queries = [q for q in original_queries if q.get("id") not in removed_id_set]

    import copy as _copy

    revised = dict(candidate)
    revised["requirements"] = _copy.deepcopy(kept_reqs)
    revised["queries"] = _copy.deepcopy(kept_queries)
    return revised, dict(
        removed_ids=sorted(removed_ids),
        reason="source_policy"
        if all(v == "source_policy" for v in removal_reasons.values())
        else "planning_candidate_reclassified",
        removed_reasons=removal_reasons,
    )


def _selection_closure(spec, payload, offered, known_rejected_ids=None):
    """Close a proposed flow over already offered exact structural fragments.

    Only adds numbered peers/adjacent pieces in the same SOURCE FLOW; never
    drops a contaminated candidate or invents quantity/conditions. Same
    long-line statement fragments and same conditional branches remain
    expandable across flow boundaries. The entire expansion is atomic under
    the original 40-ID ceiling.

    Numbered-flow isolation:
      - When both entries carry ``flow_id``: ``same_numbered_flow`` requires
        identical ``flow_id``. Sibling lists under one heading that restart
        numbering (``1, 2`` then ``1, 2`` again) stay isolated even when they
        share ``(source_id, section_path)``.
      - When BOTH entries lack ``flow_id``: fall back to ``section_path``
        grouping (legacy catalogs).
      - Otherwise (one has flow_id, the other doesn't): no same-numbered-flow.
      Same-statement long-line fragments and same-condition branches ignore
      the flow boundary.

    Rejected-requirement binding handling:
      Bindings whose ``requirement_id`` is in ``spec["rejected_requirements"]``
      OR the pipeline-local ``known_rejected_ids`` are dropped locally with a
      trace record (id + reason). Truly unknown binding IDs still raise
      ``SpecError("binding_requirement_invalid")``.
    """
    import copy

    result = copy.deepcopy(payload)
    requirements = {r["id"]: r for r in spec["requirements"]}
    rejected_ids = {x["id"] for x in spec.get("rejected_requirements", []) or []}
    if known_rejected_ids:
        rejected_ids |= set(known_rejected_ids)
    original = set(payload["evidence_ids"])
    all_ids = set(original)
    peers = [e["unit"] for e in offered.values()]
    skipped_local = []
    processed = []
    for binding in result["bindings"]:
        r_id = binding["requirement_id"]
        r = requirements.get(r_id)
        if r is None:
            if r_id in rejected_ids:
                skipped_local.append(dict(requirement_id=r_id, reason="known_rejected_local"))
                continue
            raise SpecError("binding_requirement_invalid")
        expanded = set(binding["evidence_ids"])
        for eid in binding["evidence_ids"]:
            e = offered[eid]
            u, source = e["unit"], e["source"]
            if applicability(r, u, source, peers, entries=offered) is not None:
                continue
            statement = (e.get("statement_offset"), e.get("statement_length"))
            span = branch_span(source.snippet or "", u.offset)
            conditional = (
                (source.snippet or "")[span[0] : span[1]]
                .lstrip(" -*")
                .lower()
                .startswith(("if ", "when "))
            )
            e_flow_id = e.get("flow_id")
            for other, value in offered.items():
                v = value["unit"]
                if v.source_id != u.source_id:
                    continue
                same_statement = statement[0] is not None and statement == (
                    value.get("statement_offset"),
                    value.get("statement_length"),
                )
                v_flow_id = value.get("flow_id")
                if r["kind"] in {"procedure", "capability"} and v.step_index:
                    if e_flow_id is not None and v_flow_id is not None:
                        same_numbered_flow = e_flow_id == v_flow_id
                    elif e_flow_id is None and v_flow_id is None:
                        same_numbered_flow = value["unit"].section_path == u.section_path
                    else:
                        same_numbered_flow = False
                else:
                    same_numbered_flow = False
                same_condition = conditional and span == branch_span(source.snippet or "", v.offset)
                if (same_statement or same_numbered_flow or same_condition) and applicability(
                    r, v, source, peers, entries=offered
                ) is None:
                    expanded.add(other)
        binding["evidence_ids"] = sorted(
            expanded,
            key=lambda eid: (offered[eid]["unit"].source_id, offered[eid]["unit"].offset),
        )
        all_ids.update(expanded)
        processed.append(binding)
    result["bindings"] = processed
    if len(all_ids) > 40:
        return payload, dict(status="budget_declined", added_ids=[], skipped_local=skipped_local)
    added = sorted(
        all_ids - original,
        key=lambda eid: (offered[eid]["unit"].source_id, offered[eid]["unit"].offset),
    )
    result["evidence_ids"] += added
    return result, dict(
        status="expanded" if added else "unchanged",
        added_ids=added,
        skipped_local=skipped_local,
    )


def execute_run(db, run, *, internal_context, provider_query, api):
    context = internal_context
    budget = ResearchBudget()
    trace = dict(version="research-execution-v7", events=[], last_failure=None, analysis_gaps=[])
    spec = None
    ledger = None
    sources = []
    full = set()
    seen = set()
    offered = {}
    catalogs = {}
    entries = {}
    fact_pool = {}
    analysis_rows = ()
    candidates = []
    candidate_origins = []
    used_queries = set()
    queue = []
    rules = _rules(api, context)

    def allowed(url):
        return any(
            api._is_official_vendor_host(url, r) and api._is_official_vendor_path(url, r)
            for r in rules
        )

    def checkpoint():
        trace["budgets"] = budget.to_dict() | {"sources": len(sources)}
        run.execution_trace = dict(trace)
        db.commit()

    def fail(code, attempt):
        trace["last_failure"] = dict(code=code, attempt=attempt)

    def discover(q):
        for name in search_adapters.configured_providers(api.settings):
            if budget.to_dict()["logical_searches"] >= 3:
                return None, None
            # A fallback is another logical search, even for the same query.
            budget.reserve("logical_searches")
            event = dict(
                stage="search", provider=name, query=q["text"], requirement_id=q["requirement_id"]
            )
            trace["events"].append(event)
            checkpoint()
            provider = None
            result = None
            try:
                with search_adapters.build_search_client(
                    api.settings, name, tavily_client=api.ResearchProviderClient
                ) as provider:
                    result = provider.search(q["text"])
            except (
                api.ResearchProviderTransportError,
                api.ResearchProviderResponseError,
                search_adapters.ResearchProviderConfigError,
            ) as error:
                event.update(error_code="provider_failure", failure=error.diagnostic)
            finally:
                attempts = getattr(provider, "last_attempt_count", 1) if provider else 0
                elapsed = getattr(provider, "last_call_latency_ms", 0) if provider else 0
                event.update(
                    physical_attempts=attempts,
                    latency_ms=elapsed,
                    attempts=getattr(provider, "last_attempts", []),
                )
                run.attempt_count += attempts
                run.latency_ms += elapsed
                checkpoint()
            if result is not None:
                return result, event
        return None, None

    def fetch(q):
        if q["text"] in used_queries or budget.to_dict()["logical_searches"] >= 3:
            return
        used_queries.add(q["text"])
        if len(used_queries) == 1:
            run.provider_query = q["text"]
        result, event = discover(q)
        if result is not None:
            event["sources_before_filters"] = [
                dict(provider_source_id=s.provider_source_id, url=s.url, title=s.title)
                for s in result.sources
            ]
            available = []
            for source in result.sources:
                candidate_url = (
                    search_adapters.official_language_candidate(source.url, allowed)
                    if result.provider == "minimax"
                    else source.url
                    if allowed(source.url)
                    else None
                )
                if candidate_url is None:
                    continue
                if candidate_url != source.url:
                    event.setdefault("language_candidates", []).append(
                        dict(discovered_url=source.url, read_candidate=candidate_url)
                    )
                    source = replace(source, url=candidate_url)
                available.append(source)
            quota = min(
                2 if budget.to_dict()["logical_searches"] < 3 else 5,
                5 - budget.to_dict()["official_reads"],
            )
            fresh = []
            for source in available:
                if source.url in seen or len(sources) + len(fresh) >= 5 or quota <= 0:
                    continue
                if any(s.provider_source_id == source.provider_source_id for s in sources + fresh):
                    trace["events"].append(dict(stage="source_guard", code="provider_id_collision"))
                    continue
                seen.add(source.url)
                budget.reserve("official_reads")
                quota -= 1
                read = dict(stage="official_read", url=source.url)
                trace["events"].append(read)
                checkpoint()
                started = time.monotonic()
                try:
                    snapshot = research_reader.read_official(source.url, allowed)
                    source = replace(
                        source,
                        url=snapshot.url,
                        domain=urlsplit(snapshot.url).hostname,
                        snippet=snapshot.text,
                    )
                    seen.add(snapshot.url)
                    full.add(source.provider_source_id)
                    read.update(
                        status="read",
                        sha256=hashlib.sha256(snapshot.text.encode()).hexdigest(),
                        final_url=snapshot.url,
                        byte_count=snapshot.byte_count,
                    )
                except research_reader.ReadError as error:
                    read.update(status="not_verified", code=error.code)
                finally:
                    elapsed = int((time.monotonic() - started) * 1000)
                    read["latency_ms"] = elapsed
                    run.latency_ms += elapsed
                fresh.append(source)
            sources.extend(
                api._persist_sources(
                    db,
                    run,
                    api.ProviderSearchResult(
                        query=q["text"], provider=result.provider, sources=fresh
                    ),
                )
            )
            run.source_count = len(sources)
        checkpoint()

    hint = None
    policy_removed_ids: list[str] = []
    while budget.to_dict()["llm_calls"] < 3:
        stage = (
            ("joint" if budget.to_dict()["llm_calls"] else "planning")
            if spec is None
            else "selection"
        )
        if stage == "joint":
            if not used_queries:
                for q in _unplanned_queries(context, rules):
                    fetch(q)
            catalogs, entries = build_catalogs(sources)
            offered = _unplanned_offer(
                context.research_question,
                entries,
                offered,
                60 if budget.to_dict()["llm_calls"] == 1 else 100,
            )
            budget.offer(list(offered))
        if spec is not None:
            if not queue:
                queue = queries(spec, rules)
            if not used_queries:
                # Initial slots retrieve separate planned obligations. One slot
                # remains for missing evidence after selection, within the same 3.
                for q in queue[:2]:
                    fetch(q)
            elif ledger and not ledger.report()["overall_completable"]:
                remaining = next((q for q in queue if q["text"] not in used_queries), None)
                if (
                    remaining
                    and budget.to_dict()["logical_searches"] < 3
                    and budget.to_dict()["official_reads"] < 5
                ):
                    fetch(remaining)
            catalogs, entries = build_catalogs(sources)
            offered = _offer(
                spec, entries, offered, 60 if budget.to_dict()["llm_calls"] == 1 else 100
            )
            budget.offer(list(offered))
        budget.reserve("llm_calls")
        attempt = budget.to_dict()["llm_calls"]
        event = dict(stage="llm", phase=stage, attempt=attempt, retry_hint=hint)
        trace["events"].append(event)
        run.attempt_count += 1
        if hint:
            run.retry_count += 1
        trace["evidence_locations"] = [
            dict(
                evidence_id=eid,
                provider_source_id=e["unit"].source_id,
                offset=e["unit"].offset,
                length=e["unit"].length,
                section=e["unit"].section,
                statement_offset=e.get("statement_offset"),
                statement_length=e.get("statement_length"),
            )
            for eid, e in offered.items()
        ]
        # Group offered IDs into source-flow buckets for selection/joint calls.
        # Only flow_id-tagged fragments appear; unattributed entries (pre/post
        # description lines, K=1 inherited descriptions) are reported per-flow.
        flow_buckets: dict[str, dict] = {}
        for _eid, _entry in offered.items():
            _fid = _entry.get("flow_id")
            if not _fid:
                continue
            _bucket = flow_buckets.setdefault(
                _fid,
                dict(
                    flow_id=_fid,
                    evidence_ids=[],
                    source=_entry["unit"].source_id,
                    section=_entry["unit"].section,
                    flow_step_total=_entry.get("flow_step_total"),
                ),
            )
            _bucket["evidence_ids"].append(_eid)
        flow_catalog = [
            dict(
                flow_id=b["flow_id"],
                evidence_ids=sorted(
                    b["evidence_ids"],
                    key=lambda _eid: (
                        offered[_eid]["unit"].source_id,
                        offered[_eid]["unit"].offset,
                    ),
                ),
                source=b["source"],
                section=b["section"],
                flow_step_total=b["flow_step_total"],
            )
            for b in sorted(
                flow_buckets.values(),
                key=lambda x: (x["source"], x["flow_id"]),
            )
        ]
        message = dict(
            stage=stage,
            internal_context=context.to_dict(),
            research_question=context.research_question,
            scope=context.scope,
            question_anchors=question_parts(context.research_question)["anchors"],
            policy_spans=question_parts(context.research_question)["policy_spans"],
            input_obligations=input_v7.extract_input_obligations(
                context.research_question,
                anchor_list=question_parts(context.research_question)["anchors"],
            ),
            flow_catalog=flow_catalog if stage in {"selection", "joint"} else [],
            task_contract=spec,
            previous_support=ledger.report() if ledger else None,
            search_results=[
                dict(
                    provider_source_id=s.provider_source_id,
                    url=s.url,
                    title=s.title,
                    domain=s.domain,
                )
                for s in sources
            ],
            evidence_catalog=[
                dict(
                    evidence_id=eid,
                    provider_source_id=e["unit"].source_id,
                    text=e["unit"].text,
                    section=e["unit"].section,
                    context=e["unit"].context,
                    offset=e["unit"].offset,
                    length=e["unit"].length,
                    step_index=e["unit"].step_index,
                    step_total=e["unit"].step_total,
                    flow_id=e.get("flow_id"),
                    flow_step_total=e.get("flow_step_total"),
                    statement_offset=e.get("statement_offset"),
                    statement_length=e.get("statement_length"),
                )
                for eid, e in sorted(
                    offered.items(), key=lambda p: (p[1]["unit"].source_id, p[1]["unit"].offset)
                )
            ],
        )
        checkpoint()
        started = time.monotonic()
        try:
            with api.ResearchLLMClient(
                api_key=api.settings.llm_api_key,
                base_url=api.settings.llm_base_url,
                model=api.settings.llm_model,
                timeout_seconds=api.settings.llm_timeout_seconds,
                prompt_module=research_plan_v7
                if stage == "planning"
                else research_joint_v7
                if stage == "joint"
                else research_agent_v7,
            ) as client:
                result = client.generate_research(
                    user_message=message, max_tokens=8192, retry_hint=hint
                )
            event["response_metadata"] = result.response_metadata
            run.input_tokens += result.usage.input_tokens or 0
            run.output_tokens += result.usage.output_tokens or 0
            if result.usage.input_tokens is None or result.usage.output_tokens is None:
                run.unknown_token_calls += 1
            payload = result.payload
            if stage in {"planning", "joint"}:
                if stage == "joint":
                    if not isinstance(payload, dict) or set(payload) != {
                        "requirements",
                        "queries",
                        "evidence_ids",
                        "bindings",
                        "analyses",
                    }:
                        raise SpecError("joint_envelope_invalid")
                    plan_payload = {k: payload[k] for k in ("requirements", "queries")}
                else:
                    plan_payload = payload
                # SourcePolicy归位: drop pure policy requirements BEFORE compile_spec.
                # Negative cases (mixed/partial quotes, duplicate IDs, dangling
                # queries) are preserved unchanged so compile_spec still rejects.
                revalidated, policy_event = _pre_validate_planning(
                    context.research_question, plan_payload, context.scope
                )
                if policy_event is not None:
                    event["policy_reclassification"] = policy_event
                    policy_removed_ids.extend(policy_event["removed_ids"])
                    plan_payload = revalidated
                proposed = compile_spec(context.research_question, plan_payload, context.scope)
                # Freeze atomically after BOTH representation and query checks.
                proposed_queue = queries(proposed, rules)
                spec = proposed
                queue = proposed_queue
                run.task_contract = spec
                ledger = SupportLedger(spec)
                event["accepted_requirement_ids"] = [r["id"] for r in spec["requirements"]]
                hint = None
                if stage == "planning":
                    continue
                payload = {k: payload[k] for k in ("evidence_ids", "bindings", "analyses")}
            # Independent channels share a closed envelope. A rejected analysis
            # never authorizes an analysis row, and cannot erase valid exact facts.
            if not isinstance(payload, dict) or set(payload) != {
                "evidence_ids",
                "bindings",
                "analyses",
            }:
                raise SpecError("selection_envelope_invalid")
            from app.services.research_spec_v7 import closed

            validate(
                closed(
                    {k: SELECTION_SCHEMA["properties"][k] for k in ("evidence_ids", "bindings")}
                ),
                {k: payload[k] for k in ("evidence_ids", "bindings")},
            )
            if not isinstance(payload["analyses"], list) or len(payload["analyses"]) > 8:
                raise SpecError("analysis_budget_invalid")
            selected = payload["evidence_ids"]
            if any(i not in offered for i in selected):
                raise SpecError("selection_id_not_offered")
            used = {i for b in payload["bindings"] for i in b["evidence_ids"]}
            if not used <= set(selected):
                raise SpecError("support_id_not_selected")
            payload, event["selection_closure"] = _selection_closure(
                spec, payload, offered, known_rejected_ids=policy_removed_ids or None
            )
            selected = payload["evidence_ids"]
            new = exact_facts(
                selected,
                offered,
                run_id=run.id,
                project_id=run.project_id,
                api=api,
                full_source_ids=full,
            )
            # Retain UUIDs of already seen immutable facts so cross-round premise
            # references can never point at rows that were displaced by a retry.
            for f in new:
                fact_pool.setdefault(f.evidence_id, f)
            report = evaluate_support(
                spec, payload["bindings"], tuple(fact_pool[i] for i in selected), offered, catalogs
            )
            ledger.update(report, attempt=attempt)
            event["selected_evidence_ids"] = selected
            event["support_report"] = report
            event["guard_failures"] = [
                dict(evidence_id=f.evidence_id, code=f.evidence_location.get("first_failed_guard"))
                for f in new
                if f.status != "confirmed"
            ]
            # Z1b: budget base is the LATEST ledger.items[*].evidence_ids
            # (NOT fact_pool, which retains past un-delivered diagnostic
            # IDs that would falsely inflate the budget). Shape-legal
            # raw is deep-copied into a bounded archive (≤24); the
            # adoption-budget check runs ONLY at pick time (representative
            # / fill remaining) with base = this round's selected ∪
            # latest ledger delivered IDs. Over-40 is recorded once per
            # candidate as analysis_selection_budget_exhausted and the
            # row is dropped; the raw stays in archive for later rounds.
            # Final delivered union > 40 is a real protection (drop
            # rows from the tail) rather than a trace-only event.
            # Schema validation precedes semantics and adoption.
            import copy as _copy

            # Phase 1 — schema validation + raw archive (no budget here).
            fresh_legal = []
            fresh_rejections = []
            for index, c in enumerate(payload["analyses"][:8]):  # per-round ≤8
                try:
                    validate(ANALYSIS_SCHEMA, c, "analysis")
                    fresh_legal.append((c, index))
                except SpecError as error:
                    fresh_rejections.append(
                        dict(
                            candidate_index=index,
                            code=str(error),
                            candidate_id=f"a{attempt}s{index}",
                            proposed_attempt=attempt,
                            proposed_slot=index,
                        )
                    )

            def _same_analysis(a, b):
                return (
                    a.get("requirement_id") == b.get("requirement_id")
                    and a.get("facet") == b.get("facet")
                    and a.get("operator") == b.get("operator")
                    and a.get("scenario") == b.get("scenario")
                    and a.get("proposal") == b.get("proposal")
                    and list(a.get("premises", []) or []) == list(b.get("premises", []) or [])
                )

            for c, slot in fresh_legal:
                deep = _copy.deepcopy(c)
                if not any(_same_analysis(deep, x) for x in candidates):
                    candidates.append(deep)
                    candidate_origins.append(
                        dict(
                            candidate_id=f"a{attempt}s{slot}",
                            proposed_attempt=attempt,
                            proposed_slot=slot,
                        )
                    )
            candidates = candidates[:24]
            candidate_origins = candidate_origins[:24]

            # Phase 2 — rebuild resolved rows from empty (per-candidate
            # analyses). Legal rows carry evidence_location.premises with
            # the current full covered support group, same Run/project —
            # use that directly for the budget union below.
            ledger_report = ledger.report()
            candidate_rows = []
            rejections = []
            for raw, origin in zip(candidates, candidate_origins):
                rows, rej = analyses(
                    spec,
                    [raw],
                    ledger_report,
                    tuple(fact_pool.values()),
                    offered,
                )
                candidate_rows.extend(
                    replace(row, evidence_location=row.evidence_location | origin) for row in rows
                )
                rejections.extend(
                    issue | origin | dict(candidate_index=origin["proposed_slot"]) for issue in rej
                )

            # Phase 3 — conflict revocation (drop rows whose req_id is
            # now conflicting in the ledger; unrelated req_ids preserved).
            conflicting_ids = {
                item["requirement_id"]
                for item in ledger_report["items"]
                if item.get("answer_polarity") == "conflicting"
                or "conflicting_capability" in (item.get("reason_codes") or [])
            }
            candidate_rows = [
                r
                for r in candidate_rows
                if r.evidence_location["requirement_id"] not in conflicting_ids
            ]

            # Phase 4 — adoption budget at pick time. base is the LATEST
            # ledger's delivered IDs ∪ this round's selected; fact_pool
            # only feeds analyses() as fact-check input, never as budget
            # base. Old analysis_rows is NOT used as base either.
            def _row_resolved_eids(row):
                out = set()
                for p in row.evidence_location.get("premises", []) or []:
                    out.update(p.get("evidence_ids") or [])
                return out

            ledger_delivered_ids: set[str] = set()
            for item in ledger_report["items"]:
                ledger_delivered_ids.update(item.get("evidence_ids") or [])
            base = set(selected) | ledger_delivered_ids

            requested_facets: dict[tuple[str, str], bool] = {}
            for req in spec["requirements"]:
                if req["kind"] == "analysis":
                    for facet in req.get("analysis_facets", []) or []:
                        requested_facets[(req["id"], facet)] = True

            representatives: dict[tuple[str, str], int] = {}
            for idx, row in enumerate(candidate_rows):
                key = (
                    row.evidence_location["requirement_id"],
                    row.evidence_location["facet"],
                )
                if key in requested_facets and key not in representatives:
                    representatives[key] = idx

            adopted_rows: list = []
            used_indices: set[int] = set()
            already_adopted_resolved: set[str] = set()
            budget_rejections: list[dict] = []

            # Representative pass — one per (req_id, facet), preferring
            # oldest legal candidate. Old legal before new legal.
            for key in requested_facets:
                if len(adopted_rows) >= 8:
                    break
                if key not in representatives:
                    continue
                idx = representatives[key]
                if idx in used_indices:
                    continue
                row = candidate_rows[idx]
                union = base | already_adopted_resolved | _row_resolved_eids(row)
                if len(union) > 40:
                    budget_rejections.append(
                        dict(
                            candidate_index=row.evidence_location["proposed_slot"],
                            code="analysis_selection_budget_exhausted",
                            **{
                                k: row.evidence_location[k]
                                for k in ("candidate_id", "proposed_attempt", "proposed_slot")
                            },
                        )
                    )
                    used_indices.add(idx)  # one rejection per candidate this round
                    continue
                adopted_rows.append(row)
                used_indices.add(idx)
                already_adopted_resolved.update(_row_resolved_eids(row))

            # Fill pass — remaining slots in first-appearance order.
            for idx, row in enumerate(candidate_rows):
                if len(adopted_rows) >= 8:
                    break
                if idx in used_indices:
                    continue
                union = base | already_adopted_resolved | _row_resolved_eids(row)
                if len(union) > 40:
                    budget_rejections.append(
                        dict(
                            candidate_index=row.evidence_location["proposed_slot"],
                            code="analysis_selection_budget_exhausted",
                            **{
                                k: row.evidence_location[k]
                                for k in ("candidate_id", "proposed_attempt", "proposed_slot")
                            },
                        )
                    )
                    used_indices.add(idx)  # one rejection per candidate this round
                    continue
                adopted_rows.append(row)
                used_indices.add(idx)
                already_adopted_resolved.update(_row_resolved_eids(row))

            for idx, row in enumerate(candidate_rows):
                if idx not in used_indices and len(adopted_rows) >= 8:
                    budget_rejections.append(
                        dict(
                            code="analysis_delivery_budget_exhausted",
                            **{
                                k: row.evidence_location[k]
                                for k in ("candidate_id", "proposed_attempt", "proposed_slot")
                            },
                        )
                    )

            # Phase 5 — final actual delivered union is a REAL protection,
            # not a trace. Drop rows from the tail until ≤ 40. Legal row
            # EIDs are a subset of the latest ledger delivered IDs, so
            # this only fires when accumulated drift overflows the budget.
            final_delivered: set[str] = set(ledger_delivered_ids)
            for row in adopted_rows:
                final_delivered.update(_row_resolved_eids(row))
            truncated = False
            while len(final_delivered) > 40 and adopted_rows:
                dropped = adopted_rows.pop()
                final_delivered.difference_update(_row_resolved_eids(dropped))
                truncated = True
            if truncated:
                event["analysis_budget_final_truncated"] = True

            analysis_rows = tuple(adopted_rows)
            event["analysis_rejections"] = fresh_rejections + rejections + budget_rejections
            event["schema_status"] = (
                "facts_valid_analysis_rejected"
                if fresh_rejections
                else "fact_and_analysis_shapes_valid"
            )
            current = complete_analysis_items(spec, ledger_report, analysis_rows)
            trace["analysis_gaps"] = [
                i
                for i in current["items"]
                if i.get("analysis_facets") is not None and i["status"] != "covered"
            ]
            if current["overall_completable"]:
                break
            missing = [i["requirement_id"] for i in current["items"] if i["status"] != "covered"]
            fail("support_incomplete", attempt)
            hint = (
                "Missing frozen requirement IDs: "
                + ",".join(missing)
                + ". Preserve legal previous groups; repair only gaps. Select complete same-flow steps/conditions. No completeness ratings."
            )
        except (SpecError, ClaimError) as error:
            code = str(error)
            event["validation_code"] = code
            event["validation_channel"] = "planning" if spec is None else "fact_selection"
            fail(code, attempt)
            hint = (
                "mode=manual|automatic|mixed|unspecified; quantity=single|multiple|unspecified; platform is ONE enum (iPhone, iOS, Android, mobile), never combined. queries.id must reference a requirement id, not q1/q2 anchors. Closed contract error: "
                + code
                + ". Correct the indicated schema-owned field or support group. Preserve the original requirements; do not add facts."
            )
            if code.startswith("analysis_"):
                hint = (
                    "Original requested analysis facets: "
                    + ",".join(
                        sorted(
                            analysis_inventory(
                                context.research_question + " " + (context.scope or "")
                            )
                        )
                    )
                    + ". Use analysis kind only for requested outputs. Include every requested facet. Do not invent analysis for a factual question."
                )
        except (
            api.ResearchLLMTransportError,
            api.ResearchLLMProtocolError,
            api.ResearchLLMConfigError,
        ) as error:
            meta = getattr(error, "response_metadata", None) or {}
            event["response_metadata"] = meta
            code = (
                "protocol_truncated"
                if meta.get("stop_reason") == "max_tokens"
                else "protocol_failure"
                if isinstance(error, api.ResearchLLMProtocolError)
                else "model_transport_failure"
            )
            event["error_type"] = type(error).__name__
            fail(code, attempt)
            usage = meta.get("usage", {})
            if type(usage.get("input_tokens")) is int and type(usage.get("output_tokens")) is int:
                run.input_tokens += (
                    usage["input_tokens"]
                    + usage.get("cache_creation_input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0)
                )
                run.output_tokens += usage["output_tokens"]
            else:
                run.unknown_token_calls += 1
            hint = "Invoke the declared tool exactly once. Return the closed object, no prose or thinking JSON."
        finally:
            run.latency_ms += int((time.monotonic() - started) * 1000)
            checkpoint()
    if ledger is not None:
        report = complete_analysis_items(spec, ledger.report(), analysis_rows)
        ids = list(dict.fromkeys(i for item in report["items"] for i in item["evidence_ids"]))
        # Final support union, not all candidates ever seen, is the persisted fact
        # pool. Unrelated true excerpts remain diagnostics, not delivery facts.
        for row in analysis_rows:
            ids.extend(
                i
                for p in row.evidence_location["premises"]
                for i in p["evidence_ids"]
                if i not in ids
            )
        budget.select(ids)
        kept = [fact_pool[i] for i in ids]
        run.finding_count = _persist_claims(db, run, tuple(kept) + analysis_rows, sources, offered)
        run.coverage_report = report | dict(
            semantic_review="NOT_MEASURED", answer=build_answer(spec, report, kept, analysis_rows)
        )
        run.processed_count = 1
        run.status = "completed" if report["overall_completable"] and kept else "partial_failed"
    else:
        run.status = "partial_failed"
        run.coverage_report = dict(
            version="research-support-v7",
            items=[],
            overall_completable=False,
            semantic_review="NOT_MEASURED",
        )
    run.error_message = (
        None
        if run.status == "completed"
        else "research "
        + (trace["last_failure"] or {"code": "evidence_missing"})["code"]
        + ": original question remains partly unverified"
    )
    run.completed_at = api._now()
    checkpoint()
