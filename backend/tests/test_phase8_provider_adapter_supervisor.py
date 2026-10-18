"""Search diagnostics are closed values; credentials and bodies never escape."""

import json
import traceback

import httpx
import pytest

from app.services import research_provider as provider

SECRET = "fake-provider-secret-must-not-escape"


def client_for(handler, cls=provider.ResearchProviderClient, retries=0):
    client = cls(
        api_key=SECRET, base_url="https://search.invalid", timeout_seconds=1, max_retries=retries
    )
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "authentication", False),
        (403, "authentication", False),
        (402, "quota", False),
        (429, "rate_limit", True),
        (503, "upstream_server", True),
        (400, "request_rejected", False),
        (302, "redirect_rejected", False),
    ],
)
def test_http_failures_have_safe_classification(status, code, retryable):
    with client_for(lambda r: httpx.Response(status, text=SECRET)) as client:
        with pytest.raises(
            (provider.ResearchProviderTransportError, provider.ResearchProviderResponseError)
        ) as caught:
            client.search("official upload help")
        diagnostic = caught.value.diagnostic
        assert diagnostic == {"code": code, "retryable": retryable, "http_status": status}
        assert SECRET not in json.dumps(client.last_attempts)
        assert SECRET not in "".join(traceback.format_exception(caught.value))
        assert client.last_attempt_count == 1


@pytest.mark.parametrize(
    "error,code", [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network")]
)
def test_network_errors_suppress_original_exception(error, code):
    def handler(r):
        raise error(SECRET, request=r)

    with client_for(handler, retries=1) as client:
        with pytest.raises(provider.ResearchProviderTransportError) as caught:
            client.search("upload")
        assert caught.value.diagnostic == {"code": code, "retryable": True}
        assert client.last_attempt_count == len(client.last_attempts) == 2
        assert SECRET not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "body,code", [(SECRET, "invalid_json"), (json.dumps({"results": SECRET}), "invalid_response")]
)
def test_invalid_body_never_leaks(body, code):
    with client_for(lambda r: httpx.Response(200, text=body)) as client:
        with pytest.raises(provider.ResearchProviderResponseError) as caught:
            client.search("upload")
        assert caught.value.diagnostic["code"] == code
        assert SECRET not in "".join(traceback.format_exception(caught.value))


def test_minimax_independent_http_contract_and_stable_namespaced_ids():
    from app.services.research_search_adapters import MiniMaxSearchClient

    requests = []

    def handler(r):
        requests.append(r)
        assert r.url.path == "/v1/coding_plan/search"
        assert json.loads(r.content) == {"q": "upload official help"}
        return httpx.Response(
            200,
            json={
                "base_resp": {"status_code": 0},
                "organic": [
                    {
                        "link": "https://support.microsoft.com/en-us/onedrive/upload",
                        "title": "Upload",
                        "snippet": "Discovery only",
                    },
                    {"link": "file:///private", "title": "bad"},
                ],
            },
        )

    with client_for(handler, MiniMaxSearchClient) as client:
        first = client.search("upload official help")
        second = client.search("upload official help")
        assert first.provider == "minimax"
        assert len(first.sources) == 1
        assert first.sources[0].provider_source_id.startswith("minimax-")
        assert first.sources[0].provider_source_id == second.sources[0].provider_source_id
        assert client.last_attempt_count == 1


def test_minimax_application_error_is_not_empty_success():
    from app.services.research_search_adapters import MiniMaxSearchClient

    with client_for(
        lambda r: httpx.Response(
            200, json={"base_resp": {"status_code": 1004, "status_msg": SECRET}, "organic": []}
        ),
        MiniMaxSearchClient,
    ) as client:
        with pytest.raises(provider.ResearchProviderResponseError) as caught:
            client.search("upload")
        assert caught.value.diagnostic["code"] == "authentication"
        assert SECRET not in str(caught.value)


def test_explicit_minimax_config_does_not_require_tavily_or_read_mcp_config():
    from app.db.config import Settings
    from app.services.research_search_adapters import configured_providers, require_search_config

    settings = Settings(
        _env_file=None,
        research_search_provider="minimax",
        minimax_search_api_key=SECRET,
        tavily_api_key=None,
    )
    assert configured_providers(settings) == ("minimax",)
    require_search_config(settings)


def test_default_search_provider_stays_tavily_and_duplicate_fallback_is_deduplicated():
    from app.db.config import Settings
    from app.services.research_search_adapters import configured_providers

    assert configured_providers(Settings(_env_file=None)) == ("tavily",)
    assert configured_providers(Settings(_env_file=None, research_search_fallback="tavily")) == (
        "tavily",
    )


@pytest.mark.parametrize(
    "original,expected",
    [
        (
            "https://support.google.com/photos/answer/6193313?hl=zh-Hant&co=GENIE.Platform%3DAndroid",
            "https://support.google.com/photos/answer/6193313?hl=en&co=GENIE.Platform%3DAndroid",
        ),
        (
            "https://help.dropbox.com/zh-cn/create-upload/upload-mobile",
            "https://help.dropbox.com/create-upload/upload-mobile",
        ),
        (
            "https://support.microsoft.com/zh-tw/onedrive/upload",
            "https://support.microsoft.com/onedrive/upload",
        ),
        ("https://help.dropbox.com/zh-cn/login", None),
        ("https://support.office.com/zh-cn/onedrive/upload", None),
        ("https://user:secret@help.dropbox.com/zh-cn/create-upload/upload-mobile", None),
        ("http://help.dropbox.com/zh-cn/create-upload/upload-mobile", None),
        ("https://help.dropbox.com:8443/zh-cn/create-upload/upload-mobile", None),
    ],
)
def test_locale_discovery_is_only_a_candidate_inside_unchanged_policy(original, expected):
    from app.services.research_search_adapters import official_language_candidate
    from app.services.research_service import (
        _OFFICIAL_VENDOR_RULES,
        _is_official_vendor_host,
        _is_official_vendor_path,
    )

    rules = list(_OFFICIAL_VENDOR_RULES.values())

    def allowed(u):
        return any(_is_official_vendor_host(u, r) and _is_official_vendor_path(u, r) for r in rules)

    assert official_language_candidate(original, allowed) == expected
