"""Phase 8 Research context, query and strict v14 validation boundaries.

New runs use the v5 executor for bounded official retrieval, saved evidence
locations, deterministic citation construction and independent task coverage.
The old fact-confirmation helper remains a per-finding check; it is no longer
sufficient to complete a run. Internal feedback is read only, and unknown
coverage or semantic review never becomes an affirmative capability claim.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.prompts import research_agent_v6 as _research_prompt
from app.db.config import settings
from app.models import (
    Feedback,
    Insight,
    InsightDecision,
    InsightEvidence,
    InsightRun,
    Project,
    ResearchFinding,
    ResearchFindingCategory,
    ResearchFindingStatus,
    ResearchRun,
    ResearchRunStatus,
    ResearchSource,
)

# Public dependency bindings used by the v5 executor and existing test seams.
from app.services.research_client import ResearchLLMClient as ResearchLLMClient
from app.services.research_client import ResearchLLMConfigError as ResearchLLMConfigError
from app.services.research_client import ResearchLLMProtocolError as ResearchLLMProtocolError
from app.services.research_client import ResearchLLMTransportError as ResearchLLMTransportError
from app.services.research_provider import (
    DEFAULT_MAX_RETRIES,
    ProviderSearchResult,
    ProviderSource,
    ResearchProviderConfigError,
)
from app.services.research_provider import ResearchProviderClient as ResearchProviderClient
from app.services.research_provider import (
    ResearchProviderResponseError as ResearchProviderResponseError,
)
from app.services.research_provider import (
    ResearchProviderTransportError as ResearchProviderTransportError,
)

logger = logging.getLogger(__name__)


# ---- Public limits ---------------------------------------------------------

RESEARCH_EVIDENCE_LIMIT = 5
RESEARCH_EVIDENCE_EXCERPT_MAX_CHARS = 400
RESEARCH_EVIDENCE_SUMMARY_TOTAL_MAX_CHARS = 2000
RESEARCH_QUESTION_MAX_LEN = 500
RESEARCH_SCOPE_MAX_LEN = 2000

# Mirrors :mod:`app.services.research_provider` retry table.
RESEARCH_PROVIDER_MAX_RETRIES = DEFAULT_MAX_RETRIES
RESEARCH_LLM_MAX_RETRIES = 2

# Categories the LLM is allowed to emit.
ALLOWED_FINDING_CATEGORIES: frozenset[str] = frozenset({c.value for c in ResearchFindingCategory})
ALLOWED_FINDING_STATUSES: frozenset[str] = frozenset({s.value for s in ResearchFindingStatus})

# Categories where every finding MUST carry sources (the prompt forbids
# the LLM from tagging inferred/unverified entries as ``confirmed``).
SOURCED_CATEGORIES: frozenset[str] = frozenset(
    {ResearchFindingCategory.COMPETITOR.value, ResearchFindingCategory.MARKET_PATTERN.value}
)
UNSOURCED_CATEGORIES: frozenset[str] = frozenset(
    {ResearchFindingCategory.OPPORTUNITY.value, ResearchFindingCategory.RISK.value}
)


# ---- Errors ---------------------------------------------------------------


class ResearchConfigError(RuntimeError):
    """Research environment / project ownership check failed."""


class ResearchValidationError(RuntimeError):
    """Validation failure with a safe category code."""

    @property
    def code(self) -> str:
        message = str(self)
        if "status='not_verified' forbids source_refs" in message:
            return "status_refs_invariant"
        if "missing top-level" in message:
            return "missing_top_level_field"
        # The marker list MUST be ordered most-specific first. The
        # validator raises messages like ``"source_refs provider_source_id
        # 'src-INVENTED' not in run sources"`` that contain BOTH the
        # broad ``source_refs`` marker AND the specific
        # ``provider_source_id`` marker. The specific marker is the
        # actionable category for the model (the LLM invented an ID),
        # so it MUST be matched first. A ``"source_refs url must be
        # http(s)"`` rejection, on the other hand, only carries the
        # broad marker and correctly falls through to
        # ``source_refs_invalid``.
        for marker, code in (
            ("unknown top-level", "unknown_top_level_field"),
            ("unexpected top-level", "unknown_top_level_field"),
            ("unexpected payload", "unknown_top_level_field"),
            ("summary", "summary_invalid"),
            ("confidence", "confidence_invalid"),
            ("competitors", "competitors_invalid"),
            ("market_patterns", "market_patterns_invalid"),
            ("opportunities", "opportunities_invalid"),
            ("risks", "risks_invalid"),
            # ``provider_source_id`` is checked BEFORE ``source_refs``
            # so an invented / unknown ID surfaces the more specific
            # ``source_ref_unknown`` category to the LLM, not the
            # broader ``source_refs_invalid`` bucket.
            ("provider_source_id", "source_ref_unknown"),
            ("source_refs", "source_refs_invalid"),
            ("title", "title_invalid"),
            ("description", "description_invalid"),
            ("status", "status_invalid"),
            ("category", "category_invalid"),
        ):
            if marker in message:
                return code
        return "schema_invalid"


# Map a safe validation category code to a bounded, safe retry hint.
# The hint is appended to the next LLM call as a separate user
# message — it deliberately contains NO payload bytes, NO internal
# exception text and NO credential-shaped token. Each value is one
# sentence so the LLM can act on it without exceeding the 500-char
# hint budget.
_VALIDATION_RETRY_HINT_MAP: dict[str, str] = {
    "status_refs_invariant": "not_verified opportunities and risks MUST use empty source_refs=[].",
    "missing_top_level_field": "Emit all six legacy top-level fields: summary, confidence, competitors, market_patterns, opportunities, risks; include empty arrays when no evidence exists.",
    "unknown_top_level_field": (
        "Emit ONLY these six top-level keys: summary, confidence, "
        "competitors, market_patterns, opportunities, risks. Do NOT "
        "lift finding-level fields (description, source_refs, status, "
        "title, item, category) to the top level — they belong INSIDE "
        "the four arrays. Drop any wrapper key (metadata, tags, trace, "
        "citations, references, notes, debug, source, links) — every "
        "extra key is rejected."
    ),
    "summary_invalid": "Summary must be a non-empty string ≤600 chars.",
    "confidence_invalid": "Confidence must be a finite number in [0, 1].",
    "competitors_invalid": (
        "Each competitor must carry ≥1 source_refs entry whose "
        "provider_source_id is one you actually used."
    ),
    "market_patterns_invalid": (
        "Each market_pattern must carry ≥1 source_refs entry; "
        "single-source claims are not sufficient — back the claim with "
        "at least two distinct provider_source_ids or mark it "
        "not_verified."
    ),
    "opportunities_invalid": (
        "Each opportunity must carry a status; not_verified MUST have empty source_refs."
    ),
    "risks_invalid": ("Each risk must carry a status; not_verified MUST have empty source_refs."),
    "source_refs_invalid": (
        "Every source_refs entry must carry provider_source_id, url "
        "and title; drop any provider_source_id that is not in the "
        "list returned by THIS run."
    ),
    "source_ref_unknown": (
        "Use only provider_source_id values returned by THIS run; do not invent IDs or URLs."
    ),
    "title_invalid": "Title must be a non-empty string ≤200 chars.",
    "description_invalid": (
        "Description must be ≤4000 chars. Trim the narrative before "
        "adding detail; never pad beyond the cap or the row is rejected."
    ),
    "status_invalid": "Status must be 'confirmed' or 'not_verified'.",
    "category_invalid": (
        "Category must be one of the closed enum values "
        "(summary, competitor, market_pattern, opportunity, risk)."
    ),
    "schema_invalid": ("Re-emit a payload that strictly matches the closed schema."),
}


def _build_validation_retry_hint(code: str) -> str:
    """Return a bounded, safe retry hint for the LLM.

    The hint is the safe category code from
    :attr:`ResearchValidationError.code` plus a one-sentence
    instruction. It MUST NOT carry the raw exception text, payload
    bytes or any credential-shaped token. The wire format limits the
    full hint to 500 chars.
    """
    guidance = _VALIDATION_RETRY_HINT_MAP.get(code)
    if guidance is None:
        guidance = _VALIDATION_RETRY_HINT_MAP["schema_invalid"]
    hint = f"[validation code={code}] {guidance}"
    # Defence-in-depth: redact any short-lived secret-shaped token
    # before the hint leaves the process. The map above never
    # carries credentials, but if a future change pipes upstream
    # body bytes through the channel this scrub keeps the wire
    # free of ``sk-...`` / ``AKIA...`` shaped tokens.
    hint = _SENSITIVE_TOKEN_RE.sub(" [REDACTED] ", hint)
    hint = " ".join(hint.split())
    if len(hint) > 500:
        hint = hint[:500]
    return hint


# Safe token persisted on the run row when the LLM produced a
# structurally-valid payload but no competitor / market_pattern
# finding carries an external source citation. The string is
# machine-greppable so an audit can scan the table for runs that
# surfaced internal-only research output.
INSUFFICIENT_EXTERNAL_EVIDENCE_CODE = "insufficient_external_evidence"

# Reused error message prefix for the completion-gate failure. The
# suffix is a short, payload-free explanation. The prefix carries the
# safe category token so a downstream consumer can match on it
# without re-parsing free-form text.
INSUFFICIENT_EXTERNAL_EVIDENCE_MESSAGE = (
    f"research {INSUFFICIENT_EXTERNAL_EVIDENCE_CODE}: no competitor or "
    "market_pattern finding carries an external source citation"
)


def _has_external_confirmation(
    findings: tuple[ValidatedFinding, ...],
) -> bool:
    """Return True when at least one finding is an externally
    cited competitor or market_pattern.

    A finding counts as externally confirmed when:

    * its category is in :data:`SOURCED_CATEGORIES` (i.e. it is a
      competitor or market_pattern), AND
    * its status is ``confirmed`` (i.e. it carries at least one
      ``source_refs`` entry pointing at a persisted
      :class:`ResearchSource` row from THIS run).

    A summary finding is NEVER externally confirmed — the prompt
    pins the summary to the internal context alone. A confirmed
    opportunity / risk that happens to cite a source is also NOT
    counted: the brief mandates that *competitive* or *market*
    evidence is what makes a research run "completed", not the
    incidental presence of any cited row.
    """
    for finding in findings:
        if (
            finding.category in SOURCED_CATEGORIES
            and finding.status == ResearchFindingStatus.CONFIRMED.value
        ):
            return True
    return False


# ---- Dataclasses ----------------------------------------------------------


@dataclass(frozen=True)
class InternalContext:
    insight_title: str
    insight_pain_point: str
    insight_affected_segment: str
    insight_recommendation: str
    evidence_external_ids: list[str]
    # Per-evidence truncated summaries (capped at
    # ``RESEARCH_EVIDENCE_EXCERPT_MAX_CHARS`` chars each, total bounded
    # by ``RESEARCH_EVIDENCE_SUMMARY_TOTAL_MAX_CHARS``). Each entry is
    # ``{"external_id": str, "summary": str}`` and aligns positionally
    # with ``evidence_external_ids``. The LLM sees a bounded excerpt
    # of the deterministic AI classification context, NEVER the full
    # raw feedback body.
    evidence_summaries: list[dict[str, str]]
    decision_status: str | None
    decision_priority: str | None
    research_question: str
    scope: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "insight_title": self.insight_title,
            "insight_pain_point": self.insight_pain_point,
            "insight_affected_segment": self.insight_affected_segment,
            "insight_recommendation": self.insight_recommendation,
            "evidence_external_ids": list(self.evidence_external_ids),
            "evidence_summaries": [dict(item) for item in self.evidence_summaries],
            "decision_status": self.decision_status,
            "decision_priority": self.decision_priority,
            "research_question": self.research_question,
            "scope": self.scope,
        }

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class ValidatedFinding:
    category: str
    title: str
    description: str
    confidence: float | None
    status: str
    source_refs: tuple[tuple[str, str], ...]  # (provider_source_id, url)


@dataclass(frozen=True)
class ValidatedResearchPayload:
    summary: str
    confidence: float
    findings: tuple[ValidatedFinding, ...]


# ---- Internal-context assembly -------------------------------------------


def _truncate(value: str | None, limit: int) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > limit:
        text = text[:limit]
    return text


def assemble_internal_context(
    db: Session,
    *,
    insight: Insight,
    decision: InsightDecision | None,
    research_question: str,
    scope: str | None,
) -> InternalContext:
    """Build the snapshot the LLM sees.

    Reads ``InsightEvidence`` (the representative Evidence rows that
    drove the Insight) and the PM Decision snapshot. For each of the
    first :data:`RESEARCH_EVIDENCE_LIMIT` evidence rows a bounded
    excerpt of the underlying ``Feedback.content`` is attached as a
    summary so the LLM has enough context to ground the research
    question. The excerpt is capped at
    :data:`RESEARCH_EVIDENCE_EXCERPT_MAX_CHARS` per entry and the
    total across all entries is capped at
    :data:`RESEARCH_EVIDENCE_SUMMARY_TOTAL_MAX_CHARS`. The full raw
    feedback body is never sent to the LLM.
    """
    evidence_rows = db.scalars(
        select(InsightEvidence)
        .where(InsightEvidence.insight_id == insight.id)
        .order_by(InsightEvidence.created_at, InsightEvidence.id)
    ).all()
    evidence_external_ids: list[str] = []
    evidence_summaries: list[dict[str, str]] = []
    total_summary_chars = 0
    for row in evidence_rows[:RESEARCH_EVIDENCE_LIMIT]:
        ext = row.feedback_external_id or ""
        if not ext:
            continue
        evidence_external_ids.append(ext[:200])
        feedback = db.get(Feedback, row.feedback_id) if row.feedback_id else None
        raw = (feedback.content if feedback is not None else "") or ""
        per_entry_budget = max(
            0,
            RESEARCH_EVIDENCE_SUMMARY_TOTAL_MAX_CHARS - total_summary_chars,
        )
        if per_entry_budget <= 0:
            # Total budget exhausted; record a placeholder so the
            # LLM still sees that the evidence exists but never
            # crosses the global cap.
            excerpt = ""
        else:
            per_entry_limit = min(RESEARCH_EVIDENCE_EXCERPT_MAX_CHARS, per_entry_budget)
            cleaned = " ".join(str(raw).split())
            excerpt = cleaned[:per_entry_limit]
        evidence_summaries.append({"external_id": ext[:200], "summary": excerpt})
        total_summary_chars += len(excerpt)

    return InternalContext(
        insight_title=_truncate(insight.title or "", 200) or "(untitled insight)",
        insight_pain_point=_truncate(insight.pain_point or "", 4000) or "(no pain_point)",
        insight_affected_segment=_truncate(insight.affected_segment or "unknown", 4000),
        insight_recommendation=_truncate(insight.recommendation or "", 4000) or "(none)",
        evidence_external_ids=evidence_external_ids,
        evidence_summaries=evidence_summaries,
        decision_status=decision.status if decision is not None else None,
        decision_priority=decision.pm_priority if decision is not None else None,
        research_question=_truncate(research_question, RESEARCH_QUESTION_MAX_LEN),
        scope=_truncate(scope, RESEARCH_SCOPE_MAX_LEN) if scope else None,
    )


# ---- Provider query building ----------------------------------------------


def build_provider_query(internal_context: InternalContext) -> str:
    """Compose a canonical provider query from the PM's research ask.

    The query is a single line bounded by 500 characters and is
    intentionally deterministic so retries / audits can reproduce the
    same upstream call. The query MUST be dominated by the PM's
    research question — that is the signal a real search provider
    (Tavily) ranks against.

    The earlier implementation prepended the Insight title as a
    trailing anchor. The v2 evaluation surfaced two structural
    problems with that approach:

    1. PM-internal taxonomy (e.g. ``多选模式误触``,
       ``上传体验差``) bled into the search ranking and pulled
       irrelevant hits even when the PM's question already named
       the product / platform (Google Drive, Google Photos,
       iCloud Photos, etc.).
    2. The Insight title duplicates information that already lives
       in the LLM prompt via ``internal_context`` — the LLM does
       not need the title in the search query to reason about it.

    The query is therefore strictly the PM's own words:

    1. ``research_question`` (full) — the PM's actual ask.
    2. ``scope`` (full, optional) — markets / time horizon /
       personas, as the PM wrote them.

    No ``insight_title``, no Insight segment, no Evidence
    external_ids, no evidence summaries, no decision snapshot —
    those fields live inside the LLM prompt only. Cross-product /
    cross-platform pollution (Google Drive query returning Google
    Photos hits) is mitigated later by the phrase-token source
    filter rather than by encoding generic search operators.
    """
    parts: list[str] = []
    question = internal_context.research_question.strip()
    if question:
        parts.append(question)
    if internal_context.scope:
        scope = internal_context.scope.strip()
        if scope:
            parts.append(scope)
    query = " ".join(parts)
    query = " ".join(query.split())
    return query[:500]


# ---- Phrase-token source filter -------------------------------------------
#
# The brief asks for "improve source quality … reduce irrelevant/community
# sources … make product / platform / official-support restrictions take
# priority" without hardcoding per-product logic. The orchestrator
# implements that as a *generic* phrase-token filter: phrases extracted
# verbatim from the PM's research question + scope are used to drop
# sources whose snippet / title / URL does not actually mention any of
# those phrases. No product-specific map, no per-product list — the
# PM's own wording drives the filter, so adding "iCloud Photos",
# "Google Drive", "OneDrive", "Dropbox" or any other brand works the
# same way without code changes.


# Generic English words that get capitalised at sentence start but
# carry no product information. The capitalised-word extractor skips
# these so a sentence like "What is iCloud Photos" does not surface
# "What" as a phrase-token. The list is intentionally broad so the
# v3 evaluation's "Google Drive Help / Mobile / Cite" leakage stops
# happening — "Help" / "Mobile" / "Cite" / "Article" / "Page" /
# "Source" are generic English words that appear in many unrelated
# articles and would otherwise keep cross-product pages in a Drive /
# Photos / iCloud run.
_PHRASE_SKIP_WORDS: frozenset[str] = frozenset(
    {
        # Pronouns, determiners, auxiliaries
        "I",
        "We",
        "You",
        "He",
        "She",
        "They",
        "It",
        "This",
        "That",
        "The",
        "A",
        "An",
        "Is",
        "Are",
        "Was",
        "Were",
        "Be",
        "Been",
        "Do",
        "Does",
        "Did",
        "Have",
        "Has",
        "Had",
        "Will",
        "Would",
        "Can",
        "Could",
        "Should",
        "May",
        "Might",
        "Must",
        "What",
        "How",
        "Why",
        "When",
        "Where",
        "Which",
        "Who",
        "If",
        "Then",
        "So",
        "But",
        "And",
        "Or",
        "Not",
        "Yes",
        "No",
        "Ok",
        "OK",
        "My",
        "Our",
        "Your",
        "Their",
        "Hi",
        "Hello",
        "Thanks",
        "Please",
        "Note",
        "Notes",
        # Common UI / page nouns — never brand-bearing
        "Help",
        "Article",
        "Page",
        "Pages",
        "Post",
        "Posts",
        "Thread",
        "Threads",
        "Comment",
        "Comments",
        "Reply",
        "Replies",
        "View",
        "Read",
        "Update",
        "Updates",
        "Source",
        "Sources",
        "Reference",
        "References",
        # Generic descriptors that frequently appear in PM questions
        # but carry no product identity of their own.
        "Mobile",
        "Desktop",
        "Web",
        "App",
        "Apps",
        "Online",
        "Cite",
        "Quotes",
        "Quote",
        "New",
        "Latest",
        "Today",
        "Now",
        "Here",
        "Below",
        "Best",
        "Top",
        "Most",
        "More",
        "Less",
        "Many",
        "Vs",
        "Versus",
        "Review",
        "Reviews",
        "Guide",
        "Guides",
    }
)

# Quoted strings: "Google Photos" / 'iCloud Photos' / "Google Drive".
# The inner content is wrapped in a capture group so ``findall``
# returns only the unquoted phrase (without the surrounding quote
# characters — a non-capturing pattern would have ``findall`` emit
# the whole match, including the quote characters, defeating the
# filter's substring check downstream).
_QUOTED_PHRASE_RE = re.compile(r'[""' ']([^""' ']{1,80})[""' "]")

# Hyphenated alpha compounds: iCloud-Photos / Google-Drive / One-Drive.
# Hyphens are unusual in free-form Chinese text, so any hyphenated
# token is almost certainly a brand / product name. The first
# segment accepts either an upper-case starting letter or a lower-
# case letter followed by an upper-case letter so camelCase brands
# like ``iCloud-Photos`` are still captured.
_HYPHEN_PHRASE_RE = re.compile(
    r"(?<![\w-])([A-Za-z][A-Za-z0-9]+(?:-[A-Za-z][A-Za-z0-9]+){1,3})(?![\w-])"
)

# A run of one to four adjacent capitalised words. The regex joins
# adjacent runs so ``Google Photos`` is one phrase rather than two
# single-word phrases; in a Chinese sentence ``Google Photos 在国内``
# produces the single phrase ``Google Photos`` which is exactly what
# the snippet filter needs. Each word accepts either an upper-case
# starting letter (e.g. ``Google``, ``Photos``) OR a lower-case
# starting letter followed by an upper-case letter (e.g. ``iCloud``,
# ``eBay``) so camelCase brands are captured by the same regex.
_CAPITALISED_WORD_RE = re.compile(
    r"(?<![\w.,])((?:[A-Z][A-Za-z0-9]*|[a-z]+[A-Z][A-Za-z0-9]*)(?:\s+(?:[A-Z][A-Za-z0-9]*|[a-z]+[A-Z][A-Za-z0-9]*)){0,3})"
)

# Cap on the number of phrase-tokens returned. The orchestrator only
# needs a handful of phrases to filter the small source list; keeping
# the cap bounded prevents a PM question with dozens of proper nouns
# from forcing the filter to walk a long phrase list per source.
_PHRASE_MAX_TOKENS = 6

# Per-phrase length cap, kept in line with the DB-level domain cap so
# the filter cannot accidentally extract a giant URL-fragment as a
# phrase.
_PHRASE_TOKEN_MAX_LEN = 80

# Lowercased companion to :data:`_PHRASE_SKIP_WORDS`. The phrase
# extractor compares against the capitalised set because the
# capitalised-word regex (``_CAPITALISED_WORD_RE``) only ever emits
# tokens that already start with an upper-case letter, so the
# original casing matches. The phrase filter normalises caller-supplied
# phrases to lowercase BEFORE tokenising (so case differences in the
# caller's input cannot leak through), and the comparison must
# therefore use the lowercase skip set too. Sharing one set between
# the two passes caused ``'help' / 'mobile' / 'cite'`` to slip past
# the filter in v4 — fixed by routing the filter through this
# companion.
_PHRASE_SKIP_WORDS_LOWER: frozenset[str] = frozenset(w.lower() for w in _PHRASE_SKIP_WORDS)


def _extract_phrase_tokens(*texts: str | None) -> list[str]:
    r"""Return the phrase-tokens mentioned verbatim in the given texts.

    The extractor runs three sub-passes over each input:

    1. **Quoted strings** — verbatim substrings between balanced
       quotation marks (ASCII ``"``, Chinese ``"..."``, ``'...'``).
    2. **Hyphenated compounds** — tokens of the form ``Word-Word``
       (``iCloud-Photos``, ``Google-Drive``).
    3. **Capitalised word runs** — one to four adjacent words that
       each start with an uppercase letter
       (``Google Photos``, ``iCloud``, ``Dropbox``).

    The function never matches against ``\w`` / ``_`` / ``.``
    boundaries — i.e. ``Apple.Photos`` and ``Apple_Photos`` do not
    produce a phrase — and skips the generic-English-word set above.
    No product-specific knowledge is encoded; the result depends
    only on what the PM wrote.

    Returned tokens are sorted by descending length (longest first)
    so multi-word phrases are checked before single-word ones, and
    capped at :data:`_PHRASE_MAX_TOKENS`. Duplicates are removed
    case-insensitively. Empty / whitespace input is a no-op.
    """
    tokens: list[str] = []
    seen: set[str] = set()
    for raw_text in texts:
        if not raw_text:
            continue
        text = str(raw_text).strip()
        if not text:
            continue

        # Pass 1 — verbatim quoted strings.
        for match in _QUOTED_PHRASE_RE.findall(text):
            phrase = str(match).strip()
            if not phrase:
                continue
            if len(phrase) > _PHRASE_TOKEN_MAX_LEN:
                phrase = phrase[:_PHRASE_TOKEN_MAX_LEN].strip()
            if not phrase:
                continue
            key = phrase.lower()
            if key not in seen:
                tokens.append(phrase)
                seen.add(key)

        # Pass 2 — hyphenated compounds.
        for match in _HYPHEN_PHRASE_RE.findall(text):
            phrase = str(match).strip()
            if not phrase:
                continue
            sub_tokens = phrase.split("-")
            if any(token in _PHRASE_SKIP_WORDS for token in sub_tokens):
                continue
            if len(phrase) > _PHRASE_TOKEN_MAX_LEN:
                continue
            key = phrase.lower()
            if key not in seen:
                tokens.append(phrase)
                seen.add(key)

        # Pass 3 — adjacent capitalised word runs.
        for match in _CAPITALISED_WORD_RE.findall(text):
            phrase = str(match).strip()
            if not phrase:
                continue
            sub_tokens = phrase.split()
            # Strip stop-word tokens rather than dropping the whole
            # run — ``Google Drive Help`` keeps ``Google Drive``
            # (so brand identity survives a stop-word tag-along)
            # while ``Mobile`` / ``Cite`` / ``Article`` on their
            # own collapse to the empty list and are dropped.
            filtered = [t for t in sub_tokens if t not in _PHRASE_SKIP_WORDS]
            if not filtered:
                continue
            phrase = " ".join(filtered)
            if len(phrase) > _PHRASE_TOKEN_MAX_LEN:
                phrase = phrase[:_PHRASE_TOKEN_MAX_LEN].strip()
            if not phrase:
                continue
            key = phrase.lower()
            if key not in seen:
                tokens.append(phrase)
                seen.add(key)

        if len(tokens) >= _PHRASE_MAX_TOKENS:
            break

    # Longest first so multi-word phrases like ``Google Photos``
    # outrank the single-word ``Google`` / ``Photos`` (both of which
    # also match common URLs and would otherwise dominate).
    tokens.sort(key=lambda s: (-len(s), s.lower()))
    return tokens[:_PHRASE_MAX_TOKENS]


def _filter_sources_by_phrases(
    sources: list[ProviderSource],
    phrases: list[str],
) -> list[ProviderSource]:
    """Drop sources whose snippet / title / URL does not actually
    mention one of the supplied phrases.

    Each phrase is split on whitespace (and hyphen) into tokens.
    A source is kept iff every token of at least one phrase is
    present (substring, case-insensitive) in the source's URL +
    title + snippet haystack. Multi-word phrases
    (``iCloud Photos``, ``Google Drive``) therefore require every
    token to appear — a snippet containing only ``Photos`` is NOT
    treated as supporting ``iCloud Photos``.

    Stop-word tokens (``Mobile`` / ``Cite`` / ``Help`` / ``Article``
    ...) are stripped from the phrase list before matching. This
    is the defence-in-depth layer behind
    :data:`_PHRASE_SKIP_WORDS`: even if a caller (or a future
    extraction regression) passes ``['Google Drive Help',
    'Mobile', 'Cite']`` directly to this filter, the
    generic English tokens are removed so they cannot rescue a
    cross-product page. Multi-brand queries survive — ``Dropbox``
    and ``OneDrive`` are not stop-words, so a single-word brand
    phrase still rescues its own source.

    The filter is a no-op when:

    * the source list is empty, OR
    * the phrase list is empty / every phrase collapses to the
      empty list after stop-word stripping (no phrase-tokens
      were extractable).

    The LLM never sees dropped sources, so a source whose snippet
    is about a different product / platform cannot be cited.
    """
    if not sources or not phrases:
        return list(sources)
    cleaned = [str(p).strip() for p in phrases if p and str(p).strip()]
    if not cleaned:
        return list(sources)
    # Tokenise every phrase and strip stop-word tokens; drop phrases
    # that collapse to the empty list so they cannot match
    # anything. ``replace("-", " ")`` lets ``Google-Drive`` and
    # ``Google Drive`` share the same token stream. Stop-word
    # stripping uses the LOWERCASE companion set because the
    # phrase was just lowercased — see ``_PHRASE_SKIP_WORDS_LOWER``
    # for the rationale.
    tokenised: list[list[str]] = []
    for phrase in cleaned:
        tokens = phrase.lower().replace("-", " ").split()
        tokens = [t for t in tokens if t and t not in _PHRASE_SKIP_WORDS_LOWER]
        if tokens:
            tokenised.append(tokens)
    if not tokenised:
        return list(sources)
    kept: list[ProviderSource] = []
    for src in sources:
        haystack_parts: list[str] = [src.url or ""]
        if src.title:
            haystack_parts.append(src.title)
        if src.snippet:
            haystack_parts.append(src.snippet)
        haystack = "\n".join(haystack_parts).lower()
        supported = any(
            all(tok in haystack for tok in phrase_tokens) for phrase_tokens in tokenised
        )
        if supported:
            kept.append(src)
    return kept


# ---- Low-authority / community-domain filter ------------------------------
#
# The v3 evaluation surfaced competitors citing sources from
# user-generated content platforms (``sites.google.com``,
# ``blogspot.com``, hosted-page builders) that do not constitute
# "official documentation" the PM expected when asking about a
# public product. After the phrase filter narrows by topical
# relevance, this secondary filter drops sources from known
# community / user-generated platforms so the LLM is offered only
# authoritative-looking hits.
#
# This is generic — no product-specific knowledge is encoded. The
# list is small and bounded; adding a new entry only requires
# appending the suffix. ``derive_domain`` normalises the host
# before matching so casing differences (``SITES.GOOGLE.com``) and
# trailing dots do not produce false negatives.

_LOW_AUTHORITY_DOMAIN_SUFFIXES: tuple[str, ...] = (
    "sites.google.com",
    "blogspot.com",
    "wordpress.com",
    "wixsite.com",
    "weebly.com",
    "jimdosite.com",
    "webnode.com",
    "webflow.io",
    "github.io",
    "gitlab.io",
    "notion.site",
    "medium.com",
    "substack.com",
    "tumblr.com",
    "livejournal.com",
    "blogspot.co.uk",
    "googlepages.com",
    # User-created Google Docs / Sheets / Apps Script — the host
    # publishes whatever any logged-in user uploads, so a result
    # whose source lives on these hosts is community content rather
    # than an authoritative product page. The earlier list only
    # covered sites.google.com; v4 evaluation surfaced cases
    # where a docs.google.com / script.google.com / googleusercontent
    # link was the sole cited backing for an "official" claim,
    # which is exactly the leakage the denylist exists to block.
    "docs.google.com",
    "script.google.com",
    "docs.googleusercontent.com",
    # v4 evaluation: user-created Google Docs / Apps Script exports
    # live under random ``*.googleusercontent.com`` subdomains
    # (e.g. ``docssfvaueuee.googleusercontent.com``). Every file
    # under that host is uploaded by the document owner and
    # therefore not authoritative product documentation. The
    # suffix is intentionally broad — covering all
    # ``*.googleusercontent.com`` subdomains — because Google
    # assigns the random subdomain per export.
    "googleusercontent.com",
)


def _is_low_authority_domain(url: str) -> bool:
    """Return True when ``url`` points at a known user-generated /
    community-content platform.

    The platform list is generic — no product-specific knowledge
    is encoded. ``derive_domain`` is used (so the URL host is
    normalised for casing and trailing dots) before the suffix
    match.
    """
    if not url:
        return False
    try:
        from app.services.research_provider import derive_domain

        domain = derive_domain(url).lower()
    except Exception:
        return False
    if not domain:
        return False
    return any(
        domain == suffix or domain.endswith("." + suffix)
        for suffix in _LOW_AUTHORITY_DOMAIN_SUFFIXES
    )


def _filter_sources_by_low_authority(
    sources: list[ProviderSource],
) -> list[ProviderSource]:
    """Drop sources whose domain matches a known user-generated
    platform suffix.

    Applied AFTER the phrase-token filter so the orchestrator
    never silently swaps a community-page hit for an
    authoritative one — when the user explicitly asked for
    official documentation and the only returned hits are on
    user-generated platforms, the run finishes with
    ``source_count == 0`` and ``status='failed'`` (the same as
    any other provider-failure path) so the audit row reflects
    the real outcome.
    """
    if not sources:
        return list(sources)
    return [src for src in sources if not _is_low_authority_domain(src.url)]


# ---- Official-vendor site: constraint + path filter ----------------------
#
# v5 evaluation reproduced the same failure mode across five real
# Tavily queries: when the PM explicitly asks for "official
# documentation" / "仅官方" for a single product (Google Photos /
# Google Drive / Dropbox / OneDrive / iCloud Photos), the provider
# returned community threads, Q&A pages, user docs, YouTube
# transcripts and aggregator posts as the SOLE backing for the
# model output. A thread under ``support.google.com`` (e.g.
# ``community.support.google.com/thread/...``) is NOT an official
# help page; it is a user-to-user forum hosted on the same parent
# domain.
#
# Two deterministic layers close this gap:
#
# 1. **Query-level site: constraint** — when the question is a
#    single-product question asking for official documentation,
#    append ``site:<vendor-host>`` to the provider query so the
#    upstream ranking itself filters to the official help domain.
# 2. **Host + path filter** — after the provider returns, the
#    result is post-filtered so only URLs on the official vendor
#    host AND on an allowed path (the vendor's ``/answer/`` /
#    ``/guide/`` / product-root namespace) survive. Threads under
#    the same parent domain (``community.`` / ``answers.`` /
#    ``forum.`` / ``discuss.`` / ``q.``) are explicitly blocked.
#
# Multi-brand questions and non-"official" questions bypass these
# layers entirely (the existing phrase-token + low-authority
# filters still apply).

# Vendor rule table — the canonical product key maps to the
# official help host and the allowed URL-path prefixes. The
# path list is intentionally small: only the vendor's help-root
# namespace, the product namespace, and the manual ``/answer/``
# / ``/guide/`` paths. Adding a new product requires editing
# only this tuple; no orchestration / helper changes.
_OFFICIAL_VENDOR_RULES: dict[str, dict[str, Any]] = {
    "google_photos": {
        "site": "support.google.com",
        # ``/photos/answer/<id>`` is the canonical help namespace;
        # ``/photos/thread/<id>`` is a user thread on the same
        # parent domain — the path prefix alone is permissive
        # (any ``/photos/...`` URL would match), so the explicit
        # ``blocked_paths`` list below is what actually keeps
        # threads out of the cited-source set.
        "path_prefixes": ["/photos/answer/"],
        "blocked_paths": ["/photos/thread/"],
        "blocked_subdomains": (
            "community.",
            "answers.",
            "forum.",
            "discuss.",
            "q.",
            "productforums.",
        ),
        "product_keywords": ("google photos",),
    },
    "google_drive": {
        "site": "support.google.com",
        "path_prefixes": ["/drive/answer/"],
        "blocked_paths": ["/drive/thread/"],
        "blocked_subdomains": (
            "community.",
            "answers.",
            "forum.",
            "discuss.",
            "q.",
            "productforums.",
        ),
        "product_keywords": ("google drive",),
    },
    "onedrive": {
        "site": "support.microsoft.com",
        # Microsoft uses locale-prefixed paths
        # (``/en-us/onedrive/<article>``) AND bare
        # ``/onedrive/<article>`` aliases; the rule accepts both.
        "path_prefixes": [
            "/en-us/onedrive/",
            "/onedrive/",
            "/office/onedrive/",
        ],
        "blocked_subdomains": (
            "answers.",
            "social.",
            "techcommunity.",
            "forum.",
            "q.",
        ),
        "product_keywords": ("onedrive",),
    },
    "dropbox": {
        "site": "help.dropbox.com",
        # Dropbox help uses category-namespace paths
        # (``/create-upload/...``, ``/guide/...``, ``/sync/...``,
        # ``/share/...``, ``/install/...``, etc.). Threads live on
        # the forum host (``forums.dropbox.com``) which is already
        # blocked by ``blocked_subdomains``; this list is the
        # help-article allow-list so random non-help paths on
        # ``help.dropbox.com`` (e.g. ``/login``) do not leak in.
        "path_prefixes": [
            "/guide/",
            "/answer/",
            "/create-upload/",
            "/sync/",
            "/share/",
            "/install/",
            "/files-folders/",
            "/delete-restore/",
            "/account/",
            "/security/",
            "/plan-billing/",
            "/workspace/",
            "/integrations/",
            "/team-admin/",
            "/preview/",
            "/paper/",
        ],
        "blocked_subdomains": (
            "community.",
            "answers.",
            "forum.",
            "discuss.",
            "q.",
            "forums.",
        ),
        "product_keywords": ("dropbox",),
    },
    "icloud_photos": {
        "site": "support.apple.com",
        # Apple support uses locale-prefixed article paths
        # (``/en-us/<article-id>``, ``/zh-cn/...``) and guide
        # namespaces (``/guide/...``, ``/icloud/...``). The
        # locale-prefix list covers the article namespace;
        # the ``/guide/`` and ``/icloud/`` paths cover the
        # structured manual.
        "path_prefixes": [
            "/en-us/",
            "/zh-cn/",
            "/guide/",
            "/icloud/",
        ],
        "blocked_subdomains": (
            "discussions.",
            "answers.",
            "community.",
            "forum.",
            "q.",
        ),
        # v7 — the live-Tavily pass showed real PM questions
        # for iCloud Photos that do NOT contain the literal
        # phrase ``icloud photos``. They mention ``iCloud``,
        # ``icloud.com`` (the web Photos UI), the ``Shared
        # Photo Library`` feature name, or the generic
        # ``Apple Support`` brand. Each alias below is
        # checked as a lowercase substring against the
        # question + scope. The multi-product guard in
        # :func:`_detect_official_vendor_rule` still refuses
        # to narrow a multi-vendor question to a single rule,
        # so these aliases are safe to add.
        "product_keywords": (
            "icloud photos",
            "icloud",
            "icloud.com",
            "shared photo library",
            "apple support",
        ),
    },
}

# v14 — vendor-brand-citation keywords. These are the
# ``product_keywords`` entries that name the VENDOR's
# brand rather than a product surface (e.g. ``Apple
# Support`` cites the vendor brand; ``iCloud`` /
# ``iCloud.com`` / ``Shared Photo Library`` are actual
# product surfaces). The brand citation is excluded
# from the leftmost-product-keyword anchor and from the
# secondary-surface emission so the query body surfaces
# only the product surfaces, not the brand name. Adding
# a new vendor with a brand-citation keyword requires
# appending the lowercase form here.
_VENDOR_BRAND_CITATION_KEYWORDS: frozenset[str] = frozenset(
    {
        "apple support",
    }
)

# Markers that signal the PM wants only the official vendor
# documentation. The check is case-insensitive for English
# variants and case-sensitive for Chinese variants (the
# Chinese keywords are unambiguous in any cased form).
_OFFICIAL_KEYWORDS_EN: tuple[str, ...] = (
    "only official",
    "official documentation",
    "official docs",
    "official help",
    "official support",
    "vendor docs",
    "first-party",
)
_OFFICIAL_KEYWORDS_ZH: tuple[str, ...] = (
    "仅官方",
    "只官方",
    "官方文档",
    "官方帮助",
    "官方支持",
    "官方资料",
)


def _question_requests_official(question: str, scope: str | None) -> bool:
    """Return True when the PM's research question + scope contain
    an "official-only" marker.
    """
    text = " ".join(str(value).strip() for value in (question, scope) if value)
    if not text:
        return False
    text_lower = text.lower()
    if any(marker in text_lower for marker in _OFFICIAL_KEYWORDS_EN):
        return True
    if any(marker in text for marker in _OFFICIAL_KEYWORDS_ZH):
        return True
    return False


def _detect_official_vendor_rule(question: str, scope: str | None) -> dict[str, Any] | None:
    """Return the official vendor rule for a single-product
    "official-only" question. Return ``None`` when the question
    is multi-product, does not request official-only, or names
    no canonical product from :data:`_OFFICIAL_VENDOR_RULES`.

    Multi-product detection is conservative: the question must
    mention EXACTLY one of the canonical product keywords (after
    lowercasing) for the rule to fire. Two or more product hits
    keep the multi-brand path intact so a comparison question
    is not silently narrowed to one vendor.
    """
    if not _question_requests_official(question, scope):
        return None
    text = " ".join(str(value).strip() for value in (question, scope) if value)
    text_lower = text.lower()
    matched: list[dict[str, Any]] = []
    for rule in _OFFICIAL_VENDOR_RULES.values():
        for keyword in rule["product_keywords"]:
            if keyword in text_lower:
                matched.append(rule)
                break
    if len(matched) == 1:
        return matched[0]
    return None


def _vendor_site_query_suffix(rule: dict[str, Any]) -> str:
    """Return the ``site:<host>`` token appended to the provider
    query for the rule's official vendor.
    """
    return f"site:{rule['site']}"


# v7 — the live-Tavily pass showed the previous
# ``<full PM question> site:<host>`` shape over-weighted the
# PM's verbose instructions (the Google Drive question was
# 281 chars; the resulting query returned community threads
# and a Google Doc, then the path filter dropped everything).
# The short focused query below is the v7 fix: take the
# product keyword from the question + a bounded window of
# the next content tokens + a site:host/path constraint.
# The shape mirrors the working Tavily call
# ``Google Drive Android upload multiple files photos
# site:support.google.com/drive/answer`` that the v7 audit
# surfaced as the canonical official-only query.
_OFFICIAL_QUERY_PRODUCT_TOKENS: tuple[str, ...] = (
    # Words that, when present in the question, are
    # uninformative for the official search. The list is
    # deliberately small: removing too many tokens strips
    # the platform / action signal that the v7 audit said
    # the search needed (e.g. ``Android``).
    "how",
    "to",
    "do",
    "can",
    "i",
    "we",
    "you",
    "the",
    "a",
    "an",
    "is",
    "are",
    "be",
    "been",
    "being",
    "on",
    "in",
    "at",
    "of",
    "for",
    "and",
    "or",
    "but",
    "if",
    "not",
    "no",
    "this",
    "that",
    "these",
    "those",
    "with",
    "from",
    "by",
    "as",
    "it",
    "its",
    "their",
    "his",
    "her",
    "support",
    "help",
    "docs",
    "documentation",
    "guide",
    "tutorial",
    "question",
    "questions",
    "issue",
    "issues",
    "problem",
    "problems",
    # v9 — PM-instruction noise that appears in
    # ``Cite only official Microsoft Support`` / ``provide
    # official docs please`` style requests. These words are
    # part of the PM's CITATION INSTRUCTION, not the
    # research subject — they must NOT bleed into the
    # Tavily query when the fallback path (no recognised
    # platform / action token) appends a window of
    # original-text tokens after the product anchor.
    # ``cite`` is the citation instruction verb; ``only``
    # is the qualifier; ``official`` is the request-mode
    # marker (the official-only trigger is detected
    # separately upstream by ``_question_requests_official``
    # so by the time we get here ``official`` is redundant
    # context). All three are lowercased on comparison
    # via ``stripped.lower() in _OFFICIAL_QUERY_PRODUCT_TOKENS``.
    "cite",
    "only",
    "official",
    "请问",
    "如何",
    "怎么",
    "为什么",
    "是否",
    "可以",
    "能否",
    "不能",
    "的",
    "了",
    "吗",
    "呢",
    "在",
    "是",
    "我",
    "你",
    "他",
    "她",
    "它",
    "们",
    "和",
    "与",
    "或",
    "但",
    "不",
    "没",
    "也",
    "都",
    "就",
    "要",
    "会",
    "能",
    "请",
    "帮助",
    "文档",
    "指南",
    "教程",
    "问题",
    "支持",
)
_OFFICIAL_QUERY_MAX_BODY_CHARS = 80

# Round 3 — bilingual action-word map. Real PM questions mix
# English and Chinese freely; Chinese action terms are mapped
# to their English equivalents so the upstream Tavily query
# uses the SAME canonical English verbs / nouns the vendor's
# own English help pages use. The mapping is the ONLY place
# these terms are encoded — no per-product knowledge is added
# so a new language / alias only requires appending to this
# dict. Each value is a single English token so the upstream
# query shape stays short and the dedup in
# :func:`_scan_semantic_terms` is straightforward.
_ACTION_TERM_CJK_TO_EN: dict[str, str] = {
    "上传": "upload",
    "下载": "download",
    "选择": "select",
    "多选": "select",
    "多张": "multiple",
    "多个": "multiple",
    "批量": "batch",
    "照片": "photos",
    "图片": "photos",
    "文件": "files",
    "备份": "backup",
    "共享": "share",
    "分享": "share",
    "同步": "sync",
    "删除": "delete",
    "编辑": "edit",
    "查看": "view",
    "存储": "storage",
}

# Round 3 — English action terms recognised in the PM's
# question. v14 — the scanner was a single whole-word
# alternation (``\b<term>\b``) that dropped the conjugated
# forms ``uploads`` / ``manually`` (real PM-question
# usage). v14a widened it to ``\b<term>\w*\b`` but that
# surfaced spurious words inside OTHER tokens —
# ``viewpoint`` matched ``view``, ``shareholder`` matched
# ``share``, ``editorial`` matched ``edit``,
# ``storagecorp`` matched ``storage``. v14b replaces
# BOTH with a per-canonical FINITE alternation: each
# canonical action term has a small explicit list of
# accepted surface forms; each form is matched as a
# whole word. The canonical (lowercase) form is what is
# emitted into the upstream query so the shape stays
# uniform. A surface form appearing in the question
# maps to its canonical key (e.g. ``manually`` →
# ``manual``); unknown suffixes do NOT map to anything
# and are not added to the action set.
_ACTION_TERM_FORMS: dict[str, tuple[str, ...]] = {
    "manual": ("manual", "manually"),
    "upload": ("upload", "uploads", "uploaded", "uploading"),
    "add": ("add", "adds", "added", "adding"),
    "select": ("select", "selects", "selected", "selecting"),
    "download": ("download", "downloads", "downloaded", "downloading"),
    "share": ("share", "shares", "shared", "sharing"),
    "sync": ("sync", "syncs", "synced", "syncing"),
    "delete": ("delete", "deletes", "deleted", "deleting"),
    "edit": ("edit", "edits", "edited", "editing"),
    "view": ("view", "views", "viewed", "viewing"),
    "photos": ("photos", "photo"),
    "files": ("files", "file"),
    "backup": ("backup", "backups"),
    "multiple": ("multiple", "multi"),
    "batch": ("batch", "batches"),
    "storage": ("storage",),
}

# Round 3 — English platform terms preserved in their
# canonical capitalization. These are platform signals the
# upstream ranking uses to disambiguate mobile / desktop /
# OS-specific help pages. A PM question that mentions
# ``Android`` always carries the platform token through to
# the upstream query even when the surrounding sentence is
# verbose (the Round 2 8-token budget would have dropped it
# in a long PM question — that regression is what the
# semantic-priority approach fixes).
_PLATFORM_TERMS_EN: tuple[str, ...] = (
    "Android",
    "iPhone",
    "iOS",
    "iPad",
    "Mac",
    "Windows",
    # v14 — ``Mobile`` is recognised as a platform signal so
    # the v14b Dropbox regression (``mobile users manually
    # add photos``) carries the ``Mobile`` token into the
    # body of the official provider query. Without this
    # entry, the upstream ranking would drop the platform
    # signal and surface unrelated ``/photos-not-appearing``
    # / ``/ios-formats`` pages instead of the canonical
    # ``/create-upload/upload-mobile`` page. Canonical form
    # is emitted in original capitalization.
    "Mobile",
)


def _scan_semantic_terms(body: str) -> tuple[list[str], list[str]]:
    """Return ``(platform_tokens, action_tokens)`` detected in
    ``body``.

    The scanner is the Round 3 replacement for the Round 2
    per-token stop-word drop + 8-token take-first-N budget.
    The new shape is *semantic priority*: the body is
    decomposed into recognised product / platform / action
    tokens, the verbose PM-internal language
    (``我们的用户在 Android 客户端``, ``请提供详细步骤``,
    ``app, user, device, from, which, to``) is dropped
    because none of its words is in the recognised set, and
    the resulting query always carries ``<product>
    <platform> <action tokens>``.

    Order: each list is in order of FIRST appearance in
    ``body`` so the upstream query retains the PM's
    positioning of product → platform → action (the canonical
    short Tavily call the v7 audit found to return
    ``/drive/answer/2424368`` followed this shape).
    Duplicates are dropped case-insensitively.

    Platform terms are emitted in canonical capitalization
    (``Android`` not ``ANDROID``). Action terms are emitted
    in their canonical English form regardless of the case
    the PM used (so the upstream query looks uniform even
    when the PM wrote ``Upload`` / ``UPLOAD``).
    """
    platforms: list[str] = []
    actions: list[str] = []
    seen_platforms: set[str] = set()
    seen_actions: set[str] = set()
    # ``hits`` accumulates every recognised term with its
    # ``[start, end)`` span and category so the sort below
    # can walk the body left-to-right and de-duplicate.
    hits: list[tuple[int, int, str, str]] = []

    # CJK action terms — substring scan. CJK runs have no
    # whitespace between chars so a word-boundary regex
    # would not work; substring ``find`` is the equivalent
    # tokenisation. ``idx = pos + len(cjk_term)`` advances
    # PAST the match so a longer match at the same start
    # position is still picked up by the outer loop (e.g.
    # ``图片`` after ``图片库``).
    for cjk_term, en_term in _ACTION_TERM_CJK_TO_EN.items():
        idx = 0
        while True:
            pos = body.find(cjk_term, idx)
            if pos == -1:
                break
            hits.append((pos, pos + len(cjk_term), "action", en_term))
            idx = pos + len(cjk_term)

    body_lc = body.lower()

    # English action terms — case-insensitive whole-word
    # match against a per-canonical FINITE alternation
    # (:data:`_ACTION_TERM_FORMS`). Each canonical action
    # term has a small explicit list of accepted surface
    # forms (``upload``/``uploads``/``uploaded`` /
    # ``uploading`` etc.); the regex matches any of those
    # forms as a WHOLE WORD and emits the canonical
    # lowercase key. Crucially the alternation does NOT
    # use ``\w*`` — that would let ``viewpoint`` match
    # ``view`` / ``shareholder`` match ``share`` /
    # ``editorial`` match ``edit`` / ``storagecorp`` match
    # ``storage``. With the explicit finite set, those
    # spurious hits do not surface.
    for canonical, forms in _ACTION_TERM_FORMS.items():
        pattern = re.compile(
            r"\b(?:" + "|".join(re.escape(f) for f in forms) + r")\b",
            re.IGNORECASE,
        )
        for match in pattern.finditer(body_lc):
            hits.append((match.start(), match.end(), "action", canonical))

    # English platform terms — case-insensitive whole-word
    # match; the emitted token uses the CANONICAL
    # capitalization from :data:`_PLATFORM_TERMS_EN`.
    for platform_term in _PLATFORM_TERMS_EN:
        pattern = re.compile(r"\b" + re.escape(platform_term) + r"\b", re.IGNORECASE)
        for match in pattern.finditer(body_lc):
            hits.append((match.start(), match.end(), "platform", platform_term))

    # Sort by position; ties broken by longer-first so a
    # longer platform match (e.g. ``iPhone``) outranks a
    # shorter overlapping one (e.g. ``iPad`` at the same
    # start, if such overlap ever occurred).
    hits.sort(key=lambda h: (h[0], -h[1]))

    for _start, _end, kind, en_term in hits:
        if kind == "platform":
            key = en_term.lower()
            if key not in seen_platforms:
                seen_platforms.add(key)
                platforms.append(en_term)
        else:
            key = en_term.lower()
            if key not in seen_actions:
                seen_actions.add(key)
                actions.append(en_term)
    return platforms, actions


def _build_official_provider_query(
    question: str,
    scope: str | None,
    rule: dict[str, Any],
) -> str:
    """Build a short focused provider query for an official-only
    research run.

    Round 3 / v9 — the body is composed by **semantic
    priority**:

    * **Product** tokens come from the rule's
      ``product_keywords`` (matched leftmost / earliest
      against the question) and are emitted from the
      ORIGINAL case slice of the question text so
      ``Google Drive`` / ``OneDrive`` / ``iCloud`` /
      ``Dropbox`` keep the capitalisation the PM wrote.
    * **Platform** tokens are recognised English platform
      names (Android / iPhone / iOS / iPad / Mac /
      Windows) preserved in canonical capitalization.
    * **Action** tokens are recognised English action
      verbs / nouns (``upload`` / ``select`` / ``multiple``
      / ``photos`` / ``files`` / ``backup`` etc.) AND
      Chinese action terms mapped to those English
      equivalents via :data:`_ACTION_TERM_CJK_TO_EN` so
      the upstream Tavily query uses the SAME canonical
      English terms the vendor's own help pages use, even
      when the PM wrote the question in Chinese.
    * The platform / action scanner runs over the FULL
      question text, NOT just the tail after the product
      keyword. v9 audit fix — real PM questions routinely
      lead with the audience / verb before naming the
      product (``an iPhone user select existing pictures
      for a manual upload in OneDrive?`` has iPhone /
      select / upload BEFORE ``OneDrive``). Scanning only
      the post-product tail dropped these critical tokens
      and forced the fallback path to leak PM-instruction
      noise into the upstream query. Full-text scan keeps
      the canonical ``<product> <platform> <action>``
      shape with platform / action signals surfaced
      regardless of PM positioning.
    * The site constraint is the rule's host joined with
      the first (most specific) ``path_prefixes`` entry,
      with the trailing slash stripped via ``rstrip("/")``
      — Tavily treats the un-stripped
      ``site:support.google.com/drive/answer/`` as
      STRICTER than the canonical help path and would
      otherwise over-narrow the upstream ranking. v9
      adds a Dropbox-specific override: when one of
      ``upload`` / ``photos`` / ``files`` is in the
      recognised action set, the site path re-routes to
      ``/create-upload`` (where the canonical
      ``/create-upload/upload-mobile`` page lives)
      instead of the default ``/guide/``. The
      post-filter URL allow-list is unchanged — both
      namespaces are already in ``path_prefixes``.

    PM-internal background (``我们的用户在 Android 客户端``,
    ``请提供详细步骤以及限制条件``, ``app, user, device,
    from, which, to``, ``Cite only official Microsoft
    Support``) is dropped because none of those words is
    in the recognised product / platform / action sets,
    OR is in the v9-extended stop-word list
    :data:`_OFFICIAL_QUERY_PRODUCT_TOKENS` (which now also
    drops ``cite`` / ``only`` / ``official`` so the
    fallback path does not leak PM citation instructions).

    Fallback (no known action AND no known platform) — a
    small window (4 tokens) of original text after the
    product keyword is appended with the English / Chinese
    stop-word list dropped per token. The window is bounded
    so a verbose question with no recognisable action /
    platform still produces a focused upstream query
    rather than re-leaking the full question.

    Length safety net — the 80-char last-whitespace cap
    (:data:`_OFFICIAL_QUERY_MAX_BODY_CHARS`) is applied
    AFTER semantic-priority selection so a question with
    many recognised terms still produces a bounded body.

    The final result is then capped at 500 chars to fit
    :data:`QUERY_MAX_LENGTH`. The function is deterministic
    so the audit row's ``provider_query`` field matches
    the call that actually went upstream.
    """
    text = (question or "").strip()
    if not text:
        text = (scope or "").strip()
    # v9 — the ``site:`` constraint path is the vendor's
    # help-root namespace used by the URL filter
    # (``_is_official_vendor_path``). The rules keep the
    # canonical trailing slash there because that is what
    # the URL prefix match expects. For the ``site:``
    # constraint, however, Tavily treats
    # ``site:support.google.com/drive/answer/`` as STRICTER
    # than ``site:support.google.com/drive/answer`` —
    # appending the trailing slash narrows the upstream
    # match to that exact sub-path and ranked community
    # threads + Google Docs above the official
    # ``/drive/answer/2424368`` page in the v7 audit.
    # Strip the trailing slash ONLY for the ``site:``
    # join so the URL-path allow-list behaviour is
    # unchanged. ``site_path`` is the default
    # ``path_prefixes[0]`` join; the Dropbox-specific
    # override below may replace it BEFORE the scan above
    # needs the action set, so we resolve the path AFTER
    # scanning (see below).
    if not text:
        # No question / scope at all — fall back to the
        # site constraint alone so the audit row is still
        # meaningful.
        site_path_default = (
            str(rule["path_prefixes"][0]).rstrip("/") if rule.get("path_prefixes") else ""
        )
        return f"site:{rule['site']}{site_path_default}"[:500]

    text_lower = text.lower()
    product_keyword: str | None = None
    product_idx = -1
    for keyword in rule.get("product_keywords") or ():
        idx = text_lower.find(str(keyword).lower())
        if idx != -1 and (product_idx == -1 or idx < product_idx):
            product_keyword = keyword
            product_idx = idx
    # v14 — vendor-brand-citation demotion. PM questions
    # routinely cite the vendor's brand
    # ("According to official Apple Support") BEFORE the
    # product name (``iCloud.com`` / ``Shared Photo
    # Library``). The leftmost-product-keyword rule would
    # otherwise surface the vendor brand in the body so
    # the upstream ranking ranks the vendor's brand page
    # above the actual product documentation. When the
    # chosen keyword is the vendor-brand citation, prefer
    # an iCloud-subject keyword (``icloud`` /
    # ``icloud.com`` / ``icloud photos``) — the LONGEST
    # such match wins (``iCloud.com`` is more specific
    # than ``iCloud`` because it tells the upstream
    # ranking the WEB Photos UI rather than the generic
    # iCloud subject). When none of the iCloud-subject
    # keywords matches the question, fall back to the
    # LONGEST specific match (e.g. ``Shared Photo
    # Library``). The post-filter URL allow-list is
    # unchanged because the rule's ``product_keywords``
    # ALREADY lists both the brand and the specific
    # iCloud names (no new entry is added here).
    if product_keyword is not None and product_keyword.lower() == "apple support":
        _specific: str | None = None
        _specific_idx = -1
        _specific_len = -1
        # Prefer the LONGEST product_keyword whose
        # lowercase form contains ``icloud`` so the body
        # surfaces the actual iCloud subject (the brand
        # citation has already been excluded). Ties
        # broken by leftmost-position so the
        # first-mentioned surface wins when two iCloud
        # keywords have the same length.
        for keyword in rule.get("product_keywords") or ():
            if str(keyword).lower() == "apple support":
                continue
            if "icloud" not in str(keyword).lower():
                continue
            idx = text_lower.find(str(keyword).lower())
            if idx == -1:
                continue
            if (
                _specific is None
                or len(keyword) > _specific_len
                or (len(keyword) == _specific_len and idx < _specific_idx)
            ):
                _specific = keyword
                _specific_idx = idx
                _specific_len = len(keyword)
        # Fallback — longest specific match (e.g. the
        # question only mentions ``Shared Photo
        # Library``).
        if _specific is None:
            for keyword in rule.get("product_keywords") or ():
                if str(keyword).lower() == "apple support":
                    continue
                idx = text_lower.find(str(keyword).lower())
                if idx != -1 and len(keyword) > _specific_len:
                    _specific = keyword
                    _specific_idx = idx
                    _specific_len = len(keyword)
        if _specific is not None:
            product_keyword = _specific
            product_idx = _specific_idx
    # v9 — platforms / actions are populated only inside
    # the ``else`` branch below (when a product keyword is
    # found). They default to empty so the Dropbox path
    # re-route at the bottom of the function can read them
    # without a ``NameError`` when the product was not
    # found in the question.
    platforms: list[str] = []
    actions: list[str] = []
    if product_keyword is None or product_idx == -1:
        # No product keyword in the question — use the
        # vendor site name as the body so the query still
        # names the vendor.
        body = rule["site"]
    else:
        # v9 — scan platforms and actions over the FULL
        # question text, NOT just the tail after the
        # product keyword. Real PM questions routinely
        # lead with the audience / verb before naming the
        # product (``an iPhone user select existing
        # pictures for a manual upload in OneDrive?`` has
        # iPhone / select / upload BEFORE ``OneDrive``).
        # Scanning only the post-product tail dropped
        # these critical action tokens and forced the
        # fallback path to leak PM-instruction noise
        # (``Cite only official Microsoft Support``) into
        # the upstream query. Scanning the full text
        # captures them in first-appearance order, which
        # keeps the canonical ``<product> <platform>
        # <action>`` shape with the platform / action
        # signals surfaced regardless of PM positioning.
        # The product anchor itself is still emitted from
        # the original-case slice below so the
        # ``OneDrive`` / ``Google Drive`` / ``iCloud``
        # capitalisation the PM wrote is preserved.
        platforms, actions = _scan_semantic_terms(text)
        # Round 3 — preserve the user's original casing
        # for the product anchor. ``product_keyword``
        # comes from ``rule['product_keywords']`` which is
        # the SEARCH key (lowercased: ``"google drive"``
        # / ``"onedrive"`` / ``"icloud"`` etc.) used only
        # to find the match position in ``text_lower``.
        # The emitted product tokens come from the
        # ORIGINAL slice of ``text`` at that position so
        # ``Google Drive`` / ``OneDrive`` / ``iCloud``
        # stay capitalised in the upstream query the PM
        # (and the audit integration tests) expect.
        # ``len(product_keyword)`` is the same in both
        # casings (the keyword is ASCII) so the slice
        # spans the exact same characters as the lowercase
        # match.
        product_original = text[product_idx : product_idx + len(product_keyword)]
        product_tokens = product_original.split()
        # v14 — emit OTHER product_keyword matches as
        # additional product tokens. The PM's question may
        # name multiple surfaces from the SAME vendor
        # (e.g. ``iCloud.com`` and ``Shared Photo
        # Library``) for parallel sub-flows. The body must
        # surface EACH surface's full original-case slice
        # so the upstream ranking scores each flow
        # separately. Constraints:
        # * the secondary's position MUST NOT overlap the
        #   primary's span (so we do not double-count a
        #   substring of the primary as a separate
        #   surface).
        # * the secondary MUST NOT be a brand-citation
        #   keyword (the brand citation has been
        #   demoted above; the secondary loop must honour
        #   the same demotion so ``Apple Support`` does
        #   not sneak back in as a secondary surface).
        # * only same-vendor product_keywords are
        #   considered (the rule's product_keywords are
        #   the vendor's own surfaces — cross-vendor
        #   leakage is rejected by the multi-vendor
        #   guard in :func:`_detect_official_vendor_rule`
        #   before this function is called).
        # The order is primary first, then secondaries
        # in question-appearance order so the upstream
        # query retains the PM's positioning of the
        # surfaces.
        primary_start = product_idx
        primary_end = product_idx + len(product_keyword)
        _seen_lower: set[str] = {product_keyword.lower()}
        _extras: list[tuple[int, str]] = []
        for keyword in rule.get("product_keywords") or ():
            kl = str(keyword).lower()
            if kl in _seen_lower:
                continue
            if kl in _VENDOR_BRAND_CITATION_KEYWORDS:
                continue
            idx = text_lower.find(kl)
            if idx == -1:
                continue
            if primary_start <= idx < primary_end:
                # Substring of the primary's span — skip
                # to avoid emitting the same surface twice
                # (e.g. ``icloud`` inside the primary
                # ``icloud.com``).
                continue
            _seen_lower.add(kl)
            _extras.append((idx, text[idx : idx + len(keyword)]))
        _extras.sort(key=lambda e: e[0])
        for _, original in _extras:
            product_tokens.append(original)
        if actions or platforms:
            # Best case: at least one recognised platform
            # / action term — emit
            # ``<product> <platform> <action>`` in
            # first-appearance order. No take-first-N
            # token budget: every recognised term is kept
            # so a verbose Chinese question with many
            # action terms still surfaces them all in the
            # upstream query (the 80-char last-whitespace
            # cap below is the only length control). PM
            # instruction noise (``Cite only official
            # Microsoft Support``) does NOT leak here
            # because ``candidate_tokens`` only contains
            # recognised platform / action / product
            # tokens — nothing from the instruction tail.
            candidate_tokens = product_tokens + platforms + actions
        else:
            # Fallback: no recognisable action OR platform
            # token. Append a small window (max 4 tokens)
            # of original text after the product keyword,
            # with the small stop-word list
            # :data:`_OFFICIAL_QUERY_PRODUCT_TOKENS`
            # dropped per token. v9 — the stop-word list
            # now also drops ``cite`` / ``only`` /
            # ``official`` so the fallback path no longer
            # leaks the PM's citation instruction
            # (``Cite only official Microsoft Support``)
            # into the upstream query. The window is
            # bounded so a verbose question with no
            # recognisable action / platform still
            # produces a focused upstream query rather
            # than re-leaking the full PM question.
            tail = text[product_idx + len(product_keyword) :].strip()
            fallback_tokens: list[str] = []
            for token in tail.split():
                stripped = token.strip()
                if not stripped:
                    continue
                # Lowered exact-match against the
                # stop-word tuple — English stop words
                # match the lowercase-token form; CJK
                # stop words match the whole-token form.
                if stripped.lower() in _OFFICIAL_QUERY_PRODUCT_TOKENS:
                    continue
                fallback_tokens.append(stripped)
                if len(fallback_tokens) >= 4:
                    break
            candidate_tokens = product_tokens + fallback_tokens
        candidate_body = " ".join(candidate_tokens).strip()
        # Length safety net — last-whitespace boundary so
        # the final token is not truncated mid-word /
        # mid-CJK-run. Applied AFTER semantic-priority
        # selection so a question with many recognised
        # terms still produces a bounded body.
        if len(candidate_body) > _OFFICIAL_QUERY_MAX_BODY_CHARS:
            truncated = candidate_body[:_OFFICIAL_QUERY_MAX_BODY_CHARS]
            last_space = max(
                truncated.rfind(" "),
                truncated.rfind("　"),
            )
            if last_space > 0:
                candidate_body = truncated[:last_space].rstrip()
            else:
                candidate_body = truncated
        body = candidate_body or product_keyword
    # v9 — resolve the ``site:`` constraint path. The
    # default is ``rule['path_prefixes'][0].rstrip('/')``
    # (most specific namespace, no trailing slash — see the
    # rationale above). Dropbox has multiple topical
    # namespaces (``/guide/`` / ``/create-upload/`` /
    # ``/sync/`` / ``/share/`` ...). When the question is
    # about file / photo upload (one of ``upload`` /
    # ``photos`` / ``files`` is in the recognised action
    # set) the canonical Dropbox help article lives under
    # ``/create-upload/`` (e.g.
    # ``https://help.dropbox.com/create-upload/upload-mobile``)
    # NOT under the default ``/guide/`` which returns
    # generic top-level articles the post-filter still
    # passes but the upstream ranking never scores as
    # highly. Re-route the ``site:`` constraint to the
    # action-specific path for Dropbox upload questions
    # so the ranking surfaces the more specific page
    # first. The post-filter URL allow-list
    # (``_is_official_vendor_path``) is UNCHANGED — both
    # ``/guide/`` and ``/create-upload/`` are already in
    # ``path_prefixes``, so URLs from either namespace
    # still pass the filter. Other actions (``share`` /
    # ``sync`` / ``backup`` etc.) stay on the default
    # ``/guide/`` path. The match is data-driven via
    # ``rule['site']`` so adding a new vendor with a
    # similar upload namespace requires no code change.
    site_path = str(rule["path_prefixes"][0]).rstrip("/") if rule.get("path_prefixes") else ""
    if rule.get("site") == "help.dropbox.com" and any(
        a in actions for a in ("upload", "photos", "files")
    ):
        site_path = "/create-upload"
    site_constraint = f"site:{rule['site']}{site_path}"
    query = f"{body} {site_constraint}".strip()
    return query[:500]


def _is_official_vendor_host(url: str, rule: dict[str, Any]) -> bool:
    """Return True when ``url`` is hosted on the rule's official
    vendor domain (excluding the community / Q&A / forum sub-
    domains the rule explicitly blocks).
    """
    if not url:
        return False
    try:
        from app.services.research_provider import derive_domain

        host = derive_domain(url).lower()
    except Exception:
        return False
    if not host:
        return False
    site = str(rule["site"]).lower()
    blocked = tuple(rule.get("blocked_subdomains") or ())
    if host == site:
        return True
    if host.endswith("." + site):
        for prefix in blocked:
            if host.startswith(prefix):
                return False
        return True
    return False


def _is_official_vendor_path(url: str, rule: dict[str, Any]) -> bool:
    """Return True when ``url``'s path falls under the rule's
    allowed path-prefix set AND does not match any blocked-path
    prefix.

    The blocked-path check is a defence-in-depth gate for paths
    that share a parent namespace with an allowed path but are
    semantically community content (e.g. ``/photos/thread/<id>``
    lives under ``/photos/...`` and would otherwise sneak past
    the host filter because the host is still the vendor's
    own help domain). When the rule does not declare a
    ``blocked_paths`` list the check is a no-op.
    """
    if not url:
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    path = (parsed.path or "/").lower()
    # Strip trailing whitespace; the comparison is purely on
    # path-prefix so query-string / fragment do not affect
    # the result.
    path = path.strip() or "/"
    for blocked in rule.get("blocked_paths") or ():
        blocked_lower = str(blocked).lower()
        if path == blocked_lower or path.startswith(blocked_lower):
            return False
    for prefix in rule.get("path_prefixes") or ():
        prefix_lower = str(prefix).lower()
        if path == prefix_lower or path.startswith(prefix_lower):
            return True
    return False


def _all_cited_sources_official(
    source_refs: tuple[tuple[str, str], ...],
    allowed_provider_urls: dict[str, str] | None,
) -> bool:
    """Return True iff every cited source's URL is in the
    official vendor allowlist :data:`_OFFICIAL_VENDOR_RULES`
    (host + path both pass the rule's filters).

    Used by the v12 description-ground gate to gate the
    ``"From the official help page."`` non-claiming prefix
    carve-out — the prefix is a citation acknowledgement that
    is only honest when the cited source is genuinely an
    official vendor page. A missing or empty
    ``allowed_provider_urls`` map returns False (we cannot
    confirm the source is official without the URL).
    """
    if not source_refs or not allowed_provider_urls:
        return False
    for provider_source_id, _ in source_refs:
        url = allowed_provider_urls.get(provider_source_id, "")
        if not url:
            return False
        matched = False
        for rule in _OFFICIAL_VENDOR_RULES.values():
            if not _is_official_vendor_host(url, rule):
                continue
            # When the rule declares ``path_prefixes`` the
            # path must match; rules with no ``path_prefixes``
            # accept any path on the official host.
            path_prefixes = rule.get("path_prefixes") or ()
            if path_prefixes and not _is_official_vendor_path(url, rule):
                continue
            matched = True
            break
        if not matched:
            return False
    return True


def _filter_sources_to_official_vendor(
    sources: list[ProviderSource], rule: dict[str, Any]
) -> list[ProviderSource]:
    """Drop sources whose host or path does not fall under the
    official vendor's allowed namespace.

    Applied AFTER the low-authority filter so the orchestrator
    never silently swaps a community thread on the vendor's own
    parent domain for an authoritative product page — when the
    user explicitly asked for official documentation and the
    only returned hits are community / Q&A / user docs, the run
    finishes with ``source_count == 0`` and the existing
    completion gate stamps ``status='partial_failed'`` with the
    :data:`INSUFFICIENT_EXTERNAL_EVIDENCE_CODE` reason so the
    audit row reflects the real outcome (we did not pretend to
    have official evidence when we did not).
    """
    if not sources:
        return list(sources)
    kept: list[ProviderSource] = []
    for src in sources:
        if not _is_official_vendor_host(src.url, rule):
            continue
        if not _is_official_vendor_path(src.url, rule):
            continue
        kept.append(src)
    return kept


# ---- Evidence-quote extraction -------------------------------------------
#
# v5 prompt requires confirmed findings to embed a verbatim source
# quote in the description using the fixed format
# ``Evidence: "..."``. The validator extracts the quote and
# verifies it appears character-for-character in at least one
# cited source's haystack (title + url + domain + snippet). This
# prevents the v4 isolated experiment's failure mode where the
# model cited a source but the description contained no actual
# quote — the gate now refuses to mark a finding ``confirmed``
# unless the quote traces back to a real snippet.

_EVIDENCE_QUOTE_RE = re.compile(
    r"""Evidence\s*:\s*["“]([^"”]{1,200})["”]""",
    re.IGNORECASE,
)


# v12 — multi-select question guard. The brief carves out a
# specific problem guard: when the PM's research_question
# explicitly asks about multi-select / batch / multiple / several
# (``多选`` / ``批量`` / ``multiple`` / ``several`` /
# ``multi-select`` / ``多个`` / ``多重``), a confirmed finding's
# ``Evidence: "..."`` quote MUST directly contain a quantifier
# equivalent — generic plural ``files`` / ``tap files`` is NOT
# sufficient because the cited snippet only proves a tap / single
# interaction, not the multi-select capability the PM asked
# about. Non-multi-select questions are unaffected.
#
# The detection is keyword-based (not a general semantic
# classifier) so it cannot over-fire on paraphrased questions;
# the brief explicitly carves it out as a "conservative specific
# problem guard, not a general semantic entailment claim".
_MULTISELECT_QUESTION_PATTERNS: tuple[str, ...] = (
    r"多选",
    r"批量",
    r"多个",
    r"多重",
    r"multi[-\s]?select",
    r"\bmultiple\b",
    r"\bseveral\b",
    r"\bbatch\b",
)
_MULTISELECT_QUESTION_RE = re.compile(
    "|".join(_MULTISELECT_QUESTION_PATTERNS),
    re.IGNORECASE,
)

# Quantifier tokens that must appear in the verbatim ``Evidence:
# "..."`` quote for the finding to count as grounded on a
# multi-select question. Generic plurals (``files`` / ``photos``)
# are deliberately excluded because they prove only a generic
# plural, not the multi-select capability.
_QUANTIFIER_TOKENS_RE = re.compile(
    "|".join(_MULTISELECT_QUESTION_PATTERNS),
    re.IGNORECASE,
)


def _is_multi_select_question(research_question: str | None) -> bool:
    """Return True when ``research_question`` explicitly asks
    about multi-select / batch / multiple / several.

    The brief explicitly carves this out as a specific problem
    guard. It is keyword-based, NOT a general semantic
    classifier — paraphrased questions (e.g. ``select more than
    one`` / ``pick a bunch of files at once``) are NOT
    detected. The detector runs in linear time on the question
    string and returns False on any non-string / empty input so
    legacy callers that do not pass ``research_question`` keep
    the existing v11 behaviour (gate disabled, confirmed
    findings stay confirmed).
    """
    if not research_question or not isinstance(research_question, str):
        return False
    return bool(_MULTISELECT_QUESTION_RE.search(research_question))


def _quote_contains_quantifier(quote: str | None) -> bool:
    """Return True when the verbatim ``Evidence: "..."`` quote
    carries a multi-select / batch / multiple / several
    quantifier equivalent.

    Used by the v12 multi-select question guard (see
    :func:`_is_multi_select_question`). When the question is a
    multi-select ask and the quote lacks a quantifier equivalent,
    the confirmed finding is demoted to ``not_verified`` so the
    completion gate stamps ``partial_failed`` instead of
    crediting the run on the basis of a generic plural that does
    NOT prove the multi-select capability the PM asked about.
    Empty / non-string quote returns False.
    """
    if not quote or not isinstance(quote, str):
        return False
    return bool(_QUANTIFIER_TOKENS_RE.search(quote))


def _extract_evidence_quote(description: str) -> str | None:
    """Return the first bounded ``Evidence: "..."`` quote in
    ``description`` (case-insensitive). The quote is the raw
    captured group; the caller lower-cases for haystack lookup.
    """
    if not description:
        return None
    match = _EVIDENCE_QUOTE_RE.search(str(description))
    if not match:
        return None
    quote = match.group(1).strip()
    return quote or None


def _is_evidence_quote_supported(
    quote: str,
    source_refs: tuple[tuple[str, str], ...],
    source_snippets: dict[str, str] | None,
) -> bool:
    """Return True when ``quote`` appears verbatim (case-
    insensitive) as a contiguous substring in at least one
    cited source's raw ``snippet`` text.

    The snippet text is taken from the explicit
    ``source_snippets`` map (built from the persisted
    ``ResearchSource.snippet`` field — see
    :func:`_build_source_snippet_map`). Title / URL / domain
    fields are NEVER searched: the gate's contract is
    "the quote must appear in the snippet the provider
    returned", not "the quote must appear in some text we
    concatenated for the name-support gate". An empty
    ``source_snippets`` value (None snippet or empty string)
    always returns False so a row whose ``snippet`` was lost
    on the wire (the v6 case where the provider returned a
    hit but no body text) cannot satisfy the quote
    requirement by accident.

    The match is contiguous: a multi-line snippet whose
    first line carries the real quote is matched, and a
    multi-line quote (the regex captures newlines) that
    spans the snippet is matched — no whitespace /
    newline normalization is applied because the gate's
    point is to refuse silent promotion on a quote that
    is NOT the same character sequence the source carried.

    An empty / unparseable quote returns False — the gate
    is strict and the caller's intent is to refuse silent
    promotion to ``confirmed`` when no traceable quote
    exists.
    """
    if not quote or source_snippets is None:
        return False
    needle = quote.lower()
    if not needle:
        return False
    for provider_source_id, _ in source_refs:
        snippet = source_snippets.get(provider_source_id)
        if not snippet:
            continue
        if needle in snippet.lower():
            return True
    return False


# ---- v13 description-ground gate -------------------------------------------
#
# v10 — the previous evidence-quote gate validates that the
# ``Evidence: "..."`` quote is a verbatim substring of the cited
# snippet, but it does NOT validate that the description's prose
# OUTSIDE the quote is grounded in the snippet. v10a (Google Drive
# run a102e4af) and v10b (Google Drive run 722b547a) surfaced this
# with two real-world false positives: the model quoted real
# snippets and EXPANDED the surrounding prose to claim
# capabilities (``一次多选`` / ``仅逐文件点选`` / ``避免多选的
# 替代流程``) that the snippets did not actually describe.
#
# The fail-closed fix below replaces the previous "every
# content token must be in snippet" rule with a more
# discriminating boundary that avoids two well-known over-
# /under-fire modes:
#
# 1. **Per-character CJK tokenisation is too fine.** A real
#    Chinese description like ``竞品 X 的多选入口更明显`` would
#    otherwise be split into 9 single-char tokens; matching any
#    one against an English snippet is meaningless. We tokenise
#    CJK into ≥ 2-char runs and require substantive CJK runs to
#    match the snippet's CJK chars as a substring (CJK ⇔ CJK —
#    an English snippet does NOT ground a Chinese claim, and
#    vice versa).
# 2. **Brand-token extraction is too permissive.** Drawing
#    "brand" tokens from title alone admits common English
#    nouns (``select``, ``multiple``, ``tap``) into the
#    exemption set and lets real expansion bypass the gate.
#    We restrict the brand-token set to tokens from the
#    cited source's URL hostname (the most stable brand
#    anchor) plus the finding's own name field — neither
#    produces spurious exemptions for capability / quantifier
#    tokens.
#
# The substantive check: when the description carries a real
# ``Evidence: "..."`` quote AND the claim prose (text outside
# the quote) contains at least one substantive token
# (English ≥ 3 chars not in stopwords, or CJK ≥ 2 chars not
# in stopwords), AT LEAST ONE of those substantive tokens
# must appear as a substring in at least one cited source's
# snippet. The check is "any one substantive token present
# in the snippet" rather than "every token present" — this
# matches the brief's spirit ("substantive content grounded
# in direct text") without over-firing on incidental
# connectives. It still rejects v10a/b because their added
# ``一次多选`` / ``避免多选的替代流程`` CJK runs are
# absent from the English snippets — neither run contains a
# CJK run, so the CJK ⇔ CJK substring check fails.
#
# v13 — the v11 stopword backdoor above is REMOVED entirely.
# The token-extraction helper, the brand-anchor host-token
# helper, and this stopword set are all deleted so the
# vacuous-truth branch cannot fire on all-stopword /
# all-brand-anchor prose (e.g. ``No support. Evidence: "Find
# and tap the files you want to upload."`` previously
# slipped through because ``no`` / ``support`` are in this
# set). The v13 contract replaces the entire pipeline with
# a STRICT EXACT-SHAPE check (see
# :func:`_is_description_grounded_in_snippet`).


def _strip_evidence_quote_span(description: str, quote: str | None) -> str:
    """Return the description text with the ``Evidence: "..."``
    span removed, so the remaining text is the "claim prose"
    the v10 description-ground gate validates against the
    cited snippet.

    The strip uses :data:`_EVIDENCE_QUOTE_RE` (case-insensitive)
    so a malformed ``Evidence:`` casing does not silently bypass
    the gate. When the quote is not present in the description
    (or is None), the entire description is returned unchanged
    — the claim prose IS the whole description in that case
    and the gate fires the same way.
    """
    if not description:
        return ""
    if not quote:
        return description
    match = _EVIDENCE_QUOTE_RE.search(description)
    if not match:
        return description
    return description[: match.start()] + description[match.end() :]


def _is_description_grounded_in_snippet(
    description: str,
    source_refs: tuple[tuple[str, str], ...],
    source_snippets: dict[str, str] | None,
    allowed_provider_urls: dict[str, str] | None,
) -> bool:
    """Return True when the description is provably source-grounded.

    v13 STRICT EXACT-SHAPE CONTRACT — the confirmed
    ``description`` of a finding MUST be one of THREE literal
    complete shapes (whitespace-insensitive at the boundary;
    the ``Evidence: "..."`` quote itself must remain byte-
    identical to the v5 quote-gate contract):

      1. ``Evidence: "<verbatim quote>"`` — the bare quote form.
      2. ``From the cited source. Evidence: "<verbatim quote>"``
         — always allowed regardless of the cited source's
         official status. The prefix is a non-claiming source
         pointer.
      3. ``From the official help page. Evidence: "<verbatim
         quote>"`` — allowed ONLY when every cited source is
         in the official vendor allowlist
         :data:`_OFFICIAL_VENDOR_RULES` (host + path both pass
         the rule's filters — see
         :func:`_all_cited_sources_official`).

    No other prose is permitted. The brief explicitly rejects
    the v11 stopword backdoor: a description like ``No support.
    Evidence: "Find and tap the files you want to upload."``
    carries English words (``no`` / ``support``) that v11 fell
    into the stopword-exemption set
    :data:`_DESCRIPTION_GROUND_STOPWORDS` and let the
    vacuous-truth branch fire. v13 deletes the substantive-
    token extractor / brand-anchor exemption entirely so the
    token-stopword exemption cannot be re-introduced by a
    future regression.

    History — v10 first cut used an "any one substantive token
    present in snippet" backstop. Google Drive run 309f1d93-e060-
    4e82-9c35-4b0bd5f77968 quoted ``Find and tap the files you
    want to upload.`` and the surrounding prose paraphrased /
    expanded the claim to ``indicating a tap-based multi-
    selection interaction with no documented batch-shortcut``;
    ``tap`` / ``based`` incidentally appeared in the cited
    snippet (because the LLM quoted them verbatim as part of
    the ``Evidence: "..."`` quote) and v10 fired True. Token
    overlap is NOT semantic entailment. v11 fail-closed by
    removing the "any one matches" backstop but kept a
    vacuous-truth branch — when the substantive-token
    extractor found ZERO non-stopword / non-brand-anchor tokens
    the description passed. v13 closes the last deterministic
    hole: the v11 vacuous-truth branch still admitted
    all-stopword / all-brand-anchor prose (negation words,
    capability verbs, meta-nouns, hostname tokens), which the
    audit proved is itself a substantial claim that should
    demote. v13 deletes the substantive-token extractor
    entirely.

    Concrete accepted-vs-demoted examples:

    * Accepted: ``Evidence: "Select one photo first, then use
      two fingers to slide to select multiple files or photos."``
      — pure quote form, shape #1.
    * Accepted: ``From the cited source. Evidence: "Find and
      tap the files you want to upload."`` — shape #2, always
      allowed.
    * Accepted: ``From the official help page. Evidence:
      "Select one photo first, then use two fingers to slide
      to select multiple files or photos."`` citing a real
      ``support.microsoft.com`` page — shape #3, official
      allowlist passes.
    * Demoted: ``No support. Evidence: "Find and tap the
      files you want to upload."`` — ``No`` / ``support`` are
      substantive claim tokens the snippet does not literally
      contain. v11 admitted this via the stopword backdoor;
      v13 demotes.
    * Demoted: ``Evidence: "Find and tap the files you want to
      upload." Note: backup is unavailable.`` — trailing
      prose after the quote is substantive expansion.
    * Demoted: ``Evidence: "Find and tap the files you want to
      upload." Evidence: "Another snippet."`` — a second
      ``Evidence: "..."`` claim the gate cannot verify as a
      single contiguous snippet span.
    * Demoted: ``From the Google page. Evidence: "Find and tap
      the files you want to upload."`` — ``From the Google
      page.`` is not one of the two allowed prefixes; the
      freedom-to-rewrite-of-stopwords loophole is closed.

    Failure mode: the gate returns False when the exact-shape
    check fails. The validator demotes the finding to
    ``status='not_verified'`` with empty ``source_refs`` so the
    audit row preserves the original LLM emission while the
    completion gate stamps ``partial_failed`` instead of
    crediting the run on the basis of an ungrounded citation.

    When ``source_snippets`` is ``None`` (legacy callers, unit
    tests) the gate returns False — same opt-in flag the caller
    uses to enable the quote gate
    (``validate_evidence_quotes=True``) controls the
    description-ground gate.
    """
    if not description or source_snippets is None:
        return False
    quote = _extract_evidence_quote(description)
    if not quote:
        # v13 — a confirmed description MUST carry a
        # verbatim ``Evidence: "..."`` quote. Missing
        # quote → demote (the v5 quote gate's contract
        # is upheld; no vacuous-truth backdoor).
        return False
    claim_prose = _strip_evidence_quote_span(description, quote)
    claim_prose_stripped = claim_prose.strip()
    # v13 STRICT EXACT-SHAPE contract — the description of a
    # confirmed finding MUST be one of THREE literal complete
    # shapes (whitespace-insensitive at the boundary; the
    # ``Evidence: "..."`` quote must remain byte-identical to
    # the v5 quote-gate contract):
    #   1. ``Evidence: "<verbatim quote>"`` — bare quote form.
    #   2. ``From the cited source. Evidence: "<verbatim
    #      quote>"`` — always allowed regardless of cited
    #      source official status.
    #   3. ``From the official help page. Evidence: "<verbatim
    #      quote>"`` — allowed ONLY when every cited source is
    #      in the official vendor allowlist
    #      :data:`_OFFICIAL_VENDOR_RULES` (see
    #      :func:`_all_cited_sources_official`).
    # No other prose is permitted. The brief explicitly rejects
    # the stopword backdoor — descriptions like ``No support.
    # Evidence: "..."`` previously fell into the v11 vacuous-
    # truth branch because ``no`` / ``support`` are in the
    # stopword set; v13 deletes that exemption entirely.
    if claim_prose_stripped == "":
        return True
    if claim_prose_stripped == "From the cited source.":
        return True
    if claim_prose_stripped == "From the official help page.":
        return _all_cited_sources_official(source_refs, allowed_provider_urls)
    return False


# ---- Name-support tokenisation -------------------------------------------
#
# The validator's name-support gate compares the competitor /
# market_pattern / opportunity / risk name against the cited
# source's title / snippet / URL haystack. A finding is allowed
# status="confirmed" ONLY when at least one cited source's
# haystack contains every significant token from the name. This
# prevents the v3 evaluation's "iCloud Photos competitor citing
# a Google Drive Community thread" failure mode — ``iCloud``
# and ``Photos`` are BOTH required because both are significant
# tokens (neither is a stop word).

# Generic short tokens that carry no subject-noun information.
# Words like "for", "the", "of" never appear in a brand name on
# their own. A 2-char lower bound on token length drops common
# romanisations ("i", "v") that are not informative for the
# multi-product match.
_NAME_SUPPORT_STOP_WORDS: frozenset[str] = frozenset(
    {
        "i",
        "a",
        "an",
        "in",
        "of",
        "to",
        "for",
        "on",
        "at",
        "by",
        "the",
        "and",
        "or",
        "vs",
        "vs.",
    }
)


def _name_tokens(value: str) -> list[str]:
    """Return the significant lower-cased tokens of a competitor /
    market_pattern / opportunity / risk name.

    Tokenisation:

    1. Replace hyphens and underscores with spaces.
    2. Split on whitespace.
    3. Lower-case each token.
    4. Drop tokens shorter than 2 chars (never informative).
    5. Drop :data:`_NAME_SUPPORT_STOP_WORDS` (never brand-bearing).

    The output list preserves insertion order (most-significant
    token first) so the snippet-filter check can iterate it
    cheaply.
    """
    if not value:
        return []
    raw = str(value).replace("-", " ").replace("_", " ")
    tokens: list[str] = []
    for piece in raw.split():
        token = piece.strip().lower()
        if not token:
            continue
        # Strip a trailing punctuation (``.``, ``,``, ``)``, etc.).
        while token and not token[-1].isalnum():
            token = token[:-1]
        if not token:
            continue
        if len(token) < 2:
            continue
        if token in _NAME_SUPPORT_STOP_WORDS:
            continue
        tokens.append(token)
    return tokens


def _build_source_haystack_map(
    sources: list["ResearchSource"],
) -> dict[str, str]:
    """Build the lowercase haystack map consumed by the validator's
    name-support gate.

    Each persisted ``ResearchSource`` row contributes ONE haystack
    string built from its ``title + url + domain + snippet`` (in
    that order, joined by newlines). The map is keyed on
    ``provider_source_id`` so the validator can look up the
    haystack for any cited ID.

    The function is intentionally side-effect-free — it lives in
    the service module rather than the validator so the validator
    remains a pure function of ``(payload, allowed_provider_…)``.
    """
    out: dict[str, str] = {}
    for src in sources:
        parts = [src.url or ""]
        if src.title:
            parts.append(src.title)
        if src.domain:
            parts.append(src.domain)
        if src.snippet:
            parts.append(src.snippet)
        out[src.provider_source_id] = "\n".join(parts).lower()
    return out


def _build_source_snippet_map(
    sources: list["ResearchSource"],
) -> dict[str, str]:
    """Build the raw snippet map consumed by the validator's
    evidence-quote gate.

    v6 — this map is the GATE'S ONLY INPUT for the quote
    match. Each persisted ``ResearchSource`` row contributes
    exactly its ``snippet`` field (raw text, NOT lowercased
    here so the caller's case-insensitive comparison can do
    the lowering once on the needle + once on the snippet).
    Title / URL / domain fields are deliberately omitted so
    a title that happens to contain the claim text cannot
    satisfy the quote-gate on its own — that was the v5
    failure mode where the validator's concatenated haystack
    projection mistook the URL or domain for a snippet.

    A row whose ``snippet`` is ``None`` or empty contributes
    the empty string so the lookup returns a falsy value and
    the gate refuses to match. The v5 case where the provider
    returned a hit but no body text is then correctly rejected
    by the gate rather than promoted to ``confirmed``.

    The map is keyed on ``provider_source_id`` so the
    validator can look up the snippet for any cited ID.
    """
    out: dict[str, str] = {}
    for src in sources:
        out[src.provider_source_id] = src.snippet or ""
    return out


# ---- Validation -----------------------------------------------------------


_ALLOWED_PAYLOAD_KEYS = frozenset(
    {"summary", "confidence", "competitors", "market_patterns", "opportunities", "risks"}
)
_ALLOWED_COMPETITOR_KEYS = frozenset({"name", "description", "confidence", "source_refs"})
_ALLOWED_PATTERN_KEYS = frozenset({"title", "description", "confidence", "source_refs"})
_ALLOWED_OPT_RISK_KEYS = frozenset({"title", "description", "confidence", "status", "source_refs"})
_ALLOWED_SOURCE_REF_KEYS = frozenset({"provider_source_id", "url", "title"})


def _require_finite_float(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchValidationError(f"{field} must be a finite number")
    f = float(value)
    if not math.isfinite(f):
        raise ResearchValidationError(f"{field} must be a finite number")
    return f


def _require_string(value: Any, *, field: str, min_len: int, max_len: int) -> str:
    if not isinstance(value, str):
        raise ResearchValidationError(f"{field} must be a non-empty string")
    text = value.strip()
    if len(text) < min_len:
        raise ResearchValidationError(f"{field} length below {min_len}")
    if len(text) > max_len:
        raise ResearchValidationError(f"{field} length above {max_len}")
    return text


def _is_http_url(value: str) -> bool:
    return value.lower().startswith(("http://", "https://")) and 1 <= len(value) <= 2048


def validate_research_payload(
    payload: Any,
    *,
    allowed_provider_source_ids: set[str],
    allowed_provider_urls: dict[str, str],
    source_haystacks: dict[str, str] | None = None,
    source_snippets: dict[str, str] | None = None,
    validate_evidence_quotes: bool = False,
    validate_description_grounding: bool = False,
    research_question: str | None = None,
) -> ValidatedResearchPayload:
    """Validate the structured research payload against persisted sources.

    Strict rules (match the schema validator and the DB CHECK
    constraints):

    * top-level keys are exactly ``{summary, confidence, competitors,
      market_patterns, opportunities, risks}``;
    * ``summary`` is a 1-600 char single string;
    * ``confidence`` ∈ [0, 1];
    * ``competitors`` / ``market_patterns`` arrays — every entry must
      carry ≥1 ``source_refs`` where ``provider_source_id`` is in
      ``allowed_provider_source_ids`` and ``url`` matches the URL
      persisted for that id;
    * ``opportunities`` / ``risks`` arrays — every entry must set a
      ``status`` (``confirmed`` or ``not_verified``); a confirmed
      entry carries ≥1 ``source_refs`` (pointing at persisted
      sources); a not_verified entry carries none;
    * the LLM must not invent URLs / provider_source_id values.

    v3 — **name-support semantic gate**: when
    ``source_haystacks`` is supplied, the validator additionally
    enforces that a finding's ``status='confirmed'`` is only
    permitted when at least one cited source's haystack
    (``title + url + domain + snippet``, lower-cased) contains
    EVERY significant token of the finding's name (e.g.
    ``name='iCloud Photos'`` requires both ``icloud`` and
    ``photos`` to appear in at least one cited source's
    haystack). Findings whose name is not backed by ANY cited
    source are demoted to ``status='not_verified'`` with empty
    ``source_refs`` so the DB CHECK constraint
    ``ck_research_findings_status_provided_invariant`` is still
    satisfied and the audit row preserves the original LLM
    emission.

    When ``source_haystacks`` is ``None`` (unit tests, legacy
    callers) the gate is disabled — this keeps the existing
    structural checks in place and prevents the v3 fix from
    breaking pure-shape tests that don't construct sources.

    v5 — **evidence-quote gate**: when ``validate_evidence_quotes``
    is True AND ``source_snippets`` is supplied, confirmed
    competitor / market_pattern findings additionally require a
    verbatim ``Evidence: "..."`` quote in the description that
    appears character-for-character in at least one cited
    source's raw ``snippet`` text. The snippet is read from
    the dedicated ``source_snippets`` map (built from the
    persisted ``ResearchSource.snippet`` field — see
    :func:`_build_source_snippet_map`); title / URL / domain
    fields are NEVER searched. Findings with a missing or
    fabricated quote are demoted to ``status='not_verified'``
    with empty ``source_refs``. The flag defaults to False so
    the existing structural / name-support tests continue to
    pass; only the real execution path (Phase 8 v3 runs)
    enables it.

    v10 — **description-ground gate**: when
    ``validate_description_grounding`` is True AND
    ``source_snippets`` is supplied, confirmed competitor /
    market_pattern findings additionally require that every
    content-bearing token in the description's claim prose
    (the text outside the ``Evidence: "..."`` quote) appears
    as a substring in at least one cited source's raw
    ``snippet`` text. Product / brand tokens (drawn from the
    cited source's title + url + domain — NOT the snippet)
    and stopwords / connective verbs are exempt so the gate
    focuses on action / subject / quantifier tokens. The gate
    closes the v10a / v10b false-positive failure mode where
    the model quoted a real snippet and expanded the
    surrounding prose to capabilities the snippet did not
    actually describe. Findings whose description adds
    substantive tokens outside the cited snippet are demoted
    to ``status='not_verified'`` with empty ``source_refs``
    so the audit row preserves the original LLM emission and
    the completion gate stamps ``partial_failed`` instead of
    crediting the run on the basis of an ungrounded
    citation. The flag defaults to False so legacy structural
    / pure-shape unit tests keep working without
    modification; only the real execution path enables it.

    v12 — **multi-select question guard** (specific problem
    guard, not a general semantic-entailment claim): when
    ``research_question`` is provided AND the question
    explicitly asks about multi-select / batch / multiple /
    several (``多选`` / ``批量`` / ``multiple`` /
    ``several`` / ``multi-select`` / ``多个`` / ``多重``,
    matched by :func:`_is_multi_select_question`), confirmed
    competitor / market_pattern findings additionally require
    that the verbatim ``Evidence: "..."`` quote contains a
    quantifier equivalent (same keyword set, matched by
    :func:`_quote_contains_quantifier`). Generic plural
    ``files`` / ``photos`` / ``tap files`` is NOT sufficient
    — the cited snippet only proves a generic tap / single
    interaction, not the multi-select capability the PM asked
    about. Findings whose quote lacks a quantifier equivalent
    are demoted to ``status='not_verified'`` with empty
    ``source_refs`` so the completion gate stamps
    ``partial_failed`` instead of crediting the run on the
    basis of a generic plural. Non-multi-select questions are
    unaffected (``research_question=None`` defaults to the
    gate-disabled path so legacy callers keep the v11
    behaviour).
    """
    if not isinstance(payload, dict):
        raise ResearchValidationError("payload is not an object")
    if set(payload) != _ALLOWED_PAYLOAD_KEYS:
        unexpected = set(payload) - _ALLOWED_PAYLOAD_KEYS
        missing = _ALLOWED_PAYLOAD_KEYS - set(payload)
        if missing:
            raise ResearchValidationError(f"missing top-level fields: {sorted(missing)}")
        raise ResearchValidationError(f"unexpected top-level fields: {sorted(unexpected)}")

    summary = _require_string(payload["summary"], field="summary", min_len=1, max_len=600)
    confidence = _require_finite_float(payload["confidence"], field="confidence")
    if confidence < 0.0 or confidence > 1.0:
        raise ResearchValidationError("confidence out of [0, 1]")
    if payload["competitors"] is None:
        raise ResearchValidationError("competitors array missing")
    if payload["market_patterns"] is None:
        raise ResearchValidationError("market_patterns array missing")
    if payload["opportunities"] is None:
        raise ResearchValidationError("opportunities array missing")
    if payload["risks"] is None:
        raise ResearchValidationError("risks array missing")
    if not isinstance(payload["competitors"], list):
        raise ResearchValidationError("competitors must be a list")
    if not isinstance(payload["market_patterns"], list):
        raise ResearchValidationError("market_patterns must be a list")
    if not isinstance(payload["opportunities"], list):
        raise ResearchValidationError("opportunities must be a list")
    if not isinstance(payload["risks"], list):
        raise ResearchValidationError("risks must be a list")

    findings: list[ValidatedFinding] = []
    # v12 — multi-select question guard. The brief carves out
    # a specific problem guard for research_questions that
    # explicitly ask about multi-select / batch / multiple /
    # several. The keyword detection is intentionally narrow
    # (paraphrased questions are NOT detected — the brief
    # carves it out as a "conservative specific problem
    # guard, not a general semantic entailment claim"). When
    # the question is NOT a multi-select ask
    # (``research_question`` is None or does not match the
    # pattern) the gate is disabled and confirmed findings
    # stay confirmed exactly like v11.
    quantifier_required = _is_multi_select_question(research_question)
    # The SUMMARY is derived from the LLM's own ``summary`` field,
    # which the prompt pins to the internal context alone — it does
    # not cite any external search hit. Per the strict
    # ``ck_research_findings_status_provided_invariant`` constraint
    # (confirmed findings MUST carry ≥1 source_refs) the row is
    # tagged ``not_verified`` so the INSERT does not violate the DB
    # contract. The category is still ``summary`` so the frontend
    # renders it as the headline finding.
    findings.append(
        ValidatedFinding(
            category=ResearchFindingCategory.SUMMARY.value,
            title=summary[:200],
            description=summary,
            confidence=confidence,
            status=ResearchFindingStatus.NOT_VERIFIED.value,
            source_refs=(),
        )
    )
    findings.extend(
        _validate_sourced_array(
            payload["competitors"],
            category=ResearchFindingCategory.COMPETITOR.value,
            allowed_keys=_ALLOWED_COMPETITOR_KEYS,
            allowed_provider_source_ids=allowed_provider_source_ids,
            allowed_provider_urls=allowed_provider_urls,
            source_haystacks=source_haystacks,
            source_snippets=source_snippets,
            name_field="name",
            validate_evidence_quotes=validate_evidence_quotes,
            validate_description_grounding=validate_description_grounding,
            quantifier_required=quantifier_required,
        )
    )
    findings.extend(
        _validate_sourced_array(
            payload["market_patterns"],
            category=ResearchFindingCategory.MARKET_PATTERN.value,
            allowed_keys=_ALLOWED_PATTERN_KEYS,
            allowed_provider_source_ids=allowed_provider_source_ids,
            allowed_provider_urls=allowed_provider_urls,
            source_haystacks=source_haystacks,
            source_snippets=source_snippets,
            name_field="title",
            validate_evidence_quotes=validate_evidence_quotes,
            validate_description_grounding=validate_description_grounding,
            quantifier_required=quantifier_required,
        )
    )
    findings.extend(
        _validate_optional_array(
            payload["opportunities"],
            category=ResearchFindingCategory.OPPORTUNITY.value,
            allowed_keys=_ALLOWED_OPT_RISK_KEYS,
            allowed_provider_source_ids=allowed_provider_source_ids,
            allowed_provider_urls=allowed_provider_urls,
            source_haystacks=source_haystacks,
        )
    )
    findings.extend(
        _validate_optional_array(
            payload["risks"],
            category=ResearchFindingCategory.RISK.value,
            allowed_keys=_ALLOWED_OPT_RISK_KEYS,
            allowed_provider_source_ids=allowed_provider_source_ids,
            allowed_provider_urls=allowed_provider_urls,
            source_haystacks=source_haystacks,
        )
    )
    return ValidatedResearchPayload(
        summary=summary,
        confidence=confidence,
        findings=tuple(findings),
    )


def _validate_source_refs(
    raw_refs: Any,
    *,
    allowed_provider_source_ids: set[str],
    allowed_provider_urls: dict[str, str],
) -> tuple[tuple[str, str], ...]:
    if raw_refs is None:
        return ()
    if not isinstance(raw_refs, list):
        raise ResearchValidationError("source_refs must be a list")
    refs: list[tuple[str, str]] = []
    for idx, entry in enumerate(raw_refs):
        if not isinstance(entry, dict):
            raise ResearchValidationError(f"source_refs[{idx}] not object")
        if set(entry) - _ALLOWED_SOURCE_REF_KEYS:
            raise ResearchValidationError(f"source_refs[{idx}] unexpected fields")
        provider_source_id = entry.get("provider_source_id")
        url = entry.get("url")
        title = entry.get("title")
        if not isinstance(provider_source_id, str) or not provider_source_id.strip():
            raise ResearchValidationError("source_refs provider_source_id invalid")
        if not isinstance(url, str) or not _is_http_url(url):
            raise ResearchValidationError("source_refs url must be http(s)")
        if not isinstance(title, str) or not title.strip():
            raise ResearchValidationError("source_refs title must be non-empty string")
        if provider_source_id not in allowed_provider_source_ids:
            raise ResearchValidationError(
                f"source_refs provider_source_id {provider_source_id!r} not in run sources"
            )
        expected_url = allowed_provider_urls.get(provider_source_id)
        if expected_url != url:
            raise ResearchValidationError(f"source_refs[{idx}] url does not match persisted source")
        refs.append((provider_source_id, url))
    return tuple(refs)


def _validate_sourced_array(
    raw_list: list,
    *,
    category: str,
    allowed_keys: frozenset[str],
    allowed_provider_source_ids: set[str],
    allowed_provider_urls: dict[str, str],
    source_haystacks: dict[str, str] | None,
    source_snippets: dict[str, str] | None,
    name_field: str,
    validate_evidence_quotes: bool = False,
    validate_description_grounding: bool = False,
    quantifier_required: bool = False,
) -> list[ValidatedFinding]:
    findings: list[ValidatedFinding] = []
    for idx, entry in enumerate(raw_list):
        if not isinstance(entry, dict):
            raise ResearchValidationError(f"{category}[{idx}] not object")
        if set(entry) - allowed_keys:
            raise ResearchValidationError(f"{category}[{idx}] unexpected fields")
        name = _require_string(
            entry.get(name_field),
            field=f"{category}[{idx}].{name_field}",
            min_len=1,
            max_len=200,
        )
        description = _require_string(
            entry.get("description"),
            field=f"{category}[{idx}].description",
            min_len=1,
            max_len=4000,
        )
        confidence = entry.get("confidence")
        if confidence is not None:
            confidence = _require_finite_float(confidence, field=f"{category}[{idx}].confidence")
            if confidence < 0.0 or confidence > 1.0:
                raise ResearchValidationError(f"{category}[{idx}].confidence out of [0, 1]")
        source_refs = _validate_source_refs(
            entry.get("source_refs"),
            allowed_provider_source_ids=allowed_provider_source_ids,
            allowed_provider_urls=allowed_provider_urls,
        )
        if not source_refs:
            raise ResearchValidationError(
                f"{category}[{idx}] requires source_refs (no provider hit)"
            )
        # v3 — name-support semantic gate: a sourced-category
        # finding's name MUST be backed by at least one cited
        # source. When ``source_haystacks`` is supplied (only by
        # ``_execute_run`` after sources are persisted) the gate
        # demotes findings whose significant name tokens are not
        # present in ANY cited source's haystack to
        # ``not_verified`` with empty ``source_refs``. When the
        # gate is disabled (``source_haystacks is None`` — unit
        # tests, legacy callers) every sourced finding remains
        # ``confirmed`` as before.
        if source_haystacks is not None:
            if not _is_name_supported_by_sources(name, source_refs, source_haystacks):
                findings.append(
                    ValidatedFinding(
                        category=category,
                        title=name,
                        description=description,
                        confidence=confidence,
                        status=ResearchFindingStatus.NOT_VERIFIED.value,
                        source_refs=(),
                    )
                )
                continue
        # v5/v6 — evidence-quote gate: when the caller enables
        # ``validate_evidence_quotes`` AND ``source_snippets`` is
        # supplied, a confirmed competitor / market_pattern MUST
        # carry a verbatim ``Evidence: "..."`` quote in the
        # description that appears as a contiguous substring in
        # at least one cited source's raw ``snippet`` text. The
        # dedicated ``source_snippets`` map (built from the
        # persisted ``ResearchSource.snippet`` field) is the
        # ONLY input the gate reads — title / URL / domain
        # fields are deliberately excluded. Missing /
        # fabricated quotes demote the finding to
        # ``not_verified`` rather than silently promoting it to
        # ``confirmed``. The flag is opt-in so legacy
        # structural / pure-shape unit tests keep working
        # without modification.
        if validate_evidence_quotes and source_snippets is not None:
            quote = _extract_evidence_quote(description)
            if not _is_evidence_quote_supported(quote or "", source_refs, source_snippets):
                findings.append(
                    ValidatedFinding(
                        category=category,
                        title=name,
                        description=description,
                        confidence=confidence,
                        status=ResearchFindingStatus.NOT_VERIFIED.value,
                        source_refs=(),
                    )
                )
                continue
        # v11 — description-ground gate (provably conservative
        # contract). The v10 "any one substantive token present
        # in snippet" rule let Google Drive run
        # 309f1d93-e060-4e82-9c35-4b0bd5f77968 slip through:
        # the surrounding prose paraphrased / expanded the
        # claim to ``indicating a tap-based multi-selection
        # interaction with no documented batch-shortcut`` and
        # the English words ``tap`` / ``based`` incidentally
        # appeared in the cited snippet (because the LLM
        # quoted them verbatim as part of the ``Evidence:
        # "..."`` quote). Token overlap is NOT semantic
        # entailment. The v11 contract requires the description
        # to contain ONLY the ``Evidence: "..."`` quote
        # (optionally preceded by a non-claiming source prefix
        # composed of stopwords / brand anchors / connectives);
        # ANY substantive token in the claim prose demotes the
        # finding. The gate does NOT consult the snippet — the
        # only way to be sure the claim prose is grounded is to
        # require the claim prose NOT EXIST. Findings whose
        # description adds substantive tokens outside the cited
        # snippet are demoted to ``not_verified`` with empty
        # ``source_refs``; the audit row preserves the original
        # LLM emission and the completion gate stamps
        # ``partial_failed`` instead of crediting the run on the
        # basis of an ungrounded citation. The flag is opt-in
        # (default False) so legacy structural / pure-shape unit
        # tests keep working without modification.
        if (
            validate_description_grounding
            and source_snippets is not None
            and not _is_description_grounded_in_snippet(
                description, source_refs, source_snippets, allowed_provider_urls
            )
        ):
            findings.append(
                ValidatedFinding(
                    category=category,
                    title=name,
                    description=description,
                    confidence=confidence,
                    status=ResearchFindingStatus.NOT_VERIFIED.value,
                    source_refs=(),
                )
            )
            continue
        # v12 — multi-select question guard. The brief
        # carves out a specific problem guard: when the PM's
        # research_question explicitly asks about multi-select
        # / batch / multiple / several (``多选`` / ``批量`` /
        # ``multiple`` / ``several`` / ``multi-select`` /
        # ``多个`` / ``多重``), a confirmed finding's
        # ``Evidence: "..."`` quote MUST directly contain a
        # quantifier equivalent — generic plural ``files`` /
        # ``tap files`` is NOT sufficient because the cited
        # snippet only proves a tap / single interaction,
        # not the multi-select capability the PM asked about.
        # Non-multi-select questions are unaffected (the
        # ``quantifier_required`` flag is False for those).
        # The guard runs AFTER every other gate so a finding
        # demoted by the description-ground / quote / name-
        # support gates is not re-promoted here.
        if quantifier_required:
            quote = _extract_evidence_quote(description)
            if not _quote_contains_quantifier(quote):
                findings.append(
                    ValidatedFinding(
                        category=category,
                        title=name,
                        description=description,
                        confidence=confidence,
                        status=ResearchFindingStatus.NOT_VERIFIED.value,
                        source_refs=(),
                    )
                )
                continue
        findings.append(
            ValidatedFinding(
                category=category,
                title=name,
                description=description,
                confidence=confidence,
                status=ResearchFindingStatus.CONFIRMED.value,
                source_refs=source_refs,
            )
        )
    return findings


def _is_name_supported_by_sources(
    name: str,
    source_refs: tuple[tuple[str, str], ...],
    source_haystacks: dict[str, str],
) -> bool:
    """Return True when at least one cited source's haystack contains
    every significant token of ``name``.

    A cited source's haystack is the lower-cased concatenation of
    its ``title + url + domain + snippet`` (the map is built by
    :func:`_build_source_haystack_map` after persistence so the
    validator receives an immutable snapshot). Significant tokens
    are produced by :func:`_name_tokens` (length ≥ 2, stop-word
    filtered). When the name has NO significant tokens (e.g.
    ``name='Photos'`` is filtered down to nothing) the gate
    treats the name as vacuously supported — the LLM has not
    given us anything to cross-reference against and there is no
    way to disprove the claim either; this matches the strictest
    semantic-reading of "the name tokens are all in some
    cited source" (vacuous truth). This is a deliberate trade-off
    — the multi-word phrase filter upstream already drops
    single-word-only phrases, so a vacuously-supported name is
    a vanishingly-rare edge case.
    """
    tokens = _name_tokens(name)
    if not tokens:
        return True
    for provider_source_id, _ in source_refs:
        haystack = source_haystacks.get(provider_source_id)
        if not haystack:
            continue
        if all(tok in haystack for tok in tokens):
            return True
    return False


def _validate_optional_array(
    raw_list: list,
    *,
    category: str,
    allowed_keys: frozenset[str],
    allowed_provider_source_ids: set[str],
    allowed_provider_urls: dict[str, str],
    source_haystacks: dict[str, str] | None,
) -> list[ValidatedFinding]:
    findings: list[ValidatedFinding] = []
    for idx, entry in enumerate(raw_list):
        if not isinstance(entry, dict):
            raise ResearchValidationError(f"{category}[{idx}] not object")
        if set(entry) - allowed_keys:
            raise ResearchValidationError(f"{category}[{idx}] unexpected fields")
        title = _require_string(
            entry.get("title"),
            field=f"{category}[{idx}].title",
            min_len=1,
            max_len=200,
        )
        description = _require_string(
            entry.get("description"),
            field=f"{category}[{idx}].description",
            min_len=1,
            max_len=4000,
        )
        confidence = entry.get("confidence")
        if confidence is not None:
            confidence = _require_finite_float(confidence, field=f"{category}[{idx}].confidence")
            if confidence < 0.0 or confidence > 1.0:
                raise ResearchValidationError(f"{category}[{idx}].confidence out of [0, 1]")
        status = entry.get("status")
        if status not in ALLOWED_FINDING_STATUSES:
            raise ResearchValidationError(f"{category}[{idx}].status invalid")
        source_refs = _validate_source_refs(
            entry.get("source_refs"),
            allowed_provider_source_ids=allowed_provider_source_ids,
            allowed_provider_urls=allowed_provider_urls,
        )
        if status == ResearchFindingStatus.CONFIRMED.value and not source_refs:
            raise ResearchValidationError(
                f"{category}[{idx}] status='confirmed' requires source_refs"
            )
        if status == ResearchFindingStatus.NOT_VERIFIED.value and source_refs:
            raise ResearchValidationError(
                f"{category}[{idx}] status='not_verified' forbids source_refs"
            )
        # v3 — name-support semantic gate for confirmed findings.
        # The gate only fires for ``status='confirmed'`` entries
        # because those carry external citations — a
        # ``status='not_verified'`` entry has no sources by
        # contract and the name-support question is moot.
        if (
            status == ResearchFindingStatus.CONFIRMED.value
            and source_haystacks is not None
            and not _is_name_supported_by_sources(title, source_refs, source_haystacks)
        ):
            findings.append(
                ValidatedFinding(
                    category=category,
                    title=title,
                    description=description,
                    confidence=confidence,
                    status=ResearchFindingStatus.NOT_VERIFIED.value,
                    source_refs=(),
                )
            )
            continue
        findings.append(
            ValidatedFinding(
                category=category,
                title=title,
                description=description,
                confidence=confidence,
                status=status,
                source_refs=source_refs,
            )
        )
    return findings


# ---- Persistence helpers --------------------------------------------------


def _persist_sources(
    db: Session,
    run: ResearchRun,
    provider_result: ProviderSearchResult,
) -> list[ResearchSource]:
    """Persist the provider-returned search hits as ``ResearchSource`` rows.

    The unique constraint ``(run_id, provider_source_id)`` deduplicates
    the set defensively; the LLM only sees the IDs persisted here.
    """
    rows: list[ResearchSource] = []
    seen: set[str] = set()
    for src in provider_result.sources:
        if src.provider_source_id in seen:
            continue
        seen.add(src.provider_source_id)
        row = ResearchSource(
            run_id=run.id,
            provider=provider_result.provider,
            provider_source_id=src.provider_source_id,
            url=src.url,
            title=src.title,
            domain=src.domain,
            snippet=src.snippet,
            retrieved_at=datetime.now(timezone.utc),
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def _persist_findings(
    db: Session,
    run: ResearchRun,
    findings: tuple[ValidatedFinding, ...],
    source_id_by_provider_id: dict[str, UUID],
) -> int:
    """Persist every validated finding against the source-id map.

    The CHECK constraint
    ``ck_research_findings_status_provided_invariant`` enforces the
    status/source_refs invariant at the DB layer; the validator
    above guarantees the same invariant before the INSERT.
    """
    count = 0
    for finding in findings:
        if not finding.source_refs:
            source_refs_json = None
        else:
            source_refs_json = [
                {"provider_source_id": pid, "source_id": str(source_id_by_provider_id[pid])}
                for (pid, _) in finding.source_refs
            ]
        row = ResearchFinding(
            run_id=run.id,
            category=finding.category,
            title=finding.title,
            description=finding.description,
            confidence=finding.confidence,
            status=finding.status,
            source_refs=source_refs_json,
        )
        db.add(row)
        count += 1
    db.flush()
    return count


# ---- Run execution --------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_error_message(message: str | None) -> str:
    if not message:
        return ""
    text = str(message)
    text = _SENSITIVE_TOKEN_RE.sub(" [REDACTED] ", text)
    text = text.replace("\x00", " ")
    text = " ".join(text.split())
    if len(text) > _SAFE_ERROR_MAX_LEN:
        text = text[:_SAFE_ERROR_MAX_LEN]
    return text


_SAFE_ERROR_MAX_LEN = 4000
_SENSITIVE_TOKEN_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_\-]{6,}|"
    r"AKIA[0-9A-Z]{8,}|"
    r"x-api-key=[^\s,;]+|"
    r"api[_-]?key[=:][^\s,;]+|"
    r"bearer\s+[A-Za-z0-9._\-]{6,}|"
    r"password[=:][^\s,;]+|"
    r"secret[=:][^\s,;]+|"
    r"token[=:][A-Za-z0-9._\-]{6,})",
    re.IGNORECASE,
)


def _require_research_config(prompt_version: str | None = None) -> None:
    """Abort fast when the Research Tool Provider or LLM is not configured.

    Centralised so the API layer can surface a 409 without re-iterating
    the per-environment-variable checks at every call site.
    """
    if prompt_version == "research-agent-v7":
        from app.services.research_search_adapters import require_search_config

        try:
            require_search_config(settings)
        except ResearchProviderConfigError:
            raise ResearchConfigError("研究搜索服务未配置") from None
    elif not settings.tavily_api_key:
        raise ResearchConfigError("TAVILY_API_KEY is not configured")
    if prompt_version != "research-agent-v7" and not settings.tavily_base_url:
        raise ResearchConfigError("TAVILY_BASE_URL is not configured")
    if not settings.llm_api_key or not settings.llm_base_url or not settings.llm_model:
        raise ResearchConfigError(
            "LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 未配置；无法启动真实研究。"
        )


def start_research_run(
    db: Session,
    project_id: UUID,
    insight_id: UUID,
    *,
    research_question: str,
    scope: str | None,
    prompt_version: str | None = None,
) -> ResearchRun:
    """Create, execute and finalise one research run synchronously.

    Mirrors :func:`app.services.insight_runner.start_insight_run`:
    the entire pipeline runs before the API returns (200). On any
    unrecoverable failure the run row is stamped with the terminal
    status (``failed`` or ``partial_failed``) and a redacted
    ``error_message`` so a future retry can continue from the same
    audit row without leaking credentials.

    The run row is **committed** before any external call so the
    audit trail survives every provider / LLM failure: a caller can
    ``GET /research/{run_id}`` after a 200 response and always find
    a row, even when the call ultimately failed. Without the
    pre-commit a ``db.rollback()`` would erase the freshly-flushed
    row and the API would 500 on ``model_validate(None)``.
    """
    if prompt_version not in (None, "research-agent-v6", "research-agent-v7"):
        raise ResearchConfigError("研究版本不受支持")
    _require_research_config(prompt_version)

    project = db.get(Project, project_id)
    if project is None:
        raise ResearchConfigError("项目不存在")
    insight = db.get(Insight, insight_id)
    if insight is None or insight.project_id != project_id:
        raise ResearchConfigError("洞察不存在或不属于该项目")

    decision = db.scalar(select(InsightDecision).where(InsightDecision.insight_id == insight_id))
    insight_run = db.get(InsightRun, insight.run_id)
    if insight_run is None or insight_run.project_id != project_id:
        raise ResearchConfigError("insight run is missing or project scope mismatch")

    internal_context = assemble_internal_context(
        db,
        insight=insight,
        decision=decision,
        research_question=research_question,
        scope=scope,
    )
    provider_query = build_provider_query(internal_context)
    # v7 audit fix — when the PM's question is a single-product
    # "official-only" ask the canonical ``provider_query`` is
    # a stale 281-char string and the call that actually goes
    # upstream is the focused short query from
    # ``_build_official_provider_query``. We compute the
    # effective query BEFORE the run row is created so the
    # pre-commit ``provider_query`` field already reflects
    # the upstream call — a worker reading the row at any
    # audit moment (after the pre-commit, after the search,
    # after the final completion commit) sees the same
    # string that went to Tavily. The non-official path keeps
    # the canonical ``provider_query`` because the swap does
    # not fire (``effective_provider_query == provider_query``).
    official_rule = _detect_official_vendor_rule(
        internal_context.research_question, internal_context.scope
    )
    effective_provider_query = provider_query
    if official_rule is not None:
        effective_provider_query = _build_official_provider_query(
            internal_context.research_question,
            internal_context.scope,
            official_rule,
        )

    run = ResearchRun(
        project_id=project_id,
        insight_id=insight_id,
        classification_run_id=insight_run.classification_run_id,
        clustering_run_id=insight_run.clustering_run_id,
        insight_decision_id=decision.id if decision is not None else None,
        research_question=internal_context.research_question,
        scope=internal_context.scope,
        provider=(
            settings.research_search_provider if prompt_version == "research-agent-v7" else "tavily"
        ),
        provider_query=effective_provider_query,
        model=settings.llm_model,
        prompt_version=prompt_version or _research_prompt.PROMPT_VERSION,
        status=ResearchRunStatus.RUNNING.value,
        started_at=_now(),
        input_count=1,
        internal_context=internal_context.to_dict(),
        internal_context_hash=internal_context.fingerprint(),
    )
    db.add(run)
    db.flush()
    run_id = run.id
    # Pre-commit the run row BEFORE the external call. Every error
    # path below must find the row in the database, never ``None``.
    db.commit()

    # ``_execute_run`` handles every known error type (provider,
    # LLM, validation) by committing a terminal state — no
    # exception escapes from a known failure. Anything that does
    # escape is a truly-unexpected exception (programming error,
    # DB integrity violation, etc.) and is caught below as a safety
    # net so the API still returns the persisted run.
    try:
        _execute_run(
            db,
            run,
            internal_context=internal_context,
            provider_query=effective_provider_query,
        )
    except Exception as exc:
        logger.exception("research run raised unexpected exception")
        try:
            db.rollback()
            run = db.get(ResearchRun, run_id)
            if run is not None and run.completed_at is None:
                run.status = (
                    ResearchRunStatus.PARTIAL_FAILED.value
                    if (run.source_count > 0 or run.finding_count > 0)
                    else ResearchRunStatus.FAILED.value
                )
                run.error_message = _safe_error_message(
                    f"research execution failed: {type(exc).__name__}: {exc}"
                )
                run.completed_at = _now()
                db.commit()
        except Exception:
            db.rollback()

    return run


def _execute_run(
    db: Session,
    run: ResearchRun,
    *,
    internal_context: InternalContext,
    provider_query: str,
) -> None:
    """Execute the bounded, independently checked v5 evidence pipeline."""
    import sys

    if run.prompt_version == "research-agent-v7":
        from app.services.research_pipeline_v7 import execute_run
    else:
        from app.services.research_pipeline_v6 import execute_run

    execute_run(
        db,
        run,
        internal_context=internal_context,
        provider_query=provider_query,
        api=sys.modules[__name__],
    )


# ---- Reads ---------------------------------------------------------------


def list_sources_for_run(
    db: Session, *, run_id: UUID, limit: int, offset: int
) -> tuple[int, list[ResearchSource]]:
    where = ResearchSource.run_id == run_id
    total = db.scalar(select(func.count()).select_from(ResearchSource).where(where))
    rows = db.scalars(
        select(ResearchSource)
        .where(where)
        .order_by(ResearchSource.retrieved_at, ResearchSource.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return total or 0, list(rows)


def list_findings_for_run(
    db: Session, *, run_id: UUID, limit: int, offset: int
) -> tuple[int, list[ResearchFinding]]:
    where = ResearchFinding.run_id == run_id
    total = db.scalar(select(func.count()).select_from(ResearchFinding).where(where))
    rows = db.scalars(
        select(ResearchFinding)
        .where(where)
        .order_by(ResearchFinding.created_at, ResearchFinding.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return total or 0, list(rows)


__all__ = [
    "ALLOWED_FINDING_CATEGORIES",
    "ALLOWED_FINDING_STATUSES",
    "INSUFFICIENT_EXTERNAL_EVIDENCE_CODE",
    "INSUFFICIENT_EXTERNAL_EVIDENCE_MESSAGE",
    "InternalContext",
    "RESEARCH_EVIDENCE_EXCERPT_MAX_CHARS",
    "RESEARCH_EVIDENCE_LIMIT",
    "RESEARCH_LLM_MAX_RETRIES",
    "RESEARCH_PROVIDER_MAX_RETRIES",
    "RESEARCH_QUESTION_MAX_LEN",
    "RESEARCH_SCOPE_MAX_LEN",
    "ResearchConfigError",
    "ResearchValidationError",
    "SOURCED_CATEGORIES",
    "UNSOURCED_CATEGORIES",
    "ValidatedFinding",
    "ValidatedResearchPayload",
    "_build_source_haystack_map",
    "_build_source_snippet_map",
    "_build_official_provider_query",
    "_build_validation_retry_hint",
    "_detect_official_vendor_rule",
    "_extract_evidence_quote",
    "_extract_phrase_tokens",
    "_filter_sources_by_low_authority",
    "_filter_sources_by_phrases",
    "_filter_sources_to_official_vendor",
    "_has_external_confirmation",
    "_all_cited_sources_official",
    "_is_description_grounded_in_snippet",
    "_is_evidence_quote_supported",
    "_is_multi_select_question",
    "_quote_contains_quantifier",
    "_is_low_authority_domain",
    "_is_name_supported_by_sources",
    "_is_official_vendor_host",
    "_is_official_vendor_path",
    "_name_tokens",
    "_question_requests_official",
    "_vendor_site_query_suffix",
    "assemble_internal_context",
    "build_provider_query",
    "list_findings_for_run",
    "list_sources_for_run",
    "start_research_run",
    "validate_research_payload",
]
