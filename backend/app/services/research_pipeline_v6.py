"""Bounded Research v6. Model candidates never certify coverage or facts."""

import hashlib
import re
import time
from dataclasses import asdict, replace

from app.models import ResearchFinding
from app.services import research_pipeline as legacy
from app.services import research_reader
from app.services.research_claims import ClaimError, materialize_analyses, materialize_facts
from app.services.research_contract import build_task_plan, evaluate_coverage
from app.services.research_coverage import evaluate, prepare_plan, question_anchors, serialize_plan
from app.services.research_reader import ReadError
from app.services.research_selection import SelectionError, build_catalogs, materialize
from app.services.research_tasks import TaskContractError
from app.services.research_tasks import build_task_plan as build_tasks_plan


def _safe_query(question):
    return re.sub(r"\bCite only\b[^;.!?]*[.!?]?", "", question, flags=re.I).strip(" ;")[:500]


def _ranked_offer(question, entries):
    words = set(re.findall(r"[a-z0-9]+", question.lower()))

    def score(pair):
        u = pair[1]["unit"]
        body = u.text.lower()
        context = u.context.lower()
        n = len(words & set(re.findall(r"[a-z0-9]+", body + " " + context)))
        n += 8 if u.step_index else 0
        n += 12 if "manual" in context else 0
        n += 10 if re.search(r"^(?:note:|if |tips:|- on a phone)", body) else 0
        if re.search(r"automatic|自动", question, re.I) and "automatic" in body:
            n += 25
        if re.search(r"choices|options|选项|入口", question, re.I) and re.search(
            r",.+\bor\b", body
        ):
            n += 25
        if re.search(r"troubleshoot|roaming|workaround|download", context) and not re.search(
            r"troubleshoot|roaming|workaround|download", question, re.I
        ):
            n -= 30
        return n

    return sorted(entries.items(), key=score, reverse=True)


def _persist_claims(db, run, claims, source_rows, entries):
    sources = {s.provider_source_id: s for s in source_rows}
    fact_ids = {f.id for f in claims if f.claim_kind == "fact" and f.status == "confirmed"}
    for c in claims:
        if c.run_id != run.id or c.project_id != run.project_id:
            raise ClaimError("claim_scope_mismatch")
        if c.claim_kind != "fact" and (
            c.status != "not_verified"
            or c.source_refs
            or not c.supporting_finding_ids
            or not set(c.supporting_finding_ids) <= fact_ids
        ):
            raise ClaimError("claim_support_scope_invalid")
        refs = []
        for pid, url in c.source_refs:
            source = sources.get(pid)
            if source is None or source.run_id != run.id or source.url != url:
                raise ClaimError("claim_source_scope_invalid")
            refs.append({"provider_source_id": pid, "source_id": str(source.id)})
        location = c.evidence_location
        if location and c.evidence_id in entries:
            unit = entries[c.evidence_id]["unit"]
            location = location | {
                "section": unit.section,
                "context": unit.context,
                "step_index": unit.step_index,
                "step_total": unit.step_total,
                "product_key": c.product_key,
            }
        db.add(
            ResearchFinding(
                id=c.id,
                run_id=run.id,
                category=c.category,
                title=c.title,
                description=c.description,
                confidence=c.confidence,
                status=c.status,
                source_refs=refs or None,
                claim_kind=c.claim_kind,
                supporting_finding_ids=[str(i) for i in c.supporting_finding_ids] or None,
                evidence_location=location,
            )
        )
    db.flush()
    return len(claims)


def _demote_facts_missing_full_snapshot(facts, offered, full_ids):
    """v6 channel gate: after :func:`materialize_facts`, demote every
    ``claim_kind == "fact"`` whose underlying unit's source_id is NOT
    in ``full_ids``. ``full_ids`` is the set of provider_source_ids
    whose official snapshot read succeeded inside :func:`fetch`;
    any source that hit :class:`ReadError` and only carries the
    provider's ``snippet`` text is excluded. Demoted facts are
    forced to ``status='not_verified'`` with empty ``source_refs``
    and ``evidence_location['first_failed_guard']`` set to
    ``official_snapshot_unavailable``. Provider snippets NEVER
    stand in for an official 200 response. Existing
    ``first_failed_guard`` codes from prior guard failures are
    preserved untouched — this helper only demotes facts that
    were previously confirmed by :func:`materialize_facts`.
    """
    demoted = []
    for f in facts:
        if f.claim_kind != "fact" or f.status != "confirmed":
            demoted.append(f)
            continue
        entry = offered.get(f.evidence_id)
        if entry is None:
            location = dict(f.evidence_location or {})
            location["first_failed_guard"] = "official_snapshot_unavailable"
            demoted.append(
                replace(
                    f,
                    status="not_verified",
                    source_refs=(),
                    evidence_location=location,
                )
            )
            continue
        if entry["unit"].source_id not in full_ids:
            location = dict(f.evidence_location or {})
            location["first_failed_guard"] = "official_snapshot_unavailable"
            demoted.append(
                replace(
                    f,
                    status="not_verified",
                    source_refs=(),
                    evidence_location=location,
                )
            )
            continue
        demoted.append(f)
    return demoted


def execute_run(db, run, *, internal_context, provider_query, api):
    context = internal_context
    old_plan = build_task_plan(context.research_question)
    trace = {"version": "research-execution-v6", "events": [], "analysis_gaps": []}
    sources = []
    full_ids = set()
    seen_urls = set()
    seen_hashes = {}
    offered = {}
    reads = 0
    searches = 0
    llm_calls = 0
    rule = api._detect_official_vendor_rule(context.research_question, context.scope)
    official = api._question_requests_official(context.research_question, context.scope)
    requested_rules = (
        [
            r
            for r in api._OFFICIAL_VENDOR_RULES.values()
            if any(
                keyword in context.research_question.lower() for keyword in r["product_keywords"]
            )
        ]
        if official
        else []
    )
    rules = [rule] if rule else requested_rules or list(api._OFFICIAL_VENDOR_RULES.values())

    def allowed(url):
        return any(legacy._allowed(api, r, url) for r in rules)

    queued = legacy._queries(api, context, provider_query, rule)
    # Remove only citation instructions, preserving clauses after a semicolon.
    if rule:
        focused = (_safe_query(context.research_question) + " site:" + rule["site"])[:500]
        queued = list(dict.fromkeys([queued[0], focused, *queued[1:]]))[:3]
    else:
        queued = list(dict.fromkeys([provider_query, _safe_query(context.research_question)]))[:3]
    if len(requested_rules) > 1:
        platforms, actions = api._scan_semantic_terms(context.research_question)
        vendor_queries = []
        for requested in requested_rules:
            product = next(
                k for k in requested["product_keywords"] if k in context.research_question.lower()
            )
            vendor_queries.append(
                f"{product} {' '.join(platforms + actions)} site:{requested['site']}"[:500]
            )
        queued = [queued[0], *vendor_queries][:3]
    used = []
    run.task_contract = asdict(old_plan)
    run.coverage_report = asdict(evaluate_coverage(old_plan, []))
    run.execution_trace = dict(trace)
    db.commit()

    def fetch(query, missing):
        nonlocal reads, searches
        if (
            searches >= 3
            or not query.strip()
            or not any(not w.startswith("site:") for w in query.split())
        ):
            return
        searches += 1
        used.append(query)
        event = {
            "stage": "search",
            "query": query,
            "query_index": searches - 1,
            "missing_requirement_ids": missing,
        }
        trace["events"].append(event)
        provider = None
        try:
            with api.ResearchProviderClient(
                api_key=api.settings.tavily_api_key,
                base_url=api.settings.tavily_base_url,
                timeout_seconds=api.settings.tavily_timeout_seconds,
                max_results=api.settings.tavily_max_results,
                max_retries=api.settings.tavily_max_retries,
            ) as provider:
                result = provider.search(query)
            event["sources_before_filters"] = [
                {
                    "provider_source_id": s.provider_source_id,
                    "url": s.url,
                    "title": s.title,
                    "snippet": s.snippet,
                }
                for s in result.sources
            ]
            kept = api._filter_sources_by_low_authority(result.sources)
            phrases = api._extract_phrase_tokens(context.research_question, context.scope)
            if phrases:
                kept = api._filter_sources_by_phrases(kept, phrases)
            if official:
                kept = [s for s in kept if allowed(s.url)]
            elif rule:
                kept = api._filter_sources_to_official_vendor(kept, rule)
            event["source_ids_after_filters"] = [s.provider_source_id for s in kept]
            fresh = []
            quota = min(2 if searches < 3 else 5, 5 - reads)
            for source in kept:
                if source.url in seen_urls:
                    continue
                if len(sources) + len(fresh) >= 5:
                    break
                if any(s.provider_source_id == source.provider_source_id for s in sources + fresh):
                    trace["events"].append(
                        {"stage": "source_guard", "code": "provider_source_id_collision"}
                    )
                    continue
                if allowed(source.url):
                    if quota <= 0:
                        continue
                    reads += 1
                    quota -= 1
                    read_event = {"stage": "official_read", "url": source.url}
                    trace["events"].append(read_event)
                    started = time.monotonic()
                    try:
                        snapshot = research_reader.read_official(source.url, allowed)
                        source = replace(source, url=snapshot.url, snippet=snapshot.text)
                        digest = hashlib.sha256(snapshot.text.encode()).hexdigest()
                        read_event.update(
                            status="read",
                            sha256=digest,
                            byte_count=snapshot.byte_count,
                            final_url=snapshot.url,
                        )
                        identity = (source.domain, digest)
                        if identity in seen_hashes:
                            read_event["deduplicated_to"] = seen_hashes[identity]
                            seen_urls.add(source.url)
                            continue
                        seen_hashes[identity] = source.provider_source_id
                        full_ids.add(source.provider_source_id)
                    except ReadError as error:
                        read_event.update(status="not_verified", code=error.code)
                    finally:
                        elapsed = int((time.monotonic() - started) * 1000)
                        read_event["latency_ms"] = elapsed
                        run.latency_ms += elapsed
                seen_urls.add(source.url)
                fresh.append(source)
            new_rows = api._persist_sources(
                db, run, api.ProviderSearchResult(query=query, provider="tavily", sources=fresh)
            )
            sources.extend(new_rows)
            run.source_count = len(sources)
        except (api.ResearchProviderTransportError, api.ResearchProviderResponseError):
            event["error_code"] = "provider_failure"
        finally:
            run.attempt_count += getattr(provider, "last_attempt_count", 1) if provider else 1
            run.latency_ms += getattr(provider, "last_call_latency_ms", 0) if provider else 0
            run.execution_trace = dict(trace)
            db.commit()

    fetch(queued[0], [])
    while not sources and searches < 3:
        query = next((q for q in queued if q not in used), None)
        if query is None:
            break
        fetch(query, ["usable_official_source"])
    # Multi-product comparisons need inventory from each requested official
    # namespace. A successful first vendor does not satisfy the other vendor.
    while (
        len(requested_rules) > 1
        and searches < 3
        and any(
            not any(legacy._allowed(api, requested, source.url) for source in sources)
            for requested in requested_rules
        )
    ):
        query = next((q for q in queued if q not in used), None)
        if query is None:
            break
        fetch(query, ["requested_official_vendor_inventory"])
    if not sources:
        run.status = (
            "failed"
            if any(e.get("error_code") == "provider_failure" for e in trace["events"])
            else "partial_failed"
        )
        code = "provider_failure" if run.status == "failed" else "insufficient_external_evidence"
        run.error_message = f"research {code}: retrieval_failure; no usable source was obtained; product capability remains unverified"
        run.completed_at = api._now()
        db.commit()
        return
    best = None
    best_score = None
    last_code = "coverage_incomplete"
    hint = None
    pending_search = False
    missing = []
    for attempt in range(3):
        if pending_search and searches < 3 and reads < 5 and len(sources) < 5:
            query = next((q for q in queued if q not in used), None)
            if best and best.get("plan"):
                spans = [t for t in best["plan"].items + best["plan"].unresolved if t.id in missing]
                gap = " ".join(
                    dict.fromkeys(
                        best["plan"].original_question[slice(*t.question_span)] for t in spans
                    )
                )
                subject = " ".join(
                    dict.fromkeys(t.subject.product or "" for t in best["plan"].items)
                )
                candidate = f"{subject} {gap}" + (" site:" + rule["site"] if rule else "")
                candidate = candidate.strip()[:500]
                if gap and candidate not in used:
                    query = candidate
            if query:
                fetch(query, missing)
        catalogs, entries = build_catalogs(sources)
        limit = (50, 85, 100)[attempt]
        for eid, entry in _ranked_offer(context.research_question, entries):
            if len(offered) >= limit:
                break
            if eid not in offered:
                offered[eid] = entry
        trace["evidence_locations"] = [
            {
                "evidence_id": i,
                "provider_source_id": v["unit"].source_id,
                "offset": v["unit"].offset,
                "length": v["unit"].length,
                "section": v["unit"].section,
            }
            for i, v in offered.items()
        ]
        message = {
            "internal_context": context.to_dict(),
            "research_question": context.research_question,
            "scope": context.scope,
            "task_contract": {
                "version": "research-tasks-v6",
                "question": context.research_question,
                "parse_status": "planning_pending",
                "items": [],
                "parse_notes": [],
            },
            "question_anchors": question_anchors(context.research_question),
            "search_results": [
                {
                    "provider_source_id": s.provider_source_id,
                    "url": s.url,
                    "title": s.title,
                    "domain": s.domain,
                    "snippet": (s.snippet or "")[:4000],
                }
                for s in sources
            ],
            "evidence_catalog": [
                {
                    "evidence_id": i,
                    "provider_source_id": v["unit"].source_id,
                    "text": v["unit"].text,
                    "section": v["unit"].section,
                    "context": v["unit"].context,
                    "step_index": v["unit"].step_index,
                    "step_total": v["unit"].step_total,
                }
                for i, v in sorted(
                    offered.items(),
                    key=lambda pair: (
                        next(
                            n
                            for n, source in enumerate(sources)
                            if source.provider_source_id == pair[1]["unit"].source_id
                        ),
                        pair[1]["unit"].offset,
                        pair[1]["unit"].length,
                    ),
                )
            ],
        }
        event = {"stage": "llm", "attempt": attempt + 1, "retry_hint": hint}
        trace["events"].append(event)
        run.attempt_count += 1
        llm_calls += 1
        if attempt:
            run.retry_count += 1
        started = time.monotonic()
        pending_search = False
        try:
            with api.ResearchLLMClient(
                api_key=api.settings.llm_api_key,
                base_url=api.settings.llm_base_url,
                model=api.settings.llm_model,
                timeout_seconds=api.settings.llm_timeout_seconds,
                prompt_module=api._research_prompt,
            ) as client:
                response = client.generate_research(
                    user_message=message, max_tokens=8192, retry_hint=hint
                )
            event["response_metadata"] = response.response_metadata
            run.input_tokens += response.usage.input_tokens or 0
            run.output_tokens += response.usage.output_tokens or 0
            if response.usage.input_tokens is None or response.usage.output_tokens is None:
                run.unknown_token_calls += 1
            payload = response.payload
            contract_failure = None
            if isinstance(payload, dict) and set(payload) == {
                "tasks",
                "evidence_ids",
                "task_bindings",
                "analyses",
            }:
                if not isinstance(payload["analyses"], list) or len(payload["analyses"]) > 8:
                    raise ClaimError("analysis_budget_invalid")
                plan = None
                bindings_for_coverage = payload["task_bindings"]
                try:
                    plan = prepare_plan(context.research_question, payload["tasks"])
                except TaskContractError as error:
                    # Model produced a payload that violates the v6
                    # task contract. The contract reason must drive the
                    # retry hint and the final error_message verbatim,
                    # not be downgraded to a generic coverage gap.
                    # Continue validating the evidence side so any
                    # legitimate facts / analyses that came back on the
                    # same call are still strictly checked and persisted
                    # independently of the failed contract.
                    contract_failure = {
                        "code": getattr(error, "code", str(error)),
                        "detail": getattr(error, "detail", ""),
                    }
                    event["task_contract_failure"] = contract_failure
                    plan = build_tasks_plan(context.research_question, [])
                    bindings_for_coverage = []
                facts = materialize_facts(
                    payload["evidence_ids"],
                    offered,
                    research_question=context.research_question,
                    run_id=run.id,
                    project_id=run.project_id,
                    api=api,
                )
                facts = list(_demote_facts_missing_full_snapshot(facts, offered, full_ids))
                analysis_rows = []
                rejections = []
                for index, candidate in enumerate(payload["analyses"]):
                    try:
                        analysis_rows.extend(
                            materialize_analyses(
                                [candidate],
                                [f for f in facts if f.status == "confirmed"],
                                run_id=run.id,
                                project_id=run.project_id,
                            )
                        )
                    except ClaimError as error:
                        rejections.append(
                            {
                                "candidate_index": index,
                                "category": candidate.get("category")
                                if isinstance(candidate, dict)
                                else None,
                                "code": str(error),
                            }
                        )
                coverage = evaluate(plan, bindings_for_coverage, facts, offered, catalogs, full_ids)
                event["guard_failures"] = [
                    {
                        "candidate_index": i,
                        "first_failed_guard": f.evidence_location.get(
                            "first_failed_guard", "evidence_not_verified"
                        ),
                    }
                    for i, f in enumerate(facts)
                    if f.status != "confirmed"
                ]
                event["analysis_rejections"] = rejections
                event["selected_evidence_ids"] = payload["evidence_ids"]
                event["coverage"] = coverage
                current = {
                    "plan": plan,
                    "contract": serialize_plan(plan),
                    "coverage": coverage,
                    "claims": tuple(facts) + tuple(analysis_rows),
                    "legacy": None,
                    "entries": dict(offered),
                    "analyses": rejections,
                }
                confirmed = sum(f.status == "confirmed" for f in facts)
            else:
                # Genuine closed historical payloads retain their strict v14
                # channel. They gain no v6 structured-quote privilege.
                selected = []
                if isinstance(payload, dict) and set(payload) == {
                    "summary",
                    "confidence",
                    "evidence_ids",
                }:
                    product = next((i.value for i in old_plan.items if i.kind == "product"), None)
                    payload, selected = materialize(payload, offered, product)
                urls = {s.provider_source_id: s.url for s in sources}
                snippets = api._build_source_snippet_map(sources)
                haystacks = api._build_source_haystack_map(sources)
                validated = api.validate_research_payload(
                    payload,
                    allowed_provider_source_ids=set(urls),
                    allowed_provider_urls=urls,
                    source_haystacks=haystacks,
                    source_snippets=snippets,
                    validate_evidence_quotes=True,
                    validate_description_grounding=True,
                    research_question=context.research_question,
                )
                confirmed_facts = [
                    f
                    for f in validated.findings
                    if f.status == "confirmed" and f.category in api.SOURCED_CATEGORIES
                ]
                accepted = [
                    i
                    for i in selected or list(offered)
                    if any(
                        api._extract_evidence_quote(f.description) == offered[i]["unit"].text
                        and any(r[0] == offered[i]["unit"].source_id for r in f.source_refs)
                        for f in confirmed_facts
                    )
                ]
                coverage = legacy._coverage(old_plan, catalogs, offered, accepted, full_ids)
                event["protocol_channel"] = "closed_legacy"
                event["guard_failures"] = legacy._raw_guard_failures(
                    api, payload, haystacks, snippets, urls, context.research_question
                )
                event["coverage"] = coverage
                current = {
                    "plan": None,
                    "contract": asdict(old_plan),
                    "coverage": coverage,
                    "claims": (),
                    "legacy": validated,
                    "entries": dict(offered),
                    "analyses": [],
                }
                confirmed = len(confirmed_facts)
            score = (
                coverage["overall_completable"],
                sum(i["status"] == "covered" for i in coverage["items"] if i["role"] == "core"),
                confirmed,
                sum(c.claim_kind in {"inference", "recommendation"} for c in current["claims"]),
            )
            if best_score is None or score > best_score:
                best, best_score = current, score
            if coverage["overall_completable"] and confirmed:
                break
            missing = [
                i["requirement_id"]
                for i in coverage["items"]
                if i["role"] == "core" and i["status"] != "covered"
            ]
            if contract_failure is not None:
                # Preserve the original task-contract reason for both the
                # retry hint and the final run.error_message; do not
                # silently reframe it as a generic coverage gap.
                last_code = contract_failure["code"]
                hint = (
                    f"[validation code={last_code}] {contract_failure['detail']} "
                    "Each task must reference an existing question anchor and use a "
                    "kind from allowed_kinds; one task per anchor, do not self-rate "
                    "completion; return exactly tasks,evidence_ids,task_bindings,analyses."
                )[:500]
            else:
                last_code = "coverage_incomplete" if confirmed else "insufficient_external_evidence"
                hint = f"[validation code={last_code}] Missing task IDs: {','.join(missing)}. Keep every original condition. Select all exact steps plus relevant options/conditions; never invent evidence. Return exactly tasks,evidence_ids,task_bindings,analyses."[
                    :500
                ]
            # Inventory completeness is evaluated against saved original units,
            # independently of the model's selection. Retry a selection gap
            # without spending another search when the evidence is already here.
            inventory = legacy._coverage(old_plan, catalogs, entries, list(entries), full_ids)
            pending_search = not inventory["overall_completable"]
            if current["legacy"] is not None and not old_plan.is_completable_target:
                break
        except (
            ClaimError,
            TaskContractError,
            SelectionError,
            api.ResearchValidationError,
        ) as error:
            last_code = getattr(error, "code", str(error))
            event["validation_code"] = last_code
            hint = f"[validation code={last_code}] {getattr(error, 'detail', '')} Correct the closed schema; use one task per question anchor and a kind from allowed_kinds. Do not self-rate completion. Use only offered IDs; analyses need confirmed facts."[
                :500
            ]
        except (
            api.ResearchLLMTransportError,
            api.ResearchLLMProtocolError,
            api.ResearchLLMConfigError,
        ) as error:
            meta = getattr(error, "response_metadata", None) or {}
            event["response_metadata"] = meta
            last_code = (
                "protocol_truncated"
                if meta.get("stop_reason") == "max_tokens"
                else "protocol_failure"
                if isinstance(error, api.ResearchLLMProtocolError)
                else "model_transport_failure"
            )
            error_type = type(error).__name__
            event["error_type"] = error_type
            last_code = f"{last_code}:{error_type}"
            event["failure_category"] = last_code
            usage = meta.get("usage", {})
            if "input_tokens" in usage and "output_tokens" in usage:
                run.input_tokens += (
                    usage["input_tokens"]
                    + usage.get("cache_creation_input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0)
                )
                run.output_tokens += usage["output_tokens"]
            else:
                run.unknown_token_calls += 1
            hint = f"[validation code={last_code}] Invoke submit_research exactly once with tasks,evidence_ids,task_bindings,analyses. No summary, prose answer, or completeness rating."
        finally:
            run.latency_ms += int((time.monotonic() - started) * 1000)
            run.execution_trace = dict(trace)
            db.commit()
    trace["budgets"] = {
        "logical_searches": searches,
        "official_reads": reads,
        "sources": len(sources),
        "offered_unique_evidence_units": len(offered),
        "llm_calls": llm_calls,
    }
    if best:
        run.task_contract = best["contract"]
        run.coverage_report = best["coverage"] | {"semantic_review": "NOT_MEASURED"}
        run.processed_count = 1
        if best["legacy"] is not None:
            run.finding_count = api._persist_findings(
                db, run, best["legacy"].findings, {s.provider_source_id: s.id for s in sources}
            )
        else:
            run.finding_count = _persist_claims(db, run, best["claims"], sources, best["entries"])
            summary = api.ValidatedFinding(
                "summary",
                "内部上下文",
                ("内部上下文：" + context.insight_title)[:600],
                None,
                "not_verified",
                (),
            )
            run.finding_count += api._persist_findings(
                db, run, (summary,), {s.provider_source_id: s.id for s in sources}
            )
            present = {
                c.category
                for c in best["claims"]
                if c.claim_kind in {"inference", "recommendation"}
            }
            trace["analysis_gaps"] = [
                {
                    "category": category,
                    "reason": "candidate_support_rejected"
                    if any(r.get("category") == category for r in best["analyses"])
                    else "no_supported_candidate",
                    "codes": [r["code"] for r in best["analyses"] if r.get("category") == category],
                }
                for category in ("market_pattern", "opportunity", "risk")
                if category not in present
            ]
        run.status = (
            "completed"
            if best["coverage"]["overall_completable"] and best_score[2] > 0
            else "partial_failed"
        )
    else:
        run.status = "partial_failed"
    run.error_message = (
        None
        if run.status == "completed"
        else api._safe_error_message(
            f"research {last_code}: original question remains partly unverified"
        )
    )
    run.execution_trace = dict(trace)
    run.completed_at = api._now()
    db.commit()
