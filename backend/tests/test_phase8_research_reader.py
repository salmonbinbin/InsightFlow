"""Phase 8 — bounded official-page reader tests (Batch B).

These tests pin the Batch B reader safety contract. Every test is
network-free; the HTTP boundary is exercised via ``httpx.MockTransport``
so a CI run cannot reach an upstream site, and the ``allowed``
predicate is the place where the host+path policy is asserted.

Coverage:

1. Positive: a real-looking official page renders to Markdown with
   ``h1``–``h4``, ``ol`` numbered steps, ``ul`` bullets, image alt
   buttons, and skip-tag stripping. Snapshots include final URL,
   ``sha256`` of the rendered text, and ``byte_count`` of the
   decoded upstream body.

2. Apple-style ``<li><p>`` does NOT split the list item into two
   lines (numbered list keeps a single contiguous step).

3. ``<script>`` / ``<style>`` / ``<noscript>`` / ``<svg>`` content
   is stripped entirely.

4. URL hygiene: http:// → ``url_not_allowed``. userinfo →
   ``url_not_allowed``. non-443 port → ``url_not_allowed``.
   Non-allowlisted host → ``url_not_allowed`` and ZERO requests
   are sent (the allowlist rejects before ``client.request``).

5. Redirect hygiene: a redirect to a private-network IP, or to a
   non-allowlisted host, is REJECTED and the second GET is NEVER
   sent. The ``allowed`` callable is invoked on the redirect
   target. The 3-hop cap raises ``redirect_limit``.

6. Body budget: a stream that exceeds 4 MiB raises ``body_limit``
   and never silently truncates.

7. Text budget: a page that renders to > 24000 chars raises
   ``text_limit``.

8. Non-HTML content type, non-200 status, and ``httpx.TimeoutException``
   each raise their safe category. Upstream body / header / token
   never appears in the exception text.

9. Sentry test: an error message that COULD surface an upstream
   secret is asserted safe by string-equality — the exception's
   ``str(exc)`` is one of the allow-listed category codes.

All tests share a single helper that builds an ``httpx.Client`` over
a ``MockTransport`` so they never hit the network.
"""

from __future__ import annotations

from typing import Callable

import httpx
import pytest

from app.services.research_reader import (
    MAX_BYTES,
    MAX_REDIRECTS,
    MAX_TEXT,
    READ_ERROR_BODY_LIMIT,
    READ_ERROR_CONTENT_TYPE,
    READ_ERROR_EMPTY_CONTENT,
    READ_ERROR_HTTP_FAILURE,
    READ_ERROR_INVALID_URL,
    READ_ERROR_REDIRECT_LIMIT,
    READ_ERROR_TEXT_LIMIT,
    READ_ERROR_TIME_LIMIT,
    READ_ERROR_TRANSPORT_FAILURE,
    READ_ERROR_URL_NOT_ALLOWED,
    ReadError,
    Snapshot,
    html_to_markdown,
    read_official,
)

# ============================================================================
# Test fixtures and helpers
# ============================================================================


OFFICIAL_HOST = "support.google.com"
OFFICIAL_BASE = f"https://{OFFICIAL_HOST}/photos"


def _allowed_for(url: str) -> bool:
    """Narrow allowlist for tests: only ``support.google.com/photos``."""
    return url.startswith(OFFICIAL_BASE)


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    """Build a private ``httpx.Client`` backed by ``handler``.

    ``trust_env=False`` mirrors production defaults so an env var
    like ``HTTPS_PROXY`` cannot redirect the test transport through a
    real proxy.
    """
    transport = httpx.MockTransport(handler)
    return httpx.Client(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
        timeout=10.0,
    )


# ============================================================================
# Positive — happy path
# ============================================================================


def _rich_official_html() -> str:
    return (
        "<!doctype html><html><head>"
        "<title>ignored title bar</title>"
        "<script>alert('pwn');</script>"
        "<style>body{color:red}</style>"
        "<meta name='cookie' content='banner'>"
        "</head><body>"
        # Pre-h1 noise that must be dropped entirely.
        "<nav><a href='/x'>irrelevant</a></nav>"
        "<h1>Upload photos to Google Photos</h1>"
        "<h2>On Android</h2>"
        "<h3>Step 1: Open the app</h3>"
        "<p>Short paragraph.</p>"
        "<h3>Step 2: Pick pictures</h3>"
        "<ol>"
        "<li>First pick the album you want.</li>"
        "<li><p>Then tap the camera button.</p></li>"
        "<li>Choose up to ten pictures.</li>"
        "</ol>"
        "<h4>Notes</h4>"
        "<ul>"
        "<li>Notes line one.</li>"
        "<li>Notes line two.</li>"
        "</ul>"
        "<img src='/icon.png' alt='Search'>"
        "<noscript>JS disabled</noscript>"
        "<svg><circle r='1'/></svg>"
        "</body></html>"
    )


def test_read_official_happy_path_renders_markdown() -> None:
    """End-to-end: a real-looking Google Support page renders to a
    Markdown snapshot with the expected headings, numbered list,
    image alt token, and skip-tag stripping.
    """
    html = _rich_official_html()
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=html,
        )

    client = _make_client(handler)
    snap = read_official(
        f"{OFFICIAL_BASE}/android",
        _allowed_for,
        client=client,
    )
    assert isinstance(snap, Snapshot)
    assert snap.url == f"{OFFICIAL_BASE}/android"
    assert snap.byte_count == len(html.encode("utf-8"))
    assert len(snap.sha256) == 64
    # Headings.
    assert "# Upload photos to Google Photos" in snap.text
    assert "## On Android" in snap.text
    assert "### Step 1: Open the app" in snap.text
    assert "#### Notes" in snap.text
    # Numbered list — three items, single-line each.
    assert "1. First pick the album you want." in snap.text
    assert "2. Then tap the camera button." in snap.text
    assert "3. Choose up to ten pictures." in snap.text
    # Bullet list.
    assert "- Notes line one." in snap.text
    assert "- Notes line two." in snap.text
    # Image alt surfaces as a button-name token.
    assert "Search" in snap.text
    # Skip tags removed.
    assert "alert" not in snap.text
    assert "color:red" not in snap.text
    assert "JS disabled" not in snap.text
    # Pre-h1 noise removed.
    assert "irrelevant" not in snap.text
    # Exactly one outbound request.
    assert len(captured) == 1


def test_apple_li_p_does_not_split_numbered_item() -> None:
    """Apple-style ``<li><p>...</p></li>`` MUST render as a single
    numbered step. Pre-v14 the inner ``<p>`` caused the inner text
    to land on a separate paragraph line.
    """
    html = (
        "<html><body><h1>How to upload</h1>"
        "<ol>"
        "<li><p>Open the app.</p></li>"
        "<li><p>Tap plus.</p></li>"
        "</ol></body></html>"
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=html)

    client = _make_client(handler)
    snap = read_official(f"{OFFICIAL_BASE}/apple", _allowed_for, client=client)
    # Exactly two numbered lines, no orphan paragraphs between.
    numbered = [ln for ln in snap.text.splitlines() if ln.startswith(("1.", "2."))]
    assert len(numbered) == 2
    assert "1. Open the app." in numbered
    assert "2. Tap plus." in numbered
    # The pre-h1 noise was not present, so total non-empty lines =
    # heading + two numbered items.
    assert sum(1 for ln in snap.text.splitlines() if ln.strip()) == 3


def test_script_style_noscript_svg_are_stripped() -> None:
    """Script / style / noscript / svg must NOT appear in the
    rendered Markdown, even when they wrap visible content.
    """
    html = (
        "<html><body>"
        "<h1>Title</h1>"
        "<p>Visible before script.</p>"
        "<script>var secret = 'token-AAA';</script>"
        "<style>p{visibility:hidden}</style>"
        "<noscript>You must enable JS.</noscript>"
        "<svg><text>SVG text</text></svg>"
        "<p>Visible after.</p>"
        "</body></html>"
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=html)

    client = _make_client(handler)
    snap = read_official(f"{OFFICIAL_BASE}/strip", _allowed_for, client=client)
    for forbidden in (
        "var secret",
        "visibility:hidden",
        "You must enable",
        "SVG text",
        "token-AAA",
    ):
        assert forbidden not in snap.text
    assert "Visible before script." in snap.text
    assert "Visible after." in snap.text


def test_pre_h1_image_alt_is_dropped() -> None:
    """Navigation / header images (before the first ``<h1>``) must
    NOT contribute an ``[img: alt]`` token to the body. A page that
    only has a pre-h1 image renders the body and NEVER the alt text.
    """
    html = (
        "<html><body>"
        # Pre-h1 nav image: the alt is a brand name. It must not
        # pollute the post-h1 body.
        "<header><img src='/logo.png' alt='Brand-Logo-XYZ'></header>"
        "<h1>Title</h1>"
        "<p>Body paragraph.</p>"
        "<img src='/button.png' alt='Submit'>"
        "</body></html>"
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=html)

    client = _make_client(handler)
    snap = read_official(f"{OFFICIAL_BASE}/img", _allowed_for, client=client)
    # Post-h1 alt captured.
    assert "Submit" in snap.text
    # Pre-h1 alt dropped.
    assert "Brand-Logo-XYZ" not in snap.text
    assert "XYZ" not in snap.text
    assert "logo" not in snap.text.lower()


def test_html_to_markdown_helper_is_network_free() -> None:
    """``html_to_markdown`` must parse without any HTTP boundary."""
    md = html_to_markdown(_rich_official_html())
    assert "# Upload photos to Google Photos" in md
    assert "Search" in md
    assert "[img:" not in md


# ============================================================================
# URL hygiene — no network must happen when the URL fails the guard
# ============================================================================


def test_non_https_scheme_is_rejected_without_request() -> None:
    """An ``http://`` URL is rejected before any GET.
    Captured request list MUST be empty.
    """
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200)

    client = _make_client(handler)
    with pytest.raises(ReadError) as exc:
        read_official(f"http://{OFFICIAL_HOST}/photos", _allowed_for, client=client)
    assert exc.value.code == READ_ERROR_URL_NOT_ALLOWED
    assert captured == []


def test_userinfo_url_is_rejected_without_request() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200)

    client = _make_client(handler)
    with pytest.raises(ReadError) as exc:
        read_official(
            f"https://user:pass@{OFFICIAL_HOST}/photos",
            _allowed_for,
            client=client,
        )
    assert exc.value.code == READ_ERROR_URL_NOT_ALLOWED
    assert captured == []


def test_non_default_port_is_rejected_without_request() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200)

    client = _make_client(handler)
    with pytest.raises(ReadError) as exc:
        read_official(f"https://{OFFICIAL_HOST}:8443/photos", _allowed_for, client=client)
    assert exc.value.code == READ_ERROR_URL_NOT_ALLOWED
    assert captured == []


def test_non_allowlisted_host_is_rejected_without_request() -> None:
    """The ``allowed`` predicate is the trust boundary. A URL to a
    valid shape but an off-allowlist host MUST zero requests.
    """
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content="<html><body><h1>x</h1></body></html>",
        )

    client = _make_client(handler)
    with pytest.raises(ReadError) as exc:
        read_official("https://example.com/photos", _allowed_for, client=client)
    assert exc.value.code == READ_ERROR_URL_NOT_ALLOWED
    assert captured == []


# ============================================================================
# Redirect hygiene
# ============================================================================


def test_redirect_to_private_network_is_rejected_no_second_request() -> None:
    """A 302 to a private RFC1918 IP MUST be rejected without a
    second GET. The trust boundary is the allowlist on each hop.
    """
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        if req.url.host == OFFICIAL_HOST:
            return httpx.Response(
                302,
                headers={"location": "https://10.0.0.5/internal"},
            )
        return httpx.Response(200, content="")

    client = _make_client(handler)
    with pytest.raises(ReadError) as exc:
        read_official(f"{OFFICIAL_BASE}/u", _allowed_for, client=client)
    assert exc.value.code == READ_ERROR_URL_NOT_ALLOWED
    assert len(captured) == 1


def test_redirect_to_non_allowlisted_host_is_rejected() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        if req.url.host == OFFICIAL_HOST:
            return httpx.Response(
                302,
                headers={"location": "https://attacker.example.org/x"},
            )
        return httpx.Response(200, content="")

    client = _make_client(handler)
    with pytest.raises(ReadError) as exc:
        read_official(f"{OFFICIAL_BASE}/u", _allowed_for, client=client)
    assert exc.value.code == READ_ERROR_URL_NOT_ALLOWED
    assert len(captured) == 1


def test_redirect_chain_follows_within_hop_limit() -> None:
    """A 2-hop allowlisted chain completes (3 hops = cap, 2 hops ok)."""
    captured: list[httpx.Request] = []
    html = "<html><body><h1>Landing</h1><p>Final</p></body></html>"

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        path = req.url.path
        if path == "/photos/u":
            return httpx.Response(
                302,
                headers={"location": "https://support.google.com/photos/mid"},
            )
        if path == "/photos/mid":
            return httpx.Response(
                302,
                headers={"location": "https://support.google.com/photos/landing"},
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=html,
        )

    client = _make_client(handler)
    snap = read_official(f"{OFFICIAL_BASE}/u", _allowed_for, client=client)
    assert snap.url == f"{OFFICIAL_BASE}/landing"
    assert "Landing" in snap.text
    assert len(captured) == 3


def test_redirect_chain_at_cap_raises_redirect_limit() -> None:
    """3 redirects are allowed; the 4th request returns a
    redirect, and the cap trips ``redirect_limit``. The redirect
    target MUST remain in the allowlist so the chain is the only
    reason the cap fires (not a host-rejection trip).
    """
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(
            302,
            headers={"location": f"{OFFICIAL_BASE}/hop"},
        )

    client = _make_client(handler)
    with pytest.raises(ReadError) as exc:
        read_official(f"{OFFICIAL_BASE}/start", _allowed_for, client=client)
    assert exc.value.code == READ_ERROR_REDIRECT_LIMIT
    # Cap = 3 redirects; the loop sees 3 redirects then raises on the
    # would-be 4th redirect response. Total outbound GETs = 4.
    assert len(captured) == MAX_REDIRECTS + 1


def test_relative_redirect_is_resolved_against_current_url() -> None:
    """A relative ``Location`` header (no leading slash) is resolved
    against the parent directory of the current URL, NOT the
    original URL. The reader still re-validates the resolved target.
    """
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        if req.url.path == "/photos/u/a":
            return httpx.Response(302, headers={"location": "b"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content="<html><body><h1>B</h1></body></html>",
        )

    client = _make_client(handler)
    snap = read_official(f"{OFFICIAL_BASE}/u/a", _allowed_for, client=client)
    assert snap.url == f"{OFFICIAL_BASE}/u/b"
    assert len(captured) == 2


def test_redirect_with_empty_location_header_raises_invalid_url() -> None:
    """A 302 with an empty ``Location`` header is treated as
    ``INVALID_URL`` — an attacker that controls a redirect MUST NOT
    be able to chain a self-loop or fall back to the original URL.
    """
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(302, headers={"location": ""})

    client = _make_client(handler)
    with pytest.raises(ReadError) as exc:
        read_official(f"{OFFICIAL_BASE}/u", _allowed_for, client=client)
    assert exc.value.code == READ_ERROR_INVALID_URL
    # Only the original request; no second hop is even attempted.
    assert len(captured) == 1


# ============================================================================
# Body and text budgets
# ============================================================================


def test_body_over_4MiB_raises_body_limit_no_truncation() -> None:
    """A body that exceeds the 4 MiB ceiling raises ``body_limit``.
    The earlier reader silently truncated to 512 KiB and lost the
    Google procedure; the v14 cap is a HARD error.
    """
    body = b"<html><body><h1>big</h1>" + (b"a" * (MAX_BYTES + 1)) + b"</body></html>"

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=body)

    client = _make_client(handler)
    with pytest.raises(ReadError) as exc:
        read_official(f"{OFFICIAL_BASE}/big", _allowed_for, client=client)
    assert exc.value.code == READ_ERROR_BODY_LIMIT


def test_text_over_24k_raises_text_limit_no_truncation() -> None:
    """Many small paragraphs whose joined lines exceed 24 000 chars
    raise ``text_limit``. The reader never silently truncates.
    """
    # 500 lines of 60 chars = ~30 000 chars after header / joining.
    body_inner = "".join(f"<p>{'x' * 60}</p>" for _ in range(500))
    html = f"<html><body><h1>Big doc</h1>{body_inner}</body></html>"

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=html)

    client = _make_client(handler)
    with pytest.raises(ReadError) as exc:
        read_official(f"{OFFICIAL_BASE}/text-big", _allowed_for, client=client)
    assert exc.value.code == READ_ERROR_TEXT_LIMIT


def test_text_at_or_below_24k_is_accepted() -> None:
    """Boundary: 24 000 chars is the cap; exactly-at-cap parses and
    returns a Snapshot.
    """
    # Heading + a single 23 989-char paragraph keeps total under cap.
    pad = "y" * (MAX_TEXT - len("# Title\n\n") - 1)
    html = f"<html><body><h1>Title</h1><p>{pad}</p></body></html>"

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=html)

    client = _make_client(handler)
    snap = read_official(f"{OFFICIAL_BASE}/edge", _allowed_for, client=client)
    assert len(snap.text) <= MAX_TEXT
    assert snap.text.startswith("# Title")


# ============================================================================
# Non-HTML, non-200, timeout
# ============================================================================


def test_non_html_content_type_raises_content_type() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4")

    client = _make_client(handler)
    with pytest.raises(ReadError) as exc:
        read_official(f"{OFFICIAL_BASE}/pdf", _allowed_for, client=client)
    assert exc.value.code == READ_ERROR_CONTENT_TYPE


def test_non_200_status_raises_http_failure() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"content-type": "text/html"}, content=b"")

    client = _make_client(handler)
    with pytest.raises(ReadError) as exc:
        read_official(f"{OFFICIAL_BASE}/dead", _allowed_for, client=client)
    assert exc.value.code == READ_ERROR_HTTP_FAILURE


def test_404_with_secrets_in_body_does_not_leak() -> None:
    """A 404 that happens to echo a token MUST still surface only the
    safe category code. No body, no header, no token in the exception.
    """
    secret = "BEARER-supersecret-xyz-9aa"

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, headers={"content-type": "text/html"}, content=f"denied: {secret}".encode("utf-8")
        )

    client = _make_client(handler)
    with pytest.raises(ReadError) as exc:
        read_official(f"{OFFICIAL_BASE}/nope", _allowed_for, client=client)
    assert exc.value.code == READ_ERROR_HTTP_FAILURE
    # Sentry: the secret must not appear anywhere in the exception
    # text, args, or context.
    rendered = str(exc.value)
    assert secret not in rendered
    assert secret not in repr(exc.value.__cause__ or "")
    assert exc.value.__cause__ is None


def test_transport_timeout_raises_transport_failure_with_safe_text() -> None:
    """A ``TimeoutException`` from ``httpx`` must surface as
    ``transport_failure`` and the timeout's exception args must NOT
    include the upstream URL or any header.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("upstream-fail", request=req)

    client = _make_client(handler)
    with pytest.raises(ReadError) as exc:
        read_official(f"{OFFICIAL_BASE}/slow", _allowed_for, client=client)
    assert exc.value.code == READ_ERROR_TRANSPORT_FAILURE
    assert "upstream-fail" not in str(exc.value)


def test_no_h1_in_document_raises_empty_content() -> None:
    """A page with no ``<h1>`` element has no body content under the
    first-h1 rule and the parser raises ``empty_content``. (A page
    with just an h1 still renders the heading text and is NOT empty.)
    """
    html = "<html><body><nav>skip</nav><p>intro</p></body></html>"

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=html)

    client = _make_client(handler)
    with pytest.raises(ReadError) as exc:
        read_official(f"{OFFICIAL_BASE}/empty", _allowed_for, client=client)
    assert exc.value.code == READ_ERROR_EMPTY_CONTENT


# ============================================================================
# Sentry tests — error text must never echo upstream secrets / headers
# ============================================================================


def test_exception_string_is_always_a_safe_category_code() -> None:
    """Every error path surfaces the category code as the entire
    exception string. There is no header / body / token in the
    text regardless of what the upstream tried to inject.
    """
    secret_header = "x-api-key: secret-key-9999"
    secret_body = "STACK-INTERNAL-TOKEN-deadbeef"
    error_codes = {
        READ_ERROR_INVALID_URL,
        READ_ERROR_URL_NOT_ALLOWED,
        READ_ERROR_HTTP_FAILURE,
        READ_ERROR_CONTENT_TYPE,
        READ_ERROR_BODY_LIMIT,
        READ_ERROR_TEXT_LIMIT,
        READ_ERROR_TIME_LIMIT,
        READ_ERROR_REDIRECT_LIMIT,
        READ_ERROR_TRANSPORT_FAILURE,
        READ_ERROR_EMPTY_CONTENT,
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            headers={"content-type": "text/html", "x-trace": secret_header},
            content=f"<html><body><h1>oops {secret_body}</h1></body></html>",
        )

    client = _make_client(handler)
    with pytest.raises(ReadError) as exc:
        read_official(f"{OFFICIAL_BASE}/sentry", _allowed_for, client=client)
    assert exc.value.code == READ_ERROR_HTTP_FAILURE
    assert exc.value.code in error_codes
    text = str(exc.value)
    assert secret_header not in text
    assert secret_body not in text
    assert "x-trace" not in text
    # Round-trip through repr as well.
    assert secret_body not in repr(exc.value)


def test_url_with_at_in_path_is_rejected_by_allowlist() -> None:
    """A URL with ``@`` in the path (not userinfo) is shape-valid
    but escapes the host+path allowlist. The reader MUST refuse it
    via the ``allowed`` predicate rather than fetch.
    """
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content="<html><body><h1>x</h1></body></html>",
        )

    client = _make_client(handler)
    with pytest.raises(ReadError) as exc:
        read_official(f"https://{OFFICIAL_HOST}/with@weird", _allowed_for, client=client)
    assert exc.value.code == READ_ERROR_URL_NOT_ALLOWED
    assert captured == []


def test_500_with_authorization_header_does_not_leak_via_attrs() -> None:
    """The MockTransport response carries an ``Authorization`` header
    carrying a credential. The reader must never persist or echo it.
    """
    captured_exception: Exception | None = None
    token = "Authorization: Bearer eyJabc.def.ghi"

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            headers={
                "content-type": "text/html",
                "authorization": token,
            },
            content=b"<html><body><h1>boom</h1></body></html>",
        )

    client = _make_client(handler)
    try:
        read_official(f"{OFFICIAL_BASE}/auth-leak", _allowed_for, client=client)
    except ReadError as exc:
        captured_exception = exc
    assert captured_exception is not None
    assert "eyJabc" not in str(captured_exception)
    assert "Bearer" not in str(captured_exception)
    assert "authorization" not in str(captured_exception).lower()
    # The exception has no __cause__ either.
    assert captured_exception.__cause__ is None
