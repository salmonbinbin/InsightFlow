"""Original-question input obligation ledger shared by spec and support.

Each atom is an exact substring of the user's question with a fixed
dimension (product / platform / action / object / mode / output / entry).
The canonical form is separate from the raw text, so a Chinese "上传"
and an English "upload" reduce to the same action without storing a
product answer table. Owners carry multi-product branch attribution, so
"A Android uploading photos and B iPhone moving files" can verify each
requirement against the segment owned by its product.

This is a SHARED product-independent grammar only. It never claims open
semantic understanding: unknown mappings preserve the original span or
become explicit gaps. The platform / mode / action / object vocabulary
covers the dimensions used by the support contract; it is finite and
closed so a future schema change can keep the same lookup shape.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Dimension patterns. Finite vocabulary. Inflections kept here so that no
# product answer table lives in this module.
# ---------------------------------------------------------------------------

_PLATFORM_ATOMS = re.compile(
    # ``移动端`` (mobile-end) is the only Chinese compound whose
    # characters overlap an action verb (``移动`` / move). Listing
    # it FIRST in the alternation lets the platform grammar own the
    # full 3-character span so the action grammar does NOT later
    # carve out ``移动`` as a phantom ``move`` obligation. English
    # and remaining Chinese platform tokens follow in their original
    # position. Bare ``移动`` without ``端`` stays an action atom
    # because the action-flavoured reading ("move X") is the only
    # one recognised in this corpus.
    r"移动端|"
    r"\b(?:Android|iPadOS|iOS|iPhone|iPad|mobile|phone|tablet|desktop|web|"
    r"Windows|macOS|Mac|Linux)\b"
    r"|安卓|手机|平板|桌面|网页|电脑",
    re.I,
)

# Explicit e-stem inflections. ``move(?:ing|d|s)?`` was wrong: it let
# ``move`` alone match while the inflection list claimed broader coverage
# than the regex actually offered. Listing each form explicitly keeps the
# canonical table (e.g. ``删除`` -> ``delete``) in lock-step with the
# pattern so neither side silently drops a real inflection.
_ACTION_ATOMS = re.compile(
    r"\b(?:upload(?:ing|ed|s)?|uploads?|download(?:ing|ed|s)?|downloads?|"
    r"back\s*up(?:ping|s|ped)?|backup(?:ing|s|ped)?|backups?|"
    r"add(?:ing|ed|s)?|sync(?:ing|ed|s)?|"
    r"mov(?:ing|es?|ed)|delet(?:ing|es?|ed)|creat(?:ing|es?|ed)|"
    r"animate|animat(?:ing|ed|ion|ions))\b"
    r"|上传|下载|备份|添加|同步|移动|删除|创建|动画",
    re.I,
)

# Common closed-set object phrases. Only tightly-bounded alternations
# (e.g. ``photo or video``) and well-known synonyms (e.g. ``pictures``)
# are recognised; an arbitrary phrase like ``existing things`` is NOT
# accepted as a business object. Output tells the caller to surface a
# fixed gap rather than mistreat the surface text as canonical.
_OBJECT_PHRASES = (
    (re.compile(r"\bphoto\s+or\s+video(?:s)?\b", re.I), ("photos", "videos")),
    (re.compile(r"\bvideo\s+or\s+photo(?:s)?\b", re.I), ("videos", "photos")),
    (re.compile(r"\bpictures?\b", re.I), ("photos",)),
)

_OBJECT_ATOMS = re.compile(
    r"\b(?:photo|photos|video|videos|file|files|folder|folders|album|albums)\b"
    r"|照片|图片|视频|文件|文件夹|相册",
    re.I,
)

# Literal format qualifier + a file noun. No format/capability lookup table:
# an unseen acronym is retained verbatim and still needs source proof.
_LITERAL_FILE_OBJECT = re.compile(r"\b([A-Z][A-Z0-9]{1,9})\s+files?\b")

_MODE_ATOMS = re.compile(
    r"\b(?:manual(?:ly)?|automatic(?:ally)?|auto)\b"
    r"|手动|自动",
    re.I,
)

_OUTPUT_ATOMS = re.compile(
    r"\b(?:how|why|what|which|when)\b"
    r"|如何|怎么|为什么|哪些|什么|哪",
    re.I,
)

# Named original entry: a phrase after a preposition like "from the Shared
# tab", or a quoted phrase like '"Shared tab"'. The leading preposition is
# required for the prose form to avoid catching product / feature names.
# The trailing UI-marker word (tab / menu / button / screen / page / section /
# dialog / option / view / pane / sidebar / settings / list / sheet / window /
# folder) keeps "In Google Photos Android" out of the entry slot — that phrase
# is a product+platform clause, not a UI label.
_UI_MARKERS = (
    "tab",
    "tabs",
    "menu",
    "menus",
    "button",
    "buttons",
    "screen",
    "screens",
    "page",
    "pages",
    "section",
    "sections",
    "dialog",
    "dialogs",
    "option",
    "options",
    "view",
    "views",
    "pane",
    "panes",
    "sidebar",
    "settings",
    "list",
    "sheet",
    "window",
    "folder",
    "folders",
)
_NAMED_ENTRY_RE = re.compile(
    r"\b(?:from|in|on|via|using|under|within|into)\s+"
    r"(?:the\s+|a\s+|an\s+)?"
    r"(?P<phr>(?:[A-Z][A-Za-z]+\s+)*?"
    r"(?:" + "|".join(_UI_MARKERS) + r"))\b"
    r"|"
    r"['\"](?P<q>(?:[^'\"]+))['\"]",
    re.I,
)


_PLATFORM_CANONICAL: dict[str, str] = {
    "android": "Android",
    "安卓": "Android",
    "ios": "iOS",
    "iphone": "iOS",
    "ipad": "iOS",
    "ipados": "iOS",
    "mobile": "mobile",
    "手机": "mobile",
    "移动端": "mobile",
    "phone": "phone",
    "tablet": "tablet",
    "平板": "tablet",
    "desktop": "desktop",
    "桌面": "desktop",
    "电脑": "desktop",
    "web": "web",
    "网页": "web",
    "windows": "Windows",
    "macos": "macOS",
    "mac": "macOS",
    "linux": "Linux",
}


_ACTION_CANONICAL: dict[str, str] = {
    "upload": "upload",
    "uploading": "upload",
    "uploaded": "upload",
    "uploads": "upload",
    "上传": "upload",
    "add": "add",
    "adding": "add",
    "added": "add",
    "adds": "add",
    "添加": "add",
    "backup": "backup",
    "back up": "backup",
    "back-up": "backup",
    "backing up": "backup",
    "backed up": "backup",
    "backups": "backup",
    "备份": "backup",
    "sync": "sync",
    "syncing": "sync",
    "synced": "sync",
    "syncs": "sync",
    "同步": "sync",
    "move": "move",
    "moving": "move",
    "moved": "move",
    "moves": "move",
    "移动": "move",
    "delete": "delete",
    "deleting": "delete",
    "deleted": "delete",
    "deletes": "delete",
    "删除": "delete",
    "create": "create",
    "creating": "create",
    "created": "create",
    "creates": "create",
    "创建": "create",
    "animate": "animate",
    "animating": "animate",
    "animated": "animate",
    "animation": "animate",
    "animations": "animate",
    "动画": "animate",
    "download": "download",
    "downloading": "download",
    "downloaded": "download",
    "downloads": "download",
    "下载": "download",
    # Operation-family aliases: tightly bounded verb combos declared by
    # the question. These are NEVER guesses — only literal phrases that
    # the user typed land here. Each maps to a single ``+``-joined
    # canonical the support layer can decode (the comma + and form is
    # still canonicalised to its first token with an unresolved gap).
    "select and backup": "select+backup",
    "选择并备份": "select+backup",
    "select then backup": "select+backup",
    "选择然后备份": "select+backup",
    "select and back up": "select+backup",
}

# Display strings for compound action canonicals — used by support and
# spec_v7 to recognise / categorise an operation-family alias without
# silently splitting it back into two atomic verbs.
_ACTION_FAMILY_PARTS: dict[str, tuple[str, ...]] = {
    "select+backup": ("select", "backup"),
}


_OBJECT_CANONICAL: dict[str, str] = {
    "photo": "photos",
    "photos": "photos",
    "photo or video": "photos|videos",
    "photo or videos": "photos|videos",
    "video or photo": "photos|videos",
    "video or photos": "photos|videos",
    "picture": "photos",
    "pictures": "photos",
    "photo or file": "photos|files",
    "file or photo": "photos|files",
    "photo and video": "photos|videos",
    "photo and photos": "photos",
    "照片": "photos",
    "图片": "photos",
    "video": "videos",
    "videos": "videos",
    "视频": "videos",
    "file": "files",
    "files": "files",
    "文件": "files",
    "folder": "folders",
    "folders": "folders",
    "文件夹": "folders",
    "album": "albums",
    "albums": "albums",
    "相册": "albums",
}


_MODE_CANONICAL: dict[str, str] = {
    "manual": "manual",
    "manually": "manual",
    "手动": "manual",
    "automatic": "automatic",
    "automatically": "automatic",
    "auto": "automatic",
    "自动": "automatic",
    "自动化": "automatic",
}


_OUTPUT_CANONICAL: dict[str, str] = {
    "how": "how",
    "如何": "how",
    "怎么": "how",
    "why": "why",
    "为什么": "why",
    "what": "what",
    "什么": "what",
    "which": "which",
    "哪": "which",
    "when": "when",
}


# ---------------------------------------------------------------------------
# Lazy product registry. The list is read once from the unchanged
# vendor-rules table; we never copy per-product answer tables into this
# module, only the keyword list used to recognise a product mention in the
# original question.
# ---------------------------------------------------------------------------

_PRODUCT_KEYWORDS_CACHE: list[tuple[str, str, str]] | None = None
_PRODUCT_CANONICAL_CACHE: dict[str, str] | None = None


def _product_keywords_sorted() -> list[tuple[str, str, str]]:
    global _PRODUCT_KEYWORDS_CACHE, _PRODUCT_CANONICAL_CACHE
    if _PRODUCT_KEYWORDS_CACHE is None:
        from app.services.research_service import _OFFICIAL_VENDOR_RULES

        seen: set[str] = set()
        ordered: list[tuple[str, str, str]] = []
        canonical_map: dict[str, str] = {}
        for rule_key, rule in _OFFICIAL_VENDOR_RULES.items():
            for kw in rule.get("product_keywords", ()):
                low = kw.casefold()
                if low and low not in seen:
                    seen.add(low)
                    ordered.append((kw, low, rule_key))
                    canonical_map[low] = rule_key
        ordered.sort(key=lambda kv: (-len(kv[1]), kv[1]))
        _PRODUCT_KEYWORDS_CACHE = ordered
        _PRODUCT_CANONICAL_CACHE = canonical_map
    return _PRODUCT_KEYWORDS_CACHE


def _product_pattern() -> re.Pattern[str]:
    keywords = _product_keywords_sorted()
    if not keywords:
        return re.compile(r"(?!)")
    body = "|".join(re.escape(kw) for kw, _, _ in keywords)
    return re.compile(r"(?:" + body + r")", re.I)


def _canonical(value: str, table: dict[str, str]) -> str | None:
    if not value:
        return None
    return table.get(value.casefold())


def reset_cache() -> None:
    """Drop the cached product keyword list. Test fixtures only."""
    global _PRODUCT_KEYWORDS_CACHE, _PRODUCT_CANONICAL_CACHE
    _PRODUCT_KEYWORDS_CACHE = None
    _PRODUCT_CANONICAL_CACHE = None


def extract_scope_atoms(scope_text: str) -> list[dict]:
    """Extract scope-supplement atoms from a ``scope`` text.

    Scope is supplementary input: when the ORIGINAL question is silent
    on a dimension (platform / mode / quantity / entry), the
    run-narrowing ``scope`` clause names the constraint that binds to
    every input obligation produced by the question. The same closed
    grammar used for the question text is reused here — finite
    vocabulary + canonical + ``start`` / ``end`` / ``text`` / ``id``
    — so the spec layer can back-fill an empty requirement field via
    the matched obligation's scope without ever rewriting or moving
    an atom.

    Atoms carry ``source="scope"`` so the spec layer can distinguish
    supplementary constraints from question-owned ones; a scope atom
    never overrides a question atom (explicit Q-vs-scope conflict
    surfaces as an unresolved gap instead of being erased on either
    side). Named entries ARE extracted here using the same
    ``_NAMED_ENTRY_RE`` regex the question ledger uses — the scope
    supplement binds to EXISTING (action, object) leaves, but it
    does need to participate in the same per-leaf entry match
    (Shared tab vs Other tab) and the same per-leaf conflict check.
    Action and object atoms are deliberately NOT extracted — the
    scope supplement does NOT create new business actions or
    objects.
    """
    if not isinstance(scope_text, str) or not scope_text.strip():
        return []

    atoms: list[dict] = []
    counter = {"n": 0}
    used: set[tuple[int, int]] = set()

    def _add(dimension: str, start: int, end: int, text: str, canonical: str) -> None:
        counter["n"] += 1
        atoms.append(
            dict(
                id=f"sa{counter['n']}",
                anchor_id=None,
                dimension=dimension,
                start=start,
                end=end,
                text=text,
                canonical=canonical,
                owners=[],
                source="scope",
            )
        )

    _emit_dimension(
        scope_text,
        _PLATFORM_ATOMS,
        "platform",
        _PLATFORM_CANONICAL,
        used,
        _add,
    )
    _emit_dimension(
        scope_text,
        _MODE_ATOMS,
        "mode",
        _MODE_CANONICAL,
        used,
        _add,
    )
    # Quantity markers reuse the segment grammar the input-obligation
    # ledger already applies — ``Multiple photos in one upload``
    # therefore yields ``multiple`` regardless of which side of the
    # segment it sits on.
    for m in _SEGMENT_MULTIPLE.finditer(scope_text):
        span = (m.start(), m.end())
        if span in used:
            continue
        _add("quantity", *span, m.group(), "multiple")
        used.add(span)
    for m in _SEGMENT_SINGLE.finditer(scope_text):
        span = (m.start(), m.end())
        if span in used:
            continue
        _add("quantity", *span, m.group(), "single")
        used.add(span)
    # Named original entries use the same ``_NAMED_ENTRY_RE``
    # regex the question-input ledger owns. The leading-preposition
    # requirement keeps a product / feature clause (e.g. ``in
    # Google Photos Android``) out of the entry slot; the
    # trailing-UI-marker anchor keeps ``Shared tab`` from
    # collapsing into the next clause. ``canonical`` is the surface
    # text because named entries form a closed stem set the model
    # treats as opaque labels.
    for m in _NAMED_ENTRY_RE.finditer(scope_text):
        groups = m.groupdict()
        if groups.get("phr"):
            start = m.start("phr")
            end = m.end("phr")
            text = m.group("phr")
        elif groups.get("q"):
            start = m.start("q")
            end = m.end("q")
            text = m.group("q")
        else:
            continue
        span = (start, end)
        if span in used:
            continue
        _add("entry", start, end, text, text)
        used.add(span)
    atoms.sort(key=lambda a: (a["start"], a["end"]))
    return atoms


def canonical_product(value: str | None) -> str | None:
    """Normalise a product string to its vendor rule key.

    Aliases registered in the vendor registry (e.g. ``iCloud``,
    ``Shared Photo Library``, ``Apple Support`` for the
    ``icloud_photos`` family) collapse to the SAME canonical so a
    multi-product branch check sees one product, not three.
    Unknown inputs preserve their original surface so the caller
    can detect a never-claimed name.
    """
    if not value:
        return None
    if _PRODUCT_CANONICAL_CACHE is None:
        _product_keywords_sorted()
    if _PRODUCT_CANONICAL_CACHE is None:
        return value.casefold()
    return _PRODUCT_CANONICAL_CACHE.get(value.casefold(), value.casefold())


# ---------------------------------------------------------------------------
# Owners: shared-suffix vs per-branch attribution.
#
# A consecutive pair of product mentions is CONJOINED when the text between
# them contains ONLY conjunctions and punctuation — e.g.
# "OneDrive and Dropbox Android manually upload photos" or
# "Google Drive 和 OneDrive 安卓用户如何上传". Two conjoined products share
# every dimension that appears AFTER the LAST product of the group, so
# neither side is allowed to claim the suffix alone.
#
# A non-conjoined pair (e.g. "OneDrive Android upload and Dropbox iPhone
# move") keeps the near-product boundary: each product owns the segment
# from its mention up to the next product mention. An atom placed BEFORE
# any product mention binds to every product (the whole question is about
# all of them).
# ---------------------------------------------------------------------------

_CONJUNCTION_TOKENS: frozenset[str] = frozenset(
    {
        "and",
        "or",
        "vs",
        "vs.",
        "versus",
        "和",
        "与",
        "及",
        "或",
        "以及",
        "跟",
        "同",
    }
)
_TOKEN_RE = re.compile(r"[A-Za-z一-鿿]+")


def _is_conjunction_only_gap(text: str) -> bool:
    """True iff ``text`` is empty or contains only conjunctions/punctuation.

    Used to decide whether two consecutive product mentions share a common
    suffix (a single conjunctive phrase like "and", "和", a comma) or are
    two separate branches with their own per-product dimensions.
    """
    if not text or not text.strip():
        return True
    for tok in _TOKEN_RE.findall(text):
        if tok.lower() not in _CONJUNCTION_TOKENS and tok not in _CONJUNCTION_TOKENS:
            return False
    return True


def _attribute_owners(atoms: list[dict], question: str = "") -> None:
    """Attribute each non-product atom to its owning product(s).

    See module-level comment for the shared-suffix vs per-branch rule.
    The ``question`` string is only consulted to inspect the gap between
    two consecutive product mentions; passing it is required to honour
    conjunctive phrases (e.g. ``"and"``, ``"和"``) instead of always
    defaulting to the nearest preceding product.
    """
    product_atoms = sorted(
        (a for a in atoms if a["dimension"] == "product"), key=lambda a: a["start"]
    )
    if not product_atoms:
        for atom in atoms:
            atom["owners"] = []
        return

    # 1. Group consecutive conjoined products.
    groups: list[list[dict]] = [[product_atoms[0]]]
    for nxt in product_atoms[1:]:
        prev = groups[-1][-1]
        gap = question[prev["end"] : nxt["start"]]
        if _is_conjunction_only_gap(gap):
            groups[-1].append(nxt)
        else:
            groups.append([nxt])

    # 2. Each group owns [first_product.start, next_group_first_product.start)
    # or to end-of-question for the last group. A conjoined group's owners
    # are the union of all member products so the shared suffix is reachable
    # by every side.
    q_end = len(question) if question else max((a["end"] for a in atoms), default=0)
    spans: list[tuple[int, int, list[str]]] = []
    for i, group in enumerate(groups):
        start = group[0]["start"]
        end = groups[i + 1][0]["start"] if i + 1 < len(groups) else q_end
        canonicals = sorted({p["canonical"] for p in group})
        spans.append((start, end, canonicals))

    all_canonicals = sorted({p["canonical"] for p in product_atoms})
    for atom in atoms:
        if atom["dimension"] == "product":
            atom["owners"] = [atom["canonical"]]
            continue
        owners: list[str] = []
        for s, e, canonicals in spans:
            if s <= atom["start"] < e:
                owners = canonicals
                break
        if not owners:
            # Atom before any product mention: bind to every product so the
            # whole-question grammar still owns it.
            owners = all_canonicals
        atom["owners"] = owners


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_input_atoms(
    question: str,
    anchors: dict | None = None,
    anchor_list: list | None = None,
) -> list[dict]:
    """Return the original-question input obligation ledger.

    Each atom keeps an exact substring of ``question`` together with its
    canonical form and a list of owner product canonicals. An unknown
    mapping preserves the original text and is left for human / source
    review — never coerced into a fuzzy equivalence.

    ``anchors`` and ``anchor_list`` map each atom to the question-anchor
    that contains it (used later by compile_spec to scope validation per
    requirement).
    """
    if not isinstance(question, str):
        return []

    atoms: list[dict] = []
    counter = {"n": 0}

    def _add(dimension: str, start: int, end: int, text: str, canonical: str) -> None:
        # When anchor_list is supplied (compile_spec path), atoms whose
        # span lives outside ANY anchor — i.e. inside a policy_spans
        # slice excluded by ``question_parts`` — are not business
        # atoms. Emitting them would let a quoted policy clause
        # silently introduce obligations ("…Using only official
        # Google Drive upload files Help, can …" must not surface an
        # ``upload files`` obligation).
        anchor_id = None
        if anchor_list:
            inside = False
            for a in anchor_list:
                if a["start"] <= start and end <= a["end"]:
                    inside = True
                    anchor_id = a["id"]
                    break
            if not inside:
                return
        counter["n"] += 1
        atoms.append(
            dict(
                id=f"a{counter['n']}",
                anchor_id=anchor_id,
                dimension=dimension,
                start=start,
                end=end,
                text=text,
                canonical=canonical,
                owners=[],
            )
        )

    used: set[tuple[int, int]] = set()
    # Product mentions are protected ranges. A trailing noun like the
    # ``Photos`` inside ``Google Photos`` is NOT a business object, and
    # the platform / action grammars must never claim a token that
    # lives inside a product name. ``product_spans`` collects every
    # match so the downstream ``_emit_dimension`` and the named-entry
    # regex can both bail out via ``_overlaps_product``.
    product_spans: list[tuple[int, int]] = []

    # 1) Products first so subsequent atoms can be attributed to a branch.
    # The canonical form is the vendor RULE KEY (e.g. ``icloud_photos``) so
    # the multi-product branch check sees ONE product per vendor family,
    # not several aliases of the same family.
    for m in _product_pattern().finditer(question):
        span = (m.start(), m.end())
        if span in used:
            continue
        canon = canonical_product(m.group()) or m.group().casefold()
        _add("product", *span, m.group(), canon)
        used.add(span)
        product_spans.append(span)

    def _overlaps_product(start: int, end: int) -> bool:
        for ps, pe in product_spans:
            if start < pe and ps < end:
                return True
        return False

    # 2) Platform / action / object / mode / output. Each dimension
    # respects the product protected range so the trailing noun inside
    # a product name is never accidentally promoted to a business atom.
    _emit_dimension(
        question,
        _PLATFORM_ATOMS,
        "platform",
        _PLATFORM_CANONICAL,
        used,
        _add,
        skip=_overlaps_product,
    )
    # Platform-entity span protection: action atoms may NOT overlap a
    # recognised platform entity such as ``移动端``. The platform
    # grammar has already won those tokens; letting the action grammar
    # carve a sub-span back out (e.g. ``移动`` inside ``移动端``)
    # would let a phantom ``move`` obligation leak into the question
    # ledger when the original text actually pins ``mobile``.
    platform_spans: list[tuple[int, int]] = [
        (a["start"], a["end"]) for a in atoms if a["dimension"] == "platform"
    ]

    def _overlaps_platform_or_product(start: int, end: int) -> bool:
        if _overlaps_product(start, end):
            return True
        for ps, pe in platform_spans:
            if start < pe and ps < end:
                return True
        return False

    _emit_dimension(
        question,
        _ACTION_ATOMS,
        "action",
        _ACTION_CANONICAL,
        used,
        _add,
        skip=_overlaps_platform_or_product,
    )
    # Closed-set object phrases (e.g. ``photo or video``). Emitted
    # BEFORE the keyword regex so a smaller sub-span inside the phrase
    # never wins the ``used`` slot.
    for phrase_re, members in _OBJECT_PHRASES:
        for m in phrase_re.finditer(question):
            span = (m.start(), m.end())
            if span in used or _overlaps_product(*span):
                continue
            joined = "|".join(members)
            _add("object", *span, m.group(), joined)
            used.add(span)
    literal_object_spans = []
    for m in _LITERAL_FILE_OBJECT.finditer(question):
        if not _overlaps_product(m.start(), m.end()):
            _add("object", m.start(), m.end(), m.group(), m.group(1).casefold() + " files")
            literal_object_spans.append((m.start(), m.end()))
    _emit_dimension(
        question,
        _OBJECT_ATOMS,
        "object",
        _OBJECT_CANONICAL,
        used,
        _add,
        skip=lambda start, end: (
            _overlaps_product(start, end)
            or any(start < b and a < end for a, b in literal_object_spans)
        ),
    )
    _emit_dimension(
        question,
        _MODE_ATOMS,
        "mode",
        _MODE_CANONICAL,
        used,
        _add,
        skip=_overlaps_product,
    )

    for m in _OUTPUT_ATOMS.finditer(question):
        span = (m.start(), m.end())
        if span in used or _overlaps_product(*span):
            continue
        canon = _OUTPUT_CANONICAL.get(m.group().casefold(), m.group().casefold())
        _add("output", *span, m.group(), canon)
        used.add(span)

    # 3) Named original entry. The UI-marker regex is narrow on purpose,
    # but the product protection still applies — a product name with a
    # trailing ``tab`` should never become an entry.
    for m in _NAMED_ENTRY_RE.finditer(question):
        groups = m.groupdict()
        if groups.get("phr"):
            start = m.start("phr")
            end = m.end("phr")
            text = m.group("phr")
        elif groups.get("q"):
            start = m.start("q")
            end = m.end("q")
            text = m.group("q")
        else:
            continue
        span = (start, end)
        if span in used or _overlaps_product(*span):
            continue
        _add("entry", start, end, text, text)
        used.add(span)

    atoms.sort(key=lambda a: (a["start"], a["end"]))
    _attribute_owners(atoms, question)
    return atoms


def _emit_dimension(
    question: str,
    pattern: re.Pattern[str],
    dimension: str,
    table: dict[str, str],
    used: set[tuple[int, int]],
    add,
    skip=None,
) -> None:
    for m in pattern.finditer(question):
        span = (m.start(), m.end())
        if span in used:
            continue
        if skip is not None and skip(*span):
            continue
        canon = _canonical(m.group(), table)
        if not canon:
            continue
        add(dimension, m.start(), m.end(), m.group(), canon)
        used.add(span)


# ---------------------------------------------------------------------------
# Input obligations: (action, object, mode) tuples per anchor.
#
# The atomic ledger above answers "which dimensions are mentioned?". The
# obligation ledger below answers "which (action, object, mode) tuples
# must each factual requirement cover?". The candidate requirement set
# is incomplete unless every obligation is fulfilled by exactly one
# factual requirement — never by an ``analysis`` or ``comparison``
# umbrella, which never proves a single action against a source page.
#
# Algorithm (per-action-position segmentation):
#     1. Condition filtering: an action atom whose span lives inside an
#        existing ``when``/``while``/``if`` condition (e.g. ``when
#        camera uploads are disabled``) is removed from the request
#        ledger entirely; it is part of a quoted precondition, not a
#        new requested action. The condition itself remains tracked by
#        ``mandatory_conditions`` separately.
#     2. Per action atom, identify the segment of objects that belong
#        to it: the slice from the previous action atom's end to the
#        next action atom's start. The FIRST action atom also absorbs
#        objects before its start so ``photo upload`` / ``start
#        backup`` keep their preceding noun. Each object atom in the
#        slice becomes a separate (action, object, mode) obligation
#        group — never one obligation per ``next()``-pick of a segment.
#     3. Empty-object action atoms may borrow from the next sibling
#        segment ONLY when the owner sets match; the borrow is a single
#        lookup, not a chain.
#     4. Multiple modes inside the same action slice become one
#        obligation per (action, object, mode) combination so
#        ``manual and automatic upload photos`` still requires BOTH
#        a manual and an automatic answer.
#     5. Per-owner fan-out: one obligation per product branch on the
#        owner list (so ``OneDrive + Dropbox upload photos and delete
#        files`` requires FOUR distinct obligations, never two
#        diagonal coverage).
#     6. Compound objects (``photos|videos``) expand into one
#        obligation per noun so each ``photo or video`` member still
#        needs its own answer.
# ---------------------------------------------------------------------------

_CONDITIONS_OBLIGATION_FILTER = re.compile(
    # Local fallback retained for callers that invoke
    # ``_condition_spans`` BEFORE spec_v7 is importable. The canonical
    # source is now ``research_spec_v7.condition_spans`` so the input
    # and spec layers share the same grammar — duplicating only the
    # trigger word (e.g. "关闭") would let the inner verb ("上传")
    # leak back into the obligation ledger and re-introduce
    # conditional verbs as requested actions.
    r"\b(?:while|when|unless|without|if|before|after)\b[^;?！!。？\n.]*|\boffline\b|离线|(?:禁用|关闭)(?!条件)[^，,；。？！\n]*",
    re.I,
)


def _condition_spans(text: str) -> list[tuple[int, int]]:
    """Return absolute ``(start, end)`` spans of condition clauses.

    Defer to ``research_spec_v7.condition_spans`` whenever it is
    importable so the input ledger and the spec's
    ``mandatory_conditions`` / ``condition_inventory`` share the
    exact same grammar. Falls back to the local regex only when
    spec_v7 itself is still being initialised to avoid recursion.
    """
    try:
        from app.services import research_spec_v7 as _spec_v7  # noqa: F401
    except Exception:
        pass
    else:
        if getattr(_spec_v7, "condition_spans", None) is not None:
            try:
                return _spec_v7.condition_spans(text)
            except Exception:
                pass
    out: list[tuple[int, int]] = []
    for m in _CONDITIONS_OBLIGATION_FILTER.finditer(text):
        out.append((m.start(), m.end()))
    return out


def _in_condition(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    """True iff ``(start, end)`` is fully inside one of ``spans``."""
    for cs, ce in spans:
        if cs <= start and end <= ce:
            return True
    return False


# Per-segment quantity grammar. Each obligation's span slice
# (``action`` to the next action or anchor end) carries the
# quantity markers right next to its object phrase (``one file``,
# ``multiple photos``). Anchoring quantity to the whole-anchor
# text instead would broadcast a sibling ``multiple photos`` mark
# onto the unrelated ``upload one file`` obligation.
_SEGMENT_MULTIPLE = re.compile(
    r"\b(?:multiple|several|multi[- ]select|batch|more than one|许多|多个|多张|多选|批量)\b",
    re.I,
)
_SEGMENT_SINGLE = re.compile(
    r"\b(?:one|a single|single|one of (?:the |your )?)\s+"
    r"(?:photo|video|file|picture|image|item|folder|album|文件|照片|图片|视频|相册|文件夹)\b"
    r"|一张|一个(?:文件)?|单个",
    re.I,
)


def _segment_quantity(text: str) -> str:
    """Return ``multiple`` / ``single`` / ``unspecified`` for a
    segment's quantity markers. ``multiple`` wins on collision so a
    single ``multiple`` token unambiguously marks the leaf rather
    than its sibling (the segment window only contains the leaf's
    own modifiers in the first place)."""
    if not text:
        return "unspecified"
    if _SEGMENT_MULTIPLE.search(text):
        return "multiple"
    if _SEGMENT_SINGLE.search(text):
        return "single"
    return "unspecified"


def _segment_quantity_match(text: str) -> tuple[str, str, int, int] | None:
    """X-axes2: same grammar as :func:`_segment_quantity`, but
    returns the actual original token ``(canonical, text, start,
    end)`` with offsets local to ``text``. ``None`` when no marker
    is present. The caller adds the segment's base offset to recover
    absolute indices, and uses ``text`` as the user-visible quantity
    marker in ``quantity_scope`` (instead of writing the canonical
    string ``multiple`` paired with a fake object span).
    """
    if not text:
        return None
    m = _SEGMENT_MULTIPLE.search(text)
    if m:
        return "multiple", m.group(0), m.start(), m.end()
    m = _SEGMENT_SINGLE.search(text)
    if m:
        return "single", m.group(0), m.start(), m.end()
    return None


def extract_input_obligations(
    question: str,
    anchor_list: list | None = None,
) -> list[dict]:
    """Return ordered ``(action, object, mode)`` tuples per question anchor.

    Each obligation preserves the EXACT span of its atoms in the original
    question, the canonical form, AND the per-owner product branches.
    Anchors come from ``anchor_list`` when supplied; otherwise all atoms
    share one implicit anchor.

    Action atoms whose spans fall inside a ``when`` / ``while`` / ``if``
    condition are excluded from the obligation ledger — the condition
    is tracked by ``mandatory_conditions``, not as a new request action.
    """
    if not isinstance(question, str):
        return []

    atoms = extract_input_atoms(
        question, anchors={a["id"]: a for a in (anchor_list or [])}, anchor_list=anchor_list
    )

    cond_spans = _condition_spans(question)
    # Action atoms living inside a condition span are filtered out before
    # segmenting; their wording is part of the quoted precondition, not a
    # new requested action (``when camera uploads are disabled`` does
    # NOT introduce a fresh ``uploads`` request).
    usable_atoms = [
        a
        for a in atoms
        if not (a["dimension"] == "action" and _in_condition(a["start"], a["end"], cond_spans))
    ]

    by_anchor: dict[str, list[dict]] = {}
    for atom in usable_atoms:
        aid = atom.get("anchor_id") or ""
        by_anchor.setdefault(aid, []).append(atom)

    anchors_meta = {a["id"]: a for a in (anchor_list or [])}
    obligations: list[dict] = []
    counter = {"n": 0}

    for aid, anchor_atoms in by_anchor.items():
        if not anchor_atoms:
            continue
        if aid:
            anchor_meta = anchors_meta.get(aid)
            if not anchor_meta:
                continue
            text_start, text_end = anchor_meta["start"], anchor_meta["end"]
        else:
            text_start = min(a["start"] for a in anchor_atoms)
            text_end = max(a["end"] for a in anchor_atoms)

        # Per-action-position segmentation: each action atom owns
        # the slice of objects [previous_action_end, next_action_start).
        # The FIRST action atom extends backwards into any preceding
        # objects so phrases like ``photo upload`` keep their leading
        # noun. We do NOT keep relying on a separator dictionary: the
        # implementation only walks atom positions, so ``同时`` /
        # ``plus`` / any other non-keyword connector does not collapse
        # adjacent action positions into one group.
        actions = sorted(
            [a for a in anchor_atoms if a["dimension"] == "action"],
            key=lambda a: a["start"],
        )
        if not actions:
            continue

        objects_list = sorted(
            [a for a in anchor_atoms if a["dimension"] == "object"],
            key=lambda a: a["start"],
        )
        modes_list = sorted(
            [a for a in anchor_atoms if a["dimension"] == "mode"],
            key=lambda a: a["start"],
        )

        n_actions = len(actions)

        def _shares_owner(atom: dict, owner_set: frozenset[str]) -> bool:
            atom_owners = frozenset(atom.get("owners", []) or [])
            if not owner_set or not atom_owners:
                # At least one side carries no explicit owner; treat as
                # brand-agnostic so the test grammar still binds it.
                return True
            return atom_owners == owner_set

        # Forward mode binding: each mode atom pairs with the FIRST
        # action atom whose start position is strictly greater than
        # the mode's start (i.e. the action that immediately
        # follows). Modes appearing before the first action all bind
        # to the first action (``manual and automatic upload photos``
        # keeps both modes on the one upload obligation). A mode
        # with NO subsequent action (trailing mode) attaches to the
        # last action so`` upload photos auto`` still resolves.
        action_modes: list[list[dict]] = [[] for _ in range(n_actions)]
        for m in modes_list:
            target_idx = None
            for ai, a in enumerate(actions):
                if a["start"] > m["start"]:
                    target_idx = ai
                    break
            if target_idx is None and n_actions:
                target_idx = n_actions - 1
            if target_idx is not None:
                action_modes[target_idx].append(m)

        # Per-action platform / entry binding: each platform atom is
        # bound to the FIRST action whose start position is strictly
        # greater (forward binding — ``Android upload`` binds Android
        # to upload, ``iPhone delete`` binds iPhone to delete). Each
        # entry atom is bound to the LAST action whose start position
        # is <= the atom's start (backward binding — ``from the
        # Shared tab`` modifies the upload it follows, ``from the
        # Other tab`` modifies the delete it follows). Atoms that
        # fall BEFORE the first action fall back to action 0 in both
        # directions so a leading ``Android`` / ``from the Home
        # screen`` still pins the first leaf. This isolates the
        # platform / entry constraint per (action, object) leaf so
        # two platforms in the same anchor do not collapse onto one
        # obligation and two named entries (``Shared tab`` +
        # ``Other tab``) stay attached to their own leaves instead of
        # being unified into a single entry slot.
        platforms_list = sorted(
            [a for a in anchor_atoms if a["dimension"] == "platform"],
            key=lambda a: a["start"],
        )
        entries_list = sorted(
            [a for a in anchor_atoms if a["dimension"] == "entry"],
            key=lambda a: a["start"],
        )

        def _bind_forward(atom: dict) -> int | None:
            target_idx = None
            for ai, a in enumerate(actions):
                if a["start"] > atom["start"]:
                    target_idx = ai
                    break
            if target_idx is None and n_actions:
                target_idx = 0
            return target_idx

        def _bind_backward(atom: dict) -> int | None:
            target_idx = None
            for ai, a in enumerate(actions):
                if a["start"] <= atom["start"]:
                    target_idx = ai
                else:
                    break
            if target_idx is None and n_actions:
                target_idx = 0
            return target_idx

        action_platforms: list[list[dict]] = [[] for _ in range(n_actions)]
        for p in platforms_list:
            previous = [i for i, a in enumerate(actions) if a["start"] < p["start"]]
            following = [i for i, a in enumerate(actions) if a["start"] > p["start"]]
            if not previous:
                target_idx = following[0] if following else None
            elif not following:
                target_idx = previous[-1]
            else:
                before = question[actions[previous[-1]]["end"] : p["start"]]
                # Coordinators start the next action clause; a trailing
                # device phrase otherwise belongs to the preceding action.
                target_idx = (
                    following[0]
                    if re.search(r"\b(?:and|or|then)\b|[;,；，]|并|然后|再", before, re.I)
                    else previous[-1]
                )
            if target_idx is not None:
                action_platforms[target_idx].append(p)
        action_entries: list[list[dict]] = [[] for _ in range(n_actions)]
        for e in entries_list:
            target_idx = _bind_backward(e)
            if target_idx is not None:
                action_entries[target_idx].append(e)

        for i, action in enumerate(actions):
            next_action_start = actions[i + 1]["start"] if i < n_actions - 1 else text_end

            action_owners = frozenset(action.get("owners", []) or [])

            # First action extends BACKWARDS to capture any preceding
            # object atom — the user's ``photo upload`` / ``start
            # backup`` cases rely on this so the leading noun is not
            # orphaned.
            if i == 0:
                preceding = [
                    o
                    for o in objects_list
                    if o["end"] <= action["start"] and _shares_owner(o, action_owners)
                ]
                following = [
                    o
                    for o in objects_list
                    if action["end"] <= o["start"] < next_action_start
                    and _shares_owner(o, action_owners)
                ]
                candidate_objs = preceding + following
            else:
                candidate_objs = [
                    o
                    for o in objects_list
                    if action["end"] <= o["start"] < next_action_start
                    and _shares_owner(o, action_owners)
                ]

            # Empty parallel action (e.g. ``select ... and start
            # backup``) shares the FIRST object of the next sibling
            # segment when the owner sets match — one borrow, no chain.
            if not candidate_objs and i < n_actions - 1:
                next_action = actions[i + 1]
                next_owners = frozenset(next_action.get("owners", []) or [])
                same_owner = action_owners == next_owners or not action_owners or not next_owners
                if same_owner:
                    next_next_start = actions[i + 2]["start"] if i + 2 < n_actions else text_end
                    borrowed = [
                        o
                        for o in objects_list
                        if next_action["end"] <= o["start"] < next_next_start
                        and _shares_owner(o, action_owners)
                    ]
                    if borrowed:
                        candidate_objs = [borrowed[0]]

            elliptical_object = False
            if (
                not candidate_objs
                and i > 0
                and action_modes[i]
                and actions[i - 1]["canonical"] == action["canonical"]
            ):
                previous = actions[i - 1]
                if _shares_owner(previous, action_owners):
                    candidate_objs = [
                        o
                        for o in objects_list
                        if previous["end"] <= o["start"] < action["start"]
                        and _shares_owner(o, action_owners)
                    ]
                    elliptical_object = bool(candidate_objs)
            if not candidate_objs:
                # No object: let ``compile_spec`` surface the gap as an
                # explicit unresolved obligation; never silently bind.
                continue

            # Mode slice: forward-bound modes for this action only.
            # Each mode atom binds to the next action atom; trailing
            # modes fall back to the last action. A mode whose span
            # lives inside a quoted condition is still excluded.
            candidate_modes = [
                m
                for m in action_modes[i]
                if _shares_owner(m, action_owners)
                and not _in_condition(m["start"], m["end"], cond_spans)
            ]
            if not candidate_modes:
                modes_to_emit = [None]
            else:
                modes_to_emit = candidate_modes

            base_owners = sorted(action_owners) if action_owners else []

            last_atom_end = action["end"]
            # X-axes2 fix ②: preceding-object window starts at the
            # anchor's text_start so multiple preceding objects
            # within the same anchor each slice off their own local
            # phrase, and the window never bleeds across anchor
            # boundaries. ``last_atom_end`` keeps tracking the
            # forward-side tail as before.
            preceding_window_start = text_start
            for obj in candidate_objs:
                # Per-owner fan-out honours the union of the action's
                # owners AND the object's owners so conjoined and
                # non-conjoined multi-product questions both produce
                # one obligation per declared branch.
                obj_owners = sorted(
                    frozenset(obj.get("owners", []) or []) or frozenset(action_owners)
                )
                if obj_owners:
                    owners_per = [[o] for o in obj_owners]
                elif base_owners:
                    owners_per = [[o] for o in base_owners]
                else:
                    owners_per = [[]]

                members = object_compound_members(obj["canonical"])
                if not members:
                    members = (obj["canonical"],)
                members_list = list(members)

                # Per-object quantity binding: each object's segment
                # runs from the previous atom's end (or the action's
                # end for the first following object) to the object's
                # own end. ``upload one file and multiple photos``
                # therefore yields ``single`` for the files leaf and
                # ``multiple`` for the photos leaf even when both
                # objects share the same action — the previous
                # action-segment window would have let ``multiple``
                # win for both.
                #
                # X-axes2 fix ②: preceding objects (those whose span
                # lives BEFORE the action's surface form, e.g. the
                # ``multiple photos`` in ``select multiple photos
                # and start backup``) used to be hard-coded to
                # ``unspecified``, which silently dropped a legitimate
                # ``multiple`` marker and let the model reject the
                # otherwise-correct leaf. The preceding-object window
                # now runs from the previous preceding object's end
                # (or the anchor's start for the first preceding
                # object) to this object's own end so each preceding
                # object sees ONLY its own local phrase window —
                # multiple preceding objects in the same anchor stay
                # isolated and one cannot broadcast another's marker
                # (``save file and multiple images`` keeps ``file``
                # unspecified while ``multiple images`` carries the
                # marker). Following objects continue to use the
                # forward window from the previous atom's end.
                quantity_match: tuple[str, str, int, int] | None = None
                if obj["end"] <= action["start"]:
                    seg_start = preceding_window_start
                    quantity_match = _segment_quantity_match(question[seg_start : obj["end"]])
                    preceding_window_start = obj["end"]
                else:
                    seg_start = last_atom_end
                    quantity_match = _segment_quantity_match(question[seg_start : obj["end"]])
                if elliptical_object:
                    quantity_match = None
                if quantity_match is not None:
                    obligation_quantity = quantity_match[0]
                else:
                    obligation_quantity = "unspecified"
                last_atom_end = max(last_atom_end, obj["end"])

                # X-axes fix ②: per-action platform fan-out. When the
                # action's leaf carries multiple platform atoms
                # (``Android`` + ``iPhone`` for ``Can OneDrive
                # Android and iPhone upload photos?``), each platform
                # MUST produce its own independent obligation so a
                # single-platform candidate (Android only) cannot
                # consume the iPhone obligation. The previous
                # implementation accumulated every platform atom into
                # ONE obligation's ``platform_scope`` list, which the
                # per-obligation platform guard in ``_fulfills`` then
                # treated as a wildcard union — letting the iPhone
                # obligation match an Android candidate. Iterating
                # ``platform_atom`` as the outermost fan-out here
                # gives each branch its own stable id (counter["n"])
                # and an accurate single-atom span. Empty platform
                # list (``action_platforms[i] == []``) is preserved as
                # a single ``[None]`` sentinel so obligations without
                # a platform constraint still emit with an empty
                # ``platform_scope`` — unchanged behaviour for any
                # leaf that doesn't name a specific device.
                platforms_to_emit = action_platforms[i] if action_platforms[i] else [None]
                for platform_atom in platforms_to_emit:
                    for owners_subset in owners_per:
                        # A shared action can precede several product branches,
                        # each with its own trailing device. Preserve those
                        # original owners instead of multiplying all devices
                        # across all products. Conjoined products already share
                        # the platform atom's owners and still fan out normally.
                        if (
                            platform_atom is not None
                            and owners_subset
                            and platform_atom.get("owners")
                            and not set(owners_subset) & set(platform_atom["owners"])
                        ):
                            continue
                        for member_index, member_canon in enumerate(members_list):
                            for mode_atom in modes_to_emit:
                                counter["n"] += 1
                                atoms_in = [action, obj]
                                span_start = min(a["start"] for a in atoms_in)
                                span_end = max(a["end"] for a in atoms_in)
                                if mode_atom is not None:
                                    span_start = min(span_start, mode_atom["start"])
                                    span_end = max(span_end, mode_atom["end"])
                                # mode_scope / quantity_scope carry the
                                # per-action mode / quantity markers (from
                                # the ORIGINAL question) so compile_spec can
                                # decide whether to consult the global
                                # ``scope`` constraint, conflict-resolve a
                                # question-vs-scope clash, or back-fill the
                                # requirement via the matched relation.
                                mode_scope_atoms: list[dict] = []
                                if mode_atom is not None:
                                    mode_scope_atoms.append(
                                        dict(
                                            id=mode_atom["id"],
                                            canonical=mode_atom["canonical"],
                                            text=mode_atom["text"],
                                            start=mode_atom["start"],
                                            end=mode_atom["end"],
                                            source="question",
                                        )
                                    )
                                quantity_scope_atoms: list[dict] = []
                                if quantity_match is not None:
                                    # X-axes2: write the actual
                                    # original token (``multiple`` /
                                    # ``许多`` / ``one photo`` …) with
                                    # its real absolute span, not the
                                    # canonical string paired with the
                                    # object's span as a stand-in.
                                    _canonical, _tok_text, _local_start, _local_end = quantity_match
                                    quantity_scope_atoms.append(
                                        dict(
                                            id=obj["id"],
                                            canonical=_canonical,
                                            text=question[
                                                seg_start + _local_start : seg_start + _local_end
                                            ],
                                            start=seg_start + _local_start,
                                            end=seg_start + _local_end,
                                            source="question",
                                        )
                                    )
                                platform_scope_atoms: list[dict] = []
                                if platform_atom is not None:
                                    platform_scope_atoms.append(
                                        dict(
                                            id=platform_atom["id"],
                                            canonical=platform_atom["canonical"],
                                            text=platform_atom["text"],
                                            start=platform_atom["start"],
                                            end=platform_atom["end"],
                                            source="question",
                                        )
                                    )
                                obligations.append(
                                    dict(
                                        id=f"ob{counter['n']}",
                                        anchor_id=aid or None,
                                        action_canon=action["canonical"],
                                        action_text=action["text"],
                                        action_span=(action["start"], action["end"]),
                                        object_canon=member_canon,
                                        object_text=obj["text"],
                                        object_span=(obj["start"], obj["end"]),
                                        mode_canon=mode_atom["canonical"] if mode_atom else None,
                                        mode_text=mode_atom["text"] if mode_atom else None,
                                        mode_span=(
                                            (mode_atom["start"], mode_atom["end"])
                                            if mode_atom
                                            else None
                                        ),
                                        span=(span_start, span_end),
                                        text=question[span_start:span_end],
                                        owners=list(owners_subset),
                                        quantity=obligation_quantity,
                                        mode_scope=mode_scope_atoms,
                                        quantity_scope=quantity_scope_atoms,
                                        platform_scope=platform_scope_atoms,
                                        entry_scope=[
                                            dict(
                                                id=e["id"],
                                                canonical=e["canonical"],
                                                text=e["text"],
                                                start=e["start"],
                                                end=e["end"],
                                                source="question",
                                            )
                                            for e in action_entries[i]
                                        ],
                                        compound_member_index=member_index
                                        if len(members_list) > 1
                                        else 0,
                                        compound_total=len(members_list),
                                    )
                                )
    return obligations


def _owner_key(atoms: list[dict]) -> frozenset[str]:
    """Combine all distinct product canonicals that own the listed atoms.

    Empty when no atom declares an owner (e.g. a single-product question
    where the lead atom doesn't bind to anyone). Used as the cross-
    branch filter for nearest-neighbour inheritance.
    """
    out: set[str] = set()
    for a in atoms:
        for owner in a.get("owners", []) or []:
            out.add(owner)
    return frozenset(out)


# ---------------------------------------------------------------------------
# Lookup helpers used by compile_spec / support. Always operate on the
# canonical form so the original question text stays untouched.
# ---------------------------------------------------------------------------


def canonical_action(value: str | None) -> str | None:
    """Normalise an action string to its canonical form.

    Compound operation-family phrases (e.g. ``select and backup``) are
    mapped to a ``+``-joined canonical so a single candidate action can
    cover a multi-step combo without leaking into ``action_unknown``.
    Otherwise the value is reduced to a registered simple verb.
    """
    if not value:
        return None
    low = value.casefold().strip()
    if not low:
        return None
    return _ACTION_CANONICAL.get(low)


def canonical_object(value: str | None) -> str | None:
    """Normalise an object string to a canonical noun.

    Compound closed-set phrases (e.g. ``photo or video``) collapse to a
    ``|``-joined canonical which the support layer iterates. Synonyms
    like ``pictures`` -> ``photos`` sit in the same table. Arbitrary
    unseen strings return ``None`` so the spec layer surfaces a fixed
    gap rather than silently accepting any noun phrase.
    """
    if not value:
        return None
    low = value.casefold().strip()
    if not low:
        return None
    literal = _LITERAL_FILE_OBJECT.fullmatch(value.strip())
    if literal:
        return literal.group(1).casefold() + " files"
    return _OBJECT_CANONICAL.get(low)


def canonical_mode(value: str | None) -> str | None:
    """Normalise a mode string. Returns ``None`` for ``mixed`` / ``unspecified``
    when the literal is already an enum member so the spec layer can
    recognise comparison-only ``mixed`` without a per-leaf mode atom.
    The literal mode word is its own canonical — the spec check below
    only rejects truly unknown strings.
    """
    if not value:
        return None
    low = value.casefold().strip()
    if not low:
        return None
    return _MODE_CANONICAL.get(low, low)


def canonical_platform(value: str | None) -> str | None:
    """Normalise a platform string. Returns the display enum (e.g. ``Android``)
    when the input matches a known form so back-fills stay schema-legal.
    """
    if not value:
        return None
    low = value.casefold().strip()
    if not low:
        return None
    return _PLATFORM_CANONICAL.get(low, low)


__all__ = [
    "extract_input_atoms",
    "extract_input_obligations",
    "extract_scope_atoms",
    "canonical_action",
    "canonical_object",
    "canonical_mode",
    "canonical_platform",
    "canonical_product",
    "action_family_parts",
    "object_compound_members",
    "action_atoms_in_text",
    "auxiliary_verbs_in_text",
    "reset_cache",
]


def action_family_parts(canon: str | None) -> tuple[str, ...]:
    """Decompose a ``+``-joined action family canonical into its leaves.

    ``select+backup`` -> ``("select", "backup")``. Plain canonicals
    return a single-element tuple.
    """
    if not canon:
        return ()
    if "+" in canon:
        return _ACTION_FAMILY_PARTS.get(canon, tuple(canon.split("+")))
    return (canon,)


def object_compound_members(canon: str | None) -> tuple[str, ...]:
    """Decompose a ``|``-joined compound object canonical into its nouns.

    ``photos|videos`` -> ``("photos", "videos")``. Plain canonicals
    return a single-element tuple.
    """
    if not canon:
        return ()
    if "|" in canon:
        return tuple(canon.split("|"))
    return (canon,)


# Auxiliary verbs that frequently appear as the SELECT / CHOOSE / TAP
# half of an operation family ("select and backup", "choose and
# backup"). They are NOT registered action atoms — the input-obligation
# ledger would have to invent a verb just to detect them — so the
# canonical action family wraps the REAL operation plus one
# auxiliary. The spec layer allows the auxiliary part when (and only
# when) the SAME word literally occurs in the original question, so
# ``select and delete`` still fails action_replaced even though
# ``delete`` is a registered action atom in its own right.
_AUX_VERBS_RE = re.compile(
    r"\b(?:select|choose|pick|tap|click|press|touch|hold|open|start|begin|navigate|enter)\b",
    re.I,
)


def auxiliary_verbs_in_text(text: str) -> list[str]:
    """Find auxiliary verbs (casefolded) present in ``text``.

    Returns the surface-matched words (lowercased) in source order.
    """
    return [m.group().casefold() for m in _AUX_VERBS_RE.finditer(text or "")]


def action_atoms_in_text(text: str) -> list[tuple[int, int, str, str]]:
    """Locate action atoms in arbitrary text using the same regex the
    input-obligation ledger uses.

    Returns ``[(start, end, surface, canonical), ...]`` so callers can
    reuse the full inflection coverage (e.g. ``uploads`` -> ``upload``,
    ``added`` -> ``add``) without copying the regex.
    """
    out: list[tuple[int, int, str, str]] = []
    if not text:
        return out
    for m in _ACTION_ATOMS.finditer(text):
        canon = _ACTION_CANONICAL.get(m.group().casefold())
        if canon:
            out.append((m.start(), m.end(), m.group(), canon))
    return out
