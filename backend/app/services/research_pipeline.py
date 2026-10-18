"""Bounded v5 Research execution: retrieval, evidence selections, strict guards, coverage."""

import re
import time
from dataclasses import asdict, replace
from types import ModuleType
from typing import Any

from sqlalchemy.orm import Session

from app.models import ResearchRun
from app.services.research_contract import build_task_plan, evaluate_coverage
from app.services.research_reader import ReadError, read_official
from app.services.research_selection import SelectionError, build_catalogs, materialize

MAX_QUERIES = 3
MAX_READS = 5
MAX_SOURCES = 5
MAX_OFFERED_UNITS = 100


def _allowed(api, rule, url):
    return api._is_official_vendor_host(url, rule) and api._is_official_vendor_path(url, rule)


def _queries(api, context, base, rule):
    if not rule:
        return [base]
    site = "site:" + rule["site"]
    # Search the whole allowed vendor namespace; the unchanged post-filter
    # still enforces every path. In particular Apple /guide is reachable.
    primary = re.sub(r"site:\S+", site, base) if rule["site"] == "support.apple.com" else base
    question = re.split(r"\bCite only\b", context.research_question, flags=re.I)[0].strip()
    focused = f"{question} {site}"[:500]
    plan = build_task_plan(context.research_question)
    missing = " ".join(
        dict.fromkeys(
            i.value for i in plan.items if i.kind in {"product", "platform", "entry", "operation"}
        )
    )
    followup = f"{missing} {site}"[:500]
    raw = list(dict.fromkeys([primary, focused, followup]))
    # Drop queries that are nothing but a ``site:`` filter with no
    # target terms. Such queries do not constrain the search and were
    # the third query of unparseable multi-intent questions (e.g.
    # the Apple #9 "where to upload to a Shared Photo Library" with
    # an iOS-version clause the model could not split).
    filtered = [q for q in raw if not re.fullmatch(r"\s*site:\S+\s*", q)]
    return filtered[:MAX_QUERIES]


def _coverage(plan, catalogs, entries, selected, full_ids):
    reports = []
    for sid, cat in catalogs.items():
        if sid not in full_ids:
            continue
        units = [v["unit"] for v in entries.values() if v["source"].provider_source_id == sid]
        ids = [
            i for i in selected if i in entries and entries[i]["source"].provider_source_id == sid
        ]
        # The procedure proof is bound to one actual section. Do not merge
        # unrelated sections to increase coverage.
        for section in cat.sections:
            scope_text = " / ".join(section.full_path).lower()
            if re.search(r"troubleshoot|workaround|gesture|ios\s*\d", scope_text):
                continue
            group = [i for i in ids if entries[i]["unit"].section_path == section.full_path]
            if group:
                report = evaluate_coverage(plan, units, catalog=cat, selected_evidence_ids=group)
                if report.procedure_status == "complete":
                    selected_units = [entries[i]["unit"] for i in ids]
                    manual = any(
                        re.search(r"\bmanual(?:ly)?\b", " ".join(u.section_path), re.I)
                        for u in selected_units
                        if u.step_index
                    )
                    automatic = [
                        u
                        for u in selected_units
                        if re.search(r"\bautomatic(?:ally)?\b.*\b(?:back ?up|upload)", u.text, re.I)
                    ]
                    if manual and automatic:
                        fixed = tuple(
                            replace(
                                item,
                                status="covered",
                                gap="",
                                evidence_ids=tuple(group + [u.evidence_id for u in automatic]),
                                evidence_source_ids=(sid,),
                            )
                            if item.kind == "distinguish_auto"
                            else item
                            for item in report.items
                        )
                        complete = plan.is_completable_target and all(
                            item.status == "covered" for item in fixed if item.role == "core"
                        )
                        report = replace(
                            report,
                            items=fixed,
                            overall_completable=complete,
                            failure_reasons=tuple(
                                item.gap
                                for item in fixed
                                if item.role == "core" and item.status != "covered"
                            ),
                        )
                reports.append(report)
    if not reports:
        report = asdict(evaluate_coverage(plan, []))
        if selected and plan.is_completable_target:
            report["failure_reasons"] = [
                "official_snapshot_gap: selected facts lack a complete matching official-page snapshot"
            ]
            for item in report["items"]:
                item["gap"] = (
                    "Official-page snapshot unavailable for these selected facts; complete procedure coverage is not verified."
                )
        return report
    best = max(
        reports, key=lambda r: (r.overall_completable, sum(i.status == "covered" for i in r.items))
    )
    return asdict(best)


def _offer(plan, entries):
    def score(pair):
        _, item = pair
        u = item["unit"]
        section = u.section.lower()
        text = u.text.lower()
        value = 0
        for req in plan.items:
            if req.kind in {"operation", "entry"} and req.value in section:
                value += 5
            if req.kind == "multi_select" and re.search(
                r"\b(multiple|several|multi.select|batch)\b", text
            ):
                value += 6
        if "manual" in section or "manually" in section:
            value += 4
        if re.search(r"roaming|troubleshoot|workaround|automatic", section):
            value -= 8
        if u.step_index:
            value += 1
        return value

    ordered = sorted(entries.items(), key=score, reverse=True)[:MAX_OFFERED_UNITS]
    return {i: v for i, v in ordered}


def _raw_guard_failures(api, payload, haystacks, snippets, urls, question):
    failures = []
    for category in ("competitors", "market_patterns"):
        for index, item in enumerate(payload.get(category, [])):
            refs = tuple(
                (r.get("provider_source_id", ""), r.get("url", ""))
                for r in item.get("source_refs", [])
                if isinstance(r, dict)
            )
            name = item.get("name", item.get("title", ""))
            description = item.get("description", "")
            quote = api._extract_evidence_quote(description)
            checks = [
                ("name", api._is_name_supported_by_sources(name, refs, haystacks)),
                ("quote", api._is_evidence_quote_supported(quote, refs, snippets)),
                (
                    "description",
                    api._is_description_grounded_in_snippet(description, refs, snippets, urls),
                ),
                (
                    "quantifier",
                    not api._is_multi_select_question(question)
                    or api._quote_contains_quantifier(quote),
                ),
            ]
            guard = next((n for n, ok in checks if not ok), None)
            if guard:
                failures.append(
                    {"category": category, "candidate_index": index, "first_failed_guard": guard}
                )
    return failures


def execute_run(
    db: Session, run: ResearchRun, *, internal_context: Any, provider_query: str, api: ModuleType
) -> None:
    context = internal_context
    plan = build_task_plan(context.research_question)
    trace = {"version": "research-execution-v5", "events": []}
    run.task_contract = asdict(plan)
    run.coverage_report = asdict(evaluate_coverage(plan, []))
    run.execution_trace = trace
    rule = api._detect_official_vendor_rule(context.research_question, context.scope)
    queries = _queries(api, context, provider_query, rule)
    run.provider_query = queries[0]
    db.commit()
    sources = {}
    full_ids = set()
    reads = 0
    for query_index, query in enumerate(queries):
        provider = None
        event = {"stage": "search", "query": query, "query_index": query_index}
        trace["events"].append(event)
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
            if rule:
                kept = api._filter_sources_to_official_vendor(kept, rule)
            event["source_ids_after_filters"] = [s.provider_source_id for s in kept]
            # Reserve read budget for subsequent missing-task queries.
            quota = min(2 if query_index < len(queries) - 1 else MAX_READS, MAX_READS - reads)
            fresh = [source for source in kept if source.url not in sources]
            for source in fresh[: quota if rule else MAX_SOURCES]:
                if len(sources) >= MAX_SOURCES:
                    break
                if rule and quota > 0 and reads < MAX_READS:
                    reads += 1
                    quota -= 1
                    read_event = {"stage": "official_read", "url": source.url}
                    trace["events"].append(read_event)
                    read_started = time.monotonic()
                    try:
                        snapshot = read_official(source.url, lambda url: _allowed(api, rule, url))
                        # URL after every redirect has passed the same guard.
                        source = replace(source, url=snapshot.url, snippet=snapshot.text)
                        full_ids.add(source.provider_source_id)
                        read_event.update(
                            {
                                "status": "read",
                                "sha256": snapshot.sha256,
                                "byte_count": snapshot.byte_count,
                                "final_url": snapshot.url,
                            }
                        )
                    except ReadError as error:
                        read_event.update(
                            {
                                "status": "not_verified",
                                "code": error.code if hasattr(error, "code") else str(error),
                            }
                        )
                    finally:
                        read_event["latency_ms"] = int((time.monotonic() - read_started) * 1000)
                        run.latency_ms += read_event["latency_ms"]
                sources[source.url] = source
            # If a complete known procedure is already present, stop searching.
            cats, entries = build_catalogs(list(sources.values()))
            candidate = _coverage(plan, cats, entries, list(entries), full_ids)
            if candidate["overall_completable"]:
                break
        except (api.ResearchProviderTransportError, api.ResearchProviderResponseError):
            event["error_code"] = "provider_failure"
        finally:
            run.attempt_count += getattr(provider, "last_attempt_count", 1) if provider else 1
            run.latency_ms += getattr(provider, "last_call_latency_ms", 0) if provider else 0
    result = api.ProviderSearchResult(
        query=run.provider_query, provider="tavily", sources=list(sources.values())
    )
    rows = api._persist_sources(db, run, result)
    run.source_count = len(rows)
    run.execution_trace = dict(trace)
    db.commit()
    if not rows:
        provider_failed = any(
            event.get("error_code") == "provider_failure" for event in trace["events"]
        )
        run.status = "failed" if provider_failed else "partial_failed"
        run.error_message = (
            "research provider_failure: external search unavailable"
            if provider_failed
            else api.INSUFFICIENT_EXTERNAL_EVIDENCE_MESSAGE
        )
        run.completed_at = api._now()
        db.commit()
        return
    allowed_ids = {s.provider_source_id for s in rows}
    urls = {s.provider_source_id: s.url for s in rows}
    haystacks = api._build_source_haystack_map(rows)
    snippets = api._build_source_snippet_map(rows)
    source_db_ids = {s.provider_source_id: s.id for s in rows}
    catalogs, entries = build_catalogs(rows)
    offered = _offer(plan, entries)
    trace["evidence_locations"] = [
        {
            "evidence_id": i,
            "provider_source_id": v["source"].provider_source_id,
            "offset": v["unit"].offset,
            "length": v["unit"].length,
            "section": v["unit"].section,
        }
        for i, v in offered.items()
    ]
    user_message = {
        "internal_context": context.to_dict(),
        "research_question": context.research_question,
        "scope": context.scope,
        "task_contract": asdict(plan),
        "search_results": [
            {
                "provider_source_id": s.provider_source_id,
                "url": s.url,
                "title": s.title,
                "domain": s.domain,
                "snippet": (s.snippet or "")[:4000],
            }
            for s in rows
        ],
        "evidence_catalog": [
            {
                "evidence_id": i,
                "provider_source_id": v["source"].provider_source_id,
                "text": v["unit"].text,
                "section": v["unit"].section,
                "context": v["unit"].context,
                "step_index": v["unit"].step_index,
                "step_total": v["unit"].step_total,
            }
            for i, v in offered.items()
        ],
    }
    best = None
    coverage = asdict(evaluate_coverage(plan, []))
    hint = None
    last_code = "llm_failure"
    product = next((i.value for i in plan.items if i.kind == "product"), None)
    if product is None and rule:
        product = next(iter(rule["product_keywords"]), None)
    started = time.monotonic()
    for attempt in range(api.RESEARCH_LLM_MAX_RETRIES + 1):
        run.attempt_count += 1
        if attempt:
            run.retry_count += 1
        event = {"stage": "llm", "attempt": attempt + 1, "retry_hint": hint}
        trace["events"].append(event)
        try:
            with api.ResearchLLMClient(
                api_key=api.settings.llm_api_key,
                base_url=api.settings.llm_base_url,
                model=api.settings.llm_model,
                timeout_seconds=api.settings.llm_timeout_seconds,
                prompt_module=api._research_prompt,
            ) as client:
                response = client.generate_research(
                    user_message=user_message, max_tokens=8192, retry_hint=hint
                )
            if response.response_metadata is not None:
                event["response_metadata"] = response.response_metadata
            usage = response.usage
            run.input_tokens += usage.input_tokens or 0
            run.output_tokens += usage.output_tokens or 0
            if usage.input_tokens is None or usage.output_tokens is None:
                run.unknown_token_calls += 1
            payload = response.payload
            if isinstance(payload, dict) and set(payload) == {
                "summary",
                "confidence",
                "evidence_ids",
            }:
                payload, selected = materialize(payload, offered, product)
            else:
                # Closed legacy v4 payloads remain parseable, but have no special
                # privilege at the new coverage gate.
                selected = []
            validated = api.validate_research_payload(
                payload,
                allowed_provider_source_ids=allowed_ids,
                allowed_provider_urls=urls,
                source_haystacks=haystacks,
                source_snippets=snippets,
                validate_evidence_quotes=True,
                validate_description_grounding=True,
                research_question=context.research_question,
            )
            event["guard_failures"] = _raw_guard_failures(
                api, payload, haystacks, snippets, urls, context.research_question
            )
            confirmed = [
                f
                for f in validated.findings
                if f.category in api.SOURCED_CATEGORIES and f.status == "confirmed"
            ]
            accepted = []
            for i in selected or list(offered):
                unit = offered[i]["unit"]
                if any(
                    api._extract_evidence_quote(f.description) == unit.text
                    and any(ref[0] == unit.source_id for ref in f.source_refs)
                    for f in confirmed
                ):
                    accepted.append(i)
            coverage = _coverage(plan, catalogs, offered, accepted, full_ids)
            event["selected_evidence_ids"] = selected
            event["coverage"] = coverage
            best = validated
            if (
                api._has_external_confirmation(validated.findings)
                and coverage["overall_completable"]
            ):
                break
            last_code = "coverage_not_verified" if confirmed else "evidence_guard_demoted"
            if not event["guard_failures"] and not plan.is_completable_target:
                break
            missing = [i["requirement_id"] for i in coverage["items"] if i["status"] != "covered"]
            guards = [f["first_failed_guard"] for f in event["guard_failures"]]
            hint = f"[validation code={last_code}] Select only offered exact evidence IDs for the original task. Missing requirements: {','.join(missing)}. Failed guards: {','.join(guards)}. For steps select every numbered step in the matching section; do not invent missing evidence."[
                :500
            ]
        except api.ResearchValidationError as error:
            last_code = error.code
            event["validation_code"] = last_code
            hint = api._build_validation_retry_hint(last_code)
        except SelectionError as error:
            last_code = str(error)
            event["validation_code"] = last_code
            hint = f"[validation code={last_code}] Return summary, confidence, evidence_ids only. Use unique IDs exactly from evidence_catalog."
        except (
            api.ResearchLLMTransportError,
            api.ResearchLLMProtocolError,
            api.ResearchLLMConfigError,
        ) as error:
            last_code = type(error).__name__
            event["error_type"] = last_code
            metadata = getattr(error, "response_metadata", None)
            if metadata:
                event["response_metadata"] = metadata
            usage = metadata.get("usage", {}) if isinstance(metadata, dict) else {}
            if "input_tokens" in usage and "output_tokens" in usage:
                run.input_tokens += (
                    usage["input_tokens"]
                    + usage.get("cache_creation_input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0)
                )
                run.output_tokens += usage["output_tokens"]
            else:
                run.unknown_token_calls += 1
            hint = "[validation code=tool_protocol] Invoke submit_research exactly once with summary, confidence, evidence_ids."
    run.latency_ms += int((time.monotonic() - started) * 1000)
    run.coverage_report = {**coverage, "semantic_review": "NOT_MEASURED"}
    run.execution_trace = dict(trace)
    db.commit()
    if best is not None:
        run.processed_count = 1
        run.finding_count = api._persist_findings(db, run, best.findings, source_db_ids)
    run.status = (
        "completed"
        if best is not None
        and api._has_external_confirmation(best.findings)
        and coverage["overall_completable"]
        else "partial_failed"
    )
    run.error_message = (
        None
        if run.status == "completed"
        else api._safe_error_message(
            f"research insufficient_external_evidence; {last_code}: answer coverage not verified"
        )
    )
    run.completed_at = api._now()
    db.commit()
