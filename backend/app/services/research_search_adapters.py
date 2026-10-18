"""Independent HTTP discovery adapters; official reading/grounding stays downstream."""

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.services.research_provider import (
    ProviderSource,
    ResearchProviderClient,
    ResearchProviderConfigError,
    ResearchProviderResponseError,
    coerce_source,
)


class MiniMaxSearchClient(ResearchProviderClient):
    """Coding Plan search HTTP contract, without an MCP runtime dependency."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._provider_name = "minimax"

    def _search_request(self, query: str) -> tuple[str, dict]:
        return "/v1/coding_plan/search", {"q": query}

    def _search_sources(self, data: dict) -> list[ProviderSource]:
        base = data.get("base_resp")
        if not isinstance(base, dict) or type(base.get("status_code")) is not int:
            raise ResearchProviderResponseError(
                "search response fields invalid", code="invalid_response"
            )
        if base["status_code"] != 0:
            code = "authentication" if base["status_code"] == 1004 else "application_error"
            raise ResearchProviderResponseError("search provider " + code, code=code)
        rows = data.get("organic")
        if not isinstance(rows, list):
            raise ResearchProviderResponseError(
                "search response fields invalid", code="invalid_response"
            )
        sources = []
        seen = set()
        for row in rows[: self._max_results]:
            if not isinstance(row, dict) or not isinstance(row.get("link"), str):
                continue
            url = row["link"]
            source = coerce_source(
                {
                    "id": "minimax-" + hashlib.sha256(url.encode()).hexdigest()[:32],
                    "url": url,
                    "title": row.get("title"),
                    "content": row.get("snippet"),
                },
                provider="minimax",
            )
            if source is not None and url not in seen:
                sources.append(source)
                seen.add(url)
        return sources


def configured_providers(settings) -> tuple[str, ...]:
    primary = getattr(settings, "research_search_provider", "tavily")
    fallback = getattr(settings, "research_search_fallback", None)
    return tuple(dict.fromkeys(p for p in (primary, fallback) if p))


def official_language_candidate(url: str, allowed) -> str | None:
    """Propose a locale variant for independent reading; never translate evidence.

    Same HTTPS host only. The candidate must pass the unchanged official
    policy. Unsupported legacy hosts and namespaces stay rejected.
    """
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            return None
    except ValueError:
        return None
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if any(k == "hl" for k, _ in query):
        query = [(k, "en" if k == "hl" else v) for k, v in query]
    localized = re.match(r"^/[a-z]{2}-[a-zA-Z]{2}(?=/)", parsed.path)
    paths = [parsed.path]
    if localized:
        remainder = parsed.path[localized.end() :]
        paths = [remainder, "/en-us" + remainder]
    for path in paths:
        candidate = urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query), ""))
        if allowed(candidate):
            return candidate
    return None


def require_search_config(settings) -> None:
    # Validate before the Run starts. No credential discovery from desktop config.
    for name in configured_providers(settings):
        key = settings.tavily_api_key if name == "tavily" else settings.minimax_search_api_key
        url = settings.tavily_base_url if name == "tavily" else settings.minimax_search_base_url
        if not key or not url:
            raise ResearchProviderConfigError(
                "search provider configuration missing", code="configuration"
            )


def build_search_client(settings, name: str, *, tavily_client=ResearchProviderClient):
    if name == "tavily":
        cls, key, url = tavily_client, settings.tavily_api_key, settings.tavily_base_url
    elif name == "minimax":
        cls, key, url = (
            MiniMaxSearchClient,
            settings.minimax_search_api_key,
            settings.minimax_search_base_url,
        )
    else:
        raise ResearchProviderConfigError("search provider unsupported", code="configuration")
    return cls(
        api_key=key,
        base_url=url,
        timeout_seconds=settings.tavily_timeout_seconds,
        max_results=settings.tavily_max_results,
        max_retries=settings.tavily_max_retries,
    )
