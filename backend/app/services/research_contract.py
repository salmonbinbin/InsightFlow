"""Phase 8 — pure task / evidence / coverage contract (Batch A r2).

Pre-persistence / pre-source-read pure contract. Does NOT call the
LLM, the research provider, or the database. v14 validator gates
continue to live in ``research_service``; this module is a
deliberately minimal, conservative pre-flight that downstream
stages may integrate, never replace.

Three primitives:

1. :class:`TaskPlan` — built by a STRICT grammar from the raw
   question. Tokens outside the grammar (e.g. ``while offline``)
   become :data:`KIND_UNRESOLVED` CORE and block completion. No
   fixed 10-question ID table.

2. :class:`Catalog` — built by :func:`build_evidence_catalog`
   from a saved Markdown snapshot. h1-h4 headers become
   :class:`Section` ranges; numbered list items become
   :class:`EvidenceUnit` rows with ``section_path``,
   ``step_index``, ``step_total`` and a content-derived
   ``catalog_id``. SHA-256 fingerprints the snapshot.

3. :class:`EvidenceUnit` can also be built directly via
   :func:`build_evidence_unit`, but the result is an ISOLATED
   unit (no ``catalog_id``). An isolated unit may satisfy a
   single product / platform / entry / operation / quantifier /
   limit / version token, but is NOT eligible to complete a
   procedure requirement. Procedure coverage requires catalog
   units selected from a single section, with ``step_index``
   covering 1..``step_total``, no duplicates, no cross-section
   splicing.

:class:`CoverageReport` is the only completion gate. LLM
self-rating, free-text section labels, and bare digits / ``all``
/ ``many`` never widen coverage. Multi-select quantifiers must
be ``multiple`` / ``several`` / ``multi-select`` / ``multi
select`` / ``batch`` in the SAME flow as the action.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional, Sequence, Tuple

MAX_CITATION_CHARS = 200
KIND_PRODUCT = "product"
KIND_PLATFORM = "platform"
KIND_ENTRY = "entry"
KIND_OPERATION = "operation"
KIND_MULTI_SELECT = "multi_select"
KIND_PROCEDURE = "procedure"
KIND_LIMIT = "limit"
KIND_VERSION = "version"
KIND_WHERE = "where"
KIND_CHOICES = "choices"
KIND_DISTINGUISH_AUTO = "distinguish_auto"
KIND_UNRESOLVED = "unresolved_clause"
ROLE_CORE = "core"
ROLE_AUXILIARY = "auxiliary"
ROLE_REVIEW = "review_required"

_PRODUCT_TOKENS = (
    "google photos",
    "google drive",
    "icloud photos",
    "icloud",
    "dropbox",
    "onedrive",
    "apple photos",
)
_PLATFORM_TOKENS = (
    "android",
    "ios",
    "iphone",
    "ipad",
    "macos",
    "mac",
    "windows",
    "web",
    "phone",
    "tablet",
    "desktop",
    "mobile",
    "shared photo library",
)
_ENTRY_TOKENS = (
    "icloud.com",
    "drive.google.com",
    "photos.google.com",
    "dropbox.com",
    "onedrive.live.com",
    "shared photo library",
)
_OPERATION_TOKENS = (
    "upload",
    "backup",
    "sync",
    "share",
    "select",
    "move",
    "delete",
    "add photo",
    "add photos",
    "create album",
)
_MANUAL_FLOW_TOKENS = (
    "manually",
    "manual upload",
    "manual add",
    "by hand",
    "manually upload",
    "manually add",
    "tap to upload",
)
_AUTO_FLOW_TOKENS = (
    "camera upload",
    "auto upload",
    "automatic upload",
    "auto backup",
    "automatic backup",
    "background sync",
    "roaming",
    "auto switch",
    "syncing",
    "automatic",
    "auto",
)
_WORKAROUND_TOKENS = (
    "workaround",
    "gesture",
    "two-finger",
    "two finger",
    "limitation",
    "limitations",
)
# Intentionally narrow. ``all`` / ``many`` / bare digits are NOT
# accepted — a storage number must not satisfy multi-select.
_QUANTIFIER_TOKENS = ("multiple", "several", "multi-select", "multi select", "batch")
_LIMIT_QUESTION_PHRASES = (
    "any limits",
    "are there limits",
    "what are the limits",
    "what's the limit",
    "what is the limit",
    "maximum",
    "max ",
    "any restrictions",
    "what limits",
    "state the limits",
    "state any limits",
)
_WHERE_QUESTION_PHRASES = (
    "where should",
    "where can",
    "where do",
    "where to",
    "upload to a",
    "upload to the",
    "save to a",
    "save to the",
)
_CHOICES_QUESTION_PHRASES = (
    "what are the choices",
    "what choices",
    "which choices",
    "what are the options",
    "which options",
    "what options",
    "upload choices",
    "upload options",
)
_CONDITION_KEYWORDS = (
    "while ",
    "when ",
    "if ",
    "unless ",
    "in case ",
    "after ",
    "before ",
    "during ",
    "as soon as",
    "once ",
)
_STEP_QUESTION_PHRASES = (
    "what are the steps",
    "steps to",
    "how to",
    "step by step",
    "the procedure",
    "the workflow",
)
# Narrow procedure grammar gate. Only the explicit patterns
# below can be auto-verified; everything else defaults to
# review_required. Known conditional clauses (while/when/if)
# still block regardless of pattern.
_PRODUCT_GRAMMAR = r"(?:google photos|google drive|onedrive|dropbox)"
_PLATFORM_GRAMMAR = r"(?:android|ios|iphone|mobile)"
_NARROW_PROCEDURE_PATTERNS = (
    re.compile(
        rf"what are (?:the )?(?:{_PLATFORM_GRAMMAR}(?: app)? )?steps to "
        rf"(?:manually )?(?:upload|back up|add) "
        rf"(?:(?:multiple|several|one|existing) )?(?:photos|pictures|files|photo or video)"
        rf"(?:(?: to| in) (?:{_PRODUCT_GRAMMAR}|(?:the )?{_PLATFORM_GRAMMAR}(?: app)?))?"
        rf"(?: on {_PLATFORM_GRAMMAR})?[?.]?",
        re.IGNORECASE,
    ),
    re.compile(
        rf"how does an? {_PLATFORM_GRAMMAR} user select existing pictures "
        rf"for a manual upload in {_PRODUCT_GRAMMAR}[?.]?",
        re.IGNORECASE,
    ),
)


def _matches_procedure_grammar(question: str) -> bool:
    body = re.sub(r"^according to [^,]+,\s*", "", question.strip(), flags=re.I)
    body = re.sub(r"\s*cite only official [^;.!?]+[.!?]?", "", body, flags=re.I).strip()
    body = re.sub(r";\s*distinguish automatic backup\.?$", "", body, flags=re.I).strip()
    return any(pattern.fullmatch(body) for pattern in _NARROW_PROCEDURE_PATTERNS)


_VERSION_PATTERN = re.compile(r"\b(?:ios|android|windows|macos|mac os)[ \-_]?\d{1,2}(?:\.\d+)?\b")
_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_H2 = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_H3 = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
_H4 = re.compile(r"^####\s+(.+?)\s*$", re.MULTILINE)
_NUMBERED_STEP = re.compile(
    r"^[ \t]*(?:step[ \t]+)?\d+[.):][ \t]+.+?$", re.IGNORECASE | re.MULTILINE
)
_TERMINATOR = re.compile(r"[.?!]\s")


class ParseStatus(str, Enum):
    PARSED = "parsed"
    UNPARSEABLE_EMPTY = "unparseable_empty"
    UNPARSEABLE_MULTI_INTENT = "unparseable_multi_intent"
    UNPARSEABLE_UNKNOWN = "unparseable_unknown"


class CoverageStatus(str, Enum):
    COVERED = "covered"
    PARTIAL = "partial"
    NOT_VERIFIED = "not_verified"
    NOT_APPLICABLE = "not_applicable"


class ProcedureStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


class ContractError(ValueError):
    """Raised when input violates a contract invariant."""


@dataclass(frozen=True)
class RequirementItem:
    id: str
    role: str
    kind: str
    value: str
    predicate_note: str


@dataclass(frozen=True)
class TaskPlan:
    question: str
    parse_status: ParseStatus
    items: Tuple[RequirementItem, ...]
    parse_notes: Tuple[str, ...]

    @property
    def core_items(self) -> Tuple[RequirementItem, ...]:
        return tuple(i for i in self.items if i.role == ROLE_CORE)

    @property
    def unresolved_clauses(self) -> Tuple[RequirementItem, ...]:
        return tuple(i for i in self.items if i.role == ROLE_CORE and i.kind == KIND_UNRESOLVED)

    @property
    def review_required(self) -> bool:
        return any(i.role == ROLE_REVIEW for i in self.items)

    @property
    def is_completable_target(self) -> bool:
        return (
            self.parse_status == ParseStatus.PARSED
            and not self.unresolved_clauses
            and not self.review_required
        )


@dataclass(frozen=True)
class EvidenceUnit:
    source_id: str
    offset: int
    length: int
    text: str
    section: str
    context: str
    section_path: Tuple[str, ...] = ()
    step_index: int = 0
    step_total: int = 0
    catalog_id: str = ""

    @property
    def evidence_id(self) -> str:
        return self.catalog_id or f"{self.source_id}:{self.offset}:{self.length}"

    @property
    def is_isolated(self) -> bool:
        return not self.catalog_id


@dataclass(frozen=True)
class Section:
    title: str
    path: Tuple[str, ...]
    level: int
    start_offset: int
    end_offset: int
    unit_ids: Tuple[str, ...]
    step_total: int

    @property
    def full_path(self) -> Tuple[str, ...]:
        return self.path + (self.title,)


@dataclass(frozen=True)
class Catalog:
    source_id: str
    snapshot_sha256: str
    snapshot_length: int
    sections: Tuple[Section, ...]
    units: Tuple[EvidenceUnit, ...]


@dataclass(frozen=True)
class CoverageItem:
    requirement_id: str
    role: str
    kind: str
    status: CoverageStatus
    evidence_ids: Tuple[str, ...]
    evidence_source_ids: Tuple[str, ...]
    gap: str


@dataclass(frozen=True)
class CoverageReport:
    task_question: str
    parse_status: ParseStatus
    items: Tuple[CoverageItem, ...]
    procedure_status: ProcedureStatus
    overall_completable: bool
    failure_reasons: Tuple[str, ...]


# --- TaskPlan — strict grammar -------------------------------------------


def build_task_plan(question: str) -> TaskPlan:
    """Bounded-grammar question parse.

    Only the narrow :data:`_NARROW_PROCEDURE_PATTERNS` (``what
    are ... steps to ...`` / ``how to ...``) can be auto-
    verified. Any other question is parsed for tokens but every
    requirement is marked :data:`ROLE_REVIEW` so it cannot be
    auto-completed by string co-occurrence alone. Conditional
    clauses (``while offline`` etc.) are recorded as
    :data:`KIND_UNRESOLVED` CORE and block completion. The
    function does NOT query a fixed 10-question ID table.
    """
    q_raw = question if isinstance(question, str) else ""
    q = q_raw.strip()
    if not q:
        return TaskPlan(q_raw, ParseStatus.UNPARSEABLE_EMPTY, (), ("empty question",))
    lower = q.lower()
    notes: list[str] = []
    products = _scan_products(lower)
    platforms = _scan_platforms(lower)
    entries = _scan_entries(lower)
    operations = _scan_operations(lower)
    multi_select = any(t in lower for t in _QUANTIFIER_TOKENS)
    distinguish_auto = _scan_distinguish_auto(lower)
    version = _scan_version(lower)
    limit = _scan_phrase(lower, _LIMIT_QUESTION_PHRASES)
    where = _scan_phrase(lower, _WHERE_QUESTION_PHRASES)
    choices = _scan_phrase(lower, _CHOICES_QUESTION_PHRASES)
    condition = _scan_condition_clause(q)
    narrow_procedure = _matches_procedure_grammar(q)
    if len(products) > 1:
        return _unparseable(q, f"multi-intent: distinct products {products!r}")
    if len(platforms) > 2:
        return _unparseable(q, f"multi-intent: distinct platforms {platforms!r}")
    items_seq: list[RequirementItem] = []
    seq = 0

    def _n(prefix: str) -> str:
        nonlocal seq
        seq += 1
        return f"req-{prefix}-{seq}"

    def _add(prefix: str, kind: str, value: str, note: str, role: str) -> None:
        items_seq.append(RequirementItem(_n(prefix), role, kind, value, note))

    auto_role = ROLE_CORE

    for p in products:
        _add("product", KIND_PRODUCT, p, "Mention this product.", auto_role)
    for p in platforms:
        _add("platform", KIND_PLATFORM, p, "Match this platform section.", auto_role)
    for e in entries:
        _add("entry", KIND_ENTRY, e, "Multi-entry: one missing -> NOT_VERIFIED.", auto_role)
    for op in operations:
        _add(
            "operation",
            KIND_OPERATION,
            op,
            "Auto/roaming/sync and workaround/gesture/version-narrow do not qualify.",
            auto_role,
        )
    if multi_select:
        _add(
            "multi-select",
            KIND_MULTI_SELECT,
            "multi_select",
            "v14 quantifier (multiple/several/multi-select/batch) in SAME flow. Generic plural, all/many, bare digits NOT accepted.",
            auto_role,
        )
    if narrow_procedure:
        _add(
            "procedure",
            KIND_PROCEDURE,
            "procedure",
            "Catalog units, single section, step_index 1..N of step_total, no duplicates, no cross-section splicing. Isolated NOT eligible.",
            ROLE_CORE,
        )
    if distinguish_auto is not None:
        _add(
            "distinguish-auto",
            KIND_DISTINGUISH_AUTO,
            distinguish_auto,
            "Match the requested flow (manual vs automatic).",
            auto_role,
        )
    if version is not None:
        _add("version", KIND_VERSION, version, "Mention the requested version.", auto_role)
    if limit is not None:
        _add("limit", KIND_LIMIT, limit, "No evidence -> NOT_VERIFIED, NOT 'no limit'.", auto_role)
    if where is not None:
        _add("where", KIND_WHERE, where, "Evidence must point at the destination.", auto_role)
    if choices is not None:
        _add("choices", KIND_CHOICES, choices, "Evidence must enumerate the choice set.", auto_role)
    if condition is not None:
        _add(
            "unresolved",
            KIND_UNRESOLVED,
            condition,
            "Conditional clause cannot be resolved. Blocks completion.",
            ROLE_CORE,
        )
        notes.append(f"unresolved clause: {condition!r}")
    if not items_seq:
        return TaskPlan(
            q, ParseStatus.UNPARSEABLE_UNKNOWN, (), ("no supported token matched the question",)
        )
    if not narrow_procedure:
        _add(
            "review",
            KIND_UNRESOLVED,
            "review required: original question outside supported complete grammar",
            "Original question retained; automatic completeness is unavailable.",
            ROLE_CORE,
        )
        notes.append("review required: unsupported complete grammar")
    return TaskPlan(q, ParseStatus.PARSED, tuple(items_seq), tuple(notes))


def _unparseable(q: str, note: str) -> TaskPlan:
    return TaskPlan(q, ParseStatus.UNPARSEABLE_MULTI_INTENT, (), (note,))


def _scan_products(lower: str) -> list[str]:
    matches = [p for p in _PRODUCT_TOKENS if p in lower]
    return [p for p in matches if not any(p != other and p in other for other in matches)]


def _scan_platforms(lower: str) -> list[str]:
    return [p for p in _PLATFORM_TOKENS if re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", lower)]


def _scan_entries(lower: str) -> list[str]:
    return [e for e in _ENTRY_TOKENS if e in lower]


def _scan_operations(lower: str) -> list[str]:
    out: list[str] = []
    lower = re.sub(r"\bback\s+up\b", "backup", lower)
    for o in sorted(_OPERATION_TOKENS, key=len, reverse=True):
        if o in lower and not any(o in f for f in out):
            out.append(o)
    return out


def _scan_distinguish_auto(lower: str) -> Optional[str]:
    has_m = any(t in lower for t in _MANUAL_FLOW_TOKENS)
    has_a = any(t in lower for t in _AUTO_FLOW_TOKENS)
    if has_m and not has_a:
        return "manual"
    if has_a and not has_m:
        return "automatic"
    if has_m and has_a:
        return "both"
    return None


def _scan_version(lower: str) -> Optional[str]:
    m = _VERSION_PATTERN.search(lower)
    return m.group(0) if m else None


def _scan_phrase(lower: str, phrases: Sequence[str]) -> Optional[str]:
    for p in phrases:
        if p in lower:
            return p
    return None


def _scan_condition_clause(question: str) -> Optional[str]:
    lower = question.lower()
    for kw in _CONDITION_KEYWORDS:
        idx = lower.find(kw)
        if idx < 0:
            continue
        rest = question[idx:]
        end = _TERMINATOR.search(rest[1:] + " ")
        clause = rest[: end.start() + 1] if end else rest.rstrip(".?! ")
        return clause.strip()
    return None


# ============================================================================
# EvidenceUnit and Catalog
# ============================================================================


def build_evidence_unit(
    *, snapshot: str, source_id: str, offset: int, length: int, section: str = "", context: str = ""
) -> EvidenceUnit:
    """Build an ISOLATED EvidenceUnit. No catalog_id, no
    step_index, no section_path. NOT eligible to complete a
    procedure requirement.
    """
    if isinstance(source_id, bool) or not isinstance(source_id, str) or not source_id:
        raise ContractError("source_id must be a non-empty string")
    if not isinstance(snapshot, str):
        raise ContractError("snapshot must be a string")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ContractError("offset must be a non-negative integer")
    if isinstance(length, bool) or not isinstance(length, int) or length <= 0:
        raise ContractError("length must be a positive integer")
    if length > MAX_CITATION_CHARS:
        raise ContractError(f"length must be <= {MAX_CITATION_CHARS} chars")
    if offset + length > len(snapshot):
        raise ContractError("offset + length exceeds snapshot length")
    return EvidenceUnit(
        source_id=source_id,
        offset=offset,
        length=length,
        text=snapshot[offset : offset + length],
        section=section if isinstance(section, str) else "",
        context=context if isinstance(context, str) else "",
    )


def build_evidence_catalog(source_id: str, snapshot: str) -> Catalog:
    """Parse a saved Markdown snapshot into a :class:`Catalog`.
    h1-h4 headers become Section ranges; numbered list items
    become EvidenceUnit rows. All offsets and ids are derived
    from the snapshot bytes. Content-addressed by SHA-256.
    """
    if isinstance(source_id, bool) or not isinstance(source_id, str) or not source_id:
        raise ContractError("source_id must be a non-empty string")
    if not isinstance(snapshot, str):
        raise ContractError("snapshot must be a string")
    snap_sha = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
    headers: list[tuple[int, int, str]] = []
    for pat, level in ((_H1, 1), (_H2, 2), (_H3, 3), (_H4, 4)):
        for m in pat.finditer(snapshot):
            headers.append((m.start(), level, m.group(1).strip()))
    headers.sort(key=lambda x: x[0])
    raw_sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    for i, (off, level, title) in enumerate(headers):
        end = headers[i + 1][0] if i + 1 < len(headers) else len(snapshot)
        while stack and stack[-1][0] >= level:
            stack.pop()
        raw_sections.append(
            Section(
                title=title,
                path=tuple(item[1] for item in stack),
                level=level,
                start_offset=off,
                end_offset=end,
                unit_ids=(),
                step_total=0,
            )
        )
        stack.append((level, title))
    units: list[EvidenceUnit] = []
    final_sections: list[Section] = []
    for sec in raw_sections:
        body = snapshot[sec.start_offset : sec.end_offset]
        steps = [
            (sec.start_offset + m.start(), len(m.group(0)), m.group(0))
            for m in _NUMBERED_STEP.finditer(body)
        ]
        numbers = [
            int(re.search(r"\d+", m.group(0)).group()) for m in _NUMBERED_STEP.finditer(body)
        ]
        total_steps = max(numbers, default=0)
        sec_units = [
            EvidenceUnit(
                source_id=source_id,
                offset=off,
                length=length,
                text=text,
                section=sec.title,
                context=" / ".join(sec.full_path),
                section_path=sec.full_path,
                step_index=numbers[idx - 1],
                step_total=total_steps,
                catalog_id=f"{source_id}@{sec.start_offset}#{idx}",
            )
            for idx, (off, length, text) in enumerate(steps, start=1)
            if length <= MAX_CITATION_CHARS
        ]
        final_sections.append(
            Section(
                title=sec.title,
                path=sec.path,
                level=sec.level,
                start_offset=sec.start_offset,
                end_offset=sec.end_offset,
                unit_ids=tuple(u.catalog_id for u in sec_units),
                step_total=total_steps,
            )
        )
        units.extend(sec_units)
    return Catalog(
        source_id=source_id,
        snapshot_sha256=snap_sha,
        snapshot_length=len(snapshot),
        sections=tuple(final_sections),
        units=tuple(units),
    )


def extract_citation(evidence: EvidenceUnit) -> str:
    return evidence.text


# ============================================================================
# Coverage evaluation
# ============================================================================


def evaluate_coverage(
    plan: TaskPlan,
    evidence_units: Sequence[EvidenceUnit],
    *,
    catalog: Optional[Catalog] = None,
    selected_evidence_ids: Optional[Sequence[str]] = None,
) -> CoverageReport:
    """Pure coverage report. ``selected_evidence_ids`` (when
    given) names the catalog-derived units the caller claims
    cover the question. Procedure coverage is computed ONLY
    over the selected units and rejects duplicate / partial /
    cross-section / cross-source selections and isolated
    units. Without ``selected_evidence_ids``, the procedure
    requirement (if any) is INSUFFICIENT.
    """
    if plan.parse_status != ParseStatus.PARSED:
        return _abort(
            plan,
            (f"plan not parsable: {plan.parse_status.value}",),
            ProcedureStatus.INSUFFICIENT,
            "parse",
        )
    if plan.unresolved_clauses:
        clauses = "; ".join(i.value for i in plan.unresolved_clauses)
        return _abort(
            plan, (f"unresolved clause(s): {clauses}",), ProcedureStatus.INSUFFICIENT, "unresolved"
        )
    if not evidence_units and not catalog:
        return _abort(plan, ("no evidence supplied",), ProcedureStatus.INSUFFICIENT, "empty")
    if selected_evidence_ids and len(selected_evidence_ids) != len(set(selected_evidence_ids)):
        report = _abort(
            plan, ("duplicate evidence selection",), ProcedureStatus.INSUFFICIENT, "empty"
        )
        return replace(
            report,
            items=tuple(replace(item, gap="duplicate evidence selection") for item in report.items),
        )
    selected_set = set(selected_evidence_ids or ())
    selected_units: Tuple[EvidenceUnit, ...] = (
        tuple(u for u in evidence_units if u.evidence_id in selected_set)
        if selected_evidence_ids
        else ()
    )
    items: list[CoverageItem] = []
    failure_reasons: list[str] = []
    overall_ok = True
    procedure_status = ProcedureStatus.COMPLETE
    for req in plan.items:
        if req.role == ROLE_REVIEW:
            # Review-required items are NOT auto-completable; they
            # require downstream human / model review and never block
            # overall_completable via the failure path. The procedure
            # gate keeps controlling the run-level outcome.
            items.append(
                CoverageItem(
                    requirement_id=req.id,
                    role=req.role,
                    kind=req.kind,
                    status=CoverageStatus.NOT_VERIFIED,
                    evidence_ids=(),
                    evidence_source_ids=(),
                    gap="review required: not in narrow procedure grammar",
                )
            )
            continue
        if req.kind == KIND_PROCEDURE:
            status, ids, srcs, gap, proc = _eval_procedure(catalog, selected_units)
            procedure_status = proc
        else:
            status, ids, srcs, gap = _eval_simple(
                req, selected_units if selected_evidence_ids else evidence_units, plan.question
            )
        items.append(
            CoverageItem(
                requirement_id=req.id,
                role=req.role,
                kind=req.kind,
                status=status,
                evidence_ids=ids,
                evidence_source_ids=srcs,
                gap=gap,
            )
        )
        if req.role == ROLE_CORE and status != CoverageStatus.COVERED:
            overall_ok = False
            failure_reasons.append(f"{req.id}({req.kind}={req.value!r}): {status.value} {gap}")
    return CoverageReport(
        task_question=plan.question,
        parse_status=plan.parse_status,
        items=tuple(items),
        procedure_status=procedure_status,
        overall_completable=overall_ok,
        failure_reasons=tuple(failure_reasons),
    )


def _abort(plan, reasons, proc, reason):
    gap_map = {
        "parse": lambda i: f"plan not parsable: {plan.parse_status.value}",
        "unresolved": lambda i: f"review required; unresolved clause: {i.value!r}",
        "empty": lambda i: "no evidence supplied",
    }
    items = tuple(
        CoverageItem(
            requirement_id=i.id,
            role=i.role,
            kind=i.kind,
            status=CoverageStatus.NOT_VERIFIED,
            evidence_ids=(),
            evidence_source_ids=(),
            gap=gap_map[reason](i),
        )
        for i in plan.items
    )
    return CoverageReport(
        task_question=plan.question,
        parse_status=plan.parse_status,
        items=items,
        procedure_status=proc,
        overall_completable=False,
        failure_reasons=reasons,
    )


def _eval_procedure(catalog, selected_units):
    if catalog is None:
        return _nvp("procedure requires a parsed catalog")
    if not selected_units:
        return _nvp("procedure requires selected_evidence_ids")
    if any(u.is_isolated for u in selected_units):
        return _nvp("isolated unit cannot satisfy a procedure")
    sources = {u.source_id for u in selected_units}
    if sources != {catalog.source_id}:
        return _nvp("selected units are from a different source than the catalog")
    sections = {u.section_path for u in selected_units}
    if len(sections) != 1:
        return _nvp("procedure requires a single section")
    indices = [u.step_index for u in selected_units if u.step_index > 0]
    if len(indices) != len(set(indices)):
        return _nvp("procedure selection contains duplicate step_index")
    if not indices or min(indices) != 1:
        return _partialp(selected_units, "procedure does not start at step 1")
    step_total = max((u.step_total for u in selected_units if u.step_index > 0), default=0)
    if set(indices) != set(range(1, step_total + 1)):
        missing = sorted(set(range(1, step_total + 1)) - set(indices))
        return _partialp(
            selected_units,
            f"procedure is missing step(s) {missing} of {step_total}",
        )
    return _ok(selected_units, proc=ProcedureStatus.COMPLETE)


def _eval_simple(req, evidence_units, plan_question):
    handler = _SIMPLE_HANDLERS.get(req.kind)
    if handler is None:
        return (CoverageStatus.NOT_APPLICABLE, (), (), f"unsupported requirement kind: {req.kind}")
    return handler(req, evidence_units, plan_question)


def _h_product(req, evs, _q):
    v = req.value
    m = [ev for ev in evs if v in ev.text.lower() or v in ev.context.lower()]
    return _ok_simple(m) if m else _nv("no evidence mentions the requested product")


def _h_platform(req, evs, _q):
    aliases = {
        "iphone": ("iphone", "ios"),
        "ipad": ("ipad", "ios"),
        "ios": ("ios", "iphone", "ipad"),
    }
    wanted = aliases.get(req.value, (req.value,))
    matched = [
        ev
        for ev in evs
        if any(
            re.search(
                r"(?<!\w)" + re.escape(term) + r"(?!\w)",
                ev.section.lower() + " " + ev.context.lower() + " " + ev.text.lower(),
            )
            for term in wanted
        )
    ]
    return (
        _ok_simple(matched)
        if matched
        else _nv("no evidence is from the requested platform section")
    )


def _h_entry(req, evs, _q):
    v = req.value
    m = [
        ev
        for ev in evs
        if v in ev.section.lower() or v in ev.context.lower() or v in ev.text.lower()
    ]
    return (
        _ok_simple(m)
        if m
        else _nv(
            "entry not backed by any cited evidence (multi-entry: one missing -> not complete)"
        )
    )


def _h_operation(req, evs, plan_question):
    op = req.value.lower()
    wants_manual = any(t in plan_question.lower() for t in _MANUAL_FLOW_TOKENS)
    m = []
    for ev in evs:
        el = re.sub(r"\bback\s+up\b", "backup", ev.text.lower())
        heading = re.sub(r"\bback\s+up\b", "backup", ev.section.lower())
        if not (op in el or op in heading):
            continue
        if op in ("upload", "add photo", "add photos", "backup"):
            if wants_manual and _is_auto_or_sync(ev.text):
                continue
        if _is_workaround(ev.text):
            continue
        m.append(ev)
    return (
        _ok_simple(m)
        if m
        else _nv(
            "operation not backed by matching evidence; auto/sync/roaming/workaround cannot substitute"
        )
    )


def _h_multi_select(req, evs, _q):
    m = [ev for ev in evs if _has_quantifier(ev.text)]
    return (
        _ok_simple(m)
        if m
        else _nv(
            "multi-select requires a v14 quantifier (multiple/several/multi-select/batch) in the same flow as the action"
        )
    )


def _h_distinguish_auto(req, evs, _q):
    flow = req.value
    m = []
    for ev in evs:
        el = (ev.text + " " + ev.context).lower()
        if flow == "manual":
            if any(t in el for t in _MANUAL_FLOW_TOKENS):
                m.append(ev)
        elif flow == "automatic":
            if any(t in el for t in _AUTO_FLOW_TOKENS):
                m.append(ev)
        elif any(t in el for t in _MANUAL_FLOW_TOKENS) and any(t in el for t in _AUTO_FLOW_TOKENS):
            m.append(ev)
    return (
        _ok_simple(m)
        if m
        else _nv("evidence does not match the requested flow (manual vs automatic)")
    )


def _h_version(req, evs, _q):
    v = req.value.lower()
    m = [ev for ev in evs if v in ev.text.lower()]
    return (
        _ok_simple(m) if m else _nv("version-narrow evidence cannot satisfy a cross-version claim")
    )


def _h_limit(req, evs, _q):
    m = [ev for ev in evs if req.value in ev.text.lower()]
    return (
        _ok_simple(m)
        if m
        else _nv(
            "limit was explicitly asked; no evidence names the same limit (NOT inferred as 'no limit')"
        )
    )


def _h_where(req, evs, _q):
    v = req.value
    m = [ev for ev in evs if v in ev.text.lower() or v in ev.context.lower()]
    return _ok_simple(m) if m else _nv("'where' destination not backed by any evidence")


def _h_choices(req, evs, _q):
    m = [ev for ev in evs if _is_choice_enumeration(ev.text)]
    return _ok_simple(m) if m else _nv("choice enumeration not backed by any evidence")


_SIMPLE_HANDLERS: dict = {
    KIND_PRODUCT: _h_product,
    KIND_PLATFORM: _h_platform,
    KIND_ENTRY: _h_entry,
    KIND_OPERATION: _h_operation,
    KIND_MULTI_SELECT: _h_multi_select,
    KIND_DISTINGUISH_AUTO: _h_distinguish_auto,
    KIND_VERSION: _h_version,
    KIND_LIMIT: _h_limit,
    KIND_WHERE: _h_where,
    KIND_CHOICES: _h_choices,
}


def _ok(units, gap="", proc=None):
    return (CoverageStatus.COVERED, _ids(units), _srcs(units), gap, proc)


def _ok_simple(units):
    return (CoverageStatus.COVERED, _ids(units), _srcs(units), "")


def _nv(gap):
    return (CoverageStatus.NOT_VERIFIED, (), (), gap)


def _nvp(gap):
    return (CoverageStatus.NOT_VERIFIED, (), (), gap, ProcedureStatus.INSUFFICIENT)


def _partialp(units, gap):
    return (CoverageStatus.PARTIAL, _ids(units), _srcs(units), gap, ProcedureStatus.PARTIAL)


def _uniq(units, attr):
    seen: list[str] = []
    for u in units:
        v = getattr(u, attr)
        if v not in seen:
            seen.append(v)
    return tuple(seen)


def _ids(units):
    return _uniq(units, "evidence_id")


def _srcs(units):
    return _uniq(units, "source_id")


def _is_auto_or_sync(text: str) -> bool:
    lower = text.lower()
    return any(t in lower for t in _AUTO_FLOW_TOKENS) or "sync" in lower


def _is_workaround(text: str) -> bool:
    return any(t in text.lower() for t in _WORKAROUND_TOKENS)


def _has_quantifier(text: str) -> bool:
    return any(t in text.lower() for t in _QUANTIFIER_TOKENS)


def _is_choice_enumeration(text: str) -> bool:
    return (", " in text and " or " in text) or bool(re.search(r"\b\w+,\s+\w+,\s+\w+", text))


__all__ = [
    "MAX_CITATION_CHARS",
    "KIND_PRODUCT",
    "KIND_PLATFORM",
    "KIND_ENTRY",
    "KIND_OPERATION",
    "KIND_MULTI_SELECT",
    "KIND_PROCEDURE",
    "KIND_LIMIT",
    "KIND_VERSION",
    "KIND_WHERE",
    "KIND_CHOICES",
    "KIND_DISTINGUISH_AUTO",
    "KIND_UNRESOLVED",
    "ROLE_CORE",
    "ROLE_AUXILIARY",
    "ROLE_REVIEW",
    "ParseStatus",
    "CoverageStatus",
    "ProcedureStatus",
    "RequirementItem",
    "TaskPlan",
    "EvidenceUnit",
    "Section",
    "Catalog",
    "CoverageItem",
    "CoverageReport",
    "ContractError",
    "build_task_plan",
    "build_evidence_unit",
    "build_evidence_catalog",
    "extract_citation",
    "evaluate_coverage",
]
