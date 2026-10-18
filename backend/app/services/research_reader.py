"""Phase 8 — bounded official-page reader.

The reader is a deliberately small, conservative HTML fetcher that
supplies a single official web page as a Markdown snapshot for the
Research Agent's catalog. It is the ONLY network boundary in the
Batch B (reader) surface; every other Research Agent module operates
on persisted rows.

The reader is untrusted-input aware:

* The URL the caller passes in is NEVER fetched as-is. Each step
  splits the URL into scheme / host / port / path and re-checks
  against the caller-supplied ``allowed(url)`` predicate BEFORE the
  GET goes out. The host ``+`` path allowlist is the safety boundary;
  a Research Service caller that takes URLs from an LLM response
  composes ``allowed`` from its own host+path catalogue and refuses
  anything outside that catalogue. The reader never sees the LLM
  output directly.

* HTTPS only, no userinfo, default port 443 — non-HTTPS, userinfo-
  carrying, or non-443-port URLs are rejected before any
  ``socket.connect``.

* ``client`` is an injected ``httpx.Client`` so the caller (and
  tests) own the transport. Default is a private ``httpx.Client``
  that this function closes itself; callers that want a shared
  client can inject theirs. The reader never logs the transport
  and never echoes upstream headers / body / tokens.

* Redirects: at most :data:`MAX_REDIRECTS` hops, and each redirect
  target is re-validated against ``allowed`` and the scheme / port
  rules above. A redirect that escapes the allowlist is rejected
  rather than followed.

* Body budget: at most :data:`MAX_BYTES` decoded bytes; when the
  stream passes the limit the read aborts with
  ``READ_ERROR_BODY_LIMIT`` — there is no silent truncation.

* Text budget: the parser emits at most :data:`MAX_TEXT` characters;
  when the conversion would exceed the budget the call aborts with
  ``READ_ERROR_TEXT_LIMIT`` — there is no silent truncation. The
  upstream Google Support HTML is ~1.66 MiB; the prior 512 KiB text
  budget silently dropped the procedure steps. 24 000 chars is the
  v14 brief value that captures the procedure with slack.

* Wall clock: every redirect + the final GET share a 10-second
  ``time.monotonic()`` budget. A per-request ``timeout`` of 10 s
  is also set so a single hung socket cannot block past the
  budget.

* Content-Type: ``text/html`` only — anything else is rejected.

* Markdown conversion: an :class:`HTMLParser` driven from the
  first ``<h1>``. ``<script>`` / ``<style>`` / ``<noscript>`` /
  ``<svg>`` are stripped. ``<h1>``–``<h4>`` become ``#``–``####``
  prefix lines. ``<ol>`` / ``<ul>`` produce numbered / dashed
  list lines; Apple-style ``<li><p>`` does NOT split the list
  item because ``<p>`` inside a list is treated as a soft break.
  ``<img alt="...">`` emits the alt text as a button-name token
  (the original alt text) so an image-only submit button is preserved.

* Errors: a fixed, allow-listed category code (``str(exc)`` is the
  literal code). Upstream bodies, response headers, and tokens
  NEVER appear in the exception text or in ``__cause__``.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

# ---- Limits (named so tests / callers can import them) ------------------

# Decoded body budget. 4 MiB exceeds Google's Support HTML (~1.66 MiB);
# anything larger is rejected so a runaway upstream cannot OOM the
# reader. Applied to the byte stream BEFORE the HTML is decoded.
MAX_BYTES = 4 * 1024 * 1024

# Markdown text budget. The parser aborts rather than truncating; the
# v14 brief set this to 24 000 chars to capture the full Google upload
# procedure with slack while bounding downstream LLM prompts.
MAX_TEXT = 24000

# Redirect cap. Up to three hops, after which the next hop is the
# limit. A redirect that escapes the allowlist is rejected BEFORE its
# hop is consumed so an attacker cannot chain redirects against an
# off-allowlist target.
MAX_REDIRECTS = 3

# Per-request socket timeout. The total call also carries a
# ``time.monotonic()`` ceiling of the same value.
REQUEST_TIMEOUT_SECONDS = 10.0
TOTAL_TIMEOUT_SECONDS = 10.0

# URL length bound — matches the DB CHECK constraint on
# ``research_sources.url`` and is enforced here so the row never
# overflows the column even on adversarial input.
MAX_URL_LENGTH = 2048


# ---- Error codes --------------------------------------------------------

# Fixed, allow-listed categories. The string is the entire exception
# repr — no upstream body, header, or token is allowed in or near it.
READ_ERROR_INVALID_URL = "invalid_url"
READ_ERROR_URL_NOT_ALLOWED = "url_not_allowed"
READ_ERROR_HTTP_FAILURE = "http_failure"
READ_ERROR_CONTENT_TYPE = "content_type"
READ_ERROR_BODY_LIMIT = "body_limit"
READ_ERROR_TEXT_LIMIT = "text_limit"
READ_ERROR_TIME_LIMIT = "time_limit"
READ_ERROR_REDIRECT_LIMIT = "redirect_limit"
READ_ERROR_TRANSPORT_FAILURE = "transport_failure"
READ_ERROR_EMPTY_CONTENT = "empty_content"


class ReadError(ValueError):
    """Bounded-reader failure with a fixed safe category code.

    The string form of the exception IS the category code; downstream
    callers MUST treat the string as a safe, allow-listed literal and
    MUST NOT include it (or any ``__cause__`` / ``__context__`` chain)
    in user-visible error messages. Upstream bodies, response headers,
    and tokens NEVER appear in the exception text.
    """

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not code:
            code = READ_ERROR_TRANSPORT_FAILURE
        # Strip any formatting that could let a code leak detail.
        super().__init__(code)
        self.code = code


# ---- Public result type -------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """A successfully-read official page rendered as Markdown.

    ``url`` is the FINAL URL after redirects (every redirect target
    was re-validated against ``allowed``). ``text`` is the parsed
    Markdown; ``sha256`` fingerprints ``text``; ``byte_count`` is the
    decoded byte count of the upstream response body (NOT the
    Markdown length). The reader does NOT write to any table — the
    caller (the Research Agent orchestrator) is responsible for
    persisting the snapshot alongside its source row.
    """

    url: str
    text: str
    sha256: str
    byte_count: int


# ---- HTML → Markdown ----------------------------------------------------


_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg"})


class _OfficialMarkdown(HTMLParser):
    """Streaming HTML → Markdown converter.

    The conversion starts at the FIRST ``<h1>``; anything before it
    (cookie banner, nav, breadcrumbs) is dropped. ``<h1>``–``<h4>``
    produce ``#``–``####`` prefix lines. ``<ol>`` / ``<ul>`` produce
    numbered / dashed list lines; ``<li>`` does NOT split numbering
    when it contains an inner ``<p>`` (Apple Support HTML uses
    ``<li><p>...</p></li>``).

    ``<img alt="...">`` emits a the original alt text token so an image-only
    submit button (e.g. Google "Search") still surfaces in the
    catalog. ``<script>`` / ``<style>`` / ``<noscript>`` / ``<svg>``
    are stripped wholesale.

    The parser holds the per-chunk buffer and a soft line list. The
    ``text_limit`` argument caps the rendered line count; when the
    cap is reached ``finish()`` raises :class:`ReadError`.
    """

    def __init__(self, text_limit: int = MAX_TEXT) -> None:
        super().__init__(convert_charrefs=True)
        self._text_limit = text_limit
        self._skip_depth = 0
        self._started = False
        self._lines: list[str] = []
        self._buffer: list[str] = []
        self._list_stack: list[tuple[str, int]] = []
        # In a list; suppresses the ``<p>`` / ``<div>`` flush so an
        # Apple ``<li><p>...</p></li>`` does not split numbering.
        self._in_list = False

    # --- helpers ---

    def _flush(self) -> None:
        if not self._buffer:
            return
        joined = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
        self._buffer = []
        if joined:
            self._lines.append(joined)
            self._raise_if_over_limit()

    def _raise_if_over_limit(self) -> None:
        rendered = sum(len(line) + 1 for line in self._lines)
        if rendered > self._text_limit:
            raise ReadError(READ_ERROR_TEXT_LIMIT)

    def _open_list(self, kind: str) -> None:
        self._flush()
        # Children of any list count; we only differentiate for the
        # bullet character (``ol`` → numbered, ``ul`` → ``-``).
        self._list_stack.append((kind, 0))

    def _close_list(self, kind: str) -> None:
        self._flush()
        for idx in range(len(self._list_stack) - 1, -1, -1):
            if self._list_stack[idx][0] == kind:
                del self._list_stack[idx:]
                break

    # --- parser callbacks ---

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "h1":
            self._started = True
        if not self._started:
            # Pre-h1 (nav / header) ``<img alt=...>`` is intentionally
            # dropped; navigation logos / hero images are not the
            # button-name evidence the contract cares about. The
            # post-h1 body is the only place an alt becomes a
            # catalog-visible button-name token.
            return
        # Image button-name capture. Rendered as a token so a parser
        # that selects evidence can still see the alt text.
        if tag == "img":
            alt = ""
            for k, v in attrs:
                if k.lower() == "alt" and v:
                    alt = v
                    break
            if alt:
                self._buffer.append(f" {alt} ")
            return
        if tag in ("h1", "h2", "h3", "h4"):
            self._flush()
            self._buffer.append("#" * int(tag[1]) + " ")
        elif tag == "ol":
            self._open_list("ol")
            self._in_list = True
        elif tag == "ul":
            self._open_list("ul")
            self._in_list = True
        elif tag == "li":
            self._flush()
            if self._list_stack:
                kind, count = self._list_stack[-1]
                count += 1
                self._list_stack[-1] = (kind, count)
                bullet = f"{count}. " if kind == "ol" else "- "
                self._buffer.append(bullet)
            self._in_list = True
        elif tag in ("p", "div", "br") and not self._in_list:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth or not self._started:
            return
        if tag in ("h1", "h2", "h3", "h4", "li"):
            self._flush()
        if tag == "ol":
            self._close_list("ol")
        elif tag == "ul":
            self._close_list("ul")
        if tag in ("p", "div") and not self._in_list:
            self._flush()
        # Recompute in-list state: if any list remains open we are
        # still inside one.
        self._in_list = bool(self._list_stack)

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not self._started:
            return
        self._buffer.append(data)

    # --- result ---

    def finish(self) -> str:
        self._flush()
        text = "\n".join(self._lines)
        if not text:
            raise ReadError(READ_ERROR_EMPTY_CONTENT)
        return text


def html_to_markdown(html: str) -> str:
    """Public, network-free helper for tests and debugging.

    Parses ``html`` with the same rules as :func:`read_official`. Any
    ``ReadError`` raised here propagates unchanged. Useful for the
    test suite so a fixture HTML can be rendered without touching
    ``httpx``.
    """
    if not isinstance(html, str):
        raise ReadError(READ_ERROR_INVALID_URL)
    parser = _OfficialMarkdown()
    parser.feed(html)
    return parser.finish()


# ---- URL guard ----------------------------------------------------------


def _check_url(url: str, allowed: Callable[[str], bool]) -> None:
    """Reject ``url`` if it is not https://...:443/non-port with no
    userinfo, and not approved by the caller's ``allowed`` predicate.
    """
    if not isinstance(url, str) or not url:
        raise ReadError(READ_ERROR_INVALID_URL)
    if len(url) > MAX_URL_LENGTH:
        raise ReadError(READ_ERROR_INVALID_URL)
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise ReadError(READ_ERROR_URL_NOT_ALLOWED)
    if parts.username or parts.password:
        raise ReadError(READ_ERROR_URL_NOT_ALLOWED)
    try:
        port = parts.port
    except ValueError:
        raise ReadError(READ_ERROR_INVALID_URL) from None
    if port is not None and port != 443:
        raise ReadError(READ_ERROR_URL_NOT_ALLOWED)
    if not parts.netloc:
        raise ReadError(READ_ERROR_INVALID_URL)
    try:
        ok = bool(allowed(url))
    except Exception:
        # The predicate must be total; an exception from it counts as
        # denial rather than a transport failure.
        ok = False
    if not ok:
        raise ReadError(READ_ERROR_URL_NOT_ALLOWED)


# ---- Public entrypoint --------------------------------------------------


def read_official(
    url: str,
    allowed: Callable[[str], bool],
    *,
    client: httpx.Client | None = None,
) -> Snapshot:
    """Read a single official page as a Markdown :class:`Snapshot`.

    ``allowed`` is the caller's host+path allowlist. It is invoked on
    the original URL AND on every redirect target BEFORE the next
    GET goes out. ``allowed`` MUST be total and cheap (it is called in
    the hot path); exceptions are treated as denial.

    ``client`` is the injected ``httpx.Client``. When ``None`` a
    private client with ``timeout=REQUEST_TIMEOUT_SECONDS`` and
    ``follow_redirects=False`` is built and closed by this function.
    The private client respects the existing process network configuration.
    Every origin and redirect URL still passes the supplied official allowlist.

    The caller-supplied client is left open so it can be reused
    across calls (the Research Agent does batch fetches).
    """
    if not callable(allowed):
        # A None / non-callable allowlist would silently let every URL
        # through. Treat as a programming error.
        raise ReadError(READ_ERROR_URL_NOT_ALLOWED)
    owned = client is None
    # Use the user's existing network configuration, like the search client.
    transport_client = client or httpx.Client(
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=True,
    )
    start = time.monotonic()
    try:
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            elapsed = time.monotonic() - start
            if elapsed > TOTAL_TIMEOUT_SECONDS:
                raise ReadError(READ_ERROR_TIME_LIMIT)
            _check_url(current, allowed)
            # The per-request socket timeout is the smaller of
            # ``REQUEST_TIMEOUT_SECONDS`` and whatever remains of the
            # total wall-clock budget. A redirected chain cannot
            # silently extend the per-hop timeout past the total cap.
            per_request_timeout = min(
                REQUEST_TIMEOUT_SECONDS,
                max(0.0, TOTAL_TIMEOUT_SECONDS - elapsed),
            )
            try:
                with transport_client.stream(
                    "GET",
                    current,
                    follow_redirects=False,
                    timeout=per_request_timeout,
                ) as response:
                    status = response.status_code
                    if status in (301, 302, 303, 307, 308):
                        if hop >= MAX_REDIRECTS:
                            raise ReadError(READ_ERROR_REDIRECT_LIMIT)
                        location = response.headers.get("location") or ""
                        # ``urljoin`` against an empty / relative
                        # location is a programming-style error, but
                        # treat it as INVALID_URL rather than
                        # silently letting the empty string fall
                        # through to ``_check_url``.
                        if not location:
                            raise ReadError(READ_ERROR_INVALID_URL)
                        current = urljoin(current, location)
                        _check_url(current, allowed)
                        continue
                    if status != 200:
                        raise ReadError(READ_ERROR_HTTP_FAILURE)
                    content_type = (response.headers.get("content-type") or "").lower()
                    if "text/html" not in content_type:
                        raise ReadError(READ_ERROR_CONTENT_TYPE)
                    # Stream-decoded body. The 4 MiB and 10 s budgets
                    # are checked per chunk so a slow server that
                    # dribbles bytes past the cap aborts immediately
                    # rather than buffering the whole page. The
                    # ``with`` block guarantees the underlying
                    # response is closed on every exit path — normal
                    # return, exception, and budget trip.
                    body_buf = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() - start > TOTAL_TIMEOUT_SECONDS:
                            raise ReadError(READ_ERROR_TIME_LIMIT)
                        body_buf.extend(chunk)
                        if len(body_buf) > MAX_BYTES:
                            raise ReadError(READ_ERROR_BODY_LIMIT)
                    body = bytes(body_buf)
            except httpx.HTTPError:
                # Boundary: never propagate httpx's exception text —
                # it can include the URL or partial response data.
                raise ReadError(READ_ERROR_TRANSPORT_FAILURE) from None
            parser = _OfficialMarkdown()
            try:
                parser.feed(body.decode("utf-8", errors="replace"))
            except ReadError:
                raise
            except Exception:
                # The parser must never echo HTML — turn any internal
                # crash into a safe category and let the read fail.
                raise ReadError(READ_ERROR_TRANSPORT_FAILURE) from None
            text = parser.finish()
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            return Snapshot(
                url=current,
                text=text,
                sha256=digest,
                byte_count=len(body),
            )
        # Loop exhausted without a final response: redirect cap hit.
        raise ReadError(READ_ERROR_REDIRECT_LIMIT)
    finally:
        if owned:
            try:
                transport_client.close()
            except Exception:
                pass


__all__ = [
    "MAX_BYTES",
    "MAX_TEXT",
    "MAX_REDIRECTS",
    "REQUEST_TIMEOUT_SECONDS",
    "TOTAL_TIMEOUT_SECONDS",
    "MAX_URL_LENGTH",
    "READ_ERROR_INVALID_URL",
    "READ_ERROR_URL_NOT_ALLOWED",
    "READ_ERROR_HTTP_FAILURE",
    "READ_ERROR_CONTENT_TYPE",
    "READ_ERROR_BODY_LIMIT",
    "READ_ERROR_TEXT_LIMIT",
    "READ_ERROR_TIME_LIMIT",
    "READ_ERROR_REDIRECT_LIMIT",
    "READ_ERROR_TRANSPORT_FAILURE",
    "READ_ERROR_EMPTY_CONTENT",
    "ReadError",
    "Snapshot",
    "html_to_markdown",
    "read_official",
]
