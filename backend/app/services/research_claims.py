"""Phase 8 — Exact structured quotes and analysis scaffolding.

The v15 module owns a parallel fact surface to the legacy v14
``ResearchFinding`` validator:

* :class:`Claim` is a server-side frozen dataclass that carries a
  single exact, byte-identical quote extracted from a saved source
  snapshot. The :pyattr:`Claim.description` is the raw
  ``snippet[offset:offset+length]`` text — no ``Evidence: "..."``
  wrapper, no model rewrite. The legacy v14
  :func:`_is_description_grounded_in_snippet` contract therefore
  REJECTS the bare-quote description produced here; that is the
  intended v15 surface, NOT a regression of v14.

* :func:`materialize_facts` is the only path that builds
  :class:`Claim` instances whose ``claim_kind == 'fact'``. The
  model is NOT trusted to format quotes; the server re-reads the
  saved snapshot at the unit's offset / length and refuses any
  drift, range out-of-bounds, or text/length mismatch. The status
  is determined by a closed set of independent guards: the URL
  boundary guard, the provider-id guard, the official-vendor
  guard, the name-support guard, the multi-select quantifier
  guard, and the platform-scope guard. Any guard failure demotes
  the fact to ``status='not_verified', source_refs=()`` and
  records the failed code in
  ``evidence_location['first_failed_guard']`` so the audit can
  trace why a candidate was rejected.

* :func:`materialize_analyses` accepts analysis candidates the
  model emitted and rebinds their ``supporting_evidence_ids`` to
  the matching :class:`Claim.id` UUIDs. Every support must be a
  ``claim_kind == 'fact'`` Claim with non-empty
  ``source_refs`` belonging to the caller's ``run_id`` +
  ``project_id``. Analyses are permanently
  ``status='not_verified', source_refs=()``; the server augments
  the description with the original ``scenario`` and an explicit
  ``推断`` / ``建议`` label so the audit can never mistake a
  candidate for a confirmed fact.

The module is pure: no model, no DB, no network. The product_key
is derived from the same :data:`_OFFICIAL_VENDOR_RULES` map that
the v5 executor uses, so a Google Photos page and a Google Drive
page get distinct keys while two Dropbox help articles on
different URLs share the same key.
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence, Tuple
from uuid import UUID, uuid4

# ---------------------------------------------------------------------------
# Closed platform-scope vocabulary
# ---------------------------------------------------------------------------

# Generic, no product-specific knowledge. Word-boundary matched so
# ``Apple`` / ``Dropbox app`` do not accidentally collide with
# ``app`` alone.
_PLATFORM_TOKENS: frozenset[str] = frozenset(
    {
        "computer",
        "desktop",
        "browser",
        "web",
        "online",
        "mobile",
        "phone",
        "iphone",
        "ipad",
        "ipados",
        "android",
        "ios",
        "tablet",
        "smartphone",
        "app",
    }
)

# Any phone-side device scope. Used solely by the explicit
# ``on/from a computer`` directive demotion, so iOS, iPadOS and
# Android tokens also fire the gate on an iPhone / iPad / Android
# question.
_PHONE_SCOPE_TOKENS: frozenset[str] = frozenset(
    {
        "phone",
        "iphone",
        "ipad",
        "ipados",
        "android",
        "ios",
        "smartphone",
        "mobile",
        "tablet",
    }
)

# Closed platform-class memberships used by the category-inclusion
# scope match. Neutral tokens (``app`` / ``browser`` / ``web`` /
# ``online``) are intentionally NOT members of any class: they
# must not prove a device scope on either side. The phone class
# INCLUDES the iOS and Android sub-classes; iOS and Android
# remain distinct from each other (an iOS-only question never
# matches an Android-only quote and vice versa). Computer-class
# is satisfied only by another computer-class token.
_PHONE_CLASS_TOKENS: frozenset[str] = frozenset({"phone", "smartphone", "mobile", "tablet"})
_IOS_CLASS_TOKENS: frozenset[str] = frozenset({"iphone", "ipad", "ipados", "ios"})
_ANDROID_CLASS_TOKENS: frozenset[str] = frozenset({"android"})
_COMPUTER_CLASS_TOKENS: frozenset[str] = frozenset({"computer", "desktop"})

# Union of tokens that DO prove a device scope. A quote whose
# recognised token set, after this filter, is empty is treated
# as an ordinary step and is NOT demoted by the platform-scope
# gate (it may still be demoted by other gates).
_SCOPE_TOKENS: frozenset[str] = (
    _PHONE_CLASS_TOKENS | _IOS_CLASS_TOKENS | _ANDROID_CLASS_TOKENS | _COMPUTER_CLASS_TOKENS
)

# Closed set of explicit computer/desktop phrases. Substring match
# (lower-cased). A fact that starts or carries one of these on a
# phone question is demoted.
_COMPUTER_PHRASES: tuple[str, ...] = (
    "on a computer",
    "on your computer",
    "on the computer",
    "from a computer",
    "from your computer",
    "from the computer",
    "in a browser",
    "in your browser",
    "on the web",
    "via the web",
)

# Universal / market-claim keywords. Applied to EVERY analysis
# category, not just ``market_pattern`` — a chance-of-occurrence
# "all competitors do X" claim in an opportunity / risk / market
# / competitor analysis is also rejected.
_UNIVERSAL_CLAIM_NEEDLES: tuple[str, ...] = (
    "all ",
    "all of",
    "all the",
    "every ",
    "every competitor",
    "all competitor",
    "all competitors",
    "universal",
    "all brands",
    "all vendors",
    "all products",
    "所有",
    "全部",
    "通用",
    "整个市场",
    "整个行业",
    "所有竞品",
    "全部竞品",
    "所有产品",
    "全部产品",
    "行业通用",
    "业界通用",
    "市场上所有",
)


# ---------------------------------------------------------------------------
# Analysis contract
# ---------------------------------------------------------------------------

_ANALYSIS_CATEGORIES: frozenset[str] = frozenset(
    {"competitor", "market_pattern", "opportunity", "risk"}
)
# claim_kind per category — closed table.
_ANALYSIS_CLAIM_KIND: Mapping[str, str] = {
    "competitor": "inference",
    "market_pattern": "inference",
    "opportunity": "recommendation",
    "risk": "inference",
}
# Required keys per analysis candidate. Anything else is rejected.
_REQUIRED_ANALYSIS_KEYS: frozenset[str] = frozenset(
    {
        "category",
        "claim_kind",
        "title",
        "description",
        "scenario",
        "supporting_evidence_ids",
    }
)
# Forbidden keys — a candidate must not smuggle a status /
# decision / completion signal past the closed contract.
_FORBIDDEN_ANALYSIS_KEYS: frozenset[str] = frozenset(
    {
        "status",
        "source_refs",
        "pm_priority",
        "pm_decision",
        "id",
        "is_complete",
        "covered",
        "satisfied",
        "is_fully_covered",
        "all_done",
        "confidence",
        "source_ref",
    }
)

MAX_EVIDENCE_IDS = 40
MAX_CANDIDATES = 8
MAX_SUPPORTS_PER_CANDIDATE = 40
MAX_DESCRIPTION_CHARS = 4000
MAX_TITLE_CHARS = 200
MAX_SCENARIO_CHARS = 200
MAX_QUOTE_CHARS = 200
MAX_PROVIDER_ID_CHARS = 200


class ClaimError(ValueError):
    """Closed error type for contract violations.

    Inherits from :class:`ValueError` so the test suite's
    ``pytest.raises(ValueError)`` keeps matching without having
    to know the new symbol. The fixed message is a short
    category code so a downstream caller (or a future audit) can
    react without parsing free-form prose.
    """


@dataclass(frozen=True)
class Claim:
    id: UUID
    run_id: UUID
    project_id: UUID
    category: str
    title: str
    description: str
    status: str
    source_refs: Tuple[Tuple[str, str], ...]
    claim_kind: str
    supporting_finding_ids: Tuple[UUID, ...]
    evidence_location: dict | None
    evidence_id: str
    product_key: str
    confidence: float | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _word_boundary_match(token: str, text_lower: str) -> bool:
    """Word-boundary match for ASCII tokens. ``Apple`` / ``Apps``
    do NOT match ``app``; ``Dropbox app`` matches ``app`` only at
    the trailing whitespace boundary.
    """
    return re.search(r"(?<!\w)" + re.escape(token) + r"(?!\w)", text_lower) is not None


def _platform_tokens(text: str) -> set[str]:
    if not text or not isinstance(text, str):
        return set()
    from app.services.research_tasks import _normal

    lower = _normal(text)
    return {tok for tok in _PLATFORM_TOKENS if _word_boundary_match(tok, lower)}


def _has_computer_directive(text: str) -> bool:
    if not text or not isinstance(text, str):
        return False
    lower = text.lower()
    return any(phrase in lower for phrase in _COMPUTER_PHRASES)


def _is_universal_market_claim(description: str) -> bool:
    if not description or not isinstance(description, str):
        return False
    lower = description.lower()
    return any(needle in lower for needle in _UNIVERSAL_CLAIM_NEEDLES)


def _product_key_for_source(source: Any, api: Any) -> str:
    """Derive a per-product key for a single source. See plan §4."""
    url = getattr(source, "url", None) or ""
    domain = (getattr(source, "domain", None) or "").lower() or "unknown"
    if not url:
        return domain
    try:
        rules = getattr(api, "_OFFICIAL_VENDOR_RULES", None) or {}
        for rule_key, rule in rules.items():
            site = str(rule.get("site") or "").lower()
            if not site:
                continue
            try:
                if not api._is_official_vendor_host(url, rule):
                    continue
                if not api._is_official_vendor_path(url, rule):
                    continue
            except Exception:
                continue
            return f"{site}::{rule_key}"
    except Exception:
        pass
    return domain


def _evidence_location(
    *,
    source: Any,
    unit: Any,
    first_failed_guard: str | None = None,
) -> dict:
    snippet = getattr(source, "snippet", None) or ""
    sha = hashlib.sha256(snippet.encode("utf-8")).hexdigest()
    out = {
        "provider_source_id": getattr(source, "provider_source_id", ""),
        "offset": int(unit.offset),
        "length": int(unit.length),
        "text": unit.text,
        "sha256": sha,
    }
    if first_failed_guard is not None:
        out["first_failed_guard"] = first_failed_guard
    return out


def _build_haystack(source: Any) -> str:
    """Lower-cased ``url + title + domain + snippet`` haystack for
    the v14 ``_is_name_supported_by_sources`` gate.
    """
    parts: list[str] = [getattr(source, "url", None) or ""]
    title = getattr(source, "title", None)
    if title:
        parts.append(str(title))
    domain = getattr(source, "domain", None)
    if domain:
        parts.append(str(domain))
    snippet = getattr(source, "snippet", None)
    if snippet:
        parts.append(str(snippet))
    return "\n".join(parts).lower()


def _url_boundary_check(url: str) -> str | None:
    """Return the failed guard code, or None when the URL passes
    the closed HTTPS / no-userinfo / port-443 boundary check.
    """
    if not url or not isinstance(url, str):
        return "url_missing"
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "url_malformed"
    if parsed.scheme != "https":
        return "url_not_https"
    if parsed.username or parsed.password:
        return "url_userinfo"
    try:
        port = parsed.port
    except ValueError:
        return "url_bad_port"
    if port is not None and port != 443:
        return "url_bad_port"
    if not parsed.netloc:
        return "url_missing_host"
    return None


def _official_url_check(api: Any, question: str, url: str) -> str | None:
    """Return the failed guard code, or None when ``url`` is
    official for ``question``.

    Single-product questions use the rule returned by
    :func:`api._detect_official_vendor_rule`. Multi-product /
    no-specific-rule official questions use
    :func:`api._all_cited_sources_official` over the closed
    :data:`api._OFFICIAL_VENDOR_RULES` set. Non-official
    questions return None (no guard fired).
    """
    try:
        rule = api._detect_official_vendor_rule(question, None)
    except Exception:
        rule = None
    if rule is not None:
        try:
            host_ok = api._is_official_vendor_host(url, rule)
            path_ok = api._is_official_vendor_path(url, rule)
        except Exception:
            return "url_not_official"
        if not (host_ok and path_ok):
            return "url_not_official"
        return None
    try:
        requests_official = bool(api._question_requests_official(question, None))
    except Exception:
        requests_official = False
    if not requests_official:
        return None
    try:
        # ``_all_cited_sources_official`` keys on
        # ``provider_source_id`` — we don't have one, so we ask
        # the lower-level rule loop directly to keep the closed
        # set untouched.
        for vendor_rule in api._OFFICIAL_VENDOR_RULES.values():
            try:
                if api._is_official_vendor_host(url, vendor_rule) and api._is_official_vendor_path(
                    url, vendor_rule
                ):
                    return None
            except Exception:
                continue
    except Exception:
        pass
    return "url_not_official_any"


# ---------------------------------------------------------------------------
# materialize_facts
# ---------------------------------------------------------------------------


def materialize_facts(
    evidence_ids: Sequence[str],
    entries: Mapping[str, Mapping[str, Any]],
    *,
    research_question: str,
    run_id: UUID,
    project_id: UUID,
    api: Any,
) -> Tuple[Claim, ...]:
    """Build a tuple of exact structured :class:`Claim` instances.

    Hard errors (raise :class:`ClaimError`):

    1. ``evidence_ids`` is not a list / tuple, exceeds
       :data:`MAX_EVIDENCE_IDS`, contains duplicates, references
       unknown entries, or has non-string / empty IDs.
    2. ``unit`` is missing the ``source_id`` / ``offset`` /
       ``length`` / ``text`` attributes, the offset is negative,
       the length is non-positive or above
       :data:`MAX_QUOTE_CHARS`, or the length differs from
       ``len(unit.text)``.
    3. ``offset + length`` exceeds ``len(source.snippet)`` — the
       server MUST refuse a unit whose claimed range extends
       past the persisted snapshot, even when the truncated
       slice accidentally equals the precomputed text.
    4. ``source.snippet[offset:offset+length]`` does not match
       ``unit.text`` byte-for-byte — any drift means the saved
       snapshot no longer carries the unit's content.
    5. ``source.run_id`` is not the caller's ``run_id``, or
       ``unit.source_id`` is not ``source.provider_source_id``.

    Soft errors (demote to ``status='not_verified'``,
    ``source_refs=()``, ``first_failed_guard`` in
    ``evidence_location``):

    6. URL boundary: not HTTPS, has userinfo, non-443 port, or
       malformed / missing host.
    7. ``provider_source_id`` is empty or longer than
       :data:`MAX_PROVIDER_ID_CHARS`.
    8. The URL is not an official vendor page for the question's
       scope (single rule match OR closed-set match).
    9. The derived source ``title`` is not supported by the cited
       source's haystack (legacy v14 name-support gate).
    10. Multi-select question + the quote lacks an explicit
        quantifier.
    11. Platform-scope conflict: (a) the question names a
        phone-side scope AND the description names an explicit
        ``on/from a computer`` / browser / web phrase; OR (b)
        the description's recognised scope tokens (phone / iOS
        / Android / computer classes — neutral ``app`` /
        ``browser`` / ``web`` / ``online`` tokens excluded)
        fail the category-inclusion check against the
        question's class membership after word-boundary
        normalisation.
    """
    if not isinstance(evidence_ids, (list, tuple)):
        raise ClaimError("evidence_ids_invalid")
    if len(evidence_ids) > MAX_EVIDENCE_IDS:
        raise ClaimError("evidence_ids_too_many")
    if len(set(evidence_ids)) != len(evidence_ids):
        raise ClaimError("evidence_ids_duplicate")
    if not entries:
        if not evidence_ids:
            return ()
        raise ClaimError("unknown_evidence_id")
    for eid in evidence_ids:
        if not isinstance(eid, str) or not eid:
            raise ClaimError("unknown_evidence_id")
        if eid not in entries:
            raise ClaimError("unknown_evidence_id")

    multi_select = bool(api._is_multi_select_question(research_question))
    question_platforms = _platform_tokens(research_question)
    question_has_phone_scope = bool(question_platforms & _PHONE_SCOPE_TOKENS)

    out: list[Claim] = []
    for eid in evidence_ids:
        entry = entries[eid]
        unit = entry.get("unit")
        source = entry.get("source")
        if unit is None or source is None:
            raise ClaimError("entry_invalid")
        offset = getattr(unit, "offset", None)
        length = getattr(unit, "length", None)
        text = getattr(unit, "text", None)
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or not isinstance(length, int)
            or isinstance(length, bool)
        ):
            raise ClaimError("unit_offsets_invalid")
        if offset < 0 or length <= 0 or length > MAX_QUOTE_CHARS:
            raise ClaimError("unit_length_invalid")
        if not isinstance(text, str) or not text:
            raise ClaimError("unit_text_invalid")
        # The unit's text must be exactly ``length`` characters;
        # the offset + length must fit inside the saved snapshot.
        # A mismatch raises — the unit's precomputed text is no
        # longer trustworthy.
        if len(text) != length:
            raise ClaimError("text_length_mismatch")
        snippet = getattr(source, "snippet", None) or ""
        if offset + length > len(snippet):
            raise ClaimError("offset_length_out_of_bounds")
        actual = snippet[offset : offset + length]
        if actual != text:
            raise ClaimError("snapshot_drift")
        if getattr(source, "run_id", None) != run_id:
            raise ClaimError("source_run_mismatch")
        if getattr(unit, "source_id", None) != getattr(source, "provider_source_id", None):
            raise ClaimError("unit_source_mismatch")

        # ----- Soft guards (demote on failure) -----
        url = getattr(source, "url", None) or ""
        provider_source_id = getattr(source, "provider_source_id", "") or ""
        failed_guard = _url_boundary_check(url)
        if failed_guard is None:
            if not provider_source_id or len(provider_source_id) > MAX_PROVIDER_ID_CHARS:
                failed_guard = "provider_id_invalid"
        if failed_guard is None:
            failed_guard = _official_url_check(api, research_question, url)
        if failed_guard is None:
            title = getattr(source, "title", None) or getattr(source, "domain", None) or ""
            if title:
                refs = ((provider_source_id, url),)
                haystacks = {provider_source_id: _build_haystack(source)}
                try:
                    name_ok = bool(api._is_name_supported_by_sources(title, refs, haystacks))
                except Exception:
                    name_ok = False
                if not name_ok:
                    failed_guard = "name_not_supported"
        # Multi-select quantifier gate.
        if failed_guard is None and multi_select and not api._quote_contains_quantifier(text):
            failed_guard = "multi_select_quantifier_missing"
        # Platform-scope gate. Word-boundary tokenised. The
        # ``on/from a computer`` directive check fires FIRST
        # so it cannot be rescued by an ``app`` / ``browser``
        # overlap between the question and the description.
        # After that, category-based inclusion decides the
        # match: a phone-class question is satisfied by any
        # phone / iOS / Android class token in the description;
        # an iOS-class or Android-class question is satisfied
        # by its own class OR a generic phone token (iOS and
        # Android remain distinct from each other).
        # Neutral tokens (``app`` / ``browser`` / ``web`` /
        # ``online``) do NOT prove a device scope on either
        # side, and a description with no class token is
        # treated as an ordinary step that is NOT demoted
        # here.
        if failed_guard is None and question_platforms:
            if question_has_phone_scope and (
                _has_computer_directive(text)
                or (
                    _platform_tokens(getattr(unit, "section", "")) & _COMPUTER_CLASS_TOKENS
                    and not _platform_tokens(getattr(unit, "section", "")) & _PHONE_SCOPE_TOKENS
                )
            ):
                failed_guard = "scope_phone_vs_computer"
            else:
                quote_platforms = _platform_tokens(text)
                quote_scope_tokens = quote_platforms & _SCOPE_TOKENS
                if quote_scope_tokens:
                    q_phone = bool(question_platforms & _PHONE_CLASS_TOKENS)
                    q_ios = bool(question_platforms & _IOS_CLASS_TOKENS)
                    q_android = bool(question_platforms & _ANDROID_CLASS_TOKENS)
                    q_computer = bool(question_platforms & _COMPUTER_CLASS_TOKENS)
                    qt_phone = bool(quote_scope_tokens & _PHONE_CLASS_TOKENS)
                    qt_ios = bool(quote_scope_tokens & _IOS_CLASS_TOKENS)
                    qt_android = bool(quote_scope_tokens & _ANDROID_CLASS_TOKENS)
                    qt_computer = bool(quote_scope_tokens & _COMPUTER_CLASS_TOKENS)
                    matched = (
                        (q_phone and (qt_phone or qt_ios or qt_android))
                        or (q_ios and (qt_ios or qt_phone))
                        or (q_android and (qt_android or qt_phone))
                        or (q_computer and qt_computer)
                    )
                    if not matched:
                        failed_guard = "scope_platform_mismatch"

        if failed_guard is not None:
            status = "not_verified"
            source_refs: Tuple[Tuple[str, str], ...] = ()
            evidence_location = _evidence_location(
                source=source, unit=unit, first_failed_guard=failed_guard
            )
        else:
            status = "confirmed"
            source_refs = ((provider_source_id, url),)
            evidence_location = _evidence_location(source=source, unit=unit)

        product_key = _product_key_for_source(source, api)
        title_src = getattr(source, "title", None) or getattr(source, "domain", None) or product_key
        title = str(title_src)[:MAX_TITLE_CHARS]
        claim = Claim(
            id=uuid4(),
            run_id=run_id,
            project_id=project_id,
            category="competitor",
            title=title,
            description=text,
            status=status,
            source_refs=source_refs,
            claim_kind="fact",
            supporting_finding_ids=(),
            evidence_location=evidence_location,
            evidence_id=eid,
            product_key=product_key,
            confidence=None,
        )
        out.append(claim)
    return tuple(out)


# ---------------------------------------------------------------------------
# materialize_analyses
# ---------------------------------------------------------------------------


def _analysis_label(category: str) -> str:
    """The closed label prepended to the analysis description.

    ``opportunity`` is a ``recommendation`` ("建议"); every other
    category is an ``inference`` ("推断"). The label is required
    so the audit row can never be misread as a confirmed fact.
    """
    if category == "opportunity":
        return "建议"
    return "推断"


def materialize_analyses(
    candidates: Iterable[Mapping[str, Any]],
    facts: Sequence[Claim],
    *,
    run_id: UUID,
    project_id: UUID,
) -> Tuple[Claim, ...]:
    """Build a tuple of analysis :class:`Claim` instances.

    Every input fact is first validated to belong to the caller's
    ``run_id`` + ``project_id``, to be ``status='confirmed'``
    with non-empty ``source_refs``, AND to have
    ``claim_kind == 'fact'``. The model cannot inject status /
    decision / completion fields — see
    :data:`_FORBIDDEN_ANALYSIS_KEYS`. All six required keys
    must be present; candidates are limited to
    :data:`MAX_CANDIDATES`, supports per candidate to
    :data:`MAX_SUPPORTS_PER_CANDIDATE`.

    The server augments the description with the original
    ``scenario`` and an explicit ``推断`` / ``建议`` label. A
    universal-claim (English "all" / "every" / Chinese "所有" /
    ...) is rejected for every category, not just
    ``market_pattern``. Market patterns additionally require
    ≥2 distinct ``product_key`` supports.
    """
    if not isinstance(candidates, (list, tuple)):
        raise ClaimError("candidates_invalid")
    if len(candidates) > MAX_CANDIDATES:
        raise ClaimError("candidates_too_many")

    facts_list = list(facts)
    facts_by_evidence: dict[str, Claim] = {}
    for f in facts_list:
        if f.run_id != run_id:
            raise ClaimError("fact_run_mismatch")
        if f.project_id != project_id:
            raise ClaimError("fact_project_mismatch")
        if f.status != "confirmed" or not f.source_refs:
            raise ClaimError("fact_not_confirmed")
        if f.claim_kind != "fact":
            raise ClaimError("fact_not_fact_kind")
        facts_by_evidence[f.evidence_id] = f

    out: list[Claim] = []
    for cand in candidates:
        if not isinstance(cand, Mapping):
            raise ClaimError("candidate_invalid")
        # Closed contract — no extra keys, no forbidden keys.
        extra = set(cand.keys()) - _REQUIRED_ANALYSIS_KEYS
        if extra:
            raise ClaimError("candidate_extra_keys")
        forbidden_hit = set(cand.keys()) & _FORBIDDEN_ANALYSIS_KEYS
        if forbidden_hit:
            raise ClaimError("candidate_forbidden_field")
        # Every required key must be present, not just typed-
        # tolerant.
        for required in _REQUIRED_ANALYSIS_KEYS:
            if required not in cand:
                raise ClaimError(f"missing_field:{required}")

        category = cand.get("category")
        claim_kind = cand.get("claim_kind")
        if category not in _ANALYSIS_CATEGORIES:
            raise ClaimError("category_invalid")
        expected_kind = _ANALYSIS_CLAIM_KIND.get(category)
        if claim_kind != expected_kind:
            raise ClaimError("claim_kind_mismatch")

        title = cand.get("title")
        description = cand.get("description")
        scenario = cand.get("scenario")
        supports = cand.get("supporting_evidence_ids")
        if not isinstance(title, str) or not title.strip():
            raise ClaimError("title_invalid")
        if len(title) > MAX_TITLE_CHARS:
            raise ClaimError("title_too_long")
        if not isinstance(description, str) or not description.strip():
            raise ClaimError("description_invalid")
        if not isinstance(scenario, str) or not scenario.strip():
            raise ClaimError("scenario_invalid")
        if len(scenario) > MAX_SCENARIO_CHARS:
            raise ClaimError("scenario_too_long")
        if not isinstance(supports, (list, tuple)) or not supports:
            raise ClaimError("supports_empty")
        if len(supports) > MAX_SUPPORTS_PER_CANDIDATE:
            raise ClaimError("supports_too_many")
        if len(supports) != len(set(supports)):
            raise ClaimError("supports_duplicate")
        if any(not isinstance(s, str) or not s for s in supports):
            raise ClaimError("supports_invalid_type")

        # Universal-claim gate — applied to every category.
        if _is_universal_market_claim(description):
            raise ClaimError("analysis_universal_claim")

        supporting_ids: list[UUID] = []
        product_keys: set[str] = set()
        for evidence_id in supports:
            fact = facts_by_evidence.get(evidence_id)
            if fact is None:
                raise ClaimError("support_unknown")
            supporting_ids.append(fact.id)
            product_keys.add(fact.product_key)

        if category == "market_pattern":
            if len(product_keys) < 2:
                raise ClaimError("market_pattern_single_product")

        # Augment the description with the original scenario and
        # the explicit 推断 / 建议 label so the audit row can
        # never be confused with a confirmed fact.
        label = _analysis_label(category)
        augmented = f"{description}\n【{label}】场景：{scenario}"
        if len(augmented) > MAX_DESCRIPTION_CHARS:
            raise ClaimError("description_too_long")

        out.append(
            Claim(
                id=uuid4(),
                run_id=run_id,
                project_id=project_id,
                category=category,
                title=title[:MAX_TITLE_CHARS],
                description=augmented,
                status="not_verified",
                source_refs=(),
                claim_kind=claim_kind,
                supporting_finding_ids=tuple(supporting_ids),
                evidence_location=None,
                evidence_id="",
                product_key="",
                confidence=None,
            )
        )
    return tuple(out)


__all__ = [
    "Claim",
    "ClaimError",
    "materialize_facts",
    "materialize_analyses",
]
