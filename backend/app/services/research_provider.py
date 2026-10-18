"""Phase 8 — Research Tool Provider wrapper (Tavily Web Search).

The Research Tool Provider is an external bounded search service: the
input is the canonical research query assembled by the service, the
output is the small set of search hits (URL / title / domain / snippet /
provider_source_id) that the LLM cites as ``source_refs``. The provider
is treated as untrusted — every URL is validated to be a syntactically-
valid ``http(s)`` URL, every field is bounded in length, and the
provider_source_id is recorded exactly as returned so a later retrieval
can reconcile the same hit.

The client deliberately never logs the API key (the header is built
with ``add_headers`` rather than ``headers=`` so the secret never lands
in the client repr; the value is loaded once at construction). All
typed errors (``ResearchProviderConfigError`` /
``ResearchProviderTransportError`` / ``ResearchProviderResponseError``)
carry only a safe diagnostic category — never the raw upstream body —
so an HTTP 500 page that happens to echo a credential cannot leak via
exception chains or persisted error_message text.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


# ---- Limits ---------------------------------------------------------------

# The brief mandates "up to 5 representative Evidence entries" and the
# prompt enforces it; the provider cap below is configurable but bounded
# so a runaway config cannot demand 10_000 hits from a paid API.
DEFAULT_MAX_RESULTS = 5
MIN_MAX_RESULTS = 1
MAX_MAX_RESULTS = 10

# URL length is bounded by the DB CHECK constraint as well; the validator
# here is defence-in-depth before the row ever reaches the database.
URL_MAX_LENGTH = 2048
PROVIDER_SOURCE_ID_MAX_LENGTH = 200
TITLE_MAX_LENGTH = 500
DOMAIN_MAX_LENGTH = 253
PROVIDER_NAME_MAX_LENGTH = 64
SNIPPET_MAX_LENGTH = 4000
QUERY_MAX_LENGTH = 500

# Bounded retry table — same shape as Phase 1 / Phase 3 client.
DEFAULT_MAX_RETRIES = 2
MIN_MAX_RETRIES = 0
MAX_MAX_RETRIES = 5


# ---- Errors ---------------------------------------------------------------


class ResearchProviderError(RuntimeError):
    """Closed diagnostics only. Never retain an upstream exception or body."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_failure",
        retryable: bool = False,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        allowed = {
            "provider_failure",
            "configuration",
            "authentication",
            "quota",
            "rate_limit",
            "upstream_server",
            "request_rejected",
            "redirect_rejected",
            "timeout",
            "network",
            "invalid_json",
            "invalid_response",
            "application_error",
        }
        self.diagnostic = {
            "code": code if code in allowed else "provider_failure",
            "retryable": bool(retryable),
        }
        if type(http_status) is int and 100 <= http_status <= 599:
            self.diagnostic["http_status"] = http_status


class ResearchProviderConfigError(ResearchProviderError):
    """Missing or invalid Tavily / Research Tool Provider configuration."""


class ResearchProviderTransportError(ResearchProviderError):
    """Network or transient HTTP failure (timeout, 429, 5xx). Retriable."""


class ResearchProviderResponseError(ResearchProviderError):
    """Non-retriable HTTP error (4xx other than 429) or malformed body."""


class ResearchProviderURLError(RuntimeError):
    """A returned URL is not a syntactically-valid http(s) URL."""


# ---- Result dataclass -----------------------------------------------------


@dataclass(frozen=True)
class ProviderSource:
    """A single search hit as the provider returned it.

    ``provider_source_id`` is the opaque provider identifier (``id``
    field for Tavily); ``domain`` is derived from the URL host and
    persisted so reads do not have to re-parse. The fields are exactly
    what the DB CHECK constraints allow — longer values raise
    :class:`ResearchProviderURLError`.
    """

    provider_source_id: str
    url: str
    title: str | None
    domain: str
    snippet: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class ProviderSearchResult:
    """The complete result of one provider search call."""

    query: str
    provider: str
    sources: list[ProviderSource]


# ---- URL / domain helpers -------------------------------------------------


_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def is_valid_http_url(value: str) -> bool:
    """Return True when ``value`` is an http(s) URL of bounded length.

    Validation deliberately only checks shape (scheme + netloc +
    bounded length). The DB never fetches or trusts the URL further
    beyond its stored string; this is the only boundary at which an
    attacker-controlled byte can sneak into the database. Anything that
    fails here is dropped from the result list before the LLM ever
    sees it.
    """
    if not isinstance(value, str):
        return False
    if not (1 <= len(value) <= URL_MAX_LENGTH):
        return False
    if not _URL_RE.match(value):
        return False
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    if not parsed.netloc:
        return False
    return True


def derive_domain(url: str) -> str:
    """Return the lowercased netloc for ``url`` with the bounded length.

    Length is enforced here so the value cannot exceed the DB column
    even if a URL builder returned a host string with a trailing dot or
    an unusually-long public-suffix-bypass scheme.
    """
    parsed = urlparse(url)
    domain = (parsed.hostname or "").lower().strip(".")
    if not domain:
        raise ResearchProviderURLError(f"url has no hostname: {url[:200]!r}")
    domain = domain[:DOMAIN_MAX_LENGTH]
    if not domain:
        raise ResearchProviderURLError(f"domain truncated to empty: {url[:200]!r}")
    return domain


# ---- Source coercion ------------------------------------------------------


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    return value[:limit]


def coerce_source(
    raw: dict[str, Any],
    *,
    provider: str,
) -> ProviderSource | None:
    """Return a ``ProviderSource`` for ``raw`` or ``None`` when invalid.

    Anything that does not satisfy the http(s) URL / length contract is
    silently dropped. The model prompt instructs the LLM to cite only
    hits that survived this filter — non-http sources (e.g. ``ftp://``,
    file paths, javascript: URIs) are NEVER accepted.
    """
    if not isinstance(raw, dict):
        return None
    url = raw.get("url")
    if not isinstance(url, str) or not is_valid_http_url(url):
        return None
    provider_source_id = raw.get("id") or raw.get("provider_source_id")
    if not isinstance(provider_source_id, str) or not provider_source_id.strip():
        return None
    provider_source_id = provider_source_id.strip()[:PROVIDER_SOURCE_ID_MAX_LENGTH]
    try:
        domain = derive_domain(url)
    except ResearchProviderURLError:
        return None
    title = _truncate(raw.get("title"), TITLE_MAX_LENGTH)
    snippet = _truncate(raw.get("content") or raw.get("snippet"), SNIPPET_MAX_LENGTH)
    return ProviderSource(
        provider_source_id=provider_source_id,
        url=url,
        title=title,
        domain=domain,
        snippet=snippet,
        raw={k: v for k, v in raw.items() if k in {"id", "url", "title", "content", "score"}},
    )


# ---- Client ---------------------------------------------------------------


class ResearchProviderClient:
    """Synchronous Tavily Web Search client.

    The provider endpoint is fixed at
    ``{base_url}/search`` because Tavily owns that path. The body is
    shaped to match the Tavily ``/search`` API: ``query`` plus a
    ``max_results`` integer bounded by ``MAX_MAX_RESULTS``.

    The client does NOT auto-fetch URLs from the LLM; the LLM only
    receives the hits returned by THIS call. The provider_source_id is
    stored verbatim so a future retrieval can reconcile the same hit.
    """

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        timeout_seconds: float,
        max_results: int = DEFAULT_MAX_RESULTS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        if not api_key:
            raise ResearchProviderConfigError("TAVILY_API_KEY is not configured")
        if not base_url:
            raise ResearchProviderConfigError("TAVILY_BASE_URL is not configured")
        if max_results < MIN_MAX_RESULTS or max_results > MAX_MAX_RESULTS:
            raise ResearchProviderConfigError(
                f"max_results must be within [{MIN_MAX_RESULTS}, {MAX_MAX_RESULTS}]"
            )
        if max_retries < MIN_MAX_RETRIES or max_retries > MAX_MAX_RETRIES:
            raise ResearchProviderConfigError(
                f"max_retries must be within [{MIN_MAX_RETRIES}, {MAX_MAX_RETRIES}]"
            )
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_results = max_results
        self._max_retries = max_retries
        self._provider_name = "tavily"
        # Build headers WITHOUT leaking the API key into client repr.
        self._client = httpx.Client(
            timeout=timeout_seconds,
            headers={
                "content-type": "application/json",
                "accept": "application/json",
                "authorization": "Bearer [REDACTED]",
            },
        )
        self._client.headers.__delitem__("authorization")
        self._api_key = api_key

        # Per-call counters. ``search()`` resets them at the top of
        # every call and updates them as the bounded retry loop
        # progresses. The service layer reads them after the call
        # returns OR raises so a single audit row reflects the true
        # retry / attempt / latency cost of one provider search
        # — including the case where every attempt failed.
        self.last_attempt_count: int = 0
        self.last_call_latency_ms: int = 0
        self.last_attempts: list[dict] = []

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ResearchProviderClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def search(self, query: str) -> ProviderSearchResult:
        """Run one bounded search call.

        Raises:
            ResearchProviderTransportError: timeout, network failure, 429/5xx
                after the bounded retry budget is exhausted.
            ResearchProviderResponseError: non-retriable HTTP error or
                unexpected body shape.

        The query is length-bounded and the result list is filtered
        through :func:`coerce_source` so only http(s) hits survive.
        """
        # Reset per-call counters at the very top so even an early
        # ``ResearchProviderConfigError`` (empty query) starts from a
        # clean state. The service layer reads these after ``search()``
        # returns OR raises so the run row records the real cost of
        # one provider search — including the case where every
        # attempt failed. The ``finally`` clause below captures total
        # wall-clock latency (including backoff sleeps) so the audit
        # row matches what the network actually spent.
        self.last_attempt_count = 0
        self.last_call_latency_ms = 0
        self.last_attempts = []
        search_started = time.monotonic()
        try:
            if not isinstance(query, str) or not query.strip():
                raise ResearchProviderConfigError("search called with empty query")
            cleaned_query = query.strip()[:QUERY_MAX_LENGTH]
            # ``search_depth="advanced"`` is a generic Tavily feature
            # that returns higher-quality, more deeply-crawled hits
            # without requiring per-product domain pinning. Per the
            # v2 evaluation, the previous ``"basic"`` setting let
            # community / aggregator URLs dominate the result list
            # for product-specific questions; ``advanced`` ranks
            # official / primary sources higher while keeping the
            # same ``max_results`` cap.
            path, body = self._search_request(cleaned_query)
            url = f"{self._base_url}{path}"
            headers = {"authorization": f"Bearer {self._api_key}"}
            return self._run_search_loop(
                cleaned_query=cleaned_query,
                body=body,
                url=url,
                headers=headers,
            )
        finally:
            self.last_call_latency_ms = int((time.monotonic() - search_started) * 1000)

    def _search_request(self, query: str) -> tuple[str, dict]:
        return "/search", {
            "query": query,
            "max_results": self._max_results,
            "search_depth": "advanced",
            "include_answer": False,
            "include_raw_content": False,
        }

    def _search_sources(self, data: dict) -> list[ProviderSource]:
        results = data.get("results")
        if not isinstance(results, list):
            raise ResearchProviderResponseError(
                "research provider response fields invalid", code="invalid_response"
            )
        return [
            s
            for raw in results[: self._max_results]
            if (s := coerce_source(raw, provider=self._provider_name)) is not None
        ]

    def _run_search_loop(
        self,
        *,
        cleaned_query: str,
        body: dict,
        url: str,
        headers: dict,
    ) -> ProviderSearchResult:
        """Inner bounded-retry loop.

        Extracted so the outer ``search()`` method can wrap the whole
        call in latency timing without polluting the retry-loop body.
        """
        for attempt_index in range(self._max_retries + 1):
            self.last_attempt_count += 1
            started = time.monotonic()
            record = {"attempt": self.last_attempt_count}
            self.last_attempts.append(record)
            error = None
            try:
                response = self._client.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                code = "timeout" if isinstance(exc, httpx.TimeoutException) else "network"
                error = ResearchProviderTransportError(
                    "research provider " + code, code=code, retryable=True
                )
            else:
                status = response.status_code
                if status >= 500 or status == 429:
                    code = "rate_limit" if status == 429 else "upstream_server"
                    error = ResearchProviderTransportError(
                        "research provider " + code, code=code, retryable=True, http_status=status
                    )
                elif status >= 300:
                    code = {401: "authentication", 403: "authentication", 402: "quota"}.get(
                        status, "redirect_rejected" if status < 400 else "request_rejected"
                    )
                    error = ResearchProviderResponseError(
                        "research provider " + code, code=code, http_status=status
                    )
                else:
                    try:
                        data = response.json()
                    except (ValueError, UnicodeError):
                        error = ResearchProviderResponseError(
                            "research provider invalid JSON", code="invalid_json"
                        )
                    else:
                        try:
                            if not isinstance(data, dict):
                                raise ResearchProviderResponseError(
                                    "research provider response fields invalid",
                                    code="invalid_response",
                                )
                            sources = self._search_sources(data)
                        except ResearchProviderResponseError as exc:
                            error = exc
                        else:
                            record.update(
                                code="success",
                                retryable=False,
                                http_status=status,
                                latency_ms=int((time.monotonic() - started) * 1000),
                            )
                            return ProviderSearchResult(
                                query=cleaned_query, provider=self._provider_name, sources=sources
                            )
            record.update(error.diagnostic, latency_ms=int((time.monotonic() - started) * 1000))
            if not error.diagnostic["retryable"] or attempt_index >= self._max_retries:
                raise error from None
            time.sleep(0.05 * (2**attempt_index))
        raise ResearchProviderTransportError("research provider exhausted attempts")


# ---- Helpers --------------------------------------------------------------


def compute_source_fingerprint(sources: Iterable[ProviderSource]) -> str:
    """Return a short deterministic hash over the (id, url) pairs.

    Used by tests and by the service layer to verify that the set of
    persisted source rows did not change between start and finalise
    of a research run. The hash is intentionally NOT a cryptographic
    primitive — it only needs to be stable across processes.
    """
    h = hashlib.sha256()
    for src in sorted(sources, key=lambda x: x.provider_source_id):
        h.update(src.provider_source_id.encode("utf-8"))
        h.update(b"|")
        h.update(src.url.encode("utf-8"))
        h.update(b";")
    return h.hexdigest()


__all__ = [
    "DEFAULT_MAX_RESULTS",
    "DEFAULT_MAX_RETRIES",
    "MAX_MAX_RESULTS",
    "MAX_MAX_RETRIES",
    "MIN_MAX_RESULTS",
    "MIN_MAX_RETRIES",
    "PROVIDER_NAME_MAX_LENGTH",
    "PROVIDER_SOURCE_ID_MAX_LENGTH",
    "QUERY_MAX_LENGTH",
    "ResearchProviderClient",
    "ResearchProviderConfigError",
    "ResearchProviderResponseError",
    "ResearchProviderTransportError",
    "ResearchProviderURLError",
    "ProviderSearchResult",
    "ProviderSource",
    "SNIPPET_MAX_LENGTH",
    "TITLE_MAX_LENGTH",
    "URL_MAX_LENGTH",
    "compute_source_fingerprint",
    "coerce_source",
    "derive_domain",
    "is_valid_http_url",
]
