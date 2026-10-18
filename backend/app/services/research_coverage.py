"""Server evidence predicates for anchored v6 task candidates, not model ratings."""

import re
from dataclasses import asdict, replace

from app.services.research_contract import _eval_procedure
from app.services.research_tasks import TaskContractError, _normal, _unresolved, build_task_plan


def normalize(value):
    # Matching-only language aliases; citation bytes and offsets remain original.
    text = str(value).replace("已有", " existing ").replace("现有", " existing ")
    return _normal(text).replace("back up", "backup")


def _eligible_kinds(text):
    cues = {
        "procedure": r"\b(?:steps?|procedure|workflow|how)\b|步骤|如何|怎么|流程",
        "capability": r"\b(?:can|does|supports?|possible|whether)\b|能否|是否|支持|可否|可以",
        "entry_listing": r"\b(?:choices?|options?)\b|选项|入口",
        "destination": r"\bwhere\b|哪里|何处|去向|上传到",
        "comparison": r"\b(?:distinguish|separate|compare|difference)\b|区分|区别|比较|分开",
        "limit": r"\b(?:limits?|maximum|restrictions?)\b|限制|上限|最多",
        "version": r"\b(?:version|ios|ipados|android|macos)\b|版本|首次",
    }
    return [kind for kind, cue in cues.items() if re.search(cue, text, re.I)] or [
        "analysis",
        "condition",
        "restriction",
    ]


def question_anchors(question):
    """Exact source-independent clauses. IDs authorize original text only."""
    source_prefix = re.compile(
        r"^\s*(?:(?:according to|per|using only|based on)\b[^,，:：]*[,，:：]\s*|"
        r"请?(?:仅|只)(?:引用|根据|依据)[^:：，,]*[:：，,]\s*(?:说明\s*)?)",
        re.I,
    )
    result = []
    # Dots inside domains and version numbers are not sentence boundaries.
    boundaries = list(
        re.finditer(
            r"[?？;；。!！\n]|\.(?=\s|$)|,?\s+and\s+(?=(?:where|which|what|how|whether|state|list|explain)\b)|[，,]\s*(?=并(?:区分|比较))",
            question,
            re.I,
        )
    )
    start = 0
    for match in boundaries + [None]:
        end = match.start() if match else len(question)
        a, b = start, end
        while a < b and question[a].isspace():
            a += 1
        while b > a and question[b - 1].isspace():
            b -= 1
        segment = question[a:b]
        prefix = source_prefix.match(segment)
        if prefix:
            a += prefix.end()
            segment = question[a:b]
        if segment and not re.match(r"^(?:cite only|请?仅引用|仅引用)\b", segment, re.I):
            result.append(
                dict(
                    id=f"question-{len(result) + 1}",
                    text=segment,
                    start=a,
                    end=b,
                    allowed_kinds=_eligible_kinds(segment),
                )
            )
        start = match.end() if match else len(question)
    return result


def prepare_plan(question, tasks):
    if not isinstance(tasks, list) or len(tasks) > 16:
        raise TaskContractError("task_candidates_invalid")
    converted = []
    anchors = {a["id"]: a for a in question_anchors(question)}
    anchored = any(isinstance(t, dict) and "question_anchor_id" in t for t in tasks)
    used_anchors = set()
    for task_index, task in enumerate(tasks):
        if isinstance(task, dict) and "question_anchor_id" in task:
            anchor = anchors.get(task["question_anchor_id"])
            if anchor is None or "question_quote" in task:
                raise TaskContractError("task_anchor_id_invalid")
            used_anchors.add(anchor["id"])
            task = {k: v for k, v in task.items() if k != "question_anchor_id"} | {
                "question_quote": anchor["text"]
            }
        if not isinstance(task, dict) or "question_quote" not in task:
            raise TaskContractError("task_quote_missing")
        quote = task["question_quote"]
        if not isinstance(quote, str) or not quote or question.count(quote) != 1:
            raise TaskContractError("task_quote_not_unique_exact")
        start = question.index(quote)
        if task.get("role") != "core":
            raise TaskContractError("v6_tasks_must_preserve_core_requirements")
        cues = {
            "procedure": r"\b(?:steps?|procedure|workflow|how)\b|步骤|如何|怎么|流程",
            "capability": r"\b(?:can|does|supports?|possible|whether)\b|能否|是否|支持|可否|可以",
            "entry_listing": r"\b(?:choices?|options?)\b|选项|入口",
            "destination": r"\bwhere\b|哪里|何处|去向|上传到",
            "comparison": r"\b(?:distinguish|separate|compare|difference)\b|区分|区别|比较|分开",
            "limit": r"\b(?:limits?|maximum|restrictions?)\b|限制|上限|最多",
            "version": r"\b(?:version|ios|ipados|android|macos)\b|版本|首次",
        }
        cue = cues.get(task.get("kind"))
        if cue and not re.search(cue, quote, re.I):
            raise TaskContractError(
                "task_kind_not_in_question_anchor",
                f"tasks[{task_index}] kind={task.get('kind')}. Use a kind allowed by this question anchor; remove redundant tasks for the same anchor. A can/whether question is capability, not an extra procedure.",
            )
        if re.search(r"[?？;；].+\w", quote):
            raise TaskContractError("mixed_question_anchor")
        if re.search(r"\band\s+(?:where|which|what|state)\b|以及.*(?:哪里|选项|限制)", quote, re.I):
            raise TaskContractError("mixed_question_anchor")
        converted.append(
            {k: v for k, v in task.items() if k != "question_quote"}
            | {"question_span": {"start": start, "end": start + len(quote)}}
        )
    plan = build_task_plan(question, converted)
    if anchored:
        for aid, anchor in anchors.items():
            if aid not in used_anchors:
                plan.unresolved.append(
                    _unresolved(
                        question, (anchor["start"], anchor["end"]), len(plan.unresolved) + 1
                    )
                )
    return plan


def _has(text, phrases):
    return any(p in normalize(text) for p in phrases)


def _scope(task, units, entries):
    for u in units:
        src = entries[u.evidence_id]["source"]
        body = normalize(" ".join([src.title or "", src.url, u.context, u.text]))
        product = normalize(task.subject.product or "")
        if product and any(
            t not in body for t in re.findall(r"[a-z0-9]+", product) if t not in {"apple", "google"}
        ):
            return False
        platform = normalize(task.subject.platform or "")
        aliases = {
            "iphone": ("iphone", "ios", "phone"),
            "ios": ("ios", "iphone", "ipad"),
            "phone or tablet": ("phone", "tablet", "ios", "iphone", "ipad"),
            "mobile": ("mobile", "phone", "android", "ios"),
        }
        if platform and not any(t in body for t in aliases.get(platform, (platform,))):
            # Common parent steps inherit only a selected, confirmed branch in
            # the SAME saved source section. A different page/section cannot
            # lend its phone label to unrelated computer instructions.
            peers = " ".join(
                p.text
                for p in units
                if p.source_id == u.source_id and p.section_path == u.section_path
            ).lower()
            if not any(t in peers for t in aliases.get(platform, (platform,))):
                return False
        entry = normalize(task.subject.entry or "")
        if entry and entry != "app" and not all(t in body for t in entry.split()):
            return False
    return bool(units)


def _action(task, units):
    action = normalize(task.action)
    concepts = [
        c for c in ("backup", "upload", "select", "sync", "share", "move", "delete") if c in action
    ]
    if not concepts:
        return False
    text = normalize(" ".join(u.text + " " + u.context for u in units))
    return all(c in text for c in concepts if c not in {"select"})


def _bad_flow(units):
    return any(_has(u.context, ("roaming", "troubleshoot", "workaround", "gesture")) for u in units)


def _procedure(task, units, catalogs, entries, anchor):
    if _bad_flow(units):
        return False
    if len({u.source_id for u in units}) != 1 or len({u.section_path for u in units}) != 1:
        return False
    cat = catalogs.get(units[0].source_id) if units else None
    if cat is None:
        return False
    status, *_ = _eval_procedure(cat, tuple(units))
    if status != "covered" or not _action(task, units):
        return False
    source = entries[units[0].evidence_id]["source"]
    snapshot = source.snippet or ""
    section = next((s for s in cat.sections if s.full_path == units[0].section_path), None)
    if section is None:
        return False
    wants_existing = "existing" in normalize(anchor).split()
    found_requested_branch = False
    offset = section.start_offset
    for raw in snapshot[section.start_offset : section.end_offset].splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        branch = re.match(r"^\s*(?:[-*]\s*)?(?:if\b|when\b|如果|若)", line, re.I)
        relevant = branch and (
            (wants_existing and "existing" in normalize(line).split())
            or (
                offset > min(u.offset for u in units if u.step_index)
                and any(
                    u.offset < offset + len(line) and offset < u.offset + u.length for u in units
                )
            )
        )
        if relevant:
            found_requested_branch |= wants_existing and "existing" in normalize(line).split()
            content_start = re.match(r"^\s*(?:[-*]\s*)?", line).end()
            # Every nonblank character of the original conditional line must
            # be carried by selected confirmed spans. No textual concatenation
            # or approximate quote matching is used as evidence.
            for pos in range(content_start, len(line)):
                if not line[pos].isspace() and not any(
                    u.offset <= offset + pos < u.offset + u.length for u in units
                ):
                    return False
        offset += len(raw)
    return not wants_existing or found_requested_branch


def _capability_backed_by_procedure(task, units, catalogs):
    """For a ``capability`` task, the catalog section that supplies
    the proof is the SAME source/section as the selected units. When
    that section contains a numbered procedure in the saved
    snapshot, the capability is only proven if the selected
    confirmed same-run units cover the full step sequence — not
    merely a conditional note lifted out of context. Sections
    without numbered steps fall back to the existing capability
    gate (no change). Internal quote / condition / quantity /
    official-guard predicates remain untouched.
    """
    if not units:
        return True
    src_ids = {u.source_id for u in units}
    section_paths = {tuple(u.section_path) for u in units}
    if len(src_ids) != 1 or len(section_paths) != 1:
        return False
    source_id = next(iter(src_ids))
    section_path = next(iter(section_paths))
    cat = catalogs.get(source_id)
    if cat is None:
        return True
    section_step_ids = {
        u.catalog_id for u in cat.units if u.step_index and tuple(u.section_path) == section_path
    }
    if not section_step_ids:
        return True
    selected_step_ids = {u.catalog_id for u in units if u.step_index}
    return section_step_ids <= selected_step_ids


def _comparison_supplement_evidence(task, plan, bindings, facts, entries, catalogs, full_ids):
    """For a ``comparison`` task, locate same-run core ``procedure``
    tasks whose explicit non-empty ``product`` and ``platform`` equal
    the comparison's (None / missing never match — ``None == None``
    cannot rescue a comparison). For each candidate, run a
    procedure-only :func:`evaluate` on a plan built with
    ``dataclasses.replace(plan, items=(other,))`` (the original
    ``unresolved`` clauses are preserved so the same condition-proof
    gate applies) and only the candidate's own binding. The
    procedure is considered independently complete only when
    ``overall_completable`` is True, which exercises every gate:
    scope, action, nested-branch completeness, quantity / mode /
    version / device_scope / distinguish_auto constraints, plus the
    unresolved-condition proof.

    Every ID in the candidate's binding must already be
    ``status == "confirmed"`` AND backed by a full-text source;
    filtering failed IDs and reusing the surviving fragment is
    forbidden. Cross-source, cross-product, or only-partial manual
    procedures never supplement.
    """
    if task.kind != "comparison":
        return []
    task_product = task.subject.product
    task_platform = task.subject.platform
    if not task_product or not task_platform:
        return []
    by_id = {f.evidence_id: f for f in facts if f.claim_kind == "fact"}
    extras: list[str] = []
    seen: set[str] = set()
    for other in plan.items:
        if other.id == task.id:
            continue
        if other.kind != "procedure" or other.role != "core":
            continue
        other_product = other.subject.product
        other_platform = other.subject.platform
        if not other_product or not other_platform:
            continue
        if other_product != task_product or other_platform != task_platform:
            continue
        other_binding = next((b for b in bindings if b["task_id"] == other.id), None)
        if not other_binding:
            continue
        binding_ids = other_binding["evidence_ids"]
        if not binding_ids:
            continue
        if not all(
            by_id.get(i)
            and by_id[i].status == "confirmed"
            and entries[i]["unit"].source_id in full_ids
            for i in binding_ids
        ):
            continue
        sub_plan = replace(plan, items=(other,))
        sub_report = evaluate(
            sub_plan,
            [other_binding],
            list(by_id.values()),
            entries,
            catalogs,
            full_ids,
        )
        if not sub_report["overall_completable"]:
            continue
        for i in binding_ids:
            if i not in seen:
                seen.add(i)
                extras.append(i)
    return extras


def _comparison(units):
    manual = [u for u in units if _has(u.context, ("manual", "手动")) and u.step_index]
    auto = [
        u
        for u in units
        if re.search(r"\bautomatic(?:ally)?\b|自动", u.text, re.I)
        and _has(u.text, ("backup", "back up", "upload", "save", "备份", "上传"))
        and not _has(u.context, ("roaming", "mobile data", "daily limit"))
    ]
    return bool(manual and auto)


def _negative_state(text):
    return bool(
        re.search(
            r"disabled|\boff\b|without|not enabled|don.t have.{0,60}enabled|禁用|关闭|未启用|未开启",
            text,
            re.I,
        )
    )


def _condition_proof(clause, facts):
    # An explicit negative state is proven only by an explicit equivalent
    # negative state on the same named feature. Unknown predicates remain gaps.
    if not _negative_state(clause):
        return []
    words = set(re.findall(r"[a-z]+", normalize(clause))) - {
        "when",
        "if",
        "while",
        "are",
        "is",
        "the",
        "a",
        "an",
        "device",
        "users",
        "user",
        "have",
        "has",
        "disabled",
        "enabled",
        "off",
        "without",
        "not",
        "and",
    }
    words = {w.rstrip("s") for w in words}
    if not words:
        return []
    matched = []
    for f in facts:
        if f.status != "confirmed" or not _negative_state(f.description):
            continue
        present = {w.rstrip("s") for w in re.findall(r"[a-z]+", normalize(f.description))}
        if words <= present:
            matched.append(f.evidence_id)
    return matched


def evaluate(plan, bindings, facts, entries, catalogs, full_ids):
    if not isinstance(bindings, list) or len(bindings) > 16:
        raise TaskContractError("binding_schema")
    by_id = {f.evidence_id: f for f in facts if f.claim_kind == "fact"}
    tasks = {t.id: t for t in plan.items}
    bound = {}
    for b in bindings:
        if not isinstance(b, dict) or set(b) != {"task_id", "evidence_ids"}:
            raise TaskContractError("binding_schema")
        tid = b["task_id"]
        ids = b["evidence_ids"]
        if (
            tid not in tasks
            or tid in bound
            or not isinstance(ids, list)
            or len(ids) > 40
            or len(ids) != len(set(ids))
            or any(i not in by_id for i in ids)
        ):
            raise TaskContractError("binding_evidence_invalid")
        bound[tid] = ids
    report = []
    accepted = [
        f
        for f in facts
        if f.status == "confirmed"
        and f.evidence_location
        and f.evidence_id in entries
        and entries[f.evidence_id]["unit"].source_id in full_ids
    ]
    condition_proofs = []
    for u in plan.unresolved:
        ids = _condition_proof(plan.original_question[slice(*u.question_span)], accepted)
        if ids:
            condition_proofs.append((u.question_span, ids))
    for task in plan.items:
        ids = bound.get(task.id, [])
        valid = [
            i
            for i in ids
            if by_id[i].status == "confirmed" and entries[i]["unit"].source_id in full_ids
        ]
        units = [entries[i]["unit"] for i in valid]
        scope = _scope(task, units, entries)
        ok = False
        gap = "证据尚未覆盖这一要求"
        if scope and len(valid) == len(ids):
            if task.kind == "procedure":
                ok = _procedure(
                    task,
                    units,
                    catalogs,
                    entries,
                    plan.original_question[slice(*task.question_span)],
                )
            elif task.kind == "capability":
                ok = (
                    _action(task, units)
                    and not _bad_flow(units)
                    and any(
                        re.search(r"\bcan\b|\byou can\b|可以|能够", u.text, re.I)
                        or (
                            re.search(
                                r"\b(?:multiple|several|multi[- ]select|batch)\b", u.text, re.I
                            )
                            and re.search(r"\b(?:tap|touch|select|choose)\b", u.text, re.I)
                        )
                        for u in units
                    )
                )
                # If the official answer supplies a procedure in that same
                # section, all its steps are part of the capability proof.
                step_units = [u for u in units if u.step_index]
                if step_units:
                    ok = ok and _procedure(
                        task,
                        units,
                        catalogs,
                        entries,
                        plan.original_question[slice(*task.question_span)],
                    )
                else:
                    # Selected sentence-level units cannot by themselves
                    # prove a capability when the same catalog section
                    # also carries the numbered procedure: the procedure
                    # steps must be carried by confirmed same-run units.
                    ok = ok and _capability_backed_by_procedure(task, units, catalogs)
            elif task.kind == "comparison":
                supplement_ids = _comparison_supplement_evidence(
                    task,
                    plan,
                    bindings,
                    facts,
                    entries,
                    catalogs,
                    full_ids,
                )
                compare_units = list(units)
                if supplement_ids:
                    existing_unit_ids = {u.evidence_id for u in compare_units}
                    for i in supplement_ids:
                        if i not in existing_unit_ids:
                            compare_units.append(entries[i]["unit"])
                            existing_unit_ids.add(i)
                ok = _comparison(compare_units)
                if ok and supplement_ids:
                    valid = list(dict.fromkeys(valid + supplement_ids))
            elif task.kind == "entry_listing":
                ok = _action(task, units) and any(
                    re.search(r",.+\bor\b|、.+或", u.text, re.I) for u in units
                )
            elif task.kind == "destination":
                ok = _action(task, units) and any(
                    re.search(r"\b(?:use|open|go to)\b|使用|打开|前往", u.text, re.I) for u in units
                )
            elif task.kind in {"condition", "restriction"}:
                ok = bool(
                    _condition_proof(
                        plan.original_question[slice(*task.question_span)],
                        [by_id[i] for i in valid],
                    )
                )
            elif task.kind == "version":
                anchor = plan.original_question[slice(*task.question_span)]
                versions = re.findall(
                    r"\b(?:ios|android|ipados|macos)\s*\d+(?:\.\d+)*", anchor, re.I
                )
                ok = bool(versions) and all(
                    any(v.lower() in u.text.lower() for u in units) for v in versions
                )
            elif task.kind == "limit":
                ok = any(
                    re.search(
                        r"\b(?:maximum|limit|restriction|up to)\b|上限|限制|最多", u.text, re.I
                    )
                    for u in units
                )
            else:
                gap = "此要求需要人工语义复核，程序尚不能证明完整覆盖"
        for c in task.constraints:
            if c.kind == "quantity" and c.value == "multiple":
                ok = ok and any(
                    re.search(
                        r"\b(?:multiple|several|multi[- ]select|batch)\b|多张|多选|批量",
                        u.text,
                        re.I,
                    )
                    for u in units
                )
            elif c.kind == "distinguish_auto":
                ok = ok and _comparison([entries[f.evidence_id]["unit"] for f in accepted])
            elif c.kind == "mode" and normalize(c.value) in {"manual", "automatic"}:
                wanted = normalize(c.value)
                ok = ok and any(_has(u.text + " " + u.context, (wanted,)) for u in units)
            elif c.kind in {"condition", "mode", "unknown"}:
                ok = ok and bool(condition_proofs)
            elif c.kind == "version":
                ok = False
                gap = "版本条件须有逐项原文证明，当前仍需复核"
            elif c.kind == "device_scope":
                ok = ok and scope
            elif c.kind == "quantity" and c.value not in {"single"}:
                ok = False
        report.append(
            {
                "requirement_id": task.id,
                "role": task.role,
                "kind": task.kind,
                "status": "covered" if ok else "partial" if valid else "not_verified",
                "evidence_ids": valid,
                "evidence_source_ids": list(
                    dict.fromkeys(entries[i]["unit"].source_id for i in valid)
                ),
                "gap": "" if ok else gap,
            }
        )
    seen = {x["requirement_id"] for x in report}
    for task in plan.unresolved:
        if task.id in seen:
            continue
        ids = next(
            (
                ids
                for (a, b), ids in condition_proofs
                if a <= task.question_span[0] and task.question_span[1] <= b
            ),
            [],
        )
        report.append(
            {
                "requirement_id": task.id,
                "role": "core",
                "kind": "unresolved_clause",
                "status": "covered" if ids else "not_verified",
                "evidence_ids": ids,
                "evidence_source_ids": list(
                    dict.fromkeys(entries[i]["unit"].source_id for i in ids)
                ),
                "gap": "" if ids else "原问题中的条件或子问题尚未获得可核对证据",
            }
        )
    complete = (
        bool(report)
        and any(x["role"] == "core" for x in report)
        and bool(accepted)
        and all(x["status"] == "covered" for x in report if x["role"] == "core")
    )
    return {
        "task_question": plan.original_question,
        "parse_status": "parsed" if plan.items else "unparseable_unknown",
        "items": report,
        "procedure_status": "complete" if complete else "insufficient",
        "overall_completable": complete,
        "failure_reasons": [
            x["gap"] for x in report if x["role"] == "core" and x["status"] != "covered"
        ],
        "semantic_review": "NOT_MEASURED",
    }


def serialize_plan(plan):
    items = []
    for task in plan.items + plan.unresolved:
        if any(x["id"] == task.id for x in items):
            continue
        items.append(asdict(task) | {"value": plan.original_question[slice(*task.question_span)]})
    return {
        "version": "research-tasks-v6",
        "question": plan.original_question,
        "parse_status": "parsed" if plan.items else "unparseable_unknown",
        "items": items,
        "parse_notes": ["模型候选经原文位置校验；完成性由服务器证据谓词独立判断"],
    }
